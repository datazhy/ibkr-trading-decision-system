"""Hyperliquid 合约到 IBKR 普通股的三重映射门禁。"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any

from .instruments import ASSET_EQUITY, asset_class_of


def _d(v: Any) -> Decimal | None:
    try:
        return None if v in (None, "") else Decimal(str(v))
    except (InvalidOperation, TypeError, ValueError):
        return None


def evaluate_mapping(*, coin: str, canonical_symbol: str, category: str | None,
                     universe: dict[str, Any] | None, ibkr: dict[str, Any] | None,
                     chain_closes: list[Any] | None = None,
                     cash_closes: list[Any] | None = None,
                     denylist: set[str] | None = None,
                     max_deviation: Decimal = Decimal("0.05")) -> dict[str, Any]:
    symbol = canonical_symbol.upper()
    reasons: list[str] = []
    denylist = {str(x).upper() for x in (denylist or set())}
    if str(category or "").lower() not in {"stock", "stocks"}:
        reasons.append("官方分类不是 stocks")
    if universe and universe.get("isDelisted"):
        reasons.append("合约已下架")
    if symbol in denylist:
        reasons.append("命中显式 denylist")
    ibkr = ibkr or {}
    asset_class = ibkr.get("asset_class") or asset_class_of(ibkr.get("sec_type"), ibkr.get("stock_type"))
    if asset_class != ASSET_EQUITY:
        reasons.append("IBKR 不是美国普通股")
    if str(ibkr.get("currency") or "USD").upper() != "USD":
        reasons.append("币种不是 USD")
    if not ibkr.get("conid"):
        reasons.append("缺少唯一 IBKR conid")
    if str(ibkr.get("exchange") or "").upper() != "SMART":
        reasons.append("未确认 SMART exchange")
    if not ibkr.get("primary_exchange"):
        reasons.append("缺少 primary exchange 消歧")
    chain, cash = [_d(x) for x in (chain_closes or [])], [_d(x) for x in (cash_closes or [])]
    chain = [x for x in chain if x and x > 0]
    cash = [x for x in cash if x and x > 0]
    deviations = []
    if chain and cash:
        for a, b in zip(chain[-3:], cash[-3:]):
            deviations.append(abs(a / b - 1))
        if len(deviations) < 3:
            reasons.append("不足 3 个完整交易日偏差")
        elif max(deviations) > max_deviation:
            reasons.append("连续 3 日链上/正股偏差超过门槛")
    else:
        reasons.append("缺少连续 3 日链上或正股收盘价")
    status = "READY" if not reasons else "BLOCKED"
    if reasons and any("偏差" in reason or "缺少连续" in reason for reason in reasons):
        status = "SUSPECT" if not any("不是" in reason or "denylist" in reason for reason in reasons) else "BLOCKED"
    return {"coin": coin, "canonical_symbol": symbol, "status": status,
            "reason": "；".join(reasons) or None,
            "max_rth_deviation_ratio": str(max(deviations)) if deviations else None,
            "asset_class": asset_class, "ibkr": ibkr}


def save_mapping(db: Any, result: dict[str, Any], *, checked_at: str,
                 source: str = "IBKR") -> None:
    row = result.get("ibkr") or {}
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO hl_asset_mappings(coin,canonical_symbol,conid,sec_type,stock_type,"
            "primary_exchange,currency,status,reason,max_rth_deviation_ratio,checked_at,source) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(coin) DO UPDATE SET canonical_symbol=excluded.canonical_symbol,"
            "conid=excluded.conid,sec_type=excluded.sec_type,stock_type=excluded.stock_type,"
            "primary_exchange=excluded.primary_exchange,currency=excluded.currency,status=excluded.status,"
            "reason=excluded.reason,max_rth_deviation_ratio=excluded.max_rth_deviation_ratio,"
            "checked_at=excluded.checked_at,source=excluded.source",
            (result["coin"], result["canonical_symbol"], row.get("conid"), row.get("sec_type"),
             row.get("stock_type"), row.get("primary_exchange"), row.get("currency", "USD"),
             result["status"], result.get("reason"), result.get("max_rth_deviation_ratio"),
             checked_at, source))
