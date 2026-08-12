"""数据健康闸门：首轮/未就绪不写零快照。"""
from decimal import Decimal

from app.db import Database
from app.notify import Notifier
from app.sync import SyncService


class FakeClient:
    def __init__(self, positions, balances):
        self._p, self._b = positions, balances
    def resolve_account_hash(self, account_number):
        return "ACC"
    def fetch_positions_and_balances(self, h):
        return self._p, self._b


def _sync(make_config, positions, balances):
    cfg = make_config()
    db = Database(cfg.db_path)
    db.init_schema()
    svc = SyncService(cfg, db, FakeClient(positions, balances), Notifier([], db))
    return cfg, db, svc


def test_not_ready_does_not_write_zero_snapshot(make_config):
    balances = {"equity": Decimal("0"), "cash": Decimal("0"),
                "margin_balance": Decimal("0"), "buying_power": Decimal("0"), "_ready": False}
    cfg, db, svc = _sync(make_config, [], balances)
    ok = svc.sync_positions_and_balances()
    assert ok is False
    assert db.latest_positions() == []
    assert db.latest_balances() is None                 # 未写零快照
    assert db.get_sync_state("data_health") == "WARMING_UP"


def test_zero_equity_without_ready_flag_also_skipped(make_config):
    # 无 _ready 标记时按 equity>0 判断
    balances = {"equity": Decimal("0"), "cash": Decimal("0"),
                "margin_balance": Decimal("0"), "buying_power": Decimal("0")}
    cfg, db, svc = _sync(make_config, [], balances)
    assert svc.sync_positions_and_balances() is False
    assert db.latest_balances() is None


def test_ready_writes_snapshot_with_quality(make_config):
    positions = [{"symbol": "MU", "qty": Decimal("100"), "avg_cost": Decimal("95"),
                  "market_value": Decimal("9500"), "upl": Decimal("0"),
                  "price": Decimal("95"), "price_quality": "REALTIME",
                  "quote_as_of": "2026-07-10T18:00:00+00:00"}]
    balances = {"equity": Decimal("100000"), "cash": Decimal("50000"),
                "margin_balance": Decimal("50000"), "buying_power": Decimal("80000"), "_ready": True}
    cfg, db, svc = _sync(make_config, positions, balances)
    assert svc.sync_positions_and_balances() is True
    rows = db.latest_positions()
    assert len(rows) == 1
    assert rows[0]["price_quality"] == "REALTIME"
    assert db.get_sync_state("data_health") == "READY"
