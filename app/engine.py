"""规则引擎集成层：从 DB 组装快照 → 跑纯规则 → 去重推送 + 记录 + 冷静期。

把「取数/时间/IO」都放这里，纯逻辑留在 rules.py / stop_loss.py，方便测试。
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from .config import Config
from .db import Database, to_decimal
from . import alerting
from .counting import trading_counts
from .decisions import merge_realized_by_decision
from .indicators import IndicatorError
from .logging_setup import get_logger
from .notify import Notifier
from .rules import (
    RuleContext, RuleStatus, RuleThresholds, RULES_VERSION, evaluate_ruleset,
)
from .sectors import load_sector_map
from .stop_loss import compute_stop

log = get_logger()

_DEDUP_WINDOW = timedelta(hours=4)


def _et_day_start_utc(now: datetime, tz) -> str:
    et = now.astimezone(tz)
    start = et.replace(hour=0, minute=0, second=0, microsecond=0)
    return start.astimezone(timezone.utc).isoformat()


def _et_date_of_iso(ts_iso: str, tz) -> str:
    """UTC ISO -> 美东交易日。所有日期边界一律 ET，不用 UTC 自然日。"""
    return datetime.fromisoformat(ts_iso).astimezone(tz).date().isoformat()


def _et_month_start_utc(now: datetime, tz) -> str:
    et = now.astimezone(tz)
    start = et.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return start.astimezone(timezone.utc).isoformat()


@dataclass
class ContextMeta:
    """本次规则评估实际使用的数据批次（写入 rule_runs 供页面互证）。"""
    sync_run_id: str | None
    positions_snapshot_id: str | None
    balances_snapshot_id: str | None
    quotes_as_of: str | None
    quote_quality_summary: dict


def _fmt_local(ts_iso: str, tz) -> str:
    """通知里的时间也用本地时区（与看板一致），不标时区后缀。"""
    try:
        return datetime.fromisoformat(ts_iso).astimezone(tz).strftime("%Y-%m-%d %H:%M")
    except (TypeError, ValueError):
        return str(ts_iso)


def _duration_text(since_iso: str, now: datetime) -> str:
    try:
        secs = (now - datetime.fromisoformat(since_iso)).total_seconds()
    except (TypeError, ValueError):
        return "未知时长"
    if secs < 3600:
        return f"{int(secs // 60)} 分钟"
    if secs < 86400:
        return f"{secs / 3600:.1f} 小时"
    return f"{secs / 86400:.1f} 天"


class RuleEngine:
    def __init__(self, cfg: Config, db: Database, notifier: Notifier,
                 thresholds: RuleThresholds | None = None,
                 alert_policy: "alerting.AlertPolicy | None" = None):
        self.cfg = cfg
        self.db = db
        self.notifier = notifier
        self.thresholds = thresholds or RuleThresholds()
        self.alert_policy = alert_policy or alerting.AlertPolicy(
            worsen_abs=cfg.alert_worsen_abs, worsen_rel=cfg.alert_worsen_rel,
            retrigger_cooldown=timedelta(minutes=cfg.alert_retrigger_cooldown_min))

    # ---------- 止损引擎（每日收盘后） ----------

    def compute_stop_levels(self, now: datetime | None = None) -> int:
        now = now or datetime.now(timezone.utc)
        computed_date = now.astimezone(self.cfg.market_tz).date().isoformat()
        balances = self.db.latest_balances()
        equity = to_decimal(balances["equity"]) if balances else Decimal("0")
        positions = self.db.latest_positions()
        n = 0
        for p in positions:
            symbol = p["symbol"]
            qty = to_decimal(p["qty"])
            if qty == 0:
                continue
            bars = self.db.get_daily_bars(symbol, limit=60)
            try:
                adv = compute_stop(
                    {"symbol": symbol, "qty": qty, "avg_cost": to_decimal(p["avg_cost"])},
                    bars, equity, allow_degraded=True,
                )
            except IndicatorError as exc:
                log.warning("止损计算跳过 %s：%s", symbol, exc)
                continue
            if not adv.data_sufficient:
                log.info("止损降级 %s：%s", symbol, adv.data_note)
            self.db.upsert_stop_level(computed_date, {
                "symbol": adv.symbol, "side": adv.side, "close": adv.close,
                "atr": adv.atr, "swing": adv.swing, "stop_price": adv.stop_price,
                "risk_amount": adv.risk_amount, "risk_pct": adv.risk_pct,
                "exceeds_2pct": adv.exceeds_2pct, "suggested_trim": adv.suggested_trim_shares,
                "reached_1r": adv.reached_1r, "trailing_stop": adv.trailing_stop,
                "take_profit": adv.take_profit_hint, "atr_period": adv.atr_period,
                "bars_used": adv.bars_used, "data_sufficient": adv.data_sufficient,
                "data_note": adv.data_note,
            })
            n += 1
        log.info("止损位已计算 %d 个标的（%s）", n, computed_date)
        return n

    # ---------- 组装规则上下文 ----------

    def build_rule_context(self, now: datetime, pending_execs=None) -> RuleContext:
        """向后兼容入口：只要 ctx。需要快照溯源的调用方用 build_context_with_meta。"""
        return self.build_context_with_meta(now, pending_execs)[0]

    def build_context_with_meta(self, now: datetime, pending_execs=None):
        tz = self.cfg.market_tz
        balances = self.db.latest_balances()
        equity = to_decimal(balances["equity"]) if balances else Decimal("0")
        margin = (to_decimal(balances["margin_balance"])
                  if balances and balances["margin_balance"] is not None else None)

        pos_rows = self.db.latest_positions()
        account_ready = bool(pos_rows) or (equity is not None and equity > 0)
        positions: list[dict] = []
        current_prices: dict[str, Decimal] = {}
        price_quality: dict[str, str] = {}
        quote_as_ofs: list[str] = []
        quality_counts: dict[str, int] = {}
        for r in pos_rows:
            sym = r["symbol"]
            qty = to_decimal(r["qty"])
            mv = to_decimal(r["market_value"])
            positions.append({
                "symbol": sym, "qty": qty, "avg_cost": to_decimal(r["avg_cost"]),
                "market_value": mv, "upl": to_decimal(r["upl"]),
            })
            # 现价只认行情快照。**禁止**回退 market_value/qty —— 那是账面价格、
            # 没有质量标记，且与 close 并非同一时点（实测 KMEM: mv/qty=19.00 vs close=19.14），
            # 正是首页与 R7 各说各话的根源。
            snap_price = to_decimal(r["price"]) if r["price"] is not None else None
            if snap_price is not None and snap_price > 0:
                current_prices[sym] = snap_price
            q = r["price_quality"]
            if q:
                price_quality[sym] = q
                quality_counts[q] = quality_counts.get(q, 0) + 1
            if r["quote_as_of"]:
                quote_as_ofs.append(r["quote_as_of"])

        meta = ContextMeta(
            sync_run_id=(pos_rows[0]["sync_run_id"] if pos_rows else None),
            positions_snapshot_id=(pos_rows[0]["ts"] if pos_rows else None),
            balances_snapshot_id=(balances["ts"] if balances else None),
            # 全部走收盘价回退时无可信行情时点 -> None（不拿 ticker.time 冒充）
            quotes_as_of=(min(quote_as_ofs) if quote_as_ofs else None),
            quote_quality_summary=quality_counts,
        )
        # 当日/本月计数
        day_start = _et_day_start_utc(now, tz)
        month_start = _et_month_start_utc(now, tz)
        filled_today_rows = self.db.filled_orders_since(day_start)
        filled_today: dict[str, int] = {}
        for o in filled_today_rows:
            if o["symbol"]:
                filled_today[o["symbol"]] = filled_today.get(o["symbol"], 0) + 1
        filled_month_total = len(self.db.filled_orders_since(month_start))

        # ---- decision 口径（IBKR）----
        # orders 表拿不到手动单（reqCompletedOrders 始终超时），R4/R5 若继续数它
        # 会永远是 0 —— 那不是「没触发」，是「从没数到过」。改从 executions 还原决策。
        risk_increasing_today = None
        decisions_month_total = None
        classification_complete = True
        kind_summary: dict[str, int] = {}
        # 与首页/历史页共用同一个统计函数 —— 三个页面绝不能各算各的
        counts = trading_counts(
            self.db, tz, month_start_iso=month_start,
            today_et=now.astimezone(tz).date().isoformat(),
        )
        risk_increasing_today = counts.risk_increasing_today
        decisions_month_total = counts.decisions_month
        classification_complete = counts.classification_complete_today
        kind_summary = counts.kinds_month

        # 已实现亏损事件：executions（盘中增量，水位在 r6_state，非时间戳）
        # 合并判定要看该决策的全部成交，而不只是本轮新到的几笔
        rows = self.db.r6_candidate_executions() if (
            pending_execs is None or pending_execs) else []
        # **按 decision_key 合并**：一次认亏被券商拆成多笔部分成交时，
        # 逐笔比阈值会让每笔都躲过 2%（线上实测：清仓 KMEM 拆成 7 笔，
        # 单笔最大 1.82%，合计 5.4%，R6 一声没响）。R6 的「单笔」指的是
        # 一次认亏决策，不是一条成交回报。
        realized_events = merge_realized_by_decision(rows) if rows else []

        # 止损位
        stop_levels = {
            r["symbol"]: {
                "side": r["side"],
                "stop_price": to_decimal(r["stop_price"]),
                # 日线不足算出的止损位仅供参考 → R7 只降级告警，不发 CRIT
                "data_sufficient": bool(r["data_sufficient"]),
                "atr_period": r["atr_period"],
                "atr": to_decimal(r["atr"]),      # 滞回带宽度 = atr × r7_hysteresis_atr
            }
            for r in self.db.latest_stop_levels()
        }
        # R7 当前触发态：滞回判定要知道「上一轮是不是已经在告警」
        r7_active = {
            k.split(":", 1)[1] for k in self.db.active_alert_keys()
            if k.startswith("R7:") and ":" in k
        }

        # 冷静期
        cd = self.db.active_cooldown(now.isoformat())
        cooldown_active = cd is not None
        cooldown_ends_at = datetime.fromisoformat(cd["ends_at"]) if cd else None
        new_fills_in_cooldown = []
        if cd:
            for o in self.db.filled_orders_since(cd["started_at"]):
                new_fills_in_cooldown.append({"symbol": o["symbol"], "ref_id": o["order_id"]})

        ctx = RuleContext(
            now=now, equity=equity, margin_balance=margin, positions=positions,
            sector_map=load_sector_map(self.cfg.sector_map_path),
            filled_today=filled_today, filled_month_total=filled_month_total,
            risk_increasing_today=risk_increasing_today,
            decisions_month_total=decisions_month_total,
            decision_classification_complete=classification_complete,
            decision_kind_summary=kind_summary,
            realized_events=realized_events, stop_levels=stop_levels,
            current_prices=current_prices, price_quality=price_quality,
            r7_active=r7_active,
            cooldown_active=cooldown_active, cooldown_ends_at=cooldown_ends_at,
            new_fills_in_cooldown=new_fills_in_cooldown, account_ready=account_ready,
        )
        return ctx, meta

    # ---------- 评估 + 派发 ----------

    def evaluate_and_dispatch(self, now: datetime | None = None) -> list:
        now = now or datetime.now(timezone.utc)
        # 2A：本轮待判 executions（评估后统一标记 DONE，保证同一 execution 不重复触发）
        pending_execs = self.db.pending_r6_executions()
        ctx, meta = self.build_context_with_meta(now, pending_execs=pending_execs)
        result = evaluate_ruleset(ctx, self.thresholds)

        self._dispatch_alerts(now, result)

        # 2A：R6 已正常评估（TRIGGERED/OK）→ 标记本轮 executions 已判，避免重复触发。
        # INSUFFICIENT_DATA/ERROR 时不标记，留待下轮重评。
        if pending_execs:
            r6 = result.by_rule().get("R6")
            if r6 and r6.status in (RuleStatus.TRIGGERED, RuleStatus.OK):
                self.db.mark_r6_done([r["exec_id"] for r in pending_execs])

        self._record_run(now, ctx, result, meta)
        return result.hits

    def _dispatch_alerts(self, now: datetime, result) -> None:
        """按告警生命周期推送：状态变化才通知，持续存在每日一次摘要。

        评估记录（rule_runs / rule_events）不受影响 —— 少发的是通知，不是审计。
        原先固定 4h 去重窗口会让 R1/R2 这类长期风险每天叫 6 次，读的人只会关掉通知。
        """
        et_date = now.astimezone(self.cfg.market_tz).date().isoformat()
        seen: set[str] = set()
        for hit in result.hits:
            seen.add(hit.dedup_key)
            # R6 触发冷静期（与推送解耦，避免漏建）
            if hit.triggers_cooldown_hours and not self.db.cooldown_exists_for(hit.dedup_key):
                ends = now + timedelta(hours=hit.triggers_cooldown_hours)
                self.db.start_cooldown(now.isoformat(), ends.isoformat(), hit.title, hit.dedup_key)
                log.info("进入冷静期至 %s（%s）", ends.isoformat(), hit.dedup_key)

            state = self.db.get_alert_state(hit.dedup_key)
            should, reason = alerting.decide_notify(
                state, severity=hit.severity, value=hit.value, now_et_date=et_date,
                rule_id=hit.rule_id, now=now, policy=self.alert_policy,
            )
            if should:
                title = hit.title
                if reason == alerting.DAILY_SUMMARY:
                    since = state["first_triggered_at"] if state else now.isoformat()
                    title = f"{hit.title}（持续中 · 每日摘要）"
                    msg = f"{hit.message}\n此风险自 {_fmt_local(since, self.cfg.local_tz)} 起持续存在。"
                elif reason == alerting.ESCALATION:
                    title = f"{hit.title}（严重度升级）"
                    msg = hit.message
                elif reason == alerting.WORSENED:
                    title = f"{hit.title}（实际值恶化）"
                    prev = state["notified_value"] if state else None
                    msg = f"{hit.message}\n上次通知时为 {prev}，已显著恶化。"
                elif reason == alerting.RETRIGGER:
                    title = f"{hit.title}（恢复后再次触发）"
                    msg = hit.message
                else:
                    msg = hit.message
                msg = f"{msg}\n规则引用：{hit.rule_id}"
                self.notifier.send(title, msg)
                self.db.record_rule_event(hit.rule_id, hit.severity, hit.symbol, title, msg)
                log.info("告警通知 %s：%s", hit.dedup_key, alerting.reason_label(reason))
            # notified=True 时同步推进「今天已说过」水位：首次触发当天不该再补一条日摘要
            self.db.upsert_alert_active(
                dedup_key=hit.dedup_key, rule_id=hit.rule_id, symbol=hit.symbol,
                severity=hit.severity, value=hit.value, now_iso=now.isoformat(),
                notified=should, et_date=et_date,
            )

        # 恢复通知：上轮 ACTIVE、本轮不再触发
        for key in self.db.active_alert_keys():
            if key in seen:
                continue
            state = self.db.get_alert_state(key)
            if alerting.decide_resolve(state):
                title = f"✅ 风险已解除：{state['rule_id']}"
                sym = state["symbol"]
                msg = (f"{state['rule_id']}{('（' + sym + '）') if sym else ''} "
                       f"已恢复正常，该风险自 "
                       f"{_fmt_local(state['first_triggered_at'], self.cfg.local_tz)} 起持续了 "
                       f"{_duration_text(state['first_triggered_at'], now)}。")
                msg = f"{msg}\n规则引用：{state['rule_id']}"
                self.notifier.send(title, msg)
                self.db.record_rule_event(state["rule_id"], "INFO", sym, title, msg)
                log.info("告警恢复 %s", key)
            self.db.resolve_alert(key, now.isoformat())

    def _record_run(self, now, ctx, result, meta: "ContextMeta | None" = None) -> None:
        """审计：每次评估都追加一条 rule_run（含未触发/数据不足/异常）。

        meta 记录本次评估实际读到的快照批次 —— 页面据此可以证明自己与规则引擎
        看的是同一批数据，而不是各读各的。
        """
        meta = meta or ContextMeta(None, None, None, None, {})
        data_sources = {
            "positions_as_of": meta.positions_snapshot_id,
            "balances_as_of": meta.balances_snapshot_id,
            "quotes_as_of": meta.quotes_as_of,
            "sync_run_id": meta.sync_run_id,
            "orders_last_sync": self.db.get_sync_state("orders_last_sync"),
        }
        evaluations = [
            {"rule": e.rule_id, "status": e.status.value, "hits": len(e.hits),
             "detail": e.detail, "counts": e.counts,
             # 该规则触发时的最高严重度（供看板「未处理告警」分级统计）
             "severity": ("CRIT" if any(h.severity == "CRIT" for h in e.hits) else "WARN")}
            for e in result.evaluations
        ]
        per_symbol = {e.rule_id: e.per_symbol for e in result.evaluations if e.per_symbol}
        self.db.record_rule_run(
            run_ts=now.isoformat(), run_status=result.run_status.value,
            snapshot_as_of=meta.positions_snapshot_id, account_ready=ctx.account_ready,
            data_sources=json.dumps(data_sources, ensure_ascii=False),
            evaluations=json.dumps(evaluations, ensure_ascii=False),
            rules_version=RULES_VERSION, thresholds_version=self.thresholds.version,
            sync_run_id=meta.sync_run_id,
            positions_snapshot_id=meta.positions_snapshot_id,
            balances_snapshot_id=meta.balances_snapshot_id,
            quotes_as_of=meta.quotes_as_of,
            quote_quality_summary=json.dumps(meta.quote_quality_summary, ensure_ascii=False),
            rule_summary=json.dumps(result.summary, ensure_ascii=False),
            per_symbol=json.dumps(per_symbol, ensure_ascii=False),
        )
