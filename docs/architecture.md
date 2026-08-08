# Architecture

Sherlock is a stdio MCP server (FastMCP) exposing 30 read-only New Relic
tools, plus a GitHub Copilot **agent team** that consumes them.

```
┌─────────────────────────────────────────────────────────────┐
│                    AI Assistant (LLM)                        │
│              (GitHub Copilot / Claude / Cursor)              │
└───────────────────────────┬─────────────────────────────────┘
                            │ stdio (MCP protocol)
┌───────────────────────────▼─────────────────────────────────┐
│                    sherlock/main.py                          │
│  ┌──────────────┐  ┌──────────────┐  ┌────────────────┐     │
│  │ FastMCP tool │  │ Audit Logger │  │ Response Scrub │     │
│  │ registry     │  │ (JSON lines) │  │ (injection)    │     │
│  └──────┬───────┘  └──────────────┘  └────────────────┘     │
│         │                                                    │
│  ┌──────▼──────────────────────────────────────────────────┐ │
│  │                 sherlock/tools/                          │ │
│  │  intelligence_tools │ investigate │ golden_signals      │ │
│  │  changes │ traces │ logs │ k8s │ apm │ alerts           │ │
│  │  synthetics │ dependencies │ nrql │ entities            │ │
│  │  history_tools │ webhook_tools                          │ │
│  └──────┬──────────────────────────────────────────────────┘ │
│         │                                                    │
│  ┌──────▼──────────────────────────────────────────────────┐ │
│  │                 sherlock/core/                           │ │
│  │  context │ credentials │ intelligence (+learners/)      │ │
│  │  nrql_executor │ sanitize │ fingerprint │ cache         │ │
│  │  deeplinks │ dependency_graph │ graph_builder           │ │
│  │  session_memory │ incident_history │ structured_output  │ │
│  │  webhook │ account_memory │ report_template │ naming    │ │
│  └──────┬──────────────────────────────────────────────────┘ │
│         │                                                    │
│  ┌──────▼──────────────────────────────────────────────────┐ │
│  │                sherlock/client/                          │ │
│  │  NerdGraphClient — pooled httpx, tenacity retries,      │ │
│  │  GraphQL parse → read-only enforcement, 429 handling    │ │
│  └─────────────────────────────────────────────────────────┘ │
└──────────────────────────────────────────────────────────────┘
                            │
                     NerdGraph GraphQL API
               US: https://api.newrelic.com/graphql
               EU: https://api.eu.newrelic.com/graphql
```

## Layer Responsibilities

| Layer               | Purpose |
| ------------------- | ------- |
| `sherlock/main.py`  | FastMCP server: tool registry (`_TOOL_SPECS`), schema generation from type hints, `_instrumented()` wrapper adding scrub + audit + never-raise JSON error contract |
| `sherlock/tools/`   | One module per domain — each tool returns a JSON string and degrades gracefully |
| `sherlock/core/`    | Shared primitives — credentials, context singleton, account intelligence + per-domain learners, single NRQL executor, sanitization, caching, deep links, session/incident memory, webhook delivery |
| `sherlock/client/`  | HTTP transport — NerdGraph client with pooled connections, retry/backoff, and parse-based read-only enforcement |
| `sherlock/scripts/` | CLI entry points: `sherlock-cli`, `sherlock-validate`, `sherlock-relearn`, `sherlock-digest` |

Key mechanics:

* **Tool schemas from type hints** — FastMCP derives input schemas from
  each handler's signature; enum parameters are `Literal[...]`, defaults
  come from keyword defaults.
* **One NRQL path** — every tool runs NRQL through
  `core/nrql_executor.execute_nrql()` (and `execute_nrql_compare()` for
  `COMPARE WITH` baselines), which sends NRQL as a typed GraphQL variable.
* **Account intelligence** — `learn_account` fans out to 7 concurrent
  learners (`core/learners/`: apm, k8s, logs, synthetics, alerts, asb,
  entities) then runs cross-domain passes (monitor→APM association,
  naming-convention inference, cross-account detection).
* **Pooled HTTP** — one `httpx.AsyncClient` per event loop with keep-alive;
  429s honor `Retry-After` exactly.

## Agent-Team Investigation Flow (sweep-first)

For "investigate service X" requests, the team defined in
`.github/agents/` + `.github/skills/` runs **sweep-first**: the composite
`investigate_service` tool sweeps all domains server-side in one call,
then the Team Lead dispatches specialist agents only for flagged domains.

```
User: "investigate service X"
  │
  ▼
sherlock-team-lead (orchestrator)
  ├── resolve_account / connect_account (if needed)
  ├── find_similar_incidents(service, symptoms) → past-incident recall
  ├── investigate_service(X) ── ONE call, concurrent server-side sweep:
  │     golden signals (+ last-week baseline) │ recent changes (+ onset
  │     correlation) │ error logs (+ patterns) │ K8s │ incidents │
  │     dependencies │ associated synthetics
  │
  ├── TARGETED DISPATCH (only domains the sweep flagged) ─────
  │   ├── sherlock-apm ──────→ get_service_golden_signals (baseline),
  │   │                        get_recent_changes, get_slow_traces →
  │   │                        summarize_trace (hot span)
  │   ├── sherlock-k8s ──────→ get_k8s_health, NRQL fallbacks
  │   ├── sherlock-logs ─────→ search_logs (log_patterns first)
  │   ├── sherlock-alerts ───→ get_service_incidents, find_similar_incidents
  │   ├── sherlock-synthetics → get_monitor_status, investigate_synthetic
  │   └── sherlock-infra ────→ get_service_dependencies
  │
  ▼
sherlock-team-lead (synthesize flagged-domain findings + sweep bundle)
  │
  ▼
Unified Investigation Report (+ optional post_report_to_webhook)
```

| Agent                 | Specialty                                          | Primary Tools |
| --------------------- | -------------------------------------------------- | ------------- |
| `sherlock-team-lead`  | Orchestrator, synthesizer                          | `investigate_service`, `find_similar_incidents`, `connect_account`, `get_nrql_context` |
| `sherlock-apm`        | Golden signals, transactions, deployments, traces  | `get_service_golden_signals`, `get_recent_changes`, `get_slow_traces`, `summarize_trace`, `get_app_metrics` |
| `sherlock-k8s`        | Pods, containers, nodes, K8s events                | `get_k8s_health`, `run_nrql_query` |
| `sherlock-logs`       | Error patterns, log volume, exception analysis     | `search_logs` (log_patterns), `run_nrql_query` |
| `sherlock-alerts`     | Active incidents, alert policies, violations       | `get_service_incidents`, `get_incidents`, `get_alerts`, `find_similar_incidents` |
| `sherlock-synthetics` | Monitor health, failure locations, availability    | `get_synthetic_monitors`, `get_monitor_status`, `investigate_synthetic` |
| `sherlock-infra`      | Dependencies, host health, browser, messaging      | `get_service_dependencies`, `run_nrql_query` |

## Report Template Enforcement

The team-lead agent's investigation report follows a strict template
documented in `.github/agents/sherlock-team-lead.agent.md` §Report
Template — ENFORCEMENT RULES. `sherlock/core/report_template.py` exposes
`validate_report_markdown(md)` as an optional self-check the agent can
run on its draft before sending. This is an advisory lint, not a runtime
gate.

## Project Structure

```
sherlock/                        # repo root
├── pyproject.toml               # build config; wheel packages sherlock/
├── Makefile                     # development commands
├── .env.example
├── sherlock/                    # THE package (everything importable)
│   ├── main.py                  # FastMCP server entry (30 tools)
│   ├── paths.py                 # all .sherlock/ path resolution (SHERLOCK_HOME)
│   ├── client/
│   │   └── newrelic.py          # NerdGraph client (pooled, read-only, retries)
│   ├── core/
│   │   ├── intelligence.py      # learn orchestrator (re-exports models/helpers)
│   │   ├── intelligence_models.py
│   │   ├── learners/            # per-domain learners (apm, k8s, logs, ...)
│   │   ├── naming.py            # naming-convention inference
│   │   ├── nrql_executor.py     # THE NRQL execution path
│   │   ├── sanitize.py          # fuzzy resolution + injection scrubbing
│   │   ├── fingerprint.py       # drain-style log template clustering
│   │   ├── session_memory.py    # in-session snapshots (persists via history)
│   │   ├── incident_history.py  # persistent .sherlock/history.jsonl
│   │   ├── structured_output.py # InvestigationReport schema
│   │   ├── webhook.py           # Slack/Teams payloads + delivery
│   │   ├── credentials.py       # keyring profiles + backend hardening
│   │   ├── target.py            # per-call account targeting (cross-account)
│   │   ├── context.py │ cache.py │ deeplinks.py │ deps/graph modules ...
│   ├── tools/                   # one module per domain (see tools.md)
│   └── scripts/
│       ├── cli.py               # sherlock-cli — run any tool interactively
│       ├── validate_connection.py  # sherlock-validate — create profiles
│       ├── relearn.py           # sherlock-relearn — refresh intelligence
│       └── digest.py            # sherlock-digest — proactive health digest
├── .github/
│   ├── copilot-instructions.md  # agent-team rules, report format
│   ├── agents/                  # 7 agent definitions
│   ├── skills/                  # 8 domain skills
│   └── workflows/ci.yml         # pytest (3.11+3.12), ruff, mypy, gitleaks, pip-audit
├── docs/                        # this documentation tree
├── tests/                       # 800+ tests, pytest -n auto
└── .sherlock/                   # runtime state (gitignored; see security.md)
    ├── profiles.json │ cache/ │ deps/ │ account_memory.json
    ├── history.jsonl            # persistent incident memory (newest 1000)
    └── logs/                    # sherlock.log + audit.log (JSON lines)
```

## Key Design Patterns

* **All tool functions are `async`** — NerdGraph calls are awaited;
  multi-query tools use `asyncio.gather` with `return_exceptions=True`
* **Fuzzy name resolution** — `fuzzy_resolve_service()` /
  `fuzzy_resolve_monitor()` for every user-facing name input, with
  environment-mismatch warnings
* **Context access** — `AccountContext().get_active()` returns
  (credentials, intelligence) for the active profile
* **Never raise** — tools return `json.dumps({...})` with
  `error`/`hint`/`data_available` on failure; the `_instrumented()`
  wrapper is the safety net
* **Line length 100, Google docstrings, double quotes** — enforced by ruff;
  `mypy sherlock` is blocking in CI

## Adding a New Tool

1. Create the async handler in `sherlock/tools/` (full type hints — the
   MCP schema derives from them; `Literal` for enums)
2. Register it in `sherlock/main.py` `_TOOL_SPECS` **and**
   `sherlock/scripts/cli.py` `TOOL_MAP`
3. Bump the tool-count assertion in `tests/test_dispatcher.py`
4. Add tests in `tests/` (respx-mocked NerdGraph; see any recent
   `test_*.py` for the pattern)
5. Update `docs/tools.md` and, if agent-facing, the agent/skill files
