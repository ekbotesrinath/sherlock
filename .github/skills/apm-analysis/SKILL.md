---
name: apm-analysis
description: >
  Deep APM analysis for New Relic APM. Golden signals deep-dive, transaction
  performance analysis, error classification, deployment correlation, and
  SLA/Apdex assessment.
---

# APM Analysis — Sherlock APM Playbook

> Deep APM investigation using New Relic APM via Sherlock MCP tools.
> Use the sherlock-apm agent for execution.

---

## Investigation Steps

### Step 1 — Golden Signals (with vs-last-week baseline)

```
mcp_sherlock_get_service_golden_signals(service_name, since_minutes)
```

The response includes a `baseline` field (NRQL COMPARE WITH 1 week ago):
per-metric `delta_pct` and verdicts like "🔴 error rate 24x last week" or
"🔴 traffic -85% vs last week — possible upstream outage". Report every
non-✅ baseline verdict — a metric can look fine in absolute terms while
being wildly abnormal vs its weekly baseline.

### Step 2 — Change Correlation

```
mcp_sherlock_get_recent_changes(service_name, since_minutes=240)
```

One timeline of APM deployment markers + K8s rollout events, auto-correlated
against the error-rate onset. If `correlation.verdict` contains "prime
suspect", quote it verbatim ("deploy X landed 4m before the error onset") —
do not re-derive timing by eye. For full deployment history/metadata:

```
mcp_sherlock_get_deployments(app_name)
```

### Step 3 — Error Breakdown

```sql
SELECT count(*) FROM TransactionError
WHERE appName = '{service}'
FACET error.class, error.message
SINCE 1 hour ago
LIMIT 20
```

### Step 4 — Slow Transaction Analysis

```sql
SELECT average(duration), percentile(duration, 95, 99)
FROM Transaction
WHERE appName = '{service}'
FACET name
SINCE 1 hour ago
ORDER BY average(duration) DESC
LIMIT 10
```

### Step 5 — Throughput Trend

```sql
SELECT rate(count(*), 1 minute)
FROM Transaction
WHERE appName = '{service}'
TIMESERIES
SINCE 2 hours ago
```

### Step 6 — External Service Impact

```sql
SELECT average(duration), count(*)
FROM Transaction
WHERE appName = '{service}'
FACET externalCallName
SINCE 1 hour ago
ORDER BY average(duration) DESC
```

---

## Thresholds

| Metric | HEALTHY | WARNING | CRITICAL |
|--------|---------|---------|----------|
| Error Rate | <1% | 1-5% | >5% |
| P95 Latency | <1s | 1-5s | >5s |
| P99 Latency | <2s | 2-10s | >10s |
| Throughput change | Stable | -30% | -50% |
| Apdex | >0.9 | 0.7-0.9 | <0.7 |
