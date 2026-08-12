"""日线「缺了就补」的纯函数：目标交易日推算 + 尝试退避。

背景：daily_bars 曾长期为空（cron 每天只跑一次，撞上 gateway 故障就整天没有），
导致 stop_levels 为空、R7 永不触发。这些函数是兜底补拉的判定核心。
"""
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from app.sync import (
    bump_attempt, expected_last_bar_date, should_attempt, stale_symbols,
)

ET = ZoneInfo("America/New_York")


def _utc(y, m, d, h, mi=0):
    return datetime(y, m, d, h, mi, tzinfo=timezone.utc)


# ---------------- 目标交易日推算 ----------------

def test_before_close_expects_previous_day():
    """盘中（10:00 ET 周四）最新完整日线应是周三。"""
    assert expected_last_bar_date(_utc(2026, 7, 16, 14), ET).isoformat() == "2026-07-15"


def test_after_close_expects_today():
    """收盘后（16:30 ET 周四）应期望当天日线。"""
    assert expected_last_bar_date(_utc(2026, 7, 16, 20, 30), ET).isoformat() == "2026-07-16"


def test_close_boundary_needs_settlement_margin():
    """16:00 整还没到 16:15 余量 → 仍期望昨天，避免收盘瞬间空拉。"""
    assert expected_last_bar_date(_utc(2026, 7, 16, 20, 0), ET).isoformat() == "2026-07-15"
    assert expected_last_bar_date(_utc(2026, 7, 16, 20, 15), ET).isoformat() == "2026-07-16"


def test_weekend_falls_back_to_friday():
    for when in (_utc(2026, 7, 18, 14), _utc(2026, 7, 19, 23)):   # 周六、周日
        assert expected_last_bar_date(when, ET).isoformat() == "2026-07-17"


def test_monday_premarket_falls_back_to_friday():
    """周一盘前：先退到周日，再退回周五。"""
    assert expected_last_bar_date(_utc(2026, 7, 20, 13), ET).isoformat() == "2026-07-17"


def test_expectation_follows_et_not_utc():
    """UTC 已是次日、ET 仍在当天盘中时，不得误期望「今天」的日线。"""
    # UTC 2026-07-17 01:00 = ET 2026-07-16 21:00（周四盘后）
    assert expected_last_bar_date(_utc(2026, 7, 17, 1), ET).isoformat() == "2026-07-16"
    # UTC 2026-07-16 12:00 = ET 08:00（周四盘前）→ 仍是周三
    assert expected_last_bar_date(_utc(2026, 7, 16, 12), ET).isoformat() == "2026-07-15"


# ---------------- 落后判定 ----------------

def test_stale_detects_missing_and_behind():
    latest = {"KMEM": None, "DRAM": "2026-07-14", "MRVL": "2026-07-15", "BMNR": "2026-07-15"}
    assert stale_symbols(latest, "2026-07-15") == ["DRAM", "KMEM"]


def test_nothing_stale_when_all_current():
    assert stale_symbols({"MRVL": "2026-07-15"}, "2026-07-15") == []


def test_ahead_of_expected_is_not_stale():
    assert stale_symbols({"MRVL": "2026-07-16"}, "2026-07-15") == []


# ---------------- 尝试退避（节假日收敛） ----------------

def test_first_attempt_always_allowed():
    assert should_attempt(None, "2026-07-16") is True


def test_attempts_stop_at_limit():
    assert should_attempt("2026-07-16|2", "2026-07-16") is True
    assert should_attempt("2026-07-16|3", "2026-07-16") is False


def test_new_target_date_resets_attempts():
    """节假日试满 3 次后停；次日目标日期变化 → 重新开始。"""
    assert should_attempt("2026-07-16|3", "2026-07-17") is True


def test_corrupt_state_does_not_wedge():
    assert should_attempt("garbage", "2026-07-16") is True
    assert should_attempt("2026-07-16|abc", "2026-07-16") is True


def test_bump_counts_within_same_day():
    assert bump_attempt(None, "2026-07-16") == "2026-07-16|1"
    assert bump_attempt("2026-07-16|1", "2026-07-16") == "2026-07-16|2"


def test_bump_resets_on_new_day():
    assert bump_attempt("2026-07-16|3", "2026-07-17") == "2026-07-17|1"


def test_holiday_converges_to_three_attempts():
    """整条退避链路：节假日目标日期不变，第 4 次起不再打扰 IBKR。"""
    state, expected, tries = None, "2026-07-03", 0
    for _ in range(10):
        if not should_attempt(state, expected):
            break
        state = bump_attempt(state, expected)
        tries += 1
    assert tries == 3 and state == "2026-07-03|3"


# ---------------- 端到端：只在落后时才碰 IBKR ----------------

from decimal import Decimal

from app.db import Database
from app.notify import Notifier
from app.sync import SyncService


class _BarClient:
    """记录被请求的标的，返回到 through_date 为止的日线。"""
    def __init__(self, through_date="2026-07-16"):
        self.calls = []
        self.through = through_date

    def fetch_daily_bars(self, symbol, start, end):
        self.calls.append(symbol)
        return [{"date": self.through, "o": Decimal("10"), "h": Decimal("11"),
                 "l": Decimal("9"), "c": Decimal("10.5"), "v": 1000}]


def _svc(make_config, client):
    cfg = make_config()
    db = Database(cfg.db_path)
    db.init_schema()
    db.insert_positions_snapshot("2026-07-16T20:00:00+00:00", [
        {"symbol": "KMEM", "qty": Decimal("100"), "avg_cost": Decimal("19"),
         "market_value": Decimal("1900"), "upl": Decimal("0")},
    ])
    return cfg, db, SyncService(cfg, db, client, Notifier([], db))


AFTER_CLOSE = _utc(2026, 7, 16, 20, 30)     # 16:30 ET 周四 → 期望 2026-07-16


def test_backfill_pulls_when_bars_missing(make_config):
    client = _BarClient()
    cfg, db, svc = _svc(make_config, client)
    assert svc.ensure_daily_bars_fresh(AFTER_CLOSE) is True
    assert client.calls == ["KMEM"]
    assert db.latest_bar_date("KMEM") == "2026-07-16"


def test_backfill_skips_when_bars_current(make_config):
    """已是最新 → 一次 IBKR 请求都不该发（这是 30 分钟轮询的零开销前提）。"""
    client = _BarClient()
    cfg, db, svc = _svc(make_config, client)
    db.upsert_daily_bars("KMEM", [{"date": "2026-07-16", "o": 10, "h": 11, "l": 9,
                                   "c": 10, "v": 1}])
    assert svc.ensure_daily_bars_fresh(AFTER_CLOSE) is False
    assert client.calls == []


def test_backfill_stops_after_three_failed_attempts(make_config):
    """节假日：IBKR 只有 07-15 的数据，试 3 次后不再打扰。"""
    client = _BarClient(through_date="2026-07-15")
    cfg, db, svc = _svc(make_config, client)
    for _ in range(5):
        svc.ensure_daily_bars_fresh(AFTER_CLOSE)
    assert client.calls == ["KMEM"] * 3


def test_backfill_reports_no_advance_when_data_unchanged(make_config):
    """拉到的还是旧日期 → 返回 False，不触发止损位重算。"""
    client = _BarClient(through_date="2026-07-15")
    cfg, db, svc = _svc(make_config, client)
    db.upsert_daily_bars("KMEM", [{"date": "2026-07-15", "o": 10, "h": 11, "l": 9,
                                   "c": 10, "v": 1}])
    assert svc.ensure_daily_bars_fresh(AFTER_CLOSE) is False


def test_backfill_noop_without_positions(make_config):
    cfg = make_config()
    db = Database(cfg.db_path)
    db.init_schema()
    client = _BarClient()
    svc = SyncService(cfg, db, client, Notifier([], db))
    assert svc.ensure_daily_bars_fresh(AFTER_CLOSE) is False
    assert client.calls == []
