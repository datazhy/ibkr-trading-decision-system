"""持仓计划的用户可见标签与格式化。

数据库/API 保留稳定的英文枚举；网页和投资日记只通过这里输出中文语义，避免模板
各自维护一套映射。金额、价格和比例仍以 Decimal/字符串保存，只有展示层舍入。
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo


STATUS_LABELS = {
    "PENDING": "待完善",
    "DRAFT": "草稿",
    "ACTIVE": "有效",
    "REVIEW_DUE": "待复盘",
    "TRIGGERED": "已触发",
    "INVALIDATED": "逻辑失效",
    "CLOSED": "已关闭",
    "ARCHIVED": "已归档",
}
ACTION_LABELS = {
    "ADD": "加仓",
    "REDUCE": "减仓",
    "EXIT": "平仓",
    "REVIEW": "重新评估",
}
TRIGGER_LABELS = {
    "PRICE": "价格条件",
    "EVENT": "事件条件",
    "DATE": "日期条件",
    "MANUAL": "手动确认",
}
MATCH_LABELS = {
    "MATCHED": "计划内执行",
    "PLAN_OUTSIDE": "计划外操作",
    "PENDING_CONFIRMATION": "待确认",
    "NOT_APPLICABLE": "不适用",
}
ACTION_STATUS_LABELS = {
    "DRAFT": "草稿",
    "ACTIVE": "有效",
    "TRIGGERED": "已触发",
    "EXECUTED": "已执行",
    "CANCELED": "已取消",
}
EVENT_TYPE_LABELS = {
    "OPEN": "建仓",
    "ADD": "加仓",
    "REDUCE": "减仓",
    "EXIT": "全部退出",
    "ADJUST": "仓位调整",
}
EVENT_LABELS = {
    "EARNINGS": "财报公布",
    "EARNINGS_RELEASE": "财报公布",
    "REPORT": "财报公布",
}


def _decimal(value) -> Decimal | None:
    if value in (None, ""):
        return None
    try:
        return value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None


def _trim(number: str) -> str:
    if "." in number:
        number = number.rstrip("0").rstrip(".")
    return number or "0"


def format_number(value, places: int = 2) -> str:
    number = _decimal(value)
    if number is None:
        return "—"
    return _trim(f"{number:,.{places}f}")


def format_price(value) -> str:
    number = _decimal(value)
    if number is None:
        return "—"
    # 展示最多两位小数；仅对 <$1 的低价股保留 4 位（否则会退化成 $0.05 丢精度）。
    places = 4 if abs(number) < Decimal("1") else 2
    return f"${number:,.{places}f}"


def format_money(value) -> str:
    number = _decimal(value)
    return "—" if number is None else f"${number:,.2f}"


def format_quantity(value) -> str:
    return format_number(value, 4)


def format_percent(value, places: int = 1) -> str:
    number = _decimal(value)
    if number is None:
        return "—"
    return f"{number * 100:,.{places}f}%"


def status_label(value) -> str:
    value = str(value or "")
    return STATUS_LABELS.get(value, value or "—")


def action_label(value) -> str:
    value = str(value or "")
    return ACTION_LABELS.get(value, value or "—")


def trigger_label(value) -> str:
    value = str(value or "")
    return TRIGGER_LABELS.get(value, value or "—")


def match_label(value) -> str:
    value = str(value or "")
    return MATCH_LABELS.get(value, value or "—")


def action_status_label(value) -> str:
    value = str(value or "")
    return ACTION_STATUS_LABELS.get(value, value or "—")


def event_type_label(value) -> str:
    value = str(value or "")
    return EVENT_TYPE_LABELS.get(value, value or "—")


def version_label(value) -> str:
    value = str(value or "")
    return {"PUBLISHED": "正式版本", "DRAFT": "草稿", "LEGACY": "旧记录"}.get(
        value, value or "—")


def event_label(value) -> str:
    value = str(value or "")
    return EVENT_LABELS.get(value, value or "")


def parse_datetime(value: str | None, tz: ZoneInfo | None = None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(tz or timezone.utc)


def format_datetime(value: str | None, tz: ZoneInfo | None = None) -> str:
    parsed = parse_datetime(value, tz)
    if parsed is None:
        return "—"
    return f"{parsed.month}月{parsed.day}日 {parsed.hour:02d}:{parsed.minute:02d}"


def format_input_datetime(value: str | None, tz: ZoneInfo | None = None) -> str:
    parsed = parse_datetime(value, tz)
    return parsed.strftime("%Y-%m-%dT%H:%M") if parsed is not None else ""


def relative_time(value: str | None, now: datetime | None = None,
                  tz: ZoneInfo | None = None) -> str:
    parsed = parse_datetime(value, tz)
    if parsed is None:
        return "—"
    now_local = (now or datetime.now(timezone.utc))
    if now_local.tzinfo is None:
        now_local = now_local.replace(tzinfo=timezone.utc)
    now_local = now_local.astimezone(tz or timezone.utc)
    seconds = (parsed - now_local).total_seconds()
    if abs(seconds) < 60:
        return "刚刚"
    if seconds > 0 and seconds < 7 * 86400:
        days = int(seconds // 86400)
        if days:
            return f"{format_datetime(value, tz)} · {days}天后"
        hours = int(seconds // 3600)
        if hours:
            return f"{format_datetime(value, tz)} · {hours}小时后"
        return f"{format_datetime(value, tz)} · {int(seconds // 60)}分钟后"
    if -3600 < seconds < 0:
        return f"{int(abs(seconds) // 60)}分钟前"
    if parsed.date() == now_local.date():
        return f"今天{parsed.hour:02d}:{parsed.minute:02d}"
    if parsed.date().toordinal() == now_local.date().toordinal() - 1:
        return f"昨天 {parsed.hour:02d}:{parsed.minute:02d}"
    return format_datetime(value, tz)


def action_display(action: dict | None) -> str:
    if not action:
        return "—"
    amount = ""
    if action.get("quantity") not in (None, ""):
        amount = f"{format_quantity(action['quantity'])}股"
    elif action.get("percentage") not in (None, ""):
        amount = f"{format_percent(action['percentage'])}"
    else:
        amount = "数量待定"
    condition = event_label(action.get("event_condition")) or action.get("event_condition") or ""
    if action.get("price_min") not in (None, "") or action.get("price_max") not in (None, ""):
        condition = f"{format_price(action.get('price_min'))}–{format_price(action.get('price_max'))}"
    if action.get("additional_conditions"):
        condition = f"{condition}；{action['additional_conditions']}" if condition else action["additional_conditions"]
    tail = f" · {condition}" if condition else ""
    return f"{action_label(action.get('action_type'))}{amount}{tail}"
