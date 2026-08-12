"""技术面 EMA 周期配置与趋势口径回归。"""
from datetime import date, timedelta
from decimal import Decimal

from app import technical as T


def _rising_bars(n=320):
    start = date(2025, 1, 1)
    rows = []
    for i in range(n):
        close = Decimal("100") + Decimal(i) / Decimal("10")
        rows.append({
            "date": (start + timedelta(days=i)).isoformat(),
            "o": close - Decimal("0.2"),
            "h": close + Decimal("0.5"),
            "l": close - Decimal("0.5"),
            "c": close,
            "v": 1_000_000 + i,
        })
    return rows


def test_technical_ema_periods_cover_5_to_250():
    assert T.EMA_PERIODS == (5, 10, 20, 60, 120, 250)
    card = T.compute_symbol("TEST", _rising_bars(), Decimal("140"))
    assert tuple(card["ema"]) == T.EMA_PERIODS
    assert all(card["ema"][period] is not None for period in T.EMA_PERIODS)
    assert set(card["ema_slope"]) == {20, 60, 120, 250}


def test_strong_bull_requires_full_ema250_ordering():
    ema = {5: Decimal("105"), 10: Decimal("104"), 20: Decimal("103"),
           60: Decimal("102"), 120: Decimal("101"), 250: Decimal("100")}
    slopes = {20: T.UP, 60: T.UP, 120: T.UP, 250: T.UP}
    assert T._daily_trend_state(Decimal("106"), ema, slopes, Decimal("2")) == "强多头"

    # EMA250 尚未形成完整多头排列时，不应误标为强多头。
    ema[250] = Decimal("103")
    assert T._daily_trend_state(Decimal("106"), ema, slopes, Decimal("2")) != "强多头"


def test_medium_trend_break_uses_ema60():
    ema = {5: Decimal("99"), 10: Decimal("100"), 20: Decimal("101"),
           60: Decimal("102"), 120: Decimal("95"), 250: Decimal("90")}
    slopes = {20: T.DOWN, 60: T.FLAT, 120: T.UP, 250: T.UP}
    assert T._daily_trend_state(Decimal("100"), ema, slopes, Decimal("2")) == "中期趋势破坏"
    rec = T.decide(
        {"daily_trend_state": "中期趋势破坏", "position_state": "明显转弱",
         "technical_summary": "中期趋势破坏"},
        data_ok=True, risk_breached=False, over_limit=False,
        plan_allows_add=False, confirmation=T.CLOSE_CONFIRMED)
    assert rec["final_action"] == "CHECK_REDUCE"
    assert "EMA60" in rec["reason"]
