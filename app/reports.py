"""每日推送报告：盘前仓位体检、盘后成交清单。

构建纯文本（飞书友好），读 DB 快照。盘前的「违规项」由调用方传入当前规则命中，
盘后的「触发规则」读当日已记录的 rule_events。
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from .config import Config
from .db import Database, to_decimal


def _money(v) -> str:
    if v is None:
        return "-"
    return f"{Decimal(str(v)):,.2f}"


def _pct(v) -> str:
    if v is None:
        return "-"
    return f"{Decimal(str(v)) * 100:.1f}%"


_REC_ACTION_LABELS = {
    "EXIT": "退出", "REDUCE": "减仓", "DO_NOT_ADD": "禁止加仓",
    "HOLD": "持有", "ADD_WATCH": "观察加仓", "REVIEW": "复盘",
    "BLOCKED": "数据受阻",
}


def _recommendation_line(row) -> str:
    label = _REC_ACTION_LABELS.get(row["action"], row["action"])
    quantity = f" {row['quantity']} 股" if row["quantity"] else ""
    reason = f"：{row['reason']}" if row["reason"] else ""
    return (f"  合成建议：{label}{quantity}{reason}"
            f"（recommendation_id:{row['id']}）")


def build_morning_report(cfg: Config, db: Database, now: datetime, hits: list | None = None) -> tuple[str, str]:
    date_et = now.astimezone(cfg.market_tz).strftime("%Y-%m-%d")
    lines = [f"【盘前仓位体检 · {date_et} ET】"]

    cd = db.active_cooldown(now.isoformat())
    if cd:
        ends = datetime.fromisoformat(cd["ends_at"]).astimezone(cfg.market_tz)
        rule_ref = str(cd["trigger_ref"] or "R6").split(":", 1)[0]
        lines.append(
            f"🔴 冷静期进行中（至 {ends:%m-%d %H:%M}），请谨慎交易"
            f"（规则引用：{rule_ref}）")

    bal = db.latest_balances()
    if bal:
        mb = to_decimal(bal["margin_balance"])
        margin_flag = " ⚠️动用融资" if mb is not None and mb < 0 else ""
        lines.append(
            f"净资产 {_money(bal['equity'])} | 现金 {_money(bal['cash'])} | "
            f"Margin {_money(mb)}{margin_flag}"
        )

    stops = {r["symbol"]: r for r in db.latest_stop_levels()}
    recommendations = {r["symbol"]: r for r in db.current_recommendations(
        getattr(cfg, "ib_account", None) or "")}
    positions = db.latest_positions()
    if not positions:
        lines.append("（当前无持仓）")
    else:
        lines.append("—— 持仓 · 距建议止损 · 风险占比 ——")
        for p in positions:
            sym = p["symbol"]
            qty = to_decimal(p["qty"])
            price = (to_decimal(p["market_value"]) / qty) if qty != 0 else None
            s = stops.get(sym)
            if s and price is not None:
                stop = to_decimal(s["stop_price"])
                side = s["side"]
                dist = ((price - stop) / price) if side == "LONG" else ((stop - price) / price)
                risk_pct = _pct(s["risk_pct"])
                flag = " 🔴风险>2%" if s["exceeds_2pct"] else ""
                tp = " 🟢已达1R" if s["reached_1r"] else ""
                weak = "" if s["data_sufficient"] else (
                    f" ⚠️{s['data_note'] or '数据不足，仅供参考'}"
                )
                lines.append(
                    f"· {sym}({side}) 现价{_money(price)} 止损{_money(stop)} "
                    f"距止损{_pct(dist)} 风险{risk_pct}{flag}{tp}{weak}（规则引用：R7）"
                )
            else:
                lines.append(
                    f"· {sym} 现价{_money(price)} ⚠️无止损位（日线不足 5 根，未计算，风险未纳入统计）"
                )
            recommendation = recommendations.get(sym)
            if recommendation is not None:
                lines.append(_recommendation_line(recommendation))

    hits = hits or []
    if hits:
        lines.append("—— 当前违规项 ——")
        for h in hits:
            lines.append(f"· [{h.rule_id}] {h.title}")
    else:
        lines.append("—— 无触发的风控规则 ——")

    return f"📋 盘前仓位体检 {date_et}", "\n".join(lines)


def build_evening_report(cfg: Config, db: Database, now: datetime) -> tuple[str, str]:
    date_et = now.astimezone(cfg.market_tz).strftime("%Y-%m-%d")
    tz = cfg.market_tz
    day_start = now.astimezone(tz).replace(hour=0, minute=0, second=0, microsecond=0)
    day_start_utc = day_start.astimezone(timezone.utc).isoformat()

    lines = [f"【盘后小结 · {date_et} ET】"]

    fills = db.filled_orders_since(day_start_utc)
    if fills:
        lines.append(f"—— 当日成交（{len(fills)} 笔）——")
        for o in fills:
            lines.append(
                f"· {o['symbol']} {o['side']} {_money(o['qty'])}@{_money(o['price'])}"
            )
    else:
        lines.append("—— 当日无成交 ——")

    events = [e for e in db.recent_rule_events(200) if e["ts"] >= day_start_utc]
    if events:
        lines.append("—— 当日触发的规则 ——")
        for e in events:
            lines.append(f"· [{e['rule_id']}] {e['title']}")
    else:
        lines.append("—— 当日无风控告警 ——")

    lines.append("—— 别忘了补填今日复盘表 ——")
    return f"🌙 盘后小结 {date_et}", "\n".join(lines)
