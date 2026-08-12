"""收益计算（纯函数，无 IO / 无时钟 / 无 DB）。

三条不可逾越的口径（用户明确要求）：
  1. IBKR 用 NaN / max double(1.797e308) 表示「不可用」而非 null —— 这类哨兵值
     必须在入库前变成 None，绝不能当数字参与任何计算。
  2. 资金流未确认覆盖整个窗口时，只能叫「净资产变动」，**不得**称为盈亏。
  3. 历史长度不足窗口天数时，不得使用「近 N 日」这个名字。
  4. 禁止跨日累加 DailyPnL（IBKR 每个交易日重置，累加会得到错误数字）。
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Iterable, Sequence

# IBKR 的 UNSET_DOUBLE。见 ibapi：不可用的浮点字段填这个值，不是 null。
_MAX_DOUBLE = 1.7976931348623157e308

RESET_POLICY_IBKR = "IBKR_SESSION_DAILY"   # 每个交易日重置 => 禁止跨日累加

#: 三、资金流文案统一：全站只用这一句，杜绝各页面各写各的
FLOW_INCOMPLETE_NOTE = "资金流尚未完整登记，当前仅展示净资产变动，不代表投资盈亏。"
#: 资金流不完整时曲线/卡片的统一标题
CHANGE_ONLY_TITLE = "净资产变动"
PNL_TITLE = "累计盈亏"

# 窗口状态
PNL = "PNL"                                # 资金流完整，可称「盈亏」
CHANGE_ONLY = "CHANGE_ONLY"                # 资金流不完整，只能称「净资产变动」
INSUFFICIENT_HISTORY = "INSUFFICIENT_HISTORY"   # 历史长度不足，不得叫「近 N 日」


def clean_pnl(v) -> Decimal | None:
    """把 IBKR 数值字段清洗成 Decimal；NaN/inf/max double 等哨兵一律返回 None。"""
    if v is None:
        return None
    if isinstance(v, Decimal):
        f = float(v)
        return None if (math.isnan(f) or math.isinf(f) or abs(f) >= _MAX_DOUBLE) else v
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if math.isnan(f) or math.isinf(f) or abs(f) >= _MAX_DOUBLE:
        return None
    return Decimal(str(v))


#: PnL 数据来源
SOURCE_IBKR = "IBKR_reqPnL"
SOURCE_DERIVED = "DERIVED_portfolio+executions"

#: 今日盈亏的降级基准
BASIS_NET_LIQ = "NET_LIQ_CHANGE"                  # 净资产变动（资金流已确认覆盖）
BASIS_NET_LIQ_RAW = "NET_LIQ_CHANGE_UNADJUSTED"   # 净资产变动（资金流未确认）


def derive_pnl(*, positions_upl, today_realized, net_liquidation, prev_close_nl,
               flows, coverage_ok: bool) -> dict:
    """reqPnL 不可用时的降级口径。

    **为什么需要它**：实测 IBKR 的 reqPnL 会成片返回 nan —— 2026-07-16 ET 09:00 至
    11:35 期间 dailyPnL/realizedPnL/unrealizedPnL 三项全空（订阅建立成功、无任何
    错误码），11:46 又自行恢复。原因未确认（与开盘时段相关，但盘中亦曾正常，
    故不能断言是行情权限所致）。无论成因，都不该让看板整段时间显示空白。

    降级用的两个源在 reqPnL 失效期间仍然可用（已实测）：
      - portfolio 的 unrealizedPNL（IBKR 直接给，随延迟价更新）
      - executions 的 realizedPNL（券商权威值）
    今日盈亏则回到净资产变动：当日净资产 − 前一交易日日终净资产 − 当日资金流。

    返回值一律带 source/quality，页面必须显示这是降级口径而非券商 PnL 接口原值。
    """
    unrealized = None
    vals = [v for v in (positions_upl or []) if v is not None]
    if vals:
        unrealized = sum(vals, Decimal("0"))

    daily = basis = None
    if net_liquidation is not None and prev_close_nl is not None:
        daily = net_liquidation - prev_close_nl - (flows or Decimal("0"))
        basis = BASIS_NET_LIQ if coverage_ok else BASIS_NET_LIQ_RAW

    return {
        "daily_pnl": daily,
        "realized_pnl": today_realized,
        "unrealized_pnl": unrealized,
        "source": SOURCE_DERIVED,
        "quality": "DEGRADED" if (daily is not None or unrealized is not None)
                   else "UNAVAILABLE",
        "daily_basis": basis,
        # 资金流覆盖是否已确认。未确认时净值变动口径可能被出入金污染，
        # 原子观测据此标 CASH_FLOW_COVERAGE_UNCONFIRMED。
        "coverage_ok": bool(coverage_ok),
    }


def pnl_quality(daily, realized, unrealized) -> str:
    """OK=三项齐全；PARTIAL=部分被哨兵过滤；UNAVAILABLE=全无。"""
    vals = (daily, realized, unrealized)
    if all(v is None for v in vals):
        return "UNAVAILABLE"
    if any(v is None for v in vals):
        return "PARTIAL"
    return "OK"


def et_date_of(dt: datetime, market_tz) -> str:
    """UTC 时刻 -> 美东日历日（YYYY-MM-DD）。UTC 与 ET 日期分开存，禁止混用。"""
    return dt.astimezone(market_tz).date().isoformat()


#: 美股盘前 04:00 ET。IBKR 的日内 PnL 在新交易会话开始时重置，此前拿到的
#: dailyPnL 讲的仍是上一个交易日那笔账。
_SESSION_START_HHMM = (4, 0)


def pnl_session_date(now: datetime, market_tz) -> str:
    """IBKR dailyPnL 所属的**交易会话日**（不是 ET 日历日）。

    凌晨 02:00 拿到的 dailyPnL 是昨天那笔账 —— 若按日历日记成「今天」，
    今日百分比的分母就会取到已经包含这笔盈亏的当日日终净值，
    等于拿结果去除以它自己。

    注：04:00 边界依据 IBKR 会话惯例，非官方文档承诺；reset_policy 字段记录了
    这一口径，若实测发现重置时点不同，改这里即可。
    """
    et = now.astimezone(market_tz)
    d = et.date()
    if (et.hour, et.minute) < _SESSION_START_HHMM:
        d -= timedelta(days=1)
    while d.weekday() >= 5:          # 周末回退到周五会话
        d -= timedelta(days=1)
    return d.isoformat()


# ---------------- 资金流 ----------------

def flows_sum(flows: Iterable) -> Decimal:
    """资金流净额（正=净入金）。

    追加式审计：冲正记录本身就是一条金额相反的普通记录，直接求和即自动抵消，
    无需排除任何行 —— 这正是不允许 UPDATE/DELETE 原记录的原因。
    """
    total = Decimal("0")
    for f in flows:
        amt = f["amount"] if not isinstance(f, dict) else f.get("amount")
        if amt is None:
            continue
        total += amt if isinstance(amt, Decimal) else Decimal(str(amt))
    return total


def _merge_ranges(ranges: Sequence[tuple[str, str]]) -> list[tuple[str, str]]:
    """合并相邻/重叠的日期区间（ISO 日期字符串可直接字典序比较）。"""
    if not ranges:
        return []
    out: list[list[str]] = []
    for s, e in sorted(ranges):
        if out and s <= _next_day(out[-1][1]):      # 相接或重叠
            if e > out[-1][1]:
                out[-1][1] = e
        else:
            out.append([s, e])
    return [(s, e) for s, e in out]


def _next_day(d: str) -> str:
    return (date.fromisoformat(d) + timedelta(days=1)).isoformat()


#: 允许的尾部未覆盖天数。券商流水是 T+1，**覆盖永远追不上今天** ——
#: 严格要求覆盖到今天，等于永远拿不到「盈亏」口径。
#: 放宽到 3 天（T+1 + 周末），但必须把这几天**明确标注为未经券商确认**，
#: 而不是当作已确认。诚实不等于什么都不给看。
MAX_UNCOVERED_TAIL_DAYS = 3

UNCOVERED_TAIL_NOTE = ("最近 {n} 天尚未经券商流水确认（Flex 为 T+1）。"
                       "若这几天有出入金，盈亏会被高估或低估。")


def coverage_with_tail(coverages: Sequence, start_date: str, end_date: str,
                       *, max_tail_days: int = MAX_UNCOVERED_TAIL_DAYS) -> dict:
    """带尾部容差的覆盖判定。

    返回 {ok, covered_through, uncovered_tail_days, note}。
    ok=True 表示**起点到 covered_through 已被券商流水覆盖**，
    尾部若干天则如实标注 —— 不假装它们也被确认过。
    """
    if start_date > end_date:
        return {"ok": False, "covered_through": None,
                "uncovered_tail_days": None, "note": None}
    if coverage_complete(coverages, start_date, end_date):
        return {"ok": True, "covered_through": end_date,
                "uncovered_tail_days": 0, "note": None}

    ranges = _merge_ranges([
        ((c["start_date"] if not isinstance(c, dict) else c.get("start_date")),
         (c["end_date"] if not isinstance(c, dict) else c.get("end_date")))
        for c in coverages
        if (c["status"] if not isinstance(c, dict) else c.get("status")) == "COMPLETE"])
    for s, e in ranges:
        if s <= start_date and e < end_date:
            gap = (date.fromisoformat(end_date) - date.fromisoformat(e)).days
            if 0 < gap <= max_tail_days:
                return {"ok": True, "covered_through": e,
                        "uncovered_tail_days": gap,
                        "note": UNCOVERED_TAIL_NOTE.format(n=gap)}
    return {"ok": False, "covered_through": None,
            "uncovered_tail_days": None, "note": None}


def coverage_complete(coverages: Sequence, start_date: str, end_date: str) -> bool:
    """[start_date, end_date] 是否被 COMPLETE 声明完整覆盖。

    没有声明 = 不完整。宁可显示「净资产变动」，也不拿不确定的资金流算盈亏。
    """
    if start_date > end_date:
        return False
    ranges = []
    for c in coverages:
        status = c["status"] if not isinstance(c, dict) else c.get("status")
        if status != "COMPLETE":
            continue
        s = c["start_date"] if not isinstance(c, dict) else c.get("start_date")
        e = c["end_date"] if not isinstance(c, dict) else c.get("end_date")
        ranges.append((s, e))
    for s, e in _merge_ranges(ranges):
        if s <= start_date and e >= end_date:
            return True
    return False


# ---------------- 交易基线（跳过入金前的空账户阶段） ----------------

def trading_baseline(equity_series, weekday_of, *, cash_flow_dates=None) -> dict | None:
    """从日终净资产序列里找出「最后一次资金流跳变之后」的基线点。

    equity_series: [{et_date, net_liquidation}] 按日期升序（已是 READY 质量）
    weekday_of(et_date) -> 0..6

    传入 cash_flow_dates 时，只认已登记的真实外部资金流，绝不用
    净资产涨跌猜入出金。不传时保留旧启发式，仅供旧数据兼容。

    **为什么需要**：账户 07-14 入金前净资产是 378，入金后 47069。若窗口从入金前算，
    「近 7 日盈亏」会把 46,691 的转账当成收益。检测到这种跳变后，把基线设在跳变之后，
    窗口内就没有资金流了 —— 无需用户登记精确金额，也能算出入金后的纯盈亏。

    跨周末的正常涨跌不算跳变（与曲线连贯同一口径）。返回最后一次跳变后的那个点；
    没有跳变则返回 None（全程无资金流，任意起点都是纯盈亏）。
    """
    if cash_flow_dates is not None:
        dates = sorted(str(d)[:10] for d in cash_flow_dates if d)
        if not dates or not equity_series:
            return None
        last_flow = dates[-1]
        # 日终净值已包含当日到账资金，以资金流当日（或之后
        # 第一个有效日）作新阶段起点。若资金流早于现有历史，
        # 当前整段本就在入金之后，无需裁剪。
        if last_flow < equity_series[0]["et_date"]:
            return None
        return next((r for r in equity_series if r["et_date"] >= last_flow), None)

    baseline = None
    for i in range(1, len(equity_series)):
        prev = equity_series[i - 1]
        cur = equity_series[i]
        pv = prev["net_liquidation"]
        pv = pv if isinstance(pv, Decimal) else Decimal(str(pv))
        cv = cur["net_liquidation"]
        cv = cv if isinstance(cv, Decimal) else Decimal(str(cv))
        if pv <= 0:
            baseline = cur           # 从 0/空账户变为有值，视为入金
            continue
        d0, d1 = date.fromisoformat(prev["et_date"]), date.fromisoformat(cur["et_date"])
        # 跨周末的相邻交易日不算跳变
        crosses_weekend = (d1 - d0).days > 1 and all(
            weekday_of((d0 + timedelta(days=k)).isoformat()) >= 5
            for k in range(1, (d1 - d0).days))
        if crosses_weekend:
            continue
        if abs(cv - pv) / pv > EQUITY_JUMP_RATIO:
            baseline = cur           # 记住最后一次跳变后的点
    return baseline


# ---------------- 窗口盈亏 ----------------

@dataclass
class WindowResult:
    days: int                      # 请求的窗口天数（自然日）
    status: str                    # PNL / CHANGE_ONLY / INSUFFICIENT_HISTORY
    label: str                     # 面向用户的名称，随 status 变化
    amount: Decimal | None = None
    pct: Decimal | None = None
    start_date: str | None = None  # 实际起点（美东日历日）
    end_date: str | None = None
    start_value: Decimal | None = None
    end_value: Decimal | None = None
    flows: Decimal | None = None   # 已扣除的资金流净额；CHANGE_ONLY 时为参考值
    days_available: int = 0        # 实际可用历史天数
    note: str | None = None
    coverage_ok: bool = False      # 该窗口的资金流是否已完整登记
    available_on: str | None = None  # 历史攒够后预计可用的美东日期


def compute_window(
    *,
    days: int,
    today_et: str,
    end_value: Decimal | None,
    start_row,                     # daily_equity 行（不晚于 today-days 的最近一条）；None=历史不足
    earliest_row,                  # 最早的有效 daily_equity 行；None=完全无历史
    flows: Decimal,
    coverage_ok: bool,
    baseline_row=None,             # 交易基线（最后一次资金流跳变之后）；None=全程无跳变
) -> WindowResult:
    """滚动自然日窗口的盈亏 / 净资产变动。

    起点取三者中最晚的：目标起点（today-days）、最早历史、交易基线。
    **交易基线**是最后一次入金/出金之后的点 —— 从它算起，窗口内没有资金流，
    差额就是纯盈亏，无需用户登记精确金额。这解决了「账户 07-14 才入金，
    近 7 日却把入金算成收益」的问题。

    adjusted_pnl = end_nl - start_nl - net_flows。
    """
    target_start = (date.fromisoformat(today_et) - timedelta(days=days)).isoformat()

    if end_value is None or earliest_row is None:
        return WindowResult(days=days, status=INSUFFICIENT_HISTORY,
                            label=f"近{days}日", days_available=0,
                            note="尚无有效日终净资产快照")

    earliest = earliest_row["et_date"]

    # 交易基线晚于目标起点时，用它做起点（窗口落在入金后的纯交易阶段）
    clipped_to_baseline = False
    if baseline_row is not None and baseline_row["et_date"] > target_start:
        start_row = baseline_row
        clipped_to_baseline = True
        # 有效历史从基线算起（入金前的空账户阶段不算数）
        eff_earliest = baseline_row["et_date"]
    else:
        eff_earliest = earliest

    days_available = (date.fromisoformat(today_et)
                      - date.fromisoformat(eff_earliest)).days

    # 历史不足：起点晚于目标起点 => 不允许叫「近 N 日」
    if start_row is None or (eff_earliest > target_start and not clipped_to_baseline):
        available_on = (date.fromisoformat(eff_earliest) + timedelta(days=days)).isoformat()
        return WindowResult(
            days=days, status=INSUFFICIENT_HISTORY, label=f"近{days}日",
            days_available=days_available, start_date=eff_earliest, end_date=today_et,
            coverage_ok=coverage_ok, available_on=available_on,
            note=f"历史数据不足：有效历史 {days_available}/{days} 天",
        )

    start_value = start_row["net_liquidation"]
    if not isinstance(start_value, Decimal):
        start_value = Decimal(str(start_value))
    start_date = start_row["et_date"]
    change = end_value - start_value

    # 从交易基线算起时，窗口内无资金流 —— 就是纯盈亏，即便没有覆盖声明。
    # 否则回到旧口径：有覆盖声明才叫盈亏，否则只能叫净资产变动。
    window_is_clean = clipped_to_baseline or coverage_ok
    if not window_is_clean:
        return WindowResult(
            days=days, status=CHANGE_ONLY, label=f"近{days}日净资产变动",
            amount=change, pct=None, start_date=start_date, end_date=today_et,
            start_value=start_value, end_value=end_value, flows=flows,
            days_available=days_available, coverage_ok=False,
            note=FLOW_INCOMPLETE_NOTE,
        )

    # clipped：基线之后无资金流，flows 视为 0；否则扣除已登记 flows
    eff_flows = Decimal("0") if clipped_to_baseline else flows
    amount = change - eff_flows
    pct = (amount / start_value) if start_value > 0 else None
    note = None
    if clipped_to_baseline:
        note = f"自入金后（{start_date}）起的盈亏，入金不计入收益"
    return WindowResult(
        days=days, status=PNL, label=f"近{days}日盈亏", amount=amount, pct=pct,
        start_date=start_date, end_date=today_et, start_value=start_value,
        end_value=end_value, flows=eff_flows, days_available=days_available,
        coverage_ok=True, note=note,
    )


# ---------------- 日历：每日收益 ----------------

#: 日终净资产环比变化超过此比例，视为未登记的资金流（与曲线/基线同口径）。
EQUITY_JUMP_RATIO = Decimal("0.20")

def daily_returns(equity_rows, flows_by_date, weekday_of, *, pnl_by_date=None) -> list[dict]:
    """逐日收益：当日净资产 − 前一有数据日净资产 − 当日资金流。

    equity_rows: [{et_date, net_liquidation}] 升序（READY）
    flows_by_date: {et_date: 已登记资金流净额}
    pnl_by_date: 仅保留为向后兼容参数，不覆盖有前后净值的历史日。
      reqPnL 的重置时刻由 TWS 全局配置和品种决定，不能把“同日最后一条”
      当成官方日终收益。生产实测 2026-07-30 盘后重置后只剩 +194.12，
      而 IBKR 净资产差额（且无外部资金流）为 +8,817.88。
    weekday_of(et_date) -> 0..6

    第一天无前值 -> 收益 None。
    只扣除 Flex/IBKR 明确记录的外部资金流，绝不再按 20% 净资产跳变猜入出金。
    否则高波动账户的真实盈利会被错误改成“资金流”。
    """
    out = []
    prev = None
    prev_date = None
    for r in equity_rows:
        nl = r["net_liquidation"]
        nl = nl if isinstance(nl, Decimal) else Decimal(str(nl))
        d = r["et_date"]
        flow = flows_by_date.get(d, Decimal("0"))
        if prev is None:
            out.append({"et_date": d, "net_liquidation": nl, "amount": None,
                        "pct": None, "has_flow": flow != 0, "has_jump": False, "flow": flow})
            prev, prev_date = nl, d
            continue
        amount = nl - prev - flow
        pct = (amount / prev) if prev > 0 else None
        out.append({"et_date": d, "net_liquidation": nl, "amount": amount,
                    "pct": pct, "has_flow": flow != 0, "has_jump": False,
                    "flow": flow, "source": "IBKR_NAV_MINUS_EXTERNAL_FLOW"})
        prev, prev_date = nl, d
    return out


# ---------------- 历史盈亏（自入金后全部收益） ----------------

def compute_inception(*, today_et: str, end_value, baseline_row, earliest_row) -> WindowResult:
    """自入金后（交易基线）至今的**全部**盈亏。

    用户口径：把「近 30 日」换成「历史盈亏」—— 账户 07-14 才入金，30 天窗口大半是空的，
    不如直接算入金以来的累计收益。从交易基线算起窗口内无资金流，就是纯盈亏。

    无基线（全程无资金流）时用最早历史点。
    """
    start = baseline_row or earliest_row
    if end_value is None or start is None:
        return WindowResult(days=0, status=INSUFFICIENT_HISTORY, label="历史盈亏",
                            note="尚无有效日终净资产快照")
    start_value = start["net_liquidation"]
    if not isinstance(start_value, Decimal):
        start_value = Decimal(str(start_value))
    start_date = start["et_date"]
    days_available = (date.fromisoformat(today_et) - date.fromisoformat(start_date)).days
    amount = end_value - start_value
    pct = (amount / start_value) if start_value > 0 else None
    note = (f"自入金后（{start_date}）起的全部盈亏，入金不计入收益"
            if baseline_row else f"自 {start_date} 起的全部盈亏")
    return WindowResult(
        days=days_available, status=PNL, label="历史盈亏", amount=amount, pct=pct,
        start_date=start_date, end_date=today_et, start_value=start_value,
        end_value=end_value, flows=Decimal("0"), days_available=days_available,
        coverage_ok=True, note=note,
    )


# ---------------- 今日百分比 ----------------

@dataclass
class DailyPct:
    pct: Decimal | None
    basis: Decimal | None          # 分母：前一美东交易日的有效收盘净资产
    basis_date: str | None
    available: bool
    reason: str | None = None


#: 前收基准与 IBKR 隐含基准的最大容忍偏差（相对）。超出即认为会话归属判断有误。
_BASIS_TOLERANCE = Decimal("0.005")


def daily_pct(
    *,
    daily_pnl: Decimal | None,
    prev_close_row,                # 前一交易日 daily_equity 行；None=基准缺失
    has_unreconciled_flows: bool,
    net_liquidation: Decimal | None = None,
    tolerance: Decimal = _BASIS_TOLERANCE,
) -> DailyPct:
    """今日收益率。基准缺失、当日有未核对资金流、或基准与 IBKR 口径对不上时，
    只给金额、不给百分比。

    一致性校验的由来：IBKR 未公开 dailyPnL 的会话重置时点，实测盘后拿到的
    dailyPnL 基准已经是当日收盘（会话已翻篇），若仍按日历日去取「前一交易日」
    收盘做分母，分母会错一整天。IBKR 自己的隐含基准就是 `净资产 - dailyPnL`；
    拿它和我们选的基准对一下，对不上就说明会话判断有误 —— 此时宁可不给百分比，
    也不给一个用错分母算出来的数。
    """
    if daily_pnl is None:
        return DailyPct(None, None, None, False, "今日盈亏不可用")
    if prev_close_row is None:
        return DailyPct(None, None, None, False, "缺少前一交易日收盘净资产基准")
    if has_unreconciled_flows:
        return DailyPct(None, None, prev_close_row["et_date"], False,
                        "当日存在未核对资金流")
    basis = prev_close_row["net_liquidation"]
    if not isinstance(basis, Decimal):
        basis = Decimal(str(basis))
    basis_date = prev_close_row["et_date"]
    if basis <= 0:
        return DailyPct(None, basis, basis_date, False, "基准净资产无效")
    if net_liquidation is not None:
        implied = net_liquidation - daily_pnl        # IBKR 口径下的当日起始净值
        if abs(implied - basis) / basis > tolerance:
            return DailyPct(
                None, basis, basis_date, False,
                f"基准与 IBKR 口径不符（IBKR 隐含起始净值 {implied:,.2f}，"
                f"{basis_date} 收盘 {basis:,.2f}）",
            )
    return DailyPct(daily_pnl / basis, basis, basis_date, True)
