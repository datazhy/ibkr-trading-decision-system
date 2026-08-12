"""美股市场时段判断（纯函数）。

一个交易日在美东时间分为：
    盘前  PRE        04:00 – 09:30 ET
    盘中  REGULAR    09:30 – 16:00 ET
    盘后  POST       16:00 – 20:00 ET
    隔夜  OVERNIGHT  20:00 – 次日 04:00 ET（美股隔夜交易时段）
    休市  CLOSED     更新窗口之外

**更新窗口**（用户口径）：北京时间周一 08:00 到周六 08:00。
换算成美东即 **周日 20:00 ET 到周五 20:00 ET**（连续，含每晚隔夜）。
窗口内一切正常更新（含推送预警）；窗口外（周末）不更新、不推送。

用途（三处共用同一判断，避免各写各的）：
  1. 调度器：更新窗口外停更（周末不刷、不推送）
  2. 行情展示：区分盘前/盘中/盘后/隔夜价格
  3. 数据新鲜度：休市时「行情未更新」是正常的，不该报过期

常规交易时段额外经过 NYSE 交易日与提前收盘门禁：休市日不能被判成 REGULAR，
提前收盘后也不能继续使用盘中确认语义。更新窗口仍按用户口径保持不变。
"""
from __future__ import annotations

from datetime import datetime, time, timedelta

# 时段
PRE = "PRE"                # 盘前
REGULAR = "REGULAR"        # 盘中
POST = "POST"              # 盘后
OVERNIGHT = "OVERNIGHT"    # 隔夜
CLOSED = "CLOSED"          # 休市（更新窗口外，即周末）

_SESSION_LABEL = {
    PRE: "盘前", REGULAR: "盘中", POST: "盘后", OVERNIGHT: "隔夜", CLOSED: "休市",
}

# 美东时段边界
_PRE_OPEN = time(4, 0)
_REGULAR_OPEN = time(9, 30)
_REGULAR_CLOSE = time(16, 0)
_POST_CLOSE = time(20, 0)     # 盘后结束 = 隔夜开始，也是更新窗口的每周边界


def session_label(session: str) -> str:
    return _SESSION_LABEL.get(session, session)


def is_update_window(now: datetime, market_tz) -> bool:
    """是否在更新窗口内：美东周日 20:00 → 周五 20:00（= 北京周一 08:00 → 周六 08:00）。

    窗口外（美东周五 20:00 到周日 20:00 的周末）不更新、不推送。
    """
    et = now.astimezone(market_tz)
    wd = et.weekday()          # 周一=0 … 周日=6
    t = et.time()
    if wd == 4 and t >= _POST_CLOSE:      # 周五 20:00 之后
        return False
    if wd == 5:                            # 周六整天
        return False
    if wd == 6 and t < _POST_CLOSE:        # 周日 20:00 之前
        return False
    return True


def in_reopen_grace(now: datetime, market_tz, minutes: int = 60) -> bool:
    """更新窗口刚重开的宽限期（美东周日 20:00 起 minutes 分钟）。

    周末停更期间 IB Gateway 已按每日 logoff 登出，窗口重开瞬间必然连不上、
    必然由 watchdog 重启一次恢复 —— 属**预期内**，此时不该推送「Gateway 失联」。
    与 scripts/watchdog.py 的 in_reopen_grace 同口径（那边负责宿主机侧的静默）。
    """
    et = now.astimezone(market_tz)
    if et.weekday() != 6:                  # 仅周日
        return False
    cur = et.hour * 60 + et.minute
    start = _POST_CLOSE.hour * 60          # 20:00 ET
    return start <= cur < start + minutes


def market_session(now: datetime, market_tz) -> str:
    """当前市场时段。更新窗口外一律 CLOSED。"""
    if not is_update_window(now, market_tz):
        return CLOSED
    et = now.astimezone(market_tz)
    t = et.time()
    if _PRE_OPEN <= t < _REGULAR_OPEN:
        return PRE
    if _REGULAR_OPEN <= t < _REGULAR_CLOSE:
        # 延迟 import 避免把更新窗口的轻量判断绑死在日历模块上。
        from .us_market_calendar import regular_close_time
        close = regular_close_time(et.date())
        if close is None:
            return CLOSED
        if t < close:
            return REGULAR
        return POST
    if _REGULAR_CLOSE <= t < _POST_CLOSE:
        return POST
    return OVERNIGHT           # 20:00–次日 04:00（窗口内的夜间）


def should_sync(now: datetime, market_tz) -> bool:
    """本轮同步（持仓/行情/PnL/规则/推送）是否应当执行。

    更新窗口内一律执行；窗口外（周末）完全停 —— 价格不动，评估结论不变，
    也不该在周末推送预警。用户口径：其它时间内容不用更新。
    """
    return is_update_window(now, market_tz)


def next_open_beijing(now: datetime, market_tz, display_tz) -> str:
    """下一次开盘（更新窗口开启，即美东周日 20:00）对应的北京时间。

    周末休市时显示。周日 20:00 ET = 北京周一 08:00。
    """
    et = now.astimezone(market_tz)
    # 找到下一个「周日 20:00 ET」（若当前已在窗口内则不需要，但调用方只在 CLOSED 时用）
    candidate = et.replace(hour=_POST_CLOSE.hour, minute=0, second=0, microsecond=0)
    # 先对齐到周日
    while candidate.weekday() != 6:
        candidate += timedelta(days=1)
    if candidate <= et:
        candidate += timedelta(days=7)
    return candidate.astimezone(display_tz).strftime("%m-%d %H:%M")
