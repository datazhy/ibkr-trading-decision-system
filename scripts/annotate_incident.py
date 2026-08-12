#!/usr/bin/env python3
"""事故标注 CLI。**默认只读** —— 需要写入必须显式 --allow-side-effects。

这个默认值本身就是事故的教训：诊断工具默认能改状态，就迟早会有人在排查
问题时误改。这里把「安全」放在默认那一侧。
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sys

sys.path.insert(0, "/app")

from app import event_annotations as ea          # noqa: E402
from app.config import load_config               # noqa: E402
from app.db import Database                      # noqa: E402

INCIDENT = "brief-p05-probe-20260729"
REASON_CODE = "incomplete_market_context_side_effect"
REASON_TEXT = (
    "2026-07-29 一次生产探测误调 process_assessments，传入的市场评估上下文"
    "不完整（calculated_metrics 结构无法被规则消费），导致本轮未产出任何"
    "市场事件，7 条仍在触发的事件被判定为恢复；6 秒后正常同步重新评估，"
    "同一批事件被重开为新 episode。"
    "因果依据：11:00:57 之后没有任何新的市场评估，最新一份（07:39:55）"
    "仍然触发全部 6 条市场规则 —— 真实恢复要求底层评估不再触发，此处不成立。"
    "该边界为人为造成，不代表真实风险恢复。")


def main() -> int:
    ap = argparse.ArgumentParser(description="标注 2026-07-29 生产探测事故")
    ap.add_argument("--allow-side-effects", action="store_true",
                    help="确认写入标注（不加则只预览，不写任何数据）")
    ap.add_argument("--window-start", default="2026-07-29T11:00:00")
    ap.add_argument("--window-end", default="2026-07-29T11:01:30")
    ap.add_argument("--created-by", default="ops:incident-review")
    args = ap.parse_args()

    cfg = load_config()
    d = Database(cfg.db_path)
    now = dt.datetime.now(dt.timezone.utc)

    # ---- 1) 只读定位：被误判恢复的 episode 与随后重开的 episode ----
    resolved = d.transitions_in_window(args.window_start, args.window_end,
                                       "resolved")
    reopened = d.transitions_in_window(args.window_start, args.window_end,
                                       "reopened")
    res_ids = {t["event_id"] for t in resolved}

    # ---- 2) 因果校验：底层市场评估必须**没有变化** ----
    with d.connect() as conn:
        mkt = conn.execute(
            "SELECT created_at, triggered_rules FROM market_environment_assessments"
            " ORDER BY created_at DESC LIMIT 1").fetchone()
        newer = conn.execute(
            "SELECT COUNT(*) c FROM market_environment_assessments"
            " WHERE created_at > ?", (args.window_start,)).fetchone()["c"]
    still_firing = set(json.loads(mkt["triggered_rules"] or "[]")) if mkt else set()
    evidence = {
        "incident": INCIDENT,
        "window": [args.window_start, args.window_end],
        "latest_market_assessment_at": (mkt["created_at"] if mkt else None),
        "new_market_assessments_after_window_start": newer,
        "rules_still_firing_in_latest_assessment": sorted(still_firing),
        "causal_argument": ("窗口后无新市场评估，且最新评估仍触发这些规则；"
                            "真实恢复要求评估不再触发，故恢复非真实数据造成"),
    }
    if newer != 0:
        print("!! 窗口后存在新的市场评估，因果不成立，拒绝批量标注")
        return 2

    # ---- 3) 逐条确认：只标注**既被关闭又被重开**的那些 ----
    targets = []
    for t in resolved:
        row = d.get_risk_event(t["event_id"]) if hasattr(d, "get_risk_event") else None
        if row is None:
            with d.connect() as conn:
                r = conn.execute("SELECT * FROM risk_event_registry"
                                 " WHERE event_id=?", (t["event_id"],)).fetchone()
                row = dict(r) if r else None
        if row is None:
            continue
        rule = row.get("rule_code")
        # 市场类规则必须仍在最新评估里触发，才算「假恢复」
        if row.get("source_type") == "market" and rule not in still_firing:
            print(f"   跳过 {rule}：最新评估里已不触发，可能是真实恢复")
            continue
        targets.append((row, t))

    # 后续 episode（seq+1）也要标注 —— 它的起点是人为的
    follow = []
    for row, _t in targets:
        with d.connect() as conn:
            nxt = conn.execute(
                "SELECT * FROM risk_event_registry WHERE identity=?"
                " AND episode_seq=? LIMIT 1",
                (row["identity"], (row.get("episode_seq") or 1) + 1)).fetchone()
        if nxt:
            tid = next((t["id"] for t in reopened
                        if t["event_id"] == nxt["event_id"]), None)
            follow.append((dict(nxt), tid))

    print(f"=== 事故 {INCIDENT} ===")
    print(f"  窗口 {args.window_start} ~ {args.window_end}")
    print(f"  被误判恢复的 episode : {len(targets)}")
    print(f"  随之重开的 episode   : {len(follow)}")
    for row, t in targets:
        print(f"    - {row['rule_code']:<26} seq={row['episode_seq']} "
              f"transition_id={t['id']}")
    for row, tid in follow:
        print(f"    + {row['rule_code']:<26} seq={row['episode_seq']} "
              f"transition_id={tid}")

    # ---- 4) 误发通知 ----
    with d.connect() as conn:
        notifs = [dict(r) for r in conn.execute(
            "SELECT notification_id, title, notification_type, success"
            " FROM risk_notifications WHERE attempted_at>=? AND attempted_at<=?",
            (args.window_start, args.window_end)).fetchall()]
    false_alerts = [n for n in notifs
                    if n["notification_type"] == "opened" and n["success"]]
    print(f"  窗口内通知 {len(notifs)} 条，其中误发告警 {len(false_alerts)} 条")

    if not args.allow_side_effects:
        print()
        print("  【只读模式】未写入任何数据。")
        print("  确认无误后加 --allow-side-effects 执行标注。")
        return 0

    print()
    print("  ⚠️  写入模式：将追加标注（原始事件与通知一字不改）")

    n_ann = n_new = 0
    notif_id = false_alerts[0]["notification_id"] if false_alerts else None
    for row, t in targets:
        r = ea.annotate_event(
            d, risk_event_id=row["event_id"],
            annotation_type=ea.T_PROBE_INDUCED, origin=ea.ORIGIN_PROBE,
            reason_code=REASON_CODE, reason_text=REASON_TEXT,
            transition_id=t["id"], episode_sequence=row.get("episode_seq"),
            excluded_from_research=True, excluded_from_precision_metrics=True,
            related_notification_id=notif_id, incident_reference=INCIDENT,
            evidence=evidence, created_by=args.created_by, now=now)
        n_ann += 1
        n_new += int(r["created"])
    for row, tid in follow:
        r = ea.annotate_event(
            d, risk_event_id=row["event_id"],
            annotation_type=ea.T_PROBE_INDUCED, origin=ea.ORIGIN_PROBE,
            reason_code=REASON_CODE,
            reason_text=REASON_TEXT + "（本 episode 的起点由该假恢复造成）",
            transition_id=tid, episode_sequence=row.get("episode_seq"),
            excluded_from_research=True, excluded_from_precision_metrics=True,
            related_notification_id=notif_id, incident_reference=INCIDENT,
            evidence=evidence, created_by=args.created_by, now=now)
        n_ann += 1
        n_new += int(r["created"])

    n_cls = 0
    for n in false_alerts:
        c = ea.classify_notification(
            d, notification_id=n["notification_id"],
            classification=ea.C_PROBE_FALSE_ALERT,
            reason_text=("由生产探测造成的假恢复触发的重开告警；"
                         "success 保持不变 —— 该通知技术上确实发送成功"),
            incident_reference=INCIDENT, created_by=args.created_by, now=now)
        n_cls += int(c["created"])

    print()
    print("=== 标注结果 ===")
    print(f"  annotation_count              : {n_ann}（新建 {n_new}）")
    print(f"  affected_event_count          : {len(targets) + len(follow)}")
    print(f"  affected_transition_count     : {len(targets) + len(follow)}")
    print(f"  affected_notification_count   : {len(false_alerts)}（新分类 {n_cls}）")
    print(f"  excluded_from_research_count  : {len(targets) + len(follow)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
