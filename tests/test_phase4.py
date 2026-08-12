"""Phase 4：recommendation 前向复盘、执行纪律统计与首页硬约束 Top 3。"""
from __future__ import annotations

import datetime as dt
from decimal import Decimal

from app import phase4_metrics as p4
from app import recommendation_service as reco
from app import review_service as reviews
from app import webdata
from app import webresearch
from app.db import Database


def _days(start: dt.date, n: int) -> list[str]:
    out, day = [], start
    while len(out) < n:
        if day.weekday() < 5:
            out.append(day.isoformat())
        day += dt.timedelta(days=1)
    return out


def _bars(days):
    return [{"date": d, "o": 100, "h": 101, "l": 99, "c": 100, "v": 1000}
            for d in days]


def _rec(action="DO_NOT_ADD", decided_by="position_limit"):
    inp = reco.SymbolInput(
        account="ACC", symbol="RKLB", cycle_id="cyc",
        account_reliable=True, technical_reliable=True,
        over_limit=(action == "DO_NOT_ADD"),
        current_weight=Decimal("0.10"), max_weight=Decimal("0.05"),
        input_refs={"snapshot_id": "snap-1"},
    )
    r = reco.synthesize_symbol(inp)
    r["action"] = action
    r["decided_by"] = decided_by
    r["decision_fingerprint"] = reco.decision_fingerprint(r)
    return r


def test_recommendation_id_enters_forward_outcomes_without_win_rate(make_config, tmp_path):
    cfg = make_config(ib_account="ACC", db_path=tmp_path / "review.db")
    db = Database(cfg.db_path); db.init_schema()
    days = _days(dt.date(2026, 1, 5), 40)
    for symbol in ("QQQ", "SOXX", "VIX", "RKLB"):
        db.upsert_daily_bars(symbol, _bars(days))
    rid = reco.persist(db, _rec(), now=days[2] + "T15:00:00+00:00")

    stats = reviews.generate_reviews(
        cfg, db, now=dt.datetime(2026, 3, 20, tzinfo=dt.timezone.utc))
    row = db.review_for_event(rid, reviews.CALC_VERSION)

    assert stats["recommendations_scanned"] == 1
    assert row is not None and row["source_type"] == "recommendation"
    assert row["event_id"] == rid and row["rule_code"] == "REC_DO_NOT_ADD"
    # recommendation 不套 precision 标签；主动减仓/禁止加仓不能被简化成输赢。
    assert row["outcome_labels"] == '{"1": {}, "3": {}, "5": {}, "10": {}, "20": {}}'
    note = webresearch._data_note(db)
    assert note["event_reviews_total"] == 0
    assert note["recommendation_reviews_total"] == 1
    assert "recommendation 复盘 1 个" in note["note"]


def _seed_plan_evidence(db):
    with db.connect() as c:
        c.execute("INSERT INTO position_cycles (id,account,symbol,cycle_number,status,opened_at,current_quantity,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
                  ("cyc", "ACC", "RKLB", 1, "ACTIVE", "2026-01-01", "10", "2026-01-01", "2026-01-01"))
        c.execute("INSERT INTO position_plan_versions (id,position_cycle_id,account,version,created_at,created_by,lifecycle_status) VALUES (?,?,?,?,?,?,?)",
                  ("pv", "cyc", "ACC", 1, "2026-01-01", "tester", "PUBLISHED"))
        for row in (
            ("a1", "EXECUTED", "2026-01-02", "2026-01-02"),
            ("a2", "TRIGGERED", "2026-01-03", None),
            ("a3", "ACTIVE", None, None),
        ):
            c.execute("INSERT INTO position_plan_actions (id,position_cycle_id,account,plan_version_id,action_type,trigger_type,status,triggered_at,confirmed_at,created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                      (row[0], "cyc", "ACC", "pv", "REDUCE", "MANUAL", row[1], row[2], row[3], "2026-01-01"))
        c.execute("INSERT INTO position_plan_execution_links (id,position_cycle_id,account,plan_action_id,execution_id,match_status,matched_at) VALUES (?,?,?,?,?,?,?)",
                  ("l1", "cyc", "ACC", "a1", "ex1", "MATCHED", "2026-01-02"))
        c.execute("INSERT INTO position_plan_execution_links (id,position_cycle_id,account,execution_id,match_status,matched_at) VALUES (?,?,?,?,?,?)",
                  ("l2", "cyc", "ACC", "ex2", "PLAN_OUTSIDE", "2026-01-03"))
        c.execute("INSERT INTO position_plan_execution_links (id,position_cycle_id,account,execution_id,match_status,matched_at) VALUES (?,?,?,?,?,?)",
                  ("l3", "cyc", "ACC", "ex3", "PENDING_CONFIRMATION", "2026-01-04"))


def test_plan_execution_denominator_and_pending_are_explicit(tmp_path):
    db = Database(tmp_path / "metrics.db"); db.init_schema(); _seed_plan_evidence(db)
    m = p4.plan_execution(db, "ACC")
    assert m["formal_actions"] == 3
    assert m["triggered_n"] == 2 and m["completed_n"] == 1
    assert m["completion_rate"] == "0.5000"
    assert m["matched_executions"] == 1 and m["outside_executions"] == 1
    assert m["pending_executions"] == 1


def test_rule_violation_requires_observable_conflict_and_reduce_is_not_failure(tmp_path):
    db = Database(tmp_path / "violations.db"); db.init_schema(); _seed_plan_evidence(db)
    rid = reco.persist(db, _rec(), now="2026-01-01T00:00:00+00:00")
    with db.connect() as c:
        for eid, typ, ts, match in (
            ("ex1", "ADD", "2026-01-02T15:00:00+00:00", "MATCHED"),
            ("ex2", "REDUCE", "2026-01-03T15:00:00+00:00", "PLAN_OUTSIDE"),
            ("ex4", "ADD", "2025-12-31T15:00:00+00:00", "PENDING_CONFIRMATION"),
        ):
            c.execute("INSERT INTO position_cycle_events (id,position_cycle_id,account,symbol,event_type,execution_id,execution_time,match_status,created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                      ("evt-" + eid, "cyc", "ACC", "RKLB", typ, eid, ts, match, ts))
    m = p4.rule_violations(db, "ACC")
    assert m["recommendation_observed_n"] == 2
    assert m["recommendation_unobserved_n"] == 1
    assert m["guard_observed_n"] == 2 and m["guard_violations"] == 1
    assert m["hard_observed_n"] == 2 and m["hard_violations"] == 1
    assert m["proactive_reduce_n"] == 1
    assert m["examples"][0]["recommendation_id"] == rid


def _event(db, event_id, source, severity, status="open", rule=None,
           ts="2026-08-10T10:00:00+00:00"):
    db.create_risk_event({
        "event_id": event_id, "identity": "i-" + event_id, "episode_seq": 1,
        "source_type": source, "event_code": rule or event_id,
        "rule_code": rule or event_id, "category": "test", "severity": severity,
        "max_severity": severity, "status": status, "title": "事件" + event_id,
        "explanation": "说明", "first_detected_at": ts, "last_detected_at": ts,
        "created_at": ts, "updated_at": ts, "reliable": True,
    })


def test_overview_top3_uses_open_hard_registry_not_recent_occurrences(make_config, tmp_path):
    cfg = make_config(ib_account="ACC", db_path=tmp_path / "top3.db")
    db = Database(cfg.db_path); db.init_schema()
    _event(db, "critical", "account", "critical")
    _event(db, "high", "position", "high")
    _event(db, "warning", "behavior", "warning")
    _event(db, "fourth", "account", "warning", ts="2026-08-09T10:00:00+00:00")
    _event(db, "technical", "technical", "critical")   # 非硬约束，排除
    _event(db, "acked", "account", "critical", status="acknowledged")

    rk = webdata.risk_summary(cfg, db, [], dt.datetime(2026, 8, 10, tzinfo=dt.timezone.utc))
    assert rk["hard_alerts_total"] == 4
    assert [x["event_id"] for x in rk["open_alerts"]] == ["critical", "high", "warning"]
    assert len(rk["open_alerts"]) == 3
