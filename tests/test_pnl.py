"""收益计算纯函数：哨兵过滤、窗口口径、资金流覆盖、今日百分比基准。

对应验收项 1/2/9：历史不足显示数据不足；资金流部分覆盖不得显示盈亏；
今日百分比基准缺失与存在未核对资金流。
"""
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from app.pnl import (
    CHANGE_ONLY, INSUFFICIENT_HISTORY, PNL, clean_pnl, compute_window,
    coverage_complete, daily_pct, et_date_of, flows_sum, pnl_quality,
)

MAX_DOUBLE = 1.7976931348623157e308


def _eq(et_date, nl):
    return {"et_date": et_date, "net_liquidation": Decimal(str(nl))}


# ---------------- 哨兵值过滤 ----------------

def test_clean_pnl_normal():
    assert clean_pnl(-340.8786) == Decimal("-340.8786")
    assert clean_pnl(Decimal("111.07")) == Decimal("111.07")
    assert clean_pnl(0) == Decimal("0")


def test_clean_pnl_rejects_max_double():
    """IBKR 用 max double 表示「不可用」，绝不能当成 1.79e308 元入库。"""
    assert clean_pnl(MAX_DOUBLE) is None
    assert clean_pnl(-MAX_DOUBLE) is None
    assert clean_pnl(Decimal(str(MAX_DOUBLE))) is None


def test_clean_pnl_rejects_nan_and_inf():
    assert clean_pnl(float("nan")) is None
    assert clean_pnl(float("inf")) is None
    assert clean_pnl(float("-inf")) is None
    assert clean_pnl(Decimal("NaN")) is None


def test_clean_pnl_rejects_garbage():
    assert clean_pnl(None) is None
    assert clean_pnl("abc") is None


def test_pnl_quality_levels():
    d = Decimal("1")
    assert pnl_quality(d, d, d) == "OK"
    assert pnl_quality(d, None, d) == "PARTIAL"
    assert pnl_quality(None, None, None) == "UNAVAILABLE"


# ---------------- 资金流求和（含冲正） ----------------

def test_flows_sum_basic():
    assert flows_sum([{"amount": Decimal("100")}, {"amount": Decimal("-30")}]) == Decimal("70")


def test_reversal_cancels_out_by_plain_summation():
    """验收项 3：冲正是追加一条相反金额的记录，求和自然抵销 —— 原记录不动。"""
    flows = [
        {"amount": Decimal("46691")},    # #1 登记错了
        {"amount": Decimal("-46691")},   # #2 冲正 #1
        {"amount": Decimal("46000")},    # #3 正确金额
    ]
    assert flows_sum(flows) == Decimal("46000")


def test_flows_sum_empty():
    assert flows_sum([]) == Decimal("0")


# ---------------- 覆盖声明 ----------------

def _cov(s, e, status="COMPLETE"):
    return {"start_date": s, "end_date": e, "status": status}


def test_coverage_requires_declaration():
    assert coverage_complete([], "2026-07-01", "2026-07-16") is False


def test_coverage_full_range():
    assert coverage_complete([_cov("2026-07-01", "2026-07-31")],
                             "2026-07-10", "2026-07-16") is True


def test_coverage_partial_is_not_complete():
    """验收项 2：只覆盖了一部分 → 不算完整。"""
    assert coverage_complete([_cov("2026-07-10", "2026-07-14")],
                             "2026-07-10", "2026-07-16") is False


def test_coverage_partial_status_ignored():
    assert coverage_complete([_cov("2026-07-01", "2026-07-31", "PARTIAL")],
                             "2026-07-10", "2026-07-16") is False


def test_coverage_adjacent_ranges_merge():
    assert coverage_complete(
        [_cov("2026-07-01", "2026-07-09"), _cov("2026-07-10", "2026-07-20")],
        "2026-07-05", "2026-07-16") is True


def test_coverage_gap_breaks_merge():
    assert coverage_complete(
        [_cov("2026-07-01", "2026-07-08"), _cov("2026-07-10", "2026-07-20")],
        "2026-07-05", "2026-07-16") is False


# ---------------- 窗口：历史不足 ----------------

def test_insufficient_history_for_30d():
    """验收项 1：账户只有 6 天历史，不得叫「近30日盈亏」。"""
    r = compute_window(days=30, today_et="2026-07-16", end_value=Decimal("43500"),
                       start_row=None, earliest_row=_eq("2026-07-11", 378.83),
                       flows=Decimal("0"), coverage_ok=True)
    assert r.status == INSUFFICIENT_HISTORY
    assert r.label == "近30日"          # 不能叫「近30日盈亏」
    assert r.days_available == 5
    assert "有效历史 5/30 天" in r.note        # 给出 N/目标天数
    assert r.available_on == "2026-08-10"      # 最早 07-11 + 30 天
    assert r.amount is None


def test_insufficient_history_for_7d():
    r = compute_window(days=7, today_et="2026-07-16", end_value=Decimal("43500"),
                       start_row=None, earliest_row=_eq("2026-07-11", 378.83),
                       flows=Decimal("0"), coverage_ok=True)
    assert r.status == INSUFFICIENT_HISTORY and r.amount is None


def test_no_history_at_all():
    r = compute_window(days=7, today_et="2026-07-16", end_value=Decimal("43500"),
                       start_row=None, earliest_row=None, flows=Decimal("0"),
                       coverage_ok=True)
    assert r.status == INSUFFICIENT_HISTORY and r.days_available == 0


# ---------------- 窗口：资金流覆盖决定叫法 ----------------

def test_window_change_only_when_flows_uncovered():
    """验收项 2：资金流未确认覆盖 → 只能叫「净资产变动」，绝不能叫盈亏。"""
    r = compute_window(days=7, today_et="2026-07-16", end_value=Decimal("43500"),
                       start_row=_eq("2026-07-09", 378.83),
                       earliest_row=_eq("2026-07-01", 378.83),
                       flows=Decimal("0"), coverage_ok=False)
    assert r.status == CHANGE_ONLY
    assert r.label == "近7日净资产变动"
    assert r.amount == Decimal("43121.17")     # 真实但无意义的数字
    assert r.pct is None                       # 不给收益率
    assert r.coverage_ok is False
    from app.pnl import FLOW_INCOMPLETE_NOTE
    assert r.note == FLOW_INCOMPLETE_NOTE      # 全站统一文案


def test_window_pnl_deducts_flows():
    """资金流覆盖完整 → 扣除入金后才是真盈亏。"""
    r = compute_window(days=7, today_et="2026-07-16", end_value=Decimal("43500"),
                       start_row=_eq("2026-07-09", 378.83),
                       earliest_row=_eq("2026-07-01", 378.83),
                       flows=Decimal("46691"), coverage_ok=True)
    assert r.status == PNL and r.label == "近7日盈亏"
    # 43500 - 378.83 - 46691 = -3569.83
    assert r.amount == Decimal("-3569.83")


def test_window_pct_uses_start_value():
    r = compute_window(days=7, today_et="2026-07-16", end_value=Decimal("110"),
                       start_row=_eq("2026-07-09", 100), earliest_row=_eq("2026-07-01", 100),
                       flows=Decimal("0"), coverage_ok=True)
    assert r.amount == Decimal("10") and r.pct == Decimal("0.1")


def test_window_reports_actual_dates():
    r = compute_window(days=7, today_et="2026-07-16", end_value=Decimal("110"),
                       start_row=_eq("2026-07-08", 100), earliest_row=_eq("2026-07-01", 100),
                       flows=Decimal("0"), coverage_ok=True)
    assert r.start_date == "2026-07-08" and r.end_date == "2026-07-16"


def test_window_zero_start_no_pct():
    r = compute_window(days=7, today_et="2026-07-16", end_value=Decimal("100"),
                       start_row=_eq("2026-07-09", 0), earliest_row=_eq("2026-07-01", 0),
                       flows=Decimal("0"), coverage_ok=True)
    assert r.pct is None


# ---------------- 今日百分比 ----------------

def test_daily_pct_normal():
    r = daily_pct(daily_pnl=Decimal("-340.88"), prev_close_row=_eq("2026-07-15", 43841.37),
                  has_unreconciled_flows=False)
    assert r.available is True
    assert r.basis == Decimal("43841.37") and r.basis_date == "2026-07-15"


def test_daily_pct_unavailable_without_basis():
    """验收项 9：基准缺失 → 只显示金额。"""
    r = daily_pct(daily_pnl=Decimal("-340.88"), prev_close_row=None,
                  has_unreconciled_flows=False)
    assert r.available is False and r.pct is None
    assert "基准" in r.reason


def test_daily_pct_unavailable_with_unreconciled_flows():
    """验收项 9：当日有未核对资金流 → 百分比不可用。"""
    r = daily_pct(daily_pnl=Decimal("-340.88"), prev_close_row=_eq("2026-07-15", 43841.37),
                  has_unreconciled_flows=True)
    assert r.available is False and r.pct is None
    assert "未核对资金流" in r.reason


def test_daily_pct_no_pnl():
    r = daily_pct(daily_pnl=None, prev_close_row=_eq("2026-07-15", 100),
                  has_unreconciled_flows=False)
    assert r.available is False


def test_daily_pct_invalid_basis():
    r = daily_pct(daily_pnl=Decimal("10"), prev_close_row=_eq("2026-07-15", 0),
                  has_unreconciled_flows=False)
    assert r.available is False


# ---------------- ET 日期换算 ----------------

def test_et_date_of_crosses_utc_midnight():
    from zoneinfo import ZoneInfo
    et = ZoneInfo("America/New_York")
    # UTC 2026-07-16 03:00 = ET 2026-07-15 23:00 —— 仍是 15 号
    assert et_date_of(datetime(2026, 7, 16, 3, 0, tzinfo=timezone.utc), et) == "2026-07-15"
    assert et_date_of(datetime(2026, 7, 16, 14, 0, tzinfo=timezone.utc), et) == "2026-07-16"


# ---------------- 交易会话日（今日盈亏归属） ----------------
# 回归：ET 凌晨拿到的 dailyPnL 属于上一个交易日。若按日历日归属，今日百分比的
# 分母会取到已经包含这笔盈亏的当日日终净值 —— 拿结果除以它自己。

from zoneinfo import ZoneInfo

from app.pnl import pnl_session_date

_ET = ZoneInfo("America/New_York")


def test_session_date_before_premarket_is_prior_day():
    # UTC 06:01 = ET 02:01（07-16 凌晨）→ 会话仍是 07-15
    assert pnl_session_date(datetime(2026, 7, 16, 6, 1, tzinfo=timezone.utc), _ET) == "2026-07-15"


def test_session_date_during_market_is_today():
    # UTC 14:00 = ET 10:00 盘中
    assert pnl_session_date(datetime(2026, 7, 16, 14, 0, tzinfo=timezone.utc), _ET) == "2026-07-16"


def test_session_date_after_close_is_still_today():
    # UTC 21:00 = ET 17:00 收盘后，仍属当日会话
    assert pnl_session_date(datetime(2026, 7, 16, 21, 0, tzinfo=timezone.utc), _ET) == "2026-07-16"


def test_session_date_premarket_boundary():
    # ET 03:59 → 昨天；ET 04:00 → 今天
    assert pnl_session_date(datetime(2026, 7, 16, 7, 59, tzinfo=timezone.utc), _ET) == "2026-07-15"
    assert pnl_session_date(datetime(2026, 7, 16, 8, 0, tzinfo=timezone.utc), _ET) == "2026-07-16"


def test_session_date_weekend_falls_back_to_friday():
    # ET 周六 02:00 → 周五会话
    assert pnl_session_date(datetime(2026, 7, 18, 6, 0, tzinfo=timezone.utc), _ET) == "2026-07-17"


# ---------------- 基准一致性校验 ----------------
# IBKR 未公开 dailyPnL 的会话重置时点。实测盘后拿到的 dailyPnL 基准已是当日收盘
# （会话翻篇），此时按日历日取「前一交易日」会让分母错一整天。
# 用 IBKR 自己的隐含基准（净资产 - dailyPnL）交叉验证，对不上就不给百分比。

def test_daily_pct_ok_when_basis_matches_ibkr():
    r = daily_pct(daily_pnl=Decimal("-500"), prev_close_row=_eq("2026-07-15", 43500),
                  has_unreconciled_flows=False, net_liquidation=Decimal("43000"))
    assert r.available is True                       # 43000-(-500)=43500 == 基准
    assert r.pct == Decimal("-500") / Decimal("43500")


def test_daily_pct_unavailable_when_basis_contradicts_ibkr():
    """真实场景：基准取了 07-14 的 47069.92，但 IBKR 隐含起始净值是 44178.99。"""
    r = daily_pct(daily_pnl=Decimal("-678.50"), prev_close_row=_eq("2026-07-14", 47069.92),
                  has_unreconciled_flows=False, net_liquidation=Decimal("43500.49"))
    assert r.available is False and r.pct is None
    assert "IBKR 口径不符" in r.reason
    assert r.basis == Decimal("47069.92")            # 仍报出用了什么基准，便于排查


def test_daily_pct_tolerates_small_drift():
    """净资产在盘中持续变动，微小偏差不该让百分比消失。"""
    r = daily_pct(daily_pnl=Decimal("-500"), prev_close_row=_eq("2026-07-15", 43500),
                  has_unreconciled_flows=False, net_liquidation=Decimal("43050"))
    assert r.available is True                       # 隐含 43550 vs 43500 => 0.1% 偏差


def test_daily_pct_without_net_liquidation_skips_check():
    r = daily_pct(daily_pnl=Decimal("-500"), prev_close_row=_eq("2026-07-15", 43500),
                  has_unreconciled_flows=False)
    assert r.available is True


# ---------------- 盘中降级（reqPnL 需要实时行情） ----------------
# 实测：本账户只有延迟行情，ET 09:00 开盘后 reqPnL 三项全部 nan，盘前盘后正常。
# portfolio.unrealizedPNL 与 executions.realizedPNL 盘中仍可用，用它们降级。

from app.pnl import BASIS_NET_LIQ, BASIS_NET_LIQ_RAW, SOURCE_DERIVED, derive_pnl


def test_derive_uses_portfolio_upl_for_unrealized():
    r = derive_pnl(positions_upl=[Decimal("331.5"), Decimal("-12.46"),
                                  Decimal("-2337.41"), Decimal("-541.59")],
                   today_realized=Decimal("0"), net_liquidation=Decimal("40786.57"),
                   prev_close_nl=Decimal("43500.49"), flows=Decimal("0"),
                   coverage_ok=True)
    assert r["unrealized_pnl"] == Decimal("-2559.96")     # 真实线上数据
    assert r["source"] == SOURCE_DERIVED
    assert r["quality"] == "DEGRADED"                      # 绝不冒充券商 PnL 原值


def test_derive_daily_from_net_liq_change():
    r = derive_pnl(positions_upl=[Decimal("1")], today_realized=Decimal("0"),
                   net_liquidation=Decimal("40786.57"), prev_close_nl=Decimal("43500.49"),
                   flows=Decimal("0"), coverage_ok=True)
    assert r["daily_pnl"] == Decimal("-2713.92")
    assert r["daily_basis"] == BASIS_NET_LIQ


def test_derive_daily_deducts_flows():
    r = derive_pnl(positions_upl=[], today_realized=None,
                   net_liquidation=Decimal("50000"), prev_close_nl=Decimal("43500"),
                   flows=Decimal("5000"), coverage_ok=True)
    assert r["daily_pnl"] == Decimal("1500")          # 6500 涨幅里 5000 是入金


def test_derive_marks_unadjusted_when_flows_uncertain():
    r = derive_pnl(positions_upl=[], today_realized=None,
                   net_liquidation=Decimal("50000"), prev_close_nl=Decimal("43500"),
                   flows=Decimal("0"), coverage_ok=False)
    assert r["daily_basis"] == BASIS_NET_LIQ_RAW      # 未确认资金流 -> 口径要标出来


def test_derive_without_basis_gives_no_daily():
    r = derive_pnl(positions_upl=[Decimal("5")], today_realized=None,
                   net_liquidation=Decimal("100"), prev_close_nl=None,
                   flows=Decimal("0"), coverage_ok=True)
    assert r["daily_pnl"] is None and r["daily_basis"] is None
    assert r["unrealized_pnl"] == Decimal("5")        # 未实现仍可给


def test_derive_all_missing_is_unavailable():
    r = derive_pnl(positions_upl=[], today_realized=None, net_liquidation=None,
                   prev_close_nl=None, flows=Decimal("0"), coverage_ok=True)
    assert r["quality"] == "UNAVAILABLE"


def test_derive_ignores_none_upl():
    r = derive_pnl(positions_upl=[Decimal("10"), None], today_realized=None,
                   net_liquidation=None, prev_close_nl=None, flows=Decimal("0"),
                   coverage_ok=True)
    assert r["unrealized_pnl"] == Decimal("10")


# ---------------- 交易基线：入金后的纯盈亏（第 3 项） ----------------
# 真实场景：账户 07-14 入金（378 → 47069），近 7 日不能把 46,691 转账当收益。

from app.pnl import trading_baseline

_WD = lambda d: __import__("datetime").date.fromisoformat(d).weekday()


def _series(*pairs):
    return [{"et_date": d, "net_liquidation": Decimal(str(v))} for d, v in pairs]


def test_baseline_detects_deposit_jump():
    s = _series(("2026-07-13", "378.83"), ("2026-07-14", "47069.92"),
                ("2026-07-15", "43500.49"), ("2026-07-16", "39592.46"))
    b = trading_baseline(s, _WD)
    assert b["et_date"] == "2026-07-14"        # 入金后那天
    assert b["net_liquidation"] == Decimal("47069.92")


def test_baseline_from_empty_account():
    """0/空账户变为有值 = 入金。"""
    s = _series(("2026-07-13", "0"), ("2026-07-14", "40000"))
    assert trading_baseline(s, _WD)["et_date"] == "2026-07-14"


def test_baseline_none_when_no_jumps():
    """全程无资金流 -> 无基线，任意起点都是纯盈亏。"""
    s = _series(("2026-07-13", "40000"), ("2026-07-14", "40500"),
                ("2026-07-15", "39800"))
    assert trading_baseline(s, _WD) is None


def test_baseline_ignores_weekend_gap():
    """周五->周一的正常涨跌不算资金流跳变。"""
    s = _series(("2026-07-17", "40000"), ("2026-07-20", "48000"))   # +20%，跨周末
    assert trading_baseline(s, _WD) is None


def test_baseline_takes_last_jump():
    """两次入金 -> 用最后一次之后。"""
    s = _series(("2026-07-10", "1000"), ("2026-07-11", "20000"),    # 入金1
                ("2026-07-12", "19500"), ("2026-07-13", "60000"))   # 入金2
    assert trading_baseline(s, _WD)["et_date"] == "2026-07-13"


# ---------------- 窗口用基线 -> 盈亏而非净资产变动 ----------------

def test_window_clipped_to_baseline_is_pnl_without_coverage():
    """从入金后基线算起，即便没有覆盖声明也是盈亏（窗口内无资金流）。"""
    r = compute_window(
        days=7, today_et="2026-07-20", end_value=Decimal("40174"),
        start_row=_eq("2026-07-13", "378.83"),      # 目标起点在入金前
        earliest_row=_eq("2026-07-10", "378.83"),
        flows=Decimal("0"), coverage_ok=False,       # 没登记资金流
        baseline_row=_eq("2026-07-14", "47069.92"),  # 入金后基线
    )
    assert r.status == PNL                            # 是盈亏，不是净资产变动
    assert r.start_date == "2026-07-14"
    # 47069.92 -> 40174 = -6895.92（纯亏损，入金不计入）
    assert r.amount == Decimal("40174") - Decimal("47069.92")
    assert "入金后" in r.note


def test_window_baseline_before_target_uses_target():
    """基线早于目标起点时，用目标起点（基线不影响近距离窗口）。"""
    r = compute_window(
        days=7, today_et="2026-08-20", end_value=Decimal("40000"),
        start_row=_eq("2026-08-13", "41000"),
        earliest_row=_eq("2026-07-14", "47069.92"),
        flows=Decimal("0"), coverage_ok=True,
        baseline_row=_eq("2026-07-14", "47069.92"),  # 一个月前，早于目标起点
    )
    assert r.start_date == "2026-08-13"              # 用目标起点，不是基线
    assert r.amount == Decimal("40000") - Decimal("41000")


def test_window_no_baseline_keeps_old_behavior():
    """无基线（全程无资金流）时，行为与旧逻辑一致。"""
    r = compute_window(
        days=7, today_et="2026-07-20", end_value=Decimal("41000"),
        start_row=_eq("2026-07-13", "40000"), earliest_row=_eq("2026-07-01", "40000"),
        flows=Decimal("0"), coverage_ok=True, baseline_row=None,
    )
    assert r.status == PNL and r.amount == Decimal("1000")


# ---------------- 历史盈亏（自入金后全部，第 3 项） ----------------

from app.pnl import compute_inception


def test_inception_from_baseline():
    """自入金后基线到今天的全部盈亏。"""
    r = compute_inception(
        today_et="2026-07-20", end_value=Decimal("40174"),
        baseline_row=_eq("2026-07-14", "47069.92"),
        earliest_row=_eq("2026-07-10", "378.83"))
    assert r.label == "历史盈亏" and r.status == PNL
    assert r.start_date == "2026-07-14"
    assert r.amount == Decimal("40174") - Decimal("47069.92")   # 入金不计入
    assert "入金后" in r.note


def test_inception_without_baseline_uses_earliest():
    r = compute_inception(
        today_et="2026-07-20", end_value=Decimal("41000"),
        baseline_row=None, earliest_row=_eq("2026-07-01", "40000"))
    assert r.start_date == "2026-07-01"
    assert r.amount == Decimal("1000")
    assert "入金后" not in r.note


def test_inception_no_data():
    r = compute_inception(today_et="2026-07-20", end_value=None,
                          baseline_row=None, earliest_row=None)
    assert r.status == INSUFFICIENT_HISTORY


# ---------------- 日历：每日收益（第 2 项） ----------------

from app.pnl import daily_returns

_WD2 = lambda d: __import__("datetime").date.fromisoformat(d).weekday()


def test_daily_returns_basic():
    rows = _series(("2026-07-14", "47069.92"), ("2026-07-15", "43500.49"),
                   ("2026-07-16", "39592.46"))
    out = daily_returns(rows, {}, _WD2)
    assert out[0]["amount"] is None                 # 第一天无前值
    assert out[1]["amount"] == Decimal("43500.49") - Decimal("47069.92")
    assert out[1]["pct"] == (Decimal("43500.49") - Decimal("47069.92")) / Decimal("47069.92")
    assert out[2]["amount"] == Decimal("39592.46") - Decimal("43500.49")


def test_daily_returns_deducts_flow():
    """入金日：收益扣除入金，并标记 has_flow。"""
    rows = _series(("2026-07-13", "378.83"), ("2026-07-14", "47069.92"))
    out = daily_returns(rows, {"2026-07-14": Decimal("46691")}, _WD2)
    # 47069.92 - 378.83 - 46691 = 0.09（纯交易，不含入金）
    assert out[1]["amount"] == Decimal("0.09")
    assert out[1]["has_flow"] is True


def test_daily_returns_weekend_gap_is_one_day_return():
    """周五->周一相对是一个交易日的收益。"""
    rows = _series(("2026-07-17", "40000"), ("2026-07-20", "40500"))
    out = daily_returns(rows, {}, _WD2)
    assert out[1]["amount"] == Decimal("500")


def test_daily_returns_never_infers_cash_flow_from_equity_jump():
    """没有券商资金流记录时，不得仅凭 20% 跳变擅自改成“资金流”。"""
    rows = _series(("2026-07-13", "378.83"), ("2026-07-14", "47069.92"),
                   ("2026-07-15", "43500.49"))
    out = daily_returns(rows, {}, _WD2)   # 无登记资金流
    jump = next(r for r in out if r["et_date"] == "2026-07-14")
    assert jump["amount"] == Decimal("46691.09")
    assert jump["has_jump"] is False
    # 入金后的正常交易日照常算
    normal = next(r for r in out if r["et_date"] == "2026-07-15")
    assert normal["amount"] == Decimal("43500.49") - Decimal("47069.92")
    assert normal["has_jump"] is False


def test_daily_returns_registered_flow_is_deducted_not_hidden():
    """已登记入金：扣除后显示为收益（标 has_flow，不是 has_jump）。"""
    rows = _series(("2026-07-13", "378.83"), ("2026-07-14", "47069.92"))
    out = daily_returns(rows, {"2026-07-14": Decimal("46691")}, _WD2)
    r = out[1]
    assert r["has_jump"] is False and r["has_flow"] is True
    assert r["amount"] == Decimal("0.09")


def test_reset_daily_pnl_cannot_override_official_nav_change():
    rows = _series(("2026-07-29", "40000"), ("2026-07-30", "49000"))
    out = daily_returns(
        rows, {}, _WD2, pnl_by_date={"2026-07-30": Decimal("194.12")})
    r = out[1]
    assert r["amount"] == Decimal("9000")
    assert r["has_jump"] is False
    assert r["source"] == "IBKR_NAV_MINUS_EXTERNAL_FLOW"


def test_explicit_cash_flows_disable_equity_jump_guessing():
    from app.pnl import trading_baseline
    rows = _series(("2026-07-29", "40000"), ("2026-07-30", "49000"))
    assert trading_baseline(rows, _WD2, cash_flow_dates=[]) is None
