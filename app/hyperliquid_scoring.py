"""可解释、版本化的 Hyperliquid 方向评分与钱包画像。

这里不调用 market_engine.score：后者是风险事件严重度累加器，并不支持
有符号连续特征。本模块只接收已经经过 PIT 数据门禁的数值。
"""
from __future__ import annotations

import hashlib
import json
import math
import statistics
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable

from .hyperliquid_client import as_decimal

RULES_VERSION = "hl-chain-v1/2026-08"
REDUCED_RULES_VERSION = "hl-chain-reduced-noctx-v1/2026-08"
POLICY_VERSION = "hl-policy-v1/2026-08"
ENTER_THRESHOLD = Decimal("60")
EXIT_THRESHOLD = Decimal("40")
HIGH_THRESHOLD = Decimal("75")

MODEL_WEIGHTS: dict[tuple[str, str], dict[str, Decimal]] = {
    ("INTRADAY", "OFF_RTH"): {
        "off_rth_oracle_return": Decimal("0.25"),
        "qualified_wallet_aggressor_imbalance": Decimal("0.25"),
        "qualified_wallet_net_exposure": Decimal("0.35"),
        "wallet_breadth": Decimal("0.15"),
    },
    ("INTRADAY", "RTH"): {
        "qualified_wallet_aggressor_imbalance": Decimal("0.35"),
        "qualified_wallet_net_exposure": Decimal("0.45"),
        "wallet_breadth": Decimal("0.20"),
    },
    ("SWING", "OFF_RTH"): {
        "five_day_off_rth_oracle_return": Decimal("0.15"),
        "one_day_qualified_wallet_aggressor_imbalance": Decimal("0.15"),
        "five_day_qualified_wallet_net_exposure": Decimal("0.45"),
        "three_day_wallet_breadth_persistence": Decimal("0.25"),
    },
}

# 无AWS历史锁定轨：方向只使用成交K线和PIT钱包流。
# fundingHistory的funding rate/premium index仅用于质量诊断和事后消融，
# 不单独贡献方向分。
# hl_trade_return明确是成交价收益，不是oracle return；该profile永不自动晋级为
# FULL_CTX，未来拿到asset_ctxs也必须新建完整模型validation commitment。
REDUCED_MODEL_WEIGHTS: dict[tuple[str, str], dict[str, Decimal]] = {
    ("INTRADAY", "OFF_RTH"): {
        "hl_trade_return_5m": Decimal("0.25"),
        "qualified_wallet_aggressor_imbalance": Decimal("0.30"),
        "qualified_wallet_net_exposure": Decimal("0.35"),
        "wallet_breadth": Decimal("0.10"),
    },
    ("INTRADAY", "RTH"): {
        "hl_trade_return_5m": Decimal("0.20"),
        "qualified_wallet_aggressor_imbalance": Decimal("0.30"),
        "qualified_wallet_net_exposure": Decimal("0.40"),
        "wallet_breadth": Decimal("0.10"),
    },
    ("SWING", "OFF_RTH"): {
        "five_day_hl_trade_return": Decimal("0.20"),
        "one_day_qualified_wallet_aggressor_imbalance": Decimal("0.20"),
        "five_day_qualified_wallet_net_exposure": Decimal("0.40"),
        "three_day_wallet_breadth_persistence": Decimal("0.20"),
    },
}


def _d(value: Any) -> Decimal | None:
    return as_decimal(value)


def _clamp(value: Decimal, low: Decimal, high: Decimal) -> Decimal:
    return max(low, min(high, value))


def rules_hash(model: str, regime: str, profile: str = "FULL_CTX") -> str:
    reduced = str(profile).upper() == "REDUCED_NO_CTX"
    weights = (REDUCED_MODEL_WEIGHTS if reduced else MODEL_WEIGHTS).get((model, regime), {})
    version = REDUCED_RULES_VERSION if reduced else RULES_VERSION
    material = json.dumps({"version": version, "profile": profile, "model": model, "regime": regime,
                           "weights": {k: str(v) for k, v in weights.items()},
                           "thresholds": [str(ENTER_THRESHOLD), str(EXIT_THRESHOLD),
                                          str(HIGH_THRESHOLD)]}, sort_keys=True)
    return hashlib.sha256(material.encode()).hexdigest()[:16]


def robust_standardize(value: Any, history: Iterable[Any]) -> Decimal | None:
    """截止当前时点用过去窗口的中位数/MAD，MAD=0 回退 IQR。"""
    v = _d(value)
    vals = sorted(x for x in (_d(x) for x in history) if x is not None)
    if v is None or len(vals) < 5:
        return None
    median = Decimal(str(statistics.median([float(x) for x in vals])))
    deviations = [abs(x - median) for x in vals]
    mad = Decimal(str(statistics.median([float(x) for x in deviations])))
    if mad > 0:
        z = (v - median) / (mad * Decimal("1.4826"))
    else:
        q1, q3 = statistics.quantiles([float(x) for x in vals], n=4, method="inclusive")[0], statistics.quantiles([float(x) for x in vals], n=4, method="inclusive")[2]
        iqr = Decimal(str(q3 - q1))
        if iqr <= 0:
            return None
        z = (v - median) / iqr
    return _clamp(z, Decimal("-3"), Decimal("3")) / Decimal("3")


def signed_score(features: dict[str, Any], model: str, regime: str,
                 profile: str = "FULL_CTX") -> dict[str, Any]:
    """所有模型特征齐全才评分，不对缺失特征偷偷重归一。"""
    reduced = str(profile).upper() == "REDUCED_NO_CTX"
    weights = (REDUCED_MODEL_WEIGHTS if reduced else MODEL_WEIGHTS).get((model, regime))
    if not weights:
        return {"score": None, "quality_status": "BLOCKED", "missing": ["model_regime"],
                "profile": profile,
                "rules_version": REDUCED_RULES_VERSION if reduced else RULES_VERSION}
    missing = [name for name in weights if _d(features.get(name)) is None]
    if missing:
        return {"score": None, "quality_status": "LIMITED", "missing": missing,
                "components": {name: features.get(name) for name in weights},
                "profile": profile,
                "rules_version": REDUCED_RULES_VERSION if reduced else RULES_VERSION}
    components = {name: _clamp(_d(features[name]) or Decimal("0"), Decimal("-1"), Decimal("1"))
                  for name in weights}
    score = sum((components[name] * weight * Decimal("100") for name, weight in weights.items()),
                Decimal("0"))
    return {"score": score.quantize(Decimal("0.01")), "quality_status": "READY",
            "components": {k: str(v) for k, v in components.items()}, "missing": [],
            "profile": profile,
            "rules_version": REDUCED_RULES_VERSION if reduced else RULES_VERSION}


def transition(previous: str | None, score: Decimal | None,
               quality_status: str = "READY") -> str:
    if quality_status != "READY" or score is None:
        return "BLOCKED" if quality_status == "BLOCKED" else "EXPIRED"
    if previous == "BULLISH":
        if score <= -ENTER_THRESHOLD:
            return "BEARISH"
        if score < EXIT_THRESHOLD:
            return "NEUTRAL"
        return "BULLISH"
    if previous == "BEARISH":
        if score >= ENTER_THRESHOLD:
            return "BULLISH"
        if score > -EXIT_THRESHOLD:
            return "NEUTRAL"
        return "BEARISH"
    if score >= ENTER_THRESHOLD:
        return "BULLISH"
    if score <= -ENTER_THRESHOLD:
        return "BEARISH"
    return "NEUTRAL"


def confidence(score: Decimal | None, *, quality_status: str,
               qualified_wallets: int = 0, coverage: Decimal | None = None,
               ctx_age_seconds: int | None = None) -> str:
    if quality_status != "READY" or score is None:
        return "LOW"
    if abs(score) < HIGH_THRESHOLD or qualified_wallets < 5:
        return "MEDIUM"
    if coverage is not None and coverage < Decimal("0.95"):
        return "MEDIUM"
    if ctx_age_seconds is not None and ctx_age_seconds > 600:
        return "MEDIUM"
    return "HIGH"


def wallet_classification(*, maker_ratio: Any, median_hold_minutes: Any,
                          bilateral_balance: Any) -> str:
    maker, hold, balance = _d(maker_ratio), _d(median_hold_minutes), _d(bilateral_balance)
    if maker is None or hold is None or balance is None:
        return "INSUFFICIENT"
    if maker >= Decimal("0.80") and hold <= Decimal("60") and balance >= Decimal("0.70"):
        return "LIQUIDITY"
    return "DIRECTIONAL" if maker < Decimal("0.50") else "MIXED"


def winsorize(values: Iterable[Any], lower: Decimal = Decimal("0.05"),
              upper: Decimal = Decimal("0.95")) -> list[Decimal]:
    vals = sorted(x for x in (_d(x) for x in values) if x is not None)
    if len(vals) < 2:
        return vals
    lo_idx = max(0, min(len(vals) - 1, math.floor((len(vals) - 1) * float(lower))))
    hi_idx = max(0, min(len(vals) - 1, math.ceil((len(vals) - 1) * float(upper))))
    lo, hi = vals[lo_idx], vals[hi_idx]
    return [_clamp(x, lo, hi) for x in vals]


def wallet_quality(metrics: dict[str, Any], peer_metrics: list[dict[str, Any]] | None = None
                   ) -> dict[str, Any]:
    """计算可解释质量分；不足样本和无定义比率不伪造高分。"""
    peer_metrics = peer_metrics or []
    cycles = int(metrics.get("completed_cycles") or 0)
    active_days = int(metrics.get("active_days") or 0)
    notional = _d(metrics.get("notional_90d")) or Decimal("0")
    coverage = _d(metrics.get("coverage"))
    if cycles < 20 or active_days < 20 or notional < Decimal("100000") \
            or coverage is None or coverage < Decimal("0.90"):
        return {"eligible": False, "quality_score": None, "weight": None,
                "reason": "样本或覆盖率不足"}
    if metrics.get("classification") == "LIQUIDITY":
        return {"eligible": False, "quality_score": None, "weight": None,
                "reason": "LIQUIDITY 钱包不进入方向池"}
    fields = ("profit_factor", "payoff_ratio", "net_pnl_over_notional",
              "drawdown_inverse", "profitable_week_ratio")
    values = {field: _d(metrics.get(field)) for field in fields}
    if any(values[field] is None for field in fields):
        return {"eligible": False, "quality_score": None, "weight": None,
                "reason": "质量指标不可计算"}
    # 百分位只有在同类池足够大时才稳定，否则使用透明原值归一化并标注低置信度。
    peer_ok = len(peer_metrics) >= 20
    components: dict[str, Decimal] = {}
    for field in fields:
        peers = [_d(p.get(field)) for p in peer_metrics]
        peers = [p for p in peers if p is not None]
        if peer_ok and peers:
            rank = sum(1 for p in peers if p <= values[field]) / Decimal(len(peers))
            components[field] = Decimal(str(rank))
        else:
            # 质量分仍可展示，但不声称稳定百分位。
            components[field] = _clamp(values[field], Decimal("-1"), Decimal("1")) \
                if field in {"net_pnl_over_notional", "drawdown_inverse"} \
                else _clamp(values[field] / (Decimal("2") if field == "profit_factor" else Decimal("5")),
                           Decimal("0"), Decimal("1"))
    weighted = (components["profit_factor"] * Decimal("0.25")
                + components["payoff_ratio"] * Decimal("0.20")
                + components["net_pnl_over_notional"] * Decimal("0.20")
                + components["drawdown_inverse"] * Decimal("0.20")
                + components["profitable_week_ratio"] * Decimal("0.15"))
    sample_conf = min(Decimal("1"), Decimal(str(math.sqrt(cycles / 30))))
    weight = weighted * sample_conf * coverage
    return {"eligible": True, "quality_score": weighted, "sample_confidence": sample_conf,
            "weight": weight, "percentile_stable": peer_ok,
            "components": {k: str(v) for k, v in components.items()}}


@dataclass(frozen=True)
class ChainSignal:
    signal_id: str
    model: str
    regime: str
    coin: str
    state: str
    score: Decimal | None
    confidence: str
    quality_status: str
    quality_reasons: tuple[str, ...]
    rules_version: str = RULES_VERSION
