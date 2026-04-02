"""
Async Grafana HTTP client for Sherlock.

Queries the Grafana HTTP API for alerting data.
Supports Bearer token auth, Basic auth, API key auth, or no auth.
Read-only: only GET requests are issued.

Grafana API reference:
  https://grafana.com/docs/grafana/latest/developers/http_api/
  https://grafana.com/docs/grafana/latest/developers/http_api/alerting/

Supported alert systems:
  - Grafana Alerting v2 (Grafana 8+): /api/alertmanager/grafana/api/v2/alerts
  - Legacy alerting (Grafana <=7):    /api/alerts
  - Unified alert rules:              /api/v1/provisioning/alert-rules
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

logger = logging.getLogger("sherlock.client.grafana")

GRAFANA_ALERTING_V2 = "/api/alertmanager/grafana/api/v2/alerts"
GRAFANA_ALERT_RULES = "/api/v1/provisioning/alert-rules"
GRAFANA_LEGACY_ALERTS = "/api/alerts"
GRAFANA_HEALTH = "/api/health"
GRAFANA_DATASOURCES = "/api/datasources"
GRAFANA_FOLDERS = "/api/folders"


class _RetryableHTTPError(Exception):
    pass


class GrafanaClient:
    """Async HTTP client for the Grafana HTTP API.

    All requests are GET-only (read-only enforcement).
    Supports Bearer token, Basic auth, and X-API-Key header auth.
    """

    def __init__(
        self,
        base_url: str,
        token: str | None = None,
        api_key: str | None = None,
        username: str | None = None,
        password: str | None = None,
        org_id: int | None = None,
        timeout: int = 30,
    ) -> None:
        """Initialise the Grafana client.

        Args:
            base_url: Base URL of the Grafana server (e.g. 'http://grafana:3000').
            token: Optional service account token or personal API token
                (sent as 'Authorization: Bearer <token>').
            api_key: Optional Grafana API key (legacy — sent as 'Authorization: Bearer <key>').
                Functionally equivalent to 'token'; use one or the other.
            username: Optional Basic auth username (default admin/admin for local installs).
            password: Optional Basic auth password.
            org_id: Optional Grafana organization ID sent as 'X-Grafana-Org-Id' header.
                Required only for multi-org Grafana instances.
            timeout: Default request timeout in seconds.
        """
        self.base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._headers: dict[str, str] = {
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        self._auth: tuple[str, str] | None = None

        bearer = token or api_key
        if bearer:
            self._headers["Authorization"] = f"Bearer {bearer}"
        elif username and password:
            self._auth = (username, password)

        if org_id is not None:
            self._headers["X-Grafana-Org-Id"] = str(org_id)

    # ── Internal HTTP helpers ────────────────────────────────────────────

    @retry(
        retry=retry_if_exception_type(_RetryableHTTPError),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=8),
    )
    async def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        """Execute a GET request and return the parsed JSON body.

        Args:
            path: API path (e.g. '/api/health').
            params: Optional query parameters.

        Returns:
            Parsed JSON response (dict or list).

        Raises:
            _RetryableHTTPError: For 429 or 5xx responses (triggers retry).
            httpx.HTTPStatusError: For 4xx (non-429) client errors.
        """
        url = f"{self.base_url}{path}"
        async with httpx.AsyncClient(
            timeout=self._timeout,
            headers=self._headers,
            auth=self._auth,
            follow_redirects=True,
        ) as http:
            resp = await http.get(url, params=params)
            if resp.status_code == 429 or resp.status_code >= 500:
                raise _RetryableHTTPError(f"HTTP {resp.status_code} from {url}")
            resp.raise_for_status()
            return resp.json()

    # ── Public API ───────────────────────────────────────────────────────

    async def health(self) -> dict[str, Any]:
        """Check Grafana health.

        Returns:
            Dict with 'commit', 'database', 'version' keys.
            'database' == 'ok' when Grafana is healthy.
        """
        return await self._get(GRAFANA_HEALTH)

    async def get_datasources(self) -> list[dict[str, Any]]:
        """List all configured data sources.

        Returns:
            List of datasource objects with 'name', 'type', 'url', 'id' fields.
            Common types: 'prometheus', 'loki', 'elasticsearch', 'tempo'.
        """
        result = await self._get(GRAFANA_DATASOURCES)
        return result if isinstance(result, list) else []

    async def get_firing_alerts(
        self,
        active: bool = True,
        silenced: bool = False,
        inhibited: bool = False,
        filter_labels: dict[str, str] | None = None,
    ) -> list[dict[str, Any]]:
        """Get currently firing alerts from Grafana Alertmanager (v2 API).

        This uses the unified alerting API available in Grafana 8+.

        Args:
            active: Include active (firing) alerts. Default True.
            silenced: Include silenced alerts. Default False.
            inhibited: Include inhibited alerts. Default False.
            filter_labels: Optional dict of label filters (exact match).
                e.g. {'severity': 'critical', 'service': 'api'}

        Returns:
            List of alert objects. Each has:
                'labels'      — dict of label key/value pairs (name, severity, etc.)
                'annotations' — dict with 'summary', 'description', 'runbook_url'
                'startsAt'    — ISO8601 timestamp when alert fired
                'endsAt'      — ISO8601 timestamp (for resolved alerts)
                'status'      — dict with 'state': 'active'|'suppressed'|'unprocessed'
                'generatorURL' — link to the alert rule in Grafana
                'fingerprint' — unique ID for this alert instance
        """
        params: dict[str, Any] = {
            "active": str(active).lower(),
            "silenced": str(silenced).lower(),
            "inhibited": str(inhibited).lower(),
        }
        if filter_labels:
            # Alertmanager v2 accepts repeated 'filter' params as label matchers
            params["filter"] = [f'{k}="{v}"' for k, v in filter_labels.items()]
        result = await self._get(GRAFANA_ALERTING_V2, params=params)
        return result if isinstance(result, list) else []

    async def get_alert_rules(self) -> list[dict[str, Any]]:
        """List all Grafana alert rule definitions (not instances).

        Uses the provisioning API (Grafana 9+).

        Returns:
            List of alert rule objects. Each has:
                'uid'          — unique rule ID
                'title'        — rule display name
                'condition'    — condition expression ref
                'data'         — list of query+expression objects
                'noDataState'  — behaviour when no data ('NoData', 'OK', 'Alerting')
                'execErrState' — behaviour on error
                'for'          — pending period (e.g. '5m')
                'labels'       — dict of labels (severity, team, etc.)
                'annotations'  — dict (summary, description, runbook_url)
                'folderUID'    — folder containing the rule
                'ruleGroup'    — rule group name
        """
        result = await self._get(GRAFANA_ALERT_RULES)
        if isinstance(result, dict):
            # Provisioning API returns {"groups": [...]} or flat list depending on version
            return result.get("groups", [result]) if "groups" in result else [result]
        return result if isinstance(result, list) else []

    async def get_legacy_alerts(
        self,
        state: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Get alerts from the legacy Grafana alerting API (Grafana <=7).

        Falls back automatically if v2 API is not available.

        Args:
            state: Optional filter — 'alerting', 'ok', 'no_data', 'paused',
                'pending'. If None, returns all states.
            limit: Maximum number of alerts to return.

        Returns:
            List of alert objects with 'id', 'dashboardId', 'name', 'state', 'url'.
        """
        params: dict[str, Any] = {"limit": limit}
        if state:
            params["state"] = state
        result = await self._get(GRAFANA_LEGACY_ALERTS, params=params)
        return result if isinstance(result, list) else []
