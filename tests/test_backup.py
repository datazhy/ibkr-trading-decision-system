from __future__ import annotations

import gzip
import shutil
import sqlite3
from pathlib import Path

import pytest

from scripts import backup


def _make_db(path: Path, value: str = "ok") -> None:
    with sqlite3.connect(path) as conn:
        conn.execute("create table sample (value text)")
        conn.execute("insert into sample values (?)", (value,))


def _read_compressed_db(path: Path, tmp_path: Path) -> str:
    extracted = tmp_path / "extracted.db"
    with gzip.open(path, "rb") as src, extracted.open("wb") as dst:
        shutil.copyfileobj(src, dst)
    with sqlite3.connect(extracted) as conn:
        return conn.execute("select value from sample").fetchone()[0]


def test_backup_is_atomic_compressed_and_prunes_before_copy(tmp_path, monkeypatch):
    source = tmp_path / "source.db"
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    _make_db(source)
    for stamp in ("20260101-000000", "20260102-000000"):
        _make_db(backup_dir / f"dashboard-{stamp}.db", stamp)

    monkeypatch.setattr(backup, "DB_PATH", source)
    monkeypatch.setattr(backup, "BACKUP_DIR", backup_dir)
    monkeypatch.setattr(backup, "KEEP", 2)

    backup.backup_once()

    completed = backup._backup_files()
    assert len(completed) == 2
    assert completed[-1].suffixes == [".db", ".gz"]
    assert not list(backup_dir.glob("*.tmp"))
    assert _read_compressed_db(completed[-1], tmp_path) == "ok"


def test_failed_backup_removes_partial_file_and_keeps_last_restore_point(tmp_path, monkeypatch):
    source = tmp_path / "source.db"
    source.write_text("not a sqlite database")
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    previous = backup_dir / "dashboard-20260101-000000.db"
    _make_db(previous, "previous")

    monkeypatch.setattr(backup, "DB_PATH", source)
    monkeypatch.setattr(backup, "BACKUP_DIR", backup_dir)
    monkeypatch.setattr(backup, "KEEP", 1)

    with pytest.raises(sqlite3.DatabaseError):
        backup.backup_once()

    assert backup._backup_files() == [previous]
    assert not list(backup_dir.glob("*.tmp"))


def test_stale_temp_files_are_cleaned(tmp_path, monkeypatch):
    source = tmp_path / "source.db"
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    _make_db(source)
    stale_raw = backup_dir / "dashboard-20260101-000000.db.tmp"
    stale_gzip = backup_dir / "dashboard-20260101-000000.db.gz.tmp"
    stale_raw.write_bytes(b"partial")
    stale_gzip.write_bytes(b"partial")

    monkeypatch.setattr(backup, "DB_PATH", source)
    monkeypatch.setattr(backup, "BACKUP_DIR", backup_dir)
    monkeypatch.setattr(backup, "KEEP", 2)

    backup.backup_once()

    assert not stale_raw.exists()
    assert not stale_gzip.exists()
    assert len(backup._backup_files()) == 1
