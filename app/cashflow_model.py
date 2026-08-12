"""统一现金流分类体系（纯函数，无 I/O）。

这一层要回答一个看似简单、实则最容易搞错的问题：**账户里的钱变了，是因为我赚了，
还是因为我把钱搬进搬出了？**

四条纪律：

1. **外部现金流与交易现金流严格分开**。买入股票导致现金下降，绝不是「出金」；
   卖出导致现金上升，绝不是「入金」。混在一起会让「扣除资金流后的收益」把
   正常交易也扣掉，得出完全错误的业绩。

2. **分类不确定就是 unknown**。IBKR 的 CashTransaction type 字段有几十种取值，
   还会随版本增加。认不出来的一律归 unknown 并保留原文，绝不猜 ——
   猜错一笔分红为入金，就会让当期收益凭空少一笔。

3. **去重优先用券商流水号**。description 不能作为唯一性依据：同一天两笔金额相同的
   佣金，描述可能一字不差。

4. **凭证绝不进入任何输出**。raw_json 落库前脱敏。
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

# ---------------- 一级分类 ----------------
CAT_EXTERNAL = "external_cash_flow"     # 真正的资金进出，影响业绩口径
CAT_TRADE = "trade_cash_flow"           # 买卖证券产生，**不是**资金进出
CAT_INCOME = "income"                   # 分红等收入
CAT_EXPENSE = "expense"                 # 佣金、行情费等支出
CAT_TAX = "tax"                         # 代扣税
CAT_INTEREST = "interest"               # 利息（可正可负）
CAT_TRANSFER = "transfer"               # 账户间划转
CAT_CORPORATE = "corporate_action"      # 公司行动
CAT_UNKNOWN = "unknown"

CATEGORIES = (CAT_EXTERNAL, CAT_TRADE, CAT_INCOME, CAT_EXPENSE, CAT_TAX,
              CAT_INTEREST, CAT_TRANSFER, CAT_CORPORATE, CAT_UNKNOWN)

#: 只有这两类会进入「外部现金流」口径参与业绩调整。
#: transfer 也算 —— 账户间划转对本账户而言就是资金进出。
EXTERNAL_CATEGORIES = (CAT_EXTERNAL, CAT_TRANSFER)

DIR_IN = "inflow"
DIR_OUT = "outflow"

QUALITY_OK = "ok"
QUALITY_ESTIMATED = "estimated"
QUALITY_UNCLASSIFIED = "unclassified"
QUALITY_CONFLICT = "conflict"

#: IBKR CashTransaction 的 type 字段 -> (一级分类, 子类型)。
#: 键全部小写并去掉非字母数字，用于容忍 IBKR 在不同报表版本里的写法差异
#: （"Withholding Tax" / "WITHHOLDING_TAX" / "withholdingtax" 都能命中）。
_TYPE_MAP: dict[str, tuple[str, str]] = {
    # 出入金
    "deposits": (CAT_EXTERNAL, "deposit"),
    "depositswithdrawals": (CAT_EXTERNAL, "deposit_withdrawal"),
    "deposit": (CAT_EXTERNAL, "deposit"),
    "withdrawal": (CAT_EXTERNAL, "withdrawal"),
    "withdrawals": (CAT_EXTERNAL, "withdrawal"),
    "electronicfundtransfer": (CAT_EXTERNAL, "deposit_withdrawal"),
    "wiretransfer": (CAT_EXTERNAL, "deposit_withdrawal"),
    # 划转
    "internaltransfer": (CAT_TRANSFER, "internal_transfer"),
    "internaltransfers": (CAT_TRANSFER, "internal_transfer"),
    "accounttransfer": (CAT_TRANSFER, "account_transfer"),
    "assettransfer": (CAT_TRANSFER, "account_transfer"),
    # 分红
    "dividends": (CAT_INCOME, "dividend"),
    "dividend": (CAT_INCOME, "dividend"),
    "paymentinlieuofdividends": (CAT_INCOME, "payment_in_lieu"),
    "paymentinlieu": (CAT_INCOME, "payment_in_lieu"),
    "dividendaccrual": (CAT_INCOME, "dividend_accrual"),
    "dividendreversal": (CAT_INCOME, "dividend_reversal"),
    # 税
    "withholdingtax": (CAT_TAX, "withholding_tax"),
    "taxes": (CAT_TAX, "withholding_tax"),
    "tax": (CAT_TAX, "withholding_tax"),
    "backupwithholding": (CAT_TAX, "backup_withholding"),
    # 利息
    "brokerinterestreceived": (CAT_INTEREST, "credit_interest"),
    "brokerinterestpaid": (CAT_INTEREST, "debit_interest"),
    "creditinterest": (CAT_INTEREST, "credit_interest"),
    "debitinterest": (CAT_INTEREST, "debit_interest"),
    "margininterest": (CAT_INTEREST, "margin_interest"),
    "cashinterest": (CAT_INTEREST, "cash_interest"),
    "bondinterest": (CAT_INTEREST, "bond_interest"),
    "bondinterestreceived": (CAT_INTEREST, "bond_interest"),
    "bondinterestpaid": (CAT_INTEREST, "bond_interest"),
    # 费用
    "commissionadjustments": (CAT_EXPENSE, "trade_commission"),
    "commission": (CAT_EXPENSE, "trade_commission"),
    "brokerfees": (CAT_EXPENSE, "other_fee"),
    "otherfees": (CAT_EXPENSE, "other_fee"),
    "othefees": (CAT_EXPENSE, "other_fee"),
    "regulatoryfee": (CAT_EXPENSE, "regulatory_fee"),
    "marketdatafee": (CAT_EXPENSE, "market_data_fee"),
    "subscriptionfee": (CAT_EXPENSE, "subscription_fee"),
    "advisorfees": (CAT_EXPENSE, "other_fee"),
    # 公司行动
    "corporateaction": (CAT_CORPORATE, "corporate_action"),
    "stockdividend": (CAT_CORPORATE, "stock_dividend"),
    "spinoff": (CAT_CORPORATE, "spinoff"),
    # 交易现金流（StatementOfFunds 里的活动）
    "buy": (CAT_TRADE, "trade_buy"),
    "sell": (CAT_TRADE, "trade_sell"),
    "trades": (CAT_TRADE, "trade"),
    "optionpremium": (CAT_TRADE, "option_premium"),
    "bondmaturity": (CAT_TRADE, "bond_principal"),
    "forex": (CAT_TRADE, "fx_trade"),
    "fxtranslation": (CAT_TRADE, "fx_translation"),
}

#: 描述文本里的兜底关键词。只在 type 认不出来时用，且命中后标记为 estimated ——
#: 靠描述猜出来的分类，可信度天然低于 type 字段。
_DESC_HINTS: list[tuple[re.Pattern, tuple[str, str]]] = [
    (re.compile(r"withholding\s*tax", re.I), (CAT_TAX, "withholding_tax")),
    (re.compile(r"payment\s*in\s*lieu", re.I), (CAT_INCOME, "payment_in_lieu")),
    (re.compile(r"\bdividend\b", re.I), (CAT_INCOME, "dividend")),
    (re.compile(r"margin\s*interest", re.I), (CAT_INTEREST, "margin_interest")),
    (re.compile(r"\binterest\b", re.I), (CAT_INTEREST, "cash_interest")),
    (re.compile(r"commission", re.I), (CAT_EXPENSE, "trade_commission")),
    (re.compile(r"market\s*data", re.I), (CAT_EXPENSE, "market_data_fee")),
    (re.compile(r"\bfee\b", re.I), (CAT_EXPENSE, "other_fee")),
    (re.compile(r"wire|ach|electronic\s*fund", re.I),
     (CAT_EXTERNAL, "deposit_withdrawal")),
]

#: raw_json 里必须抹掉的键（防止任何环节把凭证带进库）。
_SENSITIVE_KEYS = ("t", "token", "flex_token", "q", "queryid", "query_id",
                   "apikey", "api_key", "password", "secret", "auth")
#: 值里出现这些形态也一并抹掉（Flex token 是长数字串）。
_TOKEN_PATTERN = re.compile(r"\b\d{12,}\b")


def _norm_type(raw: str | None) -> str:
    return re.sub(r"[^a-z0-9]", "", (raw or "").lower())


def classify(raw_type: str | None, description: str | None = None,
             *, has_symbol: bool = False) -> tuple[str, str, str]:
    """分类一笔流水。返回 (category, subtype, quality)。

    认不出来返回 unknown —— **绝不猜测**。一笔被猜错的分红会直接让当期收益失真。
    """
    key = _norm_type(raw_type)
    if key in _TYPE_MAP:
        cat, sub = _TYPE_MAP[key]
        # 「其它费用」这类笼统子类型，用描述在**同一级分类内**细化。
        # 这不是猜测 —— 一级分类仍由权威的 type 字段决定，只是把
        # market_data_fee 从 other_fee 里区分出来，便于单独查询。
        if sub == "other_fee" and description:
            for pattern, (hint_cat, hint_sub) in _DESC_HINTS:
                if hint_cat == cat and pattern.search(description):
                    return cat, hint_sub, QUALITY_OK
        return cat, sub, QUALITY_OK
    # type 认不出来时，用描述做兜底，但明确标记为估算
    for pattern, (cat, sub) in _DESC_HINTS:
        if description and pattern.search(description):
            return cat, sub, QUALITY_ESTIMATED
    return CAT_UNKNOWN, (raw_type or "unknown").strip()[:60], QUALITY_UNCLASSIFIED


def direction_of(amount: Decimal | None) -> str | None:
    if amount is None or amount == 0:
        return None
    return DIR_IN if amount > 0 else DIR_OUT


def is_external(category: str | None) -> bool:
    """是否计入「外部现金流」。

    这是全模块最关键的一个判断：只有它返回 True 的流水，才会在业绩计算里
    被当成「我搬进搬出的钱」而扣除。分红、利息、佣金都不算 —— 它们是
    投资活动的结果，本来就该计入收益。
    """
    return category in EXTERNAL_CATEGORIES


def d(v: Any) -> Decimal | None:
    if v is None or v == "":
        return None
    if isinstance(v, Decimal):
        return v
    try:
        return Decimal(str(v).replace(",", ""))
    except (InvalidOperation, ValueError, TypeError):
        return None


# ---------------- 去重 ----------------

def dedupe_key(*, broker_transaction_id: str | None, account_id: str | None,
               occurred_at: str | None, category: str | None,
               subtype: str | None, amount: Decimal | str | None,
               currency: str | None, symbol: str | None) -> str:
    """稳定的去重键。

    优先用券商流水号；没有时才退回「日期+账户+类型+金额+币种+标的」的哈希。
    **绝不把 description 作为唯一性依据** —— 同一天两笔金额相同的佣金，
    描述可能一字不差，用它去重会误删真实流水。
    """
    if broker_transaction_id:
        return f"txn:{str(broker_transaction_id).strip()}"
    parts = [str(account_id or ""), str(occurred_at or "")[:10],
             str(category or ""), str(subtype or ""),
             str(amount if amount is not None else ""), str(currency or ""),
             str(symbol or "")]
    return "hash:" + hashlib.sha256("|".join(parts).encode()).hexdigest()[:24]


def content_hash(row: dict) -> str:
    """内容指纹。用于判断「同一笔流水的内容是否被券商修正过」。"""
    parts = [str(row.get(k) or "") for k in
             ("amount", "currency", "occurred_at", "settled_at", "category",
              "subtype", "symbol", "conid", "fx_rate", "amount_base")]
    return hashlib.sha256("|".join(parts).encode()).hexdigest()[:24]


# ---------------- 脱敏 ----------------

def redact(obj: Any) -> Any:
    """递归脱敏。任何疑似凭证的键或长数字串都换成占位符。

    这是 raw_json 落库前的最后一道闸 —— 上游若不慎把请求参数塞进来，
    这里必须挡住。
    """
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if str(k).lower() in _SENSITIVE_KEYS:
                out[k] = "***REDACTED***"
            else:
                out[k] = redact(v)
        return out
    if isinstance(obj, (list, tuple)):
        return [redact(x) for x in obj]
    if isinstance(obj, str):
        return _TOKEN_PATTERN.sub("***", obj)
    return obj


def to_raw_json(obj: Any, *, limit: int = 4000) -> str:
    """脱敏后序列化。超长截断，避免单条流水撑爆库。"""
    try:
        text = json.dumps(redact(obj), ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        text = json.dumps({"note": "序列化失败"}, ensure_ascii=False)
    return text[:limit]


def sanitize_error(msg: str | None) -> str:
    """异常信息脱敏。IBKR 的错误有时会回显请求 URL，其中带着 token。"""
    if not msg:
        return ""
    text = str(msg)
    text = re.sub(r"([?&](?:t|q|token)=)[^&\s]+", r"\1***", text, flags=re.I)
    text = _TOKEN_PATTERN.sub("***", text)
    return text[:400]


# ---------------- 标准化的一行 ----------------

@dataclass
class CashFlowRow:
    """标准化后的一笔现金流。字段与 cash_flows 表一一对应。"""
    account_id: str | None
    occurred_at: str
    et_date: str
    category: str
    subtype: str
    direction: str | None
    amount: Decimal
    currency: str
    amount_base: Decimal | None = None
    fx_rate: Decimal | None = None
    settled_at: str | None = None
    symbol: str | None = None
    conid: str | None = None
    description: str | None = None
    broker_transaction_id: str | None = None
    source: str = "FLEX_QUERY"
    quality_status: str = QUALITY_OK
    raw: dict | None = None

    def as_db_row(self, now_iso: str) -> dict:
        key = dedupe_key(
            broker_transaction_id=self.broker_transaction_id,
            account_id=self.account_id, occurred_at=self.occurred_at,
            category=self.category, subtype=self.subtype, amount=self.amount,
            currency=self.currency, symbol=self.symbol)
        row = {
            "cashflow_id": key,
            "broker_transaction_id": self.broker_transaction_id,
            "account": self.account_id, "account_id": self.account_id,
            "occurred_at": self.occurred_at, "settled_at": self.settled_at,
            "effective_at": self.occurred_at, "et_date": self.et_date,
            "category": self.category, "subtype": self.subtype,
            "direction": self.direction or direction_of(self.amount),
            "amount": str(self.amount), "currency": self.currency,
            "amount_base": (str(self.amount_base)
                            if self.amount_base is not None else None),
            "fx_rate": str(self.fx_rate) if self.fx_rate is not None else None,
            "symbol": self.symbol, "conid": self.conid,
            "description": self.description, "note": self.description,
            "source": self.source, "external_ref": self.broker_transaction_id,
            "raw_json": to_raw_json(self.raw or {}),
            "quality_status": self.quality_status,
            "created_at": now_iso, "updated_at": now_iso,
        }
        row["content_hash"] = content_hash(row)
        return row
