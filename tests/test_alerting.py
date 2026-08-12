"""告警生命周期：持续风险不得重复轰炸，状态变化必须立即通知。"""
from decimal import Decimal

import pytest

from app.alerting import (
    DAILY_SUMMARY, ESCALATION, FIRST_TRIGGER, RESOLVED, RETRIGGER, SUPPRESSED,
    WORSENED, AlertPolicy, decide_notify, decide_resolve, severity_rank, worsened,
)

TODAY = "2026-07-16"


def _st(**kw):
    base = {"state": "ACTIVE", "severity": "WARN", "notified_severity": "WARN",
            "notified_value": "0.78", "last_summary_date": TODAY}
    base.update(kw)
    return base


# ---------------- 通知判定 ----------------

def test_first_trigger_notifies():
    ok, why = decide_notify(None, severity="WARN", value=Decimal("0.78"), now_et_date=TODAY)
    assert ok is True and why == FIRST_TRIGGER


def test_steady_state_is_suppressed():
    """R1 集中度 78%，昨天是这个数今天还是 —— 不得重复推送。"""
    ok, why = decide_notify(_st(), severity="WARN", value=Decimal("0.78"), now_et_date=TODAY)
    assert ok is False and why == SUPPRESSED


def test_escalation_notifies_immediately():
    ok, why = decide_notify(_st(notified_severity="WARN"), severity="CRIT",
                            value=Decimal("0.78"), now_et_date=TODAY)
    assert ok is True and why == ESCALATION


def test_downgrade_does_not_notify():
    ok, why = decide_notify(_st(notified_severity="CRIT"), severity="WARN",
                            value=Decimal("0.78"), now_et_date=TODAY)
    assert ok is False and why == SUPPRESSED


def test_daily_summary_once_per_et_day():
    ok, why = decide_notify(_st(last_summary_date="2026-07-15"), severity="WARN",
                            value=Decimal("0.78"), now_et_date=TODAY)
    assert ok is True and why == DAILY_SUMMARY


def test_retrigger_after_resolve_notifies():
    ok, why = decide_notify(_st(state="RESOLVED"), severity="WARN",
                            value=Decimal("0.78"), now_et_date=TODAY)
    assert ok is True and why == RETRIGGER


# ---------------- 显著恶化 ----------------

def test_worsened_by_absolute_change():
    """集中度 78% -> 84%：涨了 6 个百分点，超过 5pp 阈值。"""
    ok, why = decide_notify(_st(notified_value="0.78"), severity="WARN",
                            value=Decimal("0.84"), now_et_date=TODAY)
    assert ok is True and why == WORSENED


def test_small_drift_is_not_worsening():
    """78% -> 80%：2 个百分点，不值得再叫一次。"""
    ok, why = decide_notify(_st(notified_value="0.78"), severity="WARN",
                            value=Decimal("0.80"), now_et_date=TODAY)
    assert ok is False and why == SUPPRESSED


def test_improvement_never_notifies_as_worsening():
    ok, why = decide_notify(_st(notified_value="0.78"), severity="WARN",
                            value=Decimal("0.60"), now_et_date=TODAY)
    assert ok is False and why == SUPPRESSED


def test_worsened_by_relative_change_on_small_values():
    """小数值靠相对阈值兜底：0.021 -> 0.030 是 +43%，绝对只差 0.9pp。"""
    assert worsened(Decimal("0.021"), Decimal("0.030")) is True


def test_worsen_thresholds_are_configurable():
    strict = AlertPolicy(worsen_abs=Decimal("0.01"), worsen_rel=Decimal("0.05"))
    assert worsened(Decimal("0.78"), Decimal("0.80"), strict) is True
    loose = AlertPolicy(worsen_abs=Decimal("0.50"), worsen_rel=Decimal("9"))
    assert worsened(Decimal("0.78"), Decimal("0.84"), loose) is False


def test_worsened_handles_missing_values():
    assert worsened(None, Decimal("1")) is False
    assert worsened(Decimal("1"), None) is False


def test_notify_without_value_falls_back_to_other_rules():
    """没有 value 的告警（如 R6 冷静期内交易）仍按首触发/升级/日摘要判定。"""
    ok, why = decide_notify(_st(notified_value=None), severity="WARN",
                            value=None, now_et_date=TODAY)
    assert ok is False and why == SUPPRESSED


# ---------------- 恢复 ----------------

def test_resolve_notifies_when_was_active():
    assert decide_resolve(_st(state="ACTIVE")) is True


def test_resolve_silent_when_already_resolved():
    assert decide_resolve(_st(state="RESOLVED")) is False


def test_resolve_silent_when_never_seen():
    assert decide_resolve(None) is False


def test_severity_rank_order():
    assert severity_rank("CRIT") > severity_rank("WARN") > severity_rank("INFO")
    assert severity_rank(None) == 0


# ---------------- 端到端：持续风险不得重复轰炸 ----------------

from datetime import datetime, timedelta, timezone

from app.db import Database
from app.engine import RuleEngine
from app.notify import Notifier

NOW = datetime(2026, 7, 16, 14, 0, tzinfo=timezone.utc)   # ET 10:00


class _Spy(Notifier):
    """记录实际推送，不发网络请求。"""
    def __init__(self, db):
        super().__init__([], db)
        self.sent = []

    def send(self, title, body):
        self.sent.append((title, body))
        return True


def _engine(make_config):
    cfg = make_config(ib_account="U1")
    db = Database(cfg.db_path)
    db.init_schema()
    spy = _Spy(db)
    return cfg, db, RuleEngine(cfg, db, spy), spy


def _seed_concentrated(db, ts, equity="10000", mv="9000"):
    """单标的占 90% -> R1 触发。"""
    db.insert_balances_snapshot(ts, {
        "equity": Decimal(equity), "cash": Decimal("0"), "margin_balance": Decimal("0"),
        "buying_power": Decimal("0"), "account": "U1"}, sync_run_id="b1")
    db.insert_positions_snapshot(ts, [{
        "symbol": "KMEM", "qty": Decimal("100"), "avg_cost": Decimal("90"),
        "market_value": Decimal(mv), "upl": Decimal("0"),
        "price": Decimal("90"), "price_source": "LAST", "price_quality": "DELAYED",
        "quote_as_of": ts}], sync_run_id="b1")


def test_persistent_risk_notifies_once_not_every_run(make_config):
    """R1 持续 90% 不变：首次通知，之后同一天内一律静默。

    旧行为是固定 4h 去重窗口 —— 一天叫 6 次同一条，读的人只会关掉通知。
    """
    cfg, db, eng, spy = _engine(make_config)
    _seed_concentrated(db, NOW.isoformat())
    eng.evaluate_and_dispatch(NOW)
    assert len(spy.sent) == 1                       # 首次触发

    for i in range(1, 20):                          # 之后每分钟评估一次
        eng.evaluate_and_dispatch(NOW + timedelta(minutes=i))
    assert len(spy.sent) == 1                       # 仍然只有那一条


def test_evaluation_audit_continues_while_notifications_suppressed(make_config):
    """通知被抑制，但每轮评估仍完整记录 —— 少发的是通知，不是审计。"""
    cfg, db, eng, spy = _engine(make_config)
    _seed_concentrated(db, NOW.isoformat())
    for i in range(5):
        eng.evaluate_and_dispatch(NOW + timedelta(minutes=i))
    with db.connect() as c:
        runs = c.execute("SELECT COUNT(*) n FROM rule_runs").fetchone()["n"]
    assert runs == 5                                # 5 次评估全部落库
    assert len(spy.sent) == 1                       # 只推送一次


def test_daily_summary_next_et_day(make_config):
    cfg, db, eng, spy = _engine(make_config)
    _seed_concentrated(db, NOW.isoformat())
    eng.evaluate_and_dispatch(NOW)
    eng.evaluate_and_dispatch(NOW + timedelta(days=1))   # 次日
    assert len(spy.sent) == 2
    assert "每日摘要" in spy.sent[1][0]
    assert "持续存在" in spy.sent[1][1] or "持续" in spy.sent[1][1]


def test_r1_worsening_does_not_repush_same_day(make_config):
    """R1 属于每日限一次：集中度 90% -> 98% 仍是同一件事，当天不再推。

    （非 capped 规则的恶化推送由 test_non_capped_rule_still_notifies_on_worsening 覆盖）
    """
    cfg, db, eng, spy = _engine(make_config)
    _seed_concentrated(db, NOW.isoformat(), mv="9000")     # 90%
    eng.evaluate_and_dispatch(NOW)
    later = NOW + timedelta(minutes=5)
    _seed_concentrated(db, later.isoformat(), mv="9800")   # 98%，+8pp
    eng.evaluate_and_dispatch(later)
    assert len(spy.sent) == 1
    assert "规则引用：R1" in spy.sent[0][1]


def test_small_drift_stays_silent(make_config):  # noqa: D103
    cfg, db, eng, spy = _engine(make_config)
    _seed_concentrated(db, NOW.isoformat(), mv="9000")     # 90%
    eng.evaluate_and_dispatch(NOW)
    later = NOW + timedelta(minutes=5)
    _seed_concentrated(db, later.isoformat(), mv="9200")   # 92%，仅 +2pp
    eng.evaluate_and_dispatch(later)
    assert len(spy.sent) == 1


def test_resolution_notifies_and_retrigger_notifies_again(make_config):
    cfg, db, eng, spy = _engine(make_config)
    _seed_concentrated(db, NOW.isoformat(), mv="9000")     # 触发
    eng.evaluate_and_dispatch(NOW)
    assert len(spy.sent) == 1

    t2 = NOW + timedelta(minutes=5)
    _seed_concentrated(db, t2.isoformat(), mv="1000")      # 10% -> 恢复
    eng.evaluate_and_dispatch(t2)
    assert len(spy.sent) == 2 and "已解除" in spy.sent[1][0]

    # 冷却期内（30 分钟）再次触发：不推送，但状态照常回到 ACTIVE
    t3 = NOW + timedelta(minutes=10)
    _seed_concentrated(db, t3.isoformat(), mv="9000")
    eng.evaluate_and_dispatch(t3)
    assert len(spy.sent) == 2                              # 仍是 2 条
    assert db.get_alert_state("R1:KMEM")["state"] == "ACTIVE"   # 状态不丢

    # 之后持续存在也不再推（R1 每日限一次）
    t4 = NOW + timedelta(minutes=45)
    _seed_concentrated(db, t4.isoformat(), mv="9000")
    eng.evaluate_and_dispatch(t4)
    assert len(spy.sent) == 2


def test_alert_state_tracks_lifecycle(make_config):
    cfg, db, eng, spy = _engine(make_config)
    _seed_concentrated(db, NOW.isoformat())
    eng.evaluate_and_dispatch(NOW)
    st = db.get_alert_state("R1:KMEM")
    assert st["state"] == "ACTIVE" and st["notify_count"] == 1
    assert st["first_triggered_at"] == NOW.isoformat()
    assert Decimal(st["notified_value"]) == Decimal("0.9")


# ---------------- R1/R2 每日限一次 ----------------
# 集中度这类持续风险整天都在小幅波动，「显著恶化」会让它变相高频轰炸。
# 78% → 84% 仍然是同一件事：仓位太重。

def test_r1_capped_to_once_per_day_even_when_worsening():
    st = _st(notified_value="0.78", last_summary_date=TODAY)
    ok, why = decide_notify(st, severity="WARN", value=Decimal("0.90"),
                            now_et_date=TODAY, rule_id="R1")
    assert ok is False and why == SUPPRESSED      # 恶化 12pp 也不再推


def test_r2_capped_to_once_per_day():
    st = _st(notified_value="0.88", last_summary_date=TODAY)
    ok, _ = decide_notify(st, severity="WARN", value=Decimal("0.95"),
                          now_et_date=TODAY, rule_id="R2")
    assert ok is False


def test_r1_notifies_once_next_day():
    st = _st(notified_value="0.78", last_summary_date="2026-07-15")
    ok, why = decide_notify(st, severity="WARN", value=Decimal("0.78"),
                            now_et_date=TODAY, rule_id="R1")
    assert ok is True and why == DAILY_SUMMARY


def test_r1_escalation_still_immediate():
    """升级是状态变化，不受每日上限约束。"""
    st = _st(notified_severity="WARN", last_summary_date=TODAY)
    ok, why = decide_notify(st, severity="CRIT", value=Decimal("0.78"),
                            now_et_date=TODAY, rule_id="R1")
    assert ok is True and why == ESCALATION


def test_r1_retrigger_still_immediate():
    st = _st(state="RESOLVED", last_summary_date=TODAY)
    ok, why = decide_notify(st, severity="WARN", value=Decimal("0.78"),
                            now_et_date=TODAY, rule_id="R1")
    assert ok is True and why == RETRIGGER


def test_non_capped_rule_still_notifies_on_worsening():
    """R6/R7 这类事件型规则不受每日上限约束。"""
    st = _st(notified_value="0.02", last_summary_date=TODAY)
    ok, why = decide_notify(st, severity="WARN", value=Decimal("0.09"),
                            now_et_date=TODAY, rule_id="R6")
    assert ok is True and why == WORSENED


def test_r1_end_to_end_one_push_per_day(make_config):
    """端到端：R1 触发后集中度一路恶化，当天仍只推一条。"""
    cfg, db, eng, spy = _engine(make_config)
    base = NOW
    for i, mv in enumerate(("9000", "9300", "9600", "9900")):   # 90% -> 99%
        ts = base + timedelta(minutes=i * 5)
        _seed_concentrated(db, ts.isoformat(), mv=mv)
        eng.evaluate_and_dispatch(ts)
    assert len(spy.sent) == 1                    # 恶化 9pp 也不再重复

    nxt = base + timedelta(days=1)
    _seed_concentrated(db, nxt.isoformat(), mv="9900")
    eng.evaluate_and_dispatch(nxt)
    assert len(spy.sent) == 2                    # 次日一条摘要


# ---------------- 再触发冷却（P0-1） ----------------
# 线上实测：DRAM 贴着止损线震荡，「触发→解除→再触发」反复推送，一支 10 条，
# 24 小时全站 52 条。滞回带管规则层不来回改判，冷却管通知层不重复。

def test_retrigger_within_cooldown_is_suppressed():
    st = _st(state="RESOLVED", resolved_at="2026-07-16T15:41:00+00:00")
    ok, why = decide_notify(st, severity="WARN", value=Decimal("0.5"),
                            now_et_date=TODAY, rule_id="R7",
                            now=datetime(2026, 7, 16, 15, 44, tzinfo=timezone.utc))
    assert ok is False and why == SUPPRESSED      # 解除后 3 分钟就再触发 -> 不推


def test_retrigger_after_cooldown_notifies():
    st = _st(state="RESOLVED", resolved_at="2026-07-16T15:00:00+00:00")
    ok, why = decide_notify(st, severity="WARN", value=Decimal("0.5"),
                            now_et_date=TODAY, rule_id="R7",
                            now=datetime(2026, 7, 16, 15, 45, tzinfo=timezone.utc))
    assert ok is True and why == RETRIGGER        # 45 分钟后 -> 推


def test_retrigger_cooldown_is_configurable():
    from app.alerting import AlertPolicy
    st = _st(state="RESOLVED", resolved_at="2026-07-16T15:41:00+00:00")
    loose = AlertPolicy(retrigger_cooldown=timedelta(hours=4))
    ok, _ = decide_notify(st, severity="WARN", value=Decimal("0.5"), now_et_date=TODAY,
                          rule_id="R7", now=datetime(2026, 7, 16, 17, 0, tzinfo=timezone.utc),
                          policy=loose)
    assert ok is False


def test_retrigger_without_now_falls_back_to_notify():
    """未提供 now 时不做冷却判定（保持旧行为，不静默吞掉告警）。"""
    st = _st(state="RESOLVED", resolved_at="2026-07-16T15:41:00+00:00")
    ok, why = decide_notify(st, severity="WARN", value=Decimal("0.5"),
                            now_et_date=TODAY, rule_id="R7")
    assert ok is True and why == RETRIGGER
