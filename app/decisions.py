"""主动交易决策的识别与分类（纯函数）。

三种计数互不等同，禁止混用同一标签：
  - order      一次委托指令           （orders.order_id / permId）
  - execution  一次成交回报，部分成交多条（executions.exec_id）
  - decision   一次主动交易决策        （decision_key = decision_id ?? perm_id）

同一 decision_key 的多笔部分成交只算一次决策。

R4 数「风险增加」的决策（OPEN/ADD/REVERSAL）；R5 数全部主动决策
（OPEN/ADD/REDUCE/CLOSE/REVERSAL）。强平、公司行动、自动行权、系统修正
都不是你的决策，一律排除。
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

# 决策类型
OPEN = "OPEN"            # 0 -> 非0
ADD = "ADD"              # 同向加仓
REDUCE = "REDUCE"        # 同向减仓（未平完）
CLOSE = "CLOSE"          # 非0 -> 0
REVERSAL = "REVERSAL"    # 方向翻转：必须单独标记，不得静默当成一次普通 ADD
UNKNOWN = "UNKNOWN"      # 无法还原成交前持仓 -> 不得计入 R4

#: 风险增加型决策（R4 只数这些）。REVERSAL 建立了反向新仓，属于风险增加，
#: 但类型独立保留，便于在 evidence 中与普通加仓区分。
RISK_INCREASING = {OPEN, ADD, REVERSAL}

#: 非主动决策的成交来源：强平/公司行动/自动行权/系统修正，都不计入 R4/R5。
_NON_DISCRETIONARY_PREFIXES = ("CORP", "EXERCISE", "ASSIGN", "ADJUST", "SPLIT", "DIV")


def _d(v) -> Decimal:
    if isinstance(v, Decimal):
        return v
    return Decimal(str(v)) if v is not None else Decimal("0")


def decision_key(row) -> str | None:
    """decision_key = COALESCE(decision_id, perm_id)。两者皆无 -> None（无法归组）。"""
    def get(k):
        try:
            return row[k]
        except (KeyError, IndexError, TypeError):
            return None
    did = get("decision_id")
    if did:
        return str(did)
    pid = get("perm_id")
    return str(pid) if pid else None


def is_discretionary(row) -> bool:
    """是否为主动交易决策（排除强平/公司行动/自动行权/系统修正）。"""
    def get(k):
        try:
            return row[k]
        except (KeyError, IndexError, TypeError):
            return None
    if get("liquidation"):
        return False
    ref = (get("order_ref") or "").upper()
    if any(ref.startswith(p) for p in _NON_DISCRETIONARY_PREFIXES):
        return False
    return True


def signed_shares(row) -> Decimal:
    """带方向的成交数量：BUY 为正，SELL 为负。"""
    def get(k):
        try:
            return row[k]
        except (KeyError, IndexError, TypeError):
            return None
    qty = _d(get("shares"))
    side = (get("side") or "").upper()
    return -qty if side == "SELL" else qty


def classify_decision(pre_qty, post_qty) -> str:
    """按成交前后的**有符号**持仓判定决策类型。

    绝不用买卖方向猜：空头账户里 BUY 是减仓，不是加仓。
    """
    pre, post = _d(pre_qty), _d(post_qty)
    if pre == 0 and post != 0:
        return OPEN
    if pre != 0 and post == 0:
        return CLOSE
    if pre == 0 and post == 0:
        return UNKNOWN                       # 净额为 0 的一组成交，无法归类
    if (pre > 0) != (post > 0):
        return REVERSAL                      # 方向翻转
    return ADD if abs(post) > abs(pre) else (REDUCE if abs(post) < abs(pre) else UNKNOWN)


@dataclass
class Decision:
    key: str
    symbol: str
    et_date: str                 # 美东交易日（所有日期边界用 ET，不用 UTC 自然日）
    kind: str                    # OPEN/ADD/REDUCE/CLOSE/REVERSAL/UNKNOWN
    signed_shares: Decimal       # 该决策的净成交量（带方向）
    executions: int              # 该决策包含的成交笔数（部分成交 > 1）
    pre_qty: Decimal | None
    post_qty: Decimal | None
    first_ts: str


def group_decisions(rows, *, pre_qty_lookup, et_date_of) -> list[Decision]:
    """把 executions 按 decision_key 归组并分类。

    rows: 已过滤 is_current=1 的 executions（任意顺序）
    pre_qty_lookup(symbol, ts) -> 成交前有符号持仓；None = 无法还原
    et_date_of(ts_iso) -> 美东交易日
    """
    groups: dict[str, dict] = {}
    for r in rows:
        if not is_discretionary(r):
            continue
        key = decision_key(r)
        if key is None:
            continue                          # 无 permId 无法归组，宁可不计也不错计
        sym = r["symbol"]
        ts = r["execution_time"]
        g = groups.get(key)
        if g is None:
            groups[key] = g = {"symbol": sym, "first_ts": ts, "net": Decimal("0"), "n": 0}
        g["n"] += 1
        g["net"] += signed_shares(r)
        if ts < g["first_ts"]:
            g["first_ts"] = ts

    out: list[Decision] = []
    for key, g in groups.items():
        pre = pre_qty_lookup(g["symbol"], g["first_ts"])
        if pre is None:
            kind, post = UNKNOWN, None
        else:
            pre = _d(pre)
            post = pre + g["net"]
            kind = classify_decision(pre, post)
        out.append(Decision(
            key=key, symbol=g["symbol"], et_date=et_date_of(g["first_ts"]), kind=kind,
            signed_shares=g["net"], executions=g["n"],
            pre_qty=pre if pre is not None else None, post_qty=post, first_ts=g["first_ts"],
        ))
    out.sort(key=lambda d: d.first_ts)
    return out


def merge_realized_by_decision(rows) -> list[dict]:
    """把 executions 的已实现盈亏按 decision_key 合并成「一次认亏」。

    **为什么必须合并**（线上真实案例）：一次清仓 KMEM 1786 股被券商拆成 7 笔部分成交，
    每笔已实现 -122 ~ -737，单笔最大仅占净资产 1.82% < 2% 阈值 —— R6 一声没响。
    但这 7 笔是同一个决策，合计 -2192.67 = 净资产的 5.4%。

    R6 的「单笔」本意是「一次认亏决策」，不是「一条成交回报」。部分成交纯粹是
    券商的撮合细节，不该成为绕过风控的通道。

    返回 [{symbol, realized, ref_id, ts, executions}]，ref_id 用 decision_key
    以保证 R6 的 dedup_key 稳定（同一决策只触发一次冷静期）。
    """
    groups: dict[str, dict] = {}
    for r in rows:
        v = r["realized_pnl"] if not isinstance(r, dict) else r.get("realized_pnl")
        if v is None:
            continue
        key = decision_key(r) or (r["exec_id"] if not isinstance(r, dict)
                                  else r.get("exec_id"))
        if key is None:
            continue
        g = groups.get(key)
        if g is None:
            groups[key] = g = {
                "symbol": r["symbol"], "realized": Decimal("0"),
                "ref_id": key, "ts": r["execution_time"], "executions": 0,
            }
        g["realized"] += _d(v)
        g["executions"] += 1
        if r["execution_time"] < g["ts"]:
            g["ts"] = r["execution_time"]
    return sorted(groups.values(), key=lambda x: x["ts"])


def risk_increasing_by_symbol(decisions, et_date: str) -> dict[str, int]:
    """R4 用：指定美东交易日内，各标的的风险增加决策次数。"""
    out: dict[str, int] = {}
    for d in decisions:
        if d.et_date == et_date and d.kind in RISK_INCREASING:
            out[d.symbol] = out.get(d.symbol, 0) + 1
    return out


def classification_complete(decisions, et_date: str | None = None) -> bool:
    """该范围内的决策是否全部分类成功。存在 UNKNOWN -> R4 必须 INSUFFICIENT_DATA。"""
    for d in decisions:
        if et_date is not None and d.et_date != et_date:
            continue
        if d.kind == UNKNOWN:
            return False
    return True


def count_decisions(decisions) -> int:
    """R5 用：主动决策总数（含未分类）。"""
    return len(decisions)


def kind_summary(decisions) -> dict[str, int]:
    out: dict[str, int] = {}
    for d in decisions:
        out[d.kind] = out.get(d.kind, 0) + 1
    return out
