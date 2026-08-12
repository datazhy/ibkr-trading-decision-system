"""止损/止盈建议引擎（每日收盘后计算，只提示不下单）。

多头：建议止损 = max(swing_low - 0.3*ATR, close - 2*ATR)
空头：对称 = min(swing_high + 0.3*ATR, close + 2*ATR)
另计算：若按建议止损的单笔亏损金额、占净资产百分比、>2% 时的建议减仓股数，
以及 1R 减半 + 1*ATR trailing 的止盈提示。
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from typing import Sequence

from . import indicators

LONG = "LONG"
SHORT = "SHORT"

_ATR_STOP_MULT = Decimal("2")      # close ± 2*ATR
_SWING_ATR_MULT = Decimal("0.3")   # swing ∓ 0.3*ATR
_RISK_LIMIT_PCT = Decimal("0.02")  # 单笔风险占净资产上限 2%

#: 日线不足时的统一标注（看板/报告/R7 均引用此文案）
DEGRADED_NOTE = "数据不足，仅供参考"


def _d(v) -> Decimal:
    return v if isinstance(v, Decimal) else Decimal(str(v))


@dataclass
class StopAdvice:
    symbol: str
    side: str
    qty: Decimal              # 绝对数量（正）
    entry: Decimal            # 持仓均价
    close: Decimal            # 计算用收盘价
    atr: Decimal
    swing: Decimal            # 多头=swing_low，空头=swing_high
    stop_price: Decimal
    risk_per_share: Decimal   # 当前价到止损的每股风险（可为负=已越过止损）
    risk_amount: Decimal      # = risk_per_share * qty
    risk_pct: Decimal | None  # 占净资产比例；equity<=0 时为 None
    exceeds_2pct: bool
    suggested_trim_shares: int
    initial_risk_per_share: Decimal  # 均价到止损的每股风险（R 的口径）
    reached_1r: bool
    half_position_shares: int
    trailing_stop: Decimal
    take_profit_hint: str
    atr_period: int           # 实际使用的 ATR 周期（降级时 < 请求值）
    bars_used: int            # 参与计算的日线根数
    data_sufficient: bool     # False = 日线不足，结果仅供参考
    data_note: str | None     # 不足时的说明文案，充足时为 None


def compute_stop(
    position: dict,
    bars: Sequence[dict],
    equity: Decimal,
    *,
    atr_period: int = 14,
    swing_lookback: int = 20,
    close: Decimal | None = None,
    allow_degraded: bool = False,
) -> StopAdvice:
    """按日线算建议止损/止盈。

    allow_degraded=True 时，新上市标的日线不足 atr_period+1 根则降级用可用根数
    （见 indicators.effective_atr_period），结果标记 data_sufficient=False。
    默认 False 保持原有严格口径（不足即 raise）。
    """
    symbol = position["symbol"]
    qty_signed = _d(position["qty"])
    entry = _d(position["avg_cost"])
    equity = _d(equity)

    if qty_signed == 0:
        raise ValueError(f"{symbol} 持仓数量为 0，无需计算止损")
    side = LONG if qty_signed > 0 else SHORT
    qty = abs(qty_signed)

    eff_period = (
        indicators.effective_atr_period(len(bars), atr_period)
        if allow_degraded else atr_period      # 严格模式下由 indicators.atr 自己报错
    )
    a = indicators.atr(bars, eff_period)
    data_sufficient = eff_period >= atr_period
    data_note = None if data_sufficient else (
        f"{DEGRADED_NOTE}：仅 {len(bars)} 根日线（标准需 {atr_period + 1} 根），"
        f"降级用 ATR{eff_period}"
    )
    c = _d(close) if close is not None else _d(bars[-1]["c"])

    if side == LONG:
        swing = indicators.swing_low(bars, swing_lookback)
        stop = max(swing - _SWING_ATR_MULT * a, c - _ATR_STOP_MULT * a)
        risk_ps = c - stop
        initial_risk_ps = entry - stop
        trailing = indicators.recent_high_close(bars, swing_lookback) - a
        # stop 已抬到成本之上 => 利润已被保护，视为已超 1R
        reached_1r = stop >= entry or (initial_risk_ps > 0 and (c - entry) >= initial_risk_ps)
    else:
        swing = indicators.swing_high(bars, swing_lookback)
        stop = min(swing + _SWING_ATR_MULT * a, c + _ATR_STOP_MULT * a)
        risk_ps = stop - c
        initial_risk_ps = stop - entry
        trailing = indicators.recent_low_close(bars, swing_lookback) + a
        # stop 已压到成本之下 => 利润已被保护，视为已超 1R
        reached_1r = stop <= entry or (initial_risk_ps > 0 and (entry - c) >= initial_risk_ps)

    risk_amount = risk_ps * qty
    risk_pct = (risk_amount / equity) if equity > 0 else None
    exceeds = risk_pct is not None and risk_pct > _RISK_LIMIT_PCT

    trim_shares = 0
    if exceeds and risk_ps > 0 and equity > 0:
        target_qty = (_RISK_LIMIT_PCT * equity / risk_ps).to_integral_value(ROUND_FLOOR)
        trim = (qty - target_qty)
        trim_shares = int(trim.to_integral_value(ROUND_CEILING)) if trim > 0 else 0

    half_shares = int((qty / 2).to_integral_value(ROUND_FLOOR))

    if reached_1r:
        hint = (
            f"浮盈已达 1R，建议减半仓约 {half_shares} 股；"
            f"剩余以 1*ATR trailing 跟踪，参考跟踪止损 {trailing:.2f}（回吐 {a:.2f} 触发）"
        )
    else:
        hint = "未达 1R，暂不减仓"
    if not data_sufficient:
        hint = f"{hint}（{DEGRADED_NOTE}）"

    return StopAdvice(
        symbol=symbol,
        side=side,
        qty=qty,
        entry=entry,
        close=c,
        atr=a,
        swing=swing,
        stop_price=stop,
        risk_per_share=risk_ps,
        risk_amount=risk_amount,
        risk_pct=risk_pct,
        exceeds_2pct=exceeds,
        suggested_trim_shares=trim_shares,
        initial_risk_per_share=initial_risk_ps,
        reached_1r=reached_1r,
        half_position_shares=half_shares,
        trailing_stop=trailing,
        take_profit_hint=hint,
        atr_period=eff_period,
        bars_used=len(bars),
        data_sufficient=data_sufficient,
        data_note=data_note,
    )
