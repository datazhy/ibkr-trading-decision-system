"""历史快照的来源身份与账户/持仓时间对齐（纯函数 + 只读查询）。

生产现实（实测）：21644 条账户快照里**只有 3.5% 带 snapshot_id** —— 快照层是
后来才加的，之前 17 天的数据没有这个字段。但其中 96% 的账户汇总在持仓表里有
**完全相同的时间戳**，因为它们本来就是同一次同步写进去的。

所以身份策略：

    native_snapshot_id            有原始 snapshot_id，直接用
    reconstructed_timestamp_group 无 id，但账户与持仓时间戳能可靠配对
    insufficient_identity         配不上，**跳过并记录原因**

**绝不向原始表回填伪造的 snapshot_id。** 历史数据就是历史数据，往里写一个
系统自己编的 id，会让「这份快照当时是不是完整的」这个问题永远说不清。
重放需要稳定的键，就自己算一个 `source_snapshot_key`，存在重放自己的表里。

对齐的要害是**绝不跨时间拼接**：拿 10:00 的账户余额配 10:05 的持仓，算出来的
集中度既不是 10:00 的也不是 10:05 的，而是一个从未存在过的账户状态。
"""
from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from . import cashflow_model as cm

# 身份类型
ID_NATIVE = "native_snapshot_id"
ID_RECONSTRUCTED = "reconstructed_timestamp_group"
ID_INSUFFICIENT = "insufficient_identity"

# 对齐状态
ALIGN_EXACT = "exact"
ALIGN_TOLERANCE = "within_tolerance"
ALIGN_ESTIMATED = "estimated"
ALIGN_INVALID = "invalid"

#: 账户汇总与持仓的最大允许时间差（秒）。默认 60 秒 —— 同步周期就是 60 秒，
#: 超过一个周期就可能跨了两次采集，那是两份不同的账户状态。
DEFAULT_TOLERANCE_SECONDS = 60

#: 持仓市值合计与账户 positions_value 的允许相对偏差。
#: 超出即认为持仓不完整（同步只写进了一部分标的）。
MV_TOLERANCE = Decimal("0.02")


def _d(v):
    return cm.d(v)


def _dt(v) -> datetime | None:
    if not v:
        return None
    try:
        t = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


def position_group_signature(positions: list[dict]) -> str:
    """持仓组合的稳定指纹。

    只用 symbol 与数量 —— 价格每分钟都在变，把它算进去会让同一批持仓在
    不同时刻产生不同的键，重复重放就不再幂等。
    """
    parts = sorted(
        f"{p.get('symbol') or ''}:{p.get('qty') or p.get('quantity') or ''}"
        for p in positions or [])
    return hashlib.sha256("|".join(parts).encode()).hexdigest()[:16]


def source_snapshot_key(*, account_id: str | None, data_as_of: str,
                        balance_signature: str, positions: list[dict]) -> str:
    """重放专用的稳定来源键。

    同一份历史数据无论重放多少次，键值都一样 —— 这是幂等的基础。
    """
    raw = "|".join([
        str(account_id or ""), str(data_as_of or ""),
        str(balance_signature or ""), position_group_signature(positions)])
    return "rsk-" + hashlib.sha256(raw.encode()).hexdigest()[:20]


def balance_signature(row: dict) -> str:
    """账户汇总的指纹。用净值与现金 —— 它们唯一确定这一时刻的账户规模。"""
    parts = [str(row.get(k) or "") for k in
             ("equity", "total_cash", "cash", "maint_margin", "excess_liquidity")]
    return hashlib.sha256("|".join(parts).encode()).hexdigest()[:16]


def _mv_consistency(balance: dict, positions: list[dict]) -> tuple[bool | None, str]:
    """持仓市值合计与账户 positions_value 是否一致。

    老快照的 positions_value 为 NULL（该列是后加的），此时**无法验证完整性**，
    返回 None 并说明 —— 这类快照只能标 estimated，不能冒充 exact。
    """
    pv = _d(balance.get("positions_value"))
    mv = sum((_d(p.get("market_value")) or Decimal("0")) for p in positions or [])
    if pv is None:
        return None, "账户未记录 positions_value（该字段为后加），无法验证持仓完整性"
    if pv == 0:
        return (mv == 0), ("持仓市值与账户口径一致（均为零）" if mv == 0
                           else f"账户 positions_value 为零但持仓合计 {mv}")
    rel = abs(mv - pv) / abs(pv)
    if rel <= MV_TOLERANCE:
        return True, f"持仓市值与账户口径一致（偏差 {rel:.2%}）"
    return False, (f"持仓市值合计 {mv} 与账户 positions_value {pv} 相差 "
                   f"{rel:.1%}，持仓可能不完整")


def resolve_context(db, balance_row: dict, *,
                    tolerance_seconds: int = DEFAULT_TOLERANCE_SECONDS) -> dict:
    """为一条账户快照解析出可用于重放的完整上下文。

    **只读**。绝不写任何表，绝不修改原始数据。
    """
    account = balance_row.get("account")
    ts = balance_row.get("ts")
    snapshot_id = (balance_row.get("snapshot_id") or "").strip() or None

    out: dict = {
        "account_id": account,
        "account_data_as_of": ts,
        "positions_data_as_of": None,
        "alignment_gap_seconds": None,
        "alignment_status": ALIGN_INVALID,
        "source_identity_type": ID_INSUFFICIENT,
        "snapshot_id": snapshot_id,
        "source_snapshot_key": None,
        "account_summary": balance_row,
        "positions": [],
        "data_quality": balance_row.get("quality_status"),
        "reliable": False,
        "skip_reason": None,
    }

    positions: list[dict] = []
    # ---- 1) 有原生 snapshot_id：直接用，这是最可靠的来源 ----
    if snapshot_id:
        try:
            positions = db.positions_for_snapshot(snapshot_id)
        except Exception:  # noqa: BLE001
            positions = []
        if positions:
            out.update({
                "source_identity_type": ID_NATIVE,
                "positions": positions,
                "positions_data_as_of": positions[0].get("ts") or ts,
                "alignment_gap_seconds": 0,
                "alignment_status": ALIGN_EXACT,
            })
        else:
            out["skip_reason"] = (
                f"快照 {snapshot_id[:12]} 有账户汇总但没有持仓明细")
            return _finalize(out, balance_row, positions)

    # ---- 2) 无 id：按时间戳重建 ----
    if not positions:
        try:
            exact = db.positions_at_exact_ts(ts)
        except Exception:  # noqa: BLE001
            exact = []
        if exact:
            positions = exact
            out.update({
                "source_identity_type": ID_RECONSTRUCTED,
                "positions": positions,
                "positions_data_as_of": ts,
                "alignment_gap_seconds": 0,
                "alignment_status": ALIGN_EXACT,
            })
        else:
            near = _nearest_positions(db, ts, tolerance_seconds)
            if near is None:
                out["skip_reason"] = (
                    f"账户时间 {ts} 前后 {tolerance_seconds} 秒内没有持仓记录，"
                    "无法可靠配对")
                return out
            positions, gap = near
            out.update({
                "source_identity_type": ID_RECONSTRUCTED,
                "positions": positions,
                "positions_data_as_of": positions[0].get("ts"),
                "alignment_gap_seconds": gap,
                "alignment_status": ALIGN_TOLERANCE,
            })

    return _finalize(out, balance_row, positions)


def _finalize(out: dict, balance_row: dict, positions: list[dict]) -> dict:
    """完成完整性校验、来源键与可靠性判定。"""
    if not positions:
        out["alignment_status"] = ALIGN_INVALID
        out["reliable"] = False
        if not out.get("skip_reason"):
            out["skip_reason"] = "没有可用的持仓明细"
        return out

    consistent, note = _mv_consistency(balance_row, positions)
    out["consistency_note"] = note
    if consistent is False:
        # 持仓明显不完整 —— 用它算集中度会得到完全错误的结论
        out["alignment_status"] = ALIGN_INVALID
        out["reliable"] = False
        out["skip_reason"] = note
        return out
    if consistent is None and out["alignment_status"] == ALIGN_EXACT:
        # 时间戳精确但无法验证完整性：降级为 estimated，不冒充 exact
        out["alignment_status"] = ALIGN_ESTIMATED

    out["source_snapshot_key"] = source_snapshot_key(
        account_id=balance_row.get("account"),
        data_as_of=balance_row.get("ts"),
        balance_signature=balance_signature(balance_row),
        positions=positions)
    out["reliable"] = out["alignment_status"] in (ALIGN_EXACT, ALIGN_TOLERANCE,
                                                  ALIGN_ESTIMATED)
    return out


def _nearest_positions(db, ts: str, tolerance_seconds: int):
    """时间容差内最近的一批持仓。

    **只找不晚于账户时间的**（加上一点点向后容差）—— 用账户快照之后才出现的
    持仓，等于用未来数据判断过去，是标准的 look-ahead。
    """
    t = _dt(ts)
    if t is None:
        return None
    try:
        rows = db.positions_near_ts(ts, tolerance_seconds)
    except Exception:  # noqa: BLE001
        return None
    best = None
    for group_ts, group in rows:
        gt = _dt(group_ts)
        if gt is None:
            continue
        gap = (t - gt).total_seconds()
        # 允许持仓略早于账户（同一次同步先写持仓后写余额），
        # 但**绝不允许持仓晚于账户超过 1 秒**
        if gap < -1:
            continue
        if abs(gap) > tolerance_seconds:
            continue
        if best is None or abs(gap) < abs(best[1]):
            best = (group, gap)
    if best is None:
        return None
    return best[0], int(abs(best[1]))


def summarize_coverage(contexts: list[dict]) -> dict:
    """一批上下文的覆盖情况摘要。"""
    total = len(contexts)
    by_identity: dict[str, int] = {}
    by_alignment: dict[str, int] = {}
    for c in contexts:
        by_identity[c["source_identity_type"]] = by_identity.get(
            c["source_identity_type"], 0) + 1
        by_alignment[c["alignment_status"]] = by_alignment.get(
            c["alignment_status"], 0) + 1
    usable = sum(1 for c in contexts if c["reliable"])
    return {
        "total": total, "usable": usable, "skipped": total - usable,
        "by_identity_type": by_identity, "by_alignment_status": by_alignment,
        "native_ratio": (f"{by_identity.get(ID_NATIVE, 0)}/{total}"
                         if total else "0/0"),
    }
