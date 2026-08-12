"""持仓周期、计划审计和投资日记归档专项测试。"""
from pathlib import Path
from decimal import Decimal
from urllib.parse import unquote

import pytest
from fastapi.testclient import TestClient

from app.db import Database
from app.position_plans import PositionPlanError, PositionPlanService
from app.position_plan_view import format_datetime, format_percent, format_price, status_label
from app.recommendation_service import persist as persist_recommendation
from app.webapp import create_app


def _service(tmp_path, account="ACC"):
    db = Database(Path(tmp_path) / f"{account}.db")
    db.init_schema()
    return db, PositionPlanService(db, account)


def _position(qty, *, symbol="RKLB", price="10", avg_cost="10"):
    return {"symbol": symbol, "qty": str(qty), "price": price,
            "avg_cost": avg_cost, "market_value": str(float(qty) * float(price)),
            "upl": "0"}


def _execution(eid, ts, side="BUY", shares="10", price="10", account="ACC"):
    return {"exec_id": eid, "correction_root": eid, "execution_time": ts,
            "symbol": "RKLB", "side": side, "shares": shares, "price": price,
            "account": account, "raw_json": "{}"}


def _open(db, service, *, snapshot="s1", observed="2026-08-05T10:00:00+00:00",
          qty="10"):
    service.reconcile(snapshot_id=snapshot, observed_at=observed,
                      positions=[_position(qty)])
    return service.list_cycles()[0]["id"]


def test_new_positive_position_creates_one_pending_cycle(tmp_path):
    db, service = _service(tmp_path)
    cycle_id = _open(db, service)
    rows = service.list_cycles()
    assert len(rows) == 1
    assert rows[0]["id"] == cycle_id
    assert rows[0]["status"] == "PENDING"
    assert rows[0]["current_quantity"] == "10"


def test_repeated_execution_and_snapshot_are_idempotent(tmp_path):
    db, service = _service(tmp_path)
    db.upsert_executions([_execution("e1", "2026-08-05T10:00:00+00:00")], account="ACC")
    _open(db, service)
    service.reconcile(snapshot_id="s1", observed_at="2026-08-05T10:00:00+00:00",
                      positions=[_position("10")])
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM position_cycles").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM position_cycle_events").fetchone()[0] == 1


def test_add_and_reduce_keep_same_cycle(tmp_path):
    db, service = _service(tmp_path)
    cycle_id = _open(db, service)
    service.reconcile(snapshot_id="s2", observed_at="2026-08-05T10:01:00+00:00",
                      positions=[_position("15")])
    service.reconcile(snapshot_id="s3", observed_at="2026-08-05T10:02:00+00:00",
                      positions=[_position("8")])
    rows = service.list_cycles()
    assert len(rows) == 1 and rows[0]["id"] == cycle_id
    assert rows[0]["current_quantity"] == "8"
    with db.connect() as conn:
        types = [r["event_type"] for r in conn.execute(
            "SELECT event_type FROM position_cycle_events WHERE position_cycle_id=? "
            "ORDER BY execution_time, created_at", (cycle_id,))]
    assert types[:2] == ["ADD", "REDUCE"]


def test_partial_zero_does_not_close_and_two_reliable_zeros_archive(tmp_path):
    db, service = _service(tmp_path)
    cycle_id = _open(db, service)
    service.reconcile(snapshot_id="s2", observed_at="2026-08-05T10:01:00+00:00", positions=[])
    assert service.list_cycles()[0]["status"] == "PENDING"
    service.reconcile(snapshot_id="s3", observed_at="2026-08-05T10:02:00+00:00", positions=[])
    assert service.list_cycles(include_closed=True)[0]["status"] == "ARCHIVED"
    with db.connect() as conn:
        journal = conn.execute(
            "SELECT source_type, position_cycle_id, is_readonly FROM journal_entries"
        ).fetchone()
    assert journal["source_type"] == "POSITION_PLAN_ARCHIVE"
    assert journal["position_cycle_id"] == cycle_id
    assert journal["is_readonly"] == 1


def test_same_symbol_rebuy_creates_new_cycle(tmp_path):
    db, service = _service(tmp_path)
    first = _open(db, service)
    service.reconcile(snapshot_id="s2", observed_at="2026-08-05T10:01:00+00:00", positions=[])
    service.reconcile(snapshot_id="s3", observed_at="2026-08-05T10:02:00+00:00", positions=[])
    service.reconcile(snapshot_id="s4", observed_at="2026-08-05T11:00:00+00:00",
                      positions=[_position("5", price="12")])
    current = service.list_cycles()[0]
    assert current["id"] != first
    assert current["cycle_number"] == 2
    assert len(service.list_cycles(include_closed=True)) == 2


def test_plan_version_is_immutable_and_price_match_is_conservative(tmp_path):
    db, service = _service(tmp_path)
    cycle_id = _open(db, service)
    created = service.create_version(cycle_id, {
        "current_conclusion": "观察加仓", "max_weight": "0.30",
        "thesis": "产品订单兑现",
        "invalidation_conditions": "订单取消",
        "actions": [{"action_type": "ADD", "trigger_type": "PRICE",
                      "price_min": "9", "price_max": "11", "quantity": "5"}],
    })
    db.upsert_executions([_execution("e2", "2026-08-05T10:01:00+00:00",
                                     shares="5", price="10.5")], account="ACC")
    service.process_executions()
    with db.connect() as conn:
        link = conn.execute(
            "SELECT match_status FROM position_plan_execution_links WHERE execution_id='e2'"
        ).fetchone()
        assert link["match_status"] == "MATCHED"
        with pytest.raises(Exception):
            conn.execute("UPDATE position_plan_versions SET thesis='改写' WHERE id=?",
                         (created["version"]["id"],))


def test_plan_outside_operation_is_marked_not_forced_matched(tmp_path):
    db, service = _service(tmp_path)
    cycle_id = _open(db, service)
    service.create_version(cycle_id, {
        "current_conclusion": "观察加仓", "max_weight": "0.30",
        "thesis": "理由", "invalidation_conditions": "失效",
        "actions": [{"action_type": "ADD", "trigger_type": "PRICE",
                      "price_min": "9", "price_max": "11", "quantity": "5"}],
    })
    db.upsert_executions([_execution("e2", "2026-08-05T10:01:00+00:00",
                                     shares="5", price="20")], account="ACC")
    service.process_executions()
    with db.connect() as conn:
        assert conn.execute(
            "SELECT match_status FROM position_plan_execution_links WHERE execution_id='e2'"
        ).fetchone()["match_status"] == "PLAN_OUTSIDE"


def test_account_isolation_for_same_symbol(tmp_path):
    db_a, a = _service(tmp_path, "A")
    b = PositionPlanService(a.db, "B")
    a.reconcile(snapshot_id="a1", observed_at="2026-08-05T10:00:00+00:00",
                positions=[_position("10")])
    b.reconcile(snapshot_id="b1", observed_at="2026-08-05T10:00:00+00:00",
                positions=[_position("20")])
    assert a.list_cycles()[0]["current_quantity"] == "10"
    assert b.list_cycles()[0]["current_quantity"] == "20"
    assert a.list_cycles()[0]["id"] != b.list_cycles()[0]["id"]


def test_duplicate_archive_is_one_journal_row_and_archive_is_readonly(tmp_path):
    db, service = _service(tmp_path)
    cycle_id = _open(db, service)
    service.reconcile(snapshot_id="s2", observed_at="2026-08-05T10:01:00+00:00", positions=[])
    service.reconcile(snapshot_id="s3", observed_at="2026-08-05T10:02:00+00:00", positions=[])
    first = service.archive_cycle(cycle_id)
    second = service.archive_cycle(cycle_id)
    assert first["journal_entry_id"] == second["journal_entry_id"]
    with db.connect() as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM journal_entries WHERE position_cycle_id=?", (cycle_id,)
        ).fetchone()[0] == 1
    assert db.update_journal_entry(first["journal_entry_id"], "不能修改") is False
    assert db.delete_journal_entry(first["journal_entry_id"]) is False


def test_unreliable_zero_does_not_advance_close_counter(tmp_path):
    db, service = _service(tmp_path)
    cycle_id = _open(db, service)
    service.reconcile(snapshot_id="bad1", observed_at="2026-08-05T10:01:00+00:00",
                      positions=[], allow_close=False)
    service.reconcile(snapshot_id="bad2", observed_at="2026-08-05T10:02:00+00:00",
                      positions=[], allow_close=False)
    with db.connect() as conn:
        row = conn.execute(
            "SELECT status, zero_observed_count FROM position_cycles WHERE id=?",
            (cycle_id,)).fetchone()
    assert row["status"] == "PENDING"
    assert row["zero_observed_count"] == 0


def test_late_execution_is_audited_without_mutating_archive(tmp_path):
    db, service = _service(tmp_path)
    cycle_id = _open(db, service)
    service.reconcile(snapshot_id="s2", observed_at="2026-08-05T10:01:00+00:00", positions=[])
    service.reconcile(snapshot_id="s3", observed_at="2026-08-05T10:02:00+00:00", positions=[])
    with db.connect() as conn:
        before = conn.execute(
            "SELECT metadata_json FROM journal_entries WHERE position_cycle_id=?",
            (cycle_id,)).fetchone()[0]
    db.upsert_executions([_execution("late", "2026-08-05T10:01:30+00:00",
                                     side="SELL", shares="10")], account="ACC")
    result = service.process_executions()
    assert result["late_executions"] == 1
    with db.connect() as conn:
        after = conn.execute(
            "SELECT metadata_json FROM journal_entries WHERE position_cycle_id=?",
            (cycle_id,)).fetchone()[0]
        assert conn.execute(
            "SELECT COUNT(*) FROM position_plan_audit_log WHERE operation='LATE_EXECUTION'"
        ).fetchone()[0] == 1
    assert before == after


def test_api_and_overview_expose_plan_module(make_config, tmp_path):
    cfg = make_config(db_path=tmp_path / "api.db")
    db = Database(cfg.db_path)
    db.init_schema()
    ts = "2026-08-05T10:00:00+00:00"
    db.write_account_snapshot(
        snapshot_id="snap-1", ts=ts,
        balances={"account": "ACC", "equity": "10000", "cash": "9000",
                  "margin_balance": "0", "buying_power": "9000"},
        positions=[_position("10")],
    )
    service = PositionPlanService(db, "ACC")
    cycle_id = _open(db, service, snapshot="plan-1", observed=ts)
    client = TestClient(create_app(cfg, db))
    overview = client.get("/")
    assert overview.status_code == 200
    assert "持仓决策与操作计划" in overview.text
    # 待完善卡片直接内嵌填写表单（队列提示已在 refactor 493d4ff 移除）。
    assert "待完善" in overview.text
    assert 'name="thesis"' in overview.text
    # 操作计划改为多行「数值 + 单位」，字段名统一为 act_*；旧的 amount/percentage/
    # checkbox 已不再常驻表单。
    assert 'name="act_type"' in overview.text
    assert 'name="act_amount"' in overview.text
    assert 'name="percentage"' not in overview.text
    assert 'name="hold_only_review"' not in overview.text
    assert 'name="limit_confirmed"' not in overview.text
    listing = client.get("/api/v1/position-plans?account=ACC")
    assert listing.status_code == 200
    assert listing.json()["data"]["cycles"][0]["id"] == cycle_id
    created = client.post(
        f"/api/v1/position-plans/{cycle_id}/versions?account=ACC",
        json={"current_conclusion": "持有", "max_weight": "0.30",
              "thesis": "API 计划", "invalidation_conditions": "API 失效",
              "hold_only_review": True, "review_at": "2026-08-10T10:00:00+00:00"},
    )
    assert created.status_code == 201
    detail = client.get(f"/api/v1/position-plans/{cycle_id}?account=ACC")
    assert detail.status_code == 200
    assert detail.json()["data"]["latest_plan"]["thesis"] == "API 计划"


def test_pending_and_draft_are_not_counted_as_active(tmp_path):
    db, service = _service(tmp_path)
    cycle_id = _open(db, service)
    assert service.dashboard()["counts"]["active"] == 0
    assert service.dashboard()["counts"]["pending"] == 1
    service.save_draft(cycle_id, {
        "current_conclusion": "观察加仓", "thesis": "订单兑现",
        "invalidation_conditions": "订单取消", "max_weight": "0.3",
        "actions": [{"action_type": "ADD", "trigger_type": "PRICE",
                      "price_min": "9", "price_max": "11", "quantity": "5"}],
    })
    assert service.dashboard()["counts"]["active"] == 0
    assert service.dashboard()["counts"]["draft"] == 1
    with db.connect() as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM position_plan_versions WHERE position_cycle_id=? "
            "AND lifecycle_status='PUBLISHED'", (cycle_id,)).fetchone()[0] == 0
        assert conn.execute(
            "SELECT status FROM position_plan_actions WHERE position_cycle_id=?",
            (cycle_id,)).fetchone()[0] == "DRAFT"


def test_publish_creates_formal_version_and_active_trigger(tmp_path):
    db, service = _service(tmp_path)
    cycle_id = _open(db, service)
    result = service.publish_version(cycle_id, {
        "current_conclusion": "观察加仓", "thesis": "订单兑现",
        "invalidation_conditions": "订单取消", "max_weight": "0.3",
        "actions": [{"action_type": "ADD", "trigger_type": "PRICE",
                      "price_min": "8", "price_max": "9", "quantity": "5"}],
    })
    assert result["published"] is True
    assert result["version"]["version"] == 1
    assert result["version"]["lifecycle_status"] == "PUBLISHED"
    assert result["version"]["actions"][0]["status"] == "ACTIVE"
    assert service.dashboard()["counts"]["active"] == 1


def test_active_modification_keeps_old_version_immutable(tmp_path):
    db, service = _service(tmp_path)
    cycle_id = _open(db, service)
    first = service.publish_version(cycle_id, {
        "current_conclusion": "持有", "thesis": "订单兑现",
        "invalidation_conditions": "订单取消", "max_weight": "0.3",
        "hold_only_review": True, "review_at": "2026-08-10T10:00:00+00:00",
    })
    service.save_draft(cycle_id, {
        "current_conclusion": "计划减仓", "thesis": "财报后重新评估",
        "invalidation_conditions": "订单取消", "max_weight": "0.3",
        "change_reason": "财报变化",
        "actions": [{"action_type": "REDUCE", "trigger_type": "EVENT",
                      "event_condition": "财报公布", "percentage": "0.3"}],
    })
    service.publish_version(cycle_id)
    detail = service.detail(cycle_id)
    assert [v["version"] for v in detail["versions"]] == [2, 1]
    assert detail["versions"][1]["thesis"] == "订单兑现"
    with db.connect() as conn:
        with pytest.raises(Exception):
            conn.execute("UPDATE position_plan_versions SET thesis='篡改' WHERE id=?",
                         (first["version"]["id"],))


def test_labels_and_user_time_formats_are_localized(tmp_path):
    assert status_label("PENDING") == "待完善"
    # 展示统一收敛到两位小数（commit b695c1c）；全精度值仍存库供触发/对账。
    assert format_price("75.17370555") == "$75.17"
    assert format_percent("0.151") == "15.1%"
    assert format_datetime("2026-08-08T14:30:16.025525+00:00") == "8月8日 14:30"


def test_account_and_stock_weights_use_distinct_denominators(tmp_path):
    db, service = _service(tmp_path)
    db.write_account_snapshot(
        snapshot_id="balance-1", ts="2026-08-05T10:00:00+00:00",
        balances={"account": "ACC", "equity": "1000", "cash": "0",
                  "margin_balance": "0", "buying_power": "0"},
        positions=[_position("10", price="10"), _position("10", symbol="BMNR", price="5")],
    )
    service.reconcile(snapshot_id="s1", observed_at="2026-08-05T10:00:00+00:00",
                      positions=[_position("10", price="10"),
                                 _position("10", symbol="BMNR", price="5")])
    rows = {r["symbol"]: r for r in service.list_cycles()}
    assert rows["RKLB"]["account_weight"] == "0.1"
    assert rows["RKLB"]["stock_weight"] == str(Decimal("100") / Decimal("150"))


def test_empty_action_amount_cannot_be_published_and_review_is_not_exit(tmp_path):
    db, service = _service(tmp_path)
    cycle_id = _open(db, service)
    service.save_draft(cycle_id, {
        "current_conclusion": "观察加仓", "thesis": "理由",
        "invalidation_conditions": "失效", "max_weight": "0.3",
        "actions": [{"action_type": "REDUCE", "trigger_type": "EVENT",
                      "event_condition": "财报公布"}],
    })
    with pytest.raises(PositionPlanError):
        service.publish_version(cycle_id)
    service.save_draft(cycle_id, {
        "current_conclusion": "持有", "thesis": "理由",
        "invalidation_conditions": "失效", "max_weight": "0.3",
        "actions": [{"action_type": "REVIEW", "trigger_type": "EVENT",
                      "event_condition": "财报公布", "percentage": "0.3"}],
    })
    service.publish_version(cycle_id)
    card = service.dashboard()["cards"][0]
    assert card["next_action"].startswith("重新评估")
    assert card["risk_exit_line"] == "退出计划未设置"


def test_draft_placeholder_is_not_shown_as_formal_action(tmp_path):
    db, service = _service(tmp_path)
    cycle_id = _open(db, service)
    service.save_draft(cycle_id, {
        "current_conclusion": "观察加仓", "thesis": "理由",
        "invalidation_conditions": "失效", "max_weight": "0.3",
        "actions": [{"action_type": "EXIT", "trigger_type": "EVENT",
                      "event_condition": "财报公布"}],
    })
    card = service.dashboard()["cards"][0]
    assert card["has_published_plan"] is False
    assert "全部退出" not in card["next_action"]


def test_web_form_publishes_via_merged_amount_and_keeps_input_on_failure(
        make_config, tmp_path):
    cfg = make_config(db_path=tmp_path / "form.db")
    db = Database(cfg.db_path)
    db.init_schema()
    ts = "2026-08-05T10:00:00+00:00"
    db.write_account_snapshot(
        snapshot_id="snap-1", ts=ts,
        balances={"account": "ACC", "equity": "10000", "cash": "9000",
                  "margin_balance": "0", "buying_power": "9000"},
        positions=[_position("10")],
    )
    service = PositionPlanService(db, "ACC")
    cycle_id = _open(db, service, snapshot="plan-1", observed=ts)
    client = TestClient(create_app(cfg, db), follow_redirects=False)
    form = {"strategy_type": "波段", "current_conclusion": "观察加仓",
            "thesis": "订单兑现", "invalidation_conditions": "订单取消",
            "max_weight": "0.3", "review_at": "",
            "act_type": "ADD", "act_amount": "5", "act_unit": "shares",
            "act_condition": "回踩9-11加仓"}

    # 缺少必填项时草稿已落库，跳回原卡片带出错误，用户输入不丢。
    broken = client.post(f"/position-plans/{cycle_id}/quick",
                         data={**form, "thesis": "", "intent": "publish"})
    assert broken.status_code == 303
    assert f"plan_error={cycle_id}" in broken.headers["location"]
    assert service.detail(cycle_id)["draft_plan"]["invalidation_conditions"] == "订单取消"
    page = client.get(f"/?plan_error={cycle_id}"
                      "&plan_error_msg=正式发布前还需填写：核心购买理由、最大允许账户仓位")
    assert "正式发布前还需填写：核心购买理由" in page.text
    # 缺必填项不是可以强行放行的情况，哪怕文案里出现了「最大允许账户仓位」。
    assert "确认超限并发布" not in page.text
    over = client.get(f"/?plan_error={cycle_id}"
                      "&plan_error_msg=该加仓计划预计超过最大允许账户仓位，确认后才能发布")
    assert "确认超限并发布" in over.text

    published = client.post(f"/position-plans/{cycle_id}/quick",
                            data={**form, "intent": "publish"})
    assert published.status_code == 303
    assert "plan_error" not in published.headers["location"]
    action = service.detail(cycle_id)["formal_plan"]["actions"][0]
    # 「数值 + 单位」拆回服务端二选一的 quantity / percentage。
    assert action["quantity"] == "5" and not action["percentage"]

    percent_form = {**form, "act_amount": "30", "act_unit": "percent",
                    "act_type": "REDUCE", "change_reason": "仓位变化",
                    "intent": "publish"}
    assert client.post(f"/position-plans/{cycle_id}/quick",
                       data=percent_form).status_code == 303
    action = service.detail(cycle_id)["formal_plan"]["actions"][0]
    assert action["percentage"] == "0.3" and not action["quantity"]


def test_web_form_infers_hold_only_review_from_empty_action_type(
        make_config, tmp_path):
    cfg = make_config(db_path=tmp_path / "hold.db")
    db = Database(cfg.db_path)
    db.init_schema()
    ts = "2026-08-05T10:00:00+00:00"
    db.write_account_snapshot(
        snapshot_id="snap-1", ts=ts,
        balances={"account": "ACC", "equity": "10000", "cash": "9000",
                  "margin_balance": "0", "buying_power": "9000"},
        positions=[_position("10")],
    )
    service = PositionPlanService(db, "ACC")
    cycle_id = _open(db, service, snapshot="plan-1", observed=ts)
    client = TestClient(create_app(cfg, db), follow_redirects=False)
    # 操作类型留空即「仅持有，按期复盘」，不再需要用户勾同义的确认框。
    response = client.post(f"/position-plans/{cycle_id}/quick", data={
        "strategy_type": "长线", "current_conclusion": "持有",
        "thesis": "长期逻辑", "invalidation_conditions": "逻辑破坏",
        "max_weight": "0.3", "review_at": "", "action_type": "",
        "trigger_type": "MANUAL", "amount": "", "amount_unit": "shares",
        "intent": "publish"})
    assert response.status_code == 303
    assert "plan_error" not in response.headers["location"]
    plan = service.detail(cycle_id)["formal_plan"]
    assert plan["hold_only_review"] and not plan["actions"]


def test_web_form_requires_explicit_confirm_to_publish_over_the_weight_limit(
        make_config, tmp_path):
    cfg = make_config(db_path=tmp_path / "limit.db")
    db = Database(cfg.db_path)
    db.init_schema()
    ts = "2026-08-05T10:00:00+00:00"
    db.write_account_snapshot(
        snapshot_id="snap-1", ts=ts,
        balances={"account": "ACC", "equity": "10000", "cash": "9000",
                  "margin_balance": "0", "buying_power": "9000"},
        positions=[_position("10")],
    )
    service = PositionPlanService(db, "ACC")
    cycle_id = _open(db, service, snapshot="plan-1", observed=ts)
    client = TestClient(create_app(cfg, db), follow_redirects=False)
    form = {"strategy_type": "波段", "current_conclusion": "观察加仓",
            "thesis": "订单兑现", "invalidation_conditions": "订单取消",
            "max_weight": "0.02", "review_at": "",
            "act_type": "ADD", "act_amount": "100", "act_unit": "shares",
            "act_condition": "回踩9-11加仓"}

    blocked = client.post(f"/position-plans/{cycle_id}/quick",
                          data={**form, "intent": "publish"})
    assert "预计超过最大允许账户仓位" in unquote(blocked.headers["location"])
    assert not service.detail(cycle_id)["formal_plan"]

    # 二次确认走独立 intent，不常驻表单，也不会在没超限时出现。
    confirmed = client.post(f"/position-plans/{cycle_id}/quick",
                            data={**form, "intent": "publish_confirm"})
    assert confirmed.status_code == 303
    assert "plan_error" not in confirmed.headers["location"]
    assert service.detail(cycle_id)["formal_plan"]["limit_confirmed"]


def test_layout_expanded_card_and_full_width_rows_span_the_grid():
    base = Path(__file__).parents[1] / "app" / "templates" / "base.html"
    overview = Path(__file__).parents[1] / "app" / "templates" / "overview.html"
    form = Path(__file__).parents[1] / "app" / "templates" / "_plan_form.html"
    css = base.read_text(encoding="utf-8")
    form_text = form.read_text(encoding="utf-8")
    overview_text = overview.read_text(encoding="utf-8")
    assert ".pp-card[open]{grid-column:1/-1" in css
    # .full 必须匹配 details 与按钮行，否则它们只占半列、右侧留大片空白。
    assert ".pp-form > .full{grid-column:1/-1}" in css
    assert ".pp-form label.full" not in css
    # 操作计划整段整行跨列；默认折叠、有内容（编辑已有动作）才展开。
    assert '<details class="full"' in form_text
    assert '<details class="full"{% if actions %} open{% endif %}>' in form_text
    # 概览内嵌计划编辑表单与卡片；队列/计数条已在 refactor 493d4ff 移除。
    assert "_plan_form" in overview_text
    assert "pp-card" in overview_text
    assert "pp-status" in overview_text
    # 展开卡片不再被 JS 挪到列表最前，避免点击后位置跳动。
    assert "parentElement.prepend(card)" not in overview_text


def test_cards_default_collapsed_and_open_targets_the_requested_cycle(
        make_config, tmp_path):
    """卡片默认全折叠（refactor 493d4ff 移除待办队列与自动展开），
    只有 ?open=<cycle_id> 指定的卡片展开；service 仍正确追踪 todos/next_review。"""
    cfg = make_config(db_path=tmp_path / "queue.db")
    db = Database(cfg.db_path)
    db.init_schema()
    ts = "2026-08-05T10:00:00+00:00"
    db.write_account_snapshot(
        snapshot_id="snap-queue", ts=ts,
        balances={"account": "ACC", "equity": "10000", "cash": "9000",
                  "margin_balance": "0", "buying_power": "9000"},
        positions=[_position("10")],
    )
    service = PositionPlanService(db, "ACC")
    cycle_id = _open(db, service, snapshot="queue-1", observed=ts)
    client = TestClient(create_app(cfg, db))
    # 默认折叠：PENDING 卡不自动展开
    html = client.get("/").text
    card_pos = html.index(f'id="pp-{cycle_id}"')
    assert "open" not in html[card_pos:card_pos + 120]
    # 指定 ?open= 才展开该卡片
    opened = client.get(f"/?open={cycle_id}").text
    opos = opened.index(f'id="pp-{cycle_id}"')
    assert "open" in opened[opos:opos + 120]

    service.publish_version(cycle_id, {
        "current_conclusion": "持有", "thesis": "理由",
        "invalidation_conditions": "失效", "max_weight": "0.3",
        "hold_only_review": True, "review_at": "2026-08-12T10:00:00+00:00",
    })
    dashboard = service.dashboard(now="2026-08-06T10:00:00+00:00")
    assert dashboard["todos"] == []
    assert dashboard["next_review_at"] == "2026-08-12T10:00:00+00:00"


def test_low_confidence_event_match_stays_pending_confirmation(tmp_path):
    db, service = _service(tmp_path)
    cycle_id = _open(db, service)
    service.publish_version(cycle_id, {
        "current_conclusion": "持有", "thesis": "理由",
        "invalidation_conditions": "失效", "max_weight": "0.3",
        "actions": [{"action_type": "ADD", "trigger_type": "EVENT",
                      "event_condition": "财报公布", "quantity": "5"}],
    })
    db.upsert_executions([_execution("e-low", "2026-08-05T10:01:00+00:00",
                                     shares="5", price="10")], account="ACC")
    service.process_executions()
    with db.connect() as conn:
        assert conn.execute(
            "SELECT match_status FROM position_plan_execution_links WHERE execution_id='e-low'"
        ).fetchone()[0] == "PENDING_CONFIRMATION"


def test_archive_only_creates_investment_journal_not_trade_review(tmp_path):
    db, service = _service(tmp_path)
    cycle_id = _open(db, service)
    before = db.query_reviews(version="v1")[1]
    service.reconcile(snapshot_id="z1", observed_at="2026-08-05T10:01:00+00:00", positions=[])
    service.reconcile(snapshot_id="z2", observed_at="2026-08-05T10:02:00+00:00", positions=[])
    with db.connect() as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM journal_entries WHERE position_cycle_id=?",
            (cycle_id,)).fetchone()[0] == 1
        assert conn.execute(
            "SELECT source_type FROM journal_entries WHERE position_cycle_id=?",
            (cycle_id,)).fetchone()[0] == "POSITION_PLAN_ARCHIVE"
    assert db.query_reviews(version="v1")[1] == before


def test_price_trigger_requires_regular_realtime_quote_with_as_of(tmp_path):
    db, service = _service(tmp_path)
    cycle_id = _open(db, service, observed="2026-08-05T14:20:00+00:00")
    created = service.create_version(cycle_id, {
        "current_conclusion": "达到区间后减仓", "max_weight": "0.30",
        "thesis": "理由", "invalidation_conditions": "失效",
        "actions": [{"action_type": "REDUCE", "trigger_type": "PRICE",
                     "price_min": "9", "price_max": "11", "quantity": "5"}],
    })
    action_id = created["version"]["actions"][0]["id"]

    service.reconcile(
        snapshot_id="delayed", observed_at="2026-08-05T14:31:00+00:00",
        positions=[{**_position("10"), "price_quality": "DELAYED",
                    "price_source": "IBKR_DELAYED",
                    "quote_as_of": "2026-08-05T14:30:00+00:00"}],
    )
    with db.connect() as conn:
        action = conn.execute(
            "SELECT * FROM position_plan_actions WHERE id=?", (action_id,)).fetchone()
        assert action["status"] == "ACTIVE"
        assert action["trigger_evaluation_status"] == "BLOCKED"
        assert "DELAYED" in action["trigger_evaluation_note"]

    service.reconcile(
        snapshot_id="live", observed_at="2026-08-05T14:32:00+00:00",
        positions=[{**_position("10"), "price_quality": "REALTIME",
                    "price_source": "FUTU_REGULAR",
                    "quote_as_of": "2026-08-05T14:31:59+00:00"}],
    )
    with db.connect() as conn:
        action = conn.execute(
            "SELECT * FROM position_plan_actions WHERE id=?", (action_id,)).fetchone()
        assert action["status"] == "TRIGGERED"
        assert action["trigger_evaluation_status"] == "TRIGGERED"
        assert '"session": "REGULAR"' in action["trigger_quote_json"]


def test_price_trigger_uses_quote_as_of_session_not_sync_time(tmp_path):
    db, service = _service(tmp_path)
    cycle_id = _open(db, service, observed="2026-08-05T14:20:00+00:00")
    created = service.create_version(cycle_id, {
        "current_conclusion": "达到区间后减仓", "max_weight": "0.30",
        "thesis": "理由", "invalidation_conditions": "失效",
        "actions": [{"action_type": "REDUCE", "trigger_type": "PRICE",
                     "price_min": "9", "price_max": "11", "quantity": "5"}],
    })
    action_id = created["version"]["actions"][0]["id"]

    service.reconcile(
        snapshot_id="post-quote", observed_at="2026-08-05T14:32:00+00:00",
        positions=[{**_position("10"), "price_quality": "REALTIME",
                    "price_source": "FUTU",
                    "quote_as_of": "2026-08-05T21:00:00+00:00"}],
    )

    with db.connect() as conn:
        action = conn.execute(
            "SELECT status, trigger_evaluation_note FROM position_plan_actions WHERE id=?",
            (action_id,),
        ).fetchone()
    assert action["status"] == "ACTIVE"
    assert "POST" in action["trigger_evaluation_note"]


def test_manual_action_confirmation_is_intent_not_execution(tmp_path):
    db, service = _service(tmp_path)
    cycle_id = _open(db, service)
    created = service.create_version(cycle_id, {
        "current_conclusion": "人工复核后减仓", "max_weight": "0.30",
        "thesis": "理由", "invalidation_conditions": "失效",
        "actions": [{"action_type": "REDUCE", "trigger_type": "MANUAL",
                     "quantity": "5", "additional_conditions": "人工确认"}],
    })
    action_id = created["version"]["actions"][0]["id"]

    confirmed = service.confirm_action_intent(cycle_id, action_id, actor="tester")

    assert confirmed["status"] == "TRIGGERED"
    assert confirmed["confirmation_intent"] == "EXECUTE"
    assert confirmed["confirmed_by"] == "tester"
    assert confirmed["executed_at"] is None
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 0
        operation = conn.execute(
            "SELECT operation FROM position_plan_audit_log ORDER BY id DESC LIMIT 1"
        ).fetchone()[0]
        assert operation == "CONFIRM_ACTION_INTENT"


def test_confirmed_action_links_execution_and_sets_decision_id(tmp_path):
    db, service = _service(tmp_path)
    cycle_id = _open(db, service)
    created = service.create_version(cycle_id, {
        "current_conclusion": "人工复核后减仓", "max_weight": "0.30",
        "thesis": "理由", "invalidation_conditions": "失效",
        "actions": [{"action_type": "REDUCE", "trigger_type": "MANUAL",
                     "quantity": "5", "additional_conditions": "人工确认"}],
    })
    action_id = created["version"]["actions"][0]["id"]
    service.confirm_action_intent(cycle_id, action_id, actor="tester")
    db.upsert_executions([
        _execution("sell-1", "2026-08-05T10:05:00+00:00", side="SELL", shares="5")
    ], account="ACC")
    service.process_executions()

    service.confirm_execution(
        cycle_id, "sell-1", match_status="MATCHED", action_id=action_id,
        actor="tester",
    )

    with db.connect() as conn:
        execution = conn.execute(
            "SELECT decision_id FROM executions WHERE exec_id='sell-1'"
        ).fetchone()
        action = conn.execute(
            "SELECT status, execution_id FROM position_plan_actions WHERE id=?",
            (action_id,),
        ).fetchone()
        link = conn.execute(
            "SELECT match_status, confirmed_by_user FROM position_plan_execution_links "
            "WHERE execution_id='sell-1'"
        ).fetchone()
    assert execution["decision_id"] == action_id
    assert tuple(action) == ("EXECUTED", "sell-1")
    assert tuple(link) == ("MATCHED", "tester")


@pytest.mark.parametrize(
    ("observed_at", "expected_session"),
    [
        ("2026-08-05T12:00:00+00:00", "PRE"),
        ("2026-08-05T21:00:00+00:00", "POST"),
        ("2026-08-06T01:00:00+00:00", "OVERNIGHT"),
    ],
)
def test_price_trigger_blocks_every_extended_session(tmp_path, observed_at, expected_session):
    db, service = _service(tmp_path)
    cycle_id = _open(db, service, observed="2026-08-05T10:00:00+00:00")
    created = service.create_version(cycle_id, {
        "current_conclusion": "达到区间后减仓", "max_weight": "0.30",
        "thesis": "理由", "invalidation_conditions": "失效",
        "actions": [{"action_type": "REDUCE", "trigger_type": "PRICE",
                     "price_min": "9", "price_max": "11", "quantity": "5"}],
    })
    action_id = created["version"]["actions"][0]["id"]

    service.reconcile(
        snapshot_id=expected_session.lower(), observed_at=observed_at,
        positions=[{**_position("10"), "price_quality": "REALTIME",
                    "price_source": "FUTU", "quote_as_of": observed_at}],
    )

    with db.connect() as conn:
        action = conn.execute(
            "SELECT status, trigger_evaluation_status, trigger_evaluation_note, "
            "trigger_quote_json FROM position_plan_actions WHERE id=?", (action_id,)
        ).fetchone()
    assert action["status"] == "ACTIVE"
    assert action["trigger_evaluation_status"] == "BLOCKED"
    assert expected_session in action["trigger_evaluation_note"]
    assert f'"session": "{expected_session}"' in action["trigger_quote_json"]


def test_action_confirmation_automatically_binds_recommendation_that_references_action(tmp_path):
    db, service = _service(tmp_path)
    cycle_id = _open(db, service)
    created = service.create_version(cycle_id, {
        "current_conclusion": "人工复核后减仓", "max_weight": "0.30",
        "thesis": "理由", "invalidation_conditions": "失效",
        "actions": [{"action_type": "REDUCE", "trigger_type": "MANUAL",
                     "quantity": "5", "additional_conditions": "人工确认"}],
    })
    action_id = created["version"]["actions"][0]["id"]
    rec_id = persist_recommendation(db, {
        "account": "ACC", "symbol": "RKLB", "cycle_id": cycle_id,
        "action": "REDUCE", "decided_by": "plan", "reason": "按正式计划减仓",
        "covered_action_ids": [], "input_refs": {"action_id": action_id},
        "decision_fingerprint": "test-bind-action", "rule_set_version": "test",
        "rule_set_hash": "test",
    })

    confirmed = service.confirm_action_intent(cycle_id, action_id, actor="tester")

    assert confirmed["recommendation_id"] == rec_id
    with db.connect() as conn:
        detail = conn.execute(
            "SELECT detail_json FROM position_plan_audit_log "
            "WHERE operation='CONFIRM_ACTION_INTENT' ORDER BY id DESC LIMIT 1"
        ).fetchone()[0]
    assert rec_id in detail


def test_explicit_recommendation_must_reference_confirmed_action(tmp_path):
    db, service = _service(tmp_path)
    cycle_id = _open(db, service)
    created = service.create_version(cycle_id, {
        "current_conclusion": "人工复核后减仓", "max_weight": "0.30",
        "thesis": "理由", "invalidation_conditions": "失效",
        "actions": [{"action_type": "REDUCE", "trigger_type": "MANUAL",
                     "quantity": "5"}],
    })
    action_id = created["version"]["actions"][0]["id"]
    rec_id = persist_recommendation(db, {
        "account": "ACC", "symbol": "RKLB", "cycle_id": cycle_id,
        "action": "HOLD", "decided_by": "none", "reason": "无动作",
        "covered_action_ids": [], "input_refs": {"action_id": "other-action"},
        "decision_fingerprint": "test-wrong-action", "rule_set_version": "test",
        "rule_set_hash": "test",
    })

    with pytest.raises(PositionPlanError, match="未引用该正式操作"):
        service.confirm_action_intent(
            cycle_id, action_id, recommendation_id=rec_id, actor="tester")


def test_manual_execution_confirmation_keeps_partial_action_triggered(tmp_path):
    db, service = _service(tmp_path)
    cycle_id = _open(db, service)
    created = service.create_version(cycle_id, {
        "current_conclusion": "分两次减仓", "max_weight": "0.30",
        "thesis": "理由", "invalidation_conditions": "失效",
        "actions": [{"action_type": "REDUCE", "trigger_type": "MANUAL",
                     "quantity": "10"}],
    })
    action_id = created["version"]["actions"][0]["id"]
    service.confirm_action_intent(cycle_id, action_id, actor="tester")
    db.upsert_executions([
        _execution("sell-partial", "2026-08-05T10:05:00+00:00", side="SELL", shares="4")
    ], account="ACC")
    service.process_executions()

    service.confirm_execution(
        cycle_id, "sell-partial", match_status="MATCHED", action_id=action_id,
        actor="tester",
    )

    with db.connect() as conn:
        action = conn.execute(
            "SELECT status, executed_at, execution_id FROM position_plan_actions WHERE id=?",
            (action_id,),
        ).fetchone()
    assert tuple(action) == ("TRIGGERED", None, "sell-partial")


def test_manual_execution_confirmation_rejects_opposite_action_direction(tmp_path):
    db, service = _service(tmp_path)
    cycle_id = _open(db, service)
    created = service.create_version(cycle_id, {
        "current_conclusion": "只允许加仓", "max_weight": "0.30",
        "thesis": "理由", "invalidation_conditions": "失效",
        "actions": [{"action_type": "ADD", "trigger_type": "MANUAL",
                     "quantity": "5"}],
    })
    action_id = created["version"]["actions"][0]["id"]
    db.upsert_executions([
        _execution("sell-wrong", "2026-08-05T10:05:00+00:00", side="SELL", shares="2")
    ], account="ACC")
    service.process_executions()

    with pytest.raises(PositionPlanError, match="成交方向与所选正式操作不一致"):
        service.confirm_execution(
            cycle_id, "sell-wrong", match_status="MATCHED", action_id=action_id,
            actor="tester",
        )


def test_web_confirmation_routes_persist_intent_and_execution_link(make_config):
    cfg = make_config(ib_account="ACC")
    db = Database(cfg.db_path)
    db.init_schema()
    service = PositionPlanService(db, "ACC")
    cycle_id = _open(db, service)
    created = service.create_version(cycle_id, {
        "current_conclusion": "人工复核后减仓", "max_weight": "0.30",
        "thesis": "理由", "invalidation_conditions": "失效",
        "actions": [{"action_type": "REDUCE", "trigger_type": "MANUAL",
                     "quantity": "5"}],
    })
    action_id = created["version"]["actions"][0]["id"]
    client = TestClient(create_app(cfg, db))

    response = client.post(
        f"/position-plans/{cycle_id}/actions/{action_id}/confirm",
        data={"note": "网页确认"}, follow_redirects=False,
    )
    assert response.status_code == 303
    with db.connect() as conn:
        action = conn.execute(
            "SELECT status, confirmed_by, confirmation_note FROM position_plan_actions WHERE id=?",
            (action_id,),
        ).fetchone()
    assert tuple(action) == ("TRIGGERED", "dashboard", "网页确认")

    db.upsert_executions([
        _execution("sell-web", "2026-08-05T10:05:00+00:00", side="SELL", shares="5")
    ], account="ACC")
    service.process_executions()
    response = client.post(
        f"/position-plans/{cycle_id}/executions/sell-web/confirm",
        data={"match_status": "MATCHED", "action_id": action_id},
        follow_redirects=False,
    )
    assert response.status_code == 303
    with db.connect() as conn:
        link = conn.execute(
            "SELECT match_status, confirmed_by_user FROM position_plan_execution_links "
            "WHERE execution_id='sell-web'"
        ).fetchone()
    assert tuple(link) == ("MATCHED", "dashboard")
