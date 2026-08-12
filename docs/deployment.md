# Deployment Guide

[English](deployment.md) | [简体中文](zh-CN/deployment.md)

This guide describes a generic self-hosted deployment. Never copy production credentials, databases, hostnames, or operational reports into the repository.

## 1. Requirements

- Linux host with Docker Engine and Docker Compose;
- an IBKR user, password, and TOTP secret;
- a dedicated read-only IBKR user whenever possible;
- a domain managed by Cloudflare for the included Caddy configuration;
- outbound access to IBKR and any explicitly enabled optional service.

The full stack includes IB Gateway and is intended for a private server. The dashboard contains financial information and must not be exposed without authentication.

## 2. Configuration

```bash
cp .env.example .env
chmod 600 .env
python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'
```

Set the generated key as `ACCOUNT_ENC_KEY`, then fill the required broker fields:

```dotenv
TWS_USERID=
TWS_PASSWORD=
TOTP_KEY=
ACCOUNT_ENC_KEY=
```

For HTTPS:

```dotenv
DASHBOARD_SITE=dashboard.example.com
CF_API_TOKEN=
```

Do not include a scheme or port in `DASHBOARD_SITE`.

## 3. Initialize the Database

```bash
docker compose build
docker compose run --rm web python manage.py init
```

Enable application authentication before public network access:

```dotenv
DASHBOARD_AUTH_ENABLED=true
DASHBOARD_AUTH_USER=admin
```

Set the password interactively:

```bash
docker compose run --rm web python manage.py webpass
```

The password is stored as a PBKDF2 hash in the local database. Plaintext is never written to the repository.

## 4. Preflight

Validate Compose syntax:

```bash
docker compose config >/dev/null
```

The Hyperliquid preflight is relevant only after its database has been initialized and collection has been enabled:

```bash
PYTHONPATH=. python scripts/deploy_preflight.py --mode shadow
```

Use `--mode full` only when broker credentials, application authentication, the site, and the main database are ready.

## 5. Start Services

Dashboard, backup, and HTTPS only:

```bash
docker compose up -d --build web backup caddy
```

Full read-only IBKR stack:

```bash
docker compose --profile live up -d --build
docker compose --profile live ps
```

The `app/` directory is copied into the image. Rebuild after code changes.

## 6. Verify

```bash
docker compose exec web python manage.py status
docker compose exec web python -c \
  'import urllib.request; print(urllib.request.urlopen("http://127.0.0.1:8000/healthz").read().decode())'
curl -fsS https://dashboard.example.com/healthz
```

Expected health output includes `"readonly": true`.

Inspect service state without printing environment variables:

```bash
docker compose --profile live ps
docker compose logs --tail 100 web
docker compose logs --tail 100 sync
```

Do not paste raw Gateway, environment, or database output into public issues.

## 7. Optional Futu Quote Source

Futu OpenD is quote-only. Configure it through `FUTU_*` variables. If it is unavailable or stale, symbols fall back to IBKR according to quality rules. Futu never replaces IBKR account state.

## 8. Optional X Monitor Source

Set a host directory that contains the separate local source database:

```dotenv
X_MONITOR_DATA_PATH=./data/x-monitor
```

Docker mounts it read-only at `/app/x_monitor`. The default path is under the ignored `data/` directory.

## 9. Opportunity Rollout

Start with candidate scanning only:

```dotenv
OPPORTUNITY_ENABLED=true
PORTFOLIO_BUILDER_ENABLED=false
```

Wait for the configured number of successful, non-empty shadow trading days. Review quote quality, scan results, database growth, and primary account synchronization before enabling portfolio proposals.

## 10. Hyperliquid Collection-Only Rollout

Start with public-data collection and no decision impact:

```dotenv
HYPERLIQUID_ENABLED=true
HL_COLLECTOR_ENABLED=true
HL_WALLET_SCORING_ENABLED=false
HL_SIGNAL_SHADOW_ENABLED=false
HL_INTRADAY_ALERTS_ENABLED=false
HL_SWING_INFLUENCE_ENABLED=false
```

The collector writes `hyperliquid.db`, which is separate from `dashboard.db`. Do not enable wallet scoring, signals, alerts, or recommendation influence until the required quality and out-of-sample gates pass.

To stop collection without deleting data:

```bash
docker compose stop hyperliquid
```

## 11. Backup and Recovery

The backup sidecar uses SQLite online backup and writes compressed files to `backups/`. Both `data/` and `backups/` are ignored by Git.

Verify archives periodically:

```bash
for archive in backups/*.db.gz; do gzip -t "$archive"; done
```

Recovery should be rehearsed on a separate host or temporary directory. Never restore over the only copy of a production database.

## 12. Updating

```bash
git pull --ff-only
docker compose --profile live up -d --build
docker compose --profile live ps
```

Run tests and preflight before production rollout. Keep feature flags off when a migration, source, or validation gate is not ready.

## 13. Privacy Checklist

Before sharing logs or opening an issue:

- remove account identifiers, positions, executions, wallet addresses, hostnames, and IP addresses;
- do not attach `.env`, databases, backups, incident evidence, or generated reports;
- rotate any token that may have appeared in terminal output;
- prefer a minimal synthetic reproduction.
