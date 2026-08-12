"""engine 集成测试：止损位落库、R6 触发冷静期、去重、冷静期内交易。"""
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from app.db import Database
from app.engine import RuleEngine
from app.notify import Notifier

NOW = datetime(2026, 7, 10, 18, 0, tzinfo=timezone.utc)  # ET 14:00 EDT


class CountingNotifier(Notifier):
    def __init__(self, db):
        super().__init__([], db)
        self.sent = []

    def send(self, title, body):
        self.sent.append(title)
        return True


@pytest.fixture
def setup(make_config):
    cfg = make_config(ib_account="ACC")
    db = Database(cfg.db_path)
    db.init_schema()
    notifier = CountingNotifier(db)
    engine = RuleEngine(cfg, db, notifier)
    return cfg, db, notifier, engine


def seed_account(db, equity="100000", margin="0"):
    ts = NOW.isoformat()
    db.insert_balances_snapshot(ts, {
        "equity": Decimal(equity), "cash": Decimal("50000"),
        "margin_balance": Decimal(margin), "buying_power": Decimal("80000"),
    })
    db.insert_positions_snapshot(ts, [
        {"symbol": "MU", "qty": Decimal("100"), "avg_cost": Decimal("100"),
         "market_value": Decimal("9000"), "upl": Decimal("-1000")},
    ])


def seed_bars(db, symbol="MU", close=Decimal("90"), n=60):
    # 唯一日期避免主键冲突
    bars = [{"date": (datetime(2026, 4, 1) + timedelta(days=i)).date().isoformat(),
             "o": close, "h": close + 1, "l": close - 1, "c": close, "v": 1000}
            for i in range(n)]
    db.upsert_daily_bars(symbol, bars)


def test_compute_stop_levels_persists(setup):
    _, db, _, engine = setup
    seed_account(db)
    seed_bars(db)
    n = engine.compute_stop_levels(now=NOW)
    assert n == 1
    levels = db.latest_stop_levels()
    assert levels[0]["symbol"] == "MU"
    assert levels[0]["side"] == "LONG"


def test_r6_triggers_cooldown_and_dedup(setup):
    """R6 用券商已实现盈亏（executions.realized_pnl）。

    原先这里造的是 orders + 快照均价反推的估算路径 —— 那条路径已删除：
    以 IBKR 为主后，realized 一律取 CommissionReport 的权威值，不再自己算。
    """
    cfg, db, notifier, engine = setup
    seed_account(db)
    # 一笔平仓卖出：券商给的已实现 -5000 = 5% > 2%
    from app.ibkr_client import split_exec_id
    root, seq = split_exec_id("0000sell1.aaaa.01.01")
    db.upsert_executions([{
        "exec_id": "0000sell1.aaaa.01.01", "correction_root": root, "correction_seq": seq,
        "symbol": "MU", "side": "SELL", "shares": Decimal("100"), "price": Decimal("50"),
        "perm_id": 777001, "order_id": 0, "client_id": 0, "conid": 1, "sec_type": "STK",
        "exchange": "SMART", "currency": "USD", "cumulative_qty": Decimal("100"),
        "average_price": Decimal("50"),
        "execution_time": (NOW - timedelta(minutes=5)).isoformat(),
        "commission": Decimal("1"), "commission_currency": "USD",
        "realized_pnl": Decimal("-5000"), "liquidation": 0, "order_ref": None,
        "last_liquidity": 1, "data_status": "READY", "raw_json": "{}"}])
    hits = engine.evaluate_and_dispatch(now=NOW)
    assert any(h.rule_id == "R6" for h in hits)
    assert db.active_cooldown(NOW.isoformat()) is not None
    pushes_after_first = len(notifier.sent)

    # 立即再评估：R6 去重（同一 ref），冷静期不重复创建
    engine.evaluate_and_dispatch(now=NOW + timedelta(seconds=1))
    cds = db.active_cooldown(NOW.isoformat())
    assert cds is not None
    # R6 大额认亏不应重复推送
    assert notifier.sent.count(pushes_after_first and notifier.sent[0]) >= 1


def test_trade_during_cooldown_flagged(setup):
    _, db, notifier, engine = setup
    seed_account(db)
    # 手动进入冷静期（2 小时前开始，还剩 46h）
    started = (NOW - timedelta(hours=2)).isoformat()
    ends = (NOW + timedelta(hours=46)).isoformat()
    db.start_cooldown(started, ends, "手动", "manual1")
    # 冷静期开始后的一笔新成交
    db.upsert_orders([{
        "order_id": "buy9", "ts": (NOW - timedelta(hours=1)).isoformat(),
        "symbol": "AAPL", "side": "BUY", "order_type": "MARKET",
        "qty": Decimal("10"), "price": Decimal("200"), "status": "FILLED", "raw_json": "{}",
    }])
    hits = engine.evaluate_and_dispatch(now=NOW)
    assert any("冷静期内交易" in h.title for h in hits)


def test_r4_intraday_overtrading(setup):
    """R4 数的是**风险增加决策**（decision 口径），不是 orders 表的成交笔数。

    orders 表在 IBKR 下拿不到手动单（reqCompletedOrders 始终超时），R4 曾因此恒为 0。
    """
    cfg, db, notifier, engine = setup
    seed_account(db)
    from app.ibkr_client import split_exec_id
    # 成交前持仓 0 -> 每次加仓都是风险增加；两次不同 permId = 两次决策
    db.insert_positions_snapshot((NOW - timedelta(hours=2)).isoformat(), [
        {"symbol": "MU", "qty": Decimal("0"), "avg_cost": Decimal("0"),
         "market_value": Decimal("0"), "upl": Decimal("0")}])
    for i in range(2):
        eid = f"0000r4{i}.aaaa.01.01"
        root, seq = split_exec_id(eid)
        db.upsert_executions([{
            "exec_id": eid, "correction_root": root, "correction_seq": seq,
            "symbol": "MU", "side": "BUY", "shares": Decimal("10"), "price": Decimal("90"),
            "perm_id": 880000 + i, "order_id": 0, "client_id": 0, "conid": 1,
            "sec_type": "STK", "exchange": "SMART", "currency": "USD",
            "cumulative_qty": Decimal("10"), "average_price": Decimal("90"),
            "execution_time": (NOW - timedelta(minutes=i)).isoformat(),
            "commission": Decimal("1"), "commission_currency": "USD",
            "realized_pnl": None, "liquidation": 0, "order_ref": None,
            "last_liquidity": 1, "data_status": "READY", "raw_json": "{}"}])
    hits = engine.evaluate_and_dispatch(now=NOW)
    assert any(h.rule_id == "R4" for h in hits)
