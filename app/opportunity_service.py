"""机会发现与组合构建。

只读市场数据、生成确定性建议并创建持仓计划草稿；本模块没有也不会调用券商订单 API。
"""
from __future__ import annotations

import hashlib
import json
import statistics
import uuid
from datetime import datetime, timezone
from decimal import Decimal, ROUND_FLOOR
from typing import Any, Iterable

from . import indicators
from . import market_service
from . import technical
from .config import Config
from .db import Database, to_decimal
from .position_plans import PositionPlanService
from .stop_loss import compute_stop

RULES_VERSION = "OPPORTUNITY/2026-08-v1"
OUTCOME_VERSION = "OPPORTUNITY-OUTCOME/2026-08-v1"
OUTCOME_HORIZONS = (5, 10, 20, 40)
ACTIVE = "ACTIVE"
READY = "READY"
WATCH = "WATCH"
BLOCKED = "BLOCKED"


class OpportunityError(ValueError):
    pass


def _id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _d(value: Any, default: Decimal | None = None) -> Decimal | None:
    if value in (None, ""):
        return default
    try:
        return value if isinstance(value, Decimal) else Decimal(str(value))
    except (ArithmeticError, ValueError, TypeError):
        return default


def _stored(value: Any) -> str | None:
    value = _d(value)
    return None if value is None else str(value)


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _load(value: str | None, default: Any) -> Any:
    try:
        return json.loads(value) if value else default
    except (TypeError, ValueError):
        return default


def _row(row) -> dict | None:
    return dict(row) if row is not None else None


def _rules_payload(cfg: Config) -> dict:
    return {
        "version": RULES_VERSION,
        "min_price": cfg.opportunity_min_price,
        "min_median_dollar_volume": cfg.opportunity_min_median_dollar_volume,
        "min_bars": cfg.opportunity_min_bars,
        "full_bars": cfg.opportunity_full_bars,
        "ready_score": cfg.opportunity_ready_score,
        "watch_score": cfg.opportunity_watch_score,
        "risk_per_trade": cfg.opportunity_risk_per_trade,
        "target_position_max": cfg.opportunity_target_position_max,
        "sector_max": cfg.opportunity_sector_max,
        "max_positions": cfg.opportunity_max_positions,
    }


def rules_hash(cfg: Config) -> str:
    raw = _json(_rules_payload(cfg)).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def prior_high(bars: list[dict], lookback: int = 20) -> Decimal | None:
    """当前 bar 不参与突破基准，防止把当前高点算作历史高点。"""
    if len(bars) < lookback + 1:
        return None
    return max(_d(bar["h"], Decimal("0")) for bar in bars[-lookback - 1:-1])


def _median_dollar_volume(bars: list[dict], lookback: int = 20) -> Decimal | None:
    if len(bars) < lookback:
        return None
    values = [_d(b["c"], Decimal("0")) * Decimal(int(b.get("v") or 0))
              for b in bars[-lookback:]]
    return Decimal(str(statistics.median(values))) if values else None


def _relative_strength(bars: list[dict], spy_bars: list[dict], days: int) -> Decimal | None:
    if len(bars) < days + 1 or len(spy_bars) < days + 1:
        return None
    stock_start, stock_end = _d(bars[-days - 1]["c"]), _d(bars[-1]["c"])
    spy_start, spy_end = _d(spy_bars[-days - 1]["c"]), _d(spy_bars[-1]["c"])
    if not stock_start or not spy_start or stock_end is None or spy_end is None:
        return None
    return (stock_end / stock_start - 1) - (spy_end / spy_start - 1)


def _volume_ratio(bars: list[dict], lookback: int = 20) -> Decimal | None:
    if len(bars) < lookback + 1:
        return None
    avg = sum(Decimal(int(b.get("v") or 0)) for b in bars[-lookback - 1:-1]) / lookback
    return Decimal(int(bars[-1].get("v") or 0)) / avg if avg else None


def _up_down_volume_ratio(bars: list[dict], lookback: int = 20) -> Decimal | None:
    window = bars[-lookback:]
    up = [Decimal(int(b.get("v") or 0)) for b in window if _d(b["c"]) >= _d(b["o"])]
    down = [Decimal(int(b.get("v") or 0)) for b in window if _d(b["c"]) < _d(b["o"])]
    if not up or not down:
        return None
    return (sum(up) / len(up)) / (sum(down) / len(down)) if sum(down) else None


def _candidate_stop(symbol: str, bars: list[dict], price: Decimal) -> tuple[Decimal | None, Decimal | None]:
    try:
        advice = compute_stop(
            {"symbol": symbol, "qty": "1", "avg_cost": str(price)}, bars,
            Decimal("1000000"), close=price, allow_degraded=False,
        )
    except (ValueError, ArithmeticError, indicators.IndicatorError):
        return None, None
    if advice.stop_price >= price or advice.risk_per_share <= 0:
        return None, None
    return advice.stop_price, advice.risk_per_share


def score_candidate(symbol: str, bars: list[dict], spy_bars: list[dict], *,
                    market_state: str, cfg: Config, asset_type: str = "STOCK",
                    expected_bar_date: str | None = None) -> dict:
    """确定性机会评分。输入仅包含已收盘日线，不读取未来数据。"""
    symbol = symbol.strip().upper()
    blocked: list[str] = []
    reasons: list[str] = []
    components = {"trend": 0, "setup": 0, "volume_price": 0,
                  "relative_strength": 0, "market": 0, "penalties": 0}
    n = len(bars)
    confidence = "FULL" if n >= cfg.opportunity_full_bars else (
        "LIMITED" if n >= cfg.opportunity_min_bars else "INSUFFICIENT")
    if asset_type.upper() not in {"STOCK", "COMMON_STOCK", "COMMON"}:
        blocked.append("仅支持美股普通股")
    if n < cfg.opportunity_min_bars:
        blocked.append(f"日线不足{cfg.opportunity_min_bars}根")
    if not bars:
        return _score_result(symbol, "NONE", BLOCKED, 0, confidence, None, None, None,
                             None, None, None, None, components, reasons, blocked, bars)

    last = bars[-1]
    price = _d(last.get("c"))
    if price is None or price < _d(cfg.opportunity_min_price, Decimal("5")):
        blocked.append(f"股价低于 ${cfg.opportunity_min_price}")
    if expected_bar_date and last.get("date") != expected_bar_date:
        blocked.append(f"日线落后：{last.get('date')}，市场基准为 {expected_bar_date}")
    quality_fields = ("o", "h", "l", "c", "v")
    if any(last.get(k) in (None, "") for k in quality_fields):
        blocked.append("最新日线 OHLCV 不完整")
    if price is None:
        return _score_result(symbol, "NONE", BLOCKED, 0, confidence, None, None, None,
                             None, None, None, None, components, reasons, blocked, bars)

    mdv = _median_dollar_volume(bars)
    if mdv is None or mdv < _d(cfg.opportunity_min_median_dollar_volume, Decimal("20000000")):
        blocked.append(f"20日中位成交额低于 ${cfg.opportunity_min_median_dollar_volume}")

    try:
        card = technical.compute_symbol(symbol, bars, price)
        rules = technical.evaluate_rules(card)
    except (ValueError, ArithmeticError, indicators.IndicatorError):
        card, rules = {}, []
        blocked.append("技术指标计算失败")
    codes = {r["code"] for r in rules}
    ema = card.get("ema") or {}
    ema20, ema50 = ema.get(20), ema.get(50)
    slopes = card.get("ema_slope") or {}

    # 趋势结构 30
    if ema20 is not None and price > ema20:
        components["trend"] += 8
    if ema20 is not None and ema50 is not None and ema20 > ema50:
        components["trend"] += 8
    if slopes.get(50) == technical.UP:
        components["trend"] += 8
    if card.get("weekly_trend_state") == "周线多头":
        components["trend"] += 6
    if components["trend"] >= 24:
        reasons.append("日周线趋势结构健康")

    # 入场形态 25：突破使用 prior 20d high，不包含当前 bar。
    p_high = prior_high(bars)
    atr = card.get("atr14")
    setup = "NONE"
    breakout_extension = ((price - p_high) / p_high) if p_high and price > p_high else None
    if p_high and price > p_high and breakout_extension <= Decimal("0.05"):
        setup = "BREAKOUT"
        components["setup"] += 20
        day_range = _d(last["h"]) - _d(last["l"])
        close_location = ((price - _d(last["l"])) / day_range) if day_range > 0 else Decimal("1")
        if close_location >= Decimal("0.70"):
            components["setup"] += 5
        reasons.append("收盘突破此前20日高点")
    elif ema20 is not None and ema50 is not None and ema20 > ema50 and price >= ema20:
        recent = bars[-5:]
        touched = any(_d(b["l"]) <= ema20 * Decimal("1.01") for b in recent)
        recent_high = max(_d(b["h"]) for b in bars[-20:]) if len(bars) >= 20 else price
        drawdown = (recent_high - price) / recent_high if recent_high else Decimal("0")
        if touched and Decimal("0.02") <= drawdown <= Decimal("0.12"):
            setup = "PULLBACK"
            components["setup"] += 20
            if all(_d(b["c"]) >= ema50 for b in recent):
                components["setup"] += 5
            reasons.append("上升趋势中回踩EMA20后企稳")

    # 量价确认 20
    rvol = _volume_ratio(bars)
    if setup == "BREAKOUT" and rvol is not None and rvol >= Decimal("1.5"):
        components["volume_price"] += 10
        reasons.append("突破成交量确认")
    elif setup == "PULLBACK" and rvol is not None and rvol <= Decimal("0.9"):
        components["volume_price"] += 10
        reasons.append("回踩缩量")
    uv = _up_down_volume_ratio(bars)
    if uv is not None and uv >= Decimal("1.2"):
        components["volume_price"] += 5
    if _d(last["c"]) > _d(last["o"]):
        components["volume_price"] += 5

    # 相对 SPY 强弱 15
    rs20 = _relative_strength(bars, spy_bars, 20)
    rs60 = _relative_strength(bars, spy_bars, 60)
    if rs20 is not None and rs20 > 0:
        components["relative_strength"] += 7
    if rs60 is not None and rs60 > 0:
        components["relative_strength"] += 8
    if components["relative_strength"] == 15:
        reasons.append("20/60日均跑赢SPY")

    # 市场环境 10
    if market_state == "supportive":
        components["market"] = 10
    elif market_state == "neutral":
        components["market"] = 5
    elif market_state in {"stressed", "blocked"}:
        blocked.append("市场环境不允许新增风险")

    # 复用 T1-T8 的确定性约束。
    if "T3" in codes:
        components["penalties"] -= 20
        reasons.append("T3 过度延伸：扣20分")
    if "T8" in codes:
        components["penalties"] -= 10
        reasons.append("T8 日周线冲突：扣10分")
    if "T7" in codes:
        blocked.append("T7 中期趋势破坏")

    score = sum(components.values())
    if confidence == "LIMITED":
        score = min(score, 79)
    score = max(0, min(100, score))
    status = BLOCKED if blocked else (
        READY if score >= cfg.opportunity_ready_score and setup != "NONE" else
        WATCH if score >= cfg.opportunity_watch_score else "LOW_PRIORITY")
    if "T6" in codes and status == READY:
        status = WATCH
        reasons.append("T6 短线转弱：最高只能为WATCH")

    stop, risk_ps = _candidate_stop(symbol, bars, price)
    if stop is None or risk_ps is None:
        blocked.append("无法生成可靠止损")
        status = BLOCKED
    entry_low = price
    entry_high = price + (atr * Decimal("0.5") if atr else Decimal("0"))
    if setup == "PULLBACK" and ema20 is not None:
        entry_low = min(price, ema20)

    return _score_result(symbol, setup, status, score, confidence, price, ema20, ema50,
                         atr, rvol, entry_low, entry_high, components, reasons, blocked,
                         bars, stop=stop, risk_ps=risk_ps, mdv=mdv, rs20=rs20, rs60=rs60,
                         rules=rules, prior_high_20d=p_high)


def _score_result(symbol, setup, status, score, confidence, price, ema20, ema50,
                  atr, rvol, entry_low, entry_high, components, reasons, blocked,
                  bars, *, stop=None, risk_ps=None, mdv=None, rs20=None, rs60=None,
                  rules=None, prior_high_20d=None) -> dict:
    last = bars[-1] if bars else {}
    return {
        "symbol": symbol, "setup_type": setup, "status": status, "score": int(score),
        "grade": "A" if score >= 85 else ("B" if score >= 75 else ("C" if score >= 60 else "D")),
        "confidence": confidence, "current_price": price, "ema20": ema20, "ema50": ema50,
        "atr14": atr, "rvol20": rvol, "entry_low": entry_low, "entry_high": entry_high,
        "stop_price": stop, "risk_per_share": risk_ps,
        "median_dollar_volume_20d": mdv, "relative_strength_20d": rs20,
        "relative_strength_60d": rs60, "score_components": components,
        "reasons": reasons, "blocked_reasons": blocked,
        "bar_as_of": last.get("bar_as_of") or last.get("date"),
        "adjustment_mode": last.get("adjustment_mode"),
        "quality_status": "BLOCKED" if blocked else confidence,
        "input_refs": {"latest_bar_date": last.get("date"), "bar_source": last.get("source"),
                       "prior_high_20d": prior_high_20d, "technical_rules": rules or []},
    }


class OpportunityService:
    def __init__(self, cfg: Config, db: Database):
        self.cfg = cfg
        self.db = db

    # ---------- 候选池 ----------

    def list_watchlist(self, *, include_archived: bool = False) -> list[dict]:
        sql = "SELECT * FROM opportunity_watchlist"
        if not include_archived:
            sql += " WHERE status<>'ARCHIVED'"
        sql += " ORDER BY CASE status WHEN 'ACTIVE' THEN 0 WHEN 'PAUSED' THEN 1 ELSE 2 END, symbol"
        with self.db.connect() as conn:
            return [dict(r) for r in conn.execute(sql).fetchall()]

    def active_symbols(self) -> list[str]:
        with self.db.connect() as conn:
            rows = conn.execute(
                "SELECT symbol FROM opportunity_watchlist WHERE status='ACTIVE' ORDER BY symbol LIMIT ?",
                (self.cfg.opportunity_max_active_symbols,)).fetchall()
        return [r["symbol"] for r in rows]

    def shadow_readiness(self) -> dict:
        """返回组合构建上线门禁状态。

        同一行情日反复手工扫描只计一天；失败扫描、空自选池扫描和没有
        ``data_as_of`` 的记录均不计入观察期，避免把“系统能运行”误当成
        “候选质量已经被观察”。
        """
        required = max(0, int(getattr(self.cfg, "opportunity_shadow_min_days", 5)))
        with self.db.connect() as conn:
            active = int(conn.execute(
                "SELECT COUNT(*) FROM opportunity_watchlist WHERE status='ACTIVE'"
            ).fetchone()[0])
            rows = conn.execute(
                "SELECT DISTINCT data_as_of FROM opportunity_scan_runs "
                "WHERE status='SUCCESS' AND universe_count>0 "
                "AND data_as_of IS NOT NULL AND TRIM(data_as_of)<>'' "
                "ORDER BY data_as_of"
            ).fetchall()
        dates = [str(r["data_as_of"]) for r in rows]
        completed = len(dates)
        remaining = max(0, required - completed)
        return {
            "required_days": required,
            "completed_days": completed,
            "remaining_days": remaining,
            "effective_dates": dates,
            "active_symbols": active,
            "ready": remaining == 0,
            "opportunity_enabled": bool(self.cfg.opportunity_enabled),
            "portfolio_builder_enabled": bool(self.cfg.portfolio_builder_enabled),
        }

    def _require_shadow_ready(self) -> dict:
        state = self.shadow_readiness()
        if not state["ready"]:
            raise OpportunityError(
                "组合构建仍处于影子观察期：已完成 "
                f"{state['completed_days']}/{state['required_days']} 个有效交易日；"
                "成功但自选池为空的扫描不计入观察天数")
        return state

    def save_watch(self, symbol: str, *, sector: str | None = None,
                   thesis: str | None = None, why_watch: str | None = None,
                   note: str | None = None, asset_type: str = "STOCK",
                   exchange: str | None = None, actor: str = "web") -> dict:
        symbol = (symbol or "").strip().upper()
        if not symbol or len(symbol) > 12 or not all(c.isalnum() or c in ".-" for c in symbol):
            raise OpportunityError("股票代码格式不正确")
        now = _now()
        with self.db.connect() as conn:
            active = conn.execute(
                "SELECT COUNT(*) FROM opportunity_watchlist WHERE status='ACTIVE' AND symbol<>?",
                (symbol,)).fetchone()[0]
            if active >= self.cfg.opportunity_max_active_symbols:
                raise OpportunityError(f"活跃自选池最多 {self.cfg.opportunity_max_active_symbols} 只")
            conn.execute(
                "INSERT INTO opportunity_watchlist "
                "(symbol,status,thesis,why_watch,sector,asset_type,exchange,note,source,created_by,"
                " created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(symbol) DO UPDATE SET "
                "status=CASE WHEN opportunity_watchlist.status='ARCHIVED' THEN 'ACTIVE' "
                "ELSE opportunity_watchlist.status END, thesis=excluded.thesis,"
                "why_watch=excluded.why_watch,sector=excluded.sector,asset_type=excluded.asset_type,"
                "exchange=excluded.exchange,note=COALESCE(excluded.note,opportunity_watchlist.note),"
                "updated_at=excluded.updated_at,archived_at=NULL",
                (symbol, ACTIVE, thesis, why_watch, sector, asset_type.upper(), exchange, note,
                 "MANUAL", actor, now, now),
            )
            return dict(conn.execute(
                "SELECT * FROM opportunity_watchlist WHERE symbol=?", (symbol,)).fetchone())

    def set_watch_status(self, symbol: str, status: str) -> dict:
        status = status.upper()
        if status not in {"ACTIVE", "PAUSED", "ARCHIVED"}:
            raise OpportunityError("候选状态不正确")
        now = _now()
        with self.db.connect() as conn:
            if status == ACTIVE:
                active = conn.execute(
                    "SELECT COUNT(*) FROM opportunity_watchlist "
                    "WHERE status='ACTIVE' AND symbol<>?", (symbol.upper(),)).fetchone()[0]
                if active >= self.cfg.opportunity_max_active_symbols:
                    raise OpportunityError(
                        f"活跃自选池最多 {self.cfg.opportunity_max_active_symbols} 只")
            cur = conn.execute(
                "UPDATE opportunity_watchlist SET status=?,updated_at=?,archived_at=? WHERE symbol=?",
                (status, now, now if status == "ARCHIVED" else None, symbol.upper()),)
            if cur.rowcount != 1:
                raise OpportunityError("候选股票不存在")
            return dict(conn.execute(
                "SELECT * FROM opportunity_watchlist WHERE symbol=?", (symbol.upper(),)).fetchone())

    # ---------- 扫描 ----------

    def can_manual_scan(self, now: datetime | None = None) -> tuple[bool, int]:
        now = now or datetime.now(timezone.utc)
        with self.db.connect() as conn:
            row = conn.execute(
                "SELECT started_at FROM opportunity_scan_runs WHERE trigger_source='manual' "
                "ORDER BY started_at DESC LIMIT 1").fetchone()
        if not row:
            return True, 0
        try:
            elapsed = int((now - datetime.fromisoformat(row["started_at"])).total_seconds())
        except (TypeError, ValueError):
            return True, 0
        remaining = max(0, self.cfg.opportunity_manual_scan_cooldown_seconds - elapsed)
        return remaining == 0, remaining

    def run_scan(self, *, trigger_source: str = "manual",
                 now: datetime | None = None) -> dict:
        if not self.cfg.opportunity_enabled:
            raise OpportunityError("机会扫描功能尚未启用")
        now = now or datetime.now(timezone.utc)
        if trigger_source == "manual":
            allowed, remaining = self.can_manual_scan(now)
            if not allowed:
                raise OpportunityError(f"扫描冷却中，请 {remaining} 秒后再试")
        started = now.isoformat()
        scan_id = _id("osr")
        rh = rules_hash(self.cfg)
        symbols = self.active_symbols()
        mrow = _row(self.db.latest_market_assessment())
        market = market_service.load_market_assessment(self.db, mrow) if mrow else {}
        market_state = market.get("market_state") or "blocked"
        spy_bars = self.db.get_daily_bars("SPY", limit=max(self.cfg.opportunity_full_bars, 260))
        expected = spy_bars[-1]["date"] if spy_bars else None
        with self.db.connect() as conn:
            conn.execute(
                "INSERT INTO opportunity_scan_runs "
                "(id,status,trigger_source,started_at,universe_count,market_assessment_id,"
                " market_state,rules_version,rules_hash,data_as_of) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (scan_id, "RUNNING", trigger_source, started, len(symbols),
                 market.get("assessment_id"), market_state, RULES_VERSION, rh, expected),)
        counts = {READY: 0, WATCH: 0, BLOCKED: 0, "eligible": 0}
        errors = []
        watch_by_symbol = {r["symbol"]: r for r in self.list_watchlist()}
        for symbol in symbols:
            try:
                bars = self.db.get_daily_bars(symbol, limit=max(self.cfg.opportunity_full_bars, 260))
                watch = watch_by_symbol[symbol]
                candidate = score_candidate(
                    symbol, bars, spy_bars, market_state=market_state, cfg=self.cfg,
                    asset_type=watch.get("asset_type") or "STOCK", expected_bar_date=expected)
                self._save_candidate(scan_id, candidate, rh)
                if candidate["status"] in counts:
                    counts[candidate["status"]] += 1
                if candidate["status"] != BLOCKED:
                    counts["eligible"] += 1
            except Exception as exc:  # 单标失败隔离；完整错误不写页面
                errors.append(f"{symbol}: {type(exc).__name__}")
                candidate = _score_result(
                    symbol, "NONE", BLOCKED, 0, "INSUFFICIENT", None, None, None,
                    None, None, None, None, {}, [], ["扫描计算失败"], [])
                self._save_candidate(scan_id, candidate, rh)
                counts[BLOCKED] += 1
        finished = datetime.now(timezone.utc)
        status = "PARTIAL" if errors else "SUCCESS"
        with self.db.connect() as conn:
            conn.execute(
                "UPDATE opportunity_scan_runs SET status=?,finished_at=?,eligible_count=?,"
                "ready_count=?,watch_count=?,blocked_count=?,error_summary=?,duration_ms=? WHERE id=?",
                (status, finished.isoformat(), counts["eligible"], counts[READY], counts[WATCH],
                 counts[BLOCKED], "；".join(errors[:20]) or None,
                 int((finished - now).total_seconds() * 1000), scan_id),)
        return self.scan_detail(scan_id)

    def _save_candidate(self, scan_id: str, c: dict, rh: str) -> None:
        with self.db.connect() as conn:
            conn.execute(
                "INSERT INTO opportunity_candidates "
                "(id,scan_id,symbol,setup_type,status,score,grade,confidence,current_price,"
                "ema20,ema50,atr14,rvol20,entry_low,entry_high,stop_price,risk_per_share,"
                "median_dollar_volume_20d,relative_strength_20d,relative_strength_60d,"
                "score_components,reasons_json,blocked_reasons,input_refs,bar_as_of,"
                "adjustment_mode,quality_status,rules_version,rules_hash,created_at,"
                "effective_score,effective_status,chain_score_adjustment) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (_id("oc"), scan_id, c["symbol"], c["setup_type"], c["status"], c["score"],
                 c["grade"], c["confidence"], _stored(c["current_price"]), _stored(c["ema20"]),
                 _stored(c["ema50"]), _stored(c["atr14"]), _stored(c["rvol20"]),
                 _stored(c["entry_low"]), _stored(c["entry_high"]), _stored(c["stop_price"]),
                 _stored(c["risk_per_share"]), _stored(c["median_dollar_volume_20d"]),
                 _stored(c["relative_strength_20d"]), _stored(c["relative_strength_60d"]),
                 _json(c["score_components"]), _json(c["reasons"]), _json(c["blocked_reasons"]),
                 _json(c["input_refs"]), c["bar_as_of"], c["adjustment_mode"],
                 c["quality_status"], RULES_VERSION, rh, _now(), c["score"], c["status"], 0),)

    def latest_scan(self) -> dict | None:
        with self.db.connect() as conn:
            row = conn.execute(
                "SELECT * FROM opportunity_scan_runs WHERE status<>'RUNNING' "
                "ORDER BY started_at DESC LIMIT 1").fetchone()
        return self.scan_detail(row["id"]) if row else None

    def scan_detail(self, scan_id: str) -> dict:
        with self.db.connect() as conn:
            run = conn.execute("SELECT * FROM opportunity_scan_runs WHERE id=?", (scan_id,)).fetchone()
            if not run:
                raise OpportunityError("扫描记录不存在")
            rows = conn.execute(
                "SELECT * FROM opportunity_candidates WHERE scan_id=? "
                "ORDER BY CASE status WHEN 'READY' THEN 0 WHEN 'WATCH' THEN 1 "
                "WHEN 'LOW_PRIORITY' THEN 2 ELSE 3 END, score DESC, symbol", (scan_id,)).fetchall()
        candidates = []
        for row in rows:
            item = dict(row)
            for key, default in (("score_components", {}), ("reasons_json", []),
                                 ("blocked_reasons", []), ("input_refs", {})):
                item[key] = _load(item.get(key), default)
            candidates.append(item)
        return {"run": dict(run), "candidates": candidates}

    # ---------- 前向结果复盘 ----------

    def calculate_outcomes(self, *, candidate_id: str | None = None) -> dict:
        """计算 READY 候选的 5/10/20/40 个交易日前向结果。

        候选日为第 0 日，horizon=5 表示之后第 5 根交易日收盘。未来日线不足时只写
        PENDING，不猜测自然日，也不使用未收盘行情。SPY 必须在起止交易日均有数据，
        否则该期限标记 BLOCKED，避免用错位基准计算 alpha。
        """
        params: list[Any] = []
        where = "WHERE c.status='READY'"
        if candidate_id:
            where += " AND c.id=?"
            params.append(candidate_id)
        with self.db.connect() as conn:
            candidates = [dict(r) for r in conn.execute(
                "SELECT c.*,r.data_as_of scan_data_as_of FROM opportunity_candidates c "
                "JOIN opportunity_scan_runs r ON r.id=c.scan_id " + where
                + " ORDER BY c.created_at,c.symbol", params).fetchall()]
        now = _now()
        counts = {"candidates": len(candidates), "complete": 0, "pending": 0, "blocked": 0}
        spy_bars = self.db.get_daily_bars("SPY", limit=1200)
        spy_by_date = {b["date"]: b for b in spy_bars}
        for candidate in candidates:
            symbol = candidate["symbol"]
            start_date = candidate.get("bar_as_of") or candidate.get("scan_data_as_of")
            bars = self.db.get_daily_bars(symbol, limit=1200)
            by_date = {b["date"]: idx for idx, b in enumerate(bars)}
            start_idx = by_date.get(start_date)
            start_price = _d(candidate.get("current_price"))
            stop_price = _d(candidate.get("stop_price"))
            for horizon in OUTCOME_HORIZONS:
                status = "PENDING"
                outcome_date = start_date or "unknown"
                outcome_price = spy_start = spy_end = None
                return_pct = spy_return = alpha = mfe = mae = r_multiple = None
                note = "等待未来交易日收盘数据"
                if not start_date or start_idx is None or start_price is None or start_price <= 0:
                    status, note = "BLOCKED", "候选起始交易日或价格缺失"
                elif start_idx + horizon < len(bars):
                    end_bar = bars[start_idx + horizon]
                    window = bars[start_idx + 1:start_idx + horizon + 1]
                    outcome_date = end_bar["date"]
                    outcome_price = _d(end_bar.get("c"))
                    spy_start = _d((spy_by_date.get(start_date) or {}).get("c"))
                    spy_end = _d((spy_by_date.get(outcome_date) or {}).get("c"))
                    if outcome_price is None or outcome_price <= 0:
                        status, note = "BLOCKED", "候选终点收盘价缺失"
                    elif spy_start is None or spy_start <= 0 or spy_end is None or spy_end <= 0:
                        status, note = "BLOCKED", "SPY 起止交易日无法严格对齐"
                    elif not window:
                        status, note = "BLOCKED", "前向交易日窗口为空"
                    else:
                        return_pct = outcome_price / start_price - 1
                        spy_return = spy_end / spy_start - 1
                        alpha = return_pct - spy_return
                        highs = [_d(b.get("h")) for b in window]
                        lows = [_d(b.get("l")) for b in window]
                        if any(v is None for v in highs + lows):
                            status, note = "BLOCKED", "前向窗口 OHLC 数据不完整"
                        else:
                            mfe = max(highs) / start_price - 1
                            mae = min(lows) / start_price - 1
                            initial_risk_pct = ((start_price - stop_price) / start_price
                                                if stop_price is not None and stop_price < start_price
                                                else None)
                            r_multiple = (return_pct / initial_risk_pct
                                          if initial_risk_pct and initial_risk_pct > 0 else None)
                            status, note = "COMPLETE", "按已收盘交易日计算"
                self._upsert_outcome(
                    candidate["id"], horizon=horizon, outcome_date=outcome_date,
                    candidate_price=start_price, outcome_price=outcome_price,
                    spy_start=spy_start, spy_end=spy_end, return_pct=return_pct,
                    spy_return=spy_return, alpha=alpha, mfe=mfe, mae=mae,
                    r_multiple=r_multiple, status=status, quality_note=note,
                    calculated_at=now)
                counts[status.lower()] += 1
        return {**counts, "calculation_version": OUTCOME_VERSION,
                "horizons": list(OUTCOME_HORIZONS)}

    def _upsert_outcome(self, candidate_id: str, *, horizon: int, outcome_date: str,
                        candidate_price: Decimal | None, outcome_price: Decimal | None,
                        spy_start: Decimal | None, spy_end: Decimal | None,
                        return_pct: Decimal | None, spy_return: Decimal | None,
                        alpha: Decimal | None, mfe: Decimal | None, mae: Decimal | None,
                        r_multiple: Decimal | None, status: str, quality_note: str,
                        calculated_at: str) -> None:
        with self.db.connect() as conn:
            conn.execute(
                "INSERT INTO opportunity_outcomes "
                "(id,candidate_id,horizon_days,outcome_date,candidate_price,outcome_price,"
                "spy_start_price,spy_end_price,return_pct,spy_return_pct,alpha_pct,mfe_pct,"
                "mae_pct,r_multiple,status,quality_note,calculated_at,calculation_version) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(candidate_id,horizon_days) DO UPDATE SET "
                "outcome_date=excluded.outcome_date,candidate_price=excluded.candidate_price,"
                "outcome_price=excluded.outcome_price,spy_start_price=excluded.spy_start_price,"
                "spy_end_price=excluded.spy_end_price,return_pct=excluded.return_pct,"
                "spy_return_pct=excluded.spy_return_pct,alpha_pct=excluded.alpha_pct,"
                "mfe_pct=excluded.mfe_pct,mae_pct=excluded.mae_pct,"
                "r_multiple=excluded.r_multiple,status=excluded.status,"
                "quality_note=excluded.quality_note,calculated_at=excluded.calculated_at,"
                "calculation_version=excluded.calculation_version",
                (_id("oo"), candidate_id, horizon, outcome_date,
                 _stored(candidate_price) or "0", _stored(outcome_price), _stored(spy_start),
                 _stored(spy_end), _stored(return_pct), _stored(spy_return), _stored(alpha),
                 _stored(mfe), _stored(mae), _stored(r_multiple), status, quality_note,
                 calculated_at, OUTCOME_VERSION),)

    def outcome_summary(self) -> dict:
        """返回只读统计；至少 30 个独立 READY 样本前不允许据此自动调参。"""
        with self.db.connect() as conn:
            rows = [dict(r) for r in conn.execute(
                "SELECT o.*,c.setup_type FROM opportunity_outcomes o "
                "JOIN opportunity_candidates c ON c.id=o.candidate_id "
                "WHERE o.status='COMPLETE' ORDER BY c.setup_type,o.horizon_days").fetchall()]
        groups: dict[tuple[str, int], list[dict]] = {}
        for row in rows:
            groups.setdefault((row["setup_type"], int(row["horizon_days"])), []).append(row)
        metrics = []
        for (setup, horizon), items in sorted(groups.items()):
            def avg(field: str) -> Decimal | None:
                vals = [_d(x.get(field)) for x in items]
                vals = [x for x in vals if x is not None]
                return sum(vals, Decimal("0")) / len(vals) if vals else None
            returns = [_d(x.get("return_pct")) for x in items]
            returns = [x for x in returns if x is not None]
            metrics.append({
                "setup_type": setup, "horizon_days": horizon, "sample_count": len(items),
                "win_rate": (Decimal(sum(1 for x in returns if x > 0)) / len(returns)
                             if returns else None),
                "average_return_pct": avg("return_pct"),
                "average_spy_return_pct": avg("spy_return_pct"),
                "average_alpha_pct": avg("alpha_pct"),
                "average_mfe_pct": avg("mfe_pct"), "average_mae_pct": avg("mae_pct"),
                "average_r_multiple": avg("r_multiple"),
            })
        distinct = len({row["candidate_id"] for row in rows})
        return {"ready_samples_with_outcomes": distinct, "minimum_samples_for_tuning": 30,
                "tuning_allowed": distinct >= 30, "metrics": metrics,
                "calculation_version": OUTCOME_VERSION}

    # ---------- 组合构建 ----------

    def _apply_chain_candidate(self, candidate: dict) -> dict:
        """Stage 5 晋升后才派生 effective_*；候选 baseline 永不覆盖。"""
        out = dict(candidate)
        out.setdefault("effective_score", candidate.get("score"))
        out.setdefault("effective_status", candidate.get("status"))
        if not getattr(self.cfg, "hl_swing_influence_enabled", False) \
                or not getattr(self.cfg, "hyperliquid_enabled", False):
            return out
        try:
            from .hyperliquid_db import HyperliquidDatabase
            from .hyperliquid_service import HyperliquidService
            from .hyperliquid_results import active_promotion
            hl_db = HyperliquidDatabase(getattr(self.cfg, "hl_db_path", "./data/hyperliquid.db"))
            if not hl_db.available():
                return out
            symbol = str(candidate.get("symbol") or "").upper()
            with self.db.connect() as conn:
                mapping = conn.execute(
                    "SELECT coin FROM hl_asset_mappings WHERE canonical_symbol=? AND status='READY' "
                    "ORDER BY checked_at DESC LIMIT 1", (symbol,)).fetchone()
            if not mapping:
                return out
            coin = mapping["coin"]
            rows = HyperliquidService(self.cfg, hl_db).signals(
                model="SWING", coin=coin, active_only=True, limit=1)
            signal = rows[0] if rows else None
            if not signal or signal.get("confidence") != "HIGH" \
                    or signal.get("quality_status") != "READY":
                return out
            if not active_promotion(
                    self.db, model="SWING", rules_version=signal.get("rules_version") or "",
                    rules_hash=signal.get("rules_hash") or "", policy_hash=signal.get("policy_hash")):
                return out
            adjustment = 0
            effective_status = candidate.get("status")
            if signal.get("state") == "BEARISH":
                adjustment = -15
                effective_status = {READY: WATCH, WATCH: "LOW_PRIORITY"}.get(
                    candidate.get("status"), candidate.get("status"))
            elif signal.get("state") == "BULLISH":
                adjustment = 10
            if not adjustment:
                return out
            effective_score = max(0, min(100, int(candidate.get("score") or 0) + adjustment))
            out.update({"effective_score": effective_score, "effective_status": effective_status,
                        "chain_signal_id": signal.get("signal_id"),
                        "chain_score_adjustment": adjustment,
                        "chain_rules_version": signal.get("rules_version"),
                        "chain_rules_hash": signal.get("rules_hash")})
            with self.db.connect() as conn:
                conn.execute(
                    "UPDATE opportunity_candidates SET effective_score=?,effective_status=?,"
                    "chain_signal_id=?,chain_score_adjustment=?,chain_rules_version=?,chain_rules_hash=? "
                    "WHERE id=?",
                    (effective_score, effective_status, signal.get("signal_id"), adjustment,
                     signal.get("rules_version"), signal.get("rules_hash"), candidate.get("id")),
                )
            return out
        except Exception:  # noqa: BLE001 — 链上旁路故障保持 baseline 组合
            return out

    def build_portfolio(self, *, scan_id: str | None = None) -> dict:
        if not self.cfg.portfolio_builder_enabled:
            raise OpportunityError("组合构建功能尚未启用")
        self._require_shadow_ready()
        scan = self.scan_detail(scan_id) if scan_id else self.latest_scan()
        if not scan:
            raise OpportunityError("尚无机会扫描结果")
        balance_row = self.db.latest_balances()
        if not balance_row:
            raise OpportunityError("缺少账户净资产快照")
        balance = dict(balance_row)
        equity = _d(balance.get("equity"), Decimal("0"))
        cash = _d(balance.get("total_cash")) or _d(balance.get("cash"), Decimal("0"))
        if equity <= 0:
            raise OpportunityError("账户净资产无效")
        account = balance.get("account") or self.cfg.ib_account
        if not account:
            raise OpportunityError("缺少账户标识")
        market_state = scan["run"].get("market_state") or "blocked"
        budget_pct = {"supportive": Decimal("0.04"), "neutral": Decimal("0.03"),
                      "cautious": Decimal("0.02"), "stressed": Decimal("0"),
                      "blocked": Decimal("0")}.get(market_state, Decimal("0"))
        budget_amount = equity * budget_pct
        risk_per_trade = equity * _d(self.cfg.opportunity_risk_per_trade, Decimal("0.0075"))
        target_max = _d(self.cfg.opportunity_target_position_max, Decimal("0.20"))
        sector_max = _d(self.cfg.opportunity_sector_max, Decimal("0.35"))
        min_cash = equity * Decimal("0.05")
        alloc_cash = max(Decimal("0"), cash - min_cash)
        positions = [dict(r) for r in self.db.latest_positions()
                     if _d(r["qty"], Decimal("0")) != 0]
        stop_levels = {r["symbol"]: dict(r) for r in self.db.latest_stop_levels()}
        watch = {r["symbol"]: r for r in self.list_watchlist(include_archived=True)}
        pos_by_symbol = {p["symbol"]: p for p in positions}
        sector_values: dict[str, Decimal] = {}
        existing_risk = Decimal("0")
        blocked_reasons: list[str] = []
        current_items: list[dict] = []
        for p in positions:
            symbol = p["symbol"]
            qty = _d(p.get("qty"), Decimal("0"))
            value = abs(_d(p.get("market_value"), Decimal("0")))
            sector = (watch.get(symbol) or {}).get("sector") or "未分类"
            sector_values[sector] = sector_values.get(sector, Decimal("0")) + value
            price = _d(p.get("price")) or (_d(p.get("market_value")) / _d(p.get("qty"))
                                                   if _d(p.get("qty")) else None)
            stop = risk = None
            level = stop_levels.get(symbol)
            if qty < 0:
                blocked_reasons.append(f"{symbol} 为做空持仓，当前组合构建仅支持多头普通股")
            elif level and int(level.get("data_sufficient") or 0) == 1 \
                    and (level.get("side") or "LONG") == "LONG" and price is not None:
                stop = _d(level.get("stop_price"))
                if stop is not None and stop < price:
                    risk = (price - stop) * abs(qty)
                    existing_risk += risk
                elif stop is not None:
                    blocked_reasons.append(f"{symbol} 当前价格已触及或跌破正式止损")
            if stop is None or risk is None:
                blocked_reasons.append(f"{symbol} 缺少可靠止损，无法计算总开放风险")
            weight = value / equity
            if weight > Decimal("0.30"):
                blocked_reasons.append(f"{symbol} 当前仓位超过30%硬上限")
            current_items.append({"symbol": symbol, "item_type": "CURRENT", "score": None,
                                  "current_quantity": p.get("qty"), "proposed_quantity": "0",
                                  "current_weight": weight, "proposed_weight": weight,
                                  "stop_price": stop, "risk_amount": risk, "risk_pct": risk / equity if risk else None,
                                  "sector": sector, "reason": "现有持仓纳入组合风险", "input_refs": {}})
        if market_state in {"stressed", "blocked"}:
            blocked_reasons.append("当前市场环境禁止新增仓位")
        if existing_risk > budget_amount:
            blocked_reasons.append("现有持仓开放风险已超过市场风险预算")
        proposal_id = _id("pp")
        selected: list[dict] = []
        excluded: list[dict] = []
        position_count = len(positions)
        proposed_risk = Decimal("0")
        used_cash = Decimal("0")
        ready = [self._apply_chain_candidate(c) for c in scan["candidates"]
                 if c["status"] == READY]
        hard_block = bool(blocked_reasons)
        for c in sorted(ready, key=lambda x: (-int(x.get("effective_score") or x["score"]), x["symbol"])):
            symbol = c["symbol"]
            reason = None
            effective_score = int(c.get("effective_score") or c["score"])
            if c.get("effective_status") not in (None, READY):
                reason = f"链上护栏将候选从{c['status']}降级为{c['effective_status']}"
            price = _d(c.get("entry_high")) or _d(c.get("current_price"))
            risk_ps = _d(c.get("risk_per_share"))
            sector = (watch.get(symbol) or {}).get("sector") or "未分类"
            current = pos_by_symbol.get(symbol)
            current_value = abs(_d(current.get("market_value"), Decimal("0"))) if current else Decimal("0")
            if hard_block:
                reason = "组合处于BLOCKED状态"
            elif price is None or price <= 0 or risk_ps is None or risk_ps <= 0:
                reason = "缺少可靠入场价或止损"
            elif not current and position_count >= self.cfg.opportunity_max_positions:
                reason = f"持仓数量已达{self.cfg.opportunity_max_positions}只"
            else:
                risk_room = max(Decimal("0"), budget_amount - existing_risk - proposed_risk)
                symbol_cap = max(Decimal("0"), equity * target_max - current_value)
                sector_room = max(Decimal("0"), equity * sector_max - sector_values.get(sector, Decimal("0")))
                cash_room = max(Decimal("0"), alloc_cash - used_cash)
                shares = min(risk_per_trade / risk_ps, risk_room / risk_ps,
                             symbol_cap / price, sector_room / price, cash_room / price)
                qty = int(shares.to_integral_value(rounding=ROUND_FLOOR))
                if qty < 1:
                    reason = "风险、行业或现金约束下无法买入至少1股"
                else:
                    capital = price * qty
                    item_risk = risk_ps * qty
                    total_value = current_value + capital
                    selected.append({
                        "symbol": symbol, "item_type": "ADD", "setup_type": c["setup_type"],
                        "candidate_id": c["id"], "score": effective_score,
                        "current_quantity": current.get("qty") if current else "0",
                        "proposed_quantity": str(qty),
                        "current_weight": current_value / equity,
                        "proposed_weight": total_value / equity,
                        "entry_low": c.get("entry_low"), "entry_high": c.get("entry_high"),
                        "stop_price": c.get("stop_price"), "risk_amount": item_risk,
                        "risk_pct": item_risk / equity, "sector": sector,
                        "reason": "按机会评分、单笔风险和组合约束生成",
                        "input_refs": {"scan_id": scan["run"]["id"], "candidate_id": c["id"],
                                       "baseline_score": c.get("score"),
                                       "chain_signal_id": c.get("chain_signal_id"),
                                       "chain_score_adjustment": c.get("chain_score_adjustment", 0)},
                    })
                    proposed_risk += item_risk
                    used_cash += capital
                    sector_values[sector] = sector_values.get(sector, Decimal("0")) + capital
                    if not current:
                        position_count += 1
            if reason:
                excluded.append({"symbol": symbol, "item_type": "BLOCKED",
                                 "setup_type": c["setup_type"], "candidate_id": c["id"],
                                 "score": effective_score, "current_quantity": current.get("qty") if current else "0",
                                 "proposed_quantity": "0", "current_weight": current_value / equity,
                                 "proposed_weight": current_value / equity, "entry_low": c.get("entry_low"),
                                 "entry_high": c.get("entry_high"), "stop_price": c.get("stop_price"),
                                 "risk_amount": "0", "risk_pct": "0", "sector": sector,
                                 "reason": None, "blocked_reason": reason,
                                 "input_refs": {"candidate_id": c["id"],
                                                "baseline_score": c.get("score"),
                                                "chain_signal_id": c.get("chain_signal_id"),
                                                "chain_score_adjustment": c.get("chain_score_adjustment", 0)}})
        status = BLOCKED if blocked_reasons else READY
        now = _now()
        with self.db.connect() as conn:
            conn.execute(
                "INSERT INTO portfolio_proposals "
                "(id,scan_id,account,status,account_snapshot_id,market_assessment_id,market_state,"
                "equity,cash,risk_budget_pct,risk_budget_amount,existing_risk_amount,"
                "proposed_risk_amount,remaining_risk_amount,blocked_reasons,input_refs,"
                "rules_version,rules_hash,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (proposal_id, scan["run"]["id"], account, status, balance.get("ts"),
                 scan["run"].get("market_assessment_id"), market_state, str(equity), str(cash),
                 str(budget_pct), str(budget_amount), str(existing_risk), str(proposed_risk),
                 str(max(Decimal("0"), budget_amount - existing_risk - proposed_risk)),
                 _json(blocked_reasons), _json({"minimum_cash": min_cash, "used_cash": used_cash}),
                 RULES_VERSION, rules_hash(self.cfg), now),)
            for item in current_items + selected + excluded:
                self._insert_proposal_item(conn, proposal_id, item, now)
        return self.proposal_detail(proposal_id)

    @staticmethod
    def _insert_proposal_item(conn, proposal_id: str, item: dict, now: str) -> None:
        conn.execute(
            "INSERT INTO portfolio_proposal_items "
            "(id,proposal_id,symbol,item_type,setup_type,candidate_id,score,current_quantity,"
            "proposed_quantity,current_weight,proposed_weight,entry_low,entry_high,stop_price,"
            "risk_amount,risk_pct,sector,reason,blocked_reason,input_refs,created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (_id("ppi"), proposal_id, item["symbol"], item["item_type"], item.get("setup_type"),
             item.get("candidate_id"), item.get("score"), _stored(item.get("current_quantity")),
             _stored(item.get("proposed_quantity")), _stored(item.get("current_weight")),
             _stored(item.get("proposed_weight")), _stored(item.get("entry_low")),
             _stored(item.get("entry_high")), _stored(item.get("stop_price")),
             _stored(item.get("risk_amount")), _stored(item.get("risk_pct")), item.get("sector"),
             item.get("reason"), item.get("blocked_reason"), _json(item.get("input_refs") or {}), now),)

    def latest_proposal(self) -> dict | None:
        with self.db.connect() as conn:
            row = conn.execute("SELECT id FROM portfolio_proposals ORDER BY created_at DESC LIMIT 1").fetchone()
        return self.proposal_detail(row["id"]) if row else None

    def proposal_detail(self, proposal_id: str) -> dict:
        with self.db.connect() as conn:
            p = conn.execute("SELECT * FROM portfolio_proposals WHERE id=?", (proposal_id,)).fetchone()
            if not p:
                raise OpportunityError("组合提案不存在")
            items = conn.execute(
                "SELECT * FROM portfolio_proposal_items WHERE proposal_id=? "
                "ORDER BY CASE item_type WHEN 'ADD' THEN 0 WHEN 'CURRENT' THEN 1 ELSE 2 END,"
                "score DESC,symbol", (proposal_id,)).fetchall()
        proposal = dict(p)
        proposal["blocked_reasons"] = _load(proposal.get("blocked_reasons"), [])
        proposal["input_refs"] = _load(proposal.get("input_refs"), {})
        out = []
        for row in items:
            item = dict(row)
            item["input_refs"] = _load(item.get("input_refs"), {})
            out.append(item)
        return {"proposal": proposal, "items": out}

    def adopt_item(self, item_id: str, *, actor: str = "web") -> dict:
        """采纳建议只创建计划草稿，不调用任何券商交易接口。"""
        if not self.cfg.portfolio_builder_enabled:
            raise OpportunityError("组合构建功能尚未启用，不能采纳组合建议")
        self._require_shadow_ready()
        with self.db.connect() as conn:
            row = conn.execute(
                "SELECT i.*,p.account,p.status proposal_status FROM portfolio_proposal_items i "
                "JOIN portfolio_proposals p ON p.id=i.proposal_id WHERE i.id=?", (item_id,)).fetchone()
        if not row:
            raise OpportunityError("组合建议不存在")
        item = dict(row)
        if item["adoption_status"] == "ADOPTED" and item.get("adopted_cycle_id"):
            return {"item_id": item_id, "cycle_id": item["adopted_cycle_id"], "idempotent": True}
        if item["item_type"] != "ADD" or item["proposal_status"] != READY:
            raise OpportunityError("只有可执行组合中的新增建议可以采纳")
        svc = PositionPlanService(self.db, item["account"], actor=actor)
        cycle = svc.create_pending_opportunity_plan(
            symbol=item["symbol"], quantity=item["proposed_quantity"],
            entry_low=item.get("entry_low"), entry_high=item.get("entry_high"),
            stop_price=item.get("stop_price"), target_weight=item.get("proposed_weight"),
            max_weight=self.cfg.opportunity_target_position_max,
            thesis=item.get("reason") or "机会扫描入选",
            source_refs={"proposal_item_id": item_id, "proposal_id": item["proposal_id"],
                         "candidate_id": item.get("candidate_id")},
            idempotency_key=f"opportunity-adopt:{item_id}",
        )
        now = _now()
        with self.db.connect() as conn:
            conn.execute(
                "UPDATE portfolio_proposal_items SET adoption_status='ADOPTED',"
                "adopted_cycle_id=?,adopted_at=? WHERE id=? AND adoption_status<>'ADOPTED'",
                (cycle["id"], now, item_id),)
        return {"item_id": item_id, "cycle_id": cycle["id"], "idempotent": False}
