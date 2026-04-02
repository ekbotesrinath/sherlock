"""Grafana Loki log search tools for Sherlock.

Provides two MCP tools:
  connect_loki        - authenticate and store a Loki connection.
  search_loki_logs    - search logs using a LogQL query.
"""

import json
import logging
import time

from client.loki.client import LokiClient
from core.datasource_context import DataSourceContext

logger = logging.getLogger("sherlock.tools.loki")

MAX_LOGQL_LENGTH = 2000


async def connect_loki(
    url: str,
    token: str | None = None,
    username: str | None = None,
    password: str | None = None,
    org_id: str | None = None,
) -> str:
    """Connect to a Grafana Loki instance and store the active connection.

    Supports both self-hosted Loki and Grafana Cloud Loki.

    For self-hosted Loki:
      connect_loki(url="http://loki:3100")

    For Grafana Cloud Loki (basic auth):
      connect_loki(url="https://logs-prod-us-central1.grafana.net",
                   username="123456", password="<api-key>")

    For Loki with Bearer token:
      connect_loki(url="http://loki:3100", token="<token>")

    Args:
        url: Base URL of the Loki server.
        token: Optional Bearer token for authentication.
        username: Optional Basic auth username (Grafana Cloud tenant ID).
        password: Optional Basic auth password (Grafana Cloud API key).
        org_id: Optional tenant/org ID sent as X-Scope-OrgID header.
            Required for multi-tenant Loki. Omit for single-tenant deployments.

    Returns:
        JSON string with connection status.
    """
    start = time.time()
    try:
        if not url or not url.strip():
            return json.dumps({
                "error": "Loki URL is required.",
                "tool": "connect_loki",
                "data_available": False,
            })

        client = LokiClient(
            base_url=url.strip(),
            token=token,
            username=username,
            password=password,
            org_id=org_id,
        )

        healthy = await client.health()
        if not healthy:
            return json.dumps({
                "error": f"Loki at '{url}' did not respond to health check.",
                "tool": "connect_loki",
                "hint": (
                    "Verify the URL and network connectivity. "
                    "Loki exposes GET /ready returning HTTP 200 when ready."
                ),
                "data_available": False,
            })

        # Discover available labels to confirm the connection is usable.
        try:
            known_labels = await client.labels()
        except Exception:
            known_labels = []

        DataSourceContext().set_loki(client)
        duration_ms = int((time.time() - start) * 1000)
        logger.info("Connected to Loki: %s", url)

        return json.dumps({
            "status": "connected",
            "url": url.strip(),
            "auth": "bearer" if token else ("basic" if username else "none"),
            "org_id": org_id or "default",
            "known_labels": known_labels[:20],  # First 20 for context
            "duration_ms": duration_ms,
        })

    except Exception as exc:
        duration_ms = int((time.time() - start) * 1000)
        logger.error("Loki connection failed: %s", exc)
        return json.dumps({
            "error": str(exc),
            "tool": "connect_loki",
            "hint": "Check the Loki URL and authentication configuration.",
            "data_available": False,
            "duration_ms": duration_ms,
        })


async def search_loki_logs(
    logql: str = "",
    since_minutes: int = 60,
    limit: int = 100,
    direction: str = "backward",
) -> str:
    """Search Grafana Loki logs using a LogQL query.

    REQUIRES: connect_loki must be called first.

    LogQL quick reference:
      - Stream selector (required): {label="value"}
        e.g. {service="api"}, {namespace="prod"}, {job="nginx"}
      - Filter operator:
        |= "text"      exact match
        != "text"      exclude
        |~ "regex"     regex match
        !~ "regex"     regex exclude
      - Examples:
        {service="checkout"} |= "error"
        {namespace="prod",app="api"} |~ "timeout|exception"
        {job="varlogs"} | json | level="error"
        rate({service="api"}[5m])   <- metric query

    Args:
        logql: LogQL query string. Must include a stream selector in braces.
            Use empty string to be prompted with available labels.
        since_minutes: Look-back window in minutes. Defaults to 60.
        limit: Maximum number of log lines to return (1-5000). Defaults to 100.
        direction: 'backward' for newest-first (default), 'forward' for oldest-first.

    Returns:
        JSON string with:
          - logql: The query used.
          - since_minutes: Time window.
          - log_count: Total log lines returned.
          - streams: List of log stream objects, each with:
              - labels: dict of label key/value pairs.
              - entries: list of {timestamp, line} objects.
          - flat_logs: Flat list of {timestamp, labels, line} for easy reading.
          - available_labels: Known label names (only returned when no logql
            is provided, to help build queries).
          - data_available: bool.
          - duration_ms: Query execution time.
    """
    start = time.time()
    ctx = DataSourceContext()
    client = ctx.get_loki()

    if client is None:
        return json.dumps({
            "error": "Loki not connected.",
            "tool": "search_loki_logs",
            "hint": "Call connect_loki first with your Loki URL.",
            "data_available": False,
        })

    # If no query provided, return available labels to help build one.
    if not logql or not logql.strip():
        try:
            labels = await client.labels()
            return json.dumps({
                "error": "logql is required. Use the label names below to build a query.",
                "tool": "search_loki_logs",
                "hint": (
                    "Example: search_loki_logs(logql='{service=\"api\"} |= \"error\"', "
                    "since_minutes=30)"
                ),
                "available_labels": labels,
                "data_available": False,
            })
        except Exception as exc:
            return json.dumps({
                "error": "logql is required.",
                "tool": "search_loki_logs",
                "labels_error": str(exc),
                "data_available": False,
            })

    if len(logql) > MAX_LOGQL_LENGTH:
        return json.dumps({
            "error": (
                f"LogQL query too long ({len(logql)} chars, max {MAX_LOGQL_LENGTH})."
            ),
            "tool": "search_loki_logs",
            "data_available": False,
        })

    try:
        now = time.time()
        start_ts = now - (since_minutes * 60)

        result = await client.query_range(
            logql=logql.strip(),
            start_ts=start_ts,
            end_ts=now,
            limit=limit,
            direction=direction,
        )

        status = result.get("status", "unknown")
        data = result.get("data", {})
        result_type = data.get("resultType", "unknown")
        raw_results: list = data.get("result", [])

        streams: list[dict] = []
        flat_logs: list[dict] = []

        if result_type == "streams":
            for stream in raw_results:
                stream_labels: dict = stream.get("stream", {})
                entries: list = stream.get("values", [])
                parsed_entries = [
                    {"timestamp": e[0], "line": e[1]}
                    for e in entries
                    if len(e) >= 2
                ]
                streams.append({
                    "labels": stream_labels,
                    "entries": parsed_entries,
                    "entry_count": len(parsed_entries),
                })
                for entry in parsed_entries:
                    flat_logs.append({
                        "timestamp": entry["timestamp"],
                        "labels": stream_labels,
                        "line": entry["line"],
                    })
        else:
            # Metric query — return raw resultType and results.
            streams = raw_results

        duration_ms = int((time.time() - start) * 1000)
        logger.info(
            "Loki query returned %d streams, %d lines in %dms (type=%s)",
            len(streams), len(flat_logs), duration_ms, result_type,
        )

        return json.dumps({
            "logql": logql,
            "loki_url": client.base_url,
            "since_minutes": since_minutes,
            "direction": direction,
            "loki_status": status,
            "result_type": result_type,
            "stream_count": len(streams),
            "log_count": len(flat_logs),
            "streams": streams,
            "flat_logs": flat_logs[:limit],
            "data_available": True,
            "duration_ms": duration_ms,
        })

    except Exception as exc:
        logger.error("search_loki_logs failed: %s", exc)
        return json.dumps({
            "error": str(exc),
            "tool": "search_loki_logs",
            "hint": (
                "Check LogQL syntax — the stream selector {label=\"value\"} is required. "
                "Run with empty logql to get available label names."
            ),
            "data_available": False,
            "duration_ms": int((time.time() - start) * 1000),
        })
