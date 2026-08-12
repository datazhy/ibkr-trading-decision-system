"""按日未实现盈亏序列（来自持仓快照）。

收益归因缺了这一块，market_pnl 就只能倒推，`data_quality` 永远是 estimated。
补上它之后，归因等式的每一项都有独立来源，剩下的差额才真的是"未解释"。

**同一日只用同一个 snapshot_id。** 一天之内有上千份分钟级快照，混用不同快照
会让「未实现盈亏变化」里混进快照之间的持仓买卖 —— 那部分变化属于交易行为，
不是持仓浮盈浮亏的变化。所以每天固定取**最后一份**快照。

字段优先级：
    1. IBKR 原始 unrealized_pnl（券商权威，含其自己的成本口径）
    2. market_value - cost_basis（我们自己算，标 estimated）
    3. 都没有 -> None，标 insufficient_data，**绝不用 0 冒充**

**清仓不双重计算**：股票卖掉后，那部分浮盈变成已实现盈亏。持仓快照里它已经
消失，未实现盈亏自然减少；同时 Flex 的 realized_pnl 增加。两者相加正好等于
真实盈亏，不需要也不能再做任何调整 —— 手工"补回"一次就会双计。
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from . import cashflow_model as cm
from .logging_setup import get_logger

log = get_logger()

CALC_VERSION = "upnl-v1/2026-07"

ZERO = Decimal("0")

SRC_IBKR = "ibkr_unrealized"
SRC_DERIVED = "market_value_minus_cost"
SRC_NONE = "none"

Q_OK = "ok"
Q_ESTIMATED = "estimated"
Q_INSUFFICIENT = "insufficient_data"


def _d(v):
    return cm.d(v)


def _s(v):
    return None if v is None else str(v)


def position_unrealized(pos: dict) -> tuple[Decimal | None, str]:
    """单个持仓的未实现盈亏与来源。

    先用券商给的 upl；没有再用市值减成本；都没有就是 None ——
    返回 0 会让「算不出来」和「确实不赚不亏」变得无法区分。
    """
    upl = _d(pos.get("upl") or pos.get("unrealized_pnl"))
    if upl is not None:
        return upl, SRC_IBKR
    mv = _d(pos.get("market_value") or pos.get("mv"))
    qty = _d(pos.get("qty") or pos.get("quantity"))
    cost = _d(pos.get("avg_cost") or pos.get("average_cost"))
    if mv is not None and qty is not None and cost is not None:
        return mv - (qty * cost), SRC_DERIVED
    return None, SRC_NONE


def snapshot_unrealized(positions: list[dict]) -> dict:
    """一份快照的未实现盈亏合计。

    只要有一只持仓算不出来，整份快照就标 estimated —— 一个缺了几只持仓的
    "合计"不该被当成完整数字使用。
    """
    total = ZERO
    mv_total = cost_total = ZERO
    sources: set[str] = set()
    missing = 0
    rows: list[dict] = []

    for p in positions or []:
        upl, src = position_unrealized(p)
        sources.add(src)
        mv = _d(p.get("market_value") or p.get("mv"))
        qty = _d(p.get("qty") or p.get("quantity"))
        cost = _d(p.get("avg_cost") or p.get("average_cost"))
        basis = (qty * cost) if (qty is not None and cost is not None) else None
        if upl is None:
            missing += 1
        else:
            total += upl
        if mv is not None:
            mv_total += mv
        if basis is not None:
            cost_total += basis
        rows.append({
            "symbol": p.get("symbol"), "conid": p.get("conid"),
            "quantity": _s(qty), "market_value": _s(mv),
            "cost_basis": _s(basis), "unrealized_pnl": _s(upl),
            "source_field": src,
        })

    if not positions:
        # 空仓是合法状态：没有持仓就没有未实现盈亏，这是 0 而不是「算不出来」
        return {"unrealized_pnl": "0", "market_value": "0", "cost_basis": "0",
                "position_count": 0, "source_field": SRC_NONE,
                "quality_status": Q_OK, "positions": []}
    if missing == len(positions):
        return {"unrealized_pnl": None, "market_value": _s(mv_total),
                "cost_basis": _s(cost_total), "position_count": len(positions),
                "source_field": SRC_NONE, "quality_status": Q_INSUFFICIENT,
                "positions": rows}

    quality = Q_OK
    if missing:
        quality = Q_ESTIMATED
    elif SRC_DERIVED in sources:
        quality = Q_ESTIMATED       # 用市值减成本推的，不是券商原始字段

    return {
        "unrealized_pnl": _s(total), "market_value": _s(mv_total),
        "cost_basis": _s(cost_total), "position_count": len(positions),
        "source_field": (SRC_IBKR if sources == {SRC_IBKR}
                         else (SRC_DERIVED if SRC_DERIVED in sources else SRC_NONE)),
        "quality_status": quality, "positions": rows,
        "missing_positions": missing,
    }


def build_series(cfg, db, *, start: str, end: str, account: str | None = None,
                 now: datetime | None = None, persist: bool = True) -> dict:
    """构建区间内的按日未实现盈亏序列。"""
    now = now or datetime.now(timezone.utc)
    account = account if account is not None else getattr(cfg, "ib_account", None)
    try:
        eods = db.eod_snapshots_between(start, end, account)
    except Exception:  # noqa: BLE001
        eods = []

    daily: list[dict] = []
    attrib_rows: list[dict] = []
    for row in eods:
        sid = row.get("snapshot_id")
        ts = row.get("ts")
        if not (sid and ts):
            continue
        try:
            # 只取这一个 snapshot_id 的持仓 —— 绝不跨快照拼
            positions = db.positions_for_snapshot(sid)
        except Exception:  # noqa: BLE001
            positions = []
        res = snapshot_unrealized(positions)
        et = str(ts)[:10]
        daily.append({
            "account_id": account, "et_date": et, "snapshot_id": sid,
            "snapshot_ts": ts,
            "unrealized_pnl": res["unrealized_pnl"],
            "market_value": res["market_value"],
            "cost_basis": res["cost_basis"],
            "position_count": res["position_count"],
            "source_field": res["source_field"],
            "quality_status": res["quality_status"],
            "calculation_version": CALC_VERSION,
            "created_at": now.isoformat(),
        })
        for p in res["positions"]:
            attrib_rows.append({
                "account_id": account, "et_date": et, "snapshot_id": sid,
                **p, "calculation_version": CALC_VERSION,
                "created_at": now.isoformat()})

    if persist and daily:
        try:
            db.save_daily_unrealized(daily)
            db.save_position_attribution(attrib_rows)
        except Exception:  # noqa: BLE001
            log.warning("未实现盈亏落库失败（不影响返回结果）", exc_info=True)

    return {
        "series": daily, "days": len(daily),
        "attribution_rows": len(attrib_rows),
        "calculation_version": CALC_VERSION,
    }


def period_change(series: list[dict]) -> dict:
    """区间内未实现盈亏的变化。

    起点用**区间内第一天**的值，而不是区间外最近的一天 —— 用区间外的值会把
    区间之外的浮盈变化算进来。数据不足时返回 None 与原因。
    """
    usable = [r for r in series
              if r.get("unrealized_pnl") not in (None, "")]
    if len(usable) < 2:
        return {"start": None, "end": None, "change": None,
                "quality": Q_INSUFFICIENT,
                "reason": (f"区间内只有 {len(usable)} 天有可用的未实现盈亏，"
                           "至少需要 2 天才能算变化")}
    first, last = usable[0], usable[-1]
    a, b = _d(first["unrealized_pnl"]), _d(last["unrealized_pnl"])
    qualities = {r.get("quality_status") for r in usable}
    quality = Q_OK if qualities == {Q_OK} else Q_ESTIMATED
    return {
        "start": _s(a), "end": _s(b), "change": _s(b - a),
        "start_date": first["et_date"], "end_date": last["et_date"],
        "days": len(usable), "quality": quality,
        "reason": (None if quality == Q_OK else
                   "部分日期的未实现盈亏由市值减成本推算，或有持仓缺少数据"),
    }


def position_contributions(db, *, start_date: str, end_date: str,
                           account: str | None = None) -> list[dict]:
    """各持仓对未实现盈亏变化的贡献。

    只对**两端都持有**的标的给出贡献。期间买入或清仓的标的，其变化里混着
    交易行为，单独列出而不给贡献数字 —— 把清仓标的的浮盈消失算成"贡献了负值"，
    会与已实现盈亏重复计算。
    """
    a = {r["symbol"]: r for r in db.position_attribution(
        start_date, account=account, version=CALC_VERSION)}
    b = {r["symbol"]: r for r in db.position_attribution(
        end_date, account=account, version=CALC_VERSION)}
    out = []
    for sym in sorted(set(a) | set(b)):
        ra, rb = a.get(sym), b.get(sym)
        ua = _d((ra or {}).get("unrealized_pnl"))
        ub = _d((rb or {}).get("unrealized_pnl"))
        if ra and rb and ua is not None and ub is not None:
            out.append({"symbol": sym, "start_unrealized": _s(ua),
                        "end_unrealized": _s(ub), "contribution": _s(ub - ua),
                        "status": "held_throughout"})
        elif rb and not ra:
            out.append({"symbol": sym, "start_unrealized": None,
                        "end_unrealized": _s(ub), "contribution": None,
                        "status": "opened_during_period",
                        "note": "期间新建仓，其浮盈不属于期初持仓的变化"})
        elif ra and not rb:
            out.append({"symbol": sym, "start_unrealized": _s(ua),
                        "end_unrealized": None, "contribution": None,
                        "status": "closed_during_period",
                        "note": ("期间已清仓，浮盈已转为已实现盈亏，"
                                 "不在此处重复计算")})
    return out
