"""rule_runs 审计：5 态区分 + 纯追加不覆盖。"""
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from app import webdata
from app.db import Database
from app.engine import RuleEngine
from app.notify import Notifier

NOW = datetime(2026, 7, 10, 18, 0, tzinfo=timezone.utc)


@pytest.fixture
def env(make_config):
    cfg = make_config()
    db = Database(cfg.db_path)
    db.init_schema()
    return cfg, db, RuleEngine(cfg, db, Notifier([], db))


def _seed_balances(db, equity="100000"):
    db.insert_balances_snapshot(NOW.isoformat(), {
        "equity": Decimal(equity), "cash": Decimal("50000"),
        "margin_balance": Decimal("50000"), "buying_power": Decimal("80000")})


def test_never_run(make_config):
    cfg = make_config()
    db = Database(cfg.db_path); db.init_schema()
    st = webdata.rule_run_status(cfg, db, NOW)
    assert st["state"] == "NEVER_RUN"


def test_ok_no_trigger(env):
    cfg, db, engine = env
    _seed_balances(db)
    db.insert_positions_snapshot(NOW.isoformat(), [
        {"symbol": "MU", "qty": Decimal("10"), "avg_cost": Decimal("100"),
         "market_value": Decimal("1000"), "upl": Decimal("0")}])  # 1% 集中度，无违规
    engine.evaluate_and_dispatch(now=NOW)
    assert db.latest_rule_run()["run_status"] == "OK"


def test_insufficient_data_when_no_balances(env):
    cfg, db, engine = env
    engine.evaluate_and_dispatch(now=NOW)  # 无余额/持仓
    assert db.latest_rule_run()["run_status"] == "INSUFFICIENT_DATA"


def test_not_evaluated_when_frozen_quote(env):
    cfg, db, engine = env
    _seed_balances(db)
    db.insert_positions_snapshot(NOW.isoformat(), [
        {"symbol": "MU", "qty": Decimal("10"), "avg_cost": Decimal("100"),
         "market_value": Decimal("900"), "upl": Decimal("-100"),
         "price": Decimal("90"), "price_quality": "FROZEN"}])  # 跌破止损但行情冻结
    db.upsert_stop_level(NOW.astimezone(cfg.market_tz).date().isoformat(), {
        "symbol": "MU", "side": "LONG", "close": Decimal("95"), "atr": Decimal("2"),
        "swing": Decimal("92"), "stop_price": Decimal("92"), "risk_amount": Decimal("0"),
        "risk_pct": Decimal("0"), "exceeds_2pct": False, "suggested_trim": 0,
        "reached_1r": False, "trailing_stop": Decimal("0"), "take_profit": ""})
    engine.evaluate_and_dispatch(now=NOW)
    # 冻结价 -> 不判定（而非「过期」）：整体 run_status 随之为 NOT_EVALUATED
    assert db.latest_rule_run()["run_status"] == "NOT_EVALUATED"


def test_runs_are_appended_not_overwritten(env):
    cfg, db, engine = env
    _seed_balances(db)
    engine.evaluate_and_dispatch(now=NOW)
    engine.evaluate_and_dispatch(now=NOW + timedelta(minutes=1))
    with db.connect() as conn:
        n = conn.execute("SELECT COUNT(*) c FROM rule_runs").fetchone()["c"]
    assert n == 2  # 追加写，历史不被覆盖


def test_webdata_status_never_run_vs_ran(env):
    cfg, db, engine = env
    _seed_balances(db)
    engine.evaluate_and_dispatch(now=NOW)
    st = webdata.rule_run_status(cfg, db, NOW)
    assert st["state"] != "NEVER_RUN"
    assert st["rules"]  # 含每条规则状态
