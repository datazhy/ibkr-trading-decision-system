"""2A：R6 由 executions 驱动的端到端行为（engine 层）。

覆盖验收：重复同步不重复触发、迟到 commission 不误触发、correction 用最终数据、
重启补拉不重复、独立水位不被 orders 推进、冷静期延长。
"""
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from app.db import Database
from app.engine import RuleEngine
from app.ibkr_client import parse_ib_execution
from app.notify import Notifier
from test_executions import mk_fill

NOW = datetime(2026, 7, 16, 18, 0, tzinfo=timezone.utc)


class RecNotifier(Notifier):
    def __init__(self, db):
        super().__init__([], db)
        self.sent = []

    def send(self, title, body):
        self.sent.append(title)
        return True


@pytest.fixture
def env(make_config):
    cfg = make_config()
    db = Database(cfg.db_path)
    db.init_schema()
    db.insert_balances_snapshot(NOW.isoformat(), {
        "equity": Decimal("100000"), "cash": Decimal("50000"),
        "margin_balance": Decimal("50000"), "buying_power": Decimal("80000")})
    n = RecNotifier(db)
    return cfg, db, n, RuleEngine(cfg, db, n)


def _exec(db, **kw):
    db.upsert_executions([parse_ib_execution(mk_fill(**kw))])


def test_r6_triggers_from_execution(env):
    cfg, db, n, engine = env
    _exec(db, realized=-5000.0)                    # 5% > 2%
    hits = engine.evaluate_and_dispatch(now=NOW)
    assert any(h.rule_id == "R6" for h in hits)
    assert db.active_cooldown(NOW.isoformat()) is not None


def test_repeated_sync_does_not_retrigger(env):
    """同一 execution 重复同步 → 不重复入库、不重复触发、冷静期只建一次。"""
    cfg, db, n, engine = env
    _exec(db, realized=-5000.0)
    engine.evaluate_and_dispatch(now=NOW)
    _exec(db, realized=-5000.0)                    # 重复同步同一 execId
    hits2 = engine.evaluate_and_dispatch(now=NOW + timedelta(seconds=45))
    assert not [h for h in hits2 if h.rule_id == "R6" and h.triggers_cooldown_hours]
    with db.connect() as c:
        assert c.execute("select count(*) c from cooldown_state").fetchone()["c"] == 1


def test_late_commission_no_premature_trigger(env):
    """commission 未到时不得按错误金额触发；到齐后才触发。"""
    cfg, db, n, engine = env
    _exec(db, has_comm=False)                      # 无 commission/realized
    hits1 = engine.evaluate_and_dispatch(now=NOW)
    assert not [h for h in hits1 if h.rule_id == "R6"]
    assert db.active_cooldown(NOW.isoformat()) is None

    _exec(db, has_comm=True, realized=-5000.0)     # commission 后到
    hits2 = engine.evaluate_and_dispatch(now=NOW + timedelta(seconds=45))
    assert any(h.rule_id == "R6" for h in hits2)


def test_correction_uses_final_data(env):
    """修正执行：用修正后的最终 realized，不把旧执行一起累计。"""
    cfg, db, n, engine = env
    _exec(db, exec_id="c.01", realized=-5000.0)
    _exec(db, exec_id="c.02", realized=-1000.0)    # 修正为仅亏 1%（<2%）
    hits = engine.evaluate_and_dispatch(now=NOW)
    assert not [h for h in hits if h.rule_id == "R6"]   # 按修正后数据不触发
    assert db.active_cooldown(NOW.isoformat()) is None


def test_restart_backfill_no_duplicate(env):
    """重启后当日全量补拉 → 已判过的 execution 不重复触发。"""
    cfg, db, n, engine = env
    _exec(db, realized=-5000.0)
    engine.evaluate_and_dispatch(now=NOW)
    for _ in range(3):                              # 模拟重启后多轮全量补拉
        _exec(db, realized=-5000.0)
        engine.evaluate_and_dispatch(now=NOW + timedelta(minutes=1))
    with db.connect() as c:
        assert c.execute("select count(*) c from cooldown_state").fetchone()["c"] == 1


def test_cursor_not_advanced_by_orders(env):
    """独立水位：R6 用 executions.r6_state，不被 orders 数据推进/挤掉。"""
    cfg, db, n, engine = env
    db.upsert_orders([{                             # 大量更晚的订单
        "order_id": "late", "ts": (NOW + timedelta(hours=1)).isoformat(), "symbol": "MU",
        "side": "BUY", "order_type": "MKT", "qty": Decimal("10"), "price": Decimal("90"),
        "status": "FILLED", "raw_json": "{}"}])
    _exec(db, realized=-5000.0)                     # 成交时间早于订单
    hits = engine.evaluate_and_dispatch(now=NOW)
    assert any(h.rule_id == "R6" for h in hits)     # 不受 orders 影响


def test_r6_marked_done_after_eval(env):
    cfg, db, n, engine = env
    _exec(db, realized=-5000.0)
    engine.evaluate_and_dispatch(now=NOW)
    assert db.pending_r6_executions() == []         # 评估后置 DONE


def test_r6_not_marked_when_data_insufficient(make_config):
    """净资产缺失 → R6 INSUFFICIENT → 不标记 DONE，留待下轮重评。"""
    cfg = make_config()
    db = Database(cfg.db_path); db.init_schema()    # 无 balances
    engine = RuleEngine(cfg, db, RecNotifier(db))
    _exec(db, realized=-5000.0)
    engine.evaluate_and_dispatch(now=NOW)
    assert len(db.pending_r6_executions()) == 1     # 仍待判


def test_cooldown_extended_by_second_big_loss(env):
    """再次大额认亏 → 新冷静期，active_cooldown 取更晚的结束时间（延长）。"""
    cfg, db, n, engine = env
    _exec(db, exec_id="a.01", realized=-5000.0)
    engine.evaluate_and_dispatch(now=NOW)
    first_end = db.active_cooldown(NOW.isoformat())["ends_at"]

    later = NOW + timedelta(hours=5)
    # 不同 permId = 另一次认亏决策（同 permId 会被按决策合并成一笔）
    _exec(db, exec_id="b.01", realized=-6000.0, perm_id=222222)
    engine.evaluate_and_dispatch(now=later)
    second_end = db.active_cooldown(later.isoformat())["ends_at"]
    assert second_end > first_end                   # 冷静期被延长
    with db.connect() as c:
        assert c.execute("select count(*) c from cooldown_state").fetchone()["c"] == 2


# ---------------- P0-3：部分成交不得绕过 R6 ----------------
# 线上真实事件（2026-07-16）：清仓 KMEM 1786 股被拆成 7 笔部分成交，
# 每笔已实现 -122 ~ -737，单笔最大占净资产 1.82% < 2% —— R6 一声没响。
# 但这 7 笔是同一个决策，合计 -2192.67 = 净资产的 5.4%。

_KMEM_PARTIALS = [
    ("-613.832905", 500), ("-245.853162", 200), ("-122.926581", 100),
    ("-121.926602", 100), ("-121.926602", 100), ("-737.559486", 600),
    ("-228.643441", 186),
]


def _kmem_fill(db, i, realized, shares, perm_id=1357400000):
    _exec(db, exec_id=f"0000k{i}.6a58.01.01", realized=float(realized),
          shares=shares, symbol="KMEM", perm_id=perm_id)


def test_partial_fills_of_one_decision_trigger_r6_together(env):
    """要害用例：7 笔单独都不到 2%，合计 5.4% -> 必须触发。"""
    cfg, db, n, engine = env
    for i, (r, qty) in enumerate(_KMEM_PARTIALS):
        _kmem_fill(db, i, r, qty)
    hits = engine.evaluate_and_dispatch(now=NOW)
    r6 = [h for h in hits if h.rule_id == "R6" and h.triggers_cooldown_hours]
    assert len(r6) == 1                              # 一次决策 = 一次认亏
    assert db.active_cooldown(NOW.isoformat()) is not None
    assert "-2,192.67" in r6[0].message              # 7 笔合计，不是单笔
    # 逐笔比阈值时，7 笔里最大的一笔也只有 0.74%（本 fixture 净资产 10 万）-> 全都不触发
    worst = max(abs(float(r)) for r, _ in _KMEM_PARTIALS)
    assert worst / 100000 < 0.02


def test_partial_fills_arriving_in_batches_still_trigger(env):
    """部分成交分批到达：先到的几笔各自判定后标 DONE，
    后到的仍须与已 DONE 的合并 —— 否则合计金额永远凑不齐。"""
    cfg, db, n, engine = env
    # 第 1 批：只有 -613（0.6% < 2%）-> 不触发，该笔被标记 DONE
    _kmem_fill(db, 0, "-613.832905", 500)
    assert not [h for h in engine.evaluate_and_dispatch(now=NOW)
                if h.rule_id == "R6" and h.triggers_cooldown_hours]
    assert db.active_cooldown(NOW.isoformat()) is None

    # 第 2 批：剩余 6 笔到达。必须与已 DONE 的第 1 笔合并（合计 -2192，2.2% > 2%）——
    # 若只合并本批新到的，永远凑不齐。
    for i, (r, qty) in enumerate(_KMEM_PARTIALS[1:], start=1):
        _kmem_fill(db, i, r, qty)
    later = NOW + timedelta(minutes=1)
    hits = engine.evaluate_and_dispatch(now=later)
    r6 = [h for h in hits if h.rule_id == "R6" and h.triggers_cooldown_hours]
    assert r6, "后到的部分成交必须与已判定过的合并"
    assert "-2,192.67" in r6[0].message              # 全部 7 笔
    assert db.active_cooldown(later.isoformat()) is not None


def test_merged_decision_does_not_double_trigger_cooldown(env):
    """后续分批到达时不重复建冷静期（dedup_key = decision_key）。"""
    cfg, db, n, engine = env
    for i, (r, qty) in enumerate(_KMEM_PARTIALS[:5]):
        _kmem_fill(db, i, r, qty)
    engine.evaluate_and_dispatch(now=NOW)
    for i, (r, qty) in enumerate(_KMEM_PARTIALS[5:], start=5):
        _kmem_fill(db, i, r, qty)
    engine.evaluate_and_dispatch(now=NOW + timedelta(minutes=1))
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) c FROM cooldown_state").fetchone()["c"] == 1


def test_separate_decisions_still_trigger_separately(env):
    """两次独立的大额认亏仍是两次 —— 合并不能把不同决策粘在一起。"""
    cfg, db, n, engine = env
    _exec(db, exec_id="0000a.1111.01.01", realized=-5000.0, perm_id=111)
    engine.evaluate_and_dispatch(now=NOW)
    _exec(db, exec_id="0000b.2222.01.01", realized=-6000.0, perm_id=222)
    engine.evaluate_and_dispatch(now=NOW + timedelta(hours=5))
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) c FROM cooldown_state").fetchone()["c"] == 2
