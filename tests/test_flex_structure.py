"""Flex 报表结构解析与同步链路硬化的回归测试。

全部基于**按真实生产报表层级构造的虚构夹具**（tests/fixtures/flex_sample.py）。
真实报表留在服务器 secrets/ 且权限 600，不进 Git、不进测试目录。

这批测试守的是线上实际踩到的坑：
  · 外层是 FlexQueryResponse，不是 FlexStatements
  · Statement of Funds 容器叫 StmtFunds，不叫 StatementOfFunds
  · 同一笔成交在两个 section 各出现一次，且 tradeID ≠ transactionID
  · 「什么都没解析出来」绝不能报 success
"""
import datetime as dt
import logging
from decimal import Decimal as D

import pytest
from fastapi.testclient import TestClient

from app import cashflow_model as cm
from app import flex_parser as fp
from app import flex_sync
from app import flex_xml as fx
from app.db import Database
from app.flex import FetchResult, FlexError
from app.webapp import create_app
from fixtures import flex_sample as fs

NOW = dt.datetime(2026, 7, 29, 16, 0, tzinfo=dt.timezone.utc)
TOKEN = "987654321098765"
QUERY_ID = "123456789"
TZ = None


@pytest.fixture
def env(make_config):
    cfg = make_config(ib_account=fs.TEST_ACCOUNT, flex_token=TOKEN,
                      flex_query_id=QUERY_ID, flex_max_retries=1,
                      flex_poll_interval=0)
    d = Database(cfg.db_path)
    d.init_schema()
    return cfg, d


class StagedClient:
    """按阶段可编程的假客户端，模拟 SendRequest -> 轮询 -> GetStatement。"""

    def __init__(self, *, send=None, statements=None, send_error=None):
        self.send = send if send is not None else fs.send_request_ok()
        self.statements = list(statements or [fs.production_shaped()])
        self.send_error = send_error
        self.calls = {"send": 0, "get": 0}

    def fetch_detailed(self) -> FetchResult:
        self.calls["send"] += 1
        if self.send_error:
            raise self.send_error
        res = FetchResult()
        res.reference_code = fx.parse_send_request_response(self.send)
        res.send_request_ok = True
        for i, xml in enumerate(self.statements):
            self.calls["get"] += 1
            res.poll_count = i + 1
            res.get_statement_called = True
            if fx.looks_like_not_ready(xml):
                continue
            err = fx.error_of(xml)
            if err:
                raise FlexError(f"Flex 获取失败: {err}")
            res.xml = xml
            return res
        raise FlexError("Flex 报表生成超时")


# ---------------- 1–5. 外层包装与命名空间 ----------------

def test_flexqueryresponse_wrapper(make_config):
    xml = fs.production_shaped(wrapper="full")
    root = fx.parse_xml(xml)
    assert fx.local_name(root.tag) == "FlexQueryResponse"
    sts = fx.extract_flex_statements(root)
    assert len(sts) == 1
    assert fx.local_name(sts[0].tag) == "FlexStatement"


def test_flexstatements_root():
    root = fx.parse_xml(fs.production_shaped(wrapper="statements"))
    assert fx.local_name(root.tag) == "FlexStatements"
    assert len(fx.extract_flex_statements(root)) == 1


def test_bare_flexstatement_root():
    root = fx.parse_xml(fs.production_shaped(wrapper="bare"))
    assert fx.local_name(root.tag) == "FlexStatement"
    # 根就是 FlexStatement 时返回自身
    assert fx.extract_flex_statements(root) == [root]


def test_1019_warn_is_still_not_ready():
    """IBKR 生产会返回 Status=Warn + 1019，必须继续轮询。"""
    xml = ("<FlexStatementResponse><Status>Warn</Status>"
           "<ErrorCode>1019</ErrorCode>"
           "<ErrorMessage>Statement generation in progress.</ErrorMessage>"
           "</FlexStatementResponse>")
    assert fx.looks_like_not_ready(xml) is True
    assert "1019" in (fx.error_of(xml) or "")


def test_namespace_is_handled():
    """带命名空间时 root.iter('Trade') 一个都匹配不到，必须走局部名。"""
    import xml.etree.ElementTree as ET
    xml = fs.production_shaped(namespace=True)
    root = ET.fromstring(xml)
    assert root.tag.startswith("{")
    assert list(root.iter("Trade")) == []              # 朴素写法失效
    assert len(fx.find_descendants_by_local_name(root, "Trade")) == 2
    assert len(fx.extract_flex_statements(root)) == 1
    st = fx.validate_final_report(xml)
    assert st["has_namespace"] is True
    assert st["section_counts"]["trades"] == 2


def test_missing_flexstatement_raises():
    with pytest.raises(fx.FlexStructureError, match="FlexStatement"):
        fx.extract_flex_statements(fx.parse_xml(fs.empty_response()))
    # 绝不返回空列表让上游继续
    with pytest.raises(fx.FlexStructureError):
        fx.validate_final_report(fs.empty_response())


# ---------------- 6–7. section 解析 ----------------

def test_statement_of_funds_without_container(make_config):
    """真实报表的容器叫 StmtFunds，明细必须照样解析出来。"""
    cfg = make_config()
    xml = fs.production_shaped()
    assert "<StatementOfFunds>" not in xml
    assert "<StmtFunds>" in xml
    root = fx.parse_xml(xml)
    lines = fx.find_descendants_by_local_name(root, "StatementOfFundsLine")
    assert len(lines) == fs.EXPECTED_SECTIONS["statement_of_funds"]
    rows = fp.parse_statement_of_funds(xml, cfg.market_tz)
    # 7 行里 4 条是余额锚点（amount=0），不算现金流；
    # 剩下入金 + 买入 + 一条带流水号的调整 = 3
    assert len(rows) == 3


def test_section_counts_are_leaf_based(make_config):
    """容器不存在不代表没有数据 —— 计数必须按叶子记录。"""
    cfg = make_config()
    r = fp.parse_all(fs.production_shaped(), cfg.market_tz)
    for key, expected in fs.EXPECTED_SECTIONS.items():
        assert r["sections"][key] == expected, key
    assert all(v > 0 for v in fs.EXPECTED_SECTIONS.values())
    assert r["root_tag"] == "FlexQueryResponse"
    assert r["flex_statements"] == 1


def test_change_in_nav_is_official_total_daily_pnl(make_config):
    rows = fp.parse_change_in_nav(fs.production_shaped())
    day = next(x for x in rows if x["et_date"] == "2026-07-30")
    # MTM 8648.562 本身不含 -2.441458 佣金；总收益以 NAV 恒等式为准。
    assert D(day["mtm"]) == D("8648.562")
    assert D(day["commissions"]) == D("-2.441458")
    assert D(day["capital_movements"]) == 0
    assert D(day["total_pnl"]) == D("8646.120542")


def test_transfer_is_capital_movement_not_cash_flow(make_config):
    cfg = make_config()
    parsed = fp.parse_all(fs.production_shaped(), cfg.market_tz)
    assert len(parsed["transfers"]) == 1
    row = parsed["transfers"][0]
    assert row["symbol"] == "TESTX"
    assert row["quantity"] == "10"
    assert row["position_amount_base"] == "1000"
    assert "SENSITIVE-OTHER-ACCOUNT" not in row["raw_json"]
    assert all(x.broker_transaction_id != "TR001" for x in parsed["cash_flows"])


def test_report_period_spans_all_daily_flex_statements():
    xml = ("<FlexQueryResponse><FlexStatements>"
           "<FlexStatement fromDate='20260729' toDate='20260729'>"
           "<AccountInformation accountId='A'/></FlexStatement>"
           "<FlexStatement fromDate='20260730' toDate='20260730'>"
           "<AccountInformation accountId='A'/></FlexStatement>"
           "</FlexStatements></FlexQueryResponse>")
    assert fp.report_period(xml) == ("20260729", "20260730")


def test_cross_section_duplicates_are_removed(make_config):
    """同一笔成交在 Trade 与 SOF 各出现一次，且 tradeID ≠ transactionID。

    不统一去重键的话，交易现金流会翻倍（线上实测 43 → 86）。
    """
    cfg = make_config()
    xml = fs.production_shaped()
    # 夹具确实复现了「两个 ID 不同」这个真实特征
    root = fx.parse_xml(xml)
    trade = fx.find_descendants_by_local_name(root, "Trade")[0]
    assert trade.attrib["tradeID"] != trade.attrib["transactionID"]

    r = fp.parse_all(xml, cfg.market_tz)
    assert r["raw_row_count"] > len(r["cash_flows"])
    assert r["deduplicated_away"] > 0
    assert len(r["cash_flows"]) == fs.EXPECTED_DEDUPED_FLOWS
    ext = [x for x in r["cash_flows"] if cm.is_external(x.category)]
    assert len(ext) == fs.EXPECTED_EXTERNAL_FLOWS
    # 买入只出现一次
    buys = [x for x in r["cash_flows"] if x.subtype == "trade_buy"]
    assert len(buys) == 1
    # 去重键唯一
    keys = [x.as_db_row(NOW.isoformat())["cashflow_id"] for x in r["cash_flows"]]
    assert len(set(keys)) == len(keys)


def test_canonical_txn_id_prefers_transaction_id():
    assert fp.canonical_txn_id({"tradeID": "T1", "transactionID": "X1"}) == "X1"
    assert fp.canonical_txn_id({"tradeID": "T1"}) == "T1"
    assert fp.canonical_txn_id({}) is None


# ---------------- 8–12. 同步链路硬化 ----------------

def test_empty_reference_code_is_not_success(env):
    cfg, d = env
    c = StagedClient(send=fs.send_request_no_reference())
    res = flex_sync.run_flex_sync(cfg, d, now=NOW, client=c)
    assert res["status"] == flex_sync.ST_FAILED
    run = d.latest_flex_run()
    assert run["status"] == "failed"
    assert run["reference_code"] is None
    assert c.calls["get"] == 0            # 根本没走到 GetStatement


def test_send_request_failure_is_not_success(env):
    cfg, d = env
    c = StagedClient(send=fs.send_request_fail("1003", "Invalid token"))
    res = flex_sync.run_flex_sync(cfg, d, now=NOW, client=c)
    assert res["status"] == flex_sync.ST_FAILED
    assert c.calls["send"] == 1           # 凭证错误不重试


def test_report_without_flexstatement_is_not_success(env):
    cfg, d = env
    c = StagedClient(statements=[fs.empty_response()])
    res = flex_sync.run_flex_sync(cfg, d, now=NOW, client=c)
    assert res["status"] == flex_sync.ST_FAILED
    assert res["error_code"] == "INVALID_REPORT"
    run = d.latest_flex_run()
    assert run["records_received"] in (None, 0)
    # 关键：绝不是 success
    assert run["status"] != "success"


def test_parse_error_is_not_empty_success(env):
    cfg, d = env
    c = StagedClient(statements=["<FlexQueryResponse><broken"])
    res = flex_sync.run_flex_sync(cfg, d, now=NOW, client=c)
    assert res["status"] == flex_sync.ST_FAILED
    assert res["error_code"] in ("INVALID_REPORT", "PARSE_ERROR")


def test_not_ready_then_success_polls(env):
    """1019 不是失败，应继续轮询直到拿到报表。"""
    cfg, d = env
    c = StagedClient(statements=[fs.statement_not_ready(),
                                 fs.statement_not_ready(),
                                 fs.production_shaped()])
    res = flex_sync.run_flex_sync(cfg, d, now=NOW, client=c)
    assert res["status"] in ("success", "partial")
    assert res["poll_count"] == 3
    run = d.latest_flex_run()
    assert run["poll_count"] == 3
    assert run["reference_code"] == "REF123456"


def test_successful_sync_records_full_trace(env):
    cfg, d = env
    res = flex_sync.run_flex_sync(cfg, d, now=NOW, client=StagedClient())
    assert res["status"] in ("success", "partial")
    assert res["reference_code_present"] is True
    assert res["root_tag"] == "FlexQueryResponse"
    assert res["flex_statements"] == 1
    assert res["records_received"] == fs.EXPECTED_DEDUPED_FLOWS
    assert res["records_inserted"] == fs.EXPECTED_DEDUPED_FLOWS
    assert res["deduplicated_away"] > 0
    assert res["nav_daily_rows"] == 2
    assert res["transfer_rows"] == 1
    transfers = d.flex_transfers_series(fs.TEST_ACCOUNT)
    assert len(transfers) == 1
    assert transfers[0]["symbol"] == "TESTX"
    official = d.flex_nav_daily_series(fs.TEST_ACCOUNT)
    assert official[-1]["et_date"] == "2026-07-30"
    assert official[-1]["total_pnl"] == "8646.120542"
    run = d.latest_flex_run()
    assert run["report_from"] == "20260401" and run["report_to"] == "20260630"
    import json
    assert json.loads(run["section_counts"])["trades"] == 2


# ---------------- 13–14. 手动与定时同源 ----------------

def test_manual_and_scheduled_share_core(env):
    cfg, d = env
    a = flex_sync.run_flex_sync(cfg, d, now=NOW,
                                trigger=flex_sync.TRIGGER_MANUAL,
                                client=StagedClient())
    b = flex_sync.run_flex_sync(cfg, d, now=NOW,
                                trigger=flex_sync.TRIGGER_SCHEDULED,
                                client=StagedClient())
    assert a["trigger"] == "manual" and b["trigger"] == "scheduled"
    # 除 trigger 外，流程与结果一致
    for k in ("root_tag", "flex_statements", "records_received"):
        assert a[k] == b[k]
    # 第二次是幂等复跑，不再新增
    assert a["records_inserted"] == fs.EXPECTED_DEDUPED_FLOWS
    assert b["records_inserted"] == 0 and b["records_skipped"] > 0
    assert len(d.flex_runs_by_trigger("manual")) == 1
    assert len(d.flex_runs_by_trigger("scheduled")) == 1


def test_manual_api_calls_real_sync(env):
    """手动同步必须真的进 Flex 客户端，不能只写一条 run 记录。"""
    cfg, d = env
    c = TestClient(create_app(cfg, d))
    r = c.post("/api/v1/flex/sync")
    body = r.json()
    # 没有真实网络，必然失败 —— 但必须是「真的尝试过」的失败，
    # 而不是凭空返回 success
    assert body["success"] is False
    run = d.latest_flex_run()
    assert run is not None
    assert run["status"] == "failed"
    assert run["trigger_source"] == "manual"


# ---------------- 15–16. 幂等与凭证 ----------------

def test_repeated_sync_does_not_double_count(env):
    cfg, d = env
    flex_sync.run_flex_sync(cfg, d, now=NOW, client=StagedClient())
    _, total_after_first = d.query_cash_flows(limit=1000)
    ext_first = d.external_flows_between("2026-01-01T00:00:00+00:00",
                                         "2026-12-31T23:59:59+00:00")
    b = flex_sync.run_flex_sync(cfg, d, now=NOW, client=StagedClient())
    _, total_after_second = d.query_cash_flows(limit=1000)
    ext_second = d.external_flows_between("2026-01-01T00:00:00+00:00",
                                          "2026-12-31T23:59:59+00:00")
    assert total_after_second == total_after_first       # 不翻倍
    assert b["records_inserted"] == 0
    assert b["records_skipped"] > 0
    assert len(ext_second) == len(ext_first)
    assert (sum(D(r["amount"]) for r in ext_second)
            == sum(D(r["amount"]) for r in ext_first))   # 聚合不翻倍


def test_token_never_leaks(env, caplog):
    cfg, d = env
    with caplog.at_level(logging.DEBUG):
        flex_sync.run_flex_sync(cfg, d, now=NOW, client=StagedClient())
        flex_sync.run_flex_sync(cfg, d, now=NOW, client=StagedClient(
            send_error=FlexError(f"failed ?t={TOKEN}&q={QUERY_ID}")))
    assert TOKEN not in caplog.text and QUERY_ID not in caplog.text

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
    for path in ("/api/v1/flex/status", "/api/v1/performance/cash-flows"):
        assert TOKEN not in c.get(path).text
        assert QUERY_ID not in c.get(path).text


def test_data_quality_not_ok_without_period(env):
    """报表区间为空时不得标 ok —— 我们不知道这份数据覆盖了哪一段时间。"""
    cfg, d = env
    flex_sync.run_flex_sync(cfg, d, now=NOW, client=StagedClient())
    d.update_flex_run(d.latest_flex_run()["run_id"],
                      report_from=None, report_to=None)
    assert flex_sync.status(cfg, d)["data_quality"] != "ok"


# ---------------- 17–18. 覆盖判定 ----------------

def test_coverage_partial_when_report_lags(env):
    """Flex 是 T+1，最后 1–2 天永远不在报表里。

    要求完整覆盖会让来源永远是 none，整个接入白做；但也不能假装全覆盖。
    所以标 partial 并说明还差几天。
    """
    from app import performance as pf
    cfg, d = env
    flex_sync.run_flex_sync(cfg, d, now=NOW, client=StagedClient())
    # 夹具报表覆盖 2026-04-01 → 2026-06-30
    full = pf.flex_coverage(d, "2026-05-01", "2026-06-30")
    assert full["source"] == pf.SRC_FLEX and full["uncovered_days"] == 0

    partial = pf.flex_coverage(d, "2026-05-01", "2026-07-02")
    assert partial["source"] == pf.SRC_FLEX_PARTIAL
    assert partial["uncovered_days"] == 2
    assert partial["coverage_end"] == "2026-06-30"

    # 缺口过大：不能再当权威
    stale = pf.flex_coverage(d, "2026-05-01", "2026-08-30")
    assert stale["source"] == pf.SRC_NONE

    # 区间开头未覆盖：那是真正的盲区，一律 none
    blind = pf.flex_coverage(d, "2026-01-01", "2026-06-30")
    assert blind["source"] == pf.SRC_NONE


def test_verified_no_flows_differs_from_no_data(env):
    """「有数据且证明没发生」与「没有数据」必须区分。"""
    from app import performance as pf
    cfg, d = env
    flex_sync.run_flex_sync(cfg, d, now=NOW, client=StagedClient())
    # 夹具里的入金在 2026-04-02，取一个之后的、有覆盖的区间
    res = pf.compute_period(cfg, d, start="2026-05-01", end="2026-06-30",
                            account=fs.TEST_ACCOUNT)
    assert res["cash_flow_source"] == pf.SRC_FLEX
    assert D(res["external_cash_flow"] or 0) == D("0")

    # 没有任何 Flex 记录的库：来源必须是 none
    empty = Database(str(cfg.db_path) + "-empty")
    empty.init_schema()
    assert pf.flex_coverage(empty, "2026-05-01", "2026-06-30")["source"] == pf.SRC_NONE
