"""2A：executions 解析 + 幂等 + correction + commission 两阶段（纯函数/DB，无需 gateway）。"""
from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace as NS

import pytest

from app.db import Database
from app.ibkr_client import parse_ib_execution, split_exec_id

T = datetime(2026, 7, 16, 14, 30, tzinfo=timezone.utc)


def mk_fill(exec_id="0000e0d5.68b3b1a2.01.01", side="SLD", shares=100, price=50.0,
            cum=100, avg=50.0, commission=1.05, realized=-5000.0, has_comm=True,
            symbol="MU", conid=9999, liquidation=0, perm_id=123456):
    cr = NS(execId=exec_id if has_comm else "", commission=commission,
            currency="USD", realizedPNL=realized)
    return NS(
        contract=NS(symbol=symbol, conId=conid, secType="STK", currency="USD"),
        execution=NS(execId=exec_id, time=T, acctNumber="U1", exchange="NASDAQ",
                     side=side, shares=shares, price=price, permId=perm_id, clientId=0,
                     orderId=7, liquidation=liquidation, cumQty=cum, avgPrice=avg,
                     orderRef="ref1", lastLiquidity=1),
        commissionReport=cr, time=T)


# ---------------- execId / correction 解析 ----------------

def test_split_exec_id():
    assert split_exec_id("0000e0d5.68b3b1a2.01.01") == ("0000e0d5.68b3b1a2.01", 1)
    assert split_exec_id("0000e0d5.68b3b1a2.01.02") == ("0000e0d5.68b3b1a2.01", 2)


def test_split_exec_id_no_suffix():
    assert split_exec_id("abc") == ("abc", 0)


# ---------------- 解析 ----------------

def test_parse_execution_full():
    r = parse_ib_execution(mk_fill())
    assert r["exec_id"] == "0000e0d5.68b3b1a2.01.01"
    assert r["correction_root"] == "0000e0d5.68b3b1a2.01" and r["correction_seq"] == 1
    assert r["side"] == "SELL" and r["shares"] == Decimal("100")
    assert r["cumulative_qty"] == Decimal("100") and r["average_price"] == Decimal("50.0")
    assert r["conid"] == 9999 and r["sec_type"] == "STK" and r["currency"] == "USD"
    assert r["commission"] == Decimal("1.05") and r["commission_currency"] == "USD"
    assert r["realized_pnl"] == Decimal("-5000.0")
    assert r["data_status"] == "READY"
    assert r["execution_time"].startswith("2026-07-16T14:30")


def test_parse_execution_pending_commission():
    """commissionReport 未到 → PENDING_COMMISSION，realized 为 None（R6 不得判定）。"""
    r = parse_ib_execution(mk_fill(has_comm=False))
    assert r["data_status"] == "PENDING_COMMISSION"
    assert r["commission"] is None and r["realized_pnl"] is None


def test_parse_execution_zero_commission_is_valid():
    r = parse_ib_execution(mk_fill(commission=0.0))
    assert r["commission"] == Decimal("0") and r["data_status"] == "READY"


def test_parse_execution_sentinel_realized_ignored():
    r = parse_ib_execution(mk_fill(realized=1.7976931348623157e308))
    assert r["realized_pnl"] is None   # 哨兵=非平仓


def test_parse_execution_zero_realized_ignored():
    assert parse_ib_execution(mk_fill(realized=0.0))["realized_pnl"] is None


def test_parse_execution_liquidation_flag():
    """被动强平标记留存（主动/被动区分用，本阶段仅存字段）。"""
    assert parse_ib_execution(mk_fill(liquidation=1))["liquidation"] == 1
    assert parse_ib_execution(mk_fill(liquidation=0))["liquidation"] == 0


# ---------------- DB：幂等 / correction / R6 水位 ----------------

@pytest.fixture
def db(tmp_path):
    d = Database(tmp_path / "t.db")
    d.init_schema()
    return d


def test_upsert_idempotent(db):
    row = parse_ib_execution(mk_fill())
    db.upsert_executions([row])
    db.upsert_executions([row])          # 重复同步
    with db.connect() as c:
        assert c.execute("select count(*) c from executions").fetchone()["c"] == 1


def test_late_commission_upgrades_status(db):
    """commission 迟到：先 PENDING（R6 不判），后到 → READY + realized 补齐。"""
    db.upsert_executions([parse_ib_execution(mk_fill(has_comm=False))])
    assert db.pending_r6_executions() == []          # 未就绪，R6 不判
    db.upsert_executions([parse_ib_execution(mk_fill(has_comm=True))])
    pend = db.pending_r6_executions()
    assert len(pend) == 1 and pend[0]["realized_pnl"] == "-5000.0"


def test_correction_supersedes_old(db):
    """修正执行：同 root 取最大 seq 为 current，旧的置 0，不重复累计。"""
    db.upsert_executions([parse_ib_execution(mk_fill(exec_id="root.01", realized=-5000.0))])
    db.upsert_executions([parse_ib_execution(
        mk_fill(exec_id="root.02", realized=-4000.0, shares=80))])
    with db.connect() as c:
        rows = {r["exec_id"]: r["is_current"] for r in c.execute(
            "select exec_id, is_current from executions")}
    assert rows["root.01"] == 0 and rows["root.02"] == 1
    pend = db.pending_r6_executions()
    assert len(pend) == 1 and pend[0]["exec_id"] == "root.02"   # 只用修正后数据


def test_r6_state_prevents_reprocessing(db):
    db.upsert_executions([parse_ib_execution(mk_fill())])
    assert len(db.pending_r6_executions()) == 1
    db.mark_r6_done(["0000e0d5.68b3b1a2.01.01"])
    assert db.pending_r6_executions() == []      # 已判过不再出现


def test_pending_excludes_non_loss(db):
    db.upsert_executions([parse_ib_execution(mk_fill(realized=3000.0))])   # 盈利
    assert db.pending_r6_executions() == []      # R6 只关心亏损


def test_partial_fills_accumulate(db):
    """部分成交：同一订单的多笔 fill（fill 序号不同 → 不同 root），各自独立入库、都有效。

    真实 execId 结构 prefix.hash.<fill序号>.<修正序号>：
      部分成交 → ...01.01 / ...02.01（不同 root）
      修正     → ...01.01 / ...01.02（同 root）
    """
    db.upsert_executions([
        parse_ib_execution(mk_fill(exec_id="0000e0d5.68b3b1a2.01.01",
                                   shares=40, cum=40, realized=-2000.0)),
        parse_ib_execution(mk_fill(exec_id="0000e0d5.68b3b1a2.02.01",
                                   shares=60, cum=100, realized=-3000.0)),
    ])
    rows = db.pending_r6_executions()
    assert len(rows) == 2                      # 两笔都是有效执行，不是互为修正
    with db.connect() as c:
        cums = [r["cumulative_qty"] for r in c.execute(
            "select cumulative_qty from executions order by exec_id")]
    assert cums == ["40", "100"]               # cumQty 反映累计进度


def test_partial_fill_vs_correction_root_distinction(db):
    """回归：部分成交(不同 fill 序号)不得被误判为互为修正。"""
    from app.ibkr_client import split_exec_id
    assert split_exec_id("0000e0d5.68b3b1a2.01.01")[0] != \
           split_exec_id("0000e0d5.68b3b1a2.02.01")[0]     # 部分成交 → 不同 root
    assert split_exec_id("0000e0d5.68b3b1a2.01.01")[0] == \
           split_exec_id("0000e0d5.68b3b1a2.01.02")[0]     # 修正 → 同 root
