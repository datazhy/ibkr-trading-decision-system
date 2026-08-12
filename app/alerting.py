"""告警生命周期（纯函数）。

问题：R1/R2 这类持续存在的风险，用固定 4 小时去重窗口会一直重复推送同一条
——集中度 78% 昨天是这个数，今天还是这个数，每 4 小时叫一次只会让人关掉通知。

改为按**状态变化**通知：
  首次触发        -> 立即
  严重度升级      -> 立即（WARN -> CRIT）
  实际值显著恶化  -> 立即（阈值可配置）
  持续存在        -> 每个美东日一次摘要
  恢复正常        -> 立即
  恢复后再次触发  -> 立即（视作新的首次触发）

规则评估本身仍每分钟写 rule_runs，审计不受影响 —— 少发的是通知，不是记录。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

# 通知原因
FIRST_TRIGGER = "FIRST_TRIGGER"
ESCALATION = "ESCALATION"
WORSENED = "WORSENED"
DAILY_SUMMARY = "DAILY_SUMMARY"
RESOLVED = "RESOLVED"
RETRIGGER = "RETRIGGER"
SUPPRESSED = "SUPPRESSED"          # 不通知：状态未变化

_REASON_LABEL = {
    FIRST_TRIGGER: "首次触发",
    ESCALATION: "严重度升级",
    WORSENED: "实际值显著恶化",
    DAILY_SUMMARY: "持续存在（每日摘要）",
    RESOLVED: "已恢复正常",
    RETRIGGER: "恢复后再次触发",
}

_SEVERITY_RANK = {"INFO": 0, "WARN": 1, "CRIT": 2}


def severity_rank(s: str | None) -> int:
    return _SEVERITY_RANK.get((s or "").upper(), 0)


def reason_label(reason: str) -> str:
    return _REASON_LABEL.get(reason, reason)


#: 每日限一次推送的规则：这类「持续性风险」的数值整天都在小幅波动，
#: 恶化通知会变成变相的高频轰炸。集中度今天 78% 明天 84%，都是同一件事：仓位太重。
#: 升级（WARN→CRIT）与恢复仍然立即通知 —— 那是状态变化，不是数值抖动。
DAILY_CAPPED_RULES = frozenset({"R1", "R2"})


@dataclass(frozen=True)
class AlertPolicy:
    """「显著恶化」的判定阈值 + 再触发冷却（可配置）。

    两个口径取「或」：
      - 绝对变化：比例类指标（集中度、风险占比）变化超过 worsen_abs（默认 5 个百分点）
      - 相对变化：数值变化超过上次通知值的 worsen_rel（默认 20%）
    只在「变得更糟」的方向上触发；好转不推送（好转到恢复时由 RESOLVED 通知）。
    """
    worsen_abs: Decimal = Decimal("0.05")
    worsen_rel: Decimal = Decimal("0.20")
    #: 解除后多久内再次触发不重复推送。与规则层的滞回带互补：
    #: 滞回管「价格贴线时不来回改判」，冷却管「真的来回穿越时不重复通知」。
    retrigger_cooldown: timedelta = timedelta(minutes=30)


DEFAULT_POLICY = AlertPolicy()


def _d(v):
    if v is None:
        return None
    return v if isinstance(v, Decimal) else Decimal(str(v))


def worsened(previous, current, policy: AlertPolicy = DEFAULT_POLICY) -> bool:
    """当前值相对上次通知值是否「显著恶化」。

    约定：value 越大越糟（集中度、风险占比、亏损幅度都按绝对值口径传入）。
    """
    prev, cur = _d(previous), _d(current)
    if prev is None or cur is None:
        return False
    if cur <= prev:
        return False                      # 没变糟
    delta = cur - prev
    if delta >= policy.worsen_abs:
        return True
    if prev > 0 and (delta / prev) >= policy.worsen_rel:
        return True
    return False


def decide_notify(state, *, severity, value, now_et_date: str, rule_id: str | None = None,
                  now: datetime | None = None,
                  policy: AlertPolicy = DEFAULT_POLICY) -> tuple[bool, str]:
    """某条仍在触发的告警：这次要不要通知？

    state: alert_state 行（dict/Row），None = 从未见过。
    rule_id: 属于 DAILY_CAPPED_RULES（R1/R2）时，同一天内最多推一次 ——
             这类持续风险的数值整天在动，「显著恶化」会让它变相高频轰炸。
    now:     用于再触发冷却。价格在止损线附近来回穿越时，「解除→再触发」
             每次都推会打成机关枪（线上实测 DRAM 一支 10 条）。
    """
    if state is None:
        return True, FIRST_TRIGGER
    def get(k):
        try:
            return state[k]
        except (KeyError, IndexError, TypeError):
            return None
    if get("state") == "RESOLVED":
        # 冷却期内的再触发：状态照常更新，只是不再推送
        resolved_at = get("resolved_at")
        if now is not None and resolved_at:
            try:
                gap = now - datetime.fromisoformat(resolved_at)
            except (TypeError, ValueError):
                gap = None
            if gap is not None and gap < policy.retrigger_cooldown:
                return False, SUPPRESSED
        return True, RETRIGGER
    # 升级是状态变化，任何规则都立即通知
    if severity_rank(severity) > severity_rank(get("notified_severity")):
        return True, ESCALATION

    capped = rule_id in DAILY_CAPPED_RULES
    already_today = get("last_summary_date") == now_et_date
    if capped and already_today:
        return False, SUPPRESSED          # R1/R2 今天已经说过了，不再重复
    if not capped and worsened(get("notified_value"), value, policy):
        return True, WORSENED
    if not already_today:
        return True, DAILY_SUMMARY
    return False, SUPPRESSED


def decide_resolve(state) -> bool:
    """某条告警本次未触发：是否需要发「已恢复」通知。"""
    if state is None:
        return False
    def get(k):
        try:
            return state[k]
        except (KeyError, IndexError, TypeError):
            return None
    return get("state") == "ACTIVE"
