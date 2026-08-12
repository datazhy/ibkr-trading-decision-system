# 贡献指南

[English](CONTRIBUTING.md) | [简体中文](CONTRIBUTING.zh-CN.md)

感谢你帮助改进个人交易决策系统。

## 开始前

- 不得包含真实账户数据、持仓、成交、钱包观察名单、域名、日志、数据库、凭据或服务器生成报告。
- 必须保留券商只读边界。下单、撤单、行权或转账能力不在项目范围内。
- 明确区分盈亏口径以及 order、execution、decision 计数。
- 可选数据源必须可以独立降级，不能覆盖 IBKR 权威账户事实。

## 开发

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example.zh-CN .env
python -m pytest -q
```

测试必须使用合成数据，不要先复制生产数据再尝试脱敏。

## Pull Request

1. 保持改动聚焦；
2. 行为变化需要增加或更新测试；
3. 用户可见行为变化时同步更新中英文文档；
4. 运行 `python -m pytest -q` 与 `git diff --check`；
5. 说明安全影响、数据源影响、迁移要求和验证证据。

提交贡献即表示你同意按照本仓库的 MIT License 授权该贡献。
