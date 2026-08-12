from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.db import Database
from app.hyperliquid_db import HyperliquidDatabase
from app.hyperliquid_outcomes import calculate_outcome, derive_actionable_at
from app.hyperliquid_results import (save_equity_bars, calculate_and_save_signal,
                                     promotion_gate, record_validation_run,
                                     auto_suspend_after_failed_windows,
                                     set_promotion, record_monthly_health)
from app.hyperliquid_scoring import REDUCED_RULES_VERSION, signed_score, transition
from app.hyperliquid_service import HyperliquidService
from app.hyperliquid_alerts import HyperliquidAlertDispatcher
from app import recommendation_service as reco


def test_hl_read_only_does_not_create_or_write(tmp_path):
    path = tmp_path / "missing" / "hyperliquid.db"
    db = HyperliquidDatabase(path)
    assert not db.available()
    try:
        db.connect(read_only=True).__enter__()
    except FileNotFoundError:
        pass
    else:
        raise AssertionError("read-only missing DB unexpectedly opened")
    assert not path.exists()


def test_context_splits_mark_basis_from_off_rth_return(tmp_path):
    db = HyperliquidDatabase(tmp_path / "hl.db")
    db.init_schema()
    svc = HyperliquidService(type("Cfg", (), {"hl_dex": "xyz"})(), db)
    universe = [{"name": "xyz:NVDA"}]
    svc.ingest_contexts(universe, [{"oraclePx": "100", "markPx": "101", "openInterest": "2"}],
                        observed_at="2026-08-11T17:00:00+00:00")
    svc.ingest_contexts(universe, [{"oraclePx": "105", "markPx": "106"}],
                        observed_at="2026-08-11T22:00:00+00:00")
    with db.connect(read_only=True) as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM hl_asset_context_buckets_5m ORDER BY bucket_start")]
    assert rows[0]["mark_oracle_deviation"] == "0.01"
    assert rows[0]["off_rth_oracle_return"] is None
    assert rows[1]["off_rth_oracle_return"] == "0.05"
    assert rows[1]["off_rth_basis_change"] is not None


def test_transition_does_not_keep_bullish_when_score_turns_bearish():
    assert transition("BULLISH", -50) == "NEUTRAL"
    assert transition("BULLISH", -60) == "BEARISH"
    assert transition("BEARISH", 50) == "NEUTRAL"
    assert transition("BEARISH", 60) == "BULLISH"


def test_actionable_at_moves_off_rth_signal_to_next_open():
    value = derive_actionable_at("2026-08-10T22:00:00+00:00", "OFF_RTH")
    assert value.startswith("2026-08-11T13:30:00")


def test_signed_score_requires_all_components():
    result = signed_score({"off_rth_oracle_return": "0.5"}, "INTRADAY", "OFF_RTH")
    assert result["score"] is None
    assert result["quality_status"] == "LIMITED"


def test_reduced_no_ctx_profile_is_separate_and_never_requires_oracle_feature():
    result = signed_score({
        "hl_trade_return_5m": "0.2",
        "funding_premium_index": "0.1",
        "qualified_wallet_aggressor_imbalance": "0.3",
        "qualified_wallet_net_exposure": "0.4",
        "wallet_breadth": "0.5",
    }, "INTRADAY", "OFF_RTH", profile="REDUCED_NO_CTX")
    assert result["quality_status"] == "READY"
    assert result["rules_version"] == REDUCED_RULES_VERSION
    assert "off_rth_oracle_return" not in result["components"]


def test_outcome_off_rth_uses_next_rth_bar_and_spy(tmp_path):
    main = Database(Path(tmp_path) / "main.db")
    main.init_schema()
    start = datetime(2026, 8, 10, 13, 30, tzinfo=timezone.utc)
    stock = []
    spy = []
    for i in range(30):
        t = start + timedelta(minutes=5 * i)
        stock.append({"symbol": "NVDA", "bar_start": t.isoformat(), "o": 100 + i,
                      "h": 101 + i, "l": 99 + i, "c": 100 + i, "v": 10,
                      "session": "REGULAR", "use_rth": True})
        spy.append({"symbol": "SPY", "bar_start": t.isoformat(), "o": 500,
                    "h": 501, "l": 499, "c": 500 + i / 10, "v": 10,
                    "session": "REGULAR", "use_rth": True})
    save_equity_bars(main, stock)
    save_equity_bars(main, spy)
    signal = {"signal_id": "s-off", "model": "INTRADAY", "regime": "OFF_RTH",
              "session": "OFF_RTH", "coin": "xyz:NVDA", "canonical_symbol": "NVDA",
              "state": "BULLISH", "signal_at": (start - timedelta(hours=1)).isoformat(),
              "actionable_at": start.isoformat()}
    out = calculate_outcome(signal, stock, spy, "30m")
    assert out["status"] == "COMPLETE"
    assert out["entry_time"] == start.isoformat()
    assert out["alpha_pct"] is not None
    calculate_and_save_signal(main, signal, stock, spy, horizons=("30m",))
    with main.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM hl_signal_outcomes").fetchone()[0] == 1


def test_wallet_cycle_uses_funding_and_fee(tmp_path):
    hl = HyperliquidDatabase(Path(tmp_path) / "hl.db")
    hl.init_schema()
    cfg = type("Cfg", (), {"hl_dex": "xyz", "hl_wallet_weight_cap": "0.20",
                            "hl_min_directional_wallets": 5})()
    svc = HyperliquidService(cfg, hl)
    address = "0x" + "1" * 40
    svc.ingest_wallet_fills(address, [
        {"coin": "xyz:NVDA", "time": "1000", "tid": "1", "px": "100", "sz": "2",
         "dir": "Open Long", "startPosition": "0", "closedPnl": "0", "fee": "-1"},
        {"coin": "xyz:NVDA", "time": "2000", "tid": "2", "px": "110", "sz": "2",
         "dir": "Close Long", "startPosition": "2", "closedPnl": "20", "fee": "-2"},
    ])
    svc.ingest_wallet_funding(address, [{"time": "1500", "coin": "xyz:NVDA", "delta": {"usdc": "-0.5"}}])
    assert svc.rebuild_wallet_cycles(address) == 1
    with hl.connect(read_only=True) as conn:
        row = conn.execute("SELECT net_pnl FROM hl_wallet_cycles").fetchone()
    assert row[0] == "22.5"


def test_service_classifies_wallet_after_history_rebuild(tmp_path):
    hl = HyperliquidDatabase(Path(tmp_path) / "hl.db")
    hl.init_schema()
    svc = HyperliquidService(type("Cfg", (), {"hl_dex": "xyz"})(), hl)
    address = "0x" + "3" * 40
    svc.ingest_wallet_fills(address, [
        {"coin": "xyz:NVDA", "time": str(1_000 + i), "tid": str(i),
         "px": "100", "sz": "1", "dir": "Open Long", "crossed": True,
         "startPosition": "0", "closedPnl": "0", "fee": "0"}
        for i in range(20)
    ])
    result = svc.classify_wallet(
        address, valid_from="2026-08-12T00:00:00+00:00",
        data_origin="HISTORICAL_BACKFILL")
    assert result["classification"] in {"DIRECTIONAL", "MIXED", "LIQUIDITY"}


def test_chain_guardrail_only_tightens():
    rec = {"action": reco.ADD_WATCH, "reason": "计划加仓", "decided_by": reco.BY_PLAN,
           "current_weight": None, "target_weight": None, "max_weight": None,
           "delta_weight": None, "quantity": None, "amount": None,
           "trigger_condition": None, "invalidation_condition": None, "blocked_reason": None,
           "covered_action_ids": [], "confirmation": None,
           "rule_set_hash": reco._rule_set_hash()}
    out = reco._apply_chain_guardrail(rec, {"model": "SWING"})
    assert out["action"] == reco.DO_NOT_ADD
    assert out["decided_by"] == reco.BY_CHAIN
    rec["action"] = reco.EXIT
    assert reco._apply_chain_guardrail(rec, {"model": "SWING"})["action"] == reco.EXIT


def test_chain_alerts_have_separate_quota_and_dedupe(tmp_path):
    main = Database(Path(tmp_path) / "main.db")
    main.init_schema()
    calls = []
    dispatcher = HyperliquidAlertDispatcher(main, max_per_hour=1, max_per_day=1)
    signal = {"signal_id": "sig-a", "canonical_symbol": "NVDA", "state": "BULLISH"}
    assert dispatcher.dispatch(signal, lambda symbol, body: calls.append(symbol))["status"] == "SENT"
    assert dispatcher.dispatch(signal, lambda symbol, body: calls.append(symbol))["status"] == "DUPLICATE"
    assert len(calls) == 1


def test_promotion_gate_requires_oos_sample_and_paired_baseline():
    rows = [{"status": "COMPLETE", "model": "INTRADAY", "horizon": "2h",
             "direction": "BULLISH", "canonical_symbol": "NVDA",
             "entry_time": f"2026-01-{(i % 20) + 1:02d}T14:00:00+00:00",
             "return_pct": "0.20", "mae_pct": "-0.01"} for i in range(25)]
    baseline = [{**row, "return_pct": "0.05"} for row in rows]
    gate = promotion_gate(rows, baseline_rows=baseline, model="INTRADAY",
                          min_raw_n=20, min_effective_n=20, min_directional_n=10)
    assert gate["verdict"] == "FAIL"  # missing bearish direction is a hard gate
    assert "directional_n<10" in gate["reasons"]


def test_two_failed_validation_windows_suspend_active_model(tmp_path):
    db = Database(Path(tmp_path) / "main.db")
    db.init_schema()
    set_promotion(db, model="INTRADAY", rules_version="r1", rules_hash="h1",
                  policy_version="p1", policy_hash="ph1", state="ACTIVE",
                  evaluation_run_id="seed", approved_by="tester")
    for _ in range(2):
        record_validation_run(db, model="INTRADAY", rules_version="r1", rules_hash="h1",
                              policy_version="p1", policy_hash="ph1", rows=[], verdict="FAIL")
    result = auto_suspend_after_failed_windows(db, model="INTRADAY", rules_version="r1",
                                               rules_hash="h1", policy_hash="ph1")
    assert result["status"] == "SUSPENDED"
    with db.connect() as conn:
        assert conn.execute("SELECT state FROM hl_model_promotions WHERE model='INTRADAY'").fetchone()[0] == "SUSPENDED"


def test_retention_keeps_recent_dedupe_and_deletes_old_rows(tmp_path):
    hl = HyperliquidDatabase(Path(tmp_path) / "hl.db")
    hl.init_schema()
    cfg = type("Cfg", (), {"hl_recent_trades_retention_hours": 72,
                            "hl_retention_days": 400})()
    svc = HyperliquidService(cfg, hl)
    svc.ingest_trades([{"coin": "xyz:NVDA", "time": "1000", "tid": "old",
                        "side": "B", "px": "100", "sz": "1", "users": ["0x"+"1"*40, "0x"+"2"*40]}],
                      received_at="2025-01-01T00:00:00+00:00")
    svc.ingest_trades([{"coin": "xyz:NVDA", "time": "2000", "tid": "new",
                        "side": "B", "px": "100", "sz": "1", "users": ["0x"+"1"*40, "0x"+"2"*40]}],
                      received_at="2026-08-12T00:00:00+00:00")
    with hl.connect() as conn:
        conn.execute("INSERT INTO hl_recent_trades(coin,block_time,tid,side,px,sz,buyer,seller,received_at,raw_json) "
                     "VALUES(?,?,?,?,?,?,?,?,?,?)",
                     ("xyz:NVDA", "3000", "old2", "B", "100", "1", None, None,
                      "2025-01-01T00:00:00+00:00", "{}"))
    deleted = svc.cleanup_retention(now="2026-08-12T00:00:00+00:00")
    assert deleted["hl_recent_trades"] == 1
    with hl.connect(read_only=True) as conn:
        assert conn.execute("SELECT COUNT(*) FROM hl_recent_trades").fetchone()[0] == 1


def test_monthly_health_isolated_when_hl_unavailable(tmp_path):
    main = Database(Path(tmp_path) / "main.db")
    main.init_schema()
    hl = HyperliquidDatabase(Path(tmp_path) / "missing.db")
    result = record_monthly_health(main, hl, month="2026-08")
    assert result["status"] == "UNAVAILABLE"
    with main.connect() as conn:
        assert conn.execute("SELECT status FROM hl_health_reports WHERE report_month='2026-08'").fetchone()[0] == "UNAVAILABLE"
