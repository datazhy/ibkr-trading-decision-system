"""页面导航、配色语义、曲线坐标轴。"""
from datetime import datetime, timezone
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from app import webdata
from app.db import Database
from app.webapp import create_app
# tests/ 不是包（conftest 已把根目录加进 sys.path），直接按模块名导入
from test_web import NOW, seeded  # noqa: F401 — 复用 fixture


@pytest.fixture
def client(seeded):  # noqa: F811
    cfg, db = seeded
    db.backfill_daily_equity("ACC", cfg.market_tz)
    return TestClient(create_app(cfg, db))


# ---------------- 导航与新页面 ----------------

def test_new_pages_render(client):
    for path in ("/", "/history", "/rules", "/reference", "/status"):
        r = client.get(path)
        assert r.status_code == 200, path


def test_brand_links_home(client):
    """品牌名称统一，点击标题能回首页。"""
    html = client.get("/rules").text
    assert '<a class="brand" href="/"' in html
    assert "小鱼交易台" in html
    assert "规则日志 · 小鱼交易台" in html


def test_nav_has_all_pages(client):
    html = client.get("/").text
    # 主导航在 3d13b76 重排：交易历史→成交，触发记录/口径说明下沉到二级导航
    # （不在首页）。首页主导航与页脚覆盖以下关键页。
    for label in ("概览", "成交", "日记", "简报", "收益", "风控", "系统状态"):
        assert label in html


def test_status_bar_moved_off_overview(client):
    """状态栏不再挂在首页，改由「系统状态」页承载。"""
    home = client.get("/").text
    assert 'class="statusbar"' not in home
    assert "系统健康" not in home          # 详情层也移走了
    status = client.get("/status").text
    assert "能力状态" in status and "系统健康" in status
    assert "Watchdog" in status and "调度器" in status


def test_nav_health_dot_present_on_every_page(client):
    """健康圆点跟着导航走，每一页都能看到降级。"""
    for path in ("/", "/history", "/rules", "/reference", "/status"):
        assert 'class="health' in client.get(path).text


def test_reference_page_has_pnl_bases_and_rules(client):
    html = client.get("/reference").text
    assert "三种盈亏口径" in html
    assert "不能相加" in html
    for rid in ("R1", "R2", "R3", "R4", "R5", "R6", "R7"):
        assert f">{rid}</span>" in html
    assert "买入佣金已经计入持仓均价" in html   # 最容易算错的口径要讲清


def test_overview_no_longer_embeds_pnl_bases(client):
    """三口径从首页移走，只留一个入口链接。"""
    html = client.get("/").text
    assert "统计范围" not in html
    assert 'href="/reference"' in html


def test_position_quote_source_is_stable_and_latest_quote_row_is_removed(client):
    """持仓行情来源不显示持续变化的相对秒数，顶部也不再显示最近行情。"""
    html = client.get("/").text
    assert "最近行情" not in html
    assert 'id="live-latest-quote"' not in html
    assert 'const metaText = source + " · " + quality;' in html
    assert "if (meta.textContent !== metaText) meta.textContent = metaText;" in html
    assert 'quality + " · " + age(p.quote_as_of)' not in html
    assert "富途行情" in html


def test_positions_replace_risk_budget_with_ibkr_realized_pnl(client):
    """当前持仓显示部分平仓的已实现盈亏，不再占一列重复展示风险预算。"""
    html = client.get("/").text
    start = html.index('<table class="postbl">')
    table = html[start:html.index("</table>", start)]
    assert "<th>已实现盈亏</th>" in table
    assert "<th>风险预算</th>" not in table
    assert 'data-live-position="realized_pnl"' in table
    assert 'set(row, "realized_pnl", p.realized_pnl, signed)' in html


def test_position_pnl_columns_are_stable_two_line_blocks(client):
    """日涨跌按价格涨跌幅/每股涨跌额展示，浮动盈亏按百分比/金额展示。"""
    html = client.get("/").text
    start = html.index('<table class="postbl">')
    table = html[start:html.index("</table>", start)]
    assert "<th>日涨跌</th>" in table
    assert table.index("<th>日涨跌</th>") < table.index("<th>现价</th>")
    assert table.index('class="pos-col-change"') < table.index('class="pos-col-price"')
    assert 'data-live-position="price" class="' in table
    assert "<th>今日盈亏</th>" not in table
    assert "<th>历史盈亏率</th>" not in table
    assert 'data-live-position="day_change"' in table
    assert 'data-live-position="day_pnl"' not in table
    assert 'data-live-position="upl_pct"' in table
    assert 'data-live-position="upl"' in table
    assert table.count('class="pnl-stack"') >= 2
    assert ".posfilter .postbl{min-width:1248px;table-layout:fixed}" in html
    assert ".posfilter .postbl .pnl-stack{min-height:38px;display:flex;flex-direction:column;" in html
    assert 'set(row, "upl_pct", uplPctValue, percent)' in html
    assert 'set(row, "day_change", dayChangeValue, signed)' in html
    assert 'color(price, dayChangeValue)' in html
    assert 'p.daily_change != null ? Number(p.daily_change) : NaN' in html
    assert 'p.daily_change_percent != null ? Number(p.daily_change_percent) : NaN' in html
    assert '(n >= 0 ? "+" : "") + money2(n)' in html


def test_rule_reference_thresholds_come_from_engine(make_config):
    """阈值从 RuleThresholds 读出，不在模板写死 —— 改阈值页面必须跟着变。"""
    from app.rules import RuleThresholds
    cfg = make_config()
    rules = webdata.rule_reference(cfg, RuleThresholds(single_concentration=Decimal("0.25")))
    r1 = next(r for r in rules if r["id"] == "R1")
    assert "25%" in r1["threshold"]


# ---------------- 红涨绿跌 + 状态色解耦 ----------------

def test_profit_is_red_loss_is_green(client):
    """A 股习惯：红涨绿跌。两套调色板各自的取色都要对。"""
    css = client.get("/").text
    assert "--d-profit:#e5534b" in css      # 暗色：盈利=红
    assert "--d-loss:#3fb27f" in css        # 暗色：亏损=绿
    assert "--l-profit:#c0392b" in css      # 亮色：盈利=红（加深，白底才压得住）
    assert "--l-loss:#1a8f5c" in css        # 亮色：亏损=绿
    assert ".up{color:var(--profit)}" in css
    assert ".down{color:var(--loss)}" in css


def test_status_colors_decoupled_from_pnl_colors(client):
    """状态色必须与涨跌色分开：否则盈利变红会让告警也变红、亏损变绿会让正常也变绿。"""
    css = client.get("/").text
    assert ".flag{color:var(--crit)" in css     # 告警用 crit，不是 down
    assert ".ok{color:var(--good)" in css       # 正常用 good，不是 up
    assert "--good:" in css and "--crit:" in css


def test_light_mode_keeps_red_up_green_down(client):
    """红涨绿跌在**三条**亮色激活路径上都必须成立 ——
    跟随系统、手动选亮色，以及手动选暗色时不能被亮色污染。

    只查一处会漏：改主题最容易漏掉的正是「手动选择」那条分支。
    """
    css = client.get("/").text

    def block(anchor):
        seg = css[css.index(anchor):]
        return seg[:seg.index("}")]

    auto = block(":root:not([data-theme]){")
    manual = block(':root[data-theme="light"]{')
    for name, seg in (("跟随系统", auto), ("手动亮色", manual)):
        assert "--profit:var(--l-profit)" in seg, name
        assert "--loss:var(--l-loss)" in seg, name

    dark = block(':root[data-theme="dark"]{')
    assert "--profit:var(--d-profit)" in dark
    assert "--l-" not in dark               # 暗色分支不得引用亮色变量


# ---------------- 曲线日期轴 ----------------

def _svg(n=6):
    from datetime import date, timedelta
    base = date(2026, 5, 1)
    pts = [((base + timedelta(days=i)).isoformat(), Decimal(1000 + i * 100))
           for i in range(n)]
    return webdata.render_equity_svg(pts)


def test_curve_has_date_ticks_not_just_last_date():
    """曲线下方要有日期刻度，不能只在角落写一个最新日期。"""
    import re
    svg = _svg(6)
    ticks = re.findall(r'text-anchor="(?:start|middle|end)">(\d\d-\d\d)<', svg)
    assert len(ticks) >= 3                  # 至少首、中、尾
    assert "05-01" in ticks                 # 首点


def test_curve_ticks_capped_at_five():
    import re
    svg = _svg(60)
    ticks = re.findall(r'text-anchor="(?:start|middle|end)">(\d\d-\d\d)<', svg)
    assert len(ticks) <= 5                  # 不能挤成一团


def test_curve_shows_year_once():
    assert "2026" in _svg(6)


def test_curve_rising_is_red():
    """上涨用盈利色（红）。"""
    assert "var(--profit)" in _svg(6)


def test_curve_falling_is_green():
    pts = [("2026-07-10", Decimal("2000")), ("2026-07-11", Decimal("1000"))]
    assert "var(--loss)" in webdata.render_equity_svg(pts)


def test_curve_has_axis_line():
    assert "chart-axis" in _svg(6)


def test_curve_too_few_points_says_so():
    assert "数据不足" in webdata.render_equity_svg([("2026-07-10", Decimal("1"))])


def test_capability_state_text_not_shrunk_by_status_classes(client):
    """.ok/.flag 带 font-size:11px —— 能力状态用它们会把 20px 的数字压成小字。"""
    html = client.get("/status").text
    import re
    block = html[html.index("能力状态"):html.index("数据新鲜度")]
    for m in re.finditer(r'class="v sm([^"]*)"', block):
        assert "ok" not in m.group(1) and "flag" not in m.group(1)
    # 颜色由 style 承担（具体是哪个色取决于当时状态），而不是靠会改字号的类
    assert 'style="color:var(--' in block


# ---------------- 曲线：三系列 / 标记 / 断线 / 悬停 ----------------

def _curve_pts():
    return [("2026-07-10", Decimal("378.83")), ("2026-07-13", Decimal("378.83")),
            ("2026-07-14", Decimal("47069.92")), ("2026-07-15", Decimal("43500.49")),
            ("2026-07-16", Decimal("40786.57"))]


def test_curve_breaks_line_at_cash_flow_jump():
    """入金前后不连线：378 → 47069 是转账，一条上扬的线会被读成暴涨。"""
    svg = webdata.render_series_svg(
        _curve_pts(), marks=[{"date": "2026-07-14", "amount": Decimal("46691"), "note": "入金"}])
    assert svg.count("<polyline") == 2      # 入金处断成两段


def test_curve_breaks_unregistered_jump_too():
    """资金流未登记时也要断线并标「跳变」。

    「不推断资金流金额」与「不把跳变画成连续走势」是两件事：前者是数据纪律，
    后者是展示诚实。378 → 47069 无论登记与否，都不该用一条上扬的线连起来。
    """
    svg = webdata.render_series_svg(_curve_pts(), marks=[])
    assert svg.count("<polyline") == 2          # 仍然断开
    assert "净资产跳变" in svg                   # 标出来，但不猜金额
    assert "入金" not in svg                     # 绝不替用户断言那是入金


def test_curve_does_not_break_normal_moves():
    """正常波动不断线。"""
    pts = [("2026-07-13", Decimal("40000")), ("2026-07-14", Decimal("41000")),
           ("2026-07-15", Decimal("39500"))]
    assert webdata.render_series_svg(pts).count("<polyline") == 1


def test_curve_marks_trades_and_alerts():
    svg = webdata.render_series_svg(
        _curve_pts(), trades=[{"date": "2026-07-15", "n": 2}],
        alerts=[{"date": "2026-07-16", "rules": "R1·R2"}])
    assert "<polygon" in svg and "成交 2 笔" in svg      # 成交三角
    assert "触发 R1·R2" in svg                           # R1/R2 方块


def test_curve_shades_data_gaps():
    """Gateway 挂掉那几天要画成阴影区。"""
    pts = [("2026-07-13", Decimal("1000")), ("2026-07-17", Decimal("1100"))]  # 周一->周五
    gaps = webdata.find_gaps(pts)
    assert gaps == [("2026-07-13", "2026-07-17")]       # 跳过 3 个工作日 -> 中断
    svg = webdata.render_series_svg(pts, gaps=gaps)
    assert "数据中断" in svg and "<rect" in svg


def test_find_gaps_counts_weekdays_not_calendar_days():
    """周五 07-10 到周一 07-13 自然日差 3 天，但中间没有交易日 —— 不是中断。"""
    assert webdata.find_gaps([("2026-07-10", Decimal("1")),
                              ("2026-07-13", Decimal("2"))]) == []


def test_curve_hover_shows_amount_time_quality():
    svg = webdata.render_series_svg(
        _curve_pts(), quality_by_date={"2026-07-16": "DEGRADED"})
    assert "<title>" in svg
    assert "数据质量：DEGRADED" in svg
    assert "2026-07-16" in svg


def test_find_gaps_ignores_normal_weekend():
    pts = [("2026-07-17", Decimal("1")), ("2026-07-20", Decimal("2"))]   # 周五->周一
    assert webdata.find_gaps(pts) == []                  # 3 天 = 周末，不算中断


def test_overview_does_not_render_asset_curve(client):
    html = client.get("/").text
    assert "资产曲线" not in html
    assert "curve-ctrls" not in html
    assert "tab-net" not in html
    assert "tab-change" not in html
    assert "rangehint" not in html


def test_daily_pnl_series_is_day_over_day(make_config):
    """每日盈亏 = 当日累计 − 前一日。看的是「今天赚了多少」，不是「至今赚了多少」。"""
    from decimal import Decimal as D
    cfg = make_config(ib_account="ACC")
    db = Database(cfg.db_path)
    db.init_schema()
    days = [("2026-07-06", "10000"), ("2026-07-07", "10150"),
            ("2026-07-08", "10050"), ("2026-07-09", "10400")]
    for d, v in days:
        db.upsert_daily_equity("ACC", d, net_liquidation=D(v),
                               as_of=f"{d}T21:00:00+00:00", source="TEST",
                               quality="READY")
    # 券商流水覆盖该区间且无出入金 -> 允许算盈亏
    db.set_cash_flow_coverage(account="ACC", start_date="2026-07-01",
                              end_date="2026-07-31", status="COMPLETE",
                              source="FLEX_QUERY")
    cv = webdata.equity_curve_data(cfg, db, "ALL", NOW)
    assert cv["coverage_ok"] is True
    # 首日无前值 -> 不产生数据点（不画一根假的零柱）
    assert [d for d, _ in cv["daily_pnl"]] == ["2026-07-07", "2026-07-08",
                                               "2026-07-09"]
    assert [v for _, v in cv["daily_pnl"]] == [D("150"), D("-100"), D("350")]
    # 累计盈亏仍是相对起点
    assert cv["adjusted"][-1][1] == D("400")
    # 覆盖完整 -> 默认看盈亏而非净资产
    assert cv["default_series"] == "adjusted"
    assert cv["svg_daily_pnl"] and "chart-zero" in cv["svg_daily_pnl"]


def test_daily_pnl_locked_without_coverage(make_config):
    """没有覆盖证明就不给看盈亏 —— 那会把入金画成收益。"""
    from decimal import Decimal as D
    cfg = make_config(ib_account="ACC")
    db = Database(cfg.db_path)
    db.init_schema()
    for d, v in (("2026-07-06", "10000"), ("2026-07-07", "20000")):
        db.upsert_daily_equity("ACC", d, net_liquidation=D(v),
                               as_of=f"{d}T21:00:00+00:00", source="TEST",
                               quality="READY")
    cv = webdata.equity_curve_data(cfg, db, "ALL", NOW)
    assert cv["coverage_ok"] is False
    assert cv["svg_daily_pnl"] is None
    assert cv["svg_adjusted"] is None
    assert cv["default_series"] == "net"


def test_curve_ignores_reset_reqpnl_and_uses_official_nav_change(make_config):
    """历史总收益不能被盘后已重置的 reqPnL +194.12 覆盖。"""
    cfg = make_config(ib_account="ACC")
    db = Database(cfg.db_path); db.init_schema()
    for d, v in (("2026-07-29", "40000"), ("2026-07-30", "49000")):
        db.upsert_daily_equity("ACC", d, net_liquidation=Decimal(v),
                               as_of=f"{d}T23:59:00+00:00", source="TEST",
                               quality="READY")
    db.insert_pnl_snapshot({
        "account": "ACC", "as_of": "2026-07-31T07:59:00+00:00",
        "et_date": "2026-07-30", "daily_pnl": Decimal("194.12"),
        "realized_pnl": Decimal("0"), "unrealized_pnl": Decimal("194.12"),
        "net_liquidation": Decimal("49000"), "source": "IBKR_reqPnL",
        "quality": "OK", "reset_policy": "IBKR_SESSION_DAILY",
    })
    now = datetime(2026, 7, 31, 12, 0, tzinfo=timezone.utc)
    cv = webdata.equity_curve_data(cfg, db, "ALL", now)
    assert cv["daily_pnl"] == [("2026-07-30", Decimal("9000"))]
    assert cv["adjusted"][-1][1] == Decimal("9000")
    assert cv["pnl_series_source"] == "IBKR_NAV_MINUS_EXTERNAL_FLOW"
    assert cv["marks"] == []


def test_curve_prefers_flex_change_in_nav_over_tws_nav_delta(make_config):
    cfg = make_config(ib_account="ACC")
    db = Database(cfg.db_path); db.init_schema()
    for d, v in (("2026-07-29", "39646.49"), ("2026-07-30", "48464.37")):
        db.upsert_daily_equity("ACC", d, net_liquidation=Decimal(v),
                               as_of=f"{d}T23:59:00+00:00", source="IBKR_TWS",
                               quality="READY")
    now_iso = "2026-07-31T12:00:00+00:00"
    for d, start, end, pnl, twr in (
        ("2026-07-29", "43669.653047", "39592.593047", "-4077.06", "-9.336"),
        ("2026-07-30", "39592.593047", "48238.713589", "8646.120542", "21.837722"),
    ):
        db.upsert_flex_nav_daily({
            "account_id": "ACC", "et_date": d, "currency": "USD",
            "starting_value": start, "ending_value": end, "total_pnl": pnl,
            "mtm": pnl, "commissions": "0", "capital_movements": "0",
            "deposits_withdrawals": "0", "internal_cash_transfers": "0",
            "asset_transfers": "0", "dividends": "0", "interest": "0",
            "fees": "0", "other": "0", "twr": twr, "raw_json": "{}",
            "source": "FLEX_CHANGE_IN_NAV", "report_hash": "test",
            "created_at": now_iso, "updated_at": now_iso,
        })
    now = datetime(2026, 7, 31, 12, 0, tzinfo=timezone.utc)
    cv = webdata.equity_curve_data(cfg, db, "ALL", now)
    assert cv["daily_pnl"] == [
        ("2026-07-29", Decimal("-4077.06")),
        ("2026-07-30", Decimal("8646.120542")),
    ]
    assert cv["pnl_series_source"] == "FLEX_CHANGE_IN_NAV"
    assert cv["show_return"] is True

    cal = webdata.calendar_data(cfg, db, 2026, 7, now)
    cell = next(c for week in cal["weeks"] for c in week
                if c and c["et_date"] == "2026-07-30")
    assert cell["amount"] == Decimal("8646.120542")


def test_curve_axis_includes_official_days_missing_from_local_snapshots(make_config):
    """Flex 有日结、TWS 本地日快照缺失时，资金曲线不得漏日。"""
    cfg = make_config(ib_account="ACC")
    db = Database(cfg.db_path); db.init_schema()
    db.upsert_daily_equity("ACC", "2026-07-31", net_liquidation=Decimal("49000"),
                           as_of="2026-07-31T12:00:00+00:00", source="IBKR_TWS",
                           quality="READY")
    now_iso = "2026-07-31T12:00:00+00:00"
    for d, start, end, pnl, capital, asset, twr in (
        ("2026-07-14", "373.68", "46732.15", "3381.81", "42976.66",
         "42976.66", "7.801113698"),
        ("2026-07-15", "46732.15", "43798.47", "-2933.68", "0", "0",
         "-6.277647072"),
    ):
        db.upsert_flex_nav_daily({
            "account_id": "ACC", "et_date": d, "currency": "USD",
            "starting_value": start, "ending_value": end, "total_pnl": pnl,
            "mtm": pnl, "commissions": "0", "capital_movements": capital,
            "deposits_withdrawals": "0", "internal_cash_transfers": "0",
            "asset_transfers": asset, "dividends": "0", "interest": "0",
            "fees": "0", "other": "0", "twr": twr, "raw_json": "{}",
            "source": "FLEX_CHANGE_IN_NAV", "report_hash": "test",
            "created_at": now_iso, "updated_at": now_iso,
        })
    now = datetime(2026, 7, 31, 12, 0, tzinfo=timezone.utc)
    cv = webdata.equity_curve_data(cfg, db, "ALL", now)
    assert [d for d, _ in cv["points"]] == [
        "2026-07-14", "2026-07-15", "2026-07-31"]
    assert cv["daily_pnl"][:2] == [
        ("2026-07-14", Decimal("3381.81")),
        ("2026-07-15", Decimal("-2933.68")),
    ]
    assert cv["marks"][0] == {
        "date": "2026-07-14", "amount": Decimal("42976.66"),
        "note": "IBKR 官方资产转移",
    }

    cal = webdata.calendar_data(cfg, db, 2026, 7, now)
    cell = next(c for week in cal["weeks"] for c in week
                if c and c["et_date"] == "2026-07-14")
    assert cell["flow_label"] == "资产转移"


def test_pnl_cards_stop_at_session_date_and_chain_official_twr(make_config):
    """美东开盘前不混入当日快照；有资产转移时用官方 TWR 链接。"""
    cfg = make_config(ib_account="ACC")
    db = Database(cfg.db_path); db.init_schema()
    for d, value in (("2026-07-14", "46732.15"), ("2026-07-15", "43798.47"),
                     # Flex 覆盖区间内的周末快照噪声不得计收益。
                     ("2026-07-25", "40000"), ("2026-07-26", "40005.56"),
                     ("2026-07-30", "48238.71"), ("2026-07-31", "40000")):
        db.upsert_daily_equity("ACC", d, net_liquidation=Decimal(value),
                               as_of=f"{d}T12:00:00+00:00", source="IBKR_TWS",
                               quality="READY")
    db.insert_balances_snapshot("2026-07-31T12:00:00+00:00", {
        "equity": Decimal("49000"), "cash": Decimal("0"),
        "margin_balance": Decimal("0"), "buying_power": Decimal("0"),
        "account": "ACC"})
    now_iso = "2026-07-31T12:00:00+00:00"
    for d, start, end, pnl, capital, twr in (
        ("2026-07-14", "373.68", "46732.15", "3381.81", "42976.66", "10"),
        ("2026-07-15", "46732.15", "43798.47", "-2933.68", "0", "-5"),
        ("2026-07-30", "39592.59", "48238.71", "8646.12", "0", "20"),
    ):
        db.upsert_flex_nav_daily({
            "account_id": "ACC", "et_date": d, "currency": "USD",
            "starting_value": start, "ending_value": end, "total_pnl": pnl,
            "mtm": pnl, "commissions": "0", "capital_movements": capital,
            "deposits_withdrawals": "0", "internal_cash_transfers": "0",
            "asset_transfers": capital, "dividends": "0", "interest": "0",
            "fees": "0", "other": "0", "twr": twr, "raw_json": "{}",
            "source": "FLEX_CHANGE_IN_NAV", "report_hash": "test",
            "created_at": now_iso, "updated_at": now_iso,
        })
    now = datetime(2026, 7, 31, 7, 59, tzinfo=timezone.utc)
    cards = webdata.pnl_cards(cfg, db, now)
    history = cards["windows"][1]
    assert history.end_date == "2026-07-30"
    assert history.amount == Decimal("9094.25")
    # (1+10%)*(1-5%)*(1+20%)-1 = 25.4%，不是盈亏除以 373.68。
    assert history.pct == Decimal("0.254")


def test_overview_does_not_expose_curve_range_selector(client):
    html = client.get("/").text
    assert "资产曲线" not in html
    assert 'href="/?range=7D"' not in html


def test_curve_auto_all_when_history_short(seeded):  # noqa: F811
    cfg, db = seeded
    db.backfill_daily_equity("ACC", cfg.market_tz)
    cv = webdata.equity_curve_data(cfg, db, "7D", NOW)
    assert cv["auto_all"] is True and cv["range"] == "ALL"


# ---------------- 全站北京时间 ----------------

def test_display_time_is_beijing_without_label():
    """UTC 2026-07-16 06:00 = 北京 14:00；且不加「北京时间」四个字。"""
    assert webdata.abs_time("2026-07-16T06:00:00+00:00") == "2026-07-16 14:00"
    assert "ET" not in webdata.abs_time("2026-07-16T06:00:00+00:00")
    assert "北京" not in webdata.abs_time("2026-07-16T06:00:00+00:00")


def test_no_et_suffix_anywhere_on_pages(client):
    for path in ("/", "/history", "/rules", "/status", "/reference"):
        html = client.get(path).text
        assert " ET<" not in html and " ET " not in html.replace("ET 交易日", "")


# ---------------- 进度型空状态 ----------------

def test_window_card_shows_progress_bar(make_config):
    """近7日历史不足时是进度型空状态（近30日已改为「历史盈亏」，总能算出）。

    用**相对真实日期**的两天：概览路由用真实 now 计算窗口，硬编码绝对日期会随
    日期滚动漂移（近7日会从 INSUFFICIENT 变成 CHANGE_ONLY）。
    """
    from app.db import Database
    from datetime import timedelta
    cfg = make_config(ib_account="U1")
    d = Database(cfg.db_path)
    d.init_schema()
    today = datetime.now(cfg.market_tz).date()
    # 只有 2 天历史（不足 7 天），且无跳变（无基线）
    for i, dt in enumerate((today - timedelta(days=1), today)):
        d.insert_balances_snapshot(f"{dt.isoformat()}T20:00:00+00:00", {
            "equity": Decimal(40000 + i * 100), "cash": Decimal("0"),
            "margin_balance": Decimal("0"), "buying_power": Decimal("0"), "account": "U1"})
    d.backfill_daily_equity("U1", cfg.market_tz)
    html = TestClient(create_app(cfg, d)).get("/", params={}).text
    # 近7日应是进度型空状态
    assert "暂不可计算" in html
    assert "历史覆盖" in html and "预计可用日期" in html
    assert 'class="bar"' in html


def test_inception_pnl_replaces_30day(client):
    """近30日已换成「历史盈亏」，自入金后累计。"""
    html = client.get("/").text
    assert "历史盈亏" in html
    assert "近30日" not in html


# ---------------- 历史页只留按成交 ----------------

def test_history_has_no_decision_view(client):
    html = client.get("/history").text
    assert "按决策" not in html
    assert "按成交" not in html          # 没有切换了，直接就是成交明细


def test_history_uses_event_driven_two_second_refresh(client):
    html = client.get("/history").text
    assert 'id="history-live"' in html
    assert 'hx-trigger="every 2s [!document.hidden]"' in html
    assert "IBKR 成交事件推送" in html
    assert "45 秒同步" not in html


# ---------------- 收益卡片技术文字折叠 ----------------

def test_pnl_cards_fold_technical_text(client):
    html = client.get("/").text
    assert 'class="why-more"' in html     # 技术细节收进 ⓘ
    # 用 h2 锚点框定收益区（「账户风险评估」等字样也出现在顶部状态栏，裸串不可靠）
    start = html.index("<h2>收益概览")
    block = html[start:html.index("<h2", start + 5)]
    assert block.count("why-more") >= 3   # 净资产/今日盈亏/持仓浮动盈亏


def test_risk_cards_use_alert_color_not_loss_green(make_config, tmp_path):
    """回归：超阈值必须是警告色。

    用 .down 表示「超标」会显示成亏损绿 —— 集中度 78% 画成绿色，
    看起来像好事。这是拆分涨跌色/状态色时最容易漏的地方。

    风险区在 3d13b76 合并为「账户风险评估」并改为 `{% if risk %}` 门控，需有可评估
    的账户快照才渲染，故本用例自建一个高集中度快照，不复用 seeded fixture。
    """
    cfg = make_config(db_path=tmp_path / "risk-color.db")
    db = Database(cfg.db_path)
    db.init_schema()
    db.write_account_snapshot(
        snapshot_id="s1", ts="2026-08-05T10:00:00+00:00",
        balances={"account": "ACC", "equity": "10000", "cash": "2000",
                  "margin_balance": "0", "buying_power": "2000"},
        positions=[{"symbol": "RKLB", "qty": "100", "price": "78",
                    "avg_cost": "70", "market_value": "7800", "upl": "800"}])
    html = TestClient(create_app(cfg, db)).get("/").text
    # 锚定 body 里的风险区 h2（顶部状态栏也含「账户风险评估」字样，裸串不可靠）
    start = html.index('账户风险评估<span class="h2sub')
    block = html[start:html.index("<h2", start + 5)]
    # 核心：风险卡片不得套 .up/.down（那是涨跌色，超标会被画成亏损绿）
    assert 'class="v down"' not in block and 'class="v up"' not in block
    # 超标改用状态色分级：卡片带严重程度标签 sevtag（正常/接近/严重），不是涨跌绿
    assert "sevtag" in block


def test_pnl_cards_use_updown_class_not_status_color():
    """盈亏卡片用涨跌类（.up/.down），风险摘要用 style 直填状态色 —— 两套不混。

    语义不同：盈亏红涨绿跌，风险超标变红。撞色会让「盈利」和「超标」混淆。
    直接看模板源码（渲染后条件类会消失，断言输出不可靠）。"""
    import pathlib
    tpl = pathlib.Path("app/templates/overview.html").read_text()
    # 收益区到「账户风险评估」为止；风险区（账户风险评估→市场环境）在其后，
    # 不属于收益卡片。风险摘要已并入账户风险评估（refactor 3d13b76）。
    income = tpl[tpl.index("收益概览"):tpl.index("账户风险评估")]
    risk = tpl[tpl.index("账户风险评估"):tpl.index("市场环境")]
    assert "'up' if p.daily" in income                 # 盈亏走涨跌类
    assert "sevtag" not in income                      # 收益不套风险分级标签
    assert "sevtag" in risk                            # 风险走状态分级标签


# ---------------- 交易历史实时性 ----------------
# 线上故障：executions 有今日 12 笔（45 秒同步），transactions 只有 2 笔
# （每日 05:30 cron）。明细表读慢表 -> 盘中刚成交的单要等到次日凌晨才显示。

def test_history_reads_executions_not_transactions(make_config):
    """明细表必须读 executions —— 与风险摘要/R4/R5/已实现盈亏同源。"""
    from app.db import Database
    from app.ibkr_client import split_exec_id
    cfg = make_config(ib_account="U1")
    d = Database(cfg.db_path)
    d.init_schema()
    # transactions 里放一笔「旧的」，executions 里放一笔「刚成交的」
    d.upsert_transactions([{
        "txn_id": "old1", "ts": "2026-07-15T03:00:00+00:00", "symbol": "OLD",
        "action": "BUY", "qty": Decimal("1"), "price": Decimal("1"),
        "fees": Decimal("0"), "amount": Decimal("-1"), "realized": None,
        "raw_json": "{}"}])
    root, seq = split_exec_id("0000new1.aaaa.01.01")
    d.upsert_executions([{
        "exec_id": "0000new1.aaaa.01.01", "correction_root": root, "correction_seq": seq,
        "symbol": "KMEM", "side": "SELL", "shares": Decimal("500"),
        "price": Decimal("17.73"), "perm_id": 1, "order_id": 0, "client_id": 0,
        "conid": 1, "sec_type": "STK", "exchange": "SMART", "currency": "USD",
        "cumulative_qty": Decimal("500"), "average_price": Decimal("17.73"),
        "execution_time": "2026-07-16T15:35:55+00:00", "commission": Decimal("1.5"),
        "commission_currency": "USD", "realized_pnl": Decimal("-613.832905"),
        "liquidation": 0, "order_ref": None, "last_liquidity": 1,
        "data_status": "READY", "raw_json": "{}"}])

    rows = webdata.history_rows(cfg, d)
    assert [r["symbol"] for r in rows] == ["KMEM"]        # 只认 executions
    assert rows[0]["realized"] == Decimal("-613.832905")  # 券商权威值，无估算
    assert "OLD" not in [r["symbol"] for r in rows]       # 慢表不再是数据源


def test_history_marks_pending_commission(make_config):
    """佣金是异步事件，可能比成交回报晚到 —— 标「待入账」而不是显示 0。"""
    from app.db import Database
    from app.ibkr_client import split_exec_id
    cfg = make_config(ib_account="U1")
    d = Database(cfg.db_path)
    d.init_schema()
    root, seq = split_exec_id("0000pend.aaaa.01.01")
    d.upsert_executions([{
        "exec_id": "0000pend.aaaa.01.01", "correction_root": root, "correction_seq": seq,
        "symbol": "DRAM", "side": "BUY", "shares": Decimal("100"),
        "price": Decimal("53.52"), "perm_id": 2, "order_id": 0, "client_id": 0,
        "conid": 2, "sec_type": "STK", "exchange": "SMART", "currency": "USD",
        "cumulative_qty": Decimal("100"), "average_price": Decimal("53.52"),
        "execution_time": "2026-07-16T15:40:00+00:00", "commission": None,
        "commission_currency": None, "realized_pnl": None,
        "liquidation": 0, "order_ref": None, "last_liquidity": 1,
        "data_status": "PENDING_COMMISSION", "raw_json": "{}"}])
    r = webdata.history_rows(cfg, d)[0]
    assert r["pending_commission"] is True
    assert r["realized"] is None                          # 开仓无已实现，不是 0
    assert r["amount"] == Decimal("-5352.00")             # 买入为现金流出


def test_history_newest_first_with_time(make_config):
    from app.db import Database
    from app.ibkr_client import split_exec_id
    cfg = make_config(ib_account="U1")
    d = Database(cfg.db_path)
    d.init_schema()
    rows_in = []
    for i, ts in enumerate(("2026-07-16T15:35:55+00:00", "2026-07-16T15:40:00+00:00")):
        eid = f"0000t{i}.aaaa.01.01"
        root, seq = split_exec_id(eid)
        rows_in.append({
            "exec_id": eid, "correction_root": root, "correction_seq": seq,
            "symbol": "X", "side": "BUY", "shares": Decimal("1"), "price": Decimal("1"),
            "perm_id": i, "order_id": 0, "client_id": 0, "conid": 1, "sec_type": "STK",
            "exchange": "SMART", "currency": "USD", "cumulative_qty": Decimal("1"),
            "average_price": Decimal("1"), "execution_time": ts,
            "commission": Decimal("0"), "commission_currency": "USD",
            "realized_pnl": None, "liquidation": 0, "order_ref": None,
            "last_liquidity": 1, "data_status": "READY", "raw_json": "{}"})
    d.upsert_executions(rows_in)
    rows = webdata.history_rows(cfg, d)
    assert rows[0]["ts"] > rows[1]["ts"]                  # 最新在前
    assert ":" in rows[0]["time"]                         # 有时刻，不只是日期


# ---------------- 1D 盘中走势（第 16 项） ----------------
# 原先 1D 读 daily_equity，一天只有一个点 —— 画不出走势。
# balances_snapshot 是分钟级的，1D 改读它。

def test_intraday_downsamples(make_config):
    from app.db import Database
    cfg = make_config(ib_account="U1")
    d = Database(cfg.db_path)
    d.init_schema()
    # 一天 600 个分钟级快照
    for i in range(600):
        ts = f"2026-07-16T{13 + i // 60:02d}:{i % 60:02d}:00+00:00"
        d.insert_balances_snapshot(ts, {
            "equity": Decimal(40000 + i), "cash": Decimal("0"),
            "margin_balance": Decimal("0"), "buying_power": Decimal("0"),
            "account": "U1"})
    pts = d.intraday_equity("U1", "2026-07-16T00:00:00+00:00", max_points=240)
    assert 200 <= len(pts) <= 241                  # 降采样，不撑爆 SVG
    assert pts[-1][1] == "40599"                   # 最后一点必须是最新值


def test_intraday_keeps_all_when_few_points(make_config):
    from app.db import Database
    cfg = make_config(ib_account="U1")
    d = Database(cfg.db_path)
    d.init_schema()
    for i in range(5):
        d.insert_balances_snapshot(f"2026-07-16T13:0{i}:00+00:00", {
            "equity": Decimal(40000 + i), "cash": Decimal("0"),
            "margin_balance": Decimal("0"), "buying_power": Decimal("0"),
            "account": "U1"})
    assert len(d.intraday_equity("U1", "2026-07-16T00:00:00+00:00")) == 5


def test_curve_1d_uses_intraday_points(seeded):  # noqa: F811
    cfg, db = seeded
    db.backfill_daily_equity("ACC", cfg.market_tz)
    cv = webdata.equity_curve_data(cfg, db, "1D", NOW)
    assert cv["range"] == "1D" and cv["auto_all"] is False   # 1D 不因历史短而放全


def test_curve_intraday_labels_are_times_not_dates():
    svg = webdata.render_series_svg([("09:30", Decimal("40000")),
                                     ("10:00", Decimal("40200")),
                                     ("11:00", Decimal("39800"))])
    import re
    ticks = re.findall(r'text-anchor="(?:start|middle|end)">([0-9:]+)<', svg)
    assert "09:30" in ticks and "11:00" in ticks
    assert "2026" not in svg              # 盘中曲线没有年份可言


def test_curve_daily_labels_still_have_year():
    svg = webdata.render_series_svg([("2026-07-15", Decimal("1")),
                                     ("2026-07-16", Decimal("2"))])
    assert "2026" in svg


def test_curve_1d_explains_cross_day_range(make_config):
    """一个美东交易日在北京时间跨日：x 轴只有 HH:MM 会让「12:00 → 11:22」像倒序。"""
    from app.db import Database
    cfg = make_config(ib_account="U1")
    d = Database(cfg.db_path)
    d.init_schema()
    # ET 07-16 交易日 = UTC 07-16 04:00 起
    for i, ts in enumerate(("2026-07-16T04:00:00+00:00", "2026-07-17T03:22:00+00:00")):
        d.insert_balances_snapshot(ts, {
            "equity": Decimal(43500 - i * 3900), "cash": Decimal("0"),
            "margin_balance": Decimal("0"), "buying_power": Decimal("0"),
            "account": "U1"})
    now = datetime(2026, 7, 17, 3, 55, tzinfo=timezone.utc)   # ET 07-16 23:55
    cv = webdata.equity_curve_data(cfg, d, "1D", now)
    assert cv["range_note"] is not None
    assert "美东 2026-07-16 交易日" in cv["range_note"]
    assert "07-16 12:00" in cv["range_note"]      # 北京时间起点
    assert "07-17 11:22" in cv["range_note"]      # 北京时间终点（跨日）


# ---------------- 市场时段（第 1 项） ----------------

def _mid_now():   # 盘中：UTC 14:00 = ET 10:00 周四
    return datetime(2026, 7, 16, 14, 0, tzinfo=timezone.utc)


def _weekend_now():   # 周六 UTC 15:00
    return datetime(2026, 7, 18, 15, 0, tzinfo=timezone.utc)


def test_overview_shows_market_session(seeded):  # noqa: F811
    cfg, db = seeded
    db.backfill_daily_equity("ACC", cfg.market_tz)
    data = webdata.overview_data(cfg, db, _mid_now())
    assert data["market_session"] == "REGULAR"
    assert data["market_session_label"] == "盘中"


def test_status_bar_relaxes_freshness_when_closed(seeded):  # noqa: F811
    """休市时行情「未更新」是正常的，不该标红报过期。"""
    cfg, db = seeded
    st = webdata.data_status_bar(cfg, db, _weekend_now())
    assert st["market_session"] == "CLOSED"
    assert "休市中" not in st["market_note"]      # 不写「休市中」
    assert "下次开盘" in st["market_note"]
    # 休市时没有 crit（都放宽到 warn 上限）
    assert all(it["css"] != "crit" for it in st["entries"])


def test_capabilities_ok_when_closed_only_close_price(make_config):
    """休市 + 只有收盘价：行情能力与规则能力都应是 OK（收盘价此时就是有效价）。"""
    from app.db import Database
    cfg = make_config(ib_account="U1")
    d = Database(cfg.db_path)
    d.init_schema()
    now = datetime(2026, 7, 18, 15, 0, tzinfo=timezone.utc)   # 周六
    d.insert_balances_snapshot(now.isoformat(), {
        "equity": Decimal("40000"), "cash": Decimal("0"), "margin_balance": Decimal("0"),
        "buying_power": Decimal("0"), "account": "U1"}, sync_run_id="b1")
    d.insert_positions_snapshot(now.isoformat(), [{
        "symbol": "KMEM", "qty": Decimal("100"), "avg_cost": Decimal("19"),
        "market_value": Decimal("1914"), "upl": Decimal("14"),
        "price": Decimal("19.14"), "price_source": "CLOSE",
        "price_quality": "DELAYED_FROZEN", "quote_as_of": None}], sync_run_id="b1")
    d.set_sync_state("data_health", "READY")
    caps = {c["key"]: c for c in webdata.data_status_bar(cfg, d, now)["capabilities"]}
    assert caps["quotes"]["state"] == "OK"
    assert "休市中" in caps["quotes"]["note"]


def test_price_label_pre_market(make_config):
    from app.db import Database
    cfg = make_config(ib_account="U1")
    d = Database(cfg.db_path)
    d.init_schema()
    pre = datetime(2026, 7, 16, 9, 0, tzinfo=timezone.utc)   # ET 05:00 盘前
    d.insert_balances_snapshot(pre.isoformat(), {
        "equity": Decimal("40000"), "cash": Decimal("0"), "margin_balance": Decimal("0"),
        "buying_power": Decimal("0"), "account": "U1"}, sync_run_id="b1")
    d.insert_positions_snapshot(pre.isoformat(), [{
        "symbol": "KMEM", "qty": Decimal("100"), "avg_cost": Decimal("19"),
        "market_value": Decimal("1914"), "upl": Decimal("14"),
        "price": Decimal("19.14"), "price_source": "LAST", "price_quality": "DELAYED",
        "quote_as_of": pre.isoformat()}], sync_run_id="b1")
    row = webdata.overview_data(cfg, d, pre)["positions"][0]
    assert row["price_label"] == "盘前价"


# ---------------- 第 2 项：资产曲线跨周末连贯 ----------------

def test_curve_connects_across_weekend():
    """周末没有日终快照，但周五→周一必须连贯，不断线、不标中断。"""
    # 周五 07-17 → 周一 07-20（周六日无数据）
    pts = [("2026-07-16", Decimal("40000")), ("2026-07-17", Decimal("40500")),
           ("2026-07-20", Decimal("41000"))]
    svg = webdata.render_series_svg(pts)
    assert svg.count("<polyline") == 1        # 一条连贯的线
    assert "数据中断" not in svg
    assert webdata.find_gaps(pts) == []


def test_curve_weekend_gap_not_broken_even_on_big_move():
    """周一相对周五的正常涨跌（哪怕 20%+）不该被当成资金流跳变而断线。"""
    pts = [("2026-07-17", Decimal("40000")), ("2026-07-20", Decimal("50000"))]
    assert webdata.render_series_svg(pts).count("<polyline") == 1


def test_curve_still_breaks_intraday_cash_flow_jump():
    """对照：工作日内的 20%+ 跳变仍要断（那是资金流，不是行情）。

    断开后两点各成孤立点（circle），不再由一条 polyline 相连。"""
    pts = [("2026-07-16", Decimal("40000")), ("2026-07-17", Decimal("50000"))]
    svg = webdata.render_series_svg(pts)
    assert svg.count("<polyline") == 0                    # 无连线：两点被断开
    assert 'r="2.5"' in svg                               # 孤立段画成实心点

    # 对照：不跳变时两点连成一条线
    ok = webdata.render_series_svg([("2026-07-16", Decimal("40000")),
                                    ("2026-07-17", Decimal("40100"))])
    assert ok.count("<polyline") == 1


def test_curve_still_shades_multiday_outage():
    """对照：gateway 挂 3 个工作日仍要标中断。"""
    pts = [("2026-07-13", Decimal("40000")), ("2026-07-17", Decimal("40500"))]  # 周一→周五
    assert "数据中断" in webdata.render_series_svg(pts, gaps=webdata.find_gaps(pts))


# ---------------- 收益日历（第 2 项） ----------------

def _cal_db(make_config):
    from app.db import Database
    cfg = make_config(ib_account="U1")
    d = Database(cfg.db_path)
    d.init_schema()
    for dt, v in (("2026-07-14", "47069.92"), ("2026-07-15", "43500.49"),
                  ("2026-07-16", "39592.46"), ("2026-07-17", "40174.62")):
        d.insert_balances_snapshot(f"{dt}T20:00:00+00:00", {
            "equity": Decimal(v), "cash": Decimal("0"), "margin_balance": Decimal("0"),
            "buying_power": Decimal("0"), "account": "U1"})
    d.backfill_daily_equity("U1", cfg.market_tz)
    return cfg, d


def test_calendar_page_renders(make_config):
    cfg, d = _cal_db(make_config)
    html = TestClient(create_app(cfg, d)).get("/calendar", params={"year": 2026, "month": 7}).text
    assert "收益日历" in html
    assert "2026 年 7 月" in html
    # 15 号相对 14 号亏损，应出现
    assert "-3,569.43" in html or "3,569" in html


def test_calendar_month_navigation(make_config):
    cfg, d = _cal_db(make_config)
    data = webdata.calendar_data(cfg, d, 2026, 7)
    assert data["prev"] == {"year": 2026, "month": 6}
    assert data["next"] == {"year": 2026, "month": 8}
    # 跨年
    dec = webdata.calendar_data(cfg, d, 2026, 12)
    assert dec["next"] == {"year": 2027, "month": 1}
    jan = webdata.calendar_data(cfg, d, 2026, 1)
    assert jan["prev"] == {"year": 2025, "month": 12}


def test_calendar_daily_return_and_pct(make_config):
    cfg, d = _cal_db(make_config)
    data = webdata.calendar_data(cfg, d, 2026, 7)
    # 找 07-15 那格
    cells = [c for wk in data["weeks"] for c in wk if c and c["et_date"] == "2026-07-15"]
    assert len(cells) == 1
    c = cells[0]
    assert c["amount"] == Decimal("43500.49") - Decimal("47069.92")
    assert c["pct"] is not None


def test_calendar_weekend_cells_empty(make_config):
    cfg, d = _cal_db(make_config)
    data = webdata.calendar_data(cfg, d, 2026, 7)
    # 07-18 周六无数据
    sat = [c for wk in data["weeks"] for c in wk if c and c["et_date"] == "2026-07-18"]
    assert sat[0]["amount"] is None


def test_calendar_month_total(make_config):
    cfg, d = _cal_db(make_config)
    data = webdata.calendar_data(cfg, d, 2026, 7)
    assert data["month_amount"] is not None       # 有数据 -> 有合计


def test_calendar_in_nav(make_config):
    cfg, d = _cal_db(make_config)
    assert "收益日历" in TestClient(create_app(cfg, d)).get("/").text


def test_calendar_year_view(make_config):
    """按年视图：12 个月各自收益 + 全年合计（第 3 项）。"""
    cfg, d = _cal_db(make_config)
    client = TestClient(create_app(cfg, d))
    html = client.get("/calendar", params={"year": 2026, "view": "year"}).text
    assert "2026 年" in html
    assert "全年合计" in html
    assert "7 月" in html and "12 月" in html          # 12 个月都在
    assert 'class="ymon' in html                       # 月份卡片
    # 年数据结构
    data = webdata.calendar_year_data(cfg, d, 2026)
    jul = next(m for m in data["months"] if m["month"] == 7)
    assert jul["has_data"] is True and jul["amount"] is not None
    assert data["year_amount"] is not None


def test_calendar_year_month_pct_chains_official_twr(make_config):
    """月收益率不得在资产转移月使用「盈亏 ÷ 月初小额净资产」。"""
    cfg = make_config(ib_account="ACC")
    d = Database(cfg.db_path); d.init_schema()
    now_iso = "2026-07-31T12:00:00+00:00"
    for day, pnl, twr, capital in (
        ("2026-07-14", "3381.81", "10", "42976.66"),
        ("2026-07-15", "-2933.68", "-5", "0"),
    ):
        d.upsert_flex_nav_daily({
            "account_id": "ACC", "et_date": day, "currency": "USD",
            "starting_value": "373.68", "ending_value": "43798.47",
            "total_pnl": pnl, "mtm": pnl, "commissions": "0",
            "capital_movements": capital, "deposits_withdrawals": "0",
            "internal_cash_transfers": "0", "asset_transfers": capital,
            "dividends": "0", "interest": "0", "fees": "0", "other": "0",
            "twr": twr, "raw_json": "{}", "source": "FLEX_CHANGE_IN_NAV",
            "report_hash": "test", "created_at": now_iso, "updated_at": now_iso,
        })
    data = webdata.calendar_year_data(
        cfg, d, 2026, datetime(2026, 7, 31, 12, tzinfo=timezone.utc))
    july = next(m for m in data["months"] if m["month"] == 7)
    assert july["amount"] == Decimal("448.13")
    assert july["pct"] == Decimal("0.045")  # 1.10 * 0.95 - 1


def test_calendar_month_year_toggle(make_config):
    cfg, d = _cal_db(make_config)
    html = TestClient(create_app(cfg, d)).get(
        "/calendar", params={"year": 2026, "month": 7}).text
    assert "view=year" in html and "按年" in html       # 有切换入口


def test_calendar_weekend_cells_shaded(make_config):
    """周六周日列做灰底填充（第 3 项）。"""
    cfg, d = _cal_db(make_config)
    html = TestClient(create_app(cfg, d)).get(
        "/calendar", params={"year": 2026, "month": 7}).text
    # 表头六/日带 weekend 类；周末格子也带 weekend 类
    assert '<th class="weekend">六</th>' in html
    assert '<th class="weekend">日</th>' in html
    assert html.count("weekend") >= 4          # 至少表头两列 + 若干周末格


def test_next_open_moved_from_nav_to_status(make_config):
    """下次开盘时间不在导航栏，移到系统状态页（第 2 项）。"""
    cfg, d = _cal_db(make_config)
    client = TestClient(create_app(cfg, d))
    # 概览导航栏不再出现「下次开盘」
    assert "下次开盘" not in client.get("/").text
    # 系统状态页有市场时段区块
    status_html = client.get("/status").text
    assert "市场时段" in status_html


# ---------------- 持仓卡片布局 + 突出显示（第 4 项） ----------------

def _pos_db(make_config):
    from app.db import Database
    cfg = make_config(ib_account="U1")
    d = Database(cfg.db_path)
    d.init_schema()
    ts = datetime(2026, 7, 16, 14, tzinfo=timezone.utc).isoformat()   # 盘中
    d.insert_balances_snapshot(ts, {
        "equity": Decimal("40000"), "cash": Decimal("0"), "margin_balance": Decimal("0"),
        "buying_power": Decimal("0"), "account": "U1"}, sync_run_id="b1")
    # 三个持仓：BMNR 正常小仓、DRAM 大仓已跌破、MRVL 中仓
    d.insert_positions_snapshot(ts, [
        {"symbol": "BMNR", "qty": Decimal("100"), "avg_cost": Decimal("14"),
         "market_value": Decimal("1500"), "upl": Decimal("100"),
         "price": Decimal("15"), "price_source": "LAST", "price_quality": "DELAYED",
         "quote_as_of": ts},
        {"symbol": "DRAM", "qty": Decimal("500"), "avg_cost": Decimal("53"),
         "market_value": Decimal("25000"), "upl": Decimal("-1000"),
         "price": Decimal("50"), "price_source": "LAST", "price_quality": "DELAYED",
         "quote_as_of": ts},
        {"symbol": "MRVL", "qty": Decimal("21"), "avg_cost": Decimal("200"),
         "market_value": Decimal("4000"), "upl": Decimal("-200"),
         "price": Decimal("190"), "price_source": "LAST", "price_quality": "DELAYED",
         "quote_as_of": ts},
    ], sync_run_id="b1")
    # DRAM 止损位在现价之上 -> 已跌破
    d.upsert_stop_level("2026-07-15", {
        "symbol": "DRAM", "side": "LONG", "close": Decimal("53"), "atr": Decimal("2"),
        "swing": Decimal("52"), "stop_price": Decimal("52"), "risk_amount": Decimal("0"),
        "risk_pct": Decimal("0"), "exceeds_2pct": False, "suggested_trim": 0,
        "reached_1r": False, "trailing_stop": Decimal("52"), "take_profit": "-",
        "atr_period": 14, "bars_used": 60, "data_sufficient": True, "data_note": None})
    return cfg, d


def test_positions_render_as_rows(make_config):
    cfg, d = _pos_db(make_config)
    html = TestClient(create_app(cfg, d)).get("/").text
    assert 'class="postbl"' in html          # 一行一只的持仓表
    assert "BMNR" in html and "DRAM" in html and "MRVL" in html


def test_breached_position_sorted_first(make_config):
    """已跌破止损的持仓置顶（突出显示）。"""
    cfg, d = _pos_db(make_config)
    now = datetime(2026, 7, 16, 14, tzinfo=timezone.utc)
    positions = webdata.overview_data(cfg, d, now)["positions"]
    assert positions[0]["symbol"] == "DRAM"        # 已跌破 -> 第一
    assert positions[0]["breached"] is True


def test_breached_row_has_crit_class(make_config):
    cfg, d = _pos_db(make_config)
    html = TestClient(create_app(cfg, d)).get("/").text
    # 已跌破止损的行带 crit 类（整行红色突出）
    assert 'pfrow crit' in html
    # 处置建议列给出明确状态，取代原来的独立橙色提示行
    assert "已触发止损" in html


def test_position_card_shows_session_price_label(make_config):
    """盘中价格标「现价」。"""
    cfg, d = _pos_db(make_config)
    html = TestClient(create_app(cfg, d)).get("/").text
    assert "现价" in html


def test_position_falls_back_to_close_when_no_live_quote(make_config):
    """休市/无实时行情：用日线收盘价作展示价，不再显示「无行情」（第 2 项）。"""
    from app.db import Database
    cfg = make_config(ib_account="U1")
    d = Database(cfg.db_path)
    d.init_schema()
    # 周六休市：无实时价格（price=None），但有日线收盘
    ts = datetime(2026, 7, 18, 12, tzinfo=timezone.utc).isoformat()   # 周六 = CLOSED
    d.insert_balances_snapshot(ts, {
        "equity": Decimal("40000"), "cash": Decimal("0"), "margin_balance": Decimal("0"),
        "buying_power": Decimal("0"), "account": "U1"}, sync_run_id="b1")
    d.insert_positions_snapshot(ts, [
        {"symbol": "DRAM", "qty": Decimal("100"), "avg_cost": Decimal("53"),
         "market_value": Decimal("5200"), "upl": Decimal("-100"),
         "price": None, "price_source": None, "price_quality": "MISSING",
         "quote_as_of": None},
    ], sync_run_id="b1")
    d.upsert_daily_bars("DRAM", [
        {"date": "2026-07-16", "o": Decimal("53"), "h": Decimal("54"),
         "l": Decimal("51"), "c": Decimal("52"), "v": 1000},
        {"date": "2026-07-17", "o": Decimal("52"), "h": Decimal("53"),
         "l": Decimal("51"), "c": Decimal("51.5"), "v": 1000},
    ])
    now = datetime(2026, 7, 18, 12, tzinfo=timezone.utc)
    q = webdata.overview_data(cfg, d, now)["positions"][0]
    assert q["price"] == Decimal("51.5")       # 回退到最近收盘
    assert q["price_label"] == "收盘价"
    html = TestClient(create_app(cfg, d)).get("/").text
    assert "无行情" not in html
    assert "收盘价" in html


# ---------------- 投资日记 ----------------

def _journal_db(make_config):
    from app.db import Database
    cfg = make_config(ib_account="U1")
    d = Database(cfg.db_path)
    d.init_schema()
    return cfg, d


def test_journal_reachable_in_nav(make_config):
    cfg, d = _journal_db(make_config)
    html = TestClient(create_app(cfg, d)).get("/").text
    assert "投资日记" in html
    # 3d13b76 重排后：投资日记（/journal）在首页主导航；收益日历（/calendar）
    # 下沉到收益二级导航，不再与日记并列，故只校验日记可达。
    assert 'href="/journal"' in html


def test_journal_add_and_list_newest_first(make_config):
    cfg, d = _journal_db(make_config)
    client = TestClient(create_app(cfg, d))
    client.post("/journal", content="content=%E7%AC%AC%E4%B8%80%E6%9D%A1",
                headers={"content-type": "application/x-www-form-urlencoded"})
    client.post("/journal", content="content=%E7%AC%AC%E4%BA%8C%E6%9D%A1",
                headers={"content-type": "application/x-www-form-urlencoded"})
    entries = d.list_journal_entries()
    assert len(entries) == 2
    # 页面按更新时间倒序：最新（第二条）在前
    html = client.get("/journal").text
    assert html.index("第二条") < html.index("第一条")


def test_journal_edit_bumps_and_marks_edited(make_config):
    cfg, d = _journal_db(make_config)
    eid = d.add_journal_entry("原文")
    client = TestClient(create_app(cfg, d))
    client.post("/journal/edit", content=f"id={eid}&content=%E6%94%B9%E5%90%8E",
                headers={"content-type": "application/x-www-form-urlencoded"})
    e = d.list_journal_entries()[0]
    assert e["content"] == "改后"
    assert e["updated_at"] != e["created_at"]      # 更新时间已刷新
    assert "已编辑" in client.get("/journal").text


def test_journal_delete(make_config):
    cfg, d = _journal_db(make_config)
    eid = d.add_journal_entry("待删除")
    client = TestClient(create_app(cfg, d))
    client.post("/journal/delete", content=f"id={eid}",
                headers={"content-type": "application/x-www-form-urlencoded"})
    assert d.list_journal_entries() == []


def test_journal_blank_content_ignored(make_config):
    cfg, d = _journal_db(make_config)
    client = TestClient(create_app(cfg, d))
    client.post("/journal", content="content=%20%20",
                headers={"content-type": "application/x-www-form-urlencoded"})
    assert d.list_journal_entries() == []          # 空白不入库


def test_journal_renders_markdown_and_keeps_source_for_editing(make_config):
    cfg, d = _journal_db(make_config)
    source = "# 复盘\n\n**结论**\n\n- 保留尾仓\n\n| 标的 | 动作 |\n| --- | --- |\n| MU | 减仓 |"
    d.add_journal_entry(source)
    html = TestClient(create_app(cfg, d)).get("/journal").text
    assert "<h1>复盘</h1>" in html
    assert "<strong>结论</strong>" in html
    assert "<li>保留尾仓</li>" in html
    assert "<table>" in html and "<td>MU</td>" in html
    # 编辑框保留 Markdown 源文，不把渲染后的 HTML 写回去。
    assert "# 复盘" in html and "**结论**" in html


def test_journal_markdown_escapes_html_and_dangerous_links(make_config):
    cfg, d = _journal_db(make_config)
    d.add_journal_entry('<script>alert("x")</script>\n\n[x](javascript:alert(1))')
    html = TestClient(create_app(cfg, d)).get("/journal").text
    assert '<script>alert("x")</script>' not in html
    assert '&lt;script&gt;alert(&quot;x&quot;)&lt;/script&gt;' in html
    assert 'href="javascript:' not in html


def test_journal_legacy_plain_text_keeps_line_breaks(make_config):
    cfg, d = _journal_db(make_config)
    d.add_journal_entry("第一行\n第二行")
    html = TestClient(create_app(cfg, d)).get("/journal").text
    assert "第一行<br" in html and "第二行" in html
    assert "支持 Markdown" in html







# ---------------- 调度器：累计计数不长期挂红灯 ----------------

def test_stale_missed_count_not_red(make_config):
    """错过执行是累计数、永不归零：早已过去的一次不该一直标红。"""
    from app.db import Database
    from datetime import timedelta
    cfg = make_config(ib_account="U1")
    d = Database(cfg.db_path); d.init_schema()
    old = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
    d.set_sync_state("scheduler_missed_count", "46")
    d.set_sync_state("scheduler_last_missed", old)
    d.set_sync_state("scheduler_last_error", old)
    d.set_sync_state("scheduler_last_error_job", "positions_rules")
    sy = webdata.system_health(cfg, d, datetime.now(timezone.utc))
    assert sy["scheduler"]["missed_recent"] is False      # 2 天前 -> 不算正在发生
    assert sy["scheduler"]["error_recent"] is False
    html = TestClient(create_app(cfg, d)).get("/status").text
    assert "累计 46 次" in html and "近 1 小时无" in html
    assert "已恢复" in html                                # 旧异常标注已恢复


def test_recent_miss_is_red(make_config):
    """刚刚发生的错过执行仍要标红（不能把真问题也淡化掉）。"""
    from app.db import Database
    from datetime import timedelta
    cfg = make_config(ib_account="U1")
    d = Database(cfg.db_path); d.init_schema()
    d.set_sync_state("scheduler_missed_count", "3")
    d.set_sync_state("scheduler_last_missed",
                     (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat())
    sy = webdata.system_health(cfg, d, datetime.now(timezone.utc))
    assert sy["scheduler"]["missed_recent"] is True
    assert "近 1 小时无" not in TestClient(create_app(cfg, d)).get("/status").text


# ---------------- 主题切换 ----------------

def test_theme_toggle_present(client):
    """页面必须提供手动切换主题的入口。"""
    html = client.get("/").text
    assert 'id="themetog"' in html
    assert "跟随系统" in html and "亮色" in html and "暗色" in html
    assert 'title="切换主题：跟随系统 / 亮色 / 暗色"' in html


def test_theme_applied_before_first_paint(client):
    """主题必须在**首次绘制前**定好，否则会闪一下默认色再跳。

    判据：读 localStorage 并写 data-theme 的脚本出现在 <head> 内，
    且在 <body> 之前。
    """
    html = client.get("/").text
    head = html[:html.index("</head>")]
    assert 'localStorage.getItem("theme")' in head
    assert 'setAttribute("data-theme"' in head
    assert head.index('localStorage.getItem("theme")') < len(head)
    # 内联同步脚本，不能带 defer/async（否则就晚了）
    seg = head[head.index("<script>"):head.index("</script>") + 9]
    assert "defer" not in seg and "async" not in seg


def test_theme_palettes_defined_once(client):
    """两套调色板各定义一次，三条规则只负责「用哪一套」。"""
    html = client.get("/").text
    assert "--d-bg:" in html and "--l-bg:" in html
    # 手动选择压过系统偏好
    assert ':root[data-theme="light"]' in html
    assert ':root[data-theme="dark"]' in html
    # 没手动选过时才跟随系统
    assert ":root:not([data-theme])" in html
    # 亮色下有 color-scheme，原生控件与滚动条才会跟着变
    assert "color-scheme:light" in html and "color-scheme:dark" in html


def test_theme_toggle_degrades_without_localstorage(client):
    """localStorage 不可用时静默回到跟随系统，不得抛错。"""
    html = client.get("/").text
    head = html[:html.index("</head>")]
    assert "catch (e)" in head          # 读取被 try/catch 包住
    body = html[html.index("</head>"):]
    assert "catch (e)" in body          # 写入也被包住


def test_charts_follow_theme_variables(client):
    """图表不能硬编码颜色，否则切到亮色会看不见。"""
    html = client.get("/").text
    assert ".chart-label{font-size:10px;fill:var(--muted)}" in html
    assert ".chart-axis{stroke:var(--line)" in html
    assert ".chart-zero{stroke:var(--muted)" in html


def _current_positions_table(html):
    """从概览页取「当前持仓」表（含表头「已实现盈亏」的那张 postbl）。"""
    import re
    for t in re.findall(r'<table class="postbl">.*?</table>', html, re.S):
        if "已实现盈亏" in t:
            return t
    return ""


def test_equity_weight_highlights_only_over_threshold(make_config):
    """占总资产超过单仓上限(默认30%)才高亮 crit 色；未超不加色。"""
    import re
    from decimal import Decimal as D
    cfg = make_config(ib_account="U1")     # risk_single_weight_equity_max=0.30
    d = Database(cfg.db_path); d.init_schema()
    d.write_account_snapshot(
        snapshot_id="s1", ts=NOW.isoformat(), sync_run_id="r1",
        quality_status="fresh", ai_analysis_allowed=True,
        balances={"account": "U1", "currency": "USD", "equity": D("20000"),
                  "cash": D("5000"), "total_cash": D("5000"),
                  "margin_balance": D("0"), "buying_power": D("40000"),
                  "available_funds": D("5000"), "excess_liquidity": D("5000"),
                  "maint_margin": D("0"), "init_margin": D("0"),
                  "positions_value": D("15000")},
        positions=[{"symbol": "BIG", "qty": D("100"), "avg_cost": D("50"),
                    "market_value": D("12000"), "upl": D("0"), "price": D("120"),
                    "price_quality": "DELAYED"},        # 12000/20000 = 60% → 高亮
                   {"symbol": "SML", "qty": D("100"), "avg_cost": D("30"),
                    "market_value": D("3000"), "upl": D("0"), "price": D("30"),
                    "price_quality": "DELAYED"}])        # 3000/20000 = 15% → 不高亮
    html = TestClient(create_app(cfg, d)).get("/").text
    tbl = _current_positions_table(html)
    rows = {re.search(r'class="sym">([^<]+)', r).group(1): r
            for r in re.findall(r'<tr class="pfrow.*?</tr>', tbl, re.S)}
    assert "color:var(--crit)" in rows["BIG"]      # 60% 超阈值 → 高亮
    assert "color:var(--crit)" not in rows["SML"]  # 15% → 不高亮


def test_current_price_bold_only_the_number(make_config):
    """现价数字加粗（与均价一样 <b>），但价格标签(盘前价/收盘价等)不加粗；
    且实时更新锚点 data-live-position=price 仍在加粗元素上。"""
    import re
    from decimal import Decimal as D
    from test_web import NOW as _NOW
    cfg = make_config(ib_account="ACC")
    d = Database(cfg.db_path); d.init_schema()
    d.insert_balances_snapshot(_NOW.isoformat(), {
        "equity": D("100000"), "cash": D("60000"), "account": "ACC",
        "margin_balance": D("0"), "buying_power": D("120000")})
    d.insert_positions_snapshot(_NOW.isoformat(), [
        {"symbol": "MU", "qty": D("100"), "avg_cost": D("100"),
         "market_value": D("9000"), "upl": D("-1000"), "price": D("90"),
         "price_quality": "DELAYED"}])
    html = TestClient(create_app(cfg, d)).get("/").text
    tbl = _current_positions_table(html)
    row = re.search(r'<tr class="pfrow.*?</tr>', tbl, re.S).group(0)
    # 现价数字在 <b data-live-position="price"> 里；标签在独立的非加粗 span
    assert re.search(r'<b data-live-position="price" class="(?:up|down)?">', row)
    assert '<span class="plabel' in row       # 标签仍是普通 span，不加粗
    # 止损价不再加粗
    assert '<b>86' not in row and '<b>90.00</b>' not in row
