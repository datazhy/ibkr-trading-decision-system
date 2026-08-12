"""Hyperliquid 快速通道历史数据完整性报告。

报告回答一个明确问题：当前数据是否足以冻结规则并开始历史锁定测试。
它同时读取独立 Hyperliquid 库和主库，但只向 Hyperliquid 库写入一条不可变
报告快照，不创建跨库事务或外键。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

from .config import load_config
from .db import Database
from .hyperliquid_db import HyperliquidDatabase
from .us_market_calendar import REGULAR_OPEN, is_trading_day, regular_close_time


REPORT_VERSION = "hl-data-quality-v1"
ALLOWED_ORIGINS = {
    "LIVE", "HISTORICAL_BACKFILL", "HISTORICAL_LOCKED_TEST",
    "PROSPECTIVE_CONFIRMATION",
}
ORIGIN_TABLES = (
    "hl_recent_trades", "hl_trade_buckets_5m", "hl_address_flow_buckets_5m",
    "hl_asset_context_buckets_5m", "hl_market_candles", "hl_wallet_fills",
    "hl_market_funding_history",
    "hl_wallet_funding", "hl_wallet_ledger_updates", "hl_wallet_equity_snapshots",
    "hl_wallet_positions", "hl_wallet_cycles", "hl_wallet_daily_metrics",
    "hl_wallet_classification_intervals", "hl_wallet_memberships",
    "hl_wallet_group_versions", "hl_wallet_scores", "hl_signal_features_5m",
    "hl_signal_features_daily", "hl_signal_episodes",
)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def _parse_dt(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _scalar(conn: Any, sql: str, params: Iterable[Any] = ()) -> Any:
    row = conn.execute(sql, tuple(params)).fetchone()
    return None if row is None else row[0]


def _rows(conn: Any, sql: str, params: Iterable[Any] = ()) -> list[dict[str, Any]]:
    return [dict(row) for row in conn.execute(sql, tuple(params)).fetchall()]


def _table_exists(conn: Any, table: str) -> bool:
    return bool(_scalar(conn, "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)))


def _short_wallet(address: str) -> str:
    address = str(address)
    return address if len(address) < 14 else address[:8] + "…" + address[-6:]


def _aws_credential_state() -> dict[str, Any]:
    """只报告凭据来源类别，绝不返回 access key、token 或文件内容。"""
    env_names = sorted(k for k in os.environ if k.startswith("AWS_"))
    configured_paths = [
        os.getenv("AWS_SHARED_CREDENTIALS_FILE"),
        os.getenv("AWS_CONFIG_FILE"),
        str(Path.home() / ".aws" / "credentials"),
        str(Path.home() / ".aws" / "config"),
        "/etc/boto.cfg",
    ]
    shared_files = sorted({str(Path(path)) for path in configured_paths
                           if path and Path(path).is_file()})
    method = None
    if os.getenv("AWS_ACCESS_KEY_ID") and os.getenv("AWS_SECRET_ACCESS_KEY"):
        method = "ENVIRONMENT"
    elif os.getenv("AWS_WEB_IDENTITY_TOKEN_FILE") and os.getenv("AWS_ROLE_ARN"):
        method = "WEB_IDENTITY"
    elif os.getenv("AWS_CONTAINER_CREDENTIALS_RELATIVE_URI") or os.getenv(
            "AWS_CONTAINER_CREDENTIALS_FULL_URI"):
        method = "CONTAINER_ROLE"
    elif shared_files:
        method = "SHARED_CONFIG"
    resolved = False
    resolved_method = method
    # 不在每小时质量任务里探测隐式 EC2 metadata：当 metadata 不可达时，
    # botocore 可能等待数十秒并占住 chain executor，反而挤掉 30 秒历史回溯。
    # requester-pays 下载器仍会做真实鉴权；运行身份应通过显式环境、容器角色、
    # Web Identity 或共享配置提供，质量报告只解析这些可审计的 provider hint。
    if method is not None:
        try:
            import boto3
            credentials = boto3.Session().get_credentials()
            if credentials is not None:
                resolved = True
                resolved_method = str(
                    getattr(credentials, "method", None) or method
                ).upper()
        except Exception:  # noqa: BLE001 — 报告不能因凭据探测失败而中断
            resolved = False
    return {
        "available": resolved,
        "method": resolved_method,
        "aws_env_names": env_names,
        "shared_config_files": shared_files,
    }


def _expected_rth_bars(start: date, end: date) -> tuple[int, int]:
    sessions = bars = 0
    cursor = start
    while cursor <= end:
        if is_trading_day(cursor):
            close = regular_close_time(cursor)
            if close is not None:
                sessions += 1
                minutes = (datetime.combine(cursor, close) -
                           datetime.combine(cursor, REGULAR_OPEN)).seconds // 60
                bars += minutes // 5
        cursor += timedelta(days=1)
    return sessions, bars


def _check(check_id: str, category: str, status: str, severity: str, *,
           observed: Any, threshold: str, evidence: str, impact: str,
           remediation: str) -> dict[str, Any]:
    return {
        "id": check_id, "category": category, "status": status,
        "severity": severity, "observed": observed, "threshold": threshold,
        "evidence": evidence, "impact": impact, "remediation": remediation,
    }


def build_quality_snapshot(cfg: Any, hl_db: HyperliquidDatabase, main_db: Database,
                           *, now: datetime | None = None,
                           aws_state: dict[str, Any] | None = None) -> dict[str, Any]:
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    end = now
    start = end - timedelta(days=max(1, int(getattr(cfg, "hl_history_backfill_days", 180))))
    aws_state = dict(aws_state or _aws_credential_state())
    checks: list[dict[str, Any]] = []

    with hl_db.connect(read_only=True) as hl, main_db.connect() as main:
        hl_integrity = _scalar(hl, "PRAGMA integrity_check")
        main_integrity = _scalar(main, "PRAGMA integrity_check")
        checks.append(_check(
            "database_integrity", "integrity",
            "PASS" if hl_integrity == main_integrity == "ok" else "FAIL", "CRITICAL",
            observed={"hyperliquid": hl_integrity, "dashboard": main_integrity},
            threshold="两个SQLite库 integrity_check 均为 ok",
            evidence="分别对 hyperliquid.db 和 dashboard.db 执行 PRAGMA integrity_check",
            impact="数据库损坏会使所有覆盖率和PIT结论失去可信度。",
            remediation="停止锁定测试，先从独立备份恢复并重新校验。",
        ))

        latest_run = _rows(hl, "SELECT * FROM hl_backfill_runs ORDER BY started_at DESC LIMIT 1")
        latest_run = latest_run[0] if latest_run else None
        latest_at = _parse_dt(latest_run.get("started_at") if latest_run else None)
        age_minutes = ((now - latest_at).total_seconds() / 60) if latest_at else None
        backfill_fresh = age_minutes is not None and age_minutes <= 20
        checks.append(_check(
            "backfill_freshness", "timeliness", "PASS" if backfill_fresh else "FAIL", "HIGH",
            observed={"latest_started_at": latest_run.get("started_at") if latest_run else None,
                      "age_minutes": round(age_minutes, 2) if age_minutes is not None else None},
            threshold="最近回溯步骤不超过20分钟",
            evidence="hl_backfill_runs.started_at 的最新值",
            impact="回溯停止会使计划完成时间失真。",
            remediation="检查 risk-hyperliquid 与 sync chain executor 的健康和调度诊断。",
        ))

        origin_profiles: list[dict[str, Any]] = []
        invalid_origins = 0
        for table in ORIGIN_TABLES:
            if not _table_exists(hl, table):
                continue
            for row in _rows(hl, f"SELECT data_origin,COUNT(*) AS rows FROM {table} GROUP BY data_origin"):
                origin = str(row.get("data_origin") or "")
                count = int(row.get("rows") or 0)
                origin_profiles.append({"table": table, "data_origin": origin, "rows": count})
                if origin not in ALLOWED_ORIGINS:
                    invalid_origins += count
        for table in ("equity_bars_5m", "hl_signal_outcomes"):
            if not _table_exists(main, table):
                continue
            for row in _rows(main, f"SELECT data_origin,COUNT(*) AS rows FROM {table} GROUP BY data_origin"):
                origin = str(row.get("data_origin") or "")
                count = int(row.get("rows") or 0)
                origin_profiles.append({"table": table, "data_origin": origin, "rows": count})
                if origin not in ALLOWED_ORIGINS:
                    invalid_origins += count
        checks.append(_check(
            "origin_labels", "validity", "PASS" if invalid_origins == 0 else "FAIL", "CRITICAL",
            observed={"invalid_rows": invalid_origins, "profiles": len(origin_profiles)},
            threshold="所有带来源标签的行都属于四个互斥枚举",
            evidence="逐表 GROUP BY data_origin；LIVE/HISTORICAL_BACKFILL/HISTORICAL_LOCKED_TEST/PROSPECTIVE_CONFIRMATION",
            impact="来源混用会污染锁定测试或把回补样本伪装成前瞻样本。",
            remediation="隔离非法来源行，修复写入路径后重新生成报告。",
        ))

        assets = _rows(hl, "SELECT coin,category,is_delisted FROM hl_assets WHERE is_delisted=0")
        stock_assets = [row for row in assets if str(row.get("category") or "").lower() == "stocks"]
        mappings = _rows(main, "SELECT coin,canonical_symbol,status,reason FROM hl_asset_mappings")
        stock_coins = {str(row["coin"]) for row in stock_assets}
        stock_mappings = [row for row in mappings if str(row.get("coin")) in stock_coins]
        mapping_counts = Counter(
            str(row.get("status") or "UNKNOWN") for row in stock_mappings
        )
        ready_mappings = [row for row in stock_mappings if row.get("status") == "READY"]
        map_denominator = max(1, len(stock_assets))
        mapping_ratio = min(1.0, len(ready_mappings) / map_denominator)
        checks.append(_check(
            "asset_mapping_ready", "integrity", "PASS" if mapping_ratio >= 0.90 else "BLOCKED", "CRITICAL",
            observed={"ready": len(ready_mappings), "active_stock_assets": len(stock_assets),
                      "ratio": round(mapping_ratio, 4), "status_counts": dict(mapping_counts)},
            threshold="至少90%的stocks候选完成三重映射和3个RTH偏差检查",
            evidence="hl_assets 与主库 hl_asset_mappings",
            impact="错误ticker/conid会把链上信号配到错误现实股票。",
            remediation="继续积累RTH收盘锚点并完成IBKR合约消歧；未通过者保持BLOCKED。",
        ))

        historical_ctx = _rows(hl,
            "SELECT COUNT(*) AS rows,COUNT(DISTINCT c.coin) AS coins,MIN(c.bucket_start) AS min_time,"
            "MAX(c.bucket_start) AS max_time FROM hl_asset_context_buckets_5m c "
            "JOIN hl_assets a ON a.coin=c.coin AND a.is_delisted=0 AND lower(a.category)='stocks' "
            "WHERE c.data_origin='HISTORICAL_BACKFILL' AND c.bucket_start>=? AND c.bucket_start<=?",
            (start.isoformat(), end.isoformat()))[0]
        ctx_coin_ratio = min(1.0, int(historical_ctx.get("coins") or 0) / map_denominator)
        aws_ready = bool(aws_state.get("available"))
        checks.append(_check(
            "asset_ctx_requester_identity", "access", "PASS" if aws_ready else "BLOCKED", "CRITICAL",
            observed={"available": aws_ready, "method": aws_state.get("method"),
                      "env_names": aws_state.get("aws_env_names", []),
                      "shared_config_file_count": len(aws_state.get("shared_config_files", []))},
            threshold="boto3标准凭据链可解析一个允许 requester-pays 的AWS身份",
            evidence="容器环境变量、共享配置、Web/Container role与 boto3 credential resolver（不读取秘密值）",
            impact="无法下载官方asset_ctxs，历史oracle/mark/basis特征不可重建。",
            remediation="向risk-hyperliquid注入只读S3计费身份或实例/容器角色，并验证单日get_object。",
        ))
        ctx_ready = int(historical_ctx.get("rows") or 0) > 0 and ctx_coin_ratio >= 0.90
        checks.append(_check(
            "historical_asset_ctx", "completeness", "PASS" if ctx_ready else "BLOCKED", "CRITICAL",
            observed={**historical_ctx, "coin_coverage_ratio": round(ctx_coin_ratio, 4)},
            threshold="请求窗口内至少90%候选股票有官方历史5分钟ctx；后续逐标的检查时间覆盖率",
            evidence="hl_asset_context_buckets_5m 且 data_origin=HISTORICAL_BACKFILL",
            impact="缺失时不能计算off-RTH oracle return、basis变化或历史信号。",
            remediation="逐日下载并导入官方asset_ctxs；成交K线不得替代ctx。",
        ))

        market_funding = _rows(hl,
            "SELECT COUNT(*) AS rows,COUNT(DISTINCT f.coin) AS coins,MIN(f.event_time) AS min_time,"
            "MAX(f.event_time) AS max_time FROM hl_market_funding_history f "
            "JOIN hl_assets a ON a.coin=f.coin AND a.is_delisted=0 AND lower(a.category)='stocks' "
            "WHERE f.data_origin='HISTORICAL_BACKFILL' AND f.event_time>=? AND f.event_time<=?",
            (start.isoformat(), end.isoformat()))[0]
        funding_cursors = _rows(
            hl, "SELECT scope,cursor_value FROM hl_cursors "
                "WHERE scope LIKE 'history:v2:market_funding:%'")
        funding_complete_coins: set[str] = set()
        funding_incomplete_coins: set[str] = set()
        stock_coins = {str(row.get("coin")) for row in stock_assets}
        for row in funding_cursors:
            coin = str(row.get("scope") or "").removeprefix(
                "history:v2:market_funding:")
            if coin not in stock_coins:
                continue
            try:
                cursor_state = json.loads(row.get("cursor_value") or "{}")
            except (TypeError, ValueError):
                cursor_state = {}
            if isinstance(cursor_state, dict) and cursor_state.get("complete"):
                funding_complete_coins.add(coin)
            else:
                funding_incomplete_coins.add(coin)
        funding_coin_ratio = min(1.0, len(funding_complete_coins) / map_denominator)
        funding_cursor_observed = {
            **market_funding,
            "complete_coins": len(funding_complete_coins),
            "incomplete_coins": len(funding_incomplete_coins),
            "coin_coverage_ratio": round(funding_coin_ratio, 4),
        }
        funding_ready = (int(market_funding.get("rows") or 0) > 0
                         and funding_coin_ratio >= 0.90)
        checks.append(_check(
            "historical_market_funding", "completeness",
            "PASS" if funding_ready else "BLOCKED", "CRITICAL",
            observed=funding_cursor_observed,
            threshold="至少90%候选股票有官方fundingHistory；逐币短页游标已完成",
            evidence="hl_market_funding_history，fundingHistory返回的fundingRate与premium",
            impact="无AWS缩减历史轨缺少官方可回溯的资金费和impact-price premium确认。",
            remediation="保持共享20%限流预算分页；premium只作funding premium index，不作mark-oracle basis。",
        ))

        controls = _rows(main, "SELECT address,mode FROM hl_wallet_controls WHERE mode IN ('PINNED','AUTO') ORDER BY address")
        addresses = [str(row["address"]).lower() for row in controls]
        cursor_rows = _rows(hl, "SELECT scope,cursor_value,updated_at FROM hl_cursors WHERE scope LIKE 'history:v2:wallet:%'")
        wallet_state: dict[str, dict[str, Any]] = {}
        for row in cursor_rows:
            parts = str(row["scope"]).split(":")
            if len(parts) < 4:
                continue
            address = parts[3].lower()
            try:
                state = json.loads(row.get("cursor_value") or "{}")
            except (TypeError, ValueError):
                state = {}
            if isinstance(state, dict):
                state["updated_at"] = row.get("updated_at")
                wallet_state[address] = state
        per_wallet: list[dict[str, Any]] = []
        complete_wallets = incomplete_wallets = 0
        for address in addresses:
            state = wallet_state.get(address, {})
            fills = int(_scalar(hl, "SELECT COUNT(*) FROM hl_wallet_fills WHERE address=? AND data_origin='HISTORICAL_BACKFILL'", (address,)) or 0)
            funding = int(_scalar(hl, "SELECT COUNT(*) FROM hl_wallet_funding WHERE address=? AND data_origin='HISTORICAL_BACKFILL'", (address,)) or 0)
            ledger = int(_scalar(hl, "SELECT COUNT(*) FROM hl_wallet_ledger_updates WHERE address=? AND data_origin='HISTORICAL_BACKFILL'", (address,)) or 0)
            complete = bool(state.get("fills") and state.get("funding") and state.get("ledger")
                            and not state.get("fills_incomplete") and not state.get("funding_incomplete")
                            and not state.get("ledger_incomplete"))
            incomplete = bool(state.get("fills_incomplete") or state.get("funding_incomplete")
                              or state.get("ledger_incomplete"))
            complete_wallets += int(complete)
            incomplete_wallets += int(incomplete)
            per_wallet.append({
                "wallet": _short_wallet(address), "fills": fills, "funding": funding,
                "ledger": ledger, "fills_pages": int(state.get("fills_pages") or 0),
                "complete": complete, "incomplete": incomplete,
                "updated_at": state.get("updated_at"),
            })
        min_complete_wallets = min(20, len(addresses)) if addresses else 20
        wallet_ready = complete_wallets >= min_complete_wallets and incomplete_wallets < len(addresses)
        checks.append(_check(
            "wallet_history_completion", "completeness", "PASS" if wallet_ready else "BLOCKED", "CRITICAL",
            observed={"tracked": len(addresses), "complete": complete_wallets,
                      "incomplete": incomplete_wallets, "with_cursor": len(wallet_state)},
            threshold=f"至少{min_complete_wallets}个跟踪钱包完成fills/funding/ledger且无截断",
            evidence="hl_wallet_controls 与 history:v2:wallet 游标、三张历史账本",
            impact="周期PnL、资金流调整收益和钱包质量分不可审计。",
            remediation="继续跨轮次分页；受10k截断钱包保持LIMITED并从信号池剔除。",
        ))

        candle_stats = _rows(hl,
            "SELECT COUNT(*) AS rows,COUNT(DISTINCT c.coin) AS coins,MIN(c.open_time) AS min_time,"
            "MAX(c.open_time) AS max_time FROM hl_market_candles c "
            "JOIN hl_assets a ON a.coin=c.coin AND a.is_delisted=0 AND lower(a.category)='stocks' "
            "WHERE c.data_origin='HISTORICAL_BACKFILL' "
            "AND c.open_time>=? AND c.open_time<=?", (start.isoformat(), end.isoformat()))[0]
        candle_ratio = min(1.0, int(candle_stats.get("coins") or 0) / map_denominator)
        checks.append(_check(
            "historical_candles", "completeness", "PASS" if candle_ratio >= 0.90 else "WARN", "MEDIUM",
            observed={**candle_stats, "coin_coverage_ratio": round(candle_ratio, 4)},
            threshold="至少90%候选股票有历史成交K线（仅作成交/价格辅助，不作ctx锚点）",
            evidence="hl_market_candles 且 data_origin=HISTORICAL_BACKFILL",
            impact="影响成交量、波动和执行诊断，但不能补偿oracle/mark缺失。",
            remediation="继续轮换candleSnapshot回溯，保持与ctx语义分离。",
        ))

        expected_sessions, expected_bars = _expected_rth_bars(start.date(), end.date())
        required_symbols = sorted({"SPY", *[str(row["canonical_symbol"]).upper()
                                            for row in stock_mappings
                                            if row.get("status") in {"READY", "SUSPECT"}]})
        equity_rows: list[dict[str, Any]] = []
        complete_symbols = 0
        for symbol in required_symbols:
            stats = _rows(main,
                "SELECT COUNT(DISTINCT bar_start) AS rows,COUNT(DISTINCT substr(bar_start,1,10)) AS sessions,"
                "MIN(bar_start) AS min_time,MAX(bar_start) AS max_time FROM equity_bars_5m "
                "WHERE symbol=? AND source IN ('IBKR_historical_5m','FUTU_historical_5m') "
                "AND data_origin='HISTORICAL_BACKFILL' "
                "AND bar_start>=? AND bar_start<=?", (symbol, start.isoformat(), end.isoformat()))[0]
            sources = _rows(main,
                "SELECT source,COUNT(*) AS rows FROM equity_bars_5m WHERE symbol=? "
                "AND data_origin='HISTORICAL_BACKFILL' AND bar_start>=? AND bar_start<=? "
                "GROUP BY source", (symbol, start.isoformat(), end.isoformat()))
            ratio = min(1.0, int(stats.get("rows") or 0) / max(1, expected_bars))
            complete = ratio >= 0.90
            complete_symbols += int(complete)
            equity_rows.append({"symbol": symbol, **stats,
                                "source_rows": {row["source"]: int(row["rows"]) for row in sources},
                                "expected_bars": expected_bars,
                                "coverage_ratio": round(ratio, 4), "complete": complete})
        equity_ratio = complete_symbols / max(1, len(required_symbols))
        checks.append(_check(
            "equity_5m_history", "completeness", "PASS" if equity_ratio >= 0.90 else "BLOCKED", "CRITICAL",
            observed={"required_symbols": len(required_symbols), "complete_symbols": complete_symbols,
                      "symbol_coverage_ratio": round(equity_ratio, 4),
                      "expected_sessions": expected_sessions, "expected_bars_per_symbol": expected_bars},
            threshold="至少90%必需标的在请求窗口达到90%的预期RTH 5分钟bar覆盖",
            evidence="主库 equity_bars_5m，IBKR_historical_5m，HISTORICAL_BACKFILL",
            impact="无法计算2h/20d现实收益、SPY alpha、MFE或MAE。",
            remediation="保持单并发7日分块回溯；无bar标记PENDING，不伪造价格。",
        ))

        futu_rows = int(_scalar(main,
            "SELECT COUNT(*) FROM equity_bars_5m WHERE source='FUTU_historical_5m' "
            "AND data_origin='HISTORICAL_BACKFILL' AND bar_start>=? AND bar_start<=?",
            (start.isoformat(), end.isoformat())) or 0)
        overlap = _rows(main,
            "SELECT i.symbol,i.bar_start,CAST(i.c AS REAL) AS ibkr_close,"
            "CAST(f.c AS REAL) AS futu_close FROM equity_bars_5m i "
            "JOIN equity_bars_5m f ON f.symbol=i.symbol AND f.bar_start=i.bar_start "
            "WHERE i.source='IBKR_historical_5m' AND f.source='FUTU_historical_5m' "
            "AND i.data_origin='HISTORICAL_BACKFILL' AND f.data_origin='HISTORICAL_BACKFILL' "
            "AND i.bar_start>=? AND i.bar_start<=? LIMIT 200000",
            (start.isoformat(), end.isoformat()))
        deviations: list[float] = []
        overlap_by_symbol = Counter()
        for row in overlap:
            ibkr_close = float(row.get("ibkr_close") or 0)
            futu_close = float(row.get("futu_close") or 0)
            if ibkr_close > 0 and futu_close > 0:
                deviations.append(abs(futu_close / ibkr_close - 1.0))
                overlap_by_symbol[str(row["symbol"])] += 1
        deviations.sort()
        p95 = (deviations[min(len(deviations) - 1, int(len(deviations) * 0.95))]
               if deviations else None)
        verified_symbols = sum(1 for count in overlap_by_symbol.values() if count >= 78)
        required_verified = min(10, len([row for row in equity_rows
                                        if row.get("source_rows", {}).get("FUTU_historical_5m", 0) > 0]))
        source_agreement = (futu_rows == 0 or
                            (required_verified > 0 and verified_symbols >= required_verified
                             and p95 is not None and p95 <= 0.005))
        checks.append(_check(
            "equity_source_agreement", "consistency",
            "PASS" if source_agreement else "BLOCKED", "CRITICAL",
            observed={"futu_rows": futu_rows, "overlap_rows": len(deviations),
                      "verified_symbols": verified_symbols,
                      "required_verified_symbols": required_verified,
                      "p95_close_deviation": round(p95, 8) if p95 is not None else None},
            threshold="使用富途时至少10个标的（不足10则全部）各有≥78个同窗bar，且收盘价偏差p95≤0.5%",
            evidence="equity_bars_5m 按 symbol+bar_start 对齐 IBKR_historical_5m 与 FUTU_historical_5m",
            impact="时区、复权或标的映射错误会系统性污染现实结果。",
            remediation="IBKR继续并行回补；超阈值标的隔离并检查时区、复权和conid。",
        ))

        daily_existing = _rows(main,
            "SELECT source,COUNT(*) AS rows,COUNT(DISTINCT symbol) AS symbols,"
            "MIN(date) AS min_date,MAX(date) AS max_date FROM daily_bars GROUP BY source")

        gap_rows = _rows(hl,
            "SELECT reason,status,COUNT(*) AS rows,MIN(started_at) AS first_seen,"
            "MAX(COALESCE(ended_at,started_at)) AS last_seen FROM hl_data_gaps "
            "GROUP BY reason,status ORDER BY rows DESC")
        critical_gap_rows = sum(int(row["rows"]) for row in gap_rows
                                if row["status"] == "INCOMPLETE" and
                                row["reason"] not in {"RECENT_TRADES_NO_OVERLAP"})
        checks.append(_check(
            "unresolved_gaps", "completeness", "PASS" if critical_gap_rows == 0 else "WARN", "HIGH",
            observed={"material_incomplete_gaps": critical_gap_rows,
                      "gap_categories": len(gap_rows)},
            threshold="影响钱包/ctx/正股结果的INCOMPLETE缺口为0；recentTrades无重叠单独诊断",
            evidence="hl_data_gaps 按 reason/status 汇总",
            impact="缺口会缩小可用钱包/标的样本，并可能造成选择偏差。",
            remediation="逐项排除受影响实体；缺口关闭前不得进入锁定样本。",
        ))

        future_cutoff = (now + timedelta(minutes=5)).isoformat()
        future_counts = {
            "wallet_fills": int(_scalar(hl, "SELECT COUNT(*) FROM hl_wallet_fills WHERE event_time>?", (future_cutoff,)) or 0),
            "asset_ctx": int(_scalar(hl, "SELECT COUNT(*) FROM hl_asset_context_buckets_5m WHERE bucket_start>?", (future_cutoff,)) or 0),
            "market_funding": int(_scalar(hl, "SELECT COUNT(*) FROM hl_market_funding_history WHERE event_time>?", (future_cutoff,)) or 0),
            "candles": int(_scalar(hl, "SELECT COUNT(*) FROM hl_market_candles WHERE open_time>?", (future_cutoff,)) or 0),
            "equity_bars": int(_scalar(main, "SELECT COUNT(*) FROM equity_bars_5m WHERE bar_start>?", (future_cutoff,)) or 0),
        }
        future_total = sum(future_counts.values())
        checks.append(_check(
            "future_timestamps", "validity", "PASS" if future_total == 0 else "FAIL", "CRITICAL",
            observed={"rows": future_total, "by_table": future_counts},
            threshold="事件时间不得超过报告时间5分钟",
            evidence="五张核心历史/行情表的事件时间上界检查",
            impact="未来时间会造成PIT泄漏和错误窗口对齐。",
            remediation="隔离异常行，修复时区/时间戳解析后重放。",
        ))

        run_rows = _rows(hl,
            "SELECT started_at,status,rows_written,incomplete_items,scope_json FROM hl_backfill_runs "
            "ORDER BY started_at LIMIT 5000")
        cumulative = 0
        progress_rows: list[dict[str, Any]] = []
        for row in run_rows:
            cumulative += int(row.get("rows_written") or 0)
            try:
                scope = json.loads(row.get("scope_json") or "{}").get("scope", "unknown")
            except (TypeError, ValueError):
                scope = "unknown"
            progress_rows.append({
                "started_at": row.get("started_at"), "scope": scope,
                "status": row.get("status"), "rows_written": int(row.get("rows_written") or 0),
                "cumulative_rows": cumulative, "incomplete_items": int(row.get("incomplete_items") or 0),
            })

        check_by_id = {row["id"]: row for row in checks}
        readiness_rows = [
            {"component": "资产映射", "complete": len(ready_mappings), "total": map_denominator,
             "coverage_ratio": round(mapping_ratio, 4), "status": check_by_id["asset_mapping_ready"]["status"]},
            {"component": "历史asset ctx", "complete": int(historical_ctx.get("coins") or 0), "total": map_denominator,
             "coverage_ratio": round(ctx_coin_ratio, 4), "status": check_by_id["historical_asset_ctx"]["status"]},
            {"component": "历史funding/premium", "complete": len(funding_complete_coins),
             "total": map_denominator, "coverage_ratio": round(funding_coin_ratio, 4),
             "status": check_by_id["historical_market_funding"]["status"]},
            {"component": "钱包三账本", "complete": complete_wallets, "total": max(1, len(addresses)),
             "coverage_ratio": round(complete_wallets / max(1, len(addresses)), 4), "status": check_by_id["wallet_history_completion"]["status"]},
            {"component": "成交K线标的", "complete": int(candle_stats.get("coins") or 0), "total": map_denominator,
             "coverage_ratio": round(candle_ratio, 4), "status": check_by_id["historical_candles"]["status"]},
            {"component": "现实正股5m", "complete": complete_symbols, "total": max(1, len(required_symbols)),
             "coverage_ratio": round(equity_ratio, 4), "status": check_by_id["equity_5m_history"]["status"]},
        ]

        blocking = [row for row in checks if row["status"] in {"BLOCKED", "FAIL"}
                    and row["severity"] == "CRITICAL"]
        reduced_exclusions = {"asset_ctx_requester_identity", "historical_asset_ctx"}
        reduced_blocking = [row for row in blocking if row["id"] not in reduced_exclusions]
        failing = [row for row in checks if row["status"] in {"BLOCKED", "FAIL", "WARN"}]
        status = "BLOCKED" if blocking else ("PARTIAL" if failing else "READY")
        status_counts = dict(Counter(row["status"] for row in checks))
        summary = {
            "decision": "NOT_READY_FOR_LOCKED_TEST" if status != "READY" else "READY_FOR_COMMITMENT",
            "status": status, "generated_at": _iso(now), "requested_start": _iso(start),
            "requested_end": _iso(end), "report_version": REPORT_VERSION,
            "check_status_counts": status_counts, "blocking_check_ids": [row["id"] for row in blocking],
            "reduced_history_decision": ("READY_FOR_REDUCED_COMMITMENT" if not reduced_blocking
                                         else "NOT_READY_FOR_REDUCED_LOCKED_TEST"),
            "reduced_blocking_check_ids": [row["id"] for row in reduced_blocking],
            "assets": {"active": len(assets), "stocks": len(stock_assets),
                       "mapping_status_counts": dict(mapping_counts)},
            "wallets": {"tracked": len(addresses), "complete": complete_wallets,
                        "incomplete": incomplete_wallets},
            "asset_ctx": historical_ctx, "market_funding": funding_cursor_observed,
            "candles": candle_stats,
            "equity": {"required_symbols": len(required_symbols),
                       "complete_symbols": complete_symbols, "expected_bars_per_symbol": expected_bars,
                       "existing_daily_sources": daily_existing,
                       "source_agreement": check_by_id["equity_source_agreement"]["observed"]},
            "backfill": {"run_count": len(run_rows), "latest": latest_run},
            "aws": {"available": aws_ready, "method": aws_state.get("method")},
        }

    material = {"summary": summary, "checks": checks, "readiness": readiness_rows,
                "wallets": per_wallet, "equity": equity_rows, "gaps": gap_rows,
                "origins": origin_profiles, "progress": progress_rows}
    digest = hashlib.sha256(_json(material).encode()).hexdigest()
    report_id = f"hldq_{now:%Y%m%dT%H%M%SZ}_{digest[:10]}"
    return {"report_id": report_id, **material}


def render_markdown(report: dict[str, Any]) -> str:
    summary = report["summary"]
    checks = report["checks"]
    lines = [
        "# Hyperliquid XYZ 历史数据完整性报告",
        "",
        f"生成时间：`{summary['generated_at']}`  ",
        f"评估窗口：`{summary['requested_start']}` 至 `{summary['requested_end']}`  ",
        f"完整ctx轨结论：**{summary['decision']}**（状态 `{summary['status']}`）  ",
        f"无AWS缩减历史轨结论：**{summary.get('reduced_history_decision')}**",
        "",
        "## 技术摘要",
        "",
    ]
    blockers = [row for row in checks if row["status"] in {"BLOCKED", "FAIL"}]
    if blockers:
        lines.append("当前不能创建锁定测试承诺。阻断项：")
        lines.append("")
        for row in blockers:
            lines.append(f"- **{row['id']}**：{row['evidence']}；当前值 `{_json(row['observed'])}`。")
    else:
        lines.append("所有关键完整性门禁已通过，可以进入不可变验证承诺步骤。")
    lines.extend(["", "## 检查结果", "",
                  "| 检查 | 状态 | 严重级别 | 当前值 | 门槛 |",
                  "|---|---|---|---|---|"])
    for row in checks:
        observed = _json(row["observed"]).replace("|", "\\|")
        lines.append(f"| `{row['id']}` | {row['status']} | {row['severity']} | `{observed}` | {row['threshold']} |")
    lines.extend(["", "## 主要风险及修复顺序", ""])
    for index, row in enumerate([x for x in checks if x["status"] != "PASS"], 1):
        lines.append(f"{index}. **{row['id']}**：{row['impact']} {row['remediation']}")
    lines.extend(["", "## 方法与限制", "",
                  "- 报告按当前服务器快照生成；仍在到达的回补分区会改变后续覆盖率。",
                  "- 成交K线与asset ctx分别统计，绝不把candleSnapshot当作历史mark/oracle。",
                  "- fundingHistory只提供资金费率和funding premium index；缩减轨不生成历史oracle、mark、OI或basis。",
                  "- 缩减轨使用独立rules/feature-set版本，只能做影子历史检验，不能替代完整ctx模型晋级。",
                  "- funding/ledger行数可能合法为0，钱包完成度以可恢复游标是否完整结束为准。",
                  "- 报告只判断能否开始锁定测试，不证明信号有效，也不授权生产影响。",
                  "", "## 下一步", "",
                  "1. 按无AWS决策继续补齐缩减轨的funding、钱包、映射和正股对账门。",
                  "2. 完整ctx轨保持历史BLOCKED，只从实时ctx前瞻积累，不用成交K线代替。",
                  "3. 缩减轨自身状态变为READY后，再冻结该版资产/钱包群体与rules/policy hash，并在读取锁定结果前创建不可变commitment。",
                  ""])
    return "\n".join(lines)


def write_quality_report(cfg: Any, *, output_dir: str | Path | None = None,
                         now: datetime | None = None,
                         aws_state: dict[str, Any] | None = None) -> dict[str, Any]:
    hl_db = HyperliquidDatabase(cfg.hl_db_path)
    if not hl_db.available():
        raise RuntimeError("Hyperliquid数据库不可用")
    main_db = Database(cfg.db_path)
    report = build_quality_snapshot(cfg, hl_db, main_db, now=now, aws_state=aws_state)
    output = Path(output_dir or (Path(cfg.hl_db_path).parent / "hyperliquid-reports"))
    output.mkdir(parents=True, exist_ok=True)
    json_path = output / f"{report['report_id']}.json"
    md_path = output / f"{report['report_id']}.md"
    latest_json = output / "latest.json"
    latest_md = output / "latest.md"
    payload = _json(report) + "\n"
    markdown = render_markdown(report)
    json_path.write_text(payload, encoding="utf-8")
    md_path.write_text(markdown, encoding="utf-8")
    latest_json.write_text(payload, encoding="utf-8")
    latest_md.write_text(markdown, encoding="utf-8")
    with hl_db.connect() as conn:
        conn.execute(
            "INSERT INTO hl_data_quality_reports(report_id,generated_at,requested_start,requested_end,"
            "report_version,status,summary_json,checks_json,report_path) VALUES(?,?,?,?,?,?,?,?,?)",
            (report["report_id"], report["summary"]["generated_at"],
             report["summary"]["requested_start"], report["summary"]["requested_end"],
             REPORT_VERSION, report["summary"]["status"], _json(report["summary"]),
             _json(report["checks"]), str(md_path)),
        )
    return {"report_id": report["report_id"], "status": report["summary"]["status"],
            "decision": report["summary"]["decision"], "json_path": str(json_path),
            "markdown_path": str(md_path), "blocking": report["summary"]["blocking_check_ids"]}


def main() -> int:
    parser = argparse.ArgumentParser(description="生成Hyperliquid历史数据完整性报告")
    parser.add_argument("--output-dir")
    args = parser.parse_args()
    cfg = load_config()
    result = write_quality_report(cfg, output_dir=args.output_dir)
    print(_json(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
