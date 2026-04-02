"""
Tests for Prometheus tools (connect, promql, alerts).
"""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.datasource_context import DataSourceContext
from tools.prometheus.promql import connect_prometheus, get_prometheus_alerts, run_promql_query


@pytest.fixture(autouse=True)
def reset_context():
    ctx = DataSourceContext()
    ctx.clear_prometheus()
    yield
    ctx.clear_prometheus()


@pytest.fixture
def mock_client():
    client = MagicMock()
    client.base_url = "http://prometheus:9090"
    client.alertmanager_url = "http://alertmanager:9093"
    client.health = AsyncMock(return_value=True)
    client.query = AsyncMock(return_value={"status": "success", "data": {"resultType": "vector", "result": []}})
    client.query_range = AsyncMock(return_value={"status": "success", "data": {"resultType": "matrix", "result": []}})
    client.get_firing_alerts = AsyncMock(return_value=[])
    client.get_silences = AsyncMock(return_value=[])
    return client


@pytest.fixture
def connected_client(mock_client):
    DataSourceContext().set_prometheus(mock_client)
    return mock_client


# ── connect_prometheus ────────────────────────────────────────────────────


class TestConnectPrometheus:

    @pytest.mark.asyncio
    async def test_successful_connection(self):
        mock = MagicMock()
        mock.base_url = "http://prometheus:9090"
        mock.health = AsyncMock(return_value={"status": "success"})

        with patch("tools.promql.PrometheusClient", return_value=mock):
            result = json.loads(await connect_prometheus(url="http://prometheus:9090"))

        assert result["status"] == "connected"
        assert DataSourceContext().get_prometheus() is mock

    @pytest.mark.asyncio
    async def test_missing_url_returns_error(self):
        result = json.loads(await connect_prometheus(url=""))
        assert "error" in result
        assert result["data_available"] is False

    @pytest.mark.asyncio
    async def test_health_check_failure_returns_error(self):
        mock = MagicMock()
        mock.base_url = "http://prometheus:9090"
        mock.health = AsyncMock(side_effect=Exception("connection refused"))

        with patch("tools.promql.PrometheusClient", return_value=mock):
            result = json.loads(await connect_prometheus(url="http://prometheus:9090"))

        assert "error" in result
        assert result["data_available"] is False

    @pytest.mark.asyncio
    async def test_stores_alertmanager_url(self):
        mock = MagicMock()
        mock.base_url = "http://prometheus:9090"
        mock.alertmanager_url = "http://alertmanager:9093"
        mock.health = AsyncMock(return_value={"status": "success"})

        with patch("tools.promql.PrometheusClient", return_value=mock):
            result = json.loads(await connect_prometheus(
                url="http://prometheus:9090",
                alertmanager_url="http://alertmanager:9093",
            ))

        assert result["status"] == "connected"


# ── run_promql_query ──────────────────────────────────────────────────────


class TestRunPromqlQuery:

    @pytest.mark.asyncio
    async def test_not_connected_returns_error(self):
        result = json.loads(await run_promql_query(promql="up"))
        assert "error" in result
        assert result["data_available"] is False

    @pytest.mark.asyncio
    async def test_instant_query_returns_results(self, connected_client):
        connected_client.query = AsyncMock(return_value={
            "status": "success",
            "data": {
                "resultType": "vector",
                "result": [
                    {"metric": {"job": "api"}, "value": [1711152000, "1"]},
                ],
            },
        })
        result = json.loads(await run_promql_query(promql="up", query_type="instant"))
        assert result["result_type"] == "vector"
        assert "error" not in result

    @pytest.mark.asyncio
    async def test_range_query_returns_results(self, connected_client):
        connected_client.query_range = AsyncMock(return_value={
            "status": "success",
            "data": {
                "resultType": "matrix",
                "result": [
                    {"metric": {"job": "api"}, "values": [[1711152000, "1"]]},
                ],
            },
        })
        result = json.loads(await run_promql_query(promql="rate(http_requests_total[5m])", query_type="range"))
        assert result["result_type"] == "matrix"
        assert "error" not in result

    @pytest.mark.asyncio
    async def test_empty_promql_returns_error(self, connected_client):
        result = json.loads(await run_promql_query(promql=""))
        assert "error" in result
        assert result["data_available"] is False

    @pytest.mark.asyncio
    async def test_client_exception_returns_error(self, connected_client):
        connected_client.query = AsyncMock(side_effect=Exception("bad expression"))
        result = json.loads(await run_promql_query(promql="INVALID{{{", query_type="instant"))
        assert "error" in result
        assert result["data_available"] is False


# ── get_prometheus_alerts ─────────────────────────────────────────────────


class TestGetPrometheusAlerts:

    @pytest.mark.asyncio
    async def test_not_connected_returns_error(self):
        result = json.loads(await get_prometheus_alerts())
        assert "error" in result
        assert result["data_available"] is False

    @pytest.mark.asyncio
    async def test_no_alertmanager_configured_returns_error(self, connected_client):
        connected_client.alertmanager_url = None
        result = json.loads(await get_prometheus_alerts())
        assert "error" in result
        assert result["data_available"] is False

    @pytest.mark.asyncio
    async def test_returns_firing_alerts(self, connected_client):
        connected_client.get_firing_alerts = AsyncMock(return_value=[
            {
                "labels": {"alertname": "HighCPU", "severity": "critical"},
                "annotations": {"summary": "CPU > 90%"},
                "status": {"state": "active", "silencedBy": [], "inhibitedBy": []},
            }
        ])
        result = json.loads(await get_prometheus_alerts())
        assert result["data_available"] is True
        assert result["alert_count"] == 1
        assert result["alerts"][0]["labels"]["alertname"] == "HighCPU"

    @pytest.mark.asyncio
    async def test_no_alerts_firing(self, connected_client):
        connected_client.get_firing_alerts = AsyncMock(return_value=[])
        result = json.loads(await get_prometheus_alerts())
        assert result["data_available"] is True
        assert result["alert_count"] == 0

    @pytest.mark.asyncio
    async def test_client_exception_returns_error(self, connected_client):
        connected_client.get_firing_alerts = AsyncMock(side_effect=Exception("connection refused"))
        result = json.loads(await get_prometheus_alerts())
        assert "error" in result
        assert result["data_available"] is False
