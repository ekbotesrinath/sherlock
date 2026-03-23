"""
Datadog metrics tools for Sherlock.

Provides two MCP tools:
  connect_datadog         — authenticate and store a Datadog connection.
  query_datadog_metrics   — execute a Datadog metrics query.
"""

import json
import logging
import time

from client.datadog import DatadogClient
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
