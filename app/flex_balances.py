"""Statement of Funds 余额标记识别（纯函数）。

真实报表的 52 条 `StatementOfFundsLine` 里有 4 条不是经济事件，而是**余额锚点**：
期初/期末的现金余额。它们是分币种现金对账的权威起点与终点 —— 有了它们，就不再
需要账户快照覆盖整个 90 天报表区间才能对账。

**识别不能只看 activityCode 为空。** 空 activityCode 也可能是券商没填的真实活动
（调整、公司行动等）。所以要多维印证：

    activityCode 为空
  + balance 有值
  + transactionID / tradeID 为空      ← 经济事件必有流水号
  + amount 为 0 或空                  ← 余额锚点不产生现金变动
  + debit / credit 均为空
  + symbol / conid 为空

以上全部成立才认定是余额标记。任一条不成立 —— 尤其是**有流水号或金额非零** ——
一律当作经济事件走正常流程，宁可漏认一条余额，也不能把一笔真实活动吞掉。

类型判定按 activityDescription 的关键词，再由 levelOfDetail 决定是"币种级"还是
"账户级"。判不出来就是 `unknown_balance_marker`，**绝不猜** —— 把一条期末余额
猜成期初，会让整段对账的方向反过来。
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from decimal import Decimal
from typing import Iterable

from . import cashflow_model as cm
from . import flex_xml as fx
from .flex import parse_flex_datetime

# 余额类型
BAL_OPENING = "opening_balance"
BAL_CLOSING = "closing_balance"
BAL_CCY_OPENING = "currency_opening_balance"
BAL_CCY_CLOSING = "currency_closing_balance"
BAL_UNKNOWN = "unknown_balance_marker"

BALANCE_TYPES = (BAL_OPENING, BAL_CLOSING, BAL_CCY_OPENING, BAL_CCY_CLOSING,
                 BAL_UNKNOWN)

OPENING_TYPES = (BAL_OPENING, BAL_CCY_OPENING)
CLOSING_TYPES = (BAL_CLOSING, BAL_CCY_CLOSING)

#: 描述里的期初关键词。中英文都列上 —— IBKR 的本地化版本会返回中文描述。
_OPENING_HINTS = ("opening", "beginning", "starting", "start of",
                  "期初", "起始")
_CLOSING_HINTS = ("closing", "ending", "end of", "final",
                  "期末", "结束")

#: levelOfDetail = Currency 表示这是**某个币种**的余额，而不是账户合计。
#: 真实报表 4 条全是 Currency 级 —— 这正是必须分币种对账的直接证据。
_CURRENCY_LEVELS = ("currency", "currencysummary")

QUALITY_OK = "ok"
QUALITY_AMBIGUOUS = "ambiguous"


@dataclass
class BalanceMarker:
    """一条余额锚点。**绝不进入 cash_flows。**"""
    account_id: str | None
    balance_date: str
    balance_type: str
    currency: str
    balance: Decimal
    fx_rate_to_base: Decimal | None = None
    balance_base: Decimal | None = None
    level_of_detail: str | None = None
    description: str | None = None
    node_index: int | None = None
    quality_status: str = QUALITY_OK
    raw: dict | None = None

    @property
    def is_opening(self) -> bool:
        return self.balance_type in OPENING_TYPES

    @property
    def is_closing(self) -> bool:
        return self.balance_type in CLOSING_TYPES

    def balance_id(self, report_hash: str) -> str:
        """幂等键：同一份报表 + 账户 + 币种 + 类型 + 日期唯一。"""
        raw = (f"{report_hash}|{self.account_id}|{self.currency}|"
               f"{self.balance_type}|{self.balance_date}")
        return hashlib.sha256(raw.encode()).hexdigest()[:24]


def looks_like_balance_marker(attrib: dict) -> bool:
    """是否为余额标记。多维印证，任一条不成立即否。

    **宁可漏认一条余额，也不能把一笔真实活动当成余额吞掉** ——
    前者只是对账少一个锚点，后者是数据丢失。
    """
    code = (attrib.get("activityCode") or "").strip()
    if code:
        return False
    if (attrib.get("balance") or "") == "":
        return False
    # 经济事件必有流水号；余额锚点没有
    if (attrib.get("transactionID") or "").strip():
        return False
    if (attrib.get("tradeID") or "").strip():
        return False
    # 余额锚点不产生现金变动
    amount = cm.d(attrib.get("amount"))
    if amount is not None and amount != 0:
        return False
    if (attrib.get("debit") or "").strip() or (attrib.get("credit") or "").strip():
        return False
    # 余额是账户级的，不挂在具体标的上
    if (attrib.get("symbol") or "").strip() or (attrib.get("conid") or "").strip():
        return False
    return True


def classify_balance(attrib: dict, *, report_from: str | None = None,
                     report_to: str | None = None,
                     node_index: int | None = None,
                     total_nodes: int | None = None) -> tuple[str, str]:
    """判定余额类型。返回 (balance_type, quality)。

    优先用描述关键词；描述判不出来时才看日期是否落在区间端点；
    仍判不出来就是 unknown_balance_marker —— 猜错方向会让对账整个反过来。
    """
    desc = (attrib.get("activityDescription") or attrib.get("description") or "")
    low = desc.lower()
    level = (attrib.get("levelOfDetail") or "").strip().lower()
    per_currency = level in _CURRENCY_LEVELS

    kind = None
    if any(h in low for h in _OPENING_HINTS):
        kind = "opening"
    elif any(h in low for h in _CLOSING_HINTS):
        kind = "closing"

    quality = QUALITY_OK
    if kind is None:
        # 描述认不出来：用日期端点做次级证据，但标记为存疑
        day = _norm_day(attrib.get("date") or attrib.get("reportDate"))
        if day and report_from and day == _norm_day(report_from):
            kind, quality = "opening", QUALITY_AMBIGUOUS
        elif day and report_to and day == _norm_day(report_to):
            kind, quality = "closing", QUALITY_AMBIGUOUS
        elif node_index == 0:
            kind, quality = "opening", QUALITY_AMBIGUOUS
        elif total_nodes and node_index == total_nodes - 1:
            kind, quality = "closing", QUALITY_AMBIGUOUS

    if kind is None:
        return BAL_UNKNOWN, QUALITY_AMBIGUOUS
    if kind == "opening":
        return (BAL_CCY_OPENING if per_currency else BAL_OPENING), quality
    return (BAL_CCY_CLOSING if per_currency else BAL_CLOSING), quality


def _norm_day(v) -> str | None:
    if not v:
        return None
    s = str(v)[:10]
    if len(s) == 8 and "-" not in s:
        return f"{s[:4]}-{s[4:6]}-{s[6:8]}"
    return s


def parse_balance_markers(xml: str, tz, *, report_from: str | None = None,
                          report_to: str | None = None) -> list[BalanceMarker]:
    """从报表里取出全部余额锚点。"""
    root = fx.parse_xml(xml)
    lines = fx.find_descendants_by_local_name(root, "StatementOfFundsLine")
    total = len(lines)
    out: list[BalanceMarker] = []
    for i, el in enumerate(lines):
        a = el.attrib
        if not looks_like_balance_marker(a):
            continue
        btype, quality = classify_balance(
            a, report_from=report_from, report_to=report_to,
            node_index=i, total_nodes=total)
        day = _norm_day(a.get("date") or a.get("reportDate"))
        if not day:
            continue
        bal = cm.d(a.get("balance"))
        if bal is None:
            continue
        rate = cm.d(a.get("fxRateToBase"))
        cur = (a.get("currency") or "").upper() or "USD"
        if rate is None and cur == "USD":
            rate = Decimal("1")
        out.append(BalanceMarker(
            account_id=a.get("accountId"),
            balance_date=day, balance_type=btype, currency=cur,
            balance=bal, fx_rate_to_base=rate,
            balance_base=(bal * rate) if rate is not None else None,
            level_of_detail=a.get("levelOfDetail"),
            description=a.get("activityDescription") or a.get("description"),
            node_index=i, quality_status=quality, raw=dict(a)))
    return out


def by_currency(markers: Iterable[BalanceMarker]) -> dict[str, dict]:
    """按币种归集期初与期末。

    同一币种出现多条同类型余额时**取日期最早的作期初、最晚的作期末** ——
    报表可能包含中间日的余额行，用节点顺序会拿错。
    """
    out: dict[str, dict] = {}
    for m in markers:
        slot = out.setdefault(m.currency, {"opening": None, "closing": None,
                                           "unknown": []})
        if m.balance_type == BAL_UNKNOWN:
            slot["unknown"].append(m)
            continue
        key = "opening" if m.is_opening else "closing"
        cur = slot[key]
        if cur is None:
            slot[key] = m
        elif key == "opening" and m.balance_date < cur.balance_date:
            slot[key] = m
        elif key == "closing" and m.balance_date > cur.balance_date:
            slot[key] = m
    return out


def summarize(markers: Iterable[BalanceMarker]) -> dict:
    """结构摘要，供同步日志与页面展示（不含金额）。"""
    ms = list(markers)
    counts: dict[str, int] = {}
    for m in ms:
        counts[m.balance_type] = counts.get(m.balance_type, 0) + 1
    return {
        "total": len(ms),
        "by_type": counts,
        "currencies": sorted({m.currency for m in ms}),
        "ambiguous": sum(1 for m in ms if m.quality_status == QUALITY_AMBIGUOUS),
        "unknown": counts.get(BAL_UNKNOWN, 0),
    }
