"""Prometheus query and Alertmanager tools for Sherlock.

Provides three MCP tools:
  connect_prometheus    - authenticate and store a Prometheus connection.
  run_promql_query      - execute a PromQL instant or range query.
  get_prometheus_alerts - list currently firing alerts from Alertmanager.
"""

import json
import logging
import time

from client.prometheus.client import PrometheusClient
from core.datasource_context import DataSourceContext

logger = logging.getLogger("sherlock.tools.promql")

MAX_PROMQL_LENGTH = 2000


async def connect_prometheus(
    url: str,
    token: str | None = None,
    username: str | None = None,
    password: str | None = None,
    alertmanager_url: str | None = None,
) -> str:
    """Connect to a Prometheus instance and store the active connection.

    Args:
        url: Base URL of the Prometheus server (e.g. 'http://prometheus:9090').
        token: Optional Bearer token for authentication.
        username: Optional Basic auth username.
        password: Optional Basic auth password (required when username is set).
        alertmanager_url: Optional base URL of the Alertmanager instance
            (e.g. 'http://alertmanager:9093'). Required for get_prometheus_alerts.

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
            alertmanager_url=alertmanager_url,
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
            "alertmanager_url": alertmanager_url or "not configured",
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


async def get_prometheus_alerts(
    label_filter: str | None = None,
    include_silenced: bool = False,
    include_inhibited: bool = False,
) -> str:
    """List currently firing alerts from Alertmanager.

    REQUIRES: connect_prometheus must be called first WITH alertmanager_url set.

    Use this to answer:
      - "Are there any active Prometheus/Alertmanager alerts?"
      - "What critical alerts are firing right now?"
      - "Show me all alerts for the 'api' service."

    Args:
        label_filter: Optional label selector to filter alerts,
            e.g. 'severity="critical"' or '{job="api-server",env="prod"}'.
            Omit to return all active alerts.
        include_silenced: Include silenced alerts. Defaults to False.
        include_inhibited: Include inhibited alerts. Defaults to False.

    Returns:
        JSON string with:
          - alertmanager_url: The Alertmanager endpoint queried.
          - alert_count: Number of firing alert instances.
          - alerts: List of alert objects with labels (name, severity, job,
            instance, etc.), annotations (summary, description), state,
            startsAt, endsAt, and generatorURL.
          - silences: List of active silences (id, matchers, createdBy,
            comment, startsAt, endsAt).
          - data_available: bool.
          - duration_ms: Query execution time.
    """
    start = time.time()
    client = DataSourceContext().get_prometheus()

    if client is None:
        return json.dumps({
            "error": "Prometheus not connected.",
            "tool": "get_prometheus_alerts",
            "hint": "Call connect_prometheus first with url and alertmanager_url.",
            "data_available": False,
        })

    if not client.alertmanager_url:
        return json.dumps({
            "error": "alertmanager_url not configured on this Prometheus connection.",
            "tool": "get_prometheus_alerts",
            "hint": "Reconnect using connect_prometheus with the alertmanager_url parameter set.",
            "data_available": False,
        })

    try:
        raw_alerts = await client.get_firing_alerts(
            label_filter=label_filter,
            include_silenced=include_silenced,
            include_inhibited=include_inhibited,
        )
        raw_silences = await client.get_silences()

        alerts = [
            {
                "name": a.get("labels", {}).get("alertname", ""),
                "labels": a.get("labels", {}),
                "annotations": a.get("annotations", {}),
                "state": a.get("status", {}).get("state", ""),
                "startsAt": a.get("startsAt", ""),
                "endsAt": a.get("endsAt", ""),
                "generatorURL": a.get("generatorURL", ""),
                "fingerprint": a.get("fingerprint", ""),
            }
            for a in raw_alerts
        ]

        active_silences = [
            {
                "id": s.get("id", ""),
                "state": s.get("status", {}).get("state", ""),
                "matchers": s.get("matchers", []),
                "createdBy": s.get("createdBy", ""),
                "comment": s.get("comment", ""),
                "startsAt": s.get("startsAt", ""),
                "endsAt": s.get("endsAt", ""),
            }
            for s in raw_silences
            if s.get("status", {}).get("state") == "active"
        ]

        duration_ms = int((time.time() - start) * 1000)
        logger.info(
            "Alertmanager returned %d alerts, %d active silences in %dms",
            len(alerts), len(active_silences), duration_ms,
        )

        return json.dumps({
            "alertmanager_url": client.alertmanager_url,
            "label_filter": label_filter,
            "alert_count": len(alerts),
            "alerts": alerts,
            "active_silence_count": len(active_silences),
            "silences": active_silences,
            "data_available": True,
            "duration_ms": duration_ms,
        })

    except Exception as exc:
        logger.error("get_prometheus_alerts failed: %s", exc)
        return json.dumps({
            "error": str(exc),
            "tool": "get_prometheus_alerts",
            "hint": (
                "Verify alertmanager_url is correct and the Alertmanager API "
                "is accessible. Default port is 9093."
            ),
            "data_available": False,
            "duration_ms": int((time.time() - start) * 1000),
        })
