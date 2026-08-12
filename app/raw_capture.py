"""IBKR 原始字段留存（供审计与排障）。

为什么要存原始值：标准化过程是有损的 —— 一旦「净资产」算错了，只看标准字段
永远查不出是取错了 tag 还是换算错了。留一份原值，问题就能定位到具体环节。

三条纪律：
1. **脱敏**。原始响应可能带账户标识、会话信息；凭证虽不流经这里，仍按不可信处理。
2. **失败不拖垮快照**。序列化异常只丢弃 raw_json，绝不让一次账户同步失败 ——
   审计数据的重要性低于账户数据本身。
3. **不给前端默认返回**。raw_json 只经受控接口按需取用。
"""
from __future__ import annotations

import json
from decimal import Decimal
from typing import Any

#: 命中即整条丢弃的键（大小写不敏感的子串匹配）。
#: 宁可多丢几个无害字段，也不能漏出一个凭证。
_SENSITIVE = (
    "password", "passwd", "secret", "token", "apikey", "api_key",
    "auth", "credential", "session", "cookie", "totp", "otp",
    "signature", "privatekey", "private_key",
)

#: 需要打码而非丢弃的键（保留可读性，用于对账）
_MASK = ("account", "acctid", "accountid", "acctcode", "username", "userid")

_MAX_LEN = 20000          # 单条 raw_json 上限，防止异常大响应撑爆库


def _is_sensitive(key: str) -> bool:
    k = key.lower()
    return any(s in k for s in _SENSITIVE)


def _needs_mask(key: str) -> bool:
    k = key.lower()
    return any(s in k for s in _MASK)


def mask_value(v: Any) -> str:
    s = str(v)
    if len(s) <= 4:
        return "***"
    return s[:2] + "***" + s[-2:]


def _clean(v: Any, depth: int = 0) -> Any:
    """递归脱敏。深度受限，避免异常结构导致栈溢出。"""
    if depth > 6:
        return "<深度超限>"
    if isinstance(v, dict):
        out = {}
        for k, val in v.items():
            ks = str(k)
            if _is_sensitive(ks):
                continue                       # 敏感键整条丢弃，连键名都不留值
            out[ks] = mask_value(val) if _needs_mask(ks) else _clean(val, depth + 1)
        return out
    if isinstance(v, (list, tuple)):
        return [_clean(x, depth + 1) for x in v[:200]]
    if isinstance(v, Decimal):
        return str(v)                          # Decimal 不能直接 JSON 序列化
    if isinstance(v, (str, int, float, bool)) or v is None:
        return v
    return str(v)                              # 其它类型退化为字符串


def to_raw_json(payload: Any) -> str | None:
    """脱敏并序列化。任何异常都返回 None —— 审计数据不得拖垮账户快照。"""
    try:
        s = json.dumps(_clean(payload), ensure_ascii=False, default=str)
        if len(s) > _MAX_LEN:
            s = json.dumps({"_truncated": True, "_original_len": len(s),
                            "_head": s[:_MAX_LEN]}, ensure_ascii=False)
        return s
    except Exception:  # noqa: BLE001 — 故意吞掉：raw_json 是可选的审计信息
        return None


def account_raw(rows: Any) -> str | None:
    """IBKR accountSummary/accountValues -> 脱敏 raw_json。"""
    items = []
    for r in rows or []:
        try:
            items.append({"tag": getattr(r, "tag", None), "value": getattr(r, "value", None),
                          "currency": getattr(r, "currency", None)})
        except Exception:  # noqa: BLE001
            continue
    return to_raw_json({"source": "IBKR_accountSummary", "values": items})


def position_raw(item: Any) -> str | None:
    """IBKR PortfolioItem -> 脱敏 raw_json。"""
    try:
        c = getattr(item, "contract", None)
        return to_raw_json({
            "source": "IBKR_portfolio",
            "contract": {
                "symbol": getattr(c, "symbol", None), "secType": getattr(c, "secType", None),
                "conId": getattr(c, "conId", None), "exchange": getattr(c, "exchange", None),
                "primaryExchange": getattr(c, "primaryExchange", None),
                "currency": getattr(c, "currency", None),
            },
            "position": getattr(item, "position", None),
            "marketPrice": getattr(item, "marketPrice", None),
            "marketValue": getattr(item, "marketValue", None),
            "averageCost": getattr(item, "averageCost", None),
            "unrealizedPNL": getattr(item, "unrealizedPNL", None),
            "realizedPNL": getattr(item, "realizedPNL", None),
        })
    except Exception:  # noqa: BLE001
        return None
