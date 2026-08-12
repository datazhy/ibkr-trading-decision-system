"""建议合成层单测：§1.3 优先级、按路径门禁、指纹去重不写爆表。"""
from datetime import datetime, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from app import recommendation_service as rs
from app.db import Database


def _inp(**kw) -> rs.SymbolInput:
    base = dict(account="ACC", symbol="RKLB", cycle_id="cyc1",
                input_refs={"snapshot_id": "snap-1"})
    base.update(kw)
    return rs.SymbolInput(**base)


# ---------------- §1.3 优先级链 ----------------

def test_hard_stop_overrides_everything():
    """止损触发覆盖计划加仓与技术强多头。"""
    inp = _inp(stop_breached=True, exit_on_stop=True,
               account_reliable=True, technical_reliable=True,
               technical_action="ADD_WATCH",
               plan_actions=[{"id": "act1", "action_type": "ADD", "quantity": "5"}])
    rec = rs.synthesize_symbol(inp)
    assert rec["action"] == rs.EXIT
    assert rec["decided_by"] == rs.BY_STOP


def test_over_limit_reduces_when_traceable_quantity_present():
    inp = _inp(account_reliable=True, over_limit=True,
               current_weight=Decimal("0.11"), max_weight=Decimal("0.02"),
               reduce_quantity=Decimal("50"),
               technical_reliable=True, technical_action="ADD_WATCH")
    rec = rs.synthesize_symbol(inp)
    assert rec["action"] == rs.REDUCE
    assert rec["decided_by"] == rs.BY_LIMIT
    assert rec["quantity"] == "50"


def test_over_limit_do_not_add_when_no_reduce_quantity():
    inp = _inp(account_reliable=True, over_limit=True,
               current_weight=Decimal("0.11"), max_weight=Decimal("0.02"))
    rec = rs.synthesize_symbol(inp)
    assert rec["action"] == rs.DO_NOT_ADD
    assert rec["decided_by"] == rs.BY_LIMIT


def test_cooldown_covers_plan_add_and_records_covered_action_ids():
    inp = _inp(account_reliable=True, cooldown_active=True,
               plan_present=True,
               plan_actions=[{"id": "act9", "action_type": "ADD", "quantity": "5"}])
    rec = rs.synthesize_symbol(inp)
    assert rec["action"] == rs.DO_NOT_ADD
    assert rec["covered_action_ids"] == ["act9"]


def test_published_add_action_becomes_add_watch_with_action_ref():
    inp = _inp(account_reliable=True, plan_present=True, price_reliable=True,
               confirmation="INTRADAY_PENDING",
               plan_actions=[{"id": "act2", "action_type": "ADD",
                              "trigger_type": "PRICE", "quantity": "5", "met": False}])
    rec = rs.synthesize_symbol(inp)
    assert rec["action"] == rs.ADD_WATCH
    assert rec["decided_by"] == rs.BY_PLAN
    assert rec["input_refs"]["action_id"] == "act2"


def test_extended_hours_add_action_stays_pending():
    """扩展时段的价格触发动作不得形成收盘确认，只挂 pending 理由。"""
    inp = _inp(account_reliable=True, plan_present=True, price_reliable=True,
               confirmation="EXTENDED_HOURS_PENDING",
               plan_actions=[{"id": "act3", "action_type": "ADD",
                              "trigger_type": "PRICE", "quantity": "5", "met": True}])
    rec = rs.synthesize_symbol(inp)
    assert rec["action"] == rs.ADD_WATCH
    assert "扩展时段" in rec["reason"]


def test_no_plan_gives_review_never_auto_add():
    """无正式计划：只复盘，绝不从剩余空间推导加仓（§1.3.8 / §1.7）。"""
    inp = _inp(account_reliable=True, plan_present=False,
               current_weight=Decimal("0.01"), max_weight=Decimal("0.05"))
    rec = rs.synthesize_symbol(inp)
    assert rec["action"] == rs.REVIEW
    assert rec["decided_by"] == rs.BY_NONE


# ---------------- §1.3.1 按路径门禁 ----------------

def test_stale_daily_blocks_technical_but_account_limit_still_fires():
    """日线过期只让技术路径不可用；fresh 账户快照证明超限仍可独立 DO_NOT_ADD。"""
    inp = _inp(account_reliable=True, over_limit=True,
               current_weight=Decimal("0.11"), max_weight=Decimal("0.02"),
               technical_reliable=False)      # 技术路径被门禁挡住
    rec = rs.synthesize_symbol(inp)
    assert rec["action"] == rs.DO_NOT_ADD
    assert rec["decided_by"] == rs.BY_LIMIT


def test_account_unreliable_and_no_other_path_blocks():
    inp = _inp(account_reliable=False, technical_reliable=False, plan_present=True)
    rec = rs.synthesize_symbol(inp)
    assert rec["action"] == rs.BLOCKED
    assert rec["decided_by"] == rs.BY_DATA
    assert rec["blocked_reason"]


def test_daily_rth_coverage_gate_blocks_stale_bar_but_records_expected_date(monkeypatch):
    """最近完整 RTH 日线缺失时，只关闭技术路径，并保存预期日期 provenance。"""
    captured = []

    class FakeDB:
        def get_daily_bars(self, symbol, limit=600):
            return [{"date": "2026-08-06", "o": Decimal("10"),
                     "h": Decimal("11"), "l": Decimal("9"),
                     "c": Decimal("10"), "v": 100}]

        def active_plan_actions(self, cycle_id):
            return []

    class FakeTechnical:
        EXTENDED_HOURS_PENDING = "EXTENDED_HOURS_PENDING"

        @staticmethod
        def confirmation_basis(session, **kwargs):
            return "CLOSE_CONFIRMED"

        @staticmethod
        def compute_symbol(*args, **kwargs):
            return {"daily_trend_state": "UP"}

        @staticmethod
        def decide(*args, **kwargs):
            return {"decided_by": "technical", "final_action": rs.HOLD,
                    "reason": "test"}

        @staticmethod
        def evaluate_rules(*args, **kwargs):
            return []

    def capture(inp):
        captured.append(inp)
        return {"action": rs.REVIEW}

    monkeypatch.setattr(rs, "synthesize_symbol", capture)
    cfg = type("Cfg", (), {"market_tz": ZoneInfo("America/New_York"),
                            "ib_account": "ACC"})()
    row = {"symbol": "RKLB", "price": "10", "price_source": "IBKR",
           "source": "IBKR", "price_quality": "REALTIME",
           "quote_as_of": "2026-08-10T12:00:00+00:00", "account": "ACC"}
    card = {"account_weight": "0.01", "max_weight": "0.05",
            "has_published_plan": False}

    to_decimal = lambda value: None if value is None else Decimal(str(value))
    rs._synthesize_position(
        cfg, FakeDB(), FakeTechnical(), to_decimal, row, Decimal("1"), "PRE",
        {}, {}, card, None, None,
        datetime(2026, 8, 10, 12, 0, tzinfo=timezone.utc))

    assert captured[0].technical_reliable is False
    assert captured[0].input_refs["bar_max_date"] == "2026-08-06"
    assert captured[0].input_refs["expected_rth_bar_date"] == "2026-08-07"


# ---------------- §1.5.2 指纹 ----------------

def test_fingerprint_ignores_volatile_ids():
    """snapshot_id / quote_as_of 变化不得改变指纹（否则 60s 同步写爆表）。"""
    a = rs.synthesize_symbol(_inp(account_reliable=True, over_limit=True,
                                  current_weight=Decimal("0.11"),
                                  max_weight=Decimal("0.02"),
                                  input_refs={"snapshot_id": "snap-1"},
                                  quote={"as_of": "2026-08-08T10:00:00Z"}))
    b = rs.synthesize_symbol(_inp(account_reliable=True, over_limit=True,
                                  current_weight=Decimal("0.11"),
                                  max_weight=Decimal("0.02"),
                                  input_refs={"snapshot_id": "snap-9999"},
                                  quote={"as_of": "2026-08-08T10:59:00Z"}))
    assert a["decision_fingerprint"] == b["decision_fingerprint"]


def test_fingerprint_changes_on_action_change():
    a = rs.synthesize_symbol(_inp(account_reliable=True, over_limit=True,
                                  current_weight=Decimal("0.11"),
                                  max_weight=Decimal("0.02")))
    b = rs.synthesize_symbol(_inp(account_reliable=True, over_limit=False,
                                  plan_present=False))
    assert a["decision_fingerprint"] != b["decision_fingerprint"]


# ---------------- §1.5.3 落库去重 / 状态跃迁 ----------------

@pytest.fixture
def db(tmp_path):
    d = Database(tmp_path / "rec.db")
    d.init_schema()
    return d


def test_persist_dedups_on_unchanged_fingerprint(db):
    """指纹不变时不新增行，只更新 last_seen_at —— 挡住 60s 同步写爆表。"""
    for i in range(10):
        rec = rs.synthesize_symbol(_inp(
            account_reliable=True, over_limit=True,
            current_weight=Decimal("0.11"), max_weight=Decimal("0.02"),
            input_refs={"snapshot_id": f"snap-{i}"}))      # 每次 snapshot 都变
        rs.persist(db, rec, now=f"2026-08-08T10:{i:02d}:00+00:00")
    with db.connect() as conn:
        rows = conn.execute("SELECT * FROM recommendation_snapshots").fetchall()
    assert len(rows) == 1                                  # 只落一行
    assert rows[0]["last_seen_at"] == "2026-08-08T10:09:00+00:00"
    assert rows[0]["status"] == "ACTIVE"


def test_persist_supersedes_on_decision_change(db):
    rec1 = rs.synthesize_symbol(_inp(account_reliable=True, over_limit=True,
                                     current_weight=Decimal("0.11"),
                                     max_weight=Decimal("0.02")))
    rs.persist(db, rec1, now="2026-08-08T10:00:00+00:00")
    rec2 = rs.synthesize_symbol(_inp(account_reliable=True, over_limit=True,
                                     current_weight=Decimal("0.11"),
                                     max_weight=Decimal("0.02"),
                                     reduce_quantity=Decimal("50")))  # 决策变：加了减仓量
    rs.persist(db, rec2, now="2026-08-08T11:00:00+00:00")
    history = db.recommendation_history("ACC", "RKLB")
    assert len(history) == 2
    assert history[0]["status"] == "ACTIVE" and history[0]["action"] == "REDUCE"
    assert history[1]["status"] == "SUPERSEDED"
    assert history[0]["supersedes_id"] == history[1]["id"]
    current = db.current_recommendations("ACC")
    assert len(current) == 1 and current[0]["action"] == "REDUCE"


def test_blocked_recommendation_stored_with_blocked_status(db):
    rec = rs.synthesize_symbol(_inp(account_reliable=False, technical_reliable=False,
                                    plan_present=True))
    rid = rs.persist(db, rec, now="2026-08-08T10:00:00+00:00")
    row = db.recommendation_history("ACC", "RKLB")[0]
    assert row["id"] == rid and row["status"] == "BLOCKED" and row["action"] == "BLOCKED"
