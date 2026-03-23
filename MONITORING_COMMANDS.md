# Sherlock — Monitoring Tool Commands

Quick reference for all `make` commands grouped by monitoring tool.

---

## New Relic

| Command | What it does |
|---------|-------------|
| `make newrelic-connect` | Test New Relic connectivity (reads from `.env`) |
| `make relearn` | Re-discover all entities in the account |
| `make logs` | Tail Sherlock's own log file (pretty-printed JSON) |
| `make audit` | Tail the audit log (tool calls / responses) |

**Credentials required** — set in `.env`:
```
NEW_RELIC_ACCOUNT_ID=<your account id>
NEW_RELIC_API_KEY=NRAK-...
NEW_RELIC_REGION=US
```

---

## Azure Monitor

Three authentication methods are available — choose based on what you have access to.

### Option A — Service Principal (needs Tenant ID + Client ID + Secret)

```bash
make azure-connect          # connect only
make azure-alerts           # connect + show fired alerts
make azure-app-insights     # connect App Insights (needs APP_ID + API_KEY)
```

**Credentials required** — set in `.env` or enter when prompted:
```
AZURE_TENANT_ID=<tenant id>
AZURE_CLIENT_ID=<client id / app id>
AZURE_CLIENT_SECRET=<client secret>
AZURE_SUBSCRIPTION_ID=<subscription id>  # optional — auto-detected if omitted
```

---

### Option B — Azure CLI (requires `az login` first)

```bash
make azure-connect-cli      # connect only
make azure-alerts-cli       # connect + show fired alerts
```

**Prerequisite:** Azure CLI installed and logged in:
```bash
az login
```

---

### Option C — Interactive browser (device code — no install needed)

```bash
make azure-connect-interactive   # connect only (authenticates + caches token)
make azure-alerts-interactive    # connect + show fired alerts
```

**How it works:**
1. First run: prints a code + URL — open the URL in any browser, enter the code
2. If your account has multiple subscriptions, you are prompted to pick one
3. All subsequent runs: silently reuses the cached token from `~/.sherlock/azure_token_cache.json`
4. No re-authentication until the refresh token expires (~90 days)

> **Recommended** if you have neither a service principal nor Azure CLI installed.

### Switching accounts / force new sign-in

```bash
make azure-logout
```

Deletes `~/.sherlock/azure_token_cache.json`. The next `make azure-connect-interactive` (or any `*-interactive` command) will prompt for a fresh device code sign-in, letting you authenticate as a different user or subscription.

---

### App Insights (separate tool)

Two options — choose based on what you have:

#### Option A — API key

```bash
make azure-app-insights
```

**Credentials required** — set in `.env` or enter when prompted:
```
AZURE_APP_INSIGHTS_APP_ID=<application id GUID>
AZURE_APP_INSIGHTS_API_KEY=<api key>
```

Find both in: Azure Portal → App Insights resource → **Configure → API Access**.

#### Option B — Interactive browser (no API key needed) ✅ Recommended

```bash
make azure-app-insights-interactive
```

**Nothing required** — the command:
1. Reuses the cached management token (from `make azure-connect-interactive`) to auto-discover all App Insights resources in the subscription
2. If multiple resources exist, selects the first one (pass `--app-id <guid>` to target a specific one)
3. Prompts for a second device code sign-in scoped to `api.applicationinsights.io` (once, then cached)

> Run `make azure-connect-interactive` at least once beforehand to prime the subscription token cache.

---

## Prometheus

```bash
make prometheus-connect         # connect + validate
make prometheus-alerts          # show firing alerts
make prometheus-query           # run default query: up
make prometheus-query PROMQL="rate(http_requests_total[5m])"  # custom query
```

**Credentials required** — set in `.env` or enter when prompted:
```
PROMETHEUS_URL=http://your-prometheus:9090
PROMETHEUS_USERNAME=<optional>
PROMETHEUS_PASSWORD=<optional>
```

---

## Datadog

```bash
make datadog-connect            # connect + validate
make datadog-monitors           # list all monitors and their status
make datadog-logs               # search logs (default: status:error, last 60 min)
make datadog-logs QUERY="service:my-api status:error" SINCE=30   # custom search
```

**Credentials required** — set in `.env` or enter when prompted:
```
DD_API_KEY=<datadog api key>
DD_APP_KEY=<datadog application key>
DD_SITE=datadoghq.com           # or datadoghq.eu etc.
```

---

## Loki (Grafana)

```bash
make loki-connect               # connect + list available labels
make loki-logs                  # stream logs (default selector, last 60 min)
make loki-logs LOGQL='{app="my-service"} |= "error"' SINCE=30   # custom query
```

**Credentials required** — set in `.env` or enter when prompted:
```
LOKI_URL=http://your-loki:3100
LOKI_USERNAME=<optional>
LOKI_PASSWORD=<optional>
```

---

## Development & Testing

```bash
make install                    # install all dependencies
make test                       # run all tests (verbose)
make test-fast                  # run all tests, stop on first failure
make test-domain-tools          # test NR domain tools (APM, K8s, logs, alerts, synthetics)
make test-monitoring-tools      # test external tools (App Insights, Azure Monitor, Datadog, Prometheus)
make test-cov                   # run all tests with HTML coverage report
make lint                       # ruff + mypy
make format                     # auto-format with ruff
make clean                      # remove all __pycache__ directories
```

---

## Quick Decision Guide

```
Which tool are you connecting to?
│
├── New Relic ──────────────────→  make newrelic-connect
│
├── Azure Monitor
│   ├── Have service principal? →  make azure-alerts
│   ├── Have az CLI installed?  →  make azure-alerts-cli
│   └── Just a browser?        →  make azure-alerts-interactive  ← easiest
│
├── Prometheus ─────────────────→  make prometheus-alerts
├── Datadog ────────────────────→  make datadog-monitors
└── Loki ───────────────────────→  make loki-logs
```
