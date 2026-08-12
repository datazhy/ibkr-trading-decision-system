# 部署指南

[English](../deployment.md) | [简体中文](deployment.md)

本文描述通用的自托管部署。不要把生产凭据、数据库、真实域名或运营报告复制进仓库。

## 1. 前置条件

- 安装 Docker Engine 与 Docker Compose 的 Linux 主机；
- IBKR 用户名、密码与 TOTP Secret；
- 尽可能使用专门的 IBKR 只读子用户；
- 如使用内置 Caddy 配置，需要由 Cloudflare 管理的域名；
- 能访问 IBKR 与显式启用的可选服务。

完整栈包含 IB Gateway，适合部署在私有服务器。看板包含财务信息，未启用认证时不得暴露到公网。

## 2. 配置

```bash
cp .env.example.zh-CN .env
chmod 600 .env
python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'
```

把生成值写入 `ACCOUNT_ENC_KEY`，然后填写券商必需字段：

```dotenv
TWS_USERID=
TWS_PASSWORD=
TOTP_KEY=
ACCOUNT_ENC_KEY=
```

HTTPS 配置：

```dotenv
DASHBOARD_SITE=dashboard.example.com
CF_API_TOKEN=
```

`DASHBOARD_SITE` 不要包含协议或端口。

## 3. 初始化数据库

```bash
docker compose build
docker compose run --rm web python manage.py init
```

公网访问前启用应用认证：

```dotenv
DASHBOARD_AUTH_ENABLED=true
DASHBOARD_AUTH_USER=admin
```

交互式设置密码：

```bash
docker compose run --rm web python manage.py webpass
```

密码只以 PBKDF2 哈希保存在本地数据库，不会以明文写入仓库。

## 4. 预检

验证 Compose 语法：

```bash
docker compose config >/dev/null
```

Hyperliquid 预检只适用于已经初始化独立数据库并打开采集的环境：

```bash
PYTHONPATH=. python scripts/deploy_preflight.py --mode shadow
```

只有券商凭据、应用认证、域名和主数据库都就绪时，才使用 `--mode full`。

## 5. 启动服务

只启动看板、备份和 HTTPS：

```bash
docker compose up -d --build web backup caddy
```

启动完整 IBKR 只读栈：

```bash
docker compose --profile live up -d --build
docker compose --profile live ps
```

`app/` 会复制进镜像，修改代码后必须重新构建。

## 6. 验证

```bash
docker compose exec web python manage.py status
docker compose exec web python -c \
  'import urllib.request; print(urllib.request.urlopen("http://127.0.0.1:8000/healthz").read().decode())'
curl -fsS https://dashboard.example.com/healthz
```

健康输出应包含 `"readonly": true`。

检查服务状态时不要输出环境变量：

```bash
docker compose --profile live ps
docker compose logs --tail 100 web
docker compose logs --tail 100 sync
```

不要把原始 Gateway 日志、环境变量或数据库输出粘贴到公开 Issue。

## 7. 可选 Futu 行情源

Futu OpenD 只提供行情。通过 `FUTU_*` 配置；不可用或过期时按质量规则逐标的回退 IBKR。Futu 不能替代 IBKR 账户状态。

## 8. 可选 X Monitor 数据源

设置包含独立本地源库的宿主机目录：

```dotenv
X_MONITOR_DATA_PATH=./data/x-monitor
```

Docker 会只读挂载到 `/app/x_monitor`。默认路径位于被 Git 忽略的 `data/` 下。

## 9. 机会模块上线

先只打开候选扫描：

```dotenv
OPPORTUNITY_ENABLED=true
PORTFOLIO_BUILDER_ENABLED=false
```

等待达到配置的成功且非空影子交易日数量。确认行情质量、扫描结果、数据库增长和主账户同步稳定后，再打开组合提案。

## 10. Hyperliquid 纯采集上线

先采集公共数据，不影响决策：

```dotenv
HYPERLIQUID_ENABLED=true
HL_COLLECTOR_ENABLED=true
HL_WALLET_SCORING_ENABLED=false
HL_SIGNAL_SHADOW_ENABLED=false
HL_INTRADAY_ALERTS_ENABLED=false
HL_SWING_INFLUENCE_ENABLED=false
```

采集器写入独立于 `dashboard.db` 的 `hyperliquid.db`。在数据质量和样本外门禁通过前，不要打开钱包评分、信号、告警或建议影响。

停止采集但保留数据：

```bash
docker compose stop hyperliquid
```

## 11. 备份与恢复

备份 sidecar 使用 SQLite 在线备份并把压缩文件写入 `backups/`。`data/` 与 `backups/` 都会被 Git 忽略。

定期验证压缩包：

```bash
for archive in backups/*.db.gz; do gzip -t "$archive"; done
```

恢复演练应在独立主机或临时目录中进行，绝不能直接覆盖唯一的生产数据库。

## 12. 更新

```bash
git pull --ff-only
docker compose --profile live up -d --build
docker compose --profile live ps
```

生产更新前先运行测试和预检。迁移、数据源或验证门禁未就绪时，保持对应功能开关关闭。

## 13. 隐私检查

分享日志或创建 Issue 前：

- 删除账户标识、持仓、成交、钱包地址、域名与 IP；
- 不要附加 `.env`、数据库、备份、事故证据或生成报告；
- 任何可能出现在终端输出中的 Token 都应轮换；
- 优先提供最小化的合成复现。
