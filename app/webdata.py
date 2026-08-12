"""看板页面数据组装：概览、行为计数器、交易历史、规则日志。

按职责拆成了几个模块，本模块是聚合入口（路由与模板只 import 这里）：
  webformat  金额/时间/质量标签/新鲜度（含全站北京时间口径）
  webcharts  曲线 SVG
  webstatus  系统状态页：数据新鲜度、能力维度、系统健康

尽量把计算放这里（可测），FastAPI 路由只做请求解析与渲染。
所有金额用 Decimal，展示层格式化。
"""
from __future__ import annotations

import csv
import io
import json
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from decimal import Decimal

from markdown_it import MarkdownIt
from markupsafe import Markup

from . import pnl as pnl_calc
from . import reconcile
from .config import Config
from .counting import load_decisions, trading_counts
from .decisions import decision_key
from .decisions import (
    classification_complete as classification_complete_fn,
    group_decisions, kind_summary as kind_summary_fn,
)
from .db import Database, to_decimal
from .market_hours import market_session, session_label
from . import position_plans as position_plans_service
from . import technical as technical_mod
from .position_pnl import (
    aggregate_current_cycle_realized, aggregate_realized_for_position,
    execution_identity_key, revalue_position, valid_price,
)
from .sectors import load_sector_map
# 拆分后的展示层模块。这里重新导出，使既有调用点（webapp / 模板 / 测试）无需改动。
from .webcharts import (  # noqa: F401
    find_gaps, render_equity_svg, render_series_svg,
)
from .webformat import (  # noqa: F401
    CONF_HIGH, CONF_LABEL, CONF_LOW, CONF_MEDIUM, CONF_UNAVAILABLE, DISPLAY_TZ,
    QUALITY_LABEL, _account, _et_date, _et_day_start_utc, _et_month_start_utc,
    abs_time, clean_decimals, freshness, money, num, pct, rel_time, signed_money,
    stop_confidence,
)
from .webstatus import (  # noqa: F401
    capabilities, data_state_panel, data_status_bar, market_panel, risk_panel,
    system_health,
)

_JOURNAL_MARKDOWN = MarkdownIt(
    "commonmark", {"html": False, "breaks": True, "linkify": False},
).enable(["table", "strikethrough"])

# ---------------- 概览页 ----------------

def behavior_counters(cfg: Config, db: Database, now: datetime) -> dict:
    """行为计数。

    「本月成交笔数」曾读 orders 表（恒空）而首页显示 0，同时历史页从 transactions
    数出 2、规则页从 executions 数出 2 —— 同一个「笔数」在三个页面上三个数。
    现在统一走 counting.trading_counts，并把口径写进名字里。
    """
    tz = cfg.market_tz
    month_start = _et_month_start_utc(now, tz)
    counts = trading_counts(db, tz, month_start_iso=month_start,
                            today_et=now.astimezone(tz).date().isoformat())
    fills = db.filled_orders_since(month_start)

    # 逆势加仓：本月内在「已持有且当时浮亏」的标的上同向加仓的次数（启发式）
    counter_adds = 0
    for o in fills:
        side = (o["side"] or "").upper()
        symbol = o["symbol"]
        if not symbol:
            continue
        snap = db.snapshot_before(symbol, o["ts"])
        if not snap:
            continue
        qty = to_decimal(snap["qty"]) or Decimal("0")
        upl = to_decimal(snap["upl"]) or Decimal("0")
        if side == "BUY" and qty > 0 and upl < 0:            # 加多亏损多头
            counter_adds += 1
        elif side == "SELL_SHORT" and qty < 0 and upl < 0:   # 加空亏损空头
            counter_adds += 1

    # ---- 二、R6 状态：从未触发时不得显示「— 小时」（那读起来像数据缺失）----
    last_cd = db.last_cooldown()
    hours_since = None
    if last_cd:
        started = datetime.fromisoformat(last_cd["started_at"])
        hours_since = (now - started).total_seconds() / 3600

    cd = db.active_cooldown(now.isoformat())
    cooldown_active = cd is not None
    cooldown_ends = None
    remaining_hours = None
    if cd:
        ends = datetime.fromisoformat(cd["ends_at"])
        cooldown_ends = ends.astimezone(DISPLAY_TZ).strftime("%m-%d %H:%M")
        remaining_hours = max((ends - now).total_seconds() / 3600, 0)

    # 未达阈值的普通已实现亏损事件：有过亏损但没触发 R6，这与「从未亏过」不是一回事。
    # 读 executions（45 秒同步）而非 transactions（每日 cron）—— 否则当天的亏损
    # 要等到次日凌晨才出现在这张卡片上。
    month_losses = db.realized_losses_from_executions(month_start)
    if cooldown_active:
        r6_state = "ACTIVE"
    elif last_cd is not None:
        r6_state = "TRIGGERED_BEFORE"
    else:
        r6_state = "NEVER"

    return {
        # 口径写进名字：decision / execution / risk-increasing 三者不可混用
        "decisions_month": counts.decisions_month,
        "executions_month": counts.executions_month,
        "risk_increasing_today": counts.risk_increasing_today_total,
        "risk_increasing_by_symbol": counts.risk_increasing_today,
        "kinds_month": counts.kinds_month,
        "classification_complete": counts.classification_complete_month,
        "counter_adds": counter_adds,
        # R6
        "r6_state": r6_state,
        "hours_since_big_loss": None if hours_since is None else round(hours_since, 1),
        "cooldown_active": cooldown_active,
        "cooldown_ends": cooldown_ends,
        "cooldown_remaining_hours": (None if remaining_hours is None
                                     else round(remaining_hours, 1)),
        "minor_loss_events": len(month_losses),
    }


def pnl_cards(cfg: Config, db: Database, now: datetime | None = None) -> dict:
    """收益卡片：净资产 / 今日 / 近7日 / 近30日 / 今日已实现 / 当前未实现。

    每项都带截至时间与数据质量。窗口口径见 app.pnl：资金流未覆盖完整只能叫
    「净资产变动」，历史不足不得叫「近 N 日」。
    """
    now = now or datetime.now(timezone.utc)
    tz = cfg.market_tz
    account = _account(cfg, db)
    today_et = pnl_calc.et_date_of(now, tz)
    # 「今日盈亏」的今日 = IBKR 交易会话日；凌晨时段它仍是上一个交易日
    session_et = pnl_calc.pnl_session_date(now, tz)
    bal = db.latest_balances()
    p = db.latest_pnl(account)

    net_liq = to_decimal(bal["equity"]) if bal else None
    daily = to_decimal(p["daily_pnl"]) if p and p["daily_pnl"] is not None else None
    realized = to_decimal(p["realized_pnl"]) if p and p["realized_pnl"] is not None else None
    unrealized = to_decimal(p["unrealized_pnl"]) if p and p["unrealized_pnl"] is not None else None
    pnl_quality = p["quality"] if p else "UNAVAILABLE"
    pnl_as_of = p["as_of"] if p else None

    # 今日百分比：分母 = 会话日**之前**那个交易日的有效收盘净资产。
    # 用 session_et 而非 today_et —— 否则凌晨会拿会话日自己的日终值当分母。
    prev_date = (date.fromisoformat(session_et) - timedelta(days=1)).isoformat()
    prev_close = db.daily_equity_on_or_before(account, prev_date)
    today_flows = db.list_cash_flows(account, session_et, session_et)
    covered_today = pnl_calc.coverage_complete(
        db.cash_flow_coverages(account), session_et, session_et)
    dp = pnl_calc.daily_pct(daily_pnl=daily, prev_close_row=_row_dec(prev_close),
                            has_unreconciled_flows=bool(today_flows) and not covered_today,
                            net_liquidation=net_liq)

    windows = []
    earliest = db.earliest_daily_equity(account)
    coverages = db.cash_flow_coverages(account)
    # 交易基线：最后一次入金/出金之后的点。从它算起窗口内无资金流 = 纯盈亏。
    full_series = [
        {"et_date": r["et_date"], "net_liquidation": to_decimal(r["net_liquidation"])}
        for r in db.daily_equity_series(account, None, today_et)
    ]
    external_flows = db.list_cash_flows(account)
    baseline = pnl_calc.trading_baseline(
        full_series, weekday_of=lambda d: date.fromisoformat(d).weekday(),
        cash_flow_dates=[f["et_date"] for f in external_flows])
    # 历史窗口截至 IBKR 当前收益会话日。例如美东开盘前仍属于
    # 上一交易会话，不能把当天未完成的净资产快照混入历史收益。
    account_returns = _account_daily_returns(db, account, session_et)
    # 近 7 日（滚动窗口）
    target_start = (date.fromisoformat(today_et) - timedelta(days=7)).isoformat()
    start_row = db.daily_equity_on_or_before(account, target_start)
    flows7 = pnl_calc.flows_sum(
        _flow_dec(f) for f in db.list_cash_flows(
            account, _next_day_str(start_row["et_date"]) if start_row else target_start,
            today_et))
    cov7 = pnl_calc.coverage_with_tail(
        coverages, start_row["et_date"] if start_row else target_start,
        today_et)["ok"]
    daily7 = [r for r in account_returns
              if r["et_date"] > target_start and r["amount"] is not None]
    if (start_row is not None and earliest is not None
            and earliest["et_date"] <= target_start and daily7):
        start_value = to_decimal(start_row["net_liquidation"])
        amount7 = sum((r["amount"] for r in daily7), Decimal("0"))
        windows.append(pnl_calc.WindowResult(
            days=7, status=pnl_calc.PNL, label="近7日盈亏",
            amount=amount7,
            pct=_compound_daily_pct(daily7),
            start_date=start_row["et_date"], end_date=session_et,
            start_value=start_value, end_value=net_liq, flows=Decimal("0"),
            days_available=(date.fromisoformat(today_et)
                            - date.fromisoformat(earliest["et_date"])).days,
            note="IBKR 日终净资产差额减 Flex/IBKR 官方外部资金流",
            coverage_ok=True,
        ))
    else:
        windows.append(pnl_calc.compute_window(
            days=7, today_et=today_et, end_value=net_liq,
            start_row=_row_dec(start_row), earliest_row=_row_dec(earliest),
            flows=flows7, coverage_ok=cov7, baseline_row=baseline))
    # 历史盈亏（自入金后全部）—— 替代原「近 30 日」
    valid_returns = [r for r in account_returns if r["amount"] is not None]
    if valid_returns and earliest is not None:
        inception_amount = sum((r["amount"] for r in valid_returns), Decimal("0"))
        official_history = db.flex_nav_daily_series(account, None, session_et)
        inception_base = (to_decimal(official_history[0]["starting_value"])
                          if official_history else to_decimal(earliest["net_liquidation"]))
        inception_start = valid_returns[0]["et_date"]
        windows.append(pnl_calc.WindowResult(
            days=(date.fromisoformat(session_et) - date.fromisoformat(inception_start)).days,
            status=pnl_calc.PNL, label="历史盈亏", amount=inception_amount,
            pct=_compound_daily_pct(valid_returns),
            start_date=inception_start, end_date=session_et,
            start_value=inception_base, end_value=net_liq, flows=Decimal("0"),
            days_available=(date.fromisoformat(session_et)
                            - date.fromisoformat(inception_start)).days,
            note="IBKR 净资产变化逐日汇总，仅扣除 Flex/IBKR 官方外部资金流",
            coverage_ok=True,
        ))
    else:
        windows.append(pnl_calc.compute_inception(
            today_et=today_et, end_value=net_liq,
            baseline_row=baseline, earliest_row=_row_dec(earliest)))

    official_history = db.flex_nav_daily_series(account, None, session_et)
    starts = ([earliest["et_date"]] if earliest else [])
    if official_history:
        starts.append(official_history[0]["et_date"])
    history_start = min(starts) if starts else None
    days_available = ((date.fromisoformat(today_et) - date.fromisoformat(history_start)).days
                      if history_start else 0)
    return {
        "net_liquidation": net_liq,
        "net_liq_as_of": bal["ts"] if bal else None,
        "net_liq_quality": (bal["data_quality"] or "READY") if bal else "UNAVAILABLE",
        "daily": daily, "daily_pct": dp,
        "realized": realized, "unrealized": unrealized,
        "pnl_quality": pnl_quality, "pnl_as_of": pnl_as_of,
        "pnl_source": p["source"] if p else None,
        "daily_basis": (p["daily_basis"] if p else None),
        "reset_policy": p["reset_policy"] if p else None,
        "session_date": session_et,
        "session_is_prior_day": session_et != today_et,
        "windows": windows,
        "days_available": days_available,
        "history_start": history_start,
    }


def _row_dec(row):
    """daily_equity 行 -> {et_date, net_liquidation:Decimal}；None 透传。"""
    if row is None:
        return None
    return {"et_date": row["et_date"], "net_liquidation": to_decimal(row["net_liquidation"])}


def _flow_dec(row) -> dict:
    return {"amount": to_decimal(row["amount"]), "et_date": row["et_date"]}


def _next_day_str(d: str) -> str:
    return (date.fromisoformat(d) + timedelta(days=1)).isoformat()


def _compound_daily_pct(rows: list[dict]) -> Decimal | None:
    """把每日收益率按时间加权方式链接。

    Flex Change in NAV 的 twr 是官方每日时间加权收益率；有入金、
    资产转移时，不能用「累计盈亏 ÷ 期初小额净资产」作为期间收益率。
    """
    usable = [r for r in rows if r.get("amount") is not None]
    if not usable or any(r.get("pct") is None for r in usable):
        return None
    factor = Decimal("1")
    for r in usable:
        factor *= Decimal("1") + to_decimal(r["pct"])
    return factor - Decimal("1")


def _nav_flow_label(row: dict) -> str | None:
    """Change in NAV 资本变动的官方类型，避免把资产转移误称为入金。"""
    labels = []
    if to_decimal(row.get("deposits_withdrawals")) != 0:
        labels.append("入金/出金")
    if to_decimal(row.get("asset_transfers")) != 0:
        labels.append("资产转移")
    if to_decimal(row.get("internal_cash_transfers")) != 0:
        labels.append("内部现金转移")
    if not labels and to_decimal(row.get("capital_movements")) != 0:
        labels.append("其他资本变动")
    return "、".join(labels) if labels else None


def _account_daily_returns(db: Database, account: str, end_date: str,
                           start_date: str | None = None) -> list[dict]:
    """账户历史日收益的唯一出口。

    总收益不是成交 realizedPNL：后者只含已平仓部分。这里使用 IBKR 官方 NAV
    的逐日差额，并且只扣 Flex/IBKR 明确记录的外部资金流。reqPnL 只用于实时卡片，
    不再跨日归档，因为其重置日程不是固定的日历边界。
    """
    rows = [
        {"et_date": r["et_date"],
         "net_liquidation": to_decimal(r["net_liquidation"])}
        for r in db.daily_equity_series(account, start_date, end_date)
    ]
    flows_by_date: dict[str, Decimal] = {}
    for f in db.list_cash_flows(account, start_date, end_date):
        d = f["et_date"]
        flows_by_date[d] = flows_by_date.get(d, Decimal("0")) + to_decimal(f["amount"])
    result = pnl_calc.daily_returns(
        rows, flows_by_date, weekday_of=lambda d: date.fromisoformat(d).weekday())
    by_date = {r["et_date"]: r for r in result}
    official = db.flex_nav_daily_series(account, start_date, end_date)
    if official:
        first_official = official[0]["et_date"]
        last_official = official[-1]["et_date"]
        official_dates = {r["et_date"] for r in official}
        # Flex 已覆盖的区间内，周末/节假日本地快照不是官方收益行。
        # 快照采样时差可产生几美元假变动，必须整个排除。
        by_date = {
            d: r for d, r in by_date.items()
            if not (first_official <= d <= last_official and d not in official_dates)
        }
    for o in official:
        d = o["et_date"]
        amount = to_decimal(o["total_pnl"])
        twr = to_decimal(o["twr"])
        row = by_date.get(d, {
            "et_date": d, "net_liquidation": to_decimal(o["ending_value"]),
            "has_jump": False,
        })
        row.update({
            "amount": amount,
            # Flex TWR 是百分数：21.83 表示 21.83%，展示层内部用 0.2183。
            "pct": (twr / Decimal("100")) if twr is not None else None,
            "has_flow": to_decimal(o["capital_movements"]) != 0,
            "flow": to_decimal(o["capital_movements"]),
            "flow_label": _nav_flow_label(o),
            "source": "FLEX_CHANGE_IN_NAV",
        })
        by_date[d] = row
    return [by_date[d] for d in sorted(by_date)]


# 曲线时间范围（自然日；ALL=全部历史，含入金前阶段）
CURVE_RANGES = {"1D": 1, "7D": 7, "30D": 30, "3M": 90, "YTD": None, "ALL": None}


def _md_hm(ts_iso: str) -> str:
    try:
        return datetime.fromisoformat(ts_iso).astimezone(DISPLAY_TZ).strftime("%m-%d %H:%M")
    except (TypeError, ValueError):
        return ts_iso


def _ymd_hm(ts_iso: str) -> str:
    try:
        return datetime.fromisoformat(ts_iso).astimezone(DISPLAY_TZ).strftime("%Y-%m-%d %H:%M")
    except (TypeError, ValueError):
        return ts_iso or ""


def journal_entries(cfg: Config, db: Database) -> list[dict]:
    """投资日记：按更新时间倒序，安全渲染 Markdown。"""
    from .position_plan_view import format_datetime

    out = []
    for e in db.list_journal_entries():
        edited = e["updated_at"] != e["created_at"]
        out.append({
            "id": e["id"],
            "content": e["content"],
            # MarkdownIt 禁用原始 HTML，并拒绝 javascript: 等危险链接；Markup 只标记
            # 这段由解析器生成的 HTML，编辑框仍使用未经转换的原文。
            "content_html": Markup(_JOURNAL_MARKDOWN.render(e["content"])),
            "created_disp": format_datetime(e["created_at"], cfg.local_tz),
            "updated_disp": format_datetime(e["updated_at"], cfg.local_tz),
            "edited": edited,
            "source_type": e.get("source_type") or "MANUAL",
            "source_label": ("持仓周期归档" if e.get("source_type") == "POSITION_PLAN_ARCHIVE"
                             else "手动日记"),
            "position_cycle_id": e.get("position_cycle_id"),
            "account": e.get("account"),
            "is_readonly": bool(e.get("is_readonly")),
            "archived_at": (format_datetime(e.get("archived_at"), cfg.local_tz)
                            if e.get("archived_at") else None),
        })
    return out


def _hhmm(ts_iso: str) -> str:
    """盘中曲线的 x 轴标签：北京时间 HH:MM（与全站展示时区一致）。"""
    try:
        return datetime.fromisoformat(ts_iso).astimezone(DISPLAY_TZ).strftime("%H:%M")
    except (TypeError, ValueError):
        return ts_iso[11:16]


def _curve_markers(cfg: Config, db: Database, account: str, start_date, end_date):
    """曲线上的事件标记：成交、R1/R2 触发。日期一律按美东交易日归组。"""
    tz = cfg.market_tz
    lo = _et_day_start_utc(None, tz, start_date) if start_date else "0000"
    hi = _et_day_start_utc(None, tz, (date.fromisoformat(end_date)
                                      + timedelta(days=1)).isoformat())
    trades: dict[str, int] = {}
    for r in db.executions_between(lo, hi):
        d = _et_date(r["execution_time"], tz)
        trades[d] = trades.get(d, 0) + 1
    alerts: dict[str, set] = {}
    for e in db.recent_rule_events(500):
        if e["rule_id"] not in ("R1", "R2") or not (lo <= e["ts"] < hi):
            continue
        alerts.setdefault(_et_date(e["ts"], tz), set()).add(e["rule_id"])
    return (
        [{"date": d, "n": n} for d, n in sorted(trades.items())],
        [{"date": d, "rules": "·".join(sorted(r))} for d, r in sorted(alerts.items())],
    )


def equity_curve_data(cfg: Config, db: Database, rng: str = "7D",
                      now: datetime | None = None) -> dict:
    """净资产 / 净资产变动 / 资金流调整后盈亏 —— 一张图三个系列，标签切换。

    默认 7D；历史不足 7 天时自动回退 ALL（否则默认视图是一张空图）。
    非 ALL 时起点裁到最近一笔**已登记**的资金流之后 = 当前账户阶段。
    """
    now = now or datetime.now(timezone.utc)
    tz = cfg.market_tz
    account = _account(cfg, db)
    today_et = pnl_calc.et_date_of(now, tz)
    rng = rng if rng in CURVE_RANGES else "7D"

    earliest = db.earliest_daily_equity(account)
    all_official_rows = db.flex_nav_daily_series(account, None, today_et)
    history_starts = ([earliest["et_date"]] if earliest else [])
    if all_official_rows:
        history_starts.append(all_official_rows[0]["et_date"])
    earliest_date = min(history_starts) if history_starts else None
    days_avail = ((date.fromisoformat(today_et) - date.fromisoformat(earliest_date)).days
                  if earliest_date else 0)
    auto_all = False
    if (rng not in ("ALL", "YTD", "1D") and CURVE_RANGES[rng]
            and days_avail < CURVE_RANGES[rng]):
        rng, auto_all = "ALL", True      # 历史不足，默认范围会是空图 -> 自动放全

    if rng == "ALL":
        start_date = None
    elif rng == "YTD":
        start_date = date.fromisoformat(today_et).replace(month=1, day=1).isoformat()
    else:
        start_date = (date.fromisoformat(today_et)
                      - timedelta(days=CURVE_RANGES[rng])).isoformat()

    flows = db.list_cash_flows(account)
    phase_note = None
    if rng not in ("ALL", "1D") and flows:
        last_flow = flows[-1]["et_date"]
        if start_date is None or last_flow > start_date:
            start_date = last_flow
            phase_note = f"起点已裁至最近一笔资金流（{last_flow}）之后，入金不计为收益"

    range_note = None
    if rng == "1D":
        # 1D 走盘中分钟级快照：daily_equity 一天只有一个点，画不出当日走势
        intra = db.intraday_equity(account, _et_day_start_utc(now, tz, today_et))
        points = [(_hhmm(ts), to_decimal(v)) for ts, v in intra]
        quality_by_date = {}
        if intra:
            # 一个美东交易日在北京时间是跨日的（ET 09:30–16:00 = 北京 21:30–次日 04:00），
            # x 轴只有 HH:MM 会让「12:00 → 11:22」看起来像倒序。这里把范围讲清楚。
            range_note = (f"美东 {today_et} 交易日 · 北京时间 "
                          f"{_md_hm(intra[0][0])} → {_md_hm(intra[-1][0])}")
    else:
        rows = db.daily_equity_series(account, start_date, today_et)
        official_rows = [r for r in all_official_rows
                         if (start_date is None or r["et_date"] >= start_date)]
        first_official = official_rows[0]["et_date"] if official_rows else None
        last_official = official_rows[-1]["et_date"] if official_rows else None
        # 官方报表已覆盖的历史区间，完全使用 Flex 日结；只保留报表
        # 区间之外的实时快照，避免周末快照和采样时差污染历史曲线。
        point_by_date = {
            r["et_date"]: to_decimal(r["net_liquidation"])
            for r in rows
            if not (first_official and first_official <= r["et_date"] <= last_official)
        }
        quality_by_date = {
            r["et_date"]: r["quality"] for r in rows
            if r["et_date"] in point_by_date
        }
        for r in official_rows:
            point_by_date[r["et_date"]] = to_decimal(r["ending_value"])
            quality_by_date[r["et_date"]] = "IBKR_FLEX_OFFICIAL"
        points = sorted(point_by_date.items())

    marks = [
        {"date": f["et_date"], "amount": to_decimal(f["amount"]),
         "note": f["note"] or ("入金" if to_decimal(f["amount"]) > 0 else "出金")}
        for f in flows
        if (start_date is None or f["et_date"] >= start_date) and f["et_date"] <= today_et
    ]
    marked_dates = {m["date"] for m in marks}
    for r in ([] if rng == "1D" else official_rows):
        label = _nav_flow_label(r)
        if label and r["et_date"] not in marked_dates:
            marks.append({"date": r["et_date"],
                          "amount": to_decimal(r["capital_movements"]),
                          "note": f"IBKR 官方{label}"})
    marks.sort(key=lambda m: m["date"])
    if rng == "1D":
        trades, alerts, gaps = [], [], []      # 盘中视图不叠日级标记
    else:
        trades, alerts = _curve_markers(cfg, db, account,
                                        points[0][0] if points else today_et, today_et)
        gaps = find_gaps(points)
    cov = pnl_calc.coverage_with_tail(
        db.cash_flow_coverages(account),
        points[0][0] if points else today_et, today_et)
    coverage_ok = cov["ok"]

    official_rows = ([] if rng == "1D" else [
        r for r in all_official_rows
        if (not points or r["et_date"] >= points[0][0])
    ])
    official_by_date = {r["et_date"]: to_decimal(r["total_pnl"])
                        for r in official_rows}

    # 系列 2/3/4：IBKR NAV 差额减去 Flex/IBKR 官方外部资金流。
    # reqPnL 的重置日程不固定，绝不能再把每个内部日期的最后一条跨日累加。
    change, adjusted, daily_pnl = [], [], []
    if points:
        base = points[0][1]
        by_date: dict[str, Decimal] = {}
        for f in flows:
            if points[0][0] < f["et_date"] <= today_et:
                by_date[f["et_date"]] = (by_date.get(f["et_date"], Decimal("0"))
                                         + to_decimal(f["amount"]))
        run = Decimal("0")
        prev_adj = Decimal("0")
        for i, (d, v) in enumerate(points):
            run += by_date.get(d, Decimal("0"))
            change.append((d, v - base))
            adj = v - base - run
            adjusted.append((d, adj))              # 累计盈亏，资金流不计入
            # **每日盈亏 = 当日累计 - 昨日累计。** 看的是「今天赚了多少」，
            # 而不是「从起点至今赚了多少」—— 后者是趋势，前者才是节奏。
            # 第一个点没有前一日，留空而不是当成 0（那会画出一根假的零柱）。
            if i > 0:
                daily_pnl.append((d, adj - prev_adj))
            prev_adj = adj
        if official_by_date:
            fallback_by_date = dict(daily_pnl)
            daily_pnl, adjusted = [], []
            cumulative = Decimal("0")
            for i, (d, _) in enumerate(points):
                # 首个点若有 Flex 官方当日收益，也必须画出；
                # 只有纯 NAV 兜底的首点才因缺前值留空。
                amount = official_by_date.get(d)
                if amount is None and i > 0:
                    amount = fallback_by_date.get(d)
                if amount is not None:
                    daily_pnl.append((d, amount))
                    cumulative += amount
                adjusted.append((d, cumulative))
    pnl_series_available = bool(daily_pnl)
    expected_dates = [d for d, _ in points[1:]] if len(points) > 1 else []
    official_complete = bool(expected_dates) and all(d in official_by_date
                                                     for d in expected_dates)
    performance_coverage_ok = coverage_ok or official_complete
    if official_by_date and official_complete:
        pnl_source = "FLEX_CHANGE_IN_NAV"
    elif official_by_date:
        pnl_source = "FLEX_CHANGE_IN_NAV+IBKR_NAV_FALLBACK"
    else:
        pnl_source = "IBKR_NAV_MINUS_EXTERNAL_FLOW"

    common = dict(marks=marks, trades=trades, alerts=alerts, gaps=gaps,
                  quality_by_date=quality_by_date)
    # 各周期是否有足够历史：需要的自然日 > 现有历史即禁用（点了也是同一张图）。
    # 1D/YTD/ALL 永远可用（ALL/YTD 就是"有多少显示多少"，1D 是当日）。
    ranges_meta = []
    for name in CURVE_RANGES:
        need = CURVE_RANGES[name]
        enough = name in ("1D", "YTD", "ALL") or need is None or days_avail >= need
        ranges_meta.append({"name": name, "enough": enough})

    return {
        "range": rng, "ranges": list(CURVE_RANGES), "ranges_meta": ranges_meta,
        "auto_all": auto_all,
        "range_note": range_note,
        "days_available": days_avail,
        "points": points, "cum_pnl": change, "adjusted": adjusted,
        "daily_pnl": daily_pnl,
        "marks": marks, "trades": trades, "alerts": alerts, "gaps": gaps,
        "phase_note": phase_note, "coverage_ok": coverage_ok,
        "covered_through": cov["covered_through"],
        "uncovered_tail_days": cov["uncovered_tail_days"],
        "tail_note": None if official_complete else cov["note"],
        "start_date": points[0][0] if points else None,
        "end_date": points[-1][0] if points else None,
        # 三个系列：净资产 / 净资产变动 / 资金流调整后盈亏
        "svg_net": render_series_svg(points, **common),
        "svg_change": render_series_svg(change, **common, value_fmt="{:+,.0f}"),
        "svg_adjusted": (render_series_svg(adjusted, **common, value_fmt="{:+,.0f}")
                         if performance_coverage_ok else None),
        "svg_daily_pnl": (render_series_svg(daily_pnl, **common,
                                            value_fmt="{:+,.0f}", zero_line=True)
                          if performance_coverage_ok else None),
        "pnl_series_source": pnl_source,
        "default_series": "adjusted" if performance_coverage_ok else "net",
        "locked_note": (None if performance_coverage_ok
                        else "Flex/IBKR 外部资金流覆盖未完整"),
        "flow_note": None,
        # 兼容旧字段
        "svg": render_series_svg(points, **common),
        "cum_title": (pnl_calc.PNL_TITLE if performance_coverage_ok
                      else pnl_calc.CHANGE_ONLY_TITLE),
        "show_return": performance_coverage_ok,
        "note": (None if performance_coverage_ok
                 else pnl_calc.FLOW_INCOMPLETE_NOTE),
    }


#: 单笔风险的参考上限（与 stop_loss 模块同一口径）
_RISK_LIMIT_PCT = Decimal("0.02")


def live_risk(*, price, stop, qty, side: str, equity) -> dict:
    """用**当前**持仓量与现价换算单笔风险。

    stop_levels 里的 risk_amount/risk_pct/suggested_trim 是每日收盘后按当时持仓量
    算的，盘中一加减仓就失真。止损价（由 ATR/swing 推出）不依赖仓位，可以沿用；
    但风险敞口必须跟着当前仓位走。

    **价格已越过止损时**（price < stop 的多头）：不显示负的风险额 —— 那是
    「-2.87%」这种看起来很安全、实则最危险的数字。改为 breached=True，
    并给出已越过的幅度。
    """
    from decimal import ROUND_CEILING, ROUND_FLOOR
    qty_abs = abs(qty or Decimal("0"))
    if price is None or stop is None or qty_abs == 0:
        return {"distance": None, "risk_amount": None, "risk_pct": None,
                "exceeds": False, "trim": 0, "target": None, "breached": False,
                "beyond_stop": None}
    risk_ps = (price - stop) if side == "LONG" else (stop - price)
    distance = ((price - stop) / price) if side == "LONG" else ((stop - price) / price)

    if risk_ps < 0:
        # 已跌破（多头）/涨破（空头）止损位：风险已经兑现，不是「负风险」
        return {"distance": distance, "risk_amount": None, "risk_pct": None,
                "exceeds": False, "trim": 0, "target": None, "breached": True,
                "beyond_stop": -risk_ps * qty_abs}

    risk_amount = risk_ps * qty_abs
    risk_pct = (risk_amount / equity) if (equity and equity > 0) else None
    exceeds = risk_pct is not None and risk_pct > _RISK_LIMIT_PCT
    trim = 0
    target = None                            # 2% 预算内可持股数（参考上限）
    if exceeds and risk_ps > 0 and equity and equity > 0:
        tgt = (_RISK_LIMIT_PCT * equity / risk_ps).to_integral_value(ROUND_FLOOR)
        target = int(tgt) if tgt > 0 else 0
        over = qty_abs - tgt
        trim = int(over.to_integral_value(ROUND_CEILING)) if over > 0 else 0
    return {"distance": distance, "risk_amount": risk_amount, "risk_pct": risk_pct,
            "exceeds": exceeds, "trim": trim, "target": target,
            "breached": False, "beyond_stop": None}


def _price_label(session: str, is_live: bool, quality: str, fallback: str) -> str:
    """按市场时段给价格措辞。

    盘中/盘前/盘后的延迟价都是「当时段的实时价」，可分别叫盘中价/盘前价/盘后价；
    收盘冻结价（DELAYED_FROZEN）或休市时段一律「收盘价」——那不是当下的市场价。
    """
    from .market_hours import CLOSED, OVERNIGHT, POST, PRE, REGULAR
    if not is_live:
        return fallback                    # MISSING/FROZEN 等，用质量标签
    return {REGULAR: "现价", PRE: "盘前价", POST: "盘后价",
            OVERNIGHT: "隔夜价", CLOSED: "收盘价"}.get(session, "现价")


def prev_close_for(db: Database, symbol: str, session_et: str) -> Decimal | None:
    """前收 = **会话日之前**最后一根日线的收盘。

    不能用 stop_levels.close —— 那是「最近一根」收盘，盘后正是当前展示的价格本身，
    自己减自己恒等于 0（今日盈亏会一直显示 +0.00）。
    """
    for b in reversed(db.get_daily_bars(symbol, limit=5)):
        if b["date"] < session_et:
            return b["c"]
    return None


#: 持仓状态建议的严重度分级（供「建议」列 + 排序 + 配色统一）。
_ADVICE_CRIT = "crit"
_ADVICE_WARN = "warn"
_ADVICE_INFO = "info"
_ADVICE_OK = "ok"


#: 合成建议动作 → 建议列的告警等级
_RECO_ADVICE_LEVEL = {
    "BLOCKED": _ADVICE_INFO, "EXIT": _ADVICE_CRIT, "REDUCE": _ADVICE_WARN,
    "DO_NOT_ADD": _ADVICE_WARN, "ADD_WATCH": _ADVICE_OK, "HOLD": _ADVICE_OK,
    "REVIEW": _ADVICE_WARN,
}


def _advice_from_reco(reco) -> dict:
    """把合成建议(recommendation_snapshots 行)映射成建议列的 {level,label,detail}。

    合成建议已综合止损/仓位上限/正式计划/技术/市场并保留优先级，比纯风险预算的
    _position_advice 更权威，故在有建议时作为「建议」列的主体；数字仍来自后端。
    """
    from . import recommendation_service as reco_
    action = reco["action"]
    detail = reco["reason"] or ""
    qty = reco["quantity"]
    if qty:
        detail = f"{detail}（{qty} 股）" if detail else f"{qty} 股"
    return {
        "level": _RECO_ADVICE_LEVEL.get(action, _ADVICE_INFO),
        "label": reco_.ACTION_LABEL.get(action, action),
        "detail": detail,
        "from_reco": True,
        "decided_by": reco_.DECIDED_BY_LABEL.get(reco["decided_by"], reco["decided_by"]),
    }


def _position_advice(row: dict) -> dict:
    """把一只持仓的处置建议归纳成 {level, label, detail}，供「建议」列直接展示。

    所有数量都取自后端已算好的字段（target/trim），前端不自行估算。
    仅在无合成建议时作为回退（合成建议见 _advice_from_reco）。
    """
    if row.get("price") is None:
        return {"level": _ADVICE_INFO, "label": "等待更新", "detail": "暂无行情"}
    if row.get("breached"):
        beyond = row.get("beyond_stop")
        return {"level": _ADVICE_CRIT, "label": "已触发止损",
                "detail": (f"现价越过止损位 {money(beyond)}" if beyond else "现价已越过止损位")}
    if row.get("exceeds"):
        target, trim = row.get("target"), row.get("trim")
        if target is not None and trim:
            return {"level": _ADVICE_WARN, "label": "建议减仓",
                    "detail": f"降至 {target:,} 股以内（约减 {trim:,} 股）"}
        return {"level": _ADVICE_WARN, "label": "建议减仓", "detail": "超出风险预算"}
    # 接近止损：距止损 < 3%（还没跌破，但很近）
    dist = row.get("distance")
    if dist is not None and dist <= Decimal("0.03"):
        return {"level": _ADVICE_WARN, "label": "接近止损",
                "detail": f"距止损仅 {pct(dist)}"}
    if row.get("stop") is None:
        return {"level": _ADVICE_INFO, "label": "无止损位",
                "detail": row.get("stop_note") or "日线不足，未计算"}
    return {"level": _ADVICE_OK, "label": "风险正常", "detail": "无需调整"}


def overview_data(cfg: Config, db: Database, now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    tz = cfg.market_tz
    session_et = pnl_calc.pnl_session_date(now, tz)
    bal = db.latest_balances()
    equity = to_decimal(bal["equity"]) if bal else None
    stops = {r["symbol"]: r for r in db.latest_stop_levels()}
    session = market_session(now, tz)      # 盘前/盘中/盘后/休市
    # 当前持仓的已实现盈亏按「最近一次净持仓为零之后」的成交周期计算。
    # reqPnLSingle 是交易日窗口值，不能覆盖历史；Flex 历史记录也不能跨周期
    # 直接按 symbol SUM，否则新建仓但没有卖出的 MU 会继承旧周期收益。
    execution_rows = db.executions_between("0000")
    # 当日有成交的标的：它们的今日盈亏不能按前收估算（成本不是前收）
    traded_today = {
        r["symbol"] for r in db.executions_between(
            _et_day_start_utc(now, tz, session_et))
    }

    # 合成建议（Phase 2）：按标的取当前建议，融入「建议」列。旁路，不可拖垮首页。
    try:
        from . import recommendation_service as _reco
        reco_acct = _reco._resolve_account(cfg, db)
        reco_by_sym = {r["symbol"]: r for r in db.current_recommendations(reco_acct)} \
            if reco_acct else {}
    except Exception:  # noqa: BLE001
        reco_by_sym = {}

    positions = []
    for p in db.latest_positions():
        sym = p["symbol"]
        qty = to_decimal(p["qty"])
        current_realized = aggregate_realized_for_position(
            execution_rows, p, account=getattr(cfg, "ib_account", None))
        # 价格只认行情快照。禁止 market_value/qty 反推 —— 那是没有质量标记的账面价，
        # 与规则引擎读到的不是同一个东西，正是首页与 R7 互相矛盾的根源。
        price = valid_price(p["price"])
        quality = p["price_quality"] or "MISSING"
        label, is_live, hint = QUALITY_LABEL.get(quality, (quality, False, ""))
        # 价格措辞按市场时段区分：盘中「现价」、盘前「盘前价」、盘后「盘后价」、
        # 休市/收盘冻结「收盘价」。有实时价格才谈时段，否则回到质量标签本身。
        price_label = _price_label(session, is_live, quality, label)
        s = stops.get(sym)
        mv = to_decimal(p["market_value"])
        row = {
            "symbol": sym, "qty": qty, "avg_cost": to_decimal(p["avg_cost"]),
            "market_value": mv, "upl": to_decimal(p["upl"]),
            "price": price,
            "price_label": price_label,
            "price_source": p["price_source"],
            "quote_source": p["source"] or "",
            "quote_source_label": ("富途" if (p["source"] or "").startswith("FUTU")
                                   else ("IBKR" if (p["source"] or "").startswith("IBKR")
                                         else "未知来源")),
            "quote_is_realtime": is_live,
            "quality": quality, "quality_label": label, "quality_hint": hint,
            "quality_css": "ok" if is_live else ("warn" if price is not None else "crit"),
            "quote_as_of": p["quote_as_of"],
            "quote_rel": rel_time(p["quote_as_of"], now) if p["quote_as_of"] else "无时点",
            "day_pnl": None, "day_change": None, "day_pct": None,
            "day_pnl_source": None,
            "realized_pnl": (current_realized.value
                             if current_realized.value is not None else
                             (to_decimal(p["realized_pnl"])
                              if p["realized_pnl"] is not None else None)),
            "realized_pnl_source": (current_realized.source
                                     if current_realized.value is not None
                                     else p["realized_pnl_source"]),
            "prev_close": None,
            "stop": None, "side": None, "distance": None,
            "risk_pct": None, "risk_amount": None, "exceeds": False,
            "reached_1r": False, "trim": 0, "target": None, "stop_computed_date": None,
            "breached": False, "beyond_stop": None,
            "stop_weak": False, "stop_note": None, "confidence": None,
            "advice": None,
        }
        # 今日盈亏：优先用 IBKR 逐持仓 PnL（reqPnLSingle）。
        row["day_pnl"] = to_decimal(p["day_pnl"]) if p["day_pnl"] is not None else None
        row["day_pnl_source"] = p["day_pnl_source"]
        row["day_pnl_is_estimate"] = False
        # 日涨跌是**标的**的价格变化，与持仓数量和建仓时点无关。
        quote_prev_close = valid_price(p["previous_close"])
        prev_close = (quote_prev_close if quote_prev_close and quote_prev_close > 0
                      else prev_close_for(db, sym, session_et))
        row["prev_close"] = prev_close
        # 休市/无实时行情：用**最近一根日线收盘**作展示价，用户总能看到一个价格。
        # 用 latest close 而非 prev_close：展示要的是当前最新收盘，不是「前一日」。
        # 只用于展示，不喂进 live_risk（下方风险判断仍以实时价 gated，收盘价不参与
        # 止损穿越判断——与能力页「收盘价不参与」口径一致）。
        if price is None:
            bars = db.get_daily_bars(sym, limit=1)
            last_close = bars[-1]["c"] if bars else None
            if last_close and last_close > 0:
                row["price"] = last_close
                row["price_label"] = "收盘价"
                row["quality_css"] = "warn"
                row["quote_source_label"] = "IBKR"
                row["quality_label"] = "收盘价"
                row["quality_hint"] = "最近交易日日线收盘价，非实时"
                row["quote_rel"] = "最近交易日"
        display_price = row["price"]
        # 现价一旦可用，市值与浮动盈亏必须从同一价格重算；行情失败则保留旧的
        # 有效快照值，避免单只股票缺行情把整个持仓表变成 0/空值。
        revalue_position(row, price=display_price, source=row.get("quote_source"))
        if display_price is not None and prev_close and prev_close > 0:
            row["day_change"] = display_price - prev_close
            row["day_pct"] = row["day_change"] / prev_close
        # 降级：reqPnLSingle 盘中拿不到（需实时行情）。若该标的**当日无成交**，
        # 则 (现价-前收)×数量 就是准确的今日盈亏；当日有成交时成本不是前收，
        # 自算会严重偏离（实测自算合计 -2784 vs 实际 -690.84），此时宁可留空。
        if row["day_pnl"] is None and price is not None and prev_close and qty:
            if sym in traded_today:
                row["stop_note"] = row["stop_note"]     # 保持不变
                row["day_pnl_source"] = "当日有成交，无法按前收估算"
            else:
                row["day_pnl"] = (price - prev_close) * qty
                row["day_pnl_source"] = "估算：(现价 − 前收) × 数量 · 当日无成交"
                row["day_pnl_is_estimate"] = True
        if s and price is not None:
            stop = to_decimal(s["stop_price"]); side = s["side"]
            # 风险额/占比/参考上限用**当前**持仓量与现价实时换算。
            # stop_levels 是每日收盘后算的，里面的 risk_amount 用的是当时的持仓量 ——
            # 盘中加减仓后那个数字就废了（线上实测：DRAM 早上 3.9 股、盘中 574.9 股，
            # 风险占比仍显示按 3.9 股算的 0.04%）。止损价本身不依赖仓位，可沿用。
            r = live_risk(price=price, stop=stop, qty=qty, side=side, equity=equity)
            row.update({
                "stop": stop, "side": side,
                "distance": r["distance"],
                "risk_amount": r["risk_amount"],
                "risk_pct": r["risk_pct"],
                "exceeds": r["exceeds"],
                "trim": r["trim"],
                "target": r["target"],
                "breached": r["breached"],
                "beyond_stop": r["beyond_stop"],
                "reached_1r": bool(s["reached_1r"]),
                "stop_weak": not bool(s["data_sufficient"]),
                "stop_note": s["data_note"],
                "stop_computed_date": s["computed_date"],
            })
        elif price is not None:
            # 无止损位 ≠ 无风险：日线不足 5 根等情况必须显式标出，不能静默留空
            row["stop_note"] = "无止损位（日线不足，未计算）"
        row["confidence"] = stop_confidence(s, price, quality)
        # 风险预算参考（估算，不是交易指令）：目标 = 2% 预算内可持股数，
        # trim = 建议减持股数。文案在模板里区分「参考上限」与「建议减持」，避免歧义。
        row["risk_budget_note"] = None       # 旧的合并提示行已废弃，改列内展示
        # 近止损标记：距止损 ≤3% 且未跌破。独立于建议文案，专供行样式高亮，
        # 这样「建议」列换成合成建议后仍保留「接近止损」的视觉提示。
        dist = row.get("distance")
        row["near_stop"] = bool(dist is not None and not row.get("breached")
                                and dist <= Decimal("0.03"))
        # 建议列：有合成建议就用它（已综合止损/上限/计划/技术/市场），否则回退风险建议
        reco = reco_by_sym.get(sym)
        row["advice"] = _advice_from_reco(reco) if reco else _position_advice(row)
        positions.append(row)

    # 排序：一律按市值降序（重仓在前）。跌破止损/超风险靠行内标记提示，不再插队打乱市值顺序。
    _enrich_positions(positions)
    positions.sort(key=lambda q: -(q["market_value"] or Decimal("0")))

    # 技术面简报：给每只持仓挂一句技术信号（详情见 /technical）。旁路，不可拖垮首页。
    try:
        t_session = market_session(now, cfg.market_tz)
        stops_t = {r["symbol"]: r["stop_price"] for r in db.latest_stop_levels()}
        for q in positions:
            conf = technical_mod.confirmation_basis(
                t_session, price_source=q.get("price_source"),
                quote_source=q.get("quote_source"))
            tc = technical_mod.compute_symbol(
                q["symbol"], db.get_daily_bars(q["symbol"], limit=600), q.get("price"),
                stop_price=stops_t.get(q["symbol"]), confirmation=conf)
            dt = tc["daily_trend_state"]
            q["tech"] = {
                "summary": tc["technical_summary"],
                "css": ("good" if dt in ("强多头", "多头趋势")
                        else "crit" if dt in ("中期趋势破坏", "长期弱势") else "warn"),
            }
    except Exception:  # noqa: BLE001
        pass

    dynamic_unrealized = _complete_unrealized(positions)
    pnl = pnl_cards(cfg, db, now)
    if dynamic_unrealized is not None:
        pnl["unrealized"] = dynamic_unrealized
        pnl["pnl_source"] = "当前价 × IBKR 持仓成本"

    quote_status = _quote_status_summary(positions, now, session)
    try:
        plan_account = bal["account"] if bal is not None else None
        position_plans = position_plans_service.dashboard_for_account(
            db, plan_account, now=now.isoformat())
    except Exception:  # noqa: BLE001 — 计划旁路不可影响原有看板
        position_plans = {"account": None, "cards": [], "counts": {
                              "current_positions": 0, "active": 0, "pending": 0, "draft": 0,
                              "review_due": 0, "triggered": 0, "plan_outside": 0},
                          "error": "持仓计划数据暂不可用"}

    # 计划卡片与上方「当前持仓」表保持同序：按持仓表已排好的 symbol 顺序重排。
    if position_plans.get("cards"):
        pos_order = {p["symbol"]: i for i, p in enumerate(positions)}
        position_plans["cards"].sort(
            key=lambda c: pos_order.get(c.get("symbol"), len(pos_order)))

    return {
        "balances": bal,
        "positions": positions,
        "position_plans": position_plans,
        "quote_status": quote_status,
        "counters": behavior_counters(cfg, db, now),
        "status": data_status_bar(cfg, db, now),
        "pnl": pnl,
        "risk": risk_summary(cfg, db, positions, now),
        "day_pnl_check": _day_pnl_check(
            cfg, db, positions, account_unrealized=dynamic_unrealized),
        "total_cash": (to_decimal(bal["total_cash"])
                       if bal and bal["total_cash"] is not None else None),
        "market_session": session,
        "market_session_label": session_label(session),
    }


def _realized_by_symbol(db: Database, execution_rows=None, positions=None,
                        account: str | None = None) -> dict[str, tuple[Decimal, str]]:
    """返回当前持仓页的周期已实现盈亏（兼容旧调用点）。

    ``positions`` 存在时按每只持仓的 conid/currency 等身份计算；没有持仓
    上下文时也只按单一身份组的当前周期返回，绝不再读取 Flex 全历史 SUM。
    """
    rows = execution_rows if execution_rows is not None else db.executions_between("0000")
    out: dict[str, tuple[Decimal, str]] = {}
    if positions is not None:
        for position in positions:
            result = aggregate_realized_for_position(rows, position, account=account)
            symbol = position["symbol"] if not isinstance(position, dict) else position.get("symbol")
            if symbol and result.value is not None:
                out[str(symbol)] = (result.value, result.source or "executions")
        return out

    # No position identity is available: only return symbols for which all
    # executions resolve to one identity group.  Ambiguous symbols are omitted
    # rather than merged across conids/currencies/accounts.
    groups: dict[tuple, list] = {}
    for row in rows:
        symbol = row["symbol"] if not isinstance(row, dict) else row.get("symbol")
        if symbol:
            groups.setdefault(execution_identity_key(row), []).append(row)
    by_symbol: dict[str, list] = {}
    for key, group in groups.items():
        symbol = key[1]
        if symbol:
            by_symbol.setdefault(symbol, []).append(group)
    for symbol, groups_for_symbol in by_symbol.items():
        if len(groups_for_symbol) != 1:
            continue
        result = aggregate_current_cycle_realized(groups_for_symbol[0])
        if result.value is not None:
            out[symbol] = (result.value, result.source or "executions")
    return out


def _complete_unrealized(positions: list[dict]) -> Decimal | None:
    """只有所有当前持仓都有有效价格时才汇总，避免部分数据冒充账户总额。"""
    if not positions:
        return Decimal("0")
    if any(p.get("upl") is None for p in positions):
        return None
    return sum((p["upl"] for p in positions), Decimal("0"))


def _quote_status_summary(positions: list[dict], now: datetime, session: str) -> dict:
    """持仓区行情状态摘要；来源与实时性分别统计，避免“有价格=实时”的误导。"""
    total = len(positions)
    live = sum(1 for p in positions if p.get("quote_is_realtime"))
    futu_count = sum(1 for p in positions if p.get("quote_source_label") == "富途")
    futu_live = sum(1 for p in positions
                    if p.get("quote_is_realtime") and p.get("quote_source_label") == "富途")
    ibkr_fallback = sum(1 for p in positions if p.get("quote_source_label") == "IBKR")
    missing = sum(1 for p in positions if p.get("price") is None)
    timestamps = []
    for p in positions:
        try:
            timestamps.append(datetime.fromisoformat(p["quote_as_of"]))
        except (KeyError, TypeError, ValueError):
            pass
    latest = max(timestamps).isoformat() if timestamps else None

    if total == 0:
        label, css = "无持仓", "muted"
    elif session == "CLOSED":
        label, css = "休市 · 非实时", "muted"
    elif futu_live == total:
        label, css = "富途实时", "ok"
    elif live == total:
        label, css = "实时（含 IBKR 回退）", "ok"
    elif live:
        label, css = "部分实时 · 已回退", "warn"
    elif ibkr_fallback:
        label, css = "IBKR 回退 · 非实时", "warn"
    else:
        label, css = "行情不可用", "flag"
    return {
        "label": label, "css": css, "total": total, "live": live,
        "futu_count": futu_count,
        "futu_live": futu_live, "ibkr_fallback": ibkr_fallback,
        "missing": missing, "latest_as_of": latest,
        "latest_rel": rel_time(latest, now) if latest else "无可信时点",
    }


#: 「重点仓位」的权重门槛。占已投资仓位 20% 以上的，值得单独看一眼。
#: 这不是风险阈值 —— 只是展示层的强调，不参与任何规则判断。
KEY_POSITION_WEIGHT = Decimal("0.20")


def _enrich_positions(rows: list[dict]) -> None:
    """补上盈亏率与仓位权重。**就地修改，不新建结构。**

    两个数原本都没有：
      · 盈亏率 = 浮动盈亏 / 成本    —— 只看金额无法比较不同规模的仓位，
        「亏 5000」在 1 万的仓位上是灾难，在 50 万的仓位上是噪音
      · 权重   = 市值 / 多头市值合计 —— 识别「重点仓位」的唯一依据

    分母为 0 或缺失时一律返回 None，**绝不填 0** —— 那会让「算不出来」
    看起来像「没有盈亏」。
    """
    invested = sum((r["market_value"] for r in rows
                    if r.get("market_value") and r["market_value"] > 0),
                   Decimal("0"))
    for r in rows:
        qty, cost = r.get("qty"), r.get("avg_cost")
        mv, upl = r.get("market_value"), r.get("upl")

        basis = (abs(qty) * cost) if (qty is not None and cost is not None) else None
        r["cost_basis"] = basis
        r["upl_pct"] = (upl / basis) if (basis and basis != 0
                                         and upl is not None) else None
        r["weight"] = (mv / invested) if (invested and invested != 0
                                          and mv is not None) else None
        r["is_key"] = bool(r["weight"] is not None
                           and r["weight"] >= KEY_POSITION_WEIGHT)


def _day_pnl_check(cfg: Config, db: Database, positions: list[dict],
                   account_unrealized: Decimal | None = None):
    """持仓浮盈合计 vs 账户**未实现** —— 同口径对照。

    原先拿它跟账户 DailyPnL 比：DailyPnL 含已实现，而持仓表里没有已平仓的标的，
    于是任何有平仓的交易日都必然显示「🔴 不一致」（线上 -317 vs -3032），
    数字全对，错的是口径。已实现改作信息项单独展示。
    """
    p = db.latest_pnl(_account(cfg, db))
    if account_unrealized is None:
        account_unrealized = (to_decimal(p["unrealized_pnl"])
                              if p and p["unrealized_pnl"] is not None else None)
    realized_today = (to_decimal(p["realized_pnl"])
                      if p and p["realized_pnl"] is not None else None)
    return reconcile.check_unrealized(
        position_upls=[q["upl"] for q in positions],
        account_unrealized=account_unrealized,
        realized_today=realized_today,
    )


def risk_summary(cfg: Config, db: Database, positions: list[dict],
                 now: datetime | None = None) -> dict:
    """首页风险摘要：最大单标的集中度、最大板块集中度、最高单笔风险、未处理告警。"""
    now = now or datetime.now(timezone.utc)
    bal = db.latest_balances()
    equity = to_decimal(bal["equity"]) if bal else None
    sector_map = load_sector_map(cfg.sector_map_path)

    top_symbol = top_ratio = None
    by_sector: dict[str, Decimal] = {}
    for q in positions:
        mv = abs(q["market_value"] or Decimal("0"))
        if equity and equity > 0:
            r = mv / equity
            if top_ratio is None or r > top_ratio:
                top_ratio, top_symbol = r, q["symbol"]
        sec = sector_map.get(q["symbol"])
        if sec:
            by_sector[sec] = by_sector.get(sec, Decimal("0")) + mv
    top_sector = top_sector_ratio = None
    if equity and equity > 0:
        for sec, tot in by_sector.items():
            r = tot / equity
            if top_sector_ratio is None or r > top_sector_ratio:
                top_sector_ratio, top_sector = r, sec

    worst = None
    for q in positions:
        if q["risk_pct"] is None:
            continue
        if worst is None or q["risk_pct"] > worst["risk_pct"]:
            worst = q

    # 首页只展示统一事件 registry 中仍为 open 的硬约束 Top 3。
    # registry 已按 episode 去重，不能退回 occurrence/旧 rule_run 的“最近三条”。
    from . import risk_events as _rev
    hard_sources = sorted({_rev.SRC_ACCOUNT, _rev.SRC_POSITION, _rev.SRC_COMBINED,
                           _rev.SRC_DATA_QUALITY, _rev.SRC_BEHAVIOR})
    try:
        hard_open, hard_total = db.query_risk_events(
            status=_rev.ST_OPEN, source_type=hard_sources, limit=3)
        severity_counts = {}
        for severity in (_rev.SEV_CRITICAL, _rev.SEV_HIGH, _rev.SEV_WARNING):
            _, severity_counts[severity] = db.query_risk_events(
                status=_rev.ST_OPEN, source_type=hard_sources,
                severity=severity, limit=1)
    except Exception:  # noqa: BLE001 — 首页旁路，事件读取失败不能拖垮持仓
        hard_open, hard_total, severity_counts = [], 0, {}
    open_alerts = [{
        "event_id": e.get("event_id"), "rule": e.get("rule_code"),
        "title": e.get("title") or e.get("rule_code"),
        "detail": e.get("explanation"),
        "severity": _rev.normalize_severity(e.get("severity")),
        "symbol": e.get("affected_symbol") or e.get("affected_instrument"),
    } for e in hard_open]
    crit_alerts = severity_counts.get(_rev.SEV_CRITICAL, 0)
    high_alerts = severity_counts.get(_rev.SEV_HIGH, 0)
    warn_alerts = max(0, hard_total - crit_alerts - high_alerts)

    # 分级：严重=超阈值；接近=达阈值 90%。用 Decimal 常量算好「接近线」，
    # 避免模板里 Decimal × float 报 TypeError。
    _NEAR = Decimal("0.9")
    top_thr, sec_thr = Decimal("0.30"), Decimal("0.40")
    limit_pct = _RISK_LIMIT_PCT
    return {
        "top_symbol": top_symbol, "top_ratio": top_ratio,
        "top_threshold": top_thr, "top_near": top_thr * _NEAR,
        "top_over": (top_ratio - top_thr) if top_ratio is not None else None,
        "top_sector": top_sector, "top_sector_ratio": top_sector_ratio,
        "sector_threshold": sec_thr, "sector_near": sec_thr * _NEAR,
        "sector_over": (top_sector_ratio - sec_thr)
                       if top_sector_ratio is not None else None,
        "worst_symbol": worst["symbol"] if worst else None,
        "worst_risk_pct": worst["risk_pct"] if worst else None,
        "worst_trim": worst["trim"] if worst else 0,
        "worst_target": worst["target"] if worst else None,
        "worst_threshold": limit_pct, "worst_near": limit_pct * _NEAR,
        # 最高单笔风险必须连同可信度与降级原因一起显示
        "worst_confidence": (worst["confidence"] if worst else
                             {"level": CONF_UNAVAILABLE, "label": CONF_LABEL[CONF_UNAVAILABLE],
                              "reasons": ["无可估算的单笔风险"], "is_estimate": False}),
        "open_alerts": open_alerts,
        "hard_alerts_total": hard_total,
        "crit_alerts": crit_alerts,
        "high_alerts": high_alerts,
        "warn_alerts": warn_alerts,
    }


# ---------------- 日历页（每日收益） ----------------

def calendar_data(cfg: Config, db: Database, year: int, month: int,
                  now: datetime | None = None) -> dict:
    """某年月的日历：每格显示当日收益额与收益率。

    收益 = 当日日终净资产 − 前一有数据日 − 当日资金流。net_liq 每日按美东交易日归档，
    周末无数据格留空。月份可切换（上/下月链接由模板用 prev/next 生成）。
    """
    import calendar as _cal
    now = now or datetime.now(timezone.utc)
    tz = cfg.market_tz
    account = _account(cfg, db)

    # 取到本月为止的全部日终（要算月初第一天的收益需要上月最后一天）
    month_end = (date(year, month, _cal.monthrange(year, month)[1])).isoformat()
    rows = [{"et_date": r["et_date"], "net_liquidation": to_decimal(r["net_liquidation"])}
            for r in db.daily_equity_series(account, None, month_end)]
    returns = {r["et_date"]: r for r in _account_daily_returns(
        db, account, month_end)}

    # 月网格（周一为每周第一天）
    _cal.setfirstweekday(_cal.MONDAY)
    weeks = []
    month_amount = Decimal("0"); have_any = False
    for week in _cal.monthcalendar(year, month):
        cells = []
        for day in week:
            if day == 0:
                cells.append(None)
                continue
            d = date(year, month, day).isoformat()
            r = returns.get(d)
            if r and r["amount"] is not None:
                have_any = True
                month_amount += r["amount"]
            cells.append({
                "day": day, "et_date": d,
                "amount": r["amount"] if r else None,
                "pct": r["pct"] if r else None,
                "has_flow": r["has_flow"] if r else False,
                "flow_label": r.get("flow_label") if r else None,
                "has_jump": r["has_jump"] if r else False,
                "is_future": d > pnl_calc.et_date_of(now, tz),
            })
        weeks.append(cells)

    # 上/下月
    prev_y, prev_m = (year - 1, 12) if month == 1 else (year, month - 1)
    next_y, next_m = (year + 1, 1) if month == 12 else (year, month + 1)
    return {
        "year": year, "month": month,
        "weeks": weeks,
        "weekday_labels": ["一", "二", "三", "四", "五", "六", "日"],
        "month_amount": month_amount if have_any else None,
        "prev": {"year": prev_y, "month": prev_m},
        "next": {"year": next_y, "month": next_m},
        "has_data": have_any,
    }


def calendar_year_data(cfg: Config, db: Database, year: int,
                       now: datetime | None = None) -> dict:
    """整年视图：12 个月各自的收益额与收益率，供「按年」查看。

    每月收益 = 该月各交易日官方总收益之和；月收益率链接每日
    Flex TWR。这样入金、出金或资产转移不会被误当成投资收益。
    """
    now = now or datetime.now(timezone.utc)
    tz = cfg.market_tz
    account = _account(cfg, db)
    year_end = date(year, 12, 31).isoformat()
    daily = _account_daily_returns(db, account, year_end)

    today = pnl_calc.et_date_of(now, tz)
    cy, cm = int(today[:4]), int(today[5:7])
    labels = ["1 月", "2 月", "3 月", "4 月", "5 月", "6 月",
              "7 月", "8 月", "9 月", "10 月", "11 月", "12 月"]
    months = []
    year_amount = Decimal("0"); year_any = False
    for m in range(1, 13):
        prefix = f"{year:04d}-{m:02d}"
        amt = Decimal("0"); has = False
        month_rows = [r for r in daily if r["et_date"].startswith(prefix)]
        for r in month_rows:
            if r["amount"] is None:
                continue
            amt += r["amount"]; has = True
        pct = _compound_daily_pct(month_rows) if has else None
        months.append({
            "month": m, "label": labels[m - 1],
            "amount": amt if has else None, "pct": pct, "has_data": has,
            "is_future": (year, m) > (cy, cm),
        })
        if has:
            year_amount += amt; year_any = True

    return {
        "year": year,
        "months": months,
        "year_amount": year_amount if year_any else None,
        "prev_year": year - 1, "next_year": year + 1,
        "has_data": year_any,
    }


# ---------------- 交易历史页 ----------------

def history_rows(cfg: Config, db: Database, *, symbol=None, start=None, end=None,
                 action=None) -> list[dict]:
    """交易明细：直接读 **executions**（事件推送后约 1 秒落库）。

    原先读 transactions —— 那张表由每日 05:30 的 cron 同步，盘中刚成交的单要等到
    第二天凌晨才出现在页面上。而风险摘要、R4/R5、已实现盈亏用的都是 executions，
    明细表读另一张慢表，等于让同一份数据在页面上有两个进度。

    realized_pnl 直接来自 IBKR CommissionReport（券商权威值），**无需任何估算** ——
    用快照均价估算已实现盈亏的老路径（曾算出 -0.70，再减双边佣金得到错误的 -1.82）
    已随本改动一并删除。
    佣金是异步事件，可能比成交晚到几秒，此时标 pending_commission。
    """
    tz = cfg.market_tz
    rows = db.executions_between(start or "0000", end)
    out = []
    for r in rows:
        act = (r["side"] or "").upper()
        if action and action.upper() not in act:
            continue
        if symbol and r["symbol"] != symbol:
            continue
        qty = to_decimal(r["shares"])
        price = to_decimal(r["price"])
        amount = (qty * price) if (qty is not None and price is not None) else None
        if amount is not None and act == "BUY":
            amount = -amount                     # 买入为现金流出
        realized = to_decimal(r["realized_pnl"]) if r["realized_pnl"] is not None else None
        out.append({
            "date": _et_date(r["execution_time"], tz),
            "time": abs_time(r["execution_time"]),
            "ts": r["execution_time"],
            "symbol": r["symbol"],
            "action": act,
            "qty": qty,
            "price": price,
            "amount": amount,
            "fees": to_decimal(r["commission"]),
            "realized": realized,
            "realized_is_estimate": False,       # executions 的 realized 就是券商值
            "realized_source": "IBKR_CommissionReport" if realized is not None else None,
            "exec_id": r["exec_id"],
            # 佣金/已实现是异步事件，可能比成交回报晚到
            "pending_commission": r["commission"] is None,
        })
    out.sort(key=lambda x: x["ts"], reverse=True)
    return out


CSV_HEADERS = ["日期", "标的", "方向", "数量", "价格", "金额", "手续费", "盈亏"]


def history_csv(cfg: Config, db: Database, **filters) -> str:
    rows = history_rows(cfg, db, **filters)
    buf = io.StringIO()
    buf.write("﻿")  # UTF-8 BOM，便于 Excel 正确识别中文
    w = csv.writer(buf)
    w.writerow(CSV_HEADERS)
    for r in rows:
        w.writerow([
            r["date"], r["symbol"], r["action"],
            "" if r["qty"] is None else str(r["qty"]),
            "" if r["price"] is None else str(r["price"]),
            "" if r["amount"] is None else str(r["amount"]),
            "" if r["fees"] is None else str(r["fees"]),
            "" if r["realized"] is None else str(r["realized"]),
        ])
    return buf.getvalue()


# ---------------- 规则日志页 ----------------

_RUN_STATE_LABEL = {
    "OK": ("✅ 正常评估 · 无触发", "ok"),
    "TRIGGERED": ("🔴 有规则触发", "crit"),
    "INSUFFICIENT_DATA": ("⚠️ 数据不足 · 部分规则未判定", "warn"),
    "STALE_DATA": ("⚠️ 行情过期 · 部分规则未判定", "warn"),
    # 关键：不能说成「正常」。只有收盘冻结价时，止损穿越判断根本没执行。
    "NOT_EVALUATED": ("⚠️ 未判定 · 行情质量不足以执行判断", "warn"),
    "PARTIALLY_EVALUATED": ("⚠️ 部分判定 · 部分标的行情质量不足", "warn"),
    "ERROR": ("🟠 规则运行异常", "warn"),
}


def rule_run_status(cfg: Config, db: Database, now: datetime | None = None) -> dict:
    """最近一次规则运行的可解释状态；区分 从未运行/无触发/数据不足/过期/异常。"""
    tz = cfg.market_tz
    row = db.latest_rule_run()
    if row is None:
        return {
            "state": "NEVER_RUN", "label": "⚪ 从未运行", "css": "warn",
            "detail": "规则引擎尚未执行过——同步服务是否在运行？",
            "run_time": None, "account_ready": None, "rules": [],
        }
    try:
        evals = json.loads(row["evaluations"])
    except (TypeError, ValueError):
        evals = []
    label, css = _RUN_STATE_LABEL.get(row["run_status"], (row["run_status"], "warn"))
    run_dt = datetime.fromisoformat(row["run_ts"]).astimezone(DISPLAY_TZ)
    stale = ""
    if now is not None:
        age = (now - datetime.fromisoformat(row["run_ts"])).total_seconds()
        if age > 600:  # 最近一次运行超过 10 分钟 → 引擎可能已停
            stale = f"（距今 {age/60:.0f} 分钟，引擎可能未在运行）"
    # 行情质量摘要：与首页读同一批快照的同一字段，两页不可能给出矛盾结论
    try:
        qsum = json.loads(row["quote_quality_summary"] or "{}")
    except (TypeError, ValueError):
        qsum = {}
    quote_summary = "、".join(
        f"{QUALITY_LABEL.get(k, (k,))[0]}×{v}" for k, v in sorted(qsum.items())
    )
    return {
        "state": row["run_status"], "label": label, "css": css,
        "detail": f"最近评估 {run_dt:%Y-%m-%d %H:%M}{stale}",
        "run_time": f"{run_dt:%Y-%m-%d %H:%M}",
        "account_ready": bool(row["account_ready"]),
        "rules": evals,
        "sync_run_id": row["sync_run_id"],
        "positions_snapshot_abs": abs_time(row["positions_snapshot_id"], tz),
        "balances_snapshot_abs": abs_time(row["balances_snapshot_id"], tz),
        # 全收盘价回退时无可信行情时点 —— 显式说明，不拿运行时间冒充
        "quotes_abs": (abs_time(row["quotes_as_of"], tz) if row["quotes_as_of"]
                       else "收盘价（无时点）"),
        "quote_summary": quote_summary,
        "summary": _json_or(row["rule_summary"], {}),
        "per_symbol": _json_or(row["per_symbol"], {}),
    }


def _json_or(raw, default):
    try:
        return json.loads(raw) if raw else default
    except (TypeError, ValueError):
        return default


def rule_events(cfg: Config, db: Database, limit: int = 200,
                now: datetime | None = None) -> list[dict]:
    """规则事件，按 UTC 时间戳倒序（DB 侧 ORDER BY ts DESC, id DESC）。

    时间展示统一走 ET 换算。旧实现把 ET 日期与 **UTC 时分** 拼在一起
    （`_et_date(ts)` + `ts[11:16]`），显示出的时刻本身就是错的。
    """
    now = now or datetime.now(timezone.utc)
    tz = cfg.market_tz
    out = []
    for e in db.recent_rule_events(limit):
        out.append({
            "time": abs_time(e["ts"], tz),
            "rel": rel_time(e["ts"], now),
            "ts": e["ts"],
            "rule_id": e["rule_id"], "severity": e["severity"],
            "symbol": e["symbol"], "title": e["title"], "message": e["message"],
        })
    return out


def _fmt_duration(seconds: float) -> str:
    if seconds < 60:
        return f"{int(seconds)}秒"
    if seconds < 3600:
        return f"{int(seconds // 60)}分钟"
    if seconds < 86400:
        return f"{seconds / 3600:.1f}小时"
    return f"{seconds / 86400:.1f}天"


def rule_alert_summary(cfg: Config, db: Database, now: datetime | None = None,
                       limit: int = 500) -> list[dict]:
    """持续存在的告警：首次触发 / 最近确认 / 持续时长。

    按 (rule_id, symbol) 聚合 —— 同一个问题反复推送不应读成许多互不相干的事件。
    """
    now = now or datetime.now(timezone.utc)
    tz = cfg.market_tz
    groups: dict[tuple, dict] = {}
    for e in db.recent_rule_events(limit):
        key = (e["rule_id"], e["symbol"])
        g = groups.get(key)
        if g is None:
            groups[key] = g = {
                "rule_id": e["rule_id"], "symbol": e["symbol"], "title": e["title"],
                "severity": e["severity"], "first_ts": e["ts"], "last_ts": e["ts"],
                "count": 0,
            }
        g["count"] += 1
        if e["ts"] < g["first_ts"]:
            g["first_ts"] = e["ts"]
        if e["ts"] > g["last_ts"]:
            g["last_ts"] = e["ts"]
            g["title"], g["severity"] = e["title"], e["severity"]
    out = []
    for g in groups.values():
        try:
            dur = (datetime.fromisoformat(g["last_ts"])
                   - datetime.fromisoformat(g["first_ts"])).total_seconds()
        except (TypeError, ValueError):
            dur = 0
        g.update({
            "first_abs": abs_time(g["first_ts"], tz),
            "last_abs": abs_time(g["last_ts"], tz),
            "last_rel": rel_time(g["last_ts"], now),
            "duration": _fmt_duration(dur) if dur > 0 else "单次",
        })
        out.append(g)
    out.sort(key=lambda g: g["last_ts"], reverse=True)
    return out


# ---------------- 交易历史汇总 ----------------

def history_summary(cfg: Config, db: Database, rows: list[dict], *,
                    start=None, end=None, now: datetime | None = None) -> dict:
    """筛选结果汇总。

    **不再展示「毛实现盈亏」**，也不再有「净 = 毛 − 双边佣金」这个公式 ——
    在 IBKR 口径下它必然算错（买入佣金已含在持仓成本里，会被扣两次）。
    页面只展示：券商已实现盈亏、佣金（成本信息）、execution 数、decision 数、对账状态。
    """
    now = now or datetime.now(timezone.utc)
    tz = cfg.market_tz
    buy_amount = sell_amount = Decimal("0")
    realized_total = Decimal("0")
    n_authoritative = n_estimate = 0
    estimate_total = Decimal("0")
    for r in rows:
        action = (r.get("action") or "").upper()
        amount = r.get("amount") or Decimal("0")
        if "SELL" in action or "COVER" in action:
            sell_amount += abs(amount)
        elif "BUY" in action:
            buy_amount += abs(amount)
        if r.get("realized") is None:
            continue
        if r.get("realized_is_estimate"):
            estimate_total += r["realized"]
            n_estimate += 1
        else:
            realized_total += r["realized"]
            n_authoritative += 1

    # execution / decision：来自 executions（有 permId），与 transactions 的行数不是一回事
    execs = db.executions_between(start or "0000", end)
    decs = group_decisions(
        execs,
        pre_qty_lookup=lambda sym, ts: db.qty_before(sym, ts),
        et_date_of=lambda ts: _et_date(ts, tz),
    )
    commission = reconcile.sum_commission(execs)

    # 账户级对账只在「同一账户 + 同一日内重置窗口」内有意义：
    # reqPnL.realizedPnL 每个交易日重置，拿它跟任意历史筛选区间对账没有意义。
    account = _account(cfg, db)
    session_et = pnl_calc.pnl_session_date(now, tz)
    day_start = (datetime.fromisoformat(f"{session_et}T00:00:00")
                 .replace(tzinfo=tz).astimezone(timezone.utc).isoformat())
    day_end = (datetime.fromisoformat(f"{session_et}T00:00:00")
               .replace(tzinfo=tz) + timedelta(days=1)).astimezone(timezone.utc).isoformat()
    covers_session = (start is None or start <= day_start) and (end is None or end >= day_end)
    p = db.latest_pnl(account)
    broker_daily_realized = (to_decimal(p["realized_pnl"])
                             if p and p["realized_pnl"] is not None else None)
    rec = reconcile.reconcile_realized(
        broker_value=broker_daily_realized if covers_session else None,
        execution_rows=db.executions_between(day_start, day_end),
    )
    return {
        "rows": len(rows),
        "executions": len(execs),
        "decisions": len(decs),
        "decision_kinds": kind_summary_fn(decs),
        "classification_complete": classification_complete_fn(decs),
        "buy_amount": buy_amount,
        "sell_amount": sell_amount,
        "realized": realized_total if n_authoritative else None,
        "realized_source": "IBKR CommissionReport.realizedPNL",
        "estimate_realized": estimate_total if n_estimate else None,
        "n_estimate": n_estimate,
        "commission": commission,
        "commission_note": "佣金仅作成本信息展示，不再从 IBKR 已实现盈亏中二次扣减",
        "reconcile": rec,
        "reconcile_scope": (f"{session_et} 交易日（账户级 PnL 每日重置，"
                            f"仅该窗口可对账）" if covers_session else
                            "所选区间跨越账户级 PnL 的每日重置窗口，无法账户级对账"),
    }


# ---------------- 口径与规则说明页 ----------------

def rule_reference(cfg: Config, thresholds=None) -> list[dict]:
    """R1–R7 的规则说明。

    阈值**从 RuleThresholds 实时读出**，不在模板里写死 —— 否则改了阈值页面还在讲旧数字。
    """
    from .rules import DEFAULT_THRESHOLDS, RULES_VERSION
    th = thresholds or DEFAULT_THRESHOLDS
    pct_ = lambda v: f"{Decimal(str(v)) * 100:.0f}%"
    return [
        {"id": "R1", "name": "单一标的集中度", "severity": "WARN",
         "threshold": f"单一标的市值 > 净资产 {pct_(th.single_concentration)}",
         "why": "单一标的过重时，个股风险等同于账户风险 —— 一次意外就能决定整体结果。",
         "source": "持仓快照市值 / 账户净资产（NetLiquidation）",
         "note": "按绝对市值计算，空头按绝对值参与。"},
        {"id": "R2", "name": "板块集中度", "severity": "WARN",
         "threshold": f"同板块合计市值 > 净资产 {pct_(th.sector_concentration)}",
         "why": "分散到多个标的但都在同一板块，实质仍是单一押注 —— 板块级利空会同时命中。",
         "source": "持仓快照市值 + 本地板块映射表",
         "note": "未配置板块映射表时该规则跳过（SKIPPED），不会静默当作通过。"},
        {"id": "R3", "name": "融资动用", "severity": "WARN",
         "threshold": "基准货币现金为负（动用融资）",
         "why": "杠杆会放大回撤，也会让强平线变成真实威胁。",
         "source": "IBKR TotalCashValue（无独立 margin balance 字段，现金为负即融资）",
         "note": "当前为单币种口径；多币种借款与 ExcessLiquidity 缓冲尚未纳入。"},
        {"id": "R4", "name": "盘中过度交易", "severity": "WARN",
         "threshold": f"同一标的当日风险增加决策 ≥ {th.intraday_symbol_fills} 次",
         "why": "同一天反复加仓同一标的，通常是情绪在驱动，不是计划在驱动。",
         "source": "executions 按 decision_key 归组（decision 口径，非成交笔数）",
         "note": "只数 OPEN/ADD/REVERSAL；REDUCE/CLOSE/强平/公司行动不计。"
                 "无法还原成交前持仓时返回 INSUFFICIENT_DATA，不会硬算。"},
        {"id": "R5", "name": "月度交易频次", "severity": "WARN",
         "threshold": f"本月主动交易决策 > {th.monthly_fills} 次",
         "why": "频次本身就是成本，也是过度交易最直接的信号。",
         "source": "executions 按 decision_key 归组（decision 口径）",
         "note": "数全部主动决策（含减仓平仓），与 R4 的「只数风险增加」口径不同。"},
        {"id": "R6", "name": "大额认亏冷静期", "severity": "CRIT",
         "threshold": f"单笔已实现亏损 > 净资产 {pct_(th.realized_loss_pct)}"
                      f" → 进入 {th.cooldown_hours} 小时冷静期",
         "why": "刚砍掉一笔大亏时最容易报复性交易，冷静期是给自己设的缓冲。",
         "source": "IBKR CommissionReport.realizedPNL（券商权威值，非估算）",
         "note": "冷静期内的新成交会额外提醒。本系统只提醒，不会阻止你在券商端下单。"},
        {"id": "R7", "name": "触及建议止损位", "severity": "CRIT / WARN",
         "threshold": "现价触及 ATR 推导的建议止损位",
         "why": "止损位是事前定的纪律，触及时最需要的是被提醒，而不是自己找理由。",
         "source": "日线 ATR + swing 推导的止损位，对比行情快照价格",
         "note": "实时行情 → CRIT；延迟行情 → 最高 WARN；"
                 "收盘冻结价/行情过期 → NOT_EVALUATED（不判定，绝不报「未触及」）；"
                 "止损位基于不足 15 根日线时降级为 WARN。"},
    ]
