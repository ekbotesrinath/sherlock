"""
Async Grafana Loki HTTP client for Sherlock.

Queries the Loki HTTP API v1 for log range queries and instant label searches.
Supports Bearer token auth, Basic auth, or no auth.
Read-only: only GET requests are issued.

Loki API reference: https://grafana.com/docs/loki/latest/reference/loki-http-api/
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

logger = logging.getLogger("sherlock.client.loki")

LOKI_API_V1 = "/loki/api/v1"


class _RetryableHTTPError(Exception):
    pass


class LokiClient:
    """Async HTTP client for the Grafana Loki API v1.

    All requests are GET-only (read-only enforcement).
    Supports Bearer token and Basic auth.
    """

    def __init__(
        self,
        base_url: str,
        token: str | None = None,
        username: str | None = None,
        password: str | None = None,
        org_id: str | None = None,
        timeout: int = 30,
    ) -> None:
        """Initialize the Loki client.

        Args:
            base_url: Base URL of the Loki server
                (e.g. 'http://loki:3100' or 'https://logs-prod-us-central1.grafana.net').
            token: Optional Bearer token. For Grafana Cloud use a Grafana API key
                or service account token.
            username: Optional Basic auth username. For Grafana Cloud this is the
                numeric tenant ID shown in the Loki connection details.
            password: Optional Basic auth password (API key for Grafana Cloud).
            org_id: Optional Loki tenant/organisation ID sent as 'X-Scope-OrgID'
                header. Required for multi-tenant Loki deployments.
            timeout: Default request timeout in seconds.
        """
        self.base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._headers: dict[str, str] = {"Accept": "application/json"}
        self._auth: tuple[str, str] | None = None

        if token:
            self._headers["Authorization"] = f"Bearer {token}"
        elif username and password:
            self._auth = (username, password)

        if org_id:
            self._headers["X-Scope-OrgID"] = org_id

    async def query_range(
        self,
        logql: str,
        start_ts: float,
        end_ts: float,
        limit: int = 100,
        direction: str = "backward",
    ) -> dict[str, Any]:
        """Execute a LogQL range query returning log lines or metric values.

        Args:
            logql: LogQL expression
                (e.g. '{service="api"} |= "error"' or
                 'rate({job="nginx"}[5m])').
            start_ts: Start of the time range (Unix nanoseconds or seconds —
                Loki accepts both; seconds are auto-detected).
            end_ts: End of the time range (same unit as start_ts).
            limit: Maximum number of log lines to return per stream.
                Ignored for metric queries. Defaults to 100.
            direction: Log ordering — 'backward' (newest first, default) or
                'forward' (oldest first).

        Returns:
            Parsed Loki API response dict with 'status' and 'data' fields.
            data.resultType is 'streams' for log queries or 'matrix'/'vector'
            for metric queries.
        """
        # Loki wants nanosecond timestamps; convert if seconds-range values passed.
        start = _maybe_to_ns(start_ts)
        end = _maybe_to_ns(end_ts)
        params: dict[str, Any] = {
            "query": logql,
            "start": start,
            "end": end,
            "limit": limit,
            "direction": direction,
        }
        return await self._get(
            f"{self.base_url}{LOKI_API_V1}/query_range", params
        )

    async def query(
        self,
        logql: str,
        time_ts: float | None = None,
        limit: int = 100,
        direction: str = "backward",
    ) -> dict[str, Any]:
        """Execute an instant LogQL query.

        Args:
            logql: LogQL expression.
            time_ts: Optional evaluation timestamp (Unix seconds or ns).
                Defaults to now.
            limit: Maximum log lines to return.
            direction: 'backward' (newest first) or 'forward'.

        Returns:
            Parsed Loki API response dict.
        """
        params: dict[str, Any] = {"query": logql, "limit": limit, "direction": direction}
        if time_ts is not None:
            params["time"] = _maybe_to_ns(time_ts)
        return await self._get(f"{self.base_url}{LOKI_API_V1}/query", params)

    async def labels(self) -> list[str]:
        """Return all label names known to Loki.

        Useful for building query filters without guessing label names.

        Returns:
            List of label name strings.
        """
        result = await self._get(f"{self.base_url}{LOKI_API_V1}/labels", {})
        return result.get("data", [])

    async def label_values(self, label: str) -> list[str]:
        """Return all values for a given label name.

        Args:
            label: Label name (e.g. 'service', 'job', 'namespace').

        Returns:
            List of value strings.
        """
        result = await self._get(
            f"{self.base_url}{LOKI_API_V1}/label/{label}/values", {}
        )
        return result.get("data", [])

    async def health(self) -> bool:
        """Check if the Loki server is reachable via /ready.

        Returns:
            True if ready (HTTP 200), False otherwise.
        """
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(
                    f"{self.base_url}/ready",
                    headers=self._headers,
                    auth=self._auth,
                )
            return resp.status_code == 200
        except Exception as exc:
            logger.debug("Loki health check failed: %s", exc)
            return False

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
                resp = await client.get(
                    url,
                    params=params,
                    headers=self._headers,
                    auth=self._auth,
                )
                if resp.status_code == 429:
                    raise _RetryableHTTPError("429 Rate Limited")
                if resp.status_code in (500, 502, 503, 504):
                    raise _RetryableHTTPError(f"HTTP {resp.status_code}")
                resp.raise_for_status()
                return resp.json()

        return await _do()


def _maybe_to_ns(ts: float) -> int:
    """Convert a Unix timestamp to nanoseconds.

    Loki requires nanosecond-precision timestamps. Detects whether the input
    is already in nanoseconds (value > 1e15) or seconds and converts accordingly.

    Args:
        ts: Unix timestamp in either seconds or nanoseconds.

    Returns:
        Unix timestamp in nanoseconds.
    """
    if ts < 1e15:
        # Seconds — multiply to nanoseconds.
        return int(ts * 1_000_000_000)
    return int(ts)
