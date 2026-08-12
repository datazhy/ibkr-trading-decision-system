from datetime import date, timedelta
from decimal import Decimal

from app import opportunity_service as opp


def _bars(n=260, *, pullback=False, last_volume=2_000_000, dollar_volume=True):
    start = date(2025, 1, 1)
    rows = []
    for i in range(n):
        close = Decimal("70") + Decimal(i) * Decimal("30") / Decimal(max(1, n - 1))
        open_ = close - Decimal("0.2") if i % 2 == 0 else close + Decimal("0.2")
        volume = 1_000_000 if dollar_volume else 1_000
        rows.append({
            "date": (start + timedelta(days=i)).isoformat(),
            "o": open_, "h": close + Decimal("0.5"),
            "l": close - Decimal("0.5"), "c": close, "v": volume,
            "source": "test", "adjustment_mode": "split-adjusted",
            "bar_as_of": (start + timedelta(days=i)).isoformat(),
        })
    if pullback:
        for i in range(n - 20, n - 5):
            rows[i]["h"] = Decimal("112")
        for i in range(n - 5, n):
            rows[i].update({"o": Decimal("101"), "h": Decimal("103"),
                            "l": Decimal("100.5"), "c": Decimal("102"),
                            "v": 500_000})
    else:
        rows[-1].update({"o": Decimal("100"), "h": Decimal("103"),
                         "l": Decimal("99.5"), "c": Decimal("102"),
                         "v": last_volume})
    if not dollar_volume:
        for row in rows:
            row["v"] = 1_000
    return rows


def _spy(n=260):
    rows = _bars(n)
    for row in rows:
        row.update({"o": Decimal("100"), "h": Decimal("101"),
                    "l": Decimal("99"), "c": Decimal("100"), "v": 5_000_000})
    return rows


def _mock_technical(monkeypatch, codes=()):
    monkeypatch.setattr(opp.technical, "compute_symbol", lambda *a, **k: {
        "ema": {20: Decimal("100"), 50: Decimal("90")},
        "ema_slope": {50: opp.technical.UP},
        "weekly_trend_state": "周线多头",
        "atr14": Decimal("2"),
    })
    monkeypatch.setattr(opp.technical, "evaluate_rules", lambda card: [
        {"code": code, "title": code} for code in codes
    ])
    monkeypatch.setattr(opp, "_candidate_stop", lambda *a, **k: (
        Decimal("95"), Decimal("7")))


def test_prior_high_excludes_current_bar():
    bars = _bars(25)
    bars[-1]["h"] = Decimal("999")
    assert opp.prior_high(bars, 20) != Decimal("999")
    assert opp.prior_high(bars, 20) == max(row["h"] for row in bars[-21:-1])


def test_breakout_exact_score_and_ready(make_config, monkeypatch):
    _mock_technical(monkeypatch)
    result = opp.score_candidate(
        "TEST", _bars(), _spy(), market_state="supportive",
        cfg=make_config(), expected_bar_date=_bars()[-1]["date"])
    assert result["setup_type"] == "BREAKOUT"
    assert result["score_components"] == {
        "trend": 30, "setup": 25, "volume_price": 15,
        "relative_strength": 15, "market": 10, "penalties": 0,
    }
    assert result["score"] == 95
    assert result["status"] == "READY"


def test_pullback_exact_score_and_ready(make_config, monkeypatch):
    _mock_technical(monkeypatch)
    bars = _bars(pullback=True)
    result = opp.score_candidate(
        "TEST", bars, _spy(), market_state="supportive", cfg=make_config(),
        expected_bar_date=bars[-1]["date"])
    assert result["setup_type"] == "PULLBACK"
    assert result["score"] == 95
    assert result["status"] == "READY"


def test_limited_history_caps_score_at_79(make_config, monkeypatch):
    _mock_technical(monkeypatch)
    bars = _bars(130)
    result = opp.score_candidate(
        "TEST", bars, _spy(130), market_state="supportive", cfg=make_config(),
        expected_bar_date=bars[-1]["date"])
    assert result["confidence"] == "LIMITED"
    assert result["score"] == 79


def test_technical_guardrails_t3_t6_t7_t8(make_config, monkeypatch):
    bars, spy = _bars(), _spy()
    _mock_technical(monkeypatch, ("T3",))
    assert opp.score_candidate("T3", bars, spy, market_state="supportive", cfg=make_config())["score"] == 75
    _mock_technical(monkeypatch, ("T6",))
    assert opp.score_candidate("T6", bars, spy, market_state="supportive", cfg=make_config())["status"] == "WATCH"
    _mock_technical(monkeypatch, ("T7",))
    assert opp.score_candidate("T7", bars, spy, market_state="supportive", cfg=make_config())["status"] == "BLOCKED"
    _mock_technical(monkeypatch, ("T8",))
    assert opp.score_candidate("T8", bars, spy, market_state="supportive", cfg=make_config())["score"] == 85


def test_hard_gates_block_stale_illiquid_non_equity_and_insufficient(make_config, monkeypatch):
    _mock_technical(monkeypatch)
    cfg = make_config()
    good = _bars()
    cases = [
        opp.score_candidate("STALE", good, _spy(), market_state="supportive", cfg=cfg,
                            expected_bar_date="2099-01-01"),
        opp.score_candidate("ILLIQ", _bars(dollar_volume=False), _spy(),
                            market_state="supportive", cfg=cfg),
        opp.score_candidate("ETF", good, _spy(), market_state="supportive", cfg=cfg,
                            asset_type="ETF"),
        opp.score_candidate("SHORT", _bars(119), _spy(119), market_state="supportive", cfg=cfg),
    ]
    assert all(row["status"] == "BLOCKED" for row in cases)


def test_missing_reliable_stop_blocks_candidate(make_config, monkeypatch):
    _mock_technical(monkeypatch)
    monkeypatch.setattr(opp, "_candidate_stop", lambda *a, **k: (None, None))
    result = opp.score_candidate("TEST", _bars(), _spy(), market_state="supportive", cfg=make_config())
    assert result["status"] == "BLOCKED"
    assert "无法生成可靠止损" in result["blocked_reasons"]
