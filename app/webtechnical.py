"""技术面页面数据装配：拉日线 + 现价 + 止损位 → 逐持仓算技术状态。

只读、按需计算，不落表。价格来自最近持仓快照（富途/IBKR 报价链路已有），
日线来自本地 daily_bars，止损位来自 stop_levels。
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from . import market_hours, position_plans, technical
from .config import Config
from .db import Database, to_decimal
from .webformat import DISPLAY_TZ, _account, rel_time

#: 拉多少根日线参与计算（覆盖 EMA250 预热与 52 周高点）
_BARS_LIMIT = 600
#: 报价过期阈值（秒）
_STALE_WARN = 60
_STALE_OVER = 300


def technical_page_data(cfg: Config, db: Database, now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    session = market_hours.market_session(now, cfg.market_tz)
    intraday = session == market_hours.REGULAR

    positions = db.latest_positions()
    # 每标的的确认态由报价自身时段推出（扩展时段绝不算收盘确认，见 P0-1）。
    conf_by_sym = {
        r["symbol"]: technical.confirmation_basis(
            session, price_source=r["price_source"], quote_source=r["source"])
        for r in positions}
    stops = {r["symbol"]: r["stop_price"] for r in db.latest_stop_levels()}
    # 计划面板：拿每标的的账户仓位、计划上限、是否有加仓空间（综合裁决用）
    try:
        pp = position_plans.dashboard_for_account(db, _account(cfg, db), now=now.isoformat())
        plan_by_sym = {c["symbol"]: c for c in pp.get("cards", [])}
    except Exception:  # noqa: BLE001 —— 计划旁路不可拖垮技术面
        plan_by_sym = {}

    # 市值降序，与「当前持仓」表同序
    def _mv(r):
        return abs(to_decimal(r["market_value"]) or Decimal("0"))
    rows = sorted(positions, key=_mv, reverse=True)

    cards = []
    stale_count = 0
    insufficient = 0
    for r in rows:
        symbol = r["symbol"]
        qty = to_decimal(r["qty"]) or Decimal("0")
        if qty == 0:
            continue
        price = to_decimal(r["price"])
        bars = db.get_daily_bars(symbol, limit=_BARS_LIMIT)
        conf = conf_by_sym.get(symbol, technical.CLOSE_CONFIRMED)
        card = technical.compute_symbol(
            symbol, bars, price,
            stop_price=stops.get(symbol), confirmation=conf)

        # 报价新鲜度：过期时页面提示，且不得据此生成新建议（P2）
        as_of = r["quote_as_of"]
        age = None
        if as_of:
            try:
                age = (now - datetime.fromisoformat(as_of)).total_seconds()
            except (TypeError, ValueError):
                age = None
        card["quote_rel"] = rel_time(as_of, now) if as_of else "无时点"
        card["quote_source"] = "富途" if (r["source"] or "").startswith("FUTU") else "IBKR"
        card["stale_css"] = ("crit" if (age is not None and age >= _STALE_OVER)
                             else ("warn" if (age is not None and age >= _STALE_WARN) else "ok"))
        card["is_stale"] = age is not None and age >= _STALE_OVER
        if card["is_stale"]:
            stale_count += 1
        if card["bars_available"] < technical.HIGH_52W:
            insufficient += 1

        # ---- T 规则 + 综合裁决（技术面服从仓位上限与止损）----
        stop = to_decimal(stops.get(symbol))
        risk_breached = (price is not None and stop is not None
                         and ((qty > 0 and price < stop) or (qty < 0 and price > stop)))
        plan = plan_by_sym.get(symbol) or {}
        acct_w = to_decimal(plan.get("account_weight"))
        max_w = to_decimal(plan.get("max_weight"))
        if acct_w is not None and max_w is not None and max_w > 0:
            over_limit = acct_w > max_w
            plan_allows_add = acct_w < max_w
        else:
            over_limit = None
            plan_allows_add = False
        data_ok = (not card["is_stale"]) and card["daily_trend_state"] is not None
        card["rules"] = technical.evaluate_rules(card, confirmation=conf)
        rec = technical.decide(
            card, data_ok=data_ok, risk_breached=bool(risk_breached),
            over_limit=over_limit, plan_allows_add=plan_allows_add, confirmation=conf)
        rec["final_label"] = technical.FINAL_ACTION_LABEL.get(
            rec["final_action"], rec["final_action"])
        card["recommendation"] = rec
        card["over_limit"] = over_limit
        cards.append(card)

    summary = technical.summarize(cards)

    # 技术触发时间线：只呈现事件（状态变化），不呈现每次轮询
    def _status_label(row):
        s = row.get("status")
        oc = row.get("occurrence_count") or 1
        return {"open": ("持续中" if oc > 1 else "新触发"), "acknowledged": "已确认",
                "resolved": "已恢复", "suppressed": "已失效"}.get(s, s or "—")
    try:
        ev_rows, _ = db.query_risk_events(source_type=["technical"], limit=40)
    except Exception:  # noqa: BLE001
        ev_rows = []
    timeline = [{
        "symbol": r.get("affected_symbol"),
        "rule_code": r.get("rule_code"),
        "title": r.get("title"),
        "summary": r.get("explanation"),
        "status": r.get("status"),
        "status_label": _status_label(r),
        "severity": r.get("severity"),
        "when": rel_time(r.get("last_detected_at"), now),
    } for r in ev_rows]

    return {
        "cards": cards,
        "timeline": timeline,
        "summary": summary,
        "session": session,
        "session_label": market_hours.session_label(session),
        "intraday": intraday,
        "stale_count": stale_count,
        "insufficient_count": insufficient,
        "as_of_rel": now.astimezone(DISPLAY_TZ).strftime("%Y-%m-%d %H:%M"),
        "calc_version": technical.CALC_VERSION,
        "ema_periods": technical.EMA_PERIODS,
    }
