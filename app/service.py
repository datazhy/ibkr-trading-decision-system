"""守护进程入口：装配依赖并启动调度器（systemd 运行此模块）。

用法: python -m app.service
"""
from __future__ import annotations

import sys
import dataclasses

from .config import ConfigError, load_config
from .db import Database
from .engine import RuleEngine
from .errors import BrokerApiError
from .logging_setup import setup_logging
from .notify import Notifier
from .sync import SyncService
from .scheduler import build_scheduler


def make_broker_client(cfg):
    """装配数据源适配器（IBKR）。"""
    from .ibkr_client import IBKRClient
    return IBKRClient(cfg)


def build_context(env_file: str | None = ".env"):
    cfg = load_config(env_file)
    log = setup_logging(cfg.log_path, cfg.log_level)
    db = Database(cfg.db_path)
    db.init_schema()
    notifier = Notifier(cfg.apprise_urls, db)
    # 实时层独占原 clientId（尤其 clientId=0 的手工订单/成交事件）。分钟级
    # 校准/日线任务使用独立只读 clientId，避免 IB Gateway 踢掉同 ID 连接。
    broker_cfg = (dataclasses.replace(
        cfg, ib_client_id=cfg.ib_client_id + 40,
        ib_exec_client_id=cfg.ib_exec_client_id + 40)
        if cfg.live_sync_enabled else cfg)
    client = make_broker_client(broker_cfg)
    sync = SyncService(cfg, db, client, notifier)
    engine = RuleEngine(cfg, db, notifier)
    return cfg, log, db, notifier, sync, engine


def main() -> int:
    try:
        cfg, log, db, notifier, sync, engine = build_context()
    except ConfigError as exc:
        print(f"配置错误: {exc}", file=sys.stderr)
        return 2
    except BrokerApiError as exc:
        print(f"启动失败: {exc}", file=sys.stderr)
        print("提示：请检查 ib-gateway 容器与 .env 中的 IBKR 配置", file=sys.stderr)
        return 3

    log.info("小鱼交易台同步服务启动（IBKR 只读模式，不下单）")

    # 不在主线程建立 IB 连接：ib_async 的 IB 对象与创建它的线程/loop 绑定，
    # 若主线程先连、scheduler 线程再用会 RuntimeError。首轮同步交给 scheduler
    # 的单线程执行器（≤60s 内完成）。
    log.info("首轮同步由调度器线程执行（保证 IB 连接线程一致）")

    sched = build_scheduler(cfg, db, sync, notifier, engine)
    live_worker = None
    if cfg.live_sync_enabled:
        from .live_sync import LiveSyncWorker
        live_worker = LiveSyncWorker(cfg, db, notifier, engine)
        live_worker.start()
        log.info("实时层已启动：页面态 %.1fs / 快照与风控 %.1fs / 校准 %.1fs",
                 cfg.live_publish_interval, cfg.live_flush_interval,
                 cfg.live_reconcile_interval)
    try:
        sched.start()
    except (KeyboardInterrupt, SystemExit):
        log.info("同步服务停止")
    finally:
        if live_worker is not None:
            live_worker.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
