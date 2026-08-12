"""建议合成层 Stage 3：只读 API 与首页「今日行动」面板。"""
from starlette.testclient import TestClient

from app import recommendation_service as rs
from app.db import Database
from app.position_plans import PositionPlanService
from app.webapp import create_app


def _seed(make_config, tmp_path):
    cfg = make_config(db_path=tmp_path / "recoweb.db")
    db = Database(cfg.db_path)
    db.init_schema()
    ts = "2026-08-05T10:00:00+00:00"
    db.write_account_snapshot(
        snapshot_id="snap-1", ts=ts,
        balances={"account": "ACC", "equity": "10000", "cash": "2000",
                  "margin_balance": "0", "buying_power": "2000"},
        positions=[{"symbol": "RKLB", "qty": "100", "price": "78",
                    "avg_cost": "70", "market_value": "7800", "upl": "800"}])
    service = PositionPlanService(db, "ACC")
    service.reconcile(snapshot_id="snap-1", observed_at=ts,
                      positions=[{"symbol": "RKLB", "qty": "100", "price": "78",
                                  "avg_cost": "70", "market_value": "7800", "upl": "800"}])
    cycle_id = service.list_cycles()[0]["id"]
    service.publish_version(cycle_id, {
        "current_conclusion": "持有", "thesis": "理由",
        "invalidation_conditions": "跌破前低", "max_weight": "0.02",
        "hold_only_review": True, "review_at": "2026-08-12T10:00:00+00:00"})
    rs.run_synthesis(cfg, db)
    return cfg, db


def test_recommendations_api_returns_current_cards(make_config, tmp_path):
    cfg, db = _seed(make_config, tmp_path)
    client = TestClient(create_app(cfg, db))
    r = client.get("/api/v1/recommendations")
    assert r.status_code == 200
    body = r.json()
    assert body["success"]
    cards = body["data"]["cards"]
    assert len(cards) == 1
    assert cards[0]["symbol"] == "RKLB"
    assert cards[0]["action"] == "DO_NOT_ADD"
    assert cards[0]["action_label"] == "不加仓"


def test_recommendation_history_api(make_config, tmp_path):
    cfg, db = _seed(make_config, tmp_path)
    client = TestClient(create_app(cfg, db))
    r = client.get("/api/v1/recommendations/RKLB/history")
    assert r.status_code == 200
    assert r.json()["data"]["history"][0]["symbol"] == "RKLB"


def test_overview_folds_recommendation_into_positions_advice(make_config, tmp_path):
    """合成建议融入「当前持仓」的建议列，不再是独立的「今日行动」面板。"""
    cfg, db = _seed(make_config, tmp_path)
    client = TestClient(create_app(cfg, db))
    html = client.get("/").text
    assert "<h2>今日行动" not in html          # 独立面板已移除
    assert "RKLB" in html                      # 持仓行在
    assert "不加仓" in html                    # 合成建议动作出现在建议列
    assert "依据仓位上限" in html              # 建议列展示 provenance（decided_by）
    assert 'class="pos-advice"' in html         # 建议列允许完整换行展示
    assert ".posfilter .postbl td.pos-advice{height:auto;overflow:visible;text-overflow:clip;white-space:normal}" in html


def test_overview_without_recommendations_falls_back_to_risk_advice(make_config, tmp_path):
    """无合成建议时，建议列回退到风险预算建议，页面正常渲染、无空面板。"""
    cfg = make_config(db_path=tmp_path / "empty.db")
    db = Database(cfg.db_path)
    db.init_schema()
    client = TestClient(create_app(cfg, db))
    html = client.get("/").text
    assert "<h2>今日行动" not in html
    assert "当前持仓" in html                  # 持仓区正常渲染
