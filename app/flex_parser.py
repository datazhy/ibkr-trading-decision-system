"""Flex Query 报表解析（纯函数，无网络）。

把 IBKR 的 XML 拆成标准化的现金流、已实现盈亏与账户信息。

**两个在真实生产报表上验证出来的要害：**

1. **绝不按固定路径找节点。** 真实报表的 Statement of Funds 容器叫 `StmtFunds`
   而不是 `StatementOfFunds`，52 条明细直接挂在它下面。所有查找一律走
   flex_xml 的局部名递归，容器叫什么、有没有、带不带命名空间都无所谓。

2. **同一笔经济事件会同时出现在多个 section。** 真实报表里 43 笔成交在
   `Trade` 和 `StatementOfFundsLine` 各出现一次，2 笔出入金在 `CashTransaction`
   和 `StatementOfFundsLine` 各出现一次。更麻烦的是 `Trade.tradeID` 与
   `Trade.transactionID` **并不相同**，用 tradeID 做去重键会让同一笔交易
   进库两次 —— 实测会把 43 笔交易现金流变成 86 笔、把 2 笔入金变成 4 笔。

   解法：**全部 section 统一用 transactionID 作为规范键**（SOF 与 Trade 的
   transactionID 实测 43/43 互相命中），并给来源定优先级，让同一事件
   确定性地只保留一条，而不是靠后写覆盖先写。

section_counts 报的是**叶子记录原始数量**（用于验收对账），与去重后的
cash_flows 条数是两回事，不要混。
"""
from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from decimal import Decimal
from typing import Iterable

from . import cashflow_model as cm
from . import flex_xml as fx
from .flex import FlexError, parse_flex_datetime
from .logging_setup import get_logger

log = get_logger()

#: StatementOfFunds 的 activityCode -> 归一化 type。
#: 这是唯一能把「买入导致现金下降」与「银行出金」区分开的字段。
_ACTIVITY_CODE_MAP = {
    "BUY": "buy", "SELL": "sell",
    "DEP": "deposits", "WITH": "withdrawals",
    "DIV": "dividends", "PIL": "paymentinlieuofdividends",
    "FRTAX": "withholdingtax", "WHTAX": "withholdingtax",
    "CINT": "creditinterest", "DINT": "debitinterest",
    "MINT": "margininterest", "BINT": "bondinterest",
    "COMM": "commission", "FEES": "otherfees", "OFEE": "otherfees",
    "ADJ": "otherfees", "FX": "forex", "XFER": "internaltransfer",
    "CORP": "corporateaction",
}

#: 同一规范键出现多次时的来源优先级（数字大者胜）。
#: StatementOfFunds 是券商的现金分类账，字段最完整、口径最统一，故优先级最高。
_SOURCE_PRIORITY = {
    "FLEX_STATEMENT_OF_FUNDS": 30,
    "FLEX_CASH_TRANSACTION": 20,
    "FLEX_TRADE": 10,
}


def _txt(el, *names) -> str | None:
    for n in names:
        v = el.attrib.get(n)
        if v not in (None, ""):
            return v
    return None


def canonical_txn_id(attrib: dict) -> str | None:
    """规范交易号。**优先 transactionID**。

    Trade 节点同时有 tradeID 与 transactionID 且两者不同；SOF 节点两者都有
    并与 Trade 一致。只有统一取 transactionID，跨 section 的同一事件才会
    落到同一个去重键上。
    """
    for key in ("transactionID", "origTransactionID", "tradeID", "actionID"):
        v = attrib.get(key)
        if v not in (None, ""):
            return str(v).strip()
    return None


def report_period(xml: str) -> tuple[str | None, str | None]:
    root = fx.parse_xml(xml)
    starts, ends = [], []
    for st in fx.iter_by_local_name(root, "FlexStatement"):
        start = _txt(st, "fromDate", "periodFrom")
        end = _txt(st, "toDate", "periodTo")
        if start:
            starts.append(start)
        if end:
            ends.append(end)
    # 开启 Flex“按日细分”后，一份响应会包含多个 FlexStatement，每段一天。
    # 只取第一段会把 90 日官方报表误登记成单日覆盖。
    return (min(starts) if starts else None, max(ends) if ends else None)


def accounts_in_report(xml: str) -> list[str]:
    root = fx.parse_xml(xml)
    out = set()
    for name in ("FlexStatement", "AccountInformation"):
        for el in fx.iter_by_local_name(root, name):
            a = _txt(el, "accountId")
            if a:
                out.add(a)
    return sorted(out)


def base_currency(xml: str) -> str | None:
    root = fx.parse_xml(xml)
    for acc in fx.iter_by_local_name(root, "AccountInformation"):
        c = _txt(acc, "currency", "baseCurrency")
        if c:
            return c
    return None


def _et_date(iso: str | None, tz) -> str | None:
    if not iso:
        return None
    try:
        return datetime.fromisoformat(iso).astimezone(tz).date().isoformat()
    except (TypeError, ValueError):
        return str(iso)[:10]


def _build(el, *, raw_type: str | None, tz, source: str,
           amount_attr=("amount", "netAmount", "proceeds")) -> cm.CashFlowRow | None:
    a = el.attrib
    amount = None
    for name in amount_attr:
        amount = cm.d(a.get(name))
        if amount is not None:
            break
    # 金额为 0 的行通常是余额标记（真实报表里有 4 条这样的 SOF 行），不是现金流
    if amount is None or amount == 0:
        return None

    occurred = parse_flex_datetime(
        a.get("dateTime") or a.get("date") or a.get("reportDate")
        or a.get("settleDate"), tz)
    if not occurred:
        return None
    settled = parse_flex_datetime(a.get("settleDate"), tz)

    desc = a.get("description") or a.get("activityDescription")
    symbol = a.get("symbol") or None
    cat, sub, quality = cm.classify(raw_type, desc, has_symbol=bool(symbol))

    fx_rate = cm.d(a.get("fxRateToBase"))
    amount_base = None
    if fx_rate is not None and fx_rate != 0:
        amount_base = amount * fx_rate
    elif (a.get("currency") or "").upper() == "USD":
        amount_base, fx_rate = amount, Decimal("1")

    return cm.CashFlowRow(
        account_id=a.get("accountId"),
        occurred_at=occurred,
        et_date=_et_date(occurred, tz) or str(occurred)[:10],
        settled_at=settled,
        category=cat, subtype=sub,
        direction=cm.direction_of(amount),
        amount=amount, currency=(a.get("currency") or "USD").upper(),
        amount_base=amount_base, fx_rate=fx_rate,
        symbol=symbol, conid=a.get("conid"),
        description=desc,
        broker_transaction_id=canonical_txn_id(a),
        source=source, quality_status=quality,
        raw=dict(a))


def parse_cash_transactions(xml: str, tz) -> list[cm.CashFlowRow]:
    """CashTransaction 明细。容器 `CashTransactions` 可有可无。"""
    root = fx.parse_xml(xml)
    out = []
    for c in fx.iter_by_local_name(root, "CashTransaction"):
        row = _build(c, raw_type=c.attrib.get("type"), tz=tz,
                     source="FLEX_CASH_TRANSACTION")
        if row:
            out.append(row)
    return out


def parse_statement_of_funds(xml: str, tz) -> list[cm.CashFlowRow]:
    """StatementOfFundsLine 明细。

    **不要求外层存在容器** —— 真实报表的容器叫 `StmtFunds`，
    未来也可能出现 `StatementOfFunds`，或者干脆没有。递归按叶子名找即可。
    """
    root = fx.parse_xml(xml)
    out = []
    for c in fx.iter_by_local_name(root, "StatementOfFundsLine"):
        code = (c.attrib.get("activityCode") or "").strip().upper()
        raw_type = _ACTIVITY_CODE_MAP.get(code) or c.attrib.get("activityDescription")
        row = _build(c, raw_type=raw_type, tz=tz,
                     source="FLEX_STATEMENT_OF_FUNDS")
        if row:
            out.append(row)
    return out


def parse_trade_cash_flows(xml: str, tz) -> list[cm.CashFlowRow]:
    """Trade -> 交易现金流 + 佣金。

    **明确归 trade_cash_flow**，绝不进入外部现金流口径：买入导致现金下降
    是持仓结构的变化，不是资金搬出账户。
    """
    root = fx.parse_xml(xml)
    out = []
    for t in fx.iter_by_local_name(root, "Trade"):
        a = t.attrib
        occurred = parse_flex_datetime(a.get("dateTime") or a.get("tradeDate"), tz)
        if not occurred:
            continue
        et = _et_date(occurred, tz) or str(occurred)[:10]
        cur = (a.get("currency") or "USD").upper()
        rate = cm.d(a.get("fxRateToBase")) or (Decimal("1") if cur == "USD" else None)
        conid, symbol = a.get("conid"), a.get("symbol")
        tid = canonical_txn_id(a)

        net = cm.d(a.get("netCash"))
        if net is not None and net != 0:
            buy_sell = (a.get("buySell") or "").upper()
            sub = "trade_buy" if buy_sell.startswith("B") else "trade_sell"
            out.append(cm.CashFlowRow(
                account_id=a.get("accountId"), occurred_at=occurred, et_date=et,
                settled_at=parse_flex_datetime(a.get("settleDateTarget"), tz),
                category=cm.CAT_TRADE, subtype=sub,
                direction=cm.direction_of(net), amount=net, currency=cur,
                amount_base=(net * rate) if rate is not None else None,
                fx_rate=rate, symbol=symbol, conid=conid,
                description=a.get("description") or f"{buy_sell} {symbol}",
                broker_transaction_id=tid, source="FLEX_TRADE",
                quality_status=cm.QUALITY_OK, raw=dict(a)))

        comm = cm.d(a.get("ibCommission"))
        if comm is not None and comm != 0:
            out.append(cm.CashFlowRow(
                account_id=a.get("accountId"), occurred_at=occurred, et_date=et,
                category=cm.CAT_EXPENSE, subtype="trade_commission",
                direction=cm.direction_of(comm), amount=comm,
                currency=(a.get("ibCommissionCurrency") or cur).upper(),
                amount_base=(comm * rate) if rate is not None else None,
                fx_rate=rate, symbol=symbol, conid=conid,
                description=f"佣金 {symbol}",
                # 佣金与成交是同一 transactionID 下的两笔现金变动，
                # 加后缀避免它们互相覆盖
                broker_transaction_id=(f"{tid}:COMM" if tid else None),
                source="FLEX_TRADE", quality_status=cm.QUALITY_OK, raw=dict(a)))
    return out


def parse_conversion_rates(xml: str) -> list[dict]:
    """ConversionRate 明细。真实报表约 3000 条。

    当前账户以 USD 为主，汇率暂时用不上 —— 但**绝不能因此假设永远只有 USD**。
    先把它可靠地存下来，多币种对账与归因才有据可依。
    """
    root = fx.parse_xml(xml)
    out, seen = [], set()
    for el in fx.iter_by_local_name(root, "ConversionRate"):
        a = el.attrib
        day = a.get("reportDate") or a.get("date")
        frm = (a.get("fromCurrency") or "").upper()
        to = (a.get("toCurrency") or "").upper()
        rate = cm.d(a.get("rate"))
        if not (day and frm and to) or rate is None:
            continue
        day = _norm_day(day)
        key = f"{day}|{frm}|{to}"
        if key in seen:
            continue
        seen.add(key)
        out.append({
            "rate_id": key, "rate_date": day, "from_currency": frm,
            "to_currency": to, "rate": str(rate),
        })
    return out


def _norm_day(v) -> str:
    s = str(v or "")[:10]
    return f"{s[:4]}-{s[4:6]}-{s[6:8]}" if len(s) == 8 and "-" not in s else s


def parse_realized_pnl(xml: str, tz) -> list[dict]:
    """已实现盈亏。**用 IBKR 权威字段**，不用当前持仓成本反推历史。"""
    root = fx.parse_xml(xml)
    out, seen = [], set()

    def add(a: dict, pnl: Decimal, source: str):
        tid = canonical_txn_id(a)
        key = f"{tid}|{a.get('conid')}|{pnl}"
        if key in seen:
            return
        seen.add(key)
        out.append({
            "record_id": key[:120],
            "account_id": a.get("accountId"),
            "symbol": a.get("symbol"), "conid": a.get("conid"),
            "open_date": parse_flex_datetime(a.get("openDateTime"), tz),
            "close_date": parse_flex_datetime(
                a.get("dateTime") or a.get("tradeDate"), tz),
            "quantity": str(cm.d(a.get("quantity")) or 0),
            "proceeds": _s(cm.d(a.get("proceeds"))),
            "cost_basis": _s(cm.d(a.get("cost") or a.get("costBasis"))),
            "realized_pnl": str(pnl),
            "commission": _s(cm.d(a.get("ibCommission"))),
            "currency": (a.get("currency") or "USD").upper(),
            "fx_rate": _s(cm.d(a.get("fxRateToBase"))),
            "transaction_id": tid,
            "source": source,
            "raw_json": cm.to_raw_json(a),
        })

    for name, source in (("Trade", "FLEX_TRADE"),
                         ("ClosedLot", "FLEX_CLOSED_LOT")):
        for t in fx.iter_by_local_name(root, name):
            a = t.attrib
            pnl = cm.d(a.get("fifoPnlRealized") or a.get("realizedPnl"))
            if pnl is not None and pnl != 0:
                add(a, pnl, source)
    return out


_NAV_CAPITAL_MOVEMENT_FIELDS = (
    "depositsWithdrawals", "internalCashTransfers", "paxosTransfers",
    "excessFundSweep", "assetTransfers", "debitCardActivity", "billPay",
    "grantActivity", "donations", "carbonCredits", "linkingAdjustments",
)


def parse_change_in_nav(xml: str) -> list[dict]:
    """解析 IBKR Flex `ChangeInNAV` 按日官方总收益。

    total_pnl 使用官方恒等式：期末 NAV − 期初 NAV − 资本搬入搬出。
    MTM 本身不含佣金等独立项目，不能直接把 `mtm` 当总收益；例如生产
    2026-07-30 的 MTM=8648.562、佣金=-2.441458，官方总收益=8646.120542。
    """
    root = fx.parse_xml(xml)
    out = []
    for el in fx.iter_by_local_name(root, "ChangeInNAV"):
        a = dict(el.attrib)
        start, end = cm.d(a.get("startingValue")), cm.d(a.get("endingValue"))
        day = _norm_day(a.get("toDate") or a.get("fromDate"))
        account = a.get("accountId")
        currency = (a.get("currency") or "USD").upper()
        if start is None or end is None or not day or not account:
            continue

        capital = sum((cm.d(a.get(k)) or Decimal("0")
                       for k in _NAV_CAPITAL_MOVEMENT_FIELDS), Decimal("0"))
        fees = sum((cm.d(a.get(k)) or Decimal("0") for k in (
            "brokerFees", "changeInBrokerFeeAccruals", "advisorFees",
            "clientFees", "otherFees", "commissions", "forexCommissions",
            "transactionTax", "salesTax", "billableSalesTax")), Decimal("0"))
        other = sum((cm.d(a.get(k)) or Decimal("0") for k in (
            "other", "otherIncome", "corporateActionProceeds",
            "costAdjustments", "transferredPnlAdjustments")), Decimal("0"))
        out.append({
            "account_id": account, "et_date": day, "currency": currency,
            "starting_value": str(start), "ending_value": str(end),
            "total_pnl": str(end - start - capital),
            "mtm": _s(cm.d(a.get("mtm"))),
            "commissions": _s(cm.d(a.get("commissions"))),
            "capital_movements": str(capital),
            "deposits_withdrawals": _s(cm.d(a.get("depositsWithdrawals"))),
            "internal_cash_transfers": _s(cm.d(a.get("internalCashTransfers"))),
            "asset_transfers": _s(cm.d(a.get("assetTransfers"))),
            "dividends": _s(cm.d(a.get("dividends"))),
            "interest": _s(cm.d(a.get("interest"))),
            "fees": str(fees), "other": str(other),
            "twr": _s(cm.d(a.get("twr"))),
            "raw_json": cm.to_raw_json(a), "source": "FLEX_CHANGE_IN_NAV",
        })
    return out


_TRANSFER_SAFE_RAW_FIELDS = (
    "reportDate", "date", "dateTime", "settleDate", "type", "direction",
    "transferCompany", "deliveringBroker", "currency", "assetCategory",
    "symbol", "description", "conid", "quantity", "transferPrice",
    "costBasis", "openDateTime", "positionAmount", "positionAmountInBase",
    "pnlAmount", "pnlAmountInBase", "cashTransfer", "code", "clientReference",
    "transactionID", "levelOfDetail", "positionInstructionID",
    "positionInstructionSetID",
)


def parse_transfers(xml: str) -> list[dict]:
    """解析 IBKR Flex `Transfer` 证券转仓明细。

    转仓是资产搬入搬出，不是现金入金，也不是投资收益。`raw_json` 特意不保存
    Transfer Account / Transfer Account Name，避免把对手方账户标识复制进数据库。
    """
    root = fx.parse_xml(xml)
    out = []
    for el in fx.iter_by_local_name(root, "Transfer"):
        a = dict(el.attrib)
        account = a.get("accountId")
        day = _norm_day(a.get("reportDate") or a.get("date") or a.get("dateTime"))
        if not account or not day:
            continue
        tid = (a.get("transactionID") or "").strip()
        identity = "|".join((account, tid, day, a.get("conid") or "",
                              a.get("symbol") or "", a.get("direction") or "",
                              a.get("quantity") or "", a.get("positionAmountInBase") or ""))
        transfer_id = "FLEX_TRANSFER:" + hashlib.sha256(
            identity.encode("utf-8", "replace")).hexdigest()[:32]
        safe_raw = {k: a[k] for k in _TRANSFER_SAFE_RAW_FIELDS if a.get(k) not in (None, "")}
        out.append({
            "transfer_id": transfer_id, "account_id": account,
            "report_date": day, "transfer_date": _norm_day(a.get("date") or a.get("dateTime")),
            "settle_date": _norm_day(a.get("settleDate")),
            "transfer_type": a.get("type"), "direction": a.get("direction"),
            "transfer_company": a.get("transferCompany"),
            "delivering_broker": a.get("deliveringBroker"),
            "currency": (a.get("currency") or "USD").upper(),
            "asset_category": a.get("assetCategory"), "symbol": a.get("symbol"),
            "description": a.get("description"), "conid": a.get("conid"),
            "quantity": _s(cm.d(a.get("quantity"))),
            "transfer_price": _s(cm.d(a.get("transferPrice"))),
            "cost_basis": _s(cm.d(a.get("costBasis"))),
            "position_amount": _s(cm.d(a.get("positionAmount"))),
            "position_amount_base": _s(cm.d(a.get("positionAmountInBase"))),
            "pnl_amount": _s(cm.d(a.get("pnlAmount"))),
            "pnl_amount_base": _s(cm.d(a.get("pnlAmountInBase"))),
            "cash_transfer": _s(cm.d(a.get("cashTransfer"))),
            "transaction_id": tid or None, "code": a.get("code"),
            "level_of_detail": a.get("levelOfDetail"),
            "raw_json": cm.to_raw_json(safe_raw), "source": "FLEX_TRANSFER",
        })
    return out


def _s(v) -> str | None:
    return None if v is None else str(v)


def deduplicate(rows: Iterable[cm.CashFlowRow]) -> tuple[list[cm.CashFlowRow], dict]:
    """跨 section 去重。

    同一规范键只保留优先级最高的来源。**这一步不是可选的优化** ——
    真实报表里 43 笔成交在两个 section 各出现一次，不去重就会把交易现金流
    翻倍，进而让对账和收益归因全错。
    """
    best: dict[str, cm.CashFlowRow] = {}
    order: list[str] = []
    dropped: dict[str, int] = {}
    for r in rows:
        key = cm.dedupe_key(
            broker_transaction_id=r.broker_transaction_id,
            account_id=r.account_id, occurred_at=r.occurred_at,
            category=r.category, subtype=r.subtype, amount=r.amount,
            currency=r.currency, symbol=r.symbol)
        prev = best.get(key)
        if prev is None:
            best[key] = r
            order.append(key)
            continue
        if _SOURCE_PRIORITY.get(r.source, 0) > _SOURCE_PRIORITY.get(prev.source, 0):
            dropped[prev.source] = dropped.get(prev.source, 0) + 1
            best[key] = r
        else:
            dropped[r.source] = dropped.get(r.source, 0) + 1
    return [best[k] for k in order], dropped


def parse_all(xml: str, tz, *, validate: bool = True) -> dict:
    """解析全部 section。

    validate=True 时先做结构校验，不合法直接抛 FlexStructureError ——
    **不返回空集合让上游误以为「报表是空的」**。
    """
    structure = None
    if validate:
        structure = fx.validate_final_report(xml)      # 不合法即抛

    result: dict = {"cash_flows": [], "realized_pnl": [], "nav_daily": [],
                    "transfers": [], "errors": [],
                    "sections": {}, "structure": structure}

    raw_rows: list[cm.CashFlowRow] = []
    for name, fn in (("cash_transactions", parse_cash_transactions),
                     ("statement_of_funds", parse_statement_of_funds),
                     ("trades", parse_trade_cash_flows)):
        try:
            raw_rows.extend(fn(xml, tz))
        except Exception as exc:  # noqa: BLE001 — 单 section 失败不影响其余
            result["errors"].append(f"{name}: {cm.sanitize_error(str(exc))}")

    deduped, dropped = deduplicate(raw_rows)
    result["cash_flows"] = deduped
    result["raw_row_count"] = len(raw_rows)
    result["deduplicated_away"] = len(raw_rows) - len(deduped)
    result["dedupe_by_source"] = dropped

    try:
        result["realized_pnl"] = parse_realized_pnl(xml, tz)
    except Exception as exc:  # noqa: BLE001
        result["errors"].append(f"realized_pnl: {cm.sanitize_error(str(exc))}")

    try:
        result["nav_daily"] = parse_change_in_nav(xml)
    except Exception as exc:  # noqa: BLE001
        result["errors"].append(f"change_in_nav: {cm.sanitize_error(str(exc))}")

    try:
        result["transfers"] = parse_transfers(xml)
    except Exception as exc:  # noqa: BLE001
        result["errors"].append(f"transfers: {cm.sanitize_error(str(exc))}")

    # 余额锚点：**不进 cash_flows**，单独走 statement_cash_balances
    try:
        from . import flex_balances as fb
        pf_, pt_ = report_period(xml)
        result["balance_markers"] = fb.parse_balance_markers(
            xml, tz, report_from=pf_, report_to=pt_)
        result["balance_summary"] = fb.summarize(result["balance_markers"])
    except Exception as exc:  # noqa: BLE001
        result["balance_markers"] = []
        result["errors"].append(f"balance_markers: {cm.sanitize_error(str(exc))}")

    try:
        result["conversion_rates"] = parse_conversion_rates(xml)
    except Exception as exc:  # noqa: BLE001
        result["conversion_rates"] = []
        result["errors"].append(f"conversion_rates: {cm.sanitize_error(str(exc))}")

    # section_counts 报**叶子记录原始数量**，与去重后的条数是两回事。
    # 容器不存在不影响它 —— 这正是真实报表 StmtFunds 的情况。
    try:
        root = fx.parse_xml(xml)
        result["sections"] = fx.section_counts(root)
        result["sections"]["realized_pnl"] = len(result["realized_pnl"])
        result["sections"]["nav_daily"] = len(result["nav_daily"])
        result["sections"]["transfer_rows"] = len(result["transfers"])
        result["period_from"], result["period_to"] = report_period(xml)
        result["accounts"] = accounts_in_report(xml)
        result["base_currency"] = base_currency(xml)
        result["root_tag"] = fx.local_name(root.tag)
        result["flex_statements"] = fx.count_by_local_name(root, "FlexStatement")
    except Exception as exc:  # noqa: BLE001
        result["errors"].append(f"metadata: {cm.sanitize_error(str(exc))}")
    return result


def summarize(rows: Iterable[cm.CashFlowRow]) -> dict[str, int]:
    out: dict[str, int] = {}
    for r in rows:
        out[r.category] = out.get(r.category, 0) + 1
    return out
