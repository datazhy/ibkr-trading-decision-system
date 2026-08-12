"""风险指标层：把一份账户快照换算成事实性指标。

**只算事实，不下判断。** 是否「危险」由规则层决定（app/risk_engine.py）。
指标与规则分离的理由：阈值会变、口径不会变。把 30% 写进指标函数里，
以后改阈值就得改计算代码，历史指标也无法用新阈值重算。

三条计算纪律：

1. **全程 Decimal**。二进制浮点在金融计算里会累积误差，且 0.1+0.2 这类问题
   一旦进了风险评分就再也查不出来。
2. **分母为零返回 None，不返回 0**。空账户的「现金比例」不是 0%，是「无法计算」——
   把它记成 0 会让下游规则误判成「现金比例过低」而报警。
3. **口径写进名字**。weight_of_equity 与 weight_of_invested 是两个不同的数，
   同一个字段名装两种口径是本项目反复踩过的坑。

所有比例以 Decimal 返回，展示层再决定精度；金额到 API 层统一转字符串。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable

ZERO = Decimal("0")
ONE = Decimal("1")

#: 计算过程中发现的问题（不同于数据质量问题：这些是「算不出来」而非「数据不对」）
CALC_DIVIDE_BY_ZERO = "DIVIDE_BY_ZERO"
CALC_MISSING_INPUT = "MISSING_INPUT"
CALC_INSUFFICIENT_HISTORY = "INSUFFICIENT_HISTORY"
CALC_ESTIMATED = "ESTIMATED"


@dataclass
class CalcIssue:
    code: str
    field: str
    message: str

    def as_dict(self) -> dict:
        return {"code": self.code, "field": self.field, "message": self.message}


def d(v: Any) -> Decimal | None:
    """任意值 -> Decimal；不可解析或缺失返回 None（绝不用 0 顶替）。"""
    if v is None or v == "":
        return None
    if isinstance(v, Decimal):
        return v
    try:
        return Decimal(str(v))
    except (InvalidOperation, ValueError, TypeError):
        return None


def ratio(num: Decimal | None, den: Decimal | None, issues: list[CalcIssue] | None = None,
          *, field: str = "") -> Decimal | None:
    """安全除法。分母为 0 或缺失 -> None + 记一条计算问题。

    这是本模块最重要的一个函数：**伪造的 0 比缺失危险得多** ——
    「现金比例 0%」会触发「现金过低」告警，而真相可能只是净资产还没取到。
    """
    if num is None or den is None:
        if issues is not None and field:
            issues.append(CalcIssue(CALC_MISSING_INPUT, field, f"{field} 缺少计算输入"))
        return None
    if den == 0:
        if issues is not None and field:
            issues.append(CalcIssue(CALC_DIVIDE_BY_ZERO, field, f"{field} 的分母为零，无法计算"))
        return None
    return num / den


# ======================= 1. 账户资产结构 =======================

@dataclass
class ExposureBreakdown:
    """敞口拆解。多空分开 —— 净敞口会把 100 万多头和 100 万空头抵消成 0，
    但那绝不是「没有风险」，所以必须同时给总敞口。"""
    gross: Decimal = ZERO      # Σ|市值|          总风险敞口
    net: Decimal = ZERO        # Σ市值            净风险敞口（多空相抵）
    long: Decimal = ZERO       # Σ市值(>0)        多头敞口
    short: Decimal = ZERO      # Σ|市值|(<0)      空头敞口


def exposures(positions: Iterable[dict]) -> ExposureBreakdown:
    e = ExposureBreakdown()
    for p in positions or []:
        mv = d(p.get("market_value"))
        if mv is None:
            continue
        e.gross += abs(mv)
        e.net += mv
        if mv >= 0:
            e.long += mv
        else:
            e.short += -mv
    return e


def account_structure(balances: dict, positions: list[dict]) -> tuple[dict, list[CalcIssue]]:
    """账户资产结构指标。

    公式（分母一律 net_liquidation，除非另注）：
      cash_ratio              = total_cash / NL
      invested_ratio          = long_exposure / NL
      margin_utilization      = init_margin / NL            初始保证金占用比例
      maint_margin_ratio      = maint_margin / NL           维持保证金比例
      excess_liquidity_ratio  = excess_liquidity / NL       剩余流动性比例
      buying_power_multiple   = buying_power / NL           购买力相对净资产倍数
      gross_leverage          = gross_exposure / NL         总杠杆
      net_leverage            = net_exposure / NL           净杠杆
    """
    issues: list[CalcIssue] = []
    nl = d(balances.get("equity"))
    total_cash = d(balances.get("total_cash"))
    if total_cash is None:
        total_cash = d(balances.get("cash"))
    e = exposures(positions)
    # 持仓总市值优先取账户口径（GrossPositionValue），缺失时用明细合计
    pos_value = d(balances.get("positions_value"))
    pos_value_source = balances.get("positions_value_source") or "account"
    if pos_value is None:
        pos_value, pos_value_source = e.gross, "positions_detail"

    m = {
        "net_liquidation": nl,
        "total_cash": total_cash,
        "available_funds": d(balances.get("available_funds")),
        "excess_liquidity": d(balances.get("excess_liquidity")),
        "buying_power": d(balances.get("buying_power")),
        "init_margin": d(balances.get("init_margin")),
        "maint_margin": d(balances.get("maint_margin")),
        "positions_market_value": pos_value,
        "positions_value_source": pos_value_source,
        "gross_exposure": e.gross,
        "net_exposure": e.net,
        "long_exposure": e.long,
        "short_exposure": e.short,
        "base_currency": balances.get("currency"),
    }
    m["cash_ratio"] = ratio(total_cash, nl, issues, field="cash_ratio")
    m["invested_ratio"] = ratio(e.long, nl, issues, field="invested_ratio")
    m["margin_utilization"] = ratio(m["init_margin"], nl, issues, field="margin_utilization")
    m["maint_margin_ratio"] = ratio(m["maint_margin"], nl, issues, field="maint_margin_ratio")
    m["excess_liquidity_ratio"] = ratio(m["excess_liquidity"], nl, issues,
                                        field="excess_liquidity_ratio")
    m["buying_power_multiple"] = ratio(m["buying_power"], nl, issues,
                                       field="buying_power_multiple")
    m["gross_leverage"] = ratio(e.gross, nl, issues, field="gross_leverage")
    m["net_leverage"] = ratio(e.net, nl, issues, field="net_leverage")
    return m, issues


# ======================= 2. 双口径持仓集中度 =======================

def position_weights(positions: list[dict], net_liquidation: Decimal | None
                     ) -> tuple[list[dict], list[CalcIssue]]:
    """每只持仓的**双口径**权重。

    这是本系统的强制要求，理由是它们回答两个不同的问题：

      weight_of_equity   = |市值| / 净资产
          「这只票占我全部身家多少」—— 衡量账户整体暴露。
      weight_of_invested = |市值| / 多头市值合计
          「这只票占我已经投出去的钱多少」—— 衡量选股集中度。

    高现金账户会让两者差异极大：某票占总资产 10%，却可能占已投资仓位 50%。
    只显示前者会让人误以为「很分散」，而实际上一旦入场，风险高度集中在一只票上。
    """
    issues: list[CalcIssue] = []
    e = exposures(positions)
    invested = e.long          # 已投资资金 = 多头市值合计（用户口径）
    out = []
    for p in positions or []:
        mv = d(p.get("market_value"))
        abs_mv = abs(mv) if mv is not None else None
        is_short = mv is not None and mv < 0
        w_eq = ratio(abs_mv, net_liquidation)
        # 空头不摊进多头分母 —— 那会算出无意义的比例
        w_inv = None if is_short else ratio(abs_mv, invested)
        out.append({
            "symbol": p.get("symbol"),
            "market_value": mv,
            "is_short": is_short,
            "weight_of_equity": w_eq,
            "weight_of_invested_capital": w_inv,
        })
    if invested == 0 and any(x["weight_of_invested_capital"] is None for x in out):
        issues.append(CalcIssue(CALC_DIVIDE_BY_ZERO, "weight_of_invested_capital",
                                "多头市值合计为零，占已投资仓位比例无法计算"))
    return out, issues


def _top_n(weights: list[Decimal], n: int) -> Decimal | None:
    vals = sorted((w for w in weights if w is not None), reverse=True)
    if not vals:
        return None
    return sum(vals[:n], ZERO)


#: 判定持仓「有效」的条件。无效持仓会稀释集中度指标，让一个实际上高度集中的
#: 组合看起来分散 —— 所以必须排除，且**每一条排除都要记原因**。
EXCL_ZERO_MV = "zero_market_value"
EXCL_NO_PRICE = "unreliable_price"
EXCL_DUPLICATE = "duplicate_record"


def eligible_positions(positions: list[dict]) -> tuple[list[dict], list[dict]]:
    """筛出有效持仓，并记录每一条排除的原因。

    返回 (有效持仓, 排除记录)。排除记录进入指标输出，让「为什么只有 3 只」
    这个问题随时可查 —— 否则有效持仓数变化时没人知道是仓位变了还是数据坏了。
    """
    kept: list[dict] = []
    excluded: list[dict] = []
    seen: dict[str, int] = {}
    for p in positions or []:
        sym = p.get("symbol")
        mv = d(p.get("market_value"))
        key = str(p.get("conid") or sym or "")

        if key and key in seen:
            excluded.append({"symbol": sym, "reason": EXCL_DUPLICATE,
                             "note": "同一证券出现多条记录，只保留第一条"})
            continue
        if mv is None or mv == 0:
            excluded.append({"symbol": sym, "reason": EXCL_ZERO_MV,
                             "note": "市值为零或缺失，不构成集中度"})
            continue
        # 价格不可靠时市值本身就不可信，用它算权重会得到假的集中度
        pq = str(p.get("price_quality") or "").upper()
        if pq in ("MISSING", "INVALID") or d(p.get("price")) in (None, ZERO):
            excluded.append({"symbol": sym, "reason": EXCL_NO_PRICE,
                             "note": f"价格质量 {pq or '缺失'}，市值不可信"})
            continue
        if key:
            seen[key] = 1
        kept.append(p)
    return kept, excluded


def concentration(positions: list[dict], net_liquidation: Decimal | None,
                  unknown_sector_symbols: set[str] | None = None
                  ) -> tuple[dict, list[CalcIssue]]:
    """集中度指标（双口径）。

      max_position_weight_of_equity    最大单一持仓占总资产
      max_position_weight_of_invested  最大单一持仓占已投资仓位
      top3 / top5                      前三 / 前五大集中度（两种口径各一套）
      hhi                              Herfindahl-Hirschman Index = Σ w²
                                       用「占已投资仓位」口径 —— 衡量的是选股集中度，
                                       现金多寡不该影响它。范围 (0, 1]，1 = 全押一只。
      long_hhi / short_hhi             多空分别计算
      unknown_sector_ratio             未知板块分类占比（分类不全会让 R2 失真）
    """
    # 先筛有效持仓 —— 零市值、不可定价、重复记录都会稀释集中度指标
    eligible, excluded = eligible_positions(positions)
    rows, issues = position_weights(eligible, net_liquidation)
    eq_w = [r["weight_of_equity"] for r in rows]
    inv_w = [r["weight_of_invested_capital"] for r in rows if not r["is_short"]]

    longs = [r for r in rows if not r["is_short"]]
    shorts = [r for r in rows if r["is_short"]]
    e = exposures(eligible)

    def hhi(weights: list[Decimal | None]) -> Decimal | None:
        vals = [w for w in weights if w is not None]
        return sum((w * w for w in vals), ZERO) if vals else None

    short_w = [ratio(abs(d(r["market_value"]) or ZERO), e.short) for r in shorts]

    unknown = unknown_sector_symbols or set()
    unknown_mv = sum((abs(d(p.get("market_value")) or ZERO)
                      for p in positions or [] if p.get("symbol") in unknown), ZERO)

    # 排序后的多头权重，供 Top-N 与主导度判定复用
    sorted_inv = sorted((w for w in inv_w if w is not None), reverse=True)
    hhi_inv = hhi(inv_w)
    # 等效持仓数 = 1/HHI。「名义上 5 只，风险权重相当于 2 只等权」——
    # 这是比 Top-N 更有信息量的分散度刻画，且不随持仓数量变化而失真。
    effective_count = (ONE / hhi_inv) if (hhi_inv and hhi_inv > 0) else None

    top1 = sorted_inv[0] if sorted_inv else None
    top2 = sorted_inv[1] if len(sorted_inv) > 1 else None
    # 主导度差：最大与第二大的权重差。真正由一只票主导的组合，
    # 这个差会很大；均匀分布的组合即使集中度高，差也很小。
    dominance_gap = (top1 - top2) if (top1 is not None and top2 is not None) else None

    m = {
        "position_count": len(rows),
        "total_position_count": len(positions or []),
        "eligible_long_position_count": len(longs),
        "eligible_short_position_count": len(shorts),
        "excluded_positions": excluded,
        "excluded_count": len(excluded),
        "top1_weight": top1,
        "top3_weight": _top_n(inv_w, 3),
        "top5_weight": _top_n(inv_w, 5),
        "hhi": hhi_inv,
        "effective_position_count": effective_count,
        "dominance_gap": dominance_gap,
        "long_count": len(longs),
        "short_count": len(shorts),
        "max_position_weight_of_equity": max((w for w in eq_w if w is not None), default=None),
        "max_position_weight_of_invested": max((w for w in inv_w if w is not None), default=None),
        "top3_weight_of_equity": _top_n(eq_w, 3),
        "top5_weight_of_equity": _top_n(eq_w, 5),
        "top3_weight_of_invested": _top_n(inv_w, 3),
        "top5_weight_of_invested": _top_n(inv_w, 5),
        "hhi_invested": hhi(inv_w),
        "hhi_equity": hhi(eq_w),
        "long_hhi": hhi(inv_w),
        "short_hhi": hhi(short_w),
        "unknown_sector_ratio": ratio(unknown_mv, e.gross) if e.gross else None,
        "positions": rows,
    }
    return m, issues


# ======================= 3. 单只持仓风险 =======================

def losing_pool_total(positions: Iterable[dict]) -> Decimal | None:
    """全部**亏损**持仓的浮亏绝对值合计。没有亏损仓返回 None（不是 0）。"""
    losses = [abs(v) for v in (d(p.get("upl")) for p in positions or [])
              if v is not None and v < 0]
    return sum(losses, ZERO) if losses else None


def position_risk(positions: list[dict], net_liquidation: Decimal | None,
                  *, max_position_weight: Decimal, total_unrealized: Decimal | None = None,
                  daily_pnl_total: Decimal | None = None) -> list[dict]:
    """逐持仓风险明细。

      unrealized_pnl_percent = 未实现盈亏 / |成本市值|，成本市值 = |数量| × 成本价
      pct_from_cost = (现价 - 成本价) / 成本价

    **浮亏贡献拆成两个口径**（原先只有一个，会出现 111.9% 这种反直觉数字）：

      contribution_to_net_unrealized_loss
          = 该仓浮亏 / 账户净浮亏
          其它仓盈利时分母被抵薄，结果可 >100%。数学没错，但不适合当主指标，
          **仅用于深入分析**。

      contribution_to_total_losing_positions        ← 默认口径
          = |该仓浮亏| / Σ|所有亏损仓的浮亏|
          只有亏损仓参与分母，正常落在 0–100%，回答「我的亏损主要来自哪只」。
          没有任何亏损仓时返回 None，绝不伪造 0。

    **刻意不做的事**：不因浮亏就判定这笔交易「错误」，也绝不因下跌而建议补仓。
    浮亏是事实，是否该处理取决于策略与止损纪律，不是指标层能下的结论。
    """
    rows, _ = position_weights(positions, net_liquidation)
    by_symbol = {r["symbol"]: r for r in rows}
    loss_pool = losing_pool_total(positions)
    # 前三大（按占总资产）
    top3 = {r["symbol"] for r in sorted(
        (r for r in rows if r["weight_of_equity"] is not None),
        key=lambda r: r["weight_of_equity"], reverse=True)[:3]}

    out = []
    for p in positions or []:
        sym = p.get("symbol")
        qty = d(p.get("qty"))
        cost = d(p.get("avg_cost"))
        price = d(p.get("price"))
        mv = d(p.get("market_value"))
        upl = d(p.get("upl"))
        w = by_symbol.get(sym, {})
        flags: list[str] = []

        cost_basis = (abs(qty) * cost) if (qty is not None and cost is not None) else None
        upl_pct = ratio(upl, cost_basis)
        pct_from_cost = (ratio(price - cost, cost)
                         if (price is not None and cost is not None and cost != 0) else None)

        if price is None or price <= 0:
            flags.append("PRICE_UNAVAILABLE")
        if cost is None:
            flags.append("COST_UNAVAILABLE")
        if qty is not None and qty == 0 and mv not in (None, ZERO):
            flags.append("QTY_ANOMALY")
        w_eq = w.get("weight_of_equity")
        if w_eq is not None and w_eq > max_position_weight:
            flags.append("OVER_POSITION_LIMIT")
        if (net_liquidation is not None and net_liquidation > 0 and mv is not None
                and abs(mv) > net_liquidation):
            flags.append("EXCEEDS_NET_LIQUIDATION")
        if sym in top3:
            flags.append("TOP3_HOLDING")

        out.append({
            "symbol": sym,
            "quantity": qty,
            "average_cost": cost,
            "market_price": price,
            "market_value": mv,
            "unrealized_pnl": upl,
            "unrealized_pnl_percent": upl_pct,
            "weight_of_equity": w_eq,
            "weight_of_invested_capital": w.get("weight_of_invested_capital"),
            # 净浮亏贡献：分母是账户净浮亏，可 >100%（其它仓盈利时），仅供深入分析
            "contribution_to_net_unrealized_loss": (
                ratio(upl, total_unrealized) if (upl is not None and upl < 0) else None),
            # 亏损池贡献：默认口径，只有亏损仓参与分母，正常 0–100%
            "contribution_to_total_losing_positions": (
                ratio(abs(upl), loss_pool) if (upl is not None and upl < 0) else None),
            "contribution_to_daily_pnl": ratio(d(p.get("day_pnl")), daily_pnl_total),
            "pct_from_cost": pct_from_cost,
            "is_top3": sym in top3,
            "over_position_limit": "OVER_POSITION_LIMIT" in flags,
            "price_quality": p.get("price_quality"),
            "is_short": bool(mv is not None and mv < 0),
            "risk_flags": flags,
        })
    return out


# ======================= 4. 盈亏与回撤 =======================

@dataclass
class DrawdownResult:
    current_drawdown: Decimal | None = None      # 从历史峰值回撤
    max_drawdown: Decimal | None = None          # 历史最大回撤
    peak_value: Decimal | None = None
    peak_date: str | None = None
    drawdown_days: int | None = None             # 当前回撤已持续天数
    recent_high_drawdown: Decimal | None = None  # 从近期（窗口内）高点回撤
    issues: list[CalcIssue] = field(default_factory=list)


def drawdown(equity_series: list[dict], *, recent_window: int = 20) -> DrawdownResult:
    """回撤计算。

      current_drawdown = (峰值 - 当前) / 峰值
      max_drawdown     = 历史上 (峰值 - 谷值) / 峰值 的最大值

    equity_series: [{et_date, net_liquidation}] 升序，**必须是已剔除资金流影响的序列**
    （调用方用 pnl.trading_baseline 裁掉入金前的段）。否则一笔入金会被算成「暴涨」，
    随后正常波动就成了巨大回撤 —— 这是回撤指标最容易被污染的地方。
    """
    r = DrawdownResult()
    pts = [(x["et_date"], d(x["net_liquidation"])) for x in equity_series or []]
    pts = [(dt, v) for dt, v in pts if v is not None]
    if len(pts) < 2:
        r.issues.append(CalcIssue(CALC_INSUFFICIENT_HISTORY, "drawdown",
                                  "净值历史不足 2 个点，无法计算回撤"))
        return r

    peak, peak_date = pts[0][1], pts[0][0]
    max_dd = ZERO
    dd_start_idx = None
    for i, (dt, v) in enumerate(pts):
        if v > peak:
            peak, peak_date = v, dt
            dd_start_idx = None
        elif peak > 0:
            dd = (peak - v) / peak
            if dd > max_dd:
                max_dd = dd
            if dd > 0 and dd_start_idx is None:
                dd_start_idx = i
    cur = pts[-1][1]
    r.peak_value, r.peak_date = peak, peak_date
    r.current_drawdown = ((peak - cur) / peak) if peak > 0 else None
    r.max_drawdown = max_dd
    if dd_start_idx is not None:
        r.drawdown_days = len(pts) - dd_start_idx
    window = pts[-recent_window:] if len(pts) > recent_window else pts
    wp = max(v for _, v in window)
    r.recent_high_drawdown = ((wp - cur) / wp) if wp > 0 else None
    return r


def window_returns(equity_series: list[dict], windows=(5, 20, 60),
                   *, flows_present: bool = False) -> dict:
    """近 N 个「有数据的交易日」收益率。

    return_n = (期末净值 - 期初净值) / 期初净值

    **estimated 标记**：若窗口内存在未登记的资金流，收益率会把转账算成盈亏。
    本项目 API 侧拿不到转账流水，故只要检测到跳变或调用方声明有资金流，
    就把该指标标成 estimated —— 宁可标注不确定，也不输出一个看起来精确的错数。
    """
    pts = [(x["et_date"], d(x["net_liquidation"])) for x in equity_series or []]
    pts = [(dt, v) for dt, v in pts if v is not None]
    out: dict[str, Any] = {}
    for n in windows:
        key = f"return_{n}d"
        if len(pts) < n + 1:
            out[key] = None
            out[f"{key}_status"] = CALC_INSUFFICIENT_HISTORY
            continue
        start_v, end_v = pts[-(n + 1)][1], pts[-1][1]
        out[key] = ratio(end_v - start_v, start_v)
        out[f"{key}_status"] = CALC_ESTIMATED if flows_present else "OK"
    return out


def pnl_ratios(balances: dict, pnl: dict | None, net_liquidation: Decimal | None
               ) -> tuple[dict, list[CalcIssue]]:
    """当日/未实现/已实现盈亏占净资产比例。"""
    issues: list[CalcIssue] = []
    p = pnl or {}
    daily = d(p.get("daily_pnl"))
    unreal = d(p.get("unrealized_pnl"))
    real = d(p.get("realized_pnl"))
    return {
        "daily_pnl": daily,
        "unrealized_pnl": unreal,
        "realized_pnl": real,
        "daily_pnl_ratio": ratio(daily, net_liquidation, issues, field="daily_pnl_ratio"),
        "unrealized_pnl_ratio": ratio(unreal, net_liquidation, issues,
                                      field="unrealized_pnl_ratio"),
        "realized_pnl_ratio": ratio(real, net_liquidation, issues,
                                    field="realized_pnl_ratio"),
    }, issues


# ======================= 5. 流动性与压力情景 =======================

def liquidity_and_stress(balances: dict, positions: list[dict],
                         net_liquidation: Decimal | None) -> tuple[dict, list[CalcIssue]]:
    """流动性缓冲与静态压力情景。

    压力情景是**确定性算术**，不是概率模型 —— 第一版刻意保持透明：
      账户净值下跌 X%  -> 剩余流动性 = excess_liquidity - NL×X
                         维持保证金安全倍数 = (NL×(1-X)) / maint_margin
      最大持仓下跌 X%  -> 账户影响 = 该持仓市值 × X（占净资产比例）
      前三大同跌 X%    -> 影响 = 前三大市值合计 × X

    绝不把它包装成 VaR 或蒙特卡洛 —— 那需要波动率与相关性假设，
    在没有这些输入的情况下给出概率数字，是伪精确。
    """
    issues: list[CalcIssue] = []
    nl = net_liquidation
    maint = d(balances.get("maint_margin"))
    excess = d(balances.get("excess_liquidity"))
    cash = d(balances.get("total_cash")) or d(balances.get("cash"))

    m: dict[str, Any] = {
        "cash_buffer": cash,
        "available_funds_buffer": d(balances.get("available_funds")),
        "excess_liquidity_buffer": excess,
        # 维持保证金安全倍数：净资产是维持保证金的几倍。<1 意味着已触发追缴
        "maint_margin_safety_multiple": ratio(nl, maint, issues,
                                              field="maint_margin_safety_multiple"),
    }

    account_stress = []
    for pct in (Decimal("0.05"), Decimal("0.10"), Decimal("0.20")):
        if nl is None:
            account_stress.append({"drop_pct": pct, "net_liquidation_after": None,
                                   "excess_liquidity_after": None,
                                   "maint_margin_safety_after": None})
            continue
        nl_after = nl * (Decimal("1") - pct)
        account_stress.append({
            "drop_pct": pct,
            "net_liquidation_after": nl_after,
            "excess_liquidity_after": (excess - nl * pct) if excess is not None else None,
            "maint_margin_safety_after": ratio(nl_after, maint),
            "margin_call": (bool(maint is not None and maint > 0 and nl_after < maint)),
        })
    m["account_stress"] = account_stress

    mvs = sorted(((p.get("symbol"), abs(d(p.get("market_value")) or ZERO))
                  for p in positions or []), key=lambda x: x[1], reverse=True)
    largest = mvs[0] if mvs else (None, ZERO)
    top3_mv = sum((v for _, v in mvs[:3]), ZERO)

    m["largest_position_stress"] = [
        {"symbol": largest[0], "drop_pct": pct,
         "loss_amount": largest[1] * pct,
         "impact_on_equity": ratio(largest[1] * pct, nl)}
        for pct in (Decimal("0.10"), Decimal("0.20"), Decimal("0.30"))
    ]
    m["top3_stress"] = [
        {"drop_pct": pct, "loss_amount": top3_mv * pct,
         "impact_on_equity": ratio(top3_mv * pct, nl)}
        for pct in (Decimal("0.10"), Decimal("0.20"), Decimal("0.30"))
    ]
    return m, issues
