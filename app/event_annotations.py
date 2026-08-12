"""事件标注与研究排除。

原则只有一条：**原始记录只增不改。**

一次生产探测让 7 条仍在触发的事件被误判恢复、随即重开，并误发了一条告警。
删掉那段历史比留着更糟 —— 那会让「当时到底发生了什么」永远说不清，
也会让下一个人重蹈覆辙。所以：

  · risk_event_registry / risk_event_transitions / risk_notifications 一行不动
  · 标注是**追加**的另一张表
  · 通知的 success 保持为 1 —— 它在技术上确实发送成功了，
    改成 failed 是另一种伪造。分类是叠加的事实，不是对原记录的修正
  · 研究层默认排除被标注的样本，但可以显式 include_excluded=true 查回来
  · 研究视图可以把被人为切断的 episode 合并回一段连续区间，
    但**只在视图层合并，绝不回写原始数据**
"""
from __future__ import annotations

import hashlib
import uuid
from datetime import datetime, timezone

# ---------------- 标注类型 ----------------
T_PROBE_INDUCED = "probe_induced_transition"
T_OPERATOR_ERROR = "operator_error"
T_PIPELINE_GAP = "data_pipeline_gap"
T_KNOWN_FP = "known_false_positive"
T_RESEARCH_EXCLUSION = "research_exclusion"
T_INFO = "informational_note"

ANNOTATION_TYPES = (T_PROBE_INDUCED, T_OPERATOR_ERROR, T_PIPELINE_GAP,
                    T_KNOWN_FP, T_RESEARCH_EXCLUSION, T_INFO)

# ---------------- 来源 ----------------
ORIGIN_PROBE = "production_probe"
ORIGIN_OPERATOR = "operator"
ORIGIN_SYSTEM = "system"

# ---------------- 通知分类 ----------------
C_PROBE_FALSE_ALERT = "probe_induced_false_alert"

#: 给用户看的统一说明。事件详情页与研究页都用这句。
PROBE_NOTICE = ("该事件边界由生产探测引起，不代表真实风险恢复，"
                "默认不纳入规则效果研究。")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def annotation_id(*, risk_event_id: str, transition_id, incident: str | None) -> str:
    """确定性 id —— 同一事件 + 同一 transition + 同一事故永远算出同一个 id，
    这是幂等的基础（重复标注不会产生第二条）。"""
    raw = f"{risk_event_id}|{transition_id if transition_id is not None else '-'}|{incident or ''}"
    return "ann-" + hashlib.sha256(raw.encode()).hexdigest()[:20]


def annotate_event(db, *, risk_event_id: str, annotation_type: str,
                   origin: str, reason_code: str, reason_text: str,
                   transition_id=None, episode_sequence=None,
                   excluded_from_research: bool = False,
                   excluded_from_precision_metrics: bool = False,
                   related_notification_id: str | None = None,
                   incident_reference: str | None = None,
                   evidence: dict | None = None,
                   created_by: str = "system",
                   now: datetime | None = None) -> dict:
    """追加一条标注。**幂等** —— 重复调用不会产生第二条。"""
    if annotation_type not in ANNOTATION_TYPES:
        raise ValueError(f"未知标注类型：{annotation_type}")
    now = now or _now()
    aid = annotation_id(risk_event_id=risk_event_id, transition_id=transition_id,
                        incident=incident_reference)
    created = db.add_event_annotation({
        "annotation_id": aid, "risk_event_id": risk_event_id,
        "transition_id": transition_id, "episode_sequence": episode_sequence,
        "annotation_type": annotation_type, "origin": origin,
        "reason_code": reason_code, "reason_text": reason_text,
        "excluded_from_research": excluded_from_research,
        "excluded_from_precision_metrics": excluded_from_precision_metrics,
        "related_notification_id": related_notification_id,
        "incident_reference": incident_reference,
        "evidence": evidence or {}, "created_by": created_by,
        "created_at": now.isoformat()})
    return {"annotation_id": aid, "created": created}


def classify_notification(db, *, notification_id: str, classification: str,
                          reason_text: str | None = None,
                          incident_reference: str | None = None,
                          created_by: str = "system",
                          now: datetime | None = None) -> dict:
    """给通知加运营分类。

    **不动 success 字段** —— 那条通知确实发出去了，用户确实收到了。
    把它改成 failed 会让通知链路的健康度统计失真。
    """
    now = now or _now()
    cid = "ncl-" + hashlib.sha256(
        f"{notification_id}|{classification}|{incident_reference or ''}"
        .encode()).hexdigest()[:20]
    created = db.add_notification_classification({
        "classification_id": cid, "notification_id": notification_id,
        "classification": classification, "reason_text": reason_text,
        "incident_reference": incident_reference, "created_by": created_by,
        "created_at": now.isoformat()})
    return {"classification_id": cid, "created": created}


# ======================= 研究层过滤 =======================

def filter_for_research(db, events: list[dict], *,
                        include_excluded: bool = False,
                        metric: str = "research") -> dict:
    """研究样本过滤。返回 {events, stats}。

    默认排除被标注的样本；`include_excluded=True` 时全部返回，
    但每条都带上 `excluded` 与 `exclusion_reasons`，让调用方自己决定。
    """
    try:
        by_event = db.annotations_by_event()
    except Exception:  # noqa: BLE001
        by_event = {}
    col = ("excluded_from_precision_metrics" if metric == "precision"
           else "excluded_from_research")

    kept, excluded = [], []
    by_reason: dict[str, int] = {}
    for e in events:
        anns = by_event.get(e.get("event_id")) or []
        hits = [a for a in anns if a.get(col)]
        item = {**e, "annotations": anns,
                "excluded": bool(hits),
                "exclusion_reasons": [a["reason_code"] for a in hits]}
        if hits:
            for a in hits:
                by_reason[a["reason_code"]] = by_reason.get(a["reason_code"], 0) + 1
            excluded.append(item)
            if include_excluded:
                kept.append(item)
        else:
            kept.append(item)

    return {
        "events": kept,
        "stats": {
            "total_events": len(events),
            "eligible_events": len(events) - len(excluded),
            "excluded_events": len(excluded),
            "excluded_by_reason": by_reason,
            "include_excluded": include_excluded,
            "metric": metric,
        },
    }


# ======================= 研究视图：合并被人为切断的 episode =======================

def merge_artificial_splits(db, events: list[dict]) -> dict:
    """把被人为切断的 episode 在**研究视图**里合并回一段连续区间。

    为什么必须做：一个真实持续了 10 小时的风险，被探测切成 seq=1 和 seq=2
    两段，研究层会把它当成两个独立样本 —— precision 分母凭空多一个，
    episode 时长统计也全错。

    为什么只在视图层做：原始记录是「当时系统确实这么判的」这一事实的唯一
    证据。回写会毁掉它。所以这里产出一个**新的合并视图对象**，
    并标明 `research_view_merged=True` 与合并依据。
    """
    try:
        by_event = db.annotations_by_event()
    except Exception:  # noqa: BLE001
        by_event = {}

    # 找出所有「假恢复」标注涉及的 identity
    artificial: dict[str, list[dict]] = {}
    for e in events:
        anns = by_event.get(e.get("event_id")) or []
        if any(a["annotation_type"] == T_PROBE_INDUCED for a in anns):
            artificial.setdefault(e.get("identity"), []).append(
                {**e, "_annotations": anns})

    merged, merged_ids = [], set()
    for identity, group in artificial.items():
        # 同一 identity 下的全部 episode（含未被标注的后续段）
        segs = sorted([e for e in events if e.get("identity") == identity],
                      key=lambda x: (x.get("episode_seq") or 0))
        if len(segs) < 2:
            continue
        anns = [a for s in segs for a in (by_event.get(s.get("event_id")) or [])
                if a["annotation_type"] == T_PROBE_INDUCED]
        merged_ids.update(s["event_id"] for s in segs)
        first, last = segs[0], segs[-1]
        merged.append({
            **first,
            "event_id": f"merged:{identity}",
            "research_view_merged": True,
            "merged_from_event_ids": [s["event_id"] for s in segs],
            "merged_episode_seqs": [s.get("episode_seq") for s in segs],
            "first_detected_at": first.get("first_detected_at"),
            "last_detected_at": last.get("last_detected_at"),
            "resolved_at": last.get("resolved_at"),
            "status": last.get("status"),
            "merge_basis": (
                f"episode {first.get('episode_seq')} 与 "
                f"{last.get('episode_seq')} 之间的恢复由生产探测造成，"
                "非真实风险恢复；研究视图按一段连续区间处理"),
            "merge_annotation_ids": sorted({a["annotation_id"] for a in anns}),
            "original_records_unchanged": True,
        })

    passthrough = [e for e in events if e.get("event_id") not in merged_ids]
    return {
        "events": passthrough + merged,
        "merged_count": len(merged),
        "merged_detail": merged,
        "note": ("合并只发生在研究视图。原始事件与 transition 记录未被修改，"
                 "实时页面仍显示完整时间线。"),
    }
