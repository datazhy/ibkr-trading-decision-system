"""IBKR 适配器解析器测试（SimpleNamespace 模拟 ib_async 对象，无需网关）。"""
from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace as NS

from app.ibkr_client import (
    parse_ib_account_summary,
    parse_ib_bars,
    parse_ib_fill,
    parse_ib_portfolio,
    parse_ib_trade,
)


def test_parse_portfolio_long_short_and_filters():
    items = [
        NS(contract=NS(symbol="MU", secType="STK"), position=400,
           marketValue=38000.0, averageCost=95.0, unrealizedPNL=0.0),
        NS(contract=NS(symbol="MRVL", secType="STK"), position=-100,
           marketValue=-7600.0, averageCost=80.0, unrealizedPNL=400.0),
        NS(contract=NS(symbol="SPY", secType="OPT"), position=2,
           marketValue=1000.0, averageCost=5.0, unrealizedPNL=0.0),  # 期权过滤
        NS(contract=NS(symbol="ZERO", secType="STK"), position=0,
           marketValue=0.0, averageCost=0.0, unrealizedPNL=0.0),     # 零仓过滤
    ]
    rows = parse_ib_portfolio(items)
    assert [r["symbol"] for r in rows] == ["MU", "MRVL"]
    assert rows[1]["qty"] == Decimal("-100")  # 空头负数
    assert rows[0]["market_value"] == Decimal("38000.0")


def test_parse_account_summary_margin_semantics():
    rows = [
        NS(tag="NetLiquidation", value="100000.5", currency="USD"),
        NS(tag="TotalCashValue", value="-2200", currency="USD"),   # 负现金=融资
        NS(tag="BuyingPower", value="42000", currency="USD"),
        NS(tag="NetLiquidation", value="700000", currency="HKD"),  # 非基准货币忽略
        NS(tag="AccountType", value="INDIVIDUAL", currency=""),    # 非数值忽略
    ]
    b = parse_ib_account_summary(rows)
    assert b["equity"] == Decimal("100000.5")
    assert b["cash"] == Decimal("-2200")
    assert b["margin_balance"] == Decimal("-2200")  # R3 语义等价
    assert b["buying_power"] == Decimal("42000")


def _mk_trade(perm=123456, action="BUY", otype="LMT", qty=100, lmt=95.5, status="Filled"):
    return NS(
        contract=NS(symbol="MU"),
        order=NS(permId=perm, orderId=7, action=action, orderType=otype,
                 totalQuantity=qty, lmtPrice=lmt, account="U111"),
        orderStatus=NS(status=status),
        log=[NS(time=datetime(2026, 7, 10, 14, 0, tzinfo=timezone.utc))],
    )


def test_parse_trade_uses_permid_and_status():
    r = parse_ib_trade(_mk_trade())
    assert r["order_id"] == "123456"
    assert r["side"] == "BUY"
    assert r["status"] == "FILLED"
    assert r["price"] == Decimal("95.5")
    assert r["ts"].startswith("2026-07-10T14:00")


def test_parse_trade_market_order_no_price():
    r = parse_ib_trade(_mk_trade(otype="MKT", lmt=0))
    assert r["price"] is None
    r2 = parse_ib_trade(_mk_trade(otype="MKT", lmt=1.7976931348623157e308))  # IBKR 哨兵
    assert r2["price"] is None


def _mk_fill(side="SLD", shares=100, price=50.0, realized=-5000.0, exec_id="0001.abc"):
    return NS(
        contract=NS(symbol="MU"),
        execution=NS(execId=exec_id, side=side, shares=shares, price=price,
                     acctNumber="U111", time=None),
        commissionReport=NS(commission=1.02, realizedPNL=realized),
        time=datetime(2026, 7, 10, 15, 30, tzinfo=timezone.utc),
    )


def test_parse_fill_sell_with_realized():
    r = parse_ib_fill(_mk_fill())
    assert r["txn_id"] == "0001.abc"
    assert r["action"] == "SELL"          # SLD -> SELL
    assert r["qty"] == Decimal("100")
    assert r["amount"] == Decimal("5000.0")   # 卖出入金为正
    assert r["fees"] == Decimal("1.02")
    assert r["realized"] == Decimal("-5000.0")  # 精确已实现盈亏


def test_parse_fill_buy_no_realized():
    r = parse_ib_fill(_mk_fill(side="BOT", realized=0.0))
    assert r["action"] == "BUY"
    assert r["amount"] == Decimal("-5000.0")  # 买入出金为负
    assert r["realized"] is None              # 0 视为非平仓


def test_parse_fill_sentinel_realized_ignored():
    r = parse_ib_fill(_mk_fill(realized=1.7976931348623157e308))
    assert r["realized"] is None


def test_parse_bars_date_normalization():
    bars = [
        NS(date=datetime(2026, 7, 9, 4, 0, tzinfo=timezone.utc),
           open=10, high=12, low=9, close=11, volume=1000),
        NS(date="2026-07-10", open=11, high=13, low=10, close=12, volume=1200),
    ]
    rows = parse_ib_bars(bars)
    assert rows[0]["date"] == "2026-07-09"
    assert rows[1]["date"] == "2026-07-10"
    assert rows[1]["c"] == Decimal("12")
