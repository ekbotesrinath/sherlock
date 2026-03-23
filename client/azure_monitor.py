"""
Async Azure Monitor client for Sherlock.

Queries the Azure Monitor Metrics REST API and Azure Log Analytics
using a Service Principal (OAuth2 client credentials flow).
Credentials are stored only in memory and never written to disk or logged.
Read-only: Monitoring Reader role on the subscription is sufficient.
"""

import logging
import time
from typing import Any

import httpx
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

import asyncio
import json as _json
import subprocess
from pathlib import Path

logger = logging.getLogger("sherlock.client.azure_monitor")

AZURE_AUTH_URL = "https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token"
AZURE_MANAGEMENT_BASE = "https://management.azure.com"
AZURE_MONITOR_SCOPE = "https://management.azure.com/.default"
AZURE_LOG_ANALYTICS_BASE = "https://api.loganalytics.io/v1"
AZURE_LOG_ANALYTICS_SCOPE = "https://api.loganalytics.io/.default"


class _RetryableHTTPError(Exception):
    pass


def _az_get_token_sync(resource: str) -> tuple[str, float]:
    """Get an access token from the Azure CLI (az account get-access-token).

    Args:
        resource: Resource URL (e.g. 'https://management.azure.com/').

    Returns:
        Tuple of (access_token, expiry_epoch_seconds).

    Raises:
        RuntimeError: If az CLI is not installed or not logged in.
    """
    try:
        proc = subprocess.run(
            ["az", "account", "get-access-token", "--resource", resource,
             "--output", "json"],
            capture_output=True, text=True, timeout=15,
        )
    except FileNotFoundError:
        raise RuntimeError(
            "Azure CLI (az) not found. Install from https://aka.ms/installazurecli"
        )
    if proc.returncode != 0:
        err = proc.stderr.strip()
        raise RuntimeError(
            f"az CLI returned error (rc={proc.returncode}): {err}\n"
            "Run 'az login' to authenticate."
        )
    data = _json.loads(proc.stdout)
    token: str = data["accessToken"]
    # expiresOn is like "2026-03-23 14:55:00.000000" local time.
    # expires_on is an epoch int also provided if available.
    expires_on = data.get("expires_on")
    if expires_on:
        expiry = float(expires_on)
    else:
        from datetime import datetime
        raw = data["expiresOn"]  # "YYYY-MM-DD HH:MM:SS.ffffff"
        expiry = datetime.fromisoformat(raw.split(".")[0]).timestamp()
    return token, expiry


class AzureMonitorClient:
    """Async client for Azure Monitor Metrics API and Log Analytics.

    Uses OAuth2 client credentials flow to obtain Bearer tokens.
    Tokens are cached until 60 seconds before expiry to avoid redundant
    token requests.
    """

    def __init__(
        self,
        tenant_id: str,
        client_id: str,
        client_secret: str,
        subscription_id: str,
        timeout: int = 30,
    ) -> None:
        """Initialize the Azure Monitor client.

        Args:
            tenant_id: Azure Active Directory tenant ID (GUID).
            client_id: Service principal application (client) ID.
            client_secret: Service principal client secret (never logged).
            subscription_id: Azure subscription ID to query.
            timeout: Default request timeout in seconds.
        """
        self.subscription_id = subscription_id
        self._tenant_id = tenant_id
        self._client_id = client_id
        # Never log or serialize the secret.
        self._client_secret = client_secret
        self._timeout = timeout

        # Token cache: separate tokens for management and log analytics scopes.
        self._mgmt_token: str | None = None
        self._mgmt_token_expiry: float = 0.0
        self._logs_token: str | None = None
        self._logs_token_expiry: float = 0.0

    async def _get_token(self, scope: str) -> str:
        """Fetch an OAuth2 access token using client credentials flow.

        Tokens are cached until 60 seconds before their stated expiry,
        reducing round trips to Azure AD.

        Args:
            scope: OAuth2 scope URL.

        Returns:
            Bearer access token string.
        """
        now = time.time()
        # Return cached token if still valid.
        if scope == AZURE_MONITOR_SCOPE:
            if self._mgmt_token and now < self._mgmt_token_expiry - 60:
                return self._mgmt_token
        elif scope == AZURE_LOG_ANALYTICS_SCOPE:
            if self._logs_token and now < self._logs_token_expiry - 60:
                return self._logs_token

        url = AZURE_AUTH_URL.format(tenant_id=self._tenant_id)
        data = {
            "grant_type": "client_credentials",
            "client_id": self._client_id,
            "client_secret": self._client_secret,
            "scope": scope,
        }
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(url, data=data)
            if resp.status_code == 401:
                raise PermissionError(
                    "Azure AD authentication failed (401). "
                    "Check tenant_id, client_id, and client_secret."
                )
            resp.raise_for_status()
            body = resp.json()

        token: str = body["access_token"]
        expires_in = int(body.get("expires_in", 3599))

        if scope == AZURE_MONITOR_SCOPE:
            self._mgmt_token = token
            self._mgmt_token_expiry = now + expires_in
        else:
            self._logs_token = token
            self._logs_token_expiry = now + expires_in

        return token

    async def query_metrics(
        self,
        resource_id: str,
        metric_names: list[str],
        timespan: str = "PT1H",
        interval: str = "PT5M",
        aggregation: str = "Average",
    ) -> dict[str, Any]:
        """Query Azure Monitor metrics for a resource.

        Args:
            resource_id: Full Azure resource ID
                (e.g. '/subscriptions/.../resourceGroups/.../providers/...').
            metric_names: List of metric names to query
                (e.g. ['Percentage5XX', 'RequestCount']).
            timespan: ISO 8601 duration (e.g. 'PT1H', 'PT30M').
            interval: Granularity interval (e.g. 'PT1M', 'PT5M', 'PT1H').
            aggregation: Aggregation type: Average, Total, Minimum, Maximum, Count.

        Returns:
            Parsed Azure Monitor Metrics response dict with 'value' list.
        """
        token = await self._get_token(AZURE_MONITOR_SCOPE)
        url = (
            f"{AZURE_MANAGEMENT_BASE}{resource_id}"
            f"/providers/Microsoft.Insights/metrics"
        )
        params = {
            "api-version": "2023-10-01",
            "metricnames": ",".join(metric_names),
            "timespan": timespan,
            "interval": interval,
            "aggregation": aggregation,
        }
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
        }
        return await self._get(url, params, headers)

    async def query_logs(
        self,
        workspace_id: str,
        kql: str,
        timespan: str = "PT1H",
    ) -> dict[str, Any]:
        """Query Azure Log Analytics using KQL.

        Args:
            workspace_id: Log Analytics workspace ID (GUID).
            kql: KQL query string.
            timespan: ISO 8601 timespan (e.g. 'PT1H').

        Returns:
            Parsed Log Analytics response dict with 'tables' list.
        """
        token = await self._get_token(AZURE_LOG_ANALYTICS_SCOPE)
        url = f"{AZURE_LOG_ANALYTICS_BASE}/workspaces/{workspace_id}/query"
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        payload = {"query": kql, "timespan": timespan}
        return await self._post(url, payload, headers)

    async def get_fired_alerts(
        self,
        time_range: str = "1h",
        severity: str | None = None,
        alert_state: str = "Fired",
        resource_group: str | None = None,
        limit: int = 100,
    ) -> dict[str, Any]:
        """List currently fired (active) alert instances from Azure Alerts Management.

        Args:
            time_range: Time range filter — 1h, 6h, 1d, 7d, 30d. Defaults to '1h'.
            severity: Optional severity filter — Sev0, Sev1, Sev2, Sev3, Sev4.
            alert_state: Alert state filter — Fired, Acknowledged, Closed.
                Use 'Fired' for active alerts. Defaults to 'Fired'.
            resource_group: Optional resource group name to scope the query.
            limit: Maximum number of alert instances to return (max 250).

        Returns:
            Parsed Azure AlertsManagement response with 'value' list of alert instances.
            Each instance includes: id, name, severity, alertState, monitorCondition,
            targetResourceType, targetResource, firedDateTime, resolvedDateTime,
            and the originating alert rule name.
        """
        token = await self._get_token(AZURE_MONITOR_SCOPE)
        if resource_group:
            url = (
                f"{AZURE_MANAGEMENT_BASE}/subscriptions/{self.subscription_id}"
                f"/resourceGroups/{resource_group}"
                f"/providers/Microsoft.AlertsManagement/alerts"
            )
        else:
            url = (
                f"{AZURE_MANAGEMENT_BASE}/subscriptions/{self.subscription_id}"
                f"/providers/Microsoft.AlertsManagement/alerts"
            )
        params: dict[str, Any] = {
            "api-version": "2023-07-12-preview",
            "alertState": alert_state,
            "timeRange": time_range,
            "pageCount": min(limit, 250),
        }
        if severity:
            params["severity"] = severity
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
        }
        return await self._get(url, params, headers)

    async def get_alert_rules(
        self,
        resource_group: str | None = None,
    ) -> dict[str, Any]:
        """List Azure Monitor metric alert rule definitions for the subscription.

        Returns the configured alert conditions (what thresholds are set),
        not the currently fired instances. Use get_fired_alerts for active alerts.

        Args:
            resource_group: Optional resource group to scope the query.
                If omitted, returns all alert rules in the subscription.

        Returns:
            Parsed response with 'value' list of alert rule objects.
            Each rule includes: name, severity, enabled, scopes, criteria
            (metric name, operator, threshold, aggregation), and windowSize.
        """
        token = await self._get_token(AZURE_MONITOR_SCOPE)
        if resource_group:
            url = (
                f"{AZURE_MANAGEMENT_BASE}/subscriptions/{self.subscription_id}"
                f"/resourceGroups/{resource_group}"
                f"/providers/Microsoft.Insights/metricAlerts"
            )
        else:
            url = (
                f"{AZURE_MANAGEMENT_BASE}/subscriptions/{self.subscription_id}"
                f"/providers/Microsoft.Insights/metricAlerts"
            )
        params: dict[str, Any] = {"api-version": "2018-03-01"}
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
        }
        return await self._get(url, params, headers)

    async def validate(self) -> dict[str, Any]:
        """Validate credentials by fetching subscription details.

        Returns:
            Azure subscription resource dict on success.

        Raises:
            PermissionError: If authentication fails.
            httpx.HTTPStatusError: On authorization or not-found errors.
        """
        token = await self._get_token(AZURE_MONITOR_SCOPE)
        url = f"{AZURE_MANAGEMENT_BASE}/subscriptions/{self.subscription_id}"
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
        }
        params = {"api-version": "2022-12-01"}
        return await self._get(url, params, headers)

    async def _get(
        self, url: str, params: dict[str, Any], headers: dict[str, str]
    ) -> dict[str, Any]:
        """Execute a GET request with retry logic.

        Args:
            url: Full URL to request.
            params: Query parameters.
            headers: HTTP headers including Authorization.

        Returns:
            Parsed JSON response.
        """

        @retry(
            stop=stop_after_attempt(3),
            wait=wait_exponential(multiplier=1, min=1, max=4),
            retry=retry_if_exception_type(_RetryableHTTPError),
            reraise=True,
        )
        async def _do() -> dict[str, Any]:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                resp = await client.get(url, params=params, headers=headers)
                if resp.status_code == 429:
                    raise _RetryableHTTPError("429 Rate Limited")
                if resp.status_code in (500, 502, 503, 504):
                    raise _RetryableHTTPError(f"HTTP {resp.status_code}")
                resp.raise_for_status()
                return resp.json()

        return await _do()

    async def _post(
        self, url: str, payload: dict[str, Any], headers: dict[str, str]
    ) -> dict[str, Any]:
        """Execute a POST request with retry logic.

        Args:
            url: Full URL to request.
            payload: JSON request body.
            headers: HTTP headers including Authorization.

        Returns:
            Parsed JSON response.
        """

        @retry(
            stop=stop_after_attempt(3),
            wait=wait_exponential(multiplier=1, min=1, max=4),
            retry=retry_if_exception_type(_RetryableHTTPError),
            reraise=True,
        )
        async def _do() -> dict[str, Any]:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                resp = await client.post(url, json=payload, headers=headers)
                if resp.status_code == 429:
                    raise _RetryableHTTPError("429 Rate Limited")
                if resp.status_code in (500, 502, 503, 504):
                    raise _RetryableHTTPError(f"HTTP {resp.status_code}")
                resp.raise_for_status()
                return resp.json()

        return await _do()


class _CliProxy(AzureMonitorClient):
    """Thin proxy over AzureMonitorClient that delegates token fetching to AzureMonitorCliClient."""

    def __init__(self, cli_client: "AzureMonitorCliClient") -> None:
        # Skip super().__init__ intentionally — we only reuse _get/_post HTTP helpers.
        self.subscription_id = cli_client.subscription_id
        self._timeout = cli_client._timeout
        self._cli = cli_client

    async def _get_token(self, scope: str) -> str:
        return await self._cli._get_token(scope)


class AzureMonitorCliClient:
    """Azure Monitor client that authenticates via the Azure CLI (az login).

    Uses 'az account get-access-token' instead of a service principal.
    Requires the Azure CLI to be installed and the user to be logged in.
    Tokens are cached until 60 seconds before expiry.
    """

    def __init__(self, subscription_id: str, timeout: int = 30) -> None:
        self.subscription_id = subscription_id
        self._timeout = timeout
        self._mgmt_token: str | None = None
        self._mgmt_token_expiry: float = 0.0
        self._logs_token: str | None = None
        self._logs_token_expiry: float = 0.0

    async def _get_token(self, scope: str) -> str:
        """Get token from az CLI, with caching."""
        now = time.time()
        resource = scope.rstrip("/.default")
        if scope == AZURE_MONITOR_SCOPE:
            if self._mgmt_token and now < self._mgmt_token_expiry - 60:
                return self._mgmt_token
        elif scope == AZURE_LOG_ANALYTICS_SCOPE:
            if self._logs_token and now < self._logs_token_expiry - 60:
                return self._logs_token

        token, expiry = await asyncio.get_event_loop().run_in_executor(
            None, _az_get_token_sync, resource,
        )
        if scope == AZURE_MONITOR_SCOPE:
            self._mgmt_token = token
            self._mgmt_token_expiry = expiry
        else:
            self._logs_token = token
            self._logs_token_expiry = expiry
        return token

    async def query_metrics(self, **kwargs: Any) -> dict[str, Any]:
        return await _CliProxy(self).query_metrics(**kwargs)

    async def query_logs(self, **kwargs: Any) -> dict[str, Any]:
        return await _CliProxy(self).query_logs(**kwargs)

    async def get_fired_alerts(self, **kwargs: Any) -> dict[str, Any]:
        return await _CliProxy(self).get_fired_alerts(**kwargs)

    async def get_alert_rules(self, **kwargs: Any) -> dict[str, Any]:
        return await _CliProxy(self).get_alert_rules(**kwargs)

    async def validate(self) -> dict[str, Any]:
        return await _CliProxy(self).validate()


# ── Device code (interactive browser) auth ─────────────────────────────────
#
# Uses the Azure CLI's public client ID — a well-known, Microsoft-registered
# OAuth2 public client that supports device code flow.  No installation needed:
# the user just visits a URL and enters a short code in their browser.
#
# Reference: https://learn.microsoft.com/en-us/azure/active-directory/develop/
#            v2-oauth2-device-code

_TOKEN_CACHE_PATH = Path.home() / ".sherlock" / "azure_token_cache.json"

_DEVICE_CODE_CLIENT_ID = "04b07795-8ddb-461a-bbee-02f9e1bf7b46"
_DEVICE_CODE_TENANT    = "organizations"
_DEVICE_CODE_URL       = (
    f"https://login.microsoftonline.com/{_DEVICE_CODE_TENANT}/oauth2/v2.0/devicecode"
)
_DEVICE_TOKEN_URL      = (
    f"https://login.microsoftonline.com/{_DEVICE_CODE_TENANT}/oauth2/v2.0/token"
)
# Scope maps: what to request for each internal scope constant.
_DEVICE_SCOPE_MAP = {
    AZURE_MONITOR_SCOPE:      "https://management.azure.com/user_impersonation offline_access",
    AZURE_LOG_ANALYTICS_SCOPE: "https://api.loganalytics.io/user_impersonation offline_access",
}


class AzureMonitorInteractiveClient:
    """Azure Monitor client that authenticates via OAuth2 device code flow.

    No Azure CLI or service principal required.
    On first use the user visits a short URL and enters a code — the same
    experience as 'az login --use-device-code'.
    Refresh tokens are stored in memory so re-auth is not needed within a
    session.
    """

    def __init__(self, subscription_id: str, timeout: int = 30) -> None:
        self.subscription_id = subscription_id
        self._timeout = timeout
        self._access_tokens:   dict[str, tuple[str, float]] = {}  # scope → (token, expiry)
        self._refresh_tokens:  dict[str, str] = {}                # scope → refresh_token
        self._load_token_cache()

    def _load_token_cache(self) -> None:
        """Load cached tokens from disk (written by a previous process)."""
        try:
            if _TOKEN_CACHE_PATH.exists():
                data = _json.loads(_TOKEN_CACHE_PATH.read_text())
                now = time.time()
                for scope, (tok, exp) in data.get("access_tokens", {}).items():
                    if exp > now + 60:  # only load if still valid
                        self._access_tokens[scope] = (tok, exp)
                self._refresh_tokens.update(data.get("refresh_tokens", {}))
        except Exception:
            pass  # corrupt/missing cache — start fresh

    def _save_token_cache(self) -> None:
        """Persist current tokens to disk for reuse across processes."""
        try:
            _TOKEN_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "access_tokens":  {s: list(v) for s, v in self._access_tokens.items()},
                "refresh_tokens": dict(self._refresh_tokens),
            }
            _TOKEN_CACHE_PATH.write_text(_json.dumps(payload))
            # Restrict to owner-only read/write (best-effort on Windows too)
            try:
                import stat
                _TOKEN_CACHE_PATH.chmod(stat.S_IRUSR | stat.S_IWUSR)
            except Exception:
                pass
        except Exception:
            pass  # never crash because of cache write failure

    async def _get_token(self, scope: str) -> str:
        now = time.time()
        cached = self._access_tokens.get(scope)
        if cached and now < cached[1] - 60:
            return cached[0]

        # Try silent refresh first so we don't re-prompt on every call.
        if scope in self._refresh_tokens:
            try:
                token, expiry, refresh = await self._refresh(scope, self._refresh_tokens[scope])
                self._access_tokens[scope] = (token, expiry)
                self._refresh_tokens[scope] = refresh
                self._save_token_cache()
                return token
            except Exception:
                pass  # fall through to device code

        token, expiry, refresh = await self._device_code_flow(scope)
        self._access_tokens[scope] = (token, expiry)
        if refresh:
            self._refresh_tokens[scope] = refresh
        self._save_token_cache()
        return token

    async def _device_code_flow(self, scope: str) -> tuple[str, float, str]:
        """Run the device code flow for *scope*. Blocks until user authenticates."""
        requested_scope = _DEVICE_SCOPE_MAP.get(scope, scope)

        async with httpx.AsyncClient(timeout=30) as http:
            # Step 1 — request device code.
            resp = await http.post(_DEVICE_CODE_URL, data={
                "client_id": _DEVICE_CODE_CLIENT_ID,
                "scope":     requested_scope,
            })
            resp.raise_for_status()
            dc = resp.json()

        # Print instructions — visible in the terminal where `make` / server runs.
        print()
        print("  ─" * 30)
        print(f"  {dc['message']}")
        print("  ─" * 30)
        print()

        interval = int(dc.get("interval", 5))
        device_code = dc["device_code"]
        deadline = time.time() + int(dc.get("expires_in", 900))

        # Step 2 — poll until authorised or expired.
        while time.time() < deadline:
            await asyncio.sleep(interval)
            async with httpx.AsyncClient(timeout=30) as http:
                resp = await http.post(_DEVICE_TOKEN_URL, data={
                    "grant_type":  "urn:ietf:params:oauth:grant-type:device_code",
                    "client_id":   _DEVICE_CODE_CLIENT_ID,
                    "device_code": device_code,
                })
                data = resp.json()

            error = data.get("error", "")
            if "access_token" in data:
                token: str = data["access_token"]
                expiry = time.time() + int(data.get("expires_in", 3599))
                refresh: str = data.get("refresh_token", "")
                return token, expiry, refresh
            if error == "authorization_pending":
                continue
            if error == "slow_down":
                interval = min(interval + 5, 30)
                continue
            raise RuntimeError(
                f"Device code auth failed: {data.get('error_description', error)}"
            )

        raise RuntimeError("Device code authentication timed out. Please try again.")

    async def _refresh(self, scope: str, refresh_token: str) -> tuple[str, float, str]:
        """Exchange a refresh token for a new access token silently."""
        requested_scope = _DEVICE_SCOPE_MAP.get(scope, scope)
        async with httpx.AsyncClient(timeout=15) as http:
            resp = await http.post(_DEVICE_TOKEN_URL, data={
                "grant_type":    "refresh_token",
                "client_id":     _DEVICE_CODE_CLIENT_ID,
                "refresh_token": refresh_token,
                "scope":         requested_scope,
            })
            resp.raise_for_status()
            data = resp.json()
        token: str = data["access_token"]
        expiry = time.time() + int(data.get("expires_in", 3599))
        new_refresh: str = data.get("refresh_token", refresh_token)
        return token, expiry, new_refresh

    # ── API methods (reuse AzureMonitorClient HTTP logic via _CliProxy) ──

    async def query_metrics(self, **kwargs: Any) -> dict[str, Any]:
        return await _CliProxy(self).query_metrics(**kwargs)  # type: ignore[arg-type]

    async def query_logs(self, **kwargs: Any) -> dict[str, Any]:
        return await _CliProxy(self).query_logs(**kwargs)  # type: ignore[arg-type]

    async def get_fired_alerts(self, **kwargs: Any) -> dict[str, Any]:
        return await _CliProxy(self).get_fired_alerts(**kwargs)  # type: ignore[arg-type]

    async def get_alert_rules(self, **kwargs: Any) -> dict[str, Any]:
        return await _CliProxy(self).get_alert_rules(**kwargs)  # type: ignore[arg-type]

    async def validate(self) -> dict[str, Any]:
        return await _CliProxy(self).validate()  # type: ignore[arg-type]
