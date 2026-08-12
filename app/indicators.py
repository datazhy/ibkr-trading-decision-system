"""技术指标：ATR14、近 N 日 swing low/high。

纯函数，输入按日期升序的日线序列（每根含 Decimal 的 h/l/c），输出 Decimal。
无外部依赖，便于历史回放测试。
"""
from __future__ import annotations

from decimal import Decimal
from typing import Sequence


class IndicatorError(ValueError):
    pass


#: ATR 降级的绝对下限。低于此根数不给任何 ATR —— ATR4 已是统计意义的边界。
MIN_ATR_BARS = 5


def effective_atr_period(
    n_bars: int, period: int = 14, min_bars: int = MIN_ATR_BARS
) -> int:
    """按可用根数返回实际能用的 ATR 周期（新上市标的日线不足时降级用）。

    数据充足（>= period+1 根）返回 period 本身，口径不变；
    不足则降级为 n_bars-1（首根无前收须跳过）；连 min_bars 都不到则 raise。
    """
    if n_bars >= period + 1:
        return period
    if n_bars < min_bars:
        raise IndicatorError(f"ATR 至少需要 {min_bars} 根日线，实际 {n_bars}")
    return n_bars - 1


def _d(v) -> Decimal:
    return v if isinstance(v, Decimal) else Decimal(str(v))


def true_ranges(bars: Sequence[dict]) -> list[Decimal]:
    """逐根真实波幅 TR。第 1 根用 high-low（无前收）。返回长度 = len(bars)。"""
    trs: list[Decimal] = []
    prev_close: Decimal | None = None
    for b in bars:
        high, low, close = _d(b["h"]), _d(b["l"]), _d(b["c"])
        if prev_close is None:
            tr = high - low
        else:
            tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
        trs.append(tr)
        prev_close = close
    return trs


def atr(bars: Sequence[dict], period: int = 14) -> Decimal:
    """Wilder 平滑 ATR。

    需要至少 period+1 根（首根无前收，从第 2 根起累计 period 个 TR 做初值）。
    """
    if len(bars) < period + 1:
        raise IndicatorError(f"ATR{period} 需要至少 {period + 1} 根日线，实际 {len(bars)}")
    trs = true_ranges(bars)
    # 跳过第 1 根（其 TR 无前收，口径不同），用第 2..period+1 根初始化
    seed = trs[1 : period + 1]
    prev_atr = sum(seed, Decimal("0")) / Decimal(period)
    # Wilder 递推剩余
    for tr in trs[period + 1 :]:
        prev_atr = (prev_atr * (period - 1) + tr) / Decimal(period)
    return prev_atr


def ema_series(bars: Sequence[dict], period: int) -> list[Decimal]:
    """EMA 序列（按日期升序）。用前 period 根收盘的 SMA 播种，其后标准 EMA 递推。

    返回长度 = len(bars) - period + 1（从播种点起）。不足 period 根 raise。
    """
    closes = [_d(b["c"]) for b in bars]
    if len(closes) < period:
        raise IndicatorError(f"EMA{period} 需要至少 {period} 根日线，实际 {len(closes)}")
    k = Decimal(2) / Decimal(period + 1)
    seed = sum(closes[:period], Decimal("0")) / Decimal(period)
    out = [seed]
    prev = seed
    for c in closes[period:]:
        prev = (c - prev) * k + prev
        out.append(prev)
    return out


def ema(bars: Sequence[dict], period: int) -> Decimal:
    """最新 EMA 值。"""
    return ema_series(bars, period)[-1]


def ema_slope(bars: Sequence[dict], period: int, lookback: int) -> Decimal:
    """EMA 现值相对 lookback 根前的变化量（正=上升）。序列不足 lookback+1 则 raise。"""
    series = ema_series(bars, period)
    if len(series) <= lookback:
        raise IndicatorError(
            f"EMA{period} 斜率需要额外 {lookback} 根，序列仅 {len(series)}")
    return series[-1] - series[-1 - lookback]


def swing_low(bars: Sequence[dict], lookback: int = 20) -> Decimal:
    """近 lookback 根的最低价（用 low）。"""
    if not bars:
        raise IndicatorError("swing_low 需要至少 1 根日线")
    window = bars[-lookback:]
    return min(_d(b["l"]) for b in window)


def swing_high(bars: Sequence[dict], lookback: int = 20) -> Decimal:
    """近 lookback 根的最高价（用 high）。"""
    if not bars:
        raise IndicatorError("swing_high 需要至少 1 根日线")
    window = bars[-lookback:]
    return max(_d(b["h"]) for b in window)


def recent_high_close(bars: Sequence[dict], lookback: int = 20) -> Decimal:
    """近 lookback 根的最高收盘（trailing 止盈锚点用）。"""
    if not bars:
        raise IndicatorError("recent_high_close 需要至少 1 根日线")
    return max(_d(b["c"]) for b in bars[-lookback:])


def recent_low_close(bars: Sequence[dict], lookback: int = 20) -> Decimal:
    if not bars:
        raise IndicatorError("recent_low_close 需要至少 1 根日线")
    return min(_d(b["c"]) for b in bars[-lookback:])
