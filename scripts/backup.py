"""SQLite 每日热备 sidecar：原子写入 gzip 备份并按数量轮转。

作为独立容器常驻运行：备份一次 → 睡 24h。失败会清理临时文件并保留最近一份
已完成备份，避免磁盘不足时同时失去在线库和可恢复副本。
"""
from __future__ import annotations

import gzip
import os
import shutil
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

DB_PATH = Path(os.environ.get("DB_PATH", "/app/data/dashboard.db"))
BACKUP_DIR = Path(os.environ.get("BACKUP_DIR", "/app/backups"))
KEEP = max(1, int(os.environ.get("BACKUP_KEEP", "2")))
INTERVAL = int(os.environ.get("BACKUP_INTERVAL_SECONDS", str(24 * 3600)))
BACKUP_SOURCES = os.environ.get("BACKUP_SOURCES", "")


def _backup_files(prefix: str = "dashboard") -> list[Path]:
    files = [*BACKUP_DIR.glob(f"{prefix}-*.db"), *BACKUP_DIR.glob(f"{prefix}-*.db.gz")]
    return sorted(files, key=lambda path: path.name)


def _prune_backups(keep: int, prefix: str = "dashboard") -> None:
    backups = _backup_files(prefix)
    for old in backups[: max(0, len(backups) - keep)]:
        old.unlink()
        print(f"[backup] 轮转删除 {old.name}", flush=True)


def _compress(source: Path, destination: Path) -> None:
    with source.open("rb") as src, destination.open("wb") as raw_dst:
        with gzip.GzipFile(fileobj=raw_dst, mode="wb", compresslevel=1) as gz_dst:
            shutil.copyfileobj(src, gz_dst, length=1024 * 1024)
        raw_dst.flush()
        os.fsync(raw_dst.fileno())


def _configured_sources() -> list[tuple[str, Path]]:
    """显式解析双库来源；旧环境无 BACKUP_SOURCES 时保持 dashboard 兼容。"""
    if not BACKUP_SOURCES.strip():
        return [("dashboard", DB_PATH)]
    sources: list[tuple[str, Path]] = []
    for item in BACKUP_SOURCES.split(","):
        name, sep, path = item.partition(":")
        if sep and name.strip() and path.strip():
            sources.append((name.strip(), Path(path.strip())))
    return sources or [("dashboard", DB_PATH)]


def backup_once(prefix: str | None = None, source: Path | None = None) -> None:
    prefix = prefix or "dashboard"
    source = source or DB_PATH
    if not source.exists():
        print(f"[backup] 源库不存在，跳过: {source}", flush=True)
        return

    BACKUP_DIR.mkdir(parents=True, exist_ok=True)

    for stale in BACKUP_DIR.glob(f"{prefix}-*.tmp"):
        stale.unlink(missing_ok=True)
        print(f"[backup] 清理未完成备份 {stale.name}", flush=True)

    # 默认保留一份已完成备份，再为新库腾空间；KEEP=1 时也不先删掉唯一恢复点。
    _prune_backups(max(1, KEEP - 1), prefix)

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    raw_temp = BACKUP_DIR / f"{prefix}-{stamp}.db.tmp"
    gzip_temp = BACKUP_DIR / f"{prefix}-{stamp}.db.gz.tmp"
    destination = BACKUP_DIR / f"{prefix}-{stamp}.db.gz"
    src = dst = None
    try:
        src = sqlite3.connect(source)
        dst = sqlite3.connect(raw_temp)
        src.backup(dst)
        dst.close()
        dst = None
        src.close()
        src = None

        _compress(raw_temp, gzip_temp)
        raw_temp.unlink()
        gzip_temp.replace(destination)
        print(
            f"[backup] OK -> {destination} ({destination.stat().st_size / 1024 / 1024:.1f} MiB)",
            flush=True,
        )
    except Exception:
        raw_temp.unlink(missing_ok=True)
        gzip_temp.unlink(missing_ok=True)
        raise
    finally:
        if dst is not None:
            dst.close()
        if src is not None:
            src.close()

    _prune_backups(KEEP, prefix)


if __name__ == "__main__":
    while True:
        try:
            for source_prefix, source_path in _configured_sources():
                backup_once(source_prefix, source_path)
        except Exception as exc:  # noqa: BLE001
            print(f"[backup] 失败（下轮重试）: {exc}", flush=True)
        time.sleep(INTERVAL)
