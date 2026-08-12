"""历史重放上下文（**只读**）。

为下一阶段的账户规则历史重放准备：按 snapshot_id 或时点重建「当时的世界」——
当时的账户、持仓、风险指标、有效规则版本、可用现金流、数据质量。

**这个模块最重要的性质是它什么都不写。** 历史重放绝不能：
  · 发送飞书通知
  · 创建或修改实时 open episode
  · 推进告警冷静期水位
  · 触碰任何实时状态

所以这里连 db 的写方法都不调用。`assert_read_only()` 提供一道显式检查，
调用方在 historical_replay 模式下必须先过它 —— 把约束写成可执行的代码，
比写在文档里可靠。
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from . import cashflow_model as cm
from .logging_setup import get_logger

log = get_logger()

MODE_LIVE = "live"
MODE_REPLAY = "historical_replay"


class ReplayViolation(RuntimeError):
    """历史重放模式下试图产生副作用。"""


def assert_read_only(mode: str, action: str) -> None:
    """在会产生副作用的地方调用。重放模式下直接抛异常。

    刻意抛而不是静默跳过：静默跳过会让「重放意外发了通知」这类 bug
    在测试里也看不出来。
    """
    if mode == MODE_REPLAY:
        raise ReplayViolation(
            f"历史重放模式禁止该操作：{action}。"
            "重放只允许读取，不得发送通知、创建实时事件或修改冷静期。")


class NullNotifier:
    """重放模式专用的通知器。调用即记录，永不真的发出去。"""

    def __init__(self):
        self.suppressed: list[tuple[str, str]] = []

    def send(self, title: str, body: str) -> bool:
        self.suppressed.append((title, body))
        log.debug("历史重放：已拦截一条通知（%s）", title[:40])
        return False        # 明确返回未发送，避免上游记成「已通知」


def build_context(db, *, snapshot_id: str | None = None,
                  as_of: str | None = None, account: str | None = None,
                  cfg=None) -> dict:
    """重建某个历史时点的完整上下文。**纯读取。**

    snapshot_id 与 as_of 二选一；两者都给时以 snapshot_id 为准。
    """
    ctx: dict = {"mode": MODE_REPLAY, "snapshot_id": snapshot_id,
                 "as_of": as_of, "account": account, "missing": []}

    # ---- 账户快照 ----
    row = None
    try:
        if snapshot_id:
            row = db.latest_snapshot_row(snapshot_id=snapshot_id)
        elif as_of:
            rows = db.equity_series_between("0000", as_of, account=account)
            row = rows[-1] if rows else None
    except Exception:  # noqa: BLE001
        row = None
    if row is None:
        ctx["missing"].append("account_snapshot")
    ctx["balances"] = dict(row) if row else None
    ts = (row or {}).get("ts") or as_of
    ctx["as_of"] = ts

    # ---- 持仓 ----
    try:
        ctx["positions"] = (db.positions_for_snapshot(snapshot_id)
                            if snapshot_id else db.positions_as_of(ts))
    except Exception:  # noqa: BLE001
        ctx["positions"] = []
        ctx["missing"].append("positions")

    # ---- 当时的风险评估（不重算，取当时落库的那一份）----
    try:
        ctx["risk_assessment"] = db.assessment_as_of(ts, account=account)
    except Exception:  # noqa: BLE001
        ctx["risk_assessment"] = None
    if ctx["risk_assessment"] is None:
        ctx["missing"].append("risk_assessment")

    # ---- 当时的市场评估 ----
    try:
        day = str(ts or "")[:10]
        series = db.market_score_series(day, day)
        ctx["market_assessment"] = series[-1] if series else None
    except Exception:  # noqa: BLE001
        ctx["market_assessment"] = None

    # ---- 当时有效的规则版本 ----
    ctx["rule_set_version"] = (ctx.get("risk_assessment") or {}).get(
        "rule_set_version")
    try:
        ctx["rules_at_time"] = db.rules_effective_at(ts) if ts else []
    except Exception:  # noqa: BLE001
        ctx["rules_at_time"] = []

    # ---- 当时可用的现金流 ----
    # 「当时可用」而不是「现在知道的」：Flex 是 T+1 的，事后才补齐的流水
    # 在当时并不存在，把它算进去就是未来数据泄漏。
    try:
        ctx["cash_flows"] = db.cash_flows_known_at(ts, account=account) if ts else []
    except Exception:  # noqa: BLE001
        ctx["cash_flows"] = []

    # ---- 数据质量 ----
    try:
        ctx["quality_issues"] = (db.quality_issues_for(snapshot_id)
                                 if snapshot_id else [])
    except Exception:  # noqa: BLE001
        ctx["quality_issues"] = []
    ctx["quality_status"] = (row or {}).get("quality_status")
    ctx["reliable"] = bool(ctx["risk_assessment"]
                           and ctx["risk_assessment"].get("reliable"))
    return ctx


def summarize(ctx: dict) -> dict:
    """上下文摘要。用于快速确认「这个时点的数据够不够重放」。"""
    return {
        "as_of": ctx.get("as_of"),
        "snapshot_id": ctx.get("snapshot_id"),
        "has_balances": ctx.get("balances") is not None,
        "position_count": len(ctx.get("positions") or []),
        "has_risk_assessment": ctx.get("risk_assessment") is not None,
        "has_market_assessment": ctx.get("market_assessment") is not None,
        "rule_set_version": ctx.get("rule_set_version"),
        "cash_flow_count": len(ctx.get("cash_flows") or []),
        "missing": ctx.get("missing") or [],
        "replayable": not ctx.get("missing"),
    }
