from decimal import Decimal

from app.stop_loss import LONG, SHORT, compute_stop


def make_bars(closes, spread=Decimal("0.5")):
    """按收盘价列表构造日线，high/low = close ± spread。"""
    return [{"h": Decimal(str(c)) + spread, "l": Decimal(str(c)) - spread, "c": Decimal(str(c))}
            for c in closes]


def flat_bars(close, n=20, atr_spread=Decimal("0.5")):
    # high-low = 2*atr_spread，TR 恒定 => ATR = 2*atr_spread
    return [{"h": Decimal(str(close)) + atr_spread,
             "l": Decimal(str(close)) - atr_spread,
             "c": Decimal(str(close))} for _ in range(n)]


def test_long_stop_uses_max_of_two_formulas():
    # ATR=1（spread 0.5 => high-low=1，TR=1），close=10，swing_low=9.5
    bars = flat_bars(10, n=20, atr_spread=Decimal("0.5"))
    pos = {"symbol": "X", "qty": Decimal("100"), "avg_cost": Decimal("8")}
    adv = compute_stop(pos, bars, Decimal("10000"))
    assert adv.side == LONG
    assert adv.atr == Decimal("1")
    # max(9.5 - 0.3, 10 - 2) = max(9.2, 8) = 9.2
    assert adv.stop_price == Decimal("9.2")
    assert adv.risk_per_share == Decimal("0.8")           # 10 - 9.2
    assert adv.risk_amount == Decimal("80.0")             # 0.8 * 100
    assert adv.risk_pct == Decimal("80.0") / Decimal("10000")
    assert adv.exceeds_2pct is False


def test_long_risk_exceeds_2pct_suggests_trim():
    # 让每股风险很大以超过 2%
    bars = flat_bars(100, n=20, atr_spread=Decimal("5"))  # ATR=10
    pos = {"symbol": "BIG", "qty": Decimal("100"), "avg_cost": Decimal("100")}
    adv = compute_stop(pos, bars, Decimal("100000"))
    # stop = max(swing_low(95) - 3, 100 - 20) = max(92, 80) = 92 => risk_ps=8
    assert adv.stop_price == Decimal("92")
    assert adv.risk_per_share == Decimal("8")
    # risk = 8*100 = 800 = 0.8% ... 需要更极端；调大数量
    pos2 = {"symbol": "BIG", "qty": Decimal("400"), "avg_cost": Decimal("100")}
    adv2 = compute_stop(pos2, bars, Decimal("100000"))
    # risk = 8*400 = 3200 = 3.2% > 2%
    assert adv2.exceeds_2pct is True
    # 目标数量 = 0.02*100000/8 = 250，减仓 = 400-250 = 150
    assert adv2.suggested_trim_shares == 150


def test_short_stop_symmetric():
    bars = flat_bars(10, n=20, atr_spread=Decimal("0.5"))  # ATR=1, swing_high=10.5
    pos = {"symbol": "S", "qty": Decimal("-100"), "avg_cost": Decimal("12")}
    adv = compute_stop(pos, bars, Decimal("10000"))
    assert adv.side == SHORT
    # min(10.5 + 0.3, 10 + 2) = min(10.8, 12) = 10.8
    assert adv.stop_price == Decimal("10.8")
    assert adv.risk_per_share == Decimal("0.8")  # 10.8 - 10


def test_reached_1r_via_profit():
    # ATR=1, close=10, swing_low=9.5 => stop=max(9.2, 8)=9.2；entry=9.6（现价在成本上方）
    bars = flat_bars(10, n=20, atr_spread=Decimal("0.5"))
    pos = {"symbol": "P", "qty": Decimal("100"), "avg_cost": Decimal("9.6")}
    adv = compute_stop(pos, bars, Decimal("10000"))
    assert adv.stop_price == Decimal("9.2")
    assert adv.initial_risk_per_share == Decimal("0.4")  # 9.6 - 9.2
    # 走"浮盈达标"分支（stop 9.2 < entry 9.6），close-entry=0.4 >= 1R
    assert adv.stop_price < adv.entry
    assert adv.reached_1r is True
    assert "减半仓" in adv.take_profit_hint


def test_not_reached_1r():
    bars = flat_bars(10, n=20, atr_spread=Decimal("0.5"))  # ATR=1, close=10, swing_low=9.5
    pos = {"symbol": "Q", "qty": Decimal("100"), "avg_cost": Decimal("10")}
    adv = compute_stop(pos, bars, Decimal("10000"))
    # stop=9.2, initial_risk=0.8, close-entry=0 < 0.8
    assert adv.reached_1r is False
    assert "未达 1R" in adv.take_profit_hint


# ---------------- 新上市标的：日线不足时的降级口径 ----------------
# 口径：>=15 根 → 原有 ATR14 不变；<15 根 → 用可用根数（ATR n-1）并标注
# 「数据不足，仅供参考」；<5 根 → 仍然拒算。

import pytest

from app.indicators import IndicatorError, effective_atr_period
from app.stop_loss import DEGRADED_NOTE


def test_effective_period_keeps_14_when_sufficient():
    assert effective_atr_period(15) == 14      # 恰好够
    assert effective_atr_period(90) == 14      # 富余


def test_effective_period_degrades_to_available():
    assert effective_atr_period(10) == 9       # KMEM 实况：10 根 → ATR9
    assert effective_atr_period(5) == 4        # 下限


def test_effective_period_rejects_below_min():
    with pytest.raises(IndicatorError, match="至少需要 5 根"):
        effective_atr_period(4)


def test_sufficient_bars_unchanged_by_degrade_flag():
    """日线充足时，allow_degraded 不得改变任何结果（原有规则保持）。"""
    pos = {"symbol": "MRVL", "qty": Decimal("10"), "avg_cost": Decimal("10")}
    bars = flat_bars(10, n=30)
    strict = compute_stop(pos, bars, Decimal("100000"))
    degraded = compute_stop(pos, bars, Decimal("100000"), allow_degraded=True)
    assert strict.stop_price == degraded.stop_price
    assert degraded.atr_period == 14
    assert degraded.data_sufficient is True
    assert degraded.data_note is None


def test_insufficient_bars_degrades_and_marks():
    pos = {"symbol": "KMEM", "qty": Decimal("1786"), "avg_cost": Decimal("18.955")}
    bars = flat_bars(19, n=10)                 # 复刻 KMEM：只有 10 根
    adv = compute_stop(pos, bars, Decimal("43500"), allow_degraded=True)
    assert adv.data_sufficient is False
    assert adv.atr_period == 9 and adv.bars_used == 10
    assert DEGRADED_NOTE in adv.data_note and "10 根" in adv.data_note
    assert DEGRADED_NOTE in adv.take_profit_hint
    assert adv.stop_price > 0                  # 关键：给得出止损位，不再留白


def test_insufficient_bars_still_raises_without_flag():
    """默认严格：不开 allow_degraded 时行为与改造前一致。"""
    pos = {"symbol": "KMEM", "qty": Decimal("100"), "avg_cost": Decimal("19")}
    with pytest.raises(IndicatorError):
        compute_stop(pos, flat_bars(19, n=10), Decimal("43500"))


def test_below_min_bars_raises_even_when_degraded():
    pos = {"symbol": "NEW", "qty": Decimal("100"), "avg_cost": Decimal("19")}
    with pytest.raises(IndicatorError):
        compute_stop(pos, flat_bars(19, n=4), Decimal("43500"), allow_degraded=True)
