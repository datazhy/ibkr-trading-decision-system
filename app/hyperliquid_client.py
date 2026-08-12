"""Hyperliquid public Info/WS adapter.

只实现公开只读接口。所有金额和价格原样保留为字符串，消费层再转 Decimal。
"""
from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Callable

import httpx

INFO_URL = "https://api.hyperliquid.xyz/info"
WS_URL = "wss://api.hyperliquid.xyz/ws"


class HyperliquidApiError(RuntimeError):
    def __init__(self, message: str, *, code: str = "API_ERROR", payload: Any = None):
        super().__init__(message)
        self.code = code
        self.payload = payload


def as_decimal(value: Any) -> Decimal | None:
    if value in (None, ""):
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None


class WeightBudget:
    """简单滑动窗口令牌桶；预算由整个 HL 服务共享。"""

    def __init__(self, limit_per_minute: int = 600):
        self.limit = max(1, int(limit_per_minute))
        self._events: list[tuple[float, float, bool]] = []
        self._lock = threading.Lock()

    def _trim(self, now: float) -> None:
        self._events = [(ts, weight, backfill) for ts, weight, backfill in self._events
                        if now - ts < 60]

    def used(self) -> float:
        with self._lock:
            now = time.monotonic()
            self._trim(now)
            return sum(weight for _, weight, _ in self._events)

    def reserve(self, weight: float, *, backfill: bool = False,
                backfill_limit_pct: float = 0.20) -> bool:
        weight = max(0.0, float(weight))
        with self._lock:
            now = time.monotonic()
            self._trim(now)
            used = sum(w for _, w, _ in self._events)
            if used + weight > self.limit:
                return False
            backfill_used = sum(w for _, w, is_backfill in self._events if is_backfill)
            if backfill and backfill_used + weight > self.limit * max(0.0, backfill_limit_pct):
                return False
            self._events.append((now, weight, bool(backfill)))
            return True


@dataclass
class HyperliquidClient:
    base_url: str = INFO_URL
    timeout: float = 20.0
    budget: WeightBudget | None = None
    user_agent: str = "xiaoyu-hyperliquid-readonly/1"
    backfill_limit_pct: float = 0.20

    def __post_init__(self) -> None:
        self.budget = self.budget or WeightBudget()

    def _weight(self, request_type: str, *, expected_items: int = 0) -> int:
        if request_type in {"l2Book", "allMids", "clearinghouseState", "orderStatus",
                            "spotClearinghouseState", "exchangeStatus"}:
            base = 2
        elif request_type == "userRole":
            base = 60
        else:
            base = 20
        if request_type in {"recentTrades", "historicalOrders", "userFills",
                            "userFillsByTime", "fundingHistory", "userFunding",
                            "userNonFundingLedgerUpdates"}:
            base += (max(0, int(expected_items)) + 19) // 20
        if request_type == "candleSnapshot":
            base += (max(0, int(expected_items)) + 59) // 60
        return base

    def info(self, request_type: str, payload: dict[str, Any] | None = None,
             *, expected_items: int = 2000, backfill: bool = False) -> Any:
        body = {"type": request_type, **(payload or {})}
        weight = self._weight(request_type, expected_items=expected_items)
        if not self.budget or not self.budget.reserve(
                weight, backfill=backfill, backfill_limit_pct=self.backfill_limit_pct):
            raise HyperliquidApiError("Hyperliquid 内部限流预算不足", code="RATE_LIMIT_BUDGET")
        try:
            with httpx.Client(timeout=self.timeout, headers={
                    "Content-Type": "application/json", "User-Agent": self.user_agent}) as client:
                response = client.post(self.base_url, json=body)
            response.raise_for_status()
            data = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise HyperliquidApiError(f"Hyperliquid Info 请求失败: {type(exc).__name__}",
                                      code="HTTP_ERROR") from exc
        if isinstance(data, dict) and data.get("error"):
            raise HyperliquidApiError("Hyperliquid Info 返回错误", code="REMOTE_ERROR",
                                      payload=data)
        return data

    def perp_dexs(self) -> Any:
        return self.info("perpDexs", expected_items=0)

    def categories(self) -> dict[str, str]:
        data = self.info("perpCategories", expected_items=0)
        if not isinstance(data, list):
            raise HyperliquidApiError("perpCategories 响应格式不正确", code="SCHEMA_ERROR")
        return {str(row[0]): str(row[1]) for row in data
                if isinstance(row, (list, tuple)) and len(row) >= 2}

    def meta_and_contexts(self, dex: str = "xyz") -> tuple[dict, list[dict]]:
        data = self.info("metaAndAssetCtxs", {"dex": dex}, expected_items=0)
        if not isinstance(data, list) or len(data) != 2 or not isinstance(data[0], dict):
            raise HyperliquidApiError("metaAndAssetCtxs 响应格式不正确", code="SCHEMA_ERROR")
        meta = data[0]
        contexts = data[1] if isinstance(data[1], list) else []
        return meta, contexts

    def recent_trades(self, coin: str) -> list[dict]:
        data = self.info("recentTrades", {"coin": coin}, expected_items=10)
        return data if isinstance(data, list) else []

    def user_fills_by_time(self, address: str, start_time: int, end_time: int | None = None,
                           *, backfill: bool = False) -> list[dict]:
        payload: dict[str, Any] = {"user": address, "startTime": int(start_time),
                                   "aggregateByTime": False}
        if end_time is not None:
            payload["endTime"] = int(end_time)
        data = self.info("userFillsByTime", payload, expected_items=2000,
                         backfill=backfill)
        if not isinstance(data, list):
            raise HyperliquidApiError("userFillsByTime 响应格式不正确", code="SCHEMA_ERROR")
        return data

    def user_fills_window(self, address: str, start_time: int, end_time: int,
                          *, max_pages: int = 5) -> tuple[list[dict], bool]:
        """分页读取最近 10k fills；返回 (rows, incomplete)。

        API 时间边界 inclusive，下一页故意从 last timestamp 重读并用
        ``(coin,time,tid)`` 去重；若同毫秒页无法前进，标记缺口而不静默跳过。
        """
        cursor = int(start_time)
        seen: set[tuple[str, str, str]] = set()
        rows: list[dict] = []
        incomplete = False
        for _ in range(max(1, int(max_pages))):
            try:
                page = self.user_fills_by_time(address, cursor, end_time, backfill=True)
            except HyperliquidApiError as exc:
                # 同一轮只允许使用回补子预算；已有完整页必须先落库并明确
                # 标 INCOMPLETE，不能因为下一页预算不足丢弃已取得的数据。
                if rows and exc.code == "RATE_LIMIT_BUDGET":
                    incomplete = True
                    break
                raise
            if not page:
                break
            before = len(seen)
            for item in page:
                key = (str(item.get("coin") or ""), str(item.get("time") or ""), str(item.get("tid") or ""))
                if key[0] and key[1] and key[2] and key not in seen:
                    seen.add(key); rows.append(item)
            max_time = max((int(item.get("time")) for item in page if str(item.get("time", "")).isdigit()),
                           default=cursor)
            if len(page) < 2000:
                break
            if max_time < cursor or (max_time == cursor and len(seen) == before):
                incomplete = True
                break
            cursor = max_time
        else:
            incomplete = True
        return rows, incomplete

    def clearinghouse_state(self, address: str, dex: str = "xyz") -> dict:
        data = self.info("clearinghouseState", {"user": address, "dex": dex}, expected_items=0)
        if not isinstance(data, dict):
            raise HyperliquidApiError("clearinghouseState 响应格式不正确", code="SCHEMA_ERROR")
        return data

    def user_funding(self, address: str, start_time: int, end_time: int | None = None,
                     *, dex: str = "xyz", backfill: bool = False) -> list[dict]:
        payload: dict[str, Any] = {"user": address, "startTime": int(start_time)}
        if end_time is not None:
            payload["endTime"] = int(end_time)
        # The current API uses dex for HIP-3 funding history even though old docs
        # show a first-DEX example; keep it explicit and let the server validate.
        payload["dex"] = dex
        data = self.info("userFunding", payload, expected_items=2000, backfill=backfill)
        return data if isinstance(data, list) else []

    def user_ledger_updates(self, address: str, start_time: int,
                            end_time: int | None = None, *, backfill: bool = False) -> list[dict]:
        payload: dict[str, Any] = {"user": address, "startTime": int(start_time)}
        if end_time is not None:
            payload["endTime"] = int(end_time)
        data = self.info("userNonFundingLedgerUpdates", payload, expected_items=2000,
                         backfill=backfill)
        return data if isinstance(data, list) else []

    def funding_history(self, coin: str, start_time: int, end_time: int | None = None,
                        *, backfill: bool = False) -> list[dict]:
        """读取单个perp的市场资金费历史；官方单页当前最多500行。"""
        payload: dict[str, Any] = {"coin": str(coin), "startTime": int(start_time)}
        if end_time is not None:
            payload["endTime"] = int(end_time)
        data = self.info("fundingHistory", payload, expected_items=500,
                         backfill=backfill)
        if not isinstance(data, list):
            raise HyperliquidApiError("fundingHistory 响应格式不正确", code="SCHEMA_ERROR")
        return data

    def candle_snapshot(self, coin: str, interval: str, start_time: int,
                        end_time: int, *, backfill: bool = False) -> list[dict]:
        data = self.info("candleSnapshot", {"req": {"coin": coin, "interval": interval,
                                                      "startTime": int(start_time),
                                                      "endTime": int(end_time)}},
                         expected_items=5000, backfill=backfill)
        return data if isinstance(data, list) else []


def normalize_trade(trade: dict[str, Any]) -> dict[str, Any] | None:
    """把 WS/REST 公共成交统一为可去重结构。"""
    coin = trade.get("coin")
    tid = trade.get("tid")
    event_time = trade.get("time") or trade.get("block_time")
    if not coin or tid is None or event_time is None:
        return None
    users = trade.get("users") or [None, None]
    if not isinstance(users, (list, tuple)) or len(users) < 2:
        users = [None, None]
    side = str(trade.get("side") or "").upper()
    aggressor = users[0] if side == "B" else users[1] if side == "A" else None
    maker = users[1] if side == "B" else users[0] if side == "A" else None
    return {"coin": str(coin), "block_time": str(event_time), "tid": str(tid),
            "side": side or None, "px": trade.get("px"), "sz": trade.get("sz"),
            "buyer": users[0], "seller": users[1], "aggressor": aggressor,
            "maker": maker, "raw": trade}


def normalize_fill(address: str, fill: dict[str, Any]) -> dict[str, Any] | None:
    coin = fill.get("coin")
    event_time = fill.get("time")
    tid = fill.get("tid")
    if not coin or event_time is None or tid is None:
        return None
    return {"address": address.lower(), "coin": str(coin), "event_time": str(event_time),
            "tid": str(tid), "px": fill.get("px"), "sz": fill.get("sz"),
            "side": fill.get("side"), "direction": fill.get("dir"),
            "start_position": fill.get("startPosition"),
            "closed_pnl": fill.get("closedPnl"), "fee": fill.get("fee"),
            "crossed": bool(fill.get("crossed")), "raw": fill}
