import io
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.db import Database
from app.hyperliquid_backfill import HistoricalBackfillExecutor
from app.hyperliquid_client import HyperliquidApiError, HyperliquidClient
from app.hyperliquid_db import HyperliquidDatabase
from app.hyperliquid_results import (VALIDATION_POLICY_V2,
                                     create_validation_commitment,
                                     fast_track_verdict,
                                     record_validation_run,
                                     save_equity_bars)
from app.hyperliquid_service import HyperliquidService


def _cfg(tmp_path):
    return type("Cfg", (), {
        "db_path": Path(tmp_path) / "main.db",
        "hl_db_path": Path(tmp_path) / "hl.db",
        "hl_dex": "xyz",
        "hl_max_tracked_wallets": 50,
        "hl_wallet_weight_cap": "0.20",
        "hl_min_directional_wallets": 5,
    })()


def test_data_origin_is_persisted_across_raw_and_outcome_tables(tmp_path):
    cfg = _cfg(tmp_path)
    main = Database(cfg.db_path)
    main.init_schema()
    hl = HyperliquidDatabase(cfg.hl_db_path)
    hl.init_schema()
    svc = HyperliquidService(cfg, hl)
    address = "0x" + "1" * 40
    svc.ingest_wallet_fills(address, [{
        "coin": "xyz:NVDA", "time": "1000", "tid": "1", "px": "100", "sz": "1",
        "dir": "Open Long", "startPosition": "0", "closedPnl": "0", "fee": "0",
    }], data_origin="HISTORICAL_BACKFILL")
    save_equity_bars(main, [{"symbol": "NVDA", "bar_start":
                             "2026-01-01T14:30:00+00:00", "o": "100", "c": "100"}],
                     data_origin="HISTORICAL_BACKFILL")
    with hl.connect(read_only=True) as conn:
        assert conn.execute("SELECT data_origin FROM hl_wallet_fills").fetchone()[0] == "HISTORICAL_BACKFILL"
    with main.connect() as conn:
        assert conn.execute("SELECT data_origin FROM equity_bars_5m").fetchone()[0] == "HISTORICAL_BACKFILL"


def test_locked_validation_requires_immutable_commitment_and_matching_window(tmp_path):
    db = Database(Path(tmp_path) / "main.db")
    db.init_schema()
    commitment = create_validation_commitment(
        db, model="INTRADAY", rules_version="r1", rules_hash="h1",
        policy_version="p1", policy_hash="ph1",
        locked_test_start="2026-01-01T00:00:00+00:00",
        locked_test_end="2026-02-01T00:00:00+00:00",
        feature_manifest={"features": ["wallet_breadth"]},
        gate={"raw_n": 500, "effective_n": 200, "directional_n": 50},
        committed_at="2025-12-31T00:00:00+00:00",
    )
    row = {"status": "COMPLETE", "model": "INTRADAY", "horizon": "2h",
           "direction": "BULLISH", "canonical_symbol": "NVDA",
           "entry_time": "2026-01-15T14:30:00+00:00", "return_pct": "0.2",
           "data_origin": "HISTORICAL_LOCKED_TEST"}
    result = record_validation_run(
        db, model="INTRADAY", rules_version="r1", rules_hash="h1",
        policy_version="p1", policy_hash="ph1", rows=[row], verdict="FAIL",
        validation_policy_version=VALIDATION_POLICY_V2,
        sample_origin="HISTORICAL_LOCKED_TEST",
        commitment_id=commitment["commitment_id"],
    )
    assert result["run_id"]
    with pytest.raises(ValueError, match="超出已承诺区间"):
        record_validation_run(
            db, model="INTRADAY", rules_version="r1", rules_hash="h1",
            policy_version="p1", policy_hash="ph1",
            rows=[{**row, "entry_time": "2026-03-01T00:00:00+00:00"}],
            validation_policy_version=VALIDATION_POLICY_V2,
            sample_origin="HISTORICAL_LOCKED_TEST",
            commitment_id=commitment["commitment_id"],
        )
    with pytest.raises(ValueError, match="data_origin"):
        record_validation_run(
            db, model="INTRADAY", rules_version="r1", rules_hash="h1",
            policy_version="p1", policy_hash="ph1",
            rows=[{**row, "data_origin": "HISTORICAL_BACKFILL"}],
            validation_policy_version=VALIDATION_POLICY_V2,
            sample_origin="HISTORICAL_LOCKED_TEST",
            commitment_id=commitment["commitment_id"],
        )


def test_pit_wallet_score_does_not_read_future_closed_cycle(tmp_path):
    cfg = _cfg(tmp_path)
    hl = HyperliquidDatabase(cfg.hl_db_path)
    hl.init_schema()
    svc = HyperliquidService(cfg, hl)
    address = "0x" + "2" * 40
    with hl.connect() as conn:
        conn.execute(
            "INSERT INTO hl_wallet_candidates(address,first_seen_at,last_seen_at,status) VALUES(?,?,?,?)",
            (address, "2026-01-01", "2026-03-01", "CANDIDATE"))
        conn.execute(
            "INSERT INTO hl_wallet_cycles(cycle_id,address,coin,opened_at,closed_at,side,status,"
            "left_censored,gross_closed_pnl,fees,funding_pnl,net_pnl,closed_notional,raw_refs_json,data_origin) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("future", address, "xyz:NVDA", "2026-02-01T00:00:00+00:00",
             "2026-03-01T00:00:00+00:00", "LONG", "COMPLETE", 0, "10", "0", "0",
             "10", "100", "{}", "HISTORICAL_BACKFILL"))
    result = svc.score_wallet(address, as_of="2026-02-15T00:00:00+00:00",
                              data_origin="HISTORICAL_BACKFILL")
    assert result["metrics"]["completed_cycles"] == 0


class _Client:
    def user_fills_by_time(self, address, start, end=None, backfill=False):
        return [{"coin": "xyz:NVDA", "time": "1760000000000", "tid": "t1",
                 "px": "100", "sz": "1", "dir": "Open Long",
                 "startPosition": "0", "closedPnl": "0", "fee": "0"}]


def test_backfill_executor_auto_tracks_and_resumes_wallet(tmp_path):
    cfg = _cfg(tmp_path)
    main = Database(cfg.db_path)
    main.init_schema()
    hl = HyperliquidDatabase(cfg.hl_db_path)
    hl.init_schema()
    address = "0x" + "3" * 40
    with hl.connect() as conn:
        conn.execute(
            "INSERT INTO hl_wallet_candidates(address,first_seen_at,last_seen_at,status,discovered_from) "
            "VALUES(?,?,?,?,?)", (address, "2026-01-01", "2026-08-01", "CANDIDATE", "TEST"))
    svc = HyperliquidService(cfg, hl, _Client())
    executor = HistoricalBackfillExecutor(cfg, svc, main)
    result = executor.run_step(days=180, scope="wallets")
    assert result["step"] == "fills"
    with main.connect() as conn:
        assert conn.execute("SELECT mode FROM hl_wallet_controls WHERE address=?",
                            (address,)).fetchone()[0] == "AUTO"
    with hl.connect(read_only=True) as conn:
        row = conn.execute("SELECT data_origin FROM hl_wallet_fills WHERE address=?",
                           (address,)).fetchone()
        assert row[0] == "HISTORICAL_BACKFILL"


def test_fast_track_never_promotes_before_required_days():
    gate = {"verdict": "PASS"}
    assert fast_track_verdict(historical_gate=gate, prospective_days=19,
                              required_days=20)["verdict"] == "SHADOW"
    assert fast_track_verdict(historical_gate=gate, prospective_days=20,
                              required_days=20)["verdict"] == "ELIGIBLE"


def test_fill_pagination_keeps_completed_page_when_backfill_budget_is_exhausted():
    class Client(HyperliquidClient):
        calls = 0

        def user_fills_by_time(self, address, start_time, end_time=None, *, backfill=False):
            self.calls += 1
            if self.calls == 1:
                return [{"coin": "xyz:NVDA", "time": 1000 + i, "tid": str(i)}
                        for i in range(2000)]
            raise HyperliquidApiError("budget", code="RATE_LIMIT_BUDGET")

    rows, incomplete = Client().user_fills_window("0x" + "5" * 40, 0, 999999)
    assert len(rows) == 2000
    assert incomplete is True


def test_market_funding_backfill_pages_inclusively_and_keeps_premium_semantics(tmp_path):
    cfg = _cfg(tmp_path)
    main = Database(cfg.db_path); main.init_schema()
    hl = HyperliquidDatabase(cfg.hl_db_path); hl.init_schema()

    class Client:
        calls = []

        def funding_history(self, coin, start_time, end_time=None, *, backfill=False):
            self.calls.append((coin, start_time, end_time, backfill))
            if len(self.calls) == 1:
                return [{"coin": coin, "time": start_time + i * 3600000,
                         "fundingRate": "0.0001", "premium": "0.0002"}
                        for i in range(500)]
            # inclusive边界会重读第一条；主键必须去重。
            return [{"coin": coin, "time": start_time,
                     "fundingRate": "0.0001", "premium": "0.0002"},
                    {"coin": coin, "time": start_time + 3600000,
                     "fundingRate": "0.0003", "premium": "0.0004"}]

    client = Client()
    service = HyperliquidService(cfg, hl, client)
    executor = HistoricalBackfillExecutor(cfg, service, main)
    with hl.connect() as conn:
        conn.execute(
            "INSERT INTO hl_assets(coin,dex,symbol,category,metadata_json,data_as_of,fetched_at) "
            "VALUES(?,?,?,?,?,?,?)",
            ("xyz:NVDA", "xyz", "NVDA", "stocks", "{}",
             "2026-08-12T00:00:00+00:00", "2026-08-12T00:00:00+00:00"),
        )
        for run_id in ("run1", "run2"):
            conn.execute(
                "INSERT INTO hl_backfill_runs(run_id,started_at,status,requested_start,"
                "requested_end,data_origin,scope_json) VALUES(?,?,?,?,?,?,?)",
                (run_id, "2026-08-12T00:00:00+00:00", "RUNNING", "2026-01-01",
                 "2026-08-12", "HISTORICAL_BACKFILL", '{"scope":"market_funding"}'),
            )
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    end = datetime(2026, 8, 12, tzinfo=timezone.utc)

    first = executor.backfill_one_market_funding(run_id="run1", start=start, end=end)
    second = executor.backfill_one_market_funding(run_id="run2", start=start, end=end)

    assert first["received"] == 500 and first["complete"] is False
    assert second["received"] == 2 and second["complete"] is True
    assert client.calls[1][1] == client.calls[0][1] + 499 * 3600000
    with hl.connect(read_only=True) as conn:
        count = conn.execute("SELECT COUNT(*) FROM hl_market_funding_history").fetchone()[0]
        row = conn.execute(
            "SELECT funding_rate,funding_premium_index,data_origin,event_time "
            "FROM hl_market_funding_history ORDER BY event_time_ms DESC LIMIT 1"
        ).fetchone()
    assert count == 501
    assert tuple(row)[:3] == ("0.0003", "0.0004", "HISTORICAL_BACKFILL")
    assert str(row[3]).endswith("+00:00")


def test_funding_history_client_declares_500_item_weight_and_schema():
    class Client(HyperliquidClient):
        seen = None

        def info(self, request_type, payload=None, **kwargs):
            self.seen = (request_type, payload, kwargs)
            return [{"coin": "xyz:NVDA", "time": 1, "fundingRate": "0", "premium": "0"}]

    client = Client()
    rows = client.funding_history("xyz:NVDA", 1, 2, backfill=True)
    assert len(rows) == 1
    assert client.seen == (
        "fundingHistory",
        {"coin": "xyz:NVDA", "startTime": 1, "endTime": 2},
        {"expected_items": 500, "backfill": True},
    )


def test_archive_asset_context_import_is_labeled_and_does_not_use_candle(tmp_path):
    cfg = _cfg(tmp_path)
    main = Database(cfg.db_path)
    main.init_schema()
    hl = HyperliquidDatabase(cfg.hl_db_path)
    hl.init_schema()
    executor = HistoricalBackfillExecutor(cfg, HyperliquidService(cfg, hl), main)
    archive = Path(tmp_path) / "ctx.csv"
    archive.write_text(
        'time,coin,markPx,oraclePx,openInterest,dayNtlVlm\n'
        '1760000000000,xyz:NVDA,101,100,2000,500000\n', encoding="utf-8")
    result = executor.import_asset_context_archive(archive)
    assert result["status"] == "SUCCESS"
    assert result["rows"] == 1
    assert result["seen"] == 1
    assert result["skipped"] == 0
    assert result["invalid"] == 0
    assert result["buckets"] == 1
    assert len(result["sha256"]) == 64
    with hl.connect(read_only=True) as conn:
        row = conn.execute(
            "SELECT data_origin,mark_px,oracle_px FROM hl_asset_context_buckets_5m").fetchone()
    assert tuple(row) == ("HISTORICAL_BACKFILL", "101", "100")


def test_asset_ctx_download_is_requester_pays_and_atomic(monkeypatch, tmp_path):
    calls = []

    class Client:
        def get_object(self, **kwargs):
            calls.append(kwargs)
            return {"Body": io.BytesIO(b"archive"), "ContentLength": 7}

    monkeypatch.setitem(sys.modules, "boto3", SimpleNamespace(client=lambda name: Client()))

    target = HistoricalBackfillExecutor.download_asset_context_day(
        date(2026, 8, 1), tmp_path)

    assert target.read_bytes() == b"archive"
    assert not target.with_suffix(target.suffix + ".part").exists()
    assert calls == [{
        "Bucket": "hyperliquid-archive",
        "Key": "asset_ctxs/20260801.csv.lz4",
        "RequestPayer": "requester",
    }]


def test_archive_ctx_never_guesses_unprefixed_main_dex_as_xyz(tmp_path):
    cfg = _cfg(tmp_path)
    main = Database(cfg.db_path)
    main.init_schema()
    hl = HyperliquidDatabase(cfg.hl_db_path)
    hl.init_schema()
    executor = HistoricalBackfillExecutor(cfg, HyperliquidService(cfg, hl), main)
    archive = Path(tmp_path) / "ctx.csv"
    archive.write_text(
        "time,coin,dex,mark_px,oracle_px,impact_bid_px,impact_ask_px\n"
        "1760000000000,BTC,,101,100,99,102\n"
        "1760000000000,NVDA,xyz,201,200,199,202\n"
        "1760000060000,xyz:TSLA,,301,300,299,302\n",
        encoding="utf-8",
    )

    result = executor.import_asset_context_archive(archive)

    assert result["status"] == "SUCCESS"
    assert result["seen"] == 3
    assert result["skipped"] == 1
    assert result["rows"] == 2
    with hl.connect(read_only=True) as conn:
        rows = conn.execute(
            "SELECT coin,impact_bid_px,impact_ask_px FROM hl_asset_context_buckets_5m "
            "ORDER BY coin"
        ).fetchall()
    assert [tuple(row) for row in rows] == [
        ("xyz:NVDA", "199", "202"),
        ("xyz:TSLA", "299", "302"),
    ]


def test_archive_ctx_compacts_to_last_observation_per_five_minutes(tmp_path):
    cfg = _cfg(tmp_path)
    main = Database(cfg.db_path)
    main.init_schema()
    hl = HyperliquidDatabase(cfg.hl_db_path)
    hl.init_schema()
    executor = HistoricalBackfillExecutor(cfg, HyperliquidService(cfg, hl), main)
    archive = Path(tmp_path) / "ctx.csv"
    archive.write_text(
        "time,coin,mark_px,oracle_px\n"
        "2026-08-12T10:01:00Z,xyz:NVDA,101,100\n"
        "2026-08-12T10:04:59Z,xyz:NVDA,103,102\n"
        "2026-08-12T10:05:00Z,xyz:NVDA,104,103\n",
        encoding="utf-8",
    )

    result = executor.import_asset_context_archive(archive)

    assert result["rows"] == 2
    assert result["buckets"] == 2
    with hl.connect(read_only=True) as conn:
        rows = conn.execute(
            "SELECT bucket_start,mark_px FROM hl_asset_context_buckets_5m ORDER BY bucket_start"
        ).fetchall()
    assert [tuple(row) for row in rows] == [
        ("2026-08-12T10:00:00+00:00", "103"),
        ("2026-08-12T10:05:00+00:00", "104"),
    ]


def test_node_fills_archive_requires_explicit_address(tmp_path):
    cfg = _cfg(tmp_path)
    main = Database(cfg.db_path)
    main.init_schema()
    hl = HyperliquidDatabase(cfg.hl_db_path)
    hl.init_schema()
    executor = HistoricalBackfillExecutor(cfg, HyperliquidService(cfg, hl), main)
    address = "0x" + "4" * 40
    archive = Path(tmp_path) / "fills.jsonl"
    archive.write_text(
        '{"user":"' + address + '","fill":{"coin":"xyz:NVDA","time":1760000000000,'
        '"tid":"t1","px":"100","sz":"1","dir":"Open Long",'
        '"startPosition":"0","closedPnl":"0","fee":"0"}}\n'
        '{"fill":{"coin":"xyz:NVDA","time":1760000001000,"tid":"bad"}}\n',
        encoding="utf-8")
    result = executor.import_node_fills_archive(archive)
    assert result["rows"] == 1
    assert result["invalid"] == 1
    assert result["status"] == "INCOMPLETE"


def test_equity_backfill_advances_one_week_per_step(tmp_path):
    cfg = _cfg(tmp_path)
    main = Database(cfg.db_path)
    main.init_schema()
    hl = HyperliquidDatabase(cfg.hl_db_path)
    hl.init_schema()
    with main.connect() as conn:
        conn.execute(
            "INSERT INTO hl_asset_mappings(coin,canonical_symbol,conid,status,checked_at,source) "
            "VALUES(?,?,?,?,?,?)",
            ("xyz:NVDA", "NVDA", 1, "READY", "2026-08-01", "TEST"))

    class Broker:
        def __init__(self):
            self.calls = []

        def fetch_5m_bars(self, symbol, start, end, use_rth=True):
            self.calls.append((symbol, start, end))
            return [{"symbol": symbol, "bar_start": start.isoformat(), "o": "1", "c": "1"}]

    broker = Broker()
    executor = HistoricalBackfillExecutor(cfg, HyperliquidService(cfg, hl), main)
    result = executor.run_equity_step(broker, days=180)
    assert result["symbol"] == "SPY"
    assert result["complete"] is False
    assert (broker.calls[0][2] - broker.calls[0][1]).days <= 7


def test_wallet_backfill_persists_page_cursor_between_runs(tmp_path):
    cfg = _cfg(tmp_path)
    main = Database(cfg.db_path)
    main.init_schema()
    hl = HyperliquidDatabase(cfg.hl_db_path)
    hl.init_schema()
    address = "0x" + "6" * 40
    with hl.connect() as conn:
        conn.execute(
            "INSERT INTO hl_wallet_candidates(address,first_seen_at,last_seen_at,status) "
            "VALUES(?,?,?,?)", (address, "2026-01-01", "2026-08-01", "CANDIDATE"))

    class Client:
        starts = []

        def user_fills_by_time(self, address, start, end=None, backfill=False):
            self.starts.append(start)
            if len(self.starts) == 1:
                return [{"coin": "xyz:NVDA", "time": start + i + 1, "tid": str(i),
                         "px": "100", "sz": "1", "dir": "Open Long",
                         "startPosition": "0", "closedPnl": "0", "fee": "0"}
                        for i in range(2000)]
            return []

    client = Client()
    executor = HistoricalBackfillExecutor(cfg, HyperliquidService(cfg, hl, client), main)
    first = executor.run_step(days=180, scope="wallets")
    second = executor.run_step(days=180, scope="wallets")
    assert first["complete"] is False
    assert second["complete"] is True
    assert client.starts[1] == first["cursor"]
