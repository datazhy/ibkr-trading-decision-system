"""Flex 报表 XML 遍历与校验（纯函数，无网络、无 I/O）。

IBKR 的 Flex 报表在不同 Query 配置、不同版本下**外层包装并不一致**：

    FlexQueryResponse → FlexStatements → FlexStatement   （最常见）
    FlexStatements → FlexStatement                        （部分导出）
    FlexStatement                                          （片段）

而且 section 容器时有时无：真实生产报表里 Statement of Funds 的容器叫
`StmtFunds`（不是 `StatementOfFunds`），52 条 `StatementOfFundsLine` 直接挂在
它下面。所以**绝不能按固定路径找节点**，只能按局部标签名递归查找。

一条硬性原则：**找不到 FlexStatement 就是错误**，必须抛异常。返回空列表然后
继续标记 success，会让「一次什么都没拿到的同步」看起来像成功 —— 那比直接失败
危险得多，因为它不会引起任何人注意。
"""
from __future__ import annotations

import xml.etree.ElementTree as ET
from typing import Iterable, Iterator


class FlexStructureError(ValueError):
    """报表结构不符合预期。"""


def local_name(tag) -> str:
    """去掉命名空间的局部标签名。

    `{http://x}FlexStatement` -> `FlexStatement`
    非字符串（注释、PI 节点的 tag 是可调用对象）返回空串。
    """
    if not isinstance(tag, str):
        return ""
    if tag.startswith("{"):
        return tag.split("}", 1)[1]
    return tag


def iter_by_local_name(root, name: str) -> Iterator:
    """递归遍历所有局部名匹配的后代（含 root 自身）。

    用它替代 `root.iter(name)` —— 后者在有命名空间时一个都匹配不到，
    而 IBKR 是否带 namespace 取决于导出方式，不能赌。
    """
    if local_name(getattr(root, "tag", None)) == name:
        yield root
    for el in root.iter():
        if el is root:
            continue
        if local_name(el.tag) == name:
            yield el


def find_descendants_by_local_name(root, name: str) -> list:
    return list(iter_by_local_name(root, name))


def count_by_local_name(root, name: str) -> int:
    return sum(1 for _ in iter_by_local_name(root, name))


def parse_xml(xml: str):
    """解析字符串为根节点。解析失败抛 FlexStructureError（不返回 None）。"""
    if not xml or not xml.strip():
        raise FlexStructureError("报表内容为空")
    try:
        return ET.fromstring(xml)
    except ET.ParseError as exc:
        raise FlexStructureError(f"XML 解析失败: {exc}") from exc


def extract_flex_statements(root) -> list:
    """取出全部 FlexStatement 节点。

    支持 FlexQueryResponse / FlexStatements / 裸 FlexStatement 三种外层，
    带不带命名空间都可以。**一个都找不到时抛异常**。
    """
    if local_name(getattr(root, "tag", None)) == "FlexStatement":
        return [root]
    statements = find_descendants_by_local_name(root, "FlexStatement")
    if not statements:
        raise FlexStructureError(
            f"报表中没有任何 FlexStatement 节点（根标签 "
            f"{local_name(getattr(root, 'tag', None)) or '?'}）")
    return statements


#: 业务明细节点。校验时至少要出现其中之一，否则这份报表没有任何可用内容。
DETAIL_NODES = ("CashTransaction", "Trade", "StatementOfFundsLine",
                "ClosedLot", "OpenPosition", "Order", "TradeConfirm",
                "ChangeInNAV", "Transfer")

#: section 计数按**叶子记录**统计，不按容器。容器不存在不代表没有数据 ——
#: 真实报表里 `StatementOfFunds` 容器就不存在，但有 52 条明细行。
SECTION_LEAF = {
    "account_information": "AccountInformation",
    "cash_transactions": "CashTransaction",
    "trades": "Trade",
    "statement_of_funds": "StatementOfFundsLine",
    "closed_lots": "ClosedLot",
    "open_positions": "OpenPosition",
    "change_in_nav": "ChangeInNAV",
    "transfers": "Transfer",
}


def section_counts(root) -> dict[str, int]:
    """各 section 的叶子记录数。"""
    return {key: count_by_local_name(root, leaf)
            for key, leaf in SECTION_LEAF.items()}


def validate_final_report(xml: str) -> dict:
    """校验一份**最终报表**（GetStatement 的产物）。

    通过则返回结构摘要，不通过一律抛 FlexStructureError。
    绝不「空成功」—— 这是本模块存在的主要理由。
    """
    root = parse_xml(xml)
    root_tag = local_name(root.tag)

    # 先识别错误响应：IBKR 的失败也是 HTTP 200 + XML
    status = None
    for el in iter_by_local_name(root, "Status"):
        status = (el.text or "").strip()
        break
    if status and status.lower() == "fail":
        code = _first_text(root, "ErrorCode") or "?"
        msg = _first_text(root, "ErrorMessage") or "未知错误"
        raise FlexStructureError(f"报表返回失败状态 [{code}] {msg}")

    statements = extract_flex_statements(root)     # 找不到即抛

    counts = section_counts(root)
    has_detail = any(count_by_local_name(root, n) > 0 for n in DETAIL_NODES)
    has_account = counts.get("account_information", 0) > 0
    if not (has_detail or has_account):
        raise FlexStructureError(
            "报表中既没有 AccountInformation，也没有任何业务明细节点，"
            "这不是一份可用的报表")

    period_from = period_to = None
    account_ids: list[str] = []
    for st in statements:
        period_from = period_from or st.attrib.get("fromDate") or st.attrib.get("periodFrom")
        period_to = period_to or st.attrib.get("toDate") or st.attrib.get("periodTo")
        acc = st.attrib.get("accountId")
        if acc and acc not in account_ids:
            account_ids.append(acc)

    return {
        "root_tag": root_tag,
        "flex_statements": len(statements),
        "section_counts": counts,
        "detail_records": sum(count_by_local_name(root, n) for n in DETAIL_NODES),
        "period_from": period_from, "period_to": period_to,
        "account_count": len(account_ids),
        "has_namespace": isinstance(root.tag, str) and root.tag.startswith("{"),
    }


def _first_text(root, name: str) -> str | None:
    for el in iter_by_local_name(root, name):
        return (el.text or "").strip() or None
    return None


def parse_send_request_response(xml: str) -> str:
    """SendRequest 响应 -> ReferenceCode。

    空 ReferenceCode 一律抛异常 —— 拿着空引用去 GetStatement 只会得到
    一份无意义的响应，而后续流程可能把它当成「报表为空」而静默成功。
    """
    root = parse_xml(xml)
    status = _first_text(root, "Status")
    if status and status.lower() == "fail":
        code = _first_text(root, "ErrorCode") or "?"
        msg = _first_text(root, "ErrorMessage") or "未知错误"
        raise FlexStructureError(f"SendRequest 被拒绝 [{code}] {msg}")
    ref = _first_text(root, "ReferenceCode")
    if not ref:
        raise FlexStructureError(
            "SendRequest 响应中没有 ReferenceCode（或为空），无法继续获取报表")
    return ref


def looks_like_not_ready(xml: str) -> bool:
    """是否为「报表尚未生成」（错误码 1019）。这不是失败，应继续轮询。"""
    try:
        root = parse_xml(xml)
    except FlexStructureError:
        return False
    # 生产实测 GetStatement 会把 1019 返回成 Status=Warn，而不是 Fail。
    # 只按 Fail 判断会在第一次轮询就把「正在生成」当成最终 XML，随后报
    # “没有 FlexStatement”，导致 T+1 官方报表永远停在前一天。
    return (_first_text(root, "ErrorCode") or "") == "1019"


def error_of(xml: str) -> str | None:
    """若为错误响应返回 `[码] 描述`，否则 None。"""
    try:
        root = parse_xml(xml)
    except FlexStructureError:
        return None
    status = _first_text(root, "Status")
    if not status or status.lower() not in ("fail", "warn"):
        return None
    return (f"[{_first_text(root, 'ErrorCode') or '?'}] "
            f"{_first_text(root, 'ErrorMessage') or '未知错误'}")
