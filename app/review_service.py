"""事件复盘生成：为每个 episode 算出「事前、事中、事后」的完整记录。

复盘资格（review_status）的判定顺序，每一步都可能提前退出：

    excluded          -> 数据质量规则等本就不该用涨跌评价的；写明原因
    insufficient_data -> 缺基础数据（无净值序列、无日线）
    pending           -> 事件太新，最短窗口都没走完
    eligible          -> 部分窗口可算
    completed         -> 全部窗口都已走完

**绝不为了凑样本而放宽资格**。窗口没走完就出结论，等于用半截数据下判断；
而把 pending 当成 completed 会让 precision 系统性偏高（还没跌完就先算成没跌）。
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from decimal import Decimal

from . import event_annotations as ea
from . import forward_outcomes as fo
from . import outcome_labels as ol
from . import trading_calendar as tcal
from .logging_setup import get_logger

log = get_logger()

#: v2 起接入 Flex 权威现金流。**旧版结果保留不删** ——
#: 新旧口径可以并排比较，才能看出「接入券商流水后结论变了多少」。
#: v3 起接入分币种对账与未实现盈亏。**旧版结果全部保留** ——
#: 新旧口径可以并排比较，才能看出接入之后结论变了多少。
CALC_VERSION = "review-v3-attrib/2026-07"
LEGACY_VERSIONS = ("review-v1/2026-07", "review-v2-flex/2026-07")

ST_PENDING = "pending"
ST_ELIGIBLE = "eligible"
ST_COMPLETED = "completed"
ST_INSUFFICIENT = "insufficient_data"
ST_EXCLUDED = "excluded"

# 提前量分类
LEAD_LEADING = "leading"
LEAD_CONCURRENT = "concurrent"
LEAD_LAGGING = "lagging"
LEAD_NO_OUTCOME = "no_outcome"

#: 规则与结果发生在同一个交易日内，算 concurrent 而不是 leading ——
#: 「同时发现」不是「提前预警」，把它算成提前量会系统性高估规则价值。
CONCURRENT_WINDOW_MINUTES = 24 * 60


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _d(v):
    if v is None or v == "":
        return None
    return v if isinstance(v, Decimal) else Decimal(str(v))


def build_review(cfg, db, event: dict, *, calendar: tcal.TradingCalendar,
                 now: datetime | None = None,
                 windows=tcal.FORWARD_WINDOWS) -> dict:
    """为单个事件生成复盘记录（不落库）。"""
    now = now or datetime.now(timezone.utc)
    source = event.get("source_type") or ""
    labels = ol.default_labels(cfg)

    base = {
        "review_id": uuid.uuid4().hex,
        "event_id": event["event_id"],
        "source_type": source,
        "rule_code": event.get("rule_code"),
        "rule_version": event.get("rule_version"),
        "rule_set_version": event.get("rule_set_version"),
        "affected_symbol": event.get("affected_symbol"),
        "affected_instrument": event.get("affected_instrument"),
        "first_detected_at": event.get("first_detected_at"),
        "peak_severity": event.get("max_severity") or event.get("severity"),
        "peak_actual_value": event.get("actual_value"),
        "resolved_at": event.get("resolved_at"),
        "duration_minutes": tcal.minutes_between(
            event.get("first_detected_at"),
            event.get("resolved_at") or now.isoformat()),
        "data_quality_status": event.get("quality_status"),
        "calculation_version": CALC_VERSION,
        "created_at": _now_iso(), "updated_at": _now_iso(),
    }

    # ---- 不适用的规则类型 ----
    if source == "data_quality":
        return {**base, "review_status": ST_EXCLUDED,
                "exclusion_reason": (
                    "数据质量规则的正确性由数据本身证明，用市场涨跌或账户回撤"
                    "评价它没有意义，强行套用只会得到一个看起来很差的假指标"),
                "forward_outcomes": "{}", "outcome_labels": "{}"}

    open_at = event.get("first_detected_at")
    open_day = tcal.et_date_of(open_at, cfg.market_tz)
    if not open_day or not len(calendar):
        return {**base, "review_status": ST_INSUFFICIENT,
                "exclusion_reason": "缺少交易日历或事件时间非法",
                "forward_outcomes": "{}", "outcome_labels": "{}"}

    # ---- 事件发生时的上下文 ----
    ctx = _context_at_open(db, event, open_at)
    base.update(ctx)

    # ---- 各窗口前瞻结果 ----
    outcomes: dict[str, dict] = {}
    label_hits: dict[str, dict] = {}
    completed_windows = 0
    cash_status, cash_note = None, None

    for n in windows:
        end_day = calendar.offset(open_day, n)
        if end_day is None:
            outcomes[str(n)] = {"status": "pending",
                                "note": f"事件之后尚未积累满 {n} 个交易日"}
            continue
        try:
            res = _window_outcome(cfg, db, event, open_at, open_day, end_day,
                                  calendar=calendar)
        except Exception:  # noqa: BLE001 — 单窗口失败不影响其余窗口
            log.warning("窗口结果计算失败 event=%s n=%d",
                        event.get("event_id"), n, exc_info=True)
            outcomes[str(n)] = {"status": "error", "note": "计算失败"}
            continue
        outcomes[str(n)] = res
        completed_windows += 1
        if cash_status is None:
            cash_status = res.get("cash_flow_status")
            cash_note = res.get("cash_flow_note")
        label_hits[str(n)] = ol.evaluate_all(res, source, labels)

    if completed_windows == 0:
        status = ST_PENDING
    elif completed_windows == len(windows):
        status = ST_COMPLETED
    else:
        status = ST_ELIGIBLE

    lead_minutes, lead_class = _lead_time(
        event, outcomes, label_hits, source=source, calendar=calendar,
        open_day=open_day, cfg=cfg)

    return {
        **base,
        "forward_outcomes": json.dumps(outcomes, ensure_ascii=False, default=str),
        "outcome_labels": json.dumps(label_hits, ensure_ascii=False),
        "lead_time_minutes": lead_minutes,
        "lead_time_class": lead_class,
        "cash_flow_status": cash_status,
        "cash_flow_note": cash_note,
        "trading_activity": json.dumps(
            _trading_activity(db, event, open_at, calendar, open_day),
            ensure_ascii=False, default=str),
        "review_status": status,
        "exclusion_reason": None,
    }


def _context_at_open(db, event: dict, open_at: str) -> dict:
    """事件发生**当时**的账户与市场状态。只读当时已存在的评估，不用后来的。"""
    out = {"account_value_at_open": None, "account_value_at_peak": None,
           "account_value_at_resolution": None, "market_state_at_open": None,
           "account_risk_at_open": None, "market_risk_at_open": None,
           "combined_risk_at_open": None}
    try:
        rows = db.equity_series_between(open_at, open_at[:10] + "T23:59:59+00:00")
        if rows:
            out["account_value_at_open"] = str(rows[0].get("equity"))
    except Exception:  # noqa: BLE001
        pass
    try:
        if event.get("assessment_id"):
            a = db.assessment_by_id(event["assessment_id"])
            if a:
                out["account_risk_at_open"] = a.get("risk_score")
            else:
                m = db.market_assessment_by_id(event["assessment_id"])
                if m:
                    out["market_risk_at_open"] = m.get("market_risk_score")
                    out["market_state_at_open"] = m.get("market_state")
    except Exception:  # noqa: BLE001
        pass
    if out["market_state_at_open"] is None:
        try:
            day = str(event.get("data_as_of") or "")[:10]
            rows = db.market_score_series(day, day)
            if rows:
                out["market_state_at_open"] = rows[-1].get("market_state")
                out["market_risk_at_open"] = rows[-1].get("market_risk_score")
        except Exception:  # noqa: BLE001
            pass
    if event.get("resolved_at"):
        try:
            r = db.equity_series_between(event["resolved_at"],
                                         event["resolved_at"][:10] + "T23:59:59+00:00")
            if r:
                out["account_value_at_resolution"] = str(r[0].get("equity"))
        except Exception:  # noqa: BLE001
            pass
    return out


def _flex_covers(db, start_day: str, end_day: str) -> bool:
    """Flex 报表是否覆盖了这个窗口。

    只有覆盖了才用权威流水 —— 报表区间之外「没有流水」不等于「确实没发生流水」，
    拿它当权威会把未覆盖期间的入金静默当成收益。
    """
    try:
        run = db.latest_usable_flex_run()
    except Exception:  # noqa: BLE001
        return False
    if not run:
        return False
    frm, to = run.get("report_from"), run.get("report_to")
    if not (frm and to):
        return False

    def norm(v: str) -> str:
        v = str(v)[:10]
        return f"{v[:4]}-{v[4:6]}-{v[6:8]}" if len(v) == 8 and "-" not in v else v

    return norm(frm) <= start_day and norm(to) >= end_day


def _window_outcome(cfg, db, event: dict, open_at: str, open_day: str,
                    end_day: str, *, calendar) -> dict:
    """单个窗口的账户 + 市场 + 持仓结果。"""
    start_iso = open_at
    end_iso = end_day + "T23:59:59+00:00"

    equity_rows = db.equity_series_between(start_iso, end_iso)
    execs = db.executions_in_window(start_iso, end_iso)

    # 优先用券商权威流水；拿不到才退回启发式，并如实标注来源。
    # 「靠净值跳变猜」和「有券商流水」是两种可信度完全不同的结论，
    # 页面与统计都必须能区分它们。
    account_id = getattr(cfg, "ib_account", None)
    try:
        ext_flows = db.external_flows_between(start_iso, end_iso,
                                              account=account_id)
    except Exception:  # noqa: BLE001
        ext_flows = []
    has_flex = _flex_covers(db, open_day, end_day)
    cash = fo.detect_cash_flow(equity_rows, execs,
                               cash_flows=ext_flows if has_flex else None)

    external_total = None
    if has_flex:
        external_total = sum(
            (fo._d(f.get("amount_base")) or fo._d(f.get("amount")) or Decimal("0")
             for f in ext_flows), Decimal("0"))

    res: dict = {"status": "ok", "window_start": open_day, "window_end": end_day,
                 "cash_flow_status": cash["status"],
                 "cash_flow_note": cash.get("note"),
                 "cash_flow_source": cash.get("source"),
                 "deposit_total": cash.get("deposits"),
                 "withdrawal_total": cash.get("withdrawals"),
                 "external_cash_flow_total": (str(external_total)
                                              if external_total is not None else None)}
    res.update(fo.account_outcome(equity_rows, external_flow=external_total))

    # 现金流严重污染时，账户口径的结果不可信 —— 明确置空而不是留个可疑数字
    if cash["status"] == fo.CF_CONTAMINATED:
        for k in ("account_change", "account_max_adverse", "account_max_favorable",
                  "account_max_drawdown"):
            res[k] = None
        res["account_excluded_reason"] = "窗口内存在疑似大额出入金，账户结果不参与统计"

    # 市场
    res.update(fo.market_outcome(
        db.bars_in_range("QQQ", open_day, end_day),
        db.bars_in_range("SOXX", open_day, end_day),
        db.bars_in_range("VIX", open_day, end_day),
        db.bars_in_range("VXN", open_day, end_day),
        qqq_ma50_at_open=_ma50_at(db, "QQQ", open_day),
        market_states=[r.get("market_state")
                       for r in db.market_score_series(open_day, end_day)] or None))

    # 持仓
    sym = event.get("affected_symbol")
    if sym:
        bars = db.bars_in_range(sym, open_day, end_day)
        pos_execs = db.executions_in_window(start_iso, end_iso, symbol=sym)
        base_qty, end_qty, base_eq = _position_qty(db, sym, start_iso, end_iso,
                                                   equity_rows)
        res.update(fo.position_outcome(
            bars, base_qty=base_qty, end_qty=end_qty, base_equity=base_eq,
            executions=pos_execs))
    return res


def _ma50_at(db, symbol: str, as_of: str) -> Decimal | None:
    """as_of 当日的 50 日均线。**只用该日及之前的数据** —— 防未来数据泄漏。"""
    bars = db.bars_up_to(symbol, as_of, limit=50)
    closes = [b["c"] for b in bars if b.get("c") is not None]
    if len(closes) < 50:
        return None
    return sum(closes, Decimal("0")) / Decimal(len(closes))


def _position_qty(db, symbol: str, start_iso: str, end_iso: str,
                  equity_rows: list[dict]):
    """窗口起止时的持仓数量与期初净值。"""
    base_qty = end_qty = base_eq = None
    try:
        rows = db.positions_between(symbol, start_iso, end_iso)
        if rows:
            base_qty = _d(rows[0].get("qty"))
            end_qty = _d(rows[-1].get("qty"))
    except Exception:  # noqa: BLE001
        pass
    if equity_rows:
        base_eq = _d(equity_rows[0].get("equity"))
    return base_qty, end_qty, base_eq


def _trading_activity(db, event: dict, open_at: str, calendar, open_day: str) -> dict:
    """窗口内的成交摘要。用于识别用户是否主动减仓。"""
    end_day = calendar.offset(open_day, max(tcal.FORWARD_WINDOWS)) or calendar.last
    if not end_day:
        return {}
    execs = db.executions_in_window(open_at, end_day + "T23:59:59+00:00",
                                  symbol=event.get("affected_symbol"))
    return {
        "count": len(execs),
        "symbols": sorted({e.get("symbol") for e in execs if e.get("symbol")}),
        "has_sells": any(str(e.get("side") or "").upper().startswith(("S", "SLD"))
                         for e in execs),
        "has_buys": any(str(e.get("side") or "").upper().startswith(("B", "BOT"))
                        for e in execs),
        "note": ("窗口内存在成交，解读结果时须区分「风险未发生」与"
                 "「用户已采取行动」" if execs else "窗口内无成交"),
    }


def _lead_time(event: dict, outcomes: dict, label_hits: dict, *, source: str,
               calendar, open_day: str, cfg) -> tuple[int | None, str]:
    """提前量：结果首次发生的时间 - 规则首次触发的时间。

    只有结果发生在规则**之后**才算提前预警。同日算 concurrent，早于规则算 lagging，
    从未发生算 no_outcome —— 把 concurrent 记成 leading 会系统性高估规则价值。
    """
    main = ol.default_label_for(source)
    if not main:
        return None, LEAD_NO_OUTCOME
    # 找最早命中主标签的窗口，同时记住它前一个（未命中的）窗口
    ordered = sorted((int(k) for k in label_hits), key=int)
    hit_window = prev_window = None
    for n in ordered:
        if label_hits[str(n)].get(main) is True:
            hit_window = n
            break
        prev_window = n
    if hit_window is None:
        return None, LEAD_NO_OUTCOME

    # 结果发生在 (prev_window, hit_window] 之间。取**下界**作为提前量的保守估计：
    # 用上界（窗口末日）会把提前量算到最大，系统性高估规则价值 ——
    # 命中 1 日窗口只能说明「一天之内出事了」，不能说明「提前了一天预警」。
    anchor_day = (calendar.offset(open_day, prev_window) if prev_window
                  else open_day)
    if not anchor_day:
        return None, LEAD_NO_OUTCOME
    minutes = tcal.minutes_between(event.get("first_detected_at"),
                                   anchor_day + "T16:00:00+00:00")
    if minutes is None:
        return None, LEAD_NO_OUTCOME
    if minutes < 0:
        return minutes, LEAD_LAGGING
    if minutes <= CONCURRENT_WINDOW_MINUTES:
        return minutes, LEAD_CONCURRENT
    return minutes, LEAD_LEADING


# ---------------- 批量生成 ----------------

def generate_reviews(cfg, db, *, now: datetime | None = None,
                     limit: int = 500, force: bool = False) -> dict:
    """为所有事件生成/刷新复盘。**异常全吞** —— 复盘是离线分析，
    它出问题绝不能影响线上风险评估与告警。"""
    now = now or datetime.now(timezone.utc)
    stats = {"scanned": 0, "recommendations_scanned": 0,
             "written": 0, "recommendations_written": 0,
             "pending": 0, "completed": 0,
             "eligible": 0, "excluded": 0, "insufficient": 0, "errors": 0}
    try:
        calendar = tcal.from_db(db)
        rows, _ = db.query_risk_events(limit=limit)
        stats["scanned"] = len(rows)
        for ev in rows:
            try:
                existing = db.review_for_event(ev["event_id"], CALC_VERSION)
                # 已 completed 的不重算（除非强制）：结果不会再变，重算只是浪费
                if existing and not force and existing.get("review_status") == ST_COMPLETED:
                    stats["completed"] += 1
                    continue
                rev = build_review(cfg, db, ev, calendar=calendar, now=now)
                if existing:
                    rev["review_id"] = existing["review_id"]
                    rev["created_at"] = existing.get("created_at") or rev["created_at"]
                db.save_event_review(rev)
                stats["written"] += 1
                key = {ST_PENDING: "pending", ST_COMPLETED: "completed",
                       ST_ELIGIBLE: "eligible", ST_EXCLUDED: "excluded",
                       ST_INSUFFICIENT: "insufficient"}.get(rev["review_status"])
                if key:
                    stats[key] += 1
            except Exception:  # noqa: BLE001
                stats["errors"] += 1
                log.warning("复盘生成失败 event=%s", ev.get("event_id"), exc_info=True)

        # recommendation 复用同一 forward outcome 窗口与现金流口径，但不套用
        # precision/胜率标签。尤其 REDUCE/EXIT 后价格上涨不等于规则失败。
        recs = db.recommendations_for_review(limit=max(limit, 2000))
        stats["recommendations_scanned"] = len(recs)
        for rec in recs:
            ev = _recommendation_event(rec)
            try:
                existing = db.review_for_event(ev["event_id"], CALC_VERSION)
                if existing and not force and existing.get("review_status") == ST_COMPLETED:
                    continue
                rev = build_review(cfg, db, ev, calendar=calendar, now=now)
                if existing:
                    rev["review_id"] = existing["review_id"]
                    rev["created_at"] = existing.get("created_at") or rev["created_at"]
                db.save_event_review(rev)
                stats["recommendations_written"] += 1
            except Exception:  # noqa: BLE001
                stats["errors"] += 1
                log.warning("建议复盘生成失败 recommendation=%s",
                            rec.get("id"), exc_info=True)
    except Exception:  # noqa: BLE001
        log.warning("复盘批量生成异常（线上评估不受影响）", exc_info=True)
    return stats


def _recommendation_event(rec: dict) -> dict:
    """把 recommendation snapshot 映射成 build_review 可消费的只读 source。"""
    status = str(rec.get("status") or "").upper()
    return {
        "event_id": rec["id"],
        "source_type": "recommendation",
        "rule_code": f"REC_{rec.get('action') or 'UNKNOWN'}",
        "rule_version": rec.get("rule_set_version"),
        "rule_set_version": rec.get("rule_set_version"),
        "affected_symbol": rec.get("symbol"),
        "affected_instrument": None,
        "first_detected_at": rec.get("created_at"),
        "resolved_at": (rec.get("last_seen_at")
                        if status in {"SUPERSEDED", "EXPIRED"} else None),
        "max_severity": None,
        "actual_value": rec.get("quantity") or rec.get("delta_weight"),
        "quality_status": ("blocked" if status == "BLOCKED" else "ready"),
    }


def recompute_stats(cfg, db, *, now: datetime | None = None) -> dict:
    """重算后的覆盖情况统计。**纯读取**，不发通知、不改实时事件状态。

    回答一个具体问题：接入权威现金流与未实现盈亏之后，有多少复盘窗口
    真的变得更可信了。
    """
    now = now or datetime.now(timezone.utc)
    stats = {"total": 0, "flex_query": 0, "flex_query_partial": 0,
             "heuristic": 0, "none": 0, "unrealized_available": 0,
             "labels_changed": None, "note": ""}
    try:
        rows, total = db.query_reviews(version=CALC_VERSION, limit=5000)
        stats["total"] = total
        for r in rows:
            src = r.get("cash_flow_source") or "none"
            if src in stats:
                stats[src] += 1
            else:
                stats["none"] += 1
        # 与上一版本的对比（旧结果保留，可并排看）
        prev, prev_total = db.query_reviews(version=LEGACY_VERSIONS[-1], limit=5000)
        stats["previous_version"] = LEGACY_VERSIONS[-1]
        stats["previous_total"] = prev_total
        if total == 0:
            stats["note"] = ("尚无复盘记录 —— 事件仍在积累，观察窗口未走完。"
                             "样本不足时不给结论。")
        else:
            stats["note"] = (f"{stats['flex_query'] + stats['flex_query_partial']}"
                             f"/{total} 个窗口使用了券商权威现金流")
    except Exception:  # noqa: BLE001
        stats["note"] = "统计读取失败"
    return stats
