"""1 秒当前态 / 5 秒快照 / 60 秒校准的边界测试。"""
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock

from app.db import Database
from app.ibkr_client import IBKRClient
from app.scheduler import build_scheduler


def test_live_fetch_revalues_from_futu_price_using_ibkr_position_inputs(make_config):
    cfg = make_config(futu_quote_enabled=True)
    contract = SimpleNamespace(symbol="AAA", secType="STK", conId=101, currency="USD")
    item = SimpleNamespace(contract=contract, position=10, averageCost=8,
                           marketPrice=9.5, marketValue=95,
                           unrealizedPNL=15, realizedPNL=0)
    summary = [
        SimpleNamespace(tag="NetLiquidation", value="1000", currency="USD"),
        SimpleNamespace(tag="TotalCashValue", value="905", currency="USD"),
        SimpleNamespace(tag="BuyingPower", value="1800", currency="USD"),
        SimpleNamespace(tag="GrossPositionValue", value="95", currency="USD"),
    ]

    class IB:
        def portfolio(self, account): return [item]
        def accountSummary(self, account): return summary

    class Futu:
        def cached(self, symbols, **kwargs):
            return {"AAA": {"price": Decimal("9.75"), "price_source": "LAST",
                    "quality": "REALTIME", "as_of": "2026-07-31T14:00:00+00:00",
                    "previous_close": Decimal("9"),
                    "market_data_type": None, "source": "FUTU_OpenD",
                    "requested_market_data_type": None,
                    "effective_market_data_type": None}}

    client = IBKRClient.__new__(IBKRClient)
    client.cfg = cfg
    client._live_started = True
    client._live_account = "U1"
    client._live_account_pnl = SimpleNamespace(
        dailyPnL=12, realizedPnL=2, unrealizedPnL=10)
    client._live_position_pnl = {
        101: SimpleNamespace(dailyPnL=12, realizedPnL=Decimal("4.25"))}
    client._live_times = {
        "positions_as_of": "2026-07-31T14:00:00+00:00",
        "account_as_of": "2026-07-31T13:59:00+00:00",
        "pnl_as_of": "2026-07-31T14:00:00+00:00",
        "orders_as_of": None, "executions_as_of": None,
        "reconciled_at": "2026-07-31T13:59:30+00:00",
    }
    client._futu_quote_client = Futu()
    client._ensure_connected = lambda: IB()
    client.start_live = lambda account: None

    positions, balances = client.fetch_live_positions_and_balances("U1")
    p = positions[0]
    assert p["qty"] == Decimal("10")                 # IBKR
    assert p["avg_cost"] == Decimal("8")             # IBKR
    assert p["market_value"] == Decimal("97.50")     # 富途现价 × IBKR 数量
    assert p["upl"] == Decimal("17.50")               # 富途现价 × IBKR 成本
    assert p["price"] == Decimal("9.75")             # 富途
    assert p["day_pnl"] == Decimal("12")             # IBKR reqPnLSingle
    assert p["futu_market_value"] == Decimal("97.50")
    assert p["futu_unrealized_estimate"] == Decimal("17.50")
    assert p["futu_intraday_mtm"] == Decimal("7.50")
    assert p["realized_pnl"] == Decimal("4.25")      # IBKR 逐持仓已实现
    assert p["realized_pnl_source"] == "IBKR_reqPnLSingle"
    assert balances["equity"] == Decimal("1000")     # IBKR accountSummary
    assert balances["cash"] == Decimal("905")        # IBKR accountSummary
    assert balances["buying_power"] == Decimal("1800")  # IBKR accountSummary
    assert balances["_account_pnl"]["source"] == "IBKR_reqPnL"
    assert balances["_account_pnl"]["daily_pnl"] == Decimal("12")
    assert balances["_futu_shadow"]["account_pnl_authoritative"] is False
    assert balances["account_as_of"] == "2026-07-31T13:59:00+00:00"


def test_snapshot_persists_independent_source_times(make_config):
    cfg = make_config()
    db = Database(cfg.db_path)
    db.init_schema()
    balances = {
        "equity": Decimal("1000"), "cash": Decimal("500"),
        "margin_balance": Decimal("500"), "buying_power": Decimal("1500"),
        "account": "U1", "positions_as_of": "p", "account_as_of": "a",
        "pnl_as_of": "n", "orders_as_of": "o", "executions_as_of": "e",
        "reconciled_at": "r",
    }
    db.write_account_snapshot(snapshot_id="s", ts="2026-07-31T14:00:00+00:00",
                              balances=balances, positions=[])
    row = dict(db.latest_snapshot_row())
    assert [row[k] for k in ("positions_as_of", "account_as_of", "pnl_as_of",
                              "orders_as_of", "executions_as_of", "reconciled_at")] \
           == ["p", "a", "n", "o", "e", "r"]


def test_futu_daily_mark_to_market_closes_intraday_trades():
    positions = [{"symbol": "AAA", "qty": Decimal("12")}]
    quotes = {"AAA": {"price": Decimal("110"), "previous_close": Decimal("100"),
                       "source": "FUTU_OpenD"}}
    fills = [{"symbol": "AAA", "side": "BUY", "shares": Decimal("2"),
              "price": Decimal("105"), "commission": Decimal("1")}]
    result = IBKRClient._futu_mark_to_market(positions, quotes, fills)
    # 日初 10 股涨 10 元 + 日内买入 2 股涨 5 元 - 1 元佣金。
    assert result["by_symbol"]["AAA"] == Decimal("109")
    assert result["total"] == Decimal("109") and result["complete"] is True


def test_futu_daily_mark_to_market_requires_every_quote():
    positions = [{"symbol": "AAA", "qty": Decimal("1")}]
    assert IBKRClient._futu_mark_to_market(positions, {}, []) is None


def test_live_mode_removes_all_duplicate_account_polling_jobs(make_config):
    cfg = make_config(live_sync_enabled=True)
    db = Database(cfg.db_path); db.init_schema()
    dummy = MagicMock()
    scheduler = build_scheduler(cfg, db, dummy, dummy, dummy)
    ids = {job.id for job in scheduler.get_jobs()}
    assert "positions_rules" not in ids
    assert "risk_ops" in ids                        # 只读最新实时快照做事件运营
    assert "orders" not in ids and "executions" not in ids
