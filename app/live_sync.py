"""IBKR/Futu 长连接实时层。

三层频率各自承担不同职责：
- 1 秒：覆盖式发布 ``live_state_json``，供网页读，不生成历史行；
- 5 秒：原子账户快照、订单/成交落库、完整规则评估；
- 60 秒：向 IBKR 主动重取全量，校准可能遗漏的事件。

所有 broker 对象都在本线程创建和使用，遵守 ib_async 的线程/loop 约束。
"""
from __future__ import annotations

import threading
import time
from datetime import datetime, timezone

from .logging_setup import get_logger
from .market_hours import should_sync
from .sync import SyncService

log = get_logger()


class LiveSyncWorker:
    def __init__(self, cfg, db, notifier, engine, *, client_factory=None):
        self.cfg = cfg
        self.db = db
        self.notifier = notifier
        self.engine = engine
        self.client_factory = client_factory
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(
            target=self.run, name="ibkr-live-sync", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 10.0) -> None:
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=timeout)

    def _make_client(self):
        if self.client_factory is not None:
            return self.client_factory(self.cfg)
        from .ibkr_client import IBKRClient
        return IBKRClient(self.cfg)

    def run(self) -> None:
        backoff = 1.0
        while not self._stop.is_set():
            client = None
            try:
                client = self._make_client()
                sync = SyncService(self.cfg, self.db, client, self.notifier)
                account = sync.account_hash()
                client.start_live(account)
                self.db.set_sync_state("live_sync_status", "CONNECTED")
                quote_stop = threading.Event()
                quote_thread = None
                if getattr(self.cfg, "futu_quote_enabled", False):
                    quote_thread = threading.Thread(
                        target=self._run_futu_quotes,
                        args=(client, quote_stop),
                        name="futu-quote-cache", daemon=True)
                    quote_thread.start()
                try:
                    self._run_connected(client, sync, account)
                finally:
                    quote_stop.set()
                    if quote_thread and quote_thread.is_alive():
                        quote_thread.join(timeout=3.0)
                backoff = 1.0
            except Exception as exc:  # noqa: BLE001 — 常驻层必须自行恢复
                self.db.set_sync_state("live_sync_status", "ERROR")
                self.db.set_sync_state("live_sync_error", f"{type(exc).__name__}: {exc}")
                log.warning("实时层异常，%.1fs 后重连：%s", backoff, type(exc).__name__)
                self._stop.wait(backoff)
                backoff = min(backoff * 2, 30.0)
            finally:
                if client is not None:
                    try:
                        client.close_live()
                    except Exception:  # noqa: BLE001
                        pass
        self.db.set_sync_state("live_sync_status", "STOPPED")

    def _run_futu_quotes(self, client, stop: threading.Event) -> None:
        """Dedicated OpenD pipeline.  It never touches IBKR account objects."""
        backoff = 1.0
        while not stop.is_set() and not self._stop.is_set():
            try:
                result = client.refresh_futu_quotes()
                now_iso = datetime.now(timezone.utc).isoformat()
                self.db.set_sync_state("futu_quote_worker_last_run", now_iso)
                self.db.set_sync_state(
                    "futu_quote_worker_status",
                    "READY" if result.get("ok") else "DEGRADED")
                self.db.set_sync_state(
                    "futu_quote_coverage", str(result.get("coverage", "0")))
                backoff = 1.0
                stop.wait(max(float(self.cfg.live_publish_interval), 1.0))
            except Exception as exc:  # noqa: BLE001
                self.db.set_sync_state("futu_quote_worker_status", "ERROR")
                self.db.set_sync_state(
                    "futu_quote_worker_error", type(exc).__name__)
                log.warning("富途行情线程异常，IBKR 账户线程继续运行：%s",
                            type(exc).__name__)
                stop.wait(backoff)
                backoff = min(backoff * 2, 30.0)

    def _run_connected(self, client, sync: SyncService, account: str) -> None:
        now_mono = time.monotonic()
        next_publish = now_mono
        next_flush = now_mono
        next_reconcile = now_mono + self.cfg.live_reconcile_interval
        latest_live: dict = {}
        market_live: dict = {}
        persisted_executions_as_of: str | None = None

        while not self._stop.is_set():
            client.pump_live(min(self.cfg.live_publish_interval, 0.25))
            current = time.monotonic()

            if current >= next_reconcile:
                client.reconcile_live(account)
                self.db.set_sync_state(
                    "live_last_reconcile", datetime.now(timezone.utc).isoformat())
                next_reconcile = current + self.cfg.live_reconcile_interval

            if current >= next_publish:
                latest_live = client.live_payload(account)
                latest_live["status"] = "CONNECTED"
                latest_live["publish_interval_seconds"] = self.cfg.live_publish_interval
                latest_live["flush_interval_seconds"] = self.cfg.live_flush_interval
                latest_live["market"] = market_live
                self.db.set_live_state(latest_live)
                self.db.set_sync_state("live_last_publish", latest_live["generated_at"])
                # execDetails/commissionReport 是 IBKR 主动推送，这里只读
                # 本地事件缓存并落库，不发 reqExecutions，因此可按
                # 1 秒发布节奏安全更新成交历史，不占用官方请求频率。
                executions_as_of = (latest_live.get("timestamps") or {}).get(
                    "executions_as_of")
                if (executions_as_of
                        and executions_as_of != persisted_executions_as_of):
                    sync.sync_live_executions()
                    persisted_executions_as_of = executions_as_of
                next_publish = current + self.cfg.live_publish_interval

            if current >= next_flush and should_sync(
                    datetime.now(timezone.utc), self.cfg.market_tz):
                # 先落成交，随后持仓质量检查才能把数量变化与本次成交对应起来。
                changed_exec = sync.sync_live_executions()
                sync.sync_live_orders()
                ok = sync.sync_positions_and_balances(
                    trigger="live", fetcher=client.fetch_live_positions_and_balances)
                if ok:
                    self.db.set_sync_state(
                        "live_last_flush", datetime.now(timezone.utc).isoformat())
                    self.engine.evaluate_and_dispatch()
                elif changed_exec:
                    self.engine.evaluate_and_dispatch()
                # 市场代理与持仓共用同一条富途长连接。富途缺失的 VIX/VXN/US10Y
                # 不会被清空，market_service 会继续读取 IBKR 最近有效值。
                try:
                    from . import market_service
                    result = market_service.sync_live_market_data(
                        self.cfg, self.db, latest_live.get("market_quotes") or [])
                    if result["changed"] or not market_live:
                        market_service.assess(
                            self.cfg, self.db, now=datetime.now(timezone.utc), force=True)
                    market_live = market_service.live_market_payload(self.db)
                except Exception:  # noqa: BLE001 — 市场实时层不得影响账户风控
                    log.warning("富途市场实时评估失败（账户风控不受影响）", exc_info=True)
                next_flush = current + self.cfg.live_flush_interval


__all__ = ["LiveSyncWorker"]
