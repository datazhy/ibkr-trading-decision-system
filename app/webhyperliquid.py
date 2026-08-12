"""链上情报页面的只读数据适配器。"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from .hyperliquid_db import HyperliquidDatabase
from .hyperliquid_service import HyperliquidService


def page_data(cfg: Any) -> dict[str, Any]:
    db = HyperliquidDatabase(getattr(cfg, "hl_db_path", "./data/hyperliquid.db"))
    health = db.health()
    if not health.get("available"):
        return {"enabled": bool(getattr(cfg, "hyperliquid_enabled", False)),
                "health": health, "assets": [], "signals": [], "wallets": [],
                "generated_at": datetime.now(timezone.utc).isoformat()}
    svc = HyperliquidService(cfg, db)
    return {"enabled": bool(getattr(cfg, "hyperliquid_enabled", False)),
            "health": health, "assets": svc.assets(limit=200),
            "signals": svc.signals(active_only=False, limit=100),
            "wallets": svc.wallets(limit=50),
            "generated_at": datetime.now(timezone.utc).isoformat()}
