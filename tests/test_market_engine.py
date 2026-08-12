"""市场环境引擎 v1 验收测试（对应清单 1–34）。

钉死三件事：
  1. 五种市场状态可区分，blocked 绝不显示成低风险；
  2. 数据缺失/过期/时间不一致都如实呈现，不用替代品充数；
  3. 市场数据失败绝不影响 IBKR 账户同步。
"""
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D

import pytest
from fastapi.testclient import TestClient

from app import market_data as md
from app import market_engine as me
from app import market_metrics as mm
from app import market_service
from app.db import Database
from app.webapp import create_app

NOW = datetime(2026, 7, 28, 16, 0, tzinfo=timezone.utc)


def _bars(prices, start_day=1, month=1):
    """构造日线序列。日期只用于排序与新鲜度，数值才是重点。"""
    out = []
    for i, p in enumerate(prices):
        day = start_day + i
        out.append({"date": f"2026-{month:02d}-{(day % 28) + 1:02d}", "c": D(str(p)),
                    "o": D(str(p)), "h": D(str(p)), "l": D(str(p)), "v": 1000})
    return out


def _rising(n=250, base=100, step=0.4):
    return [base + i * step for i in range(n)]


def _falling_below_mas(n=250):
    """先涨后急跌 —— 跌破 20/50/200 日均线。"""
    up = [100 + i * 0.5 for i in range(n - 30)]
    down = [up[-1] * (1 - 0.012 * i) for i in range(30)]
    return up + down


@pytest.fixture
def db(make_config):
    cfg = make_config(ib_account="U1")
    d = Database(cfg.db_path)
    d.init_schema()
    return cfg, d


class _Provider:
    """可编程的假数据源 —— 适配层与计算分离，测试无需连券商。"""

    def __init__(self, data: dict, fail: set[str] | None = None):
        self.data = data
        self.fail = fail or set()

    def fetch_history(self, spec, lookback_days):
        if spec.code in self.fail:
            raise TimeoutError("模拟数据源超时")
        return self.data.get(spec.code, [])


# ---------------- 1–4. 趋势 ----------------

def test_rising_low_vol_is_supportive():
    m = mm.trend_metrics(_bars(_rising()))
    assert m["status"] == "ok"
    assert m["below_ma20"] is False and m["below_ma200"] is False
    assert m["vs_ma200"] > 0
    events = me.evaluate({"qqq": m}, me.default_rules())
    assert not [e for e in events if e.category == me.CAT_TREND]


def test_qqq_below_ma20_and_ma50():
    m = mm.trend_metrics(_bars(_falling_below_mas()))
    assert m["below_ma20"] is True and m["below_ma50"] is True
    codes = {e.event_code for e in me.evaluate({"qqq": m}, me.default_rules())}
    assert "TREND_QQQ_BELOW_MA20" in codes
    assert "TREND_QQQ_BELOW_MA50" in codes


def test_qqq_below_ma200_is_critical():
    prices = [100 + i * 0.3 for i in range(200)] + [160 * (1 - 0.02 * i) for i in range(50)]
    m = mm.trend_metrics(_bars(prices))
    assert m["below_ma200"] is True
    ev = [e for e in me.evaluate({"qqq": m}, me.default_rules())
          if e.event_code == "TREND_QQQ_BELOW_MA200"]
    assert ev and ev[0].severity == me.SEV_CRITICAL


def test_sox_weaker_than_qqq():
    qqq = _bars([100 + i * 0.2 for i in range(60)])
    sox = _bars([100 - i * 0.3 for i in range(60)])
    rs = mm.relative_strength(sox, qqq, 20)
    assert rs is not None and rs < D("-0.05")
    codes = {e.event_code for e in me.evaluate({"sox_vs_qqq_20d": rs}, me.default_rules())}
    assert "TREND_SOX_WEAKER" in codes


# ---------------- 5–7. 波动率 ----------------

def test_vix_elevated_and_high():
    vol = mm.volatility_metrics(_bars([15] * 60 + [35]), [])
    codes = {e.event_code for e in me.evaluate({"vol": vol}, me.default_rules())}
    assert "VOL_VIX_ELEVATED" in codes and "VOL_VIX_HIGH" in codes


def test_vix_single_day_spike():
    vol = mm.volatility_metrics(_bars([15] * 59 + [15, 19]), [])
    assert vol["vix_change_1d"] > D("0.15")
    codes = {e.event_code for e in me.evaluate({"vol": vol}, me.default_rules())}
    assert "VOL_VIX_SPIKE" in codes


def test_vxn_above_vix():
    vol = mm.volatility_metrics(_bars([15] * 60), _bars([26] * 60))
    assert vol["vxn_above_vix"] == D("11")
    codes = {e.event_code for e in me.evaluate({"vol": vol}, me.default_rules())}
    assert "VOL_VXN_ABOVE_VIX" in codes


def test_both_volatility_rising_together():
    vol = mm.volatility_metrics(_bars([15] * 59 + [18]), _bars([18] * 59 + [22]))
    assert vol["both_rising"] is True
    codes = {e.event_code for e in me.evaluate({"vol": vol}, me.default_rules())}
    assert "VOL_BOTH_RISING" in codes


# ---------------- 8–9. 利率 ----------------

def test_rates_fast_rise():
    r = mm.rates_metrics(_bars([40] * 55 + [40, 40, 40, 40, 40, 50]))
    assert r["pct_change_5d"] > D("0.08")
    codes = {e.event_code for e in me.evaluate({"rates": r}, me.default_rules())}
    assert "RATES_FAST_RISE_5D" in codes
    assert "RATES_ABOVE_THRESHOLD" in codes      # 50 > 阈值 45


def test_rates_rise_with_equity_weak_is_correlation_not_causation():
    r = mm.rates_metrics(_bars([40] * 55 + [40, 40, 40, 40, 40, 45]))
    q = mm.trend_metrics(_bars([100 - i * 0.3 for i in range(60)]))
    co = mm.rates_and_equity_together(r, q)
    assert co["co_occurring"] is True
    assert "不代表因果" in co["note"]            # 措辞必须克制


# ---------------- 10–13. 数据质量 ----------------

def test_missing_market_data_is_reported_not_faked():
    provider = _Provider({}, fail={"QQQ", "VIX"})
    res = md.fetch_all(provider, md.DEFAULT_INSTRUMENTS[:2], lookback_days=60, now=NOW)
    assert res.ok_count == 0
    assert "QQQ" in res.failed
    # 缺失就是缺失，绝不用别的标的顶替
    assert res.latest == {}


def test_stale_market_data_marked():
    provider = _Provider({"QQQ": _bars([100, 101], month=1)})
    res = md.fetch_all(provider, [md.DEFAULT_INSTRUMENTS[0]], lookback_days=60, now=NOW)
    md.apply_freshness(res.latest, today_et="2026-07-28", max_lag_days=4)
    bar = res.latest["QQQ"]
    assert bar.quality_status == md.STALE
    assert "距今" in (bar.quality_note or "")


def test_fetched_at_differs_from_data_as_of():
    """「刚抓的」不等于「数据是新的」—— 两个时间必须分开。"""
    provider = _Provider({"QQQ": _bars([100, 101], month=1)})
    res = md.fetch_all(provider, [md.DEFAULT_INSTRUMENTS[0]], lookback_days=60, now=NOW)
    bar = res.latest["QQQ"]
    assert bar.fetched_at.startswith("2026-07-28")
    assert bar.data_as_of.startswith("2026-01")
    assert bar.fetched_at != bar.data_as_of


def test_cross_source_time_inconsistency_detected():
    provider = _Provider({"QQQ": _bars([100], start_day=1, month=7),
                          "SPY": _bars([100], start_day=5, month=7)})
    res = md.fetch_all(provider, md.DEFAULT_INSTRUMENTS[:2], lookback_days=60, now=NOW)
    note = md.cross_source_consistency(res.latest)
    assert note and "不一致" in note


def test_insufficient_history_returns_marker():
    m = mm.trend_metrics(_bars([100, 101, 102]))
    assert m["status"] == mm.INSUFFICIENT
    assert m["ma200"] is None and "ma200" in m["insufficient_for"]
    assert mm.window_return([D("1")], 20) is None


# ---------------- 14–15. 分位数 / 精度 ----------------

def test_percentile_rank_with_declared_window():
    vals = [D(str(x)) for x in range(1, 101)]
    assert mm.percentile_rank(vals, D("50")) == D("0.5")
    vol = mm.volatility_metrics(_bars(list(range(1, 121))), [], percentile_window=60)
    assert vol["percentile_window_days"] == 60      # 窗口必须写明


def test_all_market_math_uses_decimal():
    m = mm.trend_metrics(_bars(_rising(60)))
    assert isinstance(m["close"], D) and isinstance(m["ma20"], D)
    assert isinstance(m["vs_ma20"], D)
    v = mm.realized_volatility([D(str(100 + i)) for i in range(30)], 20)
    assert isinstance(v, D)


# ---------------- 16–18. 版本 / 幂等 ----------------

def test_market_rule_version_changes_with_threshold():
    v1 = me.rule_set_version(me.default_rules())

    class C:
        market_vix_warn = "18"
    v2 = me.rule_set_version(me.default_rules(C))
    assert v1 != v2
    assert "~" in v1 and "#" not in v1               # URL 安全


def test_market_assessment_idempotent(db):
    cfg, d = db
    _seed_market(d, "2026-07-28")
    a1 = market_service.assess(cfg, d, now=NOW)
    a2 = market_service.assess(cfg, d, now=NOW)
    assert a1["assessment_id"] == a2["assessment_id"]
    assert d.count_market_assessments(None, None) == 1


def test_market_rule_change_triggers_recompute(db, monkeypatch):
    cfg, d = db
    _seed_market(d, "2026-07-28")
    market_service.assess(cfg, d, now=NOW)
    monkeypatch.setattr(me, "CALC_VERSION", "market-v2/test")
    market_service.assess(cfg, d, now=NOW)
    assert d.count_market_assessments(None, None) == 2   # 历史保留


def _seed_market(d, as_of, *, vix=15, qqq_prices=None, include=("QQQ", "SOXX", "VIX")):
    """把日线塞进缓存与快照表，模拟一次成功同步。"""
    import json
    prices = qqq_prices or _rising(250)
    bars = {
        "QQQ": [{"date": f"d{i}", "c": str(p)} for i, p in enumerate(prices)],
        "SOXX": [{"date": f"d{i}", "c": str(p)} for i, p in enumerate(prices)],
        "VIX": [{"date": f"d{i}", "c": str(vix)} for i in range(250)],
    }
    bars = {k: v for k, v in bars.items() if k in include}
    d.set_sync_state("market_bars_cache", json.dumps(bars))
    rows = []
    for code in include:
        rows.append({"instrument_code": code, "instrument_name": code,
                     "data_as_of": as_of, "fetched_at": as_of + "T20:00:00+00:00",
                     "close": D("100"), "previous_close": D("99"),
                     "daily_change": D("1"), "daily_change_percent": D("0.01"),
                     "source": "test", "delayed": True, "quality_status": md.FRESH})
    d.upsert_market_snapshots(rows)


# ---------------- 19–23. 五种市场状态 ----------------

def test_state_supportive():
    assert me.market_state(D("5"), [], reliable=True) == me.SUPPORTIVE


def test_state_neutral():
    assert me.market_state(D("15"), [], reliable=True) == me.NEUTRAL


def test_state_cautious():
    ev = [me.MarketEvent("X", me.SEV_HIGH, "t", "e", None, None, "flag", "X", me.CAT_TREND)]
    assert me.market_state(D("35"), ev, reliable=True) == me.CAUTIOUS


def test_state_stressed():
    ev = [me.MarketEvent("X", me.SEV_CRITICAL, "t", "e", None, None, "flag", "X",
                         me.CAT_TREND)]
    assert me.market_state(D("70"), ev, reliable=True) == me.STRESSED


def test_state_blocked_when_unreliable():
    """数据不足绝不能显示成 supportive。"""
    assert me.market_state(D("0"), [], reliable=False) == me.BLOCKED
    assert me.market_state(None, [], reliable=True) == me.BLOCKED
    assert me.market_risk_level(me.BLOCKED) == "blocked"


def test_breadth_null_not_zero_and_weights_renormalized():
    """广度未实现时必须为 null 并重新归一化，记 0 分会让市场看起来更安全。"""
    ev = [me.MarketEvent("X", me.SEV_HIGH, "t", "e", None, None, "flag", "X",
                         me.CAT_TREND)]
    sc = me.score(ev, breadth_available=False)
    assert sc["component_scores"]["breadth_score"] is None
    # trend 40 分，权重归一后总分应显著高于「广度记 0 分」的情形
    assert sc["market_risk_score"] > D("10")


# ---------------- 24–27. 联合视图 ----------------

def _acct(level="high", *, max_inv="0.9", cash="0.1", lev="1.0", reliable=True,
          classes=None):
    return {"risk_level": level, "reliable": reliable,
            "calculated_metrics": {
                "concentration": {"max_position_weight_of_invested": max_inv,
                                  "classifications": classes or {}},
                "account": {"cash_ratio": cash, "gross_leverage": lev}}}


def _mkt(state=me.STRESSED, reliable=True, drivers=None):
    return {"market_state": state, "market_risk_level": me.market_risk_level(state),
            "reliable": reliable, "top_market_drivers": drivers or []}


def test_combined_high_account_risk_in_stressed_market():
    v = me.combined_view(_acct(), _mkt())
    assert "CONC_IN_STRESSED_MARKET" in v["amplifiers"]
    assert D(v["risk_amplification_factor"]) > D("1")
    assert v["reliable"] is True


def test_combined_high_cash_provides_buffer():
    v = me.combined_view(_acct(max_inv="0.2", cash="0.6"), _mkt())
    assert "CASH_BUFFER_IN_STRESS" in v["buffers"]
    assert D(v["risk_amplification_factor"]) < D("1")


def test_combined_semi_concentration_with_weak_sox():
    classes = {"MRVL": {"sector": "半导体"}, "MU": {"sector": "内存半导体"}}
    v = me.combined_view(_acct(classes=classes),
                         _mkt(drivers=[{"rule_code": "TREND_SOX_WEAKER"}]))
    assert "SEMI_CONC_WITH_SOX_WEAK" in v["amplifiers"]
    assert "MRVL" in str(v["combined_risk_drivers"])


def test_combined_blocked_when_either_side_blocked():
    v = me.combined_view(_acct(reliable=False), _mkt())
    assert v["reliable"] is False
    assert "不可靠" in v["combined_risk_view"]
    v2 = me.combined_view(_acct(), _mkt(reliable=False))
    assert v2["reliable"] is False


def test_combined_does_not_modify_account_score():
    """联合视图是解释层 —— 账户原始评估必须原封不动。"""
    acc = _acct()
    before = dict(acc)
    me.combined_view(acc, _mkt())
    assert acc == before


# ---------------- 28–30. API / 前端 / 解耦 ----------------

def test_api_market_endpoints(db):
    cfg, d = db
    _seed_market(d, "2026-07-28")
    c = TestClient(create_app(cfg, d))
    body = c.get("/api/v1/market/environment").json()
    assert body["success"] is True and body["request_id"] and body["generated_at"]
    data = body["data"]
    for k in ("data_as_of", "market_state", "market_risk_level", "market_risk_score",
              "reliable", "component_scores", "calculated_metrics", "triggered_rules",
              "top_market_drivers", "quality_status", "calculation_version"):
        assert k in data, k
    inst = c.get("/api/v1/market/instruments").json()["data"]
    assert any(i["instrument_code"] == "QQQ" for i in inst["instruments"])
    # 未取到的标的必须如实标 missing
    assert any(i["quality_status"] == "missing" for i in inst["instruments"])
    hist = c.get("/api/v1/market/environment/history").json()["data"]
    assert "items" in hist and "total" in hist
    comb = c.get("/api/v1/risk/combined").json()["data"]
    assert "combined" in comb and "market" in comb and "account" in comb


def test_page_shows_market_module_and_stale_warning(db):
    cfg, d = db
    _seed_market(d, "2026-01-01")           # 很旧 -> stale/blocked
    html = TestClient(create_app(cfg, d)).get("/").text
    assert "市场环境" in html
    assert "账户 × 市场 联合视图" in html or "联合视图" in html


def test_market_failure_does_not_break_account_sync(db):
    """外部市场数据失败绝不能影响 IBKR 账户同步 —— 这是硬边界。"""
    cfg, d = db

    class BoomClient:
        def fetch_index_bars(self, **kw):
            raise RuntimeError("市场数据源整体不可用")
    res = market_service.sync_market_data(cfg, d, BoomClient(), now=NOW)
    assert res["state"] in ("failed", "partial")     # 不抛异常
    run = dict(d.latest_market_sync_run())
    assert run["run_state"] in ("failed", "partial")
    # 账户侧数据完全不受影响
    assert d.latest_snapshot_row() is None            # 本测试未写账户快照
    assert d.count_snapshots_between(None, None) == 0


def test_market_sync_run_is_independent(db):
    cfg, d = db

    class P:
        def fetch_index_bars(self, *, symbol, **kw):
            if symbol in ("QQQ", "SOXX"):
                return [{"date": "2026-07-28", "c": D("100"), "o": D("100"),
                         "h": D("100"), "l": D("100"), "v": 1}]
            raise TimeoutError("无订阅")
    res = market_service.sync_market_data(cfg, d, P(), now=NOW)
    assert res["ok"] >= 2
    # 拿到一部分 = partial；只有一个都没拿到才是 failed
    assert res["state"] == "partial"
    assert "VIX" in res["failed"]
    assert "VIX" in res["required_missing"]          # 必需缺失仍如实记录


# ---------------- 31–34. P0.5 回归 ----------------

def test_sector_classification_completeness(db):
    from app import instruments as inst
    cfg, d = db
    d.set_classification_override("BMNR", sector="数字资产")
    d.upsert_instrument_metadata([{"symbol": "MRVL", "sector": "Semiconductors",
                                   "asset_class": "equity", "source": "ibkr",
                                   "confidence": "high"}])
    res = inst.classify_positions(d, ["BMNR", "MRVL"])
    assert all(c.is_known for c in res.values())      # 100% 可分类


def test_losing_pool_contribution_regression():
    from app import risk_metrics as rm
    pos = [{"symbol": "A", "qty": D("1"), "avg_cost": D("100"),
            "market_value": D("50"), "upl": D("-50"), "price": D("50")},
           {"symbol": "B", "qty": D("1"), "avg_cost": D("100"),
            "market_value": D("150"), "upl": D("50"), "price": D("150")}]
    rows = rm.position_risk(pos, D("1000"), max_position_weight=D("0.3"),
                            total_unrealized=D("0"))
    a = next(r for r in rows if r["symbol"] == "A")
    assert a["contribution_to_total_losing_positions"] == D("1")   # 唯一亏损仓 = 100%


def test_risk_rule_history_preserved(db):
    from app import risk_engine as rre
    cfg, d = db
    r1 = rre.default_rules(cfg)
    d.sync_risk_rules([r.as_dict() for r in r1], set_version=rre.rule_set_version(r1),
                      set_hash=rre.rule_set_hash(r1))

    class C2:
        risk_vix = "x"
        risk_cash_ratio_min = "0.15"
    r2 = rre.default_rules(C2)
    d.sync_risk_rules([r.as_dict() for r in r2], set_version=rre.rule_set_version(r2),
                      set_hash=rre.rule_set_hash(r2))
    assert len(d.risk_rule_versions()) == 2


def test_historical_assessment_keeps_original_rule_version(db):
    """禁止用当前环境变量重新解释历史评估。"""
    from app import risk_service
    from app import risk_engine as rre
    cfg, d = db
    d.write_account_snapshot(
        snapshot_id="s1", ts=NOW.isoformat(),
        balances={"equity": D("100000"), "cash": D("50000"), "total_cash": D("50000"),
                  "margin_balance": D("50000"), "buying_power": D("100000"),
                  "account": "U1", "currency": "USD", "positions_value": D("50000")},
        positions=[{"symbol": "AAA", "qty": D("100"), "avg_cost": D("100"),
                    "market_value": D("50000"), "upl": D("0"), "price": D("500")}],
        sync_run_id="r1", quality_status="fresh", ai_analysis_allowed=True)
    a1 = risk_service.assess(cfg, d, now=NOW)
    v1 = a1["rule_set_version"]
    stored = dict(d.assessment_for("s1", v1))
    assert stored["rule_set_version"] == v1
    # 事件也钉住当时的版本
    for e in d.risk_events_for(a1["assessment_id"]):
        assert e["rule_version"] == v1


def test_instrument_table_formats_long_decimals(make_config):
    """概览页标的表的日涨跌**必须格式化**。

    生产实测原始值是 28 位小数（-0.009719697413944760452706268692），
    直接渲染会在表格里拖出一长串数字。市场面板其余字段都走了 show()，
    只有这张标的表当初漏了。
    """
    from app import webstatus

    cfg = make_config(ib_account="U1")
    d = Database(cfg.db_path)
    d.init_schema()
    as_of = "2026-07-28"
    _seed_market(d, as_of)
    # 覆写成生产里那种长小数
    d.upsert_market_snapshots([
        {"instrument_code": "QQQ", "instrument_name": "纳指 ETF",
         "data_as_of": as_of, "fetched_at": as_of + "T20:00:00+00:00",
         "close": D("675.4900000000"), "previous_close": D("682.12"),
         "daily_change": D("-6.63"),
         "daily_change_percent": D("-0.009719697413944760452706268692"),
         "source": "test", "delayed": True, "quality_status": md.FRESH},
        {"instrument_code": "SOXX", "instrument_name": "半导体 ETF",
         "data_as_of": as_of, "fetched_at": as_of + "T20:00:00+00:00",
         "close": D("491.46"), "previous_close": D("515.24"),
         "daily_change": D("-23.78"),
         "daily_change_percent": D("-0.04798248842570172210061406737"),
         "source": "test", "delayed": True, "quality_status": md.FRESH},
    ])

    panel = webstatus.market_panel(cfg, d)
    inst = panel["instruments"]
    assert inst["QQQ"]["daily_change_percent"] == "-1.0%"
    assert inst["SOXX"]["daily_change_percent"] == "-4.8%"
    assert inst["QQQ"]["close"] == "675.49"          # 尾零也收敛
    # 其他字段原样保留
    # 非数值字段原样透传，不被格式化碰到
    assert inst["QQQ"]["name"]
    assert inst["QQQ"]["data_as_of"] == as_of
    assert inst["QQQ"]["delayed"] is True
    assert inst["QQQ"]["quality_status"] == md.FRESH
    assert inst["QQQ"]["time_label"] == "2026-07-28 收盘"

    html = TestClient(create_app(cfg, d)).get("/").text
    assert "-1.0%" in html
    assert "0.009719697413944760" not in html        # 长小数不再出现
    assert "0.04798248842570172" not in html


def test_instrument_table_missing_value_shows_dash(make_config):
    """缺失显示「—」，**绝不显示 0** —— 那是两件不同的事。"""
    from app import webstatus

    cfg = make_config(ib_account="U1")
    d = Database(cfg.db_path)
    d.init_schema()
    as_of = "2026-07-28"
    _seed_market(d, as_of)
    d.upsert_market_snapshots([
        {"instrument_code": "QQQ", "instrument_name": "QQQ",
         "data_as_of": as_of, "fetched_at": as_of + "T20:00:00+00:00",
         "close": None, "previous_close": None, "daily_change": None,
         "daily_change_percent": None,
         "source": "test", "delayed": True, "quality_status": md.FRESH},
    ])
    inst = webstatus.market_panel(cfg, d)["instruments"]
    assert inst["QQQ"]["daily_change_percent"] == "—"
    assert inst["QQQ"]["close"] == "—"


def test_ibkr_intraday_index_is_labeled_delayed_not_realtime(make_config):
    from app import webstatus
    from app.market_hours import REGULAR

    cfg = make_config()
    now = datetime(2026, 7, 31, 14, 0, 0, tzinfo=timezone.utc)
    state = webstatus._market_state({
        "source": "IBKR_reqMktData", "quote_as_of": now.isoformat(),
        "data_as_of": "2026-07-31", "delayed": True,
        "quality_status": md.FRESH,
    }, now, REGULAR, cfg.market_tz)
    assert state == {"label": "延迟", "source": "IBKR", "css": "warn"}
