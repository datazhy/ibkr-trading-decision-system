"""R1–R7 历史回放测试（Phase 2 验收核心）。每条规则至少一个触发 + 一个不触发用例。"""
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from app.rules import RuleContext, evaluate_rules

NOW = datetime(2026, 7, 10, 14, 0, tzinfo=timezone.utc)


def ctx(**kw) -> RuleContext:
    base = dict(now=NOW, equity=Decimal("100000"), margin_balance=Decimal("0"), positions=[])
    base.update(kw)
    return RuleContext(**base)


def rule_ids(hits):
    return [h.rule_id for h in hits]


# ---------------- R1 单一标的 > 30% ----------------

def test_r1_triggers():
    c = ctx(positions=[{"symbol": "MU", "qty": 100, "avg_cost": 300, "market_value": 35000, "upl": 0}])
    hits = [h for h in evaluate_rules(c) if h.rule_id == "R1"]
    assert len(hits) == 1 and hits[0].symbol == "MU"


def test_r1_not_triggered_at_30pct_boundary():
    c = ctx(positions=[{"symbol": "MU", "qty": 100, "avg_cost": 300, "market_value": 30000, "upl": 0}])
    assert "R1" not in rule_ids(evaluate_rules(c))  # 等于 30% 不触发（严格大于）


# ---------------- R2 同板块 > 40% ----------------

def test_r2_triggers():
    c = ctx(
        positions=[
            {"symbol": "MU", "qty": 1, "avg_cost": 1, "market_value": 25000, "upl": 0},
            {"symbol": "MRVL", "qty": 1, "avg_cost": 1, "market_value": 20000, "upl": 0},
        ],
        sector_map={"MU": "内存半导体", "MRVL": "内存半导体"},
    )
    hits = [h for h in evaluate_rules(c) if h.rule_id == "R2"]
    assert len(hits) == 1
    assert "内存半导体" in hits[0].title


def test_r2_not_triggered_without_sector_map():
    c = ctx(positions=[
        {"symbol": "MU", "qty": 1, "avg_cost": 1, "market_value": 25000, "upl": 0},
        {"symbol": "MRVL", "qty": 1, "avg_cost": 1, "market_value": 20000, "upl": 0},
    ])
    assert "R2" not in rule_ids(evaluate_rules(c))


# ---------------- R3 margin < 0 ----------------

def test_r3_triggers():
    c = ctx(margin_balance=Decimal("-500"))
    hits = [h for h in evaluate_rules(c) if h.rule_id == "R3"]
    assert len(hits) == 1 and hits[0].severity == "CRIT"


def test_r3_not_triggered_when_zero():
    assert "R3" not in rule_ids(evaluate_rules(ctx(margin_balance=Decimal("0"))))


# ---------------- R4 当日同标的 ≥ 2 笔 ----------------

def test_r4_triggers():
    c = ctx(filled_today={"MU": 2, "AAPL": 1})
    hits = [h for h in evaluate_rules(c) if h.rule_id == "R4"]
    assert len(hits) == 1 and hits[0].symbol == "MU"


def test_r4_not_triggered_below_threshold():
    assert "R4" not in rule_ids(evaluate_rules(ctx(filled_today={"MU": 1})))


# ---------------- R5 本月 > 10 笔 ----------------

def test_r5_triggers():
    hits = [h for h in evaluate_rules(ctx(filled_month_total=11)) if h.rule_id == "R5"]
    assert len(hits) == 1


def test_r5_not_triggered_at_10():
    assert "R5" not in rule_ids(evaluate_rules(ctx(filled_month_total=10)))


# ---------------- R6 单笔已实现亏损 > 2% + 冷静期 ----------------

def test_r6_triggers_and_flags_cooldown():
    c = ctx(realized_events=[{"symbol": "MU", "realized": Decimal("-2500"), "ref_id": "o1"}])
    hits = [h for h in evaluate_rules(c) if h.rule_id == "R6"]
    assert len(hits) == 1
    assert hits[0].triggers_cooldown_hours == 48


def test_r6_small_loss_not_triggered():
    c = ctx(realized_events=[{"symbol": "MU", "realized": Decimal("-1500"), "ref_id": "o1"}])
    assert "R6" not in rule_ids(evaluate_rules(c))


def test_r6_trade_during_cooldown_appends_alert():
    c = ctx(
        cooldown_active=True,
        cooldown_ends_at=NOW + timedelta(hours=40),
        new_fills_in_cooldown=[{"symbol": "AAPL", "ref_id": "o9"}],
    )
    hits = [h for h in evaluate_rules(c) if "冷静期内交易" in h.title]
    assert len(hits) == 1 and hits[0].symbol == "AAPL"


# ---------------- R7 触及止损位 ----------------

def test_r7_long_breach_triggers():
    c = ctx(
        positions=[{"symbol": "MU", "qty": 100, "avg_cost": 100, "market_value": 9000, "upl": 0}],
        stop_levels={"MU": {"side": "LONG", "stop_price": Decimal("92")}},
        current_prices={"MU": Decimal("90")},
    )
    hits = [h for h in evaluate_rules(c) if h.rule_id == "R7"]
    assert len(hits) == 1 and hits[0].symbol == "MU"


def test_r7_long_above_stop_ok():
    c = ctx(
        positions=[{"symbol": "MU", "qty": 100, "avg_cost": 100, "market_value": 9500, "upl": 0}],
        stop_levels={"MU": {"side": "LONG", "stop_price": Decimal("92")}},
        current_prices={"MU": Decimal("95")},
    )
    assert "R7" not in rule_ids(evaluate_rules(c))


def test_r7_short_breach_triggers():
    c = ctx(
        positions=[{"symbol": "S", "qty": -100, "avg_cost": 50, "market_value": -6000, "upl": 0}],
        stop_levels={"S": {"side": "SHORT", "stop_price": Decimal("58")}},
        current_prices={"S": Decimal("60")},
    )
    hits = [h for h in evaluate_rules(c) if h.rule_id == "R7"]
    assert len(hits) == 1


# ---------------- 组合：多规则同时触发 ----------------

def test_multiple_rules_fire_together():
    c = ctx(
        equity=Decimal("100000"),
        margin_balance=Decimal("-100"),
        positions=[{"symbol": "MU", "qty": 100, "avg_cost": 300, "market_value": 40000, "upl": 0}],
        sector_map={"MU": "内存半导体"},
        filled_today={"MU": 3},
        filled_month_total=12,
    )
    ids = set(rule_ids(evaluate_rules(c)))
    assert {"R1", "R3", "R4", "R5"} <= ids


def test_no_equity_skips_pct_rules():
    # equity=0 时百分比类规则（R1/R2/R6）不应误报
    c = ctx(equity=Decimal("0"),
            positions=[{"symbol": "MU", "qty": 1, "avg_cost": 1, "market_value": 999999, "upl": 0}])
    assert "R1" not in rule_ids(evaluate_rules(c))
