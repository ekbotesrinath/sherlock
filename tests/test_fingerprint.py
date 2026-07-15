"""
Tests for drain-style log fingerprinting (core/fingerprint.py) and its
integration into search_logs.
"""

import json

import httpx
import pytest
import respx

from sherlock.core.fingerprint import WILDCARD, fingerprint_logs, mask_message
from sherlock.tools.logs import search_logs


class TestMaskMessage:
    """Variable-token masking."""

    def test_masks_numbers(self):
        assert mask_message("retry 3 of 5 failed") == "retry <NUM> of <NUM> failed"

    def test_masks_uuid(self):
        masked = mask_message("order 550e8400-e29b-41d4-a716-446655440000 not found")
        assert masked == "order <UUID> not found"

    def test_masks_ip_and_port(self):
        assert mask_message("connect to 10.0.12.34:5432 refused") == "connect to <IP> refused"

    def test_masks_timestamp(self):
        masked = mask_message("at 2026-07-15T10:32:11.123Z worker died")
        assert masked == "at <TIMESTAMP> worker died"

    def test_masks_path(self):
        masked = mask_message("failed to read /var/lib/app/config.yaml here")
        assert masked == "failed to read <PATH> here"

    def test_masks_hex_and_email(self):
        assert mask_message("txid 0xdeadbeef") == "txid <HEX>"
        assert mask_message("user bob@example.com locked") == "user <EMAIL> locked"

    def test_words_with_digits_untouched(self):
        assert mask_message("error42 in stage2b") == "error42 in stage2b"


class TestFingerprintLogs:
    """Template clustering behavior."""

    def test_identical_shapes_collapse_to_one_pattern(self):
        messages = [f"timeout connecting to db-{i} after 30s retry {i}" for i in range(500)]
        patterns = fingerprint_logs(messages)
        assert len(patterns) == 1
        assert patterns[0]["count"] == 500
        assert patterns[0]["percentage"] == 100.0

    def test_distinct_errors_stay_distinct(self):
        messages = (
            ["connection refused by payment-db port 5432"] * 30
            + ["null pointer exception in OrderService.checkout"] * 10
            + ["disk usage above threshold on node worker-1"] * 5
        )
        patterns = fingerprint_logs(messages)
        assert len(patterns) == 3
        # Sorted by count desc.
        assert [p["count"] for p in patterns] == [30, 10, 5]
        assert "connection refused" in patterns[0]["template"]

    def test_variable_positions_become_wildcards(self):
        messages = [
            "failed to process order alpha for tenant acme",
            "failed to process order beta for tenant globex",
        ]
        patterns = fingerprint_logs(messages)
        assert len(patterns) == 1
        template = patterns[0]["template"]
        assert template.startswith("failed to process order")
        assert WILDCARD in template

    def test_different_token_counts_never_merge(self):
        messages = ["a b c", "a b c d e f"]
        patterns = fingerprint_logs(messages)
        assert len(patterns) == 2

    def test_example_is_raw_message(self):
        messages = ["timeout after 30s on 10.0.0.1:443"] * 3
        patterns = fingerprint_logs(messages)
        assert patterns[0]["example"] == "timeout after 30s on 10.0.0.1:443"
        assert "<IP>" in patterns[0]["template"]

    def test_max_patterns_cap(self):
        # 30 structurally different messages (unique first token, distinct
        # lengths mod padding) → more clusters than the cap.
        messages = [f"kind{i} " + "word " * (i % 7) + "tail" for i in range(30)]
        patterns = fingerprint_logs(messages, max_patterns=5)
        assert len(patterns) == 5

    def test_empty_and_blank_messages_skipped(self):
        patterns = fingerprint_logs(["", "   ", "real error here"])
        assert len(patterns) == 1
        assert patterns[0]["percentage"] == 100.0

    def test_empty_input(self):
        assert fingerprint_logs([]) == []


class TestSearchLogsFingerprintIntegration:
    """log_patterns appears in search_logs responses."""

    @respx.mock
    @pytest.mark.asyncio
    async def test_log_patterns_in_response(self, mock_context):
        logs = [
            {
                "timestamp": 1750000000000 + i,
                "message": f"timeout connecting to db-{i} after 30s",
                "service.name": "payment-svc-prod",
                "level": "ERROR",
            }
            for i in range(50)
        ] + [
            {
                "timestamp": 1750000100000,
                "message": "null pointer exception in OrderService",
                "service.name": "payment-svc-prod",
                "level": "ERROR",
            }
        ]
        respx.post("https://api.newrelic.com/graphql").mock(
            return_value=httpx.Response(
                200,
                json={"data": {"actor": {"account": {"nrql": {"results": logs}}}}},
            )
        )

        result = await search_logs(service_name="payment-svc-prod", severity="ERROR")
        parsed = json.loads(result)

        assert parsed["total_logs"] == 51
        fp = parsed["log_patterns"]
        assert fp["total_messages"] == 51
        assert fp["distinct_patterns"] == 2
        assert fp["patterns"][0]["count"] == 50
        assert "timeout connecting to" in fp["patterns"][0]["template"]

    @respx.mock
    @pytest.mark.asyncio
    async def test_no_patterns_key_when_no_logs(self, mock_context):
        respx.post("https://api.newrelic.com/graphql").mock(
            return_value=httpx.Response(
                200,
                json={"data": {"actor": {"account": {"nrql": {"results": []}}}}},
            )
        )

        result = await search_logs(service_name="payment-svc-prod")
        parsed = json.loads(result)
        assert "log_patterns" not in parsed
