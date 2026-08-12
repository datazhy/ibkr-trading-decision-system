"""只读探测防护与事故标注验收测试（对应清单 1–18）。

起因是一次真实事故：一个「只读探测」脚本调用了 process_assessments，
传了 notifier=None 就以为安全 —— 结果 7 条仍在触发的事件被判恢复，
正常同步随后把它们重开，并向用户误发了一条飞书告警。

这组测试钉死两件事：
  1. **只读就是只读** —— preview 一行都不写；缺授权的写入直接抛异常
  2. **原始记录只增不改** —— 标注是追加的另一张表，事件与通知一字未动
"""
import datetime as dt
import sqlite3
from decimal import Decimal as D

import pytest
from fastapi.testclient import TestClient

from app import event_annotations as ea
from app import event_service as es
from app import execution_context as ec
from app.db import Database
from app.webapp import create_app

NOW = dt.datetime(2026, 7, 29, 22, 0, tzinfo=dt.timezone.utc)
ACC = "U1234567"
INCIDENT = "brief-p05-probe-20260729"


@pytest.fixture
def env(make_config):
    cfg = make_config(ib_account=ACC, apprise_urls=[])
    d = Database(cfg.db_path)
    d.init_schema()
    return cfg, d


def _account(events=None, aid="acct-1"):
    return {
        "assessment_id": aid, "snapshot_id": "snap-1", "reliable": True,
        "risk_level": "high", "data_as_of": NOW.isoformat(),
        "quality_status": "fresh", "rule_set_version": "risk-v2~h",
        "calculated_metrics": {"account": {}, "concentration": {},
                               "positions": []},
        "events": events if events is not None else [
            {"event_code": "ACC_CASH_LOW", "rule_code": "ACC_CASH_LOW",
             "category": "liquidity", "severity": "high",
             "title": "现金比例过低", "explanation": "E",
             "actual_value": "0.01", "threshold_value": "0.05"}],
    }


def _market(events=None, aid="mkt-1"):
    return {
        "assessment_id": aid, "market_state": "stressed",
        "market_risk_level": "high", "reliable": True,
        "data_as_of": "2026-07-29", "quality_status": "fresh",
        "calculated_metrics": {"qqq": {"close": "480", "below_ma50": True}},
        "events": events if events is not None else [
            {"event_code": "TREND_QQQ_BELOW_MA50",
             "rule_code": "TREND_QQQ_BELOW_MA50", "category": "trend",
             "severity": "high", "title": "QQQ 跌破 50 日均线",
             "explanation": "E", "actual_value": "-0.05",
             "threshold_value": "0", "affected_instrument": "QQQ"}],
    }


def _counts(d) -> dict:
    c = sqlite3.connect(d.db_path)
    out = {}
    for t in ("risk_event_registry", "risk_event_transitions",
              "risk_notifications", "alert_state"):
        try:
            out[t] = c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        except sqlite3.Error:
            out[t] = None
    c.close()
    return out


class Recorder:
    def __init__(self):
        self.sent = []

    def send(self, title, body):
        self.sent.append((title, body))
        return True


# ---------------- 1–5. preview 完全只读 ----------------

def test_preview_does_not_touch_registry(env):
    cfg, d = env
    before = _counts(d)
    res = es.preview_assessments(cfg, d, account=_account(), market=_market(),
                                 now=NOW)
    assert res["read_only"] is True
    assert res["trigger_count"] >= 2
    assert _counts(d)["risk_event_registry"] == before["risk_event_registry"]


def test_preview_adds_no_transition(env):
    cfg, d = env
    before = _counts(d)
    es.preview_assessments(cfg, d, account=_account(), market=_market(), now=NOW)
    assert _counts(d)["risk_event_transitions"] == before["risk_event_transitions"]


def test_preview_does_not_touch_alert_state(env):
    cfg, d = env
    before = _counts(d)
    es.preview_assessments(cfg, d, account=_account(), market=_market(), now=NOW)
    assert _counts(d)["alert_state"] == before["alert_state"]


def test_preview_writes_no_notification(env):
    cfg, d = env
    before = _counts(d)
    es.preview_assessments(cfg, d, account=_account(), market=_market(), now=NOW)
    assert _counts(d)["risk_notifications"] == before["risk_notifications"]


def test_preview_opens_and_closes_no_episode(env):
    """先真实建一条事件，再用不完整上下文预览 —— episode 必须纹丝不动。

    这正是事故的场景：预览会**告诉你**「这些会被判恢复」，但不会真的关。
    """
    cfg, d = env
    es.process_assessments(cfg, d, None, account=_account(), market=_market(),
                           now=NOW, execution_context=ec.test_context())
    rows, total = d.query_risk_events()
    assert total >= 2
    snapshot = {r["event_id"]: (r["status"], r["episode_seq"],
                                r["resolved_at"]) for r in rows}

    # 不完整上下文：市场事件全空
    res = es.preview_assessments(cfg, d, account=_account(),
                                 market=_market(events=[]), now=NOW)
    assert res["would_resolve_count"] >= 1
    assert any("将被判定为恢复" in w["warning"] for w in res["would_resolve"])

    after, _ = d.query_risk_events()
    assert {r["event_id"]: (r["status"], r["episode_seq"], r["resolved_at"])
            for r in after} == snapshot


# ---------------- 6–11. 执行上下文守卫 ----------------

def test_missing_execution_context_is_rejected(env):
    cfg, d = env
    with pytest.raises(ec.SideEffectNotAllowed) as exc:
        es.process_assessments(cfg, d, None, account=_account(), market=None,
                               now=NOW)
    assert "缺少 execution_context" in str(exc.value)
    assert "notifier=None **不代表只读**" in str(exc.value)
    assert _counts(d)["risk_event_registry"] == 0


def test_allow_side_effects_false_is_rejected(env):
    cfg, d = env
    with pytest.raises(ec.SideEffectNotAllowed) as exc:
        es.process_assessments(cfg, d, None, account=_account(), market=None,
                               now=NOW, execution_context=ec.read_only())
    assert "allow_side_effects=False" in str(exc.value)
    assert "preview_assessments()" in str(exc.value)
    assert _counts(d)["risk_event_registry"] == 0


def test_unknown_source_is_rejected(env):
    """诊断脚本即使自称 allow_side_effects=True 也拦下 —— 来源不在白名单。"""
    cfg, d = env
    bad = ec.ExecutionContext(source="diagnostic", allow_side_effects=True)
    with pytest.raises(ec.SideEffectNotAllowed) as exc:
        es.process_assessments(cfg, d, None, account=_account(), market=None,
                               now=NOW, execution_context=bad)
    assert "不在允许列表" in str(exc.value)


def test_scheduler_context_allowed(env):
    cfg, d = env
    out = es.process_assessments(cfg, d, None, account=_account(), market=None,
                                 now=NOW,
                                 execution_context=ec.scheduler_context())
    assert out["triggers"] >= 1
    assert out["source"] == ec.SRC_SCHEDULER
    assert d.query_risk_events()[1] >= 1


def test_sync_worker_context_allowed(env):
    cfg, d = env
    out = es.process_assessments(cfg, d, None, account=_account(), market=None,
                                 now=NOW, execution_context=ec.sync_context())
    assert out["source"] == ec.SRC_SYNC_WORKER
    assert d.query_risk_events()[1] >= 1


def test_automated_test_context_allowed(env):
    cfg, d = env
    out = es.process_assessments(cfg, d, None, account=_account(), market=None,
                                 now=NOW, execution_context=ec.test_context())
    assert out["source"] == ec.SRC_AUTOMATED_TEST


def test_none_notifier_does_not_bypass_guard(env):
    """**notifier=None 不是只读凭证。** 事故的核心误判就在这里。"""
    cfg, d = env
    with pytest.raises(ec.SideEffectNotAllowed):
        es.process_assessments(cfg, d, None, account=_account(),
                               market=_market(), now=NOW)
    assert _counts(d)["risk_event_registry"] == 0
    assert _counts(d)["risk_event_transitions"] == 0


def test_side_effect_execution_is_audited(env):
    cfg, d = env
    ctx = ec.manual_api_context(initiated_by="tester")
    es.process_assessments(cfg, d, Recorder(), account=_account(),
                           market=_market(), now=NOW, execution_context=ctx)
    rows = d.side_effect_executions()
    assert rows
    r = rows[0]
    assert r["source"] == ec.SRC_MANUAL_API
    assert r["correlation_id"] == ctx.correlation_id
    assert r["initiated_by"] == "tester"
    assert r["started_at"]


# ---------------- 12–15. 标注 ----------------

def _make_split(d):
    """造一段被人为切断的 episode：seq=1 被误判恢复，seq=2 重开。"""
    base = NOW - dt.timedelta(hours=10)
    common = dict(source_type="market", event_code="TREND_QQQ_BELOW_MA50",
                  rule_code="TREND_QQQ_BELOW_MA50", category="trend",
                  title="QQQ 跌破 50 日均线", explanation="E",
                  actual_value="-0.05", threshold_value="0", unit="ratio",
                  affected_symbol=None, affected_instrument="QQQ",
                  account_id=ACC, snapshot_id="s", assessment_id="a",
                  recommended_check="检查", quality_status="fresh", reliable=1,
                  notification_owner="ops", rule_version="market-v1",
                  rule_set_version="market-v1")
    d.create_risk_event({
        **common, "event_id": "evt-seq1", "identity": "ident-qqq",
        "episode_seq": 1, "severity": "high", "max_severity": "high",
        "status": "resolved",
        "first_detected_at": base.isoformat(),
        "last_detected_at": (NOW - dt.timedelta(minutes=6)).isoformat(),
        "occurrence_count": 40,
        "created_at": base.isoformat(),
        "updated_at": (NOW - dt.timedelta(minutes=6)).isoformat()})
    with sqlite3.connect(d.db_path) as c:
        c.execute("UPDATE risk_event_registry SET resolved_at=? WHERE event_id=?",
                  ((NOW - dt.timedelta(minutes=6)).isoformat(), "evt-seq1"))
    d.create_risk_event({
        **common, "event_id": "evt-seq2", "identity": "ident-qqq",
        "episode_seq": 2, "severity": "high", "max_severity": "high",
        "status": "open",
        "first_detected_at": (NOW - dt.timedelta(minutes=5)).isoformat(),
        "last_detected_at": NOW.isoformat(), "occurrence_count": 1,
        "created_at": (NOW - dt.timedelta(minutes=5)).isoformat(),
        "updated_at": NOW.isoformat()})
    return ["evt-seq1", "evt-seq2"]


def test_annotation_is_idempotent(env):
    cfg, d = env
    ids = _make_split(d)
    kw = dict(risk_event_id=ids[0], annotation_type=ea.T_PROBE_INDUCED,
              origin=ea.ORIGIN_PROBE,
              reason_code="incomplete_market_context_side_effect",
              reason_text="不完整市场上下文导致的假恢复",
              transition_id=None, episode_sequence=1,
              excluded_from_research=True,
              excluded_from_precision_metrics=True,
              incident_reference=INCIDENT, created_by="tester")
    a = ea.annotate_event(d, **kw, now=NOW)
    b = ea.annotate_event(d, **kw, now=NOW + dt.timedelta(hours=1))
    assert a["created"] is True and b["created"] is False
    assert a["annotation_id"] == b["annotation_id"]
    assert len(d.event_annotations(ids[0])) == 1


def test_annotated_event_excluded_from_research_by_default(env):
    cfg, d = env
    ids = _make_split(d)
    ea.annotate_event(d, risk_event_id=ids[0],
                      annotation_type=ea.T_PROBE_INDUCED,
                      origin=ea.ORIGIN_PROBE,
                      reason_code="incomplete_market_context_side_effect",
                      reason_text="假恢复", episode_sequence=1,
                      excluded_from_research=True,
                      excluded_from_precision_metrics=True,
                      incident_reference=INCIDENT, created_by="t", now=NOW)
    rows, _ = d.query_risk_events()
    res = ea.filter_for_research(d, [dict(r) for r in rows])
    assert res["stats"]["total_events"] == 2
    assert res["stats"]["eligible_events"] == 1
    assert res["stats"]["excluded_events"] == 1
    assert res["stats"]["excluded_by_reason"] == {
        "incomplete_market_context_side_effect": 1}
    assert all(e["event_id"] != ids[0] for e in res["events"])


def test_include_excluded_returns_them(env):
    cfg, d = env
    ids = _make_split(d)
    ea.annotate_event(d, risk_event_id=ids[0],
                      annotation_type=ea.T_PROBE_INDUCED,
                      origin=ea.ORIGIN_PROBE, reason_code="rc",
                      reason_text="r", excluded_from_research=True,
                      incident_reference=INCIDENT, created_by="t", now=NOW)
    rows, _ = d.query_risk_events()
    res = ea.filter_for_research(d, [dict(r) for r in rows],
                                 include_excluded=True)
    assert len(res["events"]) == 2
    flagged = [e for e in res["events"] if e["excluded"]]
    assert len(flagged) == 1
    assert flagged[0]["exclusion_reasons"] == ["rc"]


def test_original_records_unchanged_by_annotation(env):
    """标注只增不改 —— 原始事件与 transition 一字未动。"""
    cfg, d = env
    ids = _make_split(d)
    c = sqlite3.connect(d.db_path); c.row_factory = sqlite3.Row
    before = {r["event_id"]: dict(r) for r in
              c.execute("SELECT * FROM risk_event_registry")}
    tbefore = [dict(r) for r in c.execute("SELECT * FROM risk_event_transitions")]

    ea.annotate_event(d, risk_event_id=ids[0],
                      annotation_type=ea.T_PROBE_INDUCED,
                      origin=ea.ORIGIN_PROBE, reason_code="rc",
                      reason_text="r", excluded_from_research=True,
                      incident_reference=INCIDENT, created_by="t", now=NOW)

    after = {r["event_id"]: dict(r) for r in
             c.execute("SELECT * FROM risk_event_registry")}
    tafter = [dict(r) for r in c.execute("SELECT * FROM risk_event_transitions")]
    c.close()
    assert after == before
    assert tafter == tbefore
    assert len(d.event_annotations(ids[0])) == 1


def test_notification_classification_keeps_success(env):
    """误发通知保留 success —— 它在技术上确实发送成功了。"""
    cfg, d = env
    nid = "notif-1"
    with sqlite3.connect(d.db_path) as c:
        cols = [x[1] for x in c.execute("PRAGMA table_info(risk_notifications)")]
        vals = {"notification_id": nid, "channel": "feishu",
                "notification_type": "opened", "severity": "high",
                "title": "🟠【高风险】市场风险恶化（2 项）",
                "success": 1, "attempted_at": NOW.isoformat(),
                "sent_at": NOW.isoformat(), "retry_count": 0}
        use = {k: v for k, v in vals.items() if k in cols}
        c.execute("INSERT INTO risk_notifications (%s) VALUES (%s)" % (
            ",".join(use), ",".join("?" * len(use))), tuple(use.values()))

    r = ea.classify_notification(
        d, notification_id=nid, classification=ea.C_PROBE_FALSE_ALERT,
        reason_text="由生产探测引起的假恢复触发的重开告警",
        incident_reference=INCIDENT, created_by="t", now=NOW)
    assert r["created"] is True
    assert ea.classify_notification(
        d, notification_id=nid, classification=ea.C_PROBE_FALSE_ALERT,
        incident_reference=INCIDENT, created_by="t", now=NOW)["created"] is False

    c = sqlite3.connect(d.db_path); c.row_factory = sqlite3.Row
    row = c.execute("SELECT * FROM risk_notifications WHERE notification_id=?",
                    (nid,)).fetchone()
    c.close()
    assert row["success"] == 1          # **未被改成 failed**
    cls = d.notification_classifications(nid)
    assert len(cls) == 1
    assert cls[0]["classification"] == ea.C_PROBE_FALSE_ALERT


# ---------------- 16–17. 研究视图合并 ----------------

def test_research_view_merges_artificial_split(env):
    """被人为切断的两段在研究视图里合并回一条连续区间。"""
    cfg, d = env
    ids = _make_split(d)
    for eid, seq in zip(ids, (1, 2)):
        ea.annotate_event(d, risk_event_id=eid,
                          annotation_type=ea.T_PROBE_INDUCED,
                          origin=ea.ORIGIN_PROBE,
                          reason_code="incomplete_market_context_side_effect",
                          reason_text="假恢复", episode_sequence=seq,
                          excluded_from_research=True,
                          excluded_from_precision_metrics=True,
                          incident_reference=INCIDENT, created_by="t", now=NOW)
    rows, _ = d.query_risk_events()
    m = ea.merge_artificial_splits(d, [dict(r) for r in rows])
    assert m["merged_count"] == 1
    mv = m["merged_detail"][0]
    assert mv["research_view_merged"] is True
    assert sorted(mv["merged_from_event_ids"]) == sorted(ids)
    assert mv["merged_episode_seqs"] == [1, 2]
    # 合并后是一段连续区间：起点取第一段，终点取最后一段
    assert mv["first_detected_at"] < mv["last_detected_at"]
    assert mv["status"] == "open"
    assert "由生产探测造成" in mv["merge_basis"]
    assert mv["merge_annotation_ids"]
    assert mv["original_records_unchanged"] is True
    # 合并后只剩一个样本，而不是两个
    assert len([e for e in m["events"] if e.get("identity") == "ident-qqq"
                or e.get("research_view_merged")]) == 1


def test_realtime_page_still_shows_full_timeline(env):
    """实时页面不受影响 —— 原始时间线完整显示，事件不被删除或隐藏。"""
    cfg, d = env
    ids = _make_split(d)
    ea.annotate_event(d, risk_event_id=ids[0],
                      annotation_type=ea.T_PROBE_INDUCED,
                      origin=ea.ORIGIN_PROBE,
                      reason_code="incomplete_market_context_side_effect",
                      reason_text="不完整市场上下文导致的假恢复",
                      episode_sequence=1, excluded_from_research=True,
                      incident_reference=INCIDENT, created_by="t", now=NOW)
    cl = TestClient(create_app(cfg, d))
    html = cl.get(f"/events/{ids[0]}").text
    assert "QQQ 跌破 50 日均线" in html          # 事件本身仍在
    assert ea.PROBE_NOTICE in html               # 且带上说明
    assert "probe_induced_transition" in html
    assert "完整原始记录" in html
    # 事件列表里也没被隐藏
    assert "QQQ" in cl.get("/events").text

    r = cl.get(f"/api/v1/events/{ids[0]}/annotations")
    assert r.status_code == 200
    data = r.json()["data"]
    assert len(data["annotations"]) == 1
    assert data["annotations"][0]["excluded_from_research"] == 1


def test_research_exclusion_api(env):
    cfg, d = env
    ids = _make_split(d)
    for eid, seq in zip(ids, (1, 2)):
        ea.annotate_event(d, risk_event_id=eid,
                          annotation_type=ea.T_PROBE_INDUCED,
                          origin=ea.ORIGIN_PROBE, reason_code="rc",
                          reason_text="r", episode_sequence=seq,
                          excluded_from_research=True,
                          incident_reference=INCIDENT, created_by="t", now=NOW)
    cl = TestClient(create_app(cfg, d))
    data = cl.get("/api/v1/research/exclusions").json()["data"]
    assert data["total_events"] == 2
    assert data["eligible_events"] == 0
    assert data["excluded_events"] == 2
    assert data["excluded_by_reason"] == {"rc": 2}
    assert data["research_view_merged_count"] == 1
    assert data["research_view_merged"][0]["merge_basis"]

    inc = cl.get("/api/v1/research/exclusions?include_excluded=true"
                 ).json()["data"]
    assert inc["include_excluded"] is True
    assert len(inc["events"]) == 2


def test_unannotated_events_behave_unchanged(env):
    """旧事件没有标注时行为完全不变。"""
    cfg, d = env
    _make_split(d)
    rows, _ = d.query_risk_events()
    res = ea.filter_for_research(d, [dict(r) for r in rows])
    assert res["stats"]["excluded_events"] == 0
    assert res["stats"]["eligible_events"] == 2
    assert ea.merge_artificial_splits(d, [dict(r) for r in rows])["merged_count"] == 0


def test_audit_records_completion(env):
    """审计必须收尾 —— 只记开始不记结束，等于没有可追溯性。"""
    cfg, d = env
    ctx = ec.sync_context(initiated_by="test.cycle")
    es.process_assessments(cfg, d, Recorder(), account=_account(),
                           market=_market(), now=NOW, execution_context=ctx)
    r = d.side_effect_executions()[0]
    assert r["finished_at"], "finished_at 未写入"
    assert r["affected_event_count"] >= 2
    assert r["error"] is None
    assert r["correlation_id"] == ctx.correlation_id
