"""系统状态页：数据新鲜度、能力维度、watchdog/调度器/DB/PnL 健康。"""
from __future__ import annotations

import csv
import io
import json
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

from . import pnl as pnl_calc
from . import reconcile
from .config import Config
from .counting import load_decisions, trading_counts
from .decisions import decision_key
from .decisions import (
    classification_complete as classification_complete_fn,
    group_decisions, kind_summary as kind_summary_fn,
)
from .db import Database, to_decimal
from .sectors import load_sector_map

from .market_hours import market_session, session_label, next_open_beijing
from .webformat import (
    DISPLAY_TZ, QUALITY_LABEL, _QUOTE_PLAIN, _account, _stat, abs_time,
    freshness, rel_time,
)


def _has_col(row, name: str) -> bool:
    """sqlite3.Row 没有 .get()，且老库可能还没补上这一列。

    直接 row[name] 在缺列时抛 IndexError —— 状态栏不该因为一个新列没迁移完
    就整条崩掉，所以先问一句。
    """
    try:
        return name in row.keys()
    except Exception:  # noqa: BLE001 — dict 或其他行类型也要能用
        return isinstance(row, dict) and name in row


def data_status_bar(cfg: Config, db: Database, now: datetime | None = None) -> dict:
    """全局数据状态栏：每一路数据各自的时点与新鲜度。"""
    now = now or datetime.now(timezone.utc)
    tz = cfg.market_tz
    bal = db.latest_balances()
    pos = db.latest_positions()
    run = db.latest_rule_run()
    health = db.get_sync_state("data_health") or "UNKNOWN"

    quote_as_of, quality_counts = None, {}
    for r in pos:
        q = r["price_quality"]
        if q:
            quality_counts[q] = quality_counts.get(q, 0) + 1
        if r["quote_as_of"] and (quote_as_of is None or r["quote_as_of"] < quote_as_of):
            quote_as_of = r["quote_as_of"]

    # 行情摘要：说人话。用户要知道的是「这价能不能信」，不是枚举名。
    if quality_counts:
        if len(quality_counts) == 1:
            quote_extra = _QUOTE_PLAIN.get(next(iter(quality_counts)), "行情状态未知")
        else:
            quote_extra = "、".join(f"{QUALITY_LABEL.get(k, (k,))[0]}{v}个"
                                   for k, v in sorted(quality_counts.items()))
    else:
        quote_extra = "暂无行情"

    # 请求的行情类型 vs 实际生效的类型。两者不一致 = 订阅没生效（例如免费非综合
    # 实时还没开通或协议未签），系统已自动回退。这件事必须让人看见 ——
    # 否则会以为自己在用实时行情，实际一直是延迟的。
    req_types = {r["requested_market_data_type"] for r in pos
                 if _has_col(r, "requested_market_data_type")
                 and r["requested_market_data_type"] is not None}
    eff_types = {r["effective_market_data_type"] for r in pos
                 if _has_col(r, "effective_market_data_type")
                 and r["effective_market_data_type"] is not None}
    quote_downgraded = bool(req_types and eff_types and req_types != eff_types)
    if quote_downgraded:
        quote_extra += "（已请求实时但未生效，自动回退）"

    session = market_session(now, tz)
    gw_css = {"READY": "ok", "WARMING_UP": "warn", "DEGRADED": "warn"}.get(health, "crit")
    gw_label = {"READY": "已连接", "WARMING_UP": "数据预热中",
                "DEGRADED": "降级运行", "ERROR": "同步失败"}.get(health, "状态未知")
    caps = capabilities(cfg, db, now, quality_counts=quality_counts, run=run,
                        health=health, session=session)

    entries = [
        _stat("账户数据", bal["ts"] if bal else None, now, tz, warn_after=180, crit_after=600),
        _stat("持仓数据", pos[0]["ts"] if pos else None, now, tz, warn_after=180, crit_after=600),
        # 行情：收盘价回退时 quote_as_of 为 None，此时不谈新鲜度，只报质量
        _stat("行情", quote_as_of, now, tz, warn_after=300, crit_after=1800,
              extra=quote_extra),
        _stat("成交同步", db.get_sync_state("executions_last_sync"), now, tz,
              warn_after=300, crit_after=1800),
        _stat("规则评估", run["run_ts"] if run else None, now, tz,
              warn_after=180, crit_after=600),
    ]
    if quote_as_of is None and quality_counts:
        # 全部走收盘价：没有可信行情时点，绝不显示成「刚刚更新」
        entries[2].update({"abs": "盘后", "rel": "非实时价格", "css": "warn"})
    # 休市（周末/夜间）行情本就不动，"未更新"是正常的，不该标红报过期。
    # 同样，账户/持仓/PnL 在休市时低频更新，也放宽到 warn 上限。
    from .market_hours import CLOSED
    if session == CLOSED:
        for it in entries:
            if it["css"] == "crit":
                it["css"] = "warn"
        next_open = next_open_beijing(now, tz, DISPLAY_TZ)
        market_note = f"下次开盘 {next_open}"      # 不写「休市中」，只给开盘时间
    else:
        market_note = session_label(session)
    # 导航栏圆点：取各能力里最差的一个 —— 任何一项降级都应该在每一页都看得见
    # Hyperliquid 是可选旁路；其故障必须在独立卡展示，不能把 IBKR overall 拉红。
    cap_states = [c["state"] for c in caps if c.get("key") != "hyperliquid"]
    overall = ("crit" if "DOWN" in cap_states
               else ("warn" if ("PARTIAL" in cap_states or "UNKNOWN" in cap_states) else "ok"))
    return {
        "gateway": {"label": gw_label, "css": gw_css, "health": health},
        "overall_css": overall,
        "market_session": session,
        "market_session_label": session_label(session),
        "market_note": market_note,
        # 键名刻意不叫 items：Jinja 里 status.items 会解析成 dict.items 方法
        "entries": entries,
        # 能力维度：整体一个 READY 会掩盖「行情只有收盘价、R7 根本没判」这种事
        "capabilities": caps,
        # 技术标识与系统健康收进详情层：日常看板不需要盯着一串 hex
        "detail": {
            "sync_run_id": db.get_sync_state("last_sync_run_id"),
            "data_health": health,
            "quote_quality": quality_counts,
            "quote_as_of": quote_as_of,
            # 免费非综合实时是否已生效。requested != effective 就是没生效。
            "quote_requested_type": sorted(req_types) or None,
            "quote_effective_type": sorted(eff_types) or None,
            "quote_downgraded": quote_downgraded,
            "system": system_health(cfg, db, now),
        },
    }


# ---- 七、能力维度：连接 / 账户数据 / 行情能力 / 规则能力 ----

_CAP_LABEL = {"OK": "正常", "PARTIAL": "部分可用", "DOWN": "不可用", "UNKNOWN": "未知"}


def capabilities(cfg: Config, db: Database, now: datetime, *, quality_counts: dict,
                 run, health: str, session: str | None = None) -> list[dict]:
    """把「整体 READY」拆成四个能力。

    盘后行情只有收盘冻结价时，连接和账户数据都正常，但 **R7 根本没执行判断** ——
    此时报整体 READY 是误导，规则能力必须显示 PARTIAL。
    """
    # 连接健康
    conn_state = {"READY": "OK", "WARMING_UP": "PARTIAL", "DEGRADED": "PARTIAL"}.get(health, "DOWN")
    wd_healthy = db.get_sync_state("watchdog_healthy")
    if wd_healthy == "0":
        conn_state = "DOWN"

    # 账户数据健康
    bal = db.latest_balances()
    acct_state = "OK" if bal and freshness(bal["ts"], now, warn_after=180,
                                           crit_after=600) != "crit" else "DOWN"
    if bal and freshness(bal["ts"], now, warn_after=180, crit_after=600) == "warn":
        acct_state = "PARTIAL"

    # 行情能力：能否拿到可用于判断的实时/延迟价
    live = sum(v for k, v in quality_counts.items()
               if k in ("REALTIME", "REALTIME_NONCONSOLIDATED", "DELAYED"))
    total = sum(quality_counts.values())
    if total == 0:
        quote_state, quote_note = "DOWN", "无行情数据"
    elif live == total:
        quote_state, quote_note = "OK", "全部为可判定行情"
    elif live == 0:
        # 休市时只有收盘价是正常的，不算能力降级
        from .market_hours import CLOSED
        if session == CLOSED:
            quote_state, quote_note = "OK", "休市中，收盘价为当前有效价格"
        else:
            quote_state, quote_note = "PARTIAL", "仅收盘/冻结价，可展示但不能用于止损判断"
    else:
        quote_state, quote_note = "PARTIAL", f"{live}/{total} 为可判定行情"

    # 规则能力：以最近一次评估的实际结论为准
    rule_state, rule_note = "UNKNOWN", "尚未评估"
    if run is not None:
        try:
            summary = json.loads(run["rule_summary"] or "{}")
        except (TypeError, ValueError):
            summary = {}
        not_eval = summary.get("not_evaluated", 0)
        errors = summary.get("error", 0)
        if errors:
            rule_state, rule_note = "DOWN", f"{errors} 条规则运行异常"
        elif not_eval:
            from .market_hours import CLOSED
            if session == CLOSED:
                rule_state = "OK"
                rule_note = "休市中，止损穿越判断暂停（收盘价不参与），其余规则正常"
            else:
                rule_state = "PARTIAL"
                rule_note = f"{not_eval} 条规则未判定（如 R7 仅有收盘冻结价）"
        else:
            rule_state, rule_note = "OK", "全部规则已判定"

    result = [
        {"key": "connection", "label": "连接健康", "state": conn_state,
         "state_label": _CAP_LABEL[conn_state], "note": db.get_sync_state("watchdog_detail") or ""},
        {"key": "account", "label": "账户数据", "state": acct_state,
         "state_label": _CAP_LABEL[acct_state],
         "note": abs_time(bal["ts"], cfg.market_tz) if bal else "无数据"},
        {"key": "quotes", "label": "行情能力", "state": quote_state,
         "state_label": _CAP_LABEL[quote_state], "note": quote_note},
        {"key": "rules", "label": "规则能力", "state": rule_state,
         "state_label": _CAP_LABEL[rule_state], "note": rule_note},
    ]
    # 链上旁路只读自己的库和最近采集批次；这里不调用网络，也不创建缺失的数据库文件。
    if getattr(cfg, "hyperliquid_enabled", False):
        try:
            from .hyperliquid_db import HyperliquidDatabase
            hl = HyperliquidDatabase(getattr(cfg, "hl_db_path", "./data/hyperliquid.db"))
            health = hl.health()
            if not health.get("available"):
                chain_state, chain_note = "DOWN", f"{health.get('status', '不可用')}（不影响主链路）"
            else:
                run = health.get("latest_run") or {}
                run_status = str(run.get("status") or "").upper()
                finished = run.get("finished_at")
                age = None
                if finished:
                    try:
                        dt = datetime.fromisoformat(str(finished).replace("Z", "+00:00"))
                        if dt.tzinfo is None:
                            dt = dt.replace(tzinfo=timezone.utc)
                        age = max(0, (now - dt.astimezone(timezone.utc)).total_seconds())
                    except ValueError:
                        pass
                if run_status in {"ERROR", "FAILED"}:
                    chain_state, chain_note = "DOWN", "最近采集失败"
                elif age is not None and age > 900:
                    chain_state, chain_note = "PARTIAL", f"最近采集 {int(age)} 秒前"
                else:
                    chain_state, chain_note = "OK", "独立库可读，采集正常"
            result.append({"key": "hyperliquid", "label": "链上信号", "state": chain_state,
                           "state_label": {"OK": "正常", "PARTIAL": "部分可用",
                                            "DOWN": "不可用"}.get(chain_state, chain_state),
                           "note": chain_note})
        except Exception:  # noqa: BLE001 — 状态卡不可影响主状态页
            result.append({"key": "hyperliquid", "label": "链上信号", "state": "DOWN",
                           "state_label": "不可用", "note": "链上健康读取失败（不影响主链路）"})
    return result


def system_health(cfg: Config, db: Database, now: datetime) -> dict:
    """系统健康详情：watchdog / scheduler / DB 写入 / PnL 订阅 / 积压 / 恢复。"""
    tz = cfg.market_tz
    bal = db.latest_balances()
    account = _account(cfg, db)
    p = db.latest_pnl(account)

    def _pair(ts):
        return {"abs": abs_time(ts, tz), "rel": rel_time(ts, now)} if ts else None

    def _recent(ts, hours: float = 1) -> bool:
        """该时点是否发生在最近 N 小时内。

        用来区分「此刻正在发生」与「历史留痕」：错过执行数/最近异常都是**累计**记录，
        永不归零。若一律标红，一次早已过去的 gateway 登出会让状态页天天挂假红灯。
        """
        if not ts:
            return False
        try:
            return (now - datetime.fromisoformat(ts)).total_seconds() < hours * 3600
        except (TypeError, ValueError):
            return False

    missed_ts = db.get_sync_state("scheduler_last_missed")
    error_ts = db.get_sync_state("scheduler_last_error")

    return {
        "watchdog": {
            "state": db.get_sync_state("watchdog_state") or "未知",
            "healthy": db.get_sync_state("watchdog_healthy"),
            "last_check": _pair(db.get_sync_state("watchdog_last_check")),
            "detail": db.get_sync_state("watchdog_detail") or "",
            "fail_count": db.get_sync_state("watchdog_fail_count") or "0",
            "restarts_24h": db.get_sync_state("watchdog_restarts_24h") or "0",
            "last_restart": _pair(db.get_sync_state("watchdog_last_restart")),
        },
        "scheduler": {
            "last_success": _pair(db.get_sync_state("scheduler_last_success")),
            "last_job": db.get_sync_state("scheduler_last_job") or "—",
            "last_error": _pair(error_ts),
            "last_error_job": db.get_sync_state("scheduler_last_error_job"),
            "missed_count": db.get_sync_state("scheduler_missed_count") or "0",
            "last_missed": _pair(missed_ts),
            # 只有最近 1 小时内真的发生过，才当成「正在发生的问题」标红
            "missed_recent": _recent(missed_ts),
            "error_recent": _recent(error_ts),
        },
        "database": {"last_write": _pair(bal["ts"] if bal else None)},
        "pnl": {
            "quality": p["quality"] if p else "UNAVAILABLE",
            "source": p["source"] if p else "—",
            "as_of": _pair(p["as_of"] if p else None),
            "reset_policy": p["reset_policy"] if p else "—",
        },
    }



# ---- 数据状态区（账户快照层 / 数据质量层的页面出口）----

def data_state_panel(cfg: Config, db: Database, now: datetime | None = None) -> dict:
    """页面顶部的数据状态区。

    这是「页面不得在数据过期时继续显示实时账户数据」这条纪律的落点：
    状态由**此刻**的新鲜度与落库时的质量结论共同决定，两者取更差的一个。
    """
    from . import data_quality as dq
    from .market_hours import CLOSED, market_session
    now = now or datetime.now(timezone.utc)
    row = db.latest_snapshot_row()
    latest_run = db.latest_sync_run()
    last_ok = db.last_successful_sync_run()

    if row is None:
        return {
            "status": dq.ERROR, "label": "数据不可用", "icon": "⛔",
            "css": "crit", "data_as_of": None, "data_as_of_abs": "—",
            "age_text": "无任何快照", "ai_allowed": False,
            "ai_reason": "尚无账户快照，无法进行分析",
            "issues": [], "critical": 0, "warning": 0,
            "last_success_abs": None, "last_sync_state": None,
            "connected": False, "snapshot_id": None,
            "note": "系统尚未成功抓取过账户数据",
        }

    row = dict(row)
    th = dq.thresholds_from_config(cfg)
    market_open = market_session(now, cfg.market_tz) != CLOSED
    live = dq.check_freshness(row.get("ts"), now, market_open=market_open, th=th)
    stored = row.get("quality_status") or dq.FRESH
    live_status = (dq.ERROR if any(i.severity == dq.CRITICAL for i in live)
                   else (dq.STALE if live else dq.FRESH))
    rank = {dq.FRESH: 0, dq.STALE: 1, dq.ERROR: 2}
    status = stored if rank.get(stored, 0) >= rank[live_status] else live_status

    issues = db.quality_issues_for(row["snapshot_id"]) if row.get("snapshot_id") else []
    issues = issues + [i.as_dict() for i in live]      # 把「此刻才过期」也并进来
    crit = sum(1 for i in issues if i["severity"] == dq.CRITICAL)
    warn = sum(1 for i in issues if i["severity"] == dq.WARNING)

    ai_allowed = bool(row.get("ai_analysis_allowed")) and status != dq.ERROR
    if ai_allowed:
        ai_reason = "数据完整且新鲜，可用于分析" if not warn else \
            f"可用于分析，但有 {warn} 项警告需一并告知"
    elif status == dq.ERROR:
        ai_reason = "数据存在严重问题，禁止输出确定性结论"
    else:
        ai_reason = "数据质量不满足分析门槛"

    label, icon, css = {
        dq.FRESH: ("数据正常", "✅", "ok"),
        dq.STALE: ("数据可能已过期", "⚠️", "warn"),
        dq.ERROR: ("数据不可用于可靠分析", "⛔", "crit"),
    }[status]

    age = None
    try:
        age = (now - datetime.fromisoformat(row["ts"])).total_seconds()
    except (TypeError, ValueError, KeyError):
        pass
    if age is None:
        age_text = "时点未知"
    elif age < 90:
        age_text = f"{int(age)} 秒前"
    elif age < 5400:
        age_text = f"{age / 60:.0f} 分钟前"
    else:
        age_text = f"{age / 3600:.1f} 小时前"

    lr = dict(latest_run) if latest_run else {}
    gw = db.get_sync_state("watchdog_healthy")
    return {
        "status": status, "label": label, "icon": icon, "css": css,
        "snapshot_id": row.get("snapshot_id"),
        "data_as_of": row.get("ts"),
        "data_as_of_abs": abs_time(row.get("ts"), cfg.market_tz),
        "age_text": age_text,
        "market_open": market_open,
        "stale_threshold_min": int((th.stale_seconds_market if market_open
                                    else th.stale_seconds_closed) / 60),
        "ai_allowed": ai_allowed, "ai_reason": ai_reason,
        "issues": issues, "critical": crit, "warning": warn,
        "last_sync_state": lr.get("run_state") or lr.get("status"),
        "last_sync_error": lr.get("error_message"),
        "last_success_abs": (abs_time(dict(last_ok)["finished_at"], cfg.market_tz)
                             if last_ok and dict(last_ok).get("finished_at") else None),
        "connected": gw != "0",
    }


# ---- 风险区域（风险计算引擎的页面出口）----

def risk_panel(cfg: Config, db: Database, now: datetime | None = None) -> dict | None:
    """概览页风险区域数据。评估失败或无快照返回 None，页面据此隐藏该区。"""
    from . import risk_service
    now = now or datetime.now(timezone.utc)
    try:
        a = risk_service.assess(cfg, db, now=now)
    except Exception:  # noqa: BLE001 — 风险区渲染失败不得让整页 500
        return None
    if a is None:
        return None
    m = a.get("calculated_metrics", {})
    acct, conc, dd, liq = (m.get("account", {}), m.get("concentration", {}),
                           m.get("drawdown", {}), m.get("liquidity", {}))

    def s(v, kind="ratio"):
        """Decimal/字符串 -> 展示串。None 一律显示「—」，绝不显示 0。"""
        if v is None or v == "None":
            return "—"
        try:
            dv = Decimal(str(v))
        except Exception:  # noqa: BLE001
            return str(v)
        if kind == "ratio":
            return f"{dv:.1%}"
        if kind == "multiple":
            return f"{dv:.2f}x"
        if kind == "index":
            return f"{dv:.2f}"
        if kind == "count":
            return f"{dv:.2f}"
        return f"{dv:,.2f}"

    label, icon, css = {
        "low": ("低风险", "✅", "ok"),
        "moderate": ("中等风险", "🟡", "warn"),
        "high": ("高风险", "⚠️", "warn"),
        "critical": ("严重风险", "🔴", "crit"),
        "blocked": ("无法判断", "⛔", "crit"),
    }.get(a["risk_level"], ("未知", "❔", "warn"))

    high_count = sum(1 for e in a.get("events", [])
                     if e.get("severity") in ("high", "critical"))
    return {
        "level": a["risk_level"], "level_label": label, "icon": icon, "css": css,
        "score": a.get("risk_score"), "reliable": a["reliable"],
        "blocked_reason": a.get("blocked_reason"),
        "data_as_of_abs": abs_time(a["data_as_of"], cfg.market_tz),
        "snapshot_id": a["snapshot_id"],
        "component_scores": a.get("component_scores", {}),
        "cash_ratio": s(acct.get("cash_ratio")),
        "gross_leverage": s(acct.get("gross_leverage"), "multiple"),
        "max_position_equity": s(conc.get("max_position_weight_of_equity")),
        "max_position_invested": s(conc.get("max_position_weight_of_invested")),
        "top3_invested": s(conc.get("top3_weight_of_invested")),
        # ---- 集中度上下文：让「为什么 Top3 不算数」一眼可见 ----
        "conc": {
            "total_position_count": conc.get("total_position_count"),
            "eligible_long": conc.get("eligible_long_position_count"),
            "eligible_short": conc.get("eligible_short_position_count"),
            "excluded_count": conc.get("excluded_count") or 0,
            "excluded": conc.get("excluded_positions") or [],
            "hhi": s(conc.get("hhi"), "index"),
            "effective_count": s(conc.get("effective_position_count"), "count"),
            "top1": s(conc.get("top1_weight")),
            "top3": s(conc.get("top3_weight")),
            "top5": s(conc.get("top5_weight")),
            "dominance_gap": s(conc.get("dominance_gap")),
            **(a.get("concentration_applicability") or {}),
            "normalization_method": a.get("score_normalization_method"),
        },
        "current_drawdown": s(dd.get("current_drawdown")),
        "maint_safety": s(liq.get("maint_margin_safety_multiple"), "multiple"),
        "high_risk_count": high_count,
        "drivers": a.get("top_risk_drivers", [])[:5],
        "positions": sorted(
            m.get("positions", []),
            key=lambda p: Decimal(str(p.get("weight_of_equity") or 0)), reverse=True),
        "ai_allowed": a["ai_analysis_allowed"],
        "calc_version": a.get("calculation_version"),
        "fmt": s,
        # 单一持仓占总资产的超限阈值（供当前持仓表「占总资产」列高亮，阈值不写死在模板）
        "weight_equity_max": Decimal(str(getattr(
            cfg, "risk_single_weight_equity_max", "0.30"))),
    }


# ---- 市场环境模块（市场环境引擎的页面出口）----

def _market_state(inst: dict, now: datetime, session: str, tz,
                  max_lag_days: int = 4) -> dict:
    """市场标的的「新鲜度 × 来源」组合状态。

    修正老 bug：以前一律标 fresh/正常。区分三类：
      · 有实时报价源（富途股票/ETF）：按报价分钟数判 实时/延迟/过期；
      · IBKR 指数盘中报价：按 marketDataType 如实标「实时」或「延迟」；
      · 只有日线的指数（IBKR 的 VIX/VXN/US10Y）：本就日频，标「收盘价·来源」而非
        「正常」（不骗人），仅当数据落后超过容忍天数才判「过期」。
    这样既不再把过期当正常，也不会对日频指数在盘中天天误报。
    """
    from datetime import datetime as _dt, date as _date
    from .market_hours import CLOSED
    src = inst.get("source") or ""
    src_label = ("富途" if src.startswith("FUTU") else
                 ("IBKR" if src.startswith("IBKR_reqMktData") else
                  ("IBKR回退" if src else "")))
    quote_as_of = inst.get("quote_as_of")
    data_as_of = inst.get("data_as_of")

    if quote_as_of:                                    # 盘中报价：按分钟与类型判
        try:
            age = (now - _dt.fromisoformat(quote_as_of)).total_seconds()
        except (TypeError, ValueError):
            age = None
        if inst.get("delayed"):
            if age is not None and age > 1800:
                return {"label": "过期", "source": src_label, "css": "crit"}
            return {"label": "延迟", "source": src_label, "css": "warn"}
        if session == CLOSED:
            return {"label": "收盘价", "source": src_label, "css": "ok"}
        if age is None or age <= 600:
            return {"label": "实时", "source": src_label, "css": "ok"}
        if age <= 1800:
            return {"label": "延迟", "source": src_label, "css": "warn"}
        return {"label": "过期", "source": src_label, "css": "crit"}

    # 无实时报价 = 日线口径（IBKR 指数）
    if not src or inst.get("quality_status") in ("missing", "error"):
        return {"label": "无行情", "source": src_label, "css": "crit"}
    try:
        lag = (now.astimezone(tz).date() - _date.fromisoformat(data_as_of)).days
    except (TypeError, ValueError):
        lag = None
    if lag is not None and lag > max_lag_days:         # 真过期（覆盖长周末+假期）
        return {"label": "过期", "source": src_label, "css": "crit"}
    # 容忍范围内：如实标「收盘价」，不再冒充「正常」
    return {"label": "收盘价", "source": src_label, "css": "ok"}


def market_panel(cfg: Config, db: Database, now: datetime | None = None) -> dict | None:
    """概览页市场环境区数据。评估失败返回 None，页面隐藏该区。"""
    from . import market_engine as me
    from . import market_service
    now = now or datetime.now(timezone.utc)
    try:
        a = market_service.assess(cfg, db, now=now)
        acc = None
        try:
            from . import risk_service
            acc = risk_service.assess(cfg, db, now=now)
        except Exception:  # noqa: BLE001
            acc = None
        combined = me.combined_view(acc, a, cfg=cfg)
    except Exception:  # noqa: BLE001 — 市场区渲染失败不得让整页 500
        return None
    if a is None:
        return None

    m = a.get("calculated_metrics", {}) or {}
    inst = (m.get("data", {}) or {}).get("instruments", {}) or {}

    def show(v, kind="ratio"):
        if v is None or v == "None":
            return "—"
        try:
            dv = Decimal(str(v))
        except Exception:  # noqa: BLE001
            return str(v)
        if kind == "ratio":
            return f"{dv:+.1%}"
        if kind == "level":
            return f"{dv:.2f}"
        return str(dv)

    label, icon, css = {
        "supportive": ("环境支持", "✅", "ok"),
        "neutral": ("中性", "⚪", "ok"),
        "cautious": ("需谨慎", "⚠️", "warn"),
        "stressed": ("市场承压", "🔴", "crit"),
        "blocked": ("无法判断", "⛔", "crit"),
    }.get(a["market_state"], ("未知", "❔", "warn"))

    qqq, sox, vol, rates = (m.get("qqq", {}), m.get("sox", {}),
                            m.get("vol", {}), m.get("rates", {}))
    live_as_of = (m.get("data", {}) or {}).get("live_as_of")

    def yield_percent(value) -> str:
        if value is None:
            return "—"
        number = Decimal(str(value))
        if abs(number) > 10:  # TNX/IBKR 报价是收益率 ×10
            number /= 10
        return f"{number:.3f}%"

    drivers = []
    for driver in a.get("top_market_drivers", [])[:5]:
        item = dict(driver)
        try:
            actual = Decimal(str(item.get("actual_value")))
            threshold = Decimal(str(item.get("threshold_value")))
        except Exception:  # noqa: BLE001
            actual = threshold = None
        # IBKR/TNX uses yield ×10 (46.7 means 4.670%). Keep engine storage and
        # thresholds unchanged, but never expose the broker index unit to users.
        if (item.get("affected_instrument") == "US10Y" and actual is not None
                and threshold is not None and abs(actual) > 10 and abs(threshold) > 10):
            item["actual_display"] = f"{actual / 10:.3f}%"
            item["threshold_display"] = f"{threshold / 10:.3f}%"
        drivers.append(item)

    session = market_session(now, cfg.market_tz)
    _rank = {"ok": 0, "warn": 1, "crit": 2}
    _inst_out, _mkt_worst = {}, "ok"
    for code, v in inst.items():
        q = _market_state(v, now, session, cfg.market_tz, cfg.market_max_lag_days)
        _inst_out[code] = {
            **v, "q": q,
            "time_label": (abs_time(v.get("quote_as_of"), cfg.market_tz)
                           if v.get("quote_as_of") else
                           (f"{v.get('data_as_of')} 收盘" if v.get("data_as_of") else "—")),
            "close": (yield_percent(v.get("close")) if code == "US10Y"
                      else show(v.get("close"), "level")),
            "daily_change_percent": show(v.get("daily_change_percent"))}
        if _rank[q["css"]] > _rank[_mkt_worst]:
            _mkt_worst = q["css"]

    return {
        "state": a["market_state"], "label": label, "icon": icon, "css": css,
        "score": a.get("market_risk_score"), "reliable": a["reliable"],
        "blocked_reason": a.get("blocked_reason"),
        "data_as_of": live_as_of or a.get("data_as_of") or "—",
        "quality_status": a.get("quality_status"),
        "qqq_vs_ma20": show(qqq.get("vs_ma20")), "qqq_vs_ma50": show(qqq.get("vs_ma50")),
        "qqq_vs_ma200": show(qqq.get("vs_ma200")),
        "sox_vs_qqq": show(m.get("sox_vs_qqq_20d")),
        "vix": show(vol.get("vix_current"), "level"),
        "vix_chg": show(vol.get("vix_change_1d")),
        "vxn": show(vol.get("vxn_current"), "level"),
        "us10y": yield_percent(rates.get("current")),
        "drivers": drivers,
        # 逐项格式化后再进模板。**原始值是 28 位小数**（-0.0097196974139…），
        # 直接渲染会在表格里拖出一长串数字。其余字段都走了 show()，
        # 只有这张表当初漏了。
        "instruments": _inst_out,
        "data_css": _mkt_worst,        # 全部标的里最差的新鲜度，供顶部能力汇总
        "combined": combined,
        "calc_version": a.get("calculation_version"),
    }
