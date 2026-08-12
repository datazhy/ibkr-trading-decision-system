"""当日盈亏指标数据完整性修复 v1 —— 23 项验收测试。

钉死的东西只有一句：**分子和分母必须来自同一次采集周期。**

这条规矩不是洁癖。取证证据：`ACC_DAILY_LOSS_HIGH` 在 24 小时内开合 11 次，
4 个 episode 只活了 1 分钟。成因是 `risk_service.build_context` 用
`db.latest_pnl()` 取分子（可能来自上一周期）去配本周期的分母 —— 720 对相邻
样本里 496 对「净值冻结但当日盈亏在跳」，40 对净值与盈亏方向互相矛盾。

所以下面的测试大半在证明「**拼不齐时系统会拒绝出数**」，而不是证明它算得对。
"""
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D

import pytest

from app import daily_pnl_observation as dpo
from app import daily_pnl_service as dps
from app import risk_engine as re_
from app.db import Database

NOW = datetime(2026, 7, 30, 2, 0, tzinfo=timezone.utc)
TS = NOW.isoformat()
SESSION = "2026-07-29"
THR = D("0.03")


def _num(**kw):
    base = {"daily_pnl": D("-1200"), "source": "IBKR_reqPnL", "basis": None,
            "quality": "OK", "observed_at": TS, "correlation_id": "run-A",
            "session_date": SESSION, "currency": "USD", "snapshot_id": "snap-A"}
    base.update(kw)
    return base


def _den(**kw):
    base = {"net_liquidation": D("40000"), "observed_at": TS,
            "correlation_id": "run-A", "session_date": SESSION,
            "currency": "USD", "snapshot_id": "snap-A"}
    base.update(kw)
    return base


def _build(num=None, den=None, **kw):
    kw.setdefault("now", NOW)
    return dpo.build(account_id="U1", numerator=num or _num(),
                     denominator=den or _den(), **kw)


@pytest.fixture
def db(make_config):
    cfg = make_config(ib_account="U1")
    d = Database(cfg.db_path)
    d.init_schema()
    return cfg, d


# ---------------- 1–5. 同周期配对 ----------------

def test_01_same_correlation_id_is_reliable():
    """reqPnL 与 net_liquidation 同 correlation_id -> 可靠观测。"""
    o = _build()
    assert o.reliable is True
    assert o.quality_status == dpo.Q_OK
    assert o.quality_reasons == ()
    assert o.daily_loss_ratio == D("0.03")


def test_02_different_correlation_id_forbidden():
    """不同 correlation_id **禁止拼接** —— 这是本次修复的核心。"""
    o = _build(num=_num(correlation_id="run-OLD"),
               den=_den(correlation_id="run-NEW"))
    assert o.reliable is False
    assert dpo.R_CROSS_CYCLE in o.quality_reasons
    assert dpo.outcome_for(o, THR)[0] == dpo.OUT_INSUFFICIENT


def test_02b_missing_correlation_id_counts_as_cross_cycle():
    """拿不到批次凭证时按跨周期处理。「不知道是不是同一批」与「确定不是」
    在风险上是一回事，不能给它便宜。"""
    o = _build(num=_num(correlation_id=None), den=_den(correlation_id=None))
    assert o.reliable is False
    assert dpo.R_CROSS_CYCLE in o.quality_reasons


def test_03_skew_within_tolerance_ok():
    """分子分母时间差在容差内 -> 仍然可靠。"""
    earlier = (NOW - timedelta(seconds=45)).isoformat()
    o = _build(num=_num(observed_at=earlier), skew_tolerance_seconds=90)
    assert o.observation_skew_seconds == D("45.0")
    assert o.reliable is True


def test_04_skew_exceeded_is_insufficient():
    """时间差超容差 -> insufficient，不是「凑合能用」。"""
    earlier = (NOW - timedelta(seconds=300)).isoformat()
    o = _build(num=_num(observed_at=earlier), skew_tolerance_seconds=90)
    assert o.observation_skew_seconds == D("300.0")
    assert o.reliable is False
    assert dpo.R_SKEW_EXCEEDED in o.quality_reasons
    assert dpo.outcome_for(o, THR)[0] == dpo.OUT_INSUFFICIENT


def test_05_no_fallback_to_old_denominator(db):
    """**拿不到同周期分母不许退回旧的。**

    `pnl_for_run` 找不到同批次就返回 None，调用方据此标 insufficient；
    `latest_pnl` 那条路仍在，但注释已写明它不可用于闭合比值。
    """
    cfg, d = db
    d.insert_pnl_snapshot({
        "account": "U1", "as_of": TS, "et_date": SESSION,
        "daily_pnl": "-1200", "realized_pnl": "0", "unrealized_pnl": "-1200",
        "net_liquidation": "40000", "source": "IBKR_reqPnL", "quality": "OK",
        "reset_policy": "IBKR_SESSION_DAILY", "sync_run_id": "run-A",
        "daily_basis": None})
    assert d.pnl_for_run("U1", "run-A") is not None
    # 换一个批次号：必须一无所获，而不是回退给最新那条
    assert d.pnl_for_run("U1", "run-B") is None
    assert d.latest_pnl("U1") is not None      # 旧接口仍在，但语义不同

    # 模拟「同周期分子取不到」：直接把 numerator 传 None，而不是让 helper 兜回默认值
    o = dpo.build(account_id="U1", numerator=None, denominator=_den(), now=NOW)
    assert o.reliable is False
    assert dpo.R_NO_NUMERATOR in o.quality_reasons
    assert o.quality_status == dpo.Q_INSUFFICIENT
    assert dpo.outcome_for(o, THR)[0] == dpo.OUT_INSUFFICIENT


# ---------------- 6–10. DERIVED / 来源切换 ----------------

def test_06_derived_stays_degraded():
    """DERIVED 口径保持 DEGRADED，不许冒充券商原值。"""
    o = _build(num=_num(source="DERIVED_portfolio+executions",
                        basis="NET_LIQ_CHANGE_UNADJUSTED", quality="DEGRADED"))
    assert o.quality_status == dpo.Q_DEGRADED
    assert o.reliable is False
    assert dpo.R_DEGRADED_SOURCE in o.quality_reasons


def test_06b_unadjusted_basis_flags_cash_flow():
    """未校准净值变动 + 资金流覆盖未确认 -> 额外记一条原因。
    出入金会把净值变动伪装成盈亏，这个必须写下来。"""
    o = _build(num=_num(source="DERIVED_portfolio+executions",
                        basis="NET_LIQ_CHANGE_UNADJUSTED", quality="DEGRADED",
                        flow_coverage_ok=False))
    assert dpo.R_FLOW_UNCONFIRMED in o.quality_reasons


def test_07_degraded_does_not_open_event():
    """DEGRADED 不打开事件 —— outcome 是 insufficient_data，不是 triggered。"""
    o = _build(num=_num(daily_pnl=D("-4000"),        # 10% 亏损，远超阈值
                        source="DERIVED_portfolio+executions",
                        basis="NET_LIQ_CHANGE_UNADJUSTED", quality="DEGRADED"))
    assert o.daily_loss_ratio > THR                 # 数字确实超标
    assert dpo.outcome_for(o, THR)[0] == dpo.OUT_INSUFFICIENT
    assert o.event_transition_allowed is False


def test_08_degraded_does_not_resolve_event():
    """DEGRADED 也不恢复事件。**缺失不等于恢复** —— 这是取证里 11 次假恢复
    的根本教训，比不打开更重要。"""
    o = _build(num=_num(daily_pnl=D("-100"),         # 0.25%，看着像"已恢复"
                        source="DERIVED_portfolio+executions",
                        basis="NET_LIQ_CHANGE_UNADJUSTED", quality="DEGRADED"))
    assert o.daily_loss_ratio < THR
    assert dpo.outcome_for(o, THR)[0] == dpo.OUT_INSUFFICIENT
    assert o.event_transition_allowed is False


def test_09_missing_data_keeps_state():
    """数据缺失时状态保持：影子重放里 insufficient 样本既不开也不关。"""
    obs = [
        {"daily_loss_ratio": "0.05", "reliable": 1},   # 开
        {"daily_loss_ratio": None, "reliable": 0},     # 缺 -> 保持
        {"daily_loss_ratio": "0.01", "reliable": 0},   # 看着恢复但不可靠 -> 保持
        {"daily_loss_ratio": "0.06", "reliable": 1},   # 仍然超标
    ]
    from decimal import Decimal
    st, opened, resolved = False, 0, 0
    for o in obs:
        if not o["reliable"]:
            continue
        r = Decimal(o["daily_loss_ratio"]) if o["daily_loss_ratio"] else None
        if r is None:
            continue
        if not st and r > THR:
            st, opened = True, opened + 1
        elif st and r <= THR:
            st, resolved = False, resolved + 1
    assert (opened, resolved) == (1, 0)     # 全程一个 episode，没有假恢复


def test_10_source_switch_does_not_auto_transition():
    """来源切换本身不构成迁移理由。切换后首个样本没闭合就明确记下来。"""
    o = _build(num=_num(source="DERIVED_portfolio+executions",
                        basis="NET_LIQ_CHANGE_UNADJUSTED", quality="DEGRADED"),
               previous_source="IBKR_reqPnL")
    assert o.source_changed is True
    assert dpo.R_SOURCE_SWITCH_UNCLOSED in o.quality_reasons
    assert o.event_transition_allowed is False

    # 切换后能闭合的话，切换标记仍在，但不影响可靠性
    o2 = _build(previous_source="DERIVED_portfolio+executions")
    assert o2.source_changed is True
    assert o2.reliable is True
    assert dpo.R_SOURCE_SWITCH_UNCLOSED not in o2.quality_reasons


# ---------------- 11–12. 盈利语义 ----------------

def test_11_profit_ratio_is_zero():
    """盈利时亏损率是 **0**，不是 None。None 会被误读成「算不出来」。"""
    o = _build(num=_num(daily_pnl=D("500")))
    assert o.daily_loss_ratio == D("0")
    o0 = _build(num=_num(daily_pnl=D("0")))
    assert o0.daily_loss_ratio == D("0")


def test_12_profit_outcome_is_not_triggered():
    """盈利的结论是 not_triggered，理由明确写「当日未发生亏损」。"""
    o = _build(num=_num(daily_pnl=D("500")))
    outcome, ratio, reason = dpo.outcome_for(o, THR)
    assert outcome == dpo.OUT_NOT_TRIGGERED
    assert ratio == D("0")
    assert reason == dpo.REASON_NO_LOSS
    # 亏了但没超阈值：同样 not_triggered，但理由不同
    o2 = _build(num=_num(daily_pnl=D("-400")))
    assert dpo.outcome_for(o2, THR)[2] == dpo.REASON_WITHIN


def test_12b_engine_profit_is_zero_not_none():
    """规则引擎侧同步修正：盈利 -> 0，缺字段 -> None。"""
    assert re_._loss_ratio(D("-0.04")) == D("0.04")
    assert re_._loss_ratio(D("0.02")) == D("0")
    assert re_._loss_ratio(D("0")) == D("0")
    assert re_._loss_ratio(None) is None


def test_12c_engine_reason_distinguishes_profit(make_config):
    """引擎产出的 not_triggered 要能区分「没亏」与「亏了但没超」。"""
    cfg = make_config()
    rules = re_.default_rules(cfg)

    def outcome_of(ratio):
        m = {"account": {}, "concentration": {}, "drawdown": {}, "liquidity": {},
             "pnl": {"daily_pnl_ratio": ratio}, "positions": []}
        res = re_.evaluate_rules(m, rules, collect_outcomes=True)
        outs = res[1] if isinstance(res, tuple) else res
        return next(o for o in outs if o.rule_code == "ACC_DAILY_LOSS_HIGH")

    assert outcome_of(D("0.01")).reason == re_.REASON_NO_DAILY_LOSS
    assert outcome_of(D("-0.01")).reason == re_.REASON_DAILY_LOSS_WITHIN
    assert outcome_of(D("-0.05")).outcome == re_.OUTCOME_TRIGGERED
    assert outcome_of(None).outcome == re_.OUTCOME_INSUFFICIENT


# ---------------- 13–15. 交易日与资金流 ----------------

def test_13_same_session_date_ok():
    o = _build()
    assert o.session_date == SESSION
    assert o.reliable is True


def test_14_cross_session_date_forbidden():
    """跨交易日禁止拼接。dailyPnL 每个交易日重置，跨日配对必然错。"""
    o = _build(num=_num(session_date="2026-07-28"))
    assert o.reliable is False
    assert dpo.R_SESSION_MISMATCH in o.quality_reasons


def test_15_external_cash_flow_recorded():
    """外部现金流调整额随观测落库，事后能核对口径。"""
    o = _build(num=_num(external_cash_flow_adjustment=D("-500")))
    assert o.external_cash_flow_adjustment == D("-500")
    assert o.as_row()["external_cash_flow_adjustment"] == "-500"


def test_15b_currency_mismatch_forbidden():
    o = _build(num=_num(currency="HKD"))
    assert o.reliable is False
    assert dpo.R_CURRENCY_MISMATCH in o.quality_reasons


def test_15c_stale_observation_flagged():
    old = (NOW - timedelta(hours=3)).isoformat()
    o = _build(num=_num(observed_at=old), den=_den(observed_at=old),
               max_age_seconds=600)
    assert o.reliable is False
    assert dpo.R_STALE in o.quality_reasons


# ---------------- 16–17. 质量传导 ----------------

def test_16_quality_propagates_to_assessment(make_config):
    """PnL 质量必须能传到评估层，而且**影子期不改行为**。"""
    from app import risk_service as rs
    cfg = make_config()

    class Ctx:
        quality_status = "fresh"
        ai_analysis_allowed = True
        daily_pnl_observation = _build(
            num=_num(source="DERIVED_portfolio+executions",
                     basis="NET_LIQ_CHANGE_UNADJUSTED", quality="DEGRADED"))

    off = rs.pnl_quality_propagation(Ctx(), enabled=False)
    assert off["pnl_quality_status"] == dpo.Q_DEGRADED   # 真实质量已如实上报
    assert off["pnl_reliable"] is False
    assert dpo.R_DEGRADED_SOURCE in off["pnl_quality_reasons"]
    # 影子期：不动 quality_status / ai_analysis_allowed
    assert off["quality_status"] == "fresh"
    assert off["ai_analysis_allowed"] is True
    assert off["reliable_downgraded"] is False
    assert off["applied"] is False


def test_17_ai_allowed_propagates_when_enabled(make_config):
    """v2 打开后，不可靠的当日盈亏要禁掉 AI 结论。"""
    from app import risk_service as rs

    class Ctx:
        quality_status = "fresh"
        ai_analysis_allowed = True
        daily_pnl_observation = _build(num=_num(correlation_id="other"))

    on = rs.pnl_quality_propagation(Ctx(), enabled=True)
    assert on["reliable_downgraded"] is True
    assert on["ai_analysis_allowed"] is False
    assert on["applied"] is True

    class OkCtx(Ctx):
        daily_pnl_observation = _build()

    good = rs.pnl_quality_propagation(OkCtx(), enabled=True)
    assert good["reliable_downgraded"] is False
    assert good["ai_analysis_allowed"] is True


# ---------------- 18–20. 影子期纪律 ----------------

def test_18_shadow_writes_no_events(db):
    """影子计算**不写实时事件**。跑完 record 后事件表必须仍是空的。"""
    cfg, d = db
    o = _build()
    dps.record(cfg, d, o, snapshot_id=None, threshold=THR)
    rows, total = d.query_risk_events()
    assert total == 0
    assert rows == []


def test_19_shadow_sends_no_notification(db):
    """影子计算不发通知。通知表必须仍是空的。"""
    cfg, d = db
    dps.record(cfg, d, _build(), snapshot_id=None, threshold=THR)
    with d.connect() as c:
        assert c.execute("SELECT COUNT(*) FROM risk_notifications").fetchone()[0] == 0


def test_20_comparison_is_idempotent(db):
    """同一时点重复比较不产生第二行 —— 重跑同步不该把统计撑大。"""
    cfg, d = db
    o = _build()
    r1 = dps.record(cfg, d, o, snapshot_id=None, threshold=THR)
    r2 = dps.record(cfg, d, o, snapshot_id=None, threshold=THR)
    assert r1["observation_written"] is True
    assert r2["observation_written"] is False
    assert r2["comparison_written"] is False
    assert len(d.daily_pnl_comparisons_since("2026-07-01")) == 1


def test_20b_divergence_classification():
    assert dpo.classify_divergence(None, None, "a", "a") == dpo.DIV_BOTH_NONE
    assert dpo.classify_divergence("0.04", None, "a", "b") == dpo.DIV_OLD_ONLY
    assert dpo.classify_divergence(None, "0.04", "a", "b") == dpo.DIV_NEW_ONLY
    assert dpo.classify_divergence("0.04", "0.05", "t", "n") == dpo.DIV_OUTCOME
    assert dpo.classify_divergence("0.04", "0.05", "t", "t") == dpo.DIV_RATIO_ONLY
    assert dpo.classify_divergence("0.04", "0.04", "t", "t") == dpo.DIV_SAME


def test_20c_shadow_summary_counts_blocked_transitions(db):
    """影子汇总要能数出 v2 挡掉了多少次假打开/假恢复。"""
    cfg, d = db
    rows = [
        ("2026-07-29T14:00:00+00:00", "0.05", 1),   # 开
        ("2026-07-29T14:01:00+00:00", "0.02", 0),   # 不可靠的"恢复" -> 被挡
        ("2026-07-29T14:02:00+00:00", "0.06", 1),   # 仍超标
    ]
    for ts, ratio, rel in rows:
        d.insert_daily_pnl_observation({
            "account_id": "U1", "session_date": SESSION, "observed_at": ts,
            "daily_loss_ratio": ratio, "quality_status": "OK" if rel else "DEGRADED",
            "reliable": bool(rel), "quality_reasons": [],
            "calculation_version": dpo.CALC_VERSION})
    s = dps.shadow_summary(d, "2026-07-01", threshold=THR)
    assert s["samples_total"] == 3
    assert s["v2_reliable"] == 2
    assert s["blocked_false_resolves"] == 1      # 那次假恢复被挡住了
    # 这三行就是整个修复的价值所在：同一份数据，旧口径被那次不可靠的 0.02
    # 骗出一次假恢复、随后重开成第二个 episode；v2 把它挡住，全程一个 episode。
    assert s["old_episode_sim"]["episodes"] == 2
    assert s["old_episode_sim"]["resolved"] == 1
    assert s["v2_episode_sim"]["episodes"] == 1
    assert s["v2_episode_sim"]["resolved"] == 0


# ---------------- 21–23. 版本 / 精度 / 时区 ----------------

def test_21_calculation_version_recorded():
    o = _build()
    assert o.calculation_version == dpo.CALC_VERSION
    assert "daily-pnl-obs-v2" in dpo.CALC_VERSION
    assert o.as_row()["calculation_version"] == dpo.CALC_VERSION


def test_22_decimal_precision_preserved():
    """全程 Decimal，不经 float。取证已证明最小穿越余量 0.0477pp 比 float
    误差大 14 个数量级，但保持精确仍是底线 —— 精度问题不该由运气兜着。"""
    o = _build(num=_num(daily_pnl=D("-1234.56789012345678901234567890")),
               den=_den(net_liquidation=D("40000.00000000000000000001")))
    assert isinstance(o.daily_loss_ratio, D)
    exact = D("1234.56789012345678901234567890") / D("40000.00000000000000000001")
    assert o.daily_loss_ratio == exact
    # 落库是字符串，不丢位
    assert o.as_row()["daily_pnl"] == "-1234.56789012345678901234567890"


def test_23_timezone_handling():
    """naive 时间按 UTC 处理；带偏移的时间正确换算后再算 skew。"""
    naive = "2026-07-30T02:00:00"
    o = _build(num=_num(observed_at=naive))
    assert o.observation_skew_seconds == D("0.0")

    # +08:00 的 01:30 == UTC 17:30 前一天，与 UTC 02:00 差 8.5 小时
    o2 = _build(num=_num(observed_at="2026-07-30T10:00:00+08:00"))
    assert o2.observation_skew_seconds == D("0.0")    # 同一时刻，不同写法
    assert o2.reliable is True

    o3 = _build(num=_num(observed_at="2026-07-30T02:00:00+08:00"))
    assert o3.observation_skew_seconds == D("28800.0")
    assert dpo.R_SKEW_EXCEEDED in o3.quality_reasons


def test_23b_diagnostics_api_is_readonly(make_config):
    """诊断接口只读：调用前后事件、通知、观测数量都不变。"""
    from fastapi.testclient import TestClient
    from app.webapp import create_app
    cfg = make_config(ib_account="U1")
    d = Database(cfg.db_path)
    d.init_schema()
    c = TestClient(create_app(cfg, d))
    before = len(d.daily_pnl_observations_since("2026-01-01"))
    r = c.get("/api/v1/risk/daily-pnl/diagnostics")
    assert r.status_code == 200
    data = r.json()["data"]
    assert data["v2_enabled"] is False
    assert data["calculation_version"] == dpo.CALC_VERSION
    assert "shadow" in data and "latest" in data
    assert data["skew_tolerance_seconds"] == 90
    assert len(d.daily_pnl_observations_since("2026-01-01")) == before
    with d.connect() as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM risk_notifications").fetchone()[0] == 0


# ---------------- 部署验证发现的时序问题 ----------------

def test_24_pipeline_order_is_not_data_loss():
    """「分子还没写」与「分子真的没有」必须分开记。

    部署验证时撞上的真实问题：sync 先跑风险评估、再写 PnL 快照，所以评估时刻
    同批次分子必然缺席。当时被记成 NUMERATOR_MISSING + CROSS_CYCLE_PAIRING，
    读起来像数据坏了 —— 实际上同周期的分子分母好得很，只是评估看不到。

    这个区分不是措辞讲究：影子报告要靠它判断「v2 能不能切」。
    """
    o = dpo.build(account_id="U1", numerator=None, denominator=_den(),
                  correlation_id="run-A", numerator_pending=True, now=NOW)
    assert o.reliable is False                       # 仍然不许驱动迁移
    assert dpo.R_PIPELINE_ORDER in o.quality_reasons
    assert dpo.R_NO_NUMERATOR not in o.quality_reasons
    assert dpo.R_CROSS_CYCLE not in o.quality_reasons
    assert "管道时序" in dpo.REASON_TEXT[dpo.R_PIPELINE_ORDER]

    # 真缺数据时仍然报 NUMERATOR_MISSING，没被这个分支吞掉
    o2 = dpo.build(account_id="U1", numerator=None, denominator=_den(),
                   correlation_id="run-A", numerator_pending=False, now=NOW)
    assert dpo.R_NO_NUMERATOR in o2.quality_reasons
    assert dpo.R_PIPELINE_ORDER not in o2.quality_reasons


def test_24b_no_phantom_source_switch_when_source_unknown():
    """分子还没到手时不许报「来源变了」—— 那是噪音，会污染切换次数统计。"""
    o = dpo.build(account_id="U1", numerator=None, denominator=_den(),
                  correlation_id="run-A", previous_source="IBKR_reqPnL",
                  numerator_pending=True, now=NOW)
    assert o.source_changed is False
    assert dpo.R_SOURCE_SWITCH_UNCLOSED not in o.quality_reasons


def test_24c_v2_switch_would_starve_under_current_ordering(make_config):
    """**切换前必须证明的事**：在现行写入顺序下打开 v2，每个样本都会是
    insufficient_data，当日亏损规则将永久不再触发。

    这是个静默失效 —— 没有报错、没有告警，只是规则从此不响。所以这条测试
    把它钉成一个显式事实，避免有人只翻了开关就以为完事。
    """
    from app import risk_service as rs

    class Ctx:
        quality_status = "fresh"
        ai_analysis_allowed = True
        daily_pnl_observation = dpo.build(
            account_id="U1", numerator=None, denominator=_den(),
            correlation_id="run-A", numerator_pending=True, now=NOW)

    on = rs.pnl_quality_propagation(Ctx(), enabled=True)
    assert on["pnl_event_transition_allowed"] is False
    assert on["reliable_downgraded"] is True
    # 结论：切 v2 的前置条件是先把 PnL 快照写到评估之前
    assert dpo.R_PIPELINE_ORDER in on["pnl_quality_reasons"]
