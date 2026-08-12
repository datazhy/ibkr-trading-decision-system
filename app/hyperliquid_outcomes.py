"""链上 episode 的现实正股结果与样本外统计。"""
from __future__ import annotations

import hashlib
import json
import random
import statistics
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable
from zoneinfo import ZoneInfo


CALCULATION_VERSION = "hl-outcomes-v1/2026-08"


def derive_actionable_at(signal_at: str, session: str, *, market_tz: str = "America/New_York") -> str | None:
    """现实交易锚点：RTH 取下一根 5m；其它时段取下一 RTH 开盘。"""
    from .us_market_calendar import REGULAR_OPEN, is_trading_day, regular_close_time
    dt = _dt(signal_at)
    if dt is None:
        return None
    tz = ZoneInfo(market_tz)
    et = dt.astimezone(tz)
    session = str(session or "").upper()
    day = et.date()
    if session in {"RTH", "REGULAR"} and is_trading_day(day):
        close = regular_close_time(day)
        if REGULAR_OPEN <= et.time() < close:
            minute = et.minute - (et.minute % 5) + 5
            candidate = et.replace(minute=minute, second=0, microsecond=0)
            if candidate.time() < close:
                return candidate.astimezone(timezone.utc).isoformat()
    if session in {"PRE", "PREMARKET"} and is_trading_day(day) and et.time() < REGULAR_OPEN:
        return et.replace(hour=REGULAR_OPEN.hour, minute=REGULAR_OPEN.minute,
                          second=0, microsecond=0).astimezone(timezone.utc).isoformat()
    day += timedelta(days=1)
    while not is_trading_day(day):
        day += timedelta(days=1)
    return datetime.combine(day, REGULAR_OPEN, tzinfo=tz).astimezone(timezone.utc).isoformat()


def _d(value: Any) -> Decimal | None:
    if value in (None, ""):
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None


def _dt(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        raw = str(value).replace("Z", "+00:00")
        try:
            dt = datetime.fromisoformat(raw)
        except ValueError:
            return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def signal_snapshot_hash(signal: dict[str, Any]) -> str:
    material = {k: signal.get(k) for k in (
        "signal_id", "model", "regime", "coin", "state", "score", "signal_at",
        "actionable_at", "wallet_group_version", "rules_version", "rules_hash")}
    return hashlib.sha256(json.dumps(material, sort_keys=True, default=str).encode()).hexdigest()


def _bar_time(bar: dict) -> datetime | None:
    return _dt(bar.get("bar_start") or bar.get("date") or bar.get("time"))


def choose_entry(signal: dict[str, Any], bars: list[dict], *, max_delay_minutes: int = 10
                 ) -> tuple[dict | None, str | None]:
    """现实可执行锚点：RTH 下一根 bar；非 RTH 下一 RTH 开盘 bar。"""
    action = _dt(signal.get("actionable_at") or signal.get("signal_at"))
    if action is None:
        return None, "缺少 actionable_at"
    session = str(signal.get("session") or signal.get("regime") or "").upper()
    candidates = []
    for bar in bars:
        t = _bar_time(bar)
        if t is None or t < action:
            continue
        if session in {"REGULAR", "RTH"}:
            candidates.append(bar)
        elif str(bar.get("session") or "").upper() == "REGULAR" or bar.get("use_rth"):
            candidates.append(bar)
    if not candidates:
        return None, "暂无下一可交易正股 bar"
    entry = sorted(candidates, key=lambda x: _bar_time(x) or datetime.max.replace(tzinfo=timezone.utc))[0]
    t = _bar_time(entry)
    if t and t > action + timedelta(minutes=max_delay_minutes):
        return None, "actionable_at 后超过 10 分钟未取得正股 bar"
    price = _d(entry.get("o") or entry.get("c"))
    if price is None or price <= 0:
        return None, "正股 entry bar 价格无效"
    return entry, None


def calculate_outcome(signal: dict[str, Any], bars: list[dict], spy_bars: list[dict],
                      horizon: str) -> dict[str, Any]:
    entry, note = choose_entry(signal, bars)
    base = {"signal_id": signal.get("signal_id"), "model": signal.get("model"),
            "regime": signal.get("regime"), "direction": signal.get("state"),
            "canonical_symbol": signal.get("canonical_symbol") or signal.get("coin"),
            "horizon": horizon, "calculation_version": CALCULATION_VERSION}
    if entry is None:
        return {**base, "status": "PENDING", "quality_note": note}
    entry_time = _bar_time(entry)
    entry_px = _d(entry.get("o") or entry.get("c"))
    if entry_time is None or entry_px is None or entry_px <= 0:
        return {**base, "status": "BLOCKED", "quality_note": "entry bar 不完整"}
    ordered = sorted((b for b in bars if _bar_time(b) and _bar_time(b) >= entry_time),
                     key=lambda b: _bar_time(b))
    if horizon in {"30m", "2h"}:
        minutes = 30 if horizon == "30m" else 120
        cutoff = entry_time + timedelta(minutes=minutes)
        # bar_start=14:00 是 14:00–14:05 的下一根 bar，不能拿它计算
        # 13:30→14:00 的 30m 结果；使用 cutoff 前最后一根完整 5m bar。
        window = [b for b in ordered if (_bar_time(b) or cutoff) < cutoff]
        if not window or (_bar_time(window[-1]) or entry_time) < cutoff - timedelta(minutes=5):
            return {**base, "status": "PENDING", "entry_time": entry_time.isoformat(),
                    "entry_price": str(entry_px), "quality_note": "前向 5m bar 尚未完整"}
        end_bar = window[-1]
    else:
        days = {"close": 1, "5d": 5, "10d": 10, "20d": 20, "40d": 40, "60d": 60}
        if horizon not in days:
            return {**base, "status": "BLOCKED", "quality_note": "未知结果窗口"}
        sessions = []
        for bar in ordered:
            t = _bar_time(bar)
            day = t.date() if t else None
            if day is not None and day not in sessions:
                sessions.append(day)
        target = sessions[min(days[horizon] - 1, len(sessions) - 1)] if sessions else None
        if target is None or len(sessions) < days[horizon]:
            return {**base, "status": "PENDING", "entry_time": entry_time.isoformat(),
                    "entry_price": str(entry_px), "quality_note": "前向交易日尚未完整"}
        end_bar = next((b for b in reversed(ordered) if (_bar_time(b) or entry_time).date() == target), None)
        window = [b for b in ordered if (_bar_time(b) or entry_time).date() <= target]
    end_px = _d(end_bar.get("c")) if end_bar else None
    if end_px is None or end_px <= 0:
        return {**base, "status": "BLOCKED", "quality_note": "终点价格无效"}
    all_window = window if horizon in {"30m", "2h"} else window
    highs = [_d(b.get("h")) for b in all_window]
    lows = [_d(b.get("l")) for b in all_window]
    highs = [x for x in highs if x is not None]
    lows = [x for x in lows if x is not None]
    ret = end_px / entry_px - 1
    end_time = _bar_time(end_bar)
    if horizon in {"30m", "2h"}:
        spy_ordered = sorted((b for b in spy_bars if _bar_time(b)), key=_bar_time)
        spy_start_bar = next((b for b in spy_ordered if _bar_time(b) == entry_time), None)
        spy_end_bar = next((b for b in spy_ordered if end_time and _bar_time(b) == end_time), None)
    else:
        spy_by_time = {_bar_time(b).date(): b for b in spy_bars if _bar_time(b)}
        spy_start_bar = spy_by_time.get(entry_time.date())
        spy_end_bar = spy_by_time.get(end_time.date()) if end_time else None
    spy_start = _d((spy_start_bar or {}).get("c"))
    spy_end = _d((spy_end_bar or {}).get("c"))
    spy_ret = spy_end / spy_start - 1 if spy_start and spy_end else None
    return {**base, "status": "COMPLETE", "entry_time": entry_time.isoformat(),
            "entry_price": str(entry_px), "outcome_time": end_time.isoformat() if end_time else None,
            "outcome_price": str(end_px), "return_pct": str(ret),
            "spy_start_price": str(spy_start) if spy_start else None,
            "spy_end_price": str(spy_end) if spy_end else None,
            "spy_return_pct": str(spy_ret) if spy_ret is not None else None,
            "alpha_pct": str(ret - spy_ret) if spy_ret is not None else None,
            "mfe_pct": str(max(highs) / entry_px - 1) if highs else None,
            "mae_pct": str(min(lows) / entry_px - 1) if lows else None,
            "quality_note": "RTH 正股 5m bar"}


def validation_summary(rows: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """按 model/horizon/direction 聚合；不把 neutral 退出事件当中性基准。"""
    groups: dict[tuple[str, str, str], list[dict]] = defaultdict(list)
    for row in rows:
        if row.get("status") != "COMPLETE":
            continue
        direction = str(row.get("direction") or "").upper()
        if direction not in {"BULLISH", "BEARISH"}:
            continue
        groups[(str(row.get("model")), str(row.get("horizon")), direction)].append(row)
    result = []
    for (model, horizon, direction), items in sorted(groups.items()):
        returns = [_d(r.get("return_pct")) for r in items]
        alphas = [_d(r.get("alpha_pct")) for r in items]
        returns = [x for x in returns if x is not None]
        alphas = [x for x in alphas if x is not None]
        wins = [x for x in returns if (x >= 0 if direction == "BULLISH" else x <= 0)]
        losses = [x for x in returns if (x < 0 if direction == "BULLISH" else x > 0)]
        gross_win = sum(wins, Decimal("0"))
        gross_loss = abs(sum(losses, Decimal("0")))
        ci_low, ci_high = block_bootstrap_ci(items, field="alpha_pct")
        result.append({"model": model, "horizon": horizon, "direction": direction,
                       "raw_n": len(items), "effective_n": len({
                           (r.get("canonical_symbol"), str(r.get("entry_time", ""))[:10])
                           for r in items}),
                       "average_return": str(sum(returns, Decimal("0")) / len(returns)) if returns else None,
                       "average_alpha": str(sum(alphas, Decimal("0")) / len(alphas)) if alphas else None,
                       "profit_factor": str(gross_win / gross_loss) if gross_loss else None,
                       "alpha_ci90_low": str(ci_low) if ci_low is not None else None,
                       "alpha_ci90_high": str(ci_high) if ci_high is not None else None})
    return {"groups": result, "note": "neutral 为状态退出事件，不作为无偏收益基准"}


def block_bootstrap_ci(rows: Iterable[dict[str, Any]], *, field: str = "alpha_pct",
                       iterations: int = 500, seed: int = 17) -> tuple[Decimal | None, Decimal | None]:
    """按 symbol×decision-date 重采样，避免重叠 episode 伪造有效样本量。"""
    grouped: dict[tuple[str, str], list[Decimal]] = defaultdict(list)
    for row in rows:
        value = _d(row.get(field))
        if value is None:
            continue
        key = (str(row.get("canonical_symbol") or row.get("coin") or ""),
               str(row.get("entry_time") or row.get("signal_at") or "")[:10])
        grouped[key].append(value)
    blocks = [sum(values, Decimal("0")) / len(values) for values in grouped.values() if values]
    if not blocks:
        return None, None
    if len(blocks) == 1:
        return blocks[0], blocks[0]
    rng = random.Random(seed)
    samples = []
    for _ in range(max(100, int(iterations))):
        draw = [blocks[rng.randrange(len(blocks))] for _ in blocks]
        samples.append(sum(draw, Decimal("0")) / len(draw))
    samples.sort()
    lo = samples[max(0, int(len(samples) * 0.05))]
    hi = samples[min(len(samples) - 1, int(len(samples) * 0.95))]
    return lo, hi
