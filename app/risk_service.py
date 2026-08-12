"""风险评估编排：组装上下文 → 前置校验 → 算指标 → 跑规则 → 评分 → 持久化。

**单一快照原则**：所有计算绑定同一个 snapshot_id。绝不把不同时刻的账户汇总与
持仓明细拼在一起算 —— 那会算出一个从未真实存在过的账户状态，
而它看起来和真的一模一样。
"""
from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from . import daily_pnl_observation as dpo
from . import daily_pnl_v2 as dpv2
from . import data_quality as dq
from . import instruments
from . import pnl as pnl_calc
from . import risk_engine as re_
from . import risk_metrics as rm
from .config import Config
from .db import Database, to_decimal
from .logging_setup import get_logger
from .sectors import load_sector_map

log = get_logger()


# ---------------- 风险上下文 ----------------

@dataclass
class RiskContext:
    snapshot_id: str
    account_id: str | None
    data_as_of: str
    net_liquidation: Decimal | None
    total_cash: Decimal | None
    available_funds: Decimal | None
    buying_power: Decimal | None
    maintenance_margin: Decimal | None
    initial_margin: Decimal | None
    positions_market_value: Decimal | None
    base_currency: str | None
    balances: dict = field(default_factory=dict)
    positions: list[dict] = field(default_factory=list)
    orders: list[dict] = field(default_factory=list)
    executions: list[dict] = field(default_factory=list)
    quality_status: str = dq.FRESH
    quality_issues: list[dict] = field(default_factory=list)
    ai_analysis_allowed: bool = True
    equity_series: list[dict] = field(default_factory=list)
    prev_positions: list[dict] = field(default_factory=list)
    recent_exec_symbols: set[str] = field(default_factory=set)
    pnl: dict = field(default_factory=dict)
    flows_present: bool = False
    #: 当日盈亏的原子观测（分子分母同周期闭合）。影子期只用于记录质量与比较；
    #: v2 打开后才接管 ACC_DAILY_LOSS_HIGH 与质量下传。
    daily_pnl_observation: Any = None


# 前置校验失败码
PRECHECK_NO_SNAPSHOT = "NO_SNAPSHOT_ID"
PRECHECK_QUALITY_BLOCKED = "QUALITY_BLOCKED"
PRECHECK_CORE_FIELDS_MISSING = "CORE_FIELDS_MISSING"
PRECHECK_SNAPSHOT_MISMATCH = "SNAPSHOT_MISMATCH"
PRECHECK_SEVERELY_STALE = "SEVERELY_STALE"


def precheck(ctx: RiskContext, now: datetime, cfg: Config) -> list[tuple[str, str]]:
    """五项前置校验。返回 [(code, 人话原因)]，空列表表示可以做可靠计算。

    注意：校验不通过**不等于不计算** —— 仍会算出能安全计算的原始指标，
    只是结果标记为 blocked/unreliable，且禁止输出确定性结论。
    """
    fails: list[tuple[str, str]] = []
    if not ctx.snapshot_id:
        fails.append((PRECHECK_NO_SNAPSHOT, "快照标识缺失，无法保证数据同源"))
    if ctx.quality_status == dq.ERROR or not ctx.ai_analysis_allowed:
        fails.append((PRECHECK_QUALITY_BLOCKED,
                      "数据质量层判定该快照存在严重问题，风险结论不可靠"))
    if ctx.net_liquidation is None or (ctx.total_cash is None and ctx.balances.get("cash") is None):
        fails.append((PRECHECK_CORE_FIELDS_MISSING, "净资产或现金缺失，核心指标无法计算"))
    # 持仓必须与账户汇总同属一个快照
    bad = [p.get("symbol") for p in ctx.positions
           if p.get("snapshot_id") and p.get("snapshot_id") != ctx.snapshot_id]
    if bad:
        fails.append((PRECHECK_SNAPSHOT_MISMATCH,
                      f"持仓与账户汇总不属于同一快照：{','.join(str(b) for b in bad[:3])}"))
    try:
        age = (now - datetime.fromisoformat(ctx.data_as_of)).total_seconds()
        if age >= cfg.dq_error_seconds:
            fails.append((PRECHECK_SEVERELY_STALE,
                          f"数据已过期 {age / 3600:.1f} 小时，超出可用于分析的范围"))
    except (TypeError, ValueError):
        fails.append((PRECHECK_SEVERELY_STALE, "数据时点非法，无法判断新鲜度"))
    return fails


def build_context(cfg: Config, db: Database, snapshot_id: str | None = None,
                  now: datetime | None = None, *,
                  daily_pnl_observation: Any = None,
                  calculation_mode: str | None = None) -> RiskContext | None:
    """从库里组装风险上下文。snapshot_id 为空则取最新一份。

    `daily_pnl_observation` 由调用方（sync 的 v2 顺序）**显式注入** ——
    那一刻分子分母同时在手，闭合是构造上原子的。注入了就直接用，
    这里绝不再去库里「猜最新一条」：猜出来的那一条正是事故成因。
    """
    now = now or datetime.now(timezone.utc)
    row = (db.snapshot_by_id(snapshot_id) if snapshot_id else db.latest_snapshot_row())
    if row is None:
        return None
    b = dict(row)
    sid = b["snapshot_id"]
    positions = db.positions_for_snapshot(sid)
    account = b.get("account")

    # 账户级 PnL。
    #
    # **注意这里有两条路，刻意保留**：
    #   · 旧路（latest_pnl）拿的是「最新一条」，可能来自上一个采集周期 ——
    #     取证已证实这会与本快照的 net_liquidation 拼出跨周期比值，是
    #     ACC_DAILY_LOSS_HIGH 24 小时开合 11 次的直接成因。
    #   · 新路（pnl_for_run）只认同一个 sync_run_id，拼不齐就不拼。
    #
    # 影子期仍由旧路驱动事件（对比报告出来前不动线上行为），
    # 但观测与质量按新路计算，两者的分歧逐条落进 daily_pnl_metric_comparisons。
    run_id = b.get("sync_run_id")
    v2 = (calculation_mode == dpv2.MODE_V2
          or (calculation_mode is None and dpv2.enabled(cfg)))

    same_run = db.pnl_for_run(account, run_id) if (account and run_id) else None

    if v2:
        # v2：分子只允许来自同周期。拼不齐就让指标缺失（-> insufficient_data），
        # **绝不退回 latest_pnl 凑数** —— 那正是 24 小时开合 11 次的成因。
        # `latest_pnl` 在这条路上一次都不会被调用，这是结构保证而非纪律。
        pnl = dict(same_run) if same_run else {}
    else:
        pnl_row = db.latest_pnl(account) if account else None
        pnl = dict(pnl_row) if pnl_row else {}

    # 观测：调用方注入优先。注入意味着「分子分母在同一刻同时到手」，
    # 比这里事后去库里配对更可信，也是 v2 顺序的全部意义。
    observation = daily_pnl_observation
    if observation is None:
        observation = _observe(cfg, db, account=account, balances=b,
                               same_run_pnl=same_run, run_id=run_id, now=now)

    # 净值序列：裁到交易基线之后，入金前的段不参与回撤计算
    series = [{"et_date": r["et_date"], "net_liquidation": to_decimal(r["net_liquidation"])}
              for r in db.daily_equity_series(account, None, None)] if account else []
    external_flows = db.list_cash_flows(account) if account else []
    baseline = pnl_calc.trading_baseline(
        series, weekday_of=lambda dstr: datetime.fromisoformat(dstr).weekday(),
        cash_flow_dates=[f["et_date"] for f in external_flows])
    flows_present = bool(external_flows)
    if baseline is not None:
        series = [x for x in series if x["et_date"] >= baseline["et_date"]]

    since = (now - timedelta(days=1)).isoformat()
    try:
        recent_syms = {r["symbol"] for r in db.executions_between(since, None) if r["symbol"]}
    except Exception:  # noqa: BLE001
        recent_syms = set()

    return RiskContext(
        snapshot_id=sid, account_id=account, data_as_of=b["ts"],
        net_liquidation=to_decimal(b.get("equity")),
        total_cash=to_decimal(b.get("total_cash")) or to_decimal(b.get("cash")),
        available_funds=to_decimal(b.get("available_funds")),
        buying_power=to_decimal(b.get("buying_power")),
        maintenance_margin=to_decimal(b.get("maint_margin")),
        initial_margin=to_decimal(b.get("init_margin")),
        positions_market_value=to_decimal(b.get("positions_value")),
        base_currency=b.get("currency"),
        balances=b, positions=positions,
        orders=db.open_orders(), executions=[],
        quality_status=b.get("quality_status") or dq.FRESH,
        quality_issues=db.quality_issues_for(sid),
        ai_analysis_allowed=bool(b.get("ai_analysis_allowed")),
        equity_series=series, recent_exec_symbols=recent_syms,
        pnl=pnl, flows_present=flows_present,
        daily_pnl_observation=observation,
    )


def _observe(cfg: Config, db: Database, *, account, balances: dict,
             same_run_pnl, run_id, now) -> Any:
    """按同周期规则闭合一次观测。拿不到同批次分子就是 insufficient —— 这是
    结论，不是失败：`dpo.build` 会把 CROSS_CYCLE/NUMERATOR_MISSING 记进原因。"""
    num = {}
    if same_run_pnl is not None:
        p = dict(same_run_pnl)
        num = {"daily_pnl": p.get("daily_pnl"), "source": p.get("source"),
               "basis": p.get("daily_basis"), "quality": p.get("quality"),
               "observed_at": p.get("as_of"), "correlation_id": p.get("sync_run_id"),
               "session_date": p.get("et_date"), "snapshot_id": None,
               "currency": balances.get("currency")}
    den = {"net_liquidation": balances.get("equity"),
           "observed_at": balances.get("ts"), "correlation_id": run_id,
           "snapshot_id": balances.get("snapshot_id"),
           "currency": balances.get("currency")}
    prev = None
    try:
        prev = db.latest_daily_pnl_observation(account) if account else None
    except Exception:  # noqa: BLE001
        prev = None
    # 「本轮分子还没写」与「本轮分子真的没有」要分开。
    # 现行管道里 sync 先跑风险评估、再写 PnL 快照，所以评估时刻同批次分子
    # 必然缺席 —— 那是时序，不是数据问题。记成 PIPELINE_ORDER_PENDING，
    # 影子报告才不会把它误读成缺数据。
    #
    # 判据：拿到了 run_id、但这个 run 还没有 PnL 行。切换 v2 时把 sync 的
    # 写入顺序调到评估之前，这个分支就不会再走到。
    pending = bool(run_id) and same_run_pnl is None
    return dpo.build(
        account_id=account, numerator=num or None, denominator=den,
        observed_at=balances.get("ts"), correlation_id=run_id,
        previous_source=(prev["current_source"] if prev else None),
        previous_basis=(prev["basis"] if prev else None),
        skew_tolerance_seconds=getattr(cfg, "daily_pnl_skew_tolerance_seconds", 90),
        max_age_seconds=getattr(cfg, "daily_pnl_max_age_seconds", 600),
        numerator_pending=pending, now=now)


def pnl_quality_propagation(ctx: RiskContext, *, enabled: bool) -> dict:
    """PnL 子系统质量向评估层的传导结果。

    **纯函数，好让「打开开关会发生什么」可以被测试而不必真的打开。**

    历史缺口：risk_assessment 对全部 721 个样本都记 fresh/reliable，包括那 51 个
    pnl quality 为 DEGRADED 的 —— 规则完全不知道自己在用降级数据。

    `enabled=False`（影子期）时只回报观测质量，不改 quality_status / reliable /
    ai_analysis_allowed。因为 reliable 一旦转 false，`register_triggers` 就不再判
    恢复，那会影响**所有**规则的事件行为，不只是当日亏损这一条。
    """
    obs = ctx.daily_pnl_observation
    out = {
        "pnl_quality_status": getattr(obs, "quality_status", None),
        "pnl_reliable": bool(getattr(obs, "reliable", False)),
        "pnl_quality_reasons": list(getattr(obs, "quality_reasons", ()) or ()),
        "pnl_event_transition_allowed": bool(
            getattr(obs, "event_transition_allowed", False)),
        "pnl_event_transition_block_reason": getattr(
            obs, "event_transition_block_reason", None),
        "quality_status": ctx.quality_status,
        "reliable_downgraded": False,
        "ai_analysis_allowed": ctx.ai_analysis_allowed,
        "applied": bool(enabled),
    }
    if not enabled or obs is None:
        return out
    if not obs.reliable:
        # **刻意不改 quality_status 的取值。** 它是三态词表（fresh/stale/error），
        # webstatus 的 rank 字典会对未知值 KeyError，展示层也有多处硬编码分支。
        # 为了「让 DEGRADED 不再被记成 fresh」而引入第四个取值，风险大于收益。
        # 真实质量改为落在 pnl_quality_status / pnl_quality_reasons 这些
        # 专用字段上 —— 可查询、可断言，且不牵动既有分支。
        #
        # 会变的只有两件事：AI 禁止基于不可靠的当日盈亏输出结论，
        # 以及当日亏损规则降为 insufficient_data（在 assess 里生效）。
        out["reliable_downgraded"] = True
        out["ai_analysis_allowed"] = False
    return out


def compute_metrics(cfg: Config, ctx: RiskContext, db_for_class=None
                    ) -> tuple[dict, list[rm.CalcIssue]]:
    """全部指标。任何一块算不出来只影响该块，不影响其余。"""
    issues: list[rm.CalcIssue] = []
    nl = ctx.net_liquidation

    acct, i1 = rm.account_structure(ctx.balances, ctx.positions)
    issues += i1
    # 分类：人工覆盖 > IBKR 元数据 > 遗留 sectors.json > Unknown（带原因）
    sector_map = load_sector_map(cfg.sector_map_path)
    syms = [p["symbol"] for p in ctx.positions if p.get("symbol")]
    classes = instruments.classify_positions(
        db_for_class, syms, legacy_sector_map=sector_map,
        conids={p["symbol"]: p.get("conid") for p in ctx.positions if p.get("symbol")})
    unknown = {s for s, c in classes.items() if not c.is_known}
    conc, i2 = rm.concentration(ctx.positions, nl, unknown)
    conc["classifications"] = {s: c.as_dict() for s, c in classes.items()}
    conc["unknown_symbols"] = sorted(unknown)
    issues += i2
    pnl_m, i3 = rm.pnl_ratios(ctx.balances, ctx.pnl, nl)
    issues += i3
    liq, i4 = rm.liquidity_and_stress(ctx.balances, ctx.positions, nl)
    issues += i4

    total_unreal = pnl_m.get("unrealized_pnl")
    if total_unreal is None:
        # 账户级未实现缺失时用持仓浮盈合计 —— 贡献度是持仓间的相对占比，
        # 用同源的持仓合计做分母比直接放弃计算更有用（口径在字段名里已写明）。
        sums = [rm.d(p.get("upl")) for p in ctx.positions]
        vals = [x for x in sums if x is not None]
        total_unreal = sum(vals, Decimal("0")) if vals else None
    pos_rows = rm.position_risk(
        ctx.positions, nl,
        max_position_weight=Decimal(str(getattr(cfg, "risk_single_weight_equity_max", "0.30"))),
        total_unrealized=total_unreal, daily_pnl_total=pnl_m.get("daily_pnl"))

    dd = rm.drawdown(ctx.equity_series)
    issues += dd.issues
    wr = rm.window_returns(ctx.equity_series, flows_present=ctx.flows_present)

    return {
        "account": acct,
        "concentration": {k: v for k, v in conc.items() if k != "positions"},
        "positions": pos_rows,
        "pnl": {**pnl_m, **wr},
        "drawdown": {
            "current_drawdown": dd.current_drawdown, "max_drawdown": dd.max_drawdown,
            "peak_value": dd.peak_value, "peak_date": dd.peak_date,
            "drawdown_days": dd.drawdown_days,
            "recent_high_drawdown": dd.recent_high_drawdown,
        },
        "liquidity": liq,
        "calc_issues": [i.as_dict() for i in issues],
    }, issues


def _unexplained_changes(ctx: RiskContext, db: Database) -> dict[str, str]:
    """相邻快照间的大额持仓变化且无成交记录。"""
    out: dict[str, str] = {}
    prev = db.previous_snapshot_positions(ctx.snapshot_id, ctx.account_id)
    if not prev:
        return out
    prev_map = {p["symbol"]: to_decimal(p["qty"]) for p in prev if p.get("symbol")}
    cur_map = {p["symbol"]: to_decimal(p["qty"]) for p in ctx.positions if p.get("symbol")}
    for sym, pq in prev_map.items():
        if sym in ctx.recent_exec_symbols or pq in (None, Decimal("0")):
            continue
        cq = cur_map.get(sym)
        if cq is None:
            out[sym] = f"持仓从 {pq} 股消失，但近 24 小时没有该标的的成交记录"
        elif abs(pq - cq) / abs(pq) > Decimal("0.5"):
            out[sym] = f"持仓从 {pq} 股变为 {cq} 股（变动 >50%），但没有对应成交记录"
    return out


def _set_version_for_mode(cfg: Config, rules, *,
                          calculation_mode: str | None = None) -> str:
    """本轮落库用的规则集版本。

    开关关闭时**逐字节等于** `re_.rule_set_version(rules)` —— 候选代码的存在
    不得让线上产生新版本的评估，也就不会影响幂等键与已有历史。

    v2 生效时只换算法标签，内容哈希（阈值与规则定义）保持不变；
    `risk_events.rule_semantics_changed` 因此不会把它当成规则变化去开新
    episode（见第四节：口径切换不得断开在途 episode）。
    """
    v2 = (calculation_mode == dpv2.MODE_V2
          or (calculation_mode is None and dpv2.enabled(cfg)))
    if not v2:
        return re_.rule_set_version(rules)
    return f"{dpv2.CALC_VERSION_V2}~{re_.rule_set_hash(rules)}"


def assess(cfg: Config, db: Database, *, snapshot_id: str | None = None,
           now: datetime | None = None, force: bool = False,
           daily_pnl_observation: Any = None,
           calculation_mode: str | None = None) -> dict | None:
    """对一份快照做完整风险评估并落库。返回评估结果 dict。

    幂等：同一 snapshot_id + 同一 calculation_version 只算一次；
    规则版本变化后（CALC_VERSION 改动）会重新计算，历史评估仍保留可对比。

    `daily_pnl_observation` / `calculation_mode` 供 v2 顺序显式传入本轮观测。
    不传时行为与以往完全一致（由开关决定走哪条路）。
    """
    now = now or datetime.now(timezone.utc)
    ctx = build_context(cfg, db, snapshot_id, now,
                        daily_pnl_observation=daily_pnl_observation,
                        calculation_mode=calculation_mode)
    if ctx is None:
        return None

    # 规则集版本：阈值一变就是新版本，历史评估据此追溯当时口径
    rules_for_set = re_.default_rules(cfg)
    set_hash = re_.rule_set_hash(rules_for_set)
    set_version = _set_version_for_mode(cfg, rules_for_set,
                                        calculation_mode=calculation_mode)

    if not force:
        # 幂等键必须与本轮真正落库的版本一致，否则 v2 首轮会命中旧版本的
        # 已有评估、直接返回，v2 反而一次都没跑
        existing = db.assessment_for(ctx.snapshot_id, set_version)
        if existing:
            return load_assessment(db, dict(existing))
    try:
        db.sync_risk_rules([r.as_dict() for r in rules_for_set],
                           set_version=set_version, set_hash=set_hash)
    except Exception:  # noqa: BLE001 — 审计落库失败不得阻断评估
        log.warning("规则版本落库失败（评估继续）", exc_info=True)

    fails = precheck(ctx, now, cfg)
    reliable = not fails
    blocked_reason = "；".join(m for _, m in fails) if fails else None

    v2_on = bool(getattr(cfg, "daily_pnl_v2_enabled", False))
    prop = pnl_quality_propagation(ctx, enabled=v2_on)

    metrics, _ = compute_metrics(cfg, ctx, db)
    if v2_on and not prop["pnl_event_transition_allowed"]:
        # 观测没闭合：让当日亏损率缺失，规则自然落到 insufficient_data。
        # **这既不打开事件也不关闭事件** —— 缺失不等于恢复。
        metrics.setdefault("pnl", {})["daily_pnl_ratio"] = None
    rules = rules_for_set
    events, outcomes = re_.evaluate_rules(
        metrics, rules, collect_outcomes=True, quality_status=ctx.quality_status,
        unexplained_changes=_unexplained_changes(ctx, db))
    # 数据质量类事件：由质量层结论驱动，不重复判定
    if ctx.quality_status == dq.ERROR:
        events.append(re_.RiskEvent(
            "DATA_QUALITY_INSUFFICIENT", re_.SEV_CRITICAL, "数据质量不足",
            "数据质量层判定该快照存在严重问题，本次风险结论不可靠",
            ctx.quality_status, "fresh", "status", "DATA_QUALITY_INSUFFICIENT",
            re_.CAT_DATA, blocks_ai=True,
            recommended_check="查看数据质量问题列表，确认同步与行情是否正常"))

    sc = re_.score(events, weights=_weights(cfg), quality_status=ctx.quality_status,
                   outcomes=outcomes)
    level = re_.risk_level(sc["total_risk_score"], events, reliable=reliable)
    drivers = re_.top_risk_drivers(events)

    assessment_id = uuid.uuid4().hex
    payload = {
        "assessment_id": assessment_id,
        "snapshot_id": ctx.snapshot_id,
        "account_id": ctx.account_id,
        "data_as_of": ctx.data_as_of,
        "risk_level": level,
        "risk_score": str(sc["total_risk_score"]),
        "reliable": reliable,
        "blocked_reason": blocked_reason,
        "component_scores": {k: str(v) for k, v in sc["component_scores"].items()},
        "calculated_metrics": metrics,
        "triggered_rules": sorted({e.rule_code for e in events}),
        "rule_outcomes": [o.as_dict() for o in outcomes],
        "concentration_applicability": re_.applicability_summary(
            outcomes, re_.CAT_CONCENTRATION),
        "score_normalization_method": sc.get("score_normalization_method"),
        "normalization_detail": sc.get("normalization_detail", {}),
        "top_risk_drivers": drivers,
        "data_quality_status": prop["quality_status"],
        "ai_analysis_allowed": (ctx.ai_analysis_allowed and reliable
                                and prop["ai_analysis_allowed"]),
        # PnL 子系统的真实质量。历史上这里对 DEGRADED 样本一律记 fresh/reliable，
        # 规则完全不知道自己在用降级数据 —— 现在它可查询、可断言。
        "pnl_quality_status": prop["pnl_quality_status"],
        "pnl_reliable": prop["pnl_reliable"],
        "data_quality_reasons": prop["pnl_quality_reasons"],
        # v2 生效时记 v2 的口径版本；关闭时保持影子期的观测版本不变
        "pnl_calculation_version": (dpv2.active_daily_pnl_version(cfg)
                                    or dpo.CALC_VERSION),
        "calculation_version": set_version,
        "rule_set_version": set_version,
        "rule_set_hash": set_hash,
        "events": [{**e.as_dict(ctx.snapshot_id, now.isoformat()),
                    "rule_version": set_version, "rule_set_hash": set_hash}
                   for e in events],
        "created_at": now.isoformat(),
    }
    db.save_risk_assessment(payload)
    log.info("风险评估 snapshot=%s level=%s score=%s reliable=%s rules=%d",
             ctx.snapshot_id[:8], level, sc["total_risk_score"], reliable, len(events))
    return payload


def _weights(cfg: Config) -> dict:
    out = {}
    for key in re_.DEFAULT_WEIGHTS:
        v = getattr(cfg, f"risk_weight_{key.replace('_score', '')}", None)
        if v is not None:
            out[key] = Decimal(str(v))
    return out


def load_assessment(db: Database, row: dict) -> dict:
    """把库里的评估行还原成 API 输出结构。"""
    def j(v, default):
        try:
            return json.loads(v) if v else default
        except (TypeError, ValueError):
            return default
    return {
        "assessment_id": row["assessment_id"],
        "snapshot_id": row["snapshot_id"],
        "account_id": row.get("account_id"),
        "data_as_of": row["data_as_of"],
        "risk_level": row["risk_level"],
        "risk_score": row.get("risk_score"),
        "reliable": bool(row.get("reliable")),
        "blocked_reason": row.get("blocked_reason"),
        "component_scores": j(row.get("component_scores"), {}),
        "calculated_metrics": j(row.get("calculated_metrics"), {}),
        "triggered_rules": j(row.get("triggered_rules"), []),
        "top_risk_drivers": j(row.get("top_risk_drivers"), []),
        "data_quality_status": row.get("data_quality_status"),
        "ai_analysis_allowed": bool(row.get("ai_analysis_allowed")),
        "calculation_version": row.get("calculation_version"),
        "rule_set_version": row.get("rule_set_version"),
        "rule_set_hash": row.get("rule_set_hash"),
        "created_at": row.get("created_at"),
        "events": db.risk_events_for(row["assessment_id"]),
        # 旧口径算出的历史评估没有这些列，读回来是空 —— 给空而不是伪造
        "rule_outcomes": j(row.get("rule_outcomes"), []),
        "concentration_applicability": j(row.get("concentration_applicability"), {}),
        "score_normalization_method": row.get("score_normalization_method"),
        "normalization_detail": j(row.get("normalization_detail"), {}),
    }
