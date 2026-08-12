"""建议合成层 Stage 2 接线：适配器从真实库组装 SymbolInput，合成并落库。"""
from decimal import Decimal

import pytest

from app import recommendation_service as rs
from app.db import Database
from app.position_plans import PositionPlanService


def _cfg(make_config, tmp_path):
    return make_config(db_path=tmp_path / "wire.db")


def _seed_position(db, *, symbol="RKLB", qty="100", price="78",
                   avg_cost="70", mv="7800", equity="10000", cash="2000",
                   ts="2026-08-05T10:00:00+00:00"):
    db.write_account_snapshot(
        snapshot_id="snap-1", ts=ts,
        balances={"account": "ACC", "equity": equity, "cash": cash,
                  "margin_balance": "0", "buying_power": cash},
        positions=[{"symbol": symbol, "qty": qty, "price": price,
                    "avg_cost": avg_cost, "market_value": mv, "upl": "800"}])


def _seed_daily_bars(db, symbol="RKLB", n=60, high="82.54"):
    """seed 一段日线，让技术路径真正参与（不 seed 时 bool(bars) 短路，遮蔽 bug）。"""
    bars = [{"date": f"2026-{'06' if d <= 30 else '07'}-{((d - 1) % 30) + 1:02d}",
             "o": 78, "h": float(high), "l": 76, "c": 79, "v": 1_000_000}
            for d in range(1, n + 1)]
    db.upsert_daily_bars(symbol, bars)


def test_run_synthesis_with_daily_bars_does_not_crash_on_technical_path(make_config, tmp_path):
    """回归：compute_symbol() 不返回 is_stale；有日线时技术路径必须能跑通、不 KeyError。

    线上部署首次合成时 4 个持仓全被 skip（synthesized=4/written=0），根因即此——
    单测此前没 seed 日线，bool(bars) 短路遮蔽了 card_tech['is_stale'] 的 KeyError。
    """
    cfg = _cfg(make_config, tmp_path)
    db = Database(cfg.db_path)
    db.init_schema()
    _seed_position(db)
    _seed_daily_bars(db)                      # 关键：让技术路径真正参与
    out = rs.run_synthesis(cfg, db)
    assert out["skipped"] == 0                # 不得再有标的被异常跳过
    assert out["written"] >= 1
    assert len(db.current_recommendations("ACC")) == 1


def test_run_synthesis_persists_recommendation_for_position(make_config, tmp_path):
    cfg = _cfg(make_config, tmp_path)
    db = Database(cfg.db_path)
    db.init_schema()
    _seed_position(db)
    # 建一个正式计划：max_weight 0.02，让 78% 的持仓明确超限
    service = PositionPlanService(db, "ACC")
    service.reconcile(snapshot_id="snap-1", observed_at="2026-08-05T10:00:00+00:00",
                      positions=[{"symbol": "RKLB", "qty": "100", "price": "78",
                                  "avg_cost": "70", "market_value": "7800", "upl": "800"}])
    cycle_id = service.list_cycles()[0]["id"]
    service.publish_version(cycle_id, {
        "current_conclusion": "持有", "thesis": "理由",
        "invalidation_conditions": "失效", "max_weight": "0.02",
        "hold_only_review": True, "review_at": "2026-08-12T10:00:00+00:00"})

    out = rs.run_synthesis(cfg, db)
    assert out["written"] >= 1
    current = db.current_recommendations("ACC")
    assert len(current) == 1
    rec = current[0]
    assert rec["symbol"] == "RKLB"
    # 78% ≫ 2% 上限 → 超限；无可追溯减仓量 → DO_NOT_ADD（不凭空造减仓数字）
    assert rec["action"] == "DO_NOT_ADD"
    assert rec["decided_by"] == "position_limit"
    # provenance：snapshot / plan 引用入 input_refs
    import json
    refs = json.loads(rec["input_refs"])
    assert "plan_version_id" in refs


def test_run_synthesis_is_idempotent_across_repeated_syncs(make_config, tmp_path):
    """重复运行（模拟 60s 同步）不得每次新增行 —— 指纹不变只更新 last_seen_at。"""
    cfg = _cfg(make_config, tmp_path)
    db = Database(cfg.db_path)
    db.init_schema()
    _seed_position(db)
    for _ in range(5):
        rs.run_synthesis(cfg, db)
    with db.connect() as conn:
        n = conn.execute("SELECT COUNT(*) FROM recommendation_snapshots").fetchone()[0]
    assert n == 1


def test_run_synthesis_no_account_returns_zero(make_config, tmp_path):
    cfg = _cfg(make_config, tmp_path)
    db = Database(cfg.db_path)
    db.init_schema()
    out = rs.run_synthesis(cfg, db)
    assert out["written"] == 0


def test_run_synthesis_swallows_errors(make_config, tmp_path):
    """整体旁路：库损坏也只返回计数，绝不外抛（不能拖垮同步链）。"""
    cfg = _cfg(make_config, tmp_path)
    db = Database(cfg.db_path)
    db.init_schema()
    _seed_position(db)
    # 删掉建议表制造异常，run_synthesis 内部 persist 会失败但被吞
    with db.connect() as conn:
        conn.execute("DROP TABLE recommendation_snapshots")
    out = rs.run_synthesis(cfg, db)          # 不抛
    assert out["written"] == 0
