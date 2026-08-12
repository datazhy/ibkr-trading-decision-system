"""展示层格式化：金额/百分比/时间/行情质量标签/新鲜度。\n\n页面时间一律北京时间（DISPLAY_TZ）；数据层仍是 UTC + 美东交易日。"""
from __future__ import annotations

import csv
import io
import json
import re
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

from . import pnl as pnl_calc
from . import reconcile
from .config import Config
from .counting import load_decisions, trading_counts
from .decisions import decision_key
from .decisions import (
    classification_complete as classification_complete_fn,
    group_decisions, kind_summary as kind_summary_fn,
)
from .db import Database, to_decimal
from .sectors import load_sector_map

# 行情质量 -> (展示措辞, 是否可称「现价」, 说明)
# 用户口径：只有 last/有效延迟行情能叫「现价」；close 回退一律叫「收盘价」。
# ---- 四、风险可信度 ----
# 建议止损/单笔风险/参考持仓上限都由 ATR 推出，其可信度取决于日线根数与行情质量。
# 一个基于 10 根日线、降级 ATR9 算出的止损位，不该和 90 根日线的止损位摆在一起
# 让人以为同样可靠。
CONF_HIGH = "HIGH"
CONF_MEDIUM = "MEDIUM"
CONF_LOW = "LOW"
CONF_UNAVAILABLE = "UNAVAILABLE"

CONF_LABEL = {CONF_HIGH: "高", CONF_MEDIUM: "中", CONF_LOW: "低", CONF_UNAVAILABLE: "不可用"}


def stop_confidence(stop_row, price, price_quality: str | None) -> dict:
    """建议止损/单笔风险的可信度分级 + 降级原因。

    HIGH        日线充足（ATR14）且有可用价格
    MEDIUM      日线充足，但价格只是收盘冻结价 / 行情过期
    LOW         日线不足，ATR 已降级（新上市标的）
    UNAVAILABLE 没有止损位或没有价格 —— 风险数字根本算不出
    """
    if stop_row is None or price is None:
        return {"level": CONF_UNAVAILABLE, "label": CONF_LABEL[CONF_UNAVAILABLE],
                "reasons": ["无建议止损位或无有效价格，单笔风险无法估算"],
                "is_estimate": False}
    reasons: list[str] = []
    level = CONF_HIGH
    if not stop_row["data_sufficient"]:
        level = CONF_LOW
        bars, period = stop_row["bars_used"], stop_row["atr_period"]
        reasons.append(f"仅 {bars} 根日线，降级使用 ATR{period}")
    q = price_quality or "MISSING"
    if q in ("DELAYED_FROZEN", "FROZEN"):
        reasons.append("价格为收盘冻结价，非实时")
        if level == CONF_HIGH:
            level = CONF_MEDIUM
    elif q == "STALE":
        reasons.append("行情时间戳过期")
        if level == CONF_HIGH:
            level = CONF_MEDIUM
    elif q == "DELAYED":
        reasons.append("价格约延迟 15 分钟")
    return {"level": level, "label": CONF_LABEL[level], "reasons": reasons,
            # 非 HIGH 一律算估算值：数字仍展示，但不能被读成精确结论
            "is_estimate": level != CONF_HIGH}


#: 行情状态的人话版本（状态栏用）。枚举名留在详情层。
_QUOTE_PLAIN = {
    "REALTIME": "实时行情",
    "REALTIME_NONCONSOLIDATED": "实时行情（非综合）",
    "DELAYED": "延迟约15分钟",
    "DELAYED_FROZEN": "收盘价（非实时）",
    "FROZEN": "行情已冻结",
    "STALE": "行情已过期",
    "MISSING": "未取到行情",
    "INVALID": "行情异常",
}

QUALITY_LABEL = {
    "REALTIME": ("实时", True, ""),
    "REALTIME_NONCONSOLIDATED": ("实时", True, "非综合行情"),
    "DELAYED": ("延迟", True, "约15分钟延迟"),
    "DELAYED_FROZEN": ("收盘价", False, "收盘冻结价"),
    "FROZEN": ("冻结", False, "行情冻结/停牌"),
    "STALE": ("过期", False, "行情时间戳过期"),
    "MISSING": ("无行情", False, "未取到行情"),
    "INVALID": ("异常", False, "行情无效"),
}


#: 页面展示时区：全站北京时间。数据层仍是 UTC + 美东交易日，两者互不干扰。
DISPLAY_TZ = ZoneInfo("Asia/Shanghai")


def _account(cfg: Config, db: Database) -> str:
    """当前账户号。放在最底层模块：状态页与数据页都要用，避免互相 import。"""
    bal = db.latest_balances()
    if bal and bal["account"]:
        return bal["account"]
    return cfg.ib_account or "DEFAULT"


def money(v) -> str:
    if v is None or v == "":
        return "-"
    try:
        return f"{Decimal(str(v)):,.2f}"
    except Exception:  # noqa: BLE001 —— 非数值原样返回，绝不因格式化崩页面
        return str(v)


def signed_money(v) -> str:
    if v is None or v == "":
        return "-"
    try:
        d = Decimal(str(v))
    except Exception:  # noqa: BLE001 —— 非数值原样返回，绝不因格式化崩页面
        return str(v)
    return f"{'+' if d >= 0 else ''}{d:,.2f}"


def rel_time(then_iso: str | None, now: datetime) -> str:
    """相对时间：'35秒前' / '4分钟前' / '2小时前'。"""
    if not then_iso:
        return "无数据"
    try:
        age = (now - datetime.fromisoformat(then_iso)).total_seconds()
    except (TypeError, ValueError):
        return "时间无效"
    if age < 0:
        return "刚刚"
    if age < 60:
        return f"{int(age)}秒前"
    if age < 3600:
        return f"{int(age // 60)}分钟前"
    if age < 86400:
        return f"{int(age // 3600)}小时前"
    return f"{int(age // 86400)}天前"


def abs_time(ts_iso: str | None, tz=None) -> str:
    """页面时间一律北京时间。

    数据层照旧用 UTC 存、用美东交易日归档（那是市场的口径，不能动）；
    但**展示**统一到用户所在时区，且不加「北京时间」四个字 —— 页面只有一种时区，
    不需要每个时间戳都自我说明。tz 参数保留是为了兼容既有调用点，实际忽略。
    """
    if not ts_iso:
        return "—"
    try:
        return (datetime.fromisoformat(ts_iso).astimezone(DISPLAY_TZ)
                .strftime("%Y-%m-%d %H:%M"))
    except (TypeError, ValueError):
        return ts_iso


def freshness(then_iso: str | None, now: datetime, *, warn_after: float,
              crit_after: float) -> str:
    """数据新鲜度分级：ok / warn / crit。

    过期的数据绝不能继续显示成正常 —— 这是「看板在骗人」和「看板在报警」的分界。
    """
    if not then_iso:
        return "crit"
    try:
        age = (now - datetime.fromisoformat(then_iso)).total_seconds()
    except (TypeError, ValueError):
        return "crit"
    if age >= crit_after:
        return "crit"
    if age >= warn_after:
        return "warn"
    return "ok"


def _stat(label: str, ts_iso: str | None, now: datetime, tz, *,
          warn_after: float, crit_after: float, extra: str = "") -> dict:
    return {
        "label": label,
        "abs": abs_time(ts_iso, tz),
        "rel": rel_time(ts_iso, now),
        "css": freshness(ts_iso, now, warn_after=warn_after, crit_after=crit_after),
        "extra": extra,
    }



def pct(v) -> str:
    if v is None or v == "":
        return "-"
    try:
        return f"{Decimal(str(v)) * 100:.1f}%"
    except Exception:  # noqa: BLE001 —— 非数值原样返回，绝不因格式化崩页面
        return str(v)


def num(v) -> str:
    """任意数值 → 最多两位小数、去尾零的展示串。

    兜底用：任何可能把原始 Decimal/float 直接渲染出去的地方套上它，
    杜绝「2.970835190693689776637365969」这类机器精度泄漏到页面。
    """
    if v is None or v == "":
        return "—"
    try:
        d = Decimal(str(v)).quantize(Decimal("0.01"))
    except Exception:  # noqa: BLE001 —— 非数值原样返回，不影响文本字段
        return str(v)
    s = f"{d:,.2f}"
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return s


_LONG_DEC_RE = re.compile(r"-?\d+\.\d{3,}")


def clean_decimals(text):
    """把任意字符串里的长小数就地收敛到最多两位。

    兜底用：引擎/AI 生成、已落库的叙事串里常把原始 Decimal 写进正文，
    展示时用它扫一遍，杜绝「当前 2.970835190693…」这类机器精度。
    """
    if not isinstance(text, str):
        return text

    def _round(m):
        try:
            d = Decimal(m.group(0)).quantize(Decimal("0.01"))
        except Exception:  # noqa: BLE001
            return m.group(0)
        s = f"{d:.2f}"
        return s.rstrip("0").rstrip(".") if "." in s else s

    return _LONG_DEC_RE.sub(_round, text)


def _et_day_start_utc(now: datetime | None, tz, et_date: str | None = None) -> str:
    """某美东日历日 00:00 对应的 UTC ISO。"""
    d = et_date or now.astimezone(tz).date().isoformat()
    return (datetime.fromisoformat(f"{d}T00:00:00").replace(tzinfo=tz)
            .astimezone(timezone.utc).isoformat())


def _et_month_start_utc(now: datetime, tz) -> str:
    et = now.astimezone(tz)
    return et.replace(day=1, hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc).isoformat()


def _et_date(ts_iso: str, tz) -> str:
    try:
        return datetime.fromisoformat(ts_iso).astimezone(tz).strftime("%Y-%m-%d")
    except ValueError:
        return ts_iso[:10]
