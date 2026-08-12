import dataclasses
from datetime import date, timedelta
from decimal import Decimal

import pytest

from app.db import Database
from app.opportunity_service import OpportunityError, OpportunityService, rules_hash


def _db_service(make_config, tmp_path, **overrides):
    values = {"db_path": tmp_path / "opportunity.db", "opportunity_enabled": True,
              "portfolio_builder_enabled": True, "opportunity_shadow_min_days": 0}
    values.update(overrides)
    cfg = make_config(**values)
    db = Database(cfg.db_path)
    db.init_schema()
    return cfg, db, OpportunityService(cfg, db)


def _snapshot(db, *, equity="100000", cash="100000", positions=()):
    db.write_account_snapshot(
        snapshot_id="snap-1", ts="2026-08-10T21:00:00+00:00",
        balances={"account": "ACC", "equity": equity, "cash": cash,
                  "total_cash": cash, "margin_balance": "0", "buying_power": cash},
        positions=list(positions),
    )


def _position(symbol, value="10000", price="100", qty="100"):
    return {"symbol": symbol, "qty": qty, "avg_cost": price, "price": price,
            "market_value": value, "upl": "0"}


def _stop(db, symbol, stop="95", price="100", sufficient=True):
    db.upsert_stop_level("2026-08-10", {
        "symbol": symbol, "side": "LONG", "close": price, "atr": "2",
        "swing": stop, "stop_price": stop, "risk_amount": "500", "risk_pct": "0.005",
        "data_sufficient": sufficient, "bars_used": 260,
    })


def _scan_with_candidates(cfg, db, svc, candidates, market_state="supportive"):
    scan_id = "scan-1"
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO opportunity_scan_runs "
            "(id,status,trigger_source,started_at,finished_at,universe_count,eligible_count,"
            "ready_count,watch_count,blocked_count,market_state,rules_version,rules_hash,data_as_of) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (scan_id, "SUCCESS", "test", "2026-08-10T22:00:00+00:00",
             "2026-08-10T22:01:00+00:00", len(candidates), len(candidates), len(candidates),
             0, 0, market_state, "test", rules_hash(cfg), "2026-08-10"),)
    for item in candidates:
        c = {
            "symbol": item["symbol"], "setup_type": item.get("setup_type", "BREAKOUT"),
            "status": "READY", "score": item.get("score", 90), "grade": "A",
            "confidence": "FULL", "current_price": item.get("price", "100"),
            "ema20": "95", "ema50": "90", "atr14": "2", "rvol20": "1.5",
            "entry_low": item.get("entry_low", item.get("price", "100")),
            "entry_high": item.get("entry_high", item.get("price", "100")),
            "stop_price": item.get("stop", "95"),
            "risk_per_share": item.get("risk", "5"),
            "median_dollar_volume_20d": "50000000", "relative_strength_20d": "0.1",
            "relative_strength_60d": "0.2", "score_components": {}, "reasons": ["test"],
            "blocked_reasons": [], "bar_as_of": "2026-08-10",
            "adjustment_mode": "split-adjusted", "quality_status": "FULL", "input_refs": {},
        }
        svc._save_candidate(scan_id, c, rules_hash(cfg))
    return scan_id


def test_portfolio_sizes_by_075_percent_trade_risk(make_config, tmp_path):
    cfg, db, svc = _db_service(make_config, tmp_path)
    _snapshot(db)
    svc.save_watch("AAA", sector="Technology")
    _scan_with_candidates(cfg, db, svc, [{"symbol": "AAA", "risk": "5"}])
    result = svc.build_portfolio()
    item = next(x for x in result["items"] if x["item_type"] == "ADD")
    assert item["proposed_quantity"] == "150"
    assert Decimal(item["risk_amount"]) == Decimal("750")
    assert Decimal(item["risk_pct"]) == Decimal("0.0075")
    assert Decimal(item["proposed_weight"]) == Decimal("0.15")


def test_target_position_sector_and_cash_caps(make_config, tmp_path):
    cfg, db, svc = _db_service(make_config, tmp_path)
    _snapshot(db, cash="6000", positions=[
        _position("HELD1", value="17000", qty="170"),
        _position("HELD2", value="17000", qty="170"),
    ])
    _stop(db, "HELD1", stop="99")
    _stop(db, "HELD2", stop="99")
    svc.save_watch("HELD1", sector="Technology")
    svc.save_watch("HELD2", sector="Technology")
    svc.save_watch("AAA", sector="Technology")
    _scan_with_candidates(cfg, db, svc, [{"symbol": "AAA", "risk": "1"}])
    result = svc.build_portfolio()
    item = next(x for x in result["items"] if x["item_type"] == "ADD")
    # 行业只剩 1%，现金在保留 5% 后也只剩 1,000 美元，两者都限制为 10 股。
    assert item["proposed_quantity"] == "10"
    assert Decimal(item["proposed_weight"]) == Decimal("0.01")


def test_existing_position_without_formal_stop_blocks_builder(make_config, tmp_path):
    cfg, db, svc = _db_service(make_config, tmp_path)
    _snapshot(db, positions=[_position("HELD")])
    svc.save_watch("AAA", sector="Technology")
    _scan_with_candidates(cfg, db, svc, [{"symbol": "AAA"}])
    result = svc.build_portfolio()
    assert result["proposal"]["status"] == "BLOCKED"
    assert any("缺少可靠止损" in x for x in result["proposal"]["blocked_reasons"])
    assert not [x for x in result["items"] if x["item_type"] == "ADD"]


def test_over_30_percent_position_and_stressed_market_block_new_risk(make_config, tmp_path):
    cfg, db, svc = _db_service(make_config, tmp_path)
    _snapshot(db, positions=[_position("HELD", value="31000", qty="310")])
    _stop(db, "HELD")
    svc.save_watch("AAA", sector="Other")
    _scan_with_candidates(cfg, db, svc, [{"symbol": "AAA"}], market_state="stressed")
    result = svc.build_portfolio()
    reasons = result["proposal"]["blocked_reasons"]
    assert result["proposal"]["risk_budget_pct"] == "0"
    assert any("30%硬上限" in x for x in reasons)
    assert any("禁止新增仓位" in x for x in reasons)
    assert result["proposal"]["proposed_risk_amount"] == "0"


def test_max_eight_positions_and_no_forced_fill(make_config, tmp_path):
    cfg, db, svc = _db_service(make_config, tmp_path)
    positions = [_position(f"P{i}", value="1000", qty="10") for i in range(8)]
    _snapshot(db, positions=positions)
    for i in range(8):
        _stop(db, f"P{i}", stop="99")
    svc.save_watch("AAA", sector="Other")
    _scan_with_candidates(cfg, db, svc, [{"symbol": "AAA"}])
    result = svc.build_portfolio()
    blocked = next(x for x in result["items"] if x["symbol"] == "AAA")
    assert blocked["item_type"] == "BLOCKED"
    assert "持仓数量已达8只" in blocked["blocked_reason"]


def test_adoption_is_idempotent_and_creates_pending_draft_only(make_config, tmp_path):
    cfg, db, svc = _db_service(make_config, tmp_path)
    _snapshot(db)
    svc.save_watch("AAA", sector="Technology")
    _scan_with_candidates(cfg, db, svc, [{"symbol": "AAA"}])
    proposal = svc.build_portfolio()
    item = next(x for x in proposal["items"] if x["item_type"] == "ADD")
    first = svc.adopt_item(item["id"], actor="test")
    second = svc.adopt_item(item["id"], actor="test")
    assert first["cycle_id"] == second["cycle_id"]
    assert second["idempotent"] is True
    with db.connect() as conn:
        cycle = conn.execute("SELECT * FROM position_cycles WHERE id=?", (first["cycle_id"],)).fetchone()
        drafts = conn.execute(
            "SELECT COUNT(*) FROM position_plan_versions WHERE position_cycle_id=? "
            "AND lifecycle_status='DRAFT'", (first["cycle_id"],)).fetchone()[0]
        orders = conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0]
    assert cycle["status"] == "PENDING"
    assert cycle["current_quantity"] == "0"
    assert drafts == 1
    assert orders == 0


def test_shadow_gate_counts_only_distinct_successful_nonempty_dates(make_config, tmp_path):
    cfg, db, svc = _db_service(
        make_config, tmp_path, opportunity_shadow_min_days=5)
    _snapshot(db)
    svc.save_watch("AAA", sector="Technology")
    _scan_with_candidates(cfg, db, svc, [{"symbol": "AAA"}])
    with db.connect() as conn:
        rows = [
            ("dup", "SUCCESS", 1, "2026-08-10"),
            ("empty", "SUCCESS", 0, "2026-08-09"),
            ("failed", "FAILED", 1, "2026-08-08"),
        ]
        for scan_id, status, universe, data_as_of in rows:
            conn.execute(
                "INSERT INTO opportunity_scan_runs "
                "(id,status,trigger_source,started_at,finished_at,universe_count,eligible_count,"
                "ready_count,watch_count,blocked_count,market_state,rules_version,rules_hash,data_as_of) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (scan_id, status, "test", f"{data_as_of}T20:00:00+00:00",
                 f"{data_as_of}T20:01:00+00:00", universe, 0, 0, 0, 0,
                 "neutral", "test", rules_hash(cfg), data_as_of))
    state = svc.shadow_readiness()
    assert state["completed_days"] == 1
    assert state["remaining_days"] == 4
    assert state["ready"] is False
    with pytest.raises(OpportunityError, match="1/5"):
        svc.build_portfolio()

    with db.connect() as conn:
        for idx, data_as_of in enumerate(("2026-08-04", "2026-08-05", "2026-08-06", "2026-08-07")):
            conn.execute(
                "INSERT INTO opportunity_scan_runs "
                "(id,status,trigger_source,started_at,finished_at,universe_count,eligible_count,"
                "ready_count,watch_count,blocked_count,market_state,rules_version,rules_hash,data_as_of) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (f"effective-{idx}", "SUCCESS", "scheduler",
                 f"{data_as_of}T20:00:00+00:00", f"{data_as_of}T20:01:00+00:00",
                 1, 1, 0, 1, 0, "neutral", "test", rules_hash(cfg), data_as_of))
    assert svc.shadow_readiness()["ready"] is True
    assert svc.build_portfolio()["proposal"]["status"] == "READY"


def test_adoption_is_blocked_when_builder_flag_is_closed(make_config, tmp_path):
    cfg, db, svc = _db_service(make_config, tmp_path)
    _snapshot(db)
    svc.save_watch("AAA", sector="Technology")
    _scan_with_candidates(cfg, db, svc, [{"symbol": "AAA"}])
    proposal = svc.build_portfolio()
    item = next(x for x in proposal["items"] if x["item_type"] == "ADD")
    closed = OpportunityService(
        dataclasses.replace(cfg, portfolio_builder_enabled=False), db)
    with pytest.raises(OpportunityError, match="不能采纳"):
        closed.adopt_item(item["id"], actor="test")
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM position_cycles WHERE status='PENDING'"
        ).fetchone()[0] == 0


def test_feature_flags_control_scan_and_builder(make_config, tmp_path):
    cfg, db, svc = _db_service(make_config, tmp_path, opportunity_enabled=False,
                               portfolio_builder_enabled=False)
    with pytest.raises(OpportunityError, match="尚未启用"):
        svc.run_scan()
    with pytest.raises(OpportunityError, match="尚未启用"):
        svc.build_portfolio()
