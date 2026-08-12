"""阈值回测：历史重放 + 候选阈值 + walk-forward + 组合分析。

真实 episode 需要时间积累。在事件历史还很短的时候，唯一诚实的做法是**历史重放**：
回到每个历史交易日，只用当时已经存在的数据重新判断规则会不会触发，再看之后
发生了什么。这既能立刻产生样本，本身也是防未来数据泄漏的正确姿势。

**防未来数据泄漏**是本模块的第一约束，具体做法：
  1. 取数一律走 `bars_up_to(symbol, as_of)` —— 物理上拿不到 as_of 之后的数据
  2. 均线、分位数只用滚动历史窗口，绝不用全历史统计量
  3. 规则定义用当时有效的版本
  4. 不用「事件后来是否恢复」反推当时该不该触发
  5. 前瞻结果与触发判断分两次取数，中间不共享任何序列

**候选阈值有限枚举**：当前阈值 ± 有限档位，或配置明确指定。不做网格搜索 ——
在几十个样本上搜几百个阈值，找到的一定是噪音。
"""
from __future__ import annotations

import hashlib
import json
import time
import uuid
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Callable, Sequence

from . import market_metrics as mm
from . import outcome_labels as ol
from . import rule_performance as rp
from . import trading_calendar as tcal
from .logging_setup import get_logger

log = get_logger()

CALC_VERSION = "backtest-v1/2026-07"

SEG_FULL = "full"
SEG_TRAIN = "train"
SEG_VALIDATION = "validation"

#: 训练/验证切分比例。时间序列**绝不打乱** —— 打乱会让未来数据进入训练期。
TRAIN_RATIO = Decimal("0.7")

#: 验证期相对训练期的退化超过这个幅度即判 unstable。
UNSTABLE_PRECISION_DROP = Decimal("0.20")


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


# ---------------- 可回测的规则 ----------------

class BacktestableRule:
    """一条支持数值阈值回放的规则。

    metric_fn 只接受「截至该时点的数据」，它的签名保证了调用方无法传入未来数据。
    """

    def __init__(self, rule_code: str, source_type: str, metric_fn: Callable,
                 *, direction: str = "above", current: str,
                 candidates: Sequence[str], unit: str = "ratio",
                 label: str | None = None):
        self.rule_code = rule_code
        self.source_type = source_type
        self.metric_fn = metric_fn
        self.direction = direction        # above: 指标 > 阈值即触发
        self.current = Decimal(str(current))
        self.candidates = [Decimal(str(c)) for c in candidates]
        self.unit = unit
        self.label = label or ol.default_label_for(source_type)

    def fires(self, value: Decimal | None, threshold: Decimal) -> bool | None:
        if value is None:
            return None               # 指标算不出来 -> 不触发也不算未触发
        return value > threshold if self.direction == "above" else value < threshold


def _pct_candidates(current: str, steps=(-0.10, -0.05, 0, 0.05, 0.10)) -> list[str]:
    """当前阈值附近的有限档位。5 档，不是几百个小数点。"""
    c = Decimal(str(current))
    out = []
    for s in steps:
        v = c + Decimal(str(s))
        if v > 0:
            out.append(str(v.quantize(Decimal("0.01"))))
    return sorted(set(out), key=Decimal)


def _abs_candidates(current: str, steps) -> list[str]:
    c = Decimal(str(current))
    return sorted({str((c + Decimal(str(s))).quantize(Decimal("0.01")))
                   for s in steps if c + Decimal(str(s)) > 0}, key=Decimal)


def default_rules(cfg=None, db=None) -> list[BacktestableRule]:
    """第一版可回测规则集。全部走 point-in-time 取数。"""
    def g(name: str, fallback: str) -> str:
        v = getattr(cfg, name, None) if cfg is not None else None
        return str(v) if v not in (None, "") else fallback

    def qqq_vs_ma50(db_, as_of):
        bars = db_.bars_up_to("QQQ", as_of, limit=60)
        closes = [b["c"] for b in bars if b.get("c") is not None]
        ma = mm.sma(closes, 50)
        if ma is None or not closes:
            return None
        return -(closes[-1] - ma) / ma          # 正数 = 低于均线的幅度

    def sox_vs_qqq(db_, as_of):
        s = db_.bars_up_to("SOXX", as_of, limit=30)
        q = db_.bars_up_to("QQQ", as_of, limit=30)
        rs_ = mm.relative_strength(s, q, 20)
        return None if rs_ is None else -rs_    # 正数 = SOXX 弱于 QQQ 的幅度

    def vix_level(db_, as_of):
        bars = db_.bars_up_to("VIX", as_of, limit=5)
        return bars[-1]["c"] if bars else None

    def vxn_level(db_, as_of):
        bars = db_.bars_up_to("VXN", as_of, limit=5)
        return bars[-1]["c"] if bars else None

    def rates_5d(db_, as_of):
        bars = db_.bars_up_to("US10Y", as_of, limit=10)
        closes = [b["c"] for b in bars if b.get("c") is not None]
        return mm.window_return(closes, 5)

    def qqq_drawdown(db_, as_of):
        bars = db_.bars_up_to("QQQ", as_of, limit=60)
        closes = [b["c"] for b in bars if b.get("c") is not None]
        return mm.drawdown_from_high(closes, 60)

    return [
        BacktestableRule("TREND_QQQ_BELOW_MA50", "market", qqq_vs_ma50,
                         current="0", candidates=["-0.02", "0", "0.01", "0.02", "0.03"]),
        BacktestableRule("TREND_QQQ_DRAWDOWN", "market", qqq_drawdown,
                         current=g("market_qqq_drawdown_max", "0.10"),
                         candidates=_pct_candidates(g("market_qqq_drawdown_max", "0.10"),
                                                    (-0.05, -0.03, 0, 0.03, 0.05))),
        BacktestableRule("TREND_SOX_WEAKER", "market", sox_vs_qqq,
                         current=str(abs(Decimal(g("market_sox_underperform_max", "-0.05")))),
                         candidates=["0.02", "0.03", "0.05", "0.07", "0.10"]),
        BacktestableRule("VOL_VIX_ELEVATED", "market", vix_level,
                         current=g("market_vix_warn", "20"), unit="level",
                         candidates=_abs_candidates(g("market_vix_warn", "20"),
                                                    (-5, -2, 0, 2, 5))),
        BacktestableRule("VOL_VIX_HIGH", "market", vix_level,
                         current=g("market_vix_crit", "30"), unit="level",
                         candidates=_abs_candidates(g("market_vix_crit", "30"),
                                                    (-5, -2, 0, 2, 5))),
        BacktestableRule("VOL_VXN_ELEVATED", "market", vxn_level,
                         current=g("market_vxn_warn", "22"), unit="level",
                         candidates=_abs_candidates(g("market_vxn_warn", "22"),
                                                    (-4, -2, 0, 2, 4))),
        BacktestableRule("RATES_FAST_RISE_5D", "market", rates_5d,
                         current=g("market_rates_5d_pct", "0.08"),
                         candidates=["0.04", "0.06", "0.08", "0.10", "0.12"]),
    ]


# ---------------- 历史重放 ----------------

def replay_rule(db, rule: BacktestableRule, threshold: Decimal, *,
                calendar: tcal.TradingCalendar, days: list[str],
                window: int, label_code: str, cfg=None) -> dict:
    """在给定交易日序列上重放一条规则。

    每个 as_of 只用 `bars_up_to(as_of)`，前瞻结果另取一次数 ——
    两次取数不共享序列，杜绝「算指标时顺手看到了未来」。
    """
    from . import forward_outcomes as fo
    labels = ol.default_labels(cfg)
    label = ol.label_by_code(label_code, labels)
    if label is None:
        return {"error": f"未知标签 {label_code}"}

    tp = fp = 0
    outcome_days = 0            # 该窗口内确实发生风险结果的日子（recall 分母）
    triggered_days: list[str] = []
    leads: list[Decimal] = []
    consecutive_miss = max_consecutive_miss = 0

    for as_of in days:
        end_day = calendar.offset(as_of, window)
        if end_day is None:
            continue            # 前瞻窗口还没走完，这一天不进统计

        # ---- 1) 当时的指标（只用截至 as_of 的数据）----
        try:
            value = rule.metric_fn(db, as_of)
        except Exception:  # noqa: BLE001
            value = None
        fires = rule.fires(_d(value), threshold)

        # ---- 2) 之后发生了什么（独立取数）----
        outcome = _forward_for_backtest(db, as_of, end_day, rule.source_type)
        hit = ol.evaluate_label(label, outcome)
        if hit is None:
            continue            # 结果无法判定：既不算命中也不算误报

        if hit:
            outcome_days += 1
        if fires:
            triggered_days.append(as_of)
            if hit:
                tp += 1
                # 提前量：从触发日到窗口末日的交易日数（保守估计）
                leads.append(Decimal(calendar.count_between(as_of, end_day) - 1))
            else:
                fp += 1
        if hit and not fires:
            consecutive_miss += 1
            max_consecutive_miss = max(max_consecutive_miss, consecutive_miss)
        elif hit and fires:
            consecutive_miss = 0

    triggered = tp + fp
    fn = max(0, outcome_days - tp)
    tn = max(0, len(days) - triggered - fn)
    months = Decimal(len(days)) / Decimal("21") if days else None

    return {
        "threshold_value": str(threshold),
        "observation_window": window,
        "outcome_label": label_code,
        "trigger_count": triggered,
        "triggers_per_month": _s((Decimal(triggered) / months).quantize(Decimal("0.01"))
                                 if months and months > 0 else None),
        "true_positive": tp, "false_positive": fp,
        "false_negative": fn, "true_negative": tn,
        "precision_value": _s(rp._ratio(tp, triggered)
                              if triggered >= rp.MIN_SAMPLES_FOR_METRICS else None),
        "recall_value": _s(rp._ratio(tp, outcome_days)
                           if outcome_days >= rp.MIN_SAMPLES_FOR_METRICS else None),
        "false_positive_rate": _s(rp._ratio(fp, triggered)
                                  if triggered >= rp.MIN_SAMPLES_FOR_METRICS else None),
        "avg_lead_time_days": _s(rp._mean(leads)),
        "median_lead_time_days": _s(rp._median(leads)),
        "max_consecutive_misses": max_consecutive_miss,
        # 通知数估算：按现行策略，同一天的多次触发会被合并成一条
        "estimated_notifications": len(set(triggered_days)),
        "sample_size": triggered,
        "confidence": rp.confidence_for(triggered),
        "outcome_days": outcome_days,
        "days_evaluated": len(days),
    }


def _forward_for_backtest(db, as_of: str, end_day: str, source_type: str) -> dict:
    """回测用的前瞻结果。与触发判断分开取数。"""
    from . import forward_outcomes as fo
    out = fo.market_outcome(
        db.bars_in_range("QQQ", as_of, end_day),
        db.bars_in_range("SOXX", as_of, end_day),
        db.bars_in_range("VIX", as_of, end_day),
        db.bars_in_range("VXN", as_of, end_day),
        qqq_ma50_at_open=_ma50_pit(db, "QQQ", as_of),
        market_states=[r.get("market_state")
                       for r in db.market_score_series(as_of, end_day)] or None)
    if source_type in ("account", "combined", "position", "behavior"):
        rows = db.equity_series_between(as_of + "T00:00:00+00:00",
                                        end_day + "T23:59:59+00:00")
        out.update(fo.account_outcome(rows))
    return out


def _ma50_pit(db, symbol: str, as_of: str) -> Decimal | None:
    """point-in-time 50 日均线：**只用 as_of 及之前**的 50 根日线。"""
    bars = db.bars_up_to(symbol, as_of, limit=50)
    closes = [b["c"] for b in bars if b.get("c") is not None]
    if len(closes) < 50:
        return None
    return sum(closes, Decimal("0")) / Decimal(len(closes))


def rolling_percentile(db, symbol: str, as_of: str, *, window: int = 252
                       ) -> Decimal | None:
    """滚动分位数。**绝不用全历史** —— 用整段历史算「当时」的 VIX 分位，
    等于把未来的极值信息带进了过去的判断。"""
    bars = db.bars_up_to(symbol, as_of, limit=window)
    closes = [b["c"] for b in bars if b.get("c") is not None]
    if len(closes) < 30:
        return None
    return mm.percentile_rank(closes, closes[-1])


# ---------------- 训练 / 验证 ----------------

def split_days(days: list[str], ratio: Decimal = TRAIN_RATIO
               ) -> tuple[list[str], list[str]]:
    """按时间顺序切分。**不随机打乱** —— 打乱会把未来数据混进训练期。"""
    if len(days) < 10:
        return days, []
    cut = int(len(days) * float(ratio))
    return days[:cut], days[cut:]


def walk_forward(days: list[str], *, folds: int = 3) -> list[tuple[list, list]]:
    """向前滚动验证：用前一段评估，在后一段验证，逐步前移。"""
    if len(days) < folds * 10:
        return []
    out = []
    step = len(days) // (folds + 1)
    for i in range(1, folds + 1):
        train = days[:step * i]
        valid = days[step * i:step * (i + 1)]
        if train and valid:
            out.append((train, valid))
    return out


def stability_verdict(train: dict, valid: dict) -> tuple[str, str]:
    """训练期与验证期的差异。退化明显即 unstable。"""
    tp_, vp_ = _d(train.get("precision_value")), _d(valid.get("precision_value"))
    if tp_ is None or vp_ is None:
        return "insufficient_data", "训练期或验证期样本不足，无法评估稳定性"
    drop = tp_ - vp_
    if drop >= UNSTABLE_PRECISION_DROP:
        return "unstable", (f"训练期 precision {tp_:.0%} → 验证期 {vp_:.0%}，"
                            f"退化 {drop:.0%}，疑似过拟合该段历史")
    return "stable", f"训练期 {tp_:.0%} → 验证期 {vp_:.0%}，未见明显退化"


# ---------------- 规则组合 ----------------

#: 第一版只评估这五组高价值组合，不做全量组合爆炸。
DEFAULT_COMBOS = [
    ("CONC_WITH_SOX_WEAK", ["CONC_SINGLE_INVESTED_HIGH", "TREND_SOX_WEAKER"]),
    ("LEVERAGE_WITH_VOL", ["ACC_GROSS_LEVERAGE_HIGH", "VOL_VIX_ELEVATED"]),
    ("DRAWDOWN_WITH_STRESSED", ["ACC_DRAWDOWN_HIGH", "TREND_QQQ_DRAWDOWN"]),
    ("QQQ_MA50_WITH_VXN", ["TREND_QQQ_BELOW_MA50", "VOL_VXN_ELEVATED"]),
    ("POS_LOSS_WITH_POOL", ["POS_UNREALIZED_LOSS_HIGH", "POS_LOSS_POOL_SHARE_HIGH"]),
]


def evaluate_combo(db, rules: list[BacktestableRule], combo_code: str,
                   members: list[str], *, calendar, days: list[str],
                   window: int, label_code: str, cfg=None) -> dict | None:
    """组合规则：两条同时成立才算触发。

    **组合 precision 通常更高，recall 通常更低** —— 这个代价必须同时展示。
    只报 precision 提升会让人误以为组合无脑更好。
    """
    by_code = {r.rule_code: r for r in rules}
    available = [by_code[m] for m in members if m in by_code]
    if len(available) < 2:
        return None

    labels = ol.default_labels(cfg)
    label = ol.label_by_code(label_code, labels)
    if label is None:
        return None

    tp = fp = outcome_days = 0
    leads: list[Decimal] = []
    for as_of in days:
        end_day = calendar.offset(as_of, window)
        if end_day is None:
            continue
        fires_all = True
        for r in available:
            try:
                v = r.metric_fn(db, as_of)
            except Exception:  # noqa: BLE001
                v = None
            if not r.fires(_d(v), r.current):
                fires_all = False
                break
        outcome = _forward_for_backtest(db, as_of, end_day, available[0].source_type)
        hit = ol.evaluate_label(label, outcome)
        if hit is None:
            continue
        if hit:
            outcome_days += 1
        if fires_all:
            if hit:
                tp += 1
                leads.append(Decimal(calendar.count_between(as_of, end_day) - 1))
            else:
                fp += 1

    triggered = tp + fp
    combo_p = (rp._ratio(tp, triggered)
               if triggered >= rp.MIN_SAMPLES_FOR_METRICS else None)
    combo_r = (rp._ratio(tp, outcome_days)
               if outcome_days >= rp.MIN_SAMPLES_FOR_METRICS else None)

    # 各单规则对照
    member_stats = {}
    best_recall = None
    best_precision = None
    for r in available:
        st = replay_rule(db, r, r.current, calendar=calendar, days=days,
                         window=window, label_code=label_code, cfg=cfg)
        member_stats[r.rule_code] = {
            "trigger_count": st["trigger_count"],
            "precision": st["precision_value"], "recall": st["recall_value"],
            "avg_lead_time_days": st["avg_lead_time_days"],
        }
        rr, pp = _d(st["recall_value"]), _d(st["precision_value"])
        if rr is not None and (best_recall is None or rr > best_recall):
            best_recall = rr
        if pp is not None and (best_precision is None or pp > best_precision):
            best_precision = pp

    return {
        "combo_code": combo_code,
        "member_rules": json.dumps(members, ensure_ascii=False),
        "observation_window": window, "outcome_label": label_code,
        "combo_triggers": triggered,
        "combo_precision": _s(combo_p), "combo_recall": _s(combo_r),
        "combo_lead_time_days": _s(rp._mean(leads)),
        "member_stats": json.dumps(member_stats, ensure_ascii=False),
        # 负值 = 组合的召回不如最好的单规则，这是组合的代价
        "recall_delta": _s(combo_r - best_recall
                           if (combo_r is not None and best_recall is not None) else None),
        "precision_delta": _s(combo_p - best_precision
                              if (combo_p is not None and best_precision is not None)
                              else None),
        "sample_size": triggered,
        "confidence": rp.confidence_for(triggered),
        "created_at": datetime.now(timezone.utc).isoformat(),
    }


# ---------------- 任务编排 ----------------

def param_hash(params: dict) -> str:
    """相同参数的回测可识别与复用。"""
    raw = json.dumps(params, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode()).hexdigest()[:20]


def run_backtest(cfg, db, *, rules: list[str] | None = None,
                 date_start: str | None = None, date_end: str | None = None,
                 windows: Sequence[int] = tcal.FORWARD_WINDOWS,
                 labels: Sequence[str] | None = None,
                 candidate_thresholds: dict | None = None,
                 reuse: bool = True, now: datetime | None = None) -> dict:
    """执行一次回测。**任何异常都不外抛** —— 回测是离线分析，
    它失败绝不能影响账户同步、市场同步或风险告警。"""
    now = now or datetime.now(timezone.utc)
    t0 = time.monotonic()
    all_rules = default_rules(cfg, db)
    if rules:
        all_rules = [r for r in all_rules if r.rule_code in set(rules)]

    try:
        calendar = tcal.from_db(db)
        days_all = calendar.days
    except Exception as exc:  # noqa: BLE001 — 连日历都取不到就别开任务了
        log.warning("回测无法读取交易日历（线上评估不受影响）", exc_info=True)
        return {"backtest_run_id": None, "status": "failed",
                "error": f"{type(exc).__name__}: {str(exc)[:200]}"}
    if date_start:
        days_all = [d for d in days_all if d >= str(date_start)[:10]]
    if date_end:
        days_all = [d for d in days_all if d <= str(date_end)[:10]]

    params = {
        "rules": sorted(r.rule_code for r in all_rules),
        "date_start": date_start, "date_end": date_end,
        "windows": list(windows), "labels": list(labels or []),
        "candidates": candidate_thresholds or {},
        "calc_version": CALC_VERSION,
    }
    phash = param_hash(params)

    if reuse:
        try:
            prev = db.find_backtest_by_hash(phash)
            if prev:
                log.info("回测参数命中已有结果，复用 %s", prev["backtest_run_id"])
                return {"backtest_run_id": prev["backtest_run_id"], "reused": True,
                        "status": prev["status"]}
        except Exception:  # noqa: BLE001
            pass

    run_id = uuid.uuid4().hex
    train_days, valid_days = split_days(days_all)
    try:
        db.create_backtest_run({
            "backtest_run_id": run_id, "param_hash": phash,
            "requested_rules": json.dumps(params["rules"], ensure_ascii=False),
            "date_start": days_all[0] if days_all else None,
            "date_end": days_all[-1] if days_all else None,
            "observation_windows": json.dumps(list(windows)),
            "outcome_labels": json.dumps(list(labels or []), ensure_ascii=False),
            "candidate_thresholds": json.dumps(candidate_thresholds or {},
                                               ensure_ascii=False),
            "train_end": train_days[-1] if train_days else None,
            "status": "running", "started_at": now.isoformat(),
            "data_version": f"days={len(days_all)}",
            "calculation_version": CALC_VERSION, "created_at": now.isoformat()})
    except Exception as exc:  # noqa: BLE001 — 建任务就失败，不能把异常抛给调用方
        log.warning("回测任务创建失败（线上评估不受影响）", exc_info=True)
        return {"backtest_run_id": None, "status": "failed",
                "error": f"{type(exc).__name__}: {str(exc)[:200]}"}

    evaluated = failed = 0
    rows: list[dict] = []
    combos: list[dict] = []
    try:
        for rule in all_rules:
            label_code = rule.label
            if not label_code:
                continue
            cands = rule.candidates
            if candidate_thresholds and rule.rule_code in candidate_thresholds:
                cands = [Decimal(str(x))
                         for x in candidate_thresholds[rule.rule_code]]
            try:
                for window in windows:
                    for thr in cands:
                        for seg, seg_days in (("full", days_all),
                                              ("train", train_days),
                                              ("validation", valid_days)):
                            if not seg_days:
                                continue
                            res = replay_rule(
                                db, rule, thr, calendar=calendar, days=seg_days,
                                window=window, label_code=label_code, cfg=cfg)
                            rows.append({
                                **res, "backtest_run_id": run_id,
                                "rule_code": rule.rule_code,
                                "is_current_threshold": 1 if thr == rule.current else 0,
                                "segment": seg,
                                "segment_start": seg_days[0],
                                "segment_end": seg_days[-1],
                                "delta_vs_current": None,
                                "created_at": now.isoformat()})
                evaluated += 1
            except Exception:  # noqa: BLE001
                failed += 1
                log.warning("规则回测失败 %s", rule.rule_code, exc_info=True)

        _attach_deltas(rows)
        db.save_backtest_results(rows)

        # 组合分析（只用 full 段与主窗口）
        main_window = 5 if 5 in windows else list(windows)[0]
        for combo_code, members in DEFAULT_COMBOS:
            try:
                label_code = ol.default_label_for("market")
                c = evaluate_combo(db, all_rules, combo_code, members,
                                   calendar=calendar, days=days_all,
                                   window=main_window, label_code=label_code,
                                   cfg=cfg)
                if c:
                    combos.append({**c, "backtest_run_id": run_id})
            except Exception:  # noqa: BLE001
                log.warning("组合分析失败 %s", combo_code, exc_info=True)
        db.save_combo_results(combos)

        status = "success" if failed == 0 else "partial"
        db.finish_backtest_run(run_id, status=status,
                               duration_ms=int((time.monotonic() - t0) * 1000),
                               rules_evaluated=evaluated, rules_failed=failed)
        return {"backtest_run_id": run_id, "status": status, "reused": False,
                "rules_evaluated": evaluated, "rules_failed": failed,
                "result_rows": len(rows), "combos": len(combos),
                "days": len(days_all), "train_days": len(train_days),
                "validation_days": len(valid_days)}
    except Exception as exc:  # noqa: BLE001
        db.finish_backtest_run(run_id, status="failed",
                               duration_ms=int((time.monotonic() - t0) * 1000),
                               rules_evaluated=evaluated, rules_failed=failed,
                               error_message=f"{type(exc).__name__}: {str(exc)[:200]}")
        log.warning("回测失败 run=%s（线上评估不受影响）", run_id, exc_info=True)
        return {"backtest_run_id": run_id, "status": "failed",
                "error": str(exc)[:200]}


def _attach_deltas(rows: list[dict]) -> None:
    """给每个候选阈值补上「与当前阈值相比变化了什么」。

    候选阈值的结果必须**同时展示收益与代价** —— 只说 recall 涨了 12%，
    不说通知量涨了 68%，是在诱导用户做出他不会同意的改动。
    """
    current = {}
    for r in rows:
        if r.get("is_current_threshold"):
            key = (r["rule_code"], r["observation_window"], r["outcome_label"],
                   r["segment"])
            current[key] = r
    for r in rows:
        key = (r["rule_code"], r["observation_window"], r["outcome_label"],
               r["segment"])
        base = current.get(key)
        if not base or base is r:
            continue
        def delta(field):
            a, b = _d(r.get(field)), _d(base.get(field))
            return _s(a - b) if (a is not None and b is not None) else None
        r["delta_vs_current"] = json.dumps({
            "threshold": _s(_d(r["threshold_value"]) - _d(base["threshold_value"])),
            "trigger_count": r["trigger_count"] - base["trigger_count"],
            "notifications": (r["estimated_notifications"]
                              - base["estimated_notifications"]),
            "notifications_pct": _s(
                ((Decimal(r["estimated_notifications"])
                  - Decimal(base["estimated_notifications"]))
                 / Decimal(base["estimated_notifications"])).quantize(Decimal("0.01"))
                if base["estimated_notifications"] else None),
            "precision": delta("precision_value"),
            "recall": delta("recall_value"),
            "lead_time_days": delta("avg_lead_time_days"),
        }, ensure_ascii=False)
