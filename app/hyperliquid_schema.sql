PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;
PRAGMA busy_timeout = 30000;

CREATE TABLE IF NOT EXISTS hl_assets (
    coin TEXT PRIMARY KEY,
    dex TEXT NOT NULL,
    symbol TEXT NOT NULL,
    category TEXT,
    is_delisted INTEGER NOT NULL DEFAULT 0,
    max_leverage TEXT,
    sz_decimals INTEGER,
    metadata_json TEXT NOT NULL,
    data_as_of TEXT NOT NULL,
    fetched_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_hl_assets_symbol ON hl_assets(symbol);

CREATE TABLE IF NOT EXISTS hl_collection_runs (
    run_id TEXT PRIMARY KEY,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL,
    endpoint TEXT,
    rows_seen INTEGER NOT NULL DEFAULT 0,
    weight_used TEXT,
    error_code TEXT,
    error_message TEXT
);

CREATE TABLE IF NOT EXISTS hl_backfill_runs (
    run_id TEXT PRIMARY KEY,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL,
    requested_start TEXT NOT NULL,
    requested_end TEXT NOT NULL,
    data_origin TEXT NOT NULL DEFAULT 'HISTORICAL_BACKFILL',
    scope_json TEXT NOT NULL,
    rows_written INTEGER NOT NULL DEFAULT 0,
    incomplete_items INTEGER NOT NULL DEFAULT 0,
    error_code TEXT,
    error_message TEXT
);

CREATE TABLE IF NOT EXISTS hl_backfill_items (
    run_id TEXT NOT NULL,
    item_type TEXT NOT NULL,
    item_key TEXT NOT NULL,
    status TEXT NOT NULL,
    cursor_value TEXT,
    rows_written INTEGER NOT NULL DEFAULT 0,
    gap_reason TEXT,
    details_json TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (run_id, item_type, item_key),
    FOREIGN KEY (run_id) REFERENCES hl_backfill_runs(run_id)
);
CREATE INDEX IF NOT EXISTS idx_hl_backfill_items_status
    ON hl_backfill_items(run_id, status, item_type);

CREATE TABLE IF NOT EXISTS hl_data_quality_reports (
    report_id TEXT PRIMARY KEY,
    generated_at TEXT NOT NULL,
    requested_start TEXT NOT NULL,
    requested_end TEXT NOT NULL,
    report_version TEXT NOT NULL,
    status TEXT NOT NULL,
    summary_json TEXT NOT NULL,
    checks_json TEXT NOT NULL,
    report_path TEXT
);
CREATE INDEX IF NOT EXISTS idx_hl_data_quality_reports_time
    ON hl_data_quality_reports(generated_at DESC);

CREATE TABLE IF NOT EXISTS hl_data_gaps (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    coin TEXT,
    started_at TEXT NOT NULL,
    ended_at TEXT,
    reason TEXT NOT NULL,
    status TEXT NOT NULL,
    details_json TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_hl_gaps_coin_time ON hl_data_gaps(coin, started_at);

CREATE TABLE IF NOT EXISTS hl_cursors (
    scope TEXT PRIMARY KEY,
    cursor_value TEXT,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS hl_recent_trades (
    coin TEXT NOT NULL,
    block_time TEXT NOT NULL,
    tid TEXT NOT NULL,
    side TEXT,
    px TEXT,
    sz TEXT,
    buyer TEXT,
    seller TEXT,
    received_at TEXT NOT NULL,
    raw_json TEXT NOT NULL,
    data_origin TEXT NOT NULL DEFAULT 'LIVE',
    PRIMARY KEY (coin, block_time, tid)
);
CREATE INDEX IF NOT EXISTS idx_hl_recent_trades_received ON hl_recent_trades(received_at);

CREATE TABLE IF NOT EXISTS hl_trade_buckets_5m (
    coin TEXT NOT NULL,
    bucket_start TEXT NOT NULL,
    notional TEXT,
    buy_notional TEXT,
    sell_notional TEXT,
    trade_count INTEGER NOT NULL DEFAULT 0,
    raw_imbalance TEXT,
    cleaned_imbalance TEXT,
    liquidity_coverage TEXT,
    quality_status TEXT NOT NULL DEFAULT 'READY',
    data_as_of TEXT NOT NULL,
    data_origin TEXT NOT NULL DEFAULT 'LIVE',
    PRIMARY KEY (coin, bucket_start)
);

CREATE TABLE IF NOT EXISTS hl_address_flow_buckets_5m (
    aggressor_address TEXT NOT NULL,
    coin TEXT NOT NULL,
    bucket_start TEXT NOT NULL,
    buy_notional TEXT,
    sell_notional TEXT,
    trade_count INTEGER NOT NULL DEFAULT 0,
    classification_version TEXT,
    event_time TEXT NOT NULL,
    ingested_at TEXT NOT NULL,
    data_origin TEXT NOT NULL DEFAULT 'LIVE',
    PRIMARY KEY (aggressor_address, coin, bucket_start, classification_version)
);
CREATE INDEX IF NOT EXISTS idx_hl_address_flow_coin_time
    ON hl_address_flow_buckets_5m(coin, bucket_start);

CREATE TABLE IF NOT EXISTS hl_asset_context_buckets_5m (
    coin TEXT NOT NULL,
    bucket_start TEXT NOT NULL,
    mark_px TEXT,
    oracle_px TEXT,
    mid_px TEXT,
    impact_bid_px TEXT,
    impact_ask_px TEXT,
    funding TEXT,
    funding_premium_index TEXT,
    open_interest TEXT,
    oi_notional TEXT,
    day_ntl_vlm TEXT,
    day_base_vlm TEXT,
    mark_oracle_deviation TEXT,
    off_rth_oracle_return TEXT,
    off_rth_basis_change TEXT,
    session TEXT NOT NULL DEFAULT 'UNKNOWN',
    data_as_of TEXT NOT NULL,
    fetched_at TEXT NOT NULL,
    quality_status TEXT NOT NULL DEFAULT 'READY',
    data_origin TEXT NOT NULL DEFAULT 'LIVE',
    PRIMARY KEY (coin, bucket_start)
);

CREATE TABLE IF NOT EXISTS hl_market_candles (
    coin TEXT NOT NULL,
    interval TEXT NOT NULL,
    open_time TEXT NOT NULL,
    close_time TEXT,
    open_px TEXT,
    high_px TEXT,
    low_px TEXT,
    close_px TEXT,
    volume TEXT,
    trade_count INTEGER,
    raw_json TEXT NOT NULL,
    ingested_at TEXT NOT NULL,
    data_origin TEXT NOT NULL DEFAULT 'HISTORICAL_BACKFILL',
    PRIMARY KEY (coin, interval, open_time)
);
CREATE INDEX IF NOT EXISTS idx_hl_market_candles_time
    ON hl_market_candles(coin, open_time);

-- 官方 fundingHistory 可回溯 funding rate 与 impact-price premium index；
-- 它不包含 mark/oracle/OI，绝不能写入 asset context 表或冒充完整ctx。
CREATE TABLE IF NOT EXISTS hl_market_funding_history (
    coin TEXT NOT NULL,
    event_time TEXT NOT NULL,
    event_time_ms INTEGER NOT NULL,
    funding_rate TEXT,
    funding_premium_index TEXT,
    raw_json TEXT NOT NULL,
    ingested_at TEXT NOT NULL,
    data_origin TEXT NOT NULL DEFAULT 'HISTORICAL_BACKFILL',
    PRIMARY KEY (coin, event_time_ms)
);
CREATE INDEX IF NOT EXISTS idx_hl_market_funding_history_time
    ON hl_market_funding_history(event_time);

CREATE TABLE IF NOT EXISTS hl_wallet_candidates (
    address TEXT PRIMARY KEY,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    xyz_notional_90d TEXT,
    active_days INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'CANDIDATE',
    discovered_from TEXT
);

CREATE TABLE IF NOT EXISTS hl_wallet_fills (
    address TEXT NOT NULL,
    coin TEXT NOT NULL,
    event_time TEXT NOT NULL,
    tid TEXT NOT NULL,
    px TEXT NOT NULL,
    sz TEXT NOT NULL,
    side TEXT,
    direction TEXT,
    start_position TEXT,
    closed_pnl TEXT,
    fee TEXT,
    crossed INTEGER,
    dex TEXT NOT NULL,
    raw_json TEXT NOT NULL,
    ingested_at TEXT NOT NULL,
    data_origin TEXT NOT NULL DEFAULT 'LIVE',
    PRIMARY KEY (address, coin, event_time, tid)
);
CREATE INDEX IF NOT EXISTS idx_hl_wallet_fills_time ON hl_wallet_fills(address, event_time);
CREATE INDEX IF NOT EXISTS idx_hl_wallet_fills_coin ON hl_wallet_fills(coin, event_time);

CREATE TABLE IF NOT EXISTS hl_wallet_funding (
    address TEXT NOT NULL,
    event_time TEXT NOT NULL,
    coin TEXT NOT NULL,
    usdc TEXT,
    signed_position TEXT,
    funding_rate TEXT,
    raw_json TEXT NOT NULL,
    ingested_at TEXT NOT NULL,
    data_origin TEXT NOT NULL DEFAULT 'LIVE',
    PRIMARY KEY (address, event_time, coin)
);

CREATE TABLE IF NOT EXISTS hl_wallet_ledger_updates (
    address TEXT NOT NULL,
    event_time TEXT NOT NULL,
    tx_hash TEXT NOT NULL,
    update_type TEXT,
    coin TEXT,
    usdc TEXT,
    raw_json TEXT NOT NULL,
    ingested_at TEXT NOT NULL,
    data_origin TEXT NOT NULL DEFAULT 'LIVE',
    PRIMARY KEY (address, event_time, tx_hash)
);

CREATE TABLE IF NOT EXISTS hl_wallet_equity_snapshots (
    address TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    account_value TEXT,
    xyz_notional TEXT,
    xyz_unrealized_pnl TEXT,
    source TEXT NOT NULL,
    quality_status TEXT NOT NULL,
    raw_json TEXT,
    data_origin TEXT NOT NULL DEFAULT 'LIVE',
    PRIMARY KEY (address, observed_at, source)
);

CREATE TABLE IF NOT EXISTS hl_wallet_positions (
    address TEXT NOT NULL,
    coin TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    signed_size TEXT,
    entry_px TEXT,
    position_value TEXT,
    unrealized_pnl TEXT,
    source TEXT NOT NULL,
    raw_json TEXT NOT NULL,
    data_origin TEXT NOT NULL DEFAULT 'LIVE',
    PRIMARY KEY (address, coin, observed_at, source)
);

CREATE TABLE IF NOT EXISTS hl_wallet_cycles (
    cycle_id TEXT PRIMARY KEY,
    address TEXT NOT NULL,
    coin TEXT NOT NULL,
    opened_at TEXT NOT NULL,
    closed_at TEXT,
    side TEXT,
    status TEXT NOT NULL,
    left_censored INTEGER NOT NULL DEFAULT 0,
    gross_closed_pnl TEXT,
    fees TEXT,
    funding_pnl TEXT,
    net_pnl TEXT,
    closed_notional TEXT,
    mark_to_market_pnl TEXT,
    raw_refs_json TEXT
    ,data_origin TEXT NOT NULL DEFAULT 'LIVE'
);
CREATE INDEX IF NOT EXISTS idx_hl_wallet_cycles_score ON hl_wallet_cycles(address, closed_at);

CREATE TABLE IF NOT EXISTS hl_wallet_daily_metrics (
    address TEXT NOT NULL,
    metric_date TEXT NOT NULL,
    model TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 0,
    completed_cycles INTEGER NOT NULL DEFAULT 0,
    net_pnl TEXT,
    closed_notional TEXT,
    equity_return TEXT,
    drawdown TEXT,
    long_pnl TEXT,
    short_pnl TEXT,
    data_coverage TEXT,
    quality_status TEXT NOT NULL,
    data_origin TEXT NOT NULL DEFAULT 'LIVE',
    PRIMARY KEY (address, metric_date, model)
);

CREATE TABLE IF NOT EXISTS hl_wallet_classification_intervals (
    address TEXT NOT NULL,
    classification TEXT NOT NULL,
    valid_from TEXT NOT NULL,
    valid_to TEXT,
    version TEXT NOT NULL,
    evidence_json TEXT NOT NULL,
    data_origin TEXT NOT NULL DEFAULT 'LIVE',
    PRIMARY KEY (address, valid_from, version)
);

CREATE TABLE IF NOT EXISTS hl_wallet_memberships (
    address TEXT NOT NULL,
    model TEXT NOT NULL,
    valid_from TEXT NOT NULL,
    valid_to TEXT,
    admitted_at TEXT NOT NULL,
    weight TEXT,
    quality_score TEXT,
    sample_confidence TEXT,
    data_coverage TEXT,
    group_version TEXT NOT NULL,
    data_origin TEXT NOT NULL DEFAULT 'LIVE',
    PRIMARY KEY (address, model, valid_from, group_version)
);

CREATE TABLE IF NOT EXISTS hl_wallet_group_versions (
    group_version TEXT PRIMARY KEY,
    model TEXT NOT NULL,
    effective_at TEXT NOT NULL,
    rules_version TEXT NOT NULL,
    members_json TEXT NOT NULL,
    created_at TEXT NOT NULL
    ,data_origin TEXT NOT NULL DEFAULT 'LIVE'
);

CREATE TABLE IF NOT EXISTS hl_wallet_scores (
    address TEXT NOT NULL,
    model TEXT NOT NULL,
    as_of TEXT NOT NULL,
    rules_version TEXT NOT NULL,
    quality_score TEXT,
    sample_confidence TEXT,
    data_coverage TEXT,
    weight TEXT,
    metrics_json TEXT NOT NULL,
    data_origin TEXT NOT NULL DEFAULT 'LIVE',
    PRIMARY KEY (address, model, as_of, rules_version)
);

CREATE TABLE IF NOT EXISTS hl_signal_features_5m (
    coin TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    regime TEXT NOT NULL,
    feature_json TEXT NOT NULL,
    quality_status TEXT NOT NULL,
    data_as_of TEXT NOT NULL,
    data_origin TEXT NOT NULL DEFAULT 'LIVE',
    PRIMARY KEY (coin, observed_at, regime)
);

CREATE TABLE IF NOT EXISTS hl_signal_features_daily (
    coin TEXT NOT NULL,
    observed_date TEXT NOT NULL,
    feature_json TEXT NOT NULL,
    quality_status TEXT NOT NULL,
    data_as_of TEXT NOT NULL,
    data_origin TEXT NOT NULL DEFAULT 'LIVE',
    PRIMARY KEY (coin, observed_date)
);

CREATE TABLE IF NOT EXISTS hl_signal_episodes (
    signal_id TEXT PRIMARY KEY,
    model TEXT NOT NULL,
    regime TEXT NOT NULL,
    session TEXT NOT NULL,
    coin TEXT NOT NULL,
    canonical_symbol TEXT,
    state TEXT NOT NULL,
    score TEXT,
    confidence TEXT,
    signal_at TEXT NOT NULL,
    actionable_at TEXT,
    valid_until TEXT,
    component_json TEXT NOT NULL,
    diagnostic_json TEXT NOT NULL,
    wallet_group_version TEXT,
    quality_status TEXT NOT NULL,
    quality_reasons_json TEXT NOT NULL,
    input_refs_json TEXT NOT NULL,
    rules_version TEXT NOT NULL,
    rules_hash TEXT NOT NULL,
    policy_version TEXT,
    policy_hash TEXT,
    last_seen_at TEXT NOT NULL,
    data_origin TEXT NOT NULL DEFAULT 'LIVE',
    validation_commitment_id TEXT
);
CREATE INDEX IF NOT EXISTS idx_hl_signal_episodes_coin_time
    ON hl_signal_episodes(coin, signal_at DESC);
