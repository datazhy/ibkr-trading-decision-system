"""当日盈亏的**原子观测**。

起因是一次严格取证：`ACC_DAILY_LOSS_HIGH` 在 24 小时内开合 11 次，其中 4 个
episode 只活了 1 分钟。取证结论不是「阈值附近随机抖动」——账户当时确实有 77%
的采样点处于 3% 以上亏损，峰值 16.24%。问题在**度量本身**：

  · `risk_service.build_context` 用 `db.latest_pnl(account)` 取分子，而本轮的
    pnl 快照要等到 `sync._write_pnl_snapshot` 才写。评估跑在写之前，于是
    **分子来自上一周期、分母来自本周期**。
  · 后果：720 对相邻样本里 707 对的一致性残差 >1，496 对「净值冻结但当日盈亏
    在跳」，40 对净值与盈亏方向互相矛盾（无出入金时这在经济上不可能）。
  · `DERIVED_portfolio+executions` 的 quality 是 DEGRADED，却没有向上传导 ——
    risk_assessment 对全部 721 个样本都记 fresh/reliable，规则完全不知道自己
    在用降级数据。

所以这里立一条硬规矩：

    **分子与分母必须来自同一次采集周期。**
    禁止「最新 daily_pnl ÷ 任意一条最近 net_liquidation」。
    即使两者单独都 fresh，跨周期拼接出来的比值也是假的。

拼不齐就标 `insufficient_data`，**绝不退回用旧分母凑一个数出来**。
缺失不等于恢复，也不等于超标 —— 它只是「这一轮没算出来」。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

#: 计算口径版本。配对规则或门禁一变就要升，历史观测据此追溯当时口径。
CALC_VERSION = "daily-pnl-obs-v2/2026-07"

# ---------------- 质量状态 ----------------
Q_OK = "OK"
Q_DEGRADED = "DEGRADED"
Q_INSUFFICIENT = "INSUFFICIENT"

# ---------------- 质量原因码 ----------------
#: 分子（daily_pnl）本身没取到
R_NO_NUMERATOR = "NUMERATOR_MISSING"
#: 分母（net_liquidation）没取到
R_NO_DENOMINATOR = "DENOMINATOR_MISSING"
#: 分子分母不属于同一次采集周期 —— 这是本次事故的直接成因
R_CROSS_CYCLE = "CROSS_CYCLE_PAIRING"
#: 同周期但两侧观测时刻相差超过容差
R_SKEW_EXCEEDED = "SKEW_EXCEEDED"
#: 分子分母的会话日不一致（跨交易日拼接）
R_SESSION_MISMATCH = "SESSION_DATE_MISMATCH"
#: 币种口径不一致
R_CURRENCY_MISMATCH = "CURRENCY_MISMATCH"
#: 来源本身是降级口径（DERIVED / 未校准净值变动）
R_DEGRADED_SOURCE = "DEGRADED_SOURCE"
#: 观测过期
R_STALE = "STALE_OBSERVATION"
#: 外部现金流覆盖未确认（推导口径下净值变动可能被出入金污染）
R_FLOW_UNCONFIRMED = "CASH_FLOW_COVERAGE_UNCONFIRMED"
#: 来源刚切换且新来源的首个样本没能闭合
R_SOURCE_SWITCH_UNCLOSED = "SOURCE_SWITCH_NOT_CLOSED"
#: 管道时序所致：风险评估跑在 PnL 快照写入**之前**，本轮分子此刻还不存在。
#: 这**不是数据问题** —— 同周期的分子分母确实存在，只是评估看不到。
#: 与真正的 NUMERATOR_MISSING 分开记，否则影子报告会把「时序」误读成「缺数据」。
#: 切换 v2 时必须先把 sync 里的写入顺序调到评估之前，这个原因码届时应当消失。
R_PIPELINE_ORDER = "PIPELINE_ORDER_PENDING"

#: 只要命中其中任何一条，就不是 reliable observation。
_FATAL = frozenset((R_NO_NUMERATOR, R_NO_DENOMINATOR, R_CROSS_CYCLE,
                    R_SKEW_EXCEEDED, R_SESSION_MISMATCH, R_CURRENCY_MISMATCH,
                    R_DEGRADED_SOURCE, R_STALE, R_SOURCE_SWITCH_UNCLOSED,
                    R_PIPELINE_ORDER))

#: 人话解释，给诊断 API 与日志用。
REASON_TEXT = {
    R_NO_NUMERATOR: "未取到当日盈亏",
    R_NO_DENOMINATOR: "未取到净资产",
    R_CROSS_CYCLE: "当日盈亏与净资产不属于同一次采集周期，禁止拼接",
    R_SKEW_EXCEEDED: "当日盈亏与净资产的观测时刻相差超过容差",
    R_SESSION_MISMATCH: "当日盈亏与净资产的交易日归属不一致",
    R_CURRENCY_MISMATCH: "当日盈亏与净资产的币种口径不一致",
    R_DEGRADED_SOURCE: "当日盈亏来自降级口径，不足以驱动风险状态迁移",
    R_STALE: "观测已过期",
    R_FLOW_UNCONFIRMED: "外部现金流覆盖未确认，净值变动口径可能被出入金污染",
    R_SOURCE_SWITCH_UNCLOSED: "来源刚切换且新来源首个样本未能闭合配对",
    R_PIPELINE_ORDER: ("管道时序：风险评估先于 PnL 快照写入，本轮分子此刻尚未落库"
                       "（非数据缺失；切换 v2 前需调整写入顺序）"),
}

# ---------------- 规则结果 ----------------
OUT_TRIGGERED = "triggered"
OUT_NOT_TRIGGERED = "not_triggered"
OUT_INSUFFICIENT = "insufficient_data"
OUT_BLOCKED = "blocked"

#: 盈利时的措辞。**不能说「指标无法计算」** —— 当日盈利是一个明确的结论，
#: 而不是数据缺失。取证时 89 个盈利样本被标成 insufficient_data，属误标。
REASON_NO_LOSS = "当日未发生亏损"
REASON_WITHIN = "当日亏损未超过阈值"


def _d(v) -> Decimal | None:
    if v is None or v == "":
        return None
    if isinstance(v, Decimal):
        return v
    try:
        return Decimal(str(v))
    except Exception:  # noqa: BLE001
        return None


def _dt(v) -> datetime | None:
    if v is None or v == "":
        return None
    if isinstance(v, datetime):
        return v if v.tzinfo else v.replace(tzinfo=timezone.utc)
    try:
        d = datetime.fromisoformat(str(v))
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except Exception:  # noqa: BLE001
        return None


@dataclass(frozen=True)
class DailyPnlObservation:
    """一次当日盈亏观测。**分子与分母的身份各自独立记录**，好让「是不是同一
    周期」这件事永远可被事后验证，而不是靠调用方自觉。"""
    account_id: str | None
    session_date: str | None
    observed_at: str | None
    correlation_id: str | None
    source: str | None
    basis: str | None
    daily_pnl: Decimal | None
    daily_pnl_observed_at: str | None
    net_liquidation: Decimal | None
    net_liquidation_observed_at: str | None
    numerator_snapshot_id: str | None
    denominator_snapshot_id: str | None
    observation_skew_seconds: Decimal | None
    external_cash_flow_adjustment: Decimal | None
    daily_loss_ratio: Decimal | None
    quality_status: str
    reliable: bool
    quality_reasons: tuple[str, ...]
    previous_source: str | None = None
    current_source: str | None = None
    source_changed: bool = False
    basis_changed: bool = False
    calculation_version: str = CALC_VERSION
    created_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat())

    # ---- 派生判断 ----

    @property
    def event_transition_allowed(self) -> bool:
        """能否驱动事件的打开/恢复。**只有 reliable observation 才可以。**"""
        return self.reliable

    @property
    def event_transition_block_reason(self) -> str | None:
        if self.reliable:
            return None
        return "；".join(REASON_TEXT.get(r, r) for r in self.quality_reasons) or "质量不足"

    def as_row(self) -> dict:
        return {
            "account_id": self.account_id, "session_date": self.session_date,
            "observed_at": self.observed_at, "correlation_id": self.correlation_id,
            "source": self.source, "basis": self.basis,
            "daily_pnl": None if self.daily_pnl is None else str(self.daily_pnl),
            "daily_pnl_observed_at": self.daily_pnl_observed_at,
            "net_liquidation": (None if self.net_liquidation is None
                                else str(self.net_liquidation)),
            "net_liquidation_observed_at": self.net_liquidation_observed_at,
            "numerator_snapshot_id": self.numerator_snapshot_id,
            "denominator_snapshot_id": self.denominator_snapshot_id,
            "observation_skew_seconds": (None if self.observation_skew_seconds is None
                                         else str(self.observation_skew_seconds)),
            "external_cash_flow_adjustment": (
                None if self.external_cash_flow_adjustment is None
                else str(self.external_cash_flow_adjustment)),
            "daily_loss_ratio": (None if self.daily_loss_ratio is None
                                 else str(self.daily_loss_ratio)),
            "quality_status": self.quality_status, "reliable": self.reliable,
            "quality_reasons": list(self.quality_reasons),
            "previous_source": self.previous_source,
            "current_source": self.current_source,
            "source_changed": self.source_changed,
            "basis_changed": self.basis_changed,
            "calculation_version": self.calculation_version,
            "created_at": self.created_at,
        }

    def log_fields(self) -> dict:
        """结构化日志字段。**不含账户号，也不含金额** —— 定位问题只需要口径、
        时间偏差和门禁结论，把真实持仓金额写进日志没有必要。"""
        return {
            "correlation_id": self.correlation_id, "source": self.source,
            "basis": self.basis, "session_date": self.session_date,
            "observation_skew_seconds": (
                None if self.observation_skew_seconds is None
                else float(self.observation_skew_seconds)),
            "reliable": self.reliable,
            "quality_status": self.quality_status,
            "quality_reasons": ",".join(self.quality_reasons) or "-",
            "source_changed": self.source_changed,
            "event_transition_allowed": self.event_transition_allowed,
            "event_transition_block_reason": self.event_transition_block_reason or "-",
        }


# ======================= 构建 =======================

#: 已知的降级来源/口径。命中即 DEGRADED，默认不得驱动状态迁移。
DEGRADED_SOURCES = frozenset(("DERIVED_portfolio+executions",))
DEGRADED_BASES = frozenset(("NET_LIQ_CHANGE_UNADJUSTED",))


def build(*, account_id: str | None,
          numerator: dict | None, denominator: dict | None,
          observed_at: str | None = None,
          correlation_id: str | None = None,
          previous_source: str | None = None,
          previous_basis: str | None = None,
          skew_tolerance_seconds: int = 90,
          max_age_seconds: int = 600,
          numerator_pending: bool = False,
          now: datetime | None = None) -> DailyPnlObservation:
    """把一对（分子、分母）闭合成一次观测。**纯函数，不碰数据库。**

    `numerator` 需要：daily_pnl / source / basis / quality / observed_at /
    correlation_id / snapshot_id / session_date / currency / flow_coverage_ok
    `denominator` 需要：net_liquidation / observed_at / correlation_id /
    snapshot_id / session_date / currency

    两侧的 correlation_id 都是**必填**语义：拿不到就当跨周期处理，因为
    「不知道是不是同一批」和「确定不是同一批」在风险上是一回事。
    """
    now = now or datetime.now(timezone.utc)
    num = numerator or {}
    den = denominator or {}
    reasons: list[str] = []

    pnl = _d(num.get("daily_pnl"))
    nl = _d(den.get("net_liquidation"))
    src = num.get("source")
    basis = num.get("basis")
    cur_src, cur_basis = src, basis

    n_at, d_at = _dt(num.get("observed_at")), _dt(den.get("observed_at"))
    n_corr = num.get("correlation_id")
    d_corr = den.get("correlation_id")
    corr = correlation_id or n_corr or d_corr

    # ---- 同周期配对：这是整个模块存在的理由 ----
    # `numerator_pending` 表示「分子本轮会写、只是还没写」——那是管道时序，
    # 不是跨周期拼接，也不是数据缺失。混在一起记会让影子报告完全读不懂。
    if numerator_pending:
        reasons.append(R_PIPELINE_ORDER)
    else:
        same_cycle = bool(n_corr) and bool(d_corr) and n_corr == d_corr
        if not same_cycle:
            reasons.append(R_CROSS_CYCLE)

    skew = None
    if n_at is not None and d_at is not None:
        skew = Decimal(str(abs((n_at - d_at).total_seconds())))
        if skew > Decimal(str(skew_tolerance_seconds)):
            reasons.append(R_SKEW_EXCEEDED)

    # ---- 各自存在性 ----
    if pnl is None and not numerator_pending:
        reasons.append(R_NO_NUMERATOR)
    if nl is None or nl == 0:
        reasons.append(R_NO_DENOMINATOR)

    # ---- 会话日一致 ----
    n_sess, d_sess = num.get("session_date"), den.get("session_date")
    session_date = n_sess or d_sess
    if n_sess and d_sess and n_sess != d_sess:
        reasons.append(R_SESSION_MISMATCH)

    # ---- 币种一致 ----
    n_ccy, d_ccy = num.get("currency"), den.get("currency")
    if n_ccy and d_ccy and n_ccy != d_ccy:
        reasons.append(R_CURRENCY_MISMATCH)

    # ---- 新鲜度 ----
    newest = max((x for x in (n_at, d_at) if x is not None), default=None)
    if newest is not None and (now - newest).total_seconds() > max_age_seconds:
        reasons.append(R_STALE)

    # ---- 来源降级 ----
    if (src in DEGRADED_SOURCES or basis in DEGRADED_BASES
            or num.get("quality") == Q_DEGRADED):
        reasons.append(R_DEGRADED_SOURCE)
    if basis in DEGRADED_BASES and num.get("flow_coverage_ok") is not True:
        reasons.append(R_FLOW_UNCONFIRMED)

    # ---- 来源切换 ----
    # 只有在**知道**当前来源时才谈得上切换。分子还没到手（src 为 None）时
    # 报「来源变了」纯属噪音 —— 部署验证时就撞上过这个伪标记。
    source_changed = bool(previous_source) and bool(src) and previous_source != src
    basis_changed = bool(previous_basis) and previous_basis != basis
    # 切换本身不等于数据无效，但新来源的首个样本必须**独立**闭合。
    # 没闭合就明确记下来 —— 否则事后分不清「切换导致的」和「本来就缺的」。
    if source_changed and any(r in _FATAL for r in reasons):
        reasons.append(R_SOURCE_SWITCH_UNCLOSED)

    reliable = not any(r in _FATAL for r in reasons)
    if reliable:
        quality = Q_OK
    elif R_DEGRADED_SOURCE in reasons and pnl is not None and nl:
        # 数字算得出来，只是口径降级 —— 与「彻底没有」要区分开
        quality = Q_DEGRADED
    else:
        quality = Q_INSUFFICIENT

    # ---- 亏损率 ----
    # **盈利不是缺失**：daily_pnl >= 0 时亏损率就是 0，是个明确结论。
    ratio = None
    if pnl is not None and nl:
        ratio = (-pnl / nl) if pnl < 0 else Decimal("0")

    return DailyPnlObservation(
        account_id=account_id, session_date=session_date,
        observed_at=observed_at or (newest.isoformat() if newest else None),
        correlation_id=corr, source=src, basis=basis,
        daily_pnl=pnl,
        daily_pnl_observed_at=n_at.isoformat() if n_at else None,
        net_liquidation=nl,
        net_liquidation_observed_at=d_at.isoformat() if d_at else None,
        numerator_snapshot_id=num.get("snapshot_id"),
        denominator_snapshot_id=den.get("snapshot_id"),
        observation_skew_seconds=skew,
        external_cash_flow_adjustment=_d(num.get("external_cash_flow_adjustment")),
        daily_loss_ratio=ratio, quality_status=quality, reliable=reliable,
        quality_reasons=tuple(dict.fromkeys(reasons)),
        previous_source=previous_source, current_source=cur_src,
        source_changed=source_changed, basis_changed=basis_changed,
    )


# ======================= 规则结果 =======================

def outcome_for(obs: DailyPnlObservation, threshold: Decimal
                ) -> tuple[str, Decimal | None, str]:
    """由观测推出 `ACC_DAILY_LOSS_HIGH` 的结果。返回 (outcome, ratio, reason)。

    四态严格区分：

      · reliable 且 ratio > 阈值 -> triggered
      · reliable 且 ratio <= 阈值 -> not_triggered（盈利与「亏了但没超」都在这里，
        但 reason 不同 —— 用户该能看出是哪一种）
      · 观测缺失 / DEGRADED / 跨周期 / skew 超限 -> insufficient_data
      · 观测被显式阻断 -> blocked

    **insufficient_data 绝不解释为恢复。** 它既不打开事件，也不关闭事件。
    """
    if not obs.reliable or obs.daily_loss_ratio is None:
        return (OUT_INSUFFICIENT, obs.daily_loss_ratio,
                obs.event_transition_block_reason or "指标无法闭合计算")
    ratio = obs.daily_loss_ratio
    if ratio > threshold:
        return OUT_TRIGGERED, ratio, ""
    if obs.daily_pnl is not None and obs.daily_pnl >= 0:
        return OUT_NOT_TRIGGERED, ratio, REASON_NO_LOSS
    return OUT_NOT_TRIGGERED, ratio, REASON_WITHIN


# ======================= 影子比较 =======================

DIV_SAME = "same"
DIV_RATIO_ONLY = "ratio_differs_same_outcome"
DIV_OUTCOME = "outcome_differs"
DIV_OLD_ONLY = "old_computable_only"
DIV_NEW_ONLY = "new_computable_only"
DIV_BOTH_NONE = "both_incomputable"


def classify_divergence(old_ratio, new_ratio, old_outcome, new_outcome) -> str:
    o, n = _d(old_ratio), _d(new_ratio)
    if o is None and n is None:
        return DIV_BOTH_NONE
    if n is None:
        return DIV_OLD_ONLY
    if o is None:
        return DIV_NEW_ONLY
    if old_outcome != new_outcome:
        return DIV_OUTCOME
    return DIV_SAME if o == n else DIV_RATIO_ONLY


def comparison_row(*, obs: DailyPnlObservation, old_ratio, old_quality: str,
                   old_outcome: str, threshold: Decimal,
                   old_source: str | None = None) -> dict:
    """一行影子比较。`*_would_transition` 记的是「**如果**由这一侧驱动，本轮
    是否允许状态迁移」—— 影子期不真的迁移，只记录。"""
    new_outcome, new_ratio, _ = outcome_for(obs, threshold)
    old_would = old_outcome in (OUT_TRIGGERED, OUT_NOT_TRIGGERED)
    return {
        "observed_at": obs.observed_at, "session_date": obs.session_date,
        "correlation_id": obs.correlation_id,
        "old_source": old_source, "new_source": obs.source,
        "old_ratio": None if old_ratio is None else str(old_ratio),
        "new_ratio": None if new_ratio is None else str(new_ratio),
        "old_quality": old_quality, "new_quality": obs.quality_status,
        "old_outcome": old_outcome, "new_outcome": new_outcome,
        "divergence_type": classify_divergence(old_ratio, new_ratio,
                                               old_outcome, new_outcome),
        "old_would_transition": old_would,
        "new_would_transition": obs.event_transition_allowed,
        "calculation_version": obs.calculation_version,
    }
