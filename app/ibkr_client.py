"""IBKR 适配器：连接 IB Gateway（ib_async），只读模式。

只读保证（双保险）：
  1) ib.connect(readonly=True) —— API 层拒绝一切下单调用；
  2) gateway 容器配置 READ_ONLY_API=yes —— 券商网关层再锁一道。

解析器（parse_ib_*）为纯函数、duck-typing（只取字段），可用 SimpleNamespace 测试。

字段映射说明：
  - margin_balance ← TotalCashValue：IBKR 无独立 margin balance 字段，
    现金为负即动用融资，R3 语义等价。
  - realized ← CommissionReport.realizedPNL：精确单笔已实现盈亏（R6 不再估算）。
  - orders 用 permId 作主键（跨会话稳定；orderId 仅在 clientId 内唯一）。
"""
from __future__ import annotations

import json
import math
import copy
import time
import threading
from collections import deque
from datetime import datetime, timezone, timedelta, date as date_t
from decimal import Decimal
from dataclasses import dataclass
from typing import Any, Sequence

from .config import Config
from .errors import BrokerApiError
from .logging_setup import get_logger
from . import raw_capture
from .pnl import clean_pnl
from .rules import PriceQuality
from .futu_quotes import MARKET_LIVE_SYMBOLS
from .pnl_reliability import PnLSubscription, validate_callback
from .position_pnl import revalue_position, valid_price

log = get_logger()


# 富途 OpenD 不识别这些指数；概览盘中值由现有 IBKR 只读长连接订阅。
# US10Y 在系统内使用统一代号，IBKR 对应 CBOE TNX（收益率 ×10）。
IBKR_LIVE_MARKET_INDEXES = {
    "VIX": ("VIX", "CBOE", "USD"),
    "VXN": ("VXN", "CBOE", "USD"),
    "US10Y": ("TNX", "CBOE", "USD"),
}


def _d(v: Any) -> Decimal:
    if v is None:
        return Decimal("0")
    return Decimal(str(v))


def _valid_px(x: Any) -> bool:
    """价格有效：非 None、非 NaN、> 0。"""
    if x is None:
        return False
    try:
        f = float(x)
    except (TypeError, ValueError):
        return False
    return not math.isnan(f) and f > 0


def _is_halted(x: Any) -> bool:
    """IBKR halted: 0=否，1/2=是，nan=未知。

    注意 bool(float('nan')) is True —— 直接 bool() 会把「未知」误判成停牌，
    进而把正常延迟行情强制降级为 FROZEN、使 R7 永不触发。故用 >0 比较（nan>0 为 False）。
    """
    try:
        return float(x) > 0
    except (TypeError, ValueError):
        return False


# marketDataType: 1=Live, 2=Frozen, 3=Delayed, 4=Delayed-frozen
# 本账户免费行情为「非综合实时」，故 md_type=1 记为 REALTIME_NONCONSOLIDATED；
# 综合实时需付费订阅（本账户未订阅），实际 API 侧只能拿到延迟(3)。
_MDT_QUALITY = {1: PriceQuality.REALTIME_NONCONSOLIDATED, 3: PriceQuality.DELAYED,
                2: PriceQuality.FROZEN, 4: PriceQuality.DELAYED_FROZEN}

# 价格来源（决定质量与展示措辞：LAST/MID 叫「现价」，CLOSE 只能叫「收盘价」）
PRICE_SOURCE_LAST = "LAST"
PRICE_SOURCE_MID = "BID_ASK_MID"
PRICE_SOURCE_CLOSE = "CLOSE"
PRICE_SOURCE_NONE = "NONE"


def _has_usable(quotes: dict) -> bool:
    """这一轮有没有拿到**任何**可用价格。

    回退判据刻意只看结果，不看错误码：未订阅实时行情时 IBKR 回的 10089 是异步
    error 事件，`reqMktData` 不抛异常，ticker 只是不填充。收盘价（CLOSE）也算
    可用 —— 盘后本来就只有收盘价，不该因此触发回退。
    """
    return any(q.get("price") is not None for q in (quotes or {}).values())


@dataclass(frozen=True)
class QuoteInfo:
    price: Decimal | None
    price_source: str
    quality: str
    as_of: str | None          # CLOSE 回退时为 None —— 收盘价没有可信时点
    market_data_type: int | None


def classify_quote(*, md_type, last, close, bid, ask, tick_time, halted,
                   now: datetime, stale_seconds: int = 90) -> QuoteInfo:
    """纯函数：把一条 ticker 归类为 QuoteInfo。

    价格来源优先级：last > bid/ask 中点 > close（收盘回退）。

    **来源决定质量**：一旦回退到 close，质量一律标 *_FROZEN，并且**不参与** tick
    新鲜度判定。原因是 IBKR 盘后仍把 ticker.time 刷成当前时刻（实测 05:11 的
    ticker.time 就是 05:11），若拿它判新鲜度，会把昨天的收盘价当成刚刚的实时价，
    使 R7 对着一个不动的收盘价发 WARN。收盘价的 as_of 因此写 None。
    """
    mdt = _MDT_QUALITY.get(md_type, PriceQuality.MISSING)

    if _valid_px(last):
        price, src = _d(last), PRICE_SOURCE_LAST
    elif _valid_px(bid) and _valid_px(ask):
        price, src = (_d(bid) + _d(ask)) / 2, PRICE_SOURCE_MID
    elif _valid_px(close):
        # 盘后：last=-1（IBKR 哨兵）、bid/ask=nan，而 close 有效。用收盘价，
        # 但只能作「收盘冻结价」，不触发 R7。
        q = (PriceQuality.DELAYED_FROZEN if md_type in (3, 4) else PriceQuality.FROZEN)
        return QuoteInfo(_d(close), PRICE_SOURCE_CLOSE, q.value, None, md_type)
    else:
        return QuoteInfo(None, PRICE_SOURCE_NONE, PriceQuality.INVALID.value, None, md_type)

    as_of = _ts_iso(tick_time) if tick_time else None
    quality = mdt
    if _is_halted(halted):
        quality = PriceQuality.FROZEN
    # 过期判定（对实时/非综合实时/延迟收紧；冻结本就不触发）
    elif tick_time is not None and quality in (
            PriceQuality.REALTIME, PriceQuality.REALTIME_NONCONSOLIDATED, PriceQuality.DELAYED):
        t = tick_time if isinstance(tick_time, datetime) else None
        if t is not None:
            if t.tzinfo is None:
                t = t.replace(tzinfo=timezone.utc)
            if (now - t).total_seconds() > stale_seconds:
                quality = PriceQuality.STALE
    return QuoteInfo(price, src, quality.value, as_of, md_type)


def _ts_iso(t: Any) -> str:
    if isinstance(t, datetime):
        if t.tzinfo is None:
            t = t.replace(tzinfo=timezone.utc)
        return t.astimezone(timezone.utc).isoformat()
    return datetime.now(timezone.utc).isoformat()


# ----------------------- 纯函数解析器 -----------------------

def parse_ib_portfolio(items: Sequence[Any]) -> list[dict]:
    """PortfolioItem -> positions 行。position 自带方向（空头为负）。"""
    out = []
    for it in items:
        symbol = getattr(it.contract, "symbol", None)
        sec_type = getattr(it.contract, "secType", "STK")
        if not symbol or sec_type not in ("STK", "ETF"):
            continue  # v1 只看股票/ETF
        qty = _d(it.position)
        if qty == 0:
            continue
        out.append({
            "symbol": symbol,
            "qty": qty,
            "avg_cost": _d(it.averageCost),
            "market_value": _d(it.marketValue),
            "upl": _d(it.unrealizedPNL),
            "conid": getattr(it.contract, "conId", None),
        })
    return out


def parse_ib_account_summary(rows: Sequence[Any], currency: str = "USD") -> dict:
    """AccountValue 列表 -> balances。只取基准货币行。"""
    vals: dict[str, Decimal] = {}
    for r in rows:
        if getattr(r, "currency", "") not in ("", currency):
            continue
        try:
            vals[r.tag] = _d(r.value)
        except Exception:  # noqa: BLE001 — 非数值 tag（如 AccountType）跳过
            continue

    def pick(*tags: str) -> Decimal:
        for t in tags:
            if t in vals:
                return vals[t]
        return Decimal("0")

    def has(*tags: str) -> Decimal | None:
        """与 pick 的区别：取不到返回 None 而不是 0。

        金融字段里 0 和「没取到」是两回事 —— 保证金占用 0（无杠杆）与保证金字段
        缺失（数据不全）会导出完全不同的风控结论，绝不能都记成 0。
        """
        for t in tags:
            if t in vals:
                return vals[t]
        return None

    cash = pick("TotalCashValue", "CashBalance")
    return {
        "equity": pick("NetLiquidation", "NetLiquidationByCurrency"),
        "cash": cash,
        # IBKR 无 margin balance 字段；现金为负=融资，R3 语义等价
        "margin_balance": cash,
        "buying_power": pick("BuyingPower", "AvailableFunds"),
        # ---- 账户快照层补充字段（缺失即 None，不用 0 顶替）----
        "total_cash": has("TotalCashValue", "CashBalance"),
        "available_funds": has("AvailableFunds"),
        "excess_liquidity": has("ExcessLiquidity"),
        "init_margin": has("FullInitMarginReq", "InitMarginReq"),
        "maint_margin": has("FullMaintMarginReq", "MaintMarginReq"),
        "positions_value": has("GrossPositionValue"),
        "currency": currency,
    }


_SIDE_MAP = {"BUY": "BUY", "SELL": "SELL", "BOT": "BUY", "SLD": "SELL"}


def parse_ib_trade(trade: Any) -> dict:
    """Trade(order+orderStatus) -> orders 行。permId 作主键。"""
    order = trade.order
    status = getattr(trade.orderStatus, "status", "Unknown")
    symbol = getattr(trade.contract, "symbol", None)
    ts = None
    log_entries = getattr(trade, "log", None) or []
    if log_entries:
        ts = getattr(log_entries[0], "time", None)
    price = getattr(order, "lmtPrice", None)
    # IBKR 用极大值/0 表示市价单无限价
    if price is not None and (price <= 0 or price >= 1e300):
        price = None
    return {
        "order_id": str(order.permId or order.orderId),
        "ts": _ts_iso(ts),
        "symbol": symbol,
        "side": _SIDE_MAP.get(str(order.action).upper(), str(order.action)),
        "order_type": getattr(order, "orderType", None),
        "qty": _d(order.totalQuantity),
        "price": _d(price) if price is not None else None,
        "status": str(status).upper(),
        "raw_json": json.dumps({
            "permId": order.permId, "orderId": order.orderId,
            "action": order.action, "orderType": order.orderType,
            "totalQuantity": str(order.totalQuantity), "status": str(status),
            "symbol": symbol,
        }, ensure_ascii=False, default=str),
    }


def parse_ib_fill(fill: Any) -> dict:
    """Fill(execution+commissionReport) -> transactions 行（含精确 realized）。"""
    ex = fill.execution
    cr = getattr(fill, "commissionReport", None)
    symbol = getattr(fill.contract, "symbol", None)
    side = _SIDE_MAP.get(str(ex.side).upper(), str(ex.side))
    qty = _d(ex.shares)
    price = _d(ex.price)
    gross = price * qty
    amount = gross if side == "SELL" else -gross  # 卖出入金为正
    commission = _d(getattr(cr, "commission", 0)) if cr else Decimal("0")
    realized_raw = getattr(cr, "realizedPNL", None) if cr else None
    # IBKR 对无平仓的成交给 0 或极大哨兵值；仅记录真实平仓盈亏
    realized = None
    if realized_raw is not None and abs(float(realized_raw)) < 1e300 and float(realized_raw) != 0:
        realized = _d(realized_raw)
    return {
        "txn_id": str(ex.execId),
        "ts": _ts_iso(getattr(fill, "time", None) or getattr(ex, "time", None)),
        "symbol": symbol,
        "action": side,
        "qty": qty,
        "price": price,
        "fees": abs(commission),
        "amount": amount,
        "realized": realized,
        "raw_json": json.dumps({
            "execId": ex.execId, "side": ex.side, "shares": str(ex.shares),
            "price": str(ex.price), "symbol": symbol,
            "realizedPNL": str(realized_raw) if realized_raw is not None else None,
        }, ensure_ascii=False, default=str),
    }


def _finite(x: Any) -> bool:
    """有限数值：非 None、非 NaN、非 IBKR 哨兵（1.79e308）。0 视为有效。"""
    if x is None:
        return False
    try:
        f = float(x)
    except (TypeError, ValueError):
        return False
    return not math.isnan(f) and abs(f) < 1e300


def split_exec_id(exec_id: str) -> tuple[str, int]:
    """IBKR execId 形如 0000e0d5.68b3b1a2.01.01，末段为修正序号（correction）。

    返回 (correction_root, correction_seq)。同一笔执行的修正版共享 root、seq 递增。
    """
    parts = str(exec_id).rsplit(".", 1)
    if len(parts) == 2 and parts[1].isdigit():
        return parts[0], int(parts[1])
    return str(exec_id), 0


def parse_ib_execution(fill: Any) -> dict:
    """Fill(contract+execution+commissionReport) -> executions 行（纯函数）。

    commission/realizedPNL 允许后到：未到时 data_status=PENDING_COMMISSION 且
    realized_pnl=None，R6 不得据此判定（避免按错误金额触发）。
    """
    ex = fill.execution
    c = fill.contract
    cr = getattr(fill, "commissionReport", None)
    exec_id = str(ex.execId)
    root, seq = split_exec_id(exec_id)

    # commissionReport 到达的判据：其 execId 被填充（未到时为默认空对象）
    has_comm = bool(cr is not None and getattr(cr, "execId", ""))
    commission = comm_ccy = realized = None
    if has_comm:
        if _finite(getattr(cr, "commission", None)):
            commission = abs(_d(cr.commission))          # 0 佣金有效
        comm_ccy = getattr(cr, "currency", None) or None
        rp = getattr(cr, "realizedPNL", None)
        if _finite(rp) and float(rp) != 0:               # 0/哨兵=非平仓，无 realized
            realized = _d(rp)

    return {
        "exec_id": exec_id, "correction_root": root, "correction_seq": seq,
        "perm_id": getattr(ex, "permId", None), "order_id": getattr(ex, "orderId", None),
        "client_id": getattr(ex, "clientId", None),
        "conid": getattr(c, "conId", None), "symbol": getattr(c, "symbol", None),
        "sec_type": getattr(c, "secType", None), "exchange": getattr(ex, "exchange", None),
        "currency": getattr(c, "currency", None),
        "side": _SIDE_MAP.get(str(ex.side).upper(), str(ex.side)),
        "shares": _d(ex.shares), "price": _d(ex.price),
        "cumulative_qty": _d(getattr(ex, "cumQty", None)),
        "average_price": _d(getattr(ex, "avgPrice", None)),
        "execution_time": _ts_iso(getattr(fill, "time", None) or getattr(ex, "time", None)),
        "commission": commission, "commission_currency": comm_ccy, "realized_pnl": realized,
        "liquidation": int(getattr(ex, "liquidation", 0) or 0),
        "order_ref": getattr(ex, "orderRef", None) or None,
        "last_liquidity": getattr(ex, "lastLiquidity", None),
        "source": "reqExecutions",
        "data_status": "READY" if has_comm else "PENDING_COMMISSION",
        "raw_json": json.dumps({
            "execId": exec_id, "permId": getattr(ex, "permId", None),
            "side": ex.side, "shares": str(ex.shares), "price": str(ex.price),
            "cumQty": str(getattr(ex, "cumQty", "")), "symbol": getattr(c, "symbol", None),
            "conId": getattr(c, "conId", None),
            "realizedPNL": str(getattr(cr, "realizedPNL", None)) if has_comm else None,
        }, ensure_ascii=False, default=str),
    }


def parse_ib_bars(bars: Sequence[Any]) -> list[dict]:
    out = []
    for b in bars:
        d = b.date
        if isinstance(d, datetime):
            d = d.date()
        if isinstance(d, date_t):
            d = d.isoformat()
        out.append({
            "date": str(d),
            "o": _d(b.open), "h": _d(b.high), "l": _d(b.low), "c": _d(b.close),
            "v": int(b.volume or 0),
        })
    return out


# ----------------------- 客户端 -----------------------

def _ensure_loop() -> None:
    """确保当前线程有 event loop。

    ib_async 的 IB 对象在创建时绑定当时线程的 loop，其同步方法（含 ib.run）通过
    asyncio.get_event_loop() 取当前线程 loop。APScheduler 在线程池中执行 job，
    非主线程默认没有 loop → RuntimeError: There is no current event loop。
    配合 scheduler 的单线程执行器，可保证 IB 始终在同一线程/同一 loop 上使用。
    """
    import asyncio
    try:
        asyncio.get_event_loop()
    except RuntimeError:
        asyncio.set_event_loop(asyncio.new_event_loop())


class IBKRClient:
    """连接 IB Gateway。惰性连接 + 断线自动重连（每次调用前确保连接）。

    线程约定：所有 IB 调用必须在同一线程（scheduler 使用单线程执行器），
    因为 ib_async 非线程安全且 IB 对象与 event loop 绑定。
    """

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self._ib = None        # 主连接（持仓/余额/行情/日线）
        self._ib_exec = None   # executions 专用连接（clientId=0）
        #: 实时行情探测的冷却截止（monotonic 秒）。None = 下一轮就试。
        #: 订阅未生效时用它避免每轮白等；重连时清空，因为行情权限是
        #: **建立会话时**读取的，新会话值得再试一次。
        self._rt_probe_until = None
        self._futu_quote_client = None
        if cfg.futu_quote_enabled:
            from .futu_quotes import FutuQuoteClient
            self._futu_quote_client = FutuQuoteClient(cfg)
        # 实时层只在所属线程访问这些对象；不跨线程共享 IB/Trade/Fill 实例。
        self._live_started = False
        self._live_account = None
        self._live_account_pnl = None
        self._live_position_pnl: dict[int, Any] = {}
        self._live_times: dict[str, str | None] = {
            "positions_as_of": None, "account_as_of": None, "pnl_as_of": None,
            "orders_as_of": None, "executions_as_of": None,
            "reconciled_at": None,
        }
        self._live_handlers: list[tuple[Any, Any]] = []
        self._last_market_quotes: dict[str, dict] = {}
        self._live_market_tickers: dict[str, Any] = {}
        self._live_market_contracts: dict[str, Any] = {}
        self._live_market_subscription_generation = 0
        # 最近一次有效持仓报价。富途/IBKR 本轮短暂无报价时只读回退，并保留原时点。
        self._last_position_quotes: dict[str, dict] = {}
        self._connection_generation = 0
        self._subscription_generation = 0
        self._pnl_meta_by_object: dict[int, PnLSubscription] = {}
        self._pnl_active_by_key: dict[tuple[str, str, int | None], set[int]] = {}
        self._pnl_active_by_request_id: dict[str, set[int]] = {}
        self._pnl_callback_audit = deque(maxlen=10000)
        self._pnl_subscription_audit = deque(maxlen=2000)
        self._pnl_audit_lock = threading.RLock()
        self._futu_universe = set(MARKET_LIVE_SYMBOLS)
        self._futu_universe_lock = threading.RLock()

    def _ensure_connected(self):
        from ib_async import IB  # 延迟导入
        _ensure_loop()
        if self._ib is None:
            self._ib = IB()
        if not self._ib.isConnected():
            try:
                self._ib.connect(
                    host=self.cfg.ib_host, port=self.cfg.ib_port,
                    clientId=self.cfg.ib_client_id, timeout=15,
                    readonly=True,  # 只读：API 层拒绝下单
                    account=self.cfg.ib_account or "",
                )
                self._connection_generation += 1
                if getattr(self, "_live_started", False):
                    # 会话断开后服务端订阅全部失效；对象仍在但不再会更新，必须重订。
                    self._live_account_pnl = None
                    self._live_position_pnl.clear()
                    # Keep old object metadata so a late callback can be identified as
                    # an old connection generation instead of merely "unknown".
                    getattr(self, "_pnl_active_by_key", {}).clear()
                    getattr(self, "_pnl_active_by_request_id", {}).clear()
                    self._record_subscription_event(
                        event_type="reconnect", request_id=None,
                        account=self._live_account, model_code="", conid=None,
                        subscription_generation=None)
                log.info("已连接 IB Gateway %s:%s (readonly)", self.cfg.ib_host, self.cfg.ib_port)
            except Exception as exc:
                raise BrokerApiError(f"无法连接 IB Gateway {self.cfg.ib_host}:{self.cfg.ib_port}: {exc}") from exc
        return self._ib

    def _ensure_exec_connected(self):
        """executions 专用连接。

        IBKR 规定：只有 clientId=0 的 API 连接能收到 TWS/手机上手动下的订单与成交。
        主连接（clientId=17）看不到手动交易，故 executions/completedOrders 走此连接。
        仍为 readonly，只读。
        """
        from ib_async import IB  # 延迟导入
        _ensure_loop()
        if self._ib_exec is None:
            self._ib_exec = IB()
        if not self._ib_exec.isConnected():
            try:
                self._ib_exec.connect(
                    host=self.cfg.ib_host, port=self.cfg.ib_port,
                    clientId=self.cfg.ib_exec_client_id, timeout=15,
                    readonly=True, account=self.cfg.ib_account or "",
                )
                log.info("已连接 IB Gateway（executions 专用 clientId=%s, readonly）",
                         self.cfg.ib_exec_client_id)
            except Exception as exc:
                raise BrokerApiError(
                    f"executions 连接失败（clientId={self.cfg.ib_exec_client_id}）: {exc}") from exc
        return self._ib_exec

    # ---- Broker 接口 ----

    def fetch_executions(self, account_hash: str) -> list[dict]:
        """当日 executions 增量（含 commissionReport）。仅当日，非历史源。

        数据源要点（实测）：
          - reqExecutions() 返回的 Fill.commissionReport 是**空对象**（execId=''）；
          - commissionReport 是**异步事件**，只会填充到 ib.fills() 会话缓存。
        故：reqExecutions 触发/补拉 → 等事件 → 读 ib.fills() 取带佣金的版本。
        超时必备：req* 等 *End 事件可能永久阻塞，会拖垮 45s 同步循环。
        """
        import asyncio
        from ib_async import ExecutionFilter
        ib = self._ensure_exec_connected()
        try:
            ib.run(asyncio.wait_for(
                ib.reqExecutionsAsync(ExecutionFilter()), self.cfg.ib_req_timeout))
        except Exception as exc:
            raise BrokerApiError(f"IBKR executions 拉取失败/超时: {type(exc).__name__} {exc}") from exc
        ib.sleep(self.cfg.ib_commission_wait)   # 等 commissionReport 事件到达
        fills = ib.fills()                      # 会话缓存：commission/realizedPNL 已填充
        rows = []
        for f in fills:
            if account_hash and getattr(f.execution, "acctNumber", "") not in ("", account_hash):
                continue
            rows.append(parse_ib_execution(f))
        return rows

    def fetch_completed_orders(self, account_hash: str) -> list[dict]:
        """当日已完成/撤销/拒绝订单。apiOnly=False → 含手动下单。带超时（同上）。"""
        import asyncio
        ib = self._ensure_exec_connected()
        try:
            trades = ib.run(asyncio.wait_for(
                ib.reqCompletedOrdersAsync(apiOnly=False), self.cfg.ib_req_timeout))
        except Exception as exc:
            raise BrokerApiError(
                f"IBKR completedOrders 拉取失败/超时: {type(exc).__name__} {exc}") from exc
        rows = []
        for t in trades:
            if account_hash and getattr(t.order, "account", "") not in ("", account_hash):
                continue
            rows.append(parse_ib_trade(t))
        return rows

    def resolve_account_hash(self, account_number: str | None) -> str:
        ib = self._ensure_connected()
        accounts = ib.managedAccounts()
        if not accounts:
            raise BrokerApiError("IB Gateway 未返回任何账户（登录未完成？）")
        if account_number:
            if account_number in accounts:
                return account_number
            raise BrokerApiError(f"配置的 IB_ACCOUNT 不在 gateway 账户列表中: {accounts}")
        if len(accounts) > 1:
            log.warning("IBKR 返回多个账户 %s，默认取第一个；可用 IB_ACCOUNT 指定", accounts)
        return accounts[0]

    def fetch_positions_and_balances(self, account_hash: str) -> tuple[list[dict], dict]:
        ib = self._ensure_connected()
        try:
            items = ib.portfolio(account_hash)
            summary = ib.accountSummary(account_hash)
        except Exception as exc:
            raise BrokerApiError(f"IBKR 持仓/余额拉取失败: {exc}") from exc
        positions = parse_ib_portfolio(items)
        balances = parse_ib_account_summary(summary)
        # 原始字段留存（脱敏、失败即丢弃，绝不影响账户数据本身）
        balances["raw_json"] = raw_capture.account_raw(summary)
        _raw_by_symbol = {}
        for it in items:
            sym = getattr(getattr(it, "contract", None), "symbol", None)
            if sym:
                _raw_by_symbol[sym] = raw_capture.position_raw(it)
        for p in positions:
            p["raw_json"] = _raw_by_symbol.get(p["symbol"])
            p.setdefault("currency", getattr(
                getattr(next((i for i in items
                              if getattr(getattr(i, "contract", None), "symbol", None)
                              == p["symbol"]), None), "contract", None), "currency", None))
        balances["_ready"] = bool(summary) and balances["equity"] > 0
        # 组合订阅在 gateway 每日 23:45 ET relogin 后可能失效：portfolio() 返回空，
        # 但 accountSummary（净资产）仍正常。连接层 isConnected() 仍为 True，
        # _ensure_connected 不会重连 → 组合永远空、持仓快照静默停更（实测停 12h，
        # watchdog 只查余额/规则新鲜度，察觉不到）。这里主动识别并重连自愈。
        if not items and balances.get("equity", 0) and balances["equity"] > 0:
            items = self._recover_portfolio(account_hash, balances["equity"])
            positions = parse_ib_portfolio(items)
        # 行情质量：对股票/ETF 持仓请求实时报价并分级
        contracts = [
            it.contract for it in items
            if getattr(getattr(it, "contract", None), "symbol", None)
            and getattr(it, "position", 0) != 0
            and getattr(it.contract, "secType", "") in ("STK", "ETF")
        ]
        self.update_futu_universe([getattr(c, "symbol", None) for c in contracts])
        quotes = self._fetch_position_quotes(contracts) if contracts else {}
        if not hasattr(self, "_last_position_quotes"):
            self._last_position_quotes = {}
        for p in positions:
            q = quotes.get(p["symbol"])
            fresh_quote = bool(q and valid_price(q.get("price")) is not None)
            if not fresh_quote:
                q = None
            if q is None:
                q = self._last_position_quotes.get(p["symbol"])
                if q is not None and valid_price(q.get("price")) is not None:
                    q = dict(q)
                    q["quality"] = PriceQuality.STALE.value
                else:
                    q = None
            if q:
                if fresh_quote:
                    self._last_position_quotes[p["symbol"]] = dict(q)
                p["price"] = q["price"]
                p["price_source"] = q["price_source"]
                p["price_quality"] = q["quality"]
                p["quote_as_of"] = q["as_of"]
                p["market_data_type"] = q["market_data_type"]
                p["source"] = q["source"]
                # 请求的类型与实际生效的类型。两者不一致 = 订阅没生效，
                # 这是判断「免费非综合实时是否已到位」的直接凭据。
                p["requested_market_data_type"] = q.get("requested_market_data_type")
                p["effective_market_data_type"] = q.get("effective_market_data_type")
                p["previous_close"] = q.get("previous_close")
                if str(q.get("source") or "").startswith("FUTU"):
                    p["futu_market_value"] = p["qty"] * q["price"]
                    p["futu_unrealized_estimate"] = (
                        p["qty"] * (q["price"] - p["avg_cost"]))
            else:
                p["price"] = None
                p["price_source"] = PRICE_SOURCE_NONE
                p["price_quality"] = PriceQuality.MISSING.value
                p["source"] = "IBKR_reqMktData"
            p["market_value_source"] = "IBKR_portfolio"
            p["upl_source"] = "IBKR_portfolio"
            if q:
                revalue_position(p, price=q.get("price"), source=q.get("source"))
        # 账户级 + 逐持仓 PnL 同批取：账户 DailyPnL 与逐持仓合计必须是同一时刻的，
        # 否则对账差异反映的是取数时点差，而不是数据问题。
        bundle = self.fetch_pnl_bundle(
            account_hash, [p.get("conid") for p in positions if p.get("conid")])
        for p in positions:
            v = bundle["positions"].get(p.get("conid"))
            if v:
                if v["day_pnl"] is not None:
                    p["day_pnl"] = v["day_pnl"]
                    p["day_pnl_source"] = "IBKR_reqPnLSingle"
                if v["realized_pnl"] is not None:
                    p["realized_pnl"] = v["realized_pnl"]
                    p["realized_pnl_source"] = "IBKR_reqPnLSingle"
        balances["account"] = account_hash
        balances["source"] = "IBKR_accountSummary"
        balances["positions_value_source"] = "IBKR_accountSummary"
        balances["_account_pnl"] = bundle["account"]   # 与本批持仓同一时刻
        return positions, balances

    # ---- 长连接实时层 -------------------------------------------------

    def _mark_live(self, key: str, *_args) -> None:
        self._live_times[key] = datetime.now(timezone.utc).isoformat()

    def _bind_live_event(self, event, key: str) -> None:
        def handler(*args):
            self._mark_live(key, *args)
        event += handler
        self._live_handlers.append((event, handler))

    def _record_subscription_event(self, *, event_type: str,
                                   request_id: str | None, account: str | None,
                                   model_code: str, conid: int | None,
                                   subscription_generation: int | None) -> None:
        row = {
            "event_type": event_type,
            "occurred_at": datetime.now(timezone.utc).isoformat(),
            "request_id": request_id, "account": account,
            "model_code": model_code, "conid": conid,
            "connection_generation": getattr(self, "_connection_generation", 0),
            "subscription_generation": subscription_generation,
        }
        lock = getattr(self, "_pnl_audit_lock", None)
        if lock is None:
            lock = self._pnl_audit_lock = threading.RLock()
        with lock:
            queue = getattr(self, "_pnl_subscription_audit", None)
            if queue is None:
                queue = self._pnl_subscription_audit = deque(maxlen=2000)
            queue.append(row)

    def _register_pnl_subscription(self, obj, *, account: str, model_code: str = "",
                                   conid: int | None = None) -> PnLSubscription:
        self._subscription_generation = getattr(
            self, "_subscription_generation", 0) + 1
        broker_req_id = getattr(obj, "reqId", None)
        request_id = (str(broker_req_id) if broker_req_id not in (None, "")
                      else f"local:{self._connection_generation}:"
                           f"{self._subscription_generation}")
        meta = PnLSubscription(
            request_id=request_id, account=account, model_code=model_code,
            conid=conid,
            connection_generation=getattr(self, "_connection_generation", 0),
            subscription_generation=self._subscription_generation,
            subscribed_at=datetime.now(timezone.utc).isoformat())
        object_id = id(obj)
        lock = getattr(self, "_pnl_audit_lock", None)
        if lock is None:
            lock = self._pnl_audit_lock = threading.RLock()
        with lock:
            meta_by_object = getattr(self, "_pnl_meta_by_object", None)
            if meta_by_object is None:
                meta_by_object = self._pnl_meta_by_object = {}
            active_by_key = getattr(self, "_pnl_active_by_key", None)
            if active_by_key is None:
                active_by_key = self._pnl_active_by_key = {}
            active_by_request = getattr(self, "_pnl_active_by_request_id", None)
            if active_by_request is None:
                active_by_request = self._pnl_active_by_request_id = {}
            meta_by_object[object_id] = meta
            active_by_key.setdefault((account, model_code, conid), set()).add(object_id)
            active_by_request.setdefault(request_id, set()).add(object_id)
        self._record_subscription_event(
            event_type=("resubscribed" if getattr(self, "_live_started", False)
                        and getattr(self, "_connection_generation", 0) > 1
                        else "subscribed"),
            request_id=request_id, account=account,
            model_code=model_code, conid=conid,
            subscription_generation=meta.subscription_generation)
        return meta

    def _unregister_pnl_subscription(self, obj) -> PnLSubscription | None:
        if obj is None:
            return None
        object_id = id(obj)
        lock = getattr(self, "_pnl_audit_lock", None)
        if lock is None:
            lock = self._pnl_audit_lock = threading.RLock()
        with lock:
            meta = getattr(self, "_pnl_meta_by_object", {}).pop(object_id, None)
            if meta is None:
                return None
            key = (meta.account, meta.model_code, meta.conid)
            active_by_key = getattr(self, "_pnl_active_by_key", {})
            active_by_request = getattr(self, "_pnl_active_by_request_id", {})
            for mapping, entry in ((active_by_key, key),
                                   (active_by_request, meta.request_id)):
                object_ids = mapping.get(entry)
                if object_ids is not None:
                    object_ids.discard(object_id)
                    if not object_ids:
                        mapping.pop(entry, None)
            return meta

    def _record_pnl_callback(self, callback_type: str, obj) -> None:
        now = datetime.now(timezone.utc)
        meta = getattr(self, "_pnl_meta_by_object", {}).get(id(obj))
        account = getattr(obj, "account", None)
        model_code = getattr(obj, "modelCode", None)
        broker_req_id = getattr(obj, "reqId", None)
        decision = validate_callback(
            meta, current_account=(getattr(self, "_live_account", None)
                                   or (meta.account if meta else None)
                                   or account or ""),
            current_connection_generation=getattr(self, "_connection_generation", 0),
            callback_account=account, callback_model_code=model_code,
            callback_request_id=(str(broker_req_id)
                                 if broker_req_id not in (None, "") else None))
        reasons = list(decision.reasons)
        if meta is not None:
            lock = getattr(self, "_pnl_audit_lock", None)
            if lock is None:
                lock = self._pnl_audit_lock = threading.RLock()
            with lock:
                object_id = id(obj)
                key = (meta.account, meta.model_code, meta.conid)
                if len(getattr(self, "_pnl_active_by_key", {}).get(key, ())) > 1:
                    reasons.append("DUPLICATE_ACTIVE_SUBSCRIPTION")
                if len(getattr(self, "_pnl_active_by_request_id", {}).get(
                        meta.request_id, ())) > 1:
                    reasons.append("REQ_ID_REUSED")
        accepted = not reasons
        row = {
            "callback_type": callback_type, "received_at": now.isoformat(),
            "broker_timestamp": None,
            "request_id": meta.request_id if meta else (
                str(broker_req_id) if broker_req_id not in (None, "") else None),
            "account": account or (meta.account if meta else None),
            "model_code": model_code if model_code is not None else (
                meta.model_code if meta else ""),
            "conid": getattr(obj, "conId", None) or (meta.conid if meta else None),
            "connection_generation": getattr(self, "_connection_generation", 0),
            "subscription_generation": meta.subscription_generation if meta else None,
            "daily_pnl": clean_pnl(getattr(obj, "dailyPnL", None)),
            "unrealized_pnl": clean_pnl(getattr(obj, "unrealizedPnL", None)),
            "realized_pnl": clean_pnl(getattr(obj, "realizedPnL", None)),
            "session_date": now.astimezone(self.cfg.market_tz).date().isoformat(),
            "reset_configuration": "IBKR_TWS_CONFIG_UNCONFIRMED",
            "source": ("IBKR_reqPnLSingle" if callback_type == "pnlSingle"
                       else "IBKR_reqPnL"),
            "accepted": accepted,
            "quality_reasons": reasons,
        }
        lock = getattr(self, "_pnl_audit_lock", None)
        if lock is None:
            lock = self._pnl_audit_lock = threading.RLock()
        with lock:
            queue = getattr(self, "_pnl_callback_audit", None)
            if queue is None:
                queue = self._pnl_callback_audit = deque(maxlen=10000)
            queue.append(row)
        if accepted:
            self._mark_live("pnl_as_of")

    def drain_pnl_audit(self) -> tuple[list[dict], list[dict]]:
        lock = getattr(self, "_pnl_audit_lock", None)
        if lock is None:
            return [], []
        with lock:
            callbacks = list(getattr(self, "_pnl_callback_audit", ()))
            lifecycle = list(getattr(self, "_pnl_subscription_audit", ()))
            getattr(self, "_pnl_callback_audit", deque()).clear()
            getattr(self, "_pnl_subscription_audit", deque()).clear()
        return callbacks, lifecycle

    def _bind_pnl_events(self, ib) -> None:
        def account_handler(obj):
            self._record_pnl_callback("pnl", obj)

        def position_handler(obj):
            self._record_pnl_callback("pnlSingle", obj)

        ib.pnlEvent += account_handler
        ib.pnlSingleEvent += position_handler
        self._live_handlers.extend([
            (ib.pnlEvent, account_handler),
            (ib.pnlSingleEvent, position_handler),
        ])

    def _refresh_live_pnl_subscriptions(self, account_hash: str) -> None:
        ib = self._ensure_connected()
        if self._live_account_pnl is None:
            self._live_account_pnl = ib.reqPnL(account_hash)
            self._register_pnl_subscription(
                self._live_account_pnl, account=account_hash)
        wanted = {
            int(getattr(item.contract, "conId", 0))
            for item in ib.portfolio(account_hash)
            if getattr(item, "position", 0) != 0 and getattr(item.contract, "conId", 0)
        }
        for conid in wanted - set(self._live_position_pnl):
            obj = ib.reqPnLSingle(account_hash, "", conid)
            self._live_position_pnl[conid] = obj
            self._register_pnl_subscription(
                obj, account=account_hash, model_code="", conid=conid)
        for conid in set(self._live_position_pnl) - wanted:
            try:
                ib.cancelPnLSingle(account_hash, "", conid)
            finally:
                obj = self._live_position_pnl.get(conid)
                meta = self._unregister_pnl_subscription(obj)
                self._record_subscription_event(
                    event_type="cancelled", request_id=meta.request_id if meta else None,
                    account=account_hash, model_code="", conid=conid,
                    subscription_generation=(meta.subscription_generation
                                             if meta else None))
                self._live_position_pnl.pop(conid, None)

    def start_live(self, account_hash: str) -> None:
        """启动 IBKR 持仓/PnL/订单/成交、指数行情与富途报价的持续订阅。"""
        if self._live_started and self._live_account == account_hash:
            return
        ib = self._ensure_connected()
        ib_exec = self._ensure_exec_connected()
        self._live_account = account_hash

        self._bind_live_event(ib.positionEvent, "positions_as_of")
        self._bind_live_event(ib.updatePortfolioEvent, "positions_as_of")
        self._bind_live_event(ib.accountValueEvent, "account_as_of")
        self._bind_pnl_events(ib)
        self._bind_live_event(ib_exec.openOrderEvent, "orders_as_of")
        self._bind_live_event(ib_exec.orderStatusEvent, "orders_as_of")
        self._bind_live_event(ib_exec.execDetailsEvent, "executions_as_of")
        self._bind_live_event(ib_exec.commissionReportEvent, "executions_as_of")

        # 初始全量同时建立后续事件订阅；clientId=0 连接会自动绑定手工订单。
        self.reconcile_live(account_hash)
        self._refresh_live_pnl_subscriptions(account_hash)
        symbols = [getattr(i.contract, "symbol", "") for i in ib.portfolio(account_hash)]
        self.update_futu_universe(symbols)
        now = datetime.now(timezone.utc).isoformat()
        for key in ("positions_as_of", "account_as_of", "pnl_as_of",
                    "orders_as_of", "executions_as_of"):
            self._live_times[key] = self._live_times[key] or now
        self._live_started = True
        self._ensure_live_market_subscriptions()
        log.info("实时订阅已启动 account=%s positions=%d", account_hash, len(symbols))

    def _ensure_live_market_subscriptions(self) -> None:
        """为 VIX/VXN/US10Y 建立持久只读行情订阅；失败不影响账户实时层。"""
        generation = getattr(self, "_connection_generation", 0)
        if (generation and getattr(
                self, "_live_market_subscription_generation", 0) == generation):
            return
        from ib_async import Index

        ib = self._ensure_connected()
        requested = int(getattr(self.cfg, "ib_market_data_type", 3) or 3)
        try:
            ib.reqMarketDataType(requested)
        except Exception:  # noqa: BLE001 — 指数行情失败不得阻断账户订阅
            log.warning("IBKR 指数行情类型设置失败", exc_info=True)

        tickers: dict[str, Any] = {}
        contracts: dict[str, Any] = {}
        for code, (symbol, exchange, currency) in IBKR_LIVE_MARKET_INDEXES.items():
            contract = Index(symbol, exchange, currency)
            try:
                ib.qualifyContracts(contract)
                tickers[code] = ib.reqMktData(contract)
                contracts[code] = contract
            except Exception:  # noqa: BLE001 — 逐标的降级为历史日线
                log.warning("IBKR 指数实时订阅失败 %s", code, exc_info=True)
        self._live_market_tickers = tickers
        self._live_market_contracts = contracts
        self._live_market_subscription_generation = generation
        log.info("IBKR 指数行情订阅 %d/%d 个，requestedType=%s",
                 len(tickers), len(IBKR_LIVE_MARKET_INDEXES), requested)

    def _live_index_market_quotes(self, now: datetime) -> dict[str, dict]:
        """读取指数 ticker 缓存；只有 LAST/MID 且有真实时点才覆盖日线。"""
        self._ensure_live_market_subscriptions()
        out: dict[str, dict] = {}
        requested = int(getattr(self.cfg, "ib_market_data_type", 3) or 3)
        # 延迟行情通常落后约 15 分钟；这里判断的是订阅是否停止更新，而不是把
        # 正常的延迟行情误判成 stale。
        stale_seconds = max(
            int(getattr(self.cfg, "futu_quote_stale_seconds", 300) or 300), 1800)
        for code, ticker in getattr(self, "_live_market_tickers", {}).items():
            try:
                q = classify_quote(
                    md_type=getattr(ticker, "marketDataType", None),
                    last=getattr(ticker, "last", None),
                    close=getattr(ticker, "close", None),
                    bid=getattr(ticker, "bid", None), ask=getattr(ticker, "ask", None),
                    tick_time=getattr(ticker, "time", None),
                    halted=getattr(ticker, "halted", None), now=now,
                    stale_seconds=stale_seconds)
                previous = getattr(ticker, "close", None)
                if (q.price is None or q.price_source not in (
                        PRICE_SOURCE_LAST, PRICE_SOURCE_MID)
                        or not q.as_of or not _valid_px(previous)):
                    continue
                out[code] = {
                    "price": q.price, "previous_close": _d(previous),
                    "price_source": q.price_source, "quality": q.quality,
                    "as_of": q.as_of, "market_data_type": q.market_data_type,
                    "requested_market_data_type": requested,
                    "effective_market_data_type": q.market_data_type,
                    "source": "IBKR_reqMktData",
                }
            except Exception:  # noqa: BLE001 — 坏 ticker 只降级该指数
                log.warning("IBKR 指数 ticker 解析失败 %s", code, exc_info=True)
        return out

    def update_futu_universe(self, symbols) -> None:
        lock = getattr(self, "_futu_universe_lock", None)
        if lock is None:
            lock = self._futu_universe_lock = threading.RLock()
        with lock:
            self._futu_universe = {
                str(s) for s in symbols if s} | set(MARKET_LIVE_SYMBOLS)

    def refresh_futu_quotes(self) -> dict:
        """Network maintenance entrypoint for the dedicated quote worker only."""
        if self._futu_quote_client is None:
            return {"enabled": False, "requested": 0, "fresh": 0}
        lock = getattr(self, "_futu_universe_lock", None)
        if lock is None:
            lock = self._futu_universe_lock = threading.RLock()
        with lock:
            symbols = sorted(getattr(self, "_futu_universe", set(MARKET_LIVE_SYMBOLS)))
        ok = self._futu_quote_client.start(symbols)
        health = self._futu_quote_client.health(symbols)
        return {"enabled": True, "ok": ok, **health}

    def pump_live(self, seconds: float = 0.2) -> None:
        """让 ib_async 事件循环处理所有主/成交连接的推送。"""
        if not self._live_started or not self._live_account:
            raise BrokerApiError("实时订阅尚未启动")
        self._ensure_connected()
        self._ensure_live_market_subscriptions()
        self._ib.sleep(max(0.0, seconds))
        self._refresh_live_pnl_subscriptions(self._live_account)

    def reconcile_live(self, account_hash: str) -> None:
        """主动重取持仓/成交，并重申订单自动绑定，校准实时缓存。"""
        import asyncio
        from ib_async import ExecutionFilter

        ib = self._ensure_connected()
        ib_exec = self._ensure_exec_connected()
        # IBKR 偶尔不发送 *End（本项目 openOrders/completedOrders/executions 已实测发生）。
        # 订单使用 clientId=0 的自动绑定持续流；ib_async 也明确推荐读取 openTrades
        # 缓存而不是重复 reqOpenOrders（后者更慢且可能陈旧）。每轮重新声明自动绑定，
        # 断线恢复后仍能接住手工订单。持仓与成交继续主动重取，并彼此隔离超时。
        ib_exec.reqAutoOpenOrders(True)
        ib_exec.openTrades()
        timeout = min(float(self.cfg.ib_req_timeout), 5.0)
        requests = (
            ("positions", ib, ib.reqPositionsAsync()),
            ("executions", ib_exec,
             ib_exec.reqExecutionsAsync(ExecutionFilter())),
        )
        failed = []
        for name, runner, awaitable in requests:
            try:
                runner.run(asyncio.wait_for(awaitable, timeout))
            except Exception as exc:  # noqa: BLE001 — 缓存仍可用，下一轮继续校准
                failed.append(f"{name}:{type(exc).__name__}")
        ib.sleep(min(self.cfg.ib_commission_wait, 1.0))
        if failed:
            log.warning("实时缓存部分校准未完成（不阻断事件流）：%s", ",".join(failed))
        # 账户订阅在 connect(account=...) 时建立；有账户汇总即满足启动闸门。
        if not ib.accountSummary(account_hash):
            raise BrokerApiError("IBKR 实时缓存校准后账户汇总仍为空")
        self._refresh_live_pnl_subscriptions(account_hash)
        self._live_times["reconciled_at"] = datetime.now(timezone.utc).isoformat()

    def _live_ibkr_quote(self, item) -> dict | None:
        """富途缺失时使用 IBKR portfolio 标记价，明确标成回退而非实时。"""
        px = getattr(item, "marketPrice", None)
        if not _valid_px(px):
            return None
        session_open = True
        try:
            from .market_hours import CLOSED, market_session
            session_open = market_session(datetime.now(timezone.utc), self.cfg.market_tz) != CLOSED
        except Exception:  # noqa: BLE001
            pass
        return {
            "price": _d(px), "price_source": PRICE_SOURCE_LAST,
            "quality": (PriceQuality.DELAYED.value if session_open
                        else PriceQuality.DELAYED_FROZEN.value),
            "as_of": self._live_times.get("positions_as_of"),
            "market_data_type": 3, "source": "IBKR_updatePortfolio",
            "requested_market_data_type": 3, "effective_market_data_type": 3,
        }

    def _live_today_fills(self, account_hash: str, now: datetime) -> list[dict]:
        """读取 clientId=0 缓存中的本交易日成交，供富途盯市今日盈亏使用。"""
        try:
            fills = self._ensure_exec_connected().fills()
        except Exception:  # noqa: BLE001 — 成交缓存不可用时保留 IBKR PnL
            return []
        today = now.astimezone(self.cfg.market_tz).date()
        rows = []
        for fill in fills:
            ex = getattr(fill, "execution", None)
            contract = getattr(fill, "contract", None)
            if ex is None or contract is None:
                continue
            account = getattr(ex, "acctNumber", "")
            if account_hash and account not in ("", account_hash):
                continue
            ts = getattr(fill, "time", None) or getattr(ex, "time", None)
            if not isinstance(ts, datetime):
                continue
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            if ts.astimezone(self.cfg.market_tz).date() != today:
                continue
            side = _SIDE_MAP.get(str(getattr(ex, "side", "")).upper(), "")
            shares, price = _d(getattr(ex, "shares", 0)), _d(getattr(ex, "price", 0))
            if side not in ("BUY", "SELL") or shares <= 0 or price <= 0:
                continue
            cr = getattr(fill, "commissionReport", None)
            has_comm = bool(cr is not None and getattr(cr, "execId", ""))
            commission = (abs(_d(getattr(cr, "commission", 0))) if has_comm else None)
            rows.append({
                "symbol": getattr(contract, "symbol", None), "side": side,
                "shares": shares, "price": price, "commission": commission,
            })
        return [r for r in rows if r["symbol"]]

    @staticmethod
    def _futu_mark_to_market(positions: list[dict], quotes: dict[str, dict],
                             fills: list[dict]) -> dict | None:
        """以前收为基准，用富途价格和 IBKR 数量/成交闭合当日盯市盈亏。

        公式：日初数量×(现价−前收) + Σ成交有符号数量×(现价−成交价) − 佣金。
        它能正确覆盖日内加减仓；输入不完整时返回 None，由 IBKR PnL 回退接管。
        """
        end_qty = {p["symbol"]: _d(p.get("qty")) for p in positions}
        by_symbol: dict[str, list[dict]] = {}
        for fill in fills:
            by_symbol.setdefault(fill["symbol"], []).append(fill)
        symbols = set(end_qty) | set(by_symbol)
        if not symbols:
            return {"total": Decimal("0"), "by_symbol": {}, "complete": True}
        total = Decimal("0")
        per_symbol: dict[str, Decimal] = {}
        complete = True
        for symbol in symbols:
            quote = quotes.get(symbol) or {}
            price = quote.get("price")
            previous = quote.get("previous_close")
            if (not str(quote.get("source") or "").startswith("FUTU")
                    or price is None or previous is None):
                return None
            price, previous = _d(price), _d(previous)
            trades = by_symbol.get(symbol, [])
            signed_trades = [(_d(t["shares"]) if t["side"] == "BUY"
                              else -_d(t["shares"]), t) for t in trades]
            start_qty = end_qty.get(symbol, Decimal("0")) - sum(
                (q for q, _ in signed_trades), Decimal("0"))
            pnl = start_qty * (price - previous)
            for signed_qty, trade in signed_trades:
                pnl += signed_qty * (price - _d(trade["price"]))
                commission = trade.get("commission")
                if commission is None:
                    complete = False
                else:
                    pnl -= _d(commission)
            per_symbol[symbol] = pnl
            total += pnl
        return {"total": total, "by_symbol": per_symbol, "complete": complete}

    def fetch_live_positions_and_balances(
            self, account_hash: str) -> tuple[list[dict], dict]:
        """读取事件缓存，不重新订阅、不等待 4 秒，供 5 秒快照使用。"""
        self.start_live(account_hash)
        ib = self._ensure_connected()
        items = ib.portfolio(account_hash)
        summary = ib.accountSummary(account_hash)
        positions = parse_ib_portfolio(items)
        balances = parse_ib_account_summary(summary)
        balances["raw_json"] = raw_capture.account_raw(summary)
        balances["_ready"] = bool(summary) and balances["equity"] > 0
        balances["account"] = account_hash
        balances["source"] = "IBKR_accountSummary"

        now = datetime.now(timezone.utc)
        fills = self._live_today_fills(account_hash, now)
        by_symbol = {getattr(i.contract, "symbol", ""): i for i in items}
        symbols = [p["symbol"] for p in positions]
        fill_symbols = [r["symbol"] for r in fills]
        futu_universe = sorted(set(symbols) | set(fill_symbols) | set(MARKET_LIVE_SYMBOLS))
        self.update_futu_universe(futu_universe)
        futu = (self._futu_quote_client.cached(futu_universe, now=now)
                if self._futu_quote_client is not None else {})
        if not hasattr(self, "_last_position_quotes"):
            self._last_position_quotes = {}
        self._last_market_quotes = {
            symbol: futu[symbol] for symbol in MARKET_LIVE_SYMBOLS if symbol in futu}
        self._last_market_quotes.update(self._live_index_market_quotes(now))
        for p in positions:
            item = by_symbol.get(p["symbol"])
            q = futu.get(p["symbol"])
            fresh_quote = bool(q and valid_price(q.get("price")) is not None)
            if not fresh_quote:
                q = None
            if q is None:
                q = self._live_ibkr_quote(item) if item else None
                if q is not None and valid_price(q.get("price")) is None:
                    q = None
            if q is None:
                q = self._last_position_quotes.get(p["symbol"])
                if q is not None and valid_price(q.get("price")) is not None:
                    q = dict(q)
                    q["quality"] = PriceQuality.STALE.value
                else:
                    q = None
            if q:
                if fresh_quote or str(q.get("source") or "").startswith("IBKR"):
                    self._last_position_quotes[p["symbol"]] = dict(q)
                p["price"] = q["price"]
                p["price_source"] = q["price_source"]
                p["price_quality"] = q["quality"]
                p["quote_as_of"] = q["as_of"]
                p["market_data_type"] = q["market_data_type"]
                p["source"] = q["source"]
                p["requested_market_data_type"] = q.get("requested_market_data_type")
                p["effective_market_data_type"] = q.get("effective_market_data_type")
                p["previous_close"] = q.get("previous_close")
            else:
                p.update(price=None, price_source=PRICE_SOURCE_NONE,
                         price_quality=PriceQuality.MISSING.value,
                         quote_as_of=None, market_data_type=None,
                         source="IBKR_updatePortfolio")
            if item is not None:
                p["currency"] = getattr(item.contract, "currency", None)
                p["raw_json"] = raw_capture.position_raw(item)
            # IBKR marketValue/upl remain authoritative risk inputs.  Futu
            # valuation is written only to explicit shadow fields.
            if q and str(q.get("source") or "").startswith("FUTU"):
                p["futu_market_value"] = p["qty"] * q["price"]
                p["futu_unrealized_estimate"] = (
                    p["qty"] * (q["price"] - p["avg_cost"]))
                quote_at = datetime.fromisoformat(
                    str(q["as_of"]).replace("Z", "+00:00"))
                if quote_at.tzinfo is None:
                    quote_at = quote_at.replace(tzinfo=timezone.utc)
                p["futu_quote_age_seconds"] = max(
                    (now - quote_at.astimezone(timezone.utc)).total_seconds(), 0)
            p["market_value_source"] = "IBKR_updatePortfolio"
            p["upl_source"] = "IBKR_updatePortfolio"
            if q:
                revalue_position(p, price=q.get("price"), source=q.get("source"))
            live_pnl = self._live_position_pnl.get(p.get("conid"))
            day_pnl = clean_pnl(getattr(live_pnl, "dailyPnL", None))
            if day_pnl is not None:
                p["day_pnl"] = day_pnl
                p["day_pnl_source"] = "IBKR_reqPnLSingle"
            realized_pnl = clean_pnl(getattr(live_pnl, "realizedPnL", None))
            if realized_pnl is not None:
                p["realized_pnl"] = realized_pnl
                p["realized_pnl_source"] = "IBKR_reqPnLSingle"

        acc = self._live_account_pnl
        account_pnl = {
            "daily_pnl": clean_pnl(getattr(acc, "dailyPnL", None)),
            "realized_pnl": clean_pnl(getattr(acc, "realizedPnL", None)),
            "unrealized_pnl": clean_pnl(getattr(acc, "unrealizedPnL", None)),
            "source": "IBKR_reqPnL", "quality": None, "daily_basis": None,
        }
        if positions and all(p.get("upl") is not None for p in positions):
            account_pnl["unrealized_pnl"] = sum(
                (p["upl"] for p in positions), Decimal("0"))
            account_pnl["unrealized_source"] = "当前价 × IBKR 持仓成本"
        elif not positions:
            account_pnl["unrealized_pnl"] = Decimal("0")
        derived = self._futu_mark_to_market(positions, futu, fills)
        futu_position_symbols = {p["symbol"] for p in positions}
        covered = len(futu_position_symbols & set(futu))
        quote_coverage = (Decimal(covered) / Decimal(len(futu_position_symbols))
                          if futu_position_symbols else Decimal("1"))
        futu_shadow = {
            "source": "futu", "authority": "market_quote",
            "account_pnl_authoritative": False,
            "quote_coverage": quote_coverage,
            "status": "complete" if quote_coverage == 1 else "partial",
            "futu_total_position_value": sum(
                (p.get("futu_market_value") or Decimal("0") for p in positions),
                Decimal("0")),
            "futu_intraday_mark_to_market_change": (
                derived["total"] if derived is not None else None),
            "complete_inputs": bool(derived and derived["complete"]),
        }
        if derived is not None:
            for p in positions:
                if p["symbol"] in derived["by_symbol"]:
                    p["futu_intraday_mtm"] = derived["by_symbol"][p["symbol"]]
        balances["_account_pnl"] = account_pnl
        balances["_futu_shadow"] = futu_shadow
        balances["positions_value_source"] = "IBKR_accountSummary"
        balances.update(self._live_times)
        return positions, balances

    def fetch_live_orders(self, account_hash: str) -> list[dict]:
        self.start_live(account_hash)
        rows = []
        for trade in self._ensure_exec_connected().trades():
            account = getattr(trade.order, "account", "")
            if account_hash and account not in ("", account_hash):
                continue
            rows.append(parse_ib_trade(trade))
        return rows

    def fetch_live_executions(self, account_hash: str) -> list[dict]:
        self.start_live(account_hash)
        rows = []
        for fill in self._ensure_exec_connected().fills():
            account = getattr(fill.execution, "acctNumber", "")
            if account_hash and account not in ("", account_hash):
                continue
            rows.append(parse_ib_execution(fill))
        return rows

    def live_payload(self, account_hash: str) -> dict:
        positions, balances = self.fetch_live_positions_and_balances(account_hash)
        public_balances = {
            key: balances.get(key) for key in (
                "equity", "cash", "margin_balance", "buying_power", "total_cash",
                "available_funds", "excess_liquidity", "positions_value",
                "positions_value_source", "currency")
        }
        public_positions = [{
            key: p.get(key) for key in (
                "symbol", "qty", "avg_cost", "market_value", "upl", "price",
                "price_source", "price_quality", "quote_as_of", "source", "day_pnl",
                "day_pnl_source", "realized_pnl", "realized_pnl_source",
                "market_value_source", "upl_source",
                "previous_close", "currency", "conid")
        } for p in positions]
        market_quotes = [{
            key: q.get(key) for key in (
                "price", "previous_close", "price_source", "quality", "as_of",
                "source", "open", "high", "low", "volume", "received_at",
                "market_data_type", "requested_market_data_type",
                "effective_market_data_type")
        } | {"symbol": symbol} for symbol, q in getattr(
            self, "_last_market_quotes", {}).items()]
        return {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "account": account_hash, "balances": public_balances,
            "account_pnl": balances.get("_account_pnl") or {},
            "positions": public_positions, "market_quotes": market_quotes,
            "timestamps": dict(self._live_times),
        }

    def close_live(self) -> None:
        if self._futu_quote_client is not None:
            self._futu_quote_client._reset()
        if self._ib is not None:
            for contract in getattr(self, "_live_market_contracts", {}).values():
                try:
                    self._ib.cancelMktData(contract)
                except Exception:  # noqa: BLE001
                    pass
        self._live_market_tickers = {}
        self._live_market_contracts = {}
        self._live_market_subscription_generation = 0
        for ib in (self._ib_exec, self._ib):
            if ib is not None:
                try:
                    ib.disconnect()
                except Exception:  # noqa: BLE001
                    pass
        self._live_started = False

    def _fetch_position_quotes(self, contracts) -> dict:
        """富途实时优先，缺失标的逐个回退 IBKR。

        这里只选择**报价**。传入的 contract、持仓数量、成本、账户余额、PnL
        仍来自前面的 IBKR portfolio/accountSummary/reqPnL，不会被富途替换。
        """
        primary = {}
        if self._futu_quote_client is not None:
            symbols = [getattr(c, "symbol", None) for c in contracts]
            # Cache-only: establishing/reconnecting OpenD here used to block the
            # account scheduler.  The dedicated quote worker owns all network IO.
            cached = getattr(self._futu_quote_client, "cached", None)
            current = getattr(self._futu_quote_client, "current", None)
            wanted = [s for s in symbols if s]
            if cached:
                primary = cached(wanted)
            elif current:
                primary = current(wanted, ensure_started=False)
            else:  # compatibility for simple test doubles only
                primary = self._futu_quote_client.fetch(wanted)
        missing = [c for c in contracts
                   if getattr(c, "symbol", None) not in primary]
        if not missing:
            return primary
        fallback = self._fetch_quotes(missing)
        if primary:
            log.info("富途实时报价 %d 个，IBKR 回退 %d 个", len(primary), len(fallback))
        elif self._futu_quote_client is not None:
            log.info("富途本轮无可用报价，全部回退 IBKR（%d 个）", len(fallback))
        return {**fallback, **primary}

    def _recover_portfolio(self, account_hash: str, equity) -> list:
        """portfolio() 空但账户有净值 → 重连以重新订阅 accountUpdates，重取一次。

        实测：全新连接 + 等 ~4s 让 accountUpdate 事件回填后，portfolio() 恢复正常。
        带 90s 冷却，避免罕见的持续为空时每分钟重连风暴。
        """
        import time as _time
        now = _time.time()
        if now - getattr(self, "_last_portfolio_recover", 0.0) < 90:
            log.warning("portfolio 空（净资产=%s），重连冷却中，本轮先接受空", equity)
            return []
        self._last_portfolio_recover = now
        log.warning("portfolio 空但净资产=%s，疑似组合订阅失效，重连重订阅后重取", equity)
        try:
            self._ib.disconnect()
        except Exception:  # noqa: BLE001 — 断开失败也无妨，下面重建
            pass
        self._ib = None
        self._rt_probe_until = None        # 新会话重新读权限，值得再探一次实时
        ib = self._ensure_connected()
        ib.sleep(4)                       # 等 accountUpdate 事件回填组合
        items = ib.portfolio(account_hash)
        log.info("重连后 portfolio 取得 %d 项", len(items))
        return items

    def fetch_pnl_bundle(self, account_hash: str, conids) -> dict:
        """账户级 + 逐持仓 PnL，**一次订阅、共用一次等待**。

        两者必须在同一时刻取：盘后价格持续变动，分两次订阅会让「逐持仓合计」
        与「账户 DailyPnL」对不上（实测差 0.16，被对账判成 MISMATCH，而数据本身没错，
        错的是取数时点）。共用等待顺带省掉一次 4s。
        """
        ib = self._ensure_connected()
        acc = None
        subs = []
        try:
            acc = ib.reqPnL(account_hash)
            acc_meta = self._register_pnl_subscription(acc, account=account_hash)
            for cid in conids:
                if cid:
                    obj = ib.reqPnLSingle(account_hash, "", cid)
                    meta = self._register_pnl_subscription(
                        obj, account=account_hash, model_code="", conid=cid)
                    subs.append((cid, obj, meta))
            ib.sleep(self.cfg.ib_pnl_wait)      # PnL 经事件异步回填，等一拍
            self._record_pnl_callback("pnl", acc)
            for _cid, obj, _meta in subs:
                self._record_pnl_callback("pnlSingle", obj)
            return {
                "account": {
                    "daily_pnl": clean_pnl(getattr(acc, "dailyPnL", None)),
                    "realized_pnl": clean_pnl(getattr(acc, "realizedPnL", None)),
                    "unrealized_pnl": clean_pnl(getattr(acc, "unrealizedPnL", None)),
                },
                "positions": {
                    cid: {
                        "day_pnl": clean_pnl(getattr(p, "dailyPnL", None)),
                        "realized_pnl": clean_pnl(getattr(p, "realizedPnL", None)),
                    }
                    for cid, p, _meta in subs
                },
            }
        except Exception:  # noqa: BLE001
            log.warning("PnL 拉取失败，本轮 PnL 留空", exc_info=False)
            return {"account": {}, "positions": {}}
        finally:
            for cid, obj, meta in subs:
                try:
                    ib.cancelPnLSingle(account_hash, "", cid)
                except Exception:  # noqa: BLE001
                    pass
                self._record_subscription_event(
                    event_type="cancelled", request_id=meta.request_id,
                    account=account_hash, model_code="", conid=cid,
                    subscription_generation=meta.subscription_generation)
                self._unregister_pnl_subscription(obj)
            if acc is not None:
                try:
                    ib.cancelPnL(account_hash)
                except Exception:  # noqa: BLE001
                    pass
                meta = locals().get("acc_meta")
                if meta is not None:
                    self._record_subscription_event(
                        event_type="cancelled", request_id=meta.request_id,
                        account=account_hash, model_code="", conid=None,
                        subscription_generation=meta.subscription_generation)
                self._unregister_pnl_subscription(acc)

    def fetch_position_pnl(self, account_hash: str, conids) -> dict:
        """逐持仓当日盈亏（reqPnLSingle）。实测各持仓 dailyPnL 之和 == 账户级 dailyPnL。

        必须用它而不是 (今收-前收)*数量：当日新建的仓位成本不是前收，自算会严重偏离
        （实测自算合计 -2784 vs 实际 -690.84）。
        realizedPNL 常返回 max double 哨兵（表示无已实现），由 clean_pnl 过滤。
        """
        ib = self._ensure_connected()
        subs = []
        try:
            for cid in conids:
                if cid:
                    subs.append((cid, ib.reqPnLSingle(account_hash, "", cid)))
            if not subs:
                return {}
            ib.sleep(self.cfg.ib_pnl_wait)
            return {
                cid: {
                    "day_pnl": clean_pnl(getattr(p, "dailyPnL", None)),
                    "unrealized": clean_pnl(getattr(p, "unrealizedPnL", None)),
                    "realized": clean_pnl(getattr(p, "realizedPnL", None)),
                }
                for cid, p in subs
            }
        except Exception as exc:
            log.warning("逐持仓 PnL 拉取失败（%s），本轮持仓今日盈亏留空", type(exc).__name__)
            return {}
        finally:
            for cid, _ in subs:
                try:
                    ib.cancelPnLSingle(account_hash, "", cid)
                except Exception:  # noqa: BLE001
                    pass

    def fetch_pnl(self, account_hash: str) -> dict:
        """账户级盈亏（reqPnL）。返回原始值，哨兵过滤由 app.pnl.clean_pnl 负责。

        accountValues 里没有 RealizedPnL/UnrealizedPnL 标签（实测），必须走此订阅接口。
        """
        ib = self._ensure_connected()
        try:
            pnl = ib.reqPnL(account_hash)
            ib.sleep(self.cfg.ib_pnl_wait)      # PnL 经事件异步回填，需等一拍
            out = {
                "account": account_hash,
                "daily_pnl": getattr(pnl, "dailyPnL", None),
                "realized_pnl": getattr(pnl, "realizedPnL", None),
                "unrealized_pnl": getattr(pnl, "unrealizedPnL", None),
            }
        except Exception as exc:
            raise BrokerApiError(f"IBKR PnL 拉取失败: {exc}") from exc
        finally:
            try:
                ib.cancelPnL(account_hash)
            except Exception:  # noqa: BLE001
                pass
        return out

    def _fetch_quotes(self, contracts) -> dict:
        """订阅行情、固定等待、逐合约分级。容忍部分无数据（无数据→MISSING/INVALID）。

        不用 reqTickers（它等所有合约到齐；某标的无延迟数据会拖垮整体）。
        改 reqMktData 订阅 + ib_quote_wait 秒固定等待，然后读各 ticker 当前值。
        失败/异常整体降级（持仓照常落库，R7 视为 MISSING，安全不触发）。
        """
        ib = self._ensure_connected()
        # 补齐 portfolio 合约缺失的 exchange（否则 reqMktData 报 error 321）
        for c in contracts:
            if not getattr(c, "exchange", None):
                c.exchange = "SMART"
        requested = int(getattr(self.cfg, "ib_market_data_type", 3) or 3)
        fallback = int(getattr(self.cfg, "ib_market_data_type_fallback", 3) or 3)

        now_mono = time.monotonic()
        # 上次实时探测失败后，短期内直接用回退类型 —— 否则每轮都白等一个
        # ib_quote_wait（本项目约 10s），同步周期凭空多一倍行情等待。
        # 但**不能永久放弃**：订阅一旦生效要能自动接上，所以是「冷却」而非「关闭」。
        probe_due = (self._rt_probe_until is None or now_mono >= self._rt_probe_until)
        if requested != fallback and not probe_due:
            out, effective = self._try_market_data_type(ib, contracts, fallback)
        else:
            out, effective = self._try_market_data_type(ib, contracts, requested)

            # ---- 实时拿不到就整体回退 ----
            # 为什么「按结果」判断而不是「按错误码」：未订阅时 IBKR 的 10089 是
            # **异步 error 事件**，reqMktData 本身不抛异常，ticker 只是静静地不填充。
            # 靠捕获异常判断会漏，结果是价格全空 —— 那比延迟价更糟。
            # 所以判据只有一条：这一轮有没有真的拿到可用价格。
            if requested != fallback and not _has_usable(out):
                log.info("行情类型 %d 未取到可用报价（订阅可能未生效），回退到 %d，"
                         "并在 %d 分钟内直接使用回退类型",
                         requested, fallback, self.cfg.ib_realtime_probe_cooldown_min)
                self._rt_probe_until = now_mono + (
                    self.cfg.ib_realtime_probe_cooldown_min * 60)
                out, effective = self._try_market_data_type(ib, contracts, fallback)
                if not _has_usable(out):
                    log.warning("行情类型 %d 与回退 %d 均无可用报价，本轮标记 MISSING",
                                requested, fallback)
            elif requested != fallback and effective == requested:
                if self._rt_probe_until is not None:
                    log.info("行情类型 %d 已生效，恢复实时取价", requested)
                self._rt_probe_until = None

        now = datetime.now(timezone.utc)

        # 隔夜时段补救：SMART 只给到收盘价的标的，改走隔夜场再问一次。
        # 实测 20:00–24:00 ET 有 LAST，00:00–04:00 ET 全部回退 CLOSE ——
        # 美股隔夜成交在独立场内（IBKR 的 OVERNIGHT/IBEOS），SMART 午夜后取不到。
        overnight = self._overnight_retry(ib, contracts, out, now)
        if overnight:
            out.update(overnight)

        # 「请求了哪种、实际生效哪种」必须**最后**盖章 —— 隔夜补价会整条替换
        # 条目，若在补价前写就会被覆盖成 NULL，看起来像没记录过。
        # 两者不一致即订阅未生效，这是判断实时行情到位与否的直接凭据。
        for v in out.values():
            v["requested_market_data_type"] = requested
            v["effective_market_data_type"] = effective
        return out

    def _try_market_data_type(self, ib, contracts, md_type: int
                              ) -> tuple[dict, int | None]:
        """按指定行情类型订阅一轮并分级。返回 (结果, 实际生效的类型)。

        「实际生效的类型」取自 ticker 自报的 `marketDataType` —— 请求 1 而
        IBKR 给回 3 是可能的（未订阅时它可能静默降级而不是报错），
        这个差异必须留痕，否则会误以为已经在用实时行情。
        """
        tickers = []
        try:
            ib.reqMarketDataType(md_type)
            tickers = [ib.reqMktData(c) for c in contracts]
            ib.sleep(self.cfg.ib_quote_wait)
        except Exception as exc:  # noqa: BLE001
            log.warning("行情订阅失败（type=%d, %s）", md_type, type(exc).__name__)
            for c in contracts:
                try:
                    ib.cancelMktData(c)
                except Exception:  # noqa: BLE001
                    pass
            return {}, None

        now = datetime.now(timezone.utc)
        out, seen = {}, []
        for t in tickers:
            sym = getattr(getattr(t, "contract", None), "symbol", None)
            if not sym:
                continue
            reported = getattr(t, "marketDataType", None)
            if reported is not None:
                seen.append(reported)
            q = classify_quote(
                md_type=reported,
                last=getattr(t, "last", None), close=getattr(t, "close", None),
                bid=getattr(t, "bid", None), ask=getattr(t, "ask", None),
                tick_time=getattr(t, "time", None),
                halted=getattr(t, "halted", None), now=now)
            out[sym] = {"price": q.price, "price_source": q.price_source,
                        "quality": q.quality, "as_of": q.as_of,
                        "market_data_type": q.market_data_type,
                        "source": "IBKR_reqMktData"}
        for c in contracts:
            try:
                ib.cancelMktData(c)  # 释放行情线，回退前必须放掉
            except Exception:  # noqa: BLE001
                pass
        # 取众数：个别标的可能因自身订阅差异报不同值
        effective = max(set(seen), key=seen.count) if seen else None
        return out, effective

    def _overnight_retry(self, ib, contracts, out: dict, now: datetime) -> dict:
        """隔夜时段对「只拿到收盘价」的标的走隔夜场重试。

        **只在隔夜时段执行，且只补救回退到 CLOSE 的标的** —— 正常路径一行不动。
        任何异常都吞掉：拿不到隔夜价就维持收盘价（诚实的旧行为），
        绝不因为补救失败而让整轮行情丢失。
        """
        if not getattr(self.cfg, "ib_overnight_quotes", True):
            return {}
        try:
            from .market_hours import OVERNIGHT, market_session
            if market_session(now, self.cfg.market_tz) != OVERNIGHT:
                return {}
        except Exception:  # noqa: BLE001
            return {}

        need = [c for c in contracts
                if out.get(getattr(c, "symbol", None), {}).get("price_source")
                == PRICE_SOURCE_CLOSE]
        if not need:
            return {}

        venue = getattr(self.cfg, "ib_overnight_exchange", "OVERNIGHT")
        retry, fixed = [], {}
        try:
            for c in need:
                alt = copy.copy(c)
                alt.exchange = venue
                retry.append((getattr(c, "symbol", None), alt,
                              ib.reqMktData(alt)))
            ib.sleep(self.cfg.ib_quote_wait)
        except Exception as exc:  # noqa: BLE001
            log.info("隔夜场行情订阅失败（%s），维持收盘价", type(exc).__name__)
            for _sym, alt, _t in retry:
                try:
                    ib.cancelMktData(alt)
                except Exception:  # noqa: BLE001
                    pass
            return {}

        for sym, alt, t in retry:
            try:
                q = classify_quote(
                    md_type=getattr(t, "marketDataType", None),
                    last=getattr(t, "last", None), close=getattr(t, "close", None),
                    bid=getattr(t, "bid", None), ask=getattr(t, "ask", None),
                    tick_time=getattr(t, "time", None),
                    halted=getattr(t, "halted", None), now=now)
                # **只有真拿到成交/报价才采用** —— 隔夜场同样回退 CLOSE 时，
                # 用它替换毫无意义，反而会掩盖「隔夜确实没有报价」这个事实
                if sym and q.price is not None and q.price_source in (
                        PRICE_SOURCE_LAST, PRICE_SOURCE_MID):
                    fixed[sym] = {"price": q.price, "price_source": q.price_source,
                                  "quality": q.quality, "as_of": q.as_of,
                                  "market_data_type": q.market_data_type,
                                  "source": f"IBKR_reqMktData@{venue}"}
            except Exception:  # noqa: BLE001
                pass
            finally:
                try:
                    ib.cancelMktData(alt)
                except Exception:  # noqa: BLE001
                    pass

        if fixed:
            log.info("隔夜场补到 %d/%d 个标的的实时报价", len(fixed), len(need))
        else:
            log.info("隔夜场无报价（%d 个标的），维持收盘价", len(need))
        return fixed

    def fetch_orders(self, account_hash: str, start: datetime, end: datetime) -> list[dict]:
        """当日/本会话订单（trades()）。IBKR 不提供历史订单查询窗口，upsert 幂等兜底。"""
        ib = self._ensure_connected()
        try:
            trades = ib.trades()
        except Exception as exc:
            raise BrokerApiError(f"IBKR 订单拉取失败: {exc}") from exc
        rows = []
        for t in trades:
            if account_hash and getattr(t.order, "account", "") not in ("", account_hash):
                continue
            rows.append(parse_ib_trade(t))
        return rows

    def fetch_transactions(self, account_hash: str, start: datetime, end: datetime) -> list[dict]:
        """当日成交（fills()，含精确 realizedPNL）。历史缺口可用 Flex Query 手动补。"""
        ib = self._ensure_connected()
        try:
            fills = ib.fills()
        except Exception as exc:
            raise BrokerApiError(f"IBKR 成交拉取失败: {exc}") from exc
        rows = []
        for f in fills:
            if account_hash and getattr(f.execution, "acctNumber", "") not in ("", account_hash):
                continue
            rows.append(parse_ib_fill(f))
        return rows

    def fetch_index_bars(self, *, symbol: str, sec_type: str = "STK",
                         exchange: str = "SMART", currency: str = "USD",
                         lookback_days: int = 260) -> list[dict]:
        """市场标的日线（股票/ETF/指数通用）。

        指数（IND）与股票的 whatToShow 不同：指数没有成交，必须用 TRADES 之外的
        口径，否则 IBKR 直接返回空。这里对指数用 TRADES 失败后退到 MIDPOINT。
        """
        import asyncio
        from ib_async import Index, Stock
        ib = self._ensure_connected()
        contract = (Index(symbol, exchange, currency) if sec_type.upper() == "IND"
                    else Stock(symbol, exchange, currency))
        try:
            ib.qualifyContracts(contract)
        except Exception as exc:  # noqa: BLE001
            raise BrokerApiError(f"合约无法解析 {symbol}: {str(exc)[:100]}") from exc

        attempts = (["TRADES", "MIDPOINT"] if sec_type.upper() == "IND"
                    else ["TRADES"])
        last_err = None
        for what in attempts:
            try:
                bars = ib.reqHistoricalData(
                    contract, endDateTime="", durationStr=f"{lookback_days} D",
                    barSizeSetting="1 day", whatToShow=what, useRTH=True,
                    formatDate=1, timeout=self.cfg.ib_req_timeout)
            except (asyncio.TimeoutError, Exception) as exc:  # noqa: BLE001
                last_err = exc
                continue
            if bars:
                return [{"date": str(getattr(b, "date", "")), "o": _d(b.open),
                         "h": _d(b.high), "l": _d(b.low), "c": _d(b.close),
                         "v": int(getattr(b, "volume", 0) or 0)} for b in bars]
        raise BrokerApiError(
            f"{symbol} 无日线返回（可能无行情订阅权限）"
            + (f": {str(last_err)[:80]}" if last_err else ""))

    def fetch_contract_details(self, symbols: list[str]) -> dict[str, dict]:
        """批量取证券元数据（reqContractDetails）。

        IBKR 自带 industry/category/longName/stockType，是最权威且免费的分类来源。
        单个标的失败不影响其余 —— 分类是可降级能力，绝不能拖垮账户同步。
        """
        import asyncio
        from ib_async import Stock
        ib = self._ensure_connected()
        out: dict[str, dict] = {}
        for sym in symbols:
            try:
                dets = ib.reqContractDetails(Stock(sym, "SMART", "USD"))
            except (asyncio.TimeoutError, Exception) as exc:  # noqa: BLE001
                log.debug("contractDetails 取不到 %s: %s", sym, exc)
                continue
            if not dets:
                continue
            d = dets[0]
            from .instruments import classify_from_ibkr
            cls = classify_from_ibkr(d)
            if cls is None:
                continue
            row = cls.as_dict()
            contract = getattr(d, "contract", None)
            # 映射门禁需要保留消歧字段；不能只依赖同名 ticker。
            row["exchange"] = getattr(contract, "exchange", None)
            row["primary_exchange"] = getattr(contract, "primaryExchange", None)
            row["currency"] = getattr(contract, "currency", None)
            row["raw_json"] = raw_capture.to_raw_json({
                "symbol": sym, "industry": getattr(d, "industry", None),
                "category": getattr(d, "category", None),
                "subcategory": getattr(d, "subcategory", None),
                "stockType": getattr(d, "stockType", None),
                "longName": getattr(d, "longName", None),
                "exchange": row["exchange"], "primaryExchange": row["primary_exchange"],
                "currency": row["currency"], "conid": row.get("conid"),
            })
            out[sym] = row
        return out

    def fetch_daily_bars(self, symbol: str, start: datetime, end: datetime) -> list[dict]:
        from ib_async import Stock
        ib = self._ensure_connected()
        stk = Stock(symbol, "SMART", "USD")
        start_iso = start.date().isoformat()
        # 本 gateway 单次大区间历史会超时（实测 260 D 可用、400 D 起超时），
        # 故分块：每次 260 D（沿用市场指数已验证可用的档位），用 endDateTime
        # 向前翻页，去重拼接——IBKR 拉长历史的标准做法。块间 sleep 尊重分页限速。
        chunk = 260
        by_date: dict[str, dict] = {}
        end_dt: object = ""                       # 空串 = 现在
        for _ in range(12):                       # 最多 12 块（约 8 年），防呆
            try:
                bars = ib.reqHistoricalData(
                    stk, endDateTime=end_dt, durationStr=f"{chunk} D",
                    barSizeSetting="1 day", whatToShow="TRADES",
                    useRTH=True, formatDate=1, timeout=90,
                )
            except Exception as exc:
                raise BrokerApiError(f"IBKR 日线拉取失败 {symbol}: {exc}") from exc
            parsed = parse_ib_bars(bars)
            if not parsed:
                break
            for b in parsed:
                by_date[b["date"]] = b
            earliest = min(b["date"] for b in parsed)
            if earliest <= start_iso:             # 已覆盖到目标起点
                break
            end_dt = datetime.fromisoformat(f"{earliest}T00:00:00").replace(
                tzinfo=timezone.utc)              # 下一块以本块最早日为终点向前翻
            time.sleep(3)
        return [by_date[d] for d in sorted(by_date) if d >= start_iso]

    def fetch_5m_bars(self, symbol: str, start: datetime, end: datetime,
                      *, use_rth: bool = True) -> list[dict]:
        """读取现实正股 5 分钟 bar，供链上 signal outcome 使用。

        该接口只读 TRADES，不把 Hyperliquid mark/perp 价格写入主库。单次请求
        最多覆盖7个日历日（IBKR 5分钟bar允许的官方step size），调用方负责
        跨块游标、限速、幂等落库和结果窗口计算。
        """
        from ib_async import Stock
        ib = self._ensure_connected()
        contract = Stock(symbol, "SMART", "USD")
        try:
            ib.qualifyContracts(contract)
        except Exception as exc:  # noqa: BLE001
            raise BrokerApiError(f"合约无法解析 {symbol}: {str(exc)[:100]}") from exc
        # 单次任务最多回拉一周；历史执行器以游标逐步推进。保持一请求/任务，
        # 避免长时间占用chain executor或产生并发历史请求。
        end_dt = end
        window_start = max(start, end - timedelta(days=7))
        out: dict[str, dict] = {}
        try:
            bars = ib.reqHistoricalData(
                contract, endDateTime=end_dt, durationStr="1 W",
                barSizeSetting="5 mins", whatToShow="TRADES", useRTH=use_rth,
                formatDate=1, timeout=self.cfg.ib_req_timeout)
        except Exception as exc:  # noqa: BLE001
            raise BrokerApiError(f"IBKR 5分钟行情拉取失败 {symbol}: {exc}") from exc
        for bar in bars or []:
            ts = getattr(bar, "date", None)
            if ts is None:
                continue
            if isinstance(ts, datetime):
                dt = ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)
                key = dt.astimezone(timezone.utc).isoformat()
            else:
                key = str(ts)
            out[key] = {
                "symbol": symbol, "bar_start": key,
                "o": _d(getattr(bar, "open", None)),
                "h": _d(getattr(bar, "high", None)),
                "l": _d(getattr(bar, "low", None)),
                "c": _d(getattr(bar, "close", None)),
                "v": _d(getattr(bar, "volume", None)),
                "session": "REGULAR" if use_rth else None,
                "use_rth": use_rth, "source": "IBKR_historical_5m",
            }
        return [out[key] for key in sorted(out)
                if window_start.isoformat() <= key <= end.isoformat()]

    def auth_status(self) -> tuple[bool, str]:
        try:
            ib = self._ensure_connected()
        except BrokerApiError as exc:
            return False, str(exc)
        if ib.isConnected():
            return True, f"IB Gateway 已连接（{self.cfg.ib_host}:{self.cfg.ib_port}，只读）"
        return False, "IB Gateway 未连接"

    def disconnect(self):
        for ib in (self._ib, self._ib_exec):
            if ib is not None and ib.isConnected():
                ib.disconnect()
