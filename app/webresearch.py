"""规则效果与事件复盘页面数据。

一条展示纪律贯穿全页：**样本不足时不能显示漂亮的绿色高分**。
一个「precision 100%」的绿色徽章，背后如果只有 3 个样本，比不显示更有害 ——
它会让人据此调整真金白银的风控阈值。所以样本不足一律显示灰色的「样本不足」，
并把样本量写在旁边。
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

from . import outcome_labels as ol
from . import review_service as rs
from . import rule_performance as rp
from . import threshold_advisor as ta
from . import trading_calendar as tcal
from .logging_setup import get_logger

log = get_logger()

CONF_CSS = {rp.CONF_STRONG: "conf-strong", rp.CONF_MODERATE: "conf-mod",
            rp.CONF_LOW: "conf-low", rp.CONF_INSUFFICIENT: "conf-none"}
CONF_LABEL = {rp.CONF_STRONG: "强", rp.CONF_MODERATE: "中等",
              rp.CONF_LOW: "弱", rp.CONF_INSUFFICIENT: "样本不足"}
LEAD_LABEL = {rp.CLASS_LEADING: "领先", rp.CLASS_CONCURRENT: "同步",
              rp.CLASS_LAGGING: "滞后", rp.CLASS_NOISY: "噪音",
              rp.CLASS_INSUFFICIENT: "样本不足"}
REC_LABEL = {ta.REC_KEEP: "维持", ta.REC_RAISE: "考虑提高",
             ta.REC_LOWER: "考虑降低", ta.REC_INSUFFICIENT: "样本不足",
             ta.REC_UNSTABLE: "表现不稳定"}
REC_CSS = {ta.REC_KEEP: "rec-keep", ta.REC_RAISE: "rec-raise",
           ta.REC_LOWER: "rec-lower", ta.REC_INSUFFICIENT: "rec-none",
           ta.REC_UNSTABLE: "rec-unstable"}
STATUS_LABEL = {rs.ST_PENDING: "窗口未走完", rs.ST_ELIGIBLE: "部分可算",
                rs.ST_COMPLETED: "已完成", rs.ST_INSUFFICIENT: "数据不足",
                rs.ST_EXCLUDED: "不适用"}


def _d(v):
    if v is None or v == "":
        return None
    if isinstance(v, Decimal):
        return v
    try:
        return Decimal(str(v))
    except (InvalidOperation, ValueError, TypeError):
        return None


def pct(v, digits: int = 1) -> str:
    d = _d(v)
    return "—" if d is None else f"{d:.{digits}%}"


def _jload(v, default):
    try:
        return json.loads(v) if v else default
    except (TypeError, ValueError):
        return default


def decorate_perf(r: dict) -> dict:
    """效果行 -> 展示对象。样本不足时把指标显式抹成「样本不足」。"""
    conf = r.get("confidence") or rp.CONF_INSUFFICIENT
    enough = conf != rp.CONF_INSUFFICIENT
    out = dict(r)
    out.update({
        "conf_css": CONF_CSS.get(conf, "conf-none"),
        "conf_label": CONF_LABEL.get(conf, conf),
        "enough_samples": enough,
        "lead_label": LEAD_LABEL.get(r.get("lead_class"), r.get("lead_class")),
        # 样本不足时不给数字，避免被读成「可信的高分」
        "precision_display": pct(r.get("precision_value")) if enough else "样本不足",
        "recall_display": pct(r.get("recall_value")) if enough else "样本不足",
        "fpr_display": pct(r.get("false_positive_rate")) if enough else "—",
        "score_display": (r.get("composite_score") if enough else "—"),
        "mae_display": pct(r.get("avg_mae")),
        "lead_days_display": _minutes_to_days(r.get("avg_lead_time_minutes")),
        "components": _jload(r.get("score_components"), {}),
    })
    return out


def _minutes_to_days(v) -> str:
    d = _d(v)
    if d is None:
        return "—"
    return f"{d / Decimal(1440):.1f} 天"


def performance_page_data(cfg, db, now: datetime | None = None, *,
                          window: int = 5, label_code: str | None = None,
                          source_type: str | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    rows = db.query_performance(
        observation_window=window, outcome_label=label_code,
        source_type=source_type, version=rp.CALC_VERSION, limit=500)
    perf = [decorate_perf(r) for r in rows]

    latest_run = None
    recs: list[dict] = []
    try:
        latest_run = db.latest_backtest_run()
        if latest_run:
            recs = ta.recommend_all(db, latest_run["backtest_run_id"], window=window,
                                    label_code=label_code)
    except Exception:  # noqa: BLE001
        log.warning("阈值建议读取失败", exc_info=True)

    combos = []
    if latest_run:
        try:
            for c in db.combo_results(latest_run["backtest_run_id"]):
                combos.append({
                    **c, "members": _jload(c.get("member_rules"), []),
                    "member_stats": _jload(c.get("member_stats"), {}),
                    "precision_display": pct(c.get("combo_precision")),
                    "recall_display": pct(c.get("combo_recall")),
                    "recall_delta_display": pct(c.get("recall_delta")),
                    "conf_label": CONF_LABEL.get(c.get("confidence"),
                                                 c.get("confidence")),
                })
        except Exception:  # noqa: BLE001
            pass

    enough = [p for p in perf if p["enough_samples"]]
    from . import phase4_metrics
    account = getattr(cfg, "ib_account", None)
    return {
        "phase4": phase4_metrics.dashboard(db, account=account, window=window),
        "performance": perf,
        "summary": {
            "rules_total": len({p["rule_code"] for p in perf}),
            "rules_with_samples": len({p["rule_code"] for p in enough}),
            "rows": len(perf),
            "window": window,
            "label": label_code or "（全部标签）",
        },
        "recommendations": [{
            **r, "rec_label": REC_LABEL.get(r.get("recommendation")),
            "rec_css": REC_CSS.get(r.get("recommendation"), "rec-none"),
        } for r in recs],
        "rec_summary": _rec_summary(recs),
        "combos": combos,
        "backtest_run": latest_run,
        "windows": list(tcal.FORWARD_WINDOWS),
        "labels": [l.as_dict() for l in ol.default_labels(cfg)],
        "filters": {"window": window, "label": label_code,
                    "source_type": source_type},
        "min_samples": rp.MIN_SAMPLES_FOR_METRICS,
        "data_note": _data_note(db),
    }


def _rec_summary(recs: list[dict]) -> dict:
    out: dict[str, int] = {}
    for r in recs:
        k = r.get("recommendation")
        out[k] = out.get(k, 0) + 1
    return out


def _data_note(db) -> dict:
    """诚实说明当前数据能支撑到什么程度。

    这一段比任何指标都重要 —— 用户需要知道他看到的结论建立在多少数据上。
    """
    try:
        cal = tcal.from_db(db)
        _, total = db.query_reviews(version=rs.CALC_VERSION, limit=1)
        _, completed = db.query_reviews(
            version=rs.CALC_VERSION, review_status=rs.ST_COMPLETED, limit=1)
        _, pending = db.query_reviews(
            version=rs.CALC_VERSION, review_status=rs.ST_PENDING, limit=1)
        _, recommendation_total = db.query_reviews(
            version=rs.CALC_VERSION, source_type="recommendation", limit=1)
        _, recommendation_completed = db.query_reviews(
            version=rs.CALC_VERSION, source_type="recommendation",
            review_status=rs.ST_COMPLETED, limit=1)
        _, recommendation_pending = db.query_reviews(
            version=rs.CALC_VERSION, source_type="recommendation",
            review_status=rs.ST_PENDING, limit=1)
        event_total = total - recommendation_total
        event_completed = completed - recommendation_completed
        event_pending = pending - recommendation_pending
        return {
            "trading_days": len(cal),
            "calendar_start": cal.first, "calendar_end": cal.last,
            "reviews_total": total, "reviews_completed": completed,
            "reviews_pending": pending,
            "event_reviews_total": event_total,
            "event_reviews_completed": event_completed,
            "event_reviews_pending": event_pending,
            "recommendation_reviews_total": recommendation_total,
            "recommendation_reviews_completed": recommendation_completed,
            "recommendation_reviews_pending": recommendation_pending,
            "note": (f"风险事件复盘 {event_total} 个（完成 {event_completed}、"
                     f"待观察 {event_pending}）；recommendation 复盘 {recommendation_total} 个"
                     f"（完成 {recommendation_completed}、待观察 {recommendation_pending}）。"
                     "窗口未走完的样本不参与效果统计 —— "
                     "用半截数据下结论会让 precision 系统性偏高。"),
        }
    except Exception:  # noqa: BLE001
        return {"note": "数据概况读取失败"}


def reviews_page_data(cfg, db, now: datetime | None = None, *,
                      status: str | None = None, rule_code: str | None = None,
                      page: int = 1, per_page: int = 25) -> dict:
    now = now or datetime.now(timezone.utc)
    offset = max(0, (page - 1) * per_page)
    rows, total = db.query_reviews(
        version=rs.CALC_VERSION, review_status=[status] if status else None,
        rule_code=[rule_code] if rule_code else None,
        limit=per_page, offset=offset)
    counts: dict[str, int] = {}
    allrows, _ = db.query_reviews(version=rs.CALC_VERSION, limit=2000)
    for r in allrows:
        k = r.get("review_status") or "unknown"
        counts[k] = counts.get(k, 0) + 1
    return {
        "reviews": [decorate_review(r, cfg.local_tz) for r in rows],
        "total": total, "page": page, "per_page": per_page,
        "pages": max(1, (total + per_page - 1) // per_page),
        "status_counts": counts,
        "status_options": [("", "全部"), (rs.ST_COMPLETED, "已完成"),
                           (rs.ST_ELIGIBLE, "部分可算"), (rs.ST_PENDING, "窗口未走完"),
                           (rs.ST_INSUFFICIENT, "数据不足"),
                           (rs.ST_EXCLUDED, "不适用")],
        "filters": {"status": status, "rule_code": rule_code},
        "data_note": _data_note(db),
    }


def decorate_review(r: dict, tz) -> dict:
    from .webformat import abs_time
    outcomes = _jload(r.get("forward_outcomes"), {})
    labels = _jload(r.get("outcome_labels"), {})
    windows = []
    for n in tcal.FORWARD_WINDOWS:
        w = outcomes.get(str(n)) or {}
        hits = labels.get(str(n)) or {}
        windows.append({
            "n": n,
            "status": w.get("status") or "pending",
            "note": w.get("note"),
            "account_change": pct(w.get("account_change")),
            "account_mae": pct(w.get("account_max_adverse")),
            "account_mfe": pct(w.get("account_max_favorable")),
            "account_dd": pct(w.get("account_max_drawdown")),
            "qqq_return": pct(w.get("qqq_return")),
            "qqq_dd": pct(w.get("qqq_max_drawdown")),
            "position_mae": pct(w.get("position_mae")),
            "position_return": pct(w.get("position_return")),
            "cash_flow_status": w.get("cash_flow_status"),
            "user_acted": w.get("user_acted"),
            # 命中/未命中/无法判定 三态，不合并
            "hits": [{"code": k, "value": v} for k, v in hits.items()],
            "hit_count": sum(1 for v in hits.values() if v is True),
            "undetermined": sum(1 for v in hits.values() if v is None),
        })
    return {
        **r,
        "status_label": STATUS_LABEL.get(r.get("review_status"),
                                         r.get("review_status")),
        "first_abs": abs_time(r.get("first_detected_at"), tz),
        "resolved_abs": (abs_time(r.get("resolved_at"), tz)
                         if r.get("resolved_at") else None),
        "lead_label": {rs.LEAD_LEADING: "领先", rs.LEAD_CONCURRENT: "同步",
                       rs.LEAD_LAGGING: "滞后",
                       rs.LEAD_NO_OUTCOME: "未发生目标结果"}.get(
                           r.get("lead_time_class"), "—"),
        "lead_days": _minutes_to_days(r.get("lead_time_minutes")),
        "duration_display": _minutes_to_days(r.get("duration_minutes")),
        "windows": windows,
        "trading_activity": _jload(r.get("trading_activity"), {}),
        "cash_flow_label": {"clean": "无异常", "estimated": "估算值",
                            "contaminated": "受出入金污染"}.get(
                                r.get("cash_flow_status"), "—"),
    }
