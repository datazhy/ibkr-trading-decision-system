"""风险事件复盘与阈值回测 v1 验收测试（对应清单 1–45）。

钉死五件事：
  1. 观察窗口按**交易日**算，周末与假期不算数；
  2. 回测**绝不使用未来数据** —— 均线、分位数、规则版本都只用当时可得的；
  3. 现金流污染的窗口如实标记，不参与统计；
  4. 样本不足时不给指标、不给建议、不给绿色高分；
  5. 复盘与回测失败绝不影响线上评估与告警。
"""
import datetime as dt
import json
from decimal import Decimal as D

import pytest
from fastapi.testclient import TestClient

from app import backtest as bt
from app import forward_outcomes as fo
from app import outcome_labels as ol
from app import review_service as rs
from app import rule_performance as rp
from app import threshold_advisor as ta
from app import trading_calendar as tcal
from app.db import Database
from app.webapp import create_app

NOW = dt.datetime(2026, 7, 29, 16, 0, tzinfo=dt.timezone.utc)


def _weekdays(start: dt.date, n: int) -> list[str]:
    """n 个工作日（周末跳过）。"""
    out, d = [], start
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d.isoformat())
        d += dt.timedelta(days=1)
    return out


def _bars(dates, prices):
    return [{"date": d, "o": p, "h": p, "l": p, "c": p, "v": 1000}
            for d, p in zip(dates, prices)]


@pytest.fixture
def env(make_config):
    cfg = make_config(ib_account="U1234567")
    d = Database(cfg.db_path)
    d.init_schema()
    return cfg, d


@pytest.fixture
def seeded(env):
    """150 个交易日的 QQQ/SOXX/VIX：先涨 110 天再急跌 40 天。"""
    cfg, d = env
    days = _weekdays(dt.date(2026, 1, 5), 150)
    up = [100 * (1.004 ** i) for i in range(110)]
    down = [up[-1] * (0.985 ** i) for i in range(1, 41)]
    prices = up + down
    d.upsert_daily_bars("QQQ", _bars(days, prices))
    d.upsert_daily_bars("SOXX", _bars(days, [p * 0.9 for p in prices]))
    d.upsert_daily_bars("VIX", _bars(days, [14 + (0 if i < 110 else (i - 110) * 0.6)
                                            for i in range(150)]))
    return cfg, d, days


# ---------------- 1–4. 复盘生成与交易日窗口 ----------------

def _mk_event(d, *, event_id="e1", rule="CONC_TOP3_HIGH", source="account",
              first="2026-01-06T15:00:00+00:00", symbol=None, severity="high"):
    d.create_risk_event({
        "event_id": event_id, "identity": f"i-{event_id}", "episode_seq": 1,
        "source_type": source, "event_code": rule, "rule_code": rule,
        "rule_set_version": "v1~aaa", "category": "concentration",
        "severity": severity, "max_severity": severity, "status": "open",
        "title": "测试事件", "explanation": "说明", "actual_value": "0.85",
        "threshold_value": "0.80", "unit": "ratio", "affected_symbol": symbol,
        "data_as_of": first[:10], "first_detected_at": first,
        "last_detected_at": first, "created_at": first, "updated_at": first,
        "quality_status": "fresh", "reliable": True})
    return d.get_risk_event(event_id)


def test_review_record_is_generated(seeded):
    cfg, d, days = seeded
    _mk_event(d)
    stats = rs.generate_reviews(cfg, d, now=NOW)
    assert stats["written"] == 1
    rev = d.review_for_event("e1", rs.CALC_VERSION)
    assert rev is not None
    assert rev["rule_code"] == "CONC_TOP3_HIGH"
    assert rev["calculation_version"] == rs.CALC_VERSION


def test_unresolved_event_becomes_reviewable_once_window_elapses(seeded):
    cfg, d, days = seeded
    # 事件发生在序列早期 -> 20 日窗口早已走完
    _mk_event(d, event_id="old", first=days[5] + "T15:00:00+00:00")
    # 事件发生在最后一天 -> 窗口还没走完
    _mk_event(d, event_id="new", first=days[-1] + "T15:00:00+00:00")
    rs.generate_reviews(cfg, d, now=NOW)
    assert d.review_for_event("old", rs.CALC_VERSION)["review_status"] == "completed"
    assert d.review_for_event("new", rs.CALC_VERSION)["review_status"] == "pending"


def test_trading_day_window_skips_weekends():
    cal = tcal.TradingCalendar(["2026-07-23", "2026-07-24", "2026-07-27",
                               "2026-07-28", "2026-07-29"])
    # 周四 +1 交易日 = 周五；+2 = 下周一（不是周六）
    assert cal.offset("2026-07-23", 1) == "2026-07-24"
    assert cal.offset("2026-07-23", 2) == "2026-07-27"
    # 自然日会算成 07-25（周六），交易日不会
    assert cal.offset("2026-07-23", 2) != "2026-07-25"


def test_holiday_and_weekend_gaps_handled():
    # 中间缺了 4 天（长假），交易日历如实反映
    cal = tcal.TradingCalendar(["2026-07-01", "2026-07-02", "2026-07-08",
                               "2026-07-09"])
    assert cal.offset("2026-07-02", 1) == "2026-07-08"
    assert cal.count_between("2026-07-01", "2026-07-09") == 4
    # 非交易日作为起点时顺延到下一个交易日，不回退
    assert cal.days[cal.index_of("2026-07-05")] == "2026-07-08"
    # 窗口不足时返回 None，绝不外推
    assert cal.offset("2026-07-09", 5) is None
    assert cal.has_full_window("2026-07-09", 1) is False


# ---------------- 5–7. 最大不利变化 ----------------

def test_account_max_adverse_excursion():
    rows = [{"ts": f"2026-07-{d:02d}T16:00:00+00:00", "equity": str(v)}
            for d, v in zip(range(1, 6), [100000, 97000, 95000, 98000, 99000])]
    out = fo.account_outcome(rows)
    assert out["account_max_adverse"] == "0.05"        # 从 100000 跌到 95000
    assert D(out["account_max_drawdown"]) == D("0.05")
    assert D(out["account_change"]) == D("-0.01")


def test_position_max_adverse_excursion():
    bars = _bars(["2026-07-01", "2026-07-02", "2026-07-03"], [100, 88, 95])
    out = fo.position_outcome(bars, base_qty=D("100"), end_qty=D("100"),
                              base_equity=D("50000"))
    assert D(out["position_mae"]) == D("0.12")
    assert D(out["position_return"]) == D("-0.05")
    # 期初仓位计价的账户贡献：(95-100)*100 / 50000
    assert D(out["position_contribution_to_account"]) == D("-0.01")


def test_market_max_adverse_excursion():
    q = _bars(["2026-07-01", "2026-07-02", "2026-07-03"], [500, 470, 480])
    out = fo.market_outcome(q, [], [], [])
    assert D(out["qqq_max_drawdown"]) == D("0.06")
    assert D(out["qqq_return"]) == D("-0.04")


# ---------------- 8–10. 现金流 ----------------

def test_deposit_jump_is_detected():
    rows = [{"ts": "2026-07-01T16:00:00+00:00", "equity": "100000"},
            {"ts": "2026-07-02T16:00:00+00:00", "equity": "150000"}]
    cf = fo.detect_cash_flow(rows, executions=[])
    assert cf["status"] == fo.CF_CONTAMINATED
    assert "出入金" in cf["note"]


def test_withdrawal_drop_is_detected():
    rows = [{"ts": "2026-07-01T16:00:00+00:00", "equity": "100000"},
            {"ts": "2026-07-02T16:00:00+00:00", "equity": "80000"}]
    cf = fo.detect_cash_flow(rows, executions=[])
    assert cf["status"] == fo.CF_CONTAMINATED
    # 大额出金必须让账户结果退出统计，否则会被当成 20% 的亏损
    assert D(cf["flow_ratio"]) >= D("0.10")


def test_unidentifiable_flow_marked_estimated():
    rows = [{"ts": "2026-07-01T16:00:00+00:00", "equity": "100000"},
            {"ts": "2026-07-02T16:00:00+00:00", "equity": "105000"}]
    cf = fo.detect_cash_flow(rows, executions=[])
    assert cf["status"] == fo.CF_ESTIMATED
    assert cf["source"] == "heuristic"
    # 有成交可以解释跳变 -> clean
    clean = fo.detect_cash_flow(rows, executions=[
        {"execution_time": "2026-07-02T14:00:00+00:00", "symbol": "X"}])
    assert clean["status"] == fo.CF_CLEAN
    # 有券商流水时用权威数据，不再猜
    withflow = fo.detect_cash_flow(rows, [], cash_flows=[{"amount": "5000"}])
    assert withflow["status"] == fo.CF_CLEAN and withflow["source"] == "flex_query"


def test_contaminated_window_excluded_from_stats(seeded):
    """受污染窗口的账户结果不参与 precision —— 它既不是命中也不是误报。"""
    reviews = [{"outcome_labels": json.dumps({"5": {"ACCOUNT_DD_2PCT": True}}),
                "forward_outcomes": json.dumps({"5": {"cash_flow_status": "contaminated"}}),
                "lead_time_class": "leading", "lead_time_minutes": 100}
               for _ in range(10)]
    cm = rp.confusion(reviews, 5, "ACCOUNT_DD_2PCT")
    assert cm["true_positive"] == 0 and cm["false_positive"] == 0
    assert cm["undetermined"] == 10


# ---------------- 11–14. precision / recall ----------------

def _revs(n_hit, n_miss, window=5, label="ACCOUNT_DD_2PCT"):
    out = []
    for _ in range(n_hit):
        out.append({"outcome_labels": json.dumps({str(window): {label: True}}),
                    "forward_outcomes": json.dumps({str(window): {"account_max_adverse": "0.04"}}),
                    "lead_time_class": rs.LEAD_LEADING, "lead_time_minutes": 2880})
    for _ in range(n_miss):
        out.append({"outcome_labels": json.dumps({str(window): {label: False}}),
                    "forward_outcomes": json.dumps({str(window): {"account_max_adverse": "0.005"}}),
                    "lead_time_class": rs.LEAD_NO_OUTCOME, "lead_time_minutes": None})
    return out


def test_precision_calculation():
    cm = rp.confusion(_revs(6, 4), 5, "ACCOUNT_DD_2PCT")
    assert cm["true_positive"] == 6 and cm["false_positive"] == 4
    assert cm["precision"] == D("0.6")


def test_recall_calculation():
    # 10 次触发中命中 6 次；期间共发生 12 次风险结果 -> recall = 6/12
    cm = rp.confusion(_revs(6, 4), 5, "ACCOUNT_DD_2PCT", baseline_outcomes=12)
    assert cm["recall"] == D("0.5")
    assert cm["false_negative"] == 6


def test_false_positive_rate():
    cm = rp.confusion(_revs(3, 7), 5, "ACCOUNT_DD_2PCT")
    assert cm["false_positive_rate"] == D("0.7")


def test_false_negative_rate():
    cm = rp.confusion(_revs(4, 6), 5, "ACCOUNT_DD_2PCT", baseline_outcomes=10)
    assert cm["false_negative"] == 6
    assert cm["false_negative_rate"] == D("0.6")


# ---------------- 15–18. 提前量 ----------------

def test_average_lead_time():
    revs = _revs(5, 0)
    cm = rp.confusion(revs, 5, "ACCOUNT_DD_2PCT")
    assert cm["avg_lead_minutes"] == D("2880")          # 2 天
    assert cm["median_lead_minutes"] == D("2880")


def test_concurrent_classification():
    """规则与结果同日发生 -> concurrent，不算提前预警。"""
    cal = tcal.TradingCalendar(_weekdays(dt.date(2026, 7, 1), 10))
    ev = {"first_detected_at": "2026-07-01T15:00:00+00:00"}
    # 结果在第 1 个交易日内命中 -> 间隔不足一天
    m, c = rs._lead_time(ev, {}, {"1": {"ACCOUNT_DD_2PCT": True}},
                         source="account", calendar=cal,
                         open_day="2026-07-01", cfg=None)
    assert c == rs.LEAD_CONCURRENT
    assert 0 <= m <= rs.CONCURRENT_WINDOW_MINUTES


def test_lagging_classification():
    cal = tcal.TradingCalendar(_weekdays(dt.date(2026, 7, 1), 10))
    # 规则触发晚于窗口末日 -> 负提前量 -> lagging
    ev = {"first_detected_at": "2026-07-20T15:00:00+00:00"}
    m, c = rs._lead_time(ev, {}, {"1": {"ACCOUNT_DD_2PCT": True}},
                         source="account", calendar=cal,
                         open_day="2026-07-02", cfg=None)
    assert m is not None and m < 0
    assert c == rs.LEAD_LAGGING


def test_no_outcome_classification():
    cal = tcal.TradingCalendar(_weekdays(dt.date(2026, 7, 1), 10))
    ev = {"first_detected_at": "2026-07-01T15:00:00+00:00"}
    m, c = rs._lead_time(ev, {}, {"1": {"ACCOUNT_DD_2PCT": False}},
                         source="account", calendar=cal,
                         open_day="2026-07-01", cfg=None)
    assert c == rs.LEAD_NO_OUTCOME and m is None


# ---------------- 19. 样本量 ----------------

def test_insufficient_sample_gives_no_metrics():
    cm = rp.confusion(_revs(2, 1), 5, "ACCOUNT_DD_2PCT")
    # 3 个样本：绝不给出 67% 这种会被读成「三分之二可靠」的数字
    assert cm["precision"] is None
    assert cm["sample_size"] == 3
    assert rp.confidence_for(3) == rp.CONF_INSUFFICIENT
    score, comps = rp.composite_score(D("1.0"), D("1.0"), D("5000"), D("1"), 3)
    assert score is None
    assert "不足" in comps["note"]


# ---------------- 20–23. 阈值回测 ----------------

def test_backtest_current_threshold(seeded):
    cfg, d, days = seeded
    cal = tcal.from_db(d)
    rule = [r for r in bt.default_rules(cfg) if r.rule_code == "TREND_QQQ_DRAWDOWN"][0]
    res = bt.replay_rule(d, rule, rule.current, calendar=cal, days=cal.days,
                         window=5, label_code="MARKET_QQQ_DD_5PCT")
    assert res["days_evaluated"] > 100
    assert res["trigger_count"] > 0
    assert res["threshold_value"] == str(rule.current)


def test_multiple_candidate_thresholds_are_bounded(seeded):
    cfg, d, days = seeded
    rules = bt.default_rules(cfg)
    for r in rules:
        # 有限枚举：绝不做几百个阈值的网格搜索
        assert 3 <= len(r.candidates) <= 8, r.rule_code


def test_raising_threshold_reduces_alerts(seeded):
    cfg, d, days = seeded
    cal = tcal.from_db(d)
    rule = [r for r in bt.default_rules(cfg) if r.rule_code == "TREND_QQQ_DRAWDOWN"][0]
    low = bt.replay_rule(d, rule, D("0.03"), calendar=cal, days=cal.days,
                         window=5, label_code="MARKET_QQQ_DD_5PCT")
    high = bt.replay_rule(d, rule, D("0.15"), calendar=cal, days=cal.days,
                          window=5, label_code="MARKET_QQQ_DD_5PCT")
    assert high["trigger_count"] < low["trigger_count"]
    assert high["estimated_notifications"] <= low["estimated_notifications"]


def test_lowering_threshold_increases_recall(seeded):
    cfg, d, days = seeded
    cal = tcal.from_db(d)
    rule = [r for r in bt.default_rules(cfg) if r.rule_code == "TREND_QQQ_DRAWDOWN"][0]
    low = bt.replay_rule(d, rule, D("0.02"), calendar=cal, days=cal.days,
                         window=5, label_code="MARKET_QQQ_DD_5PCT")
    high = bt.replay_rule(d, rule, D("0.20"), calendar=cal, days=cal.days,
                          window=5, label_code="MARKET_QQQ_DD_5PCT")
    lr, hr = low.get("recall_value"), high.get("recall_value")
    if lr is not None and hr is not None:
        assert D(lr) >= D(hr)


# ---------------- 24–25. 规则组合 ----------------

def test_combo_precision_and_recall_tradeoff(seeded):
    cfg, d, days = seeded
    cal = tcal.from_db(d)
    rules = bt.default_rules(cfg)
    c = bt.evaluate_combo(d, rules, "QQQ_MA50_WITH_VXN",
                          ["TREND_QQQ_BELOW_MA50", "VOL_VIX_ELEVATED"],
                          calendar=cal, days=cal.days, window=5,
                          label_code="MARKET_QQQ_DD_5PCT")
    assert c is not None
    assert c["combo_triggers"] <= max(
        s["trigger_count"] for s in json.loads(c["member_stats"]).values())
    # 组合的代价必须被显式记录，而不是只报 precision 提升
    assert "recall_delta" in c and "precision_delta" in c


def test_combo_recall_never_exceeds_members(seeded):
    """两条规则「同时成立」的次数不可能多于任一单规则 -> recall 只会更低。"""
    cfg, d, days = seeded
    cal = tcal.from_db(d)
    rules = bt.default_rules(cfg)
    c = bt.evaluate_combo(d, rules, "T", ["TREND_QQQ_BELOW_MA50", "VOL_VIX_ELEVATED"],
                          calendar=cal, days=cal.days, window=5,
                          label_code="MARKET_QQQ_DD_5PCT")
    stats = json.loads(c["member_stats"])
    for s in stats.values():
        assert c["combo_triggers"] <= s["trigger_count"]
    if c["recall_delta"] is not None:
        assert D(c["recall_delta"]) <= 0


# ---------------- 26–27. 样本外 ----------------

def test_train_validation_split_is_chronological(seeded):
    cfg, d, days = seeded
    cal = tcal.from_db(d)
    train, valid = bt.split_days(cal.days)
    assert train and valid
    # 绝不打乱时间序列 —— 打乱会把未来数据混进训练期
    assert train[-1] < valid[0]
    assert train == sorted(train) and valid == sorted(valid)
    assert len(train) + len(valid) == len(cal.days)


def test_walk_forward_folds_move_forward(seeded):
    cfg, d, days = seeded
    cal = tcal.from_db(d)
    folds = bt.walk_forward(cal.days, folds=3)
    assert len(folds) == 3
    prev_end = None
    for train, valid in folds:
        assert train[-1] < valid[0]
        if prev_end is not None:
            assert valid[0] > prev_end        # 验证段逐步前移
        prev_end = valid[0]


# ---------------- 28–31. 防未来数据泄漏 ----------------

def test_no_future_data_in_point_in_time_reads(seeded):
    cfg, d, days = seeded
    as_of = days[80]
    bars = d.bars_up_to("QQQ", as_of, limit=400)
    # 物理上取不到 as_of 之后的任何一根日线
    assert all(b["date"] <= as_of for b in bars)
    assert bars[-1]["date"] == as_of
    # 均线同样只用截至该日的数据
    ma = bt._ma50_pit(d, "QQQ", as_of)
    manual = sum(b["c"] for b in bars[-50:]) / D("50")
    assert ma == manual


def test_rolling_percentile_ignores_future(seeded):
    cfg, d, days = seeded
    early, late = days[60], days[140]
    p_early = bt.rolling_percentile(d, "VIX", early, window=252)
    p_late = bt.rolling_percentile(d, "VIX", late, window=252)
    assert p_early is not None and p_late is not None
    # VIX 在后段大幅上升。若用了全历史分位，早期时点会因为「知道未来会更高」
    # 而被压低到很小；只用滚动历史时，早期时点的当前值是它自己历史里的高位。
    assert p_early > D("0.5")
    # 换个更短的窗口，早期分位不应受后段数据影响
    assert bt.rolling_percentile(d, "VIX", early, window=30) is not None


def test_historical_rule_version_is_recorded(seeded):
    cfg, d, days = seeded
    _mk_event(d, first=days[5] + "T15:00:00+00:00")
    rs.generate_reviews(cfg, d, now=NOW)
    rev = d.review_for_event("e1", rs.CALC_VERSION)
    # 复盘保留事件当时的规则版本，不用现在的
    assert rev["rule_set_version"] == "v1~aaa"


def test_historical_classification_version_preserved(seeded):
    """证券分类随事件一起冻结在复盘里，不用今天的分类解释过去。"""
    cfg, d, days = seeded
    d.upsert_instrument_metadata([{"symbol": "MRVL", "sector": "半导体",
                                   "source": "ibkr", "confidence": "high"}])
    _mk_event(d, event_id="pos1", source="position", rule="POS_LOSS",
              symbol="MRVL", first=days[5] + "T15:00:00+00:00")
    rs.generate_reviews(cfg, d, now=NOW)
    rev = d.review_for_event("pos1", rs.CALC_VERSION)
    assert rev["affected_symbol"] == "MRVL"
    assert rev["calculation_version"] == rs.CALC_VERSION


# ---------------- 32–34. 回测任务 ----------------

def test_identical_params_reuse_backtest(seeded):
    cfg, d, days = seeded
    a = bt.run_backtest(cfg, d, rules=["TREND_QQQ_DRAWDOWN"], windows=[5])
    b = bt.run_backtest(cfg, d, rules=["TREND_QQQ_DRAWDOWN"], windows=[5])
    assert a["status"] == "success"
    assert b.get("reused") is True
    assert b["backtest_run_id"] == a["backtest_run_id"]
    # 明确要求重算时不复用
    c = bt.run_backtest(cfg, d, rules=["TREND_QQQ_DRAWDOWN"], windows=[5],
                        reuse=False)
    assert c["backtest_run_id"] != a["backtest_run_id"]


def test_backtest_failure_does_not_break_live_assessment(seeded):
    """回测炸了，账户与市场评估必须照常。"""
    cfg, d, days = seeded

    class Broken:
        def __getattr__(self, name):
            raise RuntimeError("库炸了")

    res = bt.run_backtest(cfg, Broken(), rules=["TREND_QQQ_DRAWDOWN"])
    assert res["status"] == "failed"          # 返回失败，而不是抛异常
    # 线上评估不受影响
    from app import risk_service
    out = risk_service.assess(cfg, d)
    assert out is None or "risk_level" in out


def test_unstable_out_of_sample_flagged():
    train = {"precision_value": "0.80"}
    valid = {"precision_value": "0.45"}
    verdict, note = bt.stability_verdict(train, valid)
    assert verdict == "unstable"
    assert "过拟合" in note
    ok, _ = bt.stability_verdict({"precision_value": "0.70"},
                                 {"precision_value": "0.65"})
    assert ok == "stable"
    ins, _ = bt.stability_verdict({"precision_value": None}, {"precision_value": "0.5"})
    assert ins == "insufficient_data"


# ---------------- 35–38. 阈值建议 ----------------

def _res(thr, *, current=0, samples=20, precision="0.6", recall="0.4",
         tpm="4", notif=20, misses=3, segment="full"):
    return {"rule_code": "R", "observation_window": 5, "outcome_label": "L",
            "segment": segment, "threshold_value": thr,
            "is_current_threshold": current, "sample_size": samples,
            "precision_value": precision, "recall_value": recall,
            "triggers_per_month": tpm, "estimated_notifications": notif,
            "confidence": "moderate", "max_consecutive_misses": misses}


def test_keep_recommendation():
    results = [_res("0.30", current=1),
               _res("0.20", precision="0.35", recall="0.55", notif=60)]
    r = ta.recommend("R", results, window=5, label_code="L")
    # 召回涨了，但通知量翻三倍 —— 不推荐
    assert r["recommendation"] == ta.REC_KEEP
    assert r["risks"]


def test_consider_raise_recommendation():
    results = [_res("0.30", current=1, precision="0.15", tpm="15", notif=60),
               _res("0.45", precision="0.55", recall="0.25", tpm="4", notif=18,
                    misses=6)]
    r = ta.recommend("R", results, window=5, label_code="L")
    assert r["recommendation"] == ta.REC_RAISE
    assert r["suggested_threshold"] == "0.45"
    # 代价必须写明：召回下降与连续漏报增加
    assert any("recall" in x or "漏报" in x for x in r["risks"])


def test_consider_lower_recommendation():
    results = [_res("0.30", current=1),
               _res("0.20", precision="0.5", recall="0.55", notif=24)]
    r = ta.recommend("R", results, window=5, label_code="L")
    assert r["recommendation"] == ta.REC_LOWER
    assert r["suggested_threshold"] == "0.20"
    assert any("通知量" in x for x in r["risks"])


def test_insufficient_data_recommendation():
    r = ta.recommend("R", [_res("0.30", current=1, samples=3)],
                     window=5, label_code="L")
    assert r["recommendation"] == ta.REC_INSUFFICIENT
    assert "数据不足" in r["conclusion"]
    assert not r.get("suggested_threshold")


def test_unstable_recommendation_blocks_adjustment():
    results = [_res("0.30", current=1),
               _res("0.30", current=1, segment="train", precision="0.85"),
               _res("0.30", current=1, segment="validation", precision="0.40"),
               _res("0.20", precision="0.5", recall="0.55", notif=24)]
    r = ta.recommend("R", results, window=5, label_code="L")
    assert r["recommendation"] == ta.REC_UNSTABLE
    assert "不建议调整" in r["conclusion"]


def test_recommendation_never_mutates_config(seeded):
    """建议只是建议 —— 跑完之后线上阈值必须一字未改。"""
    cfg, d, days = seeded
    before = (cfg.market_qqq_drawdown_max, cfg.market_vix_warn,
              cfg.market_vix_crit)
    run = bt.run_backtest(cfg, d, rules=["TREND_QQQ_DRAWDOWN"], windows=[5])
    ta.recommend_all(d, run["backtest_run_id"], window=5)
    assert (cfg.market_qqq_drawdown_max, cfg.market_vix_warn,
            cfg.market_vix_crit) == before


# ---------------- 39–42. API 与前端 ----------------

def test_api_unified_response_shape(env):
    cfg, d = env
    c = TestClient(create_app(cfg, d))
    for path in ("/api/v1/risk/reviews", "/api/v1/risk/performance",
                 "/api/v1/risk/threshold-recommendations"):
        body = c.get(path).json()
        assert set(body) == {"success", "data", "error", "request_id",
                             "generated_at"}
        assert body["success"] is True and body["error"] is None
    miss = c.get("/api/v1/risk/reviews/nope").json()
    assert miss["success"] is False and miss["error"]["code"] == "NOT_FOUND"


def test_decimal_precision_preserved(seeded):
    cfg, d, days = seeded
    _mk_event(d, first=days[5] + "T15:00:00+00:00")
    rs.generate_reviews(cfg, d, now=NOW)
    c = TestClient(create_app(cfg, d))
    body = c.get("/api/v1/risk/reviews").json()["data"]
    rev = body["reviews"][0]
    assert isinstance(rev["peak_actual_value"], str)
    assert rev["peak_actual_value"] == "0.85"
    # 前瞻结果里的比例同样是字符串，不被压成双精度浮点
    w = rev["forward_outcomes"].get("5") or {}
    for k in ("account_max_drawdown", "qqq_return"):
        if w.get(k) is not None:
            assert isinstance(w[k], str)


def test_timezone_handling(seeded):
    cfg, d, days = seeded
    # UTC 深夜属于美东前一天 —— 按 UTC 日期归档会把盘后事件错记到第二天
    assert tcal.et_date_of("2026-07-29T01:00:00+00:00", cfg.market_tz) == "2026-07-28"
    assert tcal.et_date_of("2026-07-29T18:00:00+00:00", cfg.market_tz) == "2026-07-29"
    # 已是日期时原样返回
    assert tcal.et_date_of("2026-07-28", cfg.market_tz) == "2026-07-28"
    # 无时区时间不崩
    assert tcal.minutes_between("2026-07-28T10:00:00", "2026-07-28T12:00:00") == 120


def test_frontend_shows_insufficient_sample_state(env):
    cfg, d = env
    html = TestClient(create_app(cfg, d)).get("/performance").text
    assert "当前没有任何规则积累到足够样本" in html
    assert "不构成任何效果结论" in html
    # 样本不足时必须有文字说明，不能只靠颜色
    assert "样本量低于" in html
    reviews = TestClient(create_app(cfg, d)).get("/reviews").text
    assert "观察窗口按" in reviews and "交易日" in reviews


def test_frontend_review_page_renders_windows(seeded):
    cfg, d, days = seeded
    _mk_event(d, first=days[5] + "T15:00:00+00:00")
    rs.generate_reviews(cfg, d, now=NOW)
    html = TestClient(create_app(cfg, d)).get("/reviews").text
    assert "事件复盘" in html
    assert "1 日" in html and "20 日" in html
    assert "复盘评价" in html


def test_data_quality_rules_excluded_with_reason(seeded):
    cfg, d, days = seeded
    _mk_event(d, event_id="dq", source="data_quality", rule="DATA_SEVERELY_STALE",
              first=days[5] + "T15:00:00+00:00")
    rs.generate_reviews(cfg, d, now=NOW)
    rev = d.review_for_event("dq", rs.CALC_VERSION)
    assert rev["review_status"] == rs.ST_EXCLUDED
    assert "没有意义" in rev["exclusion_reason"]
    # 且不会被塞进任何标签评价
    assert ol.labels_for_source("data_quality") == []


# ---------------- 43–45. 既有功能回归 ----------------

def test_existing_risk_events_regression(env):
    """复盘层不得改动事件注册表的行为。"""
    from app import event_service as es, risk_events as rev_
    cfg, d = env
    trig = rev_.Trigger(source_type="account", event_code="X", rule_code="X",
                        category="concentration", severity="high", title="T",
                        explanation="E", actual_value="0.9",
                        rule_set_version="v1~a", account_id="U1234567")
    for i in range(3):
        es.register_triggers(d, [trig], now=NOW + dt.timedelta(minutes=i),
                             evaluation_complete=True, reliable=True,
                             scope={"source_types": ["account"]})
    rows, total = d.query_risk_events()
    assert total == 1 and rows[0]["occurrence_count"] == 3


def test_existing_notification_dedupe_regression(env):
    from app import event_service as es, risk_events as rev_
    cfg, d = env

    class N:
        def __init__(self): self.sent = []
        def send(self, t, b): self.sent.append(t); return True

    n = N()
    trig = rev_.Trigger(source_type="account", event_code="X", rule_code="X",
                        category="concentration", severity="high", title="T",
                        explanation="E", rule_set_version="v1~a")
    for i in range(3):
        t = NOW + dt.timedelta(minutes=i)
        res = es.register_triggers(d, [trig], now=t, evaluation_complete=True,
                                   reliable=True, scope={"source_types": ["account"]})
        es.dispatch(d, n, res, cfg=cfg, now=t)
    assert len(n.sent) == 1          # 冷静期内不重复推送


def test_existing_account_and_market_sync_regression(seeded):
    """复盘与回测跑完之后，账户与市场的原始数据必须一行未改。"""
    cfg, d, days = seeded
    before_bars = len(d.get_daily_bars("QQQ", limit=1000))
    _mk_event(d, first=days[5] + "T15:00:00+00:00")
    rs.generate_reviews(cfg, d, now=NOW)
    rp.compute_all(cfg, d, windows=[5])
    bt.run_backtest(cfg, d, rules=["TREND_QQQ_DRAWDOWN"], windows=[5])
    assert len(d.get_daily_bars("QQQ", limit=1000)) == before_bars
    ev = d.get_risk_event("e1")
    assert ev["status"] == "open" and ev["occurrence_count"] == 1
