# 安全策略

[English](SECURITY.md) | [简体中文](SECURITY.zh-CN.md)

## 支持版本

安全修复以默认分支的最新提交为目标。

## 报告漏洞

如 GitHub 提供私密漏洞报告功能，请优先使用。疑似凭据泄露、认证绕过、券商副作用或财务数据暴露时，不要创建公开 Issue。

只提供合成或完全脱敏的证据。不要附加 `.env`、数据库、包含标识符的日志、持仓截图、TOTP 材料、Webhook 或 API Token。

## 部署要求

- 使用专门的 IBKR 只读子用户；
- 保持 `IB_READONLY=yes` 与 API 客户端 `readonly=True`；
- 公网访问前启用 `DASHBOARD_AUTH_ENABLED`；
- Secret 只保存在 `.env` 或只读 Secret 挂载；
- 限制 `.env`、`data/`、`backups/` 与 `secrets/` 的文件权限；
- 数据库、备份、日志、生成报告与事故证据不得进入 Git；
- 任何可能出现在终端输出或提交中的凭据都应立即轮换。

维护者不需要、也不会索取报告者的券商账户访问权限。
