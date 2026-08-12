"""收益与现金流页面数据。

一条贯穿全页的展示纪律：**入金不是盈利，出金不是亏损**。
页面把「账户净值变化」与「现金流调整后收益」并排放，让这个区别一眼可见。

收益率无法可靠计算时显示「当前数据不足以可靠计算」，绝不显示 0% ——
一个 0% 会被读成「不赚不亏」，而真相是「不知道」。
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation

from . import cashflow_model as cm
from . import flex_sync
from . import performance as perf
from . import reconciliation_cash as rc
from . import reconciliation_currency as rcc
from . import unrealized_pnl as upnl
from .logging_setup import get_logger

log = get_logger()

CAT_LABEL = {
    cm.CAT_EXTERNAL: "外部资金", cm.CAT_TRADE: "交易现金流",
    cm.CAT_INCOME: "收入", cm.CAT_EXPENSE: "费用", cm.CAT_TAX: "税费",
    cm.CAT_INTEREST: "利息", cm.CAT_TRANSFER: "账户划转",
    cm.CAT_CORPORATE: "公司行动", cm.CAT_UNKNOWN: "未分类",
}
SUB_LABEL = {
    "deposit": "入金", "withdrawal": "出金", "deposit_withdrawal": "出入金",
    "internal_transfer": "内部划转", "account_transfer": "账户转移",
    "dividend": "股息", "payment_in_lieu": "代付股息",
    "dividend_accrual": "股息应计", "dividend_reversal": "股息冲回",
    "withholding_tax": "代扣税", "backup_withholding": "备用预扣",
    "credit_interest": "存款利息", "debit_interest": "欠款利息",
    "margin_interest": "融资利息", "cash_interest": "现金利息",
    "bond_interest": "债券利息", "trade_commission": "交易佣金",
    "regulatory_fee": "监管费", "market_data_fee": "行情费",
    "subscription_fee": "订阅费", "other_fee": "其它费用",
    "trade_buy": "买入", "trade_sell": "卖出", "option_premium": "期权权利金",
    "bond_principal": "债券本金", "fx_trade": "外汇交易",
}
STATUS_LABEL = {
    "not_configured": "未配置", "pending": "待运行", "requesting": "请求中",
    "waiting_report": "等待报表", "downloading": "下载中", "parsing": "解析中",
    "success": "成功", "partial": "部分成功", "failed": "失败",
    "no_data": "尚无数据",
}
RECON_LABEL = {
    rc.ST_RECONCILED: "已对平", rc.ST_WITHIN_TOLERANCE: "容差内",
    rc.ST_MISMATCH: "有差异", rc.ST_INSUFFICIENT: "数据不足",
    rcc.ST_PARTIAL_CCY: "部分币种缺锚点",
}
BAL_LABEL = {
    "opening_balance": "期初余额", "closing_balance": "期末余额",
    "currency_opening_balance": "币种期初", "currency_closing_balance": "币种期末",
    "unknown_balance_marker": "未识别余额标记",
}


def _d(v):
    return cm.d(v)


def money(v, currency: str = "") -> str:
    d = _d(v)
    if d is None:
        return "—"
    sign = "-" if d < 0 else ""
    return f"{sign}{abs(d):,.2f}{(' ' + currency) if currency else ''}"


def pct(v, digits: int = 2) -> str:
    """收益率展示。**None 显示为「数据不足」，绝不显示 0%。**"""
    d = _d(v)
    if d is None:
        return "数据不足"
    return f"{d:.{digits}%}"


def _jload(v, default):
    try:
        return json.loads(v) if v else default
    except (TypeError, ValueError):
        return default


def decorate_flow(r: dict, tz) -> dict:
    from .webformat import abs_time
    cat = r.get("category") or cm.CAT_EXTERNAL
    sub = r.get("subtype") or ""
    external = cm.is_external(cat)
    return {
        **r,
        "cat_label": CAT_LABEL.get(cat, cat),
        "sub_label": SUB_LABEL.get(sub, sub or "—"),
        "is_external": external,
        "row_css": "flow-external" if external else (
            "flow-trade" if cat == cm.CAT_TRADE else "flow-other"),
        "amount_display": money(r.get("amount"), r.get("currency") or ""),
        "base_display": money(r.get("amount_base")),
        "when": abs_time(r.get("occurred_at") or r.get("effective_at"), tz),
        "quality_label": {"ok": "", "estimated": "推断",
                          "unclassified": "未分类",
                          "conflict": "冲突"}.get(r.get("quality_status"), ""),
    }


def page_data(cfg, db, now: datetime | None = None, *,
              range_key: str = "30d", category: str | None = None,
              page: int = 1, per_page: int = 50,
              anchor_page: int = 1, anchor_per_page: int = 20) -> dict:
    now = now or datetime.now(timezone.utc)
    tz = cfg.local_tz
    today = now.date()
    if range_key == "ytd":
        start, end = f"{today.year}-01-01", today.isoformat()
    else:
        days = {"7d": 7, "30d": 30, "90d": 90}.get(range_key, 30)
        start = (today - timedelta(days=days)).isoformat()
        end = today.isoformat()

    st = flex_sync.status(cfg, db)
    configured = st["configured"]

    summary = None
    try:
        summary = perf.compute_period(cfg, db, start=start, end=end,
                                      account=getattr(cfg, "ib_account", None),
                                      granularity="custom", now=now)
    except Exception:  # noqa: BLE001
        log.warning("收益概览计算失败", exc_info=True)

    offset = max(0, (page - 1) * per_page)
    rows, total = db.query_cash_flows(
        start=start, end=end, category=[category] if category else None,
        limit=per_page, offset=offset)

    recon = db.latest_reconciliation(getattr(cfg, "ib_account", None))
    if recon:
        recon = {**recon, "status_label": RECON_LABEL.get(
            recon.get("quality_status"), recon.get("quality_status")),
            "issues_list": _jload(recon.get("issues"), [])}

    # 分币种对账（以报表余额锚点为准，不依赖账户快照覆盖整个区间）
    ccy_recon = None
    try:
        ccy_recon = rcc.reconcile_report(cfg, db,
                                         account=getattr(cfg, "ib_account", None))
        ccy_recon = {
            **ccy_recon,
            "status_label": RECON_LABEL.get(ccy_recon.get("status"),
                                            ccy_recon.get("status")),
            "currencies": [{
                **c, "status_label": RECON_LABEL.get(c["status"], c["status"]),
                "opening_display": money(c.get("opening_balance"), c["currency"]),
                "closing_display": money(c.get("closing_balance"), c["currency"]),
                "expected_display": money(c.get("expected_closing_balance"),
                                          c["currency"]),
                "diff_display": money(c.get("reconciliation_difference"),
                                      c["currency"]),
                "deposits_display": money(c.get("deposits"), c["currency"]),
                "withdrawals_display": money(c.get("withdrawals"), c["currency"]),
                "trade_display": money(c.get("trade_cash_flow"), c["currency"]),
                "dividends_display": money(c.get("dividends"), c["currency"]),
                "interest_display": money(c.get("interest"), c["currency"]),
                "commissions_display": money(c.get("commissions"), c["currency"]),
                "fees_display": money(c.get("other_fees"), c["currency"]),
                "taxes_display": money(c.get("taxes"), c["currency"]),
            } for c in ccy_recon.get("currencies", [])],
        }
        base_closing = next((c.get("closing_balance")
                             for c in ccy_recon["currencies"]
                             if c["currency"] == ccy_recon.get("base_currency")),
                            None)
        if ccy_recon.get("report_to"):
            ccy_recon["cross"] = rcc.cross_validate(
                db, report_to=ccy_recon["report_to"],
                account=getattr(cfg, "ib_account", None),
                flex_closing_base=base_closing)
    except Exception:  # noqa: BLE001
        log.warning("分币种对账读取失败", exc_info=True)

    anchors = []
    anchor_history = []
    anchor_total = 0
    try:
        account = getattr(cfg, "ib_account", None)
        latest_from, latest_to = db.latest_balance_period(account)
        latest = (db.statement_balances(
            account=account, report_from=latest_from, report_to=latest_to)
            if latest_from is not None or latest_to is not None else [])
        for b in latest:
            anchors.append({**b,
                            "type_label": BAL_LABEL.get(b["balance_type"],
                                                        b["balance_type"]),
                            "balance_display": money(b["balance"], b["currency"])})
        anchor_offset = max(0, (anchor_page - 1) * anchor_per_page)
        history_rows, anchor_total = db.query_statement_balances(
            account=account, limit=anchor_per_page, offset=anchor_offset,
            exclude_report_from=latest_from, exclude_report_to=latest_to)
        anchor_pages = max(1, (anchor_total + anchor_per_page - 1) // anchor_per_page)
        if anchor_page > anchor_pages:
            anchor_page = anchor_pages
            anchor_offset = (anchor_page - 1) * anchor_per_page
            history_rows, anchor_total = db.query_statement_balances(
                account=account, limit=anchor_per_page, offset=anchor_offset,
                exclude_report_from=latest_from, exclude_report_to=latest_to)
        for b in history_rows:
            anchor_history.append({**b,
                                   "type_label": BAL_LABEL.get(
                                       b["balance_type"], b["balance_type"]),
                                   "balance_display": money(
                                       b["balance"], b["currency"])})
    except Exception:  # noqa: BLE001
        anchors = []
        anchor_history = []

    cat_counts = db.cash_flow_category_counts()
    return {
        "reconciliation_by_currency": ccy_recon,
        "balance_anchors": anchors,
        "anchor_history": anchor_history,
        "anchor_total": anchor_total,
        "anchor_page": anchor_page,
        "anchor_pages": max(1, (anchor_total + anchor_per_page - 1) // anchor_per_page),
        "rate_count": _safe(db.conversion_rate_count),
        "waterfall": _waterfall(summary) if summary else None,
        "flex": {
            **st,
            "status_label": STATUS_LABEL.get(st.get("status"), st.get("status")),
            "configured": configured,
            "last_run": (_run_view(st.get("last_run"), tz)
                         if st.get("last_run") else None),
            "using_fallback": not configured,
        },
        "summary": _summary_view(summary) if summary else None,
        "flows": [decorate_flow(r, tz) for r in rows],
        "total": total, "page": page, "per_page": per_page,
        "pages": max(1, (total + per_page - 1) // per_page),
        "category_counts": {CAT_LABEL.get(k, k): v
                            for k, v in cat_counts.items()},
        "reconciliation": recon,
        "filters": {"range": range_key, "category": category},
        "range_options": [("7d", "7 天"), ("30d", "30 天"),
                          ("90d", "90 天"), ("ytd", "年初至今")],
        "category_options": [("", "全部")] + [
            (k, CAT_LABEL[k]) for k in cm.CATEGORIES],
        "period": {"start": start, "end": end},
        "setup_steps": _setup_steps() if not configured else None,
    }


def _safe(fn, default=0):
    try:
        return fn()
    except Exception:  # noqa: BLE001
        return default


def _waterfall(s: dict) -> list[dict]:
    """收益归因瀑布。**None 显示为「不可用」，绝不当成 0。**"""
    items = [
        ("realized_pnl", "已实现盈亏", s.get("realized_pnl")),
        ("unrealized_pnl_change", "未实现盈亏变化", s.get("unrealized_pnl_change")),
        ("dividends", "分红", s.get("dividends")),
        ("interest", "利息", s.get("interest")),
        ("commissions", "佣金", s.get("commissions")),
        ("fees", "其他费用", s.get("fees")),
        ("taxes", "税费", s.get("taxes")),
        ("fx_effect", "汇率影响", s.get("fx_effect")),
        ("unexplained_difference", "未解释差额",
         s.get("unexplained_difference")),
    ]
    out = []
    for key, label, val in items:
        d = _d(val)
        out.append({
            "key": key, "label": label,
            "value": val,
            "display": ("不可用" if d is None else money(val)),
            "available": d is not None,
            "positive": (d is not None and d > 0),
            "negative": (d is not None and d < 0),
        })
    return out


def _run_view(r: dict, tz) -> dict:
    from .webformat import abs_time
    return {
        **r,
        "status_label": STATUS_LABEL.get(r.get("status"), r.get("status")),
        "started": abs_time(r.get("started_at"), tz),
        "completed": (abs_time(r.get("completed_at"), tz)
                      if r.get("completed_at") else None),
        "sections": _jload(r.get("section_counts"), {}),
    }


def _summary_view(s: dict) -> dict:
    """收益概览展示对象。

    刻意把 raw_nav_change 与 cash_flow_adjusted_pnl 并列 ——
    两者差多少，就是资金进出对「表面业绩」的影响有多大。
    """
    md = s.get("modified_dietz_detail") or {}
    twr = s.get("twr_detail") or {}
    return {
        **s,
        "opening_display": money(s.get("opening_nav")),
        "closing_display": money(s.get("closing_nav")),
        "raw_change_display": money(s.get("raw_nav_change")),
        "deposits_display": money(s.get("deposits")),
        "withdrawals_display": money(s.get("withdrawals")),
        "adjusted_display": money(s.get("cash_flow_adjusted_pnl")),
        "realized_display": money(s.get("realized_pnl")),
        "unrealized_display": money(s.get("unrealized_pnl")),
        "dividends_display": money(s.get("dividends")),
        "interest_display": money(s.get("interest")),
        "commissions_display": money(s.get("commissions")),
        "fees_display": money(s.get("fees")),
        "taxes_display": money(s.get("taxes")),
        "unexplained_display": money(s.get("unexplained_difference")),
        "md_display": pct(s.get("modified_dietz_return")),
        "twr_display": pct(s.get("time_weighted_return")),
        "md_available": s.get("modified_dietz_return") is not None,
        "twr_available": s.get("time_weighted_return") is not None,
        "md_reason": md.get("reason"), "twr_reason": twr.get("reason"),
        "md_formula": md.get("formula"), "twr_formula": twr.get("formula"),
        "weighting": {"exact_timestamp": "精确时刻", "day_weighted": "按日权重",
                      "midpoint": "中点权重"}.get(
                          s.get("weighting_assumption"),
                          s.get("weighting_assumption")),
        "quality_label": {"ok": "可靠", "estimated": "估算",
                          "insufficient": "数据不足"}.get(
                              s.get("data_quality"), s.get("data_quality")),
        "source_label": {"flex_query": "券商权威流水",
                         "flex_query_partial": "券商权威流水（末尾数日待补）",
                         "heuristic": "启发式推断",
                         "none": "无现金流数据"}.get(
                             s.get("cash_flow_source"), s.get("cash_flow_source")),
        # 报表覆盖了该区间、且区间内确实没有外部资金变动 ——
        # 这是**已验证的事实**，不该跟「没有数据」共用同一段警告文案
        "verified_no_flows": (
            s.get("cash_flow_source") in ("flex_query", "flex_query_partial")
            and _d(s.get("external_cash_flow")) in (None, Decimal("0"))),
        "coverage": s.get("coverage") or {},
    }


def _setup_steps() -> list[str]:
    """未配置时给出的操作指引。写在页面上，用户不必翻文档。"""
    return [
        "登录 IBKR 客户端门户（Client Portal）",
        "进入 Performance & Reports → Flex Queries",
        "新建一个 Activity Flex Query，勾选 Cash Transactions、Trades、"
        "Statement of Funds、Account Information 四个 section",
        "Date/Period 选 Last 90 Days，Format 选 XML，Version 选 3",
        "保存后记下 Query ID",
        "进入 Settings → Flex Web Service，启用并生成 Token（有效期最长 1 年）",
        "把 Query ID 与 Token 填入服务器 .env 的 FLEX_QUERY_ID / FLEX_TOKEN",
        "重启 sync 容器后，本页会自动开始显示流水",
    ]
