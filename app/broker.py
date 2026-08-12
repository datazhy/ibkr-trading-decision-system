"""券商数据源接口（只读）。

适配器（ibkr_client.IBKRClient）实现这组方法，SyncService 只依赖本接口，
规则引擎与看板层不感知具体数据源。保留该 Protocol 的意义是把**数据契约**写在
一处：任何新增字段都要先在这里定义语义，避免各层各自解释同一份数据。

数据契约（与 SQLite 表对齐，金额 Decimal）：
  positions 行: {symbol, qty(带方向), avg_cost, market_value, upl}
  balances:     {equity, cash, margin_balance, buying_power}
  orders 行:    {order_id, ts, symbol, side, order_type, qty, price, status, raw_json}
  txns 行:      {txn_id, ts, symbol, action, qty, price, fees, amount, realized?, raw_json}
                realized 为券商给出的精确已实现盈亏（CommissionReport.realizedPNL）
  bars 行:      {date, o, h, l, c, v}
"""
from __future__ import annotations

from datetime import datetime
from typing import Protocol, runtime_checkable


@runtime_checkable
class Broker(Protocol):
    def resolve_account_hash(self, account_number: str | None) -> str:
        """把账户号解析为 API 侧账户标识（IBKR: 账户 id）。"""
        ...

    def fetch_positions_and_balances(self, account_hash: str) -> tuple[list[dict], dict]:
        ...

    def fetch_orders(self, account_hash: str, start: datetime, end: datetime) -> list[dict]:
        ...

    def fetch_transactions(self, account_hash: str, start: datetime, end: datetime) -> list[dict]:
        ...

    def fetch_daily_bars(self, symbol: str, start: datetime, end: datetime) -> list[dict]:
        ...

    def auth_status(self) -> tuple[bool, str]:
        """(健康?, 人话描述)。IBKR: gateway 连接状态。"""
        ...
