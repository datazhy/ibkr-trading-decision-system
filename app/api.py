"""JSON API：账户快照层对外接口。

面向后续的风险分析、交易复盘与 AI 日报 —— 这些消费方拿到的不能只是数字，
必须同时拿到「这份数字有多可信」。因此每个返回快照的接口都强制带上：

    snapshot_id / data_as_of / quality_status / quality_issues / ai_analysis_allowed

统一响应结构 {success, data, error, request_id, generated_at}：
request_id 同时写进日志与 sync_runs，出问题时能把「用户看到的那一次请求」
和「后台那一次同步」串起来。

交易边界：写接口只维护本地决策数据、触发只读同步或创建待执行计划草稿；
不涉及任何下单、改单、转账。
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse

from . import backtest as bt_
from . import cashflow_model as cfm_
from . import flex_sync
from . import performance as perf_
from . import opportunity_service as opp_
from . import reconciliation_cash as recon_
from . import reconciliation_currency as reconccy_
from . import replay_service as replay_
from . import unrealized_pnl as upnl_
from . import combined_events as ce_
from . import data_quality as dq
from . import event_service
from . import risk_events as rev_
from . import outcome_labels as ol_
from . import review_service
from . import risk_trends as rt_
from . import trading_calendar as tc_
from . import rule_performance as rperf
from . import threshold_advisor as ta_
from . import market_data as md_
from . import market_engine as me_
from . import market_service
from . import futu_quotes
from . import position_plans as pp_
from . import risk_engine as re_
import threading

from . import risk_service
from . import sync as sync_mod
from . import brief_prompt as _bp
from . import event_annotations as _ea
from . import brief_service as _bsvc
from .config import Config
from .db import Database
from .logging_setup import get_logger
from .market_hours import CLOSED, market_session
from .position_pnl import aggregate_realized_for_position, dec, revalue_position, valid_price
from .hyperliquid_db import HyperliquidDatabase
from .hyperliquid_service import HyperliquidService

log = get_logger()


# ---------------- 统一响应 ----------------

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_request_id() -> str:
    return uuid.uuid4().hex[:16]


def ok(data: Any, request_id: str, status_code: int = 200) -> JSONResponse:
    return JSONResponse(status_code=status_code, content={
        "success": True, "data": jsonable(data), "error": None,
        "request_id": request_id, "generated_at": _now_iso(),
    })


def fail(code: str, message: str, request_id: str, status_code: int = 400,
         data: Any = None) -> JSONResponse:
    return JSONResponse(status_code=status_code, content={
        "success": False, "data": data,
        "error": {"code": code, "message": message},
        "request_id": request_id, "generated_at": _now_iso(),
    })


def _num(v) -> str | None:
    """金额一律以字符串出参。

    JSON 的 number 是 IEEE 754 双精度，序列化金融金额会悄悄丢精度
    （0.1+0.2 那类问题），而消费方往往直接拿去累加。字符串强迫对端显式选择
    自己的十进制类型。
    """
    if v is None:
        return None
    return str(v if isinstance(v, Decimal) else Decimal(str(v)))


def jsonable(v: Any) -> Any:
    """递归把 Decimal 转成字符串。

    金额与比例都以字符串出参：JSON number 是双精度，序列化金融数值会丢精度，
    而消费方常常直接拿去累加。字符串强迫对端显式选择自己的十进制类型。
    """
    if isinstance(v, Decimal):
        return str(v)
    if isinstance(v, dict):
        return {k: jsonable(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [jsonable(x) for x in v]
    return v


def _snapshot_payload(row: dict) -> dict:
    """账户汇总行 -> API 输出。字段名用系统标准名，不透传 IBKR 原始命名。"""
    return {
        "snapshot_id": row.get("snapshot_id"),
        "account": row.get("account"),
        "data_as_of": row.get("ts"),
        "currency": row.get("currency"),
        "net_liquidation": _num(row.get("equity")),
        "cash": _num(row.get("cash")),
        "total_cash": _num(row.get("total_cash")),
        "available_funds": _num(row.get("available_funds")),
        "excess_liquidity": _num(row.get("excess_liquidity")),
        "init_margin": _num(row.get("init_margin")),
        "maint_margin": _num(row.get("maint_margin")),
        "positions_value": _num(row.get("positions_value")),
        "positions_value_source": row.get("positions_value_source"),
        "buying_power": _num(row.get("buying_power")),
        "margin_balance": _num(row.get("margin_balance")),
        "quality_status": row.get("quality_status"),
        "ai_analysis_allowed": (None if row.get("ai_analysis_allowed") is None
                                else bool(row.get("ai_analysis_allowed"))),
        "sync_run_id": row.get("sync_run_id"),
        "source": row.get("source"),
    }


def _position_payload(row: dict) -> dict:
    # API 当前态也必须与页面首屏使用同一重估公式；这里只改变派生展示值，不回写快照。
    revalue_position(row, price=row.get("price"), source=row.get("source"))
    price = valid_price(row.get("price"))
    row["price"] = price
    try:
        previous_close = valid_price(row.get("previous_close"))
        daily_change = price - previous_close if previous_close > 0 else None
        daily_change_percent = (daily_change / previous_close
                                if daily_change is not None else None)
    except (ArithmeticError, TypeError, ValueError):
        daily_change = daily_change_percent = None
    return {
        "symbol": row.get("symbol"),
        "qty": _num(row.get("qty")),
        "avg_cost": _num(row.get("avg_cost")),
        "price": _num(row.get("price")),
        "price_source": row.get("price_source"),
        "price_quality": row.get("price_quality"),
        "quote_as_of": row.get("quote_as_of"),
        "source": row.get("source"),
        "market_value": _num(row.get("market_value")),
        "unrealized_pnl": _num(row.get("upl")), "upl": _num(row.get("upl")),
        "market_value_source": row.get("market_value_source"),
        "upl_source": row.get("upl_source"),
        "previous_close": _num(row.get("previous_close")),
        "daily_change": _num(daily_change),
        "daily_change_percent": _num(daily_change_percent),
        "day_pnl": _num(row.get("day_pnl")),
        "day_pnl_source": row.get("day_pnl_source"),
        "realized_pnl": _num(row.get("realized_pnl")),
        "realized_pnl_source": row.get("realized_pnl_source"),
        "currency": row.get("currency"),
        "conid": row.get("conid"),
    }


def _overlay_futu_position_quotes(positions: list[dict], client, now: datetime) -> None:
    """用富途 OpenD 同源现价/昨收覆盖行情字段；空结果保留原 IBKR 值。"""
    if client is None or not positions:
        return
    symbols = [str(p.get("symbol") or "") for p in positions if p.get("symbol")]
    try:
        quotes = client.current(symbols, now=now) or {}
    except Exception as exc:  # noqa: BLE001
        log.warning("Web 富途行情不可用，保留 IBKR 回退：%s", type(exc).__name__)
        for position in positions:
            if str(position.get("source") or "").startswith("FUTU"):
                position["price_quality"] = "STALE"
            revalue_position(position, price=position.get("price"),
                             source=position.get("source"))
        return
    for position in positions:
        quote = quotes.get(str(position.get("symbol") or ""))
        if not quote or valid_price(quote.get("price")) is None:
            # 已有富途价保留，但标成过期，不能在富途故障时继续伪装成实时。
            if str(position.get("source") or "").startswith("FUTU"):
                position["price_quality"] = "STALE"
            revalue_position(position, price=position.get("price"),
                             source=position.get("source"))
            continue
        position.update({
            "price": valid_price(quote.get("price")),
            "previous_close": quote.get("previous_close"),
            "price_source": quote.get("price_source"),
            "price_quality": quote.get("quality"),
            "quote_as_of": quote.get("as_of"),
            "source": quote.get("source"),
        })
        revalue_position(position, price=quote.get("price"),
                         source=quote.get("source"))


def _run_payload(row) -> dict | None:
    if row is None:
        return None
    r = dict(row)
    return {
        "sync_run_id": r.get("sync_run_id"),
        "state": r.get("run_state") or r.get("status"),
        "trigger": r.get("trigger"),
        "started_at": r.get("started_at"),
        "finished_at": r.get("finished_at"),
        "snapshot_id": r.get("snapshot_id"),
        "fetch_ms": r.get("fetch_ms"),
        "write_ms": r.get("write_ms"),
        "error_code": r.get("error_code"),
        "error_message": r.get("error_message"),
        "retry_count": r.get("retry_count"),
        "positions_count": r.get("positions_count"),
        "request_id": r.get("request_id"),
    }


def _freshness(cfg: Config, db: Database, row: dict | None, now: datetime) -> dict:
    """基于数据时点重算新鲜度 —— 快照写入时是 fresh，放到现在可能已经 stale。

    这是必须的：quality_status 存的是**写入那一刻**的结论，而 API 是**此刻**被调用的。
    直接把落库时的 fresh 返回给消费方，等于让一份两天前的数据自称新鲜。
    """
    if not row:
        return {"status": dq.ERROR, "age_seconds": None,
                "reason": "没有任何账户快照"}
    th = dq.thresholds_from_config(cfg)
    market_open = market_session(now, cfg.market_tz) != CLOSED
    issues = dq.check_freshness(row.get("ts"), now, market_open=market_open, th=th)
    try:
        age = (now - datetime.fromisoformat(row["ts"])).total_seconds()
    except (TypeError, ValueError, KeyError):
        age = None
    stored = row.get("quality_status") or dq.FRESH
    # 取「落库结论」与「此刻新鲜度」中更差的那个
    live = dq.ERROR if any(i.severity == dq.CRITICAL for i in issues) else (
        dq.STALE if issues else dq.FRESH)
    rank = {dq.FRESH: 0, dq.STALE: 1, dq.ERROR: 2}
    status = stored if rank.get(stored, 0) >= rank[live] else live
    return {"status": status, "age_seconds": None if age is None else int(age),
            "market_open": market_open,
            "stale_threshold_seconds": (th.stale_seconds_market if market_open
                                        else th.stale_seconds_closed),
            "live_issues": [i.as_dict() for i in issues]}


def _jload(v, default):
    try:
        return json.loads(v) if v else default
    except (TypeError, ValueError):
        return default


def _review_payload(r: dict) -> dict:
    """复盘行 -> API 输出。JSON 列展开，前端不必再解析一层。"""
    return {
        "review_id": r.get("review_id"), "event_id": r.get("event_id"),
        "source_type": r.get("source_type"), "rule_code": r.get("rule_code"),
        "rule_version": r.get("rule_version"),
        "rule_set_version": r.get("rule_set_version"),
        "affected_symbol": r.get("affected_symbol"),
        "affected_instrument": r.get("affected_instrument"),
        "first_detected_at": r.get("first_detected_at"),
        "peak_severity": r.get("peak_severity"),
        "peak_actual_value": r.get("peak_actual_value"),
        "resolved_at": r.get("resolved_at"),
        "duration_minutes": r.get("duration_minutes"),
        "account_value_at_open": r.get("account_value_at_open"),
        "account_value_at_resolution": r.get("account_value_at_resolution"),
        "market_state_at_open": r.get("market_state_at_open"),
        "account_risk_at_open": r.get("account_risk_at_open"),
        "market_risk_at_open": r.get("market_risk_at_open"),
        "forward_outcomes": _jload(r.get("forward_outcomes"), {}),
        "outcome_labels": _jload(r.get("outcome_labels"), {}),
        "trading_activity": _jload(r.get("trading_activity"), {}),
        "lead_time_minutes": r.get("lead_time_minutes"),
        "lead_time_class": r.get("lead_time_class"),
        "cash_flow_status": r.get("cash_flow_status"),
        "cash_flow_note": r.get("cash_flow_note"),
        "review_status": r.get("review_status"),
        "exclusion_reason": r.get("exclusion_reason"),
        "data_quality_status": r.get("data_quality_status"),
        "calculation_version": r.get("calculation_version"),
    }


def _perf_payload(r: dict) -> dict:
    out = dict(r)
    out["score_components"] = _jload(r.get("score_components"), {})
    return out


def _flex_run_payload(r: dict) -> dict:
    """同步任务 -> API 输出。**query_id_masked 只有后 4 位，token 完全不存在。**"""
    return {
        "run_id": r.get("run_id"), "status": r.get("status"),
        "query_id_masked": r.get("query_id_masked"),
        "started_at": r.get("started_at"), "completed_at": r.get("completed_at"),
        "report_from": r.get("report_from"), "report_to": r.get("report_to"),
        "records_received": r.get("records_received"),
        "records_inserted": r.get("records_inserted"),
        "records_updated": r.get("records_updated"),
        "records_skipped": r.get("records_skipped"),
        "realized_pnl_rows": r.get("realized_pnl_rows"),
        "retry_count": r.get("retry_count"), "duration_ms": r.get("duration_ms"),
        "error_code": r.get("error_code"),
        "error_message": r.get("error_message"),
        "section_counts": _jload(r.get("section_counts"), {}),
    }


def _cash_flow_payload(r: dict) -> dict:
    cat = r.get("category") or "external_cash_flow"
    return {
        "cashflow_id": r.get("cashflow_id"),
        "broker_transaction_id": r.get("broker_transaction_id"),
        "account_id": r.get("account_id") or r.get("account"),
        "occurred_at": r.get("occurred_at") or r.get("effective_at"),
        "settled_at": r.get("settled_at"), "et_date": r.get("et_date"),
        "category": cat, "subtype": r.get("subtype"),
        "is_external": cat in _EXTERNAL_CATS,
        "direction": r.get("direction"),
        "amount": r.get("amount"), "currency": r.get("currency"),
        "amount_base": r.get("amount_base"), "fx_rate": r.get("fx_rate"),
        "symbol": r.get("symbol"), "conid": r.get("conid"),
        "description": r.get("description") or r.get("note"),
        "source": r.get("source"), "quality_status": r.get("quality_status"),
        "created_at": r.get("created_at"), "updated_at": r.get("updated_at"),
    }


_EXTERNAL_CATS = ("external_cash_flow", "transfer")


def _resolve_perf_range(range_key: str | None, start: str | None,
                        end: str | None) -> tuple[str, str]:
    """解析收益区间。支持 7d/30d/90d/ytd 与自定义。"""
    today = datetime.now(timezone.utc).date()
    if start or end:
        return (str(start or (today - timedelta(days=30)))[:10],
                str(end or today)[:10])
    key = (range_key or "30d").lower()
    if key == "ytd":
        return f"{today.year}-01-01", today.isoformat()
    days = {"7d": 7, "30d": 30, "90d": 90}.get(key, 30)
    return (today - timedelta(days=days)).isoformat(), today.isoformat()



#: 每个重放响应都带这句。模拟结果被当成真实告警看，是这一层最大的风险。
_REPLAY_DISCLAIMER = (
    "历史重放结果不是当时实际发送过的告警。它是用规则在历史快照上重新计算的"
    "模拟结果，未发送任何通知，也未写入实时风险事件时间线。")


def _split(v: str | None) -> list[str] | None:
    """逗号分隔的多值筛选。空值返回 None（不筛）。"""
    if not v:
        return None
    parts = [x.strip() for x in str(v).split(",") if x.strip()]
    return parts or None


class _NullNotifier:
    """未注入 Notifier 时的占位。测试通知会明确失败，而不是假装发出去了。"""

    def send(self, title: str, body: str) -> bool:  # noqa: ARG002
        log.warning("未配置 Notifier，测试通知未发送")
        return False


def _event_payload(row: dict) -> dict:
    """事件行 -> API 输出。补上前端要用的中文标签，避免各处自己拼。"""
    if not row:
        return {}
    return {
        "event_id": row.get("event_id"),
        "identity": row.get("identity"),
        "episode_seq": row.get("episode_seq"),
        "source_type": row.get("source_type"),
        "source_label": rev_.source_label(row.get("source_type")),
        "source_event_id": row.get("source_event_id"),
        "event_code": row.get("event_code"),
        "rule_code": row.get("rule_code"),
        "rule_version": row.get("rule_version"),
        "rule_set_version": row.get("rule_set_version"),
        "category": row.get("category"),
        "severity": row.get("severity"),
        "severity_label": rev_.severity_label(row.get("severity")),
        "max_severity": row.get("max_severity"),
        "status": row.get("status"),
        "status_label": rev_.status_label(row.get("status")),
        "title": row.get("title"),
        "explanation": row.get("explanation"),
        "actual_value": row.get("actual_value"),
        "threshold_value": row.get("threshold_value"),
        "unit": row.get("unit"),
        "affected_symbol": row.get("affected_symbol"),
        "affected_instrument": row.get("affected_instrument"),
        "account_id": row.get("account_id"),
        "snapshot_id": row.get("snapshot_id"),
        "assessment_id": row.get("assessment_id"),
        "data_as_of": row.get("data_as_of"),
        "first_detected_at": row.get("first_detected_at"),
        "last_detected_at": row.get("last_detected_at"),
        "resolved_at": row.get("resolved_at"),
        "occurrence_count": row.get("occurrence_count"),
        "notification_count": row.get("notification_count"),
        "last_notified_at": row.get("last_notified_at"),
        "recommended_check": row.get("recommended_check"),
        "quality_status": row.get("quality_status"),
        "reliable": bool(row.get("reliable")),
        "notification_owner": row.get("notification_owner"),
        "acknowledged_at": row.get("acknowledged_at"),
        "acknowledged_by": row.get("acknowledged_by"),
        "suppressed_until": row.get("suppressed_until"),
        "suppressed_reason": row.get("suppressed_reason"),
        "created_at": row.get("created_at"),
        "updated_at": row.get("updated_at"),
    }


def _notification_payload(n: dict) -> dict:
    """通知记录 -> API 输出。**不含任何凭证** —— payload_summary 在构造时
    就只有风险信息，Webhook 密钥只存在于 Notifier 的 URL 里，从不进入这里。"""
    return {
        "notification_id": n.get("notification_id"),
        "event_id": n.get("event_id"), "event_ids": n.get("event_ids"),
        "channel": n.get("channel"),
        "notification_type": n.get("notification_type"),
        "severity": n.get("severity"), "title": n.get("title"),
        "payload_summary": n.get("payload_summary"),
        "attempted_at": n.get("attempted_at"), "sent_at": n.get("sent_at"),
        "success": bool(n.get("success")), "error_code": n.get("error_code"),
        "error_message": n.get("error_message"),
        "retry_count": n.get("retry_count"),
    }


def _related_assessment(row: dict | None) -> dict | None:
    if not row:
        return None
    return {"assessment_id": row.get("assessment_id"),
            "data_as_of": row.get("data_as_of"),
            "risk_level": row.get("risk_level"),
            "risk_score": row.get("risk_score"),
            "reliable": bool(row.get("reliable"))}


#: 简报生成互斥锁。手动触发可能与调度任务撞车，同一进程内串行化。
_brief_lock = threading.Lock()


def build_router(cfg: Config, db: Database, sync_provider=None,
                 notifier=None) -> APIRouter:
    """构造 API 路由。

    sync_provider: 可调用对象，返回 SyncService；为空表示当前进程不具备同步能力
    （web 容器就是这种情况 —— 同步跑在 sync 容器里），此时手动触发会明确报错，
    而不是假装成功。
    """
    notifier = notifier or _NullNotifier()
    futu_position_client = (futu_quotes.FutuQuoteClient(cfg)
                            if cfg.futu_quote_enabled else None)
    futu_position_lock = threading.Lock()
    router = APIRouter(prefix="/api/v1")

    @router.get("/account/snapshot")
    def latest_snapshot(request: Request, account: str | None = None):
        rid = new_request_id()
        now = datetime.now(timezone.utc)
        row = db.latest_snapshot_row(account)
        if row is None:
            return fail("NO_SNAPSHOT", "尚无任何账户快照", rid, status_code=404)
        row = dict(row)
        sid = row["snapshot_id"]
        fresh = _freshness(cfg, db, row, now)
        issues = db.quality_issues_for(sid)
        # AI 门禁：落库结论 + 此刻新鲜度，任一不通过即禁止
        ai_allowed = bool(row.get("ai_analysis_allowed")) and fresh["status"] != dq.ERROR
        payload = {
            **_snapshot_payload(row),
            "quality_status": fresh["status"],
            "ai_analysis_allowed": ai_allowed,
            "positions": [_position_payload(p) for p in db.positions_for_snapshot(sid)],
            "orders": [dict(o) for o in db.open_orders()] if hasattr(db, "open_orders") else [],
            "quality": {
                **fresh,
                "issues": issues,
                "critical": sum(1 for i in issues if i["severity"] == dq.CRITICAL),
                "warning": sum(1 for i in issues if i["severity"] == dq.WARNING),
            },
            "last_sync": _run_payload(db.latest_sync_run()),
        }
        log.info("api trace=%s path=/account/snapshot snapshot=%s status=%s ai=%s",
                 rid, sid[:8] if sid else "-", fresh["status"], ai_allowed)
        return ok(payload, rid)

    @router.get("/account/live")
    def live_account(request: Request):
        """1 秒覆盖式当前态；不可用时明确降级到最近一份可审计快照。"""
        rid = new_request_id()
        now = datetime.now(timezone.utc)
        payload = db.get_live_state()
        mode = "live"
        if not payload:
            row = db.latest_snapshot_row()
            if row is None:
                return fail("NO_LIVE_STATE", "实时层尚未产生数据", rid, status_code=404)
            row = dict(row)
            pnl = db.latest_pnl(row.get("account")) if hasattr(db, "latest_pnl") else None
            pnl = dict(pnl) if pnl else {}
            payload = {
                "generated_at": row.get("ts"), "account": row.get("account"),
                "balances": {
                    "equity": row.get("equity"), "cash": row.get("cash"),
                    "margin_balance": row.get("margin_balance"),
                    "buying_power": row.get("buying_power"),
                    "total_cash": row.get("total_cash"),
                    "available_funds": row.get("available_funds"),
                    "excess_liquidity": row.get("excess_liquidity"),
                    "positions_value": row.get("positions_value"),
                    "positions_value_source": row.get("positions_value_source"),
                    "currency": row.get("currency"),
                },
                "account_pnl": {
                    "daily_pnl": pnl.get("daily_pnl"),
                    "realized_pnl": pnl.get("realized_pnl"),
                    "unrealized_pnl": pnl.get("unrealized_pnl"),
                },
                "positions": [_position_payload(p)
                              for p in db.positions_for_snapshot(row["snapshot_id"])],
                "timestamps": {key: row.get(key) for key in (
                    "positions_as_of", "account_as_of", "pnl_as_of", "orders_as_of",
                    "executions_as_of", "reconciled_at")},
            }
            mode = "snapshot_fallback"
        with futu_position_lock:
            _overlay_futu_position_quotes(
                payload.get("positions") or [], futu_position_client, now)
        raw_positions = payload.get("positions") or []
        # reqPnLSingle 的 realizedPNL 只覆盖当前交易日；页面当前持仓要显示
        # 当前持仓周期值，因此在 API 出参前按 conid/currency 等身份从 executions
        # 重建周期。单只成交失败不影响其它行，Flex 全历史 SUM 不再作为宽泛兜底。
        try:
            rows = db.executions_between("0000")
            account = payload.get("account") or getattr(cfg, "ib_account", None)
            for position in raw_positions:
                result = aggregate_realized_for_position(rows, position, account=account)
                if result.value is not None:
                    position["realized_pnl"] = result.value
                    position["realized_pnl_source"] = result.source or "executions"
                revalue_position(position, price=position.get("price"),
                                 source=position.get("source"))
        except Exception as exc:  # noqa: BLE001 — 单只历史聚合失败不阻断行情页
            log.warning("当前持仓已实现盈亏聚合失败，保留快照值：%s", type(exc).__name__)
        payload["positions"] = [_position_payload(p) for p in raw_positions]
        account_pnl = dict(payload.get("account_pnl") or {})
        position_upls = [dec(p.get("upl")) for p in raw_positions]
        if raw_positions and all(value is not None for value in position_upls):
            account_pnl["unrealized_pnl"] = sum(
                position_upls, Decimal("0"))
            account_pnl["unrealized_source"] = "当前价 × IBKR 持仓成本"
        elif not raw_positions:
            account_pnl["unrealized_pnl"] = Decimal("0")
        payload["account_pnl"] = account_pnl
        try:
            generated = datetime.fromisoformat(payload.get("generated_at"))
            if generated.tzinfo is None:
                generated = generated.replace(tzinfo=timezone.utc)
            age = max(0.0, (now - generated.astimezone(timezone.utc)).total_seconds())
        except (TypeError, ValueError):
            age = None
        expected = float(getattr(cfg, "live_publish_interval", 1.0))
        payload["delivery"] = {
            "mode": mode, "age_seconds": age,
            "stale": age is None or age > max(expected * 3, 5.0),
            "expected_interval_seconds": expected,
        }
        return ok(payload, rid)

    @router.get("/account/snapshots")
    def history(request: Request, start: str | None = None, end: str | None = None,
                account: str | None = None,
                limit: int = Query(50, ge=1, le=500), offset: int = Query(0, ge=0),
                include_positions: bool = False):
        rid = new_request_id()
        rows = db.snapshots_between(start, end, account=account, limit=limit, offset=offset)
        total = db.count_snapshots_between(start, end, account)
        items = []
        for r in rows:
            item = _snapshot_payload(r)
            if include_positions and r.get("snapshot_id"):
                item["positions"] = [_position_payload(p)
                                     for p in db.positions_for_snapshot(r["snapshot_id"])]
            items.append(item)
        return ok({"items": items, "total": total, "limit": limit, "offset": offset,
                   "start": start, "end": end}, rid)

    @router.get("/sync/status")
    def sync_status(request: Request):
        rid = new_request_id()
        now = datetime.now(timezone.utc)
        latest = db.latest_sync_run()
        last_ok = db.last_successful_sync_run()
        stale_before = (now - timedelta(seconds=cfg.sync_stale_running_seconds)).isoformat()
        running = db.running_sync_run(stale_before=stale_before)
        row = db.latest_snapshot_row()
        fresh = _freshness(cfg, db, dict(row) if row else None, now)
        last_error = None
        if latest is not None and (dict(latest).get("run_state") == sync_mod.RUN_FAILED):
            last_error = {"code": dict(latest).get("error_code"),
                          "message": dict(latest).get("error_message")}
        return ok({
            "latest": _run_payload(latest),
            "last_success_at": (dict(last_ok)["finished_at"] if last_ok else None),
            "last_error": last_error,
            "is_running": running is not None,
            "data_freshness": fresh,
            "data_as_of": (dict(row)["ts"] if row else None),
        }, rid)

    @router.post("/sync/trigger")
    def trigger_sync(request: Request):
        rid = new_request_id()
        now = datetime.now(timezone.utc)
        stale_before = (now - timedelta(seconds=cfg.sync_stale_running_seconds)).isoformat()
        running = db.running_sync_run(stale_before=stale_before)
        state, reason = sync_mod.can_trigger_manual_sync(
            running_run=running, last_manual_at=db.get_sync_state("last_manual_sync_at"),
            now=now, min_interval_s=cfg.manual_sync_min_interval)
        if state != sync_mod.TRIGGER_OK:
            log.warning("手动同步被拒 trace=%s state=%s reason=%s", rid, state, reason)
            return fail(state.upper(), reason, rid, status_code=409,
                        data={"state": state, "is_running": running is not None})
        if sync_provider is None:
            return fail("SYNC_UNAVAILABLE",
                        "当前进程不具备同步能力（同步运行在 sync 服务中）", rid,
                        status_code=503)
        db.set_sync_state("last_manual_sync_at", now.isoformat())
        log.info("手动同步触发 trace=%s", rid)
        try:
            svc = sync_provider()
            done = svc.sync_positions_and_balances(trigger="manual", request_id=rid)
        except Exception as exc:  # noqa: BLE001 — API 不因同步异常而 500
            log.error("手动同步异常 trace=%s err=%s", rid, sync_mod._safe_err(exc))
            return fail("SYNC_FAILED", sync_mod._safe_err(exc), rid, status_code=500)
        return ok({"state": sync_mod.RUN_SUCCESS if done else sync_mod.RUN_FAILED,
                   "completed": done,
                   "latest": _run_payload(db.latest_sync_run())}, rid)

    # ---------------- 持仓决策与操作计划 ----------------

    def _plan_account(account: str | None) -> str | None:
        """选择单一账户上下文；不把多个账户的持仓计划拼成一个列表。"""
        current_row = db.latest_snapshot_row()
        current = (current_row["account"] if current_row is not None
                   else getattr(cfg, "ib_account", None))
        if account and current and account != current:
            # 当前 Web 会话只绑定同步服务的账户；拒绝用 query 参数切换到另一账户。
            return None
        if account:
            return account
        if current:
            return current
        return getattr(cfg, "ib_account", None)

    def _plan_error(exc: Exception, rid: str):
        message = str(exc)
        status = 403 if "其他账户" in message or "无权" in message else 400
        return fail("POSITION_PLAN_ERROR", message, rid, status_code=status)

    def _plan_actor(request: Request) -> str:
        return (request.headers.get("X-Actor") or "dashboard").strip()[:120] or "dashboard"

    @router.get("/position-plans")
    def position_plan_list(request: Request, account: str | None = None,
                           include_closed: bool = False,
                           limit: int = Query(100, ge=1, le=500)):
        rid = new_request_id()
        account = _plan_account(account)
        if not account:
            return ok({"account": None, "cards": [], "cycles": [],
                       "counts": {}}, rid)
        try:
            service = pp_.PositionPlanService(db, account)
            dashboard = service.dashboard()
            dashboard["cycles"] = service.list_cycles(
                include_closed=include_closed, limit=limit)
            return ok(dashboard, rid)
        except pp_.PositionPlanError as exc:
            return _plan_error(exc, rid)

    @router.get("/recommendations")
    def recommendations_list(request: Request, account: str | None = None):
        """每个标的的当前建议（只读）。建议由确定性规则集合成、按状态跃迁落库。"""
        rid = new_request_id()
        from . import recommendation_service as reco_
        account = _plan_account(account)
        if not account:
            return ok({"account": None, "cards": []}, rid)
        return ok(reco_.panel(db, account), rid)

    @router.get("/recommendations/{symbol}/history")
    def recommendation_history(request: Request, symbol: str,
                               account: str | None = None,
                               limit: int = Query(50, ge=1, le=200)):
        """单标的建议历史（含 SUPERSEDED），供追溯。"""
        rid = new_request_id()
        account = _plan_account(account)
        if not account:
            return ok({"account": None, "symbol": symbol, "history": []}, rid)
        rows = db.recommendation_history(account, symbol, limit=limit)
        return ok({"account": account, "symbol": symbol,
                   "history": [dict(r) for r in rows]}, rid)

    @router.get("/position-plans/queue")
    def position_plan_queue(request: Request, account: str | None = None):
        rid = new_request_id()
        account = _plan_account(account)
        if not account:
            return ok({"account": None, "cards": [], "counts": {}}, rid)
        try:
            return ok(pp_.PositionPlanService(db, account).dashboard(), rid)
        except pp_.PositionPlanError as exc:
            return _plan_error(exc, rid)

    @router.get("/position-plans/{cycle_id}")
    def position_plan_detail(request: Request, cycle_id: str,
                             account: str | None = None):
        rid = new_request_id()
        account = _plan_account(account)
        if not account:
            return fail("NO_ACCOUNT", "缺少账户标识", rid, status_code=404)
        try:
            detail = pp_.PositionPlanService(db, account).detail(cycle_id)
            return ok(detail, rid)
        except pp_.PositionPlanError as exc:
            return _plan_error(exc, rid)

    async def _plan_json(request: Request) -> dict:
        try:
            body = await request.json()
        except (ValueError, UnicodeDecodeError):
            return {}
        return body if isinstance(body, dict) else {}

    # ---------- 机会发现与组合构建 ----------

    def _opportunity_error(exc: Exception, rid: str, status_code: int = 400):
        return fail("OPPORTUNITY_ERROR", str(exc), rid, status_code=status_code,
                    data={"opportunity_enabled": cfg.opportunity_enabled,
                          "portfolio_builder_enabled": cfg.portfolio_builder_enabled})

    @router.get("/opportunities/watchlist")
    def opportunity_watchlist(request: Request, include_archived: bool = False):
        rid = new_request_id()
        svc = opp_.OpportunityService(cfg, db)
        return ok({"enabled": cfg.opportunity_enabled,
                   "items": svc.list_watchlist(include_archived=include_archived),
                   "max_active_symbols": cfg.opportunity_max_active_symbols}, rid)

    @router.get("/opportunities/readiness")
    def opportunity_readiness(request: Request):
        rid = new_request_id()
        return ok(opp_.OpportunityService(cfg, db).shadow_readiness(), rid)

    @router.post("/opportunities/watchlist")
    async def opportunity_watchlist_save(request: Request):
        rid = new_request_id()
        payload = await _plan_json(request)
        try:
            item = opp_.OpportunityService(cfg, db).save_watch(
                payload.get("symbol"), sector=payload.get("sector"),
                thesis=payload.get("thesis"), why_watch=payload.get("why_watch"),
                note=payload.get("note"), asset_type=payload.get("asset_type") or "STOCK",
                exchange=payload.get("exchange"), actor="api")
            return ok(item, rid, status_code=201)
        except opp_.OpportunityError as exc:
            return _opportunity_error(exc, rid)

    @router.delete("/opportunities/watchlist/{symbol}")
    def opportunity_watchlist_archive(symbol: str, request: Request):
        rid = new_request_id()
        try:
            return ok(opp_.OpportunityService(cfg, db).set_watch_status(symbol, "ARCHIVED"), rid)
        except opp_.OpportunityError as exc:
            return _opportunity_error(exc, rid, 404)

    @router.post("/opportunities/watchlist/{symbol}/status")
    async def opportunity_watchlist_status(symbol: str, request: Request):
        rid = new_request_id()
        payload = await _plan_json(request)
        try:
            return ok(opp_.OpportunityService(cfg, db).set_watch_status(
                symbol, payload.get("status") or "PAUSED"), rid)
        except opp_.OpportunityError as exc:
            return _opportunity_error(exc, rid)

    @router.post("/opportunities/scans")
    def opportunity_scan(request: Request):
        rid = new_request_id()
        try:
            return ok(opp_.OpportunityService(cfg, db).run_scan(trigger_source="manual"),
                      rid, status_code=201)
        except opp_.OpportunityError as exc:
            code = 503 if not cfg.opportunity_enabled else 409
            return _opportunity_error(exc, rid, code)

    @router.get("/opportunities/scans/latest")
    def opportunity_scan_latest(request: Request):
        rid = new_request_id()
        latest = opp_.OpportunityService(cfg, db).latest_scan()
        if latest is None:
            return fail("NO_OPPORTUNITY_SCAN", "尚无机会扫描结果", rid, 404,
                        data={"enabled": cfg.opportunity_enabled})
        return ok(latest, rid)

    @router.get("/opportunities/scans/{scan_id}")
    def opportunity_scan_detail(scan_id: str, request: Request):
        rid = new_request_id()
        try:
            return ok(opp_.OpportunityService(cfg, db).scan_detail(scan_id), rid)
        except opp_.OpportunityError as exc:
            return _opportunity_error(exc, rid, 404)

    @router.post("/opportunities/portfolio-proposals")
    async def opportunity_portfolio_build(request: Request):
        rid = new_request_id()
        payload = await _plan_json(request)
        try:
            result = opp_.OpportunityService(cfg, db).build_portfolio(
                scan_id=payload.get("scan_id"))
            return ok(result, rid, status_code=201)
        except opp_.OpportunityError as exc:
            code = 503 if not cfg.portfolio_builder_enabled else 409
            return _opportunity_error(exc, rid, code)

    @router.get("/opportunities/portfolio-proposals/latest")
    def opportunity_portfolio_latest(request: Request):
        rid = new_request_id()
        latest = opp_.OpportunityService(cfg, db).latest_proposal()
        if latest is None:
            return fail("NO_PORTFOLIO_PROPOSAL", "尚无组合提案", rid, 404,
                        data={"enabled": cfg.portfolio_builder_enabled})
        return ok(latest, rid)

    @router.get("/opportunities/outcomes")
    def opportunity_outcomes(request: Request):
        rid = new_request_id()
        return ok(opp_.OpportunityService(cfg, db).outcome_summary(), rid)

    @router.post("/opportunities/portfolio-items/{item_id}/adopt")
    def opportunity_item_adopt(item_id: str, request: Request):
        rid = new_request_id()
        try:
            return ok(opp_.OpportunityService(cfg, db).adopt_item(item_id, actor="api"), rid)
        except (opp_.OpportunityError, pp_.PositionPlanError) as exc:
            return _opportunity_error(exc, rid, 409)

    @router.post("/position-plans/{cycle_id}/versions")
    async def position_plan_version(request: Request, cycle_id: str,
                                    account: str | None = None):
        rid = new_request_id()
        account = _plan_account(account)
        if not account:
            return fail("NO_ACCOUNT", "缺少账户标识", rid, status_code=400)
        payload = await _plan_json(request)
        key = request.headers.get("Idempotency-Key") or payload.get("idempotency_key")
        try:
            result = pp_.PositionPlanService(db, account).create_version(
                cycle_id, payload, actor=_plan_actor(request), idempotency_key=key)
            return ok(result, rid, status_code=201)
        except pp_.PositionPlanError as exc:
            return _plan_error(exc, rid)

    @router.post("/position-plans/{cycle_id}/drafts")
    async def position_plan_draft(request: Request, cycle_id: str,
                                  account: str | None = None):
        rid = new_request_id()
        account = _plan_account(account)
        if not account:
            return fail("NO_ACCOUNT", "缺少账户标识", rid, status_code=400)
        payload = await _plan_json(request)
        key = request.headers.get("Idempotency-Key") or payload.get("idempotency_key")
        try:
            result = pp_.PositionPlanService(db, account).save_draft(
                cycle_id, payload, actor=_plan_actor(request), idempotency_key=key)
            return ok(result, rid, status_code=200)
        except pp_.PositionPlanError as exc:
            return _plan_error(exc, rid)

    @router.post("/position-plans/{cycle_id}/actions")
    async def position_plan_action(request: Request, cycle_id: str,
                                   account: str | None = None):
        rid = new_request_id()
        account = _plan_account(account)
        if not account:
            return fail("NO_ACCOUNT", "缺少账户标识", rid, status_code=400)
        try:
            payload = await _plan_json(request)
            result = pp_.PositionPlanService(db, account).create_action(
                cycle_id, payload, actor=_plan_actor(request),
                idempotency_key=(request.headers.get("Idempotency-Key")
                                 or payload.get("idempotency_key")))
            return ok(result, rid, status_code=201)
        except pp_.PositionPlanError as exc:
            return _plan_error(exc, rid)

    @router.post("/position-plans/{cycle_id}/actions/{action_id}")
    async def position_plan_action_status(request: Request, cycle_id: str,
                                          action_id: str,
                                          account: str | None = None):
        rid = new_request_id()
        account = _plan_account(account)
        if not account:
            return fail("NO_ACCOUNT", "缺少账户标识", rid, status_code=400)
        payload = await _plan_json(request)
        try:
            result = pp_.PositionPlanService(db, account).update_action(
                cycle_id, action_id, status=payload.get("status"), actor=_plan_actor(request))
            return ok(result, rid)
        except pp_.PositionPlanError as exc:
            return _plan_error(exc, rid)

    @router.post("/position-plans/{cycle_id}/status")
    async def position_plan_cycle_status(request: Request, cycle_id: str,
                                         account: str | None = None):
        rid = new_request_id()
        account = _plan_account(account)
        if not account:
            return fail("NO_ACCOUNT", "缺少账户标识", rid, status_code=400)
        payload = await _plan_json(request)
        try:
            result = pp_.PositionPlanService(db, account).update_cycle_status(
                cycle_id, status=payload.get("status"), actor=_plan_actor(request))
            return ok(result, rid)
        except pp_.PositionPlanError as exc:
            return _plan_error(exc, rid)

    @router.post("/position-plans/{cycle_id}/executions/{execution_id}/confirm")
    async def position_plan_execution_confirm(request: Request, cycle_id: str,
                                              execution_id: str,
                                              account: str | None = None):
        rid = new_request_id()
        account = _plan_account(account)
        if not account:
            return fail("NO_ACCOUNT", "缺少账户标识", rid, status_code=400)
        payload = await _plan_json(request)
        try:
            result = pp_.PositionPlanService(db, account).confirm_execution(
                cycle_id, execution_id, match_status=payload.get("match_status"),
                action_id=payload.get("action_id"),
                deviation_reason=payload.get("deviation_reason"),
                actor=_plan_actor(request))
            return ok(result, rid)
        except pp_.PositionPlanError as exc:
            return _plan_error(exc, rid)

    @router.post("/position-plans/{cycle_id}/journal/retry")
    def position_plan_journal_retry(request: Request, cycle_id: str,
                                    account: str | None = None):
        rid = new_request_id()
        account = _plan_account(account)
        if not account:
            return fail("NO_ACCOUNT", "缺少账户标识", rid, status_code=400)
        try:
            result = pp_.PositionPlanService(db, account).retry_archive(cycle_id)
            return ok(result, rid)
        except pp_.PositionPlanError as exc:
            return _plan_error(exc, rid)


    # ---------------- 风险评估 ----------------

    # ==================== 事件标注与研究排除 ====================

    @router.get("/events/{event_id}/annotations")
    def event_annotations_api(request: Request, event_id: str):
        rid = new_request_id()
        return ok({"event_id": event_id,
                   "annotations": db.event_annotations(event_id),
                   "notice": _ea.PROBE_NOTICE}, rid)

    @router.get("/research/exclusions")
    def research_exclusions(request: Request,
                            include_excluded: bool = Query(False),
                            metric: str = Query("research"),
                            limit: int = Query(200, ge=1, le=1000)):
        """研究样本与排除统计。默认排除被标注的样本。"""
        rid = new_request_id()
        rows, _ = db.query_risk_events(limit=limit)
        res = _ea.filter_for_research(db, [dict(r) for r in rows],
                                      include_excluded=include_excluded,
                                      metric=metric)
        merged = _ea.merge_artificial_splits(db, [dict(r) for r in rows])
        return ok({**res["stats"],
                   "research_view_merged_count": merged["merged_count"],
                   "research_view_merged": merged["merged_detail"],
                   "merge_note": merged["note"],
                   "events": res["events"] if include_excluded else None}, rid)

    @router.get("/ops/side-effect-executions")
    def side_effect_executions(request: Request,
                               limit: int = Query(20, ge=1, le=100)):
        rid = new_request_id()
        return ok({"executions": db.side_effect_executions(limit=limit)}, rid)

    # ==================== AI 每日风险简报 ====================

    def _brief_row(row: dict, *, full: bool = False) -> dict:
        import json as _j

        def j(v, d):
            try:
                return _j.loads(v) if v else d
            except (TypeError, ValueError):
                return d

        out = {
            "brief_id": row["brief_id"], "brief_date": row["brief_date"],
            "account_id_masked": row.get("account_id_masked"),
            "revision": row.get("revision"),
            "generation_mode": row["generation_mode"],
            "generation_status": row["generation_status"],
            "validation_status": row["validation_status"],
            "validation_errors": j(row.get("validation_errors"), []),
            "prompt_version": row.get("prompt_version"),
            "model_identifier": row.get("model_identifier"),
            "context_hash": row.get("context_hash"),
            "context_version": row.get("context_version"),
            "ai_attempts": row.get("ai_attempts"),
            "calculation_versions": j(row.get("calculation_versions"), {}),
            "account_snapshot_id": row.get("account_snapshot_id"),
            "risk_assessment_id": row.get("risk_assessment_id"),
            "market_assessment_id": row.get("market_assessment_id"),
            "performance_period_id": row.get("performance_period_id"),
            "generated_at": row.get("generated_at"),
            "delivered_at": row.get("delivered_at"),
            "disclaimer": _bp.DISCLAIMER,
        }
        if full:
            out["content"] = j(row.get("content_json"), {})
            out["rendered_markdown"] = row.get("rendered_markdown")
            out["source_references"] = db.brief_sources(row["brief_id"])
            out["deliveries"] = db.brief_deliveries(row["brief_id"])
        return out

    @router.get("/briefs/latest")
    def briefs_latest(request: Request, date: str | None = Query(None)):
        rid = new_request_id()
        row = db.latest_brief(brief_date=date)
        if row is None:
            return fail("not_found", "还没有生成过任何简报", rid, 404)
        return ok(_brief_row(row, full=True), rid)

    @router.get("/briefs")
    def briefs_list(request: Request, start: str | None = Query(None),
                    end: str | None = Query(None),
                    generation_mode: str | None = Query(None),
                    generation_status: str | None = Query(None),
                    delivered: bool | None = Query(None),
                    limit: int = Query(30, ge=1, le=200),
                    offset: int = Query(0, ge=0)):
        rid = new_request_id()
        rows, total = db.query_briefs(
            start=start, end=end, generation_mode=_split(generation_mode),
            generation_status=_split(generation_status), delivered=delivered,
            limit=limit, offset=offset)
        return ok({"briefs": [_brief_row(r) for r in rows],
                   "total": total, "limit": limit, "offset": offset,
                   "runs": db.brief_runs(limit=10),
                   "disclaimer": _bp.DISCLAIMER}, rid)

    @router.get("/briefs/{brief_id}")
    def brief_detail(request: Request, brief_id: str):
        rid = new_request_id()
        row = db.get_brief(brief_id)
        if row is None:
            return fail("not_found", "简报不存在", rid, 404)
        return ok(_brief_row(row, full=True), rid)

    @router.post("/briefs/generate")
    def briefs_generate(request: Request, date: str | None = Query(None),
                        regenerate: bool = Query(False),
                        deliver: bool = Query(False)):
        """手动生成。**默认不推送飞书** —— 手动重跑不该重复打扰。"""
        rid = new_request_id()
        now = datetime.now(timezone.utc)
        allowed, why = _bsvc.can_trigger_manually(db, now=now)
        if not allowed:
            return fail("rate_limited", why, rid, 429)
        with _brief_lock:
            res = _bsvc.generate_brief(
                cfg, db, brief_date=date,
                trigger=(_bsvc.TRIGGER_REGENERATE if regenerate
                         else _bsvc.TRIGGER_MANUAL),
                notifier=notifier, deliver=deliver, regenerate=regenerate,
                now=now)
        payload = dict(res)
        if res.get("brief_id"):
            row = db.get_brief(res["brief_id"])
            if row:
                payload["brief"] = _brief_row(row, full=True)
        return ok(payload, rid)

    @router.post("/briefs/{brief_id}/deliver-test")
    def brief_deliver_test(request: Request, brief_id: str):
        """链路测试发送。走独立渠道，**不占用正式推送去重键**。"""
        rid = new_request_id()
        if notifier is None:
            return fail("unavailable", "本进程没有推送通道", rid, 503)
        res = _bsvc.deliver_test(cfg, db, brief_id, notifier=notifier)
        return ok(res, rid)

    @router.get("/briefs/schedule/executions")
    def brief_schedule_executions(request: Request,
                                  limit: int = Query(20, ge=1, le=100)):
        rid = new_request_id()
        return ok({"executions": db.schedule_executions(limit=limit)}, rid)

    @router.post("/briefs/{brief_id}/deliver")
    def brief_deliver(request: Request, brief_id: str,
                      force: bool = Query(False)):
        rid = new_request_id()
        if notifier is None:
            return fail("unavailable", "本进程没有推送通道", rid, 503)
        res = _bsvc.deliver_brief(cfg, db, brief_id, notifier=notifier,
                                  force=force)
        return ok(res, rid)

    @router.get("/risk/assessment")
    def risk_assessment(request: Request, snapshot_id: str | None = None,
                        account: str | None = None, refresh: bool = False):
        rid = new_request_id()
        now = datetime.now(timezone.utc)
        try:
            a = risk_service.assess(cfg, db, snapshot_id=snapshot_id, now=now, force=refresh)
        except Exception as exc:  # noqa: BLE001 — 评估异常不得让 API 500
            log.error("风险评估异常 trace=%s err=%s", rid, sync_mod._safe_err(exc))
            return fail("ASSESSMENT_FAILED", sync_mod._safe_err(exc), rid, status_code=500)
        if a is None:
            return fail("NO_SNAPSHOT", "尚无账户快照，无法评估风险", rid, status_code=404)
        m = a.get("calculated_metrics", {})
        payload = {
            "assessment_id": a["assessment_id"],
            "snapshot_id": a["snapshot_id"],
            "data_as_of": a["data_as_of"],
            "risk_level": a["risk_level"],
            "total_risk_score": a["risk_score"],
            "reliable": a["reliable"],
            "blocked_reason": a.get("blocked_reason"),
            "component_scores": a["component_scores"],
            "account_metrics": m.get("account", {}),
            "concentration_metrics": m.get("concentration", {}),
            "margin_metrics": m.get("liquidity", {}),
            "drawdown_metrics": m.get("drawdown", {}),
            "pnl_metrics": m.get("pnl", {}),
            "triggered_rules": a["triggered_rules"],
            # 规则适用性：哪些集中度指标在当前持仓数下没有判别力。
            # **不适用不等于通过** —— 消费方必须能区分这两件事。
            "concentration_applicability": a.get("concentration_applicability", {}),
            "rule_outcomes": a.get("rule_outcomes", []),
            "score_normalization_method": a.get("score_normalization_method"),
            "normalization_detail": a.get("normalization_detail", {}),
            "top_risk_drivers": a["top_risk_drivers"],
            "events": a.get("events", []),
            "quality_status": a["data_quality_status"],
            "ai_analysis_allowed": a["ai_analysis_allowed"],
            "calculation_version": a["calculation_version"],
            "rule_set_version": a.get("rule_set_version"),
            "rule_set_hash": a.get("rule_set_hash"),
        }
        log.info("api trace=%s path=/risk/assessment level=%s score=%s reliable=%s",
                 rid, a["risk_level"], a["risk_score"], a["reliable"])
        return ok(payload, rid)

    @router.get("/risk/positions")
    def risk_positions(request: Request, snapshot_id: str | None = None,
                       symbol: str | None = None, flagged: bool | None = None,
                       min_weight: float | None = None,
                       sort_by: str = "weight_of_equity", desc: bool = True):
        """逐持仓风险。sort_by 支持两种仓位口径，正是双口径设计的用途。"""
        rid = new_request_id()
        try:
            a = risk_service.assess(cfg, db, snapshot_id=snapshot_id)
        except Exception as exc:  # noqa: BLE001
            return fail("ASSESSMENT_FAILED", sync_mod._safe_err(exc), rid, status_code=500)
        if a is None:
            return fail("NO_SNAPSHOT", "尚无账户快照", rid, status_code=404)
        rows = list(a.get("calculated_metrics", {}).get("positions", []))
        ev_by_sym: dict[str, list] = {}
        for e in a.get("events", []):
            if e.get("affected_symbol"):
                ev_by_sym.setdefault(e["affected_symbol"], []).append(e)
        for r in rows:
            evs = ev_by_sym.get(r.get("symbol"), [])
            r["triggered_rules"] = [e["rule_code"] for e in evs]
            r["risk_level"] = ("critical" if any(e["severity"] == "critical" for e in evs)
                               else "high" if any(e["severity"] == "high" for e in evs)
                               else "warning" if evs else "low")
        if symbol:
            rows = [r for r in rows if r.get("symbol") == symbol]
        if flagged is not None:
            rows = [r for r in rows if bool(r.get("risk_flags")) == flagged]
        if min_weight is not None:
            rows = [r for r in rows
                    if r.get("weight_of_equity") is not None
                    and Decimal(str(r["weight_of_equity"])) >= Decimal(str(min_weight))]

        def key(r):
            v = r.get(sort_by)
            return Decimal(str(v)) if v is not None else Decimal("-1")
        try:
            rows.sort(key=key, reverse=desc)
        except Exception:  # noqa: BLE001 — 非法排序字段不该让请求失败
            pass
        return ok({"snapshot_id": a["snapshot_id"], "data_as_of": a["data_as_of"],
                   "reliable": a["reliable"], "positions": rows,
                   "sort_by": sort_by, "count": len(rows)}, rid)

    @router.get("/risk/history")
    def risk_history(request: Request, start: str | None = None, end: str | None = None,
                     account: str | None = None, risk_level: str | None = None,
                     limit: int = Query(50, ge=1, le=500), offset: int = Query(0, ge=0)):
        rid = new_request_id()
        rows = db.assessments_between(start, end, account=account, risk_level=risk_level,
                                      limit=limit, offset=offset)
        total = db.count_assessments(start, end, account, risk_level)
        items = [{
            "assessment_id": r["assessment_id"], "snapshot_id": r["snapshot_id"],
            "data_as_of": r["data_as_of"], "risk_level": r["risk_level"],
            "total_risk_score": r.get("risk_score"), "reliable": bool(r.get("reliable")),
            "data_quality_status": r.get("data_quality_status"),
            "calculation_version": r.get("calculation_version"),
        } for r in rows]
        return ok({"items": items, "total": total, "limit": limit, "offset": offset}, rid)

    @router.get("/risk/rules")
    def risk_rules(request: Request, version: str | None = None):
        """规则清单（只读）。不传 version 返回当前生效规则；传了则返回该历史版本。

        历史评估必须能查到当时的阈值 —— 拿今天的环境变量去重新解释昨天的结论，
        是审计上最不能接受的做法。
        """
        rid = new_request_id()
        cur = re_.default_rules(cfg)
        cur_version = re_.rule_set_version(cur)
        if version:
            rows = db.list_risk_rules(version)
            return ok({"version": version, "count": len(rows), "rules": rows,
                       "is_current": version == cur_version}, rid)
        return ok({"version": cur_version, "hash": re_.rule_set_hash(cur),
                   "count": len(cur), "rules": [r.as_dict() for r in cur],
                   "history": db.risk_rule_versions()}, rid)


    # ---------------- 市场环境 ----------------

    @router.get("/market/environment")
    def market_environment(request: Request, refresh: bool = False):
        rid = new_request_id()
        try:
            a = market_service.assess(cfg, db, force=refresh)
        except Exception as exc:  # noqa: BLE001
            return fail("MARKET_ASSESS_FAILED", sync_mod._safe_err(exc), rid, 500)
        m = a.get("calculated_metrics", {})
        return ok({
            "data_as_of": a["data_as_of"], "market_state": a["market_state"],
            "market_risk_level": a["market_risk_level"],
            "market_risk_score": a["market_risk_score"],
            "reliable": a["reliable"], "blocked_reason": a.get("blocked_reason"),
            "component_scores": a["component_scores"],
            "calculated_metrics": m,
            "triggered_rules": a["triggered_rules"],
            "top_market_drivers": a["top_market_drivers"],
            "events": a.get("events", []),
            "quality_status": a["quality_status"],
            "calculation_version": a["calculation_version"],
            "rule_set_version": a.get("rule_set_version"),
        }, rid)

    @router.get("/market/environment/history")
    def market_history(request: Request, start: str | None = None, end: str | None = None,
                       state: str | None = None,
                       limit: int = Query(50, ge=1, le=500), offset: int = Query(0, ge=0)):
        rid = new_request_id()
        rows = db.market_assessments_between(start, end, state=state, limit=limit,
                                             offset=offset)
        return ok({"items": [{"assessment_id": r["assessment_id"],
                              "data_as_of": r["data_as_of"],
                              "market_state": r["market_state"],
                              "market_risk_level": r["market_risk_level"],
                              "market_risk_score": r.get("market_risk_score"),
                              "reliable": bool(r.get("reliable")),
                              "calculation_version": r.get("calculation_version")}
                             for r in rows],
                   "total": db.count_market_assessments(start, end, state),
                   "limit": limit, "offset": offset}, rid)

    @router.get("/market/instruments")
    def market_instruments(request: Request):
        """各标的的数据状态与关键指标 —— 每个都带质量状态，缺失如实呈现。"""
        rid = new_request_id()
        latest = db.latest_market_snapshots()
        specs = {s.code: s for s in md_.DEFAULT_INSTRUMENTS}
        items = []
        for code, spec in specs.items():
            row = latest.get(code)
            items.append({
                "instrument_code": code, "instrument_name": spec.name,
                "kind": spec.kind, "required": spec.required,
                "available": row is not None,
                "data_as_of": (row or {}).get("data_as_of"),
                "fetched_at": (row or {}).get("fetched_at"),
                "close": (row or {}).get("close"),
                "previous_close": (row or {}).get("previous_close"),
                "daily_change_percent": (row or {}).get("daily_change_percent"),
                "delayed": bool((row or {}).get("delayed", True)),
                "quality_status": (row or {}).get("quality_status", "missing"),
                "quality_note": (row or {}).get("quality_note")
                                or (None if row else "尚未取到该标的数据"),
                "source": (row or {}).get("source"),
            })
        run = db.latest_market_sync_run()
        return ok({"instruments": items,
                   "last_sync": (dict(run) if run else None)}, rid)

    @router.get("/risk/combined")
    def combined_risk(request: Request):
        """账户 × 市场 联合视图。**不修改账户原始评分**，只做解释。"""
        rid = new_request_id()
        try:
            acc = risk_service.assess(cfg, db)
        except Exception:  # noqa: BLE001
            acc = None
        try:
            mkt = market_service.assess(cfg, db)
        except Exception:  # noqa: BLE001
            mkt = None
        view = me_.combined_view(acc, mkt, cfg=cfg)
        lag = None
        if acc and mkt and acc.get("data_as_of") and mkt.get("data_as_of"):
            try:
                a_t = datetime.fromisoformat(acc["data_as_of"])
                m_t = datetime.fromisoformat(mkt["data_as_of"] + "T00:00:00+00:00")
                lag = int(abs((a_t - m_t).total_seconds()))
            except (TypeError, ValueError):
                lag = None
        return ok({
            "account": None if not acc else {
                "snapshot_id": acc["snapshot_id"], "data_as_of": acc["data_as_of"],
                "risk_level": acc["risk_level"], "risk_score": acc.get("risk_score"),
                "reliable": acc["reliable"],
                "top_risk_drivers": acc.get("top_risk_drivers", []),
            },
            "market": None if not mkt else {
                "data_as_of": mkt["data_as_of"], "market_state": mkt["market_state"],
                "market_risk_level": mkt["market_risk_level"],
                "market_risk_score": mkt.get("market_risk_score"),
                "reliable": mkt["reliable"],
                "top_market_drivers": mkt.get("top_market_drivers", []),
            },
            "combined": view,
            "data_time_lag_seconds": lag,
        }, rid)

    # ================= 风险事件运营层 =================

    @router.get("/risk/events")
    def risk_events(request: Request,
                    start: str | None = Query(None), end: str | None = Query(None),
                    source_type: str | None = Query(None),
                    category: str | None = Query(None),
                    severity: str | None = Query(None),
                    status: str | None = Query(None),
                    symbol: str | None = Query(None),
                    rule_code: str | None = Query(None),
                    reliable: bool | None = Query(None),
                    limit: int = Query(50, ge=1, le=500),
                    offset: int = Query(0, ge=0)):
        """统一风险事件时间线。账户、市场、联合、数据质量、行为纪律同一条线。"""
        rid = new_request_id()
        db.expire_suppressions(_now_iso())
        rows, total = db.query_risk_events(
            start=start, end=end,
            source_type=_split(source_type), category=_split(category),
            severity=_split(severity), status=_split(status), symbol=symbol,
            rule_code=_split(rule_code), reliable=reliable,
            limit=limit, offset=offset)
        return ok({
            "events": [_event_payload(r) for r in rows],
            "total": total, "limit": limit, "offset": offset,
            "counts": db.event_counts_by_severity(),
        }, rid)

    @router.get("/risk/events/{event_id}")
    def risk_event_detail(event_id: str, request: Request):
        """单个事件的完整视图：状态历史、通知历史、相关评估。"""
        rid = new_request_id()
        row = db.get_risk_event(event_id)
        if not row:
            return fail("NOT_FOUND", f"事件不存在：{event_id}", rid, 404)
        try:
            # 取**触发时那一版**的规则定义，不是当前版本
            rule = db.rule_definition(row.get("rule_code"),
                                      row.get("rule_set_version"))
        except Exception:  # noqa: BLE001
            rule = None
        related_acc = related_mkt = None
        if row.get("assessment_id"):
            try:
                related_acc = db.assessment_by_id(row["assessment_id"])
                if related_acc is None:
                    related_mkt = db.market_assessment_by_id(row["assessment_id"])
            except Exception:  # noqa: BLE001
                related_acc = None
        positions = []
        if row.get("snapshot_id") and row.get("affected_symbol"):
            try:
                positions = [p for p in db.positions_for_snapshot(row["snapshot_id"])
                             if p.get("symbol") == row["affected_symbol"]]
            except Exception:  # noqa: BLE001
                positions = []
        return ok({
            "event": _event_payload(row),
            "transitions": db.event_transitions(event_id),
            "notifications": [_notification_payload(n)
                              for n in db.notifications_for_event(event_id)],
            "rule_definition": rule,
            "related_account_assessment": _related_assessment(related_acc),
            "related_market_assessment": None if not related_mkt else {
                "assessment_id": related_mkt.get("assessment_id"),
                "data_as_of": related_mkt.get("data_as_of"),
                "market_state": related_mkt.get("market_state"),
                "market_risk_score": related_mkt.get("market_risk_score"),
                "reliable": bool(related_mkt.get("reliable"))},
            "related_positions": positions,
        }, rid)

    @router.post("/risk/events/{event_id}/acknowledge")
    def acknowledge_event(event_id: str, request: Request,
                          actor: str | None = Query(None)):
        """标记已知晓。**不改变风险是否仍然存在** —— acknowledged ≠ resolved。"""
        rid = new_request_id()
        row = db.get_risk_event(event_id)
        if not row:
            return fail("NOT_FOUND", f"事件不存在：{event_id}", rid, 404)
        if row.get("status") == rev_.ST_RESOLVED:
            return fail("ALREADY_RESOLVED", "该事件已恢复，无需确认", rid, 409)
        now = _now_iso()
        db.acknowledge_event(event_id, now_iso=now, actor=actor or "web")
        db.record_event_transition({
            "event_id": event_id, "transition": rev_.TRANS_ACKNOWLEDGED,
            "previous_status": row.get("status"), "new_status": rev_.ST_ACK,
            "previous_severity": row.get("severity"),
            "new_severity": row.get("severity"),
            "reason": "用户确认已知晓（风险仍然存在）",
            "actor": actor or "web", "transitioned_at": now})
        return ok({"event": _event_payload(db.get_risk_event(event_id)),
                   "note": "已记录确认。风险条件仍然成立，不等于已恢复。"}, rid)

    @router.post("/risk/events/{event_id}/suppress")
    def suppress_event(event_id: str, request: Request,
                       until: str | None = Query(None),
                       hours: int = Query(24, ge=1, le=720),
                       reason: str | None = Query(None),
                       actor: str | None = Query(None)):
        """暂时不通知。事件不删除，页面仍显示，到期自动恢复正常告警。"""
        rid = new_request_id()
        row = db.get_risk_event(event_id)
        if not row:
            return fail("NOT_FOUND", f"事件不存在：{event_id}", rid, 404)
        if row.get("status") == rev_.ST_RESOLVED:
            return fail("ALREADY_RESOLVED", "该事件已恢复，无需抑制", rid, 409)
        now = datetime.now(timezone.utc)
        until_iso = until or (now + timedelta(hours=hours)).isoformat()
        db.suppress_event(event_id, until_iso=until_iso, reason=reason,
                          now_iso=now.isoformat())
        db.record_event_transition({
            "event_id": event_id, "transition": rev_.TRANS_SUPPRESSED,
            "previous_status": row.get("status"), "new_status": rev_.ST_SUPPRESSED,
            "previous_severity": row.get("severity"),
            "new_severity": row.get("severity"),
            "reason": reason or f"人工抑制至 {until_iso}",
            "actor": actor or "web", "transitioned_at": now.isoformat()})
        return ok({"event": _event_payload(db.get_risk_event(event_id)),
                   "suppressed_until": until_iso,
                   "note": "抑制期内不推送通知，事件仍在页面显示并继续累计。"}, rid)

    @router.get("/risk/trends")
    def risk_trends(request: Request,
                    range: str | None = Query(None, alias="range"),
                    start: str | None = Query(None), end: str | None = Query(None),
                    interval: str | None = Query(None),
                    account: str | None = Query(None)):
        """风险趋势。降采样保留区间最高风险，缺失区间显式标出。"""
        rid = new_request_id()
        s, e, iv, key = rt_.resolve_range(range, start, end, interval)
        acc_rows = db.risk_score_series(s.isoformat(), e.isoformat(), account=account)
        mkt_rows = db.market_score_series(s.date().isoformat(), e.date().isoformat())
        acc_buckets = rt_.aggregate_account(acc_rows, iv)
        mkt_buckets = rt_.aggregate_market(mkt_rows, iv)
        gaps = rt_.find_gaps(acc_buckets, s, e, iv)
        counts = db.event_counts_by_severity()
        return ok({
            "range": key, "interval": iv,
            "start": s.isoformat(), "end": e.isoformat(),
            "timezone": "UTC",
            "aggregation": {
                "account_granularity": iv,
                # 市场评估按交易日幂等，粒度天然是「日」，不会因 interval 变密
                "market_granularity": "1d" if iv in ("raw", "1m", "5m", "15m", "1h", "6h") else iv,
                "note": "每桶给出 last/avg/max，highest_risk_level 为桶内最高等级"
                        "（不是对均值反查），避免平均把峰值抹平。",
            },
            "account_data_as_of": acc_rows[-1]["data_as_of"] if acc_rows else None,
            "market_data_as_of": mkt_rows[-1]["data_as_of"] if mkt_rows else None,
            "account": acc_buckets,
            "market": mkt_buckets,
            "amplification": rt_.amplification_series(acc_buckets, mkt_buckets),
            "open_high_count": counts.get("high", 0),
            "open_critical_count": counts.get("critical", 0),
            "open_warning_count": counts.get("warning", 0),
            "gaps": gaps,
            "has_gaps": bool(gaps),
        }, rid)

    @router.get("/risk/notifications")
    def risk_notifications(request: Request,
                           hours: int = Query(72, ge=1, le=8760),
                           success_only: bool = Query(False)):
        """通知历史。失败记录同样保留 —— 发送失败绝不等于已通知。"""
        rid = new_request_id()
        now = datetime.now(timezone.utc)
        rows = db.notifications_between(
            (now - timedelta(hours=hours)).isoformat(), now.isoformat(),
            success_only=success_only)
        return ok({"notifications": [_notification_payload(n) for n in rows],
                   "hours": hours, "total": len(rows)}, rid)

    @router.post("/risk/notifications/test")
    def send_test_notification(request: Request):
        """发送一条**明确标记为测试**的通知，验证推送链路。"""
        rid = new_request_id()
        res = event_service.send_test_notification(cfg, db, notifier)
        if not res.get("success"):
            return fail("SEND_FAILED", "测试通知发送失败，请检查 APPRISE_URLS 配置",
                        rid, 502, data=res)
        return ok(res, rid)

    # ================= 复盘与阈值回测 =================

    @router.get("/risk/reviews")
    def risk_reviews(request: Request,
                     start: str | None = Query(None), end: str | None = Query(None),
                     rule_code: str | None = Query(None),
                     source_type: str | None = Query(None),
                     symbol: str | None = Query(None),
                     review_status: str | None = Query(None),
                     outcome_label: str | None = Query(None),
                     limit: int = Query(50, ge=1, le=500),
                     offset: int = Query(0, ge=0)):
        """事件复盘列表。每条都带 review_status 与样本资格说明。"""
        rid = new_request_id()
        rows, total = db.query_reviews(
            version=review_service.CALC_VERSION, start=start, end=end,
            rule_code=_split(rule_code), source_type=_split(source_type),
            symbol=symbol, review_status=_split(review_status),
            limit=limit, offset=offset)
        payload = [_review_payload(r) for r in rows]
        if outcome_label:
            payload = [p for p in payload
                       if any(w.get(outcome_label) is not None
                              for w in (p.get("outcome_labels") or {}).values())]
        counts: dict[str, int] = {}
        for r in rows:
            k = r.get("review_status") or "unknown"
            counts[k] = counts.get(k, 0) + 1
        return ok({"reviews": payload, "total": total, "limit": limit,
                   "offset": offset, "status_counts": counts,
                   "calculation_version": review_service.CALC_VERSION}, rid)

    @router.get("/risk/reviews/{event_id}")
    def risk_review_detail(event_id: str, request: Request):
        """单个事件的完整复盘：事前上下文、事中变化、事后前瞻结果。"""
        rid = new_request_id()
        rev = db.review_for_event(event_id, review_service.CALC_VERSION)
        if not rev:
            return fail("NOT_FOUND", f"该事件尚无复盘记录：{event_id}", rid, 404)
        event = db.get_risk_event(event_id)
        return ok({
            "review": _review_payload(rev),
            "event": _event_payload(event) if event else None,
            "transitions": db.event_transitions(event_id),
            "notifications": [_notification_payload(n)
                              for n in db.notifications_for_event(event_id)],
            "labels": [l.as_dict() for l in ol_.default_labels(cfg)],
        }, rid)

    @router.get("/risk/performance")
    def risk_performance(request: Request,
                         rule_code: str | None = Query(None),
                         source_type: str | None = Query(None),
                         observation_window: int | None = Query(None),
                         outcome_label: str | None = Query(None),
                         limit: int = Query(200, ge=1, le=1000)):
        """规则效果排行榜。样本不足的规则同样返回，但指标为 null。"""
        rid = new_request_id()
        rows = db.query_performance(
            rule_code=rule_code, source_type=source_type,
            observation_window=observation_window, outcome_label=outcome_label,
            version=rperf.CALC_VERSION, limit=limit)
        return ok({
            "performance": [_perf_payload(r) for r in rows],
            "total": len(rows),
            "calculation_version": rperf.CALC_VERSION,
            "note": ("precision 高不代表规则一定好，recall 高但告警过密也可能不可用；"
                     "样本量低于 %d 时不输出指标。" % rperf.MIN_SAMPLES_FOR_METRICS),
            "confidence_thresholds": {str(n): lvl
                                      for n, lvl in rperf.CONF_THRESHOLDS},
        }, rid)

    @router.post("/risk/backtests")
    def start_backtest(request: Request,
                       rules: str | None = Query(None),
                       date_start: str | None = Query(None),
                       date_end: str | None = Query(None),
                       observation_windows: str | None = Query(None),
                       outcome_labels: str | None = Query(None),
                       reuse: bool = Query(True)):
        """启动阈值回测。第一版同步执行，数据模型已支持未来改为独立任务。

        **回测失败绝不影响线上评估** —— 整个执行链路异常全吞。
        """
        rid = new_request_id()
        windows = [int(x) for x in (_split(observation_windows) or [])] or None
        res = bt_.run_backtest(
            cfg, db, rules=_split(rules), date_start=date_start,
            date_end=date_end,
            windows=windows or tc_.FORWARD_WINDOWS,
            labels=_split(outcome_labels), reuse=reuse)
        if res.get("status") == "failed":
            return fail("BACKTEST_FAILED", res.get("error") or "回测失败",
                        rid, 500, data=res)
        return ok(res, rid)

    @router.get("/risk/backtests/{backtest_run_id}")
    def backtest_detail(backtest_run_id: str, request: Request,
                        rule_code: str | None = Query(None),
                        segment: str | None = Query(None)):
        """回测结果。候选阈值的收益与代价一并返回。"""
        rid = new_request_id()
        run = db.get_backtest_run(backtest_run_id)
        if not run:
            return fail("NOT_FOUND", f"回测任务不存在：{backtest_run_id}", rid, 404)
        results = db.backtest_results(backtest_run_id, rule_code=rule_code,
                                      segment=segment)
        return ok({
            "run": run,
            "results": results,
            "combos": db.combo_results(backtest_run_id),
            "note": ("候选阈值的 recall 提升必须与通知量变化一起看；"
                     "delta_vs_current 里同时给出了两者。"),
        }, rid)

    @router.get("/risk/threshold-recommendations")
    def threshold_recommendations(request: Request,
                                  backtest_run_id: str | None = Query(None),
                                  observation_window: int = Query(5),
                                  outcome_label: str | None = Query(None)):
        """阈值建议。**只生成建议，不修改任何线上配置。**"""
        rid = new_request_id()
        run_id = backtest_run_id
        if not run_id:
            latest = db.latest_backtest_run()
            run_id = latest.get("backtest_run_id") if latest else None
        if not run_id:
            return ok({"recommendations": [], "backtest_run_id": None,
                       "note": "尚无回测结果，请先运行一次回测"}, rid)
        recs = ta_.recommend_all(db, run_id, window=observation_window,
                                 label_code=outcome_label)
        summary: dict[str, int] = {}
        for r in recs:
            k = r.get("recommendation")
            summary[k] = summary.get(k, 0) + 1
        return ok({
            "backtest_run_id": run_id,
            "recommendations": recs,
            "summary": summary,
            "note": ("本系统只生成建议，绝不自动修改线上阈值。"
                     "每条建议都同时给出收益与代价，请自行权衡后手动调整 .env。"),
        }, rid)

    # ================= Flex Query 现金流与真实收益 =================

    @router.get("/flex/status")
    def flex_status(request: Request):
        """Flex 同步状态。**绝不返回 token 或完整 query id。**"""
        rid = new_request_id()
        st = flex_sync.status(cfg, db)
        st["recent_runs"] = [_flex_run_payload(r) for r in db.flex_runs(limit=5)]
        return ok(st, rid)

    @router.post("/flex/sync")
    def flex_sync_trigger(request: Request):
        """手动触发 Flex 同步。并发互斥 + 最小间隔。"""
        rid = new_request_id()
        if not flex_sync.is_configured(cfg):
            return fail("NOT_CONFIGURED",
                        "未配置 Flex 凭证。请在 IBKR 客户端门户创建 Flex Query "
                        "并生成 Token，然后填入 .env 的 FLEX_TOKEN / FLEX_QUERY_ID",
                        rid, 409, data={"configured": False})
        if not getattr(cfg, "flex_sync_enabled", True):
            return fail("DISABLED", "Flex 同步已被配置关闭（FLEX_SYNC_ENABLED=false）",
                        rid, 409)
        if db.flex_run_in_progress():
            return fail("IN_PROGRESS", "已有一次 Flex 同步正在进行，请稍后再试",
                        rid, 409)
        last = db.latest_flex_run()
        if last and last.get("completed_at"):
            try:
                gap = (datetime.now(timezone.utc)
                       - datetime.fromisoformat(last["completed_at"]))
                min_gap = timedelta(minutes=int(getattr(cfg, "flex_min_trigger_minutes", 5)))
                if gap < min_gap:
                    return fail("TOO_SOON",
                                f"距上次同步不足 {min_gap.seconds // 60} 分钟，"
                                "Flex 是 T+1 数据源，无需频繁拉取", rid, 429)
            except (TypeError, ValueError):
                pass
        # 手动与定时**共用同一个核心函数**，只有 trigger 不同
        res = flex_sync.run_flex_sync(cfg, db,
                                      trigger=flex_sync.TRIGGER_MANUAL,
                                      request_id=rid)
        if res.get("status") == flex_sync.ST_FAILED:
            return fail("SYNC_FAILED", res.get("error") or "同步失败", rid, 502,
                        data=res)
        return ok(res, rid)

    @router.get("/performance/cash-flows")
    def performance_cash_flows(request: Request,
                               start: str | None = Query(None),
                               end: str | None = Query(None),
                               category: str | None = Query(None),
                               subtype: str | None = Query(None),
                               symbol: str | None = Query(None),
                               currency: str | None = Query(None),
                               limit: int = Query(100, ge=1, le=1000),
                               offset: int = Query(0, ge=0)):
        """现金流列表。外部现金流与交易现金流通过 category 明确区分。"""
        rid = new_request_id()
        rows, total = db.query_cash_flows(
            start=start, end=end, category=_split(category),
            subtype=_split(subtype), symbol=symbol, currency=currency,
            limit=limit, offset=offset)
        return ok({
            "cash_flows": [_cash_flow_payload(r) for r in rows],
            "total": total, "limit": limit, "offset": offset,
            "category_counts": db.cash_flow_category_counts(),
            "categories": list(cfm_.CATEGORIES),
            "external_categories": list(cfm_.EXTERNAL_CATEGORIES),
            "note": ("只有 external_cash_flow 与 transfer 属于「资金进出」；"
                     "trade_cash_flow 是买卖证券产生的现金变动，不是出入金。"),
        }, rid)

    @router.get("/performance/summary")
    def performance_summary(request: Request,
                            start: str | None = Query(None),
                            end: str | None = Query(None),
                            range: str | None = Query(None, alias="range")):
        """收益概览。入金不会被算成盈利，出金不会被算成亏损。"""
        rid = new_request_id()
        s, e = _resolve_perf_range(range, start, end)
        res = perf_.compute_period(cfg, db, start=s, end=e,
                                   account=getattr(cfg, "ib_account", None))
        recon = db.latest_reconciliation(getattr(cfg, "ib_account", None))
        return ok({
            **res,
            "period_start": s, "period_end": e,
            "data_as_of": res.get("period_end"),
            "reconciliation": recon,
            "flex_configured": flex_sync.is_configured(cfg),
            "note": ("modified_dietz_return / time_weighted_return 为 null 时"
                     "表示当前数据不足以可靠计算，**不是 0%**。"),
        }, rid)

    @router.get("/performance/history")
    def performance_history(request: Request,
                            range: str | None = Query("30d", alias="range"),
                            start: str | None = Query(None),
                            end: str | None = Query(None),
                            granularity: str = Query("day")):
        """收益历史。支持 7d/30d/90d/YTD/自定义，日或周聚合。"""
        rid = new_request_id()
        s, e = _resolve_perf_range(range, start, end)
        periods = db.performance_periods(s, e, granularity=granularity,
                                         account=getattr(cfg, "ib_account", None),
                                         version=perf_.CALC_VERSION)
        return ok({
            "range": range or "custom", "start": s, "end": e,
            "granularity": granularity,
            "periods": periods, "total": len(periods),
            "calculation_version": perf_.CALC_VERSION,
            "note": ("区间收益按需计算并缓存；尚未计算过的区间返回空，"
                     "可先调用 /performance/summary 触发。"),
        }, rid)

    @router.get("/performance/reconciliation")
    def performance_reconciliation(request: Request,
                                   report_from: str | None = Query(None),
                                   report_to: str | None = Query(None)):
        """分币种现金对账。以 Statement of Funds 余额锚点为准。"""
        rid = new_request_id()
        account = getattr(cfg, "ib_account", None)
        res = reconccy_.reconcile_report(cfg, db, account=account,
                                         report_from=report_from,
                                         report_to=report_to)
        # 交叉验证只用基础币种的期末余额
        base_closing = None
        for c in res.get("currencies", []):
            if c.get("currency") == res.get("base_currency"):
                base_closing = c.get("closing_balance")
        cross = reconccy_.cross_validate(
            db, report_to=res.get("report_to") or "", account=account,
            flex_closing_base=base_closing) if res.get("report_to") else None
        return ok({
            **res,
            "balance_anchors": db.statement_balances(
                account=account, report_from=res.get("report_from"),
                report_to=res.get("report_to")),
            "snapshot_cross_validation": cross,
            "conversion_rates_available": db.conversion_rate_count(),
            "note": ("对账主口径是同一份 Flex 报表内的期初/期末余额，"
                     "账户快照仅作交叉验证；两者口径本就不同，不要求相等。"),
        }, rid)

    @router.get("/performance/attribution")
    def performance_attribution(request: Request,
                                start: str | None = Query(None),
                                end: str | None = Query(None),
                                range: str | None = Query(None, alias="range")):
        """完整收益归因。每一项都有独立来源，未解释差额如实保留。"""
        rid = new_request_id()
        s, e = _resolve_perf_range(range, start, end)
        res = perf_.compute_period(cfg, db, start=s, end=e,
                                   account=getattr(cfg, "ib_account", None))
        contributions = []
        u = res.get("unrealized_detail") or {}
        if u.get("start_date") and u.get("end_date"):
            try:
                contributions = upnl_.position_contributions(
                    db, start_date=u["start_date"], end_date=u["end_date"],
                    account=getattr(cfg, "ib_account", None))
            except Exception:  # noqa: BLE001
                contributions = []
        return ok({
            "period_start": s, "period_end": e,
            "attribution": {
                "raw_nav_change": res.get("raw_nav_change"),
                "deposits": res.get("deposits"),
                "withdrawals": res.get("withdrawals"),
                "net_external_cash_flow": res.get("external_cash_flow"),
                "cash_flow_adjusted_pnl": res.get("cash_flow_adjusted_pnl"),
                "realized_pnl": res.get("realized_pnl"),
                "unrealized_pnl_start": res.get("unrealized_pnl_start"),
                "unrealized_pnl_end": res.get("unrealized_pnl_end"),
                "unrealized_pnl_change": res.get("unrealized_pnl_change"),
                "dividends": res.get("dividends"),
                "interest": res.get("interest"),
                "commissions": res.get("commissions"),
                "other_fees": res.get("fees"),
                "taxes": res.get("taxes"),
                "fx_effect": res.get("fx_effect"),
                "unexplained_difference": res.get("unexplained_difference"),
            },
            "modified_dietz_return": res.get("modified_dietz_return"),
            "time_weighted_return": res.get("time_weighted_return"),
            "calculation_method": res.get("calculation_method_detail"),
            "calculation_version": res.get("calculation_version"),
            "data_quality": res.get("data_quality"),
            "data_limitations": res.get("data_limitations"),
            "cash_flow_source": res.get("cash_flow_source"),
            "coverage": res.get("coverage"),
            "position_contributions": contributions,
        }, rid)

    @router.get("/performance/unrealized-pnl")
    def performance_unrealized(request: Request,
                               start: str | None = Query(None),
                               end: str | None = Query(None),
                               range: str | None = Query(None, alias="range")):
        """按日未实现盈亏序列与单标的贡献。"""
        rid = new_request_id()
        s, e = _resolve_perf_range(range, start, end)
        account = getattr(cfg, "ib_account", None)
        upnl_.build_series(cfg, db, start=s, end=e, account=account)
        series = db.daily_unrealized(s, e, account=account,
                                     version=upnl_.CALC_VERSION)
        change = upnl_.period_change(series)
        contributions = []
        if change.get("start_date") and change.get("end_date"):
            contributions = upnl_.position_contributions(
                db, start_date=change["start_date"],
                end_date=change["end_date"], account=account)
        return ok({
            "period_start": s, "period_end": e,
            "series": series, "days": len(series),
            "change": change,
            "position_contributions": contributions,
            "calculation_version": upnl_.CALC_VERSION,
            "note": ("每日固定取当天最后一份快照，同一日绝不混用不同 snapshot_id；"
                     "清仓标的的浮盈已转为已实现盈亏，不在此处重复计算。"),
        }, rid)

    # ================= 账户规则历史重放 =================

    @router.post("/risk/replays")
    def start_replay(request: Request,
                     date_start: str | None = Query(None),
                     date_end: str | None = Query(None),
                     sampling_mode: str = Query(replay_.SAMPLE_DAILY_CLOSE),
                     rule_set_mode: str = Query(replay_.MODE_CURRENT_RULES),
                     interval_minutes: int = Query(60, ge=5, le=1440),
                     rules: str | None = Query(None),
                     include_unattributed: bool = Query(False),
                     reuse: bool = Query(True)):
        """启动历史重放。**结果与实时系统完全隔离，绝不发送任何通知。**"""
        rid_ = new_request_id()
        if sampling_mode not in (replay_.SAMPLE_DAILY_CLOSE,
                                 replay_.SAMPLE_INTRADAY):
            return fail("BAD_SAMPLING", f"不支持的采样模式：{sampling_mode}", rid_)
        if rule_set_mode not in (replay_.MODE_POINT_IN_TIME,
                                 replay_.MODE_CURRENT_RULES):
            return fail("BAD_MODE", f"不支持的规则版本模式：{rule_set_mode}", rid_)
        stats = db.replay_snapshot_stats()
        start = date_start or (stats["snapshot_range"]["start"] or "")[:10]
        end = date_end or (stats["snapshot_range"]["end"] or "")[:10]
        if not (start and end):
            return fail("NO_SNAPSHOTS", "没有可用的账户快照", rid_, 409)
        res = replay_.run_replay(
            cfg, db, date_start=start, date_end=end,
            sampling_mode=sampling_mode, interval_minutes=interval_minutes,
            rule_set_mode=rule_set_mode, rules=_split(rules),
            include_unattributed=include_unattributed, reuse=reuse)
        if res.get("status") == replay_.ST_FAILED:
            return fail("REPLAY_FAILED", res.get("error") or "重放失败",
                        rid_, 500, data=res)
        return ok({**res, "disclaimer": _REPLAY_DISCLAIMER}, rid_)

    @router.get("/risk/daily-pnl/diagnostics")
    def daily_pnl_diagnostics(request: Request,
                              hours: int = Query(24, ge=1, le=168)):
        """当日盈亏指标的只读诊断。

        看这个接口是为了回答一个具体问题：当日亏损率**这一轮到底闭合了没有**。
        分子分母是否同周期、时间差多少、为什么不可靠、24 小时里换了几次来源、
        有多少次状态迁移被质量门禁挡住 —— 全在这里，不用翻日志。

        **纯只读**，不触发计算、不写库、不发通知。
        """
        from datetime import datetime, timedelta, timezone

        from . import daily_pnl_observation as dpo
        from . import daily_pnl_service as dps
        from . import daily_pnl_v2 as dpv2

        rid_ = new_request_id()
        since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
        latest = db.latest_daily_pnl_observation(cfg.ib_account or None)
        cur = dict(latest) if latest else {}
        reasons = []
        try:
            reasons = json.loads(cur.get("quality_reasons") or "[]")
        except Exception:  # noqa: BLE001
            reasons = []
        summary = dps.shadow_summary(db, since,
                                     threshold=cfg.risk_daily_loss_max)
        return ok({
            "window_hours": hours,
            "latest": {
                "observed_at": cur.get("observed_at"),
                "session_date": cur.get("session_date"),
                "source": cur.get("source"),
                "basis": cur.get("basis"),
                "correlation_id": cur.get("correlation_id"),
                # 分子与分母各自的时间戳 + 两者时间差 —— 跨周期拼接的唯一凭据
                "numerator_observed_at": cur.get("daily_pnl_observed_at"),
                "denominator_observed_at": cur.get("net_liquidation_observed_at"),
                "observation_skew_seconds": cur.get("observation_skew_seconds"),
                "quality_status": cur.get("quality_status"),
                "reliable": bool(cur.get("reliable")),
                "quality_reasons": reasons,
                "quality_reason_texts": [dpo.REASON_TEXT.get(r, r) for r in reasons],
                "daily_loss_ratio": cur.get("daily_loss_ratio"),
                "source_changed": bool(cur.get("source_changed")),
                "basis_changed": bool(cur.get("basis_changed")),
            },
            "shadow": summary,
            "skew_tolerance_seconds": getattr(
                cfg, "daily_pnl_skew_tolerance_seconds", 90),
            "v2_enabled": dpv2.enabled(cfg),
            # 口径声明：哪条路在驱动事件、执行顺序如何、当前生效版本
            "mode": dpv2.mode(cfg),
            "order_mode": dpv2.order_mode(cfg),
            "active_calculation_version": dpv2.active_calculation_version(cfg),
            "active_daily_pnl_version": dpv2.active_daily_pnl_version(cfg),
            "calculation_version": dpo.CALC_VERSION,
            # v2 启用前自检结论（关闭时 skipped=true）
            "preflight": dpv2.preflight(cfg, db),
            # old / v2 各自的生效区间。回滚后历史不改，靠这个区分某条历史评估
            # 当时是哪条口径算出来的 —— 版本串本身就是区间标记。
            "version_ranges": dps.version_ranges(db, since),
            "note": ("v2 处于影子期：只落库与比较，不驱动事件、不发通知。"
                     "切换需单独授权。" if not dpv2.enabled(cfg) else
                     "v2 已接管 ACC_DAILY_LOSS_HIGH。历史评估与事件保持原样不回填。"),
        }, rid_)

    @router.get("/risk/pnl-source/reconciliation")
    def pnl_source_reconciliation(request: Request,
                                  hours: int = Query(24, ge=1, le=168),
                                  limit: int = Query(500, ge=1, le=5000)):
        """IBKR 官方 PnL 与富途 shadow 估值的只读对账。"""
        rid_ = new_request_id()
        since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
        rows = db.recent_pnl_reconciliations(since_iso=since, limit=limit)
        for row in rows:
            try:
                row["limitations"] = json.loads(row.get("limitations") or "[]")
            except Exception:  # noqa: BLE001
                row["limitations"] = ["INVALID_LIMITATIONS_JSON"]
            row["large_but_explained"] = bool(row.get("large_but_explained"))
        return ok({
            "window_hours": hours,
            "latest": rows[0] if rows else None,
            "rows": rows,
            "total": len(rows),
            "authority": {
                "account_pnl": "IBKR_reqPnL",
                "position_pnl": "IBKR_reqPnLSingle",
                "market_quote_shadow": "futu",
                "futu_account_pnl_authoritative": False,
            },
            "note": ("富途仅作行情与估值交叉验证；IBKR daily/realized/unrealized PnL、"
                     "NetLiquidation、现金、保证金和购买力保持权威。"),
        }, rid_)

    @router.get("/system/scheduler/diagnostics")
    def scheduler_diagnostics(request: Request,
                              hours: int = Query(24, ge=1, le=168),
                              job_id: str | None = Query(None),
                              limit: int = Query(1000, ge=1, le=10000)):
        """调度执行/错过/异常记录；纯只读，不触发任何 job。"""
        rid_ = new_request_id()
        since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
        rows = db.recent_scheduler_job_diagnostics(
            since_iso=since, job_id=job_id, limit=limit)
        per_job: dict[str, dict[str, int]] = {}
        for row in rows:
            bucket = per_job.setdefault(row["job_id"], {
                "executed": 0, "missed": 0, "error": 0})
            key = str(row.get("outcome") or "").lower()
            if key in bucket:
                bucket[key] += 1
        return ok({
            "window_hours": hours, "job_id": job_id,
            "rows": rows, "total": len(rows), "per_job": per_job,
            "note": ("此接口区分 scheduler cycle 是否执行与 IBKR/Futu 数据是否更新；"
                     "二者不是同一个 freshness 指标。"),
        }, rid_)

    @router.get("/risk/replays")
    def list_replays(request: Request, limit: int = Query(20, ge=1, le=100)):
        rid_ = new_request_id()
        return ok({"runs": db.replay_runs(limit=limit),
                   "disclaimer": _REPLAY_DISCLAIMER}, rid_)

    @router.get("/risk/replays/coverage")
    def replay_coverage(request: Request):
        """重放覆盖率：能重放多少、为什么不能更多。"""
        rid_ = new_request_id()
        return ok(replay_.coverage_report(cfg, db), rid_)

    @router.get("/risk/replays/{replay_run_id}")
    def replay_detail(replay_run_id: str, request: Request):
        rid_ = new_request_id()
        run = db.get_replay_run(replay_run_id)
        if not run:
            return fail("NOT_FOUND", f"重放任务不存在：{replay_run_id}", rid_, 404)
        return ok({
            "run": {**run,
                    "coverage_summary": _jload(run.get("coverage_summary"), {}),
                    "skip_reasons": _jload(run.get("skip_reasons"), {}),
                    "requested_rules": _jload(run.get("requested_rules"), [])},
            "rule_summary": db.replay_rule_summary(replay_run_id),
            "disclaimer": _REPLAY_DISCLAIMER,
        }, rid_)

    @router.get("/risk/replays/{replay_run_id}/assessments")
    def replay_assessments(replay_run_id: str, request: Request,
                           limit: int = Query(200, ge=1, le=2000),
                           offset: int = Query(0, ge=0)):
        rid_ = new_request_id()
        if not db.get_replay_run(replay_run_id):
            return fail("NOT_FOUND", f"重放任务不存在：{replay_run_id}", rid_, 404)
        rows, total = db.replay_assessments(replay_run_id, limit=limit,
                                            offset=offset)
        return ok({"assessments": [{**r,
                                    "component_scores": _jload(r.get("component_scores"), {}),
                                    "triggered_rules": _jload(r.get("triggered_rules"), [])}
                                   for r in rows],
                   "total": total, "limit": limit, "offset": offset,
                   "disclaimer": _REPLAY_DISCLAIMER}, rid_)

    @router.get("/risk/replays/{replay_run_id}/events")
    def replay_events(replay_run_id: str, request: Request,
                      rule_code: str | None = Query(None),
                      limit: int = Query(200, ge=1, le=2000),
                      offset: int = Query(0, ge=0)):
        rid_ = new_request_id()
        if not db.get_replay_run(replay_run_id):
            return fail("NOT_FOUND", f"重放任务不存在：{replay_run_id}", rid_, 404)
        rows, total = db.replay_events(replay_run_id, rule_code=rule_code,
                                       limit=limit, offset=offset)
        return ok({"events": [{**r, "source_snapshot_keys":
                               _jload(r.get("source_snapshot_keys"), [])}
                              for r in rows],
                   "total": total, "limit": limit, "offset": offset,
                   "disclaimer": _REPLAY_DISCLAIMER}, rid_)

    # ---------- Hyperliquid XYZ 链上情报（独立库，只读消费） ----------
    hl_db = HyperliquidDatabase(getattr(cfg, "hl_db_path", "./data/hyperliquid.db"))

    def _hl_service() -> HyperliquidService:
        return HyperliquidService(cfg, hl_db)

    @router.get("/hyperliquid/status")
    def hyperliquid_status(request: Request):
        rid = new_request_id()
        health = hl_db.health()
        health.update({
            "enabled": bool(getattr(cfg, "hyperliquid_enabled", False)),
            "collector_enabled": bool(getattr(cfg, "hl_collector_enabled", False)),
            "history_backfill_enabled": bool(getattr(cfg, "hl_history_backfill_enabled", False)),
            "history_backfill_days": int(getattr(cfg, "hl_history_backfill_days", 180)),
            "wallet_scoring_enabled": bool(getattr(cfg, "hl_wallet_scoring_enabled", False)),
            "signal_shadow_enabled": bool(getattr(cfg, "hl_signal_shadow_enabled", False)),
            "intraday_alerts_enabled": bool(getattr(cfg, "hl_intraday_alerts_enabled", False)),
            "swing_influence_enabled": bool(getattr(cfg, "hl_swing_influence_enabled", False)),
            "dex": getattr(cfg, "hl_dex", "xyz"),
        })
        if hl_db.available():
            with hl_db.connect(read_only=True) as conn:
                latest = conn.execute(
                    "SELECT * FROM hl_backfill_runs ORDER BY started_at DESC LIMIT 1").fetchone()
                origins = {str(r["data_origin"]): int(r["n"]) for r in conn.execute(
                    "SELECT data_origin,COUNT(*) AS n FROM hl_wallet_fills GROUP BY data_origin")}
            health["latest_backfill"] = dict(latest) if latest else None
            health["wallet_fill_origins"] = origins
        return ok(health, rid)

    @router.get("/hyperliquid/assets")
    def hyperliquid_assets(request: Request, limit: int = Query(200, ge=1, le=1000)):
        rid = new_request_id()
        if not hl_db.available():
            return fail("HL_UNAVAILABLE", "Hyperliquid 数据库不可用", rid, 503)
        return ok({"assets": _hl_service().assets(limit=limit)}, rid)

    @router.get("/hyperliquid/signals")
    def hyperliquid_signals(request: Request, model: str | None = Query(None),
                            symbol: str | None = Query(None),
                            active_only: bool = Query(False),
                            limit: int = Query(100, ge=1, le=500)):
        rid = new_request_id()
        if not hl_db.available():
            return fail("HL_UNAVAILABLE", "Hyperliquid 数据库不可用", rid, 503)
        coin = symbol
        if coin and ":" not in coin:
            coin = f"{getattr(cfg, 'hl_dex', 'xyz')}:{coin.upper()}"
        rows = _hl_service().signals(model=model, coin=coin, active_only=active_only,
                                      limit=limit)
        return ok({"signals": rows, "count": len(rows)}, rid)

    @router.get("/hyperliquid/signals/{signal_id}")
    def hyperliquid_signal_detail(signal_id: str, request: Request):
        rid = new_request_id()
        if not hl_db.available():
            return fail("HL_UNAVAILABLE", "Hyperliquid 数据库不可用", rid, 503)
        with hl_db.connect(read_only=True) as conn:
            row = conn.execute("SELECT * FROM hl_signal_episodes WHERE signal_id=?",
                               (signal_id,)).fetchone()
        if row is None:
            return fail("NOT_FOUND", "链上信号不存在", rid, 404)
        return ok(dict(row), rid)

    @router.get("/hyperliquid/wallets")
    def hyperliquid_wallets(request: Request, classification: str | None = Query(None),
                            limit: int = Query(100, ge=1, le=200)):
        rid = new_request_id()
        if not hl_db.available():
            return fail("HL_UNAVAILABLE", "Hyperliquid 数据库不可用", rid, 503)
        rows = _hl_service().wallets(classification=classification, limit=limit)
        return ok({"wallets": rows, "count": len(rows)}, rid)

    @router.get("/hyperliquid/wallets/{address}")
    def hyperliquid_wallet_detail(address: str, request: Request):
        rid = new_request_id()
        try:
            address = _hl_service().validate_address(address)
        except ValueError as exc:
            return fail("INVALID_ADDRESS", str(exc), rid, 422)
        if not hl_db.available():
            return fail("HL_UNAVAILABLE", "Hyperliquid 数据库不可用", rid, 503)
        with hl_db.connect(read_only=True) as conn:
            candidate = conn.execute("SELECT * FROM hl_wallet_candidates WHERE address=?",
                                     (address,)).fetchone()
            scores = [dict(r) for r in conn.execute(
                "SELECT * FROM hl_wallet_scores WHERE address=? ORDER BY as_of DESC LIMIT 50",
                (address,))]
            cycles = [dict(r) for r in conn.execute(
                "SELECT * FROM hl_wallet_cycles WHERE address=? ORDER BY opened_at DESC LIMIT 100",
                (address,))]
        if candidate is None:
            return fail("NOT_FOUND", "钱包尚未进入候选池", rid, 404)
        return ok({"wallet": dict(candidate), "scores": scores, "cycles": cycles,
                   "performance": _hl_service().wallet_performance(address)}, rid)

    @router.post("/hyperliquid/wallets/{address}/tracking")
    async def hyperliquid_wallet_tracking(address: str, request: Request):
        rid = new_request_id()
        # 认证关闭时，禁止公开写入控制表；启用认证时由全站 middleware 先拦截匿名请求。
        if not getattr(cfg, "dashboard_auth_enabled", False):
            return fail("AUTH_REQUIRED", "钱包跟踪控制需要先开启看板认证", rid, 403)
        try:
            body = await request.json()
            result = _hl_service().save_wallet_control(
                db, address, body.get("mode"), body.get("label"), body.get("note"))
        except ValueError as exc:
            return fail("INVALID_CONTROL", str(exc), rid, 422)
        except Exception as exc:  # noqa: BLE001 — 控制写入失败不泄露 SQL 细节
            log.exception("Hyperliquid 钱包控制写入失败")
            return fail("CONTROL_WRITE_FAILED", type(exc).__name__, rid, 500)
        return ok(result, rid)

    @router.get("/hyperliquid/outcomes")
    def hyperliquid_outcomes(request: Request, symbol: str | None = Query(None),
                             horizon: str | None = Query(None),
                             limit: int = Query(200, ge=1, le=1000)):
        rid = new_request_id()
        params: list[Any] = []
        where: list[str] = []
        if symbol:
            where.append("canonical_symbol=?"); params.append(symbol.upper())
        if horizon:
            where.append("horizon=?"); params.append(horizon)
        clause = "WHERE " + " AND ".join(where) if where else ""
        with db.connect() as conn:
            rows = [dict(r) for r in conn.execute(
                f"SELECT * FROM hl_signal_outcomes {clause} ORDER BY calculated_at DESC LIMIT ?",
                (*params, limit))]
        return ok({"outcomes": rows, "count": len(rows)}, rid)

    @router.get("/hyperliquid/validation")
    def hyperliquid_validation(request: Request, limit: int = Query(50, ge=1, le=200)):
        rid = new_request_id()
        with db.connect() as conn:
            runs = [dict(r) for r in conn.execute(
                "SELECT * FROM hl_validation_runs ORDER BY started_at DESC LIMIT ?", (limit,))]
            promotions = [dict(r) for r in conn.execute(
                "SELECT * FROM hl_model_promotions ORDER BY updated_at DESC LIMIT ?", (limit,))]
            commitments = [dict(r) for r in conn.execute(
                "SELECT * FROM hl_validation_commitments ORDER BY committed_at DESC LIMIT ?", (limit,))]
        return ok({"runs": runs, "promotions": promotions,
                   "commitments": commitments}, rid)

    return router
