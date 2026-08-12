"""Small, source-aware helpers for the IBKR PnL reliability boundary.

This module deliberately contains no broker calls.  Keeping the acceptance and
reconciliation rules pure makes it possible to test the dangerous cases (large
but legitimate PnL, stale callbacks, source switches) without a Gateway.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any


def decimal_or_none(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        value = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    if not value.is_finite():
        return None
    return value


@dataclass(frozen=True)
class PnLSubscription:
    request_id: str
    account: str
    model_code: str
    conid: int | None
    connection_generation: int
    subscription_generation: int
    subscribed_at: str


@dataclass(frozen=True)
class CallbackDecision:
    accepted: bool
    reasons: tuple[str, ...] = ()


def validate_callback(meta: PnLSubscription | None, *, current_account: str,
                      current_connection_generation: int,
                      callback_account: str | None = None,
                      callback_model_code: str | None = None,
                      callback_request_id: str | None = None) -> CallbackDecision:
    """Reject only structural/lifecycle errors, never a large dollar amount."""
    reasons: list[str] = []
    if meta is None:
        reasons.append("UNKNOWN_SUBSCRIPTION")
    else:
        if meta.connection_generation != current_connection_generation:
            reasons.append("STALE_CONNECTION_GENERATION")
        if meta.account != current_account or callback_account not in (None, "", meta.account):
            reasons.append("ACCOUNT_MISMATCH")
        if callback_model_code not in (None, "", meta.model_code):
            reasons.append("MODEL_CODE_MISMATCH")
        if callback_request_id not in (None, "", meta.request_id):
            reasons.append("REQ_ID_MISMATCH")
    return CallbackDecision(not reasons, tuple(reasons))


def classify_official_reconciliation(account_daily: Decimal | None,
                                     position_daily_total: Decimal | None,
                                     *, tolerance: Decimal = Decimal("5")) -> str:
    if account_daily is None or position_daily_total is None:
        return "insufficient"
    if abs(account_daily - position_daily_total) <= max(
            tolerance, abs(account_daily) * Decimal("0.01")):
        return "consistent"
    return "explainable_timing_difference"


def classify_futu_reconciliation(*, quote_coverage: Decimal,
                                 quote_age_seconds: Decimal | None,
                                 official_daily: Decimal | None,
                                 futu_mtm: Decimal | None) -> str:
    if official_daily is None or futu_mtm is None or quote_coverage < Decimal("1"):
        return "source_stale"
    if quote_age_seconds is not None and quote_age_seconds > Decimal("300"):
        return "source_stale"
    if official_daily == 0 and futu_mtm == 0:
        return "consistent"
    # Quotes and official PnL are intentionally not required to match dollar for
    # dollar (reset time, FX, commissions, interest, and fills differ).
    if official_daily * futu_mtm >= 0:
        return "explainable_accounting_difference"
    return "investigation_required"


def pnl_quality(*, callback_decision: CallbackDecision,
                age_seconds: Decimal | None,
                stale_after_seconds: Decimal = Decimal("180"),
                source: str = "IBKR_reqPnL") -> tuple[str, list[str]]:
    reasons = list(callback_decision.reasons)
    if age_seconds is None:
        reasons.append("NO_ACCEPTED_CALLBACK")
    elif age_seconds > stale_after_seconds:
        reasons.append("STALE_CALLBACK")
    if source.startswith("DERIVED"):
        reasons.append("DERIVED_NOT_AUTHORITATIVE")
    return ("OK" if not reasons else "DEGRADED"), reasons


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
