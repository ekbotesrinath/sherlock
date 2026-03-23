"""
Async Datadog Metrics API client for Sherlock.

Queries the Datadog Metrics Query API v1 and Events API.
API keys are never logged or serialized.
Read-only: only GET requests are issued.
"""

import logging
from typing import Any

import httpx
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

logger = logging.getLogger("sherlock.client.datadog")

# Datadog site base URLs keyed by site identifier.
DATADOG_SITES: dict[str, str] = {
    "US1": "https://api.datadoghq.com",
    "US3": "https://us3.datadoghq.com",
    "US5": "https://us5.datadoghq.com",
    "EU1": "https://api.datadoghq.eu",
    "AP1": "https://ap1.datadoghq.com",
}


class _RetryableHTTPError(Exception):
    pass


class DatadogClient:
    """Async HTTP client for the Datadog Metrics and Events API v1.

    All requests are GET-only (read-only enforcement).
    API keys are stored in memory and never written to disk or logged.
    """

    def __init__(
        self,
        api_key: str,
        app_key: str,
        site: str = "US1",
        timeout: int = 30,
    ) -> None:
        """Initialize the Datadog client.

        Args:
            api_key: Datadog API key (never logged).
            app_key: Datadog Application key (never logged).
            site: Datadog site identifier (US1, US3, US5, EU1, AP1).
            timeout: Default request timeout in seconds.
        """
        self.site = site.upper()
        self._timeout = timeout
        self._base_url = DATADOG_SITES.get(self.site, DATADOG_SITES["US1"])
        # Keys stored only in memory, never serialized.
        self._headers = {
            "DD-API-KEY": api_key,
            "DD-APPLICATION-KEY": app_key,
            "Accept": "application/json",
        }

    async def query_metrics(
        self,
        query: str,
        from_ts: int,
        to_ts: int,
    ) -> dict[str, Any]:
        """Query Datadog metrics using the v1 metrics query endpoint.

        Args:
            query: Datadog metrics query string (e.g. 'avg:system.cpu.user{*}').
            from_ts: Start of the queried time period (Unix seconds).
            to_ts: End of the queried time period (Unix seconds).

        Returns:
            Parsed Datadog API response dict with 'series' list.
        """
        params = {"query": query, "from": from_ts, "to": to_ts}
        return await self._get(f"{self._base_url}/api/v1/query", params)

    async def get_events(
        self,
        query: str,
        from_ts: int,
        to_ts: int,
        limit: int = 50,
    ) -> dict[str, Any]:
        """Query Datadog events.

        Args:
            query: Event search query string.
            from_ts: Start timestamp (Unix seconds).
            to_ts: End timestamp (Unix seconds).
            limit: Maximum number of events to return.

        Returns:
            Parsed Datadog API response dict with 'events' list.
        """
        params = {
            "query": query,
            "start": from_ts,
            "end": to_ts,
            "priority": "normal",
            "count": limit,
        }
        return await self._get(f"{self._base_url}/api/v1/events", params)

    async def get_monitors(
        self,
        name_filter: str | None = None,
        tags: list[str] | None = None,
        limit: int = 100,
    ) -> dict[str, Any]:
        """List all Datadog monitors with their current state.

        Returns monitor definitions and current status (OK, Alert, Warn, No Data).
        Use this to see what alert conditions are configured and their health.

        Args:
            name_filter: Optional substring to filter monitor names.
            tags: Optional list of tags to filter by (e.g. ['env:prod', 'service:api']).
            limit: Maximum number of monitors to return (max 1000).

        Returns:
            List of monitor objects. Each includes: id, name, type, query,
            message, tags, overall_state (OK/Alert/Warn/No Data/Ignored/Skipped),
            priority, created, modified, and creator.
        """
        params: dict[str, Any] = {
            "with_downtimes": "true",
            "count": min(limit, 1000),
            "start": 0,
        }
        if name_filter:
            params["name"] = name_filter
        if tags:
            params["monitor_tags"] = ",".join(tags)
        result = await self._get(f"{self._base_url}/api/v1/monitor", params)
        # v1/monitor returns a list directly, not a dict with 'monitors' key.
        if isinstance(result, list):
            return {"monitors": result, "total": len(result)}
        return result

    async def get_triggered_monitors(
        self,
        tags: list[str] | None = None,
        group_states: list[str] | None = None,
    ) -> dict[str, Any]:
        """List all Datadog monitors currently in a triggered (Alert/Warn) state.

        This is the equivalent of 'open incidents' — monitors that are actively
        firing right now. Triggered means overall_state is Alert or Warn.

        Args:
            tags: Optional scope tags (e.g. ['env:prod']).
            group_states: Optional group states to include — Alert, Warn, No Data.
                Defaults to ['Alert', 'Warn'] if not specified.

        Returns:
            Dict with 'monitors' list of triggered monitor objects including
            their current group states and triggered timestamps.
        """
        states = group_states or ["Alert", "Warn"]
        params: dict[str, Any] = {
            "group_states": ",".join(states),
        }
        if tags:
            params["monitor_tags"] = ",".join(tags)
        result = await self._get(
            f"{self._base_url}/api/v1/monitor",
            {**params, "with_downtimes": "true", "count": 1000},
        )
        # Filter to only triggered monitors.
        if isinstance(result, list):
            monitors: list[dict[str, Any]] = result
        else:
            monitors = result.get("monitors", [])  # type: ignore[assignment]
        triggered = [
            m for m in monitors
            if m.get("overall_state") in ("Alert", "Warn", "No Data")  # type: ignore[union-attr]
        ]
        return {"monitors": triggered, "total": len(triggered)}

    async def validate(self) -> dict[str, Any]:
        """Validate credentials by calling the /api/v1/validate endpoint.

        Returns:
            Parsed response dict — contains 'valid': True on success.
        """
        return await self._get(f"{self._base_url}/api/v1/validate", {})

    async def _get(self, url: str, params: dict[str, Any]) -> dict[str, Any]:
        """Execute a GET request with retry logic.

        Args:
            url: Full URL to request.
            params: Query parameters.

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
                resp = await client.get(url, params=params, headers=self._headers)
                if resp.status_code == 429:
                    raise _RetryableHTTPError("429 Rate Limited")
                if resp.status_code in (500, 502, 503, 504):
                    raise _RetryableHTTPError(f"HTTP {resp.status_code}")
                resp.raise_for_status()
                return resp.json()

        return await _do()
