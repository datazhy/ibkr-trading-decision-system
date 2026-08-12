"""决策识别与分类（order / execution / decision 三种计数不可混用）。

对应验收项 1-5。真实场景锚点：DRAM 一买一卖 = 2 笔成交、2 个 permId、
2 次主动决策，但只有 BUY 是风险增加 —— R4=1，R5=2。
"""
from decimal import Decimal

import pytest

from app.decisions import (
    ADD, CLOSE, OPEN, REDUCE, REVERSAL, UNKNOWN, classification_complete,
    classify_decision, count_decisions, decision_key, group_decisions,
    is_discretionary, kind_summary, risk_increasing_by_symbol, signed_shares,
)


def _ex(exec_id, symbol, side, shares, perm_id, ts, *, liquidation=0,
        order_ref=None, decision_id=None):
    return {
        "exec_id": exec_id, "symbol": symbol, "side": side, "shares": Decimal(str(shares)),
        "perm_id": perm_id, "execution_time": ts, "liquidation": liquidation,
        "order_ref": order_ref, "decision_id": decision_id,
    }


def _lookup(mapping):
    return lambda sym, ts: mapping.get(sym)


def _et(ts):
    return ts[:10]


# ---------------- decision_key ----------------

def test_decision_key_prefers_decision_id():
    assert decision_key({"decision_id": "D1", "perm_id": 999}) == "D1"


def test_decision_key_falls_back_to_perm_id():
    assert decision_key({"decision_id": None, "perm_id": 1357395055}) == "1357395055"


def test_decision_key_none_when_both_missing():
    assert decision_key({"decision_id": None, "perm_id": None}) is None


# ---------------- 分类（按有符号持仓，不靠买卖方向猜） ----------------

def test_classify_open_close():
    assert classify_decision(0, 100) == OPEN
    assert classify_decision(100, 0) == CLOSE


def test_classify_add_reduce_long():
    assert classify_decision(100, 150) == ADD
    assert classify_decision(150, 100) == REDUCE


def test_classify_add_reduce_short():
    """空头：BUY 是减仓。绝不能按买卖方向判类型。"""
    assert classify_decision(-100, -150) == ADD      # 卖出加空
    assert classify_decision(-150, -100) == REDUCE   # 买入减空


def test_classify_reversal():
    assert classify_decision(100, -50) == REVERSAL
    assert classify_decision(-100, 50) == REVERSAL


def test_classify_unknown_when_flat_to_flat():
    assert classify_decision(0, 0) == UNKNOWN


def test_signed_shares_direction():
    assert signed_shares({"side": "BUY", "shares": Decimal("10")}) == Decimal("10")
    assert signed_shares({"side": "SELL", "shares": Decimal("10")}) == Decimal("-10")


# ---------------- 非主动决策的排除 ----------------

def test_liquidation_is_not_discretionary():
    assert is_discretionary(_ex("e1", "X", "SELL", 1, 1, "t", liquidation=1)) is False


def test_corporate_action_is_not_discretionary():
    for ref in ("CORP_ACTION", "EXERCISE_AUTO", "ASSIGN_1", "ADJUST_X", "SPLIT_2FOR1", "DIV_REINVEST"):
        assert is_discretionary(_ex("e", "X", "BUY", 1, 1, "t", order_ref=ref)) is False


def test_normal_manual_trade_is_discretionary():
    assert is_discretionary(_ex("e1", "X", "BUY", 1, 1, "t")) is True


# ---------------- 验收 1：DRAM 真实场景 ----------------

def test_dram_buy_then_sell_real_case():
    """线上真实成交：BUY 1 @55.87（permId 1357395055）、SELL 1 @55.90（permId 1357395068）。

    execution=2、decision=2、R4(风险增加)=1、R5(主动决策)=2。
    """
    rows = [
        _ex("0000f9be.6a586681.01.01", "DRAM", "BUY", 1, 1357395055, "2026-07-16T03:18:36+00:00"),
        _ex("0002423f.6a5813ac.01.01", "DRAM", "SELL", 1, 1357395068, "2026-07-16T03:36:19+00:00"),
    ]
    # 买入前持仓 3.9，卖出前 4.9
    def lookup(sym, ts):
        return Decimal("3.9") if ts.startswith("2026-07-16T03:18") else Decimal("4.9")

    decs = group_decisions(rows, pre_qty_lookup=lookup, et_date_of=_et)
    assert len(rows) == 2                       # execution 数
    assert count_decisions(decs) == 2           # decision 数（R5）
    assert kind_summary(decs) == {ADD: 1, REDUCE: 1}
    r4 = risk_increasing_by_symbol(decs, "2026-07-16")
    assert r4 == {"DRAM": 1}                    # 只有 BUY 是风险增加 -> R4=1，不是 2


# ---------------- 验收 2：同一 permId 的多笔部分成交 ----------------

def test_partial_fills_of_same_perm_id_count_as_one_decision():
    rows = [
        _ex("p.a.01.01", "MU", "BUY", 30, 777, "2026-07-16T14:00:00+00:00"),
        _ex("p.b.02.01", "MU", "BUY", 30, 777, "2026-07-16T14:00:05+00:00"),
        _ex("p.c.03.01", "MU", "BUY", 40, 777, "2026-07-16T14:00:09+00:00"),
    ]
    decs = group_decisions(rows, pre_qty_lookup=_lookup({"MU": Decimal("0")}), et_date_of=_et)
    assert len(rows) == 3                       # execution 数 = 3
    assert count_decisions(decs) == 1           # decision 数 = 1
    assert decs[0].executions == 3
    assert decs[0].signed_shares == Decimal("100")
    assert decs[0].kind == OPEN                 # 0 -> 100
    assert risk_increasing_by_symbol(decs, "2026-07-16") == {"MU": 1}


# ---------------- 验收 3：SELL 只减仓/平仓 ----------------

def test_sell_reduces_never_counts_to_r4():
    rows = [_ex("s1", "MU", "SELL", 50, 888, "2026-07-16T15:00:00+00:00")]
    decs = group_decisions(rows, pre_qty_lookup=_lookup({"MU": Decimal("100")}), et_date_of=_et)
    assert decs[0].kind == REDUCE
    assert risk_increasing_by_symbol(decs, "2026-07-16") == {}   # R4 不计
    assert count_decisions(decs) == 1                            # R5 计


def test_sell_closes_never_counts_to_r4():
    rows = [_ex("s1", "MU", "SELL", 100, 889, "2026-07-16T15:00:00+00:00")]
    decs = group_decisions(rows, pre_qty_lookup=_lookup({"MU": Decimal("100")}), et_date_of=_et)
    assert decs[0].kind == CLOSE
    assert risk_increasing_by_symbol(decs, "2026-07-16") == {}
    assert count_decisions(decs) == 1


# ---------------- 验收 4：无法还原成交前持仓 ----------------

def test_unknown_when_pre_qty_unavailable():
    rows = [_ex("x1", "MU", "BUY", 10, 900, "2026-07-16T15:00:00+00:00")]
    decs = group_decisions(rows, pre_qty_lookup=lambda s, t: None, et_date_of=_et)
    assert decs[0].kind == UNKNOWN
    assert decs[0].pre_qty is None
    # 绝不能因为「是 BUY」就当成风险增加
    assert risk_increasing_by_symbol(decs, "2026-07-16") == {}
    assert classification_complete(decs, "2026-07-16") is False
    assert count_decisions(decs) == 1           # R5 仍可计数


# ---------------- 验收 5：方向翻转 ----------------

def test_reversal_is_marked_not_silently_added():
    rows = [_ex("r1", "MU", "SELL", 150, 901, "2026-07-16T15:00:00+00:00")]
    decs = group_decisions(rows, pre_qty_lookup=_lookup({"MU": Decimal("100")}), et_date_of=_et)
    assert decs[0].kind == REVERSAL             # 100 -> -50，绝不是普通 ADD
    assert kind_summary(decs) == {REVERSAL: 1}
    # 翻转建立了反向新仓，属于风险增加，但类型独立可辨
    assert risk_increasing_by_symbol(decs, "2026-07-16") == {"MU": 1}


# ---------------- 其它 ----------------

def test_liquidation_excluded_from_both_counts():
    rows = [
        _ex("a", "MU", "BUY", 10, 1, "2026-07-16T14:00:00+00:00"),
        _ex("b", "MU", "SELL", 10, 2, "2026-07-16T15:00:00+00:00", liquidation=1),
    ]
    decs = group_decisions(rows, pre_qty_lookup=_lookup({"MU": Decimal("0")}), et_date_of=_et)
    assert count_decisions(decs) == 1           # 强平不是你的决策


def test_missing_perm_id_is_dropped_not_guessed():
    rows = [_ex("a", "MU", "BUY", 10, None, "2026-07-16T14:00:00+00:00")]
    decs = group_decisions(rows, pre_qty_lookup=_lookup({"MU": Decimal("0")}), et_date_of=_et)
    assert decs == []                           # 无法归组，宁可不计也不错计


def test_et_date_boundary_not_utc():
    """日期边界用美东交易日：UTC 07-16 03:18 = ET 07-15 23:18。"""
    rows = [_ex("a", "MU", "BUY", 10, 1, "2026-07-16T03:18:36+00:00")]
    decs = group_decisions(rows, pre_qty_lookup=_lookup({"MU": Decimal("0")}),
                           et_date_of=lambda ts: "2026-07-15")
    assert decs[0].et_date == "2026-07-15"
    assert risk_increasing_by_symbol(decs, "2026-07-16") == {}    # 不属于 07-16
    assert risk_increasing_by_symbol(decs, "2026-07-15") == {"MU": 1}


# ---------------- R6：按决策合并已实现盈亏 ----------------
# 线上真实案例：清仓 KMEM 1786 股被拆成 7 笔，每笔 <2% 阈值，R6 静默；
# 但这是同一个决策，合计 -2192.67 = 净资产 5.4%。

from app.decisions import merge_realized_by_decision

_KMEM_FILLS = [
    ("-613.832905", 500), ("-245.853162", 200), ("-122.926581", 100),
    ("-121.926602", 100), ("-121.926602", 100), ("-737.559486", 600),
    ("-228.643441", 186),
]


def _kmem_rows(perm_id=1357400000):
    """7 笔部分成交，同一个 permId（同一次清仓决策）。"""
    return [
        {"exec_id": f"0000k{i}.aaaa.01.01", "symbol": "KMEM", "side": "SELL",
         "shares": Decimal(str(qty)), "perm_id": perm_id, "decision_id": None,
         "execution_time": f"2026-07-16T15:3{5 + i // 3}:{i:02d}+00:00",
         "realized_pnl": Decimal(r), "liquidation": 0, "order_ref": None}
        for i, (r, qty) in enumerate(_KMEM_FILLS)
    ]


def test_merge_realized_combines_partial_fills_of_one_decision():
    merged = merge_realized_by_decision(_kmem_rows())
    assert len(merged) == 1                       # 7 笔 -> 1 次认亏
    assert merged[0]["executions"] == 7
    assert merged[0]["realized"] == Decimal("-2192.668779")
    assert merged[0]["symbol"] == "KMEM"


def test_merged_loss_crosses_r6_threshold():
    """合并前每笔都逃过 2%，合并后 5.4% —— 这正是漏洞所在。"""
    equity = Decimal("40773")
    rows = _kmem_rows()
    worst_single = min(Decimal(r) for r, _ in _KMEM_FILLS)
    assert abs(worst_single) / equity < Decimal("0.02")        # 单笔最大 1.82%，不触发
    merged = merge_realized_by_decision(rows)[0]
    assert abs(merged["realized"]) / equity > Decimal("0.05")  # 合并后 5.4%，必须触发


def test_merge_keeps_decisions_separate():
    """不同决策不合并 —— 两次独立认亏就是两次。"""
    rows = _kmem_rows(perm_id=1) + [
        {"exec_id": "0000d1.bbbb.01.01", "symbol": "DRAM", "side": "SELL",
         "shares": Decimal("1"), "perm_id": 2, "decision_id": None,
         "execution_time": "2026-07-16T16:00:00+00:00",
         "realized_pnl": Decimal("-1.26"), "liquidation": 0, "order_ref": None},
    ]
    merged = merge_realized_by_decision(rows)
    assert len(merged) == 2
    assert {m["symbol"] for m in merged} == {"KMEM", "DRAM"}


def test_merge_uses_decision_key_as_ref_id():
    """ref_id = decision_key -> R6 的 dedup_key 稳定，同一决策只触发一次冷静期。"""
    merged = merge_realized_by_decision(_kmem_rows(perm_id=999))
    assert merged[0]["ref_id"] == "999"


def test_merge_skips_rows_without_realized():
    rows = _kmem_rows() + [
        {"exec_id": "0000b1.cccc.01.01", "symbol": "DRAM", "side": "BUY",
         "shares": Decimal("100"), "perm_id": 3, "decision_id": None,
         "execution_time": "2026-07-16T15:40:00+00:00",
         "realized_pnl": None, "liquidation": 0, "order_ref": None},
    ]
    merged = merge_realized_by_decision(rows)
    assert len(merged) == 1                       # 开仓无已实现，不参与


def test_merge_prefers_decision_id_over_perm_id():
    rows = [dict(r, decision_id="D1") for r in _kmem_rows()]
    assert merge_realized_by_decision(rows)[0]["ref_id"] == "D1"
