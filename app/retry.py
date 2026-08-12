"""有限重试：只重试「重试有意义」的错误。

金融数据同步里，无差别重试是有害的：
  - 认证失败重试 = 拿错误凭证反复敲券商，可能触发风控锁定；
  - 权限/字段校验错误重试 = 确定失败的请求白白占住单线程执行器；
  - 无上限重试 = 一次故障拖垮整个调度周期。

所以这里按错误类型分流，只对**瞬时性**故障退避重试，且必须有上限。
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable, TypeVar

T = TypeVar("T")

#: 可重试：网络抖动、超时、连接被拒等瞬时故障
_RETRYABLE_TYPES = ("TimeoutError", "ConnectionError", "ConnectionResetError",
                    "ConnectionRefusedError", "ConnectionAbortedError",
                    "BrokenPipeError", "socket.timeout", "OSError")
_RETRYABLE_TEXT = ("timeout", "timed out", "connection reset", "connection refused",
                   "temporarily unavailable", "broken pipe", "not connected",
                   "connection is closed", "no connection")

#: 不可重试：重试也不会变好，还可能有副作用
_FATAL_TEXT = ("auth", "unauthorized", "permission", "forbidden", "denied",
               "invalid credential", "login failed", "not subscribed",
               "no security definition", "invalid contract", "bad request")


def classify(exc: BaseException) -> tuple[bool, str]:
    """返回 (是否可重试, 错误码)。判定顺序：先看致命，再看可重试。"""
    name = type(exc).__name__
    text = str(exc).lower()
    for kw in _FATAL_TEXT:
        if kw in text:
            return False, "AUTH_OR_PERMISSION" if kw in (
                "auth", "unauthorized", "permission", "forbidden", "denied",
                "invalid credential", "login failed") else "INVALID_REQUEST"
    if name in _RETRYABLE_TYPES or any(k in text for k in _RETRYABLE_TEXT):
        return True, "TRANSIENT_NETWORK"
    return False, "UNKNOWN_ERROR"


@dataclass
class RetryOutcome:
    ok: bool
    value: object = None
    retry_count: int = 0
    error_code: str | None = None
    attempts: list[str] = field(default_factory=list)   # 每次失败原因（已截断）
    last_exc: BaseException | None = None

    def attempts_text(self, limit: int = 400) -> str:
        return " | ".join(self.attempts)[:limit]


def call_with_retry(fn: Callable[[], T], *, max_retries: int, base_delay: float = 1.0,
                    max_delay: float = 30.0, sleep=time.sleep) -> RetryOutcome:
    """执行 fn，对瞬时错误指数退避重试。

    max_retries 是**额外重试次数**（0 = 只试一次）。退避 base*2^n，封顶 max_delay。
    sleep 可注入，便于测试不真等。
    """
    attempts: list[str] = []
    for n in range(max_retries + 1):
        try:
            return RetryOutcome(ok=True, value=fn(), retry_count=n, attempts=attempts)
        except BaseException as exc:  # noqa: BLE001 — 需按类型分流后再决定是否吞
            retryable, code = classify(exc)
            attempts.append(f"#{n + 1} {type(exc).__name__}: {str(exc)[:80]}")
            if not retryable or n >= max_retries:
                return RetryOutcome(ok=False, retry_count=n, error_code=code,
                                    attempts=attempts, last_exc=exc)
            sleep(min(base_delay * (2 ** n), max_delay))
    # 不可达
    return RetryOutcome(ok=False, retry_count=max_retries, error_code="UNKNOWN_ERROR",
                        attempts=attempts)
