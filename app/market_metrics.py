"""市场指标层：纯函数，把日线序列换算成趋势/波动率/利率事实。

与账户侧同样的三条纪律：全程 Decimal、历史不足如实返回 insufficient_history、
不确定就说不确定。**绝不用相邻指标互相推导来填补缺失** —— 那会让一份残缺数据
看起来完整。

一条额外纪律：**不把相关描述成因果**。「利率上行且 QQQ 走弱」只陈述两件事同时发生，
不声称前者导致后者。
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Sequence

ZERO = Decimal("0")
INSUFFICIENT = "insufficient_history"


def _d(v: Any) -> Decimal | None:
    if v is None or v == "":
        return None
    if isinstance(v, Decimal):
        return v
    try:
        return Decimal(str(v))
    except (InvalidOperation, ValueError, TypeError):
        return None


def closes(bars: Sequence[dict]) -> list[Decimal]:
    out = []
    for b in bars or []:
        c = _d(b.get("c") if "c" in b else b.get("close"))
        if c is not None:
            out.append(c)
    return out


def sma(values: Sequence[Decimal], n: int) -> Decimal | None:
    if len(values) < n or n <= 0:
        return None
    return sum(values[-n:], ZERO) / Decimal(n)


def pct_vs(value: Decimal | None, reference: Decimal | None) -> Decimal | None:
    """(现值 - 参考值)/参考值。参考值为 0 或缺失 -> None。"""
    if value is None or reference is None or reference == 0:
        return None
    return (value - reference) / reference


def slope(values: Sequence[Decimal], n: int, lookback: int = 5) -> Decimal | None:
    """均线斜率 = (今日均线 - lookback 日前均线) / lookback 日前均线。

    用相对变化而非绝对差 —— 不同价位的标的绝对斜率不可比。
    """
    if len(values) < n + lookback:
        return None
    now_ma = sma(values, n)
    past_ma = sma(values[:-lookback], n)
    return pct_vs(now_ma, past_ma)


def window_return(values: Sequence[Decimal], n: int) -> Decimal | None:
    if len(values) < n + 1:
        return None
    start, end = values[-(n + 1)], values[-1]
    return pct_vs(end, start)


def drawdown_from_high(values: Sequence[Decimal], n: int) -> Decimal | None:
    """距近 n 日最高点的回撤（正数表示已回落）。"""
    if len(values) < 2:
        return None
    window = values[-n:] if len(values) > n else values
    hi = max(window)
    if hi == 0:
        return None
    return (hi - values[-1]) / hi


def realized_volatility(values: Sequence[Decimal], n: int = 20) -> Decimal | None:
    """已实现波动率（日收益标准差 × √252，年化）。

    用 Decimal 算均值与方差，最后开方时转 float —— 开方本身没有精确十进制解，
    这一步的精度损失可接受且不参与金额计算。
    """
    if len(values) < n + 1:
        return None
    rets = []
    for i in range(len(values) - n, len(values)):
        prev, cur = values[i - 1], values[i]
        if prev == 0:
            return None
        rets.append((cur - prev) / prev)
    if len(rets) < 2:
        return None
    mean = sum(rets, ZERO) / Decimal(len(rets))
    var = sum(((r - mean) ** 2 for r in rets), ZERO) / Decimal(len(rets) - 1)
    return Decimal(str(float(var) ** 0.5)) * Decimal(str(252 ** 0.5))


def percentile_rank(values: Sequence[Decimal], current: Decimal | None) -> Decimal | None:
    """当前值在历史序列中的分位（0–1）。窗口由调用方决定并必须在输出里说明。"""
    if current is None or not values:
        return None
    below = sum(1 for v in values if v <= current)
    return Decimal(below) / Decimal(len(values))


# ======================= 趋势 =======================

def trend_metrics(bars: Sequence[dict]) -> dict:
    """单个指数的趋势指标。历史不足的项返回 None 并在 status 里标注。"""
    c = closes(bars)
    if not c:
        return {"status": INSUFFICIENT, "bars": 0}
    cur = c[-1]
    ma20, ma50, ma200 = sma(c, 20), sma(c, 50), sma(c, 200)
    out = {
        "bars": len(c),
        "close": cur,
        "ma20": ma20, "ma50": ma50, "ma200": ma200,
        "vs_ma20": pct_vs(cur, ma20),
        "vs_ma50": pct_vs(cur, ma50),
        "vs_ma200": pct_vs(cur, ma200),
        "below_ma20": (cur < ma20) if ma20 is not None else None,
        "below_ma50": (cur < ma50) if ma50 is not None else None,
        "below_ma200": (cur < ma200) if ma200 is not None else None,
        "ma20_slope": slope(c, 20),
        "ma50_slope": slope(c, 50),
        "return_20d": window_return(c, 20),
        "return_60d": window_return(c, 60),
        "drawdown_from_20d_high": drawdown_from_high(c, 20),
        "drawdown_from_60d_high": drawdown_from_high(c, 60),
        "realized_vol_20d": realized_volatility(c, 20),
    }
    missing = [k for k in ("ma20", "ma50", "ma200") if out[k] is None]
    out["status"] = INSUFFICIENT if missing else "ok"
    out["insufficient_for"] = missing
    return out


def relative_strength(a_bars: Sequence[dict], b_bars: Sequence[dict],
                      n: int = 20) -> Decimal | None:
    """A 相对 B 的 n 日超额收益。负值表示 A 弱于 B。"""
    ra, rb = window_return(closes(a_bars), n), window_return(closes(b_bars), n)
    if ra is None or rb is None:
        return None
    return ra - rb


# ======================= 波动率 =======================

def volatility_metrics(vix_bars: Sequence[dict], vxn_bars: Sequence[dict],
                       *, percentile_window: int = 252) -> dict:
    """VIX / VXN 指标。

    分位数窗口默认 252 个交易日（约一年），**必须在输出里写明** ——
    「VIX 处于 80 分位」脱离窗口就没有意义。
    """
    v, x = closes(vix_bars), closes(vxn_bars)
    out: dict[str, Any] = {"percentile_window_days": percentile_window}

    def block(vals: list[Decimal], prefix: str) -> None:
        out[f"{prefix}_current"] = vals[-1] if vals else None
        out[f"{prefix}_change_1d"] = (
            pct_vs(vals[-1], vals[-2]) if len(vals) >= 2 else None)
        out[f"{prefix}_change_5d"] = (
            pct_vs(vals[-1], vals[-6]) if len(vals) >= 6 else None)
        window = vals[-percentile_window:] if len(vals) > percentile_window else vals
        out[f"{prefix}_percentile"] = (
            percentile_rank(window, vals[-1]) if len(window) >= 30 else None)
        out[f"{prefix}_bars"] = len(vals)

    block(v, "vix")
    block(x, "vxn")
    # 两者同步恶化：单看一个可能是噪音，同时上行才是系统性信号
    c1, c2 = out.get("vix_change_1d"), out.get("vxn_change_1d")
    out["both_rising"] = (c1 is not None and c2 is not None
                          and c1 > 0 and c2 > 0)
    out["vxn_above_vix"] = (
        (out["vxn_current"] - out["vix_current"])
        if (out.get("vxn_current") is not None and out.get("vix_current") is not None)
        else None)
    out["status"] = "ok" if v else INSUFFICIENT
    return out


# ======================= 利率 =======================

def rates_metrics(bars: Sequence[dict]) -> dict:
    """十年期收益率指标。

    注意 IBKR 的 TNX 报的是「收益率×10」（如 4.25% 报 42.5），本模块只做相对变化，
    量纲不影响；绝对阈值判定在规则层按同一量纲配置。
    """
    c = closes(bars)
    if not c:
        return {"status": INSUFFICIENT, "bars": 0}
    out = {
        "bars": len(c),
        "current": c[-1],
        "change_1d": (c[-1] - c[-2]) if len(c) >= 2 else None,
        "change_5d": (c[-1] - c[-6]) if len(c) >= 6 else None,
        "change_20d": (c[-1] - c[-21]) if len(c) >= 21 else None,
        "pct_change_5d": pct_vs(c[-1], c[-6]) if len(c) >= 6 else None,
    }
    # 快速上行：5 日相对涨幅超过 8%（阈值在规则层可配，这里只给事实）
    out["status"] = "ok"
    return out


def rates_and_equity_together(rates: dict, qqq_trend: dict) -> dict:
    """利率上行与股指走弱是否**同时发生**。

    刻意只描述同时性，不声称因果 —— 相关不等于因果，把它写成「利率上升导致下跌」
    会让使用者对因果关系产生错误信心。
    """
    r5 = rates.get("pct_change_5d")
    q20 = qqq_trend.get("return_20d")
    if r5 is None or q20 is None:
        return {"co_occurring": None, "note": "数据不足，无法判断"}
    co = r5 > 0 and q20 < 0
    return {"co_occurring": bool(co), "rates_5d_pct": r5, "qqq_20d_return": q20,
            "note": "仅表示两者同期发生，不代表因果关系"}
