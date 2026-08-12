"""风险事件中心页面数据。

前端只负责展示，判断全在服务层做完 —— 模板里不该出现任何风险逻辑，
否则「页面看到的」和「通知发出的」会慢慢分叉。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation

from . import risk_events as rev
from . import risk_trends as rt
from .db import Database
from .logging_setup import get_logger

log = get_logger()

#: 严重度 -> CSS 类。图标与文字都给，**不只靠颜色**传达状态。
SEV_CSS = {"critical": "crit", "high": "warn", "warning": "note", "info": "muted"}
SEV_ICON = {"critical": "🔴", "high": "🟠", "warning": "🟡", "info": "🔵"}
STATUS_ICON = {"open": "●", "acknowledged": "✔", "resolved": "✓", "suppressed": "🔕"}
STATUS_CSS = {"open": "st-open", "acknowledged": "st-ack",
              "resolved": "st-resolved", "suppressed": "st-suppressed"}


def _pct_or_raw(value: str | None, unit: str | None) -> str:
    """比例按百分比显示，其余原样。数值本身在库里是字符串，这里只做展示。"""
    if value is None or value == "":
        return "—"
    if unit == "ratio":
        try:
            return f"{Decimal(str(value)):.1%}"
        except (InvalidOperation, ValueError, TypeError):
            return str(value)
    try:
        d = Decimal(str(value))
        return f"{d:,.2f}".rstrip("0").rstrip(".") if d % 1 else f"{d:,.0f}"
    except (InvalidOperation, ValueError, TypeError):
        return str(value)


def decorate(row: dict, tz) -> dict:
    """事件行 -> 模板可直接用的展示对象。"""
    sev = row.get("severity") or "warning"
    status = row.get("status") or "open"
    from .webformat import abs_time, rel_time
    now = datetime.now(timezone.utc)
    out = dict(row)
    out.update({
        "sev_css": SEV_CSS.get(sev, "muted"),
        "sev_icon": SEV_ICON.get(sev, "⚪"),
        "sev_label": rev.severity_label(sev),
        "status_css": STATUS_CSS.get(status, ""),
        "status_icon": STATUS_ICON.get(status, "●"),
        "status_label": rev.status_label(status),
        "source_label": rev.source_label(row.get("source_type")),
        "actual_display": _pct_or_raw(row.get("actual_value"), row.get("unit")),
        "threshold_display": _pct_or_raw(row.get("threshold_value"), row.get("unit")),
        "affected": row.get("affected_symbol") or row.get("affected_instrument") or "—",
        "first_abs": abs_time(row.get("first_detected_at"), tz),
        "last_abs": abs_time(row.get("last_detected_at"), tz),
        "last_rel": rel_time(row.get("last_detected_at"), now),
        "resolved_abs": (abs_time(row.get("resolved_at"), tz)
                         if row.get("resolved_at") else None),
        # 归 legacy 的事件在页面上要标明「通知走既有链路」，否则用户会以为漏发了
        "notify_note": ("通知由 R1–R7 链路发出"
                        if (row.get("notification_owner") == "legacy") else None),
    })
    return out


def _duration(since: str | None, now: datetime) -> str:
    if not since:
        return "—"
    try:
        t = datetime.fromisoformat(since)
    except (TypeError, ValueError):
        return "—"
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)
    secs = int((now - t).total_seconds())
    if secs < 3600:
        return f"{secs // 60} 分钟"
    if secs < 86400:
        return f"{secs // 3600} 小时"
    return f"{secs // 86400} 天"


def summary(db: Database, now: datetime, tz) -> dict:
    """顶部风险摘要。"""
    counts = db.event_counts_by_severity()
    day_start = (now.astimezone(tz).replace(hour=0, minute=0, second=0, microsecond=0)
                 .astimezone(timezone.utc))
    oldest = db.oldest_unresolved_event()
    return {
        "open_critical": counts.get("critical", 0),
        "open_high": counts.get("high", 0),
        "open_warning": counts.get("warning", 0),
        "open_total": sum(counts.values()),
        "opened_today": db.events_opened_between(day_start.isoformat(),
                                                 now.isoformat()),
        "resolved_today": db.events_resolved_between(day_start.isoformat(),
                                                     now.isoformat()),
        "oldest": (None if not oldest else {
            **decorate(oldest, tz),
            "age": _duration(oldest.get("first_detected_at"), now)}),
    }


def trend_series(db: Database, range_key: str, now: datetime) -> dict:
    """趋势图数据。

    账户分与市场分虽然都是 0–100，但**含义不同**（一个是我的账户有多危险，
    一个是环境有多不友好），放同一张图必须各自标注，不能默认读者会自己区分。
    放大系数量纲完全不同，单独一栏。
    """
    s, e, iv, key = rt.resolve_range(range_key, None, None, None, now=now)
    acc = rt.aggregate_account(
        db.risk_score_series(s.isoformat(), e.isoformat()), iv)
    mkt = rt.aggregate_market(
        db.market_score_series(s.date().isoformat(), e.date().isoformat()), iv)
    amp = rt.amplification_series(acc, mkt)
    gaps = rt.find_gaps(acc, s, e, iv)

    def pts(buckets, key_name):
        out = []
        for b in buckets:
            v = (b.get(key_name) or {}).get("last")
            mx = (b.get(key_name) or {}).get("max")
            out.append({"t": b["bucket"], "v": v, "max": mx,
                        "level": b.get("highest_risk_level")
                        or b.get("highest_market_state")})
        return out

    return {
        "range": key, "interval": iv,
        "start": s.isoformat(), "end": e.isoformat(),
        "account": pts(acc, "account_risk_score"),
        "market": pts(mkt, "market_risk_score"),
        "amplification": amp,
        "gaps": gaps, "has_gaps": bool(gaps),
        "interval_label": {"raw": "原始（分钟级）", "1h": "1 小时", "6h": "6 小时",
                           "1d": "1 天"}.get(iv, iv),
    }


def sparkline_points(points: list[dict], *, width: int = 560,
                     height: int = 90) -> str:
    """内联 SVG 折线的点串。没有外部依赖，也不需要前端图表库。"""
    vals = []
    for p in points:
        try:
            vals.append(Decimal(str(p["v"])) if p.get("v") is not None else None)
        except (InvalidOperation, ValueError, TypeError):
            vals.append(None)
    known = [v for v in vals if v is not None]
    if len(known) < 2:
        return ""
    lo, hi = min(known), max(known)
    span = (hi - lo) or Decimal("1")
    n = len(vals) - 1 or 1
    out = []
    for i, v in enumerate(vals):
        if v is None:
            continue
        x = Decimal(i) * Decimal(width) / Decimal(n)
        y = Decimal(height) - (v - lo) / span * Decimal(height - 8) - Decimal(4)
        out.append(f"{x:.1f},{y:.1f}")
    return " ".join(out)


def events_page_data(cfg, db: Database, now: datetime | None = None, *,
                     range_key: str = "7d", status: str | None = None,
                     source_type: str | None = None, severity: str | None = None,
                     page: int = 1, per_page: int = 25) -> dict:
    """风险事件中心页面全部数据。"""
    now = now or datetime.now(timezone.utc)
    tz = cfg.local_tz
    try:
        db.expire_suppressions(now.isoformat())
    except Exception:  # noqa: BLE001
        log.warning("抑制到期结算失败", exc_info=True)

    offset = max(0, (page - 1) * per_page)
    rows, total = db.query_risk_events(
        status=[status] if status else None,
        source_type=[source_type] if source_type else None,
        severity=[severity] if severity else None,
        limit=per_page, offset=offset)

    trends = trend_series(db, range_key, now)
    return {
        "summary": summary(db, now, tz),
        "trends": trends,
        "account_spark": sparkline_points(trends["account"]),
        "market_spark": sparkline_points(trends["market"]),
        "events": [decorate(r, tz) for r in rows],
        "total": total, "page": page, "per_page": per_page,
        "pages": max(1, (total + per_page - 1) // per_page),
        "filters": {"status": status, "source_type": source_type,
                    "severity": severity, "range": range_key},
        "status_options": [("", "全部"), ("open", "进行中"),
                           ("acknowledged", "已确认"), ("suppressed", "已抑制"),
                           ("resolved", "已恢复")],
        "source_options": [("", "全部来源"), ("account", "账户"),
                           ("position", "持仓"), ("market", "市场"),
                           ("combined", "联合"), ("data_quality", "数据质量"),
                           ("behavior", "行为纪律")],
        "severity_options": [("", "全部级别"), ("critical", "严重"),
                             ("high", "高风险"), ("warning", "警告")],
        "range_options": [("24h", "24 小时"), ("7d", "7 天"),
                          ("30d", "30 天"), ("90d", "90 天")],
    }


def _annotations(db, event_id: str) -> dict:
    """事件标注。**不隐藏也不删除事件本身** —— 只是把「这个边界是人为的」
    说清楚，并让研究层据此排除样本。"""
    from . import event_annotations as ea
    try:
        anns = db.event_annotations(event_id)
    except Exception:  # noqa: BLE001
        anns = []
    probe = [a for a in anns if a["annotation_type"] == ea.T_PROBE_INDUCED]
    notif = []
    for a in anns:
        if a.get("related_notification_id"):
            try:
                notif.extend(db.notification_classifications(
                    a["related_notification_id"]))
            except Exception:  # noqa: BLE001
                pass
    return {
        "items": anns, "count": len(anns),
        "has_probe_induced": bool(probe),
        "notice": ea.PROBE_NOTICE if probe else None,
        "excluded_from_research": any(a["excluded_from_research"] for a in anns),
        "related_notifications": notif,
    }


def event_detail_data(cfg, db: Database, event_id: str,
                      now: datetime | None = None) -> dict | None:
    now = now or datetime.now(timezone.utc)
    tz = cfg.local_tz
    row = db.get_risk_event(event_id)
    if not row:
        return None
    from .webformat import abs_time
    transitions = []
    for t in db.event_transitions(event_id):
        transitions.append({
            **t, "at_abs": abs_time(t.get("transitioned_at"), tz),
            "sev_from": rev.severity_label(t.get("previous_severity")),
            "sev_to": rev.severity_label(t.get("new_severity")),
            "status_from": rev.status_label(t.get("previous_status")),
            "status_to": rev.status_label(t.get("new_status")),
        })
    notifications = []
    for n in db.notifications_for_event(event_id):
        notifications.append({
            **n, "at_abs": abs_time(n.get("attempted_at"), tz),
            "ok": bool(n.get("success")),
            "type_label": {"opened": "新增", "escalated": "升级",
                           "reminder": "提醒", "recovered": "恢复",
                           "blocked": "评估受阻", "test": "测试"}.get(
                               n.get("notification_type"), n.get("notification_type")),
        })
    rule = None
    try:
        rule = db.rule_definition(row.get("rule_code"), row.get("rule_set_version"))
    except Exception:  # noqa: BLE001
        rule = None
    return {
        "event": decorate(row, tz),
        "transitions": transitions,
        "notifications": notifications,
        "rule": rule,
        "duration": _duration(row.get("first_detected_at"), now),
        # 标注只是叠加说明 —— 事件本身既不删除也不隐藏
        "annotations": _annotations(db, event_id),
    }
