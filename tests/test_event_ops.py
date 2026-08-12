"""风险事件运营层 v1 验收测试（对应清单 1–40）。

钉死四件事：
  1. 同一风险持续存在时不重复建事件、不重复轰炸飞书；
  2. acknowledged 与 resolved 严格区分，数据缺失绝不当成恢复；
  3. 降采样不掩盖区间内出现过的最高风险；
  4. 通知失败既不影响风险评估，也不会被记成「已通知」。
"""
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D

import pytest
from fastapi.testclient import TestClient

from app import combined_events as ce
from app import event_service as es
from app import notification_format as nf
from app import notification_policy as npol
from app import risk_events as rev
from app import risk_trends as rt
from app import execution_context as ec
from app.db import Database
from app.webapp import create_app

NOW = datetime(2026, 7, 28, 16, 0, tzinfo=timezone.utc)


class FakeNotifier:
    """可编程的假推送通道。fail_times 控制前 N 次失败。"""

    def __init__(self, fail_times: int = 0, raise_exc: bool = False):
        self.sent: list[tuple[str, str]] = []
        self.attempts = 0
        self.fail_times = fail_times
        self.raise_exc = raise_exc

    def send(self, title, body):
        self.attempts += 1
        if self.attempts <= self.fail_times:
            if self.raise_exc:
                raise ConnectionError("模拟飞书不可达")
            return False
        self.sent.append((title, body))
        return True


@pytest.fixture
def env(make_config):
    cfg = make_config(ib_account="U1234567")
    d = Database(cfg.db_path)
    d.init_schema()
    return cfg, d


def _trig(rule_code="CONC_TOP3_HIGH", severity="high", source=rev.SRC_ACCOUNT,
          value="0.85", symbol=None, version="risk-v1~aaa", **kw):
    return rev.Trigger(
        source_type=source, event_code=rule_code, rule_code=rule_code,
        category=kw.pop("category", "concentration"), severity=severity,
        title=kw.pop("title", "前三大持仓集中度过高"),
        explanation=kw.pop("explanation", "仓位过于集中"),
        actual_value=value, threshold_value="0.80", unit="ratio",
        affected_symbol=symbol, recommended_check="检查相关性",
        rule_set_version=version, data_as_of="2026-07-28T16:00:00+00:00",
        account_id="U1234567", **kw)


def _register(d, triggers, now=NOW, sources=(rev.SRC_ACCOUNT,), reliable=True):
    return es.register_triggers(d, triggers, now=now, evaluation_complete=True,
                                reliable=reliable,
                                scope={"source_types": list(sources)})


# ---------------- 1–3. 去重与累计 ----------------

def test_repeated_trigger_does_not_create_new_event(env):
    _, d = env
    for i in range(5):
        _register(d, [_trig()], now=NOW + timedelta(minutes=i))
    rows, total = d.query_risk_events()
    assert total == 1                      # 5 次评估，仍然只有一条事件
    assert rows[0]["episode_seq"] == 1


def test_occurrence_count_accumulates(env):
    _, d = env
    for i in range(4):
        _register(d, [_trig()], now=NOW + timedelta(minutes=i))
    rows, _ = d.query_risk_events()
    assert rows[0]["occurrence_count"] == 4


def test_warning_escalates_to_high(env):
    _, d = env
    _register(d, [_trig(severity="warning")])
    res = _register(d, [_trig(severity="high")], now=NOW + timedelta(minutes=1))
    dec = res[0][0]
    assert dec.transition == rev.TRANS_ESCALATED
    assert dec.new_severity == "high"
    rows, _ = d.query_risk_events()
    assert rows[0]["severity"] == "high"


def test_high_escalates_to_critical(env):
    _, d = env
    _register(d, [_trig(severity="high")])
    res = _register(d, [_trig(severity="critical")], now=NOW + timedelta(minutes=1))
    assert res[0][0].transition == rev.TRANS_ESCALATED
    trs = d.event_transitions(res[0][0].event_id)
    assert [t["transition"] for t in trs] == ["opened", "escalated"]


def test_critical_deescalates_to_high_keeps_max(env):
    _, d = env
    _register(d, [_trig(severity="critical")])
    res = _register(d, [_trig(severity="high")], now=NOW + timedelta(minutes=1))
    assert res[0][0].transition == rev.TRANS_DEESCALATED
    row = d.get_risk_event(res[0][0].event_id)
    assert row["severity"] == "high"
    # 降级后 max_severity 仍记得到达过 critical —— 只存最终状态是不够的
    assert row["max_severity"] == "critical"


# ---------------- 6–8. 恢复与 episode ----------------

def test_risk_resolves_when_rule_stops_firing(env):
    _, d = env
    _register(d, [_trig()])
    _register(d, [], now=NOW + timedelta(minutes=5))
    rows, _ = d.query_risk_events(status=["resolved"])
    assert len(rows) == 1
    assert rows[0]["resolved_at"] is not None


def test_retrigger_after_resolve_opens_new_episode(env):
    _, d = env
    _register(d, [_trig()])
    _register(d, [], now=NOW + timedelta(minutes=5))
    res = _register(d, [_trig()], now=NOW + timedelta(minutes=10))
    assert res[0][0].transition == rev.TRANS_REOPENED
    assert res[0][0].episode_seq == 2
    rows, total = d.query_risk_events()
    # 两轮独立记录：能分别复盘「持续了多久、中间恢复过没有」
    assert total == 2
    assert {r["episode_seq"] for r in rows} == {1, 2}


def test_missing_data_is_not_treated_as_recovery(env):
    _, d = env
    _register(d, [_trig()])
    # 本轮评估未完成（数据缺失）：绝不能判恢复
    es.register_triggers(d, [], now=NOW + timedelta(minutes=5),
                         evaluation_complete=False, reliable=True,
                         scope={"source_types": [rev.SRC_ACCOUNT]})
    rows, _ = d.query_risk_events()
    assert rows[0]["status"] == "open"
    assert rows[0]["resolved_at"] is None
    # 数据不可靠同样不判恢复
    es.register_triggers(d, [], now=NOW + timedelta(minutes=6),
                         evaluation_complete=True, reliable=False,
                         scope={"source_types": [rev.SRC_ACCOUNT]})
    assert d.query_risk_events()[0][0]["status"] == "open"


# ---------------- 9–11. 冷静期与通知 ----------------

def _dispatch(cfg, d, notifier, results, now=NOW):
    return es.dispatch(d, notifier, results, cfg=cfg, now=now)


def test_no_repeat_notification_within_cooldown(env):
    cfg, d = env
    n = FakeNotifier()
    _dispatch(cfg, d, n, _register(d, [_trig()]))
    assert len(n.sent) == 1
    # 同一事件在冷静期内再次评估：不再推送
    for i in range(1, 4):
        _dispatch(cfg, d, n, _register(d, [_trig()], now=NOW + timedelta(minutes=i)),
                  now=NOW + timedelta(minutes=i))
    assert len(n.sent) == 1


def test_escalation_bypasses_cooldown(env):
    cfg, d = env
    n = FakeNotifier()
    _dispatch(cfg, d, n, _register(d, [_trig(severity="high")]))
    assert len(n.sent) == 1
    later = NOW + timedelta(minutes=2)      # 远在 4 小时冷静期内
    _dispatch(cfg, d, n, _register(d, [_trig(severity="critical")], now=later),
              now=later)
    assert len(n.sent) == 2
    assert "升级" in n.sent[1][0]


def test_recovery_notified_once_only(env):
    cfg, d = env
    n = FakeNotifier()
    _dispatch(cfg, d, n, _register(d, [_trig()]))
    t1 = NOW + timedelta(minutes=5)
    _dispatch(cfg, d, n, _register(d, [], now=t1), now=t1)
    assert len(n.sent) == 2
    assert "已解除" in n.sent[1][0]
    # 已 resolved 的事件不会再产生第二条恢复通知
    t2 = NOW + timedelta(minutes=6)
    _dispatch(cfg, d, n, _register(d, [], now=t2), now=t2)
    assert len(n.sent) == 2


def test_multiple_market_events_merged_into_one(env):
    cfg, d = env
    n = FakeNotifier()
    trigs = [
        _trig("TREND_QQQ_BELOW_MA50", source=rev.SRC_MARKET, category="trend",
              title="QQQ 跌破 50 日均线", version="market-v1~b"),
        _trig("TREND_SOX_WEAKER", source=rev.SRC_MARKET, category="trend",
              title="半导体明显弱于大盘", version="market-v1~b"),
        _trig("VOL_BOTH_RISING", source=rev.SRC_MARKET, category="volatility",
              title="VIX 与 VXN 同步上升", version="market-v1~b"),
        _trig("VOL_VIX_HIGH", source=rev.SRC_MARKET, severity="critical",
              category="volatility", title="VIX 处于高位", version="market-v1~b"),
    ]
    _dispatch(cfg, d, n, _register(d, trigs, sources=(rev.SRC_MARKET,)))
    # 四条相关事件 -> 一条通知，不是四条飞书
    assert len(n.sent) == 1
    title, body = n.sent[0]
    assert "4 项" in title
    for t in ("QQQ 跌破 50 日均线", "半导体明显弱于大盘", "VIX 处于高位"):
        assert t in body
    for rule_id in ("TREND_QQQ_BELOW_MA50", "TREND_SOX_WEAKER", "VOL_VIX_HIGH"):
        assert f"规则 {rule_id}" in body


# ---------------- 13–14. 通知失败 ----------------

def test_notification_failure_does_not_break_assessment(env):
    cfg, d = env
    n = FakeNotifier(fail_times=99, raise_exc=True)
    results = _register(d, [_trig()])
    summary = es.dispatch(d, n, results, cfg=cfg, now=NOW)   # 不抛异常
    assert summary["failed"] >= 1
    # 事件本身照常落库，风险记录不受推送失败影响
    rows, total = d.query_risk_events()
    assert total == 1 and rows[0]["status"] == "open"
    # 失败绝不能被记成已通知，否则冷静期会吞掉下一次真实告警
    assert rows[0]["notification_count"] == 0
    assert rows[0]["last_notified_at"] is None


def test_notification_retries_are_bounded(env):
    cfg, d = env
    n = FakeNotifier(fail_times=1)          # 第一次失败，第二次成功
    _dispatch(cfg, d, n, _register(d, [_trig()]))
    assert n.attempts == 2 and len(n.sent) == 1
    notes = d.notifications_between("2000-01-01", "2100-01-01", success_only=False)
    assert notes[0]["retry_count"] == 1 and notes[0]["success"] == 1

    # 一直失败时不会无限重试：默认 max_retries=2 -> 最多 3 次尝试
    cfg2, d2 = cfg, Database(str(cfg.db_path) + "2")
    d2.init_schema()
    n2 = FakeNotifier(fail_times=99)
    es.dispatch(d2, n2, _register(d2, [_trig()]), cfg=cfg2, now=NOW)
    assert n2.attempts == 3


# ---------------- 15–17. 抑制与确认 ----------------

def test_suppressed_event_is_not_notified(env):
    cfg, d = env
    n = FakeNotifier()
    res = _register(d, [_trig()])
    eid = res[0][0].event_id
    d.suppress_event(eid, until_iso=(NOW + timedelta(hours=6)).isoformat(),
                     reason="已知问题", now_iso=NOW.isoformat())
    # 恢复后再触发（会产生新 episode 通知候选），但事件处于抑制态
    t = NOW + timedelta(minutes=1)
    summary = es.dispatch(d, n, _register(d, [_trig()], now=t), cfg=cfg, now=t)
    assert len(n.sent) == 0
    assert npol.SKIP_SUPPRESSED in summary["reasons"]


def test_suppression_expires_and_restores_open(env):
    _, d = env
    res = _register(d, [_trig()])
    eid = res[0][0].event_id
    d.suppress_event(eid, until_iso=(NOW + timedelta(hours=1)).isoformat(),
                     reason="临时", now_iso=NOW.isoformat())
    assert d.get_risk_event(eid)["status"] == "suppressed"
    # 到期前不解除
    d.expire_suppressions((NOW + timedelta(minutes=30)).isoformat())
    assert d.get_risk_event(eid)["status"] == "suppressed"
    # 到期后自动回到 open，事件不删除
    d.expire_suppressions((NOW + timedelta(hours=2)).isoformat())
    row = d.get_risk_event(eid)
    assert row["status"] == "open" and row["suppressed_until"] is None


def test_acknowledged_is_not_resolved(env):
    _, d = env
    res = _register(d, [_trig()])
    eid = res[0][0].event_id
    d.acknowledge_event(eid, now_iso=NOW.isoformat(), actor="tester")
    row = d.get_risk_event(eid)
    assert row["status"] == "acknowledged"
    # 关键：确认不改变「风险是否仍然存在」
    assert row["resolved_at"] is None
    assert row["status"] != "resolved"
    # 且仍被计入未恢复事件
    assert d.oldest_unresolved_event()["event_id"] == eid


def test_escalation_pulls_acknowledged_back_to_open(env):
    _, d = env
    res = _register(d, [_trig(severity="high")])
    eid = res[0][0].event_id
    d.acknowledge_event(eid, now_iso=NOW.isoformat(), actor="tester")
    t = NOW + timedelta(minutes=1)
    _register(d, [_trig(severity="critical")], now=t)
    # 用户确认的是**当时那个级别**的风险，升级后需要重新进入视野
    assert d.get_risk_event(eid)["status"] == "open"


# ---------------- 18. 规则版本 ----------------

def test_different_rule_version_does_not_merge(env):
    _, d = env
    _register(d, [_trig(version="risk-v1~aaa")])
    res = _register(d, [_trig(version="risk-v1~bbb")], now=NOW + timedelta(minutes=1))
    assert res[0][0].transition == rev.TRANS_REOPENED
    assert res[0][0].episode_seq == 2
    _, total = d.query_risk_events()
    assert total == 2       # 阈值改了之后的「超限」不是同一件事


# ---------------- 19–24. 查询 ----------------

def test_query_by_source_type(env):
    _, d = env
    _register(d, [_trig(), _trig("TREND_QQQ_BELOW_MA50", source=rev.SRC_MARKET),
                  _trig("SEMI_X", source=rev.SRC_COMBINED)],
              sources=(rev.SRC_ACCOUNT, rev.SRC_MARKET, rev.SRC_COMBINED))
    for src, n in ((rev.SRC_ACCOUNT, 1), (rev.SRC_MARKET, 1), (rev.SRC_COMBINED, 1)):
        _, total = d.query_risk_events(source_type=[src])
        assert total == n, src
    _, total = d.query_risk_events()
    assert total == 3


def test_query_by_symbol(env):
    _, d = env
    _register(d, [_trig("POS_LOSS", symbol="MRVL"),
                  _trig("POS_LOSS2", symbol="AAPL")])
    rows, total = d.query_risk_events(symbol="MRVL")
    assert total == 1 and rows[0]["affected_symbol"] == "MRVL"


def test_event_detail_includes_transition_history(env, make_config):
    cfg, d = env
    _register(d, [_trig(severity="warning")])
    res = _register(d, [_trig(severity="critical")], now=NOW + timedelta(minutes=1))
    eid = res[0][0].event_id
    c = TestClient(create_app(cfg, d))
    body = c.get(f"/api/v1/risk/events/{eid}").json()
    assert body["success"] is True
    trs = body["data"]["transitions"]
    assert [t["transition"] for t in trs] == ["opened", "escalated"]
    assert trs[1]["previous_severity"] == "warning"
    assert trs[1]["new_severity"] == "critical"


def test_notification_history_query(env):
    """通知历史接口按**墙钟**回溯 N 小时，所以必须锚在真实 now 上派发。

    用固定的 NOW 派发，写的当天能过，48 小时后那条通知就滑出窗口了 ——
    这类测试是定时炸弹（本文件的采样播种此前已因同样原因修过一轮）。
    """
    cfg, d = env
    n = FakeNotifier()
    real_now = datetime.now(timezone.utc)
    res = _register(d, [_trig()], now=real_now)
    _dispatch(cfg, d, n, res, now=real_now)
    c = TestClient(create_app(cfg, d))
    body = c.get("/api/v1/risk/notifications?hours=48").json()
    assert body["data"]["total"] == 1
    rec = body["data"]["notifications"][0]
    assert rec["success"] is True and rec["notification_type"] == "opened"


# ---------------- 25–29. 趋势与降采样 ----------------

def _ago(**kw) -> datetime:
    """相对**真实**当前时间的锚点。

    /risk/trends 的 range 是滚动窗口，按墙钟算。用固定的 NOW 播种，
    写的当天能过，过几天就全部掉出窗口 —— 这类测试是定时炸弹，
    只有锚在真实 now 上才一直成立。
    """
    return datetime.now(timezone.utc) - timedelta(**kw)


def _seed_assessments(d, start: datetime, count: int, *, scores, levels,
                      step_min=1):
    for i in range(count):
        ts = (start + timedelta(minutes=i * step_min)).isoformat()
        d.save_risk_assessment({
            "assessment_id": f"a{i}", "snapshot_id": f"s{i}", "account_id": "U1234567",
            "data_as_of": ts, "risk_level": levels[i % len(levels)],
            "risk_score": str(scores[i % len(scores)]), "reliable": True,
            "component_scores": {"concentration_score": "50"},
            "calculated_metrics": {}, "triggered_rules": [], "top_risk_drivers": [],
            "data_quality_status": "fresh", "ai_analysis_allowed": True,
            "calculation_version": "v1", "created_at": ts, "events": []})


def test_24h_trend_uses_raw_granularity(env):
    cfg, d = env
    _seed_assessments(d, _ago(hours=2), 30,
                      scores=[10, 20, 30], levels=["low", "moderate"])
    c = TestClient(create_app(cfg, d))
    data = c.get("/api/v1/risk/trends?range=24h").json()["data"]
    assert data["interval"] == "raw"
    assert len(data["account"]) == 30


def test_7d_downsamples_to_hourly(env):
    cfg, d = env
    _seed_assessments(d, _ago(days=2), 240,
                      scores=[10, 20], levels=["low"], step_min=10)
    c = TestClient(create_app(cfg, d))
    data = c.get("/api/v1/risk/trends?range=7d").json()["data"]
    assert data["interval"] == "1h"
    # 240 个 10 分钟点 -> 约 40 个小时桶，远少于原始点数
    assert 30 <= len(data["account"]) <= 45


def test_30d_downsamples_further(env):
    cfg, d = env
    _seed_assessments(d, _ago(days=10), 480,
                      scores=[15], levels=["low"], step_min=30)
    c = TestClient(create_app(cfg, d))
    data = c.get("/api/v1/risk/trends?range=30d").json()["data"]
    assert data["interval"] == "6h"
    assert len(data["account"]) < 100
    d90 = c.get("/api/v1/risk/trends?range=90d").json()["data"]
    assert d90["interval"] == "1d"
    assert len(d90["account"]) <= 11


def test_peak_risk_not_hidden_by_average(env):
    """区间内出现过 critical，聚合后必须仍能看见 —— 这是降采样最危险的一步。"""
    _, d = env
    rows = [
        {"data_as_of": "2026-07-28T10:00:00+00:00", "risk_score": "10",
         "risk_level": "low", "reliable": 1, "component_scores": "{}"},
        {"data_as_of": "2026-07-28T10:20:00+00:00", "risk_score": "95",
         "risk_level": "critical", "reliable": 1, "component_scores": "{}"},
        {"data_as_of": "2026-07-28T10:40:00+00:00", "risk_score": "12",
         "risk_level": "low", "reliable": 1, "component_scores": "{}"},
    ]
    buckets = rt.aggregate_account(rows, "1h")
    assert len(buckets) == 1
    b = buckets[0]
    assert b["account_risk_score"]["max"] == "95"        # 峰值保留
    assert b["account_risk_score"]["last"] == "12"       # 末值另给
    assert b["highest_risk_level"] == "critical"         # 等级取桶内最高
    # 若只看均值会得到 39，看起来毫无危险 —— 所以均值绝不能是唯一输出
    assert D(b["account_risk_score"]["avg"]) < D("40")


def test_missing_intervals_are_reported(env):
    cfg, d = env
    _seed_assessments(d, _ago(days=3), 5,
                      scores=[10], levels=["low"], step_min=60)
    c = TestClient(create_app(cfg, d))
    data = c.get("/api/v1/risk/trends?range=7d").json()["data"]
    assert data["has_gaps"] is True
    assert len(data["gaps"]) >= 1
    assert all("start" in g and "end" in g for g in data["gaps"])


# ---------------- 30–33. 联合事件 ----------------

def _account(*, semi_weight=None, leverage=None, drawdown=None, cash=None,
             reliable=True):
    positions, classes = [], {}
    if semi_weight is not None:
        positions.append({"symbol": "MRVL",
                          "weight_of_invested_capital": str(semi_weight)})
        classes["MRVL"] = {"sector": "半导体", "industry": "Semiconductors"}
    return {
        "assessment_id": "acc-1", "snapshot_id": "snap-1", "reliable": reliable,
        "risk_level": "moderate", "data_as_of": "2026-07-28T16:00:00+00:00",
        "quality_status": "fresh",
        "calculated_metrics": {
            "account": {"gross_leverage": str(leverage) if leverage else None,
                        "cash_ratio": str(cash) if cash else None},
            "drawdown": {"current_drawdown": str(drawdown) if drawdown else None},
            "concentration": {"classifications": classes},
            "positions": positions,
        },
    }


def _market(state="stressed", rules=(), reliable=True, vix=None):
    return {"assessment_id": "mkt-1", "market_state": state, "reliable": reliable,
            "data_as_of": "2026-07-28", "triggered_rules": list(rules),
            "calculated_metrics": {"vol": {"vix_current": vix}}}


def test_semiconductor_concentration_with_sector_weakness():
    evs = ce.evaluate(_account(semi_weight="0.42"),
                      _market(rules=["TREND_SOX_WEAKER"]))
    codes = {e.event_code for e in evs}
    assert ce.E_SEMI in codes
    ev = next(e for e in evs if e.event_code == ce.E_SEMI)
    assert "MRVL" in ev.explanation and "42.0%" in ev.explanation
    # 可追溯到两侧评估
    assert ev.account_assessment_id == "acc-1"
    assert ev.market_assessment_id == "mkt-1"
    # 行业没转弱时不触发
    assert not ce.evaluate(_account(semi_weight="0.42"), _market(rules=[]))


def test_leverage_with_volatility_spike():
    evs = ce.evaluate(_account(leverage="2.0"),
                      _market(rules=["VOL_VIX_SPIKE"], vix="34"))
    assert {e.event_code for e in evs} == {ce.E_LEVERAGE_VOL}
    # 杠杆未超阈值不触发
    assert not ce.evaluate(_account(leverage="1.1"),
                           _market(rules=["VOL_VIX_SPIKE"]))


def test_drawdown_with_stressed_market():
    evs = ce.evaluate(_account(drawdown="0.18"), _market(state="stressed"))
    assert {e.event_code for e in evs} == {ce.E_DRAWDOWN_STRESSED}
    # 市场未承压不触发
    assert not ce.evaluate(_account(drawdown="0.18"), _market(state="neutral"))


def test_high_cash_is_buffer_never_a_risk_event():
    acc, mkt = _account(cash="0.55"), _market(state="stressed")
    # 绝不能出现在事件里
    assert ce.evaluate(acc, mkt) == []
    buf = ce.cash_buffer(acc, mkt)
    assert buf is not None
    assert buf["kind"] == "buffer"
    assert "severity" not in buf          # buffer 没有严重度，结构上就无法变成告警
    assert "缓冲" in buf["title"]


def test_combined_events_require_both_sides_reliable():
    assert ce.evaluate(_account(semi_weight="0.42", reliable=False),
                       _market(rules=["TREND_SOX_WEAKER"])) == []
    assert ce.evaluate(_account(semi_weight="0.42"),
                       _market(rules=["TREND_SOX_WEAKER"], reliable=False)) == []


# ---------------- 34–35. 前端 ----------------

def test_page_shows_open_counts(env):
    cfg, d = env
    _register(d, [_trig(severity="critical", rule_code="A"),
                  _trig(severity="high", rule_code="B"),
                  _trig(severity="warning", rule_code="C")])
    html = TestClient(create_app(cfg, d)).get("/events").text
    assert "风险摘要" in html
    assert "进行中 · 严重" in html and "进行中 · 高风险" in html
    counts = d.event_counts_by_severity()
    assert counts == {"critical": 1, "high": 1, "warning": 1}


def test_page_shows_resolved_and_acknowledged_with_text_not_only_color(env):
    cfg, d = env
    res = _register(d, [_trig()])
    eid = res[0][0].event_id
    d.acknowledge_event(eid, now_iso=NOW.isoformat(), actor="tester")
    html = TestClient(create_app(cfg, d)).get("/events").text
    # 状态必须有文字，不能只靠颜色
    assert "已确认" in html
    assert "已确认，风险仍存在" in html
    detail = TestClient(create_app(cfg, d)).get(f"/events/{eid}").text
    assert "确认不等于恢复" in detail


# ---------------- 36–39. 契约 ----------------

def test_unified_api_response_shape(env):
    cfg, d = env
    c = TestClient(create_app(cfg, d))
    for path in ("/api/v1/risk/events", "/api/v1/risk/trends",
                 "/api/v1/risk/notifications"):
        body = c.get(path).json()
        assert set(body) == {"success", "data", "error",
                             "request_id", "generated_at"}
        assert body["success"] is True and body["error"] is None
    miss = c.get("/api/v1/risk/events/nope").json()
    assert miss["success"] is False and miss["error"]["code"] == "NOT_FOUND"


def test_decimal_values_serialize_as_strings(env):
    cfg, d = env
    _seed_assessments(d, _ago(hours=1), 5,
                      scores=[D("33.3")], levels=["moderate"])
    c = TestClient(create_app(cfg, d))
    data = c.get("/api/v1/risk/trends?range=24h").json()["data"]
    v = data["account"][0]["account_risk_score"]["last"]
    assert isinstance(v, str) and v == "33.3"
    _register(d, [_trig(value="0.8204172146272926461282763820")])
    ev = c.get("/api/v1/risk/events").json()["data"]["events"][0]
    assert isinstance(ev["actual_value"], str)
    assert ev["actual_value"].startswith("0.82041721")     # 精度不丢


def test_timezone_declared_and_naive_times_handled(env):
    cfg, d = env
    c = TestClient(create_app(cfg, d))
    data = c.get("/api/v1/risk/trends?range=7d").json()["data"]
    assert data["timezone"] == "UTC"
    assert data["start"].endswith("+00:00") and data["end"].endswith("+00:00")
    # 冷静期判定遇到无时区时间不应崩溃
    assert npol._in_cooldown("2026-07-28T15:00:00", NOW, timedelta(hours=4)) is True
    assert npol._in_cooldown("2026-07-28T09:00:00", NOW, timedelta(hours=4)) is False


def test_no_credentials_in_notification_records(env):
    """凭证不得出现在落库记录或推送正文里。

    cfg 里放了三类真实敏感值：Webhook token、加密密钥、Flex token。
    格式化层只接触事件字段，所以它们无从泄漏 —— 这个测试守住的是
    「以后有人图省事把 cfg 整个塞进 payload」这类改动。
    """
    cfg, d = env
    secrets = {
        "apprise_urls": ["https://open.feishu.cn/hook/SECRET-WEBHOOK-123"],
        "flex_token": "SECRET-FLEX-456",
    }
    cfg = type(cfg)(**{**cfg.__dict__, **secrets})
    n = FakeNotifier()
    _dispatch(cfg, d, n, _register(d, [_trig()]))

    notes = d.notifications_between("2000-01-01", "2100-01-01", success_only=False)
    assert notes and notes[0]["payload_summary"]      # 确实写了内容再断言没泄漏
    blob = str(notes) + str(n.sent)
    for leak in ("SECRET-WEBHOOK-123", "SECRET-FLEX-456", "open.feishu.cn",
                 cfg.account_enc_key):
        assert leak not in blob, f"凭证泄漏：{leak[:12]}"
    # 落库的确实是风险内容，不是空字符串蒙混过关
    assert "集中度" in notes[0]["payload_summary"]


def test_notifications_carry_manual_execution_disclaimer(env):
    """措辞禁令已解除，但每条通知仍须带「系统不下单、需自行执行」尾注。"""
    cfg, d = env
    n = FakeNotifier()
    _dispatch(cfg, d, n, _register(d, [_trig(severity="critical")]))
    title, body = n.sent[0]
    assert nf.DISCLAIMER in body


# ---------------- 40. R1–R7 回归 ----------------

def test_legacy_r1_r7_alerting_still_intact():
    """既有告警状态机语义不得被运营层改动。"""
    from app import alerting
    assert alerting.decide_notify(None, severity="WARN", value=D("0.8"),
                                  now_et_date="2026-07-28") == (True, alerting.FIRST_TRIGGER)
    state = {"state": "ACTIVE", "notified_severity": "WARN", "notified_value": "0.80",
             "last_summary_date": "2026-07-28", "resolved_at": None}
    should, reason = alerting.decide_notify(state, severity="CRIT", value=D("0.9"),
                                            now_et_date="2026-07-28", rule_id="R1")
    assert should and reason == alerting.ESCALATION
    should, reason = alerting.decide_notify(state, severity="WARN", value=D("0.81"),
                                            now_et_date="2026-07-28", rule_id="R1")
    assert not should and reason == alerting.SUPPRESSED
    assert alerting.decide_resolve({"state": "ACTIVE"}) is True


def test_legacy_events_are_registered_but_not_notified_by_ops(env):
    """R1–R7 进时间线，但通知仍归既有链路 —— 不双发。"""
    cfg, d = env

    class Hit:
        rule_id, dedup_key, symbol = "R1", "R1:MRVL", "MRVL"
        severity, title, message, value = "CRIT", "单一标的集中度超限", "占比过高", D("0.42")

    n = FakeNotifier()
    trig = rev.from_legacy_hit(Hit(), account_id="U1234567", now_iso=NOW.isoformat())
    assert trig.source_type == rev.SRC_BEHAVIOR
    assert trig.severity == "critical"          # CRIT -> critical 归一
    assert trig.notification_owner == "legacy"
    results = _register(d, [trig], sources=(rev.SRC_BEHAVIOR,))
    summary = es.dispatch(d, n, results, cfg=cfg, now=NOW)
    # 事件已登记，可在统一时间线查询
    rows, total = d.query_risk_events(source_type=[rev.SRC_BEHAVIOR])
    assert total == 1 and rows[0]["rule_code"] == "R1"
    # 但运营层没有推送 —— 否则同一风险会被推两次
    assert len(n.sent) == 0
    assert summary["reasons"].get(npol.SKIP_LEGACY_OWNER) == 1


# ---------------- 补充：策略与默认值 ----------------

def test_warning_not_notified_by_default(env):
    cfg, d = env
    n = FakeNotifier()
    summary = _dispatch(cfg, d, n, _register(d, [_trig(severity="warning")]))
    assert len(n.sent) == 0
    assert summary["reasons"].get(npol.SKIP_SEVERITY) == 1
    # 但事件仍进时间线
    assert d.query_risk_events()[1] == 1


def test_rate_limit_caps_notifications(env):
    cfg, d = env
    cfg = type(cfg)(**{**cfg.__dict__, "alert_max_per_hour": 1})
    n = FakeNotifier()
    _dispatch(cfg, d, n, _register(d, [_trig(rule_code="A", severity="high")]))
    assert len(n.sent) == 1
    t = NOW + timedelta(minutes=1)
    summary = es.dispatch(
        d, n, _register(d, [_trig(rule_code="A", severity="high"),
                            _trig(rule_code="B", severity="high")], now=t),
        cfg=cfg, now=t)
    assert len(n.sent) == 1                       # 被限流挡住
    assert npol.SKIP_RATE_LIMIT in summary["reasons"]


def test_test_notification_is_clearly_marked(env):
    cfg, d = env
    n = FakeNotifier()
    res = es.send_test_notification(cfg, d, n, now=NOW)
    assert res["success"] is True
    title, body = n.sent[0]
    # 必须明确标记，否则用户会以为账户真的出事了
    assert "【测试】" in title
    assert "测试通知" in body and "不代表账户发生了新风险" in body
    assert nf.DISCLAIMER in body


def test_event_identity_is_stable_and_excludes_severity():
    a = rev.event_identity(source_type="account", rule_code="CONC_TOP3_HIGH",
                           affected=None, account_id="U1")
    b = rev.event_identity(source_type="account", rule_code="CONC_TOP3_HIGH",
                           affected=None, account_id="U1")
    assert a == b
    # 不同标的是不同事件
    c = rev.event_identity(source_type="account", rule_code="CONC_TOP3_HIGH",
                           affected="MRVL", account_id="U1")
    assert c != a


def test_process_assessments_never_raises(env):
    """运营层是账户同步的下游，它出问题绝不能反过来影响同步。"""
    cfg, d = env

    class Broken:
        def send(self, *a, **k):
            raise RuntimeError("通道炸了")

    out = es.process_assessments(
        cfg, d, Broken(),
        account={"assessment_id": "a", "snapshot_id": "s", "reliable": True,
                 "risk_level": "high", "data_as_of": "2026-07-28T16:00:00+00:00",
                 "quality_status": "fresh", "calculated_metrics": {},
                 "rule_set_version": "v~1",
                 "events": [{"event_code": "X", "rule_code": "X",
                             "category": "concentration", "severity": "critical",
                             "title": "T", "explanation": "E"}]},
        execution_context=ec.test_context(),
        market=None, now=NOW)
    assert out["triggers"] == 1
    assert d.query_risk_events()[1] == 1          # 事件照常落库
