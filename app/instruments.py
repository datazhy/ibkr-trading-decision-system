"""证券分类机制。

为什么需要它：板块集中度（R2）与「未知分类占比」规则都依赖分类，而原先只有一份
手写的 sectors.json —— 每买一只新标的就多一份「未知」，于是规则报的不是真实风险，
而是「我们还没维护映射」。这是典型的假警报，会训练用户忽略告警。

分类来源优先级（每条记录都留痕 source + confidence）：
    1. 人工覆盖  override      —— 最权威，可审计
    2. IBKR 元数据 ibkr        —— reqContractDetails 自带 industry/category/longName
    3. 遗留映射  sectors_json  —— 保留兼容，逐步被 1/2 取代
    4. 无法识别  unknown       —— **必须写明原因**，不假装知道

刷新周期：分类信息变化极慢（公司改名、ETF 重分类都是年级别事件），
默认 7 天刷新一次，绝不每分钟去问券商。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from .logging_setup import get_logger

log = get_logger()

SOURCE_OVERRIDE = "override"
SOURCE_IBKR = "ibkr"
SOURCE_SECTORS_JSON = "sectors_json"
SOURCE_UNKNOWN = "unknown"

CONF_HIGH = "high"
CONF_MEDIUM = "medium"
CONF_LOW = "low"
CONF_NONE = "none"

#: 资产类别归一
ASSET_EQUITY = "equity"
ASSET_ETF = "etf"
ASSET_INDEX = "index"
ASSET_CASH = "cash"
ASSET_BOND = "bond"
ASSET_OPTION = "option"
ASSET_FUTURE = "future"
ASSET_UNKNOWN = "unknown"

_SECTYPE_TO_ASSET = {
    "STK": ASSET_EQUITY, "ETF": ASSET_ETF, "IND": ASSET_INDEX,
    "CASH": ASSET_CASH, "BOND": ASSET_BOND, "OPT": ASSET_OPTION,
    "FUT": ASSET_FUTURE, "FOP": ASSET_OPTION,
}

#: 元数据刷新周期（天）。分类信息变化极慢，无需频繁请求。
REFRESH_DAYS = 7


@dataclass
class Classification:
    symbol: str
    conid: int | None = None
    sec_type: str | None = None
    company_name: str | None = None
    sector: str | None = None
    industry: str | None = None
    asset_class: str = ASSET_UNKNOWN
    source: str = SOURCE_UNKNOWN
    confidence: str = CONF_NONE
    unknown_reason: str | None = None

    @property
    def is_known(self) -> bool:
        return self.sector is not None and self.source != SOURCE_UNKNOWN

    def as_dict(self) -> dict:
        return {
            "symbol": self.symbol, "conid": self.conid, "sec_type": self.sec_type,
            "company_name": self.company_name, "sector": self.sector,
            "industry": self.industry, "asset_class": self.asset_class,
            "source": self.source, "confidence": self.confidence,
            "unknown_reason": self.unknown_reason,
        }


def asset_class_of(sec_type: str | None, stock_type: str | None = None) -> str:
    """IBKR secType/stockType -> 归一资产类别。

    IBKR 把 ETF 也报成 STK，要靠 stockType 才能区分 —— 这正是「ETF、现金、债券、
    期权和普通股票必须区分」这条要求的落点。
    """
    st = (stock_type or "").upper()
    if st in ("ETF", "ETN", "FUND", "CLOSED-END FUND"):
        return ASSET_ETF
    return _SECTYPE_TO_ASSET.get((sec_type or "").upper(), ASSET_UNKNOWN)


def classify_from_ibkr(detail: Any) -> Classification | None:
    """从 IBKR ContractDetails 提取分类。取不到关键字段返回 None。"""
    try:
        c = getattr(detail, "contract", None)
        symbol = getattr(c, "symbol", None)
        if not symbol:
            return None
        industry = getattr(detail, "industry", None) or None
        category = getattr(detail, "category", None) or None
        subcategory = getattr(detail, "subcategory", None) or None
        stock_type = getattr(detail, "stockType", None)
        sec_type = getattr(c, "secType", None)
        asset = asset_class_of(sec_type, stock_type)
        # 板块取 category（IBKR 的中层分类，粒度最接近「板块」概念）；
        # 缺失时退到 industry。ETF 没有行业分类，用资产类别顶上。
        sector = category or industry
        if sector is None and asset == ASSET_ETF:
            sector = "ETF"
        if sector is None:
            return None
        return Classification(
            symbol=symbol, conid=getattr(c, "conId", None), sec_type=sec_type,
            company_name=getattr(detail, "longName", None) or None,
            sector=sector, industry=subcategory or industry, asset_class=asset,
            source=SOURCE_IBKR,
            # IBKR 自己给的分类可信度高；仅靠 ETF 兜底的记 medium
            confidence=CONF_HIGH if category or industry else CONF_MEDIUM)
    except Exception:  # noqa: BLE001 — 分类失败绝不能拖垮风险计算
        return None


def resolve(symbol: str, *, override: dict | None = None, ibkr_row: dict | None = None,
            legacy_sector: str | None = None, conid: int | None = None) -> Classification:
    """按优先级合成最终分类。"""
    if override:
        return Classification(
            symbol=symbol, conid=conid, sec_type=(ibkr_row or {}).get("sec_type"),
            company_name=override.get("company_name") or (ibkr_row or {}).get("company_name"),
            sector=override.get("sector"), industry=override.get("industry"),
            asset_class=override.get("asset_class") or (ibkr_row or {}).get("asset_class")
            or ASSET_UNKNOWN,
            source=SOURCE_OVERRIDE, confidence=CONF_HIGH)
    if ibkr_row and ibkr_row.get("sector"):
        return Classification(
            symbol=symbol, conid=ibkr_row.get("conid") or conid,
            sec_type=ibkr_row.get("sec_type"), company_name=ibkr_row.get("company_name"),
            sector=ibkr_row.get("sector"), industry=ibkr_row.get("industry"),
            asset_class=ibkr_row.get("asset_class") or ASSET_UNKNOWN,
            source=ibkr_row.get("source") or SOURCE_IBKR,
            confidence=ibkr_row.get("confidence") or CONF_HIGH)
    if legacy_sector:
        return Classification(symbol=symbol, conid=conid, sector=legacy_sector,
                              asset_class=ASSET_EQUITY, source=SOURCE_SECTORS_JSON,
                              confidence=CONF_MEDIUM)
    return Classification(
        symbol=symbol, conid=conid, source=SOURCE_UNKNOWN, confidence=CONF_NONE,
        unknown_reason="IBKR 未返回行业分类，且无人工覆盖与遗留映射")


def needs_refresh(updated_at: str | None, now: datetime,
                  days: int = REFRESH_DAYS) -> bool:
    if not updated_at:
        return True
    try:
        return (now - datetime.fromisoformat(updated_at)) > timedelta(days=days)
    except (TypeError, ValueError):
        return True


def classify_positions(db, symbols: list[str], *, legacy_sector_map: dict | None = None,
                       conids: dict[str, int] | None = None) -> dict[str, Classification]:
    """对一批标的做分类。纯读库，不发外部请求 —— 供风险计算每分钟调用。"""
    if db is None:      # 无库上下文（纯函数测试）：退化到遗留映射
        overrides, meta = {}, {}
    else:
        overrides = db.classification_overrides()
        meta = db.instrument_metadata(symbols)
    legacy = legacy_sector_map or {}
    out = {}
    for sym in symbols:
        out[sym] = resolve(sym, override=overrides.get(sym), ibkr_row=meta.get(sym),
                           legacy_sector=legacy.get(sym),
                           conid=(conids or {}).get(sym))
    return out


def refresh_metadata(db, client, symbols: list[str], now: datetime | None = None,
                     *, force: bool = False) -> int:
    """按需从 IBKR 刷新元数据。只补「没有或已过期」的，不做无谓请求。

    任何异常都被吞掉并返回 0 —— 分类是可降级能力，绝不允许它影响账户同步。
    """
    now = now or datetime.now(timezone.utc)
    try:
        existing = db.instrument_metadata(symbols)
        todo = [s for s in symbols
                if force or needs_refresh((existing.get(s) or {}).get("updated_at"), now)]
        if not todo:
            return 0
        rows = client.fetch_contract_details(todo)
        # 取不到的也要落一条 unknown 记录并写明原因，避免每分钟重试
        for s in todo:
            if s not in rows:
                rows[s] = {**resolve(s).as_dict(),
                           "unknown_reason": "IBKR reqContractDetails 未返回该标的的行业分类"}
        db.upsert_instrument_metadata(list(rows.values()))
        log.info("证券元数据已刷新 %d 个标的", len(rows))
        return len(rows)
    except Exception:  # noqa: BLE001 — 分类失败不得阻断风险计算
        log.warning("证券元数据刷新失败（不影响账户同步与风险计算）", exc_info=True)
        return 0
