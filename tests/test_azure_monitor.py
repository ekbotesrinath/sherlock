"""
Tests for Azure Monitor tools (connect, metrics, logs, alerts).
"""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.datasource_context import DataSourceContext
from tools.azure.monitor_tool import (
    connect_azure_monitor,
    get_azure_monitor_alerts,
    query_azure_monitor_logs,
    query_azure_monitor_metrics,
)


@pytest.fixture(autouse=True)
def reset_context():
    ctx = DataSourceContext()
    ctx.clear_azure_monitor()
    yield
    ctx.clear_azure_monitor()


@pytest.fixture
def mock_client():
    client = MagicMock()
    client.subscription_id = "sub-123"
    client.validate = AsyncMock(return_value={"displayName": "Test Sub", "state": "Enabled"})
    client.query_metrics = AsyncMock(return_value={"value": []})
    client.query_logs = AsyncMock(return_value={"tables": [{"columns": [], "rows": []}]})
    client.get_fired_alerts = AsyncMock(return_value={"value": []})
    client.get_alert_rules = AsyncMock(return_value={"value": []})
    return client


@pytest.fixture
def connected_client(mock_client):
    DataSourceContext().set_azure_monitor(mock_client)
    return mock_client


# ── connect_azure_monitor ─────────────────────────────────────────────────


class TestConnectAzureMonitor:

    @pytest.mark.asyncio
    async def test_successful_connection(self):
        mock = MagicMock()
        mock.subscription_id = "sub-123"
        mock.validate = AsyncMock(return_value={"displayName": "Test Sub", "state": "Enabled"})

        with patch("tools.azure.monitor_tool.AzureMonitorClient", return_value=mock):
            result = json.loads(await connect_azure_monitor(
                tenant_id="t1", client_id="c1",
                client_secret="s1", subscription_id="sub-123",
            ))

        assert result["status"] == "connected"
        assert DataSourceContext().get_azure_monitor() is mock

    @pytest.mark.asyncio
    async def test_missing_tenant_id_returns_error(self):
        result = json.loads(await connect_azure_monitor(
            tenant_id="", client_id="c1", client_secret="s1", subscription_id="sub-123",
        ))
        assert "error" in result
        assert result["data_available"] is False

    @pytest.mark.asyncio
    async def test_validate_failure_returns_error(self):
        mock = MagicMock()
        mock.subscription_id = "sub-123"
        mock.validate = AsyncMock(side_effect=Exception("401 Unauthorized"))

        with patch("tools.azure.monitor_tool.AzureMonitorClient", return_value=mock):
            result = json.loads(await connect_azure_monitor(
                tenant_id="t1", client_id="c1", client_secret="bad", subscription_id="sub-123",
            ))

        assert "error" in result
        assert result["data_available"] is False


# ── query_azure_monitor_metrics ───────────────────────────────────────────


class TestQueryAzureMonitorMetrics:

    @pytest.mark.asyncio
    async def test_not_connected_returns_error(self):
        result = json.loads(await query_azure_monitor_metrics(
            resource_id="/sub/res", metric_names="Http5xx",
        ))
        assert "error" in result
        assert result["data_available"] is False

    @pytest.mark.asyncio
    async def test_returns_metrics(self, connected_client):
        connected_client.query_metrics = AsyncMock(return_value={
            "value": [{"name": {"value": "Http5xx"}, "timeseries": []}],
        })
        result = json.loads(await query_azure_monitor_metrics(
            resource_id="/sub/res", metric_names="Http5xx",
        ))
        assert result["metric_count"] == 1
        assert "error" not in result

    @pytest.mark.asyncio
    async def test_missing_resource_id_returns_error(self, connected_client):
        result = json.loads(await query_azure_monitor_metrics(
            resource_id="", metric_names="Http5xx",
        ))
        assert "error" in result
        assert result["data_available"] is False

    @pytest.mark.asyncio
    async def test_client_exception_returns_error(self, connected_client):
        connected_client.query_metrics = AsyncMock(side_effect=Exception("timeout"))
        result = json.loads(await query_azure_monitor_metrics(
            resource_id="/sub/res", metric_names="Http5xx",
        ))
        assert "error" in result
        assert result["data_available"] is False


# ── query_azure_monitor_logs ──────────────────────────────────────────────


class TestQueryAzureMonitorLogs:

    @pytest.mark.asyncio
    async def test_not_connected_returns_error(self):
        result = json.loads(await query_azure_monitor_logs(
            workspace_id="ws-1", kql="AppRequests | take 10",
        ))
        assert "error" in result
        assert result["data_available"] is False

    @pytest.mark.asyncio
    async def test_returns_rows(self, connected_client):
        connected_client.query_logs = AsyncMock(return_value={
            "tables": [{
                "columns": [{"name": "ResultCode"}, {"name": "Count"}],
                "rows": [["500", 7]],
            }]
        })
        result = json.loads(await query_azure_monitor_logs(
            workspace_id="ws-1", kql="AppRequests | take 10",
        ))
        assert result["row_count"] == 1
        assert "error" not in result

    @pytest.mark.asyncio
    async def test_missing_workspace_id_returns_error(self, connected_client):
        result = json.loads(await query_azure_monitor_logs(
            workspace_id="", kql="AppRequests | take 10",
        ))
        assert "error" in result
        assert result["data_available"] is False

    @pytest.mark.asyncio
    async def test_client_exception_returns_error(self, connected_client):
        connected_client.query_logs = AsyncMock(side_effect=Exception("403"))
        result = json.loads(await query_azure_monitor_logs(
            workspace_id="ws-1", kql="AppRequests | take 10",
        ))
        assert "error" in result
        assert result["data_available"] is False


# ── get_azure_monitor_alerts ──────────────────────────────────────────────


class TestGetAzureMonitorAlerts:

    @pytest.mark.asyncio
    async def test_not_connected_returns_error(self):
        result = json.loads(await get_azure_monitor_alerts())
        assert "error" in result
        assert result["data_available"] is False

    @pytest.mark.asyncio
    async def test_returns_alerts(self, connected_client):
        connected_client.get_fired_alerts = AsyncMock(return_value={
            "value": [
                {
                    "name": "alert-1",
                    "properties": {
                        "alertRuleName": "HighCPU",
                        "severity": "Sev1",
                        "monitorCondition": "Fired",
                        "startDateTime": "2026-03-23T01:00:00Z",
                    },
                }
            ]
        })
        result = json.loads(await get_azure_monitor_alerts())
        assert result["data_available"] is True
        assert result["alert_count"] >= 1

    @pytest.mark.asyncio
    async def test_no_alerts(self, connected_client):
        connected_client.get_fired_alerts = AsyncMock(return_value={"value": []})
        result = json.loads(await get_azure_monitor_alerts())
        assert result["data_available"] is True
        assert result["alert_count"] == 0

    @pytest.mark.asyncio
    async def test_include_rules(self, connected_client):
        connected_client.get_fired_alerts = AsyncMock(return_value={"value": []})
        connected_client.get_alert_rules = AsyncMock(return_value={
            "value": [{"name": "rule-1", "properties": {"enabled": True}}]
        })
        result = json.loads(await get_azure_monitor_alerts(include_rules=True))
        assert result["data_available"] is True
        connected_client.get_alert_rules.assert_called_once()

    @pytest.mark.asyncio
    async def test_client_exception_returns_error(self, connected_client):
        connected_client.get_fired_alerts = AsyncMock(side_effect=Exception("500"))
        result = json.loads(await get_azure_monitor_alerts())
        assert "error" in result
        assert result["data_available"] is False
