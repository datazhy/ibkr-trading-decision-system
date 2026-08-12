"""富途实时市场代理 + IBKR 缺失回退。"""
from datetime import datetime, timezone
from decimal import Decimal
import json

from app import market_service
from app.db import Database


def _ibkr_row(code, close):
    return {
        "instrument_code": code, "instrument_name": code,
        "data_as_of": "2026-07-30", "fetched_at": "2026-07-31T10:00:00+00:00",
        "open": close, "high": close, "low": close, "close": close,
        "previous_close": close, "daily_change": 0, "daily_change_percent": 0,
        "volume": 1, "source": "IBKR_historical", "delayed": True,
        "quality_status": "fresh", "quality_note": None, "raw_json": None,
    }


def test_live_etfs_use_futu_and_unavailable_indices_keep_ibkr(make_config):
    cfg = make_config()
    db = Database(cfg.db_path); db.init_schema()
    db.upsert_market_snapshots([_ibkr_row("QQQ", 100), _ibkr_row("VIX", 18)])
    db.set_sync_state("market_bars_cache", json.dumps({
        "QQQ": [{"date": "2026-07-30", "c": "100"}],
        "VIX": [{"date": "2026-07-30", "c": "18"}],
    }))

    quote_time = "2026-07-31T14:00:01+00:00"
    result = market_service.sync_live_market_data(cfg, db, [{
        "symbol": "QQQ", "price": Decimal("102"),
        "previous_close": Decimal("100"), "as_of": quote_time,
        "source": "FUTU_OpenD", "price_source": "LAST",
    }], now=datetime(2026, 7, 31, 14, 0, 2, tzinfo=timezone.utc))
    assert result == {"changed": True, "count": 1, "fallback": ["SOXX", "SPY"]}
    latest = db.latest_market_snapshots()
    assert latest["QQQ"]["source"] == "FUTU_OpenD"
    assert latest["QQQ"]["delayed"] == 0
    assert latest["QQQ"]["quote_as_of"] == quote_time
    assert latest["VIX"]["source"] == "IBKR_historical"  # 富途拿不到即保留 IBKR

    metrics, state = market_service.compute_metrics(cfg, db)
    assert metrics["qqq"]["close"] == Decimal("102")
    assert metrics["data"]["realtime_count"] == 1
    assert metrics["data"]["live_as_of"] == quote_time

    fallback = market_service.sync_live_market_data(cfg, db, [])
    assert fallback["fallback"] == ["QQQ", "SOXX", "SPY"]
    assert db.latest_market_snapshots()["QQQ"]["source"] == "IBKR_historical"


def test_vix_vxn_us10y_use_ibkr_delayed_intraday_quotes(make_config):
    cfg = make_config()
    db = Database(cfg.db_path); db.init_schema()
    db.upsert_market_snapshots([
        _ibkr_row("VIX", 18), _ibkr_row("VXN", 22), _ibkr_row("US10Y", 46.6),
    ])
    db.set_sync_state("market_bars_cache", json.dumps({
        "VIX": [{"date": "2026-07-30", "c": "18"}],
        "VXN": [{"date": "2026-07-30", "c": "22"}],
        "US10Y": [{"date": "2026-07-30", "c": "46.6"}],
    }))
    quote_time = "2026-07-31T14:00:01+00:00"
    result = market_service.sync_live_market_data(cfg, db, [{
        "symbol": "VIX", "price": Decimal("19.25"),
        "previous_close": Decimal("18"), "as_of": quote_time,
        "source": "IBKR_reqMktData", "price_source": "LAST",
        "quality": "DELAYED", "market_data_type": 3,
        "requested_market_data_type": 3, "effective_market_data_type": 3,
    }, {
        "symbol": "VXN", "price": Decimal("23.50"),
        "previous_close": Decimal("22"), "as_of": quote_time,
        "source": "IBKR_reqMktData", "price_source": "BID_ASK_MID",
        "quality": "DELAYED", "market_data_type": 3,
    }, {
        "symbol": "US10Y", "price": Decimal("46.99"),
        "previous_close": Decimal("46.60"), "as_of": quote_time,
        "source": "IBKR_reqMktData", "price_source": "LAST",
        "quality": "DELAYED", "market_data_type": 3,
    }], now=datetime(2026, 7, 31, 14, 0, 2, tzinfo=timezone.utc))

    assert result["count"] == 3
    latest = db.latest_market_snapshots()
    assert latest["VIX"]["source"] == "IBKR_reqMktData"
    assert latest["VIX"]["delayed"] == 1
    assert latest["VIX"]["quote_as_of"] == quote_time
    assert latest["US10Y"]["source"] == "IBKR_reqMktData"
    assert latest["US10Y"]["close"] == "46.99"
    metrics, _ = market_service.compute_metrics(cfg, db)
    assert metrics["vol"]["vix_current"] == Decimal("19.25")
    assert metrics["vol"]["vxn_current"] == Decimal("23.50")
    assert metrics["rates"]["current"] == Decimal("46.99")
    assert metrics["data"]["realtime_count"] == 0


def test_ibkr_close_only_quote_does_not_replace_historical_close(make_config):
    cfg = make_config()
    db = Database(cfg.db_path); db.init_schema()
    db.upsert_market_snapshots([_ibkr_row("VIX", 18)])
    market_service.sync_live_market_data(cfg, db, [{
        "symbol": "VIX", "price": Decimal("18"),
        "previous_close": Decimal("17"), "as_of": "2026-07-31T14:00:01+00:00",
        "source": "IBKR_reqMktData", "price_source": "CLOSE",
        "quality": "DELAYED_FROZEN", "market_data_type": 3,
    }])
    latest = db.latest_market_snapshots()["VIX"]
    assert latest["source"] == "IBKR_historical"
    assert latest["quote_as_of"] is None


def test_force_live_market_assessment_overwrites_same_day(make_config):
    cfg = make_config()
    db = Database(cfg.db_path); db.init_schema()
    rows = [_ibkr_row("QQQ", 100), _ibkr_row("SOXX", 200), _ibkr_row("VIX", 18)]
    db.upsert_market_snapshots(rows)
    db.set_sync_state("market_bars_cache", json.dumps({
        code: [{"date": f"2026-07-{day:02d}", "c": str(base + day)}
               for day in range(1, 31)]
        for code, base in (("QQQ", 100), ("SOXX", 200), ("VIX", 10))
    }))
    first = market_service.assess(cfg, db, force=True)
    second = market_service.assess(cfg, db, force=True)
    assert second["assessment_id"] == first["assessment_id"]
    with db.connect() as conn:
        count = conn.execute("SELECT COUNT(*) FROM market_environment_assessments").fetchone()[0]
    assert count == 1
