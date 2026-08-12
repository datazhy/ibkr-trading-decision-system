"""真实现金对账与完整收益归因 v1 验收测试（对应清单 1–25）。

全部基于按真实生产报表层级构造的**虚构**夹具。真实报表留在服务器 secrets/
且权限 600，不进 Git、不进测试目录。

这批测试守住四条在真实数据上验证过的结论：
  · 余额锚点不是经济流水，绝不进 cash_flows
  · 空 activityCode **不一定**都是余额标记（有流水号的是真实活动）
  · 佣金已含在交易现金流净额里，等式中再减一次就是重复扣除
  · 不同币种的金额从不直接相加
"""
import datetime as dt
import json
from decimal import Decimal as D

import pytest
from fastapi.testclient import TestClient

from app import cashflow_model as cm
from app import flex_balances as fb
from app import flex_parser as fp
from app import flex_sync
from app import flex_xml as fx
from app import performance as pf
from app import reconciliation_currency as rcc
from app import unrealized_pnl as upnl
from app.db import Database
from app.webapp import create_app
from fixtures import flex_sample as fs
from test_flex_structure import StagedClient

NOW = dt.datetime(2026, 7, 29, 16, 0, tzinfo=dt.timezone.utc)
TOKEN = "987654321098765"
QUERY_ID = "123456789"


@pytest.fixture
def env(make_config):
    cfg = make_config(ib_account=fs.TEST_ACCOUNT, flex_token=TOKEN,
                      flex_query_id=QUERY_ID, flex_max_retries=1,
                      flex_poll_interval=0)
    d = Database(cfg.db_path)
    d.init_schema()
    return cfg, d


@pytest.fixture
def synced(env):
    cfg, d = env
    flex_sync.run_flex_sync(cfg, d, now=NOW, client=StagedClient())
    return cfg, d


# ---------------- 1–6. 余额标记 ----------------

def test_opening_balance_marker_identified(make_config):
    cfg = make_config()
    ms = fb.parse_balance_markers(fs.production_shaped(), cfg.market_tz,
                                  report_from="20260401", report_to="20260630")
    openings = [m for m in ms if m.is_opening]
    assert len(openings) == 2
    assert all(m.balance_type == fb.BAL_CCY_OPENING for m in openings)
    assert all(m.quality_status == fb.QUALITY_OK for m in openings)
    assert all(m.balance_date == "2026-04-01" for m in openings)


def test_closing_balance_marker_identified(make_config):
    cfg = make_config()
    ms = fb.parse_balance_markers(fs.production_shaped(), cfg.market_tz,
                                  report_from="20260401", report_to="20260630")
    closings = [m for m in ms if m.is_closing]
    assert len(closings) == 2
    assert all(m.balance_type == fb.BAL_CCY_CLOSING for m in closings)
    assert all(m.balance_date == "2026-06-30" for m in closings)


def test_multi_currency_balance_markers(make_config):
    cfg = make_config()
    ms = fb.parse_balance_markers(fs.production_shaped(), cfg.market_tz)
    s = fb.summarize(ms)
    assert s["total"] == fs.EXPECTED_BALANCE_MARKERS
    assert tuple(s["currencies"]) == fs.EXPECTED_BALANCE_CURRENCIES
    byc = fb.by_currency(ms)
    for cur in fs.EXPECTED_BALANCE_CURRENCIES:
        assert byc[cur]["opening"] is not None
        assert byc[cur]["closing"] is not None
    # 非基础币种带汇率
    hkd = byc["HKD"]["closing"]
    assert hkd.fx_rate_to_base is not None and hkd.fx_rate_to_base != D("1")


def test_empty_activity_code_is_not_always_a_balance_marker(make_config):
    """空 activityCode 也可能是券商没填的真实活动。

    **有流水号或金额非零就不是余额锚点** —— 宁可漏认一条余额，
    也不能把一笔真实活动当成余额吞掉。
    """
    cfg = make_config()
    xml = fs.production_shaped()
    root = fx.parse_xml(xml)
    empty_code = [e for e in fx.find_descendants_by_local_name(
        root, "StatementOfFundsLine")
        if not (e.attrib.get("activityCode") or "").strip()]
    # 夹具里 5 条空 activityCode：4 条余额 + 1 条带流水号的调整
    assert len(empty_code) == (fs.EXPECTED_BALANCE_MARKERS
                               + fs.EXPECTED_NON_MARKER_EMPTY_CODE)
    markers = [e for e in empty_code if fb.looks_like_balance_marker(e.attrib)]
    assert len(markers) == fs.EXPECTED_BALANCE_MARKERS
    # 带 transactionID 的那条被正确排除
    non = [e for e in empty_code if not fb.looks_like_balance_marker(e.attrib)]
    assert len(non) == fs.EXPECTED_NON_MARKER_EMPTY_CODE
    assert non[0].attrib.get("transactionID")

    # 逐条否定条件
    assert fb.looks_like_balance_marker(
        {"activityCode": "", "balance": "100"}) is True
    assert fb.looks_like_balance_marker(
        {"activityCode": "", "balance": "100", "transactionID": "X"}) is False
    assert fb.looks_like_balance_marker(
        {"activityCode": "", "balance": "100", "amount": "-5"}) is False
    assert fb.looks_like_balance_marker(
        {"activityCode": "", "balance": "100", "symbol": "AAA"}) is False
    assert fb.looks_like_balance_marker(
        {"activityCode": "BUY", "balance": "100"}) is False
    assert fb.looks_like_balance_marker({"activityCode": ""}) is False


def test_balance_markers_never_enter_cash_flows(synced):
    """余额锚点不是经济事件，混进流水会被当成收入或支出计入收益。"""
    cfg, d = synced
    rows, total = d.query_cash_flows(limit=1000)
    assert total == fs.EXPECTED_DEDUPED_FLOWS
    # 每一条流水都有非零金额与流水号 —— 余额锚点两者都没有
    for r in rows:
        assert D(r["amount"]) != 0
        assert (r["broker_transaction_id"] or "") != ""
    # 锚点在自己的表里
    anchors = d.statement_balances(account=fs.TEST_ACCOUNT)
    assert len(anchors) == fs.EXPECTED_BALANCE_MARKERS


def test_balance_markers_are_idempotent(env):
    cfg, d = env
    flex_sync.run_flex_sync(cfg, d, now=NOW, client=StagedClient())
    first = d.statement_balances(account=fs.TEST_ACCOUNT)
    created = {r["balance_id"]: r["created_at"] for r in first}
    flex_sync.run_flex_sync(cfg, d, now=NOW, client=StagedClient())
    second = d.statement_balances(account=fs.TEST_ACCOUNT)
    assert len(second) == len(first) == fs.EXPECTED_BALANCE_MARKERS
    # 重复同步保留原始 created_at
    for r in second:
        assert r["created_at"] == created[r["balance_id"]]


# ---------------- 7–10. 分币种对账 ----------------

def test_reconciliation_from_same_report(synced):
    """对账不依赖账户快照覆盖整个报表区间 —— 快照库是空的也能对。"""
    cfg, d = synced
    assert d.equity_series_between("0000", "9999") == []      # 确实没有快照
    r = rcc.reconcile_report(cfg, d, account=fs.TEST_ACCOUNT)
    assert r["report_from"] == "20260401" and r["report_to"] == "20260630"
    assert r["currency_count"] == 2
    assert r["status"] in (rcc.ST_RECONCILED, rcc.ST_WITHIN_TOLERANCE,
                           rcc.ST_MISMATCH)
    usd = next(c for c in r["currencies"] if c["currency"] == "USD")
    assert usd["opening_balance"] == "1000"
    assert usd["closing_balance"] == "8999"
    assert D(usd["deposits"]) == D("10000")


def test_commission_not_double_deducted(synced):
    """佣金已含在交易现金流净额里，等式内不得再减一次。

    这不是推测 —— 真实报表上修正前的差异恰好等于佣金总额。
    """
    cfg, d = synced
    r = rcc.reconcile_report(cfg, d, account=fs.TEST_ACCOUNT)
    usd = next(c for c in r["currencies"] if c["currency"] == "USD")
    # 佣金仍然单列可见
    assert D(usd["commissions"]) < 0
    # 但等式里不含它：期望期末 = 期初 + 外部 + 交易 + 利息 + 非交易费用
    expected = (D(usd["opening_balance"])
                + D(usd["net_external_cash_flow"])
                + D(usd["trade_cash_flow"])
                + D(usd["interest"])
                + D(usd["other_fees"])
                + D(usd["other_confirmed_items"]))
    assert D(usd["expected_closing_balance"]) == expected
    assert any("不重复扣除" in i for i in usd["issues"])


def test_reconciliation_within_tolerance_and_mismatch():
    base = dict(currency="USD", totals={cm.CAT_EXTERNAL: "1000"},
                subtotals={}, tolerance=D("50"))
    ok = rcc.reconcile_currency(opening="0", closing="1000", **base)
    assert ok["status"] == rcc.ST_RECONCILED
    near = rcc.reconcile_currency(opening="0", closing="1020", **base)
    assert near["status"] == rcc.ST_WITHIN_TOLERANCE
    assert any("结算日" in i for i in near["issues"])
    bad = rcc.reconcile_currency(opening="0", closing="1500", **base)
    assert bad["status"] == rcc.ST_MISMATCH
    assert len(bad["issues"]) >= 2          # 列出可能原因供人判断
    # 缺锚点
    none = rcc.reconcile_currency(opening=None, closing="1000", **base)
    assert none["status"] == rcc.ST_INSUFFICIENT


def test_currencies_are_never_summed_directly(synced):
    """不同币种独立对账；折算只用于汇总展示。"""
    cfg, d = synced
    r = rcc.reconcile_report(cfg, d, account=fs.TEST_ACCOUNT)
    currencies = {c["currency"] for c in r["currencies"]}
    assert currencies == {"USD", "HKD"}
    for c in r["currencies"]:
        # 每个币种的等式都在原币口径内自洽
        if c["expected_closing_balance"] is not None:
            assert c["reconciliation_difference"] is not None
    hkd = next(c for c in r["currencies"] if c["currency"] == "HKD")
    # HKD 有独立的汇率，其差异折算后才进总计
    assert hkd["fx_rate_to_base"] is not None
    assert D(hkd["fx_rate_to_base"]) != D("1")
    assert "从不直接相加" in r["note"]


def test_partial_currency_coverage_status(env):
    """有流水但没有余额锚点的币种 -> partial_currency_coverage。"""
    cfg, d = env
    flex_sync.run_flex_sync(cfg, d, now=NOW, client=StagedClient())
    # 人为删掉 HKD 的锚点，制造覆盖不全
    import sqlite3
    conn = sqlite3.connect(cfg.db_path)
    conn.execute("DELETE FROM statement_cash_balances WHERE currency='HKD'")
    conn.execute("INSERT INTO cash_flows (cashflow_id, account, account_id,"
                 " currency, amount, category, subtype, direction, et_date,"
                 " occurred_at, effective_at, source, created_at, updated_at)"
                 " VALUES ('txn:HK9','TEST_ACCOUNT','TEST_ACCOUNT','HKD','100',"
                 " 'income','dividend','inflow','2026-05-01',"
                 " '2026-05-01T12:00:00+00:00','2026-05-01T12:00:00+00:00',"
                 " 'TEST','x','x')")
    conn.commit()
    r = rcc.reconcile_report(cfg, d, account=fs.TEST_ACCOUNT)
    assert r["status"] == rcc.ST_PARTIAL_CCY
    assert "HKD" in r["missing_anchor_currencies"]
    assert r["issues"]


# ---------------- 11–12. 汇率 ----------------

def test_conversion_rates_parsed(make_config):
    cfg = make_config()
    rates = fp.parse_conversion_rates(fs.production_shaped())
    assert len(rates) == 3
    pairs = {(r["from_currency"], r["to_currency"]) for r in rates}
    assert ("HKD", "USD") in pairs
    assert all(r["rate_date"].count("-") == 2 for r in rates)


def test_conversion_rates_idempotent(env):
    cfg, d = env
    flex_sync.run_flex_sync(cfg, d, now=NOW, client=StagedClient())
    first = d.conversion_rate_count()
    flex_sync.run_flex_sync(cfg, d, now=NOW, client=StagedClient())
    assert d.conversion_rate_count() == first == 3
    # 取汇率时不用未来的那一条
    assert d.conversion_rate("2026-05-01", "HKD", "USD") == "0.128"
    assert d.conversion_rate("2026-07-01", "HKD", "USD") == "0.129"
    assert d.conversion_rate("2026-01-01", "HKD", "USD") is None


# ---------------- 13–18. 未实现盈亏与归因 ----------------

def _seed_positions(d, days, account="TEST_ACCOUNT", upls=None):
    base = dt.date(2026, 7, 1)
    for i in range(days):
        ts = f"{(base + dt.timedelta(days=i)).isoformat()}T20:00:00+00:00"
        eq = D("50000")
        upl = D(str((upls or [100])[i % len(upls or [100])]))
        d.write_account_snapshot(
            snapshot_id=f"snap{i}", ts=ts, sync_run_id=f"r{i}",
            quality_status="fresh",
            balances={"account": account, "currency": "USD", "equity": eq,
                      "cash": eq / 2, "total_cash": eq / 2,
                      "margin_balance": eq / 2, "buying_power": eq * 2,
                      "available_funds": eq / 2, "excess_liquidity": eq / 2,
                      "maint_margin": eq / 10, "init_margin": eq / 8,
                      "positions_value": eq / 2},
            positions=[{"symbol": "TEST1", "qty": D("20"), "avg_cost": D("100"),
                        "market_value": D("2000") + upl, "upl": upl,
                        "price": D("100"), "price_quality": "DELAYED"}])


def test_daily_unrealized_uses_eod_snapshot(env):
    cfg, d = env
    _seed_positions(d, 3, upls=[100, 150, 220])
    r = upnl.build_series(cfg, d, start="2026-07-01", end="2026-07-05",
                          account=fs.TEST_ACCOUNT)
    assert r["days"] == 3
    series = d.daily_unrealized("2026-07-01", "2026-07-05",
                                account=fs.TEST_ACCOUNT,
                                version=upnl.CALC_VERSION)
    assert [x["unrealized_pnl"] for x in series] == ["100", "150", "220"]
    assert all(x["source_field"] == upnl.SRC_IBKR for x in series)
    # 每天只用一个 snapshot_id
    assert len({x["snapshot_id"] for x in series}) == 3


def test_unrealized_change_over_period(env):
    cfg, d = env
    _seed_positions(d, 3, upls=[100, 150, 220])
    upnl.build_series(cfg, d, start="2026-07-01", end="2026-07-05",
                      account=fs.TEST_ACCOUNT)
    series = d.daily_unrealized("2026-07-01", "2026-07-05",
                                account=fs.TEST_ACCOUNT,
                                version=upnl.CALC_VERSION)
    ch = upnl.period_change(series)
    assert ch["change"] == "120"           # 220 - 100
    assert ch["quality"] == upnl.Q_OK
    # 单点不给变化
    assert upnl.period_change(series[:1])["change"] is None


def test_closed_position_not_double_counted(env):
    """清仓后浮盈转为已实现，**不在未实现里重复计算**。"""
    cfg, d = env
    _seed_positions(d, 2, upls=[300, 300])
    # 第三天清仓：没有持仓 -> 未实现盈亏为 0（不是 None）
    d.write_account_snapshot(
        snapshot_id="snapX", ts="2026-07-03T20:00:00+00:00", sync_run_id="rX",
        quality_status="fresh",
        balances={"account": fs.TEST_ACCOUNT, "currency": "USD",
                  "equity": D("50300"), "cash": D("50300"),
                  "total_cash": D("50300"), "margin_balance": D("0"),
                  "buying_power": D("100000"), "available_funds": D("50300"),
                  "excess_liquidity": D("50300"), "maint_margin": D("0"),
                  "init_margin": D("0"), "positions_value": D("0")},
        positions=[])
    upnl.build_series(cfg, d, start="2026-07-01", end="2026-07-05",
                      account=fs.TEST_ACCOUNT)
    series = d.daily_unrealized("2026-07-01", "2026-07-05",
                                account=fs.TEST_ACCOUNT,
                                version=upnl.CALC_VERSION)
    assert series[-1]["unrealized_pnl"] == "0"
    assert series[-1]["position_count"] == 0
    # 贡献表里标为期间清仓，不给贡献数字（否则与已实现盈亏重复）
    contrib = upnl.position_contributions(
        d, start_date="2026-07-01", end_date="2026-07-03",
        account=fs.TEST_ACCOUNT)
    closed = [c for c in contrib if c["status"] == "closed_during_period"]
    assert closed and closed[0]["contribution"] is None
    assert "不在此处重复计算" in closed[0]["note"]


def test_realized_and_unrealized_not_double_counted():
    """归因等式里两者各自独立，加总不超出实际变化。"""
    attr = pf.attribute(opening_nav="100000", closing_nav="101000",
                        totals={cm.CAT_INCOME: "300", cm.CAT_INTEREST: "50",
                                cm.CAT_EXPENSE: "-20", cm.CAT_TAX: "-45"},
                        realized_pnl="400", unrealized_change="315")
    total = (D(attr["realized_pnl"]) + D(attr["unrealized_pnl"])
             + D(attr["dividends"]) + D(attr["interest"])
             + D(attr["commissions"]) + D(attr["fees"]) + D(attr["taxes"])
             + D(attr["unexplained_difference"]))
    assert total == D(attr["cash_flow_adjusted_pnl"])
    # market_pnl 是算出来的，不是倒推
    assert D(attr["market_pnl"]) == D("715")
    assert attr["data_quality"] != pf.Q_INSUFFICIENT


def test_dividend_counted_once():
    attr = pf.attribute(opening_nav="100000", closing_nav="100300",
                        totals={cm.CAT_INCOME: "300"},
                        realized_pnl="0", unrealized_change="0")
    assert D(attr["dividends"]) == D("300")
    assert D(attr["realized_pnl"]) == D("0")      # 不重复进已实现
    assert D(attr["unexplained_difference"]) == D("0")


def test_commission_convention_documented(env):
    cfg, d = env
    _seed_positions(d, 2, upls=[10, 20])
    res = pf.compute_period(cfg, d, start="2026-07-01", end="2026-07-05",
                            account=fs.TEST_ACCOUNT)
    m = res["calculation_method_detail"]
    assert "fifoPnlRealized" in m["commission_note"]
    assert "不含佣金" in m["commission_note"]
    assert "不存在重复扣除" in m["commission_note"]
    assert "只在 income 项计入一次" in m["dividend_note"]
    assert "不进入投资收益" in m["external_flow_note"]


# ---------------- 19–22. 外部现金流 / 覆盖 ----------------

def test_external_flow_excluded_from_return():
    attr = pf.attribute(opening_nav="100000", closing_nav="150000",
                        totals={cm.CAT_EXTERNAL: "50000"})
    assert D(attr["raw_nav_change"]) == D("50000")
    assert D(attr["cash_flow_adjusted_pnl"]) == D("0")


def test_unexplained_difference_is_preserved():
    """凑不平就如实保留，**不得强制归零**。"""
    attr = pf.attribute(opening_nav="100000", closing_nav="102000",
                        totals={cm.CAT_INCOME: "100"},
                        realized_pnl="500", unrealized_change="400")
    unexp = D(attr["unexplained_difference"])
    assert unexp == D("1000")                # 2000 - 500 - 400 - 100
    assert unexp != 0


def test_period_coverage_mismatch_is_reported(env):
    """已实现盈亏来自 Flex 全区间、未实现来自快照最近几天时，
    差额里混着「未测量的浮盈变化」，必须说清楚不是账目有误。"""
    cfg, d = env
    _seed_positions(d, 2, upls=[10, 20])     # 只有 7/1–7/2 有快照
    res = pf.compute_period(cfg, d, start="2026-06-01", end="2026-07-05",
                            account=fs.TEST_ACCOUNT)
    pc = res["period_coverage"]
    assert pc["uncovered_days"] and pc["uncovered_days"] > 20
    assert pc["aligned"] is False
    assert any("口径区间不一致" in x for x in res["data_limitations"])
    assert any("不代表账目有误" in x for x in res["data_limitations"])


def test_t_plus_one_latest_day_is_provisional(env):
    cfg, d = env
    flex_sync.run_flex_sync(cfg, d, now=NOW, client=StagedClient())
    cov = pf.flex_coverage(d, "2026-05-01", "2026-07-02")
    assert cov["source"] == pf.SRC_FLEX_PARTIAL
    assert cov["uncovered_days"] == 2
    assert cov["coverage_end"] == "2026-06-30"
    _seed_positions(d, 2, upls=[10, 20])
    res = pf.compute_period(cfg, d, start="2026-05-01", end="2026-07-02",
                            account=fs.TEST_ACCOUNT)
    assert any("暂定值" in x for x in res["data_limitations"])


# ---------------- 23–25. 复盘 / API / 安全 ----------------

def test_review_recalculation_versioned(env):
    from app import review_service as rs
    cfg, d = env
    assert rs.CALC_VERSION.startswith("review-v3-attrib")
    assert len(rs.LEGACY_VERSIONS) >= 2
    stats = rs.recompute_stats(cfg, d)
    assert "total" in stats and "note" in stats
    # 样本不足时不给结论
    assert stats["total"] == 0 and "样本不足" in stats["note"]


def test_new_apis_and_no_fake_zeros(synced):
    cfg, d = synced
    c = TestClient(create_app(cfg, d))
    for path in ("/api/v1/performance/reconciliation",
                 "/api/v1/performance/attribution",
                 "/api/v1/performance/unrealized-pnl"):
        body = c.get(path).json()
        assert set(body) == {"success", "data", "error", "request_id",
                             "generated_at"}
        assert body["success"] is True

    rec = c.get("/api/v1/performance/reconciliation").json()["data"]
    assert rec["currency_count"] == 2
    assert len(rec["balance_anchors"]) == fs.EXPECTED_BALANCE_MARKERS
    assert rec["conversion_rates_available"] == 3
    assert rec["snapshot_cross_validation"]["comparison_status"] == "insufficient_data"

    attr = c.get("/api/v1/performance/attribution").json()["data"]
    # 算不出来的项是 null，**不是 0**
    assert attr["attribution"]["fx_effect"] is None
    assert attr["attribution"]["unrealized_pnl_change"] is None
    assert attr["data_limitations"]

    up = c.get("/api/v1/performance/unrealized-pnl").json()["data"]
    assert up["change"]["change"] is None
    assert "至少需要 2 天" in up["change"]["reason"]

    html = c.get("/cashflow").text
    assert "不同币种的金额从不直接相加" in html
    assert "余额锚点" in html
    assert "不是经济流水" in html


def test_token_and_real_xml_never_leak(synced):
    cfg, d = synced
    import sqlite3
    conn = sqlite3.connect(cfg.db_path)
    for t in [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")]:
        try:
            blob = str(conn.execute(f"SELECT * FROM {t}").fetchall())
        except sqlite3.Error:
            continue
        assert TOKEN not in blob, t
        assert QUERY_ID not in blob, t
    c = TestClient(create_app(cfg, d))
    for path in ("/api/v1/performance/reconciliation",
                 "/api/v1/performance/attribution", "/cashflow"):
        text = c.get(path).text
        assert TOKEN not in text and QUERY_ID not in text
