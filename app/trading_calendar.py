"""交易日历与前瞻窗口（纯函数）。

**绝不用自然日代替交易日**。「5 天后」和「5 个交易日后」在含长周末或假期的
区间里能差出 3 天，而回撤统计对窗口边界很敏感 —— 用自然日会让同一条规则在
不同月份得到不可比的结果。

交易日来源：**实际存在的日线**。不硬编码假期表 —— 硬编码表会过时，而且
「那天有没有行情」这件事，数据本身就是最可靠的证据。代价是标的停牌会被误当成
非交易日，因此基准日历取自流动性最好的标的（QQQ），而不是账户持仓。
"""
from __future__ import annotations

import bisect
from datetime import date, datetime, timedelta, timezone
from typing import Iterable, Sequence

#: 基准日历标的优先级。QQQ 是市场层标的，几乎不会停牌。
BENCHMARK_SYMBOLS = ("QQQ", "SPY", "SOXX")

#: 标准前瞻窗口（交易日）。
FORWARD_WINDOWS = (1, 3, 5, 10, 20)


class TradingCalendar:
    """一份升序的交易日列表，支持「第 N 个交易日之后」查询。"""

    def __init__(self, days: Iterable[str]):
        # 去重 + 升序。日期一律 YYYY-MM-DD。
        self.days: list[str] = sorted({str(d)[:10] for d in days if d})

    def __len__(self) -> int:
        return len(self.days)

    @property
    def first(self) -> str | None:
        return self.days[0] if self.days else None

    @property
    def last(self) -> str | None:
        return self.days[-1] if self.days else None

    def is_trading_day(self, d: str) -> bool:
        i = bisect.bisect_left(self.days, str(d)[:10])
        return i < len(self.days) and self.days[i] == str(d)[:10]

    def index_of(self, d: str) -> int | None:
        """d 所在的位置。d 非交易日时返回它**之后**第一个交易日的位置。

        取「之后」而不是「之前」：事件发生在周六，它的观察起点应当是下周一，
        用上周五会把窗口悄悄前移一天。
        """
        if not self.days:
            return None
        i = bisect.bisect_left(self.days, str(d)[:10])
        return i if i < len(self.days) else None

    def offset(self, d: str, n: int) -> str | None:
        """d 之后第 n 个交易日。数据不够则返回 None（**不外推**）。"""
        i = self.index_of(d)
        if i is None:
            return None
        j = i + n
        return self.days[j] if 0 <= j < len(self.days) else None

    def between(self, start: str, end: str) -> list[str]:
        """[start, end] 闭区间内的交易日。"""
        if not self.days:
            return []
        lo = bisect.bisect_left(self.days, str(start)[:10])
        hi = bisect.bisect_right(self.days, str(end)[:10])
        return self.days[lo:hi]

    def window(self, start: str, n: int) -> list[str]:
        """从 start（含）起的 n 个交易日。不足则返回已有的部分。"""
        i = self.index_of(start)
        if i is None:
            return []
        return self.days[i:i + n + 1]

    def count_between(self, start: str, end: str) -> int:
        return len(self.between(start, end))

    def has_full_window(self, d: str, n: int) -> bool:
        """d 之后是否已经积累了完整的 n 个交易日。

        这是复盘资格判定的核心：窗口没走完就出结论，等于用半截数据下判断。
        """
        return self.offset(d, n) is not None


def from_db(db, symbols: Sequence[str] = BENCHMARK_SYMBOLS,
            *, fallback_any: bool = True) -> TradingCalendar:
    """从 daily_bars 构造交易日历。

    按 BENCHMARK_SYMBOLS 顺序取第一个有数据的；都没有时（早期部署、纯持仓账户）
    退化为「所有标的日期的并集」并在日志里说明 —— 并集会把个别标的的停牌日
    也算成交易日，但总比没有日历强。
    """
    for sym in symbols:
        try:
            rows = db.get_daily_bars(sym, limit=100000)
        except Exception:  # noqa: BLE001
            rows = []
        if len(rows) >= 20:
            return TradingCalendar(r["date"] for r in rows)
    if not fallback_any:
        return TradingCalendar([])
    try:
        return TradingCalendar(db.all_bar_dates())
    except Exception:  # noqa: BLE001
        return TradingCalendar([])


def et_date_of(ts: str | datetime, tz) -> str | None:
    """时间戳 -> 美东交易日（YYYY-MM-DD）。

    时区必须显式处理：UTC 的 2026-07-29T01:00 是美东 2026-07-28，
    按 UTC 日期归档会把盘后事件错记到第二天。
    """
    if ts is None:
        return None
    if isinstance(ts, str):
        raw = ts.strip()
        if len(raw) == 10 and raw[4] == "-":
            return raw               # 已经是日期
        try:
            dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return None
    else:
        dt = ts
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    try:
        return dt.astimezone(tz).date().isoformat()
    except (TypeError, ValueError):
        return dt.date().isoformat()


def minutes_between(a: str | None, b: str | None) -> int | None:
    """两个时间戳相差的分钟数（b - a）。任一缺失或非法返回 None。"""
    if not a or not b:
        return None
    try:
        ta = datetime.fromisoformat(str(a).replace("Z", "+00:00"))
        tb = datetime.fromisoformat(str(b).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if ta.tzinfo is None:
        ta = ta.replace(tzinfo=timezone.utc)
    if tb.tzinfo is None:
        tb = tb.replace(tzinfo=timezone.utc)
    return int((tb - ta).total_seconds() // 60)
