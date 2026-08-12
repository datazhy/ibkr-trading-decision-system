"""IBKR 模式下 engine 的 R6 精确路径：用 executions.realized_pnl（券商权威值）而非估算。"""
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from app.db import Database
from app.engine import RuleEngine
from app.notify import Notifier

NOW = datetime(2026, 7, 10, 18, 0, tzinfo=timezone.utc)


class RecordingNotifier(Notifier):
    def __init__(self, db):
        super().__init__([], db)
        self.sent = []

    def send(self, title, body):
        self.sent.append(title)
        return True


@pytest.fixture
def setup_ibkr(make_config):
    cfg = make_config()
    db = Database(cfg.db_path)
    db.init_schema()
    db.insert_balances_snapshot(NOW.isoformat(), {
        "equity": Decimal("100000"), "cash": Decimal("50000"),
        "margin_balance": Decimal("50000"), "buying_power": Decimal("80000")})
    return cfg, db, RecordingNotifier(db), None


def _seed_realized(db, exec_id="0000r6a.aaaa.01.01", realized="-5000", perm_id=555001,
                   ts=None):
    """一笔已实现亏损（executions 是 R6 的唯一数据源）。"""
    from app.ibkr_client import split_exec_id
    root, seq = split_exec_id(exec_id)
    db.upsert_executions([{
        "exec_id": exec_id, "correction_root": root, "correction_seq": seq,
        "symbol": "MU", "side": "SELL", "shares": Decimal("100"), "price": Decimal("50"),
        "perm_id": perm_id, "order_id": 0, "client_id": 0, "conid": 1,
        "sec_type": "STK", "exchange": "SMART", "currency": "USD",
        "cumulative_qty": Decimal("100"), "average_price": Decimal("50"),
        "execution_time": ts or (NOW - timedelta(minutes=5)).isoformat(),
        "commission": Decimal("1"), "commission_currency": "USD",
        "realized_pnl": Decimal(realized), "liquidation": 0, "order_ref": None,
        "last_liquidity": 1, "data_status": "READY", "raw_json": "{}"}])


def test_r6_not_affected_by_order_time(setup_ibkr):
    """回放：订单下单时间晚，成交(realized)时间早 —— R6 不得被订单时间挤掉。

    历史上 R6 用 orders 表 ts 推进 cursor，导致 realized 事件 ts < cursor 时永久漏触发。
    现在 R6 的水位是 executions.r6_state（按成交本身，不是任何时间戳），
    这类时间戳错位的 bug 在结构上已不可能发生。
    """
    cfg, db, notifier, _ = setup_ibkr
    engine = RuleEngine(cfg, db, notifier)

    # 一张较晚下单的订单（ts=NOW）
    db.upsert_orders([{
        "order_id": "late-order", "ts": NOW.isoformat(), "symbol": "MU", "side": "BUY",
        "order_type": "MARKET", "qty": Decimal("100"), "price": Decimal("90"),
        "status": "FILLED", "raw_json": "{}"}])
    engine.evaluate_and_dispatch(now=NOW)

    # 盘中成交落库，成交时间比订单早 2 小时
    fill_ts = (NOW - timedelta(hours=2)).isoformat()
    _seed_realized(db, exec_id="0000early.aaaa.01.01", perm_id=555002, ts=fill_ts)
    hits = engine.evaluate_and_dispatch(now=NOW)

    assert any(h.rule_id == "R6" for h in hits)                     # 未被订单时间挤掉
    assert db.active_cooldown(NOW.isoformat()) is not None


def test_r6_cursor_prevents_reprocessing(setup_ibkr):
    """同一 realized 事件不重复建冷静期。"""
    cfg, db, notifier, _ = setup_ibkr
    engine = RuleEngine(cfg, db, notifier)
    _seed_realized(db)
    engine.evaluate_and_dispatch(now=NOW)
    engine.evaluate_and_dispatch(now=NOW + timedelta(seconds=30))
    with db.connect() as conn:
        n = conn.execute("SELECT COUNT(*) c FROM cooldown_state").fetchone()["c"]
    assert n == 1  # 冷静期只建一次


def test_r6_uses_exact_realized(setup_ibkr):
    cfg, db, notifier, _ = setup_ibkr
    engine = RuleEngine(cfg, db, notifier)
    # 精确已实现亏损 -5000 = 5% > 2% → R6（券商权威值，非估算）
    _seed_realized(db)
    hits = engine.evaluate_and_dispatch(now=NOW)
    r6 = [h for h in hits if h.rule_id == "R6"]
    assert len(r6) == 1
    assert "5.0%" in r6[0].message
    assert db.active_cooldown(NOW.isoformat()) is not None  # 冷静期已建


def test_r6_small_exact_loss_not_triggered(setup_ibkr):
    cfg, db, notifier, _ = setup_ibkr
    engine = RuleEngine(cfg, db, notifier)
    _seed_realized(db, realized="-800", perm_id=555003)     # 0.8% < 2%
    hits = engine.evaluate_and_dispatch(now=NOW)
    assert not [h for h in hits if h.rule_id == "R6"]


def test_ibkr_mode_skips_estimation(setup_ibkr):
    """IBKR 模式下不走快照估算（同一笔卖出不会因估算被重复计入）。"""
    cfg, db, notifier, _ = setup_ibkr
    engine = RuleEngine(cfg, db, notifier)
    # 有估算路径会命中的数据（orders+快照），但 realized 为 NULL
    db.insert_positions_snapshot((NOW - timedelta(hours=1)).isoformat(), [
        {"symbol": "MU", "qty": Decimal("100"), "avg_cost": Decimal("100"),
         "market_value": Decimal("9000"), "upl": Decimal("-1000")}])
    db.upsert_orders([{
        "order_id": "o1", "ts": (NOW - timedelta(minutes=5)).isoformat(),
        "symbol": "MU", "side": "SELL", "order_type": "MARKET",
        "qty": Decimal("100"), "price": Decimal("50"), "status": "FILLED",
        "raw_json": "{}"}])
    hits = engine.evaluate_and_dispatch(now=NOW)
    # 无 realized 记录 → IBKR 模式不产生 R6（不回退估算）
    assert not [h for h in hits if h.rule_id == "R6" and h.triggers_cooldown_hours]


def test_config_needs_no_oauth_credentials(tmp_path, monkeypatch):
    """IBKR 认证在 gateway 侧完成，配置层不该要求任何 OAuth 凭证。"""
    from app.config import load_config
    for k in ("ACCOUNT_ENC_KEY", "IB_PORT"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("ACCOUNT_ENC_KEY", "x" * 44)
    monkeypatch.setenv("IB_PORT", "4003")
    cfg = load_config(env_file=None)
    assert cfg.ib_port == 4003
    assert not hasattr(cfg, "broker")        # 单一数据源，不再有 broker 开关
