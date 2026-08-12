"""前瞻窗口结果计算（纯函数）。

给定一个事件触发时点与若干交易日窗口，算出「之后发生了什么」：账户怎么走、
市场怎么走、涉事持仓怎么走。

**现金流是这一层最大的陷阱**。账户净值上涨 8% 可能是行情好，也可能是入了一笔钱；
下跌 5% 可能是亏了，也可能是取了钱。把净资产的绝对变化当成投资表现，会让规则效果
统计彻底失真。本模块的处理：

    能识别现金流   -> 扣除后计算，标 clean
    疑似有现金流   -> 照常计算但标 estimated，并写明依据
    大额现金流     -> 标 contaminated，该窗口**不参与** precision 统计

判定依据是「净值跳变但无对应成交」—— 这是现有数据能给出的最强证据。
Flex Query 接入后可以换成真实的入金出金记录，接入点已经留好（`cash_flows` 参数）。

另一个陷阱：**用户主动减仓导致损失变小，不是规则无效**。窗口内的成交记录一并
返回，由上层判断，本模块只负责如实呈现。
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any, Sequence

ZERO = Decimal("0")

# 现金流状态
CF_CLEAN = "clean"
CF_ESTIMATED = "estimated"
CF_CONTAMINATED = "contaminated"

#: 单次净值跳变超过这个比例且无对应成交，视为疑似现金流。
#: 3% 是个折中：日内正常波动很少超过它，而小额入金也不至于污染统计。
DEFAULT_JUMP_THRESHOLD = Decimal("0.03")
#: 超过这个比例直接判定为受污染，该窗口退出统计。
DEFAULT_CONTAMINATION_THRESHOLD = Decimal("0.10")


def _d(v: Any) -> Decimal | None:
    if v is None or v == "":
        return None
    if isinstance(v, Decimal):
        return v
    try:
        return Decimal(str(v))
    except (InvalidOperation, ValueError, TypeError):
        return None


def _series_stats(values: Sequence[Decimal], base: Decimal | None) -> dict:
    """相对基准值的最大不利/有利变化与最大回撤。"""
    out = {"max_adverse": None, "max_favorable": None, "max_drawdown": None,
           "end_change": None, "samples": len(values)}
    if not values or base is None or base == 0:
        return out
    lo, hi = min(values), max(values)
    out["max_adverse"] = (base - lo) / base           # 正数 = 不利
    out["max_favorable"] = (hi - base) / base
    out["end_change"] = (values[-1] - base) / base
    # 最大回撤：从窗口内峰值到其后谷值
    peak, mdd = values[0], ZERO
    for v in values:
        if v > peak:
            peak = v
        if peak > 0:
            dd = (peak - v) / peak
            if dd > mdd:
                mdd = dd
    out["max_drawdown"] = mdd
    return out


# ---------------- 现金流 ----------------

def detect_cash_flow(equity_rows: list[dict], executions: list[dict], *,
                     jump_threshold: Decimal = DEFAULT_JUMP_THRESHOLD,
                     contamination_threshold: Decimal = DEFAULT_CONTAMINATION_THRESHOLD,
                     cash_flows: list[dict] | None = None) -> dict:
    """识别窗口内的现金流污染。

    cash_flows：将来接 Flex Query 真实入金出金记录的入口。给了就直接用，
    此时状态为 clean（有权威数据，不必猜）。
    """
    if cash_flows is not None:
        # 有券商权威流水：直接算，不再靠净值跳变猜。
        # 注意只有**外部**现金流进入这里 —— 买卖证券的现金流动不是资金进出，
        # 把它扣掉会让「扣除资金流后的收益」把正常交易也扣没了。
        deposits = withdrawals = ZERO
        for f in cash_flows:
            amt = _d(f.get("amount_base")) or _d(f.get("amount"))
            if amt is None:
                continue
            if amt > 0:
                deposits += amt
            else:
                withdrawals += -amt
        total = deposits - withdrawals
        base = _d((equity_rows or [{}])[0].get("equity"))
        ratio = (abs(total) / base) if (base and base != 0) else None
        return {"status": CF_CLEAN, "net_flow": str(total),
                "deposits": str(deposits), "withdrawals": str(withdrawals),
                "flow_ratio": str(ratio) if ratio is not None else None,
                "note": (f"来自券商流水的 {len(cash_flows)} 笔外部资金变动"
                         f"（入金 {deposits}，出金 {withdrawals}），已在收益中扣除"),
                "source": "flex_query"}

    if len(equity_rows) < 2:
        return {"status": CF_ESTIMATED, "net_flow": None, "flow_ratio": None,
                "note": "净值样本不足，无法判断是否存在资金变动",
                "source": "insufficient"}

    # 成交按交易日归集：有成交的日子里，净值跳变可由持仓变化解释
    exec_days = {str(e.get("execution_time") or "")[:10] for e in (executions or [])}
    jumps, max_jump = [], ZERO
    for prev, cur in zip(equity_rows, equity_rows[1:]):
        a, b = _d(prev.get("equity")), _d(cur.get("equity"))
        if a is None or b is None or a == 0:
            continue
        change = (b - a) / a
        if abs(change) < jump_threshold:
            continue
        day = str(cur.get("ts") or "")[:10]
        if day in exec_days:
            continue            # 有成交，跳变可解释
        jumps.append({"at": cur.get("ts"), "change": str(change)})
        if abs(change) > max_jump:
            max_jump = abs(change)

    if not jumps:
        return {"status": CF_CLEAN, "net_flow": None, "flow_ratio": None,
                "note": "未发现无法由成交解释的净值跳变", "source": "heuristic"}
    if max_jump >= contamination_threshold:
        return {"status": CF_CONTAMINATED, "net_flow": None,
                "flow_ratio": str(max_jump),
                "note": (f"发现 {len(jumps)} 次无成交对应的净值跳变，"
                         f"最大 {max_jump:.1%}，疑似大额出入金；"
                         "该窗口的账户结果不参与规则效果统计"),
                "source": "heuristic", "jumps": jumps[:5]}
    return {"status": CF_ESTIMATED, "net_flow": None, "flow_ratio": str(max_jump),
            "note": (f"发现 {len(jumps)} 次无成交对应的净值跳变，最大 {max_jump:.1%}，"
                     "账户结果为估算值"),
            "source": "heuristic", "jumps": jumps[:5]}


# ---------------- 账户前瞻 ----------------

def account_outcome(equity_rows: list[dict], *, base_equity: Decimal | None = None,
                    external_flow: Decimal | None = None) -> dict:
    """账户侧窗口结果。

    刻意**不做时间加权收益**：现有数据识别不出精确的现金流时点与金额，
    伪造一个 TWR 只会让人以为这个数字可信。宁可给出带 estimated 标记的简单口径。
    """
    equities = [e for e in (_d(r.get("equity")) for r in equity_rows) if e is not None]
    base = base_equity if base_equity is not None else (equities[0] if equities else None)
    st = _series_stats(equities, base)

    excess_ratios, maint_buffers, leverages = [], [], []
    for r in equity_rows:
        eq, ex = _d(r.get("equity")), _d(r.get("excess_liquidity"))
        mm, pv = _d(r.get("maint_margin")), _d(r.get("positions_value"))
        if eq and eq != 0:
            if ex is not None:
                excess_ratios.append(ex / eq)
            if pv is not None:
                leverages.append(abs(pv) / eq)
        if mm and mm != 0 and eq is not None:
            maint_buffers.append(eq / mm)

    lev_increase = None
    if len(leverages) >= 2:
        lev_increase = max(leverages) - leverages[0]

    # 现金流调整后的收益：把外部资金进出从净值变化里剔除。
    # **入金不是收益，出金不是亏损** —— 这是本函数存在的全部理由。
    adjusted_change = None
    raw_change = None
    if base is not None and base != 0 and equities:
        raw_change = (equities[-1] - base) / base
        if external_flow is not None:
            adjusted_change = (equities[-1] - base - external_flow) / base

    return {
        "account_value_start": str(base) if base is not None else None,
        "account_value_end": str(equities[-1]) if equities else None,
        "account_change": _s(st["end_change"]),
        "raw_nav_change": _s(raw_change),
        "cash_flow_adjusted_return": _s(adjusted_change),
        "external_cash_flow_total": _s(external_flow),
        "unexplained_nav_change": _s(
            (raw_change - adjusted_change)
            if (raw_change is not None and adjusted_change is not None) else None),
        "account_max_adverse": _s(st["max_adverse"]),
        "account_max_favorable": _s(st["max_favorable"]),
        "account_max_drawdown": _s(st["max_drawdown"]),
        "excess_liquidity_ratio_min": _s(min(excess_ratios)) if excess_ratios else None,
        "maint_buffer_min": _s(min(maint_buffers)) if maint_buffers else None,
        "leverage_increase": _s(lev_increase),
        "samples": len(equities),
    }


def _s(v: Decimal | None) -> str | None:
    return None if v is None else str(v)


# ---------------- 市场前瞻 ----------------

def market_outcome(qqq_bars: list[dict], sox_bars: list[dict],
                   vix_bars: list[dict], vxn_bars: list[dict], *,
                   qqq_ma50_at_open: Decimal | None = None,
                   market_states: list[str] | None = None) -> dict:
    """市场侧窗口结果。"""
    def closes(bars):
        return [c for c in (_d(b.get("c")) for b in bars or []) if c is not None]

    q, s = closes(qqq_bars), closes(sox_bars)
    v, x = closes(vix_bars), closes(vxn_bars)
    qs = _series_stats(q, q[0] if q else None)
    ss = _series_stats(s, s[0] if s else None)

    below_ma50 = None
    if q and qqq_ma50_at_open is not None and qqq_ma50_at_open != 0:
        below_ma50 = any(c < qqq_ma50_at_open for c in q)

    return {
        "qqq_return": _s(qs["end_change"]),
        "qqq_max_drawdown": _s(qs["max_drawdown"]),
        "qqq_max_adverse": _s(qs["max_adverse"]),
        "sox_return": _s(ss["end_change"]),
        "sox_max_drawdown": _s(ss["max_drawdown"]),
        "vix_max": _s(max(v)) if v else None,
        "vix_max_change": _s((max(v) - v[0]) / v[0]) if v and v[0] else None,
        "vxn_max": _s(max(x)) if x else None,
        "vxn_max_change": _s((max(x) - x[0]) / x[0]) if x and x[0] else None,
        "qqq_below_ma50_in_window": below_ma50,
        "market_entered_stressed": (None if market_states is None
                                    else "stressed" in market_states),
        "market_states_in_window": sorted(set(market_states or [])) or None,
    }


# ---------------- 持仓前瞻 ----------------

def position_outcome(bars: list[dict], *, base_price: Decimal | None = None,
                     base_qty: Decimal | None = None,
                     base_equity: Decimal | None = None,
                     executions: list[dict] | None = None,
                     end_qty: Decimal | None = None) -> dict:
    """持仓侧窗口结果。

    **仓位变化必须显式呈现**：如果用户在窗口内减了仓，后续价格下跌与账户损益
    的关系就变了。把「减仓后损失变小」解释成规则无效，是这类分析最常见的错误 ——
    那恰恰说明用户看到告警并采取了行动。
    """
    closes = [c for c in (_d(b.get("c")) for b in bars or []) if c is not None]
    base = base_price if base_price is not None else (closes[0] if closes else None)
    st = _series_stats(closes, base)

    buys = sells = ZERO
    for e in (executions or []):
        qty = _d(e.get("shares")) or ZERO
        if str(e.get("side") or "").upper().startswith(("B", "BOT")):
            buys += qty
        else:
            sells += qty

    qty_change = None
    if base_qty is not None and end_qty is not None:
        qty_change = end_qty - base_qty

    contribution = None
    if (base is not None and base_qty is not None and base_equity
            and base_equity != 0 and closes):
        # 用**期初仓位**计价：期末仓位已受主动交易影响，用它会把用户的决策
        # 算进规则效果里
        pnl = (closes[-1] - base) * base_qty
        contribution = pnl / base_equity

    worst = None
    if base is not None and base_qty is not None and base_equity and base_equity != 0 and closes:
        worst = ((min(closes) - base) * base_qty) / base_equity

    return {
        "position_price_start": _s(base),
        "position_price_end": _s(closes[-1]) if closes else None,
        "position_return": _s(st["end_change"]),
        "position_mae": _s(st["max_adverse"]),
        "position_mfe": _s(st["max_favorable"]),
        "position_contribution_to_account": _s(contribution),
        "position_worst_contribution": _s(worst),
        "qty_start": _s(base_qty), "qty_end": _s(end_qty),
        "qty_change": _s(qty_change),
        "position_increased": (None if qty_change is None else qty_change > 0),
        "position_reduced": (None if qty_change is None else qty_change < 0),
        "position_exited": (None if end_qty is None else end_qty == 0),
        "executions_in_window": len(executions or []),
        "shares_bought": _s(buys), "shares_sold": _s(sells),
        # 上层据此避免把「主动减仓后损失变小」误读成规则无效
        "user_acted": bool(sells > 0),
    }
