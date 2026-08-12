# Contributing

[English](CONTRIBUTING.md) | [简体中文](CONTRIBUTING.zh-CN.md)

Thank you for helping improve the Personal Trading Decision System.

## Before You Start

- Never include real account data, positions, executions, wallet watchlists, hostnames, logs, databases, credentials, or generated operational reports.
- Preserve the read-only broker boundary. Changes that add order, cancellation, exercise, or transfer capabilities are out of scope.
- Keep PnL definitions and order/execution/decision counts explicit and separate.
- Keep optional sources degradable; they must not overwrite authoritative IBKR account facts.

## Development

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
python -m pytest -q
```

Use synthetic fixtures in tests. Do not copy production rows and redact them later.

## Pull Requests

1. Keep the change focused.
2. Add or update tests for behavior changes.
3. Update English and Chinese documentation together when user-visible behavior changes.
4. Run `python -m pytest -q` and `git diff --check`.
5. Explain safety impact, data-source impact, migration requirements, and validation evidence.

By contributing, you agree that your contribution is licensed under the repository's MIT License.
