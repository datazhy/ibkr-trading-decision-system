from decimal import Decimal

from fastapi.testclient import TestClient

from app.db import Database
from app.opportunity_service import OpportunityService, rules_hash
from app.webapp import create_app


def _client(make_config, tmp_path, **overrides):
    cfg = make_config(db_path=tmp_path / "web-opportunity.db", **overrides)
    db = Database(cfg.db_path); db.init_schema()
    return cfg, db, TestClient(create_app(cfg, db), follow_redirects=False)


def _seed_scan(cfg, db, svc, symbols=("AAA", "BBB", "CCC", "DDD")):
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO opportunity_scan_runs "
            "(id,status,trigger_source,started_at,finished_at,universe_count,eligible_count,"
            "ready_count,watch_count,blocked_count,market_state,rules_version,rules_hash,data_as_of) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("scan-web", "SUCCESS", "test", "2026-08-10T22:00:00+00:00",
             "2026-08-10T22:01:00+00:00", len(symbols), len(symbols), len(symbols),
             0, 0, "supportive", "test", rules_hash(cfg), "2026-08-10"),)
    for idx, symbol in enumerate(symbols):
        svc._save_candidate("scan-web", {
            "symbol": symbol, "setup_type": "BREAKOUT", "status": "READY",
            "score": 90 - idx, "grade": "A", "confidence": "FULL",
            "current_price": "100", "ema20": "95", "ema50": "90", "atr14": "2",
            "rvol20": "1.5", "entry_low": "100", "entry_high": "101",
            "stop_price": "95", "risk_per_share": "5",
            "median_dollar_volume_20d": "50000000", "relative_strength_20d": "0.1",
            "relative_strength_60d": "0.2", "score_components": {},
            "reasons": ["趋势与量价确认"], "blocked_reasons": [], "input_refs": {},
            "bar_as_of": "2026-08-10", "adjustment_mode": "split-adjusted",
            "quality_status": "FULL",
        }, rules_hash(cfg))


def _seed_proposal(cfg, db, symbol="AAA"):
    with db.connect() as conn:
        candidate_id = conn.execute(
            "SELECT id FROM opportunity_candidates WHERE symbol=?", (symbol,)
        ).fetchone()["id"]
        conn.execute(
            "INSERT INTO portfolio_proposals "
            "(id,scan_id,account,status,market_state,equity,cash,risk_budget_pct,"
            "risk_budget_amount,existing_risk_amount,proposed_risk_amount,"
            "remaining_risk_amount,blocked_reasons,input_refs,rules_version,rules_hash,created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("proposal-web", "scan-web", "DU-WEB", "READY", "supportive", "100000",
             "60000", "0.04", "4000", "1000", "750", "2250", "[]", "{}",
             "test", rules_hash(cfg), "2026-08-10T22:02:00+00:00"))
        conn.execute(
            "INSERT INTO portfolio_proposal_items "
            "(id,proposal_id,symbol,item_type,setup_type,candidate_id,score,"
            "proposed_quantity,proposed_weight,entry_low,entry_high,stop_price,"
            "risk_amount,risk_pct,sector,reason,input_refs,adoption_status,created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("item-web", "proposal-web", symbol, "ADD", "BREAKOUT", candidate_id, 90,
             "100", "0.10", "100", "101", "95", "600", "0.006", "Technology",
             "风险预算内的高分候选", "{}", "PENDING", "2026-08-10T22:02:00+00:00"))


def test_opportunity_page_renders_feature_state_and_mobile_cards(make_config, tmp_path):
    cfg, db, client = _client(make_config, tmp_path, opportunity_enabled=False,
                              portfolio_builder_enabled=False)
    response = client.get("/opportunities")
    assert response.status_code == 200
    assert "机会扫描尚未启用" in response.text
    assert "个人自选池" in response.text
    assert "机会结果复盘" in response.text
    assert "<table" not in response.text


def test_opportunity_page_and_api_show_shadow_readiness(make_config, tmp_path):
    cfg, db, client = _client(
        make_config, tmp_path, opportunity_enabled=True,
        portfolio_builder_enabled=False, opportunity_shadow_min_days=5)
    response = client.get("/opportunities")
    assert response.status_code == 200
    assert "有效影子观察 0 / 5 个交易日" in response.text
    assert "空池扫描不计入观察天数" in response.text
    readiness = client.get("/api/v1/opportunities/readiness")
    assert readiness.status_code == 200
    assert readiness.json()["data"] == {
        "required_days": 5, "completed_days": 0, "remaining_days": 5,
        "effective_dates": [], "active_symbols": 0, "ready": False,
        "opportunity_enabled": True, "portfolio_builder_enabled": False,
    }


def test_watchlist_reason_can_be_edited_from_page_and_persists(make_config, tmp_path):
    cfg, db, client = _client(make_config, tmp_path, opportunity_enabled=False,
                              portfolio_builder_enabled=False)
    created = client.post("/opportunities/watchlist", data={
        "symbol": "NVDA", "sector": "Semiconductors", "why_watch": "初始理由",
    })
    assert created.status_code == 303

    page = client.get("/opportunities")
    assert page.status_code == 200
    assert 'aria-label="NVDA 关注理由"' in page.text
    assert 'value="初始理由"' in page.text

    updated_reason = "更新后的关注理由 <突破>"
    updated = client.post("/opportunities/watchlist", data={
        "symbol": "NVDA", "sector": "Semiconductors", "why_watch": updated_reason,
    })
    assert updated.status_code == 303

    with db.connect() as conn:
        row = conn.execute(
            "SELECT why_watch FROM opportunity_watchlist WHERE symbol='NVDA'"
        ).fetchone()
    assert row["why_watch"] == updated_reason

    page = client.get("/opportunities")
    assert "更新后的关注理由 &lt;突破&gt;" in page.text
    assert "初始理由" not in page.text


def test_editing_watchlist_reason_does_not_resume_paused_symbol(make_config, tmp_path):
    cfg, db, client = _client(make_config, tmp_path, opportunity_enabled=False,
                              portfolio_builder_enabled=False)
    svc = OpportunityService(cfg, db)
    svc.save_watch("NVDA", why_watch="初始理由")
    svc.set_watch_status("NVDA", "PAUSED")

    response = client.post("/opportunities/watchlist", data={
        "symbol": "NVDA", "why_watch": "暂停期间更新",
    })
    assert response.status_code == 303
    with db.connect() as conn:
        row = conn.execute(
            "SELECT status, why_watch FROM opportunity_watchlist WHERE symbol='NVDA'"
        ).fetchone()
    assert dict(row) == {"status": "PAUSED", "why_watch": "暂停期间更新"}


def test_watchlist_api_crud_persists_note_and_flags_are_controlled(make_config, tmp_path):
    cfg, db, client = _client(make_config, tmp_path, opportunity_enabled=False,
                              portfolio_builder_enabled=False)
    created = client.post("/api/v1/opportunities/watchlist", json={
        "symbol": "nvda", "sector": "Semiconductors", "note": "manual note"})
    assert created.status_code == 201
    assert created.json()["data"]["symbol"] == "NVDA"
    assert created.json()["data"]["note"] == "manual note"
    scan = client.post("/api/v1/opportunities/scans")
    assert scan.status_code == 503
    assert scan.json()["data"]["opportunity_enabled"] is False
    archived = client.delete("/api/v1/opportunities/watchlist/NVDA")
    assert archived.status_code == 200
    assert archived.json()["data"]["status"] == "ARCHIVED"


def test_builder_api_is_blocked_by_shadow_gate_even_if_flag_is_on(make_config, tmp_path):
    cfg, db, client = _client(
        make_config, tmp_path, opportunity_enabled=True,
        portfolio_builder_enabled=True, opportunity_shadow_min_days=5)
    response = client.post("/api/v1/opportunities/portfolio-proposals", json={})
    assert response.status_code == 409
    assert "0/5" in response.json()["error"]["message"]


def test_overview_shows_only_top_three_ready_candidates(make_config, tmp_path):
    cfg, db, client = _client(make_config, tmp_path, opportunity_enabled=True,
                              portfolio_builder_enabled=True)
    svc = OpportunityService(cfg, db)
    _seed_scan(cfg, db, svc)
    html = client.get("/").text
    assert "今日机会" in html
    assert "AAA" in html and "BBB" in html and "CCC" in html
    assert "DDD" not in html
    opportunity_html = client.get("/opportunities").text
    assert all(symbol in opportunity_html for symbol in ("AAA", "BBB", "CCC", "DDD"))


def test_opportunity_routes_are_covered_by_site_auth(make_config, tmp_path):
    cfg, db, client = _client(make_config, tmp_path, opportunity_enabled=True,
                              dashboard_auth_enabled=True)
    page = client.get("/opportunities")
    api = client.get("/api/v1/opportunities/watchlist")
    assert page.status_code == 303 and page.headers["location"].startswith("/login?")
    assert api.status_code == 401


def test_opportunity_page_renders_completed_outcome_metrics(make_config, tmp_path):
    cfg, db, client = _client(make_config, tmp_path, opportunity_enabled=True,
                              portfolio_builder_enabled=True,
                              opportunity_shadow_min_days=1)
    svc = OpportunityService(cfg, db)
    _seed_scan(cfg, db, svc, symbols=("AAA",))
    _seed_proposal(cfg, db)
    with db.connect() as conn:
        candidate_id = conn.execute(
            "SELECT id FROM opportunity_candidates WHERE symbol='AAA'"
        ).fetchone()["id"]
    svc._upsert_outcome(
        candidate_id, horizon=5, outcome_date="2026-08-17",
        candidate_price=Decimal("100"),
        outcome_price=Decimal("108"),
        spy_start=Decimal("600"),
        spy_end=Decimal("612"),
        return_pct=Decimal("0.08"),
        spy_return=Decimal("0.02"),
        alpha=Decimal("0.06"),
        mfe=Decimal("0.10"),
        mae=Decimal("-0.03"),
        r_multiple=Decimal("1.6"),
        status="COMPLETE", quality_note="test", calculated_at="2026-08-17T22:00:00+00:00")
    html = client.get("/opportunities").text
    assert "BREAKOUT" in html and "5 日 · 1 样本" in html
    assert "8.0%" in html and "6.0%" in html and "1.60" in html
    assert "只展示，不调参" in html
    assert "采纳为待执行计划" in html
