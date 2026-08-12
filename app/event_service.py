"""风险事件运营层编排：评估结果 -> 事件生命周期 -> 状态历史 -> 通知。

**通知出口分工**（这是本层最重要的一条设计约束）：
    R1–R7 行为规则   -> 既有 engine._dispatch_alerts 链路，运营层只登记不推送
    账户/市场/联合   -> 本层推送
两套规则码命名空间不相交，同一风险不会被推两次。alerting.py 的状态机语义被
两条链路共用，不存在「两套互相矛盾的告警系统」。

三条不可让步的行为：
1. **通知失败绝不影响风险评估**。整层用异常全吞包裹，失败只落库记录。
2. **发送失败不得记成已通知**。否则冷静期会把下一次真实告警吞掉。
3. **数据缺失不判恢复**。评估不完整时事件维持原状。
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone

from . import combined_events as ce
from . import notification_format as nf
from . import notification_policy as npol
from . import risk_events as rev
from .logging_setup import get_logger

log = get_logger()


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ---------------- 事件生命周期 ----------------

def register_triggers(db, triggers: list[rev.Trigger], *,
                      now: datetime | None = None,
                      evaluation_complete: bool = True,
                      reliable: bool = True,
                      scope: dict | None = None) -> list[tuple]:
    """把本轮触发登记进事件注册表，并结算未触发的事件。

    scope 限定「本轮评估覆盖了哪些来源」。只有在 scope 内的既有事件才会被判恢复 ——
    只跑了市场评估却把账户事件全判恢复，是最容易犯的一类错。

    返回 [(LifecycleDecision, trigger, event_row_before), ...]，供通知层决策。
    """
    now = now or _now()
    now_iso = now.isoformat()
    scope = scope or {}
    source_types = scope.get("source_types")
    out: list[tuple] = []
    seen_identities: set[str] = set()

    for trig in triggers:
        try:
            existing = db.latest_event_for_identity(trig.identity)
            dec = rev.decide_upsert(existing, trig, now_iso=now_iso)
            seen_identities.add(trig.identity)
            _apply(db, dec, trig, existing, now_iso)
            out.append((dec, trig, existing))
        except Exception:  # noqa: BLE001 — 单个事件登记失败不影响其余
            log.warning("事件登记失败 rule=%s", trig.rule_code, exc_info=True)

    # ---- 结算：本轮未再触发的事件 ----
    if evaluation_complete and reliable and source_types:
        try:
            open_rows, _ = db.query_risk_events(
                source_type=list(source_types), status=list(rev.ACTIVE_STATUSES),
                limit=1000)
        except Exception:  # noqa: BLE001
            open_rows = []
        for row in open_rows:
            if row.get("identity") in seen_identities:
                continue
            if (row.get("notification_owner") or "ops") in ("legacy", "technical"):
                continue          # R1–R7 的恢复由 engine 负责；技术面从不推送
            dec = rev.decide_resolve(row, evaluation_complete=True, reliable=True)
            if dec is None:
                continue
            try:
                db.resolve_risk_event(dec.event_id, now_iso)
                db.record_event_transition({
                    "event_id": dec.event_id, "transition": dec.transition,
                    "previous_status": dec.previous_status,
                    "new_status": dec.new_status,
                    "previous_severity": dec.previous_severity,
                    "new_severity": dec.new_severity,
                    "actual_value": row.get("actual_value"),
                    "threshold_value": row.get("threshold_value"),
                    "reason": dec.reason, "transitioned_at": now_iso})
                out.append((dec, None, row))
            except Exception:  # noqa: BLE001
                log.warning("事件恢复登记失败 %s", dec.event_id, exc_info=True)
    return out


def _apply(db, dec, trig: rev.Trigger, existing: dict | None, now_iso: str) -> None:
    if dec.action == "create":
        db.create_risk_event({
            "event_id": dec.event_id, "identity": dec.identity,
            "episode_seq": dec.episode_seq, "source_type": trig.source_type,
            "event_code": trig.event_code, "rule_code": trig.rule_code,
            "rule_version": trig.rule_version,
            "rule_set_version": trig.rule_set_version, "category": trig.category,
            "severity": dec.new_severity, "max_severity": dec.new_severity,
            "status": dec.new_status, "title": trig.title,
            "explanation": trig.explanation, "actual_value": trig.actual_value,
            "threshold_value": trig.threshold_value, "unit": trig.unit,
            "affected_symbol": trig.affected_symbol,
            "affected_instrument": trig.affected_instrument,
            "account_id": trig.account_id, "snapshot_id": trig.snapshot_id,
            "assessment_id": trig.assessment_id, "data_as_of": trig.data_as_of,
            "first_detected_at": now_iso, "last_detected_at": now_iso,
            "occurrence_count": 1, "recommended_check": trig.recommended_check,
            "quality_status": trig.quality_status, "reliable": trig.reliable,
            "notification_owner": trig.notification_owner,
            "rule_set_hash": trig.rule_set_hash,
            "market_assessment_id": trig.market_assessment_id,
            "calculation_version": trig.calculation_version,
            "created_at": now_iso, "updated_at": now_iso})
    else:
        prev_max = rev.normalize_severity((existing or {}).get("max_severity")
                                          or (existing or {}).get("severity"))
        new_max = (dec.new_severity
                   if rev.severity_rank(dec.new_severity) > rev.severity_rank(prev_max)
                   else prev_max)
        db.touch_risk_event(
            dec.event_id, now_iso=now_iso, severity=dec.new_severity,
            status=dec.new_status, actual_value=trig.actual_value,
            threshold_value=trig.threshold_value, data_as_of=trig.data_as_of,
            assessment_id=trig.assessment_id, snapshot_id=trig.snapshot_id,
            quality_status=trig.quality_status, reliable=trig.reliable,
            max_severity=new_max)

    # 只有真正的状态/严重度变化才记一条转换；持续触发不写，否则历史表会被灌满
    if dec.transition != rev.TRANS_UPDATED:
        db.record_event_transition({
            "event_id": dec.event_id, "transition": dec.transition,
            "previous_status": dec.previous_status, "new_status": dec.new_status,
            "previous_severity": dec.previous_severity,
            "new_severity": dec.new_severity, "actual_value": trig.actual_value,
            "threshold_value": trig.threshold_value, "reason": dec.reason,
            "transitioned_at": now_iso})


# ---------------- 通知派发 ----------------

def dispatch(db, notifier, results: list[tuple], *, cfg,
             now: datetime | None = None,
             account_state: dict | None = None,
             in_market_hours: bool = True) -> dict:
    """按策略决定并发送通知。**整体异常全吞** —— 通知失败不能拖垮风险评估。"""
    now = now or _now()
    summary = {"sent": 0, "skipped": 0, "failed": 0, "batches": 0, "reasons": {}}
    try:
        policy = npol.policy_from_config(cfg)
        db.expire_suppressions(now.isoformat())

        sent_hour = db.count_notifications_since(
            (now - timedelta(hours=1)).isoformat())
        sent_day = db.count_notifications_since(
            (now - timedelta(days=1)).isoformat())

        pending = []
        for dec, trig, row_before in results:
            current = db.get_risk_event(dec.event_id) or {}
            nd = npol.decide(dec, current, policy=policy, now=now,
                             sent_last_hour=sent_hour, sent_today=sent_day,
                             in_market_hours=in_market_hours)
            if not nd.should:
                summary["skipped"] += 1
                summary["reasons"][nd.reason] = summary["reasons"].get(nd.reason, 0) + 1
                continue
            pending.append((dec, nd, trig or _pseudo_trigger(current), current))

        if not pending:
            return summary

        groups = (npol.group_for_merge(pending) if policy.merge_batch
                  else [[p] for p in pending])
        for group in groups:
            ok = _send_group(db, notifier, group, cfg=cfg, now=now,
                             policy=policy, account_state=account_state)
            summary["batches"] += 1
            if ok:
                summary["sent"] += len(group)
            else:
                summary["failed"] += len(group)
    except Exception:  # noqa: BLE001 — 通知层任何异常都不外抛
        log.warning("通知派发异常（风险评估不受影响）", exc_info=True)
    return summary


def _pseudo_trigger(row: dict) -> rev.Trigger:
    """恢复通知没有 trigger（本轮规则未触发），用事件行还原一个用于分组。"""
    return rev.Trigger(
        source_type=row.get("source_type") or rev.SRC_ACCOUNT,
        event_code=row.get("event_code") or "", rule_code=row.get("rule_code") or "",
        category=row.get("category") or "", severity=row.get("severity") or "warning",
        title=row.get("title") or "", explanation=row.get("explanation") or "")


def _send_group(db, notifier, group: list, *, cfg, now, policy,
                account_state) -> bool:
    """发送一组（合并）通知，带有限重试。返回是否最终成功。"""
    rows = [g[3] for g in group]
    ntype = group[0][1].notification_type
    event_ids = [g[0].event_id for g in group]
    tz = getattr(cfg, "local_tz", timezone.utc)
    url = getattr(cfg, "dashboard_url", None)

    title, body = nf.format_batch(rows, notification_type=ntype, tz=tz,
                                  dashboard_url=url, account_state=account_state)

    key = (npol.batch_dedupe_key(event_ids, ntype) if len(event_ids) > 1
           else npol.dedupe_key(event_ids[0], ntype))
    nid = uuid.uuid4().hex
    attempted = now.isoformat()
    ok, err_code, err_msg, retries = False, None, None, 0

    for attempt in range(policy.max_retries + 1):
        retries = attempt
        try:
            ok = bool(notifier.send(title, body))
        except Exception as exc:  # noqa: BLE001
            ok, err_code, err_msg = False, type(exc).__name__, str(exc)[:200]
        if ok:
            err_code = err_msg = None
            break
        if err_code is None:
            err_code, err_msg = "SEND_FAILED", "推送通道返回失败"
        if attempt < policy.max_retries:
            log.info("通知发送失败，重试 %d/%d", attempt + 1, policy.max_retries)

    db.record_notification({
        "notification_id": nid,
        "event_id": event_ids[0],
        "event_ids": json.dumps(event_ids, ensure_ascii=False),
        "channel": "feishu", "notification_type": ntype,
        "severity": max((r.get("severity") for r in rows),
                        key=lambda s: rev.severity_rank(s), default="warning"),
        "title": title, "payload_summary": nf.payload_summary(title, body),
        "dedupe_key": key, "attempted_at": attempted,
        "sent_at": now.isoformat() if ok else None,
        "success": ok, "error_code": err_code, "error_message": err_msg,
        "retry_count": retries})

    # 只有真的发出去了才推进「已通知」水位 —— 失败记成成功会吞掉下次告警
    if ok:
        for (dec, _nd, _trig, row) in group:
            db.mark_event_notified(dec.event_id, now_iso=now.isoformat(),
                                   severity=dec.new_severity,
                                   value=row.get("actual_value"))
    else:
        log.error("通知最终发送失败（已重试 %d 次）：%s", retries, title)
    return ok


# ---------------- 对外入口 ----------------

def _collect(cfg, db, triggers, sources, stats, account, market,
             legacy_hits, account_id, now_iso=None):
    """把评估结果收集成 Trigger。**纯函数** —— 预览与真实执行共用这一段。"""
    from datetime import datetime as _dtc, timezone as _tzc
    now_iso = now_iso or _dtc.now(_tzc.utc).isoformat()
    if account:
        for ev in account.get("events") or []:
            triggers.append(rev.from_account_event(
                ev, assessment=account, account_id=account_id))
        sources |= {rev.SRC_ACCOUNT, rev.SRC_POSITION, rev.SRC_DATA_QUALITY}
    if market:
        for ev in market.get("events") or []:
            triggers.append(rev.from_market_event(ev, assessment=market))
        sources |= {rev.SRC_MARKET, rev.SRC_DATA_QUALITY}

    # 联合事件
    if account and market:
        rules = ce.default_rules(cfg)
        version = ce.rule_set_version(rules)
        by_code = {r.rule_code: r for r in rules}
        for cev in ce.evaluate(account, market, rules):
            rule = by_code.get(cev.rule_code)
            triggers.append(rev.Trigger(
                source_type=rev.SRC_COMBINED, event_code=cev.event_code,
                rule_code=cev.rule_code, category=cev.category,
                severity=cev.severity, title=cev.title,
                explanation=cev.explanation, actual_value=cev.actual_value,
                threshold_value=cev.threshold_value, unit=cev.unit,
                affected_symbol=cev.affected_symbol,
                recommended_check=cev.recommended_check,
                # **单条规则版本与规则集版本都要落库** —— 只存规则集版本时，
                # 「这条联合规则当时是什么定义」在事件登记里查不到。
                rule_version=(ce.rule_version(rule) if rule else None),
                rule_set_version=version,
                rule_set_hash=ce.rule_set_hash(rules),
                market_assessment_id=market.get("assessment_id"),
                calculation_version=ce.CALC_VERSION,
                assessment_id=cev.account_assessment_id,
                snapshot_id=account.get("snapshot_id"),
                data_as_of=account.get("data_as_of"),
                quality_status=account.get("quality_status"),
                reliable=True, account_id=account_id))
            stats["combined"] += 1
        sources.add(rev.SRC_COMBINED)

    # R1–R7：只登记，不通知
    for hit in (legacy_hits or []):
        triggers.append(rev.from_legacy_hit(
            hit, account_id=account_id, now_iso=now_iso))
    if legacy_hits is not None:
        sources.add(rev.SRC_BEHAVIOR)



_TREND_CODE = {"强多头": "TREND_STRONG", "多头趋势": "TREND_BULL", "回踩趋势": "TREND_PULLBACK",
               "均线纠缠": "TREND_TANGLE", "短线转弱": "TREND_SOFT",
               "中期趋势破坏": "TREND_BROKEN", "长期弱势": "TREND_WEAK", "中性": "TREND_NEUTRAL"}
_TREND_SEV = {"中期趋势破坏": "high", "长期弱势": "high"}


def _trend_trigger(card: dict, account_id, now_iso: str):
    """日线趋势状态本身作为一条技术事件：rule_code 编码状态，状态一变即旧恢复/新触发，
    时间线据此呈现「状态变化」而非每轮轮询。"""
    st = card.get("daily_trend_state")
    sym = card.get("symbol")
    code = _TREND_CODE.get(st, "TREND")
    return rev.Trigger(
        source_type=rev.SRC_TECHNICAL,
        event_code=f"technical:{sym}:{code}",
        rule_code=code,
        category="technical",
        severity=_TREND_SEV.get(st, "warning"),
        title=f"{sym} · {st}",
        explanation=card.get("technical_summary") or st,
        actual_value=str(st or ""),
        affected_symbol=sym,
        rule_version=card.get("calculation_version"),
        rule_set_version=card.get("calculation_version"),
        data_as_of=now_iso,
        calculation_version=card.get("calculation_version"),
        reliable=True,
        account_id=account_id,
        notification_owner="technical",
    )


def _technical_trigger(rule: dict, card: dict, account_id, now_iso: str):
    sev = {"info": "warning", "warn": "high", "crit": "critical"}.get(
        rule.get("severity"), "warning")
    sym = card.get("symbol")
    return rev.Trigger(
        source_type=rev.SRC_TECHNICAL,
        event_code=f"technical:{sym}:{rule['code']}",
        rule_code=rule["code"],
        category="technical",
        severity=sev,
        title=f"{sym} · {rule['label']}",
        explanation=rule["summary"],
        actual_value=str(card.get("daily_trend_state") or ""),
        affected_symbol=sym,
        rule_version=card.get("calculation_version"),
        rule_set_version=card.get("calculation_version"),
        data_as_of=now_iso,
        calculation_version=card.get("calculation_version"),
        reliable=True,
        account_id=account_id,
        notification_owner="technical",
    )


def register_technical_events(cfg, db, *, now: datetime | None = None) -> dict:
    """把技术面 T1–T8 命中登记进事件生命周期（技术触发时间线用）。

    **与账户/市场链路隔离**：scope 只含 SRC_TECHNICAL，绝不会结算其它来源的事件；
    也不调用 dispatch —— 技术信号只进时间线，从不推送。数据不全时置 reliable=False，
    避免把「这轮没算」误判成「风险已恢复」。任何异常都不外抛。
    """
    from decimal import Decimal

    from . import market_hours, technical
    from .db import to_decimal

    now = now or _now()
    try:
        session = market_hours.market_session(now, cfg.market_tz)
        positions = db.latest_positions()
        stops = {r["symbol"]: r["stop_price"] for r in db.latest_stop_levels()}
        account_id = getattr(cfg, "ib_account", None)
        triggers: list = []
        evaluated = skipped = 0
        for r in positions:
            if (to_decimal(r["qty"]) or Decimal("0")) == 0:
                continue
            bars = db.get_daily_bars(r["symbol"], limit=600)
            conf = technical.confirmation_basis(
                session, price_source=r["price_source"], quote_source=r["source"])
            card = technical.compute_symbol(
                r["symbol"], bars, to_decimal(r["price"]),
                stop_price=stops.get(r["symbol"]), confirmation=conf)
            if not bars or card["daily_trend_state"] is None:
                skipped += 1                      # 数据不足：不参与结算
                continue
            evaluated += 1
            triggers.append(_trend_trigger(card, account_id, now.isoformat()))
            for rule in technical.evaluate_rules(card, confirmation=conf):
                triggers.append(_technical_trigger(rule, card, account_id, now.isoformat()))
        if evaluated == 0:
            return {"triggers": 0, "skipped": skipped}
        # 有标的没算出来就不结算，宁可让旧技术事件多留一轮，也不误判恢复
        reliable = skipped == 0
        results = register_triggers(
            db, triggers, now=now, evaluation_complete=True, reliable=reliable,
            scope={"source_types": [rev.SRC_TECHNICAL]})
        return {"triggers": len(triggers), "events": len(results), "skipped": skipped}
    except Exception:  # noqa: BLE001
        log.warning("技术面事件登记异常（不影响同步）", exc_info=True)
        return {"triggers": 0}


def build_triggers(cfg, db, *, account: dict | None, market: dict | None,
                   legacy_hits: list | None = None) -> tuple[list, set, dict]:
    """把评估结果转成 Trigger 列表。**纯函数，不写任何表。**

    从 process_assessments 里抽出来，好让只读预览与真实执行共用同一段逻辑 ——
    预览与执行走两套代码，预览就失去了意义。
    """
    triggers: list[rev.Trigger] = []
    sources: set[str] = set()
    stats = {"triggers": 0, "combined": 0}
    account_id = getattr(cfg, "ib_account", None)
    _collect(cfg, db, triggers, sources, stats, account, market,
             legacy_hits, account_id)
    stats["triggers"] = len(triggers)
    return triggers, sources, stats


def preview_assessments(cfg, db, *, account: dict | None, market: dict | None,
                        now: datetime | None = None,
                        legacy_hits: list | None = None) -> dict:
    """**只读预览**：如果现在执行，会发生什么。

    跑完整规则、算出每条 Trigger 将导致的 transition，但——
      · 不写 risk_event_registry
      · 不写 risk_event_transitions
      · 不开关任何 episode
      · 不碰 alert_state
      · 不调用 notifier

    这是排查问题时该用的工具。事故的教训是：`notifier=None` 不等于只读，
    因为事件登记与 episode 开关根本不经过 notifier。
    """
    now = now or _now()
    now_iso = now.isoformat()
    triggers, sources, stats = build_triggers(
        cfg, db, account=account, market=market, legacy_hits=legacy_hits)

    planned: list[dict] = []
    for trig in triggers:
        identity = trig.identity
        try:
            existing = db.latest_event_for_identity(identity)
        except Exception:  # noqa: BLE001
            existing = None
        dec = rev.decide_upsert(existing, trig, now_iso=now_iso)
        planned.append({
            "rule_code": trig.rule_code, "source_type": trig.source_type,
            "identity": identity, "action": dec.action,
            "transition": getattr(dec, "transition", None),
            "new_severity": dec.new_severity, "new_status": dec.new_status,
            "episode_seq": dec.episode_seq,
            "existing_event_id": (existing or {}).get("event_id"),
        })

    # 会被判为恢复的事件 —— 事故里正是这一步出的问题，预览必须显式列出
    would_resolve = []
    try:
        active, _ = (db.query_risk_events(
            source_type=sorted(sources), status=list(rev.ACTIVE_STATUSES),
            limit=500) if sources else ([], 0))
    except Exception:  # noqa: BLE001
        active = []
    fired = {p["identity"] for p in planned}
    for e in active:
        if e.get("identity") not in fired:
            would_resolve.append({
                "event_id": e.get("event_id"), "rule_code": e.get("rule_code"),
                "source_type": e.get("source_type"),
                "episode_seq": e.get("episode_seq"),
                "warning": "本轮未触发 -> 将被判定为恢复",
            })

    return {
        "read_only": True, "evaluated_at": now_iso,
        "sources": sorted(sources), "trigger_count": len(triggers),
        "planned_transitions": planned,
        "would_resolve": would_resolve,
        "would_resolve_count": len(would_resolve),
        "stats": stats,
        "note": ("这是只读预览，未写入任何表。would_resolve 里的事件如果"
                 "并非真的恢复，说明传入的评估上下文不完整。"),
    }


def process_assessments(cfg, db, notifier, *, account: dict | None,
                        market: dict | None, now: datetime | None = None,
                        legacy_hits: list | None = None,
                        in_market_hours: bool = True,
                        execution_context=None) -> dict:
    """一轮完整的运营层处理。任何异常都不外抛。

    **必须显式传 execution_context** —— 没有它一律拒绝写入。
    这不是形式主义：一次「只读探测」误调本函数，让 7 条仍在触发的事件被判
    恢复并误发了告警。`notifier=None` 当时被当作只读的证据，但事件登记与
    episode 开关根本不经过 notifier。
    """
    from . import execution_context as ec

    ctx = ec.require(execution_context, "运营层写入")
    exec_id = ec.start_audit(db, ctx, now=now)
    now = now or _now()
    stats = {"triggers": 0, "combined": 0, "notify": {},
             "execution_id": exec_id, "source": ctx.source}
    try:
        triggers: list[rev.Trigger] = []
        sources: set[str] = set()
        account_id = getattr(cfg, "ib_account", None)

        _collect(cfg, db, triggers, sources, stats, account,
                 market, legacy_hits, account_id, now.isoformat())

        stats["triggers"] = len(triggers)
        reliable = bool((account or {}).get("reliable", True)
                        and (market or {}).get("reliable", True))
        results = register_triggers(
            db, triggers, now=now, evaluation_complete=True, reliable=reliable,
            scope={"source_types": sorted(sources)})

        state = {
            "account_risk_level": (account or {}).get("risk_level"),
            "market_state": (market or {}).get("market_state"),
        }
        stats["notify"] = dispatch(db, notifier, results, cfg=cfg, now=now,
                                   account_state=state,
                                   in_market_hours=in_market_hours)
        err = None
    except Exception as exc:  # noqa: BLE001
        err = str(exc)[:300]
        log.warning("风险事件运营层处理异常（不影响数据同步）", exc_info=True)

    # 收尾审计：这一轮到底动了多少事件、发了多少通知。
    # **有副作用的入口必须留下完整痕迹** —— 事故之后这是最基本的可追溯性。
    notify = stats.get("notify") or {}
    ec.finish_audit(
        db, exec_id,
        affected_event_count=int(stats.get("triggers") or 0),
        notification_count=int(notify.get("sent") or notify.get("count") or 0),
        error=err)
    return stats


def send_test_notification(cfg, db, notifier, *, now: datetime | None = None) -> dict:
    """发送一条**明确标记为测试**的通知，用于线上验证推送链路。

    标记不能省：用户看到一条没有「测试」二字的风险告警，会立刻去查账户。
    """
    now = now or _now()
    tz = getattr(cfg, "local_tz", timezone.utc)
    row = {
        "title": "推送链路自检", "severity": "warning", "source_type": "data_quality",
        "rule_code": "OPS_TEST", "explanation": "验证飞书推送链路可用，与账户风险无关。",
        "recommended_check": "无需操作。看到本条即说明通知链路正常。",
        "first_detected_at": now.isoformat(), "data_as_of": now.date().isoformat(),
        "reliable": True, "rule_set_version": "ops-test",
    }
    title, body = nf.format_single(
        row, notification_type=npol.N_TEST, tz=tz,
        dashboard_url=getattr(cfg, "dashboard_url", None), is_test=True)
    nid = uuid.uuid4().hex
    ok = False
    try:
        ok = bool(notifier.send(title, body))
    except Exception as exc:  # noqa: BLE001
        log.warning("测试通知发送异常：%s", exc)
    db.record_notification({
        "notification_id": nid, "event_id": None, "event_ids": "[]",
        "channel": "feishu", "notification_type": npol.N_TEST,
        "severity": "warning", "title": title,
        "payload_summary": nf.payload_summary(title, body),
        "dedupe_key": f"test:{nid[:8]}", "attempted_at": now.isoformat(),
        "sent_at": now.isoformat() if ok else None, "success": ok,
        "error_code": None if ok else "SEND_FAILED",
        "error_message": None if ok else "推送通道返回失败", "retry_count": 0})
    return {"success": ok, "notification_id": nid, "title": title}
