"""AI 每日风险简报 v1 验收测试（对应清单 1–40）。

钉死五条：
  1. **AI 不自行计算任何指标** —— 编造的数字必须被校验器拦下
  2. **blocked 时不输出虚假风险结论** —— 数据故障不得显示成低风险
  3. **AI 失败时仍能生成模板简报** —— AI 不可用不能导致当天没有简报
  4. **not_applicable 不被解释为正常，入金不被说成盈利**
  5. **同一简报不重复推送，飞书失败不影响落库**
"""
import datetime as dt
import json
from decimal import Decimal as D
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app import brief_context as bc
from app import brief_generator as bg
from app import brief_prompt as bp
from app import brief_service as bs
from app import brief_validator as bv
from app import execution_context as ec
from app.db import Database
from app.webapp import create_app

NOW = dt.datetime(2026, 7, 29, 22, 0, tzinfo=dt.timezone.utc)
DAY = "2026-07-29"
ACC = "U1234567"


# ---------------- 夹具 ----------------

class FakeNotifier:
    def __init__(self, ok=True, boom=False):
        self.sent, self._ok, self._boom = [], ok, boom

    def send(self, title, body):
        if self._boom:
            raise RuntimeError("飞书接口 500")
        self.sent.append((title, body))
        return self._ok


class FakeProvider:
    """可编程 AI provider。测试不需要网络。"""
    name = "fake"
    model = "fake-model"

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def generate(self, *, system, messages, schema):
        self.calls.append({"system": system, "messages": messages})
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r if isinstance(r, str) else json.dumps(r, ensure_ascii=False)


def test_build_provider_uses_configurable_openai_compatible_endpoint():
    cfg = SimpleNamespace(
        ai_api_key="test-key",
        ai_base_url="https://ai.example.test/v1/",
        ai_model="test-model",
        ai_max_tokens=1234,
        ai_timeout_seconds=42,
    )
    provider = bg.build_provider(cfg)
    assert isinstance(provider, bg.OpenAICompatibleProvider)
    assert provider.name == "openai_compatible"
    assert provider.model == "test-model"
    assert provider._base_url == "https://ai.example.test/v1"


def test_openai_compatible_provider_sends_json_schema(monkeypatch):
    captured = {}

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"choices": [{"message": {"content": '{"ok": true}'}}]}

    class Client:
        def __init__(self, *, timeout):
            captured["timeout"] = timeout

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def post(self, url, *, headers, json):
            captured.update(url=url, headers=headers, payload=json)
            return Response()

    import httpx
    monkeypatch.setattr(httpx, "Client", Client)
    provider = bg.OpenAICompatibleProvider(
        api_key="test-key", base_url="https://ai.example.test/v1",
        model="test-model", max_tokens=99, timeout=7)
    result = provider.generate(
        system="system", messages=[{"role": "user", "content": "input"}],
        schema={"type": "object", "properties": {"ok": {"type": "boolean"}}})

    assert result == '{"ok": true}'
    assert captured["url"] == "https://ai.example.test/v1/chat/completions"
    assert captured["headers"]["Authorization"] == "Bearer test-key"
    assert captured["payload"]["response_format"]["type"] == "json_schema"


@pytest.fixture
def env(make_config):
    cfg = make_config(ib_account=ACC, apprise_urls=[])
    d = Database(cfg.db_path)
    d.init_schema()
    return cfg, d


def _seed(d, *, ts=None, quality="fresh", ai_allowed=True, equity="100000",
          cash="30000", positions=None):
    ts = ts or (NOW - dt.timedelta(hours=1)).isoformat()
    pos = positions if positions is not None else [
        {"symbol": "AAA", "qty": D("100"), "avg_cost": D("50"),
         "market_value": D("40000"), "upl": D("-5000"), "price": D("400"),
         "price_quality": "DELAYED"},
        {"symbol": "BBB", "qty": D("100"), "avg_cost": D("30"),
         "market_value": D("30000"), "upl": D("1000"), "price": D("300"),
         "price_quality": "DELAYED"},
    ]
    pv = sum(p["market_value"] for p in pos)
    d.write_account_snapshot(
        snapshot_id=f"snap-{ts[-8:]}", ts=ts, sync_run_id="r1",
        quality_status=quality, ai_analysis_allowed=ai_allowed,
        balances={"account": ACC, "currency": "USD", "equity": D(equity),
                  "cash": D(cash), "total_cash": D(cash),
                  "margin_balance": D(cash), "buying_power": D(equity) * 2,
                  "available_funds": D(cash), "excess_liquidity": D(cash),
                  "maint_margin": D("10000"), "init_margin": D("12000"),
                  "positions_value": pv},
        positions=pos)
    return ts


def _assess(cfg, d, now=NOW):
    from app import risk_service
    return risk_service.assess(cfg, d, now=now)


def _ctx(cfg, d, now=NOW, day=DAY):
    return bc.build(cfg, d, brief_date=day, now=now)


# ---------------- 1–3. 三种生成模式 ----------------

def test_full_brief(env):
    cfg, d = env
    _seed(d)
    _assess(cfg, d)
    ctx = _ctx(cfg, d)
    assert ctx["generation_mode"] in (bc.GATE_FULL, bc.GATE_LIMITED)
    res = bg.generate(ctx, provider=None)
    assert res["status"] == bg.STATUS_TEMPLATE
    assert res["validation"]["valid"], res["validation"]["errors"]
    assert res["content"]["account_summary"]["risk_level"]


def test_limited_brief(env):
    """账户数据过期 -> limited：可以出简报，但必须声明限制。"""
    cfg, d = env
    _seed(d, ts=(NOW - dt.timedelta(hours=10)).isoformat())
    _assess(cfg, d)
    ctx = _ctx(cfg, d)
    assert ctx["generation_mode"] == bc.GATE_LIMITED
    assert any("小时" in x for x in ctx["data_limitations"])
    assert "确定性的风险升级结论" in ctx["gate"]["blocked_analyses"]
    res = bg.generate(ctx, provider=None)
    assert res["validation"]["valid"]
    assert any("受限简报" in s for s in res["content"]["executive_summary"])


def test_blocked_brief_gives_no_risk_conclusion(env):
    """blocked：**一个风险数字都不给。**"""
    cfg, d = env
    _seed(d, quality="error", ai_allowed=False)
    _assess(cfg, d)
    ctx = _ctx(cfg, d)
    assert ctx["generation_mode"] == bc.GATE_BLOCKED
    res = bg.generate(ctx, provider=None)
    assert res["status"] == bg.STATUS_BLOCKED
    c = res["content"]
    assert c["account_summary"]["risk_level"] is None
    assert c["account_summary"]["risk_score"] is None
    assert c["top_risks"] == []
    blob = json.dumps(c, ensure_ascii=False)
    for bad in ("风险较低", "风险可控", "无风险", "状况良好"):
        assert bad not in blob
    assert res["validation"]["valid"], res["validation"]["errors"]


def test_ai_analysis_not_allowed_blocks(env):
    cfg, d = env
    _seed(d, ai_allowed=False)
    _assess(cfg, d)
    ctx = _ctx(cfg, d)
    assert ctx["generation_mode"] == bc.GATE_BLOCKED
    reasons = " ".join(ctx["gate"]["blocked_reasons"])
    assert "ai_analysis_allowed=false" in reasons


# ---------------- 5–7. 数据新鲜度与 T+1 ----------------

def test_account_stale_is_limited(env):
    cfg, d = env
    _seed(d, ts=(NOW - dt.timedelta(hours=8)).isoformat())
    _assess(cfg, d)
    gate = _ctx(cfg, d)["gate"]
    assert gate["mode"] == bc.GATE_LIMITED
    assert any(c["check"] == "account_fresh" and c["passed"] is False
               for c in gate["checks"])


def test_account_severely_stale_is_blocked(env):
    cfg, d = env
    _seed(d, ts=(NOW - dt.timedelta(hours=60)).isoformat())
    _assess(cfg, d, now=NOW - dt.timedelta(hours=60) + dt.timedelta(minutes=1))
    gate = _ctx(cfg, d)["gate"]
    assert gate["mode"] == bc.GATE_BLOCKED


def test_market_stale_is_noted(env):
    """账户与市场时间差过大时必须显式说明，不得伪装成同一时点。"""
    align = bc.time_alignment(
        account_as_of="2026-07-29T22:00:00+00:00",
        market_as_of="2026-07-27T20:00:00+00:00",
        flex_report_to="20260728", now=NOW)
    assert align["exceeds_tolerance"] is True
    assert any("不代表同一时刻" in n for n in align["notes"])
    assert any("T+1" in n for n in align["notes"])


def test_flex_t_plus_one_stated(env):
    cfg, d = env
    _seed(d)
    _assess(cfg, d)
    ctx = _ctx(cfg, d)
    res = bg.generate(ctx, provider=None)
    blob = json.dumps(res["content"], ensure_ascii=False)
    assert "T+1" in blob


# ---------------- 8–10. 口径纪律 ----------------

def test_null_never_becomes_zero(env):
    """上下文为 null 的字段写成 0 必须被拦下。"""
    cfg, d = env
    _seed(d)
    _assess(cfg, d)
    ctx = _ctx(cfg, d)
    ctx["account"]["cash_ratio"] = None
    ctx["performance"] = dict(ctx.get("performance") or {},
                              realized_pnl=None)
    content = bg.template(ctx)
    content["account_summary"]["cash_ratio"] = "0"
    content["performance"]["realized_pnl"] = "0.00"
    r = bv.validate(content, ctx)
    assert not r["valid"]
    assert any("不得显示为零" in e for e in r["errors"])
    # 模板自己绝不会犯这个错
    ok = bg.template(ctx)
    assert ok["account_summary"]["cash_ratio"] is None


def test_deposit_not_called_profit(env):
    cfg, d = env
    _seed(d)
    _assess(cfg, d)
    ctx = _ctx(cfg, d)
    res = bg.generate(ctx, provider=None)
    blob = json.dumps(res["content"], ensure_ascii=False)
    assert "入金不是盈利" in blob


def test_withdrawal_not_called_loss(env):
    cfg, d = env
    _seed(d)
    _assess(cfg, d)
    ctx = _ctx(cfg, d)
    res = bg.generate(ctx, provider=None)
    blob = json.dumps(res["content"], ensure_ascii=False)
    assert "出金不是亏损" in blob


# ---------------- 11–13. 集中度与观察期 ----------------

def test_top3_not_applicable_is_shown(env):
    cfg, d = env
    _seed(d)                      # 2 只持仓 -> Top3/Top5 均不适用
    _assess(cfg, d)
    ctx = _ctx(cfg, d)
    na = {x["rule_code"] for x in ctx["concentration"]["not_applicable_rules"]}
    assert "CONC_TOP3_HIGH" in na
    res = bg.generate(ctx, provider=None)
    narr = res["content"]["concentration"]["narrative"]
    assert "不适用不等于规则通过" in narr
    assert "HHI" in narr and "等效持仓数" in narr


def test_top5_not_applicable_is_shown(env):
    cfg, d = env
    _seed(d)
    _assess(cfg, d)
    ctx = _ctx(cfg, d)
    na = {x["rule_code"] for x in ctx["concentration"]["not_applicable_rules"]}
    assert "CONC_TOP5_HIGH" in na
    listed = " ".join(bg.template(ctx)["concentration"]["not_applicable_rules"])
    assert "CONC_TOP5_HIGH" in listed


def test_not_applicable_written_as_normal_is_rejected(env):
    cfg, d = env
    _seed(d)
    _assess(cfg, d)
    ctx = _ctx(cfg, d)
    content = bg.template(ctx)
    content["concentration"]["narrative"] = "集中度指标全部正常，检查通过。"
    r = bv.validate(content, ctx)
    assert not r["valid"]
    assert any("不适用不等于规则通过" in e for e in r["errors"])


def test_observation_period_note(env):
    """新集中度规则处于观察期，不得声称已被历史验证。"""
    cfg, d = env
    _seed(d, positions=[
        {"symbol": "AAA", "qty": D("100"), "avg_cost": D("50"),
         "market_value": D("60000"), "upl": D("-5000"), "price": D("600"),
         "price_quality": "DELAYED"},
        {"symbol": "BBB", "qty": D("100"), "avg_cost": D("30"),
         "market_value": D("5000"), "upl": D("100"), "price": D("50"),
         "price_quality": "DELAYED"},
        {"symbol": "CCC", "qty": D("100"), "avg_cost": D("30"),
         "market_value": D("5000"), "upl": D("100"), "price": D("50"),
         "price_quality": "DELAYED"}])
    _assess(cfg, d)
    ctx = _ctx(cfg, d)
    assert ctx["concentration"]["observation_period_rules"]
    blob = json.dumps(bg.template(ctx), ensure_ascii=False)
    assert "数据积累期" in blob and "尚无足够历史样本" in blob


# ---------------- 14–17. 风险变化分类 ----------------

def _event(code, sev, *, first, last, status="active", escalated_at=None,
           prev=None, symbol=None, resolved_at=None):
    return {"event_id": f"e-{code}-{symbol or ''}", "rule_code": code,
            "title": f"{code} 标题", "severity": sev, "peak_severity": sev,
            "status": status, "first_detected_at": first,
            "last_detected_at": last, "resolved_at": resolved_at,
            "escalated_at": escalated_at, "previous_severity": prev,
            "affected_symbol": symbol, "occurrence_count": 3,
            "rule_version": "v1", "source_type": "account",
            "actual_value": "0.9", "threshold_value": "0.8"}


def test_new_high_event(env):
    since = NOW - dt.timedelta(days=1)
    ch = bc.classify_changes(
        [_event("ACC_CASH_LOW", "high", first=(NOW - dt.timedelta(hours=2)).isoformat(),
                last=NOW.isoformat())], since=since, now=NOW)
    assert ch["counts"]["new"] == 1
    assert ch["ordered"][0]["change_kind"] == "new"


def test_escalation_to_critical_ranks_first(env):
    since = NOW - dt.timedelta(days=1)
    events = [
        _event("A_WARN", "warning", first=(NOW - dt.timedelta(days=5)).isoformat(),
               last=NOW.isoformat()),
        _event("B_NEW_HIGH", "high", first=(NOW - dt.timedelta(hours=1)).isoformat(),
               last=NOW.isoformat()),
        _event("C_ESC", "critical", first=(NOW - dt.timedelta(days=3)).isoformat(),
               last=NOW.isoformat(),
               escalated_at=(NOW - dt.timedelta(hours=3)).isoformat(),
               prev="high"),
    ]
    ch = bc.classify_changes(events, since=since, now=NOW)
    assert ch["ordered"][0]["rule_code"] == "C_ESC"       # critical 升级最优先
    assert ch["ordered"][0]["change_kind"] == "escalated"
    assert ch["ordered"][1]["rule_code"] == "B_NEW_HIGH"  # 其次是新 high


def test_resolved_event(env):
    since = NOW - dt.timedelta(days=1)
    ch = bc.classify_changes(
        [_event("R", "high", first=(NOW - dt.timedelta(days=3)).isoformat(),
                last=NOW.isoformat(), status="resolved",
                resolved_at=(NOW - dt.timedelta(hours=2)).isoformat())],
        since=since, now=NOW)
    assert ch["counts"]["resolved"] == 1


def test_persistent_events_are_merged(env):
    """同一规则在多个标的上持续触发，合并成一句话 —— 避免每天重复大段内容。"""
    since = NOW - dt.timedelta(days=1)
    old = (NOW - dt.timedelta(days=5)).isoformat()
    events = [_event("POS_LOSS", "warning", first=old, last=NOW.isoformat(),
                     symbol=s) for s in ("AAA", "BBB", "CCC")]
    ch = bc.classify_changes(events, since=since, now=NOW)
    assert len(ch["persistent"]) == 1
    m = ch["persistent"][0]
    assert m["merged_count"] == 3
    assert "3 个对象上持续触发" in m["merge_note"]
    assert set(m["merged_affected"]) == {"AAA", "BBB", "CCC"}


# ---------------- 18–19. 历史基线 ----------------

def test_seven_day_baseline(env):
    b = bc.baseline([D(x) for x in ("10", "20", "30", "40", "50", "60")],
                    window=7)
    assert b["sufficient"] is True
    assert b["sample_size"] == 6
    assert b["median"] == "35"
    cmp_ = bc.compare_to_baseline(D("50"), b)
    assert cmp_["comparable"] and cmp_["direction"] == "above"
    assert cmp_["delta"] == "15"


def test_thirty_day_insufficient_samples(env):
    b = bc.baseline([D("10"), D("20")], window=30)
    assert b["sufficient"] is False
    assert "历史样本不足" in b["note"]
    cmp_ = bc.compare_to_baseline(D("50"), b)
    assert cmp_["comparable"] is False
    assert "历史样本不足" in cmp_["reason"]


def test_insufficient_baseline_never_claims_pattern(env):
    cfg, d = env
    _seed(d)
    _assess(cfg, d)
    ctx = _ctx(cfg, d)
    content = bg.template(ctx)
    h = content["historical_comparison"]
    assert "历史样本不足" in (h["thirty_day_baseline"] or "")


# ---------------- 20–25. AI 输出校验 ----------------

def _valid_ai_output(ctx):
    """从模板产出一份合法输出，测试再在其上做单点破坏。"""
    return bg.template(ctx)


def test_ai_invalid_json_falls_back(env):
    cfg, d = env
    _seed(d); _assess(cfg, d)
    ctx = _ctx(cfg, d)
    p = FakeProvider(["这不是 JSON{{{", "还是不是 JSON"])
    res = bg.generate(ctx, provider=p)
    assert res["status"] == bg.STATUS_TEMPLATE
    assert len(p.calls) == bg.MAX_AI_ATTEMPTS
    assert res["validation"]["valid"]


def test_ai_nonexistent_symbol_rejected(env):
    cfg, d = env
    _seed(d); _assess(cfg, d)
    ctx = _ctx(cfg, d)
    bad = _valid_ai_output(ctx)
    bad["top_risks"] = [{"title": "X", "explanation": "y",
                         "recommended_check": "检查 z",
                         "affected_object": "ZZZZ_NOT_REAL"}]
    r = bv.validate(bad, ctx)
    assert not r["valid"]
    assert any("不存在的标的" in e for e in r["errors"])


def test_ai_fabricated_number_rejected(env):
    """AI 编造的数字必须被拦下 —— 这是「AI 不自行计算」的执行手段。"""
    cfg, d = env
    _seed(d); _assess(cfg, d)
    ctx = _ctx(cfg, d)
    bad = _valid_ai_output(ctx)
    bad["account_summary"]["narrative"] = "净资产 987654.32，现金比例 77.77%。"
    r = bv.validate(bad, ctx)
    assert not r["valid"]
    assert any("不存在" in e and "987654.32" in e for e in r["errors"])


def test_ai_nonexistent_rule_rejected(env):
    cfg, d = env
    _seed(d); _assess(cfg, d)
    ctx = _ctx(cfg, d)
    bad = _valid_ai_output(ctx)
    bad["top_risks"] = [{"title": "X", "explanation": "y",
                         "recommended_check": "检查 z",
                         "rule_code": "FAKE_RULE_CODE"}]
    r = bv.validate(bad, ctx)
    assert not r["valid"]
    assert any("不存在的规则" in e for e in r["errors"])


def test_actionable_wording_is_allowed(env):
    """措辞禁令已解除：明确的操作倾向不再被校验器拦截。

    仍受约束的是**数字可追溯性** —— 措辞自由不等于可以编造数字。
    """
    cfg, d = env
    _seed(d); _assess(cfg, d)
    ctx = _ctx(cfg, d)
    for text in ("建议减仓至 20%", "不追涨，等待回踩 EMA20", "跌破止损，执行减仓"):
        content = _valid_ai_output(ctx)
        content["checks"] = [text]
        r = bv.validate(content, ctx)
        assert r["valid"], (text, r["errors"])


def test_legitimate_check_wording_passes(env):
    """「检查买入依据是否仍成立」是合法文案，不得被误杀。"""
    cfg, d = env
    _seed(d); _assess(cfg, d)
    ctx = _ctx(cfg, d)
    good = _valid_ai_output(ctx)
    good["checks"] = ["检查该持仓的买入依据是否仍成立",
                      "检查计划仓位上限与退出条件"]
    r = bv.validate(good, ctx)
    assert r["valid"], r["errors"]


def test_forward_looking_wording_is_allowed(env):
    """预测措辞禁令已解除，校验器不再扫描措辞。"""
    cfg, d = env
    _seed(d); _assess(cfg, d)
    ctx = _ctx(cfg, d)
    content = _valid_ai_output(ctx)
    content["market_summary"]["narrative"] = "半导体板块即将反弹，股价将回升。"
    r = bv.validate(content, ctx)
    assert r["valid"], r["errors"]


def test_validation_retry_then_success(env):
    """校验失败 -> 回灌错误重试 -> 第二次通过。"""
    cfg, d = env
    _seed(d); _assess(cfg, d)
    ctx = _ctx(cfg, d)
    good = _valid_ai_output(ctx)
    bad = json.loads(json.dumps(good, ensure_ascii=False))
    # 用「编造的数字」触发校验失败 —— 措辞禁令已解除，数字可追溯性仍是硬闸门
    bad["checks"] = ["检查净资产 987654.32 的异常变动"]
    p = FakeProvider([bad, good])
    res = bg.generate(ctx, provider=p)
    assert res["status"] == bg.STATUS_AI
    assert res["attempts"] == 2
    assert len(p.calls) == 2
    # 第二次调用带上了违规原因
    assert "未通过校验" in p.calls[1]["messages"][-1]["content"]


def test_template_fallback_after_max_attempts(env):
    cfg, d = env
    _seed(d); _assess(cfg, d)
    ctx = _ctx(cfg, d)
    bad = _valid_ai_output(ctx)
    # 用「编造的数字」触发校验失败 —— 措辞禁令已解除，数字可追溯性仍是硬闸门
    bad["checks"] = ["检查净资产 987654.32 的异常变动"]
    p = FakeProvider([bad, bad])
    res = bg.generate(ctx, provider=p)
    assert res["status"] == bg.STATUS_TEMPLATE
    assert res["validation"]["valid"]
    assert len(res["errors"]) == bg.MAX_AI_ATTEMPTS


def test_ai_exception_does_not_break_brief(env):
    """AI 服务整个挂掉，简报照样出得来。"""
    cfg, d = env
    _seed(d); _assess(cfg, d)
    ctx = _ctx(cfg, d)
    p = FakeProvider([RuntimeError("API 超时"), RuntimeError("API 超时")])
    res = bg.generate(ctx, provider=p)
    assert res["status"] == bg.STATUS_TEMPLATE
    assert res["validation"]["valid"]


def test_missing_critical_event_rejected(env):
    cfg, d = env
    _seed(d); _assess(cfg, d)
    ctx = _ctx(cfg, d)
    bad = _valid_ai_output(ctx)          # 先出简报
    # 再往上下文里塞一条 critical 新事件 —— 简报里没有它，必须被判遗漏
    ctx["risk_changes"]["new"] = [{"rule_code": "ACC_MAINT_MARGIN_BUFFER_LOW",
                                   "title": "维持保证金缓冲过低",
                                   "severity": "critical"}]
    r = bv.validate(bad, ctx)
    assert not r["valid"]
    assert any("遗漏" in e for e in r["errors"])


def test_account_id_leak_rejected(env):
    cfg, d = env
    _seed(d); _assess(cfg, d)
    ctx = _ctx(cfg, d)
    bad = _valid_ai_output(ctx)
    bad["headline"] = f"账户 {ACC} 今日风险简报"
    r = bv.validate(bad, ctx)
    assert not r["valid"]
    assert any("泄露了完整账户标识" in e for e in r["errors"])


def test_timestamp_mismatch_rejected(env):
    cfg, d = env
    _seed(d); _assess(cfg, d)
    ctx = _ctx(cfg, d)
    bad = _valid_ai_output(ctx)
    bad["account_summary"]["data_as_of"] = "2020-01-01T00:00:00+00:00"
    r = bv.validate(bad, ctx)
    assert not r["valid"]
    assert any("数据时间" in e for e in r["errors"])


def test_schema_extra_field_rejected(env):
    cfg, d = env
    _seed(d); _assess(cfg, d)
    ctx = _ctx(cfg, d)
    bad = _valid_ai_output(ctx)
    bad["account_summary"]["sharpe_ratio"] = "1.8"      # Schema 之外的指标
    r = bv.validate(bad, ctx)
    assert not r["valid"]
    assert any("Schema 之外的字段" in e for e in r["errors"])


# ---------------- 26–28. 幂等与 revision ----------------

def test_same_context_hash_is_reused(env):
    cfg, d = env
    _seed(d); _assess(cfg, d)
    a = bs.generate_brief(cfg, d, brief_date=DAY, now=NOW)
    b = bs.generate_brief(cfg, d, brief_date=DAY, now=NOW)
    assert a["status"] == "success"
    assert b["reused"] is True
    assert b["brief_id"] == a["brief_id"]
    rows, total = d.query_briefs()
    assert total == 1


def test_data_change_creates_new_revision(env):
    cfg, d = env
    _seed(d); _assess(cfg, d)
    a = bs.generate_brief(cfg, d, brief_date=DAY, now=NOW)
    # 数据变了 -> context_hash 变 -> 新 revision，旧版本不被覆盖
    _seed(d, ts=(NOW - dt.timedelta(minutes=30)).isoformat(), equity="120000")
    _assess(cfg, d, now=NOW + dt.timedelta(minutes=1))
    b = bs.generate_brief(cfg, d, brief_date=DAY,
                          now=NOW + dt.timedelta(minutes=2))
    assert b["brief_id"] != a["brief_id"]
    assert d.get_brief(a["brief_id"]) is not None        # 旧版本仍在
    assert d.get_brief(b["brief_id"])["revision"] == 2


def test_manual_trigger_min_interval(env):
    cfg, d = env
    _seed(d); _assess(cfg, d)
    bs.generate_brief(cfg, d, brief_date=DAY, trigger=bs.TRIGGER_MANUAL, now=NOW)
    ok, why = bs.can_trigger_manually(d, now=NOW + dt.timedelta(seconds=10))
    assert ok is False and "请至少间隔" in why
    ok2, _ = bs.can_trigger_manually(d, now=NOW + dt.timedelta(hours=1))
    assert ok2 is True


# ---------------- 29–31. 推送 ----------------

def test_feishu_not_sent_twice(env):
    cfg, d = env
    _seed(d); _assess(cfg, d)
    n = FakeNotifier()
    r1 = bs.generate_brief(cfg, d, brief_date=DAY, trigger=bs.TRIGGER_SCHEDULED,
                           notifier=n, now=NOW)
    assert r1["delivery"]["status"] == "sent"
    assert len(n.sent) == 1
    # 同一 context_hash 再来一次 -> 跳过
    r2 = bs.generate_brief(cfg, d, brief_date=DAY, trigger=bs.TRIGGER_SCHEDULED,
                           notifier=n, now=NOW)
    assert r2["delivery"]["status"] == "skipped"
    assert "不重复打扰" in r2["delivery"]["reason"]
    assert len(n.sent) == 1


def test_manual_regenerate_does_not_push(env):
    """手动重新生成默认不重复推送。"""
    cfg, d = env
    _seed(d); _assess(cfg, d)
    n = FakeNotifier()
    bs.generate_brief(cfg, d, brief_date=DAY, trigger=bs.TRIGGER_REGENERATE,
                      notifier=n, regenerate=True, now=NOW)
    assert n.sent == []


def test_feishu_failure_does_not_break_persistence(env):
    cfg, d = env
    _seed(d); _assess(cfg, d)
    n = FakeNotifier(boom=True)
    res = bs.generate_brief(cfg, d, brief_date=DAY,
                            trigger=bs.TRIGGER_SCHEDULED, notifier=n, now=NOW)
    assert res["status"] == "success"
    assert res["brief_id"]
    assert d.get_brief(res["brief_id"]) is not None      # 简报照样落库
    assert res["delivery"]["status"] == "failed"
    rec = d.brief_deliveries(res["brief_id"])
    assert rec and rec[0]["status"] == "failed"


def test_blocked_brief_is_still_delivered(env):
    """blocked 简报允许发送数据故障提示 —— 用户需要知道系统出问题了。"""
    cfg, d = env
    _seed(d, quality="error", ai_allowed=False)
    _assess(cfg, d)
    n = FakeNotifier()
    res = bs.generate_brief(cfg, d, brief_date=DAY,
                            trigger=bs.TRIGGER_SCHEDULED, notifier=n, now=NOW)
    assert res["generation_status"] == bg.STATUS_BLOCKED
    assert res["delivery"]["status"] == "sent"
    title, body = n.sent[0]
    assert "⛔" in title
    assert "检查" in body
    for bad in ("风险较低", "风险可控"):
        assert bad not in body


def test_validation_failed_is_not_delivered(env):
    cfg, d = env
    _seed(d); _assess(cfg, d)
    ctx = _ctx(cfg, d)
    bad = _valid_ai_output(ctx)
    # 用「编造的数字」触发校验失败 —— 措辞禁令已解除，数字可追溯性仍是硬闸门
    bad["checks"] = ["检查净资产 987654.32 的异常变动"]
    n = FakeNotifier()
    # 让模板也"坏掉"，模拟校验失败落库的情形
    orig = bg.template
    bg.template = lambda c: bad
    try:
        res = bs.generate_brief(cfg, d, brief_date=DAY,
                                trigger=bs.TRIGGER_SCHEDULED, notifier=n, now=NOW)
    finally:
        bg.template = orig
    assert res["status"] == "partial"
    row = d.get_brief(res["brief_id"])
    assert row["validation_status"] == "failed"
    assert res["delivery"]["status"] == "skipped"
    assert n.sent == []


# ---------------- 32–34. 安全 ----------------

def test_token_never_enters_prompt(env):
    """Token / 密钥 / 原始 XML 一律进不了 prompt。"""
    ctx = {
        "brief_date": DAY,
        "flex_token": "SECRET123456",
        "account": {"api_key": "sk-abc", "risk_level": "high",
                    "raw_xml": "<FlexQueryResponse/>", "account_id": ACC},
        "nested": [{"password": "p", "ok": 1}],
        "account_id_masked": "U1***67",
        "_internal": {"positions": []},
    }
    clean = bp.sanitize_context(ctx)
    blob = json.dumps(clean, ensure_ascii=False)
    for bad in ("SECRET123456", "sk-abc", "FlexQueryResponse", ACC, "password"):
        assert bad not in blob, bad
    assert clean["account_id_masked"] == "U1***67"
    assert clean["account"]["risk_level"] == "high"
    assert "_internal" not in clean

    msgs = bp.build_messages(ctx)
    assert "SECRET123456" not in msgs[0]["content"]
    assert ACC not in msgs[0]["content"]


def test_raw_flex_xml_not_in_prompt(env):
    cfg, d = env
    _seed(d); _assess(cfg, d)
    ctx = _ctx(cfg, d)
    body = bp.build_messages(ctx)[0]["content"]
    assert "<FlexQueryResponse" not in body
    assert "<?xml" not in body


def test_account_id_is_masked_everywhere(env):
    cfg, d = env
    _seed(d); _assess(cfg, d)
    res = bs.generate_brief(cfg, d, brief_date=DAY, now=NOW)
    row = d.get_brief(res["brief_id"])
    assert ACC not in json.dumps(dict(row), ensure_ascii=False, default=str)
    assert row["account_id_masked"] == bc.mask_account(ACC)
    assert row["account_id_masked"] != ACC


def test_source_refs_store_hash_not_value(env):
    cfg, d = env
    _seed(d); _assess(cfg, d)
    res = bs.generate_brief(cfg, d, brief_date=DAY, now=NOW)
    srcs = d.brief_sources(res["brief_id"])
    assert srcs
    for s in srcs:
        assert s["value_hash"] and len(s["value_hash"]) == 16
        assert "value" not in s or s.get("value") is None
    tables = {s["source_table"] for s in srcs}
    assert "risk_assessments" in tables


# ---------------- 35–38. API 与页面 ----------------

def test_api_unified_response(env):
    cfg, d = env
    _seed(d); _assess(cfg, d)
    bs.generate_brief(cfg, d, brief_date=DAY, now=NOW)
    cl = TestClient(create_app(cfg, d))

    r = cl.get("/api/v1/briefs/latest")
    assert r.status_code == 200
    body = r.json()
    assert body["success"] is True and "data" in body and "request_id" in body
    data = body["data"]
    assert data["generation_status"] == bg.STATUS_TEMPLATE
    assert data["disclaimer"]
    assert data["source_references"]

    r2 = cl.get("/api/v1/briefs?limit=5")
    assert r2.status_code == 200
    assert r2.json()["data"]["total"] == 1

    bid = data["brief_id"]
    r3 = cl.get(f"/api/v1/briefs/{bid}")
    assert r3.status_code == 200
    assert r3.json()["data"]["brief_id"] == bid

    r4 = cl.get("/api/v1/briefs/does-not-exist")
    assert r4.status_code == 404
    assert r4.json()["success"] is False


def test_api_manual_generate(env):
    """手动生成走的是**墙钟**就绪判定（数据年龄 <= FRESH_ENOUGH_HOURS）。

    所以这里必须锚在真实 now 上播种 —— 用固定的 NOW 播种，写的当天能过，
    过 12 小时就判「未就绪」返回 waiting。这类测试是定时炸弹。
    """
    cfg, d = env
    real_now = dt.datetime.now(dt.timezone.utc)
    ts = (real_now - dt.timedelta(hours=1)).isoformat()
    day = real_now.astimezone(cfg.market_tz).date().isoformat()
    _seed(d, ts=ts); _assess(cfg, d, now=real_now)
    cl = TestClient(create_app(cfg, d))
    r = cl.post(f"/api/v1/briefs/generate?date={day}")
    assert r.status_code == 200
    data = r.json()["data"]
    assert data["status"] in ("success", "partial")
    assert data["brief"]["brief_id"]
    # 最小间隔保护（同一日期立刻再请求应被挡）
    r2 = cl.post(f"/api/v1/briefs/generate?date={day}")
    assert r2.status_code == 429


def test_api_history_query_filters(env):
    cfg, d = env
    _seed(d); _assess(cfg, d)
    bs.generate_brief(cfg, d, brief_date=DAY, now=NOW)
    cl = TestClient(create_app(cfg, d))
    r = cl.get(f"/api/v1/briefs?generation_status={bg.STATUS_TEMPLATE}")
    assert r.json()["data"]["total"] == 1
    r2 = cl.get("/api/v1/briefs?generation_status=ai_generated")
    assert r2.json()["data"]["total"] == 0
    r3 = cl.get("/api/v1/briefs?delivered=true")
    assert r3.json()["data"]["total"] == 0


def test_page_shows_sources_and_version(env):
    cfg, d = env
    _seed(d); _assess(cfg, d)
    bs.generate_brief(cfg, d, brief_date=DAY, now=NOW)
    html = TestClient(create_app(cfg, d)).get("/briefs").text
    assert "每日风险简报" in html
    assert "数据来源" in html
    assert "模板降级" in html
    assert "简报版本与生成方式" in html
    assert "系统不接入下单接口" in html
    assert "不适用不等于规则通过" in html
    assert "Prompt 版本" in html
    assert 'class="brief-change-list"' in html
    assert 'class="brief-change-row"' in html
    assert "overflow-wrap:anywhere" in html
    assert ACC not in html                 # 页面上也不出现完整账户号


def test_page_without_brief(env):
    cfg, d = env
    html = TestClient(create_app(cfg, d)).get("/briefs").text
    assert "还没有生成过任何简报" in html


# ---------------- 39. 现有链路回归 ----------------

def test_existing_risk_and_alerts_unaffected(env):
    """简报层不得改动风险评估与事件登记的任何行为。"""
    cfg, d = env
    _seed(d)
    a = _assess(cfg, d)
    before = (a["risk_level"], a["risk_score"], tuple(a["triggered_rules"]))
    bs.generate_brief(cfg, d, brief_date=DAY, now=NOW)
    after_row = d.latest_assessment(ACC)
    from app import risk_service
    after = risk_service.load_assessment(d, dict(after_row))
    assert (after["risk_level"], after["risk_score"],
            tuple(after["triggered_rules"])) == before
    rows, total = d.query_risk_events()
    assert total == 0          # 简报不创建任何风险事件


def test_scheduled_run_isolates_failures(env):
    """调度入口吞掉一切异常 —— AI 失败不能影响同步与告警。"""
    cfg, d = env
    _seed(d); _assess(cfg, d)        # 数据就绪，否则会先判 waiting 而到不了 build
    orig = bc.build

    def boom(*a, **k):
        raise RuntimeError("上下文构建炸了")

    bc.build = boom
    try:
        res = bs.run_scheduled(cfg, d, now=NOW)
    finally:
        bc.build = orig
    assert res["status"] == "failed"
    assert "上下文构建炸了" in str(res.get("error"))


def test_readiness_waits_then_degrades(env):
    """数据未就绪时有限等待，超过上限出 limited 简报，绝不无限等待。"""
    cfg, d = env
    # 没有任何快照 -> 未就绪
    r = bs.generate_brief(cfg, d, brief_date=DAY, now=NOW, wait_attempt=0)
    assert r["status"] == "waiting"
    assert r["brief_id"] is None if "brief_id" in r else True

    r2 = bs.generate_brief(cfg, d, brief_date=DAY, now=NOW,
                           wait_attempt=bs.MAX_WAIT_ATTEMPTS)
    assert r2["status"] in ("success", "partial")
    row = d.get_brief(r2["brief_id"])
    assert row["generation_mode"] == bc.GATE_BLOCKED     # 没数据 -> blocked


def test_readiness_check_shape(env):
    cfg, d = env
    _seed(d); _assess(cfg, d)
    rd = bs.check_readiness(cfg, d, brief_date=DAY, now=NOW)
    names = {c["item"] for c in rd["checks"]}
    assert {"risk_assessment", "account_snapshot", "market_assessment",
            "flex_report"} <= names
    # 市场与 Flex 不是必需项 —— 它们缺失只降级不阻塞
    opt = {c["item"] for c in rd["checks"] if not c["required"]}
    assert {"market_assessment", "flex_report"} <= opt


# ---------------- 40. 飞书短版 ----------------

def test_feishu_body_is_short_and_safe(env):
    cfg, d = env
    _seed(d); _assess(cfg, d)
    ctx = _ctx(cfg, d)
    content = bg.template(ctx)
    title, body = bs.feishu_body(content, status=bg.STATUS_TEMPLATE,
                                 brief_id="b1", site="https://example.invalid")
    assert len(body) < len(bg.render_markdown(content,
                                              status=bg.STATUS_TEMPLATE))
    assert "今日检查" in body
    assert "数据限制" in body
    assert "/briefs" in body
    assert bp.DISCLAIMER in body
    assert ACC not in body


def test_identifier_fields_are_not_number_checked(env):
    """版本串与哈希里的数字不是指标 —— 生产实测 rule_version 的哈希片段
    曾被判成编造的数字，导致整份合法简报被拒发。"""
    cfg, d = env
    _seed(d); _assess(cfg, d)
    ctx = _ctx(cfg, d)
    content = bg.template(ctx)
    content["top_risks"] = [{
        "title": "T", "explanation": "e", "recommended_check": "检查 x",
        "rule_version": "risk-v2-conc/2026-07~797fe2b69126ff3b",
        "rule_code": None}]
    r = bv.validate(content, ctx)
    assert not any("疑似编造" in e for e in r["errors"]), r["errors"]
    # 但正文里的编造金额仍然被拦下
    content["account_summary"]["narrative"] = "净资产 987654.32。"
    r2 = bv.validate(content, ctx)
    assert any("987654.32" in e for e in r2["errors"])


def test_long_decimals_are_displayed_readably(env):
    """上下文保留全精度（对账要用），**展示必须收敛到两位** ——
    "总杠杆 0.2688448839480364478078871397" 没人读得下去。"""
    assert bg._fmt("0.2688448839480364478078871397") == "0.27"
    assert bg._fmt("0.2558079775497202270428207636") == "0.26"
    assert bg._fmt("43970.82") == "43970.82"          # 短小数原样
    assert bg._fmt(None) == "无数据"
    assert bg._display("3.909182229493349448016453679") == "3.91"
    assert bg._display(None) is None


def test_two_decimal_display_rounding_is_traceable(env):
    """P0-2 回归：展示层把长小数收敛到两位是合法的，校验器不得判为「编造」。

    2026-08-06 起展示口径从四位改两位（commit b695c1c），但数字溯源校验只登记
    四位规范形式，导致 1001.088334 → 1001.09 的正常舍入被判编造，简报连续多日
    validation failed / push skipped。修复方式是放宽 provenance 登记，不回退展示。
    """
    ctx = {"performance": {"realized_pnl": "1001.088334"}}
    # 合法的两位展示：放行
    ok, _ = bv._check_numbers({"x": "已实现盈亏 1001.09"}, ctx)
    assert ok == [], ok
    # 比例的两位展示同样放行：上下文存 0.3144，简报写 31.44%
    ok2, _ = bv._check_numbers({"x": "占比 31.44%"}, {"r": "0.3144"})
    assert ok2 == [], ok2
    # 但真正编造 / 重算的数字仍必须被拦
    bad, _ = bv._check_numbers({"x": "净资产 987654.32"}, ctx)
    assert bad, "编造数字必须被拦"
    bad2, _ = bv._check_numbers({"x": "翻倍 2002.18"}, ctx)
    assert bad2, "重算数字必须被拦"


def test_nonfinite_context_numbers_do_not_crash_traceability_check():
    """生产回归：NaN/Infinity 不能让 numbers_traceable 校验器自身崩溃。"""
    ctx = {"good": "1200", "bad_nan": "NaN", "bad_inf": "Infinity"}
    errs, _ = bv._check_numbers({"x": "净资产 1200"}, ctx)
    assert errs == [], errs
    assert bc._d("NaN") is None
    assert bc._d("Infinity") is None


def test_nonfinite_numbers_in_brief_are_rejected_explicitly():
    """上下文异常值可跳过，但简报输出中的非有限数字必须明确判失败。"""
    for value in ("风险值 NaN", "风险值 Infinity", float("nan")):
        errs, _ = bv._check_numbers({"x": value}, {"good": "1200"})
        assert any("非法非有限数字" in e for e in errs), (value, errs)


def test_unquantizable_context_number_does_not_crash_traceability_check():
    """极大但有限的 Decimal 超出默认量化精度时也不能使校验器崩溃。"""
    errs, _ = bv._check_numbers(
        {"x": "净资产 1200"}, {"good": "1200", "huge": "1E+9999"})
    assert errs == [], errs


def test_change_lines_dedupe_and_truncate(env):
    """同一规则在同一标的上的多条登记要合并；超长列表要截断。

    生产实测一天 15 条新增挤成一整段，等于没有重点。
    """
    items = ([{"rule_code": "POS_LOSS", "affected": "MRVL", "title": "T",
               "severity": "warning", "actual_value": "0.98",
               "threshold_value": "0.60"}] * 3
             + [{"rule_code": f"R{i}", "affected": None, "title": f"T{i}",
                 "severity": "warning", "actual_value": None,
                 "threshold_value": None} for i in range(10)]
             + [{"rule_code": "CRIT", "affected": None, "title": "严重",
                 "severity": "critical", "actual_value": None,
                 "threshold_value": None}])
    lines = bg._lines(items)
    assert len([x for x in lines if "MRVL" in x]) == 1      # 三条合成一条
    assert len(lines) == bg._MAX_LINES_PER_BUCKET + 1       # +1 是「另有 N 起」
    assert "另有" in lines[-1]
    assert "严重" in lines[0]                                # critical 排最前


# ==================== P0.5 字段补齐与生产验收 ====================

#: 生产实测的真实市场指标结构（脱敏后的形状，数值为构造值）。
#: v1 曾假设指标在 `calculated_metrics.indices` 下 —— **那个键从不存在**，
#: 导致市场明细整片为空。这份 fixture 钉住真实路径，防止再次跑偏。
_REAL_MARKET_METRICS = {
    "qqq": {"bars": 260, "close": "480.25", "ma20": "492.10",
            "ma50": "509.60", "ma200": "470.00",
            "vs_ma20": "-0.0460", "vs_ma50": "-0.0576", "vs_ma200": "0.0218",
            "below_ma20": True, "below_ma50": True, "below_ma200": False,
            "return_20d": "-0.0312", "drawdown_from_20d_high": "-0.0521",
            "status": "ok", "insufficient_for": []},
    "sox": {"bars": 260, "close": "212.40", "ma20": "228.00",
            "ma50": "241.30", "ma200": "230.00",
            "vs_ma20": "-0.0684", "vs_ma50": "-0.1197", "vs_ma200": "-0.0765",
            "below_ma20": True, "below_ma50": True, "below_ma200": True,
            "return_20d": "-0.0910", "drawdown_from_20d_high": "-0.1102",
            "status": "ok", "insufficient_for": []},
    "spy": {"bars": 260, "close": "590.10", "below_ma20": False,
            "below_ma50": False, "below_ma200": False, "status": "ok"},
    "vol": {"vix_current": "18.29", "vix_change_1d": "0.84",
            "vix_change_5d": "2.11", "vix_percentile": "0.62",
            "vxn_current": "28.61", "vxn_change_1d": "1.20",
            "vxn_change_5d": "3.40", "vxn_percentile": "0.78",
            "both_rising": True, "vxn_above_vix": "10.32", "status": "ok"},
    "rates": {"current": "46.04", "change_1d": "0.31", "change_5d": "1.02",
              "change_20d": "2.15", "pct_change_5d": "0.0227", "status": "ok"},
    "sox_vs_qqq_20d": "-0.1329",
    "multi_index_weak": True,
    "rates_equity_note": "利率上行与股指走弱同现",
    "data": {"missing_required": False, "missing_list": [], "stale": False,
             "stale_list": [], "inconsistent": False,
             "instruments": {
                 s: {"data_as_of": "2026-07-28", "quality_status": "fresh",
                     "delayed": True, "close": c,
                     "daily_change_percent": ch, "name": s}
                 for s, c, ch in (("QQQ", "480.25", "-0.0121"),
                                  ("SOXX", "212.40", "-0.0233"),
                                  ("SPY", "590.10", "-0.0044"),
                                  ("US10Y", "46.04", "0.0068"),
                                  ("VIX", "18.29", "0.0481"),
                                  ("VXN", "28.61", "0.0437"))}},
    "breadth": {"available": False, "note": "宽度数据源未接入"},
}


def _seed_market(d, *, metrics=None, as_of="2026-07-29",
                 assessment_id="mkt-1", reliable=True):
    d.save_market_assessment({
        "assessment_id": assessment_id, "data_as_of": as_of,
        "market_state": "stressed", "market_risk_level": "high",
        "market_risk_score": "57.8", "reliable": reliable,
        "blocked_reason": None,
        "component_scores": {"trend_score": "70", "volatility_score": "55"},
        "calculated_metrics": (_REAL_MARKET_METRICS if metrics is None
                               else metrics),
        "triggered_rules": ["TREND_QQQ_BELOW_MA50"],
        "top_market_drivers": [], "quality_status": "fresh",
        "calculation_version": "market-v1/2026-07",
        "rule_set_version": "market-v1/2026-07", "rule_set_hash": "abc",
        "events": [], "created_at": NOW.isoformat()})


# ---------------- 1–4. 市场字段映射 ----------------

def test_real_market_structure_maps(env):
    """按生产真实结构取数，市场摘要不再整片为空。"""
    cfg, d = env
    _seed(d); _assess(cfg, d)
    _seed_market(d)
    ctx = _ctx(cfg, d)
    m = ctx["market"]
    assert m["map_version"] == "market-map-v2/2026-07"
    assert m["qqq"]["close"] == "480.25"
    assert m["qqq"]["vs_ma50"] == "-0.0576"
    assert m["qqq"]["trend"] == "跌破20 日、50 日均线 · 站上200 日均线"
    assert m["soxx"]["close"] == "212.40"
    assert m["sox_vs_qqq_20d"] == "-0.1329"
    assert m["vix"]["current"] == "18.29"
    assert m["vix"]["change_1d"] == "0.84"
    assert m["vix"]["change_5d"] == "2.11"
    assert m["vxn"]["current"] == "28.61"
    assert m["vxn"]["above_vix"] == "10.32"
    assert m["us10y"]["current"] == "46.04"
    assert m["us10y"]["change_5d"] == "1.02"
    # 每个指标都有数据时间与质量状态
    for k in ("qqq", "soxx", "vix", "vxn", "us10y"):
        assert m[k]["data_as_of"] == "2026-07-28"
        assert m[k]["quality_status"] == "fresh"

    content = bg.template(ctx)
    ms = content["market_summary"]
    assert ms["qqq_close"] == "480.25"
    assert ms["soxx_vs_qqq_20d"] == "-0.1329"
    assert ms["vix"] == "18.29" and ms["vxn"] == "28.61"
    assert ms["us10y"] == "46.04"
    assert ms["instrument_data_as_of"]["qqq"] == "2026-07-28"
    assert bv.validate(content, ctx)["valid"]


def test_legacy_market_structure_compatible(env):
    """旧结构（indices）仍能读出来，不因为换了映射就丢历史数据。"""
    cfg, d = env
    _seed(d); _assess(cfg, d)
    _seed_market(d, metrics={
        "indices": {"QQQ": {"close": "400", "below_ma50": True,
                            "below_ma20": True, "below_ma200": False}},
        "vol": {"vix_current": "20"}, "rates": {"current": "44"}})
    m = _ctx(cfg, d)["market"]
    assert m["qqq"]["close"] == "400"
    assert m["qqq"]["trend"] is not None
    assert m["vix"]["current"] == "20"
    assert m["us10y"]["current"] == "44"


def test_missing_qqq_returns_null_not_zero(env):
    cfg, d = env
    _seed(d); _assess(cfg, d)
    _seed_market(d, metrics={"vol": {"vix_current": "18"}})
    m = _ctx(cfg, d)["market"]
    assert m["qqq"]["close"] is None
    assert m["qqq"]["trend"] is None            # 缺失不写成「正常」
    assert m["qqq"]["available"] is False
    content = bg.template(_ctx(cfg, d))
    assert content["market_summary"]["qqq_close"] is None


def test_missing_vix_produces_limitation(env):
    cfg, d = env
    _seed(d); _assess(cfg, d)
    _seed_market(d, metrics={"qqq": {"close": "400", "below_ma20": False}})
    ctx = _ctx(cfg, d)
    assert ctx["market"]["vix"]["current"] is None
    lims = " ".join(ctx["data_limitations"])
    assert "VIX 指标缺失" in lims
    assert "缺失不等于 0" in lims


# ---------------- 5–6. 持仓敞口 ----------------

def test_positions_market_value_mapped(env):
    cfg, d = env
    _seed(d); _assess(cfg, d)
    acct = _ctx(cfg, d)["account"]
    assert acct["positions_market_value"] == "70000"      # 40000 + 30000
    assert acct["long_exposure"] == "70000"
    assert acct["gross_exposure"] == "70000"
    assert acct["exposure_basis"]
    content = bg.template(_ctx(cfg, d))
    assert content["account_summary"]["positions_market_value"] == "70000"
    assert content["account_summary"]["exposure_basis"]
    # 口径必须写在简报里
    assert "口径" in bg.render_markdown(content, status=bg.STATUS_TEMPLATE)


def test_net_value_vs_long_exposure_distinguished(env):
    """有空头时，净市值与多头敞口必须分开表述。"""
    cfg, d = env
    _seed(d, positions=[
        {"symbol": "AAA", "qty": D("100"), "avg_cost": D("50"),
         "market_value": D("60000"), "upl": D("0"), "price": D("600"),
         "price_quality": "DELAYED"},
        {"symbol": "SHORTY", "qty": D("-50"), "avg_cost": D("40"),
         "market_value": D("-20000"), "upl": D("0"), "price": D("400"),
         "price_quality": "DELAYED"}])
    _assess(cfg, d)
    acct = _ctx(cfg, d)["account"]
    assert acct["long_exposure"] == "60000"
    assert acct["short_exposure"] == "20000"
    assert acct["gross_exposure"] == "80000"     # 多空绝对值合计
    assert acct["net_exposure"] == "40000"       # 多空相抵
    assert "净市值" in acct["exposure_basis"]     # 有空头 -> 净值口径
    lims = " ".join(_ctx(cfg, d)["data_limitations"])
    assert "「持仓总市值」口径" in lims


def test_no_silent_derivation_from_nav_minus_cash(env):
    """持仓市值缺失时**不倒推**，如实说明。"""
    cfg, d = env
    _seed(d); _assess(cfg, d)
    ctx = _ctx(cfg, d)
    ctx["account"]["positions_market_value"] = None
    lims = bc._limitations(ctx["gate"], ctx["time_alignment"],
                           ctx["performance"], ctx["concentration"],
                           ctx["historical_comparison"], ctx["data_quality"],
                           ctx_account=ctx["account"], market=ctx["market"])
    assert any("未用 净资产减现金 倒推" in x for x in lims)


# ---------------- 7–8. 联合事件版本 ----------------

def test_combined_event_has_rule_version(env):
    from app import combined_events as ce
    rules = ce.default_rules()
    setv = ce.rule_set_version(rules)
    assert setv.startswith("combined-v1/2026-07~")
    for r in rules:
        rv = ce.rule_version(r)
        assert rv.startswith("combined-v1/2026-07~")
    # 阈值不变 -> 版本稳定
    assert ce.rule_version(rules[0]) == ce.rule_version(rules[0])
    # 阈值改变 -> 新版本
    import dataclasses
    changed = dataclasses.replace(rules[0], threshold=D("0.99"))
    assert ce.rule_version(changed) != ce.rule_version(rules[0])
    assert ce.rule_set_hash([changed] + rules[1:]) != ce.rule_set_hash(rules)


def test_combined_trigger_carries_versions(env):
    """联合事件登记时必须带上单条规则版本、规则集版本与两个评估 id。"""
    from app import combined_events as ce
    from app import event_service as es
    cfg, d = env
    _seed(d); _assess(cfg, d)
    _seed_market(d)
    account = {"assessment_id": "acct-1", "snapshot_id": "s1",
               "data_as_of": NOW.isoformat(), "quality_status": "fresh",
               "risk_level": "high",
               "calculated_metrics": {"account": {"gross_leverage": "3.0"},
                                      "concentration": {}, "positions": []}}
    market = {"assessment_id": "mkt-1", "market_state": "stressed",
              "market_risk_level": "high",
              "calculated_metrics": _REAL_MARKET_METRICS}
    res = es.process_assessments(cfg, d, None, account=account, market=market,
                                 now=NOW,
                                 execution_context=ec.test_context())
    rows, _ = d.query_risk_events(source_type="combined")
    for r in rows:
        assert r["rule_version"], f"{r['rule_code']} 缺 rule_version"
        assert r["rule_version"].startswith("combined-v1/")
        assert r["rule_set_version"].startswith("combined-v1/")
        assert r["rule_set_hash"]
        assert r["market_assessment_id"] == "mkt-1"
        assert r["calculation_version"] == ce.CALC_VERSION


def test_legacy_event_missing_version_is_marked_not_faked(env):
    """旧事件缺版本时标 legacy_missing_version，**不得伪造**。"""
    cfg, d = env
    _seed(d); _assess(cfg, d)
    now_iso = NOW.isoformat()
    d.create_risk_event({
        "event_id": "old-1", "identity": "id-1", "episode_seq": 1,
        "source_type": "combined", "event_code": "OLD", "rule_code": "OLD_RULE",
        "rule_version": None, "rule_set_version": None, "category": "combined",
        "severity": "high", "max_severity": "high", "status": "active",
        "title": "旧联合事件", "explanation": "x", "actual_value": "1",
        "threshold_value": "0.5", "unit": "ratio", "affected_symbol": None,
        "affected_instrument": None, "account_id": ACC, "snapshot_id": "s",
        "assessment_id": "a", "data_as_of": now_iso,
        "first_detected_at": now_iso, "last_detected_at": now_iso,
        "occurrence_count": 1, "recommended_check": "检查",
        "quality_status": "fresh", "reliable": 1, "notification_owner": "ops",
        "created_at": now_iso, "updated_at": now_iso})
    ctx = _ctx(cfg, d)
    items = [x for x in ctx["risk_changes"]["ordered"]
             if x["rule_code"] == "OLD_RULE"]
    assert items, "旧事件应出现在变化分类里"
    assert items[0]["rule_version"] == bc.LEGACY_MISSING_VERSION
    assert "legacy" in bc.LEGACY_MISSING_VERSION


# ---------------- 9–10. 测试发送隔离 ----------------

def test_test_send_does_not_consume_official_dedupe(env):
    """链路测试发送**绝不能**让明早的正式推送被跳过。"""
    cfg, d = env
    _seed(d); _assess(cfg, d)
    res = bs.generate_brief(cfg, d, brief_date=DAY, now=NOW)
    bid = res["brief_id"]
    n = FakeNotifier()

    t = bs.deliver_test(cfg, d, bid, notifier=n, now=NOW)
    assert t["status"] == "test_success"
    assert len(n.sent) == 1
    row = d.get_brief(bid)
    assert row["delivered_at"] is None          # 正式仍未发送
    ch = row["context_hash"]
    assert d.brief_delivered(context_hash=ch, channel=bs.CHANNEL_FEISHU) is False
    assert d.brief_delivered(context_hash=ch, channel=bs.CHANNEL_TEST) is False

    # 正式推送照常进行，不被测试挡住
    official = bs.deliver_brief(cfg, d, bid, notifier=n, now=NOW)
    assert official["status"] == "sent"
    assert len(n.sent) == 2
    assert d.get_brief(bid)["delivered_at"] is not None


def test_test_send_is_clearly_marked(env):
    cfg, d = env
    _seed(d); _assess(cfg, d)
    res = bs.generate_brief(cfg, d, brief_date=DAY, now=NOW)
    n = FakeNotifier()
    bs.deliver_test(cfg, d, res["brief_id"], notifier=n, now=NOW)
    title, body = n.sent[0]
    assert title.startswith("【测试】")
    assert body.startswith("这是每日风险简报推送链路测试，"
                           "不代表账户刚刚出现新的风险。")
    assert ACC not in body


# ---------------- 11–15. 调度 ----------------

def test_scheduled_uses_local_timezone(env):
    cfg, d = env
    _seed(d); _assess(cfg, d)
    res = bs.run_scheduled(cfg, d, now=NOW)
    ex = d.schedule_executions(limit=1)[0]
    assert ex["scheduled_for"].endswith("+08:00")        # Asia/Shanghai
    assert f"T{cfg.brief_hour_local:02d}:{cfg.brief_minute_local:02d}" \
        in ex["scheduled_for"]
    # brief_date 用美股交易日，不是本地自然日
    assert ex["brief_date"] == str(
        NOW.astimezone(cfg.market_tz).date())


def test_scheduled_waits_then_degrades(env):
    """数据未就绪：有限等待 -> 超上限出 limited/blocked，绝不无限等待。"""
    cfg, d = env
    r1 = bs.run_scheduled(cfg, d, now=NOW, wait_attempt=0)
    assert r1["status"] == "waiting"
    ex = d.open_schedule_execution(str(NOW.astimezone(cfg.market_tz).date()))
    assert ex is not None and ex["completed_at"] is None
    assert ex["dependency_ready_status"] == "not_ready"

    r2 = bs.run_scheduled(cfg, d, now=NOW, wait_attempt=bs.MAX_WAIT_ATTEMPTS)
    assert r2["status"] in ("success", "partial")
    # 同一条执行记录被续写，不新开
    assert r2["execution_id"] == r1["execution_id"]
    done = d.schedule_executions(limit=1)[0]
    assert done["completed_at"] is not None
    assert done["dependency_wait_count"] == bs.MAX_WAIT_ATTEMPTS


def test_limited_brief_can_still_be_delivered(env):
    cfg, d = env
    _seed(d, ts=(NOW - dt.timedelta(hours=10)).isoformat())
    _assess(cfg, d)
    n = FakeNotifier()
    res = bs.generate_brief(cfg, d, brief_date=DAY,
                            trigger=bs.TRIGGER_SCHEDULED, notifier=n, now=NOW)
    assert res["generation_mode"] == bc.GATE_LIMITED
    assert res["delivery"]["status"] == "sent"


def test_manual_revision_does_not_block_scheduled(env):
    """同日已有手动 revision，不得让 scheduled 被误判为「已发过」而跳过。"""
    cfg, d = env
    _seed(d); _assess(cfg, d)
    manual = bs.generate_brief(cfg, d, brief_date=DAY,
                               trigger=bs.TRIGGER_MANUAL, now=NOW)
    assert manual["status"] == "success"
    n = FakeNotifier()
    sched = bs.run_scheduled(cfg, d, notifier=n, now=NOW)
    # 上下文没变 -> 复用同一份简报，但**推送照常发生**
    assert sched["status"] == "success"
    assert sched["delivery"]["status"] == "sent"
    assert len(n.sent) == 1


def test_scheduled_does_not_push_twice(env):
    cfg, d = env
    _seed(d); _assess(cfg, d)
    n = FakeNotifier()
    a = bs.run_scheduled(cfg, d, notifier=n, now=NOW)
    b = bs.run_scheduled(cfg, d, notifier=n, now=NOW + dt.timedelta(minutes=20))
    assert a["delivery"]["status"] == "sent"
    assert b["delivery"]["status"] == "skipped"
    assert len(n.sent) == 1


# ---------------- 16–17. 隔离 ----------------

def test_feishu_failure_does_not_break_scheduled_audit(env):
    cfg, d = env
    _seed(d); _assess(cfg, d)
    n = FakeNotifier(boom=True)
    res = bs.run_scheduled(cfg, d, notifier=n, now=NOW)
    assert res["status"] == "success"
    assert d.get_brief(res["brief_id"]) is not None
    ex = d.schedule_executions(limit=1)[0]
    assert ex["delivery_status"] == "failed"
    assert ex["completed_at"] is not None            # 审计照样收尾


def test_brief_failure_does_not_stop_sync(env):
    """简报炸了，账户同步与风险评估必须毫发无伤。"""
    cfg, d = env
    _seed(d)
    before = _assess(cfg, d)
    orig = bc.build
    bc.build = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("炸"))
    try:
        res = bs.run_scheduled(cfg, d, now=NOW)
    finally:
        bc.build = orig
    assert res["status"] == "failed"
    # 再跑一次评估，链路完好
    after = _assess(cfg, d, now=NOW + dt.timedelta(minutes=1))
    assert after["risk_level"] == before["risk_level"]
    ex = d.schedule_executions(limit=1)[0]
    assert ex["error_message"]
    assert ex["completed_at"] is not None


def test_context_hash_is_stable_across_time(env):
    """同一份数据在不同时刻必须产生同一个指纹。

    指纹漂移会同时打掉幂等与推送去重 —— 06:30 与 06:50 重试各出一份简报，
    用户收到两条推送。生产实测踩到过两次来源：
    time_alignment.generated_at，以及门禁 check 里的「N 小时前」描述。
    """
    cfg, d = env
    _seed(d); _assess(cfg, d)
    h = [bc.build(cfg, d, brief_date=DAY,
                  now=NOW + dt.timedelta(minutes=m))["context_hash"]
         for m in (0, 20, 90)]
    assert len(set(h)) == 1, f"指纹漂移：{h}"
    # 数据变了指纹必须变
    _seed(d, ts=(NOW - dt.timedelta(minutes=5)).isoformat(), equity="123456")
    _assess(cfg, d, now=NOW + dt.timedelta(minutes=1))
    h2 = bc.build(cfg, d, brief_date=DAY,
                  now=NOW + dt.timedelta(minutes=2))["context_hash"]
    assert h2 != h[0]
