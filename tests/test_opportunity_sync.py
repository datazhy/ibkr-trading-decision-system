from datetime import datetime, timezone
from unittest.mock import MagicMock

from app.db import Database
from app.ibkr_client import BrokerApiError
from app.opportunity_service import OpportunityService
from app.sync import SyncService


def _snapshot(db):
    db.write_account_snapshot(
        snapshot_id="snap", ts="2026-08-10T21:00:00+00:00",
        balances={"account": "ACC", "equity": "100000", "cash": "90000",
                  "margin_balance": "0", "buying_power": "90000"},
        positions=[{"symbol": "AAPL", "qty": "10", "avg_cost": "100",
                    "market_value": "1000", "upl": "0", "price": "100"}],
    )


def _service(make_config, tmp_path, enabled):
    cfg = make_config(db_path=tmp_path / ("on.db" if enabled else "off.db"),
                      opportunity_enabled=enabled)
    db = Database(cfg.db_path); db.init_schema(); _snapshot(db)
    client = MagicMock()
    sync = SyncService(cfg, db, client, MagicMock())
    return cfg, db, client, sync


def test_universe_only_has_positions_when_feature_off(make_config, tmp_path):
    cfg, db, client, sync = _service(make_config, tmp_path, False)
    OpportunityService(cfg, db).save_watch("NVDA")
    symbols, held = sync.daily_bar_universe()
    assert symbols == ["AAPL"]
    assert held == {"AAPL"}


def test_enabled_universe_adds_active_watchlist_and_benchmarks(make_config, tmp_path):
    cfg, db, client, sync = _service(make_config, tmp_path, True)
    svc = OpportunityService(cfg, db)
    svc.save_watch("NVDA")
    svc.save_watch("PAUSED")
    svc.set_watch_status("PAUSED", "PAUSED")
    symbols, held = sync.daily_bar_universe()
    assert symbols == ["AAPL", "NVDA", "QQQ", "SPY"]
    assert held == {"AAPL"}


def test_new_candidates_backfill_and_existing_candidates_increment(make_config, tmp_path,
                                                                     monkeypatch):
    cfg, db, client, sync = _service(make_config, tmp_path, True)
    svc = OpportunityService(cfg, db)
    svc.save_watch("NEW")
    svc.save_watch("OLD")
    db.upsert_daily_bars("OLD", [{"date": "2026-08-08", "o": "10", "h": "11",
                                  "l": "9", "c": "10", "v": 1000}])
    calls = {}
    def fetch(symbol, start, end):
        calls[symbol] = (start, end)
        return [{"date": "2026-08-10", "o": "10", "h": "11", "l": "9",
                 "c": "10", "v": 1000}]
    client.fetch_daily_bars.side_effect = fetch
    monkeypatch.setattr("app.sync.time.sleep", lambda _: None)
    assert sync.sync_daily_bars(lookback_days=600) is True
    full_days = (calls["AAPL"][1] - calls["AAPL"][0]).days
    old_days = (calls["OLD"][1] - calls["OLD"][0]).days
    assert full_days == 600
    assert old_days < 14
    assert (calls["NEW"][1] - calls["NEW"][0]).days == 600
    assert set(calls) == {"AAPL", "NEW", "OLD", "QQQ", "SPY"}


def test_one_candidate_failure_does_not_stop_other_symbols(make_config, tmp_path,
                                                            monkeypatch):
    cfg, db, client, sync = _service(make_config, tmp_path, True)
    svc = OpportunityService(cfg, db)
    svc.save_watch("BAD")
    svc.save_watch("GOOD")
    def fetch(symbol, start, end):
        if symbol == "BAD":
            raise BrokerApiError("candidate failed")
        return [{"date": "2026-08-10", "o": "10", "h": "11", "l": "9",
                 "c": "10", "v": 1000}]
    client.fetch_daily_bars.side_effect = fetch
    monkeypatch.setattr("app.sync.time.sleep", lambda _: None)
    assert sync.sync_daily_bars() is False
    assert db.latest_bar_date("GOOD") == "2026-08-10"
    rows = {r["symbol"]: r for r in svc.list_watchlist()}
    assert rows["BAD"]["last_sync_status"] == "FAILED"
    assert rows["GOOD"]["last_sync_status"] == "SUCCESS"
