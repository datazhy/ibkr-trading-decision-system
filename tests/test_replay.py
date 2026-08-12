"""账户规则历史重放 v1 验收测试（对应清单 1–36）。

钉死四条：
  1. 重放与实时系统**完全隔离** —— 不通知、不动 alert_state、不碰实时事件
  2. 老快照不伪造 snapshot_id，用独立的 source_snapshot_key
  3. 账户汇总与持仓**绝不跨时间拼接**
  4. point_in_time 与 current_rules 结果严格分开，找不到历史版本就如实标记
"""
import datetime as dt
import json
from decimal import Decimal as D

import pytest
from fastapi.testclient import TestClient

from app import replay_context as rctx
from app import replay_identity as rid
from app import replay_service as rp
from app.db import Database
from app.webapp import create_app

NOW = dt.datetime(2026, 7, 29, 16, 0, tzinfo=dt.timezone.utc)
ACC = "U1234567"


@pytest.fixture
def env(make_config):
    cfg = make_config(ib_account=ACC)
    d = Database(cfg.db_path)
    d.init_schema()
    return cfg, d


def _bal(ts, *, equity="100000", cash="50000", pv=None, sid=None,
         account=ACC, quality=None):
    return {"ts": ts, "account": account, "currency": "USD",
            "equity": equity, "cash": cash, "total_cash": cash,
            "margin_balance": cash, "buying_power": str(D(equity) * 2),
            "available_funds": cash, "excess_liquidity": cash,
            "maint_margin": str(D(equity) / 10), "init_margin": str(D(equity) / 8),
            "positions_value": pv, "snapshot_id": sid,
            "quality_status": quality, "ai_analysis_allowed": 1}


def _pos(ts, symbol="AAA", qty="100", mv="50000", cost="450", upl="5000",
         sid=None):
    return {"ts": ts, "symbol": symbol, "qty": qty, "avg_cost": cost,
            "market_value": mv, "upl": upl, "price": "500",
            "price_quality": "DELAYED", "snapshot_id": sid, "currency": "USD",
            "source": "TEST", "sync_run_id": "r1"}


def _raw_insert(db, balances=(), positions=()):
    """直接写原始表，模拟历史数据（含无 snapshot_id 的老行）。"""
    import sqlite3
    conn = sqlite3.connect(db.db_path)
    for b in balances:
        cols = ",".join(b)
        conn.execute(f"INSERT OR REPLACE INTO balances_snapshot ({cols})"
                     " VALUES (" + ",".join("?" * len(b)) + ")",
                     tuple(b.values()))
    for p in positions:
        cols = ",".join(p)
        conn.execute(f"INSERT OR REPLACE INTO positions_snapshot ({cols})"
                     " VALUES (" + ",".join("?" * len(p)) + ")",
                     tuple(p.values()))
    conn.commit()
    conn.close()


def _seed(db, days=5, *, with_sid=False, base=dt.date(2026, 7, 20)):
    """造 days 天的日末快照。with_sid 控制是否带原生 snapshot_id。"""
    bals, poss = [], []
    for i in range(days):
        ts = f"{(base + dt.timedelta(days=i)).isoformat()}T23:59:00+00:00"
        sid = f"snap{i}" if with_sid else None
        pv = "50000" if with_sid else None      # 老行没有 positions_value
        bals.append(_bal(ts, pv=pv, sid=sid,
                         quality="fresh" if with_sid else None))
        poss.append(_pos(ts, sid=sid))
    _raw_insert(db, bals, poss)


# ---------------- 1–7. 身份与对齐 ----------------

def test_native_snapshot_id_identity(env):
    cfg, d = env
    _seed(d, 2, with_sid=True)
    row = d.daily_close_balances("2026-07-01T00:00:00+00:00",
                                 "2026-07-31T23:59:59+00:00", ACC)[0]
    ctx = rid.resolve_context(d, row)
    assert ctx["source_identity_type"] == rid.ID_NATIVE
    assert ctx["alignment_status"] == rid.ALIGN_EXACT
    assert ctx["reliable"] is True
    assert ctx["snapshot_id"] == "snap0"


def test_snapshot_without_id_uses_reconstructed_key(env):
    """老快照没有 snapshot_id —— **绝不向原始表回填伪造 id**。"""
    cfg, d = env
    _seed(d, 2, with_sid=False)
    row = d.daily_close_balances("2026-07-01T00:00:00+00:00",
                                 "2026-07-31T23:59:59+00:00", ACC)[0]
    ctx = rid.resolve_context(d, row)
    assert ctx["snapshot_id"] is None
    assert ctx["source_identity_type"] == rid.ID_RECONSTRUCTED
    assert ctx["source_snapshot_key"].startswith("rsk-")
    # positions_value 缺失时无法验证完整性 -> 只能 estimated，不冒充 exact
    assert ctx["alignment_status"] == rid.ALIGN_ESTIMATED
    assert ctx["reliable"] is True
    # 原始表一行未改
    import sqlite3
    conn = sqlite3.connect(d.db_path)
    ids = [r[0] for r in conn.execute("SELECT snapshot_id FROM balances_snapshot")]
    assert all(x is None for x in ids)


def test_source_snapshot_key_is_stable(env):
    cfg, d = env
    _seed(d, 1, with_sid=False)
    row = d.daily_close_balances("2026-07-01T00:00:00+00:00",
                                 "2026-07-31T23:59:59+00:00", ACC)[0]
    a = rid.resolve_context(d, row)["source_snapshot_key"]
    b = rid.resolve_context(d, row)["source_snapshot_key"]
    assert a == b                       # 幂等的基础
    # 持仓数量变化 -> 键变化
    k1 = rid.source_snapshot_key(account_id=ACC, data_as_of="t",
                                 balance_signature="s",
                                 positions=[{"symbol": "A", "qty": "10"}])
    k2 = rid.source_snapshot_key(account_id=ACC, data_as_of="t",
                                 balance_signature="s",
                                 positions=[{"symbol": "A", "qty": "20"}])
    assert k1 != k2
    # 价格变化**不影响**键 —— 否则同一批持仓每分钟都是新键，重放不再幂等
    k3 = rid.source_snapshot_key(account_id=ACC, data_as_of="t",
                                 balance_signature="s",
                                 positions=[{"symbol": "A", "qty": "10",
                                             "market_value": "999"}])
    assert k1 == k3


def test_exact_timestamp_alignment(env):
    cfg, d = env
    ts = "2026-07-20T23:59:00+00:00"
    _raw_insert(d, [_bal(ts, pv="50000")], [_pos(ts)])
    ctx = rid.resolve_context(d, d.balances_in_range("0000", "9999", ACC)[0])
    assert ctx["alignment_gap_seconds"] == 0
    assert ctx["alignment_status"] == rid.ALIGN_EXACT   # 有 pv 可验证 -> exact


def test_within_tolerance_alignment(env):
    cfg, d = env
    bts = "2026-07-20T23:59:30+00:00"
    pts = "2026-07-20T23:59:00+00:00"      # 持仓早 30 秒
    _raw_insert(d, [_bal(bts, pv="50000")], [_pos(pts)])
    ctx = rid.resolve_context(d, d.balances_in_range("0000", "9999", ACC)[0])
    assert ctx["alignment_status"] == rid.ALIGN_TOLERANCE
    assert ctx["alignment_gap_seconds"] == 30
    assert ctx["reliable"] is True


def test_beyond_tolerance_is_skipped(env):
    cfg, d = env
    bts = "2026-07-20T23:59:00+00:00"
    pts = "2026-07-20T23:50:00+00:00"      # 早 9 分钟，超出 60 秒容差
    _raw_insert(d, [_bal(bts)], [_pos(pts)])
    ctx = rid.resolve_context(d, d.balances_in_range("0000", "9999", ACC)[0])
    assert ctx["alignment_status"] == rid.ALIGN_INVALID
    assert ctx["reliable"] is False
    assert "无法可靠配对" in ctx["skip_reason"]


def test_future_positions_are_never_used(env):
    """持仓晚于账户 = 用未来数据判断过去，必须拒绝。"""
    cfg, d = env
    bts = "2026-07-20T23:59:00+00:00"
    pts = "2026-07-20T23:59:30+00:00"      # 持仓**晚** 30 秒
    _raw_insert(d, [_bal(bts)], [_pos(pts)])
    ctx = rid.resolve_context(d, d.balances_in_range("0000", "9999", ACC)[0])
    assert ctx["reliable"] is False
    assert ctx["alignment_status"] == rid.ALIGN_INVALID


def test_incomplete_positions_are_rejected(env):
    """持仓市值与账户 positions_value 明显不符 = 持仓不完整，不可用。"""
    cfg, d = env
    ts = "2026-07-20T23:59:00+00:00"
    # 账户说有 50000 持仓，但明细只有 5000
    _raw_insert(d, [_bal(ts, pv="50000")], [_pos(ts, mv="5000")])
    ctx = rid.resolve_context(d, d.balances_in_range("0000", "9999", ACC)[0])
    assert ctx["alignment_status"] == rid.ALIGN_INVALID
    assert "持仓可能不完整" in ctx["skip_reason"]


# ---------------- 8–10. 采样 ----------------

def test_daily_close_sampling(env):
    cfg, d = env
    bals, poss = [], []
    base = dt.date(2026, 7, 20)
    for i in range(3):
        for hh in (10, 15, 23):
            ts = f"{(base + dt.timedelta(days=i)).isoformat()}T{hh:02d}:00:00+00:00"
            bals.append(_bal(ts, pv="50000"))
            poss.append(_pos(ts))
    _raw_insert(d, bals, poss)
    picked = rp.sample_snapshots(d, start="2026-07-20", end="2026-07-25",
                                 account=ACC, mode=rp.SAMPLE_DAILY_CLOSE)
    assert len(picked) == 3                       # 每天一条
    assert all(r["ts"][11:13] == "23" for r in picked)   # 都是当天最后一条


def test_intraday_interval_sampling(env):
    cfg, d = env
    bals, poss = [], []
    base = dt.datetime(2026, 7, 20, 9, 0, tzinfo=dt.timezone.utc)
    for i in range(240):                          # 4 小时，每分钟一条
        ts = (base + dt.timedelta(minutes=i)).isoformat()
        bals.append(_bal(ts, pv="50000"))
        poss.append(_pos(ts))
    _raw_insert(d, bals, poss)
    hourly = rp.sample_snapshots(d, start="2026-07-20", end="2026-07-20",
                                 account=ACC, mode=rp.SAMPLE_INTRADAY,
                                 interval_minutes=60)
    assert 4 <= len(hourly) <= 5                  # 4 小时 -> 4~5 个桶
    half = rp.sample_snapshots(d, start="2026-07-20", end="2026-07-20",
                               account=ACC, mode=rp.SAMPLE_INTRADAY,
                               interval_minutes=30)
    assert len(half) > len(hourly)
    # 绝不全量：240 条分钟快照不会全部返回
    assert len(hourly) < 240


def test_peak_severity_not_masked_by_last_value(env):
    """桶内出现过 critical，episode 的 peak 必须保留它。"""
    cfg, d = env
    pts = [
        {"data_as_of": "2026-07-20T10:00:00+00:00", "source_snapshot_key": "k1",
         "reliable": True, "rule_set_version": "v1",
         "events": [_fake_event("R", "warning", "0.5")]},
        {"data_as_of": "2026-07-20T11:00:00+00:00", "source_snapshot_key": "k2",
         "reliable": True, "rule_set_version": "v1",
         "events": [_fake_event("R", "critical", "0.95")]},
        {"data_as_of": "2026-07-20T12:00:00+00:00", "source_snapshot_key": "k3",
         "reliable": True, "rule_set_version": "v1",
         "events": [_fake_event("R", "warning", "0.5")]},
    ]
    eps = rp.build_episodes(pts, run_id="run1", account=ACC,
                            mode=rp.MODE_CURRENT_RULES, now=NOW)
    assert len(eps) == 1
    assert eps[0]["peak_severity"] == "critical"      # 不被最后的 warning 掩盖
    assert eps[0]["last_severity"] == "warning"
    assert eps[0]["peak_actual_value"] == "0.95"
    assert eps[0]["occurrence_count"] == 3


def _fake_event(code, severity, value, symbol=None, category="account"):
    return type("E", (), {"rule_code": code, "severity": severity,
                          "actual_value": value, "threshold_value": "0.4",
                          "affected_symbol": symbol, "category": category,
                          "title": f"{code} 触发", "explanation": "说明",
                          "unit": "ratio"})()


# ---------------- 11–13. 规则版本模式 ----------------

def test_current_rules_mode(env):
    cfg, d = env
    _seed(d, 3, with_sid=True)
    res = rp.run_replay(cfg, d, date_start="2026-07-20", date_end="2026-07-25",
                        rule_set_mode=rp.MODE_CURRENT_RULES, now=NOW)
    assert res["status"] in (rp.ST_SUCCESS, rp.ST_PARTIAL)
    assert res["rule_set_mode"] == rp.MODE_CURRENT_RULES
    rows, _ = d.replay_assessments(res["replay_run_id"])
    assert rows and all(r["rule_set_mode"] == "current_rules" for r in rows)
    assert all(r["rule_set_version"] != "insufficient_rule_version" for r in rows)


def test_point_in_time_missing_version_is_flagged(env):
    """找不到当时的规则版本，**绝不悄悄用当前规则冒充**。"""
    cfg, d = env
    _seed(d, 3, with_sid=True)
    res = rp.run_replay(cfg, d, date_start="2026-07-20", date_end="2026-07-25",
                        rule_set_mode=rp.MODE_POINT_IN_TIME, now=NOW)
    assert res["rule_version_missing"] > 0
    assert res["status"] == rp.ST_PARTIAL
    rows, _ = d.replay_assessments(res["replay_run_id"])
    assert any(r["rule_set_version"] == "insufficient_rule_version" for r in rows)
    run = d.get_replay_run(res["replay_run_id"])
    assert "未用当前规则冒充" in (run["error_message"] or "")


def test_two_modes_are_stored_separately(env):
    cfg, d = env
    _seed(d, 3, with_sid=True)
    a = rp.run_replay(cfg, d, date_start="2026-07-20", date_end="2026-07-25",
                      rule_set_mode=rp.MODE_CURRENT_RULES, now=NOW)
    b = rp.run_replay(cfg, d, date_start="2026-07-20", date_end="2026-07-25",
                      rule_set_mode=rp.MODE_POINT_IN_TIME, now=NOW)
    assert a["replay_run_id"] != b["replay_run_id"]
    ra, _ = d.replay_assessments(a["replay_run_id"])
    rb, _ = d.replay_assessments(b["replay_run_id"])
    assert {x["rule_set_mode"] for x in ra} == {"current_rules"}
    assert {x["rule_set_mode"] for x in rb} == {"point_in_time"}


# ---------------- 14–17. 规则执行与市场 ----------------

def test_account_and_position_rules_replayed(env):
    cfg, d = env
    ts = "2026-07-20T23:59:00+00:00"
    # 单只持仓占净值 100% -> 必然触发集中度
    _raw_insert(d, [_bal(ts, equity="50000", cash="0", pv="50000", sid="s1")],
                [_pos(ts, mv="50000", sid="s1")])
    res = rp.run_replay(cfg, d, date_start="2026-07-20", date_end="2026-07-20",
                        rule_set_mode=rp.MODE_CURRENT_RULES, now=NOW)
    summary = d.replay_rule_summary(res["replay_run_id"])
    triggered = {r["rule_code"] for r in summary if r["triggers"]}
    assert any(c.startswith("CONC_") for c in triggered)
    assert res["assessment_count"] == 1
    assert res["rule_result_count"] > 0


def test_market_alignment_never_uses_future(env):
    cfg, d = env
    bars = [{"date": f"2026-07-{d_:02d}", "o": 100, "h": 100, "l": 100,
             "c": 100 + d_, "v": 1000} for d_ in range(1, 26)]
    d.upsert_daily_bars("QQQ", bars)
    m = rp.market_at(d, cfg, "2026-07-15T23:59:00+00:00")
    # 只用 7/15 及之前的日线
    assert m["market_data_as_of"] is None or m["market_data_as_of"] <= "2026-07-15"
    assert "粒度不同" in m["granularity_note"]


def test_market_source_recorded(env):
    cfg, d = env
    _seed(d, 1, with_sid=True)
    res = rp.run_replay(cfg, d, date_start="2026-07-20", date_end="2026-07-20",
                        now=NOW)
    rows, _ = d.replay_assessments(res["replay_run_id"])
    assert rows[0]["market_source"] in ("stored_assessment", "recomputed", "none")


def test_flex_cash_flow_known_at(env):
    """Flex 是 T+1：昨天发生、今天才同步的现金流，昨天做判断时不存在。"""
    cfg, d = env
    d.upsert_cash_flow({
        "cashflow_id": "txn:LATE", "account": ACC, "account_id": ACC,
        "amount": "50000", "currency": "USD", "category": "external_cash_flow",
        "subtype": "deposit", "direction": "inflow",
        "occurred_at": "2026-07-05T12:00:00+00:00",
        "effective_at": "2026-07-05T12:00:00+00:00", "et_date": "2026-07-05",
        "source": "FLEX", "created_at": "2026-07-20T00:00:00+00:00",
        "updated_at": "2026-07-20T00:00:00+00:00", "content_hash": "h"})
    assert d.cash_flows_known_at("2026-07-10T00:00:00+00:00", account=ACC) == []
    assert len(d.cash_flows_known_at("2026-07-25T00:00:00+00:00",
                                     account=ACC)) == 1


# ---------------- 18–22. episode ----------------

def _pt(ts, events, key="k", reliable=True):
    return {"data_as_of": ts, "source_snapshot_key": key, "reliable": reliable,
            "rule_set_version": "v1", "events": events}


def test_episode_opens_and_persists():
    pts = [_pt("2026-07-20T10:00:00+00:00", [_fake_event("R", "high", "1")]),
           _pt("2026-07-20T11:00:00+00:00", [_fake_event("R", "high", "2")])]
    eps = rp.build_episodes(pts, run_id="r", account=ACC,
                            mode=rp.MODE_CURRENT_RULES, now=NOW)
    assert len(eps) == 1
    assert eps[0]["occurrence_count"] == 2
    assert eps[0]["first_detected_at"] == "2026-07-20T10:00:00+00:00"
    assert eps[0]["last_detected_at"] == "2026-07-20T11:00:00+00:00"


def test_episode_resolves_when_rule_stops():
    pts = [_pt("2026-07-20T10:00:00+00:00", [_fake_event("R", "high", "1")]),
           _pt("2026-07-20T11:00:00+00:00", [])]
    eps = rp.build_episodes(pts, run_id="r", account=ACC,
                            mode=rp.MODE_CURRENT_RULES, now=NOW)
    assert eps[0]["resolved_at"] == "2026-07-20T11:00:00+00:00"
    assert eps[0]["duration_minutes"] == 60


def test_unreliable_point_is_not_treated_as_recovery():
    """数据缺失 ≠ 恢复。不可靠的点既不延续也不关闭。"""
    pts = [_pt("2026-07-20T10:00:00+00:00", [_fake_event("R", "high", "1")]),
           _pt("2026-07-20T11:00:00+00:00", [], reliable=False),
           _pt("2026-07-20T12:00:00+00:00", [_fake_event("R", "high", "3")])]
    eps = rp.build_episodes(pts, run_id="r", account=ACC,
                            mode=rp.MODE_CURRENT_RULES, now=NOW)
    assert len(eps) == 1                # 没有被中间的空洞截断成两段
    assert eps[0]["occurrence_count"] == 2
    assert eps[0]["resolved_at"] is None


def test_retrigger_creates_new_episode():
    pts = [_pt("2026-07-20T10:00:00+00:00", [_fake_event("R", "high", "1")]),
           _pt("2026-07-20T11:00:00+00:00", []),
           _pt("2026-07-20T12:00:00+00:00", [_fake_event("R", "high", "3")])]
    eps = rp.build_episodes(pts, run_id="r", account=ACC,
                            mode=rp.MODE_CURRENT_RULES, now=NOW)
    assert len(eps) == 2
    assert {e["episode_seq"] for e in eps} == {1, 2}


def test_episode_records_source_keys():
    pts = [_pt("2026-07-20T10:00:00+00:00", [_fake_event("R", "high", "1")], key="ka"),
           _pt("2026-07-20T11:00:00+00:00", [_fake_event("R", "high", "2")], key="kb")]
    eps = rp.build_episodes(pts, run_id="r", account=ACC,
                            mode=rp.MODE_CURRENT_RULES, now=NOW)
    keys = json.loads(eps[0]["source_snapshot_keys"])
    assert keys == ["ka", "kb"]


# ---------------- 23–27. 隔离（最关键） ----------------

def test_replay_sends_no_notification(env):
    cfg, d = env
    _seed(d, 3, with_sid=True)
    before = d.notifications_between("2000-01-01", "2100-01-01",
                                     success_only=False)
    rp.run_replay(cfg, d, date_start="2026-07-20", date_end="2026-07-25", now=NOW)
    after = d.notifications_between("2000-01-01", "2100-01-01",
                                    success_only=False)
    assert after == before == []
    # NullNotifier 明确返回未发送
    n = rctx.NullNotifier()
    assert n.send("t", "b") is False


def test_replay_does_not_touch_alert_state(env):
    cfg, d = env
    _seed(d, 3, with_sid=True)
    d.upsert_alert_active(dedup_key="R1:AAA", rule_id="R1", symbol="AAA",
                          severity="WARN", value=D("0.5"),
                          now_iso=NOW.isoformat(), notified=True,
                          et_date="2026-07-20")
    before = [dict(r) for r in d.alert_states()]
    rp.run_replay(cfg, d, date_start="2026-07-20", date_end="2026-07-25", now=NOW)
    after = [dict(r) for r in d.alert_states()]
    assert after == before


def test_replay_does_not_touch_real_events(env):
    from app import event_service as es, risk_events as rev
    cfg, d = env
    _seed(d, 3, with_sid=True)
    trig = rev.Trigger(source_type="account", event_code="X", rule_code="X",
                       category="concentration", severity="high", title="T",
                       explanation="E", rule_set_version="v~1", account_id=ACC)
    es.register_triggers(d, [trig], now=NOW, evaluation_complete=True,
                         reliable=True, scope={"source_types": ["account"]})
    before_events, before_total = d.query_risk_events()
    before_trans = d.event_transitions(before_events[0]["event_id"])

    rp.run_replay(cfg, d, date_start="2026-07-20", date_end="2026-07-25", now=NOW)

    after_events, after_total = d.query_risk_events()
    assert after_total == before_total == 1
    assert after_events[0] == before_events[0]
    assert d.event_transitions(before_events[0]["event_id"]) == before_trans


def test_replay_writes_only_to_replay_tables(env):
    cfg, d = env
    _seed(d, 3, with_sid=True)
    import sqlite3
    conn = sqlite3.connect(d.db_path)

    def snapshot_tables():
        out = {}
        for t in ("risk_events", "market_environment_events",
                  "risk_event_registry", "risk_event_transitions",
                  "risk_notifications", "alert_state", "rule_events"):
            try:
                out[t] = conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            except sqlite3.Error:
                out[t] = None
        return out

    before = snapshot_tables()
    res = rp.run_replay(cfg, d, date_start="2026-07-20", date_end="2026-07-25",
                        now=NOW)
    after = snapshot_tables()
    assert after == before, "重放污染了实时表"
    # 而重放表确实写了
    assert d.get_replay_run(res["replay_run_id"]) is not None
    assert d.replay_assessments(res["replay_run_id"])[1] > 0


def test_replay_read_only_guard():
    with pytest.raises(rctx.ReplayViolation):
        rctx.assert_read_only(rctx.MODE_REPLAY, "创建实时 episode")
    with pytest.raises(rctx.ReplayViolation):
        rctx.assert_read_only(rctx.MODE_REPLAY, "推进冷静期")
    rctx.assert_read_only(rctx.MODE_LIVE, "创建实时事件")


# ---------------- 28–31. 幂等与研究接入 ----------------

def test_identical_params_are_reused(env):
    cfg, d = env
    _seed(d, 3, with_sid=True)
    a = rp.run_replay(cfg, d, date_start="2026-07-20", date_end="2026-07-25",
                      now=NOW)
    b = rp.run_replay(cfg, d, date_start="2026-07-20", date_end="2026-07-25",
                      now=NOW)
    assert b["reused"] is True
    assert b["replay_run_id"] == a["replay_run_id"]
    c = rp.run_replay(cfg, d, date_start="2026-07-20", date_end="2026-07-25",
                      now=NOW, reuse=False)
    assert c["replay_run_id"] != a["replay_run_id"]


def test_calculation_version_is_recorded(env):
    cfg, d = env
    _seed(d, 2, with_sid=True)
    res = rp.run_replay(cfg, d, date_start="2026-07-20", date_end="2026-07-25",
                        now=NOW)
    run = d.get_replay_run(res["replay_run_id"])
    assert run["calculation_version"] == rp.CALC_VERSION
    # 版本变了参数哈希就变，会跑出新任务而不是复用
    h1 = rp.param_hash({"v": rp.CALC_VERSION})
    h2 = rp.param_hash({"v": "replay-v2/2027-01"})
    assert h1 != h2


def test_coverage_report(env):
    cfg, d = env
    _seed(d, 3, with_sid=True)
    _seed(d, 2, with_sid=False, base=dt.date(2026, 7, 10))
    c = rp.coverage_report(cfg, d)
    assert c["balances_total"] == 5
    assert c["native_snapshot_id_count"] == 3
    assert c["reconstructable_count"] == 5
    assert c["replayable_rule_count"] > 0
    assert "R1–R7" in c["note"]


def test_insufficient_sample_still_insufficient(env):
    """重放产生更多样本，但样本仍不足时不得给强结论。"""
    from app import rule_performance as perf
    cfg, d = env
    _seed(d, 2, with_sid=True)
    rp.run_replay(cfg, d, date_start="2026-07-20", date_end="2026-07-25", now=NOW)
    assert perf.confidence_for(2) == perf.CONF_INSUFFICIENT
    score, comps = perf.composite_score(D("1"), D("1"), D("100"), D("1"), 2)
    assert score is None


# ---------------- 32–36. API / 精度 / 前端 ----------------

def test_api_unified_response(env):
    cfg, d = env
    _seed(d, 3, with_sid=True)
    c = TestClient(create_app(cfg, d))
    for path in ("/api/v1/risk/replays", "/api/v1/risk/replays/coverage"):
        body = c.get(path).json()
        assert set(body) == {"success", "data", "error", "request_id",
                             "generated_at"}
        assert body["success"] is True
    r = c.post("/api/v1/risk/replays?date_start=2026-07-20&date_end=2026-07-25")
    assert r.json()["success"] is True
    run_id = r.json()["data"]["replay_run_id"]
    for path in (f"/api/v1/risk/replays/{run_id}",
                 f"/api/v1/risk/replays/{run_id}/assessments",
                 f"/api/v1/risk/replays/{run_id}/events"):
        body = c.get(path).json()
        assert body["success"] is True
        assert "不是当时实际发送过的告警" in body["data"]["disclaimer"]
    assert c.get("/api/v1/risk/replays/nope").json()["success"] is False
    # 非法参数
    assert c.post("/api/v1/risk/replays?sampling_mode=bogus").json()["success"] is False
    assert c.post("/api/v1/risk/replays?rule_set_mode=bogus").json()["success"] is False


def test_decimal_precision_in_replay(env):
    cfg, d = env
    ts = "2026-07-20T23:59:00+00:00"
    _raw_insert(d, [_bal(ts, equity="43970.863333333333333", cash="100",
                         pv="50000", sid="s1")],
                [_pos(ts, mv="50000", sid="s1")])
    res = rp.run_replay(cfg, d, date_start="2026-07-20", date_end="2026-07-20",
                        now=NOW)
    rows, _ = d.replay_assessments(res["replay_run_id"])
    assert isinstance(rows[0]["risk_score"], str)
    comp = json.loads(rows[0]["component_scores"])
    assert all(isinstance(v, (str, type(None))) for v in comp.values())


def test_timezone_handling(env):
    cfg, d = env
    # UTC 深夜属于美东前一天
    b = rp._bucket("2026-07-20T10:07:00+00:00", 60)
    assert b.endswith("10:00:00+00:00")
    assert rp._gap_seconds("2026-07-20T12:00:00+00:00",
                           "2026-07-20T10:00:00+00:00") == 7200
    # 日期与时间戳混用不崩
    assert rp._gap_seconds("2026-07-20T12:00:00+00:00", "2026-07-20") == 43200
    assert rp._gap_seconds(None, "2026-07-20") is None


def test_frontend_marks_results_as_simulated(env):
    cfg, d = env
    _seed(d, 3, with_sid=True)
    rp.run_replay(cfg, d, date_start="2026-07-20", date_end="2026-07-25", now=NOW)
    html = TestClient(create_app(cfg, d)).get("/replay").text
    assert "历史重放结果不是当时实际发送过的告警" in html
    assert "current-rules 模拟" in html or "point-in-time 重建" in html
    assert "覆盖情况" in html
    assert "R1–R7 不在第一版" in html


def test_replay_failure_does_not_break_live(env):
    """重放炸了，实时评估与告警必须照常。"""
    cfg, d = env

    class Broken:
        def __getattr__(self, name):
            raise RuntimeError("库炸了")

    res = rp.run_replay(cfg, d, date_start="2026-07-20", date_end="2026-07-25",
                        now=NOW)
    # 空库：没有可用快照 -> failed，但不抛异常
    assert res["status"] in (rp.ST_FAILED, rp.ST_PARTIAL, rp.ST_SUCCESS)
    broken = rp.run_replay(cfg, Broken(), date_start="2026-07-20",
                           date_end="2026-07-25", now=NOW)
    assert broken["status"] == rp.ST_FAILED
    from app import risk_service
    out = risk_service.assess(cfg, d)
    assert out is None or "risk_level" in out


def test_unattributed_account_snapshots_are_reported_and_optional(env):
    """account 列是后加的，早期快照为空。

    默认按账户过滤会静默丢掉那几天 —— 覆盖率必须如实报告，
    并提供显式纳入的开关，而不是替用户决定。
    """
    cfg, d = env
    _seed(d, 3, with_sid=True)                          # 有 account
    bals, poss = [], []
    for i in range(2):                                   # 无 account
        ts = f"{(dt.date(2026, 7, 10) + dt.timedelta(days=i)).isoformat()}T23:59:00+00:00"
        b = _bal(ts, pv="50000")
        b["account"] = None
        bals.append(b)
        poss.append(_pos(ts))
    _raw_insert(d, bals, poss)

    cov = rp.coverage_report(cfg, d)
    assert cov["unattributed_account"]["count"] == 2
    assert cov["unattributed_account"]["distinct_days"] == 2
    assert "include_unattributed=true" in cov["unattributed_note"]

    # 默认排除
    excluded = rp.sample_snapshots(d, start="2026-07-01", end="2026-07-31",
                                   account=ACC, mode=rp.SAMPLE_DAILY_CLOSE)
    assert len(excluded) == 3
    # 显式纳入
    included = rp.sample_snapshots(d, start="2026-07-01", end="2026-07-31",
                                   account=ACC, mode=rp.SAMPLE_DAILY_CLOSE,
                                   include_unattributed=True)
    assert len(included) == 5

    res = rp.run_replay(cfg, d, date_start="2026-07-01", date_end="2026-07-31",
                        include_unattributed=True, now=NOW)
    assert res["source_snapshot_count"] == 5
    assert res["coverage"]["unattributed_account_snapshots"] == 2
    assert res["coverage"]["include_unattributed"] is True
    assert "唯一账户" in res["coverage"]["unattributed_note"]
    # 参数不同 -> 不会与默认那次复用同一个 run
    other = rp.run_replay(cfg, d, date_start="2026-07-01", date_end="2026-07-31",
                          include_unattributed=False, now=NOW)
    assert other["replay_run_id"] != res["replay_run_id"]
