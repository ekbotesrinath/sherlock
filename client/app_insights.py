"""
Async Azure Application Insights client for Sherlock.

Queries the Application Insights REST API v1 using KQL (Kusto Query Language).
Supports both API-key auth and AAD service principal (OAuth2 client credentials).

Useful tables:
  requests      — HTTP request telemetry (duration, resultCode, success, url)
  exceptions    — Unhandled exceptions (type, message, outerMessage, stack)
  dependencies  — Outgoing calls (type, target, name, duration, success)
  traces        — Log/trace messages (message, severityLevel)
  customEvents  — Custom business events
  pageViews     — Browser page load telemetry (name, duration, url)
  availabilityResults — Availability test results (name, success, duration, location)
  performanceCounters — Server perf counters (CPU, memory, etc.)

API reference:
  https://dev.applicationinsights.io/reference/get-query
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

logger = logging.getLogger("sherlock.client.app_insights")

APP_INSIGHTS_API_BASE = "https://api.applicationinsights.io/v1/apps"
APP_INSIGHTS_SCOPE = "https://api.applicationinsights.io/.default"
AZURE_AUTH_URL = "https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token"


class _RetryableHTTPError(Exception):
    pass


class AppInsightsClient:
    """Async client for the Azure Application Insights REST API v1.

    Supports two authentication modes:
      1. API key: simple, set X-API-Key header. Read-only by design.
      2. AAD service principal: OAuth2 client credentials, requires
         'Monitoring Reader' on the App Insights resource.

    All requests are GET-only (read-only enforcement).
    Credentials are stored only in memory and never written to disk or logged.
    """

    def __init__(
        self,
        app_id: str,
        api_key: str | None = None,
        tenant_id: str | None = None,
        client_id: str | None = None,
        client_secret: str | None = None,
        bearer_token: str | None = None,
        resource_id: str | None = None,
        timeout: int = 30,
    ) -> None:
        """Initialize the Application Insights client.

        Provide either ``api_key`` (simpler) or the AAD triple
        ``tenant_id`` + ``client_id`` + ``client_secret``.

        Args:
            app_id: Application Insights application ID (found in the
                Azure portal under App Insights > Configure > API Access).
                This is a GUID, NOT the instrumentation key.
            api_key: Optional API key for simple auth. Created in the
                portal under App Insights > Configure > API Access.
            tenant_id: AAD tenant ID (required for service principal auth).
            client_id: Service principal app/client ID.
            client_secret: Service principal client secret (never logged).
            resource_id: Full Azure resource ID for this App Insights component
                (e.g. /subscriptions/.../providers/microsoft.insights/components/name).
                Used to build Azure Portal deep links.  Optional.
            timeout: Default HTTP request timeout in seconds.
        """
        self.app_id = app_id
        self.resource_id = resource_id  # used for Portal deep links
        self._timeout = timeout

        # Auth mode selection: API key > direct bearer > AAD service principal.
        self._api_key = api_key
        self._bearer_token = bearer_token  # pre-fetched token (e.g. from device code flow)
        self._tenant_id = tenant_id
        self._client_id = client_id
        self._client_secret = client_secret

        # AAD token cache.
        self._aad_token: str | None = None
        self._aad_token_expiry: float = 0.0

    async def _get_headers(self) -> dict[str, str]:
        """Build request headers with the appropriate auth scheme.

        Returns:
            Headers dict with Authorization or X-API-Key.

        Raises:
            ValueError: If neither api_key nor AAD credentials are configured.
        """
        if self._api_key:
            return {
                "X-API-Key": self._api_key,
                "Accept": "application/json",
            }
        elif self._bearer_token:
            return {
                "Authorization": f"Bearer {self._bearer_token}",
                "Accept": "application/json",
            }
        elif self._tenant_id and self._client_id and self._client_secret:
            token = await self._get_aad_token()
            return {
                "Authorization": f"Bearer {token}",
                "Accept": "application/json",
            }
        else:
            raise ValueError(
                "AppInsightsClient requires api_key, bearer_token, or "
                "(tenant_id + client_id + client_secret)."
            )

    async def _get_aad_token(self) -> str:
        """Fetch an AAD Bearer token using client credentials flow.

        Tokens are cached until 60 seconds before expiry.

        Returns:
            Bearer access token string.
        """
        now = time.time()
        if self._aad_token and now < self._aad_token_expiry - 60:
            return self._aad_token

        url = AZURE_AUTH_URL.format(tenant_id=self._tenant_id)
        data = {
            "grant_type": "client_credentials",
            "client_id": self._client_id,
            "client_secret": self._client_secret,
            "scope": APP_INSIGHTS_SCOPE,
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

        self._aad_token = body["access_token"]
        self._aad_token_expiry = now + int(body.get("expires_in", 3599))
        return self._aad_token  # type: ignore[return-value]

    async def query(
        self,
        kql: str,
        timespan: str = "PT1H",
    ) -> dict[str, Any]:
        """Execute a KQL query against the Application Insights REST API.

        Args:
            kql: KQL query string. Can reference any App Insights table
                (requests, exceptions, dependencies, traces, customEvents,
                pageViews, availabilityResults, performanceCounters).
            timespan: ISO 8601 duration to restrict the query window
                (e.g. 'PT1H', 'PT30M', 'P1D'). Applied server-side as
                an additional time filter.

        Returns:
            Parsed response dict with 'tables' list. Each table has 'name',
            'columns' (list of {name, type}), and 'rows' (list of lists).
        """
        headers = await self._get_headers()
        url = f"{APP_INSIGHTS_API_BASE}/{self.app_id}/query"
        params: dict[str, Any] = {"query": kql, "timespan": timespan}
        return await self._get(url, params, headers)

    async def get_requests(
        self,
        since_minutes: int = 60,
        top_n: int = 100,
        min_duration_ms: float | None = None,
        failed_only: bool = False,
    ) -> dict[str, Any]:
        """Get HTTP request telemetry — the primary APM golden signal source.

        Returns request count, failure rate, and response time distribution.
        Equivalent to the 'Requests' blade in the App Insights portal.

        Args:
            since_minutes: Look-back window in minutes.
            top_n: Maximum rows to return.
            min_duration_ms: Optional minimum duration filter in milliseconds.
                Use to focus on slow requests.
            failed_only: If True, return only failed requests (success==False).

        Returns:
            Parsed App Insights query response tables.
        """
        filters = ["success == false" if failed_only else ""]
        if min_duration_ms is not None:
            filters.append(f"duration >= {min_duration_ms}")
        where_clause = " | where ".join(f for f in filters if f)
        if where_clause:
            where_clause = f"| where {where_clause}"

        kql = f"""requests
| where timestamp >= ago({since_minutes}m)
{where_clause}
| project timestamp, name, url, resultCode, duration, success,
          operation_Id, cloud_RoleName, client_City
| order by timestamp desc
| take {top_n}"""

        timespan = f"PT{since_minutes}M" if since_minutes < 60 else f"PT{since_minutes // 60}H"
        return await self.query(kql, timespan=timespan)

    async def get_exceptions(
        self,
        since_minutes: int = 60,
        top_n: int = 100,
    ) -> dict[str, Any]:
        """Get exception telemetry — unhandled errors tracked by the SDK.

        Returns exception type, message, and occurrence count.
        Equivalent to the 'Failures' blade > Exceptions in the portal.

        Args:
            since_minutes: Look-back window in minutes.
            top_n: Maximum rows to return.

        Returns:
            Parsed App Insights query response tables.
        """
        kql = f"""exceptions
| where timestamp >= ago({since_minutes}m)
| project timestamp, type, outerMessage, problemId,
          operation_Id, cloud_RoleName, severityLevel
| order by timestamp desc
| take {top_n}"""

        timespan = f"PT{since_minutes}M" if since_minutes < 60 else f"PT{since_minutes // 60}H"
        return await self.query(kql, timespan=timespan)

    async def get_dependencies(
        self,
        since_minutes: int = 60,
        top_n: int = 100,
        failed_only: bool = False,
    ) -> dict[str, Any]:
        """Get dependency call telemetry — outgoing HTTP, DB, queue calls.

        Shows which downstream services are slow or failing.
        Equivalent to the 'Dependencies' section in the portal.

        Args:
            since_minutes: Look-back window in minutes.
            top_n: Maximum rows to return.
            failed_only: Return only failed dependency calls.

        Returns:
            Parsed App Insights query response tables.
        """
        where_failed = "| where success == false" if failed_only else ""
        kql = f"""dependencies
| where timestamp >= ago({since_minutes}m)
{where_failed}
| project timestamp, type, target, name, duration, success,
          resultCode, operation_Id, cloud_RoleName
| order by timestamp desc
| take {top_n}"""

        timespan = f"PT{since_minutes}M" if since_minutes < 60 else f"PT{since_minutes // 60}H"
        return await self.query(kql, timespan=timespan)

    async def get_availability(
        self,
        since_minutes: int = 60,
        top_n: int = 100,
    ) -> dict[str, Any]:
        """Get availability test results — uptime check outcomes.

        Shows whether configured availability/ping tests are passing.

        Args:
            since_minutes: Look-back window in minutes.
            top_n: Maximum rows to return.

        Returns:
            Parsed App Insights query response tables.
        """
        kql = f"""availabilityResults
| where timestamp >= ago({since_minutes}m)
| project timestamp, name, success, duration, location,
          message, operation_Id
| order by timestamp desc
| take {top_n}"""

        timespan = f"PT{since_minutes}M" if since_minutes < 60 else f"PT{since_minutes // 60}H"
        return await self.query(kql, timespan=timespan)

    async def validate(self) -> dict[str, Any]:
        """Validate credentials by running a minimal query.

        Returns:
            Parsed response dict — a single-row result confirms connectivity.

        Raises:
            PermissionError: On auth failure.
            httpx.HTTPStatusError: On HTTP errors.
        """
        return await self.query("requests | take 1 | project timestamp", timespan="PT1H")

    async def _get(
        self,
        url: str,
        params: dict[str, Any],
        headers: dict[str, str],
    ) -> dict[str, Any]:
        """Execute a GET request with retry logic.

        Args:
            url: Full URL to request.
            params: Query parameters.
            headers: Request headers (including auth).

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
