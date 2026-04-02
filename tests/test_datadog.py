"""
Tests for Datadog tools (connect, metrics, monitors, logs).
"""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.datasource_context import DataSourceContext
from tools.datadog.datadog_tool import (
    connect_datadog,
    get_datadog_monitors,
    query_datadog_metrics,
    search_datadog_logs,
)


@pytest.fixture(autouse=True)
def reset_context():
    ctx = DataSourceContext()
    ctx.clear_datadog()
    yield
    ctx.clear_datadog()


@pytest.fixture
def mock_client():
    client = MagicMock()
    client.site = "US1"
    client.validate = AsyncMock(return_value={"valid": True})
    client.query_metrics = AsyncMock(return_value={"series": []})
    client.get_monitors = AsyncMock(return_value={"monitors": [], "total": 0})
    client.get_triggered_monitors = AsyncMock(return_value={"monitors": [], "total": 0})
    client.search_logs = AsyncMock(return_value={"data": []})
    return client


@pytest.fixture
def connected_client(mock_client):
    DataSourceContext().set_datadog(mock_client)
    return mock_client


# ── connect_datadog ───────────────────────────────────────────────────────


class TestConnectDatadog:

    @pytest.mark.asyncio
    async def test_successful_connection(self):
        mock = MagicMock()
        mock.site = "US1"
        mock.validate = AsyncMock(return_value={"valid": True})

        with patch("tools.datadog.datadog_tool.DatadogClient", return_value=mock):
            result = json.loads(await connect_datadog(api_key="key123", app_key="app123"))

        assert result["status"] == "connected"
        assert DataSourceContext().get_datadog() is mock

    @pytest.mark.asyncio
    async def test_missing_api_key_returns_error(self):
        result = json.loads(await connect_datadog(api_key="", app_key="app123"))
        assert "error" in result
        assert result["data_available"] is False

    @pytest.mark.asyncio
    async def test_missing_app_key_returns_error(self):
        result = json.loads(await connect_datadog(api_key="key123", app_key=""))
        assert "error" in result
        assert result["data_available"] is False

    @pytest.mark.asyncio
    async def test_validate_failure_returns_error(self):
        mock = MagicMock()
        mock.site = "US1"
        mock.validate = AsyncMock(return_value={"valid": False})

        with patch("tools.datadog.datadog_tool.DatadogClient", return_value=mock):
            result = json.loads(await connect_datadog(api_key="bad", app_key="bad"))

        assert "error" in result
        assert result["data_available"] is False


# ── query_datadog_metrics ─────────────────────────────────────────────────


class TestQueryDatadogMetrics:

    @pytest.mark.asyncio
    async def test_not_connected_returns_error(self):
        result = json.loads(await query_datadog_metrics(query="avg:system.cpu.user{*}"))
        assert "error" in result
        assert result["data_available"] is False

    @pytest.mark.asyncio
    async def test_returns_series(self, connected_client):
        connected_client.query_metrics = AsyncMock(return_value={
            "series": [{"scope": "host:web-1", "pointlist": [[1711152000, 42.5]]}]
        })
        result = json.loads(await query_datadog_metrics(query="avg:system.cpu.user{*}"))
        assert result["series_count"] == 1
        assert "error" not in result

    @pytest.mark.asyncio
    async def test_missing_query_returns_error(self, connected_client):
        result = json.loads(await query_datadog_metrics(query=""))
        assert "error" in result
        assert result["data_available"] is False

    @pytest.mark.asyncio
    async def test_client_exception_returns_error(self, connected_client):
        connected_client.query_metrics = AsyncMock(side_effect=Exception("timeout"))
        result = json.loads(await query_datadog_metrics(query="avg:system.cpu.user{*}"))
        assert "error" in result
        assert result["data_available"] is False


# ── get_datadog_monitors ──────────────────────────────────────────────────


class TestGetDatadogMonitors:

    @pytest.mark.asyncio
    async def test_not_connected_returns_error(self):
        result = json.loads(await get_datadog_monitors())
        assert "error" in result
        assert result["data_available"] is False

    @pytest.mark.asyncio
    async def test_returns_all_monitors(self, connected_client):
        connected_client.get_monitors = AsyncMock(return_value={"monitors": [
            {"id": 1, "name": "CPU High", "overall_state": "OK", "type": "metric alert"},
            {"id": 2, "name": "Error Rate", "overall_state": "Alert", "type": "metric alert"},
        ], "total": 2})
        result = json.loads(await get_datadog_monitors())
        assert result["data_available"] is True
        assert result["total"] == 2

    @pytest.mark.asyncio
    async def test_triggered_only(self, connected_client):
        connected_client.get_triggered_monitors = AsyncMock(return_value={"monitors": [
            {"id": 2, "name": "Error Rate", "overall_state": "Alert", "type": "metric alert"},
        ], "total": 1})
        result = json.loads(await get_datadog_monitors(triggered_only=True))
        assert result["data_available"] is True
        assert result["total"] == 1
        connected_client.get_triggered_monitors.assert_called_once()

    @pytest.mark.asyncio
    async def test_client_exception_returns_error(self, connected_client):
        connected_client.get_monitors = AsyncMock(side_effect=Exception("API error"))
        result = json.loads(await get_datadog_monitors())
        assert "error" in result
        assert result["data_available"] is False


# ── search_datadog_logs ───────────────────────────────────────────────────


class TestSearchDatadogLogs:

    @pytest.mark.asyncio
    async def test_not_connected_returns_error(self):
        result = json.loads(await search_datadog_logs(query="status:error"))
        assert "error" in result
        assert result["data_available"] is False

    @pytest.mark.asyncio
    async def test_returns_logs(self, connected_client):
        connected_client.search_logs = AsyncMock(return_value={
            "data": [
                {"id": "log-1", "attributes": {"status": "error", "service": "api", "message": "boom"}},
            ]
        })
        result = json.loads(await search_datadog_logs(query="status:error"))
        assert result["data_available"] is True
        assert result["log_count"] == 1

    @pytest.mark.asyncio
    async def test_empty_query_defaults_to_wildcard(self, connected_client):
        """Empty query should still succeed (treated as '*')."""
        connected_client.search_logs = AsyncMock(return_value={"data": []})
        result = json.loads(await search_datadog_logs(query=""))
        assert result["data_available"] is True

    @pytest.mark.asyncio
    async def test_client_exception_returns_error(self, connected_client):
        connected_client.search_logs = AsyncMock(side_effect=Exception("rate limit"))
        result = json.loads(await search_datadog_logs(query="status:error"))
        assert "error" in result
        assert result["data_available"] is False
