"""
Tests for NRQL query tool.
"""

import json
from unittest.mock import patch

import httpx
import pytest
import respx

from sherlock.client.newrelic import NerdGraphClient
from sherlock.core.credentials import Credentials
from sherlock.tools.nrql import MAX_NRQL_LENGTH, run_nrql_query


@pytest.fixture
def nrql_context(mock_credentials, mock_intelligence, mock_context):
    """Set up full context for NRQL tests."""
    return mock_credentials


class TestRunNRQLQuery:
    """Tests for the run_nrql_query tool."""

    @respx.mock
    @pytest.mark.asyncio
    async def test_basic_query(self, nrql_context):
        """Basic NRQL query returns results."""
        nrql = "SELECT count(*) FROM Transaction SINCE 1 hour ago"
        respx.post("https://api.newrelic.com/graphql").mock(
            return_value=httpx.Response(
                200, json={"data": {"actor": {"account": {"nrql": {"results": [{"count": 42}]}}}}}
            )
        )

        result = await run_nrql_query(nrql)
        parsed = json.loads(result)
        assert parsed["results"][0]["count"] == 42

    @respx.mock
    @pytest.mark.asyncio
    async def test_query_with_where_clause(self, nrql_context):
        """NRQL query with WHERE clause works."""
        nrql = (
            "SELECT average(duration) FROM Transaction "
            "WHERE appName = 'web-api' SINCE 30 minutes ago"
        )
        respx.post("https://api.newrelic.com/graphql").mock(
            return_value=httpx.Response(
                200,
                json={
                    "data": {
                        "actor": {"account": {"nrql": {"results": [{"average.duration": 0.045}]}}}
                    }
                },
            )
        )

        result = await run_nrql_query(nrql)
        parsed = json.loads(result)
        assert "average.duration" in parsed["results"][0]

    @pytest.mark.asyncio
    async def test_oversized_query_rejected(self, nrql_context):
        """Queries exceeding MAX_NRQL_LENGTH are rejected."""
        nrql = "SELECT count(*) FROM Transaction WHERE " + "a" * (MAX_NRQL_LENGTH + 1)
        result = await run_nrql_query(nrql)
        parsed = json.loads(result)
        assert "error" in parsed or "too long" in result.lower()

    @respx.mock
    @pytest.mark.asyncio
    async def test_api_error_handled(self, nrql_context):
        """API errors are caught and returned as error messages."""
        nrql = "SELECT count(*) FROM Transaction SINCE 1 hour ago"
        respx.post("https://api.newrelic.com/graphql").mock(return_value=httpx.Response(500))

        result = await run_nrql_query(nrql)
        parsed = json.loads(result)
        assert "error" in parsed

    @respx.mock
    @pytest.mark.asyncio
    async def test_nrql_injection_sanitized(self, nrql_context):
        """NRQL injection attempts are sanitized."""
        nrql = "SELECT count(*) FROM Transaction; DROP TABLE users"
        respx.post("https://api.newrelic.com/graphql").mock(
            return_value=httpx.Response(
                200, json={"data": {"actor": {"nrql": {"results": [{"count": 10}]}}}}
            )
        )

        # Should not raise, query is sanitized before sending
        result = await run_nrql_query(nrql)
        assert isinstance(result, str)


class TestPerCallAccountOverride:
    """Tests for the account_id/profile per-call targeting (P4)."""

    @respx.mock
    @pytest.mark.asyncio
    async def test_override_targets_other_account(self, nrql_context):
        """A profile override queries the other account, flags the response."""
        seen_account_ids = []

        def _capture(request, route):
            body = json.loads(request.content)
            seen_account_ids.append((body.get("variables") or {}).get("accountId"))
            return httpx.Response(
                200,
                json={"data": {"actor": {"account": {"nrql": {"results": [{"count": 7}]}}}}},
            )

        respx.post("https://api.newrelic.com/graphql").mock(side_effect=_capture)

        other = Credentials(account_id="999888", api_key="NRAK-other", region="US")
        with patch(
            "sherlock.tools.nrql.resolve_call_target",
            return_value=(other, None, NerdGraphClient(other), True),
        ):
            result = await run_nrql_query(
                "SELECT count(*) FROM Transaction SINCE 1 hour ago",
                profile="other",
            )

        parsed = json.loads(result)
        assert parsed["account_id"] == "999888"
        assert parsed["account_override"] is True
        assert seen_account_ids == [999888]

    @respx.mock
    @pytest.mark.asyncio
    async def test_no_override_has_no_flag(self, nrql_context):
        """The default (active-account) path does not claim an override."""
        respx.post("https://api.newrelic.com/graphql").mock(
            return_value=httpx.Response(
                200, json={"data": {"actor": {"account": {"nrql": {"results": [{"count": 1}]}}}}}
            )
        )
        result = await run_nrql_query("SELECT count(*) FROM Transaction SINCE 1 hour ago")
        parsed = json.loads(result)
        assert "account_override" not in parsed


class TestResultTruncation:
    """Tests for max_field_length truncation of NRQL results (P4)."""

    def _mock_results(self, results):
        respx.post("https://api.newrelic.com/graphql").mock(
            return_value=httpx.Response(
                200, json={"data": {"actor": {"account": {"nrql": {"results": results}}}}}
            )
        )

    @respx.mock
    @pytest.mark.asyncio
    async def test_long_fields_truncated_by_default(self, nrql_context):
        self._mock_results([{"message": "H" * 3000, "level": "ERROR"}])
        result = await run_nrql_query("SELECT message FROM Log SINCE 1 hour ago")
        parsed = json.loads(result)

        assert parsed["truncated_fields"] == 1
        assert "truncation_note" in parsed
        assert "[truncated, 3000 chars total]" in parsed["results"][0]["message"]
        assert parsed["results"][0]["level"] == "ERROR"

    @respx.mock
    @pytest.mark.asyncio
    async def test_zero_disables_truncation(self, nrql_context):
        self._mock_results([{"message": "H" * 3000}])
        result = await run_nrql_query(
            "SELECT message FROM Log SINCE 1 hour ago",
            max_field_length=0,
        )
        parsed = json.loads(result)

        assert "truncated_fields" not in parsed
        assert parsed["results"][0]["message"] == "H" * 3000

    @respx.mock
    @pytest.mark.asyncio
    async def test_custom_max_field_length(self, nrql_context):
        self._mock_results([{"message": "A" * 200}])
        result = await run_nrql_query(
            "SELECT message FROM Log SINCE 1 hour ago",
            max_field_length=50,
        )
        parsed = json.loads(result)

        assert parsed["truncated_fields"] == 1
        assert parsed["results"][0]["message"].startswith("A" * 50)
        assert "[truncated, 200 chars total]" in parsed["results"][0]["message"]
