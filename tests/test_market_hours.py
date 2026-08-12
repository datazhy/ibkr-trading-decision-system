"""市场时段判断：盘前/盘中/盘后/休市 + 周末停更 + 下次开盘。"""
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from app.market_hours import (
    CLOSED, OVERNIGHT, POST, PRE, REGULAR, is_update_window, market_session,
    next_open_beijing, session_label, should_sync,
)

ET = ZoneInfo("America/New_York")
CN = ZoneInfo("Asia/Shanghai")


def _utc(y, mo, d, h, mi=0):
    return datetime(y, mo, d, h, mi, tzinfo=timezone.utc)


# 2026-07-16 是周四。UTC+4=EDT。
def test_premarket():
    # ET 05:00 = UTC 09:00
    assert market_session(_utc(2026, 7, 16, 9, 0), ET) == PRE


def test_regular_hours():
    # ET 10:00 = UTC 14:00
    assert market_session(_utc(2026, 7, 16, 14, 0), ET) == REGULAR


def test_regular_open_boundary():
    # ET 09:30 整 = 盘中；09:29 = 盘前
    assert market_session(_utc(2026, 7, 16, 13, 30), ET) == REGULAR
    assert market_session(_utc(2026, 7, 16, 13, 29), ET) == PRE


def test_regular_close_boundary():
    # ET 16:00 整 = 盘后；15:59 = 盘中
    assert market_session(_utc(2026, 7, 16, 20, 0), ET) == POST
    assert market_session(_utc(2026, 7, 16, 19, 59), ET) == REGULAR


def test_postmarket():
    # ET 18:00 = UTC 22:00
    assert market_session(_utc(2026, 7, 16, 22, 0), ET) == POST


def test_post_close_to_overnight_boundary():
    # ET 20:00 整 = 隔夜（不再是休市）。周四 20:00 = UTC 周五 00:00
    assert market_session(_utc(2026, 7, 17, 0, 0), ET) == OVERNIGHT


def test_overnight_before_premarket():
    # 周四 ET 02:00 = UTC 06:00（盘前 04:00 之前）-> 隔夜
    assert market_session(_utc(2026, 7, 16, 6, 0), ET) == OVERNIGHT


def test_weekend_saturday_is_closed():
    # 2026-07-18 周六，ET 11:00 = UTC 15:00 —— 盘中时间但周末
    assert market_session(_utc(2026, 7, 18, 15, 0), ET) == CLOSED


def test_weekend_sunday_is_closed():
    # 2026-07-19 周日
    assert market_session(_utc(2026, 7, 19, 18, 0), ET) == CLOSED


def test_friday_evening_still_trades():
    # 周五盘后仍在更新窗口
    assert market_session(_utc(2026, 7, 17, 22, 0), ET) == POST
    assert is_update_window(_utc(2026, 7, 17, 22, 0), ET) is True


# ---------------- 停更判断 ----------------

def test_should_sync_weekday_yes():
    assert should_sync(_utc(2026, 7, 16, 14, 0), ET) is True    # 盘中
    assert should_sync(_utc(2026, 7, 16, 9, 0), ET) is True     # 盘前


def test_should_sync_weekend_no():
    assert should_sync(_utc(2026, 7, 18, 15, 0), ET) is False   # 周六
    assert should_sync(_utc(2026, 7, 19, 18, 0), ET) is False   # 周日


def test_should_sync_overnight_yes():
    # ET 02:00 周四深夜仍在更新窗口内（隔夜）
    assert should_sync(_utc(2026, 7, 16, 6, 0), ET) is True


# ---------------- 下次开盘（北京时间） ----------------

def test_next_open_from_weekend_is_monday_8am_beijing():
    # 周六休市 -> 下次开盘 = 周日 20:00 ET = 北京周一 08:00
    got = next_open_beijing(_utc(2026, 7, 18, 15, 0), ET, CN)   # 周六
    assert got == "07-20 08:00"        # 07-20 周一 08:00 北京


def test_next_open_from_sunday_before_open():
    # 周日 15:00 ET（窗口未开）-> 当天 20:00 ET = 北京周一 08:00
    got = next_open_beijing(_utc(2026, 7, 19, 19, 0), ET, CN)   # UTC 周日 19:00 = ET 15:00
    assert got == "07-20 08:00"


def test_should_sync_covers_full_week():
    # 更新窗口 = 周日 20:00 ET 到周五 20:00 ET，中间连续（含隔夜）
    assert should_sync(_utc(2026, 7, 20, 1, 0), ET) is True     # 周日 21:00 ET 隔夜
    assert should_sync(_utc(2026, 7, 22, 6, 0), ET) is True     # 周三 02:00 ET 隔夜


def test_session_labels():
    assert session_label(PRE) == "盘前"
    assert session_label(REGULAR) == "盘中"
    assert session_label(POST) == "盘后"
    assert session_label(OVERNIGHT) == "隔夜"
    assert session_label(CLOSED) == "休市"


def test_in_reopen_grace_app_side():
    """app 侧宽限期与 watchdog 同口径：周日 20:00 ET 起 60 分钟。"""
    from app.market_hours import in_reopen_grace
    assert in_reopen_grace(_utc(2026, 7, 27, 0, 2), ET) is True     # 周日 20:02 ET
    assert in_reopen_grace(_utc(2026, 7, 26, 23, 59), ET) is False  # 周日 19:59 ET，未开窗
    assert in_reopen_grace(_utc(2026, 7, 27, 1, 30), ET) is False   # 周日 21:30 ET，超宽限
    assert in_reopen_grace(_utc(2026, 7, 22, 18, 0), ET) is False   # 周三盘中不受影响

# ---------------- NYSE 交易日 / 提前收盘门禁 ----------------

def test_independence_day_observed_is_not_regular_session():
    # 2026-07-04 是周六，NYSE 于周五 07-03 补休；10:00 ET 不能判 REGULAR。
    assert market_session(_utc(2026, 7, 3, 14, 0), ET) == CLOSED
    # 补休前一天 07-02 是正常交易日，不应被误推成 13:00 提前收盘。
    assert market_session(_utc(2026, 7, 2, 18, 30), ET) == REGULAR


def test_early_close_stops_regular_session_at_1pm_et():
    # 2026-11-27 为感恩节后周五，13:00 ET 起已经不是常规盘中。
    assert market_session(_utc(2026, 11, 27, 17, 59), ET) == REGULAR
    assert market_session(_utc(2026, 11, 27, 18, 0), ET) == POST


def test_latest_completed_rth_day_skips_holiday_and_respects_early_close():
    from app.us_market_calendar import latest_completed_regular_day
    # 补休日当天上午，最近完整 RTH 是周四 07-02。
    assert latest_completed_regular_day(_utc(2026, 7, 3, 14, 0), ET).isoformat() == "2026-07-02"
    # 感恩节后周五 13:01 ET，当前提前收盘日已经完整结束。
    assert latest_completed_regular_day(_utc(2026, 11, 27, 18, 1), ET).isoformat() == "2026-11-27"
