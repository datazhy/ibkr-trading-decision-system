"""Phase 4 复盘证据：计划执行、规则违反与 recommendation 前向结果。

全部指标只描述数据库里真实可观测的样本。缺少 recommendation、尚待人工确认的成交
和未走完前向窗口均单独计数，不会被当成违反或未命中。
"""
from __future__ import annotations

import json
from collections import defaultdict
from decimal import Decimal, InvalidOperation

from . import review_service as rs

MIN_CONCLUSION_SAMPLES = 20
_GUARD_ACTIONS = {"DO_NOT_ADD", "REDUCE", "EXIT"}
_HARD_DECIDERS = {"hard_stop", "position_limit"}


def _d(v):
    if v in (None, ""):
        return None
    try:
        return v if isinstance(v, Decimal) else Decimal(str(v))
    except (InvalidOperation, TypeError, ValueError):
        return None


def _rate(num: int, den: int) -> str | None:
    return None if den <= 0 else str((Decimal(num) / Decimal(den)).quantize(Decimal("0.0001")))


def _mean(values) -> str | None:
    vals = [v for v in (_d(x) for x in values) if v is not None]
    if not vals:
        return None
    return str((sum(vals, Decimal("0")) / Decimal(len(vals))).quantize(Decimal("0.0001")))


def plan_execution(db, account: str | None = None) -> dict:
    args: list = []
    account_sql = ""
    if account:
        account_sql = " AND a.account=?"
        args.append(account)
    with db.connect() as conn:
        actions = [dict(r) for r in conn.execute(
            "SELECT a.* FROM position_plan_actions a "
            "JOIN position_plan_versions v ON v.id=a.plan_version_id "
            "WHERE v.lifecycle_status='PUBLISHED'" + account_sql,
            args).fetchall()]
        link_args: list = []
        link_sql = ""
        if account:
            link_sql = " WHERE account=?"
            link_args.append(account)
        links = [dict(r) for r in conn.execute(
            "SELECT * FROM position_plan_execution_links" + link_sql, link_args).fetchall()]

    triggered = [a for a in actions if a.get("triggered_at") or
                 a.get("status") in {"TRIGGERED", "EXECUTED"}]
    confirmed = [a for a in triggered if a.get("confirmed_at")]
    completed = [a for a in triggered if a.get("status") == "EXECUTED"]
    by_link = defaultdict(int)
    for link in links:
        by_link[link.get("match_status") or "UNKNOWN"] += 1
    observed_links = by_link["MATCHED"] + by_link["PLAN_OUTSIDE"]
    return {
        "formal_actions": len(actions),
        "triggered_n": len(triggered),
        "confirmed_n": len(confirmed),
        "completed_n": len(completed),
        "completion_rate": _rate(len(completed), len(triggered)),
        "confirmation_rate": _rate(len(confirmed), len(triggered)),
        "matched_executions": by_link["MATCHED"],
        "outside_executions": by_link["PLAN_OUTSIDE"],
        "pending_executions": by_link["PENDING_CONFIRMATION"],
        "execution_observed_n": observed_links,
        "outside_rate": _rate(by_link["PLAN_OUTSIDE"], observed_links),
        "enough_samples": len(triggered) >= MIN_CONCLUSION_SAMPLES,
        "definition": ("计划完成率 = 已达到计划数量并标记 EXECUTED 的正式 action / "
                       "已真实触发的正式 action；ACTIVE 未触发和已取消 action 不进入分母。"),
    }


def rule_violations(db, account: str | None = None) -> dict:
    args: list = []
    account_sql = " WHERE e.execution_id IS NOT NULL"
    if account:
        account_sql += " AND e.account=?"
        args.append(account)
    with db.connect() as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT e.execution_id, e.account, e.symbol, e.event_type, e.execution_time, "
            " e.match_status, e.plan_action_id, r.id recommendation_id, "
            " r.action recommendation_action, r.decided_by, r.status recommendation_status "
            "FROM position_cycle_events e "
            "LEFT JOIN recommendation_snapshots r ON r.id=("
            " SELECT rr.id FROM recommendation_snapshots rr "
            " WHERE rr.account=e.account AND rr.symbol=e.symbol "
            "   AND rr.created_at<=e.execution_time "
            " ORDER BY rr.created_at DESC LIMIT 1)" + account_sql +
            " ORDER BY e.execution_time", args).fetchall()]

    observed = violations = unobserved = proactive_reduce = 0
    plan_observed = plan_violations = pending_plan = 0
    guard_observed = guard_violations = hard_observed = hard_violations = 0
    by_rule: dict[str, dict[str, int]] = defaultdict(lambda: {"observed_n": 0, "violations": 0})
    examples = []
    for row in rows:
        match = row.get("match_status")
        if match in {"MATCHED", "PLAN_OUTSIDE"}:
            plan_observed += 1
            if match == "PLAN_OUTSIDE":
                plan_violations += 1
        elif match == "PENDING_CONFIRMATION":
            pending_plan += 1

        rec_action = row.get("recommendation_action")
        event_type = row.get("event_type")
        if not row.get("recommendation_id"):
            unobserved += 1
            continue
        observed += 1
        if event_type in {"REDUCE", "EXIT"}:
            proactive_reduce += 1
        if rec_action not in _GUARD_ACTIONS:
            continue
        guard_observed += 1
        decided_by = row.get("decided_by") or "unknown"
        by_rule[decided_by]["observed_n"] += 1
        is_violation = event_type in {"OPEN", "ADD"}
        if decided_by in _HARD_DECIDERS:
            hard_observed += 1
        if is_violation:
            guard_violations += 1
            violations += 1
            by_rule[decided_by]["violations"] += 1
            if decided_by in _HARD_DECIDERS:
                hard_violations += 1
            if len(examples) < 10:
                examples.append({
                    "execution_id": row.get("execution_id"), "symbol": row.get("symbol"),
                    "event_type": event_type, "recommendation_id": row.get("recommendation_id"),
                    "recommendation_action": rec_action, "decided_by": decided_by,
                })

    rule_rows = []
    for code, x in sorted(by_rule.items()):
        rule_rows.append({"rule": code, **x,
                          "violation_rate": _rate(x["violations"], x["observed_n"]),
                          "enough_samples": x["observed_n"] >= MIN_CONCLUSION_SAMPLES})
    return {
        "execution_events": len(rows), "recommendation_observed_n": observed,
        "recommendation_unobserved_n": unobserved,
        "proactive_reduce_n": proactive_reduce,
        "guard_observed_n": guard_observed, "guard_violations": guard_violations,
        "guard_violation_rate": _rate(guard_violations, guard_observed),
        "hard_observed_n": hard_observed, "hard_violations": hard_violations,
        "plan_observed_n": plan_observed, "plan_violations": plan_violations,
        "plan_violation_rate": _rate(plan_violations, plan_observed),
        "pending_plan_n": pending_plan,
        "rules": rule_rows, "examples": examples,
        "enough_samples": guard_observed >= MIN_CONCLUSION_SAMPLES,
        "note": ("只有成交时点存在可追溯 recommendation 且动作与护栏直接冲突时才记违反；"
                 "无建议、数据不足和待人工确认不记违反。REDUCE/EXIT 一律作为主动降险事实，"
                 "不会因后续价格上涨被判为规则失败。"),
    }


def recommendation_outcomes(db, window: int = 5) -> dict:
    rows, total = db.query_reviews(version=rs.CALC_VERSION,
                                   source_type="recommendation", limit=5000)
    grouped: dict[str, dict] = defaultdict(lambda: {
        "sample_n": 0, "pending_n": 0, "position_returns": [],
        "position_maes": [], "account_changes": [], "references": [],
    })
    completed_total = 0
    for row in rows:
        action = str(row.get("rule_code") or "REC_UNKNOWN").removeprefix("REC_")
        g = grouped[action]
        try:
            win = (json.loads(row.get("forward_outcomes") or "{}").get(str(window)) or {})
        except (TypeError, ValueError):
            win = {}
        if win.get("status") in {None, "pending", "error"}:
            g["pending_n"] += 1
            continue
        g["sample_n"] += 1
        completed_total += 1
        g["position_returns"].append(win.get("position_return"))
        g["position_maes"].append(win.get("position_mae"))
        g["account_changes"].append(win.get("account_change"))
        if len(g["references"]) < 5:
            g["references"].append(row.get("event_id"))

    out = []
    for action, g in sorted(grouped.items()):
        out.append({
            "action": action, "sample_n": g["sample_n"], "pending_n": g["pending_n"],
            "avg_position_return": _mean(g["position_returns"]),
            "avg_position_mae": _mean(g["position_maes"]),
            "avg_account_change": _mean(g["account_changes"]),
            "references": g["references"],
            "enough_samples": g["sample_n"] >= MIN_CONCLUSION_SAMPLES,
        })
    return {
        "window": int(window), "reviews_total": total, "completed_n": completed_total,
        "actions": out, "enough_samples": completed_total >= MIN_CONCLUSION_SAMPLES,
        "note": ("这里只展示 recommendation_id 对应的描述性前向结果，不计算“胜率”。"
                 "不同动作的目标不对称，尤其主动减仓后价格上涨不能反推规则失败。"),
    }


def dashboard(db, *, account: str | None = None, window: int = 5) -> dict:
    return {
        "plan": plan_execution(db, account),
        "violations": rule_violations(db, account),
        "recommendation_outcomes": recommendation_outcomes(db, window),
        "min_conclusion_samples": MIN_CONCLUSION_SAMPLES,
    }
