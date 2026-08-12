"""watchdog 决策纯函数测试：阈值/宽限/冷却/每小时上限/去重/状态机。

不依赖 docker/gateway —— decide() 是纯函数。
"""
from datetime import datetime, timedelta, timezone

import importlib.util
import sys
from pathlib import Path

# scripts/ 不是包，按路径加载（须先注册 sys.modules，dataclass 解析注解时要用）
_spec = importlib.util.spec_from_file_location(
    "watchdog", Path(__file__).resolve().parents[1] / "scripts" / "watchdog.py")
wd = importlib.util.module_from_spec(_spec)
sys.modules["watchdog"] = wd
_spec.loader.exec_module(wd)

NOW = datetime(2026, 7, 15, 12, 0, tzinfo=timezone.utc)


def _st(**kw):
    s = wd.WDState()
    for k, v in kw.items():
        setattr(s, k, v)
    return s


# ---------------- 健康路径 ----------------

def test_healthy_stays_none():
    assert wd.decide(_st(state="OK"), True, NOW)[0] == wd.ACT_NONE


def test_healthy_after_failure_is_recovered():
    act, _ = wd.decide(_st(state="FAILING", fail_count=3, notified_fault=True), True, NOW)
    assert act == wd.ACT_RECOVERED


def test_healthy_after_giveup_is_recovered():
    act, _ = wd.decide(_st(state="GIVEN_UP", notified_giveup=True), True, NOW)
    assert act == wd.ACT_RECOVERED


# ---------------- 连续失败阈值 ----------------

def test_below_threshold_no_action():
    act, reason = wd.decide(_st(fail_count=0), False, NOW)
    assert act == wd.ACT_NONE and "1/3" in reason


def test_second_failure_still_no_action():
    assert wd.decide(_st(fail_count=1), False, NOW)[0] == wd.ACT_NONE


def test_third_failure_triggers_restart():
    act, reason = wd.decide(_st(fail_count=2), False, NOW)
    assert act == wd.ACT_RESTART and "连续失败 3 次" in reason


# ---------------- 启动宽限期 ----------------

def test_grace_period_blocks_restart():
    """刚重启 2 分钟内（<5 分钟宽限），等 TOTP 登录，不重复重启。"""
    st = _st(fail_count=2, last_restart=(NOW - timedelta(minutes=2)).isoformat())
    act, reason = wd.decide(st, False, NOW)
    assert act == wd.ACT_NONE and "宽限" in reason


# ---------------- 冷却 ----------------

def test_cooldown_blocks_restart():
    """距上次重启 10 分钟（<15 分钟冷却）→ 不重启。"""
    st = _st(fail_count=2, last_restart=(NOW - timedelta(minutes=10)).isoformat())
    act, reason = wd.decide(st, False, NOW)
    assert act == wd.ACT_NONE and "冷却" in reason


def test_after_cooldown_restarts():
    st = _st(fail_count=2, last_restart=(NOW - timedelta(minutes=20)).isoformat(),
             restarts=[(NOW - timedelta(minutes=20)).isoformat()])
    assert wd.decide(st, False, NOW)[0] == wd.ACT_RESTART


# ---------------- 每小时上限 ----------------

def test_hourly_limit_gives_up():
    """1 小时内已重启 2 次 → 停止自动重启 + CRIT。"""
    st = _st(fail_count=2,
             last_restart=(NOW - timedelta(minutes=20)).isoformat(),
             restarts=[(NOW - timedelta(minutes=50)).isoformat(),
                       (NOW - timedelta(minutes=20)).isoformat()])
    act, reason = wd.decide(st, False, NOW)
    assert act == wd.ACT_GIVE_UP and "停止自动重启" in reason


def test_giveup_not_repeated():
    st = _st(state="GIVEN_UP", fail_count=2,
             last_restart=(NOW - timedelta(minutes=20)).isoformat(),
             restarts=[(NOW - timedelta(minutes=50)).isoformat(),
                       (NOW - timedelta(minutes=20)).isoformat()])
    act, _ = wd.decide(st, False, NOW)
    assert act == wd.ACT_NONE  # 已放弃，不再重复动作


def test_old_restarts_fall_out_of_window():
    """2 小时前的重启不计入每小时上限。"""
    st = _st(fail_count=2,
             last_restart=(NOW - timedelta(minutes=20)).isoformat(),
             restarts=[(NOW - timedelta(hours=2)).isoformat(),
                       (NOW - timedelta(minutes=20)).isoformat()])
    assert wd.decide(st, False, NOW)[0] == wd.ACT_RESTART


def test_recent_restarts_window():
    rs = [(NOW - timedelta(hours=2)).isoformat(), (NOW - timedelta(minutes=10)).isoformat()]
    assert len(wd.recent_restarts(rs, NOW)) == 1


# ---------------- 每日维护窗口（IBGA 23:45 ET logoff） ----------------

def test_in_window_normal_range():
    assert wd.in_window(cur_min=600, start_min=540, end_min=660) is True     # 10:00 ∈ [9:00,11:00]
    assert wd.in_window(cur_min=500, start_min=540, end_min=660) is False


def test_in_window_crossing_midnight():
    """23:40 → 00:15 跨午夜。"""
    s, e = wd._hhmm_to_min("23:40"), wd._hhmm_to_min("00:15")
    assert wd.in_window(wd._hhmm_to_min("23:45"), s, e) is True   # logoff 时刻
    assert wd.in_window(wd._hhmm_to_min("23:50"), s, e) is True
    assert wd.in_window(wd._hhmm_to_min("00:05"), s, e) is True   # 午夜后
    assert wd.in_window(wd._hhmm_to_min("00:20"), s, e) is False  # 窗口外
    assert wd.in_window(wd._hhmm_to_min("12:00"), s, e) is False  # 盘中


def test_et_minutes_follows_dst():
    """ET 换算须跟随夏令时：同一 UTC 时刻在 EDT/EST 下的 ET 不同。"""
    edt = datetime(2026, 7, 16, 3, 45, tzinfo=timezone.utc)   # 夏令时 UTC-4 → 23:45 ET
    est = datetime(2026, 1, 16, 4, 45, tzinfo=timezone.utc)   # 冬令时 UTC-5 → 23:45 ET
    assert wd._et_minutes(edt) == wd._hhmm_to_min("23:45")
    assert wd._et_minutes(est) == wd._hhmm_to_min("23:45")


def test_maintenance_window_covers_logoff_both_dst():
    """两种时令下的 23:45 ET logoff 都应落在维护窗口内（固定 UTC 窗口会错位）。"""
    assert wd.in_maintenance_window(datetime(2026, 7, 16, 3, 45, tzinfo=timezone.utc)) is True
    assert wd.in_maintenance_window(datetime(2026, 1, 16, 4, 45, tzinfo=timezone.utc)) is True


def test_maintenance_window_excludes_trading_hours():
    """盘中故障不得被静默。"""
    assert wd.in_maintenance_window(datetime(2026, 7, 16, 18, 0, tzinfo=timezone.utc)) is False  # 14:00 ET


# ---------------- 更新窗口（周末暂停）----------------

def test_update_window_open_on_weekday():
    """周三盘中在更新窗口内（正常探活）。2026-07-15 是周三。"""
    assert wd.in_update_window(datetime(2026, 7, 15, 18, 0, tzinfo=timezone.utc)) is True  # 14:00 ET 周三


def test_update_window_closed_on_saturday():
    """周六休市，窗口外（watchdog 应暂停）。2026-07-18 是周六。"""
    assert wd.in_update_window(datetime(2026, 7, 18, 16, 0, tzinfo=timezone.utc)) is False  # 12:00 ET 周六


def test_update_window_closes_friday_2000et():
    """周五 20:00 ET 之后进入周末窗口外。2026-07-17 周五。"""
    # 20:30 ET 周五 = 00:30 UTC 周六
    assert wd.in_update_window(datetime(2026, 7, 18, 0, 30, tzinfo=timezone.utc)) is False
    # 19:00 ET 周五 = 23:00 UTC 周五：仍在窗口内
    assert wd.in_update_window(datetime(2026, 7, 17, 23, 0, tzinfo=timezone.utc)) is True


def test_update_window_reopens_sunday_2000et():
    """周日 20:00 ET 窗口重开（北京周一 08:00）。2026-07-19 周日。"""
    # 19:00 ET 周日 = 23:00 UTC 周日：仍关闭
    assert wd.in_update_window(datetime(2026, 7, 19, 23, 0, tzinfo=timezone.utc)) is False
    # 20:30 ET 周日 = 00:30 UTC 周一：已重开
    assert wd.in_update_window(datetime(2026, 7, 20, 0, 30, tzinfo=timezone.utc)) is True


# ---------------- 状态持久化 ----------------

def test_state_roundtrip(tmp_path):
    p = tmp_path / "wd.json"
    st = _st(state="FAILING", fail_count=2, last_detail="L3 API 握手失败")
    st.save(p)
    loaded = wd.WDState.load(p)
    assert loaded.state == "FAILING" and loaded.fail_count == 2
    assert loaded.last_detail == "L3 API 握手失败"


def test_state_load_corrupt_returns_default(tmp_path):
    p = tmp_path / "bad.json"
    p.write_text("{not json")
    assert wd.WDState.load(p).state == "UNKNOWN"


def test_state_file_has_no_secrets(tmp_path):
    """状态文件不得含任何凭证字段。"""
    p = tmp_path / "wd.json"
    _st(state="OK", last_detail="L1-L4 全部正常").save(p)
    txt = p.read_text().lower()
    for bad in ("password", "totp", "userid", "secret", "token"):
        assert bad not in txt


# ---------------- 窗口重开宽限期（周末后不推送例行告警）----------------

def test_reopen_grace_covers_sunday_window_open():
    """周日 20:00 ET 窗口重开后 1 小时内属例行恢复，不该推送。

    2026-07-26 是周日；20:02 ET = 2026-07-27 00:02 UTC。
    """
    assert wd.in_reopen_grace(datetime(2026, 7, 27, 0, 2, tzinfo=timezone.utc)) is True
    # 19:59 ET 周日（窗口还没开）不在宽限期
    assert wd.in_reopen_grace(datetime(2026, 7, 26, 23, 59, tzinfo=timezone.utc)) is False
    # 21:30 ET 周日（超过 60 分钟宽限）不再静默
    assert wd.in_reopen_grace(datetime(2026, 7, 27, 1, 30, tzinfo=timezone.utc)) is False


def test_reopen_grace_not_on_weekdays():
    """周中的故障必须照常告警，不能被宽限期误伤。"""
    # 周三 20:02 ET = 2026-07-23 00:02 UTC（周四 UTC）
    assert wd.in_reopen_grace(datetime(2026, 7, 23, 0, 2, tzinfo=timezone.utc)) is False
