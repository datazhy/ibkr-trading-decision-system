"""规则效果统计与提前量分析（纯函数）。

precision / recall 在风控里的含义与教科书略有不同，必须写清楚：

    precision = 触发后确实出现目标风险结果的事件数 / 总触发事件数
                「我报警的时候，有多少次真的出事了」

    recall    = 风险结果发生前曾经触发过规则的次数 / 总风险结果次数
                「真的出事的时候，我有多少次提前报了警」

两者都不能单独用来判断规则好坏：
  · precision 高但一年只触发 2 次 —— 大部分风险它都没看见
  · recall 高但每天报 20 次 —— 用户会关掉通知，等于 recall 归零
  · 样本 5 个的 100% precision，说明不了任何事

因此每个指标都必须**带样本量与置信等级**出现，且样本不足时不给强结论。
综合分只用于排序，其构成必须可展开 —— 一个不能解释自己怎么算出来的分数，
不该被用来决定线上阈值。
"""
from __future__ import annotations

import hashlib
import json
import statistics
import uuid
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Sequence

from . import outcome_labels as ol
from . import review_service as rs
from . import trading_calendar as tcal

CALC_VERSION = "perf-v1/2026-07"

# 置信等级
CONF_INSUFFICIENT = "insufficient"
CONF_LOW = "low"
CONF_MODERATE = "moderate"
CONF_STRONG = "strong"

#: 置信等级的样本量门槛。透明规则，不用统计模型 ——
#: 第一版的目的是「不要给出站不住的结论」，而不是精确刻画不确定性。
CONF_THRESHOLDS = ((30, CONF_STRONG), (15, CONF_MODERATE), (5, CONF_LOW))

#: 样本量低于此值时，precision/recall 一律不输出 —— 3 个样本算出的 67%
#: 会被读成「三分之二的可靠性」，而它其实什么都不是。
MIN_SAMPLES_FOR_METRICS = 5

# 提前量分类
CLASS_LEADING = "leading"
CLASS_CONCURRENT = "concurrent"
CLASS_LAGGING = "lagging"
CLASS_NOISY = "noisy"
CLASS_INSUFFICIENT = "insufficient_data"

#: 触发多但命中少 -> noisy。这个比例是「误报到什么程度算噪音」的显式定义。
NOISY_PRECISION_MAX = Decimal("0.20")
NOISY_MIN_TRIGGERS = 10


def _d(v: Any) -> Decimal | None:
    if v is None or v == "":
        return None
    if isinstance(v, Decimal):
        return v
    try:
        return Decimal(str(v))
    except (InvalidOperation, ValueError, TypeError):
        return None


def _s(v) -> str | None:
    return None if v is None else str(v)


def confidence_for(n: int) -> str:
    for threshold, level in CONF_THRESHOLDS:
        if n >= threshold:
            return level
    return CONF_INSUFFICIENT


def _ratio(num: int, den: int) -> Decimal | None:
    """分母为零返回 None，绝不返回伪造的 0。"""
    if den <= 0:
        return None
    return (Decimal(num) / Decimal(den)).quantize(Decimal("0.0001"))


def _mean(values: Sequence[Decimal]) -> Decimal | None:
    if not values:
        return None
    return (sum(values, Decimal("0")) / Decimal(len(values))).quantize(Decimal("0.0001"))


def _median(values: Sequence[Decimal]) -> Decimal | None:
    if not values:
        return None
    return Decimal(str(statistics.median([float(v) for v in values]))
                   ).quantize(Decimal("0.0001"))


# ---------------- 混淆矩阵 ----------------

def confusion(reviews: list[dict], window: int, label_code: str, *,
              baseline_outcomes: int | None = None) -> dict:
    """从复盘记录算混淆矩阵。

    TP：触发了，且窗口内命中标签
    FP：触发了，但窗口内未命中
    未判定：窗口数据不足或现金流污染 —— **单独计数，不算进 FP**，
            否则数据缺失会被统计成规则误报。

    FN/TN 需要「未触发时的结果发生率」作为基线。事件驱动的复盘只看得见触发过的
    时点，看不见未触发的时点，故 FN 只能由 baseline_outcomes 提供；
    没有基线时 recall 返回 None，而不是编一个数字。
    """
    tp = fp = undetermined = 0
    maes, mfes, leads = [], [], []
    for r in reviews:
        try:
            labels = json.loads(r.get("outcome_labels") or "{}")
            outcomes = json.loads(r.get("forward_outcomes") or "{}")
        except (TypeError, ValueError):
            undetermined += 1
            continue
        hit = (labels.get(str(window)) or {}).get(label_code)
        win = outcomes.get(str(window)) or {}
        if win.get("cash_flow_status") == "contaminated":
            undetermined += 1
            continue
        if hit is None:
            undetermined += 1
            continue
        if hit:
            tp += 1
            lt = r.get("lead_time_minutes")
            if lt is not None and r.get("lead_time_class") == rs.LEAD_LEADING:
                leads.append(Decimal(str(lt)))
        else:
            fp += 1
        for key, bucket in (("account_max_adverse", maes),
                            ("account_max_favorable", mfes)):
            v = _d(win.get(key))
            if v is not None:
                bucket.append(v)

    triggered = tp + fp
    fn = None
    if baseline_outcomes is not None:
        fn = max(0, baseline_outcomes - tp)

    return {
        "true_positive": tp, "false_positive": fp,
        "false_negative": fn, "undetermined": undetermined,
        "triggered": triggered,
        "precision": _ratio(tp, triggered) if triggered >= MIN_SAMPLES_FOR_METRICS else None,
        "recall": (_ratio(tp, baseline_outcomes)
                   if (baseline_outcomes and baseline_outcomes >= MIN_SAMPLES_FOR_METRICS)
                   else None),
        "false_positive_rate": (_ratio(fp, triggered)
                                if triggered >= MIN_SAMPLES_FOR_METRICS else None),
        "false_negative_rate": (_ratio(fn, baseline_outcomes)
                                if (fn is not None and baseline_outcomes
                                    and baseline_outcomes >= MIN_SAMPLES_FOR_METRICS)
                                else None),
        "avg_mae": _mean(maes), "avg_mfe": _mean(mfes),
        "avg_lead_minutes": _mean(leads), "median_lead_minutes": _median(leads),
        "sample_size": triggered,
    }


def classify_lead(reviews: list[dict], precision: Decimal | None,
                  triggered: int) -> str:
    """规则的提前量分类。依据透明且可配置。"""
    if triggered < MIN_SAMPLES_FOR_METRICS:
        return CLASS_INSUFFICIENT
    if (precision is not None and precision <= NOISY_PRECISION_MAX
            and triggered >= NOISY_MIN_TRIGGERS):
        return CLASS_NOISY
    classes = [r.get("lead_time_class") for r in reviews if r.get("lead_time_class")]
    if not classes:
        return CLASS_INSUFFICIENT
    counts = {c: classes.count(c) for c in set(classes)}
    # 只有当 leading 占多数时才叫 leading —— 「偶尔提前」不是提前预警能力
    top = max(counts, key=lambda k: counts[k])
    if top == rs.LEAD_NO_OUTCOME:
        return CLASS_NOISY if triggered >= NOISY_MIN_TRIGGERS else CLASS_INSUFFICIENT
    return {rs.LEAD_LEADING: CLASS_LEADING,
            rs.LEAD_CONCURRENT: CLASS_CONCURRENT,
            rs.LEAD_LAGGING: CLASS_LAGGING}.get(top, CLASS_INSUFFICIENT)


# ---------------- 综合参考分 ----------------

def composite_score(precision, recall, avg_lead_minutes, triggers_per_month,
                    sample_size: int) -> tuple[Decimal | None, dict]:
    """综合参考分（0–100）。**只用于排序，不得自动决定线上阈值。**

    构成全部展开，任何一项都能单独查看。样本不足时返回 None ——
    给一个「样本 3 个、得分 92」的绿色高分，比不给分更有害。
    """
    comps = {
        "precision_component": None, "recall_component": None,
        "lead_time_component": None, "noise_penalty": None,
        "sample_confidence": confidence_for(sample_size),
    }
    if sample_size < MIN_SAMPLES_FOR_METRICS:
        comps["note"] = f"样本量 {sample_size} 不足 {MIN_SAMPLES_FOR_METRICS}，不计分"
        return None, comps

    p = _d(precision) or Decimal("0")
    r = _d(recall)
    lead = _d(avg_lead_minutes)
    tpm = _d(triggers_per_month) or Decimal("0")

    comps["precision_component"] = str((p * Decimal("40")).quantize(Decimal("0.1")))
    comps["recall_component"] = (
        str((r * Decimal("30")).quantize(Decimal("0.1"))) if r is not None else None)
    # 提前量：1 个交易日（约 390 分钟交易时段）封顶 20 分
    if lead is not None and lead > 0:
        capped = min(lead / Decimal("7200"), Decimal("1"))     # 5 交易日封顶
        comps["lead_time_component"] = str((capped * Decimal("20")).quantize(Decimal("0.1")))
    # 噪音惩罚：每月超过 10 次触发开始扣分，最多扣 20
    if tpm > 10:
        penalty = min((tpm - 10) * Decimal("2"), Decimal("20"))
        comps["noise_penalty"] = str(-penalty.quantize(Decimal("0.1")))

    total = Decimal("0")
    for k in ("precision_component", "recall_component", "lead_time_component",
              "noise_penalty"):
        v = _d(comps.get(k))
        if v is not None:
            total += v
    total = max(Decimal("0"), min(total, Decimal("100")))
    return total.quantize(Decimal("0.1")), comps


# ---------------- 单条规则 ----------------

def compute_rule_performance(db, rule_code: str, *, window: int, label_code: str,
                             cfg=None, baseline_outcomes: int | None = None,
                             current_threshold: str | None = None,
                             now: datetime | None = None) -> dict:
    """某条规则在 (窗口, 标签) 下的完整效果统计。"""
    now = now or datetime.now(timezone.utc)
    reviews = db.reviews_for_rule(rule_code, rs.CALC_VERSION)
    all_reviews, _ = db.query_reviews(version=rs.CALC_VERSION, rule_code=rule_code,
                                      limit=1000)

    cm = confusion(reviews, window, label_code, baseline_outcomes=baseline_outcomes)
    durations = [Decimal(str(r["duration_minutes"])) for r in all_reviews
                 if r.get("duration_minutes") is not None]
    firsts = sorted(r["first_detected_at"] for r in all_reviews
                    if r.get("first_detected_at"))
    span_months = None
    if len(firsts) >= 2:
        mins = tcal.minutes_between(firsts[0], firsts[-1])
        if mins and mins > 0:
            span_months = Decimal(mins) / Decimal(43200)      # 30 天
    tpm = (Decimal(len(all_reviews)) / span_months
           if span_months and span_months > 0 else None)

    source = (all_reviews[0].get("source_type") if all_reviews else None)
    escalated = sum(1 for r in all_reviews
                    if r.get("peak_severity") in ("high", "critical"))
    critical = sum(1 for r in all_reviews if r.get("peak_severity") == "critical")
    resolved = sum(1 for r in all_reviews if r.get("resolved_at"))
    n_all = len(all_reviews)

    notif = 0
    try:
        notif = db.notification_count_for_rule(rule_code)
    except Exception:  # noqa: BLE001
        notif = 0

    score, comps = composite_score(cm["precision"], cm["recall"],
                                   cm["avg_lead_minutes"], tpm, cm["sample_size"])

    return {
        "perf_id": _perf_id(rule_code, window, label_code),
        "rule_code": rule_code,
        "rule_set_version": (all_reviews[0].get("rule_set_version")
                             if all_reviews else None),
        "source_type": source, "category": None,
        "observation_window": window, "outcome_label": label_code,
        "date_start": firsts[0] if firsts else None,
        "date_end": firsts[-1] if firsts else None,
        "episodes_total": n_all,
        "episodes_reviewed": len(reviews),
        "episodes_insufficient": cm["undetermined"],
        "avg_duration_minutes": _s(_mean(durations)),
        "median_duration_minutes": _s(_median(durations)),
        "triggers_per_month": _s(tpm.quantize(Decimal("0.01")) if tpm else None),
        "notifications_total": notif,
        "notifications_per_hit": _s(_ratio(notif, cm["true_positive"])
                                    if cm["true_positive"] else None),
        "outcome_rate_when_triggered": _s(cm["precision"]),
        "outcome_rate_when_not_triggered": None,   # 需基线，见 backtest 模块
        "precision_value": _s(cm["precision"]),
        "recall_value": _s(cm["recall"]),
        "false_positive_rate": _s(cm["false_positive_rate"]),
        "false_negative_rate": _s(cm["false_negative_rate"]),
        "avg_lead_time_minutes": _s(cm["avg_lead_minutes"]),
        "median_lead_time_minutes": _s(cm["median_lead_minutes"]),
        "avg_mae": _s(cm["avg_mae"]), "avg_mfe": _s(cm["avg_mfe"]),
        "escalation_rate": _s(_ratio(escalated, n_all)),
        "resolution_rate": _s(_ratio(resolved, n_all)),
        "critical_rate": _s(_ratio(critical, n_all)),
        "lead_class": classify_lead(reviews, cm["precision"], cm["sample_size"]),
        "sample_size": cm["sample_size"],
        "confidence": confidence_for(cm["sample_size"]),
        "composite_score": _s(score),
        "score_components": json.dumps(comps, ensure_ascii=False),
        "current_threshold": current_threshold,
        "calculation_version": CALC_VERSION,
        "computed_at": now.isoformat(),
    }


def _perf_id(rule_code: str, window: int, label: str) -> str:
    raw = f"{rule_code}|{window}|{label}|{CALC_VERSION}"
    return hashlib.sha256(raw.encode()).hexdigest()[:20]


def compute_all(cfg, db, *, windows=tcal.FORWARD_WINDOWS,
                now: datetime | None = None) -> dict:
    """为所有出现过的规则计算效果。异常全吞。"""
    from . import risk_engine as re_, market_engine as me_, combined_events as ce_
    stats = {"rules": 0, "rows": 0, "errors": 0}
    thresholds = _current_thresholds(cfg)
    try:
        rows, _ = db.query_reviews(version=rs.CALC_VERSION, limit=5000)
        by_rule: dict[str, str] = {}
        for r in rows:
            if r.get("rule_code") and r.get("source_type"):
                by_rule[r["rule_code"]] = r["source_type"]
        for rule_code, source in by_rule.items():
            labels = ol.labels_for_source(source, ol.default_labels(cfg))
            if not labels:
                continue
            stats["rules"] += 1
            for window in windows:
                for label in labels:
                    try:
                        p = compute_rule_performance(
                            db, rule_code, window=window,
                            label_code=label.label_code, cfg=cfg,
                            current_threshold=thresholds.get(rule_code),
                            now=now)
                        db.save_rule_performance(p)
                        stats["rows"] += 1
                    except Exception:  # noqa: BLE001
                        stats["errors"] += 1
    except Exception:  # noqa: BLE001
        stats["errors"] += 1
    return stats


def _current_thresholds(cfg) -> dict[str, str]:
    """当前线上阈值。排行榜要显示它 —— 看效果而不知道当前设定是多少，无从判断。"""
    out: dict[str, str] = {}
    try:
        from . import risk_engine as re_
        for r in re_.default_rules(cfg):
            out[r.rule_code] = str(getattr(r, "threshold", "") or "")
    except Exception:  # noqa: BLE001
        pass
    try:
        from . import market_engine as me_
        for r in me_.default_rules(cfg):
            out[r.rule_code] = str(getattr(r, "threshold", "") or "")
    except Exception:  # noqa: BLE001
        pass
    try:
        from . import combined_events as ce_
        for r in ce_.default_rules(cfg):
            out[r.rule_code] = str(r.threshold)
    except Exception:  # noqa: BLE001
        pass
    return out


# ==================== 研究样本排除 ====================

def apply_research_exclusions(db, rows: list[dict], *,
                              include_excluded: bool = False,
                              metric: str = "precision",
                              merge_artificial: bool = True) -> dict:
    """规则效果统计前的样本过滤。

    默认排除被标注的样本，并把被人为切断的 episode 在**研究视图**里合并 ——
    一段被探测切成两半的真实风险，如果按两个样本统计，
    precision 的分母凭空多一个，episode 时长也全错。
    """
    from . import event_annotations as ea

    merged_info = {"merged_count": 0, "merged_detail": []}
    if merge_artificial:
        m = ea.merge_artificial_splits(db, rows)
        rows, merged_info = m["events"], m

    res = ea.filter_for_research(db, rows, include_excluded=include_excluded,
                                 metric=metric)
    res["stats"]["research_view_merged_count"] = merged_info["merged_count"]
    res["merged_detail"] = merged_info.get("merged_detail", [])
    return res
