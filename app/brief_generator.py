"""简报生成：AI Provider 抽象层 + **确定性模板降级**。

两件事必须同时成立：
  1. 有 AI 时，简报读起来像人写的
  2. **没有 AI 时，简报照样出得来** —— AI 服务不可用绝不能导致当天没有简报，
     更不能影响账户同步和实时告警

所以模板降级不是"兜底的残次品"，而是与 AI 版本共享同一份上下文、同一套
Schema 的**一等公民**。生产环境当前没有配置 API Key，走的就是模板路径。

状态标记三态：
    ai_generated        AI 生成并通过校验
    template_fallback   AI 不可用或校验失败，用确定性模板
    blocked_data_brief  门禁 blocked，只出数据状态简报
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any, Protocol

from . import brief_context as bc
from . import brief_prompt as bp

log = logging.getLogger("brief.generator")

STATUS_AI = "ai_generated"
STATUS_TEMPLATE = "template_fallback"
STATUS_BLOCKED = "blocked_data_brief"

#: AI 校验失败后的重试次数。超过就降级模板 —— 无限重试只会烧钱且拖垮生成窗口。
MAX_AI_ATTEMPTS = 2

DEFAULT_MODEL = "gpt-5"
DEFAULT_BASE_URL = "https://api.openai.com/v1"


class BriefProvider(Protocol):
    """AI provider 接口。抽象出来是为了让测试不需要网络，
    也让「没有 provider」成为一个正常状态而不是异常。"""

    name: str
    model: str

    def generate(self, *, system: str, messages: list[dict],
                 schema: dict) -> str: ...


class OpenAICompatibleProvider:
    """OpenAI-compatible Chat Completions provider.

    The implementation uses the existing ``httpx`` dependency and keeps the
    endpoint configurable, so self-hosted compatible gateways can be used.
    """

    name = "openai_compatible"

    def __init__(self, *, api_key: str | None = None,
                 model: str = DEFAULT_MODEL, max_tokens: int = 16000,
                 timeout: float = 120.0,
                 base_url: str = DEFAULT_BASE_URL):
        self.model = model
        self._api_key = api_key
        self._max_tokens = max_tokens
        self._timeout = timeout
        self._base_url = base_url.rstrip("/")

    def generate(self, *, system: str, messages: list[dict],
                 schema: dict) -> str:
        import httpx

        headers = {"Authorization": f"Bearer {self._api_key}",
                   "Content-Type": "application/json"}
        payload = {
            "model": self.model,
            "max_tokens": self._max_tokens,
            "messages": [{"role": "system", "content": system}, *messages],
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": "daily_risk_brief",
                    "strict": True,
                    "schema": schema,
                },
            },
        }
        with httpx.Client(timeout=self._timeout) as client:
            resp = client.post(f"{self._base_url}/chat/completions",
                               headers=headers, json=payload)
            resp.raise_for_status()
        data = resp.json()
        choices = data.get("choices") or []
        if not choices:
            raise RuntimeError("模型返回中没有候选内容")
        message = choices[0].get("message") or {}
        if message.get("refusal"):
            raise RuntimeError("模型拒绝生成")
        content = message.get("content")
        if isinstance(content, str) and content.strip():
            return content
        raise RuntimeError("模型返回中没有文本内容")


def build_provider(cfg) -> BriefProvider | None:
    """按配置构造 provider。没配 Key 就返回 None —— **这不是错误**，
    只是意味着走模板路径。"""
    key = getattr(cfg, "ai_api_key", None)
    if not key:
        return None
    return OpenAICompatibleProvider(
        api_key=key,
        model=getattr(cfg, "ai_model", None) or DEFAULT_MODEL,
        max_tokens=int(getattr(cfg, "ai_max_tokens", 16000) or 16000),
        timeout=float(getattr(cfg, "ai_timeout_seconds", 120) or 120),
        base_url=getattr(cfg, "ai_base_url", None) or DEFAULT_BASE_URL)


# ======================= 生成入口 =======================

def generate(context: dict, *, provider: BriefProvider | None,
             validator=None) -> dict:
    """产出简报内容。返回 {content, status, attempts, validation, model, errors}。

    blocked 模式**不调用 AI** —— 核心数据都不可用时，让模型去组织语言只会
    增加它编出风险结论的机会。直接用确定性模板写数据状态简报。
    """
    from . import brief_validator as bv

    validator = validator or bv.validate
    mode = context.get("generation_mode")

    if mode == bc.GATE_BLOCKED:
        content = blocked_template(context)
        result = validator(content, context)
        return {"content": content, "status": STATUS_BLOCKED, "attempts": 0,
                "validation": result, "model": None, "errors": []}

    errors: list[dict] = []
    if provider is not None:
        system = bp.SYSTEM_PROMPT
        messages = bp.build_messages(context)
        schema = bp.output_schema()
        for attempt in range(1, MAX_AI_ATTEMPTS + 1):
            try:
                raw = provider.generate(system=system, messages=messages,
                                        schema=schema)
                content = _parse_json(raw)
            except Exception as exc:                       # noqa: BLE001
                errors.append({"attempt": attempt, "stage": "call",
                               "error": _sanitize(str(exc))})
                log.warning("AI 简报生成失败 attempt=%d: %s", attempt,
                            _sanitize(str(exc)))
                continue

            result = validator(content, context)
            if result["valid"]:
                return {"content": content, "status": STATUS_AI,
                        "attempts": attempt, "validation": result,
                        "model": provider.model, "errors": errors}
            errors.append({"attempt": attempt, "stage": "validation",
                           "error": result["errors"]})
            log.warning("AI 简报校验未通过 attempt=%d: %s", attempt,
                        result["errors"][:3])
            # 把违规原因回灌，给模型一次改正机会
            messages = messages + [
                {"role": "assistant",
                 "content": json.dumps(content, ensure_ascii=False)},
                {"role": "user",
                 "content": ("上一份输出未通过校验，问题如下：\n"
                             + "\n".join(f"- {e}" for e in result["errors"][:10])
                             + "\n请修正后重新输出完整 JSON。")},
            ]

    # ---- 降级：确定性模板 ----
    content = template(context)
    result = validator(content, context)
    return {"content": content, "status": STATUS_TEMPLATE,
            "attempts": len(errors), "validation": result,
            "model": None, "errors": errors}


def _parse_json(raw: str) -> dict:
    """容忍模型偶尔包一层 markdown 代码块。"""
    txt = (raw or "").strip()
    if txt.startswith("```"):
        txt = re.sub(r"^```(?:json)?\s*", "", txt)
        txt = re.sub(r"\s*```$", "", txt)
    return json.loads(txt)


_SECRET_RE = re.compile(
    r"(sk-[A-Za-z0-9_\-]{8,}|Bearer\s+\S+|[A-Za-z0-9]{32,})")


def _sanitize(msg: str) -> str:
    """错误信息里可能带上 Key 片段。**绝不让它进日志或数据库。**"""
    return _SECRET_RE.sub("***", str(msg))[:500]


# ======================= 确定性模板 =======================

#: 展示精度。上下文里保留全精度（对账要用），**展示时必须收敛** ——
#: "总杠杆 0.2688448839480364478078871397" 没人读得下去。
_DISPLAY_Q = bc.Decimal("0.01")


def _fmt(v, unit: str = "") -> str:
    """None 一律显示「无数据」。**绝不显示 0** —— 那是两件不同的事。"""
    if v is None or v == "" or v == "None":
        return "无数据"
    d = bc._d(v)
    if d is not None and -d.as_tuple().exponent > 2:
        return f"{d.quantize(_DISPLAY_Q).normalize():f}{unit}"
    return f"{v}{unit}"


def _pct(v) -> str:
    d = bc._d(v)
    if d is None:
        return "无数据"
    return f"{d * 100:.2f}%"


def _display(v):
    """展示用取值：长小数收敛到 2 位，其余原样。None 保持 None。"""
    if v is None:
        return None
    d = bc._d(v)
    if d is not None and -d.as_tuple().exponent > 2:
        return format(d.quantize(_DISPLAY_Q).normalize(), "f")
    return v if isinstance(v, str) else str(v)


def template(ctx: dict) -> dict:
    """确定性模板简报。直接使用结构化数据，不经过任何模型。

    行文刻意平实 —— 它的价值是**永远可用且永远正确**，不是好看。
    """
    acct = ctx.get("account") or {}
    mkt = ctx.get("market") or {}
    changes = ctx.get("risk_changes") or {}
    counts = changes.get("counts") or {}
    conc = ctx.get("concentration") or {}
    perf = ctx.get("performance") or {}
    hist = ctx.get("historical_comparison") or {}
    chain = ctx.get("hyperliquid") or {}
    mode = ctx.get("generation_mode")

    prev = hist.get("previous_day") or {}
    trend = "无可比基线"
    if prev.get("date"):
        delta = bc._d(prev.get("score_delta"))
        if delta is None:
            trend = f"与 {prev['date']} 相比风险分变化未知"
        elif delta > 0:
            trend = f"与 {prev['date']} 相比风险分上升 {delta}"
        elif delta < 0:
            trend = f"与 {prev['date']} 相比风险分下降 {abs(delta)}"
        else:
            trend = f"与 {prev['date']} 相比风险分持平"

    summary = [
        f"账户风险 {_fmt(acct.get('risk_level'))}"
        f"（评分 {_fmt(acct.get('risk_score'))}），"
        f"市场环境 {_fmt(mkt.get('market_state'))}"
        f"（{_fmt(mkt.get('market_risk_level'))}）。",
        f"{trend}。今日新增 {counts.get('new', 0)} 起、"
        f"升级 {counts.get('escalated', 0)} 起、"
        f"恢复 {counts.get('resolved', 0)} 起，"
        f"另有 {counts.get('persistent', 0)} 起持续存在。",
    ]
    if mode == bc.GATE_LIMITED:
        summary.append("本次为受限简报：部分数据过期或仍为估算值，"
                       "不给出确定性的风险升级结论。")

    top = []
    for t in (ctx.get("top_risks") or [])[:5]:
        expl = t.get("explanation_facts") or f"{t.get('rule_code')} 触发"
        if t.get("in_observation_period"):
            expl += f"（{bc.OBSERVATION_NOTE}）"
        top.append({
            "title": t.get("title") or t.get("rule_code") or "未命名风险",
            "severity": t.get("severity"),
            "actual_value": _display(t.get("actual_value")),
            "threshold": _display(t.get("threshold")),
            "affected_object": t.get("affected_object"),
            "explanation": expl,
            "recommended_check": (t.get("recommended_check")
                                  or "检查该项指标的原始数据与规则阈值是否仍合理"),
            "rule_code": t.get("rule_code"),
            "rule_version": t.get("rule_version"),
            "observation_period_note": (bc.OBSERVATION_NOTE
                                        if t.get("in_observation_period") else None),
        })

    na_rules = [f"{x['rule_code']}：{x['reason']}"
                for x in (conc.get("not_applicable_rules") or [])]
    conc_narr = None
    if conc.get("available"):
        conc_narr = (
            f"名义持仓 {_fmt(conc.get('nominal_position_count'))} 个，"
            f"有效多头 {_fmt(conc.get('eligible_long_position_count'))} 个，"
            f"HHI {_fmt(conc.get('hhi'))}，"
            f"等效持仓数 {_fmt(conc.get('effective_position_count'))}，"
            f"最大单仓占已投资 {_pct(conc.get('largest_position_weight'))}。")
        if na_rules:
            conc_narr += ("当前持仓数量不足以使 Top3/Top5 指标具有区分能力，"
                          "系统改用 HHI、等效持仓数量和单仓主导度判断集中风险。"
                          "**不适用不等于规则通过。**")
        if conc.get("observation_period_rules"):
            conc_narr += bc.OBSERVATION_NOTE

    perf_narr = None
    if perf:
        perf_narr = (
            f"区间 {perf.get('period_start')} ~ {perf.get('period_end')}："
            f"净值变化 {_fmt(perf.get('raw_nav_change'))}，"
            f"其中外部资金进出 {_fmt(perf.get('external_cash_flow'))}，"
            f"剔除资金进出后的收益 {_fmt(perf.get('cash_flow_adjusted_pnl'))}。"
            f"已实现盈亏 {_fmt(perf.get('realized_pnl'))}，"
            f"未实现盈亏变化 {_fmt(perf.get('unrealized_pnl_change'))}，"
            f"未解释差额 {_fmt(perf.get('unexplained_difference'))}。"
            f"数据质量 {_fmt(perf.get('data_quality'))}。"
            "入金不是盈利，出金不是亏损。")

    checks = _checks(ctx, top)

    return {
        "brief_version": bc.BRIEF_VERSION,
        "brief_date": ctx.get("brief_date"),
        "generation_mode": mode,
        "headline": (f"{ctx.get('brief_date')} 账户风险 "
                     f"{_fmt(acct.get('risk_level'))} · 市场 "
                     f"{_fmt(mkt.get('market_state'))}"),
        "executive_summary": summary[:3],
        "account_summary": {
            "risk_level": acct.get("risk_level"),
            "risk_score": acct.get("risk_score"),
            "net_liquidation": acct.get("net_liquidation"),
            "total_cash": acct.get("total_cash"),
            "cash_ratio": acct.get("cash_ratio"),
            "positions_market_value": acct.get("positions_market_value"),
            "exposure_basis": acct.get("exposure_basis"),
            "long_exposure": acct.get("long_exposure"),
            "short_exposure": acct.get("short_exposure"),
            "gross_exposure": acct.get("gross_exposure"),
            "net_exposure": acct.get("net_exposure"),
            "gross_leverage": acct.get("gross_leverage"),
            "current_drawdown": acct.get("current_drawdown"),
            "data_as_of": acct.get("data_as_of"),
            "narrative": (f"净资产 {_fmt(acct.get('net_liquidation'))}，"
                          f"持仓总市值 {_fmt(acct.get('positions_market_value'))}"
                          f"（{acct.get('exposure_basis') or '口径未标注'}），"
                          f"现金比例 {_pct(acct.get('cash_ratio'))}，"
                          f"总杠杆 {_fmt(acct.get('gross_leverage'))}，"
                          f"当前回撤 {_pct(acct.get('current_drawdown'))}。"),
        },
        "market_summary": {
            "market_state": mkt.get("market_state"),
            "risk_level": mkt.get("market_risk_level"),
            "risk_score": mkt.get("market_risk_score"),
            "qqq_close": (mkt.get("qqq") or {}).get("close"),
            "qqq_trend": (mkt.get("qqq") or {}).get("trend"),
            "soxx_close": (mkt.get("soxx") or {}).get("close"),
            "soxx_trend": (mkt.get("soxx") or {}).get("trend"),
            "soxx_vs_qqq_20d": mkt.get("sox_vs_qqq_20d"),
            "vix": (mkt.get("vix") or {}).get("current"),
            "vix_change_1d": (mkt.get("vix") or {}).get("change_1d"),
            "vix_change_5d": (mkt.get("vix") or {}).get("change_5d"),
            "vxn": (mkt.get("vxn") or {}).get("current"),
            "vxn_change_1d": (mkt.get("vxn") or {}).get("change_1d"),
            "vxn_above_vix": (mkt.get("vxn") or {}).get("above_vix"),
            "us10y": (mkt.get("us10y") or {}).get("current"),
            "us10y_change_5d": (mkt.get("us10y") or {}).get("change_5d"),
            "instrument_data_as_of": {
                k: (mkt.get(k) or {}).get("data_as_of")
                for k in ("qqq", "soxx", "vix", "vxn", "us10y")},
            "data_as_of": mkt.get("data_as_of"),
            "top_drivers": [str(d.get("title") or d.get("rule_code") or d)
                            if isinstance(d, dict) else str(d)
                            for d in (mkt.get("top_drivers") or [])][:5],
            "narrative": (f"市场状态 {_fmt(mkt.get('market_state'))}，"
                          f"风险等级 {_fmt(mkt.get('market_risk_level'))}。"
                          f"QQQ {_fmt((mkt.get('qqq') or {}).get('close'))}"
                          f"（{(mkt.get('qqq') or {}).get('trend') or '趋势数据缺失'}），"
                          f"SOXX 相对 QQQ 20 日 "
                          f"{_fmt(mkt.get('sox_vs_qqq_20d'))}。"
                          f"VIX {_fmt((mkt.get('vix') or {}).get('current'))}"
                          f"（1 日变化 {_fmt((mkt.get('vix') or {}).get('change_1d'))}），"
                          f"VXN 高出 VIX {_fmt((mkt.get('vxn') or {}).get('above_vix'))}，"
                          f"十年期美债 {_fmt((mkt.get('us10y') or {}).get('current'))}"
                          f"（5 日变化 {_fmt((mkt.get('us10y') or {}).get('change_5d'))}）。"
                          f"{mkt.get('delay_note') or ''}"
                          "以上仅描述当前市场环境，不预测未来价格。"),
        },
        "chain_summary": {
            "status": chain.get("status"), "note": chain.get("note"),
            "signals": [
                {"symbol": s.get("symbol"), "model": s.get("model"),
                 "state": s.get("state"), "score": s.get("score"),
                 "confidence": s.get("confidence"), "signal_at": s.get("signal_at"),
                 "actionable_at": s.get("actionable_at"),
                 "quality_status": s.get("quality_status")}
                for s in (chain.get("signals") or [])[:10]
            ],
        },
        "risk_changes": {
            k: _lines(changes.get(k) or [])
            for k in ("new", "escalated", "persistent", "resolved",
                      "acknowledged", "suppressed")
        },
        "top_risks": top,
        "concentration": {
            "nominal_position_count": conc.get("nominal_position_count"),
            "eligible_long_position_count": conc.get("eligible_long_position_count"),
            "hhi": conc.get("hhi"),
            "effective_position_count": conc.get("effective_position_count"),
            "largest_position_weight": conc.get("largest_position_weight"),
            "single_position_dominance": conc.get("single_position_dominance"),
            "not_applicable_rules": na_rules,
            "narrative": conc_narr,
        },
        "performance": {
            "raw_nav_change": perf.get("raw_nav_change"),
            "cash_flow_adjusted_pnl": perf.get("cash_flow_adjusted_pnl"),
            "realized_pnl": perf.get("realized_pnl"),
            "unrealized_pnl_change": perf.get("unrealized_pnl_change"),
            "external_cash_flow": perf.get("external_cash_flow"),
            "dividends": perf.get("dividends"), "interest": perf.get("interest"),
            "commissions": perf.get("commissions"), "taxes": perf.get("taxes"),
            "unexplained_difference": perf.get("unexplained_difference"),
            "modified_dietz_return": perf.get("modified_dietz_return"),
            "twr_return": perf.get("twr_return"),
            "data_quality": perf.get("data_quality"),
            "narrative": perf_narr,
            "limitations": list(perf.get("limitations") or []),
        },
        "historical_comparison": _hist_block(hist),
        "checks": checks,
        "data_limitations": list(ctx.get("data_limitations") or []),
    }


#: 单个变化类别最多列几条。**简报的价值在于突出变化，不是穷举。**
#: 生产实测一天 15 条新增挤成一整段，等于没有重点。
_MAX_LINES_PER_BUCKET = 6


def _lines(items: list[dict]) -> list[str]:
    """把事件渲染成短句。

    先按 (规则, 对象) 去重 —— 同一条规则在同一标的上可能因多次评估
    留下多条登记，全列出来只是噪音。
    """
    seen: set = set()
    uniq: list[dict] = []
    for it in items:
        key = (it.get("rule_code"), it.get("affected"))
        if key in seen:
            continue
        seen.add(key)
        uniq.append(it)
    # 严重的排前面，截断时留下的才是最该看的
    uniq.sort(key=lambda x: -_sev_rank(x.get("severity")))
    extra = len(uniq) - _MAX_LINES_PER_BUCKET
    items = uniq[:_MAX_LINES_PER_BUCKET]

    out = []
    for it in items:
        if it.get("merge_note"):
            out.append(it["merge_note"])
            continue
        who = f"{it.get('affected')} " if it.get("affected") else ""
        val, thr = it.get("actual_value"), it.get("threshold_value")
        detail = (f"（当前 {_fmt(val)}，阈值 {_fmt(thr)}）"
                  if val is not None and thr is not None else "")
        out.append(f"{who}{it.get('title') or it.get('rule_code')}"
                   f"[{it.get('severity')}]{detail}")
    if extra > 0:
        out.append(f"（另有 {extra} 起同类事件，完整列表见风险事件页）")
    return out


def _sev_rank(sev) -> int:
    return {"critical": 3, "high": 2, "warning": 1}.get(str(sev or "").lower(), 0)


def _hist_block(hist: dict) -> dict:
    b7 = ((hist.get("baselines") or {}).get("7d") or {}).get("risk_score") or {}
    b30 = ((hist.get("baselines") or {}).get("30d") or {}).get("risk_score") or {}
    c7 = ((hist.get("comparisons") or {}).get("7d") or {}).get("risk_score") or {}
    prev = hist.get("previous_day") or {}

    def desc(base: dict, cmp_: dict) -> str | None:
        if not base.get("sufficient"):
            return base.get("note") or "历史样本不足，暂不能形成稳定基线"
        if not cmp_.get("comparable"):
            return cmp_.get("reason")
        return (f"中位数 {base.get('median')}（{base.get('sample_size')} 个样本），"
                f"当前{'高于' if cmp_['direction'] == 'above' else ('低于' if cmp_['direction'] == 'below' else '等于')}"
                f"基线 {cmp_.get('delta')}")

    prev_txt = None
    if prev.get("date"):
        prev_txt = (f"{prev['date']}：{prev.get('risk_level')} / "
                    f"{prev.get('risk_score')}"
                    + (f"，风险等级已变化" if prev.get("level_changed") else ""))
    elif prev.get("note"):
        prev_txt = prev["note"]

    return {
        "previous_day": prev_txt,
        "seven_day_baseline": desc(b7, c7),
        "thirty_day_baseline": (desc(b30, {}) if b30.get("sufficient")
                                else (b30.get("note") or "30 日历史样本不足，"
                                      "暂不能形成稳定基线")),
        "sample_size": hist.get("sample_size"),
        "narrative": None,
    }


def _checks(ctx: dict, top: list[dict]) -> list[str]:
    """今日需要处理的事项。措辞由上游规则层决定，这里不再强制加「检查」前缀。"""
    out: list[str] = []
    for t in top[:3]:
        c = t.get("recommended_check")
        if c:
            out.append(str(c))
    q = ctx.get("data_quality") or {}
    if q.get("issue_count"):
        out.append(f"检查数据同步：当前有 {q['issue_count']} 个数据质量问题待确认")
    changes = (ctx.get("risk_changes") or {}).get("counts") or {}
    if changes.get("new") or changes.get("escalated"):
        out.append("检查今日新增与升级的风险事件是否已经知晓")
    if not out:
        out.append("检查账户数据同步是否正常，本日无新增风险事件")
    return out[:5]


# ======================= blocked 模板 =======================

def blocked_template(ctx: dict) -> dict:
    """数据状态简报。

    **绝不输出任何账户风险结论** —— 包括「当前风险较低」。
    数据不可用时说"低风险"，比不出简报危险得多。
    """
    gate = ctx.get("gate") or {}
    acct = ctx.get("account") or {}
    reasons = gate.get("blocked_reasons") or ["核心数据不可用"]
    blocked = gate.get("blocked_analyses") or []
    last_sync = acct.get("data_as_of")

    summary = [
        "本日无法生成风险简报：" + "；".join(reasons) + "。",
        (f"最近一次成功的账户数据时间为 {last_sync}。" if last_sync
         else "没有任何可用的账户快照记录。"),
        "以下分析已被阻止：" + "、".join(blocked) + "。" if blocked else
        "所有账户风险分析均已被阻止。",
    ]
    checks = [
        "检查 IBKR Gateway 连接状态与同步任务是否正常运行",
        "检查系统状态页的数据质量问题列表",
        "检查最近一次同步失败的日志原因",
    ]
    if acct.get("blocked_reason"):
        checks.insert(0, f"检查风险评估被阻断的原因：{acct['blocked_reason']}")

    return {
        "brief_version": bc.BRIEF_VERSION,
        "brief_date": ctx.get("brief_date"),
        "generation_mode": bc.GATE_BLOCKED,
        "headline": f"{ctx.get('brief_date')} 数据状态简报 · 风险分析已阻止",
        "executive_summary": summary[:3],
        # 账户与市场区块**全部留空** —— 一个数字都不给，避免被误读成结论
        "account_summary": {"risk_level": None, "risk_score": None,
                            "net_liquidation": None, "total_cash": None,
                            "cash_ratio": None, "positions_market_value": None,
                            "exposure_basis": None, "long_exposure": None,
                            "short_exposure": None, "gross_exposure": None,
                            "net_exposure": None,
                            "gross_leverage": None, "current_drawdown": None,
                            "data_as_of": last_sync,
                            "narrative": "核心账户数据不可用，本次不给出任何风险结论。"},
        "market_summary": {"market_state": None, "risk_level": None,
                           "risk_score": None, "qqq_close": None,
                           "qqq_trend": None, "soxx_close": None,
                           "soxx_trend": None, "soxx_vs_qqq_20d": None,
                           "vix": None, "vix_change_1d": None,
                           "vix_change_5d": None, "vxn": None,
                           "vxn_change_1d": None, "vxn_above_vix": None,
                           "us10y": None, "us10y_change_5d": None,
                           "instrument_data_as_of": {},
                           "data_as_of": None, "top_drivers": [],
                           "narrative": "账户数据不可用时不单独输出市场结论。"},
        "chain_summary": {"status": "UNAVAILABLE", "note": "核心账户数据不可用，本节省略",
                          "signals": []},
        "risk_changes": {k: [] for k in ("new", "escalated", "persistent",
                                         "resolved", "acknowledged", "suppressed")},
        "top_risks": [],
        "concentration": {"nominal_position_count": None,
                          "eligible_long_position_count": None,
                          "hhi": None, "effective_position_count": None,
                          "largest_position_weight": None,
                          "single_position_dominance": None,
                          "not_applicable_rules": [],
                          "narrative": "数据不可用，未计算集中度。"},
        "performance": {k: None for k in
                        ("raw_nav_change", "cash_flow_adjusted_pnl", "realized_pnl",
                         "unrealized_pnl_change", "external_cash_flow", "dividends",
                         "interest", "commissions", "taxes",
                         "unexplained_difference", "modified_dietz_return",
                         "twr_return", "data_quality", "narrative")}
        | {"limitations": ["核心数据不可用，本次未计算收益归因"]},
        "historical_comparison": {"previous_day": None,
                                  "seven_day_baseline": None,
                                  "thirty_day_baseline": None,
                                  "sample_size": None,
                                  "narrative": "数据不可用，未做历史对比。"},
        "checks": checks[:5],
        "data_limitations": list(ctx.get("data_limitations") or []) or reasons,
    }


# ======================= Markdown 渲染 =======================

def render_markdown(content: dict, *, status: str) -> str:
    """网页与归档用的 Markdown。飞书用的是另一份更短的（见 brief_service）。"""
    L: list[str] = []
    badge = {STATUS_AI: "AI 生成", STATUS_TEMPLATE: "模板降级（未使用 AI）",
             STATUS_BLOCKED: "数据受阻简报"}.get(status, status)
    L.append(f"# {content.get('headline')}")
    L.append(f"*{badge} · 模式 {content.get('generation_mode')} · "
             f"{content.get('brief_version')}*")
    L.append("")
    L.append("## 今日结论")
    for s in content.get("executive_summary") or []:
        L.append(f"- {s}")

    a = content.get("account_summary") or {}
    L += ["", "## 账户概况",
          f"- 风险等级：{_fmt(a.get('risk_level'))}（评分 {_fmt(a.get('risk_score'))}）",
          f"- 净资产：{_fmt(a.get('net_liquidation'))}",
          f"- 总现金：{_fmt(a.get('total_cash'))} · 现金比例：{_pct(a.get('cash_ratio'))}",
          f"- 持仓总市值：{_fmt(a.get('positions_market_value'))}"
          f"（口径：{a.get('exposure_basis') or '未标注'}）",
          f"- 多头敞口：{_fmt(a.get('long_exposure'))} · "
          f"空头敞口：{_fmt(a.get('short_exposure'))}",
          f"- 总风险敞口：{_fmt(a.get('gross_exposure'))} · "
          f"净敞口：{_fmt(a.get('net_exposure'))}",
          f"- 总杠杆：{_fmt(a.get('gross_leverage'))} · 当前回撤：{_pct(a.get('current_drawdown'))}",
          f"- 数据截至：{_fmt(a.get('data_as_of'))}"]

    m = content.get("market_summary") or {}
    L += ["", "## 市场环境",
          f"- 状态：{_fmt(m.get('market_state'))} · 风险 {_fmt(m.get('risk_level'))}"
          f"（{_fmt(m.get('risk_score'))}）",
          f"- QQQ：{_fmt(m.get('qqq_close'))}（{_fmt(m.get('qqq_trend'))}）",
          f"- SOXX：{_fmt(m.get('soxx_close'))}（{_fmt(m.get('soxx_trend'))}）· "
          f"相对 QQQ 20 日：{_fmt(m.get('soxx_vs_qqq_20d'))}",
          f"- VIX：{_fmt(m.get('vix'))}（1 日 {_fmt(m.get('vix_change_1d'))} · "
          f"5 日 {_fmt(m.get('vix_change_5d'))}）",
          f"- VXN：{_fmt(m.get('vxn'))}（1 日 {_fmt(m.get('vxn_change_1d'))} · "
          f"高出 VIX {_fmt(m.get('vxn_above_vix'))}）",
          f"- 十年期美债：{_fmt(m.get('us10y'))}"
          f"（5 日变化 {_fmt(m.get('us10y_change_5d'))}）",
          f"- 数据截至：{_fmt(m.get('data_as_of'))}"]
    if m.get("narrative"):
        L.append(f"- {m['narrative']}")

    chain = content.get("chain_summary") or {}
    if str(chain.get("status") or "").upper() not in {"DISABLED", "UNAVAILABLE"} or chain.get("signals"):
        L += ["", "## 链上资金动向"]
        for signal in (chain.get("signals") or []):
            L.append(f"- {signal.get('symbol')} {signal.get('model')}："
                     f"{signal.get('state')}（评分 {_fmt(signal.get('score'))}，"
                     f"置信度 {_fmt(signal.get('confidence'))}，"
                     f"可执行时间 {_fmt(signal.get('actionable_at'))}）")
        if chain.get("note"):
            L.append(f"- {chain['note']}")

    ch = content.get("risk_changes") or {}
    L += ["", "## 风险变化"]
    for key, label in (("escalated", "严重度升级"), ("new", "今日新增"),
                       ("resolved", "今日恢复"), ("persistent", "持续存在"),
                       ("acknowledged", "已确认"), ("suppressed", "被抑制")):
        items = ch.get(key) or []
        L.append(f"- **{label}**（{len(items)}）："
                 + ("；".join(items) if items else "无"))

    L += ["", "## 最大风险来源"]
    if content.get("top_risks"):
        for t in content["top_risks"]:
            L.append(f"### {t.get('title')} [{_fmt(t.get('severity'))}]")
            L.append(f"- 当前值 {_fmt(t.get('actual_value'))} / "
                     f"阈值 {_fmt(t.get('threshold'))}"
                     + (f" · 影响对象 {t['affected_object']}"
                        if t.get("affected_object") else ""))
            L.append(f"- {t.get('explanation')}")
            L.append(f"- 建议检查：{t.get('recommended_check')}")
            L.append(f"- 规则 {_fmt(t.get('rule_code'))} · "
                     f"版本 {_fmt(t.get('rule_version'))}")
            if t.get("observation_period_note"):
                L.append(f"- ⚠️ {t['observation_period_note']}")
    else:
        L.append("- 无")

    c = content.get("concentration") or {}
    L += ["", "## 集中度",
          f"- 名义持仓 {_fmt(c.get('nominal_position_count'))} · "
          f"有效多头 {_fmt(c.get('eligible_long_position_count'))}",
          f"- HHI {_fmt(c.get('hhi'))} · 等效持仓数 "
          f"{_fmt(c.get('effective_position_count'))}",
          f"- 最大单仓权重 {_pct(c.get('largest_position_weight'))} · "
          f"单仓主导 {'是' if c.get('single_position_dominance') else '否'}"]
    for r in c.get("not_applicable_rules") or []:
        L.append(f"- ⚠️ 不适用：{r}")
    if c.get("narrative"):
        L.append(f"- {c['narrative']}")

    p = content.get("performance") or {}
    L += ["", "## 盈亏与收益归因",
          f"- 净值变化 {_fmt(p.get('raw_nav_change'))} · "
          f"外部资金进出 {_fmt(p.get('external_cash_flow'))}",
          f"- 剔除资金进出后的收益 {_fmt(p.get('cash_flow_adjusted_pnl'))}",
          f"- 已实现 {_fmt(p.get('realized_pnl'))} · "
          f"未实现变化 {_fmt(p.get('unrealized_pnl_change'))}",
          f"- 分红 {_fmt(p.get('dividends'))} · 利息 {_fmt(p.get('interest'))} · "
          f"佣金费用 {_fmt(p.get('commissions'))} · 税费 {_fmt(p.get('taxes'))}",
          f"- 未解释差额 {_fmt(p.get('unexplained_difference'))}",
          f"- Modified Dietz {_fmt(p.get('modified_dietz_return'))} · "
          f"TWR {_fmt(p.get('twr_return'))}",
          f"- 数据质量 {_fmt(p.get('data_quality'))}"]
    for lim in p.get("limitations") or []:
        L.append(f"- ⚠️ {lim}")

    h = content.get("historical_comparison") or {}
    L += ["", "## 历史基线对比",
          f"- 上一有效交易日：{_fmt(h.get('previous_day'))}",
          f"- 7 日基线：{_fmt(h.get('seven_day_baseline'))}",
          f"- 30 日基线：{_fmt(h.get('thirty_day_baseline'))}",
          f"- 样本量：{_fmt(h.get('sample_size'))}"]

    L += ["", "## 今日需要检查"]
    for c_ in content.get("checks") or []:
        L.append(f"- {c_}")

    L += ["", "## 数据限制"]
    for lim in content.get("data_limitations") or []:
        L.append(f"- {lim}")

    L += ["", "---", f"*{bp.DISCLAIMER}*"]
    return "\n".join(L)
