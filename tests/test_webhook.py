"""
Tests for Slack/Teams webhook delivery (core/webhook.py + the
post_report_to_webhook tool).
"""

import json
from datetime import datetime, timezone

import httpx
import pytest
import respx

from sherlock.core.session_memory import InvestigationSnapshot, SessionMemory
from sherlock.core.webhook import (
    build_report_payload,
    build_slack_payload,
    build_teams_payload,
    get_webhook_url,
    post_webhook,
    send_report,
)
from sherlock.tools.webhook_tools import post_report_to_webhook

SLACK_URL = "https://hooks.slack.example/services/T000/B000/XXX"
TEAMS_URL = "https://outlook.office.example/webhook/abc"


def _report(**overrides):
    report = {
        "service_name": "payments-prod/payment-svc-prod",
        "severity": "CRITICAL",
        "confidence": "HIGH",
        "account_id": "123456",
        "account_name": "Test Account",
        "window_minutes": 60,
        "root_cause": "DB connection pool exhausted",
        "causal_chain": "DB pool exhausted → health checks fail → 503s",
        "error_rate": 15.4,
        "open_incident_ids": ["INC-1", "INC-2"],
        "recommendations": [
            {"priority": "P1", "action": "Scale the DB pool", "why": "pool at 100%"}
        ],
    }
    report.update(overrides)
    return report


class TestWebhookUrl:
    """Env-var URL resolution."""

    def test_unset_returns_none(self, monkeypatch):
        monkeypatch.delenv("SHERLOCK_SLACK_WEBHOOK_URL", raising=False)
        assert get_webhook_url("slack") is None

    def test_non_https_rejected(self, monkeypatch):
        monkeypatch.setenv("SHERLOCK_SLACK_WEBHOOK_URL", "http://insecure.example/hook")
        assert get_webhook_url("slack") is None

    def test_https_accepted(self, monkeypatch):
        monkeypatch.setenv("SHERLOCK_TEAMS_WEBHOOK_URL", TEAMS_URL)
        assert get_webhook_url("teams") == TEAMS_URL

    def test_unknown_target(self):
        assert get_webhook_url("pager") is None


class TestPayloadBuilders:
    """Slack Block Kit / Teams MessageCard formatting."""

    def test_slack_header_and_fields(self):
        payload = build_slack_payload(
            "payment-svc — CRITICAL", "CRITICAL", [("Root cause", "DB pool")], "body"
        )
        assert payload["text"].startswith("🔴")
        assert payload["blocks"][0]["type"] == "header"
        assert "🔴" in payload["blocks"][0]["text"]["text"]
        fields = payload["blocks"][1]["fields"]
        assert fields[0]["text"] == "*Root cause:*\nDB pool"
        assert payload["blocks"][2]["text"]["text"] == "body"

    def test_slack_fields_split_at_ten_per_section(self):
        fields = [(f"f{i}", "v") for i in range(12)]
        payload = build_slack_payload("t", "HEALTHY", fields)
        sections = [b for b in payload["blocks"] if b["type"] == "section"]
        assert len(sections) == 2
        assert len(sections[0]["fields"]) == 10
        assert len(sections[1]["fields"]) == 2

    def test_slack_empty_fields_skipped(self):
        payload = build_slack_payload("t", "HEALTHY", [("Empty", ""), ("Set", "x")])
        fields = payload["blocks"][1]["fields"]
        assert len(fields) == 1

    def test_teams_theme_color_by_severity(self):
        payload = build_teams_payload("t", "WARNING", [("Severity", "WARNING")])
        assert payload["@type"] == "MessageCard"
        assert payload["themeColor"] == "E8A317"
        assert payload["sections"][0]["facts"][0] == {"name": "Severity", "value": "WARNING"}

    def test_long_values_truncated(self):
        payload = build_teams_payload("t", "HEALTHY", [("Big", "x" * 2000)])
        value = payload["sections"][0]["facts"][0]["value"]
        assert len(value) == 500
        assert value.endswith("…")

    def test_report_payload_has_title_and_recommendations(self):
        payload = build_report_payload(_report(), "slack")
        assert "payment-svc-prod" in payload["text"]
        assert "CRITICAL" in payload["text"]
        body = payload["blocks"][-1]["text"]["text"]
        assert "*P1*: Scale the DB pool" in body


class TestPostWebhook:
    """HTTP delivery (mocked)."""

    @respx.mock
    @pytest.mark.asyncio
    async def test_2xx_is_delivered(self):
        respx.post(SLACK_URL).mock(return_value=httpx.Response(200, text="ok"))
        outcome = await post_webhook(SLACK_URL, {"text": "hi"})
        assert outcome == {"delivered": True, "status_code": 200, "error": None}

    @respx.mock
    @pytest.mark.asyncio
    async def test_4xx_is_not_delivered(self):
        respx.post(SLACK_URL).mock(return_value=httpx.Response(403, text="invalid_token"))
        outcome = await post_webhook(SLACK_URL, {"text": "hi"})
        assert outcome["delivered"] is False
        assert outcome["error"] == "HTTP 403"

    @respx.mock
    @pytest.mark.asyncio
    async def test_network_error_never_raises(self):
        respx.post(SLACK_URL).mock(side_effect=httpx.ConnectError("boom"))
        outcome = await post_webhook(SLACK_URL, {"text": "hi"})
        assert outcome["delivered"] is False
        assert "boom" in outcome["error"]

    @pytest.mark.asyncio
    async def test_send_report_without_env_returns_config_error(self, monkeypatch):
        monkeypatch.delenv("SHERLOCK_SLACK_WEBHOOK_URL", raising=False)
        outcome = await send_report(_report(), "slack")
        assert outcome["delivered"] is False
        assert "SHERLOCK_SLACK_WEBHOOK_URL" in outcome["error"]

    @respx.mock
    @pytest.mark.asyncio
    async def test_send_report_posts_formatted_payload(self, monkeypatch):
        monkeypatch.setenv("SHERLOCK_TEAMS_WEBHOOK_URL", TEAMS_URL)
        route = respx.post(TEAMS_URL).mock(return_value=httpx.Response(200))
        outcome = await send_report(_report(), "teams")
        assert outcome["delivered"] is True
        sent = json.loads(route.calls[0].request.content)
        assert sent["@type"] == "MessageCard"
        assert "payment-svc-prod" in sent["title"]


class TestPostReportTool:
    """The post_report_to_webhook MCP tool."""

    def setup_method(self):
        SessionMemory.reset()

    def teardown_method(self):
        SessionMemory.reset()

    def _seed_snapshot(self):
        SessionMemory().record(
            InvestigationSnapshot(
                timestamp=datetime.now(timezone.utc),
                account_id="123456",
                account_name="Test Account",
                service_name="payments-prod/payment-svc-prod",
                bare_name="payment-svc-prod",
                namespace="payments-prod",
                severity="CRITICAL",
                root_cause="DB connection pool exhausted",
                causal_chain="DB pool exhausted → 503s",
                causal_pattern="DB_CASCADE",
                error_rate=15.4,
                is_otel=False,
            )
        )

    @pytest.mark.asyncio
    async def test_no_investigation_yet_gives_hint(self, mock_context, monkeypatch):
        monkeypatch.setenv("SHERLOCK_SLACK_WEBHOOK_URL", SLACK_URL)
        result = await post_report_to_webhook("slack")
        parsed = json.loads(result)
        assert parsed["data_available"] is False
        assert "investigation" in parsed["hint"].lower()

    @respx.mock
    @pytest.mark.asyncio
    async def test_delivers_latest_report(self, mock_context, monkeypatch):
        monkeypatch.setenv("SHERLOCK_SLACK_WEBHOOK_URL", SLACK_URL)
        self._seed_snapshot()
        route = respx.post(SLACK_URL).mock(return_value=httpx.Response(200))

        result = await post_report_to_webhook("slack")
        parsed = json.loads(result)

        assert parsed["delivered"] is True
        assert parsed["severity"] == "CRITICAL"
        assert parsed["service_name"] == "payments-prod/payment-svc-prod"
        sent = json.loads(route.calls[0].request.content)
        assert "DB connection pool exhausted" in json.dumps(sent)

    @pytest.mark.asyncio
    async def test_missing_env_var_returns_hint(self, mock_context, monkeypatch):
        monkeypatch.delenv("SHERLOCK_TEAMS_WEBHOOK_URL", raising=False)
        self._seed_snapshot()

        result = await post_report_to_webhook("teams")
        parsed = json.loads(result)

        assert parsed["delivered"] is False
        assert "SHERLOCK_TEAMS_WEBHOOK_URL" in parsed["hint"]
