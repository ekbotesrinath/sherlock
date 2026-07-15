"""
Tests for the investigate_service composite tool.

Sub-tools are patched at the investigate module's import sites, so these
tests exercise the fan-out, aggregation, and failure-isolation logic
without any network mocking.
"""

import json
from unittest.mock import AsyncMock, patch

import pytest

from sherlock.core.session_memory import SessionMemory
from sherlock.tools.investigate import investigate_service

GOLDEN_WARNING = json.dumps(
    {
        "overall_status": "WARNING",
        "health_signals": ["⚠️ Elevated error rate: 10.0%"],
        "errors": {"error_rate_pct": 10.0},
        "instrumentation": "apm",
        "baseline": {
            "available": True,
            "verdicts": [
                "✅ latency in line with last week",
                "✅ error rate in line with last week",
                "✅ traffic in line with last week",
            ],
        },
    }
)

CHANGES_CORRELATED = json.dumps(
    {
        "total_changes": 1,
        "timeline": [{"type": "deployment_marker", "label": "deploy of payment-svc-prod"}],
        "correlation": {
            "error_onset": {"detected": True},
            "correlated_changes": [
                {"label": "deploy of payment-svc-prod", "minutes_before_onset": 4.0}
            ],
            "verdict": (
                "🎯 deploy of payment-svc-prod landed 4m before the error onset — prime suspect."
            ),
        },
    }
)

LOGS_OK = json.dumps({"total_logs": 42, "logs": []})
K8S_OK = json.dumps({"health_signals": []})
INCIDENTS_OK = json.dumps({"total_incidents": 2, "incidents": []})
DEPS_OK = json.dumps({"in_graph": True})
SYNTH_OK = json.dumps({"monitor_name": "Payment Checkout - Prod", "diagnosis": "PASSING"})


@pytest.fixture
def _fresh_session_memory():
    """Isolate SessionMemory across tests."""
    SessionMemory.reset()
    yield
    SessionMemory.reset()


def _patch_subtools(**overrides):
    """Patch every sub-tool at the investigate module import sites."""
    defaults = {
        "get_service_golden_signals": GOLDEN_WARNING,
        "get_recent_changes": CHANGES_CORRELATED,
        "search_logs": LOGS_OK,
        "get_k8s_health": K8S_OK,
        "get_service_incidents": INCIDENTS_OK,
        "get_service_dependencies": DEPS_OK,
        "get_monitor_status": SYNTH_OK,
    }
    defaults.update(overrides)
    patchers = []
    for name, value in defaults.items():
        mock = value if isinstance(value, AsyncMock) else AsyncMock(return_value=value)
        patchers.append(patch(f"sherlock.tools.investigate.{name}", mock))
    return patchers


class TestInvestigateService:
    """Composite fan-out and aggregation behavior."""

    @pytest.mark.asyncio
    async def test_bundle_contains_all_domains(self, mock_context, _fresh_session_memory):
        patchers = _patch_subtools()
        for p in patchers:
            p.start()
        try:
            result = await investigate_service("payment-svc-prod")
        finally:
            for p in patchers:
                p.stop()
        parsed = json.loads(result)

        # payment-svc-prod has an associated monitor in the fixture.
        assert parsed["domains_run"] == [
            "golden_signals",
            "recent_changes",
            "logs",
            "kubernetes",
            "incidents",
            "dependencies",
            "synthetics",
        ]
        assert set(parsed["domains"].keys()) == set(parsed["domains_run"])
        assert parsed["failed_domains"] == []
        assert parsed["overall_status"] == "WARNING"
        assert parsed["service_name"] == "payment-svc-prod"

    @pytest.mark.asyncio
    async def test_root_cause_hint_from_change_correlation(
        self, mock_context, _fresh_session_memory
    ):
        patchers = _patch_subtools()
        for p in patchers:
            p.start()
        try:
            result = await investigate_service("payment-svc-prod")
        finally:
            for p in patchers:
                p.stop()
        parsed = json.loads(result)
        assert "prime suspect" in parsed["root_cause_hint"]
        assert any("🎯" in s for s in parsed["summary"])
        assert any("incident(s)" in s for s in parsed["summary"])
        assert any("ERROR-level log" in s for s in parsed["summary"])

    @pytest.mark.asyncio
    async def test_failed_domain_is_isolated(self, mock_context, _fresh_session_memory):
        patchers = _patch_subtools(search_logs=AsyncMock(side_effect=RuntimeError("boom")))
        for p in patchers:
            p.start()
        try:
            result = await investigate_service("payment-svc-prod")
        finally:
            for p in patchers:
                p.stop()
        parsed = json.loads(result)
        assert parsed["failed_domains"] == ["logs"]
        assert parsed["domains"]["logs"]["error"] == "boom"
        # Other domains unaffected.
        assert parsed["domains"]["golden_signals"]["overall_status"] == "WARNING"
        assert "error" not in parsed

    @pytest.mark.asyncio
    async def test_synthetics_skipped_without_associated_monitor(
        self, mock_context, mock_intelligence, _fresh_session_memory
    ):
        mock_intelligence.synthetics.monitor_map = {}
        patchers = _patch_subtools()
        for p in patchers:
            p.start()
        try:
            result = await investigate_service("payment-svc-prod")
        finally:
            for p in patchers:
                p.stop()
        parsed = json.loads(result)
        assert "synthetics" not in parsed["domains_run"]
        assert len(parsed["domains_run"]) == 6

    @pytest.mark.asyncio
    async def test_critical_signal_escalates_overall(self, mock_context, _fresh_session_memory):
        k8s_critical = json.dumps({"health_signals": ["🔴 3 pod(s) in Failed state"]})
        patchers = _patch_subtools(get_k8s_health=k8s_critical)
        for p in patchers:
            p.start()
        try:
            result = await investigate_service("payment-svc-prod")
        finally:
            for p in patchers:
                p.stop()
        parsed = json.loads(result)
        assert parsed["overall_status"] == "CRITICAL"

    @pytest.mark.asyncio
    async def test_baseline_regression_verdicts_in_summary(
        self, mock_context, _fresh_session_memory
    ):
        golden = json.loads(GOLDEN_WARNING)
        golden["baseline"]["verdicts"][1] = "🔴 error rate 24.0x last week (0.5% → 12.0%)"
        patchers = _patch_subtools(get_service_golden_signals=json.dumps(golden))
        for p in patchers:
            p.start()
        try:
            result = await investigate_service("payment-svc-prod")
        finally:
            for p in patchers:
                p.stop()
        parsed = json.loads(result)
        assert any("24.0x last week" in s for s in parsed["summary"])
        # ✅ baseline verdicts are filtered out of the summary.
        assert not any(s.startswith("✅") for s in parsed["summary"])

    @pytest.mark.asyncio
    async def test_snapshot_recorded_with_deploy_pattern(self, mock_context, _fresh_session_memory):
        patchers = _patch_subtools()
        for p in patchers:
            p.start()
        try:
            await investigate_service("payment-svc-prod")
        finally:
            for p in patchers:
                p.stop()
        snap = SessionMemory().get_last("123456")
        assert snap is not None
        assert snap.service_name == "payment-svc-prod"
        assert snap.severity == "WARNING"
        assert snap.causal_pattern == "DEPLOY_REGRESSION"
        assert "prime suspect" in snap.root_cause
        assert snap.error_rate == 10.0

    @pytest.mark.asyncio
    async def test_fuzzy_resolution(self, mock_context, _fresh_session_memory):
        patchers = _patch_subtools()
        for p in patchers:
            p.start()
        try:
            result = await investigate_service("payment-svc")
        finally:
            for p in patchers:
                p.stop()
        parsed = json.loads(result)
        assert parsed["service_name"] == "payment-svc-prod"
        assert parsed["resolved_from"] == "payment-svc"

    @pytest.mark.asyncio
    async def test_not_connected_returns_error(self, _fresh_session_memory):
        from sherlock.core.context import AccountContext

        AccountContext.reset_singleton()
        try:
            result = await investigate_service("payment-svc-prod")
        finally:
            AccountContext.reset_singleton()
        parsed = json.loads(result)
        assert "error" in parsed
        assert parsed["data_available"] is False
