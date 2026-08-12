"""富途 OpenD 行情适配器（仅行情，不接触交易接口）。

职责边界：
- 只导入并使用 ``OpenQuoteContext``；
- 持仓、成本、账户、订单、成交和 PnL 仍全部来自 IBKR；
- 连接失败、订阅失败、价格无效或时间戳过期时返回空结果，由调用方逐标的回退 IBKR。
"""
from __future__ import annotations

import math
import socket
import threading
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Callable

from .logging_setup import get_logger
from .market_hours import CLOSED, OVERNIGHT, POST, PRE, REGULAR, market_session
from .rules import PriceQuality

log = get_logger()

_SESSION_PRICE_FIELD = {
    PRE: ("pre_price", "PRE_MARKET"),
    REGULAR: ("last_price", "LAST"),
    POST: ("after_price", "AFTER_HOURS"),
    OVERNIGHT: ("overnight_price", "OVERNIGHT"),
}

# 富途当前账户实测可直接取得实时行情的市场代理。VIX/VXN/TNX 在该 OpenD
# 返回「未知股票」，因此不在这里用其它产品冒充，仍由既有 IBKR 日线提供。
MARKET_LIVE_SYMBOLS = ("QQQ", "SPY", "SOXX")


def _price(value: Any) -> Decimal | None:
    try:
        number = float(value)
        if math.isnan(number) or number <= 0:
            return None
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None


def _quote_time(value: Any, market_tz) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=market_tz)
    return parsed.astimezone(timezone.utc)


def quote_from_snapshot(row: dict, *, now: datetime, market_tz,
                        stale_seconds: int) -> dict | None:
    """把一行富途快照转换成项目统一报价；无效/过期即返回 None。"""
    session = market_session(now, market_tz)
    field, price_source = (("last_price", "CLOSE") if session == CLOSED
                           else _SESSION_PRICE_FIELD[session])
    px = _price(row.get(field))
    as_of = _quote_time(row.get("update_time"), market_tz)
    if px is None or as_of is None:
        return None
    age = max((now.astimezone(timezone.utc) - as_of).total_seconds(), 0)
    if session != CLOSED and age > stale_seconds:
        return None
    return {
        "price": px,
        "price_source": price_source,
        "quality": (PriceQuality.DELAYED_FROZEN.value if session == CLOSED
                    else PriceQuality.REALTIME.value),
        "as_of": as_of.isoformat(),
        "market_data_type": None,
        "source": "FUTU_OpenD",
        "requested_market_data_type": None,
        "effective_market_data_type": None,
        # 下游所有价格派生值都从同一行富途快照取，避免再混入 IBKR 行情。
        "previous_close": _price(row.get("prev_close_price")),
        "open": _price(row.get("open_price")),
        "high": _price(row.get("high_price")),
        "low": _price(row.get("low_price")),
        "volume": _price(row.get("volume")),
        "received_at": row.get("_received_at"),
    }


class FutuQuoteClient:
    """长连接富途 OpenD；异常时快速失败，让 IBKR 回退接管。"""

    def __init__(self, cfg, *, context_factory: Callable | None = None):
        self.cfg = cfg
        self._context_factory = context_factory
        self._ctx = None
        self._subscribed: set[str] = set()
        self._rows_by_code: dict[str, dict] = {}
        self._handler_installed = False
        self._last_reconnect_attempt = 0.0

    def _factory(self):
        if self._context_factory is not None:
            return self._context_factory
        from futu import OpenQuoteContext  # 延迟导入：未启用富途时不增加启动依赖
        return OpenQuoteContext

    def _reset(self) -> None:
        if self._ctx is not None:
            try:
                self._ctx.close()
            except Exception:  # noqa: BLE001
                pass
        self._ctx = None
        self._subscribed.clear()
        with self._lock:
            self._rows_by_code.clear()
        self._handler_installed = False

    def _ensure_context(self):
        if self._ctx is not None:
            return self._ctx
        # OpenQuoteContext 在端口不可达时会内部重试较久；先做短 TCP 探测，
        # 保证 OpenD 故障不会拖慢 IBKR 持仓同步。
        if self._context_factory is None:
            with socket.create_connection(
                    (self.cfg.futu_host, self.cfg.futu_port),
                    timeout=self.cfg.futu_connect_timeout):
                pass
        self._ctx = self._factory()(
            host=self.cfg.futu_host, port=self.cfg.futu_port)
        return self._ctx

    @property
    def _lock(self):
        # 延迟创建兼容用 __new__ 构造的轻量测试对象。
        lock = getattr(self, "_cache_lock", None)
        if lock is None:
            lock = self._cache_lock = threading.RLock()
        return lock

    @staticmethod
    def _records(data) -> list[dict]:
        if data is None:
            return []
        if isinstance(data, list):
            return [dict(r) for r in data]
        if isinstance(data, dict):
            return [dict(data)]
        to_dict = getattr(data, "to_dict", None)
        return to_dict(orient="records") if callable(to_dict) else []

    def _accept_rows(self, data, *, received_at: datetime | None = None) -> None:
        rows = self._records(data)
        received_dt = received_at or datetime.now(timezone.utc)
        received_iso = received_dt.isoformat()
        session = market_session(received_dt, self.cfg.market_tz)
        with self._lock:
            for row in rows:
                code = str(row.get("code") or "")
                if code.startswith("US."):
                    # snapshot 使用 update_time；QUOTE 推送使用 data_date/data_time。
                    # 富途扩展时段推送的 data_date/data_time 仍是上一个常规时段
                    # 成交时间，但 pre/after/overnight_price 已经是新价。此时用本次
                    # 实时推送的接收时间；盘中才使用交易所成交时间。
                    data_date, data_time = row.get("data_date"), row.get("data_time")
                    if session == REGULAR and data_date and data_time:
                        row["update_time"] = f"{data_date} {data_time}"
                    elif data_date and data_time:
                        row["update_time"] = received_iso
                    row["_received_at"] = received_iso
                    previous = self._rows_by_code.get(code, {})
                    # 推送包可能只有变更字段；保留初始 snapshot 的其它时段价格。
                    self._rows_by_code[code] = {**previous, **row}

    def _install_push_handler(self, ctx) -> None:
        if self._handler_installed:
            return
        try:
            from futu import RET_OK, StockQuoteHandlerBase
        except ImportError:
            return
        # OpenD 的真实上下文提供 set_handler；测试替身或精简上下文可能只支持
        # snapshot/subscribe。推送不可用时仍可安全使用快照预热，不应整条行情链失败。
        if not hasattr(ctx, "set_handler"):
            return
        owner = self

        class _Handler(StockQuoteHandlerBase):
            def on_recv_rsp(self, rsp_pb):
                ret, data = super().on_recv_rsp(rsp_pb)
                if ret == RET_OK:
                    owner._accept_rows(data)
                return ret, data

        ctx.set_handler(_Handler())
        self._handler_installed = True

    def _sync_subscriptions(self, ctx, codes: set[str]) -> bool:
        removed = self._subscribed - codes
        if removed:
            ret, msg = ctx.unsubscribe(sorted(removed), ["QUOTE"])
            if ret != 0:
                raise RuntimeError(f"富途取消订阅失败: {msg}")
            self._subscribed -= removed
            with self._lock:
                for code in removed:
                    self._rows_by_code.pop(code, None)
        added = codes - self._subscribed
        if added:
            ret, msg = ctx.subscribe(
                sorted(added), ["QUOTE"], subscribe_push=True,
                extended_time=True)
            if ret != 0:
                raise RuntimeError(f"富途订阅失败: {msg}")
            self._subscribed |= added
        return bool(added)

    def start(self, symbols: list[str]) -> bool:
        """建立推送订阅并用一次 snapshot 给缓存预热。"""
        codes = {f"US.{symbol}" for symbol in symbols if symbol}
        if not codes:
            return True
        try:
            ctx = self._ensure_context()
            self._install_push_handler(ctx)
            added = self._sync_subscriptions(ctx, codes)
            with self._lock:
                missing_cache = any(code not in self._rows_by_code for code in codes)
            if added or missing_cache:
                ret, data = ctx.get_market_snapshot(sorted(codes))
                if ret != 0:
                    raise RuntimeError(f"富途快照失败: {data}")
                self._accept_rows(data)
            return True
        except Exception as exc:  # noqa: BLE001
            log.warning("富途行情不可用，回退 IBKR：%s", type(exc).__name__)
            self._reset()
            return False

    def current(self, symbols: list[str], *, now: datetime | None = None,
                ensure_started: bool = True) -> dict[str, dict]:
        """读取推送缓存；不发网络请求，过期报价自然被过滤并触发 IBKR 回退。"""
        now = now or datetime.now(timezone.utc)
        if not symbols:
            return {}
        if ensure_started and not self.start(symbols):
            return {}
        with self._lock:
            rows = [dict(self._rows_by_code.get(f"US.{symbol}", {}))
                    for symbol in symbols]
        out: dict[str, dict] = {}
        for row in rows:
            code = str(row.get("code") or "")
            quote = quote_from_snapshot(
                row, now=now, market_tz=self.cfg.market_tz,
                stale_seconds=self.cfg.futu_quote_stale_seconds)
            if code.startswith("US.") and quote is not None:
                out[code[3:]] = quote
        # 推送静默中断通常不会抛异常，只表现为全部时间戳过期。有限频率重建
        # OpenD 上下文，恢复后自动从 IBKR 回退切回富途。
        if ensure_started and symbols and not out:
            current_mono = time.monotonic()
            if current_mono - self._last_reconnect_attempt >= 30:
                self._last_reconnect_attempt = current_mono
                self._reset()
                if self.start(symbols):
                    return self.current(symbols, now=now, ensure_started=False)
        return out

    def cached(self, symbols: list[str], *, now: datetime | None = None) -> dict[str, dict]:
        """Pure cache read used by the IBKR account pipeline.

        It must never establish a context, subscribe, request a snapshot, or
        reconnect.  Network maintenance belongs to the quote worker.
        """
        return self.current(symbols, now=now, ensure_started=False)

    def health(self, symbols: list[str], *, now: datetime | None = None) -> dict:
        now = now or datetime.now(timezone.utc)
        wanted = [s for s in symbols if s]
        quotes = self.cached(wanted, now=now)
        ages = []
        for quote in quotes.values():
            as_of = _quote_time(quote.get("as_of"), timezone.utc)
            if as_of is not None:
                ages.append(max((now - as_of).total_seconds(), 0))
        return {
            "source": "futu", "authority": "market_quote",
            "account_pnl_authoritative": False,
            "requested": len(wanted), "fresh": len(quotes),
            "coverage": (Decimal(len(quotes)) / Decimal(len(wanted))
                         if wanted else Decimal("1")),
            "max_age_seconds": max(ages) if ages else None,
            "subscription_status": "connected" if self._ctx is not None else "disconnected",
        }

    def fetch(self, symbols: list[str], *, now: datetime | None = None) -> dict[str, dict]:
        now = now or datetime.now(timezone.utc)
        if not symbols or market_session(now, self.cfg.market_tz) == CLOSED:
            return {}
        if not self.start(symbols):
            return {}
        return self.current(symbols, now=now, ensure_started=False)

    def history_quota(self) -> dict[str, int]:
        """读取历史K线股票额度；不返回账户或凭据内容。"""
        ctx = self._ensure_context()
        ret, data = ctx.get_history_kl_quota(get_detail=False)
        if ret != 0 or not isinstance(data, tuple) or len(data) < 2:
            raise RuntimeError(f"富途历史K线额度读取失败: {str(data)[:120]}")
        return {"used": int(data[0]), "remaining": int(data[1])}

    def fetch_5m_bars(self, symbol: str, start: datetime, end: datetime,
                      *, use_rth: bool = True) -> list[dict]:
        """分页读取富途美国股票5分钟历史K线。

        每个新股票会占用一份7天滚动历史K线额度；调用方必须用持久游标保证同一
        窗口不重复请求。只请求常规时段，来源与IBKR严格分列。
        """
        from futu import AuType, KLType
        ctx = self._ensure_context()
        page_key = None
        rows: dict[str, dict] = {}
        for _ in range(100):
            ret, data, next_key = ctx.request_history_kline(
                f"US.{symbol}", start=start.date().isoformat(), end=end.date().isoformat(),
                ktype=KLType.K_5M, autype=AuType.NONE, max_count=1000,
                page_req_key=page_key, extended_time=not use_rth)
            if ret != 0:
                raise RuntimeError(f"富途5分钟历史K线失败 {symbol}: {str(data)[:160]}")
            for row in self._records(data):
                raw_time = row.get("time_key") or row.get("time")
                if not raw_time:
                    continue
                try:
                    parsed = datetime.fromisoformat(str(raw_time))
                except ValueError:
                    continue
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=self.cfg.market_tz)
                # Futu K_5M 的 time_key 是区间结束时刻（首根09:35、末根16:00）；
                # 项目和IBKR统一以区间开始时刻作为 bar_start。
                bar_end = parsed.astimezone(timezone.utc)
                key = (bar_end - timedelta(minutes=5)).isoformat()
                rows[key] = {
                    "symbol": symbol, "bar_start": key,
                    "bar_end": bar_end.isoformat(),
                    "o": _price(row.get("open")), "h": _price(row.get("high")),
                    "l": _price(row.get("low")), "c": _price(row.get("close")),
                    "v": _price(row.get("volume")),
                    "session": "REGULAR" if use_rth else None,
                    "use_rth": use_rth, "source": "FUTU_historical_5m",
                }
            if not next_key:
                break
            page_key = next_key
        else:
            raise RuntimeError(f"富途历史K线分页异常 {symbol}: 超过100页")
        return [rows[key] for key in sorted(rows)
                if start.astimezone(timezone.utc).isoformat() <= key <=
                end.astimezone(timezone.utc).isoformat()]
