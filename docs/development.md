# Developer Guide

See [architecture.md](architecture.md) for the repo layout, layer
responsibilities, and the checklist for adding a new tool.

## Running Tests

```shell
# All tests (parallel via pytest-xdist)
make test
# or: pytest tests/ -q

# Fast tests (stop on first failure)
make test-fast

# Domain-specific tool tests (golden signals, K8s, APM, logs, alerts, synthetics)
make test-domain-tools

# Focused suites
make test-synthetics
make test-dependencies
make test-deeplinks
make test-session-memory
make test-structured-output
make test-frustration
make test-asb

# With coverage report
make test-cov
```

Test conventions worth knowing:

* NerdGraph is mocked with `respx` — no test touches the network
* `tests/conftest.py` zeroes retry waits (the suite stays around a
  minute) and redirects the persistent incident history to a tmp path so
  `SessionMemory.record()` never writes into the repo
* `tests/test_dispatcher.py` materializes the FastMCP tool list and
  asserts the exact tool count — bump it when adding tools
* The `mock_context` fixture provides a connected `AccountContext` with a
  fully populated fake intelligence

## Linting, Formatting, Types

```shell
make lint          # ruff check . && mypy .
make format        # ruff format .

# What CI enforces (all blocking):
ruff check .
ruff format --check .
mypy sherlock --ignore-missing-imports
```

Style: line length 100 (E501 on), Google docstrings, double quotes.

## CI

`.github/workflows/ci.yml` runs on every push/PR: pytest on Python 3.11
and 3.12, ruff (lint + format check), mypy (blocking), gitleaks (secret
scanning), and pip-audit (dependency vulnerabilities).

## Console Scripts & Make Targets

```shell
make run                          # python -m sherlock.main (the MCP server)
make cli                          # interactive CLI for all 30 tools
make cli ARGS="--tool get_alerts" # single-shot tool run
make connect                      # create/validate a credential profile
make relearn                      # refresh intelligence (all profiles)
make relearn PROFILE=my-profile   # ... one profile
make relearn-list                 # list available profiles
make digest                       # proactive health digest (all profiles)
make digest ARGS="--profile PROD --post slack"
make clean                        # remove __pycache__ directories
```

Installed console scripts (from the wheel): `sherlock`, `sherlock-cli`,
`sherlock-relearn`, `sherlock-validate`, `sherlock-digest`.

## Logs & State

```shell
make logs      # tail .sherlock/logs/sherlock.log (JSON lines)
make audit     # tail .sherlock/logs/audit.log

ls .sherlock/  # cache/ deps/ profiles.json account_memory.json history.jsonl logs/
```

All state paths resolve through `sherlock/paths.py`; set `SHERLOCK_HOME`
to relocate the `.sherlock/` directory.

## Cache Management

```shell
# Intelligence cache:
ls .sherlock/cache/

# Force re-learn (bypasses the cache guardrail)
# In Copilot Chat: "re-learn the account"  (learn_account with force=True)
make relearn PROFILE=my-profile
```

## Troubleshooting

| Issue                    | Solution |
| ------------------------ | -------- |
| `NotConnectedError`      | Run `connect_account` first |
| `CredentialError`        | Check API key format (`NRAK-...`) and region (US/EU) |
| `CredentialError` mentioning keyring backend | Weak/no OS keychain — see [security.md](security.md#keyring-backend-hardening) |
| `ServiceNotFoundError`   | Verify service name with `get_apm_applications` |
| `MonitorNotFoundError`   | Verify monitor name with `get_synthetic_monitors` |
| `ReadOnlyViolation`      | Mutation attempted — this server is read-only by design |
| `Timeout on investigate` | Check network; individual domains degrade gracefully and appear in `failed_domains` |
| Webhook not delivering   | Set `SHERLOCK_SLACK_WEBHOOK_URL` / `SHERLOCK_TEAMS_WEBHOOK_URL` (https) in the server environment |
