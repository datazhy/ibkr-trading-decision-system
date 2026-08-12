"""账户快照层 + 数据质量层验收测试。

对应验收清单 1–12。重点不是覆盖率数字，而是把「会导致错误结论的场景」钉死：
半成品快照、失败覆盖有效数据、坏数据被 AI 当成好数据。
"""
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from app import data_quality as dq
from app import sync as sync_mod
from app.db import Database
from app.webapp import create_app

NOW = datetime(2026, 7, 28, 16, 0, tzinfo=timezone.utc)


def _bal(**over):
    b = {"equity": Decimal("40000"), "cash": Decimal("1000"),
         "margin_balance": Decimal("1000"), "buying_power": Decimal("2000"),
         "account": "U1", "currency": "USD", "positions_value": Decimal("39000")}
    b.update(over)
    return b


def _pos(symbol="AAA", qty="10", mv="39000", **over):
    p = {"symbol": symbol, "qty": Decimal(qty), "avg_cost": Decimal("100"),
         "market_value": Decimal(mv), "upl": Decimal("500"), "price": Decimal("3900"),
         "price_quality": "DELAYED"}
    p.update(over)
    return p


@pytest.fixture
def db(make_config):
    cfg = make_config(ib_account="U1")
    d = Database(cfg.db_path)
    d.init_schema()
    return cfg, d


# ---------------- 1. 正常账户快照写入 ----------------

def test_normal_snapshot_write(db):
    cfg, d = db
    n = d.write_account_snapshot(
        snapshot_id="s1", ts=NOW.isoformat(), balances=_bal(), positions=[_pos()],
        sync_run_id="r1", quality_status=dq.FRESH, ai_analysis_allowed=True)
    assert n == 1
    row = dict(d.latest_snapshot_row())
    assert row["snapshot_id"] == "s1"
    assert row["quality_status"] == dq.FRESH
    assert row["ai_analysis_allowed"] == 1
    # 账户汇总与持仓明细用同一个 snapshot_id 关联
    assert [p["symbol"] for p in d.positions_for_snapshot("s1")] == ["AAA"]


def test_history_is_append_only(db):
    """历史快照不被覆盖 —— 这是所有回溯分析的地基。"""
    cfg, d = db
    for i, eq in enumerate(("40000", "41000", "42000")):
        d.write_account_snapshot(
            snapshot_id=f"s{i}", ts=(NOW + timedelta(minutes=i)).isoformat(),
            balances=_bal(equity=Decimal(eq)), positions=[_pos()], sync_run_id=f"r{i}")
    assert d.count_snapshots_between(None, None) == 3
    assert dict(d.latest_snapshot_row())["equity"] == "42000"      # 最新可查
    rows = d.snapshots_between(None, None, limit=10)
    assert [r["equity"] for r in rows] == ["42000", "41000", "40000"]   # 历史仍在


# ---------------- 2. 空持仓账户 ----------------

def test_empty_positions_account_is_valid(db):
    """空仓是合法状态，不该被判成数据缺失（前提：账户持仓市值也是 0）。"""
    cfg, d = db
    rep = dq.evaluate(balances=_bal(positions_value=Decimal("0")), positions=[],
                      now=NOW, data_as_of=NOW.isoformat(), market_open=True)
    assert rep.status == dq.FRESH
    assert rep.ai_analysis_allowed is True
    n = d.write_account_snapshot(snapshot_id="s1", ts=NOW.isoformat(),
                                 balances=_bal(positions_value=Decimal("0")),
                                 positions=[], sync_run_id="r1")
    assert n == 0
    assert d.positions_for_snapshot("s1") == []


# ---------------- 3. IBKR 返回部分数据 ----------------

def test_partial_data_flags_critical(db):
    """有持仓总市值却没有持仓明细 = 半成品，必须 critical 且阻断 AI。"""
    rep = dq.evaluate(balances=_bal(positions_value=Decimal("39000")), positions=[],
                      now=NOW, data_as_of=NOW.isoformat(), market_open=True)
    codes = {i.issue_code for i in rep.issues}
    assert "POSITIONS_EMPTY_BUT_VALUE" in codes
    assert rep.status == dq.ERROR
    assert rep.ai_analysis_allowed is False


def test_missing_core_fields_block_ai():
    rep = dq.evaluate(balances={"equity": None, "cash": None}, positions=[],
                      now=NOW, data_as_of=NOW.isoformat(), market_open=True)
    codes = {i.issue_code for i in rep.issues}
    assert {"NET_LIQ_MISSING", "CASH_MISSING"} <= codes
    assert rep.ai_analysis_allowed is False


# ---------------- 4. IBKR 请求失败 ----------------

def test_sync_failure_does_not_touch_last_valid_snapshot(db):
    """同步失败绝不能破坏最后一份有效快照。"""
    cfg, d = db
    d.write_account_snapshot(snapshot_id="good", ts=NOW.isoformat(), balances=_bal(),
                             positions=[_pos()], sync_run_id="r1",
                             quality_status=dq.FRESH, ai_analysis_allowed=True)
    # 模拟一次失败的 run：只写 sync_runs，不碰快照
    d.start_sync_run("r2", (NOW + timedelta(minutes=1)).isoformat(),
                     account="U1", source="IBKR")
    d.finish_sync_run("r2", status="ERROR", run_state=sync_mod.RUN_FAILED,
                      error_code="BROKER_API_ERROR", error_message="timeout")
    row = dict(d.latest_snapshot_row())
    assert row["snapshot_id"] == "good"          # 有效快照原样保留
    assert d.count_snapshots_between(None, None) == 1
    assert dict(d.latest_sync_run())["run_state"] == "failed"


def test_no_history_plus_failure_blocks_ai():
    rep = dq.evaluate(balances=_bal(), positions=[_pos()], now=NOW,
                      data_as_of=NOW.isoformat(), market_open=True,
                      has_valid_history=False)
    assert "NO_VALID_SNAPSHOT" in {i.issue_code for i in rep.issues}
    assert rep.ai_analysis_allowed is False


# ---------------- 5. 重复同步 ----------------

def test_duplicate_snapshot_detected(db):
    """同账户短时间内重复生成快照 -> info 级提示，但不阻断。"""
    prev = {"ts": NOW.isoformat(), "equity": "40000"}
    rep = dq.evaluate(balances=_bal(), positions=[_pos()], now=NOW,
                      data_as_of=(NOW + timedelta(seconds=1)).isoformat(),
                      market_open=True, prev_balances=prev)
    codes = {i.issue_code for i in rep.issues}
    assert "DUPLICATE_SNAPSHOT" in codes
    assert rep.ai_analysis_allowed is True       # 重复不影响数据正确性


def test_repeated_write_same_snapshot_id_is_idempotent(db):
    """同一 snapshot_id 重复写入不产生重复行（INSERT OR REPLACE）。"""
    cfg, d = db
    for _ in range(3):
        d.write_account_snapshot(snapshot_id="s1", ts=NOW.isoformat(), balances=_bal(),
                                 positions=[_pos()], sync_run_id="r1")
    assert d.count_snapshots_between(None, None) == 1
    assert len(d.positions_for_snapshot("s1")) == 1


# ---------------- 6. 数据过期 ----------------

def test_stale_data_marked_and_not_realtime():
    old = (NOW - timedelta(minutes=30)).isoformat()
    rep = dq.evaluate(balances=_bal(), positions=[_pos()], now=NOW, data_as_of=old,
                      market_open=True)          # 交易时段阈值 300s
    assert rep.status == dq.STALE
    assert "DATA_STALE" in {i.issue_code for i in rep.issues}
    assert rep.ai_analysis_allowed is True       # 过期但未严重过期，仍可分析


def test_closed_market_uses_looser_threshold():
    """休市时同样的 30 分钟不算过期 —— 否则每晚都在误报。"""
    old = (NOW - timedelta(minutes=30)).isoformat()
    rep = dq.evaluate(balances=_bal(), positions=[_pos()], now=NOW, data_as_of=old,
                      market_open=False)
    assert rep.status == dq.FRESH


def test_severely_stale_blocks_ai():
    old = (NOW - timedelta(days=2)).isoformat()
    rep = dq.evaluate(balances=_bal(), positions=[_pos()], now=NOW, data_as_of=old,
                      market_open=True)
    assert rep.status == dq.ERROR
    assert rep.ai_analysis_allowed is False


# ---------------- 7. 净资产异常跳变 ----------------

def test_equity_jump_warns_but_allows_ai():
    prev = {"ts": (NOW - timedelta(minutes=10)).isoformat(), "equity": "40000"}
    rep = dq.evaluate(balances=_bal(equity=Decimal("80000"),
                                    positions_value=Decimal("79000")),
                      positions=[_pos(mv="79000")], now=NOW,
                      data_as_of=NOW.isoformat(), market_open=True, prev_balances=prev)
    assert "EQUITY_JUMP" in {i.issue_code for i in rep.issues}
    assert rep.status == dq.FRESH                # 跳变是警告，不是错误
    assert rep.ai_analysis_allowed is True


def test_negative_equity_is_critical():
    rep = dq.evaluate(balances=_bal(equity=Decimal("-100")), positions=[],
                      now=NOW, data_as_of=NOW.isoformat(), market_open=True)
    assert "NET_LIQ_NEGATIVE" in {i.issue_code for i in rep.issues}
    assert rep.ai_analysis_allowed is False


# ---------------- 8. 持仓汇总与明细不一致 ----------------

def test_positions_value_mismatch_is_critical():
    """账户说持仓 39000，明细只有 10000 -> 快照残缺。"""
    rep = dq.evaluate(balances=_bal(positions_value=Decimal("39000")),
                      positions=[_pos(mv="10000")], now=NOW,
                      data_as_of=NOW.isoformat(), market_open=True)
    assert "POSITIONS_VALUE_MISMATCH" in {i.issue_code for i in rep.issues}
    assert rep.status == dq.ERROR
    assert rep.ai_analysis_allowed is False


def test_small_difference_within_tolerance_is_ok():
    """1% 的差异在容差内 —— 汇率与未结算资金天然会有小偏差，不该刷屏。"""
    rep = dq.evaluate(balances=_bal(positions_value=Decimal("39000")),
                      positions=[_pos(mv="38700")], now=NOW,
                      data_as_of=NOW.isoformat(), market_open=True)
    assert "POSITIONS_VALUE_MISMATCH" not in {i.issue_code for i in rep.issues}


def test_qty_zero_but_value_nonzero_is_critical():
    rep = dq.evaluate(balances=_bal(), positions=[_pos(qty="0", mv="39000")],
                      now=NOW, data_as_of=NOW.isoformat(), market_open=True)
    assert "QTY_ZERO_VALUE_NONZERO" in {i.issue_code for i in rep.issues}
    assert rep.ai_analysis_allowed is False


# ---------------- 9. 数据库事务中途失败 ----------------

def test_bad_position_fails_before_any_write(db):
    """字段缺失在建 payload 阶段就失败 —— 连事务都不开，天然不会留半成品。"""
    cfg, d = db
    d.write_account_snapshot(snapshot_id="good", ts=NOW.isoformat(), balances=_bal(),
                             positions=[_pos()], sync_run_id="r1")

    bad_pos = [_pos(), {"symbol": "BAD"}]        # 缺 qty/avg_cost -> KeyError
    with pytest.raises(KeyError):
        d.write_account_snapshot(
            snapshot_id="broken", ts=(NOW + timedelta(minutes=1)).isoformat(),
            balances=_bal(equity=Decimal("99999")), positions=bad_pos, sync_run_id="r2")

    # 关键：broken 快照整份不存在，good 完好
    assert d.count_snapshots_between(None, None) == 1
    assert dict(d.latest_snapshot_row())["snapshot_id"] == "good"
    assert d.positions_for_snapshot("broken") == []


def test_rollback_also_discards_quality_issues(db):
    """质量问题与快照同事务 —— 快照没写成，问题记录也不该留下孤儿。"""
    cfg, d = db
    with pytest.raises(KeyError):
        d.write_account_snapshot(
            snapshot_id="broken", ts=NOW.isoformat(), balances=_bal(),
            positions=[{"symbol": "BAD"}], sync_run_id="r1",
            issues=[{"issue_code": "X", "severity": "warning",
                     "category": "sanity", "message": "m"}])
    assert d.quality_issues_for("broken") == []


def test_真事务回滚_写入中途失败不留半成品(db):
    """**事务内部**失败必须整份回滚。

    上面两个测试是在建 payload 阶段失败（还没开事务）。这个测试让失败发生在
    账户汇总已经 INSERT、持仓正要 INSERT 的那一刻 —— 这才是「半成品快照」真正的
    产生时机，也是 write_account_snapshot 用单事务包起来的理由。

    做法：给 conid 传一个 SQLite 无法绑定的对象，绑定错误发生在 executemany 时，
    即事务已开、账户汇总已写入之后。
    """
    import sqlite3
    cfg, d = db
    d.write_account_snapshot(snapshot_id="good", ts=NOW.isoformat(), balances=_bal(),
                             positions=[_pos()], sync_run_id="r1")

    with pytest.raises((sqlite3.InterfaceError, sqlite3.ProgrammingError)):
        d.write_account_snapshot(
            snapshot_id="halfway", ts=(NOW + timedelta(minutes=1)).isoformat(),
            balances=_bal(equity=Decimal("99999")),
            positions=[_pos(conid=object())],       # 绑定阶段炸在事务内
            sync_run_id="r2")

    # 账户汇总那一行已经 execute 过，但事务回滚后必须消失
    assert d.count_snapshots_between(None, None) == 1
    row = dict(d.latest_snapshot_row())
    assert row["snapshot_id"] == "good"
    assert row["equity"] == "40000"          # 不是 99999
    assert d.positions_for_snapshot("halfway") == []


# ---------------- 10. 已有同步运行时再次触发 ----------------

def test_concurrent_trigger_rejected():
    now = datetime.now(timezone.utc)
    state, reason = sync_mod.can_trigger_manual_sync(
        running_run={"sync_run_id": "r1"}, last_manual_at=None, now=now,
        min_interval_s=30)
    assert state == sync_mod.TRIGGER_BUSY
    assert "正在运行" in reason


def test_rapid_retrigger_rejected():
    now = datetime.now(timezone.utc)
    state, _ = sync_mod.can_trigger_manual_sync(
        running_run=None, last_manual_at=(now - timedelta(seconds=3)).isoformat(),
        now=now, min_interval_s=30)
    assert state == sync_mod.TRIGGER_TOO_SOON


def test_trigger_allowed_when_idle():
    now = datetime.now(timezone.utc)
    state, _ = sync_mod.can_trigger_manual_sync(
        running_run=None, last_manual_at=(now - timedelta(minutes=5)).isoformat(),
        now=now, min_interval_s=30)
    assert state == sync_mod.TRIGGER_OK


def test_stale_running_run_does_not_block(db):
    """僵死的 running 任务不能永久锁死手动同步。"""
    cfg, d = db
    d.start_sync_run("old", (NOW - timedelta(hours=2)).isoformat(),
                     account="U1", source="IBKR")
    d.mark_sync_run_running("old")
    stale_before = (NOW - timedelta(seconds=600)).isoformat()
    assert d.running_sync_run(stale_before=stale_before) is None


# ---------------- 11 & 12. AI 门禁 ----------------

def test_critical_blocks_ai_warning_does_not():
    crit = dq.evaluate(balances=_bal(equity=None), positions=[], now=NOW,
                       data_as_of=NOW.isoformat(), market_open=True)
    assert crit.ai_analysis_allowed is False

    warn = dq.evaluate(balances=_bal(buying_power=Decimal("-5")), positions=[_pos()],
                       now=NOW, data_as_of=NOW.isoformat(), market_open=True)
    assert warn.warnings and warn.critical == []
    assert warn.ai_analysis_allowed is True      # 仅 warning 不阻断
    assert warn.status == dq.FRESH


def test_api_exposes_ai_gate_and_quality(db):
    """AI 消费方必须能一次拿到 snapshot_id / data_as_of / quality / ai_allowed。"""
    cfg, d = db
    d.write_account_snapshot(
        snapshot_id="s1", ts=datetime.now(timezone.utc).isoformat(), balances=_bal(),
        positions=[_pos()], sync_run_id="r1", quality_status=dq.FRESH,
        ai_analysis_allowed=True,
        issues=[{"issue_code": "QUOTES_DELAYED", "severity": "info",
                 "category": "source", "message": "延迟行情"}])
    c = TestClient(create_app(cfg, d))
    body = c.get("/api/v1/account/snapshot").json()
    assert body["success"] is True
    assert body["request_id"] and body["generated_at"]
    data = body["data"]
    for k in ("snapshot_id", "data_as_of", "quality_status", "ai_analysis_allowed"):
        assert k in data
    assert data["quality"]["issues"][0]["issue_code"] == "QUOTES_DELAYED"
    assert data["net_liquidation"] == "40000"        # 金额以字符串出参，不丢精度


def test_api_marks_stale_snapshot_not_fresh(db):
    """放久了的快照，API 必须按此刻重算成 stale，不能返回落库时的 fresh。"""
    cfg, d = db
    old = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat()
    d.write_account_snapshot(snapshot_id="s1", ts=old, balances=_bal(),
                             positions=[_pos()], sync_run_id="r1",
                             quality_status=dq.FRESH, ai_analysis_allowed=True)
    c = TestClient(create_app(cfg, d))
    data = c.get("/api/v1/account/snapshot").json()["data"]
    assert data["quality_status"] == dq.ERROR        # 3 天前 -> 严重过期
    assert data["ai_analysis_allowed"] is False      # 门禁自动关闭


def test_page_shows_data_as_of_and_state(db):
    cfg, d = db
    d.write_account_snapshot(
        snapshot_id="s1", ts=datetime.now(timezone.utc).isoformat(), balances=_bal(),
        positions=[_pos()], sync_run_id="r1", quality_status=dq.FRESH,
        ai_analysis_allowed=True)
    html = TestClient(create_app(cfg, d)).get("/").text
    assert "数据截至" in html
    assert "数据正常" in html and "✅" in html        # 文字 + 图标，不只靠颜色
    assert "AI 分析" in html


def test_page_warns_when_stale(db):
    cfg, d = db
    old = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat()
    d.write_account_snapshot(snapshot_id="s1", ts=old, balances=_bal(),
                             positions=[_pos()], sync_run_id="r1",
                             quality_status=dq.FRESH, ai_analysis_allowed=True)
    html = TestClient(create_app(cfg, d)).get("/").text
    assert "不可用于可靠分析" in html or "可能已过期" in html
    assert "⛔ 已阻止" in html


def test_manual_trigger_without_sync_capability_is_explicit(db):
    """web 进程不跑同步 —— 必须明确报错，不能假装触发成功。"""
    cfg, d = db
    c = TestClient(create_app(cfg, d))
    r = c.post("/api/v1/sync/trigger")
    assert r.status_code == 503
    assert r.json()["error"]["code"] == "SYNC_UNAVAILABLE"
