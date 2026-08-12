"""Hyperliquid 高频数据的独立 SQLite 访问层。

该模块刻意不复用主库 Database：采集器是 hyperliquid.db 的唯一写者，
Web/结果计算只能用只读 URI 打开，避免高频写入污染主库事务。
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

SCHEMA_PATH = Path(__file__).with_name("hyperliquid_schema.sql")


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class HyperliquidDatabase:
    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)

    @contextmanager
    def connect(self, *, read_only: bool = False) -> Iterator[sqlite3.Connection]:
        if read_only:
            if not self.db_path.exists():
                raise FileNotFoundError(self.db_path)
            uri = f"file:{self.db_path.resolve()}?mode=ro"
            conn = sqlite3.connect(uri, uri=True, timeout=30)
        else:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("PRAGMA foreign_keys=ON")
        if not read_only:
            conn.execute("PRAGMA journal_mode=WAL")
        if read_only:
            conn.execute("PRAGMA query_only=ON")
        try:
            yield conn
            if not read_only:
                conn.commit()
        except Exception:
            if not read_only:
                conn.rollback()
            raise
        finally:
            conn.close()

    def init_schema(self) -> None:
        with self.connect() as conn:
            conn.execute("PRAGMA auto_vacuum=INCREMENTAL")
            conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
            additions = {
                "hl_recent_trades": (("data_origin", "TEXT NOT NULL DEFAULT 'LIVE'"),),
                "hl_trade_buckets_5m": (("data_origin", "TEXT NOT NULL DEFAULT 'LIVE'"),),
                "hl_address_flow_buckets_5m": (("data_origin", "TEXT NOT NULL DEFAULT 'LIVE'"),),
                "hl_asset_context_buckets_5m": (
                    ("session", "TEXT NOT NULL DEFAULT 'UNKNOWN'"),
                    ("data_origin", "TEXT NOT NULL DEFAULT 'LIVE'"),
                ),
                "hl_wallet_fills": (("data_origin", "TEXT NOT NULL DEFAULT 'LIVE'"),),
                "hl_wallet_funding": (("data_origin", "TEXT NOT NULL DEFAULT 'LIVE'"),),
                "hl_wallet_ledger_updates": (("data_origin", "TEXT NOT NULL DEFAULT 'LIVE'"),),
                "hl_wallet_equity_snapshots": (("data_origin", "TEXT NOT NULL DEFAULT 'LIVE'"),),
                "hl_wallet_positions": (("data_origin", "TEXT NOT NULL DEFAULT 'LIVE'"),),
                "hl_wallet_cycles": (("data_origin", "TEXT NOT NULL DEFAULT 'LIVE'"),),
                "hl_wallet_daily_metrics": (("data_origin", "TEXT NOT NULL DEFAULT 'LIVE'"),),
                "hl_wallet_classification_intervals": (("data_origin", "TEXT NOT NULL DEFAULT 'LIVE'"),),
                "hl_wallet_memberships": (("data_origin", "TEXT NOT NULL DEFAULT 'LIVE'"),),
                "hl_wallet_group_versions": (("data_origin", "TEXT NOT NULL DEFAULT 'LIVE'"),),
                "hl_wallet_scores": (("data_origin", "TEXT NOT NULL DEFAULT 'LIVE'"),),
                "hl_signal_features_5m": (("data_origin", "TEXT NOT NULL DEFAULT 'LIVE'"),),
                "hl_signal_features_daily": (("data_origin", "TEXT NOT NULL DEFAULT 'LIVE'"),),
                "hl_signal_episodes": (
                    ("data_origin", "TEXT NOT NULL DEFAULT 'LIVE'"),
                    ("validation_commitment_id", "TEXT"),
                ),
            }
            for table, columns in additions.items():
                existing = {row["name"] for row in conn.execute(
                    f"PRAGMA table_info({table})")}
                for name, ddl in columns:
                    if existing and name not in existing:
                        conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")

    def available(self) -> bool:
        if not self.db_path.exists():
            return False
        try:
            with self.connect(read_only=True) as conn:
                row = conn.execute("PRAGMA integrity_check").fetchone()
                return bool(row and row[0] == "ok")
        except (OSError, sqlite3.DatabaseError):
            return False

    @staticmethod
    def decode_json(value: Any, default: Any = None) -> Any:
        if value in (None, ""):
            return default
        try:
            return json.loads(value)
        except (TypeError, ValueError):
            return default

    def health(self) -> dict[str, Any]:
        if not self.db_path.exists():
            return {"available": False, "status": "NOT_STARTED", "path": str(self.db_path)}
        try:
            with self.connect(read_only=True) as conn:
                run = conn.execute(
                    "SELECT * FROM hl_collection_runs ORDER BY started_at DESC LIMIT 1"
                ).fetchone()
                signal = conn.execute(
                    "SELECT MAX(last_seen_at) FROM hl_signal_episodes"
                ).fetchone()[0]
            return {"available": True, "status": "READY", "path": str(self.db_path),
                    "latest_run": dict(run) if run else None, "latest_signal_at": signal}
        except (OSError, sqlite3.DatabaseError) as exc:
            return {"available": False, "status": "ERROR", "path": str(self.db_path),
                    "error": type(exc).__name__}
