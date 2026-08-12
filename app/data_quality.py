"""数据质量层：对一份账户快照做体检，产出问题清单与整体结论。

设计原则（与本项目一贯的数据纪律一致）：

1. **纯函数**。所有规则只接受数据、返回结论，不做 IO、不读时钟（now 由调用方传入）。
   这样每条规则都能被单测精确覆盖，也能对历史快照回放。
2. **不确定就说不确定**。缺数据时产出 issue，而不是用默认值把窟窿填上 ——
   一个「看起来正常」的错误结论，比一个明确的「数据不可用」危险得多。
3. **阈值全部可配**。金融数据的容差与账户规模、币种、结算周期强相关，
   写死必然在别的账户上误报。见 config.Config 的 dq_* 字段。
4. **误报有代价**。天天报警等于没有报警，所以合理性检查一律带相对容差，
   且对「已知会不一致的口径」（如未结算资金、多币种折算）预留缓冲。

整体状态只有三档：
    fresh  数据新鲜且可信，可正常展示与分析
    stale  数据本身可信但已过期 —— 可以看，不能当成实时
    error  存在 critical 问题 —— 不可用于可靠分析

`ai_analysis_allowed` 是给后续 AI 分析的硬门禁，判定见 evaluate() 末尾。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable

# ---- 整体状态 ----
FRESH = "fresh"
STALE = "stale"
ERROR = "error"

# ---- 严重程度 ----
INFO = "info"
WARNING = "warning"
CRITICAL = "critical"

# ---- 问题分类 ----
COMPLETENESS = "completeness"     # 完整性
FRESHNESS = "freshness"           # 时效性
SANITY = "sanity"                 # 合理性
CONTINUITY = "continuity"         # 重复与连续性
SOURCE = "source"                 # 数据源状态


@dataclass(frozen=True)
class Issue:
    issue_code: str
    severity: str
    category: str
    message: str
    affected_field: str | None = None
    expected_value: Any = None
    actual_value: Any = None
    blocks_ai: bool = False

    def as_dict(self, detected_at: str | None = None) -> dict:
        d = {
            "issue_code": self.issue_code, "severity": self.severity,
            "category": self.category, "message": self.message,
            "affected_field": self.affected_field,
            "expected_value": self.expected_value, "actual_value": self.actual_value,
            "blocks_ai": self.blocks_ai,
        }
        if detected_at:
            d["detected_at"] = detected_at
        return d


@dataclass
class QualityThresholds:
    """可配置阈值。默认值针对「单账户、美元、延迟行情」的典型场景。"""
    #: 交易时段内数据超过多少秒算过期
    stale_seconds_market: int = 300
    #: 非交易时段（盘后/休市）的过期阈值 —— 本就低频更新，不能用盘中标准
    stale_seconds_closed: int = 7200
    #: 超过该秒数视为严重过期，直接 error 且禁止 AI 分析
    error_seconds: int = 86400
    #: 持仓明细市值合计 vs 账户持仓总市值：相对容差
    positions_value_rel_tol: Decimal = Decimal("0.02")
    #: 净资产 ≈ 现金 + 持仓市值 的相对容差（未结算资金/多币种折算会天然有偏差）
    equity_identity_rel_tol: Decimal = Decimal("0.05")
    #: 相邻快照净资产跳变超过该比例 → 警告（可能是资金流，也可能是数据错误）
    equity_jump_rel: Decimal = Decimal("0.20")
    #: 当日盈亏占净资产比例超过该值 → 警告（相对账户规模明显不匹配）
    daily_pnl_rel_warn: Decimal = Decimal("0.20")
    #: 单只持仓市值占净资产超过该比例且无融资迹象 → 警告
    single_position_over_equity: Decimal = Decimal("1.00")
    #: 同一账户同一数据时间的重复快照容忍秒数（小于该间隔视为重复）
    duplicate_window_seconds: int = 5


def _d(v: Any) -> Decimal | None:
    if v is None or v == "":
        return None
    if isinstance(v, Decimal):
        return v
    try:
        return Decimal(str(v))
    except (InvalidOperation, ValueError, TypeError):
        return None


def _rel_diff(a: Decimal, b: Decimal) -> Decimal:
    """相对差异，分母取两者绝对值较大者（避免除零与小分母放大）。"""
    scale = max(abs(a), abs(b))
    if scale == 0:
        return Decimal("0")
    return abs(a - b) / scale


# ---------------- 1. 完整性 ----------------

def check_completeness(balances: dict, positions: list[dict]) -> list[Issue]:
    out: list[Issue] = []
    equity = _d(balances.get("equity"))
    if equity is None:
        out.append(Issue("NET_LIQ_MISSING", CRITICAL, COMPLETENESS,
                         "账户净资产缺失，无法进行任何风险计算",
                         "equity", "非空", None, blocks_ai=True))
    if _d(balances.get("cash")) is None:
        out.append(Issue("CASH_MISSING", CRITICAL, COMPLETENESS,
                         "现金余额缺失", "cash", "非空", None, blocks_ai=True))
    if positions is None:
        out.append(Issue("POSITIONS_MISSING", CRITICAL, COMPLETENESS,
                         "持仓列表缺失（不是空仓，是没取到）",
                         "positions", "列表", None, blocks_ai=True))
        return out

    pos_value = _d(balances.get("positions_value"))
    if pos_value is not None and pos_value != 0 and not positions:
        out.append(Issue("POSITIONS_EMPTY_BUT_VALUE", CRITICAL, COMPLETENESS,
                         "账户显示有持仓市值，但持仓明细为空 —— 快照不完整",
                         "positions", f"合计 {pos_value}", "0 条", blocks_ai=True))

    if not balances.get("currency"):
        out.append(Issue("CURRENCY_MISSING", WARNING, COMPLETENESS,
                         "币种字段缺失，多币种场景下金额口径无法确认", "currency"))

    for p in positions:
        if p.get("symbol") in (None, ""):
            out.append(Issue("POSITION_SYMBOL_MISSING", CRITICAL, COMPLETENESS,
                             "存在无标的代码的持仓记录", "symbol", "非空", None,
                             blocks_ai=True))
        if _d(p.get("qty")) is None:
            out.append(Issue("POSITION_QTY_MISSING", CRITICAL, COMPLETENESS,
                             f"{p.get('symbol')} 持仓数量缺失", "qty", "非空", None,
                             blocks_ai=True))
    return out


def check_positions_value_consistency(balances: dict, positions: list[dict],
                                      th: QualityThresholds) -> list[Issue]:
    """持仓明细市值合计 vs 账户持仓总市值。"""
    acct = _d(balances.get("positions_value"))
    if acct is None or not positions:
        return []
    # GrossPositionValue 是多空绝对市值合计；明细也必须按 gross 口径对账。
    detail = sum((abs(_d(p.get("market_value")) or Decimal("0")) for p in positions),
                 Decimal("0"))
    if _rel_diff(acct, detail) > th.positions_value_rel_tol:
        return [Issue("POSITIONS_VALUE_MISMATCH", CRITICAL, COMPLETENESS,
                      "持仓明细市值合计与账户持仓总市值严重不一致，快照可能残缺",
                      "positions_value", str(acct), str(detail), blocks_ai=True)]
    return []


# ---------------- 2. 时效性 ----------------

def check_freshness(data_as_of: str | None, now: datetime, *, market_open: bool,
                    th: QualityThresholds) -> list[Issue]:
    """距离数据时点的秒数是否超过阈值。交易时段与休市用不同标准。"""
    if not data_as_of:
        return [Issue("DATA_AS_OF_MISSING", CRITICAL, FRESHNESS,
                      "数据时点缺失，无法判断新鲜度", "data_as_of", "非空", None,
                      blocks_ai=True)]
    try:
        age = (now - datetime.fromisoformat(data_as_of)).total_seconds()
    except (TypeError, ValueError):
        return [Issue("DATA_AS_OF_INVALID", CRITICAL, FRESHNESS,
                      "数据时点格式非法", "data_as_of", "ISO 时间", data_as_of,
                      blocks_ai=True)]
    limit = th.stale_seconds_market if market_open else th.stale_seconds_closed
    if age >= th.error_seconds:
        return [Issue("DATA_SEVERELY_STALE", CRITICAL, FRESHNESS,
                      f"数据已过期 {age / 3600:.1f} 小时，不可用于任何可靠分析",
                      "data_as_of", f"< {th.error_seconds}s", f"{age:.0f}s",
                      blocks_ai=True)]
    if age > limit:
        return [Issue("DATA_STALE", WARNING, FRESHNESS,
                      f"数据已过期 {age / 60:.0f} 分钟（{'交易时段' if market_open else '休市时段'}"
                      f"阈值 {limit / 60:.0f} 分钟），不得当作实时数据",
                      "data_as_of", f"< {limit}s", f"{age:.0f}s")]
    return []


# ---------------- 3. 合理性 ----------------

def check_sanity(balances: dict, positions: list[dict], pnl: dict | None,
                 th: QualityThresholds) -> list[Issue]:
    out: list[Issue] = []
    equity = _d(balances.get("equity"))
    cash = _d(balances.get("cash"))

    if equity is not None and equity < 0:
        out.append(Issue("NET_LIQ_NEGATIVE", CRITICAL, SANITY,
                         "净资产为负 —— 账户可能爆仓，或数据口径错误",
                         "equity", ">= 0", str(equity), blocks_ai=True))

    bp = _d(balances.get("buying_power"))
    if bp is not None and bp < 0:
        out.append(Issue("BUYING_POWER_NEGATIVE", WARNING, SANITY,
                         "购买力为负", "buying_power", ">= 0", str(bp)))

    for p in positions or []:
        sym = p.get("symbol")
        qty, mv, price = _d(p.get("qty")), _d(p.get("market_value")), _d(p.get("price"))
        cost = _d(p.get("avg_cost"))
        if qty is not None and qty == 0 and mv is not None and mv != 0:
            out.append(Issue("QTY_ZERO_VALUE_NONZERO", CRITICAL, SANITY,
                             f"{sym} 持仓数量为 0 但市值不为 0", "qty", "0 → 市值 0",
                             f"qty=0, mv={mv}", blocks_ai=True))
        if price is not None and price <= 0:
            out.append(Issue("PRICE_NOT_POSITIVE", WARNING, SANITY,
                             f"{sym} 市价 <= 0，该标的的风险计算不可信",
                             "price", "> 0", str(price)))
        if cost is not None and cost < 0:
            out.append(Issue("AVG_COST_NEGATIVE", WARNING, SANITY,
                             f"{sym} 成本价为负", "avg_cost", ">= 0", str(cost)))
        # 单只持仓市值显著大于净资产，且没有融资迹象（现金非负）→ 可疑
        if (equity is not None and equity > 0 and mv is not None
                and abs(mv) > equity * th.single_position_over_equity
                and (cash is None or cash >= 0)):
            out.append(Issue("POSITION_EXCEEDS_EQUITY", WARNING, SANITY,
                             f"{sym} 单只持仓市值超过账户净资产，但未检测到融资",
                             "market_value", f"<= {equity}", str(mv)))

    # 当日盈亏与账户规模严重不匹配
    daily = _d((pnl or {}).get("daily_pnl"))
    if daily is not None and equity and equity > 0:
        if abs(daily) > equity * th.daily_pnl_rel_warn:
            out.append(Issue("DAILY_PNL_OUT_OF_SCALE", WARNING, SANITY,
                             f"当日盈亏占净资产 {abs(daily) / equity:.1%}，与账户规模不匹配",
                             "daily_pnl", f"<= {th.daily_pnl_rel_warn:.0%}", str(daily)))

    # 净资产 ≈ 现金 + 持仓市值（大额不可解释偏差）
    pos_value = _d(balances.get("positions_value"))
    if equity is not None and cash is not None and pos_value is not None:
        implied = cash + pos_value
        if _rel_diff(equity, implied) > th.equity_identity_rel_tol:
            out.append(Issue("EQUITY_IDENTITY_MISMATCH", WARNING, SANITY,
                             "净资产与「现金 + 持仓市值」存在大额偏差，"
                             "可能是未结算资金或多币种折算，也可能是取数口径错误",
                             "equity", str(implied), str(equity)))
    return out


# ---------------- 4. 重复与连续性 ----------------

def check_continuity(balances: dict, positions: list[dict], *, prev_balances: dict | None,
                     prev_positions: list[dict] | None, recent_exec_symbols: set[str],
                     now_ts: str | None, th: QualityThresholds) -> list[Issue]:
    out: list[Issue] = []
    if not prev_balances:
        return out

    # 重复快照：同账户、数据时间几乎相同
    prev_ts, cur_ts = prev_balances.get("ts"), now_ts
    if prev_ts and cur_ts:
        try:
            gap = abs((datetime.fromisoformat(cur_ts)
                       - datetime.fromisoformat(prev_ts)).total_seconds())
            if gap < th.duplicate_window_seconds:
                out.append(Issue("DUPLICATE_SNAPSHOT", INFO, CONTINUITY,
                                 f"与上一份快照间隔仅 {gap:.1f} 秒，疑似重复生成",
                                 "ts", f">= {th.duplicate_window_seconds}s", f"{gap:.1f}s"))
        except (TypeError, ValueError):
            pass

    # 净资产跳变
    prev_eq, cur_eq = _d(prev_balances.get("equity")), _d(balances.get("equity"))
    if prev_eq and cur_eq is not None and prev_eq > 0:
        if _rel_diff(prev_eq, cur_eq) > th.equity_jump_rel:
            out.append(Issue("EQUITY_JUMP", WARNING, CONTINUITY,
                             f"净资产较上一份快照跳变 {_rel_diff(prev_eq, cur_eq):.1%}，"
                             "可能是资金流（需登记）或数据异常",
                             "equity", str(prev_eq), str(cur_eq)))

    # 持仓消失/大幅变化但无成交
    prev_map = {p["symbol"]: p for p in (prev_positions or []) if p.get("symbol")}
    cur_map = {p["symbol"]: p for p in (positions or []) if p.get("symbol")}
    for sym, prev_p in prev_map.items():
        if sym in recent_exec_symbols:
            continue                       # 有成交可解释，正常
        if sym not in cur_map:
            out.append(Issue("POSITION_VANISHED", WARNING, CONTINUITY,
                             f"{sym} 持仓消失但没有对应成交记录", "positions",
                             "有持仓或有成交", "已消失"))
            continue
        pq, cq = _d(prev_p.get("qty")), _d(cur_map[sym].get("qty"))
        if pq and cq is not None and pq != 0 and _rel_diff(pq, cq) > Decimal("0.5"):
            out.append(Issue("POSITION_QTY_JUMP", WARNING, CONTINUITY,
                             f"{sym} 持仓数量大幅变化但无成交记录", "qty",
                             str(pq), str(cq)))
    return out


# ---------------- 5. 数据源状态 ----------------

def check_source(*, connected: bool, account_data_ok: bool, quote_qualities: dict[str, int],
                 partial_fields: Iterable[str] = (), auth_problem: str | None = None
                 ) -> list[Issue]:
    out: list[Issue] = []
    if not connected:
        out.append(Issue("BROKER_DISCONNECTED", CRITICAL, SOURCE,
                         "IBKR 连接不可用，数据无法刷新", "connection", "已连接",
                         "断开", blocks_ai=True))
    if not account_data_ok:
        out.append(Issue("ACCOUNT_DATA_UNAVAILABLE", CRITICAL, SOURCE,
                         "账户数据不可用", "account", "可用", "不可用", blocks_ai=True))
    if auth_problem:
        out.append(Issue("BROKER_AUTH_PROBLEM", CRITICAL, SOURCE,
                         f"券商会话或权限异常：{auth_problem}", "auth", "正常",
                         auth_problem, blocks_ai=True))

    total = sum(quote_qualities.values()) if quote_qualities else 0
    if total:
        delayed = sum(v for k, v in quote_qualities.items()
                      if k in ("DELAYED", "DELAYED_FROZEN"))
        missing = sum(v for k, v in quote_qualities.items()
                      if k in ("MISSING", "INVALID"))
        if missing:
            out.append(Issue("QUOTES_UNAVAILABLE", WARNING, SOURCE,
                             f"{missing}/{total} 个标的无有效行情，其风险指标不可信",
                             "price", "全部有效", f"{missing} 个缺失"))
        if delayed:
            out.append(Issue("QUOTES_DELAYED", INFO, SOURCE,
                             f"{delayed}/{total} 个标的为延迟行情 —— 展示可用，"
                             "但不足以支撑止损穿越等实时判断",
                             "price_quality", "REALTIME", "DELAYED"))
    for f in partial_fields:
        out.append(Issue("PARTIAL_FIELDS", WARNING, SOURCE,
                         f"数据源仅返回部分字段：{f}", f, "完整", "缺失"))
    return out


# ---------------- 汇总 ----------------

@dataclass
class QualityReport:
    status: str                       # fresh / stale / error
    ai_analysis_allowed: bool
    issues: list[Issue] = field(default_factory=list)

    @property
    def critical(self) -> list[Issue]:
        return [i for i in self.issues if i.severity == CRITICAL]

    @property
    def warnings(self) -> list[Issue]:
        return [i for i in self.issues if i.severity == WARNING]

    def as_dicts(self, detected_at: str | None = None) -> list[dict]:
        return [i.as_dict(detected_at) for i in self.issues]

    def summary(self) -> dict:
        return {"status": self.status, "ai_analysis_allowed": self.ai_analysis_allowed,
                "critical": len(self.critical), "warning": len(self.warnings),
                "total": len(self.issues)}


def evaluate(*, balances: dict, positions: list[dict], now: datetime,
             data_as_of: str | None, market_open: bool,
             pnl: dict | None = None, prev_balances: dict | None = None,
             prev_positions: list[dict] | None = None,
             recent_exec_symbols: set[str] | None = None,
             connected: bool = True, account_data_ok: bool = True,
             quote_qualities: dict[str, int] | None = None,
             partial_fields: Iterable[str] = (), auth_problem: str | None = None,
             thresholds: QualityThresholds | None = None,
             has_valid_history: bool = True) -> QualityReport:
    """跑全部规则，给出整体结论。

    整体状态：任一 critical → error；否则有过期问题 → stale；否则 fresh。
    AI 门禁（宁可不分析，也不能基于坏数据给出确定性结论）：
      - 任一 critical 问题 → false
      - 核心字段缺失 → false（由 completeness 的 critical 覆盖）
      - 严重过期 → false（由 DATA_SEVERELY_STALE 覆盖）
      - 同步失败且无可用历史快照 → false
      - 仅 warning → true，但必须把 warning 列表一并交给 AI
    """
    th = thresholds or QualityThresholds()
    issues: list[Issue] = []
    issues += check_completeness(balances, positions)
    issues += check_positions_value_consistency(balances, positions, th)
    issues += check_freshness(data_as_of, now, market_open=market_open, th=th)
    issues += check_sanity(balances, positions, pnl, th)
    issues += check_continuity(balances, positions, prev_balances=prev_balances,
                               prev_positions=prev_positions,
                               recent_exec_symbols=recent_exec_symbols or set(),
                               now_ts=data_as_of, th=th)
    issues += check_source(connected=connected, account_data_ok=account_data_ok,
                           quote_qualities=quote_qualities or {},
                           partial_fields=partial_fields, auth_problem=auth_problem)

    if not has_valid_history:
        issues.append(Issue("NO_VALID_SNAPSHOT", CRITICAL, SOURCE,
                            "同步失败且没有可用的历史快照", "snapshot", "存在",
                            "无", blocks_ai=True))

    has_critical = any(i.severity == CRITICAL for i in issues)
    is_stale = any(i.issue_code in ("DATA_STALE", "DATA_SEVERELY_STALE") for i in issues)
    if has_critical:
        status = ERROR
    elif is_stale:
        status = STALE
    else:
        status = FRESH

    ai_allowed = not any(i.blocks_ai for i in issues)
    return QualityReport(status=status, ai_analysis_allowed=ai_allowed, issues=issues)


def thresholds_from_config(cfg) -> QualityThresholds:
    """从 Config 构造阈值。集中在这里，规则函数只认 QualityThresholds。"""
    return QualityThresholds(
        stale_seconds_market=cfg.dq_stale_seconds_market,
        stale_seconds_closed=cfg.dq_stale_seconds_closed,
        error_seconds=cfg.dq_error_seconds,
        positions_value_rel_tol=Decimal(cfg.dq_positions_value_rel_tol),
        equity_identity_rel_tol=Decimal(cfg.dq_equity_identity_rel_tol),
        equity_jump_rel=Decimal(cfg.dq_equity_jump_rel),
        daily_pnl_rel_warn=Decimal(cfg.dq_daily_pnl_rel_warn),
        single_position_over_equity=Decimal(cfg.dq_single_position_over_equity),
    )
