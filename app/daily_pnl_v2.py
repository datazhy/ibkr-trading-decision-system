"""当日盈亏 v2 切换控制：双顺序、自检、版本与模式声明。

这个模块**只负责「用哪条路」**，不负责计算。计算在
`daily_pnl_observation`（纯函数）与 `daily_pnl_service`（落库）里。

## 为什么需要双顺序

现行管道的顺序是：写账户/持仓快照 → 跑风险评估 → 写 PnL 快照。
风险评估跑在 PnL 写入**之前**，所以它拿不到本轮的分子，历史上用
`latest_pnl()` 抓上一周期的分子去配本周期的分母 —— 这是
`ACC_DAILY_LOSS_HIGH` 在 24 小时里开合 11 次的直接成因。

v2 把顺序改成：快照 → PnL 快照 → 观测 → 风险评估，让分子分母天然同批次。

**但顺序不能无条件改。** 影子期必须让旧路一字不动地继续驱动线上事件，
否则就没有干净的对照。所以两条顺序并存，由 `DAILY_PNL_V2_ENABLED` 选择：

    off（生产现状）: snapshot -> assess(old) -> pnl_snapshot -> 观测/比较（影子）
    on             : snapshot -> pnl_snapshot -> 观测 -> assess(v2, 注入观测)

## 为什么自检必须硬失败

v2 依赖若干张表和列。缺任何一项时，**静默退回旧路是最坏的选择** ——
运维以为切过去了，实际还在跑旧口径，而且没人知道。所以自检不通过就
明确拒绝启用 v2 并大声报错，让人去修，而不是假装成功。

自检拒绝 ≠ 规则停摆：拒绝的是「以 v2 模式运行」，旧路照常工作。
"""
from __future__ import annotations

import logging

log = logging.getLogger("riskdash")

# ---------------- 模式 ----------------
MODE_OLD = "old_latest_pnl"
MODE_V2 = "daily_pnl_v2_atomic"

#: 执行顺序。名字直接说明「评估相对 PnL 写入」在前还是在后。
ORDER_ASSESS_BEFORE_PNL = "assess_then_pnl"      # 旧路（现状）
ORDER_PNL_BEFORE_ASSESS = "pnl_then_assess"      # v2

# ---------------- 版本（**只在 v2 开启时使用**）----------------
#: v2 的当日盈亏口径版本。开关关闭时不出现在任何落库记录里。
DAILY_PNL_VERSION_V2 = "daily-pnl-v2-atomic/2026-07"
#: v2 生效时的风险计算版本。**注意**：它只换算法标签，不换规则内容哈希 ——
#: 阈值与规则定义一个字都没动，所以 episode 不该因此断开（见
#: `risk_events.rule_semantics_key`）。
CALC_VERSION_V2 = "risk-v3-daily-pnl-atomic/2026-07"


def enabled(cfg) -> bool:
    return bool(getattr(cfg, "daily_pnl_v2_enabled", False))


def mode(cfg) -> str:
    return MODE_V2 if enabled(cfg) else MODE_OLD


def order_mode(cfg) -> str:
    return ORDER_PNL_BEFORE_ASSESS if enabled(cfg) else ORDER_ASSESS_BEFORE_PNL


def active_calculation_version(cfg) -> str | None:
    """v2 生效时的风险计算版本；关闭时返回 None（表示「用现有版本，别动」）。

    刻意不在这里 import risk_engine 的 CALC_VERSION —— 返回 None 让调用方
    保持它自己的默认值，这样「开关关闭时不产生新版本评估」是**结构保证**，
    而不是靠这里恰好抄对了那个字符串。
    """
    return CALC_VERSION_V2 if enabled(cfg) else None


def active_daily_pnl_version(cfg) -> str | None:
    return DAILY_PNL_VERSION_V2 if enabled(cfg) else None


# ======================= 启用前自检 =======================

class PreflightFailed(RuntimeError):
    """v2 自检未通过。**这是设计的一部分** —— 把一次静默的口径错位
    变成一次响亮的启动失败。"""


#: 自检项：(代号, 人话, 检查函数)
CHECK_PNL_TABLE = "pnl_snapshot_table"
CHECK_OBS_TABLE = "daily_pnl_observations_table"
CHECK_CMP_TABLE = "daily_pnl_comparisons_table"
CHECK_ASSESS_COLS = "risk_assessments_v2_columns"
CHECK_RUN_KEYS = "snapshot_and_run_id_available"
CHECK_ATOMIC_WRITERS = "atomic_write_functions"

#: risk_assessments 上 v2 需要的列（质量传导的落点）
_REQUIRED_ASSESS_COLS = ("pnl_quality_status", "pnl_reliable",
                         "data_quality_reasons", "pnl_calculation_version")

#: 原子写入与同周期读取所依赖的 Database 方法
_REQUIRED_DB_METHODS = ("insert_pnl_snapshot", "pnl_for_run",
                        "insert_daily_pnl_observation",
                        "insert_daily_pnl_comparison",
                        "latest_daily_pnl_observation",
                        "write_account_snapshot")


def _table_exists(db, name: str) -> bool:
    try:
        with db.connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                (name,)).fetchone()
        return row is not None
    except Exception:  # noqa: BLE001
        return False


def _columns(db, table: str) -> set[str]:
    try:
        with db.connect() as conn:
            return {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
    except Exception:  # noqa: BLE001
        return set()


def preflight(cfg, db) -> dict:
    """v2 启用前自检。返回 {ok, checks, failures}。**只读，不写任何东西。**

    开关关闭时直接返回 ok=True 并标 skipped —— 没启用就没什么要检的。
    """
    if not enabled(cfg):
        return {"ok": True, "skipped": True, "mode": MODE_OLD,
                "checks": {}, "failures": []}

    checks: dict[str, bool] = {}
    failures: list[tuple[str, str]] = []

    def need(code: str, ok: bool, msg: str) -> None:
        checks[code] = bool(ok)
        if not ok:
            failures.append((code, msg))

    need(CHECK_PNL_TABLE, _table_exists(db, "pnl_snapshot"),
         "缺少 pnl_snapshot 表：v2 的分子来源")
    need(CHECK_OBS_TABLE, _table_exists(db, "daily_pnl_observations"),
         "缺少 daily_pnl_observations 表：原子观测无处落库")
    need(CHECK_CMP_TABLE, _table_exists(db, "daily_pnl_metric_comparisons"),
         "缺少 daily_pnl_metric_comparisons 表：old/v2 分歧无处记录")

    cols = _columns(db, "risk_assessments")
    missing = [c for c in _REQUIRED_ASSESS_COLS if c not in cols]
    need(CHECK_ASSESS_COLS, not missing,
         f"risk_assessments 缺列 {missing}：PnL 质量无法向上传导")

    # snapshot_id / sync_run_id 是同周期配对的钥匙，缺了 v2 无从闭合
    bal_cols = _columns(db, "balances_snapshot")
    need(CHECK_RUN_KEYS,
         {"snapshot_id", "sync_run_id"} <= bal_cols,
         "balances_snapshot 缺 snapshot_id/sync_run_id：无法做同周期配对")

    absent = [m for m in _REQUIRED_DB_METHODS if not callable(getattr(db, m, None))]
    need(CHECK_ATOMIC_WRITERS, not absent, f"Database 缺方法 {absent}")

    return {"ok": not failures, "skipped": False, "mode": MODE_V2,
            "checks": checks, "failures": failures}


def require_ready(cfg, db) -> dict:
    """自检不通过就抛。**不静默退回 old** —— 那会让人以为切换成功了。"""
    res = preflight(cfg, db)
    if not res["ok"]:
        detail = "；".join(f"{c}: {m}" for c, m in res["failures"])
        raise PreflightFailed(
            f"拒绝以 v2 模式运行（DAILY_PNL_V2_ENABLED=true）：{detail}。"
            " 请修复后再启用；在此之前请把开关置为 false，旧口径会继续正常工作。")
    return res


# ======================= 启动日志 =======================

def startup_fields(cfg) -> dict:
    """启动时声明当前口径。**不含金额，不含完整账户号。**"""
    return {
        "daily_pnl_mode": mode(cfg),
        "order_mode": order_mode(cfg),
        "active_calculation_version": active_calculation_version(cfg) or "(unchanged)",
        "active_daily_pnl_version": active_daily_pnl_version(cfg) or "(none)",
        "skew_tolerance_seconds": getattr(cfg, "daily_pnl_skew_tolerance_seconds", 90),
        "max_age_seconds": getattr(cfg, "daily_pnl_max_age_seconds", 600),
    }


def log_startup(cfg) -> dict:
    f = startup_fields(cfg)
    log.info("当日盈亏口径 daily_pnl_mode=%s order_mode=%s "
             "active_calculation_version=%s active_daily_pnl_version=%s "
             "skew_tol=%ss max_age=%ss",
             f["daily_pnl_mode"], f["order_mode"],
             f["active_calculation_version"], f["active_daily_pnl_version"],
             f["skew_tolerance_seconds"], f["max_age_seconds"])
    return f
