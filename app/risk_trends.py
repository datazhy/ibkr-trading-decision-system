"""风险趋势与降采样（纯函数）。

账户评估每分钟一条，90 天约 13 万条 —— 直接返回既拖垮前端，也没人看得懂。
所以按时间桶聚合。**聚合的唯一危险是把峰值抹平**：一小时内风险冲到 critical 又
回落，只报平均分就等于告诉用户「这一小时很平静」。所以每个桶同时给出：

    last  —— 桶末值（趋势线用它，避免锯齿）
    avg   —— 桶均值
    max   —— 桶内最高分
    highest_risk_level —— 桶内到达过的最高等级（**不是**对平均分反查等级）

原始数据一条不删，聚合只发生在查询时。数据量到不了需要物化汇总表的量级
（SQLite 上 13 万行的分桶聚合在毫秒级），过早建汇总表只会多一处需要同步的真相。
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

#: 预设区间 -> 默认聚合粒度。24 小时用原始分钟级，更长区间逐级放粗。
RANGE_PRESETS = {
    "24h": (timedelta(hours=24), "raw"),
    "7d": (timedelta(days=7), "1h"),
    "30d": (timedelta(days=30), "6h"),
    "90d": (timedelta(days=90), "1d"),
}

_INTERVAL_SECONDS = {
    "raw": 0, "1m": 60, "5m": 300, "15m": 900, "1h": 3600,
    "6h": 21600, "12h": 43200, "1d": 86400,
}

_LEVEL_RANK = {"low": 0, "moderate": 1, "high": 2, "critical": 3, "blocked": 4,
               "supportive": 0, "neutral": 1, "cautious": 2, "stressed": 3}


def _d(v: Any) -> Decimal | None:
    if v is None or v == "":
        return None
    if isinstance(v, Decimal):
        return v
    try:
        return Decimal(str(v))
    except (InvalidOperation, ValueError, TypeError):
        return None


def level_rank(level: str | None) -> int:
    return _LEVEL_RANK.get((level or "").lower(), -1)


def worst_level(levels: list[str | None]) -> str | None:
    known = [l for l in levels if l and level_rank(l) >= 0]
    if not known:
        return None
    return max(known, key=level_rank)


def resolve_range(range_key: str | None, start: str | None, end: str | None,
                  interval: str | None, *, now: datetime | None = None
                  ) -> tuple[datetime, datetime, str, str]:
    """解析时间范围与粒度。返回 (start, end, interval, range_key)。

    自定义区间没给 interval 时按跨度自动选，规则与预设一致 ——
    否则用户查 90 天自定义区间会拿到 13 万个点。
    """
    now = now or datetime.now(timezone.utc)
    if start or end:
        s = _parse_dt(start) or (now - timedelta(days=7))
        e = _parse_dt(end) or now
        if e < s:
            s, e = e, s
        key = "custom"
        iv = interval or _auto_interval(e - s)
        return s, e, iv, key

    key = (range_key or "24h").lower()
    span, default_iv = RANGE_PRESETS.get(key, RANGE_PRESETS["24h"])
    if key not in RANGE_PRESETS:
        key = "24h"
    return now - span, now, (interval or default_iv), key


def _auto_interval(span: timedelta) -> str:
    hours = span.total_seconds() / 3600
    if hours <= 24:
        return "raw"
    if hours <= 24 * 7:
        return "1h"
    if hours <= 24 * 30:
        return "6h"
    return "1d"


def _parse_dt(v: str | None) -> datetime | None:
    if not v:
        return None
    try:
        dt = datetime.fromisoformat(v.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def bucket_of(ts: str, interval: str) -> str | None:
    """时间戳所属的桶起点（ISO）。interval='raw' 时返回原时间戳。"""
    if interval == "raw":
        return ts
    secs = _INTERVAL_SECONDS.get(interval)
    if not secs:
        return ts
    dt = _parse_dt(ts)
    if dt is None:
        return None
    epoch = int(dt.timestamp())
    return datetime.fromtimestamp(epoch - epoch % secs,
                                  tz=timezone.utc).isoformat()


def _stats(values: list[Decimal]) -> dict:
    if not values:
        return {"last": None, "avg": None, "max": None, "min": None}
    total = sum(values, Decimal("0"))
    return {
        "last": str(values[-1]),
        "avg": str((total / Decimal(len(values))).quantize(Decimal("0.1"))),
        "max": str(max(values)),
        "min": str(min(values)),
    }


def aggregate_account(rows: list[dict], interval: str) -> list[dict]:
    """账户评估序列 -> 分桶聚合。rows 需按 data_as_of 正序。"""
    buckets: dict[str, dict] = {}
    order: list[str] = []
    for r in rows:
        b = bucket_of(r.get("data_as_of") or "", interval)
        if b is None:
            continue
        if b not in buckets:
            buckets[b] = {"scores": [], "levels": [], "components": {},
                          "reliable": 0, "n": 0}
            order.append(b)
        slot = buckets[b]
        slot["n"] += 1
        s = _d(r.get("risk_score"))
        if s is not None:
            slot["scores"].append(s)
        slot["levels"].append(r.get("risk_level"))
        if r.get("reliable"):
            slot["reliable"] += 1
        comp = r.get("component_scores")
        if isinstance(comp, str):
            try:
                comp = json.loads(comp)
            except (TypeError, ValueError):
                comp = {}
        for k, v in (comp or {}).items():
            dv = _d(v)
            if dv is not None:
                slot["components"].setdefault(k, []).append(dv)

    out = []
    for b in order:
        slot = buckets[b]
        out.append({
            "bucket": b, "samples": slot["n"],
            "account_risk_score": _stats(slot["scores"]),
            # 桶内最高等级，不是对均分反查 —— 平均值会把 critical 尖峰抹掉
            "highest_risk_level": worst_level(slot["levels"]),
            "last_risk_level": next((l for l in reversed(slot["levels"]) if l), None),
            "component_scores": {k: _stats(v) for k, v in slot["components"].items()},
            "reliable_ratio": (f"{slot['reliable']}/{slot['n']}"),
        })
    return out


def aggregate_market(rows: list[dict], interval: str) -> list[dict]:
    """市场评估序列 -> 分桶聚合。

    注意市场评估按**交易日**幂等，天然是日粒度；小于 1 天的 interval 不会让它
    变密，只会让多数桶为空。API 必须把这个差异写在响应里。
    """
    buckets: dict[str, dict] = {}
    order: list[str] = []
    for r in rows:
        raw = r.get("data_as_of") or ""
        ts = raw if "T" in raw else f"{raw}T00:00:00+00:00"
        b = bucket_of(ts, interval if interval != "raw" else "1d")
        if b is None:
            continue
        if b not in buckets:
            buckets[b] = {"scores": [], "states": [], "components": {}, "n": 0}
            order.append(b)
        slot = buckets[b]
        slot["n"] += 1
        s = _d(r.get("market_risk_score"))
        if s is not None:
            slot["scores"].append(s)
        slot["states"].append(r.get("market_state"))
        comp = r.get("component_scores")
        if isinstance(comp, str):
            try:
                comp = json.loads(comp)
            except (TypeError, ValueError):
                comp = {}
        for k, v in (comp or {}).items():
            dv = _d(v)
            if dv is not None:
                slot["components"].setdefault(k, []).append(dv)

    out = []
    for b in order:
        slot = buckets[b]
        out.append({
            "bucket": b, "samples": slot["n"],
            "market_risk_score": _stats(slot["scores"]),
            "highest_market_state": worst_level(slot["states"]),
            "last_market_state": next((s for s in reversed(slot["states"]) if s), None),
            "component_scores": {k: _stats(v) for k, v in slot["components"].items()},
        })
    return out


def find_gaps(buckets: list[dict], start: datetime, end: datetime,
              interval: str) -> list[dict]:
    """标出缺失区间。**空洞必须显式呈现** ——
    前端把两个相隔 8 小时的点直连成线，会让人以为中间一切正常。"""
    secs = _INTERVAL_SECONDS.get(interval, 0)
    if not secs:
        return _raw_gaps(buckets, start, end)
    have = {b["bucket"] for b in buckets}
    gaps, cur, run_start = [], int(start.timestamp()), None
    cur -= cur % secs
    end_ts = int(end.timestamp())
    while cur <= end_ts:
        iso = datetime.fromtimestamp(cur, tz=timezone.utc).isoformat()
        if iso in have:
            if run_start is not None:
                gaps.append({"start": run_start, "end": iso})
                run_start = None
        elif run_start is None:
            run_start = iso
        cur += secs
    if run_start is not None:
        gaps.append({"start": run_start, "end": end.isoformat()})
    return gaps


def _raw_gaps(buckets: list[dict], start: datetime, end: datetime,
              max_gap_minutes: int = 15) -> list[dict]:
    """原始粒度下：相邻点间隔超过阈值即视为缺失区间。"""
    gaps = []
    prev = start
    for b in buckets:
        cur = _parse_dt(b["bucket"])
        if cur is None:
            continue
        if (cur - prev).total_seconds() > max_gap_minutes * 60:
            gaps.append({"start": prev.isoformat(), "end": cur.isoformat()})
        prev = cur
    if (end - prev).total_seconds() > max_gap_minutes * 60:
        gaps.append({"start": prev.isoformat(), "end": end.isoformat()})
    return gaps


def amplification_series(account_buckets: list[dict],
                         market_buckets: list[dict]) -> list[dict]:
    """联合放大系数趋势。

    按「桶内账户最高等级 × 市场最高等级」给出一个粗粒度的关注度指示，
    **不是**对历史评估的重新计算 —— 历史的账户与市场评分保持原值不动。
    """
    mkt_by_day = {}
    for b in market_buckets:
        mkt_by_day[b["bucket"][:10]] = b
    out = []
    for b in account_buckets:
        m = mkt_by_day.get(b["bucket"][:10])
        acc_rank = level_rank(b.get("highest_risk_level"))
        mkt_rank = level_rank((m or {}).get("highest_market_state"))
        if acc_rank < 0 or mkt_rank < 0:
            factor = None
        else:
            factor = str((Decimal("1") + Decimal(acc_rank * mkt_rank) / Decimal("10")
                          ).quantize(Decimal("0.01")))
        out.append({"bucket": b["bucket"], "risk_amplification_factor": factor,
                    "account_level": b.get("highest_risk_level"),
                    "market_state": (m or {}).get("highest_market_state")})
    return out
