"""从已落库的结构化结果里取出简报需要的数字，并同时登记来源引用。

拆成独立模块的原因：`brief_context` 负责**判断与组织**（门禁、分类、基线），
这里负责**取数**。两件事混在一起时，"这个数字从哪来的" 会淹没在判断逻辑里。

铁律与 `brief_context` 一致：**这里不发明任何数字**。每个字段要么直接来自
某张表的某一列，要么是那些列的简单选取，并且都登记 source reference。
"""
from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal

from . import brief_context as bc

_d, _s, _dt = bc._d, bc._s, bc._dt


def _ref(refs: list, **kw):
    refs.append(bc.source_ref(**kw))


# ======================= 账户 =======================

def account_block(cfg, db, account_id: str | None, refs: list) -> dict | None:
    """账户风险评估。取的是**已落库的评估结果**，不重新计算。"""
    from . import risk_service

    try:
        row = db.latest_assessment(account_id)
    except Exception:  # noqa: BLE001
        row = None
    if row is None:
        return None
    a = risk_service.load_assessment(db, dict(row))
    if a is None:
        return None

    m = a.get("calculated_metrics") or {}
    acct, liq, dd = (m.get("account") or {}, m.get("liquidity") or {},
                     m.get("drawdown") or {})

    aid = a.get("assessment_id")
    ver = a.get("calculation_version")
    as_of = a.get("data_as_of")
    for path, val in (("risk_level", a.get("risk_level")),
                      ("risk_score", a.get("risk_score")),
                      ("net_liquidation", acct.get("net_liquidation")),
                      ("cash_ratio", acct.get("cash_ratio")),
                      ("positions_market_value", acct.get("positions_market_value")),
                      ("long_exposure", acct.get("long_exposure")),
                      ("short_exposure", acct.get("short_exposure")),
                      ("gross_exposure", acct.get("gross_exposure")),
                      ("gross_leverage", acct.get("gross_leverage")),
                      ("current_drawdown", dd.get("current_drawdown"))):
        _ref(refs, source_type=(bc.SRC_RULE if path.startswith("risk")
                                else bc.SRC_DERIVED),
             source_table="risk_assessments", source_id=aid,
             source_version=ver, data_as_of=as_of,
             field_path=f"account.{path}", value=val)

    return {
        "assessment_id": aid,
        "snapshot_id": a.get("snapshot_id"),
        "account_id_masked": bc.mask_account(a.get("account_id")),
        "data_as_of": as_of,
        "calculation_version": ver,
        "rule_set_version": a.get("rule_set_version"),
        "risk_level": a.get("risk_level"),
        "risk_score": _s(a.get("risk_score")),
        "reliable": bool(a.get("reliable")),
        "blocked_reason": a.get("blocked_reason"),
        "ai_analysis_allowed": bool(a.get("ai_analysis_allowed")),
        "data_quality_status": a.get("data_quality_status"),
        "component_scores": {k: _s(v) for k, v in
                             (a.get("component_scores") or {}).items()},
        "score_normalization_method": a.get("score_normalization_method"),
        # 账户概况 —— 金额一律字符串，绝不转 float
        "net_liquidation": _s(acct.get("net_liquidation")),
        "total_cash": _s(acct.get("total_cash")),
        "cash_ratio": _s(acct.get("cash_ratio")),
        # ---- 持仓敞口：用**风险引擎已在使用的标准字段**，口径写清楚 ----
        # 绝不用 net_liquidation - cash 倒推 —— 那个差值里混着未结算资金、
        # 应计利息和多币种折算，看起来像持仓市值，其实不是。
        "positions_market_value": _s(acct.get("positions_market_value")),
        "positions_value_source": acct.get("positions_value_source"),
        "long_exposure": _s(acct.get("long_exposure")),
        "short_exposure": _s(acct.get("short_exposure")),
        "gross_exposure": _s(acct.get("gross_exposure")),
        "net_exposure": _s(acct.get("net_exposure")),
        "invested_ratio": _s(acct.get("invested_ratio")),
        "exposure_basis": _exposure_basis(acct),
        "gross_leverage": _s(acct.get("gross_leverage")),
        "net_leverage": _s(acct.get("net_leverage")),
        "current_drawdown": _s(dd.get("current_drawdown")),
        "max_drawdown": _s(dd.get("max_drawdown")),
        "maint_margin_safety_multiple": _s(liq.get("maint_margin_safety_multiple")),
        "excess_liquidity_ratio": _s(acct.get("excess_liquidity_ratio")),
        "triggered_rules": a.get("triggered_rules") or [],
        "top_risk_drivers": a.get("top_risk_drivers") or [],
        "events": a.get("events") or [],
        "concentration_applicability": a.get("concentration_applicability") or {},
        "_concentration_metrics": m.get("concentration") or {},
        "_positions": m.get("positions") or [],
    }


#: 持仓市值的口径说明。**简报展示「持仓总市值」时必须说清是净值还是多头市值。**
#: 风险引擎的 positions_market_value 是券商报的持仓市值合计（多空相抵后的净值），
#: 与 long_exposure（多头总敞口）在有空头时会不同。
_BASIS_NET = "净市值（多空相抵，来自券商 positions_value）"
_BASIS_LONG = "多头总市值（账户无空头，净市值与多头敞口相同）"


def _exposure_basis(acct: dict) -> str:
    """说明「持仓总市值」这个数字到底是什么口径。"""
    short = _d(acct.get("short_exposure"))
    src = acct.get("positions_value_source")
    basis = _BASIS_LONG if (short is None or short == 0) else _BASIS_NET
    return basis + (f"；来源 {src}" if src else "")


# ======================= 市场 =======================

#: 市场指标结构版本。**生产实测的真实结构**（v1 假设的 `indices` 从不存在）：
#:   calculated_metrics.qqq / spy / sox   趋势指标（close / ma20/50/200 / vs_maN）
#:   calculated_metrics.vol               vix_current / vxn_current / *_change_1d/5d
#:   calculated_metrics.rates             current / change_1d / change_5d / change_20d
#:   calculated_metrics.sox_vs_qqq_20d    半导体相对大盘强弱
#:   calculated_metrics.data.instruments  每个标的的 data_as_of / quality_status
MARKET_MAP_VERSION = "market-map-v2/2026-07"

#: 旧结构（v1 假设）—— 真实生产从未产生过，但历史评估若有则兼容读取
_LEGACY_INDEX_KEYS = ("indices", "by_code")

#: 简报里用到的标的 -> calculated_metrics 中的趋势键。
#: **注意 SOXX 的指标键是 `sox`** —— 直接用 symbol 找会永远取空。
_TREND_KEY = {"QQQ": "qqq", "SOXX": "sox", "SPY": "spy"}


def _instrument_meta(metrics: dict, symbol: str) -> dict:
    """标的的数据时间与质量状态。缺失一律 None，绝不填 0 或猜。"""
    inst = ((metrics.get("data") or {}).get("instruments") or {}).get(symbol) or {}
    return {
        "data_as_of": inst.get("data_as_of"),
        "quality_status": inst.get("quality_status"),
        "delayed": inst.get("delayed"),
        "close": _s(inst.get("close")),
        "daily_change_percent": _s(inst.get("daily_change_percent")),
        "source": inst.get("source"),
        "quote_as_of": inst.get("quote_as_of"),
        "available": bool(inst),
    }


def _trend_block(metrics: dict, symbol: str, legacy: dict) -> dict:
    """一个指数的趋势快照。

    **只读已经算好的指标，绝不在这里重算均线或变化率** ——
    简报层重算 = 两套口径，迟早对不上。
    """
    t = metrics.get(_TREND_KEY.get(symbol, symbol.lower())) or {}
    if not t:                                   # 兼容旧结构
        t = legacy.get(symbol) or legacy.get(symbol.lower()) or {}
    meta = _instrument_meta(metrics, symbol)
    return {
        "symbol": symbol,
        "close": _s(t.get("close")) or meta.get("close"),
        "daily_change_percent": meta.get("daily_change_percent"),
        "ma20": _s(t.get("ma20")), "ma50": _s(t.get("ma50")),
        "ma200": _s(t.get("ma200")),
        "vs_ma20": _s(t.get("vs_ma20")), "vs_ma50": _s(t.get("vs_ma50")),
        "vs_ma200": _s(t.get("vs_ma200")),
        "below_ma20": t.get("below_ma20"), "below_ma50": t.get("below_ma50"),
        "below_ma200": t.get("below_ma200"),
        "return_20d": _s(t.get("return_20d")),
        "drawdown_from_20d_high": _s(t.get("drawdown_from_20d_high")),
        "status": t.get("status"),
        "data_as_of": meta.get("data_as_of"),
        "quote_as_of": meta.get("quote_as_of"),
        "source": meta.get("source"),
        "quality_status": meta.get("quality_status"),
        "delayed": meta.get("delayed"),
        # 相对均线状态用**已算好的布尔**表述，不自己比大小
        "trend": _trend_words(t),
        "available": bool(t) or meta.get("available"),
    }


def _trend_words(t: dict) -> str | None:
    """把 below_maN 三个布尔翻译成一句中文。全缺失时返回 None（不是「正常」）。"""
    flags = [t.get("below_ma20"), t.get("below_ma50"), t.get("below_ma200")]
    if all(f is None for f in flags):
        return None
    names = ("20 日", "50 日", "200 日")
    below = [n for n, f in zip(names, flags) if f is True]
    above = [n for n, f in zip(names, flags) if f is False]
    parts = []
    if below:
        parts.append("跌破" + "、".join(below) + "均线")
    if above:
        parts.append("站上" + "、".join(above) + "均线")
    return " · ".join(parts) or None


def market_block(db, refs: list) -> dict | None:
    """市场环境。**只描述环境，不预测价格** —— 这条约束在 prompt 里再强调一次。

    字段路径按**生产实测的真实结构**取；旧结构做兼容读取但不猜测。
    """
    try:
        row = db.latest_market_assessment()
    except Exception:  # noqa: BLE001
        row = None
    if row is None:
        return None
    r = dict(row)
    import json as _json

    def j(v, default):
        if isinstance(v, (dict, list)):
            return v
        try:
            return _json.loads(v) if v else default
        except (TypeError, ValueError):
            return default

    metrics = j(r.get("calculated_metrics"), {})
    legacy = {}
    for k in _LEGACY_INDEX_KEYS:
        if isinstance(metrics.get(k), dict):
            legacy = metrics[k]
            break

    aid, ver = r.get("assessment_id"), r.get("calculation_version")
    live_as_of = ((metrics.get("data") or {}).get("live_as_of"))
    as_of = live_as_of or r.get("data_as_of")
    for path, val in (("market_state", r.get("market_state")),
                      ("market_risk_level", r.get("market_risk_level")),
                      ("market_risk_score", r.get("market_risk_score"))):
        _ref(refs, source_type=bc.SRC_RULE,
             source_table="market_environment_assessments",
             source_id=aid, source_version=ver, data_as_of=as_of,
             field_path=f"market.{path}", value=val)

    qqq = _trend_block(metrics, "QQQ", legacy)
    soxx = _trend_block(metrics, "SOXX", legacy)
    spy = _trend_block(metrics, "SPY", legacy)

    vol = metrics.get("vol") or {}
    rates = metrics.get("rates") or {}
    vix_meta = _instrument_meta(metrics, "VIX")
    vxn_meta = _instrument_meta(metrics, "VXN")
    us10y_meta = _instrument_meta(metrics, "US10Y")

    vix = {
        "current": _s(vol.get("vix_current")) or vix_meta.get("close"),
        "change_1d": _s(vol.get("vix_change_1d")),
        "change_5d": _s(vol.get("vix_change_5d")),
        "percentile": _s(vol.get("vix_percentile")),
        "data_as_of": vix_meta.get("data_as_of"),
        "quality_status": vix_meta.get("quality_status"),
        "available": bool(vol.get("vix_current") or vix_meta.get("close")),
    }
    vxn = {
        "current": _s(vol.get("vxn_current")) or vxn_meta.get("close"),
        "change_1d": _s(vol.get("vxn_change_1d")),
        "change_5d": _s(vol.get("vxn_change_5d")),
        "percentile": _s(vol.get("vxn_percentile")),
        # VXN 与 VIX 的关系用**已算好的差值**，不在这里相减
        "above_vix": _s(vol.get("vxn_above_vix")),
        "both_rising": vol.get("both_rising"),
        "data_as_of": vxn_meta.get("data_as_of"),
        "quality_status": vxn_meta.get("quality_status"),
        "available": bool(vol.get("vxn_current") or vxn_meta.get("close")),
    }
    us10y = {
        "current": _s(rates.get("current")) or us10y_meta.get("close"),
        "change_1d": _s(rates.get("change_1d")),
        "change_5d": _s(rates.get("change_5d")),
        "change_20d": _s(rates.get("change_20d")),
        "pct_change_5d": _s(rates.get("pct_change_5d")),
        "data_as_of": us10y_meta.get("data_as_of"),
        "quality_status": us10y_meta.get("quality_status"),
        "available": bool(rates.get("current") or us10y_meta.get("close")),
    }

    for path, val, sid in (("qqq.close", qqq["close"], aid),
                           ("soxx.close", soxx["close"], aid),
                           ("vix.current", vix["current"], aid),
                           ("vxn.current", vxn["current"], aid),
                           ("us10y.current", us10y["current"], aid),
                           ("sox_vs_qqq_20d",
                            _s(metrics.get("sox_vs_qqq_20d")), aid)):
        _ref(refs, source_type=bc.SRC_DERIVED,
             source_table="market_environment_assessments",
             source_id=sid, source_version=ver, data_as_of=as_of,
             field_path=f"market.{path}", value=val)

    limits = _market_limits(qqq, soxx, vix, vxn, us10y, metrics)

    return {
        "assessment_id": aid, "data_as_of": as_of, "calculation_version": ver,
        "map_version": MARKET_MAP_VERSION,
        "market_state": r.get("market_state"),
        "market_risk_level": r.get("market_risk_level"),
        "market_risk_score": _s(r.get("market_risk_score")),
        "reliable": bool(r.get("reliable")),
        "blocked_reason": r.get("blocked_reason"),
        "quality_status": r.get("quality_status"),
        "component_scores": {k: _s(v) for k, v in
                             (j(r.get("component_scores"), {}) or {}).items()},
        "qqq": qqq, "soxx": soxx, "spy": spy,
        "vix": vix, "vxn": vxn, "us10y": us10y,
        "sox_vs_qqq_20d": _s(metrics.get("sox_vs_qqq_20d")),
        "multi_index_weak": metrics.get("multi_index_weak"),
        "rates_equity_note": metrics.get("rates_equity_note"),
        "top_drivers": j(r.get("top_market_drivers"), []),
        "triggered_rules": j(r.get("triggered_rules"), []),
        "delayed": not bool((metrics.get("data") or {}).get("realtime_count")),
        "delay_note": ("QQQ/SPY/SOXX 使用富途实时行情；VIX/VXN/美债为 IBKR 回退"
                       if (metrics.get("data") or {}).get("realtime_count")
                       else "市场数据当前使用 IBKR 回退"),
        "limitations": limits,
    }


def _market_limits(qqq, soxx, vix, vxn, us10y, metrics) -> list[str]:
    """缺失项如实列出。**缺失写 null 并说明，绝不写 0。**"""
    out = []
    for name, blk in (("QQQ", qqq), ("SOXX", soxx), ("VIX", vix),
                      ("VXN", vxn), ("十年期美债", us10y)):
        if not blk.get("available"):
            out.append(f"{name} 指标缺失，本次简报不含该项（缺失不等于 0）")
        elif blk.get("quality_status") and blk["quality_status"] != "fresh":
            out.append(f"{name} 数据质量为 {blk['quality_status']}，"
                       f"截至 {blk.get('data_as_of')}")
    if metrics.get("sox_vs_qqq_20d") is None:
        out.append("半导体相对大盘强弱指标缺失")
    if (metrics.get("breadth") or {}).get("available") is False:
        out.append("市场宽度指标不可用："
                   + str((metrics.get("breadth") or {}).get("note") or ""))
    return out


# ======================= 数据质量 =======================

def quality_block(db, acct: dict | None) -> dict:
    issues = []
    sid = (acct or {}).get("snapshot_id")
    if sid:
        try:
            issues = [dict(x) for x in db.quality_issues_for(sid)]
        except Exception:  # noqa: BLE001
            issues = []
    crit = [i for i in issues
            if str(i.get("severity") or "").lower() in ("critical", "error")]
    return {
        "snapshot_id": sid,
        "status": (acct or {}).get("data_quality_status"),
        "issue_count": len(issues), "critical_count": len(crit),
        "issues": issues,
        "summary": [f"{i.get('issue_type')}: {i.get('detail')}" for i in issues[:8]],
    }


# ======================= 风险事件 =======================

def event_block(db, *, since: datetime, now: datetime, refs: list) -> list[dict]:
    """事件登记表。窗口取 30 天 —— 持续事件的首次发现时间可能很早，
    只取 1 天会把它们全部误判为新增。"""
    start = (now - timedelta(days=30)).isoformat()
    try:
        rows, _ = db.query_risk_events(start=start, limit=500)
    except Exception:  # noqa: BLE001
        return []
    out = [dict(r) for r in rows]
    for e in out[:20]:
        _ref(refs, source_type=bc.SRC_RULE, source_table="risk_event_registry",
             source_id=e.get("event_id"), source_version=e.get("rule_version"),
             data_as_of=e.get("last_detected_at"),
             field_path=f"event.{e.get('rule_code')}.actual_value",
             value=e.get("actual_value"))
    return out


# ======================= 集中度 =======================

def concentration_block(acct: dict | None, refs: list) -> dict:
    """集中度。**not_applicable 必须原样带上原因，绝不写成「规则通过」。**"""
    if not acct:
        return {"available": False, "not_applicable_rules": [],
                "observation_period_rules": []}
    m = acct.get("_concentration_metrics") or {}
    ap = acct.get("concentration_applicability") or {}
    aid = acct.get("assessment_id")

    for path in ("hhi", "effective_position_count", "top1_weight"):
        _ref(refs, source_type=bc.SRC_DERIVED, source_table="risk_assessments",
             source_id=aid, source_version=acct.get("calculation_version"),
             data_as_of=acct.get("data_as_of"),
             field_path=f"concentration.{path}", value=m.get(path))

    triggered = set(acct.get("triggered_rules") or [])
    obs = [r for r in bc.OBSERVATION_PERIOD_RULES if r in triggered]
    return {
        "available": True,
        "nominal_position_count": m.get("total_position_count"),
        "eligible_long_position_count": m.get("eligible_long_position_count"),
        "eligible_short_position_count": m.get("eligible_short_position_count"),
        "excluded_count": m.get("excluded_count") or 0,
        "excluded_positions": [
            {"symbol": x.get("symbol"), "reason": x.get("reason"),
             "note": x.get("note")} for x in (m.get("excluded_positions") or [])],
        "largest_position_weight": _s(m.get("top1_weight")),
        "top3_weight": _s(m.get("top3_weight")),
        "top5_weight": _s(m.get("top5_weight")),
        "hhi": _s(m.get("hhi")),
        "effective_position_count": _s(m.get("effective_position_count")),
        "dominance_gap": _s(m.get("dominance_gap")),
        "single_position_dominance": "SINGLE_POSITION_DOMINANCE" in triggered,
        "applicable_rule_count": ap.get("applicable_rule_count"),
        "not_applicable_rule_count": ap.get("not_applicable_rule_count"),
        "not_applicable_rules": [
            {"rule_code": x.get("rule_code"), "reason": x.get("reason")}
            for x in (ap.get("not_applicable") or [])],
        "score_normalization_method": acct.get("score_normalization_method"),
        "observation_period_rules": obs,
        "observation_note": bc.OBSERVATION_NOTE if obs else None,
    }


# ======================= 收益归因 =======================

def performance_block(cfg, db, brief_date: str, account_id: str | None,
                      refs: list) -> dict | None:
    """当月至今的收益归因。取已落库的 performance_periods 结果。"""
    from . import performance as pf

    day = str(brief_date)[:10]
    start = day[:8] + "01"
    try:
        row = pf.compute_period(cfg, db, start=start, end=day,
                                account=account_id, granularity="mtd")
    except Exception:  # noqa: BLE001
        row = None
    if not row:
        return None

    pid, ver = row.get("period_id"), row.get("calculation_version")
    for path in ("cash_flow_adjusted_pnl", "realized_pnl",
                 "unrealized_pnl_change", "external_cash_flow",
                 "modified_dietz_return", "twr_return"):
        _ref(refs, source_type=bc.SRC_DERIVED, source_table="performance_periods",
             source_id=pid, source_version=ver, data_as_of=row.get("period_end"),
             field_path=f"performance.{path}", value=row.get(path))

    cov = row.get("coverage") or {}
    limits = list(row.get("data_limitations") or [])
    # 三条口径提醒必须每次都在 —— 它们是这套数字最常被误读的地方
    limits.append("入金不是盈利，出金不是亏损 —— 收益率已剔除外部资金进出的影响")
    limits.append("券商 Flex 流水为 T+1，最新一天可能为暂定数据")
    pc = row.get("period_coverage") or {}
    if pc.get("mismatch"):
        limits.append(
            "已实现盈亏和未实现盈亏序列覆盖区间不同，因此当前归因存在未测量部分，"
            "不代表现金账目未对平")

    return {
        "period_id": pid, "period_start": row.get("period_start"),
        "period_end": row.get("period_end"),
        "calculation_version": ver,
        "opening_nav": row.get("opening_nav"), "closing_nav": row.get("closing_nav"),
        "raw_nav_change": row.get("raw_nav_change"),
        "external_cash_flow": row.get("external_cash_flow"),
        "deposits": row.get("deposits"), "withdrawals": row.get("withdrawals"),
        "cash_flow_adjusted_pnl": row.get("cash_flow_adjusted_pnl"),
        "realized_pnl": row.get("realized_pnl"),
        "unrealized_pnl_change": row.get("unrealized_pnl_change"),
        "dividends": row.get("dividends"), "interest": row.get("interest"),
        "commissions": row.get("commissions"), "fees": row.get("fees"),
        "taxes": row.get("taxes"),
        "unexplained_difference": row.get("unexplained_difference"),
        "modified_dietz_return": row.get("modified_dietz_return"),
        "twr_return": row.get("twr_return"),
        "data_quality": row.get("data_quality"),
        "quality_note": row.get("quality_note"),
        "cash_flow_source": row.get("cash_flow_source"),
        "flex_report_to": cov.get("coverage_end"),
        "flex_coverage": cov,
        "period_coverage": pc,
        "limitations": limits,
    }


# ======================= 历史基线 =======================

def baseline_block(db, account_id: str | None, *, now: datetime,
                   current_account: dict | None,
                   current_market: dict | None) -> dict:
    """近 7 / 30 个可用交易日的基线。样本不足时明确说明，不编造。"""
    out: dict = {"baselines": {}, "comparisons": {}, "previous_day": None}
    end = now.isoformat()

    for w in bc.BASELINE_WINDOWS:
        start = (now - timedelta(days=w)).isoformat()
        try:
            rows = [dict(r) for r in
                    db.assessments_between(start, end, account=account_id)]
        except Exception:  # noqa: BLE001
            rows = []
        daily = _one_per_day(rows, "data_as_of")
        key = f"{w}d"
        out["baselines"][key] = {
            "risk_score": bc.baseline([_d(r.get("risk_score")) for r in daily],
                                      window=w),
            "sample_size": len(daily),
        }
        cur = _d((current_account or {}).get("risk_score"))
        out["comparisons"][key] = {
            "risk_score": bc.compare_to_baseline(
                cur, out["baselines"][key]["risk_score"]),
        }
        try:
            mrows = [dict(r) for r in db.market_assessments_between(start, end)]
        except Exception:  # noqa: BLE001
            mrows = []
        mdaily = _one_per_day(mrows, "data_as_of")
        mb = bc.baseline([_d(r.get("market_risk_score")) for r in mdaily], window=w)
        out["baselines"][key]["market_risk_score"] = mb
        out["comparisons"][key]["market_risk_score"] = bc.compare_to_baseline(
            _d((current_market or {}).get("market_risk_score")), mb)

    # 上一有效交易日
    try:
        prev = [dict(r) for r in db.assessments_between(
            (now - timedelta(days=10)).isoformat(), end, account=account_id)]
    except Exception:  # noqa: BLE001
        prev = []
    today = str((current_account or {}).get("data_as_of") or "")[:10]
    earlier = [r for r in _one_per_day(prev, "data_as_of")
               if str(r.get("data_as_of") or "")[:10] < today]
    if earlier:
        p = earlier[-1]
        cur_score = _d((current_account or {}).get("risk_score"))
        p_score = _d(p.get("risk_score"))
        out["previous_day"] = {
            "date": str(p.get("data_as_of") or "")[:10],
            "risk_level": p.get("risk_level"),
            "risk_score": _s(p_score),
            "score_delta": (_s(cur_score - p_score)
                            if cur_score is not None and p_score is not None
                            else None),
            "level_changed": p.get("risk_level") != (
                current_account or {}).get("risk_level"),
        }
    else:
        out["previous_day"] = {"available": False,
                               "note": "没有更早的有效交易日评估可供对比"}

    out["sample_size"] = out["baselines"].get("7d", {}).get("sample_size", 0)
    return out


def _one_per_day(rows: list[dict], ts_key: str) -> list[dict]:
    """每个自然日只保留最后一条 —— 同一天多条会让基线被高频采样主导。"""
    by_day: dict[str, dict] = {}
    for r in sorted(rows, key=lambda x: str(x.get(ts_key) or "")):
        day = str(r.get(ts_key) or "")[:10]
        if day:
            by_day[day] = r
    return [by_day[k] for k in sorted(by_day)]


# ======================= 最大风险来源 =======================

def top_risks(acct: dict | None, changes: dict, *, limit: int = 5) -> list[dict]:
    """最多 5 项最大风险来源。按变化优先级排序，新增与升级排在持续之前。"""
    out: list[dict] = []
    seen: set = set()
    for it in (changes.get("ordered") or []):
        if len(out) >= limit:
            break
        key = (it.get("rule_code"), it.get("affected"))
        if key in seen or it.get("change_kind") in ("resolved", "suppressed"):
            continue
        seen.add(key)
        out.append({
            "title": it.get("title") or it.get("rule_code"),
            "severity": it.get("severity"),
            "actual_value": it.get("actual_value"),
            "threshold": it.get("threshold_value"),
            "affected_object": it.get("affected"),
            "is_new": it.get("change_kind") == "new",
            "change_kind": it.get("change_kind"),
            "rule_code": it.get("rule_code"),
            "rule_version": it.get("rule_version"),
            "in_observation_period": it.get("in_observation_period"),
            "explanation_facts": _facts_for(acct, it.get("rule_code")),
            "recommended_check": _check_for(acct, it.get("rule_code")),
        })
    return out


def _facts_for(acct: dict | None, rule_code: str | None) -> str | None:
    for e in (acct or {}).get("events") or []:
        if e.get("rule_code") == rule_code:
            return e.get("explanation")
    return None


def _check_for(acct: dict | None, rule_code: str | None) -> str | None:
    for e in (acct or {}).get("events") or []:
        if e.get("rule_code") == rule_code:
            return e.get("recommended_check")
    return None
