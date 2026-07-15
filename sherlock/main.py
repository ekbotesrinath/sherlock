"""
Sherlock — main entry point.

Registers all MCP tools on a FastMCP server (input schemas are derived
from the tool functions' type hints), configures logging, and starts the
stdio transport. Every tool response is scrubbed for prompt injection and
audit-logged before being returned to the client.
"""

import asyncio
import functools
import json
import logging
import logging.handlers
import os
import time
from collections.abc import Awaitable, Callable
from typing import Any, Literal

from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP
from pythonjsonlogger.json import JsonFormatter

from sherlock.core.context import AccountContext
from sherlock.core.exceptions import ReadOnlyViolation
from sherlock.core.sanitize import scrub_tool_response
from sherlock.paths import DOTENV_FILE, LOG_DIR

# ── Load environment ─────────────────────────────────────────────────────

# Load .env from the project root (not cwd) so credentials are found
# regardless of how the MCP server is launched.
load_dotenv(DOTENV_FILE)

# ── Directory setup ──────────────────────────────────────────────────────

LOG_DIR.mkdir(parents=True, exist_ok=True)

# ── Logging setup (NEVER write to stdout — reserved for MCP protocol) ───


def _json_formatter() -> JsonFormatter:
    """Build the shared JSON log formatter.

    python-json-logger escapes quotes/newlines properly — the previous
    hand-rolled format string produced invalid JSON whenever a message
    contained either (B5). extra= fields are serialized automatically.
    """
    return JsonFormatter(
        "%(asctime)s %(levelname)s %(name)s %(message)s",
        rename_fields={"asctime": "time", "levelname": "level", "name": "logger"},
    )


def _setup_logging() -> None:
    """Configure structured JSON logging to file only."""
    root_logger = logging.getLogger("sherlock")
    root_logger.setLevel(logging.DEBUG)
    # FastMCP calls logging.basicConfig (stderr); keep sherlock logs
    # file-only by not propagating to the root logger.
    root_logger.propagate = False

    # Main log — rotating 10MB, 5 backups.
    main_handler = logging.handlers.RotatingFileHandler(
        LOG_DIR / "sherlock.log",
        maxBytes=10 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    main_handler.setLevel(logging.DEBUG)
    main_handler.setFormatter(_json_formatter())
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
    audit_handler.setFormatter(_json_formatter())
    audit_logger.addHandler(audit_handler)

    # Suppress logging to stdout/stderr from other libraries.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


_setup_logging()

logger = logging.getLogger("sherlock.main")
audit_logger = logging.getLogger("sherlock.audit")

# ── Tool imports ─────────────────────────────────────────────────────────

from sherlock.tools.alerts import get_alerts, get_incidents, get_service_incidents
from sherlock.tools.apm import get_apm_applications, get_app_metrics, get_deployments
from sherlock.tools.changes import get_recent_changes
from sherlock.tools.dependencies import get_service_dependencies
from sherlock.tools.golden_signals import get_service_golden_signals
from sherlock.tools.history_tools import find_similar_incidents
from sherlock.tools.intelligence_tools import (
    connect_account,
    get_account_summary,
    get_frustration_context_tool,
    get_nrql_context,
    get_session_context_tool,
    get_structured_report_tool,
    learn_account_tool,
    list_profiles,
    resolve_account,
)
from sherlock.tools.investigate import investigate_service
from sherlock.tools.k8s import get_k8s_health
from sherlock.tools.logs import search_logs
from sherlock.tools.nrql import run_nrql_query
from sherlock.tools.synthetics import (
    get_monitor_results,
    get_monitor_status,
    get_synthetic_monitors,
    investigate_synthetic,
)
from sherlock.tools.traces import get_slow_traces, summarize_trace
from sherlock.tools.webhook_tools import post_report_to_webhook

ToolHandler = Callable[..., Awaitable[str]]

# ── Tool registry ────────────────────────────────────────────────────────
# (name, handler, MCP description). Input schemas come from each handler's
# type hints via FastMCP — enums via Literal params, defaults from keyword
# defaults, required-ness from parameters without defaults.

_TOOL_SPECS: list[tuple[str, ToolHandler, str]] = [
    (
        "connect_account",
        connect_account,
        "Connect to a New Relic account. Call this first — required before "
        "all other tools. Validates credentials, learns the account structure, "
        "and caches intelligence. Connect with a saved profile_name (loads "
        "credentials from the OS keychain) or a service_name (auto-resolves "
        "the account from memory). NEVER pass api_key: it is deprecated and "
        "leaks the key into the conversation — profiles are created once via "
        "the sherlock-validate CLI.",
    ),
    (
        "list_profiles",
        list_profiles,
        "List all saved New Relic credential profiles.",
    ),
    (
        "learn_account",
        learn_account_tool,
        "Discover ALL entity names, types, and relationships in the active account. "
        "MUST be called before any investigation to get real service names, K8s "
        "deployment names, synthetic monitor names, and entity relationships. "
        "Results are cached — subsequent calls refresh the cache.",
    ),
    (
        "get_account_summary",
        get_account_summary,
        "Get complete intelligence summary for the active account.",
    ),
    (
        "get_session_context",
        get_session_context_tool,
        "Return investigation history from the current session. "
        "Use this to answer follow-up questions like 'is it still degraded?', "
        "'why did that happen again?', or 'what was the last service we checked?' "
        "without running a full new investigation. "
        "Optionally filter by service_name. "
        "Session history is lost when VS Code restarts.",
    ),
    (
        "get_frustration_context",
        get_frustration_context_tool,
        "Detect if the engineer is in a frustration or retry loop. "
        "Combines language signals (frustration keywords in the prompt) "
        "with retry signals (same service investigated multiple times recently). "
        "Returns mode=ESCALATION when frustrated, triggering a different "
        "investigation strategy that avoids repeating failed queries and "
        "broadens the investigation scope. "
        "Call at the start of any investigation where frustration is suspected.",
    ),
    (
        "get_structured_report",
        get_structured_report_tool,
        "Return the most recent investigation as machine-readable structured JSON. "
        "This is the machine-readable parallel to the human markdown report. "
        "Use this to feed MTTR dashboards, Slack/Teams notifications, "
        "ticketing systems, or programmatic comparisons between investigations. "
        "Supports three formats: "
        "'full' (all investigation fields), "
        "'summary' (verdict + root cause only), "
        "'metrics' (numeric values only). "
        "Run an investigation first, then call this.",
    ),
    (
        "get_nrql_context",
        get_nrql_context,
        "Call before constructing any NRQL to get real service names, "
        "monitor names, and attribute names for this account.",
    ),
    (
        "investigate_service",
        investigate_service,
        "Full multi-domain investigation of a service in ONE call: golden "
        "signals with last-week baseline, recent changes with error-onset "
        "correlation, error logs, K8s health, incidents, dependencies, and "
        "associated synthetic monitors — all run concurrently server-side. "
        "Returns one structured bundle with an aggregated verdict. Prefer "
        "this over calling the individual domain tools when starting an "
        "investigation.",
    ),
    (
        "investigate_synthetic",
        investigate_synthetic,
        "Use when a synthetic monitor is failing or a login flow / "
        "health check is broken. Deep investigation with APM correlation.",
    ),
    (
        "get_service_golden_signals",
        get_service_golden_signals,
        "Get the four golden signals (latency, traffic, errors, saturation) "
        "for an APM service with trend data, plus a baseline comparison vs "
        "the same window last week (delta percentages and verdicts like "
        "'error rate 24x last week'). Essential for APM domain analysis.",
    ),
    (
        "get_k8s_health",
        get_k8s_health,
        "Get Kubernetes health data (pods, restarts, resource usage, deployments) "
        "for a service or namespace. Use the BARE deployment name (after '/') "
        "and namespace (before '/') for best results. Automatically adapts to "
        "single-cluster vs multi-cluster accounts.",
    ),
    (
        "search_logs",
        search_logs,
        "Search logs with filters for service, severity, and keywords. "
        "Essential for log domain analysis and error pattern detection. "
        "Results include log_patterns: drain-style template clusters that "
        "collapse thousands of repetitive messages into distinct error "
        "patterns with counts — read those before the raw log lines.",
    ),
    (
        "get_synthetic_monitors",
        get_synthetic_monitors,
        "List all synthetic monitors for the active account with status summary.",
    ),
    (
        "get_monitor_status",
        get_monitor_status,
        "Use to check if a specific synthetic monitor is passing or failing "
        "and in which locations.",
    ),
    (
        "get_monitor_results",
        get_monitor_results,
        "Get raw run results for a synthetic monitor, useful for digging into failures.",
    ),
    (
        "get_apm_applications",
        get_apm_applications,
        "List all APM applications for the active account.",
    ),
    (
        "get_app_metrics",
        get_app_metrics,
        "Get key performance metrics for an APM application.",
    ),
    (
        "get_deployments",
        get_deployments,
        "Get recent deployment history for an APM application.",
    ),
    (
        "get_recent_changes",
        get_recent_changes,
        "Answer 'what changed?' — one timeline of APM deployment markers and "
        "K8s rollout events, auto-correlated against the service's error-rate "
        "onset (e.g. 'deploy X landed 4m before the spike'). Call this EARLY "
        "in any investigation where errors started suddenly. Omit service_name "
        "for account-wide changes.",
    ),
    (
        "get_slow_traces",
        get_slow_traces,
        "Find the slowest distributed traces touching a service (APM or "
        "OTel), with percentile context ('4.2s = 8x the p95'). Use when "
        "latency is elevated and you need to see WHICH requests are slow. "
        "Feed a returned trace_id to summarize_trace to find the hot span.",
    ),
    (
        "summarize_trace",
        summarize_trace,
        "Summarize ONE distributed trace: total duration, per-service time "
        "breakdown, errored spans, and the hot span (largest self-time) — "
        "'72% of this trace is one SELECT in payment-svc'. Call after "
        "get_slow_traces to explain where a slow request spent its time.",
    ),
    (
        "get_alerts",
        get_alerts,
        "Get all alert policies for the active account. Takes no parameters. "
        "Returns account-wide policies, not service-specific.",
    ),
    (
        "get_incidents",
        get_incidents,
        "Get incidents filtered by state (open or closed). Returns account-wide "
        "incidents — does NOT accept a service name filter. Use "
        "get_service_incidents for service-specific incidents.",
    ),
    (
        "get_service_incidents",
        get_service_incidents,
        "Get incidents related to a specific service or monitor. "
        "Use this instead of get_incidents when filtering by service name.",
    ),
    (
        "run_nrql_query",
        run_nrql_query,
        "Execute a raw NRQL query. Use get_nrql_context first to get valid names.",
    ),
    (
        "get_service_dependencies",
        get_service_dependencies,
        "Get upstream and downstream service dependencies for an APM service. "
        "Shows which services call this service and which services it calls, "
        "with health warnings for unhealthy dependencies.",
    ),
    (
        "find_similar_incidents",
        find_similar_incidents,
        "Match the current investigation against PAST incidents from the "
        "persistent history (survives restarts): 'this looks like the "
        "incident from 3 days ago — root cause was X'. Scores matches on "
        "service name, symptom keywords vs recorded root causes, and known "
        "causal patterns. Call early in an investigation, passing the "
        "symptoms as free text.",
    ),
    (
        "post_report_to_webhook",
        post_report_to_webhook,
        "Post the most recent investigation report to the team's Slack or "
        "Teams channel via the operator-configured incoming webhook. Use "
        "AFTER an investigation when the user asks to share/notify the "
        "team. Requires SHERLOCK_SLACK_WEBHOOK_URL or "
        "SHERLOCK_TEAMS_WEBHOOK_URL to be set in the server environment.",
    ),
    (
        "resolve_account",
        resolve_account,
        "Resolve which New Relic account a service belongs to. "
        "Call BEFORE connect_account when investigating a service. "
        "Returns the profile_name and account_id for instant connection, "
        "skipping the learn cycle if the service was previously indexed.",
    ),
]

# Raw (un-instrumented) handlers, kept for the CLI and for direct testing.
TOOL_HANDLERS: dict[str, ToolHandler] = {name: handler for name, handler, _ in _TOOL_SPECS}


# ── Cross-cutting instrumentation (scrub + audit + error contract) ──────


def _instrumented(name: str, handler: ToolHandler) -> ToolHandler:
    """Wrap a tool handler with response scrubbing, audit logging, and the
    never-raise JSON error contract.

    functools.wraps preserves the handler's signature (via __wrapped__), so
    FastMCP still derives the input schema from the original type hints.
    """

    @functools.wraps(handler)
    async def wrapper(*args: Any, **kwargs: Any) -> str:
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
            result = await handler(*args, **kwargs)

            # Scrub response for prompt injection.
            try:
                parsed = json.loads(result)
                scrubbed = scrub_tool_response(parsed, account_id=account_id, tool=name)
                result = json.dumps(scrubbed)
            except (json.JSONDecodeError, TypeError):
                result = str(scrub_tool_response(result, account_id=account_id, tool=name))

            duration_ms = int((time.time() - start) * 1000)
            audit_logger.info(
                json.dumps(
                    {
                        "event": "tool_call",
                        "tool": name,
                        "account_id": account_id,
                        "duration_ms": duration_ms,
                        "success": True,
                    }
                )
            )
            return result

        except ReadOnlyViolation as exc:
            duration_ms = int((time.time() - start) * 1000)
            logger.warning(
                "SECURITY WARNING: ReadOnlyViolation in tool '%s': %s (keyword: %s)",
                name,
                exc.message,
                exc.blocked_keyword,
            )
            audit_logger.warning(
                json.dumps(
                    {
                        "event": "SECURITY_WARNING",
                        "tool": name,
                        "account_id": account_id,
                        "violation": exc.message,
                        "blocked_keyword": exc.blocked_keyword,
                        "duration_ms": duration_ms,
                    }
                )
            )
            return json.dumps(
                {
                    "error": "Operation blocked: this server is read-only.",
                    "tool": name,
                    "hint": "Only read/query operations are allowed.",
                    "data_available": False,
                }
            )

        except Exception as exc:
            duration_ms = int((time.time() - start) * 1000)
            logger.error("Tool '%s' failed: %s", name, exc)
            audit_logger.info(
                json.dumps(
                    {
                        "event": "tool_call",
                        "tool": name,
                        "account_id": account_id,
                        "duration_ms": duration_ms,
                        "success": False,
                        "error": str(exc),
                    }
                )
            )
            return json.dumps(
                {
                    "error": str(exc),
                    "tool": name,
                    "data_available": False,
                }
            )

    return wrapper


# ── MCP Server ───────────────────────────────────────────────────────────

server = FastMCP("sherlock", log_level="WARNING")

for _name, _handler, _description in _TOOL_SPECS:
    server.add_tool(
        _instrumented(_name, _handler),
        name=_name,
        description=_description,
        # Tools return JSON strings; keep the wire format plain TextContent.
        structured_output=False,
    )


# ── Entry points ─────────────────────────────────────────────────────────


def _warn_if_plaintext_key(api_key: str) -> bool:
    """Warn when an API key is supplied via plaintext environment/.env.

    Args:
        api_key: The API key value read from the environment.

    Returns:
        True if a plaintext key was detected and a warning was logged.
    """
    if not api_key:
        return False
    logger.warning(
        "SECURITY WARNING: a plaintext New Relic API key was found in the "
        "environment (.env or shell). Keys on disk are readable by any local "
        "process. Move it to the OS keychain via scripts/validate_connection.py "
        "and remove NEW_RELIC_API_KEY from .env."
    )
    return True


async def _auto_connect_from_env() -> None:
    """Auto-connect using .env credentials if present and no account is active."""
    ctx = AccountContext()
    if ctx.is_connected():
        return

    account_id = os.getenv("NEW_RELIC_ACCOUNT_ID", "").strip()
    api_key = os.getenv("NEW_RELIC_API_KEY", "").strip()
    region_env = os.getenv("NEW_RELIC_REGION", "US").strip().upper()
    region: Literal["US", "EU"] = "EU" if region_env == "EU" else "US"

    _warn_if_plaintext_key(api_key)

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
    from sherlock.client.newrelic import close_shared_http_client

    logger.info("Sherlock server starting (stdio transport)")
    await _auto_connect_from_env()
    try:
        await server.run_stdio_async()
    finally:
        await close_shared_http_client()


def main_sync() -> None:
    """Synchronous entry point for the MCP server."""
    asyncio.run(main())


if __name__ == "__main__":
    main_sync()
