"""X 推文监控数据源：只读读取 x-monitor 的 SQLite。

数据来自可选的本地 X Monitor 项目，通过只读挂载访问：

    docker-compose: ${X_MONITOR_DATA_PATH:-./data/x-monitor}:/app/x_monitor:ro

**只读是双保险**：挂载点 `:ro` + 连接串 `mode=ro`。源库是 WAL 模式，
只读读取与对方的写入可以并发，不会锁库、不影响它的推送。

## 三个必须遵守的约束（对方交接文档明确列出，踩了就是错数据）

1. **`created_at` 不能当字符串排序或比较** —— 它是 Twitter 原始格式
   `Thu Jul 30 10:23:59 +0000 2026`，直接 ORDER BY 会按星期几的字母排。
   排序与增量游标一律用 `received_at`（ISO8601 UTC）。
2. **中文版只有 2026-07-30 之后的新推文有** —— 历史约 1,878 条 `text_zh`
   为 NULL。展示一律 `COALESCE(text_zh, text)` 兜底，并把「是否有中文版」
   如实标出来，不能让用户以为看到的都是译文。
3. **源库只保留最近 5,000 条，每天自动裁剪** —— 所以我们**必须自己落库**。
   这个模块只负责读，持久化在 `x_service`。
"""
from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timedelta, timezone

from .logging_setup import get_logger

log = get_logger()

#: 容器内的只读挂载点。可用环境变量覆盖，便于测试与本地开发。
DEFAULT_DB_PATH = "/app/x_monitor/monitor.db"

#: 纯原创过滤。源库规则层已过滤转推/回复/引用，唯一例外是 `aleabitoreddit`
#: 放开了引用+回复，所以这里再挡一道。
ORIGINAL_ONLY = "is_reply = 0 AND is_retweet = 0 AND is_quote = 0"

BEIJING = timezone(timedelta(hours=8))


def db_path() -> str:
    return os.environ.get("X_MONITOR_DB", DEFAULT_DB_PATH)


def available(path: str | None = None) -> bool:
    """源库是否可读。未挂载时返回 False —— 这不是错误，只是功能未启用。"""
    p = path or db_path()
    try:
        return os.path.exists(p)
    except Exception:  # noqa: BLE001
        return False


def _connect(path: str | None = None) -> sqlite3.Connection:
    """只读连接。`mode=ro` 让任何写入直接抛错，而不是悄悄改了别人的库。"""
    p = path or db_path()
    conn = sqlite3.connect(f"file:{p}?mode=ro", uri=True, timeout=5)
    conn.row_factory = sqlite3.Row
    return conn


def parse_created_at(raw: str | None) -> datetime | None:
    """把 Twitter 原始时间解析成 datetime。

    格式 `Thu Jul 30 10:23:59 +0000 2026`。解析失败返回 None ——
    宁可显示「时间未知」，也不要猜一个假时间出来。
    """
    if not raw:
        return None
    try:
        return datetime.strptime(raw, "%a %b %d %H:%M:%S %z %Y")
    except (ValueError, TypeError):
        return None


def to_beijing(dt: datetime | None) -> datetime | None:
    return dt.astimezone(BEIJING) if dt else None


# ======================= 读取 =======================

def fetch_tweets(*, since: str = "", limit: int = 500,
                 original_only: bool = True, path: str | None = None) -> list[dict]:
    """按 `received_at` 游标增量拉取推文。

    `since` 为空则从头拉。返回按 received_at 正序，调用方把最后一条的
    `received_at` 存下来当下次游标 —— `tweet_id` 唯一，重复消费天然幂等。
    """
    sql = ["SELECT tweet_id, author_handle, author_name, author_user_id,",
           " text, text_zh, ai_summary, enriched_at, tweet_url,",
           " is_reply, is_retweet, is_quote, media_count,",
           " created_at, received_at, source",
           " FROM tweets WHERE 1=1"]
    args: list = []
    if since:
        sql.append(" AND received_at > ?")
        args.append(since)
    if original_only:
        sql.append(f" AND {ORIGINAL_ONLY}")
    sql.append(" ORDER BY received_at LIMIT ?")   # 绝不用 created_at 排序
    args.append(int(limit))
    with _connect(path) as conn:
        return [dict(r) for r in conn.execute("".join(sql), args)]


def fetch_daily_reports(*, since_day: str = "", path: str | None = None) -> list[dict]:
    """每日「昨日信号」早报成稿。`day` 是 YYYY-MM-DD，可安全字符串比较。"""
    sql = "SELECT day, report_json, created_at FROM daily_reports"
    args: list = []
    if since_day:
        sql += " WHERE day > ?"
        args.append(since_day)
    sql += " ORDER BY day"
    with _connect(path) as conn:
        return [dict(r) for r in conn.execute(sql, args)]


def fetch_handles(*, path: str | None = None) -> list[dict]:
    """监控名单（handle ↔ user_id ↔ 昵称）。"""
    with _connect(path) as conn:
        return [dict(r) for r in conn.execute(
            "SELECT handle, user_id, display_name, last_checked_at, active"
            " FROM handles ORDER BY handle")]


def source_stats(*, path: str | None = None) -> dict:
    """源库概况，用于页面上标明数据来源与新鲜度。"""
    try:
        with _connect(path) as conn:
            r = dict(conn.execute(
                "SELECT COUNT(*) n, MIN(received_at) lo, MAX(received_at) hi"
                " FROM tweets").fetchone())
            r["authors"] = conn.execute(
                "SELECT COUNT(DISTINCT author_handle) FROM tweets").fetchone()[0]
            r["reports"] = conn.execute("SELECT COUNT(*) FROM daily_reports").fetchone()[0]
            return r
    except Exception as exc:  # noqa: BLE001
        log.warning("读取 X 源库概况失败：%s", type(exc).__name__)
        return {}


# ======================= 早报解析 =======================

def parse_report(report_json: str | None) -> dict:
    """解析早报 JSON。结构见交接文档第五节。

    解析失败返回空 dict —— 早报是增值信息，坏一份不该让整页打不开。
    """
    if not report_json:
        return {}
    try:
        raw = json.loads(report_json)
    except (ValueError, TypeError):
        return {}
    if not isinstance(raw, dict):
        return {}
    rep = raw.get("report") or {}
    if not isinstance(rep, dict):
        rep = {}
    return {
        "day": raw.get("day"),
        "tldr": rep.get("tldr") or "",
        "top_events": [e for e in (rep.get("top_events") or []) if isinstance(e, dict)],
        "topic_heat": [t for t in (rep.get("topic_heat") or []) if isinstance(t, dict)],
        "continuous_signals": rep.get("continuous_signals") or [],
        "anomaly_signals": rep.get("anomaly_signals") or [],
        "missed_items": rep.get("missed_items") or [],
        "topic_stats": raw.get("topic_stats") or [],
        "noise": raw.get("noise") or {},
    }
