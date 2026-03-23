"""
Azure Monitor tools for Sherlock.

Provides six MCP tools:
  connect_azure_monitor              — authenticate with a Service Principal.
  connect_azure_monitor_cli          — authenticate via Azure CLI (az login).
  connect_azure_monitor_interactive  — authenticate via browser device-code flow (no install needed).
  query_azure_monitor_metrics        — query Azure Monitor resource metrics.
  query_azure_monitor_logs           — query Azure Log Analytics using KQL.
  get_azure_monitor_alerts           — list fired alert instances and/or alert rules.
"""

import json
import logging
import time

from client.azure_monitor import AzureMonitorClient, AzureMonitorCliClient, AzureMonitorInteractiveClient
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


async def connect_azure_monitor_interactive(
    subscription_id: str = "",
) -> str:
    """Connect to Azure Monitor via browser device-code flow — no Azure CLI or service principal needed.

    Prints a short URL and a one-time code to the terminal. Open the URL in any
    browser, enter the code, and sign in with your Azure account. Sherlock then
    stores the connection automatically.

    Refresh tokens are cached in memory so re-authentication is not needed
    within the same server session (~1 hour).

    Args:
        subscription_id: Optional Azure subscription ID. If omitted, the first
            subscription available to your account is used.

    Returns:
        JSON string with connection status and subscription name.
    """
    import json as _json

    start = time.time()
    try:
        client = AzureMonitorInteractiveClient(
            subscription_id=subscription_id.strip() or "auto",
        )

        # Authenticate (device code flow — blocks until user completes browser sign-in).
        # Fetches subscription list to resolve auto-detect and validate access.
        mgmt_token = await client._get_token(
            "https://management.azure.com/.default"
        )

        # Resolve subscription_id if not provided.
        if not subscription_id or not subscription_id.strip():
            # Check token cache for a previously chosen subscription — avoids re-prompting.
            try:
                from client.azure_monitor import _TOKEN_CACHE_PATH
                import json as _json_cache
                if _TOKEN_CACHE_PATH.exists():
                    _cached = _json_cache.loads(_TOKEN_CACHE_PATH.read_text())
                    subscription_id = _cached.get("subscription_id", "")
            except Exception:
                subscription_id = ""

        if not subscription_id or not subscription_id.strip():
            import httpx
            async with httpx.AsyncClient(timeout=15) as http:
                resp = await http.get(
                    "https://management.azure.com/subscriptions",
                    params={"api-version": "2022-12-01"},
                    headers={"Authorization": f"Bearer {mgmt_token}"},
                )
                resp.raise_for_status()
                subs = resp.json().get("value", [])
            if not subs:
                return _json.dumps({
                    "error": "No Azure subscriptions found for this account.",
                    "tool": "connect_azure_monitor_interactive",
                    "data_available": False,
                })
            if len(subs) > 1:
                # List available subscriptions and prompt user to pick.
                print("\n  Available subscriptions:")
                for i, s in enumerate(subs, 1):
                    state = s.get("state", "?")
                    print(f"    {i}. {s['displayName']} ({s['subscriptionId']})  [{state}]")
                print()
                while True:
                    try:
                        choice = input(f"  Select subscription [1-{len(subs)}] (default 1): ").strip()
                        idx = int(choice) - 1 if choice else 0
                        if 0 <= idx < len(subs):
                            chosen = subs[idx]
                            break
                        print(f"  Please enter a number between 1 and {len(subs)}.")
                    except ValueError:
                        print("  Please enter a number.")
                subscription_id = chosen["subscriptionId"]
                print(f"  Using: {chosen['displayName']} ({subscription_id})")
            else:
                subscription_id = subs[0]["subscriptionId"]
        client.subscription_id = subscription_id

        sub_info = await client.validate()
        sub_name = sub_info.get("displayName", subscription_id)
        sub_state = sub_info.get("state", "unknown")

        DataSourceContext().set_azure_monitor(client)
        # Persist subscription_id into token cache so other tools can auto-discover it.
        client._save_token_cache()
        try:
            from client.azure_monitor import _TOKEN_CACHE_PATH
            import json as _json_sub
            _cache = _json_sub.loads(_TOKEN_CACHE_PATH.read_text()) if _TOKEN_CACHE_PATH.exists() else {}
            _cache["subscription_id"] = subscription_id
            _TOKEN_CACHE_PATH.write_text(_json_sub.dumps(_cache))
        except Exception:
            pass
        duration_ms = int((time.time() - start) * 1000)
        logger.info(
            "Connected to Azure Monitor via device code: subscription=%s (%s)",
            subscription_id, sub_name,
        )

        return _json.dumps({
            "status": "connected",
            "auth_method": "device_code",
            "subscription_id": subscription_id,
            "subscription_name": sub_name,
            "subscription_state": sub_state,
            "duration_ms": duration_ms,
        })

    except RuntimeError as exc:
        return json.dumps({
            "error": str(exc),
            "tool": "connect_azure_monitor_interactive",
            "data_available": False,
        })
    except Exception as exc:
        duration_ms = int((time.time() - start) * 1000)
        logger.error("Azure Monitor interactive connection failed: %s", exc)
        return json.dumps({
            "error": str(exc),
            "tool": "connect_azure_monitor_interactive",
            "hint": "Ensure your account has 'Monitoring Reader' on the subscription.",
            "data_available": False,
            "duration_ms": duration_ms,
        })


async def connect_azure_monitor_cli(
    subscription_id: str = "",
) -> str:
    """Connect to Azure Monitor using the Azure CLI (az login) — no credentials needed.

    Uses 'az account get-access-token' to obtain tokens from your existing
    CLI session. Run 'az login' (or 'az login --use-device-code') once first.
    No Tenant ID, Client ID, or Client Secret required.

    Args:
        subscription_id: Optional Azure subscription ID to use.
            If omitted, uses the subscription set as default in 'az account show'.

    Returns:
        JSON string with connection status and subscription name.
    """
    import subprocess
    import json as _json

    start = time.time()
    try:
        # Discover subscription from az CLI if not provided.
        if not subscription_id or not subscription_id.strip():
            try:
                proc = subprocess.run(
                    ["az", "account", "show", "--output", "json"],
                    capture_output=True, text=True, timeout=10,
                )
                if proc.returncode != 0:
                    return _json.dumps({
                        "error": (
                            "No subscription_id provided and 'az account show' failed. "
                            "Run 'az login' first, then retry."
                        ),
                        "tool": "connect_azure_monitor_cli",
                        "hint": "Run: az login",
                        "data_available": False,
                    })
                account = _json.loads(proc.stdout)
                subscription_id = account["id"]
            except FileNotFoundError:
                return _json.dumps({
                    "error": "Azure CLI (az) not found.",
                    "tool": "connect_azure_monitor_cli",
                    "hint": "Install Azure CLI: https://aka.ms/installazurecli",
                    "data_available": False,
                })

        client = AzureMonitorCliClient(subscription_id=subscription_id.strip())

        # Validate by fetching subscription details via CLI token.
        sub_info = await client.validate()
        sub_name = sub_info.get("displayName", subscription_id)
        sub_state = sub_info.get("state", "unknown")

        DataSourceContext().set_azure_monitor(client)
        duration_ms = int((time.time() - start) * 1000)
        logger.info(
            "Connected to Azure Monitor via CLI: subscription=%s (%s)",
            subscription_id, sub_name,
        )

        return _json.dumps({
            "status": "connected",
            "auth_method": "azure_cli",
            "subscription_id": subscription_id,
            "subscription_name": sub_name,
            "subscription_state": sub_state,
            "duration_ms": duration_ms,
        })

    except RuntimeError as exc:
        return json.dumps({
            "error": str(exc),
            "tool": "connect_azure_monitor_cli",
            "hint": "Run 'az login' (or 'az login --use-device-code') and try again.",
            "data_available": False,
        })
    except Exception as exc:
        duration_ms = int((time.time() - start) * 1000)
        logger.error("Azure Monitor CLI connection failed: %s", exc)
        return json.dumps({
            "error": str(exc),
            "tool": "connect_azure_monitor_cli",
            "hint": "Ensure 'az login' was successful and you have 'Monitoring Reader' role.",
            "data_available": False,
            "duration_ms": duration_ms,
        })


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


async def get_azure_monitor_alerts(
    time_range: str = "1h",
    severity: str | None = None,
    resource_group: str | None = None,
    include_rules: bool = False,
    limit: int = 100,
) -> str:
    """List currently fired Azure Monitor alert instances, and optionally the
    configured alert rule definitions.

    REQUIRES: connect_azure_monitor must be called first.

    Use this to answer:
      - "Are there any active Azure alerts?"
      - "What alert rules are configured for this subscription?"
      - "Show me Sev0/Sev1 critical alerts fired in the last hour."

    Args:
        time_range: Window to look back for fired alerts.
            Allowed values: '1h' (default), '6h', '1d', '7d', '30d'.
        severity: Optional severity filter. One of: Sev0, Sev1, Sev2, Sev3, Sev4.
            Sev0 = Critical, Sev1 = Error, Sev2 = Warning, Sev3 = Informational,
            Sev4 = Verbose. Omit to return all severities.
        resource_group: Optional Azure resource group name to scope the query.
            If omitted, queries the entire subscription.
        include_rules: If True, also return the configured metric alert rule
            definitions (thresholds, criteria, enabled state). Defaults to False.
        limit: Maximum fired alert instances to return (1–250). Defaults to 100.

    Returns:
        JSON string with:
          - alert_count: Number of fired alert instances.
          - alerts: List of objects with id, name, severity, alertState,
            monitorCondition, targetResource, targetResourceType, firedDateTime,
            resolvedDateTime, and alertRule.
          - rule_count (if include_rules=True): Number of configured alert rules.
          - rules (if include_rules=True): List of alert rule objects with name,
            severity, enabled, scopes, criteria, windowSize.
          - data_available: bool.
          - duration_ms: Query execution time.
    """
    start = time.time()
    ctx = DataSourceContext()
    client = ctx.get_azure_monitor()

    if client is None:
        return json.dumps({
            "error": "Azure Monitor not connected.",
            "tool": "get_azure_monitor_alerts",
            "hint": "Call connect_azure_monitor first with your tenant_id, client_id, client_secret, and subscription_id.",
            "data_available": False,
        })

    try:
        raw_alerts = await client.get_fired_alerts(
            time_range=time_range,
            severity=severity,
            resource_group=resource_group,
            limit=limit,
        )

        raw_alert_list: list = raw_alerts.get("value", [])
        alerts = [
            {
                "id": item.get("id", ""),
                "name": item.get("name", ""),
                "severity": (
                    item.get("properties", {}).get("essentials", {}).get("severity", "")
                ),
                "alertState": (
                    item.get("properties", {}).get("essentials", {}).get("alertState", "")
                ),
                "monitorCondition": (
                    item.get("properties", {}).get("essentials", {}).get("monitorCondition", "")
                ),
                "targetResource": (
                    item.get("properties", {}).get("essentials", {}).get("targetResourceName", "")
                    or item.get("properties", {}).get("essentials", {}).get("targetResource", "")
                ),
                "targetResourceType": (
                    item.get("properties", {}).get("essentials", {}).get("targetResourceType", "")
                ),
                "targetResourceGroup": (
                    item.get("properties", {}).get("essentials", {}).get("targetResourceGroup", "")
                ),
                "monitorService": (
                    item.get("properties", {}).get("essentials", {}).get("monitorService", "")
                ),
                "description": (
                    item.get("properties", {}).get("essentials", {}).get("description", "")
                ),
                "signalType": (
                    item.get("properties", {}).get("essentials", {}).get("signalType", "")
                ),
                "firedDateTime": (
                    item.get("properties", {}).get("essentials", {}).get("startDateTime", "")
                ),
                "resolvedDateTime": (
                    item.get("properties", {}).get("essentials", {}).get("resolvedDateTime", "")
                ),
                "alertRule": (
                    item.get("properties", {}).get("essentials", {}).get("alertRule", "")
                ),
            }
            for item in raw_alert_list
        ]

        result: dict = {
            "time_range": time_range,
            "severity_filter": severity,
            "resource_group_filter": resource_group,
            "alert_count": len(alerts),
            "alerts": alerts,
            "data_available": True,
            "duration_ms": int((time.time() - start) * 1000),
        }

        if include_rules:
            raw_rules = await client.get_alert_rules(resource_group=resource_group)
            raw_rule_list: list = raw_rules.get("value", [])
            rules = [
                {
                    "name": r.get("name", ""),
                    "severity": r.get("properties", {}).get("severity", ""),
                    "enabled": r.get("properties", {}).get("enabled", False),
                    "scopes": r.get("properties", {}).get("scopes", []),
                    "windowSize": r.get("properties", {}).get("windowSize", ""),
                    "criteria": r.get("properties", {}).get("criteria", {}),
                    "location": r.get("location", ""),
                }
                for r in raw_rule_list
            ]
            result["rule_count"] = len(rules)
            result["rules"] = rules

        return json.dumps(result)

    except Exception as exc:
        logger.error("get_azure_monitor_alerts failed: %s", exc)
        return json.dumps({
            "error": str(exc),
            "tool": "get_azure_monitor_alerts",
            "hint": (
                "Ensure the service principal has "
                "'Monitoring Reader' + 'AlertsManagement Reader' roles "
                "on the subscription."
            ),
            "data_available": False,
            "duration_ms": int((time.time() - start) * 1000),
        })
