"""风控规则引擎 R1–R7（纯逻辑）。

两个入口，均为纯函数（不做 IO / 时间 / DB / 通知）：
  - evaluate_ruleset(ctx, thresholds) -> RuleRunResult
        逐条规则返回状态（TRIGGERED / OK / INSUFFICIENT_DATA / STALE_DATA / ERROR / SKIPPED）
        + 命中列表 + evidence，供审计与前端可解释性。
  - evaluate_rules(ctx, thresholds) -> list[RuleHit]
        向后兼容薄封装 = ruleset 的全部 hits。

时间/计数/已实现盈亏/行情质量等派生量由上层（engine）预先算好放进 ctx。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import Enum

# ---- 严重度 ----
CRIT = "CRIT"
WARN = "WARN"
INFO = "INFO"


class RuleStatus(str, Enum):
    TRIGGERED = "TRIGGERED"                # 命中并产生告警
    OK = "OK"                             # 正常评估，未触发
    INSUFFICIENT_DATA = "INSUFFICIENT_DATA"  # 数据不足，无法判定
    STALE_DATA = "STALE_DATA"             # 数据过期，不判定
    NOT_EVALUATED = "NOT_EVALUATED"       # 行情质量不足以执行该判断（如仅有收盘冻结价）
    PARTIALLY_EVALUATED = "PARTIALLY_EVALUATED"  # 部分标的已判定，部分因质量不足未判定
    ERROR = "ERROR"                       # 规则内部异常
    SKIPPED = "SKIPPED"                   # 规则不适用（如无板块表）


class RunStatus(str, Enum):
    OK = "OK"
    TRIGGERED = "TRIGGERED"
    INSUFFICIENT_DATA = "INSUFFICIENT_DATA"
    STALE_DATA = "STALE_DATA"
    NOT_EVALUATED = "NOT_EVALUATED"
    PARTIALLY_EVALUATED = "PARTIALLY_EVALUATED"
    ERROR = "ERROR"


class PriceQuality(str, Enum):
    REALTIME = "REALTIME"                          # 综合实时（付费订阅）
    REALTIME_NONCONSOLIDATED = "REALTIME_NONCONSOLIDATED"  # 免费非综合实时（marketDataType=1）
    DELAYED = "DELAYED"                            # 约 15 分钟延迟（marketDataType=3）
    DELAYED_FROZEN = "DELAYED_FROZEN"              # 收盘冻结价（盘后 close 回退，仅供展示）
    FROZEN = "FROZEN"                              # 冻结（marketDataType=2 / 停牌）
    STALE = "STALE"                               # 时间戳过期
    MISSING = "MISSING"                           # 无行情
    INVALID = "INVALID"                           # 价格 <= 0 / NaN


# 允许触发 R7 CRIT 的实时质量（含免费非综合）
_R7_REALTIME = {PriceQuality.REALTIME, PriceQuality.REALTIME_NONCONSOLIDATED}

# 冻结价：不是当下的市场价。既不能据此报「触及」，也不能据此报「未触及」——
# 拿昨天的收盘价宣布今天没跌破止损，是另一种谎报。故一律 NOT_EVALUATED。
_R7_FROZEN = {PriceQuality.FROZEN, PriceQuality.DELAYED_FROZEN}


@dataclass(frozen=True)
class RuleThresholds:
    single_concentration: Decimal = Decimal("0.30")   # R1
    sector_concentration: Decimal = Decimal("0.40")   # R2
    realized_loss_pct: Decimal = Decimal("0.02")      # R6
    intraday_symbol_fills: int = 2                    # R4
    monthly_fills: int = 10                           # R5
    cooldown_hours: int = 48                          # R6
    #: R7 滞回带：已触发后，价格需回到止损线之外 hysteresis_atr × ATR 才算解除。
    #: 价格贴着止损线震荡时，无滞回会导致「触发→解除→再触发」反复改判
    #: （线上实测 DRAM 一支 10 条推送）。这不是新阈值，是同一阈值的去抖。
    r7_hysteresis_atr: Decimal = Decimal("0.3")
    version: str = "v1"                               # 阈值版本（审计用）


DEFAULT_THRESHOLDS = RuleThresholds()
RULES_VERSION = "R1-R7/2026-07"


@dataclass
class RuleContext:
    now: datetime
    equity: Decimal
    margin_balance: Decimal
    positions: list[dict]                    # {symbol, qty, avg_cost, market_value, upl}
    sector_map: dict[str, str] = field(default_factory=dict)      # symbol -> sector
    filled_today: dict[str, int] = field(default_factory=dict)    # symbol -> 当日成交笔数（旧路径，保留兼容）
    filled_month_total: int = 0                                   # 旧路径：成交笔数
    # ---- decision 口径（IBKR 路径）。order/execution/decision 三者不可混用 ----
    # R4：当日各标的「风险增加」决策次数（OPEN/ADD/REVERSAL），非成交笔数
    risk_increasing_today: dict[str, int] | None = None
    # R5：本月主动决策总数（OPEN/ADD/REDUCE/CLOSE/REVERSAL），非成交笔数
    decisions_month_total: int | None = None
    # 决策类型是否全部分类成功；False -> R4 只能 INSUFFICIENT_DATA
    decision_classification_complete: bool = True
    decision_kind_summary: dict[str, int] = field(default_factory=dict)
    realized_events: list[dict] = field(default_factory=list)     # {symbol, realized, ref_id, ts}
    stop_levels: dict[str, dict] = field(default_factory=dict)    # symbol -> {side, stop_price}
    current_prices: dict[str, Decimal] = field(default_factory=dict)  # symbol -> 现价
    # 行情质量：symbol -> PriceQuality。缺省视为 REALTIME（向后兼容旧测试）。
    price_quality: dict[str, str] = field(default_factory=dict)
    #: 当前处于 R7 触发态的标的（来自 alert_state）。滞回判定需要它 ——
    #: 纯函数不查 DB，由 engine 组装。
    r7_active: set = field(default_factory=set)
    cooldown_active: bool = False
    cooldown_ends_at: datetime | None = None
    new_fills_in_cooldown: list[dict] = field(default_factory=list)   # {symbol, ref_id, ts}
    account_ready: bool = True                # 账户/持仓数据是否就绪


@dataclass
class RuleHit:
    rule_id: str
    dedup_key: str
    title: str
    message: str
    severity: str = WARN
    symbol: str | None = None
    triggers_cooldown_hours: int | None = None
    #: 当前实际值（越大越糟的口径：集中度、风险占比、亏损幅度取绝对值）。
    #: 告警生命周期用它判断「实际值是否显著恶化」，None 则只按严重度与首触发判定。
    value: Decimal | None = None


@dataclass
class RuleEvaluation:
    rule_id: str
    status: RuleStatus
    hits: list[RuleHit] = field(default_factory=list)
    detail: str = ""            # evidence：实际值/阈值/原因（精简）
    #: 逐标的明细（目前 R7 用）：[{symbol, status, reason}]
    per_symbol: list[dict] = field(default_factory=list)
    #: 计数摘要（目前 R7 用）：{evaluated, not_evaluated, triggered}
    counts: dict = field(default_factory=dict)


@dataclass
class RuleRunResult:
    evaluations: list[RuleEvaluation]
    rules_version: str = RULES_VERSION

    @property
    def hits(self) -> list[RuleHit]:
        return [h for e in self.evaluations for h in e.hits]

    @property
    def run_status(self) -> RunStatus:
        st = {e.status for e in self.evaluations}
        if RuleStatus.ERROR in st:
            return RunStatus.ERROR
        if RuleStatus.TRIGGERED in st:
            return RunStatus.TRIGGERED
        if RuleStatus.STALE_DATA in st:
            return RunStatus.STALE_DATA
        if RuleStatus.INSUFFICIENT_DATA in st:
            return RunStatus.INSUFFICIENT_DATA
        if RuleStatus.NOT_EVALUATED in st:
            return RunStatus.NOT_EVALUATED
        if RuleStatus.PARTIALLY_EVALUATED in st:
            return RunStatus.PARTIALLY_EVALUATED
        return RunStatus.OK

    def by_rule(self) -> dict[str, RuleEvaluation]:
        return {e.rule_id: e for e in self.evaluations}

    @property
    def summary(self) -> dict:
        """规则摘要：触发 / 正常 / 未判定 / 异常 各多少条。

        「未判定」把 NOT_EVALUATED / PARTIALLY_EVALUATED / STALE_DATA /
        INSUFFICIENT_DATA 归在一起 —— 对用户而言它们是同一件事：这条规则这次没给出结论。
        """
        out = {"triggered": 0, "ok": 0, "not_evaluated": 0, "error": 0, "skipped": 0}
        for e in self.evaluations:
            if e.status is RuleStatus.TRIGGERED:
                out["triggered"] += 1
            elif e.status is RuleStatus.OK:
                out["ok"] += 1
            elif e.status is RuleStatus.ERROR:
                out["error"] += 1
            elif e.status is RuleStatus.SKIPPED:
                out["skipped"] += 1
            else:
                out["not_evaluated"] += 1
        return out


def _pct(v: Decimal) -> str:
    return f"{v * 100:.1f}%"


def _abs_mv(p: dict) -> Decimal:
    return abs(Decimal(str(p.get("market_value", 0))))


def _quality(ctx: RuleContext, symbol: str) -> PriceQuality:
    q = ctx.price_quality.get(symbol)
    if q is None:
        return PriceQuality.REALTIME  # 未提供质量信息时按实时（兼容）
    return PriceQuality(q) if not isinstance(q, PriceQuality) else q


# ----------------------- 单条规则（返回 RuleEvaluation） -----------------------

def _eval_r1(ctx, th) -> RuleEvaluation:
    if not (ctx.equity and ctx.equity > 0):
        return RuleEvaluation("R1", RuleStatus.INSUFFICIENT_DATA, detail="净资产缺失或非正")
    limit = th.single_concentration
    hits, max_ratio = [], Decimal("0")
    for p in ctx.positions:
        mv = _abs_mv(p)
        ratio = mv / ctx.equity
        max_ratio = max(max_ratio, ratio)
        if ratio > limit:
            sym = p["symbol"]
            hits.append(RuleHit(
                "R1", f"R1:{sym}", f"⚠️ 单一标的集中度超 {_pct(limit)}",
                f"{sym} 市值占净资产 {_pct(ratio)}（{mv:,.2f} / {ctx.equity:,.2f}）",
                severity=WARN, symbol=sym, value=ratio,
            ))
    detail = f"最大集中度 {_pct(max_ratio)} / 阈值 {_pct(limit)}"
    return RuleEvaluation("R1", RuleStatus.TRIGGERED if hits else RuleStatus.OK, hits, detail)


def _eval_r2(ctx, th) -> RuleEvaluation:
    if not ctx.sector_map:
        return RuleEvaluation("R2", RuleStatus.SKIPPED, detail="未配置板块映射表")
    if not (ctx.equity and ctx.equity > 0):
        return RuleEvaluation("R2", RuleStatus.INSUFFICIENT_DATA, detail="净资产缺失或非正")
    limit = th.sector_concentration
    by_sector: dict[str, list[dict]] = {}
    for p in ctx.positions:
        sector = ctx.sector_map.get(p["symbol"])
        if sector:
            by_sector.setdefault(sector, []).append(p)
    hits, max_ratio = [], Decimal("0")
    for sector, plist in by_sector.items():
        total = sum((_abs_mv(p) for p in plist), Decimal("0"))
        ratio = total / ctx.equity
        max_ratio = max(max_ratio, ratio)
        if ratio > limit:
            names = "、".join(sorted(p["symbol"] for p in plist))
            hits.append(RuleHit(
                "R2", f"R2:{sector}", f"⚠️ 板块集中度超 {_pct(limit)}：{sector}",
                f"{sector}（{names}）合计占净资产 {_pct(ratio)}（{total:,.2f} / {ctx.equity:,.2f}）",
                severity=WARN, value=ratio,
            ))
    detail = f"最大板块集中度 {_pct(max_ratio)} / 阈值 {_pct(limit)}"
    return RuleEvaluation("R2", RuleStatus.TRIGGERED if hits else RuleStatus.OK, hits, detail)


def _eval_r3(ctx, th) -> RuleEvaluation:
    if ctx.margin_balance is None:
        return RuleEvaluation("R3", RuleStatus.INSUFFICIENT_DATA, detail="margin 数据缺失")
    mb = Decimal(str(ctx.margin_balance))
    if mb < 0:
        hit = RuleHit("R3", "R3", "🔴 已动用融资（margin 余额为负）",
                      f"当前 margin 余额 {mb:,.2f}，处于融资状态，注意杠杆风险。", severity=CRIT)
        return RuleEvaluation("R3", RuleStatus.TRIGGERED, [hit], f"margin={mb:,.2f}")
    return RuleEvaluation("R3", RuleStatus.OK, detail=f"margin={mb:,.2f}")


def _eval_r4(ctx, th) -> RuleEvaluation:
    """当日同一标的的**风险增加决策**次数（OPEN/ADD/REVERSAL）。

    口径要点：
      - 数 decision 不数 execution：同一决策的多笔部分成交只算一次；
      - 只数风险增加：REDUCE/CLOSE/止损退出/强平/公司行动都不算 —— 减仓不是过度交易；
      - 决策类型无法还原（缺成交前持仓）时返回 INSUFFICIENT_DATA，
        绝不把所有 permId 一股脑计入。
    """
    if ctx.risk_increasing_today is None:
        # 旧路径：仍按成交笔数，语义在 detail 中写明
        counts = ctx.filled_today
        mx = max(counts.values(), default=0)
        hits = [
            RuleHit("R4", f"R4:{sym}", "⚠️ 盘中过度交易预警",
                    f"{sym} 当日已成交 {cnt} 笔（阈值 {th.intraday_symbol_fills}）。",
                    severity=WARN, symbol=sym)
            for sym, cnt in counts.items() if cnt >= th.intraday_symbol_fills
        ]
        detail = f"当日单标的最多 {mx} 笔成交 / 阈值 {th.intraday_symbol_fills}（成交笔数口径）"
        return RuleEvaluation("R4", RuleStatus.TRIGGERED if hits else RuleStatus.OK, hits, detail)

    if not ctx.decision_classification_complete:
        return RuleEvaluation(
            "R4", RuleStatus.INSUFFICIENT_DATA,
            detail="存在无法还原成交前持仓的决策，无法判定风险增加次数",
        )
    counts = ctx.risk_increasing_today
    hits = [
        RuleHit("R4", f"R4:{sym}", "⚠️ 盘中过度交易预警",
                f"{sym} 当日已有 {cnt} 次风险增加决策（开仓/加仓，阈值 "
                f"{th.intraday_symbol_fills}）。减仓与平仓不计入。",
                severity=WARN, symbol=sym)
        for sym, cnt in counts.items() if cnt >= th.intraday_symbol_fills
    ]
    mx = max(counts.values(), default=0)
    detail = (f"当日单标的最多 {mx} 次风险增加决策 / 阈值 {th.intraday_symbol_fills}"
              f"（decision 口径，含 OPEN/ADD/REVERSAL）")
    return RuleEvaluation("R4", RuleStatus.TRIGGERED if hits else RuleStatus.OK, hits, detail)


def _eval_r5(ctx, th) -> RuleEvaluation:
    """本月**主动交易决策**总数（OPEN/ADD/REDUCE/CLOSE/REVERSAL）。

    与 R4 不同：R5 数全部主动决策，减仓平仓同样算 —— 它衡量的是交易频次。
    未分类的决策仍计入总数（不影响频次），但会在 detail 中注明分类不完整。
    """
    if ctx.decisions_month_total is None:
        detail = f"本月 {ctx.filled_month_total} 笔成交 / 阈值 {th.monthly_fills}（成交笔数口径）"
        if ctx.filled_month_total > th.monthly_fills:
            hit = RuleHit("R5", f"R5:{ctx.now:%Y-%m}", "⚠️ 本月交易频次偏高",
                          f"本月累计成交 {ctx.filled_month_total} 笔（阈值 {th.monthly_fills}）。",
                          severity=WARN)
            return RuleEvaluation("R5", RuleStatus.TRIGGERED, [hit], detail)
        return RuleEvaluation("R5", RuleStatus.OK, detail=detail)

    total = ctx.decisions_month_total
    kinds = ctx.decision_kind_summary or {}
    kind_txt = "、".join(f"{k}×{v}" for k, v in sorted(kinds.items())) or "无"
    detail = f"本月 {total} 次主动交易决策 / 阈值 {th.monthly_fills}（decision 口径：{kind_txt}）"
    if not ctx.decision_classification_complete:
        detail += "；含未分类决策，分类不完整"
    if total > th.monthly_fills:
        hit = RuleHit("R5", f"R5:{ctx.now:%Y-%m}", "⚠️ 本月交易频次偏高",
                      f"本月累计 {total} 次主动交易决策（阈值 {th.monthly_fills}）。"
                      f"口径为主动决策数，非成交笔数。", severity=WARN)
        return RuleEvaluation("R5", RuleStatus.TRIGGERED, [hit], detail)
    return RuleEvaluation("R5", RuleStatus.OK, detail=detail)


def _eval_r6(ctx, th) -> RuleEvaluation:
    hits = []
    if ctx.equity and ctx.equity > 0:
        loss_limit = th.realized_loss_pct
        for ev in ctx.realized_events:
            realized = Decimal(str(ev["realized"]))
            if realized < 0 and (-realized / ctx.equity) > loss_limit:
                sym, ref = ev.get("symbol", "?"), ev.get("ref_id", "")
                hits.append(RuleHit(
                    "R6", f"R6:{ref}", f"🔴 大额认亏，进入 {th.cooldown_hours}h 冷静期",
                    f"{sym} 单笔已实现亏损 {realized:,.2f}，占净资产 {_pct(-realized / ctx.equity)}。"
                    f"接下来 {th.cooldown_hours} 小时为冷静期，请谨慎交易。",
                    severity=CRIT, symbol=sym, triggers_cooldown_hours=th.cooldown_hours,
                ))
    elif ctx.realized_events:
        return RuleEvaluation("R6", RuleStatus.INSUFFICIENT_DATA, detail="有成交但净资产缺失")

    # 冷静期内的新成交追加提醒
    if ctx.cooldown_active:
        for fill in ctx.new_fills_in_cooldown:
            sym, ref = fill.get("symbol", "?"), fill.get("ref_id", "")
            ends = f"（冷静期至 {ctx.cooldown_ends_at:%m-%d %H:%M}）" if ctx.cooldown_ends_at else ""
            hits.append(RuleHit(
                "R6", f"R6-cd:{ref}", "🔴 冷静期内交易",
                f"你正处于冷静期{ends}，却又成交了 {sym}。请复盘是否情绪化交易。",
                severity=CRIT, symbol=sym,
            ))
    detail = f"已实现亏损事件 {len(ctx.realized_events)} 起；冷静期={'是' if ctx.cooldown_active else '否'}"
    return RuleEvaluation("R6", RuleStatus.TRIGGERED if hits else RuleStatus.OK, hits, detail)


def _eval_r7(ctx, th) -> RuleEvaluation:
    """止损穿越判断。**行情质量门禁在穿越判断之前** —— 这是本规则的要害。

    冻结价（盘后收盘价）既不能判「触及」，也不能判「未触及」：拿昨天的收盘价
    宣布今天没跌破止损，与误报同样是谎报。故 FROZEN/DELAYED_FROZEN 一律
    NOT_EVALUATED，绝不落到 OK。

    质量 -> 结论：
      REALTIME / REALTIME_NONCONSOLIDATED  -> 允许 PASS(OK) 或 TRIGGERED(CRIT)
      DELAYED（有效延迟）                   -> 允许 PASS(OK) 或 TRIGGERED(最高 WARN)
      DELAYED_FROZEN / FROZEN              -> NOT_EVALUATED（不执行穿越判断）
      STALE                                -> STALE_DATA
      INVALID / MISSING / price<=0         -> INSUFFICIENT_DATA
    """
    hits = []
    n_stale = n_insufficient = n_checked = n_not_evaluated = n_with_stop = 0
    notes = []
    per_symbol: list[dict] = []
    for p in ctx.positions:
        sym = p["symbol"]
        level = ctx.stop_levels.get(sym)
        if not level:
            per_symbol.append({"symbol": sym, "status": "NO_STOP_LEVEL",
                               "reason": "无建议止损位（日线不足，未计算）"})
            continue          # 没有止损位 -> 本规则对它无事可判（页面另有「无止损位」标注）
        n_with_stop += 1
        price = ctx.current_prices.get(sym)
        q = _quality(ctx, sym)
        # 1) 价格无效 / 缺失
        if price is None or Decimal(str(price)) <= 0 or q in (PriceQuality.MISSING, PriceQuality.INVALID):
            n_insufficient += 1
            reason = f"无有效行情（{q.value}）"
            notes.append(f"{sym}:{reason}")
            per_symbol.append({"symbol": sym, "status": "INSUFFICIENT_DATA", "reason": reason})
            continue
        # 2) 质量门禁（必须先于穿越判断）：冻结价不参与任何穿越结论
        if q in _R7_FROZEN:
            n_not_evaluated += 1
            reason = "仅有收盘冻结价，不执行止损穿越判断"
            notes.append(f"{sym}:{reason}")
            per_symbol.append({"symbol": sym, "status": "NOT_EVALUATED", "reason": reason})
            continue
        # 3) 行情过期：同样不判定
        if q is PriceQuality.STALE:
            n_stale += 1
            reason = "行情过期，不执行止损穿越判断"
            notes.append(f"{sym}:{reason}")
            per_symbol.append({"symbol": sym, "status": "STALE_DATA", "reason": reason})
            continue
        n_checked += 1
        price = Decimal(str(price))
        stop = Decimal(str(level["stop_price"]))
        side = level.get("side", "LONG")
        breached = price <= stop if side == "LONG" else price >= stop

        # 滞回：已处于触发态时，价格必须回到止损线之外 hysteresis 才算解除。
        # 否则贴线震荡会让同一件事反复「触发→解除→再触发」，把通知打成机关枪。
        atr = level.get("atr")
        hysteresis = (Decimal(str(atr)) * th.r7_hysteresis_atr) if atr else Decimal("0")
        held = False
        if not breached and sym in ctx.r7_active and hysteresis > 0:
            release = stop + hysteresis if side == "LONG" else stop - hysteresis
            held = price < release if side == "LONG" else price > release
            if held:
                breached = True     # 仍在滞回带内，维持触发状态

        if not breached:
            per_symbol.append({
                "symbol": sym, "status": "OK",
                "reason": f"{q.value} 现价 {price:,.2f}，未触及止损位 {stop:,.2f}",
            })
            continue
        per_symbol.append({
            "symbol": sym, "status": "TRIGGERED",
            "reason": (f"{q.value} 现价 {price:,.2f} 已触及止损位 {stop:,.2f}"
                       if not held else
                       f"{q.value} 现价 {price:,.2f} 已回升至止损位 {stop:,.2f} 之上，"
                       f"但仍在滞回带内（需站上 {stop + hysteresis:,.2f} 才算解除）"),
        })
        arrow = "跌破" if side == "LONG" else "涨破"
        # 日线不足（新上市标的）算出的止损位仅供参考，即便行情实时也不发 CRIT
        weak_stop = not level.get("data_sufficient", True)
        if q in _R7_REALTIME and not weak_stop:
            note = "（非综合行情）" if q == PriceQuality.REALTIME_NONCONSOLIDATED else ""
            hits.append(RuleHit(
                "R7", f"R7:{sym}", f"🔴 触及建议止损位{note}",
                f"{sym}（{side}）现价 {price:,.2f} 已{arrow}建议止损位 {stop:,.2f}，考虑离场。",
                severity=CRIT, symbol=sym,
            ))
        elif weak_stop:
            period = level.get("atr_period")
            basis = f"ATR{period}" if period else "不足 14 根日线的 ATR"
            notes.append(f"{sym}:止损位数据不足，降级 WARN")
            hits.append(RuleHit(
                "R7", f"R7:{sym}", "⚠️ 触及建议止损位（数据不足，仅供参考）",
                f"{sym}（{side}）现价 {price:,.2f} 已{arrow}止损位 {stop:,.2f}；"
                f"该止损位基于 {basis}（日线不足 15 根，多为新上市标的），"
                f"仅供参考，请人工确认。",
                severity=WARN, symbol=sym,
            ))
        else:  # DELAYED → 最高 WARN
            hits.append(RuleHit(
                "R7", f"R7:{sym}", "⚠️ 触及建议止损位（延迟行情）",
                f"{sym}（{side}）延迟现价 {price:,.2f} 已{arrow}止损位 {stop:,.2f}；"
                f"行情约延迟 15 分钟，请以实时报价确认。",
                severity=WARN, symbol=sym,
            ))
    # 状态归并：没有任何标的被真正判定过时，绝不能落到 OK。
    # n_checked 只统计「行情质量足以执行穿越判断」的标的。
    n_skipped = n_not_evaluated + n_stale + n_insufficient
    if hits:
        status = RuleStatus.TRIGGERED
    elif n_with_stop == 0:
        status = RuleStatus.OK                     # 无止损位可判，与行情质量无关
    elif n_checked == 0 and n_not_evaluated:
        status = RuleStatus.NOT_EVALUATED
    elif n_checked == 0 and n_stale:
        status = RuleStatus.STALE_DATA
    elif n_checked == 0 and n_insufficient:
        status = RuleStatus.INSUFFICIENT_DATA
    elif n_checked == 0:
        status = RuleStatus.NOT_EVALUATED          # 有止损位却一个都没判 -> 不是「正常」
    elif n_skipped:
        # 判了一部分、剩下的因质量不足没判 —— 既不是「全部正常」，也不是「全未判定」
        status = RuleStatus.PARTIALLY_EVALUATED
    else:
        status = RuleStatus.OK
    counts = {"evaluated": n_checked, "not_evaluated": n_skipped,
              "triggered": len(hits), "with_stop_level": n_with_stop}
    if n_checked:
        base = f"已判定 {n_checked} 个持仓，未触及"
        if n_skipped:
            base += f"；另有 {n_skipped} 个未判定"
        detail = "；".join([base] + notes) if notes else base
    elif n_with_stop == 0:
        detail = "无建议止损位可判"
    else:
        detail = "；".join(notes) or "无可判定的持仓"
    return RuleEvaluation("R7", status, hits, detail, per_symbol=per_symbol, counts=counts)


_RULE_FUNCS = [_eval_r1, _eval_r2, _eval_r3, _eval_r4, _eval_r5, _eval_r6, _eval_r7]
_RULE_IDS = ["R1", "R2", "R3", "R4", "R5", "R6", "R7"]


def evaluate_ruleset(
    ctx: RuleContext, thresholds: RuleThresholds = DEFAULT_THRESHOLDS
) -> RuleRunResult:
    evals: list[RuleEvaluation] = []
    for rid, fn in zip(_RULE_IDS, _RULE_FUNCS):
        try:
            evals.append(fn(ctx, thresholds))
        except Exception as exc:  # noqa: BLE001 — 单条规则异常不影响其它，记 ERROR
            evals.append(RuleEvaluation(rid, RuleStatus.ERROR, detail=f"{type(exc).__name__}: {exc}"))
    return RuleRunResult(evals)


def evaluate_rules(
    ctx: RuleContext, thresholds: RuleThresholds = DEFAULT_THRESHOLDS
) -> list[RuleHit]:
    """向后兼容：返回全部命中（等价于 evaluate_ruleset(...).hits）。"""
    return evaluate_ruleset(ctx, thresholds).hits
