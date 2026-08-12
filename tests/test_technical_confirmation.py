"""P0-1 回归：技术确认态必须由报价自身时段决定，扩展时段绝不算收盘确认。

背景：三处调用点（webtechnical/webdata/event_service）原先各自
`intraday = session == REGULAR`，把盘前/盘后/夜盘塌进「收盘确认」桶，使低流动性的
扩展时段价格以「收盘突破 / CLOSE_CONFIRMED」呈现，误导加仓判断。生产复现：
2026-08-07 POST 时段 RKLB 83.85 > RTH 20 日高点 82.54 被标「收盘突破」。
"""
from decimal import Decimal

import pytest

from app import technical as T


def _bars(high="82.54", n=25):
    """构造 n 根日线，20 日高点 = high。"""
    return [{"date": f"2026-07-{d:02d}", "o": "80", "h": high, "l": "79",
             "c": "81", "v": 1_000_000} for d in range(1, n + 1)]


# ---------------- confirmation_basis 口径 ----------------

@pytest.mark.parametrize("session,expected", [
    ("REGULAR", T.INTRADAY_PENDING),
    ("PRE", T.EXTENDED_HOURS_PENDING),
    ("POST", T.EXTENDED_HOURS_PENDING),
    ("OVERNIGHT", T.EXTENDED_HOURS_PENDING),
    ("CLOSED", T.CLOSE_CONFIRMED),
    (None, T.CLOSE_CONFIRMED),
])
def test_confirmation_basis_by_session(session, expected):
    assert T.confirmation_basis(session) == expected


@pytest.mark.parametrize("source", [
    "FUTU_AFTER_HOURS", "FUTU_PRE_MARKET", "FUTU_OVERNIGHT"])
def test_extended_hours_source_overrides_wall_clock(source):
    """报价来源自报扩展时段时，即使墙钟误判 CLOSED，也必须 pending。

    这是防墙钟与报价不一致的兜底：生产快照时点若落在 CLOSED 判定边界，
    仍不能把一条 AFTER_HOURS 报价当成收盘确认。
    """
    assert T.confirmation_basis("CLOSED", quote_source=source) == T.EXTENDED_HOURS_PENDING


# ---------------- 突破标签与确认态 ----------------

def test_post_session_breakout_is_not_close_confirmed():
    """生产复现：POST 时段 price 83.85 > 20 日高点 82.54。"""
    conf = T.confirmation_basis("POST", price_source="LAST",
                                quote_source="FUTU_AFTER_HOURS")
    card = T.compute_symbol("RKLB", _bars(), Decimal("83.85"), confirmation=conf)
    assert card["confirmation"] == T.EXTENDED_HOURS_PENDING
    assert card["high_20d_state"] == "盘后突破"
    assert card["high_20d_state"] != "收盘突破"


def test_intraday_breakout_labelled_pending():
    conf = T.confirmation_basis("REGULAR")
    card = T.compute_symbol("RKLB", _bars(), Decimal("83.85"), confirmation=conf)
    assert card["confirmation"] == T.INTRADAY_PENDING
    assert card["high_20d_state"] == "盘中突破"


def test_settled_close_breakout_is_confirmed():
    conf = T.confirmation_basis("CLOSED", price_source="CLOSE",
                                quote_source="IBKR_reqMktData")
    card = T.compute_symbol("RKLB", _bars(), Decimal("83.85"), confirmation=conf)
    assert card["confirmation"] == T.CLOSE_CONFIRMED
    assert card["high_20d_state"] == "收盘突破"


# ---------------- T4 规则与综合裁决随确认态降级 ----------------

def test_t4_extended_hours_pending_not_confirmed():
    conf = T.confirmation_basis("POST", quote_source="FUTU_AFTER_HOURS")
    card = T.compute_symbol("RKLB", _bars(), Decimal("83.85"), confirmation=conf)
    rules = T.evaluate_rules(card, confirmation=conf)
    t4 = [r for r in rules if r["code"] == "T4"]
    assert t4, "应命中 T4 突破"
    assert t4[0]["confirmation"] == T.EXTENDED_HOURS_PENDING


def test_decide_extended_hours_gives_observe_not_add_watch():
    """扩展时段结构健康也只能 OBSERVE，不得 ADD_WATCH（避免盘后追突破）。"""
    # 造一张多头趋势、贴近趋势线、仍有加仓空间的卡
    card = {"daily_trend_state": "多头趋势", "position_state": "贴近趋势线",
            "technical_summary": "多头趋势"}
    ext = T.decide(card, data_ok=True, risk_breached=False, over_limit=False,
                   plan_allows_add=True, confirmation=T.EXTENDED_HOURS_PENDING)
    assert ext["final_action"] == "OBSERVE"
    confirmed = T.decide(card, data_ok=True, risk_breached=False, over_limit=False,
                         plan_allows_add=True, confirmation=T.CLOSE_CONFIRMED)
    assert confirmed["final_action"] == "ADD_WATCH"


def test_over_limit_still_overrides_regardless_of_confirmation():
    """P0-1 修复不改变优先级：超限时无论确认态如何都 DO_NOT_ADD（RKLB 场景）。"""
    card = {"daily_trend_state": "多头趋势", "position_state": "贴近趋势线",
            "technical_summary": "多头趋势"}
    for conf in (T.EXTENDED_HOURS_PENDING, T.INTRADAY_PENDING, T.CLOSE_CONFIRMED):
        rec = T.decide(card, data_ok=True, risk_breached=False, over_limit=True,
                       plan_allows_add=False, confirmation=conf)
        assert rec["final_action"] == "DO_NOT_ADD"
        assert rec["decided_by"] == "position_limit"
