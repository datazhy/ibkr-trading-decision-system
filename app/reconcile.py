"""已实现盈亏对账（纯函数）。

**IBKR 的 realizedPNL 口径**（实测反推，DRAM 真实成交验证）：
    IBKR realized = (卖价 − 持仓均价) × 数量 − 卖出佣金
    (55.90 − 56.60) − 0.56035 = -1.26035 ≈ IBKR 给的 -1.260146（差 0.0002 = 均价精度）
关键点：**买入佣金已计入 averageCost**，不作为单独一项出现。

由此，下面这个公式是错的，本模块不提供，也不许任何地方再用：
    ✗ 净实现 = 毛盈亏 − 双边佣金        （买入佣金被扣两次）
上一版历史页的 -1.82 正是这么算出来的。

本模块只做两件事：
  1. 汇总权威值（executions.realized_pnl，券商已给的数）；
  2. 与账户级 reqPnL.realizedPnL 对账，把差异摆出来。
绝不自己发明盈亏公式。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

# 对账状态
MATCHED = "MATCHED"                # 差异在容差内
MISMATCH = "MISMATCH"              # 差异超容差 —— 不得伪装成已核对
INSUFFICIENT = "INSUFFICIENT"      # 任一侧缺失，无法对账

#: 账户级与成交汇总的容差（美元）。IBKR 两侧本就有均价精度差。
TOLERANCE = Decimal("0.01")

#: 持仓浮盈 vs 账户未实现的容差（相对）。
#: 这两个数来自 IBKR 的**两个不同服务**（portfolio 与 PnL），各自用各自的价格快照，
#: 同一批次取数仍有稳定差异（实测 -1561.65 vs -1983.92，差 422 = 27%）。
#: 这不是我们的错误，也修不了；容差之内视为「口径一致、数值略有出入」，
#: 超出才提示 —— 目的是发现「持仓表漏了标的」这类真问题，而不是天天报红。
UNREALIZED_REL_TOLERANCE = Decimal("0.35")


def _d(v) -> Decimal | None:
    if v is None:
        return None
    return v if isinstance(v, Decimal) else Decimal(str(v))


@dataclass
class Reconciliation:
    broker_value: Decimal | None      # 账户级权威值（reqPnL.realizedPnL，当日重置窗口）
    executions_value: Decimal | None  # 成交汇总（Σ is_current=1 的 realized_pnl）
    difference: Decimal | None        # 券商值 − 汇总值
    status: str
    executions_counted: int = 0
    executions_missing_realized: int = 0   # 缺 realized_pnl 的成交数（只能估算）
    note: str = ""

    @property
    def matched(self) -> bool:
        return self.status == MATCHED


def sum_realized(rows) -> tuple[Decimal | None, int, int]:
    """汇总 executions 的券商已实现盈亏。

    只认 realized_pnl 有值的行（那是 CommissionReport 给的权威数）。
    返回 (合计, 计入笔数, 缺 realized 的笔数)。全都没有 -> (None, 0, n)。
    """
    total = Decimal("0")
    counted = missing = 0
    for r in rows:
        v = _d(r["realized_pnl"] if not isinstance(r, dict) else r.get("realized_pnl"))
        if v is None:
            missing += 1
            continue
        total += v
        counted += 1
    return (total if counted else None), counted, missing


def sum_commission(rows) -> Decimal:
    """佣金合计。**仅作成本信息展示**，不从 IBKR 已实现盈亏里二次扣减。"""
    total = Decimal("0")
    for r in rows:
        v = _d(r["commission"] if not isinstance(r, dict) else r.get("commission"))
        if v is not None:
            total += v
    return total


def reconcile_realized(
    *,
    broker_value,                  # reqPnL.realizedPnL；None = 缺失
    execution_rows,                # 同一账户、**同一日内重置窗口**的 executions
    tolerance: Decimal = TOLERANCE,
) -> Reconciliation:
    """账户级 vs 成交汇总 对账。

    调用方须保证 execution_rows 与 broker_value 属于同一账户、同一日内重置窗口 ——
    reqPnL.realizedPnL 每个交易日重置，拿它跟任意历史筛选区间对账没有意义。
    """
    bv = _d(broker_value)
    ev, counted, missing = sum_realized(execution_rows)
    if bv is None or ev is None:
        return Reconciliation(
            broker_value=bv, executions_value=ev, difference=None,
            status=INSUFFICIENT, executions_counted=counted,
            executions_missing_realized=missing,
            note=("账户级已实现盈亏缺失" if bv is None else "成交明细无券商已实现盈亏"),
        )
    diff = bv - ev
    ok = abs(diff) <= tolerance
    return Reconciliation(
        broker_value=bv, executions_value=ev, difference=diff,
        status=MATCHED if ok else MISMATCH,
        executions_counted=counted, executions_missing_realized=missing,
        note="" if ok else "账户级与成交汇总不一致，请以账户级为准并排查成交明细",
    )


# ---------------- 三种盈亏口径（P1-5） ----------------

#: 页面必须声明：三者时间窗口与内容重叠，相加没有意义。
PNL_BASES = [
    {
        "key": "daily", "label": "今日盈亏（DailyPnL）",
        "scope": "当日交易会话",
        "content": "已实现 + 未实现的当日变动",
        "source": "IBKR reqPnL",
        "reset": "每个交易日重置",
    },
    {
        "key": "realized", "label": "已实现盈亏",
        "scope": "所选区间的平仓成交",
        "content": "落袋部分，已扣卖出佣金（买入佣金含在持仓成本里）",
        "source": "IBKR CommissionReport.realizedPNL",
        "reset": "按区间累计，不重置",
    },
    {
        "key": "unrealized", "label": "当前未实现盈亏",
        "scope": "当前持仓的时点快照",
        "content": "浮动盈亏，随价格变动",
        "source": "IBKR reqPnL",
        "reset": "无窗口，是时点值",
    },
]

PNL_BASES_WARNING = (
    "三者统计口径不同（窗口与内容互相重叠），"
    "不能相加，也不能互相验算。"
)


# ---------------- 持仓日内盈亏 vs 账户 DailyPnL（P1-6） ----------------

@dataclass
class DayPnlCheck:
    positions_sum: Decimal | None      # 持仓浮盈合计
    account_daily: Decimal | None      # 账户未实现（同口径对照项）
    difference: Decimal | None
    status: str
    covered: int = 0
    missing: int = 0
    #: 当日已实现（信息项，不参与对账 —— 它不在持仓浮盈里）
    realized_today: Decimal | None = None
    note: str = ""


def check_unrealized(*, position_upls, account_unrealized, realized_today=None,
                     rel_tolerance: Decimal = UNREALIZED_REL_TOLERANCE) -> DayPnlCheck:
    """持仓浮盈合计 vs 账户级**未实现**盈亏 —— 同口径对照。

    **为什么不用 DailyPnL 对照**：账户 DailyPnL = 已实现 + 未实现的当日变动，
    而持仓表里只有未实现（已平仓的标的根本不在表里）。拿两者相减，任何有平仓的
    交易日都必然「不一致」—— 线上就出现过 -317 vs -3032 差 2715 的红字，
    而数据完全正确，错的是把两个口径摆在一起比。

    **为什么容差是相对值且不小**：这两个数来自 IBKR 的两个不同服务，各自用各自的
    价格快照，同一批次取数仍有稳定差异（实测 -1561.65 vs -1983.92）。
    这里要发现的是「持仓表漏了标的」这类真问题，不是 IBKR 的内部精度差。
    """
    vals = [_d(v) for v in position_upls]
    covered = [v for v in vals if v is not None]
    missing = len(vals) - len(covered)
    ps = sum(covered, Decimal("0")) if covered else None
    au = _d(account_unrealized)
    rt = _d(realized_today)
    if ps is None or au is None:
        return DayPnlCheck(ps, au, None, INSUFFICIENT, len(covered), missing,
                           realized_today=rt,
                           note="缺少持仓浮盈或账户未实现，无法对照")
    diff = au - ps
    base = max(abs(au), abs(ps))
    ok = base == 0 or (abs(diff) / base) <= rel_tolerance
    return DayPnlCheck(
        ps, au, diff, MATCHED if ok else MISMATCH, len(covered), missing,
        realized_today=rt,
        note=("两者来自 IBKR 的不同服务（portfolio / PnL），存在正常精度差"
              if ok else "差异过大，可能有持仓未纳入统计，请以账户级为准"),
    )


def check_day_pnl(*, position_day_pnls, account_daily, tolerance: Decimal = TOLERANCE) -> DayPnlCheck:
    """逐持仓当日盈亏之和 vs 账户级 DailyPnL（**仅在两者同口径时可用**）。

    只有当 reqPnLSingle 全部可用、且当日无平仓时，两者才可比 ——
    实测同批取数时精确吻合（-678.4998385253915 == -678.4998385253915）。
    当日有平仓时请改用 check_unrealized。
    """
    vals = [_d(v) for v in position_day_pnls]
    covered = [v for v in vals if v is not None]
    missing = len(vals) - len(covered)
    ps = sum(covered, Decimal("0")) if covered else None
    ad = _d(account_daily)
    if ps is None or ad is None:
        return DayPnlCheck(ps, ad, None, INSUFFICIENT, len(covered), missing)
    diff = ad - ps
    return DayPnlCheck(ps, ad, diff, MATCHED if abs(diff) <= tolerance else MISMATCH,
                       len(covered), missing)
