"""
Prometheus query tools for Sherlock.

Provides two MCP tools:
  connect_prometheus  — authenticate and store a Prometheus connection.
  run_promql_query    — execute a PromQL instant or range query.
"""

import json
import logging
import time

from client.prometheus import PrometheusClient
from core.datasource_context import DataSourceContext

logger = logging.getLogger("sherlock.tools.promql")

MAX_PROMQL_LENGTH = 2000


async def connect_prometheus(
    url: str,
    token: str | None = None,
    username: str | None = None,
    password: str | None = None,
) -> str:
    """Connect to a Prometheus instance and store the active connection.

    Args:
        url: Base URL of the Prometheus server (e.g. 'http://prometheus:9090').
        token: Optional Bearer token for authentication.
        username: Optional Basic auth username.
        password: Optional Basic auth password (required when username is set).

    Returns:
        JSON string with connection status.
    """
    start = time.time()
    try:
        if not url or not url.strip():
            return json.dumps({
                "error": "Prometheus URL is required.",
                "tool": "connect_prometheus",
                "data_available": False,
            })

        client = PrometheusClient(
            base_url=url.strip(),
            token=token,
            username=username,
            password=password,
        )

        healthy = await client.health()
        if not healthy:
            return json.dumps({
                "error": f"Prometheus at '{url}' did not respond to health check.",
                "tool": "connect_prometheus",
                "hint": (
                    "Verify the URL and network connectivity. "
                    "The server must expose /-/healthy returning HTTP 200."
                ),
                "data_available": False,
            })

        DataSourceContext().set_prometheus(client)
        duration_ms = int((time.time() - start) * 1000)
        logger.info("Connected to Prometheus: %s", url)

        return json.dumps({
            "status": "connected",
            "url": url.strip(),
            "auth": "bearer" if token else ("basic" if username else "none"),
            "duration_ms": duration_ms,
        })

    except Exception as exc:
        duration_ms = int((time.time() - start) * 1000)
        logger.error("Prometheus connection failed: %s", exc)
        return json.dumps({
            "error": str(exc),
            "tool": "connect_prometheus",
            "hint": "Check the Prometheus URL and authentication configuration.",
            "data_available": False,
            "duration_ms": duration_ms,
        })


async def run_promql_query(
    promql: str,
    since_minutes: int = 60,
    step: str = "60s",
    query_type: str = "range",
) -> str:
    """Execute a PromQL query against the connected Prometheus instance.

    Args:
        promql: PromQL expression to evaluate
            (e.g. 'rate(http_requests_total[5m])').
        since_minutes: Time window in minutes to query back from now.
            Only used for 'range' queries.
        step: Step resolution for range queries (e.g. '60s', '5m', '1h').
        query_type: 'instant' for the current value, 'range' for a timeseries.

    Returns:
        JSON string with PromQL results including series data.
    """
    start = time.time()
    try:
        client = DataSourceContext().get_prometheus()
        if not client:
            return json.dumps({
                "error": "No Prometheus connection active. Call connect_prometheus first.",
                "tool": "run_promql_query",
                "data_available": False,
            })

        if not promql or not promql.strip():
            return json.dumps({
                "error": "Empty PromQL query.",
                "tool": "run_promql_query",
                "data_available": False,
            })

        if len(promql) > MAX_PROMQL_LENGTH:
            return json.dumps({
                "error": (
                    f"PromQL query too long ({len(promql)} chars, "
                    f"max {MAX_PROMQL_LENGTH})."
                ),
                "tool": "run_promql_query",
                "data_available": False,
            })

        now = time.time()
        if query_type == "instant":
            result = await client.query(promql.strip())
        else:
            result = await client.query_range(
                promql=promql.strip(),
                start_ts=now - (since_minutes * 60),
                end_ts=now,
                step=step,
            )

        status = result.get("status", "unknown")
        data = result.get("data", {})
        result_type = data.get("resultType", "unknown")
        results = data.get("result", [])

        duration_ms = int((time.time() - start) * 1000)
        logger.info(
            "PromQL executed in %dms, %d series (status=%s)",
            duration_ms, len(results), status,
        )

        return json.dumps({
            "promql": promql,
            "prometheus_url": client.base_url,
            "query_type": query_type,
            "since_minutes": since_minutes,
            "step": step,
            "status": status,
            "result_type": result_type,
            "series_count": len(results),
            "results": results,
            "duration_ms": duration_ms,
        })

    except Exception as exc:
        duration_ms = int((time.time() - start) * 1000)
        logger.error("PromQL query failed: %s", exc)
        return json.dumps({
            "error": str(exc),
            "tool": "run_promql_query",
            "hint": (
                "Check PromQL syntax. "
                "Ensure Prometheus is connected via connect_prometheus."
            ),
            "data_available": False,
            "duration_ms": duration_ms,
        })
