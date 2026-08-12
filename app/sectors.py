"""加载板块映射表：板块 -> [标的]，反转为 标的 -> 板块（供 R2 使用）。"""
from __future__ import annotations

import json
from pathlib import Path

from .logging_setup import get_logger

log = get_logger()


def load_sector_map(path: str | Path) -> dict[str, str]:
    """返回 {symbol: sector}。文件缺失或格式异常时返回空表（R2 静默跳过）。"""
    p = Path(path)
    if not p.exists():
        log.info("板块映射表不存在（%s），R2 将跳过", p)
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        log.error("板块映射表解析失败 %s: %s", p, exc)
        return {}

    sectors = data.get("sectors", data)
    out: dict[str, str] = {}
    for sector, symbols in sectors.items():
        if sector.startswith("_") or not isinstance(symbols, list):
            continue
        for sym in symbols:
            out[str(sym).upper()] = sector
    return out
