"""Flex Query 同步编排：请求 → 轮询 → 下载 → 解析 → 幂等写入 → 对账。

**与实时账户同步完全解耦**：独立任务、独立 run 状态、异常全吞。Flex 是 T+1 的
补充数据源，它挂了不该让分钟级的账户同步、行情同步或风险告警受到任何影响。

轮询与重试都有硬上限：
  · 报表未生成（错误码 1019）不算失败，继续轮询直到 max_polls
  · 认证失败 / 权限错误（1003/1012/1015…）**立刻停止**，重试没有意义
  · 其余错误按退避重试，次数有限

凭证处理：token 只在 FlexClient 内部使用，不进日志、不进库、不进 API 响应。
异常信息统一走 sanitize_error 过一遍 —— IBKR 的报错有时会回显带 token 的 URL。
"""
from __future__ import annotations

import hashlib
import json
import time
import uuid
from datetime import datetime, timedelta, timezone

from . import cashflow_model as cm
from . import flex_parser as fp
from . import flex_xml as fx
from .flex import FetchResult, FlexError
from .logging_setup import get_logger

log = get_logger()

CALC_VERSION = "flex-v1/2026-07"

# 同步状态
ST_PENDING = "pending"
ST_REQUESTING = "requesting"
ST_WAITING = "waiting_report"
ST_DOWNLOADING = "downloading"
ST_PARSING = "parsing"
ST_SUCCESS = "success"
ST_PARTIAL = "partial"
ST_FAILED = "failed"
ST_NOT_CONFIGURED = "not_configured"

#: 这些 IBKR 错误码代表凭证或权限问题，**重试毫无意义**，立刻停止。
#: 无限重试一个失效的 token 只会招来 IBKR 的限流甚至封禁。
FATAL_ERROR_CODES = ("1003", "1012", "1015", "1018", "1020", "1021")
#: 报表尚未生成。这不是失败，继续轮询。
NOT_READY_CODE = "1019"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def is_configured(cfg) -> bool:
    return bool(getattr(cfg, "flex_token", "") and getattr(cfg, "flex_query_id", ""))


def _mask_query_id(qid: str | None) -> str | None:
    """只留后 4 位。完整 query id 也算凭证的一部分，不入库。"""
    if not qid:
        return None
    q = str(qid)
    return f"****{q[-4:]}" if len(q) > 4 else "****"


def _is_structural(exc: Exception) -> bool:
    """是否为报表结构问题（重试无意义）。"""
    return isinstance(exc, fx.FlexStructureError)


def _extract_error_code(msg: str) -> str | None:
    import re
    m = re.search(r"\[(\d{3,4})\]", msg or "")
    return m.group(1) if m else None


#: 触发来源。手动与定时**共用同一个核心函数**，只有这个字段不同 ——
#: 两条路径各写一份实现，迟早会一边修好、另一边还坏着。
TRIGGER_MANUAL = "manual"
TRIGGER_SCHEDULED = "scheduled"


def sync(cfg, db, *, now: datetime | None = None, request_id: str | None = None,
         client=None, force: bool = False) -> dict:
    """向后兼容的别名。新代码请直接用 run_flex_sync。"""
    return run_flex_sync(cfg, db, trigger=TRIGGER_SCHEDULED, now=now,
                         request_id=request_id, client=client, force=force)


def _to_et_date(v) -> str | None:
    """Flex 的 YYYYMMDD -> 美东日期 YYYY-MM-DD。已是后者则原样返回。"""
    t = str(v or "").strip()
    if len(t) == 8 and t.isdigit():
        return f"{t[:4]}-{t[4:6]}-{t[6:]}"
    return t or None


def _record_flex_coverage(db, cfg, period_from, period_to, status: str) -> None:
    """把 Flex 报表区间登记为现金流覆盖。

    **只在 success 时登记 COMPLETE** —— partial 说明报表本身有问题，
    拿它去声明「这段时间的资金流已经全知道了」就是在撒谎。
    任何异常都吞掉：登记失败不该让整轮 Flex 同步失败。
    """
    if status != ST_SUCCESS:
        return
    start, end = _to_et_date(period_from), _to_et_date(period_to)
    account = getattr(cfg, "ib_account", None)
    if not (start and end and account):
        return
    try:
        db.set_cash_flow_coverage(
            account=account, start_date=start, end_date=end, status="COMPLETE",
            note=f"由 Flex 报表 {start} ~ {end} 自动登记（券商权威流水）",
            source="FLEX_QUERY")
        log.info("现金流覆盖已登记 %s ~ %s（来源 FLEX_QUERY）", start, end)
    except Exception:  # noqa: BLE001
        log.warning("现金流覆盖登记失败（不影响 Flex 同步本身）", exc_info=True)


def run_flex_sync(cfg, db, *, trigger: str = TRIGGER_SCHEDULED,
                  now: datetime | None = None, request_id: str | None = None,
                  client=None, force: bool = False) -> dict:
    """执行一次 Flex 同步。**任何异常都不外抛。**

    手动触发与定时任务走的是这同一个函数，只有 trigger 不同。

    成功的门槛（任何一条不满足都不得标 success）：
      1. SendRequest 返回**非空** ReferenceCode
      2. 确实调用了 GetStatement
      3. 返回的 XML 能解析，且内部至少有一个 FlexStatement
      4. 至少有 AccountInformation 或任一业务明细节点
      5. 幂等落库确实执行完毕
    """
    now = now or _now()
    run_id = uuid.uuid4().hex
    t0 = time.monotonic()

    if not is_configured(cfg):
        # 未配置不是错误，是一种明确的状态。记录它，让页面能如实显示。
        db.save_flex_run({
            "run_id": run_id, "query_id_masked": None, "reference_code": None,
            "started_at": now.isoformat(), "completed_at": now.isoformat(),
            "status": ST_NOT_CONFIGURED,
            "error_code": "NOT_CONFIGURED",
            "error_message": "未配置 FLEX_TOKEN / FLEX_QUERY_ID",
            "request_id": request_id, "trigger_source": trigger,
            "created_at": now.isoformat(), "duration_ms": 0})
        return {"run_id": run_id, "status": ST_NOT_CONFIGURED,
                "configured": False, "trigger": trigger,
                "message": "未配置 Flex 凭证，请在 IBKR 后台创建 Flex Query 后填入 .env"}

    db.save_flex_run({
        "run_id": run_id, "query_id_masked": _mask_query_id(cfg.flex_query_id),
        "started_at": now.isoformat(), "status": ST_REQUESTING,
        "request_id": request_id, "trigger_source": trigger,
        "created_at": now.isoformat()})

    max_retries = int(getattr(cfg, "flex_max_retries", 2))
    backoff = int(getattr(cfg, "flex_poll_interval", 5))
    fetched = FetchResult()
    last_err = None
    retries = 0

    for attempt in range(max_retries + 1):
        retries = attempt
        try:
            c = client or _build_client(cfg)
            db.update_flex_run(run_id, status=ST_WAITING)
            fetched = _fetch(c)
            if fetched.reference_code:
                db.update_flex_run(run_id, reference_code=fetched.reference_code,
                                   poll_count=fetched.poll_count,
                                   status=ST_DOWNLOADING)
            break
        except Exception as exc:  # noqa: BLE001 — 取数任何异常都不外抛
            msg = cm.sanitize_error(str(exc))
            code = _extract_error_code(msg)
            kind = code or ("FLEX_ERROR" if isinstance(exc, FlexError)
                            else type(exc).__name__)
            last_err = (kind, msg)
            # 凭证/权限问题：立刻停，不重试。
            # 错误码可能来自 FlexError，也可能来自结构层的 FlexStructureError
            # （SendRequest 被拒绝就是后者）—— 按**错误码**判定，不按异常类型，
            # 否则换个异常类就会绕过这道闸，把失效 token 反复重试到被限流。
            if code in FATAL_ERROR_CODES:
                log.warning("Flex 认证或权限错误，停止重试：%s", msg)
                break
            if _is_structural(exc):
                # 报表结构问题重试也不会变好，直接停
                log.warning("Flex 报表结构错误，停止重试：%s", msg)
                break
            if attempt < max_retries:
                time.sleep(min(backoff * (2 ** attempt), 60))

    def _fail(code: str, msg: str) -> dict:
        db.finish_flex_run(
            run_id, status=ST_FAILED, retry_count=retries,
            duration_ms=int((time.monotonic() - t0) * 1000),
            reference_code=fetched.reference_code,
            poll_count=fetched.poll_count,
            error_code=code, error_message=cm.sanitize_error(msg))
        log.warning("Flex 同步失败（实时同步不受影响）：%s", cm.sanitize_error(msg))
        return {"run_id": run_id, "status": ST_FAILED, "configured": True,
                "trigger": trigger, "error_code": code,
                "error": cm.sanitize_error(msg),
                "reference_code_present": bool(fetched.reference_code),
                "poll_count": fetched.poll_count,
                "get_statement_called": fetched.get_statement_called}

    if last_err and fetched.xml is None:
        return _fail(*last_err)
    # 下面三道闸，任何一道不过都不得 success —— 它们各自对应一种
    # 「看起来跑完了、其实什么都没做」的失败形态
    if not fetched.reference_code:
        return _fail("NO_REFERENCE_CODE",
                     "SendRequest 未返回 ReferenceCode，同步未真正发起")
    if not fetched.get_statement_called:
        return _fail("NO_GET_STATEMENT",
                     "未调用 GetStatement，没有取得任何报表")
    if not fetched.xml:
        return _fail("EMPTY_REPORT", "GetStatement 未返回报表内容")

    return _ingest(cfg, db, run_id, fetched.xml, now=now, t0=t0, retries=retries,
                   force=force, trigger=trigger, fetched=fetched)


def _fetch(client) -> FetchResult:
    """统一取数出口。兼容只实现了 fetch() 的旧客户端与测试替身。"""
    if hasattr(client, "fetch_detailed"):
        return client.fetch_detailed()
    xml = client.fetch()
    # 老式客户端不暴露过程，按「已完成两步」记录，但仍会走后续结构校验
    return FetchResult(xml=xml, reference_code="(legacy-client)",
                       poll_count=1, send_request_ok=True,
                       get_statement_called=True)


def _build_client(cfg):
    from .flex import FlexClient
    return FlexClient(
        cfg.flex_token, cfg.flex_query_id,
        timeout=float(getattr(cfg, "flex_request_timeout", 30)),
        poll_interval=float(getattr(cfg, "flex_poll_interval", 5)),
        max_polls=int(getattr(cfg, "flex_max_polls", 12)))


def _ingest(cfg, db, run_id: str, xml: str, *, now, t0, retries: int,
            force: bool, trigger: str = TRIGGER_SCHEDULED,
            fetched=None) -> dict:
    """结构校验 -> 解析 -> 幂等写入。**任何一步不成立都判 failed。**"""
    db.update_flex_run(run_id, status=ST_PARSING)
    report_hash = hashlib.sha256(xml.encode("utf-8", "replace")).hexdigest()[:32]
    ref = getattr(fetched, "reference_code", None)
    polls = getattr(fetched, "poll_count", 0)

    def _fail(code: str, msg: str) -> dict:
        db.finish_flex_run(
            run_id, status=ST_FAILED, retry_count=retries,
            duration_ms=int((time.monotonic() - t0) * 1000),
            reference_code=ref, poll_count=polls, report_hash=report_hash,
            error_code=code, error_message=cm.sanitize_error(msg))
        log.warning("Flex 同步失败：%s", cm.sanitize_error(msg))
        return {"run_id": run_id, "status": ST_FAILED, "configured": True,
                "trigger": trigger, "error_code": code,
                "error": cm.sanitize_error(msg)}

    # 结构校验：没有 FlexStatement、没有任何明细，一律 failed。
    # 解析异常绝不能降级成「空集合 + success」。
    try:
        structure = fx.validate_final_report(xml)
    except fx.FlexStructureError as exc:
        return _fail("INVALID_REPORT", str(exc))
    except Exception as exc:  # noqa: BLE001
        return _fail("PARSE_ERROR", str(exc))

    # 同一份报表重复下载时，仍然要走一遍写入 —— 写入本身是幂等的，
    # 直接跳过会让「上次写了一半」的情况永远修不好。
    duplicate = db.flex_run_with_hash(report_hash, exclude_run_id=run_id)

    try:
        parsed = fp.parse_all(xml, cfg.market_tz, validate=False)
    except Exception as exc:  # noqa: BLE001
        return _fail("PARSE_ERROR", str(exc))
    parsed.setdefault("structure", structure)

    rows = parsed["cash_flows"]
    now_iso = now.isoformat()
    inserted = updated = skipped = 0
    issues: list[str] = []

    for r in rows:
        try:
            db_row = r.as_db_row(now_iso)
            db_row["flex_run_id"] = run_id
            res = db.upsert_cash_flow(db_row)
            if res == "inserted":
                inserted += 1
            elif res == "updated":
                updated += 1
            else:
                skipped += 1
        except Exception as exc:  # noqa: BLE001
            skipped += 1
            issues.append(cm.sanitize_error(str(exc)))

    # 余额锚点：**独立落库，绝不进 cash_flows** —— 它们不是经济事件
    balance_rows = 0
    try:
        markers = parsed.get("balance_markers") or []
        db.upsert_statement_balances([{
            "balance_id": m.balance_id(report_hash),
            "account_id": m.account_id,
            "report_from": parsed.get("period_from"),
            "report_to": parsed.get("period_to"),
            "balance_date": m.balance_date, "balance_type": m.balance_type,
            "currency": m.currency, "balance": str(m.balance),
            "balance_base": (str(m.balance_base)
                             if m.balance_base is not None else None),
            "fx_rate_to_base": (str(m.fx_rate_to_base)
                                if m.fx_rate_to_base is not None else None),
            "level_of_detail": m.level_of_detail, "description": m.description,
            "node_index": m.node_index, "report_hash": report_hash,
            "quality_status": m.quality_status,
            "created_at": now_iso, "updated_at": now_iso} for m in markers])
        balance_rows = len(markers)
    except Exception as exc:  # noqa: BLE001
        issues.append(f"余额锚点落库失败: {cm.sanitize_error(str(exc))}")

    rate_rows = 0
    try:
        rate_rows = db.upsert_conversion_rates(
            parsed.get("conversion_rates") or [], report_hash=report_hash,
            now_iso=now_iso)
    except Exception as exc:  # noqa: BLE001
        issues.append(f"汇率表落库失败: {cm.sanitize_error(str(exc))}")

    pnl_rows = 0
    for p in parsed.get("realized_pnl", []):
        try:
            db.upsert_realized_pnl({**p, "created_at": now_iso,
                                    "updated_at": now_iso})
            pnl_rows += 1
        except Exception as exc:  # noqa: BLE001
            issues.append(cm.sanitize_error(str(exc)))

    nav_rows = 0
    for p in parsed.get("nav_daily", []):
        try:
            db.upsert_flex_nav_daily({**p, "report_hash": report_hash,
                                      "created_at": now_iso, "updated_at": now_iso})
            nav_rows += 1
        except Exception as exc:  # noqa: BLE001
            issues.append(f"ChangeInNAV 落库失败: {cm.sanitize_error(str(exc))}")

    transfer_rows = 0
    for p in parsed.get("transfers", []):
        try:
            db.upsert_flex_transfer({**p, "report_hash": report_hash,
                                     "created_at": now_iso, "updated_at": now_iso})
            transfer_rows += 1
        except Exception as exc:  # noqa: BLE001
            issues.append(f"Transfer 落库失败: {cm.sanitize_error(str(exc))}")

    unclassified = sum(1 for r in rows if r.quality_status == cm.QUALITY_UNCLASSIFIED)
    if unclassified:
        issues.append(f"{unclassified} 笔流水无法分类，已记为 unknown（未猜测）")

    sections = parsed.get("sections", {})
    account_rows = int(sections.get("account_information") or 0)
    # 一条都没解析出来、也没有账户信息 —— 这不是「报表是空的」，
    # 这是「我们没能从报表里读出任何东西」。绝不 success。
    if not rows and account_rows == 0:
        return _fail("EMPTY_RESULT",
                     "报表解析后既无任何流水、也无 AccountInformation，"
                     f"section 计数 {sections}")

    period_from = parsed.get("period_from")
    period_to = parsed.get("period_to")
    if not (period_from and period_to):
        issues.append("报表未给出 fromDate/toDate，数据质量降级")

    status = ST_PARTIAL if (parsed.get("errors") or issues) else ST_SUCCESS

    # 登记现金流覆盖区间。schema 里 source 早就预留了 FLEX_QUERY ——
    # 券商流水覆盖了这段时间，就等于「这段时间的出入金已经全知道了」，
    # 比人工登记更权威。没有它，周期表现只能降级成「净资产变动」、
    # 曲线的盈亏系列也一直被锁着，明明数据已经在库里。
    _record_flex_coverage(db, cfg, period_from, period_to, status)

    db.finish_flex_run(
        run_id, status=status, retry_count=retries,
        duration_ms=int((time.monotonic() - t0) * 1000),
        reference_code=ref, poll_count=polls,
        report_from=period_from, report_to=period_to,
        records_received=len(rows), records_inserted=inserted,
        records_updated=updated, records_skipped=skipped,
        realized_pnl_rows=pnl_rows, report_hash=report_hash,
        section_counts=json.dumps(sections, ensure_ascii=False),
        error_message=("；".join((parsed.get("errors") or []) + issues)[:400]
                       or None))

    log.info("Flex 同步 %s：收到 %d 笔，新增 %d，更新 %d，跳过 %d，"
             "已实现盈亏 %d 条，官方日收益 %d 天，证券转仓 %d 条%s",
             status, len(rows), inserted, updated, skipped, pnl_rows,
             nav_rows, transfer_rows,
             "（报表内容与上次相同）" if duplicate else "")

    # 派生数据：分类汇总与对账
    try:
        _refresh_derived(cfg, db, parsed, now)
    except Exception:  # noqa: BLE001
        log.warning("Flex 派生数据刷新失败（流水已入库）", exc_info=True)

    return {"run_id": run_id, "status": status, "configured": True,
            "balance_markers": balance_rows,
            "balance_summary": parsed.get("balance_summary"),
            "conversion_rates": rate_rows,
            "trigger": trigger, "reference_code_present": bool(ref),
            "poll_count": polls,
            "root_tag": parsed.get("root_tag"),
            "flex_statements": parsed.get("flex_statements"),
            "raw_row_count": parsed.get("raw_row_count"),
            "deduplicated_away": parsed.get("deduplicated_away"),
            "unclassified": unclassified,
            "records_received": len(rows), "records_inserted": inserted,
            "records_updated": updated, "records_skipped": skipped,
            "realized_pnl_rows": pnl_rows,
            "nav_daily_rows": nav_rows,
            "transfer_rows": transfer_rows,
            "report_from": parsed.get("period_from"),
            "report_to": parsed.get("period_to"),
            "sections": parsed.get("sections", {}),
            "duplicate_report": bool(duplicate),
            "categories": fp.summarize(rows),
            "issues": issues[:10]}


def _refresh_derived(cfg, db, parsed: dict, now) -> None:
    """刷新收入支出汇总与现金对账。失败不影响已入库的流水。"""
    from . import reconciliation_cash as rec
    account = getattr(cfg, "ib_account", None)
    start = str(parsed.get("period_from") or "")[:10]
    end = str(parsed.get("period_to") or "")[:10]
    if len(start) == 8:                     # IBKR 用 YYYYMMDD
        start = f"{start[:4]}-{start[4:6]}-{start[6:]}"
    if len(end) == 8:
        end = f"{end[:4]}-{end[4:6]}-{end[6:]}"
    if not (start and end):
        return
    db.rebuild_income_expense(account, start, end, CALC_VERSION,
                              now.isoformat())
    rec.reconcile_period(cfg, db, account=account, start=start, end=end,
                         now=now)
    # 分币种对账：以报表自带的余额锚点为准，不依赖账户快照覆盖整个区间
    from . import reconciliation_currency as rcc
    rcc.reconcile_report(cfg, db, account=account,
                         report_from=parsed.get("period_from"),
                         report_to=parsed.get("period_to"), now=now)


def status(cfg, db) -> dict:
    """当前 Flex 状态。**绝不返回 token 或完整 query id。**"""
    configured = is_configured(cfg)
    last = db.latest_flex_run()
    last_success = db.latest_flex_run(status=ST_SUCCESS)
    counts = db.cash_flow_category_counts()
    return {
        "configured": configured,
        "enabled": bool(getattr(cfg, "flex_sync_enabled", True)),
        "query_id_masked": _mask_query_id(getattr(cfg, "flex_query_id", "")),
        "status": (last or {}).get("status") or (
            ST_NOT_CONFIGURED if not configured else ST_PENDING),
        "last_run": last,
        "last_success_at": (last_success or {}).get("completed_at"),
        "last_report_from": (last_success or {}).get("report_from"),
        "last_report_to": (last_success or {}).get("report_to"),
        "records_total": sum(counts.values()),
        "category_counts": counts,
        "data_quality": _quality(configured, last),
        "setup_hint": (None if configured else
                       "需在 IBKR 客户端门户创建 Flex Query 并生成 Token，"
                       "然后填入 .env 的 FLEX_TOKEN / FLEX_QUERY_ID"),
    }


def _quality(configured: bool, last: dict | None) -> str:
    if not configured:
        return ST_NOT_CONFIGURED
    if not last:
        return "no_data"
    st = last.get("status")
    if st in (ST_SUCCESS, ST_PARTIAL):
        # 报表区间为空说明我们不知道这份数据覆盖了哪一段时间，
        # 用它做业绩归因会把未覆盖期间的资金变动静默当成收益 —— 不能算 ok
        if not (last.get("report_from") and last.get("report_to")):
            return "partial"
        return "ok" if st == ST_SUCCESS else "partial"
    return "degraded"
