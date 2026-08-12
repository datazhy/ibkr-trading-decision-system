from __future__ import annotations

import os
import sys
from datetime import datetime, timezone
from types import SimpleNamespace

from app.db import Database
from app.hyperliquid_db import HyperliquidDatabase
from app.hyperliquid_quality import (
    _aws_credential_state,
    build_quality_snapshot,
    write_quality_report,
)


NOW = datetime(2026, 8, 12, 12, 0, tzinfo=timezone.utc)


def test_aws_probe_skips_metadata_without_explicit_provider(monkeypatch, tmp_path):
    for name in list(os.environ):
        if name.startswith("AWS_"):
            monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr("app.hyperliquid_quality.Path.home", lambda: tmp_path)
    monkeypatch.setitem(
        sys.modules, "boto3",
        SimpleNamespace(Session=lambda: (_ for _ in ()).throw(AssertionError("unexpected probe"))),
    )

    state = _aws_credential_state()

    assert state["available"] is False
    assert state["method"] is None


def test_aws_probe_accepts_compose_shared_credentials_mount(monkeypatch, tmp_path):
    credentials = tmp_path / "credentials"
    config = tmp_path / "config"
    credentials.write_text("[default]\naws_access_key_id=test\naws_secret_access_key=test\n")
    config.write_text("[default]\nregion=us-east-1\n")
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", str(credentials))
    monkeypatch.setenv("AWS_CONFIG_FILE", str(config))
    monkeypatch.setitem(
        sys.modules, "boto3",
        SimpleNamespace(Session=lambda: SimpleNamespace(
            get_credentials=lambda: SimpleNamespace(method="shared-credentials-file")
        )),
    )

    state = _aws_credential_state()

    assert state["available"] is True
    assert state["method"] == "SHARED-CREDENTIALS-FILE"
    assert state["shared_config_files"] == sorted([str(config), str(credentials)])


def _setup(tmp_path):
    cfg = SimpleNamespace(
        db_path=tmp_path / "dashboard.db",
        hl_db_path=tmp_path / "hyperliquid.db",
        hl_history_backfill_days=180,
    )
    main = Database(cfg.db_path)
    main.init_schema()
    hl = HyperliquidDatabase(cfg.hl_db_path)
    hl.init_schema()
    with hl.connect() as conn:
        conn.execute(
            "INSERT INTO hl_collection_runs(run_id,started_at,finished_at,status) VALUES(?,?,?,?)",
            ("live", NOW.isoformat(), NOW.isoformat(), "SUCCESS"))
        conn.execute(
            "INSERT INTO hl_backfill_runs(run_id,started_at,finished_at,status,requested_start,"
            "requested_end,data_origin,scope_json) VALUES(?,?,?,?,?,?,?,?)",
            ("bf", NOW.isoformat(), NOW.isoformat(), "SUCCESS", "2026-02-13",
             NOW.isoformat(), "HISTORICAL_BACKFILL", '{"scope":"candles"}'))
        conn.execute(
            "INSERT INTO hl_assets(coin,dex,symbol,category,metadata_json,data_as_of,fetched_at) "
            "VALUES(?,?,?,?,?,?,?)",
            ("xyz:NVDA", "xyz", "NVDA", "stocks", "{}", NOW.isoformat(), NOW.isoformat()))
    with main.connect() as conn:
        conn.execute(
            "INSERT INTO hl_asset_mappings(coin,canonical_symbol,conid,status,checked_at,source) "
            "VALUES(?,?,?,?,?,?)", ("xyz:NVDA", "NVDA", 1, "SUSPECT", NOW.isoformat(), "TEST"))
    return cfg, main, hl


def test_quality_snapshot_blocks_without_ctx_and_reports_origins(tmp_path):
    cfg, main, hl = _setup(tmp_path)
    report = build_quality_snapshot(
        cfg, hl, main, now=NOW,
        aws_state={"available": False, "method": None, "aws_env_names": [],
                   "shared_config_files": []})
    assert report["summary"]["status"] == "BLOCKED"
    assert "historical_asset_ctx" in report["summary"]["blocking_check_ids"]
    checks = {row["id"]: row for row in report["checks"]}
    assert checks["origin_labels"]["status"] == "PASS"
    assert checks["asset_ctx_requester_identity"]["status"] == "BLOCKED"


def test_quality_report_persists_snapshot_and_files(tmp_path):
    cfg, _main, hl = _setup(tmp_path)
    result = write_quality_report(
        cfg, output_dir=tmp_path / "reports", now=NOW,
        aws_state={"available": False, "method": None, "aws_env_names": [],
                   "shared_config_files": []})
    assert result["status"] == "BLOCKED"
    assert (tmp_path / "reports" / "latest.json").exists()
    assert (tmp_path / "reports" / "latest.md").exists()
    with hl.connect(read_only=True) as conn:
        row = conn.execute("SELECT status,report_version FROM hl_data_quality_reports").fetchone()
    assert tuple(row) == ("BLOCKED", "hl-data-quality-v1")
