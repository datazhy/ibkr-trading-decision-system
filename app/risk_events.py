"""统一风险事件模型（纯函数，无 I/O）。

账户引擎、市场引擎、联合视图、以及既有的 R1–R7 行为规则各自产出「触发」，
但前端时间线与通知层需要一套统一词汇。本模块负责这层归一，并定义两件事：

**事件身份（event identity）** —— 什么算「同一个风险」。
    identity = source_type + rule_code + affected(symbol/instrument) + account_id
同一身份持续触发只更新一条记录，不会每分钟新建。

**episode** —— 什么算「同一轮触发」。
    episode = 从首次触发到恢复的一个闭区间。
    恢复后再次触发**开新 episode**（episode_seq + 1），不复用旧的。

为什么选「开新 episode」而不是「重开原事件」：一次持续 3 天的集中度超限，和
一周后又超一次，是两件需要分别复盘的事。把它们合成一条记录会丢掉「持续了多久、
中间恢复过没有」这个最有价值的信息。代价是事件条数变多，但换来可复盘性。

**规则版本隔离**：rule_set_version 不同的触发不合并到同一 episode。阈值从 80% 改到
70% 之后的「超限」与之前的不是同一件事，合并会让历史统计变成不可解释的混合物。

**数据缺失 ≠ 恢复**：本模块只在「本轮确实完成了评估且规则未触发」时才判恢复。
评估本身失败或数据不可靠时，事件保持原状 —— 拿不到数据就宣布风险解除，
是这个系统最危险的一种谎报。
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any

# ---------------- 来源 ----------------
SRC_ACCOUNT = "account"
SRC_POSITION = "position"
SRC_MARKET = "market"
SRC_COMBINED = "combined"
SRC_DATA_QUALITY = "data_quality"
SRC_BEHAVIOR = "behavior"          # 既有 R1–R7 行为纪律规则
SRC_TECHNICAL = "technical"        # 技术面 T1–T8 规则（只进时间线，不推通知）
SOURCE_TYPES = (SRC_ACCOUNT, SRC_POSITION, SRC_MARKET, SRC_COMBINED,
                SRC_DATA_QUALITY, SRC_BEHAVIOR, SRC_TECHNICAL)

# ---------------- 状态 ----------------
ST_OPEN = "open"                   # 风险条件当前仍成立
ST_ACK = "acknowledged"            # 用户已知晓，但风险尚未消失
ST_RESOLVED = "resolved"           # 风险条件已恢复
ST_SUPPRESSED = "suppressed"       # 暂时不通知，但记录保留
STATUSES = (ST_OPEN, ST_ACK, ST_RESOLVED, ST_SUPPRESSED)

#: 仍然「活着」的状态。acknowledged 和 suppressed 都还活着 ——
#: 用户点了「已知晓」不代表风险消失，这是本模块最容易被写错的一条。
ACTIVE_STATUSES = (ST_OPEN, ST_ACK, ST_SUPPRESSED)

# ---------------- 严重度 ----------------
SEV_INFO = "info"
SEV_WARNING = "warning"
SEV_HIGH = "high"
SEV_CRITICAL = "critical"

_SEVERITY_RANK = {SEV_INFO: 0, SEV_WARNING: 1, SEV_HIGH: 2, SEV_CRITICAL: 3}

#: R1–R7 用 INFO/WARN/CRIT，新引擎用 warning/high/critical。
#: 统一到后者，让两套来源能排进同一条时间线。
_LEGACY_SEVERITY = {"INFO": SEV_INFO, "WARN": SEV_WARNING, "CRIT": SEV_CRITICAL}


def normalize_severity(s: str | None) -> str:
    """把任意来源的严重度归一到统一阶梯。无法识别时降级为 warning ——
    宁可当成风险看，也不要因为词汇不认识就静默丢掉。"""
    if not s:
        return SEV_WARNING
    raw = str(s).strip()
    if raw in _SEVERITY_RANK:
        return raw
    up = raw.upper()
    if up in _LEGACY_SEVERITY:
        return _LEGACY_SEVERITY[up]
    low = raw.lower()
    return low if low in _SEVERITY_RANK else SEV_WARNING


def severity_rank(s: str | None) -> int:
    return _SEVERITY_RANK.get(normalize_severity(s), 0)


def severity_label(s: str | None) -> str:
    return {SEV_INFO: "提示", SEV_WARNING: "警告",
            SEV_HIGH: "高风险", SEV_CRITICAL: "严重"}.get(normalize_severity(s), s or "")


def status_label(s: str | None) -> str:
    return {ST_OPEN: "进行中", ST_ACK: "已确认", ST_RESOLVED: "已恢复",
            ST_SUPPRESSED: "已抑制"}.get(s or "", s or "")


def source_label(s: str | None) -> str:
    return {SRC_ACCOUNT: "账户", SRC_POSITION: "持仓", SRC_MARKET: "市场",
            SRC_COMBINED: "联合", SRC_DATA_QUALITY: "数据质量",
            SRC_BEHAVIOR: "行为纪律"}.get(s or "", s or "")


# ---------------- 事件身份 ----------------

def event_identity(*, source_type: str, rule_code: str,
                   affected: str | None = None,
                   account_id: str | None = None) -> str:
    """稳定的事件身份键。同一逻辑风险在任何时刻算出来都一样。

    刻意**不含**严重度与实际值 —— 集中度从 82% 涨到 85%、从 warning 升到 high，
    仍然是同一个风险，只是变严重了。把这些放进身份会让每次数值波动都新建事件。
    """
    parts = [source_type or "", rule_code or "", affected or "", account_id or ""]
    return "|".join(p.replace("|", "/") for p in parts)


def identity_hash(identity: str) -> str:
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16]


def event_id_for(identity: str, episode_seq: int) -> str:
    """事件主键 = 身份哈希 + episode 序号。可读、稳定、可反查。"""
    return f"{identity_hash(identity)}-{episode_seq:04d}"


#: rule_set_version 的形状是 `<算法标签>~<规则内容哈希>`（见
#: risk_engine.rule_set_version）。两部分的含义完全不同：
#:   · 内容哈希 = 阈值与规则定义。它变了才真的「不是同一件事」
#:   · 算法标签 = 实现口径。换测量方式不改变风险本身
_VERSION_SEP = "~"


def rule_semantics_key(version: str | None) -> str | None:
    """取版本里**决定规则语义**的那一段（内容哈希）。

    没有分隔符的旧格式回退到整串比较 —— 对历史行保持保守，不去猜。
    """
    if not version:
        return None
    if _VERSION_SEP in version:
        return version.rsplit(_VERSION_SEP, 1)[1] or None
    return version


def rule_semantics_changed(old: str | None, new: str | None) -> bool:
    """规则语义是否发生变化 —— 这是「要不要开新 episode」的唯一依据。

    **为什么不直接比整个版本串**：当日盈亏 v2 只换分子分母的配对方式，
    阈值 3% 与规则定义一字未动，内容哈希因此完全相同。若按整串比较，
    切换 v2 的那一刻所有在途 episode 都会被判「版本变化」而断开重开，
    凭空产生一批假恢复与假告警 —— 那正是这次修复要消灭的现象，
    不该由修复本身再造一遍。

    阈值从 80% 改到 85% 仍然会断开：那时内容哈希真的变了。
    """
    if old is None or new is None:
        return False
    ko, kn = rule_semantics_key(old), rule_semantics_key(new)
    if ko is None or kn is None:
        return False
    return ko != kn


def _d(v: Any) -> Decimal | None:
    if v is None or v == "":
        return None
    if isinstance(v, Decimal):
        return v
    try:
        return Decimal(str(v))
    except (InvalidOperation, ValueError, TypeError):
        return None


# ---------------- 归一化的触发 ----------------

@dataclass(frozen=True)
class Trigger:
    """一次「规则命中」的统一表示。各引擎的输出都先转成它，再进生命周期。"""
    source_type: str
    event_code: str
    rule_code: str
    category: str
    severity: str
    title: str
    explanation: str
    actual_value: str | None = None
    threshold_value: str | None = None
    unit: str | None = None
    affected_symbol: str | None = None
    affected_instrument: str | None = None
    recommended_check: str | None = None
    rule_version: str | None = None
    rule_set_version: str | None = None
    rule_set_hash: str | None = None
    #: 联合事件同时依赖账户与市场两次评估 —— 两个 id 都要留，
    #: 否则「当时是哪份市场评估触发的」无从查起。
    market_assessment_id: str | None = None
    calculation_version: str | None = None
    snapshot_id: str | None = None
    assessment_id: str | None = None
    data_as_of: str | None = None
    quality_status: str | None = None
    reliable: bool = True
    account_id: str | None = None
    #: 既有 R1–R7 的通知由 engine 负责，运营层只登记不重复推送。
    notification_owner: str = "ops"          # ops / legacy

    @property
    def affected(self) -> str | None:
        return self.affected_symbol or self.affected_instrument

    @property
    def identity(self) -> str:
        return event_identity(source_type=self.source_type, rule_code=self.rule_code,
                              affected=self.affected, account_id=self.account_id)


def _s(v: Any) -> str | None:
    """金额/比例一律转字符串保存 —— JSON number 是双精度，会丢精度。"""
    if v is None:
        return None
    return str(v)


def from_account_event(ev: dict, *, assessment: dict, account_id: str | None) -> Trigger:
    """账户风险引擎事件 -> Trigger。data_quality 类别单独归源，便于筛选。"""
    cat = ev.get("category") or "account"
    src = SRC_DATA_QUALITY if cat == "data_quality" else (
        SRC_POSITION if cat == "position" else SRC_ACCOUNT)
    return Trigger(
        source_type=src,
        event_code=ev.get("event_code") or ev.get("rule_code") or "",
        rule_code=ev.get("rule_code") or "",
        category=cat,
        severity=normalize_severity(ev.get("severity")),
        title=ev.get("title") or "",
        explanation=ev.get("explanation") or "",
        actual_value=_s(ev.get("actual_value")),
        threshold_value=_s(ev.get("threshold_value")),
        unit=ev.get("unit"),
        affected_symbol=ev.get("affected_symbol"),
        recommended_check=ev.get("recommended_check"),
        rule_version=ev.get("rule_version"),
        rule_set_version=assessment.get("rule_set_version"),
        snapshot_id=assessment.get("snapshot_id"),
        assessment_id=assessment.get("assessment_id"),
        data_as_of=assessment.get("data_as_of"),
        quality_status=assessment.get("quality_status"),
        reliable=bool(assessment.get("reliable", True)),
        account_id=account_id,
    )


def from_market_event(ev: dict, *, assessment: dict) -> Trigger:
    cat = ev.get("category") or "market"
    src = SRC_DATA_QUALITY if cat == "data_quality" else SRC_MARKET
    return Trigger(
        source_type=src,
        event_code=ev.get("event_code") or ev.get("rule_code") or "",
        rule_code=ev.get("rule_code") or "",
        category=cat,
        severity=normalize_severity(ev.get("severity")),
        title=ev.get("title") or "",
        explanation=ev.get("explanation") or "",
        actual_value=_s(ev.get("actual_value")),
        threshold_value=_s(ev.get("threshold_value")),
        unit=ev.get("unit"),
        affected_instrument=ev.get("affected_instrument"),
        recommended_check=ev.get("recommended_check"),
        rule_version=ev.get("rule_version"),
        rule_set_version=assessment.get("rule_set_version"),
        assessment_id=assessment.get("assessment_id"),
        data_as_of=assessment.get("data_as_of"),
        quality_status=assessment.get("quality_status"),
        reliable=bool(assessment.get("reliable", True)),
        # 市场事件没有账户维度：不填 account_id，否则同一市场风险会按账户分裂
        account_id=None,
    )


def _rules_version() -> str:
    """R1–R7 的规则集版本。延迟 import 避免循环依赖。"""
    try:
        from .rules import RULES_VERSION
        return RULES_VERSION
    except Exception:  # noqa: BLE001
        return "R1-R7/unknown"


def from_legacy_hit(hit: Any, *, account_id: str | None,
                    now_iso: str | None = None) -> Trigger:
    """既有 R1–R7 的 RuleHit -> Trigger（**只登记，不由运营层通知**）。

    通知仍归 engine._dispatch_alerts —— 那条链路已经上线验证过去重、冷静期、
    日摘要与恢复通知。运营层再推一次就是双发。
    """
    sym = getattr(hit, "symbol", None)
    return Trigger(
        source_type=SRC_BEHAVIOR,
        event_code=getattr(hit, "dedup_key", "") or getattr(hit, "rule_id", ""),
        rule_code=getattr(hit, "rule_id", "") or "",
        category="behavior",
        severity=normalize_severity(getattr(hit, "severity", None)),
        title=getattr(hit, "title", "") or "",
        explanation=getattr(hit, "message", "") or "",
        actual_value=_s(getattr(hit, "value", None)),
        affected_symbol=sym,
        # R1–R7 有自己的版本常量（rules.RULES_VERSION）—— 之前只写了
        # "legacy-r1-r7" 这个占位串，导致简报无法追溯当时的规则定义。
        rule_version=getattr(hit, "rules_version", None) or _rules_version(),
        rule_set_version=_rules_version(),
        data_as_of=now_iso,
        account_id=account_id,
        notification_owner="legacy",
    )


# ---------------- 生命周期决策 ----------------

TRANS_OPENED = "opened"
TRANS_ESCALATED = "escalated"
TRANS_DEESCALATED = "deescalated"
TRANS_RESOLVED = "resolved"
TRANS_REOPENED = "reopened"          # 恢复后再次触发（开新 episode）
TRANS_ACKNOWLEDGED = "acknowledged"
TRANS_SUPPRESSED = "suppressed"
TRANS_UNSUPPRESSED = "unsuppressed"
TRANS_UPDATED = "updated"            # 仍在触发，数值变化但状态与严重度未变


@dataclass
class LifecycleDecision:
    """一次 upsert 的结果。由生命周期函数产出，持久化层照做即可。"""
    action: str                       # create / update
    transition: str
    event_id: str
    identity: str
    episode_seq: int
    previous_status: str | None = None
    new_status: str = ST_OPEN
    previous_severity: str | None = None
    new_severity: str = SEV_WARNING
    reason: str = ""
    #: 是否值得走通知决策。仅状态/严重度变化与首次出现才为 True；
    #: 单纯数值波动不进通知层，由冷静期兜底。
    notify_candidate: bool = False


def decide_upsert(existing: dict | None, trig: Trigger, *,
                  now_iso: str) -> LifecycleDecision:
    """规则本轮触发：新建事件、还是更新既有 episode？

    existing 应为「该 identity 下最新的一条」（可能是 resolved 的）。
    """
    sev = normalize_severity(trig.severity)
    if existing is None:
        seq = 1
        return LifecycleDecision(
            action="create", transition=TRANS_OPENED,
            event_id=event_id_for(trig.identity, seq), identity=trig.identity,
            episode_seq=seq, new_status=ST_OPEN, new_severity=sev,
            reason="首次触发", notify_candidate=True)

    prev_status = existing.get("status") or ST_OPEN
    prev_sev = normalize_severity(existing.get("severity"))
    prev_seq = int(existing.get("episode_seq") or 1)

    # 规则**语义**变了 -> 不是同一件事，开新 episode。
    version_changed = rule_semantics_changed(
        existing.get("rule_set_version"), trig.rule_set_version)

    if prev_status == ST_RESOLVED or version_changed:
        seq = prev_seq + 1
        return LifecycleDecision(
            action="create",
            transition=TRANS_REOPENED,
            event_id=event_id_for(trig.identity, seq), identity=trig.identity,
            episode_seq=seq, previous_status=prev_status, new_status=ST_OPEN,
            previous_severity=prev_sev, new_severity=sev,
            reason=("规则版本变化，按新一轮触发记录"
                    if version_changed else "恢复后再次触发"),
            notify_candidate=True)

    # 仍在同一 episode 内
    if severity_rank(sev) > severity_rank(prev_sev):
        return LifecycleDecision(
            action="update", transition=TRANS_ESCALATED,
            event_id=existing["event_id"], identity=trig.identity,
            episode_seq=prev_seq, previous_status=prev_status,
            # 升级会把 acknowledged 拉回 open：用户确认的是**当时那个**级别的风险
            new_status=ST_OPEN if prev_status == ST_ACK else prev_status,
            previous_severity=prev_sev, new_severity=sev,
            reason=f"严重度升级 {severity_label(prev_sev)} → {severity_label(sev)}",
            notify_candidate=True)

    if severity_rank(sev) < severity_rank(prev_sev):
        return LifecycleDecision(
            action="update", transition=TRANS_DEESCALATED,
            event_id=existing["event_id"], identity=trig.identity,
            episode_seq=prev_seq, previous_status=prev_status,
            new_status=prev_status, previous_severity=prev_sev, new_severity=sev,
            reason=f"严重度降级 {severity_label(prev_sev)} → {severity_label(sev)}",
            notify_candidate=False)

    return LifecycleDecision(
        action="update", transition=TRANS_UPDATED,
        event_id=existing["event_id"], identity=trig.identity,
        episode_seq=prev_seq, previous_status=prev_status, new_status=prev_status,
        previous_severity=prev_sev, new_severity=sev,
        reason="风险持续存在", notify_candidate=False)


def decide_resolve(existing: dict, *, evaluation_complete: bool,
                   reliable: bool) -> LifecycleDecision | None:
    """规则本轮**未**触发：能否判定恢复？

    只有「本轮确实完成了一次可靠评估」才算恢复。评估中断或数据不可靠时返回 None,
    事件维持原状 —— 数据缺失不是风险消失。
    """
    if not evaluation_complete or not reliable:
        return None
    status = existing.get("status") or ST_OPEN
    if status == ST_RESOLVED:
        return None
    return LifecycleDecision(
        action="update", transition=TRANS_RESOLVED,
        event_id=existing["event_id"],
        identity=existing.get("identity") or "",
        episode_seq=int(existing.get("episode_seq") or 1),
        previous_status=status, new_status=ST_RESOLVED,
        previous_severity=normalize_severity(existing.get("severity")),
        new_severity=normalize_severity(existing.get("severity")),
        reason="风险条件已不再成立", notify_candidate=True)
