"""
Async Prometheus HTTP client for Sherlock.

Queries the Prometheus HTTP API v1 for instant and range queries.
Supports Bearer token auth, Basic auth, or no auth.
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

logger = logging.getLogger("sherlock.client.prometheus")

PROMETHEUS_API_V1 = "/api/v1"


class _RetryableHTTPError(Exception):
    pass


class PrometheusClient:
    """Async HTTP client for the Prometheus HTTP API v1.

    All requests are GET-only (read-only enforcement).
    Supports Bearer token and Basic auth.
    """

    def __init__(
        self,
        base_url: str,
        token: str | None = None,
        username: str | None = None,
        password: str | None = None,
        timeout: int = 30,
    ) -> None:
        """Initialize the Prometheus client.

        Args:
            base_url: Base URL of the Prometheus server (e.g. 'http://prometheus:9090').
            token: Optional Bearer token for authentication.
            username: Optional Basic auth username.
            password: Optional Basic auth password (required if username is set).
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

    async def query(
        self,
        promql: str,
        time_ts: float | None = None,
    ) -> dict[str, Any]:
        """Execute an instant PromQL query.

        Args:
            promql: PromQL expression to evaluate.
            time_ts: Optional evaluation timestamp (Unix seconds). Defaults to now.

        Returns:
            Parsed Prometheus API response dict with 'status' and 'data' fields.
        """
        params: dict[str, Any] = {"query": promql}
        if time_ts is not None:
            params["time"] = time_ts
        return await self._get(f"{self.base_url}{PROMETHEUS_API_V1}/query", params)

    async def query_range(
        self,
        promql: str,
        start_ts: float,
        end_ts: float,
        step: str = "60s",
    ) -> dict[str, Any]:
        """Execute a range PromQL query returning a timeseries.

        Args:
            promql: PromQL expression to evaluate.
            start_ts: Start timestamp (Unix seconds).
            end_ts: End timestamp (Unix seconds).
            step: Query step resolution (e.g. '60s', '5m').

        Returns:
            Parsed Prometheus API response dict with 'status' and 'data' fields.
        """
        params: dict[str, Any] = {
            "query": promql,
            "start": start_ts,
            "end": end_ts,
            "step": step,
        }
        return await self._get(
            f"{self.base_url}{PROMETHEUS_API_V1}/query_range", params
        )

    async def health(self) -> bool:
        """Check if the Prometheus server is reachable via /-/healthy.

        Returns:
            True if healthy (HTTP 200), False otherwise.
        """
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(
                    f"{self.base_url}/-/healthy",
                    headers=self._headers,
                    auth=self._auth,
                )
            return resp.status_code == 200
        except Exception as exc:
            logger.debug("Prometheus health check failed: %s", exc)
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
