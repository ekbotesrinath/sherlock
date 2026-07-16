# Features & Workflows

How Sherlock's intelligence features work, and the prompts that exercise
them.

## Workflows

### One-Call Deep Investigation (sweep-first)

```
User: "Investigate the checkout service — it seems slow"
→ investigate_service("checkout") sweeps ALL domains concurrently server-side:
  golden signals (+ last-week baseline), recent changes (+ error-onset
  correlation), error logs (+ patterns), K8s health, incidents,
  dependencies, associated synthetics
→ Returns ONE bundle: overall_status, per-domain verdicts, root_cause_hint
→ Team Lead dispatches specialist agents only for the flagged domains
```

### Quick Health Check

```
User: "How is web-api performing?"
→ get_service_golden_signals("web-api")
→ latency p50/p99, error rate, throughput, saturation with threshold alerts,
  plus baseline verdicts vs the same window last week
```

### What Changed?

```
User: "Errors started at 14:30 — did we deploy something?"
→ get_recent_changes("payment-svc", since_minutes=240)
→ ONE timeline of APM deployment markers + K8s rollout events, correlated
  against the error-rate onset:
  "🎯 deploy of payment-svc (rev abc123) landed 4m before the error onset — prime suspect."
```

### Slow-Request Forensics

```
User: "Why are some checkout requests taking 5 seconds?"
→ get_slow_traces("checkout", since_minutes=60)
→ slowest distributed traces with vs-p95 context ("4.2s = 8.4x the p95")
→ summarize_trace(trace_id)
→ "🔥 hot span: 'SELECT orders' in payment-svc — 1800ms self-time (72% of the trace)"
```

### Have We Seen This Before?

```
User: "checkout is throwing 503s again"
→ find_similar_incidents(service_name="checkout", symptoms="503 errors DB pool")
→ Matches against the persistent incident history (survives restarts):
  "Similar incident 3 days ago — root cause: DB connection pool exhausted"
```

### Share to the Team Channel

```
User: "Post that report to the team channel"
→ post_report_to_webhook(target="slack")
→ Latest investigation formatted as Block Kit and delivered to the
  operator-configured incoming webhook
```

### Proactive Health Digest (cron)

```shell
# Sweep every known service of every profile; post to Slack; exit 2 if
# anything is CRITICAL (cron/CI can alert on it)
sherlock-digest --post slack
sherlock-digest --profile PROD --window 60 --json
```

### Other prompts

| Prompt | Tool(s) |
| ------ | ------- |
| "What does the payment service depend on?" | `get_service_dependencies` (direction, max_depth) |
| "Why is the Login Flow monitor failing?" | `investigate_synthetic` → diagnosis + APM correlation |
| "Switch to the staging account" | `connect_account("staging")` — cached intelligence skips the learn cycle |
| "Top 10 slowest transactions last hour" | `run_nrql_query` (after `get_nrql_context`) |
| "Is checkout still degraded?" | `get_session_context` — compares against the prior snapshot |
| "Why is this STILL not working?!" | `get_frustration_context` → `mode: ESCALATION` |
| "Give me the JSON report" | `get_structured_report(format="full")` |
| "Check the message health of our service bus" | ASB intelligence from `learn_account` |

---

## Change Correlation (`get_recent_changes`)

Answers the first question of any incident: **"what changed?"**

* **Deployment markers** — APM `Deployment` events (change-tracking API)
* **K8s rollouts** — `InfrastructureEvent` rows from the kube-events
  integration (ScalingReplicaSet, SuccessfulCreate/Rescale, DeploymentRollback),
  namespace-scoped via the account's naming convention
* **Error-onset detection** — the service's error-rate timeseries is
  scanned for the first sustained spike (≥5% and ≥3x the pre-onset median)
* **Correlation** — changes landing 0–60 min before the onset are flagged;
  the nearest becomes the **prime suspect** verdict

## Baseline Comparison (golden signals)

`get_service_golden_signals` runs an NRQL `COMPARE WITH 1 week ago` query
alongside the live signals and returns a `baseline` key with per-metric
`delta_pct` and verdicts ("🔴 error rate 24x last week", "🔴 traffic -90%
vs last week — possible upstream outage"). A metric can look normal in
absolute terms while being wildly abnormal vs its weekly baseline.
Baseline failures never block the main response.

## Distributed Traces (`get_slow_traces` / `summarize_trace`)

* `get_slow_traces` facets `Span` events by `trace.id` (APM `appName` OR
  OTel `entity.name`), slowest first, with the service's p50/p95/p99 span
  durations for context. `only_errors=true` filters to traces containing
  an errored span.
* `summarize_trace` fetches every span of one trace and computes
  **self-time** per span (duration minus direct children, clamped at 0
  for async overlap). The span with the largest self-time is the **hot
  span**; the response includes a per-service breakdown, errored spans,
  and verdict lines ready for the report.

## Log Pattern Fingerprinting (`search_logs`)

`search_logs` responses include `log_patterns`: drain-style template
clusters computed server-side (stdlib only). Variable tokens (UUIDs,
timestamps, IPs, emails, hex ids, paths, numbers) are masked, messages
are bucketed by token count, and similar templates merge at ≥0.5
positional similarity. Ten thousand repetitive lines collapse to a
handful of `{template, count, percentage, example}` entries — agents read
the patterns before the raw lines.

## Persistent Incident Memory (`find_similar_incidents`)

Every recorded investigation snapshot is appended to
`.sherlock/history.jsonl` (compacted to the newest 1000). Unlike session
memory, this **survives restarts**. `find_similar_incidents` scores past
incidents on service-name match, symptom keywords vs recorded root
causes, and known causal patterns — surfacing "this looks like the
incident from 3 days ago; root cause was X" at the start of an
investigation.

## Session Memory

In-memory record of investigations within the current session (with
persistent copies going to incident history — see above). Every completed
investigation stores an `InvestigationSnapshot`: severity, root cause,
causal chain/pattern, error rate, OTel flag, incident ids, chronic/stale
flags.

| Engineer Says                | What Happens |
| ---------------------------- | ------------ |
| "Is X still degraded?"       | `get_session_context` retrieves the last snapshot; if < 30 min old, compares against a quick live check |
| "Why did that happen again?" | Pulls root cause from the prior investigation of that service |
| "Check the same service"     | Uses the last investigated service name from context |
| "Has it improved?"           | Compares current quick check against prior snapshot severity |

## Frustration Detection & Escalation

Sherlock detects when an engineer is stuck in a retry loop and escalates
the investigation strategy.

| Signal Type     | Examples |
| --------------- | -------- |
| **Language**    | "still broken", "why is this still failing", "checked 3 times", "nothing works" |
| **Retry count** | Same service investigated ≥ 2 times within 20 minutes |

When `get_frustration_context` returns `mode: ESCALATION`: wider time
windows, changed query strategy (avoid repeating empty queries),
cross-account GUID check, and explicit acknowledgment of the retries.

## Structured Output & Webhooks

`get_structured_report` returns the typed `InvestigationReport` JSON in
three formats:

| Format    | Contents                                                       | Use Case |
| --------- | -------------------------------------------------------------- | -------- |
| `full`    | All fields: severity, domains, causal chain, recommendations   | MTTR dashboards, ticketing |
| `summary` | Verdict + root cause only                                      | Quick comparisons |
| `metrics` | Numeric values only                                            | Charting, alerting pipelines |

`post_report_to_webhook` (or `sherlock-digest --post`) formats reports as
Slack Block Kit / Teams MessageCard and delivers them to the
operator-configured webhook — see [security.md](security.md#webhook-output-safety).

## Proactive Health Digest (`sherlock-digest`)

The digest script is the "morning coffee" view of whole accounts:

1. Connects each saved profile (or `--profile NAME`)
2. Sweeps every known APM + OTel service through the golden-signals check
   (bounded concurrency, `--concurrency 5`, cap `--max-services 50`)
3. Prints a worst-first digest: critical/warning services with their
   health signals and non-green baseline verdicts; healthy services
   collapsed to a count
4. `--post slack|teams` delivers the digest to the team webhook;
   `--json` emits the machine-readable bundle
5. Exit codes: `0` ok, `1` config/delivery failure, `2` something is
   CRITICAL — wire it to cron and alert on exit 2

## Synthetics Deep-Dive

**Monitor discovery** — during `learn_account`: name, GUID, type,
enabled status, locations, period, associated APM service (if tagged).

**Diagnosis codes** from `get_monitor_status`:

| Code                   | Meaning |
| ---------------------- | ------- |
| `PASSING`              | All locations succeeding, response times normal |
| `INTERMITTENT`         | Some checks failing sporadically across locations |
| `REGIONAL_FAILURE`     | Specific locations consistently failing |
| `GLOBAL_FAILURE`       | All locations failing — likely a service outage |
| `DEGRADED_PERFORMANCE` | Checks passing but response times elevated |

**APM correlation** in `investigate_synthetic`:

* Global failure + APM errors → service-side root cause
* Global failure + APM healthy → network/DNS/CDN issue
* Regional failure → regional infrastructure problem
* Degraded performance + APM latency → upstream dependency slowdown

**Fuzzy monitor resolution** uses a 0.5 token-overlap threshold (more
lenient than the 0.6 used for services, since monitor names are more
descriptive).

## Service Dependencies

The dependency graph is built during `connect_account` from three
strategies, merged in priority order:

| Strategy       | Confidence | Source |
| -------------- | ---------- | ------ |
| **Span-based** | 1.0        | `Span` events — `peer.service.name`, `http.url` |
| **Log-based**  | 0.7        | Log error messages containing service references |
| **Inferred**   | 0.4        | Shared naming segments between services |

Graphs persist under `.sherlock/deps/` with a 24-hour staleness TTL —
stale graphs remain usable but are flagged. `get_service_dependencies`
supports `direction` (upstream/downstream/both) and `max_depth`.

## Azure Service Bus Discovery

`learn_account` discovers ASB resources from New Relic's Azure
integration events — no hardcoded namespaces:

| Resource   | Event Type                       | Key Metrics |
| ---------- | -------------------------------- | ----------- |
| Queues     | `AzureServiceBusQueueSample`     | `activeMessages`, `deadLetterMessages`, `incomingMessages` |
| Topics     | `AzureServiceBusTopicSample`     | `incomingMessages` |
| Namespaces | `AzureServiceBusNamespaceSample` | Namespace names |

Intelligence output: namespace list, queue/topic inventories, DLQ alerts,
inferred naming patterns. During investigations agents correlate: DLQ
spike + service errors → messages failing to process; active-message
growth + healthy consumers → producer outpacing consumers; zero incoming
→ producer may be down.

> ASB discovery requires the New Relic Azure integration. Without
> `AzureServiceBusQueueSample` data, `learn_account` reports
> `⚪ Azure Service Bus: not configured`.

## OTel Service Detection

Accounts mix **APM agent** services and **OpenTelemetry** services, which
use different event types:

| Type     | Event Table                       | Name Attribute                | Error Attribute |
| -------- | --------------------------------- | ----------------------------- | --------------- |
| **APM**  | `Transaction`, `TransactionError` | `appName`                     | `error IS true` |
| **OTel** | `Span`, `Log`                     | `entity.name`, `service.name` | `otel.status_code = 'ERROR'` |

Golden signals and app metrics probe both and fall back automatically:
`FROM Transaction` → 0 results → `FROM Span`. Responses are tagged
`is_otel: true` so downstream queries use the right attributes. The trace
tools query both attributes in one `WHERE` clause.

## Zero-Result Fallback Protocol

Every domain agent follows a mandatory multi-attempt fallback ladder
before reporting NO_DATA — preventing false negatives from wrong event
types, name formats, or windows.

| Domain    | Min Attempts | Fallback Strategy |
| --------- | ------------ | ----------------- |
| **APM**   | 2            | `Transaction` → `Span` (OTel fallback) |
| **K8s**   | 5            | `get_k8s_health` → `deploymentName LIKE` → `podName LIKE` → `label.app LIKE` → `namespaceName =` |
| **Logs**  | 3            | `appName` → `entity.name` → bare-name wildcard |
| **Infra** | 2            | `Metric` → `Log`-based fallback |

When all fallbacks are exhausted, agents report structured NO_DATA
listing exactly what was tried:

```json
{
  "domain": "k8s",
  "tried": ["get_k8s_health (0 results)", "deploymentName LIKE (0 results)"],
  "fallbacks_exhausted": true,
  "cross_account_suspected": false,
  "recommendation": "Verify K8s integration is configured for this namespace"
}
```

### Platform Log Discovery (tenant-agnostic)

`search_logs` auto-discovers Kubernetes platform namespaces
(istio-system, ingress-nginx, kube-system, …) during `learn_account` and
queries them when a platform-component investigation returns zero results
from service-attribute searches. Works across log-forwarder conventions
(Fluent Bit, OTel k8sattributes, Fluentd).

Discovered data in `learn_account` output: `logs.namespace_attribute`,
`logs.cluster_attribute`, `logs.platform_namespaces`,
`logs.all_discovered_namespaces` (cap 50). Platform-sourced responses set
`platform_log_source: true` so agents apply Envoy response-flags
interpretation.
