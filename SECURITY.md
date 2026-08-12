# Security Policy

[English](SECURITY.md) | [简体中文](SECURITY.zh-CN.md)

## Supported Version

Security fixes target the latest commit on the default branch.

## Reporting a Vulnerability

Please use GitHub's private vulnerability reporting feature when available. Do not open a public issue for a suspected credential leak, authentication bypass, broker-side effect, or exposure of financial data.

Include only synthetic or fully redacted evidence. Do not attach `.env` files, databases, logs containing identifiers, screenshots of positions, TOTP material, webhook URLs, or API tokens.

## Deployment Requirements

- Use a dedicated read-only IBKR user.
- Keep `IB_READONLY=yes` and the API client's `readonly=True` setting.
- Enable `DASHBOARD_AUTH_ENABLED` before internet exposure.
- Store secrets only in `.env` or read-only secret mounts.
- Restrict filesystem permissions on `.env`, `data/`, `backups/`, and `secrets/`.
- Keep databases, backups, logs, generated reports, and incident evidence out of Git.
- Rotate any credential that may have appeared in terminal output or a commit.

The maintainers do not request or need access to a reporter's brokerage account.
