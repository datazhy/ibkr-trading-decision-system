-- 小鱼交易台 — SQLite 表结构
-- 金额一律用 TEXT 存 Decimal 字符串，读出时转回 Decimal，避免浮点误差。
-- 时间戳 ts 一律 UTC ISO8601（如 2026-07-10T13:30:00+00:00）。

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

-- 同步批次：一次「账户+持仓+行情」同步共用一个 sync_run_id。
-- 存在的意义是让页面与规则引擎能证明自己读的是同一时点的数据，
-- 而不是把不同时刻的快照拼在一起冒充同一时点。
CREATE TABLE IF NOT EXISTS sync_runs (
    sync_run_id TEXT NOT NULL PRIMARY KEY,
    started_at  TEXT NOT NULL,      -- UTC ISO
    finished_at TEXT,               -- UTC ISO
    account     TEXT,
    status      TEXT NOT NULL,      -- READY/WARMING_UP/DEGRADED/ERROR
    source      TEXT NOT NULL,      -- IBKR
    note        TEXT
);
CREATE INDEX IF NOT EXISTS idx_sync_runs_started ON sync_runs (started_at);

-- 持仓快照（每 60s 一批）
CREATE TABLE IF NOT EXISTS positions_snapshot (
    ts           TEXT NOT NULL,
    symbol       TEXT NOT NULL,
    qty          TEXT NOT NULL,   -- Decimal（可为负=空头）
    avg_cost     TEXT NOT NULL,   -- Decimal
    market_value TEXT NOT NULL,   -- Decimal
    upl          TEXT NOT NULL,   -- Decimal 未实现盈亏
    price         TEXT,           -- Decimal 行情价；**禁止**用 market_value/qty 回退
    price_source  TEXT,           -- LAST / BID_ASK_MID / CLOSE / NONE
    price_quality TEXT,           -- REALTIME/REALTIME_NONCONSOLIDATED/DELAYED/
                                  -- DELAYED_FROZEN/FROZEN/STALE/MISSING/INVALID
    market_data_type INTEGER,     -- IBKR marketDataType：1实时 2冻结 3延迟 4延迟冻结
    quote_as_of   TEXT,           -- 行情时间戳 UTC ISO；CLOSE 回退时为 NULL（收盘价无可信时点）
    source        TEXT,           -- IBKR_reqMktData 等
    sync_run_id   TEXT,           -- 所属同步批次
    conid         INTEGER,        -- IBKR 合约 ID（reqPnLSingle 用）
    day_pnl       TEXT,           -- Decimal 该持仓当日盈亏（IBKR reqPnLSingle）
    day_pnl_source TEXT,          -- IBKR_reqPnLSingle
    realized_pnl  TEXT,           -- Decimal 该持仓已实现盈亏（IBKR reqPnLSingle）
    realized_pnl_source TEXT,     -- IBKR_reqPnLSingle
    previous_close TEXT,          -- 富途昨收；今日盯市盈亏基准
    market_value_source TEXT,     -- IBKR_updatePortfolio / IBKR_portfolio
    upl_source TEXT,              -- IBKR_updatePortfolio / IBKR_portfolio
    futu_market_value TEXT,       -- 富途价格 × IBKR 数量，仅 shadow
    futu_unrealized_estimate TEXT,-- 富途价格 × IBKR 成本，仅 shadow
    futu_intraday_mtm TEXT,       -- 富途前收盯市 + IBKR 成交，仅 shadow
    futu_quote_age_seconds TEXT,  -- shadow 行情年龄
    PRIMARY KEY (ts, symbol)
);
CREATE INDEX IF NOT EXISTS idx_positions_symbol_ts ON positions_snapshot (symbol, ts);
-- 注：sync_run_id 相关索引在 db._migrate() 补列之后创建（老库此处尚无该列）

-- 账户余额快照（每 60s 一条）
CREATE TABLE IF NOT EXISTS balances_snapshot (
    ts             TEXT NOT NULL PRIMARY KEY,
    equity         TEXT NOT NULL,  -- 净资产（= IBKR NetLiquidation）
    cash           TEXT NOT NULL,
    margin_balance TEXT NOT NULL,  -- <0 表示动用融资
    buying_power   TEXT NOT NULL,
    account        TEXT,           -- 账户号（当前单账户；多账户见 pnl_snapshot/daily_equity 主键）
    source         TEXT,           -- IBKR_accountValues 等
    data_quality   TEXT,           -- READY/WARMING_UP/DEGRADED
    sync_run_id    TEXT,
    positions_as_of TEXT,          -- IBKR 最近持仓/portfolio 事件 UTC
    account_as_of   TEXT,          -- IBKR 最近账户余额事件 UTC
    pnl_as_of       TEXT,          -- IBKR 最近 reqPnL/reqPnLSingle 事件 UTC
    orders_as_of    TEXT,          -- IBKR 最近订单事件 UTC
    executions_as_of TEXT,         -- IBKR 最近成交/佣金事件 UTC
    reconciled_at   TEXT,          -- 最近一次 60 秒主动校准完成 UTC
    positions_value_source TEXT    -- FUTU_price_x_IBKR_qty / IBKR_accountSummary
);

-- 订单（含状态），按 order_id 增量 upsert
CREATE TABLE IF NOT EXISTS orders (
    order_id   TEXT NOT NULL PRIMARY KEY,
    ts         TEXT NOT NULL,      -- 下单/进入时间 UTC
    symbol     TEXT,
    side       TEXT,               -- BUY / SELL / BUY_TO_COVER / SELL_SHORT ...
    order_type TEXT,               -- MARKET / LIMIT / STOP ...
    qty        TEXT,               -- Decimal
    price      TEXT,               -- Decimal，可空（市价单）
    status     TEXT NOT NULL,      -- WORKING / FILLED / CANCELED ...
    raw_json   TEXT NOT NULL,
    updated_at TEXT NOT NULL       -- 本地写入时间 UTC
);
CREATE INDEX IF NOT EXISTS idx_orders_symbol_ts ON orders (symbol, ts);
CREATE INDEX IF NOT EXISTS idx_orders_status ON orders (status);

-- 成交流水，按 txn_id 增量 upsert
CREATE TABLE IF NOT EXISTS transactions (
    txn_id   TEXT NOT NULL PRIMARY KEY,
    ts       TEXT NOT NULL,        -- 成交时间 UTC
    symbol   TEXT,
    action   TEXT,                 -- BUY / SELL / DIVIDEND / FEE ...
    qty      TEXT,                 -- Decimal
    price    TEXT,                 -- Decimal
    fees     TEXT,                 -- Decimal
    amount   TEXT,                 -- Decimal 净额（含方向）
    realized TEXT,                 -- Decimal 精确已实现盈亏（IBKR 提供；无则 NULL→引擎估算）
    raw_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_txn_symbol_ts ON transactions (symbol, ts);

-- 日线（Market Data API），按 (symbol, date) 唯一
CREATE TABLE IF NOT EXISTS daily_bars (
    symbol TEXT NOT NULL,
    date   TEXT NOT NULL,          -- YYYY-MM-DD（交易日）
    o      TEXT NOT NULL,
    h      TEXT NOT NULL,
    l      TEXT NOT NULL,
    c      TEXT NOT NULL,
    v      INTEGER NOT NULL,
    source TEXT,                   -- IBKR_historical / legacy / ...
    adjustment_mode TEXT,          -- 历史价格复权口径
    bar_as_of TEXT,                -- 该根日线代表的交易日
    fetched_at TEXT,               -- 实际抓取时间 UTC
    corporate_action_provenance TEXT, -- 公司行动/拆股处理说明
    PRIMARY KEY (symbol, date)
);

-- 加密的应用密文（账户号等）
CREATE TABLE IF NOT EXISTS app_secrets (
    key             TEXT NOT NULL PRIMARY KEY,
    value_encrypted TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);

-- 同步状态：记录各任务上次成功同步的游标/时间
CREATE TABLE IF NOT EXISTS sync_state (
    key        TEXT NOT NULL PRIMARY KEY,  -- 如 orders_last_ts / transactions_last_ts
    value      TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

-- 告警去重：记录某告警键最近一次推送时间（token 提醒、同步失败等）
CREATE TABLE IF NOT EXISTS alert_log (
    alert_key  TEXT NOT NULL,
    ts         TEXT NOT NULL,      -- 推送时间 UTC
    message    TEXT NOT NULL,
    PRIMARY KEY (alert_key, ts)
);
CREATE INDEX IF NOT EXISTS idx_alert_key_ts ON alert_log (alert_key, ts);

-- ===== Phase 2 =====

-- 建议止损位（止损引擎每日收盘后输出，R7 与盘前报告用）
CREATE TABLE IF NOT EXISTS stop_levels (
    symbol         TEXT NOT NULL,
    computed_date  TEXT NOT NULL,   -- YYYY-MM-DD（ET 交易日）
    side           TEXT NOT NULL,   -- LONG / SHORT
    close          TEXT NOT NULL,
    atr            TEXT NOT NULL,
    swing          TEXT NOT NULL,
    stop_price     TEXT NOT NULL,
    risk_amount    TEXT,
    risk_pct       TEXT,
    exceeds_2pct   INTEGER NOT NULL DEFAULT 0,
    suggested_trim INTEGER NOT NULL DEFAULT 0,
    reached_1r     INTEGER NOT NULL DEFAULT 0,
    trailing_stop  TEXT,
    take_profit    TEXT,
    atr_period     INTEGER,          -- 实际使用的 ATR 周期（新股日线不足时 < 14）
    bars_used      INTEGER,          -- 参与计算的日线根数
    data_sufficient INTEGER NOT NULL DEFAULT 1,  -- 0 = 日线不足，仅供参考
    data_note      TEXT,             -- 数据不足说明；充足时为空
    updated_at     TEXT NOT NULL,
    PRIMARY KEY (symbol, computed_date)
);

-- 冷静期状态（R6 触发 48h）
CREATE TABLE IF NOT EXISTS cooldown_state (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at  TEXT NOT NULL,
    ends_at     TEXT NOT NULL,
    reason      TEXT,
    trigger_ref TEXT
);
CREATE INDEX IF NOT EXISTS idx_cooldown_ends ON cooldown_state (ends_at);

-- 规则触发日志（Phase 3 页面3展示；每次实际推送记一条）
CREATE TABLE IF NOT EXISTS rule_events (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    ts       TEXT NOT NULL,
    rule_id  TEXT NOT NULL,
    severity TEXT,
    symbol   TEXT,
    title    TEXT,
    message  TEXT
);
CREATE INDEX IF NOT EXISTS idx_rule_events_ts ON rule_events (ts);

-- 规则运行审计（每次评估都记一条，含未触发；纯追加，禁止 REPLACE 覆盖历史）
CREATE TABLE IF NOT EXISTS rule_runs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    run_ts          TEXT NOT NULL,        -- 运行时刻 UTC ISO
    run_status      TEXT NOT NULL,        -- OK/TRIGGERED/INSUFFICIENT_DATA/STALE_DATA/ERROR
    snapshot_as_of  TEXT,                 -- 所用持仓/余额快照的 ts
    account_ready   INTEGER NOT NULL DEFAULT 1,
    data_sources    TEXT,                 -- JSON：各数据源水位（positions/balances/orders/quotes ts）
    evaluations     TEXT NOT NULL,        -- JSON：每条规则 status/detail/命中数
    rules_version   TEXT,
    thresholds_version TEXT,
    error           TEXT,
    sync_run_id            TEXT,          -- 本次评估所用数据的同步批次（与首页同批即可互证）
    positions_snapshot_id  TEXT,          -- = positions_snapshot.ts
    balances_snapshot_id   TEXT,          -- = balances_snapshot.ts
    quotes_as_of           TEXT,          -- 该批次行情的最早有效时点；全 CLOSE 回退时为 NULL
    quote_quality_summary  TEXT,          -- JSON：{质量: 标的数}，供页面与规则页互相印证
    rule_summary           TEXT,          -- JSON：{triggered, ok, not_evaluated, error, skipped}
    per_symbol             TEXT           -- JSON：R7 逐标的 {symbol, status, reason}
);
CREATE INDEX IF NOT EXISTS idx_rule_runs_ts ON rule_runs (run_ts);

-- ===== 第二阶段 2A：盘中 executions 增量同步 =====
-- 来源 reqExecutions（仅当日增量，非历史源）+ CommissionReport（可分两阶段到达）。
-- 幂等键 exec_id；correction 由 correction_root/seq/is_current 处理；
-- R6 水位用 r6_state（不用时间戳，天然容忍同时间多笔与迟到）。
CREATE TABLE IF NOT EXISTS executions (
    exec_id             TEXT NOT NULL PRIMARY KEY,   -- execution.execId（幂等键）
    account             TEXT,                        -- 账户号；旧数据可能为空
    correction_root     TEXT NOT NULL,               -- execId 去尾段（修正关联）
    correction_seq      INTEGER NOT NULL DEFAULT 0,  -- execId 尾段（修正序号）
    is_current          INTEGER NOT NULL DEFAULT 1,  -- 同 root 内最新修正=1，旧的=0
    perm_id             INTEGER,
    order_id            INTEGER,
    client_id           INTEGER,
    conid               INTEGER,
    symbol              TEXT,
    sec_type            TEXT,
    exchange            TEXT,
    currency            TEXT,
    side                TEXT,                        -- BUY/SELL（BOT/SLD 归一）
    shares              TEXT,                        -- Decimal
    price               TEXT,                        -- Decimal
    cumulative_qty      TEXT,                        -- Decimal（部分成交累计）
    average_price       TEXT,                        -- Decimal
    execution_time      TEXT NOT NULL,               -- UTC ISO
    received_at         TEXT NOT NULL,               -- 本地落库时间 UTC ISO
    commission          TEXT,                        -- Decimal，未到为 NULL
    commission_currency TEXT,
    realized_pnl        TEXT,                        -- Decimal 精确已实现盈亏，未到为 NULL
    liquidation         INTEGER,                     -- 强平标记（主动/被动区分用）
    order_ref           TEXT,
    last_liquidity      INTEGER,
    decision_id         TEXT,                        -- 主动交易决策 ID；当前恒 NULL，
                                                     -- decision_key 退化用 perm_id
    source              TEXT NOT NULL DEFAULT 'reqExecutions',
    data_status         TEXT NOT NULL DEFAULT 'PENDING_COMMISSION',  -- PENDING_COMMISSION/READY
    r6_state            TEXT NOT NULL DEFAULT 'PENDING',             -- PENDING/DONE
    raw_json            TEXT
);
CREATE INDEX IF NOT EXISTS idx_exec_root ON executions (correction_root, correction_seq);
CREATE INDEX IF NOT EXISTS idx_exec_time ON executions (execution_time);
CREATE INDEX IF NOT EXISTS idx_exec_r6 ON executions (r6_state, data_status, is_current);

-- ===== 第三阶段：收益展示与数据新鲜度 =====

-- 账户级盈亏快照（来源 IBKR reqPnL）。
-- IBKR 用 NaN / max double(1.797e308) 表示「不可用」而非 null —— 这类哨兵值一律
-- 不入库（字段写 NULL 且 quality 降级），绝不能让它们混进数字参与计算。
CREATE TABLE IF NOT EXISTS pnl_snapshot (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    account         TEXT NOT NULL,
    as_of           TEXT NOT NULL,        -- UTC ISO（取数时刻）
    et_date         TEXT NOT NULL,        -- 美东交易日；与 as_of 分开存，禁止互相推导后混用
    daily_pnl       TEXT,                 -- Decimal；无效为 NULL
    realized_pnl    TEXT,                 -- Decimal 当日已实现
    unrealized_pnl  TEXT,                 -- Decimal 当前未实现
    net_liquidation TEXT,                 -- Decimal
    source          TEXT NOT NULL,        -- IBKR_reqPnL
    quality         TEXT NOT NULL,        -- OK / PARTIAL / UNAVAILABLE
    reset_policy    TEXT,                 -- IBKR_SESSION_DAILY（每个交易日重置，禁止跨日累加）
    daily_basis     TEXT,                 -- 降级时今日盈亏的基准口径（NET_LIQ_CHANGE…）
    sync_run_id     TEXT,
    created_at      TEXT NOT NULL,
    UNIQUE (account, as_of)
);
CREATE INDEX IF NOT EXISTS idx_pnl_account_date ON pnl_snapshot (account, et_date);

-- 日终有效净资产：曲线与 7/30 日计算的唯一口径。
-- 按**美东交易日**归档（原先曲线用 substr(ts,1,10) 取 UTC 日，导致日期整体错位）。
CREATE TABLE IF NOT EXISTS daily_equity (
    account         TEXT NOT NULL,
    et_date         TEXT NOT NULL,        -- YYYY-MM-DD 美东
    net_liquidation TEXT NOT NULL,        -- Decimal
    as_of           TEXT NOT NULL,        -- 该值取自哪条快照（UTC ISO）
    source          TEXT NOT NULL,
    quality         TEXT NOT NULL,        -- READY / DEGRADED
    PRIMARY KEY (account, et_date)
);

-- 资金流（入金/出金）。
-- 纪律（用户明确要求，勿改）：
--   1. 金额只能来自 IBKR 入金记录/活动报表/用户提供的准确数字；
--   2. **禁止**由净资产跳变自动推断（例如拿 47069.92-378.83 当入金额）；
--   3. 追加式审计：已登记记录需要修正时**新增冲正记录**（reverses_id 指向原记录），
--      不得 UPDATE 或 DELETE 原记录。
CREATE TABLE IF NOT EXISTS cash_flows (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    account      TEXT NOT NULL,
    currency     TEXT NOT NULL DEFAULT 'USD',
    amount       TEXT NOT NULL,           -- Decimal：正=入金，负=出金
    effective_at TEXT NOT NULL,           -- UTC ISO（资金实际到账时刻）
    et_date      TEXT NOT NULL,           -- 美东日期；与 effective_at 分开存
    note         TEXT,
    source       TEXT NOT NULL,           -- MANUAL / IBKR_ACTIVITY / FLEX_QUERY（未来）
    external_ref TEXT,                    -- 活动报表凭证号等外部可核对标识
    reverses_id  INTEGER,                 -- 冲正记录指向被冲正的原记录；普通记录为 NULL
    created_at   TEXT NOT NULL,
    FOREIGN KEY (reverses_id) REFERENCES cash_flows (id)
);
CREATE INDEX IF NOT EXISTS idx_cash_flows_account_date ON cash_flows (account, et_date);

-- 资金流覆盖声明：哪个区间的资金流「已登记完整」。
-- 没有覆盖声明的窗口一律按不完整处理 —— 页面只能显示「净资产变动」，不得称为盈亏。
CREATE TABLE IF NOT EXISTS cash_flow_coverage (
    account    TEXT NOT NULL,
    start_date TEXT NOT NULL,             -- 美东日期，含
    end_date   TEXT NOT NULL,             -- 美东日期，含
    status     TEXT NOT NULL,             -- COMPLETE / PARTIAL
    note       TEXT,
    source     TEXT NOT NULL,             -- MANUAL / FLEX_QUERY（未来）
    created_at TEXT NOT NULL,
    PRIMARY KEY (account, start_date, end_date)
);

-- ===== 告警生命周期（P1-5）=====
-- 目的：R1/R2 这类持续风险不该每 4 小时重复推送同一条通知。
-- 规则评估仍每分钟记录到 rule_runs（审计完整），但**通知**只在状态变化时发出。
CREATE TABLE IF NOT EXISTS alert_state (
    dedup_key          TEXT NOT NULL PRIMARY KEY,   -- 与 RuleHit.dedup_key 一致
    rule_id            TEXT NOT NULL,
    symbol             TEXT,
    state              TEXT NOT NULL,      -- ACTIVE / RESOLVED
    severity           TEXT,               -- 当前严重度
    notified_severity  TEXT,               -- 上次通知时的严重度（升级判定用）
    first_triggered_at TEXT NOT NULL,      -- 本轮首次触发 UTC
    last_seen_at       TEXT NOT NULL,      -- 最近一次评估中仍然触发 UTC
    last_notified_at   TEXT,               -- 最近一次实际推送 UTC
    notified_value     TEXT,               -- 上次通知时的实际值（显著恶化判定用）
    last_value         TEXT,               -- 最近一次评估的实际值
    last_summary_date  TEXT,               -- 上次日摘要的美东日期
    resolved_at        TEXT,
    notify_count       INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_alert_state_rule ON alert_state (rule_id, state);

-- ===== 投资日记 =====
-- 个人投资心得笔记；手写记录与持仓计划归档共用此表，按更新时间倒序展示。
CREATE TABLE IF NOT EXISTS journal_entries (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    content     TEXT NOT NULL,
    created_at  TEXT NOT NULL,      -- UTC ISO
    updated_at  TEXT NOT NULL,      -- UTC ISO，编辑后刷新；列表按它倒序
    source_type TEXT NOT NULL DEFAULT 'MANUAL',
    position_cycle_id TEXT,
    account     TEXT,
    is_readonly INTEGER NOT NULL DEFAULT 0,
    metadata_json TEXT,
    archived_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_journal_updated ON journal_entries (updated_at DESC);

-- ===== 持仓决策与操作计划 =====
-- 这些表只记录决策、审计和归档，不具备任何提交/修改/取消 IBKR 订单的能力。
CREATE TABLE IF NOT EXISTS position_cycles (
    id TEXT NOT NULL PRIMARY KEY,
    account TEXT NOT NULL,
    symbol TEXT NOT NULL,
    conid INTEGER,
    cycle_number INTEGER NOT NULL,
    status TEXT NOT NULL, -- PENDING/DRAFT/ACTIVE/REVIEW_DUE/TRIGGERED/INVALIDATED/CLOSED/ARCHIVED
    opened_at TEXT NOT NULL,
    opened_at_confidence TEXT NOT NULL DEFAULT 'observed', -- observed/estimated/unknown
    closed_at TEXT,
    closed_confirmed_at TEXT,
    opening_execution_id TEXT,
    opening_quantity TEXT,
    opening_average_price TEXT,
    current_quantity TEXT NOT NULL DEFAULT '0',
    current_average_cost TEXT,
    current_market_value TEXT,
    current_price TEXT,
    account_weight TEXT,             -- 当前市值 / 账户净资产
    stock_weight TEXT,               -- 当前市值 / 股票持仓市值
    current_weight TEXT,
    target_weight TEXT,
    max_weight TEXT,
    last_snapshot_id TEXT,
    last_observed_at TEXT,
    zero_observed_count INTEGER NOT NULL DEFAULT 0,
    zero_first_observed_at TEXT,
    journal_sync_status TEXT NOT NULL DEFAULT 'NOT_REQUIRED', -- NOT_REQUIRED/PENDING/SYNCED/FAILED
    journal_sync_error TEXT,
    investment_journal_entry_id INTEGER,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_position_cycles_account_status
    ON position_cycles (account, status, symbol);
CREATE INDEX IF NOT EXISTS idx_position_cycles_account_symbol
    ON position_cycles (account, symbol, cycle_number);
CREATE UNIQUE INDEX IF NOT EXISTS idx_position_cycles_active
    ON position_cycles (account, symbol)
    WHERE status IN ('PENDING','DRAFT','ACTIVE','REVIEW_DUE','TRIGGERED','INVALIDATED');

CREATE TABLE IF NOT EXISTS position_plan_versions (
    id TEXT NOT NULL PRIMARY KEY,
    position_cycle_id TEXT NOT NULL,
    account TEXT NOT NULL,
    version INTEGER NOT NULL,
    strategy_type TEXT,
    current_conclusion TEXT,
    thesis TEXT,
    why_now TEXT,
    confirmed_facts TEXT,
    subjective_assumptions TEXT,
    catalysts TEXT,
    invalidation_conditions TEXT,
    notes TEXT,
    review_at TEXT,
    max_weight TEXT,
    target_weight TEXT,
    change_reason TEXT,
    supersedes_id TEXT,
    created_at TEXT NOT NULL,
    created_by TEXT NOT NULL,
    lifecycle_status TEXT NOT NULL DEFAULT 'DRAFT', -- DRAFT/PUBLISHED/LEGACY
    updated_at TEXT,
    draft_of_version_id TEXT,
    hold_only_review INTEGER NOT NULL DEFAULT 0,
    limit_confirmed INTEGER NOT NULL DEFAULT 0,
    published_at TEXT,
    published_by TEXT,
    FOREIGN KEY (position_cycle_id) REFERENCES position_cycles(id)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_position_plan_version_number
    ON position_plan_versions (position_cycle_id, version);
CREATE INDEX IF NOT EXISTS idx_position_plan_versions_latest
    ON position_plan_versions (position_cycle_id, version DESC);

CREATE TABLE IF NOT EXISTS position_plan_actions (
    id TEXT NOT NULL PRIMARY KEY,
    position_cycle_id TEXT NOT NULL,
    account TEXT NOT NULL,
    plan_version_id TEXT NOT NULL,
    action_type TEXT NOT NULL, -- ADD/REDUCE/EXIT/REVIEW
    trigger_type TEXT NOT NULL, -- PRICE/EVENT/DATE/MANUAL
    price_min TEXT,
    price_max TEXT,
    event_condition TEXT,
    quantity TEXT,
    percentage TEXT,
    additional_conditions TEXT,
    priority INTEGER NOT NULL DEFAULT 100,
    status TEXT NOT NULL DEFAULT 'DRAFT', -- DRAFT/ACTIVE/TRIGGERED/EXECUTED/CANCELED
    triggered_at TEXT,
    trigger_evaluation_status TEXT, -- WAITING/BLOCKED/TRIGGERED
    trigger_evaluation_note TEXT,
    trigger_evaluated_at TEXT,
    trigger_quote_json TEXT,         -- price/source/quality/session/as_of/snapshot_id
    confirmed_at TEXT,               -- 人工执行意图确认，不等于成交
    confirmed_by TEXT,
    confirmation_intent TEXT,        -- EXECUTE
    confirmation_note TEXT,
    recommendation_id TEXT,
    executed_at TEXT,
    execution_id TEXT,
    canceled_at TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY (position_cycle_id) REFERENCES position_cycles(id),
    FOREIGN KEY (plan_version_id) REFERENCES position_plan_versions(id)
);
CREATE INDEX IF NOT EXISTS idx_position_plan_actions_cycle_status
    ON position_plan_actions (position_cycle_id, status, priority);
CREATE INDEX IF NOT EXISTS idx_position_plan_actions_version
    ON position_plan_actions (plan_version_id, created_at);

CREATE TABLE IF NOT EXISTS position_cycle_observations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    position_cycle_id TEXT NOT NULL,
    account TEXT NOT NULL,
    symbol TEXT NOT NULL,
    snapshot_id TEXT,
    observed_at TEXT NOT NULL,
    quantity TEXT NOT NULL,
    average_cost TEXT,
    market_value TEXT,
    account_weight TEXT,
    stock_weight TEXT,
    current_weight TEXT,
    observation_status TEXT NOT NULL, -- POSITIVE/ZERO/MISSING/UNRELIABLE
    UNIQUE (position_cycle_id, snapshot_id),
    FOREIGN KEY (position_cycle_id) REFERENCES position_cycles(id)
);
CREATE INDEX IF NOT EXISTS idx_position_cycle_obs_latest
    ON position_cycle_observations (position_cycle_id, observed_at DESC);

CREATE TABLE IF NOT EXISTS position_cycle_events (
    id TEXT NOT NULL PRIMARY KEY,
    position_cycle_id TEXT NOT NULL,
    account TEXT NOT NULL,
    symbol TEXT NOT NULL,
    event_type TEXT NOT NULL, -- OPEN/ADD/REDUCE/EXIT/ADJUST
    execution_id TEXT,
    order_id TEXT,
    plan_action_id TEXT,
    execution_time TEXT NOT NULL,
    quantity TEXT,
    price TEXT,
    pre_quantity TEXT,
    post_quantity TEXT,
    average_cost TEXT,
    realized_pnl TEXT,
    market_value TEXT,
    snapshot_id TEXT,
    match_status TEXT, -- MATCHED/PLAN_OUTSIDE/PENDING_CONFIRMATION/NOT_APPLICABLE
    deviation_reason TEXT,
    created_at TEXT NOT NULL,
    UNIQUE (position_cycle_id, execution_id),
    FOREIGN KEY (position_cycle_id) REFERENCES position_cycles(id)
);
CREATE INDEX IF NOT EXISTS idx_position_cycle_events_cycle_time
    ON position_cycle_events (position_cycle_id, execution_time);

CREATE TABLE IF NOT EXISTS position_plan_execution_links (
    id TEXT NOT NULL PRIMARY KEY,
    position_cycle_id TEXT NOT NULL,
    account TEXT NOT NULL,
    plan_action_id TEXT,
    execution_id TEXT NOT NULL,
    match_status TEXT NOT NULL, -- MATCHED/PLAN_OUTSIDE/PENDING_CONFIRMATION
    deviation_reason TEXT,
    matched_at TEXT NOT NULL,
    confirmed_by_user TEXT,
    UNIQUE (position_cycle_id, execution_id),
    FOREIGN KEY (position_cycle_id) REFERENCES position_cycles(id),
    FOREIGN KEY (plan_action_id) REFERENCES position_plan_actions(id)
);
CREATE INDEX IF NOT EXISTS idx_position_plan_links_status
    ON position_plan_execution_links (position_cycle_id, match_status);

CREATE TABLE IF NOT EXISTS position_plan_audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account TEXT NOT NULL,
    position_cycle_id TEXT,
    actor TEXT NOT NULL,
    operation TEXT NOT NULL,
    idempotency_key TEXT,
    detail_json TEXT,
    created_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_position_plan_audit_idempotency
    ON position_plan_audit_log (idempotency_key)
    WHERE idempotency_key IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_position_plan_audit_cycle
    ON position_plan_audit_log (account, position_cycle_id, created_at DESC);

-- ===== 数据质量问题（账户快照层 / 数据质量层）=====
-- 每次同步产出的质量问题逐条落库，供页面展示、审计与 AI 门禁判定。
-- 与快照的关系是 1:N —— 快照级的整体结论（quality_status / ai_analysis_allowed）
-- 存在 balances_snapshot 上，明细问题存这里。
CREATE TABLE IF NOT EXISTS data_quality_issues (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    snapshot_id    TEXT,               -- 关联 balances_snapshot.snapshot_id
    sync_run_id    TEXT,
    account        TEXT,
    issue_code     TEXT NOT NULL,      -- 稳定的机器可读码，如 NET_LIQ_MISSING
    severity       TEXT NOT NULL,      -- info / warning / critical
    category       TEXT NOT NULL,      -- completeness/freshness/sanity/continuity/source
    message        TEXT NOT NULL,      -- 人话描述
    affected_field TEXT,
    expected_value TEXT,
    actual_value   TEXT,
    blocks_ai      INTEGER NOT NULL DEFAULT 0,   -- 是否阻止后续 AI 分析
    detected_at    TEXT NOT NULL       -- UTC ISO
);
CREATE INDEX IF NOT EXISTS idx_dqi_snapshot ON data_quality_issues (snapshot_id);
CREATE INDEX IF NOT EXISTS idx_dqi_detected ON data_quality_issues (detected_at DESC);
CREATE INDEX IF NOT EXISTS idx_dqi_severity ON data_quality_issues (severity, detected_at DESC);

-- ===== 风险评估层 =====
-- 与账户快照一一对应：一个 snapshot_id 一份评估。结论**不写回**快照原始表，
-- 保证原始数据与派生结论各自独立、互不污染。
CREATE TABLE IF NOT EXISTS risk_assessments (
    assessment_id       TEXT NOT NULL PRIMARY KEY,
    snapshot_id         TEXT NOT NULL,
    account_id          TEXT,
    data_as_of          TEXT NOT NULL,      -- 快照数据时点 UTC ISO
    risk_level          TEXT NOT NULL,      -- low/moderate/high/critical/blocked
    risk_score          TEXT,               -- Decimal 字符串，0-100
    reliable            INTEGER NOT NULL,   -- 数据是否足以支撑可靠判断
    blocked_reason      TEXT,               -- reliable=0 时的原因
    component_scores    TEXT,               -- JSON 六维分项
    calculated_metrics  TEXT,               -- JSON 全量指标（可回溯）
    triggered_rules     TEXT,               -- JSON 触发规则码列表
    top_risk_drivers    TEXT,               -- JSON
    data_quality_status TEXT,
    ai_analysis_allowed INTEGER,
    calculation_version TEXT NOT NULL,      -- 规则/算法版本，变化后可重算
    created_at          TEXT NOT NULL,
    UNIQUE (snapshot_id, calculation_version)   -- 同快照同版本只算一次（幂等）
);
CREATE INDEX IF NOT EXISTS idx_ra_as_of ON risk_assessments (data_as_of DESC);
CREATE INDEX IF NOT EXISTS idx_ra_level ON risk_assessments (risk_level, data_as_of DESC);
CREATE INDEX IF NOT EXISTS idx_ra_snapshot ON risk_assessments (snapshot_id);

-- 触发的风险事件（1:N）
CREATE TABLE IF NOT EXISTS risk_events (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    assessment_id     TEXT NOT NULL,
    snapshot_id       TEXT NOT NULL,
    event_code        TEXT NOT NULL,
    rule_code         TEXT NOT NULL,
    category          TEXT NOT NULL,
    severity          TEXT NOT NULL,      -- warning/high/critical
    title             TEXT NOT NULL,
    explanation       TEXT NOT NULL,
    actual_value      TEXT,
    threshold_value   TEXT,
    unit              TEXT,
    affected_symbol   TEXT,
    blocks_ai         INTEGER NOT NULL DEFAULT 0,
    recommended_check TEXT,               -- 建议用户检查什么，**不是**交易指令
    detected_at       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_re_assessment ON risk_events (assessment_id);
CREATE INDEX IF NOT EXISTS idx_re_detected ON risk_events (detected_at DESC);
CREATE INDEX IF NOT EXISTS idx_re_symbol ON risk_events (affected_symbol);

-- 规则定义快照（供审计：某次评估当时用的是哪套阈值）
CREATE TABLE IF NOT EXISTS risk_rules (
    rule_code         TEXT NOT NULL,
    version           TEXT NOT NULL,
    name              TEXT NOT NULL,
    category          TEXT NOT NULL,
    description       TEXT,
    enabled           INTEGER NOT NULL DEFAULT 1,
    severity          TEXT NOT NULL,
    threshold         TEXT,
    comparator        TEXT,
    calculation_basis TEXT,
    unit              TEXT,
    scope             TEXT,
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL,
    PRIMARY KEY (rule_code, version)
);

-- ===== 证券元数据（分类机制）=====
-- 优先级：人工覆盖 > IBKR reqContractDetails > 遗留 sectors.json > Unknown
-- 缓存表：分类信息变化极慢，不该每分钟去问券商。
CREATE TABLE IF NOT EXISTS instrument_metadata (
    symbol        TEXT NOT NULL PRIMARY KEY,
    conid         INTEGER,
    sec_type      TEXT,            -- STK / ETF / IND / OPT / FUT / CASH / BOND
    company_name  TEXT,
    sector        TEXT,            -- 板块（R2 与集中度用）
    industry      TEXT,            -- 更细的行业
    asset_class   TEXT,            -- equity / etf / index / cash / bond / option / unknown
    source        TEXT NOT NULL,   -- override / ibkr / sectors_json / unknown
    confidence    TEXT NOT NULL,   -- high / medium / low / none
    unknown_reason TEXT,           -- 无法分类时必须写明原因
    raw_json      TEXT,            -- IBKR contractDetails 脱敏原文
    updated_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_im_sector ON instrument_metadata (sector);

-- 人工确认的分类覆盖（最高优先级，可审计）
CREATE TABLE IF NOT EXISTS instrument_classification_overrides (
    symbol       TEXT NOT NULL PRIMARY KEY,
    sector       TEXT,
    industry     TEXT,
    asset_class  TEXT,
    company_name TEXT,
    note         TEXT,             -- 为什么这样覆盖
    created_by   TEXT,
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL
);

-- ===== 市场环境引擎 =====
-- 与账户数据完全解耦：市场数据抓取失败绝不能影响 IBKR 账户同步。
CREATE TABLE IF NOT EXISTS market_data_snapshots (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    instrument_code TEXT NOT NULL,      -- QQQ / SPY / SOXX / VIX / VXN / US10Y
    instrument_name TEXT,
    data_as_of      TEXT NOT NULL,      -- 数据本身的时间（交易日）
    fetched_at      TEXT NOT NULL,      -- 抓取时刻 —— 与数据时间分开存
    open            TEXT, high TEXT, low TEXT, close TEXT,
    previous_close  TEXT,
    daily_change    TEXT,
    daily_change_percent TEXT,
    volume          INTEGER,
    source          TEXT NOT NULL,      -- IBKR_historical 等
    delayed         INTEGER NOT NULL DEFAULT 1,
    quality_status  TEXT NOT NULL,      -- fresh / stale / error / missing
    quality_note    TEXT,
    quote_as_of     TEXT,               -- 实时报价时点 UTC；日线来源为空
    raw_json        TEXT,
    UNIQUE (instrument_code, data_as_of)
);
CREATE INDEX IF NOT EXISTS idx_mds_code_date
    ON market_data_snapshots (instrument_code, data_as_of DESC);

CREATE TABLE IF NOT EXISTS market_environment_assessments (
    assessment_id       TEXT NOT NULL PRIMARY KEY,
    data_as_of          TEXT NOT NULL,
    market_state        TEXT NOT NULL,   -- supportive/neutral/cautious/stressed/blocked
    market_risk_level   TEXT NOT NULL,
    market_risk_score   TEXT,
    reliable            INTEGER NOT NULL,
    blocked_reason      TEXT,
    component_scores    TEXT,            -- JSON（breadth 缺失时为 null，权重重新归一）
    calculated_metrics  TEXT,
    triggered_rules     TEXT,
    top_market_drivers  TEXT,
    quality_status      TEXT,
    calculation_version TEXT NOT NULL,
    rule_set_version    TEXT,
    rule_set_hash       TEXT,
    created_at          TEXT NOT NULL,
    UNIQUE (data_as_of, calculation_version)
);
CREATE INDEX IF NOT EXISTS idx_mea_as_of ON market_environment_assessments (data_as_of DESC);
CREATE INDEX IF NOT EXISTS idx_mea_state ON market_environment_assessments (market_state, data_as_of DESC);

CREATE TABLE IF NOT EXISTS market_environment_events (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    assessment_id       TEXT NOT NULL,
    event_code          TEXT NOT NULL,
    rule_code           TEXT NOT NULL,
    category            TEXT NOT NULL,
    severity            TEXT NOT NULL,
    title               TEXT NOT NULL,
    explanation         TEXT NOT NULL,
    actual_value        TEXT, threshold_value TEXT, unit TEXT,
    affected_instrument TEXT,
    recommended_check   TEXT,
    rule_version        TEXT,
    detected_at         TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_mee_assessment ON market_environment_events (assessment_id);

CREATE TABLE IF NOT EXISTS market_data_sync_runs (
    run_id       TEXT NOT NULL PRIMARY KEY,
    started_at   TEXT NOT NULL,
    finished_at  TEXT,
    run_state    TEXT NOT NULL,      -- running/success/partial/failed
    instruments_ok    INTEGER DEFAULT 0,
    instruments_failed INTEGER DEFAULT 0,
    fetch_ms     INTEGER,
    error_code   TEXT,
    error_message TEXT,
    request_id   TEXT
);
CREATE INDEX IF NOT EXISTS idx_mdsr_started ON market_data_sync_runs (started_at DESC);

-- ===== 风险事件运营层 v1 =====
-- 统一事件注册表。账户/持仓/市场/联合/数据质量/行为纪律六类来源归一到这里，
-- 供时间线、通知与复盘共用一套词汇。
--
-- 为什么需要新表而不是只做服务层聚合：risk_events 与 market_environment_events
-- 是**逐次评估的追加日志**（每分钟一条），没有生命周期概念。运营层需要的是
-- 「一个风险从首次触发到恢复」的单条可变记录 —— 累计次数、通知次数、确认状态
-- 都必须能就地更新。这两种形态无法用同一张表承载，原表保持只追加不动。
CREATE TABLE IF NOT EXISTS risk_event_registry (
    event_id           TEXT NOT NULL PRIMARY KEY,
    identity           TEXT NOT NULL,      -- source|rule|affected|account
    episode_seq        INTEGER NOT NULL DEFAULT 1,
    source_type        TEXT NOT NULL,      -- account/position/market/combined/data_quality/behavior
    source_event_id    TEXT,               -- 上游 risk_events / market_environment_events 的 id
    event_code         TEXT NOT NULL,
    rule_code          TEXT NOT NULL,
    rule_version       TEXT,
    rule_set_version   TEXT,
    category           TEXT NOT NULL,
    severity           TEXT NOT NULL,      -- info/warning/high/critical
    max_severity       TEXT,               -- 本 episode 内到达过的最高severity（降级后仍可查）
    status             TEXT NOT NULL,      -- open/acknowledged/resolved/suppressed
    title              TEXT NOT NULL,
    explanation        TEXT NOT NULL,
    actual_value       TEXT,
    threshold_value    TEXT,
    unit               TEXT,
    affected_symbol    TEXT,
    affected_instrument TEXT,
    account_id         TEXT,
    snapshot_id        TEXT,
    assessment_id      TEXT,
    data_as_of         TEXT,
    first_detected_at  TEXT NOT NULL,
    last_detected_at   TEXT NOT NULL,
    resolved_at        TEXT,
    occurrence_count   INTEGER NOT NULL DEFAULT 1,
    notification_count INTEGER NOT NULL DEFAULT 0,
    last_notified_at   TEXT,
    notified_severity  TEXT,               -- 上次通知时的严重度（升级判定，复用 alerting 语义）
    notified_value     TEXT,
    recommended_check  TEXT,
    quality_status     TEXT,
    reliable           INTEGER NOT NULL DEFAULT 1,
    -- 通知归属：ops = 运营层负责推送；legacy = 既有 R1–R7 链路负责，
    -- 运营层只登记不推送，避免同一风险双发。
    notification_owner TEXT NOT NULL DEFAULT 'ops',
    acknowledged_at    TEXT,
    acknowledged_by    TEXT,               -- 预留多用户
    suppressed_until   TEXT,
    suppressed_reason  TEXT,
    created_at         TEXT NOT NULL,
    updated_at         TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_rer_identity ON risk_event_registry (identity, episode_seq DESC);
CREATE INDEX IF NOT EXISTS idx_rer_status ON risk_event_registry (status, severity);
CREATE INDEX IF NOT EXISTS idx_rer_last ON risk_event_registry (last_detected_at DESC);
CREATE INDEX IF NOT EXISTS idx_rer_first ON risk_event_registry (first_detected_at DESC);
CREATE INDEX IF NOT EXISTS idx_rer_source ON risk_event_registry (source_type, status);
CREATE INDEX IF NOT EXISTS idx_rer_symbol ON risk_event_registry (affected_symbol);

-- 状态与严重度变化历史。**只保存最终状态是不够的** ——
-- 「这个风险什么时候升到 critical、什么时候降回来」正是复盘要看的东西。
CREATE TABLE IF NOT EXISTS risk_event_transitions (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id          TEXT NOT NULL,
    transition        TEXT NOT NULL,      -- opened/escalated/deescalated/resolved/...
    previous_status   TEXT,
    new_status        TEXT,
    previous_severity TEXT,
    new_severity      TEXT,
    actual_value      TEXT,
    threshold_value   TEXT,
    reason            TEXT,
    actor             TEXT,               -- 人工操作来源（预留多用户）
    transitioned_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ret_event ON risk_event_transitions (event_id, transitioned_at);
CREATE INDEX IF NOT EXISTS idx_ret_time ON risk_event_transitions (transitioned_at DESC);

-- 通知日志。每一次「尝试发送」都记一条，成功与否都留痕 ——
-- 发送失败绝不能被记成已通知，否则下轮冷静期会把真正的告警吞掉。
CREATE TABLE IF NOT EXISTS risk_notifications (
    notification_id   TEXT NOT NULL PRIMARY KEY,
    event_id          TEXT,               -- 合并通知时为代表事件；明细见 event_ids
    event_ids         TEXT,               -- JSON 数组：本次合并覆盖的全部事件
    channel           TEXT NOT NULL,      -- feishu/...
    notification_type TEXT NOT NULL,      -- opened/escalated/reminder/recovered/blocked/test
    severity          TEXT,
    title             TEXT,
    payload_summary   TEXT,               -- 正文摘要，**不含任何凭证**
    dedupe_key        TEXT,
    attempted_at      TEXT NOT NULL,
    sent_at           TEXT,
    success           INTEGER NOT NULL DEFAULT 0,
    error_code        TEXT,
    error_message     TEXT,
    retry_count       INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_rn_event ON risk_notifications (event_id, attempted_at DESC);
CREATE INDEX IF NOT EXISTS idx_rn_time ON risk_notifications (attempted_at DESC);
CREATE INDEX IF NOT EXISTS idx_rn_dedupe ON risk_notifications (dedupe_key, attempted_at DESC);

-- ===== 风险事件复盘与阈值回测 v1 =====
-- 复盘与回测的结果**全部落在新表**，原始事件表与原始风险评估表一行不改 ——
-- 复盘是对历史的解读，解读可以重算，事实不能被解读覆盖。
CREATE TABLE IF NOT EXISTS risk_event_reviews (
    review_id            TEXT NOT NULL PRIMARY KEY,
    event_id             TEXT NOT NULL,
    source_type          TEXT NOT NULL,
    rule_code            TEXT NOT NULL,
    rule_version         TEXT,
    rule_set_version     TEXT,
    affected_symbol      TEXT,
    affected_instrument  TEXT,
    first_detected_at    TEXT NOT NULL,
    peak_severity        TEXT,
    peak_actual_value    TEXT,
    resolved_at          TEXT,
    duration_minutes     INTEGER,
    account_value_at_open       TEXT,
    account_value_at_peak       TEXT,
    account_value_at_resolution TEXT,
    market_state_at_open        TEXT,
    account_risk_at_open        TEXT,
    market_risk_at_open         TEXT,
    combined_risk_at_open       TEXT,
    forward_outcomes     TEXT,            -- JSON：按窗口的前瞻结果与标签命中
    outcome_labels       TEXT,            -- JSON：{window: {label: true/false/null}}
    lead_time_minutes    INTEGER,
    lead_time_class      TEXT,            -- leading/concurrent/lagging/no_outcome
    cash_flow_status     TEXT,            -- clean/estimated/contaminated
    cash_flow_note       TEXT,
    trading_activity     TEXT,            -- JSON：窗口内成交，用于识别主动减仓
    review_status        TEXT NOT NULL,   -- pending/eligible/completed/insufficient_data/excluded
    exclusion_reason     TEXT,            -- excluded 必须写明原因
    data_quality_status  TEXT,
    calculation_version  TEXT NOT NULL,
    created_at           TEXT NOT NULL,
    updated_at           TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_rer_event_ver
    ON risk_event_reviews (event_id, calculation_version);
CREATE INDEX IF NOT EXISTS idx_rev_rule ON risk_event_reviews (rule_code, review_status);
CREATE INDEX IF NOT EXISTS idx_rev_first ON risk_event_reviews (first_detected_at DESC);
CREATE INDEX IF NOT EXISTS idx_rev_symbol ON risk_event_reviews (affected_symbol);

-- 规则效果统计。按 (规则, 窗口, 标签) 三元组成行 ——
-- 同一条规则在 1 日窗口和 20 日窗口下的表现可以完全相反，合成一行会掩盖这件事。
CREATE TABLE IF NOT EXISTS risk_rule_performance (
    perf_id              TEXT NOT NULL PRIMARY KEY,
    rule_code            TEXT NOT NULL,
    rule_set_version     TEXT,
    source_type          TEXT,
    category             TEXT,
    observation_window   INTEGER NOT NULL,
    outcome_label        TEXT NOT NULL,
    date_start           TEXT,
    date_end             TEXT,
    episodes_total       INTEGER NOT NULL DEFAULT 0,
    episodes_reviewed    INTEGER NOT NULL DEFAULT 0,
    episodes_insufficient INTEGER NOT NULL DEFAULT 0,
    avg_duration_minutes TEXT,
    median_duration_minutes TEXT,
    triggers_per_month   TEXT,
    notifications_total  INTEGER NOT NULL DEFAULT 0,
    notifications_per_hit TEXT,
    outcome_rate_when_triggered   TEXT,
    outcome_rate_when_not_triggered TEXT,
    precision_value      TEXT,
    recall_value         TEXT,
    false_positive_rate  TEXT,
    false_negative_rate  TEXT,
    avg_lead_time_minutes TEXT,
    median_lead_time_minutes TEXT,
    avg_mae              TEXT,            -- 平均最大不利变化
    avg_mfe              TEXT,            -- 平均最大有利变化
    escalation_rate      TEXT,
    resolution_rate      TEXT,
    critical_rate        TEXT,
    lead_class           TEXT,            -- leading/concurrent/lagging/noisy/insufficient_data
    sample_size          INTEGER NOT NULL DEFAULT 0,
    confidence           TEXT NOT NULL,   -- insufficient/low/moderate/strong
    composite_score      TEXT,
    score_components     TEXT,            -- JSON：综合分的构成，必须可展开
    current_threshold    TEXT,
    calculation_version  TEXT NOT NULL,
    computed_at          TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_perf_key ON risk_rule_performance
    (rule_code, observation_window, outcome_label, calculation_version);
CREATE INDEX IF NOT EXISTS idx_perf_rule ON risk_rule_performance (rule_code);

-- 回测任务
CREATE TABLE IF NOT EXISTS risk_backtest_runs (
    backtest_run_id      TEXT NOT NULL PRIMARY KEY,
    param_hash           TEXT NOT NULL,   -- 相同参数可识别与复用
    requested_rules      TEXT,            -- JSON
    date_start           TEXT,
    date_end             TEXT,
    observation_windows  TEXT,            -- JSON
    outcome_labels       TEXT,            -- JSON
    candidate_thresholds TEXT,            -- JSON
    train_end            TEXT,            -- 训练/验证切分点
    status               TEXT NOT NULL,   -- pending/running/success/partial/failed
    started_at           TEXT,
    completed_at         TEXT,
    duration_ms          INTEGER,
    data_version         TEXT,
    calculation_version  TEXT,
    rules_evaluated      INTEGER DEFAULT 0,
    rules_failed         INTEGER DEFAULT 0,
    error_message        TEXT,
    created_at           TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_bt_hash ON risk_backtest_runs (param_hash, status);
CREATE INDEX IF NOT EXISTS idx_bt_created ON risk_backtest_runs (created_at DESC);

-- 回测结果：每个 (规则, 候选阈值, 窗口, 标签, 区间段) 一行
CREATE TABLE IF NOT EXISTS risk_backtest_results (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    backtest_run_id      TEXT NOT NULL,
    rule_code            TEXT NOT NULL,
    threshold_value      TEXT NOT NULL,
    is_current_threshold INTEGER NOT NULL DEFAULT 0,
    observation_window   INTEGER NOT NULL,
    outcome_label        TEXT NOT NULL,
    segment              TEXT NOT NULL,   -- full/train/validation/walk_N
    segment_start        TEXT,
    segment_end          TEXT,
    trigger_count        INTEGER NOT NULL DEFAULT 0,
    triggers_per_month   TEXT,
    true_positive        INTEGER NOT NULL DEFAULT 0,
    false_positive       INTEGER NOT NULL DEFAULT 0,
    false_negative       INTEGER NOT NULL DEFAULT 0,
    true_negative        INTEGER NOT NULL DEFAULT 0,
    precision_value      TEXT,
    recall_value         TEXT,
    false_positive_rate  TEXT,
    avg_lead_time_days   TEXT,
    median_lead_time_days TEXT,
    max_consecutive_misses INTEGER,
    estimated_notifications INTEGER,
    delta_vs_current     TEXT,            -- JSON：与当前阈值的对比
    sample_size          INTEGER NOT NULL DEFAULT 0,
    confidence           TEXT,
    created_at           TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_btr_run ON risk_backtest_results (backtest_run_id, rule_code);
CREATE INDEX IF NOT EXISTS idx_btr_rule ON risk_backtest_results (rule_code, segment);

-- 规则组合分析结果
CREATE TABLE IF NOT EXISTS risk_combo_results (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    backtest_run_id      TEXT NOT NULL,
    combo_code           TEXT NOT NULL,
    member_rules         TEXT NOT NULL,   -- JSON
    observation_window   INTEGER NOT NULL,
    outcome_label        TEXT NOT NULL,
    combo_triggers       INTEGER DEFAULT 0,
    combo_precision      TEXT,
    combo_recall         TEXT,
    combo_lead_time_days TEXT,
    member_stats         TEXT,            -- JSON：各单规则的对照数据
    recall_delta         TEXT,            -- 组合相对最优单规则的召回变化（常为负）
    precision_delta      TEXT,
    sample_size          INTEGER DEFAULT 0,
    confidence           TEXT,
    created_at           TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_combo_run ON risk_combo_results (backtest_run_id);

-- ===== Flex Query 现金流与真实收益层 v1 =====
-- 注：cash_flows 表在本阶段**扩展**而非新建（见 db._migrate）。原表只存外部出入金，
-- 新增 category/subtype 等列后可容纳全部流水类型。老消费方（净值曲线的资金流调整）
-- 必须按 category='external_cash_flow' 过滤 —— 否则分红和佣金会被当成转账扣掉。

CREATE TABLE IF NOT EXISTS flex_sync_runs (
    run_id            TEXT NOT NULL PRIMARY KEY,
    query_id_masked   TEXT,               -- 只存后 4 位，绝不存完整 query id
    reference_code    TEXT,
    started_at        TEXT NOT NULL,
    completed_at      TEXT,
    status            TEXT NOT NULL,      -- pending/requesting/waiting_report/downloading/parsing/success/partial/failed/not_configured
    report_from       TEXT,
    report_to         TEXT,
    records_received  INTEGER DEFAULT 0,
    records_inserted  INTEGER DEFAULT 0,
    records_updated   INTEGER DEFAULT 0,
    records_skipped   INTEGER DEFAULT 0,
    realized_pnl_rows INTEGER DEFAULT 0,
    poll_count        INTEGER DEFAULT 0,
    retry_count       INTEGER DEFAULT 0,
    duration_ms       INTEGER,
    error_code        TEXT,
    error_message     TEXT,               -- 已脱敏
    request_id        TEXT,
    report_hash       TEXT,               -- 报表内容指纹，用于识别重复下载
    trigger_source    TEXT,               -- manual / scheduled
    section_counts    TEXT,               -- JSON
    created_at        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_fsr_started ON flex_sync_runs (started_at DESC);
CREATE INDEX IF NOT EXISTS idx_fsr_status ON flex_sync_runs (status, started_at DESC);
CREATE INDEX IF NOT EXISTS idx_fsr_hash ON flex_sync_runs (report_hash);

-- 已实现盈亏。**用 IBKR 权威字段**，不用当前持仓成本反推历史。
CREATE TABLE IF NOT EXISTS realized_pnl_records (
    record_id       TEXT NOT NULL PRIMARY KEY,
    account_id      TEXT,
    symbol          TEXT,
    conid           TEXT,
    open_date       TEXT,
    close_date      TEXT,
    quantity        TEXT,
    proceeds        TEXT,
    cost_basis      TEXT,
    realized_pnl    TEXT NOT NULL,
    commission      TEXT,
    currency        TEXT,
    fx_rate         TEXT,
    transaction_id  TEXT,
    source          TEXT,
    raw_json        TEXT,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_rpr_close ON realized_pnl_records (close_date DESC);
CREATE INDEX IF NOT EXISTS idx_rpr_symbol ON realized_pnl_records (symbol);

-- 收入支出汇总（按日 × 分类）。查询时也能从 cash_flows 聚合，
-- 这张表用于把「已确认口径」固化下来，避免分类规则变化后历史报表跟着变。
CREATE TABLE IF NOT EXISTS income_expense_records (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id      TEXT,
    et_date         TEXT NOT NULL,
    category        TEXT NOT NULL,
    subtype         TEXT,
    currency        TEXT NOT NULL,
    amount          TEXT NOT NULL,
    amount_base     TEXT,
    record_count    INTEGER NOT NULL DEFAULT 0,
    calculation_version TEXT NOT NULL,
    created_at      TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_ier_key ON income_expense_records
    (account_id, et_date, category, subtype, currency, calculation_version);
CREATE INDEX IF NOT EXISTS idx_ier_date ON income_expense_records (et_date DESC);

-- 收益区间。Modified Dietz / TWR 的计算结果与假设一并落库。
CREATE TABLE IF NOT EXISTS performance_periods (
    period_id         TEXT NOT NULL PRIMARY KEY,
    account_id        TEXT,
    period_start      TEXT NOT NULL,
    period_end        TEXT NOT NULL,
    granularity       TEXT NOT NULL,     -- day/week/custom
    opening_nav       TEXT,
    closing_nav       TEXT,
    raw_nav_change    TEXT,
    external_cash_flow TEXT,
    deposits          TEXT,
    withdrawals       TEXT,
    cash_flow_adjusted_pnl TEXT,
    modified_dietz_return  TEXT,
    time_weighted_return   TEXT,
    realized_pnl      TEXT,
    unrealized_pnl    TEXT,
    dividends         TEXT,
    interest          TEXT,
    commissions       TEXT,
    fees              TEXT,
    taxes             TEXT,
    fx_effect         TEXT,
    market_pnl        TEXT,
    unexplained_difference TEXT,
    calculation_method TEXT,             -- 公式与权重假设
    weighting_assumption TEXT,           -- 现金流权重假设（midpoint/day_weighted）
    data_quality      TEXT NOT NULL,     -- ok/estimated/insufficient
    quality_note      TEXT,
    cash_flow_source  TEXT,              -- flex_query/heuristic/none
    calculation_version TEXT NOT NULL,
    created_at        TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_pp_key ON performance_periods
    (account_id, period_start, period_end, granularity, calculation_version);
CREATE INDEX IF NOT EXISTS idx_pp_end ON performance_periods (period_end DESC);

-- 现金对账
CREATE TABLE IF NOT EXISTS cash_reconciliations (
    recon_id          TEXT NOT NULL PRIMARY KEY,
    account_id        TEXT,
    period_start      TEXT NOT NULL,
    period_end        TEXT NOT NULL,
    currency          TEXT NOT NULL,
    opening_cash      TEXT,
    closing_cash      TEXT,
    external_cash_flow TEXT,
    trade_cash_flow   TEXT,
    income            TEXT,
    expense           TEXT,
    tax               TEXT,
    interest          TEXT,
    other             TEXT,
    expected_closing_cash TEXT,
    reconciliation_difference TEXT,
    tolerance         TEXT,
    quality_status    TEXT NOT NULL,     -- reconciled/within_tolerance/mismatch/insufficient_data
    issues            TEXT,              -- JSON
    calculation_version TEXT NOT NULL,
    created_at        TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_cr_key ON cash_reconciliations
    (account_id, period_start, period_end, currency, calculation_version);
CREATE INDEX IF NOT EXISTS idx_cr_end ON cash_reconciliations (period_end DESC);

-- ===== 真实现金对账与完整收益归因 v1 =====
-- Statement of Funds 的期初/期末余额锚点。**绝不进 cash_flows** ——
-- 它们不是经济事件，混进流水会被当成收入或支出计入收益。
CREATE TABLE IF NOT EXISTS statement_cash_balances (
    balance_id        TEXT NOT NULL PRIMARY KEY,
    account_id        TEXT,
    report_from       TEXT,
    report_to         TEXT,
    balance_date      TEXT NOT NULL,
    balance_type      TEXT NOT NULL,   -- opening/closing/currency_opening/currency_closing/unknown
    currency          TEXT NOT NULL,
    balance           TEXT NOT NULL,
    balance_base      TEXT,
    fx_rate_to_base   TEXT,
    level_of_detail   TEXT,
    description       TEXT,
    node_index        INTEGER,
    source            TEXT NOT NULL DEFAULT 'FLEX_STATEMENT_OF_FUNDS',
    report_hash       TEXT,
    quality_status    TEXT NOT NULL DEFAULT 'ok',
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_scb_period ON statement_cash_balances
    (account_id, report_from, report_to, currency);
CREATE INDEX IF NOT EXISTS idx_scb_date ON statement_cash_balances (balance_date DESC);

-- 券商汇率表。当前账户以 USD 为主，但**不能假设永远只有 USD**。
CREATE TABLE IF NOT EXISTS flex_conversion_rates (
    rate_id       TEXT NOT NULL PRIMARY KEY,
    rate_date     TEXT NOT NULL,
    from_currency TEXT NOT NULL,
    to_currency   TEXT NOT NULL,
    rate          TEXT NOT NULL,
    report_hash   TEXT,
    source        TEXT NOT NULL DEFAULT 'FLEX_CONVERSION_RATE',
    created_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_fcr_lookup ON flex_conversion_rates
    (rate_date, from_currency, to_currency);

-- IBKR Flex「Change in NAV」按日官方业绩。历史收益以此为第一优先；
-- reqPnL 只用于实时，因为它的重置日程由 TWS/品种配置决定。
CREATE TABLE IF NOT EXISTS flex_nav_daily (
    account_id          TEXT NOT NULL,
    et_date             TEXT NOT NULL,
    currency            TEXT NOT NULL,
    starting_value      TEXT NOT NULL,
    ending_value        TEXT NOT NULL,
    total_pnl           TEXT NOT NULL, -- ending - starting - capital movements
    mtm                 TEXT,
    commissions         TEXT,
    capital_movements   TEXT NOT NULL,
    deposits_withdrawals TEXT,
    internal_cash_transfers TEXT,
    asset_transfers     TEXT,
    dividends           TEXT,
    interest            TEXT,
    fees                TEXT,
    other               TEXT,
    twr                 TEXT,          -- IBKR 返回百分数，如 21.83 表示 21.83%
    raw_json            TEXT NOT NULL,
    source              TEXT NOT NULL DEFAULT 'FLEX_CHANGE_IN_NAV',
    report_hash         TEXT,
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL,
    PRIMARY KEY (account_id, et_date, currency)
);
CREATE INDEX IF NOT EXISTS idx_fnd_date ON flex_nav_daily (et_date DESC);

-- IBKR Flex「Transfers」证券转仓明细。证券转入/转出是资本搬运，不进入
-- cash_flows，也不计作投资收益；Change in NAV 的 assetTransfers 是其日级汇总。
CREATE TABLE IF NOT EXISTS flex_transfers (
    transfer_id          TEXT NOT NULL PRIMARY KEY,
    account_id           TEXT NOT NULL,
    report_date          TEXT NOT NULL,
    transfer_date        TEXT,
    settle_date          TEXT,
    transfer_type        TEXT,
    direction            TEXT,
    transfer_company     TEXT,
    delivering_broker    TEXT,
    currency             TEXT,
    asset_category       TEXT,
    symbol               TEXT,
    description          TEXT,
    conid                TEXT,
    quantity             TEXT,
    transfer_price       TEXT,
    cost_basis           TEXT,
    position_amount      TEXT,
    position_amount_base TEXT,
    pnl_amount           TEXT,
    pnl_amount_base      TEXT,
    cash_transfer        TEXT,
    transaction_id       TEXT,
    code                 TEXT,
    level_of_detail      TEXT,
    raw_json             TEXT NOT NULL,
    source               TEXT NOT NULL DEFAULT 'FLEX_TRANSFER',
    report_hash          TEXT,
    created_at           TEXT NOT NULL,
    updated_at           TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ft_date ON flex_transfers
    (account_id, report_date DESC);
CREATE INDEX IF NOT EXISTS idx_ft_txn ON flex_transfers (transaction_id);

-- 按日未实现盈亏。来自持仓快照，**同一日只用同一个 snapshot_id** ——
-- 混用不同快照会让「未实现盈亏变化」里混进快照之间的持仓变动。
CREATE TABLE IF NOT EXISTS daily_unrealized_pnl (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id        TEXT,
    et_date           TEXT NOT NULL,
    snapshot_id       TEXT NOT NULL,
    snapshot_ts       TEXT NOT NULL,
    unrealized_pnl    TEXT,
    market_value      TEXT,
    cost_basis        TEXT,
    position_count    INTEGER,
    source_field      TEXT,            -- ibkr_unrealized / market_value_minus_cost / none
    quality_status    TEXT NOT NULL,   -- ok/estimated/insufficient_data
    calculation_version TEXT NOT NULL,
    created_at        TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_dup_key ON daily_unrealized_pnl
    (account_id, et_date, calculation_version);
CREATE INDEX IF NOT EXISTS idx_dup_date ON daily_unrealized_pnl (et_date DESC);

-- 单标的未实现盈亏贡献
CREATE TABLE IF NOT EXISTS position_pnl_attribution (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id        TEXT,
    et_date           TEXT NOT NULL,
    snapshot_id       TEXT NOT NULL,
    symbol            TEXT NOT NULL,
    conid             TEXT,
    quantity          TEXT,
    market_value      TEXT,
    cost_basis        TEXT,
    unrealized_pnl    TEXT,
    source_field      TEXT,
    calculation_version TEXT NOT NULL,
    created_at        TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_ppa_key ON position_pnl_attribution
    (account_id, et_date, symbol, calculation_version);
CREATE INDEX IF NOT EXISTS idx_ppa_date ON position_pnl_attribution (et_date DESC);

-- 分币种对账结果（与旧的 cash_reconciliations 并存：那张表是单币种汇总口径）
CREATE TABLE IF NOT EXISTS currency_reconciliations (
    recon_id          TEXT NOT NULL PRIMARY KEY,
    account_id        TEXT,
    report_from       TEXT NOT NULL,
    report_to         TEXT NOT NULL,
    currency          TEXT NOT NULL,
    opening_balance   TEXT,
    closing_balance   TEXT,
    deposits          TEXT,
    withdrawals       TEXT,
    net_external_cash_flow TEXT,
    trade_cash_flow   TEXT,
    dividends         TEXT,
    interest          TEXT,
    commissions       TEXT,
    other_fees        TEXT,
    taxes             TEXT,
    other_confirmed_items TEXT,
    expected_closing_balance TEXT,
    reconciliation_difference TEXT,
    tolerance         TEXT,
    status            TEXT NOT NULL,
    issues            TEXT,
    fx_rate_to_base   TEXT,
    difference_base   TEXT,
    calculation_version TEXT NOT NULL,
    report_hash       TEXT,
    created_at        TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_cr_ccy_key ON currency_reconciliations
    (account_id, report_from, report_to, currency, calculation_version);
CREATE INDEX IF NOT EXISTS idx_cr_ccy_end ON currency_reconciliations (report_to DESC);

-- ===== 账户规则历史重放 v1 =====
-- **与实时风险系统完全隔离的四张表。** 重放结果绝不写入
-- risk_event_registry / risk_event_transitions / alert_state /
-- risk_notifications / risk_events / market_environment_events。
-- 模拟出来的事件混进真实时间线，会让「当时到底发生了什么」永远说不清。
CREATE TABLE IF NOT EXISTS risk_replay_runs (
    replay_run_id     TEXT NOT NULL PRIMARY KEY,
    param_hash        TEXT NOT NULL,     -- 相同参数可识别复用
    account_id        TEXT,
    date_start        TEXT NOT NULL,
    date_end          TEXT NOT NULL,
    sampling_mode     TEXT NOT NULL,     -- daily_close / intraday_interval
    sampling_interval_minutes INTEGER,
    requested_rules   TEXT,              -- JSON
    rule_set_mode     TEXT NOT NULL,     -- point_in_time / current_rules
    status            TEXT NOT NULL,     -- pending/running/success/partial/failed
    source_snapshot_count   INTEGER DEFAULT 0,
    eligible_snapshot_count INTEGER DEFAULT 0,
    skipped_snapshot_count  INTEGER DEFAULT 0,
    assessment_count  INTEGER DEFAULT 0,
    event_count       INTEGER DEFAULT 0,
    rule_result_count INTEGER DEFAULT 0,
    coverage_summary  TEXT,              -- JSON：身份与对齐分布
    skip_reasons      TEXT,              -- JSON：跳过原因计数
    started_at        TEXT,
    completed_at      TEXT,
    duration_ms       INTEGER,
    calculation_version TEXT NOT NULL,
    error_message     TEXT,
    created_at        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_rrr_hash ON risk_replay_runs (param_hash, status);
CREATE INDEX IF NOT EXISTS idx_rrr_created ON risk_replay_runs (created_at DESC);

-- 重放产生的评估。与 risk_assessments 结构相近但**完全独立**。
CREATE TABLE IF NOT EXISTS risk_replay_assessments (
    replay_assessment_id TEXT NOT NULL PRIMARY KEY,
    replay_run_id     TEXT NOT NULL,
    account_id        TEXT,
    data_as_of        TEXT NOT NULL,
    et_date           TEXT NOT NULL,
    snapshot_id       TEXT,              -- 原生 id，可能为空
    source_snapshot_key TEXT NOT NULL,
    source_identity_type TEXT NOT NULL,
    alignment_status  TEXT NOT NULL,
    alignment_gap_seconds INTEGER,
    positions_data_as_of TEXT,
    risk_level        TEXT,
    risk_score        TEXT,
    reliable          INTEGER NOT NULL DEFAULT 0,
    component_scores  TEXT,
    triggered_rules   TEXT,
    market_data_as_of TEXT,
    market_state      TEXT,
    market_source     TEXT,              -- stored_assessment / recomputed / none
    market_time_gap_seconds INTEGER,
    rule_set_version  TEXT,
    rule_set_mode     TEXT NOT NULL,
    data_quality      TEXT,
    created_at        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_rra_run ON risk_replay_assessments (replay_run_id, data_as_of);
CREATE INDEX IF NOT EXISTS idx_rra_key ON risk_replay_assessments (source_snapshot_key);

-- 重放 episode。独立于真实 episode，**永不触发通知**。
CREATE TABLE IF NOT EXISTS risk_replay_events (
    replay_event_id   TEXT NOT NULL PRIMARY KEY,
    replay_run_id     TEXT NOT NULL,
    identity          TEXT NOT NULL,
    episode_seq       INTEGER NOT NULL DEFAULT 1,
    rule_code         TEXT NOT NULL,
    rule_version      TEXT,
    rule_set_mode     TEXT NOT NULL,
    source_type       TEXT NOT NULL,
    category          TEXT,
    affected_symbol   TEXT,
    account_id        TEXT,
    title             TEXT,
    explanation       TEXT,
    first_detected_at TEXT NOT NULL,
    last_detected_at  TEXT NOT NULL,
    resolved_at       TEXT,
    peak_severity     TEXT,
    last_severity     TEXT,
    peak_actual_value TEXT,
    threshold_value   TEXT,
    unit              TEXT,
    occurrence_count  INTEGER NOT NULL DEFAULT 1,
    duration_minutes  INTEGER,
    source_snapshot_keys TEXT,           -- JSON 数组
    reliable          INTEGER NOT NULL DEFAULT 1,
    matched_real_event_id TEXT,          -- 与真实事件的匹配（仅分析用）
    match_lead_minutes    INTEGER,
    created_at        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_rre_run ON risk_replay_events (replay_run_id, rule_code);
CREATE INDEX IF NOT EXISTS idx_rre_identity ON risk_replay_events (identity, episode_seq);
CREATE INDEX IF NOT EXISTS idx_rre_first ON risk_replay_events (first_detected_at DESC);

-- 逐采样点的规则结果（审计用：证明某个时点确实评估过、结论是什么）
CREATE TABLE IF NOT EXISTS risk_replay_rule_results (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    replay_run_id     TEXT NOT NULL,
    replay_assessment_id TEXT NOT NULL,
    source_snapshot_key  TEXT NOT NULL,
    data_as_of        TEXT NOT NULL,
    rule_code         TEXT NOT NULL,
    triggered         INTEGER NOT NULL DEFAULT 0,
    severity          TEXT,
    actual_value      TEXT,
    threshold_value   TEXT,
    affected_symbol   TEXT,
    rule_version      TEXT,
    created_at        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_rrrr_run ON risk_replay_rule_results (replay_run_id, rule_code);
CREATE INDEX IF NOT EXISTS idx_rrrr_assess ON risk_replay_rule_results (replay_assessment_id);

-- ==================== AI 每日风险简报 v1 ====================
-- 两阶段架构：确定性上下文构建 -> AI 语言组织 -> 校验 -> 落库 -> 页面/飞书
-- **只保存简报需要的来源引用、摘要和版本，不复制完整账户原始数据。**

CREATE TABLE IF NOT EXISTS daily_brief_runs (
    run_id              TEXT PRIMARY KEY,
    brief_date          TEXT NOT NULL,
    trigger             TEXT NOT NULL,          -- scheduled / manual / regenerate
    status              TEXT NOT NULL,          -- success / partial / failed / skipped / waiting
    generation_mode     TEXT,                   -- full / limited / blocked
    generation_status   TEXT,                   -- ai_generated / template_fallback / blocked_data_brief
    brief_id            TEXT,
    reused              INTEGER NOT NULL DEFAULT 0,
    wait_attempts       INTEGER NOT NULL DEFAULT 0,
    readiness           TEXT,                   -- 数据就绪检查明细 JSON
    error               TEXT,
    duration_ms         INTEGER,
    started_at          TEXT NOT NULL,
    finished_at         TEXT
);
CREATE INDEX IF NOT EXISTS idx_brief_runs_date
    ON daily_brief_runs(brief_date DESC, started_at DESC);

CREATE TABLE IF NOT EXISTS daily_briefs (
    brief_id            TEXT PRIMARY KEY,
    brief_date          TEXT NOT NULL,
    account_id_masked   TEXT,                   -- **只存脱敏账户号**
    revision            INTEGER NOT NULL DEFAULT 1,
    generation_mode     TEXT NOT NULL,
    generation_status   TEXT NOT NULL,
    content_json        TEXT NOT NULL,
    rendered_markdown   TEXT,
    prompt_version      TEXT,
    model_identifier    TEXT,
    context_hash        TEXT NOT NULL,
    context_version     TEXT,
    validation_status   TEXT NOT NULL,          -- passed / failed / not_required
    validation_errors   TEXT,
    ai_attempts         INTEGER NOT NULL DEFAULT 0,
    account_snapshot_id TEXT,
    risk_assessment_id  TEXT,
    market_assessment_id TEXT,
    performance_period_id TEXT,
    calculation_versions TEXT,                  -- 各计算层版本 JSON
    generated_at        TEXT NOT NULL,
    delivered_at        TEXT,
    created_at          TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_briefs_date
    ON daily_briefs(brief_date DESC, revision DESC);
-- 幂等键：同日 + 同账户 + 同上下文 + 同 prompt 版本 -> 复用，不重复生成
CREATE UNIQUE INDEX IF NOT EXISTS idx_briefs_idem
    ON daily_briefs(brief_date, account_id_masked, context_hash, prompt_version);

CREATE TABLE IF NOT EXISTS daily_brief_sources (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    brief_id            TEXT NOT NULL,
    source_type         TEXT NOT NULL,          -- fact / rule_result / derived_metric / ai_explanation / limitation
    source_table        TEXT NOT NULL,
    source_id           TEXT,
    source_version      TEXT,
    data_as_of          TEXT,
    field_path          TEXT NOT NULL,
    value_hash          TEXT,                   -- **存哈希不存原值**
    created_at          TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_brief_sources_brief
    ON daily_brief_sources(brief_id);

CREATE TABLE IF NOT EXISTS daily_brief_deliveries (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    brief_id            TEXT NOT NULL,
    channel             TEXT NOT NULL,          -- feishu / ...
    status              TEXT NOT NULL,          -- sent / failed / skipped
    reason              TEXT,
    context_hash        TEXT,
    attempt             INTEGER NOT NULL DEFAULT 1,
    created_at          TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_brief_deliveries_brief
    ON daily_brief_deliveries(brief_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_brief_deliveries_hash
    ON daily_brief_deliveries(context_hash, status);

-- 调度执行审计。与 daily_brief_runs 的区别：runs 记录**每一次生成尝试**，
-- 这里记录**一次计划触发的完整生命周期**（含跨多轮等待），
-- 让「昨晚 06:30 那次到底发生了什么」有单一权威记录。
CREATE TABLE IF NOT EXISTS brief_schedule_executions (
    execution_id          TEXT PRIMARY KEY,
    brief_date            TEXT NOT NULL,
    scheduled_for         TEXT NOT NULL,        -- 计划触发时刻（本地时区）
    actual_started_at     TEXT NOT NULL,
    dependency_wait_count INTEGER NOT NULL DEFAULT 0,
    dependency_ready_status TEXT,               -- ready / degraded / not_ready
    dependency_detail     TEXT,                 -- 就绪检查明细 JSON
    brief_id              TEXT,
    generation_mode       TEXT,
    generation_status     TEXT,
    validation_status     TEXT,
    delivery_status       TEXT,
    reused                INTEGER NOT NULL DEFAULT 0,
    completed_at          TEXT,
    error_message         TEXT,
    created_at            TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sched_exec_date
    ON brief_schedule_executions(brief_date DESC, scheduled_for DESC);

-- ==================== 事件标注（追加式，永不覆盖原始记录）====================
-- 起因：2026-07-29 一次生产探测错误调用了 process_assessments，不完整的市场
-- 上下文让 7 条仍在触发的事件被判为恢复，随后正常同步又把它们重开，
-- 并误发了一条飞书告警。原始历史**不能删也不能改**——那会让「当时到底
-- 发生了什么」永远说不清。但它必须被标注，否则假的 episode 边界会把一段
-- 连续风险错算成两个独立样本，污染规则效果研究。
CREATE TABLE IF NOT EXISTS risk_event_annotations (
    annotation_id       TEXT PRIMARY KEY,
    risk_event_id       TEXT NOT NULL,
    transition_id       INTEGER,
    episode_sequence    INTEGER,
    annotation_type     TEXT NOT NULL,
    origin              TEXT NOT NULL,
    reason_code         TEXT NOT NULL,
    reason_text         TEXT NOT NULL,
    excluded_from_research           INTEGER NOT NULL DEFAULT 0,
    excluded_from_precision_metrics  INTEGER NOT NULL DEFAULT 0,
    related_notification_id TEXT,
    incident_reference  TEXT,
    evidence            TEXT,              -- 因果依据 JSON，可审计
    created_by          TEXT NOT NULL,
    created_at          TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_evt_annot_event
    ON risk_event_annotations(risk_event_id);
CREATE INDEX IF NOT EXISTS idx_evt_annot_incident
    ON risk_event_annotations(incident_reference);
-- 幂等键：同一事件 + 同一 transition + 同一事故只记一条
CREATE UNIQUE INDEX IF NOT EXISTS idx_evt_annot_idem
    ON risk_event_annotations(risk_event_id, IFNULL(transition_id, -1),
                              IFNULL(incident_reference, ''));

-- 通知的运营分类。**不改 success** —— 那条通知在技术上确实发送成功了，
-- 改成 failed 是另一种伪造。分类是叠加的事实，不是对原记录的修正。
CREATE TABLE IF NOT EXISTS notification_classifications (
    classification_id   TEXT PRIMARY KEY,
    notification_id     TEXT NOT NULL,
    classification      TEXT NOT NULL,     -- probe_induced_false_alert / ...
    reason_text         TEXT,
    incident_reference  TEXT,
    created_by          TEXT NOT NULL,
    created_at          TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_notif_class_idem
    ON notification_classifications(notification_id, classification,
                                    IFNULL(incident_reference, ''));

-- 有副作用入口的执行审计。**只读探测再也不能悄悄改状态。**
CREATE TABLE IF NOT EXISTS side_effect_executions (
    execution_id        TEXT PRIMARY KEY,
    source              TEXT NOT NULL,     -- scheduler / sync_worker / manual_api / automated_test
    correlation_id      TEXT,
    initiated_by        TEXT,
    started_at          TEXT NOT NULL,
    finished_at         TEXT,
    affected_event_count      INTEGER NOT NULL DEFAULT 0,
    notification_count        INTEGER NOT NULL DEFAULT 0,
    error               TEXT
);
CREATE INDEX IF NOT EXISTS idx_side_effect_started
    ON side_effect_executions(started_at DESC);

-- ============ 当日盈亏原子观测 ============
-- 为什么要单独一张表：`ACC_DAILY_LOSS_HIGH` 曾在 24 小时内开合 11 次，取证发现
-- 分子（daily_pnl）取自上一采集周期、分母（net_liquidation）取自本周期 ——
-- 720 对相邻样本里 496 对「净值冻结但盈亏在跳」。把分子与分母的身份**各自**
-- 落库，是让「是不是同一批」这件事永远可被事后验证的唯一办法。
CREATE TABLE IF NOT EXISTS daily_pnl_observations (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id      TEXT,
    session_date    TEXT,                 -- 美东交易日归属
    observed_at     TEXT NOT NULL,
    correlation_id  TEXT,                 -- 同周期凭证（= sync_run_id）
    source          TEXT,
    basis           TEXT,
    daily_pnl                     TEXT,   -- Decimal 分子
    daily_pnl_observed_at         TEXT,   -- 分子的独立时间戳
    net_liquidation               TEXT,   -- Decimal 分母
    net_liquidation_observed_at   TEXT,   -- 分母的独立时间戳
    numerator_snapshot_id         TEXT,
    denominator_snapshot_id       TEXT,
    observation_skew_seconds      TEXT,   -- 两侧时间差；超容差即 insufficient
    external_cash_flow_adjustment TEXT,
    daily_loss_ratio              TEXT,   -- 盈利为 0，不是 NULL
    quality_status  TEXT NOT NULL,        -- OK / DEGRADED / INSUFFICIENT
    reliable        INTEGER NOT NULL,
    quality_reasons TEXT,                 -- JSON 数组
    previous_source TEXT,
    current_source  TEXT,
    source_changed  INTEGER NOT NULL DEFAULT 0,
    basis_changed   INTEGER NOT NULL DEFAULT 0,
    calculation_version TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    UNIQUE (account_id, observed_at, calculation_version)
);
CREATE INDEX IF NOT EXISTS idx_dpo_observed ON daily_pnl_observations(observed_at DESC);
CREATE INDEX IF NOT EXISTS idx_dpo_session ON daily_pnl_observations(session_date, observed_at);

-- ============ 旧指标 vs v2 影子比较 ============
-- 影子期只记录，不驱动事件。`*_would_transition` 记的是「如果由这一侧驱动，
-- 本轮是否允许状态迁移」，用来在切换前把假打开/假恢复数清点出来。
CREATE TABLE IF NOT EXISTS daily_pnl_metric_comparisons (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    observed_at     TEXT NOT NULL,
    session_date    TEXT,
    correlation_id  TEXT,
    old_source      TEXT,
    new_source      TEXT,
    old_ratio       TEXT,
    new_ratio       TEXT,
    old_quality     TEXT,
    new_quality     TEXT,
    old_outcome     TEXT,
    new_outcome     TEXT,
    divergence_type TEXT NOT NULL,
    old_would_transition INTEGER NOT NULL DEFAULT 0,
    new_would_transition INTEGER NOT NULL DEFAULT 0,
    calculation_version  TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    UNIQUE (observed_at, calculation_version)
);
CREATE INDEX IF NOT EXISTS idx_dpmc_observed
    ON daily_pnl_metric_comparisons(observed_at DESC);
CREATE INDEX IF NOT EXISTS idx_dpmc_divergence
    ON daily_pnl_metric_comparisons(divergence_type, observed_at);

-- ============ IBKR PnL raw callback and subscription audit ============
CREATE TABLE IF NOT EXISTS ibkr_pnl_subscription_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    request_id TEXT,
    account TEXT,
    model_code TEXT,
    conid INTEGER,
    connection_generation INTEGER NOT NULL,
    subscription_generation INTEGER
);
CREATE INDEX IF NOT EXISTS idx_ipse_time
    ON ibkr_pnl_subscription_events(occurred_at DESC);

CREATE TABLE IF NOT EXISTS ibkr_pnl_callbacks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    callback_type TEXT NOT NULL,
    received_at TEXT NOT NULL,
    broker_timestamp TEXT,
    request_id TEXT,
    account TEXT,
    model_code TEXT,
    conid INTEGER,
    connection_generation INTEGER NOT NULL,
    subscription_generation INTEGER,
    session_date TEXT,
    reset_configuration TEXT,
    source TEXT NOT NULL,
    daily_pnl TEXT,
    unrealized_pnl TEXT,
    realized_pnl TEXT,
    accepted INTEGER NOT NULL,
    quality_reasons TEXT
);
CREATE INDEX IF NOT EXISTS idx_ipc_time ON ibkr_pnl_callbacks(received_at DESC);
CREATE INDEX IF NOT EXISTS idx_ipc_generation
    ON ibkr_pnl_callbacks(connection_generation, subscription_generation);

CREATE TABLE IF NOT EXISTS pnl_source_reconciliations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    observed_at TEXT NOT NULL,
    sync_run_id TEXT,
    snapshot_id TEXT,
    account TEXT,
    official_daily_pnl TEXT,
    official_realized_pnl TEXT,
    official_unrealized_pnl TEXT,
    position_daily_total TEXT,
    futu_intraday_mtm TEXT,
    futu_total_position_value TEXT,
    quote_coverage TEXT,
    official_position_status TEXT,
    futu_status TEXT,
    large_but_explained INTEGER NOT NULL DEFAULT 0,
    limitations TEXT,
    UNIQUE(sync_run_id)
);
CREATE INDEX IF NOT EXISTS idx_psr_time
    ON pnl_source_reconciliations(observed_at DESC);

CREATE TABLE IF NOT EXISTS scheduler_job_diagnostics (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT NOT NULL,
    scheduled_at TEXT,
    observed_at TEXT NOT NULL,
    outcome TEXT NOT NULL,
    lateness_seconds TEXT,
    error_type TEXT
);
CREATE INDEX IF NOT EXISTS idx_sjd_job_time
    ON scheduler_job_diagnostics(job_id, observed_at DESC);

-- ============================================================
-- X 推文数据（来源：可选的本地 X Monitor，只读挂载）
--
-- 为什么要自己落库：源库每天裁剪、只保留最近 5,000 条。
-- 它是数据管道，不是数据仓库 —— 我们必须自己持久化，否则历史会被抹掉。
-- ============================================================

CREATE TABLE IF NOT EXISTS x_tweets (
    tweet_id        TEXT PRIMARY KEY,   -- X 全局唯一，天然幂等键
    author_handle   TEXT NOT NULL,
    author_name     TEXT,
    author_user_id  TEXT,
    text            TEXT,               -- 原文
    text_zh         TEXT,               -- 中文正文（2026-07-30 起的新推文才有）
    ai_summary      TEXT,               -- AI 中文分析（正文 >300 字符才有）
    enriched_at     TEXT,               -- NULL = 无中文版
    tweet_url       TEXT,
    is_reply        INTEGER,
    is_retweet      INTEGER,
    is_quote        INTEGER,
    media_count     INTEGER,
    created_at      TEXT,               -- Twitter 原始格式，**不可字符串排序**
    created_at_utc  TEXT,               -- 我们解析后的 ISO8601，可排序
    received_at     TEXT NOT NULL,      -- 源库入库时间，增量游标用它
    source          TEXT,
    ingested_at     TEXT NOT NULL       -- 本项目摄取时间
);
-- 列表页按发布时间倒序；created_at_utc 可能为 NULL（解析失败），故并列 received_at
CREATE INDEX IF NOT EXISTS idx_x_tweets_time
    ON x_tweets(created_at_utc DESC, received_at DESC);
CREATE INDEX IF NOT EXISTS idx_x_tweets_author ON x_tweets(author_handle, received_at DESC);
CREATE INDEX IF NOT EXISTS idx_x_tweets_recv ON x_tweets(received_at);

CREATE TABLE IF NOT EXISTS x_daily_reports (
    day          TEXT PRIMARY KEY,      -- YYYY-MM-DD，可安全字符串比较
    report_json  TEXT,
    created_at   TEXT,
    ingested_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS x_authors (
    handle          TEXT PRIMARY KEY,
    user_id         TEXT,
    display_name    TEXT,
    last_checked_at TEXT,
    active          INTEGER,
    ingested_at     TEXT NOT NULL
);

-- ============================================================================
-- 建议合成层（Phase 2）：确定性规则集把技术/计划/仓位/风险/市场合成为**可追溯**
-- 的当前建议，并按「状态跃迁」落库（不是每次同步都写一行）。
--
-- 去重键是 decision_fingerprint，**不是** input_refs：POSITIONS_SYNC_INTERVAL=60s
-- 会让 snapshot_id / quote_as_of 每分钟变一次，若把易变 ID 纳入去重键，去重永不命中，
-- 4 个标的约 5,760 行/日。指纹只哈希「决策相关值」，易变 ID 仅存 input_refs 供追溯。
-- 指纹未变 → 只更新最近一行的 last_seen_at 与 input_refs；变了 → 旧行 SUPERSEDED、插新行。
-- ============================================================================
CREATE TABLE IF NOT EXISTS recommendation_snapshots (
    id                   TEXT NOT NULL PRIMARY KEY,   -- rec_...
    account              TEXT NOT NULL,
    symbol               TEXT NOT NULL,
    cycle_id             TEXT,                         -- position_cycles.id（可空）
    action               TEXT NOT NULL,                -- 受控枚举，见 recommendation_service.ACTIONS
    decided_by           TEXT NOT NULL,                -- data_quality/hard_stop/position_limit/plan/technical/market/none
    reason               TEXT,
    -- 权重与数量（quantity/amount 仅在已有来源能确定时填写，否则留空并降级 REVIEW）
    current_weight       TEXT,
    target_weight        TEXT,
    max_weight           TEXT,
    delta_weight         TEXT,
    quantity             TEXT,
    amount               TEXT,
    trigger_condition    TEXT,
    invalidation_condition TEXT,
    blocked_reason       TEXT,
    covered_action_ids   TEXT,                         -- JSON array：被硬约束覆盖的正式 action_id
    input_refs           TEXT,                         -- JSON object：snapshot/plan/action/rule/assessment/stop/bar 等 ID
    decision_fingerprint TEXT NOT NULL,                -- 去重键（只含决策值，不含易变 ID）
    rule_set_version     TEXT NOT NULL,
    rule_set_hash        TEXT NOT NULL,
    -- 行情/日线 provenance
    quote_source         TEXT,
    quote_session        TEXT,
    quote_quality        TEXT,
    quote_as_of          TEXT,
    bar_as_of            TEXT,
    adjustment_mode      TEXT,
    confirmation         TEXT,                          -- CLOSE_CONFIRMED/INTRADAY_PENDING/EXTENDED_HOURS_PENDING
    -- 生命周期
    status               TEXT NOT NULL DEFAULT 'ACTIVE',-- ACTIVE/SUPERSEDED/BLOCKED/ACKNOWLEDGED/EXPIRED
    supersedes_id        TEXT,
    created_at           TEXT NOT NULL,
    last_seen_at         TEXT NOT NULL
);
-- 「每个标的的当前建议」查询：account+symbol 上按 status 过滤取最新
CREATE INDEX IF NOT EXISTS idx_rec_current
    ON recommendation_snapshots(account, symbol, status, created_at);
-- 指纹去重命中查询
CREATE INDEX IF NOT EXISTS idx_rec_fingerprint
    ON recommendation_snapshots(account, symbol, decision_fingerprint);


-- ============================================================================
-- 机会发现与组合构建（P0）
-- 候选池由用户维护；扫描与组合提案均为不可变快照，不具备券商下单能力。
-- ============================================================================
CREATE TABLE IF NOT EXISTS opportunity_watchlist (
    symbol          TEXT NOT NULL PRIMARY KEY,
    status          TEXT NOT NULL DEFAULT 'ACTIVE', -- ACTIVE/PAUSED/ARCHIVED
    thesis          TEXT,
    why_watch       TEXT,
    sector          TEXT,
    asset_type      TEXT NOT NULL DEFAULT 'STOCK',
    exchange        TEXT,
    tags_json       TEXT,
    note            TEXT,
    source          TEXT NOT NULL DEFAULT 'MANUAL',
    created_by      TEXT NOT NULL DEFAULT 'web',
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    archived_at     TEXT,
    last_sync_status TEXT,
    last_sync_at    TEXT,
    last_sync_error TEXT
);
CREATE INDEX IF NOT EXISTS idx_ow_status ON opportunity_watchlist(status, symbol);

CREATE TABLE IF NOT EXISTS opportunity_scan_runs (
    id              TEXT NOT NULL PRIMARY KEY,
    status          TEXT NOT NULL, -- RUNNING/SUCCESS/PARTIAL/FAILED
    trigger_source  TEXT NOT NULL, -- scheduler/manual/shadow
    started_at      TEXT NOT NULL,
    finished_at     TEXT,
    universe_count  INTEGER NOT NULL DEFAULT 0,
    eligible_count  INTEGER NOT NULL DEFAULT 0,
    ready_count     INTEGER NOT NULL DEFAULT 0,
    watch_count     INTEGER NOT NULL DEFAULT 0,
    blocked_count   INTEGER NOT NULL DEFAULT 0,
    market_assessment_id TEXT,
    market_state    TEXT,
    rules_version   TEXT NOT NULL,
    rules_hash      TEXT NOT NULL,
    data_as_of      TEXT,
    error_summary   TEXT,
    duration_ms     INTEGER
);
CREATE INDEX IF NOT EXISTS idx_osr_started ON opportunity_scan_runs(started_at DESC);

CREATE TABLE IF NOT EXISTS opportunity_candidates (
    id              TEXT NOT NULL PRIMARY KEY,
    scan_id         TEXT NOT NULL,
    symbol          TEXT NOT NULL,
    setup_type      TEXT NOT NULL, -- BREAKOUT/PULLBACK/NONE
    status          TEXT NOT NULL, -- READY/WATCH/LOW_PRIORITY/BLOCKED/STALE
    score           INTEGER NOT NULL,
    grade           TEXT,
    confidence      TEXT NOT NULL, -- FULL/LIMITED/INSUFFICIENT
    current_price   TEXT,
    ema20           TEXT,
    ema50           TEXT,
    atr14           TEXT,
    rvol20          TEXT,
    entry_low       TEXT,
    entry_high      TEXT,
    stop_price      TEXT,
    risk_per_share  TEXT,
    median_dollar_volume_20d TEXT,
    relative_strength_20d TEXT,
    relative_strength_60d TEXT,
    score_components TEXT NOT NULL, -- JSON
    reasons_json    TEXT NOT NULL, -- JSON
    blocked_reasons TEXT NOT NULL, -- JSON
    input_refs      TEXT NOT NULL, -- JSON
    bar_as_of       TEXT,
    adjustment_mode TEXT,
    quality_status  TEXT NOT NULL,
    rules_version   TEXT NOT NULL,
    rules_hash      TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    effective_score  INTEGER,
    effective_status TEXT,
    chain_signal_id  TEXT,
    chain_score_adjustment INTEGER NOT NULL DEFAULT 0,
    chain_rules_version TEXT,
    chain_rules_hash TEXT,
    FOREIGN KEY (scan_id) REFERENCES opportunity_scan_runs(id),
    UNIQUE(scan_id, symbol)
);
CREATE INDEX IF NOT EXISTS idx_oc_scan_score ON opportunity_candidates(scan_id, status, score DESC);
CREATE INDEX IF NOT EXISTS idx_oc_symbol_time ON opportunity_candidates(symbol, created_at DESC);

CREATE TABLE IF NOT EXISTS portfolio_proposals (
    id              TEXT NOT NULL PRIMARY KEY,
    scan_id         TEXT NOT NULL,
    account         TEXT,
    status          TEXT NOT NULL, -- READY/BLOCKED/SUPERSEDED/ACKNOWLEDGED
    account_snapshot_id TEXT,
    market_assessment_id TEXT,
    market_state    TEXT NOT NULL,
    equity          TEXT,
    cash            TEXT,
    risk_budget_pct TEXT NOT NULL,
    risk_budget_amount TEXT,
    existing_risk_amount TEXT,
    proposed_risk_amount TEXT,
    remaining_risk_amount TEXT,
    blocked_reasons TEXT NOT NULL, -- JSON
    input_refs      TEXT NOT NULL, -- JSON
    rules_version   TEXT NOT NULL,
    rules_hash      TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    FOREIGN KEY (scan_id) REFERENCES opportunity_scan_runs(id)
);
CREATE INDEX IF NOT EXISTS idx_pp_created ON portfolio_proposals(created_at DESC);

CREATE TABLE IF NOT EXISTS portfolio_proposal_items (
    id              TEXT NOT NULL PRIMARY KEY,
    proposal_id     TEXT NOT NULL,
    symbol          TEXT NOT NULL,
    item_type       TEXT NOT NULL, -- CURRENT/ADD/REVIEW/REDUCE/BLOCKED
    setup_type      TEXT,
    candidate_id    TEXT,
    score           INTEGER,
    current_quantity TEXT,
    proposed_quantity TEXT,
    current_weight  TEXT,
    proposed_weight TEXT,
    entry_low       TEXT,
    entry_high      TEXT,
    stop_price      TEXT,
    risk_amount     TEXT,
    risk_pct        TEXT,
    sector          TEXT,
    reason          TEXT,
    blocked_reason  TEXT,
    input_refs      TEXT NOT NULL,
    adoption_status TEXT NOT NULL DEFAULT 'PENDING', -- PENDING/ADOPTED/SKIPPED
    adopted_cycle_id TEXT,
    adopted_at      TEXT,
    created_at      TEXT NOT NULL,
    FOREIGN KEY (proposal_id) REFERENCES portfolio_proposals(id),
    FOREIGN KEY (candidate_id) REFERENCES opportunity_candidates(id),
    UNIQUE(proposal_id, symbol)
);
CREATE INDEX IF NOT EXISTS idx_ppi_proposal ON portfolio_proposal_items(proposal_id, item_type, score DESC);


CREATE TABLE IF NOT EXISTS opportunity_outcomes (
    id              TEXT NOT NULL PRIMARY KEY,
    candidate_id    TEXT NOT NULL,
    horizon_days    INTEGER NOT NULL, -- 5/10/20/40 个交易日
    outcome_date    TEXT NOT NULL,
    candidate_price TEXT NOT NULL,
    outcome_price   TEXT,
    spy_start_price TEXT,
    spy_end_price   TEXT,
    return_pct      TEXT,
    spy_return_pct  TEXT,
    alpha_pct       TEXT,
    mfe_pct         TEXT,
    mae_pct         TEXT,
    r_multiple      TEXT,
    status          TEXT NOT NULL, -- PENDING/COMPLETE/BLOCKED
    quality_note    TEXT,
    calculated_at   TEXT NOT NULL,
    calculation_version TEXT NOT NULL,
    FOREIGN KEY (candidate_id) REFERENCES opportunity_candidates(id),
    UNIQUE(candidate_id, horizon_days)
);
CREATE INDEX IF NOT EXISTS idx_oo_candidate ON opportunity_outcomes(candidate_id, horizon_days);

-- Hyperliquid 与现实正股结果（链上库本身保持独立；这里不建立跨库 FK）
CREATE TABLE IF NOT EXISTS hl_asset_mappings (
    coin TEXT PRIMARY KEY,
    canonical_symbol TEXT NOT NULL,
    conid INTEGER,
    sec_type TEXT,
    stock_type TEXT,
    primary_exchange TEXT,
    currency TEXT,
    status TEXT NOT NULL, -- PENDING/READY/SUSPECT/BLOCKED
    reason TEXT,
    max_rth_deviation_ratio TEXT,
    checked_at TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'IBKR'
);
CREATE INDEX IF NOT EXISTS idx_hl_asset_mappings_status ON hl_asset_mappings(status, canonical_symbol);

CREATE TABLE IF NOT EXISTS hl_wallet_controls (
    address TEXT PRIMARY KEY,
    mode TEXT NOT NULL, -- PINNED/AUTO/EXCLUDED
    label TEXT,
    note TEXT,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS equity_bars_5m (
    symbol TEXT NOT NULL,
    bar_start TEXT NOT NULL,
    bar_end TEXT,
    o TEXT,
    h TEXT,
    l TEXT,
    c TEXT,
    v TEXT,
    session TEXT,
    use_rth INTEGER NOT NULL DEFAULT 1,
    source TEXT NOT NULL,
    fetched_at TEXT NOT NULL,
    data_origin TEXT NOT NULL DEFAULT 'LIVE',
    PRIMARY KEY (symbol, bar_start, source)
);
CREATE INDEX IF NOT EXISTS idx_equity_bars_5m_time ON equity_bars_5m(symbol, bar_start);

CREATE TABLE IF NOT EXISTS hl_signal_outcomes (
    id TEXT PRIMARY KEY,
    signal_id TEXT NOT NULL,
    signal_snapshot_hash TEXT NOT NULL,
    model TEXT NOT NULL,
    regime TEXT NOT NULL,
    direction TEXT NOT NULL,
    canonical_symbol TEXT NOT NULL,
    actionable_at TEXT,
    horizon TEXT NOT NULL,
    entry_time TEXT,
    entry_price TEXT,
    outcome_time TEXT,
    outcome_price TEXT,
    spy_start_price TEXT,
    spy_end_price TEXT,
    return_pct TEXT,
    spy_return_pct TEXT,
    alpha_pct TEXT,
    mfe_pct TEXT,
    mae_pct TEXT,
    status TEXT NOT NULL, -- PENDING/COMPLETE/BLOCKED
    quality_note TEXT,
    calculation_version TEXT NOT NULL,
    calculated_at TEXT NOT NULL,
    data_origin TEXT NOT NULL DEFAULT 'LIVE',
    validation_commitment_id TEXT,
    UNIQUE(signal_id, horizon, calculation_version)
);
CREATE INDEX IF NOT EXISTS idx_hl_signal_outcomes_symbol ON hl_signal_outcomes(canonical_symbol, horizon, status);

CREATE TABLE IF NOT EXISTS hl_validation_runs (
    run_id TEXT PRIMARY KEY,
    model TEXT NOT NULL,
    rules_version TEXT NOT NULL,
    rules_hash TEXT NOT NULL,
    policy_version TEXT,
    policy_hash TEXT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL,
    sample_json TEXT NOT NULL,
    verdict TEXT,
    note TEXT,
    validation_policy_version TEXT NOT NULL DEFAULT 'validation-policy-v1',
    sample_origin TEXT NOT NULL DEFAULT 'PROSPECTIVE_CONFIRMATION',
    commitment_id TEXT
);

CREATE TABLE IF NOT EXISTS hl_validation_commitments (
    commitment_id TEXT PRIMARY KEY,
    validation_policy_version TEXT NOT NULL,
    model TEXT NOT NULL,
    rules_version TEXT NOT NULL,
    rules_hash TEXT NOT NULL,
    policy_version TEXT,
    policy_hash TEXT,
    development_start TEXT,
    development_end TEXT,
    validation_start TEXT,
    validation_end TEXT,
    locked_test_start TEXT NOT NULL,
    locked_test_end TEXT NOT NULL,
    feature_manifest_json TEXT NOT NULL,
    gate_json TEXT NOT NULL,
    commitment_hash TEXT NOT NULL UNIQUE,
    committed_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'COMMITTED'
);
CREATE INDEX IF NOT EXISTS idx_hl_validation_commitments_model
    ON hl_validation_commitments(model, committed_at DESC);

CREATE TABLE IF NOT EXISTS hl_validation_results (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    comparison_group TEXT NOT NULL,
    horizon TEXT NOT NULL,
    symbol TEXT,
    direction TEXT,
    raw_n INTEGER NOT NULL DEFAULT 0,
    effective_n INTEGER NOT NULL DEFAULT 0,
    return_pct TEXT,
    alpha_pct TEXT,
    profit_factor TEXT,
    mae_pct TEXT,
    max_drawdown_pct TEXT,
    ci_low TEXT,
    ci_high TEXT,
    metrics_json TEXT NOT NULL,
    FOREIGN KEY (run_id) REFERENCES hl_validation_runs(run_id)
);

CREATE TABLE IF NOT EXISTS hl_model_promotions (
    model TEXT NOT NULL,
    rules_version TEXT NOT NULL,
    rules_hash TEXT NOT NULL,
    policy_version TEXT,
    policy_hash TEXT,
    state TEXT NOT NULL, -- SHADOW/ELIGIBLE/ACTIVE/SUSPENDED
    evaluation_run_id TEXT,
    approved_by TEXT,
    approved_at TEXT,
    reason TEXT,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (model, rules_version, rules_hash, policy_hash)
);

CREATE TABLE IF NOT EXISTS hl_health_reports (
    report_month TEXT PRIMARY KEY,
    generated_at TEXT NOT NULL,
    status TEXT NOT NULL,
    metrics_json TEXT NOT NULL,
    note TEXT
);

CREATE TABLE IF NOT EXISTS hl_alert_dispatches (
    id TEXT PRIMARY KEY,
    signal_id TEXT NOT NULL,
    canonical_symbol TEXT NOT NULL,
    dedupe_key TEXT NOT NULL,
    attempted_at TEXT NOT NULL,
    sent_at TEXT,
    success INTEGER NOT NULL DEFAULT 0,
    error_code TEXT,
    UNIQUE(dedupe_key)
);
