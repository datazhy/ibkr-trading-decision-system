"""每日风险简报编排：数据就绪 → 上下文 → 生成 → 校验 → 落库 → 推送。

编排层的三条硬约束：

1. **AI 失败不得影响账户同步与实时告警。** 本模块的任何异常都在这里被吞掉并
   记录，绝不向上冒泡打断调度器里的其他任务。
2. **同一份简报不重复推送。** 幂等键是 (日期, 账户, context_hash, prompt 版本)；
   推送去重键是 context_hash。手动 regenerate 默认**不**重复推飞书。
3. **飞书失败不影响简报落库。** 先落库，再推送；推送结果单独记一行。

生成时机不靠固定时间，而是检查数据依赖是否就绪：账户日终快照、市场数据、
风险评估、Flex（有则用最新 T+1）。没就绪就有限次等待，超过上限出 limited 简报 ——
**绝不无限等待**，那会让某天永远没有简报。
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone

from . import brief_context as bc
from . import brief_generator as bg
from . import brief_prompt as bp

log = logging.getLogger("brief.service")

TRIGGER_SCHEDULED = "scheduled"
TRIGGER_MANUAL = "manual"
TRIGGER_REGENERATE = "regenerate"

#: 数据未就绪时的最大等待次数。超过就出 limited 简报，不再等。
MAX_WAIT_ATTEMPTS = 3

#: 账户数据「足够新」的年龄上限（小时）。就绪判定用年龄而非日期字符串 ——
#: 简报生成时刻（北京 06:30）与快照时间戳（UTC）本来就不在同一个自然日。
FRESH_ENOUGH_HOURS = 12

#: 手动触发的最小间隔（秒）—— 防止连点把 AI 额度打光
MIN_MANUAL_INTERVAL_SECONDS = 300

#: 生成互斥锁名
_LOCK = "daily_brief"

#: 推送渠道。**测试发送用独立 channel** —— 与正式简报共用去重键会让
#: 一次链路测试把当天的正式 06:30 推送吃掉，那正是最不该发生的事。
CHANNEL_FEISHU = "feishu"
CHANNEL_TEST = "feishu_test"

TEST_TITLE_PREFIX = "【测试】"
TEST_LEAD = ("这是每日风险简报推送链路测试，不代表账户刚刚出现新的风险。")


# ======================= 数据就绪 =======================

def check_readiness(cfg, db, *, brief_date: str, now: datetime) -> dict:
    """检查生成简报所需的数据依赖是否就绪。

    返回 {ready, checks, missing}。**未就绪不等于不能生成** ——
    它只决定是否值得再等一会儿。
    """
    checks: list[dict] = []
    missing: list[str] = []

    def add(name: str, ok: bool, detail: str, required: bool = True):
        checks.append({"item": name, "ready": ok, "detail": detail,
                       "required": required})
        if required and not ok:
            missing.append(name)

    day = str(brief_date)[:10]

    try:
        row = db.latest_assessment(getattr(cfg, "ib_account", None))
    except Exception:  # noqa: BLE001
        row = None
    if row is None:
        add("risk_assessment", False, "没有任何风险评估")
    else:
        as_of = str(dict(row).get("data_as_of") or "")
        # **按数据年龄判断，不按日期字符串比较。**
        # 简报在北京 06:30 生成，此时北京日期已是 D+1，而快照时间戳（UTC）
        # 仍是 D —— 拿日期串比大小会让每次调度都判「未就绪」，
        # 然后白等三轮再降级成 limited。年龄才是真正要问的问题。
        t = bc._dt(as_of)
        age_h = None if t is None else (now - t).total_seconds() / 3600
        add("risk_assessment",
            age_h is not None and age_h <= FRESH_ENOUGH_HOURS,
            (f"最新风险评估 {as_of[:19]}"
             + (f"（{age_h:.1f} 小时前）" if age_h is not None else "（时间缺失）")))
        add("account_snapshot", bool(dict(row).get("snapshot_id")),
            f"快照 {str(dict(row).get('snapshot_id') or '')[:12]}")

    try:
        m = db.latest_market_assessment()
    except Exception:  # noqa: BLE001
        m = None
    if m is None:
        add("market_assessment", False, "没有市场环境评估", required=False)
    else:
        as_of = str(dict(m).get("data_as_of") or "")
        # 市场数据是日线，落后一个交易日是正常的，不作为必需项
        add("market_assessment", True, f"市场评估 {as_of[:19]}", required=False)

    try:
        f = db.latest_usable_flex_run()
    except Exception:  # noqa: BLE001
        f = None
    add("flex_report", f is not None,
        (f"Flex 报表覆盖至 {dict(f).get('report_to')}" if f
         else "没有可用的 Flex 报表（T+1，可选）"), required=False)

    return {"ready": not missing, "checks": checks, "missing": missing,
            "checked_at": now.isoformat()}


# ======================= 飞书短版 =======================

def feishu_body(content: dict, *, status: str, brief_id: str,
                site: str | None) -> tuple[str, str]:
    """飞书推送用的短版。完整内容留在网页。"""
    mode = content.get("generation_mode")
    icon = {"full": "📋", "limited": "⚠️", "blocked": "⛔"}.get(mode, "📋")
    badge = {bg.STATUS_AI: "AI 生成", bg.STATUS_TEMPLATE: "模板降级",
             bg.STATUS_BLOCKED: "数据故障"}.get(status, status)
    title = f"{icon} {content.get('brief_date')} 风险简报 · {badge}"

    L: list[str] = []
    for s in (content.get("executive_summary") or [])[:3]:
        L.append(s)
    L.append("")

    a = content.get("account_summary") or {}
    m = content.get("market_summary") or {}
    if mode != bc.GATE_BLOCKED:
        L.append(f"账户风险：{bg._fmt(a.get('risk_level'))}"
                 f"（{bg._fmt(a.get('risk_score'))}）")
        L.append(f"市场环境：{bg._fmt(m.get('market_state'))}"
                 f" / {bg._fmt(m.get('risk_level'))}")
        L.append("")

    top = content.get("top_risks") or []
    if top:
        L.append("最大风险来源：")
        for t in top[:3]:
            L.append(f"· {t.get('title')}"
                     f"（{bg._fmt(t.get('actual_value'))} / 阈值 "
                     f"{bg._fmt(t.get('threshold'))}）")
        L.append("")

    ch = content.get("risk_changes") or {}
    L.append(f"变化：新增 {len(ch.get('new') or [])} · "
             f"升级 {len(ch.get('escalated') or [])} · "
             f"恢复 {len(ch.get('resolved') or [])}")

    p = content.get("performance") or {}
    if mode != bc.GATE_BLOCKED and p.get("cash_flow_adjusted_pnl") is not None:
        L.append(f"当期收益（已剔除资金进出）：{p['cash_flow_adjusted_pnl']}")
    L.append("")

    L.append("今日检查：")
    for c in (content.get("checks") or [])[:5]:
        L.append(f"· {c}")

    lims = content.get("data_limitations") or []
    if lims:
        L.append("")
        L.append("数据限制：")
        for x in lims[:3]:
            L.append(f"· {x}")
        if len(lims) > 3:
            L.append(f"· （另有 {len(lims) - 3} 条，详见看板）")

    L.append("")
    if site:
        L.append(f"完整简报：{site}/briefs")
    L.append(bp.DISCLAIMER)
    return title, "\n".join(L)


# ======================= 主流程 =======================

def generate_brief(cfg, db, *, brief_date: str | None = None,
                   trigger: str = TRIGGER_MANUAL, notifier=None,
                   provider=None, deliver: bool | None = None,
                   regenerate: bool = False, wait_attempt: int = 0,
                   now: datetime | None = None) -> dict:
    """生成（或复用）一份简报。

    `deliver` 为 None 时按触发方式决定：scheduled 推送，manual/regenerate 不推。
    **手动重新生成默认不重复打扰。**
    """
    now = now or datetime.now(timezone.utc)
    t0 = now
    run_id = uuid.uuid4().hex
    # 简报覆盖的是**美股交易日**，不是本地自然日 —— 北京 06:30 生成时，
    # 本地已是 D+1，而这份简报讲的是刚收盘的 D。
    day = str(brief_date or now.astimezone(getattr(cfg, "market_tz", timezone.utc))
              .date())[:10]
    if deliver is None:
        deliver = (trigger == TRIGGER_SCHEDULED)

    readiness = check_readiness(cfg, db, brief_date=day, now=now)

    def finish(status: str, **kw) -> dict:
        fin = datetime.now(timezone.utc)
        row = {"run_id": run_id, "brief_date": day, "trigger": trigger,
               "status": status, "readiness": readiness,
               "wait_attempts": wait_attempt,
               "duration_ms": int((fin - t0).total_seconds() * 1000),
               "started_at": t0.isoformat(), "finished_at": fin.isoformat(),
               **kw}
        try:
            db.save_brief_run(row)
        except Exception:  # noqa: BLE001
            log.exception("简报运行记录写入失败")
        return row

    # 数据未就绪且还有等待次数 —— 交给调度器稍后重试
    if not readiness["ready"] and wait_attempt < MAX_WAIT_ATTEMPTS:
        log.info("简报数据未就绪（第 %d 次），缺少 %s", wait_attempt + 1,
                 readiness["missing"])
        return finish("waiting", error=f"数据未就绪：{readiness['missing']}")

    try:
        ctx = bc.build(cfg, db, brief_date=day, now=now)
    except Exception as exc:                                # noqa: BLE001
        log.exception("简报上下文构建失败")
        return finish("failed", error=bg._sanitize(str(exc)))

    if not readiness["ready"]:
        # 等到上限仍未就绪：降级为 limited，并把原因写进限制清单
        if ctx["generation_mode"] == bc.GATE_FULL:
            ctx["generation_mode"] = bc.GATE_LIMITED
            ctx["gate"]["mode"] = bc.GATE_LIMITED
        ctx["data_limitations"] = list(ctx.get("data_limitations") or []) + [
            f"等待 {MAX_WAIT_ATTEMPTS} 次后数据仍未完全就绪"
            f"（缺少 {'、'.join(readiness['missing'])}），本次为受限简报"]
        ctx["context_hash"] = bc.context_hash(ctx)

    masked = ctx.get("account_id_masked")
    chash = ctx["context_hash"]

    # ---- 幂等：同日 + 同账户 + 同 hash + 同 prompt 版本 -> 直接复用 ----
    if not regenerate:
        try:
            existing = db.find_brief(brief_date=day, account_id_masked=masked,
                                     context_hash=chash,
                                     prompt_version=bp.PROMPT_VERSION)
        except Exception:  # noqa: BLE001
            existing = None
        if existing:
            log.info("简报复用 brief=%s（context_hash 未变）",
                     existing["brief_id"][:8])
            out = finish("success", brief_id=existing["brief_id"], reused=True,
                         generation_mode=existing["generation_mode"],
                         generation_status=existing["generation_status"])
            if deliver and notifier is not None:
                out["delivery"] = deliver_brief(cfg, db, existing["brief_id"],
                                                notifier=notifier, now=now)
            return out

    provider = provider if provider is not None else bg.build_provider(cfg)
    result = bg.generate(ctx, provider=provider)
    content = result["content"]
    validation = result["validation"]

    # 校验失败：不发送、记 validation_failed。模板版本本身失败时也如实记录 ——
    # 那说明模板有 bug，必须暴露而不是掩盖。
    v_status = "passed" if validation["valid"] else "failed"
    if not validation["valid"]:
        log.error("简报校验未通过 status=%s errors=%s",
                  result["status"], validation["errors"][:5])

    revision = 1
    try:
        revision = db.max_brief_revision(day, masked) + 1
    except Exception:  # noqa: BLE001
        pass

    brief_id = uuid.uuid4().hex
    acct = ctx.get("account") or {}
    row = {
        "brief_id": brief_id, "brief_date": day, "account_id_masked": masked,
        "revision": revision,
        "generation_mode": ctx["generation_mode"],
        "generation_status": result["status"],
        "content": content,
        "rendered_markdown": bg.render_markdown(content, status=result["status"]),
        "prompt_version": bp.PROMPT_VERSION,
        "model_identifier": result.get("model"),
        "context_hash": chash, "context_version": ctx.get("context_version"),
        "validation_status": v_status,
        "validation_errors": validation["errors"],
        "ai_attempts": result.get("attempts") or 0,
        "account_snapshot_id": acct.get("snapshot_id"),
        "risk_assessment_id": acct.get("assessment_id"),
        "market_assessment_id": (ctx.get("market") or {}).get("assessment_id"),
        "performance_period_id": (ctx.get("performance") or {}).get("period_id"),
        "calculation_versions": {
            "context": ctx.get("context_version"),
            "risk": acct.get("calculation_version"),
            "market": (ctx.get("market") or {}).get("calculation_version"),
            "performance": (ctx.get("performance") or {}).get("calculation_version"),
            "prompt": bp.PROMPT_VERSION,
        },
        "generated_at": now.isoformat(), "created_at": now.isoformat(),
    }
    try:
        db.save_brief(row)
        db.save_brief_sources(brief_id, ctx.get("source_references") or [],
                              now.isoformat())
    except Exception as exc:                                # noqa: BLE001
        log.exception("简报落库失败")
        return finish("failed", error=bg._sanitize(str(exc)))

    status = "success" if validation["valid"] else "partial"
    out = finish(status, brief_id=brief_id,
                 generation_mode=ctx["generation_mode"],
                 generation_status=result["status"],
                 error=(None if validation["valid"]
                        else f"校验未通过：{validation['errors'][:3]}"))

    # ---- 推送。**先落库再推送，推送失败不影响已落库的简报。** ----
    if deliver and notifier is not None:
        out["delivery"] = deliver_brief(cfg, db, brief_id, notifier=notifier,
                                        now=now)
    return out


# ======================= 推送 =======================

def deliver_test(cfg, db, brief_id: str, *, notifier,
                 now: datetime | None = None) -> dict:
    """链路测试发送。

    三条隔离，缺一不可：
      1. 标题带【测试】，正文首句声明这是链路测试
      2. 走**独立 channel**（feishu_test），不占用正式去重键
      3. **不写 delivered_at** —— 正式简报仍处于未发送状态

    这样明早 06:30 的正式推送不会因为「这个 context_hash 发过了」被跳过。
    """
    return deliver_brief(cfg, db, brief_id, notifier=notifier, now=now,
                         channel=CHANNEL_TEST, test_mode=True, force=True)


def deliver_brief(cfg, db, brief_id: str, *, notifier, now: datetime | None = None,
                  force: bool = False, channel: str = CHANNEL_FEISHU,
                  test_mode: bool = False) -> dict:
    """把简报推到飞书。

    三条拦截：校验未通过不发、同 context_hash 已发过不发、没有 notifier 不发。
    **任何异常都被吞掉并记录** —— 推送失败绝不能影响简报本身。
    """
    now = now or datetime.now(timezone.utc)
    row = db.get_brief(brief_id)
    if row is None:
        return {"status": "skipped", "reason": "简报不存在"}

    import json as _j
    try:
        content = _j.loads(row["content_json"])
    except (TypeError, ValueError):
        return {"status": "skipped", "reason": "简报内容无法解析"}

    def record(status: str, reason: str | None):
        try:
            db.record_brief_delivery({
                "brief_id": brief_id, "channel": channel, "status": status,
                "reason": reason, "context_hash": row["context_hash"],
                "created_at": now.isoformat()})
        except Exception:  # noqa: BLE001
            log.exception("推送记录写入失败")
        return {"status": status, "reason": reason}

    if row["validation_status"] != "passed":
        return record("skipped", "校验未通过的简报不推送")
    if not force and db.brief_delivered(context_hash=row["context_hash"],
                                        channel=channel):
        return record("skipped", "相同 context_hash 已推送过，不重复打扰")

    site = getattr(cfg, "dashboard_url", None) or getattr(cfg, "site_url", None)
    title, body = feishu_body(content, status=row["generation_status"],
                              brief_id=brief_id, site=site)
    if test_mode:
        title = f"{TEST_TITLE_PREFIX}{title}"
        body = f"{TEST_LEAD}\n\n{body}"
    try:
        ok = bool(notifier.send(title, body))
    except Exception as exc:                                # noqa: BLE001
        log.warning("飞书推送异常：%s", bg._sanitize(str(exc)))
        return record("failed", bg._sanitize(str(exc)))

    if not ok:
        return record("failed", "推送通道返回失败")
    if test_mode:
        # **不写 delivered_at** —— 正式简报必须仍处于未发送状态
        return record("test_success", "链路测试发送成功，未占用正式推送状态")
    try:
        db.mark_brief_delivered(brief_id, now.isoformat())
    except Exception:  # noqa: BLE001
        log.exception("标记已推送失败")
    return record("sent", None)


# ======================= 手动触发保护 =======================

def can_trigger_manually(db, *, now: datetime,
                         min_interval: int = MIN_MANUAL_INTERVAL_SECONDS
                         ) -> tuple[bool, str | None]:
    """手动触发的最小间隔保护。"""
    try:
        runs = db.brief_runs(limit=5)
    except Exception:  # noqa: BLE001
        return True, None
    for r in runs:
        if r.get("trigger") not in (TRIGGER_MANUAL, TRIGGER_REGENERATE):
            continue
        t = bc._dt(r.get("started_at"))
        if t is None:
            continue
        elapsed = (now - t).total_seconds()
        if elapsed < min_interval:
            return False, (f"距上次手动生成仅 {int(elapsed)} 秒，"
                           f"请至少间隔 {min_interval} 秒")
        break
    return True, None


def run_scheduled(cfg, db, *, notifier=None, now: datetime | None = None,
                  wait_attempt: int = 0) -> dict:
    """调度器入口。**任何异常都在这里终止，绝不影响其他调度任务。**

    同时写调度执行审计 —— 「昨晚 06:30 那次到底发生了什么」需要单一权威记录，
    而 daily_brief_runs 记的是每一次生成尝试（等待重试会有多行）。
    """
    now = now or datetime.now(timezone.utc)
    tz = getattr(cfg, "local_tz", timezone.utc)
    local = now.astimezone(tz)
    day = str(now.astimezone(getattr(cfg, "market_tz", timezone.utc)).date())[:10]
    scheduled_for = local.replace(
        hour=int(getattr(cfg, "brief_hour_local", 6)),
        minute=int(getattr(cfg, "brief_minute_local", 30)),
        second=0, microsecond=0).isoformat()

    # 等待重试接着写同一条执行记录，不新开一条
    existing = None
    try:
        existing = db.open_schedule_execution(day)
    except Exception:  # noqa: BLE001
        pass
    exec_id = (existing or {}).get("execution_id") or uuid.uuid4().hex
    started = (existing or {}).get("actual_started_at") or now.isoformat()

    audit = {
        "execution_id": exec_id, "brief_date": day,
        "scheduled_for": scheduled_for, "actual_started_at": started,
        "dependency_wait_count": wait_attempt,
        "created_at": (existing or {}).get("created_at") or now.isoformat(),
    }

    try:
        res = generate_brief(cfg, db, trigger=TRIGGER_SCHEDULED,
                             notifier=notifier, now=now,
                             wait_attempt=wait_attempt)
    except Exception as exc:                                # noqa: BLE001
        log.exception("每日简报调度任务失败（已隔离，不影响同步与告警）")
        res = {"status": "failed", "error": bg._sanitize(str(exc))}

    ready = (res.get("readiness") or {}).get("ready")
    audit.update({
        "dependency_ready_status": ("ready" if ready else
                                    ("not_ready" if res.get("status") == "waiting"
                                     else "degraded")),
        "dependency_detail": res.get("readiness") or {},
        "brief_id": res.get("brief_id"),
        "generation_mode": res.get("generation_mode"),
        "generation_status": res.get("generation_status"),
        "delivery_status": (res.get("delivery") or {}).get("status"),
        "reused": res.get("reused"),
        "error_message": res.get("error"),
    })
    if res.get("brief_id"):
        try:
            row = db.get_brief(res["brief_id"])
            audit["validation_status"] = (row or {}).get("validation_status")
        except Exception:  # noqa: BLE001
            pass
    # waiting 表示还会再来 —— 保持 completed_at 为空，下一轮接着写这条
    if res.get("status") != "waiting":
        audit["completed_at"] = datetime.now(timezone.utc).isoformat()

    try:
        db.save_schedule_execution(audit)
    except Exception:  # noqa: BLE001
        log.exception("调度执行审计写入失败（不影响简报本身）")
    return {**res, "execution_id": exec_id}
