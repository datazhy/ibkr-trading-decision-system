"""心跳监控（dead man's switch）。

每次持仓同步成功后 GET 一次 HEARTBEAT_URL（healthchecks.io 等服务）。
超过设定时间没收到心跳，监控服务会反向推送告警（飞书/邮件）——
解决「同步进程整个挂掉时无人报警」的静默故障。

失败绝不抛出：心跳挂了不能影响主流程。
"""
from __future__ import annotations

from .logging_setup import get_logger

log = get_logger()


def ping(url: str | None) -> bool:
    if not url:
        return False
    try:
        import httpx  # 延迟导入
        resp = httpx.get(url, timeout=5, follow_redirects=True)
        return resp.status_code < 400
    except Exception as exc:  # noqa: BLE001
        log.debug("心跳 ping 失败（不影响主流程）: %s", exc)
        return False
