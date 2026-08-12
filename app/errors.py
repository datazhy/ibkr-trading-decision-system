"""数据源（券商）错误类型。

只读系统里所有对外部数据源的失败都归一到 BrokerApiError：调用方据此决定是
跳过本轮、降级，还是推送告警 —— 绝不因为一次取数失败就写入残缺快照。
"""
from __future__ import annotations


class BrokerApiError(RuntimeError):
    """券商 API 调用失败（网络异常 / 非预期响应 / 连接不可用）。"""

    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code
