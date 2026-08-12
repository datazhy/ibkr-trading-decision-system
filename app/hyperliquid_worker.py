"""独立 Hyperliquid 采集进程入口。

默认只读、只写 hyperliquid.db；网络故障只记录本轮状态，不会触碰主库。
"""
from __future__ import annotations

import logging
import time
import asyncio
import json
from datetime import datetime, timezone
from typing import Callable
from zoneinfo import ZoneInfo

from .config import load_config
from .hyperliquid_client import HyperliquidClient, WeightBudget
from .hyperliquid_db import HyperliquidDatabase, utcnow_iso
from .hyperliquid_service import HyperliquidService

log = logging.getLogger(__name__)


def collect_wallets_once(cfg, service: HyperliquidService) -> dict:
    """低频轮询已固定/自动准入钱包；每个地址故障隔离。"""
    if not getattr(cfg, "hl_wallet_scoring_enabled", False):
        return {"status": "DISABLED", "wallets": 0}
    try:
        from .db import Database
        with Database(cfg.db_path).connect() as conn:
            rows = conn.execute(
                "SELECT address FROM hl_wallet_controls WHERE mode IN ('PINNED','AUTO') "
                "ORDER BY CASE mode WHEN 'PINNED' THEN 0 ELSE 1 END, updated_at DESC LIMIT ?",
                (max(1, int(getattr(cfg, "hl_max_tracked_wallets", 50))),),
            ).fetchall()
    except Exception:
        return {"status": "NO_CONTROLS", "wallets": 0}
    now_ms = int(time.time() * 1000)
    start_ms = now_ms - 30 * 24 * 3600 * 1000
    done = errors = 0
    for row in rows:
        address = str(row["address"]).lower()
        try:
            if hasattr(service.client, "user_fills_window"):
                fills, fills_incomplete = service.client.user_fills_window(address, start_ms, now_ms)
            else:
                fills = service.client.user_fills_by_time(address, start_ms, now_ms, backfill=True)
                fills_incomplete = False
            service.ingest_wallet_fills(address, fills, dex=getattr(cfg, "hl_dex", "xyz"))
            service.upsert_wallet_candidate(address)
            if fills_incomplete:
                with service.db.connect() as conn:
                    conn.execute(
                        "INSERT INTO hl_data_gaps(coin,started_at,ended_at,reason,status,details_json,created_at) "
                        "VALUES(NULL,?,?,?,?,?,?)",
                        (datetime.fromtimestamp(start_ms / 1000, tz=timezone.utc).isoformat(),
                         datetime.fromtimestamp(now_ms / 1000, tz=timezone.utc).isoformat(),
                         "USER_FILLS_PAGE_INCOMPLETE_OR_10000_CAP", "INCOMPLETE",
                         json.dumps({"address": address, "rows": len(fills)}), utcnow_iso()),
                    )
            funding = service.client.user_funding(address, start_ms, now_ms,
                                                  dex=getattr(cfg, "hl_dex", "xyz"))
            service.ingest_wallet_funding(address, funding)
            ledger = service.client.user_ledger_updates(address, start_ms, now_ms)
            service.ingest_wallet_ledger(address, ledger)
            state = service.client.clearinghouse_state(address, getattr(cfg, "hl_dex", "xyz"))
            service.ingest_clearinghouse_state(address, state)
            service.rebuild_wallet_cycles(address)
            service.classify_wallet(address)
            service.refresh_cleaned_imbalance()
            service.score_wallet(address, model="INTRADAY")
            service.score_wallet(address, model="SWING")
            done += 1
        except Exception:  # noqa: BLE001
            errors += 1
            log.warning("Hyperliquid 钱包采集失败 address=%s", address, exc_info=True)
    if done:
        try:
            et = datetime.now(timezone.utc).astimezone(ZoneInfo("America/New_York"))
            # 每日群体在 16:30 ET 后冻结，20:00 ET 生效；之前的采集只更新
            # 评分，不提前改变正在使用的 membership 版本。
            if et.time() < datetime.strptime("16:30", "%H:%M").time():
                return {"status": "SUCCESS" if not errors else "PARTIAL",
                        "wallets": done, "errors": errors, "group_status": "NOT_DUE"}
            effective_at = service._scheduled_group_effective_at(utcnow_iso())
            for model in ("INTRADAY", "SWING"):
                service.freeze_wallet_group(model=model, effective_at=effective_at)
        except Exception:
            log.warning("Hyperliquid 钱包群体冻结失败", exc_info=True)
    return {"status": "SUCCESS" if not errors else "PARTIAL", "wallets": done, "errors": errors}


async def collect_trade_stream(cfg, service: HyperliquidService, *, duration_seconds: int = 240,
                               maintenance: Callable[[], object] | None = None,
                               maintenance_interval_seconds: int = 60) -> dict:
    """公共 trades WS；只订阅，不发送 exchange/交易消息。"""
    try:
        import websockets
    except ImportError:
        return {"status": "DEPENDENCY_MISSING", "trades": 0}
    coins = [row["coin"] for row in service.assets(limit=1000)
             if row.get("coin") and not row.get("is_delisted")]
    if not coins:
        return {"status": "NO_ASSETS", "trades": 0}
    count = 0
    pending: list[dict] = []
    # 仅做极短 overlap 试探；recentTrades 不是可靠历史回补，预算不足/无 overlap
    # 都不伪造连续性。全量正确性来自 WS 去重账本与 data gap 标记。
    for coin in coins[:5]:
        try:
            recent = service.client.recent_trades(coin)
            with service.db.connect(read_only=True) as conn:
                anchor = conn.execute(
                    "SELECT block_time,tid FROM hl_recent_trades WHERE coin=? "
                    "ORDER BY block_time DESC,tid DESC LIMIT 1", (coin,)).fetchone()
            keys = {(str(x.get("time") or x.get("block_time")), str(x.get("tid")))
                    for x in recent if isinstance(x, dict)}
            overlap = bool(anchor and (str(anchor["block_time"]), str(anchor["tid"])) in keys)
            if anchor is None or not overlap:
                with service.db.connect() as conn:
                    conn.execute(
                        "INSERT INTO hl_data_gaps(coin,started_at,ended_at,reason,status,details_json,created_at) "
                        "VALUES(?,?,?,?,?,?,?)",
                        (coin, utcnow_iso(), utcnow_iso(), "RECENT_TRADES_NO_OVERLAP", "INCOMPLETE",
                         json.dumps({"rows": len(recent), "anchor": dict(anchor) if anchor else None}), utcnow_iso()))
            count += service.ingest_trades(recent).get("trades", 0)
        except Exception:
            continue
    deadline = asyncio.get_running_loop().time() + max(1, duration_seconds)
    maintenance_every = max(60, int(maintenance_interval_seconds))
    next_maintenance = asyncio.get_running_loop().time() + maintenance_every
    try:
        async with websockets.connect(
                "wss://api.hyperliquid.xyz/ws", ping_interval=20, ping_timeout=20,
                close_timeout=5, max_size=8 * 1024 * 1024) as ws:
            for coin in coins:
                await ws.send(json.dumps({"method": "subscribe",
                                          "subscription": {"type": "trades", "coin": coin}}))
            while asyncio.get_running_loop().time() < deadline:
                now_loop = asyncio.get_running_loop().time()
                # 维护步骤必须与WS共享同一HyperliquidClient/WeightBudget。只在本轮
                # 剩余时间至少一个间隔时执行，确保最后一次回补额度在下一轮钱包/
                # candle步骤前过期，不相互挤占20%子预算。
                if (maintenance is not None and now_loop >= next_maintenance
                        and deadline - now_loop >= maintenance_every):
                    try:
                        await asyncio.to_thread(maintenance)
                    except Exception:
                        log.warning("Hyperliquid WS期间历史维护步骤失败", exc_info=True)
                    # 从本次调用完成时刻起算下一个间隔，避免网络耗时
                    # 压缩两次请求的真实间隔并抢占共享回溯预算。
                    next_maintenance = (asyncio.get_running_loop().time()
                                        + maintenance_every)
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=30)
                except asyncio.TimeoutError:
                    await ws.ping()
                    continue
                if isinstance(raw, bytes):
                    raw = raw.decode("utf-8", "ignore")
                try:
                    message = json.loads(raw)
                except (TypeError, ValueError):
                    continue
                data = message.get("data") if isinstance(message, dict) else None
                if message.get("channel") != "trades" or not isinstance(data, list):
                    continue
                pending.extend(x for x in data if isinstance(x, dict))
                if len(pending) >= 100:
                    result = service.ingest_trades(pending)
                    count += result.get("trades", 0)
                    pending.clear()
            if pending:
                count += service.ingest_trades(pending).get("trades", 0)
        return {"status": "SUCCESS", "trades": count, "coins": len(coins)}
    except Exception as exc:  # noqa: BLE001 — reconnect由主循环处理
        if pending:
            count += service.ingest_trades(pending).get("trades", 0)
        return {"status": "ERROR", "trades": count, "error": type(exc).__name__}


def collect_once(cfg, service: HyperliquidService | None = None) -> dict:
    if not getattr(cfg, "hyperliquid_enabled", False) or not getattr(cfg, "hl_collector_enabled", False):
        return {"status": "DISABLED"}
    db = service.db if service else HyperliquidDatabase(cfg.hl_db_path)
    # 允许被 scheduler/测试直接调用；初始化仍只发生在 HL 库，不会复制主库 schema。
    if not db.db_path.exists():
        db.init_schema()
    svc = service or HyperliquidService(cfg, db, HyperliquidClient(
        timeout=cfg.hl_http_timeout,
        budget=WeightBudget(cfg.hl_api_weight_per_minute),
        backfill_limit_pct=float(getattr(cfg, "hl_backfill_weight_pct", "0.20"))))
    run_id = f"hlrun_{int(time.time() * 1000)}"
    started = utcnow_iso()
    with db.connect() as conn:
        conn.execute("INSERT INTO hl_collection_runs(run_id,started_at,status,endpoint) VALUES(?,?,?,?)",
                     (run_id, started, "RUNNING", "metaAndAssetCtxs"))
    try:
        result = svc.sync_metadata(dex=cfg.hl_dex)
        cleanup = svc.cleanup_retention(now=utcnow_iso())
        wallet_result = collect_wallets_once(cfg, svc)
        backfill_result = {"status": "DISABLED"}
        if getattr(cfg, "hl_history_backfill_enabled", False):
            try:
                from .db import Database
                from .hyperliquid_backfill import HistoricalBackfillExecutor
                executor = HistoricalBackfillExecutor(cfg, svc, Database(cfg.db_path))
                cursor = executor._cursor("history:v2:next_scope")
                scope = "candles" if cursor.get("scope") == "wallets" else "wallets"
                backfill_result = executor.run_step(
                    days=int(getattr(cfg, "hl_history_backfill_days", 180)), scope=scope)
                executor._set_cursor("history:v2:next_scope", {"scope": scope})
            except Exception as exc:  # noqa: BLE001 — 回溯故障不影响实时采集
                log.warning("Hyperliquid 历史回溯步骤失败", exc_info=True)
                backfill_result = {"status": "ERROR", "error": type(exc).__name__}
        signal_result = {"features": 0, "signals": 0, "limited": 0}
        if getattr(cfg, "hl_signal_shadow_enabled", False):
            try:
                from .db import Database
                signal_result = svc.generate_shadow_signals(main_db=Database(cfg.db_path))
            except Exception:
                log.warning("Hyperliquid 影子信号生成失败", exc_info=True)
        with db.connect() as conn:
            conn.execute("UPDATE hl_collection_runs SET finished_at=?,status=?,rows_seen=? WHERE run_id=?",
                         (utcnow_iso(), "SUCCESS", result.get("assets", 0), run_id))
        return {"status": "SUCCESS", **result, "wallet_result": wallet_result,
                "backfill_result": backfill_result,
                "signal_result": signal_result, "retention": cleanup}
    except Exception as exc:  # noqa: BLE001 — 采集故障不影响其它进程
        log.exception("Hyperliquid 采集失败")
        with db.connect() as conn:
            conn.execute("UPDATE hl_collection_runs SET finished_at=?,status=?,error_code=?,error_message=? WHERE run_id=?",
                         (utcnow_iso(), "ERROR", type(exc).__name__, str(exc)[:500], run_id))
        return {"status": "ERROR", "error": type(exc).__name__}


def main() -> int:
    cfg = load_config()
    db = HyperliquidDatabase(cfg.hl_db_path)
    db.init_schema()
    client = HyperliquidClient(timeout=cfg.hl_http_timeout,
                               budget=WeightBudget(cfg.hl_api_weight_per_minute),
                               backfill_limit_pct=float(getattr(cfg, "hl_backfill_weight_pct", "0.20")))
    service = HyperliquidService(cfg, db, client)
    funding_executor = None
    if getattr(cfg, "hl_history_backfill_enabled", False):
        from .db import Database
        from .hyperliquid_backfill import HistoricalBackfillExecutor
        funding_executor = HistoricalBackfillExecutor(cfg, service, Database(cfg.db_path))

    def maintain_market_funding() -> object:
        if funding_executor is None:
            return {"status": "DISABLED"}
        return funding_executor.run_market_funding_step(
            days=int(getattr(cfg, "hl_history_backfill_days", 180)))

    while True:
        collect_once(cfg, service)
        if cfg.hyperliquid_enabled and cfg.hl_collector_enabled:
            asyncio.run(collect_trade_stream(
                cfg, service, duration_seconds=250,
                maintenance=maintain_market_funding if funding_executor else None,
                maintenance_interval_seconds=60))
        # WS只在metadata/钱包/candle轮询时短暂重连，不再人为留下5分钟空窗。
        time.sleep(1)


if __name__ == "__main__":
    raise SystemExit(main())
