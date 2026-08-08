# Available Tools (30)

Every tool returns a JSON string. On failure the payload contains
`error`, `hint`, and `data_available: false` — tools never raise across
the MCP boundary. Enum parameters are typed (`Literal`) so MCP clients
get real schema enums.

## Connection & Intelligence (10 tools)

| #  | Tool                      | Description |
| -- | ------------------------- | ----------- |
| 1  | `connect_account`         | Connect to a New Relic account by profile name (or via `resolve_account`). Returns `intelligence_source`, `skip_learn_account`, and `naming_convention`. **Never pass `api_key`** — the argument is deprecated; create profiles via `sherlock-validate` |
| 2  | `resolve_account`         | Resolve which account owns a service — call BEFORE `connect_account` to skip the learn cycle if previously indexed |
| 3  | `list_profiles`           | List all saved credential profiles |
| 4  | `learn_account`           | Re-discover account topology. Server-side cache guardrail: `force=False` (default) returns `already_learned` immediately when cached; `force=True` does a full re-learn |
| 5  | `get_account_summary`     | Summary of discovered assets (APM, OTel, K8s, infra, browser, mobile, workloads, ASB) |
| 6  | `get_session_context`     | Investigation history from the current session for follow-up questions |
| 7  | `get_frustration_context` | Detect frustration/retry loops — combines language signals with session retry count to trigger escalation mode |
| 8  | `get_structured_report`   | The most recent investigation as machine-readable JSON (`full`, `summary`, or `metrics` format) |
| 9  | `get_nrql_context`        | Real service/monitor/attribute names for this account — call before constructing any NRQL. The logs domain includes `live_schema`: the ACTUAL attribute keys on `Log` events with sample values (e.g. `entity.name` vs `service.name`, `severity.text` vs `level`), discovered live via `keyset()` + `uniques()` (`discover=false` to skip) |
| 10 | `find_similar_incidents`  | Match current symptoms against the **persistent** incident history (survives restarts): "this looks like the incident from 3 days ago — root cause was X" |

## Composite Investigation (1 tool)

| #  | Tool                  | Description |
| -- | --------------------- | ----------- |
| 11 | `investigate_service` | Full multi-domain investigation in ONE call: golden signals with last-week baseline, recent changes with error-onset correlation, error logs, K8s health, incidents, dependencies, and associated synthetic monitors — run concurrently server-side. Returns one bundle with `overall_status`, per-domain summaries, `root_cause_hint`, and `failed_domains`. Accepts `resource_filter` (see below). **Prefer this to start any investigation** |

## Query (1 tool)

| #  | Tool             | Description |
| -- | ---------------- | ----------- |
| 12 | `run_nrql_query` | Execute any read-only NRQL query. `account_id`/`profile` target another account for one call (see below); string fields longer than `max_field_length` (default 2000) are truncated with a marker — pass `0` for full values |

### Per-Call Account Targeting

`run_nrql_query` and `search_logs` accept optional `account_id` or
`profile` arguments that resolve credentials from the saved keyring
profiles for THAT ONE CALL, without touching the active connection. This
is built for cross-account correlation (e.g. a request enters a service
in account A and lands in account B): instead of a full
`connect_account` round-trip (~5s) per hop, each query names its target.
The response carries `account_override: true` when an override was used.
Cached intelligence for the target account is used opportunistically;
accounts never learned fall back to default attribute spellings plus the
fallback ladder.

### Sub-Entity Resource Filtering (`resource_filter`)

Some telemetry sources — notably **Azure Function Apps** — report several
deployed apps under ONE New Relic entity (same name, same GUID); only
`cloud.resource_id` tells them apart. Service-scoped tools accept an
optional `resource_filter` argument (a `cloud.resource_id` substring,
e.g. `func-dfin-eus2-dev-gail-v2-di`) that scopes every underlying NRQL
query to the matching sub-resource:

- `investigate_service` (applies to golden signals + logs domains)
- `get_service_golden_signals` (all signals, timeseries, baseline, top errors)
- `search_logs` (all attribute-fallback variants; works without `service_name` too)
- `get_app_metrics`
- `get_slow_traces`

The filter value is sanitized before embedding and echoed back as
`resource_filter` in the response. K8s/incident/dependency tools ignore
it — those operate at entity level.

## APM & Performance (4 tools)

| #  | Tool                   | Description |
| -- | ---------------------- | ----------- |
| 13 | `get_apm_applications` | List all APM-instrumented applications |
| 14 | `get_app_metrics`      | Key metrics for a specific application (accepts `resource_filter`) |
| 15 | `get_deployments`      | Recent deployment history/metadata for an application |
| 16 | `get_recent_changes`   | "What changed?" — ONE timeline of APM deployment markers + K8s rollout events, auto-correlated against the service's error-rate onset ("deploy X landed 4m before the spike — prime suspect") |

### APM Entity Disambiguation

When multiple APM reporters share a `name` (migrations, multi-cluster
shared services), Sherlock preserves all candidates in
`intel.apm.service_guid_candidates[name]` alongside the preferred GUID in
`intel.apm.service_guids[name]`. The `resolve_apm_guid()` helper in
`sherlock/core/deeplinks.py` returns `None` when the GUID is ambiguous,
and response builders omit the entity-view link rather than attaching a
potentially-wrong GUID. Chart links (which filter by `appName` string)
remain available.

## Distributed Traces (2 tools)

| #  | Tool              | Description |
| -- | ----------------- | ----------- |
| 17 | `get_slow_traces` | The slowest distributed traces touching a service (APM or OTel spans), with percentile context ("4.2s = 8.4x the p95"). `only_errors=true` finds failing traces instead. Accepts `resource_filter` |
| 18 | `summarize_trace` | Summarize ONE trace: total duration, per-service self-time breakdown, errored spans, and the **hot span** (largest self-time) — "72% of this trace is one SELECT in payment-svc" |

## Golden Signals (1 tool)

| #  | Tool                         | Description |
| -- | ---------------------------- | ----------- |
| 19 | `get_service_golden_signals` | Latency, errors, traffic, saturation with trend timeseries, plus a `baseline` comparison vs the same window LAST WEEK (delta percentages and verdicts like "🔴 error rate 24x last week"). Accepts `resource_filter` |

## Logs (1 tool)

| #  | Tool          | Description |
| -- | ------------- | ----------- |
| 20 | `search_logs` | Search logs by service, severity, keyword, time window. Includes `log_patterns`: drain-style template clusters that collapse thousands of repetitive messages into distinct patterns with counts — read those before the raw lines. Accepts `resource_filter`, `account_id`/`profile` per-call targeting, and `max_field_length` truncation (patterns are computed from FULL messages before truncation) |

## Infrastructure & Kubernetes (1 tool)

| #  | Tool             | Description |
| -- | ---------------- | ----------- |
| 21 | `get_k8s_health` | K8s health — pods, restarts, resource usage, deployments, events (with deep links). Namespace auto-resolved via NamingConvention; server-side guardrail overrides a wrong client-provided namespace |

### Multi-Cluster K8s Awareness

`get_k8s_health` detects multi-cluster accounts and adapts via a 4-mode
cluster resolution:

| Mode        | Condition                                   | Behavior |
| ----------- | ------------------------------------------- | -------- |
| `none`      | 0 clusters known                            | Legacy behavior — no cluster filter |
| `single`    | 1 cluster known, no `cluster_name` param    | Auto-filters to that cluster |
| `explicit`  | `cluster_name` param provided               | Filters to the specified cluster (fuzzy-resolved) |
| `breakdown` | 2+ clusters known, no `cluster_name` param  | Per-cluster FACET — every signal prefixed with `[cluster-name]` |

Response additions: `cluster_mode`, `cluster_name` (null in breakdown),
`clusters_known`, `links_by_cluster` (breakdown only).

## Alerts & Incidents (3 tools)

| #  | Tool                    | Description |
| -- | ----------------------- | ----------- |
| 22 | `get_alerts`            | List alert policies and their conditions (account-wide) |
| 23 | `get_incidents`         | List incidents filtered by state (open/closed), with deep links |
| 24 | `get_service_incidents` | Incidents for a specific service (fuzzy name resolution) |

## Synthetics (4 tools)

| #  | Tool                     | Description |
| -- | ------------------------ | ----------- |
| 25 | `get_synthetic_monitors` | List all synthetic monitors with metadata |
| 26 | `get_monitor_status`     | Deep health check — per-location success rates, diagnosis codes |
| 27 | `get_monitor_results`    | Recent check results for a monitor |
| 28 | `investigate_synthetic`  | Full investigation — monitor health + APM correlation + recommendations |

## Service Dependencies (1 tool)

| #  | Tool                       | Description |
| -- | -------------------------- | ----------- |
| 29 | `get_service_dependencies` | Upstream and downstream dependencies with call counts, error rates, latency, confidence scores, and health warnings |

## Team Output (1 tool)

| #  | Tool                     | Description |
| -- | ------------------------ | ----------- |
| 30 | `post_report_to_webhook` | Post the latest investigation report to the team's Slack or Teams channel via the operator-configured incoming webhook (`SHERLOCK_SLACK_WEBHOOK_URL` / `SHERLOCK_TEAMS_WEBHOOK_URL`). The tool takes no URL argument by design — see [security.md](security.md#webhook-output-safety) |

## Deep Link Routing

Sherlock distinguishes two categories of deep links in tool responses:

**Entity views** (primary recommendations) — open the canonical NR1 view
for an entity: APM service overview, K8s deployment detail, Logs UI.
Response dict keys: `service_overview`, `errors_inbox`, `workload_view`,
`view_in_nr`.

**NRQL queries and charts** (supporting evidence) — open the NRQL query
builder pre-loaded with the query Sherlock used. Response dict keys:
`error_chart`, `latency_chart`, `restart_chart`, `view_nrql`.

The Team Lead synthesis prefers entity-view links for top-level
recommendations and uses chart links only when the chart itself is the
evidence being cited.
