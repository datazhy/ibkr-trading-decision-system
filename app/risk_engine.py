"""风险规则层 + 可解释评分。

与指标层（risk_metrics）严格分离：指标算事实，规则下判断。这样阈值可以随时改，
历史快照也能用新阈值重算 —— 如果把 30% 写进计算函数，这两件事都做不到。

评分刻意做成**可解释的加权分档**，不用任何机器学习：
风险评分的唯一价值是回答「为什么今天是高风险」。一个不能拆解的数字，
在真要做决策时没人敢用。

风险级别与数据可靠性**严格分开**：
    blocked   数据不足以做可靠判断 —— 不是「没风险」，是「不知道」
    critical  数据可靠，且触发了严重风险规则
把两者混为一谈，会让一次同步故障看起来像「账户很安全」，那是最危险的误导。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from . import risk_metrics as rm

#: v2 起集中度规则带适用前提，并新增 HHI / 等效持仓数 / 单仓主导度。
#: **口径变了版本必须跟着变** —— assess() 按版本幂等，不改版本会一直返回
#: 旧口径算好的缓存，新规则永远不会生效。
CALC_VERSION = "risk-v2-conc/2026-07"
LEGACY_CALC_VERSIONS = ("risk-v1/2026-07",)

# ---- 风险级别 ----
LOW = "low"
MODERATE = "moderate"
HIGH = "high"
CRITICAL = "critical"
BLOCKED = "blocked"

# ---- 严重程度 ----
SEV_WARNING = "warning"
SEV_HIGH = "high"
SEV_CRITICAL = "critical"

_SEV_WEIGHT = {SEV_WARNING: 1, SEV_HIGH: 2, SEV_CRITICAL: 3}

# ---- 规则分类（同时是评分维度）----
CAT_ACCOUNT = "account"
CAT_CONCENTRATION = "concentration"
CAT_LEVERAGE = "leverage"
CAT_DRAWDOWN = "drawdown"
CAT_LIQUIDITY = "liquidity"
CAT_LOSS = "loss"
CAT_POSITION = "position"
CAT_DATA = "data_quality"

# 比较符
GT = "gt"        # 实际值 > 阈值即触发
LT = "lt"        # 实际值 < 阈值即触发


@dataclass(frozen=True)
class RiskRule:
    rule_code: str
    name: str
    category: str
    description: str
    severity: str
    threshold: Decimal | None
    comparator: str
    calculation_basis: str          # 指标取自哪个字段，写死在这里便于审计
    unit: str = "ratio"             # ratio / multiple / count / currency
    enabled: bool = True
    version: str = CALC_VERSION
    scope: str = "account"          # account / position
    #: 该规则至少需要多少个**有效多头持仓**才有判别力。
    #: Top3 在只有 3 只持仓的账户上恒等于 100% —— 那不是风险信号，是算术必然。
    #: 生产实测：Top3/Top5 在本账户上 17/17 天触发，纯噪音。
    min_long_positions: int = 0
    #: 不适用时给用户看的解释。必须说清「为什么这个指标在这里没有意义」。
    not_applicable_note: str = ""

    def as_dict(self) -> dict:
        return {
            "rule_code": self.rule_code, "name": self.name, "category": self.category,
            "description": self.description, "enabled": self.enabled,
            "severity": self.severity,
            "threshold": None if self.threshold is None else str(self.threshold),
            "comparator": self.comparator, "calculation_basis": self.calculation_basis,
            "unit": self.unit, "scope": self.scope, "version": self.version,
            "min_long_positions": self.min_long_positions,
        }


# ---------------- 规则结果状态 ----------------
# 一条规则跑完之后有五种可能，把它们区分开是本次降噪的核心：
#   triggered         触发了
#   not_triggered     没触发（指标在阈值内）
#   not_applicable    **前提不成立，这个指标在当前组合上没有判别力**
#   blocked           数据质量导致不允许出结论
#   insufficient_data 指标算不出来
#: 当日亏损规则的 not_triggered 措辞。「没亏」与「亏了但没超」必须分开说。
REASON_NO_DAILY_LOSS = "当日未发生亏损"
REASON_DAILY_LOSS_WITHIN = "当日亏损未超过阈值"

OUTCOME_TRIGGERED = "triggered"
OUTCOME_NOT_TRIGGERED = "not_triggered"
OUTCOME_NOT_APPLICABLE = "not_applicable"
OUTCOME_BLOCKED = "blocked"
OUTCOME_INSUFFICIENT = "insufficient_data"


@dataclass
class RuleOutcome:
    """一条规则在一次评估中的结果。**不适用的规则不产生事件、不计分、不通知**，
    但必须留下记录 —— 否则用户会以为这条规则「通过了」。"""
    rule_code: str
    category: str
    outcome: str
    severity: str | None = None
    actual_value: Any = None
    threshold_value: Any = None
    reason: str = ""
    applicable: bool = True

    def as_dict(self) -> dict:
        return {
            "rule_code": self.rule_code, "category": self.category,
            "outcome": self.outcome, "severity": self.severity,
            "actual_value": None if self.actual_value is None else str(self.actual_value),
            "threshold_value": (None if self.threshold_value is None
                                else str(self.threshold_value)),
            "reason": self.reason, "applicable": self.applicable,
        }


@dataclass
class RiskEvent:
    event_code: str
    severity: str
    title: str
    explanation: str
    actual_value: Any
    threshold_value: Any
    unit: str
    rule_code: str
    category: str
    affected_symbol: str | None = None
    blocks_ai: bool = False
    recommended_check: str = ""      # 建议用户检查什么，**不是**交易指令

    def as_dict(self, snapshot_id: str | None = None, detected_at: str | None = None) -> dict:
        return {
            "event_code": self.event_code, "severity": self.severity, "title": self.title,
            "explanation": self.explanation,
            "actual_value": None if self.actual_value is None else str(self.actual_value),
            "threshold_value": (None if self.threshold_value is None
                                else str(self.threshold_value)),
            "unit": self.unit, "affected_symbol": self.affected_symbol,
            "rule_code": self.rule_code, "category": self.category,
            "snapshot_id": snapshot_id, "blocks_ai": self.blocks_ai,
            "recommended_check": self.recommended_check, "detected_at": detected_at,
        }


# ======================= 默认规则集 =======================
# 阈值默认值与 rules.RuleThresholds 对齐（R1 单标的 30% / R2 板块 40%），
# 避免同一件事在告警与评估里有两个标准。

def default_rules(cfg=None) -> list[RiskRule]:
    """构造规则集。阈值可由 cfg 覆盖（全部 risk_* 配置项）。"""
    def t(name: str, fallback: str) -> Decimal:
        v = getattr(cfg, name, None) if cfg is not None else None
        return Decimal(str(v)) if v is not None else Decimal(fallback)

    def i(name: str, fallback: int) -> int:
        v = getattr(cfg, name, None) if cfg is not None else None
        try:
            return int(v) if v is not None else fallback
        except (TypeError, ValueError):
            return fallback

    return [
        # ---------- 账户层 ----------
        RiskRule("ACC_CASH_LOW", "现金比例过低", CAT_LIQUIDITY,
                 "现金占净资产比例过低，应对回撤与追缴的缓冲不足",
                 SEV_WARNING, t("risk_cash_ratio_min", "0.05"), LT, "cash_ratio"),
        RiskRule("ACC_GROSS_LEVERAGE_HIGH", "总杠杆过高", CAT_LEVERAGE,
                 "总敞口相对净资产倍数过高，价格波动会被放大",
                 SEV_HIGH, t("risk_gross_leverage_max", "2.0"), GT, "gross_leverage",
                 unit="multiple"),
        RiskRule("ACC_NET_LEVERAGE_HIGH", "净杠杆过高", CAT_LEVERAGE,
                 "净敞口相对净资产倍数过高",
                 SEV_HIGH, t("risk_net_leverage_max", "1.5"), GT, "net_leverage",
                 unit="multiple"),
        RiskRule("ACC_MAINT_MARGIN_BUFFER_LOW", "维持保证金缓冲过低", CAT_LIQUIDITY,
                 "净资产相对维持保证金的安全倍数过低，接近追缴线",
                 SEV_CRITICAL, t("risk_maint_safety_min", "2.0"), LT,
                 "maint_margin_safety_multiple", unit="multiple"),
        RiskRule("ACC_EXCESS_LIQUIDITY_LOW", "剩余流动性过低", CAT_LIQUIDITY,
                 "剩余流动性占净资产比例过低",
                 SEV_HIGH, t("risk_excess_liquidity_min", "0.10"), LT,
                 "excess_liquidity_ratio"),
        RiskRule("ACC_DAILY_LOSS_HIGH", "当日亏损超过阈值", CAT_LOSS,
                 "当日亏损占净资产比例超过阈值",
                 SEV_HIGH, t("risk_daily_loss_max", "0.03"), GT, "daily_loss_ratio"),
        RiskRule("ACC_DRAWDOWN_HIGH", "当前回撤超过阈值", CAT_DRAWDOWN,
                 "从历史峰值的回撤超过阈值",
                 SEV_HIGH, t("risk_drawdown_max", "0.15"), GT, "current_drawdown"),
        RiskRule("ACC_MAX_DRAWDOWN_HIGH", "历史最大回撤超过阈值", CAT_DRAWDOWN,
                 "历史最大回撤超过阈值（反映策略的历史承压幅度）",
                 SEV_WARNING, t("risk_max_drawdown_max", "0.25"), GT, "max_drawdown"),
        RiskRule("DATA_QUALITY_INSUFFICIENT", "数据质量不足", CAT_DATA,
                 "存在严重数据质量问题，风险结论不可靠",
                 SEV_CRITICAL, None, GT, "quality_status", unit="status"),
        RiskRule("DATA_SEVERELY_STALE", "数据严重过期", CAT_DATA,
                 "数据过期时间超过可接受范围",
                 SEV_CRITICAL, None, GT, "quality_status", unit="status"),

        # ---------- 集中度层 ----------
        RiskRule("CONC_SINGLE_EQUITY_HIGH", "单只持仓占总资产过高", CAT_CONCENTRATION,
                 "单只持仓占净资产比例超过阈值（与 R1 同口径）",
                 SEV_HIGH, t("risk_single_weight_equity_max", "0.30"), GT,
                 "max_position_weight_of_equity"),
        RiskRule("CONC_SINGLE_INVESTED_HIGH", "单只持仓占已投资仓位过高", CAT_CONCENTRATION,
                 "单只持仓占已投资资金比例超过阈值 —— 高现金账户尤其要看这个口径",
                 SEV_WARNING, t("risk_single_weight_invested_max", "0.50"), GT,
                 "max_position_weight_of_invested", min_long_positions=1,
                 not_applicable_note="没有有效多头持仓，无法判断单仓集中度"),
        # Top-N 只在持仓数明显多于 N 时才有判别力。持仓 3 只时 Top3 恒为 100%，
        # 那是算术必然而非风险信号 —— 生产实测 17/17 天触发，纯噪音。
        RiskRule("CONC_TOP3_HIGH", "前三大持仓集中度过高", CAT_CONCENTRATION,
                 "前三大持仓合计占已投资仓位比例过高",
                 SEV_WARNING, t("risk_top3_invested_max", "0.80"), GT,
                 "top3_weight_of_invested",
                 min_long_positions=i("risk_top3_min_position_count", 6),
                 not_applicable_note=(
                     "有效多头持仓不足，Top3 占比天然接近 100%，"
                     "该指标不具备区分能力")),
        RiskRule("CONC_TOP5_HIGH", "前五大持仓集中度过高", CAT_CONCENTRATION,
                 "前五大持仓合计占已投资仓位比例过高",
                 SEV_WARNING, t("risk_top5_invested_max", "0.95"), GT,
                 "top5_weight_of_invested",
                 min_long_positions=i("risk_top5_min_position_count", 10),
                 not_applicable_note=(
                     "有效多头持仓不足，Top5 占比天然接近 100%，"
                     "该指标不具备区分能力")),
        # 以下三条替 Top-N 承担少持仓账户的集中度判断 —— 它们不随持仓数量失真
        RiskRule("CONC_HHI_HIGH", "组合集中度指数过高", CAT_CONCENTRATION,
                 "HHI = Σ权重²，衡量整体集中程度，不随持仓数量变化而失真",
                 SEV_HIGH, t("risk_hhi_threshold", "0.40"), GT, "hhi",
                 unit="index", min_long_positions=i("risk_hhi_min_positions", 2),
                 not_applicable_note="只有一只有效持仓，HHI 恒为 1，无判别力"),
        RiskRule("EFFECTIVE_POSITION_COUNT_LOW", "等效持仓数过少", CAT_CONCENTRATION,
                 "1/HHI = 等效等权持仓数。名义上多只、风险权重却相当于很少几只",
                 SEV_WARNING, t("risk_effective_position_count_min", "3"), LT,
                 "effective_position_count", unit="count",
                 min_long_positions=i("risk_effective_min_positions", 2),
                 not_applicable_note="只有一只有效持仓，等效持仓数恒为 1，无判别力"),
        RiskRule("SINGLE_POSITION_DOMINANCE", "单一持仓主导组合", CAT_CONCENTRATION,
                 "最大持仓占比高**且**明显甩开第二名 —— 组合实际由它一只决定",
                 SEV_HIGH, t("risk_dominance_gap_min", "0.20"), GT,
                 "dominance_gap", min_long_positions=2,
                 not_applicable_note="持仓少于 2 只，无法判断主导关系"),
        RiskRule("CONC_UNKNOWN_SECTOR_HIGH", "未知分类持仓比例过高", CAT_CONCENTRATION,
                 "未映射板块的持仓占比过高，板块集中度判断会失真",
                 SEV_WARNING, t("risk_unknown_sector_max", "0.30"), GT,
                 "unknown_sector_ratio"),

        # ---------- 单只持仓层 ----------
        RiskRule("POS_UNREALIZED_LOSS_HIGH", "单只持仓浮亏超过阈值", CAT_POSITION,
                 "该持仓浮亏占其成本比例超过阈值",
                 SEV_WARNING, t("risk_position_loss_max", "0.20"), GT,
                 "unrealized_pnl_percent", scope="position"),
        RiskRule("POS_LOSS_POOL_SHARE_HIGH", "单只持仓占账户亏损的比例过高", CAT_POSITION,
                 "该持仓占全部亏损仓位浮亏合计的比例过高，亏损高度集中于一只标的",
                 SEV_WARNING, t("risk_loss_contribution_max", "0.60"), GT,
                 "contribution_to_total_losing_positions", scope="position"),
        RiskRule("POS_EXCEEDS_EQUITY", "持仓市值超过账户净资产", CAT_POSITION,
                 "单只持仓市值超过账户净资产（隐含杠杆）",
                 SEV_CRITICAL, Decimal("1.0"), GT, "weight_of_equity", scope="position"),
        RiskRule("POS_PRICE_UNAVAILABLE", "持仓价格不可用", CAT_POSITION,
                 "该持仓无有效市价，其风险指标不可信",
                 SEV_HIGH, None, GT, "risk_flags", unit="flag", scope="position"),
        RiskRule("POS_COST_UNAVAILABLE", "成本数据不可用", CAT_POSITION,
                 "该持仓无成本价，盈亏与偏离度无法计算",
                 SEV_WARNING, None, GT, "risk_flags", unit="flag", scope="position"),
        RiskRule("POS_QTY_ANOMALY", "持仓数量异常", CAT_POSITION,
                 "持仓数量为零但市值非零",
                 SEV_HIGH, None, GT, "risk_flags", unit="flag", scope="position"),
        RiskRule("POS_UNEXPLAINED_CHANGE", "大额持仓突变无成交记录", CAT_POSITION,
                 "相邻快照间持仓大幅变化，但没有对应成交记录",
                 SEV_HIGH, None, GT, "position_change", unit="flag", scope="position"),
    ]


# ======================= 规则评估 =======================

def _loss_ratio(daily_pnl_ratio):
    """当日盈亏比 -> 当日亏损率。

    · 亏损（<0）-> 取正值，参与阈值比较
    · 盈利或打平（>=0）-> **0**，是「未发生亏损」这个明确结论
    · 缺字段（None）-> None，才是真正的 insufficient_data

    区分后两者很重要：把盈利说成「指标无法计算」会让用户以为系统坏了，
    也会让 insufficient_rule_count 这类统计失真。
    """
    if daily_pnl_ratio is None:
        return None
    return -daily_pnl_ratio if daily_pnl_ratio < 0 else Decimal("0")


def _fmt(v: Any, unit: str) -> str:
    """人读的展示值。比例 -> 百分比，倍数 -> Nx，其余原样。"""
    if v is None:
        return "—"
    if not isinstance(v, Decimal):
        try:
            v = Decimal(str(v))
        except Exception:  # noqa: BLE001 — flag 类值本就不是数字
            return str(v)
    if unit == "ratio":
        return f"{v:.1%}"
    if unit == "multiple":
        return f"{v:.2f}x"
    if unit == "currency":
        return f"{v:,.2f}"
    # 兜底：count/index 等未特别处理的单位也必须取整，绝不把原始 Decimal 精度抖到叙事里
    s = f"{v.quantize(Decimal('0.01')):,.2f}"
    return s.rstrip("0").rstrip(".") if "." in s else s


def evaluate_rules(metrics: dict, rules: list[RiskRule], *,
                   quality_status: str = "fresh",
                   unexplained_changes: dict[str, str] | None = None,
                   collect_outcomes: bool = False):
    """按规则集评估，产出触发事件。

    指标为 None（算不出来）时**不触发** —— 缺失不等于超标。这类情况由数据质量层
    单独报告，混进风险规则会制造大量假阳性。

    **适用性前置判定**：规则的 min_long_positions 不满足时判 not_applicable ——
    不产生事件、不计分、不通知，但保留记录。Top3 在只有 3 只持仓的账户上恒等于
    100%，把它报成风险是在制造噪音，而噪音会让人关掉整个系统。

    collect_outcomes=True 时返回 (events, outcomes)，否则只返回 events
    （向后兼容既有调用方）。
    """
    events: list[RiskEvent] = []
    outcomes: list[RuleOutcome] = []
    by_code = {r.rule_code: r for r in rules if r.enabled}
    acct = metrics.get("account", {})
    conc = metrics.get("concentration", {})
    liq = metrics.get("liquidity", {})
    dd = metrics.get("drawdown", {})
    pnl = metrics.get("pnl", {})

    # 账户级数值规则
    numeric_sources = {
        "cash_ratio": acct.get("cash_ratio"),
        "gross_leverage": acct.get("gross_leverage"),
        "net_leverage": acct.get("net_leverage"),
        "excess_liquidity_ratio": acct.get("excess_liquidity_ratio"),
        "maint_margin_safety_multiple": liq.get("maint_margin_safety_multiple"),
        "current_drawdown": dd.get("current_drawdown"),
        "max_drawdown": dd.get("max_drawdown"),
        "max_position_weight_of_equity": conc.get("max_position_weight_of_equity"),
        "max_position_weight_of_invested": conc.get("max_position_weight_of_invested"),
        "top3_weight_of_invested": conc.get("top3_weight_of_invested"),
        "top5_weight_of_invested": conc.get("top5_weight_of_invested"),
        "hhi": conc.get("hhi"),
        "effective_position_count": conc.get("effective_position_count"),
        "dominance_gap": conc.get("dominance_gap"),
        "unknown_sector_ratio": conc.get("unknown_sector_ratio"),
        # 当日亏损：只有亏损方向才参与判断（盈利不该触发「亏损过大」）。
        # **盈利时取 0，不是 None** —— 取证发现 89 个盈利样本因为拿到 None 被标成
        # insufficient_data，理由写「指标无法计算」。当日盈利是一个明确结论，
        # 不是数据缺失；只有真的缺字段（daily_pnl_ratio 为 None）才算缺失。
        "daily_loss_ratio": _loss_ratio(pnl.get("daily_pnl_ratio")),
    }

    checks = {
        "ACC_CASH_LOW": ("现金缓冲偏薄，回撤或追缴时腾挪空间小",
                         "确认是否为有意保留的低现金策略；检查近期是否有大额买入"),
        "ACC_GROSS_LEVERAGE_HIGH": ("总敞口被放大，波动对净资产的影响成倍",
                                    "核对是否使用了融资；检查各持仓规模是否符合计划"),
        "ACC_NET_LEVERAGE_HIGH": ("净方向暴露过大", "检查多空是否失衡"),
        "ACC_MAINT_MARGIN_BUFFER_LOW": ("接近保证金追缴线，市场下跌可能触发强平",
                                        "立即核对保证金要求与可用资金；确认券商端的追缴状态"),
        "ACC_EXCESS_LIQUIDITY_LOW": ("剩余流动性薄，应对波动能力弱",
                                     "核对未结算资金与近期出入金安排"),
        "ACC_DAILY_LOSS_HIGH": ("当日亏损相对账户规模偏大",
                                "回看当日成交与触发原因，确认是否偏离交易计划"),
        "ACC_DRAWDOWN_HIGH": ("当前净值距历史峰值回撤较大",
                              "检查回撤是否来自单一标的；确认止损纪律是否被执行"),
        "ACC_MAX_DRAWDOWN_HIGH": ("历史最大回撤较深",
                                  "评估该回撤幅度是否在可承受范围内"),
        "CONC_SINGLE_EQUITY_HIGH": ("单一标的占账户比重过大",
                                    "确认该仓位规模是否符合仓位管理规则"),
        "CONC_SINGLE_INVESTED_HIGH": ("已投资资金高度集中在一只标的",
                                      "注意：占总资产比例可能看起来不高，但一旦满仓风险会显著放大"),
        "CONC_TOP3_HIGH": ("前三大持仓占据绝大部分投资仓位",
                           "检查这几只标的是否高度相关（同板块/同驱动因素）"),
        "CONC_TOP5_HIGH": ("持仓分散度不足", "评估是否需要分散到更多标的"),
        "CONC_HHI_HIGH": ("组合集中度指数偏高，风险高度集中在少数标的",
                          "查看等效持仓数与最大单仓占比，确认是否符合仓位计划"),
        "EFFECTIVE_POSITION_COUNT_LOW": (
            "等效持仓数偏低 —— 名义上的分散并未转化为真实的风险分散",
            "检查各仓位权重是否失衡；确认是否存在一两只票主导组合"),
        "SINGLE_POSITION_DOMINANCE": (
            "单一持仓明显主导组合，其涨跌几乎等同于账户涨跌",
            "确认该仓位规模是否符合单笔风险预算；检查是否有减仓计划"),
        "CONC_UNKNOWN_SECTOR_HIGH": ("较多持仓未映射板块，板块集中度可能被低估",
                                     "补充 config/sectors.json 的板块映射"),
    }

    eligible_longs = conc.get("eligible_long_position_count")
    if eligible_longs is None:
        eligible_longs = conc.get("long_count") or 0

    for code, rule in by_code.items():
        if rule.scope != "account" or rule.category == CAT_DATA:
            continue

        # ---- 适用性前置判定 ----
        if rule.min_long_positions and eligible_longs < rule.min_long_positions:
            outcomes.append(RuleOutcome(
                rule_code=code, category=rule.category,
                outcome=OUTCOME_NOT_APPLICABLE, applicable=False,
                actual_value=numeric_sources.get(rule.calculation_basis),
                threshold_value=rule.threshold,
                reason=(f"{rule.name}不适用：当前仅有 {eligible_longs} 个有效多头持仓"
                        f"（该规则至少需要 {rule.min_long_positions} 个）。"
                        f"{rule.not_applicable_note}")))
            continue

        val = numeric_sources.get(rule.calculation_basis)
        if val is None or rule.threshold is None:
            outcomes.append(RuleOutcome(
                rule_code=code, category=rule.category,
                outcome=OUTCOME_INSUFFICIENT,
                threshold_value=rule.threshold,
                reason="指标无法计算，缺失不等于超标"))
            continue

        # SINGLE_POSITION_DOMINANCE 是**双条件**：占比高 且 明显甩开第二名。
        # 只看差值会把两只各占 40%/20% 的组合误判成"被主导"。
        if code == "SINGLE_POSITION_DOMINANCE":
            top1 = conc.get("top1_weight")
            floor = _dominance_floor(rules)
            if top1 is None or top1 <= floor:
                outcomes.append(RuleOutcome(
                    rule_code=code, category=rule.category,
                    outcome=OUTCOME_NOT_TRIGGERED, actual_value=val,
                    threshold_value=rule.threshold,
                    reason=(f"最大单仓占比 {_fmt(top1, 'ratio')} 未超过主导门槛 "
                            f"{_fmt(floor, 'ratio')}，不构成单一主导")))
                continue

        hit = (val > rule.threshold) if rule.comparator == GT else (val < rule.threshold)
        if not hit:
            # 当日亏损规则：0 意味着当天根本没亏，与「亏了但没超阈值」是两件事，
            # 用户看事件详情时该能区分出来。
            reason = ""
            if code == "ACC_DAILY_LOSS_HIGH":
                reason = (REASON_NO_DAILY_LOSS if val == 0
                          else REASON_DAILY_LOSS_WITHIN)
            outcomes.append(RuleOutcome(
                rule_code=code, category=rule.category,
                outcome=OUTCOME_NOT_TRIGGERED, actual_value=val,
                threshold_value=rule.threshold, reason=reason))
            continue

        expl, check = checks.get(code, (rule.description, "请核对相关数据"))
        events.append(RiskEvent(
            event_code=code, severity=rule.severity, title=rule.name,
            explanation=f"{expl}（当前 {_fmt(val, rule.unit)}，阈值 {_fmt(rule.threshold, rule.unit)}）",
            actual_value=val, threshold_value=rule.threshold, unit=rule.unit,
            rule_code=code, category=rule.category, recommended_check=check))
        outcomes.append(RuleOutcome(
            rule_code=code, category=rule.category, outcome=OUTCOME_TRIGGERED,
            severity=rule.severity, actual_value=val,
            threshold_value=rule.threshold))

    # 单只持仓规则
    for p in metrics.get("positions", []) or []:
        sym = p.get("symbol")
        flags = set(p.get("risk_flags") or [])
        upl_pct = p.get("unrealized_pnl_percent")
        loss_pct = -upl_pct if (upl_pct is not None and upl_pct < 0) else None
        contrib = p.get("contribution_to_total_losing_positions")
        w_eq = p.get("weight_of_equity")

        pos_checks = [
            ("POS_UNREALIZED_LOSS_HIGH", loss_pct,
             "该持仓浮亏较大", "回看买入依据是否仍成立；确认止损位是否已设定并被遵守"),
            ("POS_LOSS_POOL_SHARE_HIGH", contrib,
             "账户的亏损高度集中于该标的", "检查该标的的风险敞口是否超出单笔风险预算"),
            ("POS_EXCEEDS_EQUITY", w_eq,
             "该持仓市值超过账户净资产", "确认是否使用融资，并核对保证金要求"),
        ]
        for code, val, expl, check in pos_checks:
            rule = by_code.get(code)
            if rule is None or val is None or rule.threshold is None:
                continue
            if not ((val > rule.threshold) if rule.comparator == GT else (val < rule.threshold)):
                continue
            events.append(RiskEvent(
                event_code=code, severity=rule.severity, title=f"{sym} {rule.name}",
                explanation=f"{expl}（当前 {_fmt(val, rule.unit)}，阈值 {_fmt(rule.threshold, rule.unit)}）",
                actual_value=val, threshold_value=rule.threshold, unit=rule.unit,
                rule_code=code, category=rule.category, affected_symbol=sym,
                recommended_check=check))

        flag_rules = {
            "PRICE_UNAVAILABLE": ("POS_PRICE_UNAVAILABLE", "该标的无有效市价，其风险指标不可信",
                                  "检查行情订阅与该标的的交易状态（是否停牌/退市）"),
            "COST_UNAVAILABLE": ("POS_COST_UNAVAILABLE", "该标的无成本价",
                                 "核对持仓来源（转入持仓可能缺成本）"),
            "QTY_ANOMALY": ("POS_QTY_ANOMALY", "持仓数量为零但市值非零",
                            "核对券商端该标的的实际持仓"),
        }
        for flag, (code, expl, check) in flag_rules.items():
            if flag in flags and code in by_code:
                r = by_code[code]
                events.append(RiskEvent(
                    event_code=code, severity=r.severity, title=f"{sym} {r.name}",
                    explanation=expl, actual_value=flag, threshold_value=None,
                    unit="flag", rule_code=code, category=r.category,
                    affected_symbol=sym, recommended_check=check))

    # 持仓突变无成交
    for sym, detail in (unexplained_changes or {}).items():
        r = by_code.get("POS_UNEXPLAINED_CHANGE")
        if r:
            events.append(RiskEvent(
                event_code=r.rule_code, severity=r.severity, title=f"{sym} {r.name}",
                explanation=detail, actual_value=detail, threshold_value=None,
                unit="flag", rule_code=r.rule_code, category=r.category,
                affected_symbol=sym,
                recommended_check="核对券商端成交记录；确认是否为公司行动（拆股/合并）或数据同步问题"))
    return (events, outcomes) if collect_outcomes else events


def _dominance_floor(rules: list[RiskRule]) -> Decimal:
    """主导判定的占比门槛：复用单仓阈值，避免同一件事有两个标准。"""
    for r in rules:
        if r.rule_code == "CONC_SINGLE_INVESTED_HIGH" and r.threshold is not None:
            return r.threshold
    return Decimal("0.50")


def applicability_summary(outcomes: list[RuleOutcome], category: str | None = None
                          ) -> dict:
    """适用性摘要。让「为什么这个维度分数变了」随时可查。"""
    rows = [o for o in outcomes
            if category is None or o.category == category]
    na = [o for o in rows if o.outcome == OUTCOME_NOT_APPLICABLE]
    return {
        "applicable_rule_count": sum(1 for o in rows if o.applicable),
        "not_applicable_rule_count": len(na),
        "not_applicable": [{"rule_code": o.rule_code, "reason": o.reason}
                           for o in na],
        "triggered_rule_count": sum(1 for o in rows
                                    if o.outcome == OUTCOME_TRIGGERED),
        "insufficient_rule_count": sum(1 for o in rows
                                       if o.outcome == OUTCOME_INSUFFICIENT),
    }


# ======================= 评分 =======================

DEFAULT_WEIGHTS = {
    "concentration_score": Decimal("0.25"),
    "leverage_score": Decimal("0.20"),
    "drawdown_score": Decimal("0.20"),
    "liquidity_score": Decimal("0.20"),
    "loss_score": Decimal("0.10"),
    "data_quality_score": Decimal("0.05"),
}

_CAT_TO_SCORE = {
    CAT_CONCENTRATION: "concentration_score",
    CAT_LEVERAGE: "leverage_score",
    CAT_DRAWDOWN: "drawdown_score",
    CAT_LIQUIDITY: "liquidity_score",
    CAT_LOSS: "loss_score",
    CAT_DATA: "data_quality_score",
    CAT_POSITION: "concentration_score",   # 单仓风险计入集中度维度
    CAT_ACCOUNT: "liquidity_score",
}

#: 单条事件按严重度贡献的分值（0–100 制）
_EVENT_POINTS = {SEV_WARNING: Decimal("20"), SEV_HIGH: Decimal("40"),
                 SEV_CRITICAL: Decimal("70")}


NORM_ADDITIVE = "additive"
NORM_APPLICABLE = "normalized_over_applicable_rules"


def score(events: list[RiskEvent], *, weights: dict | None = None,
          quality_status: str = "fresh",
          outcomes: list[RuleOutcome] | None = None) -> dict:
    """分项评分 + 加权总分。0 = 无风险，100 = 极高风险。

    基础算法简单到可以口算：分项分 = Σ 该维度事件分值（上限 100）。

    **归一化**：给了 outcomes 时，分项分按「适用规则」重新归一 ——

        分项分 = 已触发分值 / 全部适用规则的满分 × 100

    为什么必须这样：Top3/Top5 在少持仓账户上判为 not_applicable 之后，
    它们不再贡献分值。如果还用绝对加总，同一个高度集中的账户会因为
    "少了两条规则触发" 而显得更安全 —— 那是把降噪做成了粉饰。
    归一化让「适用的检查里有多大比例在报警」成为分数的含义，
    规则数量变化不会改变风险的相对高低。
    """
    w = {**DEFAULT_WEIGHTS, **(weights or {})}
    comp = {k: Decimal("0") for k in DEFAULT_WEIGHTS}
    for e in events:
        key = _CAT_TO_SCORE.get(e.category, "liquidity_score")
        comp[key] = min(Decimal("100"), comp[key] + _EVENT_POINTS.get(e.severity, Decimal("20")))

    method = NORM_ADDITIVE
    norm_detail: dict = {}
    if outcomes:
        method = NORM_APPLICABLE
        by_key: dict[str, list[RuleOutcome]] = {}
        for o in outcomes:
            by_key.setdefault(_CAT_TO_SCORE.get(o.category, "liquidity_score"),
                              []).append(o)
        for key, rows in by_key.items():
            applicable = [o for o in rows if o.applicable]
            if not applicable:
                continue
            # 该维度所有适用规则如果全部触发能拿到的分
            max_points = sum(
                (_EVENT_POINTS.get(o.severity or _severity_of(o), Decimal("20"))
                 for o in applicable), Decimal("0"))
            if max_points <= 0:
                continue
            got = sum((_EVENT_POINTS.get(o.severity, Decimal("20"))
                       for o in applicable if o.outcome == OUTCOME_TRIGGERED),
                      Decimal("0"))
            normalized = (got / max_points * Decimal("100")).quantize(Decimal("0.1"))

            # **归一化只覆盖有 outcome 的规则。** 逐持仓规则（POS_*）是按标的
            # 循环评估的，不产出 outcome —— 直接覆写会把它们的分数抹掉，
            # 让一个持仓大额浮亏的账户显示成低风险。所以叠加而非替换。
            covered = {o.rule_code for o in rows}
            uncovered = sum(
                (_EVENT_POINTS.get(e.severity, Decimal("20")) for e in events
                 if _CAT_TO_SCORE.get(e.category, "liquidity_score") == key
                 and e.rule_code not in covered), Decimal("0"))
            comp[key] = min(Decimal("100"), normalized + uncovered)
            norm_detail[key] = {
                "applicable_rule_count": len(applicable),
                "not_applicable_rule_count": sum(
                    1 for o in rows if o.outcome == OUTCOME_NOT_APPLICABLE),
                "triggered_points": str(got), "max_points": str(max_points),
                "normalized_component": str(normalized),
                "uncovered_event_points": str(uncovered),
                "uncovered_note": ("逐持仓规则不参与归一化，其分值直接叠加"
                                   if uncovered else None),
            }

    if quality_status == "stale":
        comp["data_quality_score"] = max(comp["data_quality_score"], Decimal("40"))
    elif quality_status == "error":
        comp["data_quality_score"] = Decimal("100")

    total = sum((comp[k] * w.get(k, Decimal("0")) for k in comp), Decimal("0"))
    return {"component_scores": comp, "total_risk_score": total.quantize(Decimal("0.1")),
            "weights": w, "score_normalization_method": method,
            "normalization_detail": norm_detail}


#: 未触发的规则没有 severity，用规则集里的定义补 —— 否则满分基数会算错。
_RULE_SEVERITY_CACHE: dict[str, str] = {}


def _severity_of(o: RuleOutcome) -> str:
    if o.severity:
        return o.severity
    if not _RULE_SEVERITY_CACHE:
        for r in default_rules():
            _RULE_SEVERITY_CACHE[r.rule_code] = r.severity
    return _RULE_SEVERITY_CACHE.get(o.rule_code, SEV_WARNING)


def risk_level(total_score: Decimal, events: list[RiskEvent], *, reliable: bool) -> str:
    """风险级别。

    **blocked 优先于一切** —— 数据不可靠时不允许给出任何风险结论，
    包括「低风险」。把一次数据故障显示成 low risk 是本系统最不能犯的错。
    """
    if not reliable:
        return BLOCKED
    if any(e.severity == SEV_CRITICAL for e in events):
        return CRITICAL
    if total_score >= Decimal("60") or any(e.severity == SEV_HIGH for e in events):
        return HIGH
    if total_score >= Decimal("25"):
        return MODERATE
    return LOW


def top_risk_drivers(events: list[RiskEvent], limit: int = 5) -> list[dict]:
    """最大风险来源。按严重度排序，每条都带「是什么/当前值/阈值/该查什么」。"""
    ranked = sorted(events, key=lambda e: (-_SEV_WEIGHT.get(e.severity, 0), e.event_code))
    out = []
    for e in ranked[:limit]:
        out.append({
            "rule_code": e.rule_code, "title": e.title, "severity": e.severity,
            "category": e.category, "affected_symbol": e.affected_symbol,
            # 原值保留全精度（供 API 消费方自行处理），另给一份人读的展示值 ——
            # 页面直接印 Decimal 会出现 0.8204172146272926461282763820 这种噪音
            "actual_value": None if e.actual_value is None else str(e.actual_value),
            "threshold_value": (None if e.threshold_value is None
                                else str(e.threshold_value)),
            "actual_display": _fmt(e.actual_value, e.unit),
            "threshold_display": _fmt(e.threshold_value, e.unit),
            "unit": e.unit, "explanation": e.explanation,
            "recommended_check": e.recommended_check,
        })
    return out


# ======================= 规则集版本与哈希 =======================

def rule_set_hash(rules: list[RiskRule]) -> str:
    """规则集内容哈希。

    阈值或计算方式一变，哈希就变 —— 这是「历史评估必须能追溯当时口径」的锚点。
    只用**影响判定结果**的字段：改个描述文案不该产生新版本。
    """
    import hashlib
    parts = []
    for r in sorted(rules, key=lambda x: x.rule_code):
        if not r.enabled:
            continue
        parts.append("|".join([
            r.rule_code, r.category, r.severity, r.comparator,
            r.calculation_basis, r.unit,
            "" if r.threshold is None else str(r.threshold),
        ]))
    return hashlib.sha256("\n".join(parts).encode()).hexdigest()[:16]


#: 版本号分隔符。用 `~`（RFC 3986 的 unreserved 字符）而非 `#` ——
#: `#` 在 URL 里是片段分隔符，`?version=a#b` 传到服务端只剩 `a`，
#: 会让「按版本查历史规则」这个接口永远查不到带哈希的版本。
_VERSION_SEP = "~"


def rule_set_version(rules: list[RiskRule]) -> str:
    """版本号 = 算法版本 + 内容哈希。同算法下改阈值也会产生新版本。"""
    return f"{CALC_VERSION}{_VERSION_SEP}{rule_set_hash(rules)}"
