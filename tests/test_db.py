from datetime import timedelta
from decimal import Decimal

import pytest

from app.db import Database, to_decimal


@pytest.fixture
def db(tmp_path):
    d = Database(tmp_path / "test.db")
    d.init_schema()
    return d


def test_positions_and_balances_roundtrip(db):
    ts = "2026-07-10T13:30:00+00:00"
    db.insert_positions_snapshot(ts, [
        {"symbol": "MU", "qty": Decimal("100"), "avg_cost": Decimal("50.25"),
         "market_value": Decimal("5500"), "upl": Decimal("475")},
    ])
    db.insert_balances_snapshot(ts, {
        "equity": Decimal("10000"), "cash": Decimal("2000"),
        "margin_balance": Decimal("-1500"), "buying_power": Decimal("8000"),
    })
    rows = db.latest_positions()
    assert len(rows) == 1
    assert to_decimal(rows[0]["avg_cost"]) == Decimal("50.25")
    bal = db.latest_balances()
    assert to_decimal(bal["margin_balance"]) == Decimal("-1500")


def test_orders_upsert_updates_status(db):
    base = {"order_id": "1", "ts": "2026-07-10T13:00:00+00:00", "symbol": "MU",
            "side": "BUY", "order_type": "LIMIT", "qty": Decimal("10"),
            "price": Decimal("50"), "status": "WORKING", "raw_json": "{}"}
    db.upsert_orders([base])
    updated = dict(base, status="FILLED")
    db.upsert_orders([updated])
    with db.connect() as conn:
        rows = conn.execute("SELECT order_id, status FROM orders").fetchall()
    assert len(rows) == 1
    assert rows[0]["status"] == "FILLED"


def test_transactions_upsert_idempotent(db):
    txn = {"txn_id": "abc", "ts": "2026-07-10T14:00:00+00:00", "symbol": "MU",
           "action": "TRADE", "qty": Decimal("100"), "price": Decimal("50.25"),
           "fees": Decimal("0.5"), "amount": Decimal("-5025.5"), "raw_json": "{}"}
    db.upsert_transactions([txn])
    db.upsert_transactions([txn])
    with db.connect() as conn:
        n = conn.execute("SELECT COUNT(*) c FROM transactions").fetchone()["c"]
    assert n == 1


def test_daily_bars_upsert(db):
    bars = [{"date": "2026-07-09", "o": 10, "h": 12, "l": 9, "c": 11, "v": 1000}]
    db.upsert_daily_bars("MU", bars)
    db.upsert_daily_bars("MU", [{"date": "2026-07-09", "o": 10, "h": 12, "l": 9, "c": 11.5, "v": 1200}])
    with db.connect() as conn:
        row = conn.execute("SELECT c, v FROM daily_bars WHERE symbol='MU'").fetchone()
    assert to_decimal(row["c"]) == Decimal("11.5")
    assert row["v"] == 1200


def test_secret_and_sync_state(db):
    db.set_secret("acct", "enc-blob")
    assert db.get_secret("acct") == "enc-blob"
    assert db.get_secret("missing") is None
    db.set_sync_state("cursor", "2026-07-10")
    assert db.get_sync_state("cursor") == "2026-07-10"


def test_alert_dedup_tracking(db):
    assert db.last_alert_ts("k") is None
    db.record_alert("k", "hello")
    assert db.last_alert_ts("k") is not None
