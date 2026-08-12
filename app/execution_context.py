"""副作用执行上下文。**让只读探测在结构上无法改状态。**

起因是一次真实事故：一个「只读探测」脚本调用了 `process_assessments`，
传了 `notifier=None` 就以为安全 —— 结果不完整的市场上下文让 7 条仍在触发的
事件被判恢复，正常同步随后把它们重开，并向用户误发了一条飞书告警。

教训不是「下次记得别调用那个函数」。**靠记性的规则迟早会失守**，
尤其在排查问题的深夜。所以这里把它变成结构约束：

  - 有副作用的入口必须显式传 `ExecutionContext`
  - 上下文必须来自**已知的正式来源**（scheduler / sync_worker /
    manual_api / automated_test）
  - `allow_side_effects` 必须显式为 True
  - 缺任何一项都抛 `SideEffectNotAllowed`，而不是「默默照做」

`notifier=None` **不代表只读** —— 事件登记、episode 开关、alert_state
全都不经过 notifier。这是那次事故最直接的误判，所以这里专门写出来。

要看「如果现在评估会发生什么」，用 `preview_assessments()` ——
它跑完整规则并返回将会发生的 transition，但一行都不写。
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone


class SideEffectNotAllowed(RuntimeError):
    """在没有合法执行上下文的情况下试图产生副作用。

    这个异常是**设计的一部分**，不是缺陷 —— 它把一次静默的数据损坏
    变成一次响亮的失败。
    """


# ---------------- 允许产生副作用的正式来源 ----------------
SRC_SCHEDULER = "scheduler"
SRC_SYNC_WORKER = "sync_worker"
SRC_MANUAL_API = "manual_api"
SRC_AUTOMATED_TEST = "automated_test"

ALLOWED_SOURCES = (SRC_SCHEDULER, SRC_SYNC_WORKER, SRC_MANUAL_API,
                   SRC_AUTOMATED_TEST)

#: 诊断脚本 / 管理 CLI 的默认来源。**不在允许列表里** ——
#: 需要副作用时必须显式改用正式来源并加 --allow-side-effects。
SRC_DIAGNOSTIC = "diagnostic"


@dataclass(frozen=True)
class ExecutionContext:
    """一次执行的身份与授权。

    `allow_side_effects` 默认 **False** —— 安全的那一侧才是默认值。
    """
    source: str
    allow_side_effects: bool = False
    correlation_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    initiated_by: str | None = None

    def require_side_effects(self, what: str = "写入") -> None:
        """副作用发生**之前**调用。不满足条件就抛，绝不静默降级。"""
        if not self.allow_side_effects:
            raise SideEffectNotAllowed(
                f"拒绝{what}：execution_context.allow_side_effects=False。"
                f"（source={self.source}，correlation_id={self.correlation_id}）"
                " 只读探测请改用 preview_assessments()。")
        if self.source not in ALLOWED_SOURCES:
            raise SideEffectNotAllowed(
                f"拒绝{what}：来源 {self.source!r} 不在允许列表 "
                f"{ALLOWED_SOURCES} 中。诊断脚本默认只读；"
                " 确需副作用时请显式声明正式来源。")


def read_only(source: str = SRC_DIAGNOSTIC, **kw) -> ExecutionContext:
    """只读上下文。诊断脚本与管理 CLI 的默认值。"""
    return ExecutionContext(source=source, allow_side_effects=False, **kw)


def scheduler_context(**kw) -> ExecutionContext:
    return ExecutionContext(source=SRC_SCHEDULER, allow_side_effects=True, **kw)


def sync_context(**kw) -> ExecutionContext:
    return ExecutionContext(source=SRC_SYNC_WORKER, allow_side_effects=True, **kw)


def manual_api_context(initiated_by: str | None = None, **kw) -> ExecutionContext:
    return ExecutionContext(source=SRC_MANUAL_API, allow_side_effects=True,
                            initiated_by=initiated_by, **kw)


def test_context(**kw) -> ExecutionContext:
    """自动化测试专用 —— 只应在隔离数据库上使用。"""
    return ExecutionContext(source=SRC_AUTOMATED_TEST, allow_side_effects=True,
                            **kw)


def require(ctx: "ExecutionContext | None", what: str = "写入") -> ExecutionContext:
    """守卫。**上下文缺失一律拒绝** —— 「没传就当允许」正是事故的成因。"""
    if ctx is None:
        raise SideEffectNotAllowed(
            f"拒绝{what}：缺少 execution_context。"
            " 有副作用的入口必须显式声明来源与授权；"
            " 只读探测请改用 preview_assessments()。"
            " 注意 notifier=None **不代表只读** —— 事件登记、episode 开关、"
            "alert_state 都不经过 notifier。")
    ctx.require_side_effects(what)
    return ctx


# ---------------- 执行审计 ----------------

def start_audit(db, ctx: ExecutionContext, *,
                now: datetime | None = None) -> str:
    """记录一次有副作用的执行开始。返回 execution_id。"""
    now = now or datetime.now(timezone.utc)
    try:
        db.start_side_effect_execution({
            "execution_id": ctx.correlation_id, "source": ctx.source,
            "correlation_id": ctx.correlation_id,
            "initiated_by": ctx.initiated_by,
            "started_at": now.isoformat()})
    except Exception:  # noqa: BLE001
        pass          # 审计失败不该阻断正常业务
    return ctx.correlation_id


def finish_audit(db, execution_id: str, *, affected_event_count: int = 0,
                 notification_count: int = 0, error: str | None = None,
                 now: datetime | None = None) -> None:
    now = now or datetime.now(timezone.utc)
    try:
        db.finish_side_effect_execution(
            execution_id, finished_at=now.isoformat(),
            affected_event_count=affected_event_count,
            notification_count=notification_count, error=error)
    except Exception:  # noqa: BLE001
        pass
