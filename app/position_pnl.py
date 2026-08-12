"""当前持仓的价格重估与历史已实现盈亏聚合。

持仓快照里的 ``market_value``/``upl`` 是 IBKR portfolio 事件的时点值，不能在
富途行情更新后继续当作当前值。这里仅用 IBKR 的数量、均价和现价做展示层重估，
不回写原始快照。

已实现盈亏优先采用 executions 中 IBKR CommissionReport 的逐成交值；缺少券商值
的成交才用 FIFO 成本法补算，并把手续费计入成本或平仓结果。当前持仓页按成交
身份与净持仓周期归属，不把 Flex 的全历史汇总跨周期带入新建仓。
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation


ZERO = Decimal("0")


def dec(value) -> Decimal | None:
    """把数据库/API 值安全转为 Decimal；空值保持 None。"""
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None


def valid_price(value) -> Decimal | None:
    """返回可用于估值的正价格；空值、非数字和 0 均视为不可用。"""
    value = dec(value)
    if value is None or not value.is_finite():
        return None
    return value if value > 0 else None


def revalue_position(position: dict, *, price=None, source: str | None = None) -> bool:
    """按 ``数量 × (现价 - IBKR 持仓均价)`` 重估一只持仓。

    返回 False 表示输入不完整；此时调用方应保留已有有效值，绝不填 0。
    """
    candidate = position.get("price") if price is None else price
    px = valid_price(candidate)
    # 调用方传入了空/0 行情时，保留 position 中已有的有效价格。
    if px is None and price is not None:
        px = valid_price(position.get("price"))
    qty = dec(position.get("qty"))
    avg = dec(position.get("avg_cost"))
    if px is None or px <= 0 or qty is None or avg is None:
        return False
    position["price"] = px
    position["market_value"] = qty * px
    position["upl"] = qty * (px - avg)
    source_name = source or position.get("source") or "QUOTE"
    if str(source_name).upper().startswith("FUTU"):
        position["market_value_source"] = "FUTU_price_x_IBKR_qty"
        position["upl_source"] = "FUTU_price_x_IBKR_cost"
    else:
        position["market_value_source"] = "QUOTE_price_x_IBKR_qty"
        position["upl_source"] = "QUOTE_price_x_IBKR_cost"
    return True


def _signed_side(side: str | None) -> int:
    value = str(side or "").upper()
    if value in {"BUY", "BOT", "BUY_TO_COVER"}:
        return 1
    if value in {"SELL", "SLD", "SELL_SHORT"}:
        return -1
    return 0


@dataclass(frozen=True)
class RealizedResult:
    value: Decimal | None
    source: str | None
    complete: bool
    rows: int
    # The following fields describe the *current* position cycle.  They are
    # optional so callers that use the historical aggregate keep the old API.
    cycle_id: int | None = None
    cycle_start: str | None = None
    cycle_end: str | None = None
    cycle_qty: Decimal | None = None
    current_cycle: bool = False


def aggregate_realized(rows) -> RealizedResult:
    """按成交顺序累计单标的已实现盈亏。

    ``realized_pnl`` 是券商事实来源；没有它时，使用 FIFO lot 成本法。买入佣金
    加入 lot 成本，卖出/平仓佣金从本次已实现盈亏扣除。跨零交易会先平旧方向，
    再以剩余数量建立新 lot。
    """
    ordered = sorted(list(rows or []), key=lambda r: str(
        r.get("execution_time") if isinstance(r, dict) else r["execution_time"]))
    if not ordered:
        return RealizedResult(None, None, True, 0)

    lots: deque[dict] = deque()  # {qty: signed Decimal, cost: net price/share}
    total = ZERO
    saw_value = False
    broker_values = 0
    derived_values = 0
    complete = True

    for row in ordered:
        get = row.get if isinstance(row, dict) else row.__getitem__
        broker = dec(get("realized_pnl"))
        shares = dec(get("shares"))
        price = dec(get("price"))
        side = _signed_side(get("side"))
        fee = dec(get("commission"))
        if fee is not None:
            fee = abs(fee)

        # CommissionReport 已给出事实值时，即使本地 lot 信息不完整也不能丢掉。
        if broker is not None:
            total += broker
            saw_value = True
            broker_values += 1

        if shares is None or shares <= 0 or price is None or price <= 0 or side == 0:
            if broker is None:
                complete = False
            continue

        remaining = shares
        derived = ZERO
        closed = False
        while remaining > 0 and lots and (lots[0]["qty"] * side) < 0:
            lot = lots[0]
            close_qty = min(remaining, abs(lot["qty"]))
            closed = True
            # 对部分成交按数量分摊本笔手续费；手续费缺失时不能伪造完整净值。
            close_fee = (fee * close_qty / shares) if fee is not None else ZERO
            if fee is None:
                complete = False
            if lot["qty"] > 0:       # 平多：卖价 - 成本
                derived += (price - lot["cost"]) * close_qty - close_fee
            else:                    # 平空：成本 - 买价
                derived += (lot["cost"] - price) * close_qty - close_fee
            lot["qty"] += Decimal(side) * close_qty
            remaining -= close_qty
            if lot["qty"] == 0:
                lots.popleft()

        if broker is None and closed:
            total += derived
            saw_value = True
            derived_values += 1

        # 剩余部分建立反向新 lot。开仓佣金进入 lot 成本，避免以后平仓时漏算。
        if remaining > 0:
            open_fee = (fee * remaining / shares) if fee is not None else ZERO
            if fee is None:
                complete = False
            unit_cost = (price + open_fee / remaining if side > 0
                         else price - open_fee / remaining)
            lots.append({"qty": Decimal(side) * remaining, "cost": unit_cost})

    # 只有买入/建仓记录时，历史已实现盈亏是确定的 0，而不是缺失。
    if not saw_value:
        total = ZERO
    source = ("IBKR executions.realized_pnl" if broker_values and not derived_values
              else "FIFO executions + commissions")
    return RealizedResult(total, source, complete, len(ordered))


# ---------------------------------------------------------------------------
# Current-position-cycle attribution
# ---------------------------------------------------------------------------

# ``executions.exchange`` is the execution venue (NASDAQ/DARK/OVERNIGHT), not
# a different security.  When conid is present it is the stable instrument
# identity and venue must not split one position into several cycles.  Account
# is retained when a source supplies it; the current executions table predates
# that column and therefore relies on the configured single account.
def _get(row, key):
    if isinstance(row, dict):
        return row.get(key)
    try:
        return row[key]
    except (KeyError, IndexError, TypeError):
        return None


def _identity_value(value, *, upper: bool = False) -> str | None:
    if value is None or str(value).strip() == "":
        return None
    value = str(value).strip()
    return value.upper() if upper else value


def execution_identity_key(row) -> tuple[str | None, ...]:
    """Return the instrument/account identity used for cycle grouping.

    Conid + security type + currency prevent same-ticker instruments from
    being merged.  ``exchange`` is intentionally absent when conid exists:
    IBKR reports the same stock executed on several venues, and those fills
    belong to one position.  If conid is unavailable, symbol remains the
    fallback identity and callers must reject an ambiguous multi-conid set.
    """
    return (
        _identity_value(_get(row, "account"), upper=True),
        _identity_value(_get(row, "symbol"), upper=True),
        _identity_value(_get(row, "conid")),
        _identity_value(_get(row, "sec_type"), upper=True),
        _identity_value(_get(row, "currency"), upper=True),
        _identity_value(_get(row, "multiplier")),
        _identity_value(_get(row, "instrument_id")),
    )


def _valid_execution_row(row) -> bool:
    """Ignore superseded/cancelled rows while tolerating old test fixtures."""
    current = _get(row, "is_current")
    if current is not None and str(current).lower() not in {"1", "true", "yes"}:
        return False
    status = _identity_value(_get(row, "data_status"), upper=True)
    return status not in {"CANCELLED", "CANCELED", "REJECTED", "ERROR", "INVALID"}


def executions_for_position(rows, position, *, account: str | None = None) -> list:
    """Select executions that can belong to one current position.

    A missing conid in the position is allowed only when all matching fills
    have one unique conid; multiple conids for the same ticker are treated as
    ambiguous rather than silently merged.  Unknown optional fields do not
    create a false mismatch, but a known conflict is always excluded.
    """
    symbol = _identity_value(_get(position, "symbol"), upper=True)
    if not symbol:
        return []
    candidates = [r for r in (rows or [])
                  if _valid_execution_row(r)
                  and _identity_value(_get(r, "symbol"), upper=True) == symbol]
    if not candidates:
        return []

    pos_conid = _identity_value(_get(position, "conid"))
    if pos_conid is not None:
        candidates = [r for r in candidates
                      if _identity_value(_get(r, "conid")) == pos_conid]
    else:
        conids = {_identity_value(_get(r, "conid")) for r in candidates}
        conids.discard(None)
        if len(conids) > 1:
            return []

    expected_account = _identity_value(account or _get(position, "account"),
                                       upper=True)
    optional_fields = ("sec_type", "currency", "multiplier", "instrument_id")
    filtered = []
    for row in candidates:
        row_account = _identity_value(_get(row, "account"), upper=True)
        if expected_account and row_account and row_account != expected_account:
            continue
        mismatch = False
        for field in optional_fields:
            expected = _identity_value(_get(position, field),
                                       upper=field in {"sec_type", "currency"})
            actual = _identity_value(_get(row, field),
                                     upper=field in {"sec_type", "currency"})
            if expected is not None and actual is not None and expected != actual:
                mismatch = True
                break
        if not mismatch:
            filtered.append(row)
    return filtered


@dataclass(frozen=True)
class PositionCycle:
    cycle_id: int
    start: str | None
    end: str | None
    direction: str
    buys: Decimal
    sells: Decimal
    final_qty: Decimal
    realized: Decimal
    complete: bool
    broker_rows: int
    derived_rows: int
    current: bool


def _cycle_direction(sign: int) -> str:
    return "LONG" if sign > 0 else "SHORT"


def rebuild_position_cycles(rows, *, initial_qty=None, initial_cost=None) -> list[PositionCycle]:
    """Rebuild signed position cycles from valid executions in time order.

    A cycle starts at zero -> non-zero and ends at non-zero -> zero.  A trade
    crossing zero is split into the closing portion of the old cycle and the
    opening portion of the new one.  Broker ``realized_pnl`` is accepted only
    when the trade actually closes an existing lot; a non-zero value attached
    to a buy-only opening fill can therefore never leak into the new cycle.
    """
    ordered = sorted(
        [r for r in (rows or []) if _valid_execution_row(r)],
        key=lambda r: str(_get(r, "execution_time") or ""),
    )
    lots: deque[dict] = deque()
    states: list[dict] = []
    active: dict | None = None
    next_cycle_id = 1

    def lot_qty() -> Decimal:
        return sum((lot["qty"] for lot in lots), ZERO)

    def new_cycle(sign: int, ts: str | None) -> dict:
        nonlocal next_cycle_id
        state = {
            "cycle_id": next_cycle_id,
            "start": ts,
            "end": None,
            "direction": _cycle_direction(sign),
            "buys": ZERO,
            "sells": ZERO,
            "realized": ZERO,
            "complete": True,
            "broker_rows": 0,
            "derived_rows": 0,
            "has_broker": False,
            "has_derived": False,
            "final_qty": ZERO,
        }
        next_cycle_id += 1
        states.append(state)
        return state

    def add_trade(state: dict, sign: int, qty: Decimal) -> None:
        if sign > 0:
            state["buys"] += qty
        else:
            state["sells"] += qty

    # The execution retention window can start after a position was already
    # open.  When the caller supplies the live quantity, seed the book with an
    # explicitly *inferred* baseline so a broker realizedPNL on a partial sell
    # (for example MRVL's 7-share sale) is not misclassified as opening a short.
    # The unknown lot cost makes FIFO incomplete, but broker values remain
    # authoritative for the closing fills that carry them.
    seed_qty = dec(initial_qty)
    if seed_qty is not None and seed_qty != ZERO:
        active = new_cycle(1 if seed_qty > ZERO else -1, None)
        active["complete"] = False
        lots.append({"qty": seed_qty, "cost": dec(initial_cost)})

    for row in ordered:
        side = _signed_side(_get(row, "side"))
        shares = dec(_get(row, "shares"))
        price = dec(_get(row, "price"))
        ts = _get(row, "execution_time")
        if side == 0 or shares is None or shares <= 0 or price is None or price <= 0:
            if active is not None:
                active["complete"] = False
            continue

        pre_qty = lot_qty()
        if pre_qty == ZERO:
            active = new_cycle(side, str(ts) if ts is not None else None)
            close_qty = ZERO
            open_qty = shares
            add_trade(active, side, open_qty)
        elif pre_qty * Decimal(side) > 0:
            if active is None:
                active = new_cycle(1 if pre_qty > 0 else -1,
                                   str(ts) if ts is not None else None)
            close_qty = ZERO
            open_qty = shares
            add_trade(active, side, open_qty)
        else:
            if active is None:
                active = new_cycle(1 if pre_qty > 0 else -1,
                                   str(ts) if ts is not None else None)
            close_qty = min(shares, abs(pre_qty))
            open_qty = shares - close_qty
            add_trade(active, side, close_qty)

        fee = dec(_get(row, "commission"))
        fee = abs(fee) if fee is not None else None
        fee_missing = fee is None
        broker = dec(_get(row, "realized_pnl"))
        remaining = shares
        derived = ZERO
        actual_closed = ZERO
        close_cost_known = True
        while remaining > ZERO and lots and (lots[0]["qty"] * side) < 0:
            lot = lots[0]
            take = min(remaining, abs(lot["qty"]))
            actual_closed += take
            close_fee = (fee * take / shares) if fee is not None else ZERO
            if fee is None:
                active["complete"] = False
            if lot["cost"] is None:
                active["complete"] = False
                close_cost_known = False
            elif lot["qty"] > ZERO:
                derived += (price - lot["cost"]) * take - close_fee
            else:
                derived += (lot["cost"] - price) * take - close_fee
            lot["qty"] += Decimal(side) * take
            remaining -= take
            if lot["qty"] == ZERO:
                lots.popleft()

        # If the trade opened a new lot, its opening fee belongs to that lot.
        if remaining > ZERO:
            if fee is None:
                active["complete"] = False
            open_fee = (fee * remaining / shares) if fee is not None else ZERO
            unit_cost = (price + open_fee / remaining if side > 0
                         else price - open_fee / remaining)
            lots.append({"qty": Decimal(side) * remaining, "cost": unit_cost})

        # Broker PnL is authoritative only for a closing portion.  In a
        # cross-zero trade it belongs to the old cycle, never the new lot.
        if close_qty > ZERO:
            if broker is not None:
                active["realized"] += broker
                active["broker_rows"] += 1
                active["has_broker"] = True
            elif actual_closed > ZERO and close_cost_known:
                active["realized"] += derived
                active["derived_rows"] += 1
                active["has_derived"] = True
                if actual_closed < close_qty:
                    active["complete"] = False

        post_qty = lot_qty()
        if post_qty == ZERO:
            active["end"] = str(ts) if ts is not None else None
            active["final_qty"] = ZERO
            active = None
        elif close_qty > ZERO and open_qty > ZERO:
            # The old cycle closed at zero before the residual opened the new
            # opposite-direction cycle.  The same execution timestamp is used
            # for both sides to preserve the audit trail.
            active["end"] = str(ts) if ts is not None else None
            active["final_qty"] = ZERO
            active = new_cycle(side, str(ts) if ts is not None else None)
            add_trade(active, side, open_qty)
            if fee_missing:
                active["complete"] = False

    final_qty = lot_qty()
    if active is not None:
        active["final_qty"] = final_qty

    current_state = active if final_qty != ZERO else None
    result = []
    for state in states:
        result.append(PositionCycle(
            cycle_id=state["cycle_id"], start=state["start"], end=state["end"],
            direction=state["direction"], buys=state["buys"], sells=state["sells"],
            final_qty=state["final_qty"], realized=state["realized"],
            complete=state["complete"], broker_rows=state["broker_rows"],
            derived_rows=state["derived_rows"], current=state is current_state,
        ))
    return result


def aggregate_current_cycle_realized(rows, *, current_qty=None) -> RealizedResult:
    """Return realized PnL for the cycle containing the final non-zero lot."""
    valid_rows = [r for r in (rows or []) if _valid_execution_row(r)]
    expected_qty = dec(current_qty)
    signed_net = ZERO
    for row in valid_rows:
        side = _signed_side(_get(row, "side"))
        shares = dec(_get(row, "shares"))
        if side and shares is not None and shares > ZERO:
            signed_net += Decimal(side) * shares
    inferred_initial = (expected_qty - signed_net
                        if expected_qty is not None else None)
    cycles = rebuild_position_cycles(valid_rows, initial_qty=inferred_initial)
    current = next((c for c in cycles if c.current), None)
    if current is None:
        return RealizedResult(None, None, True, len(valid_rows))

    source = ("IBKR executions.realized_pnl" if current.broker_rows
              else "FIFO executions + commissions" if current.derived_rows
              else "current cycle (no closing execution)")
    complete = current.complete
    if expected_qty is not None and expected_qty != current.final_qty:
        # The execution retention window does not reconcile to the live
        # position.  Keep the calculable value but mark it incomplete instead
        # of silently presenting a historical cycle as current.
        complete = False
    return RealizedResult(
        current.realized, source, complete, len(valid_rows),
        cycle_id=current.cycle_id, cycle_start=current.start,
        cycle_end=current.end, cycle_qty=current.final_qty, current_cycle=True,
    )


def aggregate_realized_for_position(rows, position, *, account: str | None = None
                                    ) -> RealizedResult:
    """Identity-safe current-cycle realized PnL for one position row."""
    candidates = executions_for_position(rows, position, account=account)
    if not candidates:
        return RealizedResult(None, None, True, 0)
    return aggregate_current_cycle_realized(
        candidates, current_qty=_get(position, "qty"))
