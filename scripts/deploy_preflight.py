"""部署前置检查。

该命令只读检查配置、两库和公网入口前置条件，不连接 IBKR、不启动容器、
不输出任何凭据。它把“影子采集可运行”和“完整 live profile 可上线”分成
两个明确门禁，避免空凭据导致 ib-gateway/sync 反复重启。

用法：
    python scripts/deploy_preflight.py --mode shadow
    python scripts/deploy_preflight.py --mode full
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from pathlib import Path
from typing import Callable

# When invoked as ``python scripts/deploy_preflight.py`` (the documented form),
# Python puts ``scripts/`` rather than the repository root on sys.path.  Make
# the command work identically on the host and inside the deployment image.
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.config import Config, ConfigError, load_config


def _present(name: str) -> bool:
    return bool((os.environ.get(name) or "").strip())


def _read_only(path: Path, fn: Callable[[sqlite3.Connection], object]) -> object:
    if not path.exists():
        raise FileNotFoundError(path)
    uri = f"file:{path.resolve()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=5)
    try:
        return fn(conn)
    finally:
        conn.close()


def _check_db(path: Path, *, label: str) -> list[str]:
    errors: list[str] = []
    try:
        integrity = _read_only(path, lambda c: c.execute("PRAGMA integrity_check").fetchone())
        if not integrity or integrity[0] != "ok":
            errors.append(f"{label}_INTEGRITY")
    except (OSError, sqlite3.DatabaseError, FileNotFoundError):
        errors.append(f"{label}_UNAVAILABLE")
    return errors


def _site_error(site: str | None) -> str | None:
    value = (site or "").strip()
    if not value:
        return "DASHBOARD_SITE_MISSING"
    if value.startswith(("http://", "https://")) or "/" in value or any(ch.isspace() for ch in value):
        return "DASHBOARD_SITE_INVALID"
    if ":" in value:
        return "DASHBOARD_SITE_MUST_NOT_HAVE_PORT"
    if value in {"dash.example.com", "example.com", "localhost"} or value.endswith(".example.com"):
        return "DASHBOARD_SITE_PLACEHOLDER"
    return None


def _check_shadow(cfg: Config) -> list[str]:
    errors: list[str] = []
    if not cfg.hyperliquid_enabled:
        errors.append("HYPERLIQUID_DISABLED")
    if not cfg.hl_collector_enabled:
        errors.append("HL_COLLECTOR_DISABLED")
    errors.extend(_check_db(cfg.hl_db_path, label="HL_DB"))
    if cfg.hl_db_path.exists():
        try:
            latest = _read_only(
                cfg.hl_db_path,
                lambda c: c.execute(
                    "SELECT status FROM hl_collection_runs ORDER BY rowid DESC LIMIT 1"
                ).fetchone(),
            )
            if not latest or latest[0] != "SUCCESS":
                errors.append("HL_COLLECTION_NOT_READY")
        except (OSError, sqlite3.DatabaseError, FileNotFoundError):
            errors.append("HL_COLLECTION_STATE_UNREADABLE")
    return errors


def _check_full(cfg: Config) -> list[str]:
    errors = _check_shadow(cfg)
    for name in ("TWS_USERID", "TWS_PASSWORD", "TOTP_KEY"):
        if not _present(name):
            errors.append(f"{name}_MISSING")
    if not _present("LIVE_SYNC_ENABLED") or os.environ.get("LIVE_SYNC_ENABLED", "").strip().lower() in {
        "0", "false", "no", "off"
    }:
        errors.append("LIVE_SYNC_DISABLED")
    if not cfg.dashboard_auth_enabled:
        errors.append("DASHBOARD_AUTH_DISABLED")
    site_error = _site_error(os.environ.get("DASHBOARD_SITE"))
    if site_error:
        errors.append(site_error)
    if not _present("CF_API_TOKEN"):
        errors.append("CF_API_TOKEN_MISSING")
    errors.extend(_check_db(cfg.db_path, label="MAIN_DB"))
    if cfg.db_path.exists():
        try:
            password_hash = _read_only(
                cfg.db_path,
                lambda c: c.execute(
                    "SELECT value FROM sync_state WHERE key='web_auth_password_hash'"
                ).fetchone(),
            )
            if not password_hash or not password_hash[0]:
                errors.append("DASHBOARD_PASSWORD_NOT_SET")
        except (OSError, sqlite3.DatabaseError, FileNotFoundError):
            errors.append("DASHBOARD_AUTH_STATE_UNREADABLE")
    caddy_candidates = (
        Path(os.environ.get("CADDYFILE_PATH", "")) if os.environ.get("CADDYFILE_PATH") else None,
        Path("deploy/Caddyfile"),
        Path("/opt/risk-dashboard/deploy/Caddyfile"),
        Path("/app/deploy/Caddyfile"),
    )
    if not any(path is not None and path.exists() for path in caddy_candidates):
        errors.append("CADDYFILE_MISSING")
    return errors


def run(mode: str, *, env_file: str = ".env") -> int:
    try:
        cfg = load_config(env_file)
    except ConfigError as exc:
        print(f"FAIL CONFIG_ERROR {exc}")
        return 1
    errors = _check_shadow(cfg) if mode == "shadow" else _check_full(cfg)
    if errors:
        print(f"FAIL {mode.upper()} " + ",".join(errors))
        return 1
    print(f"READY {mode.upper()}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="小鱼交易台部署前置检查（只读）")
    parser.add_argument("--mode", choices=("shadow", "full"), default="shadow")
    parser.add_argument("--env-file", default=".env")
    args = parser.parse_args()
    return run(args.mode, env_file=args.env_file)


if __name__ == "__main__":
    raise SystemExit(main())
