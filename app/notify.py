"""Apprise 推送封装（→ 飞书等），带按 key 的时间窗去重。"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from .db import Database
from .logging_setup import get_logger

log = get_logger()


class Notifier:
    def __init__(self, apprise_urls: list[str], db: Database):
        self.db = db
        self._urls = apprise_urls
        self._apprise = None  # 延迟导入，便于测试

    def _client(self):
        if self._apprise is None:
            import apprise  # 延迟导入
            ap = apprise.Apprise()
            for url in self._urls:
                if not ap.add(url):
                    log.warning("Apprise URL 添加失败（格式或类型不支持）")
            self._apprise = ap
        return self._apprise

    def send(self, title: str, body: str) -> bool:
        """立即推送。无配置 URL 时仅记录日志并返回 False。"""
        if not self._urls:
            log.warning("未配置 APPRISE_URLS，跳过推送：%s", title)
            return False
        try:
            ok = self._client().notify(title=title, body=body)
            if not ok:
                log.error("Apprise 推送失败：%s", title)
            return bool(ok)
        except Exception:  # noqa: BLE001 — 推送失败不能拖垮主流程
            log.exception("Apprise 推送异常：%s", title)
            return False

    def send_deduped(
        self, alert_key: str, title: str, body: str, dedup_window: timedelta
    ) -> bool:
        """去重推送：同一 alert_key 在窗口内只推一次。"""
        last = self.db.last_alert_ts(alert_key)
        now = datetime.now(timezone.utc)
        if last is not None and now - last < dedup_window:
            log.info("告警去重跳过：%s（距上次 %s）", alert_key, now - last)
            return False
        ok = self.send(title, body)
        if ok:
            self.db.record_alert(alert_key, f"{title}\n{body}")
        return ok
