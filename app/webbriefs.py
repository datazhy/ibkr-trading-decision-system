"""每日简报页面的数据层。

页面设计的一条原则：**AI 文字不是唯一信息来源。** 每个关键数字旁边都显示
它的结构化字段值与来源，让用户随时能绕过文字直接看数据。
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from . import brief_generator as bg
from . import brief_prompt as bp
from . import webdata

_STATUS_BADGE = {
    bg.STATUS_AI: ("AI 生成", "ok"),
    bg.STATUS_TEMPLATE: ("模板降级（未使用 AI）", "warn"),
    bg.STATUS_BLOCKED: ("数据受阻简报", "crit"),
}
_MODE_BADGE = {
    "full": ("完整", "ok"),
    "limited": ("数据受限", "warn"),
    "blocked": ("已阻止风险结论", "crit"),
}


def _j(v, default):
    if isinstance(v, (dict, list)):
        return v
    try:
        return json.loads(v) if v else default
    except (TypeError, ValueError):
        return default


def page_data(cfg, db, now: datetime | None = None, *,
              brief_id: str | None = None, date: str | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    row = None
    if brief_id:
        row = db.get_brief(brief_id)
    if row is None:
        row = db.latest_brief(brief_date=date)

    history, total = db.query_briefs(limit=30)
    runs = db.brief_runs(limit=10)

    if row is None:
        return {
            "available": False,
            "disclaimer": bp.DISCLAIMER,
            "history": history, "total": total, "runs": runs,
        "executions": db.schedule_executions(limit=10),
            "note": "还没有生成过任何简报。可在系统状态页手动触发，"
                    "或等待每日调度任务。",
        }

    content = _j(row.get("content_json"), {})
    status = row["generation_status"]
    s_label, s_css = _STATUS_BADGE.get(status, (status, "warn"))
    m_label, m_css = _MODE_BADGE.get(row["generation_mode"],
                                     (row["generation_mode"], "warn"))

    sources = db.brief_sources(row["brief_id"])
    by_field = {s["field_path"]: s for s in sources}

    return {
        "available": True,
        "brief_id": row["brief_id"],
        "brief_date": row["brief_date"],
        "revision": row.get("revision"),
        "account_masked": row.get("account_id_masked"),
        "status": status, "status_label": s_label, "status_css": s_css,
        "mode": row["generation_mode"], "mode_label": m_label, "mode_css": m_css,
        "is_ai": status == bg.STATUS_AI,
        "is_blocked": status == bg.STATUS_BLOCKED,
        "validation_status": row["validation_status"],
        "validation_errors": _j(row.get("validation_errors"), []),
        "prompt_version": row.get("prompt_version"),
        "model": row.get("model_identifier") or "—（未使用 AI）",
        "context_hash": row.get("context_hash"),
        "context_version": row.get("context_version"),
        "ai_attempts": row.get("ai_attempts"),
        "calc_versions": _j(row.get("calculation_versions"), {}),
        "generated_at_abs": webdata.abs_time(row.get("generated_at"),
                                             cfg.market_tz),
        "delivered_at_abs": (webdata.abs_time(row["delivered_at"], cfg.market_tz)
                             if row.get("delivered_at") else None),
        "content": content,
        "markdown": row.get("rendered_markdown"),
        "sources": sources,
        "source_by_field": by_field,
        "source_count": len(sources),
        "deliveries": db.brief_deliveries(row["brief_id"]),
        "history": history, "total": total, "runs": runs,
        "executions": db.schedule_executions(limit=10),
        "disclaimer": bp.DISCLAIMER,
        # 关键数字的结构化取值 —— 与 AI 文字并列展示，供用户交叉核对
        "facts": _facts(content, by_field),
    }


def _facts(content: dict, by_field: dict) -> list[dict]:
    """关键数字 + 来源。让「这个数字哪来的」一眼可查。"""
    a = content.get("account_summary") or {}
    m = content.get("market_summary") or {}
    c = content.get("concentration") or {}
    p = content.get("performance") or {}

    rows = [
        ("账户风险等级", a.get("risk_level"), "account.risk_level"),
        ("账户风险评分", a.get("risk_score"), "account.risk_score"),
        ("净资产", a.get("net_liquidation"), "account.net_liquidation"),
        ("现金比例", a.get("cash_ratio"), "account.cash_ratio"),
        ("持仓总市值", a.get("positions_market_value"),
         "account.positions_market_value"),
        ("多头敞口", a.get("long_exposure"), "account.long_exposure"),
        ("空头敞口", a.get("short_exposure"), "account.short_exposure"),
        ("总风险敞口", a.get("gross_exposure"), "account.gross_exposure"),
        ("总杠杆", a.get("gross_leverage"), "account.gross_leverage"),
        ("当前回撤", a.get("current_drawdown"), "account.current_drawdown"),
        ("市场状态", m.get("market_state"), "market.market_state"),
        ("市场风险评分", m.get("risk_score"), "market.market_risk_score"),
        ("QQQ 收盘", m.get("qqq_close"), "market.qqq.close"),
        ("SOXX 收盘", m.get("soxx_close"), "market.soxx.close"),
        ("SOXX 相对 QQQ 20 日", m.get("soxx_vs_qqq_20d"),
         "market.sox_vs_qqq_20d"),
        ("VIX", m.get("vix"), "market.vix.current"),
        ("VXN", m.get("vxn"), "market.vxn.current"),
        ("十年期美债", m.get("us10y"), "market.us10y.current"),
        ("HHI", c.get("hhi"), "concentration.hhi"),
        ("等效持仓数", c.get("effective_position_count"),
         "concentration.effective_position_count"),
        ("最大单仓权重", c.get("largest_position_weight"),
         "concentration.top1_weight"),
        ("剔除资金进出后收益", p.get("cash_flow_adjusted_pnl"),
         "performance.cash_flow_adjusted_pnl"),
        ("已实现盈亏", p.get("realized_pnl"), "performance.realized_pnl"),
        ("Modified Dietz", p.get("modified_dietz_return"),
         "performance.modified_dietz_return"),
        ("TWR", p.get("twr_return"), "performance.twr_return"),
    ]
    out = []
    for label, value, path in rows:
        src = by_field.get(path) or {}
        out.append({
            "label": label,
            # None 显示「无数据」，绝不显示 0
            "value": ("无数据" if value in (None, "", "None") else str(value)),
            "field_path": path,
            "source_table": src.get("source_table"),
            "source_id": (str(src.get("source_id") or "")[:12] or None),
            "source_version": src.get("source_version"),
            "data_as_of": src.get("data_as_of"),
            "source_type": src.get("source_type"),
        })
    return out
