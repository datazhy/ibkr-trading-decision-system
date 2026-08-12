## Summary

Describe what changed and why.

## Safety and Data Impact

- [ ] Broker access remains read-only; no order, cancellation, exercise, or transfer capability was added.
- [ ] No real account data, positions, executions, wallet watchlists, credentials, hostnames, logs, databases, or generated reports are included.
- [ ] Financial definitions and data-source precedence remain explicit.
- [ ] Optional integrations degrade without blocking the primary IBKR path.

## Validation

- [ ] `python -m pytest -q`
- [ ] `git diff --check`
- [ ] English and Chinese documentation were updated together when needed.

Add exact test results and any deployment or migration notes.
