"""日志配置。绝不记录 token、密钥、完整账户号。"""
from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

_SENSITIVE_HINTS = ("token", "secret", "authorization", "refresh_token", "access_token")


class _RedactFilter(logging.Filter):
    """兜底：若日志文本疑似包含敏感字段名，做粗粒度提示（真正的脱敏靠调用方）。"""

    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage().lower()
        if any(h in msg for h in _SENSITIVE_HINTS) and "=" in msg:
            record.msg = "[REDACTED — 疑似敏感信息未脱敏，已拦截原始内容]"
            record.args = ()
        return True


def setup_logging(log_path: str | Path, level: str = "INFO") -> logging.Logger:
    log_path = Path(log_path)
    log_path.parent.mkdir(parents=True, exist_ok=True)

    logger = logging.getLogger("riskdash")
    logger.setLevel(getattr(logging, level.upper(), logging.INFO))
    logger.handlers.clear()

    fmt = logging.Formatter(
        "%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S%z",
    )

    file_h = RotatingFileHandler(log_path, maxBytes=5_000_000, backupCount=5, encoding="utf-8")
    file_h.setFormatter(fmt)
    file_h.addFilter(_RedactFilter())

    stream_h = logging.StreamHandler()
    stream_h.setFormatter(fmt)
    stream_h.addFilter(_RedactFilter())

    logger.addHandler(file_h)
    logger.addHandler(stream_h)
    logger.propagate = False
    return logger


def get_logger() -> logging.Logger:
    return logging.getLogger("riskdash")
