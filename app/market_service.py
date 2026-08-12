"""市场环境评估编排：取数 → 质量 → 指标 → 规则 → 评分 → 落库。

与账户同步**完全解耦**：独立任务、独立 run 状态、异常全吞。
市场数据取不到是常态（订阅权限、节假日、外部服务抖动），绝不能因此影响
IBKR 账户同步 —— 账户数据才是这个系统的命脉。
"""
from __future__ import annotations

import json
import time
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from . import market_data as md
from . import market_engine as me
from . import market_metrics as mm
from . import pnl as pnl_calc
from .futu_quotes import MARKET_LIVE_SYMBOLS
from .logging_setup import get_logger

log = get_logger()

IBKR_LIVE_INDEX_CODES = frozenset(("VIX", "VXN", "US10Y"))


def sync_live_market_data(cfg, db, quotes: list[dict] | dict[str, dict], *,
                          now: datetime | None = None) -> dict:
    """写入可信盘中行情：ETF 用富途，VIX/VXN/US10Y 用 IBKR 只读订阅。"""
    now = now or datetime.now(timezone.utc)
    rows = list(quotes.values()) if isinstance(quotes, dict) else list(quotes or [])
    specs = {s.code: s for s in md.DEFAULT_INSTRUMENTS}
    latest = db.latest_market_snapshots()
    payload = []
    changed = False
    for q in rows:
        code = str(q.get("symbol") or "")
        spec = specs.get(code)
        source = str(q.get("source") or "")
        is_futu = source.startswith("FUTU")
        is_ibkr_index = (code in IBKR_LIVE_INDEX_CODES
                         and source.startswith("IBKR_reqMktData"))
        if spec is None or not (is_futu or is_ibkr_index):
            continue
        if is_futu and code not in MARKET_LIVE_SYMBOLS:
            continue
        price, previous = md._d(q.get("price")), md._d(q.get("previous_close"))
        quote_as_of = q.get("as_of")
        if price is None or previous is None or not quote_as_of:
            continue
        if is_ibkr_index and q.get("price_source") not in ("LAST", "BID_ASK_MID"):
            continue
        quote_quality = str(q.get("quality") or "")
        if is_ibkr_index and quote_quality in (
                "STALE", "MISSING", "INVALID", "FROZEN", "DELAYED_FROZEN"):
            continue
        try:
            data_date = datetime.fromisoformat(quote_as_of).astimezone(
                cfg.market_tz).date().isoformat()
        except (TypeError, ValueError):
            continue
        market_data_type = q.get("effective_market_data_type", q.get("market_data_type"))
        try:
            market_data_type = int(market_data_type)
        except (TypeError, ValueError):
            market_data_type = None
        delayed = bool(is_ibkr_index and (
            market_data_type in (3, 4) or quote_quality.startswith("DELAYED")))
        source_label = ("IBKR 延迟盘中行情" if delayed else
                        ("IBKR 实时盘中行情" if is_ibkr_index else "富途实时行情"))
        bar = md.MarketBar(
            instrument_code=code, instrument_name=spec.name,
            data_as_of=data_date, fetched_at=now.isoformat(),
            open=md._d(q.get("open")), high=md._d(q.get("high")),
            low=md._d(q.get("low")), close=price, previous_close=previous,
            volume=(int(md._d(q.get("volume")) or 0) or None),
            source=source, delayed=delayed, quality_status=md.FRESH,
            quality_note=f"{source_label}；历史均线仍使用已落库日线",
            quote_as_of=quote_as_of,
            raw_json=json.dumps({
                "price_source": q.get("price_source"),
                "received_at": q.get("received_at"),
                "quality": q.get("quality"),
                "market_data_type": q.get("market_data_type"),
                "requested_market_data_type": q.get("requested_market_data_type"),
                "effective_market_data_type": q.get("effective_market_data_type"),
            }, ensure_ascii=False, default=str),
        )
        payload.append(bar.as_row())
        old = latest.get(code) or {}
        changed = changed or old.get("quote_as_of") != quote_as_of or str(
            old.get("close")) != str(price)
    received_codes = {r["instrument_code"] for r in payload}
    futu_missing = sorted(set(MARKET_LIVE_SYMBOLS) - received_codes)
    expected_codes = set(MARKET_LIVE_SYMBOLS) | set(IBKR_LIVE_INDEX_CODES)
    missing_codes = sorted(expected_codes - received_codes)
    staled = 0
    if missing_codes:
        staled = db.mark_live_market_stale(
            missing_codes, note="盘中行情缺失或过期，已回退最近有效收盘数据")
    if payload:
        db.upsert_market_snapshots(payload)
        db.set_sync_state("market_live_last_update", now.isoformat())
    return {"changed": changed or bool(staled), "count": len(payload),
            # 保持原有调用契约：fallback 仍表示富途 ETF 覆盖缺口。
            "fallback": futu_missing}


def sync_market_data(cfg, db, client, *, now: datetime | None = None,
                     request_id: str | None = None) -> dict:
    """抓取市场数据并落库。返回 run 摘要。**任何异常都不外抛。**"""
    now = now or datetime.now(timezone.utc)
    run_id = uuid.uuid4().hex
    trace = request_id or run_id[:8]
    t0 = time.monotonic()
    db.start_market_sync_run(run_id, now.isoformat(), request_id=request_id)
    try:
        provider = md.IBKRMarketData(client)
        specs = md.DEFAULT_INSTRUMENTS
        res = md.fetch_all(provider, specs, lookback_days=cfg.market_lookback_days,
                           now=now)
        today_et = pnl_calc.et_date_of(now, cfg.market_tz)
        md.apply_freshness(res.latest, today_et=today_et,
                           max_lag_days=cfg.market_max_lag_days)
        if res.latest:
            db.upsert_market_snapshots([b.as_row() for b in res.latest.values()])
        # 日线**持久化**到 daily_bars（与持仓标的同一张表）：回测需要一份可审计、
        # 不会被下一次失败抹掉的历史来源。sync_state 那份只是给实时评估的快取。
        for code, bars in res.bars_by_code.items():
            try:
                db.upsert_daily_bars(code, [
                    {"date": b["date"], "o": b.get("o"), "h": b.get("h"),
                     "l": b.get("l"), "c": b.get("c"), "v": b.get("v") or 0}
                    for b in bars if b.get("date") and b.get("c") is not None])
            except Exception:  # noqa: BLE001 — 单个标的落库失败不影响其余
                log.warning("市场日线落库失败 %s", code, exc_info=True)
        # 快取只在**确实取到数据**时才覆盖。取数失败时保留上一份 ——
        # 无条件覆盖会把已有的 260 根日线换成空字典，实时指标随之全哑。
        if res.bars_by_code:
            db.set_sync_state("market_bars_cache", json.dumps(
                {k: [{"date": b["date"], "c": str(b["c"])} for b in v]
                 for k, v in res.bars_by_code.items()}, ensure_ascii=False))
        fetch_ms = int((time.monotonic() - t0) * 1000)
        required_missing = [s.code for s in specs
                            if s.required and s.code not in res.latest]
        # failed 只留给「什么都没拿到」；拿到一部分就是 partial ——
        # 必需标的缺失会在评估层把 reliable 判为 False，不必在同步层夸大成完全失败。
        if res.ok_count == 0:
            state = "failed"
        elif res.failed or required_missing:
            state = "partial"
        else:
            state = "success"
        db.finish_market_sync_run(
            run_id, run_state=state, fetch_ms=fetch_ms,
            instruments_ok=res.ok_count, instruments_failed=len(res.failed),
            error_code=("REQUIRED_INSTRUMENT_MISSING" if required_missing else None),
            error_message="；".join(f"{k}: {v}" for k, v in res.failed.items())[:500] or None)
        log.info("市场数据同步 trace=%s state=%s ok=%d failed=%d",
                 trace, state, res.ok_count, len(res.failed))
        return {"run_id": run_id, "state": state, "ok": res.ok_count,
                "failed": res.failed, "required_missing": required_missing}
    except Exception as exc:  # noqa: BLE001 — 市场数据绝不影响账户同步
        db.finish_market_sync_run(run_id, run_state="failed",
                                  error_code="MARKET_SYNC_ERROR",
                                  error_message=f"{type(exc).__name__}: {str(exc)[:300]}")
        log.warning("市场数据同步失败 trace=%s（账户同步不受影响）", trace, exc_info=True)
        return {"run_id": run_id, "state": "failed", "ok": 0, "failed": {}, "error": str(exc)[:200]}


def _bars_cache(db) -> dict[str, list[dict]]:
    """实时评估用的日线快取。空则回落到 daily_bars —— 快取是加速手段，
    不是唯一真相，它丢了不该让市场指标整体变哑。"""
    try:
        raw = db.get_sync_state("market_bars_cache")
        cached = json.loads(raw) if raw else {}
    except (TypeError, ValueError):
        cached = {}
    if cached:
        return cached
    out: dict[str, list[dict]] = {}
    for spec in md.DEFAULT_INSTRUMENTS:
        try:
            rows = db.get_daily_bars(spec.code, limit=400)
        except Exception:  # noqa: BLE001
            rows = []
        if rows:
            out[spec.code] = [{"date": r["date"], "c": str(r["c"])} for r in rows]
    return out


def compute_metrics(cfg, db) -> tuple[dict, dict]:
    """全部市场指标 + 数据状态。返回 (metrics, data_state)。"""
    bars = _bars_cache(db)
    latest = db.latest_market_snapshots()

    def series(code: str) -> list[dict]:
        rows = [dict(r) for r in bars.get(code, [])]
        live = latest.get(code) or {}
        # 日线负责历史，可信盘中源只替换/追加「今天这一根」的当前价。这样均线、
        # 波动率和市场规则会随盘中价格变化，同时不丢失 IBKR 历史深度。
        source = str(live.get("source") or "")
        is_live_source = (source == "FUTU_OpenD"
                          or source.startswith("IBKR_reqMktData"))
        if (is_live_source and live.get("quote_as_of")
                and live.get("close") is not None):
            point = {"date": live.get("data_as_of"), "c": live.get("close")}
            if rows and str(rows[-1].get("date")) == str(point["date"]):
                rows[-1] = {**rows[-1], **point}
            else:
                rows.append(point)
        return rows

    qqq = mm.trend_metrics(series("QQQ"))
    spy = mm.trend_metrics(series("SPY"))
    sox = mm.trend_metrics(series("SOXX"))
    vol = mm.volatility_metrics(series("VIX"), series("VXN"),
                                percentile_window=cfg.market_percentile_window)
    rates = mm.rates_metrics(series("US10Y"))

    required = [s.code for s in md.DEFAULT_INSTRUMENTS if s.required]
    missing_required = [c for c in required if c not in latest]
    stale = [c for c, r in latest.items() if r.get("quality_status") == md.STALE]
    inconsistent = len({r.get("data_as_of") for r in latest.values()}) > 1

    metrics = {
        "qqq": qqq, "spy": spy, "sox": sox, "vol": vol, "rates": rates,
        "sox_vs_qqq_20d": mm.relative_strength(series("SOXX"), series("QQQ"), 20),
        "multi_index_weak": bool(qqq.get("below_ma50") and spy.get("below_ma50")),
        "rates_equity_co": mm.rates_and_equity_together(rates, qqq).get("co_occurring"),
        "rates_equity_note": "仅表示两者同期发生，不代表因果关系",
        "data": {
            "missing_required": bool(missing_required),
            "missing_list": missing_required,
            "stale": bool(stale), "stale_list": stale,
            "inconsistent": inconsistent,
            "instruments": {c: {"data_as_of": r.get("data_as_of"),
                                "quality_status": r.get("quality_status"),
                                "delayed": bool(r.get("delayed")),
                                "close": r.get("close"),
                                "daily_change_percent": r.get("daily_change_percent"),
                                "name": r.get("instrument_name"),
                                "source": r.get("source"),
                                "quote_as_of": r.get("quote_as_of")}
                            for c, r in latest.items()},
            "live_as_of": max((r.get("quote_as_of") for r in latest.values()
                                if r.get("quote_as_of")), default=None),
            "realtime_count": sum(1 for r in latest.values()
                                  if r.get("source") == "FUTU_OpenD"
                                  and not bool(r.get("delayed"))),
        },
        # breadth 第一版不实现：没有可靠数据源，宁可留空也不用替代品充数
        "breadth": {"available": False,
                    "note": "第一版未接入可靠的市场广度数据，故不参与评分"},
    }
    data_as_of = max((r.get("data_as_of") for r in latest.values()), default=None)
    return metrics, {"data_as_of": data_as_of, "missing_required": missing_required,
                     "stale": stale, "inconsistent": inconsistent,
                     "instrument_count": len(latest)}


def assess(cfg, db, *, now: datetime | None = None, force: bool = False) -> dict | None:
    """市场环境评估。与 data_as_of + 规则版本一一对应（幂等）。"""
    now = now or datetime.now(timezone.utc)
    metrics, state = compute_metrics(cfg, db)
    data_as_of = state["data_as_of"]
    rules = me.default_rules(cfg)
    version = me.rule_set_version(rules)
    if data_as_of is None:
        return {
            "assessment_id": None, "data_as_of": None, "market_state": me.BLOCKED,
            "market_risk_level": "blocked", "market_risk_score": None,
            "reliable": False, "blocked_reason": "尚无任何市场数据",
            "component_scores": {}, "calculated_metrics": metrics,
            "triggered_rules": [], "top_market_drivers": [],
            "quality_status": "error", "calculation_version": version,
            "rule_set_version": version, "events": [],
        }
    existing = db.market_assessment_for(data_as_of, version)
    if existing and not force:
        return load_market_assessment(db, dict(existing))

    events = me.evaluate(metrics, rules)
    reliable = not state["missing_required"] and not state["stale"]
    sc = me.score(events, breadth_available=False)
    st = me.market_state(sc["market_risk_score"], events, reliable=reliable)
    blocked_reason = None
    if not reliable:
        bits = []
        if state["missing_required"]:
            bits.append("缺少必需市场数据：" + "、".join(state["missing_required"]))
        if state["stale"]:
            bits.append("数据过期：" + "、".join(state["stale"]))
        blocked_reason = "；".join(bits)

    payload = {
        # force=True 的盘中重算覆盖同一交易日的评估，避免每 5 秒生成一条历史。
        "assessment_id": (existing["assessment_id"] if existing else uuid.uuid4().hex),
        "data_as_of": data_as_of,
        "market_state": st,
        "market_risk_level": me.market_risk_level(st),
        "market_risk_score": (None if sc["market_risk_score"] is None
                              else str(sc["market_risk_score"])),
        "reliable": reliable, "blocked_reason": blocked_reason,
        "component_scores": {k: (None if v is None else str(v))
                             for k, v in sc["component_scores"].items()},
        "calculated_metrics": metrics,
        "triggered_rules": sorted({e.rule_code for e in events}),
        "top_market_drivers": me.top_drivers(events),
        "quality_status": ("fresh" if reliable
                           else ("stale" if state["stale"] else "error")),
        "calculation_version": version,
        "rule_set_version": version,
        "rule_set_hash": me.rule_set_hash(rules),
        "events": [e.as_dict(now.isoformat(), version) for e in events],
        "created_at": now.isoformat(),
    }
    db.save_market_assessment(payload)
    log.info("市场评估 as_of=%s state=%s score=%s reliable=%s rules=%d",
             data_as_of, st, payload["market_risk_score"], reliable, len(events))
    return payload


def live_market_payload(db) -> dict:
    """给 1 秒页面态的轻量市场摘要；全部数字来自最近一次已落库评估。"""
    row = db.latest_market_assessment()
    if row is None:
        return {}
    assessment = load_market_assessment(db, dict(row))
    metrics = assessment.get("calculated_metrics") or {}
    return {
        "state": assessment.get("market_state"),
        "risk_level": assessment.get("market_risk_level"),
        "score": assessment.get("market_risk_score"),
        "reliable": assessment.get("reliable"),
        "as_of": (metrics.get("data") or {}).get("live_as_of")
                 or assessment.get("data_as_of"),
        "instruments": (metrics.get("data") or {}).get("instruments") or {},
        "qqq": metrics.get("qqq") or {}, "spy": metrics.get("spy") or {},
        "sox": metrics.get("sox") or {},
        "sox_vs_qqq_20d": metrics.get("sox_vs_qqq_20d"),
        "vol": metrics.get("vol") or {}, "rates": metrics.get("rates") or {},
    }


def load_market_assessment(db, row: dict) -> dict:
    def j(v, default):
        try:
            return json.loads(v) if v else default
        except (TypeError, ValueError):
            return default
    return {
        "assessment_id": row["assessment_id"], "data_as_of": row["data_as_of"],
        "market_state": row["market_state"],
        "market_risk_level": row["market_risk_level"],
        "market_risk_score": row.get("market_risk_score"),
        "reliable": bool(row.get("reliable")),
        "blocked_reason": row.get("blocked_reason"),
        "component_scores": j(row.get("component_scores"), {}),
        "calculated_metrics": j(row.get("calculated_metrics"), {}),
        "triggered_rules": j(row.get("triggered_rules"), []),
        "top_market_drivers": j(row.get("top_market_drivers"), []),
        "quality_status": row.get("quality_status"),
        "calculation_version": row.get("calculation_version"),
        "rule_set_version": row.get("rule_set_version"),
        "rule_set_hash": row.get("rule_set_hash"),
        "created_at": row.get("created_at"),
        "events": db.market_events_for(row["assessment_id"]),
    }
