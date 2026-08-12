"""SQLite 访问层：连接、建表、以及各表的 upsert 辅助。

金额统一以 Decimal 传入/传出，落库时转 str 存 TEXT。
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterable, Iterator
import json as _json      # 简报层直接用；旧方法内的局部 import 保持不动

SCHEMA_PATH = Path(__file__).with_name("schema.sql")


def _s(v) -> str | None:
    """任意值 -> 字符串（质量问题的期望值/实际值可能是 Decimal/数字/None）。"""
    return None if v is None else str(v)


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _dec(v: Any) -> str | None:
    """Decimal / 数值 -> 存储字符串；None 透传。"""
    if v is None:
        return None
    if isinstance(v, Decimal):
        return str(v)
    return str(Decimal(str(v)))


def to_decimal(v: str | None) -> Decimal | None:
    return None if v is None else Decimal(v)


class Database:
    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def init_schema(self) -> None:
        sql = SCHEMA_PATH.read_text(encoding="utf-8")
        with self.connect() as conn:
            conn.executescript(sql)
        self._migrate()

    def _migrate(self) -> None:
        """轻量迁移：老库补新列（CREATE TABLE IF NOT EXISTS 不会加列）。"""
        with self.connect() as conn:
            txn_cols = {r["name"] for r in conn.execute("PRAGMA table_info(transactions)")}
            if "realized" not in txn_cols:
                conn.execute("ALTER TABLE transactions ADD COLUMN realized TEXT")
            pos_cols = {r["name"] for r in conn.execute("PRAGMA table_info(positions_snapshot)")}
            for col in ("price", "price_quality", "quote_as_of"):
                if col not in pos_cols:
                    conn.execute(f"ALTER TABLE positions_snapshot ADD COLUMN {col} TEXT")
            # 第三阶段：行情来源/批次可追溯
            for col, ddl in (("price_source", "TEXT"), ("market_data_type", "INTEGER"),
                             ("source", "TEXT"), ("sync_run_id", "TEXT"),
                             ("conid", "INTEGER"), ("day_pnl", "TEXT"),
                             ("day_pnl_source", "TEXT"),
                             ("realized_pnl", "TEXT"),
                             ("realized_pnl_source", "TEXT"),
                             # 请求 vs 生效的行情类型：不一致即订阅未生效
                             ("requested_market_data_type", "INTEGER"),
                             ("effective_market_data_type", "INTEGER"),
                             ("previous_close", "TEXT"),
                             ("market_value_source", "TEXT"),
                             ("upl_source", "TEXT"),
                             ("futu_market_value", "TEXT"),
                             ("futu_unrealized_estimate", "TEXT"),
                             ("futu_intraday_mtm", "TEXT"),
                             ("futu_quote_age_seconds", "TEXT")):
                if col not in pos_cols:
                    conn.execute(f"ALTER TABLE positions_snapshot ADD COLUMN {col} {ddl}")
            bal_cols = {r["name"] for r in conn.execute("PRAGMA table_info(balances_snapshot)")}
            for col in ("account", "source", "data_quality", "sync_run_id"):
                if col not in bal_cols:
                    conn.execute(f"ALTER TABLE balances_snapshot ADD COLUMN {col} TEXT")
            if "positions_value_source" not in bal_cols:
                conn.execute(
                    "ALTER TABLE balances_snapshot ADD COLUMN positions_value_source TEXT")
            pnl_cols = {r["name"] for r in conn.execute("PRAGMA table_info(pnl_snapshot)")}
            if "daily_basis" not in pnl_cols:
                conn.execute("ALTER TABLE pnl_snapshot ADD COLUMN daily_basis TEXT")
            market_cols = {r["name"] for r in conn.execute(
                "PRAGMA table_info(market_data_snapshots)")}
            if "quote_as_of" not in market_cols:
                conn.execute("ALTER TABLE market_data_snapshots ADD COLUMN quote_as_of TEXT")
            exec_cols = {r["name"] for r in conn.execute("PRAGMA table_info(executions)")}
            if "account" not in exec_cols:
                conn.execute("ALTER TABLE executions ADD COLUMN account TEXT")
            if "decision_id" not in exec_cols:
                conn.execute("ALTER TABLE executions ADD COLUMN decision_id TEXT")
            journal_cols = {r["name"] for r in conn.execute(
                "PRAGMA table_info(journal_entries)")}
            for col, ddl in (
                ("source_type", "TEXT NOT NULL DEFAULT 'MANUAL'"),
                ("position_cycle_id", "TEXT"),
                ("account", "TEXT"),
                ("is_readonly", "INTEGER NOT NULL DEFAULT 0"),
                ("metadata_json", "TEXT"),
                ("archived_at", "TEXT"),
            ):
                if col not in journal_cols:
                    conn.execute(f"ALTER TABLE journal_entries ADD COLUMN {col} {ddl}")
            conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_journal_position_cycle "
                         "ON journal_entries (position_cycle_id) "
                         "WHERE position_cycle_id IS NOT NULL")
            conn.execute(
                "CREATE TRIGGER IF NOT EXISTS trg_journal_archive_immutable_update "
                "BEFORE UPDATE ON journal_entries "
                "WHEN OLD.is_readonly=1 OR OLD.source_type='POSITION_PLAN_ARCHIVE' "
                "BEGIN SELECT RAISE(ABORT, 'archived investment journal entries are immutable'); END"
            )
            conn.execute(
                "CREATE TRIGGER IF NOT EXISTS trg_journal_archive_immutable_delete "
                "BEFORE DELETE ON journal_entries "
                "WHEN OLD.is_readonly=1 OR OLD.source_type='POSITION_PLAN_ARCHIVE' "
                "BEGIN SELECT RAISE(ABORT, 'archived investment journal entries are immutable'); END"
            )
            cycle_cols = {r["name"] for r in conn.execute(
                "PRAGMA table_info(position_cycles)")}
            if cycle_cols:
                for col in ("current_price", "account_weight", "stock_weight"):
                    if col not in cycle_cols:
                        conn.execute(f"ALTER TABLE position_cycles ADD COLUMN {col} TEXT")
                # 旧索引没有 DRAFT 状态；删除并按新生命周期口径幂等重建。
                conn.execute("DROP INDEX IF EXISTS idx_position_cycles_active")
                conn.execute(
                    "CREATE UNIQUE INDEX IF NOT EXISTS idx_position_cycles_active "
                    "ON position_cycles (account, symbol) WHERE status IN "
                    "('PENDING','DRAFT','ACTIVE','REVIEW_DUE','TRIGGERED','INVALIDATED')"
                )
            event_cols = {r["name"] for r in conn.execute(
                "PRAGMA table_info(position_cycle_events)")}
            if event_cols and "order_id" not in event_cols:
                conn.execute("ALTER TABLE position_cycle_events ADD COLUMN order_id TEXT")
            observation_cols = {r["name"] for r in conn.execute(
                "PRAGMA table_info(position_cycle_observations)")}
            if observation_cols:
                for col in ("account_weight", "stock_weight"):
                    if col not in observation_cols:
                        conn.execute(
                            f"ALTER TABLE position_cycle_observations ADD COLUMN {col} TEXT")
            plan_cols = {r["name"] for r in conn.execute(
                "PRAGMA table_info(position_plan_versions)")}
            if plan_cols:
                # 版本表原先没有草稿/发布边界；草稿可更新，正式版本和 legacy 版本不可变。
                for col, ddl in (
                    ("lifecycle_status", "TEXT NOT NULL DEFAULT 'DRAFT'"),
                    ("updated_at", "TEXT"),
                    ("draft_of_version_id", "TEXT"),
                    ("hold_only_review", "INTEGER NOT NULL DEFAULT 0"),
                    ("limit_confirmed", "INTEGER NOT NULL DEFAULT 0"),
                    ("max_weight", "TEXT"),
                    ("target_weight", "TEXT"),
                    ("published_at", "TEXT"),
                    ("published_by", "TEXT"),
                ):
                    if col not in plan_cols:
                        conn.execute(f"ALTER TABLE position_plan_versions ADD COLUMN {col} {ddl}")
                # 先移除旧实现的无条件 immutable trigger，随后才能回填新列和迁移生命周期。
                conn.execute("DROP TRIGGER IF EXISTS trg_position_plan_versions_immutable_update")
                conn.execute("DROP TRIGGER IF EXISTS trg_position_plan_versions_immutable_delete")
                if "account" not in plan_cols:
                    conn.execute("ALTER TABLE position_plan_versions ADD COLUMN account TEXT")
                    conn.execute(
                        "UPDATE position_plan_versions SET account=("
                        "SELECT account FROM position_cycles pc "
                        "WHERE pc.id=position_plan_versions.position_cycle_id) "
                        "WHERE account IS NULL")
                conn.execute("UPDATE position_plan_versions SET updated_at=COALESCE(updated_at, created_at)")

                # 旧实现没有发布动作：保留每个周期最新内容为 DRAFT，早期记录标为
                # LEGACY，只读但不参与正式触发。若已完成本迁移或已有正式版本，幂等跳过。
                cycle_rows = conn.execute(
                    "SELECT id, status FROM position_cycles WHERE status NOT IN ('CLOSED','ARCHIVED')"
                ).fetchall()
                for cycle in cycle_rows:
                    has_zero_draft = conn.execute(
                        "SELECT 1 FROM position_plan_versions "
                        "WHERE position_cycle_id=? AND lifecycle_status='DRAFT' AND version=0 LIMIT 1",
                        (cycle["id"],),
                    ).fetchone()
                    has_published = conn.execute(
                        "SELECT 1 FROM position_plan_versions "
                        "WHERE position_cycle_id=? AND lifecycle_status='PUBLISHED' LIMIT 1",
                        (cycle["id"],),
                    ).fetchone()
                    if has_zero_draft or has_published:
                        continue
                    versions = conn.execute(
                        "SELECT * FROM position_plan_versions WHERE position_cycle_id=? "
                        "ORDER BY version DESC", (cycle["id"],)).fetchall()
                    if not versions:
                        continue
                    latest = versions[0]
                    for old in versions[1:]:
                        conn.execute(
                            "UPDATE position_plan_versions SET lifecycle_status='LEGACY', "
                            "updated_at=COALESCE(updated_at, created_at) WHERE id=?",
                            (old["id"],))
                    conn.execute(
                        "UPDATE position_plan_versions SET lifecycle_status='DRAFT', version=0, "
                        "updated_at=COALESCE(updated_at, created_at) WHERE id=?",
                        (latest["id"],))
                    has_content = any(
                        latest[k] not in (None, "") for k in (
                            "strategy_type", "current_conclusion", "thesis", "why_now",
                            "confirmed_facts", "subjective_assumptions", "catalysts",
                            "invalidation_conditions", "notes", "review_at"))
                    new_status = "DRAFT" if has_content else "PENDING"
                    conn.execute(
                        "UPDATE position_cycles SET status=? WHERE id=? "
                        "AND status NOT IN ('CLOSED','ARCHIVED')",
                        (new_status, cycle["id"]))
                conn.execute(
                    "CREATE TRIGGER trg_position_plan_versions_immutable_update "
                    "BEFORE UPDATE ON position_plan_versions "
                    "WHEN OLD.lifecycle_status IN ('PUBLISHED','LEGACY') "
                    "BEGIN SELECT RAISE(ABORT, 'published position plan versions are immutable'); END"
                )
                conn.execute(
                    "CREATE TRIGGER trg_position_plan_versions_immutable_delete "
                    "BEFORE DELETE ON position_plan_versions "
                    "WHEN OLD.lifecycle_status IN ('PUBLISHED','LEGACY') "
                    "BEGIN SELECT RAISE(ABORT, 'published position plan versions are immutable'); END"
                )
            action_cols = {r["name"] for r in conn.execute(
                "PRAGMA table_info(position_plan_actions)")}
            if action_cols and "account" not in action_cols:
                conn.execute("ALTER TABLE position_plan_actions ADD COLUMN account TEXT")
                conn.execute(
                    "UPDATE position_plan_actions SET account=("
                    "SELECT account FROM position_cycles pc "
                    "WHERE pc.id=position_plan_actions.position_cycle_id) "
                    "WHERE account IS NULL")
            if action_cols:
                for col in (
                    "trigger_evaluation_status", "trigger_evaluation_note",
                    "trigger_evaluated_at", "trigger_quote_json",
                    "confirmed_at", "confirmed_by", "confirmation_intent",
                    "confirmation_note", "recommendation_id",
                ):
                    if col not in action_cols:
                        conn.execute(
                            f"ALTER TABLE position_plan_actions ADD COLUMN {col} TEXT")
            link_cols = {r["name"] for r in conn.execute(
                "PRAGMA table_info(position_plan_execution_links)")}
            if link_cols and "account" not in link_cols:
                conn.execute(
                    "ALTER TABLE position_plan_execution_links ADD COLUMN account TEXT")
                conn.execute(
                    "UPDATE position_plan_execution_links SET account=("
                    "SELECT account FROM position_cycles pc "
                    "WHERE pc.id=position_plan_execution_links.position_cycle_id) "
                        "WHERE account IS NULL")
            if action_cols:
                # 旧库里 ACTIVE action 可能挂在未发布版本上；统一降为草稿，
                # 正式发布时由服务复制为 ACTIVE action。
                conn.execute(
                    "UPDATE position_plan_actions SET status='DRAFT' "
                    "WHERE status IN ('ACTIVE','TRIGGERED') AND plan_version_id IN ("
                    "SELECT id FROM position_plan_versions WHERE lifecycle_status<>'PUBLISHED')"
                )
            run_cols = {r["name"] for r in conn.execute("PRAGMA table_info(rule_runs)")}
            for col in ("sync_run_id", "positions_snapshot_id", "balances_snapshot_id",
                        "quotes_as_of", "quote_quality_summary", "rule_summary",
                        "per_symbol"):
                if col not in run_cols:
                    conn.execute(f"ALTER TABLE rule_runs ADD COLUMN {col} TEXT")
            # 依赖上面新增列的索引：必须补完列才能建（老库执行 schema.sql 时列还不存在）
            conn.execute("CREATE INDEX IF NOT EXISTS idx_positions_run "
                         "ON positions_snapshot (sync_run_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_balances_run "
                         "ON balances_snapshot (sync_run_id)")
            stop_cols = {r["name"] for r in conn.execute("PRAGMA table_info(stop_levels)")}
            for col, ddl in (
                ("atr_period", "INTEGER"),
                ("bars_used", "INTEGER"),
                ("data_sufficient", "INTEGER NOT NULL DEFAULT 1"),
                ("data_note", "TEXT"),
            ):
                if col not in stop_cols:
                    conn.execute(f"ALTER TABLE stop_levels ADD COLUMN {col} {ddl}")

            # ---- 账户快照层：显式 snapshot_id + 账户口径补全 + 原始字段留存 ----
            # 原表只存了标准化后的净资产/现金/融资/购买力，不足以支撑保证金与
            # 币种维度的分析；raw_json 留存 IBKR 原始字段，保证「标准字段算错了
            # 还能回溯原值」。全部为可空新列，老数据保持原样，迁移幂等。
            bal_cols2 = {r["name"] for r in conn.execute("PRAGMA table_info(balances_snapshot)")}
            for col, ddl in (
                ("snapshot_id", "TEXT"),           # 快照唯一标识（账户汇总↔持仓明细的关联键）
                ("total_cash", "TEXT"),            # 总现金
                ("available_funds", "TEXT"),       # 可用资金 / 可用保证金
                ("excess_liquidity", "TEXT"),      # 剩余流动性
                ("init_margin", "TEXT"),           # 初始保证金占用
                ("maint_margin", "TEXT"),          # 维持保证金占用
                ("positions_value", "TEXT"),       # 持仓总市值（账户口径）
                ("currency", "TEXT"),              # 基准币种
                ("quality_status", "TEXT"),        # fresh / stale / error
                ("ai_analysis_allowed", "INTEGER"),
                ("raw_json", "TEXT"),              # IBKR 原始字段（标准字段的溯源依据）
            ):
                if col not in bal_cols2:
                    conn.execute(f"ALTER TABLE balances_snapshot ADD COLUMN {col} {ddl}")
            bal_cols3 = {r["name"] for r in conn.execute("PRAGMA table_info(balances_snapshot)")}
            for col in ("positions_as_of", "account_as_of", "pnl_as_of",
                        "orders_as_of", "executions_as_of", "reconciled_at"):
                if col not in bal_cols3:
                    conn.execute(f"ALTER TABLE balances_snapshot ADD COLUMN {col} TEXT")
            conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_bal_snapshot_id "
                         "ON balances_snapshot (snapshot_id) WHERE snapshot_id IS NOT NULL")

            pos_cols2 = {r["name"] for r in conn.execute("PRAGMA table_info(positions_snapshot)")}
            for col, ddl in (("snapshot_id", "TEXT"), ("currency", "TEXT"), ("raw_json", "TEXT")):
                if col not in pos_cols2:
                    conn.execute(f"ALTER TABLE positions_snapshot ADD COLUMN {col} {ddl}")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_pos_snapshot_id "
                         "ON positions_snapshot (snapshot_id)")

            # ---- 同步任务：耗时/错误/重试/条数，供可观测性与排障 ----
            # run_state 是新的归一状态（running/success/partial/failed）；旧的 status
            # 列（READY/WARMING_UP/…）保留不动，既有读取方不受影响。
            run_cols2 = {r["name"] for r in conn.execute("PRAGMA table_info(sync_runs)")}
            for col, ddl in (
                ("run_state", "TEXT"),
                ("snapshot_id", "TEXT"),
                ("trigger", "TEXT"),               # auto / manual
                ("request_id", "TEXT"),            # 与 API/日志串联的追踪 ID
                ("fetch_ms", "INTEGER"),
                ("write_ms", "INTEGER"),
                ("error_code", "TEXT"),
                ("error_message", "TEXT"),
                ("retry_count", "INTEGER"),
                ("positions_count", "INTEGER"),
                ("orders_count", "INTEGER"),
                ("executions_count", "INTEGER"),
            ):
                if col not in run_cols2:
                    conn.execute(f"ALTER TABLE sync_runs ADD COLUMN {col} {ddl}")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_sync_runs_started "
                         "ON sync_runs (started_at DESC)")

            # ---- 风险规则版本审计 ----
            rr = {r["name"] for r in conn.execute("PRAGMA table_info(risk_rules)")}
            if rr:
                for col, ddl in (("threshold_unit", "TEXT"), ("config_source", "TEXT"),
                                 ("version_hash", "TEXT"), ("effective_from", "TEXT"),
                                 ("effective_to", "TEXT")):
                    if col not in rr:
                        conn.execute(f"ALTER TABLE risk_rules ADD COLUMN {col} {ddl}")
            reg = {r["name"] for r in conn.execute(
                "PRAGMA table_info(risk_event_registry)")}
            if reg:
                # 联合事件同时依赖账户与市场两次评估，两个 id 都要留；
                # rule_set_hash 让「阈值当时是什么」可验证。
                for col in ("rule_set_hash", "market_assessment_id",
                            "calculation_version"):
                    if col not in reg:
                        conn.execute(
                            f"ALTER TABLE risk_event_registry ADD COLUMN {col} TEXT")

            ra = {r["name"] for r in conn.execute("PRAGMA table_info(risk_assessments)")}
            if ra:
                for col in ("rule_set_version", "rule_set_hash",
                            # 规则适用性必须落库 —— 否则命中缓存读回来时
                            # 「哪些指标不适用」就丢了，页面会显示成一片正常
                            "concentration_applicability", "rule_outcomes",
                            "score_normalization_method", "normalization_detail",
                            # PnL 子系统的真实质量。历史上这里对 DEGRADED 样本
                            # 一律记 fresh/reliable，规则不知道自己在用降级数据。
                            "pnl_quality_status", "pnl_reliable",
                            "data_quality_reasons", "pnl_calculation_version"):
                    if col not in ra:
                        conn.execute(f"ALTER TABLE risk_assessments ADD COLUMN {col} TEXT")
            rev = {r["name"] for r in conn.execute("PRAGMA table_info(risk_events)")}
            if rev:
                for col in ("rule_version", "rule_set_hash"):
                    if col not in rev:
                        conn.execute(f"ALTER TABLE risk_events ADD COLUMN {col} TEXT")

            # cash_flows 扩容：原表只存外部出入金，现在要容纳全部流水类型。
            # **既有行一律回填 category='external_cash_flow'** —— 它们本来就是
            # 出入金；不回填会让净值曲线的资金流调整突然把它们漏掉。
            cf = {r["name"] for r in conn.execute("PRAGMA table_info(cash_flows)")}
            if cf:
                fr = {r["name"] for r in conn.execute(
                    "PRAGMA table_info(flex_sync_runs)")}
                if fr and "trigger_source" not in fr:
                    conn.execute("ALTER TABLE flex_sync_runs "
                                 "ADD COLUMN trigger_source TEXT")

                for col, ddl in (("cashflow_id", "TEXT"),
                                 ("broker_transaction_id", "TEXT"),
                                 ("account_id", "TEXT"),
                                 ("occurred_at", "TEXT"), ("settled_at", "TEXT"),
                                 ("category", "TEXT"), ("subtype", "TEXT"),
                                 ("direction", "TEXT"), ("amount_base", "TEXT"),
                                 ("fx_rate", "TEXT"), ("symbol", "TEXT"),
                                 ("conid", "TEXT"), ("description", "TEXT"),
                                 ("raw_json", "TEXT"), ("quality_status", "TEXT"),
                                 ("content_hash", "TEXT"), ("flex_run_id", "TEXT"),
                                 ("updated_at", "TEXT")):
                    if col not in cf:
                        conn.execute(f"ALTER TABLE cash_flows ADD COLUMN {col} {ddl}")
                conn.execute(
                    "UPDATE cash_flows SET category='external_cash_flow' "
                    "WHERE category IS NULL OR category=''")
                conn.execute(
                    "UPDATE cash_flows SET account_id=account WHERE account_id IS NULL")
                conn.execute(
                    "UPDATE cash_flows SET occurred_at=effective_at "
                    "WHERE occurred_at IS NULL")
                conn.execute(
                    "UPDATE cash_flows SET direction=CASE WHEN CAST(amount AS REAL)>=0 "
                    "THEN 'inflow' ELSE 'outflow' END WHERE direction IS NULL")
                conn.execute(
                    "UPDATE cash_flows SET quality_status='ok' WHERE quality_status IS NULL")
                # 幂等写入的去重键
                conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_cf_cashflow_id "
                             "ON cash_flows (cashflow_id)")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_cf_category "
                             "ON cash_flows (category, et_date)")

            # 机会发现：daily_bars 必须保存数据来源与复权口径。
            bar_cols = {r["name"] for r in conn.execute("PRAGMA table_info(daily_bars)")}
            for col, ddl in (("source", "TEXT"), ("adjustment_mode", "TEXT"),
                             ("bar_as_of", "TEXT"), ("fetched_at", "TEXT"),
                             ("corporate_action_provenance", "TEXT")):
                if col not in bar_cols:
                    conn.execute(f"ALTER TABLE daily_bars ADD COLUMN {col} {ddl}")
            conn.execute(
                "UPDATE daily_bars SET source=COALESCE(source,'legacy'), "
                "adjustment_mode=COALESCE(adjustment_mode,'unknown'), "
                "bar_as_of=COALESCE(bar_as_of,date)"
            )
            watch_cols = {r["name"] for r in conn.execute(
                "PRAGMA table_info(opportunity_watchlist)")}
            for col, ddl in (("sector", "TEXT"), ("asset_type", "TEXT NOT NULL DEFAULT 'STOCK'"),
                             ("exchange", "TEXT"), ("last_sync_status", "TEXT"),
                             ("last_sync_at", "TEXT"), ("last_sync_error", "TEXT")):
                if watch_cols and col not in watch_cols:
                    conn.execute(f"ALTER TABLE opportunity_watchlist ADD COLUMN {col} {ddl}")

            # Hyperliquid 阶段 5 的有效评分与基线分离；老库必须显式补列，
            # 这样影子期和历史机会回测不会被链上组件改写。
            opportunity_cols = {r["name"] for r in conn.execute(
                "PRAGMA table_info(opportunity_candidates)")}
            if opportunity_cols:
                for col, ddl in (
                    ("effective_score", "INTEGER"),
                    ("effective_status", "TEXT"),
                    ("chain_signal_id", "TEXT"),
                    ("chain_score_adjustment", "INTEGER NOT NULL DEFAULT 0"),
                    ("chain_rules_version", "TEXT"),
                    ("chain_rules_hash", "TEXT"),
                ):
                    if col not in opportunity_cols:
                        conn.execute(f"ALTER TABLE opportunity_candidates ADD COLUMN {col} {ddl}")
                conn.execute("UPDATE opportunity_candidates SET effective_score=COALESCE(effective_score,score), "
                             "effective_status=COALESCE(effective_status,status), "
                             "chain_score_adjustment=COALESCE(chain_score_adjustment,0)")

            # Hyperliquid validation-policy-v2：历史回补、锁定测试和实时前瞻
            # 必须是不可混算的来源标签。CREATE TABLE IF NOT EXISTS 不会为老库补列。
            for table, columns in {
                "equity_bars_5m": (
                    ("data_origin", "TEXT NOT NULL DEFAULT 'LIVE'"),
                ),
                "hl_signal_outcomes": (
                    ("data_origin", "TEXT NOT NULL DEFAULT 'LIVE'"),
                    ("validation_commitment_id", "TEXT"),
                ),
                "hl_validation_runs": (
                    ("validation_policy_version", "TEXT NOT NULL DEFAULT 'validation-policy-v1'"),
                    ("sample_origin", "TEXT NOT NULL DEFAULT 'PROSPECTIVE_CONFIRMATION'"),
                    ("commitment_id", "TEXT"),
                ),
            }.items():
                existing = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
                for name, ddl in columns:
                    if existing and name not in existing:
                        conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")

    # ---------- 快照写入 ----------

    def start_sync_run(self, sync_run_id: str, started_at: str, *, account: str | None,
                       source: str, status: str = "RUNNING") -> None:
        with self.connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO sync_runs "
                "(sync_run_id, started_at, account, status, source) VALUES (?,?,?,?,?)",
                (sync_run_id, started_at, account, status, source),
            )

    def finish_sync_run(self, sync_run_id: str, *, status: str, note: str | None = None,
                        run_state: str | None = None, snapshot_id: str | None = None,
                        fetch_ms: int | None = None, write_ms: int | None = None,
                        error_code: str | None = None, error_message: str | None = None,
                        retry_count: int | None = None, positions_count: int | None = None,
                        orders_count: int | None = None,
                        executions_count: int | None = None) -> None:
        """收尾同步任务。除既有 status 外，记录归一状态与耗时/错误/条数。

        error_message 由调用方保证不含凭证（异常文本可能带连接串，故统一截断）。
        """
        with self.connect() as conn:
            conn.execute(
                "UPDATE sync_runs SET finished_at=?, status=?, note=?, run_state=?, "
                " snapshot_id=?, fetch_ms=?, write_ms=?, error_code=?, error_message=?, "
                " retry_count=?, positions_count=?, orders_count=?, executions_count=? "
                "WHERE sync_run_id=?",
                (_utcnow_iso(), status, note, run_state, snapshot_id, fetch_ms, write_ms,
                 error_code, (error_message or None) and error_message[:500],
                 retry_count, positions_count, orders_count, executions_count, sync_run_id),
            )

    def mark_sync_run_running(self, sync_run_id: str, *, trigger: str = "auto",
                              request_id: str | None = None) -> None:
        with self.connect() as conn:
            conn.execute(
                "UPDATE sync_runs SET run_state='running', trigger=?, request_id=? "
                "WHERE sync_run_id=?", (trigger, request_id, sync_run_id))

    def record_quality_issues(self, issues: Iterable[dict], *, snapshot_id: str | None,
                              sync_run_id: str | None, account: str | None) -> int:
        """独立落库质量问题（用于同步失败等没有快照的场景）。"""
        rows = [
            (snapshot_id, sync_run_id, account, i["issue_code"], i["severity"],
             i["category"], i["message"], i.get("affected_field"),
             _s(i.get("expected_value")), _s(i.get("actual_value")),
             1 if i.get("blocks_ai") else 0, i.get("detected_at") or _utcnow_iso())
            for i in issues
        ]
        if not rows:
            return 0
        with self.connect() as conn:
            conn.executemany(
                "INSERT INTO data_quality_issues "
                "(snapshot_id, sync_run_id, account, issue_code, severity, category, "
                " message, affected_field, expected_value, actual_value, blocks_ai, "
                " detected_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", rows)
        return len(rows)

    # ---------- 账户快照：单事务原子写入 ----------

    def write_account_snapshot(self, *, snapshot_id: str, ts: str, balances: dict,
                               positions: Iterable[dict], sync_run_id: str | None = None,
                               quality_status: str | None = None,
                               ai_analysis_allowed: bool | None = None,
                               issues: Iterable[dict] = ()) -> int:
        """账户汇总 + 持仓明细 + 质量问题，**同一个事务**写入。

        为什么必须原子：分两次写时，进程在中间崩溃会留下「有账户汇总、没有持仓明细」
        的半成品快照 —— 而看板与规则引擎会把它当成一份完整数据来读，得出「持仓为空」
        的错误结论。要么整份可见，要么整份不可见，不存在中间态。

        返回写入的持仓行数。
        """
        pos_rows = list(positions)
        pos_payload = [
            (ts, r["symbol"], _dec(r["qty"]), _dec(r["avg_cost"]),
             _dec(r["market_value"]), _dec(r["upl"]),
             _dec(r.get("price")), r.get("price_source"), r.get("price_quality"),
             r.get("market_data_type"), r.get("quote_as_of"), r.get("source"),
             sync_run_id, r.get("conid"), _dec(r.get("day_pnl")), r.get("day_pnl_source"),
             _dec(r.get("realized_pnl")), r.get("realized_pnl_source"),
             snapshot_id, r.get("currency"), r.get("raw_json"),
             r.get("requested_market_data_type"),
             r.get("effective_market_data_type"), _dec(r.get("previous_close")),
             r.get("market_value_source"), r.get("upl_source"),
             _dec(r.get("futu_market_value")),
             _dec(r.get("futu_unrealized_estimate")),
             _dec(r.get("futu_intraday_mtm")),
             _dec(r.get("futu_quote_age_seconds")))
            for r in pos_rows
        ]
        now_iso = _utcnow_iso()
        issue_payload = [
            (snapshot_id, sync_run_id, balances.get("account"), i["issue_code"],
             i["severity"], i["category"], i["message"], i.get("affected_field"),
             _s(i.get("expected_value")), _s(i.get("actual_value")),
             1 if i.get("blocks_ai") else 0, i.get("detected_at") or now_iso)
            for i in issues
        ]
        with self.connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO balances_snapshot "
                "(ts, equity, cash, margin_balance, buying_power, account, source, "
                " data_quality, sync_run_id, snapshot_id, total_cash, available_funds, "
                " excess_liquidity, init_margin, maint_margin, positions_value, currency, "
                " quality_status, ai_analysis_allowed, raw_json, positions_as_of, "
                " account_as_of, pnl_as_of, orders_as_of, executions_as_of, reconciled_at,"
                " positions_value_source) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (ts, _dec(balances["equity"]), _dec(balances["cash"]),
                 _dec(balances["margin_balance"]), _dec(balances["buying_power"]),
                 balances.get("account"), balances.get("source"),
                 balances.get("data_quality"), sync_run_id, snapshot_id,
                 _dec(balances.get("total_cash")), _dec(balances.get("available_funds")),
                 _dec(balances.get("excess_liquidity")), _dec(balances.get("init_margin")),
                 _dec(balances.get("maint_margin")), _dec(balances.get("positions_value")),
                 balances.get("currency"), quality_status,
                 None if ai_analysis_allowed is None else int(bool(ai_analysis_allowed)),
                 balances.get("raw_json"), balances.get("positions_as_of"),
                 balances.get("account_as_of"), balances.get("pnl_as_of"),
                 balances.get("orders_as_of"), balances.get("executions_as_of"),
                 balances.get("reconciled_at"), balances.get("positions_value_source")),
            )
            if pos_payload:
                conn.executemany(
                    "INSERT OR REPLACE INTO positions_snapshot "
                    "(ts, symbol, qty, avg_cost, market_value, upl, price, price_source, "
                    " price_quality, market_data_type, quote_as_of, source, sync_run_id, "
                    " conid, day_pnl, day_pnl_source, realized_pnl, realized_pnl_source,"
                    " snapshot_id, currency, raw_json,"
                    " requested_market_data_type, effective_market_data_type,"
                    " previous_close, market_value_source, upl_source,"
                    " futu_market_value, futu_unrealized_estimate, futu_intraday_mtm,"
                    " futu_quote_age_seconds) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    pos_payload)
            if issue_payload:
                conn.executemany(
                    "INSERT INTO data_quality_issues "
                    "(snapshot_id, sync_run_id, account, issue_code, severity, category, "
                    " message, affected_field, expected_value, actual_value, blocks_ai, "
                    " detected_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", issue_payload)
        return len(pos_payload)

    def open_orders(self, limit: int = 100) -> list[dict]:
        """当前挂单（非终态）。终态订单属于历史，不进「当前挂单」。"""
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT order_id, ts, symbol, side, order_type, qty, price, status "
                "FROM orders WHERE UPPER(status) NOT IN "
                "('FILLED','CANCELLED','CANCELED','INACTIVE','APICANCELLED','EXPIRED') "
                "ORDER BY ts DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

    # ---------- 证券元数据 ----------

    def upsert_instrument_metadata(self, rows: list[dict]) -> int:
        now = _utcnow_iso()
        with self.connect() as conn:
            for r in rows:
                conn.execute(
                    "INSERT INTO instrument_metadata (symbol, conid, sec_type,"
                    " company_name, sector, industry, asset_class, source, confidence,"
                    " unknown_reason, raw_json, updated_at)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?) "
                    "ON CONFLICT(symbol) DO UPDATE SET conid=excluded.conid,"
                    " sec_type=excluded.sec_type, company_name=excluded.company_name,"
                    " sector=excluded.sector, industry=excluded.industry,"
                    " asset_class=excluded.asset_class, source=excluded.source,"
                    " confidence=excluded.confidence,"
                    " unknown_reason=excluded.unknown_reason,"
                    " raw_json=excluded.raw_json, updated_at=excluded.updated_at",
                    (r["symbol"], r.get("conid"), r.get("sec_type"), r.get("company_name"),
                     r.get("sector"), r.get("industry"), r.get("asset_class"),
                     r.get("source"), r.get("confidence"), r.get("unknown_reason"),
                     r.get("raw_json"), now))
        return len(rows)

    def instrument_metadata(self, symbols: list[str] | None = None) -> dict[str, dict]:
        with self.connect() as conn:
            if symbols:
                q = ",".join("?" * len(symbols))
                rows = conn.execute(
                    f"SELECT * FROM instrument_metadata WHERE symbol IN ({q})",
                    symbols).fetchall()
            else:
                rows = conn.execute("SELECT * FROM instrument_metadata").fetchall()
        return {r["symbol"]: dict(r) for r in rows}

    def classification_overrides(self) -> dict[str, dict]:
        with self.connect() as conn:
            rows = conn.execute("SELECT * FROM instrument_classification_overrides").fetchall()
        return {r["symbol"]: dict(r) for r in rows}

    def set_classification_override(self, symbol: str, *, sector: str | None,
                                    industry: str | None = None,
                                    asset_class: str | None = None,
                                    company_name: str | None = None,
                                    note: str | None = None,
                                    created_by: str = "manual") -> None:
        now = _utcnow_iso()
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO instrument_classification_overrides (symbol, sector, industry,"
                " asset_class, company_name, note, created_by, created_at, updated_at)"
                " VALUES (?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(symbol) DO UPDATE SET sector=excluded.sector,"
                " industry=excluded.industry, asset_class=excluded.asset_class,"
                " company_name=excluded.company_name, note=excluded.note,"
                " updated_at=excluded.updated_at",
                (symbol, sector, industry, asset_class, company_name, note,
                 created_by, now, now))

    # ---------- 风险评估层 ----------

    def snapshot_by_id(self, snapshot_id: str):
        with self.connect() as conn:
            return conn.execute("SELECT * FROM balances_snapshot WHERE snapshot_id=?",
                                (snapshot_id,)).fetchone()

    def previous_snapshot_positions(self, snapshot_id: str, account: str | None) -> list[dict]:
        """上一份快照的持仓（用于「突变无成交」检查）。"""
        with self.connect() as conn:
            cur = conn.execute("SELECT ts FROM balances_snapshot WHERE snapshot_id=?",
                               (snapshot_id,)).fetchone()
            if not cur:
                return []
            prev = conn.execute(
                "SELECT snapshot_id FROM balances_snapshot "
                "WHERE snapshot_id IS NOT NULL AND ts < ? "
                + ("AND account=? " if account else "")
                + "ORDER BY ts DESC LIMIT 1",
                (cur["ts"], account) if account else (cur["ts"],)).fetchone()
            if not prev:
                return []
            rows = conn.execute(
                "SELECT symbol, qty, market_value FROM positions_snapshot WHERE snapshot_id=?",
                (prev["snapshot_id"],)).fetchall()
        return [dict(r) for r in rows]

    def assessment_for(self, snapshot_id: str, version: str):
        with self.connect() as conn:
            return conn.execute(
                "SELECT * FROM risk_assessments WHERE snapshot_id=? AND calculation_version=?",
                (snapshot_id, version)).fetchone()

    def assessment_for_snapshot(self, snapshot_id: str):
        """该快照上最新的评估（不限版本）。影子比较用 —— 取已落库的结论，
        不重算，重算会引入新时点让比较失真。"""
        with self.connect() as conn:
            return conn.execute(
                "SELECT * FROM risk_assessments WHERE snapshot_id=? "
                "ORDER BY created_at DESC LIMIT 1", (snapshot_id,)).fetchone()

    def save_risk_assessment(self, p: dict) -> None:
        """评估与事件同事务写入 —— 有结论没明细同样是半成品。"""
        import json as _json
        with self.connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO risk_assessments "
                "(assessment_id, snapshot_id, account_id, data_as_of, risk_level, risk_score,"
                " reliable, blocked_reason, component_scores, calculated_metrics,"
                " triggered_rules, top_risk_drivers, data_quality_status,"
                " ai_analysis_allowed, calculation_version, created_at,"
                " rule_set_version, rule_set_hash, concentration_applicability,"
                " rule_outcomes, score_normalization_method, normalization_detail,"
                " pnl_quality_status, pnl_reliable, data_quality_reasons,"
                " pnl_calculation_version) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (p["assessment_id"], p["snapshot_id"], p.get("account_id"), p["data_as_of"],
                 p["risk_level"], p.get("risk_score"), int(bool(p.get("reliable"))),
                 p.get("blocked_reason"),
                 _json.dumps(p.get("component_scores", {}), ensure_ascii=False, default=str),
                 _json.dumps(p.get("calculated_metrics", {}), ensure_ascii=False, default=str),
                 _json.dumps(p.get("triggered_rules", []), ensure_ascii=False),
                 _json.dumps(p.get("top_risk_drivers", []), ensure_ascii=False, default=str),
                 p.get("data_quality_status"), int(bool(p.get("ai_analysis_allowed"))),
                 p["calculation_version"], p["created_at"],
                 p.get("rule_set_version"), p.get("rule_set_hash"),
                 _json.dumps(p.get("concentration_applicability", {}),
                             ensure_ascii=False, default=str),
                 _json.dumps(p.get("rule_outcomes", []),
                             ensure_ascii=False, default=str),
                 p.get("score_normalization_method"),
                 _json.dumps(p.get("normalization_detail", {}),
                             ensure_ascii=False, default=str),
                 p.get("pnl_quality_status"),
                 None if p.get("pnl_reliable") is None
                 else int(bool(p.get("pnl_reliable"))),
                 _json.dumps(p.get("data_quality_reasons", []), ensure_ascii=False),
                 p.get("pnl_calculation_version")))
            conn.execute("DELETE FROM risk_events WHERE assessment_id=?", (p["assessment_id"],))
            rows = [
                (p["assessment_id"], p["snapshot_id"], e["event_code"], e["rule_code"],
                 e["category"], e["severity"], e["title"], e["explanation"],
                 e.get("actual_value"), e.get("threshold_value"), e.get("unit"),
                 e.get("affected_symbol"), 1 if e.get("blocks_ai") else 0,
                 e.get("recommended_check"), e.get("detected_at") or p["created_at"],
                 e.get("rule_version"), e.get("rule_set_hash"))
                for e in p.get("events", [])
            ]
            if rows:
                conn.executemany(
                    "INSERT INTO risk_events (assessment_id, snapshot_id, event_code,"
                    " rule_code, category, severity, title, explanation, actual_value,"
                    " threshold_value, unit, affected_symbol, blocks_ai,"
                    " recommended_check, detected_at, rule_version, rule_set_hash)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    rows)

    def risk_events_for(self, assessment_id: str) -> list[dict]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT event_code, rule_code, category, severity, title, explanation,"
                "       actual_value, threshold_value, unit, affected_symbol, blocks_ai,"
                "       recommended_check, detected_at, rule_version, rule_set_hash "
                "FROM risk_events WHERE assessment_id=? "
                "ORDER BY CASE severity WHEN 'critical' THEN 0 WHEN 'high' THEN 1 "
                "         ELSE 2 END, id", (assessment_id,)).fetchall()
        return [dict(r) for r in rows]

    def latest_assessment(self, account: str | None = None):
        sql = ("SELECT * FROM risk_assessments "
               + ("WHERE account_id=? " if account else "")
               + "ORDER BY data_as_of DESC LIMIT 1")
        with self.connect() as conn:
            return conn.execute(sql, (account,) if account else ()).fetchone()

    def assessments_between(self, start: str | None, end: str | None, *,
                            account: str | None = None, risk_level: str | None = None,
                            limit: int = 50, offset: int = 0) -> list[dict]:
        clauses, params = ["1=1"], []
        if start:
            clauses.append("data_as_of >= ?"); params.append(start)
        if end:
            clauses.append("data_as_of <= ?"); params.append(end)
        if account:
            clauses.append("account_id = ?"); params.append(account)
        if risk_level:
            clauses.append("risk_level = ?"); params.append(risk_level)
        params.extend([limit, offset])
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM risk_assessments WHERE " + " AND ".join(clauses)
                + " ORDER BY data_as_of DESC LIMIT ? OFFSET ?", params).fetchall()
        return [dict(r) for r in rows]

    def count_assessments(self, start: str | None, end: str | None,
                          account: str | None = None, risk_level: str | None = None) -> int:
        clauses, params = ["1=1"], []
        if start:
            clauses.append("data_as_of >= ?"); params.append(start)
        if end:
            clauses.append("data_as_of <= ?"); params.append(end)
        if account:
            clauses.append("account_id = ?"); params.append(account)
        if risk_level:
            clauses.append("risk_level = ?"); params.append(risk_level)
        with self.connect() as conn:
            return conn.execute("SELECT COUNT(*) FROM risk_assessments WHERE "
                                + " AND ".join(clauses), params).fetchone()[0]

    def sync_risk_rules(self, rules: list[dict], *, set_version: str,
                        set_hash: str) -> int:
        """规则定义按版本落库。

        同一 (rule_code, version) 只写一次 —— 相同版本重复调用是幂等的。
        阈值变化会产生新 version（含内容哈希），旧版本**保留不覆盖**，
        历史评估因此永远能查到「当时用的是什么口径」。
        """
        now = _utcnow_iso()
        with self.connect() as conn:
            # 旧版本收口：为同 rule_code 的其它版本补 effective_to
            conn.execute(
                "UPDATE risk_rules SET effective_to=? "
                "WHERE version<>? AND effective_to IS NULL", (now, set_version))
            for r in rules:
                conn.execute(
                    "INSERT INTO risk_rules (rule_code, version, name, category, description,"
                    " enabled, severity, threshold, comparator, calculation_basis, unit,"
                    " scope, threshold_unit, config_source, version_hash, effective_from,"
                    " created_at, updated_at)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
                    "ON CONFLICT(rule_code, version) DO NOTHING",
                    (r["rule_code"], set_version, r["name"], r["category"],
                     r.get("description"), int(bool(r.get("enabled", True))), r["severity"],
                     r.get("threshold"), r.get("comparator"), r.get("calculation_basis"),
                     r.get("unit"), r.get("scope"), r.get("unit"),
                     r.get("config_source", "env"), set_hash, now, now, now))
        return len(rules)

    def list_risk_rules(self, version: str | None = None) -> list[dict]:
        with self.connect() as conn:
            if version:
                rows = conn.execute("SELECT * FROM risk_rules WHERE version=? ORDER BY rule_code",
                                    (version,)).fetchall()
            else:
                rows = conn.execute("SELECT * FROM risk_rules ORDER BY version DESC, rule_code"
                                    ).fetchall()
        return [dict(r) for r in rows]

    def risk_rule_versions(self) -> list[dict]:
        """所有规则版本及其生效区间（供审计与历史追溯）。"""
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT version, version_hash, MIN(effective_from) AS effective_from,"
                "       MAX(effective_to) AS effective_to, COUNT(*) AS rule_count "
                "FROM risk_rules GROUP BY version, version_hash "
                "ORDER BY effective_from DESC").fetchall()
        return [dict(r) for r in rows]

    # ---------- 市场环境 ----------

    def start_market_sync_run(self, run_id: str, started_at: str, *,
                              request_id: str | None = None) -> None:
        with self.connect() as conn:
            conn.execute("INSERT OR REPLACE INTO market_data_sync_runs "
                         "(run_id, started_at, run_state, request_id) VALUES (?,?,?,?)",
                         (run_id, started_at, "running", request_id))

    def finish_market_sync_run(self, run_id: str, *, run_state: str,
                               fetch_ms: int | None = None, instruments_ok: int = 0,
                               instruments_failed: int = 0, error_code: str | None = None,
                               error_message: str | None = None) -> None:
        with self.connect() as conn:
            conn.execute(
                "UPDATE market_data_sync_runs SET finished_at=?, run_state=?, fetch_ms=?,"
                " instruments_ok=?, instruments_failed=?, error_code=?, error_message=? "
                "WHERE run_id=?",
                (_utcnow_iso(), run_state, fetch_ms, instruments_ok, instruments_failed,
                 error_code, error_message, run_id))

    def latest_market_sync_run(self):
        with self.connect() as conn:
            return conn.execute("SELECT * FROM market_data_sync_runs "
                                "ORDER BY started_at DESC LIMIT 1").fetchone()

    def upsert_market_snapshots(self, rows: list[dict]) -> int:
        with self.connect() as conn:
            for r in rows:
                conn.execute(
                    "INSERT INTO market_data_snapshots (instrument_code, instrument_name,"
                    " data_as_of, fetched_at, open, high, low, close, previous_close,"
                    " daily_change, daily_change_percent, volume, source, delayed,"
                    " quality_status, quality_note, quote_as_of, raw_json)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
                    "ON CONFLICT(instrument_code, data_as_of) DO UPDATE SET "
                    " fetched_at=excluded.fetched_at, open=excluded.open,"
                    " high=excluded.high, low=excluded.low, close=excluded.close,"
                    " previous_close=excluded.previous_close,"
                    " daily_change=excluded.daily_change,"
                    " daily_change_percent=excluded.daily_change_percent,"
                    " volume=excluded.volume, source=excluded.source,"
                    " delayed=excluded.delayed, quote_as_of=excluded.quote_as_of,"
                    " quality_status=excluded.quality_status,"
                    " quality_note=excluded.quality_note, raw_json=excluded.raw_json",
                    (r["instrument_code"], r.get("instrument_name"), r["data_as_of"],
                     r["fetched_at"], _dec(r.get("open")), _dec(r.get("high")),
                     _dec(r.get("low")), _dec(r.get("close")),
                     _dec(r.get("previous_close")), _dec(r.get("daily_change")),
                     _dec(r.get("daily_change_percent")), r.get("volume"),
                     r.get("source"), int(bool(r.get("delayed", True))),
                     r.get("quality_status"), r.get("quality_note"),
                     r.get("quote_as_of"), r.get("raw_json")))
        return len(rows)

    def latest_market_snapshots(self) -> dict[str, dict]:
        """每个标的最新有效一条；实时源过期时自动选回 IBKR 历史。"""
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM (SELECT m.*, ROW_NUMBER() OVER ("
                " PARTITION BY instrument_code ORDER BY "
                " CASE quality_status WHEN 'fresh' THEN 0 WHEN 'stale' THEN 1 ELSE 2 END,"
                " data_as_of DESC, id DESC) AS _rank FROM market_data_snapshots m)"
                " WHERE _rank=1").fetchall()
        return {r["instrument_code"]: dict(r) for r in rows}

    def mark_live_market_stale(self, codes: list[str], *, note: str) -> int:
        """盘中报价缺失/过期时降级；没有 quote_as_of 的历史日线绝不误伤。"""
        if not codes:
            return 0
        placeholders = ",".join("?" for _ in codes)
        with self.connect() as conn:
            cur = conn.execute(
                f"UPDATE market_data_snapshots SET quality_status='stale', delayed=1,"
                f" quality_note=? WHERE quote_as_of IS NOT NULL"
                f" AND quality_status!='stale' AND instrument_code IN ({placeholders})",
                (note, *codes))
        return cur.rowcount

    def market_assessment_for(self, data_as_of: str, version: str):
        with self.connect() as conn:
            return conn.execute(
                "SELECT * FROM market_environment_assessments "
                "WHERE data_as_of=? AND calculation_version=?",
                (data_as_of, version)).fetchone()

    def latest_market_assessment(self):
        with self.connect() as conn:
            return conn.execute("SELECT * FROM market_environment_assessments "
                                "ORDER BY data_as_of DESC, created_at DESC LIMIT 1").fetchone()

    def save_market_assessment(self, p: dict) -> None:
        import json as _json
        with self.connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO market_environment_assessments "
                "(assessment_id, data_as_of, market_state, market_risk_level,"
                " market_risk_score, reliable, blocked_reason, component_scores,"
                " calculated_metrics, triggered_rules, top_market_drivers,"
                " quality_status, calculation_version, rule_set_version, rule_set_hash,"
                " created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (p["assessment_id"], p["data_as_of"], p["market_state"],
                 p["market_risk_level"], p.get("market_risk_score"),
                 int(bool(p.get("reliable"))), p.get("blocked_reason"),
                 _json.dumps(p.get("component_scores", {}), ensure_ascii=False, default=str),
                 _json.dumps(p.get("calculated_metrics", {}), ensure_ascii=False, default=str),
                 _json.dumps(p.get("triggered_rules", []), ensure_ascii=False),
                 _json.dumps(p.get("top_market_drivers", []), ensure_ascii=False, default=str),
                 p.get("quality_status"), p["calculation_version"],
                 p.get("rule_set_version"), p.get("rule_set_hash"), p["created_at"]))
            conn.execute("DELETE FROM market_environment_events WHERE assessment_id=?",
                         (p["assessment_id"],))
            rows = [(p["assessment_id"], e["event_code"], e["rule_code"], e["category"],
                     e["severity"], e["title"], e["explanation"], e.get("actual_value"),
                     e.get("threshold_value"), e.get("unit"), e.get("affected_instrument"),
                     e.get("recommended_check"), e.get("rule_version"),
                     e.get("detected_at") or p["created_at"])
                    for e in p.get("events", [])]
            if rows:
                conn.executemany(
                    "INSERT INTO market_environment_events (assessment_id, event_code,"
                    " rule_code, category, severity, title, explanation, actual_value,"
                    " threshold_value, unit, affected_instrument, recommended_check,"
                    " rule_version, detected_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)

    def market_events_for(self, assessment_id: str) -> list[dict]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT event_code, rule_code, category, severity, title, explanation,"
                "       actual_value, threshold_value, unit, affected_instrument,"
                "       recommended_check, rule_version, detected_at "
                "FROM market_environment_events WHERE assessment_id=? "
                "ORDER BY CASE severity WHEN 'critical' THEN 0 WHEN 'high' THEN 1 "
                "         ELSE 2 END, id", (assessment_id,)).fetchall()
        return [dict(r) for r in rows]

    def market_assessments_between(self, start: str | None, end: str | None, *,
                                   state: str | None = None, limit: int = 50,
                                   offset: int = 0) -> list[dict]:
        clauses, params = ["1=1"], []
        if start:
            clauses.append("data_as_of >= ?"); params.append(start)
        if end:
            clauses.append("data_as_of <= ?"); params.append(end)
        if state:
            clauses.append("market_state = ?"); params.append(state)
        params.extend([limit, offset])
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM market_environment_assessments WHERE "
                + " AND ".join(clauses) + " ORDER BY data_as_of DESC LIMIT ? OFFSET ?",
                params).fetchall()
        return [dict(r) for r in rows]

    def count_market_assessments(self, start: str | None, end: str | None,
                                 state: str | None = None) -> int:
        clauses, params = ["1=1"], []
        if start:
            clauses.append("data_as_of >= ?"); params.append(start)
        if end:
            clauses.append("data_as_of <= ?"); params.append(end)
        if state:
            clauses.append("market_state = ?"); params.append(state)
        with self.connect() as conn:
            return conn.execute("SELECT COUNT(*) FROM market_environment_assessments "
                                "WHERE " + " AND ".join(clauses), params).fetchone()[0]

    # ---------- 数据质量问题 ----------

    def quality_issues_for(self, snapshot_id: str) -> list[dict]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT issue_code, severity, category, message, affected_field, "
                "       expected_value, actual_value, blocks_ai, detected_at "
                "FROM data_quality_issues WHERE snapshot_id=? "
                "ORDER BY CASE severity WHEN 'critical' THEN 0 WHEN 'warning' THEN 1 "
                "         ELSE 2 END, id", (snapshot_id,)).fetchall()
        return [dict(r) for r in rows]

    def latest_snapshot_row(self, account: str | None = None):
        """最新一份账户快照（带 snapshot_id 与质量结论）。"""
        sql = ("SELECT * FROM balances_snapshot WHERE snapshot_id IS NOT NULL "
               + ("AND account=? " if account else "")
               + "ORDER BY ts DESC LIMIT 1")
        with self.connect() as conn:
            return conn.execute(sql, (account,) if account else ()).fetchone()

    def snapshots_between(self, start: str | None, end: str | None, *,
                          account: str | None = None, limit: int = 100,
                          offset: int = 0) -> list[dict]:
        """时间范围内的历史快照（倒序）。历史只增不改，天然可回溯。"""
        clauses, params = ["snapshot_id IS NOT NULL"], []
        if start:
            clauses.append("ts >= ?"); params.append(start)
        if end:
            clauses.append("ts <= ?"); params.append(end)
        if account:
            clauses.append("account = ?"); params.append(account)
        params.extend([limit, offset])
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM balances_snapshot WHERE " + " AND ".join(clauses)
                + " ORDER BY ts DESC LIMIT ? OFFSET ?", params).fetchall()
        return [dict(r) for r in rows]

    def count_snapshots_between(self, start: str | None, end: str | None,
                                account: str | None = None) -> int:
        clauses, params = ["snapshot_id IS NOT NULL"], []
        if start:
            clauses.append("ts >= ?"); params.append(start)
        if end:
            clauses.append("ts <= ?"); params.append(end)
        if account:
            clauses.append("account = ?"); params.append(account)
        with self.connect() as conn:
            return conn.execute("SELECT COUNT(*) FROM balances_snapshot WHERE "
                                + " AND ".join(clauses), params).fetchone()[0]

    def positions_for_snapshot(self, snapshot_id: str) -> list[dict]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM positions_snapshot WHERE snapshot_id=? ORDER BY symbol",
                (snapshot_id,)).fetchall()
        return [dict(r) for r in rows]

    def latest_sync_run(self):
        with self.connect() as conn:
            return conn.execute(
                "SELECT * FROM sync_runs ORDER BY started_at DESC LIMIT 1").fetchone()

    def last_successful_sync_run(self):
        with self.connect() as conn:
            return conn.execute(
                "SELECT * FROM sync_runs WHERE run_state IN ('success','partial') "
                "ORDER BY started_at DESC LIMIT 1").fetchone()

    def running_sync_run(self, *, stale_before: str):
        """当前仍在运行的同步任务；早于 stale_before 的视为僵死，不算并发。"""
        with self.connect() as conn:
            return conn.execute(
                "SELECT * FROM sync_runs WHERE run_state='running' AND started_at >= ? "
                "ORDER BY started_at DESC LIMIT 1", (stale_before,)).fetchone()

    def insert_positions_snapshot(self, ts: str, rows: Iterable[dict],
                                  sync_run_id: str | None = None) -> int:
        payload = [
            (ts, r["symbol"], _dec(r["qty"]), _dec(r["avg_cost"]),
             _dec(r["market_value"]), _dec(r["upl"]),
             _dec(r.get("price")), r.get("price_source"), r.get("price_quality"),
             r.get("market_data_type"), r.get("quote_as_of"), r.get("source"),
             sync_run_id, r.get("conid"), _dec(r.get("day_pnl")), r.get("day_pnl_source"),
             _dec(r.get("realized_pnl")), r.get("realized_pnl_source"))
            for r in rows
        ]
        with self.connect() as conn:
            conn.executemany(
                "INSERT OR REPLACE INTO positions_snapshot "
                "(ts, symbol, qty, avg_cost, market_value, upl, price, price_source, "
                " price_quality, market_data_type, quote_as_of, source, sync_run_id, "
                " conid, day_pnl, day_pnl_source, realized_pnl, realized_pnl_source) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                payload,
            )
        return len(payload)

    def insert_balances_snapshot(self, ts: str, b: dict, sync_run_id: str | None = None) -> None:
        with self.connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO balances_snapshot "
                "(ts, equity, cash, margin_balance, buying_power, account, source, "
                " data_quality, sync_run_id, total_cash) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (ts, _dec(b["equity"]), _dec(b["cash"]),
                 _dec(b["margin_balance"]), _dec(b["buying_power"]),
                 b.get("account"), b.get("source"), b.get("data_quality"), sync_run_id,
                 _dec(b.get("total_cash"))),
            )

    # ---------- 增量 upsert ----------

    def upsert_orders(self, rows: Iterable[dict]) -> int:
        now = _utcnow_iso()
        payload = [
            (r["order_id"], r["ts"], r.get("symbol"), r.get("side"),
             r.get("order_type"), _dec(r.get("qty")), _dec(r.get("price")),
             r["status"], r["raw_json"], now)
            for r in rows
        ]
        with self.connect() as conn:
            conn.executemany(
                "INSERT OR REPLACE INTO orders "
                "(order_id, ts, symbol, side, order_type, qty, price, status, raw_json, updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                payload,
            )
        return len(payload)

    def upsert_transactions(self, rows: Iterable[dict]) -> int:
        payload = [
            (r["txn_id"], r["ts"], r.get("symbol"), r.get("action"),
             _dec(r.get("qty")), _dec(r.get("price")), _dec(r.get("fees")),
             _dec(r.get("amount")), _dec(r.get("realized")), r["raw_json"])
            for r in rows
        ]
        with self.connect() as conn:
            conn.executemany(
                "INSERT OR REPLACE INTO transactions "
                "(txn_id, ts, symbol, action, qty, price, fees, amount, realized, raw_json) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                payload,
            )
        return len(payload)

    def realized_losses_from_executions(self, since_iso: str) -> list[sqlite3.Row]:
        """券商已实现亏损（executions.realized_pnl < 0，仅当前修正版本）。

        与 transactions 版的区别：executions 由 IBKR 成交事件推送，是实时源。
        """
        with self.connect() as conn:
            return conn.execute(
                "SELECT exec_id, execution_time AS ts, symbol, realized_pnl AS realized "
                "FROM executions WHERE realized_pnl IS NOT NULL "
                "AND CAST(realized_pnl AS REAL) < 0 AND is_current = 1 "
                "AND execution_time >= ? ORDER BY execution_time",
                (since_iso,),
            ).fetchall()

    def realized_loss_events_since(self, since_iso: str) -> list[sqlite3.Row]:
        """精确已实现亏损事件（realized 非空且<0），R6 用。"""
        with self.connect() as conn:
            return conn.execute(
                "SELECT txn_id, ts, symbol, realized FROM transactions "
                "WHERE realized IS NOT NULL AND CAST(realized AS REAL) < 0 AND ts >= ? "
                "ORDER BY ts",
                (since_iso,),
            ).fetchall()

    # ---------- 2A: executions（盘中增量，幂等 + correction + R6 水位） ----------

    def upsert_executions(self, rows: Iterable[dict], account: str | None = None) -> int:
        """幂等写入。冲突时只更新「可后到/可修正」的字段，不覆盖审计事实（时间/价格/数量）。

        commission/realized 后到 → data_status 由 PENDING_COMMISSION 升级为 READY，
        r6_state 保持 PENDING，从而让 R6 在数据完整后才判定。
        """
        rows = list(rows)
        if not rows:
            return 0
        now = _utcnow_iso()
        payload = [
            (account or r.get("account"), r["exec_id"],
             r["correction_root"], int(r.get("correction_seq", 0)),
             r.get("perm_id"), r.get("order_id"), r.get("client_id"), r.get("conid"),
             r.get("symbol"), r.get("sec_type"), r.get("exchange"), r.get("currency"),
             r.get("side"), _dec(r.get("shares")), _dec(r.get("price")),
             _dec(r.get("cumulative_qty")), _dec(r.get("average_price")),
             r["execution_time"], now,
             _dec(r.get("commission")), r.get("commission_currency"), _dec(r.get("realized_pnl")),
             1 if r.get("liquidation") else 0, r.get("order_ref"), r.get("last_liquidity"),
             r.get("source", "reqExecutions"), r.get("data_status", "PENDING_COMMISSION"),
             r.get("raw_json"))
            for r in rows
        ]
        with self.connect() as conn:
            conn.executemany(
                "INSERT INTO executions "
                "(account, exec_id, correction_root, correction_seq, perm_id, order_id, client_id, conid,"
                " symbol, sec_type, exchange, currency, side, shares, price, cumulative_qty,"
                " average_price, execution_time, received_at, commission, commission_currency,"
                " realized_pnl, liquidation, order_ref, last_liquidity, source, data_status, raw_json) "
                "VALUES (?,?,?,? ,?,?,?,? ,?,?,?,? ,?,?,?,? ,?,?,?,? ,?,?,?,? ,?,?,?,?) "
                "ON CONFLICT(exec_id) DO UPDATE SET "
                "  account=COALESCE(excluded.account, executions.account),"
                "  commission=COALESCE(excluded.commission, executions.commission),"
                "  commission_currency=COALESCE(excluded.commission_currency, executions.commission_currency),"
                "  realized_pnl=COALESCE(excluded.realized_pnl, executions.realized_pnl),"
                "  cumulative_qty=COALESCE(excluded.cumulative_qty, executions.cumulative_qty),"
                "  average_price=COALESCE(excluded.average_price, executions.average_price),"
                "  data_status=CASE WHEN excluded.data_status='READY' THEN 'READY' ELSE executions.data_status END",
                payload,
            )
            # correction：同 root 内 seq 最大者为当前有效，其余置 0
            roots = sorted({r["correction_root"] for r in rows})
            for root in roots:
                conn.execute(
                    "UPDATE executions SET is_current = CASE WHEN correction_seq = "
                    "  (SELECT MAX(correction_seq) FROM executions WHERE correction_root=?) "
                    "THEN 1 ELSE 0 END WHERE correction_root=?",
                    (root, root),
                )
        return len(payload)

    def pending_r6_executions(self) -> list[sqlite3.Row]:
        """待 R6 判定：当前有效修正 + commission 已到 + 有已实现亏损 + 未评估过。"""
        with self.connect() as conn:
            return conn.execute(
                "SELECT exec_id, symbol, realized_pnl, execution_time FROM executions "
                "WHERE is_current=1 AND data_status='READY' AND realized_pnl IS NOT NULL "
                "  AND CAST(realized_pnl AS REAL) < 0 AND r6_state='PENDING' "
                "ORDER BY execution_time",
            ).fetchall()

    def r6_candidate_executions(self) -> list[sqlite3.Row]:
        """待判决策的**全部**已实现成交（不限 r6_state）。

        部分成交是分批到达的：若只取 r6_state='PENDING' 的那几笔去合并，
        先到的会各自判定、各自标 DONE，后到的再也凑不齐合计金额 —— 漏洞照旧。
        所以先找出「含待判成交的 decision_key」，再把这些决策的全部成交取回来重算。
        同一决策的重复触发由 cooldown_exists_for / alert_state 拦截。
        """
        with self.connect() as conn:
            return conn.execute(
                "SELECT * FROM executions "
                "WHERE is_current=1 AND realized_pnl IS NOT NULL "
                "  AND COALESCE(decision_id, CAST(perm_id AS TEXT)) IN ("
                "    SELECT COALESCE(decision_id, CAST(perm_id AS TEXT)) FROM executions "
                "    WHERE is_current=1 AND data_status='READY' "
                "      AND realized_pnl IS NOT NULL AND CAST(realized_pnl AS REAL) < 0 "
                "      AND r6_state='PENDING') "
                "ORDER BY execution_time"
            ).fetchall()

    def mark_r6_done(self, exec_ids: Iterable[str]) -> int:
        ids = list(exec_ids)
        if not ids:
            return 0
        with self.connect() as conn:
            conn.executemany(
                "UPDATE executions SET r6_state='DONE' WHERE exec_id=?",
                [(i,) for i in ids],
            )
        return len(ids)

    def executions_since(self, since_iso: str) -> list[sqlite3.Row]:
        with self.connect() as conn:
            return conn.execute(
                "SELECT * FROM executions WHERE is_current=1 AND execution_time >= ? "
                "ORDER BY execution_time",
                (since_iso,),
            ).fetchall()

    def latest_realized_txn_ts(self) -> str | None:
        """最新一条带 realized 的成交时间（R6 独立 cursor 推进用）。"""
        with self.connect() as conn:
            row = conn.execute(
                "SELECT ts FROM transactions WHERE realized IS NOT NULL ORDER BY ts DESC LIMIT 1"
            ).fetchone()
        return row["ts"] if row else None

    def upsert_daily_bars(self, symbol: str, bars: Iterable[dict], *,
                          source: str = "IBKR_historical",
                          adjustment_mode: str = "TRADES_SPLIT_ADJUSTED_OR_UNKNOWN",
                          fetched_at: str | None = None,
                          corporate_action_provenance: str | None = None) -> int:
        """幂等写入日线，并保存行情来源、复权口径和抓取时间。

        调用方可在单根 bar 上覆盖 provenance；旧调用不传任何新字段时仍兼容，
        但不再产生来源不明的新数据。
        """
        fetched = fetched_at or _utcnow_iso()
        payload = []
        for b in bars:
            day = b["date"]
            payload.append((
                symbol, day, _dec(b["o"]), _dec(b["h"]), _dec(b["l"]),
                _dec(b["c"]), int(b["v"]), b.get("source") or source,
                b.get("adjustment_mode") or adjustment_mode,
                b.get("bar_as_of") or day, b.get("fetched_at") or fetched,
                b.get("corporate_action_provenance", corporate_action_provenance),
            ))
        if not payload:
            return 0
        with self.connect() as conn:
            conn.executemany(
                "INSERT INTO daily_bars "
                "(symbol,date,o,h,l,c,v,source,adjustment_mode,bar_as_of,fetched_at,"
                " corporate_action_provenance) VALUES (?,?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(symbol,date) DO UPDATE SET "
                "o=excluded.o,h=excluded.h,l=excluded.l,c=excluded.c,v=excluded.v,"
                "source=excluded.source,adjustment_mode=excluded.adjustment_mode,"
                "bar_as_of=excluded.bar_as_of,fetched_at=excluded.fetched_at,"
                "corporate_action_provenance=excluded.corporate_action_provenance",
                payload,
            )
        return len(payload)

    # ---------- app_secrets ----------

    def set_secret(self, key: str, value_encrypted: str) -> None:
        with self.connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO app_secrets (key, value_encrypted, updated_at) "
                "VALUES (?,?,?)",
                (key, value_encrypted, _utcnow_iso()),
            )

    def get_secret(self, key: str) -> str | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT value_encrypted FROM app_secrets WHERE key=?", (key,)
            ).fetchone()
        return row["value_encrypted"] if row else None

    # ---------- sync_state ----------

    def set_sync_state(self, key: str, value: str) -> None:
        with self.connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO sync_state (key, value, updated_at) VALUES (?,?,?)",
                (key, value, _utcnow_iso()),
            )

    def get_sync_state(self, key: str) -> str | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT value FROM sync_state WHERE key=?", (key,)
            ).fetchone()
        return row["value"] if row else None

    def set_live_state(self, payload: dict) -> None:
        """覆盖式发布 1 秒当前态；它不是历史快照，不增长数据库行数。"""
        self.set_sync_state("live_state_json", _json.dumps(
            payload, ensure_ascii=False, separators=(",", ":"), default=str))

    def get_live_state(self) -> dict | None:
        raw = self.get_sync_state("live_state_json")
        if not raw:
            return None
        try:
            value = _json.loads(raw)
        except (TypeError, ValueError):
            return None
        return value if isinstance(value, dict) else None

    # ---------- 投资日记 ----------

    def add_journal_entry(self, content: str) -> int:
        """新增一条日记，返回 id。created_at == updated_at。"""
        now = _utcnow_iso()
        with self.connect() as conn:
            cur = conn.execute(
                "INSERT INTO journal_entries "
                "(content, created_at, updated_at, source_type, is_readonly) "
                "VALUES (?,?,?,?,0)", (content, now, now, "MANUAL"),
            )
            return cur.lastrowid

    def list_journal_entries(self, limit: int = 500) -> list[dict]:
        """按更新时间倒序（最新在上）。"""
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT id, content, created_at, updated_at, source_type, "
                "position_cycle_id, account, is_readonly, metadata_json, archived_at "
                "FROM journal_entries "
                "ORDER BY updated_at DESC, id DESC LIMIT ?", (limit,),
            ).fetchall()
        return [dict(r) for r in rows]

    def update_journal_entry(self, entry_id: int, content: str) -> bool:
        """编辑内容并刷新 updated_at（列表会因此置顶）。"""
        with self.connect() as conn:
            cur = conn.execute(
                "UPDATE journal_entries SET content=?, updated_at=? "
                "WHERE id=? AND COALESCE(is_readonly, 0)=0 "
                "AND COALESCE(source_type, 'MANUAL')='MANUAL'",
                (content, _utcnow_iso(), entry_id),
            )
            return cur.rowcount > 0

    def delete_journal_entry(self, entry_id: int) -> bool:
        with self.connect() as conn:
            cur = conn.execute(
                "DELETE FROM journal_entries WHERE id=? AND COALESCE(is_readonly, 0)=0 "
                "AND COALESCE(source_type, 'MANUAL')='MANUAL'", (entry_id,))
            return cur.rowcount > 0

    # ---------- alert 去重 ----------

    def last_alert_ts(self, alert_key: str) -> datetime | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT ts FROM alert_log WHERE alert_key=? ORDER BY ts DESC LIMIT 1",
                (alert_key,),
            ).fetchone()
        return datetime.fromisoformat(row["ts"]) if row else None

    def record_alert(self, alert_key: str, message: str) -> None:
        with self.connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO alert_log (alert_key, ts, message) VALUES (?,?,?)",
                (alert_key, _utcnow_iso(), message),
            )

    # ---------- Phase 2: 止损位 ----------

    def upsert_stop_level(self, computed_date: str, adv: dict) -> None:
        with self.connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO stop_levels "
                "(symbol, computed_date, side, close, atr, swing, stop_price, "
                " risk_amount, risk_pct, exceeds_2pct, suggested_trim, reached_1r, "
                " trailing_stop, take_profit, atr_period, bars_used, data_sufficient, "
                " data_note, updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (adv["symbol"], computed_date, adv["side"], _dec(adv["close"]),
                 _dec(adv["atr"]), _dec(adv["swing"]), _dec(adv["stop_price"]),
                 _dec(adv.get("risk_amount")), _dec(adv.get("risk_pct")),
                 1 if adv.get("exceeds_2pct") else 0, int(adv.get("suggested_trim", 0)),
                 1 if adv.get("reached_1r") else 0, _dec(adv.get("trailing_stop")),
                 adv.get("take_profit"), adv.get("atr_period"), adv.get("bars_used"),
                 1 if adv.get("data_sufficient", True) else 0, adv.get("data_note"),
                 _utcnow_iso()),
            )

    def latest_stop_levels(self) -> list[sqlite3.Row]:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT computed_date FROM stop_levels ORDER BY computed_date DESC LIMIT 1"
            ).fetchone()
            if not row:
                return []
            return conn.execute(
                "SELECT * FROM stop_levels WHERE computed_date=? ORDER BY symbol",
                (row["computed_date"],),
            ).fetchall()

    # ---- 建议合成层（Phase 2）----

    def current_recommendations(self, account: str) -> list[sqlite3.Row]:
        """每个标的的当前建议（ACTIVE 或 BLOCKED）。首页「今日行动」与 API 用。"""
        with self.connect() as conn:
            return conn.execute(
                "SELECT * FROM recommendation_snapshots "
                "WHERE account=? AND status IN ('ACTIVE','BLOCKED') "
                "ORDER BY symbol", (account,)).fetchall()

    def recommendation_history(self, account: str, symbol: str,
                               limit: int = 50) -> list[sqlite3.Row]:
        """单标的建议历史（含 SUPERSEDED），最新在前。供追溯与效果统计。"""
        with self.connect() as conn:
            return conn.execute(
                "SELECT * FROM recommendation_snapshots WHERE account=? AND symbol=? "
                "ORDER BY created_at DESC LIMIT ?", (account, symbol, limit)).fetchall()

    def recommendations_for_review(self, limit: int = 2000) -> list[dict]:
        """全部建议快照（含已取代版本），供离线前向复盘。

        recommendation id 是复盘的 source reference；不只读当前建议，否则一旦建议
        被取代就会丢掉当时真实存在过的决策样本。
        """
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM recommendation_snapshots "
                "ORDER BY created_at DESC LIMIT ?", (int(limit),)).fetchall()
        return [dict(r) for r in rows]

    def active_plan_actions(self, cycle_id: str) -> list[sqlite3.Row]:
        """某持仓周期正式计划里 ACTIVE/TRIGGERED 的原始动作，供建议合成读取。
        只读正式计划的动作（草稿不入建议）。"""
        with self.connect() as conn:
            return conn.execute(
                "SELECT * FROM position_plan_actions WHERE position_cycle_id=? "
                "AND status IN ('ACTIVE','TRIGGERED') ORDER BY priority, created_at",
                (cycle_id,)).fetchall()

    # ---------- Phase 2: 冷静期 ----------

    def start_cooldown(self, started_at: str, ends_at: str, reason: str, trigger_ref: str) -> None:
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO cooldown_state (started_at, ends_at, reason, trigger_ref) "
                "VALUES (?,?,?,?)",
                (started_at, ends_at, reason, trigger_ref),
            )

    def active_cooldown(self, now_iso: str) -> sqlite3.Row | None:
        with self.connect() as conn:
            return conn.execute(
                "SELECT * FROM cooldown_state WHERE ends_at > ? ORDER BY ends_at DESC LIMIT 1",
                (now_iso,),
            ).fetchone()

    def cooldown_exists_for(self, trigger_ref: str) -> bool:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM cooldown_state WHERE trigger_ref=? LIMIT 1", (trigger_ref,)
            ).fetchone()
        return row is not None

    # ---------- Phase 2: 规则日志 ----------

    def record_rule_event(self, rule_id: str, severity: str, symbol: str | None,
                          title: str, message: str) -> None:
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO rule_events (ts, rule_id, severity, symbol, title, message) "
                "VALUES (?,?,?,?,?,?)",
                (_utcnow_iso(), rule_id, severity, symbol, title, message),
            )

    def recent_rule_events(self, limit: int = 100) -> list[sqlite3.Row]:
        with self.connect() as conn:
            return conn.execute(
                # 按 UTC 时间戳倒序；同一时刻多条用 id 兜底，保证顺序稳定
                "SELECT * FROM rule_events ORDER BY ts DESC, id DESC LIMIT ?",
                (limit,)
            ).fetchall()

    # ---------- 阶段一：规则运行审计（纯追加） ----------

    def record_rule_run(self, *, run_ts: str, run_status: str, snapshot_as_of: str | None,
                        account_ready: bool, data_sources: str, evaluations: str,
                        rules_version: str | None, thresholds_version: str | None,
                        error: str | None = None, sync_run_id: str | None = None,
                        positions_snapshot_id: str | None = None,
                        balances_snapshot_id: str | None = None,
                        quotes_as_of: str | None = None,
                        quote_quality_summary: str | None = None,
                        rule_summary: str | None = None,
                        per_symbol: str | None = None) -> None:
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO rule_runs "
                "(run_ts, run_status, snapshot_as_of, account_ready, data_sources, "
                " evaluations, rules_version, thresholds_version, error, sync_run_id, "
                " positions_snapshot_id, balances_snapshot_id, quotes_as_of, "
                " quote_quality_summary, rule_summary, per_symbol) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (run_ts, run_status, snapshot_as_of, 1 if account_ready else 0,
                 data_sources, evaluations, rules_version, thresholds_version, error,
                 sync_run_id, positions_snapshot_id, balances_snapshot_id, quotes_as_of,
                 quote_quality_summary, rule_summary, per_symbol),
            )

    def latest_rule_run(self) -> sqlite3.Row | None:
        with self.connect() as conn:
            return conn.execute(
                "SELECT * FROM rule_runs ORDER BY run_ts DESC, id DESC LIMIT 1"
            ).fetchone()

    # ---------- Phase 2: 成交（FILLED 订单）查询 ----------

    def filled_orders_since(self, since_iso: str) -> list[sqlite3.Row]:
        with self.connect() as conn:
            return conn.execute(
                "SELECT order_id, symbol, side, qty, price, ts, status FROM orders "
                "WHERE status='FILLED' AND ts >= ? ORDER BY ts",
                (since_iso,),
            ).fetchall()

    def latest_bar_date(self, symbol: str) -> str | None:
        """该标的已落库的最新日线日期（YYYY-MM-DD）；无数据返回 None。"""
        with self.connect() as conn:
            row = conn.execute(
                "SELECT MAX(date) AS d FROM daily_bars WHERE symbol=?", (symbol,)
            ).fetchone()
        return row["d"] if row and row["d"] else None

    def get_daily_bars(self, symbol: str, limit: int = 60) -> list[dict]:
        """取该标的最近 limit 根日线，按日期升序返回（供指标计算）。"""
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT date, o, h, l, c, v, source, adjustment_mode, bar_as_of, "
                "fetched_at, corporate_action_provenance FROM daily_bars WHERE symbol=? "
                "ORDER BY date DESC LIMIT ?",
                (symbol, limit),
            ).fetchall()
        rows = list(reversed(rows))
        return [
            {"date": r["date"], "o": to_decimal(r["o"]), "h": to_decimal(r["h"]),
             "l": to_decimal(r["l"]), "c": to_decimal(r["c"]), "v": r["v"],
             "source": r["source"], "adjustment_mode": r["adjustment_mode"],
             "bar_as_of": r["bar_as_of"], "fetched_at": r["fetched_at"],
             "corporate_action_provenance": r["corporate_action_provenance"]}
            for r in rows
        ]

    def latest_avg_cost(self, symbol: str) -> str | None:
        """该标的最近一次快照的均价（估算已实现盈亏用）。"""
        with self.connect() as conn:
            row = conn.execute(
                "SELECT avg_cost FROM positions_snapshot WHERE symbol=? ORDER BY ts DESC LIMIT 1",
                (symbol,),
            ).fetchone()
        return row["avg_cost"] if row else None

    # ---------- 只读查询（供 status / 看板） ----------

    def latest_positions(self) -> list[sqlite3.Row]:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT ts FROM positions_snapshot ORDER BY ts DESC LIMIT 1"
            ).fetchone()
            if not row:
                return []
            return conn.execute(
                "SELECT * FROM positions_snapshot WHERE ts=? ORDER BY symbol",
                (row["ts"],),
            ).fetchall()

    def latest_balances(self) -> sqlite3.Row | None:
        with self.connect() as conn:
            return conn.execute(
                "SELECT * FROM balances_snapshot ORDER BY ts DESC LIMIT 1"
            ).fetchone()

    # ---------- Phase 3: 看板查询 ----------

    def equity_curve(self, days: int = 180) -> list[tuple[str, str]]:
        """净资产曲线：每个交易日取最后一条 equity，按日期升序返回 (date, equity)。"""
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT substr(ts,1,10) AS d, equity, ts FROM balances_snapshot ORDER BY ts"
            ).fetchall()
        by_day: dict[str, str] = {}
        for r in rows:
            by_day[r["d"]] = r["equity"]  # 后写覆盖 => 当日最后一条
        items = sorted(by_day.items())
        return items[-days:]

    def query_transactions(self, *, symbol: str | None = None, start: str | None = None,
                           end: str | None = None, action: str | None = None,
                           limit: int = 2000) -> list[sqlite3.Row]:
        clauses, params = [], []
        if symbol:
            clauses.append("symbol = ?"); params.append(symbol)
        if action:
            clauses.append("action = ?"); params.append(action)
        if start:
            clauses.append("ts >= ?"); params.append(start)
        if end:
            clauses.append("ts <= ?"); params.append(end)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        params.append(limit)
        with self.connect() as conn:
            return conn.execute(
                f"SELECT * FROM transactions {where} ORDER BY ts DESC LIMIT ?", params
            ).fetchall()

    def execution_symbols(self) -> list[str]:
        """有过成交的标的（交易历史筛选用）。读 executions —— 与明细表同源。"""
        with self.connect() as conn:
            return [r["symbol"] for r in conn.execute(
                "SELECT DISTINCT symbol FROM executions WHERE symbol IS NOT NULL "
                "ORDER BY symbol")]

    def transaction_symbols(self) -> list[str]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT DISTINCT symbol FROM transactions WHERE symbol IS NOT NULL ORDER BY symbol"
            ).fetchall()
        return [r["symbol"] for r in rows]

    def snapshot_before(self, symbol: str, ts_iso: str) -> sqlite3.Row | None:
        """某标的在给定时间点(含)之前的最近一条持仓快照。"""
        with self.connect() as conn:
            return conn.execute(
                "SELECT * FROM positions_snapshot WHERE symbol=? AND ts <= ? "
                "ORDER BY ts DESC LIMIT 1",
                (symbol, ts_iso),
            ).fetchone()

    def last_cooldown(self) -> sqlite3.Row | None:
        """最近一次冷静期记录（含已结束的），用于「距上次大额认亏」计数。"""
        with self.connect() as conn:
            return conn.execute(
                "SELECT * FROM cooldown_state ORDER BY started_at DESC LIMIT 1"
            ).fetchone()

    # ---------- 第三阶段：账户级盈亏 ----------

    def insert_pnl_snapshot(self, p: dict) -> None:
        """写入一条 PnL 快照。哨兵值须由调用方（app.pnl）先过滤成 None。"""
        with self.connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO pnl_snapshot "
                "(account, as_of, et_date, daily_pnl, realized_pnl, unrealized_pnl, "
                " net_liquidation, source, quality, reset_policy, sync_run_id, "
                " daily_basis, created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (p["account"], p["as_of"], p["et_date"], _dec(p.get("daily_pnl")),
                 _dec(p.get("realized_pnl")), _dec(p.get("unrealized_pnl")),
                 _dec(p.get("net_liquidation")), p["source"], p["quality"],
                 p.get("reset_policy"), p.get("sync_run_id"), p.get("daily_basis"),
                 _utcnow_iso()),
            )

    def latest_pnl(self, account: str) -> sqlite3.Row | None:
        """最新一条 PnL 快照。

        **注意**：这是「最新」，不是「同周期」。用它当分子、再配另一个时刻的
        净资产当分母，就会造出跨周期比值 —— 那正是 ACC_DAILY_LOSS_HIGH 抖动
        11 次的成因。需要闭合比值时请用 `pnl_for_run()`。
        """
        with self.connect() as conn:
            return conn.execute(
                "SELECT * FROM pnl_snapshot WHERE account=? ORDER BY as_of DESC LIMIT 1",
                (account,),
            ).fetchone()

    def daily_pnl_series(self, account: str, start_date: str | None = None,
                         end_date: str | None = None) -> list[sqlite3.Row]:
        """每个内部会话日的最后一条有效 DailyPnL（仅供实时诊断/对账）。

        不能把本查询直接用作历史日收益：IBKR 官方说明 reqPnL 的重置日程由
        TWS 配置和品种决定；生产也实测到盘后重置发生在内部 et_date 翻转之前。
        历史表现应使用 Flex Change in NAV；未配置该栏目时使用 IBKR NAV 差额
        减去 Flex 官方外部资金流。
        """
        where = [
            "account=?", "daily_pnl IS NOT NULL", "quality<>'UNAVAILABLE'",
            # 未确认资金流时由净值反推的降级值仍可被转账污染，
            # 不能又以「权威 DailyPnL」名义回流到日历。
            "COALESCE(daily_basis,'')<>'NET_LIQ_CHANGE_UNADJUSTED'",
        ]
        args: list = [account]
        if start_date:
            where.append("et_date>=?")
            args.append(start_date)
        if end_date:
            where.append("et_date<=?")
            args.append(end_date)
        sql = (
            "SELECT * FROM (SELECT p.*, ROW_NUMBER() OVER ("
            "PARTITION BY et_date ORDER BY as_of DESC) AS _rn "
            "FROM pnl_snapshot p WHERE " + " AND ".join(where) + ") "
            "WHERE _rn=1 ORDER BY et_date"
        )
        with self.connect() as conn:
            return conn.execute(sql, args).fetchall()

    def pnl_for_run(self, account: str, sync_run_id: str) -> sqlite3.Row | None:
        """**同一采集批次**的 PnL 快照。取不到就返回 None ——
        调用方必须据此标 insufficient_data，不许退回 `latest_pnl()` 凑数。"""
        if not (account and sync_run_id):
            return None
        with self.connect() as conn:
            return conn.execute(
                "SELECT * FROM pnl_snapshot WHERE account=? AND sync_run_id=? "
                "ORDER BY as_of DESC LIMIT 1", (account, sync_run_id),
            ).fetchone()

    # ---------- 当日盈亏原子观测 ----------

    def insert_daily_pnl_observation(self, o: dict) -> bool:
        """写入一次观测。同 (account, observed_at, version) 幂等。"""
        with self.connect() as conn:
            cur = conn.execute(
                "INSERT OR IGNORE INTO daily_pnl_observations ("
                "account_id,session_date,observed_at,correlation_id,source,basis,"
                "daily_pnl,daily_pnl_observed_at,net_liquidation,"
                "net_liquidation_observed_at,numerator_snapshot_id,"
                "denominator_snapshot_id,observation_skew_seconds,"
                "external_cash_flow_adjustment,daily_loss_ratio,quality_status,"
                "reliable,quality_reasons,previous_source,current_source,"
                "source_changed,basis_changed,calculation_version,created_at"
                ") VALUES (" + ",".join("?" * 24) + ")",
                (o.get("account_id"), o.get("session_date"), o["observed_at"],
                 o.get("correlation_id"), o.get("source"), o.get("basis"),
                 o.get("daily_pnl"), o.get("daily_pnl_observed_at"),
                 o.get("net_liquidation"), o.get("net_liquidation_observed_at"),
                 o.get("numerator_snapshot_id"), o.get("denominator_snapshot_id"),
                 o.get("observation_skew_seconds"),
                 o.get("external_cash_flow_adjustment"), o.get("daily_loss_ratio"),
                 o["quality_status"], 1 if o.get("reliable") else 0,
                 _json.dumps(o.get("quality_reasons") or [], ensure_ascii=False),
                 o.get("previous_source"),
                 o.get("current_source"), 1 if o.get("source_changed") else 0,
                 1 if o.get("basis_changed") else 0, o["calculation_version"],
                 o.get("created_at") or _utcnow_iso()))
            return cur.rowcount > 0

    def latest_daily_pnl_observation(self, account: str | None = None
                                     ) -> sqlite3.Row | None:
        with self.connect() as conn:
            if account:
                return conn.execute(
                    "SELECT * FROM daily_pnl_observations WHERE account_id=? "
                    "ORDER BY observed_at DESC LIMIT 1", (account,)).fetchone()
            return conn.execute("SELECT * FROM daily_pnl_observations "
                                "ORDER BY observed_at DESC LIMIT 1").fetchone()

    def daily_pnl_observations_since(self, since_iso: str) -> list[sqlite3.Row]:
        with self.connect() as conn:
            return list(conn.execute(
                "SELECT * FROM daily_pnl_observations WHERE observed_at>=? "
                "ORDER BY observed_at", (since_iso,)))

    def insert_daily_pnl_comparison(self, c: dict) -> bool:
        with self.connect() as conn:
            cur = conn.execute(
                "INSERT OR IGNORE INTO daily_pnl_metric_comparisons ("
                "observed_at,session_date,correlation_id,old_source,new_source,"
                "old_ratio,new_ratio,old_quality,new_quality,old_outcome,"
                "new_outcome,divergence_type,old_would_transition,"
                "new_would_transition,calculation_version,created_at"
                ") VALUES (" + ",".join("?" * 16) + ")",
                (c["observed_at"], c.get("session_date"), c.get("correlation_id"),
                 c.get("old_source"), c.get("new_source"), c.get("old_ratio"),
                 c.get("new_ratio"), c.get("old_quality"), c.get("new_quality"),
                 c.get("old_outcome"), c.get("new_outcome"), c["divergence_type"],
                 1 if c.get("old_would_transition") else 0,
                 1 if c.get("new_would_transition") else 0,
                 c["calculation_version"], c.get("created_at") or _utcnow_iso()))
            return cur.rowcount > 0

    def daily_pnl_comparisons_since(self, since_iso: str) -> list[sqlite3.Row]:
        with self.connect() as conn:
            return list(conn.execute(
                "SELECT * FROM daily_pnl_metric_comparisons WHERE observed_at>=? "
                "ORDER BY observed_at", (since_iso,)))

    # ---------- IBKR PnL callback audit / double-source shadow ----------

    def insert_ibkr_pnl_callbacks(self, rows: Iterable[dict]) -> int:
        payload = [
            (r["callback_type"], r["received_at"], r.get("broker_timestamp"),
             r.get("request_id"), r.get("account"), r.get("model_code"),
             r.get("conid"), int(r.get("connection_generation") or 0),
             r.get("subscription_generation"), r.get("session_date"),
             r.get("reset_configuration"), r["source"],
             _dec(r.get("daily_pnl")), _dec(r.get("unrealized_pnl")),
             _dec(r.get("realized_pnl")), 1 if r.get("accepted") else 0,
             _json.dumps(r.get("quality_reasons") or [], ensure_ascii=False))
            for r in rows
        ]
        if not payload:
            return 0
        with self.connect() as conn:
            conn.executemany(
                "INSERT INTO ibkr_pnl_callbacks "
                "(callback_type, received_at, broker_timestamp, request_id, account,"
                " model_code, conid, connection_generation, subscription_generation,"
                " session_date, reset_configuration, source, daily_pnl, unrealized_pnl,"
                " realized_pnl, accepted, quality_reasons) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", payload)
        return len(payload)

    def insert_ibkr_pnl_subscription_events(self, rows: Iterable[dict]) -> int:
        payload = [
            (r["event_type"], r["occurred_at"], r.get("request_id"),
             r.get("account"), r.get("model_code"), r.get("conid"),
             int(r.get("connection_generation") or 0),
             r.get("subscription_generation"))
            for r in rows
        ]
        if not payload:
            return 0
        with self.connect() as conn:
            conn.executemany(
                "INSERT INTO ibkr_pnl_subscription_events "
                "(event_type, occurred_at, request_id, account, model_code, conid,"
                " connection_generation, subscription_generation) "
                "VALUES (?,?,?,?,?,?,?,?)", payload)
        return len(payload)

    def insert_pnl_reconciliation(self, row: dict) -> bool:
        with self.connect() as conn:
            cur = conn.execute(
                "INSERT OR REPLACE INTO pnl_source_reconciliations "
                "(observed_at, sync_run_id, snapshot_id, account, official_daily_pnl,"
                " official_realized_pnl, official_unrealized_pnl, position_daily_total,"
                " futu_intraday_mtm, futu_total_position_value, quote_coverage,"
                " official_position_status, futu_status, large_but_explained, limitations) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (row["observed_at"], row.get("sync_run_id"), row.get("snapshot_id"),
                 row.get("account"), _dec(row.get("official_daily_pnl")),
                 _dec(row.get("official_realized_pnl")),
                 _dec(row.get("official_unrealized_pnl")),
                 _dec(row.get("position_daily_total")),
                 _dec(row.get("futu_intraday_mtm")),
                 _dec(row.get("futu_total_position_value")),
                 _dec(row.get("quote_coverage")), row.get("official_position_status"),
                 row.get("futu_status"), 1 if row.get("large_but_explained") else 0,
                 _json.dumps(row.get("limitations") or [], ensure_ascii=False)))
        return cur.rowcount > 0

    def recent_pnl_reconciliations(self, *, since_iso: str | None = None,
                                   limit: int = 500) -> list[dict]:
        where, args = "", []
        if since_iso:
            where, args = "WHERE observed_at>=?", [since_iso]
        args.append(limit)
        with self.connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM pnl_source_reconciliations {where} "
                "ORDER BY observed_at DESC LIMIT ?", args).fetchall()
        return [dict(r) for r in rows]

    def insert_scheduler_job_diagnostic(self, row: dict) -> None:
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO scheduler_job_diagnostics "
                "(job_id, scheduled_at, observed_at, outcome, lateness_seconds, error_type) "
                "VALUES (?,?,?,?,?,?)",
                (row["job_id"], row.get("scheduled_at"), row["observed_at"],
                 row["outcome"], _dec(row.get("lateness_seconds")),
                 row.get("error_type")))

    def recent_scheduler_job_diagnostics(self, *, since_iso: str | None = None,
                                         job_id: str | None = None,
                                         limit: int = 1000) -> list[dict]:
        clauses, args = [], []
        if since_iso:
            clauses.append("observed_at>=?")
            args.append(since_iso)
        if job_id:
            clauses.append("job_id=?")
            args.append(job_id)
        where = "WHERE " + " AND ".join(clauses) if clauses else ""
        args.append(limit)
        with self.connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM scheduler_job_diagnostics {where} "
                "ORDER BY observed_at DESC LIMIT ?", args).fetchall()
        return [dict(r) for r in rows]

    def assessment_versions_since(self, since_iso: str) -> list[sqlite3.Row]:
        """窗口内各 calculation_version 的生效区间。

        用于回答「这条历史评估当时是哪条口径算的」—— 回滚不回填历史，
        版本串本身就是区间标记。
        """
        with self.connect() as conn:
            return list(conn.execute(
                "SELECT calculation_version, MIN(data_as_of) AS first_at,"
                " MAX(data_as_of) AS last_at, COUNT(*) AS n"
                " FROM risk_assessments WHERE data_as_of>=?"
                " GROUP BY calculation_version ORDER BY first_at", (since_iso,)))

    # ---------- X 推文数据（来源：同机 x-monitor，只读挂载后摄取落库） ----------

    def upsert_x_tweets(self, rows: list[dict]) -> int:
        """幂等写入。`tweet_id` 是主键，重复摄取不会产生第二行。"""
        if not rows:
            return 0
        payload = [
            (r["tweet_id"], r["author_handle"], r.get("author_name"),
             r.get("author_user_id"), r.get("text"), r.get("text_zh"),
             r.get("ai_summary"), r.get("enriched_at"), r.get("tweet_url"),
             r.get("is_reply"), r.get("is_retweet"), r.get("is_quote"),
             r.get("media_count"), r.get("created_at"), r.get("created_at_utc"),
             r["received_at"], r.get("source"), r["ingested_at"])
            for r in rows]
        with self.connect() as conn:
            conn.executemany(
                "INSERT OR REPLACE INTO x_tweets (tweet_id, author_handle,"
                " author_name, author_user_id, text, text_zh, ai_summary,"
                " enriched_at, tweet_url, is_reply, is_retweet, is_quote,"
                " media_count, created_at, created_at_utc, received_at, source,"
                " ingested_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                payload)
        return len(payload)

    def upsert_x_daily_reports(self, rows: list[dict]) -> int:
        if not rows:
            return 0
        with self.connect() as conn:
            conn.executemany(
                "INSERT OR REPLACE INTO x_daily_reports (day, report_json,"
                " created_at, ingested_at) VALUES (?,?,?,?)",
                [(r["day"], r.get("report_json"), r.get("created_at"),
                  r["ingested_at"]) for r in rows])
        return len(rows)

    def upsert_x_authors(self, rows: list[dict]) -> int:
        if not rows:
            return 0
        with self.connect() as conn:
            conn.executemany(
                "INSERT OR REPLACE INTO x_authors (handle, user_id, display_name,"
                " last_checked_at, active, ingested_at) VALUES (?,?,?,?,?,?)",
                [(r["handle"], r.get("user_id"), r.get("display_name"),
                  r.get("last_checked_at"), r.get("active"), r["ingested_at"])
                 for r in rows])
        return len(rows)

    def x_tweets_since(self, since_iso: str, *, limit: int = 200,
                       handle: str | None = None, before: str | None = None,
                       before_id: str | None = None) -> list[sqlite3.Row]:
        """按**发布时间**倒序取推文。

        排序用 `COALESCE(created_at_utc, received_at)` —— 绝不用 `created_at`
        （Twitter 原始格式，字符串排序会按星期几字母排）。
        """
        sql = ("SELECT * FROM x_tweets WHERE COALESCE(created_at_utc, received_at) >= ?")
        args: list = [since_iso]
        if handle:
            sql += " AND author_handle = ?"
            args.append(handle)
        if before:
            if before_id:
                sql += (" AND (COALESCE(created_at_utc, received_at) < ? OR "
                        "(COALESCE(created_at_utc, received_at) = ? AND tweet_id < ?))")
                args.extend([before, before, before_id])
            else:
                sql += " AND COALESCE(created_at_utc, received_at) < ?"
                args.append(before)
        sql += (" ORDER BY COALESCE(created_at_utc, received_at) DESC, "
                "tweet_id DESC LIMIT ?")
        args.append(int(limit))
        with self.connect() as conn:
            return list(conn.execute(sql, args))

    def x_daily_reports(self, *, limit: int = 30,
                        before_day: str | None = None) -> list[sqlite3.Row]:
        sql = "SELECT * FROM x_daily_reports"
        args: list = []
        if before_day:
            sql += " WHERE day < ?"
            args.append(before_day)
        sql += " ORDER BY day DESC LIMIT ?"
        args.append(int(limit))
        with self.connect() as conn:
            return list(conn.execute(sql, args))

    def x_authors(self) -> list[sqlite3.Row]:
        with self.connect() as conn:
            return list(conn.execute("SELECT * FROM x_authors ORDER BY handle"))

    def x_tweet_counts_by_author(self) -> list[sqlite3.Row]:
        with self.connect() as conn:
            return list(conn.execute(
                "SELECT author_handle, COUNT(*) n, MAX(received_at) last_recv"
                " FROM x_tweets GROUP BY author_handle"))

    def x_tweets_stats(self) -> dict:
        with self.connect() as conn:
            r = conn.execute(
                "SELECT COUNT(*) n, MIN(received_at) lo, MAX(received_at) hi,"
                " COUNT(DISTINCT author_handle) authors,"
                " SUM(CASE WHEN enriched_at IS NOT NULL THEN 1 ELSE 0 END) zh"
                " FROM x_tweets").fetchone()
            d = dict(r) if r else {}
            d["reports"] = conn.execute(
                "SELECT COUNT(*) FROM x_daily_reports").fetchone()[0]
            return d

    # ---------- 第三阶段：日终净资产（美东交易日） ----------

    def upsert_daily_equity(self, account: str, et_date: str, *, net_liquidation,
                            as_of: str, source: str, quality: str) -> None:
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO daily_equity (account, et_date, net_liquidation, as_of, "
                " source, quality) VALUES (?,?,?,?,?,?) "
                "ON CONFLICT(account, et_date) DO UPDATE SET "
                " net_liquidation=excluded.net_liquidation, as_of=excluded.as_of, "
                " source=excluded.source, quality=excluded.quality",
                (account, et_date, _dec(net_liquidation), as_of, source, quality),
            )

    def intraday_equity(self, account: str, start_iso: str, end_iso: str | None = None,
                        max_points: int = 240) -> list[tuple[str, str]]:
        """当日盘中净资产走势（balances_snapshot 是分钟级的）。

        1D 视图原先读 daily_equity，一天只有一个点 —— 画不出走势。
        均匀降采样到 max_points，避免一天 1440 个点撑爆 SVG。
        """
        sql = "SELECT ts, equity FROM balances_snapshot WHERE ts >= ?"
        args: list = [start_iso]
        if end_iso:
            sql += " AND ts < ?"
            args.append(end_iso)
        if account:
            sql += " AND (account IS NULL OR account = ?)"
            args.append(account)
        sql += " ORDER BY ts"
        with self.connect() as conn:
            rows = conn.execute(sql, args).fetchall()
        if len(rows) <= max_points:
            return [(r["ts"], r["equity"]) for r in rows]
        step = len(rows) / max_points
        picked = [rows[min(int(i * step), len(rows) - 1)] for i in range(max_points)]
        if picked[-1]["ts"] != rows[-1]["ts"]:
            picked.append(rows[-1])              # 最后一点必须是最新值
        return [(r["ts"], r["equity"]) for r in picked]

    def daily_equity_series(self, account: str, start_date: str | None = None,
                            end_date: str | None = None,
                            quality: str = "READY") -> list[sqlite3.Row]:
        """按美东日期升序返回日终净资产。默认只取 READY —— 降级数据不进正式曲线。"""
        sql = "SELECT * FROM daily_equity WHERE account=?"
        args: list = [account]
        if quality:
            sql += " AND quality=?"
            args.append(quality)
        if start_date:
            sql += " AND et_date>=?"
            args.append(start_date)
        if end_date:
            sql += " AND et_date<=?"
            args.append(end_date)
        sql += " ORDER BY et_date"
        with self.connect() as conn:
            return conn.execute(sql, args).fetchall()

    def daily_equity_on_or_before(self, account: str, et_date: str,
                                  quality: str = "READY") -> sqlite3.Row | None:
        """窗口起点：不晚于 et_date 的最近一个有效日终净值。"""
        with self.connect() as conn:
            return conn.execute(
                "SELECT * FROM daily_equity WHERE account=? AND et_date<=? AND quality=? "
                "ORDER BY et_date DESC LIMIT 1",
                (account, et_date, quality),
            ).fetchone()

    def earliest_daily_equity(self, account: str, quality: str = "READY") -> sqlite3.Row | None:
        with self.connect() as conn:
            return conn.execute(
                "SELECT * FROM daily_equity WHERE account=? AND quality=? "
                "ORDER BY et_date LIMIT 1",
                (account, quality),
            ).fetchone()

    # ---------- 第三阶段：资金流（追加式审计） ----------

    def add_cash_flow(self, *, account: str, amount, effective_at: str, et_date: str,
                      currency: str = "USD", note: str | None = None,
                      source: str = "MANUAL", external_ref: str | None = None,
                      reverses_id: int | None = None) -> int:
        """追加一条资金流。**只追加** —— 修正走 reverse_cash_flow，绝不 UPDATE/DELETE。"""
        with self.connect() as conn:
            cur = conn.execute(
                "INSERT INTO cash_flows (account, currency, amount, effective_at, et_date, "
                " note, source, external_ref, reverses_id, created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (account, currency, _dec(amount), effective_at, et_date, note, source,
                 external_ref, reverses_id, _utcnow_iso()),
            )
            return int(cur.lastrowid)

    def cash_flow_by_ref(self, account: str, external_ref: str) -> sqlite3.Row | None:
        """按券商流水号查已登记的资金流（Flex 回补的幂等键）。"""
        with self.connect() as conn:
            return conn.execute(
                "SELECT * FROM cash_flows WHERE account=? AND external_ref=? "
                "AND reverses_id IS NULL LIMIT 1",
                (account, external_ref),
            ).fetchone()

    def get_cash_flow(self, flow_id: int) -> sqlite3.Row | None:
        with self.connect() as conn:
            return conn.execute("SELECT * FROM cash_flows WHERE id=?", (flow_id,)).fetchone()

    def list_cash_flows(self, account: str, start_date: str | None = None,
                        end_date: str | None = None,
                        *, categories: tuple[str, ...] | None = None
                        ) -> list[sqlite3.Row]:
        """资金流。**默认只返回外部现金流**。

        Flex 接入后这张表还装着分红、利息、佣金与交易现金流。净值曲线的
        「扣除资金流后的盈亏」只能扣真正搬进搬出的钱 —— 把分红也扣掉会让
        当期收益凭空少一笔，把买入也扣掉更是直接算错业绩。所以默认口径收窄，
        需要全量的调用方显式传 categories。
        """
        cats = categories if categories is not None else ("external_cash_flow",
                                                          "transfer")
        sql = ("SELECT * FROM cash_flows WHERE account=? AND "
               f"COALESCE(category,'external_cash_flow') IN ({','.join('?' * len(cats))})")
        args: list = [account, *cats]
        if start_date:
            sql += " AND et_date>=?"
            args.append(start_date)
        if end_date:
            sql += " AND et_date<=?"
            args.append(end_date)
        sql += " ORDER BY et_date, id"
        with self.connect() as conn:
            return conn.execute(sql, args).fetchall()

    def set_cash_flow_coverage(self, *, account: str, start_date: str, end_date: str,
                               status: str, note: str | None = None,
                               source: str = "MANUAL") -> None:
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO cash_flow_coverage (account, start_date, end_date, status, "
                " note, source, created_at) VALUES (?,?,?,?,?,?,?) "
                "ON CONFLICT(account, start_date, end_date) DO UPDATE SET "
                " status=excluded.status, note=excluded.note, source=excluded.source",
                (account, start_date, end_date, status, note, source, _utcnow_iso()),
            )

    def cash_flow_coverages(self, account: str) -> list[sqlite3.Row]:
        with self.connect() as conn:
            return conn.execute(
                "SELECT * FROM cash_flow_coverage WHERE account=? AND status='COMPLETE' "
                "ORDER BY start_date",
                (account,),
            ).fetchall()

    def backfill_daily_equity(self, account: str, market_tz,
                              source: str = "BACKFILL_balances_snapshot") -> int:
        """从历史 balances_snapshot 按**美东交易日**回填 daily_equity（每日最后一条）。

        原曲线用 substr(ts,1,10) 按 UTC 日聚合，日期整体错位（ET 盘后落在次日 UTC）。
        此回填以 ET 日为准。幂等：重复执行结果一致。
        已存在的行不覆盖 —— 实时归档写的是权威值，回填只补历史空缺。
        """
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT ts, equity, account FROM balances_snapshot ORDER BY ts"
            ).fetchall()
            existing = {r["et_date"] for r in conn.execute(
                "SELECT et_date FROM daily_equity WHERE account=?", (account,))}
        by_day: dict[str, tuple[str, str]] = {}
        for r in rows:
            try:
                et = datetime.fromisoformat(r["ts"]).astimezone(market_tz).date().isoformat()
            except (TypeError, ValueError):
                continue
            by_day[et] = (r["equity"], r["ts"])     # 后写覆盖 => 该 ET 日最后一条
        n = 0
        for et_date, (equity, ts) in sorted(by_day.items()):
            if et_date in existing:
                continue
            self.upsert_daily_equity(account, et_date, net_liquidation=equity,
                                     as_of=ts, source=source, quality="READY")
            n += 1
        return n

    # ---------- decision / execution 计数（三种口径不可混用） ----------

    def executions_between(self, start_iso: str, end_iso: str | None = None,
                           *, current_only: bool = True) -> list[sqlite3.Row]:
        """区间内的成交。current_only=True 只取每个修正链的最新版本。"""
        sql = "SELECT * FROM executions WHERE execution_time >= ?"
        args: list = [start_iso]
        if end_iso:
            sql += " AND execution_time < ?"
            args.append(end_iso)
        if current_only:
            sql += " AND is_current = 1"
        sql += " ORDER BY execution_time"
        with self.connect() as conn:
            return conn.execute(sql, args).fetchall()

    def realized_pnl_records_by_symbol(self) -> dict[str, str]:
        """Flex 已实现盈亏记录的历史兜底汇总（不修改原始记录）。"""
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT symbol, realized_pnl FROM realized_pnl_records "
                "WHERE symbol IS NOT NULL AND realized_pnl IS NOT NULL").fetchall()
        totals: dict[str, Decimal] = {}
        for row in rows:
            try:
                totals[str(row["symbol"])] = (
                    totals.get(str(row["symbol"]), Decimal("0"))
                    + Decimal(str(row["realized_pnl"])))
            except (InvalidOperation, TypeError, ValueError):
                continue
        return {symbol: str(total) for symbol, total in totals.items()}

    def qty_before(self, symbol: str, ts_iso: str) -> str | None:
        """成交前的有符号持仓（决策类型分类用）。无快照 -> None（不得瞎猜）。"""
        row = self.snapshot_before(symbol, ts_iso)
        return row["qty"] if row else None

    # ---------- 告警生命周期 ----------

    def get_alert_state(self, dedup_key: str) -> sqlite3.Row | None:
        with self.connect() as conn:
            return conn.execute(
                "SELECT * FROM alert_state WHERE dedup_key=?", (dedup_key,)
            ).fetchone()

    def upsert_alert_active(self, *, dedup_key: str, rule_id: str, symbol: str | None,
                            severity: str, value, now_iso: str, notified: bool,
                            et_date: str) -> None:
        """记录某条告警本轮仍在触发。notified=True 时同步推进通知水位。"""
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO alert_state (dedup_key, rule_id, symbol, state, severity, "
                " notified_severity, first_triggered_at, last_seen_at, last_notified_at, "
                " notified_value, last_value, last_summary_date, notify_count) "
                "VALUES (?,?,?,'ACTIVE',?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(dedup_key) DO UPDATE SET "
                "  state='ACTIVE', severity=excluded.severity, last_seen_at=excluded.last_seen_at,"
                "  last_value=excluded.last_value,"
                # 从 RESOLVED 复活时重置首触发时间；否则保留原值
                "  first_triggered_at=CASE WHEN alert_state.state='RESOLVED' "
                "      THEN excluded.first_triggered_at ELSE alert_state.first_triggered_at END,"
                "  resolved_at=NULL,"
                "  notified_severity=CASE WHEN ? THEN excluded.notified_severity "
                "      ELSE alert_state.notified_severity END,"
                "  last_notified_at=CASE WHEN ? THEN excluded.last_notified_at "
                "      ELSE alert_state.last_notified_at END,"
                "  notified_value=CASE WHEN ? THEN excluded.notified_value "
                "      ELSE alert_state.notified_value END,"
                "  last_summary_date=CASE WHEN ? THEN excluded.last_summary_date "
                "      ELSE alert_state.last_summary_date END,"
                "  notify_count=alert_state.notify_count + CASE WHEN ? THEN 1 ELSE 0 END",
                (dedup_key, rule_id, symbol, severity, severity, now_iso, now_iso,
                 now_iso if notified else None, _dec(value), _dec(value),
                 et_date if notified else None, 1 if notified else 0,
                 notified, notified, notified, notified, notified),
            )

    def resolve_alert(self, dedup_key: str, now_iso: str) -> None:
        with self.connect() as conn:
            conn.execute(
                "UPDATE alert_state SET state='RESOLVED', resolved_at=? WHERE dedup_key=?",
                (now_iso, dedup_key),
            )

    def active_alert_keys(self) -> list[str]:
        with self.connect() as conn:
            return [r["dedup_key"] for r in conn.execute(
                "SELECT dedup_key FROM alert_state WHERE state='ACTIVE'")]

    def alert_states(self) -> list[sqlite3.Row]:
        with self.connect() as conn:
            return conn.execute(
                "SELECT * FROM alert_state ORDER BY last_seen_at DESC").fetchall()

    # ================= 风险事件运营层 =================

    def latest_event_for_identity(self, identity: str) -> dict | None:
        """该身份下最新的一轮 episode（可能已 resolved）。"""
        with self.connect() as conn:
            r = conn.execute(
                "SELECT * FROM risk_event_registry WHERE identity=? "
                "ORDER BY episode_seq DESC LIMIT 1", (identity,)).fetchone()
        return dict(r) if r else None

    def get_risk_event(self, event_id: str) -> dict | None:
        with self.connect() as conn:
            r = conn.execute("SELECT * FROM risk_event_registry WHERE event_id=?",
                             (event_id,)).fetchone()
        return dict(r) if r else None

    def create_risk_event(self, e: dict) -> None:
        cols = ("event_id, identity, episode_seq, source_type, source_event_id,"
                " event_code, rule_code, rule_version, rule_set_version, category,"
                " severity, max_severity, status, title, explanation, actual_value,"
                " threshold_value, unit, affected_symbol, affected_instrument,"
                " account_id, snapshot_id, assessment_id, data_as_of,"
                " first_detected_at, last_detected_at, occurrence_count,"
                " recommended_check, quality_status, reliable, notification_owner,"
                " rule_set_hash, market_assessment_id, calculation_version,"
                " created_at, updated_at")
        with self.connect() as conn:
            conn.execute(
                f"INSERT OR REPLACE INTO risk_event_registry ({cols}) "
                "VALUES (" + ",".join("?" * 36) + ")",
                (e["event_id"], e["identity"], e["episode_seq"], e["source_type"],
                 e.get("source_event_id"), e["event_code"], e["rule_code"],
                 e.get("rule_version"), e.get("rule_set_version"), e["category"],
                 e["severity"], e.get("max_severity") or e["severity"], e["status"],
                 e["title"], e["explanation"], e.get("actual_value"),
                 e.get("threshold_value"), e.get("unit"), e.get("affected_symbol"),
                 e.get("affected_instrument"), e.get("account_id"),
                 e.get("snapshot_id"), e.get("assessment_id"), e.get("data_as_of"),
                 e["first_detected_at"], e["last_detected_at"],
                 e.get("occurrence_count", 1), e.get("recommended_check"),
                 e.get("quality_status"), int(bool(e.get("reliable", True))),
                 e.get("notification_owner", "ops"),
                 e.get("rule_set_hash"), e.get("market_assessment_id"),
                 e.get("calculation_version"),
                 e["created_at"], e["updated_at"]))

    def touch_risk_event(self, event_id: str, *, now_iso: str, severity: str,
                         status: str, actual_value: str | None,
                         threshold_value: str | None, data_as_of: str | None,
                         assessment_id: str | None, snapshot_id: str | None,
                         quality_status: str | None, reliable: bool,
                         max_severity: str) -> None:
        """事件仍在触发：累计次数、更新当前值与严重度。

        occurrence_count 用 SQL 自增而非读改写 —— 并发评估下读改写会丢计数。
        """
        with self.connect() as conn:
            conn.execute(
                "UPDATE risk_event_registry SET last_detected_at=?, severity=?,"
                " max_severity=?, status=?, actual_value=?, threshold_value=?,"
                " data_as_of=COALESCE(?, data_as_of), assessment_id=COALESCE(?, assessment_id),"
                " snapshot_id=COALESCE(?, snapshot_id), quality_status=?, reliable=?,"
                " occurrence_count=occurrence_count+1, updated_at=?, resolved_at=NULL"
                " WHERE event_id=?",
                (now_iso, severity, max_severity, status, actual_value, threshold_value,
                 data_as_of, assessment_id, snapshot_id, quality_status,
                 int(bool(reliable)), now_iso, event_id))

    def resolve_risk_event(self, event_id: str, now_iso: str) -> None:
        with self.connect() as conn:
            conn.execute(
                "UPDATE risk_event_registry SET status='resolved', resolved_at=?,"
                " updated_at=? WHERE event_id=?", (now_iso, now_iso, event_id))

    def record_event_transition(self, t: dict) -> None:
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO risk_event_transitions (event_id, transition,"
                " previous_status, new_status, previous_severity, new_severity,"
                " actual_value, threshold_value, reason, actor, transitioned_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (t["event_id"], t["transition"], t.get("previous_status"),
                 t.get("new_status"), t.get("previous_severity"), t.get("new_severity"),
                 t.get("actual_value"), t.get("threshold_value"), t.get("reason"),
                 t.get("actor"), t["transitioned_at"]))

    def event_transitions(self, event_id: str) -> list[dict]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM risk_event_transitions WHERE event_id=? "
                "ORDER BY transitioned_at, id", (event_id,)).fetchall()
        return [dict(r) for r in rows]

    def mark_event_notified(self, event_id: str, *, now_iso: str,
                            severity: str, value: str | None) -> None:
        with self.connect() as conn:
            conn.execute(
                "UPDATE risk_event_registry SET notification_count=notification_count+1,"
                " last_notified_at=?, notified_severity=?, notified_value=?, updated_at=?"
                " WHERE event_id=?", (now_iso, severity, value, now_iso, event_id))

    def acknowledge_event(self, event_id: str, *, now_iso: str,
                          actor: str | None) -> None:
        """只改 acknowledged 标记，**不动 resolved_at，也不动风险是否存在**。"""
        with self.connect() as conn:
            conn.execute(
                "UPDATE risk_event_registry SET status='acknowledged',"
                " acknowledged_at=?, acknowledged_by=?, updated_at=?"
                " WHERE event_id=? AND status IN ('open','suppressed')",
                (now_iso, actor, now_iso, event_id))

    def suppress_event(self, event_id: str, *, until_iso: str, reason: str | None,
                       now_iso: str) -> None:
        with self.connect() as conn:
            conn.execute(
                "UPDATE risk_event_registry SET status='suppressed', suppressed_until=?,"
                " suppressed_reason=?, updated_at=? WHERE event_id=? AND status<>'resolved'",
                (until_iso, reason, now_iso, event_id))

    def expire_suppressions(self, now_iso: str) -> int:
        """抑制到期自动回到 open。事件本身不删，页面仍能看到抑制过的历史。"""
        with self.connect() as conn:
            cur = conn.execute(
                "UPDATE risk_event_registry SET status='open', suppressed_until=NULL,"
                " updated_at=? WHERE status='suppressed' AND suppressed_until IS NOT NULL"
                " AND suppressed_until<=?", (now_iso, now_iso))
            return cur.rowcount

    def query_risk_events(self, *, start=None, end=None, source_type=None,
                          category=None, severity=None, status=None, symbol=None,
                          rule_code=None, reliable=None, limit=100,
                          offset=0) -> tuple[list[dict], int]:
        where, args = [], []
        if start:
            where.append("last_detected_at>=?"); args.append(start)
        if end:
            where.append("first_detected_at<=?"); args.append(end)
        for col, val in (("source_type", source_type), ("category", category),
                         ("severity", severity), ("status", status),
                         ("rule_code", rule_code)):
            if val:
                vals = val if isinstance(val, (list, tuple)) else [val]
                where.append(f"{col} IN ({','.join('?' * len(vals))})")
                args.extend(vals)
        if symbol:
            where.append("(affected_symbol=? OR affected_instrument=?)")
            args.extend([symbol, symbol])
        if reliable is not None:
            where.append("reliable=?"); args.append(int(bool(reliable)))
        clause = (" WHERE " + " AND ".join(where)) if where else ""
        with self.connect() as conn:
            total = conn.execute(
                f"SELECT COUNT(*) FROM risk_event_registry{clause}", args).fetchone()[0]
            rows = conn.execute(
                f"SELECT * FROM risk_event_registry{clause} "
                "ORDER BY CASE status WHEN 'open' THEN 0 WHEN 'acknowledged' THEN 1 "
                "  WHEN 'suppressed' THEN 2 ELSE 3 END, "
                "CASE severity WHEN 'critical' THEN 0 WHEN 'high' THEN 1 "
                "  WHEN 'warning' THEN 2 ELSE 3 END, last_detected_at DESC "
                "LIMIT ? OFFSET ?", (*args, int(limit), int(offset))).fetchall()
        return [dict(r) for r in rows], int(total)

    def event_counts_by_severity(self, *, statuses=("open", "acknowledged",
                                                    "suppressed")) -> dict:
        ph = ",".join("?" * len(statuses))
        with self.connect() as conn:
            rows = conn.execute(
                f"SELECT severity, COUNT(*) c FROM risk_event_registry "
                f"WHERE status IN ({ph}) GROUP BY severity", tuple(statuses)).fetchall()
        return {r["severity"]: r["c"] for r in rows}

    def events_opened_between(self, start: str, end: str) -> int:
        with self.connect() as conn:
            return conn.execute(
                "SELECT COUNT(*) FROM risk_event_registry "
                "WHERE first_detected_at>=? AND first_detected_at<?",
                (start, end)).fetchone()[0]

    def events_resolved_between(self, start: str, end: str) -> int:
        with self.connect() as conn:
            return conn.execute(
                "SELECT COUNT(*) FROM risk_event_registry "
                "WHERE resolved_at>=? AND resolved_at<?", (start, end)).fetchone()[0]

    def oldest_unresolved_event(self) -> dict | None:
        with self.connect() as conn:
            r = conn.execute(
                "SELECT * FROM risk_event_registry WHERE status IN "
                "('open','acknowledged','suppressed') ORDER BY first_detected_at LIMIT 1"
            ).fetchone()
        return dict(r) if r else None

    # ---------------- 通知日志 ----------------

    def record_notification(self, n: dict) -> None:
        with self.connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO risk_notifications (notification_id, event_id,"
                " event_ids, channel, notification_type, severity, title,"
                " payload_summary, dedupe_key, attempted_at, sent_at, success,"
                " error_code, error_message, retry_count)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (n["notification_id"], n.get("event_id"), n.get("event_ids"),
                 n["channel"], n["notification_type"], n.get("severity"),
                 n.get("title"), n.get("payload_summary"), n.get("dedupe_key"),
                 n["attempted_at"], n.get("sent_at"), int(bool(n.get("success"))),
                 n.get("error_code"), n.get("error_message"),
                 int(n.get("retry_count", 0))))

    def notifications_for_event(self, event_id: str) -> list[dict]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM risk_notifications WHERE event_id=? OR event_ids LIKE ?"
                " ORDER BY attempted_at DESC", (event_id, f'%"{event_id}"%')).fetchall()
        return [dict(r) for r in rows]

    def notifications_between(self, start: str, end: str, *,
                              success_only: bool = True) -> list[dict]:
        sql = ("SELECT * FROM risk_notifications WHERE attempted_at>=? AND attempted_at<?"
               + (" AND success=1" if success_only else "")
               + " ORDER BY attempted_at DESC")
        with self.connect() as conn:
            return [dict(r) for r in conn.execute(sql, (start, end)).fetchall()]

    def count_notifications_since(self, since: str, *,
                                  success_only: bool = True) -> int:
        sql = ("SELECT COUNT(*) FROM risk_notifications WHERE attempted_at>=?"
               + (" AND success=1" if success_only else ""))
        with self.connect() as conn:
            return conn.execute(sql, (since,)).fetchone()[0]

    def risk_score_series(self, start: str | None, end: str | None, *,
                          account: str | None = None,
                          limit: int = 200000) -> list[dict]:
        """趋势用的正序全量序列（与分页用的 assessments_between 区分开）。"""
        where, args = [], []
        if start:
            where.append("data_as_of>=?"); args.append(start)
        if end:
            where.append("data_as_of<=?"); args.append(end)
        if account:
            where.append("account_id=?"); args.append(account)
        clause = (" WHERE " + " AND ".join(where)) if where else ""
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT assessment_id, data_as_of, risk_level, risk_score, reliable,"
                " component_scores FROM risk_assessments" + clause +
                " ORDER BY data_as_of LIMIT ?", (*args, int(limit))).fetchall()
        return [dict(r) for r in rows]

    def market_score_series(self, start: str | None, end: str | None,
                            *, limit: int = 100000) -> list[dict]:
        """市场评估正序序列。注意市场评估按 **交易日** 幂等，粒度天然是「日」，
        与账户的分钟级不同 —— 趋势 API 必须把这个差异显式告诉调用方。"""
        where, args = [], []
        if start:
            where.append("data_as_of>=?"); args.append(start)
        if end:
            where.append("data_as_of<=?"); args.append(end)
        clause = (" WHERE " + " AND ".join(where)) if where else ""
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT assessment_id, data_as_of, market_state, market_risk_level,"
                " market_risk_score, reliable, component_scores, created_at"
                " FROM market_environment_assessments" + clause +
                " ORDER BY data_as_of LIMIT ?", (*args, int(limit))).fetchall()
        return [dict(r) for r in rows]

    def assessment_by_id(self, assessment_id: str) -> dict | None:
        with self.connect() as conn:
            r = conn.execute("SELECT * FROM risk_assessments WHERE assessment_id=?",
                             (assessment_id,)).fetchone()
        return dict(r) if r else None

    def market_assessment_by_id(self, assessment_id: str) -> dict | None:
        with self.connect() as conn:
            r = conn.execute(
                "SELECT * FROM market_environment_assessments WHERE assessment_id=?",
                (assessment_id,)).fetchone()
        return dict(r) if r else None

    def rule_definition(self, rule_code: str,
                        version: str | None = None) -> dict | None:
        """某条规则的定义。给了 version 就取**当时那一版**的阈值 ——
        事件详情要回答的是「触发时用的是什么阈值」，不是「现在是什么阈值」。"""
        sql = "SELECT * FROM risk_rules WHERE rule_code=?"
        args: list = [rule_code]
        if version:
            sql += " AND version=?"
            args.append(version)
        sql += " ORDER BY effective_from DESC LIMIT 1"
        with self.connect() as conn:
            r = conn.execute(sql, args).fetchone()
        return dict(r) if r else None

    def all_bar_dates(self) -> list[str]:
        """所有标的的日线日期并集（交易日历退化来源）。"""
        with self.connect() as conn:
            return [r[0] for r in conn.execute(
                "SELECT DISTINCT date FROM daily_bars ORDER BY date")]

    def bars_in_range(self, symbol: str, start: str, end: str) -> list[dict]:
        """区间日线（闭区间，升序）。回测按时点取数用它。"""
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT date, o, h, l, c, v FROM daily_bars "
                "WHERE symbol=? AND date>=? AND date<=? ORDER BY date",
                (symbol, str(start)[:10], str(end)[:10])).fetchall()
        return [{"date": r["date"], "o": to_decimal(r["o"]), "h": to_decimal(r["h"]),
                 "l": to_decimal(r["l"]), "c": to_decimal(r["c"]), "v": r["v"]}
                for r in rows]

    def bars_up_to(self, symbol: str, as_of: str, limit: int = 400) -> list[dict]:
        """**截至** as_of（含）的最近 limit 根日线，升序。

        回测的取数入口：只给出该时点已经存在的数据，从源头上杜绝未来数据泄漏。
        """
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT date, o, h, l, c, v FROM daily_bars "
                "WHERE symbol=? AND date<=? ORDER BY date DESC LIMIT ?",
                (symbol, str(as_of)[:10], int(limit))).fetchall()
        rows = list(reversed(rows))
        return [{"date": r["date"], "o": to_decimal(r["o"]), "h": to_decimal(r["h"]),
                 "l": to_decimal(r["l"]), "c": to_decimal(r["c"]), "v": r["v"]}
                for r in rows]

    def equity_series_between(self, start: str, end: str,
                              account: str | None = None) -> list[dict]:
        """区间内的账户净值序列（升序）。前瞻结果的账户侧来源。"""
        sql = ("SELECT ts, equity, total_cash, excess_liquidity, maint_margin,"
               " positions_value FROM balances_snapshot WHERE ts>=? AND ts<=?")
        args: list = [start, end]
        if account:
            sql += " AND account=?"
            args.append(account)
        sql += " ORDER BY ts"
        with self.connect() as conn:
            return [dict(r) for r in conn.execute(sql, args).fetchall()]

    def executions_in_window(self, start: str, end: str,
                             symbol: str | None = None) -> list[dict]:
        """复盘窗口内的成交。用于「用户是否主动减仓」的判定 ——
        主动减仓导致损失变小，绝不能被解释成规则无效。

        与 executions_between 分开命名：那个返回 Row 且区间为左闭右开，
        被决策分类链路依赖，不能改签名。
        """
        sql = ("SELECT * FROM executions WHERE execution_time>=? AND execution_time<=?"
               " AND is_current=1")
        args: list = [start, end]
        if symbol:
            sql += " AND symbol=?"
            args.append(symbol)
        sql += " ORDER BY execution_time"
        with self.connect() as conn:
            return [dict(r) for r in conn.execute(sql, args).fetchall()]

    # ================= 复盘与回测 =================

    def positions_between(self, symbol: str, start: str, end: str) -> list[dict]:
        """区间内该标的的持仓快照（升序）。用于识别窗口内仓位变化。"""
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT ts, qty, avg_cost, market_value, upl, price FROM positions_snapshot"
                " WHERE symbol=? AND ts>=? AND ts<=? ORDER BY ts",
                (symbol, start, end)).fetchall()
        return [dict(r) for r in rows]

    def review_for_event(self, event_id: str, version: str) -> dict | None:
        with self.connect() as conn:
            r = conn.execute(
                "SELECT * FROM risk_event_reviews WHERE event_id=? AND calculation_version=?",
                (event_id, version)).fetchone()
        return dict(r) if r else None

    def save_event_review(self, r: dict) -> None:
        cols = ("review_id, event_id, source_type, rule_code, rule_version,"
                " rule_set_version, affected_symbol, affected_instrument,"
                " first_detected_at, peak_severity, peak_actual_value, resolved_at,"
                " duration_minutes, account_value_at_open, account_value_at_peak,"
                " account_value_at_resolution, market_state_at_open,"
                " account_risk_at_open, market_risk_at_open, combined_risk_at_open,"
                " forward_outcomes, outcome_labels, lead_time_minutes, lead_time_class,"
                " cash_flow_status, cash_flow_note, trading_activity, review_status,"
                " exclusion_reason, data_quality_status, calculation_version,"
                " created_at, updated_at")
        with self.connect() as conn:
            conn.execute(
                f"INSERT OR REPLACE INTO risk_event_reviews ({cols}) "
                "VALUES (" + ",".join("?" * 33) + ")",
                tuple(r.get(c.strip()) for c in cols.split(",")))

    def query_reviews(self, *, version: str, start=None, end=None, rule_code=None,
                      source_type=None, symbol=None, review_status=None,
                      limit=100, offset=0) -> tuple[list[dict], int]:
        where, args = ["calculation_version=?"], [version]
        if start:
            where.append("first_detected_at>=?"); args.append(start)
        if end:
            where.append("first_detected_at<=?"); args.append(end)
        for col, val in (("rule_code", rule_code), ("source_type", source_type),
                         ("review_status", review_status)):
            if val:
                vals = val if isinstance(val, (list, tuple)) else [val]
                where.append(f"{col} IN ({','.join('?' * len(vals))})")
                args.extend(vals)
        if symbol:
            where.append("(affected_symbol=? OR affected_instrument=?)")
            args.extend([symbol, symbol])
        clause = " WHERE " + " AND ".join(where)
        with self.connect() as conn:
            total = conn.execute(
                f"SELECT COUNT(*) FROM risk_event_reviews{clause}", args).fetchone()[0]
            rows = conn.execute(
                f"SELECT * FROM risk_event_reviews{clause} "
                "ORDER BY first_detected_at DESC LIMIT ? OFFSET ?",
                (*args, int(limit), int(offset))).fetchall()
        return [dict(r) for r in rows], int(total)

    def reviews_for_rule(self, rule_code: str, version: str,
                         statuses=("completed", "eligible")) -> list[dict]:
        ph = ",".join("?" * len(statuses))
        with self.connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM risk_event_reviews WHERE rule_code=? "
                f"AND calculation_version=? AND review_status IN ({ph})",
                (rule_code, version, *statuses)).fetchall()
        return [dict(r) for r in rows]

    def save_rule_performance(self, p: dict) -> None:
        cols = ("perf_id, rule_code, rule_set_version, source_type, category,"
                " observation_window, outcome_label, date_start, date_end,"
                " episodes_total, episodes_reviewed, episodes_insufficient,"
                " avg_duration_minutes, median_duration_minutes, triggers_per_month,"
                " notifications_total, notifications_per_hit,"
                " outcome_rate_when_triggered, outcome_rate_when_not_triggered,"
                " precision_value, recall_value, false_positive_rate,"
                " false_negative_rate, avg_lead_time_minutes, median_lead_time_minutes,"
                " avg_mae, avg_mfe, escalation_rate, resolution_rate, critical_rate,"
                " lead_class, sample_size, confidence, composite_score,"
                " score_components, current_threshold, calculation_version, computed_at")
        with self.connect() as conn:
            conn.execute(
                f"INSERT OR REPLACE INTO risk_rule_performance ({cols}) "
                "VALUES (" + ",".join("?" * 38) + ")",
                tuple(p.get(c.strip()) for c in cols.split(",")))

    def query_performance(self, *, rule_code=None, source_type=None,
                          observation_window=None, outcome_label=None,
                          version=None, limit=500) -> list[dict]:
        where, args = ["1=1"], []
        for col, val in (("rule_code", rule_code), ("source_type", source_type),
                         ("outcome_label", outcome_label),
                         ("calculation_version", version)):
            if val:
                where.append(f"{col}=?"); args.append(val)
        if observation_window:
            where.append("observation_window=?"); args.append(int(observation_window))
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM risk_rule_performance WHERE " + " AND ".join(where)
                + " ORDER BY CAST(COALESCE(composite_score,'0') AS REAL) DESC LIMIT ?",
                (*args, int(limit))).fetchall()
        return [dict(r) for r in rows]

    # ---------- 回测任务 ----------

    def find_backtest_by_hash(self, param_hash: str) -> dict | None:
        with self.connect() as conn:
            r = conn.execute(
                "SELECT * FROM risk_backtest_runs WHERE param_hash=? AND status='success'"
                " ORDER BY completed_at DESC LIMIT 1", (param_hash,)).fetchone()
        return dict(r) if r else None

    def create_backtest_run(self, r: dict) -> None:
        cols = ("backtest_run_id, param_hash, requested_rules, date_start, date_end,"
                " observation_windows, outcome_labels, candidate_thresholds, train_end,"
                " status, started_at, data_version, calculation_version, created_at")
        with self.connect() as conn:
            conn.execute(
                f"INSERT OR REPLACE INTO risk_backtest_runs ({cols}) "
                "VALUES (" + ",".join("?" * 14) + ")",
                tuple(r.get(c.strip()) for c in cols.split(",")))

    def finish_backtest_run(self, run_id: str, *, status: str, duration_ms: int,
                            rules_evaluated: int = 0, rules_failed: int = 0,
                            error_message: str | None = None) -> None:
        with self.connect() as conn:
            conn.execute(
                "UPDATE risk_backtest_runs SET status=?, completed_at=?, duration_ms=?,"
                " rules_evaluated=?, rules_failed=?, error_message=?"
                " WHERE backtest_run_id=?",
                (status, datetime.now(timezone.utc).isoformat(), duration_ms,
                 rules_evaluated, rules_failed, error_message, run_id))

    def get_backtest_run(self, run_id: str) -> dict | None:
        with self.connect() as conn:
            r = conn.execute("SELECT * FROM risk_backtest_runs WHERE backtest_run_id=?",
                             (run_id,)).fetchone()
        return dict(r) if r else None

    def save_backtest_results(self, rows: list[dict]) -> None:
        if not rows:
            return
        cols = ("backtest_run_id, rule_code, threshold_value, is_current_threshold,"
                " observation_window, outcome_label, segment, segment_start,"
                " segment_end, trigger_count, triggers_per_month, true_positive,"
                " false_positive, false_negative, true_negative, precision_value,"
                " recall_value, false_positive_rate, avg_lead_time_days,"
                " median_lead_time_days, max_consecutive_misses,"
                " estimated_notifications, delta_vs_current, sample_size,"
                " confidence, created_at")
        names = [c.strip() for c in cols.split(",")]
        with self.connect() as conn:
            conn.executemany(
                f"INSERT INTO risk_backtest_results ({cols}) "
                "VALUES (" + ",".join("?" * 26) + ")",
                [tuple(r.get(n) for n in names) for r in rows])

    def backtest_results(self, run_id: str, *, rule_code: str | None = None,
                         segment: str | None = None) -> list[dict]:
        sql = "SELECT * FROM risk_backtest_results WHERE backtest_run_id=?"
        args: list = [run_id]
        if rule_code:
            sql += " AND rule_code=?"; args.append(rule_code)
        if segment:
            sql += " AND segment=?"; args.append(segment)
        sql += " ORDER BY rule_code, CAST(threshold_value AS REAL), observation_window"
        with self.connect() as conn:
            return [dict(r) for r in conn.execute(sql, args).fetchall()]

    def save_combo_results(self, rows: list[dict]) -> None:
        if not rows:
            return
        cols = ("backtest_run_id, combo_code, member_rules, observation_window,"
                " outcome_label, combo_triggers, combo_precision, combo_recall,"
                " combo_lead_time_days, member_stats, recall_delta, precision_delta,"
                " sample_size, confidence, created_at")
        names = [c.strip() for c in cols.split(",")]
        with self.connect() as conn:
            conn.executemany(
                f"INSERT INTO risk_combo_results ({cols}) "
                "VALUES (" + ",".join("?" * 15) + ")",
                [tuple(r.get(n) for n in names) for r in rows])

    def combo_results(self, run_id: str) -> list[dict]:
        with self.connect() as conn:
            return [dict(r) for r in conn.execute(
                "SELECT * FROM risk_combo_results WHERE backtest_run_id=?"
                " ORDER BY combo_code", (run_id,)).fetchall()]

    def notification_count_for_rule(self, rule_code: str) -> int:
        """该规则累计触发过多少次通知（跨 episode）。"""
        with self.connect() as conn:
            return conn.execute(
                "SELECT COALESCE(SUM(notification_count),0) FROM risk_event_registry"
                " WHERE rule_code=?", (rule_code,)).fetchone()[0]

    def latest_backtest_run(self) -> dict | None:
        with self.connect() as conn:
            r = conn.execute(
                "SELECT * FROM risk_backtest_runs WHERE status IN ('success','partial')"
                " ORDER BY completed_at DESC LIMIT 1").fetchone()
        return dict(r) if r else None

    # ================= Flex Query 现金流层 =================

    def save_flex_run(self, r: dict) -> None:
        cols = ("run_id, query_id_masked, reference_code, started_at, completed_at,"
                " status, report_from, report_to, records_received, records_inserted,"
                " records_updated, records_skipped, realized_pnl_rows, poll_count,"
                " retry_count, duration_ms, error_code, error_message, request_id,"
                " report_hash, section_counts, trigger_source, created_at")
        names = [c.strip() for c in cols.split(",")]
        with self.connect() as conn:
            conn.execute(
                f"INSERT OR REPLACE INTO flex_sync_runs ({cols}) "
                "VALUES (" + ",".join("?" * len(names)) + ")",
                tuple(r.get(n) for n in names))

    def update_flex_run(self, run_id: str, **fields) -> None:
        if not fields:
            return
        sets = ", ".join(f"{k}=?" for k in fields)
        with self.connect() as conn:
            conn.execute(f"UPDATE flex_sync_runs SET {sets} WHERE run_id=?",
                         (*fields.values(), run_id))

    def finish_flex_run(self, run_id: str, **fields) -> None:
        fields.setdefault("completed_at", _utcnow_iso())
        self.update_flex_run(run_id, **fields)

    def latest_flex_run(self, status: str | None = None) -> dict | None:
        sql = "SELECT * FROM flex_sync_runs"
        args: list = []
        if status:
            sql += " WHERE status=?"
            args.append(status)
        sql += " ORDER BY started_at DESC LIMIT 1"
        with self.connect() as conn:
            r = conn.execute(sql, args).fetchone()
        return dict(r) if r else None

    def flex_runs(self, limit: int = 20) -> list[dict]:
        with self.connect() as conn:
            return [dict(r) for r in conn.execute(
                "SELECT * FROM flex_sync_runs ORDER BY started_at DESC LIMIT ?",
                (int(limit),)).fetchall()]

    def flex_run_with_hash(self, report_hash: str,
                           exclude_run_id: str | None = None) -> dict | None:
        sql = "SELECT * FROM flex_sync_runs WHERE report_hash=?"
        args: list = [report_hash]
        if exclude_run_id:
            sql += " AND run_id<>?"
            args.append(exclude_run_id)
        sql += " ORDER BY started_at DESC LIMIT 1"
        with self.connect() as conn:
            r = conn.execute(sql, args).fetchone()
        return dict(r) if r else None

    def flex_run_in_progress(self) -> bool:
        """是否有同步正在进行。手动触发的并发互斥用它。"""
        with self.connect() as conn:
            n = conn.execute(
                "SELECT COUNT(*) FROM flex_sync_runs WHERE status IN "
                "('pending','requesting','waiting_report','downloading','parsing')"
                " AND started_at > datetime('now','-10 minutes')").fetchone()[0]
        return bool(n)

    # ---------- 现金流幂等写入 ----------

    def upsert_cash_flow(self, row: dict) -> str:
        """幂等写入。返回 inserted / updated / skipped。

        去重键是 cashflow_id（优先券商流水号）。内容指纹相同即跳过 ——
        同一份报表重复下载不会重复计入收益。内容变了则更新，
        但**保留原始 created_at 与首次报告来源**，便于追溯修正历史。
        """
        cid = row.get("cashflow_id")
        if not cid:
            return "skipped"
        with self.connect() as conn:
            old = conn.execute(
                "SELECT id, content_hash, created_at, flex_run_id FROM cash_flows"
                " WHERE cashflow_id=?", (cid,)).fetchone()
            if old is not None:
                if old["content_hash"] == row.get("content_hash"):
                    return "skipped"
                conn.execute(
                    "UPDATE cash_flows SET amount=?, currency=?, amount_base=?,"
                    " fx_rate=?, occurred_at=?, effective_at=?, settled_at=?,"
                    " et_date=?, category=?, subtype=?, direction=?, symbol=?,"
                    " conid=?, description=?, note=?, raw_json=?, quality_status=?,"
                    " content_hash=?, updated_at=? WHERE cashflow_id=?",
                    (row["amount"], row["currency"], row.get("amount_base"),
                     row.get("fx_rate"), row.get("occurred_at"),
                     row.get("effective_at"), row.get("settled_at"),
                     row.get("et_date"), row.get("category"), row.get("subtype"),
                     row.get("direction"), row.get("symbol"), row.get("conid"),
                     row.get("description"), row.get("description"),
                     row.get("raw_json"), row.get("quality_status"),
                     row.get("content_hash"), row.get("updated_at"), cid))
                return "updated"
            conn.execute(
                "INSERT INTO cash_flows (cashflow_id, broker_transaction_id, account,"
                " account_id, currency, amount, amount_base, fx_rate, occurred_at,"
                " effective_at, settled_at, et_date, category, subtype, direction,"
                " symbol, conid, description, note, source, external_ref, raw_json,"
                " quality_status, content_hash, flex_run_id, created_at, updated_at)"
                " VALUES (" + ",".join("?" * 27) + ")",
                (cid, row.get("broker_transaction_id"), row.get("account"),
                 row.get("account_id"), row["currency"], row["amount"],
                 row.get("amount_base"), row.get("fx_rate"), row.get("occurred_at"),
                 row.get("effective_at"), row.get("settled_at"), row.get("et_date"),
                 row.get("category"), row.get("subtype"), row.get("direction"),
                 row.get("symbol"), row.get("conid"), row.get("description"),
                 row.get("description"), row.get("source"),
                 row.get("external_ref"), row.get("raw_json"),
                 row.get("quality_status"), row.get("content_hash"),
                 row.get("flex_run_id"), row.get("created_at"),
                 row.get("updated_at")))
            return "inserted"

    def query_cash_flows(self, *, start=None, end=None, category=None,
                         subtype=None, symbol=None, currency=None,
                         account=None, limit=200, offset=0
                         ) -> tuple[list[dict], int]:
        where, args = ["1=1"], []
        if start:
            where.append("et_date>=?"); args.append(str(start)[:10])
        if end:
            where.append("et_date<=?"); args.append(str(end)[:10])
        for col, val in (("category", category), ("subtype", subtype),
                         ("symbol", symbol), ("currency", currency),
                         ("account", account)):
            if val:
                vals = val if isinstance(val, (list, tuple)) else [val]
                where.append(f"{col} IN ({','.join('?' * len(vals))})")
                args.extend(vals)
        clause = " WHERE " + " AND ".join(where)
        with self.connect() as conn:
            total = conn.execute(
                f"SELECT COUNT(*) FROM cash_flows{clause}", args).fetchone()[0]
            rows = conn.execute(
                f"SELECT * FROM cash_flows{clause} ORDER BY et_date DESC, id DESC"
                " LIMIT ? OFFSET ?", (*args, int(limit), int(offset))).fetchall()
        return [dict(r) for r in rows], int(total)

    def cash_flow_category_counts(self) -> dict[str, int]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT COALESCE(category,'external_cash_flow') c, COUNT(*) n"
                " FROM cash_flows GROUP BY c").fetchall()
        return {r["c"]: r["n"] for r in rows}

    def cash_flow_totals(self, start: str, end: str, *,
                         account: str | None = None) -> dict[str, str]:
        """区间内按分类的金额合计（基础币种优先，缺失时退回原币金额）。"""
        sql = ("SELECT COALESCE(category,'external_cash_flow') c,"
               " SUM(CAST(COALESCE(amount_base, amount) AS REAL)) s"
               " FROM cash_flows WHERE et_date>=? AND et_date<=?")
        args: list = [str(start)[:10], str(end)[:10]]
        if account:
            sql += " AND account=?"
            args.append(account)
        sql += " GROUP BY c"
        with self.connect() as conn:
            return {r["c"]: str(r["s"] or 0)
                    for r in conn.execute(sql, args).fetchall()}

    def cash_flow_subtype_totals(self, start: str, end: str, *,
                                 account: str | None = None) -> dict[str, str]:
        sql = ("SELECT COALESCE(subtype,'unknown') s2,"
               " SUM(CAST(COALESCE(amount_base, amount) AS REAL)) s"
               " FROM cash_flows WHERE et_date>=? AND et_date<=?")
        args: list = [str(start)[:10], str(end)[:10]]
        if account:
            sql += " AND account=?"
            args.append(account)
        sql += " GROUP BY s2"
        with self.connect() as conn:
            return {r["s2"]: str(r["s"] or 0)
                    for r in conn.execute(sql, args).fetchall()}

    def external_flows_between(self, start_iso: str, end_iso: str, *,
                               account: str | None = None) -> list[dict]:
        """区间内的**外部**现金流（按 occurred_at）。业绩计算与复盘用它。"""
        sql = ("SELECT * FROM cash_flows WHERE"
               " COALESCE(category,'external_cash_flow') IN ('external_cash_flow','transfer')"
               " AND COALESCE(occurred_at, effective_at)>=?"
               " AND COALESCE(occurred_at, effective_at)<=?")
        args: list = [start_iso, end_iso]
        if account:
            sql += " AND account=?"
            args.append(account)
        sql += " ORDER BY COALESCE(occurred_at, effective_at)"
        with self.connect() as conn:
            return [dict(r) for r in conn.execute(sql, args).fetchall()]

    def upsert_realized_pnl(self, r: dict) -> None:
        cols = ("record_id, account_id, symbol, conid, open_date, close_date,"
                " quantity, proceeds, cost_basis, realized_pnl, commission,"
                " currency, fx_rate, transaction_id, source, raw_json,"
                " created_at, updated_at")
        names = [c.strip() for c in cols.split(",")]
        with self.connect() as conn:
            conn.execute(
                f"INSERT OR REPLACE INTO realized_pnl_records ({cols}) "
                "VALUES (" + ",".join("?" * len(names)) + ")",
                tuple(r.get(n) for n in names))

    def upsert_flex_nav_daily(self, r: dict) -> None:
        """写入 IBKR Flex Change in NAV 日级官方业绩。"""
        cols = ("account_id, et_date, currency, starting_value, ending_value,"
                " total_pnl, mtm, commissions, capital_movements,"
                " deposits_withdrawals, internal_cash_transfers, asset_transfers,"
                " dividends, interest, fees, other, twr, raw_json, source,"
                " report_hash, created_at, updated_at")
        names = [c.strip() for c in cols.split(",")]
        with self.connect() as conn:
            conn.execute(
                f"INSERT INTO flex_nav_daily ({cols}) VALUES ("
                + ",".join("?" * len(names)) + ") ON CONFLICT(account_id,et_date,currency)"
                " DO UPDATE SET starting_value=excluded.starting_value,"
                " ending_value=excluded.ending_value,total_pnl=excluded.total_pnl,"
                " mtm=excluded.mtm,commissions=excluded.commissions,"
                " capital_movements=excluded.capital_movements,"
                " deposits_withdrawals=excluded.deposits_withdrawals,"
                " internal_cash_transfers=excluded.internal_cash_transfers,"
                " asset_transfers=excluded.asset_transfers,dividends=excluded.dividends,"
                " interest=excluded.interest,fees=excluded.fees,other=excluded.other,"
                " twr=excluded.twr,raw_json=excluded.raw_json,source=excluded.source,"
                " report_hash=excluded.report_hash,updated_at=excluded.updated_at",
                tuple(r.get(n) for n in names))

    def flex_nav_daily_series(self, account: str, start_date: str | None = None,
                              end_date: str | None = None,
                              currency: str = "USD") -> list[dict]:
        where = ["account_id=?", "currency=?"]
        args: list = [account, currency]
        if start_date:
            where.append("et_date>=?"); args.append(start_date)
        if end_date:
            where.append("et_date<=?"); args.append(end_date)
        with self.connect() as conn:
            return [dict(x) for x in conn.execute(
                "SELECT * FROM flex_nav_daily WHERE " + " AND ".join(where)
                + " ORDER BY et_date", args).fetchall()]

    def upsert_flex_transfer(self, r: dict) -> None:
        """写入 IBKR Flex 证券转仓明细；稳定 transfer_id 保证重复同步幂等。"""
        cols = ("transfer_id, account_id, report_date, transfer_date, settle_date,"
                " transfer_type, direction, transfer_company, delivering_broker,"
                " currency, asset_category, symbol, description, conid, quantity,"
                " transfer_price, cost_basis, position_amount, position_amount_base,"
                " pnl_amount, pnl_amount_base, cash_transfer, transaction_id, code,"
                " level_of_detail, raw_json, source, report_hash, created_at, updated_at")
        names = [c.strip() for c in cols.split(",")]
        updates = ",".join(f"{n}=excluded.{n}" for n in names[1:] if n != "created_at")
        with self.connect() as conn:
            conn.execute(
                f"INSERT INTO flex_transfers ({cols}) VALUES ("
                + ",".join("?" * len(names))
                + f") ON CONFLICT(transfer_id) DO UPDATE SET {updates}",
                tuple(r.get(n) for n in names))

    def flex_transfers_series(self, account: str, start_date: str | None = None,
                              end_date: str | None = None) -> list[dict]:
        where = ["account_id=?"]
        args: list = [account]
        if start_date:
            where.append("report_date>=?"); args.append(start_date)
        if end_date:
            where.append("report_date<=?"); args.append(end_date)
        with self.connect() as conn:
            return [dict(x) for x in conn.execute(
                "SELECT * FROM flex_transfers WHERE " + " AND ".join(where)
                + " ORDER BY report_date, transaction_id, transfer_id", args).fetchall()]

    def realized_pnl_between(self, start: str, end: str, *,
                             account: str | None = None) -> list[dict]:
        sql = ("SELECT * FROM realized_pnl_records WHERE close_date>=? AND close_date<=?")
        args: list = [start, end]
        if account:
            sql += " AND account_id=?"
            args.append(account)
        sql += " ORDER BY close_date"
        with self.connect() as conn:
            return [dict(r) for r in conn.execute(sql, args).fetchall()]

    def realized_pnl_total(self, start: str, end: str,
                           account: str | None = None) -> str | None:
        sql = ("SELECT SUM(CAST(realized_pnl AS REAL)) FROM realized_pnl_records"
               " WHERE close_date>=? AND close_date<=?")
        args: list = [start, end]
        if account:
            sql += " AND account_id=?"
            args.append(account)
        with self.connect() as conn:
            v = conn.execute(sql, args).fetchone()[0]
        return None if v is None else str(v)

    def rebuild_income_expense(self, account: str | None, start: str, end: str,
                               version: str, now_iso: str) -> int:
        """按日 × 分类重建收入支出汇总（幂等）。"""
        with self.connect() as conn:
            conn.execute(
                "DELETE FROM income_expense_records WHERE et_date>=? AND et_date<=?"
                " AND calculation_version=?", (start, end, version))
            rows = conn.execute(
                "SELECT account, et_date, COALESCE(category,'external_cash_flow') c,"
                " COALESCE(subtype,'') s, currency,"
                " SUM(CAST(amount AS REAL)) amt,"
                " SUM(CAST(COALESCE(amount_base, amount) AS REAL)) amt_base,"
                " COUNT(*) n FROM cash_flows"
                " WHERE et_date>=? AND et_date<=?"
                + (" AND account=?" if account else "")
                + " GROUP BY account, et_date, c, s, currency",
                ((start, end, account) if account else (start, end))).fetchall()
            conn.executemany(
                "INSERT OR REPLACE INTO income_expense_records"
                " (account_id, et_date, category, subtype, currency, amount,"
                "  amount_base, record_count, calculation_version, created_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?)",
                [(r["account"], r["et_date"], r["c"], r["s"], r["currency"],
                  str(r["amt"]), str(r["amt_base"]), r["n"], version, now_iso)
                 for r in rows])
            return len(rows)

    def save_performance_period(self, p: dict) -> None:
        cols = ("period_id, account_id, period_start, period_end, granularity,"
                " opening_nav, closing_nav, raw_nav_change, external_cash_flow,"
                " deposits, withdrawals, cash_flow_adjusted_pnl,"
                " modified_dietz_return, time_weighted_return, realized_pnl,"
                " unrealized_pnl, dividends, interest, commissions, fees, taxes,"
                " fx_effect, market_pnl, unexplained_difference,"
                " calculation_method, weighting_assumption, data_quality,"
                " quality_note, cash_flow_source, calculation_version, created_at")
        names = [c.strip() for c in cols.split(",")]
        with self.connect() as conn:
            conn.execute(
                f"INSERT OR REPLACE INTO performance_periods ({cols}) "
                "VALUES (" + ",".join("?" * len(names)) + ")",
                tuple(p.get(n) for n in names))

    def performance_periods(self, start: str, end: str, *,
                            granularity: str = "day",
                            account: str | None = None,
                            version: str | None = None) -> list[dict]:
        sql = ("SELECT * FROM performance_periods WHERE period_end>=?"
               " AND period_end<=? AND granularity=?")
        args: list = [start, end, granularity]
        if account:
            sql += " AND account_id=?"
            args.append(account)
        if version:
            sql += " AND calculation_version=?"
            args.append(version)
        sql += " ORDER BY period_end"
        with self.connect() as conn:
            return [dict(r) for r in conn.execute(sql, args).fetchall()]

    def save_reconciliation(self, r: dict) -> None:
        cols = ("recon_id, account_id, period_start, period_end, currency,"
                " opening_cash, closing_cash, external_cash_flow, trade_cash_flow,"
                " income, expense, tax, interest, other, expected_closing_cash,"
                " reconciliation_difference, tolerance, quality_status, issues,"
                " calculation_version, created_at")
        names = [c.strip() for c in cols.split(",")]
        with self.connect() as conn:
            conn.execute(
                f"INSERT OR REPLACE INTO cash_reconciliations ({cols}) "
                "VALUES (" + ",".join("?" * len(names)) + ")",
                tuple(r.get(n) for n in names))

    def latest_reconciliation(self, account: str | None = None) -> dict | None:
        sql = "SELECT * FROM cash_reconciliations"
        args: list = []
        if account:
            sql += " WHERE account_id=?"
            args.append(account)
        sql += " ORDER BY period_end DESC, created_at DESC LIMIT 1"
        with self.connect() as conn:
            r = conn.execute(sql, args).fetchone()
        return dict(r) if r else None

    def cash_balance_at(self, ts_iso: str, account: str | None = None) -> str | None:
        """某时点最近一条快照的现金余额。对账的期初/期末来源。"""
        sql = ("SELECT total_cash, cash FROM balances_snapshot WHERE ts<=?")
        args: list = [ts_iso]
        if account:
            sql += " AND account=?"
            args.append(account)
        sql += " ORDER BY ts DESC LIMIT 1"
        with self.connect() as conn:
            r = conn.execute(sql, args).fetchone()
        if not r:
            return None
        return r["total_cash"] if r["total_cash"] is not None else r["cash"]

    # ---------- 历史重放（只读） ----------

    def positions_as_of(self, ts_iso: str) -> list[dict]:
        """某时点最近一批持仓快照。"""
        with self.connect() as conn:
            snap = conn.execute(
                "SELECT MAX(ts) t FROM positions_snapshot WHERE ts<=?",
                (ts_iso,)).fetchone()
            if not snap or not snap["t"]:
                return []
            return [dict(r) for r in conn.execute(
                "SELECT * FROM positions_snapshot WHERE ts=?", (snap["t"],))]

    def assessment_as_of(self, ts_iso: str,
                         account: str | None = None) -> dict | None:
        """某时点**当时已存在**的最近一次风险评估。绝不取之后的。"""
        sql = "SELECT * FROM risk_assessments WHERE data_as_of<=?"
        args: list = [ts_iso]
        if account:
            sql += " AND account_id=?"
            args.append(account)
        sql += " ORDER BY data_as_of DESC LIMIT 1"
        with self.connect() as conn:
            r = conn.execute(sql, args).fetchone()
        return dict(r) if r else None

    def rules_effective_at(self, ts_iso: str) -> list[dict]:
        """该时点生效的规则定义（用当时的版本解释当时的事件）。"""
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM risk_rules WHERE effective_from<=?"
                " AND (effective_to IS NULL OR effective_to>?)",
                (ts_iso, ts_iso)).fetchall()
        return [dict(r) for r in rows]

    def cash_flows_known_at(self, ts_iso: str,
                            account: str | None = None) -> list[dict]:
        """**该时点已经入库**的现金流。

        用 created_at 而非 occurred_at 过滤：Flex 是 T+1 的，一笔昨天发生的
        入金今天才同步进来，在昨天做判断时它并不存在。用 occurred_at 会把
        「事后才知道的事实」当成当时可用的信息 —— 那是标准的未来数据泄漏。
        """
        sql = ("SELECT * FROM cash_flows WHERE created_at<=?"
               " AND COALESCE(category,'external_cash_flow') IN"
               " ('external_cash_flow','transfer')")
        args: list = [ts_iso]
        if account:
            sql += " AND account=?"
            args.append(account)
        sql += " ORDER BY COALESCE(occurred_at, effective_at)"
        with self.connect() as conn:
            return [dict(r) for r in conn.execute(sql, args).fetchall()]

    def flex_runs_by_trigger(self, trigger: str, limit: int = 5) -> list[dict]:
        """按触发来源查同步记录。用于确认手动与定时走的是同一套流程。"""
        with self.connect() as conn:
            return [dict(r) for r in conn.execute(
                "SELECT * FROM flex_sync_runs WHERE trigger_source=?"
                " ORDER BY started_at DESC LIMIT ?", (trigger, int(limit)))]

    # ================= 余额锚点 / 汇率 / 未实现盈亏 =================

    def upsert_statement_balances(self, rows: list[dict]) -> int:
        """余额锚点幂等写入。券商修正报表时更新，保留原始 created_at 与来源。"""
        if not rows:
            return 0
        n = 0
        with self.connect() as conn:
            for r in rows:
                old = conn.execute(
                    "SELECT created_at FROM statement_cash_balances WHERE balance_id=?",
                    (r["balance_id"],)).fetchone()
                created = old["created_at"] if old else r["created_at"]
                conn.execute(
                    "INSERT OR REPLACE INTO statement_cash_balances"
                    " (balance_id, account_id, report_from, report_to, balance_date,"
                    "  balance_type, currency, balance, balance_base, fx_rate_to_base,"
                    "  level_of_detail, description, node_index, source, report_hash,"
                    "  quality_status, created_at, updated_at)"
                    " VALUES (" + ",".join("?" * 18) + ")",
                    (r["balance_id"], r.get("account_id"), r.get("report_from"),
                     r.get("report_to"), r["balance_date"], r["balance_type"],
                     r["currency"], r["balance"], r.get("balance_base"),
                     r.get("fx_rate_to_base"), r.get("level_of_detail"),
                     r.get("description"), r.get("node_index"),
                     r.get("source", "FLEX_STATEMENT_OF_FUNDS"),
                     r.get("report_hash"), r.get("quality_status", "ok"),
                     created, r["updated_at"]))
                n += 1
        return n

    def statement_balances(self, *, account: str | None = None,
                           report_from: str | None = None,
                           report_to: str | None = None) -> list[dict]:
        sql = "SELECT * FROM statement_cash_balances WHERE 1=1"
        args: list = []
        if account:
            sql += " AND account_id=?"; args.append(account)
        if report_from:
            sql += " AND report_from=?"; args.append(report_from)
        if report_to:
            sql += " AND report_to=?"; args.append(report_to)
        sql += " ORDER BY currency, balance_date, node_index"
        with self.connect() as conn:
            return [dict(r) for r in conn.execute(sql, args).fetchall()]

    def query_statement_balances(self, *, account: str | None = None,
                                 limit: int = 20, offset: int = 0,
                                 exclude_report_from: str | None = None,
                                 exclude_report_to: str | None = None,
                                 ) -> tuple[list[dict], int]:
        """余额锚点历史分页；最近记录优先，避免页面一次加载完整归档。"""
        filters: list[str] = []
        args: list = []
        if account:
            filters.append("account_id=?")
            args.append(account)
        if exclude_report_from is not None and exclude_report_to is not None:
            filters.append("NOT (report_from=? AND report_to=?)")
            args.extend([exclude_report_from, exclude_report_to])
        where = " WHERE " + " AND ".join(filters) if filters else ""
        with self.connect() as conn:
            total = conn.execute(
                "SELECT COUNT(*) FROM statement_cash_balances" + where,
                args).fetchone()[0]
            rows = conn.execute(
                "SELECT * FROM statement_cash_balances" + where +
                " ORDER BY balance_date DESC, node_index DESC LIMIT ? OFFSET ?",
                (*args, int(limit), int(offset))).fetchall()
        return [dict(r) for r in rows], int(total)

    def latest_balance_period(self, account: str | None = None
                              ) -> tuple[str | None, str | None]:
        """最近一份带余额锚点的报表区间。"""
        sql = ("SELECT report_from, report_to FROM statement_cash_balances"
               + (" WHERE account_id=?" if account else "")
               + " ORDER BY report_to DESC, created_at DESC LIMIT 1")
        with self.connect() as conn:
            r = conn.execute(sql, (account,) if account else ()).fetchone()
        return (r["report_from"], r["report_to"]) if r else (None, None)

    def upsert_conversion_rates(self, rows: list[dict], *, report_hash=None,
                                now_iso: str | None = None) -> int:
        if not rows:
            return 0
        now_iso = now_iso or _utcnow_iso()
        with self.connect() as conn:
            conn.executemany(
                "INSERT OR REPLACE INTO flex_conversion_rates"
                " (rate_id, rate_date, from_currency, to_currency, rate,"
                "  report_hash, source, created_at) VALUES (?,?,?,?,?,?,?,?)",
                [(r["rate_id"], r["rate_date"], r["from_currency"],
                  r["to_currency"], r["rate"], report_hash,
                  "FLEX_CONVERSION_RATE", now_iso) for r in rows])
        return len(rows)

    def conversion_rate(self, rate_date: str, frm: str, to: str = "USD"):
        """某日汇率。取**不晚于该日**的最近一条 —— 用未来的汇率折算过去的金额
        是一种隐蔽的未来数据泄漏。"""
        with self.connect() as conn:
            r = conn.execute(
                "SELECT rate FROM flex_conversion_rates WHERE from_currency=?"
                " AND to_currency=? AND rate_date<=? ORDER BY rate_date DESC LIMIT 1",
                (frm.upper(), to.upper(), str(rate_date)[:10])).fetchone()
        return r["rate"] if r else None

    def conversion_rate_count(self) -> int:
        with self.connect() as conn:
            return conn.execute(
                "SELECT COUNT(*) FROM flex_conversion_rates").fetchone()[0]

    def save_daily_unrealized(self, rows: list[dict]) -> int:
        if not rows:
            return 0
        cols = ("account_id, et_date, snapshot_id, snapshot_ts, unrealized_pnl,"
                " market_value, cost_basis, position_count, source_field,"
                " quality_status, calculation_version, created_at")
        names = [c.strip() for c in cols.split(",")]
        with self.connect() as conn:
            conn.executemany(
                f"INSERT OR REPLACE INTO daily_unrealized_pnl ({cols})"
                " VALUES (" + ",".join("?" * len(names)) + ")",
                [tuple(r.get(n) for n in names) for r in rows])
        return len(rows)

    def daily_unrealized(self, start: str, end: str, *, account=None,
                         version: str | None = None) -> list[dict]:
        sql = "SELECT * FROM daily_unrealized_pnl WHERE et_date>=? AND et_date<=?"
        args: list = [str(start)[:10], str(end)[:10]]
        if account:
            sql += " AND account_id=?"; args.append(account)
        if version:
            sql += " AND calculation_version=?"; args.append(version)
        sql += " ORDER BY et_date"
        with self.connect() as conn:
            return [dict(r) for r in conn.execute(sql, args).fetchall()]

    def save_position_attribution(self, rows: list[dict]) -> int:
        if not rows:
            return 0
        cols = ("account_id, et_date, snapshot_id, symbol, conid, quantity,"
                " market_value, cost_basis, unrealized_pnl, source_field,"
                " calculation_version, created_at")
        names = [c.strip() for c in cols.split(",")]
        with self.connect() as conn:
            conn.executemany(
                f"INSERT OR REPLACE INTO position_pnl_attribution ({cols})"
                " VALUES (" + ",".join("?" * len(names)) + ")",
                [tuple(r.get(n) for n in names) for r in rows])
        return len(rows)

    def position_attribution(self, et_date: str, *, account=None,
                             version: str | None = None) -> list[dict]:
        sql = "SELECT * FROM position_pnl_attribution WHERE et_date=?"
        args: list = [str(et_date)[:10]]
        if account:
            sql += " AND account_id=?"; args.append(account)
        if version:
            sql += " AND calculation_version=?"; args.append(version)
        with self.connect() as conn:
            return [dict(r) for r in conn.execute(sql, args).fetchall()]

    def eod_snapshots_between(self, start: str, end: str,
                              account: str | None = None) -> list[dict]:
        """每个交易日的**最后一份** fresh 快照。

        按 et_date 取 MAX(ts) —— 一天之内混用不同 snapshot_id 会让
        「未实现盈亏变化」里混进快照之间的持仓变动。
        """
        sql = ("SELECT b.* FROM balances_snapshot b JOIN ("
               "  SELECT substr(ts,1,10) d, MAX(ts) mts FROM balances_snapshot"
               "  WHERE ts>=? AND ts<=?"
               + (" AND account=?" if account else "")
               + "  GROUP BY d) x ON b.ts = x.mts"
               " ORDER BY b.ts")
        args = ([f"{str(start)[:10]}T00:00:00+00:00",
                 f"{str(end)[:10]}T23:59:59+00:00"]
                + ([account] if account else []))
        with self.connect() as conn:
            return [dict(r) for r in conn.execute(sql, args).fetchall()]

    def save_currency_reconciliation(self, r: dict) -> None:
        cols = ("recon_id, account_id, report_from, report_to, currency,"
                " opening_balance, closing_balance, deposits, withdrawals,"
                " net_external_cash_flow, trade_cash_flow, dividends, interest,"
                " commissions, other_fees, taxes, other_confirmed_items,"
                " expected_closing_balance, reconciliation_difference, tolerance,"
                " status, issues, fx_rate_to_base, difference_base,"
                " calculation_version, report_hash, created_at")
        names = [c.strip() for c in cols.split(",")]
        with self.connect() as conn:
            conn.execute(
                f"INSERT OR REPLACE INTO currency_reconciliations ({cols})"
                " VALUES (" + ",".join("?" * len(names)) + ")",
                tuple(r.get(n) for n in names))

    def currency_reconciliations(self, *, account=None, report_from=None,
                                 report_to=None, version=None) -> list[dict]:
        sql = "SELECT * FROM currency_reconciliations WHERE 1=1"
        args: list = []
        for col, val in (("account_id", account), ("report_from", report_from),
                         ("report_to", report_to),
                         ("calculation_version", version)):
            if val:
                sql += f" AND {col}=?"; args.append(val)
        sql += " ORDER BY currency"
        with self.connect() as conn:
            return [dict(r) for r in conn.execute(sql, args).fetchall()]

    def cash_flow_totals_by_currency(self, start: str, end: str, *,
                                     account: str | None = None) -> dict:
        """按 (币种, 分类) 的原币金额合计。

        **绝不跨币种相加** —— 把 HKD 和 USD 直接相加得到的数没有任何意义。
        """
        sql = ("SELECT currency, COALESCE(category,'external_cash_flow') c,"
               " COALESCE(subtype,'') s, SUM(CAST(amount AS REAL)) amt"
               " FROM cash_flows WHERE et_date>=? AND et_date<=?")
        args: list = [str(start)[:10], str(end)[:10]]
        if account:
            sql += " AND account=?"; args.append(account)
        sql += " GROUP BY currency, c, s"
        out: dict = {}
        with self.connect() as conn:
            for r in conn.execute(sql, args).fetchall():
                cur = out.setdefault(r["currency"] or "USD",
                                     {"by_category": {}, "by_subtype": {}})
                cur["by_category"][r["c"]] = str(
                    float(cur["by_category"].get(r["c"], 0)) + (r["amt"] or 0))
                if r["s"]:
                    cur["by_subtype"][r["s"]] = str(
                        float(cur["by_subtype"].get(r["s"], 0)) + (r["amt"] or 0))
        return out

    def latest_usable_flex_run(self) -> dict | None:
        """最近一次**可用**的 Flex 同步（success 或 partial）。

        partial 通常只是有几条流水未分类，报表区间与主体数据仍然有效。
        只认 success 会让一条未分类记录使整份报表的覆盖判定失效。
        """
        with self.connect() as conn:
            r = conn.execute(
                "SELECT * FROM flex_sync_runs WHERE status IN ('success','partial')"
                " AND report_from IS NOT NULL AND report_to IS NOT NULL"
                " ORDER BY completed_at DESC LIMIT 1").fetchone()
        return dict(r) if r else None

    # ================= 历史重放（只读原始表，独立写重放表） =================

    def positions_at_exact_ts(self, ts: str) -> list[dict]:
        """时间戳完全相同的持仓。无 snapshot_id 的历史数据靠它配对 ——
        实测 96% 的账户快照都能命中。"""
        with self.connect() as conn:
            return [dict(r) for r in conn.execute(
                "SELECT * FROM positions_snapshot WHERE ts=?", (ts,)).fetchall()]

    def positions_near_ts(self, ts: str, tolerance_seconds: int):
        """容差窗口内的持仓，按时间戳分组返回 [(ts, rows), ...]。"""
        from datetime import datetime, timedelta, timezone
        try:
            t = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
            if t.tzinfo is None:
                t = t.replace(tzinfo=timezone.utc)
        except (TypeError, ValueError):
            return []
        lo = (t - timedelta(seconds=tolerance_seconds)).isoformat()
        hi = (t + timedelta(seconds=1)).isoformat()   # 只允许极小的向后容差
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM positions_snapshot WHERE ts>=? AND ts<=? ORDER BY ts",
                (lo, hi)).fetchall()
        groups: dict[str, list[dict]] = {}
        for r in rows:
            groups.setdefault(r["ts"], []).append(dict(r))
        return list(groups.items())

    def balances_in_range(self, start: str, end: str,
                          account: str | None = None) -> list[dict]:
        sql = "SELECT * FROM balances_snapshot WHERE ts>=? AND ts<=?"
        args: list = [start, end]
        if account:
            sql += " AND account=?"
            args.append(account)
        sql += " ORDER BY ts"
        with self.connect() as conn:
            return [dict(r) for r in conn.execute(sql, args).fetchall()]

    def daily_close_balances(self, start: str, end: str,
                             account: str | None = None) -> list[dict]:
        """每个自然日的最后一条账户快照。"""
        sql = ("SELECT b.* FROM balances_snapshot b JOIN ("
               " SELECT substr(ts,1,10) d, MAX(ts) mts FROM balances_snapshot"
               " WHERE ts>=? AND ts<=?"
               + (" AND account=?" if account else "")
               + " GROUP BY d) x ON b.ts=x.mts ORDER BY b.ts")
        args = [start, end] + ([account] if account else [])
        with self.connect() as conn:
            return [dict(r) for r in conn.execute(sql, args).fetchall()]

    def replay_snapshot_stats(self, account: str | None = None) -> dict:
        """重放覆盖率所需的原始数据统计。"""
        with self.connect() as conn:
            def one(sql, args=()):
                return conn.execute(sql, args).fetchone()
            total = one("SELECT COUNT(*) FROM balances_snapshot")[0]
            native = one("SELECT COUNT(*) FROM balances_snapshot"
                         " WHERE snapshot_id IS NOT NULL AND snapshot_id<>''")[0]
            rng = one("SELECT MIN(ts), MAX(ts) FROM balances_snapshot")
            nrng = one("SELECT MIN(ts), MAX(ts) FROM balances_snapshot"
                       " WHERE snapshot_id IS NOT NULL AND snapshot_id<>''")
            days = one("SELECT COUNT(DISTINCT substr(ts,1,10))"
                       " FROM balances_snapshot")[0]
            paired = one(
                "SELECT COUNT(*) FROM balances_snapshot b WHERE EXISTS"
                " (SELECT 1 FROM positions_snapshot p WHERE p.ts=b.ts)")[0]
        return {
            "balances_total": total, "native_snapshot_id": native,
            "reconstructable": paired,
            "unpairable": total - paired,
            "snapshot_range": {"start": rng[0], "end": rng[1]},
            "native_range": {"start": nrng[0], "end": nrng[1]},
            "distinct_days": days,
        }

    # ---------- 重放任务 ----------

    def find_replay_by_hash(self, param_hash: str) -> dict | None:
        with self.connect() as conn:
            r = conn.execute(
                "SELECT * FROM risk_replay_runs WHERE param_hash=?"
                " AND status IN ('success','partial')"
                " ORDER BY completed_at DESC LIMIT 1", (param_hash,)).fetchone()
        return dict(r) if r else None

    def create_replay_run(self, r: dict) -> None:
        cols = ("replay_run_id, param_hash, account_id, date_start, date_end,"
                " sampling_mode, sampling_interval_minutes, requested_rules,"
                " rule_set_mode, status, started_at, calculation_version,"
                " created_at")
        names = [c.strip() for c in cols.split(",")]
        with self.connect() as conn:
            conn.execute(
                f"INSERT OR REPLACE INTO risk_replay_runs ({cols})"
                " VALUES (" + ",".join("?" * len(names)) + ")",
                tuple(r.get(n) for n in names))

    def finish_replay_run(self, run_id: str, **fields) -> None:
        if not fields:
            return
        fields.setdefault("completed_at", _utcnow_iso())
        sets = ", ".join(f"{k}=?" for k in fields)
        with self.connect() as conn:
            conn.execute(f"UPDATE risk_replay_runs SET {sets}"
                         " WHERE replay_run_id=?", (*fields.values(), run_id))

    def get_replay_run(self, run_id: str) -> dict | None:
        with self.connect() as conn:
            r = conn.execute("SELECT * FROM risk_replay_runs WHERE replay_run_id=?",
                             (run_id,)).fetchone()
        return dict(r) if r else None

    def replay_runs(self, limit: int = 20) -> list[dict]:
        with self.connect() as conn:
            return [dict(r) for r in conn.execute(
                "SELECT * FROM risk_replay_runs ORDER BY created_at DESC LIMIT ?",
                (int(limit),)).fetchall()]

    def save_replay_assessments(self, rows: list[dict]) -> int:
        if not rows:
            return 0
        cols = ("replay_assessment_id, replay_run_id, account_id, data_as_of,"
                " et_date, snapshot_id, source_snapshot_key, source_identity_type,"
                " alignment_status, alignment_gap_seconds, positions_data_as_of,"
                " risk_level, risk_score, reliable, component_scores,"
                " triggered_rules, market_data_as_of, market_state, market_source,"
                " market_time_gap_seconds, rule_set_version, rule_set_mode,"
                " data_quality, created_at")
        names = [c.strip() for c in cols.split(",")]
        with self.connect() as conn:
            conn.executemany(
                f"INSERT OR REPLACE INTO risk_replay_assessments ({cols})"
                " VALUES (" + ",".join("?" * len(names)) + ")",
                [tuple(r.get(n) for n in names) for r in rows])
        return len(rows)

    def replay_assessments(self, run_id: str, *, limit: int = 500,
                           offset: int = 0) -> tuple[list[dict], int]:
        with self.connect() as conn:
            total = conn.execute(
                "SELECT COUNT(*) FROM risk_replay_assessments WHERE replay_run_id=?",
                (run_id,)).fetchone()[0]
            rows = conn.execute(
                "SELECT * FROM risk_replay_assessments WHERE replay_run_id=?"
                " ORDER BY data_as_of LIMIT ? OFFSET ?",
                (run_id, int(limit), int(offset))).fetchall()
        return [dict(r) for r in rows], int(total)

    def save_replay_events(self, rows: list[dict]) -> int:
        if not rows:
            return 0
        cols = ("replay_event_id, replay_run_id, identity, episode_seq, rule_code,"
                " rule_version, rule_set_mode, source_type, category,"
                " affected_symbol, account_id, title, explanation,"
                " first_detected_at, last_detected_at, resolved_at, peak_severity,"
                " last_severity, peak_actual_value, threshold_value, unit,"
                " occurrence_count, duration_minutes, source_snapshot_keys,"
                " reliable, matched_real_event_id, match_lead_minutes, created_at")
        names = [c.strip() for c in cols.split(",")]
        with self.connect() as conn:
            conn.executemany(
                f"INSERT OR REPLACE INTO risk_replay_events ({cols})"
                " VALUES (" + ",".join("?" * len(names)) + ")",
                [tuple(r.get(n) for n in names) for r in rows])
        return len(rows)

    def replay_events(self, run_id: str, *, rule_code: str | None = None,
                      limit: int = 500, offset: int = 0) -> tuple[list[dict], int]:
        where, args = ["replay_run_id=?"], [run_id]
        if rule_code:
            where.append("rule_code=?"); args.append(rule_code)
        clause = " WHERE " + " AND ".join(where)
        with self.connect() as conn:
            total = conn.execute(
                f"SELECT COUNT(*) FROM risk_replay_events{clause}", args).fetchone()[0]
            rows = conn.execute(
                f"SELECT * FROM risk_replay_events{clause}"
                " ORDER BY first_detected_at DESC LIMIT ? OFFSET ?",
                (*args, int(limit), int(offset))).fetchall()
        return [dict(r) for r in rows], int(total)

    def save_replay_rule_results(self, rows: list[dict]) -> int:
        if not rows:
            return 0
        cols = ("replay_run_id, replay_assessment_id, source_snapshot_key,"
                " data_as_of, rule_code, triggered, severity, actual_value,"
                " threshold_value, affected_symbol, rule_version, created_at")
        names = [c.strip() for c in cols.split(",")]
        with self.connect() as conn:
            conn.executemany(
                f"INSERT INTO risk_replay_rule_results ({cols})"
                " VALUES (" + ",".join("?" * len(names)) + ")",
                [tuple(r.get(n) for n in names) for r in rows])
        return len(rows)

    def replay_rule_summary(self, run_id: str) -> list[dict]:
        with self.connect() as conn:
            return [dict(r) for r in conn.execute(
                "SELECT rule_code, COUNT(*) evaluations,"
                " SUM(triggered) triggers FROM risk_replay_rule_results"
                " WHERE replay_run_id=? GROUP BY rule_code"
                " ORDER BY triggers DESC", (run_id,)).fetchall()]

    def rules_version_at(self, ts_iso: str) -> str | None:
        """该时点生效的规则集版本。point_in_time 模式用它。"""
        with self.connect() as conn:
            r = conn.execute(
                "SELECT version FROM risk_rules WHERE effective_from<=?"
                " AND (effective_to IS NULL OR effective_to>?)"
                " ORDER BY effective_from DESC LIMIT 1",
                (ts_iso, ts_iso)).fetchone()
        return r["version"] if r else None

    def unattributed_snapshot_stats(self) -> dict:
        """account 字段为空的快照统计。

        该列是后来才加的，早期快照没有写。它们**几乎肯定**属于配置的那个账户
        （本系统只同步单账户），但「几乎肯定」不足以静默纳入 ——
        如实报出来，由使用者显式决定。
        """
        with self.connect() as conn:
            r = conn.execute(
                "SELECT COUNT(*) n, MIN(ts) f, MAX(ts) t,"
                " COUNT(DISTINCT substr(ts,1,10)) d FROM balances_snapshot"
                " WHERE account IS NULL OR account=''").fetchone()
        return {"count": r["n"], "start": r["f"], "end": r["t"],
                "distinct_days": r["d"]}

    # ==================== AI 每日风险简报 ====================

    def save_brief_run(self, p: dict) -> None:
        with self.connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO daily_brief_runs (run_id, brief_date,"
                " trigger, status, generation_mode, generation_status, brief_id,"
                " reused, wait_attempts, readiness, error, duration_ms,"
                " started_at, finished_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (p["run_id"], p["brief_date"], p["trigger"], p["status"],
                 p.get("generation_mode"), p.get("generation_status"),
                 p.get("brief_id"), int(bool(p.get("reused"))),
                 int(p.get("wait_attempts") or 0),
                 _json.dumps(p.get("readiness") or {}, ensure_ascii=False,
                             default=str),
                 p.get("error"), p.get("duration_ms"),
                 p["started_at"], p.get("finished_at")))

    def brief_runs(self, *, limit: int = 30) -> list[dict]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM daily_brief_runs ORDER BY started_at DESC LIMIT ?",
                (limit,)).fetchall()
        return [dict(r) for r in rows]

    def save_brief(self, p: dict) -> None:
        with self.connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO daily_briefs (brief_id, brief_date,"
                " account_id_masked, revision, generation_mode, generation_status,"
                " content_json, rendered_markdown, prompt_version,"
                " model_identifier, context_hash, context_version,"
                " validation_status, validation_errors, ai_attempts,"
                " account_snapshot_id, risk_assessment_id, market_assessment_id,"
                " performance_period_id, calculation_versions, generated_at,"
                " delivered_at, created_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (p["brief_id"], p["brief_date"], p.get("account_id_masked"),
                 int(p.get("revision") or 1), p["generation_mode"],
                 p["generation_status"],
                 _json.dumps(p.get("content") or {}, ensure_ascii=False,
                             default=str),
                 p.get("rendered_markdown"), p.get("prompt_version"),
                 p.get("model_identifier"), p["context_hash"],
                 p.get("context_version"), p["validation_status"],
                 _json.dumps(p.get("validation_errors") or [],
                             ensure_ascii=False, default=str),
                 int(p.get("ai_attempts") or 0),
                 p.get("account_snapshot_id"), p.get("risk_assessment_id"),
                 p.get("market_assessment_id"), p.get("performance_period_id"),
                 _json.dumps(p.get("calculation_versions") or {},
                             ensure_ascii=False, default=str),
                 p["generated_at"], p.get("delivered_at"), p["created_at"]))

    def find_brief(self, *, brief_date: str, account_id_masked: str | None,
                   context_hash: str, prompt_version: str | None) -> dict | None:
        """幂等查找：日期 + 账户 + context_hash + prompt 版本全同才算同一份。"""
        with self.connect() as conn:
            r = conn.execute(
                "SELECT * FROM daily_briefs WHERE brief_date=?"
                " AND IFNULL(account_id_masked,'')=IFNULL(?,'')"
                " AND context_hash=? AND IFNULL(prompt_version,'')=IFNULL(?,'')",
                (brief_date, account_id_masked, context_hash,
                 prompt_version)).fetchone()
        return dict(r) if r else None

    def max_brief_revision(self, brief_date: str,
                           account_id_masked: str | None) -> int:
        with self.connect() as conn:
            r = conn.execute(
                "SELECT MAX(revision) m FROM daily_briefs WHERE brief_date=?"
                " AND IFNULL(account_id_masked,'')=IFNULL(?,'')",
                (brief_date, account_id_masked)).fetchone()
        return int(r["m"] or 0)

    def get_brief(self, brief_id: str) -> dict | None:
        with self.connect() as conn:
            r = conn.execute("SELECT * FROM daily_briefs WHERE brief_id=?",
                             (brief_id,)).fetchone()
        return dict(r) if r else None

    def latest_brief(self, *, brief_date: str | None = None) -> dict | None:
        sql = "SELECT * FROM daily_briefs"
        args: list = []
        if brief_date:
            sql += " WHERE brief_date=?"
            args.append(brief_date)
        sql += " ORDER BY brief_date DESC, revision DESC, created_at DESC LIMIT 1"
        with self.connect() as conn:
            r = conn.execute(sql, tuple(args)).fetchone()
        return dict(r) if r else None

    def query_briefs(self, *, start=None, end=None, generation_mode=None,
                     generation_status=None, delivered=None,
                     limit: int = 30, offset: int = 0) -> tuple[list[dict], int]:
        where, args = [], []
        if start:
            where.append("brief_date>=?"); args.append(str(start)[:10])
        if end:
            where.append("brief_date<=?"); args.append(str(end)[:10])
        for col, val in (("generation_mode", generation_mode),
                         ("generation_status", generation_status)):
            if val:
                vals = val if isinstance(val, (list, tuple)) else [val]
                where.append(f"{col} IN ({','.join('?' * len(vals))})")
                args.extend(vals)
        if delivered is not None:
            where.append("delivered_at IS NOT NULL" if delivered
                         else "delivered_at IS NULL")
        clause = (" WHERE " + " AND ".join(where)) if where else ""
        with self.connect() as conn:
            total = conn.execute(
                f"SELECT COUNT(*) c FROM daily_briefs{clause}",
                tuple(args)).fetchone()["c"]
            rows = conn.execute(
                f"SELECT * FROM daily_briefs{clause}"
                " ORDER BY brief_date DESC, revision DESC LIMIT ? OFFSET ?",
                tuple(args) + (limit, offset)).fetchall()
        return [dict(r) for r in rows], int(total)

    def save_brief_sources(self, brief_id: str, refs: list[dict],
                           created_at: str) -> None:
        if not refs:
            return
        with self.connect() as conn:
            conn.execute("DELETE FROM daily_brief_sources WHERE brief_id=?",
                         (brief_id,))
            conn.executemany(
                "INSERT INTO daily_brief_sources (brief_id, source_type,"
                " source_table, source_id, source_version, data_as_of,"
                " field_path, value_hash, created_at)"
                " VALUES (?,?,?,?,?,?,?,?,?)",
                [(brief_id, r.get("source_type"), r.get("source_table"),
                  r.get("source_id"), r.get("source_version"),
                  r.get("data_as_of"), r.get("field_path"),
                  r.get("value_hash"), created_at) for r in refs])

    def brief_sources(self, brief_id: str) -> list[dict]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM daily_brief_sources WHERE brief_id=?"
                " ORDER BY source_table, field_path", (brief_id,)).fetchall()
        return [dict(r) for r in rows]

    def record_brief_delivery(self, p: dict) -> None:
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO daily_brief_deliveries (brief_id, channel, status,"
                " reason, context_hash, attempt, created_at)"
                " VALUES (?,?,?,?,?,?,?)",
                (p["brief_id"], p["channel"], p["status"], p.get("reason"),
                 p.get("context_hash"), int(p.get("attempt") or 1),
                 p["created_at"]))

    def brief_delivered(self, *, context_hash: str, channel: str = "feishu") -> bool:
        """同一 context_hash 是否已在**该渠道**成功推送过。

        去重键含 channel —— 测试渠道（feishu_test）的 test_success 不会
        让正式渠道判定「已发过」。状态也只认 'sent'，test_success 不算数。
        """
        with self.connect() as conn:
            r = conn.execute(
                "SELECT 1 FROM daily_brief_deliveries WHERE context_hash=?"
                " AND channel=? AND status='sent' LIMIT 1",
                (context_hash, channel)).fetchone()
        return r is not None

    def brief_deliveries(self, brief_id: str) -> list[dict]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM daily_brief_deliveries WHERE brief_id=?"
                " ORDER BY created_at DESC", (brief_id,)).fetchall()
        return [dict(r) for r in rows]

    def mark_brief_delivered(self, brief_id: str, at: str) -> None:
        with self.connect() as conn:
            conn.execute("UPDATE daily_briefs SET delivered_at=? WHERE brief_id=?",
                         (at, brief_id))

    # ---------------- 调度执行审计 ----------------

    def save_schedule_execution(self, p: dict) -> None:
        with self.connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO brief_schedule_executions (execution_id,"
                " brief_date, scheduled_for, actual_started_at,"
                " dependency_wait_count, dependency_ready_status,"
                " dependency_detail, brief_id, generation_mode,"
                " generation_status, validation_status, delivery_status,"
                " reused, completed_at, error_message, created_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (p["execution_id"], p["brief_date"], p["scheduled_for"],
                 p["actual_started_at"], int(p.get("dependency_wait_count") or 0),
                 p.get("dependency_ready_status"),
                 _json.dumps(p.get("dependency_detail") or {},
                             ensure_ascii=False, default=str),
                 p.get("brief_id"), p.get("generation_mode"),
                 p.get("generation_status"), p.get("validation_status"),
                 p.get("delivery_status"), int(bool(p.get("reused"))),
                 p.get("completed_at"), p.get("error_message"),
                 p["created_at"]))

    def schedule_executions(self, *, limit: int = 20) -> list[dict]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM brief_schedule_executions"
                " ORDER BY scheduled_for DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

    def open_schedule_execution(self, brief_date: str) -> dict | None:
        """当天尚未完成的调度执行 —— 等待重试时接着写同一条记录。"""
        with self.connect() as conn:
            r = conn.execute(
                "SELECT * FROM brief_schedule_executions WHERE brief_date=?"
                " AND completed_at IS NULL ORDER BY scheduled_for DESC LIMIT 1",
                (brief_date,)).fetchone()
        return dict(r) if r else None

    # ---------------- 事件标注（追加式，不覆盖原始记录）----------------

    def add_event_annotation(self, p: dict) -> bool:
        """追加一条标注。幂等：同事件 + 同 transition + 同事故只记一条。

        返回是否**新增**（已存在返回 False）。
        """
        with self.connect() as conn:
            cur = conn.execute(
                "INSERT OR IGNORE INTO risk_event_annotations (annotation_id,"
                " risk_event_id, transition_id, episode_sequence,"
                " annotation_type, origin, reason_code, reason_text,"
                " excluded_from_research, excluded_from_precision_metrics,"
                " related_notification_id, incident_reference, evidence,"
                " created_by, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (p["annotation_id"], p["risk_event_id"], p.get("transition_id"),
                 p.get("episode_sequence"), p["annotation_type"], p["origin"],
                 p["reason_code"], p["reason_text"],
                 int(bool(p.get("excluded_from_research"))),
                 int(bool(p.get("excluded_from_precision_metrics"))),
                 p.get("related_notification_id"), p.get("incident_reference"),
                 _json.dumps(p.get("evidence") or {}, ensure_ascii=False,
                             default=str),
                 p["created_by"], p["created_at"]))
            return cur.rowcount > 0

    def event_annotations(self, risk_event_id: str | None = None, *,
                          incident_reference: str | None = None) -> list[dict]:
        where, args = [], []
        if risk_event_id:
            where.append("risk_event_id=?"); args.append(risk_event_id)
        if incident_reference:
            where.append("incident_reference=?"); args.append(incident_reference)
        clause = (" WHERE " + " AND ".join(where)) if where else ""
        with self.connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM risk_event_annotations{clause}"
                " ORDER BY created_at", tuple(args)).fetchall()
        return [dict(r) for r in rows]

    def excluded_event_ids(self, *, metric: str = "research") -> set[str]:
        """默认排除出研究样本的事件 id。"""
        col = ("excluded_from_precision_metrics" if metric == "precision"
               else "excluded_from_research")
        with self.connect() as conn:
            rows = conn.execute(
                f"SELECT DISTINCT risk_event_id FROM risk_event_annotations"
                f" WHERE {col}=1").fetchall()
        return {r["risk_event_id"] for r in rows}

    def annotations_by_event(self) -> dict[str, list[dict]]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM risk_event_annotations ORDER BY created_at"
            ).fetchall()
        out: dict[str, list[dict]] = {}
        for r in rows:
            out.setdefault(r["risk_event_id"], []).append(dict(r))
        return out

    def add_notification_classification(self, p: dict) -> bool:
        """给通知加运营分类。**不改 success** —— 那条通知技术上确实发出去了。"""
        with self.connect() as conn:
            cur = conn.execute(
                "INSERT OR IGNORE INTO notification_classifications"
                " (classification_id, notification_id, classification,"
                " reason_text, incident_reference, created_by, created_at)"
                " VALUES (?,?,?,?,?,?,?)",
                (p["classification_id"], p["notification_id"],
                 p["classification"], p.get("reason_text"),
                 p.get("incident_reference"), p["created_by"], p["created_at"]))
            return cur.rowcount > 0

    def notification_classifications(self, notification_id: str | None = None
                                     ) -> list[dict]:
        sql = "SELECT * FROM notification_classifications"
        args: tuple = ()
        if notification_id:
            sql += " WHERE notification_id=?"
            args = (notification_id,)
        with self.connect() as conn:
            return [dict(r) for r in conn.execute(sql + " ORDER BY created_at",
                                                  args).fetchall()]

    def transitions_in_window(self, start: str, end: str,
                              transition: str | None = None) -> list[dict]:
        where = ["transitioned_at>=?", "transitioned_at<=?"]
        args = [start, end]
        if transition:
            where.append("transition=?"); args.append(transition)
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM risk_event_transitions WHERE "
                + " AND ".join(where) + " ORDER BY transitioned_at",
                tuple(args)).fetchall()
        return [dict(r) for r in rows]

    # ---------------- 副作用执行审计 ----------------

    def start_side_effect_execution(self, p: dict) -> None:
        with self.connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO side_effect_executions (execution_id,"
                " source, correlation_id, initiated_by, started_at)"
                " VALUES (?,?,?,?,?)",
                (p["execution_id"], p["source"], p.get("correlation_id"),
                 p.get("initiated_by"), p["started_at"]))

    def finish_side_effect_execution(self, execution_id: str, *,
                                     finished_at: str,
                                     affected_event_count: int = 0,
                                     notification_count: int = 0,
                                     error: str | None = None) -> None:
        with self.connect() as conn:
            conn.execute(
                "UPDATE side_effect_executions SET finished_at=?,"
                " affected_event_count=?, notification_count=?, error=?"
                " WHERE execution_id=?",
                (finished_at, int(affected_event_count),
                 int(notification_count), error, execution_id))

    def side_effect_executions(self, *, limit: int = 20) -> list[dict]:
        with self.connect() as conn:
            return [dict(r) for r in conn.execute(
                "SELECT * FROM side_effect_executions"
                " ORDER BY started_at DESC LIMIT ?", (limit,)).fetchall()]
