"""告警策略（纯函数，无 I/O）。

设计前提：**告警疲劳比漏报更容易毁掉一个风控系统**。一个每天响 30 次的看板，
用户三天后就会静音它，那时真正的 critical 也一起被静音了。所以第一版默认只推送
少量真正需要人介入的事件，warning 一律只进时间线。

主动推送的六种情形（全部可配置）：
    1. 新出现的 critical
    2. 新出现的 high
    3. high 升级为 critical（即使仍在冷静期）
    4. 已通知过的事件恢复
    5. 数据质量导致整个风险评估 blocked
    6. IBKR 或市场数据长时间不可用

冷静期语义沿用既有 alerting.py：**状态变化才通知**。持续存在的风险每天最多一条
摘要；数值波动不触发通知。这套语义已经在 R1–R7 上线验证过，不重写。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from .risk_events import (SEV_CRITICAL, SEV_HIGH, SEV_WARNING, ST_RESOLVED,
                          ST_SUPPRESSED, TRANS_ESCALATED, TRANS_OPENED,
                          TRANS_REOPENED, TRANS_RESOLVED, severity_rank)

# 通知类型（落库到 risk_notifications.notification_type）
N_OPENED = "opened"
N_ESCALATED = "escalated"
N_REMINDER = "reminder"
N_RECOVERED = "recovered"
N_BLOCKED = "blocked"
N_TEST = "test"

# 不通知的原因（用于日志与测试断言，不落库）
SKIP_SEVERITY = "severity_below_policy"
SKIP_COOLDOWN = "within_cooldown"
SKIP_SUPPRESSED = "event_suppressed"
SKIP_RATE_LIMIT = "rate_limited"
SKIP_QUIET_HOURS = "quiet_hours"
SKIP_LEGACY_OWNER = "owned_by_legacy_channel"
SKIP_NOT_NOTIFIED = "never_notified_no_recovery"
SKIP_DISABLED = "channel_disabled"
SKIP_NO_CHANGE = "no_state_change"


@dataclass(frozen=True)
class NotifyPolicy:
    """全部策略项均可由 .env 覆盖，默认值偏保守。"""
    notify_high: bool = True
    notify_critical: bool = True
    notify_warning: bool = False            # 默认不推 warning —— 避免告警疲劳
    notify_recovery: bool = True
    notify_data_quality: bool = True
    #: 同一事件的冷静期。升级会绕过它。
    cooldown: timedelta = timedelta(hours=4)
    max_per_hour: int = 6
    max_per_day: int = 20
    merge_batch: bool = True                # 同一轮的多条事件合并成一条
    notify_outside_market_hours: bool = True
    #: 发送失败重试次数（不含首次）。**有上限**，绝不无限重试。
    max_retries: int = 2
    retry_backoff_seconds: int = 5


DEFAULT_POLICY = NotifyPolicy()


def policy_from_config(cfg) -> NotifyPolicy:
    def b(name: str, default: bool) -> bool:
        return bool(getattr(cfg, name, default))

    def i(name: str, default: int) -> int:
        try:
            return int(getattr(cfg, name, default))
        except (TypeError, ValueError):
            return default

    return NotifyPolicy(
        notify_high=b("alert_notify_high", True),
        notify_critical=b("alert_notify_critical", True),
        notify_warning=b("alert_notify_warning", False),
        notify_recovery=b("alert_notify_recovery", True),
        notify_data_quality=b("alert_notify_data_quality", True),
        cooldown=timedelta(minutes=i("alert_cooldown_minutes", 240)),
        max_per_hour=i("alert_max_per_hour", 6),
        max_per_day=i("alert_max_per_day", 20),
        merge_batch=b("alert_merge_batch", True),
        notify_outside_market_hours=b("alert_outside_market_hours", True),
        max_retries=i("alert_max_retries", 2),
        retry_backoff_seconds=i("alert_retry_backoff_seconds", 5),
    )


def _severity_enabled(sev: str, policy: NotifyPolicy) -> bool:
    if sev == SEV_CRITICAL:
        return policy.notify_critical
    if sev == SEV_HIGH:
        return policy.notify_high
    if sev == SEV_WARNING:
        return policy.notify_warning
    return False


@dataclass
class NotifyDecision:
    should: bool
    notification_type: str | None = None
    reason: str = ""

    def __bool__(self) -> bool:      # 便于 `if decision:`
        return self.should


def decide(decision, event_row: dict, *, policy: NotifyPolicy, now: datetime,
           sent_last_hour: int = 0, sent_today: int = 0,
           in_market_hours: bool = True) -> NotifyDecision:
    """某个生命周期决策要不要发通知。

    decision: risk_events.LifecycleDecision
    event_row: 该事件在库中的当前行（可能为 None 表示刚新建）
    """
    row = event_row or {}
    sev = decision.new_severity

    # 归 legacy 链路的事件（R1–R7）运营层不推 —— 否则同一风险双发。
    # 技术面事件只进时间线、从不推送，同样在此拦下。
    if (row.get("notification_owner") or "ops") in ("legacy", "technical"):
        return NotifyDecision(False, reason=SKIP_LEGACY_OWNER)

    # ---- 恢复通知 ----
    if decision.transition == TRANS_RESOLVED:
        if not policy.notify_recovery:
            return NotifyDecision(False, reason=SKIP_DISABLED)
        # 从没通知过的事件不需要「恢复」通知 —— 用户根本不知道它发生过
        if not row.get("last_notified_at"):
            return NotifyDecision(False, reason=SKIP_NOT_NOTIFIED)
        return NotifyDecision(True, N_RECOVERED, reason="事件恢复")

    # ---- 抑制中 ----
    if (row.get("status") == ST_SUPPRESSED
            and not _suppression_expired(row.get("suppressed_until"), now)):
        return NotifyDecision(False, reason=SKIP_SUPPRESSED)

    # ---- 升级：绕过冷静期，但仍受严重度开关约束 ----
    if decision.transition == TRANS_ESCALATED:
        if not _severity_enabled(sev, policy):
            return NotifyDecision(False, reason=SKIP_SEVERITY)
        return NotifyDecision(True, N_ESCALATED, reason="严重度升级")

    # ---- 新事件 / 新一轮 episode ----
    if decision.transition in (TRANS_OPENED, TRANS_REOPENED):
        if not _severity_enabled(sev, policy):
            return NotifyDecision(False, reason=SKIP_SEVERITY)
        if _in_cooldown(row.get("last_notified_at"), now, policy.cooldown):
            return NotifyDecision(False, reason=SKIP_COOLDOWN)
        if not policy.notify_outside_market_hours and not in_market_hours:
            return NotifyDecision(False, reason=SKIP_QUIET_HOURS)
        if sent_last_hour >= policy.max_per_hour or sent_today >= policy.max_per_day:
            return NotifyDecision(False, reason=SKIP_RATE_LIMIT)
        return NotifyDecision(True, N_OPENED, reason="新增风险事件")

    # 其余（updated / deescalated）：状态没变，不打扰
    return NotifyDecision(False, reason=SKIP_NO_CHANGE)


def _in_cooldown(last_notified_at: str | None, now: datetime,
                 cooldown: timedelta) -> bool:
    if not last_notified_at:
        return False
    try:
        last = datetime.fromisoformat(last_notified_at)
    except (TypeError, ValueError):
        return False
    if last.tzinfo is None:
        last = last.replace(tzinfo=now.tzinfo)
    return (now - last) < cooldown


def _suppression_expired(until: str | None, now: datetime) -> bool:
    if not until:
        return False
    try:
        t = datetime.fromisoformat(until)
    except (TypeError, ValueError):
        return True
    if t.tzinfo is None:
        t = t.replace(tzinfo=now.tzinfo)
    return t <= now


def dedupe_key(event_id: str, notification_type: str) -> str:
    return f"{event_id}:{notification_type}"


def batch_dedupe_key(event_ids: list[str], notification_type: str) -> str:
    """合并通知的去重键：按事件集合而非单个事件，同一批不会重复发。"""
    import hashlib
    joined = ",".join(sorted(event_ids))
    return f"batch:{notification_type}:{hashlib.sha256(joined.encode()).hexdigest()[:12]}"


def group_for_merge(pending: list) -> list[list]:
    """把同一轮待发通知按「类型 + 来源」合并。

    合并的判断依据：QQQ 跌破 20 日均线、跌破 50 日均线、VXN 上升、市场转为 stressed
    是**同一件事的四个侧面**，发四条飞书只会让人关掉通知。恢复通知与新增通知
    不合并 —— 「好消息和坏消息」混在一条里会让人误读。

    pending: [(decision, notify_decision, trigger, event_row), ...]
    """
    buckets: dict[tuple, list] = {}
    for item in pending:
        nd = item[1]
        trig = item[2]
        key = (nd.notification_type, _merge_domain(trig.source_type))
        buckets.setdefault(key, []).append(item)
    # 按严重度排序，最严重的批次先发
    out = list(buckets.values())
    out.sort(key=lambda g: -max(severity_rank(i[0].new_severity) for i in g))
    return out


def _merge_domain(source_type: str) -> str:
    """市场与数据质量各自成域；账户与持仓合成一域（都是「我的账户怎么了」）。"""
    return {"market": "market", "data_quality": "data_quality"}.get(
        source_type, "account")
