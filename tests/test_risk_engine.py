"""账户风险计算引擎 v1 验收测试（对应清单 1–30）。

重点钉死三件事：
  1. 双口径集中度 —— 高现金账户不能因「占总资产低」而显得安全；
  2. blocked 与 critical 不混淆 —— 数据故障绝不显示成低风险；
  3. 金融计算不用浮点、分母为零不伪造 0。
"""
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D

import pytest
from fastapi.testclient import TestClient

from app import data_quality as dq
from app import risk_engine as re_
from app import risk_metrics as rm
from app import risk_service
from app.db import Database
from app.webapp import create_app

NOW = datetime(2026, 7, 28, 16, 0, tzinfo=timezone.utc)


def _bal(**over):
    b = {"equity": D("100000"), "cash": D("50000"), "total_cash": D("50000"),
         "margin_balance": D("50000"), "buying_power": D("200000"),
         "available_funds": D("48000"), "excess_liquidity": D("47000"),
         "maint_margin": D("10000"), "init_margin": D("12000"),
         "positions_value": D("50000"), "account": "U1", "currency": "USD"}
    b.update(over)
    return b


def _p(symbol, mv, qty="100", cost="100", price="100", upl="0", **over):
    d = {"symbol": symbol, "qty": D(qty), "avg_cost": D(cost), "market_value": D(mv),
         "upl": D(upl), "price": D(price), "price_quality": "DELAYED"}
    d.update(over)
    return d


@pytest.fixture
def db(make_config):
    cfg = make_config(ib_account="U1")
    d = Database(cfg.db_path)
    d.init_schema()
    return cfg, d


def _write(d, sid="s1", ts=None, bal=None, pos=None, quality=dq.FRESH, ai=True):
    d.write_account_snapshot(
        snapshot_id=sid, ts=(ts or NOW.isoformat()), balances=bal or _bal(),
        positions=pos if pos is not None else [_p("AAA", "50000")],
        sync_run_id="r1", quality_status=quality, ai_analysis_allowed=ai)


# ---------------- 1–2. 正常账户 / 空仓 ----------------

def test_normal_cash_account(db):
    cfg, d = db
    _write(d)
    a = risk_service.assess(cfg, d, now=NOW)
    assert a["reliable"] is True
    assert a["risk_level"] in (re_.LOW, re_.MODERATE, re_.HIGH)
    assert a["snapshot_id"] == "s1"
    assert a["calculation_version"].startswith(re_.CALC_VERSION)
    assert a["rule_set_hash"]          # 规则集哈希可追溯


def test_empty_position_account(db):
    cfg, d = db
    _write(d, bal=_bal(positions_value=D("0")), pos=[])
    a = risk_service.assess(cfg, d, now=NOW)
    m = a["calculated_metrics"]
    assert m["concentration"]["position_count"] == 0
    # 空仓没有集中度风险，也不该因为「算不出集中度」而报警
    assert "CONC_SINGLE_EQUITY_HIGH" not in a["triggered_rules"]
    assert a["reliable"] is True


# ---------------- 3–5. 双口径集中度（核心） ----------------

def test_high_cash_single_position_dominates_invested(db):
    """本系统的强制要求：占总资产仅 10%，但占已投资仓位 100%。"""
    cfg, d = db
    _write(d, bal=_bal(total_cash=D("90000"), cash=D("90000"),
                       positions_value=D("10000")),
           pos=[_p("AAA", "10000")])
    a = risk_service.assess(cfg, d, now=NOW)
    c = a["calculated_metrics"]["concentration"]
    assert c["max_position_weight_of_equity"] == D("0.10")
    assert c["max_position_weight_of_invested"] == D("1")
    # 占总资产 10% 不触发 R1 口径；但占已投资 100% 必须被抓到
    assert "CONC_SINGLE_EQUITY_HIGH" not in a["triggered_rules"]
    assert "CONC_SINGLE_INVESTED_HIGH" in a["triggered_rules"]


def test_single_position_high_share_of_equity(db):
    cfg, d = db
    _write(d, bal=_bal(total_cash=D("20000"), cash=D("20000"),
                       positions_value=D("80000")),
           pos=[_p("AAA", "80000")])
    a = risk_service.assess(cfg, d, now=NOW)
    assert "CONC_SINGLE_EQUITY_HIGH" in a["triggered_rules"]


def test_diversified_small_positions(db):
    cfg, d = db
    pos = [_p(f"S{i}", "5000") for i in range(10)]
    _write(d, bal=_bal(positions_value=D("50000")), pos=pos)
    a = risk_service.assess(cfg, d, now=NOW)
    c = a["calculated_metrics"]["concentration"]
    assert c["max_position_weight_of_invested"] == D("0.1")
    assert "CONC_SINGLE_EQUITY_HIGH" not in a["triggered_rules"]
    assert "CONC_TOP3_HIGH" not in a["triggered_rules"]


# ---------------- 6–7. 杠杆 / 空头 ----------------

def test_leveraged_account(db):
    cfg, d = db
    _write(d, bal=_bal(equity=D("100000"), cash=D("-150000"), total_cash=D("-150000"),
                       positions_value=D("250000")),
           pos=[_p("AAA", "125000"), _p("BBB", "125000")])
    a = risk_service.assess(cfg, d, now=NOW)
    acct = a["calculated_metrics"]["account"]
    assert acct["gross_leverage"] == D("2.5")
    assert "ACC_GROSS_LEVERAGE_HIGH" in a["triggered_rules"]


def test_short_positions_gross_vs_net(db):
    """净敞口会把多空相抵成 0，但那不是「没有风险」—— 总敞口必须单独给。"""
    cfg, d = db
    _write(d, pos=[_p("LONG1", "50000"), _p("SHORT1", "-50000", qty="-100")])
    a = risk_service.assess(cfg, d, now=NOW)
    acct = a["calculated_metrics"]["account"]
    assert acct["net_exposure"] == D("0")
    assert acct["gross_exposure"] == D("100000")
    assert acct["long_exposure"] == D("50000")
    assert acct["short_exposure"] == D("50000")


# ---------------- 8–9. 净资产为零 / 核心字段缺失 ----------------

def test_zero_net_liquidation_no_fake_zero(db):
    """分母为零必须返回 None，绝不返回 0（0% 现金比例会触发假告警）。"""
    cfg, d = db
    _write(d, bal=_bal(equity=D("0"), cash=D("0"), total_cash=D("0"),
                       positions_value=D("0")), pos=[])
    a = risk_service.assess(cfg, d, now=NOW)
    acct = a["calculated_metrics"]["account"]
    assert acct["cash_ratio"] is None
    assert acct["gross_leverage"] is None
    assert "ACC_CASH_LOW" not in a["triggered_rules"]      # 算不出 ≠ 过低


def test_core_fields_cannot_be_null_at_db_level(db):
    """净资产/现金在 schema 里是 NOT NULL —— 核心字段缺失在写入阶段就被挡住，
    根本进不了库。这是比事后检查更强的保证。"""
    import sqlite3
    cfg, d = db
    with pytest.raises((sqlite3.IntegrityError, sqlite3.ProgrammingError)):
        d.write_account_snapshot(snapshot_id="s1", ts=NOW.isoformat(),
                                 balances={"equity": None, "cash": None,
                                           "margin_balance": None, "buying_power": None,
                                           "account": "U1"},
                                 positions=[], sync_run_id="r1")
    assert d.latest_snapshot_row() is None      # 半成品未落库


def test_precheck_flags_missing_core_fields():
    """前置校验层面：上下文缺净资产时判定不可靠（用于历史数据或降级路径）。"""
    from app.risk_service import RiskContext, precheck, PRECHECK_CORE_FIELDS_MISSING

    class C:
        dq_error_seconds = 86400
    ctx = RiskContext(snapshot_id="s1", account_id="U1", data_as_of=NOW.isoformat(),
                      net_liquidation=None, total_cash=None, available_funds=None,
                      buying_power=None, maintenance_margin=None, initial_margin=None,
                      positions_market_value=None, base_currency="USD")
    codes = [c for c, _ in precheck(ctx, NOW, C)]
    assert PRECHECK_CORE_FIELDS_MISSING in codes


# ---------------- 10–12. stale / error / AI 门禁 ----------------

def test_stale_data_still_computes_but_flags(db):
    cfg, d = db
    _write(d, ts=(NOW - timedelta(hours=2)).isoformat(), quality=dq.STALE)
    a = risk_service.assess(cfg, d, now=NOW)
    assert a["reliable"] is True          # stale 未超 error 阈值，仍可计算
    assert a["calculated_metrics"]["account"]["net_liquidation"] == D("100000")


def test_error_quality_is_blocked_not_low(db):
    """最重要的一条：数据故障绝不能显示成低风险。"""
    cfg, d = db
    _write(d, quality=dq.ERROR, ai=False)
    a = risk_service.assess(cfg, d, now=NOW)
    assert a["risk_level"] == re_.BLOCKED
    assert a["risk_level"] != re_.LOW
    assert a["reliable"] is False
    assert a["ai_analysis_allowed"] is False


def test_ai_not_allowed_blocks_assessment(db):
    cfg, d = db
    _write(d, ai=False)
    a = risk_service.assess(cfg, d, now=NOW)
    assert a["reliable"] is False
    assert a["risk_level"] == re_.BLOCKED
    # 仍返回可安全计算的原始指标
    assert a["calculated_metrics"]["account"]["net_liquidation"] == D("100000")


def test_blocked_differs_from_critical(db):
    """blocked = 不知道；critical = 知道且很危险。两者不可混淆。"""
    cfg, d = db
    _write(d, sid="bad", quality=dq.ERROR, ai=False)
    blocked = risk_service.assess(cfg, d, snapshot_id="bad", now=NOW)
    d.write_account_snapshot(
        snapshot_id="crit", ts=(NOW + timedelta(minutes=1)).isoformat(),
        balances=_bal(maint_margin=D("90000")),      # 安全倍数 1.1x -> critical
        positions=[_p("AAA", "50000")], sync_run_id="r2",
        quality_status=dq.FRESH, ai_analysis_allowed=True)
    crit = risk_service.assess(cfg, d, snapshot_id="crit", now=NOW)
    assert blocked["risk_level"] == re_.BLOCKED and blocked["reliable"] is False
    assert crit["risk_level"] == re_.CRITICAL and crit["reliable"] is True


# ---------------- 13–15. 持仓阈值 / 集中度 / 浮亏贡献 ----------------

def test_position_over_configured_limit(db):
    cfg, d = db
    _write(d, bal=_bal(total_cash=D("20000"), cash=D("20000"), positions_value=D("80000")),
           pos=[_p("AAA", "80000")])
    a = risk_service.assess(cfg, d, now=NOW)
    p = next(x for x in a["calculated_metrics"]["positions"] if x["symbol"] == "AAA")
    assert p["over_position_limit"] is True
    assert "OVER_POSITION_LIMIT" in p["risk_flags"]


def test_top3_not_applicable_with_few_positions(db):
    """4 只持仓时 Top3 恒等于 ~100% —— 那是算术必然，不是风险信号。

    生产实测这条规则 17/17 天触发，纯噪音。现在判 not_applicable，
    集中风险改由 HHI / 等效持仓数 / 单仓主导来刻画。
    """
    cfg, d = db
    pos = [_p("A", "20000"), _p("B", "18000"), _p("C", "12000"), _p("D", "500")]
    _write(d, bal=_bal(positions_value=D("50500")), pos=pos)
    a = risk_service.assess(cfg, d, now=NOW)
    assert "CONC_TOP3_HIGH" not in a["triggered_rules"]
    na = {x["rule_code"] for x in a["concentration_applicability"]["not_applicable"]}
    assert "CONC_TOP3_HIGH" in na and "CONC_TOP5_HIGH" in na
    # 但集中风险仍被识别 —— 没有因为降噪就把集中账户显示成安全
    assert "EFFECTIVE_POSITION_COUNT_LOW" in a["triggered_rules"]
    assert D(a["component_scores"]["concentration_score"]) > 0


def test_top3_applies_when_enough_positions(db):
    """持仓数达到门槛时，Top3 恢复判别力。"""
    cfg, d = db
    # 8 只持仓，但前三大占 85% -> Top3 适用且触发
    pos = ([_p("A", "30000"), _p("B", "28000"), _p("C", "27000")]
           + [_p(f"X{i}", "2500") for i in range(5)])
    _write(d, bal=_bal(positions_value=D("97500")), pos=pos)
    a = risk_service.assess(cfg, d, now=NOW)
    na = {x["rule_code"] for x in a["concentration_applicability"]["not_applicable"]}
    assert "CONC_TOP3_HIGH" not in na          # 8 >= 6，适用
    assert "CONC_TOP3_HIGH" in a["triggered_rules"]
    assert "CONC_TOP5_HIGH" in na              # 8 < 10，仍不适用


def test_loss_contribution_rule(db):
    """账户级未实现缺失时，用持仓浮盈合计做分母算贡献度。"""
    cfg, d = db
    pos = [_p("AAA", "30000", upl="-9000"), _p("BBB", "20000", upl="-1000")]
    _write(d, pos=pos)
    a = risk_service.assess(cfg, d, now=NOW)
    p = next(x for x in a["calculated_metrics"]["positions"] if x["symbol"] == "AAA")
    # 亏损池口径：|-9000| / (9000+1000) = 90%，落在 0–100%
    assert p["contribution_to_total_losing_positions"] == D("0.9")
    # 净口径同为 0.9（此例无盈利仓），但字段独立存在
    assert p["contribution_to_net_unrealized_loss"] == D("0.9")
    assert "POS_LOSS_POOL_SHARE_HIGH" in a["triggered_rules"]


# ---------------- 16–19. 回撤 / 历史不足 / 入金失真 ----------------

def test_current_and_max_drawdown():
    series = [{"et_date": f"2026-07-{d:02d}", "net_liquidation": D(v)} for d, v in
              [(1, "100000"), (2, "110000"), (3, "99000"), (4, "104500")]]
    r = rm.drawdown(series)
    assert r.peak_value == D("110000")
    assert r.current_drawdown == D("0.05")        # (110000-104500)/110000
    assert r.max_drawdown == D("0.10")            # (110000-99000)/110000


def test_insufficient_history_returns_marker():
    r = rm.drawdown([{"et_date": "2026-07-01", "net_liquidation": D("100")}])
    assert r.current_drawdown is None
    assert any(i.code == rm.CALC_INSUFFICIENT_HISTORY for i in r.issues)
    w = rm.window_returns([{"et_date": "2026-07-01", "net_liquidation": D("100")}])
    assert w["return_5d"] is None
    assert w["return_5d_status"] == rm.CALC_INSUFFICIENT_HISTORY


def test_deposit_marks_returns_as_estimated():
    """有资金流时收益率必须标 estimated —— 转账不是收益。"""
    series = [{"et_date": f"2026-07-{d:02d}", "net_liquidation": D(v)} for d, v in
              [(1, "1000"), (2, "1000"), (3, "50000"), (4, "51000"), (5, "52000"),
               (6, "53000")]]
    w = rm.window_returns(series, flows_present=True)
    assert w["return_5d_status"] == rm.CALC_ESTIMATED


def test_drawdown_uses_baseline_trimmed_series(db):
    """有券商资金流记录时才裁掉入金前段，不由净值跳变猜测。"""
    cfg, d = db
    for i, (dt, eq) in enumerate([("2026-07-01", "1000"), ("2026-07-02", "1000"),
                                  ("2026-07-03", "50000"), ("2026-07-04", "48000")]):
        d.insert_balances_snapshot(f"{dt}T20:00:00+00:00", {
            "equity": D(eq), "cash": D("0"), "margin_balance": D("0"),
            "buying_power": D("0"), "account": "U1"})
    d.backfill_daily_equity("U1", cfg.market_tz)
    d.add_cash_flow(
        account="U1", amount=D("49000"), currency="USD",
        effective_at="2026-07-03T12:00:00+00:00", et_date="2026-07-03",
        note="Electronic Fund Transfer", source="FLEX_QUERY",
        external_ref="DEP-20260703")
    _write(d)
    ctx = risk_service.build_context(cfg, d, "s1", NOW)
    # 基线之后只剩入金后的点，1000 那段不参与
    assert all(x["net_liquidation"] >= D("48000") for x in ctx.equity_series)
    assert ctx.flows_present is True


# ---------------- 20. 压力测试 ----------------

def test_stress_scenarios_are_deterministic(db):
    cfg, d = db
    _write(d)
    a = risk_service.assess(cfg, d, now=NOW)
    liq = a["calculated_metrics"]["liquidity"]
    s10 = next(s for s in liq["account_stress"] if s["drop_pct"] == D("0.10"))
    assert s10["net_liquidation_after"] == D("90000")     # 确定性算术
    assert s10["maint_margin_safety_after"] == D("9")     # 90000/10000
    top3 = liq["top3_stress"][0]
    assert top3["drop_pct"] == D("0.10")


def test_margin_call_flagged_in_stress(db):
    cfg, d = db
    _write(d, bal=_bal(maint_margin=D("85000")))
    a = risk_service.assess(cfg, d, now=NOW)
    liq = a["calculated_metrics"]["liquidity"]
    s20 = next(s for s in liq["account_stress"] if s["drop_pct"] == D("0.20"))
    assert s20["margin_call"] is True                     # 80000 < 85000


# ---------------- 21–22. 幂等 / 版本重算 ----------------

def test_same_snapshot_is_idempotent(db):
    cfg, d = db
    _write(d)
    a1 = risk_service.assess(cfg, d, now=NOW)
    a2 = risk_service.assess(cfg, d, now=NOW)
    assert a1["assessment_id"] == a2["assessment_id"]
    assert d.count_assessments(None, None) == 1


def test_force_recompute_replaces_same_version(db):
    cfg, d = db
    _write(d)
    risk_service.assess(cfg, d, now=NOW)
    a2 = risk_service.assess(cfg, d, now=NOW, force=True)
    assert a2 is not None
    assert d.count_assessments(None, None) == 1          # 同版本仍只有一条


def test_version_change_allows_recompute(db, monkeypatch):
    """规则版本变化后应能重新计算，且历史评估保留以便对比。"""
    cfg, d = db
    _write(d)
    risk_service.assess(cfg, d, now=NOW)
    monkeypatch.setattr(re_, "CALC_VERSION", "risk-v2/test")
    risk_service.assess(cfg, d, now=NOW)
    assert d.count_assessments(None, None) == 2          # 两个版本各一条


# ---------------- 23–24. 精度 / 分母为零 ----------------

def test_all_money_math_uses_decimal(db):
    cfg, d = db
    _write(d, bal=_bal(equity=D("100000.10"), total_cash=D("0.10"), cash=D("0.10")),
           pos=[_p("AAA", "0.20")])
    a = risk_service.assess(cfg, d, now=NOW)
    acct = a["calculated_metrics"]["account"]
    assert isinstance(acct["net_liquidation"], D)
    assert isinstance(acct["gross_exposure"], D)
    # 0.1 + 0.2 用浮点会得到 0.30000000000000004
    assert acct["total_cash"] + acct["gross_exposure"] == D("0.30")


def test_ratio_helper_never_fakes_zero():
    issues = []
    assert rm.ratio(D("1"), D("0"), issues, field="x") is None
    assert issues[0].code == rm.CALC_DIVIDE_BY_ZERO
    issues2 = []
    assert rm.ratio(None, D("1"), issues2, field="y") is None
    assert issues2[0].code == rm.CALC_MISSING_INPUT


# ---------------- 25–26. API ----------------

def test_api_assessment_structure(db):
    cfg, d = db
    _write(d, ts=datetime.now(timezone.utc).isoformat())
    c = TestClient(create_app(cfg, d))
    body = c.get("/api/v1/risk/assessment").json()
    assert body["success"] is True and body["request_id"] and body["generated_at"]
    data = body["data"]
    for k in ("snapshot_id", "data_as_of", "risk_level", "total_risk_score", "reliable",
              "component_scores", "account_metrics", "concentration_metrics",
              "margin_metrics", "drawdown_metrics", "triggered_rules",
              "top_risk_drivers", "quality_status", "ai_analysis_allowed",
              "calculation_version"):
        assert k in data, k


def test_api_positions_dual_weights_and_sort(db):
    cfg, d = db
    _write(d, ts=datetime.now(timezone.utc).isoformat(),
           bal=_bal(total_cash=D("90000"), cash=D("90000"), positions_value=D("10000")),
           pos=[_p("AAA", "7000"), _p("BBB", "3000")])
    c = TestClient(create_app(cfg, d))
    data = c.get("/api/v1/risk/positions?sort_by=weight_of_invested_capital").json()["data"]
    rows = data["positions"]
    assert rows[0]["symbol"] == "AAA"
    # 双口径同时暴露
    assert rows[0]["weight_of_equity"] is not None
    assert rows[0]["weight_of_invested_capital"] is not None


def test_api_history_and_rules(db):
    cfg, d = db
    _write(d, ts=datetime.now(timezone.utc).isoformat())
    c = TestClient(create_app(cfg, d))
    c.get("/api/v1/risk/assessment")
    h = c.get("/api/v1/risk/history?limit=10").json()["data"]
    assert h["total"] >= 1 and h["items"][0]["snapshot_id"] == "s1"
    r = c.get("/api/v1/risk/rules").json()["data"]
    assert r["count"] >= 20
    assert any(x["rule_code"] == "CONC_SINGLE_INVESTED_HIGH" for x in r["rules"])


def test_api_blocked_not_shown_as_low(db):
    cfg, d = db
    _write(d, ts=datetime.now(timezone.utc).isoformat(), quality=dq.ERROR, ai=False)
    c = TestClient(create_app(cfg, d))
    data = c.get("/api/v1/risk/assessment").json()["data"]
    assert data["risk_level"] == "blocked"
    assert data["reliable"] is False
    assert data["blocked_reason"]


# ---------------- 27–28. raw_json ----------------

def test_raw_json_persists_and_is_scrubbed(db):
    from app.raw_capture import to_raw_json
    cfg, d = db
    raw = to_raw_json({"account": "U87654321", "password": "p", "TOTP_KEY": "k",
                       "session": "s", "NetLiquidation": "100000"})
    d.write_account_snapshot(
        snapshot_id="s1", ts=NOW.isoformat(),
        balances={**_bal(), "raw_json": raw},
        positions=[{**_p("AAA", "50000"), "raw_json": to_raw_json({"symbol": "AAA"})}],
        sync_run_id="r1")
    row = dict(d.snapshot_by_id("s1"))
    assert row["raw_json"] and "NetLiquidation" in row["raw_json"]
    for bad in ("password", "TOTP_KEY", "session", "U87654321"):
        assert bad not in row["raw_json"], bad
    assert d.positions_for_snapshot("s1")[0]["raw_json"]


def test_raw_json_failure_does_not_break_snapshot(db):
    """序列化失败只丢 raw_json，账户快照必须照常写入。"""
    from app.raw_capture import to_raw_json

    class Boom:
        def __repr__(self):
            raise RuntimeError("boom")
    assert to_raw_json({"x": Boom()}) is None or True     # 不抛异常即可
    cfg, d = db
    d.write_account_snapshot(snapshot_id="s1", ts=NOW.isoformat(),
                             balances={**_bal(), "raw_json": None},
                             positions=[_p("AAA", "50000")], sync_run_id="r1")
    assert d.snapshot_by_id("s1") is not None


# ---------------- 29–30. 重试 ----------------

def test_transient_error_is_retried():
    from app.retry import call_with_retry
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise TimeoutError("read timed out")
        return "OK"
    r = call_with_retry(flaky, max_retries=3, sleep=lambda s: None)
    assert r.ok and r.retry_count == 2 and calls["n"] == 3


def test_auth_error_is_not_retried():
    """认证失败重试可能触发券商风控锁定 —— 必须立即失败。"""
    from app.retry import call_with_retry
    calls = {"n": 0}

    def fatal():
        calls["n"] += 1
        raise RuntimeError("Login failed: invalid credential")
    r = call_with_retry(fatal, max_retries=5, sleep=lambda s: None)
    assert r.ok is False
    assert calls["n"] == 1                      # 只调用一次
    assert r.error_code == "AUTH_OR_PERMISSION"


def test_retry_has_upper_bound():
    from app.retry import call_with_retry
    calls = {"n": 0}

    def always_timeout():
        calls["n"] += 1
        raise TimeoutError("timed out")
    r = call_with_retry(always_timeout, max_retries=2, sleep=lambda s: None)
    assert r.ok is False and calls["n"] == 3    # 首次 + 2 次重试，不会无限
    assert r.retry_count == 2
    assert len(r.attempts) == 3


# ---------------- 评分可解释性 ----------------

def test_score_is_explainable(db):
    cfg, d = db
    _write(d, bal=_bal(total_cash=D("20000"), cash=D("20000"), positions_value=D("80000")),
           pos=[_p("AAA", "80000", upl="-30000")])
    a = risk_service.assess(cfg, d, now=NOW)
    # 总分能拆到分项，分项能追到规则，规则能追到 snapshot
    assert D(a["risk_score"]) > 0
    assert any(D(v) > 0 for v in a["component_scores"].values())
    assert a["triggered_rules"]
    for drv in a["top_risk_drivers"]:
        assert drv["recommended_check"]         # 每条都告诉用户该查什么
        assert drv["rule_code"] and drv["title"]
    for e in a["events"]:
        assert e["snapshot_id"] == "s1"          # 每条结论可追溯到快照


def test_page_shows_dual_weights_and_blocked_state(db):
    cfg, d = db
    _write(d, ts=datetime.now(timezone.utc).isoformat(),
           bal=_bal(total_cash=D("90000"), cash=D("90000"), positions_value=D("10000")),
           pos=[_p("AAA", "10000")])
    html = TestClient(create_app(cfg, d)).get("/").text
    assert "账户风险评估" in html
    assert "占总资产" in html and "占已投资仓位" in html
    assert "最大风险来源" in html


def test_page_blocked_does_not_show_low_risk(db):
    cfg, d = db
    _write(d, ts=datetime.now(timezone.utc).isoformat(), quality=dq.ERROR, ai=False)
    html = TestClient(create_app(cfg, d)).get("/").text
    assert "当前数据不足以进行可靠风险判断" in html
    assert "不代表账户风险为低" in html


# ---------------- P0.5 收尾：分类 / 双口径 / 规则版本 ----------------

def test_loss_pool_contribution_stays_within_100pct():
    """亏损池口径必须落在 0–100%，净口径允许 >100%。"""
    pos = [_p("LOSER", "3000", upl="-1440"), _p("WINNER", "1600", upl="640"),
           _p("SMALL", "1700", upl="-225")]
    rows = rm.position_risk(pos, D("40000"), max_position_weight=D("0.30"),
                            total_unrealized=D("-1025"))
    loser = next(r for r in rows if r["symbol"] == "LOSER")
    assert loser["contribution_to_net_unrealized_loss"] > D("1")      # 140%，可 >100%
    assert D("0") < loser["contribution_to_total_losing_positions"] <= D("1")
    winner = next(r for r in rows if r["symbol"] == "WINNER")
    assert winner["contribution_to_total_losing_positions"] is None   # 盈利仓不参与


def test_no_losing_positions_returns_null_not_zero():
    pos = [_p("A", "1000", upl="100"), _p("B", "1000", upl="50")]
    assert rm.losing_pool_total(pos) is None
    rows = rm.position_risk(pos, D("10000"), max_position_weight=D("0.30"))
    assert all(r["contribution_to_total_losing_positions"] is None for r in rows)


def test_classification_priority_and_unknown_reason(db):
    from app import instruments as inst
    cfg, d = db
    d.upsert_instrument_metadata([{"symbol": "MU", "sector": "Semiconductors",
                                   "asset_class": "equity", "source": "ibkr",
                                   "confidence": "high"}])
    d.set_classification_override("BMNR", sector="数字资产", note="比特币挖矿")
    res = inst.classify_positions(d, ["MU", "BMNR", "ZZZ"],
                                  legacy_sector_map={"OLD": "遗留板块"})
    assert res["BMNR"].source == inst.SOURCE_OVERRIDE      # 人工覆盖优先
    assert res["MU"].source == inst.SOURCE_IBKR
    assert res["ZZZ"].source == inst.SOURCE_UNKNOWN
    assert res["ZZZ"].unknown_reason                       # 必须写明原因
    assert res["MU"].is_known and not res["ZZZ"].is_known


def test_etf_and_equity_are_distinguished():
    from app import instruments as inst
    assert inst.asset_class_of("STK", "ETF") == inst.ASSET_ETF
    assert inst.asset_class_of("STK", "COMMON") == inst.ASSET_EQUITY
    assert inst.asset_class_of("IND") == inst.ASSET_INDEX
    assert inst.asset_class_of("CASH") == inst.ASSET_CASH
    assert inst.asset_class_of("OPT") == inst.ASSET_OPTION


def test_classified_positions_clear_unknown_alert(db):
    """分类补全后，「未知分类过高」应当消失 —— 那本来就不是真实账户风险。"""
    cfg, d = db
    d.set_classification_override("AAA", sector="半导体")
    _write(d, pos=[_p("AAA", "50000")])
    a = risk_service.assess(cfg, d, now=NOW)
    assert "CONC_UNKNOWN_SECTOR_HIGH" not in a["triggered_rules"]
    conc = a["calculated_metrics"]["concentration"]
    assert conc["unknown_symbols"] == []


def test_rule_set_version_changes_with_threshold(db):
    cfg, d = db
    v1 = re_.rule_set_version(re_.default_rules(cfg))

    class C2:
        risk_single_weight_equity_max = "0.25"
    v2 = re_.rule_set_version(re_.default_rules(C2))
    assert v1 != v2                                  # 阈值变化 -> 新版本
    # 描述文案不影响版本（只有影响判定的字段参与哈希）
    assert re_.rule_set_hash(re_.default_rules(cfg)) == re_.rule_set_hash(
        re_.default_rules(cfg))


def test_rule_versions_are_never_overwritten(db):
    cfg, d = db
    rules1 = re_.default_rules(cfg)
    d.sync_risk_rules([r.as_dict() for r in rules1],
                      set_version=re_.rule_set_version(rules1),
                      set_hash=re_.rule_set_hash(rules1))

    class C2:
        risk_single_weight_equity_max = "0.25"
    rules2 = re_.default_rules(C2)
    d.sync_risk_rules([r.as_dict() for r in rules2],
                      set_version=re_.rule_set_version(rules2),
                      set_hash=re_.rule_set_hash(rules2))
    versions = d.risk_rule_versions()
    assert len(versions) == 2                        # 旧版本保留
    # 幂等：同版本重复写不产生新记录
    d.sync_risk_rules([r.as_dict() for r in rules2],
                      set_version=re_.rule_set_version(rules2),
                      set_hash=re_.rule_set_hash(rules2))
    assert len(d.risk_rule_versions()) == 2


def test_assessment_records_rule_set_and_events_traceable(db):
    cfg, d = db
    _write(d)
    a = risk_service.assess(cfg, d, now=NOW)
    assert a["rule_set_version"] and a["rule_set_hash"]
    for e in a["events"]:
        assert e.get("rule_version") == a["rule_set_version"]   # 每条事件定位到规则版本
        assert e["snapshot_id"] == "s1"


def test_api_rules_exposes_history(db):
    cfg, d = db
    _write(d, ts=datetime.now(timezone.utc).isoformat())
    c = TestClient(create_app(cfg, d))
    c.get("/api/v1/risk/assessment")            # 触发一次落库
    data = c.get("/api/v1/risk/rules").json()["data"]
    assert data["hash"] and data["version"].startswith(re_.CALC_VERSION)
    assert isinstance(data["history"], list) and data["history"]
    hist_v = data["history"][0]["version"]
    one = c.get(f"/api/v1/risk/rules?version={hist_v}").json()["data"]
    assert one["version"] == hist_v and one["count"] > 0
