"""持仓决策与操作计划领域服务。

本模块只消费已经落库的持仓快照和 executions，不持有 IBKR 客户端，也不提供任何
下单、改单或撤单能力。所有自动判断都保守处理：无法可靠匹配时写入
``PENDING_CONFIRMATION``，不把不确定性伪装成「符合计划」。
"""
from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable
from zoneinfo import ZoneInfo

from .db import Database
from .market_hours import REGULAR, market_session
from .position_plan_view import (
    MATCH_LABELS, STATUS_LABELS, TRIGGER_LABELS, action_display, action_label,
    action_status_label, event_type_label, format_money, format_percent, format_price,
    format_datetime, format_quantity, match_label, status_label, trigger_label, version_label,
)


ACTIVE_STATUSES = ("PENDING", "DRAFT", "ACTIVE", "REVIEW_DUE", "TRIGGERED", "INVALIDATED")
ARCHIVED_STATUSES = ("CLOSED", "ARCHIVED")
FORMAL_STATUSES = ("ACTIVE", "REVIEW_DUE", "TRIGGERED", "INVALIDATED")
VERSION_DRAFT = "DRAFT"
VERSION_PUBLISHED = "PUBLISHED"
VERSION_LEGACY = "LEGACY"
PLAN_CHANGE_REASONS = {
    "财报变化", "新订单或公告", "市场环境变化", "股价达到目标",
    "原判断错误", "仓位变化", "单纯改变主意", "其他", "NEW_CYCLE",
}
ACTION_TYPES = {"ADD", "REDUCE", "EXIT", "REVIEW"}
TRIGGER_TYPES = {"PRICE", "EVENT", "DATE", "MANUAL"}
MATCH_STATUSES = {"MATCHED", "PLAN_OUTSIDE", "PENDING_CONFIRMATION"}
ACTION_STATUSES = {"DRAFT", "ACTIVE", "TRIGGERED", "CANCELED", "EXECUTED"}


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def _decimal(value: Any, default: Decimal | None = None) -> Decimal | None:
    if value is None or value == "":
        return default
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return default


def _stored(value: Any) -> str | None:
    value = _decimal(value)
    return None if value is None else str(value)


def _stored_percentage(value: Any) -> str | None:
    """把网页常见的 30 / 30% 输入归一为内部比例 0.30。"""
    number = _decimal(value)
    if number is None:
        return None
    if number > 1 and number <= 100:
        number /= Decimal("100")
    return str(number)


def _stored_ratio(value: Any) -> str | None:
    number = _decimal(value)
    if number is None:
        return None
    if number > 1 and number <= 100:
        number /= Decimal("100")
    return str(number)


def _text(value: Any) -> str | None:
    if value is None:
        return None
    value = str(value).strip()
    return value or None


def _row(row) -> dict | None:
    return dict(row) if row is not None else None


def _rows(rows: Iterable) -> list[dict]:
    return [dict(r) for r in rows]


def _j(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _json_load(value: str | None, default):
    try:
        return json.loads(value) if value else default
    except (TypeError, ValueError):
        return default


def _plan_warnings(thesis: str | None, invalidation: str | None) -> list[str]:
    warnings = []
    vague = {"看好", "会涨", "跌了加仓", "长期看好", "感觉会涨"}
    if not _text(thesis):
        warnings.append("核心购买理由尚未填写")
    elif _text(thesis) in vague or len(_text(thesis) or "") < 4:
        warnings.append("核心购买理由过于模糊，建议写成可验证的判断")
    if not _text(invalidation):
        warnings.append("投资逻辑失效条件尚未填写")
    elif _text(invalidation) in vague or len(_text(invalidation) or "") < 4:
        warnings.append("逻辑失效条件过于模糊，建议写成可执行的条件")
    return warnings


def _plan_has_core(plan: dict | None, cycle: dict | None = None) -> bool:
    """正式发布所需的四个核心字段是否齐全。"""
    if not plan:
        return False
    max_weight = plan.get("max_weight")
    if max_weight in (None, "") and cycle is not None:
        max_weight = cycle.get("max_weight")
    max_weight = _decimal(max_weight)
    return all(_text(plan.get(key)) for key in (
        "thesis", "invalidation_conditions")) and \
        max_weight is not None and Decimal("0") < max_weight <= Decimal("1")


def _plan_has_content(plan: dict | None, actions: Iterable[dict] = ()) -> bool:
    if not plan:
        return bool(list(actions))
    fields = ("strategy_type", "current_conclusion", "thesis", "why_now",
              "confirmed_facts", "subjective_assumptions", "catalysts",
              "invalidation_conditions", "notes", "review_at", "max_weight")
    return any(_text(plan.get(key)) for key in fields) or bool(list(actions))


def _action_amount_present(action: dict) -> bool:
    quantity = _decimal(action.get("quantity"))
    percentage = _decimal(action.get("percentage"))
    return (quantity is not None and quantity > 0) ^ (percentage is not None and percentage > 0)


def _normalize_actions(payload: dict) -> list[dict]:
    """只把结构化操作计划带入正式发布；旧的条件文本仍可保留为草稿提醒。"""
    if "actions" in payload:
        raw = payload.get("actions") or []
        return [dict(a) for a in raw if isinstance(a, dict) and _text(a.get("action_type"))]
    action = {
        "action_type": payload.get("action_type", payload.get("next_action_type")),
        "trigger_type": payload.get("trigger_type", payload.get("next_trigger_type")),
        "price_min": payload.get("price_min", payload.get("next_price_min")),
        "price_max": payload.get("price_max", payload.get("next_price_max")),
        "event_condition": payload.get("event_condition", payload.get("next_event_condition")),
        "quantity": payload.get("quantity", payload.get("next_quantity")),
        "percentage": payload.get("percentage", payload.get("next_percentage")),
        "additional_conditions": payload.get(
            "additional_conditions", payload.get("next_additional_conditions")),
    }
    if _text(action.get("action_type")):
        return [action]
    # 兼容旧快速建档表单：这些条件只进入草稿，不会被当成正式操作计划。
    legacy = []
    for field, action_type in (("add_condition", "ADD"),
                               ("reduce_condition", "REDUCE"),
                               ("exit_condition", "EXIT")):
        condition = _text(payload.get(field))
        if condition:
            legacy.append({"action_type": action_type, "trigger_type": "EVENT",
                           "event_condition": condition})
    return legacy


def _copy_plan_fields(plan: dict | None) -> dict:
    return {key: (plan.get(key) if plan else None) for key in (
        "strategy_type", "current_conclusion", "thesis", "why_now",
        "confirmed_facts", "subjective_assumptions", "catalysts",
        "invalidation_conditions", "notes", "review_at", "max_weight",
        "target_weight")}


def _normalize_review_at(value: Any) -> str | None:
    """保存为带 UTC 偏移的 ISO 时间，避免字符串比较混用时区。"""
    value = _text(value)
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise PositionPlanError("复盘时间格式无效") from None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat()


def _truthy(value: Any) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on", "是"}


def _side_sign(side: str | None) -> int:
    value = str(side or "").upper()
    if value in {"BUY", "BOT", "BUY_TO_COVER", "BUYTOCOVER"}:
        return 1
    if value in {"SELL", "SLD", "SELL_SHORT", "SELLSHORT"}:
        return -1
    return 0


def _iso_date_delta(start: str | None, end: str | None) -> int | None:
    if not start or not end:
        return None
    try:
        a = datetime.fromisoformat(start)
        b = datetime.fromisoformat(end)
        return max(0, (b - a).total_seconds() / 86400)
    except (TypeError, ValueError):
        return None


class PositionPlanError(ValueError):
    """用户请求或账户边界不满足持仓计划约束。"""


class PositionPlanService:
    """按 ``account + symbol + position cycle`` 管理计划和周期。

    ``reconcile`` 可以在每轮账户快照后调用；传入 ``positions=None`` 时只处理新成交，
    不会把缺失的持仓误判为清仓。这一点用于实时成交刷新和重启恢复。
    """

    def __init__(self, db: Database, account: str, *, actor: str = "system",
                 local_tz=None, market_tz=None):
        account = _text(account)
        if not account:
            raise PositionPlanError("缺少账户标识，拒绝创建持仓计划")
        self.db = db
        self.account = account
        self.actor = actor or "system"
        self.local_tz = local_tz or ZoneInfo(os.environ.get("LOCAL_TZ", "Asia/Shanghai"))
        self.market_tz = market_tz or ZoneInfo(
            os.environ.get("MARKET_TZ", "America/New_York"))

    def create_pending_opportunity_plan(self, *, symbol: str, quantity: Any,
                                        entry_low: Any, entry_high: Any,
                                        stop_price: Any, target_weight: Any,
                                        max_weight: Any, thesis: str,
                                        source_refs: dict,
                                        idempotency_key: str) -> dict:
        """把组合建议采纳为待执行周期和草稿计划。

        这里只写决策表；没有任何 Broker/IBKR 依赖，也不会提交订单。同一个
        ``idempotency_key`` 重试时返回原周期。
        """
        symbol = (_text(symbol) or "").upper()
        if not symbol:
            raise PositionPlanError("缺少股票代码")
        with self.db.connect() as conn:
            existing = conn.execute(
                "SELECT detail_json FROM position_plan_audit_log "
                "WHERE idempotency_key=? AND account=?",
                (idempotency_key, self.account)).fetchone()
            if existing:
                detail = _json_load(existing["detail_json"], {})
                cycle_id = ((detail.get("draft") or {}).get("position_cycle_id")
                            or (detail.get("version") or {}).get("position_cycle_id"))
                if cycle_id:
                    cycle = conn.execute(
                        "SELECT * FROM position_cycles WHERE id=? AND account=?",
                        (cycle_id, self.account)).fetchone()
                    if cycle:
                        return dict(cycle)
            now = utcnow_iso()
            cycle = self._create_cycle_tx(
                conn, symbol=symbol,
                position={"symbol": symbol, "qty": "0", "avg_cost": None,
                          "market_value": "0", "price": None,
                          "account_weight": "0", "stock_weight": "0",
                          "current_weight": "0", "conid": None},
                observed_at=now, snapshot_id=source_refs.get("proposal_id"),
            )
        stop_text = _stored(stop_price)
        payload = {
            "strategy_type": "波段",
            "current_conclusion": "等待人工执行",
            "thesis": thesis,
            "why_now": "机会扫描与组合约束均已通过，等待人工确认入场",
            "confirmed_facts": _j(source_refs),
            "subjective_assumptions": "候选评分只基于已收盘日线，未来走势仍存在不确定性",
            "invalidation_conditions": (
                f"收盘价跌破建议止损 {stop_text}" if stop_text else "建议止损失效或数据质量下降"
            ),
            "notes": "来源：小鱼交易台机会发现与组合构建；不代表已向券商下单",
            "target_weight": target_weight,
            "max_weight": max_weight,
            "change_reason": "NEW_CYCLE",
            "actions": [{
                "action_type": "ADD", "trigger_type": "PRICE",
                "price_min": entry_low, "price_max": entry_high,
                "quantity": quantity,
                "additional_conditions": (
                    f"人工确认后在 IBKR 执行；计划止损 {stop_text}" if stop_text
                    else "人工确认后在 IBKR 执行"
                ),
                "priority": 10,
            }],
        }
        self.save_draft(cycle["id"], payload, actor=self.actor,
                        idempotency_key=idempotency_key)
        # 采纳仅代表“待执行”，不是计划已经激活或成交。
        with self.db.connect() as conn:
            conn.execute(
                "UPDATE position_cycles SET status='PENDING',updated_at=? "
                "WHERE id=? AND account=? AND current_quantity='0'",
                (utcnow_iso(), cycle["id"], self.account),)
            return dict(conn.execute(
                "SELECT * FROM position_cycles WHERE id=?", (cycle["id"],)).fetchone())

    # ---------- 账户/成交边界 ----------

    @staticmethod
    def _execution_scope(conn, account: str, alias: str = "e") -> tuple[str, list]:
        """给旧的无 account executions 提供单账户兼容，但隔离已知多账户数据。

        旧库的 executions 没有账户列。迁移只补列，不批量改写原始成交；当库中尚无任何
        已知账户成交时，NULL 行可被当前唯一账户安全消费。只要库里出现已知账户归属，
        NULL 行不再跨账户猜测，避免把另一账户的成交绑定到本周期。
        """
        known = conn.execute(
            "SELECT COUNT(*) FROM executions WHERE account IS NOT NULL AND account<>''"
        ).fetchone()[0]
        if known == 0:
            return f"({alias}.account=? OR {alias}.account IS NULL)", [account]
        return f"{alias}.account=?", [account]

    def _latest_plan_tx(self, conn, cycle_id: str, *, lifecycle: str = VERSION_PUBLISHED) -> dict | None:
        return _row(conn.execute(
            "SELECT * FROM position_plan_versions WHERE position_cycle_id=? "
            "AND lifecycle_status=? ORDER BY version DESC LIMIT 1",
            (cycle_id, lifecycle)).fetchone())

    def _draft_plan_tx(self, conn, cycle_id: str) -> dict | None:
        return self._latest_plan_tx(conn, cycle_id, lifecycle=VERSION_DRAFT)

    def _latest_any_plan_tx(self, conn, cycle_id: str) -> dict | None:
        return _row(conn.execute(
            "SELECT * FROM position_plan_versions WHERE position_cycle_id=? "
            "AND lifecycle_status IN ('DRAFT','PUBLISHED','LEGACY') "
            "ORDER BY CASE lifecycle_status WHEN 'DRAFT' THEN 0 ELSE 1 END, "
            "version DESC LIMIT 1", (cycle_id,)).fetchone())

    def _cycle_tx(self, conn, cycle_id: str, *, for_update: bool = False) -> dict | None:
        return _row(conn.execute(
            "SELECT * FROM position_cycles WHERE id=?", (cycle_id,)).fetchone())

    def _assert_cycle_tx(self, conn, cycle_id: str, *, writable: bool = False) -> dict:
        cycle = self._cycle_tx(conn, cycle_id)
        if not cycle:
            raise PositionPlanError("持仓周期不存在")
        if cycle["account"] != self.account:
            raise PositionPlanError("无权访问其他账户的持仓周期")
        if writable and cycle["status"] in ARCHIVED_STATUSES:
            raise PositionPlanError("持仓周期已归档，只读数据不可修改")
        return cycle

    def _next_cycle_number_tx(self, conn, symbol: str) -> int:
        row = conn.execute(
            "SELECT COALESCE(MAX(cycle_number),0)+1 AS n FROM position_cycles "
            "WHERE account=? AND symbol=?", (self.account, symbol)).fetchone()
        return int(row["n"])

    def _opening_execution_tx(self, conn, symbol: str, observed_at: str,
                              previous_closed_at: str | None = None) -> dict | None:
        scope, args = self._execution_scope(conn, self.account)
        params: list[Any] = [symbol, observed_at]
        sql = (
            "SELECT e.* FROM executions e WHERE e.symbol=? AND e.execution_time<=? "
            "AND e.is_current=1 AND UPPER(e.side) IN ('BUY','BOT','BUY_TO_COVER','BUYTOCOVER') "
            f"AND {scope} "
        )
        params.extend(args)
        if previous_closed_at:
            sql += "AND e.execution_time>? "
            params.append(previous_closed_at)
        sql += "ORDER BY e.execution_time, e.exec_id LIMIT 1"
        return _row(conn.execute(sql, params).fetchone())

    def _create_cycle_tx(self, conn, *, symbol: str, position: dict,
                         observed_at: str, snapshot_id: str | None) -> dict:
        existing = conn.execute(
            "SELECT * FROM position_cycles WHERE account=? AND symbol=? "
            "AND status IN ('PENDING','DRAFT','ACTIVE','REVIEW_DUE','TRIGGERED','INVALIDATED')",
            (self.account, symbol)).fetchone()
        if existing:
            return dict(existing)
        last_closed = conn.execute(
            "SELECT closed_at FROM position_cycles WHERE account=? AND symbol=? "
            "AND status IN ('CLOSED','ARCHIVED') ORDER BY cycle_number DESC LIMIT 1",
            (self.account, symbol)).fetchone()
        previous_closed_at = last_closed["closed_at"] if last_closed else None
        opening = self._opening_execution_tx(conn, symbol, observed_at, previous_closed_at)
        opened_at = opening["execution_time"] if opening else observed_at
        confidence = "observed" if opening else "estimated"
        opening_qty = opening.get("shares") if opening else position.get("qty")
        opening_price = opening.get("price") if opening else position.get("avg_cost")
        now = utcnow_iso()
        cycle_id = _id("pc")
        conn.execute(
            "INSERT INTO position_cycles "
            "(id, account, symbol, conid, cycle_number, status, opened_at, "
            " opened_at_confidence, opening_execution_id, opening_quantity, "
            " opening_average_price, current_quantity, current_average_cost, "
            " current_market_value, current_price, account_weight, stock_weight, current_weight, "
            " last_snapshot_id, last_observed_at, journal_sync_status, created_at, updated_at) "
            "VALUES (?,?,? ,?,?,? ,?,?,? ,?,?,? ,?,?,? ,?,?,? ,?,?,?,?,?)",
            (cycle_id, self.account, symbol, position.get("conid"),
             self._next_cycle_number_tx(conn, symbol), "PENDING", opened_at, confidence,
             opening.get("exec_id") if opening else None, _stored(opening_qty),
             _stored(opening_price), _stored(position.get("qty")),
             _stored(position.get("avg_cost")), _stored(position.get("market_value")),
             _stored(position.get("price")), _stored(position.get("account_weight")),
             _stored(position.get("stock_weight")), _stored(position.get("current_weight")),
             snapshot_id, observed_at, "NOT_REQUIRED", now, now),
        )
        version_id = _id("ppv")
        conn.execute(
            "INSERT INTO position_plan_versions "
            "(id, position_cycle_id, account, version, change_reason, created_at, created_by, "
            "lifecycle_status, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (version_id, cycle_id, self.account, 0, "NEW_CYCLE", now, "system",
             VERSION_DRAFT, now),
        )
        return dict(conn.execute(
            "SELECT * FROM position_cycles WHERE id=?", (cycle_id,)).fetchone())

    def _record_observation_tx(self, conn, cycle: dict, *, snapshot_id: str,
                               observed_at: str, position: dict | None,
                               reliable: bool) -> bool:
        qty = _decimal(position.get("qty")) if position else Decimal("0")
        status = "UNRELIABLE" if not reliable else ("POSITIVE" if qty > 0 else (
            "ZERO" if position else "MISSING"))
        cur = conn.execute(
            "INSERT OR IGNORE INTO position_cycle_observations "
            "(position_cycle_id, account, symbol, snapshot_id, observed_at, quantity, "
            " average_cost, market_value, account_weight, stock_weight, current_weight, "
            " observation_status) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (cycle["id"], self.account, cycle["symbol"], snapshot_id, observed_at,
             _stored(qty), _stored(position.get("avg_cost") if position else None),
             _stored(position.get("market_value") if position else None),
             _stored(position.get("account_weight") if position else None),
             _stored(position.get("stock_weight") if position else None),
             _stored(position.get("current_weight") if position else None), status),
        )
        return cur.rowcount > 0

    def _has_snapshot_event_tx(self, conn, cycle_id: str, snapshot_id: str) -> bool:
        return conn.execute(
            "SELECT 1 FROM position_cycle_events WHERE position_cycle_id=? "
            "AND snapshot_id=? LIMIT 1", (cycle_id, snapshot_id)).fetchone() is not None

    def _plan_action_match_tx(self, conn, cycle: dict, execution: dict,
                              pre_quantity: Decimal, *, initial: bool = False
                              ) -> tuple[str, str | None, str | None]:
        """返回 match_status、action_id、偏离/待确认说明。"""
        if initial:
            return "PENDING_CONFIRMATION", None, "建仓成交发生在计划建立前，待确认"
        plan = self._latest_plan_tx(conn, cycle["id"])
        if not plan:
            return "PENDING_CONFIRMATION", None, "当前没有已发布的有效计划，待确认"
        sign = _side_sign(execution.get("side"))
        desired = "ADD" if sign > 0 else "REDUCE"
        rows = conn.execute(
            "SELECT * FROM position_plan_actions WHERE position_cycle_id=? "
            "AND plan_version_id=? AND status IN ('ACTIVE','TRIGGERED','EXECUTED') "
            "AND action_type IN (?, 'EXIT') ORDER BY priority, created_at",
            (cycle["id"], plan["id"], desired),
        ).fetchall()
        if not rows:
            return "PLAN_OUTSIDE", None, "当前计划没有对应操作方向"
        price = _decimal(execution.get("price"))
        qty = _decimal(execution.get("shares")) or Decimal("0")
        candidates: list[dict] = []
        uncertain = False
        for action in rows:
            action = dict(action)
            pmin = _decimal(action.get("price_min"))
            pmax = _decimal(action.get("price_max"))
            if action["trigger_type"] == "PRICE":
                if price is None:
                    continue
                if pmin is not None and price < pmin:
                    slippage = max(abs(pmin) * Decimal("0.01"), Decimal("0.01"))
                    if price >= pmin - slippage:
                        uncertain = True
                    else:
                        continue
                if pmax is not None and price > pmax:
                    slippage = max(abs(pmax) * Decimal("0.01"), Decimal("0.01"))
                    if price <= pmax + slippage:
                        uncertain = True
                    else:
                        continue
            else:
                uncertain = True
            aq = _decimal(action.get("quantity"))
            prior_qty = _decimal(conn.execute(
                "SELECT COALESCE(SUM(ABS(quantity)), 0) FROM position_cycle_events "
                "WHERE plan_action_id=? AND match_status='MATCHED'", (action["id"],)
            ).fetchone()[0]) or Decimal("0")
            if aq is not None:
                remaining_qty = max(Decimal("0"), aq - prior_qty)
                if qty <= 0 or qty > remaining_qty + max(Decimal("1"), aq * Decimal("0.05")):
                    continue
            ap = _decimal(action.get("percentage"))
            if ap is not None:
                actual_pct = qty / pre_quantity if pre_quantity > 0 else None
                expected_pct = ap if ap <= 1 else ap / Decimal("100")
                if actual_pct is None or actual_pct <= 0 or actual_pct > expected_pct + Decimal("0.05"):
                    continue
            if aq is None and ap is None:
                uncertain = True
            candidates.append(action)
        if not candidates:
            return "PLAN_OUTSIDE", None, "价格、数量或操作方向未命中原计划"
        if uncertain or len(candidates) != 1:
            return "PENDING_CONFIRMATION", None, "无法可靠区分计划条件，待确认"
        return "MATCHED", candidates[0]["id"], None

    def _action_execution_state_tx(self, conn, action: Any) -> tuple[str, bool]:
        """按已确认 MATCHED 成交累计量判断动作是否完成。"""
        complete = True
        if action and action["quantity"] not in (None, ""):
            planned = _decimal(action["quantity"]) or Decimal("0")
            matched_qty = _decimal(conn.execute(
                "SELECT COALESCE(SUM(ABS(quantity)), 0) FROM position_cycle_events "
                "WHERE plan_action_id=? AND match_status='MATCHED'", (action["id"],)
            ).fetchone()[0]) or Decimal("0")
            complete = matched_qty + max(Decimal("1"), planned * Decimal("0.05")) >= planned
        if complete:
            return "EXECUTED", True
        return ("TRIGGERED" if action and
                (action["status"] == "TRIGGERED" or action["confirmed_at"])
                else "ACTIVE"), False

    def _record_execution_tx(self, conn, cycle: dict, execution: dict,
                             *, snapshot_id: str | None = None,
                             pre_quantity: Decimal | None = None,
                             post_quantity: Decimal | None = None) -> bool:
        exec_id = execution.get("exec_id")
        if not exec_id or conn.execute(
                "SELECT 1 FROM position_cycle_events WHERE position_cycle_id=? "
                "AND execution_id=?", (cycle["id"], exec_id)).fetchone():
            return False
        sign = _side_sign(execution.get("side"))
        qty = _decimal(execution.get("shares")) or Decimal("0")
        if sign == 0 or qty <= 0:
            return False
        current_qty = _decimal(cycle.get("current_quantity")) or Decimal("0")
        pre_qty = (pre_quantity if pre_quantity is not None else
                   max(Decimal("0"), current_qty - (Decimal(sign) * qty)))
        post_qty = (post_quantity if post_quantity is not None else current_qty)
        if sign > 0:
            event_type = "OPEN" if (not cycle.get("opening_execution_id") or
                                     cycle.get("opening_execution_id") == exec_id) else "ADD"
        else:
            event_type = "EXIT" if qty >= pre_qty and pre_qty > 0 else "REDUCE"
        initial = sign > 0 and (
            (cycle.get("opening_execution_id") and
             cycle.get("opening_execution_id") == exec_id) or
            (not cycle.get("opening_execution_id") and
             execution.get("execution_time", "") <= cycle.get("opened_at", ""))
        )
        match_status, action_id, deviation = self._plan_action_match_tx(
            conn, cycle, execution, pre_qty, initial=initial)
        now = utcnow_iso()
        event_id = _id("pce")
        conn.execute(
            "INSERT INTO position_cycle_events "
            "(id, position_cycle_id, account, symbol, event_type, execution_id, "
            " order_id, plan_action_id, execution_time, quantity, price, pre_quantity, post_quantity, "
            " realized_pnl, snapshot_id, match_status, deviation_reason, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (event_id, cycle["id"], self.account, cycle["symbol"], event_type, exec_id,
             execution.get("order_id"), action_id, execution["execution_time"], _stored(qty),
             _stored(execution.get("price")), _stored(pre_qty), _stored(post_qty),
             _stored(execution.get("realized_pnl")), snapshot_id, match_status,
             deviation, now),
        )
        conn.execute(
            "INSERT INTO position_plan_execution_links "
            "(id, position_cycle_id, account, plan_action_id, execution_id, match_status, "
            " deviation_reason, matched_at) VALUES (?,?,?,?,?,?,?,?)",
            (_id("ppl"), cycle["id"], self.account, action_id, exec_id, match_status,
             deviation, now),
        )
        if action_id:
            action_row = conn.execute(
                "SELECT * FROM position_plan_actions WHERE id=?",
                (action_id,)).fetchone()
            next_status, complete = self._action_execution_state_tx(conn, action_row)
            conn.execute(
                "UPDATE position_plan_actions SET status=?, executed_at=?, execution_id=? "
                "WHERE id=? AND status IN ('ACTIVE','TRIGGERED')",
                (next_status, execution["execution_time"] if complete else None,
                 exec_id, action_id),
            )
            if action_row and action_row["confirmed_at"]:
                decision_id = action_row["recommendation_id"] or action_id
                conn.execute(
                    "UPDATE executions SET decision_id=COALESCE(decision_id, ?) "
                    "WHERE exec_id=?", (decision_id, exec_id))
        if sign > 0 and not cycle.get("opening_execution_id"):
            conn.execute(
                "UPDATE position_cycles SET opening_execution_id=?, opened_at=?, "
                "opened_at_confidence='observed', opening_quantity=?, "
                "opening_average_price=?, updated_at=? WHERE id=? AND account=?",
                (exec_id, execution["execution_time"], _stored(qty),
                 _stored(execution.get("price")), now, cycle["id"], self.account),
            )
        return True

    def _process_executions_tx(self, conn, cycle: dict,
                               *, snapshot_id: str | None = None,
                               state_is_post: bool = False) -> int:
        scope, scope_args = self._execution_scope(conn, self.account)
        sql = (
            "SELECT e.* FROM executions e WHERE e.symbol=? AND e.is_current=1 "
            "AND e.execution_time>=? AND " + scope + " "
            "AND NOT EXISTS (SELECT 1 FROM position_cycle_events ce "
            "WHERE ce.position_cycle_id=? AND ce.execution_id=e.exec_id) "
            "ORDER BY e.execution_time, e.exec_id"
        )
        rows = conn.execute(sql, [cycle["symbol"], cycle["opened_at"],
                                  *scope_args, cycle["id"]]).fetchall()
        count = 0
        working = _decimal(cycle.get("current_quantity")) or Decimal("0")
        sequence = list(reversed(rows)) if state_is_post else list(rows)
        for row in sequence:
            item = dict(row)
            sign = _side_sign(item.get("side"))
            qty = _decimal(item.get("shares")) or Decimal("0")
            if sign == 0 or qty <= 0:
                continue
            if state_is_post:
                post_qty = working
                pre_qty = max(Decimal("0"), post_qty - Decimal(sign) * qty)
                working = pre_qty
            else:
                pre_qty = working
                post_qty = max(Decimal("0"), pre_qty + Decimal(sign) * qty)
                working = post_qty
            if self._record_execution_tx(
                    conn, cycle, item, snapshot_id=snapshot_id,
                    pre_quantity=pre_qty, post_quantity=post_qty):
                count += 1
        cycle["_execution_post_quantity"] = _stored(working)
        return count

    def _recent_buy_tx(self, conn, cycle: dict, since: str | None) -> bool:
        if not since:
            return False
        scope, args = self._execution_scope(conn, self.account)
        row = conn.execute(
            "SELECT 1 FROM executions e WHERE e.symbol=? AND e.execution_time>=? "
            "AND e.is_current=1 AND UPPER(e.side) IN ('BUY','BOT','BUY_TO_COVER','BUYTOCOVER') "
            f"AND {scope} LIMIT 1", [cycle["symbol"], since, *args]).fetchone()
        return row is not None

    def _audit_late_executions_tx(self, conn) -> int:
        """迟到成交不回写已归档快照，只留可重试、可追踪的审计提示。"""
        scope, args = self._execution_scope(conn, self.account)
        rows = conn.execute(
            "SELECT e.* FROM executions e WHERE e.is_current=1 AND " + scope + " "
            "AND NOT EXISTS (SELECT 1 FROM position_cycle_events ce "
            "WHERE ce.execution_id=e.exec_id) "
            "AND EXISTS (SELECT 1 FROM position_cycles pc WHERE pc.account=? "
            "AND pc.symbol=e.symbol AND pc.status IN ('CLOSED','ARCHIVED') "
            "AND pc.closed_at IS NOT NULL AND e.execution_time<=pc.closed_at)",
            [*args, self.account]).fetchall()
        count = 0
        for row in rows:
            row = dict(row)
            latest = conn.execute(
                "SELECT id FROM position_cycles WHERE account=? AND symbol=? "
                "ORDER BY cycle_number DESC LIMIT 1",
                (self.account, row.get("symbol"))).fetchone()
            key = f"late-execution:{row['exec_id']}"
            cur = conn.execute(
                "INSERT OR IGNORE INTO position_plan_audit_log "
                "(account, position_cycle_id, actor, operation, idempotency_key, "
                " detail_json, created_at) VALUES (?,?,?,?,?,?,?)",
                (self.account, latest["id"] if latest else None, "system",
                 "LATE_EXECUTION", key,
                 _j({"execution_id": row["exec_id"], "symbol": row.get("symbol"),
                     "execution_time": row.get("execution_time"),
                     "note": "迟到成交未修改已归档投资日记快照"}), utcnow_iso()),
            )
            count += int(cur.rowcount > 0)
        return count

    def _plan_status_tx(self, conn, cycle_id: str, now: str) -> str:
        cycle = self._cycle_tx(conn, cycle_id)
        if cycle and cycle.get("status") == "INVALIDATED":
            return "INVALIDATED"
        plan = self._latest_plan_tx(conn, cycle_id)
        if not plan:
            return "DRAFT" if _plan_has_content(self._draft_plan_tx(conn, cycle_id)) else "PENDING"
        if conn.execute(
                "SELECT 1 FROM position_plan_actions WHERE position_cycle_id=? "
                "AND plan_version_id=? AND status='TRIGGERED' LIMIT 1",
                (cycle_id, plan["id"])).fetchone():
            return "TRIGGERED"
        review_at = plan.get("review_at")
        if review_at and review_at <= now:
            return "REVIEW_DUE"
        return "ACTIVE"

    def _mark_triggered_tx(self, conn, cycle_id: str, quote: Any,
                           triggered_at: str) -> int:
        plan = self._latest_plan_tx(conn, cycle_id)
        if not plan:
            return 0
        quote = dict(quote) if isinstance(quote, dict) else {"price": quote}
        price = _decimal(quote.get("price"))
        quality = str(quote.get("price_quality") or "").upper()
        quote_as_of = quote.get("quote_as_of")
        session = "UNKNOWN"
        if quote_as_of:
            try:
                at = datetime.fromisoformat(str(quote_as_of).replace("Z", "+00:00"))
                if at.tzinfo is None:
                    at = at.replace(tzinfo=timezone.utc)
                session = market_session(at, self.market_tz)
            except (TypeError, ValueError):
                pass
        context = {
            "price": _stored(price), "source": quote.get("price_source") or quote.get("source"),
            "quality": quality or None, "session": session,
            "as_of": quote_as_of, "snapshot_id": quote.get("snapshot_id"),
        }
        blocked_reason = None
        if price is None or price <= 0:
            blocked_reason = "缺少可用行情价格"
        elif not quote_as_of:
            blocked_reason = "行情缺少 as-of，无法证明触发时点"
        elif session == "UNKNOWN":
            blocked_reason = "行情 as-of 无效，无法判断 PRICE 触发时段"
        elif session != REGULAR:
            blocked_reason = f"{session} 不是常规交易时段，PRICE 条件不自动触发"
        elif quality not in {"REALTIME", "REALTIME_NONCONSOLIDATED"}:
            blocked_reason = f"行情质量 {quality or 'UNKNOWN'} 不支持自动 PRICE 触发"
        rows = conn.execute(
            "SELECT * FROM position_plan_actions WHERE position_cycle_id=? "
            "AND plan_version_id=? AND trigger_type='PRICE' "
            "AND status='ACTIVE'", (cycle_id, plan["id"])).fetchall()
        changed = 0
        for action in rows:
            pmin = _decimal(action["price_min"])
            pmax = _decimal(action["price_max"])
            met = (blocked_reason is None and
                   (pmin is None or price >= pmin) and
                   (pmax is None or price <= pmax))
            evaluation = "BLOCKED" if blocked_reason else ("TRIGGERED" if met else "WAITING")
            note = blocked_reason or (None if met else "价格尚未进入正式计划区间")
            conn.execute(
                "UPDATE position_plan_actions SET trigger_evaluation_status=?, "
                "trigger_evaluation_note=?, trigger_evaluated_at=?, trigger_quote_json=? "
                "WHERE id=? AND status='ACTIVE'",
                (evaluation, note, triggered_at, _j(context), action["id"]),
            )
            if met:
                conn.execute(
                    "UPDATE position_plan_actions SET status='TRIGGERED', triggered_at=? "
                    "WHERE id=? AND status='ACTIVE'", (triggered_at, action["id"]),
                )
                changed += 1
        return changed

    def _update_current_tx(self, conn, cycle: dict, position: dict,
                           *, snapshot_id: str, observed_at: str) -> None:
        old_qty = _decimal(cycle.get("current_quantity")) or Decimal("0")
        new_qty = _decimal(position.get("qty")) or Decimal("0")
        event_cycle = {**cycle, "current_quantity": _stored(new_qty)}
        self._process_executions_tx(
            conn, event_cycle, snapshot_id=snapshot_id, state_is_post=True)
        if old_qty != new_qty and not self._has_snapshot_event_tx(
                conn, cycle["id"], snapshot_id):
            sign = "ADD" if new_qty > old_qty else "REDUCE"
            conn.execute(
                "INSERT INTO position_cycle_events "
                "(id, position_cycle_id, account, symbol, event_type, execution_time, "
                " quantity, pre_quantity, post_quantity, average_cost, market_value, "
                " snapshot_id, match_status, deviation_reason, created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (_id("pce"), cycle["id"], self.account, cycle["symbol"], sign,
                 observed_at, _stored(abs(new_qty - old_qty)), _stored(old_qty),
                 _stored(new_qty), _stored(position.get("avg_cost")),
                 _stored(position.get("market_value")), snapshot_id,
                 "PENDING_CONFIRMATION", "快照数量变化未能关联到唯一成交", utcnow_iso()),
            )
        self._mark_triggered_tx(
            conn, cycle["id"], {**position, "snapshot_id": snapshot_id}, observed_at)
        status = self._plan_status_tx(conn, cycle["id"], observed_at)
        conn.execute(
            "UPDATE position_cycles SET status=?, conid=COALESCE(?,conid), "
            " current_quantity=?, current_average_cost=?, current_market_value=?, "
            " current_price=?, account_weight=?, stock_weight=?, current_weight=?, "
            " last_snapshot_id=?, last_observed_at=?, "
            " zero_observed_count=0, zero_first_observed_at=NULL, updated_at=? "
            "WHERE id=? AND account=?",
            (status, position.get("conid"), _stored(new_qty),
             _stored(position.get("avg_cost")), _stored(position.get("market_value")),
             _stored(position.get("price")), _stored(position.get("account_weight")),
             _stored(position.get("stock_weight")), _stored(position.get("current_weight")),
             snapshot_id, observed_at, utcnow_iso(), cycle["id"], self.account),
        )

    def _close_cycle_tx(self, conn, cycle: dict, *, closed_at: str) -> None:
        conn.execute(
            "UPDATE position_cycles SET status='CLOSED', current_quantity='0', "
            "closed_at=?, closed_confirmed_at=?, journal_sync_status='PENDING', "
            "journal_sync_error=NULL, updated_at=? WHERE id=? AND account=? "
            "AND status IN ('PENDING','DRAFT','ACTIVE','REVIEW_DUE','TRIGGERED','INVALIDATED')",
            (closed_at, closed_at, utcnow_iso(), cycle["id"], self.account),
        )

    def reconcile(self, *, snapshot_id: str | None = None,
                  observed_at: str | None = None,
                  positions: Iterable[dict] | None = None,
                  allow_close: bool = True) -> dict:
        """推进一个账户的持仓周期。

        ``positions`` 必须是完整且通过数据质量闸门的当前持仓集合；否则传 None，服务只
        处理成交，不推进零持仓确认。归档在本事务提交后单独执行，失败只影响 journal
        状态，不回滚已经确认的成交或清仓事实。
        """
        observed_at = observed_at or utcnow_iso()
        snapshot_key = snapshot_id or f"observation:{observed_at}"
        position_map: dict[str, dict] | None = None
        if positions is not None:
            position_map = {}
            for raw in positions:
                item = dict(raw)
                symbol = _text(item.get("symbol"))
                if not symbol:
                    continue
                qty = _decimal(item.get("qty")) or Decimal("0")
                if qty > 0:
                    position_map[symbol] = item

        closed_ids: list[str] = []
        processed = 0
        created = 0
        with self.db.connect() as conn:
            active = _rows(conn.execute(
                "SELECT * FROM position_cycles WHERE account=? AND status IN "
                "('PENDING','DRAFT','ACTIVE','REVIEW_DUE','TRIGGERED','INVALIDATED') "
                "ORDER BY symbol", (self.account,)).fetchall())
            by_symbol = {c["symbol"]: c for c in active}
            balance = conn.execute(
                "SELECT equity FROM balances_snapshot WHERE account=? ORDER BY ts DESC LIMIT 1",
                (self.account,)).fetchone()
            equity = _decimal(balance["equity"]) if balance else None
            stock_total = sum(
                max(_decimal(item.get("market_value")) or Decimal("0"), Decimal("0"))
                for item in position_map.values()
            ) if position_map is not None else None

            if position_map is not None:
                for symbol, position in position_map.items():
                    mv = _decimal(position.get("market_value"))
                    if equity and equity > 0 and mv is not None:
                        position["account_weight"] = mv / equity
                        position["current_weight"] = position["account_weight"]
                    if stock_total and stock_total > 0 and mv is not None:
                        position["stock_weight"] = mv / stock_total
                    cycle = by_symbol.get(symbol)
                    if cycle is None:
                        cycle = self._create_cycle_tx(
                            conn, symbol=symbol, position=position,
                            observed_at=observed_at, snapshot_id=snapshot_key)
                        by_symbol[symbol] = cycle
                        created += 1
                    self._record_observation_tx(
                        conn, cycle, snapshot_id=snapshot_key, observed_at=observed_at,
                        position=position, reliable=allow_close)
                    self._update_current_tx(
                        conn, cycle, position, snapshot_id=snapshot_key,
                        observed_at=observed_at)
                    processed += 1

            # 没有完整正数持仓的周期：先处理迟到成交，再累积连续零观测。
            if position_map is not None:
                for cycle in list(by_symbol.values()):
                    if cycle["symbol"] in position_map:
                        continue
                    self._process_executions_tx(conn, cycle, snapshot_id=snapshot_key)
                    if cycle.get("_execution_post_quantity") is not None:
                        conn.execute(
                            "UPDATE position_cycles SET current_quantity=?, updated_at=? "
                            "WHERE id=? AND account=?",
                            (cycle["_execution_post_quantity"], utcnow_iso(),
                             cycle["id"], self.account),
                        )
                    if not allow_close:
                        self._record_observation_tx(
                            conn, cycle, snapshot_id=snapshot_key, observed_at=observed_at,
                            position=None, reliable=False)
                        continue
                    prev = int(cycle.get("zero_observed_count") or 0)
                    first = cycle.get("zero_first_observed_at") or observed_at
                    count = prev + 1
                    observation_added = self._record_observation_tx(
                        conn, cycle, snapshot_id=snapshot_key, observed_at=observed_at,
                        position=None, reliable=True)
                    if not observation_added:
                        continue
                    recent_buy = self._recent_buy_tx(conn, cycle, first)
                    if recent_buy:
                        count = 0
                        first = None
                    current_after = (cycle.get("_execution_post_quantity")
                                     if recent_buy else "0") or "0"
                    conn.execute(
                        "UPDATE position_cycles SET current_quantity=?, "
                        "zero_observed_count=?, zero_first_observed_at=?, "
                        "last_snapshot_id=?, last_observed_at=?, updated_at=? "
                        "WHERE id=? AND account=?",
                        (current_after, count, first, snapshot_key, observed_at, utcnow_iso(),
                         cycle["id"], self.account),
                    )
                    if count >= 2 and not recent_buy:
                        self._close_cycle_tx(conn, cycle, closed_at=observed_at)
                        closed_ids.append(cycle["id"])
            else:
                for cycle in active:
                    processed += self._process_executions_tx(conn, cycle)
                    if cycle.get("_execution_post_quantity") is not None:
                        conn.execute(
                            "UPDATE position_cycles SET current_quantity=?, updated_at=? "
                            "WHERE id=? AND account=?",
                            (cycle["_execution_post_quantity"], utcnow_iso(),
                             cycle["id"], self.account),
                        )
            late_count = self._audit_late_executions_tx(conn)

        archive_results = []
        for cycle_id in closed_ids:
            archive_results.append(self.archive_cycle(cycle_id))
        return {
            "account": self.account,
            "snapshot_id": snapshot_key,
            "processed_symbols": processed,
            "created_cycles": created,
            "closed_cycles": closed_ids,
            "late_executions": late_count,
            "archives": archive_results,
        }

    def process_executions(self) -> dict:
        """成交同步后的轻量推进，不观察或关闭持仓。"""
        result = self.reconcile(positions=None, allow_close=False)
        # reconcile(positions=None) 不会改写快照字段，但成交本身可以推进当前数量，
        # 这样下一次账户快照不会把同一批成交重复记成「快照调整」。
        return result

    # ---------- 计划版本与操作 ----------

    @staticmethod
    def _action_input_present(payload: dict) -> bool:
        return "actions" in payload or any(
            key in payload for key in (
                "action_type", "next_action_type", "trigger_type", "next_trigger_type",
                "add_condition", "reduce_condition", "exit_condition"))

    @staticmethod
    def _action_payload(row: dict) -> dict:
        return {key: row.get(key) for key in (
            "action_type", "trigger_type", "price_min", "price_max", "event_condition",
            "quantity", "percentage", "additional_conditions", "priority")}

    def _plan_data(self, payload: dict, baseline: dict | None, cycle: dict,
                   now: str) -> dict:
        baseline = baseline or {}

        def value(*keys):
            for key in keys:
                if key in payload:
                    return payload.get(key)
            return baseline.get(keys[0])

        strategy_value = _text(value("strategy_type", "transaction_type"))
        review_raw = value("review_at", "next_review_at")
        review_value = _normalize_review_at(review_raw)
        if not review_value:
            # 复盘时间留空即自动设定：有交易类型按其节奏，否则默认 14 天后。
            strategy_key = (strategy_value or "").upper()
            review_days = {"短线": 3, "SHORT": 3, "波段": 7, "SWING": 7,
                           "长线": 30, "LONG": 30}.get(strategy_key, 14)
            review_value = (datetime.fromisoformat(now) +
                            timedelta(days=review_days)).isoformat()
        max_weight = value("max_weight")
        if max_weight in (None, ""):
            max_weight = cycle.get("max_weight")
        target_weight = value("target_weight")
        if target_weight in (None, ""):
            target_weight = cycle.get("target_weight")
        return {
            "strategy_type": strategy_value,
            "current_conclusion": _text(value("current_conclusion")),
            "thesis": _text(value("thesis", "purchase_reason")),
            "why_now": _text(value("why_now")),
            "confirmed_facts": _text(value("confirmed_facts")),
            "subjective_assumptions": _text(value("subjective_assumptions")),
            "catalysts": _text(value("catalysts")),
            "invalidation_conditions": _text(value(
                "invalidation_conditions", "logic_invalidated_when")),
            "notes": _text(value("notes")),
            "review_at": review_value,
            "max_weight": _stored_ratio(max_weight),
            "target_weight": _stored_ratio(target_weight),
            "hold_only_review": _truthy(payload.get("hold_only_review"))
                if "hold_only_review" in payload else bool(baseline.get("hold_only_review")),
            "limit_confirmed": _truthy(payload.get("limit_confirmed"))
                if "limit_confirmed" in payload else bool(baseline.get("limit_confirmed")),
        }

    def save_draft(self, cycle_id: str, payload: dict, *, actor: str | None = None,
                   idempotency_key: str | None = None) -> dict:
        """保存可原地修改的草稿；不会创建正式版本，也不会激活触发。"""
        actor = _text(actor) or self.actor
        payload = payload or {}
        if idempotency_key:
            with self.db.connect() as conn:
                existing = conn.execute(
                    "SELECT detail_json FROM position_plan_audit_log "
                    "WHERE idempotency_key=? AND account=?",
                    (idempotency_key, self.account)).fetchone()
                if existing:
                    return _json_load(existing["detail_json"], {})
        now = utcnow_iso()
        with self.db.connect() as conn:
            cycle = self._assert_cycle_tx(conn, cycle_id, writable=True)
            draft = self._draft_plan_tx(conn, cycle_id)
            formal = self._latest_plan_tx(conn, cycle_id)
            baseline = draft or formal or {}
            baseline = {**baseline, "max_weight": cycle.get("max_weight"),
                        "target_weight": cycle.get("target_weight")}
            data = self._plan_data(payload, baseline, cycle, now)
            action_input = self._action_input_present(payload)
            if action_input:
                actions = _normalize_actions(payload)
            elif draft:
                actions = _rows(conn.execute(
                    "SELECT * FROM position_plan_actions WHERE plan_version_id=? "
                    "AND status='DRAFT' ORDER BY priority, created_at", (draft["id"],)
                ).fetchall())
            elif formal:
                actions = _rows(conn.execute(
                    "SELECT * FROM position_plan_actions WHERE plan_version_id=? "
                    "AND status IN ('ACTIVE','TRIGGERED','EXECUTED') "
                    "ORDER BY priority, created_at", (formal["id"],)
                ).fetchall())
            else:
                actions = []

            reason = _text(payload.get("change_reason")) or (
                draft.get("change_reason") if draft else "NEW_CYCLE")
            draft_of = draft.get("draft_of_version_id") if draft else (
                formal["id"] if formal else None)
            if draft:
                conn.execute(
                    "UPDATE position_plan_versions SET strategy_type=?, current_conclusion=?, thesis=?, "
                    "why_now=?, confirmed_facts=?, subjective_assumptions=?, catalysts=?, "
                    "invalidation_conditions=?, notes=?, review_at=?, max_weight=?, target_weight=?, change_reason=?, "
                    "hold_only_review=?, limit_confirmed=?, updated_at=? WHERE id=?",
                    (data["strategy_type"], data["current_conclusion"], data["thesis"],
                     data["why_now"], data["confirmed_facts"], data["subjective_assumptions"],
                     data["catalysts"], data["invalidation_conditions"], data["notes"],
                     data["review_at"], data["max_weight"], data["target_weight"], reason,
                     int(data["hold_only_review"]),
                     int(data["limit_confirmed"]), now, draft["id"]),
                )
                draft_id = draft["id"]
            else:
                draft_id = _id("ppv")
                conn.execute(
                    "INSERT INTO position_plan_versions "
                    "(id, position_cycle_id, account, version, strategy_type, current_conclusion, thesis, "
                    "why_now, confirmed_facts, subjective_assumptions, catalysts, "
                    "invalidation_conditions, notes, review_at, max_weight, target_weight, change_reason, supersedes_id, "
                    "created_at, created_by, lifecycle_status, updated_at, draft_of_version_id, "
                    "hold_only_review, limit_confirmed) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (draft_id, cycle_id, self.account, 0, data["strategy_type"],
                     data["current_conclusion"], data["thesis"], data["why_now"],
                     data["confirmed_facts"], data["subjective_assumptions"], data["catalysts"],
                     data["invalidation_conditions"], data["notes"], data["review_at"],
                     data["max_weight"], data["target_weight"], reason,
                     None, now, actor, VERSION_DRAFT, now, draft_of,
                     int(data["hold_only_review"]), int(data["limit_confirmed"])),
                )
            conn.execute(
                "DELETE FROM position_plan_actions WHERE plan_version_id=? AND status='DRAFT'",
                (draft_id,))
            for action in actions:
                self._insert_action_tx(conn, cycle, draft_id, action, now, status="DRAFT")
            status = "DRAFT" if _plan_has_content(data, actions) else "PENDING"
            conn.execute(
                "UPDATE position_cycles SET status=?, target_weight=COALESCE(?,target_weight), "
                "max_weight=COALESCE(?,max_weight), updated_at=? WHERE id=? AND account=?",
                (status, data["target_weight"], data["max_weight"], now, cycle_id, self.account),
            )
            draft_row = _row(conn.execute(
                "SELECT * FROM position_plan_versions WHERE id=?", (draft_id,)).fetchone())
            draft_row["max_weight"] = data["max_weight"]
            draft_row["target_weight"] = data["target_weight"]
            draft_row["actions"] = _rows(conn.execute(
                "SELECT * FROM position_plan_actions WHERE plan_version_id=? "
                "ORDER BY priority, created_at", (draft_id,)).fetchall())
            detail = {"draft": draft_row, "version": draft_row,
                      "status": status, "published": False,
                      "warnings": _plan_warnings(data["thesis"],
                                                  data["invalidation_conditions"])}
            conn.execute(
                "INSERT INTO position_plan_audit_log "
                "(account, position_cycle_id, actor, operation, idempotency_key, detail_json, created_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (self.account, cycle_id, actor, "SAVE_PLAN_DRAFT", idempotency_key,
                 _j(detail), now),
            )
            return detail

    def _validate_publish_tx(self, conn, cycle: dict, draft: dict,
                             actions: list[dict]) -> None:
        plan = {**draft, "max_weight": draft.get("max_weight") or cycle.get("max_weight")}
        missing = []
        if not _text(plan.get("thesis")):
            missing.append("核心购买理由")
        if not _text(plan.get("invalidation_conditions")):
            missing.append("投资逻辑失效条件")
        max_weight = _decimal(draft.get("max_weight") or cycle.get("max_weight"))
        if max_weight is None or not Decimal("0") < max_weight <= Decimal("1"):
            missing.append("最大允许账户仓位")
        if missing:
            raise PositionPlanError("正式发布前还需填写：" + "、".join(missing))
        for action in actions:
            if not _action_amount_present(action):
                raise PositionPlanError("每条正式操作计划必须填写数量或比例，且只能填写一项")
            if action.get("trigger_type") == "PRICE" and \
                    action.get("price_min") in (None, "") and action.get("price_max") in (None, ""):
                raise PositionPlanError("价格条件必须填写价格区间")
            if action.get("trigger_type") == "EVENT" and not _text(action.get("event_condition")):
                raise PositionPlanError("事件条件必须填写事件描述")
            estimate = self._add_estimate(cycle, action)
            if estimate and estimate.get("exceeds_max_weight") and not draft.get("limit_confirmed"):
                raise PositionPlanError("该加仓计划预计超过最大允许账户仓位，确认后才能发布")
        if not actions and not bool(draft.get("hold_only_review")):
            raise PositionPlanError("至少发布一条完整操作计划，或明确选择仅持有并在指定时间复盘")
        if not actions and not _text(draft.get("review_at")):
            raise PositionPlanError("仅持有并复盘必须设置下次复盘时间")
        previous = self._latest_plan_tx(conn, cycle["id"])
        reason = _text(draft.get("change_reason"))
        if previous and reason not in PLAN_CHANGE_REASONS:
            raise PositionPlanError("修改正式计划必须填写有效的修改原因")

    def publish_version(self, cycle_id: str, payload: dict | None = None, *,
                        actor: str | None = None,
                        idempotency_key: str | None = None) -> dict:
        """把当前草稿发布为不可变正式版本。"""
        actor = _text(actor) or self.actor
        if idempotency_key:
            with self.db.connect() as conn:
                existing = conn.execute(
                    "SELECT detail_json FROM position_plan_audit_log "
                    "WHERE idempotency_key=? AND account=?",
                    (idempotency_key, self.account)).fetchone()
                if existing:
                    return _json_load(existing["detail_json"], {})
        if payload is not None:
            # 草稿与发布的幂等键分开，避免发布在保存草稿后被短路成草稿响应。
            self.save_draft(cycle_id, payload, actor=actor)
        now = utcnow_iso()
        with self.db.connect() as conn:
            cycle = self._assert_cycle_tx(conn, cycle_id, writable=True)
            draft = self._draft_plan_tx(conn, cycle_id)
            if not draft:
                raise PositionPlanError("当前没有可发布的草稿")
            actions = _rows(conn.execute(
                "SELECT * FROM position_plan_actions WHERE plan_version_id=? AND status='DRAFT' "
                "ORDER BY priority, created_at", (draft["id"],)).fetchall())
            self._validate_publish_tx(conn, cycle, draft, actions)
            previous = self._latest_plan_tx(conn, cycle_id)
            # 版本号必须在该 cycle 全部历史版本里取最大 +1：唯一约束跨所有 lifecycle_status，
            # 只看「最新已发布」会漏掉早先降级为 LEGACY 的版本，导致号码重复撞约束。
            max_version = conn.execute(
                "SELECT COALESCE(MAX(version), 0) FROM position_plan_versions "
                "WHERE position_cycle_id=?", (cycle_id,)).fetchone()[0]
            version = int(max_version) + 1
            version_id = _id("ppv")
            conn.execute(
                "INSERT INTO position_plan_versions "
                "(id, position_cycle_id, account, version, strategy_type, current_conclusion, thesis, "
                "why_now, confirmed_facts, subjective_assumptions, catalysts, invalidation_conditions, "
                "notes, review_at, max_weight, target_weight, change_reason, supersedes_id, created_at, created_by, lifecycle_status, "
                "updated_at, draft_of_version_id, hold_only_review, limit_confirmed, published_at, published_by) "
                "SELECT ?, position_cycle_id, account, ?, strategy_type, current_conclusion, thesis, "
                "why_now, confirmed_facts, subjective_assumptions, catalysts, invalidation_conditions, "
                "notes, review_at, max_weight, target_weight, change_reason, ?, ?, ?, 'PUBLISHED', ?, id, hold_only_review, "
                "limit_confirmed, ?, ? "
                "FROM position_plan_versions WHERE id=?",
                (version_id, version, previous["id"] if previous else None, now, actor, now,
                 now, actor, draft["id"]),
            )
            for action in actions:
                self._insert_action_tx(conn, cycle, version_id, action, now, status="ACTIVE")
            conn.execute(
                "UPDATE position_plan_versions SET lifecycle_status='LEGACY', version=("
                "SELECT COALESCE(MIN(version), 0)-1 FROM position_plan_versions "
                "WHERE position_cycle_id=?), updated_at=? WHERE id=?",
                (cycle_id, now, draft["id"]),
            )
            conn.execute(
                "UPDATE position_cycles SET status='ACTIVE', updated_at=? WHERE id=? AND account=?",
                (now, cycle_id, self.account),
            )
            self._mark_triggered_tx(conn, cycle_id, {"price": cycle.get("current_price")}, now)
            status = self._plan_status_tx(conn, cycle_id, now)
            conn.execute(
                "UPDATE position_cycles SET status=?, updated_at=? WHERE id=? AND account=?",
                (status, now, cycle_id, self.account),
            )
            result = _row(conn.execute(
                "SELECT * FROM position_plan_versions WHERE id=?", (version_id,)).fetchone())
            result["actions"] = _rows(conn.execute(
                "SELECT * FROM position_plan_actions WHERE plan_version_id=? "
                "ORDER BY priority, created_at", (version_id,)).fetchall())
            detail = {"version": result, "status": status, "published": True,
                      "warnings": _plan_warnings(result.get("thesis"),
                                                  result.get("invalidation_conditions"))}
            conn.execute(
                "INSERT INTO position_plan_audit_log "
                "(account, position_cycle_id, actor, operation, idempotency_key, detail_json, created_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (self.account, cycle_id, actor, "PUBLISH_PLAN_VERSION", idempotency_key,
                 _j(detail), now),
            )
            return detail

    def create_version(self, cycle_id: str, payload: dict, *, actor: str | None = None,
                       idempotency_key: str | None = None) -> dict:
        """兼容旧调用方：create_version 现在表示正式发布。"""
        return self.publish_version(cycle_id, payload, actor=actor,
                                    idempotency_key=idempotency_key)

    def _insert_action_tx(self, conn, cycle: dict, version_id: str, payload: dict,
                          now: str, *, status: str = "DRAFT") -> dict:
        action_type = str(payload.get("action_type", payload.get("type", ""))).upper()
        trigger_type = str(payload.get("trigger_type", "MANUAL")).upper()
        if action_type not in ACTION_TYPES:
            raise PositionPlanError("操作方向必须是 ADD、REDUCE、EXIT 或 REVIEW")
        if trigger_type not in TRIGGER_TYPES:
            raise PositionPlanError("触发类型必须是 PRICE、EVENT、DATE 或 MANUAL")
        priority = int(payload.get("priority") or 100)
        if status not in ACTION_STATUSES:
            raise PositionPlanError("不支持的操作计划状态")
        action_id = _id("ppa")
        conn.execute(
            "INSERT INTO position_plan_actions "
            "(id, position_cycle_id, account, plan_version_id, action_type, trigger_type, price_min, "
            " price_max, event_condition, quantity, percentage, additional_conditions, "
            " priority, status, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (action_id, cycle["id"], self.account, version_id, action_type, trigger_type,
             _stored(payload.get("price_min")), _stored(payload.get("price_max")),
             _text(payload.get("event_condition")), _stored(payload.get("quantity")),
             _stored_percentage(payload.get("percentage")),
             _text(payload.get("additional_conditions")),
             priority, status, now),
        )
        return _row(conn.execute(
            "SELECT * FROM position_plan_actions WHERE id=?", (action_id,)).fetchone())

    def create_action(self, cycle_id: str, payload: dict, *, actor: str | None = None,
                      idempotency_key: str | None = None) -> dict:
        actor = _text(actor) or self.actor
        if idempotency_key:
            with self.db.connect() as conn:
                existing = conn.execute(
                    "SELECT detail_json FROM position_plan_audit_log "
                    "WHERE idempotency_key=? AND account=?",
                    (idempotency_key, self.account)).fetchone()
                if existing:
                    return _json_load(existing["detail_json"], {})
        with self.db.connect() as conn:
            cycle = self._assert_cycle_tx(conn, cycle_id, writable=True)
            plan = self._draft_plan_tx(conn, cycle_id) or self._latest_plan_tx(conn, cycle_id)
            if not plan:
                plan = {}
            existing_actions = _rows(conn.execute(
                "SELECT * FROM position_plan_actions WHERE plan_version_id=? "
                "AND status IN ('DRAFT','ACTIVE','TRIGGERED','EXECUTED') "
                "ORDER BY priority, created_at", (plan.get("id"),)).fetchall()) if plan else []
            full_payload = _copy_plan_fields(plan)
            full_payload.update({
                "max_weight": cycle.get("max_weight"),
                "target_weight": cycle.get("target_weight"),
                "hold_only_review": plan.get("hold_only_review", 0),
                "actions": [self._action_payload(row) for row in existing_actions] + [payload],
            })
            if payload.get("change_reason"):
                full_payload["change_reason"] = payload["change_reason"]
        result = self.save_draft(cycle_id, full_payload, actor=actor)
        draft_actions = (result.get("draft") or {}).get("actions") or []
        return draft_actions[-1] if draft_actions else result

    def update_action(self, cycle_id: str, action_id: str, *, status: str,
                      actor: str | None = None) -> dict:
        actor = _text(actor) or self.actor
        status = str(status or "").upper()
        if status not in ACTION_STATUSES:
            raise PositionPlanError("不支持的计划状态")
        with self.db.connect() as conn:
            self._assert_cycle_tx(conn, cycle_id, writable=True)
            row = conn.execute(
                "SELECT * FROM position_plan_actions WHERE id=? AND position_cycle_id=?",
                (action_id, cycle_id)).fetchone()
            if not row:
                raise PositionPlanError("操作计划不存在或不属于当前账户")
            now = utcnow_iso()
            conn.execute(
                "UPDATE position_plan_actions SET status=?, canceled_at=? WHERE id=?",
                (status, now if status == "CANCELED" else None, action_id),
            )
            conn.execute(
                "INSERT INTO position_plan_audit_log "
                "(account, position_cycle_id, actor, operation, detail_json, created_at) "
                "VALUES (?,?,?,?,?,?)",
                (self.account, cycle_id, actor, "UPDATE_PLAN_ACTION",
                 _j({"action_id": action_id, "status": status}), now),
            )
            return _row(conn.execute(
                "SELECT * FROM position_plan_actions WHERE id=?", (action_id,)).fetchone())

    @staticmethod
    def _recommendation_references_action(row: Any, action_id: str) -> bool:
        """建议必须真实引用 action；同 symbol 并不足以建立 provenance。"""
        try:
            refs = json.loads(row["input_refs"] or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            refs = {}
        if not isinstance(refs, dict):
            refs = {}
        try:
            covered = json.loads(row["covered_action_ids"] or "[]")
        except (TypeError, ValueError, json.JSONDecodeError):
            covered = []
        if not isinstance(covered, list):
            covered = []
        return refs.get("action_id") == action_id or action_id in covered

    def _recommendation_for_action_tx(self, conn, cycle: dict, action_id: str,
                                      recommendation_id: str | None) -> str | None:
        params: list[Any] = [self.account, cycle["symbol"]]
        sql = (
            "SELECT * FROM recommendation_snapshots WHERE account=? AND symbol=? "
            "AND status IN ('ACTIVE','BLOCKED') "
        )
        if recommendation_id:
            sql += "AND id=? "
            params.append(recommendation_id)
        sql += "ORDER BY created_at DESC"
        rows = conn.execute(sql, tuple(params)).fetchall()
        for row in rows:
            if self._recommendation_references_action(row, action_id):
                return row["id"]
        if recommendation_id:
            raise PositionPlanError("建议 ID 不存在、已失效或未引用该正式操作")
        return None

    def confirm_action_intent(self, cycle_id: str, action_id: str, *,
                              recommendation_id: str | None = None,
                              note: str | None = None, actor: str | None = None) -> dict:
        """记录人工执行意图；不创建订单，也不把确认等同于成交。"""
        actor = _text(actor) or self.actor
        with self.db.connect() as conn:
            cycle = self._assert_cycle_tx(conn, cycle_id, writable=True)
            action = conn.execute(
                "SELECT a.* FROM position_plan_actions a "
                "JOIN position_plan_versions v ON v.id=a.plan_version_id "
                "WHERE a.id=? AND a.position_cycle_id=? "
                "AND v.lifecycle_status='PUBLISHED'",
                (action_id, cycle_id),
            ).fetchone()
            if not action or action["status"] not in ("ACTIVE", "TRIGGERED"):
                raise PositionPlanError("只能确认当前正式计划中尚未成交的操作")
            if action["trigger_type"] == "PRICE" and action["status"] != "TRIGGERED":
                raise PositionPlanError("PRICE 条件尚未可靠触发，不能确认执行意图")
            recommendation_id = self._recommendation_for_action_tx(
                conn, cycle, action_id, recommendation_id)
            now = utcnow_iso()
            conn.execute(
                "UPDATE position_plan_actions SET status='TRIGGERED', "
                "triggered_at=COALESCE(triggered_at, ?), confirmed_at=?, confirmed_by=?, "
                "confirmation_intent='EXECUTE', confirmation_note=?, recommendation_id=? "
                "WHERE id=?",
                (now, now, actor, _text(note), recommendation_id, action_id),
            )
            conn.execute(
                "UPDATE position_cycles SET status='TRIGGERED', updated_at=? "
                "WHERE id=? AND account=?", (now, cycle_id, self.account))
            conn.execute(
                "INSERT INTO position_plan_audit_log "
                "(account, position_cycle_id, actor, operation, detail_json, created_at) "
                "VALUES (?,?,?,?,?,?)",
                (self.account, cycle_id, actor, "CONFIRM_ACTION_INTENT",
                 _j({"action_id": action_id, "intent": "EXECUTE",
                     "recommendation_id": recommendation_id, "note": note}), now),
            )
            return _row(conn.execute(
                "SELECT * FROM position_plan_actions WHERE id=?", (action_id,)).fetchone())

    def update_cycle_status(self, cycle_id: str, *, status: str,
                            actor: str | None = None) -> dict:
        actor = _text(actor) or self.actor
        status = str(status or "").upper()
        if status not in {"PENDING", "DRAFT", "ACTIVE", "REVIEW_DUE", "TRIGGERED", "INVALIDATED"}:
            raise PositionPlanError("不支持的持仓计划状态")
        with self.db.connect() as conn:
            cycle = self._assert_cycle_tx(conn, cycle_id, writable=True)
            if status in FORMAL_STATUSES and not self._latest_plan_tx(conn, cycle_id):
                raise PositionPlanError("只有已发布正式计划才能进入正式状态")
            now = utcnow_iso()
            conn.execute(
                "UPDATE position_cycles SET status=?, updated_at=? WHERE id=? AND account=?",
                (status, now, cycle_id, self.account),
            )
            conn.execute(
                "INSERT INTO position_plan_audit_log "
                "(account, position_cycle_id, actor, operation, detail_json, created_at) "
                "VALUES (?,?,?,?,?,?)",
                (self.account, cycle_id, actor, "UPDATE_CYCLE_STATUS",
                 _j({"status": status}), now),
            )
            return _row(conn.execute(
                "SELECT * FROM position_cycles WHERE id=?", (cycle_id,)).fetchone())

    def confirm_execution(self, cycle_id: str, execution_id: str, *, match_status: str,
                          action_id: str | None = None, deviation_reason: str | None = None,
                          actor: str | None = None) -> dict:
        actor = _text(actor) or self.actor
        match_status = str(match_status or "").upper()
        if match_status not in MATCH_STATUSES:
            raise PositionPlanError("不支持的成交匹配状态")
        with self.db.connect() as conn:
            self._assert_cycle_tx(conn, cycle_id, writable=True)
            action_row = None
            if match_status == "MATCHED" and not action_id:
                raise PositionPlanError("确认符合计划时必须选择正式操作")
            if match_status == "PLAN_OUTSIDE":
                action_id = None
            if action_id:
                action_row = conn.execute(
                    "SELECT a.* FROM position_plan_actions a "
                    "JOIN position_plan_versions v ON v.id=a.plan_version_id "
                    "WHERE a.id=? AND a.position_cycle_id=? AND v.lifecycle_status='PUBLISHED' "
                    "AND a.status IN ('ACTIVE','TRIGGERED','EXECUTED')",
                    (action_id, cycle_id)).fetchone()
                if not action_row:
                    raise PositionPlanError("只能把成交关联到已发布且未取消的正式操作计划")
            link = conn.execute(
                "SELECT * FROM position_plan_execution_links "
                "WHERE position_cycle_id=? AND execution_id=?",
                (cycle_id, execution_id)).fetchone()
            if not link:
                raise PositionPlanError("成交尚未关联到该持仓周期")
            event = conn.execute(
                "SELECT * FROM position_cycle_events "
                "WHERE position_cycle_id=? AND execution_id=?",
                (cycle_id, execution_id)).fetchone()
            if not event:
                raise PositionPlanError("成交事件不存在，不能人工确认关联")
            if action_row is not None:
                event_direction = ("ADD" if event["event_type"] in ("OPEN", "ADD")
                                   else "REDUCE" if event["event_type"] in ("REDUCE", "EXIT")
                                   else None)
                action_direction = ("ADD" if action_row["action_type"] == "ADD"
                                    else "REDUCE" if action_row["action_type"] in ("REDUCE", "EXIT")
                                    else None)
                if not event_direction or event_direction != action_direction:
                    raise PositionPlanError("成交方向与所选正式操作不一致")
            now = utcnow_iso()
            conn.execute(
                "UPDATE position_plan_execution_links SET plan_action_id=?, match_status=?, "
                "deviation_reason=?, matched_at=?, confirmed_by_user=? "
                "WHERE position_cycle_id=? AND execution_id=?",
                (action_id, match_status, _text(deviation_reason), now, actor,
                 cycle_id, execution_id),
            )
            conn.execute(
                "UPDATE position_cycle_events SET plan_action_id=?, match_status=?, "
                "deviation_reason=? WHERE position_cycle_id=? AND execution_id=?",
                (action_id, match_status, _text(deviation_reason), cycle_id, execution_id),
            )
            if match_status == "MATCHED" and action_row is not None:
                execution = conn.execute(
                    "SELECT execution_time FROM executions WHERE exec_id=?",
                    (execution_id,),
                ).fetchone()
                executed_at = execution["execution_time"] if execution else now
                next_status, complete = self._action_execution_state_tx(conn, action_row)
                conn.execute(
                    "UPDATE position_plan_actions SET status=?, executed_at=?, execution_id=? "
                    "WHERE id=?",
                    (next_status, executed_at if complete else None, execution_id, action_id))
                if action_row["confirmed_at"]:
                    decision_id = action_row["recommendation_id"] or action_id
                    conn.execute(
                        "UPDATE executions SET decision_id=COALESCE(decision_id, ?) "
                        "WHERE exec_id=?", (decision_id, execution_id))
                cycle_status = self._plan_status_tx(conn, cycle_id, now)
                conn.execute(
                    "UPDATE position_cycles SET status=?, updated_at=? "
                    "WHERE id=? AND account=?",
                    (cycle_status, now, cycle_id, self.account))
            conn.execute(
                "INSERT INTO position_plan_audit_log "
                "(account, position_cycle_id, actor, operation, detail_json, created_at) "
                "VALUES (?,?,?,?,?,?)",
                (self.account, cycle_id, actor, "CONFIRM_EXECUTION_MATCH",
                 _j({"execution_id": execution_id, "match_status": match_status,
                     "action_id": action_id, "deviation_reason": deviation_reason}), now),
            )
            return _row(conn.execute(
                "SELECT * FROM position_plan_execution_links "
                "WHERE position_cycle_id=? AND execution_id=?",
                (cycle_id, execution_id)).fetchone())

    # ---------- 清仓归档 ----------

    def _archive_snapshot_tx(self, conn, cycle: dict) -> tuple[dict, str]:
        versions = _rows(conn.execute(
            "SELECT * FROM position_plan_versions WHERE position_cycle_id=? "
            "ORDER BY version", (cycle["id"],)).fetchall())
        actions = _rows(conn.execute(
            "SELECT * FROM position_plan_actions WHERE position_cycle_id=? "
            "ORDER BY created_at, priority", (cycle["id"],)).fetchall())
        events = _rows(conn.execute(
            "SELECT * FROM position_cycle_events WHERE position_cycle_id=? "
            "ORDER BY execution_time, created_at", (cycle["id"],)).fetchall())
        links = _rows(conn.execute(
            "SELECT * FROM position_plan_execution_links WHERE position_cycle_id=? "
            "ORDER BY matched_at", (cycle["id"],)).fetchall())
        actions_by_version: dict[str, list[dict]] = {}
        for action in actions:
            actions_by_version.setdefault(action["plan_version_id"], []).append(action)
        for version in versions:
            version["actions"] = actions_by_version.get(version["id"], [])

        buy_qty = Decimal("0")
        sell_qty = Decimal("0")
        buy_value = Decimal("0")
        sell_value = Decimal("0")
        realized = Decimal("0")
        realized_known = False
        for event in events:
            qty = _decimal(event.get("quantity")) or Decimal("0")
            price = _decimal(event.get("price"))
            value = qty * price if price is not None else Decimal("0")
            if event["event_type"] in {"OPEN", "ADD"}:
                buy_qty += qty
                buy_value += value
            elif event["event_type"] in {"REDUCE", "EXIT"}:
                sell_qty += qty
                sell_value += value
            pnl = _decimal(event.get("realized_pnl"))
            if pnl is not None:
                realized += pnl
                realized_known = True
        matched = sum(1 for link in links if link["match_status"] == "MATCHED")
        outside = sum(1 for link in links if link["match_status"] == "PLAN_OUTSIDE")
        pending = sum(1 for link in links if link["match_status"] == "PENDING_CONFIRMATION")
        executable = matched + outside + pending
        execution_rate = (Decimal(matched) / Decimal(executable)
                           if executable else None)
        latest = versions[-1] if versions else {}
        holding_days = _iso_date_delta(cycle.get("opened_at"), cycle.get("closed_at"))
        metadata = {
            "source_type": "POSITION_PLAN_ARCHIVE",
            "position_cycle_id": cycle["id"],
            "account": cycle["account"],
            "symbol": cycle["symbol"],
            "cycle_number": cycle["cycle_number"],
            "opened_at": cycle.get("opened_at"),
            "closed_at": cycle.get("closed_at"),
            "holding_days": holding_days,
            "opening_execution_id": cycle.get("opening_execution_id"),
            "opening_quantity": cycle.get("opening_quantity"),
            "opening_average_price": cycle.get("opening_average_price"),
            "first_purchase_reason": latest.get("thesis"),
            "why_now": latest.get("why_now"),
            "catalysts": latest.get("catalysts"),
            "invalidation_conditions": latest.get("invalidation_conditions"),
            "plan_versions": versions,
            "events": events,
            "execution_links": links,
            "cumulative_buy_quantity": str(buy_qty),
            "cumulative_buy_value": str(buy_value),
            "cumulative_sell_quantity": str(sell_qty),
            "cumulative_sell_value": str(sell_value),
            "realized_pnl": str(realized) if realized_known else None,
            "planned_operations": matched,
            "plan_outside_operations": outside,
            "pending_confirmation_operations": pending,
            "plan_execution_rate": str(execution_rate) if execution_rate is not None else None,
            "final_summary": latest.get("notes"),
        }
        lines = [
            f"# {cycle['symbol']} 持仓周期 #{cycle['cycle_number']} 归档",
            "",
            "> 来源：持仓周期归档（只读历史快照，仅同步到投资日记，不写入交易复盘）",
            "",
            f"- 账户：{cycle['account']}",
            f"- 持仓周期：{cycle['id']}",
            f"- 建仓：{format_datetime(cycle.get('opened_at'), self.local_tz) if cycle.get('opened_at') else '未知'}",
            f"- 清仓：{format_datetime(cycle.get('closed_at'), self.local_tz) if cycle.get('closed_at') else '未知'}",
            f"- 持有天数：{holding_days if holding_days is not None else '未知'}",
            "",
            "## 投资逻辑",
            f"- 首次购买理由：{latest.get('thesis') or '未填写'}",
            f"- 为什么当时购买：{latest.get('why_now') or '未填写'}",
            f"- 催化剂：{latest.get('catalysts') or '未填写'}",
            f"- 逻辑失效条件：{latest.get('invalidation_conditions') or '未填写'}",
            "",
            "## 成交与执行",
            f"- 累计买入：{format_quantity(buy_qty)} 股 / {format_money(buy_value)}",
            f"- 累计卖出：{format_quantity(sell_qty)} 股 / {format_money(sell_value)}",
            f"- 已实现盈亏：{format_money(realized) if realized_known else '数据不可用'}",
            f"- 计划内操作：{matched}",
            f"- 计划外操作：{outside}",
            f"- 待确认操作：{pending}",
            f"- 计划执行率：{execution_rate if execution_rate is not None else '不可计算'}",
            "",
            "## 最终总结",
            latest.get("notes") or "清仓前未填写最终总结。",
            "",
            f"原始持仓计划关联 ID：{cycle['id']}",
        ]
        return metadata, "\n".join(lines)[:20000]

    def _mark_archive_failed(self, cycle_id: str, error: str) -> None:
        with self.db.connect() as conn:
            conn.execute(
                "UPDATE position_cycles SET journal_sync_status='FAILED', "
                "journal_sync_error=?, updated_at=? WHERE id=? AND account=?",
                (error[:2000], utcnow_iso(), cycle_id, self.account),
            )

    def archive_cycle(self, cycle_id: str) -> dict:
        """把 CLOSED 周期以不可变快照幂等写入现有投资日记。"""
        try:
            with self.db.connect() as conn:
                cycle = self._assert_cycle_tx(conn, cycle_id)
                if cycle["status"] not in ARCHIVED_STATUSES:
                    raise PositionPlanError("只有已确认清仓的持仓周期才能归档")
                existing = conn.execute(
                    "SELECT * FROM journal_entries WHERE position_cycle_id=?",
                    (cycle_id,)).fetchone()
                if existing:
                    entry = dict(existing)
                    if entry.get("source_type") != "POSITION_PLAN_ARCHIVE":
                        raise PositionPlanError("持仓周期关联的投资日记记录来源不正确")
                    entry_id = entry["id"]
                    metadata_json = entry.get("metadata_json")
                else:
                    metadata, content = self._archive_snapshot_tx(conn, cycle)
                    now = utcnow_iso()
                    cur = conn.execute(
                        "INSERT INTO journal_entries "
                        "(content, created_at, updated_at, source_type, position_cycle_id, "
                        " account, is_readonly, metadata_json, archived_at) "
                        "VALUES (?,?,?,?,?,?,?,?,?)",
                        (content, now, now, "POSITION_PLAN_ARCHIVE", cycle_id,
                         self.account, 1, _j(metadata), cycle.get("closed_at") or now),
                    )
                    entry_id = cur.lastrowid
                    metadata_json = _j(metadata)
                conn.execute(
                    "UPDATE position_cycles SET status='ARCHIVED', journal_sync_status='SYNCED', "
                    "journal_sync_error=NULL, investment_journal_entry_id=?, updated_at=? "
                    "WHERE id=? AND account=?",
                    (entry_id, utcnow_iso(), cycle_id, self.account),
                )
                return {"cycle_id": cycle_id, "status": "SYNCED",
                        "journal_entry_id": entry_id, "metadata_json": metadata_json}
        except Exception as exc:  # noqa: BLE001 — 归档失败不回滚清仓事实
            self._mark_archive_failed(cycle_id, f"{type(exc).__name__}: {exc}")
            return {"cycle_id": cycle_id, "status": "FAILED", "error": str(exc)}

    def retry_archive(self, cycle_id: str) -> dict:
        with self.db.connect() as conn:
            cycle = self._assert_cycle_tx(conn, cycle_id)
            if cycle["status"] not in ARCHIVED_STATUSES and cycle.get(
                    "journal_sync_status") != "FAILED":
                raise PositionPlanError("当前持仓周期没有可重试的投资日记同步失败")
            if cycle["status"] == "CLOSED":
                pass
            elif cycle["status"] == "ARCHIVED" and cycle.get("journal_sync_status") != "FAILED":
                return {"cycle_id": cycle_id, "status": "SYNCED",
                        "journal_entry_id": cycle.get("investment_journal_entry_id")}
            conn.execute(
                "UPDATE position_cycles SET status='CLOSED', journal_sync_status='PENDING', "
                "journal_sync_error=NULL, updated_at=? WHERE id=? AND account=?",
                (utcnow_iso(), cycle_id, self.account),
            )
        return self.archive_cycle(cycle_id)

    # ---------- 查询 / 待处理队列 ----------

    def _cycle_detail_tx(self, conn, cycle_id: str) -> dict:
        cycle = self._assert_cycle_tx(conn, cycle_id)
        versions = _rows(conn.execute(
            "SELECT * FROM position_plan_versions WHERE position_cycle_id=? "
            "AND lifecycle_status='PUBLISHED' ORDER BY version DESC", (cycle_id,)).fetchall())
        draft = _row(conn.execute(
            "SELECT * FROM position_plan_versions WHERE position_cycle_id=? "
            "AND lifecycle_status='DRAFT' ORDER BY updated_at DESC LIMIT 1", (cycle_id,)).fetchone())
        actions = _rows(conn.execute(
            "SELECT * FROM position_plan_actions WHERE position_cycle_id=? "
            "ORDER BY created_at DESC, priority", (cycle_id,)).fetchall())
        events = _rows(conn.execute(
            "SELECT e.*, l.confirmed_by_user FROM position_cycle_events e "
            "LEFT JOIN position_plan_execution_links l "
            "ON l.position_cycle_id=e.position_cycle_id AND l.execution_id=e.execution_id "
            "WHERE e.position_cycle_id=? "
            "ORDER BY e.execution_time DESC, e.created_at DESC", (cycle_id,)).fetchall())
        links = _rows(conn.execute(
            "SELECT * FROM position_plan_execution_links WHERE position_cycle_id=? "
            "ORDER BY matched_at DESC", (cycle_id,)).fetchall())
        observations = _rows(conn.execute(
            "SELECT * FROM position_cycle_observations WHERE position_cycle_id=? "
            "ORDER BY observed_at DESC LIMIT 100", (cycle_id,)).fetchall())
        actions_by_version: dict[str, list[dict]] = {}
        for action in actions:
            action["action_label"] = action_label(action.get("action_type"))
            action["trigger_label"] = trigger_label(action.get("trigger_type"))
            action["status_label"] = action_status_label(action.get("status"))
            action["display"] = self._format_action(action)
            actions_by_version.setdefault(action["plan_version_id"], []).append(action)
        if draft and not _plan_has_content(draft, actions_by_version.get(draft["id"], [])):
            draft = None
        for version in versions:
            version["actions"] = actions_by_version.get(version["id"], [])
            version["lifecycle_label"] = version_label(version.get("lifecycle_status"))
            version["created_display"] = version.get("created_at")
        if draft:
            draft["actions"] = actions_by_version.get(draft["id"], [])
            draft["lifecycle_label"] = version_label(draft.get("lifecycle_status"))
            draft["created_display"] = draft.get("updated_at") or draft.get("created_at")
            draft["max_weight"] = cycle.get("max_weight")
        cycle["latest_plan"] = versions[0] if versions else None
        cycle["formal_plan"] = versions[0] if versions else None
        cycle["draft_plan"] = draft
        cycle["versions"] = versions
        cycle["actions"] = actions
        cycle["events"] = events
        cycle["execution_links"] = links
        cycle["observations"] = observations
        cycle["read_only"] = cycle["status"] in ARCHIVED_STATUSES
        cycle["status_label"] = status_label(cycle.get("status"))
        cycle["account_weight_display"] = format_percent(cycle.get("account_weight"))
        cycle["stock_weight_display"] = format_percent(cycle.get("stock_weight"))
        cycle["max_weight_display"] = format_percent(cycle.get("max_weight"))
        return cycle

    def detail(self, cycle_id: str) -> dict:
        with self.db.connect() as conn:
            return self._cycle_detail_tx(conn, cycle_id)

    @staticmethod
    def _format_action(action: dict) -> str:
        return action_display(action)

    @staticmethod
    def _add_estimate(cycle: dict, action: dict | None) -> dict | None:
        if not action or action.get("action_type") != "ADD":
            return None
        current_qty = _decimal(cycle.get("current_quantity")) or Decimal("0")
        current_weight = _decimal(cycle.get("account_weight"))
        max_weight = _decimal(cycle.get("max_weight"))
        price = (_decimal(action.get("price_min")) or
                 _decimal(action.get("price_max")) or
                 _decimal(cycle.get("current_price")) or
                 _decimal(cycle.get("current_average_cost")))
        qty = _decimal(action.get("quantity"))
        pct = _decimal(action.get("percentage"))
        if qty is None and pct is not None:
            qty = current_qty * (pct if pct <= 1 else pct / Decimal("100"))
        if qty is None or qty <= 0:
            return None
        post_qty = current_qty + qty
        estimate = {
            "quantity": str(qty),
            "post_quantity": str(post_qty),
            "post_quantity_display": format_quantity(post_qty),
            "price_display": format_price(price),
            "post_account_weight": None,
            "post_account_weight_display": "—",
            "exceeds_max_weight": False,
            "headroom_quantity": None,
            "headroom_quantity_display": "—",
            "headroom_amount_display": "—",
        }
        if price is None or current_weight is None or max_weight is None or \
                current_weight <= 0 or max_weight <= 0:
            return estimate
        market_value = _decimal(cycle.get("current_market_value"))
        implied_equity = market_value / current_weight if market_value and current_weight else None
        if implied_equity is None or implied_equity <= 0:
            return estimate
        post_weight = current_weight + (qty * price / implied_equity)
        headroom = max(Decimal("0"), (max_weight - current_weight) * implied_equity / price)
        estimate["post_account_weight"] = str(post_weight)
        estimate["post_account_weight_display"] = format_percent(post_weight)
        estimate["exceeds_max_weight"] = post_weight > max_weight
        estimate["headroom_quantity"] = str(headroom)
        estimate["headroom_quantity_display"] = format_quantity(headroom)
        estimate["headroom_amount_display"] = format_money(headroom * price)
        return estimate

    def dashboard(self, *, now: str | None = None) -> dict:
        now = now or utcnow_iso()
        with self.db.connect() as conn:
            cycles = _rows(conn.execute(
                "SELECT * FROM position_cycles WHERE account=? AND status IN "
                "('PENDING','DRAFT','ACTIVE','REVIEW_DUE','TRIGGERED','INVALIDATED') "
                "ORDER BY CASE status WHEN 'PENDING' THEN 0 WHEN 'DRAFT' THEN 1 ELSE 2 END, symbol",
                (self.account,)).fetchall())
            cards = []
            todos = []
            next_review_at = None
            counts = {
                "current_positions": len(cycles), "active": 0, "pending": 0,
                "draft": 0, "review_due": 0, "triggered": 0,
                "plan_outside": 0, "journal_failed": 0,
            }
            for cycle in cycles:
                plan = self._latest_plan_tx(conn, cycle["id"])
                if plan and plan.get("review_at") and (
                        next_review_at is None or plan["review_at"] < next_review_at):
                    next_review_at = plan["review_at"]
                draft = self._draft_plan_tx(conn, cycle["id"])
                draft_actions = _rows(conn.execute(
                    "SELECT * FROM position_plan_actions WHERE plan_version_id=? "
                    "AND status='DRAFT'", (draft["id"],)).fetchall()) if draft else []
                if draft and not _plan_has_content(draft, draft_actions):
                    draft = None
                plan_for_core = {**plan, "max_weight": cycle.get("max_weight")} if plan else None
                plan_ok = _plan_has_core(plan_for_core, cycle)
                actions = _rows(conn.execute(
                    "SELECT * FROM position_plan_actions WHERE position_cycle_id=? "
                    "AND plan_version_id=? AND status IN ('ACTIVE','TRIGGERED') "
                    "ORDER BY priority, created_at", (cycle["id"], plan["id"] if plan else "")).fetchall()
                    if plan else [])
                triggered = [a for a in actions if a.get("status") == "TRIGGERED"]
                due = bool(plan and plan.get("review_at") and plan["review_at"] <= now)
                if cycle.get("status") == "INVALIDATED":
                    effective_status = "INVALIDATED"
                elif plan:
                    effective_status = "TRIGGERED" if triggered else (
                        "REVIEW_DUE" if due else "ACTIVE")
                elif draft and _plan_has_content(draft, draft_actions):
                    effective_status = "DRAFT"
                else:
                    effective_status = "PENDING"
                metric_key = {"ACTIVE": "active", "PENDING": "pending", "DRAFT": "draft",
                              "REVIEW_DUE": "review_due", "TRIGGERED": "triggered"}.get(
                                  effective_status)
                if metric_key:
                    counts[metric_key] += 1
                if triggered:
                    counts["triggered"] += max(0, len(triggered) - (1 if effective_status == "TRIGGERED" else 0))
                outside = conn.execute(
                    "SELECT COUNT(*) FROM position_plan_execution_links "
                    "WHERE position_cycle_id=? AND match_status='PLAN_OUTSIDE' "
                    "AND (confirmed_by_user IS NULL OR confirmed_by_user='')",
                    (cycle["id"],)).fetchone()[0]
                counts["plan_outside"] += int(outside)
                if cycle.get("journal_sync_status") == "FAILED":
                    counts["journal_failed"] += 1
                # 待处理队列：优先回答「现在该处理哪只持仓」，rank 越小越紧急。
                def todo(rank: str, level: str, text: str) -> None:
                    todos.append({"cycle_id": cycle["id"], "symbol": cycle["symbol"],
                                  "rank": rank, "level": level, "text": text})
                if cycle.get("journal_sync_status") == "FAILED":
                    todo("0", "crit", "清仓归档同步投资日记失败，需要重试")
                if effective_status == "PENDING":
                    todo("1", "crit", "新建仓尚未填写计划")
                if triggered:
                    todo("2", "warn", f"{len(triggered)} 条操作条件已触发，请确认是否执行")
                if int(outside):
                    todo("3", "warn", f"有 {int(outside)} 次计划外操作待说明")
                if due:
                    todo("4", "warn", "已到下次复盘时间")
                if effective_status == "DRAFT":
                    todo("5", "warn", "草稿尚未发布，暂不参与纪律判断")
                next_action = triggered[0] if triggered else next(iter(actions), None)
                exits = [a for a in actions if a["action_type"] == "EXIT"]
                # 卡片要同时显示加仓、减仓两条：已发布看 actions，仅草稿看 draft_actions。
                display_actions = actions if plan else draft_actions
                display_plan = plan or draft
                account_weight = cycle.get("account_weight")
                stock_weight = cycle.get("stock_weight")
                cards.append({
                    **cycle,
                    "status": effective_status,
                    "status_label": status_label(effective_status),
                    "has_plan": plan_ok,
                    "has_published_plan": bool(plan),
                    "latest_plan": display_plan,
                    "formal_plan": plan,
                    "draft_plan": draft,
                    "next_action": self._format_action(next_action) if plan and next_action
                        else ("草稿尚未发布" if draft else "请完善并发布计划"),
                    "next_action_id": next_action.get("id") if next_action else None,
                    "risk_exit_line": self._format_action(exits[0]) if exits else "退出计划未设置",
                    "triggered_actions": triggered,
                    "plan_outside_count": int(outside),
                    "review_due": due,
                    "updated_display": cycle.get("updated_at"),
                    "status_display": status_label(effective_status),
                    "account_weight_display": format_percent(account_weight),
                    "stock_weight_display": format_percent(stock_weight),
                    "max_weight_display": format_percent(cycle.get("max_weight")),
                    "current_price_display": format_price(cycle.get("current_price")),
                    "current_average_cost_display": format_price(cycle.get("current_average_cost")),
                    "current_quantity_display": format_quantity(cycle.get("current_quantity")),
                    "next_action_data": next_action,
                    "plan_actions": [self._format_action(a) for a in display_actions],
                    "add_estimate": self._add_estimate(cycle, next_action),
                })
            todos.sort(key=lambda item: (item["rank"], item["symbol"]))
            return {"account": self.account, "cards": cards, "counts": counts,
                    "todos": todos, "next_review_at": next_review_at}

    def list_cycles(self, *, include_closed: bool = False, limit: int = 100) -> list[dict]:
        statuses = ACTIVE_STATUSES + (ARCHIVED_STATUSES if include_closed else ())
        placeholders = ",".join("?" for _ in statuses)
        with self.db.connect() as conn:
            return _rows(conn.execute(
                f"SELECT * FROM position_cycles WHERE account=? AND status IN ({placeholders}) "
                "ORDER BY opened_at DESC LIMIT ?",
                [self.account, *statuses, int(limit)]).fetchall())


def dashboard_for_account(db: Database, account: str | None = None,
                          *, now: str | None = None) -> dict:
    """页面入口：只从最新账户快照选择默认账户，不跨账户合并。"""
    if not account:
        row = db.latest_snapshot_row()
        account = row["account"] if row is not None else None
    if not account:
        return {"account": None, "cards": [], "counts": {
            "current_positions": 0, "active": 0, "pending": 0, "draft": 0,
            "review_due": 0, "triggered": 0, "plan_outside": 0,
            "journal_failed": 0,
        }, "todos": [], "next_review_at": None}
    return PositionPlanService(db, str(account)).dashboard(now=now)
