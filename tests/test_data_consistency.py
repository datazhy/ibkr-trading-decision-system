"""数据一致性验收：批次溯源、行情口径、资金流审计、迁移幂等。

对应验收项 4/5/6/7/8/10。
"""
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from app import webdata
from app.db import Database
from app.engine import RuleEngine
from app.notify import Notifier
from app.rules import PriceQuality

NOW = datetime(2026, 7, 16, 5, 10, tzinfo=timezone.utc)   # ET 07-16 01:10（盘后）


@pytest.fixture
def db(make_config):
    cfg = make_config()
    d = Database(cfg.db_path)
    d.init_schema()
    return cfg, d


def _seed_after_hours(d, run_id="batch0001", ts=None):
    """盘后一批快照：价格来自 close 回退（DELAYED_FROZEN），无行情时点。"""
    ts = ts or NOW.isoformat()
    d.insert_balances_snapshot(ts, {
        "equity": Decimal("43500.49"), "cash": Decimal("321.03"),
        "margin_balance": Decimal("321.03"), "buying_power": Decimal("2774.53"),
        "account": "U1234567", "source": "IBKR_accountSummary", "data_quality": "READY",
    }, sync_run_id=run_id)
    d.insert_positions_snapshot(ts, [{
        "symbol": "KMEM", "qty": Decimal("1786"), "avg_cost": Decimal("18.955"),
        "market_value": Decimal("33934.00"), "upl": Decimal("80.37"),
        "price": Decimal("19.14"),              # close，与 mv/qty=19.00 并非同一时点
        "price_source": "CLOSE", "price_quality": "DELAYED_FROZEN",
        "market_data_type": 3, "quote_as_of": None, "source": "IBKR_reqMktData",
    }], sync_run_id=run_id)
    return ts


# ---------------- 验收 7：market_value/qty 不再作为价格 ----------------

def test_overview_price_comes_from_quote_not_mv_div_qty(db):
    cfg, d = db
    _seed_after_hours(d)
    row = webdata.overview_data(cfg, d, NOW)["positions"][0]
    # mv/qty = 33934/1786 = 19.00；行情 close = 19.14。必须取后者。
    assert row["price"] == Decimal("19.14")
    assert row["price"] != Decimal("33934.00") / Decimal("1786")
    assert row["price_source"] == "CLOSE"


def test_rule_context_price_comes_from_quote_not_mv_div_qty(db):
    cfg, d = db
    _seed_after_hours(d)
    eng = RuleEngine(cfg, d, Notifier([], d))
    ctx = eng.build_rule_context(NOW)
    assert ctx.current_prices["KMEM"] == Decimal("19.14")


def test_no_price_means_no_price_not_a_guess(db):
    """行情缺失时 current_prices 里不该出现该标的 —— 宁可没有，也不拿账面价充数。"""
    cfg, d = db
    d.insert_positions_snapshot(NOW.isoformat(), [{
        "symbol": "KMEM", "qty": Decimal("1786"), "avg_cost": Decimal("18.955"),
        "market_value": Decimal("33934.00"), "upl": Decimal("80.37"),
        "price": None, "price_source": "NONE", "price_quality": "MISSING",
    }], sync_run_id="b1")
    eng = RuleEngine(cfg, d, Notifier([], d))
    ctx = eng.build_rule_context(NOW)
    assert "KMEM" not in ctx.current_prices


def test_overview_shows_close_as_closing_price_not_current(db):
    """验收项 4：close 回退时页面必须说「收盘价」，不能说「现价」。"""
    cfg, d = db
    _seed_after_hours(d)
    row = webdata.overview_data(cfg, d, NOW)["positions"][0]
    assert row["price_label"] == "收盘价"
    assert row["quality_label"] == "收盘价"
    assert row["quality_hint"] == "收盘冻结价"


def test_overview_price_label_follows_market_session(db):
    """延迟行情的措辞随市场时段：盘中「现价」、盘后「盘后价」、盘前「盘前价」。"""
    cfg, d = db
    d.insert_positions_snapshot(NOW.isoformat(), [{
        "symbol": "KMEM", "qty": Decimal("100"), "avg_cost": Decimal("19"),
        "market_value": Decimal("1900"), "upl": Decimal("0"),
        "price": Decimal("19.14"), "price_source": "LAST", "price_quality": "DELAYED",
        "market_data_type": 3, "quote_as_of": NOW.isoformat(),
    }], sync_run_id="b1")
    # 盘中：UTC 14:00 = ET 10:00
    mid = datetime(2026, 7, 16, 14, 0, tzinfo=timezone.utc)
    d.insert_positions_snapshot(mid.isoformat(), [{
        "symbol": "KMEM", "qty": Decimal("100"), "avg_cost": Decimal("19"),
        "market_value": Decimal("1900"), "upl": Decimal("0"),
        "price": Decimal("19.14"), "price_source": "LAST", "price_quality": "DELAYED",
        "market_data_type": 3, "quote_as_of": mid.isoformat()}], sync_run_id="b2")
    row = webdata.overview_data(cfg, d, mid)["positions"][0]
    assert row["price_label"] == "现价"        # 盘中
    assert row["quality_hint"] == "约15分钟延迟"
    # 盘后：UTC 22:00 = ET 18:00
    post = datetime(2026, 7, 16, 22, 0, tzinfo=timezone.utc)
    d.insert_positions_snapshot(post.isoformat(), [{
        "symbol": "KMEM", "qty": Decimal("100"), "avg_cost": Decimal("19"),
        "market_value": Decimal("1900"), "upl": Decimal("0"),
        "price": Decimal("19.14"), "price_source": "LAST", "price_quality": "DELAYED",
        "market_data_type": 3, "quote_as_of": post.isoformat()}], sync_run_id="b3")
    assert webdata.overview_data(cfg, d, post)["positions"][0]["price_label"] == "盘后价"


# ---------------- 验收 5：close 回退绝不触发 R7 ----------------

def test_close_fallback_never_triggers_r7(db):
    """止损位被「跌破」，但价格是收盘冻结价 → 绝不能触发。"""
    cfg, d = db
    _seed_after_hours(d)
    d.upsert_stop_level("2026-07-15", {
        "symbol": "KMEM", "side": "LONG", "close": Decimal("19.14"),
        "atr": Decimal("1.5"), "swing": Decimal("18"),
        "stop_price": Decimal("25"),           # 现价 19.14 远低于止损 25
        "risk_amount": Decimal("0"), "risk_pct": Decimal("0"),
        "exceeds_2pct": False, "suggested_trim": 0, "reached_1r": False,
        "trailing_stop": Decimal("18"), "take_profit": "-",
        "atr_period": 9, "bars_used": 10, "data_sufficient": True, "data_note": None,
    })
    eng = RuleEngine(cfg, d, Notifier([], d))
    ctx = eng.build_rule_context(NOW)
    from app.rules import evaluate_ruleset
    ev = evaluate_ruleset(ctx).by_rule()["R7"]
    assert ev.hits == []                       # 一条告警都不许发
    # 冻结价不参与穿越判断：既不报触及，也不报「未触及」
    assert ev.status.value == "NOT_EVALUATED"
    assert "不执行止损穿越判断" in ev.detail


# ---------------- 验收 6：首页与规则页同批次 ----------------

def test_overview_and_rules_read_same_sync_run(db):
    cfg, d = db
    _seed_after_hours(d, run_id="batch0042")
    d.set_sync_state("last_sync_run_id", "batch0042")
    eng = RuleEngine(cfg, d, Notifier([], d))
    eng.evaluate_and_dispatch(NOW)

    status = webdata.data_status_bar(cfg, d, NOW)
    run = webdata.rule_run_status(cfg, d, NOW)
    # 技术标识在详情层，不占用状态栏
    assert status["detail"]["sync_run_id"] == "batch0042"
    assert run["sync_run_id"] == "batch0042"      # 两页指向同一批数据


def test_rule_run_records_snapshot_ids(db):
    cfg, d = db
    ts = _seed_after_hours(d, run_id="batch0042")
    eng = RuleEngine(cfg, d, Notifier([], d))
    eng.evaluate_and_dispatch(NOW)
    row = d.latest_rule_run()
    assert row["sync_run_id"] == "batch0042"
    assert row["positions_snapshot_id"] == ts
    assert row["balances_snapshot_id"] == ts
    # 全部收盘价回退 → 无可信行情时点，绝不能拿运行时间冒充
    assert row["quotes_as_of"] is None
    assert "DELAYED_FROZEN" in row["quote_quality_summary"]


def test_pages_never_contradict_on_quote_quality(db):
    """验收：首页与规则页对同一快照的行情质量必须一致。"""
    cfg, d = db
    _seed_after_hours(d)
    eng = RuleEngine(cfg, d, Notifier([], d))
    eng.evaluate_and_dispatch(NOW)
    row = webdata.overview_data(cfg, d, NOW)["positions"][0]
    run = webdata.rule_run_status(cfg, d, NOW)
    assert row["quality"] == "DELAYED_FROZEN"
    assert "收盘价" in run["quote_summary"]        # 同一措辞体系
    status = webdata.data_status_bar(cfg, d, NOW)
    assert status["entries"][2]["rel"] == "非实时价格"      # 人话，不是枚举名
    assert status["entries"][2]["extra"] == "收盘价（非实时）"
    assert status["detail"]["quote_quality"] == {"DELAYED_FROZEN": 1}


# ---------------- 验收 3：资金流冲正 ----------------

def test_cash_flow_reversal_is_append_only(db):
    cfg, d = db
    fid = d.add_cash_flow(account="A", amount=Decimal("46691"),
                          effective_at=NOW.isoformat(), et_date="2026-07-15",
                          note="入金", external_ref="REF1")
    rid = d.add_cash_flow(account="A", amount=Decimal("-46691"),
                          effective_at=NOW.isoformat(), et_date="2026-07-15",
                          note=f"冲正 #{fid}", reverses_id=fid)
    d.add_cash_flow(account="A", amount=Decimal("46000"),
                    effective_at=NOW.isoformat(), et_date="2026-07-15", note="更正金额")
    rows = d.list_cash_flows("A")
    assert len(rows) == 3                       # 原记录仍在，未被删改
    assert d.get_cash_flow(fid)["amount"] == "46691"
    assert d.get_cash_flow(rid)["reverses_id"] == fid
    from app.pnl import flows_sum
    assert flows_sum({"amount": Decimal(r["amount"])} for r in rows) == Decimal("46000")


def test_cash_flow_records_audit_fields(db):
    cfg, d = db
    fid = d.add_cash_flow(account="A", amount=Decimal("100"), currency="USD",
                          effective_at=NOW.isoformat(), et_date="2026-07-15",
                          note="n", source="IBKR_ACTIVITY", external_ref="ACT-9")
    r = d.get_cash_flow(fid)
    for field in ("account", "currency", "amount", "effective_at", "et_date",
                  "note", "source", "external_ref", "created_at"):
        assert r[field] is not None


# ---------------- 验收 8：多账户主键与幂等迁移 ----------------

def test_daily_equity_is_per_account(db):
    cfg, d = db
    d.upsert_daily_equity("A", "2026-07-15", net_liquidation=Decimal("100"),
                          as_of=NOW.isoformat(), source="S", quality="READY")
    d.upsert_daily_equity("B", "2026-07-15", net_liquidation=Decimal("200"),
                          as_of=NOW.isoformat(), source="S", quality="READY")
    assert d.daily_equity_on_or_before("A", "2026-07-15")["net_liquidation"] == "100"
    assert d.daily_equity_on_or_before("B", "2026-07-15")["net_liquidation"] == "200"


def test_daily_equity_upsert_same_account_date(db):
    cfg, d = db
    for v in ("100", "150"):
        d.upsert_daily_equity("A", "2026-07-15", net_liquidation=Decimal(v),
                              as_of=NOW.isoformat(), source="S", quality="READY")
    rows = d.daily_equity_series("A")
    assert len(rows) == 1 and rows[0]["net_liquidation"] == "150"


def test_pnl_snapshot_unique_per_account_and_as_of(db):
    cfg, d = db
    for acct in ("A", "B"):
        d.insert_pnl_snapshot({
            "account": acct, "as_of": NOW.isoformat(), "et_date": "2026-07-15",
            "daily_pnl": Decimal("-1"), "realized_pnl": Decimal("0"),
            "unrealized_pnl": Decimal("1"), "net_liquidation": Decimal("100"),
            "source": "IBKR_reqPnL", "quality": "OK", "reset_policy": "IBKR_SESSION_DAILY",
        })
    assert d.latest_pnl("A")["account"] == "A"
    assert d.latest_pnl("B")["account"] == "B"


def test_daily_pnl_series_uses_last_safe_value_per_session(db):
    cfg, d = db
    for as_of, value in (("2026-07-16T15:00:00+00:00", "100"),
                         ("2026-07-17T07:59:00+00:00", "194.12")):
        d.insert_pnl_snapshot({
            "account": "A", "as_of": as_of, "et_date": "2026-07-16",
            "daily_pnl": Decimal(value), "realized_pnl": Decimal("0"),
            "unrealized_pnl": Decimal(value), "net_liquidation": Decimal("49000"),
            "source": "IBKR_reqPnL", "quality": "OK",
            "reset_policy": "IBKR_SESSION_DAILY",
        })
    rows = d.daily_pnl_series("A")
    assert len(rows) == 1 and rows[0]["daily_pnl"] == "194.12"


def test_migration_is_idempotent(db):
    """反复 init_schema 不得报错、不得丢数据。"""
    cfg, d = db
    _seed_after_hours(d)
    for _ in range(3):
        d.init_schema()
    assert len(d.latest_positions()) == 1
    assert d.latest_balances()["equity"] == "43500.49"


def test_daily_equity_backfill_uses_et_date(db):
    """验收：日终归档按美东日，不能沿用旧的 UTC substr 口径。"""
    cfg, d = db
    # UTC 07-16 03:00 = ET 07-15 23:00 -> 应归入 07-15
    d.insert_balances_snapshot("2026-07-16T03:00:00+00:00", {
        "equity": Decimal("43500"), "cash": Decimal("0"), "margin_balance": Decimal("0"),
        "buying_power": Decimal("0"), "account": "A"})
    d.backfill_daily_equity("A", cfg.market_tz)
    rows = d.daily_equity_series("A")
    assert [r["et_date"] for r in rows] == ["2026-07-15"]


def test_backfill_is_idempotent(db):
    cfg, d = db
    d.insert_balances_snapshot("2026-07-16T03:00:00+00:00", {
        "equity": Decimal("43500"), "cash": Decimal("0"), "margin_balance": Decimal("0"),
        "buying_power": Decimal("0"), "account": "A"})
    assert d.backfill_daily_equity("A", cfg.market_tz) == 1
    assert d.backfill_daily_equity("A", cfg.market_tz) == 0   # 第二次不重复写


# ---------------- 验收 10：入金标记不计入累计收益 ----------------

def test_deposit_marked_and_excluded_from_cumulative_pnl(db):
    cfg, d = db
    # 入金前 378.83，入金 46691 后 47069.92，随后跌到 43500
    for dt, v in (("2026-07-13", "378.83"), ("2026-07-14", "378.83"),
                  ("2026-07-15", "47069.92"), ("2026-07-16", "43500.49")):
        d.upsert_daily_equity("A", dt, net_liquidation=Decimal(v),
                              as_of=f"{dt}T20:00:00+00:00", source="S", quality="READY")
    d.add_cash_flow(account="A", amount=Decimal("46691"),
                    effective_at="2026-07-15T14:00:00+00:00", et_date="2026-07-15",
                    note="入金", external_ref="REF1")
    d.insert_balances_snapshot(NOW.isoformat(), {
        "equity": Decimal("43500.49"), "cash": Decimal("0"), "margin_balance": Decimal("0"),
        "buying_power": Decimal("0"), "account": "A"})

    cv = webdata.equity_curve_data(cfg, d, "ALL", NOW)
    assert cv["marks"] and cv["marks"][0]["date"] == "2026-07-15"
    # 「资金流调整后盈亏」系列必须扣掉入金：47069.92-378.83-46691 = 0.09（不是 +46691）
    adj = dict(cv["adjusted"])
    assert adj["2026-07-15"] == Decimal("0.09")
    assert adj["2026-07-16"] == Decimal("-3569.34")
    # 「净资产变动」系列则是未扣资金流的原始变化 —— 两个系列语义不同
    assert dict(cv["cum_pnl"])["2026-07-15"] == Decimal("46691.09")
    assert "入金" in cv["svg"]                 # 图上标出了资金流事件


def test_default_range_starts_after_last_cash_flow(db):
    """默认视图裁到最近资金流之后 = 当前账户阶段；ALL 才看完整历史。"""
    cfg, d = db
    for dt, v in (("2026-07-11", "378.83"), ("2026-07-15", "47069.92"),
                  ("2026-07-16", "43500.49")):
        d.upsert_daily_equity("A", dt, net_liquidation=Decimal(v),
                              as_of=f"{dt}T20:00:00+00:00", source="S", quality="READY")
    d.add_cash_flow(account="A", amount=Decimal("46691"),
                    effective_at="2026-07-15T14:00:00+00:00", et_date="2026-07-15",
                    note="入金")
    d.insert_balances_snapshot(NOW.isoformat(), {
        "equity": Decimal("43500.49"), "cash": Decimal("0"), "margin_balance": Decimal("0"),
        "buying_power": Decimal("0"), "account": "A"})

    # 历史只有 5 天 -> 请求 30D 会自动放全（否则默认视图是一张空图）
    d30 = webdata.equity_curve_data(cfg, d, "30D", NOW)
    assert d30["auto_all"] is True and d30["range"] == "ALL"
    assert d30["start_date"] == "2026-07-11"          # 完整历史
    # 历史足够时，非 ALL 范围才会裁到最近一笔资金流之后 = 当前账户阶段
    for i in range(1, 40):
        dt = (date.fromisoformat("2026-07-11") - timedelta(days=i)).isoformat()
        d.upsert_daily_equity("A", dt, net_liquidation=Decimal("378.83"),
                              as_of=f"{dt}T20:00:00+00:00", source="S", quality="READY")
    d30b = webdata.equity_curve_data(cfg, d, "30D", NOW)
    assert d30b["auto_all"] is False
    assert d30b["start_date"] == "2026-07-15"         # 入金前的 378.83 不参与
    assert d30b["phase_note"] is not None
    all_ = webdata.equity_curve_data(cfg, d, "ALL", NOW)
    assert all_["start_date"] < "2026-07-11"          # 完整历史仍可查


# ---------------- 验收 8（续）：从**老库**升级的迁移 ----------------
# 上面的幂等测试建的是新库（表结构已含新列），测不出真实升级路径的问题。
# 线上老库的表早已存在，CREATE TABLE IF NOT EXISTS 会跳过 —— 任何直接引用新列的
# 语句（如索引）都会在补列之前执行而报错。这条测试复现真实升级。

_LEGACY_SCHEMA = """
CREATE TABLE positions_snapshot (
    ts TEXT NOT NULL, symbol TEXT NOT NULL, qty TEXT NOT NULL,
    avg_cost TEXT NOT NULL, market_value TEXT NOT NULL, upl TEXT NOT NULL,
    PRIMARY KEY (ts, symbol));
CREATE TABLE balances_snapshot (
    ts TEXT NOT NULL PRIMARY KEY, equity TEXT NOT NULL, cash TEXT NOT NULL,
    margin_balance TEXT NOT NULL, buying_power TEXT NOT NULL);
CREATE TABLE transactions (
    txn_id TEXT NOT NULL PRIMARY KEY, ts TEXT NOT NULL, symbol TEXT, action TEXT,
    qty TEXT, price TEXT, fees TEXT, amount TEXT, raw_json TEXT NOT NULL);
CREATE TABLE stop_levels (
    symbol TEXT NOT NULL, computed_date TEXT NOT NULL, side TEXT NOT NULL,
    close TEXT NOT NULL, atr TEXT NOT NULL, swing TEXT NOT NULL,
    stop_price TEXT NOT NULL, risk_amount TEXT, risk_pct TEXT,
    exceeds_2pct INTEGER NOT NULL DEFAULT 0, suggested_trim INTEGER NOT NULL DEFAULT 0,
    reached_1r INTEGER NOT NULL DEFAULT 0, trailing_stop TEXT, take_profit TEXT,
    updated_at TEXT NOT NULL, PRIMARY KEY (symbol, computed_date));
CREATE TABLE rule_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT, run_ts TEXT NOT NULL,
    run_status TEXT NOT NULL, snapshot_as_of TEXT,
    account_ready INTEGER NOT NULL DEFAULT 1, data_sources TEXT,
    evaluations TEXT NOT NULL, rules_version TEXT, thresholds_version TEXT, error TEXT);
"""


def test_upgrade_from_legacy_schema(make_config, tmp_path):
    """回归：老库升级不得报错，且历史数据一行不丢。"""
    import sqlite3 as _sq
    cfg = make_config(db_path=tmp_path / "legacy.sqlite")
    con = _sq.connect(cfg.db_path)
    con.executescript(_LEGACY_SCHEMA)
    con.execute("INSERT INTO balances_snapshot VALUES (?,?,?,?,?)",
                ("2026-07-11T01:15:30+00:00", "378.83", "322.12", "322.12", "322.12"))
    con.execute("INSERT INTO positions_snapshot VALUES (?,?,?,?,?,?)",
                ("2026-07-11T01:15:30+00:00", "KMEM", "1786", "18.955", "33934", "80.37"))
    con.commit(); con.close()

    d = Database(cfg.db_path)
    d.init_schema()          # 老库 -> 新结构：这一步曾因索引引用未创建的列而崩溃

    assert d.latest_balances()["equity"] == "378.83"     # 历史数据仍在
    assert d.latest_positions()[0]["symbol"] == "KMEM"
    assert d.latest_positions()[0]["sync_run_id"] is None  # 老行新列为空，可接受
    d.init_schema()          # 再跑一次仍须幂等
    assert len(d.latest_positions()) == 1


def test_legacy_upgrade_creates_new_indexes(make_config, tmp_path):
    import sqlite3 as _sq
    cfg = make_config(db_path=tmp_path / "legacy2.sqlite")
    con = _sq.connect(cfg.db_path)
    con.executescript(_LEGACY_SCHEMA)
    con.commit(); con.close()
    Database(cfg.db_path).init_schema()
    con = _sq.connect(cfg.db_path)
    names = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='index'")}
    assert "idx_positions_run" in names and "idx_balances_run" in names


# ---------------- 今日盈亏的前收口径 ----------------

def test_prev_close_is_before_session_not_latest_bar(db):
    """回归：前收必须取会话日**之前**那根。

    用「最新一根」做前收时，盘后展示价正是那根收盘 → 今日盈亏恒为 +0.00。
    """
    cfg, d = db
    for dt, c in (("2026-07-13", "15.00"), ("2026-07-14", "15.20"), ("2026-07-15", "15.79")):
        d.upsert_daily_bars("BMNR", [{"date": dt, "o": c, "h": c, "l": c, "c": c, "v": 1}])
    # 会话日 07-15（ET 凌晨拿到的是 07-15 的账）-> 前收应为 07-14
    assert webdata.prev_close_for(d, "BMNR", "2026-07-15") == Decimal("15.20")
    # 盘中会话日 07-16 -> 前收为 07-15
    assert webdata.prev_close_for(d, "BMNR", "2026-07-16") == Decimal("15.79")


def test_day_change_prefers_quote_previous_close(db):
    """现价与行情同源昨收直接计算；日线只在行情未带昨收时兜底。"""
    cfg, d = db
    _seed_after_hours(d)
    ts = (NOW + timedelta(seconds=1)).isoformat()
    d.insert_positions_snapshot(ts, [{
        "symbol": "KMEM", "qty": Decimal("1786"), "avg_cost": Decimal("18.955"),
        "market_value": Decimal("33934"), "upl": Decimal("80.37"),
        "price": Decimal("19.14"), "previous_close": Decimal("20.50"),
        "price_source": "LAST", "price_quality": "REALTIME",
        "market_data_type": 1, "quote_as_of": ts, "source": "FUTU_QUOTE",
    }], sync_run_id="b2")
    # 旧测试辅助写入函数不含后加的 previous_close 列，补成生产快照的实际形态。
    with d.connect() as conn:
        conn.execute("UPDATE positions_snapshot SET previous_close=? WHERE ts=? AND symbol=?",
                     ("20.50", ts, "KMEM"))
    # 即使本地日线的昨收不同，也必须使用与现价同源的行情昨收 20.50。
    d.upsert_daily_bars("KMEM", [{"date": "2026-07-15", "o": "20", "h": "20",
                                  "l": "20", "c": "20", "v": 1}])
    row = webdata.overview_data(cfg, d, NOW + timedelta(seconds=1))["positions"][0]
    assert row["prev_close"] == Decimal("20.50")
    assert row["day_change"] == Decimal("-1.36")
    assert row["day_pct"] == Decimal("-1.36") / Decimal("20.50")


def test_day_pnl_comes_from_ibkr_not_self_computed(db):
    """今日盈亏必须用 IBKR 逐持仓 PnL。

    自算 (今收-前收)*数量 对当日建仓的标的严重偏离：实测自算合计 -2784，
    而 IBKR 实际 -690.84（逐持仓之和与账户级 dailyPnL 完全吻合）。
    """
    cfg, d = db
    ts = NOW.isoformat()
    d.insert_balances_snapshot(ts, {
        "equity": Decimal("33934"), "cash": Decimal("0"), "margin_balance": Decimal("0"),
        "buying_power": Decimal("0"), "account": "A"}, sync_run_id="b1")
    d.insert_positions_snapshot(ts, [{
        "symbol": "KMEM", "qty": Decimal("1786"), "avg_cost": Decimal("18.955"),
        "market_value": Decimal("33934"), "upl": Decimal("80.37"),
        "price": Decimal("19.14"), "price_source": "CLOSE",
        "price_quality": "DELAYED_FROZEN", "market_data_type": 3, "quote_as_of": None,
        "conid": 897104052, "day_pnl": Decimal("-678.68"),
        "day_pnl_source": "IBKR_reqPnLSingle",
        "realized_pnl": Decimal("125.75"),
        "realized_pnl_source": "IBKR_reqPnLSingle",
    }], sync_run_id="b1")
    # 前收 20.49 会让自算得出 (19.14-20.49)*1786 = -2411.10 —— 必须不用它
    for dt, c in (("2026-07-14", "20.49"), ("2026-07-15", "19.14")):
        d.upsert_daily_bars("KMEM", [{"date": dt, "o": c, "h": c, "l": c, "c": c, "v": 1}])

    row = webdata.overview_data(cfg, d, NOW)["positions"][0]
    assert row["day_pnl"] == Decimal("-678.68")          # IBKR 的数
    assert row["day_pnl"] != Decimal("-2411.10")         # 不是自算的数
    assert row["day_pnl_source"] == "IBKR_reqPnLSingle"
    assert row["realized_pnl"] == Decimal("125.75")      # 部分平仓的券商权威值
    assert row["realized_pnl_source"] == "IBKR_reqPnLSingle"
    # 涨跌幅是标的价格变化，仍按日线算
    assert row["day_change"] == Decimal("19.14") - Decimal("20.49")
    assert row["day_pct"] == (Decimal("19.14") - Decimal("20.49")) / Decimal("20.49")


def test_day_pnl_estimated_when_no_trades_today(db):
    """IBKR 没给逐持仓 PnL（盘中延迟行情）且该标的**当日无成交** ->
    (现价−前收)×数量 就是准确的，可以估算，但必须标成估算值。"""
    cfg, d = db
    _seed_after_hours(d)      # 未设 day_pnl，且无 executions
    d.upsert_daily_bars("KMEM", [{"date": "2026-07-14", "o": "20", "h": "20",
                                  "l": "20", "c": "20", "v": 1}])
    row = webdata.overview_data(cfg, d, NOW)["positions"][0]
    # 前收 20，现价 19.14，1786 股 -> (19.14-20)*1786
    assert row["day_pnl"] == (Decimal("19.14") - Decimal("20")) * Decimal("1786")
    assert row["day_pnl_is_estimate"] is True
    assert "估算" in row["day_pnl_source"]
    assert row["day_pct"] is not None


def test_day_pnl_blank_when_traded_today_and_no_ibkr_value(db):
    """当日有成交时，成本不是前收 —— 自算会严重偏离，宁可留空。"""
    cfg, d = db
    _seed_after_hours(d)
    d.upsert_daily_bars("KMEM", [{"date": "2026-07-14", "o": "20", "h": "20",
                                  "l": "20", "c": "20", "v": 1}])
    from app.ibkr_client import split_exec_id
    root, seq = split_exec_id("0000aaaa.bbbb.01.01")
    # NOW = ET 07-16 01:10 -> 会话日 07-15；成交落在该会话日内
    d.upsert_executions([{
        "exec_id": "0000aaaa.bbbb.01.01", "correction_root": root, "correction_seq": seq,
        "symbol": "KMEM", "side": "BUY", "shares": Decimal("100"),
        "price": Decimal("19"), "perm_id": 1, "execution_time": "2026-07-15T18:00:00+00:00",
        "raw_json": "{}"}])
    row = webdata.overview_data(cfg, d, NOW)["positions"][0]
    assert row["day_pnl"] is None
    assert "无法按前收估算" in row["day_pnl_source"]
    assert row["day_pct"] is not None      # 涨跌幅仍可算（那是标的价格变化）


def test_day_pnl_uses_prior_session_close(db):
    cfg, d = db
    ts = NOW.isoformat()
    d.insert_balances_snapshot(ts, {
        "equity": Decimal("4728"), "cash": Decimal("0"), "margin_balance": Decimal("0"),
        "buying_power": Decimal("0"), "account": "A"}, sync_run_id="b1")
    d.insert_positions_snapshot(ts, [{
        "symbol": "BMNR", "qty": Decimal("300"), "avg_cost": Decimal("14.61"),
        "market_value": Decimal("4737"), "upl": Decimal("354"),
        "price": Decimal("15.79"), "price_source": "CLOSE",
        "price_quality": "DELAYED_FROZEN", "market_data_type": 3, "quote_as_of": None,
    }], sync_run_id="b1")
    for dt, c in (("2026-07-14", "15.20"), ("2026-07-15", "15.79")):
        d.upsert_daily_bars("BMNR", [{"date": dt, "o": c, "h": c, "l": c, "c": c, "v": 1}])

    row = webdata.overview_data(cfg, d, NOW)["positions"][0]
    # NOW=ET 07-16 01:10 -> 会话日 07-15 -> 前收 15.20（不是最新的 15.79）
    assert row["prev_close"] == Decimal("15.20")
    assert row["day_change"] == Decimal("15.79") - Decimal("15.20")
    assert row["day_pct"] == (Decimal("15.79") - Decimal("15.20")) / Decimal("15.20")
    assert row["day_pct"] != Decimal("0")


def test_day_pnl_blank_without_prior_bar(db):
    """新上市标的没有前一根日线 -> 留空，不拿 0 冒充。"""
    cfg, d = db
    _seed_after_hours(d)
    d.upsert_daily_bars("KMEM", [{"date": "2026-07-15", "o": "19.14", "h": "19.14",
                                  "l": "19.14", "c": "19.14", "v": 1}])
    row = webdata.overview_data(cfg, d, NOW)["positions"][0]
    assert row["prev_close"] is None
    assert row["day_change"] is None and row["day_pct"] is None


# ---------------- P0-2：历史页必须用券商权威值 ----------------

def _seed_dram_trade(d):
    """线上真实成交：DRAM 一买一卖，SELL 带 IBKR realized=-1.260146。

    写 executions —— 明细表读的就是它（每 45 秒同步）。transactions 是每日 cron
    同步的慢表，盘中成交要等到次日凌晨才进去，已不再作为页面数据源。
    """
    from app.ibkr_client import split_exec_id

    def _ex(exec_id, side, price, commission, realized, ts, perm_id):
        root, seq = split_exec_id(exec_id)
        return {
            "exec_id": exec_id, "correction_root": root, "correction_seq": seq,
            "symbol": "DRAM", "side": side, "shares": Decimal("1"),
            "price": Decimal(price), "perm_id": perm_id, "order_id": 0, "client_id": 0,
            "conid": 870556708, "sec_type": "STK", "exchange": "SMART", "currency": "USD",
            "cumulative_qty": Decimal("1"), "average_price": Decimal(price),
            "execution_time": ts, "commission": Decimal(commission),
            "commission_currency": "USD",
            "realized_pnl": Decimal(realized) if realized else None,
            "liquidation": 0, "order_ref": None, "last_liquidity": 1,
            "data_status": "READY", "raw_json": "{}",
        }
    d.upsert_executions([
        _ex("0000f9be.6a586681.01.01", "BUY", "55.87", "0.558703", None,
            "2026-07-16T03:18:36+00:00", 1357395055),
        _ex("0002423f.6a5813ac.01.01", "SELL", "55.90", "0.56035", "-1.260146",
            "2026-07-16T03:36:19+00:00", 1357395068),
    ])


def test_history_uses_broker_realized_directly(db):
    """验收 6：已实现盈亏直接用 IBKR CommissionReport，不做任何估算，
    结果不得再出现 -1.82。"""
    cfg, d = db
    _seed_dram_trade(d)
    sell = webdata.history_rows(cfg, d, action="SELL")[0]
    assert sell["realized"] == Decimal("-1.260146")     # 券商权威值
    assert sell["realized_is_estimate"] is False
    assert sell["realized_source"] == "IBKR_CommissionReport"
    assert sell["realized"] != Decimal("-1.82")


def test_estimate_helper_is_gone(db):
    """用快照均价估算已实现盈亏的老路径已删除 —— 它正是 -1.82 的来源。"""
    assert not hasattr(webdata, "_estimate_realized")


def test_history_shows_execution_time_not_only_date(db):
    """盘中一天多笔，只有日期看不出顺序。"""
    cfg, d = db
    _seed_dram_trade(d)
    rows = webdata.history_rows(cfg, d)
    assert rows[0]["time"] and ":" in rows[0]["time"]
    assert rows[0]["ts"] > rows[1]["ts"]                 # 最新在前


def test_history_summary_never_shows_minus_1_82(db):
    """回归：-1.82 = 估算毛(-0.70) − 双边佣金(1.12)，买入佣金扣了两次。"""
    cfg, d = db
    _seed_dram_trade(d)
    rows = webdata.history_rows(cfg, d)
    summary = webdata.history_summary(cfg, d, rows, now=NOW)
    assert summary["realized"] == Decimal("-1.260146")
    assert summary["realized"] != Decimal("-1.82")
    assert "net_realized" not in summary                 # 错误公式的字段已删除
    assert "gross_realized" not in summary
    assert "二次扣减" in summary["commission_note"]
    # 明细与汇总同源（都读 executions），不会出现两个进度
    assert summary["executions"] == len(rows)


def test_history_summary_separates_three_counts(db):
    cfg, d = db
    _seed_dram_trade(d)
    summary = webdata.history_summary(cfg, d, webdata.history_rows(cfg, d), now=NOW)
    # transactions 行数、execution 数、decision 数是三个概念，标签不得混用
    assert "rows" in summary and "executions" in summary and "decisions" in summary


# ---------------- P0-4：标题随资金流完整性变化 ----------------

def _seed_curve(d):
    for dt, v in (("2026-07-14", "47069.92"), ("2026-07-15", "43500.49"),
                  ("2026-07-16", "43500.49")):
        d.upsert_daily_equity("A", dt, net_liquidation=Decimal(v),
                              as_of=f"{dt}T20:00:00+00:00", source="S", quality="READY")
    d.insert_balances_snapshot(NOW.isoformat(), {
        "equity": Decimal("43500.49"), "cash": Decimal("0"), "margin_balance": Decimal("0"),
        "buying_power": Decimal("0"), "account": "A"})


def test_curve_title_is_change_when_flows_incomplete(db):
    cfg, d = db
    _seed_curve(d)
    cv = webdata.equity_curve_data(cfg, d, "ALL", NOW)
    assert cv["coverage_ok"] is False
    assert cv["cum_title"] == "净资产变动"      # 不得叫「累计盈亏」
    assert cv["show_return"] is False           # 不显示收益率


def test_curve_title_is_pnl_when_flows_complete(db):
    cfg, d = db
    _seed_curve(d)
    d.set_cash_flow_coverage(account="A", start_date="2026-07-01",
                             end_date="2026-07-31", status="COMPLETE")
    cv = webdata.equity_curve_data(cfg, d, "ALL", NOW)
    assert cv["coverage_ok"] is True
    assert cv["cum_title"] == "累计盈亏"
    assert cv["show_return"] is True


# ---------------- P1-9：风险预算措辞不得是交易指令 ----------------

def test_risk_budget_wording_is_not_an_order(db):
    cfg, d = db
    ts = NOW.isoformat()
    d.insert_balances_snapshot(ts, {
        "equity": Decimal("43500"), "cash": Decimal("0"), "margin_balance": Decimal("0"),
        "buying_power": Decimal("0"), "account": "A"}, sync_run_id="b1")
    d.insert_positions_snapshot(ts, [{
        "symbol": "KMEM", "qty": Decimal("1786"), "avg_cost": Decimal("18.955"),
        "market_value": Decimal("33934"), "upl": Decimal("80"),
        "price": Decimal("19.14"), "price_source": "CLOSE",
        "price_quality": "DELAYED_FROZEN", "market_data_type": 3, "quote_as_of": None,
    }], sync_run_id="b1")
    d.upsert_stop_level("2026-07-15", {
        "symbol": "KMEM", "side": "LONG", "close": Decimal("19.14"), "atr": Decimal("1.5"),
        "swing": Decimal("18"), "stop_price": Decimal("17.82"),
        "risk_amount": Decimal("2355.73"), "risk_pct": Decimal("0.0542"),
        "exceeds_2pct": True, "suggested_trim": 1127, "reached_1r": False,
        "trailing_stop": Decimal("18"), "take_profit": "-",
        "atr_period": 9, "bars_used": 10, "data_sufficient": False, "data_note": "n",
    })
    row = webdata.overview_data(cfg, d, NOW)["positions"][0]
    # 旧的合并提示行已废弃，改为「建议」列展示，数量取自后端（target/trim），前端不自算。
    assert row["risk_budget_note"] is None
    adv = row["advice"]
    assert adv["label"] == "建议减仓" and adv["level"] == "warn"
    # 参考上限 target 与建议减持 trim 都来自后端计算（前端不自算）
    assert row["target"] == 659 and row["trim"] == 1127
    assert "减" in adv["detail"] and "股" in adv["detail"]  # 「降至 N 股以内（约减 M 股）」


# ---------------- 账户 PnL 与持仓必须同批次 ----------------
# 回归：曾把账户级 PnL 放在独立 job 里取，与持仓快照差几十秒。盘后价格持续变动
# （dailyPnL 从 -655 一路走到 -715），于是「逐持仓合计 vs 账户 DailyPnL」差 0.16，
# 被对账判成 MISMATCH —— 数据没错，错的是取数时点。

class _BundleClient:
    """一次调用同时返回持仓、余额、账户 PnL（模拟 fetch_pnl_bundle 的同批语义）。"""
    def __init__(self):
        self.pnl_calls = 0

    def resolve_account_hash(self, n):
        return "U1"

    def fetch_positions_and_balances(self, h):
        self.pnl_calls += 1
        positions = [{
            "symbol": "KMEM", "qty": Decimal("100"), "avg_cost": Decimal("19"),
            "market_value": Decimal("1914"), "upl": Decimal("14"),
            "price": Decimal("19.14"), "price_source": "CLOSE",
            "price_quality": "DELAYED_FROZEN", "market_data_type": 3,
            "quote_as_of": None, "conid": 897104052,
            "day_pnl": Decimal("-678.68"), "day_pnl_source": "IBKR_reqPnLSingle",
        }]
        balances = {
            "equity": Decimal("43500.49"), "cash": Decimal("321.03"),
            "margin_balance": Decimal("321.03"), "buying_power": Decimal("2774.53"),
            "_ready": True, "account": "U1", "source": "IBKR_accountSummary",
            "_account_pnl": {"daily_pnl": Decimal("-678.68"),
                             "realized_pnl": Decimal("-1.260146"),
                             "unrealized_pnl": Decimal("14")},
        }
        return positions, balances


def test_account_pnl_shares_timestamp_and_batch_with_positions(make_config):
    from app.notify import Notifier
    from app.sync import SyncService
    cfg = make_config(ib_account="U1")
    d = Database(cfg.db_path)
    d.init_schema()
    svc = SyncService(cfg, d, _BundleClient(), Notifier([], d))
    assert svc.sync_positions_and_balances() is True

    pos = d.latest_positions()[0]
    p = d.latest_pnl("U1")
    assert p["as_of"] == pos["ts"]                      # 同一时点
    assert p["sync_run_id"] == pos["sync_run_id"]       # 同一批次


def test_day_pnl_check_matches_when_same_batch(make_config):
    """同批取数时，逐持仓合计与账户 DailyPnL 必须核对通过。"""
    from app.notify import Notifier
    from app.sync import SyncService
    cfg = make_config(ib_account="U1")
    d = Database(cfg.db_path)
    d.init_schema()
    SyncService(cfg, d, _BundleClient(), Notifier([], d)).sync_positions_and_balances()
    data = webdata.overview_data(cfg, d, NOW)
    assert data["day_pnl_check"].status == "MATCHED"
    assert data["day_pnl_check"].difference == Decimal("0")


# ---------------- P0-2：风险数字必须跟当前持仓走 ----------------
# 线上实测：stop_levels 每天早上算一次，用的是当时的持仓量。DRAM 今早 3.9 股、
# 盘中加到 574.9 股，风险占比却仍显示按 3.9 股算的 0.04%。

def test_live_risk_uses_current_qty_not_snapshot():
    """同一止损位，仓位翻 100 倍，风险敞口必须跟着翻。"""
    r_small = webdata.live_risk(price=Decimal("53.56"), stop=Decimal("53.37"),
                                qty=Decimal("3.9"), side="LONG", equity=Decimal("40000"))
    r_big = webdata.live_risk(price=Decimal("53.56"), stop=Decimal("53.37"),
                              qty=Decimal("574.9"), side="LONG", equity=Decimal("40000"))
    assert r_big["risk_amount"] > r_small["risk_amount"] * 100
    assert r_small["risk_pct"] < Decimal("0.0001")     # 3.9 股几乎无风险
    assert r_big["risk_pct"] > Decimal("0.002")


def test_live_risk_computes_trim_from_current_qty():
    # 每股风险 1.0，2% 预算 = 800 元 -> 上限 800 股，当前 1000 股 -> 超 200 股
    r = webdata.live_risk(price=Decimal("50"), stop=Decimal("49"), qty=Decimal("1000"),
                          side="LONG", equity=Decimal("40000"))
    assert r["exceeds"] is True and r["trim"] == 200


def test_live_risk_short_side():
    r = webdata.live_risk(price=Decimal("49"), stop=Decimal("50"), qty=Decimal("-100"),
                          side="SHORT", equity=Decimal("40000"))
    assert r["risk_amount"] == Decimal("100")          # (50-49)*100，用绝对数量
    assert r["distance"] > 0


def test_live_risk_zero_qty_is_blank():
    r = webdata.live_risk(price=Decimal("50"), stop=Decimal("49"), qty=Decimal("0"),
                          side="LONG", equity=Decimal("40000"))
    assert r["risk_amount"] is None and r["exceeds"] is False


def test_overview_risk_reflects_intraday_position_change(db):
    """端到端：止损位是早上算的（按 3.9 股），持仓已加到 574.9 股。"""
    cfg, d = db
    ts = NOW.isoformat()
    d.insert_balances_snapshot(ts, {
        "equity": Decimal("40000"), "cash": Decimal("0"), "margin_balance": Decimal("0"),
        "buying_power": Decimal("0"), "account": "A"}, sync_run_id="b1")
    d.insert_positions_snapshot(ts, [{
        "symbol": "DRAM", "qty": Decimal("574.9"), "avg_cost": Decimal("53.34"),
        "market_value": Decimal("30793"), "upl": Decimal("0"),
        "price": Decimal("53.56"), "price_source": "LAST", "price_quality": "DELAYED",
        "quote_as_of": ts}], sync_run_id="b1")
    # 早上按 3.9 股算的快照：risk_amount 只有 0.74
    d.upsert_stop_level("2026-07-16", {
        "symbol": "DRAM", "side": "LONG", "close": Decimal("53.56"),
        "atr": Decimal("2.0"), "swing": Decimal("52"), "stop_price": Decimal("53.37"),
        "risk_amount": Decimal("0.74"), "risk_pct": Decimal("0.0000185"),
        "exceeds_2pct": False, "suggested_trim": 0, "reached_1r": False,
        "trailing_stop": Decimal("52"), "take_profit": "-", "atr_period": 14,
        "bars_used": 60, "data_sufficient": True, "data_note": None})

    row = webdata.overview_data(cfg, d, NOW)["positions"][0]
    # (53.56-53.37)*574.9 = 109.23，不是快照里的 0.74
    assert row["risk_amount"] == (Decimal("53.56") - Decimal("53.37")) * Decimal("574.9")
    assert row["risk_amount"] != Decimal("0.74")
    assert row["risk_pct"] > Decimal("0.0025")


def test_overview_hides_stop_levels_for_closed_positions(db):
    """已清仓的标的不该还在止损/风险里（KMEM 清仓后仍留在 stop_levels）。"""
    cfg, d = db
    ts = NOW.isoformat()
    d.insert_balances_snapshot(ts, {
        "equity": Decimal("40000"), "cash": Decimal("0"), "margin_balance": Decimal("0"),
        "buying_power": Decimal("0"), "account": "A"}, sync_run_id="b1")
    d.insert_positions_snapshot(ts, [{
        "symbol": "DRAM", "qty": Decimal("100"), "avg_cost": Decimal("53"),
        "market_value": Decimal("5356"), "upl": Decimal("0"),
        "price": Decimal("53.56"), "price_source": "LAST", "price_quality": "DELAYED",
        "quote_as_of": ts}], sync_run_id="b1")
    for sym in ("DRAM", "KMEM"):        # KMEM 已清仓，但止损表里还有
        d.upsert_stop_level("2026-07-16", {
            "symbol": sym, "side": "LONG", "close": Decimal("50"), "atr": Decimal("1"),
            "swing": Decimal("48"), "stop_price": Decimal("49"),
            "risk_amount": Decimal("2355"), "risk_pct": Decimal("0.054"),
            "exceeds_2pct": True, "suggested_trim": 1127, "reached_1r": False,
            "trailing_stop": Decimal("48"), "take_profit": "-", "atr_period": 14,
            "bars_used": 60, "data_sufficient": True, "data_note": None})

    data = webdata.overview_data(cfg, d, NOW)
    assert [p["symbol"] for p in data["positions"]] == ["DRAM"]
    # 风险摘要不得把已清仓标的的旧风险算进来
    assert data["risk"]["worst_symbol"] != "KMEM"


def test_live_risk_breached_stop_is_not_negative_risk(db):
    """价格已跌破止损：不显示负风险额。

    -2.87% 这种数字看起来很安全，实际是最危险的状态（风险已经兑现）。
    线上实测 DRAM 现价 51.40 < 止损 53.37。
    """
    r = webdata.live_risk(price=Decimal("51.40"), stop=Decimal("53.37"),
                          qty=Decimal("574.9"), side="LONG", equity=Decimal("40000"))
    assert r["breached"] is True
    assert r["risk_amount"] is None and r["risk_pct"] is None   # 不给负数
    assert r["exceeds"] is False and r["trim"] == 0
    assert r["beyond_stop"] == (Decimal("53.37") - Decimal("51.40")) * Decimal("574.9")


def test_live_risk_breached_short_side():
    r = webdata.live_risk(price=Decimal("55"), stop=Decimal("53"), qty=Decimal("-100"),
                          side="SHORT", equity=Decimal("40000"))
    assert r["breached"] is True and r["risk_amount"] is None


def test_live_risk_not_breached_gives_number():
    r = webdata.live_risk(price=Decimal("15.32"), stop=Decimal("13.54"),
                          qty=Decimal("400"), side="LONG", equity=Decimal("40000"))
    assert r["breached"] is False and r["risk_amount"] > 0
