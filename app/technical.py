"""技术面：按需从日线现算 EMA / 周线 / ATR / 高点 / RVOL 与结构状态。

纯计算，输入按日期升序的日线（每根含 Decimal 可转的 o/h/l/c/v）与当前价，
输出结构化状态字典。不落表、不依赖富途 K 线；ATR 复用 indicators。

只回答「结构、位置、确认」，不产生交易指令——那是 P2 规则层的事。
"""
from __future__ import annotations

from collections import OrderedDict
from datetime import date
from decimal import Decimal
from typing import Sequence

from . import indicators as ind

CALC_VERSION = "TECH/2026-08-EMA60-250"

EMA_PERIODS = (5, 10, 20, 60, 120, 250)
#: 各均线方向的回看根数（短中长期逐级拉长）
SLOPE_LOOKBACK = {20: 5, 60: 10, 120: 20, 250: 40}
WEEKLY_PERIODS = (10, 20, 40)
HIGH_20D = 20
HIGH_52W = 252
RVOL_WINDOW = 20

#: 斜率「走平」带：|Δ| < 该比例 × ATR 视为走平，否则按符号定升/降。
_FLAT_ATR_FRAC = Decimal("0.15")
#: 均线纠缠：EMA5/10/20 极差 < 该倍数 × ATR
_ENTANGLE_ATR = Decimal("0.5")

UP, FLAT, DOWN = "up", "flat", "down"

#: 技术确认态。突破/加仓只有在 CLOSE_CONFIRMED 下才算「已确认」。
#: 扩展时段（盘前/盘后/夜盘）流动性差、易假突破，一律 pending，绝不能形成收盘确认。
CLOSE_CONFIRMED = "CLOSE_CONFIRMED"
INTRADAY_PENDING = "INTRADAY_PENDING"
EXTENDED_HOURS_PENDING = "EXTENDED_HOURS_PENDING"

#: 报价来源里代表扩展时段的标记（富途按时段回报 PRE_MARKET/AFTER_HOURS/OVERNIGHT）
_EXT_SOURCE_MARKERS = ("PRE_MARKET", "AFTER_HOURS", "OVERNIGHT", "PRE", "POST")


def confirmation_basis(session: str | None, *, price_source: str | None = None,
                       quote_source: str | None = None) -> str:
    """由报价自身所处时段决定技术确认态，而不是只看墙钟是否 REGULAR。

    这是三处调用点（webtechnical / webdata / event_service）共用的唯一口径 ——
    原先各自 `intraday = session == REGULAR` 会把盘前/盘后/夜盘塌进「收盘确认」桶，
    使低流动性的扩展时段价格以「收盘突破」呈现，误导加仓判断（P0-1）。

    - REGULAR                      → INTRADAY_PENDING
    - PRE / POST / OVERNIGHT       → EXTENDED_HOURS_PENDING
    - 报价来源自报扩展时段          → EXTENDED_HOURS_PENDING（即使墙钟判 CLOSED）
    - CLOSED 且报价非扩展时段       → CLOSE_CONFIRMED
    """
    src = (quote_source or "").upper()
    if any(marker in src for marker in _EXT_SOURCE_MARKERS):
        return EXTENDED_HOURS_PENDING
    if session in ("PRE", "POST", "OVERNIGHT"):
        return EXTENDED_HOURS_PENDING
    if session == "REGULAR":
        return INTRADAY_PENDING
    return CLOSE_CONFIRMED


def _d(v) -> Decimal:
    return v if isinstance(v, Decimal) else Decimal(str(v))


def weekly_bars(bars: Sequence[dict]) -> list[dict]:
    """把日线按 ISO 周聚合成周线（o=周首开, h=周内高, l=周内低, c=周末收, v=周内合计）。

    最后一周可能未走完（forming）；这里全部返回，周线 EMA 会自然反映当前进行中的一周。
    """
    weeks: "OrderedDict[tuple, dict]" = OrderedDict()
    for b in bars:
        y, m, dd = (int(x) for x in b["date"].split("-"))
        iso = date(y, m, dd).isocalendar()
        key = (iso[0], iso[1])
        w = weeks.get(key)
        if w is None:
            weeks[key] = {"date": b["date"], "o": _d(b["o"]), "h": _d(b["h"]),
                          "l": _d(b["l"]), "c": _d(b["c"]), "v": int(b["v"])}
        else:
            w["h"] = max(w["h"], _d(b["h"]))
            w["l"] = min(w["l"], _d(b["l"]))
            w["c"] = _d(b["c"])
            w["v"] += int(b["v"])
            w["date"] = b["date"]
    return list(weeks.values())


def _safe(fn, *a, **k):
    """指标不足数据时返回 None 而非抛错——逐指标降级（spec 降级矩阵）。"""
    try:
        return fn(*a, **k)
    except ind.IndicatorError:
        return None


def _slope_state(delta: Decimal | None, atr_val: Decimal | None) -> str | None:
    if delta is None:
        return None
    if atr_val and abs(delta) < _FLAT_ATR_FRAC * atr_val:
        return FLAT
    return UP if delta > 0 else (DOWN if delta < 0 else FLAT)


def _daily_trend_state(price, e, slope, atr_val) -> str:
    """日线排列状态（spec §3）。e/slope 为各周期 EMA 值与方向；缺失按可得信息降级。"""
    e5, e10, e20, e60, e120, e250 = (e.get(p) for p in EMA_PERIODS)
    up20 = slope.get(20) == UP
    up60 = slope.get(60) == UP
    # 均线纠缠：短中均线挤在一起
    if None not in (e5, e10, e20) and atr_val:
        if max(e5, e10, e20) - min(e5, e10, e20) < _ENTANGLE_ATR * atr_val:
            entangled = True
        else:
            entangled = False
    else:
        entangled = False

    if None not in (e5, e10, e20, e60, e120, e250):
        if price > e5 > e10 > e20 > e60 > e120 > e250 and up20 and up60:
            return "强多头"
        if price > e20 > e60 > e120 > e250:
            return "多头趋势"
    if up20 and up60 and e10 is not None and e20 is not None and e20 <= price <= e10:
        return "回踩趋势"
    if entangled:
        return "均线纠缠"
    if e60 is not None and price < e60 and slope.get(60) in (FLAT, DOWN):
        return "中期趋势破坏"
    if e20 is not None and price < e20:
        return "短线转弱"
    if e120 is not None and price < e120:
        return "长期弱势"
    return "中性"


def _position_state(dist_atr: Decimal | None) -> str | None:
    """距 EMA20 的 ATR 倍数分档（spec §2.4）。"""
    if dist_atr is None:
        return None
    x = dist_atr
    if x < Decimal("-1"):
        return "明显转弱"
    if x < Decimal("0.5"):
        return "贴近趋势线" if x >= 0 else "贴近趋势线"
    if x < Decimal("1.5"):
        return "健康趋势"
    if x <= Decimal("2"):
        return "明显偏高"
    return "过度延伸"


def _weekly_trend_state(wprice, w10, w20, w40, w10_up) -> str | None:
    if None in (w10, w20, w40):
        return None
    if wprice > w10 > w20 > w40 and w10_up:
        return "周线多头"
    if w10_up and wprice < w10 and wprice > w40:
        return "周线上升但回调"
    if wprice < w40:
        return "周线弱势"
    return "周线震荡"


def _breakout_state_high20(price, high20, confirmation) -> str | None:
    if high20 is None or price is None:
        return None
    if price > high20:
        if confirmation == CLOSE_CONFIRMED:
            return "收盘突破"
        if confirmation == EXTENDED_HOURS_PENDING:
            return "盘后突破"          # 扩展时段：待 RTH 收盘确认，不能当收盘突破
        return "盘中突破"
    gap = (high20 - price) / high20 if high20 else None
    if gap is not None and gap <= Decimal("0.03"):
        return "接近20日高点"
    return "远离20日高点"


def _state_52w(price, high52) -> str | None:
    if high52 is None or price is None:
        return None
    gap = (high52 - price) / high52 if high52 else Decimal("0")
    if gap <= Decimal("0.02"):
        return "接近52周新高"
    if gap <= Decimal("0.10"):
        return "强势区"
    if gap <= Decimal("0.25"):
        return "正常回撤"
    return "深度回撤"


def _volume_state(rvol) -> str | None:
    if rvol is None:
        return None
    if rvol >= Decimal("1.5"):
        return "放量"
    if rvol >= Decimal("0.8"):
        return "正常"
    return "缩量"


def compute_symbol(symbol: str, bars: Sequence[dict], price,
                   *, stop_price=None, confirmation: str = CLOSE_CONFIRMED) -> dict:
    """算出单标的的全部技术状态。bars 按日期升序、均为已收盘日线；price 为当前价。

    `confirmation` 由调用方经 `confirmation_basis()` 从报价时段推出，决定突破/加仓
    是否算「已确认」。扩展时段传 EXTENDED_HOURS_PENDING，绝不会形成收盘突破。
    """
    price = _d(price) if price is not None else None
    n = len(bars)
    atr_val = _safe(ind.atr, bars, 14)
    atr_period_used = None
    if atr_val is None and n >= ind.MIN_ATR_BARS:
        # 降级：不足 15 根用可用根数（沿用 indicators 既有政策）
        try:
            eff = ind.effective_atr_period(n)
            atr_val = ind.atr(bars, eff)
            atr_period_used = eff
        except ind.IndicatorError:
            atr_val = None
    elif atr_val is not None:
        atr_period_used = 14

    ema = {p: _safe(ind.ema, bars, p) for p in EMA_PERIODS}
    slope_delta = {p: _safe(ind.ema_slope, bars, p, lb) for p, lb in SLOPE_LOOKBACK.items()}
    slope = {p: _slope_state(slope_delta.get(p), atr_val) for p in SLOPE_LOOKBACK}

    e20 = ema.get(20)
    dist_ema20_pct = ((price - e20) / e20) if (price is not None and e20) else None
    dist_ema20_atr = ((price - e20) / atr_val) if (price is not None and e20 is not None and atr_val) else None
    dist_stop_atr = None
    if price is not None and stop_price is not None and atr_val:
        dist_stop_atr = (price - _d(stop_price)) / atr_val

    high20 = _safe(ind.swing_high, bars, HIGH_20D) if n >= HIGH_20D else None
    dist_high20_pct = ((price - high20) / high20) if (price is not None and high20) else None
    high52 = _safe(ind.swing_high, bars, HIGH_52W) if n >= HIGH_52W else None
    dist_high52_pct = ((price - high52) / high52) if (price is not None and high52) else None

    # 周线
    wbars = weekly_bars(bars)
    wema = {p: _safe(ind.ema, wbars, p) for p in WEEKLY_PERIODS}
    wslope10 = _slope_state(_safe(ind.ema_slope, wbars, 10, 1), None)  # 周线用符号即可
    wprice = _d(wbars[-1]["c"]) if wbars else None
    weekly_trend = _weekly_trend_state(price or wprice, wema.get(10), wema.get(20),
                                       wema.get(40), wslope10 == UP)

    # RVOL：最近一根完整日线的量 / 前 RVOL_WINDOW 根均量（盘中真口径不可得，见 spec §0.3）
    rvol = None
    if n >= RVOL_WINDOW + 1:
        vols = [int(b["v"]) for b in bars]
        avg = sum(vols[-RVOL_WINDOW - 1:-1]) / RVOL_WINDOW
        if avg:
            rvol = _d(vols[-1]) / _d(avg)

    daily_trend = _daily_trend_state(price, ema, slope, atr_val) if price is not None else None
    position_state = _position_state(dist_ema20_atr)
    high20_state = _breakout_state_high20(price, high20, confirmation)
    high52_state = _state_52w(price, high52)
    volume_state = _volume_state(rvol)

    summary = _one_line(daily_trend, weekly_trend, position_state, high20_state)

    return {
        "symbol": symbol,
        "price": price,
        "bars_available": n,
        "atr14": atr_val,
        "atr14_pct": (atr_val / price) if (atr_val and price) else None,
        "atr_period_used": atr_period_used,
        "ema": ema,
        "ema_slope": slope,
        "weekly_ema": wema,
        "weekly_trend_state": weekly_trend,
        "dist_ema20_pct": dist_ema20_pct,
        "dist_ema20_atr": dist_ema20_atr,
        "dist_stop_atr": dist_stop_atr,
        "high_20d": high20,
        "dist_high_20d_pct": dist_high20_pct,
        "high_20d_state": high20_state,
        "high_52w": high52,
        "dist_high_52w_pct": dist_high52_pct,
        "high_52w_state": high52_state,
        "rvol20": rvol,
        "volume_state": volume_state,
        "daily_trend_state": daily_trend,
        "position_state": position_state,
        "confirmation": confirmation,
        "technical_summary": summary,
        "calculation_version": CALC_VERSION,
    }


def _one_line(daily, weekly, position, high20) -> str:
    """一句话技术信号：结构 + 关键位置提示。"""
    if daily is None:
        return "历史不足，未计算"
    bits = [daily]
    if position == "过度延伸":
        bits.append("短线偏高")
    elif position == "明显转弱":
        bits.append("跌破趋势线")
    if high20 in ("盘中突破", "收盘突破"):
        bits.append(high20)
    elif high20 == "接近20日高点":
        bits.append("接近突破")
    if weekly == "周线弱势" and daily in ("强多头", "多头趋势"):
        bits.append("周线未确认")
    return " · ".join(bits)


TECH_RULES_VERSION = "T1-T8/2026-08"

_SEV_INFO, _SEV_WARN, _SEV_CRIT = "info", "warn", "crit"


def evaluate_rules(card: dict, *, confirmation: str = CLOSE_CONFIRMED) -> list[dict]:
    """从已算好的状态派生 T1–T8 技术规则命中。纯函数、不落库。

    每条命中含 code/label/summary/severity/confirmation。规则只描述结构与确认，
    不产生加减仓指令——那由 decide() 结合仓位/止损裁决。
    """
    daily = card.get("daily_trend_state")
    weekly = card.get("weekly_trend_state")
    position = card.get("position_state")
    high20 = card.get("high_20d_state")
    rvol = card.get("rvol20")
    price = card.get("price")
    high52 = card.get("high_52w")
    strong_vol = rvol is not None and rvol >= Decimal("1.5")
    weekly_weak = weekly == "周线弱势"
    conf = confirmation
    out: list[dict] = []

    def hit(code, label, summary, sev, confirmation=conf):
        out.append({"code": code, "label": label, "summary": summary,
                    "severity": sev, "confirmation": confirmation})

    if daily == "强多头":
        hit("T1", "强多头排列", "强多头趋势", _SEV_INFO)
    if daily in ("多头趋势", "回踩趋势") and position == "贴近趋势线" \
            and card.get("volume_state") in ("正常", "缩量") and not weekly_weak:
        hit("T2", "健康回踩EMA20", "多头趋势中的健康回踩", _SEV_INFO)
    if position == "过度延伸":
        hit("T3", "过度延伸", "短线过度延伸，不宜追涨", _SEV_WARN)
    if high20 == "收盘突破":
        if strong_vol and not weekly_weak:
            hit("T4", "突破20日高点", "20日突破已确认", _SEV_INFO)
        else:
            hit("T4", "突破20日高点", "突破但量能不足，等待确认", _SEV_WARN)
    elif high20 == "盘后突破":
        hit("T4", "突破20日高点", "盘后突破，等待RTH收盘确认", _SEV_INFO, EXTENDED_HOURS_PENDING)
    elif high20 == "盘中突破":
        hit("T4", "突破20日高点", "盘中突破，尚未收盘确认", _SEV_INFO, INTRADAY_PENDING)
    if price is not None and high52 is not None and price > high52 \
            and daily in ("强多头", "多头趋势") and weekly == "周线多头":
        if position == "过度延伸":
            hit("T5", "突破52周高点", "52周突破，但短线过度延伸，不追涨", _SEV_WARN)
        else:
            hit("T5", "突破52周高点", "52周新高，趋势确认", _SEV_INFO)
    if daily == "短线转弱":
        hit("T6", "短线转弱", "短线转弱，中期趋势尚未破坏", _SEV_WARN)
    if daily == "中期趋势破坏" and strong_vol:
        hit("T7", "中期趋势破坏", "中期趋势破坏，检查减仓计划", _SEV_CRIT)
    if daily in ("多头趋势", "强多头") and weekly_weak:
        hit("T8", "日周线冲突", "日线反弹，周线尚未反转", _SEV_WARN)
    elif daily == "短线转弱" and weekly == "周线多头":
        hit("T8", "日周线冲突", "短线回调，长期结构尚未破坏", _SEV_INFO)
    return out


# 综合裁决优先级（固定，spec §一）：账户安全 > 仓位上限 > 操作计划 > 技术面 > 单一指标
def decide(card: dict, *, data_ok: bool, risk_breached: bool,
           over_limit: bool | None, plan_allows_add: bool,
           confirmation: str = CLOSE_CONFIRMED) -> dict:
    """把技术状态与仓位/止损/计划合成最终操作约束。返回 final_action/reason/decided_by。

    技术面再强，只要仓位超限或已破止损，都不得输出「加仓」。
    未收盘确认（盘中或扩展时段）时最多给 OBSERVE，不给 ADD_WATCH。
    """
    daily = card.get("daily_trend_state")
    position = card.get("position_state")
    summary = card.get("technical_summary")
    conf = confirmation
    confirmed = confirmation == CLOSE_CONFIRMED

    def r(action, reason, by):
        return {"final_action": action, "reason": reason, "decided_by": by,
                "confirmation": conf, "technical_summary": summary}

    if not data_ok:
        return r("HOLD_NO_ADVICE", "数据过期或历史不足，暂不生成加减仓建议", "data_quality")
    if risk_breached:
        return r("EXECUTE_STOP", "已跌破止损，优先执行止损/既定减仓计划", "hard_stop")
    if daily == "中期趋势破坏":
        return r("CHECK_REDUCE", "放量跌破EMA60，检查减仓计划", "technical")
    if over_limit:
        return r("DO_NOT_ADD", "仓位已超计划上限，不继续加仓，按计划持有或减仓", "position_limit")
    if position == "过度延伸":
        return r("DO_NOT_ADD", "短线过度延伸，不追涨", "technical")
    if daily in ("强多头", "多头趋势", "回踩趋势") and position in ("贴近趋势线", "健康趋势") \
            and plan_allows_add:
        if not confirmed:
            reason = ("盘后/盘前时段，进入加仓观察区，等待RTH收盘确认"
                      if confirmation == EXTENDED_HOURS_PENDING
                      else "进入加仓观察区，等待收盘确认")
            return r("OBSERVE", reason, "technical")
        return r("ADD_WATCH", "结构健康且仍有加仓空间，可按计划分档观察", "technical")
    if daily == "短线转弱":
        return r("HOLD", "短线转弱但中期未破，持有并观察EMA20收复", "technical")
    return r("HOLD", "结构中性，按既定计划持有", "technical")


FINAL_ACTION_LABEL = {
    "HOLD_NO_ADVICE": "暂不建议", "EXECUTE_STOP": "优先执行止损/减仓",
    "CHECK_REDUCE": "检查减仓计划", "DO_NOT_ADD": "不加仓",
    "OBSERVE": "观察，等待收盘确认", "ADD_WATCH": "可按计划分档观察",
    "HOLD": "持有",
}


#: 摘要卡片：把逐标的状态汇总成 5 类计数（spec §5.1）
def summarize(cards: Sequence[dict]) -> dict:
    def cnt(pred) -> int:
        return sum(1 for c in cards if pred(c))
    return {
        "strong": cnt(lambda c: c.get("daily_trend_state") in ("强多头", "多头趋势")
                      and c.get("weekly_trend_state") == "周线多头"),
        "pullback": cnt(lambda c: c.get("daily_trend_state") == "回踩趋势"
                        or c.get("position_state") == "贴近趋势线"),
        "breakout": cnt(lambda c: c.get("high_20d_state") in ("接近20日高点", "盘中突破", "收盘突破")
                        or c.get("high_52w_state") == "接近52周新高"),
        "extended": cnt(lambda c: c.get("position_state") == "过度延伸"),
        "broken": cnt(lambda c: c.get("daily_trend_state") == "中期趋势破坏"),
        "insufficient": cnt(lambda c: c.get("daily_trend_state") in (None, "中性")
                            and c.get("bars_available", 0) < HIGH_52W),
    }
