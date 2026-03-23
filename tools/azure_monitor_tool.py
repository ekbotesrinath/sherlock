"""
Azure Monitor tools for Sherlock.

Provides three MCP tools:
  connect_azure_monitor           — authenticate with a Service Principal.
  query_azure_monitor_metrics     — query Azure Monitor resource metrics.
  query_azure_monitor_logs        — query Azure Log Analytics using KQL.
"""

import json
import logging
import time

from client.azure_monitor import AzureMonitorClient
from core.datasource_context import DataSourceContext

logger = logging.getLogger("sherlock.tools.azure_monitor")


def _minutes_to_iso8601(minutes: int) -> str:
    """Convert a duration in minutes to an ISO 8601 period string.

    Args:
        minutes: Duration in minutes.

    Returns:
        ISO 8601 string e.g. 'PT1H', 'PT30M', 'PT1H30M'.
    """
    hours = minutes // 60
    mins = minutes % 60
    if hours > 0 and mins > 0:
        return f"PT{hours}H{mins}M"
    if hours > 0:
        return f"PT{hours}H"
    return f"PT{mins}M"


async def connect_azure_monitor(
    tenant_id: str,
    client_id: str,
    client_secret: str,
    subscription_id: str,
) -> str:
    """Connect to Azure Monitor using a Service Principal (client credentials flow).

    The service principal requires at minimum the 'Monitoring Reader' role
    on the target subscription. For Log Analytics queries, it also needs
    'Log Analytics Reader' on the workspace.

    Args:
        tenant_id: Azure Active Directory tenant ID (GUID).
        client_id: Service principal application (client) ID.
        client_secret: Service principal client secret.
        subscription_id: Azure subscription ID to query.

    Returns:
        JSON string with connection status and subscription name.
    """
    start = time.time()
    try:
        required = {
            "tenant_id": tenant_id,
            "client_id": client_id,
            "client_secret": client_secret,
            "subscription_id": subscription_id,
        }
        missing = [k for k, v in required.items() if not v or not v.strip()]
        if missing:
            return json.dumps({
                "error": f"Missing required parameters: {', '.join(missing)}.",
                "tool": "connect_azure_monitor",
                "data_available": False,
            })

        client = AzureMonitorClient(
            tenant_id=tenant_id.strip(),
            client_id=client_id.strip(),
            client_secret=client_secret.strip(),
            subscription_id=subscription_id.strip(),
        )

        # Validate by fetching subscription details — confirms auth + RBAC.
        sub_info = await client.validate()
        sub_name = sub_info.get("displayName", subscription_id)
        sub_state = sub_info.get("state", "unknown")

        DataSourceContext().set_azure_monitor(client)
        duration_ms = int((time.time() - start) * 1000)
        logger.info(
            "Connected to Azure Monitor: subscription=%s (%s)",
            subscription_id, sub_name,
        )

        return json.dumps({
            "status": "connected",
            "subscription_id": subscription_id,
            "subscription_name": sub_name,
            "subscription_state": sub_state,
            "tenant_id": tenant_id,
            "duration_ms": duration_ms,
        })

    except PermissionError as exc:
        duration_ms = int((time.time() - start) * 1000)
        return json.dumps({
            "error": str(exc),
            "tool": "connect_azure_monitor",
            "hint": "Verify tenant_id, client_id, and client_secret are correct.",
            "data_available": False,
            "duration_ms": duration_ms,
        })
    except Exception as exc:
        duration_ms = int((time.time() - start) * 1000)
        logger.error("Azure Monitor connection failed: %s", exc)
        return json.dumps({
            "error": str(exc),
            "tool": "connect_azure_monitor",
            "hint": (
                "Ensure the service principal has 'Monitoring Reader' role on the "
                "subscription. Verify all four credential fields are correct."
            ),
            "data_available": False,
            "duration_ms": duration_ms,
        })


async def query_azure_monitor_metrics(
    resource_id: str,
    metric_names: str,
    since_minutes: int = 60,
    interval: str = "PT5M",
    aggregation: str = "Average",
) -> str:
    """Query Azure Monitor resource metrics.

    Args:
        resource_id: Full Azure resource ID
            (e.g. '/subscriptions/xxx/resourceGroups/rg/providers/
            Microsoft.Web/sites/my-app').
        metric_names: Comma-separated metric names
            (e.g. 'Percentage5XX,RequestCount,AverageResponseTime').
        since_minutes: Time window in minutes to query back from now.
        interval: Granularity interval — PT1M, PT5M, PT15M, PT30M, PT1H.
        aggregation: Aggregation type — Average, Total, Minimum, Maximum, Count.

    Returns:
        JSON string with metric timeseries for each requested metric.
    """
    start = time.time()
    try:
        client = DataSourceContext().get_azure_monitor()
        if not client:
            return json.dumps({
                "error": (
                    "No Azure Monitor connection active. "
                    "Call connect_azure_monitor first."
                ),
                "tool": "query_azure_monitor_metrics",
                "data_available": False,
            })

        if not resource_id or not resource_id.strip():
            return json.dumps({
                "error": "resource_id is required.",
                "tool": "query_azure_monitor_metrics",
                "data_available": False,
            })
        if not metric_names or not metric_names.strip():
            return json.dumps({
                "error": "metric_names is required (comma-separated).",
                "tool": "query_azure_monitor_metrics",
                "hint": "Example: 'Percentage5XX,RequestCount'",
                "data_available": False,
            })

        timespan = _minutes_to_iso8601(since_minutes)
        metrics = [m.strip() for m in metric_names.split(",") if m.strip()]

        result = await client.query_metrics(
            resource_id=resource_id.strip(),
            metric_names=metrics,
            timespan=timespan,
            interval=interval,
            aggregation=aggregation,
        )

        value = result.get("value", [])
        duration_ms = int((time.time() - start) * 1000)
        logger.info(
            "Azure Monitor metrics query in %dms, %d metrics returned",
            duration_ms, len(value),
        )

        return json.dumps({
            "resource_id": resource_id,
            "metric_names": metrics,
            "since_minutes": since_minutes,
            "timespan": timespan,
            "interval": interval,
            "aggregation": aggregation,
            "metric_count": len(value),
            "metrics": value,
            "duration_ms": duration_ms,
        })

    except Exception as exc:
        duration_ms = int((time.time() - start) * 1000)
        logger.error("Azure Monitor metrics query failed: %s", exc)
        return json.dumps({
            "error": str(exc),
            "tool": "query_azure_monitor_metrics",
            "hint": (
                "Verify resource_id format and metric names. "
                "Use Azure Portal → resource → Metrics to find valid metric names."
            ),
            "data_available": False,
            "duration_ms": duration_ms,
        })


async def query_azure_monitor_logs(
    workspace_id: str,
    kql: str,
    since_minutes: int = 60,
) -> str:
    """Query Azure Log Analytics using KQL.

    Args:
        workspace_id: Log Analytics workspace ID (GUID), found in
            Azure Portal → Log Analytics workspace → Overview.
        kql: KQL query string
            (e.g. 'AzureDiagnostics | where Category == "FrontdoorAccessLog"
            | summarize count() by httpStatusCode_d').
        since_minutes: Time window in minutes to query back from now.

    Returns:
        JSON string with query result tables and rows.
    """
    start = time.time()
    try:
        client = DataSourceContext().get_azure_monitor()
        if not client:
            return json.dumps({
                "error": (
                    "No Azure Monitor connection active. "
                    "Call connect_azure_monitor first."
                ),
                "tool": "query_azure_monitor_logs",
                "data_available": False,
            })

        if not workspace_id or not workspace_id.strip():
            return json.dumps({
                "error": "workspace_id is required.",
                "tool": "query_azure_monitor_logs",
                "data_available": False,
            })
        if not kql or not kql.strip():
            return json.dumps({
                "error": "kql query string is required.",
                "tool": "query_azure_monitor_logs",
                "data_available": False,
            })

        timespan = _minutes_to_iso8601(since_minutes)

        result = await client.query_logs(
            workspace_id=workspace_id.strip(),
            kql=kql.strip(),
            timespan=timespan,
        )

        tables = result.get("tables", [])
        row_count = sum(len(t.get("rows", [])) for t in tables)
        duration_ms = int((time.time() - start) * 1000)
        logger.info(
            "Azure Monitor logs query in %dms, %d rows returned",
            duration_ms, row_count,
        )

        return json.dumps({
            "workspace_id": workspace_id,
            "kql": kql,
            "since_minutes": since_minutes,
            "timespan": timespan,
            "row_count": row_count,
            "tables": tables,
            "duration_ms": duration_ms,
        })

    except Exception as exc:
        duration_ms = int((time.time() - start) * 1000)
        logger.error("Azure Monitor logs query failed: %s", exc)
        return json.dumps({
            "error": str(exc),
            "tool": "query_azure_monitor_logs",
            "hint": (
                "Verify workspace_id and KQL syntax. "
                "Ensure the service principal has 'Log Analytics Reader' role "
                "on the workspace."
            ),
            "data_available": False,
            "duration_ms": duration_ms,
        })
