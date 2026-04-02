"""Datadog metrics, monitor, and log tools for Sherlock.

Provides four MCP tools:
  connect_datadog         - authenticate and store a Datadog connection.
  query_datadog_metrics   - execute a Datadog metrics query.
  get_datadog_monitors    - list monitors and/or triggered alert state.
  search_datadog_logs     - search Datadog logs with a query string.
"""

import json
import logging
import time

from client.datadog.client import DatadogClient
from core.datasource_context import DataSourceContext

logger = logging.getLogger("sherlock.tools.datadog")


async def connect_datadog(
    api_key: str,
    app_key: str,
    site: str = "US1",
) -> str:
    """Connect to Datadog and store the active connection.

    Args:
        api_key: Datadog API key.
        app_key: Datadog Application key.
        site: Datadog site region — US1, US3, US5, EU1, or AP1. Defaults to US1.

    Returns:
        JSON string with connection status.
    """
    start = time.time()
    try:
        if not api_key or not api_key.strip():
            return json.dumps({
                "error": "api_key is required.",
                "tool": "connect_datadog",
                "data_available": False,
            })
        if not app_key or not app_key.strip():
            return json.dumps({
                "error": "app_key is required.",
                "tool": "connect_datadog",
                "data_available": False,
            })

        client = DatadogClient(
            api_key=api_key.strip(),
            app_key=app_key.strip(),
            site=site,
        )

        validation = await client.validate()

        # Datadog /validate returns {"valid": true} on success.
        if not validation.get("valid"):
            return json.dumps({
                "error": "Datadog credential validation failed.",
                "tool": "connect_datadog",
                "hint": "Check your API key and Application key for the correct Datadog site.",
                "data_available": False,
            })

        DataSourceContext().set_datadog(client)
        duration_ms = int((time.time() - start) * 1000)
        logger.info("Connected to Datadog: site=%s", client.site)

        return json.dumps({
            "status": "connected",
            "site": client.site,
            "duration_ms": duration_ms,
        })

    except Exception as exc:
        duration_ms = int((time.time() - start) * 1000)
        logger.error("Datadog connection failed: %s", exc)
        return json.dumps({
            "error": str(exc),
            "tool": "connect_datadog",
            "hint": "Verify Datadog API key, Application key, and site region.",
            "data_available": False,
            "duration_ms": duration_ms,
        })


async def query_datadog_metrics(
    query: str,
    since_minutes: int = 60,
) -> str:
    """Query Datadog metrics for the connected account.

    Args:
        query: Datadog metrics query string
            (e.g. 'avg:system.cpu.user{*}' or
            'sum:trace.flask.request.hits{service:my-api}.as_rate()').
        since_minutes: Time window in minutes to query back from now.

    Returns:
        JSON string with metric series data, one entry per matched time series.
    """
    start = time.time()
    try:
        client = DataSourceContext().get_datadog()
        if not client:
            return json.dumps({
                "error": "No Datadog connection active. Call connect_datadog first.",
                "tool": "query_datadog_metrics",
                "data_available": False,
            })

        if not query or not query.strip():
            return json.dumps({
                "error": "Datadog metrics query string is required.",
                "tool": "query_datadog_metrics",
                "hint": "Example: 'avg:system.cpu.user{*}'",
                "data_available": False,
            })

        now = int(time.time())
        from_ts = now - (since_minutes * 60)

        result = await client.query_metrics(
            query=query.strip(),
            from_ts=from_ts,
            to_ts=now,
        )

        series = result.get("series", [])
        duration_ms = int((time.time() - start) * 1000)
        logger.info(
            "Datadog metrics query in %dms, %d series returned",
            duration_ms, len(series),
        )

        return json.dumps({
            "query": query,
            "site": client.site,
            "since_minutes": since_minutes,
            "from_ts": from_ts,
            "to_ts": now,
            "series_count": len(series),
            "series": series,
            "duration_ms": duration_ms,
        })

    except Exception as exc:
        duration_ms = int((time.time() - start) * 1000)
        logger.error("Datadog metrics query failed: %s", exc)
        return json.dumps({
            "error": str(exc),
            "tool": "query_datadog_metrics",
            "hint": "Check Datadog query syntax (e.g. 'avg:system.cpu.user{*}').",
            "data_available": False,
            "duration_ms": duration_ms,
        })


async def get_datadog_monitors(
    name_filter: str | None = None,
    tags: list[str] | None = None,
    triggered_only: bool = False,
) -> str:
    """List Datadog monitors and their current alert state.

    REQUIRES: connect_datadog must be called first.

    Use this to answer:
      - "Are there any active Datadog alerts?"
      - "What monitors are in Alert or Warn state right now?"
      - "What alert conditions are configured in Datadog?"

    Args:
        name_filter: Optional substring to filter monitor names
            (e.g. 'cpu', 'latency', 'error rate').
        tags: Optional tag filter list
            (e.g. ['env:prod', 'service:api']). Only monitors with ALL
            specified tags are returned.
        triggered_only: If True, return only monitors currently in Alert
            or Warn state. If False (default), return all monitors.

    Returns:
        JSON string with:
          - total: Total monitor count returned.
          - triggered_count: Number of monitors in Alert/Warn state.
          - monitors: List of objects with id, name, type, overall_state
            (OK/Alert/Warn/No Data/Ignored/Skipped), query, tags, priority,
            created, and modified timestamps.
          - data_available: bool.
          - duration_ms: Query execution time.
    """
    start = time.time()
    ctx = DataSourceContext()
    client = ctx.get_datadog()

    if client is None:
        return json.dumps({
            "error": "Datadog not connected.",
            "tool": "get_datadog_monitors",
            "hint": "Call connect_datadog first with your api_key, app_key, and site.",
            "data_available": False,
        })

    try:
        if triggered_only:
            raw = await client.get_triggered_monitors(tags=tags)
        else:
            raw = await client.get_monitors(name_filter=name_filter, tags=tags, limit=1000)

        monitor_list: list = raw.get("monitors", [])

        # Apply name filter client-side when using get_monitors (server-side isn't always
        # reliable for substring matching across all Datadog API versions).
        if name_filter and not triggered_only:
            lower = name_filter.lower()
            monitor_list = [
                m for m in monitor_list
                if lower in m.get("name", "").lower()
            ]

        monitors = [
            {
                "id": m.get("id"),
                "name": m.get("name", ""),
                "type": m.get("type", ""),
                "overall_state": m.get("overall_state", "Unknown"),
                "query": m.get("query", ""),
                "tags": m.get("tags", []),
                "priority": m.get("priority"),
                "created": m.get("created", ""),
                "modified": m.get("modified", ""),
            }
            for m in monitor_list
        ]

        triggered_count = sum(
            1 for m in monitors if m["overall_state"] in ("Alert", "Warn")
        )

        return json.dumps({
            "triggered_only": triggered_only,
            "name_filter": name_filter,
            "tag_filter": tags,
            "total": len(monitors),
            "triggered_count": triggered_count,
            "monitors": monitors,
            "data_available": True,
            "duration_ms": int((time.time() - start) * 1000),
        })

    except Exception as exc:
        logger.error("get_datadog_monitors failed: %s", exc)
        return json.dumps({
            "error": str(exc),
            "tool": "get_datadog_monitors",
            "hint": "Check that your Datadog app_key has 'monitors_read' scope.",
            "data_available": False,
            "duration_ms": int((time.time() - start) * 1000),
        })


async def search_datadog_logs(
    query: str = "*",
    since_minutes: int = 60,
    limit: int = 100,
    sort: str = "-timestamp",
) -> str:
    """Search Datadog logs using the Logs Events v2 API.

    REQUIRES: connect_datadog must be called first.

    Use this to answer:
      - "Are there any errors in Datadog logs?"
      - "Show me logs for service X in the last 30 minutes."
      - "Find all exceptions in the api-gateway service."

    Args:
        query: Datadog log search query string. Supports Datadog query syntax:
            - Service filter: 'service:api-gateway'
            - Status filter: 'status:error'
            - Text search: 'NullPointerException'
            - Combined: 'service:checkout status:error timeout'
            - All logs: '*' (default)
        since_minutes: Look-back window in minutes. Defaults to 60.
        limit: Maximum number of log events to return (1-1000). Defaults to 100.
        sort: Sort order — '-timestamp' (newest first, default) or
            'timestamp' (oldest first).

    Returns:
        JSON string with:
          - query: The query string used.
          - since_minutes: The time window.
          - log_count: Number of events returned.
          - logs: List of log event objects with: id, timestamp, service,
            status (ok/info/warn/error/critical), host, message, tags,
            and any other indexed attributes.
          - data_available: bool.
          - duration_ms: Query execution time.
    """
    start = time.time()
    ctx = DataSourceContext()
    client = ctx.get_datadog()

    if client is None:
        return json.dumps({
            "error": "Datadog not connected.",
            "tool": "search_datadog_logs",
            "hint": "Call connect_datadog first with your api_key, app_key, and site.",
            "data_available": False,
        })

    try:
        now = int(time.time())
        from_ts = now - (since_minutes * 60)

        raw = await client.search_logs(
            query=query,
            from_ts=from_ts,
            to_ts=now,
            limit=limit,
            sort=sort,
        )

        raw_events: list = raw.get("data", [])

        logs = [
            {
                "id": ev.get("id", ""),
                "timestamp": ev.get("attributes", {}).get("timestamp", ""),
                "service": ev.get("attributes", {}).get("service", ""),
                "status": ev.get("attributes", {}).get("status", ""),
                "host": ev.get("attributes", {}).get("host", ""),
                "message": ev.get("attributes", {}).get("message", ""),
                "tags": ev.get("attributes", {}).get("tags", []),
                "attributes": {
                    k: v
                    for k, v in ev.get("attributes", {}).get("attributes", {}).items()
                },
            }
            for ev in raw_events
        ]

        duration_ms = int((time.time() - start) * 1000)
        logger.info(
            "Datadog log search returned %d events in %dms (query=%r)",
            len(logs), duration_ms, query,
        )

        return json.dumps({
            "query": query,
            "since_minutes": since_minutes,
            "from_ts": from_ts,
            "to_ts": now,
            "sort": sort,
            "log_count": len(logs),
            "logs": logs,
            "data_available": True,
            "duration_ms": duration_ms,
        })

    except Exception as exc:
        logger.error("search_datadog_logs failed: %s", exc)
        return json.dumps({
            "error": str(exc),
            "tool": "search_datadog_logs",
            "hint": (
                "Check Datadog log query syntax. "
                "Ensure the API key has 'logs_read_data' scope."
            ),
            "data_available": False,
            "duration_ms": int((time.time() - start) * 1000),
        })
