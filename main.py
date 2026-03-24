"""
Sherlock — main entry point.

Registers all 21 MCP tools, configures logging, and starts the
stdio-based MCP server. All tool responses are scrubbed for prompt
injection before being returned to the client.
"""

import asyncio
import json
import logging
import logging.handlers
import os
import sys
import time
from pathlib import Path

from dotenv import load_dotenv
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool, ToolAnnotations

from core.context import AccountContext
from core.exceptions import ReadOnlyViolation
from core.sanitize import scrub_tool_response

# ── Load environment ─────────────────────────────────────────────────────

# Load .env relative to this file's directory (not cwd) so credentials
# are found regardless of how the MCP server is launched.
load_dotenv(Path(__file__).resolve().parent / ".env")

# ── Directory setup ──────────────────────────────────────────────────────

CONFIG_DIR = Path(__file__).resolve().parent / ".sherlock"
LOG_DIR = CONFIG_DIR / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)

# ── Logging setup (NEVER write to stdout — reserved for MCP protocol) ───

def _setup_logging() -> None:
    """Configure structured JSON logging to file only."""
    root_logger = logging.getLogger("sherlock")
    root_logger.setLevel(logging.DEBUG)

    # Main log — rotating 10MB, 5 backups.
    main_handler = logging.handlers.RotatingFileHandler(
        LOG_DIR / "sherlock.log",
        maxBytes=10 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    main_handler.setLevel(logging.DEBUG)
    main_handler.setFormatter(logging.Formatter(
        '{"time":"%(asctime)s","level":"%(levelname)s",'
        '"logger":"%(name)s","message":"%(message)s"}'
    ))
    root_logger.addHandler(main_handler)

    # Audit log — rotating 10MB, 10 backups.
    audit_logger = logging.getLogger("sherlock.audit")
    audit_handler = logging.handlers.RotatingFileHandler(
        LOG_DIR / "audit.log",
        maxBytes=10 * 1024 * 1024,
        backupCount=10,
        encoding="utf-8",
    )
    audit_handler.setLevel(logging.INFO)
    audit_handler.setFormatter(logging.Formatter(
        '{"time":"%(asctime)s","level":"%(levelname)s",'
        '"logger":"%(name)s","message":"%(message)s"}'
    ))
    audit_logger.addHandler(audit_handler)

    # Suppress logging to stdout/stderr from other libraries.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


_setup_logging()

logger = logging.getLogger("sherlock.main")
audit_logger = logging.getLogger("sherlock.audit")

# ── Tool imports ─────────────────────────────────────────────────────────

from tools.alerts import get_alerts, get_incidents, get_service_incidents
from tools.apm import get_apm_applications, get_app_metrics, get_deployments
from tools.dependencies import get_service_dependencies
from tools.entities import get_entity_guid
from tools.golden_signals import get_service_golden_signals
from tools.intelligence_tools import (
    connect_account,
    get_account_summary,
    get_nrql_context,
    learn_account_tool,
    list_connections,
    list_profiles,
)
from tools.investigate import investigate_service
from tools.k8s import get_k8s_health
from tools.logs import search_logs
from tools.azure_monitor_tool import (
    connect_azure_monitor,
    connect_azure_monitor_cli,
    connect_azure_monitor_interactive,
    get_azure_monitor_alerts,
    query_azure_monitor_logs,
    query_azure_monitor_metrics,
)
from tools.datadog_tool import connect_datadog, get_datadog_monitors, query_datadog_metrics, search_datadog_logs
from tools.loki import connect_loki, search_loki_logs
from tools.app_insights_tool import (
    connect_app_insights,
    get_app_insights_availability,
    get_app_insights_dependencies,
    get_app_insights_failures,
    query_app_insights,
)
from tools.nrql import run_nrql_query
from tools.promql import connect_prometheus, get_prometheus_alerts, run_promql_query
from tools.synthetics import (
    get_monitor_results,
    get_monitor_status,
    get_synthetic_monitors,
    investigate_synthetic,
)

# ── Tool definitions ─────────────────────────────────────────────────────

TOOLS: list[Tool] = [
    # 1. connect_account
    Tool(
        name="connect_account",
        description=(
            "Connect to a New Relic account. Call this first — required before "
            "all other tools. Validates credentials, learns the account structure, "
            "and caches intelligence. Provide EITHER a saved profile_name (which "
            "loads credentials from keychain) OR explicit account_id + api_key."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "account_id": {"type": "string", "description": "New Relic account ID"},
                "api_key": {"type": "string", "description": "New Relic User API key"},
                "region": {
                    "type": "string", "enum": ["US", "EU"], "default": "US",
                    "description": "Data center region",
                },
                "profile_name": {
                    "type": "string",
                    "description": (
                        "Saved profile name to connect from (loads credentials "
                        "from keychain). When provided, account_id and api_key "
                        "are not required."
                    ),
                },
            },
        },
    ),
    # 2. list_profiles
    Tool(
        name="list_profiles",
        description="List all saved New Relic credential profiles.",
        inputSchema={"type": "object", "properties": {}},
    ),
    # 2b. list_connections
    Tool(
        name="list_connections",
        description=(
            "Check which datasource connections are currently active. "
            "Reports live status for New Relic, Azure Monitor, Application Insights, "
            "Prometheus, Datadog, and Loki — including account/subscription details for "
            "connected ones. Also lists saved New Relic profiles and provides connect "
            "hints for inactive datasources. "
            "Call this FIRST to understand the current connection state before "
            "directing the user to connect a specific tool."
        ),
        annotations=ToolAnnotations(readOnlyHint=True),
        inputSchema={"type": "object", "properties": {}},
    ),
    # 3. learn_account
    Tool(
        name="learn_account",
        description=(
            "Discover ALL entity names, types, and relationships in the active account. "
            "MUST be called before any investigation to get real service names, K8s "
            "deployment names, synthetic monitor names, and entity relationships. "
            "Results are cached — subsequent calls refresh the cache."
        ),
        inputSchema={"type": "object", "properties": {}},
    ),
    # 4. get_account_summary
    Tool(
        name="get_account_summary",
        description="Get complete intelligence summary for the active account.",
        inputSchema={"type": "object", "properties": {}},
    ),
    # 5. get_nrql_context
    Tool(
        name="get_nrql_context",
        description=(
            "Call before constructing any NRQL to get real service names, "
            "monitor names, and attribute names for this account."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "domain": {
                    "type": "string",
                    "enum": ["apm", "k8s", "logs", "alerts", "synthetics", "all"],
                    "default": "all",
                    "description": "Domain to get context for",
                },
            },
        },
    ),
    # 6. investigate_service
    Tool(
        name="investigate_service",
        description=(
            "[LEGACY] Quick automated check across all domains for a service. "
            "Returns findings with domain-level status and recommendations. "
            "For comprehensive investigation, use the agent-team pattern "
            "(sherlock-team-lead dispatching to all 6 domain agents) instead. "
            "Use this tool ONLY for a fast summary or when agents are not available."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "service_name": {
                    "type": "string",
                    "description": "APM service name to investigate",
                },
                "namespace": {
                    "type": "string",
                    "description": "Optional K8s namespace",
                },
                "since_minutes": {
                    "type": "integer", "default": 30,
                    "description": "Time window in minutes",
                },
            },
            "required": ["service_name"],
        },
    ),
    # 7. investigate_synthetic
    Tool(
        name="investigate_synthetic",
        description=(
            "Use when a synthetic monitor is failing or a login flow / "
            "health check is broken. Deep investigation with APM correlation."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "monitor_name": {
                    "type": "string",
                    "description": "Synthetic monitor name to investigate",
                },
                "since_minutes": {
                    "type": "integer", "default": 60,
                    "description": "Time window in minutes",
                },
            },
            "required": ["monitor_name"],
        },
    ),
    # 8. get_service_golden_signals
    Tool(
        name="get_service_golden_signals",
        description=(
            "Get the four golden signals (latency, traffic, errors, saturation) "
            "for an APM service with trend data. Essential for APM domain analysis."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "service_name": {"type": "string", "description": "APM service name"},
                "since_minutes": {
                    "type": "integer", "default": 30,
                    "description": "Time window in minutes",
                },
            },
            "required": ["service_name"],
        },
    ),
    # 9. get_k8s_health
    Tool(
        name="get_k8s_health",
        description=(
            "Get Kubernetes health data (pods, restarts, resource usage, deployments) "
            "for a service or namespace. Use the BARE deployment name (after '/') "
            "and namespace (before '/') for best results."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "service_name": {"type": "string", "description": "Service name"},
                "namespace": {"type": "string", "description": "K8s namespace"},
                "since_minutes": {
                    "type": "integer", "default": 30,
                    "description": "Time window in minutes",
                },
            },
        },
    ),
    # 10. search_logs
    Tool(
        name="search_logs",
        description=(
            "Search logs with filters for service, severity, and keywords. "
            "Essential for log domain analysis and error pattern detection."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "service_name": {"type": "string", "description": "Service name filter"},
                "severity": {
                    "type": "string",
                    "description": "Severity filter (e.g. 'ERROR', 'WARN', 'ERROR,WARN')",
                },
                "keyword": {"type": "string", "description": "Keyword to search in messages"},
                "since_minutes": {
                    "type": "integer", "default": 60,
                    "description": "Time window in minutes",
                },
                "limit": {
                    "type": "integer", "default": 100,
                    "description": "Max results",
                },
            },
        },
    ),
    # 11. get_synthetic_monitors
    Tool(
        name="get_synthetic_monitors",
        description=(
            "List all synthetic monitors for the active account with status summary."
        ),
        inputSchema={"type": "object", "properties": {}},
    ),
    # 12. get_monitor_status
    Tool(
        name="get_monitor_status",
        description=(
            "Use to check if a specific synthetic monitor is passing or failing "
            "and in which locations."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "monitor_name": {
                    "type": "string",
                    "description": "Synthetic monitor name",
                },
                "since_minutes": {
                    "type": "integer", "default": 60,
                    "description": "Time window in minutes",
                },
            },
            "required": ["monitor_name"],
        },
    ),
    # 13. get_monitor_results
    Tool(
        name="get_monitor_results",
        description="Get raw run results for a synthetic monitor, useful for digging into failures.",
        inputSchema={
            "type": "object",
            "properties": {
                "monitor_name": {"type": "string", "description": "Synthetic monitor name"},
                "result_filter": {
                    "type": "string", "enum": ["FAILED", "SUCCESS"],
                    "description": "Filter by result type",
                },
                "since_minutes": {
                    "type": "integer", "default": 60,
                    "description": "Time window in minutes",
                },
                "limit": {
                    "type": "integer", "default": 50,
                    "description": "Max results",
                },
            },
            "required": ["monitor_name"],
        },
    ),
    # 14. get_apm_applications
    Tool(
        name="get_apm_applications",
        description="List all APM applications for the active account.",
        inputSchema={"type": "object", "properties": {}},
    ),
    # 15. get_app_metrics
    Tool(
        name="get_app_metrics",
        description="Get key performance metrics for an APM application.",
        inputSchema={
            "type": "object",
            "properties": {
                "app_name": {"type": "string", "description": "APM application name"},
                "since_minutes": {
                    "type": "integer", "default": 30,
                    "description": "Time window in minutes",
                },
            },
            "required": ["app_name"],
        },
    ),
    # 16. get_deployments
    Tool(
        name="get_deployments",
        description="Get recent deployment history for an APM application.",
        inputSchema={
            "type": "object",
            "properties": {
                "app_name": {"type": "string", "description": "APM application name"},
                "limit": {
                    "type": "integer", "default": 10,
                    "description": "Max deployments to return",
                },
            },
            "required": ["app_name"],
        },
    ),
    # 17. get_alerts
    Tool(
        name="get_alerts",
        description=(
            "Get all alert policies for the active account. Takes no parameters. "
            "Returns account-wide policies, not service-specific."
        ),
        inputSchema={"type": "object", "properties": {}},
    ),
    # 18. get_incidents
    Tool(
        name="get_incidents",
        description=(
            "Get incidents filtered by state (open or closed). Returns account-wide "
            "incidents — does NOT accept a service name filter. Use "
            "get_service_incidents for service-specific incidents."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "state": {
                    "type": "string", "enum": ["open", "closed"], "default": "open",
                    "description": "Incident state filter",
                },
            },
        },
    ),
    # 19. get_service_incidents
    Tool(
        name="get_service_incidents",
        description=(
            "Get incidents related to a specific service or monitor. "
            "Use this instead of get_incidents when filtering by service name."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "service_name": {
                    "type": "string",
                    "description": "Service or monitor name to search for",
                },
            },
            "required": ["service_name"],
        },
    ),
    # 20. run_nrql_query
    Tool(
        name="run_nrql_query",
        description="Execute a raw NRQL query. Use get_nrql_context first to get valid names.",
        inputSchema={
            "type": "object",
            "properties": {
                "nrql": {"type": "string", "description": "NRQL query to execute"},
            },
            "required": ["nrql"],
        },
    ),
    # 22. connect_prometheus
    Tool(
        name="connect_prometheus",
        description=(
            "Connect to a Prometheus instance. Call before run_promql_query or "
            "get_prometheus_alerts. Optionally provide alertmanager_url to enable "
            "Alertmanager alert listing. "
            "Supports Bearer token auth, Basic auth, or no auth."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "Base URL of the Prometheus server (e.g. 'http://prometheus:9090')",
                },
                "alertmanager_url": {
                    "type": "string",
                    "description": (
                        "Optional base URL of the Alertmanager instance "
                        "(e.g. 'http://alertmanager:9093'). "
                        "Required for get_prometheus_alerts."
                    ),
                },
                "token": {
                    "type": "string",
                    "description": "Optional Bearer token for authentication",
                },
                "username": {
                    "type": "string",
                    "description": "Optional Basic auth username",
                },
                "password": {
                    "type": "string",
                    "description": "Optional Basic auth password",
                },
            },
            "required": ["url"],
        },
    ),
    # 23. run_promql_query
    Tool(
        name="run_promql_query",
        description=(
            "Execute a PromQL query against the connected Prometheus instance. "
            "Requires connect_prometheus to be called first. "
            "Use query_type='range' for timeseries or 'instant' for current value."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "promql": {
                    "type": "string",
                    "description": "PromQL expression (e.g. 'rate(http_requests_total[5m])')",
                },
                "since_minutes": {
                    "type": "integer", "default": 60,
                    "description": "Time window in minutes for range queries",
                },
                "step": {
                    "type": "string", "default": "60s",
                    "description": "Step resolution for range queries (e.g. '60s', '5m')",
                },
                "query_type": {
                    "type": "string",
                    "enum": ["range", "instant"],
                    "default": "range",
                    "description": "'range' for timeseries, 'instant' for current value",
                },
            },
            "required": ["promql"],
        },
    ),
    # 24. connect_datadog
    Tool(
        name="connect_datadog",
        description=(
            "Connect to Datadog using API key and Application key. "
            "Call before query_datadog_metrics."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "api_key": {
                    "type": "string",
                    "description": "Datadog API key",
                },
                "app_key": {
                    "type": "string",
                    "description": "Datadog Application key",
                },
                "site": {
                    "type": "string",
                    "enum": ["US1", "US3", "US5", "EU1", "AP1"],
                    "default": "US1",
                    "description": "Datadog site region",
                },
            },
            "required": ["api_key", "app_key"],
        },
    ),
    # 25. query_datadog_metrics
    Tool(
        name="query_datadog_metrics",
        description=(
            "Query Datadog metrics for the connected account. "
            "Requires connect_datadog to be called first."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Datadog metrics query (e.g. 'avg:system.cpu.user{*}')",
                },
                "since_minutes": {
                    "type": "integer", "default": 60,
                    "description": "Time window in minutes",
                },
            },
            "required": ["query"],
        },
    ),
    # 26. connect_azure_monitor
    Tool(
        name="connect_azure_monitor",
        description=(
            "Connect to Azure Monitor using a Service Principal (client credentials). "
            "The principal needs 'Monitoring Reader' for metrics and "
            "'Log Analytics Reader' for log queries. "
            "Call before query_azure_monitor_metrics or query_azure_monitor_logs."
        ),
        annotations=ToolAnnotations(readOnlyHint=True),
        inputSchema={
            "type": "object",
            "properties": {
                "tenant_id": {
                    "type": "string",
                    "description": "Azure AD tenant ID (GUID)",
                },
                "client_id": {
                    "type": "string",
                    "description": "Service principal application (client) ID",
                },
                "client_secret": {
                    "type": "string",
                    "description": "Service principal client secret",
                },
                "subscription_id": {
                    "type": "string",
                    "description": "Azure subscription ID",
                },
            },
            "required": ["tenant_id", "client_id", "client_secret", "subscription_id"],
        },
    ),
    # 26b. connect_azure_monitor_cli
    Tool(
        name="connect_azure_monitor_cli",
        description=(
            "Connect to Azure Monitor using the Azure CLI (az login) — no service principal needed. "
            "User must already be logged in with 'az login'. "
            "Subscription ID is auto-detected from 'az account show' if not provided. "
            "Use this instead of connect_azure_monitor when you don't have tenant/client credentials."
        ),
        annotations=ToolAnnotations(readOnlyHint=True),
        inputSchema={
            "type": "object",
            "properties": {
                "subscription_id": {
                    "type": "string",
                    "description": "Azure subscription ID (optional — auto-detected from az CLI if omitted)",
                },
            },
        },
    ),
    # 26c. connect_azure_monitor_interactive
    Tool(
        name="connect_azure_monitor_interactive",
        description=(
            "Connect to Azure Monitor via browser device-code flow. "
            "No Azure CLI installation or service principal required. "
            "Prints a short code + URL to the terminal; user opens URL in any browser and signs in. "
            "Use this when neither a service principal nor the Azure CLI is available."
        ),
        annotations=ToolAnnotations(readOnlyHint=True),
        inputSchema={
            "type": "object",
            "properties": {
                "subscription_id": {
                    "type": "string",
                    "description": "Azure subscription ID (optional — auto-detected if omitted)",
                },
            },
        },
    ),
    # 27. query_azure_monitor_metrics
    Tool(
        name="query_azure_monitor_metrics",
        description=(
            "Query Azure Monitor resource metrics (e.g. App Service CPU, "
            "Front Door 5xx, PostgreSQL availability). "
            "Requires connect_azure_monitor first."
        ),
        annotations=ToolAnnotations(readOnlyHint=True),
        inputSchema={
            "type": "object",
            "properties": {
                "resource_id": {
                    "type": "string",
                    "description": "Full Azure resource ID",
                },
                "metric_names": {
                    "type": "string",
                    "description": "Comma-separated metric names (e.g. 'Percentage5XX,RequestCount')",
                },
                "since_minutes": {
                    "type": "integer", "default": 60,
                    "description": "Time window in minutes",
                },
                "interval": {
                    "type": "string", "default": "PT5M",
                    "description": "Granularity interval (PT1M, PT5M, PT15M, PT30M, PT1H)",
                },
                "aggregation": {
                    "type": "string",
                    "enum": ["Average", "Total", "Minimum", "Maximum", "Count"],
                    "default": "Average",
                    "description": "Aggregation type",
                },
            },
            "required": ["resource_id", "metric_names"],
        },
    ),
    # 28. query_azure_monitor_logs
    Tool(
        name="query_azure_monitor_logs",
        description=(
            "Query Azure Log Analytics using KQL. "
            "Use for Front Door access logs, App Service logs, WAF logs, etc. "
            "Requires connect_azure_monitor first."
        ),
        annotations=ToolAnnotations(readOnlyHint=True),
        inputSchema={
            "type": "object",
            "properties": {
                "workspace_id": {
                    "type": "string",
                    "description": "Log Analytics workspace ID (GUID)",
                },
                "kql": {
                    "type": "string",
                    "description": "KQL query string",
                },
                "since_minutes": {
                    "type": "integer", "default": 60,
                    "description": "Time window in minutes",
                },
            },
            "required": ["workspace_id", "kql"],
        },
    ),
    # 31. get_prometheus_alerts
    Tool(
        name="get_prometheus_alerts",
        description=(
            "List currently firing alerts from Alertmanager. "
            "Use to answer: are there Prometheus alerts firing? What is in Alert state? "
            "Requires connect_prometheus to be called with alertmanager_url set."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "label_filter": {
                    "type": "string",
                    "description": (
                        "Optional Prometheus label selector to filter alerts, "
                        "e.g. 'severity=\"critical\"' or '{job=\"api-server\"}'. "
                        "Omit to return all active alerts."
                    ),
                },
                "include_silenced": {
                    "type": "boolean",
                    "default": False,
                    "description": "Include silenced alerts. Defaults to False.",
                },
                "include_inhibited": {
                    "type": "boolean",
                    "default": False,
                    "description": "Include inhibited alerts. Defaults to False.",
                },
            },
        },
    ),
    # 32. search_datadog_logs
    Tool(
        name="search_datadog_logs",
        description=(
            "Search Datadog logs using the Logs Events v2 API. "
            "Use for log-based root cause analysis, error pattern detection, "
            "and service-scoped log inspection. "
            "Requires connect_datadog first."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "default": "*",
                    "description": (
                        "Datadog log search query. Examples: "
                        "'service:api status:error', "
                        "'host:web-01 NullPointerException', "
                        "'service:checkout timeout'. "
                        "Use '*' for all logs."
                    ),
                },
                "since_minutes": {
                    "type": "integer",
                    "default": 60,
                    "description": "Look-back window in minutes.",
                },
                "limit": {
                    "type": "integer",
                    "default": 100,
                    "description": "Max log events to return (1-1000).",
                },
                "sort": {
                    "type": "string",
                    "enum": ["-timestamp", "timestamp"],
                    "default": "-timestamp",
                    "description": "'-timestamp' for newest first (default), 'timestamp' for oldest first.",
                },
            },
        },
    ),
    # 33. connect_loki
    Tool(
        name="connect_loki",
        description=(
            "Connect to a Grafana Loki instance. Call before search_loki_logs. "
            "Supports Bearer token, Basic auth (Grafana Cloud), or no auth. "
            "For Grafana Cloud: provide username=<tenant_id> and password=<api_key>."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": (
                        "Base URL of the Loki server "
                        "(e.g. 'http://loki:3100' or "
                        "'https://logs-prod-us-central1.grafana.net')."
                    ),
                },
                "token": {
                    "type": "string",
                    "description": "Optional Bearer token for authentication.",
                },
                "username": {
                    "type": "string",
                    "description": (
                        "Optional Basic auth username. "
                        "For Grafana Cloud this is the numeric tenant ID."
                    ),
                },
                "password": {
                    "type": "string",
                    "description": (
                        "Optional Basic auth password. "
                        "For Grafana Cloud this is the API key."
                    ),
                },
                "org_id": {
                    "type": "string",
                    "description": (
                        "Optional tenant/org ID sent as X-Scope-OrgID header. "
                        "Required for multi-tenant Loki deployments."
                    ),
                },
            },
            "required": ["url"],
        },
    ),
    # 34. search_loki_logs
    Tool(
        name="search_loki_logs",
        description=(
            "Search Grafana Loki logs using a LogQL query. "
            "Use for Kubernetes pod logs, application logs, and infrastructure logs. "
            "Requires connect_loki first. "
            "Call with empty logql to discover available label names."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "logql": {
                    "type": "string",
                    "description": (
                        "LogQL query string. Must include a stream selector, e.g.: "
                        "'{service=\"api\"} |= \"error\"', "
                        "'{namespace=\"prod\",app=\"checkout\"} |~ \"timeout|exception\"', "
                        "'{job=\"nginx\"}'. "
                        "Leave empty to get a list of available label names."
                    ),
                },
                "since_minutes": {
                    "type": "integer",
                    "default": 60,
                    "description": "Look-back window in minutes. Defaults to 60.",
                },
                "limit": {
                    "type": "integer",
                    "default": 100,
                    "description": "Max log lines to return (1-5000). Defaults to 100.",
                },
                "direction": {
                    "type": "string",
                    "enum": ["backward", "forward"],
                    "default": "backward",
                    "description": "'backward' for newest first (default), 'forward' for oldest first.",
                },
            },
        },
    ),
    # 29. get_azure_monitor_alerts
    Tool(
        name="get_azure_monitor_alerts",
        description=(
            "List currently fired Azure Monitor alert instances and optionally "
            "the configured metric alert rules for the subscription. "
            "Use this to find active alerts, their severity, and what fired them. "
            "Requires connect_azure_monitor first."
        ),
        annotations=ToolAnnotations(readOnlyHint=True),
        inputSchema={
            "type": "object",
            "properties": {
                "time_range": {
                    "type": "string",
                    "enum": ["1h", "6h", "1d", "7d", "30d"],
                    "default": "1h",
                    "description": "Look-back window for fired alerts",
                },
                "severity": {
                    "type": "string",
                    "enum": ["Sev0", "Sev1", "Sev2", "Sev3", "Sev4"],
                    "description": (
                        "Optional severity filter. "
                        "Sev0=Critical, Sev1=Error, Sev2=Warning, "
                        "Sev3=Informational, Sev4=Verbose."
                    ),
                },
                "resource_group": {
                    "type": "string",
                    "description": (
                        "Optional Azure resource group to scope the query. "
                        "If omitted, queries the entire subscription."
                    ),
                },
                "include_rules": {
                    "type": "boolean",
                    "default": False,
                    "description": (
                        "If true, also return the configured metric alert rule "
                        "definitions (thresholds, criteria, enabled state)."
                    ),
                },
                "limit": {
                    "type": "integer",
                    "default": 100,
                    "description": "Max fired alert instances to return (1-250)",
                },
            },
        },
    ),
    # 30. get_datadog_monitors
    Tool(
        name="get_datadog_monitors",
        description=(
            "List Datadog monitors and their current alert state. "
            "Use triggered_only=true to see only monitors actively firing (Alert/Warn). "
            "Use name_filter or tags to narrow results. "
            "Requires connect_datadog first."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "name_filter": {
                    "type": "string",
                    "description": (
                        "Optional substring to filter monitor names "
                        "(e.g. 'cpu', 'latency', 'error rate')."
                    ),
                },
                "tags": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Optional tag filter list "
                        "(e.g. ['env:prod', 'service:api'])."
                    ),
                },
                "triggered_only": {
                    "type": "boolean",
                    "default": False,
                    "description": (
                        "If true, return only monitors in Alert or Warn state. "
                        "If false, return all monitors."
                    ),
                },
            },
        },
    ),
    # 21. get_service_dependencies
    Tool(
        name="get_service_dependencies",
        description=(
            "Get upstream and downstream service dependencies for an APM service. "
            "Shows which services call this service and which services it calls, "
            "with health warnings for unhealthy dependencies."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "service_name": {
                    "type": "string",
                    "description": "APM service name to look up dependencies for",
                },
                "direction": {
                    "type": "string",
                    "enum": ["downstream", "upstream", "both"],
                    "default": "both",
                    "description": "Which direction of dependencies to return",
                },
                "include_external": {
                    "type": "boolean",
                    "default": False,
                    "description": "Include external (non-NR) endpoint dependencies",
                },
                "max_depth": {
                    "type": "integer",
                    "default": 2,
                    "description": "Maximum dependency depth (1-5)",
                },
            },
            "required": ["service_name"],
        },
    ),
    # 35. connect_app_insights
    Tool(
        name="connect_app_insights",
        description=(
            "Connect to an Azure Application Insights resource. "
            "Required before query_app_insights, get_app_insights_failures, "
            "get_app_insights_dependencies, and get_app_insights_availability. "
            "Supports API Key auth (simpler) or AAD Service Principal auth. "
            "The app_id is the Application ID GUID found in: "
            "Azure Portal > App Insights > Configure > API Access."
        ),
        annotations=ToolAnnotations(readOnlyHint=True),
        inputSchema={
            "type": "object",
            "properties": {
                "app_id": {
                    "type": "string",
                    "description": (
                        "Application Insights Application ID (GUID — from "
                        "Azure Portal > App Insights > Configure > API Access)."
                    ),
                },
                "api_key": {
                    "type": "string",
                    "description": (
                        "API key for authentication. Create in: "
                        "Azure Portal > App Insights > Configure > API Access. "
                        "Provide api_key OR (tenant_id + client_id + client_secret)."
                    ),
                },
                "tenant_id": {
                    "type": "string",
                    "description": "AAD tenant ID (for service principal auth).",
                },
                "client_id": {
                    "type": "string",
                    "description": "Service principal application/client ID.",
                },
                "client_secret": {
                    "type": "string",
                    "description": "Service principal client secret.",
                },
            },
            "required": ["app_id"],
        },
    ),
    # 36. query_app_insights
    Tool(
        name="query_app_insights",
        description=(
            "Run a raw KQL query against the connected Application Insights resource. "
            "Requires connect_app_insights first. "
            "Available tables: requests, exceptions, dependencies, traces, "
            "customEvents, pageViews, availabilityResults, performanceCounters, customMetrics. "
            "Use this for custom analysis beyond what the pre-built tools provide."
        ),
        annotations=ToolAnnotations(readOnlyHint=True),
        inputSchema={
            "type": "object",
            "properties": {
                "kql": {
                    "type": "string",
                    "description": "KQL query string (max 4000 chars).",
                },
                "since_minutes": {
                    "type": "integer",
                    "default": 60,
                    "description": "Look-back window in minutes. Defaults to 60.",
                },
            },
            "required": ["kql"],
        },
    ),
    # 37. get_app_insights_failures
    Tool(
        name="get_app_insights_failures",
        description=(
            "Get APM failure telemetry from Application Insights: "
            "failed HTTP requests grouped by operation + result code, "
            "and top unhandled exceptions grouped by type + message. "
            "Use to answer: what errors is the app throwing? What is the error rate? "
            "Requires connect_app_insights first."
        ),
        annotations=ToolAnnotations(readOnlyHint=True),
        inputSchema={
            "type": "object",
            "properties": {
                "since_minutes": {
                    "type": "integer",
                    "default": 60,
                    "description": "Look-back window in minutes. Defaults to 60.",
                },
                "limit": {
                    "type": "integer",
                    "default": 100,
                    "description": "Max rows per result set. Defaults to 100.",
                },
                "include_exceptions": {
                    "type": "boolean",
                    "default": True,
                    "description": "Also fetch exception breakdown. Defaults to true.",
                },
            },
        },
    ),
    # 38. get_app_insights_dependencies
    Tool(
        name="get_app_insights_dependencies",
        description=(
            "Get dependency call telemetry from Application Insights: "
            "outgoing HTTP calls, database queries, queue calls, and cache operations. "
            "Returns failed dependencies (grouped by target+type+resultCode) and "
            "slow dependencies exceeding slow_threshold_ms. "
            "Use to diagnose: which downstream is failing or slow? "
            "Requires connect_app_insights first."
        ),
        annotations=ToolAnnotations(readOnlyHint=True),
        inputSchema={
            "type": "object",
            "properties": {
                "since_minutes": {
                    "type": "integer",
                    "default": 60,
                    "description": "Look-back window in minutes. Defaults to 60.",
                },
                "limit": {
                    "type": "integer",
                    "default": 100,
                    "description": "Max rows per result set. Defaults to 100.",
                },
                "failed_only": {
                    "type": "boolean",
                    "default": False,
                    "description": (
                        "Return only the failed dependency summary. "
                        "Defaults to false (returns both failed and slow)."
                    ),
                },
                "slow_threshold_ms": {
                    "type": "number",
                    "default": 1000,
                    "description": "Duration threshold for slow call detection (ms). Defaults to 1000.",
                },
            },
        },
    ),
    # 39. get_app_insights_availability
    Tool(
        name="get_app_insights_availability",
        description=(
            "Get availability test results from Application Insights: "
            "uptime percentages, pass/fail counts by test name, "
            "and recent failure details with location. "
            "Use to answer: are availability/ping tests passing? What is the uptime? "
            "Requires connect_app_insights first."
        ),
        annotations=ToolAnnotations(readOnlyHint=True),
        inputSchema={
            "type": "object",
            "properties": {
                "since_minutes": {
                    "type": "integer",
                    "default": 60,
                    "description": "Look-back window in minutes. Defaults to 60.",
                },
                "limit": {
                    "type": "integer",
                    "default": 100,
                    "description": "Max recent failure rows to return. Defaults to 100.",
                },
            },
        },
    ),
]

# ── Tool dispatch map ────────────────────────────────────────────────────

TOOL_HANDLERS = {
    "connect_account": connect_account,
    "list_profiles": list_profiles,
    "list_connections": list_connections,
    "learn_account": learn_account_tool,
    "get_account_summary": get_account_summary,
    "get_nrql_context": get_nrql_context,
    "investigate_service": investigate_service,
    "investigate_synthetic": investigate_synthetic,
    "get_service_golden_signals": get_service_golden_signals,
    "get_k8s_health": get_k8s_health,
    "search_logs": search_logs,
    "get_synthetic_monitors": get_synthetic_monitors,
    "get_monitor_status": get_monitor_status,
    "get_monitor_results": get_monitor_results,
    "get_apm_applications": get_apm_applications,
    "get_app_metrics": get_app_metrics,
    "get_deployments": get_deployments,
    "get_alerts": get_alerts,
    "get_incidents": get_incidents,
    "get_service_incidents": get_service_incidents,
    "run_nrql_query": run_nrql_query,
    "get_service_dependencies": get_service_dependencies,
    "connect_prometheus": connect_prometheus,
    "run_promql_query": run_promql_query,
    "get_prometheus_alerts": get_prometheus_alerts,
    "connect_loki": connect_loki,
    "search_loki_logs": search_loki_logs,
    "connect_datadog": connect_datadog,
    "query_datadog_metrics": query_datadog_metrics,
    "get_datadog_monitors": get_datadog_monitors,
    "search_datadog_logs": search_datadog_logs,
    "connect_azure_monitor": connect_azure_monitor,
    "connect_azure_monitor_cli": connect_azure_monitor_cli,
    "connect_azure_monitor_interactive": connect_azure_monitor_interactive,
    "query_azure_monitor_metrics": query_azure_monitor_metrics,
    "query_azure_monitor_logs": query_azure_monitor_logs,
    "get_azure_monitor_alerts": get_azure_monitor_alerts,
    "connect_app_insights": connect_app_insights,
    "query_app_insights": query_app_insights,
    "get_app_insights_failures": get_app_insights_failures,
    "get_app_insights_dependencies": get_app_insights_dependencies,
    "get_app_insights_availability": get_app_insights_availability,
}

# ── MCP Server ───────────────────────────────────────────────────────────

app = Server("sherlock")


@app.list_tools()
async def handle_list_tools() -> list[Tool]:
    """Return the list of all available tools."""
    return TOOLS


@app.call_tool()
async def handle_call_tool(name: str, arguments: dict) -> list[TextContent]:
    """Dispatch a tool call, scrub the response, and log to audit.

    Args:
        name: The tool name to call.
        arguments: The tool arguments dict.

    Returns:
        List containing a single TextContent with the JSON response.
    """
    start = time.time()
    account_id = ""

    try:
        ctx = AccountContext()
        if ctx.is_connected():
            creds, _ = ctx.get_active()
            account_id = creds.account_id
    except Exception:
        pass

    try:
        handler = TOOL_HANDLERS.get(name)
        if not handler:
            result = json.dumps({
                "error": f"Unknown tool: {name}",
                "available_tools": list(TOOL_HANDLERS.keys()),
            })
        else:
            result = await handler(**arguments)

        # Scrub response for prompt injection.
        try:
            parsed = json.loads(result)
            scrubbed = scrub_tool_response(parsed, account_id=account_id, tool=name)
            result = json.dumps(scrubbed)
        except (json.JSONDecodeError, TypeError):
            result = str(scrub_tool_response(result, account_id=account_id, tool=name))

        duration_ms = int((time.time() - start) * 1000)
        audit_logger.info(
            json.dumps({
                "event": "tool_call",
                "tool": name,
                "account_id": account_id,
                "duration_ms": duration_ms,
                "success": True,
            })
        )

        return [TextContent(type="text", text=result)]

    except ReadOnlyViolation as exc:
        duration_ms = int((time.time() - start) * 1000)
        logger.warning(
            "SECURITY WARNING: ReadOnlyViolation in tool '%s': %s (keyword: %s)",
            name, exc.message, exc.blocked_keyword,
        )
        audit_logger.warning(
            json.dumps({
                "event": "SECURITY_WARNING",
                "tool": name,
                "account_id": account_id,
                "violation": exc.message,
                "blocked_keyword": exc.blocked_keyword,
                "duration_ms": duration_ms,
            })
        )
        error_result = json.dumps({
            "error": "Operation blocked: this server is read-only.",
            "tool": name,
            "hint": "Only read/query operations are allowed.",
            "data_available": False,
        })
        return [TextContent(type="text", text=error_result)]

    except Exception as exc:
        duration_ms = int((time.time() - start) * 1000)
        logger.error("Tool '%s' failed: %s", name, exc)
        audit_logger.info(
            json.dumps({
                "event": "tool_call",
                "tool": name,
                "account_id": account_id,
                "duration_ms": duration_ms,
                "success": False,
                "error": str(exc),
            })
        )
        error_result = json.dumps({
            "error": str(exc),
            "tool": name,
            "data_available": False,
        })
        return [TextContent(type="text", text=error_result)]


# ── Entry points ─────────────────────────────────────────────────────────

async def _auto_connect_from_env() -> None:
    """Auto-connect using .env credentials if present and no account is active."""
    ctx = AccountContext()
    if ctx.is_connected():
        return

    account_id = os.getenv("NEW_RELIC_ACCOUNT_ID", "").strip()
    api_key = os.getenv("NEW_RELIC_API_KEY", "").strip()
    region = os.getenv("NEW_RELIC_REGION", "US").strip().upper()

    if not account_id or not api_key:
        logger.debug("No .env credentials found — skipping auto-connect")
        return

    logger.info("Auto-connecting to account %s from .env", account_id)
    try:
        result = await connect_account(account_id, api_key, region)
        result_data = json.loads(result)
        if result_data.get("status") == "connected":
            logger.info(
                "Auto-connect successful for account %s (%s)",
                account_id,
                result_data.get("account_name", ""),
            )
        else:
            logger.warning(
                "Auto-connect failed for account %s: %s",
                account_id,
                result_data.get("error", "unknown error"),
            )
    except Exception as exc:
        logger.warning("Auto-connect failed: %s", exc)


async def main() -> None:
    """Start the MCP server on stdio transport."""
    logger.info("Sherlock server starting (stdio transport)")
    await _auto_connect_from_env()
    async with stdio_server() as (read_stream, write_stream):
        await app.run(read_stream, write_stream, app.create_initialization_options())


def main_sync() -> None:
    """Synchronous entry point for the MCP server."""
    asyncio.run(main())


if __name__ == "__main__":
    main_sync()
