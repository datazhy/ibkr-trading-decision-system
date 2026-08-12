"""阈值建议生成（纯函数）。

**系统只生成建议，永不修改线上配置。** 阈值是用户对自己风险偏好的表达，
一个统计程序没有资格替他决定 —— 何况所谓"最优阈值"往往只是对这段历史的过拟合。

每条建议必须**同时给出收益与代价**。只说「降到 40% 后 recall 提升 12%」而不说
「通知量增加 68%」，是在诱导用户做出他知情后不会同意的改动。

建议档位：
    keep              —— 当前阈值没有明显问题，或改动的代价大于收益
    consider_raise    —— 触发过密且误报高，提高阈值可能减少噪音
    consider_lower    —— 漏报明显且通知量还有余量
    insufficient_data —— 样本不足以支持任何调整
    unstable          —— 训练期好、验证期差，疑似过拟合，不建议据此调整
"""
from __future__ import annotations

import json
from decimal import Decimal, InvalidOperation
from typing import Any

from . import backtest as bt
from . import rule_performance as rp

REC_KEEP = "keep"
REC_RAISE = "consider_raise"
REC_LOWER = "consider_lower"
REC_INSUFFICIENT = "insufficient_data"
REC_UNSTABLE = "unstable"

#: 通知量增幅超过这个比例时，即使 recall 提升也不建议调整 ——
#: 告警疲劳会让整套系统失效，那个代价大于多抓到的几次风险。
MAX_ACCEPTABLE_NOTIFICATION_INCREASE = Decimal("0.30")
#: recall 提升低于这个幅度不值得为之增加噪音。
MIN_MEANINGFUL_RECALL_GAIN = Decimal("0.05")
#: precision 低于此值且触发频繁，考虑提高阈值。
LOW_PRECISION = Decimal("0.30")
#: 每月触发超过这个次数算「过密」。
HIGH_FREQUENCY_PER_MONTH = Decimal("8")


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


def _pct(v) -> str:
    d = _d(v)
    return "—" if d is None else f"{d:.1%}"


def recommend(rule_code: str, results: list[dict], *,
              window: int, label_code: str) -> dict:
    """对一条规则给出阈值建议。

    results：该规则在各候选阈值、各区间段上的回测结果。
    """
    rows = [r for r in results
            if r.get("rule_code") == rule_code
            and int(r.get("observation_window") or 0) == window
            and r.get("outcome_label") == label_code]
    full = [r for r in rows if r.get("segment") == "full"]
    train = {r["threshold_value"]: r for r in rows if r.get("segment") == "train"}
    valid = {r["threshold_value"]: r for r in rows if r.get("segment") == "validation"}

    current = next((r for r in full if r.get("is_current_threshold")), None)
    if not current:
        return _insufficient(rule_code, window, label_code,
                             "回测结果中没有当前阈值的对照，无法比较")

    cur_thr = current["threshold_value"]
    sample = int(current.get("sample_size") or 0)

    base = {
        "rule_code": rule_code,
        "observation_window": window,
        "outcome_label": label_code,
        "current_threshold": cur_thr,
        "current_performance": _perf_view(current),
        "sample_size": sample,
        "confidence": current.get("confidence"),
        "candidates": [_candidate_view(r, current, train, valid) for r in full
                       if not r.get("is_current_threshold")],
    }

    # ---- 样本不足：直接给出 insufficient_data ----
    if sample < rp.MIN_SAMPLES_FOR_METRICS:
        return {**base, "recommendation": REC_INSUFFICIENT,
                "reasons": [f"当前阈值下只有 {sample} 个可用样本，"
                            f"低于最低要求 {rp.MIN_SAMPLES_FOR_METRICS} 个"],
                "risks": ["在这个样本量上做的任何阈值调整都无法与随机波动区分"],
                "conclusion": "数据不足以支持调整。"}

    # ---- 稳定性：训练期好、验证期差 -> unstable ----
    t_cur, v_cur = train.get(cur_thr), valid.get(cur_thr)
    stability, stability_note = ("insufficient_data", "无训练/验证对照")
    if t_cur and v_cur:
        stability, stability_note = bt.stability_verdict(t_cur, v_cur)
    base["stability"] = stability
    base["stability_note"] = stability_note

    if stability == "unstable":
        return {**base, "recommendation": REC_UNSTABLE,
                "reasons": [stability_note],
                "risks": ["在验证期已经退化的表现上调整阈值，"
                          "很可能是在拟合过去而不是防范未来"],
                "conclusion": "表现不稳定，本轮不建议调整阈值。"}

    cur_p = _d(current.get("precision_value"))
    cur_r = _d(current.get("recall_value"))
    cur_tpm = _d(current.get("triggers_per_month"))
    cur_notif = int(current.get("estimated_notifications") or 0)

    reasons: list[str] = []
    risks: list[str] = []

    # ---- 触发过密且误报高 -> 考虑提高 ----
    if (cur_p is not None and cur_p < LOW_PRECISION
            and cur_tpm is not None and cur_tpm > HIGH_FREQUENCY_PER_MONTH):
        better = _best_raise(full, current)
        reasons.append(f"当前 precision 仅 {_pct(cur_p)}，"
                       f"每月触发 {cur_tpm:.1f} 次，噪音偏高")
        if better:
            reasons.append(
                f"提高到 {better['threshold_value']} 后 precision "
                f"{_pct(better.get('precision_value'))}，"
                f"通知量从 {cur_notif} 降到 {better.get('estimated_notifications')}")
            risks.append(
                f"代价：recall 从 {_pct(cur_r)} 变为 "
                f"{_pct(better.get('recall_value'))}，"
                f"最长连续漏报从 {current.get('max_consecutive_misses')} 次变为 "
                f"{better.get('max_consecutive_misses')} 次")
            return {**base, "recommendation": REC_RAISE,
                    "suggested_threshold": better["threshold_value"],
                    "suggested_performance": _perf_view(better),
                    "reasons": reasons, "risks": risks,
                    "conclusion": (f"可考虑将阈值从 {cur_thr} 提高到 "
                                   f"{better['threshold_value']}，"
                                   "但请确认愿意接受召回下降。")}

    # ---- 漏报明显且通知量有余量 -> 考虑降低 ----
    better = _best_lower(full, current, cur_notif)
    if better:
        gain = _d(better.get("recall_value")) - (cur_r or Decimal("0"))
        notif_pct = _notif_change_pct(better, cur_notif)
        reasons.append(
            f"降到 {better['threshold_value']} 后 recall 从 {_pct(cur_r)} "
            f"提升到 {_pct(better.get('recall_value'))}（+{_pct(gain)}）")
        risks.append(
            f"代价：通知量从 {cur_notif} 增加到 "
            f"{better.get('estimated_notifications')}"
            f"（{_pct(notif_pct)}），precision 从 {_pct(cur_p)} 变为 "
            f"{_pct(better.get('precision_value'))}")
        return {**base, "recommendation": REC_LOWER,
                "suggested_threshold": better["threshold_value"],
                "suggested_performance": _perf_view(better),
                "reasons": reasons, "risks": risks,
                "conclusion": (f"可考虑将阈值从 {cur_thr} 降到 "
                               f"{better['threshold_value']}，"
                               "前提是能接受通知量上升。")}

    # ---- 其余情况保持 ----
    reasons.append(f"当前 precision {_pct(cur_p)}、recall {_pct(cur_r)}、"
                   f"每月触发 {cur_tpm:.1f} 次" if cur_tpm is not None
                   else f"当前 precision {_pct(cur_p)}、recall {_pct(cur_r)}")
    reasons.append("候选阈值中没有一个在不显著增加噪音的前提下带来有意义的召回提升")
    if stability != "insufficient_data":
        reasons.append(stability_note)
    return {**base, "recommendation": REC_KEEP,
            "reasons": reasons,
            "risks": ["保持不变意味着继续接受当前的漏报水平"],
            "conclusion": f"建议维持当前阈值 {cur_thr}。"}


def _insufficient(rule_code, window, label_code, why) -> dict:
    return {"rule_code": rule_code, "observation_window": window,
            "outcome_label": label_code, "recommendation": REC_INSUFFICIENT,
            "reasons": [why], "risks": [], "candidates": [],
            "conclusion": "数据不足以支持调整。", "sample_size": 0,
            "confidence": rp.CONF_INSUFFICIENT}


def _perf_view(r: dict) -> dict:
    return {"threshold": r.get("threshold_value"),
            "trigger_count": r.get("trigger_count"),
            "triggers_per_month": r.get("triggers_per_month"),
            "precision": r.get("precision_value"),
            "recall": r.get("recall_value"),
            "false_positive_rate": r.get("false_positive_rate"),
            "avg_lead_time_days": r.get("avg_lead_time_days"),
            "max_consecutive_misses": r.get("max_consecutive_misses"),
            "estimated_notifications": r.get("estimated_notifications"),
            "sample_size": r.get("sample_size"),
            "confidence": r.get("confidence")}


def _candidate_view(r: dict, current: dict, train: dict, valid: dict) -> dict:
    t, v = train.get(r["threshold_value"]), valid.get(r["threshold_value"])
    stab, note = (("insufficient_data", "无训练/验证对照") if not (t and v)
                  else bt.stability_verdict(t, v))
    try:
        delta = json.loads(r.get("delta_vs_current") or "null")
    except (TypeError, ValueError):
        delta = None
    return {**_perf_view(r), "delta_vs_current": delta,
            "train_precision": (t or {}).get("precision_value"),
            "validation_precision": (v or {}).get("precision_value"),
            "stability": stab, "stability_note": note}


def _notif_change_pct(cand: dict, cur_notif: int) -> Decimal | None:
    if not cur_notif:
        return None
    c = Decimal(int(cand.get("estimated_notifications") or 0))
    return (c - Decimal(cur_notif)) / Decimal(cur_notif)


def _best_raise(full: list[dict], current: dict) -> dict | None:
    """比当前更高的阈值里，precision 提升最多且样本仍足够的那个。"""
    cur_thr = _d(current["threshold_value"])
    cur_p = _d(current.get("precision_value")) or Decimal("0")
    best = None
    for r in full:
        thr = _d(r["threshold_value"])
        if thr is None or cur_thr is None or thr <= cur_thr:
            continue
        if int(r.get("sample_size") or 0) < rp.MIN_SAMPLES_FOR_METRICS:
            continue
        p = _d(r.get("precision_value"))
        if p is None or p <= cur_p:
            continue
        if best is None or p > _d(best.get("precision_value")):
            best = r
    return best


def _best_lower(full: list[dict], current: dict, cur_notif: int) -> dict | None:
    """比当前更低的阈值里，recall 提升有意义**且**通知量增幅可接受的那个。

    两个条件缺一不可 —— 只看 recall 会推荐出一个把用户淹没在通知里的阈值。
    """
    cur_thr = _d(current["threshold_value"])
    cur_r = _d(current.get("recall_value"))
    if cur_r is None:
        return None
    best = None
    for r in full:
        thr = _d(r["threshold_value"])
        if thr is None or cur_thr is None or thr >= cur_thr:
            continue
        if int(r.get("sample_size") or 0) < rp.MIN_SAMPLES_FOR_METRICS:
            continue
        rr = _d(r.get("recall_value"))
        if rr is None or (rr - cur_r) < MIN_MEANINGFUL_RECALL_GAIN:
            continue
        pct = _notif_change_pct(r, cur_notif)
        if pct is not None and pct > MAX_ACCEPTABLE_NOTIFICATION_INCREASE:
            continue        # 召回涨了，但代价是把用户淹没 —— 不推荐
        if best is None or rr > _d(best.get("recall_value")):
            best = r
    return best


def recommend_all(db, run_id: str, *, window: int = 5,
                  label_code: str | None = None) -> list[dict]:
    """一次回测的全部规则建议。"""
    results = db.backtest_results(run_id)
    if not results:
        return []
    labels = {r.get("outcome_label") for r in results if r.get("outcome_label")}
    label = label_code or (sorted(labels)[0] if labels else None)
    if not label:
        return []
    out = []
    for rule_code in sorted({r["rule_code"] for r in results}):
        try:
            out.append(recommend(rule_code, results, window=window,
                                 label_code=label))
        except Exception:  # noqa: BLE001
            out.append(_insufficient(rule_code, window, label, "建议生成失败"))
    return out
