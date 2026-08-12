"""风险结果标签（纯函数）。

回测要回答「规则触发后，风险是否真的发生了」，就必须先把「风险发生」定义清楚。
本模块用**透明、配置化**的阈值定义，不用任何机器学习黑箱 —— 一个说不清自己
为什么报警的风控系统，用户没有理由信任它。

四条纪律：
1. **标签定义在回测开始前固定**。禁止根据回测结果回头改定义 —— 那是用结论
   反推前提，得到的 precision 没有任何意义。
2. **不同类型的规则用不同的结果定义**。市场规则不该用「账户回撤」评价，
   数据质量规则更不该用涨跌评价。用一个统一的「涨跌正确率」评价全部规则，
   会让数据质量规则永远看起来很糟。
3. **每个标签都带 calculation_version**。定义改了，历史结果不能混用。
4. **主动减仓不算规则失败**。用户看到告警后减了仓、损失因此变小，是规则**起作用**
   的证据，不是规则无效。这类窗口单独标记，不计入 precision 的分母否定项。
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

CALC_VERSION = "labels-v1/2026-07"

# 标签族
FAM_ACCOUNT = "account_drawdown"
FAM_POSITION = "position_loss"
FAM_MARKET = "market_deterioration"
FAM_LIQUIDITY = "liquidity_stress"


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
class OutcomeLabel:
    """一个结果标签的完整定义。落库时原样保存，供事后审计。"""
    label_code: str
    family: str
    definition: str
    threshold: Decimal
    unit: str
    #: 该标签适用于评价哪些来源的规则。空表示通用。
    applies_to: tuple[str, ...] = ()
    calculation_version: str = CALC_VERSION

    def as_dict(self) -> dict:
        return {"label_code": self.label_code, "family": self.family,
                "definition": self.definition, "threshold": str(self.threshold),
                "unit": self.unit, "applies_to": list(self.applies_to),
                "calculation_version": self.calculation_version}


def default_labels(cfg=None) -> list[OutcomeLabel]:
    """默认标签集。阈值可经 .env 覆盖，覆盖后 calculation_version 应随之调整。"""
    def t(name: str, fallback: str) -> Decimal:
        v = getattr(cfg, name, None) if cfg is not None else None
        return Decimal(str(v)) if v not in (None, "") else Decimal(fallback)

    return [
        # ---- 账户回撤 ----
        OutcomeLabel("ACCOUNT_DD_1PCT", FAM_ACCOUNT,
                     "窗口内账户最大回撤达到 1%", t("label_acct_dd_1", "0.01"), "ratio",
                     ("account", "position", "combined", "behavior")),
        OutcomeLabel("ACCOUNT_DD_2PCT", FAM_ACCOUNT,
                     "窗口内账户最大回撤达到 2%", t("label_acct_dd_2", "0.02"), "ratio",
                     ("account", "position", "combined", "behavior")),
        OutcomeLabel("ACCOUNT_DD_3PCT", FAM_ACCOUNT,
                     "窗口内账户最大回撤达到 3%", t("label_acct_dd_3", "0.03"), "ratio",
                     ("account", "position", "combined", "behavior")),
        OutcomeLabel("ACCOUNT_DD_5PCT", FAM_ACCOUNT,
                     "窗口内账户最大回撤达到 5%", t("label_acct_dd_5", "0.05"), "ratio",
                     ("account", "position", "combined", "behavior")),
        # ---- 持仓损失 ----
        OutcomeLabel("POSITION_MAE_5PCT", FAM_POSITION,
                     "该持仓窗口内最大不利变化达到 5%",
                     t("label_pos_mae_5", "0.05"), "ratio", ("position",)),
        OutcomeLabel("POSITION_MAE_10PCT", FAM_POSITION,
                     "该持仓窗口内最大不利变化达到 10%",
                     t("label_pos_mae_10", "0.10"), "ratio", ("position",)),
        OutcomeLabel("POSITION_MAE_15PCT", FAM_POSITION,
                     "该持仓窗口内最大不利变化达到 15%",
                     t("label_pos_mae_15", "0.15"), "ratio", ("position",)),
        OutcomeLabel("POSITION_DRAG_1PCT", FAM_POSITION,
                     "该持仓对账户净值的负贡献超过 1%",
                     t("label_pos_drag", "0.01"), "ratio", ("position",)),
        # ---- 市场恶化 ----
        OutcomeLabel("MARKET_QQQ_DD_5PCT", FAM_MARKET,
                     "窗口内 QQQ 最大回撤达到 5%",
                     t("label_qqq_dd", "0.05"), "ratio", ("market", "combined")),
        OutcomeLabel("MARKET_QQQ_BELOW_MA50", FAM_MARKET,
                     "窗口内 QQQ 跌破 50 日均线", Decimal("0"), "boolean",
                     ("market", "combined")),
        OutcomeLabel("MARKET_ENTERS_STRESSED", FAM_MARKET,
                     "窗口内市场环境进入 stressed", Decimal("0"), "boolean",
                     ("market", "combined")),
        OutcomeLabel("MARKET_VIX_ABOVE_30", FAM_MARKET,
                     "窗口内 VIX 触及严重风险阈值",
                     t("label_vix_crit", "30"), "level", ("market", "combined")),
        # ---- 流动性 ----
        OutcomeLabel("LIQ_EXCESS_BELOW_15PCT", FAM_LIQUIDITY,
                     "剩余流动性比例低于 15%",
                     t("label_liq_excess", "0.15"), "ratio",
                     ("account", "combined")),
        OutcomeLabel("LIQ_MAINT_BUFFER_LOW", FAM_LIQUIDITY,
                     "维持保证金安全倍数降到 1.5 倍以下",
                     t("label_maint_buffer", "1.5"), "multiple",
                     ("account", "combined")),
        OutcomeLabel("LIQ_LEVERAGE_UP", FAM_LIQUIDITY,
                     "窗口内总杠杆进一步上升 0.2x 以上",
                     t("label_leverage_up", "0.2"), "multiple",
                     ("account", "combined")),
    ]


def labels_for_source(source_type: str, labels: list[OutcomeLabel] | None = None
                      ) -> list[OutcomeLabel]:
    """某类来源的规则该用哪些标签评价。

    数据质量规则**不返回任何标签** —— 「数据坏了」的正确性由数据本身证明，
    用市场涨跌评价它毫无意义，硬套只会得到一个看起来很差的假指标。
    """
    labels = labels or default_labels()
    if source_type == "data_quality":
        return []
    return [l for l in labels if not l.applies_to or source_type in l.applies_to]


def default_label_for(source_type: str) -> str | None:
    """每类来源的主标签（排行榜默认口径）。"""
    return {
        "account": "ACCOUNT_DD_2PCT",
        "position": "POSITION_MAE_10PCT",
        "market": "MARKET_QQQ_DD_5PCT",
        "combined": "ACCOUNT_DD_2PCT",
        "behavior": "ACCOUNT_DD_2PCT",
    }.get(source_type)


# ---------------- 标签判定 ----------------

def evaluate_label(label: OutcomeLabel, outcome: dict) -> bool | None:
    """窗口结果是否命中该标签。

    返回 None 表示**该窗口没有足够数据判定** —— 与「未命中」是两件事，
    混为一谈会让 precision 的分母悄悄变大，把数据缺失算成规则误报。
    """
    code = label.label_code
    thr = label.threshold

    if label.family == FAM_ACCOUNT:
        dd = _d(outcome.get("account_max_drawdown"))
        return None if dd is None else dd >= thr

    if label.family == FAM_POSITION:
        if code == "POSITION_DRAG_1PCT":
            drag = _d(outcome.get("position_contribution_to_account"))
            return None if drag is None else drag <= -thr
        mae = _d(outcome.get("position_mae"))
        return None if mae is None else mae >= thr

    if label.family == FAM_MARKET:
        if code == "MARKET_QQQ_DD_5PCT":
            dd = _d(outcome.get("qqq_max_drawdown"))
            return None if dd is None else dd >= thr
        if code == "MARKET_QQQ_BELOW_MA50":
            v = outcome.get("qqq_below_ma50_in_window")
            return None if v is None else bool(v)
        if code == "MARKET_ENTERS_STRESSED":
            v = outcome.get("market_entered_stressed")
            return None if v is None else bool(v)
        if code == "MARKET_VIX_ABOVE_30":
            vix = _d(outcome.get("vix_max"))
            return None if vix is None else vix >= thr

    if label.family == FAM_LIQUIDITY:
        if code == "LIQ_EXCESS_BELOW_15PCT":
            v = _d(outcome.get("excess_liquidity_ratio_min"))
            return None if v is None else v < thr
        if code == "LIQ_MAINT_BUFFER_LOW":
            v = _d(outcome.get("maint_buffer_min"))
            return None if v is None else v < thr
        if code == "LIQ_LEVERAGE_UP":
            v = _d(outcome.get("leverage_increase"))
            return None if v is None else v >= thr
    return None


def evaluate_all(outcome: dict, source_type: str,
                 labels: list[OutcomeLabel] | None = None) -> dict[str, bool | None]:
    """窗口结果 -> {label_code: 命中/未命中/无法判定}。"""
    return {l.label_code: evaluate_label(l, outcome)
            for l in labels_for_source(source_type, labels)}


def label_by_code(code: str,
                  labels: list[OutcomeLabel] | None = None) -> OutcomeLabel | None:
    for l in (labels or default_labels()):
        if l.label_code == code:
            return l
    return None
