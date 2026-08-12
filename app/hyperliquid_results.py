"""Hyperliquid 信号的现实正股结果、验证和晋级记录。

该模块只读 Hyperliquid 快照/信号，再幂等写主库的结果表；不跨库事务，
也不会把链上价格当作现实正股结果价格。
"""
from __future__ import annotations

import hashlib
import json
import random
import uuid
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable

from .hyperliquid_outcomes import (
    CALCULATION_VERSION,
    calculate_outcome,
    signal_snapshot_hash,
    validation_summary,
)


OUTCOME_HORIZONS = ("30m", "2h", "close", "5d", "10d", "20d", "40d", "60d")
PRIMARY_HORIZONS = {"INTRADAY": "2h", "SWING": "20d"}
DEFAULT_MIN_RAW_N = 200
DEFAULT_MIN_EFFECTIVE_N = 100
DEFAULT_MIN_DIRECTIONAL_N = 20
VALIDATION_POLICY_V2 = "validation-policy-v2"
SAMPLE_ORIGINS = {"HISTORICAL_BACKFILL", "HISTORICAL_LOCKED_TEST",
                  "PROSPECTIVE_CONFIRMATION"}


def _d(value: Any) -> Decimal | None:
    if value in (None, ""):
        return None
    try:
        return value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _stored(value: Any) -> str | None:
    d = _d(value)
    return None if d is None else str(d)


def _id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def _sample_origin(value: str | None, *, allow_live: bool = False) -> str:
    value = str(value or ("LIVE" if allow_live else "PROSPECTIVE_CONFIRMATION")).upper()
    allowed = SAMPLE_ORIGINS | ({"LIVE"} if allow_live else set())
    if value not in allowed:
        raise ValueError(f"非法数据来源标签: {value}")
    return value


def save_equity_bars(db: Any, bars: Iterable[dict[str, Any]], *,
                     source: str = "IBKR_historical_5m",
                     data_origin: str = "LIVE") -> int:
    """幂等写入主库 5m 正股 bars；空/无时间 bars 被忽略。"""
    rows = []
    fetched = _now()
    data_origin = _sample_origin(data_origin, allow_live=True)
    for bar in bars:
        symbol = str(bar.get("symbol") or "").strip().upper()
        start = bar.get("bar_start") or bar.get("date") or bar.get("time")
        if not symbol or not start:
            continue
        rows.append((symbol, str(start), bar.get("bar_end"), _stored(bar.get("o")),
                     _stored(bar.get("h")), _stored(bar.get("l")), _stored(bar.get("c")),
                     _stored(bar.get("v")), bar.get("session"),
                     1 if bar.get("use_rth", True) else 0, source, fetched, data_origin))
    if not rows:
        return 0
    with db.connect() as conn:
        conn.executemany(
            "INSERT INTO equity_bars_5m(symbol,bar_start,bar_end,o,h,l,c,v,session,use_rth,source,fetched_at,data_origin) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(symbol,bar_start,source) DO UPDATE SET bar_end=excluded.bar_end,o=excluded.o,"
            "h=excluded.h,l=excluded.l,c=excluded.c,v=excluded.v,session=excluded.session,"
            "use_rth=excluded.use_rth,fetched_at=excluded.fetched_at,data_origin=excluded.data_origin",
            rows,
        )
    return len(rows)


def load_equity_bars(db: Any, symbol: str, *, source: str | None = None,
                     start: str | None = None, end: str | None = None,
                     limit: int = 20000) -> list[dict[str, Any]]:
    where = ["symbol=?"]
    params: list[Any] = [symbol.upper()]
    if source:
        where.append("source=?")
        params.append(source)
    if start:
        where.append("bar_start>=?")
        params.append(start)
    if end:
        where.append("bar_start<=?")
        params.append(end)
    params.append(max(1, min(int(limit), 100000)))
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT * FROM equity_bars_5m WHERE " + " AND ".join(where)
            + " ORDER BY bar_start,CASE source WHEN 'IBKR_historical_5m' THEN 0 "
            "WHEN 'FUTU_historical_5m' THEN 1 ELSE 2 END LIMIT ?", params).fetchall()
    result = [dict(row) for row in rows]
    if source:
        return result
    # 同一时点多源时IBKR优先、富途补缺；不把两源当两根bar扩大样本量。
    deduped: dict[str, dict[str, Any]] = {}
    for row in result:
        deduped.setdefault(str(row.get("bar_start")), row)
    return list(deduped.values())


def _outcome_row(signal: dict[str, Any], outcome: dict[str, Any]) -> tuple[Any, ...]:
    return (
        _id("hlo"), signal.get("signal_id"), signal_snapshot_hash(signal),
        signal.get("model") or "", signal.get("regime") or signal.get("session") or "",
        outcome.get("direction") or signal.get("state") or "NEUTRAL",
        outcome.get("canonical_symbol") or signal.get("canonical_symbol") or signal.get("coin") or "",
        signal.get("actionable_at") or signal.get("signal_at"), outcome.get("horizon"),
        outcome.get("entry_time"), outcome.get("entry_price"), outcome.get("outcome_time"),
        outcome.get("outcome_price"), outcome.get("spy_start_price"), outcome.get("spy_end_price"),
        outcome.get("return_pct"), outcome.get("spy_return_pct"), outcome.get("alpha_pct"),
        outcome.get("mfe_pct"), outcome.get("mae_pct"), outcome.get("status") or "PENDING",
        outcome.get("quality_note"), outcome.get("calculation_version") or CALCULATION_VERSION,
        _now(), _sample_origin(signal.get("data_origin"), allow_live=True),
        signal.get("validation_commitment_id"),
    )


def save_signal_outcomes(db: Any, signal: dict[str, Any], outcomes: Iterable[dict[str, Any]]) -> int:
    """按 signal+horizon+calculation_version 幂等保存，不覆盖旧算法版本。"""
    rows = [_outcome_row(signal, row) for row in outcomes]
    if not rows:
        return 0
    with db.connect() as conn:
        conn.executemany(
            "INSERT INTO hl_signal_outcomes(id,signal_id,signal_snapshot_hash,model,regime,direction,"
            "canonical_symbol,actionable_at,horizon,entry_time,entry_price,outcome_time,outcome_price,"
            "spy_start_price,spy_end_price,return_pct,spy_return_pct,alpha_pct,mfe_pct,mae_pct,status,"
            "quality_note,calculation_version,calculated_at,data_origin,validation_commitment_id) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(signal_id,horizon,calculation_version) DO UPDATE SET signal_snapshot_hash=excluded.signal_snapshot_hash,"
            "entry_time=excluded.entry_time,entry_price=excluded.entry_price,outcome_time=excluded.outcome_time,"
            "outcome_price=excluded.outcome_price,spy_start_price=excluded.spy_start_price,spy_end_price=excluded.spy_end_price,"
            "return_pct=excluded.return_pct,spy_return_pct=excluded.spy_return_pct,alpha_pct=excluded.alpha_pct,"
            "mfe_pct=excluded.mfe_pct,mae_pct=excluded.mae_pct,status=excluded.status,quality_note=excluded.quality_note,"
            "calculated_at=excluded.calculated_at,data_origin=excluded.data_origin,"
            "validation_commitment_id=excluded.validation_commitment_id",
            rows,
        )
    return len(rows)


def calculate_and_save_signal(db: Any, signal: dict[str, Any], bars: list[dict[str, Any]],
                              spy_bars: list[dict[str, Any]], *, horizons: Iterable[str] = OUTCOME_HORIZONS) -> dict[str, Any]:
    outcomes = [calculate_outcome(signal, bars, spy_bars, horizon) for horizon in horizons]
    save_signal_outcomes(db, signal, outcomes)
    return {"signal_id": signal.get("signal_id"), "outcomes": outcomes,
            "saved": len(outcomes), "calculation_version": CALCULATION_VERSION}


def run_signal_outcomes(main_db: Any, hl_db: Any, *, limit: int = 200) -> dict[str, Any]:
    """读取独立 HL 库中最近信号，使用主库 5m bar 计算结果。"""
    if not hl_db.available():
        return {"status": "UNAVAILABLE", "signals": 0, "saved": 0}
    with hl_db.connect(read_only=True) as conn:
        rows = conn.execute(
            "SELECT * FROM hl_signal_episodes WHERE state IN ('BULLISH','BEARISH') "
            "AND quality_status='READY' ORDER BY signal_at DESC LIMIT ?",
            (max(1, min(int(limit), 1000)),),
        ).fetchall()
    total = saved = 0
    for row in rows:
        signal = dict(row)
        signal["state"] = signal.get("state")
        symbol = str(signal.get("canonical_symbol") or "").upper()
        if not symbol:
            continue
        signal["session"] = signal.get("session") or signal.get("regime")
        bars = load_equity_bars(main_db, symbol)
        spy = load_equity_bars(main_db, "SPY")
        if not bars:
            # 保留 PENDING 记录，待独立 bar 任务补齐后可重算。
            bars, spy = [], []
        result = calculate_and_save_signal(main_db, signal, bars, spy)
        total += 1
        saved += result["saved"]
    return {"status": "SUCCESS", "signals": total, "saved": saved,
            "calculation_version": CALCULATION_VERSION}


def create_validation_commitment(db: Any, *, model: str, rules_version: str,
                                 rules_hash: str, policy_version: str | None,
                                 policy_hash: str | None, locked_test_start: str,
                                 locked_test_end: str,
                                 feature_manifest: dict[str, Any],
                                 gate: dict[str, Any],
                                 development_start: str | None = None,
                                 development_end: str | None = None,
                                 validation_start: str | None = None,
                                 validation_end: str | None = None,
                                 validation_policy_version: str = VALIDATION_POLICY_V2,
                                 committed_at: str | None = None) -> dict[str, Any]:
    """在读取锁定测试结果前写入不可变承诺；相同内容幂等复用。"""
    if str(locked_test_start) >= str(locked_test_end):
        raise ValueError("locked_test_start 必须早于 locked_test_end")
    material = {
        "validation_policy_version": validation_policy_version,
        "model": model, "rules_version": rules_version, "rules_hash": rules_hash,
        "policy_version": policy_version, "policy_hash": policy_hash,
        "development_start": development_start, "development_end": development_end,
        "validation_start": validation_start, "validation_end": validation_end,
        "locked_test_start": locked_test_start, "locked_test_end": locked_test_end,
        "feature_manifest": feature_manifest, "gate": gate,
    }
    digest = hashlib.sha256(_json(material).encode()).hexdigest()
    commitment_id = f"hlvc_{digest[:24]}"
    committed_at = committed_at or _now()
    with db.connect() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO hl_validation_commitments(commitment_id,validation_policy_version,model,"
            "rules_version,rules_hash,policy_version,policy_hash,development_start,development_end,"
            "validation_start,validation_end,locked_test_start,locked_test_end,feature_manifest_json,"
            "gate_json,commitment_hash,committed_at,status) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (commitment_id, validation_policy_version, model, rules_version, rules_hash,
             policy_version, policy_hash, development_start, development_end,
             validation_start, validation_end, locked_test_start, locked_test_end,
             _json(feature_manifest), _json(gate), digest, committed_at, "COMMITTED"),
        )
        row = conn.execute("SELECT * FROM hl_validation_commitments WHERE commitment_id=?",
                           (commitment_id,)).fetchone()
    return dict(row)


def record_validation_run(db: Any, *, model: str, rules_version: str, rules_hash: str,
                          policy_version: str | None, policy_hash: str | None,
                          rows: Iterable[dict[str, Any]], verdict: str | None = None,
                          note: str | None = None,
                          baseline_rows: Iterable[dict[str, Any]] | None = None,
                          min_raw_n: int = DEFAULT_MIN_RAW_N,
                          min_effective_n: int = DEFAULT_MIN_EFFECTIVE_N,
                          min_directional_n: int = DEFAULT_MIN_DIRECTIONAL_N,
                          validation_policy_version: str = "validation-policy-v1",
                          sample_origin: str = "PROSPECTIVE_CONFIRMATION",
                          commitment_id: str | None = None) -> dict[str, Any]:
    """写入按方向/窗口的验证结果；调用方必须先冻结规则和准入群体。"""
    run_id = _id("hlvr")
    rows = list(rows)
    baseline_rows = list(baseline_rows or [])
    sample_origin = _sample_origin(sample_origin)
    row_origins = {str(r.get("data_origin") or sample_origin).upper() for r in rows}
    if row_origins - {sample_origin}:
        raise ValueError("验证样本 data_origin 与 run.sample_origin 不一致")
    if validation_policy_version == VALIDATION_POLICY_V2:
        if not commitment_id:
            raise ValueError("validation-policy-v2 必须绑定锁定承诺")
        with db.connect() as conn:
            commitment = conn.execute(
                "SELECT * FROM hl_validation_commitments WHERE commitment_id=? AND status='COMMITTED'",
                (commitment_id,)).fetchone()
        if not commitment:
            raise ValueError("锁定承诺不存在或无效")
        if (commitment["model"] != model or commitment["rules_version"] != rules_version
                or commitment["rules_hash"] != rules_hash):
            raise ValueError("验证运行与锁定承诺的模型/规则版本不一致")
        for row in rows:
            event_time = str(row.get("entry_time") or row.get("actionable_at")
                             or row.get("signal_at") or "")
            if sample_origin == "HISTORICAL_LOCKED_TEST" and not (
                    str(commitment["locked_test_start"]) <= event_time
                    <= str(commitment["locked_test_end"])):
                raise ValueError("锁定测试样本超出已承诺区间")
            if sample_origin == "PROSPECTIVE_CONFIRMATION" and event_time < str(
                    commitment["committed_at"]):
                raise ValueError("前瞻确认样本早于锁定承诺时点")
    summary = validation_summary(rows)
    gate = promotion_gate(rows, baseline_rows=baseline_rows, model=model,
                          min_raw_n=min_raw_n, min_effective_n=min_effective_n,
                          min_directional_n=min_directional_n)
    if verdict is None:
        verdict = gate["verdict"]
    summary["promotion_gate"] = gate
    started = _now()
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO hl_validation_runs(run_id,model,rules_version,rules_hash,policy_version,policy_hash,"
            "started_at,finished_at,status,sample_json,verdict,note,validation_policy_version,"
            "sample_origin,commitment_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (run_id, model, rules_version, rules_hash, policy_version, policy_hash,
             started, _now(), "COMPLETE", _json(summary), verdict, note,
             validation_policy_version, sample_origin, commitment_id),
        )
        for item in summary.get("groups", []):
            conn.execute(
                "INSERT INTO hl_validation_results(id,run_id,comparison_group,horizon,symbol,direction,raw_n,effective_n,"
                "return_pct,alpha_pct,profit_factor,mae_pct,max_drawdown_pct,ci_low,ci_high,metrics_json) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (_id("hlres"), run_id, f"{model}:{item['direction']}", item["horizon"], None,
                 item["direction"], item["raw_n"], item["effective_n"], item.get("average_return"),
                item.get("average_alpha"), item.get("profit_factor"), None, None,
                 item.get("alpha_ci90_low"), item.get("alpha_ci90_high"), _json(item)),
            )
        for item in gate.get("baseline_comparisons", []):
            conn.execute(
                "INSERT INTO hl_validation_results(id,run_id,comparison_group,horizon,symbol,direction,raw_n,effective_n,"
                "return_pct,alpha_pct,profit_factor,mae_pct,max_drawdown_pct,ci_low,ci_high,metrics_json) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (_id("hlres"), run_id, "PAIRED_BASELINE", item.get("horizon"), item.get("symbol"),
                 item.get("direction"), item.get("raw_n", 0), item.get("effective_n", 0),
                 item.get("mean_improvement"), item.get("mean_avoidance"), None, None, None,
                 item.get("ci_low"), item.get("ci_high"), _json(item)),
            )
    return {"run_id": run_id, "summary": summary, "verdict": verdict, "promotion_gate": gate}


def fast_track_verdict(*, historical_gate: dict[str, Any],
                       prospective_days: int, required_days: int,
                       severe_gaps: int = 0, version_drift: bool = False,
                       pit_violation: bool = False,
                       result_reversal: bool = False) -> dict[str, Any]:
    """validation-policy-v2 两层门；不把少量前瞻样本伪装成统计复现。"""
    reasons: list[str] = []
    if str(historical_gate.get("verdict")) != "PASS":
        reasons.append("historical_locked_test_failed")
    if int(prospective_days) < int(required_days):
        reasons.append(f"prospective_days<{int(required_days)}")
    if int(severe_gaps) > 0:
        reasons.append("severe_data_gaps")
    if version_drift:
        reasons.append("version_drift")
    if pit_violation:
        reasons.append("pit_violation")
    if result_reversal:
        reasons.append("result_reversal")
    return {"verdict": "ELIGIBLE" if not reasons else "SHADOW",
            "historical_gate": historical_gate,
            "prospective_days": int(prospective_days),
            "required_days": int(required_days), "reasons": reasons,
            "note": "前瞻窗口只确认线上稳定性，不声称独立复现历史统计效力"}


def _comparison_key(row: dict[str, Any]) -> tuple[str, str, str, str]:
    return (str(row.get("canonical_symbol") or row.get("coin") or "").upper(),
            str(row.get("horizon") or ""),
            str(row.get("entry_time") or row.get("actionable_at") or row.get("signal_at") or "")[:16],
            str(row.get("direction") or "").upper())


def promotion_gate(rows: Iterable[dict[str, Any]], *, baseline_rows: Iterable[dict[str, Any]] = (),
                   model: str | None = None, min_raw_n: int = DEFAULT_MIN_RAW_N,
                   min_effective_n: int = DEFAULT_MIN_EFFECTIVE_N,
                   min_directional_n: int = DEFAULT_MIN_DIRECTIONAL_N) -> dict[str, Any]:
    """按冻结规则版本评估晋级门槛。

    只使用 COMPLETE 的 BULLISH/BEARISH episode；neutral 是退出事件，不作为
    总体基准。baseline 必须用同一 symbol、决策时间和 horizon 配对，不能拿
    不同时间的市场收益拼成对照组。
    """
    eligible = [dict(r) for r in rows
                if str(r.get("status") or "").upper() == "COMPLETE"
                and str(r.get("direction") or "").upper() in {"BULLISH", "BEARISH"}
                and (model is None or str(r.get("model")) == model)]
    baseline = {_comparison_key(dict(r)): dict(r) for r in baseline_rows
                if str(r.get("status") or "").upper() == "COMPLETE"}
    primary_horizon = PRIMARY_HORIZONS.get(str(model or "").upper())
    selected = [r for r in eligible if primary_horizon is None or str(r.get("horizon")) == primary_horizon]
    reasons: list[str] = []
    effective_n = len({_comparison_key(r)[:3] for r in selected})
    raw_n = len(selected)
    directions = {d: sum(1 for r in selected if str(r.get("direction")).upper() == d)
                  for d in ("BULLISH", "BEARISH")}
    if raw_n < int(min_raw_n):
        reasons.append(f"raw_n<{int(min_raw_n)}")
    if effective_n < int(min_effective_n):
        reasons.append(f"effective_n<{int(min_effective_n)}")
    if any(n < int(min_directional_n) for n in directions.values()):
        reasons.append(f"directional_n<{int(min_directional_n)}")
    comparisons: list[dict[str, Any]] = []
    improvements: list[Decimal] = []
    avoidance: list[Decimal] = []
    mae_deteriorations: list[Decimal] = []
    for row in selected:
        ref = baseline.get(_comparison_key(row))
        current = _d(row.get("return_pct")); base = _d((ref or {}).get("return_pct"))
        if current is None or base is None:
            continue
        direction = str(row.get("direction") or "").upper()
        # bullish 以超额收益改善衡量；bearish 不当作做空收益，而是衡量
        # 相对基线减少的后续多头损失。
        delta = current - base
        item = next((x for x in comparisons
                     if x["symbol"] == str(row.get("canonical_symbol") or row.get("coin") or "").upper()
                     and x["horizon"] == str(row.get("horizon"))
                     and x["direction"] == direction), None)
        if item is None:
            item = {"symbol": str(row.get("canonical_symbol") or row.get("coin") or "").upper(),
                    "horizon": str(row.get("horizon")), "direction": direction,
                    "values": [], "raw_n": 0, "effective_n": 0}
            comparisons.append(item)
        item["values"].append(delta)
        item["raw_n"] += 1
        item["effective_n"] = len({_comparison_key(x)[:3] for x in selected
                                   if str(x.get("canonical_symbol") or x.get("coin") or "").upper() == item["symbol"]
                                   and str(x.get("horizon")) == item["horizon"]
                                   and str(x.get("direction")).upper() == direction})
        if direction == "BULLISH":
            improvements.append(delta)
        else:
            avoidance.append(-delta)
        current_mae, base_mae = _d(row.get("mae_pct")), _d((ref or {}).get("mae_pct"))
        if current_mae is not None and base_mae is not None:
            mae_deteriorations.append(abs(current_mae) - abs(base_mae))
    baseline_comparisons = []
    for item in comparisons:
        vals = item.pop("values")
        mean = sum(vals, Decimal("0")) / len(vals) if vals else None
        low, high = _decimal_ci(vals)
        item.update({"mean_improvement": str(mean) if mean is not None else None,
                     "mean_avoidance": str(mean) if mean is not None and item["direction"] == "BEARISH" else None,
                     "ci_low": str(low) if low is not None else None,
                     "ci_high": str(high) if high is not None else None})
        baseline_comparisons.append(item)
    mean_improvement = (sum(improvements, Decimal("0")) / len(improvements)
                        if improvements else None)
    mean_avoidance = (sum(avoidance, Decimal("0")) / len(avoidance)
                      if avoidance else None)
    all_delta = improvements + avoidance
    ci_low, ci_high = _decimal_ci(all_delta)
    if not baseline_rows:
        reasons.append("paired_baseline_missing")
    if mean_improvement is None or mean_improvement < Decimal("0.10"):
        reasons.append("bullish_improvement_below_10pct")
    if ci_low is None or ci_low <= 0:
        reasons.append("block_bootstrap_ci90_low_not_positive")
    # 核心指标的恶化限制按同一 signal 做配对；不能用绝对 MAE 水平
    # 代替“相对 baseline 是否恶化”。
    if not mae_deteriorations:
        reasons.append("paired_core_mae_missing")
    elif sum(mae_deteriorations, Decimal("0")) / len(mae_deteriorations) > Decimal("0.05"):
        reasons.append("core_mae_deterioration_over_5pct")
    return {"verdict": "PASS" if not reasons else "FAIL", "model": model,
            "primary_horizon": primary_horizon, "raw_n": raw_n,
            "effective_n": effective_n, "directional_n": directions,
            "mean_bullish_improvement": str(mean_improvement) if mean_improvement is not None else None,
            "mean_bearish_avoidance": str(mean_avoidance) if mean_avoidance is not None else None,
            "ci90_low": str(ci_low) if ci_low is not None else None,
            "ci90_high": str(ci_high) if ci_high is not None else None,
            "reasons": reasons, "baseline_comparisons": baseline_comparisons}


def _decimal_ci(values: list[Decimal], iterations: int = 500,
                seed: int = 23) -> tuple[Decimal | None, Decimal | None]:
    """小型 block bootstrap；值已按 symbol×decision-time 配对后传入。"""
    if not values:
        return None, None
    if len(values) == 1:
        return values[0], values[0]
    rng = random.Random(seed)
    draws = []
    for _ in range(max(100, iterations)):
        sample = [values[rng.randrange(len(values))] for _ in values]
        draws.append(sum(sample, Decimal("0")) / len(sample))
    draws.sort()
    return draws[int(len(draws) * 0.05)], draws[min(len(draws) - 1, int(len(draws) * 0.95))]


def auto_suspend_after_failed_windows(db: Any, *, model: str, rules_version: str,
                                      rules_hash: str, policy_hash: str | None = None,
                                      failed_windows: int = 2) -> dict[str, Any]:
    """连续两个完整验证窗口失败时自动写 SUSPENDED；不会删除历史结果。"""
    where = ["model=?", "rules_version=?", "rules_hash=?", "status='COMPLETE'"]
    params: list[Any] = [model, rules_version, rules_hash]
    if policy_hash is not None:
        where.append("policy_hash=?")
        params.append(policy_hash)
    with db.connect() as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM hl_validation_runs WHERE " + " AND ".join(where) +
            " ORDER BY finished_at DESC LIMIT ?", (*params, max(2, int(failed_windows)))).fetchall()]
    consecutive = 0
    for row in rows:
        if str(row.get("verdict") or "").upper() != "FAIL":
            break
        consecutive += 1
    if consecutive < int(failed_windows):
        return {"status": "UNCHANGED", "consecutive_failed": consecutive}
    promotion = set_promotion(db, model=model, rules_version=rules_version,
                              rules_hash=rules_hash, policy_version=rows[0].get("policy_version"),
                              policy_hash=policy_hash, state="SUSPENDED",
                              evaluation_run_id=rows[0].get("run_id"),
                              reason=f"连续{consecutive}个验证窗口未通过")
    return {"status": "SUSPENDED", "consecutive_failed": consecutive,
            "promotion": promotion}


def record_monthly_health(main_db: Any, hl_db: Any, *, month: str | None = None) -> dict[str, Any]:
    """生成独立的月度运行报告；HL 读失败时主库只记 UNAVAILABLE。"""
    month = month or datetime.now(timezone.utc).strftime("%Y-%m")
    generated = _now()
    if not hl_db.available():
        metrics = {"available": False}
        status = "UNAVAILABLE"
        note = "Hyperliquid 独立库不可读；未把故障写入主链路健康状态"
    else:
        with hl_db.connect(read_only=True) as conn:
            def count(sql: str, params: tuple[Any, ...] = ()) -> int:
                return int(conn.execute(sql, params).fetchone()[0] or 0)
            metrics = {
                "available": True,
                "assets": count("SELECT COUNT(*) FROM hl_assets"),
                "signals": count("SELECT COUNT(*) FROM hl_signal_episodes WHERE signal_at LIKE ?", (month + "%",)),
                "ready_signals": count("SELECT COUNT(*) FROM hl_signal_episodes WHERE signal_at LIKE ? AND quality_status='READY'", (month + "%",)),
                "incomplete_gaps": count("SELECT COUNT(*) FROM hl_data_gaps WHERE created_at LIKE ? AND status='INCOMPLETE'", (month + "%",)),
                "tracked_wallets": count("SELECT COUNT(*) FROM hl_wallet_candidates WHERE status IN ('CANDIDATE','ELIGIBLE')"),
            }
        status = "READY" if metrics["signals"] == 0 or metrics["ready_signals"] > 0 else "LIMITED"
        note = "月度采集/质量摘要；不代表策略收益或现实股票真值"
    with main_db.connect() as conn:
        conn.execute(
            "INSERT INTO hl_health_reports(report_month,generated_at,status,metrics_json,note) "
            "VALUES(?,?,?,?,?) ON CONFLICT(report_month) DO UPDATE SET generated_at=excluded.generated_at,"
            "status=excluded.status,metrics_json=excluded.metrics_json,note=excluded.note",
            (month, generated, status, _json(metrics), note),
        )
    return {"report_month": month, "status": status, "metrics": metrics, "note": note}


def active_promotion(db: Any, *, model: str, rules_version: str, rules_hash: str,
                     policy_hash: str | None = None) -> dict[str, Any] | None:
    where = ["model=?", "rules_version=?", "rules_hash=?", "state='ACTIVE'"]
    params: list[Any] = [model, rules_version, rules_hash]
    if policy_hash is not None:
        where.append("policy_hash=?")
        params.append(policy_hash)
    with db.connect() as conn:
        row = conn.execute("SELECT * FROM hl_model_promotions WHERE " + " AND ".join(where)
                           + " ORDER BY updated_at DESC LIMIT 1", params).fetchone()
    return dict(row) if row else None


def set_promotion(db: Any, *, model: str, rules_version: str, rules_hash: str,
                  policy_version: str | None, policy_hash: str | None, state: str,
                  evaluation_run_id: str | None = None, approved_by: str | None = None,
                  reason: str | None = None) -> dict[str, Any]:
    """写入不可覆盖的模型晋级状态；ACTIVE 必须带验收 run 与人工批准人。"""
    state = str(state or "").upper()
    if state not in {"SHADOW", "ELIGIBLE", "ACTIVE", "SUSPENDED"}:
        raise ValueError("模型状态必须是 SHADOW/ELIGIBLE/ACTIVE/SUSPENDED")
    if state == "ACTIVE" and (not evaluation_run_id or not approved_by):
        raise ValueError("ACTIVE 晋级必须提供 evaluation_run_id 和 approved_by")
    now = _now()
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO hl_model_promotions(model,rules_version,rules_hash,policy_version,policy_hash,state,"
            "evaluation_run_id,approved_by,approved_at,reason,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(model,rules_version,rules_hash,policy_hash) DO UPDATE SET state=excluded.state,"
            "evaluation_run_id=excluded.evaluation_run_id,approved_by=excluded.approved_by,"
            "approved_at=excluded.approved_at,reason=excluded.reason,updated_at=excluded.updated_at",
            (model, rules_version, rules_hash, policy_version, policy_hash, state, evaluation_run_id,
             approved_by, now if approved_by else None, reason, now),
        )
    return {"model": model, "rules_version": rules_version, "rules_hash": rules_hash,
            "policy_version": policy_version, "policy_hash": policy_hash, "state": state,
            "evaluation_run_id": evaluation_run_id, "approved_by": approved_by, "updated_at": now}
