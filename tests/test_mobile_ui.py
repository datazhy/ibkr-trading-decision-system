"""七个核心页面的移动端壳层、分页与渐进加载验收。"""
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from html import unescape

import pytest
from fastapi.testclient import TestClient

from app import webflex, x_service
from app.db import Database
from app.webapp import create_app


@pytest.fixture
def mobile_env(make_config):
    cfg = make_config(ib_account="U-MOBILE")
    db = Database(cfg.db_path)
    db.init_schema()
    return cfg, db


def test_core_pages_share_mobile_header_bottom_nav_and_more_sheet(mobile_env):
    cfg, db = mobile_env
    client = TestClient(create_app(cfg, db))
    for path in ("/", "/history", "/journal", "/x", "/briefs",
                 "/cashflow", "/events"):
        response = client.get(path)
        assert response.status_code == 200, path
        html = response.text
        assert 'class="mobile-header"' in html, path
        assert 'class="mobile-bottom-nav"' in html, path
        assert 'id="mobile-menu"' in html, path
        for label in ("概览", "成交", "风控", "更多", "日记", "X推送", "简报", "收益"):
            assert label in html, (path, label)
        bottom = html.split('<nav class="mobile-bottom-nav"', 1)[1].split("</nav>", 1)[0]
        if path == "/":
            assert '<a href="/" class="active">' in bottom
        elif path == "/history":
            assert '<a href="/history" class="active">' in bottom
        elif path == "/events":
            assert '<a href="/events" class="active">' in bottom
        else:
            assert '<button type="button" data-mobile-menu-open class="active">' in bottom
            target = {"/journal": "journal", "/x": "x", "/briefs": "briefs",
                      "/cashflow": "cashflow"}[path]
            assert f'href="/{target}" class="active"' in html


def test_history_paginates_50_and_keeps_normal_links(mobile_env):
    cfg, db = mobile_env
    now = datetime.now(timezone.utc)
    rows = []
    for i in range(101):
        rows.append({
            "account": "U-MOBILE", "exec_id": f"mobile-{i:03d}",
            "correction_root": f"mobile-{i:03d}", "correction_seq": 0,
            "symbol": "QQQ", "side": "BUY", "shares": Decimal("1"),
            "price": Decimal("500"), "currency": "USD",
            "execution_time": (now - timedelta(minutes=i)).isoformat(),
            "commission": Decimal("0.25"), "commission_currency": "USD",
            "data_status": "READY", "raw_json": "{}",
        })
    db.upsert_executions(rows)
    client = TestClient(create_app(cfg, db))

    first = client.get("/history?symbol=QQQ").text
    second = client.get("/history?symbol=QQQ&page=2").text
    last = client.get("/history?symbol=QQQ&page=3").text
    assert first.count('<details class="mobile-data-card">') == 50
    assert second.count('<details class="mobile-data-card">') == 50
    assert last.count('<details class="mobile-data-card">') == 1
    assert "共 101 笔成交" in first
    assert 'href="/history?symbol=QQQ&page=2"' in unescape(first)
    assert 'href="/history?symbol=QQQ&page=1"' in unescape(second)
    for field in ("数量", "成交价", "成交金额", "佣金", "已实现盈亏", "来源"):
        assert field in first


def _tweet(i: int, published: datetime) -> dict:
    stamp = published.isoformat()
    return {
        "tweet_id": f"tweet-{i:03d}", "author_handle": "mobile",
        "author_name": "Mobile", "text": f"post {i}",
        "created_at_utc": stamp, "received_at": stamp,
        "ingested_at": stamp, "source": "test",
    }


def test_x_cursor_survives_newer_insert_without_duplicates_or_gaps(mobile_env):
    _cfg, db = mobile_env
    now = datetime.now(timezone.utc).replace(microsecond=0)
    original = [_tweet(i, now - timedelta(minutes=i)) for i in range(61)]
    db.upsert_x_tweets(original)

    first = x_service.feed(db, days=7, limit=30, now=now)
    db.upsert_x_tweets([_tweet(999, now + timedelta(seconds=1))])
    second = x_service.feed(
        db, days=7, limit=30, now=now,
        before=first["next_before"], before_id=first["next_before_id"])
    third = x_service.feed(
        db, days=7, limit=30, now=now,
        before=second["next_before"], before_id=second["next_before_id"])

    loaded = [t["tweet_id"] for batch in (first, second, third)
              for t in batch["tweets"]]
    assert len(loaded) == len(set(loaded)) == 61
    assert set(loaded) == {r["tweet_id"] for r in original}
    assert "tweet-999" not in loaded


def test_x_initial_payload_is_bounded_and_fragments_have_fallback_links(mobile_env):
    cfg, db = mobile_env
    now = datetime.now(timezone.utc).replace(microsecond=0)
    db.upsert_x_tweets([_tweet(i, now - timedelta(minutes=i)) for i in range(35)])
    db.upsert_x_daily_reports([{
        "day": (now.date() - timedelta(days=i)).isoformat(),
        "report_json": '{"report":{"tldr":"mobile report"}}',
        "created_at": now.isoformat(), "ingested_at": now.isoformat(),
    } for i in range(15)])
    client = TestClient(create_app(cfg, db))

    page = client.get("/x").text
    assert page.count('<article class="tw">') == 30
    assert page.count('<details class="rpt"') == 7
    assert "加载更多推文" in page and "加载更多早报" in page
    assert 'href="/x?days=7&feed_limit=60' in page
    assert 'href="/x?report_limit=14"' in page

    report_more = client.get(
        "/x/reports-fragment",
        params={"before_day": (now.date() - timedelta(days=6)).isoformat(),
                "loaded": 7},
    ).text
    assert report_more.count('<details class="rpt"') == 7


def _balance(i: int, report_from: str, report_to: str, now: datetime) -> dict:
    return {
        "balance_id": f"balance-{report_to}-{i}", "account_id": "U-MOBILE",
        "report_from": report_from, "report_to": report_to,
        "balance_date": report_to, "balance_type": "closing",
        "currency": "USD", "balance": str(1000 + i),
        "source": "TEST", "quality_status": "ok",
        "created_at": now.isoformat(), "updated_at": now.isoformat(),
    }


def test_cashflow_only_renders_latest_period_plus_20_history_rows(mobile_env):
    cfg, db = mobile_env
    now = datetime.now(timezone.utc).replace(microsecond=0)
    history = []
    for i in range(25):
        day = (now.date() - timedelta(days=i + 10)).isoformat()
        history.append(_balance(i, day, day, now))
    latest = [_balance(100 + i, "2026-08-01", "2099-12-31", now)
              for i in range(2)]
    db.upsert_statement_balances(history + latest)

    data = webflex.page_data(cfg, db, now, anchor_page=1)
    assert len(data["balance_anchors"]) == 2
    assert len(data["anchor_history"]) == 20
    assert data["anchor_total"] == 25
    assert data["anchor_pages"] == 2
    second_data = webflex.page_data(cfg, db, now, anchor_page=2)
    assert len(second_data["anchor_history"]) == 5

    client = TestClient(create_app(cfg, db))
    page = client.get("/cashflow").text
    assert "历史余额锚点 · 共 25 条" in page
    assert "anchor_page=2" in page
    page_2 = client.get("/cashflow?anchor_page=2").text
    assert "第 2 / 2 页" in page_2
