# Architecture and Safety Model

[English](architecture.md) | [简体中文](zh-CN/architecture.md)

## Design Goal

The system turns broker and market data into explainable decision support without crossing the boundary into automated execution. Every recommendation must retain its inputs, rule version, data timestamp, and quality state.

## Primary Data Path

```text
IB Gateway (read-only)
        │
        ▼
Synchronization and scheduler
        │
        ├── account / positions / executions / PnL
        ├── quote quality and daily bars
        ├── market regime and technical indicators
        └── data-quality checks
        │
        ▼
dashboard.db
        │
        ├── risk rules and combined events
        ├── recommendation synthesis
        ├── position plans and execution matching
        ├── performance / replay / research
        └── FastAPI + Jinja2 dashboard
```

Account truth always comes from IBKR. Futu OpenD can supply quotes and historical bars, but it never supplies balances, positions, executions, or PnL.

## Process Separation

| Process | Responsibility | External writes |
|---|---|---|
| `ib-gateway` | Broker session with Read-Only API enabled | None to the broker |
| `sync` | Ingestion, validation, rules, recommendations, and scheduled jobs | Local databases and notifications |
| `web` | Dashboard and JSON API | Local user-authored plans, notes, and event state |
| `hyperliquid` | Optional public-data collection and shadow validation | `hyperliquid.db` only |
| `backup` | SQLite online backups and integrity checks | Local backup directory |
| `caddy` | TLS termination and reverse proxy | Certificate state only |

## Read-Only Broker Boundary

The broker boundary is enforced at several layers:

1. The client connects with `readonly=True`.
2. The Gateway container sets `IB_READONLY=yes`.
3. No API route maps to an order, cancellation, exercise, or transfer operation.
4. Recommendations and adopted opportunities create local draft records only.
5. Tests continuously check the absence of order endpoints and side effects.

The recommended deployment also uses a dedicated read-only IBKR user. This protects the account even if application-level safeguards regress.

## Data Quality Before Interpretation

The system prefers an explicit unknown state over a precise-looking but unsupported number.

- Missing account readiness blocks zero snapshots from being written.
- Stale and frozen quotes cannot prove that a stop is safe.
- Delayed data can support lower-severity warnings but not the highest urgency.
- Historical replay reads only information available at the replayed timestamp.
- Every synchronized account/position/quote batch carries a shared `sync_run_id`.
- Optional data sources degrade independently and do not overwrite authoritative IBKR facts.

## Financial Semantics

Three PnL measures must not be added together:

- daily PnL: change during the current trading day;
- realized PnL: profit or loss crystallized by closing executions;
- unrealized PnL: point-in-time cumulative value on open positions.

Three activity counts must not be substituted for one another:

- order: one broker instruction;
- execution: one fill report, including partial fills;
- decision: one human risk-taking action that can contain multiple executions.

Cash flows are imported from Flex Query or entered explicitly. The system does not infer deposits or withdrawals from unexplained equity changes.

## Recommendation Synthesis

Recommendations combine:

- portfolio and position risk;
- market regime and technical structure;
- stop quality and quote quality;
- active position plans and cooldowns;
- provenance references to the exact assessment rows.

Hard risk actions have priority over softer technical evidence. A missing or stale dependency can reduce confidence or block evaluation; it cannot silently turn a risk action into a positive recommendation.

## Opportunity Module

The opportunity service is intentionally staged:

1. `OPPORTUNITY_ENABLED` collects and scores candidates.
2. Successful, non-empty shadow days accumulate readiness evidence.
3. `PORTFOLIO_BUILDER_ENABLED` allows risk-budgeted proposals only after the minimum shadow gate.
4. Adoption creates local draft plans; no broker action is performed.

## Hyperliquid Isolation

Hyperliquid XYZ is an optional research path with a separate database and scheduler executor. Collection, wallet accounting, asset mapping, point-in-time cohorting, signal outcomes, and validation are versioned independently.

The promotion lifecycle is:

```text
SHADOW → ELIGIBLE → ACTIVE
                    └──→ SUSPENDED
```

An `ACTIVE` version requires out-of-sample evidence and manual approval. Even then, on-chain evidence may only tighten an existing recommendation; it cannot create a trade or weaken a risk action.

## Storage and Recovery

- `dashboard.db` stores the primary account, risk, planning, and review state.
- `hyperliquid.db` stores optional public-data research state.
- Both databases use SQLite-compatible online backups and integrity checks.
- Runtime databases, backups, logs, credentials, and generated reports are excluded from the public repository.

## Trust Boundary Summary

| Boundary | Allowed | Forbidden |
|---|---|---|
| IBKR | Read balances, positions, executions, PnL, and market data | Place, modify, cancel, exercise, or transfer |
| Local application | Store plans, notes, acknowledgements, and derived analytics | Treat local drafts as broker execution |
| Optional quote sources | Supply quotes and bars with provenance | Override IBKR account truth |
| AI-compatible brief service | Rephrase validated structured context | Invent numbers, bypass validation, or block deterministic fallback |
| Hyperliquid research | Collect public data and produce shadow evidence | Affect production before validation and manual approval |
