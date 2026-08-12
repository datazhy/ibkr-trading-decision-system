"""当日盈亏观测的落库与影子比较。

**为什么在写 PnL 快照的那一刻构建观测，而不是在风险评估里构建？**

因为那一刻分子和分母本来就同时在手上：`sync._write_pnl_snapshot` 拿到的
`daily`（分子）与 `equity`（分母）来自同一次 IBKR 取数、共享同一个 `ts` 和
`sync_run_id`。在这里配对是**构造上原子**的，不需要事后去库里找。

反过来，`risk_service.build_context` 里配不齐 —— 风险评估跑在 PnL 快照写入
之前，那时本轮的分子还不存在。历史上它用 `db.latest_pnl()` 拿到上一周期的分子
去配本周期的分母，这正是 `ACC_DAILY_LOSS_HIGH` 24 小时开合 11 次的成因。

影子期的纪律：**这里只写观测和比较，绝不触碰事件、通知或 alert_state。**
旧指标继续独占驱动权，直到拿到完整交易日的对比报告并获得切换授权。
"""
from __future__ import annotations

import json
import logging
from decimal import Decimal

from . import daily_pnl_observation as dpo

log = logging.getLogger("riskdash")


def _d(v):
    if v is None or v == "":
        return None
    if isinstance(v, Decimal):
        return v
    try:
        return Decimal(str(v))
    except Exception:  # noqa: BLE001
        return None


def build_from_cycle(cfg, db, *, account: str, ts: str, session_et: str,
                     run_id: str, snapshot_id: str | None,
                     daily_pnl, net_liquidation, source: str, basis: str | None,
                     quality: str, currency: str | None = None,
                     flow_coverage_ok: bool | None = None,
                     external_cash_flow_adjustment=None,
                     now=None) -> dpo.DailyPnlObservation:
    """用一次采集周期的两侧数据闭合一个观测。**纯组装，不写库。**

    分子与分母共享 `run_id` 与 `ts` —— 这不是巧合，是调用点的前提条件：
    只有在 `_write_pnl_snapshot` 这种「两个数同时在手」的地方才允许调用。
    """
    prev = None
    try:
        prev = db.latest_daily_pnl_observation(account)
    except Exception:  # noqa: BLE001 — 拿不到上一条只影响 source_changed 标记
        prev = None

    common = {"correlation_id": run_id, "session_date": session_et,
              "currency": currency, "snapshot_id": snapshot_id,
              "observed_at": ts}
    return dpo.build(
        account_id=account,
        numerator={**common, "daily_pnl": daily_pnl, "source": source,
                   "basis": basis, "quality": quality,
                   "flow_coverage_ok": flow_coverage_ok,
                   "external_cash_flow_adjustment": external_cash_flow_adjustment},
        denominator={**common, "net_liquidation": net_liquidation},
        observed_at=ts,
        correlation_id=run_id,
        previous_source=(prev["current_source"] if prev else None),
        previous_basis=(prev["basis"] if prev else None),
        skew_tolerance_seconds=getattr(cfg, "daily_pnl_skew_tolerance_seconds", 90),
        max_age_seconds=getattr(cfg, "daily_pnl_max_age_seconds", 600),
        now=now)


def _old_side(db, snapshot_id: str | None) -> dict:
    """把同一份快照上「旧指标」的结论取出来，用于比较。

    取的是已经落库的评估结果，不重算 —— 重算会引入新的时点，比较就不干净了。
    """
    out = {"ratio": None, "quality": None, "outcome": None, "source": None}
    if not snapshot_id:
        return out
    try:
        row = db.assessment_for_snapshot(snapshot_id)
    except Exception:  # noqa: BLE001
        row = None
    if row is None:
        return out
    r = dict(row)
    out["quality"] = r.get("data_quality_status")
    try:
        for o in json.loads(r.get("rule_outcomes") or "[]"):
            if isinstance(o, dict) and o.get("rule_code") == "ACC_DAILY_LOSS_HIGH":
                out["outcome"] = o.get("outcome")
                out["ratio"] = _d(o.get("actual_value"))
                break
    except Exception:  # noqa: BLE001
        pass
    try:
        m = json.loads(r.get("calculated_metrics") or "{}")
        out["source"] = ((m.get("pnl") or {}).get("source")
                         or (m.get("pnl") or {}).get("pnl_source"))
    except Exception:  # noqa: BLE001
        pass
    return out


def record(cfg, db, obs: dpo.DailyPnlObservation, *,
           snapshot_id: str | None, threshold) -> dict:
    """落库观测 + 影子比较，并打一条结构化日志。

    **影子期语义**：返回值只用于诊断与报告。调用方不得据此改事件状态。
    """
    written = compared = False
    try:
        written = db.insert_daily_pnl_observation(obs.as_row())
    except Exception:  # noqa: BLE001 — 观测落库失败不得影响账户数据与告警
        log.warning("当日盈亏观测落库失败（不影响同步与告警）", exc_info=True)

    old = _old_side(db, snapshot_id)
    row = None
    try:
        row = dpo.comparison_row(
            obs=obs, old_ratio=old["ratio"],
            old_quality=old["quality"] or "unknown",
            old_outcome=old["outcome"] or dpo.OUT_INSUFFICIENT,
            threshold=_d(threshold) or Decimal("0.03"),
            old_source=old["source"])
        compared = db.insert_daily_pnl_comparison(row)
    except Exception:  # noqa: BLE001
        log.warning("当日盈亏影子比较落库失败（不影响同步与告警）", exc_info=True)

    f = obs.log_fields()
    log.info(
        "当日盈亏观测 corr=%s source=%s basis=%s session=%s skew=%ss "
        "reliable=%s quality=%s reasons=%s switched=%s outcome=%s "
        "transition_allowed=%s block=%s",
        (f["correlation_id"] or "-")[:8], f["source"], f["basis"] or "-",
        f["session_date"], f["observation_skew_seconds"], f["reliable"],
        f["quality_status"], f["quality_reasons"], f["source_changed"],
        (row or {}).get("new_outcome", "-"), f["event_transition_allowed"],
        f["event_transition_block_reason"])
    return {"observation_written": written, "comparison_written": compared,
            "divergence_type": (row or {}).get("divergence_type"),
            "new_outcome": (row or {}).get("new_outcome"),
            "old_outcome": (row or {}).get("old_outcome")}


# ======================= old / v2 生效区间 =======================

def version_ranges(db, since_iso: str) -> list[dict]:
    """把窗口内的评估按 `calculation_version` 切成若干生效区间。

    回滚（把开关翻回 false）**不修改任何历史** —— 历史评估仍写着当时那个版本。
    所以「某条记录当时是哪条口径算的」靠版本串就能读出来，不需要另存开关历史，
    也不会因为开关来回翻而丢失可追溯性。

    返回按时间正序的 [{calculation_version, mode, first_at, last_at, count}]。
    """
    from . import daily_pnl_v2 as dpv2

    try:
        rows = db.assessment_versions_since(since_iso)
    except Exception:  # noqa: BLE001 — 诊断接口不得因此 500
        return []

    out: list[dict] = []
    for r in rows:
        r = dict(r)
        ver = r.get("calculation_version") or "(unknown)"
        mode = (dpv2.MODE_V2 if ver.startswith(dpv2.CALC_VERSION_V2)
                else dpv2.MODE_OLD)
        if out and out[-1]["calculation_version"] == ver:
            out[-1]["last_at"] = r.get("last_at") or out[-1]["last_at"]
            out[-1]["count"] += int(r.get("n") or 0)
            continue
        out.append({"calculation_version": ver, "mode": mode,
                    "first_at": r.get("first_at"), "last_at": r.get("last_at"),
                    "count": int(r.get("n") or 0)})
    return out


# ======================= 影子期汇总 =======================

def shadow_summary(db, since_iso: str, *, threshold=Decimal("0.03")) -> dict:
    """影子期对比汇总。**只读**，供诊断 API 与验收报告使用。

    episode 模拟用同一份序列分别重放旧指标与 v2：
      · 旧指标：有 ratio 就按 `>阈值` 开、`<=阈值` 关（复刻现行单阈值行为）
      · v2：只有 reliable 样本才允许迁移；insufficient 样本**保持原状态**
    这条差别正是修复的全部内容 —— 不含滞回，不含连续确认。
    """
    obs = [dict(r) for r in db.daily_pnl_observations_since(since_iso)]
    cmps = [dict(r) for r in db.daily_pnl_comparisons_since(since_iso)]
    thr = _d(threshold) or Decimal("0.03")

    total = len(obs)
    reliable = sum(1 for o in obs if o.get("reliable"))
    insufficient = sum(1 for o in obs
                       if o.get("quality_status") == dpo.Q_INSUFFICIENT)
    degraded = sum(1 for o in obs if o.get("quality_status") == dpo.Q_DEGRADED)
    switches = sum(1 for o in obs if o.get("source_changed"))
    skew_over = sum(1 for o in obs
                    if dpo.R_SKEW_EXCEEDED in (o.get("quality_reasons") or ""))
    cross = sum(1 for o in obs
                if dpo.R_CROSS_CYCLE in (o.get("quality_reasons") or ""))

    diverge = {}
    for c in cmps:
        k = c.get("divergence_type") or "?"
        diverge[k] = diverge.get(k, 0) + 1
    outcome_diff = diverge.get(dpo.DIV_OUTCOME, 0)

    # ---- episode 重放 ----
    def replay(gate_reliable: bool) -> dict:
        eps = 0
        opened = resolved = 0
        st = False
        blocked_open = blocked_resolve = 0
        for o in obs:
            ratio = _d(o.get("daily_loss_ratio"))
            ok = bool(o.get("reliable"))
            if gate_reliable and not ok:
                # 数据不闭合：保持原状态。既不开也不关。
                if ratio is not None and ratio > thr and not st:
                    blocked_open += 1
                elif st and (ratio is None or ratio <= thr):
                    blocked_resolve += 1
                continue
            if ratio is None:
                continue
            if not st and ratio > thr:
                st = True; eps += 1; opened += 1
            elif st and ratio <= thr:
                st = False; resolved += 1
        return {"episodes": eps, "opened": opened, "resolved": resolved,
                "blocked_opens": blocked_open, "blocked_resolves": blocked_resolve}

    old_sim = replay(False)
    new_sim = replay(True)

    # ---- 真实持续 >3% 段的漏检 ----
    sig = [(_d(o.get("daily_loss_ratio")), bool(o.get("reliable"))) for o in obs]
    runs, i = [], 0
    while i < len(sig):
        if sig[i][0] is not None and sig[i][0] > thr:
            j = i
            while j + 1 < len(sig) and sig[j + 1][0] is not None and sig[j + 1][0] > thr:
                j += 1
            runs.append((i, j))
            i = j + 1
        else:
            i += 1
    sustained = [r for r in runs if r[1] - r[0] + 1 >= 5]
    missed = sum(1 for a, b in sustained
                 if not any(sig[k][1] for k in range(a, b + 1)))

    first_delay = None
    if sustained:
        a = sustained[0][0]
        for k in range(a, len(obs)):
            if sig[k][1] and sig[k][0] is not None and sig[k][0] > thr:
                first_delay = k - a
                break

    return {
        "window_since": since_iso,
        "samples_total": total,
        "v2_reliable": reliable,
        "v2_reliable_pct": (round(100 * reliable / total, 1) if total else None),
        "v2_insufficient": insufficient,
        "v2_insufficient_pct": (round(100 * insufficient / total, 1) if total else None),
        "v2_degraded": degraded,
        "source_switches": switches,
        "skew_exceeded": skew_over,
        "cross_cycle": cross,
        "comparisons_total": len(cmps),
        "divergence_breakdown": diverge,
        "outcome_divergences": outcome_diff,
        "old_episode_sim": old_sim,
        "v2_episode_sim": new_sim,
        "blocked_false_opens": new_sim["blocked_opens"],
        "blocked_false_resolves": new_sim["blocked_resolves"],
        "sustained_breach_runs": len(sustained),
        "sustained_breach_missed": missed,
        "first_reliable_alert_delay_samples": first_delay,
        "calculation_version": dpo.CALC_VERSION,
        "threshold": str(thr),
    }
