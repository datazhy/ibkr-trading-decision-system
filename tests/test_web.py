"""Phase 3 看板测试：数据组装、CSV 导出、路由冒烟。"""
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from app import webdata
from app.db import Database
from app.webapp import create_app

NOW = datetime(2026, 7, 10, 18, 0, tzinfo=timezone.utc)


@pytest.fixture
def seeded(make_config):
    # 以 IBKR 为准：明细表读 executions（45s 同步），不再读每日 cron 的 transactions
    cfg = make_config(ib_account="ACC")
    db = Database(cfg.db_path)
    db.init_schema()
    # 净资产曲线：3 天
    for i, eq in enumerate((100000, 101000, 99000)):
        ts = (NOW - timedelta(days=2 - i)).isoformat()
        db.insert_balances_snapshot(ts, {
            "equity": Decimal(eq), "cash": Decimal("20000"), "account": "ACC",
            "margin_balance": Decimal("-100"), "buying_power": Decimal("50000")})
    db.insert_positions_snapshot(NOW.isoformat(), [
        {"symbol": "MU", "qty": Decimal("100"), "avg_cost": Decimal("100"),
         "market_value": Decimal("9000"), "upl": Decimal("-1000")}])
    db.upsert_stop_level("2026-07-10", {
        "symbol": "MU", "side": "LONG", "close": Decimal("90"), "atr": Decimal("2"),
        "swing": Decimal("88"), "stop_price": Decimal("86"), "risk_amount": Decimal("400"),
        "risk_pct": Decimal("0.004"), "exceeds_2pct": False, "suggested_trim": 0,
        "reached_1r": False, "trailing_stop": Decimal("88"), "take_profit": "未达 1R"})
    from app.ibkr_client import split_exec_id
    _root, _seq = split_exec_id("0000mu01.aaaa.01.01")
    db.upsert_executions([{
        "exec_id": "0000mu01.aaaa.01.01", "correction_root": _root, "correction_seq": _seq,
        "symbol": "MU", "side": "SELL", "shares": Decimal("50"), "price": Decimal("95"),
        "perm_id": 900001, "order_id": 0, "client_id": 0, "conid": 111,
        "sec_type": "STK", "exchange": "SMART", "currency": "USD",
        "cumulative_qty": Decimal("50"), "average_price": Decimal("95"),
        "execution_time": NOW.isoformat(), "commission": Decimal("0.5"),
        "commission_currency": "USD", "realized_pnl": Decimal("-250"),
        "liquidation": 0, "order_ref": None, "last_liquidity": 1,
        "data_status": "READY", "raw_json": "{}"}])
    db.record_rule_event("R3", "CRIT", None, "🔴 已动用融资", "margin -100")
    return cfg, db


def test_behavior_counters_and_overview(seeded):
    cfg, db = seeded
    # 一笔当月的逆势加仓：已持有 MU 且 upl<0 时再 BUY
    db.upsert_orders([{
        "order_id": "b1", "ts": NOW.isoformat(), "symbol": "MU", "side": "BUY",
        "order_type": "MARKET", "qty": Decimal("10"), "price": Decimal("90"),
        "status": "FILLED", "raw_json": "{}"}])
    counters = webdata.behavior_counters(cfg, db, NOW)
    # 「本月成交笔数」已删除：语义不明（数的是 orders 表，恒空）
    assert "month_fills" not in counters
    assert counters["counter_adds"] == 1
    # 三种口径各自有名字。fixture 里有一笔 MU 卖出 -> 1 次决策、1 笔成交回报；
    # 卖出是 REDUCE/CLOSE，不计入「风险增加」
    assert counters["decisions_month"] == 1
    assert counters["executions_month"] == 1
    assert counters["risk_increasing_today"] == 0
    # 概览页不再组装资产曲线；历史曲线数据仍由独立接口/函数负责。
    db.backfill_daily_equity("ACC", cfg.market_tz)
    data = webdata.overview_data(cfg, db, NOW)
    assert data["positions"][0]["symbol"] == "MU"
    assert "curve" not in data


def test_history_rows_and_csv(seeded):
    cfg, db = seeded
    rows = webdata.history_rows(cfg, db)
    assert rows[0]["symbol"] == "MU"
    # 已实现盈亏直接取 IBKR CommissionReport.realizedPNL，不再估算
    assert rows[0]["realized"] == Decimal("-250")
    assert rows[0]["realized_source"] == "IBKR_CommissionReport"
    csv_text = webdata.history_csv(cfg, db)
    assert "日期,标的,方向" in csv_text
    assert "MU" in csv_text
    assert csv_text.startswith("﻿")  # BOM


def test_routes_smoke(seeded):
    cfg, db = seeded
    client = TestClient(create_app(cfg, db))
    assert client.get("/healthz").json()["readonly"] is True
    r = client.get("/")
    assert r.status_code == 200 and "当前持仓" in r.text
    assert "资产曲线" not in r.text
    assert "curve-ctrls" not in r.text
    r = client.get("/history")
    assert r.status_code == 200 and "交易历史" in r.text
    r = client.get("/history", params={"symbol": "MU"})
    assert r.status_code == 200 and "MU" in r.text
    r = client.get("/history/export.csv")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/csv")
    assert "MU" in r.text
    r = client.get("/rules")
    assert r.status_code == 200 and "R3" in r.text


def test_live_account_endpoint_returns_one_second_state(seeded):
    cfg, db = seeded
    db.set_live_state({
        "generated_at": NOW.isoformat(), "account": "ACC",
        "balances": {"equity": "99000"},
        "account_pnl": {"daily_pnl": "125"},
        "positions": [{"symbol": "MU", "price": "91", "previous_close": "90",
                       "source": "FUTU_OpenD"}],
        "timestamps": {"positions_as_of": NOW.isoformat()},
    })
    payload = TestClient(create_app(cfg, db)).get("/api/v1/account/live").json()
    assert payload["success"] is True
    assert payload["data"]["positions"][0]["source"] == "FUTU_OpenD"
    assert payload["data"]["positions"][0]["daily_change"] == "1"
    assert payload["data"]["positions"][0]["daily_change_percent"] == str(
        Decimal("1") / Decimal("90"))
    assert payload["data"]["delivery"]["mode"] == "live"


def test_live_api_futu_overlay_is_primary_and_keeps_ibkr_fallback():
    from app.api import _overlay_futu_position_quotes

    class Futu:
        def current(self, symbols, *, now):
            assert symbols == ["MU", "SNDK"]
            return {"MU": {
                "price": Decimal("91"), "previous_close": Decimal("90"),
                "price_source": "LAST", "quality": "REALTIME",
                "as_of": NOW.isoformat(), "source": "FUTU_OpenD",
            }}

    rows = [
        {"symbol": "MU", "price": "89", "source": "IBKR_reqMktData"},
        {"symbol": "SNDK", "price": "1205", "source": "IBKR_reqMktData"},
    ]
    _overlay_futu_position_quotes(rows, Futu(), NOW)
    assert rows[0]["price"] == Decimal("91")
    assert rows[0]["previous_close"] == Decimal("90")
    assert rows[0]["source"] == "FUTU_OpenD"
    assert rows[1]["price"] == "1205"                 # 富途缺失时保留 IBKR
    assert rows[1]["source"] == "IBKR_reqMktData"


def test_no_order_endpoints(seeded):
    """交易只读保证：本地决策数据可写，但绝无券商下单/改单/转账路由。

    「只读·不下单」约束的是券商交易操作。登录、投资日记、持仓计划与机会模块只写
    本地审计/决策数据，因此可以使用写路由，但必须局限在明确的本地功能命名空间。
    """
    cfg, db = seeded
    app = create_app(cfg, db)
    allowed = ("/journal", "/login", "/position-plans", "/opportunities",
               "/api/v1/sync", "/api/v1/opportunities", "/api/v1/position-plans",
               "/api/v1/briefs", "/api/v1/risk", "/api/v1/flex",
               "/api/v1/hyperliquid")
    write_paths = []
    # IncludedRouter 不会在 app.routes 中展开；用 OpenAPI 审计实际暴露的 API。
    for path, operations in app.openapi().get("paths", {}).items():
        methods = set(operations) - {"summary", "description", "parameters", "servers"}
        if any(method.upper() not in {"GET", "HEAD"} for method in methods):
            write_paths.append(path)
    assert all(p.startswith(allowed) for p in write_paths), f"意外写路由: {write_paths}"
    # 任何路径都不得出现下单/交易/转账类语义
    banned = ("order", "trade", "buy", "sell", "transfer", "withdraw", "place", "cancel")
    all_paths = " ".join(app.openapi().get("paths", {}).keys()).lower()
    assert not any(b in all_paths for b in banned), all_paths


def test_position_table_shows_pnl_rate_and_key_positions(make_config):
    """持仓表要能一眼看出：重点仓位、均价、盈亏率。

    盈亏率原本完全没有（只有金额）—— 但「亏 5000」在 1 万的仓位上是灾难，
    在 50 万的仓位上是噪音，不看比率无法判断。
    """
    from decimal import Decimal as D
    from app.webdata import _enrich_positions, KEY_POSITION_WEIGHT

    rows = [
        {"symbol": "BIG", "qty": D("100"), "avg_cost": D("100"),
         "market_value": D("8000"), "upl": D("-2000")},
        {"symbol": "SMALL", "qty": D("50"), "avg_cost": D("20"),
         "market_value": D("1200"), "upl": D("200")},
        {"symbol": "NOCOST", "qty": D("10"), "avg_cost": None,
         "market_value": D("800"), "upl": D("50")},
    ]
    _enrich_positions(rows)
    by = {r["symbol"]: r for r in rows}

    # 成本与盈亏率
    assert by["BIG"]["cost_basis"] == D("10000")
    assert by["BIG"]["upl_pct"] == D("-0.2")
    assert by["SMALL"]["upl_pct"] == D("0.2")
    # 缺均价 -> 算不出来就是 None，**绝不填 0**
    assert by["NOCOST"]["cost_basis"] is None
    assert by["NOCOST"]["upl_pct"] is None

    # 权重与重点标记（分母是多头市值合计 10000）
    assert by["BIG"]["weight"] == D("0.8")
    assert by["BIG"]["is_key"] is True
    assert by["SMALL"]["weight"] == D("0.12")
    assert by["SMALL"]["is_key"] is False
    assert KEY_POSITION_WEIGHT == D("0.20")


def test_position_table_handles_empty_portfolio(make_config):
    """空仓或市值全缺时不能除零，权重一律 None。"""
    from decimal import Decimal as D
    from app.webdata import _enrich_positions

    rows = [{"symbol": "X", "qty": D("1"), "avg_cost": D("1"),
             "market_value": None, "upl": None}]
    _enrich_positions(rows)
    assert rows[0]["weight"] is None
    assert rows[0]["is_key"] is False
    assert rows[0]["upl_pct"] is None
