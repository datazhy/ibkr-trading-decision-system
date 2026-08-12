"""三页一致性：首页 / 规则页 / 历史页必须读同一个统计函数。

线上真实故障：首页说「本月成交笔数 0」（读 orders 表，恒空），
历史页说 2 笔（读 transactions），规则页说 2 次决策（读 executions）——
同一个「笔数」三个页面三个数。这组测试锁住这件事不能再发生。
"""
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from app import webdata
from app.counting import trading_counts
from app.db import Database
from app.engine import RuleEngine
from app.notify import Notifier

NOW = datetime(2026, 7, 16, 14, 0, tzinfo=timezone.utc)     # ET 10:00 盘中


@pytest.fixture
def seeded(make_config):
    cfg = make_config(ib_account="U1")
    d = Database(cfg.db_path)
    d.init_schema()
    ts = NOW.isoformat()
    # 成交前的持仓快照（早于 13:18 的 BUY）：决策分类要靠它还原 pre_qty
    d.insert_positions_snapshot("2026-07-16T13:00:00+00:00", [{
        "symbol": "DRAM", "qty": Decimal("3.9"), "avg_cost": Decimal("56.60"),
        "market_value": Decimal("220.23"), "upl": Decimal("0"),
        "price": Decimal("56.60"), "price_source": "LAST", "price_quality": "DELAYED",
        "quote_as_of": "2026-07-16T13:00:00+00:00"}], sync_run_id="b0")
    d.insert_balances_snapshot(ts, {
        "equity": Decimal("43500.49"), "cash": Decimal("321.03"),
        "margin_balance": Decimal("321.03"), "buying_power": Decimal("2774.53"),
        "account": "U1"}, sync_run_id="b1")
    d.insert_positions_snapshot(ts, [{
        "symbol": "DRAM", "qty": Decimal("3.9"), "avg_cost": Decimal("56.60"),
        "market_value": Decimal("220.23"), "upl": Decimal("-0.51"),
        "price": Decimal("57.40"), "price_source": "LAST", "price_quality": "DELAYED",
        "quote_as_of": ts, "conid": 870556708}], sync_run_id="b1")
    # DRAM 一买一卖（真实 execId / permId），发生在本月
    d.upsert_executions([
        _exec("0000f9be.6a586681.01.01", "BUY", "55.87", 1357395055,
              "2026-07-16T13:18:36+00:00", "0.558703", None),
        _exec("0002423f.6a5813ac.01.01", "SELL", "55.90", 1357395068,
              "2026-07-16T13:36:19+00:00", "0.56035", "-1.260146"),
    ])
    # transactions：R6 的已实现亏损事件读这张表（券商权威 realized）
    d.upsert_transactions([{
        "txn_id": "0002423f.6a5813ac.01.01", "ts": "2026-07-16T13:36:19+00:00",
        "symbol": "DRAM", "action": "SELL", "qty": Decimal("1"),
        "price": Decimal("55.90"), "fees": Decimal("0.56035"),
        "amount": Decimal("55.90"), "realized": Decimal("-1.260146"), "raw_json": "{}"}])
    return cfg, d


def _exec(exec_id, side, price, perm_id, ts, commission, realized):
    from app.ibkr_client import split_exec_id
    root, seq = split_exec_id(exec_id)
    return {
        "exec_id": exec_id, "correction_root": root, "correction_seq": seq,
        "symbol": "DRAM", "side": side, "shares": Decimal("1"),
        "price": Decimal(price), "perm_id": perm_id, "order_id": 0, "client_id": 0,
        "conid": 870556708, "sec_type": "STK", "exchange": "SMART", "currency": "USD",
        "cumulative_qty": Decimal("1"), "average_price": Decimal(price),
        "execution_time": ts, "commission": Decimal(commission),
        "commission_currency": "USD",
        "realized_pnl": Decimal(realized) if realized else None,
        "liquidation": 0, "order_ref": None, "last_liquidity": 1,
        "data_status": "READY", "raw_json": "{}",
    }


def test_three_pages_agree_on_decision_count(seeded):
    """首页、规则页、历史页对「本月主动交易决策」必须给出同一个数。"""
    cfg, d = seeded
    home = webdata.behavior_counters(cfg, d, NOW)
    hist = webdata.history_summary(cfg, d, webdata.history_rows(cfg, d), now=NOW)
    eng = RuleEngine(cfg, d, Notifier([], d))
    ctx = eng.build_rule_context(NOW)

    assert home["decisions_month"] == 2
    assert hist["decisions"] == 2
    assert ctx.decisions_month_total == 2          # R5 读的也是这个数


def test_three_pages_agree_on_execution_count(seeded):
    cfg, d = seeded
    home = webdata.behavior_counters(cfg, d, NOW)
    hist = webdata.history_summary(cfg, d, webdata.history_rows(cfg, d), now=NOW)
    assert home["executions_month"] == 2 and hist["executions"] == 2


def test_home_and_r4_agree_on_risk_increasing(seeded):
    cfg, d = seeded
    home = webdata.behavior_counters(cfg, d, NOW)
    ctx = RuleEngine(cfg, d, Notifier([], d)).build_rule_context(NOW)
    # BUY 是 OPEN/ADD -> 风险增加 1 次；SELL 是 REDUCE/CLOSE -> 不计
    assert home["risk_increasing_today"] == sum(ctx.risk_increasing_today.values())
    assert home["risk_increasing_today"] == 1


def test_ambiguous_month_fills_label_is_gone(seeded):
    """「本月成交笔数」这个语义不明的标签必须彻底消失。"""
    cfg, d = seeded
    counters = webdata.behavior_counters(cfg, d, NOW)
    assert "month_fills" not in counters
    for key in ("decisions_month", "executions_month", "risk_increasing_today"):
        assert key in counters                      # 每个数都把口径写进名字


def test_all_pages_use_same_counting_function(seeded):
    """三页与规则引擎共用 counting.trading_counts 的结果。"""
    cfg, d = seeded
    tz = cfg.market_tz
    month_start = webdata._et_month_start_utc(NOW, tz)
    counts = trading_counts(d, tz, month_start_iso=month_start, today_et="2026-07-16")
    home = webdata.behavior_counters(cfg, d, NOW)
    assert home["decisions_month"] == counts.decisions_month
    assert home["executions_month"] == counts.executions_month
    assert home["kinds_month"] == counts.kinds_month


# ---------------- R6 空状态 ----------------

def test_r6_never_triggered_shows_never_not_dash(seeded):
    """从未触发时不得显示「— 小时」——那读起来像数据缺失。"""
    cfg, d = seeded
    c = webdata.behavior_counters(cfg, d, NOW)
    assert c["r6_state"] == "NEVER"
    assert c["cooldown_active"] is False
    assert c["hours_since_big_loss"] is None
    # 有过一笔未达阈值的已实现亏损（DRAM -1.26），要与「从未亏过」区分
    assert c["minor_loss_events"] == 1


def test_r6_active_shows_remaining(seeded):
    cfg, d = seeded
    from datetime import timedelta
    d.start_cooldown(NOW.isoformat(), (NOW + timedelta(hours=48)).isoformat(),
                     "大额认亏", "R6:x")
    c = webdata.behavior_counters(cfg, d, NOW + timedelta(hours=6))
    assert c["r6_state"] == "ACTIVE" and c["cooldown_active"] is True
    assert c["cooldown_remaining_hours"] == 42.0


# ---------------- 能力维度 ----------------

def test_capabilities_flag_rules_partial_when_only_frozen_quotes(make_config):
    """盘后只有收盘冻结价：连接与账户都正常，但 R7 没判 —— 规则能力必须是 PARTIAL。"""
    cfg = make_config(ib_account="U1")
    d = Database(cfg.db_path)
    d.init_schema()
    ts = NOW.isoformat()
    d.insert_balances_snapshot(ts, {
        "equity": Decimal("43500"), "cash": Decimal("0"), "margin_balance": Decimal("0"),
        "buying_power": Decimal("0"), "account": "U1"}, sync_run_id="b1")
    d.insert_positions_snapshot(ts, [{
        "symbol": "KMEM", "qty": Decimal("100"), "avg_cost": Decimal("19"),
        "market_value": Decimal("1914"), "upl": Decimal("14"),
        "price": Decimal("19.14"), "price_source": "CLOSE",
        "price_quality": "DELAYED_FROZEN", "quote_as_of": None}], sync_run_id="b1")
    d.upsert_stop_level("2026-07-16", {
        "symbol": "KMEM", "side": "LONG", "close": Decimal("19.14"), "atr": Decimal("1.5"),
        "swing": Decimal("18"), "stop_price": Decimal("17.8"), "risk_amount": Decimal("0"),
        "risk_pct": Decimal("0"), "exceeds_2pct": False, "suggested_trim": 0,
        "reached_1r": False, "trailing_stop": Decimal("18"), "take_profit": "-",
        "atr_period": 9, "bars_used": 10, "data_sufficient": False, "data_note": "n"})
    d.set_sync_state("data_health", "READY")
    RuleEngine(cfg, d, Notifier([], d)).evaluate_and_dispatch(NOW)

    caps = {c["key"]: c for c in webdata.data_status_bar(cfg, d, NOW)["capabilities"]}
    assert list(caps) == ["connection", "account", "quotes", "rules"]
    assert caps["quotes"]["state"] == "PARTIAL"        # 只有收盘价
    assert "不能用于止损判断" in caps["quotes"]["note"]
    assert caps["rules"]["state"] == "PARTIAL"         # R7 未判定
    assert "未判定" in caps["rules"]["note"]
    # 整体不能只报 READY
    assert any(c["state"] == "PARTIAL" for c in caps.values())
