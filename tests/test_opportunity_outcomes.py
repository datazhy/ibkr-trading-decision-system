from datetime import date, timedelta
from decimal import Decimal

from app.db import Database
from app.opportunity_service import OpportunityService, rules_hash


def _service(make_config, tmp_path):
    cfg = make_config(db_path=tmp_path / "outcomes.db", opportunity_enabled=True)
    db = Database(cfg.db_path)
    db.init_schema()
    return cfg, db, OpportunityService(cfg, db)


def _bars(symbol, *, count=60, stock=True):
    start = date(2026, 5, 1)
    rows = []
    for i in range(count):
        close = Decimal("90") + Decimal(i) if stock else Decimal("100") + Decimal(i) / 2
        rows.append({
            "date": (start + timedelta(days=i)).isoformat(),
            "o": close - Decimal("0.5"), "h": close + Decimal("1"),
            "l": close - Decimal("2"), "c": close, "v": 1_000_000,
            "source": "test", "adjustment_mode": "split-adjusted",
        })
    return rows


def _candidate(cfg, db, svc, *, scan_id, symbol, start_date, price):
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO opportunity_scan_runs "
            "(id,status,trigger_source,started_at,finished_at,universe_count,eligible_count,"
            "ready_count,watch_count,blocked_count,market_state,rules_version,rules_hash,data_as_of) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (scan_id, "SUCCESS", "test", start_date + "T22:00:00+00:00",
             start_date + "T22:01:00+00:00", 1, 1, 1, 0, 0, "supportive",
             "test", rules_hash(cfg), start_date),)
    svc._save_candidate(scan_id, {
        "symbol": symbol, "setup_type": "BREAKOUT", "status": "READY", "score": 90,
        "grade": "A", "confidence": "FULL", "current_price": str(price),
        "ema20": "95", "ema50": "90", "atr14": "2", "rvol20": "1.5",
        "entry_low": str(price), "entry_high": str(price), "stop_price": "95",
        "risk_per_share": str(Decimal(str(price)) - Decimal("95")),
        "median_dollar_volume_20d": "50000000", "relative_strength_20d": "0.1",
        "relative_strength_60d": "0.2", "score_components": {}, "reasons": ["test"],
        "blocked_reasons": [], "input_refs": {}, "bar_as_of": start_date,
        "adjustment_mode": "split-adjusted", "quality_status": "FULL",
    }, rules_hash(cfg))
    with db.connect() as conn:
        return conn.execute(
            "SELECT id FROM opportunity_candidates WHERE scan_id=?", (scan_id,)).fetchone()[0]


def test_calculates_5_10_20_40_day_returns_alpha_mfe_mae_and_r(make_config, tmp_path):
    cfg, db, svc = _service(make_config, tmp_path)
    stock, spy = _bars("AAA"), _bars("SPY", stock=False)
    db.upsert_daily_bars("AAA", stock)
    db.upsert_daily_bars("SPY", spy)
    start = stock[10]
    cid = _candidate(cfg, db, svc, scan_id="scan-complete", symbol="AAA",
                     start_date=start["date"], price=start["c"])
    result = svc.calculate_outcomes(candidate_id=cid)
    assert result["complete"] == 4
    assert result["pending"] == 0
    with db.connect() as conn:
        rows = {r["horizon_days"]: dict(r) for r in conn.execute(
            "SELECT * FROM opportunity_outcomes WHERE candidate_id=?", (cid,)).fetchall()}
    five = rows[5]
    assert Decimal(five["return_pct"]) == Decimal("0.05")
    expected_spy = Decimal("107.5") / Decimal("105") - 1
    assert Decimal(five["spy_return_pct"]) == expected_spy
    assert Decimal(five["alpha_pct"]) == Decimal("0.05") - expected_spy
    assert Decimal(five["mfe_pct"]) == Decimal("0.06")
    assert Decimal(five["mae_pct"]) == Decimal("-0.01")
    assert Decimal(five["r_multiple"]) == Decimal("1")
    summary = svc.outcome_summary()
    assert summary["ready_samples_with_outcomes"] == 1
    assert summary["tuning_allowed"] is False
    assert {m["horizon_days"] for m in summary["metrics"]} == {5, 10, 20, 40}


def test_future_bars_are_pending_not_guessed(make_config, tmp_path):
    cfg, db, svc = _service(make_config, tmp_path)
    stock, spy = _bars("AAA", count=20), _bars("SPY", count=20, stock=False)
    db.upsert_daily_bars("AAA", stock)
    db.upsert_daily_bars("SPY", spy)
    start = stock[-1]
    cid = _candidate(cfg, db, svc, scan_id="scan-pending", symbol="AAA",
                     start_date=start["date"], price=start["c"])
    result = svc.calculate_outcomes(candidate_id=cid)
    assert result["pending"] == 4
    with db.connect() as conn:
        statuses = [r[0] for r in conn.execute(
            "SELECT status FROM opportunity_outcomes WHERE candidate_id=?", (cid,)).fetchall()]
    assert statuses == ["PENDING"] * 4
