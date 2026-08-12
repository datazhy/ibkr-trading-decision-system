"""同步服务：拉取 IBKR 数据并落库，失败时推送告警。

只读：本模块只调用 get_* 接口，绝不下单。
"""
from __future__ import annotations

import time
import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation

from . import daily_pnl_v2 as dpv2
from . import data_quality as dq
from . import heartbeat
from . import retry
from .broker import Broker
from .config import Config
from . import pnl as pnl_calc
from . import pnl_reliability as pnl_rel
from .db import Database, to_decimal
from .errors import BrokerApiError
from .logging_setup import get_logger
from .market_hours import CLOSED, market_session
from .notify import Notifier

log = get_logger()

# 同步任务归一状态（写入 sync_runs.run_state）
RUN_RUNNING = "running"
RUN_SUCCESS = "success"
RUN_PARTIAL = "partial"
RUN_FAILED = "failed"


def _mask(account: str | None) -> str:
    """账户号打码后再进日志 —— 日志会被翻阅、转发、贴进工单。"""
    if not account:
        return "-"
    return account[:2] + "***" + account[-2:] if len(account) > 4 else "***"


def _safe_err(exc: Exception, limit: int = 300) -> str:
    """异常文本入库/入日志前截断。

    连接类异常可能把 host:port、URL 甚至查询串带进来，故统一截断并只保留类型 + 摘要。
    绝不记录凭证：本项目的凭证只存在于 .env 与 gateway 容器内，不经过这里，
    但仍然按「异常文本不可信」处理。
    """
    return f"{type(exc).__name__}: {str(exc)[:limit]}"


# ---------- 日线兜底补拉（纯函数，便于单测） ----------
# 背景：daily_bars 原本只在北京 05:35 由 cron 拉一次，gateway 挂或容器重建撞上这一刻，
# 就整天没有日线 → stop_levels 为空 → R7 永不触发（已实际发生）。以下逻辑让调度器
# 周期性检查「日线是否落后于最近交易日」，落后就补。

_BARS_ATTEMPT_KEY = "daily_bars_backfill_attempt"   # 值格式 "YYYY-MM-DD|次数"
_MAX_BACKFILL_ATTEMPTS = 3
_CLOSE_HHMM = (16, 15)   # 收盘 16:00 ET + 15 分钟结算余量


def expected_last_bar_date(now: datetime, market_tz) -> date:
    """最近一个「应当已收盘」的工作日（ET）。

    刻意不引入交易日历：节假日会被算成工作日，届时补拉会连试
    _MAX_BACKFILL_ATTEMPTS 次后自动停止，等目标日期变化再重来 —— 用退避换掉
    一份需要长期维护的日历。
    """
    et = now.astimezone(market_tz)
    d = et.date()
    if (et.hour, et.minute) < _CLOSE_HHMM:
        d -= timedelta(days=1)          # 今天还没收盘，最新完整日线是昨天的
    while d.weekday() >= 5:             # 5=周六 6=周日
        d -= timedelta(days=1)
    return d


def stale_symbols(latest_dates: dict[str, str | None], expected: str) -> list[str]:
    """日线缺失或落后于 expected（YYYY-MM-DD）的标的。"""
    return sorted(s for s, d in latest_dates.items() if d is None or d < expected)


def should_attempt(state: str | None, expected: str,
                   max_attempts: int = _MAX_BACKFILL_ATTEMPTS) -> bool:
    """同一目标日期最多试 max_attempts 次；目标日期一变即重新计数。"""
    if not state:
        return True
    day, _, n = state.partition("|")
    if day != expected:
        return True
    try:
        return int(n) < max_attempts
    except ValueError:
        return True                     # 状态损坏时按可尝试处理，不因此瘫痪


def bump_attempt(state: str | None, expected: str) -> str:
    day, _, n = (state or "").partition("|")
    try:
        count = int(n) if day == expected else 0
    except ValueError:
        count = 0
    return f"{expected}|{count + 1}"


class SyncService:
    def __init__(self, cfg: Config, db: Database, client: Broker, notifier: Notifier):
        self.cfg = cfg
        self.db = db
        self.client = client
        self.notifier = notifier
        self._account_hash: str | None = None
        # v2 自检 + 口径声明。自检不通过就**抛** —— 静默退回旧口径是最坏的结果：
        # 运维以为切过去了，实际还在跑旧口径而且没人知道。
        # 开关关闭时 preflight 直接 skip，这里等于只打一行声明日志。
        dpv2.require_ready(cfg, db)
        dpv2.log_startup(cfg)

    # ---------- 账户号 ----------

    def get_account_number(self) -> str | None:
        """IB_ACCOUNT；留空则由适配器自动取 gateway 的第一个账户。"""
        return self.cfg.ib_account

    def account_hash(self) -> str:
        if self._account_hash is None:
            self._account_hash = self.client.resolve_account_hash(self.get_account_number())
        return self._account_hash

    # ---------- 告警 ----------

    def _alert_failure(self, op: str, exc: Exception) -> None:
        self.notifier.send_deduped(
            f"sync_fail_{op}",
            f"⚠️ 小鱼交易台：{op} 同步失败",
            f"{op} 连续重试后仍失败：{exc}",
            dedup_window=timedelta(minutes=30),
        )

    # ---------- 各同步任务 ----------

    def sync_positions_and_balances(self, *, trigger: str = "auto",
                                    request_id: str | None = None,
                                    fetcher=None) -> bool:
        """账户+持仓+行情一次同步，共用一个 sync_run_id / snapshot_id。

        批次 ID 是「页面与规则引擎读的是同一时点数据」的证据 —— 没有它，
        两个页面各读一半快照却都声称是当前状态，就是把不同时点伪装成同一时点。

        本方法保证三件事：
          1. **原子**：账户汇总、持仓明细、质量问题在同一个事务里写入，
             不存在「有汇总没明细」的半成品快照；
          2. **失败不破坏**：任何异常都只把本次 run 标为 failed，
             上一份有效快照原样保留（历史只增不改）；
          3. **可追溯**：耗时、错误码、条数、质量结论全部落库。
        """
        now = datetime.now(timezone.utc)
        ts = now.isoformat()
        run_id = uuid.uuid4().hex
        snapshot_id = uuid.uuid4().hex
        trace = request_id or run_id[:8]
        account = self.account_hash()
        self.db.start_sync_run(run_id, ts, account=account, source="IBKR")
        self.db.mark_sync_run_running(run_id, trigger=trigger, request_id=request_id)
        log.info("同步开始 trace=%s run=%s trigger=%s account=%s",
                 trace, run_id[:8], trigger, _mask(account))

        t0 = time.monotonic()
        # 有限重试：只对瞬时网络故障退避重试；认证/权限/字段错误立即失败
        # （重试错误凭证可能触发券商风控锁定，代价远大于一次同步失败）
        outcome = retry.call_with_retry(
            lambda: (fetcher or self.client.fetch_positions_and_balances)(account),
            max_retries=self.cfg.sync_max_retries,
            base_delay=self.cfg.sync_retry_base_delay)
        fetch_ms = int((time.monotonic() - t0) * 1000)
        if not outcome.ok:
            exc = outcome.last_exc or BrokerApiError("未知同步失败")
            # 失败路径：绝不触碰已有快照，只记录本次 run
            log.error("同步失败 trace=%s stage=fetch code=%s retries=%d err=%s",
                      trace, outcome.error_code, outcome.retry_count, _safe_err(exc))
            self.db.set_sync_state("data_health", "ERROR")
            self.db.finish_sync_run(run_id, status="ERROR", note=_safe_err(exc),
                                    run_state=RUN_FAILED, fetch_ms=fetch_ms,
                                    error_code=outcome.error_code,
                                    error_message=outcome.attempts_text(),
                                    retry_count=outcome.retry_count)
            self._persist_pnl_audit(run_id=run_id, snapshot_id=None,
                                    account=account, balances=None, positions=None)
            self._record_failure_issues(run_id, account, exc)
            self._alert_failure("positions_balances", exc)
            return False
        positions, balances = outcome.value
        retry_count = outcome.retry_count
        self._persist_pnl_audit(run_id=run_id, snapshot_id=snapshot_id,
                                account=account, balances=balances,
                                positions=positions)

        # 数据健康闸门：首轮/未就绪不写零快照，避免污染净资产曲线与规则输入
        try:
            equity = Decimal(str(balances.get("equity", 0)))
        except Exception:  # noqa: BLE001
            equity = Decimal("0")
        ready = balances.get("_ready", equity > 0)
        if not ready:
            self.db.set_sync_state("data_health", "WARMING_UP")
            self.db.finish_sync_run(run_id, status="WARMING_UP", run_state=RUN_PARTIAL,
                                    fetch_ms=fetch_ms, error_code="ACCOUNT_NOT_READY",
                                    error_message="账户数据未就绪，跳过快照写入",
                                    positions_count=len(positions or []))
            log.warning("账户数据未就绪 trace=%s equity=%s，跳过本轮快照写入", trace, equity)
            return False

        balances.setdefault("account", account)
        balances["data_quality"] = "READY"

        # ---- 数据质量体检（在写入之前跑，结论与快照同事务落库）----
        report = self._evaluate_quality(now, ts, balances, positions)

        t1 = time.monotonic()
        self.db.write_account_snapshot(
            snapshot_id=snapshot_id, ts=ts, balances=balances, positions=positions,
            sync_run_id=run_id, quality_status=report.status,
            ai_analysis_allowed=report.ai_analysis_allowed,
            issues=report.as_dicts(ts),
        )
        write_ms = int((time.monotonic() - t1) * 1000)

        # 持仓计划是账户快照的旁路审计层。它只能消费已经成功落库的客观数据，
        # 任何计划/日记异常都不得回滚或阻断原有持仓、规则和成交同步。
        self._sync_position_plans(
            account, positions, snapshot_id=snapshot_id, observed_at=ts,
            allow_close=report.status != dq.ERROR, balances=balances)

        self.db.set_sync_state("data_health", "READY")
        self.db.set_sync_state("last_sync_run_id", run_id)
        self.db.set_sync_state("last_snapshot_id", snapshot_id)
        # 有 critical 问题时本轮算 partial：数据写进去了，但不可用于可靠分析
        run_state = RUN_PARTIAL if report.status == dq.ERROR else RUN_SUCCESS
        self.db.finish_sync_run(run_id, status="READY", run_state=run_state,
                                snapshot_id=snapshot_id, fetch_ms=fetch_ms,
                                write_ms=write_ms, positions_count=len(positions),
                                retry_count=retry_count)
        # ---- 双顺序：由 DAILY_PNL_V2_ENABLED 选择，不得无条件调整 ----
        #
        #   off（生产现状）: assess(旧口径) -> 日终净值 -> PnL 快照 -> 影子观测
        #   on             : 日终净值 -> PnL 快照 -> 观测 -> assess(v2，注入观测)
        #
        # 影子期必须让旧路一字不动地继续驱动线上事件，否则没有干净的对照。
        # 顺序本身就是修复的核心：v2 要求评估时刻分子已落库，才谈得上同周期配对。
        v2 = dpv2.enabled(self.cfg)

        def _archive_equity_and_pnl():
            # 日终净资产按**美东交易日**归档（曲线与 7/30 日窗口的唯一口径）
            self.db.upsert_daily_equity(
                account, pnl_calc.et_date_of(now, self.cfg.market_tz),
                net_liquidation=equity, as_of=ts,
                source="IBKR_accountSummary", quality="READY")
            # 账户级 PnL 与本批持仓同一时刻取得 -> 同 ts、同 run_id 落库
            acct_pnl = balances.get("_account_pnl") or {}
            return self._write_pnl_snapshot(
                account, ts, now, acct_pnl, run_id,
                positions=positions, equity=equity, snapshot_id=snapshot_id,
                currency=balances.get("currency"))

        if not v2:
            # 快照落库后立即评估风险：与 snapshot_id 一一对应，页面与 API 直接读结果
            self._assess_quietly(snapshot_id, now, trace)
            self._log_quality(report, trace)
            _archive_equity_and_pnl()
        else:
            self._log_quality(report, trace)
            observation = _archive_equity_and_pnl()
            self._assess_quietly(snapshot_id, now, trace,
                                 observation=observation,
                                 calculation_mode=dpv2.MODE_V2)

        log.info("持仓/余额快照写入：%d 个持仓，净资产=%s（批次 %s）",
                 len(positions), balances["equity"], run_id[:8])
        heartbeat.ping(self.cfg.heartbeat_url)  # dead man's switch：同步活着的证明
        return True

    def _persist_pnl_audit(self, *, run_id: str, snapshot_id: str | None,
                           account: str, balances: dict | None,
                           positions: list[dict] | None) -> None:
        """落库 IBKR PnL 原始回调/生命周期与双源 shadow 对账。

        这是观测旁路：任何数据库或解析异常都吞掉，绝不能改变账户快照、规则或
        通知路径。金额大小只用于 ``large_but_explained`` 标签，不用于拒绝数据。
        """
        try:
            drain = getattr(self.client, "drain_pnl_audit", None)
            callbacks, lifecycle = drain() if drain else ([], [])
            self.db.insert_ibkr_pnl_callbacks(callbacks)
            self.db.insert_ibkr_pnl_subscription_events(lifecycle)
            if balances is None:
                return
            acct = balances.get("_account_pnl") or {}
            daily = pnl_rel.decimal_or_none(acct.get("daily_pnl"))
            realized = pnl_rel.decimal_or_none(acct.get("realized_pnl"))
            unrealized = pnl_rel.decimal_or_none(acct.get("unrealized_pnl"))
            pos_values = [pnl_rel.decimal_or_none(p.get("day_pnl"))
                          for p in (positions or [])]
            pos_values = [v for v in pos_values if v is not None]
            position_total = sum(pos_values, Decimal("0")) if pos_values else None
            official_status = pnl_rel.classify_official_reconciliation(
                daily, position_total)
            shadow = balances.get("_futu_shadow") or {}
            coverage = pnl_rel.decimal_or_none(shadow.get("quote_coverage")) or Decimal("0")
            futu_mtm = pnl_rel.decimal_or_none(
                shadow.get("futu_intraday_mark_to_market_change"))
            futu_status = pnl_rel.classify_futu_reconciliation(
                quote_coverage=coverage,
                quote_age_seconds=None,
                official_daily=daily,
                futu_mtm=futu_mtm)
            supported = official_status == "consistent" or futu_status in {
                "consistent", "explainable_accounting_difference"
            }
            large_explained = bool(daily is not None and abs(daily) >= Decimal("1000")
                                   and supported)
            limitations = []
            if daily is None:
                limitations.append("OFFICIAL_DAILY_PNL_UNAVAILABLE")
            if position_total is None:
                limitations.append("POSITION_DAILY_TOTAL_UNAVAILABLE")
            if coverage < Decimal("1"):
                limitations.append("FUTU_QUOTE_PARTIAL")
            if callbacks and not any(r.get("accepted") for r in callbacks):
                limitations.append("NO_ACCEPTED_PNL_CALLBACK")
            self.db.insert_pnl_reconciliation({
                "observed_at": datetime.now(timezone.utc).isoformat(),
                "sync_run_id": run_id, "snapshot_id": snapshot_id,
                "account": account, "official_daily_pnl": daily,
                "official_realized_pnl": realized,
                "official_unrealized_pnl": unrealized,
                "position_daily_total": position_total,
                "futu_intraday_mtm": futu_mtm,
                "futu_total_position_value": shadow.get("futu_total_position_value"),
                "quote_coverage": coverage,
                "official_position_status": official_status,
                "futu_status": futu_status,
                "large_but_explained": large_explained,
                "limitations": limitations,
            })
        except Exception:  # noqa: BLE001 — 旁路审计不得拖垮同步
            log.warning("PnL 审计/双源对账落库失败（账户同步不受影响）",
                        exc_info=True)

    def _sync_position_plans(self, account: str, positions: list[dict] | None, *,
                             snapshot_id: str | None = None,
                             observed_at: str | None = None,
                             allow_close: bool = False,
                             balances: dict | None = None) -> dict | None:
        """推进持仓周期；功能旁路失败不能影响核心只读同步。"""
        try:
            from .position_plans import PositionPlanService
            plan_positions = positions
            if positions is not None:
                try:
                    equity = Decimal(str((balances or {}).get("equity")))
                except (InvalidOperation, TypeError, ValueError):
                    equity = Decimal("0")
                copied = [dict(item) for item in positions]
                stock_total = sum(
                    max((Decimal(str(item.get("market_value")))
                         if item.get("market_value") not in (None, "") else Decimal("0")),
                        Decimal("0"))
                    for item in copied
                )
                for item in copied:
                    try:
                        market_value = Decimal(str(item.get("market_value")))
                    except (InvalidOperation, TypeError, ValueError):
                        market_value = Decimal("0")
                    if equity > 0 and market_value:
                        item["account_weight"] = market_value / equity
                        # legacy current_weight is retained as the account denominator.
                        item["current_weight"] = item["account_weight"]
                    if stock_total > 0 and market_value:
                        item["stock_weight"] = market_value / stock_total
                plan_positions = copied
            result = PositionPlanService(
                self.db, account, actor="sync", local_tz=self.cfg.local_tz,
                market_tz=self.cfg.market_tz).reconcile(
                snapshot_id=snapshot_id, observed_at=observed_at,
                positions=plan_positions, allow_close=allow_close)
            if result.get("closed_cycles"):
                log.info("持仓计划周期清仓归档 account=%s cycles=%s",
                         _mask(account), result["closed_cycles"])
            return result
        except Exception:  # noqa: BLE001 — 旁路失败不得拖垮账户同步
            log.warning("持仓计划同步失败（账户/成交快照不受影响）", exc_info=True)
            return None

    def sync_live_orders(self) -> bool:
        """把 IBKR 订单事件缓存落库；不发新的 broker 请求。"""
        try:
            rows = self.client.fetch_live_orders(self.account_hash())
        except Exception as exc:  # noqa: BLE001 — 实时层失败由 60 秒校准恢复
            log.warning("实时订单缓存落库失败: %s", _safe_err(exc))
            return False
        n = self.db.upsert_orders(rows)
        self.db.set_sync_state("orders_last_sync", datetime.now(timezone.utc).isoformat())
        return n > 0

    def sync_live_executions(self) -> bool:
        """把 IBKR 成交/佣金事件缓存落库；exec_id 幂等。"""
        account = self.account_hash()
        try:
            rows = self.client.fetch_live_executions(account)
        except Exception as exc:  # noqa: BLE001
            log.warning("实时成交缓存落库失败: %s", _safe_err(exc))
            return False
        n = self.db.upsert_executions(rows, account=account)
        if n:
            self._sync_position_plans(account, None, allow_close=False)
        self.db.set_sync_state("executions_last_sync", datetime.now(timezone.utc).isoformat())
        return n > 0

    # ---------- 数据质量 ----------

    def _evaluate_quality(self, now: datetime, ts: str, balances: dict,
                          positions: list[dict]) -> "dq.QualityReport":
        """对本轮快照做体检。所有上下文（上一份快照、近期成交、行情质量）都在这里取。"""
        prev_bal = self.db.latest_balances()
        prev_pos = self.db.latest_positions()
        quote_qualities: dict[str, int] = {}
        for p in positions or []:
            q = p.get("price_quality")
            if q:
                quote_qualities[q] = quote_qualities.get(q, 0) + 1
        # 近期成交的标的：用于解释「持仓消失/数量突变」，避免正常交易被报成异常
        try:
            since = (now - timedelta(days=1)).isoformat()
            recent = {r["symbol"] for r in self.db.executions_between(since, None)
                      if r["symbol"]}
        except Exception:  # noqa: BLE001 — 质量检查不得因取上下文失败而中断同步
            recent = set()
        acct_pnl = balances.get("_account_pnl") or {}
        return dq.evaluate(
            balances=balances, positions=positions, now=now, data_as_of=ts,
            market_open=market_session(now, self.cfg.market_tz) != CLOSED,
            pnl=acct_pnl,
            prev_balances=dict(prev_bal) if prev_bal else None,
            prev_positions=[dict(r) for r in prev_pos] if prev_pos else None,
            recent_exec_symbols=recent,
            connected=True, account_data_ok=True,
            quote_qualities=quote_qualities,
            thresholds=dq.thresholds_from_config(self.cfg),
        )

    def _record_failure_issues(self, run_id: str, account: str | None,
                               exc: Exception) -> None:
        """同步失败时也要留下质量记录 —— 否则「为什么今天没数据」无从追溯。

        没有 snapshot_id（本次压根没产生快照），问题挂在 sync_run 上。
        """
        has_history = self.db.latest_snapshot_row(account) is not None
        issues = [dq.Issue("SYNC_FAILED", dq.CRITICAL, dq.SOURCE,
                           f"同步失败：{_safe_err(exc)}", "sync", "success", "failed",
                           blocks_ai=not has_history)]
        if not has_history:
            issues.append(dq.Issue("NO_VALID_SNAPSHOT", dq.CRITICAL, dq.SOURCE,
                                   "同步失败且没有可用的历史快照", "snapshot", "存在",
                                   "无", blocks_ai=True))
        try:
            self.db.record_quality_issues(
                [i.as_dict(datetime.now(timezone.utc).isoformat()) for i in issues],
                snapshot_id=None, sync_run_id=run_id, account=account)
        except Exception:  # noqa: BLE001 — 记录失败不得掩盖原始错误
            log.debug("质量问题落库失败", exc_info=True)

    def _log_quality(self, report, trace: str) -> None:
        if report.issues:
            log.info("数据质量 trace=%s status=%s ai_allowed=%s issues=%s",
                     trace, report.status, report.ai_analysis_allowed,
                     ",".join(f"{i.issue_code}:{i.severity}" for i in report.issues))

    def _assess_quietly(self, snapshot_id: str, now: datetime, trace: str, *,
                        observation=None, calculation_mode: str | None = None) -> None:
        """跑风险评估。失败只记日志 —— 评估挂了不得影响账户数据本身。

        v2 顺序下 `observation` 为 None 意味着本轮观测没能建成。这时**照样评估**：
        `ACC_DAILY_LOSS_HIGH` 会因拿不到可靠观测而判 insufficient_data，
        从而保持既有 episode 状态不变。其余规则不受影响，照常评估 ——
        「一个指标缺失」绝不该让整套风控停摆。
        """
        try:
            from . import risk_service
            risk_service.assess(self.cfg, self.db, snapshot_id=snapshot_id, now=now,
                                daily_pnl_observation=observation,
                                calculation_mode=calculation_mode)
        except Exception:  # noqa: BLE001 — 风险评估失败不得影响账户数据本身
            log.warning("风险评估失败 trace=%s（账户快照已正常写入）", trace,
                        exc_info=True)

    def _write_pnl_snapshot(self, account: str, ts: str, now: datetime, raw: dict,
                            run_id: str, *, positions: list, equity,
                            snapshot_id: str | None = None,
                            currency: str | None = None):
        """写账户级 PnL 快照，返回本轮的 DailyPnlObservation（失败时 None）。

        返回观测是为了让 v2 顺序能把它**显式注入**风险评估 ——
        评估侧因此不必再去库里「猜最新一条」，那正是事故成因。

        哨兵值（NaN/max double）已在适配器层过滤成 None。

        ts 与持仓快照相同 —— 二者本就是同一次取数，必须共享时点，
        否则「逐持仓合计 vs 账户 DailyPnL」的对账差异会变成时点噪音。

        **降级**：reqPnL 会成片返回 nan（实测 2026-07-16 ET 09:00–11:35 三项全空，
        订阅正常、无错误码，之后自行恢复；成因未确认）。此时改用仍然可用的
        portfolio.unrealizedPNL + executions.realizedPNL + 净资产变动，
        并把 source/quality 标成 DERIVED/DEGRADED —— 有数字总比空着好，
        但绝不能让它冒充券商 PnL 接口的原值。
        """
        session_et = pnl_calc.pnl_session_date(now, self.cfg.market_tz)
        daily, realized = raw.get("daily_pnl"), raw.get("realized_pnl")
        unrealized = raw.get("unrealized_pnl")
        quality = raw.get("quality") or pnl_calc.pnl_quality(daily, realized, unrealized)
        source = raw.get("source") or pnl_calc.SOURCE_IBKR
        basis = raw.get("daily_basis")
        flow_coverage_ok = None

        if quality == "UNAVAILABLE":
            derived = self._derive_pnl(account, session_et, positions, equity)
            daily = derived["daily_pnl"]
            realized = derived["realized_pnl"]
            unrealized = derived["unrealized_pnl"]
            quality, source, basis = derived["quality"], derived["source"], derived["daily_basis"]
            flow_coverage_ok = derived.get("coverage_ok")

        self.db.insert_pnl_snapshot({
            "account": account, "as_of": ts,
            # 会话日 ≠ 日历日：凌晨拿到的 dailyPnL 属于上一个交易日
            "et_date": session_et,
            "daily_pnl": daily, "realized_pnl": realized, "unrealized_pnl": unrealized,
            "net_liquidation": equity, "source": source, "quality": quality,
            "reset_policy": pnl_calc.RESET_POLICY_IBKR, "sync_run_id": run_id,
            "daily_basis": basis,
        })
        if quality == "UNAVAILABLE":
            log.warning("PnL 不可用且无法降级（无持仓 upl、无前收基准）")
        else:
            log.info("PnL 快照：日内=%s 已实现=%s 未实现=%s（%s / %s，批次 %s）",
                     daily, realized, unrealized, quality, source, run_id[:8])

        # ---- 当日盈亏原子观测（影子期：只落库与比较，不驱动任何事件）----
        # 在这里构建的理由：此刻分子（daily）与分母（equity）来自同一次取数、
        # 共享同一个 ts 与 run_id，是**构造上**的原子配对。风险评估里配不齐 ——
        # 它跑在本函数之前，那时本轮分子还不存在。
        try:
            from . import daily_pnl_service as dps
            obs = dps.build_from_cycle(
                self.cfg, self.db, account=account, ts=ts, session_et=session_et,
                run_id=run_id, snapshot_id=snapshot_id,
                daily_pnl=daily, net_liquidation=equity, source=source,
                basis=basis, quality=quality, currency=currency,
                flow_coverage_ok=flow_coverage_ok, now=now)
            dps.record(self.cfg, self.db, obs, snapshot_id=snapshot_id,
                       threshold=self.cfg.risk_daily_loss_max)
            return obs
        except Exception:  # noqa: BLE001 — 影子计算绝不允许影响同步或告警
            log.warning("当日盈亏观测/影子比较失败（已隔离，不影响同步与告警）",
                        exc_info=True)
            # v2 顺序下拿不到观测就不跑 v2 规则（见 _assess_v2）——
            # 宁可这一轮 ACC_DAILY_LOSS_HIGH 判 insufficient_data，
            # 也不能拿一个来历不明的比值去开关事件
            return None

    def _derive_pnl(self, account: str, session_et: str, positions: list, equity) -> dict:
        """组装降级 PnL 的输入（前收基准、当日已实现、资金流覆盖）。"""
        prev_date = (date.fromisoformat(session_et) - timedelta(days=1)).isoformat()
        prev = self.db.daily_equity_on_or_before(account, prev_date)
        prev_nl = to_decimal(prev["net_liquidation"]) if prev else None

        # 当日已实现：券商权威值（executions.realized_pnl）
        day_start = (datetime.fromisoformat(f"{session_et}T00:00:00")
                     .replace(tzinfo=self.cfg.market_tz).astimezone(timezone.utc).isoformat())
        day_end = ((datetime.fromisoformat(f"{session_et}T00:00:00")
                    .replace(tzinfo=self.cfg.market_tz) + timedelta(days=1))
                   .astimezone(timezone.utc).isoformat())
        realized_rows = [r for r in self.db.executions_between(day_start, day_end)
                         if r["realized_pnl"] is not None]
        today_realized = (sum((to_decimal(r["realized_pnl"]) for r in realized_rows),
                              Decimal("0")) if realized_rows else Decimal("0"))

        flows = pnl_calc.flows_sum(
            {"amount": to_decimal(f["amount"])}
            for f in self.db.list_cash_flows(account, session_et, session_et))
        coverage_ok = pnl_calc.coverage_complete(
            self.db.cash_flow_coverages(account), session_et, session_et)
        return pnl_calc.derive_pnl(
            positions_upl=[p.get("upl") for p in positions],
            today_realized=today_realized, net_liquidation=equity,
            prev_close_nl=prev_nl, flows=flows, coverage_ok=coverage_ok,
        )

    def sync_orders(self, lookback_days: int = 7) -> bool:
        end = datetime.now(timezone.utc)
        start = end - timedelta(days=lookback_days)
        try:
            orders = self.client.fetch_orders(self.account_hash(), start, end)
        except BrokerApiError as exc:
            log.error("订单同步失败: %s", exc)
            self._alert_failure("orders", exc)
            return False
        n = self.db.upsert_orders(orders)
        self.db.set_sync_state("orders_last_sync", end.isoformat())
        log.info("订单同步：upsert %d 条", n)
        return True

    def sync_transactions(self, lookback_days: int = 7) -> bool:
        end = datetime.now(timezone.utc)
        cursor = self.db.get_sync_state("transactions_last_ts")
        start = (
            datetime.fromisoformat(cursor)
            if cursor
            else end - timedelta(days=lookback_days)
        )
        try:
            txns = self.client.fetch_transactions(self.account_hash(), start, end)
        except BrokerApiError as exc:
            log.error("交易同步失败: %s", exc)
            self._alert_failure("transactions", exc)
            return False
        n = self.db.upsert_transactions(txns)
        if txns:
            latest = max(t["ts"] for t in txns)
            self.db.set_sync_state("transactions_last_ts", latest)
        self.db.set_sync_state("transactions_last_sync", end.isoformat())
        log.info("交易同步：upsert %d 条", n)
        return True

    def backfill_from_flex(self, *, days: int | None = None) -> dict:
        """Flex Query 回补：历史成交 + 资金流。

        **为什么必须有**：reqExecutions 只回当日成交，sync 挂一整天那天的成交就
        永久缺失（历史上真挂过 4 天）。Flex 是 IBKR 唯一的历史来源。

        幂等：成交按 exec_id（与实时同步同键），资金流按券商流水号 external_ref。
        资金流是**追加式审计**：已登记过的（无论手工还是 Flex）一律跳过，
        不覆盖、不删除 —— 需要更正请用 cashflow reverse。
        """
        from .flex import FlexClient, parse_flex_cash, parse_flex_trades
        client = FlexClient(self.cfg.flex_token, self.cfg.flex_query_id)
        xml = client.fetch()
        tz = self.cfg.market_tz
        account = self.account_hash()

        trades = parse_flex_trades(xml, tz)
        n_trades = self.db.upsert_executions(trades, account=account) if trades else 0
        if n_trades:
            self._sync_position_plans(account, None, allow_close=False)

        flows = parse_flex_cash(xml, tz)
        n_flows = n_skipped = 0
        for f in flows:
            acct = f["account"] or account
            ref = f["external_ref"]
            if ref and self.db.cash_flow_by_ref(acct, ref):
                n_skipped += 1
                continue                    # 已登记过：绝不覆盖已有审计记录
            self.db.add_cash_flow(
                account=acct, amount=f["amount"], effective_at=f["effective_at"],
                et_date=f["et_date"], currency=f["currency"], note=f["note"],
                source=f["source"], external_ref=ref,
            )
            n_flows += 1
        log.info("Flex 回补：成交 %d 笔（upsert），资金流新增 %d 笔、已存在 %d 笔",
                 n_trades, n_flows, n_skipped)
        return {"trades": n_trades, "flows_added": n_flows,
                "flows_skipped": n_skipped, "flows_total": len(flows)}

    def sync_executions(self) -> bool:
        """2A：盘中 executions 增量同步（仅 IBKR）。

        每轮拉当日全量 → 按 exec_id 幂等 upsert（天然容忍同时间多笔/迟到/重复）。
        不使用时间戳水位；R6 水位在 executions.r6_state。
        返回 True 表示本轮有 executions 数据（触发规则评估）。
        """
        account = self.account_hash()
        try:
            rows = self.client.fetch_executions(account)
        except BrokerApiError as exc:
            log.error("executions 同步失败: %s", exc)
            self._alert_failure("executions", exc)
            return False
        n = self.db.upsert_executions(rows, account=account)
        if n:
            self._sync_position_plans(account, None, allow_close=False)
        self.db.set_sync_state("executions_last_sync", datetime.now(timezone.utc).isoformat())
        if n:
            pending = len([r for r in rows if r["data_status"] == "PENDING_COMMISSION"])
            log.info("executions 同步：upsert %d 条（待佣金 %d）", n, pending)
        return n > 0

    def sync_completed_orders(self) -> bool:
        """当日已完成/撤销/拒绝订单（apiOnly=False，含手动下单）。

        非关键路径：R6/成交口径以 executions 为准。IBKR 对 completedOrders 常不返回
        End 事件而超时，故失败仅记 warning、不发告警（避免刷屏）。
        """
        try:
            rows = self.client.fetch_completed_orders(self.account_hash())
        except BrokerApiError as exc:
            log.warning("completedOrders 同步跳过（非关键）: %s", exc)
            return False
        n = self.db.upsert_orders(rows)
        if n:
            log.info("completedOrders 同步：upsert %d 条", n)
        return n > 0

    def daily_bar_universe(self) -> tuple[list[str], set[str]]:
        """返回日线同步全集及其中的当前持仓标的。

        机会功能关闭时保持原行为；开启后加入活跃自选池与 SPY/QQQ 基准。
        """
        held = {str(row["symbol"]).upper() for row in self.db.latest_positions()
                if row["symbol"]}
        symbols = set(held)
        if self.cfg.opportunity_enabled:
            with self.db.connect() as conn:
                rows = conn.execute(
                    "SELECT symbol FROM opportunity_watchlist WHERE status='ACTIVE' "
                    "ORDER BY symbol LIMIT ?", (self.cfg.opportunity_max_active_symbols,)
                ).fetchall()
            symbols.update(str(r["symbol"]).upper() for r in rows)
            symbols.update({"SPY", "QQQ"})
        return sorted(symbols), held

    def _record_watchlist_bar_sync(self, symbol: str, status: str,
                                   error: str | None = None) -> None:
        if not self.cfg.opportunity_enabled:
            return
        with self.db.connect() as conn:
            conn.execute(
                "UPDATE opportunity_watchlist SET last_sync_status=?,last_sync_at=?,"
                "last_sync_error=? WHERE symbol=?",
                (status, datetime.now(timezone.utc).isoformat(), error, symbol),)

    def sync_daily_bars(self, lookback_days: int | None = None) -> bool:
        """为当前持仓的标的拉日线（ATR/止损/技术面用）。

        每次全量重拉整段并 REPLACE：拆股后 IBKR 会返回调整后的整段价格，
        覆盖旧行即让全段复权一致——EMA250/52 周高点不会横跨拆股价格跳变。
        """
        end = datetime.now(timezone.utc)
        start = end - timedelta(days=lookback_days or self.cfg.daily_bars_lookback_days)
        symbols, held = self.daily_bar_universe()
        if not symbols:
            log.info("无持仓且机会扫描未启用，跳过日线同步")
            return True
        ok = True
        for symbol in symbols:
            # 当前持仓继续全量刷新，确保拆股后历史复权一致；候选日常仅补最近一周，
            # 新标的才完整补历史，避免 100 只候选每天重拉 600 天。
            symbol_start = start
            latest = self.db.latest_bar_date(symbol)
            if symbol not in held and latest:
                try:
                    symbol_start = datetime.fromisoformat(latest).replace(tzinfo=timezone.utc) - timedelta(days=7)
                except ValueError:
                    symbol_start = start
            try:
                bars = self.client.fetch_daily_bars(symbol, symbol_start, end)
            except BrokerApiError as exc:
                log.error("日线同步失败 %s: %s", symbol, exc)
                self._record_watchlist_bar_sync(symbol, "FAILED", _safe_err(exc, 160))
                if symbol in held:
                    self._alert_failure(f"daily_bars_{symbol}", exc)
                ok = False
                continue
            n = self.db.upsert_daily_bars(symbol, bars)
            self._record_watchlist_bar_sync(symbol, "SUCCESS")
            log.info("日线同步 %s：%d 根（%s）", symbol, n,
                     "持仓全量" if symbol in held else ("候选增量" if latest else "候选补历史"))
            time.sleep(2)               # 标的间隔，避免历史请求突发触发 IBKR 分页限速
        return ok

    def ensure_daily_bars_fresh(self, now: datetime | None = None, *, client=None) -> bool:
        """缺了就补：日线落后于最近交易日时补拉。返回 True 表示确实拉到了更新的日线
        （调用方据此重算止损位）。

        正常情况下只查本地 sqlite，不碰 IBKR；只有确认落后才发请求。
        """
        now = now or datetime.now(timezone.utc)
        data_client = client or self.client
        symbols, held = self.daily_bar_universe()
        if not symbols:
            return False
        expected = expected_last_bar_date(now, self.cfg.market_tz).isoformat()
        latest = {s: self.db.latest_bar_date(s) for s in symbols}
        stale = stale_symbols(latest, expected)
        if not stale:
            return False

        state = self.db.get_sync_state(_BARS_ATTEMPT_KEY)
        if not should_attempt(state, expected):
            log.debug("日线补拉已达尝试上限（目标 %s，可能是节假日）：%s", expected, stale)
            return False
        self.db.set_sync_state(_BARS_ATTEMPT_KEY, bump_attempt(state, expected))
        log.info("日线落后（目标交易日 %s）：补拉 %s", expected, "、".join(stale))

        # 持仓全量重拉以保持拆股复权一致；候选已有历史时只补最近一周。
        end = now
        full_start = end - timedelta(days=self.cfg.daily_bars_lookback_days)
        advanced = False
        for symbol in stale:
            symbol_start = full_start
            if symbol not in held and latest.get(symbol):
                try:
                    symbol_start = datetime.fromisoformat(latest[symbol]).replace(
                        tzinfo=timezone.utc) - timedelta(days=7)
                except ValueError:
                    symbol_start = full_start
            try:
                bars = data_client.fetch_daily_bars(symbol, symbol_start, end)
            except BrokerApiError as exc:
                log.error("日线补拉失败 %s: %s", symbol, exc)
                self._record_watchlist_bar_sync(symbol, "FAILED", _safe_err(exc, 160))
                continue                # 非关键：cron 与下一轮补拉都会再试，不告警轰炸
            self.db.upsert_daily_bars(symbol, bars)
            self._record_watchlist_bar_sync(symbol, "SUCCESS")
            after = self.db.latest_bar_date(symbol)
            before = latest[symbol]
            if after and (before is None or after > before):
                advanced = True
                log.info("日线补拉 %s：%s → %s", symbol, before or "无", after)
        return advanced


# ---------- 手动触发同步：并发与频率防护（纯函数，便于单测） ----------

TRIGGER_OK = "ok"
TRIGGER_BUSY = "busy"                 # 已有任务在跑
TRIGGER_TOO_SOON = "too_soon"         # 距上次手动触发太近


def can_trigger_manual_sync(*, running_run, last_manual_at: str | None,
                            now: datetime, min_interval_s: int) -> tuple[str, str]:
    """能否手动触发一次同步。返回 (状态, 人话原因)。

    两道闸：
      1. 已有 running 任务 → 拒绝。单线程执行器下并发同步会让 IB 连接串线，
         且两份快照会用不同 ts 互相覆盖语义。
      2. 距上次手动触发不足 min_interval_s → 拒绝。防手抖连点，
         也避免有人拿它当刷新按钮打爆 IBKR 的请求配额。
    """
    if running_run is not None:
        return TRIGGER_BUSY, "已有同步任务正在运行，请等待其完成"
    if last_manual_at:
        try:
            gap = (now - datetime.fromisoformat(last_manual_at)).total_seconds()
        except (TypeError, ValueError):
            gap = min_interval_s
        if gap < min_interval_s:
            return TRIGGER_TOO_SOON, f"距上次手动同步仅 {gap:.0f} 秒，请 {min_interval_s} 秒后再试"
    return TRIGGER_OK, "可以触发"
