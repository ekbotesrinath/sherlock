"""Azure Application Insights tools for Sherlock.

Provides five MCP tools:
  connect_app_insights           - authenticate with an App Insights resource.
  query_app_insights             - run a raw KQL query against any AI table.
  get_app_insights_failures      - APM golden signals: request failures + exceptions.
  get_app_insights_dependencies  - downstream dependency failures and slow calls.
  get_app_insights_availability  - availability test (ping/synthetic) results.
"""

import json
import logging
import time
from typing import Any

from client.app_insights import AppInsightsClient
from core.datasource_context import DataSourceContext
from core.deeplinks import AzureDeepLinkBuilder, get_azure_builder
from client.azure_monitor import AzureMonitorInteractiveClient, _TOKEN_CACHE_PATH

logger = logging.getLogger("sherlock.tools.app_insights")

MAX_KQL_LENGTH = 4000


def _parse_tables(raw: dict[str, Any]) -> list[dict[str, Any]]:
    """Convert raw App Insights API table response to list-of-dicts format.

    The App Insights REST API returns rows as arrays aligned to a columns
    array. This helper zips them into named-key dicts for readability.

    Args:
        raw: Parsed App Insights query response with 'tables' key.

    Returns:
        List of row dicts. Empty list if no data or on parse error.
    """
    rows: list[dict[str, Any]] = []
    for table in raw.get("tables", []):
        columns = [c["name"] for c in table.get("columns", [])]
        for row in table.get("rows", []):
            rows.append(dict(zip(columns, row)))
    return rows


async def connect_app_insights_interactive(
    subscription_id: str = "",
    app_id: str = "",
) -> str:
    """Connect to Azure Application Insights using interactive browser authentication.

    No API key, service principal, or Azure CLI required — only a browser.
    On first call a device code is printed; open the URL, enter the code.
    Subsequent calls reuse the cached token silently (~/.sherlock/azure_token_cache.json).

    If app_id is omitted, all App Insights resources in the subscription are
    listed and the first one is selected automatically (or the user is prompted
    if multiple exist).

    Args:
        subscription_id: Azure subscription ID. Auto-detected if omitted.
        app_id: Application Insights Application ID (GUID). Auto-discovered if omitted.

    Returns:
        JSON string with connection status.
    """
    import time as _time
    start = _time.time()
    try:
        # ── 1. resolve subscription ──────────────────────────────────────
        resolved_sub = subscription_id.strip()
        if not resolved_sub:
            # try to read from cache (set by a previous azure-connect-interactive run)
            if _TOKEN_CACHE_PATH.exists():
                import json as _json2
                cached = _json2.loads(_TOKEN_CACHE_PATH.read_text())
                resolved_sub = cached.get("subscription_id", "")
        if not resolved_sub:
            return json.dumps({
                "error": "subscription_id is required (or run make azure-connect-interactive first).",
                "tool": "connect_app_insights_interactive",
                "data_available": False,
            })

        ai_client = AzureMonitorInteractiveClient(subscription_id=resolved_sub)

        # ── 2. resolve app_id ────────────────────────────────────────────
        resolved_app_id = app_id.strip()
        component_name = ""
        comp_resource_id = ""  # full Azure resource ID for Portal deep links
        if not resolved_app_id:
            components = await ai_client.list_app_insights_components()
            if not components:
                return json.dumps({
                    "error": "No Application Insights components found in the subscription.",
                    "tool": "connect_app_insights_interactive",
                    "data_available": False,
                })
            # auto-pick (first); callers can pass app_id to target a specific one
            comp = components[0]
            resolved_app_id = comp["app_id"]
            component_name = comp["name"]
            comp_resource_id = comp.get("resource_id", "")
            if len(components) > 1:
                logger.info(
                    "Multiple App Insights found; using '%s' (app_id=%s). "
                    "Pass app_id to target a specific resource.",
                    component_name, resolved_app_id,
                )
        else:
            component_name = resolved_app_id  # display fallback

        # ── 3. get App Insights bearer token via device code ─────────────
        token = await ai_client.get_app_insights_token()

        # ── 4. create client and validate ────────────────────────────────
        client = AppInsightsClient(app_id=resolved_app_id, bearer_token=token, resource_id=comp_resource_id or None)
        await client.validate()

        DataSourceContext().set_app_insights(client)
        duration_ms = int((_time.time() - start) * 1000)
        logger.info("Connected to App Insights (interactive): app_id=%s", resolved_app_id)
        az = get_azure_builder()
        return json.dumps({
            "status": "connected",
            "app_id": resolved_app_id,
            "component_name": component_name,
            "auth": "interactive_device_code",
            "duration_ms": duration_ms,
            "links": {
                "overview": az.app_insights_overview(comp_resource_id) if (az and comp_resource_id) else az.app_insights_browse() if az else None,
                "failures": az.app_insights_failures(comp_resource_id) if (az and comp_resource_id) else None,
                "performance": az.app_insights_performance(comp_resource_id) if (az and comp_resource_id) else None,
                "availability": az.app_insights_availability(comp_resource_id) if (az and comp_resource_id) else None,
            },
        })

    except Exception as exc:
        duration_ms = int((_time.time() - start) * 1000)
        logger.error("App Insights interactive connection failed: %s", exc)
        return json.dumps({
            "error": str(exc),
            "tool": "connect_app_insights_interactive",
            "hint": "Run make azure-connect-interactive first to cache the management token.",
            "data_available": False,
            "duration_ms": duration_ms,
        })


async def connect_app_insights(
    app_id: str,
    api_key: str | None = None,
    tenant_id: str | None = None,
    client_id: str | None = None,
    client_secret: str | None = None,
) -> str:
    """Connect to an Azure Application Insights resource.

    Supports two auth modes — use whichever is available:

    API Key (simpler, recommended for read-only access):
      connect_app_insights(app_id="<guid>", api_key="<key>")
      Create the key in: Azure Portal > App Insights > Configure > API Access.

    AAD Service Principal:
      connect_app_insights(app_id="<guid>", tenant_id="...",
                           client_id="...", client_secret="...")
      The service principal needs 'Monitoring Reader' on the App Insights resource.

    The app_id is the Application ID shown in:
      Azure Portal > App Insights > Configure > API Access
      (It is a GUID, NOT the instrumentation key.)

    Args:
        app_id: Application Insights application ID (GUID).
        api_key: Optional API key for simple authentication.
        tenant_id: AAD tenant ID (for service principal auth).
        client_id: Service principal application/client ID.
        client_secret: Service principal client secret.

    Returns:
        JSON string with connection status and app_id confirmation.
    """
    start = time.time()
    try:
        if not app_id or not app_id.strip():
            return json.dumps({
                "error": "app_id is required.",
                "tool": "connect_app_insights",
                "hint": (
                    "Find the Application ID in Azure Portal > "
                    "App Insights > Configure > API Access."
                ),
                "data_available": False,
            })

        if not api_key and not (tenant_id and client_id and client_secret):
            return json.dumps({
                "error": (
                    "Authentication required: provide api_key OR "
                    "(tenant_id + client_id + client_secret)."
                ),
                "tool": "connect_app_insights",
                "data_available": False,
            })

        client = AppInsightsClient(
            app_id=app_id.strip(),
            api_key=api_key,
            tenant_id=tenant_id,
            client_id=client_id,
            client_secret=client_secret,
        )

        # Validate by running a minimal query.
        await client.validate()

        DataSourceContext().set_app_insights(client)
        duration_ms = int((time.time() - start) * 1000)
        logger.info("Connected to App Insights: app_id=%s", app_id)

        az = get_azure_builder()
        return json.dumps({
            "status": "connected",
            "app_id": app_id.strip(),
            "auth": "api_key" if api_key else "service_principal",
            "duration_ms": duration_ms,
            "links": {
                "browse": az.app_insights_browse() if az else None,
            },
        })

    except Exception as exc:
        duration_ms = int((time.time() - start) * 1000)
        logger.error("App Insights connection failed: %s", exc)
        return json.dumps({
            "error": str(exc),
            "tool": "connect_app_insights",
            "hint": (
                "Verify the app_id and credentials. "
                "The API key must have 'Read telemetry' permission."
            ),
            "data_available": False,
            "duration_ms": duration_ms,
        })


async def query_app_insights(
    kql: str,
    since_minutes: int = 60,
) -> str:
    """Run a raw KQL query against the connected Application Insights resource.

    REQUIRES: connect_app_insights must be called first.

    Use this for custom analysis beyond what the pre-built tools offer.
    All standard App Insights tables are available:
      requests, exceptions, dependencies, traces, customEvents,
      pageViews, availabilityResults, performanceCounters, customMetrics.

    Example queries:
      - Error rate by operation:
        requests | where timestamp >= ago(1h)
        | summarize errorRate=countif(success==false)*100.0/count()
          by name | order by errorRate desc

      - Top exceptions:
        exceptions | where timestamp >= ago(1h)
        | summarize count() by type, outerMessage | order by count_ desc

      - Slow dependencies:
        dependencies | where timestamp >= ago(1h)
        | where duration > 1000 | summarize avg(duration) by target, name

    Args:
        kql: KQL query string (max 4000 chars).
        since_minutes: Look-back window used to compute the timespan parameter.

    Returns:
        JSON string with:
          - kql: Query used.
          - row_count: Total rows returned.
          - rows: List of row dicts with column names as keys.
          - data_available: bool.
          - duration_ms: Query execution time.
    """
    start = time.time()
    client = DataSourceContext().get_app_insights()

    if client is None:
        return json.dumps({
            "error": "App Insights not connected.",
            "tool": "query_app_insights",
            "hint": "Call connect_app_insights first.",
            "data_available": False,
        })

    if not kql or not kql.strip():
        return json.dumps({
            "error": "kql is required.",
            "tool": "query_app_insights",
            "data_available": False,
        })

    if len(kql) > MAX_KQL_LENGTH:
        return json.dumps({
            "error": f"KQL query too long ({len(kql)} chars, max {MAX_KQL_LENGTH}).",
            "tool": "query_app_insights",
            "data_available": False,
        })

    # Block KQL management commands (all start with '.' e.g. .create, .drop, .alter).
    if kql.strip().startswith("."):
        return json.dumps({
            "error": "KQL management commands are not permitted. This server is read-only.",
            "tool": "query_app_insights",
            "hint": "Only SELECT-equivalent KQL queries are allowed (no '.create', '.drop', '.alter', etc.).",
            "data_available": False,
        })

    try:
        timespan = _minutes_to_iso8601(since_minutes)
        raw = await client.query(kql.strip(), timespan=timespan)
        rows = _parse_tables(raw)
        duration_ms = int((time.time() - start) * 1000)
        logger.info(
            "App Insights KQL returned %d rows in %dms",
            len(rows), duration_ms,
        )
        return json.dumps({
            "kql": kql,
            "since_minutes": since_minutes,
            "row_count": len(rows),
            "rows": rows,
            "data_available": True,
            "duration_ms": duration_ms,
        })

    except Exception as exc:
        logger.error("query_app_insights failed: %s", exc)
        return json.dumps({
            "error": str(exc),
            "tool": "query_app_insights",
            "hint": "Check KQL syntax. Use table names: requests, exceptions, dependencies, traces, etc.",
            "data_available": False,
            "duration_ms": int((time.time() - start) * 1000),
        })


async def get_app_insights_failures(
    since_minutes: int = 60,
    limit: int = 100,
    include_exceptions: bool = True,
) -> str:
    """Get APM failure data: failed HTTP requests and unhandled exceptions.

    REQUIRES: connect_app_insights must be called first.

    This is the primary tool for answering:
      - "Is the application throwing errors?"
      - "What's the error rate?"
      - "What exceptions are occurring?"
      - "Which endpoints are failing?"

    Runs two queries in parallel:
      1. Failed requests — HTTP calls with success==false, grouped by operation
         name and result code to show the failure distribution.
      2. Top exceptions — unhandled exceptions grouped by type + outerMessage
         to surface the most frequent error signatures.

    Args:
        since_minutes: Look-back window in minutes. Defaults to 60.
        limit: Max rows per query result. Defaults to 100.
        include_exceptions: Also fetch exception breakdown. Defaults to True.

    Returns:
        JSON string with:
          - time_window_minutes: Window used.
          - request_summary: Aggregated failed request counts by name+resultCode.
          - exception_summary: Top exceptions by type+message with counts.
          - failed_request_count: Total failed request rows returned.
          - exception_count: Total exception rows returned.
          - data_available: bool.
          - duration_ms: Query execution time.
    """
    start = time.time()
    client = DataSourceContext().get_app_insights()

    if client is None:
        return json.dumps({
            "error": "App Insights not connected.",
            "tool": "get_app_insights_failures",
            "hint": "Call connect_app_insights first.",
            "data_available": False,
        })

    try:
        timespan = _minutes_to_iso8601(since_minutes)

        # Query 1: Failed request summary grouped by operation + result code.
        req_kql = f"""requests
| where timestamp >= ago({since_minutes}m)
| where success == false
| summarize count=count(), avg_duration_ms=avg(duration)
    by name, resultCode, cloud_RoleName
| order by count desc
| take {limit}"""
        raw_requests = await client.query(req_kql, timespan=timespan)
        failed_requests = _parse_tables(raw_requests)

        result: dict[str, Any] = {
            "time_window_minutes": since_minutes,
            "failed_request_count": len(failed_requests),
            "request_summary": failed_requests,
            "data_available": True,
        }

        if include_exceptions:
            # Query 2: Top exceptions by type + message.
            exc_kql = f"""exceptions
| where timestamp >= ago({since_minutes}m)
| summarize count=count()
    by type, outerMessage, problemId, cloud_RoleName
| order by count desc
| take {limit}"""
            raw_exceptions = await client.query(exc_kql, timespan=timespan)
            exceptions = _parse_tables(raw_exceptions)
            result["exception_count"] = len(exceptions)
            result["exception_summary"] = exceptions

        duration_ms = int((time.time() - start) * 1000)
        result["duration_ms"] = duration_ms
        logger.info(
            "App Insights failures: %d failed req groups, %d exception types in %dms",
            len(failed_requests),
            len(result.get("exception_summary", [])),
            duration_ms,
        )
        az = get_azure_builder()
        rid = getattr(client, "resource_id", None)
        result["links"] = {
            "failures": az.app_insights_failures(rid) if (az and rid) else az.app_insights_browse() if az else None,
            "overview": az.app_insights_overview(rid) if (az and rid) else None,
        }
        return json.dumps(result)

    except Exception as exc:
        logger.error("get_app_insights_failures failed: %s", exc)
        return json.dumps({
            "error": str(exc),
            "tool": "get_app_insights_failures",
            "hint": "Verify App Insights is connected and has request/exception telemetry.",
            "data_available": False,
            "duration_ms": int((time.time() - start) * 1000),
        })


async def get_app_insights_dependencies(
    since_minutes: int = 60,
    limit: int = 100,
    failed_only: bool = False,
    slow_threshold_ms: float = 1000.0,
) -> str:
    """Get dependency call telemetry — outgoing HTTP, DB, queue, and cache calls.

    REQUIRES: connect_app_insights must be called first.

    Use this to answer:
      - "What external calls is the app making?"
      - "Are any downstream dependencies failing or slow?"
      - "Which database queries are taking too long?"

    Returns two views:
      1. Failed dependencies — grouped by target + type to show failure blast radius.
      2. Slow dependencies — calls exceeding slow_threshold_ms grouped by target.

    Args:
        since_minutes: Look-back window in minutes. Defaults to 60.
        limit: Max rows per result. Defaults to 100.
        failed_only: Return only the failed dependency summary. Defaults to False
            (returns both failed and slow).
        slow_threshold_ms: Duration threshold in ms for the slow query.
            Defaults to 1000 (1 second).

    Returns:
        JSON string with:
          - time_window_minutes: Window used.
          - failed_dependency_summary: Grouped failed calls by target+type+resultCode.
          - slow_dependency_summary: Grouped slow calls by target with avg duration.
          - data_available: bool.
          - duration_ms: Query execution time.
    """
    start = time.time()
    client = DataSourceContext().get_app_insights()

    if client is None:
        return json.dumps({
            "error": "App Insights not connected.",
            "tool": "get_app_insights_dependencies",
            "hint": "Call connect_app_insights first.",
            "data_available": False,
        })

    try:
        timespan = _minutes_to_iso8601(since_minutes)

        # Failed dependencies summary.
        fail_kql = f"""dependencies
| where timestamp >= ago({since_minutes}m)
| where success == false
| summarize count=count(), avg_duration_ms=avg(duration)
    by type, target, name, resultCode, cloud_RoleName
| order by count desc
| take {limit}"""
        raw_failed = await client.query(fail_kql, timespan=timespan)
        failed_deps = _parse_tables(raw_failed)

        result: dict[str, Any] = {
            "time_window_minutes": since_minutes,
            "slow_threshold_ms": slow_threshold_ms,
            "failed_dependency_count": len(failed_deps),
            "failed_dependency_summary": failed_deps,
            "data_available": True,
        }

        if not failed_only:
            # Slow dependencies summary.
            slow_kql = f"""dependencies
| where timestamp >= ago({since_minutes}m)
| where duration >= {slow_threshold_ms}
| summarize count=count(), avg_duration_ms=avg(duration), p95_duration_ms=percentile(duration, 95)
    by type, target, name, cloud_RoleName
| order by avg_duration_ms desc
| take {limit}"""
            raw_slow = await client.query(slow_kql, timespan=timespan)
            slow_deps = _parse_tables(raw_slow)
            result["slow_dependency_count"] = len(slow_deps)
            result["slow_dependency_summary"] = slow_deps

        duration_ms = int((time.time() - start) * 1000)
        result["duration_ms"] = duration_ms
        az = get_azure_builder()
        rid = getattr(client, "resource_id", None)
        result["links"] = {
            "performance": az.app_insights_performance(rid) if (az and rid) else az.app_insights_browse() if az else None,
            "overview": az.app_insights_overview(rid) if (az and rid) else None,
        }
        return json.dumps(result)

    except Exception as exc:
        logger.error("get_app_insights_dependencies failed: %s", exc)
        return json.dumps({
            "error": str(exc),
            "tool": "get_app_insights_dependencies",
            "hint": "Verify App Insights is connected and has dependency telemetry.",
            "data_available": False,
            "duration_ms": int((time.time() - start) * 1000),
        })


async def get_app_insights_availability(
    since_minutes: int = 60,
    limit: int = 100,
) -> str:
    """Get availability test results — uptime and ping check outcomes.

    REQUIRES: connect_app_insights must be called first.

    Use this to answer:
      - "Are availability tests passing?"
      - "From which locations are tests failing?"
      - "What is the overall uptime percentage?"

    Args:
        since_minutes: Look-back window in minutes. Defaults to 60.
        limit: Max rows to return. Defaults to 100.

    Returns:
        JSON string with:
          - time_window_minutes: Window used.
          - overall_availability_pct: Percentage of passing test runs.
          - results_by_test: Grouped by test name — pass/fail counts + avg duration.
          - recent_failures: Most recent failed test runs with location and message.
          - data_available: bool.
          - duration_ms: Query execution time.
    """
    start = time.time()
    client = DataSourceContext().get_app_insights()

    if client is None:
        return json.dumps({
            "error": "App Insights not connected.",
            "tool": "get_app_insights_availability",
            "hint": "Call connect_app_insights first.",
            "data_available": False,
        })

    try:
        timespan = _minutes_to_iso8601(since_minutes)

        # Summary by test name.
        summary_kql = f"""availabilityResults
| where timestamp >= ago({since_minutes}m)
| summarize
    pass_count=countif(success==true),
    fail_count=countif(success==false),
    total=count(),
    avg_duration_ms=avg(duration)
    by name
| extend availability_pct=pass_count*100.0/total
| order by availability_pct asc"""
        raw_summary = await client.query(summary_kql, timespan=timespan)
        summary_rows = _parse_tables(raw_summary)

        # Overall availability across all tests.
        total_pass = sum(r.get("pass_count", 0) for r in summary_rows)
        total_all = sum(r.get("total", 0) for r in summary_rows)
        overall_pct = (total_pass / total_all * 100) if total_all > 0 else None

        # Recent failures with location details.
        fail_kql = f"""availabilityResults
| where timestamp >= ago({since_minutes}m)
| where success == false
| project timestamp, name, location, duration, message, operation_Id
| order by timestamp desc
| take {limit}"""
        raw_failures = await client.query(fail_kql, timespan=timespan)
        recent_failures = _parse_tables(raw_failures)

        duration_ms = int((time.time() - start) * 1000)
        az = get_azure_builder()
        rid = getattr(client, "resource_id", None)
        return json.dumps({
            "time_window_minutes": since_minutes,
            "overall_availability_pct": round(overall_pct, 2) if overall_pct is not None else None,
            "test_count": len(summary_rows),
            "results_by_test": summary_rows,
            "recent_failure_count": len(recent_failures),
            "recent_failures": recent_failures,
            "data_available": True,
            "duration_ms": duration_ms,
            "links": {
                "availability": az.app_insights_availability(rid) if (az and rid) else az.app_insights_browse() if az else None,
                "overview": az.app_insights_overview(rid) if (az and rid) else None,
            },
        })

    except Exception as exc:
        logger.error("get_app_insights_availability failed: %s", exc)
        return json.dumps({
            "error": str(exc),
            "tool": "get_app_insights_availability",
            "hint": "Verify App Insights is connected and has availabilityResults telemetry.",
            "data_available": False,
            "duration_ms": int((time.time() - start) * 1000),
        })


def _minutes_to_iso8601(minutes: int) -> str:
    """Convert minutes to ISO 8601 duration string."""
    hours = minutes // 60
    mins = minutes % 60
    if hours > 0 and mins > 0:
        return f"PT{hours}H{mins}M"
    if hours > 0:
        return f"PT{hours}H"
    return f"PT{mins}M"
