"""
Grafana alerting tools for Sherlock.

Provides two MCP tools:
  connect_grafana      - authenticate and store a Grafana connection.
  get_grafana_alerts   - retrieve firing alerts from Grafana Alertmanager.
"""

import json
import logging
import time
from typing import Any

from client.grafana.client import GrafanaClient
from core.datasource_context import DataSourceContext

logger = logging.getLogger("sherlock.tools.grafana")


async def connect_grafana(
    url: str,
    token: str | None = None,
    api_key: str | None = None,
    username: str | None = None,
    password: str | None = None,
    org_id: int | None = None,
) -> str:
    """Connect to a Grafana instance and store the active connection.

    Supports self-hosted Grafana and Grafana Cloud.

    Preferred auth (choose one):
      connect_grafana(url="http://grafana:3000", token="<service-account-token>")
      connect_grafana(url="http://grafana:3000", username="admin", password="admin")
      connect_grafana(url="http://grafana:3000", api_key="<legacy-api-key>")

    No auth (unsecured local Grafana):
      connect_grafana(url="http://grafana:3000")

    Args:
        url: Base URL of the Grafana server (e.g. 'http://grafana:3000').
        token: Service account token or personal API token (recommended).
        api_key: Legacy Grafana API key. Equivalent to token.
        username: Basic auth username (for local installs — typically 'admin').
        password: Basic auth password.
        org_id: Optional Grafana organization ID (multi-org only).

    Returns:
        JSON string with connection status, Grafana version, and discovered datasources.
    """
    start = time.time()
    try:
        if not url or not url.strip():
            return json.dumps({
                "error": "Grafana URL is required.",
                "tool": "connect_grafana",
                "data_available": False,
            })

        client = GrafanaClient(
            base_url=url.strip(),
            token=token,
            api_key=api_key,
            username=username,
            password=password,
            org_id=org_id,
        )

        # Health check
        health = await client.health()
        db_status = health.get("database", "unknown")
        version = health.get("version", "unknown")
        if db_status != "ok":
            return json.dumps({
                "error": f"Grafana health check failed: database={db_status}",
                "tool": "connect_grafana",
                "hint": "Check Grafana server status and database connectivity.",
                "data_available": False,
            })

        # Discover datasources (best-effort — fails gracefully on insufficient permissions)
        datasources: list[dict[str, Any]] = []
        try:
            datasources = await client.get_datasources()
        except Exception as exc:
            logger.debug("Could not list datasources (may lack permissions): %s", exc)

        ds_summary = [
            {"name": ds.get("name"), "type": ds.get("type"), "url": ds.get("url")}
            for ds in datasources
        ]

        DataSourceContext().set_grafana(client)
        duration_ms = int((time.time() - start) * 1000)
        logger.info("Connected to Grafana: url=%s version=%s", url, version)

        return json.dumps({
            "status": "connected",
            "url": client.base_url,
            "grafana_version": version,
            "database": db_status,
            "datasources_count": len(datasources),
            "datasources": ds_summary,
            "duration_ms": duration_ms,
            "data_available": True,
        })

    except Exception as exc:
        duration_ms = int((time.time() - start) * 1000)
        logger.error("Grafana connection failed: %s", exc)
        return json.dumps({
            "error": str(exc),
            "tool": "connect_grafana",
            "hint": (
                "Check URL, credentials, and that the Grafana server is reachable. "
                "For token auth use a service account token with 'Viewer' role."
            ),
            "data_available": False,
            "duration_ms": duration_ms,
        })


async def get_grafana_alerts(
    severity: str | None = None,
    service: str | None = None,
    namespace: str | None = None,
    include_silenced: bool = False,
    include_rules: bool = False,
) -> str:
    """Get firing alerts from Grafana Alertmanager (unified alerting).

    Tries Grafana Alertmanager v2 API first (Grafana 8+), falls back to
    the legacy alerts API automatically if v2 is not available.

    Args:
        severity: Optional severity filter — e.g. 'critical', 'warning', 'info'.
            Matches the 'severity' label on the alert.
        service: Optional service name filter. Matches 'service' or 'job' labels.
        namespace: Optional namespace filter. Matches 'namespace' label.
        include_silenced: Include silenced alerts in results (default False).
        include_rules: Also return alert rule definitions alongside firing instances
            (default False — rule definitions are large, only request when needed).

    Returns:
        JSON string with firing alerts, grouped by severity, and optionally rule definitions.
    """
    ctx = DataSourceContext()
    client = ctx.get_grafana()
    if client is None:
        return json.dumps({
            "error": "Not connected to Grafana. Call connect_grafana first.",
            "tool": "get_grafana_alerts",
            "data_available": False,
        })

    start = time.time()
    filter_labels: dict[str, str] = {}
    if severity:
        filter_labels["severity"] = severity
    if service:
        filter_labels["service"] = service
    if namespace:
        filter_labels["namespace"] = namespace

    # Try Grafana Alertmanager v2 first
    alerts: list[dict[str, Any]] = []
    api_version_used = "v2"
    try:
        alerts = await client.get_firing_alerts(
            active=True,
            silenced=include_silenced,
            inhibited=False,
            filter_labels=filter_labels if filter_labels else None,
        )
    except Exception as exc:
        logger.warning("Alertmanager v2 failed (%s), trying legacy API", exc)
        api_version_used = "legacy"
        try:
            alerts = await client.get_legacy_alerts(state="alerting", limit=200)
            # Apply label filters manually for legacy API
            if severity:
                alerts = [a for a in alerts if str(a.get("severity", "")).lower() == severity.lower()]
            if service:
                alerts = [a for a in alerts if service.lower() in str(a.get("name", "")).lower()]
        except Exception as exc2:
            return json.dumps({
                "error": f"Both Alertmanager v2 and legacy alerts API failed: {exc2}",
                "tool": "get_grafana_alerts",
                "data_available": False,
            })

    # Normalise alert objects to a consistent shape
    normalised = []
    for a in alerts:
        labels = a.get("labels", {})
        annotations = a.get("annotations", {})
        status = a.get("status", {})
        normalised.append({
            "name": labels.get("alertname") or a.get("name", "unknown"),
            "severity": labels.get("severity", "unknown"),
            "state": status.get("state", "active") if isinstance(status, dict) else a.get("state", "alerting"),
            "service": labels.get("service") or labels.get("job"),
            "namespace": labels.get("namespace"),
            "summary": annotations.get("summary") or annotations.get("message", ""),
            "description": annotations.get("description", ""),
            "runbook_url": annotations.get("runbook_url") or annotations.get("runbook", ""),
            "starts_at": a.get("startsAt") or a.get("evalDate"),
            "generator_url": a.get("generatorURL") or a.get("url", ""),
            "fingerprint": a.get("fingerprint", ""),
            "labels": labels,
        })

    # Group by severity
    by_severity: dict[str, list] = {}
    for a in normalised:
        sev = (a["severity"] or "unknown").lower()
        by_severity.setdefault(sev, []).append(a)

    # Optional: fetch rule definitions
    rules: list[dict] = []
    if include_rules:
        try:
            rules = await client.get_alert_rules()
        except Exception as exc:
            logger.debug("Could not fetch alert rules: %s", exc)

    duration_ms = int((time.time() - start) * 1000)
    total = len(normalised)
    critical_count = len(by_severity.get("critical", []))
    warning_count = len(by_severity.get("warning", []))

    return json.dumps({
        "status": "critical" if critical_count > 0 else ("warning" if warning_count > 0 else "healthy"),
        "total_firing": total,
        "by_severity": {k: len(v) for k, v in by_severity.items()},
        "alerts": normalised,
        "alert_rules": rules,
        "api_version_used": api_version_used,
        "filters_applied": filter_labels,
        "grafana_url": client.base_url,
        "duration_ms": duration_ms,
        "data_available": total > 0,
    })
