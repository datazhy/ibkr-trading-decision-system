"""X 推送接入验收测试。

守的主要是交接文档点名的三个坑 —— 那是「不知道就会踩」的类型：

  ① `created_at` 是 Twitter 原始格式（`Thu Jul 30 10:23:59 +0000 2026`），
     字符串排序会按**星期几的字母**排，结果完全是错的
  ② 中文版只有 2026-07-30 之后的新推文有，历史全是 NULL
  ③ 源库每天裁剪只留 5,000 条 —— 消费方必须自己落库

外加本次需求特有的一条：**只摄取从今天（北京）开始的推文，早报全要**。
"""
import json
import pathlib
import re
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from app import x_monitor as xm
from app import x_service as xs
from app.db import Database

NOW = datetime(2026, 7, 30, 12, 0, tzinfo=timezone.utc)   # 北京 20:00
BEIJING = timezone(timedelta(hours=8))


@pytest.fixture
def local(make_config):
    cfg = make_config(ib_account="U1")
    d = Database(cfg.db_path)
    d.init_schema()
    return cfg, d


@pytest.fixture
def src(tmp_path):
    """造一个仿真的源库（结构与字段名与真实 monitor.db 一致）。"""
    p = tmp_path / "monitor.db"
    c = sqlite3.connect(p)
    c.executescript("""
        CREATE TABLE tweets (
          tweet_id TEXT PRIMARY KEY, author_handle TEXT, author_name TEXT,
          author_user_id TEXT, text TEXT, tweet_url TEXT,
          is_reply INT, is_retweet INT, is_quote INT, media_count INT,
          created_at TEXT, received_at TEXT, pushed_at TEXT, source TEXT,
          text_zh TEXT, ai_summary TEXT, enriched_at TEXT);
        CREATE TABLE daily_reports (day TEXT PRIMARY KEY, report_json TEXT, created_at TEXT);
        CREATE TABLE handles (handle TEXT PRIMARY KEY, user_id TEXT,
          display_name TEXT, last_checked_at TEXT, active INT);
    """)
    c.commit()
    return p


def _tw(c, tid, *, handle="alice", recv, created, zh=None, text="hello",
        reply=0, rt=0, quote=0, summary=None):
    c.execute(
        "INSERT INTO tweets (tweet_id, author_handle, author_name, text, text_zh,"
        " ai_summary, enriched_at, tweet_url, is_reply, is_retweet, is_quote,"
        " media_count, created_at, received_at, source)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (tid, handle, handle.title(), text, zh, summary,
         ("2026-07-30T01:00:00+00:00" if zh else None),
         f"https://x.com/{handle}/status/{tid}", reply, rt, quote, 0,
         created, recv, "webhook"))
    c.commit()


# ================= ① created_at 不可字符串排序 =================

def test_created_at_parses_twitter_format():
    dt = xm.parse_created_at("Thu Jul 30 10:23:59 +0000 2026")
    assert dt is not None
    assert (dt.year, dt.month, dt.day, dt.hour) == (2026, 7, 30, 10)
    assert dt.tzinfo is not None


def test_created_at_unparseable_returns_none_not_guess():
    """解析不了就返回 None —— 宁可显示「时间未知」，也不能猜一个假时间。"""
    for bad in ("", None, "2026-07-30", "garbage", "Xyz Jul 30 10:23:59 +0000 2026"):
        assert xm.parse_created_at(bad) is None


def test_string_sort_of_created_at_is_wrong_but_we_dont_use_it(local, src):
    """证明坑真的存在：按 created_at 字符串排会按星期几字母排。

    这个测试不是测我们的代码，是**钉死为什么不能那样做** ——
    以后有人想「顺手」改成 ORDER BY created_at 时，这里会告诉他后果。
    """
    c = sqlite3.connect(src)
    # Fri 在字母序上小于 Thu，但时间上更晚
    _tw(c, "1", recv="2026-07-30T01:00:00+00:00",
        created="Thu Jul 30 10:00:00 +0000 2026")
    _tw(c, "2", recv="2026-07-30T02:00:00+00:00",
        created="Fri Jul 31 10:00:00 +0000 2026")
    wrong = [r[0] for r in c.execute("SELECT tweet_id FROM tweets ORDER BY created_at")]
    assert wrong == ["2", "1"], "字符串排序把更晚的 Fri 排到了前面 —— 坑确实存在"

    # 我们的路径：按解析后的 UTC 排，顺序正确
    cfg, d = local
    xs.ingest(d, path=str(src), now=NOW)
    rows = d.x_tweets_since("2026-07-01", limit=10)
    assert [r["tweet_id"] for r in rows] == ["2", "1"]      # 倒序：7-31 在前
    assert rows[0]["created_at_utc"].startswith("2026-07-31")


# ================= ② 中文版兜底与如实标注 =================

def test_content_prefers_chinese_falls_back_to_original(local, src):
    c = sqlite3.connect(src)
    _tw(c, "1", recv="2026-07-30T01:00:00+00:00",
        created="Thu Jul 30 09:00:00 +0000 2026", text="English body", zh="中文正文")
    _tw(c, "2", recv="2026-07-30T02:00:00+00:00",
        created="Thu Jul 30 10:00:00 +0000 2026", text="No translation", zh=None)
    cfg, d = local
    xs.ingest(d, path=str(src), now=NOW)
    f = xs.feed(d, days=30, now=NOW)
    by = {t["tweet_id"]: t for t in f["tweets"]}
    assert by["1"]["content"] == "中文正文"
    assert by["2"]["content"] == "No translation"      # 兜底原文，不留空


def test_translation_flag_is_honest(local, src):
    """没有中文版就要标出来 —— 不能让人以为看到的都是译文。"""
    c = sqlite3.connect(src)
    _tw(c, "1", recv="2026-07-30T01:00:00+00:00",
        created="Thu Jul 30 09:00:00 +0000 2026", zh="中文")
    _tw(c, "2", recv="2026-07-30T02:00:00+00:00",
        created="Thu Jul 30 10:00:00 +0000 2026", zh=None)
    cfg, d = local
    xs.ingest(d, path=str(src), now=NOW)
    by = {t["tweet_id"]: t for t in xs.feed(d, days=30, now=NOW)["tweets"]}
    assert by["1"]["translated"] is True
    assert by["2"]["translated"] is False


# ================= ③ 自己落库：源库裁剪后数据仍在 =================

def test_local_survives_source_trimming(local, src):
    """源库把行删了，我们本地仍然有 —— 这就是自己落库的全部意义。"""
    c = sqlite3.connect(src)
    _tw(c, "1", recv="2026-07-30T01:00:00+00:00",
        created="Thu Jul 30 09:00:00 +0000 2026")
    cfg, d = local
    xs.ingest(d, path=str(src), now=NOW)
    assert d.x_tweets_stats()["n"] == 1

    c.execute("DELETE FROM tweets")          # 模拟每日裁剪
    c.commit()
    xs.ingest(d, path=str(src), now=NOW)     # 再摄取一次
    assert d.x_tweets_stats()["n"] == 1      # 本地没丢


# ================= 本次需求：今天起的推文 + 全部早报 =================

def test_initial_cursor_is_beijing_today_start():
    cur = xs.initial_cursor(NOW)             # 北京 2026-07-30 20:00
    dt = datetime.fromisoformat(cur).astimezone(BEIJING)
    assert (dt.year, dt.month, dt.day) == (2026, 7, 30)
    assert (dt.hour, dt.minute, dt.second) == (0, 0, 0)


def test_only_today_tweets_ingested_not_history(local, src):
    """**核心需求**：不回填历史推文，只要今天（北京）起的。"""
    c = sqlite3.connect(src)
    _tw(c, "old", recv="2026-07-28T05:00:00+00:00",     # 前天
        created="Tue Jul 28 05:00:00 +0000 2026")
    _tw(c, "yday", recv="2026-07-29T10:00:00+00:00",    # 昨天（北京仍是 29 日）
        created="Wed Jul 29 10:00:00 +0000 2026")
    _tw(c, "today", recv="2026-07-30T02:00:00+00:00",   # 北京 30 日 10:00
        created="Thu Jul 30 02:00:00 +0000 2026")
    cfg, d = local
    xs.ingest(d, path=str(src), now=NOW)
    got = {r["tweet_id"] for r in d.x_tweets_since("2026-01-01", limit=99)}
    assert got == {"today"}, f"只该有今天的，实际 {got}"


def test_all_daily_reports_kept_regardless_of_date(local, src):
    """早报**全要** —— 与推文的「只要今天」相反，这是刻意的不对称。"""
    c = sqlite3.connect(src)
    for day in ("2026-07-01", "2026-07-15", "2026-07-29"):
        c.execute("INSERT INTO daily_reports (day, report_json, created_at)"
                  " VALUES (?,?,?)",
                  (day, json.dumps({"day": day, "report": {"tldr": f"主线 {day}"}}),
                   f"{day}T00:00:00+00:00"))
    c.commit()
    cfg, d = local
    xs.ingest(d, path=str(src), now=NOW)
    days = [r["day"] for r in xs.reports(d)]
    assert days == ["2026-07-29", "2026-07-15", "2026-07-01"]   # 三份都在，倒序


# ================= 幂等与只读 =================

def test_ingest_is_idempotent(local, src):
    c = sqlite3.connect(src)
    _tw(c, "1", recv="2026-07-30T01:00:00+00:00",
        created="Thu Jul 30 09:00:00 +0000 2026")
    cfg, d = local
    for _ in range(3):
        # 每轮都把游标退回去，强制重复摄取同一条
        d.set_sync_state(xs.CURSOR_KEY, "2026-07-30T00:00:00+00:00")
        xs.ingest(d, path=str(src), now=NOW)
    assert d.x_tweets_stats()["n"] == 1          # tweet_id 主键，不会重复


def test_source_connection_is_read_only(src):
    """绝不能写别人的库 —— 挂载 :ro 之外再加一道 mode=ro。"""
    conn = xm._connect(str(src))
    with pytest.raises(sqlite3.OperationalError):
        conn.execute("INSERT INTO tweets (tweet_id) VALUES ('x')")
    conn.close()


def test_missing_source_is_not_an_error(local):
    """源库没挂载时静默跳过 —— 功能未启用不该让同步流程报警。"""
    cfg, d = local
    r = xs.ingest(d, path="/nonexistent/monitor.db")
    assert r["available"] is False and r["tweets"] == 0
    assert "error" not in r


def test_ingest_failure_does_not_raise(local, src, monkeypatch):
    """摄取炸了也不能往上抛 —— 它是账户同步的下游，绝不能反过来影响同步。"""
    cfg, d = local
    monkeypatch.setattr(xm, "fetch_tweets",
                        lambda **kw: (_ for _ in ()).throw(RuntimeError("boom")))
    r = xs.ingest(d, path=str(src), now=NOW)
    assert r.get("error") is True


# ================= 纯原创过滤与早报解析 =================

def test_reply_and_quote_filtered_out(local, src):
    """源库唯一例外 aleabitoreddit 会有回复/引用，我们再挡一道。"""
    c = sqlite3.connect(src)
    _tw(c, "ok", recv="2026-07-30T01:00:00+00:00",
        created="Thu Jul 30 09:00:00 +0000 2026")
    _tw(c, "reply", recv="2026-07-30T01:10:00+00:00",
        created="Thu Jul 30 09:10:00 +0000 2026", reply=1)
    _tw(c, "quote", recv="2026-07-30T01:20:00+00:00",
        created="Thu Jul 30 09:20:00 +0000 2026", quote=1)
    cfg, d = local
    xs.ingest(d, path=str(src), now=NOW)
    got = {r["tweet_id"] for r in d.x_tweets_since("2026-01-01", limit=99)}
    assert got == {"ok"}


def test_report_parse_survives_garbage():
    """坏掉的早报不该让整页打不开。"""
    assert xm.parse_report(None) == {}
    assert xm.parse_report("not json") == {}
    assert xm.parse_report('"a string"') == {}
    ok = xm.parse_report(json.dumps(
        {"day": "2026-07-29", "report": {"tldr": "主线", "top_events": [{"title": "T"}]}}))
    assert ok["tldr"] == "主线" and len(ok["top_events"]) == 1


#: 一份字段完整的早报，字段名与真实 monitor.db 一致
_FULL_REPORT = {
    "day": "2026-08-04",
    "report": {
        "tldr": "存储产能全线告急与 Palantir 财报超预期点燃 AI 热度。",
        "top_events": [
            {"title": "Palantir 财报超预期", "what_happened": "收入同比 +93%。",
             "why_it_matters": "验证 AI 落地。",
             "related_authors": ["alice", "bob"],
             "source_tweet_ids": ["111", "222", "333", "444"]},
            {"title": "霍尔木兹海峡遇袭", "what_happened": "油轮遇袭。",
             "why_it_matters": "能源风险。"},
        ],
        "topic_heat": [
            {"topic": "存储行业", "tweet_count": 7, "author_count": 4,
             "trend": "stable", "summary": "产能售罄持续验证。"},
            {"topic": "AI 应用", "tweet_count": 5, "author_count": 3,
             "trend": "rising", "summary": "落地加速。"},
        ],
        "continuous_signals": [
            {"topic": "地缘政治", "days": 5, "description": "美伊对峙持续。"}],
        "anomaly_signals": [
            {"author": "lidangzzz", "description": "昨日发推 9 条，远超日常。"}],
        "missed_items": ["某小众标的异动"],
    },
    "noise": {"casual": 28, "duplicates": 0, "low_information": 3},
}


def test_reports_expose_full_feishu_structure(local):
    """网站早报必须与源项目飞书卡片同结构：事件含相关博主/原推链接，
    主题热度含条数·人数·趋势·摘要，连续/异常/错过信号成句，噪音统计在。"""
    cfg, d = local
    d.upsert_x_daily_reports([{"day": "2026-08-04",
                               "report_json": json.dumps(_FULL_REPORT),
                               "ingested_at": NOW.isoformat()}])
    r = xs.reports(d)[0]
    assert r["date_cn"] == "8 月 4 日"
    # 事件：相关博主原样保留、原推链接截断到 3 条并构造 x.com 规范链接
    ev = r["top_events"][0]
    assert ev["authors"] == ["alice", "bob"]
    assert len(ev["sources"]) == 3
    assert ev["sources"][0]["url"] == "https://x.com/i/status/111"
    # 主题热度趋势中文化
    assert r["topic_heat"][0]["trend_label"] == "→持平"
    assert r["topic_heat"][1]["trend_label"] == "↑升温"
    # 信号成句，不露英文字段名
    assert r["continuous_signals"] == ["「地缘政治」连续 5 天：美伊对峙持续。"]
    assert r["anomaly_signals"] == ["@lidangzzz：昨日发推 9 条，远超日常。"]
    assert r["missed_items"] == ["某小众标的异动"]
    assert r["noise"]["casual"] == 28
    for k in ("topic", "description", "author", "days"):
        assert k not in " ".join(r["continuous_signals"] + r["anomaly_signals"])


def test_x_page_renders_full_report_sections(local, src):
    from fastapi.testclient import TestClient
    from app.webapp import create_app
    cfg, d = local
    d.upsert_x_daily_reports([{"day": "2026-08-04",
                               "report_json": json.dumps(_FULL_REPORT),
                               "ingested_at": NOW.isoformat()}])
    html = TestClient(create_app(cfg, d)).get("/x").text
    for marker in ("📌 今日一句", "🔥 三件最重要的事", "📊 主题热度",
                   "🔁 连续信号", "⚠️ 异常信号", "👀 你可能错过了",
                   "相关博主", "@alice", "原推1",
                   "→持平", "美伊对峙持续", "@lidangzzz", "已折叠噪音"):
        assert marker in html, f"缺少小节：{marker}"


def test_authors_list_includes_counts(local, src):
    c = sqlite3.connect(src)
    c.execute("INSERT INTO handles VALUES ('alice','1','Alice','2026-07-30T00:00:00+00:00',1)")
    c.execute("INSERT INTO handles VALUES ('bob','2','Bob','2026-07-30T00:00:00+00:00',0)")
    c.commit()
    _tw(c, "1", handle="alice", recv="2026-07-30T01:00:00+00:00",
        created="Thu Jul 30 09:00:00 +0000 2026")
    cfg, d = local
    xs.ingest(d, path=str(src), now=NOW)
    a = {x["handle"]: x for x in xs.authors(d)}
    assert a["alice"]["tweets"] == 1 and a["alice"]["active"] is True
    assert a["bob"]["tweets"] == 0 and a["bob"]["active"] is False


# ================= 页面与导航 =================

def test_x_page_renders_with_nav(local, src):
    from fastapi.testclient import TestClient
    from app.webapp import create_app
    cfg, d = local
    c = sqlite3.connect(src)
    c.execute("INSERT INTO handles VALUES ('alice','1','Alice','2026-07-30T00:00:00+00:00',1)")
    c.execute("INSERT INTO daily_reports VALUES ('2026-07-29',?,'2026-07-29T00:00:00+00:00')",
              (json.dumps({"day": "2026-07-29",
                           "report": {"tldr": "昨日主线",
                                      "top_events": [{"title": "大事一",
                                                      "what_happened": "发生了"}],
                                      "topic_heat": [{"topic": "AI", "tweet_count": 9}]}}),))
    c.commit()
    _tw(c, "1", handle="alice", recv="2026-07-30T01:00:00+00:00",
        created="Thu Jul 30 09:00:00 +0000 2026", zh="中文推文内容")
    xs.ingest(d, path=str(src), now=NOW)

    cl = TestClient(create_app(cfg, d))
    html = cl.get("/x?days=90").text
    assert 'href="/x"' in html and "X推送" in html          # 导航项在
    assert "每日早报" in html and "推文流" in html            # 两块分开的标题
    assert "<h2>关注博主</h2>" in html
    assert "昨日主线" in html and "大事一" in html            # 早报成稿
    assert "中文推文内容" in html                            # 推文正文
    assert "中文版" in html                                  # 如实标注


def test_x_page_ok_when_source_absent(local):
    """源库没挂载时页面照样能开，并给出接入指引。"""
    from fastapi.testclient import TestClient
    from app.webapp import create_app
    cfg, d = local
    cl = TestClient(create_app(cfg, d))
    r = cl.get("/x")
    assert r.status_code == 200
    assert "x_monitor:ro" in r.text                         # 给出挂载指引


def test_compose_mounts_x_monitor_for_sync_and_web():
    """sync 必须能摄取源库，web 才能显示源库可用性。"""
    compose = pathlib.Path(__file__).parents[1] / "docker-compose.yml"
    text = compose.read_text(encoding="utf-8")
    mount = "${X_MONITOR_DATA_PATH:-./data/x-monitor}:/app/x_monitor:ro"
    assert text.count(mount) >= 2


def test_x_page_filters_by_handle(local, src):
    from fastapi.testclient import TestClient
    from app.webapp import create_app
    cfg, d = local
    c = sqlite3.connect(src)
    _tw(c, "1", handle="alice", recv="2026-07-30T01:00:00+00:00",
        created="Thu Jul 30 09:00:00 +0000 2026", text="ALICE_POST")
    _tw(c, "2", handle="bob", recv="2026-07-30T02:00:00+00:00",
        created="Thu Jul 30 10:00:00 +0000 2026", text="BOB_POST")
    xs.ingest(d, path=str(src), now=NOW)
    cl = TestClient(create_app(cfg, d))
    html = cl.get("/x?handle=alice&days=90").text
    assert "ALICE_POST" in html and "BOB_POST" not in html


def test_all_reports_reachable_through_stable_fragments(local, src):
    """首屏只渲染 7 份，但全部早报都能经普通/HTMX 分片继续访问。"""
    from fastapi.testclient import TestClient
    from app.webapp import create_app
    cfg, d = local
    c = sqlite3.connect(src)
    days = [f"2026-06-{i:02d}" for i in range(1, 29)]      # 28 份
    for day in days:
        c.execute("INSERT INTO daily_reports VALUES (?,?,?)",
                  (day, json.dumps({"day": day,
                                    "report": {"tldr": f"主线{day[-2:]}"}}),
                   f"{day}T00:00:00+00:00"))
    c.commit()
    xs.ingest(d, path=str(src), now=NOW)
    client = TestClient(create_app(cfg, d))
    html = client.get("/x").text
    assert html.count('<details class="rpt"') == 7
    assert "加载更多早报" in html

    rendered = html
    desc = sorted(days, reverse=True)
    for offset in range(7, len(desc), 7):
        rendered += client.get(
            "/x/reports-fragment",
            params={"before_day": desc[offset - 1], "loaded": offset},
        ).text
    for day in days:
        assert day in rendered, f"早报 {day} 无法通过分片访问"
    assert "7 份" in html


def test_three_column_layout_present(local):
    """左=早报 / 中=推文 / 右=博主 的三栏骨架。"""
    from fastapi.testclient import TestClient
    from app.webapp import create_app
    cfg, d = local
    html = TestClient(create_app(cfg, d)).get("/x").text
    assert ".x3{display:grid" in html
    assert "colL" in html and "colR" in html
    # 栏宽比例与弹性由 test_x_grid_is_flexible_and_fills_container 覆盖
    # 顺序必须是 早报 → 推文流 → 关注博主。
    # 用 <h2> 定位而非纯文字 —— 顶部 CSS 注释里也有这几个词，会误命中
    a = html.index("<h2>每日早报</h2>")
    b = html.index("<h2>推文流</h2>")
    c = html.index("<h2>关注博主</h2>")
    assert a < b < c, f"栏位顺序错误: 早报={a} 推文={b} 博主={c}"
    # 窄屏要塌成单栏，否则手机上没法看
    assert "max-width:1060px" in html


def test_x_grid_is_flexible_and_fills_container():
    """三栏必须是**弹性**的（minmax），不能写定宽。

    定宽栅格会产生「断点之上却放不下」的缝隙 —— 先前 340|760|232 定宽
    配 1380 断点，视口 1381–1411px 横向溢出过 31px。
    弹性栏各自收缩，从上限档一路到最窄断点都不会溢出。

    另外校验上限档正好铺满容器：不铺满就会像定宽居中那样两侧留一大片空白。
    """
    html = pathlib.Path("app/templates/x_feed.html").read_text(encoding="utf-8")
    base = pathlib.Path("app/templates/base.html").read_text(encoding="utf-8")

    m = re.search(r"grid-template-columns:minmax\((\d+)px,(\d+)px\)"
                  r" minmax\(0,(\d+)px\) minmax\((\d+)px,(\d+)px\)", html)
    assert m, "三栏应为 minmax 弹性写法，不得写死像素"
    lo_l, hi_l, hi_m, lo_r, hi_r = (int(x) for x in m.groups())
    gap = int(re.search(r"gap:(\d+)px;align-items:start", html).group(1))
    gutter = int(re.search(r"--page-gutter:(\d+)px", base).group(1))
    page_max = int(re.search(r"--page-max:(\d+)px", base).group(1))

    # 上限档正好铺满容器可用宽度
    avail = page_max - 2 * gutter
    assert hi_l + hi_m + hi_r + 2 * gap == avail, (
        f"上限档 {hi_l}+{hi_m}+{hi_r}+2×{gap} = {hi_l+hi_m+hi_r+2*gap}"
        f" != 容器可用 {avail}，会留下空白或溢出")

    # 主次不能倒：博主栏窄于早报栏
    assert hi_r < hi_l, f"右栏 {hi_r} 不该宽于左栏 {hi_l}"
    # 中栏是主内容，宽度上限放宽到 1050（约 72 字/行）—— 用户明确要求推文流
    # 成为最宽栏。下界仍守 700，防止某次调整把主内容挤窄。
    assert 700 <= hi_m <= 1050, f"中栏 {hi_m}px 不在可接受阅读区间"
    # 最窄档远低于最小断点，不可能溢出
    narrow = int(re.search(r"@media \(max-width:(\d+)px\)\{\.x3\{"
                           r"grid-template-columns:minmax\(0,1fr\)", html).group(1))
    assert lo_l + lo_r + 2 * gap < narrow, "最窄档可能溢出"


def test_all_pages_share_one_container_width():
    """所有页面共用同一个容器宽度 —— 不再有 main.wide 特例。

    X 页三栏是定宽自居中的，容器给 1440 还是 1760，视觉结果**完全相同**
    （空白只是从 main 外面搬到里面）。那个特例不产生差异，是白留的复杂度。
    """
    base = pathlib.Path("app/templates/base.html").read_text(encoding="utf-8")
    assert "main.wide" not in base, "main.wide 特例应已删除"
    assert 'class="wide"' not in base


def test_page_gutter_is_global_and_shared(local):
    """左右留白是**全局变量**，正文与顶栏共用一个值 —— 否则边缘对不齐。"""
    from fastapi.testclient import TestClient
    from app.webapp import create_app
    cfg, d = local
    html = TestClient(create_app(cfg, d)).get("/").text
    m = re.search(r"--page-gutter:(\d+)px", html)
    assert m and 16 <= int(m.group(1)) <= 64, f"全局留白不合理: {m and m.group(1)}"
    # 正文与顶栏都引用同一个变量
    assert "padding:20px var(--page-gutter) 8px" in html
    assert "padding:12px var(--page-gutter)" in html
    # 容器放开到 --page-max，不再被 1120px 卡住
    assert "max-width:var(--page-max)" in html
    mm = re.search(r"--page-max:(\d+)px", html)
    assert mm and int(mm.group(1)) >= 1500, "全局最大宽度太小，宽屏仍会大量留白"


def test_x_header_stat_line_removed(local, src):
    """顶部那行「本地已存 N 条推文 · M 位博主 · K 份早报」已去掉。

    它与三栏各自的标题计数重复（早报 30 份 / 推文 105 条 / 博主 19 位
    都已在各栏 colhd 里），留着只是噪音。
    """
    from fastapi.testclient import TestClient
    from app.webapp import create_app
    cfg, d = local
    c = sqlite3.connect(src)
    _tw(c, "1", recv="2026-07-30T01:00:00+00:00",
        created="Thu Jul 30 09:00:00 +0000 2026")
    xs.ingest(d, path=str(src), now=NOW)
    html = TestClient(create_app(cfg, d)).get("/x").text
    assert "本地已存" not in html
    # 但三栏各自的计数仍在
    assert "<h2>每日早报</h2>" in html and "<h2>推文流</h2>" in html


def test_source_unavailable_warning_survives_header_removal(local, src, monkeypatch):
    """删掉顶部统计行后，「源库掉了但本地有存档」这个信号不能一起丢。"""
    from fastapi.testclient import TestClient
    from app.webapp import create_app
    cfg, d = local
    c = sqlite3.connect(src)
    _tw(c, "1", recv="2026-07-30T01:00:00+00:00",
        created="Thu Jul 30 09:00:00 +0000 2026")
    xs.ingest(d, path=str(src), now=NOW)          # 本地已有数据
    monkeypatch.setattr(xm, "available", lambda *a, **k: False)   # 源库掉了
    html = TestClient(create_app(cfg, d)).get("/x").text
    assert "源库未挂载" in html and "不再更新" in html
