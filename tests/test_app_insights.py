"""
Tests for App Insights client and tools.

Covers:
  client/app_insights.py  — AppInsightsClient (HTTP, auth, KQL methods)
  tools/app_insights_tool.py — MCP tool wrappers
"""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
import respx

from client.app_insights import AppInsightsClient, APP_INSIGHTS_API_BASE
from core.datasource_context import DataSourceContext
from tools.app_insights_tool import (
    connect_app_insights,
    get_app_insights_availability,
    get_app_insights_dependencies,
    get_app_insights_failures,
    query_app_insights,
)


# ── Fixtures ──────────────────────────────────────────────────────────────


APP_ID = "00000000-0000-0000-0000-000000000001"
API_KEY = "test-api-key-abc123"

# Minimal valid App Insights query response (column/row format).
FAKE_TABLE_RESPONSE = {
    "tables": [
        {
            "name": "PrimaryResult",
            "columns": [
                {"name": "name", "type": "string"},
                {"name": "count", "type": "long"},
                {"name": "avg_duration_ms", "type": "real"},
            ],
            "rows": [
                ["POST /checkout", 42, 123.4],
                ["GET /health", 5, 8.1],
            ],
        }
    ]
}

EMPTY_TABLE_RESPONSE = {"tables": [{"name": "PrimaryResult", "columns": [], "rows": []}]}


@pytest.fixture(autouse=True)
def reset_datasource_context():
    """Clear the App Insights slot before and after each test."""
    ctx = DataSourceContext()
    ctx.clear_app_insights()
    yield
    ctx.clear_app_insights()


@pytest.fixture
def mock_client() -> AppInsightsClient:
    """Return a wired-up AppInsightsClient with all async methods mocked."""
    client = AppInsightsClient(app_id=APP_ID, api_key=API_KEY)
    client.validate = AsyncMock(return_value=None)
    client.query = AsyncMock(return_value=FAKE_TABLE_RESPONSE)
    return client


@pytest.fixture
def connected_client(mock_client) -> AppInsightsClient:
    """Store the mock client in DataSourceContext and return it."""
    DataSourceContext().set_app_insights(mock_client)
    return mock_client


# ── AppInsightsClient unit tests ──────────────────────────────────────────


class TestAppInsightsClientHeaders:
    """Auth header selection logic."""

    @pytest.mark.asyncio
    async def test_api_key_header(self):
        client = AppInsightsClient(app_id=APP_ID, api_key=API_KEY)
        headers = await client._get_headers()
        assert headers["X-API-Key"] == API_KEY
        assert "Authorization" not in headers

    @pytest.mark.asyncio
    async def test_no_auth_raises_on_get_headers(self):
        """Without api_key or AAD creds, _get_headers raises ValueError."""
        client = AppInsightsClient(app_id=APP_ID)
        with pytest.raises(ValueError):
            await client._get_headers()


class TestAppInsightsClientQuery:
    """HTTP query execution via respx."""

    @respx.mock
    @pytest.mark.asyncio
    async def test_query_returns_tables(self):
        """query() calls the correct URL and returns the parsed JSON."""
        url = f"{APP_INSIGHTS_API_BASE}/{APP_ID}/query"
        respx.get(url).mock(
            return_value=httpx.Response(200, json=FAKE_TABLE_RESPONSE)
        )

        client = AppInsightsClient(app_id=APP_ID, api_key=API_KEY)
        result = await client.query("requests | take 2", timespan="PT1H")

        assert "tables" in result
        assert result["tables"][0]["rows"][0][0] == "POST /checkout"

    @respx.mock
    @pytest.mark.asyncio
    async def test_query_400_raises(self):
        """query() raises on 400 Bad Request (bad KQL)."""
        url = f"{APP_INSIGHTS_API_BASE}/{APP_ID}/query"
        respx.get(url).mock(
            return_value=httpx.Response(400, json={"error": {"message": "Bad KQL"}})
        )

        client = AppInsightsClient(app_id=APP_ID, api_key=API_KEY)
        with pytest.raises(Exception):
            await client.query("INVALID KQL |||", timespan="PT1H")

    @respx.mock
    @pytest.mark.asyncio
    async def test_validate_calls_query(self):
        """validate() executes a minimal KQL probe."""
        url = f"{APP_INSIGHTS_API_BASE}/{APP_ID}/query"
        respx.get(url).mock(
            return_value=httpx.Response(200, json=EMPTY_TABLE_RESPONSE)
        )

        client = AppInsightsClient(app_id=APP_ID, api_key=API_KEY)
        await client.validate()  # Should not raise.


# ── Tool: connect_app_insights ────────────────────────────────────────────


class TestConnectAppInsights:
    """Tests for connect_app_insights tool."""

    @pytest.mark.asyncio
    async def test_connect_with_api_key(self):
        """Successful API key connection stores the client and returns 'connected'."""
        mock = AppInsightsClient.__new__(AppInsightsClient)
        mock.app_id = APP_ID
        mock.validate = AsyncMock(return_value=None)

        with patch("tools.app_insights_tool.AppInsightsClient", return_value=mock):
            result = await connect_app_insights(app_id=APP_ID, api_key=API_KEY)

        data = json.loads(result)
        assert data["status"] == "connected"
        assert data["auth"] == "api_key"
        assert DataSourceContext().get_app_insights() is mock

    @pytest.mark.asyncio
    async def test_connect_with_service_principal(self):
        """Successful AAD service principal connection reports auth='service_principal'."""
        mock = AppInsightsClient.__new__(AppInsightsClient)
        mock.app_id = APP_ID
        mock.validate = AsyncMock(return_value=None)

        with patch("tools.app_insights_tool.AppInsightsClient", return_value=mock):
            result = await connect_app_insights(
                app_id=APP_ID,
                tenant_id="tenant-1",
                client_id="client-1",
                client_secret="secret-1",
            )

        data = json.loads(result)
        assert data["status"] == "connected"
        assert data["auth"] == "service_principal"

    @pytest.mark.asyncio
    async def test_missing_app_id_returns_error(self):
        result = await connect_app_insights(app_id="")
        data = json.loads(result)
        assert "error" in data
        assert data["data_available"] is False

    @pytest.mark.asyncio
    async def test_missing_credentials_returns_error(self):
        result = await connect_app_insights(app_id=APP_ID)
        data = json.loads(result)
        assert "error" in data
        assert data["data_available"] is False

    @pytest.mark.asyncio
    async def test_validate_failure_returns_error(self):
        """If validate() raises, connect returns an error response instead of crashing."""
        mock = AppInsightsClient.__new__(AppInsightsClient)
        mock.app_id = APP_ID
        mock.validate = AsyncMock(side_effect=Exception("401 Unauthorized"))

        with patch("tools.app_insights_tool.AppInsightsClient", return_value=mock):
            result = await connect_app_insights(app_id=APP_ID, api_key="bad-key")

        data = json.loads(result)
        assert "error" in data
        assert "401" in data["error"]
        assert data["data_available"] is False


# ── Tool: query_app_insights ──────────────────────────────────────────────


class TestQueryAppInsights:
    """Tests for query_app_insights tool."""

    @pytest.mark.asyncio
    async def test_not_connected_returns_error(self):
        result = await query_app_insights(kql="requests | take 5")
        data = json.loads(result)
        assert "error" in data
        assert data["data_available"] is False

    @pytest.mark.asyncio
    async def test_empty_kql_returns_error(self, connected_client):
        result = await query_app_insights(kql="")
        data = json.loads(result)
        assert "error" in data
        assert data["data_available"] is False

    @pytest.mark.asyncio
    async def test_kql_too_long_returns_error(self, connected_client):
        result = await query_app_insights(kql="x" * 4001)
        data = json.loads(result)
        assert "error" in data
        assert data["data_available"] is False

    @pytest.mark.asyncio
    async def test_returns_flattened_rows(self, connected_client):
        result = await query_app_insights(kql="requests | take 2", since_minutes=30)
        data = json.loads(result)
        assert data["data_available"] is True
        assert data["row_count"] == 2
        assert data["rows"][0]["name"] == "POST /checkout"
        assert data["rows"][0]["count"] == 42

    @pytest.mark.asyncio
    async def test_client_exception_returns_error(self, connected_client):
        connected_client.query = AsyncMock(side_effect=Exception("timeout"))
        result = await query_app_insights(kql="requests | take 1")
        data = json.loads(result)
        assert "error" in data
        assert data["data_available"] is False


# ── Tool: get_app_insights_failures ──────────────────────────────────────


class TestGetAppInsightsFailures:
    """Tests for get_app_insights_failures tool."""

    @pytest.mark.asyncio
    async def test_not_connected_returns_error(self):
        result = await get_app_insights_failures()
        data = json.loads(result)
        assert "error" in data
        assert data["data_available"] is False

    @pytest.mark.asyncio
    async def test_returns_failed_requests(self, connected_client):
        # First call → failed requests; second → exceptions
        connected_client.query = AsyncMock(side_effect=[
            {
                "tables": [{
                    "name": "PrimaryResult",
                    "columns": [
                        {"name": "name", "type": "string"},
                        {"name": "resultCode", "type": "string"},
                        {"name": "count", "type": "long"},
                    ],
                    "rows": [["POST /payment", "500", 18]],
                }]
            },
            {
                "tables": [{
                    "name": "PrimaryResult",
                    "columns": [
                        {"name": "type", "type": "string"},
                        {"name": "outerMessage", "type": "string"},
                        {"name": "count", "type": "long"},
                    ],
                    "rows": [["NullReferenceException", "Object ref not set", 7]],
                }]
            },
        ])

        result = await get_app_insights_failures(since_minutes=60)
        data = json.loads(result)
        assert data["data_available"] is True
        assert data["failed_request_count"] == 1
        assert data["request_summary"][0]["name"] == "POST /payment"
        assert data["exception_count"] == 1
        assert data["exception_summary"][0]["type"] == "NullReferenceException"

    @pytest.mark.asyncio
    async def test_include_exceptions_false(self, connected_client):
        """When include_exceptions=False only one query is made."""
        connected_client.query = AsyncMock(return_value=EMPTY_TABLE_RESPONSE)
        result = await get_app_insights_failures(include_exceptions=False)
        data = json.loads(result)
        assert data["data_available"] is True
        assert "exception_summary" not in data
        connected_client.query.assert_called_once()

    @pytest.mark.asyncio
    async def test_client_exception_returns_error(self, connected_client):
        connected_client.query = AsyncMock(side_effect=Exception("KQL error"))
        result = await get_app_insights_failures()
        data = json.loads(result)
        assert "error" in data
        assert data["data_available"] is False


# ── Tool: get_app_insights_dependencies ──────────────────────────────────


class TestGetAppInsightsDependencies:
    """Tests for get_app_insights_dependencies tool."""

    @pytest.mark.asyncio
    async def test_not_connected_returns_error(self):
        result = await get_app_insights_dependencies()
        data = json.loads(result)
        assert "error" in data
        assert data["data_available"] is False

    @pytest.mark.asyncio
    async def test_returns_failed_and_slow(self, connected_client):
        dep_row = {
            "tables": [{
                "name": "PrimaryResult",
                "columns": [
                    {"name": "type", "type": "string"},
                    {"name": "target", "type": "string"},
                    {"name": "count", "type": "long"},
                    {"name": "avg_duration_ms", "type": "real"},
                ],
                "rows": [["SQL", "postgres:5432", 3, 4500.0]],
            }]
        }
        connected_client.query = AsyncMock(side_effect=[dep_row, dep_row])

        result = await get_app_insights_dependencies(since_minutes=60)
        data = json.loads(result)
        assert data["data_available"] is True
        assert data["failed_dependency_count"] == 1
        assert data["slow_dependency_count"] == 1
        assert data["failed_dependency_summary"][0]["target"] == "postgres:5432"

    @pytest.mark.asyncio
    async def test_failed_only_skips_slow_query(self, connected_client):
        """failed_only=True should only issue one query."""
        connected_client.query = AsyncMock(return_value=EMPTY_TABLE_RESPONSE)
        result = await get_app_insights_dependencies(failed_only=True)
        data = json.loads(result)
        assert data["data_available"] is True
        assert "slow_dependency_summary" not in data
        connected_client.query.assert_called_once()

    @pytest.mark.asyncio
    async def test_client_exception_returns_error(self, connected_client):
        connected_client.query = AsyncMock(side_effect=Exception("network error"))
        result = await get_app_insights_dependencies()
        data = json.loads(result)
        assert "error" in data
        assert data["data_available"] is False


# ── Tool: get_app_insights_availability ──────────────────────────────────


class TestGetAppInsightsAvailability:
    """Tests for get_app_insights_availability tool."""

    @pytest.mark.asyncio
    async def test_not_connected_returns_error(self):
        result = await get_app_insights_availability()
        data = json.loads(result)
        assert "error" in data
        assert data["data_available"] is False

    @pytest.mark.asyncio
    async def test_returns_availability_summary(self, connected_client):
        summary_response = {
            "tables": [{
                "name": "PrimaryResult",
                "columns": [
                    {"name": "name", "type": "string"},
                    {"name": "pass_count", "type": "long"},
                    {"name": "fail_count", "type": "long"},
                    {"name": "total", "type": "long"},
                    {"name": "avg_duration_ms", "type": "real"},
                    {"name": "availability_pct", "type": "real"},
                ],
                "rows": [["Homepage ping", 58, 2, 60, 123.0, 96.67]],
            }]
        }
        failures_response = {
            "tables": [{
                "name": "PrimaryResult",
                "columns": [
                    {"name": "timestamp", "type": "datetime"},
                    {"name": "name", "type": "string"},
                    {"name": "location", "type": "string"},
                    {"name": "duration", "type": "real"},
                    {"name": "message", "type": "string"},
                    {"name": "operation_Id", "type": "string"},
                ],
                "rows": [
                    ["2026-03-23T01:00:00Z", "Homepage ping", "East US", 5000.0, "Timeout", "op1"],
                    ["2026-03-23T01:05:00Z", "Homepage ping", "West EU", 4900.0, "Timeout", "op2"],
                ],
            }]
        }
        connected_client.query = AsyncMock(side_effect=[summary_response, failures_response])

        result = await get_app_insights_availability(since_minutes=60)
        data = json.loads(result)
        assert data["data_available"] is True
        assert data["test_count"] == 1
        assert data["overall_availability_pct"] == pytest.approx(96.67, rel=0.01)
        assert data["recent_failure_count"] == 2
        assert data["results_by_test"][0]["name"] == "Homepage ping"

    @pytest.mark.asyncio
    async def test_no_availability_tests(self, connected_client):
        """Zero results should return overall_availability_pct=None and empty lists."""
        connected_client.query = AsyncMock(return_value=EMPTY_TABLE_RESPONSE)
        result = await get_app_insights_availability()
        data = json.loads(result)
        assert data["data_available"] is True
        assert data["overall_availability_pct"] is None
        assert data["test_count"] == 0

    @pytest.mark.asyncio
    async def test_client_exception_returns_error(self, connected_client):
        connected_client.query = AsyncMock(side_effect=Exception("403 Forbidden"))
        result = await get_app_insights_availability()
        data = json.loads(result)
        assert "error" in data
        assert data["data_available"] is False
