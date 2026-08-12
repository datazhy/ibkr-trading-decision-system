"""链上信号独立通知配额；不复用风险告警计数器。"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from typing import Any, Callable


def _now() -> datetime:
    return datetime.now(timezone.utc)


class HyperliquidAlertDispatcher:
    def __init__(self, db: Any, *, max_per_hour: int = 2, max_per_day: int = 8):
        self.db = db
        self.max_per_hour = max(0, int(max_per_hour))
        self.max_per_day = max(0, int(max_per_day))

    def dispatch(self, signal: dict[str, Any], sender: Callable[[str, str], Any], *,
                 now: datetime | None = None,
                 allowed_symbols: set[str] | None = None) -> dict[str, Any]:
        now = now or _now()
        attempted = now.isoformat()
        signal_id = str(signal.get("signal_id") or "")
        symbol = str(signal.get("canonical_symbol") or signal.get("coin") or "")
        state = str(signal.get("state") or "")
        if allowed_symbols is not None and symbol.upper() not in {str(x).upper() for x in allowed_symbols}:
            return {"status": "NOT_ALLOWED", "symbol": symbol}
        # 调度器只有在主库持仓/活动观察名单和新鲜正股报价同时满足时才会
        # 传入 dispatch；缺少该字段的独立调用保留向后兼容，不能绕过调度器。
        if "equity_quote_fresh" in signal and not signal.get("equity_quote_fresh"):
            return {"status": "QUOTE_STALE", "symbol": symbol}
        base = f"{signal_id}|{state}|{symbol}"
        window = int(now.timestamp() // 3600)
        dedupe = hashlib.sha256(f"{base}|{window}".encode()).hexdigest()
        with self.db.connect() as conn:
            recent = (now - timedelta(minutes=60)).isoformat()
            if conn.execute(
                    "SELECT 1 FROM hl_alert_dispatches WHERE signal_id=? AND canonical_symbol=? "
                    "AND attempted_at>=?", (signal_id, symbol, recent)).fetchone():
                return {"status": "DUPLICATE", "dedupe_key": dedupe}
            hour = (now - timedelta(hours=1)).isoformat()
            day = (now - timedelta(days=1)).isoformat()
            h = conn.execute("SELECT COUNT(*) FROM hl_alert_dispatches WHERE attempted_at>=?", (hour,)).fetchone()[0]
            d = conn.execute("SELECT COUNT(*) FROM hl_alert_dispatches WHERE attempted_at>=?", (day,)).fetchone()[0]
            if h >= self.max_per_hour or d >= self.max_per_day:
                conn.execute(
                    "INSERT INTO hl_alert_dispatches(id,signal_id,canonical_symbol,dedupe_key,attempted_at,success,error_code) "
                    "VALUES(?,?,?,?,?,?,?)", (f"hla_{dedupe[:20]}", signal_id, symbol, dedupe, attempted, 0, "RATE_LIMIT"))
                return {"status": "RATE_LIMIT", "dedupe_key": dedupe}
        try:
            sender(symbol, json.dumps(signal, ensure_ascii=False, default=str))
        except Exception as exc:  # noqa: BLE001
            with self.db.connect() as conn:
                conn.execute(
                    "INSERT INTO hl_alert_dispatches(id,signal_id,canonical_symbol,dedupe_key,attempted_at,success,error_code) "
                    "VALUES(?,?,?,?,?,?,?)", (f"hla_{dedupe[:20]}", signal_id, symbol, dedupe, attempted, 0, type(exc).__name__))
            return {"status": "ERROR", "dedupe_key": dedupe, "error": type(exc).__name__}
        with self.db.connect() as conn:
            conn.execute(
                "INSERT INTO hl_alert_dispatches(id,signal_id,canonical_symbol,dedupe_key,attempted_at,sent_at,success) "
                "VALUES(?,?,?,?,?,?,?)", (f"hla_{dedupe[:20]}", signal_id, symbol, dedupe, attempted, _now().isoformat(), 1))
        return {"status": "SENT", "dedupe_key": dedupe}
