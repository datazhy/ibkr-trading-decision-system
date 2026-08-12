"""已实现盈亏对账 + 三种口径声明。

真实锚点（线上 DRAM 成交）：
  BUY  1 @ 55.87  佣金 0.558703
  SELL 1 @ 55.90  佣金 0.56035   realized_pnl = -1.260146   持仓均价 56.60
  IBKR 口径：(55.90 − 56.60) − 0.56035 = -1.26035 ≈ -1.260146（差 0.0002 = 均价精度）
  错误公式：0.03 − 1.119053(双边佣金) = -1.089053  ✗（买入佣金被扣两次）
  上一版历史页显示的 -1.82 = -0.70(估算毛) − 1.12(双边佣金)，同样是重复扣减。
"""
from decimal import Decimal

import pytest

from app.reconcile import (
    INSUFFICIENT, MATCHED, MISMATCH, PNL_BASES, PNL_BASES_WARNING, check_day_pnl,
    check_unrealized, reconcile_realized, sum_commission, sum_realized,
)

BUY = {"exec_id": "0000f9be.6a586681.01.01", "symbol": "DRAM", "side": "BUY",
       "commission": Decimal("0.558703"), "realized_pnl": None}
SELL = {"exec_id": "0002423f.6a5813ac.01.01", "symbol": "DRAM", "side": "SELL",
        "commission": Decimal("0.56035"), "realized_pnl": Decimal("-1.260146")}


# ---------------- 汇总 ----------------

def test_sum_realized_uses_broker_value_only():
    total, counted, missing = sum_realized([BUY, SELL])
    assert total == Decimal("-1.260146")   # 只认券商给的数
    assert counted == 1 and missing == 1   # 买入腿没有 realized（未平仓），正常


def test_sum_realized_none_when_no_broker_values():
    total, counted, missing = sum_realized([BUY])
    assert total is None and counted == 0 and missing == 1


def test_sum_commission_is_information_only():
    """佣金合计仅作成本信息展示，不参与盈亏计算。"""
    assert sum_commission([BUY, SELL]) == Decimal("1.119053")


# ---------------- 对账 ----------------

def test_reconcile_matched_real_case():
    r = reconcile_realized(broker_value=Decimal("-1.260146"), execution_rows=[BUY, SELL])
    assert r.status == MATCHED and r.matched is True
    assert r.broker_value == Decimal("-1.260146")
    assert r.executions_value == Decimal("-1.260146")
    assert r.difference == Decimal("0")


def test_reconcile_within_tolerance():
    r = reconcile_realized(broker_value=Decimal("-1.26"), execution_rows=[BUY, SELL])
    assert r.status == MATCHED             # 0.000146 差异在 0.01 容差内


def test_reconcile_mismatch_is_not_disguised():
    r = reconcile_realized(broker_value=Decimal("-5.00"), execution_rows=[BUY, SELL])
    assert r.status == MISMATCH and r.matched is False
    assert r.difference == Decimal("-3.739854")
    assert "以账户级为准" in r.note        # 摆出权威来源与差异，不装作已核对


def test_reconcile_insufficient_without_broker_value():
    r = reconcile_realized(broker_value=None, execution_rows=[BUY, SELL])
    assert r.status == INSUFFICIENT and r.difference is None
    assert r.executions_value == Decimal("-1.260146")   # 仍报出已有的一侧


def test_reconcile_insufficient_without_execution_realized():
    r = reconcile_realized(broker_value=Decimal("-1.26"), execution_rows=[BUY])
    assert r.status == INSUFFICIENT
    assert r.executions_missing_realized == 1


def test_reconcile_counts_missing_realized():
    r = reconcile_realized(broker_value=Decimal("-1.260146"), execution_rows=[BUY, SELL])
    assert r.executions_counted == 1 and r.executions_missing_realized == 1


# ---------------- 错误公式必须消失 ----------------

def test_wrong_formula_result_is_never_produced():
    """回归：-1.82 / -1.089053 这类「毛 − 双边佣金」的结果不得再出现。"""
    r = reconcile_realized(broker_value=Decimal("-1.260146"), execution_rows=[BUY, SELL])
    for bad in (Decimal("-1.82"), Decimal("-1.089053"), Decimal("-0.70")):
        assert r.executions_value != bad
        assert r.broker_value != bad


def test_module_exposes_no_gross_minus_commission_helper():
    """本模块不提供「净 = 毛 − 双边佣金」的实现，杜绝再被调用。"""
    import app.reconcile as m
    names = [n.lower() for n in dir(m)]
    assert not any("gross" in n for n in names)


# ---------------- 三种口径声明 ----------------

def test_pnl_bases_declare_three_distinct_scopes():
    keys = {b["key"] for b in PNL_BASES}
    assert keys == {"daily", "realized", "unrealized"}
    for b in PNL_BASES:
        assert b["scope"] and b["content"] and b["source"] and b["reset"]


def test_pnl_bases_warning_forbids_addition():
    assert "不能相加" in PNL_BASES_WARNING


def test_realized_basis_documents_commission_treatment():
    realized = next(b for b in PNL_BASES if b["key"] == "realized")
    assert "买入佣金含在持仓成本里" in realized["content"]


# ---------------- 持仓日内盈亏 vs 账户 DailyPnL ----------------

def test_day_pnl_check_matched_real_case():
    """实测：逐持仓之和与账户级精确吻合。"""
    c = check_day_pnl(
        position_day_pnls=[Decimal("23.99996566772461"), Decimal("-8.070097619628909"),
                           Decimal("-678.6795912170419"), Decimal("-15.750115356445349")],
        account_daily=Decimal("-678.4998385253915"),
    )
    assert c.status == MATCHED and c.covered == 4 and c.missing == 0
    assert abs(c.difference) < Decimal("0.0001")


def test_day_pnl_check_mismatch_surfaced():
    c = check_day_pnl(position_day_pnls=[Decimal("10")], account_daily=Decimal("-678"))
    assert c.status == MISMATCH and c.difference == Decimal("-688")


def test_day_pnl_check_counts_missing():
    c = check_day_pnl(position_day_pnls=[Decimal("10"), None], account_daily=Decimal("10"))
    assert c.covered == 1 and c.missing == 1


def test_day_pnl_check_insufficient():
    c = check_day_pnl(position_day_pnls=[None], account_daily=Decimal("10"))
    assert c.status == INSUFFICIENT


# ---------------- P0-4：持仓浮盈 vs 账户未实现（同口径） ----------------
# 原先拿持仓「日内盈亏合计」跟账户 DailyPnL 比：后者含已实现，前者没有已平仓的标的，
# 任何有平仓的交易日都必然红字（线上 -317 vs -3032 差 2715），数据全对、口径全错。

def test_check_unrealized_is_same_basis():
    """线上真实数据：两者来自 IBKR 不同服务，有稳定精度差，属正常。"""
    c = check_unrealized(position_upls=[Decimal("176.95"), Decimal("-1065.97"),
                                        Decimal("-672.63")],
                         account_unrealized=Decimal("-1983.9196863704665"),
                         realized_today=Decimal("-1.26"))
    assert c.positions_sum == Decimal("-1561.65")
    assert c.status == MATCHED                       # 27% 差异在相对容差内
    assert "不同服务" in c.note


def test_check_unrealized_flags_missing_position():
    """真问题：持仓表漏了一大块 -> 差异过大 -> 必须报出来。"""
    c = check_unrealized(position_upls=[Decimal("100")],
                         account_unrealized=Decimal("-5000"))
    assert c.status == MISMATCH
    assert "可能有持仓未纳入统计" in c.note


def test_realized_is_information_only_not_in_comparison():
    """当日已实现不参与对照 —— 它不在持仓浮盈里。"""
    c = check_unrealized(position_upls=[Decimal("-100")],
                         account_unrealized=Decimal("-100"),
                         realized_today=Decimal("-2193.93"))
    assert c.status == MATCHED                       # 已实现不影响对照结论
    assert c.realized_today == Decimal("-2193.93")   # 但要展示


def test_check_unrealized_insufficient():
    c = check_unrealized(position_upls=[None], account_unrealized=Decimal("-100"))
    assert c.status == INSUFFICIENT


def test_check_unrealized_zero_base():
    c = check_unrealized(position_upls=[Decimal("0")], account_unrealized=Decimal("0"))
    assert c.status == MATCHED
