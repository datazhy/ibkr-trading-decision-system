#!/usr/bin/env python3
"""命令行工具：init / status / resync / flex / cashflow 等（全部只读或本地操作）。

  python manage.py init      建表初始化
  python manage.py status    输出持仓、现金、margin 余额与连接状态
"""
from __future__ import annotations

import argparse
import os
import stat
import sys
from decimal import Decimal
from pathlib import Path

from app.config import ConfigError, load_config
from app.crypto import mask_account
from app.db import Database, to_decimal
from app.logging_setup import setup_logging
from app.notify import Notifier
from app.service import make_broker_client
from app.sync import SyncService


def _money(v) -> str:
    if v is None:
        return "-"
    d = v if isinstance(v, Decimal) else Decimal(str(v))
    return f"{d:,.2f}"


def _secure_file(path: Path) -> None:
    """token 文件权限设为 600。"""
    try:
        path.chmod(stat.S_IRUSR | stat.S_IWUSR)
    except OSError as exc:
        print(f"⚠️ 无法设置 {path} 权限为 600: {exc}", file=sys.stderr)


def cmd_init(cfg) -> int:
    db = Database(cfg.db_path)
    db.init_schema()
    print(f"✓ 数据库已初始化: {cfg.db_path}")

    print("✓ 账户 id 由 ib-gateway 提供（IB_ACCOUNT），无需额外存储")
    return 0


def cmd_webpass(cfg, args) -> int:
    """设置看板登录密码（用户名固定为 DASHBOARD_AUTH_USER，默认 admin）。

    密码只以 PBKDF2 哈希存库，绝不落明文。优先从 --password 取，否则交互输入
    （两次一致校验）。设完当前所有会话仍有效，只影响下一次登录。
    """
    import getpass
    from app import webauth

    db = Database(cfg.db_path)
    db.init_schema()
    pw = args.password
    if not pw:
        pw = getpass.getpass("新登录密码: ")
        again = getpass.getpass("再输一次: ")
        if pw != again:
            print("两次输入不一致，未修改。", file=sys.stderr)
            return 2
    if len(pw) < 6:
        print("密码至少 6 位，未修改。", file=sys.stderr)
        return 2
    webauth.set_password(db, pw)
    print(f"✓ 已设置登录密码（用户名 {cfg.dashboard_auth_user}）。"
          f"会话有效期 {cfg.dashboard_session_ttl_hours}h。")
    return 0


def cmd_resync(cfg) -> int:
    """恢复后补同步：账户/持仓/行情/订单/成交 + 立即规则评估（只读）。

    供 watchdog 在 gateway 重启并完成 TOTP 登录后调用。
    用独立 clientId（+80）避免与常驻 sync 守护进程冲突。
    """
    import dataclasses
    from app.engine import RuleEngine

    cfg = dataclasses.replace(cfg, ib_client_id=cfg.ib_client_id + 80)
    db = Database(cfg.db_path)
    db.init_schema()
    notifier = Notifier(cfg.apprise_urls, db)
    client = make_broker_client(cfg)
    sync = SyncService(cfg, db, client, notifier)
    engine = RuleEngine(cfg, db, notifier)

    ok_pos = sync.sync_positions_and_balances()   # 账户+持仓+行情质量
    ok_ord = sync.sync_orders()
    ok_txn = sync.sync_transactions()
    # 注：executions 不在此补拉 —— 它必须用 clientId=0（IBKR 特权位，才看得到手动单），
    # 而常驻 sync 已占用该 clientId，两进程会冲突。常驻 executions job（45s）本身就是
    # 「拉当日全量 + exec_id 幂等 upsert」，重连后首轮即完成补拉，不会重复触发 R6。
    if ok_pos:
        engine.evaluate_and_dispatch()            # 补同步后立即评估
    print(f"resync: positions={ok_pos} orders={ok_ord} transactions={ok_txn}")
    if ok_pos and ok_ord:
        print("RESYNC_OK")
        return 0
    return 1


def cmd_status(cfg) -> int:
    db = Database(cfg.db_path)
    db.init_schema()

    print("=" * 56)
    print("  小鱼交易台 — 状态")
    print("=" * 56)
    print(f"  数据源券商       : IBKR")

    can_fetch = False

    positions = None
    balances = None
    source = "实时"

    # 优先实时拉取，失败回退到最新快照
    try:
        notifier = Notifier(cfg.apprise_urls, db)
        client = make_broker_client(cfg)
        ok, detail = client.auth_status()
        print(f"  Gateway 状态     : {detail}")
        can_fetch = ok
        if can_fetch:
            sync = SyncService(cfg, db, client, notifier)
            acct_hash = sync.account_hash()
            positions, balances = client.fetch_positions_and_balances(acct_hash)
    except Exception as exc:  # noqa: BLE001
        print(f"  （实时拉取失败，改用最新快照: {exc}）")
        positions = balances = None

    if positions is None:
        source = "最新快照"
        rows = db.latest_positions()
        positions = [
            {
                "symbol": r["symbol"],
                "qty": to_decimal(r["qty"]),
                "avg_cost": to_decimal(r["avg_cost"]),
                "market_value": to_decimal(r["market_value"]),
                "upl": to_decimal(r["upl"]),
            }
            for r in rows
        ]
        brow = db.latest_balances()
        if brow:
            balances = {
                "equity": to_decimal(brow["equity"]),
                "cash": to_decimal(brow["cash"]),
                "margin_balance": to_decimal(brow["margin_balance"]),
                "buying_power": to_decimal(brow["buying_power"]),
            }

    print(f"  数据来源         : {source}")
    print("-" * 56)

    if balances:
        print(f"  净资产 (equity)  : {_money(balances['equity'])}")
        print(f"  现金 (cash)      : {_money(balances['cash'])}")
        mb = balances["margin_balance"]
        flag = "  ⚠️ 已动用融资" if mb is not None and mb < 0 else ""
        print(f"  Margin 余额      : {_money(mb)}{flag}")
        print(f"  购买力           : {_money(balances['buying_power'])}")
    else:
        print("  （暂无余额数据，请先运行同步服务）")

    print("-" * 56)
    if positions:
        print(f"  {'标的':<8}{'数量':>10}{'均价':>12}{'市值':>14}{'浮盈亏':>14}")
        for p in positions:
            print(
                f"  {p['symbol']:<8}{_money(p['qty']):>10}{_money(p['avg_cost']):>12}"
                f"{_money(p['market_value']):>14}{_money(p['upl']):>14}"
            )
    else:
        print("  （暂无持仓数据）")
    print("=" * 56)
    return 0


def _account_for(cfg, db) -> str:
    bal = db.latest_balances()
    if bal and bal["account"]:
        return bal["account"]
    return cfg.ib_account or "DEFAULT"


def cmd_cashflow(cfg, args) -> int:
    """资金流登记（追加式审计）。

    纪律：金额必须来自 IBKR 入金记录/活动报表/你手上的准确数字。
    **禁止**用净资产跳变去反推（例如拿 47069.92-378.83 当入金额）—— 那个差额里
    混着当天的市值波动，不是入金额。
    """
    from datetime import datetime, timezone
    from decimal import Decimal
    from zoneinfo import ZoneInfo

    db = Database(cfg.db_path)
    db.init_schema()
    account = args.account or _account_for(cfg, db)

    if args.action == "list":
        rows = db.list_cash_flows(account)
        if not rows:
            print("（无资金流记录）")
        for r in rows:
            tag = f" [冲正 #{r['reverses_id']}]" if r["reverses_id"] else ""
            print(f"#{r['id']:<4} {r['et_date']}  {r['currency']} {r['amount']:>14}  "
                  f"{r['source']:<14} {r['note'] or ''}{tag}"
                  f"{('  ref=' + r['external_ref']) if r['external_ref'] else ''}")
        print()
        for c in db.cash_flow_coverages(account):
            print(f"覆盖声明：{c['start_date']} → {c['end_date']}  {c['status']}  {c['note'] or ''}")
        return 0

    if args.action == "add":
        et = args.date
        eff = args.effective_at or datetime.fromisoformat(
            f"{et}T12:00:00").replace(tzinfo=ZoneInfo("America/New_York")).astimezone(
            timezone.utc).isoformat()
        fid = db.add_cash_flow(
            account=account, amount=Decimal(args.amount), effective_at=eff, et_date=et,
            currency=args.currency, note=args.note, source=args.source,
            external_ref=args.external_ref)
        print(f"已登记资金流 #{fid}：{et} {args.currency} {args.amount}（{args.source}）")
        print("提示：登记完整后用 `cashflow cover --start X --end Y` 声明覆盖，")
        print("      否则 7/30 日窗口只会显示「净资产变动」而非盈亏。")
        return 0

    if args.action == "reverse":
        orig = db.get_cash_flow(args.id)
        if not orig:
            print(f"未找到资金流 #{args.id}", file=sys.stderr)
            return 1
        if orig["reverses_id"]:
            print(f"#{args.id} 本身就是冲正记录，不能再冲正", file=sys.stderr)
            return 1
        neg = str(-Decimal(orig["amount"]))
        fid = db.add_cash_flow(
            account=orig["account"], amount=Decimal(neg),
            effective_at=orig["effective_at"], et_date=orig["et_date"],
            currency=orig["currency"], note=(args.note or f"冲正 #{args.id}"),
            source=orig["source"], external_ref=orig["external_ref"], reverses_id=args.id)
        print(f"已追加冲正记录 #{fid}（抵销 #{args.id} 的 {orig['amount']}）")
        print("原记录保留不动 —— 审计链完整。如需更正金额，请再 add 一条正确的。")
        return 0

    if args.action == "cover":
        db.set_cash_flow_coverage(
            account=account, start_date=args.start, end_date=args.end,
            status=args.status, note=args.note, source="MANUAL")
        print(f"已声明覆盖：{args.start} → {args.end} = {args.status}")
        if args.status != "COMPLETE":
            print("非 COMPLETE：该区间窗口仍只显示「净资产变动」。")
        return 0

    if args.action == "backfill-equity":
        n = db.backfill_daily_equity(account, cfg.market_tz)
        print(f"已按美东交易日回填 daily_equity：新增 {n} 天（账户 {account}）")
        return 0
    return 1


def cmd_flex(cfg, args) -> int:
    """Flex Query 回补：历史成交 + 资金流。

    reqExecutions 只回当日成交 —— sync 挂一整天，那天的成交就永久缺失。
    Flex 是 IBKR 唯一的历史来源，也是唯一能自动拿到准确入金金额的地方。
    """
    if not cfg.flex_token or not cfg.flex_query_id:
        print("未配置 Flex 凭证。请在 .env 中设置：", file=sys.stderr)
        print("  FLEX_TOKEN=<Flex Web Service token>", file=sys.stderr)
        print("  FLEX_QUERY_ID=<Flex Query 的 ID>", file=sys.stderr)
        print("", file=sys.stderr)
        print("获取方式（IBKR 账户管理）：", file=sys.stderr)
        print("  1) 绩效与报告 → Flex Queries → 新建 Activity Flex Query", file=sys.stderr)
        print("     勾选 Trades（含 ibExecID / permId / fifoPnlRealized）", file=sys.stderr)
        print("     与 Cash Transactions（含 transactionID / type / amount）", file=sys.stderr)
        print("     Date Format 选 yyyyMMdd，Time Format 选 HHmmss", file=sys.stderr)
        print("  2) 绩效与报告 → 设置 → Flex Web Service → 生成 token", file=sys.stderr)
        return 2

    from app.notify import Notifier
    from app.service import make_broker_client
    db = Database(cfg.db_path)
    db.init_schema()
    sync = SyncService(cfg, db, make_broker_client(cfg), Notifier([], db))
    try:
        r = sync.backfill_from_flex()
    except Exception as exc:  # noqa: BLE001 — 凭证错误/网络问题都在这里收口
        print(f"Flex 回补失败: {exc}", file=sys.stderr)
        return 1
    print(f"成交：upsert {r['trades']} 笔（按 exec_id 幂等，与实时同步同键）")
    print(f"资金流：新增 {r['flows_added']} 笔，已存在跳过 {r['flows_skipped']} 笔"
          f"（共 {r['flows_total']} 笔）")
    if r["flows_added"]:
        print()
        print("提示：资金流已自动登记（source=FLEX_QUERY，带券商流水号可核对）。")
        print("      如需让 7/30 日窗口显示为「盈亏」而非「净资产变动」，还需声明覆盖：")
        print("      manage.py cashflow cover --start <起> --end <止>")
    return 0


def cmd_daily_bars(cfg) -> int:
    """全量重拉持仓标的日线（覆盖 daily_bars_lookback_days，约 400 交易日）。

    用独立 clientId（+80）避免与常驻 sync 守护进程抢 IBKR 单会话。
    拉完打印每个标的的日线根数，供人工核对是否足够 EMA120/52 周高点。
    """
    import dataclasses

    # +81 避开守护进程占用的 clientId：主 17 / market_client 97(=+80) / watchdog 99。
    cfg = dataclasses.replace(cfg, ib_client_id=cfg.ib_client_id + 81)
    db = Database(cfg.db_path)
    db.init_schema()
    notifier = Notifier(cfg.apprise_urls, db)
    client = make_broker_client(cfg)
    sync = SyncService(cfg, db, client, notifier)

    ok = sync.sync_daily_bars()
    symbols = sorted({r["symbol"] for r in db.latest_positions()})
    for s in symbols:
        rows = db.get_daily_bars(s, limit=10000)
        first = rows[0]["date"] if rows else "—"
        last = rows[-1]["date"] if rows else "—"
        print(f"  {s:<6} {len(rows):>4} 根  {first} → {last}")
    print("DAILY_BARS_OK" if ok else "DAILY_BARS_PARTIAL")
    return 0 if ok else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="小鱼交易台 管理工具")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init", help="加密存储账户号并建表")
    sub.add_parser("status", help="输出持仓/现金/margin/token 有效期")
    sub.add_parser("resync", help="补同步（账户/持仓/订单/成交）+ 规则评估，供 watchdog 恢复后调用")
    sub.add_parser("daily-bars", help="全量重拉持仓日线（约 400 交易日），供技术面/长周期指标预热")

    sub.add_parser("flex", help="Flex Query 回补：历史成交 + 资金流（需 FLEX_TOKEN）")

    wp = sub.add_parser("webpass", help="设置看板登录密码（用户名默认 admin）")
    wp.add_argument("--password", help="直接给出密码（省略则交互输入，更安全）")

    cf = sub.add_parser("cashflow", help="资金流登记（入金/出金，追加式审计）")
    cf.add_argument("action", choices=["add", "list", "reverse", "cover", "backfill-equity"])
    cf.add_argument("--account", help="账户号（默认取最近快照的账户）")
    cf.add_argument("--date", help="美东日期 YYYY-MM-DD（add）")
    cf.add_argument("--amount", help="金额：正=入金，负=出金。必须用IBKR入金记录/活动报表的准确数字")
    cf.add_argument("--currency", default="USD")
    cf.add_argument("--note", help="备注")
    cf.add_argument("--source", default="MANUAL", choices=["MANUAL", "IBKR_ACTIVITY"])
    cf.add_argument("--external-ref", dest="external_ref", help="活动报表凭证号等可核对标识")
    cf.add_argument("--effective-at", dest="effective_at", help="到账时刻 UTC ISO（默认当日 12:00 ET）")
    cf.add_argument("--id", type=int, help="被冲正记录的 ID（reverse）")
    cf.add_argument("--start", help="覆盖起始美东日期（cover）")
    cf.add_argument("--end", help="覆盖结束美东日期（cover）")
    cf.add_argument("--status", default="COMPLETE", choices=["COMPLETE", "PARTIAL"])
    args = parser.parse_args()

    try:
        cfg = load_config()
    except ConfigError as exc:
        print(f"配置错误: {exc}", file=sys.stderr)
        return 2

    # CLI 也初始化日志（写文件 + 控制台）
    setup_logging(cfg.log_path, cfg.log_level)

    if args.cmd == "init":
        return cmd_init(cfg)
    if args.cmd == "status":
        return cmd_status(cfg)
    if args.cmd == "resync":
        return cmd_resync(cfg)
    if args.cmd == "daily-bars":
        return cmd_daily_bars(cfg)
    if args.cmd == "flex":
        return cmd_flex(cfg, args)
    if args.cmd == "webpass":
        return cmd_webpass(cfg, args)
    if args.cmd == "cashflow":
        return cmd_cashflow(cfg, args)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
