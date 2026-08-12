"""市场环境规则层 + 状态分类 + 账户/市场联合视图。

回答三个问题：
  1. 当前市场处于什么状态
  2. 风险在改善还是恶化
  3. 账户风险是否被外部环境放大

**边界**（写在代码里以免日后越界）：
  - 不用单个 VIX 数值决定全部结论
  - **不覆盖账户风险原始评分** —— 联合视图是解释层，账户历史评估永远是原值
  - 数据缺失一律 blocked，绝不解释成「低风险」
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

CALC_VERSION = "market-v1/2026-07"
_VERSION_SEP = "~"          # URL 安全，理由同 risk_engine

# ---- 市场状态 ----
SUPPORTIVE = "supportive"   # 趋势与波动率整体支持承担正常风险
NEUTRAL = "neutral"         # 无明显支持或压制
CAUTIOUS = "cautious"       # 部分指标恶化，应控制新增风险
STRESSED = "stressed"       # 趋势/波动率显著恶化
BLOCKED = "blocked"         # 数据不足，无法可靠判断

# ---- 严重度（与账户侧一致）----
SEV_WARNING = "warning"
SEV_HIGH = "high"
SEV_CRITICAL = "critical"
_SEV_W = {SEV_WARNING: 1, SEV_HIGH: 2, SEV_CRITICAL: 3}

# ---- 分类（同时是评分维度）----
CAT_TREND = "trend"
CAT_VOLATILITY = "volatility"
CAT_RATES = "rates"
CAT_BREADTH = "breadth"
CAT_DATA = "data_quality"

GT, LT = "gt", "lt"


@dataclass(frozen=True)
class MarketRule:
    rule_code: str
    name: str
    category: str
    description: str
    severity: str
    threshold: Decimal | None
    comparator: str
    calculation_basis: str
    unit: str = "ratio"
    instrument: str | None = None
    enabled: bool = True

    def as_dict(self) -> dict:
        return {"rule_code": self.rule_code, "name": self.name, "category": self.category,
                "description": self.description, "severity": self.severity,
                "threshold": None if self.threshold is None else str(self.threshold),
                "comparator": self.comparator, "unit": self.unit,
                "calculation_basis": self.calculation_basis,
                "instrument": self.instrument, "enabled": self.enabled}


@dataclass
class MarketEvent:
    event_code: str
    severity: str
    title: str
    explanation: str
    actual_value: Any
    threshold_value: Any
    unit: str
    rule_code: str
    category: str
    affected_instrument: str | None = None
    recommended_check: str = ""

    def as_dict(self, detected_at: str | None = None, rule_version: str | None = None) -> dict:
        return {"event_code": self.event_code, "severity": self.severity,
                "title": self.title, "explanation": self.explanation,
                "actual_value": None if self.actual_value is None else str(self.actual_value),
                "threshold_value": (None if self.threshold_value is None
                                    else str(self.threshold_value)),
                "actual_display": _fmt(self.actual_value, self.unit),
                "threshold_display": _fmt(self.threshold_value, self.unit),
                "unit": self.unit, "rule_code": self.rule_code,
                "category": self.category,
                "affected_instrument": self.affected_instrument,
                "recommended_check": self.recommended_check,
                "rule_version": rule_version, "detected_at": detected_at}


def _fmt(v: Any, unit: str) -> str:
    if v is None:
        return "—"
    if not isinstance(v, Decimal):
        try:
            v = Decimal(str(v))
        except Exception:  # noqa: BLE001
            return str(v)
    if unit == "ratio":
        return f"{v:.1%}"
    if unit == "level":
        return f"{v:.2f}"
    # 兜底：未特别处理的单位也取整，绝不把原始 Decimal 精度抖到叙事里
    s = f"{v.quantize(Decimal('0.01')):,.2f}"
    return s.rstrip("0").rstrip(".") if "." in s else s


def default_rules(cfg=None) -> list[MarketRule]:
    def t(name: str, fallback: str) -> Decimal:
        v = getattr(cfg, name, None) if cfg is not None else None
        return Decimal(str(v)) if v is not None else Decimal(fallback)

    return [
        # ---- 趋势 ----
        MarketRule("TREND_QQQ_BELOW_MA20", "QQQ 跌破 20 日均线", CAT_TREND,
                   "短期趋势转弱", SEV_WARNING, Decimal("0"), LT, "qqq.vs_ma20",
                   instrument="QQQ"),
        MarketRule("TREND_QQQ_BELOW_MA50", "QQQ 跌破 50 日均线", CAT_TREND,
                   "中期趋势转弱", SEV_HIGH, Decimal("0"), LT, "qqq.vs_ma50",
                   instrument="QQQ"),
        MarketRule("TREND_QQQ_BELOW_MA200", "QQQ 跌破 200 日均线", CAT_TREND,
                   "长期趋势转弱", SEV_CRITICAL, Decimal("0"), LT, "qqq.vs_ma200",
                   instrument="QQQ"),
        MarketRule("TREND_QQQ_DRAWDOWN", "QQQ 从近期高点回撤过大", CAT_TREND,
                   "从近 60 日高点回撤超过阈值", SEV_HIGH,
                   t("market_qqq_drawdown_max", "0.10"), GT, "qqq.drawdown_from_60d_high",
                   instrument="QQQ"),
        MarketRule("TREND_SOX_WEAKER", "半导体明显弱于大盘", CAT_TREND,
                   "SOXX 近 20 日跑输 QQQ 超过阈值 —— 与账户的半导体持仓直接相关",
                   SEV_HIGH, t("market_sox_underperform_max", "0.05"), LT,
                   "sox_vs_qqq_20d", instrument="SOXX"),
        MarketRule("TREND_MULTI_INDEX_WEAK", "多个主要指数同时转弱", CAT_TREND,
                   "QQQ 与 SPY 同时跌破 50 日均线", SEV_HIGH, None, LT,
                   "multi_index_weak"),
        # ---- 波动率 ----
        MarketRule("VOL_VIX_ELEVATED", "VIX 高于警戒值", CAT_VOLATILITY,
                   "市场隐含波动率处于警戒区间", SEV_WARNING,
                   t("market_vix_warn", "20"), GT, "vol.vix_current", unit="level",
                   instrument="VIX"),
        MarketRule("VOL_VIX_HIGH", "VIX 高于严重风险值", CAT_VOLATILITY,
                   "市场隐含波动率处于高风险区间", SEV_CRITICAL,
                   t("market_vix_crit", "30"), GT, "vol.vix_current", unit="level",
                   instrument="VIX"),
        MarketRule("VOL_VIX_SPIKE", "VIX 单日快速上升", CAT_VOLATILITY,
                   "单日涨幅超过阈值，市场情绪快速转向", SEV_HIGH,
                   t("market_vix_spike", "0.15"), GT, "vol.vix_change_1d",
                   instrument="VIX"),
        MarketRule("VOL_VXN_ELEVATED", "VXN 高于警戒值", CAT_VOLATILITY,
                   "纳斯达克隐含波动率处于警戒区间", SEV_WARNING,
                   t("market_vxn_warn", "22"), GT, "vol.vxn_current", unit="level",
                   instrument="VXN"),
        MarketRule("VOL_VXN_ABOVE_VIX", "VXN 明显高于 VIX", CAT_VOLATILITY,
                   "科技股波动预期显著高于大盘", SEV_WARNING,
                   t("market_vxn_vix_gap", "5"), GT, "vol.vxn_above_vix", unit="level"),
        MarketRule("VOL_BOTH_RISING", "VIX 与 VXN 同时快速上升", CAT_VOLATILITY,
                   "两个波动率指标同步恶化，是系统性信号而非单一噪音",
                   SEV_HIGH, None, GT, "vol.both_rising", unit="flag"),
        # ---- 利率 ----
        MarketRule("RATES_FAST_RISE_5D", "十年期收益率五日快速上升", CAT_RATES,
                   "利率快速上行通常压制成长股估值", SEV_WARNING,
                   t("market_rates_5d_pct", "0.08"), GT, "rates.pct_change_5d",
                   instrument="US10Y"),
        MarketRule("RATES_ABOVE_THRESHOLD", "十年期收益率突破阈值", CAT_RATES,
                   "绝对水平突破配置阈值", SEV_WARNING,
                   t("market_rates_level_max", "45"), GT, "rates.current", unit="level",
                   instrument="US10Y"),
        MarketRule("RATES_RISE_WITH_EQUITY_WEAK", "利率上升且股指同期转弱", CAT_RATES,
                   "两件事同期发生（**不代表因果**）", SEV_HIGH, None, GT,
                   "rates_equity_co", unit="flag"),
        # ---- 数据 ----
        MarketRule("MKT_DATA_MISSING", "核心市场数据缺失", CAT_DATA,
                   "关键标的取不到数据，市场判断不可靠", SEV_CRITICAL, None, GT,
                   "data.missing_required", unit="flag"),
        MarketRule("MKT_DATA_STALE", "市场数据严重过期", CAT_DATA,
                   "数据时间落后过多", SEV_CRITICAL, None, GT, "data.stale", unit="flag"),
        MarketRule("MKT_DATA_INCONSISTENT", "数据源时间不一致", CAT_DATA,
                   "各标的数据时间不同，市场状态是拼接产物", SEV_WARNING, None, GT,
                   "data.inconsistent", unit="flag"),
    ]


def rule_set_hash(rules: list[MarketRule]) -> str:
    parts = ["|".join([r.rule_code, r.category, r.severity, r.comparator,
                       r.calculation_basis, r.unit,
                       "" if r.threshold is None else str(r.threshold)])
             for r in sorted(rules, key=lambda x: x.rule_code) if r.enabled]
    return hashlib.sha256("\n".join(parts).encode()).hexdigest()[:16]


def rule_set_version(rules: list[MarketRule]) -> str:
    return f"{CALC_VERSION}{_VERSION_SEP}{rule_set_hash(rules)}"


_CHECKS = {
    "TREND_QQQ_BELOW_MA20": "确认是短期回调还是趋势反转；检查新增仓位的时机",
    "TREND_QQQ_BELOW_MA50": "中期趋势已转弱，检查是否仍在按原计划加仓",
    "TREND_QQQ_BELOW_MA200": "长期趋势转弱，重新评估整体仓位水平与风险预算",
    "TREND_QQQ_DRAWDOWN": "核对账户回撤是否同步；检查止损纪律执行情况",
    "TREND_SOX_WEAKER": "账户若集中于半导体，检查该行业敞口是否需要控制",
    "TREND_MULTI_INDEX_WEAK": "多指数同步转弱，检查整体风险敞口",
    "VOL_VIX_ELEVATED": "波动放大期间，检查单笔风险预算是否仍然合适",
    "VOL_VIX_HIGH": "高波动环境，重点检查杠杆与保证金缓冲",
    "VOL_VIX_SPIKE": "确认是否有突发事件；检查止损位在高波动下是否过近",
    "VOL_VXN_ELEVATED": "科技股波动加大，检查成长股仓位集中度",
    "VOL_VXN_ABOVE_VIX": "科技股风险高于大盘，检查账户是否过度偏向科技",
    "VOL_BOTH_RISING": "系统性波动上升，检查整体仓位与现金缓冲",
    "RATES_FAST_RISE_5D": "检查成长股与长久期资产的敞口",
    "RATES_ABOVE_THRESHOLD": "关注利率水平对估值的持续压力",
    "RATES_RISE_WITH_EQUITY_WEAK": "两者同期发生，检查组合是否对利率敏感",
    "MKT_DATA_MISSING": "检查行情订阅权限与 gateway 连接",
    "MKT_DATA_STALE": "检查市场数据同步任务是否正常",
    "MKT_DATA_INCONSISTENT": "核对各标的的数据时间来源",
}


def _get(metrics: dict, path: str):
    cur: Any = metrics
    for part in path.split("."):
        if not isinstance(cur, dict):
            return None
        cur = cur.get(part)
    return cur


def evaluate(metrics: dict, rules: list[MarketRule]) -> list[MarketEvent]:
    """跑规则。指标为 None 一律不触发 —— 缺失由数据类规则单独报告。"""
    events: list[MarketEvent] = []
    for r in rules:
        if not r.enabled:
            continue
        val = _get(metrics, r.calculation_basis)
        if val is None:
            continue
        if r.unit == "flag":
            if not val:
                continue
            hit, actual = True, val
        else:
            if r.threshold is None:
                continue
            v = val if isinstance(val, Decimal) else Decimal(str(val))
            hit = (v > r.threshold) if r.comparator == GT else (v < r.threshold)
            actual = v
        if not hit:
            continue
        events.append(MarketEvent(
            event_code=r.rule_code, severity=r.severity, title=r.name,
            explanation=f"{r.description}（当前 {_fmt(actual, r.unit)}"
                        + (f"，阈值 {_fmt(r.threshold, r.unit)}" if r.threshold is not None else "")
                        + "）",
            actual_value=actual, threshold_value=r.threshold, unit=r.unit,
            rule_code=r.rule_code, category=r.category,
            affected_instrument=r.instrument,
            recommended_check=_CHECKS.get(r.rule_code, "请核对相关市场数据")))
    return events


# ======================= 评分与状态 =======================

DEFAULT_WEIGHTS = {
    "trend_score": Decimal("0.35"),
    "volatility_score": Decimal("0.30"),
    "rates_score": Decimal("0.15"),
    "breadth_score": Decimal("0.10"),
    "data_quality_score": Decimal("0.10"),
}

_CAT_TO_SCORE = {CAT_TREND: "trend_score", CAT_VOLATILITY: "volatility_score",
                 CAT_RATES: "rates_score", CAT_BREADTH: "breadth_score",
                 CAT_DATA: "data_quality_score"}

_POINTS = {SEV_WARNING: Decimal("20"), SEV_HIGH: Decimal("40"),
           SEV_CRITICAL: Decimal("70")}


def score(events: list[MarketEvent], *, weights: dict | None = None,
          breadth_available: bool = False) -> dict:
    """分项评分 + 加权总分。

    **breadth 无可靠数据时为 null 并重新归一化其余权重** —— 记 0 分会让市场
    看起来比实际更安全，那是最不该出现的偏差方向。
    """
    w = {**DEFAULT_WEIGHTS, **(weights or {})}
    comp: dict[str, Decimal | None] = {k: Decimal("0") for k in DEFAULT_WEIGHTS}
    for e in events:
        key = _CAT_TO_SCORE.get(e.category, "trend_score")
        comp[key] = min(Decimal("100"), (comp[key] or ZERO) + _POINTS.get(e.severity,
                                                                          Decimal("20")))
    if not breadth_available:
        comp["breadth_score"] = None            # 明确「未实现」，不是 0 分

    active = {k: v for k, v in comp.items() if v is not None}
    total_w = sum((w.get(k, ZERO) for k in active), ZERO)
    if total_w == 0:
        return {"component_scores": comp, "market_risk_score": None, "weights": w}
    total = sum((active[k] * w.get(k, ZERO) for k in active), ZERO) / total_w
    return {"component_scores": comp,
            "market_risk_score": total.quantize(Decimal("0.1")), "weights": w}


ZERO = Decimal("0")


def market_state(total_score: Decimal | None, events: list[MarketEvent], *,
                 reliable: bool) -> str:
    """市场状态。blocked 优先于一切 —— 数据不足时不允许给出任何状态结论。"""
    if not reliable or total_score is None:
        return BLOCKED
    if any(e.severity == SEV_CRITICAL for e in events) or total_score >= Decimal("55"):
        return STRESSED
    if any(e.severity == SEV_HIGH for e in events) or total_score >= Decimal("30"):
        return CAUTIOUS
    if total_score >= Decimal("12"):
        return NEUTRAL
    return SUPPORTIVE


_STATE_TO_LEVEL = {SUPPORTIVE: "low", NEUTRAL: "low", CAUTIOUS: "moderate",
                   STRESSED: "high", BLOCKED: "blocked"}


def market_risk_level(state: str) -> str:
    return _STATE_TO_LEVEL.get(state, "blocked")


def top_drivers(events: list[MarketEvent], limit: int = 5) -> list[dict]:
    ranked = sorted(events, key=lambda e: (-_SEV_W.get(e.severity, 0), e.event_code))
    return [e.as_dict() for e in ranked[:limit]]


# ======================= 账户 × 市场 联合视图 =======================

@dataclass
class Amplifier:
    code: str
    kind: str           # amplifier / buffer
    title: str
    explanation: str
    factor: Decimal     # 对关注度的乘数贡献（>1 放大，<1 缓冲）


def combined_view(account: dict | None, market: dict | None, *,
                  cfg=None) -> dict:
    """联合解释层。

    **绝不修改账户风险原始评分** —— 账户历史评估必须保持原值，否则同一份快照
    在不同市场环境下会有不同的「历史」，审计就失去意义。这里只输出解释与关注度。
    """
    def t(name: str, fallback: str) -> Decimal:
        v = getattr(cfg, name, None) if cfg is not None else None
        return Decimal(str(v)) if v is not None else Decimal(fallback)

    acc_level = (account or {}).get("risk_level") or "blocked"
    acc_reliable = bool((account or {}).get("reliable"))
    mkt_state = (market or {}).get("market_state") or BLOCKED
    mkt_level = (market or {}).get("market_risk_level") or "blocked"
    mkt_reliable = bool((market or {}).get("reliable"))

    amps: list[Amplifier] = []
    m = (account or {}).get("calculated_metrics", {}) or {}
    conc = m.get("concentration", {}) or {}
    acct_m = m.get("account", {}) or {}

    def dec(v):
        try:
            return Decimal(str(v)) if v is not None else None
        except Exception:  # noqa: BLE001
            return None

    stressed = mkt_state in (STRESSED, CAUTIOUS)
    max_inv = dec(conc.get("max_position_weight_of_invested"))
    cash_ratio = dec(acct_m.get("cash_ratio"))
    gross_lev = dec(acct_m.get("gross_leverage"))

    if stressed and max_inv is not None and max_inv > Decimal("0.5"):
        amps.append(Amplifier(
            "CONC_IN_STRESSED_MARKET", "amplifier", "集中持仓遇上市场承压",
            f"已投资仓位高度集中（最大单一 {max_inv:.0%}），而市场处于 {mkt_state}，"
            "单一标的的波动会被环境放大", t("combined_conc_factor", "1.3")))
    if stressed and cash_ratio is not None and cash_ratio > Decimal("0.3"):
        amps.append(Amplifier(
            "CASH_BUFFER_IN_STRESS", "buffer", "高现金提供缓冲",
            f"现金比例 {cash_ratio:.0%}，在市场承压时提供腾挪空间",
            t("combined_cash_factor", "0.85")))
    if gross_lev is not None and gross_lev > Decimal("1.5") and mkt_state == STRESSED:
        amps.append(Amplifier(
            "LEVERAGE_IN_VOL_SPIKE", "amplifier", "杠杆遇上波动率上升",
            f"总杠杆 {gross_lev:.2f}x，波动放大时保证金压力上升更快",
            t("combined_leverage_factor", "1.5")))

    # 半导体集中 + SOX 弱势
    sox_weak = any(e.get("rule_code") == "TREND_SOX_WEAKER"
                   for e in (market or {}).get("top_market_drivers", []))
    classes = conc.get("classifications", {}) or {}
    semi = [s for s, c in classes.items()
            if c.get("sector") and any(k in str(c.get("sector"))
                                       for k in ("半导体", "Semiconduct", "存储"))]
    if sox_weak and semi:
        amps.append(Amplifier(
            "SEMI_CONC_WITH_SOX_WEAK", "amplifier", "半导体持仓遇上行业弱势",
            f"账户持有 {'、'.join(semi[:4])} 等半导体标的，而 SOXX 明显弱于大盘",
            t("combined_sector_factor", "1.3")))

    reliable = acc_reliable and mkt_reliable
    factor = Decimal("1")
    for a in amps:
        factor *= a.factor
    factor = factor.quantize(Decimal("0.01"))

    if not reliable:
        view = ("联合判断不可靠："
                + ("账户数据不足；" if not acc_reliable else "")
                + ("市场数据不足；" if not mkt_reliable else "")
                + "不得据此得出确定性结论")
    else:
        parts = [f"账户风险 {acc_level}", f"市场环境 {mkt_state}"]
        if any(a.kind == "amplifier" for a in amps):
            parts.append("存在放大因素")
        if any(a.kind == "buffer" for a in amps):
            parts.append("存在缓冲因素")
        view = "，".join(parts)

    return {
        "account_risk_level": acc_level,
        "market_risk_level": mkt_level,
        "market_state": mkt_state,
        "risk_amplification_factor": str(factor),
        "combined_risk_view": view,
        "reliable": reliable,
        "combined_risk_drivers": [
            {"code": a.code, "kind": a.kind, "title": a.title,
             "explanation": a.explanation, "factor": str(a.factor)} for a in amps],
        "amplifiers": [a.code for a in amps if a.kind == "amplifier"],
        "buffers": [a.code for a in amps if a.kind == "buffer"],
    }
