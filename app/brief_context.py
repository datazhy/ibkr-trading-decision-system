"""每日风险简报的**确定性上下文构建器**（第一阶段）。

这是整个简报架构的地基，规矩只有一条：

    **所有数字都在这里算完。AI 只负责组织语言，绝不重新计算任何指标。**

因此本模块：
  - 只从已落库的结构化结果里取数（风险评估、市场评估、事件登记、收益归因…）
  - 输出一份固定 Schema 的 JSON，交给第二阶段
  - 为每个关键数字记录**来源引用**（表、主键、计算版本、数据时间）

还有一条同样重要：**上下文里的每个数字都必须绑定明确的时间点**。
账户数据、市场数据、Flex 报表三者的时间几乎从不一致 —— 把它们拼在一起
假装是同一时刻，是这类系统最容易犯也最难发现的错误。所以时间差要显式算出来
并在超阈值时明确说明。

门禁三态：
    full     核心数据可靠，可以给完整风险结论
    limited  部分数据过期/缺失/仍是估算，可以出简报但不给确定性升级结论
    blocked  核心账户数据不可用或 ai_analysis_allowed=false，只出数据状态简报
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation  # noqa: F401  (brief_generator 复用)
from typing import Any

BRIEF_VERSION = "daily-risk-brief-v1"
CONTEXT_VERSION = "brief-context-v1/2026-07"

# ---------------- 门禁 ----------------
GATE_FULL = "full"
GATE_LIMITED = "limited"
GATE_BLOCKED = "blocked"

# ---------------- 来源类型（事实 / AI 解释分离）----------------
SRC_FACT = "fact"                   # 券商或系统直接记录的事实
SRC_RULE = "rule_result"            # 规则引擎判定结果
SRC_DERIVED = "derived_metric"      # 由事实推导出的指标
SRC_AI = "ai_explanation"           # AI 写的解释文字
SRC_LIMITATION = "limitation"       # 数据限制说明

#: 账户数据与市场数据允许的最大时间差。超过就必须在简报里说清楚 ——
#: 用昨天的市场环境解释今天的账户风险，结论会误导人。
MAX_CROSS_SOURCE_GAP_HOURS = 12

#: 账户快照超过这个年龄就不再是「当前」状态
ACCOUNT_STALE_HOURS = 6
ACCOUNT_BLOCKED_HOURS = 48

#: 历史基线的最小样本量。低于此值不得声称规律。
MIN_BASELINE_SAMPLES = 5
BASELINE_WINDOWS = (7, 30)

#: 新集中度规则的观察期声明 —— 它们 2026-07 才上线，还没有历史样本
OBSERVATION_PERIOD_RULES = ("CONC_HHI_HIGH", "EFFECTIVE_POSITION_COUNT_LOW",
                            "SINGLE_POSITION_DOMINANCE")
OBSERVATION_NOTE = ("该规则目前处于数据积累期，尚无足够历史样本评价其预警效果。")

#: 旧事件缺规则版本时的标记。**绝不伪造成当前版本** ——
#: 那会让「这条事件当时用的什么规则」永远说不清，也会污染规则效果研究。
LEGACY_MISSING_VERSION = "legacy_missing_version"


def _d(v: Any) -> Decimal | None:
    if v is None or v == "":
        return None
    try:
        d = Decimal(str(v))
    except (InvalidOperation, TypeError, ValueError):
        return None
    # Decimal 能成功解析 NaN / Infinity，但它们不是可用于简报计算、展示或
    # 溯源的数值；若继续传递，后续 quantize 会抛 InvalidOperation。
    return d if d.is_finite() else None


def _s(v: Any) -> str | None:
    """转成字符串保存。

    **绝不把 Decimal 转成 float** —— 二进制浮点会让 0.1+0.2 变成
    0.30000000000000004，金额展示出来就是错的。
    """
    d = _d(v)
    return None if d is None else str(d)


def _dt(v) -> datetime | None:
    if not v:
        return None
    try:
        t = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


def mask_account(account_id: str | None) -> str | None:
    """账户号脱敏。简报会进数据库、API 和飞书，完整账户号一处都不该出现。"""
    if not account_id:
        return None
    a = str(account_id)
    return a[:2] + "*" * max(0, len(a) - 4) + a[-2:] if len(a) > 4 else "*" * len(a)


# ======================= 来源引用 =======================

def source_ref(*, source_type: str, source_table: str, source_id: str | None,
               field_path: str, value: Any, source_version: str | None = None,
               data_as_of: str | None = None) -> dict:
    """一条来源引用。

    `value_hash` 让「简报里这个数字有没有被改过」可验证 —— 存哈希而不是原值，
    避免把金额重复散布到又一张表里。
    """
    raw = "" if value is None else str(value)
    return {
        "source_type": source_type, "source_table": source_table,
        "source_id": source_id, "source_version": source_version,
        "data_as_of": data_as_of, "field_path": field_path,
        "value_hash": hashlib.sha256(raw.encode()).hexdigest()[:16],
    }


# ======================= 门禁 =======================

def evaluate_gate(*, account: dict | None, market: dict | None,
                  performance: dict | None, quality_issues: list[dict] | None,
                  now: datetime) -> dict:
    """AI 门禁判定。返回 mode + 逐项检查 + 被阻止的分析。

    判定顺序是刻意的：**先看能不能出结论，再看结论有多可信。**
    """
    checks: list[dict] = []
    blocked_reasons: list[str] = []
    limits: list[str] = []

    def check(name: str, ok: bool | None, detail: str):
        checks.append({"check": name, "passed": ok, "detail": detail})

    # ---- 1. 最新账户快照是否存在 ----
    if not account:
        check("account_snapshot_exists", False, "没有任何账户快照")
        blocked_reasons.append("没有可用的账户快照，无法给出任何账户风险结论")
    else:
        check("account_snapshot_exists", True,
              f"快照 {str(account.get('snapshot_id') or '')[:12]}")

        # ---- 2. 账户快照是否严重过期 ----
        age_h = None
        t = _dt(account.get("data_as_of"))
        if t is not None:
            age_h = (now - t).total_seconds() / 3600
        if age_h is None:
            check("account_fresh", None, "账户数据时间缺失")
            limits.append("账户快照缺少数据时间，新鲜度无法判断")
        elif age_h > ACCOUNT_BLOCKED_HOURS:
            check("account_fresh", False, f"账户数据已过期 {age_h:.1f} 小时")
            blocked_reasons.append(
                f"账户数据已过期 {age_h:.0f} 小时（超过 {ACCOUNT_BLOCKED_HOURS} 小时上限）")
        elif age_h > ACCOUNT_STALE_HOURS:
            check("account_fresh", False, f"账户数据已过期 {age_h:.1f} 小时")
            limits.append(f"账户数据截至 {account.get('data_as_of')}，"
                          f"距今 {age_h:.1f} 小时，不代表当前实时状态")
        else:
            check("account_fresh", True, f"账户数据 {age_h:.1f} 小时内")

        # ---- 3. 风险评估是否可靠 ----
        if account.get("reliable"):
            check("risk_assessment_reliable", True, "风险评估可靠")
        else:
            check("risk_assessment_reliable", False,
                  account.get("blocked_reason") or "风险评估被标记为不可靠")
            blocked_reasons.append(
                account.get("blocked_reason") or "风险评估不可靠，不允许给出风险结论")

        # ---- 4. ai_analysis_allowed ----
        if account.get("ai_analysis_allowed"):
            check("ai_analysis_allowed", True, "允许 AI 分析")
        else:
            check("ai_analysis_allowed", False, "ai_analysis_allowed=false")
            blocked_reasons.append(
                "数据质量层判定 ai_analysis_allowed=false，本次不生成 AI 风险解读")

    # ---- 5. 市场环境是否可靠 ----
    if not market:
        check("market_available", False, "没有市场环境评估")
        limits.append("没有可用的市场环境评估，本次简报不含市场判断")
    elif not market.get("reliable"):
        check("market_available", False,
              market.get("blocked_reason") or "市场评估不可靠")
        limits.append("市场环境数据不可靠，市场部分仅作参考，不用于风险升级判断")
    else:
        check("market_available", True, f"市场状态 {market.get('market_state')}")

    # ---- 6. 收益数据是否至少有可解释的质量状态 ----
    if not performance:
        check("performance_available", False, "没有收益归因结果")
        limits.append("没有可用的收益归因结果，本次不含收益分析")
    else:
        q = performance.get("data_quality")
        if q in (None, ""):
            check("performance_quality", None, "收益数据质量未标记")
            limits.append("收益数据未标记质量状态，相关数字仅供参考")
        else:
            check("performance_quality", True, f"收益数据质量 {q}")
            if q != "exact":
                limits.append(f"收益数据质量为 {q}，不是精确值")

    # ---- 7. critical 数据质量问题 ----
    crits = [i for i in (quality_issues or [])
             if str(i.get("severity") or "").lower() in ("critical", "error")]
    if crits:
        check("no_critical_quality_issues", False, f"{len(crits)} 个严重数据质量问题")
        blocked_reasons.append(
            f"存在 {len(crits)} 个严重数据质量问题，风险结论不可靠")
    else:
        check("no_critical_quality_issues", True, "无严重数据质量问题")

    if blocked_reasons:
        mode = GATE_BLOCKED
    elif limits:
        mode = GATE_LIMITED
    else:
        mode = GATE_FULL

    return {
        "mode": mode, "checks": checks,
        "blocked_reasons": blocked_reasons,
        "limitations": limits,
        "blocked_analyses": _blocked_analyses(mode),
    }


def _blocked_analyses(mode: str) -> list[str]:
    if mode == GATE_BLOCKED:
        return ["账户风险等级判定", "风险变化分析", "集中度分析",
                "收益归因解读", "历史基线对比"]
    if mode == GATE_LIMITED:
        return ["确定性的风险升级结论"]
    return []


# ======================= 时间一致性 =======================

def time_alignment(*, account_as_of: str | None, market_as_of: str | None,
                   flex_report_to: str | None, now: datetime) -> dict:
    """账户/市场/Flex 三个来源的时间关系。

    时间差过大时必须显式说明 —— 这是「不同时间点的数据不得伪装成同一时点」
    这条规矩的落地。
    """
    a, m = _dt(account_as_of), _dt(market_as_of)
    gap_h = None
    if a is not None and m is not None:
        gap_h = abs((a - m).total_seconds()) / 3600

    notes: list[str] = []
    if gap_h is not None and gap_h > MAX_CROSS_SOURCE_GAP_HOURS:
        notes.append(
            f"账户数据（{account_as_of}）与市场数据（{market_as_of}）"
            f"相差 {gap_h:.1f} 小时，两者不代表同一时刻的状态，"
            "不应把市场环境直接当作账户当前风险的成因")
    if flex_report_to:
        notes.append(
            f"券商 Flex 流水覆盖至 {flex_report_to}（T+1 数据源），"
            "最近一个交易日的现金流可能尚未入账，最新一天可能为暂定数据")
    return {
        "account_data_as_of": account_as_of,
        "market_data_as_of": market_as_of,
        "flex_report_to": flex_report_to,
        "cross_source_gap_hours": (None if gap_h is None else f"{gap_h:.2f}"),
        "exceeds_tolerance": bool(gap_h is not None
                                  and gap_h > MAX_CROSS_SOURCE_GAP_HOURS),
        "notes": notes,
        "generated_at": now.isoformat(),
    }


# ======================= 风险变化分类 =======================

#: 排序优先级 —— 数字越小越靠前。
#: critical 升级 > 新 critical > 新 high > high 升级 > 恢复 > 持续 high > warning
_CHANGE_ORDER = {
    ("escalated", "critical"): 0,
    ("new", "critical"): 1,
    ("new", "high"): 2,
    ("escalated", "high"): 3,
    ("resolved", "critical"): 4,
    ("resolved", "high"): 5,
    ("persistent", "critical"): 6,
    ("persistent", "high"): 7,
}
_DEFAULT_ORDER = 20


def classify_changes(events: list[dict], *, since: datetime,
                     now: datetime) -> dict:
    """把事件登记表分成 新增 / 升级 / 持续 / 恢复 / 已确认 / 被抑制。

    重点是**变化**，不是每天重复列一遍所有规则 —— 每天都在的告警没有信息量，
    而它挤掉的正是今天真正变了的那一条。
    """
    buckets: dict[str, list[dict]] = {
        "new": [], "escalated": [], "persistent": [],
        "resolved": [], "acknowledged": [], "suppressed": [],
    }
    for e in events or []:
        first = _dt(e.get("first_detected_at"))
        last = _dt(e.get("last_detected_at"))
        resolved = _dt(e.get("resolved_at"))
        status = str(e.get("status") or "").lower()
        sev = str(e.get("severity") or "warning").lower()

        item = {
            "event_id": e.get("event_id"),
            "rule_code": e.get("rule_code"),
            "title": e.get("title"),
            "severity": sev,
            "peak_severity": e.get("peak_severity") or sev,
            "affected": e.get("affected_symbol") or e.get("affected_instrument"),
            "actual_value": _s(e.get("actual_value")) or e.get("actual_value"),
            "threshold_value": _s(e.get("threshold_value")) or e.get("threshold_value"),
            "first_detected_at": e.get("first_detected_at"),
            "last_detected_at": e.get("last_detected_at"),
            "occurrence_count": e.get("occurrence_count"),
            # 缺版本如实标记，不用当前版本冒充
            "rule_version": e.get("rule_version") or LEGACY_MISSING_VERSION,
            "rule_version_missing": not e.get("rule_version"),
            "rule_set_version": e.get("rule_set_version"),
            "rule_set_hash": e.get("rule_set_hash"),
            "market_assessment_id": e.get("market_assessment_id"),
            "source_type": e.get("source_type"),
            "in_observation_period": e.get("rule_code") in OBSERVATION_PERIOD_RULES,
        }

        if status == "resolved" or resolved is not None:
            if resolved is not None and resolved >= since:
                buckets["resolved"].append(item)
            continue
        if status == "suppressed":
            buckets["suppressed"].append(item)
            continue
        if status == "acknowledged":
            buckets["acknowledged"].append(item)
            continue

        escalated = _is_escalated(e, since)
        if escalated:
            item["escalation"] = escalated
            buckets["escalated"].append(item)
        elif first is not None and first >= since:
            buckets["new"].append(item)
        else:
            buckets["persistent"].append(item)

    # 持续事件：数值变化不大的合并说明，避免每天重复大段内容
    buckets["persistent"] = _merge_persistent(buckets["persistent"])

    ordered = []
    for kind in ("escalated", "new", "resolved", "persistent",
                 "acknowledged", "suppressed"):
        for it in buckets[kind]:
            ordered.append({**it, "change_kind": kind,
                            "_order": _CHANGE_ORDER.get(
                                (kind, it["severity"]), _DEFAULT_ORDER)})
    ordered.sort(key=lambda x: (x["_order"], x.get("rule_code") or ""))
    for it in ordered:
        it.pop("_order", None)

    return {**buckets, "ordered": ordered,
            "counts": {k: len(v) for k, v in buckets.items()}}


def _is_escalated(e: dict, since: datetime) -> dict | None:
    """严重度是否在窗口内上升过。

    用 peak_severity 与当前 severity 比较不可靠（peak 是历史最高，不带时间）。
    这里用登记表的 escalated_at 字段；没有该字段时保守地判为未升级 ——
    宁可漏报升级，也不要把持续事件误报成升级。
    """
    at = _dt(e.get("escalated_at"))
    if at is None or at < since:
        return None
    return {"at": e.get("escalated_at"),
            "from": e.get("previous_severity"),
            "to": e.get("severity")}


def _merge_persistent(items: list[dict]) -> list[dict]:
    """把持续多日、数值变化不大的同类事件合并为一条说明。

    合并键用 rule_code —— 同一条规则在多个标的上持续触发时，
    列成一句「3 个标的持续触发」远比列三段重复文字有用。
    """
    by_rule: dict[str, list[dict]] = {}
    for it in items:
        by_rule.setdefault(it["rule_code"] or "?", []).append(it)
    out: list[dict] = []
    for rule, group in by_rule.items():
        if len(group) == 1:
            out.append(group[0])
            continue
        affected = sorted({str(g.get("affected") or "") for g in group} - {""})
        worst = max(group, key=lambda g: _sev_rank(g["severity"]))
        out.append({
            **worst, "merged_count": len(group),
            "merged_affected": affected,
            "merge_note": (f"{rule} 在 {len(group)} 个对象上持续触发"
                           + (f"（{', '.join(affected[:5])}）" if affected else "")),
        })
    return out


def _sev_rank(sev: str | None) -> int:
    return {"critical": 3, "high": 2, "warning": 1}.get(
        str(sev or "").lower(), 0)


# ======================= 历史基线 =======================

def baseline(values: list[Decimal | None], *, window: int) -> dict:
    """一个窗口的基线统计。样本不足时**明确说不足，不给数字**。"""
    vals = [v for v in values if v is not None]
    n = len(vals)
    if n < MIN_BASELINE_SAMPLES:
        return {"window_days": window, "sample_size": n, "sufficient": False,
                "median": None, "min": None, "max": None,
                "note": f"历史样本不足（{n} 个，需要至少 {MIN_BASELINE_SAMPLES} 个），"
                        "暂不能形成稳定基线"}
    ordered = sorted(vals)
    mid = n // 2
    median = (ordered[mid] if n % 2 else (ordered[mid - 1] + ordered[mid]) / 2)
    return {"window_days": window, "sample_size": n, "sufficient": True,
            "median": _s(median), "min": _s(ordered[0]), "max": _s(ordered[-1]),
            "note": None}


def compare_to_baseline(current: Decimal | None, base: dict) -> dict:
    """当前值相对基线的位置。基线不足时只回传原因，不编造比较。"""
    if not base.get("sufficient"):
        return {"comparable": False, "reason": base.get("note"),
                "direction": None, "delta": None}
    med = _d(base.get("median"))
    if current is None or med is None:
        return {"comparable": False, "reason": "当前值缺失，无法比较",
                "direction": None, "delta": None}
    delta = current - med
    direction = "above" if delta > 0 else ("below" if delta < 0 else "equal")
    return {"comparable": True, "reason": None, "direction": direction,
            "delta": _s(delta), "median": base.get("median"),
            "sample_size": base.get("sample_size")}


# ======================= 上下文构建 =======================

#: 只记录「什么时候跑的」而与数据内容无关的键。**必须全部排除出指纹**。
#:
#: 只排顶层 generated_at 是不够的 —— time_alignment 里还有一份运行时刻，
#: 漏掉它会让每次调用都产生新 hash：06:30 与 06:50 重试各出一份简报，
#: 推送去重也随之失效，直接导致**重复推送**。
_VOLATILE_KEYS = frozenset(("generated_at", "context_hash", "checked_at"))

#: 门禁检查里 `detail` 是给人读的描述，含「账户数据 1.0 小时内」这类
#: 随时刻变化的措辞。指纹只该覆盖**判定结论**（check + passed）——
#: 把描述算进去，同一份数据每分钟都是新指纹。
_DETAIL_BEARING = ("check", "item")


def _strip_volatile(v):
    if isinstance(v, dict):
        # 门禁 / 就绪检查项：只保留结论，丢掉时间相关的描述文字
        if any(k in v for k in _DETAIL_BEARING) and (
                "passed" in v or "ready" in v):
            return {k: x for k, x in v.items()
                    if k not in ("detail", "checked_at")}
        return {k: _strip_volatile(x) for k, x in v.items()
                if k not in _VOLATILE_KEYS}
    if isinstance(v, list):
        return [_strip_volatile(x) for x in v]
    return v


def context_hash(ctx: dict) -> str:
    """上下文指纹。用于幂等：同日、同账户、同 hash、同 prompt 版本不重复生成。

    指纹只覆盖**数据内容**，不含任何运行时刻 —— 否则幂等与推送去重同时失效。
    """
    payload = _strip_volatile({k: v for k, v in ctx.items()
                               if k not in _VOLATILE_KEYS})
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


def build(cfg, db, *, brief_date: str, now: datetime,
          account: str | None = None) -> dict:
    """组装完整的简报上下文。

    **这是 AI 之前的最后一步。** 之后的所有内容都只是把这份 JSON 组织成中文，
    不允许再产生新的数字。
    """
    from . import brief_sources as bs

    account_id = account or getattr(cfg, "ib_account", None)
    refs: list[dict] = []

    acct = bs.account_block(cfg, db, account_id, refs)
    mkt = bs.market_block(db, refs)
    perf = bs.performance_block(cfg, db, brief_date, account_id, refs)
    quality = bs.quality_block(db, acct)

    gate = evaluate_gate(account=acct, market=mkt, performance=perf,
                         quality_issues=quality["issues"], now=now)

    since = now - timedelta(days=1)
    events = bs.event_block(db, since=since, now=now, refs=refs)
    changes = classify_changes(events, since=since, now=now)
    conc = bs.concentration_block(acct, refs)
    hist = bs.baseline_block(db, account_id, now=now, current_account=acct,
                             current_market=mkt)
    top = bs.top_risks(acct, changes, limit=5)

    align = time_alignment(
        account_as_of=(acct or {}).get("data_as_of"),
        market_as_of=(mkt or {}).get("data_as_of"),
        flex_report_to=(perf or {}).get("flex_report_to"), now=now)

    limitations = _limitations(gate, align, perf, conc, hist, quality,
                               ctx_account=acct, market=mkt)

    recommendations = _recommendations_block(db, account_id)
    chain = _hyperliquid_block(cfg, now, refs)

    ctx = {
        "context_version": CONTEXT_VERSION,
        "brief_version": BRIEF_VERSION,
        "brief_date": brief_date,
        "account_id_masked": mask_account(account_id),
        "timezone": str(getattr(cfg, "local_tz", "UTC")),
        "generation_mode": gate["mode"],
        "gate": gate,
        "time_alignment": align,
        "account": acct,
        "market": mkt,
        "hyperliquid": chain,
        "risk_changes": changes,
        "top_risks": top,
        "concentration": conc,
        "performance": perf,
        "historical_comparison": hist,
        "data_quality": quality,
        "data_limitations": limitations,
        # 确定性合成建议（Phase 2）。简报里任何操作倾向只能引用这里的
        # recommendation_id，且不得改动其 action —— 由 brief_validator 强校验。
        "recommendations": recommendations,
        "source_references": refs,
        "generated_at": now.isoformat(),
    }
    ctx["context_hash"] = context_hash(ctx)
    return ctx


def _hyperliquid_block(cfg, now: datetime, refs: list[dict]) -> dict:
    """可选链上资金动向；库不存在/故障时只返回省略状态，不影响简报。"""
    if not getattr(cfg, "hyperliquid_enabled", False):
        return {"status": "DISABLED", "signals": [], "note": "链上情报未启用，本节省略"}
    try:
        from .hyperliquid_db import HyperliquidDatabase
        from .hyperliquid_service import HyperliquidService
        hl_db = HyperliquidDatabase(getattr(cfg, "hl_db_path", "./data/hyperliquid.db"))
        if not hl_db.available():
            return {"status": "UNAVAILABLE", "signals": [], "note": "链上库不可用，本节省略"}
        rows = HyperliquidService(cfg, hl_db).signals(active_only=True, limit=10)
        selected = []
        for row in rows:
            if str(row.get("quality_status") or "") != "READY":
                continue
            signal_at = row.get("signal_at")
            refs.append(source_ref(source_type=SRC_DERIVED, source_table="hl_signal_episodes",
                                    source_id=row.get("signal_id"), field_path="state",
                                    value=row.get("state"), source_version=row.get("rules_version"),
                                    data_as_of=signal_at))
            selected.append({"symbol": row.get("canonical_symbol") or row.get("coin"),
                             "model": row.get("model"), "state": row.get("state"),
                             "score": row.get("score"), "confidence": row.get("confidence"),
                             "signal_at": signal_at, "actionable_at": row.get("actionable_at"),
                             "quality_status": row.get("quality_status")})
        return {"status": "READY", "signals": selected,
                "note": "仅展示 READY 链上信号；不替代现实正股验证，也不自动下单"}
    except Exception:  # noqa: BLE001 — 链上旁路故障只省略本节
        return {"status": "UNAVAILABLE", "signals": [], "note": "链上情报暂不可用，本节省略"}


def _recommendations_block(db, account_id: str | None) -> list[dict]:
    """当前每标的的确定性合成建议，供简报引用。**只暴露可引用字段，不含易变 ID。**

    简报的操作倾向必须引用 recommendation_id 且不得改动 action（见 brief_validator
    的 recommendations_grounded 检查）；这样措辞松绑后仍有机器可核的证据链。
    """
    if not account_id:
        return []
    try:
        rows = db.current_recommendations(account_id)
    except Exception:  # noqa: BLE001 —— 建议缺失不得拖垮简报
        return []
    from . import recommendation_service as reco_
    out = []
    for r in rows:
        out.append({
            "recommendation_id": r["id"],
            "symbol": r["symbol"],
            "action": r["action"],
            "action_label": reco_.ACTION_LABEL.get(r["action"], r["action"]),
            "decided_by": r["decided_by"],
            "reason": r["reason"],
            "current_weight": r["current_weight"],
            "max_weight": r["max_weight"],
            "quantity": r["quantity"],
            "confirmation": r["confirmation"],
            "status": r["status"],
        })
    return out


def _limitations(gate: dict, align: dict, perf: dict | None,
                 conc: dict, hist: dict, quality: dict,
                 ctx_account: dict | None = None,
                 market: dict | None = None) -> list[str]:
    """数据限制清单。宁可啰嗦，也不让人误以为数字比实际更确定。"""
    out: list[str] = []
    out.extend(gate.get("limitations") or [])
    out.extend(gate.get("blocked_reasons") or [])
    out.extend(align.get("notes") or [])
    acct = ctx_account or {}
    if acct.get("exposure_basis"):
        out.append(f"「持仓总市值」口径：{acct['exposure_basis']}")
    if acct.get("positions_market_value") in (None, ""):
        out.append("持仓总市值缺失 —— 未用 净资产减现金 倒推（那个差值含未结算"
                   "资金与多币种折算，不是持仓市值）")
    if perf:
        out.extend(perf.get("limitations") or [])
    out.extend((market or {}).get("limitations") or [])
    na = conc.get("not_applicable_rules") or []
    if na:
        out.append(
            f"{len(na)} 条集中度规则在当前持仓数下不适用 —— "
            "**不适用不等于正常**，这些指标没有判别力，"
            "集中风险改由 HHI、等效持仓数与单仓主导度判断")
    obs = conc.get("observation_period_rules") or []
    if obs:
        out.append(f"{'、'.join(obs)} 于 2026-07 上线，{OBSERVATION_NOTE}")
    for w in BASELINE_WINDOWS:
        b = (hist.get("baselines") or {}).get(f"{w}d") or {}
        if not b.get("sufficient"):
            out.append(f"{w} 日基线：{b.get('note')}")
    if quality.get("issue_count"):
        out.append(f"存在 {quality['issue_count']} 个数据质量问题"
                   f"（严重 {quality.get('critical_count', 0)} 个）")
    seen, uniq = set(), []
    for x in out:
        if x and x not in seen:
            seen.add(x)
            uniq.append(x)
    return uniq
