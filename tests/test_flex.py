"""Flex Query 解析：历史成交回补 + 资金流。

数据取自真实的 IBKR Flex XML 结构。两个必须处理对的坑：
  1. dateTime 不带时区（用账户时区，通常美东）—— 按 UTC 解会让成交落错交易日
  2. ibCommission 是负数（费用），executions 表存正数
"""
from datetime import timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from app.flex import (
    FlexError, parse_flex_cash, parse_flex_datetime, parse_flex_error,
    parse_flex_trades, parse_reference_code,
)

ET_TZ = ZoneInfo("America/New_York")

# 真实结构：KMEM 清仓的一笔 + DRAM 建仓的一笔
TRADES_XML = """<?xml version="1.0" encoding="UTF-8"?>
<FlexQueryResponse queryName="RiskDash" type="AF">
<FlexStatements count="1">
<FlexStatement accountId="U1234567" fromDate="20260701" toDate="20260716">
<Trades>
<Trade accountId="U1234567" symbol="KMEM" conid="897104052" assetCategory="STK"
  currency="USD" exchange="NASDAQ" tradeID="7001" ibExecID="0000k0.6a58.01.01"
  permId="1357400000" orderID="55" dateTime="20260716;153555" quantity="-500"
  tradePrice="17.73" ibCommission="-1.332905" ibCommissionCurrency="USD"
  fifoPnlRealized="-613.832905" buySell="SELL" orderReference="" />
<Trade accountId="U1234567" symbol="DRAM" conid="870556708" assetCategory="STK"
  currency="USD" exchange="SMART" tradeID="7002" ibExecID="0000d1.6a58.01.01"
  permId="1357400001" orderID="56" dateTime="20260716;154000" quantity="100"
  tradePrice="53.52" ibCommission="-0.210557" ibCommissionCurrency="USD"
  fifoPnlRealized="0" buySell="BUY" orderReference="" />
<Trade accountId="U1234567" symbol="ES" conid="1" assetCategory="FUT"
  currency="USD" tradeID="7003" ibExecID="0000f1.6a58.01.01" permId="9"
  dateTime="20260716;154500" quantity="1" tradePrice="5000" ibCommission="-2"
  fifoPnlRealized="0" buySell="BUY" />
</Trades>
</FlexStatement>
</FlexStatements>
</FlexQueryResponse>"""

CASH_XML = """<?xml version="1.0" encoding="UTF-8"?>
<FlexQueryResponse queryName="RiskDash" type="AF">
<FlexStatements count="1">
<FlexStatement accountId="U1234567" fromDate="20260701" toDate="20260716">
<CashTransactions>
<CashTransaction accountId="U1234567" currency="USD" amount="46691"
  dateTime="20260714;220000" type="Deposits/Withdrawals"
  description="CASH RECEIPTS / ELECTRONIC FUND TRANSFERS" transactionID="900001" />
<CashTransaction accountId="U1234567" currency="USD" amount="-5000"
  dateTime="20260715;100000" type="Deposits/Withdrawals"
  description="DISBURSEMENT INITIATED BY CUSTOMER" transactionID="900002" />
<CashTransaction accountId="U1234567" currency="USD" amount="1.23"
  dateTime="20260715;235959" type="Broker Interest Received"
  description="CREDIT INT" transactionID="900003" />
<CashTransaction accountId="U1234567" currency="USD" amount="-0.5"
  dateTime="20260715;235959" type="Other Fees" description="FEE"
  transactionID="900004" />
</CashTransactions>
</FlexStatement>
</FlexStatements>
</FlexQueryResponse>"""


# ---------------- 时区（最容易错的地方） ----------------

def test_datetime_uses_account_timezone_not_utc():
    """20260716;153555 是美东时间 -> UTC 19:35:55。按 UTC 解会差 4 小时。"""
    got = parse_flex_datetime("20260716;153555", ET_TZ)
    assert got == "2026-07-16T19:35:55+00:00"


def test_datetime_date_only():
    got = parse_flex_datetime("20260716", ET_TZ)
    assert got.startswith("2026-07-16T04:00")      # ET 00:00 = UTC 04:00


def test_datetime_invalid_returns_none():
    assert parse_flex_datetime("", ET_TZ) is None
    assert parse_flex_datetime("garbage", ET_TZ) is None


# ---------------- Trades ----------------

def test_trades_parse_to_executions_schema():
    rows = parse_flex_trades(TRADES_XML, ET_TZ)
    assert len(rows) == 2                          # 期货被过滤（只看股票/ETF）
    k = next(r for r in rows if r["symbol"] == "KMEM")
    assert k["exec_id"] == "0000k0.6a58.01.01"
    assert k["side"] == "SELL" and k["shares"] == Decimal("500")
    assert k["price"] == Decimal("17.73")
    assert k["realized_pnl"] == Decimal("-613.832905")
    assert k["perm_id"] == 1357400000
    assert k["source"] == "FLEX_QUERY"


def test_trades_commission_sign_is_normalized():
    """Flex 的 ibCommission 是负数（费用），executions 表存正数。"""
    k = next(r for r in parse_flex_trades(TRADES_XML, ET_TZ) if r["symbol"] == "KMEM")
    assert k["commission"] == Decimal("1.332905")   # 不是 -1.332905


def test_trades_exec_id_matches_realtime_path():
    """ibExecID 与 reqExecutions 的 execId 同值 -> 回补与实时同步天然幂等。"""
    from app.ibkr_client import split_exec_id
    k = next(r for r in parse_flex_trades(TRADES_XML, ET_TZ) if r["symbol"] == "KMEM")
    assert (k["correction_root"], k["correction_seq"]) == split_exec_id(k["exec_id"])


def test_trades_side_falls_back_to_quantity_sign():
    xml = TRADES_XML.replace('buySell="SELL"', "")
    k = next(r for r in parse_flex_trades(xml, ET_TZ) if r["symbol"] == "KMEM")
    assert k["side"] == "SELL"                      # quantity=-500


def test_trades_without_exec_id_are_dropped():
    xml = TRADES_XML.replace('ibExecID="0000k0.6a58.01.01"', 'ibExecID=""').replace(
        'tradeID="7001"', 'tradeID=""')
    assert all(r["symbol"] != "KMEM" for r in parse_flex_trades(xml, ET_TZ))


def test_trades_bad_xml_raises():
    with pytest.raises(FlexError):
        parse_flex_trades("<not-xml", ET_TZ)


# ---------------- CashTransactions ----------------

def test_cash_only_takes_deposits_and_withdrawals():
    """利息/手续费也在 CashTransactions 里，但它们不是资金流 ——
    混进来会让「扣除资金流后的盈亏」把利息当转账扣掉。"""
    rows = parse_flex_cash(CASH_XML, ET_TZ)
    assert len(rows) == 2
    assert {r["amount"] for r in rows} == {Decimal("46691"), Decimal("-5000")}


def test_cash_keeps_sign_deposit_positive_withdrawal_negative():
    rows = parse_flex_cash(CASH_XML, ET_TZ)
    dep = next(r for r in rows if r["amount"] > 0)
    wd = next(r for r in rows if r["amount"] < 0)
    assert dep["amount"] == Decimal("46691")        # 入金为正
    assert wd["amount"] == Decimal("-5000")         # 出金为负


def test_cash_et_date_from_account_timezone():
    """20260714;220000 ET = UTC 07-15 02:00，但 et_date 必须是 07-14。"""
    dep = next(r for r in parse_flex_cash(CASH_XML, ET_TZ) if r["amount"] > 0)
    assert dep["et_date"] == "2026-07-14"
    assert dep["effective_at"] == "2026-07-15T02:00:00+00:00"


def test_cash_carries_external_ref_for_reconciliation():
    """券商流水号：可与活动报表逐笔核对，也是幂等键。"""
    dep = next(r for r in parse_flex_cash(CASH_XML, ET_TZ) if r["amount"] > 0)
    assert dep["external_ref"] == "900001"
    assert dep["source"] == "FLEX_QUERY"
    assert "ELECTRONIC FUND TRANSFERS" in dep["note"]


def test_cash_empty_when_none():
    assert parse_flex_cash("<FlexQueryResponse/>", ET_TZ) == []


# ---------------- 错误处理 ----------------

FAIL_XML = """<FlexStatementResponse timestamp="17 July, 2026 12:00 PM EDT">
<Status>Fail</Status><ErrorCode>1012</ErrorCode>
<ErrorMessage>Token has expired.</ErrorMessage></FlexStatementResponse>"""

OK_XML = """<FlexStatementResponse timestamp="17 July, 2026 12:00 PM EDT">
<Status>Success</Status><ReferenceCode>1234567890</ReferenceCode>
<Url>https://.../GetStatement</Url></FlexStatementResponse>"""


def test_flex_error_is_http_200_with_xml():
    """Flex 的失败也是 200 + XML，必须显式识别，否则会把错误当报表解析。"""
    assert "1012" in parse_flex_error(FAIL_XML)
    assert "Token has expired" in parse_flex_error(FAIL_XML)
    assert parse_flex_error(OK_XML) is None


def test_reference_code_extracted():
    assert parse_reference_code(OK_XML) == "1234567890"


def test_reference_code_raises_on_fail():
    with pytest.raises(FlexError, match="1012"):
        parse_reference_code(FAIL_XML)


def test_client_requires_credentials():
    from app.flex import FlexClient
    with pytest.raises(FlexError, match="未配置"):
        FlexClient("", "123")
    with pytest.raises(FlexError, match="未配置"):
        FlexClient("tok", "")
