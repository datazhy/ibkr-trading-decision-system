"""飞书告警文案（纯函数）。

三条硬性约束：
1. **绝不出现确定性买卖指令**。小鱼交易台是只读交易决策系统，"立即清仓""必然下跌"这类
   措辞既超出数据能支撑的结论，也越过了产品边界。每条消息尾部固定声明。
2. **可追溯**。每条消息都带规则码、规则版本、数据截至时间 —— 事后能查到
   「当时用的是哪套阈值」。
3. **不含任何凭证**。落库的 payload_summary 与推送正文都只有风险信息。
"""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from .risk_events import (SEV_CRITICAL, SEV_HIGH, severity_label, source_label)

#: 固定尾注。提醒通知里的建议不会被自动执行。
DISCLAIMER = "系统不接入下单接口，请自行核实后执行。"

_SEV_ICON = {SEV_CRITICAL: "🔴", SEV_HIGH: "🟠", "warning": "🟡", "info": "🔵"}


def _icon(sev: str) -> str:
    return _SEV_ICON.get(sev, "⚪")


def _local(ts: str | None, tz) -> str:
    if not ts:
        return "—"
    try:
        dt = datetime.fromisoformat(ts)
    except (TypeError, ValueError):
        return str(ts)
    try:
        return dt.astimezone(tz).strftime("%Y-%m-%d %H:%M")
    except (TypeError, ValueError):
        return dt.strftime("%Y-%m-%d %H:%M")


def _value_line(row: dict) -> str | None:
    act, thr = row.get("actual_value"), row.get("threshold_value")
    disp = row.get("actual_display") or act
    tdisp = row.get("threshold_display") or thr
    if disp is None and tdisp is None:
        return None
    if tdisp is None:
        return f"当前值：{disp}"
    return f"当前值：{disp}（阈值 {tdisp}）"


def _affected(row: dict) -> str | None:
    a = row.get("affected_symbol") or row.get("affected_instrument")
    return f"影响对象：{a}" if a else None


def format_single(row: dict, *, notification_type: str, tz,
                  dashboard_url: str | None = None,
                  account_state: dict | None = None,
                  is_test: bool = False) -> tuple[str, str]:
    """单条事件通知。返回 (title, body)。"""
    sev = row.get("severity") or "warning"
    prefix = "【测试】" if is_test else ""
    if notification_type == "recovered":
        title = f"{prefix}✅ 风险已解除：{row.get('title') or row.get('rule_code')}"
    elif notification_type == "escalated":
        title = f"{prefix}{_icon(sev)}【{severity_label(sev)}·升级】{row.get('title')}"
    else:
        title = f"{prefix}{_icon(sev)}【{severity_label(sev)}】{row.get('title')}"

    lines: list[str] = []
    if is_test:
        lines += ["⚠️ 这是一条**测试通知**，不代表账户发生了新风险。", ""]
    lines.append(f"来源：{source_label(row.get('source_type'))} · "
                 f"规则 {row.get('rule_code')}")
    if account_state:
        acc = account_state.get("account_risk_level")
        mkt = account_state.get("market_state")
        if acc or mkt:
            lines.append(f"账户风险：{acc or '—'} · 市场风险：{mkt or '—'}")

    v = _value_line(row)
    if v:
        lines.append(v)
    a = _affected(row)
    if a:
        lines.append(a)
    lines.append(f"首次发生：{_local(row.get('first_detected_at'), tz)}")
    lines.append(f"数据截至：{row.get('data_as_of') or '—'}")
    lines.append(f"数据可靠：{'是' if row.get('reliable') else '否（结论仅供参考）'}")

    if notification_type == "recovered" and row.get("resolved_at"):
        lines.append(f"恢复时间：{_local(row.get('resolved_at'), tz)}")

    if row.get("explanation"):
        lines += ["", f"为什么重要：{row['explanation']}"]
    if row.get("recommended_check") and notification_type != "recovered":
        lines += ["", f"建议检查：{row['recommended_check']}"]

    lines += ["", f"规则版本：{row.get('rule_set_version') or '—'}"]
    if dashboard_url:
        lines.append(f"风险看板：{dashboard_url}")
    lines += ["", DISCLAIMER]
    return title, "\n".join(lines)


def format_batch(rows: list[dict], *, notification_type: str, tz,
                 dashboard_url: str | None = None,
                 account_state: dict | None = None,
                 is_test: bool = False) -> tuple[str, str]:
    """合并通知。四条相关的市场事件应该是一条「市场风险恶化」，不是四条飞书。"""
    if len(rows) == 1:
        return format_single(rows[0], notification_type=notification_type, tz=tz,
                             dashboard_url=dashboard_url,
                             account_state=account_state, is_test=is_test)

    top = max(rows, key=lambda r: {"critical": 3, "high": 2,
                                   "warning": 1}.get(r.get("severity"), 0))
    sev = top.get("severity") or "warning"
    domain = source_label(rows[0].get("source_type"))
    prefix = "【测试】" if is_test else ""
    if notification_type == "recovered":
        title = f"{prefix}✅ {len(rows)} 项{domain}风险已解除"
    else:
        title = (f"{prefix}{_icon(sev)}【{severity_label(sev)}】"
                 f"{domain}风险恶化（{len(rows)} 项）")

    lines: list[str] = []
    if is_test:
        lines += ["⚠️ 这是一条**测试通知**，不代表账户发生了新风险。", ""]
    if account_state:
        acc = account_state.get("account_risk_level")
        mkt = account_state.get("market_state")
        amp = account_state.get("risk_amplification_factor")
        if acc or mkt:
            lines.append(f"账户风险：{acc or '—'} · 市场风险：{mkt or '—'}"
                         + (f" · 联合放大 ×{amp}" if amp else ""))
    lines.append(f"数据截至：{rows[0].get('data_as_of') or '—'}")
    lines += ["", "主要触发："]
    for i, r in enumerate(rows[:8], 1):
        rule_ref = f" · 规则 {r.get('rule_code')}" if r.get("rule_code") else ""
        bits = [f"{i}. {r.get('title')}{rule_ref}"]
        v = _value_line(r)
        if v:
            bits.append(f"（{v}）")
        lines.append("".join(bits))
    if len(rows) > 8:
        lines.append(f"…… 另有 {len(rows) - 8} 项，详见看板")

    checks = []
    for r in rows:
        c = r.get("recommended_check")
        if c and c not in checks:
            checks.append(c)
    if checks and notification_type != "recovered":
        lines += ["", "建议检查："]
        lines += [f"- {c}" for c in checks[:5]]

    versions = sorted({r.get("rule_set_version") for r in rows if r.get("rule_set_version")})
    if versions:
        lines += ["", f"规则版本：{'、'.join(versions)}"]
    if dashboard_url:
        lines.append(f"风险看板：{dashboard_url}")
    lines += ["", DISCLAIMER]
    return title, "\n".join(lines)


def payload_summary(title: str, body: str, *, limit: int = 900) -> str:
    """落库用的正文摘要。只截断长度，不做任何脱敏处理是不够的 ——
    正文本身在构造时就不含凭证（凭证只存在于 Notifier 的 URL 里，从不进入这里）。"""
    text = f"{title}\n{body}"
    return text[:limit]
