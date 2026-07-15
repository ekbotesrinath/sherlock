"""
Tests for the proactive health-digest script (scripts/digest.py).
"""

import asyncio
import json

import pytest

from sherlock.scripts.digest import (
    build_digest,
    classify_result,
    digest_to_webhook_payload,
    format_digest_text,
    sweep_services,
)


def _golden_response(
    status="HEALTHY",
    signals=None,
    baseline_verdicts=None,
    error_rate=0.2,
    p95=0.4,
    rpm=120,
):
    return json.dumps(
        {
            "service_name": "svc",
            "overall_status": status,
            "health_signals": signals or [],
            "errors": {"error_rate_pct": error_rate},
            "latency": {"p95": p95},
            "throughput": {"rpm": rpm},
            "baseline": {
                "available": baseline_verdicts is not None,
                "verdicts": baseline_verdicts or [],
            },
        }
    )


class TestClassifyResult:
    """Reducing a golden-signals response to a digest row."""

    def test_healthy_service(self):
        row = classify_result("payment-svc-prod", _golden_response())
        assert row["status"] == "HEALTHY"
        assert row["error_rate_pct"] == 0.2
        assert row["rpm"] == 120

    def test_critical_with_signals(self):
        raw = _golden_response(status="CRITICAL", signals=["🔴 CRITICAL error rate: 15.0%"])
        row = classify_result("payment-svc-prod", raw)
        assert row["status"] == "CRITICAL"
        assert row["signals"] == ["🔴 CRITICAL error rate: 15.0%"]

    def test_tool_error_becomes_no_data(self):
        row = classify_result("gone-svc", json.dumps({"error": "not connected"}))
        assert row["status"] == "NO_DATA"
        assert "check failed" in row["signals"][0]

    def test_unparseable_becomes_no_data(self):
        row = classify_result("svc", "not json at all")
        assert row["status"] == "NO_DATA"

    def test_baseline_keeps_only_abnormal_verdicts(self):
        raw = _golden_response(
            status="WARNING",
            baseline_verdicts=[
                "🔴 error rate 24.0x last week",
                "✅ latency in line with last week",
            ],
        )
        row = classify_result("svc", raw)
        assert row["baseline_verdicts"] == ["🔴 error rate 24.0x last week"]


class TestSweepServices:
    """Bounded-concurrency fan-out."""

    @pytest.mark.asyncio
    async def test_sweeps_all_services_in_order(self):
        async def checker(service_name, since_minutes):
            return _golden_response(status="HEALTHY")

        rows = await sweep_services(["a", "b", "c"], 60, checker=checker)
        assert [r["service"] for r in rows] == ["a", "b", "c"]

    @pytest.mark.asyncio
    async def test_concurrency_is_bounded(self):
        active = 0
        max_active = 0

        async def checker(service_name, since_minutes):
            nonlocal active, max_active
            active += 1
            max_active = max(max_active, active)
            await asyncio.sleep(0.01)
            active -= 1
            return _golden_response()

        await sweep_services([f"s{i}" for i in range(10)], 60, concurrency=3, checker=checker)
        assert max_active <= 3

    @pytest.mark.asyncio
    async def test_checker_exception_becomes_no_data_row(self):
        async def checker(service_name, since_minutes):
            raise RuntimeError("boom")

        rows = await sweep_services(["a"], 60, checker=checker)
        assert rows[0]["status"] == "NO_DATA"
        assert "boom" in rows[0]["signals"][0]


class TestBuildDigest:
    """Digest assembly and ordering."""

    def _rows(self):
        return [
            classify_result("healthy-svc", _golden_response()),
            classify_result("warn-svc", _golden_response(status="WARNING", signals=["⚠️ High P99"])),
            classify_result(
                "crit-svc", _golden_response(status="CRITICAL", signals=["🔴 error rate"])
            ),
            classify_result("dead-svc", json.dumps({"error": "no data"})),
        ]

    def test_sorted_worst_first_with_totals(self):
        digest = build_digest("PROD", "123456", 60, self._rows())
        order = [r["service"] for r in digest["services"]]
        assert order == ["crit-svc", "warn-svc", "dead-svc", "healthy-svc"]
        assert digest["totals"] == {"CRITICAL": 1, "WARNING": 1, "NO_DATA": 1, "HEALTHY": 1}
        assert digest["overall_status"] == "CRITICAL"

    def test_overall_warning_without_critical(self):
        rows = [
            classify_result("w", _golden_response(status="WARNING")),
            classify_result("h", _golden_response()),
        ]
        digest = build_digest("PROD", "123456", 60, rows)
        assert digest["overall_status"] == "WARNING"

    def test_overall_no_data_when_nothing_responds(self):
        rows = [classify_result("d", json.dumps({"error": "x"}))]
        digest = build_digest("PROD", "123456", 60, rows)
        assert digest["overall_status"] == "NO_DATA"


class TestDigestOutput:
    """Text and webhook rendering."""

    def _digest(self):
        rows = [
            classify_result(
                "crit-svc",
                _golden_response(
                    status="CRITICAL",
                    signals=["🔴 CRITICAL error rate: 15.0%"],
                    baseline_verdicts=["🔴 error rate 24.0x last week"],
                ),
            ),
            classify_result("healthy-svc", _golden_response()),
        ]
        return build_digest("PROD", "123456", 60, rows)

    def test_text_hides_healthy_services(self):
        text = format_digest_text(self._digest())
        assert "crit-svc" in text
        assert "🔴 CRITICAL error rate: 15.0%" in text
        assert "error rate 24.0x last week" in text
        assert "healthy-svc" not in text

    def test_text_all_healthy(self):
        rows = [classify_result("h", _golden_response())]
        text = format_digest_text(build_digest("PROD", "123456", 60, rows))
        assert "All services healthy" in text

    def test_slack_payload_lists_unhealthy_only(self):
        payload = digest_to_webhook_payload(self._digest(), "slack")
        flat = json.dumps(payload)
        assert "crit-svc" in flat
        assert "healthy-svc" not in flat
        assert "1 critical" in payload["text"]

    def test_teams_payload_theme_matches_overall(self):
        payload = digest_to_webhook_payload(self._digest(), "teams")
        assert payload["@type"] == "MessageCard"
        assert payload["themeColor"] == "D93F3C"  # CRITICAL
