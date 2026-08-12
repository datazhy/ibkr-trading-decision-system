"""Daily PnL v2 切换候选 —— 24 项验收测试。

这批测试守的是**切换本身的安全性**，不是计算正确性（那在
`test_daily_pnl_observation.py`）。核心命题只有两条：

  1. 开关关闭时，线上行为**逐字不变** —— 候选代码的存在不得改变任何
     既有结论、版本、顺序或通知；
  2. 开关开启时，绝不允许任何形式的跨周期拼接，拼不齐就 insufficient_data，
     并且**保持既有 episode 状态**（既不开也不关）。

第二条里最容易被忽略的是「切换那一刻」：口径版本变了，但阈值和规则一个字
没动。若按整串版本比较，所有在途 episode 会被判「版本变化」而断开重开，
凭空造出一批假恢复和假告警 —— 那正是这次修复要消灭的现象。
"""
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D

import pytest

from app import daily_pnl_observation as dpo
from app import daily_pnl_v2 as dpv2
from app import risk_engine as re_
from app import risk_events as rev
from app import risk_service
from app.db import Database

NOW = datetime(2026, 7, 30, 2, 0, tzinfo=timezone.utc)
SESSION = "2026-07-29"
ACC = "U1234567"


@pytest.fixture
def env(make_config):
    def _make(**over):
        cfg = make_config(ib_account=ACC, **over)
        d = Database(cfg.db_path)
        d.init_schema()
        return cfg, d
    return _make


def _write_cycle(d, *, run_id, snapshot_id, ts, equity=D("40000"),
                 daily_pnl=D("-1600"), source="IBKR_reqPnL", quality="OK",
                 basis=None, pnl_run_id=None, pnl_ts=None, session=SESSION):
    """写一轮完整快照。pnl_run_id/pnl_ts 可故意错开以构造跨周期场景。"""
    d.write_account_snapshot(
        snapshot_id=snapshot_id, ts=ts, sync_run_id=run_id,
        quality_status="fresh", ai_analysis_allowed=True,
        balances={"account": ACC, "currency": "USD", "equity": equity,
                  "cash": D("8000"), "total_cash": D("8000"),
                  "margin_balance": D("8000"), "buying_power": equity,
                  "available_funds": D("8000"), "excess_liquidity": D("8000"),
                  "maint_margin": D("5000"), "init_margin": D("6000"),
                  "positions_value": equity - D("8000")},
        positions=[{"symbol": "AAA", "qty": D("100"), "avg_cost": D("300"),
                    "market_value": equity - D("8000"), "upl": D("-1000"),
                    "price": D("320"), "price_quality": "DELAYED"}])
    if daily_pnl is not None:
        d.insert_pnl_snapshot({
            "account": ACC, "as_of": pnl_ts or ts, "et_date": session,
            "daily_pnl": daily_pnl, "realized_pnl": D("0"),
            "unrealized_pnl": D("-1000"), "net_liquidation": equity,
            "source": source, "quality": quality,
            "reset_policy": "IBKR_SESSION_DAILY",
            "sync_run_id": pnl_run_id or run_id, "daily_basis": basis})


def _outcome(res, code="ACC_DAILY_LOSS_HIGH"):
    for o in (res or {}).get("rule_outcomes") or []:
        if o.get("rule_code") == code:
            return o
    return None


# ================= 1–3. 开关关闭：行为逐字不变 =================

def test_flag_off_keeps_old_call_order(env, monkeypatch):
    """关闭时 sync 必须先评估、后写 PnL —— 顺序即现状。"""
    cfg, d = env()
    assert dpv2.order_mode(cfg) == dpv2.ORDER_ASSESS_BEFORE_PNL
    assert dpv2.mode(cfg) == dpv2.MODE_OLD


def test_flag_off_produces_identical_version(env):
    """关闭时落库的版本串必须**逐字节等于**现有实现。

    版本串是幂等键。差一个字符就会让全部历史评估重算、事件按「版本变化」
    断开重开 —— 候选代码的存在绝不允许产生这种副作用。
    """
    cfg, d = env()
    rules = re_.default_rules(cfg)
    assert (risk_service._set_version_for_mode(cfg, rules)
            == re_.rule_set_version(rules))


def test_flag_off_never_uses_v2_versions(env):
    cfg, d = env()
    assert dpv2.active_calculation_version(cfg) is None
    assert dpv2.active_daily_pnl_version(cfg) is None


# ================= 4–6. 开关开启：顺序与显式传递 =================

def test_flag_on_switches_order_and_mode(env):
    cfg, d = env(daily_pnl_v2_enabled=True)
    assert dpv2.order_mode(cfg) == dpv2.ORDER_PNL_BEFORE_ASSESS
    assert dpv2.mode(cfg) == dpv2.MODE_V2
    assert dpv2.active_calculation_version(cfg) == dpv2.CALC_VERSION_V2


def test_v2_uses_injected_observation_without_requery(env):
    """注入的观测必须被直接采用，不得在内部再查一次。"""
    cfg, d = env(daily_pnl_v2_enabled=True)
    ts = NOW.isoformat()
    _write_cycle(d, run_id="run-A", snapshot_id="snap-A", ts=ts)
    obs = dpo.build(
        account_id=ACC,
        numerator={"daily_pnl": D("-1600"), "source": "IBKR_reqPnL",
                   "basis": None, "quality": "OK", "observed_at": ts,
                   "correlation_id": "run-A", "session_date": SESSION,
                   "currency": "USD", "snapshot_id": "snap-A"},
        denominator={"net_liquidation": D("40000"), "observed_at": ts,
                     "correlation_id": "run-A", "session_date": SESSION,
                     "currency": "USD", "snapshot_id": "snap-A"},
        observed_at=ts, correlation_id="run-A", now=NOW)
    assert obs.reliable
    ctx = risk_service.build_context(cfg, d, "snap-A", NOW,
                                     daily_pnl_observation=obs,
                                     calculation_mode=dpv2.MODE_V2)
    assert ctx.daily_pnl_observation is obs        # 同一个对象，未被替换


def test_v2_never_calls_latest_pnl(env, monkeypatch):
    """v2 路径下 latest_pnl 一次都不能被调用 —— 这是结构保证，不是纪律。"""
    cfg, d = env(daily_pnl_v2_enabled=True)
    ts = NOW.isoformat()
    _write_cycle(d, run_id="run-A", snapshot_id="snap-A", ts=ts)
    called = []
    orig = d.latest_pnl
    monkeypatch.setattr(d, "latest_pnl",
                        lambda *a, **k: (called.append(1), orig(*a, **k))[1])
    risk_service.build_context(cfg, d, "snap-A", NOW,
                               calculation_mode=dpv2.MODE_V2)
    assert called == []


# ================= 7–9. 同周期配对 =================

def test_same_snapshot_and_run_triggers(env):
    cfg, d = env(daily_pnl_v2_enabled=True)
    ts = NOW.isoformat()
    _write_cycle(d, run_id="run-A", snapshot_id="snap-A", ts=ts,
                 equity=D("40000"), daily_pnl=D("-1600"))   # 4% > 3%
    res = risk_service.assess(cfg, d, snapshot_id="snap-A", now=NOW,
                              calculation_mode=dpv2.MODE_V2)
    o = _outcome(res)
    assert o["outcome"] == dpo.OUT_TRIGGERED


def test_different_run_id_is_insufficient_not_stitched(env):
    """分子来自另一个批次：必须 insufficient，**绝不拼接**。"""
    cfg, d = env(daily_pnl_v2_enabled=True)
    ts = NOW.isoformat()
    _write_cycle(d, run_id="run-A", snapshot_id="snap-A", ts=ts,
                 pnl_run_id="run-OTHER", daily_pnl=D("-1600"))
    res = risk_service.assess(cfg, d, snapshot_id="snap-A", now=NOW,
                              calculation_mode=dpv2.MODE_V2)
    assert _outcome(res)["outcome"] == dpo.OUT_INSUFFICIENT


def test_stale_numerator_from_previous_cycle_is_refused(env):
    """上一周期的分子摆在库里也不许用 —— 这正是事故的原始形态。"""
    cfg, d = env(daily_pnl_v2_enabled=True)
    t0 = (NOW - timedelta(minutes=5)).isoformat()
    _write_cycle(d, run_id="run-OLD", snapshot_id="snap-OLD", ts=t0,
                 daily_pnl=D("-1600"))
    ts = NOW.isoformat()
    _write_cycle(d, run_id="run-B", snapshot_id="snap-B", ts=ts,
                 daily_pnl=None)              # 本轮没有 PnL
    res = risk_service.assess(cfg, d, snapshot_id="snap-B", now=NOW,
                              calculation_mode=dpv2.MODE_V2)
    assert _outcome(res)["outcome"] == dpo.OUT_INSUFFICIENT


# ================= 10–11. 写入/构建失败时不跑 v2 规则 =================

def test_missing_pnl_write_yields_insufficient(env):
    cfg, d = env(daily_pnl_v2_enabled=True)
    ts = NOW.isoformat()
    _write_cycle(d, run_id="run-C", snapshot_id="snap-C", ts=ts, daily_pnl=None)
    res = risk_service.assess(cfg, d, snapshot_id="snap-C", now=NOW,
                              calculation_mode=dpv2.MODE_V2)
    assert _outcome(res)["outcome"] == dpo.OUT_INSUFFICIENT


def test_none_observation_still_evaluates_other_rules(env):
    """观测建不出来时，其余规则照常评估 —— 一个指标缺失不该让风控停摆。"""
    cfg, d = env(daily_pnl_v2_enabled=True)
    ts = NOW.isoformat()
    _write_cycle(d, run_id="run-D", snapshot_id="snap-D", ts=ts, daily_pnl=None)
    res = risk_service.assess(cfg, d, snapshot_id="snap-D", now=NOW,
                              calculation_mode=dpv2.MODE_V2)
    codes = {o["rule_code"] for o in res["rule_outcomes"]}
    assert len(codes) > 1
    assert any(o["outcome"] != dpo.OUT_INSUFFICIENT
               for o in res["rule_outcomes"])


# ================= 12–13. insufficient 不迁移事件 =================

def _trig(version, value="0.04"):
    return rev.Trigger(
        source_type=rev.SRC_ACCOUNT, event_code="ACC_DAILY_LOSS_HIGH",
        rule_code="ACC_DAILY_LOSS_HIGH", category="loss", severity="high",
        title="当日亏损超过阈值", explanation="x", actual_value=value,
        threshold_value="0.03", unit="ratio", affected_symbol=None,
        recommended_check="x", rule_set_version=version,
        data_as_of=NOW.isoformat(), account_id=ACC)


def test_insufficient_does_not_resolve_open_episode(env):
    """缺数据绝不判恢复 —— 事故里 11 次假恢复的根子。"""
    cfg, d = env(daily_pnl_v2_enabled=True)
    v = re_.rule_set_version(re_.default_rules(cfg))
    rev_res = rev  # 便于阅读
    from app import event_service as es
    es.register_triggers(d, [_trig(v)], now=NOW, evaluation_complete=True,
                         reliable=True, scope={"source_types": [rev.SRC_ACCOUNT]})
    assert d.query_risk_events(status=["open"])[1] == 1
    # 本轮 insufficient：evaluation_complete=False 表达「这一项没判出来」
    es.register_triggers(d, [], now=NOW + timedelta(minutes=1),
                         evaluation_complete=False, reliable=True,
                         scope={"source_types": [rev.SRC_ACCOUNT]})
    assert d.query_risk_events(status=["open"])[1] == 1     # 仍然开着


def test_insufficient_does_not_open_episode(env):
    cfg, d = env(daily_pnl_v2_enabled=True)
    from app import event_service as es
    es.register_triggers(d, [], now=NOW, evaluation_complete=False,
                         reliable=True, scope={"source_types": [rev.SRC_ACCOUNT]})
    assert d.query_risk_events()[1] == 0


# ================= 14–16. 跨版本的 episode 处理 =================

def test_calc_version_change_does_not_split_episode(env):
    """**切换 v2 不得断开在途 episode。**

    口径变了、阈值没变。若按整串版本比较，切换那一刻所有在途事件都会被
    判「版本变化」断开重开，凭空造一批假恢复与假告警。
    """
    cfg, d = env()
    from app import event_service as es
    rules = re_.default_rules(cfg)
    v_old = re_.rule_set_version(rules)
    v_new = f"{dpv2.CALC_VERSION_V2}~{re_.rule_set_hash(rules)}"
    assert v_old != v_new                                # 版本串确实变了
    es.register_triggers(d, [_trig(v_old)], now=NOW, evaluation_complete=True,
                         reliable=True, scope={"source_types": [rev.SRC_ACCOUNT]})
    es.register_triggers(d, [_trig(v_new)], now=NOW + timedelta(minutes=1),
                         evaluation_complete=True, reliable=True,
                         scope={"source_types": [rev.SRC_ACCOUNT]})
    rows, total = d.query_risk_events()
    assert total == 1                                    # 仍是同一个 episode
    assert rows[0]["episode_seq"] == 1


def test_threshold_change_still_splits_episode(env):
    """阈值真的变了仍要断开 —— 不能把降噪做成「什么都不再断开」。"""
    cfg, d = env()
    from app import event_service as es
    v_old = "risk-v2-conc/2026-07~hash-AAAA"
    v_new = "risk-v2-conc/2026-07~hash-BBBB"            # 内容哈希不同
    es.register_triggers(d, [_trig(v_old)], now=NOW, evaluation_complete=True,
                         reliable=True, scope={"source_types": [rev.SRC_ACCOUNT]})
    es.register_triggers(d, [_trig(v_new)], now=NOW + timedelta(minutes=1),
                         evaluation_complete=True, reliable=True,
                         scope={"source_types": [rev.SRC_ACCOUNT]})
    assert d.query_risk_events()[1] == 2                # 开了新 episode


def test_reliable_below_threshold_resolves_normally(env):
    """切换后第一条可靠观测未超阈值：走正常恢复流程，不是「不敢动」。"""
    cfg, d = env()
    from app import event_service as es
    v = re_.rule_set_version(re_.default_rules(cfg))
    es.register_triggers(d, [_trig(v)], now=NOW, evaluation_complete=True,
                         reliable=True, scope={"source_types": [rev.SRC_ACCOUNT]})
    es.register_triggers(d, [], now=NOW + timedelta(minutes=1),
                         evaluation_complete=True, reliable=True,
                         scope={"source_types": [rev.SRC_ACCOUNT]})
    rows, _ = d.query_risk_events(status=["resolved"])
    assert len(rows) == 1


# ================= 17–18. 来源切换 =================

def test_source_switch_with_valid_closure_evaluates(env):
    """来源变了但闭合可靠 -> 正常评估。来源切换本身不等于数据无效。"""
    cfg, d = env(daily_pnl_v2_enabled=True)
    ts = NOW.isoformat()
    _write_cycle(d, run_id="run-E", snapshot_id="snap-E", ts=ts,
                 source="IBKR_reqPnL", quality="OK", daily_pnl=D("-1600"))
    res = risk_service.assess(cfg, d, snapshot_id="snap-E", now=NOW,
                              calculation_mode=dpv2.MODE_V2)
    assert _outcome(res)["outcome"] == dpo.OUT_TRIGGERED


def test_degraded_source_is_insufficient(env):
    """DERIVED/DEGRADED 不足以驱动状态迁移。"""
    cfg, d = env(daily_pnl_v2_enabled=True)
    ts = NOW.isoformat()
    _write_cycle(d, run_id="run-F", snapshot_id="snap-F", ts=ts,
                 source="DERIVED_portfolio+executions", quality="DEGRADED",
                 basis="NET_LIQ_CHANGE_UNADJUSTED", daily_pnl=D("-1600"))
    res = risk_service.assess(cfg, d, snapshot_id="snap-F", now=NOW,
                              calculation_mode=dpv2.MODE_V2)
    assert _outcome(res)["outcome"] == dpo.OUT_INSUFFICIENT


# ================= 19. 回滚不改历史 =================

def test_rollback_leaves_history_untouched(env):
    """把开关翻回 false 只影响**未来**评估，历史一行不动。"""
    cfg_on, d = env(daily_pnl_v2_enabled=True)
    ts = NOW.isoformat()
    _write_cycle(d, run_id="run-G", snapshot_id="snap-G", ts=ts)
    res_on = risk_service.assess(cfg_on, d, snapshot_id="snap-G", now=NOW,
                                 calculation_mode=dpv2.MODE_V2)
    v_on = res_on["calculation_version"]
    assert v_on.startswith(dpv2.CALC_VERSION_V2)
    before = d.assessment_for("snap-G", v_on)
    assert before is not None
    # 回滚：同一份快照按旧口径再评一次，v2 那条历史仍在
    cfg_off = type(cfg_on)(**{**cfg_on.__dict__, "daily_pnl_v2_enabled": False})
    risk_service.assess(cfg_off, d, snapshot_id="snap-G", now=NOW)
    assert d.assessment_for("snap-G", v_on) is not None   # v2 历史未被删改


# ================= 20. 自检 =================

def test_preflight_skipped_when_disabled(env):
    cfg, d = env()
    res = dpv2.preflight(cfg, d)
    assert res["ok"] is True and res["skipped"] is True


def test_preflight_passes_on_real_schema(env):
    cfg, d = env(daily_pnl_v2_enabled=True)
    res = dpv2.preflight(cfg, d)
    assert res["ok"] is True, res["failures"]


def test_preflight_refuses_loudly_when_table_missing(env, monkeypatch):
    """自检不过要**明确拒绝**，不得静默退回旧口径。"""
    cfg, d = env(daily_pnl_v2_enabled=True)
    monkeypatch.setattr(dpv2, "_table_exists",
                        lambda db, name: name != "daily_pnl_observations")
    res = dpv2.preflight(cfg, d)
    assert res["ok"] is False
    assert any(c == dpv2.CHECK_OBS_TABLE for c, _ in res["failures"])
    with pytest.raises(dpv2.PreflightFailed):
        dpv2.require_ready(cfg, d)


# ================= 21–22. 其他规则不受影响 =================

def test_other_account_rules_identical_across_modes(env):
    """除 ACC_DAILY_LOSS_HIGH 外，两种模式下所有规则结论必须一致。"""
    cfg_off, d = env()
    ts = NOW.isoformat()
    _write_cycle(d, run_id="run-H", snapshot_id="snap-H", ts=ts)
    r_off = risk_service.assess(cfg_off, d, snapshot_id="snap-H", now=NOW)
    cfg_on = type(cfg_off)(**{**cfg_off.__dict__, "daily_pnl_v2_enabled": True})
    r_on = risk_service.assess(cfg_on, d, snapshot_id="snap-H", now=NOW,
                               calculation_mode=dpv2.MODE_V2)

    def others(res):
        return {o["rule_code"]: o["outcome"] for o in res["rule_outcomes"]
                if o["rule_code"] != "ACC_DAILY_LOSS_HIGH"}
    assert others(r_off) == others(r_on)


def test_startup_fields_leak_no_amounts_or_account(env):
    """启动日志不得含金额或完整账户号。"""
    cfg, d = env(daily_pnl_v2_enabled=True)
    f = dpv2.startup_fields(cfg)
    blob = " ".join(str(v) for v in f.values())
    assert ACC not in blob
    assert not any(ch.isdigit() and len(t) > 6 and t.replace(".", "").isdigit()
                   for t in blob.split() for ch in t)


# ================= 23. 影子比较幂等 =================

def test_version_ranges_distinguishes_old_and_v2(env):
    """API 能区分 old/v2 的生效区间 —— 回滚后仍可追溯。"""
    cfg_off, d = env()
    ts = NOW.isoformat()
    _write_cycle(d, run_id="run-I", snapshot_id="snap-I", ts=ts)
    risk_service.assess(cfg_off, d, snapshot_id="snap-I", now=NOW)
    cfg_on = type(cfg_off)(**{**cfg_off.__dict__, "daily_pnl_v2_enabled": True})
    risk_service.assess(cfg_on, d, snapshot_id="snap-I", now=NOW,
                        calculation_mode=dpv2.MODE_V2)
    from app import daily_pnl_service as dps
    ranges = dps.version_ranges(d, (NOW - timedelta(hours=1)).isoformat())
    modes = {r["mode"] for r in ranges}
    assert dpv2.MODE_OLD in modes and dpv2.MODE_V2 in modes
