"""IBKR Flex Query：历史成交回补 + 资金流核对。

**为什么需要它**
  reqExecutions 只返回**当日**成交。sync 若挂掉一整天（历史上真的发生过 4 天），
  那天的成交就永久缺失 —— 已实现盈亏的历史从此有个洞，且补不回来。
  Flex Query 是 IBKR 唯一的历史成交来源。

  资金流同理：API 侧完全没有入金/出金字段（已扫遍 accountValues），
  Flex 的 CashTransactions 是唯一能自动拿到准确金额的地方。

**幂等**
  Flex 的 ibExecID 与 reqExecutions 的 execId 是同一个值，所以回补与实时同步
  天然幂等：同一笔成交无论从哪条路进来，都是同一行。

**时区陷阱**
  Flex 的 dateTime 格式是 `20260716;153555`，**不带时区**，用的是账户设定的时区
  （通常美东）。这里显式要求调用方传入时区，绝不默认 UTC —— 差 4~5 小时会让
  成交落到错误的交易日。

本模块是纯函数（解析 + 归一），网络请求在 FlexClient 里，便于用真实 XML 做单测。
"""
from __future__ import annotations

import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

#: Flex Web Service 端点
FLEX_BASE = "https://ndcdyn.interactivebrokers.com/AccountManagement/FlexWebService"
SEND_URL = f"{FLEX_BASE}/SendRequest"
GET_URL = f"{FLEX_BASE}/GetStatement"

#: CashTransaction 里代表出入金的 type（其余如利息/手续费不是资金流）
_DEPOSIT_TYPES = {"deposits/withdrawals", "deposits & withdrawals"}

_SIDE_MAP = {"BUY": "BUY", "SELL": "SELL", "BOT": "BUY", "SLD": "SELL"}


class FlexError(RuntimeError):
    pass


def _d(v) -> Decimal | None:
    if v is None or v == "":
        return None
    try:
        return Decimal(str(v))
    except (InvalidOperation, ValueError):
        return None


def parse_flex_datetime(raw: str, tz) -> str | None:
    """`20260716;153555` / `20260716` -> UTC ISO。

    Flex 不带时区，用账户时区（通常美东）。tz 必须显式传入 —— 默认成 UTC
    会让成交落到错误的交易日。
    """
    if not raw:
        return None
    raw = raw.strip().replace(" ", "")
    try:
        if ";" in raw:
            d, t = raw.split(";", 1)
            dt = datetime.strptime(f"{d}{t}", "%Y%m%d%H%M%S")
        else:
            dt = datetime.strptime(raw, "%Y%m%d")
    except ValueError:
        return None
    return dt.replace(tzinfo=tz).astimezone(timezone.utc).isoformat()


def _split_exec_id(exec_id: str) -> tuple[str, int]:
    """与 ibkr_client.split_exec_id 同口径（修正链：root + seq）。"""
    parts = (exec_id or "").rsplit(".", 1)
    if len(parts) == 2 and parts[1].isdigit():
        return parts[0], int(parts[1])
    return exec_id or "", 0


def parse_flex_trades(xml: str, tz) -> list[dict]:
    """Flex Trades -> executions 行（与 reqExecutions 同一 schema，可直接 upsert）。

    只取股票/ETF，与实时路径的口径保持一致。
    """
    try:
        root = ET.fromstring(xml)
    except ET.ParseError as exc:
        raise FlexError(f"Flex XML 解析失败: {exc}") from exc

    out = []
    for t in root.iter("Trade"):
        a = t.attrib
        if a.get("assetCategory") not in (None, "", "STK", "ETF"):
            continue
        exec_id = a.get("ibExecID") or a.get("tradeID")
        if not exec_id:
            continue                      # 无幂等键，宁可不收
        ts = parse_flex_datetime(a.get("dateTime") or a.get("tradeDate"), tz)
        if not ts:
            continue
        qty = _d(a.get("quantity")) or Decimal("0")
        side = _SIDE_MAP.get((a.get("buySell") or "").upper())
        if side is None:
            side = "SELL" if qty < 0 else "BUY"   # 无 buySell 时按数量符号
        root_id, seq = _split_exec_id(exec_id)
        comm = _d(a.get("ibCommission"))
        out.append({
            "exec_id": exec_id,
            "correction_root": root_id,
            "correction_seq": seq,
            "perm_id": int(a["permId"]) if (a.get("permId") or "").isdigit() else None,
            "order_id": int(a["orderID"]) if (a.get("orderID") or "").isdigit() else None,
            "client_id": None,
            "conid": int(a["conid"]) if (a.get("conid") or "").isdigit() else None,
            "symbol": a.get("symbol"),
            "sec_type": a.get("assetCategory") or "STK",
            "exchange": a.get("exchange"),
            "currency": a.get("currency"),
            "side": side,
            "shares": abs(qty),
            "price": _d(a.get("tradePrice")),
            "cumulative_qty": abs(qty),
            "average_price": _d(a.get("tradePrice")),
            "execution_time": ts,
            # Flex 的 ibCommission 是负数（费用），executions 表存正数
            "commission": abs(comm) if comm is not None else None,
            "commission_currency": a.get("ibCommissionCurrency"),
            "realized_pnl": _d(a.get("fifoPnlRealized")),
            "liquidation": 0,
            "order_ref": a.get("orderReference"),
            "last_liquidity": None,
            "source": "FLEX_QUERY",
            "data_status": "READY",
            "raw_json": "{}",
        })
    return out


def parse_flex_cash(xml: str, tz) -> list[dict]:
    """Flex CashTransactions -> cash_flows 行（只取出入金）。

    利息、手续费、分红等也在 CashTransactions 里，但它们不是资金流 ——
    混进来会让「扣除资金流后的盈亏」把利息也当成转账扣掉。
    """
    try:
        root = ET.fromstring(xml)
    except ET.ParseError as exc:
        raise FlexError(f"Flex XML 解析失败: {exc}") from exc

    out = []
    for c in root.iter("CashTransaction"):
        a = c.attrib
        if (a.get("type") or "").strip().lower() not in _DEPOSIT_TYPES:
            continue
        amount = _d(a.get("amount"))
        if amount is None or amount == 0:
            continue
        eff = parse_flex_datetime(a.get("dateTime") or a.get("settleDate")
                                  or a.get("reportDate"), tz)
        if not eff:
            continue
        out.append({
            "account": a.get("accountId"),
            "currency": a.get("currency") or "USD",
            "amount": amount,                  # 正=入金，负=出金（Flex 已带符号）
            "effective_at": eff,
            "et_date": datetime.fromisoformat(eff).astimezone(tz).date().isoformat(),
            "note": a.get("description"),
            "source": "FLEX_QUERY",
            # 券商流水号：可与活动报表逐笔核对，也是幂等键
            "external_ref": a.get("transactionID") or a.get("tradeID"),
        })
    return out


def parse_flex_error(xml: str) -> str | None:
    """Flex 的错误也是 200 + XML（<Status>Fail</Status>），必须显式识别。"""
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return None
    if root.tag == "FlexStatementResponse" or root.find("Status") is not None:
        status = (root.findtext("Status") or "").strip()
        if status.lower() == "fail":
            code = root.findtext("ErrorCode") or "?"
            msg = root.findtext("ErrorMessage") or "未知错误"
            return f"[{code}] {msg}"
    return None


def parse_reference_code(xml: str) -> str:
    """SendRequest 的响应里取 ReferenceCode。"""
    err = parse_flex_error(xml)
    if err:
        raise FlexError(f"Flex 请求被拒绝: {err}")
    try:
        root = ET.fromstring(xml)
    except ET.ParseError as exc:
        raise FlexError(f"Flex 响应解析失败: {exc}") from exc
    code = root.findtext("ReferenceCode")
    if not code:
        raise FlexError("Flex 响应中没有 ReferenceCode")
    return code.strip()


@dataclass
class FetchResult:
    """一次完整取数的全过程记录。

    把 reference_code 与 poll_count 带出来 —— 它们是判断「同步到底走到哪一步」
    的唯一依据。之前把它们埋在客户端内部，导致线上出现「success 但
    reference_code 为空、records 为 0」时无从判断是没请求还是没解析。
    """
    xml: str | None = None
    reference_code: str | None = None
    poll_count: int = 0
    send_request_ok: bool = False
    get_statement_called: bool = False


class FlexClient:
    """Flex Web Service 客户端（两步：SendRequest -> GetStatement）。

    凭证只在本类内部使用，绝不写日志、绝不进异常文本（异常统一走
    cashflow_model.sanitize_error 再对外）。
    """

    def __init__(self, token: str, query_id: str, *, timeout: float = 30.0,
                 poll_interval: float = 5.0, max_polls: int = 12):
        if not token or not query_id:
            raise FlexError("未配置 FLEX_TOKEN / FLEX_QUERY_ID")
        self._token = token
        self._query_id = query_id
        self.timeout = timeout
        self.poll_interval = poll_interval
        self.max_polls = max_polls

    def _get(self, url: str, params: dict) -> str:
        import urllib.parse
        import urllib.request
        full = f"{url}?{urllib.parse.urlencode(params)}"
        req = urllib.request.Request(full, headers={"User-Agent": "risk-dashboard"})
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            return resp.read().decode("utf-8", "replace")

    def fetch_detailed(self) -> FetchResult:
        """完整取数并记录每一步。任何一步不成立都抛异常，绝不返回空成功。"""
        import time
        from . import flex_xml as fx

        res = FetchResult()
        send_xml = self._get(SEND_URL,
                             {"t": self._token, "q": self._query_id, "v": "3"})
        # ReferenceCode 为空即抛 —— 拿空引用去 GetStatement 只会得到一份
        # 无意义响应，而后续可能把它误当成「报表为空」而静默成功
        res.reference_code = fx.parse_send_request_response(send_xml)
        res.send_request_ok = True

        for attempt in range(self.max_polls):
            res.poll_count = attempt + 1
            xml = self._get(GET_URL,
                            {"t": self._token, "q": res.reference_code, "v": "3"})
            res.get_statement_called = True
            if fx.looks_like_not_ready(xml):
                # 1019 = 报表生成中，继续等，这不是失败
                time.sleep(self.poll_interval)
                continue
            err = fx.error_of(xml)
            if err:
                raise FlexError(f"Flex 获取失败: {err}")
            res.xml = xml
            return res
        raise FlexError(f"Flex 报表生成超时（已轮询 {res.poll_count} 次）")

    def fetch(self) -> str:
        """向后兼容的简化入口。"""
        r = self.fetch_detailed()
        if not r.xml:
            raise FlexError("未取得报表内容")
        return r.xml
