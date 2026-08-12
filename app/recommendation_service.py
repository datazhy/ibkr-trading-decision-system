"""建议合成层（Phase 2）——确定性规则集把技术/计划/仓位/风险/市场合成为**可追溯**的
当前建议，并按「状态跃迁」落库。

**边界（与全项目一致）**：
  - 只读已落库的正式数据，保存来源引用；不新增动作、价格、数量、触发/失效条件。
  - 系统绝不连接下单接口；TRIGGERED 之后只能由人确认。
  - 缺失/过期/延迟数据对其**依赖路径**输出 BLOCKED，绝不解释为「低风险 / 安全」。
  - 数量字段宁可为空并降级 REVIEW，也不由展示层或 LLM 补算。

**门禁按依赖路径执行，不做账户级一刀切**（§1.3.1）：日线过期只让技术路径 BLOCKED；
若 fresh 账户快照已证明超限，仍可独立输出 DO_NOT_ADD / REDUCE，同时把技术理由标为不可用。

去重键是 `decision_fingerprint`（只含决策值），不是 input_refs —— 见 schema.sql 里的说明。
"""
from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

# 受控动作枚举（§1.4）。顺序即「风险从高到低」，仅用于展示排序参考。
BLOCKED = "BLOCKED"
EXIT = "EXIT"
REDUCE = "REDUCE"
DO_NOT_ADD = "DO_NOT_ADD"
ADD_WATCH = "ADD_WATCH"
HOLD = "HOLD"
REVIEW = "REVIEW"
ACTIONS = (BLOCKED, EXIT, REDUCE, DO_NOT_ADD, ADD_WATCH, HOLD, REVIEW)

# decided_by：本条建议最终由哪一层裁定（§1.3 优先级链）
BY_DATA = "data_quality"
BY_STOP = "hard_stop"
BY_LIMIT = "position_limit"
BY_PLAN = "plan"
BY_TECH = "technical"
BY_MARKET = "market"
BY_CHAIN = "chain_guardrail"
BY_NONE = "none"

# 生命周期状态（§1.5.4）
ST_ACTIVE = "ACTIVE"
ST_SUPERSEDED = "SUPERSEDED"
ST_BLOCKED = "BLOCKED"
ST_ACKNOWLEDGED = "ACKNOWLEDGED"
ST_EXPIRED = "EXPIRED"

#: 规则集版本。改动合成逻辑（新增/调整优先级步骤）时**必须**递增，
#: 让历史建议能按当时口径复核，且指纹随规则版本变化而变化。
RULE_SET_VERSION = "reco-rules/2026-08-v3"


def _rule_set_hash() -> str:
    """规则集指纹：绑定版本号 + 优先级步骤定义。规则逻辑一变，哈希即变，
    从而让「同样的输入在新规则下」不会被旧指纹去重掉。"""
    material = "|".join((RULE_SET_VERSION, *_STEP_SIGNATURE))
    return hashlib.sha256(material.encode()).hexdigest()[:16]


#: 优先级步骤签名 —— 改动步骤集合时同步更新，驱动 rule_set_hash 变化。
_STEP_SIGNATURE = (
    "1:data-gate", "2:hard-stop-invalidation", "3:account-plan-limit",
    "4:forbidden-override", "5:published-plan-action", "6:technical-confirm",
    "7:market-tighten", "8:chain-guardrail", "9:no-traceable-action",
)


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _id() -> str:
    return f"rec_{uuid.uuid4().hex}"


def _d(v) -> Decimal | None:
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, Decimal):
        return v
    try:
        return Decimal(str(v))
    except (InvalidOperation, ValueError, TypeError):
        return None


def _canon(v) -> str | None:
    """决策值规范化到 4 位小数、去尾零。用于指纹，保证 0.30 与 0.3000 同指纹。"""
    d = _d(v)
    if d is None:
        return None
    return format(d.quantize(Decimal("0.0001")).normalize(), "f")


# ---------------------------------------------------------------------------
# 输入契约
# ---------------------------------------------------------------------------

@dataclass
class SymbolInput:
    """单标的合成输入。由 Stage 2 的适配器从库里组装；本层只做纯裁决，不碰 IO。

    每个「路径」自带可靠性标记，门禁按路径判（§1.3.1）：
      - account_reliable：fresh 账户快照且 current_weight 可信 → 仓位/上限路径可用
      - price_reliable：报价实时且非扩展时段边界 → 精确触发价 / 止损比较可用
      - technical_reliable：行情与日线口径可靠（含 RTH 覆盖）→ 技术路径可用
    """
    account: str
    symbol: str
    cycle_id: str | None = None

    # —— 数据可靠性门禁（按路径）——
    account_reliable: bool = False
    price_reliable: bool = False
    technical_reliable: bool = False
    blocked_paths: dict = field(default_factory=dict)   # path -> reason（供 blocked_reason 汇总）

    # —— 仓位 ——
    current_weight: Decimal | None = None
    max_weight: Decimal | None = None
    target_weight: Decimal | None = None
    over_limit: bool | None = None
    reduce_quantity: Decimal | None = None              # 可追溯减仓数量（headroom / _add_estimate）

    # —— 硬约束 ——
    stop_breached: bool = False
    exit_on_stop: bool = False                          # True→EXIT，False→按计划 REDUCE
    invalidation_hit: bool = False
    invalidation_condition: str | None = None
    cooldown_active: bool = False                       # 冷静期覆盖 ADD

    # —— 正式计划动作（只读 PUBLISHED/LEGACY；草稿不得入建议）——
    plan_present: bool = False
    plan_actions: list = field(default_factory=list)    # [{id, action_type, trigger_type, quantity, percentage, condition, price_min, price_max, met}]

    # —— 技术 ——
    technical_action: str | None = None                # technical.decide 的 final_action
    technical_reason: str | None = None
    technical_rule_ids: list = field(default_factory=list)  # 命中的 T1..T8
    confirmation: str | None = None                    # CLOSE_CONFIRMED/INTRADAY_PENDING/EXTENDED_HOURS_PENDING

    # —— 市场 ——
    market_state: str | None = None                    # cautious/stressed/blocked/...

    # —— 链上：仅 Stage 5 ACTIVE 晋级后作为单调收紧护栏 ——
    chain_signal: dict | None = None

    # —— provenance ——
    quote: dict | None = None                          # {source, session, quality, as_of}
    bar: dict | None = None                            # {as_of, adjustment_mode}
    input_refs: dict = field(default_factory=dict)     # 汇总所有 ID（含易变 snapshot_id / quote_as_of）


# ---------------------------------------------------------------------------
# 合成（§1.3 八步优先级）
# ---------------------------------------------------------------------------

def synthesize_symbol(inp: SymbolInput) -> dict:
    """按固定优先级合成单标的建议。高优先级覆盖低优先级，但保留被覆盖项。

    返回一份完整的 recommendation dict（含 decision_fingerprint），可直接交给 persist()。
    """
    covered: list[str] = []
    q = inp.quote or {}
    bar = inp.bar or {}

    def build(action: str, decided_by: str, reason: str, *,
              quantity: Decimal | None = None,
              trigger: str | None = None,
              blocked_reason: str | None = None) -> dict:
        delta = None
        if inp.target_weight is not None and inp.current_weight is not None:
            delta = inp.target_weight - inp.current_weight
        rec = {
            "account": inp.account, "symbol": inp.symbol, "cycle_id": inp.cycle_id,
            "action": action, "decided_by": decided_by, "reason": reason,
            "current_weight": _s(inp.current_weight),
            "target_weight": _s(inp.target_weight),
            "max_weight": _s(inp.max_weight),
            "delta_weight": _s(delta),
            "quantity": _s(quantity),
            "amount": None,
            "trigger_condition": trigger,
            "invalidation_condition": inp.invalidation_condition,
            "blocked_reason": blocked_reason,
            "covered_action_ids": list(covered),
            "input_refs": dict(inp.input_refs),
            "confirmation": inp.confirmation,
            "quote_source": q.get("source"), "quote_session": q.get("session"),
            "quote_quality": q.get("quality"), "quote_as_of": q.get("as_of"),
            "bar_as_of": bar.get("as_of"), "adjustment_mode": bar.get("adjustment_mode"),
            "rule_set_version": RULE_SET_VERSION, "rule_set_hash": _rule_set_hash(),
        }
        rec["decision_fingerprint"] = decision_fingerprint(rec)
        return rec

    # —— Step 2：硬止损与失效条件（覆盖加仓与持有）——
    # 依赖价格可靠性：止损/失效的「是否触发」由上游用可靠报价判定后传入。
    if inp.stop_breached:
        return build(EXIT if inp.exit_on_stop else REDUCE, BY_STOP,
                     "已触发止损，优先执行止损/既定减仓计划",
                     quantity=inp.reduce_quantity if not inp.exit_on_stop else None)
    if inp.invalidation_hit:
        return build(REDUCE if inp.reduce_quantity is not None else EXIT, BY_STOP,
                     "计划失效条件已满足，按计划减仓/退出",
                     quantity=inp.reduce_quantity)

    # —— Step 3：账户与正式计划上限 ——
    # 依赖账户快照可靠；不可靠则该路径 BLOCKED，但不影响后续技术/计划路径的独立判断。
    if inp.over_limit:
        if inp.account_reliable:
            if inp.reduce_quantity is not None:
                return build(REDUCE, BY_LIMIT, "仓位已超上限且有可追溯减仓量，按计划减仓",
                             quantity=inp.reduce_quantity)
            return build(DO_NOT_ADD, BY_LIMIT, "仓位已超计划上限，不继续加仓")
        inp.blocked_paths.setdefault("position", "账户快照不可靠，无法核算仓位上限")

    # —— Step 4：禁止动作覆盖（冷静期/集中度等硬约束覆盖正式 ADD，记录被覆盖 action_id）——
    add_actions = [a for a in inp.plan_actions if _atype(a) == "ADD"]
    if inp.cooldown_active and add_actions:
        covered.extend(str(a.get("id")) for a in add_actions if a.get("id"))
        return build(DO_NOT_ADD, BY_LIMIT, "冷静期内，硬约束覆盖计划加仓动作")

    # —— Step 5：已发布计划动作（只读正式计划）——
    reduce_actions = [a for a in inp.plan_actions if _atype(a) in ("REDUCE", "EXIT")]
    if reduce_actions:
        a = reduce_actions[0]
        return _from_plan_action(a, build, reduce=True)
    if add_actions:
        # 加仓动作：需账户可靠以确认未超限；扩展时段/延迟只能 pending。
        if not inp.account_reliable:
            inp.blocked_paths.setdefault("position", "账户快照不可靠，加仓动作无法确认未超限")
        else:
            a = add_actions[0]
            return _from_plan_action(a, build, reduce=False,
                                     price_reliable=inp.price_reliable,
                                     confirmation=inp.confirmation)

    # —— Step 6：技术确认（技术面只调整状态与理由，不凭空造动作）——
    if inp.technical_reliable and inp.technical_action:
        return _from_technical(inp, build)
    if not inp.technical_reliable:
        inp.blocked_paths.setdefault("technical", "行情或日线口径不可靠，技术判断不可用")

    # —— Step 1 + Step 8：数据门禁兜底 / 无可追溯动作 ——
    # 到这里没有任何硬约束、计划动作或技术判断可用。
    if inp.blocked_paths and not inp.account_reliable:
        # 关键路径全被门禁挡住，且连仓位都无法核算 → 整条 BLOCKED。
        return build(BLOCKED, BY_DATA, "关键依赖数据不足，暂不生成建议",
                     blocked_reason=_join_blocked(inp.blocked_paths))
    if not inp.plan_present:
        # 无正式计划：只复盘，绝不从剩余仓位空间自行推导 ADD（§1.3.8 / §1.7）。
        return build(REVIEW, BY_NONE, "无正式计划，仅复盘；不从剩余空间自动推导加仓",
                     blocked_reason=_join_blocked(inp.blocked_paths) or None)
    return build(HOLD, BY_NONE, "结构中性，按既定计划持有",
                 blocked_reason=_join_blocked(inp.blocked_paths) or None)


def _atype(a: dict) -> str:
    return (a.get("action_type") or "").strip().upper()


def _from_plan_action(a: dict, build, *, reduce: bool,
                      price_reliable: bool = False, confirmation: str | None = None) -> dict:
    """把一条正式计划动作转成建议。MANUAL 待人工确认；PRICE 仅在价格口径可靠且条件满足才 TRIGGERED。"""
    aid = a.get("id")
    refs_note = f"action:{aid}" if aid else None
    trigger_type = (a.get("trigger_type") or "").strip().upper()
    condition = a.get("condition") or a.get("event_condition")
    qty = _d(a.get("quantity"))
    if reduce:
        rec = build(REDUCE, BY_PLAN, "按正式计划减仓动作", quantity=qty, trigger=condition)
    else:
        rec = build(ADD_WATCH, BY_PLAN, "正式计划加仓动作，进入观察", quantity=qty, trigger=condition)
    if aid:
        rec["input_refs"] = {**rec["input_refs"], "action_id": aid}
    # 触发态注记（不改 action 枚举，只在 reason/trigger 上体现）
    if trigger_type == "MANUAL":
        rec["reason"] += "（待人工确认）"
    elif trigger_type == "PRICE":
        if not price_reliable:
            rec["reason"] += "（价格口径不可靠，暂不触发）"
        elif confirmation == "EXTENDED_HOURS_PENDING":
            rec["reason"] += "（扩展时段，等待 RTH 收盘确认）"
        elif a.get("met"):
            rec["reason"] += "（价格条件已满足，待人工确认执行）"
    rec["decision_fingerprint"] = decision_fingerprint(rec)
    return rec


def _from_technical(inp: SymbolInput, build) -> dict:
    """技术裁决 → 建议。扩展时段的「加仓观察」一律降级为观察态，不形成收盘确认。"""
    mapping = {
        "EXECUTE_STOP": (EXIT, BY_STOP, "已跌破止损，优先执行止损"),
        "CHECK_REDUCE": (REDUCE, BY_TECH, "放量跌破 EMA60，检查减仓计划"),
        "DO_NOT_ADD": (DO_NOT_ADD, BY_TECH, inp.technical_reason or "技术面不宜加仓"),
        "ADD_WATCH": (ADD_WATCH, BY_TECH, "结构健康且仍有加仓空间，可按计划分档观察"),
        "OBSERVE": (HOLD, BY_TECH, "进入加仓观察区，等待确认"),
        "HOLD": (HOLD, BY_TECH, inp.technical_reason or "结构中性，持有"),
        "HOLD_NO_ADVICE": (REVIEW, BY_DATA, "数据过期或历史不足，暂不生成加减仓建议"),
    }
    action, by, reason = mapping.get(
        inp.technical_action, (HOLD, BY_TECH, inp.technical_reason or "持有"))
    rec = build(action, by, reason)
    if inp.technical_rule_ids:
        rec["input_refs"] = {**rec["input_refs"], "rule_ids": list(inp.technical_rule_ids)}
        rec["decision_fingerprint"] = decision_fingerprint(rec)
    return rec


def _join_blocked(paths: dict) -> str:
    return "；".join(f"{k}:{v}" for k, v in paths.items())


def _s(v) -> str | None:
    d = _d(v)
    return None if d is None else format(d, "f")


# ---------------------------------------------------------------------------
# 指纹（§1.5.2）——只哈希决策相关值，易变 ID 一律排除
# ---------------------------------------------------------------------------

def decision_fingerprint(rec: dict) -> str:
    """只哈希「决策相关值」。易变的 snapshot_id / quote_as_of / created_at / input_refs
    **绝不**进指纹，否则 60s 同步会让指纹每分钟变一次、去重永不命中、写爆表。"""
    material = {
        "action": rec.get("action"),
        "decided_by": rec.get("decided_by"),
        "current_weight": _canon(rec.get("current_weight")),
        "target_weight": _canon(rec.get("target_weight")),
        "max_weight": _canon(rec.get("max_weight")),
        "delta_weight": _canon(rec.get("delta_weight")),
        "quantity": _canon(rec.get("quantity")),
        "amount": _canon(rec.get("amount")),
        "trigger_condition": rec.get("trigger_condition"),
        "invalidation_condition": rec.get("invalidation_condition"),
        "blocked_reason": rec.get("blocked_reason"),
        "covered_action_ids": sorted(rec.get("covered_action_ids") or []),
        "confirmation": rec.get("confirmation"),
        "rule_set_hash": rec.get("rule_set_hash"),
    }
    blob = json.dumps(material, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(blob.encode()).hexdigest()


# ---------------------------------------------------------------------------
# 落库（§1.5.3 状态跃迁写入）
# ---------------------------------------------------------------------------

_COLUMNS = (
    "id", "account", "symbol", "cycle_id", "action", "decided_by", "reason",
    "current_weight", "target_weight", "max_weight", "delta_weight",
    "quantity", "amount", "trigger_condition", "invalidation_condition",
    "blocked_reason", "covered_action_ids", "input_refs", "decision_fingerprint",
    "rule_set_version", "rule_set_hash", "quote_source", "quote_session",
    "quote_quality", "quote_as_of", "bar_as_of", "adjustment_mode", "confirmation",
    "status", "supersedes_id", "created_at", "last_seen_at",
)


def persist(db, rec: dict, *, now: str | None = None) -> str:
    """按指纹去重落库，返回 recommendation_id。

    - 当前 ACTIVE/BLOCKED 行指纹相同 → 只更新 last_seen_at 与 input_refs，不新增行。
    - 指纹不同 → 旧行 SUPERSEDED，插入新行（supersedes_id 指向旧行）。
    """
    now = now or utcnow_iso()
    fp = rec.get("decision_fingerprint") or decision_fingerprint(rec)
    account, symbol = rec["account"], rec["symbol"]
    with db.connect() as conn:
        current = conn.execute(
            "SELECT id, decision_fingerprint FROM recommendation_snapshots "
            "WHERE account=? AND symbol=? AND status IN ('ACTIVE','BLOCKED') "
            "ORDER BY created_at DESC LIMIT 1", (account, symbol)).fetchone()
        if current is not None and current["decision_fingerprint"] == fp:
            conn.execute(
                "UPDATE recommendation_snapshots SET last_seen_at=?, input_refs=? WHERE id=?",
                (now, _json(rec.get("input_refs")), current["id"]))
            return current["id"]
        if current is not None:
            conn.execute(
                "UPDATE recommendation_snapshots SET status=? WHERE id=?",
                (ST_SUPERSEDED, current["id"]))
        rec_id = _id()
        status = ST_BLOCKED if rec["action"] == BLOCKED else ST_ACTIVE
        row = {
            **rec, "id": rec_id, "decision_fingerprint": fp,
            "covered_action_ids": _json(rec.get("covered_action_ids")),
            "input_refs": _json(rec.get("input_refs")),
            "status": status,
            "supersedes_id": current["id"] if current is not None else None,
            "created_at": now, "last_seen_at": now,
        }
        conn.execute(
            f"INSERT INTO recommendation_snapshots ({','.join(_COLUMNS)}) "
            f"VALUES ({','.join('?' for _ in _COLUMNS)})",
            tuple(row.get(c) for c in _COLUMNS))
        return rec_id


def _json(v) -> str | None:
    if v is None:
        return None
    return json.dumps(v, ensure_ascii=False, sort_keys=True)


# ---------------------------------------------------------------------------
# 适配器（Stage 2）——从库里组装 SymbolInput，逐标的合成并落库
# ---------------------------------------------------------------------------

#: 行情质量视为「实时可信」的取值
_REALTIME_QUALITIES = ("REALTIME", "REALTIME_NONCONSOLIDATED")
#: 行情质量「硬过期」——技术路径不可用（DELAYED/FROZEN 仍是真实价，不算硬过期）
_UNRELIABLE_QUALITIES = ("STALE", "MISSING", "INVALID")


def run_synthesis(cfg, db, *, now: datetime | None = None) -> dict:
    """对当前账户逐持仓运行确定性合成并落库。**旁路**：任何异常都不外抛，返回计数。

    复用技术页已有的计算（confirmation_basis / compute_symbol / decide）与计划面板
    的仓位、上限、加仓估算，不重写指标或仓位算法（§1.2）。
    """
    from . import market_hours, technical, position_plans
    from .db import to_decimal

    now = now or datetime.now(timezone.utc)
    now_iso = now.isoformat()
    try:
        account = _resolve_account(cfg, db)
        if not account:
            return {"account": None, "synthesized": 0, "written": 0, "skipped": 0}
        session = market_hours.market_session(now, cfg.market_tz)
        positions = db.latest_positions()
        stops = {r["symbol"]: to_decimal(r["stop_price"]) for r in db.latest_stop_levels()}
        stop_dates = {r["symbol"]: r["computed_date"] for r in db.latest_stop_levels()}
        market = db.latest_market_assessment()
        market_state = market["market_state"] if market else None
        market_ref = market["assessment_id"] if market else None
        pp = position_plans.dashboard_for_account(db, account, now=now_iso)
        card_by_sym = {c["symbol"]: c for c in pp.get("cards", [])}
    except Exception:  # noqa: BLE001 —— 组装失败不得拖垮调用方
        return {"account": None, "synthesized": 0, "written": 0, "skipped": 0, "error": True}

    written = skipped = 0
    for r in positions:
        try:
            qty = to_decimal(r["qty"]) or Decimal("0")
            if qty == 0:
                continue
            rec = _synthesize_position(
                cfg, db, technical, to_decimal, r, qty, session, stops, stop_dates,
                card_by_sym.get(r["symbol"]) or {}, market_state, market_ref, now)
            if rec is None:
                skipped += 1
                continue
            persist(db, rec, now=now_iso)
            written += 1
        except Exception:  # noqa: BLE001 —— 单标的失败只跳过该标的
            skipped += 1
    return {"account": account, "synthesized": written + skipped,
            "written": written, "skipped": skipped}


def _resolve_account(cfg, db) -> str | None:
    acct = getattr(cfg, "ib_account", None)
    if acct:
        return acct
    row = db.latest_snapshot_row()
    return row["account"] if row is not None else None


def _synthesize_position(cfg, db, technical, to_decimal, r, qty, session, stops,
                         stop_dates, card, market_state, market_ref,
                         now: datetime) -> dict | None:
    symbol = r["symbol"]
    price = to_decimal(r["price"])
    bars = db.get_daily_bars(symbol, limit=600)
    conf = technical.confirmation_basis(
        session, price_source=r["price_source"], quote_source=r["source"])
    stop = stops.get(symbol)
    card_tech = technical.compute_symbol(
        symbol, bars, price, stop_price=stop, confirmation=conf)

    # —— 数据可靠性：技术路径必须覆盖最近一个已完整结束的 RTH 交易日 ——
    quality = (r["price_quality"] or "").upper()
    acct_w = to_decimal(card.get("account_weight"))
    max_w = to_decimal(card.get("max_weight"))
    target_w = to_decimal(card.get("target_weight"))
    account_reliable = acct_w is not None
    price_reliable = (quality in _REALTIME_QUALITIES
                      and conf != technical.EXTENDED_HOURS_PENDING)
    from .us_market_calendar import latest_completed_regular_day
    expected_bar_day = latest_completed_regular_day(now, cfg.market_tz).isoformat()
    bar_max_date = bars[-1]["date"] if bars else None
    bar_covers_rth = bool(bar_max_date and str(bar_max_date)[:10] >= expected_bar_day)
    # compute_symbol() 不返回 is_stale；这里同时检查行情质量与日线 RTH 覆盖。
    technical_reliable = (bool(bars) and card_tech["daily_trend_state"] is not None
                          and quality not in _UNRELIABLE_QUALITIES and bar_covers_rth)

    # —— 仓位上限 ——
    over_limit = None
    plan_allows_add = False
    if acct_w is not None and max_w is not None and max_w > 0:
        over_limit = acct_w > max_w
        plan_allows_add = acct_w < max_w

    # —— 止损 ——
    stop_breached = bool(price is not None and stop is not None
                         and ((qty > 0 and price < stop) or (qty < 0 and price > stop)))

    # —— 正式计划动作（原始，只读 ACTIVE/TRIGGERED）——
    cycle_id = card.get("id")
    plan = card.get("formal_plan")
    plan_present = bool(card.get("has_published_plan"))
    raw_actions = db.active_plan_actions(cycle_id) if (cycle_id and plan_present) else []
    plan_actions = [_action_view(a, price) for a in raw_actions]

    # —— 技术裁决：只在 decide 落到「纯技术」时才作为技术信号，避免与合成层重复裁决 ——
    dec = technical.decide(
        card_tech, data_ok=technical_reliable, risk_breached=stop_breached,
        over_limit=over_limit, plan_allows_add=plan_allows_add, confirmation=conf)
    technical_action = dec["final_action"] if dec.get("decided_by") == "technical" else None
    technical_reason = dec.get("reason") if technical_action else None
    rule_ids = [h["code"] for h in technical.evaluate_rules(card_tech, confirmation=conf)]

    # —— provenance 引用（含易变 ID，仅存 input_refs，不进指纹）——
    refs = {
        "snapshot_ts": r["ts"] if "ts" in r.keys() else None,
        "sync_run_id": r["sync_run_id"] if "sync_run_id" in r.keys() else None,
        "quote_as_of": r["quote_as_of"],
        "bar_max_date": bar_max_date,
        "expected_rth_bar_date": expected_bar_day,
        "stop_date": stop_dates.get(symbol),
        "plan_version_id": plan.get("id") if plan else None,
        "market_assessment_id": market_ref,
    }
    refs = {k: v for k, v in refs.items() if v is not None}

    chain_signal = _load_active_chain_signal(cfg, db, symbol, now)
    if chain_signal:
        refs.update({
            "chain_signal_id": chain_signal.get("signal_id"),
            "chain_model": chain_signal.get("model"),
            "chain_state": chain_signal.get("state"),
            "chain_confidence": chain_signal.get("confidence"),
            "chain_rules_version": chain_signal.get("rules_version"),
        })

    inp = SymbolInput(
        account=r["account"] if "account" in r.keys() and r["account"] else _resolve_account(cfg, db),
        symbol=symbol, cycle_id=cycle_id,
        account_reliable=account_reliable, price_reliable=price_reliable,
        technical_reliable=technical_reliable,
        current_weight=acct_w, max_weight=max_w, target_weight=target_w,
        over_limit=over_limit, reduce_quantity=None,          # 无既有可追溯减仓量 → 超限只 DO_NOT_ADD
        stop_breached=stop_breached, exit_on_stop=True,
        invalidation_condition=(plan.get("invalidation_conditions") if plan else None),
        plan_present=plan_present, plan_actions=plan_actions,
        technical_action=technical_action, technical_reason=technical_reason,
        technical_rule_ids=rule_ids, confirmation=conf,
        market_state=market_state,
        chain_signal=chain_signal,
        quote={"source": r["source"], "session": session,
               "quality": r["price_quality"], "as_of": r["quote_as_of"]},
        bar={"as_of": bars[-1]["date"] if bars else None, "adjustment_mode": None},
        input_refs=refs,
    )
    rec = synthesize_symbol(inp)
    # 市场只收紧：cautious/stressed 把加仓类降级（§1.3.7）
    rec = _apply_market_tighten(rec, market_state)
    return _apply_chain_guardrail(rec, chain_signal)


def _action_view(a, price) -> dict:
    """把 position_plan_actions 行转成合成层可读的动作视图；met=价格条件是否满足。"""
    d = {k: a[k] for k in a.keys()}
    met = False
    if (a["trigger_type"] or "").upper() == "PRICE" and price is not None:
        lo, hi = a["price_min"], a["price_max"]
        try:
            lo_d = Decimal(str(lo)) if lo not in (None, "") else None
            hi_d = Decimal(str(hi)) if hi not in (None, "") else None
            met = ((lo_d is None or price >= lo_d) and (hi_d is None or price <= hi_d))
        except (InvalidOperation, ValueError, TypeError):
            met = False
    return {"id": a["id"], "action_type": a["action_type"],
            "trigger_type": a["trigger_type"], "quantity": a["quantity"],
            "percentage": a["percentage"], "condition": a["event_condition"],
            "price_min": a["price_min"], "price_max": a["price_max"], "met": met}


# ---------------------------------------------------------------------------
# 展示（Stage 3）——首页「今日行动」与只读 API 共用
# ---------------------------------------------------------------------------

ACTION_LABEL = {
    BLOCKED: "暂不判断", EXIT: "退出", REDUCE: "减仓", DO_NOT_ADD: "不加仓",
    ADD_WATCH: "加仓观察", HOLD: "持有", REVIEW: "待复盘",
}
#: 展示配色：风险增高用告警色，加仓观察用积极色，其余中性
ACTION_CSS = {
    BLOCKED: "crit", EXIT: "crit", REDUCE: "warn", DO_NOT_ADD: "warn",
    ADD_WATCH: "good", HOLD: "", REVIEW: "warn",
}
DECIDED_BY_LABEL = {
    BY_DATA: "数据质量", BY_STOP: "账户止损", BY_LIMIT: "仓位上限",
    BY_PLAN: "正式计划", BY_TECH: "技术面", BY_MARKET: "市场环境",
    BY_CHAIN: "链上护栏", BY_NONE: "—",
}
_CONFIRM_LABEL = {
    "CLOSE_CONFIRMED": "收盘确认", "INTRADAY_PENDING": "盘中待确认",
    "EXTENDED_HOURS_PENDING": "盘后待确认",
}


def panel(db, account: str | None, *, now=None) -> dict:
    """首页「今日行动」面板数据。每个标的一张紧凑卡（§1.6）。旁路：异常返回空。"""
    try:
        if not account:
            row = db.latest_snapshot_row()
            account = row["account"] if row is not None else None
        if not account:
            return {"account": None, "cards": []}
        rows = db.current_recommendations(account)
        return {"account": account, "cards": [_card(r) for r in rows]}
    except Exception:  # noqa: BLE001
        return {"account": None, "cards": [], "error": True}


def _pct(v) -> str | None:
    d = _d(v)
    return None if d is None else f"{d * 100:.2f}%"


def _card(r) -> dict:
    action = r["action"]
    qty = r["quantity"]
    covered = json.loads(r["covered_action_ids"]) if r["covered_action_ids"] else []
    refs = json.loads(r["input_refs"]) if r["input_refs"] else {}
    # 数据状态：扩展时段 / 延迟 / 过期直接显示
    data_notes = []
    conf_label = _CONFIRM_LABEL.get(r["confirmation"])
    if conf_label and conf_label != "收盘确认":
        data_notes.append(conf_label)
    if (r["quote_quality"] or "").upper() not in _REALTIME_QUALITIES and r["quote_quality"]:
        data_notes.append(f"行情{r['quote_quality']}")
    return {
        "id": r["id"], "symbol": r["symbol"], "cycle_id": r["cycle_id"],
        "action": action,
        "action_label": ACTION_LABEL.get(action, action),
        "action_css": ACTION_CSS.get(action, ""),
        "reason": r["reason"],
        "decided_by_label": DECIDED_BY_LABEL.get(r["decided_by"], r["decided_by"]),
        # 第一行：数量/权重；不可确定时明确「不提供数量」
        "quantity": qty,
        "quantity_display": (f"{qty} 股" if qty else "不提供数量"),
        "current_weight_display": _pct(r["current_weight"]),
        "max_weight_display": _pct(r["max_weight"]),
        "target_weight_display": _pct(r["target_weight"]),
        # 第二行
        "trigger_condition": r["trigger_condition"],
        "invalidation_condition": r["invalidation_condition"],
        "covered_action_ids": covered,
        "blocked_reason": r["blocked_reason"],
        # 第三行
        "confirmation_label": conf_label,
        "data_notes": data_notes,
        # 第四行：来源与时点
        "input_refs": refs,
        "rule_set_version": r["rule_set_version"],
        "created_at": r["created_at"],
        "last_seen_at": r["last_seen_at"],
        "status": r["status"],
    }


#: 市场收紧时，加仓类动作降级到的目标
_MARKET_TIGHTEN = {ADD_WATCH: DO_NOT_ADD}


def _apply_market_tighten(rec: dict, market_state: str | None) -> dict:
    """cautious/stressed/blocked 只收紧或阻止风险增加，绝不凭市场状态创造新的个股动作。"""
    if market_state not in ("cautious", "stressed", "blocked"):
        return rec
    tightened = _MARKET_TIGHTEN.get(rec["action"])
    if tightened is None:
        return rec
    rec["action"] = tightened
    rec["decided_by"] = BY_MARKET
    rec["reason"] = f"市场环境{market_state}，收紧加仓：{rec['reason']}"
    rec["decision_fingerprint"] = decision_fingerprint(rec)
    return rec


def _load_active_chain_signal(cfg, db, symbol: str, now: datetime) -> dict | None:
    """只读取已晋升 ACTIVE 的 swing 信号；阶段 1–4 和旧配置完全旁路。"""
    if not getattr(cfg, "hl_swing_influence_enabled", False):
        return None
    if not getattr(cfg, "hyperliquid_enabled", False):
        return None
    try:
        from .hyperliquid_db import HyperliquidDatabase
        from .hyperliquid_service import HyperliquidService
        from .hyperliquid_results import active_promotion

        hl_db = HyperliquidDatabase(getattr(cfg, "hl_db_path", "./data/hyperliquid.db"))
        if not hl_db.available():
            return None
        with db.connect() as conn:
            mapping = conn.execute(
                "SELECT coin FROM hl_asset_mappings WHERE canonical_symbol=? AND status='READY' "
                "ORDER BY checked_at DESC LIMIT 1", (symbol.upper(),)).fetchone()
        if not mapping:
            return None
        coin = mapping["coin"]
        rows = HyperliquidService(cfg, hl_db).signals(
            model="SWING", coin=coin, active_only=True, limit=1)
        row = rows[0] if rows else None
        if not row or row.get("state") not in {"BULLISH", "BEARISH"} or row.get("confidence") != "HIGH":
            return None
        if str(row.get("quality_status") or "") != "READY":
            return None
        valid_until = row.get("valid_until")
        if valid_until:
            try:
                expiry = datetime.fromisoformat(str(valid_until).replace("Z", "+00:00"))
                if expiry.tzinfo is None:
                    expiry = expiry.replace(tzinfo=timezone.utc)
                if expiry <= now:
                    return None
            except ValueError:
                return None
        promotion = active_promotion(
            db, model="SWING", rules_version=row.get("rules_version") or "",
            rules_hash=row.get("rules_hash") or "", policy_hash=row.get("policy_hash"),
        )
        return row if promotion else None
    except Exception:  # noqa: BLE001 — 链上旁路故障不得改变既有建议
        return None


def _apply_chain_guardrail(rec: dict, chain_signal: dict | None) -> dict:
    """链上 bearish 只允许单调收紧 ADD_WATCH/HOLD，不改退出或风险动作。"""
    if not chain_signal or rec.get("action") not in (ADD_WATCH, HOLD):
        return rec
    target = DO_NOT_ADD if rec.get("action") == ADD_WATCH else REVIEW
    rec["action"] = target
    rec["decided_by"] = BY_CHAIN
    rec["reason"] = (f"链上 {chain_signal.get('model', 'SWING')} 高置信度看空，"
                      f"仅收紧新增风险：{rec.get('reason') or ''}")
    rec["decision_fingerprint"] = decision_fingerprint(rec)
    return rec
