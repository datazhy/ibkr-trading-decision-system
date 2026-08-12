"""validation-policy-v2 的可恢复历史回溯执行器。

每次调用最多执行一个高权重 Hyperliquid Info 步骤，复用采集进程的共享
``WeightBudget``，因此不会绕过 20% 回补子预算。外部历史归档采用显式导入；
归档不可用时写 INCOMPLETE，不以 candleSnapshot 伪造 mark/oracle ctx。
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import uuid
from datetime import date, datetime, time as dt_time, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable
from zoneinfo import ZoneInfo

from .hyperliquid_client import HyperliquidApiError
from .hyperliquid_db import HyperliquidDatabase, utcnow_iso
from .hyperliquid_service import HyperliquidService


BACKFILL_ORIGIN = "HISTORICAL_BACKFILL"


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _dt(value: str) -> datetime:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _ms(value: datetime) -> int:
    return int(value.timestamp() * 1000)


class HistoricalBackfillExecutor:
    def __init__(self, cfg: Any, service: HyperliquidService, main_db: Any):
        self.cfg = cfg
        self.service = service
        self.hl_db: HyperliquidDatabase = service.db
        self.main_db = main_db

    def _close_interrupted_runs(self, scope: str) -> int:
        """新步骤开始时收口上次进程中断遗留的 RUNNING，不伪装持续运行。"""
        with self.hl_db.connect() as conn:
            cur = conn.execute(
                "UPDATE hl_backfill_runs SET finished_at=?,status='PARTIAL',"
                "incomplete_items=MAX(incomplete_items,1),error_code='PROCESS_RESTART',"
                "error_message='上一次回溯步骤在完成前中断' WHERE status='RUNNING' "
                "AND scope_json LIKE ?",
                (utcnow_iso(), f'%"scope": "{scope}"%'),
            )
        return int(cur.rowcount or 0)

    def _cursor(self, scope: str) -> dict[str, Any]:
        with self.hl_db.connect(read_only=True) as conn:
            row = conn.execute("SELECT cursor_value FROM hl_cursors WHERE scope=?",
                               (scope,)).fetchone()
        if not row or not row[0]:
            return {}
        try:
            value = json.loads(row[0])
            return value if isinstance(value, dict) else {}
        except (TypeError, ValueError):
            return {}

    def _set_cursor(self, scope: str, value: dict[str, Any]) -> None:
        with self.hl_db.connect() as conn:
            conn.execute(
                "INSERT INTO hl_cursors(scope,cursor_value,updated_at) VALUES(?,?,?) "
                "ON CONFLICT(scope) DO UPDATE SET cursor_value=excluded.cursor_value,"
                "updated_at=excluded.updated_at",
                (scope, _json(value), utcnow_iso()),
            )

    def _save_item(self, run_id: str, item_type: str, item_key: str, *,
                   status: str, rows: int = 0, cursor: str | None = None,
                   gap_reason: str | None = None,
                   details: dict[str, Any] | None = None) -> None:
        with self.hl_db.connect() as conn:
            conn.execute(
                "INSERT INTO hl_backfill_items(run_id,item_type,item_key,status,cursor_value,"
                "rows_written,gap_reason,details_json,updated_at) VALUES(?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(run_id,item_type,item_key) DO UPDATE SET status=excluded.status,"
                "cursor_value=excluded.cursor_value,rows_written=excluded.rows_written,"
                "gap_reason=excluded.gap_reason,details_json=excluded.details_json,"
                "updated_at=excluded.updated_at",
                (run_id, item_type, item_key, status, cursor, int(rows), gap_reason,
                 _json(details or {}), utcnow_iso()),
            )

    def admit_top_candidates(self, *, start: datetime, limit: int | None = None) -> list[str]:
        """只建立 AUTO 跟踪控制；不等于方向钱包准入或验证入池。"""
        limit = max(1, min(int(limit or getattr(self.cfg, "hl_max_tracked_wallets", 50)),
                           int(getattr(self.cfg, "hl_max_tracked_wallets", 50))))
        with self.hl_db.connect(read_only=True) as conn:
            rows = conn.execute(
                "SELECT c.address,COALESCE(SUM(CAST(f.buy_notional AS REAL)+"
                "CAST(f.sell_notional AS REAL)),0) AS notional,COUNT(DISTINCT substr(f.event_time,1,10)) AS days "
                "FROM hl_wallet_candidates c LEFT JOIN hl_address_flow_buckets_5m f "
                "ON f.aggressor_address=c.address AND f.event_time>=? "
                "GROUP BY c.address ORDER BY notional DESC,days DESC,c.last_seen_at DESC LIMIT ?",
                (start.isoformat(), limit * 4),
            ).fetchall()
        selected: list[str] = []
        with self.main_db.connect() as conn:
            controls = [dict(r) for r in conn.execute(
                "SELECT address,mode,label,updated_at FROM hl_wallet_controls "
                "ORDER BY CASE mode WHEN 'PINNED' THEN 0 WHEN 'AUTO' THEN 1 ELSE 2 END,"
                "updated_at,address").fetchall()]
            existing = {str(r["address"]).lower(): str(r["mode"]).upper()
                        for r in controls}
            # 一个回溯批次使用稳定跟踪池；已有 PINNED/AUTO 不因公共流排名
            # 每5分钟变化而被换出，否则活跃钱包会永久停留在第一页。
            selected = [str(r["address"]).lower() for r in controls
                        if str(r["mode"]).upper() in {"PINNED", "AUTO"}][:limit]
            for row in rows:
                address = str(row["address"]).lower()
                if existing.get(address) == "EXCLUDED":
                    continue
                if address not in selected and address not in existing:
                    conn.execute(
                        "INSERT INTO hl_wallet_controls(address,mode,label,note,updated_at) "
                        "VALUES(?,?,?,?,?)",
                        (address, "AUTO", "HL快速通道自动跟踪",
                         "仅用于历史回溯；不代表方向准入", utcnow_iso()),
                    )
                if address not in selected:
                    selected.append(address)
                if len(selected) >= limit:
                    break
            # 修复首版可能超出上限的自动行，仅删除本执行器生成的溢出项。
            generated = [str(r["address"]).lower() for r in controls
                         if r.get("mode") == "AUTO"
                         and r.get("label") == "HL快速通道自动跟踪"]
            for address in generated:
                if address not in selected:
                    conn.execute(
                        "DELETE FROM hl_wallet_controls WHERE address=? AND mode='AUTO' "
                        "AND label='HL快速通道自动跟踪'", (address,))
        return selected

    def _tracked_wallets(self) -> list[str]:
        with self.main_db.connect() as conn:
            rows = conn.execute(
                "SELECT address FROM hl_wallet_controls WHERE mode IN ('PINNED','AUTO') "
                "ORDER BY CASE mode WHEN 'PINNED' THEN 0 ELSE 1 END,updated_at,address LIMIT ?",
                (max(1, int(getattr(self.cfg, "hl_max_tracked_wallets", 50))),),
            ).fetchall()
        return [str(r["address"]).lower() for r in rows]

    def backfill_one_wallet_step(self, *, run_id: str, start: datetime,
                                 end: datetime) -> dict[str, Any]:
        wallets = self._tracked_wallets()
        if not wallets:
            return {"status": "NO_WALLETS", "rows": 0}
        for address in wallets:
            scope = f"history:v2:wallet:{address}:{start.date()}:{end.date()}"
            state = self._cursor(scope)
            if state.get("complete"):
                continue
            # 兼容首版“下一页预算不足即结束”的游标：从第一页幂等重读，
            # 此后改用逐页 cursor 在不同调度周期续传。
            if state.get("fills") and state.get("fills_incomplete") \
                    and not state.get("fills_cursor"):
                state.pop("fills", None)
                state.pop("fills_incomplete", None)
            try:
                if not state.get("fills"):
                    cursor = int(state.get("fills_cursor") or _ms(start))
                    fills = self.service.client.user_fills_by_time(
                        address, cursor, _ms(end), backfill=True)
                    rows = self.service.ingest_wallet_fills(
                        address, fills, dex=getattr(self.cfg, "hl_dex", "xyz"),
                        data_origin=BACKFILL_ORIGIN)
                    state["fills_rows"] = int(state.get("fills_rows") or 0) + rows
                    state["fills_pages"] = int(state.get("fills_pages") or 0) + 1
                    times = [int(x["time"]) for x in fills
                             if str(x.get("time", "")).isdigit()]
                    max_time = max(times, default=cursor)
                    terminal_gap = (len(fills) >= 2000 and
                                    (max_time <= cursor or state["fills_pages"] >= 5))
                    complete = len(fills) < 2000 or terminal_gap
                    state.update({"fills": complete, "fills_cursor": max_time,
                                  "fills_incomplete": terminal_gap})
                    self._set_cursor(scope, state)
                    if terminal_gap:
                        self._record_gap(address, start, end,
                                         "USER_FILLS_PAGE_INCOMPLETE_OR_10000_CAP",
                                         {"rows": state["fills_rows"],
                                          "pages": state["fills_pages"]})
                    item_status = ("INCOMPLETE" if terminal_gap else
                                   "SUCCESS" if complete else "PROGRESS")
                    self._save_item(run_id, "WALLET_FILLS", address, status=item_status,
                                    rows=rows,
                                    cursor=str(max_time),
                                    gap_reason="10K_OR_PAGINATION_CAP" if terminal_gap else None,
                                    details=state)
                    return {"status": "SUCCESS", "step": "fills", "address": address,
                            "rows": rows, "incomplete": terminal_gap,
                            "complete": complete, "cursor": max_time}
                if not state.get("funding"):
                    cursor = int(state.get("funding_cursor") or _ms(start))
                    values = self.service.client.user_funding(
                        address, cursor, _ms(end), dex=getattr(self.cfg, "hl_dex", "xyz"),
                        backfill=True)
                    rows = self.service.ingest_wallet_funding(
                        address, values, data_origin=BACKFILL_ORIGIN)
                    state["funding_rows"] = int(state.get("funding_rows") or 0) + rows
                    state["funding_pages"] = int(state.get("funding_pages") or 0) + 1
                    times = [int(x["time"]) for x in values
                             if str(x.get("time", "")).isdigit()]
                    max_time = max(times, default=cursor)
                    terminal_gap = (len(values) >= 2000 and
                                    (max_time <= cursor or state["funding_pages"] >= 20))
                    complete = len(values) < 2000 or terminal_gap
                    state.update({"funding": complete, "funding_cursor": max_time,
                                  "funding_incomplete": terminal_gap})
                    self._set_cursor(scope, state)
                    self._save_item(run_id, "WALLET_FUNDING", address,
                                    status="INCOMPLETE" if terminal_gap else
                                    "SUCCESS" if complete else "PROGRESS",
                                    rows=rows, cursor=str(max_time),
                                    gap_reason="PAGINATION_STALLED" if terminal_gap else None,
                                    details=state)
                    return {"status": "SUCCESS", "step": "funding", "address": address,
                            "rows": rows, "incomplete": terminal_gap,
                            "complete": complete}
                if not state.get("ledger"):
                    cursor = int(state.get("ledger_cursor") or _ms(start))
                    values = self.service.client.user_ledger_updates(
                        address, cursor, _ms(end), backfill=True)
                    rows = self.service.ingest_wallet_ledger(
                        address, values, data_origin=BACKFILL_ORIGIN)
                    state["ledger_rows"] = int(state.get("ledger_rows") or 0) + rows
                    state["ledger_pages"] = int(state.get("ledger_pages") or 0) + 1
                    times = [int(x["time"]) for x in values
                             if str(x.get("time", "")).isdigit()]
                    max_time = max(times, default=cursor)
                    terminal_gap = (len(values) >= 2000 and
                                    (max_time <= cursor or state["ledger_pages"] >= 20))
                    complete = len(values) < 2000 or terminal_gap
                    state.update({"ledger": complete, "ledger_cursor": max_time,
                                  "ledger_incomplete": terminal_gap})
                    self._set_cursor(scope, state)
                    self._save_item(run_id, "WALLET_LEDGER", address,
                                    status="INCOMPLETE" if terminal_gap else
                                    "SUCCESS" if complete else "PROGRESS",
                                    rows=rows, cursor=str(max_time),
                                    gap_reason="PAGINATION_STALLED" if terminal_gap else None,
                                    details=state)
                    return {"status": "SUCCESS", "step": "ledger", "address": address,
                            "rows": rows, "incomplete": terminal_gap,
                            "complete": complete}
                if not state.get("state"):
                    current = self.service.client.clearinghouse_state(
                        address, getattr(self.cfg, "hl_dex", "xyz"))
                    counts = self.service.ingest_clearinghouse_state(
                        address, current, data_origin="PROSPECTIVE_CONFIRMATION")
                    state.update({"state": True, "state_rows": sum(counts.values())})
                    self._set_cursor(scope, state)
                    self._save_item(run_id, "WALLET_STATE", address, status="SUCCESS",
                                    rows=sum(counts.values()), details=state)
                    return {"status": "SUCCESS", "step": "state", "address": address,
                            "rows": sum(counts.values())}
                self.service.upsert_wallet_candidate(address, discovered_from="AUTO_BACKFILL")
                cycles = self.service.rebuild_wallet_cycles(
                    address, data_origin=BACKFILL_ORIGIN)
                derived = self.rebuild_wallet_pit(address, start=start, end=end)
                state.update({"derived": True, "complete": True, "cycles": cycles,
                              "pit_dates": derived})
                if (state.get("fills_incomplete") or state.get("funding_incomplete")
                        or state.get("ledger_incomplete")):
                    with self.hl_db.connect() as conn:
                        conn.execute("UPDATE hl_wallet_candidates SET status='LIMITED' WHERE address=?",
                                     (address,))
                self._set_cursor(scope, state)
                self._save_item(run_id, "WALLET_DERIVED", address, status="SUCCESS",
                                rows=cycles + derived, details=state)
                return {"status": "SUCCESS", "step": "derived", "address": address,
                        "rows": cycles + derived}
            except HyperliquidApiError as exc:
                status = "RETRY" if exc.code == "RATE_LIMIT_BUDGET" else "INCOMPLETE"
                self._save_item(run_id, "WALLET", address, status=status,
                                gap_reason=exc.code, details={"error": type(exc).__name__})
                return {"status": status, "address": address, "error": exc.code, "rows": 0}
        return {"status": "COMPLETE", "rows": 0}

    def rebuild_wallet_pit(self, address: str, *, start: datetime,
                           end: datetime) -> int:
        """按历史日期升序分类和评分；查询层保证不会读取 as_of 之后的周期。"""
        with self.hl_db.connect(read_only=True) as conn:
            days = [str(r[0]) for r in conn.execute(
                "SELECT DISTINCT substr(event_time,1,10) FROM hl_wallet_fills "
                "WHERE address=? AND event_time>=? AND event_time<=? ORDER BY 1",
                (address, start.isoformat(), end.isoformat())).fetchall()]
        written = 0
        et = ZoneInfo("America/New_York")
        for raw_day in days:
            day = date.fromisoformat(raw_day)
            as_of = datetime.combine(day, dt_time(23, 59, 59), tzinfo=et).astimezone(
                timezone.utc).isoformat()
            with self.hl_db.connect(read_only=True) as conn:
                count = conn.execute(
                    "SELECT COUNT(*) FROM hl_wallet_fills WHERE address=? AND event_time<=?",
                    (address, as_of)).fetchone()[0]
            if count < 20:
                continue
            self.service.classify_wallet(address, valid_from=as_of,
                                         data_origin=BACKFILL_ORIGIN)
            for model in ("INTRADAY", "SWING"):
                self.service.score_wallet(address, model=model, as_of=as_of,
                                          data_origin=BACKFILL_ORIGIN)
                written += 1
        return written

    def freeze_pit_groups(self) -> int:
        with self.hl_db.connect(read_only=True) as conn:
            dates = [str(r[0]) for r in conn.execute(
                "SELECT DISTINCT substr(as_of,1,10) FROM hl_wallet_scores "
                "WHERE data_origin=? ORDER BY 1", (BACKFILL_ORIGIN,)).fetchall()]
        written = 0
        for day in dates:
            effective = self.service._scheduled_group_effective_at(f"{day}T21:00:00+00:00")
            for model in ("INTRADAY", "SWING"):
                result = self.service.freeze_wallet_group(
                    model=model, effective_at=effective, data_origin=BACKFILL_ORIGIN)
                written += result.get("status") == "READY"
        return written

    def backfill_one_candle(self, *, run_id: str, start: datetime,
                            end: datetime, interval: str = "5m") -> dict[str, Any]:
        with self.hl_db.connect(read_only=True) as conn:
            coins = [str(r[0]) for r in conn.execute(
                "SELECT coin FROM hl_assets WHERE is_delisted=0 AND lower(category) IN ('stock','stocks') "
                "ORDER BY coin").fetchall()]
        for coin in coins:
            scope = f"history:v2:candle:{interval}:{coin}:{start.date()}:{end.date()}"
            if self._cursor(scope).get("complete"):
                continue
            try:
                values = self.service.client.candle_snapshot(
                    coin, interval, _ms(start), _ms(end), backfill=True)
                rows = self.service.ingest_candles(
                    coin, interval, values, data_origin=BACKFILL_ORIGIN)
                state = {"complete": True, "rows": rows, "note":
                         "成交K线不作为历史mark/oracle锚点"}
                self._set_cursor(scope, state)
                self._save_item(run_id, "CANDLE", f"{coin}:{interval}",
                                status="SUCCESS", rows=rows, details=state)
                return {"status": "SUCCESS", "coin": coin, "rows": rows}
            except HyperliquidApiError as exc:
                status = "RETRY" if exc.code == "RATE_LIMIT_BUDGET" else "INCOMPLETE"
                self._save_item(run_id, "CANDLE", f"{coin}:{interval}", status=status,
                                gap_reason=exc.code, details={"error": type(exc).__name__})
                return {"status": status, "coin": coin, "error": exc.code, "rows": 0}
        return {"status": "COMPLETE", "rows": 0}

    def backfill_one_market_funding(self, *, run_id: str, start: datetime,
                                    end: datetime) -> dict[str, Any]:
        """回拉一个股票perp的一页官方 funding/premium 历史。

        fundingHistory 是可回溯的小时序列，但不含 mark/oracle/OI。游标按官方
        inclusive startTime 重读边界并依赖主键去重；只有短页才标记该币完成。
        """
        with self.hl_db.connect(read_only=True) as conn:
            coins = [str(row[0]) for row in conn.execute(
                "SELECT coin FROM hl_assets WHERE is_delisted=0 "
                "AND lower(category) IN ('stock','stocks') ORDER BY coin"
            ).fetchall()]
        start_ms, end_ms = _ms(start), _ms(end)
        for coin in coins:
            scope = f"history:v2:market_funding:{coin}"
            state = self._cursor(scope)
            if state.get("complete") and end_ms < int(state.get("next_refresh_ms") or 0):
                continue
            cursor = max(int(state.get("start_ms") or start_ms),
                         int(state.get("cursor_ms") or start_ms))
            try:
                page = self.service.client.funding_history(
                    coin, cursor, end_ms, backfill=True)
            except HyperliquidApiError as exc:
                status = "RETRY" if exc.code == "RATE_LIMIT_BUDGET" else "INCOMPLETE"
                self._save_item(run_id, "MARKET_FUNDING", coin, status=status,
                                gap_reason=exc.code,
                                details={"cursor_ms": cursor, "error": type(exc).__name__})
                return {"status": status, "coin": coin, "rows": 0, "error": exc.code}
            valid: list[tuple[str, str, int, Any, Any, str, str, str]] = []
            max_time = cursor
            malformed = 0
            for item in page:
                if not isinstance(item, dict) or str(item.get("coin") or "") != coin:
                    malformed += 1
                    continue
                try:
                    event_ms = int(item["time"])
                except (KeyError, TypeError, ValueError):
                    malformed += 1
                    continue
                if event_ms < start_ms or event_ms > end_ms:
                    malformed += 1
                    continue
                event_time = datetime.fromtimestamp(
                    event_ms / 1000, tz=timezone.utc).isoformat()
                valid.append((coin, event_time, event_ms, item.get("fundingRate"),
                              item.get("premium"), _json(item), utcnow_iso(),
                              BACKFILL_ORIGIN))
                max_time = max(max_time, event_ms)
            with self.hl_db.connect() as conn:
                before = conn.total_changes
                conn.executemany(
                    "INSERT OR IGNORE INTO hl_market_funding_history(coin,event_time,event_time_ms,"
                    "funding_rate,funding_premium_index,raw_json,ingested_at,data_origin) "
                    "VALUES(?,?,?,?,?,?,?,?)", valid)
                written = conn.total_changes - before
            if len(page) >= 500 and max_time <= cursor and written == 0:
                state = {**state, "complete": False, "start_ms": int(
                    state.get("start_ms") or start_ms), "cursor_ms": cursor,
                         "rows": int(state.get("rows") or 0), "stalled": True}
                self._set_cursor(scope, state)
                self._save_item(run_id, "MARKET_FUNDING", coin, status="INCOMPLETE",
                                rows=written, gap_reason="FUNDING_CURSOR_STALLED",
                                details={**state, "received": len(page),
                                         "malformed": malformed})
                return {"status": "INCOMPLETE", "coin": coin, "rows": written,
                        "error": "FUNDING_CURSOR_STALLED"}
            complete = len(page) < 500
            state = {"complete": complete,
                     "start_ms": int(state.get("start_ms") or start_ms),
                     "cursor_ms": max_time,
                     "next_refresh_ms": end_ms + 60 * 60 * 1000 if complete else None,
                     "rows": int(state.get("rows") or 0) + written,
                     "received": len(page), "malformed": malformed}
            self._set_cursor(scope, state)
            status = "INCOMPLETE" if malformed else "SUCCESS"
            self._save_item(run_id, "MARKET_FUNDING", coin, status=status,
                            rows=written, gap_reason="UNPARSEABLE_ROWS" if malformed else None,
                            details=state)
            return {"status": status, "coin": coin, "rows": written,
                    "received": len(page), "complete": complete,
                    "cursor_ms": max_time, "malformed": malformed}
        return {"status": "COMPLETE", "rows": 0}

    def run_market_funding_step(self, *, days: int = 180) -> dict[str, Any]:
        """创建可审计run并执行一页市场资金费回溯。"""
        end = datetime.now(timezone.utc)
        start = end - timedelta(days=max(1, int(days)))
        scope = "market_funding"
        self._close_interrupted_runs(scope)
        run_id = "hlbf_" + uuid.uuid4().hex
        with self.hl_db.connect() as conn:
            conn.execute(
                "INSERT INTO hl_backfill_runs(run_id,started_at,status,requested_start,requested_end,"
                "data_origin,scope_json) VALUES(?,?,?,?,?,?,?)",
                (run_id, utcnow_iso(), "RUNNING", start.isoformat(), end.isoformat(),
                 BACKFILL_ORIGIN, _json({"scope": scope, "days": days})),
            )
        try:
            result = self.backfill_one_market_funding(
                run_id=run_id, start=start, end=end)
            incomplete = int(result.get("status") in {"INCOMPLETE", "RETRY"})
            with self.hl_db.connect() as conn:
                conn.execute(
                    "UPDATE hl_backfill_runs SET finished_at=?,status=?,rows_written=?,"
                    "incomplete_items=? WHERE run_id=?",
                    (utcnow_iso(), "PARTIAL" if incomplete else "SUCCESS",
                     int(result.get("rows") or 0), incomplete, run_id),
                )
            return {"run_id": run_id, **result}
        except Exception as exc:
            with self.hl_db.connect() as conn:
                conn.execute(
                    "UPDATE hl_backfill_runs SET finished_at=?,status='ERROR',error_code=?,"
                    "error_message=? WHERE run_id=?",
                    (utcnow_iso(), type(exc).__name__, str(exc)[:500], run_id),
                )
            raise

    def run_equity_step(self, broker: Any, *, days: int = 180,
                        source: str = "IBKR_historical_5m",
                        chunk_days: int | None = None) -> dict[str, Any]:
        """每次只回拉一个映射标的的一个受限时间块。

        5分钟bar按IBKR官方step-size最多请求一周；scheduler保持单并发和至少
        15秒间隔，既避免占满chain executor，也不会退化成数百天的逐日任务。
        """
        end = datetime.now(timezone.utc)
        start = end - timedelta(days=max(1, int(days)))
        self._close_interrupted_runs("equity")
        run_id = "hlbf_" + uuid.uuid4().hex
        with self.hl_db.connect() as conn:
            conn.execute(
                "INSERT INTO hl_backfill_runs(run_id,started_at,status,requested_start,requested_end,"
                "data_origin,scope_json) VALUES(?,?,?,?,?,?,?)",
                (run_id, utcnow_iso(), "RUNNING", start.isoformat(), end.isoformat(),
                 BACKFILL_ORIGIN, _json({"scope": "equity", "days": days})),
            )
        with self.hl_db.connect(read_only=True) as conn:
            stock_coins = {str(r[0]) for r in conn.execute(
                "SELECT coin FROM hl_assets WHERE is_delisted=0 AND lower(category)='stocks'")}
        with self.main_db.connect() as conn:
            mapped = conn.execute(
                "SELECT coin,canonical_symbol FROM hl_asset_mappings "
                "WHERE status IN ('READY','SUSPECT') AND conid IS NOT NULL "
                "ORDER BY canonical_symbol").fetchall()
            symbols = ["SPY", *[str(r[1]).upper() for r in mapped
                                if str(r[0]) in stock_coins]]
        symbols = list(dict.fromkeys(symbols))
        for symbol in symbols:
            source_key = source.lower().replace("_historical_5m", "")
            scope = f"history:v2:equity:{source_key}:{symbol}:{start.date()}:{end.date()}"
            state = self._cursor(scope)
            if state.get("complete"):
                continue
            cursor_end = _dt(state["cursor_end"]) if state.get("cursor_end") else end
            requested_chunk = (chunk_days if chunk_days is not None else getattr(
                self.cfg, "hl_equity_backfill_chunk_days", 7))
            requested_chunk = max(1, min(int(days), int(requested_chunk)))
            window_start = max(start, cursor_end - timedelta(days=requested_chunk))
            try:
                from .hyperliquid_results import save_equity_bars
                bars = broker.fetch_5m_bars(symbol, window_start, cursor_end, use_rth=True)
                rows = save_equity_bars(
                    self.main_db, bars, source=source,
                    data_origin=BACKFILL_ORIGIN)
                next_end = window_start - timedelta(microseconds=1)
                state = {"complete": next_end <= start, "cursor_end": next_end.isoformat(),
                         "rows": int(state.get("rows") or 0) + rows}
                self._set_cursor(scope, state)
                self._save_item(run_id, "EQUITY_5M", f"{source}:{symbol}", status="SUCCESS",
                                rows=rows, details={**state, "source": source})
                with self.hl_db.connect() as conn:
                    conn.execute(
                        "UPDATE hl_backfill_runs SET finished_at=?,status='SUCCESS',rows_written=? "
                        "WHERE run_id=?", (utcnow_iso(), rows, run_id))
                return {"run_id": run_id, "status": "SUCCESS", "symbol": symbol,
                        "rows": rows, "complete": state["complete"],
                        "cursor_end": state["cursor_end"], "source": source}
            except Exception as exc:
                self._save_item(run_id, "EQUITY_5M", f"{source}:{symbol}", status="RETRY",
                                gap_reason=type(exc).__name__,
                                details={"error": str(exc)[:200], "source": source})
                with self.hl_db.connect() as conn:
                    conn.execute(
                        "UPDATE hl_backfill_runs SET finished_at=?,status='PARTIAL',"
                        "incomplete_items=1,error_code=?,error_message=? WHERE run_id=?",
                        (utcnow_iso(), type(exc).__name__, str(exc)[:500], run_id))
                return {"run_id": run_id, "status": "RETRY", "symbol": symbol,
                        "error": type(exc).__name__, "rows": 0, "source": source}
        with self.hl_db.connect() as conn:
            conn.execute("UPDATE hl_backfill_runs SET finished_at=?,status='SUCCESS' WHERE run_id=?",
                         (utcnow_iso(), run_id))
        return {"run_id": run_id, "status": "COMPLETE", "rows": 0, "source": source}

    def import_asset_context_archive(self, path: str | Path, *,
                                     run_id: str | None = None) -> dict[str, Any]:
        """流式导入官方 asset_ctxs，并只保留显式属于当前DEX的5分钟桶。

        官方文件可能同时含第一 perp DEX 的无前缀 coin。无前缀行只有在文件
        同时给出 ``dex=xyz`` 时才接纳；绝不根据同名 ticker 猜测为 ``xyz:*``。
        一日文件先在内存中压成每币每5分钟最后一条，再按时间顺序批量写库，
        避免把数百万原始行整文件解压到内存或逐行开启SQLite事务。
        """
        path = Path(path)
        digest = hashlib.sha256()
        with path.open("rb") as compressed:
            for chunk in iter(lambda: compressed.read(1024 * 1024), b""):
                digest.update(chunk)
        source_sha256 = digest.hexdigest()
        if path.suffix == ".lz4":
            try:
                import lz4.frame
            except ImportError as exc:  # pragma: no cover - 部署依赖门禁覆盖
                raise RuntimeError("缺少 lz4 依赖，无法读取官方归档") from exc
            handle = lz4.frame.open(path, mode="rt", encoding="utf-8")
        else:
            handle = path.open("rt", encoding="utf-8", newline="")
        target_dex = str(getattr(self.cfg, "hl_dex", "xyz") or "xyz").lower()
        with self.hl_db.connect(read_only=True) as conn:
            allowed_coins = {str(row[0]) for row in conn.execute(
                "SELECT coin FROM hl_assets WHERE lower(dex)=? AND is_delisted=0",
                (target_dex,),
            ).fetchall()}
        latest: dict[tuple[str, str], tuple[datetime, dict[str, Any]]] = {}
        seen = skipped = invalid = 0
        with handle:
            for row in csv.DictReader(handle):
                seen += 1
                payload = self._archive_ctx_payload(row)
                raw_coin = str(payload.get("coin") or payload.get("name") or
                               payload.get("asset") or "").strip()
                explicit_dex = str(payload.get("dex") or payload.get("perp_dex") or "").lower()
                if not raw_coin:
                    invalid += 1
                    continue
                candidate = raw_coin if ":" in raw_coin else (
                    f"{target_dex}:{raw_coin}" if explicit_dex == target_dex else "")
                if not candidate.startswith(f"{target_dex}:"):
                    skipped += 1
                    continue
                if allowed_coins and candidate not in allowed_coins:
                    skipped += 1
                    continue
                normalized = self._normalize_archive_ctx(payload)
                if normalized is None:
                    invalid += 1
                    continue
                coin, observed_at, ctx = normalized
                observed = _dt(observed_at).astimezone(timezone.utc)
                bucket = observed.replace(
                    minute=(observed.minute // 5) * 5, second=0, microsecond=0)
                key = (bucket.isoformat(), coin)
                prior = latest.get(key)
                if prior is None or observed >= prior[0]:
                    latest[key] = (observed, ctx)
        grouped: dict[str, list[tuple[str, dict[str, Any]]]] = {}
        for (bucket, coin), (_observed, ctx) in latest.items():
            grouped.setdefault(bucket, []).append((coin, ctx))
        written = 0
        for bucket in sorted(grouped):
            items = sorted(grouped[bucket], key=lambda item: item[0])
            written += self.service.ingest_contexts(
                [{"name": coin} for coin, _ctx in items],
                [ctx for _coin, ctx in items], dex=target_dex,
                observed_at=bucket, data_origin=BACKFILL_ORIGIN)
        status = "SUCCESS" if invalid == 0 else "INCOMPLETE"
        if run_id:
            self._save_item(run_id, "ASSET_CTX_ARCHIVE", path.name, status=status,
                            rows=written, gap_reason="UNPARSEABLE_ROWS" if invalid else None,
                            details={"seen": seen, "skipped": skipped, "invalid": invalid,
                                     "buckets": len(grouped), "sha256": source_sha256})
        return {"status": status, "rows": written, "seen": seen, "skipped": skipped,
                "invalid": invalid, "buckets": len(grouped), "sha256": source_sha256}

    @staticmethod
    def download_asset_context_day(day: date, destination: str | Path) -> Path:
        """从官方 requester-pays bucket 下载单日 asset_ctxs；需要服务器 AWS 凭据。"""
        try:
            import boto3
        except ImportError as exc:  # pragma: no cover - 镜像依赖门禁覆盖
            raise RuntimeError("缺少 boto3，无法访问 requester-pays 归档") from exc
        destination = Path(destination)
        destination.mkdir(parents=True, exist_ok=True)
        target = destination / f"{day:%Y%m%d}.csv.lz4"
        if target.is_file() and target.stat().st_size > 0:
            return target
        partial = target.with_suffix(target.suffix + ".part")
        client = boto3.client("s3")
        response = client.get_object(
            Bucket="hyperliquid-archive",
            Key=f"asset_ctxs/{day:%Y%m%d}.csv.lz4",
            RequestPayer="requester",
        )
        body = response["Body"]
        written = 0
        try:
            with partial.open("wb") as handle:
                while True:
                    chunk = body.read(1024 * 1024)
                    if not chunk:
                        break
                    handle.write(chunk)
                    written += len(chunk)
                handle.flush()
                os.fsync(handle.fileno())
            expected = response.get("ContentLength")
            if expected is not None and written != int(expected):
                raise IOError(f"asset_ctxs下载长度不匹配: expected={expected}, actual={written}")
            partial.replace(target)
        except Exception:
            partial.unlink(missing_ok=True)
            raise
        finally:
            body.close()
        return target

    def import_node_fills_archive(self, path: str | Path, *,
                                  run_id: str | None = None) -> dict[str, Any]:
        """导入官方 node_fills JSONL/LZ4；未知结构计入 invalid，不猜地址。"""
        path = Path(path)
        raw = path.read_bytes()
        if path.suffix == ".lz4":
            try:
                import lz4.frame
            except ImportError as exc:  # pragma: no cover
                raise RuntimeError("缺少 lz4 依赖，无法读取 node_fills") from exc
            raw = lz4.frame.decompress(raw)
        batches: dict[str, list[dict[str, Any]]] = {}
        written = invalid = seen = 0

        def flush() -> None:
            nonlocal written
            for address, fills in list(batches.items()):
                if fills:
                    written += self.service.ingest_wallet_fills(
                        address, fills, dex=getattr(self.cfg, "hl_dex", "xyz"),
                        data_origin=BACKFILL_ORIGIN)
            batches.clear()

        for line in io.StringIO(raw.decode("utf-8")):
            if not line.strip():
                continue
            try:
                decoded = json.loads(line)
            except ValueError:
                invalid += 1
                continue
            items = decoded if isinstance(decoded, list) else decoded.get("fills", [decoded])
            for item in items:
                fill = item.get("fill") if isinstance(item, dict) and isinstance(
                    item.get("fill"), dict) else item
                if not isinstance(fill, dict):
                    invalid += 1
                    continue
                address = str(item.get("user") or item.get("address") or
                              fill.get("user") or fill.get("address") or "").lower()
                if not (address.startswith("0x") and len(address) == 42):
                    invalid += 1
                    continue
                batches.setdefault(address, []).append(fill)
                seen += 1
                if seen % 5000 == 0:
                    flush()
        flush()
        status = "SUCCESS" if invalid == 0 else "INCOMPLETE"
        if run_id:
            self._save_item(run_id, "NODE_FILLS_ARCHIVE", path.name, status=status,
                            rows=written, gap_reason="UNPARSEABLE_ROWS" if invalid else None,
                            details={"invalid": invalid, "seen": seen,
                                     "sha256": hashlib.sha256(raw).hexdigest()})
        return {"status": status, "rows": written, "seen": seen, "invalid": invalid}

    @staticmethod
    def _archive_ctx_payload(row: dict[str, Any]) -> dict[str, Any]:
        payload: dict[str, Any] = dict(row)
        for key in ("ctx", "context", "data", "asset_ctx"):
            value = row.get(key)
            if value:
                try:
                    decoded = json.loads(value)
                    if isinstance(decoded, dict):
                        payload.update(decoded)
                except (TypeError, ValueError):
                    pass
        return payload

    @classmethod
    def _normalize_archive_ctx(cls, row: dict[str, Any]) -> tuple[str, str, dict[str, Any]] | None:
        payload = cls._archive_ctx_payload(row)
        coin = str(payload.get("coin") or payload.get("name") or payload.get("asset") or "")
        explicit_dex = str(payload.get("dex") or payload.get("perp_dex") or "").lower()
        if coin and ":" not in coin and explicit_dex == "xyz":
            coin = "xyz:" + coin
        raw_time = (payload.get("time") or payload.get("timestamp") or payload.get("block_time")
                    or payload.get("observed_at"))
        if not coin.startswith("xyz:") or raw_time in (None, ""):
            return None
        try:
            observed = (datetime.fromtimestamp(int(raw_time) / 1000, tz=timezone.utc).isoformat()
                        if str(raw_time).isdigit() else _dt(str(raw_time)).isoformat())
        except (TypeError, ValueError, OverflowError):
            return None
        aliases = {
            "markPx": ("markPx", "mark_px"), "oraclePx": ("oraclePx", "oracle_px"),
            "midPx": ("midPx", "mid_px"), "funding": ("funding",),
            "premium": ("premium", "premium_index"),
            "openInterest": ("openInterest", "open_interest"),
            "dayNtlVlm": ("dayNtlVlm", "day_ntl_vlm"),
            "dayBaseVlm": ("dayBaseVlm", "day_base_vlm"),
            "impactPxs": ("impactPxs", "impact_pxs"),
        }
        ctx: dict[str, Any] = {}
        for target, names in aliases.items():
            for name in names:
                if payload.get(name) not in (None, ""):
                    ctx[target] = payload[name]
                    break
        if not ctx.get("markPx") or not ctx.get("oraclePx"):
            return None
        if isinstance(ctx.get("impactPxs"), str):
            try:
                ctx["impactPxs"] = json.loads(ctx["impactPxs"])
            except ValueError:
                ctx["impactPxs"] = None
        if not ctx.get("impactPxs") and payload.get("impact_bid_px") not in (None, "") \
                and payload.get("impact_ask_px") not in (None, ""):
            ctx["impactPxs"] = [payload["impact_bid_px"], payload["impact_ask_px"]]
        return coin, observed, ctx

    def _record_gap(self, address: str, start: datetime, end: datetime,
                    reason: str, details: dict[str, Any]) -> None:
        with self.hl_db.connect() as conn:
            conn.execute(
                "INSERT INTO hl_data_gaps(coin,started_at,ended_at,reason,status,details_json,created_at) "
                "VALUES(NULL,?,?,?,?,?,?)",
                (start.isoformat(), end.isoformat(), reason, "INCOMPLETE",
                 _json({"address": address, **details}), utcnow_iso()),
            )

    def run_step(self, *, days: int = 180, scope: str = "wallets") -> dict[str, Any]:
        end = datetime.now(timezone.utc)
        start = end - timedelta(days=max(1, int(days)))
        self._close_interrupted_runs(scope)
        run_id = "hlbf_" + uuid.uuid4().hex
        scope = str(scope).lower()
        with self.hl_db.connect() as conn:
            conn.execute(
                "INSERT INTO hl_backfill_runs(run_id,started_at,status,requested_start,requested_end,"
                "data_origin,scope_json) VALUES(?,?,?,?,?,?,?)",
                (run_id, utcnow_iso(), "RUNNING", start.isoformat(), end.isoformat(),
                 BACKFILL_ORIGIN, _json({"scope": scope, "days": days})),
            )
        try:
            admitted = self.admit_top_candidates(start=start)
            if scope == "wallets":
                result = self.backfill_one_wallet_step(run_id=run_id, start=start, end=end)
            elif scope == "candles":
                result = self.backfill_one_candle(run_id=run_id, start=start, end=end)
            else:
                raise ValueError("scope 必须是 wallets 或 candles")
            rows = int(result.get("rows") or 0)
            incomplete = 1 if (result.get("status") in {"INCOMPLETE", "RETRY"}
                               or result.get("incomplete")) else 0
            status = "PARTIAL" if incomplete else "SUCCESS"
            with self.hl_db.connect() as conn:
                conn.execute(
                    "UPDATE hl_backfill_runs SET finished_at=?,status=?,rows_written=?,incomplete_items=? "
                    "WHERE run_id=?", (utcnow_iso(), status, rows, incomplete, run_id))
            return {"run_id": run_id, "admitted": len(admitted), **result}
        except Exception as exc:
            with self.hl_db.connect() as conn:
                conn.execute(
                    "UPDATE hl_backfill_runs SET finished_at=?,status='ERROR',error_code=?,error_message=? "
                    "WHERE run_id=?", (utcnow_iso(), type(exc).__name__, str(exc)[:500], run_id))
            raise


def main() -> int:
    import argparse
    from .config import load_config
    from .db import Database
    from .hyperliquid_client import HyperliquidClient, WeightBudget

    parser = argparse.ArgumentParser(description="Hyperliquid validation-policy-v2 历史回溯")
    parser.add_argument("--scope", choices=("wallets", "candles", "funding"),
                        default="wallets")
    parser.add_argument("--days", type=int, default=180)
    parser.add_argument("--asset-ctx-file")
    parser.add_argument("--node-fills-file")
    parser.add_argument("--download-asset-ctx-day")
    parser.add_argument("--archive-dir", default="./data/hyperliquid-archive")
    args = parser.parse_args()
    cfg = load_config()
    hl_db = HyperliquidDatabase(cfg.hl_db_path)
    hl_db.init_schema()
    main_db = Database(cfg.db_path)
    main_db.init_schema()
    client = HyperliquidClient(
        timeout=cfg.hl_http_timeout,
        budget=WeightBudget(cfg.hl_api_weight_per_minute),
        backfill_limit_pct=float(getattr(cfg, "hl_backfill_weight_pct", "0.20")),
    )
    executor = HistoricalBackfillExecutor(
        cfg, HyperliquidService(cfg, hl_db, client), main_db)
    if args.download_asset_ctx_day:
        target = executor.download_asset_context_day(
            date.fromisoformat(args.download_asset_ctx_day), args.archive_dir)
        result = executor.import_asset_context_archive(target)
    elif args.asset_ctx_file:
        result = executor.import_asset_context_archive(args.asset_ctx_file)
    elif args.node_fills_file:
        result = executor.import_node_fills_archive(args.node_fills_file)
    elif args.scope == "funding":
        result = executor.run_market_funding_step(days=args.days)
    else:
        result = executor.run_step(days=args.days, scope=args.scope)
    print(_json(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
