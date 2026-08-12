"""市场数据适配层。

**与计算逻辑严格分离**：本模块只负责「把外部数据取回来并如实标注质量」，
指标计算在 market_metrics.py。这样将来换数据源（Polygon、Alpha Vantage…）
只需换一个适配器，规则与评分一行都不用动。

数据源选择：历史日线复用 IBKR；盘中 QQQ/SPY/SOXX 由富途 OpenD 实时覆盖。
富途拿不到的 VIX/VXN/美债以及任何异常标的继续使用 IBKR，不用代理品冒充。

四条纪律：
1. **数据时间与抓取时间分开**。上一交易日的收盘价在今天抓取，它仍然是上一交易日的
   数据 —— 绝不能因为「刚抓的」就标成实时。
2. **取不到就说取不到**。指数类数据（VIX/VXN）可能因订阅权限拿不到，
   此时记 missing 并写明原因，绝不用替代品冒充。
3. **失败保留最后有效值但标 stale**，不静默沿用。
4. **市场数据失败绝不影响 IBKR 账户同步** —— 独立任务、独立状态、异常全吞。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Protocol

from . import raw_capture
from .logging_setup import get_logger

log = get_logger()

# 质量状态（与账户侧同一套词汇）
FRESH = "fresh"
STALE = "stale"
ERROR = "error"
MISSING = "missing"


@dataclass(frozen=True)
class InstrumentSpec:
    """一个市场标的的取数规格。全部可经配置覆盖，便于换券商/换代号。"""
    code: str                     # 系统内代号
    name: str
    ib_symbol: str
    sec_type: str = "STK"         # STK（ETF 也报 STK）/ IND（指数）
    exchange: str = "SMART"
    currency: str = "USD"
    kind: str = "equity_index"    # equity_index / volatility / rates
    required: bool = True         # 缺失是否影响整体可靠性


#: 第一版标的集。
#: SOXX 是 SOX 指数的 ETF 代理 —— 指数数据常需付费订阅，ETF 走股票通道更可得，
#: 且与账户持有的半导体标的相关性足够高。真拿不到时会如实标 missing。
DEFAULT_INSTRUMENTS = [
    InstrumentSpec("QQQ", "纳斯达克100 ETF", "QQQ", kind="equity_index"),
    InstrumentSpec("SPY", "标普500 ETF", "SPY", kind="equity_index", required=False),
    InstrumentSpec("SOXX", "费城半导体 ETF", "SOXX", kind="equity_index"),
    InstrumentSpec("VIX", "标普波动率指数", "VIX", sec_type="IND", exchange="CBOE",
                   kind="volatility"),
    InstrumentSpec("VXN", "纳斯达克波动率指数", "VXN", sec_type="IND", exchange="CBOE",
                   kind="volatility", required=False),
    InstrumentSpec("US10Y", "美国十年期国债收益率", "TNX", sec_type="IND",
                   exchange="CBOE", kind="rates", required=False),
]


def _d(v: Any) -> Decimal | None:
    if v is None or v == "":
        return None
    try:
        return Decimal(str(v))
    except (InvalidOperation, ValueError, TypeError):
        return None


@dataclass
class MarketBar:
    instrument_code: str
    instrument_name: str
    data_as_of: str                     # 交易日 YYYY-MM-DD
    fetched_at: str
    open: Decimal | None = None
    high: Decimal | None = None
    low: Decimal | None = None
    close: Decimal | None = None
    previous_close: Decimal | None = None
    volume: int | None = None
    source: str = "IBKR_historical"
    delayed: bool = True
    quality_status: str = FRESH
    quality_note: str | None = None
    quote_as_of: str | None = None
    raw_json: str | None = None

    @property
    def daily_change(self) -> Decimal | None:
        if self.close is None or self.previous_close is None:
            return None
        return self.close - self.previous_close

    @property
    def daily_change_percent(self) -> Decimal | None:
        dc = self.daily_change
        if dc is None or not self.previous_close:
            return None
        return dc / self.previous_close

    def as_row(self) -> dict:
        return {
            "instrument_code": self.instrument_code,
            "instrument_name": self.instrument_name,
            "data_as_of": self.data_as_of, "fetched_at": self.fetched_at,
            "open": self.open, "high": self.high, "low": self.low, "close": self.close,
            "previous_close": self.previous_close,
            "daily_change": self.daily_change,
            "daily_change_percent": self.daily_change_percent,
            "volume": self.volume, "source": self.source,
            "delayed": self.delayed, "quality_status": self.quality_status,
            "quality_note": self.quality_note, "quote_as_of": self.quote_as_of,
            "raw_json": self.raw_json,
        }


class MarketDataProvider(Protocol):
    """市场数据源接口。换数据源只需实现这一个方法。"""

    def fetch_history(self, spec: InstrumentSpec, lookback_days: int) -> list[dict]:
        """返回日线 [{date, o,h,l,c,v}]，升序。取不到应抛异常或返回空列表。"""
        ...


class IBKRMarketData:
    """IBKR 适配器。复用已有的 reqHistoricalData 通道。"""

    def __init__(self, client):
        self.client = client

    def fetch_history(self, spec: InstrumentSpec, lookback_days: int) -> list[dict]:
        return self.client.fetch_index_bars(
            symbol=spec.ib_symbol, sec_type=spec.sec_type, exchange=spec.exchange,
            currency=spec.currency, lookback_days=lookback_days)


@dataclass
class FetchResult:
    bars_by_code: dict[str, list[dict]] = field(default_factory=dict)
    latest: dict[str, MarketBar] = field(default_factory=dict)
    failed: dict[str, str] = field(default_factory=dict)     # code -> 原因

    @property
    def ok_count(self) -> int:
        return len(self.latest)


def fetch_all(provider: MarketDataProvider, specs: list[InstrumentSpec],
              *, lookback_days: int = 260, now: datetime | None = None) -> FetchResult:
    """逐个标的取数。单个失败只记录，不影响其余，更不抛给调用方。

    lookback_days 默认 260（约一年交易日）—— 200 日均线与 VIX 分位数都需要足够历史。
    """
    now = now or datetime.now(timezone.utc)
    fetched_at = now.isoformat()
    res = FetchResult()
    for spec in specs:
        try:
            bars = provider.fetch_history(spec, lookback_days)
        except Exception as exc:  # noqa: BLE001 — 市场数据失败绝不能外溢
            res.failed[spec.code] = f"{type(exc).__name__}: {str(exc)[:120]}"
            log.warning("市场数据取数失败 %s: %s", spec.code, res.failed[spec.code])
            continue
        if not bars:
            res.failed[spec.code] = "数据源未返回任何日线（可能无行情订阅权限）"
            continue
        res.bars_by_code[spec.code] = bars
        last, prev = bars[-1], (bars[-2] if len(bars) > 1 else None)
        res.latest[spec.code] = MarketBar(
            instrument_code=spec.code, instrument_name=spec.name,
            data_as_of=str(last.get("date")), fetched_at=fetched_at,
            open=_d(last.get("o")), high=_d(last.get("h")), low=_d(last.get("l")),
            close=_d(last.get("c")),
            previous_close=_d(prev.get("c")) if prev else None,
            volume=last.get("v"),
            delayed=True, quality_status=FRESH,
            raw_json=raw_capture.to_raw_json({"instrument": spec.code,
                                              "last_bar": last, "bars": len(bars)}),
        )
    return res


def apply_freshness(latest: dict[str, MarketBar], *, today_et: str,
                    max_lag_days: int = 4) -> None:
    """按数据时间标注新鲜度（就地修改）。

    max_lag_days 默认 4：覆盖长周末 + 节假日。超出即 stale ——
    「刚抓回来」不等于「数据是新的」，这是本模块最重要的一条区分。
    """
    from datetime import date as _date
    try:
        t = _date.fromisoformat(today_et)
    except (TypeError, ValueError):
        return
    for bar in latest.values():
        try:
            lag = (t - _date.fromisoformat(bar.data_as_of)).days
        except (TypeError, ValueError):
            bar.quality_status = ERROR
            bar.quality_note = "数据日期非法"
            continue
        if lag > max_lag_days:
            bar.quality_status = STALE
            bar.quality_note = f"数据为 {bar.data_as_of}，距今 {lag} 天"


def cross_source_consistency(latest: dict[str, MarketBar]) -> str | None:
    """各数据源时间是否一致。不一致会让「同一时点的市场状态」变成拼接产物。"""
    dates = {b.data_as_of for b in latest.values() if b.data_as_of}
    if len(dates) <= 1:
        return None
    return f"各标的数据时间不一致：{'、'.join(sorted(dates))}"
