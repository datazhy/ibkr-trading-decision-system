"""NYSE 常规交易日与提前收盘规则（纯函数、离线可用）。

这里只解决实时门禁需要回答的两个问题：某个美东日期是否有常规交易，
以及当天常规交易何时结束。前向复盘仍使用 :mod:`trading_calendar` 中的实际日线日历，
不把规则日历冒充历史行情事实。
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta

REGULAR_OPEN = time(9, 30)
REGULAR_CLOSE = time(16, 0)
EARLY_CLOSE = time(13, 0)

# 已知的整日特别休市；常规节假日由下面的规则计算。
_SPECIAL_CLOSURES = {
    date(2001, 9, 11), date(2001, 9, 12), date(2001, 9, 13), date(2001, 9, 14),
    date(2004, 6, 11),  # Reagan funeral
    date(2007, 1, 2),   # Ford funeral
    date(2012, 10, 29), date(2012, 10, 30),  # Hurricane Sandy
    date(2018, 12, 5),  # G.H.W. Bush funeral
}


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    d = date(year, month, 1)
    return d + timedelta(days=(weekday - d.weekday()) % 7 + 7 * (n - 1))


def _last_weekday(year: int, month: int, weekday: int) -> date:
    if month == 12:
        d = date(year + 1, 1, 1) - timedelta(days=1)
    else:
        d = date(year, month + 1, 1) - timedelta(days=1)
    return d - timedelta(days=(d.weekday() - weekday) % 7)


def _observed(d: date) -> date:
    if d.weekday() == 5:       # Saturday -> Friday
        return d - timedelta(days=1)
    if d.weekday() == 6:       # Sunday -> Monday
        return d + timedelta(days=1)
    return d


def _easter_sunday(year: int) -> date:
    """Gregorian Easter (Meeus/Jones/Butcher algorithm)."""
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31
    day = (h + l - 7 * m + 114) % 31 + 1
    return date(year, month, day)


def nyse_holidays(year: int) -> frozenset[date]:
    """返回会落在 ``year`` 内的 NYSE 常规整日休市日期。"""
    days: set[date] = set()
    # 同时计算相邻年份，覆盖元旦周六时落在上一年的 observed date。
    for y in (year - 1, year, year + 1):
        days.add(_observed(date(y, 1, 1)))
        if y >= 1998:
            days.add(_nth_weekday(y, 1, 0, 3))       # MLK Day
        days.add(_nth_weekday(y, 2, 0, 3))           # Presidents' Day
        days.add(_easter_sunday(y) - timedelta(days=2))  # Good Friday
        days.add(_last_weekday(y, 5, 0))              # Memorial Day
        if y >= 2022:
            days.add(_observed(date(y, 6, 19)))       # Juneteenth
        days.add(_observed(date(y, 7, 4)))
        days.add(_nth_weekday(y, 9, 0, 1))            # Labor Day
        days.add(_nth_weekday(y, 11, 3, 4))           # Thanksgiving
        days.add(_observed(date(y, 12, 25)))
    days.update(_SPECIAL_CLOSURES)
    return frozenset(d for d in days if d.year == year)


def is_trading_day(d: date) -> bool:
    return d.weekday() < 5 and d not in nyse_holidays(d.year)


def early_close_time(d: date) -> time | None:
    """NYSE 规则性 13:00 ET 提前收盘；非交易日返回 ``None``。"""
    if not is_trading_day(d):
        return None
    # 感恩节后周五。
    thanksgiving = _nth_weekday(d.year, 11, 3, 4)
    if d == thanksgiving + timedelta(days=1):
        return EARLY_CLOSE
    # 圣诞夜仅在其本身为交易日时提前收盘。
    if d.month == 12 and d.day == 24:
        return EARLY_CLOSE
    # 独立日前的 7/3 仅在其本身为交易日时提前收盘。若 7/4 落周末、
    # 7/3 已成为补休日，不把更早的 7/2 错推成提前收盘日。
    if d.month == 7 and d.day == 3:
        return EARLY_CLOSE
    return None


def regular_close_time(d: date) -> time | None:
    if not is_trading_day(d):
        return None
    return early_close_time(d) or REGULAR_CLOSE


def latest_completed_regular_day(now: datetime, market_tz) -> date:
    """截至 ``now`` 已完整结束的最近一个 NYSE 常规交易日。"""
    et = now.astimezone(market_tz)
    d = et.date()
    for _ in range(370):
        close = regular_close_time(d)
        if close is not None and (d < et.date() or et.time() >= close):
            return d
        d -= timedelta(days=1)
    raise RuntimeError("无法在一年内找到已完成的 NYSE 交易日")
