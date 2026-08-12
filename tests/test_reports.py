"""定时报告中的操作性内容必须带可追溯 ID。"""
from datetime import datetime, timezone
from decimal import Decimal

from app.db import Database
from app.recommendation_service import persist
from app.reports import build_morning_report


def test_morning_report_uses_grounded_recommendation_instead_of_raw_trim(make_config):
    cfg = make_config(ib_account="ACC")
    db = Database(cfg.db_path)
    db.init_schema()
    now = datetime(2026, 8, 10, 12, 0, tzinfo=timezone.utc)
    db.write_account_snapshot(
        snapshot_id="snap-report", ts=now.isoformat(),
        balances={"account": "ACC", "equity": Decimal("10000"),
                  "cash": Decimal("5000"), "margin_balance": Decimal("0"),
                  "buying_power": Decimal("5000")},
        positions=[{"symbol": "RKLB", "qty": Decimal("10"),
                    "avg_cost": Decimal("10"), "market_value": Decimal("100"),
                    "upl": Decimal("0")}],
    )
    db.upsert_stop_level("2026-08-07", {
        "symbol": "RKLB", "side": "LONG", "close": Decimal("10"),
        "atr": Decimal("1"), "swing": Decimal("9"), "stop_price": Decimal("8"),
        "risk_amount": Decimal("20"), "risk_pct": Decimal("0.02"),
        "exceeds_2pct": True, "suggested_trim": 7, "reached_1r": False,
        "trailing_stop": Decimal("8.5"), "take_profit": "未达 1R",
    })
    rec_id = persist(db, {
        "account": "ACC", "symbol": "RKLB", "action": "REDUCE",
        "decided_by": "position_limit", "reason": "仓位已超计划上限",
        "quantity": "5", "covered_action_ids": [], "input_refs": {"snapshot_id": "snap-report"},
        "decision_fingerprint": "report-grounded", "rule_set_version": "test",
        "rule_set_hash": "test",
    })

    _title, body = build_morning_report(cfg, db, now)

    assert "建议减仓7股" not in body
    assert f"合成建议：减仓 5 股：仓位已超计划上限（recommendation_id:{rec_id}）" in body
    assert "规则引用：R7" in body
