"""Hyperliquid 采集、特征和影子信号服务。"""
from __future__ import annotations

import hashlib
import json
import re
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable
from zoneinfo import ZoneInfo

from .hyperliquid_client import HyperliquidClient, as_decimal, normalize_fill, normalize_trade
from .hyperliquid_db import HyperliquidDatabase, utcnow_iso
from .hyperliquid_scoring import (POLICY_VERSION, RULES_VERSION, confidence,
                                  rules_hash, signed_score, transition,
                                  wallet_classification, wallet_quality)

ADDRESS_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")
DATA_ORIGINS = {"LIVE", "HISTORICAL_BACKFILL", "HISTORICAL_LOCKED_TEST",
                "PROSPECTIVE_CONFIRMATION"}


def _origin(value: str | None) -> str:
    value = str(value or "LIVE").upper()
    if value not in DATA_ORIGINS:
        raise ValueError(f"非法 data_origin: {value}")
    return value


def _d(value: Any) -> Decimal | None:
    return as_decimal(value)


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def _bucket(ts: str | int) -> str:
    if isinstance(ts, (int, float)) or str(ts).isdigit():
        dt = datetime.fromtimestamp(int(ts) / 1000, tz=timezone.utc)
    else:
        raw = str(ts).replace("Z", "+00:00")
        dt = datetime.fromisoformat(raw)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
    minute = dt.minute - (dt.minute % 5)
    return dt.replace(minute=minute, second=0, microsecond=0).isoformat()


def _iso_ms(ts: Any) -> str:
    if isinstance(ts, (int, float)) or str(ts).isdigit():
        return datetime.fromtimestamp(int(ts) / 1000, tz=timezone.utc).isoformat()
    raw = str(ts).replace("Z", "+00:00")
    dt = datetime.fromisoformat(raw)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


def canonical_symbol(coin: str) -> str:
    return str(coin).split(":", 1)[-1].upper()


def _rth_anchor(conn, coin: str, observed_at: str) -> dict[str, Any] | None:
    """返回事件前最近一个真实 RTH close 的 ctx，不以 candleSnapshot 代替。"""
    from .us_market_calendar import is_trading_day, regular_close_time

    raw = str(observed_at).replace("Z", "+00:00")
    dt = datetime.fromisoformat(raw)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    et = dt.astimezone(ZoneInfo("America/New_York"))
    day = et.date()
    # 事件收盘前，锚点必须来自前一交易日；收盘后则使用当日收盘。
    close = regular_close_time(day)
    if close is None or et.time() <= close:
        day -= timedelta(days=1)
        while not is_trading_day(day):
            day -= timedelta(days=1)
        close = regular_close_time(day)
    close_et = datetime.combine(day, close, tzinfo=ZoneInfo("America/New_York"))
    close_utc = close_et.astimezone(timezone.utc).isoformat()
    row = conn.execute(
        "SELECT * FROM hl_asset_context_buckets_5m WHERE coin=? AND bucket_start<=? "
        "ORDER BY bucket_start DESC LIMIT 1", (coin, close_utc)).fetchone()
    return dict(row) if row else None


def _off_rth(observed_at: str) -> bool:
    from .us_market_calendar import REGULAR_OPEN, is_trading_day, regular_close_time
    dt = datetime.fromisoformat(str(observed_at).replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    et = dt.astimezone(ZoneInfo("America/New_York"))
    close = regular_close_time(et.date())
    return not (is_trading_day(et.date()) and REGULAR_OPEN <= et.time() <= close)


def chain_session(observed_at: str) -> str:
    """链上专用时段，不复用主账户 market_session 的周末暂停语义。"""
    from .us_market_calendar import REGULAR_OPEN, is_trading_day, regular_close_time
    dt = datetime.fromisoformat(str(observed_at).replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    et = dt.astimezone(ZoneInfo("America/New_York"))
    if not is_trading_day(et.date()):
        return "WEEKEND_HOLIDAY"
    close = regular_close_time(et.date())
    if REGULAR_OPEN <= et.time() <= close:
        return "REGULAR"
    if et.time() < datetime.strptime("04:00", "%H:%M").time():
        return "OVERNIGHT"
    if et.time() < REGULAR_OPEN:
        return "PRE"
    if et.time() < datetime.strptime("20:00", "%H:%M").time():
        return "POST"
    return "OVERNIGHT"


class HyperliquidService:
    def __init__(self, cfg: Any, db: HyperliquidDatabase,
                 client: HyperliquidClient | None = None):
        self.cfg = cfg
        self.db = db
        self.client = client or HyperliquidClient(
            timeout=float(getattr(cfg, "hl_http_timeout", 20.0)),
            budget=None)

    @staticmethod
    def _scheduled_group_effective_at(now: str | None = None) -> str:
        """返回每日冻结版本的 20:00 ET 生效时点。

        16:30 ET 是冻结窗口起点；采集器可能在窗口内多次运行，故把版本
        绑定到确定的 20:00 ET，而不是每次运行时刻，避免同一天生成多个
        不可比较的群体版本。
        """
        raw = now or utcnow_iso()
        dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        et = dt.astimezone(ZoneInfo("America/New_York"))
        target = et.replace(hour=20, minute=0, second=0, microsecond=0)
        if et >= target:
            target += timedelta(days=1)
        return target.astimezone(timezone.utc).isoformat()

    def cleanup_retention(self, *, now: str | None = None) -> dict[str, int]:
        """执行链上库保留策略，并留下可审计的清理游标。

        公共逐笔只保留短期去重账本；可重算的 5m 聚合、钱包、特征和上下文
        保留 400 天。清理只在链上库内进行，失败不会触碰主库。
        """
        raw = now or utcnow_iso()
        dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        recent_hours = max(48, int(getattr(self.cfg, "hl_recent_trades_retention_hours", 72)))
        retention_days = max(30, int(getattr(self.cfg, "hl_retention_days", 400)))
        recent_cutoff = (dt - timedelta(hours=recent_hours)).isoformat()
        cutoff = (dt - timedelta(days=retention_days)).isoformat()
        specs = {
            "hl_recent_trades": ("received_at", recent_cutoff),
            "hl_trade_buckets_5m": ("bucket_start", cutoff),
            "hl_address_flow_buckets_5m": ("bucket_start", cutoff),
            "hl_asset_context_buckets_5m": ("bucket_start", cutoff),
            "hl_market_funding_history": ("event_time", cutoff),
            "hl_wallet_fills": ("event_time", cutoff),
            "hl_wallet_funding": ("event_time", cutoff),
            "hl_wallet_ledger_updates": ("event_time", cutoff),
            "hl_wallet_equity_snapshots": ("observed_at", cutoff),
            "hl_wallet_positions": ("observed_at", cutoff),
            "hl_wallet_cycles": ("COALESCE(closed_at, opened_at)", cutoff),
            "hl_wallet_daily_metrics": ("metric_date", cutoff[:10]),
            "hl_signal_features_5m": ("observed_at", cutoff),
            "hl_signal_features_daily": ("observed_date", cutoff[:10]),
            "hl_signal_episodes": ("signal_at", cutoff),
            "hl_data_gaps": ("started_at", cutoff),
            "hl_collection_runs": ("started_at", cutoff),
        }
        deleted: dict[str, int] = {}
        with self.db.connect() as conn:
            for table, (column, value) in specs.items():
                cur = conn.execute(f"DELETE FROM {table} WHERE {column} < ?", (value,))
                deleted[table] = max(0, int(cur.rowcount or 0))
            conn.execute(
                "INSERT INTO hl_cursors(scope,cursor_value,updated_at) VALUES(?,?,?) "
                "ON CONFLICT(scope) DO UPDATE SET cursor_value=excluded.cursor_value,"
                "updated_at=excluded.updated_at",
                ("retention:last_run", raw, raw),
            )
        # checkpoint/vacuum 必须在删除事务提交后执行，否则 SQLite 会报告
        # database table is locked。
        with self.db.connect() as conn:
            conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
            conn.execute("PRAGMA incremental_vacuum(200)")
        return deleted

    @staticmethod
    def _median_decimal(values: Iterable[Any]) -> Decimal | None:
        clean = sorted(x for x in (_d(v) for v in values) if x is not None)
        if not clean:
            return None
        # Decimal median without passing through binary float.
        mid = len(clean) // 2
        return clean[mid] if len(clean) % 2 else (clean[mid - 1] + clean[mid]) / 2

    def _signal_quality(self, conn, ctx: dict[str, Any], *, coin: str,
                        bucket_start: str, qualified_wallets: int,
                        coverage: Decimal | None) -> tuple[str, list[str], int | None]:
        """统一应用生产信号的数据门禁；缺项只降级，不伪造零值。"""
        reasons: list[str] = []
        mark, oracle = _d(ctx.get("mark_px")), _d(ctx.get("oracle_px"))
        if mark is None or oracle is None or mark <= 0 or oracle <= 0:
            reasons.append("mark_oracle_missing")
        deviation = abs(_d(ctx.get("mark_oracle_deviation")) or Decimal("999"))
        if deviation > Decimal(str(getattr(self.cfg, "hl_max_mark_oracle_deviation", "0.01"))):
            reasons.append("mark_oracle_deviation")
        oi = _d(ctx.get("oi_notional"))
        if oi is None or oi < Decimal(str(getattr(self.cfg, "hl_min_oi_notional", "100000"))):
            reasons.append("oi_notional_below_gate")
        # dayNtlVlm 是交易所提供的滚动日名义额；用过去 7 天中位数作流动性门。
        history = [r[0] for r in conn.execute(
            "SELECT day_ntl_vlm FROM hl_asset_context_buckets_5m "
            "WHERE coin=? AND bucket_start<=? AND bucket_start>=datetime(?, '-7 days') "
            "AND day_ntl_vlm IS NOT NULL", (coin, bucket_start, bucket_start)).fetchall()]
        median_vlm = self._median_decimal(history)
        if median_vlm is None or median_vlm < Decimal(str(getattr(self.cfg, "hl_min_daily_notional", "250000"))):
            reasons.append("seven_day_notional_below_gate")
        bid, ask = _d(ctx.get("impact_bid_px")), _d(ctx.get("impact_ask_px"))
        mid = _d(ctx.get("mid_px")) or mark or oracle
        spread = (ask - bid) / mid if bid is not None and ask is not None and mid and ask >= bid else None
        if spread is None:
            reasons.append("impact_spread_missing")
        elif spread > Decimal(str(getattr(self.cfg, "hl_max_spread_ratio", "0.005"))):
            reasons.append("impact_spread_above_gate")
        trade = conn.execute(
            "SELECT quality_status,liquidity_coverage FROM hl_trade_buckets_5m "
            "WHERE coin=? AND bucket_start=?", (coin, bucket_start)).fetchone()
        if not trade or trade["quality_status"] != "READY":
            reasons.append("trade_coverage_limited")
        else:
            cov = _d(trade["liquidity_coverage"])
            if cov is None or cov < Decimal("0.90"):
                reasons.append("liquidity_coverage_below_gate")
        min_wallets = int(getattr(self.cfg, "hl_min_directional_wallets", 5))
        if qualified_wallets < min_wallets:
            reasons.append("directional_wallet_count_below_gate")
        if coverage is None or coverage < Decimal("0.95"):
            reasons.append("wallet_data_coverage_below_gate")
        try:
            event_dt = datetime.fromisoformat(str(bucket_start).replace("Z", "+00:00"))
            asof_dt = datetime.fromisoformat(str(ctx.get("data_as_of") or "").replace("Z", "+00:00"))
            if event_dt.tzinfo is None:
                event_dt = event_dt.replace(tzinfo=timezone.utc)
            if asof_dt.tzinfo is None:
                asof_dt = asof_dt.replace(tzinfo=timezone.utc)
            age = max(0, int(abs((asof_dt - event_dt).total_seconds())))
            if age > 600:
                reasons.append("context_stale_over_10m")
        except (TypeError, ValueError):
            age = None
            reasons.append("context_time_missing")
        if ctx.get("quality_status") != "READY":
            reasons.append("context_quality_limited")
        return ("READY" if not reasons else "LIMITED"), reasons, age

    def sync_metadata(self, *, dex: str = "xyz") -> dict[str, Any]:
        # 动态发现 DEX；Universe 仍以显式 dex=xyz 的 metaAndAssetCtxs 为准。
        dexes = None
        discover = getattr(self.client, "perp_dexs", None)
        if callable(discover):
            try:
                dexes = discover()
            except Exception:
                dexes = None
        categories = self.client.categories()
        meta, contexts = self.client.meta_and_contexts(dex)
        universe = meta.get("universe") or []
        now = utcnow_iso()
        rows = []
        for item in universe:
            if not isinstance(item, dict) or not item.get("name"):
                continue
            coin = str(item["name"])
            rows.append((coin, dex, canonical_symbol(coin), categories.get(coin),
                         1 if item.get("isDelisted") else 0,
                         str(item.get("maxLeverage")) if item.get("maxLeverage") is not None else None,
                         item.get("szDecimals"), _json(item), now, now))
        with self.db.connect() as conn:
            conn.executemany(
                "INSERT INTO hl_assets(coin,dex,symbol,category,is_delisted,max_leverage,"
                "sz_decimals,metadata_json,data_as_of,fetched_at) VALUES(?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(coin) DO UPDATE SET dex=excluded.dex,symbol=excluded.symbol,"
                "category=excluded.category,is_delisted=excluded.is_delisted,"
                "max_leverage=excluded.max_leverage,sz_decimals=excluded.sz_decimals,"
                "metadata_json=excluded.metadata_json,data_as_of=excluded.data_as_of,"
                "fetched_at=excluded.fetched_at", rows)
        self.ingest_contexts(universe, contexts, dex=dex)
        return {"assets": len(rows), "categories": len(categories), "contexts": len(contexts),
                "dexes": len(dexes) if isinstance(dexes, list) else None}

    def ingest_contexts(self, universe: list[dict], contexts: list[dict], *, dex: str = "xyz",
                        observed_at: str | None = None,
                        data_origin: str = "LIVE") -> int:
        now = observed_at or utcnow_iso()
        data_origin = _origin(data_origin)
        prepared = []
        for item, ctx in zip(universe, contexts):
            if not isinstance(item, dict) or not isinstance(ctx, dict) or not item.get("name"):
                continue
            coin = str(item["name"])
            oracle, mark = _d(ctx.get("oraclePx")), _d(ctx.get("markPx"))
            oi, day_vlm = _d(ctx.get("openInterest")), _d(ctx.get("dayNtlVlm"))
            oi_notional = oi * oracle if oi is not None and oracle is not None else None
            basis = mark / oracle - 1 if mark is not None and oracle else None
            impact = ctx.get("impactPxs") or []
            impact_bid = impact[0] if len(impact) >= 1 else None
            impact_ask = impact[1] if len(impact) >= 2 else None
            prepared.append((coin, _bucket(now), mark, oracle, ctx.get("midPx"),
                              impact_bid, impact_ask, ctx.get("funding"), ctx.get("premium"),
                              ctx.get("openInterest"), oi_notional, ctx.get("dayNtlVlm"),
                              ctx.get("dayBaseVlm"), basis))
        rows = []
        if not prepared:
            return 0
        with self.db.connect() as conn:
            for coin, bucket, mark, oracle, mid, impact_bid, impact_ask, funding, premium, oi_raw, oi_notional, day_vlm_raw, day_base_raw, basis in prepared:
                anchor = _rth_anchor(conn, coin, now)
                anchor_oracle = _d((anchor or {}).get("oracle_px"))
                anchor_mark = _d((anchor or {}).get("mark_px"))
                off_session = _off_rth(now)
                off_return = (oracle / anchor_oracle - 1) if off_session and oracle is not None and anchor_oracle else None
                anchor_basis = (anchor_mark / anchor_oracle - 1) if anchor_mark is not None and anchor_oracle else None
                basis_change = basis - anchor_basis if off_session and basis is not None and anchor_basis is not None else None
                rows.append((coin, bucket, str(mark) if mark is not None else None,
                             str(oracle) if oracle is not None else None, mid,
                             impact_bid, impact_ask, funding, premium, oi_raw,
                             str(oi_notional) if oi_notional is not None else None,
                             day_vlm_raw, day_base_raw, str(basis) if basis is not None else None,
                             str(off_return) if off_return is not None else None,
                             str(basis_change) if basis_change is not None else None,
                             chain_session(now), now, now,
                             "READY" if oracle is not None and mark is not None else "LIMITED",
                             data_origin))
            conn.executemany(
                "INSERT INTO hl_asset_context_buckets_5m(coin,bucket_start,mark_px,oracle_px,"
                "mid_px,impact_bid_px,impact_ask_px,funding,funding_premium_index,open_interest,"
                "oi_notional,day_ntl_vlm,day_base_vlm,mark_oracle_deviation,off_rth_oracle_return,"
                "off_rth_basis_change,session,data_as_of,fetched_at,quality_status,data_origin) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(coin,bucket_start) DO UPDATE SET mark_px=excluded.mark_px,"
                "oracle_px=excluded.oracle_px,mid_px=excluded.mid_px,impact_bid_px=excluded.impact_bid_px,"
                "impact_ask_px=excluded.impact_ask_px,funding=excluded.funding,"
                "funding_premium_index=excluded.funding_premium_index,open_interest=excluded.open_interest,"
                "oi_notional=excluded.oi_notional,day_ntl_vlm=excluded.day_ntl_vlm,"
                "day_base_vlm=excluded.day_base_vlm,mark_oracle_deviation=excluded.mark_oracle_deviation,"
                "off_rth_oracle_return=excluded.off_rth_oracle_return,"
                "off_rth_basis_change=excluded.off_rth_basis_change,session=excluded.session,"
                "data_as_of=excluded.data_as_of,"
                "fetched_at=excluded.fetched_at,quality_status=excluded.quality_status,"
                "data_origin=excluded.data_origin", rows)
        return len(rows)

    def ingest_trades(self, trades: Iterable[dict], *, received_at: str | None = None,
                      data_origin: str = "LIVE") -> dict[str, int]:
        received = received_at or utcnow_iso()
        data_origin = _origin(data_origin)
        recent_rows: list[tuple] = []
        bucket_values: dict[tuple[str, str], dict[str, Decimal | int]] = {}
        address_values: dict[tuple[str, str, str], dict[str, Decimal | int | str]] = {}
        for raw in trades:
            item = normalize_trade(raw)
            if not item:
                continue
            px, sz = _d(item.get("px")), _d(item.get("sz"))
            if px is None or sz is None or px <= 0 or sz <= 0:
                continue
            notional = px * sz
            coin, bucket = item["coin"], _bucket(item["block_time"])
            recent_rows.append((coin, item["block_time"], item["tid"], item.get("side"),
                                str(item.get("px")), str(item.get("sz")), item.get("buyer"),
                                item.get("seller"), received, _json(item["raw"]), data_origin))
            acc = bucket_values.setdefault((coin, bucket), {"notional": Decimal("0"),
                "buy": Decimal("0"), "sell": Decimal("0"), "count": 0})
            acc["notional"] += notional
            acc["count"] += 1
            if item.get("side") == "B":
                acc["buy"] += notional
            elif item.get("side") == "A":
                acc["sell"] += notional
            address = item.get("aggressor")
            if address and ADDRESS_RE.match(str(address)) and item.get("side") in {"A", "B"}:
                key = (str(address).lower(), coin, bucket)
                flow = address_values.setdefault(key, {"buy": Decimal("0"), "sell": Decimal("0"),
                                                       "count": 0, "event_time": item["block_time"]})
                flow["count"] += 1
                flow["buy" if item["side"] == "B" else "sell"] += notional
        if not recent_rows:
            return {"trades": 0, "buckets": 0, "addresses": 0}
        with self.db.connect() as conn:
            conn.executemany(
                "INSERT OR IGNORE INTO hl_recent_trades(coin,block_time,tid,side,px,sz,buyer,seller,received_at,raw_json,data_origin) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?)", recent_rows)
            for (coin, bucket), acc in bucket_values.items():
                buy, sell = acc["buy"], acc["sell"]
                total = acc["notional"]
                imbalance = (buy - sell) / total if total else None
                conn.execute(
                    "INSERT INTO hl_trade_buckets_5m(coin,bucket_start,notional,buy_notional,sell_notional,"
                    "trade_count,raw_imbalance,cleaned_imbalance,liquidity_coverage,quality_status,data_as_of,data_origin) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(coin,bucket_start) DO UPDATE SET "
                    "notional=excluded.notional,buy_notional=excluded.buy_notional,sell_notional=excluded.sell_notional,"
                    "trade_count=excluded.trade_count,raw_imbalance=excluded.raw_imbalance,data_as_of=excluded.data_as_of",
                    (coin, bucket, str(total), str(buy), str(sell), int(acc["count"]),
                     str(imbalance) if imbalance is not None else None, None, None, "READY", received,
                     data_origin))
            for (address, coin, bucket), flow in address_values.items():
                conn.execute(
                    "INSERT INTO hl_address_flow_buckets_5m(aggressor_address,coin,bucket_start,buy_notional,"
                    "sell_notional,trade_count,classification_version,event_time,ingested_at,data_origin) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?) "
                    "ON CONFLICT(aggressor_address,coin,bucket_start,classification_version) DO UPDATE SET "
                    "buy_notional=excluded.buy_notional,sell_notional=excluded.sell_notional,"
                    "trade_count=excluded.trade_count,event_time=excluded.event_time,ingested_at=excluded.ingested_at",
                    (address, coin, bucket, str(flow["buy"]), str(flow["sell"]), int(flow["count"]),
                     "unknown", _iso_ms(flow["event_time"]), received, data_origin))
                conn.execute(
                    "INSERT INTO hl_wallet_candidates(address,first_seen_at,last_seen_at,status,discovered_from) "
                    "VALUES(?,?,?,?,?) ON CONFLICT(address) DO UPDATE SET last_seen_at=excluded.last_seen_at",
                    (address, _iso_ms(flow["event_time"]), received, "CANDIDATE", "PUBLIC_TRADES"),
                )
            cutoff = (
                datetime.now(timezone.utc)
                - timedelta(hours=max(48, int(getattr(
                    self.cfg, "hl_recent_trades_retention_hours", 72))))
            ).isoformat()
            conn.execute("DELETE FROM hl_recent_trades WHERE received_at < ?", (cutoff,))
        return {"trades": len(recent_rows), "buckets": len(bucket_values),
                "addresses": len(address_values)}

    def ingest_wallet_fills(self, address: str, fills: Iterable[dict], *, dex: str = "xyz",
                            ingested_at: str | None = None,
                            data_origin: str = "LIVE") -> int:
        if not ADDRESS_RE.match(address):
            raise ValueError("非法钱包地址")
        now = ingested_at or utcnow_iso()
        data_origin = _origin(data_origin)
        rows = []
        for raw in fills:
            f = normalize_fill(address, raw)
            if not f or not f["coin"].startswith(f"{dex}:"):
                continue
            rows.append((f["address"], f["coin"], _iso_ms(f["event_time"]), f["tid"], f.get("px"),
                         f.get("sz"), f.get("side"), f.get("direction"), f.get("start_position"),
                         f.get("closed_pnl"), f.get("fee"), 1 if f.get("crossed") else 0,
                         dex, _json(f["raw"]), now, data_origin))
        if not rows:
            return 0
        with self.db.connect() as conn:
            conn.executemany(
                "INSERT OR IGNORE INTO hl_wallet_fills(address,coin,event_time,tid,px,sz,side,direction,"
                "start_position,closed_pnl,fee,crossed,dex,raw_json,ingested_at,data_origin) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                rows)
        return len(rows)

    def ingest_candles(self, coin: str, interval: str, candles: Iterable[dict], *,
                       ingested_at: str | None = None,
                       data_origin: str = "HISTORICAL_BACKFILL") -> int:
        """保存成交K线供历史探索；它永远不能替代 mark/oracle ctx。"""
        now = ingested_at or utcnow_iso()
        data_origin = _origin(data_origin)
        rows = []
        for item in candles:
            if not isinstance(item, dict) or item.get("t") is None:
                continue
            rows.append((coin, interval, _iso_ms(item["t"]),
                         _iso_ms(item["T"]) if item.get("T") is not None else None,
                         item.get("o"), item.get("h"), item.get("l"), item.get("c"),
                         item.get("v"), int(item.get("n") or 0), _json(item), now,
                         data_origin))
        if not rows:
            return 0
        with self.db.connect() as conn:
            conn.executemany(
                "INSERT INTO hl_market_candles(coin,interval,open_time,close_time,open_px,high_px,"
                "low_px,close_px,volume,trade_count,raw_json,ingested_at,data_origin) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(coin,interval,open_time) DO UPDATE SET "
                "close_time=excluded.close_time,open_px=excluded.open_px,high_px=excluded.high_px,"
                "low_px=excluded.low_px,close_px=excluded.close_px,volume=excluded.volume,"
                "trade_count=excluded.trade_count,raw_json=excluded.raw_json,"
                "ingested_at=excluded.ingested_at,data_origin=excluded.data_origin",
                rows,
            )
        return len(rows)

    def upsert_wallet_candidate(self, address: str, *, discovered_from: str = "CONTROL",
                                observed_at: str | None = None) -> None:
        address = self.validate_address(address)
        now = observed_at or utcnow_iso()
        with self.db.connect() as conn:
            row = conn.execute(
                "SELECT MIN(event_time) AS first_seen, MAX(event_time) AS last_seen, "
                "SUM(CAST(px AS REAL)*CAST(sz AS REAL)) AS notional, COUNT(DISTINCT substr(event_time,1,10)) AS active_days "
                "FROM hl_wallet_fills WHERE address=?", (address,)).fetchone()
            conn.execute(
                "INSERT INTO hl_wallet_candidates(address,first_seen_at,last_seen_at,xyz_notional_90d,active_days,status,discovered_from) "
                "VALUES(?,?,?,?,?,?,?) ON CONFLICT(address) DO UPDATE SET last_seen_at=excluded.last_seen_at,"
                "xyz_notional_90d=excluded.xyz_notional_90d,active_days=excluded.active_days,discovered_from=excluded.discovered_from",
                (address, row["first_seen"] or now, row["last_seen"] or now,
                 str(row["notional"]) if row["notional"] is not None else None,
                 int(row["active_days"] or 0), "CANDIDATE", discovered_from),
            )

    def save_wallet_classification(self, address: str, classification: str, *, valid_from: str,
                                   version: str, valid_to: str | None = None,
                                   evidence: dict[str, Any] | None = None,
                                   data_origin: str = "LIVE") -> None:
        address = self.validate_address(address)
        classification = str(classification or "").upper()
        if classification not in {"LIQUIDITY", "DIRECTIONAL", "MIXED", "INSUFFICIENT"}:
            raise ValueError("钱包分类无效")
        data_origin = _origin(data_origin)
        with self.db.connect() as conn:
            conn.execute(
                "UPDATE hl_wallet_classification_intervals SET valid_to=? WHERE address=? AND valid_to IS NULL "
                "AND valid_from<?", (valid_from, address, valid_from))
            conn.execute(
                "INSERT OR IGNORE INTO hl_wallet_classification_intervals(address,classification,valid_from,valid_to,version,evidence_json,data_origin) "
                "VALUES(?,?,?,?,?,?,?)", (address, classification, valid_from, valid_to, version,
                                            _json(evidence or {}), data_origin))

    def classify_wallet(self, address: str, *, valid_from: str | None = None,
                        data_origin: str = "LIVE") -> dict[str, Any]:
        valid_from = valid_from or utcnow_iso()
        with self.db.connect(read_only=True) as conn:
            fills = [dict(r) for r in conn.execute(
                "SELECT crossed,direction FROM hl_wallet_fills WHERE address=? AND event_time<=? "
                "ORDER BY event_time DESC LIMIT 2000",
                (address.lower(), valid_from)).fetchall()]
        if len(fills) < 20:
            classification = "INSUFFICIENT"
            maker_ratio = hold = bilateral = None
        else:
            maker_ratio = Decimal(sum(1 for f in fills if not f.get("crossed"))) / len(fills)
            sides = {"long": 0, "short": 0}
            for fill in fills:
                direction = str(fill.get("direction") or "").lower()
                sides["short" if "short" in direction else "long"] += 1
            bilateral = Decimal(min(sides.values())) / max(1, max(sides.values()))
            hold = Decimal("60")
            classification = wallet_classification(maker_ratio=maker_ratio,
                                                    median_hold_minutes=hold,
                                                    bilateral_balance=bilateral)
        version = "wallet-class/" + str(valid_from).replace(":", "")[:19]
        self.save_wallet_classification(address, classification, valid_from=valid_from,
                                        version=version,
                                        evidence={"fills": len(fills), "maker_ratio": maker_ratio,
                                                  "bilateral_balance": bilateral},
                                        data_origin=data_origin)
        return {"address": address.lower(), "classification": classification,
                "version": version, "fills": len(fills)}

    def refresh_cleaned_imbalance(self, *, coin: str | None = None) -> int:
        """按成交时点的 PIT 分类重算 cleaned imbalance；覆盖不足只标 LIMITED。"""
        with self.db.connect() as conn:
            params: list[Any] = []
            where = ""
            if coin:
                where = "WHERE coin=?"; params.append(coin)
            buckets = [dict(r) for r in conn.execute(
                "SELECT * FROM hl_trade_buckets_5m " + where, params).fetchall()]
            updated = 0
            for bucket in buckets:
                flows = [dict(r) for r in conn.execute(
                    "SELECT * FROM hl_address_flow_buckets_5m WHERE coin=? AND bucket_start=?",
                    (bucket["coin"], bucket["bucket_start"])).fetchall()]
                buy = sell = identified = Decimal("0"); versions = set()
                for flow in flows:
                    cls = conn.execute(
                        "SELECT classification,version FROM hl_wallet_classification_intervals WHERE address=? "
                        "AND valid_from<=? AND (valid_to IS NULL OR valid_to>?) ORDER BY valid_from DESC LIMIT 1",
                        (flow["aggressor_address"], flow["event_time"], flow["event_time"])).fetchone()
                    if not cls or cls["classification"] != "DIRECTIONAL":
                        continue
                    versions.add(cls["version"])
                    b = _d(flow.get("buy_notional")) or Decimal("0")
                    s = _d(flow.get("sell_notional")) or Decimal("0")
                    buy += b; sell += s; identified += b + s
                total = _d(bucket.get("notional")) or Decimal("0")
                coverage = identified / total if total else None
                clean = (buy - sell) / identified if identified else None
                status = "READY" if clean is not None and coverage is not None and coverage >= Decimal("0.90") else "LIMITED"
                conn.execute(
                    "UPDATE hl_trade_buckets_5m SET cleaned_imbalance=?,liquidity_coverage=?,quality_status=? "
                    "WHERE coin=? AND bucket_start=?",
                    (str(clean) if clean is not None else None, str(coverage) if coverage is not None else None,
                     status, bucket["coin"], bucket["bucket_start"]),
                )
                updated += 1
        return updated

    def ingest_wallet_funding(self, address: str, funding: Iterable[dict], *,
                              ingested_at: str | None = None,
                              data_origin: str = "LIVE") -> int:
        """保存 userFunding 的 signed delta；不从 closedPnl 猜资金费。"""
        if not ADDRESS_RE.match(address):
            raise ValueError("非法钱包地址")
        now = ingested_at or utcnow_iso()
        data_origin = _origin(data_origin)
        rows = []
        for item in funding:
            if not isinstance(item, dict) or item.get("time") is None:
                continue
            delta = item.get("delta") if isinstance(item.get("delta"), dict) else item
            coin = str(item.get("coin") or delta.get("coin") or "")
            if not coin.startswith(f"{getattr(self.cfg, 'hl_dex', 'xyz')}:"):
                continue
            usdc = delta.get("usdc") if isinstance(delta, dict) else None
            if usdc is None:
                usdc = item.get("usdc")
            szi = delta.get("szi") if isinstance(delta, dict) else None
            rate = item.get("fundingRate") or delta.get("fundingRate")
            rows.append((address.lower(), _iso_ms(item["time"]), coin, str(usdc) if usdc is not None else None,
                         str(szi) if szi is not None else None,
                         str(rate) if rate is not None else None, _json(item), now, data_origin))
        if not rows:
            return 0
        with self.db.connect() as conn:
            conn.executemany(
                "INSERT OR IGNORE INTO hl_wallet_funding(address,event_time,coin,usdc,signed_position,"
                "funding_rate,raw_json,ingested_at,data_origin) VALUES(?,?,?,?,?,?,?,?,?)", rows)
        return len(rows)

    def ingest_wallet_ledger(self, address: str, updates: Iterable[dict], *,
                             ingested_at: str | None = None,
                             data_origin: str = "LIVE") -> int:
        if not ADDRESS_RE.match(address):
            raise ValueError("非法钱包地址")
        now = ingested_at or utcnow_iso()
        data_origin = _origin(data_origin)
        rows = []
        for item in updates:
            if not isinstance(item, dict) or item.get("time") is None:
                continue
            tx_hash = str(item.get("hash") or item.get("txHash") or item.get("transactionHash") or "")
            if not tx_hash:
                continue
            delta = item.get("delta") if isinstance(item.get("delta"), dict) else item
            coin = delta.get("coin") if isinstance(delta, dict) else item.get("coin")
            usdc = delta.get("usdc") if isinstance(delta, dict) else item.get("usdc")
            rows.append((address.lower(), _iso_ms(item["time"]), tx_hash, item.get("type") or item.get("updateType"),
                         coin, str(usdc) if usdc is not None else None, _json(item), now,
                         data_origin))
        if not rows:
            return 0
        with self.db.connect() as conn:
            conn.executemany(
                "INSERT OR IGNORE INTO hl_wallet_ledger_updates(address,event_time,tx_hash,update_type,coin,usdc,raw_json,ingested_at,data_origin) "
                "VALUES(?,?,?,?,?,?,?,?,?)", rows)
        return len(rows)

    def ingest_clearinghouse_state(self, address: str, state: dict[str, Any], *,
                                   observed_at: str | None = None,
                                   data_origin: str = "LIVE") -> dict[str, int]:
        """保存 XYZ positions 对账；账户级净值只标 XYZ_DEX_ESTIMATED。"""
        now = observed_at or utcnow_iso()
        data_origin = _origin(data_origin)
        positions = state.get("assetPositions") or []
        pos_rows = []
        total_notional = Decimal("0")
        total_upl = Decimal("0")
        for item in positions:
            p = item.get("position") if isinstance(item, dict) and isinstance(item.get("position"), dict) else item
            coin = str(p.get("coin") or "")
            if not coin.startswith(f"{getattr(self.cfg, 'hl_dex', 'xyz')}:"):
                continue
            szi = _d(p.get("szi")); value = _d(p.get("positionValue")); upl = _d(p.get("unrealizedPnl"))
            total_notional += abs(value or Decimal("0")); total_upl += upl or Decimal("0")
            pos_rows.append((address.lower(), coin, now, str(szi) if szi is not None else None,
                             p.get("entryPx"), str(value) if value is not None else None,
                             str(upl) if upl is not None else None, "XYZ_CLEARINGHOUSE", _json(item),
                             data_origin))
        with self.db.connect() as conn:
            if pos_rows:
                conn.executemany(
                    "INSERT OR REPLACE INTO hl_wallet_positions(address,coin,observed_at,signed_size,entry_px,"
                    "position_value,unrealized_pnl,source,raw_json,data_origin) VALUES(?,?,?,?,?,?,?,?,?,?)", pos_rows)
            account_value = None
            margin = state.get("marginSummary") if isinstance(state.get("marginSummary"), dict) else {}
            account_value = margin.get("accountValue")
            conn.execute(
                "INSERT OR REPLACE INTO hl_wallet_equity_snapshots(address,observed_at,account_value,xyz_notional,"
                "xyz_unrealized_pnl,source,quality_status,raw_json,data_origin) VALUES(?,?,?,?,?,?,?,?,?)",
                (address.lower(), now, account_value, str(total_notional), str(total_upl),
                 "XYZ_CLEARINGHOUSE", "ESTIMATED", _json(state), data_origin),
            )
        return {"positions": len(pos_rows), "equity": 1}

    def rebuild_wallet_cycles(self, address: str, *, coin: str | None = None,
                              data_origin: str = "LIVE") -> int:
        """按完整持仓周期重建 PnL；首个非零 startPosition 记左删失，不评分。"""
        data_origin = _origin(data_origin)
        with self.db.connect() as conn:
            params: list[Any] = [address.lower()]
            where = "WHERE address=?"
            if coin:
                where += " AND coin=?"; params.append(coin)
            fills = [dict(r) for r in conn.execute(
                "SELECT * FROM hl_wallet_fills " + where + " ORDER BY coin,event_time,tid", params).fetchall()]
            funding = [dict(r) for r in conn.execute(
                "SELECT * FROM hl_wallet_funding WHERE address=? ORDER BY event_time", (address.lower(),)).fetchall()]
            conn.execute("DELETE FROM hl_wallet_cycles WHERE address=?" + (" AND coin=?" if coin else ""),
                         ([address.lower(), coin] if coin else [address.lower()]))
        grouped: dict[str, list[dict]] = {}
        for fill in fills:
            grouped.setdefault(fill["coin"], []).append(fill)
        written = 0
        for c, items in grouped.items():
            position = Decimal("0"); cycle = None
            for fill in items:
                sz = _d(fill.get("sz")) or Decimal("0")
                start = _d(fill.get("start_position"))
                direction = str(fill.get("direction") or "").lower()
                if cycle is None and start == 0:
                    cycle = {"id": _id("hlc"), "address": address.lower(), "coin": c,
                             "opened_at": fill["event_time"], "side": "SHORT" if "short" in direction else "LONG",
                             "left_censored": 0, "fills": []}
                elif cycle is None:
                    cycle = {"id": _id("hlc"), "address": address.lower(), "coin": c,
                             "opened_at": fill["event_time"], "side": "SHORT" if "short" in direction else "LONG",
                             "left_censored": 1, "fills": []}
                cycle["fills"].append(fill)
                if "short" in direction:
                    position -= sz
                elif "long" in direction:
                    position += sz
                else:
                    position = start + sz if start is not None else position
                # When API reports startPosition, use it to identify exact close.
                end_position = position
                if start is not None and ("close" in direction):
                    end_position = start - sz if start > 0 else start + sz
                    position = end_position
                if cycle and position == 0 and cycle["fills"]:
                    gross = sum((_d(x.get("closed_pnl")) or Decimal("0") for x in cycle["fills"]), Decimal("0"))
                    fees = sum((_d(x.get("fee")) or Decimal("0") for x in cycle["fills"]), Decimal("0"))
                    opened, closed = cycle["opened_at"], fill["event_time"]
                    funding_pnl = sum((_d(x.get("usdc")) or Decimal("0") for x in funding
                                       if x.get("coin") == c and opened <= x.get("event_time", "") <= closed), Decimal("0"))
                    net = gross - fees + funding_pnl
                    with self.db.connect() as conn:
                        conn.execute(
                            "INSERT OR REPLACE INTO hl_wallet_cycles(cycle_id,address,coin,opened_at,closed_at,side,status,"
                            "left_censored,gross_closed_pnl,fees,funding_pnl,net_pnl,closed_notional,mark_to_market_pnl,raw_refs_json,data_origin) "
                            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                            (cycle["id"], address.lower(), c, opened, closed, cycle["side"], "COMPLETE",
                             cycle["left_censored"], str(gross), str(fees), str(funding_pnl), str(net),
                             str(sum(((_d(x.get("px")) or Decimal("0")) * (_d(x.get("sz")) or Decimal("0"))
                                      for x in cycle["fills"]), Decimal("0"))),
                             None, _json({"fill_tids": [x.get("tid") for x in cycle["fills"]]}),
                             data_origin))
                    written += 1; cycle = None
            if cycle and cycle["fills"]:
                # 未归零仓位只做 mark-to-market 记录，不进入完整周期质量评分。
                gross = sum((_d(x.get("closed_pnl")) or Decimal("0") for x in cycle["fills"]), Decimal("0"))
                fees = sum((_d(x.get("fee")) or Decimal("0") for x in cycle["fills"]), Decimal("0"))
                notional = sum(((_d(x.get("px")) or Decimal("0")) * (_d(x.get("sz")) or Decimal("0"))
                                for x in cycle["fills"]), Decimal("0"))
                with self.db.connect() as conn:
                    latest = conn.execute(
                        "SELECT unrealized_pnl FROM hl_wallet_positions WHERE address=? AND coin=? "
                        "ORDER BY observed_at DESC LIMIT 1", (address.lower(), c)).fetchone()
                    conn.execute(
                        "INSERT OR REPLACE INTO hl_wallet_cycles(cycle_id,address,coin,opened_at,closed_at,side,status,"
                        "left_censored,gross_closed_pnl,fees,funding_pnl,net_pnl,closed_notional,mark_to_market_pnl,raw_refs_json,data_origin) "
                        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (cycle["id"], address.lower(), c, cycle["opened_at"], None, cycle["side"], "OPEN",
                         cycle["left_censored"], str(gross), str(fees), "0", None, str(notional),
                         latest["unrealized_pnl"] if latest else None,
                         _json({"fill_tids": [x.get("tid") for x in cycle["fills"]]}), data_origin),
                    )
        return written

    def score_wallet(self, address: str, *, model: str = "INTRADAY",
                     as_of: str | None = None, data_origin: str = "LIVE") -> dict[str, Any]:
        """按完整周期和资金费/未实现快照生成可审计钱包质量分。"""
        as_of = as_of or utcnow_iso()
        data_origin = _origin(data_origin)
        cutoff = (datetime.fromisoformat(as_of.replace("Z", "+00:00")) - timedelta(days=90)).isoformat()
        with self.db.connect() as conn:
            cycles = [dict(r) for r in conn.execute(
                "SELECT * FROM hl_wallet_cycles WHERE address=? AND status='COMPLETE' "
                "AND left_censored=0 AND closed_at>=? AND closed_at<=?",
                (address.lower(), cutoff, as_of)).fetchall()]
            open_cycles = [dict(r) for r in conn.execute(
                "SELECT * FROM hl_wallet_cycles WHERE address=? AND status='OPEN' AND opened_at<=?",
                (address.lower(), as_of)).fetchall()]
            snaps = [dict(r) for r in conn.execute(
                "SELECT * FROM hl_wallet_equity_snapshots WHERE address=? AND observed_at>=? "
                "AND observed_at<=? ORDER BY observed_at",
                (address.lower(), cutoff, as_of)).fetchall()]
            fill_days = conn.execute(
                "SELECT COUNT(DISTINCT substr(event_time,1,10)) FROM hl_wallet_fills "
                "WHERE address=? AND event_time>=? AND event_time<=?",
                (address.lower(), cutoff, as_of)).fetchone()[0]
            cls = conn.execute(
                "SELECT classification FROM hl_wallet_classification_intervals WHERE address=? "
                "AND valid_from<=? AND (valid_to IS NULL OR valid_to>?) ORDER BY valid_from DESC LIMIT 1",
                (address.lower(), as_of, as_of)).fetchone()
            incomplete_history = bool(conn.execute(
                "SELECT 1 FROM hl_data_gaps WHERE status='INCOMPLETE' "
                "AND details_json LIKE ? LIMIT 1", (f'%{address.lower()}%',)).fetchone())
        pnl = [_d(c.get("net_pnl")) for c in cycles]
        pnl = [x for x in pnl if x is not None]
        wins = [x for x in pnl if x > 0]; losses = [x for x in pnl if x < 0]
        notional = sum((_d(c.get("closed_notional")) or Decimal("0") for c in cycles), Decimal("0"))
        unrealized = sum((_d(c.get("mark_to_market_pnl")) or Decimal("0") for c in open_cycles), Decimal("0"))
        gross_win = sum(wins, Decimal("0")); gross_loss = abs(sum(losses, Decimal("0")))
        pf = gross_win / gross_loss if gross_loss else None
        payoff = (sum(wins, Decimal("0")) / len(wins)) / (abs(sum(losses, Decimal("0"))) / len(losses)) if wins and losses else None
        curve = []; running = Decimal("0"); peak = Decimal("0"); max_dd = Decimal("0")
        for x in pnl:
            running += x; peak = max(peak, running); max_dd = min(max_dd, running - peak)
            curve.append(running)
        equity_return = None
        if len(snaps) >= 2:
            start = _d(snaps[0].get("account_value")); end = _d(snaps[-1].get("account_value"))
            equity_return = end / start - 1 if start and end and start > 0 else None
        elapsed_days = max(1, (datetime.fromisoformat(as_of.replace("Z", "+00:00"))
                             - datetime.fromisoformat(cutoff.replace("Z", "+00:00"))).days + 1)
        coverage = (Decimal(int(len({str(x.get("observed_at", ""))[:10] for x in snaps})))
                    / Decimal(elapsed_days)) if snaps else None
        coverage = min(Decimal("1"), coverage) if coverage is not None else None
        metrics = {
            "completed_cycles": len(cycles), "active_days": int(fill_days or 0),
            "notional_90d": notional, "coverage": coverage,
            "profit_factor": pf, "payoff_ratio": payoff,
            "unrealized_pnl": unrealized,
            "net_pnl_over_notional": (sum(pnl, Decimal("0")) + unrealized) / notional if notional else None,
            "drawdown_inverse": Decimal("1") + (max_dd / notional if notional else Decimal("-1")),
            "profitable_week_ratio": (Decimal(len(wins)) / len(pnl)) if pnl else None,
            "equity_return": equity_return, "classification": cls["classification"] if cls else None,
            "history_incomplete": incomplete_history,
        }
        quality = wallet_quality(metrics)
        if incomplete_history:
            quality = {**quality, "eligible": False, "quality_score": None,
                       "weight": None, "reason": "钱包历史存在不可补齐缺口"}
        with self.db.connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO hl_wallet_scores(address,model,as_of,rules_version,quality_score,"
                "sample_confidence,data_coverage,weight,metrics_json,data_origin) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (address.lower(), model, as_of, RULES_VERSION,
                 str(quality.get("quality_score")) if quality.get("quality_score") is not None else None,
                 str(quality.get("sample_confidence")) if quality.get("sample_confidence") is not None else None,
                 str(metrics.get("coverage")) if metrics.get("coverage") is not None else None,
                 str(quality.get("weight")) if quality.get("weight") is not None else None,
                 _json(metrics), data_origin),
            )
            conn.execute(
                "INSERT OR REPLACE INTO hl_wallet_daily_metrics(address,metric_date,model,active,completed_cycles,"
                "net_pnl,closed_notional,equity_return,drawdown,long_pnl,short_pnl,data_coverage,quality_status,data_origin) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (address.lower(), str(as_of)[:10], model, 1 if open_cycles else 0, len(cycles),
                 str(sum(pnl, Decimal("0")) + unrealized), str(notional),
                 str(equity_return) if equity_return is not None else None, str(max_dd), None, None,
                 str(metrics.get("coverage")) if metrics.get("coverage") is not None else None,
                 "READY" if quality.get("eligible") else "LIMITED", data_origin),
            )
            conn.execute(
                "UPDATE hl_wallet_candidates SET status=? WHERE address=?",
                ("ELIGIBLE" if quality.get("eligible") else
                 "LIMITED" if incomplete_history else "CANDIDATE", address.lower()),
            )
        return {"address": address.lower(), "model": model, "as_of": as_of,
                "metrics": metrics, "quality": quality}

    def wallet_performance(self, address: str, *, as_of: str | None = None) -> dict[str, Any]:
        """从本地权益曲线计算日/周/月/年收益；不足 365 天不伪造年化。"""
        as_of = as_of or utcnow_iso()
        try:
            end_dt = datetime.fromisoformat(as_of.replace("Z", "+00:00"))
        except ValueError:
            end_dt = datetime.now(timezone.utc)
        with self.db.connect(read_only=True) as conn:
            snaps = [dict(r) for r in conn.execute(
                "SELECT * FROM hl_wallet_equity_snapshots WHERE address=? AND account_value IS NOT NULL "
                "ORDER BY observed_at", (address.lower(),)).fetchall()]
        values = []
        for row in snaps:
            try:
                dt = datetime.fromisoformat(str(row["observed_at"]).replace("Z", "+00:00"))
            except ValueError:
                continue
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            value = _d(row.get("account_value"))
            if value and value > 0 and dt <= end_dt:
                values.append((dt, value))
        if not values:
            return {"status": "LIMITED", "source": "XYZ_CLEARINGHOUSE_ESTIMATED", "returns": {}}
        start_dt, end_value = values[0][0], values[-1][1]
        returns: dict[str, Any] = {}
        for name, days in (("day", 1), ("week", 7), ("month", 30), ("year", 365)):
            target = end_dt - timedelta(days=days)
            before = next(((dt, value) for dt, value in reversed(values) if dt <= target), None)
            if before is None:
                returns[name] = None
                continue
            returns[name] = str(end_value / before[1] - 1) if before[1] > 0 else None
        return {"status": "READY", "source": "XYZ_CLEARINGHOUSE_ESTIMATED",
                "as_of": end_dt.isoformat(), "history_days": (end_dt - start_dt).days,
                "returns": returns, "note": "XYZ DEX 权益快照估算；不用于跨账户排名"}

    def freeze_wallet_group(self, *, model: str = "INTRADAY", effective_at: str | None = None,
                            min_wallets: int | None = None, cap: Decimal | None = None,
                            data_origin: str = "LIVE") -> dict[str, Any]:
        """冻结每日群体版本；不足最小钱包数不生成可评分群体。"""
        effective_at = effective_at or utcnow_iso()
        data_origin = _origin(data_origin)
        min_wallets = min_wallets or int(getattr(self.cfg, "hl_min_directional_wallets", 5))
        cap = cap or _d(getattr(self.cfg, "hl_wallet_weight_cap", "0.20")) or Decimal("0.20")
        with self.db.connect() as conn:
            rows = [dict(r) for r in conn.execute(
                "SELECT s.*,c.status FROM hl_wallet_scores s LEFT JOIN hl_wallet_candidates c ON c.address=s.address "
                "WHERE s.model=? AND s.rules_version=? AND s.as_of<=? AND s.weight IS NOT NULL "
                "AND c.status='ELIGIBLE' "
                "AND NOT EXISTS (SELECT 1 FROM hl_wallet_scores newer WHERE newer.address=s.address "
                "AND newer.model=s.model AND newer.rules_version=s.rules_version AND newer.as_of>s.as_of AND newer.as_of<=?)",
                (model, RULES_VERSION, effective_at, effective_at)).fetchall()]
        if len(rows) < min_wallets:
            return {"status": "LIMITED", "reason": "有效方向钱包少于门槛", "count": len(rows)}
        rows.sort(key=lambda r: Decimal(str(r["weight"])), reverse=True)
        raw = [max(Decimal("0"), Decimal(str(r["weight"]))) for r in rows]
        weights = [min(cap, x) for x in raw]
        for _ in range(10):
            total = sum(weights, Decimal("0"))
            if not total:
                break
            excess = Decimal("1") - total
            if abs(excess) < Decimal("0.0000001"):
                break
            free = [i for i, w in enumerate(weights) if w < cap]
            if not free:
                break
            add = excess / len(free)
            for i in free:
                weights[i] = min(cap, weights[i] + add)
        total = sum(weights, Decimal("0"))
        if total <= 0:
            return {"status": "LIMITED", "reason": "钱包权重无效", "count": len(rows)}
        weights = [w / total for w in weights]
        material = [(r["address"], str(w)) for r, w in zip(rows, weights)]
        group_version = "hlg_" + hashlib.sha256(_json(material).encode()).hexdigest()[:16]
        with self.db.connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO hl_wallet_group_versions(group_version,model,effective_at,rules_version,members_json,created_at,data_origin) "
                "VALUES(?,?,?,?,?,?,?)", (group_version, model, effective_at, RULES_VERSION,
                                           _json(material), utcnow_iso(), data_origin))
            for r, weight in zip(rows, weights):
                conn.execute(
                    "UPDATE hl_wallet_memberships SET valid_to=? WHERE address=? AND model=? "
                    "AND valid_to IS NULL AND valid_from<?", (effective_at, r["address"], model, effective_at))
                conn.execute(
                    "INSERT OR REPLACE INTO hl_wallet_memberships(address,model,valid_from,valid_to,admitted_at,weight,quality_score,"
                    "sample_confidence,data_coverage,group_version,data_origin) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (r["address"], model, effective_at, None, effective_at, str(weight), r.get("quality_score"),
                     r.get("sample_confidence"), r.get("data_coverage"), group_version,
                     data_origin))
        return {"status": "READY", "group_version": group_version, "count": len(rows),
                "weights": {r["address"]: str(w) for r, w in zip(rows, weights)}}

    def generate_shadow_signals(self, *, main_db: Any | None = None,
                                observed_at: str | None = None,
                                data_origin: str = "LIVE",
                                validation_commitment_id: str | None = None) -> dict[str, int]:
        """从已落库上下文/方向钱包聚合影子信号；缺关键组件只记录 LIMITED/EXPIRED。"""
        if not getattr(self.cfg, "hl_signal_shadow_enabled", False):
            return {"features": 0, "signals": 0, "limited": 0}
        now = observed_at or utcnow_iso()
        data_origin = _origin(data_origin)
        with self.db.connect(read_only=True) as conn:
            contexts = [dict(r) for r in conn.execute(
                "SELECT c.* FROM hl_asset_context_buckets_5m c JOIN ("
                "SELECT coin,MAX(bucket_start) AS bucket_start FROM hl_asset_context_buckets_5m "
                "WHERE bucket_start<=? GROUP BY coin) x "
                "ON c.coin=x.coin AND c.bucket_start=x.bucket_start ORDER BY c.coin",
                (now,)).fetchall()]
        features_written = signals_written = limited = 0
        for ctx in contexts:
            coin = ctx["coin"]
            if main_db is not None:
                with main_db.connect() as conn:
                    mapping = conn.execute(
                        "SELECT status FROM hl_asset_mappings WHERE coin=?", (coin,)).fetchone()
                if not mapping or mapping["status"] != "READY":
                    continue
            regime = "RTH" if ctx.get("session") == "REGULAR" else "OFF_RTH"
            model_features = {
                "INTRADAY": ("off_rth_oracle_return", "qualified_wallet_aggressor_imbalance",
                             "qualified_wallet_net_exposure", "wallet_breadth") if regime == "OFF_RTH" else
                            ("qualified_wallet_aggressor_imbalance", "qualified_wallet_net_exposure", "wallet_breadth"),
                "SWING": ("five_day_off_rth_oracle_return", "one_day_qualified_wallet_aggressor_imbalance",
                          "five_day_qualified_wallet_net_exposure", "three_day_wallet_breadth_persistence"),
            }
            for model in ("INTRADAY", "SWING"):
                # 每个模型使用自己的冻结群体；任何 admitted_at 之前的成交/持仓
                # 都不会进入特征，避免准入回写造成幸存者偏差。
                with self.db.connect(read_only=True) as conn:
                    flows = [dict(r) for r in conn.execute(
                        "SELECT f.*,m.weight,m.group_version,m.data_coverage FROM hl_address_flow_buckets_5m f "
                        "JOIN hl_wallet_classification_intervals i ON i.address=f.aggressor_address "
                        "AND i.classification='DIRECTIONAL' AND i.valid_from<=f.event_time "
                        "AND (i.valid_to IS NULL OR i.valid_to>f.event_time) "
                        "JOIN hl_wallet_memberships m ON m.address=f.aggressor_address AND m.model=? "
                        "AND m.admitted_at<=f.event_time AND m.valid_from<=f.event_time "
                        "AND (m.valid_to IS NULL OR m.valid_to>f.event_time) "
                        "WHERE f.coin=? AND f.bucket_start=?", (model, coin, ctx["bucket_start"])).fetchall()]
                    positions = [dict(r) for r in conn.execute(
                        "SELECT p.*,m.weight,m.group_version,m.data_coverage FROM hl_wallet_positions p "
                        "JOIN hl_wallet_classification_intervals i ON i.address=p.address "
                        "AND i.classification='DIRECTIONAL' AND i.valid_from<=p.observed_at "
                        "AND (i.valid_to IS NULL OR i.valid_to>p.observed_at) "
                        "JOIN hl_wallet_memberships m ON m.address=p.address AND m.model=? "
                        "AND m.admitted_at<=p.observed_at AND m.valid_from<=p.observed_at "
                        "AND (m.valid_to IS NULL OR m.valid_to>p.observed_at) "
                        "WHERE p.coin=? AND p.observed_at<=? ORDER BY p.observed_at DESC",
                        (model, coin, ctx["bucket_start"])).fetchall()]
                    older = conn.execute(
                        "SELECT oracle_px FROM hl_asset_context_buckets_5m WHERE coin=? "
                        "AND bucket_start<=datetime(?, '-5 days') ORDER BY bucket_start DESC LIMIT 1",
                        (coin, ctx["bucket_start"])).fetchone()
                    coverage_values = [_d(x.get("data_coverage")) for x in flows if x.get("data_coverage") is not None]
                    coverage = min(coverage_values) if coverage_values else None
                    qualified_count = len({x["aggressor_address"] for x in flows})
                    group_versions = {x.get("group_version") for x in flows if x.get("group_version")}
                    group_versions.update(x.get("group_version") for x in positions if x.get("group_version"))
                    wallet_group_version = sorted(group_versions)[-1] if group_versions else None
                    buy = sum(((_d(x.get("buy_notional")) or Decimal("0")) *
                               (_d(x.get("weight")) or Decimal("1")) for x in flows), Decimal("0"))
                    sell = sum(((_d(x.get("sell_notional")) or Decimal("0")) *
                                (_d(x.get("weight")) or Decimal("1")) for x in flows), Decimal("0"))
                    total = buy + sell
                    aggressor = (buy - sell) / total if total else None
                    longs = shorts = exposure = Decimal("0")
                    for pos in positions:
                        value = _d(pos.get("position_value")) or Decimal("0")
                        size = _d(pos.get("signed_size")) or Decimal("0")
                        weight = _d(pos.get("weight")) or Decimal("1")
                        exposure += (value if size >= 0 else -abs(value)) * weight
                        longs += 1 if size > 0 else 0
                        shorts += 1 if size < 0 else 0
                    breadth = ((longs - shorts) / (longs + shorts)) if (longs + shorts) else None
                oracle = _d(ctx.get("oracle_px")); old_oracle = _d((older or {}).get("oracle_px"))
                raw_features = {
                    "off_rth_oracle_return": _d(ctx.get("off_rth_oracle_return")),
                    "qualified_wallet_aggressor_imbalance": aggressor,
                    "qualified_wallet_net_exposure": (exposure / (_d(ctx.get("oi_notional")) or Decimal("1"))) if exposure else None,
                    "wallet_breadth": breadth,
                    "five_day_off_rth_oracle_return": oracle / old_oracle - 1 if oracle and old_oracle else None,
                    "one_day_qualified_wallet_aggressor_imbalance": aggressor,
                    "five_day_qualified_wallet_net_exposure": (exposure / (_d(ctx.get("oi_notional")) or Decimal("1"))) if exposure else None,
                    "three_day_wallet_breadth_persistence": breadth,
                }
                required_features = model_features[model]
                reasons: list[str] = []
                normalized: dict[str, Any] = {}
                # 正式方向分只使用截止当前 bucket 的 20 个同 session 完整样本；
                # MAD=0 时由 robust_standardize 回退 IQR，仍无离散度则该特征缺失。
                from .hyperliquid_scoring import robust_standardize
                with self.db.connect(read_only=True) as conn:
                    for key in required_features:
                        if model == "SWING":
                            history_rows = conn.execute(
                                "SELECT feature_json,observed_date,quality_status FROM hl_signal_features_daily "
                                "WHERE coin=? AND observed_date<? AND quality_status='READY' "
                                "ORDER BY observed_date DESC LIMIT 20",
                                (coin, str(ctx["bucket_start"])[:10])).fetchall()
                        else:
                            # 同一交易日只取一个完整 session 的最新值；20 条
                            # 5m bar 不能冒充 20 个独立日样本。
                            raw_rows = conn.execute(
                                "SELECT feature_json,observed_at,quality_status FROM hl_signal_features_5m "
                                "WHERE coin=? AND regime=? AND observed_at<? "
                                "AND quality_status='READY' ORDER BY observed_at DESC LIMIT 5000",
                                (coin, regime, ctx["bucket_start"])).fetchall()
                            history_rows = []
                            days = set()
                            for item in raw_rows:
                                day = str(item["observed_at"])[:10]
                                if day in days:
                                    continue
                                days.add(day)
                                history_rows.append(item)
                                if len(history_rows) >= 20:
                                    break
                        history = []
                        for item in history_rows:
                            payload = self.db.decode_json(item[0], {}) or {}
                            historical = payload.get(f"raw_{key}", payload.get(key))
                            if historical is not None:
                                history.append(historical)
                        if len(history) < 20:
                            normalized[key] = None
                            reasons.append(f"{key}_robust_window_incomplete")
                        else:
                            normalized[key] = robust_standardize(raw_features.get(key), history)
                            if normalized[key] is None:
                                reasons.append(f"{key}_robust_scale_unavailable")
                    quality, gate_reasons, ctx_age = self._signal_quality(
                        conn, ctx, coin=coin, bucket_start=ctx["bucket_start"],
                        qualified_wallets=qualified_count, coverage=coverage)
                reasons.extend(gate_reasons)
                # 影子期可以保存 LIMITED 特征/episode，但只有所有组件齐全时
                # 才会产生可用于结果评估的 READY 方向信号。
                model_regime = regime if model == "INTRADAY" else "OFF_RTH"
                feature_payload = {key: normalized.get(key) for key in required_features}
                feature_payload.update({f"raw_{key}": raw_features.get(key)
                                        for key in required_features})
                with self.db.connect() as conn:
                    conn.execute(
                        "INSERT OR REPLACE INTO hl_signal_features_5m(coin,observed_at,regime,feature_json,quality_status,data_as_of,data_origin) "
                        "VALUES(?,?,?,?,?,?,?)", (coin, ctx["bucket_start"], model_regime,
                                                   _json(feature_payload), quality, now, data_origin))
                    if model == "SWING":
                        conn.execute(
                            "INSERT OR REPLACE INTO hl_signal_features_daily(coin,observed_date,feature_json,quality_status,data_as_of,data_origin) "
                            "VALUES(?,?,?,?,?,?)", (coin, str(ctx["bucket_start"])[:10],
                                                     _json(feature_payload), quality, now,
                                                     data_origin))
                    previous = conn.execute(
                        "SELECT state FROM hl_signal_episodes WHERE model=? AND regime=? AND coin=? "
                        "AND signal_id<>? ORDER BY signal_at DESC LIMIT 1",
                        (model, model_regime, coin, "__none__"),
                    ).fetchone()
                features_written += 1
                self.create_signal(model=model, regime=model_regime, coin=coin,
                                   features=feature_payload, quality_status=quality,
                                   quality_reasons=reasons, qualified_wallets=qualified_count,
                                   coverage=coverage, ctx_age_seconds=ctx_age,
                                   signal_at=ctx["bucket_start"],
                                   previous_state=previous["state"] if previous else None,
                                   wallet_group_version=wallet_group_version,
                                   data_origin=data_origin,
                                   validation_commitment_id=validation_commitment_id)
                signals_written += 1
                if quality != "READY":
                    limited += 1
        return {"features": features_written, "signals": signals_written, "limited": limited}

    def create_signal(self, *, signal_id: str | None = None, model: str, regime: str,
                      coin: str, features: dict[str, Any], previous_state: str | None = None,
                      quality_status: str = "READY", quality_reasons: Iterable[str] = (),
                      qualified_wallets: int = 0, coverage: Decimal | None = None,
                      ctx_age_seconds: int | None = None, signal_at: str | None = None,
                      actionable_at: str | None = None, valid_until: str | None = None,
                      wallet_group_version: str | None = None,
                      data_origin: str = "LIVE",
                      validation_commitment_id: str | None = None) -> dict[str, Any]:
        data_origin = _origin(data_origin)
        result = signed_score(features, model, regime)
        if quality_status != "READY":
            result["quality_status"] = quality_status
        score = result.get("score")
        state = transition(previous_state, score, result.get("quality_status", quality_status))
        conf = confidence(score, quality_status=result.get("quality_status", quality_status),
                          qualified_wallets=qualified_wallets, coverage=coverage,
                          ctx_age_seconds=ctx_age_seconds)
        now = signal_at or utcnow_iso()
        if actionable_at is None:
            try:
                from .hyperliquid_outcomes import derive_actionable_at
                actionable_at = derive_actionable_at(now, regime)
            except Exception:
                actionable_at = None
        if signal_id:
            sid = signal_id
        else:
            # 同一 actionable_at 只保留一个 decision episode；off-RTH 按交易日
            # 合并，避免每 5 分钟重算制造虚假样本量。
            episode_key = actionable_at or (now[:10] if str(regime).upper() == "OFF_RTH" else _bucket(now))
            sid = "hl_" + hashlib.sha256(
                f"{model}|{regime}|{coin}|{episode_key}".encode()).hexdigest()[:24]
        payload = {"signal_id": sid, "model": model, "regime": regime, "session": regime,
                   "coin": coin, "canonical_symbol": canonical_symbol(coin), "state": state,
                   "score": score, "confidence": conf, "quality_status": result.get("quality_status"),
                   "quality_reasons": list(quality_reasons) + list(result.get("missing") or []),
                   "signal_at": now, "actionable_at": actionable_at, "valid_until": valid_until,
                   "components": result.get("components") or {}, "diagnostic": features,
                   "wallet_group_version": wallet_group_version, "rules_version": RULES_VERSION,
                   "rules_hash": rules_hash(model, regime), "policy_version": POLICY_VERSION,
                   "policy_hash": hashlib.sha256(POLICY_VERSION.encode()).hexdigest()[:16],
                   "data_origin": data_origin,
                   "validation_commitment_id": validation_commitment_id}
        with self.db.connect() as conn:
            conn.execute(
                "INSERT INTO hl_signal_episodes(signal_id,model,regime,session,coin,canonical_symbol,state,score,"
                "confidence,signal_at,actionable_at,valid_until,component_json,diagnostic_json,wallet_group_version,"
                "quality_status,quality_reasons_json,input_refs_json,rules_version,rules_hash,policy_version,policy_hash,last_seen_at,"
                "data_origin,validation_commitment_id) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(signal_id) DO UPDATE SET "
                "state=excluded.state,score=excluded.score,confidence=excluded.confidence,quality_status=excluded.quality_status,"
                "quality_reasons_json=excluded.quality_reasons_json,diagnostic_json=excluded.diagnostic_json,last_seen_at=excluded.last_seen_at",
                (sid, model, regime, regime, coin, canonical_symbol(coin), state,
                 str(score) if score is not None else None, conf, now, actionable_at, valid_until,
                 _json(payload["components"]), _json(features), wallet_group_version,
                 payload["quality_status"], _json(payload["quality_reasons"]), _json({"features": True}),
                 RULES_VERSION, payload["rules_hash"], POLICY_VERSION, payload["policy_hash"], now,
                 data_origin, validation_commitment_id))
        return payload

    def status(self) -> dict[str, Any]:
        return self.db.health()

    def assets(self, *, limit: int = 500) -> list[dict[str, Any]]:
        if not self.db.available():
            return []
        with self.db.connect(read_only=True) as conn:
            return [dict(r) for r in conn.execute(
                "SELECT * FROM hl_assets ORDER BY symbol LIMIT ?", (max(1, min(limit, 1000)),))]

    def signals(self, *, model: str | None = None, coin: str | None = None,
                active_only: bool = False, limit: int = 100) -> list[dict[str, Any]]:
        if not self.db.available():
            return []
        where, params = [], []
        if model:
            where.append("model=?"); params.append(model)
        if coin:
            where.append("coin=?"); params.append(coin)
        if active_only:
            where.append("state IN ('BULLISH','BEARISH')")
        clause = "WHERE " + " AND ".join(where) if where else ""
        with self.db.connect(read_only=True) as conn:
            rows = [dict(r) for r in conn.execute(
                f"SELECT * FROM hl_signal_episodes {clause} ORDER BY signal_at DESC LIMIT ?",
                (*params, max(1, min(limit, 500))))]
        for row in rows:
            for key in ("component_json", "diagnostic_json", "quality_reasons_json", "input_refs_json"):
                row[key.removesuffix("_json")] = self.db.decode_json(row.get(key), {})
        return rows

    def wallets(self, *, classification: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        if not self.db.available():
            return []
        with self.db.connect(read_only=True) as conn:
            sql = "SELECT * FROM hl_wallet_candidates"
            params: tuple[Any, ...] = ()
            if classification:
                sql = "SELECT c.*, i.classification FROM hl_wallet_candidates c LEFT JOIN hl_wallet_classification_intervals i ON i.address=c.address AND i.valid_to IS NULL WHERE i.classification=?"
                params = (classification,)
            sql += " ORDER BY last_seen_at DESC LIMIT ?"
            return [dict(r) for r in conn.execute(sql, (*params, max(1, min(limit, 200))))]

    @staticmethod
    def validate_address(address: str) -> str:
        address = str(address or "").strip().lower()
        if not ADDRESS_RE.match(address):
            raise ValueError("地址必须是 0x 加 40 位十六进制")
        return address

    def save_wallet_control(self, main_db: Any, address: str, mode: str,
                            label: str | None = None, note: str | None = None) -> dict[str, Any]:
        address = self.validate_address(address)
        mode = str(mode or "").upper()
        if mode not in {"PINNED", "AUTO", "EXCLUDED"}:
            raise ValueError("mode 必须是 PINNED/AUTO/EXCLUDED")
        label = (label or "")[:80] or None
        note = (note or "")[:500] or None
        with main_db.connect() as conn:
            if mode == "PINNED":
                count = conn.execute("SELECT COUNT(*) FROM hl_wallet_controls WHERE mode='PINNED' AND address<>?",
                                     (address,)).fetchone()[0]
                if count >= 50:
                    raise ValueError("固定钱包已达到 50 个上限")
            now = utcnow_iso()
            conn.execute(
                "INSERT INTO hl_wallet_controls(address,mode,label,note,updated_at) VALUES(?,?,?,?,?) "
                "ON CONFLICT(address) DO UPDATE SET mode=excluded.mode,label=excluded.label,"
                "note=excluded.note,updated_at=excluded.updated_at",
                (address, mode, label, note, now))
        return {"address": address, "mode": mode, "label": label, "note": note,
                "updated_at": now}
