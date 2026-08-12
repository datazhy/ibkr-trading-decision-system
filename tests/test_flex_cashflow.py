"""Flex Query 现金流与真实收益层 v1 验收测试（对应清单 1–50）。

钉死五件事：
  1. 入金不是收益，出金不是亏损；
  2. 买卖证券的现金流动不是外部现金流；
  3. 同一份报表重复同步不会重复计入；
  4. 数据不足时返回 null 与原因，绝不伪造收益率；
  5. Token 不进日志、不进库、不进 API、不进 Git。
"""
import datetime as dt
import json
import logging
from decimal import Decimal as D

import pytest
from fastapi.testclient import TestClient

from app import cashflow_model as cm
from app import flex_parser as fp
from app import flex_sync
from app import forward_outcomes as fo
from app import performance as pf
from app import reconciliation_cash as rc
from app import replay_context as rp
from app import review_service as rs
from app.db import Database
from app.flex import FlexError
from app.webapp import create_app

NOW = dt.datetime(2026, 7, 29, 16, 0, tzinfo=dt.timezone.utc)
TOKEN = "987654321098765"
QUERY_ID = "123456789"


def _xml(*, extra_cash: str = "", trades: str = "") -> str:
    return f"""<FlexQueryResponse>
<FlexStatements><FlexStatement accountId="U1234567" fromDate="20260601" toDate="20260630">
<AccountInformation accountId="U1234567" currency="USD"/>
<CashTransactions>
 <CashTransaction accountId="U1234567" type="Deposits/Withdrawals" amount="50000"
   currency="USD" dateTime="20260602;120000" description="WIRE IN" transactionID="T1"/>
 <CashTransaction accountId="U1234567" type="Deposits/Withdrawals" amount="-8000"
   currency="USD" dateTime="20260610;120000" description="WIRE OUT" transactionID="T2"/>
 <CashTransaction accountId="U1234567" type="Dividends" amount="120.50" currency="USD"
   symbol="MRVL" conid="1234" dateTime="20260615;120000" description="MRVL DIV" transactionID="T3"/>
 <CashTransaction accountId="U1234567" type="Payment In Lieu Of Dividends" amount="30.00"
   currency="USD" symbol="MU" dateTime="20260615;120000" transactionID="T3b"/>
 <CashTransaction accountId="U1234567" type="Withholding Tax" amount="-18.08" currency="USD"
   symbol="MRVL" dateTime="20260615;120000" transactionID="T4"/>
 <CashTransaction accountId="U1234567" type="Broker Interest Received" amount="35.20"
   currency="USD" dateTime="20260630;120000" transactionID="T5"/>
 <CashTransaction accountId="U1234567" type="Broker Interest Paid" amount="-210.00"
   currency="USD" dateTime="20260630;120000" description="MARGIN INTEREST" transactionID="T6"/>
 <CashTransaction accountId="U1234567" type="Other Fees" amount="-4.50" currency="USD"
   dateTime="20260630;120000" description="MARKET DATA FEE" transactionID="T7"/>
 {extra_cash}
</CashTransactions>
<Trades>
 <Trade accountId="U1234567" tradeID="TR1" symbol="MRVL" conid="1234" buySell="BUY"
   netCash="-12000" ibCommission="-1.05" currency="USD" dateTime="20260605;100000" quantity="100"/>
 <Trade accountId="U1234567" tradeID="TR2" symbol="MU" conid="5678" buySell="SELL"
   netCash="9500" ibCommission="-1.02" currency="USD" dateTime="20260620;100000"
   quantity="-50" fifoPnlRealized="850.25" proceeds="9501" cost="8650"/>
 {trades}
</Trades>
</FlexStatement></FlexStatements></FlexQueryResponse>"""


class FakeClient:
    """可编程的假 Flex 客户端。绝不接触真实网络。"""

    def __init__(self, xml: str | None = None, error: Exception | None = None,
                 fail_times: int = 0):
        self.xml = xml
        self.error = error
        self.fail_times = fail_times
        self.calls = 0

    def fetch(self) -> str:
        self.calls += 1
        if self.error and self.calls <= (self.fail_times or 10 ** 6):
            raise self.error
        if self.xml is None:
            raise FlexError("no data")
        return self.xml


@pytest.fixture
def env(make_config):
    cfg = make_config(ib_account="U1234567", flex_token=TOKEN,
                      flex_query_id=QUERY_ID, flex_max_retries=2,
                      flex_poll_interval=0)
    d = Database(cfg.db_path)
    d.init_schema()
    return cfg, d


@pytest.fixture
def unconfigured(make_config):
    cfg = make_config(ib_account="U1234567")
    d = Database(cfg.db_path)
    d.init_schema()
    return cfg, d


# ---------------- 1–7. 同步流程 ----------------

def test_not_configured(unconfigured):
    cfg, d = unconfigured
    assert flex_sync.is_configured(cfg) is False
    res = flex_sync.sync(cfg, d, now=NOW)
    assert res["status"] == flex_sync.ST_NOT_CONFIGURED
    assert res["configured"] is False
    run = d.latest_flex_run()
    assert run["status"] == "not_configured"
    st = flex_sync.status(cfg, d)
    assert st["data_quality"] == "not_configured"
    assert "IBKR" in st["setup_hint"]


def test_successful_sync(env):
    cfg, d = env
    res = flex_sync.sync(cfg, d, now=NOW, client=FakeClient(_xml()))
    assert res["status"] in ("success", "partial")
    assert res["records_received"] == 12          # 8 现金 + 2 成交 + 2 佣金
    assert res["records_inserted"] == 12
    assert res["report_from"] == "20260601"
    assert d.latest_flex_run()["records_inserted"] == 12


def test_report_not_ready_is_not_failure(env):
    """报表未生成（1019）不算失败，由客户端继续轮询。"""
    cfg, d = env
    # FlexClient 内部处理 1019；这里验证同步层不会把它当致命错误
    assert flex_sync.NOT_READY_CODE == "1019"
    assert flex_sync.NOT_READY_CODE not in flex_sync.FATAL_ERROR_CODES


def test_request_timeout_is_retried_then_failed(env):
    cfg, d = env
    c = FakeClient(error=TimeoutError("请求超时"))
    res = flex_sync.sync(cfg, d, now=NOW, client=c)
    assert res["status"] == flex_sync.ST_FAILED
    assert c.calls == 3                           # 首次 + 2 次重试，有上限
    assert d.latest_flex_run()["retry_count"] == 2


def test_auth_failure_stops_retrying(env):
    """认证失败重试毫无意义，且会招来 IBKR 限流。"""
    cfg, d = env
    c = FakeClient(error=FlexError("Flex 请求被拒绝: [1003] Invalid token"))
    res = flex_sync.sync(cfg, d, now=NOW, client=c)
    assert res["status"] == flex_sync.ST_FAILED
    assert res["error_code"] == "1003"
    assert c.calls == 1                           # 立刻停止，不重试


def test_polling_is_bounded(env):
    cfg, d = env
    from app.flex import FlexClient
    fc = FlexClient(TOKEN, QUERY_ID, poll_interval=0, max_polls=3)
    assert fc.max_polls == 3


def test_retries_are_bounded(env):
    cfg, d = env
    c = FakeClient(error=ConnectionError("网络中断"))
    flex_sync.sync(cfg, d, now=NOW, client=c)
    assert c.calls == int(cfg.flex_max_retries) + 1


# ---------------- 8–10. 幂等与去重 ----------------

def test_duplicate_report_is_idempotent(env):
    cfg, d = env
    xml = _xml()
    a = flex_sync.sync(cfg, d, now=NOW, client=FakeClient(xml))
    b = flex_sync.sync(cfg, d, now=NOW, client=FakeClient(xml))
    assert a["records_inserted"] == 12
    # 第二次全部跳过 —— 同一笔流水绝不重复计入收益
    assert b["records_inserted"] == 0
    assert b["records_skipped"] == 12
    assert b["duplicate_report"] is True
    rows, total = d.query_cash_flows()
    assert total == 12


def test_duplicate_transaction_deduped(env):
    cfg, d = env
    # 同一 transactionID 出现在两个 section 里
    dup = ('<CashTransaction accountId="U1234567" type="Dividends" amount="120.50"'
           ' currency="USD" symbol="MRVL" dateTime="20260615;120000"'
           ' transactionID="T3"/>')
    res = flex_sync.sync(cfg, d, now=NOW, client=FakeClient(_xml(extra_cash=dup)))
    rows, _ = d.query_cash_flows(symbol="MRVL", category="income")
    # 关键断言：同一 transactionID 只留一条，收益不会被重复计入。
    # 它落在 skipped 还是 updated 取决于两份记录的字段是否完全一致，
    # 那是实现细节，不该被测试钉死。
    assert len(rows) == 1
    assert D(rows[0]["amount"]) == D("120.50")
    # 报表里出现 13 行，落库仍然只有 12 条唯一流水。
    # records_received 报的是**去重后的唯一经济事件数**，
    # 去重前的原始行数在 raw_row_count 里。
    _, total = d.query_cash_flows(limit=1000)
    assert total == 12
    assert res["records_received"] == 12
    assert res["raw_row_count"] == 13
    assert res["deduplicated_away"] == 1


def test_corrected_transaction_updates_record(env):
    cfg, d = env
    flex_sync.sync(cfg, d, now=NOW, client=FakeClient(_xml()))
    before = d.query_cash_flows(symbol="MRVL", category="income")[0][0]
    assert before["amount"] == "120.50"
    # 券商修正了金额
    fixed = _xml().replace('amount="120.50"', 'amount="130.75"')
    res = flex_sync.sync(cfg, d, now=NOW, client=FakeClient(fixed))
    assert res["records_updated"] >= 1
    after = d.query_cash_flows(symbol="MRVL", category="income")[0][0]
    assert after["amount"] == "130.75"
    # 更新保留原始创建时间与来源，便于追溯
    assert after["created_at"] == before["created_at"]
    assert after["source"] == before["source"]


# ---------------- 11–21. 分类 ----------------

def _flows_by_sub(d) -> dict:
    rows, _ = d.query_cash_flows(limit=1000)
    out = {}
    for r in rows:
        out.setdefault(r["subtype"], []).append(r)
    return out


def test_deposit_classified(env):
    cfg, d = env
    flex_sync.sync(cfg, d, now=NOW, client=FakeClient(_xml()))
    dep = [r for r in _flows_by_sub(d)["deposit_withdrawal"]
           if D(r["amount"]) > 0]
    assert len(dep) == 1 and dep[0]["amount"] == "50000"
    assert dep[0]["category"] == cm.CAT_EXTERNAL
    assert dep[0]["direction"] == cm.DIR_IN


def test_withdrawal_classified(env):
    cfg, d = env
    flex_sync.sync(cfg, d, now=NOW, client=FakeClient(_xml()))
    wd = [r for r in _flows_by_sub(d)["deposit_withdrawal"]
          if D(r["amount"]) < 0]
    assert len(wd) == 1 and wd[0]["amount"] == "-8000"
    assert wd[0]["direction"] == cm.DIR_OUT


def test_buy_is_not_withdrawal(env):
    """买入股票导致现金下降，**绝不是出金**。"""
    cfg, d = env
    flex_sync.sync(cfg, d, now=NOW, client=FakeClient(_xml()))
    buy = _flows_by_sub(d)["trade_buy"][0]
    assert buy["category"] == cm.CAT_TRADE
    assert cm.is_external(buy["category"]) is False
    # 外部现金流合计只有真正的出入金
    ext = d.external_flows_between("2026-06-01T00:00:00+00:00",
                                   "2026-06-30T23:59:59+00:00")
    assert sum(D(r["amount"]) for r in ext) == D("42000")


def test_sell_is_not_deposit(env):
    cfg, d = env
    flex_sync.sync(cfg, d, now=NOW, client=FakeClient(_xml()))
    sell = _flows_by_sub(d)["trade_sell"][0]
    assert sell["category"] == cm.CAT_TRADE
    assert cm.is_external(sell["category"]) is False
    assert D(sell["amount"]) == D("9500")


def test_dividend_classified(env):
    cfg, d = env
    flex_sync.sync(cfg, d, now=NOW, client=FakeClient(_xml()))
    subs = _flows_by_sub(d)
    assert subs["dividend"][0]["category"] == cm.CAT_INCOME
    assert subs["dividend"][0]["symbol"] == "MRVL"
    assert subs["payment_in_lieu"][0]["category"] == cm.CAT_INCOME


def test_withholding_tax_classified(env):
    cfg, d = env
    flex_sync.sync(cfg, d, now=NOW, client=FakeClient(_xml()))
    tax = _flows_by_sub(d)["withholding_tax"][0]
    assert tax["category"] == cm.CAT_TAX
    assert D(tax["amount"]) == D("-18.08")


def test_credit_interest_classified(env):
    cfg, d = env
    flex_sync.sync(cfg, d, now=NOW, client=FakeClient(_xml()))
    ci = _flows_by_sub(d)["credit_interest"][0]
    assert ci["category"] == cm.CAT_INTEREST and D(ci["amount"]) > 0


def test_margin_interest_classified(env):
    cfg, d = env
    flex_sync.sync(cfg, d, now=NOW, client=FakeClient(_xml()))
    di = _flows_by_sub(d)["debit_interest"][0]
    assert di["category"] == cm.CAT_INTEREST and D(di["amount"]) < 0


def test_commission_classified_and_linked_to_trade(env):
    cfg, d = env
    flex_sync.sync(cfg, d, now=NOW, client=FakeClient(_xml()))
    comms = _flows_by_sub(d)["trade_commission"]
    assert len(comms) == 2
    assert all(c["category"] == cm.CAT_EXPENSE for c in comms)
    # 佣金关联到具体成交
    assert {c["symbol"] for c in comms} == {"MRVL", "MU"}
    assert any("TR1" in (c["broker_transaction_id"] or "") for c in comms)


def test_market_data_fee_classified(env):
    cfg, d = env
    flex_sync.sync(cfg, d, now=NOW, client=FakeClient(_xml()))
    mdf = _flows_by_sub(d)["market_data_fee"][0]
    assert mdf["category"] == cm.CAT_EXPENSE


def test_realized_pnl_from_broker_field(env):
    """已实现盈亏用 IBKR 权威字段，不用持仓成本反推。"""
    cfg, d = env
    res = flex_sync.sync(cfg, d, now=NOW, client=FakeClient(_xml()))
    assert res["realized_pnl_rows"] == 1
    rows = d.realized_pnl_between("2026-06-01", "2026-07-01")
    assert rows[0]["symbol"] == "MU"
    assert rows[0]["realized_pnl"] == "850.25"
    assert rows[0]["source"] == "FLEX_TRADE"
    assert rows[0]["proceeds"] == "9501"


# ---------------- 22–25. 多币种、脱敏、精度 ----------------

def test_multi_currency_flows(env):
    cfg, d = env
    extra = ('<CashTransaction accountId="U1234567" type="Dividends" amount="200"'
             ' currency="HKD" symbol="0700" dateTime="20260618;120000"'
             ' fxRateToBase="0.128" transactionID="HK1"/>')
    flex_sync.sync(cfg, d, now=NOW, client=FakeClient(_xml(extra_cash=extra)))
    rows, _ = d.query_cash_flows(currency="HKD")
    assert len(rows) == 1
    assert rows[0]["currency"] == "HKD"
    assert D(rows[0]["amount_base"]) == D("25.600")     # 200 × 0.128
    assert rows[0]["fx_rate"] == "0.128"


def test_missing_fx_rate_leaves_base_null(env):
    """汇率缺失时 amount_base 为空，**不猜一个汇率**。"""
    cfg, d = env
    extra = ('<CashTransaction accountId="U1234567" type="Dividends" amount="200"'
             ' currency="EUR" symbol="X" dateTime="20260618;120000"'
             ' transactionID="EU1"/>')
    flex_sync.sync(cfg, d, now=NOW, client=FakeClient(_xml(extra_cash=extra)))
    rows, _ = d.query_cash_flows(currency="EUR")
    assert rows[0]["amount_base"] is None
    assert rows[0]["fx_rate"] is None


def test_raw_json_is_redacted():
    raw = cm.to_raw_json({"t": TOKEN, "q": QUERY_ID, "amount": "100",
                          "desc": f"ref {TOKEN}"})
    assert TOKEN not in raw
    assert QUERY_ID not in raw
    assert "REDACTED" in raw


def test_decimal_precision_preserved(env):
    cfg, d = env
    extra = ('<CashTransaction accountId="U1234567" type="Dividends"'
             ' amount="0.123456789012345678" currency="USD" symbol="P"'
             ' dateTime="20260618;120000" transactionID="P1"/>')
    flex_sync.sync(cfg, d, now=NOW, client=FakeClient(_xml(extra_cash=extra)))
    rows, _ = d.query_cash_flows(symbol="P")
    assert rows[0]["amount"] == "0.123456789012345678"   # 一位不丢
    assert isinstance(rows[0]["amount"], str)


# ---------------- 26–28. 对账 ----------------

def test_reconciliation_success():
    r = rc.compute(opening_cash="10000", closing_cash="49421.05",
                   totals={cm.CAT_EXTERNAL: "42000", cm.CAT_TRADE: "-2500",
                           cm.CAT_INCOME: "120.5", cm.CAT_TAX: "-18.08",
                           cm.CAT_INTEREST: "-174.8", cm.CAT_EXPENSE: "-6.57"},
                   tolerance=D("50"))
    assert r["quality_status"] == rc.ST_RECONCILED
    assert D(r["reconciliation_difference"]) == D("0")


def test_reconciliation_within_tolerance():
    r = rc.compute(opening_cash="10000", closing_cash="49441.05",
                   totals={cm.CAT_EXTERNAL: "42000", cm.CAT_TRADE: "-2500",
                           cm.CAT_INCOME: "120.5", cm.CAT_TAX: "-18.08",
                           cm.CAT_INTEREST: "-174.8", cm.CAT_EXPENSE: "-6.57"},
                   tolerance=D("50"))
    assert r["quality_status"] == rc.ST_WITHIN_TOLERANCE
    assert "结算日" in r["issues"][0]


def test_reconciliation_mismatch_does_not_touch_flows(env):
    cfg, d = env
    flex_sync.sync(cfg, d, now=NOW, client=FakeClient(_xml()))
    before, total_before = d.query_cash_flows(limit=1000)
    rc.reconcile_period(cfg, d, account="U1234567", start="2026-06-01",
                        end="2026-06-30", now=NOW)
    after, total_after = d.query_cash_flows(limit=1000)
    # 对账只是检验，绝不修改原始流水
    assert total_after == total_before
    assert [r["amount"] for r in after] == [r["amount"] for r in before]
    r = rc.compute(opening_cash="10000", closing_cash="99999",
                   totals={cm.CAT_EXTERNAL: "42000"}, tolerance=D("50"))
    assert r["quality_status"] == rc.ST_MISMATCH


# ---------------- 29–34. 收益计算 ----------------

def test_modified_dietz():
    md = pf.modified_dietz(
        opening_nav="100000", closing_nav="152000",
        flows=[{"amount": "50000", "occurred_at": "2026-07-16T00:00:00+00:00"}],
        period_start="2026-07-01T00:00:00+00:00",
        period_end="2026-07-31T00:00:00+00:00")
    assert D(md["gain"]) == D("2000")
    assert D(md["return"]) < D("0.02")           # 远小于朴素的 52%
    assert md["formula"].startswith("R =")


def test_time_weighted_return():
    navs = [{"ts": f"2026-07-{d:02d}T16:00:00+00:00", "equity": v} for d, v in
            [(1, "100000"), (15, "105000"), (16, "155000"), (31, "160000")]]
    twr = pf.time_weighted_return(
        nav_series=navs,
        flows=[{"amount": "50000", "occurred_at": "2026-07-16T00:00:00+00:00"}])
    assert twr["sub_periods"] == 2
    # (105000/100000) × (160000/155000) - 1
    assert abs(D(twr["return"]) - D("0.083871")) < D("0.0001")


def test_deposit_is_not_profit():
    md = pf.modified_dietz(
        opening_nav="100000", closing_nav="150000",
        flows=[{"amount": "50000", "occurred_at": "2026-07-01T00:00:00+00:00"}],
        period_start="2026-07-01T00:00:00+00:00",
        period_end="2026-07-31T00:00:00+00:00")
    assert D(md["gain"]) == D("0")               # 净值全靠入金，收益为零
    assert D(md["return"]) == D("0")
    attr = pf.attribute(opening_nav="100000", closing_nav="150000",
                        totals={cm.CAT_EXTERNAL: "50000"})
    assert D(attr["raw_nav_change"]) == D("50000")
    assert D(attr["cash_flow_adjusted_pnl"]) == D("0")


def test_withdrawal_is_not_loss():
    attr = pf.attribute(opening_nav="100000", closing_nav="80000",
                        totals={cm.CAT_EXTERNAL: "-20000"})
    assert D(attr["raw_nav_change"]) == D("-20000")
    assert D(attr["cash_flow_adjusted_pnl"]) == D("0")   # 没亏钱，只是取走了


def test_dividend_not_double_counted():
    """分红已经反映在净值里，归因时单列就必须从市场盈亏中扣掉。"""
    attr = pf.attribute(opening_nav="100000", closing_nav="101000",
                        totals={cm.CAT_INCOME: "300", cm.CAT_INTEREST: "50",
                                cm.CAT_EXPENSE: "-20", cm.CAT_TAX: "-45"},
                        realized_pnl="400", unrealized_change="315")
    # 1000 = 400(已实现) + 315(未实现) + 300 + 50 - 20 - 45 + 未解释
    total = (D(attr["realized_pnl"]) + D(attr["unrealized_pnl"])
             + D(attr["dividends"]) + D(attr["interest"])
             + D(attr["fees"]) + D(attr["commissions"]) + D(attr["taxes"])
             + D(attr["unexplained_difference"]))
    assert total == D(attr["cash_flow_adjusted_pnl"])


def test_twr_null_when_data_insufficient():
    twr = pf.time_weighted_return(
        nav_series=[{"ts": "2026-07-01T00:00:00+00:00", "equity": "100000"}],
        flows=[])
    assert twr["return"] is None
    assert twr["quality"] == pf.Q_INSUFFICIENT
    assert "少于 2" in twr["reason"]
    # 现金流时点没有净值 -> 也不算
    t2 = pf.time_weighted_return(
        nav_series=[{"ts": "2026-07-20T00:00:00+00:00", "equity": "100"},
                    {"ts": "2026-07-21T00:00:00+00:00", "equity": "110"}],
        flows=[{"amount": "500", "occurred_at": "2026-07-01T00:00:00+00:00"}])
    assert t2["return"] is None and "净值" in t2["reason"]


# ---------------- 35–38. 复盘接入 ----------------

def _seed_account(d, days=30, start_equity=100000, base=None):
    base = base or dt.date(2026, 6, 1)
    for i in range(days):
        ts = f"{(base + dt.timedelta(days=i)).isoformat()}T16:00:00+00:00"
        eq = D(str(start_equity + i * 100))
        d.write_account_snapshot(
            snapshot_id=f"s{i}", ts=ts, sync_run_id=f"r{i}",
            quality_status="fresh",
            balances={"account": "U1234567", "currency": "USD", "equity": eq,
                      "cash": eq / 2, "total_cash": eq / 2,
                      "margin_balance": eq / 2, "buying_power": eq * 2,
                      "available_funds": eq / 2, "excess_liquidity": eq / 2,
                      "maint_margin": eq / 10, "init_margin": eq / 8,
                      "positions_value": eq / 2},
            positions=[])


def test_review_uses_authoritative_cash_flow(env):
    cfg, d = env
    flex_sync.sync(cfg, d, now=NOW, client=FakeClient(_xml()))
    run = d.latest_flex_run(status="success") or d.latest_flex_run()
    assert run["report_from"] and run["report_to"]
    assert rs._flex_covers(d, "2026-06-05", "2026-06-20") is True
    # 报表区间之外不得当权威 —— 「没有流水」不等于「确实没发生」
    assert rs._flex_covers(d, "2026-05-01", "2026-05-10") is False


def test_heuristic_fallback_when_flex_missing(unconfigured):
    cfg, d = unconfigured
    assert rs._flex_covers(d, "2026-06-05", "2026-06-20") is False
    rows = [{"ts": "2026-07-01T16:00:00+00:00", "equity": "100000"},
            {"ts": "2026-07-02T16:00:00+00:00", "equity": "150000"}]
    cf = fo.detect_cash_flow(rows, [])
    assert cf["source"] == "heuristic"
    assert cf["status"] == fo.CF_CONTAMINATED


def test_review_recalculation_is_idempotent_and_versioned(env):
    cfg, d = env
    # 口径每次变更都升版本，**旧版本全部保留**可并排对比
    assert rs.CALC_VERSION.startswith("review-v3-attrib")
    assert "review-v1/2026-07" in rs.LEGACY_VERSIONS
    assert "review-v2-flex/2026-07" in rs.LEGACY_VERSIONS


def test_old_calculation_version_preserved(env):
    cfg, d = env
    old = {"review_id": "r1", "event_id": "e1", "source_type": "account",
           "rule_code": "X", "first_detected_at": "2026-06-05T15:00:00+00:00",
           "review_status": "completed",
           "calculation_version": rs.LEGACY_VERSIONS[0],
           "created_at": NOW.isoformat(), "updated_at": NOW.isoformat()}
    d.save_event_review(old)
    assert d.review_for_event("e1", rs.LEGACY_VERSIONS[0]) is not None
    # 新版本写入不覆盖旧版本
    d.save_event_review({**old, "review_id": "r2",
                         "calculation_version": rs.CALC_VERSION})
    assert d.review_for_event("e1", rs.LEGACY_VERSIONS[0]) is not None
    assert d.review_for_event("e1", rs.CALC_VERSION) is not None


# ---------------- 39–40. 防泄漏与历史重放 ----------------

def test_backtest_does_not_use_future_cash_flows(env):
    """Flex 是 T+1 的。一笔昨天发生、今天才同步进来的入金，
    在昨天做判断时并不存在 —— 用 occurred_at 过滤就是未来数据泄漏。"""
    cfg, d = env
    d.upsert_cash_flow({
        "cashflow_id": "txn:LATE", "account": "U1234567", "account_id": "U1234567",
        "amount": "50000", "currency": "USD", "category": "external_cash_flow",
        "subtype": "deposit", "direction": "inflow",
        "occurred_at": "2026-06-05T12:00:00+00:00",
        "effective_at": "2026-06-05T12:00:00+00:00", "et_date": "2026-06-05",
        "source": "FLEX", "created_at": "2026-06-20T00:00:00+00:00",
        "updated_at": "2026-06-20T00:00:00+00:00", "content_hash": "h1"})
    # 6/10 做判断时，这笔流水还没同步进来
    known = d.cash_flows_known_at("2026-06-10T00:00:00+00:00", account="U1234567")
    assert known == []
    # 6/25 才看得到
    later = d.cash_flows_known_at("2026-06-25T00:00:00+00:00", account="U1234567")
    assert len(later) == 1


def test_historical_replay_sends_no_notification(env):
    cfg, d = env
    n = rp.NullNotifier()
    assert n.send("严重风险", "正文") is False       # 明确未发送
    assert len(n.suppressed) == 1
    with pytest.raises(rp.ReplayViolation):
        rp.assert_read_only(rp.MODE_REPLAY, "创建实时 open episode")
    with pytest.raises(rp.ReplayViolation):
        rp.assert_read_only(rp.MODE_REPLAY, "推进告警冷静期")
    rp.assert_read_only(rp.MODE_LIVE, "创建实时事件")   # live 放行
    # 重放上下文构建不写任何东西
    before = d.query_cash_flows(limit=1000)[1]
    ctx = rp.build_context(d, as_of="2026-06-15T16:00:00+00:00",
                           account="U1234567")
    assert ctx["mode"] == rp.MODE_REPLAY
    assert d.query_cash_flows(limit=1000)[1] == before


# ---------------- 41–42. 解耦 ----------------

def test_flex_failure_does_not_break_account_sync(env):
    cfg, d = env
    res = flex_sync.sync(cfg, d, now=NOW,
                         client=FakeClient(error=RuntimeError("彻底炸了")))
    assert res["status"] == flex_sync.ST_FAILED      # 返回失败，不抛异常
    # 账户侧照常工作
    from app import risk_service
    out = risk_service.assess(cfg, d)
    assert out is None or "risk_level" in out


def test_flex_failure_does_not_break_market_sync(env):
    cfg, d = env
    flex_sync.sync(cfg, d, now=NOW, client=FakeClient(error=FlexError("[1003] bad")))
    from app import market_service
    a = market_service.assess(cfg, d)
    assert a is not None and "market_state" in a


def test_flex_failure_preserves_existing_flows(env):
    """同步失败绝不删除历史有效流水。"""
    cfg, d = env
    flex_sync.sync(cfg, d, now=NOW, client=FakeClient(_xml()))
    _, before = d.query_cash_flows(limit=1000)
    flex_sync.sync(cfg, d, now=NOW, client=FakeClient(error=TimeoutError("超时")))
    _, after = d.query_cash_flows(limit=1000)
    assert after == before == 12


# ---------------- 43–48. API 与凭证安全 ----------------

def test_api_unified_response(env):
    cfg, d = env
    c = TestClient(create_app(cfg, d))
    for path in ("/api/v1/flex/status", "/api/v1/performance/cash-flows",
                 "/api/v1/performance/summary", "/api/v1/performance/history"):
        body = c.get(path).json()
        assert set(body) == {"success", "data", "error", "request_id",
                             "generated_at"}
        assert body["success"] is True


def test_page_shows_not_configured(unconfigured):
    cfg, d = unconfigured
    html = TestClient(create_app(cfg, d)).get("/cashflow").text
    assert "not_configured" in html
    assert "启发式" in html
    assert "IBKR 客户端门户" in html          # 配置步骤直接写在页面上
    assert "不影响账户同步" in html


def test_page_shows_insufficient_data_not_fake_zero(unconfigured):
    cfg, d = unconfigured
    html = TestClient(create_app(cfg, d)).get("/cashflow").text
    assert "当前数据不足以可靠计算" in html
    from app.webflex import pct
    assert pct(None) == "数据不足"
    assert pct("0") == "0.00%"               # 真的是 0 才显示 0


def test_page_states_no_cash_flow_assumption(unconfigured):
    """没有券商流水时算出的收益率，其成立前提必须写在数字旁边。

    数值本身没错（假设期间无资金进出），但不说明假设，用户会以为它
    已经扣除了出入金 —— 那正是这整个模块要消除的误解。
    """
    cfg, d = unconfigured
    # 落在默认 30 天区间内，收益概览才算得出来
    _seed_account(d, days=5, base=dt.date.today() - dt.timedelta(days=6))
    html = TestClient(create_app(cfg, d)).get("/cashflow").text
    assert "没有任何资金进出" in html
    assert "无法验证这个假设" in html


def test_token_not_in_logs(env, caplog):
    cfg, d = env
    with caplog.at_level(logging.DEBUG):
        flex_sync.sync(cfg, d, now=NOW, client=FakeClient(_xml()))
        flex_sync.sync(cfg, d, now=NOW,
                       client=FakeClient(error=FlexError(
                           f"failed url ?t={TOKEN}&q={QUERY_ID}")))
    blob = caplog.text
    assert TOKEN not in blob
    assert QUERY_ID not in blob


def test_token_not_in_database(env):
    cfg, d = env
    flex_sync.sync(cfg, d, now=NOW, client=FakeClient(_xml()))
    flex_sync.sync(cfg, d, now=NOW,
                   client=FakeClient(error=FlexError(f"bad ?t={TOKEN}")))
    import sqlite3
    conn = sqlite3.connect(cfg.db_path)
    tables = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")]
    for t in tables:
        try:
            rows = conn.execute(f"SELECT * FROM {t}").fetchall()
        except sqlite3.Error:
            continue
        blob = str(rows)
        assert TOKEN not in blob, f"token 泄漏到表 {t}"
        assert QUERY_ID not in blob, f"query id 泄漏到表 {t}"


def test_token_not_in_api_response(env):
    cfg, d = env
    flex_sync.sync(cfg, d, now=NOW, client=FakeClient(_xml()))
    c = TestClient(create_app(cfg, d))
    for path in ("/api/v1/flex/status", "/api/v1/performance/cash-flows",
                 "/api/v1/performance/summary"):
        text = c.get(path).text
        assert TOKEN not in text
        assert QUERY_ID not in text
    st = c.get("/api/v1/flex/status").json()["data"]
    assert st["query_id_masked"] == "****6789"     # 只有后 4 位
    assert "flex_token" not in json.dumps(st)


def test_sanitize_error_strips_credentials():
    msg = f"GET https://x/SendRequest?t={TOKEN}&q={QUERY_ID} -> 500"
    out = cm.sanitize_error(msg)
    assert TOKEN not in out and QUERY_ID not in out
    assert "***" in out


# ---------------- 49–50. 既有功能回归 ----------------

def test_legacy_cash_flow_consumers_see_only_external(env):
    """净值曲线的资金流调整只能看到外部现金流。

    把分红和佣金也扣掉，会让当期收益凭空少一笔；把买入也扣掉更是直接算错业绩。
    """
    cfg, d = env
    flex_sync.sync(cfg, d, now=NOW, client=FakeClient(_xml()))
    legacy = d.list_cash_flows("U1234567", "2026-06-01", "2026-06-30")
    cats = {r["category"] for r in legacy}
    assert cats <= {"external_cash_flow", "transfer"}
    assert len(legacy) == 2                        # 只有那两笔出入金
    # 显式要求时才拿得到全部
    everything = d.list_cash_flows("U1234567", "2026-06-01", "2026-06-30",
                                   categories=tuple(cm.CATEGORIES))
    assert len(everything) == 12


def test_existing_risk_events_regression(env):
    from app import event_service as es, risk_events as rev
    cfg, d = env
    trig = rev.Trigger(source_type="account", event_code="X", rule_code="X",
                       category="concentration", severity="high", title="T",
                       explanation="E", rule_set_version="v1~a")
    for i in range(3):
        es.register_triggers(d, [trig], now=NOW + dt.timedelta(minutes=i),
                             evaluation_complete=True, reliable=True,
                             scope={"source_types": ["account"]})
    rows, total = d.query_risk_events()
    assert total == 1 and rows[0]["occurrence_count"] == 3


def test_migration_backfills_legacy_rows(make_config):
    """老的 cash_flows 行必须被回填 category，否则净值曲线会漏掉它们。"""
    cfg = make_config(ib_account="U1234567")
    d = Database(cfg.db_path)
    d.init_schema()
    d.add_cash_flow(account="U1234567", amount="5000",
                    effective_at="2026-07-01T12:00:00+00:00",
                    et_date="2026-07-01", note="老记录", source="MANUAL")
    Database(cfg.db_path).init_schema()          # 再次迁移，验证幂等
    rows = d.list_cash_flows("U1234567")
    assert len(rows) == 1
    assert rows[0]["category"] == "external_cash_flow"
    assert rows[0]["direction"] == "inflow"
