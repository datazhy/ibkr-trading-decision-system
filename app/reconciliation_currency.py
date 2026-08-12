"""分币种现金对账（以 Statement of Funds 余额为锚点）。

**主口径是同一份 Flex 报表内的期初/期末余额**，不是账户快照。这一点很关键：
报表回溯 90 天，而分钟级账户快照只有最近十几天；强行要求快照覆盖整个报表区间，
就永远只能得到 insufficient_data。用报表自带的余额锚点，对账立刻可行。

检验等式（**逐币种独立成立**）：

    期末余额 ≈ 期初余额
             + 外部现金流（入金 - 出金）
             + 交易现金流        ← 净额，**已含佣金**
             + 分红 + 利息
             + 非交易费用 + 税费  ← Flex 中本身为负，直接相加
             + 其他已确认项目

注意等式里**没有单独的佣金项**。IBKR 的 `Trade.netCash` 与
`StatementOfFundsLine.amount` 都是净额（买入 = -(成交金额 + 佣金)），
再减一次佣金就是重复扣除。这一点在真实报表上验证过：修正前的差异
恰好等于佣金总额，分毫不差。佣金仍然单独解析并展示，只是不进等式。

四条纪律：

1. **绝不跨币种相加**。HKD 与 USD 直接相加得到的数没有任何意义。先各自对平，
   再用报表当时可得的汇率折算成基础币种做汇总展示。

2. **余额锚点不是经济流水**。它们不进 cash_flows，也不参与上面的加减 ——
   它们是等式的两端，不是等式里的项。

3. **容差不为了通过而放大**。对不上就是对不上，把容差调到差异之上，
   等于把问题藏起来。差异的构成与可能原因必须写出来。
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from decimal import Decimal

from . import cashflow_model as cm
from . import flex_balances as fb
from .logging_setup import get_logger

log = get_logger()

CALC_VERSION = "recon-ccy-v1/2026-07"

ST_RECONCILED = "reconciled"
ST_WITHIN_TOLERANCE = "within_tolerance"
ST_MISMATCH = "mismatch"
ST_INSUFFICIENT = "insufficient_data"
ST_PARTIAL_CCY = "partial_currency_coverage"

ZERO = Decimal("0")
EXACT_EPSILON = Decimal("0.01")


def _d(v):
    return cm.d(v)


def _s(v):
    return None if v is None else str(v)


def reconcile_currency(*, currency: str, opening, closing, totals: dict,
                       subtotals: dict, tolerance: Decimal) -> dict:
    """单个币种的对账。totals/subtotals 都是**该币种的原币金额**。"""
    o, c = _d(opening), _d(closing)

    def t(cat: str) -> Decimal:
        return _d(totals.get(cat)) or ZERO

    def st(sub: str) -> Decimal:
        return _d(subtotals.get(sub)) or ZERO

    # 出入金拆成两个正数展示，但求和仍用净额
    deposits = withdrawals = ZERO
    for sub in ("deposit", "deposit_withdrawal", "withdrawal",
                "internal_transfer", "account_transfer"):
        v = st(sub)
        if v > 0:
            deposits += v
        elif v < 0:
            withdrawals += -v

    external = t(cm.CAT_EXTERNAL) + t(cm.CAT_TRANSFER)
    trade = t(cm.CAT_TRADE)
    dividends = t(cm.CAT_INCOME)
    interest = t(cm.CAT_INTEREST)
    taxes = t(cm.CAT_TAX)
    expense_total = t(cm.CAT_EXPENSE)
    commissions = st("trade_commission")
    other_fees = expense_total - commissions
    other_items = t(cm.CAT_CORPORATE) + t(cm.CAT_UNKNOWN)

    issues: list[str] = []
    if o is None or c is None:
        issues.append(f"{currency} 缺少期初或期末余额锚点，无法对账")
        return _row(currency, o, c, deposits, withdrawals, external, trade,
                    dividends, interest, commissions, other_fees, taxes,
                    other_items, None, None, tolerance, ST_INSUFFICIENT, issues)

    # **交易佣金已经含在 trade_cash_flow 里**，不能再加一次。
    #
    # IBKR 的 Trade.netCash 与 StatementOfFundsLine.amount 都是**净额**：
    # 买入时 = -(成交金额 + 佣金)。我们仍然把佣金单独解析出来，是为了让它在
    # 归因和查询里可见；但放进现金等式就成了重复扣除。
    # 这不是推测 —— 真实报表上实测差异恰好等于佣金总额（分毫不差）。
    non_trade_expense = other_fees        # 行情费、监管费等，未含在成交净额中
    movement = (external + trade + dividends + interest + taxes
                + non_trade_expense + other_items)
    expected = o + movement
    diff = c - expected

    if t(cm.CAT_UNKNOWN):
        issues.append("存在未分类流水，差异可能由此产生")

    if commissions != ZERO:
        issues.append(
            f"佣金 {commissions} 已含在交易现金流中，等式内不重复扣除"
            "（单列仅供查看）")

    if abs(diff) <= EXACT_EPSILON:
        status = ST_RECONCILED
    elif abs(diff) <= tolerance:
        status = ST_WITHIN_TOLERANCE
        issues.append(f"差异 {diff} 在容差 {tolerance} 内，"
                      "常见原因是结算日差异或未结算资金")
    else:
        status = ST_MISMATCH
        issues.extend(_explain_difference(diff, trade, external, other_items))

    return _row(currency, o, c, deposits, withdrawals, external, trade,
                dividends, interest, commissions, other_fees, taxes,
                other_items, expected, diff, tolerance, status, issues)


def _explain_difference(diff: Decimal, trade: Decimal, external: Decimal,
                        other: Decimal) -> list[str]:
    """差异的可能来源。列出来供人判断，**不做自动修正**。"""
    out = [f"差异 {diff} 超出容差，需要人工核对"]
    if other != 0:
        out.append("报表中存在未分类或公司行动项目，可能未被完整识别")
    if trade != 0:
        out.append("交易现金流不为零：若报表按结算日入账而余额按成交日，"
                   "跨期成交会造成差异")
    if external != 0:
        out.append("期间有外部资金进出：电汇到账日与记账日可能不同")
    out.append("也可能是报表区间内存在本版本尚未解析的流水类型")
    return out


def _row(currency, o, c, dep, wd, external, trade, dividends, interest,
         commissions, other_fees, taxes, other_items, expected, diff,
         tolerance, status, issues) -> dict:
    return {
        "currency": currency,
        "opening_balance": _s(o), "closing_balance": _s(c),
        "deposits": _s(dep), "withdrawals": _s(wd),
        "net_external_cash_flow": _s(external),
        "trade_cash_flow": _s(trade),
        "dividends": _s(dividends), "interest": _s(interest),
        "commissions": _s(commissions), "other_fees": _s(other_fees),
        "taxes": _s(taxes), "other_confirmed_items": _s(other_items),
        "expected_closing_balance": _s(expected),
        "reconciliation_difference": _s(diff),
        "tolerance": _s(tolerance), "status": status, "issues": issues,
    }


def reconcile_report(cfg, db, *, account: str | None = None,
                     report_from: str | None = None,
                     report_to: str | None = None,
                     report_hash: str | None = None,
                     now: datetime | None = None) -> dict:
    """对一份报表做逐币种对账并落库。**不修改任何原始流水。**"""
    now = now or datetime.now(timezone.utc)
    account = account if account is not None else getattr(cfg, "ib_account", None)
    tolerance = Decimal(str(getattr(cfg, "flex_reconciliation_tolerance", "50")))

    if not (report_from and report_to):
        report_from, report_to = db.latest_balance_period(account)
    if not (report_from and report_to):
        return {"status": ST_INSUFFICIENT, "currencies": [],
                "issues": ["尚无带余额锚点的 Flex 报表，无法对账"],
                "report_from": None, "report_to": None}

    rows = db.statement_balances(account=account, report_from=report_from,
                                 report_to=report_to)
    markers = [fb.BalanceMarker(
        account_id=r["account_id"], balance_date=r["balance_date"],
        balance_type=r["balance_type"], currency=r["currency"],
        balance=_d(r["balance"]) or ZERO,
        fx_rate_to_base=_d(r.get("fx_rate_to_base")),
        balance_base=_d(r.get("balance_base")),
        level_of_detail=r.get("level_of_detail"),
        quality_status=r.get("quality_status", "ok")) for r in rows]
    anchors = fb.by_currency(markers)

    start_day, end_day = _norm(report_from), _norm(report_to)
    per_ccy = db.cash_flow_totals_by_currency(start_day, end_day, account=account)

    # 有流水但没有余额锚点的币种 -> 覆盖不完整
    flow_currencies = set(per_ccy)
    anchor_currencies = {c for c, v in anchors.items()
                         if v["opening"] and v["closing"]}
    missing = sorted(flow_currencies - anchor_currencies)

    results: list[dict] = []
    for currency in sorted(flow_currencies | anchor_currencies):
        slot = anchors.get(currency) or {"opening": None, "closing": None}
        buckets = per_ccy.get(currency) or {"by_category": {}, "by_subtype": {}}
        res = reconcile_currency(
            currency=currency,
            opening=slot["opening"].balance if slot.get("opening") else None,
            closing=slot["closing"].balance if slot.get("closing") else None,
            totals=buckets["by_category"], subtotals=buckets["by_subtype"],
            tolerance=tolerance)
        rate = None
        if slot.get("closing") is not None:
            rate = slot["closing"].fx_rate_to_base
        if rate is None:
            r = db.conversion_rate(end_day, currency, "USD")
            rate = _d(r)
        if rate is None and currency.upper() == "USD":
            rate = Decimal("1")
        diff = _d(res["reconciliation_difference"])
        res["fx_rate_to_base"] = _s(rate)
        # 折算只用于**汇总展示**，各币种的对账判定仍在原币口径完成
        res["difference_base"] = _s(diff * rate) if (diff is not None
                                                     and rate is not None) else None
        results.append(res)
        _save(db, res, account=account, report_from=report_from,
              report_to=report_to, report_hash=report_hash, now=now)

    overall = _overall_status(results, missing)
    total_diff_base = ZERO
    unknown_base = False
    for r in results:
        v = _d(r.get("difference_base"))
        if v is None:
            unknown_base = True
        else:
            total_diff_base += v

    return {
        "report_from": report_from, "report_to": report_to,
        "account_id": account, "currencies": results,
        "currency_count": len(results),
        "missing_anchor_currencies": missing,
        "status": overall,
        "total_difference_base": (None if unknown_base else str(total_diff_base)),
        "base_currency": "USD",
        "tolerance": str(tolerance),
        "calculation_version": CALC_VERSION,
        "note": ("每个币种独立对账后再折算为基础币种汇总；"
                 "不同币种的金额从不直接相加。"),
        "issues": ([f"以下币种有流水但缺少余额锚点：{'、'.join(missing)}"]
                   if missing else []),
    }


def _overall_status(results: list[dict], missing: list[str]) -> str:
    if missing:
        return ST_PARTIAL_CCY
    if not results:
        return ST_INSUFFICIENT
    statuses = {r["status"] for r in results}
    if ST_INSUFFICIENT in statuses:
        return ST_INSUFFICIENT
    if ST_MISMATCH in statuses:
        return ST_MISMATCH
    if ST_WITHIN_TOLERANCE in statuses:
        return ST_WITHIN_TOLERANCE
    return ST_RECONCILED


def _save(db, res: dict, *, account, report_from, report_to, report_hash,
          now) -> None:
    recon_id = hashlib.sha256(
        f"{account}|{report_from}|{report_to}|{res['currency']}|{CALC_VERSION}"
        .encode()).hexdigest()[:24]
    try:
        db.save_currency_reconciliation({
            "recon_id": recon_id, "account_id": account,
            "report_from": report_from, "report_to": report_to,
            **{k: v for k, v in res.items() if k != "issues"},
            "issues": json.dumps(res["issues"], ensure_ascii=False),
            "calculation_version": CALC_VERSION, "report_hash": report_hash,
            "created_at": now.isoformat()})
    except Exception:  # noqa: BLE001 — 落库失败不影响返回结果，也不动原始流水
        log.warning("分币种对账落库失败（原始流水未受影响）", exc_info=True)


def _norm(v) -> str:
    s = str(v or "")[:10]
    return f"{s[:4]}-{s[4:6]}-{s[6:8]}" if len(s) == 8 and "-" not in s else s


# ---------------- 账户快照交叉验证 ----------------

def cross_validate(db, *, report_to: str, flex_closing_base,
                   account: str | None = None) -> dict:
    """用账户快照交叉验证 Flex 期末现金。

    **两者口径本就不同**：Flex 的 closing balance 是结算现金，IBKR 的
    TotalCashValue 含未结算资金。所以不要求相等，只报差异与时间差，
    由人判断是否合理。要求相等会逼出一个假的"对平"。
    """
    out = {
        "flex_closing_cash": (None if flex_closing_base is None
                              else str(flex_closing_base)),
        "snapshot_total_cash": None, "snapshot_ts": None,
        "comparison_difference": None, "timestamp_gap_hours": None,
        "comparison_status": "insufficient_data",
        "explanation": "",
    }
    end_iso = f"{_norm(report_to)}T23:59:59+00:00"
    try:
        cash = db.cash_balance_at(end_iso, account)
        rows = db.equity_series_between("0000", end_iso, account=account)
        snap_ts = rows[-1]["ts"] if rows else None
    except Exception:  # noqa: BLE001
        cash, snap_ts = None, None

    if cash is None:
        out["explanation"] = (
            f"报表结束日 {_norm(report_to)} 当日或之前没有账户快照，"
            "无法交叉验证。这不影响以 Statement of Funds 余额为准的对账结果。")
        return out

    out["snapshot_total_cash"] = str(cash)
    out["snapshot_ts"] = snap_ts
    if flex_closing_base is None:
        out["explanation"] = "缺少 Flex 期末余额，无法比较"
        return out

    diff = _d(cash) - _d(flex_closing_base)
    out["comparison_difference"] = str(diff)
    if snap_ts:
        try:
            gap = (datetime.fromisoformat(snap_ts)
                   - datetime.fromisoformat(end_iso)).total_seconds() / 3600
            out["timestamp_gap_hours"] = round(gap, 2)
        except (TypeError, ValueError):
            pass

    rel = abs(diff) / abs(_d(flex_closing_base)) if _d(flex_closing_base) else None
    if rel is not None and rel <= Decimal("0.01"):
        out["comparison_status"] = "consistent"
        out["explanation"] = (
            f"快照现金与 Flex 结算现金相差 {diff}（{rel:.2%}），在合理范围内。"
            "两者口径不同（TotalCashValue 含未结算资金），不要求完全相等。")
    else:
        out["comparison_status"] = "divergent"
        out["explanation"] = (
            f"快照现金与 Flex 结算现金相差 {diff}。常见原因："
            "TotalCashValue 含未结算资金与在途成交；快照时点与报表结束日不同；"
            "多币种折算口径差异。**这不代表对账失败** —— "
            "对账以 Statement of Funds 余额为准。")
    return out
