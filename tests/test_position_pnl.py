"""当前持仓：价格重估、历史已实现盈亏与总计现金验收。"""
from datetime import datetime, timezone
from decimal import Decimal as D

from fastapi.testclient import TestClient

from app import webdata
from app.api import _overlay_futu_position_quotes
from app.db import Database
from app.position_pnl import (
    aggregate_current_cycle_realized,
    aggregate_realized,
    aggregate_realized_for_position,
    rebuild_position_cycles,
)
from app.webapp import create_app


NOW = datetime(2026, 8, 4, 14, 0, tzinfo=timezone.utc)


def _exec(symbol, eid, side, shares, price, ts, *, commission=None, realized=None):
    from app.ibkr_client import split_exec_id
    root, seq = split_exec_id(eid)
    return {
        "exec_id": eid, "correction_root": root, "correction_seq": seq,
        "symbol": symbol, "side": side, "shares": D(str(shares)),
        "price": D(str(price)), "perm_id": 1, "order_id": 1, "client_id": 0,
        "conid": 123, "sec_type": "STK", "exchange": "SMART", "currency": "USD",
        "cumulative_qty": D(str(shares)), "average_price": D(str(price)),
        "execution_time": ts, "commission": None if commission is None else D(str(commission)),
        "commission_currency": "USD", "realized_pnl": realized,
        "liquidation": 0, "order_ref": None, "last_liquidity": 1,
        "data_status": "READY", "raw_json": "{}",
    }


def _db(make_config):
    cfg = make_config(ib_account="U1")
    db = Database(cfg.db_path)
    db.init_schema()
    db.insert_balances_snapshot(NOW.isoformat(), {
        "equity": D("10000"), "cash": D("4000"), "total_cash": D("4000"),
        "margin_balance": D("0"), "buying_power": D("4000"), "account": "U1",
    })
    return cfg, db


def test_latest_price_revalues_market_value_and_unrealized(make_config):
    cfg, db = _db(make_config)
    db.insert_positions_snapshot(NOW.isoformat(), [{
        "symbol": "MRVL", "qty": D("10"), "avg_cost": D("90"),
        "market_value": D("900"), "upl": D("1"), "price": D("100"),
        "price_source": "LAST", "price_quality": "REALTIME",
    }])
    row = webdata.overview_data(cfg, db, NOW)["positions"][0]
    assert row["market_value"] == D("1000")
    assert row["upl"] == D("100")
    assert row["upl_pct"] == D("100") / D("900")


def test_quote_failure_preserves_valid_price_and_does_not_zero_values():
    class NoQuote:
        def current(self, symbols, *, now):
            return {}

    rows = [{"symbol": "MRVL", "price": D("101"), "previous_close": D("100"),
             "qty": D("10"), "avg_cost": D("90"), "upl": D("999"),
             "market_value": D("9999"), "source": "FUTU_OpenD",
             "price_quality": "REALTIME"}]
    _overlay_futu_position_quotes(rows, NoQuote(), NOW)
    assert rows[0]["price"] == D("101")
    assert rows[0]["market_value"] == D("1010")
    assert rows[0]["upl"] == D("110")
    assert rows[0]["price_quality"] == "STALE"


def test_empty_or_zero_quote_cannot_overwrite_valid_position_price():
    class InvalidQuote:
        def current(self, symbols, *, now):
            return {"MRVL": {"price": D("0"), "previous_close": None,
                              "quality": "REALTIME", "source": "FUTU_OpenD"}}

    rows = [{"symbol": "MRVL", "price": D("101"), "previous_close": D("100"),
             "qty": D("10"), "avg_cost": D("90"), "upl": D("999"),
             "market_value": D("9999"), "source": "FUTU_OpenD",
             "price_quality": "REALTIME"}]
    _overlay_futu_position_quotes(rows, InvalidQuote(), NOW)
    assert rows[0]["price"] == D("101")
    assert rows[0]["market_value"] == D("1010")
    assert rows[0]["upl"] == D("110")
    assert rows[0]["price_quality"] == "STALE"


def test_one_bad_quote_does_not_drop_other_positions():
    class Partial:
        def current(self, symbols, *, now):
            return {"MRVL": {"price": D("101"), "previous_close": D("100"),
                              "price_source": "LAST", "quality": "REALTIME",
                              "as_of": NOW.isoformat(), "source": "FUTU_OpenD"}}

    rows = [
        {"symbol": "MRVL", "price": D("99"), "qty": D("10"), "avg_cost": D("90"),
         "source": "IBKR_updatePortfolio", "price_quality": "DELAYED"},
        {"symbol": "SNDK", "price": D("50"), "qty": D("2"), "avg_cost": D("40"),
         "source": "IBKR_updatePortfolio", "price_quality": "DELAYED"},
    ]
    _overlay_futu_position_quotes(rows, Partial(), NOW)
    assert rows[0]["upl"] == D("110")
    assert rows[1]["price"] == D("50")
    assert rows[1]["upl"] == D("20")


def test_fifo_partial_close_keeps_realized_after_remaining_position():
    rows = [
        _exec("MRVL", "mrvl.01.01", "BUY", 10, 100, "2026-08-01T14:00:00+00:00", commission=1),
        _exec("MRVL", "mrvl.02.01", "BUY", 10, 120, "2026-08-02T14:00:00+00:00", commission=1),
        _exec("MRVL", "mrvl.03.01", "SELL", 7, 130, "2026-08-03T14:00:00+00:00", commission=D("0.7")),
    ]
    result = aggregate_realized(rows)
    # FIFO：7×(130−100−0.10)−0.70 = 208.60
    assert result.value == D("208.60")
    assert result.source == "FIFO executions + commissions"


def test_mrvl_broker_realized_for_sold_seven_is_retained_with_open_position(make_config):
    cfg, db = _db(make_config)
    db.upsert_executions([
        _exec("MRVL", "mrvl.01.01", "BUY", 10, 100, "2026-08-01T14:00:00+00:00", commission=1),
        _exec("MRVL", "mrvl.02.01", "SELL", 7, 110, "2026-08-02T14:00:00+00:00",
              commission=D("0.7"), realized=D("68.30")),
    ])
    db.insert_positions_snapshot(NOW.isoformat(), [{
        "symbol": "MRVL", "qty": D("3"), "avg_cost": D("100"),
        "market_value": D("300"), "upl": D("0"), "price": D("105"),
        "price_source": "LAST", "price_quality": "REALTIME",
    }])
    row = webdata.overview_data(cfg, db, NOW)["positions"][0]
    assert row["realized_pnl"] == D("68.30")
    assert row["realized_pnl_source"] == "IBKR executions.realized_pnl"
    assert row["upl"] == D("15")


def test_realized_remains_available_after_full_close_and_reopen_is_new_lot():
    rows = [
        _exec("MRVL", "mrvl.01.01", "BUY", 10, 100, "2026-08-01T14:00:00+00:00", commission=1),
        _exec("MRVL", "mrvl.02.01", "SELL", 10, 110, "2026-08-02T14:00:00+00:00", commission=1),
        _exec("MRVL", "mrvl.03.01", "BUY", 5, 200, "2026-08-03T14:00:00+00:00", commission=1),
    ]
    result = aggregate_realized(rows)
    # 首仓平仓：10×(110−100−0.10)−1 = 98；再建仓不继承旧成本。
    assert result.value == D("98")


def test_broker_realized_value_is_not_double_charged_for_commission():
    row = _exec("MRVL", "mrvl.01.01", "SELL", 7, 110,
                "2026-08-02T14:00:00+00:00", commission=D("0.7"), realized=D("68.3"))
    assert aggregate_realized([row]).value == D("68.3")


def test_new_position_cycle_does_not_inherit_previous_closed_cycle():
    rows = [
        _exec("MU", "mu.01.01", "BUY", 2, 100,
              "2026-08-01T14:00:00+00:00", commission=D("0.2")),
        _exec("MU", "mu.02.01", "SELL", 2, 120,
              "2026-08-02T14:00:00+00:00", commission=D("0.2"),
              realized=D("39.60")),
        # This buy starts a new cycle.  It has no closing execution.
        _exec("MU", "mu.03.01", "BUY", 1, 110,
              "2026-08-03T14:00:00+00:00", commission=D("0.1")),
    ]
    result = aggregate_current_cycle_realized(rows, current_qty=D("1"))
    assert result.value == D("0")
    assert result.source == "current cycle (no closing execution)"
    assert result.cycle_start == "2026-08-03T14:00:00+00:00"
    cycles = rebuild_position_cycles(rows)
    assert [(c.current, c.final_qty, c.realized) for c in cycles] == [
        (False, D("0"), D("39.60")), (True, D("1"), D("0"))]


def test_current_cycle_partial_close_keeps_realized_with_remaining_position():
    rows = [
        _exec("MU", "mu.11.01", "BUY", 10, 100,
              "2026-08-01T14:00:00+00:00", commission=D("1")),
        _exec("MU", "mu.12.01", "SELL", 7, 110,
              "2026-08-02T14:00:00+00:00", commission=D("0.7"),
              realized=D("68.30")),
    ]
    result = aggregate_current_cycle_realized(rows, current_qty=D("3"))
    assert result.value == D("68.30")
    assert result.source == "IBKR executions.realized_pnl"
    assert result.cycle_qty == D("3")


def test_partial_close_with_retention_gap_uses_live_qty_as_inferred_baseline():
    # The retained execution window starts at the sell, while the live
    # snapshot still has 14 long shares.  The sell is a partial close, not an
    # opening short; broker realizedPNL remains usable, FIFO completeness is
    # explicitly marked false because the opening cost is outside the window.
    row = _exec("MRVL", "mrvl.gap.01.01", "SELL", 7, 192,
                "2026-08-01T14:00:00+00:00", commission=D("0.37653"),
                realized=D("-383.62653"))
    result = aggregate_current_cycle_realized([row], current_qty=D("14"))
    assert result.value == D("-383.62653")
    assert result.source == "IBKR executions.realized_pnl"
    assert result.cycle_qty == D("14")
    assert result.complete is False


def test_opening_fill_realized_field_is_ignored():
    # A malformed/broker-carryover realized value on a pure opening fill must
    # not turn a buy-only cycle into a realized profit.
    row = _exec("MU", "mu.21.01", "BUY", 1, 100,
                "2026-08-01T14:00:00+00:00", commission=D("0.1"),
                realized=D("999"))
    result = aggregate_current_cycle_realized([row], current_qty=D("1"))
    assert result.value == D("0")
    assert result.source == "current cycle (no closing execution)"


def test_same_symbol_different_conid_is_not_merged():
    first = _exec("MU", "mu.31.01", "BUY", 1, 100,
                  "2026-08-01T14:00:00+00:00")
    first["conid"] = 111
    second = _exec("MU", "mu.32.01", "SELL", 1, 120,
                   "2026-08-02T14:00:00+00:00", realized=D("20"))
    second["conid"] = 222
    result = aggregate_realized_for_position(
        [first, second], {"symbol": "MU", "conid": 111, "currency": "USD",
                           "qty": D("1")})
    assert result.value == D("0")
    assert result.rows == 1
    assert result.current_cycle is True


def test_same_symbol_different_account_is_not_merged():
    first = _exec("MU", "mu.35.01", "BUY", 1, 100,
                  "2026-08-01T14:00:00+00:00")
    first["account"] = "U1"
    second = _exec("MU", "mu.36.01", "SELL", 1, 120,
                   "2026-08-02T14:00:00+00:00", realized=D("20"))
    second["account"] = "U2"
    result = aggregate_realized_for_position(
        [first, second], {"symbol": "MU", "conid": 123, "currency": "USD",
                           "qty": D("1"), "account": "U1"}, account="U1")
    assert result.rows == 1
    assert result.value == D("0")


def test_long_to_short_reversal_closes_old_cycle_before_opening_new_one():
    rows = [
        _exec("AAA", "aaa.01.01", "BUY", 5, 100,
              "2026-08-01T14:00:00+00:00", commission=D("0.5")),
        _exec("AAA", "aaa.02.01", "SELL", 8, 110,
              "2026-08-02T14:00:00+00:00", commission=D("0.8"),
              realized=D("49.20")),
    ]
    result = aggregate_current_cycle_realized(rows, current_qty=D("-3"))
    assert result.value == D("0")
    cycles = rebuild_position_cycles(rows)
    assert cycles[0].realized == D("49.20") and cycles[0].current is False
    assert cycles[1].direction == "SHORT" and cycles[1].final_qty == D("-3")


def test_short_to_long_reversal_closes_old_cycle_before_opening_new_one():
    rows = [
        _exec("AAA", "aaa.11.01", "SELL", 5, 100,
              "2026-08-01T14:00:00+00:00", commission=D("0.5")),
        _exec("AAA", "aaa.12.01", "BUY", 8, 90,
              "2026-08-02T14:00:00+00:00", commission=D("0.8"),
              realized=D("49.20")),
    ]
    result = aggregate_current_cycle_realized(rows, current_qty=D("3"))
    assert result.value == D("0")
    cycles = rebuild_position_cycles(rows)
    assert cycles[0].direction == "SHORT" and cycles[0].realized == D("49.20")
    assert cycles[1].direction == "LONG" and cycles[1].final_qty == D("3")


def test_overview_new_mu_cycle_shows_zero_realized_not_old_cycle(make_config):
    cfg, db = _db(make_config)
    db.upsert_executions([
        _exec("MU", "mu.41.01", "BUY", 2, 100,
              "2026-08-01T14:00:00+00:00", commission=D("0.2")),
        _exec("MU", "mu.42.01", "SELL", 2, 120,
              "2026-08-02T14:00:00+00:00", commission=D("0.2"),
              realized=D("39.60")),
        _exec("MU", "mu.43.01", "BUY", 1, 110,
              "2026-08-03T14:00:00+00:00", commission=D("0.1")),
    ])
    # 历史 Flex 记录仍保留在数据库，但不得跨净仓位归零点带入新周期。
    db.upsert_realized_pnl({
        "record_id": "flex-old-mu", "account_id": "U1", "symbol": "MU",
        "conid": "123", "close_date": "2026-07-01T14:00:00+00:00",
        "quantity": "2", "realized_pnl": "999", "source": "FLEX_TRADE",
        "created_at": NOW.isoformat(), "updated_at": NOW.isoformat(),
    })
    db.insert_positions_snapshot(NOW.isoformat(), [{
        "symbol": "MU", "conid": 123, "currency": "USD", "qty": D("1"),
        "avg_cost": D("110"), "market_value": D("110"), "upl": D("0"),
        "price": D("110"), "price_source": "LAST", "price_quality": "REALTIME",
    }])
    row = webdata.overview_data(cfg, db, NOW)["positions"][0]
    assert row["realized_pnl"] == D("0")
    assert row["realized_pnl_source"] == "current cycle (no closing execution)"


def test_live_api_uses_current_cycle_realized_value(make_config):
    cfg, db = _db(make_config)
    db.upsert_executions([
        _exec("MU", "mu.51.01", "BUY", 2, 100,
              "2026-08-01T14:00:00+00:00", commission=D("0.2")),
        _exec("MU", "mu.52.01", "SELL", 2, 120,
              "2026-08-02T14:00:00+00:00", commission=D("0.2"),
              realized=D("39.60")),
        _exec("MU", "mu.53.01", "BUY", 1, 110,
              "2026-08-03T14:00:00+00:00", commission=D("0.1")),
    ])
    db.set_live_state({
        "generated_at": NOW.isoformat(), "account": "U1",
        "balances": {"equity": "10000"}, "account_pnl": {"daily_pnl": "0"},
        "positions": [{"symbol": "MU", "conid": 123, "currency": "USD",
                       "qty": "1", "avg_cost": "110", "price": "110",
                       "previous_close": "105", "source": "IBKR"}],
    })
    payload = TestClient(create_app(cfg, db)).get("/api/v1/account/live").json()
    position = payload["data"]["positions"][0]
    assert position["realized_pnl"] == "0"
    assert position["realized_pnl_source"] == "current cycle (no closing execution)"


def test_overview_retention_gap_keeps_mrvl_partial_close_realized(make_config):
    cfg, db = _db(make_config)
    db.upsert_executions([_exec(
        "MRVL", "mrvl.gap.11.01", "SELL", 7, 192,
        "2026-08-01T14:00:00+00:00", commission=D("0.37653"),
        realized=D("-383.62653"),
    )])
    db.insert_positions_snapshot(NOW.isoformat(), [{
        "symbol": "MRVL", "conid": 123, "currency": "USD", "qty": D("14"),
        "avg_cost": D("246.75"), "market_value": D("3454.5"), "upl": D("0"),
        "price": D("246.75"), "price_source": "LAST", "price_quality": "REALTIME",
    }])
    row = webdata.overview_data(cfg, db, NOW)["positions"][0]
    assert row["realized_pnl"] == D("-383.62653")
    assert row["realized_pnl_source"] == "IBKR executions.realized_pnl"


def test_overview_shows_total_cash_after_account_unrealized_and_missing_state(make_config):
    cfg, db = _db(make_config)
    db.insert_positions_snapshot(NOW.isoformat(), [{
        "symbol": "MRVL", "qty": D("1"), "avg_cost": D("90"),
        "market_value": D("90"), "upl": D("0"), "price": D("100"),
        "price_source": "LAST", "price_quality": "REALTIME",
    }])
    html = TestClient(create_app(cfg, db)).get("/").text
    assert "账户未实现" not in html
    assert "净资产" in html
    assert 'data-live-balance="equity"' in html
    assert "10,000.00" in html
    assert "总计现金" in html
    assert "4,000.00" in html

    # 仅缺现金字段时不得误显示 $0.00/0.00。
    cfg2, db2 = _db(make_config)
    with db2.connect() as conn:
        conn.execute("UPDATE balances_snapshot SET total_cash=NULL")
    db2.insert_positions_snapshot(NOW.isoformat(), [])
    html2 = TestClient(create_app(cfg2, db2)).get("/").text
    # <b> 现带 livenum/min-width 属性；核心是现金为 null 显示「数据缺失」而非 $0.00。
    assert "总计现金" in html2
    assert "data-live-total-cash>数据缺失</b>" in html2
