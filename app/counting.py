"""交易计数的**唯一入口**（DB -> decisions 的薄适配层）。

首页、规则页、历史页必须都走这里，否则同一个"笔数"会在三个页面上是三个数
——线上就发生过：首页说本月 0 笔，历史页说 2 笔，规则页说 2 次决策。

三种口径不可混用（命名上就不许含糊）：
  decisions_month     本月主动交易决策数   -> R5 口径
  executions_month    本月成交回报数       -> 仅审计展示，不驱动任何规则
  risk_increasing_today  今日风险增加决策  -> R4 口径
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from .decisions import (
    Decision, classification_complete, group_decisions, kind_summary,
    risk_increasing_by_symbol,
)


def load_decisions(db, market_tz, start_iso: str, end_iso: str | None = None) -> list[Decision]:
    """区间内的主动交易决策（成交按 decision_key 归组、按有符号持仓分类）。"""
    rows = db.executions_between(start_iso, end_iso)
    return group_decisions(
        rows,
        pre_qty_lookup=lambda sym, ts: db.qty_before(sym, ts),
        et_date_of=lambda ts: datetime.fromisoformat(ts).astimezone(market_tz)
                                      .date().isoformat(),
    )


@dataclass
class TradingCounts:
    decisions_month: int = 0                 # R5 口径
    executions_month: int = 0                # 仅审计
    risk_increasing_today: dict[str, int] = field(default_factory=dict)   # R4 口径
    risk_increasing_today_total: int = 0
    kinds_month: dict[str, int] = field(default_factory=dict)
    classification_complete_today: bool = True
    classification_complete_month: bool = True
    today_et: str = ""
    decisions: list = field(default_factory=list)


def trading_counts(db, market_tz, *, month_start_iso: str, today_et: str) -> TradingCounts:
    """本月/当日的统一计数。所有页面与规则引擎共用此结果。"""
    decs = load_decisions(db, market_tz, month_start_iso)
    execs = db.executions_between(month_start_iso)
    ri = risk_increasing_by_symbol(decs, today_et)
    return TradingCounts(
        decisions_month=len(decs),
        executions_month=len(execs),
        risk_increasing_today=ri,
        risk_increasing_today_total=sum(ri.values()),
        kinds_month=kind_summary(decs),
        classification_complete_today=classification_complete(decs, today_et),
        classification_complete_month=classification_complete(decs),
        today_et=today_et,
        decisions=decs,
    )
