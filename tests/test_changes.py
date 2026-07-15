"""
Tests for the get_recent_changes tool and its correlation logic.
"""

import json

import httpx
import pytest
import respx

from sherlock.tools.changes import (
    CORRELATION_WINDOW_MINUTES,
    correlate_changes,
    detect_error_onset,
    get_recent_changes,
)

# Epoch anchor (seconds) for deterministic timestamps.
BASE_S = 1_750_000_000
ONSET_S = BASE_S + 180  # onset in the 4th timeseries bucket


def _mock_nrql_response(results):
    return {"data": {"actor": {"account": {"nrql": {"results": results}}}}}


def _error_ts_with_onset():
    """Flat ~0.5% then a sustained spike starting at ONSET_S."""
    return [
        {"beginTimeSeconds": BASE_S + i * 60, "error_rate": rate}
        for i, rate in enumerate([0.5, 0.4, 0.6, 12.0, 15.0])
    ]


def _deploy_marker(ts_ms, app="payment-svc-prod", revision="abc123"):
    return {
        "timestamp": ts_ms,
        "appName": app,
        "description": "release",
        "revision": revision,
        "changelog": "",
        "user": "ci-bot",
    }


def _k8s_event(ts_ms, name="payment-svc-prod"):
    return {
        "timestamp": ts_ms,
        "reason": "ScalingReplicaSet",
        "object_kind": "Deployment",
        "object_name": name,
        "message": "Scaled up replica set",
        "clusterName": "main-cluster-prod",
        "namespaceName": "payments-prod",
    }


class TestDetectErrorOnset:
    """Unit tests for the onset detector."""

    def test_detects_sustained_spike(self):
        onset = detect_error_onset(_error_ts_with_onset())
        assert onset["detected"] is True
        assert onset["timestamp_ms"] == ONSET_S * 1000
        assert onset["error_rate_pct"] == 12.0
        assert onset["baseline_pct"] == 0.5

    def test_flat_series_no_onset(self):
        ts = [{"beginTimeSeconds": BASE_S + i * 60, "error_rate": 0.5} for i in range(5)]
        onset = detect_error_onset(ts)
        assert onset["detected"] is False

    def test_already_elevated_at_window_start(self):
        ts = [{"beginTimeSeconds": BASE_S + i * 60, "error_rate": 20.0} for i in range(5)]
        onset = detect_error_onset(ts)
        assert onset["detected"] is True
        assert "already elevated" in onset["note"]

    def test_single_bucket_spike_not_sustained(self):
        """One noisy bucket followed by recovery is not an onset."""
        rates = [0.5, 0.5, 9.0, 0.4, 0.5]
        ts = [{"beginTimeSeconds": BASE_S + i * 60, "error_rate": r} for i, r in enumerate(rates)]
        onset = detect_error_onset(ts)
        assert onset["detected"] is False

    def test_insufficient_data(self):
        assert detect_error_onset([])["detected"] is False
        assert detect_error_onset([{"beginTimeSeconds": BASE_S}])["detected"] is False

    def test_null_rates_treated_as_zero(self):
        rates = [None, None, 0.5, 12.0, 14.0]
        ts = [{"beginTimeSeconds": BASE_S + i * 60, "error_rate": r} for i, r in enumerate(rates)]
        onset = detect_error_onset(ts)
        assert onset["detected"] is True
        assert onset["error_rate_pct"] == 12.0


class TestCorrelateChanges:
    """Unit tests for change-to-onset correlation."""

    def test_change_inside_window_is_prime_suspect(self):
        onset = {"detected": True, "timestamp_ms": ONSET_S * 1000, "time": "t"}
        timeline = [
            {"timestamp_ms": (ONSET_S - 240) * 1000, "label": "deploy of payment-svc-prod"},
            {"timestamp_ms": (ONSET_S - 90 * 60) * 1000, "label": "old deploy"},
        ]
        result = correlate_changes(timeline, onset)
        assert len(result["correlated_changes"]) == 1
        assert result["correlated_changes"][0]["minutes_before_onset"] == 4.0
        assert "prime suspect" in result["verdict"]
        assert "deploy of payment-svc-prod" in result["verdict"]

    def test_change_after_onset_not_correlated(self):
        onset = {"detected": True, "timestamp_ms": ONSET_S * 1000, "time": "t"}
        timeline = [{"timestamp_ms": (ONSET_S + 300) * 1000, "label": "later deploy"}]
        result = correlate_changes(timeline, onset)
        assert result["correlated_changes"] == []
        assert "not deploy-related" in result["verdict"]

    def test_no_onset_gives_context_verdict(self):
        result = correlate_changes([], {"detected": False})
        assert result["correlated_changes"] == []
        assert "No error onset" in result["verdict"]

    def test_window_boundary_included(self):
        onset = {"detected": True, "timestamp_ms": ONSET_S * 1000, "time": "t"}
        edge_ts = (ONSET_S - CORRELATION_WINDOW_MINUTES * 60) * 1000
        result = correlate_changes([{"timestamp_ms": edge_ts, "label": "edge deploy"}], onset)
        assert len(result["correlated_changes"]) == 1


class TestGetRecentChanges:
    """Integration tests for the tool (mocked NerdGraph)."""

    def _route(self, deploys, k8s_events, error_ts):
        def _respond(request):
            body = request.content.decode()
            if "FROM Deployment" in body:
                return httpx.Response(200, json=_mock_nrql_response(deploys))
            if "InfrastructureEvent" in body:
                return httpx.Response(200, json=_mock_nrql_response(k8s_events))
            if "TIMESERIES" in body:
                return httpx.Response(200, json=_mock_nrql_response(error_ts))
            return httpx.Response(200, json=_mock_nrql_response([]))

        return _respond

    @respx.mock
    @pytest.mark.asyncio
    async def test_correlates_deploy_with_error_onset(self, mock_context):
        deploys = [_deploy_marker((ONSET_S - 240) * 1000)]
        k8s_events = [_k8s_event((ONSET_S - 90 * 60) * 1000)]
        respx.post("https://api.newrelic.com/graphql").mock(
            side_effect=self._route(deploys, k8s_events, _error_ts_with_onset())
        )

        result = await get_recent_changes("payment-svc-prod", since_minutes=240)
        parsed = json.loads(result)

        assert parsed["service_name"] == "payment-svc-prod"
        assert parsed["total_changes"] == 2
        assert parsed["correlation"]["error_onset"]["detected"] is True
        correlated = parsed["correlation"]["correlated_changes"]
        assert len(correlated) == 1
        assert correlated[0]["minutes_before_onset"] == 4.0
        assert "prime suspect" in parsed["correlation"]["verdict"]

    @respx.mock
    @pytest.mark.asyncio
    async def test_timeline_merged_and_sorted_newest_first(self, mock_context):
        deploys = [_deploy_marker((BASE_S - 600) * 1000)]
        k8s_events = [_k8s_event(BASE_S * 1000)]
        respx.post("https://api.newrelic.com/graphql").mock(
            side_effect=self._route(deploys, k8s_events, [])
        )

        result = await get_recent_changes("payment-svc-prod")
        parsed = json.loads(result)
        types = [e["type"] for e in parsed["timeline"]]
        assert types == ["k8s_deployment_event", "deployment_marker"]

    @respx.mock
    @pytest.mark.asyncio
    async def test_account_wide_without_service(self, mock_context):
        deploys = [_deploy_marker(BASE_S * 1000, app="auth-service-prod")]
        calls = []

        def _respond(request):
            body = request.content.decode()
            calls.append(body)
            if "FROM Deployment" in body:
                return httpx.Response(200, json=_mock_nrql_response(deploys))
            return httpx.Response(200, json=_mock_nrql_response([]))

        respx.post("https://api.newrelic.com/graphql").mock(side_effect=_respond)

        result = await get_recent_changes()
        parsed = json.loads(result)

        assert parsed["service_name"] is None
        assert "correlation" not in parsed
        assert parsed["total_changes"] == 1
        # No error timeseries query without a service.
        assert not any("TIMESERIES" in c for c in calls)
        # Account-wide: no appName filter on the marker query.
        assert "WHERE appName" not in parsed["nrql_used"]["deployment_markers"]

    @respx.mock
    @pytest.mark.asyncio
    async def test_k8s_query_scoped_by_namespace(self, mock_context):
        respx.post("https://api.newrelic.com/graphql").mock(side_effect=self._route([], [], []))

        result = await get_recent_changes("payment-svc-prod")
        parsed = json.loads(result)
        k8s_nrql = parsed["nrql_used"]["k8s_events"]
        assert "LIKE '%payment-svc-prod%'" in k8s_nrql
        assert "namespaceName = 'payments-prod'" in k8s_nrql

    @respx.mock
    @pytest.mark.asyncio
    async def test_no_changes_returns_hint(self, mock_context):
        respx.post("https://api.newrelic.com/graphql").mock(side_effect=self._route([], [], []))

        result = await get_recent_changes("payment-svc-prod")
        parsed = json.loads(result)
        assert parsed["total_changes"] == 0
        assert "hint" in parsed

    @respx.mock
    @pytest.mark.asyncio
    async def test_fuzzy_resolution(self, mock_context):
        respx.post("https://api.newrelic.com/graphql").mock(side_effect=self._route([], [], []))

        result = await get_recent_changes("payment-svc")
        parsed = json.loads(result)
        assert parsed["service_name"] == "payment-svc-prod"
        assert parsed["resolved_from"] == "payment-svc"

    @respx.mock
    @pytest.mark.asyncio
    async def test_api_errors_degrade_gracefully(self, mock_context):
        respx.post("https://api.newrelic.com/graphql").mock(return_value=httpx.Response(500))

        result = await get_recent_changes("payment-svc-prod")
        parsed = json.loads(result)
        assert parsed["total_changes"] == 0
        assert "error" not in parsed
