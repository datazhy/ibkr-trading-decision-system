"""真实收益计算（纯函数）。

回答一个用户真正关心的问题：**这段时间我到底赚了多少？**

账户净值涨了 8% 可能是行情好，也可能是入了一笔钱。把净资产变化当成投资表现，
是个人投资者最常见也最昂贵的自欺。本模块用两种口径消除这个混淆：

**Modified Dietz** —— 按现金流在区间内停留的时间加权。数据要求低，任何有
外部现金流记录的区间都能算。假设必须写明：现金流只有日期没有精确时刻时，
用「按日权重」而非编造一个时刻。

**TWR（时间加权收益率）** —— 按每笔外部现金流切分子区间，各段收益连乘。
它剔除了出入金时点的影响，是比较「投资能力」的标准口径。但它对数据要求高：
每个现金流时点都要有对应的净值。**数据不够就返回 null 和原因，绝不伪造。**

三条不可让步：
  1. 入金不是收益，出金不是亏损
  2. 买卖证券的现金流动**不是**外部现金流
  3. 分红只计一次 —— 它已经反映在净值里，归因时不能再加一遍
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Sequence

from . import cashflow_model as cm

#: v2 起纳入未实现盈亏，market_pnl 不再倒推。旧版本结果保留可对比。
CALC_VERSION = "perf-cash-v2-upnl/2026-07"
LEGACY_VERSIONS = ("perf-cash-v1/2026-07",)

ZERO = Decimal("0")

# 数据质量
Q_OK = "ok"
Q_ESTIMATED = "estimated"
Q_INSUFFICIENT = "insufficient"

# 现金流来源
SRC_FLEX = "flex_query"
#: 券商流水覆盖了区间的绝大部分，但**末尾若干天尚未覆盖** ——
#: Flex 是 T+1 数据源，最近 1–2 天永远不在报表里。要求完整覆盖等于
#: 永远拿不到权威来源，那整个接入就没有意义；但也不能假装全覆盖，
#: 所以单列一个状态并把「还差几天」明确写出来。
SRC_FLEX_PARTIAL = "flex_query_partial"
SRC_HEURISTIC = "heuristic"
SRC_NONE = "none"

# 权重假设
W_DAY = "day_weighted"          # 按日权重（现金流只有日期时）
W_MIDPOINT = "midpoint"         # 中点权重（连日期都不确定时）
W_EXACT = "exact_timestamp"     # 有精确时刻


def _d(v: Any) -> Decimal | None:
    return cm.d(v)


def _s(v) -> str | None:
    return None if v is None else str(v)


def _dt(v) -> datetime | None:
    if not v:
        return None
    try:
        t = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        try:
            t = datetime.fromisoformat(f"{str(v)[:10]}T00:00:00+00:00")
        except (TypeError, ValueError):
            return None
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


# ---------------- Modified Dietz ----------------

def modified_dietz(*, opening_nav, closing_nav, flows: Sequence[dict],
                   period_start, period_end) -> dict:
    """现金流调整收益率。

        R = (期末 - 期初 - 净流入) / (期初 + Σ(流入 × 权重))
        权重 = 该笔流入之后剩余的时间占比

    分母是「实际投入的平均资本」。这正是它比「净值变化率」更诚实的地方：
    月末才入的一笔大钱，不该被当成整月都在为你工作。
    """
    o, c = _d(opening_nav), _d(closing_nav)
    t0, t1 = _dt(period_start), _dt(period_end)
    if o is None or c is None or t0 is None or t1 is None:
        return {"return": None, "quality": Q_INSUFFICIENT,
                "reason": "缺少期初/期末净值或区间边界"}
    total_seconds = (t1 - t0).total_seconds()
    if total_seconds <= 0:
        return {"return": None, "quality": Q_INSUFFICIENT, "reason": "区间长度非法"}
    if o <= 0:
        return {"return": None, "quality": Q_INSUFFICIENT,
                "reason": "期初净值非正，收益率无意义"}

    net_flow = ZERO
    weighted = ZERO
    assumption = W_EXACT
    for f in flows or []:
        amt = _d(f.get("amount_base")) or _d(f.get("amount"))
        if amt is None or amt == 0:
            continue
        ts = _dt(f.get("occurred_at") or f.get("effective_at"))
        if ts is None:
            # 连日期都没有：用中点权重，并把整体标记为估算
            w = Decimal("0.5")
            assumption = W_MIDPOINT
        else:
            # 只有日期没有时刻时，IBKR 的 dateTime 会退化成当日 00:00。
            # 这仍是按日权重，比编一个「收盘时刻」诚实。
            if ts < t0:
                ts = t0
            if ts > t1:
                ts = t1
            remaining = (t1 - ts).total_seconds()
            w = Decimal(str(remaining / total_seconds))
            if assumption == W_EXACT and len(str(f.get("occurred_at") or "")) <= 10:
                assumption = W_DAY
        net_flow += amt
        weighted += amt * w

    denominator = o + weighted
    if denominator <= 0:
        return {"return": None, "quality": Q_INSUFFICIENT,
                "reason": "加权平均资本非正，收益率无意义"}

    gain = c - o - net_flow
    r = (gain / denominator).quantize(Decimal("0.000001"))
    return {
        "return": str(r), "gain": str(gain), "net_flow": str(net_flow),
        "average_capital": str(denominator.quantize(Decimal("0.01"))),
        "quality": Q_ESTIMATED if assumption != W_EXACT else Q_OK,
        "weighting_assumption": assumption,
        "formula": "R = (期末 - 期初 - 净流入) / (期初 + Σ流入×剩余时间权重)",
        "flow_count": len([f for f in (flows or []) if _d(f.get('amount'))]),
    }


# ---------------- TWR ----------------

def time_weighted_return(*, nav_series: Sequence[dict],
                         flows: Sequence[dict]) -> dict:
    """时间加权收益率。按外部现金流切分子区间，各段收益连乘。

    **数据不足时返回 None 和原因**。TWR 需要每个现金流时点前后都有净值；
    缺一个点就会让那一段的收益被算进相邻段，结果不再是 TWR 而是别的东西。
    伪造一个「差不多」的数字，比不给数字有害得多。
    """
    navs = []
    for r in nav_series or []:
        t = _dt(r.get("ts") or r.get("occurred_at"))
        v = _d(r.get("equity") or r.get("nav"))
        if t is not None and v is not None:
            navs.append((t, v))
    navs.sort(key=lambda x: x[0])
    if len(navs) < 2:
        return {"return": None, "quality": Q_INSUFFICIENT,
                "reason": "净值样本少于 2 个，无法计算"}

    ext = []
    for f in flows or []:
        amt = _d(f.get("amount_base")) or _d(f.get("amount"))
        t = _dt(f.get("occurred_at") or f.get("effective_at"))
        if amt is None or amt == 0 or t is None:
            continue
        ext.append((t, amt))
    ext.sort(key=lambda x: x[0])

    # 无外部现金流：整段就是一个子区间
    if not ext:
        start_v, end_v = navs[0][1], navs[-1][1]
        if start_v <= 0:
            return {"return": None, "quality": Q_INSUFFICIENT,
                    "reason": "期初净值非正"}
        r = (end_v / start_v - 1).quantize(Decimal("0.000001"))
        return {"return": str(r), "sub_periods": 1, "quality": Q_OK,
                "formula": "无外部现金流，TWR = 期末/期初 - 1"}

    # 有现金流：在每笔现金流处切分
    def nav_at(t: datetime) -> Decimal | None:
        """现金流时点的净值。取**不晚于**该时刻的最近一条。

        找不到就是找不到 —— 用最近的一条未来净值代替，会把现金流之后的
        涨跌算进现金流之前的子区间。
        """
        best = None
        for ts, v in navs:
            if ts <= t:
                best = v
            else:
                break
        return best

    boundaries: list[tuple[datetime, Decimal]] = []
    missing = 0
    for t, amt in ext:
        v = nav_at(t)
        if v is None:
            missing += 1
            continue
        boundaries.append((t, amt))
    if missing:
        return {"return": None, "quality": Q_INSUFFICIENT,
                "reason": f"{missing} 笔现金流所在时点缺少净值数据，"
                          "无法可靠切分子区间"}

    factor = Decimal("1")
    sub = 0
    prev_t, prev_v = navs[0]
    for t, amt in boundaries:
        v_before = nav_at(t)
        if v_before is None or prev_v <= 0:
            return {"return": None, "quality": Q_INSUFFICIENT,
                    "reason": "子区间边界净值不可用"}
        factor *= (v_before / prev_v)
        sub += 1
        # 现金流之后的起点 = 流入前净值 + 流入额
        prev_v = v_before + amt
        prev_t = t
        if prev_v <= 0:
            return {"return": None, "quality": Q_INSUFFICIENT,
                    "reason": "现金流后净值非正，收益率无意义"}
    end_v = navs[-1][1]
    factor *= (end_v / prev_v)
    sub += 1

    r = (factor - 1).quantize(Decimal("0.000001"))
    return {"return": str(r), "sub_periods": sub, "quality": Q_OK,
            "formula": "TWR = Π(各子区间 期末/期初) - 1，按外部现金流切分"}


# ---------------- 收益归因 ----------------

def attribute(*, opening_nav, closing_nav, totals: dict,
              realized_pnl=None, unrealized_change=None,
              cash_flow_source: str = SRC_FLEX) -> dict:
    """把净值变化拆成可解释的组成部分。

    **避免重复计算**是这里的核心难点：分红已经通过现金增加反映在净值里，
    归因时把它单列，就必须从「市场盈亏」里扣掉，否则加总会超出实际变化。
    因此 market_pnl 用**倒推**而非独立计算，剩余的差额显式记为
    unexplained_difference —— 有一个说不清的差额，比强行凑平诚实。
    """
    o, c = _d(opening_nav), _d(closing_nav)

    def t(cat: str) -> Decimal:
        return _d(totals.get(cat)) or ZERO

    external = t(cm.CAT_EXTERNAL) + t(cm.CAT_TRANSFER)
    dividends = t(cm.CAT_INCOME)
    interest = t(cm.CAT_INTEREST)
    taxes = t(cm.CAT_TAX)
    expense_total = t(cm.CAT_EXPENSE)

    commissions = _d(totals.get("trade_commission")) or ZERO
    fees = expense_total - commissions

    if o is None or c is None:
        return {"data_quality": Q_INSUFFICIENT,
                "quality_note": "缺少期初或期末净值，无法归因",
                "external_cash_flow": _s(external),
                "dividends": _s(dividends), "interest": _s(interest),
                "commissions": _s(commissions), "fees": _s(fees),
                "taxes": _s(taxes)}

    raw_change = c - o
    # 扣除外部资金进出后，才是真正由投资活动带来的变化
    adjusted = raw_change - external

    realized = _d(realized_pnl)
    unrealized = _d(unrealized_change)

    # 已知的现金类损益项
    known_cash_items = dividends + interest + taxes + expense_total

    market_pnl = None
    unexplained = None
    quality = Q_OK
    notes: list[str] = []

    if realized is not None and unrealized is not None:
        # 两侧都有独立来源：market_pnl 是真正算出来的，不是倒推的。
        # 剩下的差额就是**真实的未解释部分**，不得强制归零。
        market_pnl = realized + unrealized
        unexplained = adjusted - market_pnl - known_cash_items
    else:
        # 拆不出已实现/未实现时，把「投资活动变化 - 已知现金项」整体当作市场盈亏，
        # 并明确标记为估算 —— 它里面混着汇率影响，分不开就不假装分得开。
        market_pnl = adjusted - known_cash_items
        unexplained = ZERO
        quality = Q_ESTIMATED
        notes.append("缺少已实现/未实现盈亏明细，market_pnl 为倒推值，"
                     "其中可能混有汇率影响")

    if cash_flow_source == SRC_FLEX_PARTIAL:
        quality = Q_ESTIMATED if quality == Q_OK else quality
    elif cash_flow_source == SRC_HEURISTIC:
        quality = Q_ESTIMATED
        notes.append("外部现金流来自启发式识别而非券商流水，结果为估算值")
    elif cash_flow_source == SRC_NONE:
        quality = Q_INSUFFICIENT
        notes.append("没有任何现金流数据，无法区分资金进出与投资收益")

    if unexplained is not None and o and abs(unexplained) > abs(o) * Decimal("0.005"):
        notes.append(f"未解释差额 {unexplained} 超过期初净值的 0.5%，"
                     "可能存在未覆盖的流水类型或结算日差异")
        if quality == Q_OK:
            quality = Q_ESTIMATED

    return {
        "opening_nav": _s(o), "closing_nav": _s(c),
        "raw_nav_change": _s(raw_change),
        "external_cash_flow": _s(external),
        "deposits": _s(_d(totals.get("_deposits"))),
        "withdrawals": _s(_d(totals.get("_withdrawals"))),
        "cash_flow_adjusted_pnl": _s(adjusted),
        "market_pnl": _s(market_pnl),
        "realized_pnl": _s(realized), "unrealized_pnl": _s(unrealized),
        "dividends": _s(dividends), "interest": _s(interest),
        "commissions": _s(commissions), "fees": _s(fees), "taxes": _s(taxes),
        # 汇率影响目前混在 market_pnl 里，分不开就明说
        "fx_effect": None,
        "unexplained_difference": _s(unexplained),
        "data_quality": quality,
        "quality_note": "；".join(notes) or None,
        "cash_flow_source": cash_flow_source,
    }


# ---------------- 编排 ----------------

def split_deposits_withdrawals(flows: Sequence[dict]) -> tuple[Decimal, Decimal]:
    """外部现金流拆成入金与出金两个正数。页面要分别展示。"""
    dep = wd = ZERO
    for f in flows or []:
        amt = _d(f.get("amount_base")) or _d(f.get("amount"))
        if amt is None:
            continue
        if amt > 0:
            dep += amt
        else:
            wd += -amt
    return dep, wd


def _norm_day(v) -> str:
    v = str(v or "")[:10]
    return f"{v[:4]}-{v[4:6]}-{v[6:8]}" if len(v) == 8 and "-" not in v else v


def flex_coverage(db, start_day: str, end_day: str) -> dict:
    """最近一次成功的 Flex 报表对该区间的覆盖情况。

    起点必须被覆盖 —— 区间开头之外的流水我们一无所知，那是真正的盲区。
    末尾允许有缺口（T+1 使然），但缺了几天必须说清楚。
    """
    out = {"source": SRC_NONE, "coverage_end": None, "uncovered_days": None,
           "report_from": None, "report_to": None}
    try:
        run = db.latest_usable_flex_run()
    except Exception:  # noqa: BLE001
        return out
    if not run:
        return out
    frm, to = _norm_day(run.get("report_from")), _norm_day(run.get("report_to"))
    if not (frm and to and len(frm) == 10 and len(to) == 10):
        return out
    out.update({"report_from": frm, "report_to": to, "coverage_end": to})
    if frm > start_day:
        return out                      # 区间开头未覆盖：盲区，不能当权威
    if to >= end_day:
        out["source"] = SRC_FLEX
        out["uncovered_days"] = 0
        return out
    try:
        from datetime import date
        gap = (date.fromisoformat(end_day) - date.fromisoformat(to)).days
    except (TypeError, ValueError):
        gap = None
    out["uncovered_days"] = gap
    # 末尾缺口在合理范围内（T+1 + 周末）仍算权威，只是标 partial
    out["source"] = SRC_FLEX_PARTIAL if (gap is not None and gap <= 5) else SRC_NONE
    return out


def compute_period(cfg, db, *, start: str, end: str, account: str | None = None,
                   granularity: str = "custom",
                   now: datetime | None = None) -> dict:
    """一个区间的完整收益视图。落库并返回。"""
    now = now or datetime.now(timezone.utc)
    start_iso = f"{str(start)[:10]}T00:00:00+00:00"
    end_iso = f"{str(end)[:10]}T23:59:59+00:00"

    navs = db.equity_series_between(start_iso, end_iso, account=account)
    opening = navs[0].get("equity") if navs else None
    closing = navs[-1].get("equity") if navs else None

    flows = db.external_flows_between(start_iso, end_iso, account=account)
    # 来源由「券商报表是否覆盖这个区间」决定，**不由是否找到流水决定**。
    # 报表覆盖了、且一笔外部资金变动都没有 —— 这是一个**经过验证的事实**，
    # 与「我们没有任何现金流数据」是完全不同的两件事，不能都标成 none。
    coverage = flex_coverage(db, str(start)[:10], str(end)[:10])
    source = coverage["source"]

    totals = db.cash_flow_totals(str(start)[:10], str(end)[:10], account=account)
    sub_totals = db.cash_flow_subtype_totals(str(start)[:10], str(end)[:10],
                                             account=account)
    totals = {**totals, "trade_commission": sub_totals.get("trade_commission")}

    dep, wd = split_deposits_withdrawals(flows)
    totals["_deposits"], totals["_withdrawals"] = str(dep), str(-wd)

    md = modified_dietz(opening_nav=opening, closing_nav=closing, flows=flows,
                        period_start=start_iso, period_end=end_iso)
    twr = time_weighted_return(nav_series=navs, flows=flows)
    realized = db.realized_pnl_total(start_iso, end_iso, account)

    # 未实现盈亏变化：来自持仓快照，同一日只用同一个 snapshot_id
    from . import unrealized_pnl as upnl
    try:
        upnl.build_series(cfg, db, start=str(start)[:10], end=str(end)[:10],
                          account=account, now=now)
        series = db.daily_unrealized(str(start)[:10], str(end)[:10],
                                     account=account,
                                     version=upnl.CALC_VERSION)
        u_change = upnl.period_change(series)
    except Exception:  # noqa: BLE001 — 未实现盈亏算不出来不该拖垮整个归因
        u_change = {"start": None, "end": None, "change": None,
                    "quality": upnl.Q_INSUFFICIENT,
                    "reason": "未实现盈亏序列构建失败"}

    attr = attribute(opening_nav=opening, closing_nav=closing, totals=totals,
                     realized_pnl=realized,
                     unrealized_change=u_change.get("change"),
                     cash_flow_source=source)
    if u_change.get("quality") == upnl.Q_ESTIMATED and u_change.get("reason"):
        attr["quality_note"] = "；".join(
            x for x in (attr.get("quality_note"), u_change["reason"]) if x)

    # **口径区间必须一致，否则归因等式本来就不该配平。**
    # 已实现盈亏来自 Flex（回溯 90 天），未实现盈亏来自账户快照（只有最近若干天）。
    # 两者覆盖的时间不同时，把它们相加去解释同一段净值变化，
    # 差额里就混进了「未实现盈亏在快照之前那段时间的变化」——
    # 那不是真正的未解释，是我们没测量。必须说清楚，不能让人以为是账目出错。
    coverage_mismatch = _period_mismatch(str(start)[:10], str(end)[:10], u_change)
    attr["period_coverage"] = coverage_mismatch
    if source == SRC_FLEX_PARTIAL and coverage.get("uncovered_days"):
        note = (f"券商流水覆盖至 {coverage['coverage_end']}，"
                f"最近 {coverage['uncovered_days']} 天尚未覆盖"
                "（Flex 为 T+1 数据源）")
        attr["quality_note"] = "；".join(
            x for x in (attr.get("quality_note"), note) if x)

    period_id = hashlib.sha256(
        f"{account}|{start}|{end}|{granularity}|{CALC_VERSION}".encode()
    ).hexdigest()[:24]
    row = {
        "period_id": period_id, "account_id": account,
        "period_start": str(start)[:10], "period_end": str(end)[:10],
        "granularity": granularity,
        "opening_nav": _s(_d(opening)), "closing_nav": _s(_d(closing)),
        "raw_nav_change": attr.get("raw_nav_change"),
        "external_cash_flow": attr.get("external_cash_flow"),
        "deposits": str(dep), "withdrawals": str(wd),
        "cash_flow_adjusted_pnl": attr.get("cash_flow_adjusted_pnl"),
        "modified_dietz_return": md.get("return"),
        "time_weighted_return": twr.get("return"),
        "realized_pnl": attr.get("realized_pnl"),
        "unrealized_pnl": attr.get("unrealized_pnl"),
        "unrealized_pnl_start": u_change.get("start"),
        "unrealized_pnl_end": u_change.get("end"),
        "unrealized_pnl_change": u_change.get("change"),
        "dividends": attr.get("dividends"), "interest": attr.get("interest"),
        "commissions": attr.get("commissions"), "fees": attr.get("fees"),
        "taxes": attr.get("taxes"), "fx_effect": attr.get("fx_effect"),
        "market_pnl": attr.get("market_pnl"),
        "unexplained_difference": attr.get("unexplained_difference"),
        "calculation_method": md.get("formula"),
        "weighting_assumption": md.get("weighting_assumption"),
        "data_quality": attr.get("data_quality"),
        "quality_note": attr.get("quality_note"),
        "cash_flow_source": source,
        "calculation_version": CALC_VERSION,
        "created_at": now.isoformat(),
    }
    try:
        db.save_performance_period(row)
    except Exception:  # noqa: BLE001
        pass
    return {**row, "modified_dietz_detail": md, "twr_detail": twr,
            "flow_count": len(flows), "coverage": coverage,
            "unrealized_detail": u_change,
            "period_coverage": attr.get("period_coverage"),
            "data_limitations": _limitations(source, coverage, u_change, attr),
            "calculation_method_detail": {
                "attribution_identity": (
                    "cash_flow_adjusted_pnl = realized_pnl + unrealized_pnl_change"
                    " + dividends + interest + commissions + other_fees + taxes"
                    " + fx_effect + unexplained_difference"),
                "sign_convention": (
                    "佣金/费用/税费在 Flex 中本身为负数，等式中直接相加，"
                    "不再额外取负"),
                "commission_note": (
                    "已实现盈亏用 IBKR 的 fifoPnlRealized，该字段**不含佣金**；"
                    "佣金作为独立项计入，不存在重复扣除"),
                "dividend_note": (
                    "分红只在 income 项计入一次；它已通过现金增加反映在净值里，"
                    "不再进入 realized_pnl"),
                "external_flow_note": (
                    "外部现金流已从 raw_nav_change 中扣除，不进入投资收益；"
                    "买卖证券的现金流动属于 trade_cash_flow，同样不进入"),
                "modified_dietz": md.get("formula"),
                "twr": twr.get("formula"),
            }}


def _period_mismatch(start_day: str, end_day: str, u_change: dict) -> dict:
    """未实现盈亏序列与请求区间的覆盖差距。"""
    out = {"requested_start": start_day, "requested_end": end_day,
           "unrealized_start": u_change.get("start_date"),
           "unrealized_end": u_change.get("end_date"),
           "aligned": False, "uncovered_days": None}
    us = u_change.get("start_date")
    if not us:
        return out
    try:
        from datetime import date
        gap = (date.fromisoformat(us) - date.fromisoformat(start_day)).days
    except (TypeError, ValueError):
        return out
    out["uncovered_days"] = max(0, gap)
    out["aligned"] = gap <= 1
    return out


def _limitations(source: str, coverage: dict, u_change: dict,
                 attr: dict) -> list[str]:
    """把数据限制列清楚。用户看到一个收益数字时，应该同时知道它靠不靠得住。"""
    out: list[str] = []
    if source == SRC_NONE:
        out.append("没有券商现金流数据，无法区分资金进出与投资收益")
    elif source == SRC_FLEX_PARTIAL and coverage.get("uncovered_days"):
        out.append(f"券商流水覆盖至 {coverage.get('coverage_end')}，"
                   f"最近 {coverage['uncovered_days']} 天尚未覆盖，"
                   "最新一天的归因为暂定值")
    elif source == SRC_HEURISTIC:
        out.append("外部现金流来自启发式推断，非券商权威流水")
    if u_change.get("change") is None:
        out.append(f"未实现盈亏变化不可用：{u_change.get('reason') or '数据不足'}")
    elif u_change.get("quality") != "ok":
        out.append("未实现盈亏为估算值（部分由市值减成本推算）")
    if attr.get("fx_effect") is None:
        out.append("汇率影响尚未单独拆分，混在 market_pnl 中")
    pc = attr.get("period_coverage") or {}
    if pc.get("uncovered_days"):
        out.append(
            f"未实现盈亏序列只覆盖 {pc.get('unrealized_start')} 起，"
            f"比请求区间晚 {pc['uncovered_days']} 天；"
            "而已实现盈亏来自 Flex 全区间。**两者口径区间不一致**，"
            "下方未解释差额主要由这段未测量的浮盈变化构成，不代表账目有误")
    unexp = _d(attr.get("unexplained_difference"))
    if unexp is not None and unexp != 0:
        if pc.get("uncovered_days"):
            out.append(f"未解释差额 {unexp}（受上述区间不一致影响），"
                       "已如实保留未强制归零")
        else:
            out.append(f"存在未解释差额 {unexp}，已如实保留未强制归零")
    return out
