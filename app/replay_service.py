"""账户规则历史重放编排。

在历史账户快照上重新执行风险规则，产出**与实时系统完全隔离**的结果。

隔离是本模块的第一约束，具体做法不是"记得别写"，而是**结构上做不到**：
  · 只写 risk_replay_* 四张表，不 import event_service，不持有 Notifier
  · 通过 replay_context.assert_read_only() 在关键位置显式断言
  · 单元测试逐表断言 alert_state / risk_event_registry / risk_notifications 未变

两种规则版本模式，结论完全不同，**绝不混为一谈**：
    point_in_time  —— 用历史时点当时有效的规则版本。回答「当时会不会报警」
    current_rules  —— 用今天的规则跑历史数据。回答「如果这套规则当时就在，会怎样」

防未来数据泄漏贯穿始终：账户上下文只取该时点及之前；持仓不得晚于账户；
市场数据只用该日及之前；现金流按 created_at 判断当时是否已知。
"""
from __future__ import annotations

import hashlib
import json
import time
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from . import cashflow_model as cm
from . import combined_events as ce
from . import replay_context as rctx
from . import replay_identity as rid
from . import risk_engine as re_
from . import risk_metrics as rm
from . import risk_service
from .logging_setup import get_logger

log = get_logger()

CALC_VERSION = "replay-v1/2026-07"

# 采样模式
SAMPLE_DAILY_CLOSE = "daily_close"
SAMPLE_INTRADAY = "intraday_interval"

# 规则版本模式
MODE_POINT_IN_TIME = "point_in_time"
MODE_CURRENT_RULES = "current_rules"

# 任务状态
ST_PENDING = "pending"
ST_RUNNING = "running"
ST_SUCCESS = "success"
ST_PARTIAL = "partial"
ST_FAILED = "failed"

#: 第一版可重放的规则来源。R1–R7 行为纪律依赖成交与用户行为上下文，
#: 单独处理，不在此列。
REPLAYABLE_SOURCES = ("account", "position", "concentration", "combined")

ZERO = Decimal("0")


def _d(v):
    return cm.d(v)


def _s(v):
    return None if v is None else str(v)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def param_hash(params: dict) -> str:
    raw = json.dumps(params, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode()).hexdigest()[:20]


# ---------------- 采样 ----------------

def sample_snapshots(db, *, start: str, end: str, account: str | None,
                     mode: str, interval_minutes: int = 60,
                     include_unattributed: bool = False) -> list[dict]:
    """按模式选出待重放的账户快照。

    **绝不对两万多条分钟快照全量重放** —— 那会产生海量重复事件，
    既跑不动也读不懂。第一版默认 daily_close。
    """
    start_iso = f"{str(start)[:10]}T00:00:00+00:00"
    end_iso = f"{str(end)[:10]}T23:59:59.999999+00:00"
    # account 列是后加的，早期快照为空。默认按账户过滤会静默丢掉那几天；
    # 显式要求时才纳入，并在结果里标注它们是未标注账户的行。
    acct = None if include_unattributed else account
    if mode == SAMPLE_DAILY_CLOSE:
        return db.daily_close_balances(start_iso, end_iso, acct)

    rows = db.balances_in_range(start_iso, end_iso, acct)
    if interval_minutes <= 0:
        return rows
    picked: list[dict] = []
    last_bucket = None
    for r in rows:
        ts = r.get("ts") or ""
        b = _bucket(ts, interval_minutes)
        if b is None:
            continue
        if b != last_bucket:
            picked.append(r)
            last_bucket = b
        else:
            picked[-1] = r        # 桶内取最后一条
    return picked


def _bucket(ts: str, minutes: int) -> str | None:
    try:
        t = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)
    epoch = int(t.timestamp())
    step = minutes * 60
    return datetime.fromtimestamp(epoch - epoch % step,
                                  tz=timezone.utc).isoformat()


# ---------------- 市场对齐 ----------------

def market_at(db, cfg, as_of: str) -> dict:
    """该时点可用的市场环境。

    优先用**当时已落库**的评估；没有就用该日及之前的日线重算；
    历史不足就不给联合结论。**绝不用未来交易日的市场数据判断前一日账户。**
    """
    out = {"market_state": None, "market_data_as_of": None,
           "market_source": "none", "time_gap_seconds": None,
           "market_calculation_version": None, "reliable": False,
           "granularity_note": ("市场数据为日频、账户快照为分钟级，"
                                "两者粒度不同，联合判定按交易日对齐")}
    day = str(as_of or "")[:10]
    if not day:
        return out
    try:
        rows = db.market_score_series("0000-01-01", day)
    except Exception:  # noqa: BLE001
        rows = []
    if rows:
        last = rows[-1]
        out.update({
            "market_state": last.get("market_state"),
            "market_data_as_of": last.get("data_as_of"),
            "market_source": "stored_assessment",
            "market_calculation_version": last.get("calculation_version"),
            "reliable": bool(last.get("reliable")),
        })
        out["time_gap_seconds"] = _gap_seconds(as_of, last.get("data_as_of"))
        return out

    # 退化：用该日及之前的日线现算一个粗粒度状态
    try:
        from . import market_metrics as mm
        bars = db.bars_up_to("QQQ", day, limit=260)
        closes = [b["c"] for b in bars if b.get("c") is not None]
        if len(closes) >= 50:
            ma50 = mm.sma(closes, 50)
            state = "cautious" if (ma50 and closes[-1] < ma50) else "neutral"
            out.update({"market_state": state,
                        "market_data_as_of": bars[-1]["date"],
                        "market_source": "recomputed",
                        "market_calculation_version": "replay-market-v1",
                        "reliable": True})
            out["time_gap_seconds"] = _gap_seconds(as_of, bars[-1]["date"])
    except Exception:  # noqa: BLE001
        pass
    return out


def _gap_seconds(a, b) -> int | None:
    def p(v):
        s = str(v or "")
        if len(s) == 10:
            s += "T00:00:00+00:00"
        try:
            t = datetime.fromisoformat(s.replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return None
        return t if t.tzinfo else t.replace(tzinfo=timezone.utc)
    ta, tb = p(a), p(b)
    if ta is None or tb is None:
        return None
    return int(abs((ta - tb).total_seconds()))


# ---------------- 单点重放 ----------------

def replay_point(cfg, db, ctx: dict, *, rules, rule_set_mode: str,
                 rule_set_version: str | None, now: datetime) -> dict:
    """在一个历史时点上执行规则。**只读、不通知、不建实时事件。**

    隔离不是靠这里断言实现的，而是靠结构：本模块只写 risk_replay_* 四张表，
    不 import event_service、不持有 Notifier。`assert_read_only` 是给
    **真正会产生副作用的位置**用的守卫（如果将来有人把实时链路接进来），
    放在只读函数入口只会每次都抛。测试用逐表计数断言隔离是否真的成立。
    """
    balances = ctx["account_summary"]
    positions = ctx["positions"]
    as_of = ctx["account_data_as_of"]

    rc = _build_historical_context(db, ctx)
    metrics, _ = risk_service.compute_metrics(cfg, rc, db)

    events = re_.evaluate_rules(metrics, rules,
                                quality_status=rc.quality_status)
    sc = re_.score(events, weights=risk_service._weights(cfg),
                   quality_status=rc.quality_status)
    level = re_.risk_level(sc["total_risk_score"], events,
                           reliable=ctx.get("reliable", False))

    mkt = market_at(db, cfg, as_of)
    combined = []
    if mkt.get("market_state") and mkt.get("reliable"):
        try:
            combined = ce.evaluate(
                {"assessment_id": None, "reliable": ctx.get("reliable", False),
                 "calculated_metrics": metrics, "risk_level": level},
                {"assessment_id": None, "reliable": True,
                 "market_state": mkt["market_state"], "triggered_rules": [],
                 "calculated_metrics": {}},
                ce.default_rules(cfg))
        except Exception:  # noqa: BLE001
            combined = []

    return {
        "metrics": metrics, "events": events, "combined": combined,
        "risk_level": level,
        "risk_score": _s(sc.get("total_risk_score")),
        "component_scores": sc.get("component_scores", {}),
        "market": mkt,
        "rule_set_version": rule_set_version,
        "rule_set_mode": rule_set_mode,
    }


def _build_historical_context(db, ctx: dict):
    """把历史上下文装配成 RiskContext。

    与实时 `risk_service.build_context` 同构，但**每一项都只取该时点及之前**：
    净值序列裁到 as_of、不取当前挂单、不取之后的成交。
    """
    from .db import to_decimal
    b = ctx["account_summary"]
    as_of = ctx["account_data_as_of"]
    account = ctx.get("account_id")
    return risk_service.RiskContext(
        snapshot_id=(ctx.get("snapshot_id") or ctx["source_snapshot_key"]),
        account_id=account, data_as_of=as_of,
        net_liquidation=to_decimal(b.get("equity")),
        total_cash=to_decimal(b.get("total_cash")) or to_decimal(b.get("cash")),
        available_funds=to_decimal(b.get("available_funds")),
        buying_power=to_decimal(b.get("buying_power")),
        maintenance_margin=to_decimal(b.get("maint_margin")),
        initial_margin=to_decimal(b.get("init_margin")),
        positions_market_value=to_decimal(b.get("positions_value")),
        base_currency=b.get("currency"),
        balances=b, positions=ctx["positions"],
        # **不取当前挂单** —— 那是「现在」的状态，不是历史时点的
        orders=[], executions=[],
        quality_status=(b.get("quality_status") or "unknown"),
        quality_issues=[],
        ai_analysis_allowed=bool(b.get("ai_analysis_allowed")),
        equity_series=_equity_series_up_to(db, as_of, account),
        recent_exec_symbols=set(), pnl={}, flows_present=False)


def _equity_series_up_to(db, as_of: str, account: str | None) -> list[dict]:
    """截至该时点的净值序列（回撤指标需要）。**绝不含之后的数据。**"""
    from .db import to_decimal
    try:
        rows = db.daily_equity_series(account, None, str(as_of)[:10]) if account else []
    except Exception:  # noqa: BLE001
        return []
    return [{"et_date": r["et_date"],
             "net_liquidation": to_decimal(r["net_liquidation"])} for r in rows]


# ---------------- episode 构建 ----------------

def _episode_identity(run_id: str, rule_code: str, symbol: str | None,
                      account: str | None, mode: str) -> str:
    return "|".join([run_id, rule_code, symbol or "", account or "", mode])


def build_episodes(points: list[dict], *, run_id: str, account: str | None,
                   mode: str, now: datetime) -> list[dict]:
    """把逐点结果串成独立 episode。

    与实时 episode 同样的语义：首次触发打开、持续更新、不再触发关闭、
    再次触发开新一轮。但**数据缺失不等于恢复** —— 不可靠的采样点直接跳过，
    既不延续也不关闭，因为我们不知道那时候到底怎么样。
    """
    open_eps: dict[str, dict] = {}
    closed: list[dict] = []
    seq: dict[str, int] = {}

    for pt in points:
        as_of = pt["data_as_of"]
        if not pt.get("reliable"):
            continue          # 不可靠的点：既不延续也不关闭
        seen: set[str] = set()
        for ev in pt["events"]:
            code = ev.rule_code if hasattr(ev, "rule_code") else ev.get("rule_code")
            sym = (ev.affected_symbol if hasattr(ev, "affected_symbol")
                   else ev.get("affected_symbol"))
            ident = _episode_identity(run_id, code, sym, account, mode)
            seen.add(ident)
            sev = (ev.severity if hasattr(ev, "severity") else ev.get("severity"))
            val = _s(ev.actual_value if hasattr(ev, "actual_value")
                     else ev.get("actual_value"))
            cur = open_eps.get(ident)
            if cur is None:
                seq[ident] = seq.get(ident, 0) + 1
                open_eps[ident] = {
                    "replay_event_id": f"{run_id[:12]}-{_short(ident)}-{seq[ident]:03d}",
                    "replay_run_id": run_id, "identity": ident,
                    "episode_seq": seq[ident], "rule_code": code,
                    "rule_version": pt.get("rule_set_version"),
                    "rule_set_mode": mode,
                    "source_type": _source_of(ev),
                    "category": (ev.category if hasattr(ev, "category")
                                 else ev.get("category")),
                    "affected_symbol": sym, "account_id": account,
                    "title": (ev.title if hasattr(ev, "title") else ev.get("title")),
                    "explanation": (ev.explanation if hasattr(ev, "explanation")
                                    else ev.get("explanation")),
                    "first_detected_at": as_of, "last_detected_at": as_of,
                    "peak_severity": sev, "last_severity": sev,
                    "peak_actual_value": val,
                    "threshold_value": _s(
                        ev.threshold_value if hasattr(ev, "threshold_value")
                        else ev.get("threshold_value")),
                    "unit": (ev.unit if hasattr(ev, "unit") else ev.get("unit")),
                    "occurrence_count": 1,
                    "source_snapshot_keys": [pt["source_snapshot_key"]],
                    "reliable": pt.get("reliable", False),
                    "created_at": now.isoformat(),
                }
            else:
                cur["last_detected_at"] = as_of
                cur["last_severity"] = sev
                cur["occurrence_count"] += 1
                cur["source_snapshot_keys"].append(pt["source_snapshot_key"])
                # 峰值严重度只升不降 —— 用最后值会掩盖期间到达过的 critical
                from .risk_events import severity_rank
                if severity_rank(sev) > severity_rank(cur["peak_severity"]):
                    cur["peak_severity"] = sev
                    cur["peak_actual_value"] = val

        # 本轮未再触发的：关闭
        for ident in list(open_eps):
            if ident in seen:
                continue
            ep = open_eps.pop(ident)
            ep["resolved_at"] = as_of
            closed.append(_finish(ep))

    for ep in open_eps.values():
        closed.append(_finish(ep))          # 区间结束仍未恢复
    return closed


def _finish(ep: dict) -> dict:
    # 显式置 None 而不是留空键 —— 消费方直接取 resolved_at 不该 KeyError
    ep.setdefault("resolved_at", None)
    ep.setdefault("matched_real_event_id", None)
    ep.setdefault("match_lead_minutes", None)
    ep["duration_minutes"] = _minutes(ep["first_detected_at"],
                                      ep.get("resolved_at")
                                      or ep["last_detected_at"])
    ep["source_snapshot_keys"] = json.dumps(
        ep["source_snapshot_keys"][:50], ensure_ascii=False)
    ep["reliable"] = 1 if ep.get("reliable") else 0
    return ep


def _minutes(a, b) -> int | None:
    g = _gap_seconds(a, b)
    return None if g is None else g // 60


def _short(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()[:8]


def _source_of(ev) -> str:
    cat = (ev.category if hasattr(ev, "category") else ev.get("category")) or ""
    if cat == "position":
        return "position"
    if cat == "data_quality":
        return "data_quality"
    if cat == "combined":
        return "combined"
    return "account"


# ---------------- 任务编排 ----------------

def run_replay(cfg, db, *, date_start: str, date_end: str,
               account: str | None = None,
               sampling_mode: str = SAMPLE_DAILY_CLOSE,
               interval_minutes: int = 60,
               rule_set_mode: str = MODE_CURRENT_RULES,
               rules: list[str] | None = None,
               include_unattributed: bool = False,
               reuse: bool = True, now: datetime | None = None) -> dict:
    """执行一次历史重放。**任何异常都不外抛。**

    失败绝不影响实时账户同步、风险评估或飞书告警 —— 重放是离线研究。
    """
    now = now or _now()
    t0 = time.monotonic()
    account = account if account is not None else getattr(cfg, "ib_account", None)

    params = {"account": account, "start": str(date_start)[:10],
              "end": str(date_end)[:10], "sampling": sampling_mode,
              "interval": interval_minutes, "mode": rule_set_mode,
              "rules": sorted(rules or []), "version": CALC_VERSION,
              "unattributed": include_unattributed}
    phash = param_hash(params)

    if reuse:
        try:
            prev = db.find_replay_by_hash(phash)
            if prev:
                log.info("重放参数命中已有结果，复用 %s", prev["replay_run_id"])
                return {"replay_run_id": prev["replay_run_id"], "reused": True,
                        "status": prev["status"]}
        except Exception:  # noqa: BLE001
            pass

    run_id = uuid.uuid4().hex
    try:
        db.create_replay_run({
            "replay_run_id": run_id, "param_hash": phash, "account_id": account,
            "date_start": str(date_start)[:10], "date_end": str(date_end)[:10],
            "sampling_mode": sampling_mode,
            "sampling_interval_minutes": interval_minutes,
            "requested_rules": json.dumps(sorted(rules or []), ensure_ascii=False),
            "rule_set_mode": rule_set_mode, "status": ST_RUNNING,
            "started_at": now.isoformat(),
            "calculation_version": CALC_VERSION, "created_at": now.isoformat()})
    except Exception as exc:  # noqa: BLE001
        log.warning("重放任务创建失败（实时系统不受影响）", exc_info=True)
        return {"replay_run_id": None, "status": ST_FAILED,
                "error": cm.sanitize_error(str(exc))}

    try:
        return _execute(cfg, db, run_id, account=account,
                        date_start=date_start, date_end=date_end,
                        sampling_mode=sampling_mode,
                        interval_minutes=interval_minutes,
                        rule_set_mode=rule_set_mode, rules=rules,
                        include_unattributed=include_unattributed,
                        now=now, t0=t0)
    except Exception as exc:  # noqa: BLE001
        db.finish_replay_run(
            run_id, status=ST_FAILED,
            duration_ms=int((time.monotonic() - t0) * 1000),
            error_message=cm.sanitize_error(str(exc)))
        log.warning("历史重放失败 run=%s（实时系统不受影响）", run_id, exc_info=True)
        return {"replay_run_id": run_id, "status": ST_FAILED,
                "error": cm.sanitize_error(str(exc))}


def _execute(cfg, db, run_id, *, account, date_start, date_end, sampling_mode,
             interval_minutes, rule_set_mode, rules, include_unattributed,
             now, t0) -> dict:
    snapshots = sample_snapshots(db, start=date_start, end=date_end,
                                 account=account, mode=sampling_mode,
                                 interval_minutes=interval_minutes,
                                 include_unattributed=include_unattributed)

    all_rules = re_.default_rules(cfg)
    if rules:
        wanted = set(rules)
        all_rules = [r for r in all_rules if r.rule_code in wanted]
    current_version = re_.rule_set_version(all_rules)

    contexts: list[dict] = []
    skip_reasons: dict[str, int] = {}
    for row in snapshots:
        ctx = rid.resolve_context(db, row)
        if not ctx.get("reliable"):
            reason = ctx.get("skip_reason") or "未知原因"
            key = reason.split("，")[0][:60]
            skip_reasons[key] = skip_reasons.get(key, 0) + 1
        contexts.append(ctx)

    usable = [c for c in contexts if c.get("reliable")]
    coverage = rid.summarize_coverage(contexts)
    unattributed = sum(1 for c in contexts if not c.get("account_id"))
    coverage["unattributed_account_snapshots"] = unattributed
    coverage["include_unattributed"] = include_unattributed
    if unattributed:
        coverage["unattributed_note"] = (
            f"{unattributed} 个采样点的 account 字段为空（该列后加），"
            "已按配置纳入；它们属于本系统同步的唯一账户")

    assessments: list[dict] = []
    rule_results: list[dict] = []
    points: list[dict] = []
    version_missing = 0

    for ctx in usable:
        as_of = ctx["account_data_as_of"]
        # ---- 规则版本模式 ----
        version = current_version
        if rule_set_mode == MODE_POINT_IN_TIME:
            historical = db.rules_version_at(as_of)
            if not historical:
                # 找不到当时的规则版本：**如实标记，绝不悄悄用当前规则冒充**
                version_missing += 1
                version = "insufficient_rule_version"
        try:
            res = replay_point(cfg, db, ctx, rules=all_rules,
                               rule_set_mode=rule_set_mode,
                               rule_set_version=version, now=now)
        except Exception:  # noqa: BLE001
            log.warning("单点重放失败 as_of=%s", as_of, exc_info=True)
            skip_reasons["单点规则执行失败"] = skip_reasons.get(
                "单点规则执行失败", 0) + 1
            continue

        aid = f"{run_id[:12]}-{ctx['source_snapshot_key'][-10:]}"
        assessments.append({
            "replay_assessment_id": aid, "replay_run_id": run_id,
            "account_id": account, "data_as_of": as_of,
            "et_date": str(as_of)[:10],
            "snapshot_id": ctx.get("snapshot_id"),
            "source_snapshot_key": ctx["source_snapshot_key"],
            "source_identity_type": ctx["source_identity_type"],
            "alignment_status": ctx["alignment_status"],
            "alignment_gap_seconds": ctx.get("alignment_gap_seconds"),
            "positions_data_as_of": ctx.get("positions_data_as_of"),
            "risk_level": res["risk_level"], "risk_score": res["risk_score"],
            "reliable": 1 if ctx.get("reliable") else 0,
            "component_scores": json.dumps(
                {k: _s(v) for k, v in (res.get("component_scores") or {}).items()},
                ensure_ascii=False),
            "triggered_rules": json.dumps(
                sorted({e.rule_code for e in res["events"]}), ensure_ascii=False),
            "market_data_as_of": res["market"].get("market_data_as_of"),
            "market_state": res["market"].get("market_state"),
            "market_source": res["market"].get("market_source"),
            "market_time_gap_seconds": res["market"].get("time_gap_seconds"),
            "rule_set_version": version, "rule_set_mode": rule_set_mode,
            "data_quality": ctx.get("data_quality"),
            "created_at": now.isoformat(),
        })

        triggered_codes = {e.rule_code for e in res["events"]}
        for r in all_rules:
            ev = next((e for e in res["events"] if e.rule_code == r.rule_code), None)
            rule_results.append({
                "replay_run_id": run_id, "replay_assessment_id": aid,
                "source_snapshot_key": ctx["source_snapshot_key"],
                "data_as_of": as_of, "rule_code": r.rule_code,
                "triggered": 1 if ev else 0,
                "severity": (ev.severity if ev else None),
                "actual_value": _s(ev.actual_value) if ev else None,
                "threshold_value": _s(ev.threshold_value) if ev else None,
                "affected_symbol": (ev.affected_symbol if ev else None),
                "rule_version": version, "created_at": now.isoformat(),
            })

        points.append({
            "data_as_of": as_of,
            "source_snapshot_key": ctx["source_snapshot_key"],
            "reliable": ctx.get("reliable", False),
            "events": list(res["events"]) + _combined_as_events(res["combined"]),
            "rule_set_version": version,
        })

    episodes = build_episodes(points, run_id=run_id, account=account,
                             mode=rule_set_mode, now=now)

    db.save_replay_assessments(assessments)
    db.save_replay_rule_results(rule_results)
    db.save_replay_events(episodes)

    status = ST_SUCCESS
    if not usable:
        status = ST_FAILED
    elif skip_reasons or version_missing:
        status = ST_PARTIAL

    db.finish_replay_run(
        run_id, status=status,
        duration_ms=int((time.monotonic() - t0) * 1000),
        source_snapshot_count=len(snapshots),
        eligible_snapshot_count=len(usable),
        skipped_snapshot_count=len(contexts) - len(usable),
        assessment_count=len(assessments), event_count=len(episodes),
        rule_result_count=len(rule_results),
        coverage_summary=json.dumps(coverage, ensure_ascii=False),
        skip_reasons=json.dumps(skip_reasons, ensure_ascii=False),
        error_message=(f"{version_missing} 个时点找不到当时的规则版本，"
                       "已标记 insufficient_rule_version（未用当前规则冒充）"
                       if version_missing else None))

    log.info("历史重放 %s：采样 %d，可用 %d，评估 %d，episode %d",
             status, len(snapshots), len(usable), len(assessments), len(episodes))
    return {
        "replay_run_id": run_id, "status": status, "reused": False,
        "sampling_mode": sampling_mode, "rule_set_mode": rule_set_mode,
        "source_snapshot_count": len(snapshots),
        "eligible_snapshot_count": len(usable),
        "skipped_snapshot_count": len(contexts) - len(usable),
        "assessment_count": len(assessments), "event_count": len(episodes),
        "rule_result_count": len(rule_results),
        "coverage": coverage, "skip_reasons": skip_reasons,
        "rule_version_missing": version_missing,
        "duration_ms": int((time.monotonic() - t0) * 1000),
    }


def _combined_as_events(combined) -> list:
    """联合事件转成与 RiskEvent 同构的轻量对象，便于 episode 构建统一处理。"""
    out = []
    for c in combined or []:
        out.append(type("ReplayCombined", (), {
            "rule_code": c.rule_code, "severity": c.severity,
            "title": c.title, "explanation": c.explanation,
            "actual_value": c.actual_value, "threshold_value": c.threshold_value,
            "unit": c.unit, "affected_symbol": c.affected_symbol,
            "category": "combined",
        })())
    return out


def coverage_report(cfg, db, account: str | None = None) -> dict:
    """重放覆盖率。回答「能重放多少、为什么不能重放更多」。"""
    account = account if account is not None else getattr(cfg, "ib_account", None)
    stats = db.replay_snapshot_stats(account)

    market = {"start": None, "end": None, "days": 0}
    try:
        bars = db.get_daily_bars("QQQ", limit=100000)
        if bars:
            market = {"start": bars[0]["date"], "end": bars[-1]["date"],
                      "days": len(bars)}
    except Exception:  # noqa: BLE001
        pass

    flex = {"report_from": None, "report_to": None}
    try:
        run = db.latest_usable_flex_run()
        if run:
            flex = {"report_from": run.get("report_from"),
                    "report_to": run.get("report_to")}
    except Exception:  # noqa: BLE001
        pass

    upnl_cov = {"days": 0, "start": None, "end": None}
    try:
        from . import unrealized_pnl as u
        rows = db.daily_unrealized("0000-01-01", "9999-12-31", account=account,
                                   version=u.CALC_VERSION)
        if rows:
            upnl_cov = {"days": len(rows), "start": rows[0]["et_date"],
                        "end": rows[-1]["et_date"]}
    except Exception:  # noqa: BLE001
        pass

    unattributed = {}
    try:
        unattributed = db.unattributed_snapshot_stats()
    except Exception:  # noqa: BLE001
        unattributed = {}

    replayable = len([r for r in re_.default_rules(cfg)
                      if r.category in ("account", "concentration", "position",
                                        "leverage", "liquidity", "drawdown",
                                        "loss")])
    total = stats["balances_total"] or 1
    return {
        "snapshot_range": stats["snapshot_range"],
        "distinct_days": stats["distinct_days"],
        "balances_total": stats["balances_total"],
        "native_snapshot_id_count": stats["native_snapshot_id"],
        "native_snapshot_id_ratio": f"{stats['native_snapshot_id']}/{total}",
        "native_range": stats["native_range"],
        "reconstructable_count": stats["reconstructable"],
        "reconstructable_ratio": f"{stats['reconstructable']}/{total}",
        "unpairable_count": stats["unpairable"],
        "unattributed_account": unattributed,
        "unattributed_note": (
            (f"{unattributed.get('count')} 条快照的 account 字段为空"
             f"（{(unattributed.get('start') or '')[:10]} → "
             f"{(unattributed.get('end') or '')[:10]}，"
             f"{unattributed.get('distinct_days')} 个自然日）。"
             "该列是后来才加的。默认按账户过滤会跳过这几天；"
             "如需纳入，重放时传 include_unattributed=true。")
            if unattributed.get("count") else None),
        "market_coverage": market,
        "flex_coverage": flex,
        "unrealized_pnl_coverage": upnl_cov,
        "replayable_rule_count": replayable,
        "total_rule_count": len(re_.default_rules(cfg)),
        "note": ("R1–R7 行为纪律规则依赖成交与用户行为上下文，"
                 "第一版不纳入重放。"),
    }
