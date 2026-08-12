from decimal import Decimal

import pytest

from app import indicators


def bars_constant_tr(n=15, close=Decimal("10")):
    """构造 n 根日线：high-low=1，且 TR 恒为 1。"""
    return [{"h": close + Decimal("0.5"), "l": close - Decimal("0.5"), "c": close}
            for _ in range(n)]


def test_atr_constant():
    bars = bars_constant_tr(15)
    assert indicators.atr(bars, 14) == Decimal("1")


def test_atr_needs_enough_bars():
    with pytest.raises(indicators.IndicatorError):
        indicators.atr(bars_constant_tr(14), 14)


def test_true_ranges_first_bar_uses_high_low():
    bars = [{"h": 11, "l": 9, "c": 10}, {"h": 12, "l": 10, "c": 11}]
    trs = indicators.true_ranges(bars)
    assert trs[0] == Decimal("2")            # 首根 high-low
    assert trs[1] == Decimal("2")            # max(2, |12-10|, |10-10|)=2


def test_swing_low_high():
    bars = [{"h": 11, "l": 9, "c": 10},
            {"h": 13, "l": 8, "c": 12},
            {"h": 12, "l": 10, "c": 11}]
    assert indicators.swing_low(bars, 20) == Decimal("8")
    assert indicators.swing_high(bars, 20) == Decimal("13")


def test_swing_respects_lookback():
    bars = [{"h": 100, "l": 1, "c": 50},   # 极值在窗口外
            {"h": 11, "l": 9, "c": 10},
            {"h": 12, "l": 8, "c": 11}]
    assert indicators.swing_low(bars, 2) == Decimal("8")
    assert indicators.swing_high(bars, 2) == Decimal("12")
