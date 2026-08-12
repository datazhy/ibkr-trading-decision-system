"""P0: IBKR official PnL and Futu quote-shadow reliability boundaries."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock

from fastapi.testclient import TestClient

from app.config import Config
from app.db import Database
from app.ibkr_client import IBKRClient
from app.pnl_reliability import (
    PnLSubscription,
    classify_futu_reconciliation,
    classify_official_reconciliation,
    pnl_quality,
    validate_callback,
)
from app.rules import RULES_VERSION
from app.scheduler import build_scheduler
from app.sync import SyncService
from app.webapp import create_app


def _subscription(**overrides) -> PnLSubscription:
    values = {
        "request_id": "41",
        "account": "U1",
        "model_code": "",
        "conid": None,
        "connection_generation": 3,
        "subscription_generation": 7,
        "subscribed_at": "2026-07-31T13:30:00+00:00",
    }
    values.update(overrides)
    return PnLSubscription(**values)


def test_callback_gate_rejects_only_structural_mismatches():
    meta = _subscription()
    assert validate_callback(
        meta, current_account="U1", current_connection_generation=3,
        callback_account="U1", callback_model_code="", callback_request_id="41",
    ).accepted

    stale = validate_callback(
        meta, current_account="U1", current_connection_generation=4)
    assert stale.reasons == ("STALE_CONNECTION_GENERATION",)

    mismatch = validate_callback(
        meta, current_account="U2", current_connection_generation=3,
        callback_account="U2", callback_model_code="growth",
        callback_request_id="99")
    assert set(mismatch.reasons) == {
        "ACCOUNT_MISMATCH", "MODEL_CODE_MISMATCH", "REQ_ID_MISMATCH"}


def test_large_and_fast_pnl_changes_are_not_amount_outliers(make_config):
    client = IBKRClient(make_config())
    client._connection_generation = 1
    client._live_account = "U1"
    obj = SimpleNamespace(
        reqId=8, account="U1", modelCode="", dailyPnL=Decimal("8000"),
        unrealizedPnL=Decimal("7500"), realizedPnL=Decimal("500"))
    client._register_pnl_subscription(obj, account="U1")
    client._record_pnl_callback("pnl", obj)
    obj.dailyPnL = Decimal("1324.78")
    client._record_pnl_callback("pnl", obj)

    callbacks, _ = client.drain_pnl_audit()
    assert [row["daily_pnl"] for row in callbacks] == [
        Decimal("8000"), Decimal("1324.78")]
    assert all(row["accepted"] for row in callbacks)
    assert all(row["quality_reasons"] == [] for row in callbacks)


def test_old_connection_generation_callback_is_audited_and_rejected(make_config):
    client = IBKRClient(make_config())
    client._connection_generation = 1
    client._live_account = "U1"
    obj = SimpleNamespace(
        reqId=11, account="U1", modelCode="", dailyPnL=1,
        unrealizedPnL=1, realizedPnL=0)
    client._register_pnl_subscription(obj, account="U1")
    client._connection_generation = 2
    client._record_pnl_callback("pnl", obj)

    callbacks, _ = client.drain_pnl_audit()
    assert callbacks[0]["accepted"] is False
    assert callbacks[0]["quality_reasons"] == ["STALE_CONNECTION_GENERATION"]


def test_duplicate_subscription_and_req_id_reuse_are_structural_failures(make_config):
    client = IBKRClient(make_config())
    client._connection_generation = 1
    client._live_account = "U1"
    first = SimpleNamespace(
        reqId=21, account="U1", modelCode="", conId=101,
        dailyPnL=10, unrealizedPnL=10, realizedPnL=0)
    duplicate = SimpleNamespace(
        reqId=22, account="U1", modelCode="", conId=101,
        dailyPnL=10, unrealizedPnL=10, realizedPnL=0)
    reused = SimpleNamespace(
        reqId=21, account="U1", modelCode="", conId=202,
        dailyPnL=20, unrealizedPnL=20, realizedPnL=0)
    client._register_pnl_subscription(first, account="U1", conid=101)
    client._register_pnl_subscription(duplicate, account="U1", conid=101)
    client._register_pnl_subscription(reused, account="U1", conid=202)
    client._record_pnl_callback("pnlSingle", first)
    client._record_pnl_callback("pnlSingle", duplicate)
    client._record_pnl_callback("pnlSingle", reused)

    callbacks, _ = client.drain_pnl_audit()
    reasons = [set(row["quality_reasons"]) for row in callbacks]
    assert "DUPLICATE_ACTIVE_SUBSCRIPTION" in reasons[0]
    assert "REQ_ID_REUSED" in reasons[0]
    assert reasons[1] == {"DUPLICATE_ACTIVE_SUBSCRIPTION"}
    assert reasons[2] == {"REQ_ID_REUSED"}
    assert not any(row["accepted"] for row in callbacks)

    client._unregister_pnl_subscription(duplicate)
    client._unregister_pnl_subscription(reused)
    client._record_pnl_callback("pnlSingle", first)
    recovered, _ = client.drain_pnl_audit()
    assert recovered[0]["accepted"] is True


def test_reconnect_subscription_is_marked_resubscribed(make_config):
    client = IBKRClient(make_config())
    client._connection_generation = 2
    client._live_started = True
    obj = SimpleNamespace(reqId=31)
    client._register_pnl_subscription(obj, account="U1")
    _, lifecycle = client.drain_pnl_audit()
    assert lifecycle[0]["event_type"] == "resubscribed"


def test_reconciliation_allows_timing_and_accounting_differences():
    assert classify_official_reconciliation(
        Decimal("8000"), Decimal("7998")) == "consistent"
    assert classify_official_reconciliation(
        Decimal("8000"), Decimal("7300")) == "explainable_timing_difference"
    assert classify_futu_reconciliation(
        quote_coverage=Decimal("1"), quote_age_seconds=Decimal("2"),
        official_daily=Decimal("8000"), futu_mtm=Decimal("7900"),
    ) == "explainable_accounting_difference"
    assert classify_futu_reconciliation(
        quote_coverage=Decimal("0.75"), quote_age_seconds=Decimal("2"),
        official_daily=Decimal("8000"), futu_mtm=Decimal("7900"),
    ) == "source_stale"


def test_quality_gate_uses_lifecycle_not_dollar_size():
    ok = pnl_quality(
        callback_decision=validate_callback(
            _subscription(), current_account="U1",
            current_connection_generation=3),
        age_seconds=Decimal("1"), source="IBKR_reqPnL")
    assert ok == ("OK", [])
    degraded = pnl_quality(
        callback_decision=validate_callback(
            _subscription(), current_account="U1",
            current_connection_generation=3),
        age_seconds=Decimal("181"), source="IBKR_reqPnL")
    assert degraded == ("DEGRADED", ["STALE_CALLBACK"])


def test_snapshot_persists_futu_shadow_without_overwriting_ibkr(make_config):
    db = Database(make_config().db_path)
    db.init_schema()
    db.write_account_snapshot(
        snapshot_id="s1", ts="2026-07-31T14:00:00+00:00",
        balances={
            "equity": Decimal("100000"), "cash": Decimal("20000"),
            "margin_balance": Decimal("80000"),
            "buying_power": Decimal("160000"), "account": "U1",
            "source": "IBKR_accountSummary",
        },
        positions=[{
            "symbol": "AAA", "qty": Decimal("10"),
            "avg_cost": Decimal("80"), "market_value": Decimal("950"),
            "upl": Decimal("150"), "price": Decimal("97.5"),
            "price_source": "FUTU_OpenD", "source": "IBKR_portfolio",
            "market_value_source": "IBKR_updatePortfolio",
            "upl_source": "IBKR_updatePortfolio",
            "futu_market_value": Decimal("975"),
            "futu_unrealized_estimate": Decimal("175"),
            "futu_intraday_mtm": Decimal("75"),
            "futu_quote_age_seconds": Decimal("1.2"),
        }])
    with db.connect() as conn:
        row = dict(conn.execute(
            "SELECT * FROM positions_snapshot WHERE snapshot_id='s1'").fetchone())
    assert row["market_value"] == "950"
    assert row["upl"] == "150"
    assert row["market_value_source"] == "IBKR_updatePortfolio"
    assert row["futu_market_value"] == "975"
    assert row["futu_unrealized_estimate"] == "175"


def test_pnl_audit_and_diagnostics_database_roundtrip(make_config):
    db = Database(make_config().db_path)
    db.init_schema()
    assert db.insert_ibkr_pnl_callbacks([{
        "callback_type": "pnl", "received_at": "2026-07-31T14:00:00+00:00",
        "request_id": "1", "account": "U1", "model_code": "",
        "connection_generation": 2, "subscription_generation": 3,
        "session_date": "2026-07-31", "source": "IBKR_reqPnL",
        "daily_pnl": Decimal("8000"), "unrealized_pnl": Decimal("7500"),
        "realized_pnl": Decimal("500"), "accepted": True,
        "quality_reasons": [],
    }]) == 1
    assert db.insert_ibkr_pnl_subscription_events([{
        "event_type": "subscribed", "occurred_at": "2026-07-31T13:59:59+00:00",
        "request_id": "1", "account": "U1", "model_code": "",
        "connection_generation": 2, "subscription_generation": 3,
    }]) == 1
    assert db.insert_pnl_reconciliation({
        "observed_at": "2026-07-31T14:00:01+00:00", "sync_run_id": "run1",
        "snapshot_id": "snap1", "account": "U1",
        "official_daily_pnl": Decimal("8000"),
        "position_daily_total": Decimal("7998"),
        "futu_intraday_mtm": Decimal("7900"),
        "futu_total_position_value": Decimal("90000"),
        "quote_coverage": Decimal("1"), "official_position_status": "consistent",
        "futu_status": "explainable_accounting_difference",
        "large_but_explained": True, "limitations": ["RESET_TIME_DIFF"],
    })
    db.insert_scheduler_job_diagnostic({
        "job_id": "market_sync", "scheduled_at": "2026-07-31T14:00:00+00:00",
        "observed_at": "2026-07-31T14:03:01+00:00", "outcome": "EXECUTED",
        "lateness_seconds": Decimal("181"),
    })

    with db.connect() as conn:
        callback = dict(conn.execute("SELECT * FROM ibkr_pnl_callbacks").fetchone())
        lifecycle = dict(conn.execute(
            "SELECT * FROM ibkr_pnl_subscription_events").fetchone())
    assert callback["daily_pnl"] == "8000" and callback["accepted"] == 1
    assert json.loads(callback["quality_reasons"]) == []
    assert lifecycle["event_type"] == "subscribed"
    assert db.recent_pnl_reconciliations()[0]["large_but_explained"] == 1
    assert db.recent_scheduler_job_diagnostics(job_id="market_sync")[0][
        "lateness_seconds"] == "181"


def test_sync_audit_labels_large_official_profit_as_explained_without_notifications(
        make_config):
    db = Database(make_config().db_path)
    db.init_schema()
    callback = {
        "callback_type": "pnl", "received_at": "2026-07-31T14:00:00+00:00",
        "request_id": "1", "account": "U1", "model_code": "",
        "connection_generation": 2, "subscription_generation": 3,
        "session_date": "2026-07-31", "source": "IBKR_reqPnL",
        "daily_pnl": Decimal("8000"), "unrealized_pnl": Decimal("7500"),
        "realized_pnl": Decimal("500"), "accepted": True,
        "quality_reasons": [],
    }
    broker = SimpleNamespace(drain_pnl_audit=lambda: ([callback], []))
    sync = SyncService.__new__(SyncService)
    sync.db = db
    sync.client = broker
    sync._persist_pnl_audit(
        run_id="run-large", snapshot_id="snap-large", account="U1",
        balances={
            "_account_pnl": {
                "daily_pnl": Decimal("8000"), "realized_pnl": Decimal("500"),
                "unrealized_pnl": Decimal("7500"), "source": "IBKR_reqPnL"},
            "_futu_shadow": {
                "quote_coverage": Decimal("1"),
                "futu_intraday_mark_to_market_change": Decimal("7900"),
                "futu_total_position_value": Decimal("95000"),
                "account_pnl_authoritative": False,
            },
        },
        positions=[
            {"day_pnl": Decimal("4000")}, {"day_pnl": Decimal("3999")}],
    )
    row = db.recent_pnl_reconciliations()[0]
    assert row["official_daily_pnl"] == "8000"
    assert row["official_position_status"] == "consistent"
    assert row["futu_status"] == "explainable_accounting_difference"
    assert row["large_but_explained"] == 1


def test_reconciliation_and_scheduler_apis_are_read_only(make_config):
    cfg = make_config()
    db = Database(cfg.db_path)
    db.init_schema()
    db.insert_pnl_reconciliation({
        "observed_at": "2026-07-31T14:00:01+00:00", "sync_run_id": "run1",
        "snapshot_id": "snap1", "account": "U1",
        "official_daily_pnl": Decimal("8000"),
        "position_daily_total": Decimal("7998"), "quote_coverage": Decimal("1"),
        "official_position_status": "consistent", "futu_status": "consistent",
        "large_but_explained": True, "limitations": [],
    })
    # 相对 now 落点：诊断 API 按 hours=168 的滚动窗口过滤，固定日期会随真实时间
    # 推移滑出窗口（原固定 2026-07-31 即是此类「时间炸弹」）。
    recent = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    db.insert_scheduler_job_diagnostic({
        "job_id": "positions_rules", "observed_at": recent,
        "outcome": "EXECUTED", "lateness_seconds": 0,
    })
    with db.connect() as conn:
        before = conn.total_changes
    client = TestClient(create_app(cfg, db))
    rec = client.get("/api/v1/risk/pnl-source/reconciliation?hours=168").json()
    diag = client.get("/api/v1/system/scheduler/diagnostics?hours=168").json()
    assert rec["success"] and rec["data"]["authority"] == {
        "account_pnl": "IBKR_reqPnL",
        "position_pnl": "IBKR_reqPnLSingle",
        "market_quote_shadow": "futu",
        "futu_account_pnl_authoritative": False,
    }
    assert diag["success"] and diag["data"]["per_job"]["positions_rules"] == {
        "executed": 1, "missed": 0, "error": 0}
    with db.connect() as conn:
        assert conn.total_changes == before


def test_scheduler_isolates_market_and_quote_jobs(make_config):
    cfg = make_config(futu_quote_enabled=True, live_sync_enabled=False,
                      hyperliquid_enabled=True, hl_history_backfill_enabled=True)
    db = Database(cfg.db_path)
    db.init_schema()
    sync = MagicMock()
    notifier = SimpleNamespace(send=lambda *_a, **_k: None,
                               send_deduped=lambda *_a, **_k: None)
    engine = MagicMock()
    scheduler = build_scheduler(cfg, db, sync, notifier, engine)
    jobs = {job.id: job for job in scheduler.get_jobs()}
    assert jobs["positions_rules"].executor == "default"
    assert jobs["market_sync"].executor == "market"
    assert jobs["daily_bars_backfill"].executor == "market"
    assert jobs["futu_quote_refresh"].executor == "quotes"
    assert jobs["hyperliquid_equity_backfill"].executor == "chain"
    assert jobs["hyperliquid_data_quality"].executor == "chain_report"
    assert {"default", "market", "quotes", "chain", "chain_report"} <= set(
        scheduler._executors)
    scheduler.shutdown(wait=False) if scheduler.running else None


def test_p0_switch_and_rule_contract_remain_unchanged(make_config):
    cfg = make_config()
    assert isinstance(cfg, Config)
    assert cfg.daily_pnl_v2_enabled is False
    assert cfg.risk_daily_loss_max == "0.03"
    assert RULES_VERSION == "R1-R7/2026-07"
