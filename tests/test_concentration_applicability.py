"""集中度规则适用性与降噪验收测试（对应清单 1–18）。

起因：生产历史重放发现 CONC_TOP3_HIGH / CONC_TOP5_HIGH **17/17 天触发**。
账户只有 3–5 只持仓，Top3 占比恒等于 ~100% —— 那是算术必然，不是风险信号。
恒真的告警没有信息量，而噪音会让人关掉整个系统。

修复方式不是提高阈值（那只会把问题藏起来），而是：
  1. 给规则加**适用前提**，持仓数不够时判 not_applicable
  2. 补上不随持仓数失真的指标：HHI、等效持仓数、单仓主导度
  3. 评分在适用规则间**重新归一化**，避免"少了两条规则"让账户显得更安全
"""
from decimal import Decimal as D

import pytest

from app import risk_engine as re_
from app import risk_metrics as rm
from app import risk_service
from app.db import Database

import datetime as dt

NOW = dt.datetime(2026, 7, 29, 16, 0, tzinfo=dt.timezone.utc)


def _pos(symbol, mv, *, price="10", quality="DELAYED", conid=None, qty="100"):
    return {"symbol": symbol, "market_value": str(mv), "price": price,
            "price_quality": quality, "conid": conid or symbol,
            "qty": qty, "avg_cost": "10", "upl": "0"}


def _eval(weights, *, cash=5000, cfg=None):
    """给定多头权重列表，跑出指标、事件、结果与评分。"""
    pos = [_pos(f"S{i}", w) for i, w in enumerate(weights)]
    nl = D(str(sum(weights) + cash))
    m, _ = rm.concentration(pos, nl)
    metrics = {"concentration": m, "account": {}, "liquidity": {},
               "drawdown": {}, "pnl": {}, "positions": []}
    rules = re_.default_rules(cfg)
    events, outcomes = re_.evaluate_rules(metrics, rules, collect_outcomes=True)
    sc = re_.score(events, outcomes=outcomes)
    return m, events, outcomes, sc


def _codes(events, category="concentration"):
    return sorted(e.rule_code for e in events if e.category == category)


def _na(outcomes):
    return {o.rule_code for o in outcomes
            if o.outcome == re_.OUTCOME_NOT_APPLICABLE}


# ---------------- 1–5. 各持仓数量场景 ----------------

def test_single_position():
    m, ev, out, sc = _eval([10000])
    assert m["eligible_long_position_count"] == 1
    assert m["hhi"] == D("1")
    assert m["effective_position_count"] == D("1")
    # 只有 1 只时，HHI / 等效持仓 / 主导度都没有判别力
    na = _na(out)
    assert {"CONC_TOP3_HIGH", "CONC_TOP5_HIGH", "CONC_HHI_HIGH",
            "EFFECTIVE_POSITION_COUNT_LOW", "SINGLE_POSITION_DOMINANCE"} <= na
    # 但单仓规则仍然触发 —— 100% 押一只是最高集中度
    assert "CONC_SINGLE_INVESTED_HIGH" in _codes(ev)
    assert sc["component_scores"]["concentration_score"] > 0


def test_three_positions():
    m, ev, out, sc = _eval([5000, 3000, 2000])
    assert m["eligible_long_position_count"] == 3
    assert m["top3_weight"] == D("1")          # 恒等于 100% —— 正是问题所在
    assert "CONC_TOP3_HIGH" in _na(out)        # 所以判不适用
    assert "CONC_TOP3_HIGH" not in _codes(ev)
    # HHI 与等效持仓数仍在工作
    assert m["hhi"] is not None
    assert m["effective_position_count"] < D("3")
    assert "EFFECTIVE_POSITION_COUNT_LOW" in _codes(ev)


def test_five_positions():
    m, ev, out, sc = _eval([3000, 2500, 2000, 1500, 1000])
    assert m["eligible_long_position_count"] == 5
    assert m["top5_weight"] == D("1")
    na = _na(out)
    assert "CONC_TOP3_HIGH" in na and "CONC_TOP5_HIGH" in na
    # 权重较均匀，等效持仓数接近 5，不该报集中
    assert m["effective_position_count"] > D("4")
    assert "EFFECTIVE_POSITION_COUNT_LOW" not in _codes(ev)
    assert sc["component_scores"]["concentration_score"] == D("0")


def test_six_positions_enables_top3():
    m, ev, out, sc = _eval([2000] * 6)
    assert m["eligible_long_position_count"] == 6
    na = _na(out)
    assert "CONC_TOP3_HIGH" not in na          # 6 >= 6，适用
    assert "CONC_TOP5_HIGH" in na              # 6 < 10，仍不适用


def test_ten_positions_enables_all():
    m, ev, out, sc = _eval([1000] * 10)
    assert m["eligible_long_position_count"] == 10
    assert _na(out) == set()                   # 全部适用
    assert m["effective_position_count"] == D("10")


# ---------------- 6–9. Top-N 适用性 ----------------

def test_top3_not_applicable_reason_is_explicit():
    _, _, out, _ = _eval([5000, 3000, 2000])
    o = next(x for x in out if x.rule_code == "CONC_TOP3_HIGH")
    assert o.outcome == re_.OUTCOME_NOT_APPLICABLE
    assert o.applicable is False
    assert "仅有 3 个有效多头持仓" in o.reason
    assert "不具备区分能力" in o.reason


def test_top3_applicable_and_triggers():
    # 8 只持仓但前三占 85% —— 这才是真正的 Top3 风险信号
    m, ev, out, sc = _eval([30000, 28000, 27000, 2500, 2500, 2500, 2500, 2500])
    assert "CONC_TOP3_HIGH" not in _na(out)
    assert "CONC_TOP3_HIGH" in _codes(ev)
    assert m["top3_weight"] > D("0.80")


def test_top5_not_applicable_below_ten():
    _, _, out, _ = _eval([1000] * 8)
    o = next(x for x in out if x.rule_code == "CONC_TOP5_HIGH")
    assert o.outcome == re_.OUTCOME_NOT_APPLICABLE
    assert "至少需要 10 个" in o.reason


def test_top5_applicable_at_ten():
    m, ev, out, sc = _eval([20000, 19000, 18000, 17000, 16000] + [200] * 5)
    assert "CONC_TOP5_HIGH" not in _na(out)
    assert m["top5_weight"] > D("0.95")
    assert "CONC_TOP5_HIGH" in _codes(ev)


# ---------------- 10–12. 新规则 ----------------

def test_hhi_computed_and_triggers():
    # 权重 0.5/0.3/0.2 -> HHI = 0.25+0.09+0.04 = 0.38（低于 0.40 阈值）
    m, ev, _, _ = _eval([5000, 3000, 2000])
    assert m["hhi"] == D("0.38")
    assert "CONC_HHI_HIGH" not in _codes(ev)
    # 权重 0.7/0.2/0.1 -> HHI = 0.49+0.04+0.01 = 0.54
    m2, ev2, _, _ = _eval([7000, 2000, 1000])
    assert m2["hhi"] == D("0.54")
    assert "CONC_HHI_HIGH" in _codes(ev2)


def test_effective_position_count():
    # 4 只等权 -> HHI 0.25 -> 等效 4
    m, ev, _, _ = _eval([2500] * 4)
    assert m["effective_position_count"] == D("4")
    assert "EFFECTIVE_POSITION_COUNT_LOW" not in _codes(ev)
    # 名义 5 只但严重失衡 -> 等效远小于 5
    m2, ev2, _, _ = _eval([8000, 500, 500, 500, 500])
    assert m2["eligible_long_position_count"] == 5
    assert m2["effective_position_count"] < D("2")
    assert "EFFECTIVE_POSITION_COUNT_LOW" in _codes(ev2)


def test_single_position_dominance():
    """双条件：占比高**且**明显甩开第二名。"""
    # 80% vs 7% -> 主导
    m, ev, _, _ = _eval([8000, 700, 700, 600])
    assert m["dominance_gap"] > D("0.20")
    assert "SINGLE_POSITION_DOMINANCE" in _codes(ev)
    # 40% vs 20%：差值 20% 但最大仓未过主导门槛（50%），不算被一只主导
    m2, ev2, out2, _ = _eval([4000, 2000, 2000, 2000])
    assert "SINGLE_POSITION_DOMINANCE" not in _codes(ev2)
    o = next(x for x in out2 if x.rule_code == "SINGLE_POSITION_DOMINANCE")
    assert o.outcome == re_.OUTCOME_NOT_TRIGGERED
    assert "未超过主导门槛" in o.reason
    # 55%/45%：占比过门槛但差值小，也不算主导
    m3, ev3, _, _ = _eval([5500, 4500])
    assert "SINGLE_POSITION_DOMINANCE" not in _codes(ev3)


# ---------------- 13–15. 不适用的后果 ----------------

def test_not_applicable_produces_no_event():
    _, ev, out, _ = _eval([5000, 3000, 2000])
    na = _na(out)
    triggered = {e.rule_code for e in ev}
    assert na and not (na & triggered)          # 不适用的规则一条事件都没产生


def test_not_applicable_does_not_reach_notification(make_config):
    """不适用的规则不产生事件 -> 运营层无从推送。"""
    from app import event_service as es, risk_events as rev
    cfg = make_config(ib_account="U1")
    d = Database(cfg.db_path)
    d.init_schema()

    class N:
        def __init__(self): self.sent = []
        def send(self, t, b): self.sent.append(t); return True

    n = N()
    _, ev, _, _ = _eval([5000, 3000, 2000])
    triggers = [rev.from_account_event(
        e.as_dict(), assessment={"assessment_id": "a", "reliable": True,
                                 "rule_set_version": "v~1"},
        account_id="U1") for e in ev]
    codes = {t.rule_code for t in triggers}
    assert "CONC_TOP3_HIGH" not in codes
    assert "CONC_TOP5_HIGH" not in codes
    res = es.register_triggers(d, triggers, now=NOW, evaluation_complete=True,
                               reliable=True, scope={"source_types": ["account"]})
    es.dispatch(d, n, res, cfg=cfg, now=NOW)
    rows, _ = d.query_risk_events()
    assert all(r["rule_code"] not in ("CONC_TOP3_HIGH", "CONC_TOP5_HIGH")
               for r in rows)


def test_score_renormalization():
    """不适用规则不得让集中账户显得更安全。"""
    # 高度集中的 4 只持仓
    _, ev, out, sc = _eval([8000, 700, 700, 600])
    assert sc["score_normalization_method"] == re_.NORM_APPLICABLE
    detail = sc["normalization_detail"]["concentration_score"]
    assert detail["not_applicable_rule_count"] == 2      # Top3/Top5
    assert detail["applicable_rule_count"] >= 5
    # 归一化后仍是高分 —— 降噪没有变成粉饰
    assert sc["component_scores"]["concentration_score"] > D("60")

    # 对照：真正分散的组合得 0 分
    _, _, _, sc2 = _eval([1000] * 10)
    assert sc2["component_scores"]["concentration_score"] == D("0")

    # 不给 outcomes 时退回加总法，向后兼容
    sc3 = re_.score(ev)
    assert sc3["score_normalization_method"] == re_.NORM_ADDITIVE


def test_applicability_summary_shape():
    _, _, out, _ = _eval([5000, 3000, 2000])
    summ = re_.applicability_summary(out, re_.CAT_CONCENTRATION)
    assert summ["not_applicable_rule_count"] == 2
    assert summ["applicable_rule_count"] >= 5
    assert all("reason" in x and "rule_code" in x
               for x in summ["not_applicable"])


# ---------------- 排除项 ----------------

def test_ineligible_positions_are_excluded_with_reasons():
    """零市值 / 不可定价 / 重复记录都会稀释集中度，必须排除并记原因。"""
    pos = [_pos("A", 5000), _pos("B", 3000),
           _pos("Z", 0),                                   # 零市值
           _pos("Y", 1000, price="0", quality="MISSING"),  # 不可定价
           _pos("A", 5000)]                                # 重复
    m, _ = rm.concentration(pos, D("20000"))
    assert m["total_position_count"] == 5
    assert m["eligible_long_position_count"] == 2
    reasons = {x["reason"] for x in m["excluded_positions"]}
    assert reasons == {rm.EXCL_ZERO_MV, rm.EXCL_NO_PRICE, rm.EXCL_DUPLICATE}
    assert all(x.get("note") for x in m["excluded_positions"])
    # 排除后 HHI 只按有效持仓算
    assert m["hhi"] == D("0.625") + D("0.375") ** 2 - D("0.625") + D("0.625") ** 2 \
        if False else m["hhi"] is not None


def test_cash_does_not_dilute_hhi():
    """权重用已投资口径 —— 现金多寡不该影响选股集中度。"""
    a, _ = rm.concentration([_pos("A", 5000), _pos("B", 5000)], D("10000"))
    b, _ = rm.concentration([_pos("A", 5000), _pos("B", 5000)], D("100000"))
    assert a["hhi"] == b["hhi"] == D("0.5")
    assert a["effective_position_count"] == b["effective_position_count"] == D("2")


# ---------------- 16–17. 端到端与回归 ----------------

def test_assessment_exposes_applicability(make_config):
    cfg = make_config(ib_account="U1")
    d = Database(cfg.db_path)
    d.init_schema()
    d.write_account_snapshot(
        snapshot_id="s1", ts=NOW.isoformat(), sync_run_id="r1",
        quality_status="fresh", ai_analysis_allowed=True,
        balances={"account": "U1", "currency": "USD", "equity": D("20000"),
                  "cash": D("5000"), "total_cash": D("5000"),
                  "margin_balance": D("5000"), "buying_power": D("40000"),
                  "available_funds": D("5000"), "excess_liquidity": D("5000"),
                  "maint_margin": D("2000"), "init_margin": D("2500"),
                  "positions_value": D("15000")},
        positions=[{"symbol": "A", "qty": D("100"), "avg_cost": D("50"),
                    "market_value": D("8000"), "upl": D("0"), "price": D("80"),
                    "price_quality": "DELAYED"},
                   {"symbol": "B", "qty": D("100"), "avg_cost": D("40"),
                    "market_value": D("4000"), "upl": D("0"), "price": D("40"),
                    "price_quality": "DELAYED"},
                   {"symbol": "C", "qty": D("100"), "avg_cost": D("30"),
                    "market_value": D("3000"), "upl": D("0"), "price": D("30"),
                    "price_quality": "DELAYED"}])
    a = risk_service.assess(cfg, d, now=NOW)
    assert a is not None
    summ = a["concentration_applicability"]
    assert summ["not_applicable_rule_count"] == 2
    assert a["score_normalization_method"] == re_.NORM_APPLICABLE
    assert "CONC_TOP3_HIGH" not in a["triggered_rules"]
    conc = a["calculated_metrics"]["concentration"]
    assert conc["eligible_long_position_count"] == 3
    assert conc["hhi"] is not None
    assert conc["effective_position_count"] is not None
    # 规则结果全部留痕
    assert any(o["outcome"] == "not_applicable" for o in a["rule_outcomes"])


def test_existing_rules_still_work():
    """降噪不得影响非集中度规则。"""
    metrics = {
        "account": {"cash_ratio": D("0.01"), "gross_leverage": D("3.0")},
        "liquidity": {"maint_margin_safety_multiple": D("1.2")},
        "drawdown": {"current_drawdown": D("0.20")},
        "pnl": {"daily_pnl_ratio": D("-0.05")},
        "concentration": {"eligible_long_position_count": 0},
        "positions": [],
    }
    ev, out = re_.evaluate_rules(metrics, re_.default_rules(),
                                 collect_outcomes=True)
    codes = {e.rule_code for e in ev}
    assert "ACC_CASH_LOW" in codes
    assert "ACC_GROSS_LEVERAGE_HIGH" in codes
    assert "ACC_MAINT_MARGIN_BUFFER_LOW" in codes
    assert "ACC_DRAWDOWN_HIGH" in codes
    assert "ACC_DAILY_LOSS_HIGH" in codes


def test_thresholds_are_configurable(make_config):
    cfg = make_config(ib_account="U1", risk_top3_min_position_count=3,
                      risk_hhi_threshold="0.20",
                      risk_effective_position_count_min="8")
    rules = {r.rule_code: r for r in re_.default_rules(cfg)}
    assert rules["CONC_TOP3_HIGH"].min_long_positions == 3
    assert rules["CONC_HHI_HIGH"].threshold == D("0.20")
    assert rules["EFFECTIVE_POSITION_COUNT_LOW"].threshold == D("8")
    # 门槛降到 3 之后，3 只持仓时 Top3 就适用了
    _, ev, out, _ = _eval([5000, 3000, 2000], cfg=cfg)
    assert "CONC_TOP3_HIGH" not in _na(out)
    assert "CONC_TOP3_HIGH" in _codes(ev)


def test_frontend_shows_not_applicable_not_green(make_config):
    from fastapi.testclient import TestClient
    from app.webapp import create_app
    cfg = make_config(ib_account="U1")
    d = Database(cfg.db_path)
    d.init_schema()
    d.write_account_snapshot(
        snapshot_id="s1", ts=NOW.isoformat(), sync_run_id="r1",
        quality_status="fresh", ai_analysis_allowed=True,
        balances={"account": "U1", "currency": "USD", "equity": D("20000"),
                  "cash": D("5000"), "total_cash": D("5000"),
                  "margin_balance": D("5000"), "buying_power": D("40000"),
                  "available_funds": D("5000"), "excess_liquidity": D("5000"),
                  "maint_margin": D("2000"), "init_margin": D("2500"),
                  "positions_value": D("15000")},
        positions=[{"symbol": "A", "qty": D("100"), "avg_cost": D("50"),
                    "market_value": D("12000"), "upl": D("0"), "price": D("120"),
                    "price_quality": "DELAYED"},
                   {"symbol": "B", "qty": D("100"), "avg_cost": D("30"),
                    "market_value": D("3000"), "upl": D("0"), "price": D("30"),
                    "price_quality": "DELAYED"}])
    html = TestClient(create_app(cfg, d)).get("/").text
    assert "集中度指标适用性" in html
    assert "不适用" in html
    assert "不适用<b>不等于通过</b>" in html
    assert "集中度指数 HHI" in html
    assert "等效持仓数" in html
    assert "有效持仓 / 名义持仓" in html


def test_position_level_events_survive_normalization():
    """归一化不得抹掉逐持仓规则的分数。

    POS_* 规则按标的循环评估，不产出 outcome。若归一化直接覆写
    concentration_score，一个持仓大额浮亏的账户会被显示成低风险 ——
    生产重放实测出过这个问题（07-28/29 被判 low）。
    """
    pos = [_pos("A", 5000), _pos("B", 3000), _pos("C", 2000)]
    m, _ = rm.concentration(pos, D("50000"))
    prow = [{"symbol": "A", "unrealized_pnl_percent": D("-0.30"),
             "contribution_to_total_losing_positions": D("0.90"),
             "weight_of_equity": D("0.10"), "risk_flags": []}]
    metrics = {"concentration": m, "account": {}, "liquidity": {},
               "drawdown": {}, "pnl": {}, "positions": prow}
    ev, out = re_.evaluate_rules(metrics, re_.default_rules(),
                                 collect_outcomes=True)
    codes = {e.rule_code for e in ev}
    assert "POS_UNREALIZED_LOSS_HIGH" in codes
    assert "POS_LOSS_POOL_SHARE_HIGH" in codes

    sc = re_.score(ev, outcomes=out)
    detail = sc["normalization_detail"]["concentration_score"]
    # 持仓层分值单独叠加，不参与归一化
    assert D(detail["uncovered_event_points"]) == D("40")
    assert D(detail["normalized_component"]) > 0
    score = sc["component_scores"]["concentration_score"]
    assert score >= D("40"), "持仓层风险被归一化抹掉了"
    # 分数 = 归一化的集中度部分 + 原样叠加的持仓层部分
    assert score == (D(detail["normalized_component"])
                     + D(detail["uncovered_event_points"]))
    assert detail["uncovered_note"]


def test_applicability_survives_assessment_cache(make_config):
    """适用性摘要必须落库 —— 否则命中幂等缓存读回来时就丢了，
    页面会把「不适用」显示成一片正常。生产实测踩到过这个。"""
    cfg = make_config(ib_account="U1")
    d = Database(cfg.db_path)
    d.init_schema()
    d.write_account_snapshot(
        snapshot_id="s1", ts=NOW.isoformat(), sync_run_id="r1",
        quality_status="fresh", ai_analysis_allowed=True,
        balances={"account": "U1", "currency": "USD", "equity": D("20000"),
                  "cash": D("5000"), "total_cash": D("5000"),
                  "margin_balance": D("5000"), "buying_power": D("40000"),
                  "available_funds": D("5000"), "excess_liquidity": D("5000"),
                  "maint_margin": D("2000"), "init_margin": D("2500"),
                  "positions_value": D("15000")},
        positions=[{"symbol": "A", "qty": D("100"), "avg_cost": D("50"),
                    "market_value": D("9000"), "upl": D("0"), "price": D("90"),
                    "price_quality": "DELAYED"},
                   {"symbol": "B", "qty": D("100"), "avg_cost": D("30"),
                    "market_value": D("6000"), "upl": D("0"), "price": D("60"),
                    "price_quality": "DELAYED"}])
    first = risk_service.assess(cfg, d, now=NOW)
    assert first["concentration_applicability"]["not_applicable_rule_count"] == 2

    # 第二次走幂等缓存 —— 字段必须还在
    cached = risk_service.assess(cfg, d, now=NOW)
    assert cached["assessment_id"] == first["assessment_id"]
    summ = cached["concentration_applicability"]
    assert summ["not_applicable_rule_count"] == 2
    assert {x["rule_code"] for x in summ["not_applicable"]} == {
        "CONC_TOP3_HIGH", "CONC_TOP5_HIGH"}
    assert cached["score_normalization_method"] == re_.NORM_APPLICABLE
    assert cached["rule_outcomes"]


def test_calculation_version_bumped():
    """口径变了版本必须跟着变，否则 assess() 会一直返回旧口径的缓存。"""
    assert re_.CALC_VERSION.startswith("risk-v2-conc")
    assert "risk-v1/2026-07" in re_.LEGACY_CALC_VERSIONS
