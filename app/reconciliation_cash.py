"""现金对账（纯函数 + 一个落库入口）。

检验等式：

    期末现金 ≈ 期初现金 + 外部现金流 + 交易现金流 + 分红 + 利息 - 费用 - 税费

对不上的常见原因是**结算日差异**：一笔 T+1 结算的卖出，成交日已进流水，
资金要到次日才反映在现金余额里。所以容差必须可配置，且默认给得比较宽松。

**对账失败绝不修改原始流水**。对账是一种检验，不是一种修正 —— 用对账结果去
「调平」流水，等于把问题藏起来。
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

from . import cashflow_model as cm
from .logging_setup import get_logger

log = get_logger()

CALC_VERSION = "recon-v1/2026-07"

ST_RECONCILED = "reconciled"
ST_WITHIN_TOLERANCE = "within_tolerance"
ST_MISMATCH = "mismatch"
ST_INSUFFICIENT = "insufficient_data"

ZERO = Decimal("0")

#: 完全对平的判定阈值（基础币种）。小于它视为浮点与舍入噪音。
EXACT_EPSILON = Decimal("0.01")


def _d(v) -> Decimal | None:
    return cm.d(v)


def _s(v) -> str | None:
    return None if v is None else str(v)


def compute(*, opening_cash, closing_cash, totals: dict,
            tolerance: Decimal) -> dict:
    """给定期初期末现金与分类合计，算出对账结果。

    totals 的键是 cashflow_model 的一级分类。
    """
    o, c = _d(opening_cash), _d(closing_cash)

    def t(cat: str) -> Decimal:
        return _d(totals.get(cat)) or ZERO

    external = t(cm.CAT_EXTERNAL) + t(cm.CAT_TRANSFER)
    trade = t(cm.CAT_TRADE)
    income = t(cm.CAT_INCOME)
    expense = t(cm.CAT_EXPENSE)
    tax = t(cm.CAT_TAX)
    interest = t(cm.CAT_INTEREST)
    other = t(cm.CAT_CORPORATE) + t(cm.CAT_UNKNOWN)

    # 注意：expense / tax 在 Flex 里本身就是负数，直接相加即可，
    # 再减一次会把符号搞反 —— 这是这段计算最容易写错的地方。
    movement = external + trade + income + expense + tax + interest + other

    issues: list[str] = []
    if o is None or c is None:
        issues.append("缺少期初或期末现金余额，无法对账")
        return {
            "opening_cash": _s(o), "closing_cash": _s(c),
            "external_cash_flow": _s(external), "trade_cash_flow": _s(trade),
            "income": _s(income), "expense": _s(expense), "tax": _s(tax),
            "interest": _s(interest), "other": _s(other),
            "expected_closing_cash": None, "reconciliation_difference": None,
            "tolerance": _s(tolerance), "quality_status": ST_INSUFFICIENT,
            "issues": issues,
        }

    expected = o + movement
    diff = c - expected

    if totals.get(cm.CAT_UNKNOWN):
        issues.append("存在未分类流水，对账差异可能由此产生")

    if abs(diff) <= EXACT_EPSILON:
        status = ST_RECONCILED
    elif abs(diff) <= tolerance:
        status = ST_WITHIN_TOLERANCE
        issues.append(f"差异 {diff} 在容差 {tolerance} 内，"
                      "常见原因是结算日差异或未结算资金")
    else:
        status = ST_MISMATCH
        issues.append(f"差异 {diff} 超出容差 {tolerance}，"
                      "请检查报表区间是否完整、是否存在未覆盖的流水类型")

    return {
        "opening_cash": _s(o), "closing_cash": _s(c),
        "external_cash_flow": _s(external), "trade_cash_flow": _s(trade),
        "income": _s(income), "expense": _s(expense), "tax": _s(tax),
        "interest": _s(interest), "other": _s(other),
        "expected_closing_cash": _s(expected),
        "reconciliation_difference": _s(diff),
        "tolerance": _s(tolerance), "quality_status": status,
        "issues": issues,
    }


def reconcile_period(cfg, db, *, account: str | None, start: str, end: str,
                     currency: str = "USD",
                     now: datetime | None = None) -> dict:
    """对某区间做对账并落库。**不修改任何原始流水。**"""
    now = now or datetime.now(timezone.utc)
    tolerance = Decimal(str(getattr(cfg, "flex_reconciliation_tolerance", "50")))

    opening = db.cash_balance_at(f"{start}T00:00:00+00:00", account)
    closing = db.cash_balance_at(f"{end}T23:59:59+00:00", account)
    totals = db.cash_flow_totals(start, end, account=account)

    res = compute(opening_cash=opening, closing_cash=closing, totals=totals,
                  tolerance=tolerance)
    recon_id = hashlib.sha256(
        f"{account}|{start}|{end}|{currency}|{CALC_VERSION}".encode()
    ).hexdigest()[:24]
    row = {
        "recon_id": recon_id, "account_id": account,
        "period_start": start, "period_end": end, "currency": currency,
        **{k: v for k, v in res.items() if k != "issues"},
        "issues": json.dumps(res["issues"], ensure_ascii=False),
        "calculation_version": CALC_VERSION,
        "created_at": now.isoformat(),
    }
    try:
        db.save_reconciliation(row)
    except Exception:  # noqa: BLE001
        log.warning("对账结果落库失败（原始流水未受影响）", exc_info=True)
    return {**res, "recon_id": recon_id, "period_start": start,
            "period_end": end, "currency": currency}
