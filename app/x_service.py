"""X 推文摄取与读取服务。

职责分工：
  · `x_monitor`  —— 只读源库（别人的项目，一个字都不能写）
  · `x_service`  —— 摄取到本地表 + 供页面读取

**为什么必须摄取而不是直接读源库**：源库每天裁剪、只保留最近 5,000 条。
它是数据管道，不是数据仓库。直接读的话，今天能看到的历史明天就没了。

摄取是幂等的：`tweet_id` 是主键，`INSERT OR REPLACE` 重复跑不会产生第二行。
游标存在 `sync_state`，按 `received_at` 增量 —— 绝不用 `created_at`
（Twitter 原始格式，字符串比较会按星期几字母排）。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from . import x_monitor as xm
from .logging_setup import get_logger

log = get_logger()

#: 游标键。存最后一次摄取到的 received_at。
CURSOR_KEY = "x_tweets_cursor"
REPORT_CURSOR_KEY = "x_reports_cursor"

#: 单轮最多摄取多少条。全量回填约 1,900 条，四轮拉完。
BATCH = 500


#: 北京时区。摄取起点按北京日切分 —— 用户看的、页面显示的都是北京时间，
#: 用 UTC 日切会让「今天」比预期多出 8 小时的昨天内容。
BEIJING = timezone(timedelta(hours=8))


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def initial_cursor(now: datetime | None = None) -> str:
    """首次摄取的起点：**当天北京 00:00**（转成 UTC）。

    按需求只保留从启用当天开始的推文，不回填历史 —— 历史推文没有中文版
    （源库 2026-07-30 之前的 `text_zh` 全为 NULL），存下来也只是原文堆积。

    早报不受此限制：29 份全部保留，那是已经成稿的结论，有长期价值。
    """
    now = now or datetime.now(timezone.utc)
    bj = now.astimezone(BEIJING)
    start_bj = bj.replace(hour=0, minute=0, second=0, microsecond=0)
    return start_bj.astimezone(timezone.utc).isoformat()


def ingest(db, *, path: str | None = None, max_batches: int = 20,
           now: datetime | None = None) -> dict:
    """增量摄取推文 / 早报 / 博主名单。返回统计。

    源库未挂载时直接返回 `available=False` —— **这不是错误**，
    只是 X 推送功能没启用，不该让同步流程报警。
    """
    if not xm.available(path):
        return {"available": False, "tweets": 0, "reports": 0, "authors": 0}

    stats = {"available": True, "tweets": 0, "reports": 0, "authors": 0,
             "batches": 0, "cursor": None}
    try:
        # 首次运行：从**今天北京 00:00** 开始，不回填历史推文。
        # 存下来是为了让「起点」这件事可追溯，而不是每次重启都重算一个新起点。
        cursor = db.get_sync_state(CURSOR_KEY)
        if not cursor:
            cursor = initial_cursor(now)
            db.set_sync_state(CURSOR_KEY, cursor)
            log.info("X 摄取首次运行，起点设为今天北京 00:00（%s UTC），不回填历史",
                     cursor[:19])
        for _ in range(max_batches):
            rows = xm.fetch_tweets(since=cursor, limit=BATCH, path=path)
            if not rows:
                break
            n = db.upsert_x_tweets(_prepare(rows))
            stats["tweets"] += n
            stats["batches"] += 1
            cursor = rows[-1]["received_at"]      # 游标只认 received_at
            db.set_sync_state(CURSOR_KEY, cursor)
            if len(rows) < BATCH:
                break
        stats["cursor"] = cursor

        # 早报：**全部 29 份都要**，不设起点限制。
        # 与推文不同 —— 早报是已成稿的结论，有长期价值；
        # day 是 YYYY-MM-DD，可安全字符串比较
        rday = db.get_sync_state(REPORT_CURSOR_KEY) or ""
        reports = xm.fetch_daily_reports(since_day=rday, path=path)
        if reports:
            stats["reports"] = db.upsert_x_daily_reports(
                [{**r, "ingested_at": _now()} for r in reports])
            db.set_sync_state(REPORT_CURSOR_KEY, reports[-1]["day"])

        # 博主名单：全量覆盖，量小（30 行），改名/停用要能反映出来
        authors = xm.fetch_handles(path=path)
        if authors:
            stats["authors"] = db.upsert_x_authors(
                [{**a, "ingested_at": _now()} for a in authors])
    except Exception:  # noqa: BLE001 — 摄取失败不得影响账户同步与风险告警
        log.warning("X 推文摄取失败（已隔离，不影响同步与告警）", exc_info=True)
        stats["error"] = True
        return stats

    if stats["tweets"] or stats["reports"]:
        log.info("X 摄取：推文 +%d 早报 +%d 博主 %d（游标 %s）",
                 stats["tweets"], stats["reports"], stats["authors"],
                 (stats["cursor"] or "-")[:19])
    return stats


def _prepare(rows: list[dict]) -> list[dict]:
    """补上我们自己的字段：解析后的 UTC 发布时间 + 摄取时间。

    `created_at_utc` 是为了能按**发布时间**排序 —— 源库那个 `created_at`
    是 Twitter 原始格式，字符串排序会得到错的顺序。解析不出来就留 None，
    页面回退到 received_at，不猜。
    """
    now = _now()
    out = []
    for r in rows:
        dt = xm.parse_created_at(r.get("created_at"))
        out.append({**r,
                    "created_at_utc": dt.isoformat() if dt else None,
                    "ingested_at": now})
    return out


# ======================= 页面读取 =======================

def _content(row: dict) -> str:
    """展示正文：中文优先，历史数据兜底原文。"""
    return (row.get("text_zh") or row.get("text") or "").strip()


def feed(db, *, days: int = 7, limit: int = 200, handle: str | None = None,
         now: datetime | None = None, before: str | None = None,
         before_id: str | None = None) -> dict:
    """推文流。默认最近 N 天，按发布时间倒序。

    「今天」按**北京时间**切分 —— 用户看的是北京时间，不是 UTC。
    """
    now = now or datetime.now(timezone.utc)
    since = (now - timedelta(days=days)).isoformat()
    rows = [dict(r) for r in db.x_tweets_since(
        since, limit=limit, handle=handle, before=before, before_id=before_id)]
    out = []
    for r in rows:
        pub = (xm.parse_created_at(r.get("created_at"))
               or _iso(r.get("created_at_utc"))
               or _iso(r.get("received_at")))
        bj = xm.to_beijing(pub)
        out.append({
            "tweet_id": r["tweet_id"],
            "cursor_at": r.get("created_at_utc") or r.get("received_at") or "",
            "handle": r["author_handle"],
            "name": r.get("author_name") or r["author_handle"],
            "content": _content(r),
            "ai_summary": r.get("ai_summary") or "",
            # 如实标出有没有中文版：历史推文只有原文，不能让人误以为是译文
            "translated": bool(r.get("enriched_at")),
            "has_original": bool(r.get("text")),
            "original": (r.get("text") or "").strip(),
            "url": r.get("tweet_url"),
            "media_count": r.get("media_count") or 0,
            "published_bj": bj.strftime("%m-%d %H:%M") if bj else "时间未知",
            "published_day": bj.strftime("%Y-%m-%d") if bj else "未知",
            "is_reply": bool(r.get("is_reply")),
            "is_quote": bool(r.get("is_quote")),
        })
    # 按北京日期分组，页面按天呈现
    groups: dict[str, list] = {}
    for t in out:
        groups.setdefault(t["published_day"], []).append(t)
    last = out[-1] if out else None
    return {"tweets": out, "days": sorted(groups.items(), reverse=True),
            "total": len(out), "window_days": days, "handle": handle,
            "has_more": len(out) == limit,
            "next_before": last.get("cursor_at") if last else None,
            "next_before_id": last.get("tweet_id") if last else None}


def _iso(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(s)
    except (ValueError, TypeError):
        return None


#: 主题热度趋势中文标签（与源项目飞书卡片同口径）
_TREND_LABEL = {"rising": "↑升温", "falling": "↓降温", "stable": "→持平"}


def _date_cn(day: str) -> str:
    """YYYY-MM-DD → 「M 月 D 日」，与飞书卡片标题一致。"""
    try:
        d = datetime.strptime(day, "%Y-%m-%d")
        return f"{d.month} 月 {d.day} 日"
    except (ValueError, TypeError):
        return day or ""


def _fmt_signal(v) -> str:
    """把连续/异常/错过信号渲染成一句中文。兼容模型偶尔返回 dict ——
    提取描述，绝不把英文字段名露给用户（与源项目飞书卡片同口径）。"""
    if isinstance(v, str):
        return v.strip()
    if not isinstance(v, dict):
        return str(v)
    desc = (v.get("description") or v.get("text") or v.get("summary")
            or v.get("content") or "").strip()
    author = v.get("author")
    topic = v.get("topic") or v.get("title")
    days = v.get("days")
    if author:
        head = f"@{author}"
    elif topic:
        head = f"「{topic}」" + (f"连续 {days} 天" if days else "")
    else:
        head = ""
    return f"{head}：{desc}" if head and desc else (desc or head or "")


def _event(e: dict) -> dict:
    """单条「最重要的事」：补相关博主与原推链接（源推 id 无法保证已本地落库，
    统一构造 x.com 规范链接，redirect 到原推，缺 id2url 也不丢链接）。"""
    authors = [str(a) for a in (e.get("related_authors") or []) if a]
    sources = [{"n": i, "url": f"https://x.com/i/status/{sid}"}
               for i, sid in enumerate(
                   [s for s in (e.get("source_tweet_ids") or []) if s][:3], 1)]
    return {
        "title": e.get("title") or "",
        "what_happened": e.get("what_happened") or "",
        "why_it_matters": e.get("why_it_matters") or "",
        "authors": authors,
        "sources": sources,
    }


def _topic(t: dict) -> dict:
    return {
        "topic": t.get("topic") or "",
        "tweet_count": t.get("tweet_count") or 0,
        "author_count": t.get("author_count") or 0,
        "trend_label": _TREND_LABEL.get(t.get("trend") or "stable", ""),
        "summary": t.get("summary") or "",
    }


def reports(db, *, limit: int = 30, before_day: str | None = None) -> list[dict]:
    """早报列表，**与源项目飞书卡片同款完整结构**：今日一句 / 三件最重要的事
    （含相关博主、原推链接）/ 主题热度（条数·人数·趋势·摘要）/ 连续信号 /
    异常信号 / 你可能错过了 / 折叠噪音统计。

    与推文流分开呈现 —— 早报是成稿结论，推文是原始素材，混在一起会让人
    分不清哪个是 AI 的判断、哪个是博主的原话。"""
    out = []
    for r in db.x_daily_reports(limit=limit, before_day=before_day):
        r = dict(r)
        parsed = xm.parse_report(r.get("report_json"))
        # 与飞书卡片同口径的截断：事 3 / 题 5 / 各类信号 4
        top_events = [_event(e) for e in (parsed.get("top_events") or [])[:3]]
        topic_heat = [_topic(t) for t in (parsed.get("topic_heat") or [])[:5]]
        cont = [s for s in (_fmt_signal(v)
                for v in (parsed.get("continuous_signals") or [])[:4]) if s]
        anom = [s for s in (_fmt_signal(v)
                for v in (parsed.get("anomaly_signals") or [])[:4]) if s]
        missed = [s for s in (_fmt_signal(v)
                  for v in (parsed.get("missed_items") or [])[:4]) if s]
        out.append({
            "day": r["day"],
            "date_cn": _date_cn(r["day"]),
            "tldr": parsed.get("tldr") or "",
            "top_events": top_events,
            "topic_heat": topic_heat,
            "continuous_signals": cont,
            "anomaly_signals": anom,
            "missed_items": missed,
            "noise": parsed.get("noise") or {},
            "parse_ok": bool(parsed),
        })
    return out


def authors(db) -> list[dict]:
    """关注的 X 博主 + 各自的推文量与最近活动。"""
    counts = {r["author_handle"]: dict(r) for r in db.x_tweet_counts_by_author()}
    out = []
    for a in db.x_authors():
        a = dict(a)
        c = counts.get(a["handle"], {})
        last = _iso(c.get("last_recv"))
        bj = xm.to_beijing(last)
        out.append({
            "handle": a["handle"],
            "name": a.get("display_name") or a["handle"],
            "active": bool(a.get("active")),
            "tweets": int(c.get("n") or 0),
            "last_bj": bj.strftime("%m-%d %H:%M") if bj else "—",
            "url": f"https://x.com/{a['handle']}",
        })
    # 有推文的排前面，按量倒序；没推文的按 handle
    out.sort(key=lambda x: (-x["tweets"], x["handle"]))
    return out


def overview(db, *, path: str | None = None) -> dict:
    """页面顶部的数据来源与覆盖情况。"""
    local = db.x_tweets_stats()
    return {
        "source_available": xm.available(path),
        "source": xm.source_stats(path=path) if xm.available(path) else {},
        "local": local,
        "note": ("源库每天裁剪、只保留最近 5,000 条，因此本项目已自行落库持久化；"
                 "中文版仅 2026-07-30 之后的新推文有，更早的显示原文。"),
    }
