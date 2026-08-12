"""联合风险事件（纯函数）。

只做**三个**高价值场景 —— 账户风险与市场环境单独看都不够严重，合起来才值得提醒。
第一版刻意不扩展更多组合：每多一条联合规则，误报面积就大一块，而联合误报比
单边误报更难被用户判断真假。

四条约束：
1. **不修改账户与市场的原始评分**。联合事件是独立的第三类事件，两个引擎的
   历史评估保持原值。
2. **可追溯**：每个事件都带上账户评估 ID 与市场评估 ID。
3. **阈值全部配置化**，并进入 rule_set_version。
4. **高现金只能是 buffer**。市场承压时手里有现金是好事，把它算成风险事件是
   彻底的方向错误 —— 本模块只把它作为缓冲输出，永不生成告警。
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

from .market_engine import CAUTIOUS, STRESSED
from .risk_events import SEV_HIGH, SEV_WARNING

# 事件码（对外稳定，改名等于换规则）
E_SEMI = "SEMICONDUCTOR_CONCENTRATION_MARKET_WEAKNESS"
E_LEVERAGE_VOL = "LEVERAGE_VOLATILITY_AMPLIFICATION"
E_DRAWDOWN_STRESSED = "DRAWDOWN_STRESSED_MARKET"
B_CASH_BUFFER = "CASH_BUFFER_IN_STRESSED_MARKET"

CALC_VERSION = "combined-v1/2026-07"
_VERSION_SEP = "~"

#: 半导体板块的识别关键词。分类来自 instruments.py（IBKR 元数据优先），
#: 这里只做关键词匹配 —— 未分类的标的不会被算进来，宁可漏也不假报。
_SEMI_KEYWORDS = ("半导体", "semiconduct", "存储", "芯片", "chip")


def _d(v: Any) -> Decimal | None:
    if v is None or v == "":
        return None
    if isinstance(v, Decimal):
        return v
    try:
        return Decimal(str(v))
    except (InvalidOperation, ValueError, TypeError):
        return None


@dataclass(frozen=True)
class CombinedRule:
    rule_code: str
    title: str
    severity: str
    threshold: Decimal
    threshold_unit: str
    recommended_check: str


def default_rules(cfg=None) -> list[CombinedRule]:
    def t(name: str, fallback: str) -> Decimal:
        v = getattr(cfg, name, None) if cfg is not None else None
        return Decimal(str(v)) if v not in (None, "") else Decimal(fallback)

    return [
        CombinedRule(
            E_SEMI, "半导体集中持仓遇上行业转弱", SEV_HIGH,
            t("combined_semi_weight_max", "0.30"), "ratio",
            "复核半导体总敞口占已投资仓位的比例；确认这些标的是否高度相关；"
            "回看原定的板块上限与减仓条件是否仍然有效"),
        CombinedRule(
            E_LEVERAGE_VOL, "杠杆遇上波动率快速上升", SEV_HIGH,
            t("combined_leverage_max", "1.50"), "ratio",
            "复核维持保证金缓冲与可用流动性；确认波动放大时的追缴距离；"
            "回看原定的杠杆上限是否仍然适用"),
        CombinedRule(
            E_DRAWDOWN_STRESSED, "账户回撤遇上市场承压", SEV_HIGH,
            t("combined_drawdown_max", "0.10"), "ratio",
            "复核当前回撤相对个人可承受区间的位置；确认亏损集中在哪些标的；"
            "回看原定的止损与减仓纪律是否被执行"),
    ]


def rule_set_hash(rules: list[CombinedRule]) -> str:
    """只哈希影响判定的字段 —— 改注释不该让版本漂移。"""
    parts = [f"{r.rule_code}|{r.severity}|{r.threshold}|{r.threshold_unit}"
             for r in sorted(rules, key=lambda x: x.rule_code)]
    return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()[:16]


def rule_set_version(rules: list[CombinedRule]) -> str:
    """联合规则集版本：combined-v1/2026-07~<hash>。

    hash 只覆盖影响判定的字段（规则码、严重度、阈值、单位）——
    **条件或阈值一变，版本就变；改注释不该让版本漂移。**
    历史事件保留它们当时的版本，不被新版本覆盖。
    """
    return f"{CALC_VERSION}{_VERSION_SEP}{rule_set_hash(rules)}"


def rule_version(rule: CombinedRule) -> str:
    """单条联合规则的版本。

    与规则集版本区分：规则集变了但这条规则没变时，它的版本应当保持稳定 ——
    否则「这条规则什么时候改过」永远说不清。
    """
    fp = hashlib.sha256(
        f"{rule.rule_code}|{rule.severity}|{rule.threshold}|{rule.threshold_unit}"
        .encode("utf-8")).hexdigest()[:12]
    return f"{CALC_VERSION}{_VERSION_SEP}{fp}"


@dataclass
class CombinedEvent:
    event_code: str
    rule_code: str
    severity: str
    title: str
    explanation: str
    actual_value: str | None
    threshold_value: str | None
    unit: str
    recommended_check: str
    affected_symbol: str | None = None
    account_assessment_id: str | None = None
    market_assessment_id: str | None = None
    category: str = "combined"

    def as_dict(self) -> dict:
        return {
            "event_code": self.event_code, "rule_code": self.rule_code,
            "category": self.category, "severity": self.severity,
            "title": self.title, "explanation": self.explanation,
            "actual_value": self.actual_value,
            "threshold_value": self.threshold_value, "unit": self.unit,
            "recommended_check": self.recommended_check,
            "affected_symbol": self.affected_symbol,
            "account_assessment_id": self.account_assessment_id,
            "market_assessment_id": self.market_assessment_id,
        }


def _semi_exposure(account: dict) -> tuple[Decimal | None, list[str]]:
    """半导体持仓占**已投资仓位**的比例，以及涉及的标的。

    用已投资口径而非总资产口径：高现金账户在总资产口径下永远看不到集中度，
    而真正的问题是「投出去的钱押在了同一个行业」。
    """
    m = (account or {}).get("calculated_metrics", {}) or {}
    conc = m.get("concentration", {}) or {}
    classes = conc.get("classifications", {}) or {}
    positions = m.get("positions", []) or []

    semi_syms = []
    for sym, c in classes.items():
        sector = str((c or {}).get("sector") or "").lower()
        industry = str((c or {}).get("industry") or "").lower()
        blob = f"{sector} {industry}"
        if any(k.lower() in blob for k in _SEMI_KEYWORDS):
            semi_syms.append(sym)
    if not semi_syms:
        return None, []

    total = Decimal("0")
    found = []
    for p in positions:
        sym = p.get("symbol")
        if sym not in semi_syms:
            continue
        w = _d(p.get("weight_of_invested_capital"))
        if w is not None:
            total += w
            found.append(sym)
    if not found:
        return None, []
    return total, sorted(found)


def evaluate(account: dict | None, market: dict | None,
             rules: list[CombinedRule] | None = None) -> list[CombinedEvent]:
    """产出联合事件。任一侧缺失或不可靠时返回空 ——
    联合判断建立在两侧都成立的前提上，一侧不可靠就不该给出联合结论。"""
    if not account or not market:
        return []
    if not account.get("reliable") or not market.get("reliable"):
        return []

    rules = rules or default_rules()
    by_code = {r.rule_code: r for r in rules}
    acc_id = account.get("assessment_id")
    mkt_id = market.get("assessment_id")
    mkt_state = market.get("market_state")
    stressed = mkt_state == STRESSED
    stressed_or_cautious = mkt_state in (STRESSED, CAUTIOUS)

    m = (account or {}).get("calculated_metrics", {}) or {}
    acct = m.get("account", {}) or {}
    drawdown = m.get("drawdown", {}) or {}
    mkt_metrics = (market or {}).get("calculated_metrics", {}) or {}
    vol = mkt_metrics.get("vol", {}) or {}

    triggered_market = set(market.get("triggered_rules") or [])
    out: list[CombinedEvent] = []

    # ---- 1. 半导体集中 + 行业转弱 ----
    r = by_code[E_SEMI]
    semi_w, semi_syms = _semi_exposure(account)
    sox_weak = "TREND_SOX_WEAKER" in triggered_market
    if semi_w is not None and sox_weak and semi_w > r.threshold:
        out.append(CombinedEvent(
            E_SEMI, E_SEMI, r.severity, r.title,
            f"半导体持仓占已投资仓位 {semi_w:.1%}（阈值 {r.threshold:.0%}），"
            f"涉及 {'、'.join(semi_syms[:5])}；同期 SOXX 明显弱于 QQQ。"
            "行业层面的回落会同时作用在这些标的上，分散效果比表面看起来弱。",
            str(semi_w), str(r.threshold), r.threshold_unit, r.recommended_check,
            affected_symbol=None,
            account_assessment_id=acc_id, market_assessment_id=mkt_id))

    # ---- 2. 杠杆 + 波动率快速上升 ----
    r = by_code[E_LEVERAGE_VOL]
    lev = _d(acct.get("gross_leverage"))
    vol_rising = bool(
        {"VOL_VIX_SPIKE", "VOL_BOTH_RISING", "VOL_VIX_HIGH"} & triggered_market)
    if lev is not None and vol_rising and lev > r.threshold:
        vix = vol.get("vix_current")
        out.append(CombinedEvent(
            E_LEVERAGE_VOL, E_LEVERAGE_VOL, r.severity, r.title,
            f"总杠杆 {lev:.2f}x（阈值 {r.threshold:.2f}x），"
            f"同期波动率快速上升{f'（VIX {vix}）' if vix else ''}。"
            "波动放大时，同样的价格变动会更快消耗保证金缓冲。",
            str(lev), str(r.threshold), r.threshold_unit, r.recommended_check,
            account_assessment_id=acc_id, market_assessment_id=mkt_id))

    # ---- 3. 回撤 + 市场 stressed ----
    r = by_code[E_DRAWDOWN_STRESSED]
    dd = _d(drawdown.get("current_drawdown"))
    if dd is not None and stressed and dd > r.threshold:
        out.append(CombinedEvent(
            E_DRAWDOWN_STRESSED, E_DRAWDOWN_STRESSED, r.severity, r.title,
            f"账户当前回撤 {dd:.1%}（阈值 {r.threshold:.0%}），"
            f"同期市场环境为 {mkt_state}。"
            "在承压环境里，回撤的自我修复通常比常态更慢。",
            str(dd), str(r.threshold), r.threshold_unit, r.recommended_check,
            account_assessment_id=acc_id, market_assessment_id=mkt_id))

    return out


def cash_buffer(account: dict | None, market: dict | None,
                *, threshold: str = "0.30") -> dict | None:
    """高现金缓冲。**这不是风险事件**，永远不进事件表、不发告警。

    单独成函数就是为了让这一点在代码结构上无法被搞错：它没有 severity，
    也不返回 CombinedEvent。
    """
    if not account or not market:
        return None
    mkt_state = market.get("market_state")
    if mkt_state not in (STRESSED, CAUTIOUS):
        return None
    acct = ((account.get("calculated_metrics", {}) or {}).get("account", {}) or {})
    cash = _d(acct.get("cash_ratio"))
    thr = Decimal(str(threshold))
    if cash is None or cash <= thr:
        return None
    return {
        "code": B_CASH_BUFFER, "kind": "buffer",
        "title": "较高现金比例提供缓冲",
        "explanation": (f"现金占总资产 {cash:.0%}（参考线 {thr:.0%}），"
                        f"在 {mkt_state} 环境下提供腾挪空间与补仓能力。"),
        "actual_value": str(cash), "threshold_value": str(thr), "unit": "ratio",
    }
