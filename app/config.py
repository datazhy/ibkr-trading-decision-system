"""集中配置：从 .env 读取，全部走这里，不在别处直接读环境变量。"""
from __future__ import annotations

import os
from decimal import Decimal
from dataclasses import dataclass, field
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv


def _get(name: str, default: str | None = None) -> str | None:
    val = os.environ.get(name, default)
    if val is not None:
        val = val.strip()
    return val or default


def _get_required(name: str) -> str:
    val = _get(name)
    if not val:
        raise ConfigError(f"缺少必需配置项: {name}（请检查 .env）")
    return val


def _flag(name: str, default: bool) -> bool:
    """布尔配置项。未设置时用默认值；显式的 0/false/no 才关闭。"""
    raw = _get(name)
    if raw is None:
        return default
    return raw.strip().lower() not in ("0", "false", "no", "off")


def _get_bool(name: str, default: bool) -> bool:
    v = _get(name, None)
    if v is None or str(v).strip() == "":
        return default
    return str(v).strip().lower() in ("1", "true", "yes", "on")


def _get_int(name: str, default: int) -> int:
    raw = _get(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"配置项 {name} 必须是整数，当前值: {raw!r}") from exc


def _get_hours_list(name: str, default: str) -> list[int]:
    raw = _get(name, default) or default
    return sorted({int(x.strip()) for x in raw.split(",") if x.strip()}, reverse=True)


def _get_list(name: str, default: str = "") -> list[str]:
    raw = _get(name, default) or default
    return [x.strip() for x in raw.split(",") if x.strip()]


class ConfigError(RuntimeError):
    """配置缺失或非法。"""


@dataclass(frozen=True)
class Config:
    # 加密（账户号等敏感字段落库前加密）
    account_enc_key: str

    # 路径
    db_path: Path
    log_path: Path
    sector_map_path: Path

    # 时区
    market_tz: ZoneInfo
    local_tz: ZoneInfo

    # 同步间隔（秒）
    positions_sync_interval: int
    orders_sync_interval: int


    # 重授权提醒阈值（小时，降序）

    # 通知
    apprise_urls: list[str] = field(default_factory=list)

    log_level: str = "INFO"

    # IBKR（连接 compose 内的 ib-gateway 容器）
    ib_host: str = "ib-gateway"
    ib_port: int = 4004          # gnzsnz 镜像: 4003=live / 4004=paper
    ib_client_id: int = 17
    ib_account: str | None = None  # 多账户时指定（如 U1234567），单账户可留空
    ib_quote_timeout: float = 8.0  # 保留（当前未用）
    ib_quote_wait: float = 11.0    # reqMktData 后固定等待秒数（延迟数据到达约需 10s+）
    ib_market_data_type: int = 3   # 1=实时 2=冻结 3=延迟 4=延迟冻结
    #: 实时取不到时的回退类型。
    #: IBKR 的免费「非综合实时」（Cboe One + IEX）需要在 Client Portal 为
    #: **网关登录的那个用户名**开通订阅并签 Market Data API 协议。未生效时
    #: 请求 type=1 不会报异常，只会静静地一个价都拿不到 —— 那比延迟价更糟。
    #: 所以永远配一个回退，让「订阅还没生效」退化成「和以前一样」而不是「瞎掉」。
    ib_market_data_type_fallback: int = 3
    #: 实时探测失败后的冷却分钟数。订阅未生效时不必每轮都白等一个 quote_wait，
    #: 但也不能永久放弃 —— 订阅生效后要能自动接上。
    ib_realtime_probe_cooldown_min: int = 30
    # 富途仅作为持仓价格主源；失败/过期时逐标的回退 IBKR。
    # 持仓、账户、订单、成交与 PnL 不经过富途。
    futu_quote_enabled: bool = False
    futu_host: str = "127.0.0.1"
    futu_port: int = 11111
    futu_connect_timeout: float = 1.5
    futu_quote_stale_seconds: int = 300
    # 长连接实时层：事件每秒发布一次当前态，5 秒生成可审计快照，60 秒主动校准。
    # 旧的分钟轮询仍保留为独立兜底；实时层故障不会删除上一份有效快照。
    live_sync_enabled: bool = False
    live_publish_interval: float = 1.0
    live_flush_interval: float = 5.0
    live_reconcile_interval: float = 60.0
    # executions 专用连接：IBKR 仅 clientId=0 能收到 TWS/手机手动下单的订单与成交
    ib_exec_client_id: int = 0
    exec_sync_interval: int = 45   # 盘中 executions 增量同步间隔（秒，30–60）
    ib_req_timeout: float = 20.0   # IBKR 阻塞请求超时（req*End 事件可能永不到，必须兜底）
    ib_commission_wait: float = 3.0  # reqExecutions 后等 commissionReport 异步事件的秒数
    ib_pnl_wait: float = 4.0       # reqPnL 后等 PnL 事件回填的秒数
    # 告警「显著恶化」阈值：比例类指标的绝对变化（5个百分点）/ 相对变化（20%）
    alert_worsen_abs: Decimal = Decimal("0.05")
    alert_worsen_rel: Decimal = Decimal("0.20")
    #: 告警解除后多久内再次触发不重复推送（分钟）
    alert_retrigger_cooldown_min: int = 30

    # ---- 数据质量层阈值（全部可经环境变量覆盖，不在规则里硬编码）----
    #: 交易时段/休市时段的数据过期阈值（秒）—— 休市本就低频更新，同一标准必然误报
    dq_stale_seconds_market: int = 300
    dq_stale_seconds_closed: int = 7200
    #: 超过此秒数视为严重过期：整体判 error 并禁止 AI 分析
    dq_error_seconds: int = 86400
    #: 持仓明细合计 vs 账户持仓总市值的相对容差
    dq_positions_value_rel_tol: str = "0.02"
    #: 净资产 ≈ 现金 + 持仓市值 的相对容差（未结算资金/多币种折算留缓冲）
    dq_equity_identity_rel_tol: str = "0.05"
    #: 相邻快照净资产跳变告警阈值
    dq_equity_jump_rel: str = "0.20"
    #: 当日盈亏占净资产超过该比例告警
    dq_daily_pnl_rel_warn: str = "0.20"
    #: 单只持仓市值/净资产超过该比例且无融资迹象时告警
    dq_single_position_over_equity: str = "1.00"
    #: 手动触发同步的最小间隔（秒），防止短时间重复触发
    manual_sync_min_interval: int = 30
    #: 同步任务被判定为僵死的秒数（超过则不再视为「正在运行」）
    sync_stale_running_seconds: int = 600
    #: 同步的额外重试次数（0=只试一次）与退避基数秒。只对瞬时网络故障生效
    sync_max_retries: int = 2
    sync_retry_base_delay: float = 1.0

    # ---- 风险引擎阈值（全部可经环境变量覆盖）----
    # 与 rules.RuleThresholds 对齐：单标的 30%（R1）
    risk_single_weight_equity_max: str = "0.30"
    risk_single_weight_invested_max: str = "0.50"
    risk_top3_invested_max: str = "0.80"
    risk_top5_invested_max: str = "0.95"
    risk_unknown_sector_max: str = "0.30"
    risk_cash_ratio_min: str = "0.05"
    risk_gross_leverage_max: str = "2.0"
    risk_net_leverage_max: str = "1.5"
    risk_maint_safety_min: str = "2.0"
    risk_excess_liquidity_min: str = "0.10"
    risk_daily_loss_max: str = "0.03"
    risk_drawdown_max: str = "0.15"
    risk_max_drawdown_max: str = "0.25"
    risk_position_loss_max: str = "0.20"
    risk_loss_contribution_max: str = "0.60"
    # ---- 当日盈亏原子观测（分子分母同周期配对）----
    # 分子（daily_pnl）与分母（net_liquidation）允许的观测时刻偏差。
    # 同步周期约 60s，容差取 90s：同一批次内的正常抖动能过，跨批次拼接过不了。
    daily_pnl_skew_tolerance_seconds: int = 90
    daily_pnl_max_age_seconds: int = 600
    #: v2 是否接管 ACC_DAILY_LOSS_HIGH 与质量下传。**默认关** ——
    #: 影子期先跑一个完整交易日，拿到对比报告并获得授权后才允许打开。
    daily_pnl_v2_enabled: bool = False
    # 评分权重
    # ---- 集中度规则适用条件（降噪）----
    # Top-N 在少持仓账户上恒真，不是风险信号而是算术必然。
    # ---- 隔夜行情 ----
    # 美股隔夜成交在独立场内。实测 SMART 在 00:00–04:00 ET 只回收盘价，
    # 需要定向到隔夜场才拿得到真实报价。出问题可关掉，回到旧行为。
    ib_overnight_quotes: bool = True
    ib_overnight_exchange: str = "OVERNIGHT"

    # ---- AI 每日风险简报 ----
    # 没配 Key 时 provider 为 None，简报走确定性模板 —— **这不是错误状态**。
    ai_api_key: str = ""
    ai_base_url: str = "https://api.openai.com/v1"
    ai_model: str = "gpt-5"
    ai_max_tokens: int = 16000
    ai_timeout_seconds: int = 120
    brief_enabled: bool = True
    brief_hour_local: int = 6          # 本地时间，美股收盘且同步完成之后
    brief_minute_local: int = 30

    risk_top3_min_position_count: int = 6
    risk_top5_min_position_count: int = 10
    risk_hhi_threshold: str = "0.40"
    risk_effective_position_count_min: str = "3"
    risk_hhi_min_positions: int = 2
    risk_effective_min_positions: int = 2
    risk_dominance_gap_min: str = "0.20"

    risk_weight_concentration: str = "0.25"
    risk_weight_leverage: str = "0.20"
    risk_weight_drawdown: str = "0.20"
    risk_weight_liquidity: str = "0.20"
    risk_weight_loss: str = "0.10"
    risk_weight_data_quality: str = "0.05"

    # ---- 持仓日线（ATR/止损/技术面用） ----
    # 覆盖约 400 个交易日：EMA250 含预热、52 周高点（252 根）都要足够历史。
    # 每次全量重拉整段并 REPLACE，拆股后历史价格自动一致，无需复权标记列。
    daily_bars_lookback_days: int = 600

    # ---- 机会发现与组合构建 ----
    opportunity_enabled: bool = False
    portfolio_builder_enabled: bool = False
    # 只有完成这些个不同交易日的成功、非空扫描，才允许组合构建/采纳。
    # 0 仅供测试或明确跳过影子期的受控环境使用。
    opportunity_shadow_min_days: int = 5
    opportunity_max_active_symbols: int = 100
    opportunity_manual_scan_cooldown_seconds: int = 600
    opportunity_min_price: str = "5"
    opportunity_min_median_dollar_volume: str = "20000000"
    opportunity_min_bars: int = 120
    opportunity_full_bars: int = 252
    opportunity_ready_score: int = 75
    opportunity_watch_score: int = 60
    opportunity_risk_per_trade: str = "0.0075"
    opportunity_target_position_max: str = "0.20"
    opportunity_sector_max: str = "0.35"
    opportunity_max_positions: int = 8

    # ---- Hyperliquid XYZ 链上情报（默认完全关闭，影子期也不影响旧链路）----
    hyperliquid_enabled: bool = False
    hl_dex: str = "xyz"
    hl_db_path: Path = Path("./data/hyperliquid.db")
    hl_collector_enabled: bool = False
    hl_wallet_scoring_enabled: bool = False
    hl_signal_shadow_enabled: bool = False
    hl_intraday_alerts_enabled: bool = False
    hl_alert_max_per_hour: int = 2
    hl_alert_max_per_day: int = 8
    hl_swing_influence_enabled: bool = False
    hl_max_candidate_wallets: int = 200
    hl_max_tracked_wallets: int = 50
    hl_api_weight_per_minute: int = 600
    hl_backfill_weight_pct: str = "0.20"
    hl_history_backfill_enabled: bool = False
    hl_history_backfill_days: int = 180
    hl_equity_backfill_interval_seconds: int = 30
    hl_equity_backfill_chunk_days: int = 7
    hl_futu_history_backfill_enabled: bool = False
    hl_recent_trades_retention_hours: int = 72
    hl_retention_days: int = 400
    hl_symbol_denylist: list[str] = field(default_factory=lambda: ["SOXL", "KORU"])
    hl_map_max_dev_ratio: str = "0.05"
    hl_min_daily_notional: str = "250000"
    hl_min_oi_notional: str = "100000"
    hl_max_mark_oracle_deviation: str = "0.01"
    hl_max_spread_ratio: str = "0.005"
    hl_min_directional_wallets: int = 5
    hl_wallet_weight_cap: str = "0.20"
    hl_http_timeout: float = 20.0

    # ---- 市场环境引擎 ----
    market_enabled: bool = True
    market_lookback_days: int = 260          # 约一年交易日，200 日均线与分位数需要
    market_max_lag_days: int = 4             # 超过即 stale（覆盖长周末+假期）
    market_percentile_window: int = 252
    market_sync_interval_min: int = 30
    market_qqq_drawdown_max: str = "0.10"
    market_sox_underperform_max: str = "-0.05"
    market_vix_warn: str = "20"
    market_vix_crit: str = "30"
    market_vix_spike: str = "0.15"
    market_vxn_warn: str = "22"
    market_vxn_vix_gap: str = "5"
    market_rates_5d_pct: str = "0.08"
    market_rates_level_max: str = "45"
    # ---- 风险事件运营层 ----
    # 默认只推 high/critical。warning 进时间线不推送 —— 告警疲劳比漏报更致命。
    alert_notify_high: bool = True
    alert_notify_critical: bool = True
    alert_notify_warning: bool = False
    alert_notify_recovery: bool = True
    alert_notify_data_quality: bool = True
    alert_cooldown_minutes: int = 240
    alert_max_per_hour: int = 6
    alert_max_per_day: int = 20
    alert_merge_batch: bool = True
    alert_outside_market_hours: bool = True
    alert_max_retries: int = 2
    alert_retry_backoff_seconds: int = 5
    dashboard_url: str | None = None
    # ---- 看板登录保护 ----
    dashboard_auth_enabled: bool = False
    dashboard_auth_user: str = "admin"
    dashboard_session_ttl_hours: int = 120
    # ---- 复盘与回测 ----
    research_refresh_hours: int = 6
    # 联合风险事件阈值
    combined_semi_weight_max: str = "0.30"
    combined_leverage_max: str = "1.50"
    combined_drawdown_max: str = "0.10"
    combined_cash_buffer_min: str = "0.30"

    combined_conc_factor: str = "1.3"
    combined_cash_factor: str = "0.85"
    combined_leverage_factor: str = "1.5"
    combined_sector_factor: str = "1.3"
    #: Flex Query（现金流与真实收益层）。留空则该功能显示 not_configured，
    #: 但绝不影响账户同步、行情同步与风险告警。
    flex_token: str = ""
    flex_query_id: str = ""
    flex_sync_enabled: bool = True
    flex_sync_interval_hours: int = 12
    flex_report_lookback_days: int = 90
    flex_request_timeout: int = 30
    flex_poll_interval: int = 5
    flex_max_polls: int = 12
    flex_max_retries: int = 2
    flex_reconciliation_tolerance: str = "50"

    # 心跳监控（healthchecks.io 等）：每次持仓同步成功后 GET 一次；留空禁用
    heartbeat_url: str | None = None


def load_config(env_file: str | os.PathLike | None = ".env") -> Config:
    """加载并校验配置。测试可传 env_file=None 跳过文件加载。"""
    if env_file is not None and Path(env_file).exists():
        load_dotenv(env_file, override=False)

    market_tz_name = _get("MARKET_TZ", "America/New_York")
    local_tz_name = _get("LOCAL_TZ", "Asia/Shanghai")

    return Config(
        account_enc_key=_get_required("ACCOUNT_ENC_KEY"),
        db_path=Path(_get("DB_PATH", "./data/dashboard.db")).expanduser(),
        log_path=Path(_get("LOG_PATH", "./data/dashboard.log")).expanduser(),
        sector_map_path=Path(_get("SECTOR_MAP_PATH", "./config/sectors.json")).expanduser(),
        market_tz=ZoneInfo(market_tz_name),
        local_tz=ZoneInfo(local_tz_name),
        positions_sync_interval=_get_int("POSITIONS_SYNC_INTERVAL", 60),
        orders_sync_interval=_get_int("ORDERS_SYNC_INTERVAL", 300),
        apprise_urls=_get_list("APPRISE_URLS"),
        log_level=_get("LOG_LEVEL", "INFO") or "INFO",
        ib_host=_get("IB_HOST", "ib-gateway") or "ib-gateway",
        ib_port=_get_int("IB_PORT", 4004),
        ib_client_id=_get_int("IB_CLIENT_ID", 17),
        ib_account=_get("IB_ACCOUNT"),
        ib_quote_timeout=float(_get("IB_QUOTE_TIMEOUT", "8") or "8"),
        ib_quote_wait=float(_get("IB_QUOTE_WAIT", "11") or "11"),
        ib_market_data_type=_get_int("IB_MARKET_DATA_TYPE", 3),
        ib_market_data_type_fallback=_get_int("IB_MARKET_DATA_TYPE_FALLBACK", 3),
        ib_realtime_probe_cooldown_min=_get_int("IB_REALTIME_PROBE_COOLDOWN_MIN", 30),
        futu_quote_enabled=_get_bool("FUTU_QUOTE_ENABLED", False),
        futu_host=_get("FUTU_HOST", "127.0.0.1") or "127.0.0.1",
        futu_port=_get_int("FUTU_PORT", 11111),
        futu_connect_timeout=float(_get("FUTU_CONNECT_TIMEOUT", "1.5") or "1.5"),
        futu_quote_stale_seconds=_get_int("FUTU_QUOTE_STALE_SECONDS", 300),
        live_sync_enabled=_get_bool("LIVE_SYNC_ENABLED", False),
        live_publish_interval=float(_get("LIVE_PUBLISH_INTERVAL", "1") or "1"),
        live_flush_interval=float(_get("LIVE_FLUSH_INTERVAL", "5") or "5"),
        live_reconcile_interval=float(_get("LIVE_RECONCILE_INTERVAL", "60") or "60"),
        ib_exec_client_id=_get_int("IB_EXEC_CLIENT_ID", 0),
        exec_sync_interval=_get_int("EXEC_SYNC_INTERVAL", 45),
        ib_req_timeout=float(_get("IB_REQ_TIMEOUT", "20") or "20"),
        ib_commission_wait=float(_get("IB_COMMISSION_WAIT", "3") or "3"),
        ib_pnl_wait=float(_get("IB_PNL_WAIT", "4") or "4"),
        alert_worsen_abs=Decimal(_get("ALERT_WORSEN_ABS", "0.05") or "0.05"),
        alert_worsen_rel=Decimal(_get("ALERT_WORSEN_REL", "0.20") or "0.20"),
        alert_retrigger_cooldown_min=int(_get("ALERT_RETRIGGER_COOLDOWN_MIN", "30") or "30"),
        dq_stale_seconds_market=_get_int("DQ_STALE_SECONDS_MARKET", 300),
        dq_stale_seconds_closed=_get_int("DQ_STALE_SECONDS_CLOSED", 7200),
        dq_error_seconds=_get_int("DQ_ERROR_SECONDS", 86400),
        dq_positions_value_rel_tol=_get("DQ_POSITIONS_VALUE_REL_TOL", "0.02") or "0.02",
        dq_equity_identity_rel_tol=_get("DQ_EQUITY_IDENTITY_REL_TOL", "0.05") or "0.05",
        dq_equity_jump_rel=_get("DQ_EQUITY_JUMP_REL", "0.20") or "0.20",
        dq_daily_pnl_rel_warn=_get("DQ_DAILY_PNL_REL_WARN", "0.20") or "0.20",
        dq_single_position_over_equity=_get("DQ_SINGLE_POSITION_OVER_EQUITY", "1.00") or "1.00",
        manual_sync_min_interval=_get_int("MANUAL_SYNC_MIN_INTERVAL", 30),
        sync_stale_running_seconds=_get_int("SYNC_STALE_RUNNING_SECONDS", 600),
        sync_max_retries=_get_int("SYNC_MAX_RETRIES", 2),
        sync_retry_base_delay=float(_get("SYNC_RETRY_BASE_DELAY", "1.0") or "1.0"),
        risk_single_weight_equity_max=_get("RISK_SINGLE_WEIGHT_EQUITY_MAX", "0.30") or "0.30",
        risk_single_weight_invested_max=_get("RISK_SINGLE_WEIGHT_INVESTED_MAX", "0.50") or "0.50",
        risk_top3_invested_max=_get("RISK_TOP3_INVESTED_MAX", "0.80") or "0.80",
        risk_top5_invested_max=_get("RISK_TOP5_INVESTED_MAX", "0.95") or "0.95",
        risk_unknown_sector_max=_get("RISK_UNKNOWN_SECTOR_MAX", "0.30") or "0.30",
        risk_cash_ratio_min=_get("RISK_CASH_RATIO_MIN", "0.05") or "0.05",
        risk_gross_leverage_max=_get("RISK_GROSS_LEVERAGE_MAX", "2.0") or "2.0",
        risk_net_leverage_max=_get("RISK_NET_LEVERAGE_MAX", "1.5") or "1.5",
        risk_maint_safety_min=_get("RISK_MAINT_SAFETY_MIN", "2.0") or "2.0",
        risk_excess_liquidity_min=_get("RISK_EXCESS_LIQUIDITY_MIN", "0.10") or "0.10",
        risk_daily_loss_max=_get("RISK_DAILY_LOSS_MAX", "0.03") or "0.03",
        risk_drawdown_max=_get("RISK_DRAWDOWN_MAX", "0.15") or "0.15",
        risk_max_drawdown_max=_get("RISK_MAX_DRAWDOWN_MAX", "0.25") or "0.25",
        risk_position_loss_max=_get("RISK_POSITION_LOSS_MAX", "0.20") or "0.20",
        risk_loss_contribution_max=_get("RISK_LOSS_CONTRIBUTION_MAX", "0.60") or "0.60",
        daily_pnl_skew_tolerance_seconds=_get_int(
            "DAILY_PNL_SKEW_TOLERANCE_SECONDS", 90),
        daily_pnl_max_age_seconds=_get_int("DAILY_PNL_MAX_AGE_SECONDS", 600),
        daily_pnl_v2_enabled=_get_bool("DAILY_PNL_V2_ENABLED", False),
        ib_overnight_quotes=_get_bool("IB_OVERNIGHT_QUOTES", True),
        ib_overnight_exchange=_get("IB_OVERNIGHT_EXCHANGE", "OVERNIGHT") or "OVERNIGHT",
        ai_api_key=_get("AI_API_KEY", "") or "",
        ai_base_url=_get("AI_BASE_URL", "https://api.openai.com/v1")
        or "https://api.openai.com/v1",
        ai_model=_get("AI_MODEL", "gpt-5") or "gpt-5",
        ai_max_tokens=_get_int("AI_MAX_TOKENS", 16000),
        ai_timeout_seconds=_get_int("AI_TIMEOUT_SECONDS", 120),
        brief_enabled=_get_bool("BRIEF_ENABLED", True),
        brief_hour_local=_get_int("BRIEF_HOUR_LOCAL", 6),
        brief_minute_local=_get_int("BRIEF_MINUTE_LOCAL", 30),
        risk_top3_min_position_count=_get_int("RISK_TOP3_MIN_POSITION_COUNT", 6),
        risk_top5_min_position_count=_get_int("RISK_TOP5_MIN_POSITION_COUNT", 10),
        risk_hhi_threshold=_get("RISK_HHI_THRESHOLD", "0.40") or "0.40",
        risk_effective_position_count_min=_get(
            "RISK_EFFECTIVE_POSITION_COUNT_MIN", "3") or "3",
        risk_hhi_min_positions=_get_int("RISK_HHI_MIN_POSITIONS", 2),
        risk_effective_min_positions=_get_int("RISK_EFFECTIVE_MIN_POSITIONS", 2),
        risk_dominance_gap_min=_get("RISK_DOMINANCE_GAP_MIN", "0.20") or "0.20",
        risk_weight_concentration=_get("RISK_WEIGHT_CONCENTRATION", "0.25") or "0.25",
        risk_weight_leverage=_get("RISK_WEIGHT_LEVERAGE", "0.20") or "0.20",
        risk_weight_drawdown=_get("RISK_WEIGHT_DRAWDOWN", "0.20") or "0.20",
        risk_weight_liquidity=_get("RISK_WEIGHT_LIQUIDITY", "0.20") or "0.20",
        risk_weight_loss=_get("RISK_WEIGHT_LOSS", "0.10") or "0.10",
        risk_weight_data_quality=_get("RISK_WEIGHT_DATA_QUALITY", "0.05") or "0.05",
        daily_bars_lookback_days=_get_int("DAILY_BARS_LOOKBACK_DAYS", 600),
        opportunity_enabled=_get_bool("OPPORTUNITY_ENABLED", False),
        portfolio_builder_enabled=_get_bool("PORTFOLIO_BUILDER_ENABLED", False),
        opportunity_shadow_min_days=max(
            0, _get_int("OPPORTUNITY_SHADOW_MIN_DAYS", 5)),
        opportunity_max_active_symbols=_get_int("OPPORTUNITY_MAX_ACTIVE_SYMBOLS", 100),
        opportunity_manual_scan_cooldown_seconds=_get_int(
            "OPPORTUNITY_MANUAL_SCAN_COOLDOWN_SECONDS", 600),
        opportunity_min_price=_get("OPPORTUNITY_MIN_PRICE", "5") or "5",
        opportunity_min_median_dollar_volume=_get(
            "OPPORTUNITY_MIN_MEDIAN_DOLLAR_VOLUME", "20000000") or "20000000",
        opportunity_min_bars=_get_int("OPPORTUNITY_MIN_BARS", 120),
        opportunity_full_bars=_get_int("OPPORTUNITY_FULL_BARS", 252),
        opportunity_ready_score=_get_int("OPPORTUNITY_READY_SCORE", 75),
        opportunity_watch_score=_get_int("OPPORTUNITY_WATCH_SCORE", 60),
        opportunity_risk_per_trade=_get(
            "OPPORTUNITY_RISK_PER_TRADE", "0.0075") or "0.0075",
        opportunity_target_position_max=_get(
            "OPPORTUNITY_TARGET_POSITION_MAX", "0.20") or "0.20",
        opportunity_sector_max=_get(
            "OPPORTUNITY_SECTOR_MAX", "0.35") or "0.35",
        opportunity_max_positions=_get_int("OPPORTUNITY_MAX_POSITIONS", 8),
        hyperliquid_enabled=_get_bool("HYPERLIQUID_ENABLED", False),
        hl_dex=_get("HL_DEX", "xyz") or "xyz",
        hl_db_path=Path(_get("HL_DB_PATH", "./data/hyperliquid.db") or "./data/hyperliquid.db").expanduser(),
        hl_collector_enabled=_get_bool("HL_COLLECTOR_ENABLED", False),
        hl_wallet_scoring_enabled=_get_bool("HL_WALLET_SCORING_ENABLED", False),
        hl_signal_shadow_enabled=_get_bool("HL_SIGNAL_SHADOW_ENABLED", False),
        hl_intraday_alerts_enabled=_get_bool("HL_INTRADAY_ALERTS_ENABLED", False),
        hl_alert_max_per_hour=_get_int("HL_ALERT_MAX_PER_HOUR", 2),
        hl_alert_max_per_day=_get_int("HL_ALERT_MAX_PER_DAY", 8),
        hl_swing_influence_enabled=_get_bool("HL_SWING_INFLUENCE_ENABLED", False),
        hl_max_candidate_wallets=_get_int("HL_MAX_CANDIDATE_WALLETS", 200),
        hl_max_tracked_wallets=_get_int("HL_MAX_TRACKED_WALLETS", 50),
        hl_api_weight_per_minute=_get_int("HL_API_WEIGHT_PER_MINUTE", 600),
        hl_backfill_weight_pct=_get("HL_BACKFILL_WEIGHT_PCT", "0.20") or "0.20",
        hl_history_backfill_enabled=_get_bool("HL_HISTORY_BACKFILL_ENABLED", False),
        hl_history_backfill_days=_get_int("HL_HISTORY_BACKFILL_DAYS", 180),
        hl_equity_backfill_interval_seconds=max(
            15, _get_int("HL_EQUITY_BACKFILL_INTERVAL_SECONDS", 30)),
        hl_equity_backfill_chunk_days=max(
            1, min(7, _get_int("HL_EQUITY_BACKFILL_CHUNK_DAYS", 7))),
        hl_futu_history_backfill_enabled=_get_bool(
            "HL_FUTU_HISTORY_BACKFILL_ENABLED", False),
        hl_recent_trades_retention_hours=_get_int("HL_RECENT_TRADES_RETENTION_HOURS", 72),
        hl_retention_days=_get_int("HL_RETENTION_DAYS", 400),
        hl_symbol_denylist=[x.upper() for x in _get_list("HL_SYMBOL_DENYLIST", "SOXL,KORU")],
        hl_map_max_dev_ratio=_get("HL_MAP_MAX_DEV_RATIO", "0.05") or "0.05",
        hl_min_daily_notional=_get("HL_MIN_DAILY_NOTIONAL", "250000") or "250000",
        hl_min_oi_notional=_get("HL_MIN_OI_NOTIONAL", "100000") or "100000",
        hl_max_mark_oracle_deviation=_get("HL_MAX_MARK_ORACLE_DEVIATION", "0.01") or "0.01",
        hl_max_spread_ratio=_get("HL_MAX_SPREAD_RATIO", "0.005") or "0.005",
        hl_min_directional_wallets=_get_int("HL_MIN_DIRECTIONAL_WALLETS", 5),
        hl_wallet_weight_cap=_get("HL_WALLET_WEIGHT_CAP", "0.20") or "0.20",
        hl_http_timeout=float(_get("HL_HTTP_TIMEOUT", "20") or "20"),
        market_enabled=(_get("MARKET_ENABLED", "1") or "1") not in ("0", "false", "False"),
        market_lookback_days=_get_int("MARKET_LOOKBACK_DAYS", 260),
        market_max_lag_days=_get_int("MARKET_MAX_LAG_DAYS", 4),
        market_percentile_window=_get_int("MARKET_PERCENTILE_WINDOW", 252),
        market_sync_interval_min=_get_int("MARKET_SYNC_INTERVAL_MIN", 30),
        market_qqq_drawdown_max=_get("MARKET_QQQ_DRAWDOWN_MAX", "0.10") or "0.10",
        market_sox_underperform_max=_get("MARKET_SOX_UNDERPERFORM_MAX", "-0.05") or "-0.05",
        market_vix_warn=_get("MARKET_VIX_WARN", "20") or "20",
        market_vix_crit=_get("MARKET_VIX_CRIT", "30") or "30",
        market_vix_spike=_get("MARKET_VIX_SPIKE", "0.15") or "0.15",
        market_vxn_warn=_get("MARKET_VXN_WARN", "22") or "22",
        market_vxn_vix_gap=_get("MARKET_VXN_VIX_GAP", "5") or "5",
        market_rates_5d_pct=_get("MARKET_RATES_5D_PCT", "0.08") or "0.08",
        market_rates_level_max=_get("MARKET_RATES_LEVEL_MAX", "45") or "45",
        alert_notify_high=_flag("ALERT_NOTIFY_HIGH", True),
        alert_notify_critical=_flag("ALERT_NOTIFY_CRITICAL", True),
        alert_notify_warning=_flag("ALERT_NOTIFY_WARNING", False),
        alert_notify_recovery=_flag("ALERT_NOTIFY_RECOVERY", True),
        alert_notify_data_quality=_flag("ALERT_NOTIFY_DATA_QUALITY", True),
        alert_cooldown_minutes=_get_int("ALERT_COOLDOWN_MINUTES", 240),
        alert_max_per_hour=_get_int("ALERT_MAX_PER_HOUR", 6),
        alert_max_per_day=_get_int("ALERT_MAX_PER_DAY", 20),
        alert_merge_batch=_flag("ALERT_MERGE_BATCH", True),
        alert_outside_market_hours=_flag("ALERT_OUTSIDE_MARKET_HOURS", True),
        alert_max_retries=_get_int("ALERT_MAX_RETRIES", 2),
        alert_retry_backoff_seconds=_get_int("ALERT_RETRY_BACKOFF_SECONDS", 5),
        dashboard_url=_get("DASHBOARD_URL"),
        dashboard_auth_enabled=_get_bool("DASHBOARD_AUTH_ENABLED", False),
        dashboard_auth_user=_get("DASHBOARD_AUTH_USER", "admin") or "admin",
        dashboard_session_ttl_hours=_get_int("DASHBOARD_SESSION_TTL_HOURS", 120),
        research_refresh_hours=_get_int("RESEARCH_REFRESH_HOURS", 6),
        combined_semi_weight_max=_get("COMBINED_SEMI_WEIGHT_MAX", "0.30") or "0.30",
        combined_leverage_max=_get("COMBINED_LEVERAGE_MAX", "1.50") or "1.50",
        combined_drawdown_max=_get("COMBINED_DRAWDOWN_MAX", "0.10") or "0.10",
        combined_cash_buffer_min=_get("COMBINED_CASH_BUFFER_MIN", "0.30") or "0.30",
        combined_conc_factor=_get("COMBINED_CONC_FACTOR", "1.3") or "1.3",
        combined_cash_factor=_get("COMBINED_CASH_FACTOR", "0.85") or "0.85",
        combined_leverage_factor=_get("COMBINED_LEVERAGE_FACTOR", "1.5") or "1.5",
        combined_sector_factor=_get("COMBINED_SECTOR_FACTOR", "1.3") or "1.3",
        flex_token=_get("FLEX_TOKEN", "") or "",
        flex_query_id=_get("FLEX_QUERY_ID", "") or "",
        flex_sync_enabled=_flag("FLEX_SYNC_ENABLED", True),
        flex_sync_interval_hours=_get_int("FLEX_SYNC_INTERVAL", 12),
        flex_report_lookback_days=_get_int("FLEX_REPORT_LOOKBACK_DAYS", 90),
        flex_request_timeout=_get_int("FLEX_REQUEST_TIMEOUT", 30),
        flex_poll_interval=_get_int("FLEX_POLL_INTERVAL", 5),
        flex_max_polls=_get_int("FLEX_MAX_POLLS", 12),
        flex_max_retries=_get_int("FLEX_MAX_RETRIES", 2),
        flex_reconciliation_tolerance=_get("FLEX_RECONCILIATION_TOLERANCE", "50") or "50",
        heartbeat_url=_get("HEARTBEAT_URL"),
    )
