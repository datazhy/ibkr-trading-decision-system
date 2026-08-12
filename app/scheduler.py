"""APScheduler 任务编排。

时区约定：
- 数据同步 + 止损计算：按北京时间（LOCAL_TZ），收盘后凌晨跑，weekday=tue-sat
  （美股 mon-fri 收盘落到北京次日凌晨）。
- 盘前/盘后报告：按美股东部时间（MARKET_TZ），遵循规格的 09:15 ET / 16:30 ET。
"""
from __future__ import annotations

import dataclasses
import threading
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

from apscheduler.events import EVENT_JOB_ERROR, EVENT_JOB_EXECUTED, EVENT_JOB_MISSED
from apscheduler.executors.pool import ThreadPoolExecutor
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger

from . import execution_context as ec
from . import x_service
from apscheduler.triggers.interval import IntervalTrigger

from .config import Config
from .db import Database
from .engine import RuleEngine
from .logging_setup import get_logger
from .notify import Notifier
from .market_hours import (CLOSED, in_reopen_grace, market_session,
                           session_label, should_sync)
from .reports import build_evening_report, build_morning_report
from .rules import evaluate_rules
from .sync import SyncService

log = get_logger()


def build_scheduler(
    cfg: Config, db: Database, sync: SyncService, notifier: Notifier, engine: RuleEngine
) -> BlockingScheduler:
    # IBKR account jobs remain on one thread because ib_async objects are bound
    # to their creating thread/event loop.  Slow historical market data and
    # Futu OpenD maintenance each get a dedicated executor and dedicated client.
    # This prevents a 180s market timeout from coalescing three account cycles.
    sched = BlockingScheduler(
        timezone=cfg.local_tz,
        executors={
            "default": ThreadPoolExecutor(max_workers=1),
            "market": ThreadPoolExecutor(max_workers=1),
            "quotes": ThreadPoolExecutor(max_workers=1),
            "chain": ThreadPoolExecutor(max_workers=1),
            # integrity_check/报告持久化在热SQLite上可能等待写锁；不能因此
            # 跳过每30秒的现实正股历史回溯。
            "chain_report": ThreadPoolExecutor(max_workers=1),
        },
    )
    _market_client = None
    _market_client_lock = threading.Lock()
    _chain_client = None
    _chain_client_lock = threading.Lock()

    def market_client():
        nonlocal _market_client
        if _market_client is None:
            with _market_client_lock:
                if _market_client is None:
                    from .ibkr_client import IBKRClient
                    isolated_cfg = dataclasses.replace(
                        cfg, ib_client_id=cfg.ib_client_id + 80,
                        ib_exec_client_id=cfg.ib_exec_client_id + 80,
                        futu_quote_enabled=False)
                    _market_client = IBKRClient(isolated_cfg)
        return _market_client

    def chain_client():
        """链上结果专用 IBKR 连接；不与账户/市场线程共享 ib_async 对象。"""
        nonlocal _chain_client
        if _chain_client is None:
            with _chain_client_lock:
                if _chain_client is None:
                    from .ibkr_client import IBKRClient
                    isolated_cfg = dataclasses.replace(
                        cfg, ib_client_id=cfg.ib_client_id + 90,
                        ib_exec_client_id=cfg.ib_exec_client_id + 90,
                        futu_quote_enabled=False)
                    _chain_client = IBKRClient(isolated_cfg)
        return _chain_client

    def positions_then_rules():
        """持仓/余额同步 + 规则评估。

        **更新窗口外（周末）完全停**：不同步、不评估、不推送预警。
        用户口径：北京周一 08:00 到周六 08:00 更新（= 美东周日 20:00 到周五 20:00，
        含每晚隔夜），其它时间内容不用更新。gateway_check（只读）仍每 5 分钟探活，
        保证周末连接健康、周一开盘即可用。
        """
        now = datetime.now(timezone.utc)
        if not should_sync(now, cfg.market_tz):
            return                 # 更新窗口外（周末）：不同步、不评估、不推送
        if sync.sync_positions_and_balances():
            db.set_sync_state("last_full_sync_ts", now.isoformat())
            hits = engine.evaluate_and_dispatch()
            _run_ops_layer(now, legacy_hits=hits)

    def _run_ops_layer(now, legacy_hits=None):
        """风险事件运营层：把账户/市场/联合评估结果转成可追踪事件并按策略通知。

        **整体异常全吞** —— 运营层是账户同步的下游，它出问题绝不能反过来
        影响数据同步。R1–R7 的 hits 只登记进时间线，通知仍由 engine 负责，
        避免同一风险双发。
        """
        try:
            from . import event_service, market_service, risk_service
            acc = mkt = None
            try:
                acc = risk_service.assess(cfg, db)
            except Exception:  # noqa: BLE001
                log.warning("账户风险评估失败（运营层跳过）", exc_info=True)
            if getattr(cfg, "market_enabled", True):
                try:
                    # assess 按 data_as_of + 版本幂等：已评估过就直接取回，不重算
                    mkt = market_service.assess(cfg, db)
                except Exception:  # noqa: BLE001
                    log.warning("市场评估读取失败（运营层按无市场数据处理）",
                                exc_info=True)
            event_service.process_assessments(
                cfg, db, notifier, account=acc, market=mkt, now=now,
                legacy_hits=legacy_hits,
                in_market_hours=(market_session(now, cfg.market_tz) != CLOSED),
                execution_context=ec.sync_context(
                    initiated_by="scheduler.risk_cycle"))
            # 技术面事件：隔离登记（scope 仅 technical，不推送），供技术触发时间线
            event_service.register_technical_events(cfg, db, now=now)
            # 建议合成（Phase 2）：确定性规则集把技术/计划/仓位/风险/市场合成为可追溯
            # 建议并按状态跃迁落库。旁路，异常不外抛；供首页「今日行动」与只读 API。
            try:
                from . import recommendation_service
                recommendation_service.run_synthesis(cfg, db, now=now)
            except Exception:  # noqa: BLE001
                log.warning("建议合成失败（运营层跳过）", exc_info=True)
        except Exception:  # noqa: BLE001
            log.warning("风险事件运营层异常（数据同步不受影响）", exc_info=True)

    def morning_report():
        now = datetime.now(timezone.utc)
        ctx = engine.build_rule_context(now)
        hits = evaluate_rules(ctx, engine.thresholds)
        title, body = build_morning_report(cfg, db, now, hits)
        notifier.send(title, body)

    def executions_then_rules():
        """2A：盘中 executions 增量（30–60s）→ 新成交/佣金落库后立即评估 R6。

        休市无成交，直接跳过（周末盘后不可能有新 fill）。
        """
        if not should_sync(datetime.now(timezone.utc), cfg.market_tz):
            return
        if sync.sync_executions():
            engine.evaluate_and_dispatch()

    def orders_sync():
        """订单：ib.trades() + completedOrders（后者非关键，超时即跳过）。"""
        if not should_sync(datetime.now(timezone.utc), cfg.market_tz):
            return
        sync.sync_orders()
        sync.sync_completed_orders()

    def daily_bars_backfill():
        """兜底：日线落后于最近交易日就补，补到了立刻重算止损位。

        原先日线只靠每天 05:35 那一次 cron，gateway 挂或容器重建撞上就整天为空，
        止损引擎连带 R7 全哑（已实际发生过）。此 job 消除这个单点。
        """
        if sync.ensure_daily_bars_fresh(client=market_client()):
            engine.compute_stop_levels()

    def refresh_instrument_metadata():
        """证券元数据刷新：只补缺失或过期（默认 7 天）的，不做无谓请求。

        失败完全静默 —— 分类是可降级能力，绝不能影响账户同步。
        """
        from . import instruments
        rows = db.latest_positions()
        syms = sorted({r["symbol"] for r in rows if r["symbol"]})
        if syms:
            instruments.refresh_metadata(db, sync.client, syms)

    def market_sync():
        """市场数据同步 + 环境评估。**与账户同步完全解耦**：
        独立任务、独立 run 状态，异常全吞，绝不影响 IBKR 账户数据。"""
        if not getattr(cfg, "market_enabled", True):
            return
        if not should_sync(datetime.now(timezone.utc), cfg.market_tz):
            return                      # 休市窗口外不取市场数据
        try:
            from . import market_service
            market_service.sync_market_data(cfg, db, market_client())
            market_service.assess(cfg, db)
            _run_ops_layer(datetime.now(timezone.utc))
        except Exception:  # noqa: BLE001
            log.warning("市场数据任务失败（账户同步不受影响）", exc_info=True)

    def futu_quote_refresh():
        """Maintain OpenD on the quote executor; account jobs only read cache."""
        try:
            result = sync.client.refresh_futu_quotes()
            now_iso = datetime.now(timezone.utc).isoformat()
            db.set_sync_state("futu_quote_worker_last_run", now_iso)
            db.set_sync_state("futu_quote_worker_status",
                              "READY" if result.get("ok") else "DEGRADED")
            db.set_sync_state("futu_quote_coverage", str(result.get("coverage", "0")))
        except Exception:  # noqa: BLE001 — quote failure cannot affect account jobs
            db.set_sync_state("futu_quote_worker_status", "ERROR")
            log.warning("富途行情维护失败（IBKR 账户同步不受影响）", exc_info=True)

    def research_refresh():
        """事件复盘与规则效果刷新。**纯离线分析**：异常全吞，
        绝不影响账户同步、市场同步或风险告警。

        放在低频档（默认 6 小时）—— 复盘结果只在观察窗口走完后才会变化，
        高频重算既没有新信息，又白白占用这台 2 核机器。
        """
        try:
            from . import review_service, rule_performance
            # 幂等重算：新版本写入，**旧版本结果保留不删**；
            # 只动派生结果，不碰原始事件、不发通知、不改冷静期
            r = review_service.generate_reviews(cfg, db)
            p = rule_performance.compute_all(cfg, db)
            log.info("复盘刷新 scanned=%d recommendations=%d written=%d completed=%d | 效果 rules=%d rows=%d",
                     r.get("scanned", 0), r.get("recommendations_scanned", 0),
                     r.get("written", 0) + r.get("recommendations_written", 0),
                     r.get("completed", 0),
                     p.get("rules", 0), p.get("rows", 0))
        except Exception:  # noqa: BLE001
            log.warning("复盘刷新异常（线上评估不受影响）", exc_info=True)

    def flex_sync_job():
        """Flex Query 同步。**T+1 数据源，低频即可**（默认 12 小时）。

        与实时账户同步完全解耦：异常全吞，未配置时静默跳过。
        Flex 挂了绝不能影响分钟级的账户同步、行情同步或风险告警。
        """
        try:
            from . import flex_sync
            if not getattr(cfg, "flex_sync_enabled", True):
                return
            if not flex_sync.is_configured(cfg):
                return              # 未配置：静默跳过，页面自会显示 not_configured
            res = flex_sync.run_flex_sync(
                cfg, db, trigger=flex_sync.TRIGGER_SCHEDULED)
            log.info("Flex 同步 %s：收到 %s 笔，新增 %s",
                     res.get("status"), res.get("records_received"),
                     res.get("records_inserted"))
        except Exception:  # noqa: BLE001
            log.warning("Flex 同步异常（实时同步不受影响）", exc_info=True)

    def evening_report():
        now = datetime.now(timezone.utc)
        title, body = build_evening_report(cfg, db, now)
        notifier.send(title, body)

    # 实时层已每 5 秒原子写持仓/余额/PnL，不再让旧轮询路径
    # 每分钟插入一份 IBKR 行情快照。60 秒仍运行运营层，用最新实时
    # 快照生成账户/市场/联合风险事件。未开启实时层时保留原轮询。
    if getattr(cfg, "live_sync_enabled", False):
        def live_risk_ops():
            now = datetime.now(timezone.utc)
            if should_sync(now, cfg.market_tz):
                _run_ops_layer(now)

        sched.add_job(
            live_risk_ops,
            IntervalTrigger(seconds=cfg.positions_sync_interval),
            id="risk_ops", max_instances=1, coalesce=True, misfire_grace_time=30,
        )
    else:
        sched.add_job(
            positions_then_rules,
            IntervalTrigger(seconds=cfg.positions_sync_interval),
            id="positions_rules", max_instances=1, coalesce=True,
            misfire_grace_time=30,
        )

    # Flex Query：T+1 券商流水，低频
    sched.add_job(
        flex_sync_job,
        IntervalTrigger(hours=getattr(cfg, "flex_sync_interval_hours", 12)),
        id="flex_sync", max_instances=1, coalesce=True, misfire_grace_time=3600,
    )

    # 事件复盘与规则效果：低频离线分析
    sched.add_job(
        research_refresh,
        IntervalTrigger(hours=getattr(cfg, "research_refresh_hours", 6)),
        id="research_refresh", max_instances=1, coalesce=True,
        misfire_grace_time=3600,
    )

    # 实时层启用时，clientId=0 由长连接独占并按事件落订单/成交；这里不能再建
    # 第二条 clientId=0 连接。未启用时保留原轮询路径，确保可平滑降级。
    if not getattr(cfg, "live_sync_enabled", False):
        sched.add_job(
            orders_sync,
            IntervalTrigger(seconds=cfg.orders_sync_interval),
            id="orders", max_instances=1, coalesce=True, misfire_grace_time=60,
        )
        sched.add_job(
            executions_then_rules,
            IntervalTrigger(seconds=cfg.exec_sync_interval),
            id="executions", max_instances=1, coalesce=True, misfire_grace_time=30,
        )

    # 注：原「交易流水」每日 cron（transactions 表）已下线 —— executions 每 45 秒同步，
    # 明细页、汇总、R6、亏损事件卡全部改读它。留着那张慢表只会让同一份数据有两个进度。

    # X 推文摄取：每 10 分钟。
    # 源库每天裁剪、只保留最近 5,000 条 —— 它是数据管道不是数据仓库，
    # 所以必须自己落库，否则今天能看到的历史明天就没了。
    # 源库采集本身是 30 分钟一轮（aleabitoreddit 15 分钟），10 分钟足够跟上。
    sched.add_job(
        lambda: x_service.ingest(db),
        IntervalTrigger(minutes=10),
        id="x_ingest", max_instances=1, coalesce=True, misfire_grace_time=120,
    )

    # 日线：北京 05:35（tue-sat）
    sched.add_job(
        sync.sync_daily_bars,
        CronTrigger(hour=5, minute=35, day_of_week="tue-sat", timezone=cfg.local_tz),
        id="daily_bars", max_instances=1, coalesce=True,
    )

    # 止损位计算：北京 05:40（tue-sat），紧随日线同步之后
    sched.add_job(
        engine.compute_stop_levels,
        CronTrigger(hour=5, minute=40, day_of_week="tue-sat", timezone=cfg.local_tz),
        id="stop_levels", max_instances=1, coalesce=True,
    )

    # 机会扫描：北京 05:50（tue-sat），日线与止损完成后运行；与 5 秒账户循环隔离。
    if getattr(cfg, "opportunity_enabled", False):
        def daily_opportunity_scan():
            try:
                from .opportunity_service import OpportunityService
                return OpportunityService(cfg, db).run_scan(trigger_source="scheduler")
            except Exception:  # noqa: BLE001 - 非关键机会层不得影响账户同步
                log.exception("机会扫描失败（已与账户同步隔离）")
                return None

        sched.add_job(
            daily_opportunity_scan,
            CronTrigger(hour=5, minute=50, day_of_week="tue-sat", timezone=cfg.local_tz),
            id="opportunity_scan", executor="market", max_instances=1, coalesce=True,
            misfire_grace_time=3600,
        )

        def daily_opportunity_outcomes():
            try:
                from .opportunity_service import OpportunityService
                return OpportunityService(cfg, db).calculate_outcomes()
            except Exception:  # noqa: BLE001 - 复盘层不得影响账户同步或下一次扫描
                log.exception("机会前向结果复盘失败（已与账户同步隔离）")
                return None

        sched.add_job(
            daily_opportunity_outcomes,
            CronTrigger(hour=5, minute=55, day_of_week="tue-sat", timezone=cfg.local_tz),
            id="opportunity_outcomes", executor="market", max_instances=1, coalesce=True,
            misfire_grace_time=3600,
        )

    # 链上低频结果/健康任务使用独立 executor；采集本身在 hyperliquid service
    # 中运行，不能占 default 的 ib_async 线程，也不能让 HL 故障改变主链路健康。
    if getattr(cfg, "hyperliquid_enabled", False):
        def hyperliquid_mapping_cycle():
            """低频三重映射：官方类别/下架、IBKR普通股、连续3日价格偏差。"""
            try:
                from . import hyperliquid_mapping
                from .hyperliquid_db import HyperliquidDatabase
                hl_db = HyperliquidDatabase(cfg.hl_db_path)
                if not hl_db.available():
                    return {"status": "UNAVAILABLE"}
                with hl_db.connect(read_only=True) as conn:
                    assets = [dict(r) for r in conn.execute(
                        "SELECT * FROM hl_assets ORDER BY coin").fetchall()]
                    contexts = {}
                    for asset in assets:
                        raw_contexts = [dict(r) for r in conn.execute(
                            "SELECT oracle_px,bucket_start FROM hl_asset_context_buckets_5m WHERE coin=? "
                            "AND oracle_px IS NOT NULL AND session='REGULAR' "
                            "ORDER BY bucket_start DESC LIMIT 30",
                            (asset["coin"],)).fetchall()]
                        # 一个 RTH 日期只取收盘前最后一条 ctx，不能把同一天的
                        # 多个 5m bucket 当成三日验证样本。
                        by_day = {}
                        for row in raw_contexts:
                            try:
                                dt = datetime.fromisoformat(str(row["bucket_start"]).replace("Z", "+00:00"))
                                if dt.tzinfo is None:
                                    dt = dt.replace(tzinfo=timezone.utc)
                                day = dt.astimezone(ZoneInfo("America/New_York")).date().isoformat()
                            except (TypeError, ValueError):
                                continue
                            by_day.setdefault(day, row)
                        contexts[asset["coin"]] = [by_day[d] for d in sorted(by_day)[-3:]]
                client = chain_client()
                symbols = sorted({a["symbol"] for a in assets if a.get("symbol")})
                ibkr_rows = client.fetch_contract_details(symbols)
                saved = 0
                for asset in assets:
                    symbol = asset.get("symbol")
                    if not symbol:
                        continue
                    ibkr = ibkr_rows.get(symbol)
                    bars = db.get_daily_bars(symbol, limit=10)
                    chain_rows = contexts.get(asset["coin"], [])
                    cash_by_day = {str(b.get("date"))[:10]: b for b in bars if b.get("date")}
                    paired_chain, paired_cash = [], []
                    for row in chain_rows:
                        try:
                            dt = datetime.fromisoformat(str(row["bucket_start"]).replace("Z", "+00:00"))
                            if dt.tzinfo is None:
                                dt = dt.replace(tzinfo=timezone.utc)
                            day = dt.astimezone(ZoneInfo("America/New_York")).date().isoformat()
                        except (TypeError, ValueError):
                            continue
                        if day in cash_by_day:
                            paired_chain.append(row["oracle_px"])
                            paired_cash.append(cash_by_day[day].get("c"))
                    result = hyperliquid_mapping.evaluate_mapping(
                        coin=asset["coin"], canonical_symbol=symbol, category=asset.get("category"),
                        universe=HyperliquidDatabase.decode_json(asset.get("metadata_json"), {}),
                        ibkr=ibkr, chain_closes=paired_chain, cash_closes=paired_cash,
                        denylist=set(getattr(cfg, "hl_symbol_denylist", [])),
                        max_deviation=Decimal(str(getattr(cfg, "hl_map_max_dev_ratio", "0.05"))),
                    )
                    hyperliquid_mapping.save_mapping(
                        db, result, checked_at=datetime.now(timezone.utc).isoformat())
                    saved += 1
                return {"status": "SUCCESS", "mappings": saved}
            except Exception:  # noqa: BLE001 — 映射失败只阻断链上准入
                log.exception("Hyperliquid 映射任务失败")
                return {"status": "ERROR"}

        sched.add_job(
            hyperliquid_mapping_cycle,
            IntervalTrigger(hours=24), id="hyperliquid_mapping", executor="chain",
            max_instances=1, coalesce=True, misfire_grace_time=3600,
            # 首次部署立即建立资产映射，后续每 24h 刷新；不能让新部署的
            # 109 个 XYZ 标的在第一天一直停留在 UNKNOWN。
            next_run_time=datetime.now(timezone.utc),
        )

        def hyperliquid_health_cycle():
            try:
                from .hyperliquid_db import HyperliquidDatabase
                from .hyperliquid_results import auto_suspend_after_failed_windows
                health = HyperliquidDatabase(cfg.hl_db_path).health()
                log.info("Hyperliquid health status=%s available=%s", health.get("status"),
                         health.get("available"))
                # 晋升状态保存在主库；连续两个失败窗口自动停用，保留
                # evaluation_run_id/reason 供人工复核。
                with db.connect() as conn:
                    promotions = [dict(r) for r in conn.execute(
                        "SELECT * FROM hl_model_promotions WHERE state='ACTIVE'").fetchall()]
                for promotion in promotions:
                    auto_suspend_after_failed_windows(
                        db, model=promotion["model"], rules_version=promotion["rules_version"],
                        rules_hash=promotion["rules_hash"], policy_hash=promotion.get("policy_hash"),
                    )
                return health
            except Exception:  # noqa: BLE001
                log.exception("Hyperliquid 健康读取失败")
                return None

        sched.add_job(
            hyperliquid_health_cycle,
            IntervalTrigger(minutes=5), id="hyperliquid_health", executor="chain",
            max_instances=1, coalesce=True, misfire_grace_time=120,
        )

        if getattr(cfg, "hl_history_backfill_enabled", False):
            def hyperliquid_equity_backfill_cycle():
                """快速通道正股历史：富途先补全、IBKR并行做权威补齐和对账。"""
                try:
                    from .hyperliquid_backfill import HistoricalBackfillExecutor
                    from .hyperliquid_db import HyperliquidDatabase
                    from .hyperliquid_service import HyperliquidService
                    hl_db = HyperliquidDatabase(cfg.hl_db_path)
                    if not hl_db.available():
                        return {"status": "UNAVAILABLE"}
                    executor = HistoricalBackfillExecutor(
                        cfg, HyperliquidService(cfg, hl_db), db)
                    if (getattr(cfg, "hl_futu_history_backfill_enabled", False)
                            and getattr(cfg, "futu_quote_enabled", False)):
                        try:
                            from .futu_quotes import FutuQuoteClient
                            futu_client = FutuQuoteClient(cfg)
                            quota = futu_client.history_quota()
                            if quota.get("remaining", 0) > 0:
                                futu_result = executor.run_equity_step(
                                    futu_client,
                                    days=int(getattr(cfg, "hl_history_backfill_days", 180)),
                                    source="FUTU_historical_5m",
                                    chunk_days=int(getattr(cfg, "hl_history_backfill_days", 180)),
                                )
                                futu_client._reset()
                                if futu_result.get("status") not in {"COMPLETE", "RETRY"}:
                                    return futu_result
                            else:
                                futu_client._reset()
                        except Exception:  # noqa: BLE001 — 立即回退IBKR，不阻断任务
                            log.warning("富途历史K线回补不可用，回退IBKR", exc_info=True)
                    return executor.run_equity_step(
                        chain_client(), days=int(getattr(cfg, "hl_history_backfill_days", 180)))
                except Exception:  # noqa: BLE001 — 历史回溯不影响账户同步
                    log.exception("Hyperliquid 正股历史回溯失败")
                    return {"status": "ERROR"}

            sched.add_job(
                hyperliquid_equity_backfill_cycle,
                IntervalTrigger(seconds=max(15, int(getattr(
                    cfg, "hl_equity_backfill_interval_seconds", 30)))),
                id="hyperliquid_equity_backfill",
                executor="chain", max_instances=1, coalesce=True,
                misfire_grace_time=120, next_run_time=datetime.now(timezone.utc),
            )

            def hyperliquid_data_quality_cycle():
                """按小时固化回溯完整性快照；报告失败不影响任何采集/账户任务。"""
                try:
                    from .hyperliquid_quality import write_quality_report
                    return write_quality_report(cfg)
                except Exception:  # noqa: BLE001
                    log.exception("Hyperliquid 数据完整性报告失败")
                    return {"status": "ERROR"}

            sched.add_job(
                hyperliquid_data_quality_cycle,
                IntervalTrigger(hours=1), id="hyperliquid_data_quality",
                executor="chain_report", max_instances=1, coalesce=True,
                misfire_grace_time=600, next_run_time=datetime.now(timezone.utc),
            )

        def hyperliquid_monthly_health_cycle():
            try:
                from .hyperliquid_db import HyperliquidDatabase
                from .hyperliquid_results import record_monthly_health
                return record_monthly_health(db, HyperliquidDatabase(cfg.hl_db_path))
            except Exception:  # noqa: BLE001
                log.exception("Hyperliquid 月度健康报告失败")
                return {"status": "ERROR"}

        sched.add_job(
            hyperliquid_monthly_health_cycle,
            CronTrigger(day=1, hour=6, minute=0, timezone=cfg.local_tz),
            id="hyperliquid_monthly_health", executor="chain", max_instances=1,
            coalesce=True, misfire_grace_time=3600,
        )

        if getattr(cfg, "hl_intraday_alerts_enabled", False):
            def hyperliquid_alert_cycle():
                try:
                    from .hyperliquid_alerts import HyperliquidAlertDispatcher
                    from .hyperliquid_db import HyperliquidDatabase
                    hl_db = HyperliquidDatabase(cfg.hl_db_path)
                    if not hl_db.available():
                        return {"status": "UNAVAILABLE"}
                    with hl_db.connect(read_only=True) as conn:
                        signals = [dict(r) for r in conn.execute(
                            "SELECT * FROM hl_signal_episodes WHERE state IN ('BULLISH','BEARISH') "
                            "AND quality_status='READY' AND confidence='HIGH' "
                            "ORDER BY last_seen_at DESC LIMIT 50").fetchall()]
                    # 告警只面向本人当前仓位或 ACTIVE 自选标的，并且必须有
                    # 10 分钟内的正股报价；链上信号不能在没有可执行标的价格时
                    # 抢占风险通知通道。
                    now = datetime.now(timezone.utc)
                    allowed: set[str] = set()
                    with db.connect() as conn:
                        latest_pos = db.latest_positions()
                        for row in latest_pos:
                            try:
                                qty = Decimal(str(row["qty"]))
                            except Exception:
                                qty = Decimal("0")
                            if not qty:
                                continue
                            quote_at = row["quote_as_of"]
                            fresh = False
                            try:
                                qt = datetime.fromisoformat(str(quote_at).replace("Z", "+00:00"))
                                if qt.tzinfo is None:
                                    qt = qt.replace(tzinfo=timezone.utc)
                                fresh = (now - qt).total_seconds() <= 600 and str(row["price_quality"] or "").upper() in {
                                    "REALTIME", "REALTIME_NONCONSOLIDATED", "DELAYED", "DELAYED_FROZEN", "FROZEN"
                                }
                            except (TypeError, ValueError):
                                pass
                            if fresh:
                                allowed.add(str(row["symbol"]).upper())
                        watch = conn.execute(
                            "SELECT symbol FROM opportunity_watchlist WHERE status='ACTIVE' LIMIT ?",
                            (int(getattr(cfg, "opportunity_max_active_symbols", 100)),)).fetchall()
                        for row in watch:
                            quote = conn.execute(
                                "SELECT quote_as_of,quality_status FROM market_data_snapshots "
                                "WHERE instrument_code=? ORDER BY data_as_of DESC LIMIT 1",
                                (str(row["symbol"]).upper(),)).fetchone()
                            if not quote or not quote["quote_as_of"] or str(quote["quality_status"] or "").lower() not in {"fresh", "ready"}:
                                continue
                            try:
                                qt = datetime.fromisoformat(str(quote["quote_as_of"]).replace("Z", "+00:00"))
                                if qt.tzinfo is None:
                                    qt = qt.replace(tzinfo=timezone.utc)
                                if (now - qt).total_seconds() <= 600:
                                    allowed.add(str(row["symbol"]).upper())
                            except (TypeError, ValueError):
                                continue
                    dispatcher = HyperliquidAlertDispatcher(
                        db, max_per_hour=getattr(cfg, "hl_alert_max_per_hour", 2),
                        max_per_day=getattr(cfg, "hl_alert_max_per_day", 8))
                    sent = 0
                    for signal in signals:
                        symbol = str(signal.get("canonical_symbol") or "").upper()
                        if symbol not in allowed:
                            continue
                        signal["equity_quote_fresh"] = True
                        result = dispatcher.dispatch(
                            signal, lambda symbol, body: notifier.send(f"链上信号 {symbol}", body),
                            allowed_symbols=allowed)
                        sent += result.get("status") == "SENT"
                    return {"status": "SUCCESS", "sent": sent}
                except Exception:  # noqa: BLE001
                    log.exception("Hyperliquid 通知任务失败")
                    return {"status": "ERROR"}

            sched.add_job(
                hyperliquid_alert_cycle,
                IntervalTrigger(minutes=5), id="hyperliquid_alerts", executor="chain",
                max_instances=1, coalesce=True, misfire_grace_time=120,
            )

        def hyperliquid_outcome_cycle():
            """拉取可审计 RTH 5m bar，再由独立结果器计算链上 episode。"""
            try:
                from .hyperliquid_db import HyperliquidDatabase
                from .hyperliquid_results import load_equity_bars, run_signal_outcomes, save_equity_bars
                hl_db = HyperliquidDatabase(cfg.hl_db_path)
                if not hl_db.available():
                    return {"status": "UNAVAILABLE"}
                with hl_db.connect(read_only=True) as conn:
                    signals = [dict(r) for r in conn.execute(
                        "SELECT * FROM hl_signal_episodes WHERE state IN ('BULLISH','BEARISH') "
                        "ORDER BY signal_at DESC LIMIT 100").fetchall()]
                if not signals:
                    return {"status": "NO_SIGNALS"}
                symbols = sorted({str(s.get("canonical_symbol") or "").upper()
                                  for s in signals if s.get("canonical_symbol")})
                now = datetime.now(timezone.utc)
                # 60 个交易日结果需要历史 bars；由 bar 表去重，增量运行不会重复计费。
                start = now - timedelta(days=100)
                client = chain_client()
                fetched = 0
                for symbol in [*symbols, "SPY"]:
                    with db.connect() as conn:
                        latest = conn.execute(
                            "SELECT MAX(fetched_at) AS fetched_at,MAX(bar_start) AS bar_start "
                            "FROM equity_bars_5m WHERE symbol=? AND source='IBKR_historical_5m'",
                            (symbol,)).fetchone()
                    refresh = True
                    if latest and latest["fetched_at"]:
                        try:
                            fetched_at = datetime.fromisoformat(str(latest["fetched_at"]).replace("Z", "+00:00"))
                            if fetched_at.tzinfo is None:
                                fetched_at = fetched_at.replace(tzinfo=timezone.utc)
                            refresh = (now - fetched_at).total_seconds() > 600
                        except ValueError:
                            refresh = True
                    if refresh:
                        bars = client.fetch_5m_bars(symbol, start, now, use_rth=True)
                        fetched += save_equity_bars(db, bars)
                result = run_signal_outcomes(db, hl_db, limit=100)
                return {"status": "SUCCESS", "bars": fetched, **result}
            except Exception:  # noqa: BLE001 — 结果层故障不影响主账户链路
                log.exception("Hyperliquid 正股结果任务失败")
                return {"status": "ERROR"}

        sched.add_job(
            hyperliquid_outcome_cycle,
            IntervalTrigger(minutes=15), id="hyperliquid_outcomes", executor="chain",
            max_instances=1, coalesce=True, misfire_grace_time=900,
            # 有影子 episode 时立即尝试拉取 5m RTH bars；没有 episode 时
            # 任务会安全返回 NO_SIGNALS。
            next_run_time=datetime.now(timezone.utc),
        )

    # 每日风险简报：北京 06:30（tue-sat），此时美股已收盘、账户与日线同步均已完成。
    # **不只依赖固定时间** —— 生成前检查数据依赖是否就绪，未就绪则有限次等待，
    # 超过上限出 limited 简报，绝不无限等待导致当天没有简报。
    if getattr(cfg, "brief_enabled", True):
        def daily_brief():
            """AI 简报任务。**任何失败都在这里终止，不影响同步与实时告警。**"""
            from . import brief_service as bsvc
            return bsvc.run_scheduled(cfg, db, notifier=notifier)

        def daily_brief_retry():
            """就绪重试：上一轮判定 waiting 时，隔一段时间再试一次。"""
            from . import brief_service as bsvc
            runs = db.brief_runs(limit=1)
            if not runs or runs[0].get("status") != "waiting":
                return None
            attempt = int(runs[0].get("wait_attempts") or 0) + 1
            try:
                return bsvc.run_scheduled(cfg, db, notifier=notifier,
                                          wait_attempt=attempt)
            except Exception:                              # noqa: BLE001
                log.exception("简报重试失败（已隔离）")
                return None

        sched.add_job(
            daily_brief,
            CronTrigger(hour=cfg.brief_hour_local, minute=cfg.brief_minute_local,
                        day_of_week="tue-sat", timezone=cfg.local_tz),
            id="daily_brief", max_instances=1, coalesce=True,
            misfire_grace_time=3600,
        )
        sched.add_job(
            daily_brief_retry,
            IntervalTrigger(minutes=20),
            id="daily_brief_retry", max_instances=1, coalesce=True,
            misfire_grace_time=300,
        )

    # 日线兜底补拉：每 30 分钟查一次本地库，落后才发请求（不落后则零开销）
    sched.add_job(
        daily_bars_backfill,
        IntervalTrigger(minutes=30),
        id="daily_bars_backfill", executor="market",
        max_instances=1, coalesce=True, misfire_grace_time=300,
    )

    # 盘前仓位体检：09:15 ET（mon-fri）
    sched.add_job(
        morning_report,
        CronTrigger(hour=9, minute=15, day_of_week="mon-fri", timezone=cfg.market_tz),
        id="morning_report", max_instances=1, coalesce=True,
    )

    # 盘后小结：16:30 ET（mon-fri）
    sched.add_job(
        evening_report,
        CronTrigger(hour=16, minute=30, day_of_week="mon-fri", timezone=cfg.market_tz),
        id="evening_report", max_instances=1, coalesce=True,
    )

    # 市场数据：每 N 分钟（默认 30）。日线数据变化慢，无需高频
    sched.add_job(
        market_sync,
        IntervalTrigger(minutes=getattr(cfg, "market_sync_interval_min", 30)),
        id="market_sync", executor="market",
        max_instances=1, coalesce=True, misfire_grace_time=600,
    )

    if getattr(cfg, "futu_quote_enabled", False) and not getattr(
            cfg, "live_sync_enabled", False):
        sched.add_job(
            futu_quote_refresh,
            IntervalTrigger(seconds=max(1.0, getattr(cfg, "live_publish_interval", 1.0))),
            id="futu_quote_refresh", executor="quotes",
            max_instances=1, coalesce=True, misfire_grace_time=10,
        )

    # 证券元数据：每 6 小时检查一次，只刷新过期项（分类变化以年计）
    sched.add_job(
        refresh_instrument_metadata,
        IntervalTrigger(hours=6),
        id="instrument_metadata", max_instances=1, coalesce=True, misfire_grace_time=600,
    )

    # 连接健康检查：gateway 连接状态，每 5 分钟
    # （断连去重告警，恢复靠适配器自动重连 + 宿主机 watchdog）
    def gateway_check():
        ok, detail = sync.client.auth_status()
        if not ok:
            # 两种预期内的掉线不推送：
            #   ① 更新窗口外（周末）—— 本就不同步，掉线不影响任何数据；
            #   ② 周日 20:00 窗口刚重开的宽限期 —— 周末停摆后 gateway 必然是
            #      登出状态，watchdog 会自动重启恢复，推送纯属噪音（实测踩到）。
            _now = datetime.now(timezone.utc)
            if (not should_sync(_now, cfg.market_tz)
                    or in_reopen_grace(_now, cfg.market_tz)):
                log.debug("gateway 失联但处于休市/窗口重开宽限期，跳过告警: %s", detail)
                return
            notifier.send_deduped(
                "ib_gateway_down",
                "🔴 小鱼交易台：IB Gateway 失联",
                f"{detail}\n数据同步将中断，请检查 ib-gateway 容器（每日重启窗口除外）。",
                dedup_window=timedelta(minutes=30),
            )
        else:
            log.debug("gateway 健康: %s", detail)

    sched.add_job(
        gateway_check,
        IntervalTrigger(minutes=5),
        id="gateway_check", max_instances=1, coalesce=True, misfire_grace_time=120,
    )

    # ---- 调度器健康：把 job 执行情况写进 sync_state，供看板健康详情读取 ----
    # 单线程执行器下，job 堆积会表现为 MISSED（上一个还没跑完）。这个数字是
    # 「同步是否跟得上」的直接信号，比只看容器 Up 有用得多。
    def _on_event(event):
        now_iso = datetime.now(timezone.utc).isoformat()
        try:
            scheduled = getattr(event, "scheduled_run_time", None)
            if scheduled is not None:
                if scheduled.tzinfo is None:
                    scheduled = scheduled.replace(tzinfo=timezone.utc)
                scheduled_utc = scheduled.astimezone(timezone.utc)
                scheduled_iso = scheduled_utc.isoformat()
                lateness = max(
                    (datetime.now(timezone.utc) - scheduled_utc).total_seconds(), 0.0)
            else:
                scheduled_iso, lateness = None, None
            if event.code == EVENT_JOB_EXECUTED:
                outcome = "EXECUTED"
                db.set_sync_state("scheduler_last_success", now_iso)
                db.set_sync_state("scheduler_last_job", event.job_id)
            elif event.code == EVENT_JOB_ERROR:
                outcome = "ERROR"
                db.set_sync_state("scheduler_last_error", now_iso)
                db.set_sync_state("scheduler_last_error_job", event.job_id)
                log.error("job %s 异常: %s", event.job_id, event.exception)
            elif event.code == EVENT_JOB_MISSED:
                outcome = "MISSED"
                prev = db.get_sync_state("scheduler_missed_count") or "0"
                try:
                    n = int(prev) + 1
                except ValueError:
                    n = 1
                db.set_sync_state("scheduler_missed_count", str(n))
                db.set_sync_state("scheduler_last_missed", now_iso)
                log.warning("job %s 错过执行（积压）", event.job_id)
            else:
                return
            try:
                error = getattr(event, "exception", None)
                db.insert_scheduler_job_diagnostic({
                    "job_id": event.job_id,
                    "scheduled_at": scheduled_iso,
                    "observed_at": now_iso,
                    "outcome": outcome,
                    "lateness_seconds": lateness,
                    "error_type": type(error).__name__ if error else None,
                })
            except Exception:  # noqa: BLE001 — diagnostics are best effort
                log.debug("写调度诊断失败", exc_info=True)
        except Exception:  # noqa: BLE001 — 可观测性不得拖垮调度
            log.debug("写调度器健康状态失败", exc_info=True)

    sched.add_listener(_on_event, EVENT_JOB_EXECUTED | EVENT_JOB_ERROR | EVENT_JOB_MISSED)
    return sched
