"""R7 行情质量分级 + evaluate_ruleset 状态（纯函数）。"""
from datetime import datetime, timezone
from decimal import Decimal

from app.rules import (
    RuleContext, RuleStatus, RunStatus, evaluate_ruleset, evaluate_rules,
)

NOW = datetime(2026, 7, 10, 18, 0, tzinfo=timezone.utc)


def _ctx(**kw):
    base = dict(now=NOW, equity=Decimal("100000"), margin_balance=Decimal("0"), positions=[])
    base.update(kw)
    return RuleContext(**base)


def _long_breach(quality):
    """多头 MU 现价 90 跌破止损 92，行情质量=quality。"""
    return _ctx(
        positions=[{"symbol": "MU", "qty": Decimal("100"), "avg_cost": Decimal("100"),
                    "market_value": Decimal("9000"), "upl": Decimal("-1000")}],
        stop_levels={"MU": {"side": "LONG", "stop_price": Decimal("92")}},
        current_prices={"MU": Decimal("90")},
        price_quality={"MU": quality},
    )


def _r7(res):
    return res.by_rule()["R7"]


def test_r7_realtime_breach_is_crit():
    res = evaluate_ruleset(_long_breach("REALTIME"))
    ev = _r7(res)
    assert ev.status == RuleStatus.TRIGGERED
    assert ev.hits[0].severity == "CRIT"


def test_r7_realtime_nonconsolidated_is_crit_with_note():
    ev = _r7(evaluate_ruleset(_long_breach("REALTIME_NONCONSOLIDATED")))
    assert ev.status == RuleStatus.TRIGGERED
    assert ev.hits[0].severity == "CRIT"
    assert "非综合" in ev.hits[0].title


def test_r7_delayed_notes_15min():
    ev = _r7(evaluate_ruleset(_long_breach("DELAYED")))
    assert ev.hits[0].severity == "WARN"
    assert "15" in ev.hits[0].message


def test_r7_delayed_breach_is_warn_only():
    res = evaluate_ruleset(_long_breach("DELAYED"))
    ev = _r7(res)
    assert ev.status == RuleStatus.TRIGGERED
    assert ev.hits[0].severity == "WARN"
    assert "延迟" in ev.hits[0].title


def test_r7_frozen_breach_is_not_evaluated():
    """冻结价：不判定，而不是「判定为未触及」。"""
    ev = _r7(evaluate_ruleset(_long_breach("FROZEN")))
    assert ev.status == RuleStatus.NOT_EVALUATED
    assert ev.hits == []


def test_r7_delayed_frozen_breach_is_not_evaluated():
    ev = _r7(evaluate_ruleset(_long_breach("DELAYED_FROZEN")))
    assert ev.status == RuleStatus.NOT_EVALUATED
    assert ev.hits == []
    assert "不执行止损穿越判断" in ev.detail


def test_r7_frozen_not_breached_is_also_not_evaluated():
    """要害用例：价格远在止损线之外，也不得报「正常未触及」。

    这是线上真实状态 —— 4 个持仓全是收盘冻结价，R7 却报
    「检查 4 个持仓，未触及」= OK。拿昨天的收盘价宣布今天没跌破止损，
    与误报同样是谎报。
    """
    c = _long_breach("DELAYED_FROZEN")
    c.current_prices["MU"] = Decimal("150")        # 远高于止损 92
    ev = _r7(evaluate_ruleset(c))
    assert ev.status == RuleStatus.NOT_EVALUATED   # 绝不能是 OK
    assert ev.hits == []
    assert "未触及" not in ev.detail
    assert "仅有收盘冻结价" in ev.detail


def test_r7_mixed_quality_evaluates_only_live_ones():
    """部分实时部分冻结：实时的照常判定，冻结的计入未判定。"""
    c = _ctx(
        positions=[
            {"symbol": "MU", "qty": Decimal("100"), "avg_cost": Decimal("100"),
             "market_value": Decimal("9000"), "upl": Decimal("0")},
            {"symbol": "KMEM", "qty": Decimal("100"), "avg_cost": Decimal("19"),
             "market_value": Decimal("1900"), "upl": Decimal("0")},
        ],
        stop_levels={"MU": {"side": "LONG", "stop_price": Decimal("92")},
                     "KMEM": {"side": "LONG", "stop_price": Decimal("18")}},
        current_prices={"MU": Decimal("95"), "KMEM": Decimal("19")},
        price_quality={"MU": "DELAYED", "KMEM": "DELAYED_FROZEN"},
    )
    ev = _r7(evaluate_ruleset(c))
    # 判了一部分、剩下的没判 —— 既不是「全部正常」也不是「全未判定」
    assert ev.status == RuleStatus.PARTIALLY_EVALUATED
    assert "已判定 1 个持仓" in ev.detail
    assert "另有 1 个未判定" in ev.detail          # KMEM 不被掩盖
    assert ev.counts == {"evaluated": 1, "not_evaluated": 1, "triggered": 0,
                         "with_stop_level": 2}
    by_sym = {x["symbol"]: x for x in ev.per_symbol}
    assert by_sym["MU"]["status"] == "OK"
    assert by_sym["KMEM"]["status"] == "NOT_EVALUATED"
    assert "收盘冻结价" in by_sym["KMEM"]["reason"]


def test_r7_no_stop_levels_is_ok_not_unevaluated():
    """没有止损位 ≠ 行情质量不足：本规则对它无事可判，仍算正常。"""
    c = _ctx(positions=[{"symbol": "MU", "qty": Decimal("1"), "avg_cost": Decimal("1"),
                         "market_value": Decimal("1"), "upl": Decimal("0")}])
    ev = _r7(evaluate_ruleset(c))
    assert ev.status == RuleStatus.OK
    assert "无建议止损位可判" in ev.detail


def test_r7_stale_breach_does_not_trigger():
    res = evaluate_ruleset(_long_breach("STALE"))
    assert _r7(res).status == RuleStatus.STALE_DATA
    assert _r7(res).hits == []


def test_r7_missing_price_insufficient():
    res = evaluate_ruleset(_long_breach("MISSING"))
    ev = _r7(res)
    assert ev.status == RuleStatus.INSUFFICIENT_DATA
    assert ev.hits == []


def test_r7_zero_price_not_triggered():
    c = _long_breach("REALTIME")
    c.current_prices["MU"] = Decimal("0")   # 无效价：绝不能因 0<=stop 误触发
    ev = _r7(evaluate_ruleset(c))
    assert ev.status == RuleStatus.INSUFFICIENT_DATA
    assert ev.hits == []


def test_r7_default_quality_is_realtime_backcompat():
    """不提供 price_quality 时按实时（保旧行为，兼容 evaluate_rules 老测试）。"""
    c = _ctx(
        positions=[{"symbol": "MU", "qty": Decimal("100"), "avg_cost": Decimal("100"),
                    "market_value": Decimal("9000"), "upl": Decimal("0")}],
        stop_levels={"MU": {"side": "LONG", "stop_price": Decimal("92")}},
        current_prices={"MU": Decimal("90")},
    )
    hits = [h for h in evaluate_rules(c) if h.rule_id == "R7"]
    assert len(hits) == 1 and hits[0].severity == "CRIT"


# ---------------- evaluate_ruleset 状态 ----------------

def test_r1_insufficient_when_no_equity():
    res = evaluate_ruleset(_ctx(equity=Decimal("0"),
        positions=[{"symbol": "X", "qty": 1, "avg_cost": 1, "market_value": 999999, "upl": 0}]))
    assert res.by_rule()["R1"].status == RuleStatus.INSUFFICIENT_DATA


def test_r2_skipped_without_sector_map():
    assert evaluate_ruleset(_ctx()).by_rule()["R2"].status == RuleStatus.SKIPPED


def test_r3_insufficient_when_margin_none():
    assert evaluate_ruleset(_ctx(margin_balance=None)).by_rule()["R3"].status == RuleStatus.INSUFFICIENT_DATA


def test_run_status_ok_when_clean():
    assert evaluate_ruleset(_ctx()).run_status in (RunStatus.OK, RunStatus.INSUFFICIENT_DATA)


def test_run_status_error_on_bad_data():
    # realized 非数值 → R6 内部异常 → ERROR 状态（不影响其它规则）
    res = evaluate_ruleset(_ctx(realized_events=[{"symbol": "X", "realized": "not-a-number", "ref_id": "1"}]))
    assert res.by_rule()["R6"].status == RuleStatus.ERROR
    assert res.run_status == RunStatus.ERROR
    # 其它规则仍正常评估
    assert res.by_rule()["R1"].status in (RuleStatus.OK, RuleStatus.INSUFFICIENT_DATA)


# ---------------- 止损位数据不足（新上市标的）→ R7 降级 ----------------
# 口径：日线不足算出的止损位仅供参考，即便行情实时也只发 WARN，不发 CRIT。

def _weak_stop_breach(quality="REALTIME"):
    """KMEM 现价 18 跌破止损 18.5，但止损位基于 10 根日线（ATR9）。"""
    return _ctx(
        positions=[{"symbol": "KMEM", "qty": Decimal("1786"), "avg_cost": Decimal("18.955"),
                    "market_value": Decimal("32148"), "upl": Decimal("-1706")}],
        stop_levels={"KMEM": {"side": "LONG", "stop_price": Decimal("18.5"),
                              "data_sufficient": False, "atr_period": 9}},
        current_prices={"KMEM": Decimal("18")},
        price_quality={"KMEM": quality},
    )


def test_r7_weak_stop_downgrades_realtime_crit_to_warn():
    ev = _r7(evaluate_ruleset(_weak_stop_breach("REALTIME")))
    assert ev.status is RuleStatus.TRIGGERED
    hit = ev.hits[0]
    assert hit.severity == "WARN"          # 实时行情也不给 CRIT
    assert "仅供参考" in hit.title
    assert "ATR9" in hit.message and "人工确认" in hit.message


def test_r7_weak_stop_stays_warn_when_delayed():
    ev = _r7(evaluate_ruleset(_weak_stop_breach("DELAYED")))
    assert ev.hits[0].severity == "WARN"


def test_r7_weak_stop_still_respects_stale_gate():
    """行情过期时，数据不足的止损位同样不得触发（质量门禁优先）。"""
    ev = _r7(evaluate_ruleset(_weak_stop_breach("STALE")))
    assert ev.status is RuleStatus.STALE_DATA
    assert ev.hits == []


def test_r7_weak_stop_no_breach_is_ok():
    c = _weak_stop_breach("REALTIME")
    c.current_prices["KMEM"] = Decimal("19")     # 未跌破
    ev = _r7(evaluate_ruleset(c))
    assert ev.status is RuleStatus.OK and ev.hits == []


def test_r7_sufficient_stop_still_crit():
    """回归：数据充足的止损位行为不变，仍发 CRIT。"""
    ev = _r7(evaluate_ruleset(_long_breach("REALTIME")))
    assert ev.hits[0].severity == "CRIT"


# ---------------- 规则摘要与逐标的明细（P1-6） ----------------

def test_run_summary_counts_by_category():
    res = evaluate_ruleset(_long_breach("DELAYED_FROZEN"))
    s = res.summary
    assert set(s) == {"triggered", "ok", "not_evaluated", "error", "skipped"}
    assert s["not_evaluated"] >= 1          # R7 未判定
    assert sum(s.values()) == 7             # R1–R7 每条都有归属


def test_r7_per_symbol_records_each_symbol():
    ev = _r7(evaluate_ruleset(_long_breach("DELAYED_FROZEN")))
    assert len(ev.per_symbol) == 1
    assert ev.per_symbol[0]["symbol"] == "MU"
    assert ev.per_symbol[0]["status"] == "NOT_EVALUATED"
    assert ev.counts["evaluated"] == 0 and ev.counts["not_evaluated"] == 1


def test_r7_counts_triggered():
    ev = _r7(evaluate_ruleset(_long_breach("REALTIME")))
    assert ev.counts == {"evaluated": 1, "not_evaluated": 0, "triggered": 1,
                         "with_stop_level": 1}
    assert ev.per_symbol[0]["status"] == "TRIGGERED"


def test_r7_no_stop_level_symbol_is_recorded():
    c = _ctx(positions=[{"symbol": "MU", "qty": Decimal("1"), "avg_cost": Decimal("1"),
                         "market_value": Decimal("1"), "upl": Decimal("0")}])
    ev = _r7(evaluate_ruleset(c))
    assert ev.per_symbol[0]["status"] == "NO_STOP_LEVEL"   # 不被静默丢弃


# ---------------- R7 滞回带（P0-1） ----------------
# 线上实测：DRAM 现价贴着止损线 53.37 震荡，每次下穿「触发」、每次回升「解除」，
# 一支 10 条推送。滞回带让价格必须真正脱离才算解除。

def _breach_ctx(price, *, active=(), atr="1.0", stop="92"):
    return _ctx(
        positions=[{"symbol": "MU", "qty": Decimal("100"), "avg_cost": Decimal("100"),
                    "market_value": Decimal("9000"), "upl": Decimal("0")}],
        stop_levels={"MU": {"side": "LONG", "stop_price": Decimal(stop),
                            "atr": Decimal(atr)}},
        current_prices={"MU": Decimal(price)},
        price_quality={"MU": "DELAYED"},
        r7_active=set(active),
    )


def test_r7_hysteresis_holds_trigger_inside_band():
    """已触发后价格微幅回升到止损线上方，仍在滞回带内 -> 维持触发，不发「解除」。"""
    # 止损 92，ATR 1.0，滞回 0.3 -> 需站上 92.3 才算解除
    ev = _r7(evaluate_ruleset(_breach_ctx("92.1", active=["MU"])))
    assert ev.status == RuleStatus.TRIGGERED
    assert "滞回带内" in ev.per_symbol[0]["reason"]


def test_r7_hysteresis_releases_outside_band():
    ev = _r7(evaluate_ruleset(_breach_ctx("92.5", active=["MU"])))
    assert ev.status == RuleStatus.OK          # 站上 92.3 -> 真正解除


def test_r7_hysteresis_only_applies_when_already_active():
    """未触发时不受滞回影响：92.1 > 92，就是没触及。"""
    ev = _r7(evaluate_ruleset(_breach_ctx("92.1", active=[])))
    assert ev.status == RuleStatus.OK


def test_r7_still_triggers_on_real_breach():
    ev = _r7(evaluate_ruleset(_breach_ctx("91.0", active=[])))
    assert ev.status == RuleStatus.TRIGGERED


def test_r7_hysteresis_short_side():
    """空头：止损在上方，滞回带向下。"""
    c = _ctx(
        positions=[{"symbol": "MU", "qty": Decimal("-100"), "avg_cost": Decimal("100"),
                    "market_value": Decimal("-9000"), "upl": Decimal("0")}],
        stop_levels={"MU": {"side": "SHORT", "stop_price": Decimal("92"),
                            "atr": Decimal("1.0")}},
        current_prices={"MU": Decimal("91.9")},     # 已回落到止损线下方
        price_quality={"MU": "DELAYED"},
        r7_active={"MU"},
    )
    ev = _r7(evaluate_ruleset(c))
    assert ev.status == RuleStatus.TRIGGERED        # 需跌破 91.7 才解除


def test_r7_without_atr_falls_back_to_plain_compare():
    """止损位没有 ATR（老数据）时不做滞回，退回原比较 —— 不因缺字段崩溃。"""
    c = _breach_ctx("92.1", active=["MU"])
    c.stop_levels["MU"].pop("atr")
    assert _r7(evaluate_ruleset(c)).status == RuleStatus.OK
