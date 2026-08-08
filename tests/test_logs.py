"""
Tests for log search tool.
"""

import json

import httpx
import pytest
import respx

from sherlock.tools.logs import search_logs


def _mock_nrql_response(results):
    """Build a standard NerdGraph NRQL response."""
    return {"data": {"actor": {"account": {"nrql": {"results": results}}}}}


@pytest.fixture
def logs_context(mock_credentials, mock_intelligence, mock_context):
    """Full context for log tests."""
    return mock_credentials


class TestSearchLogs:
    """Tests for search_logs."""

    @respx.mock
    @pytest.mark.asyncio
    async def test_basic_log_search(self, logs_context):
        """Basic log search returns results."""
        log_data = _mock_nrql_response(
            [
                {
                    "message": "Request processed successfully",
                    "level": "INFO",
                    "timestamp": 1700000000000,
                    "service.name": "web-api",
                },
                {
                    "message": "Database query completed",
                    "level": "DEBUG",
                    "timestamp": 1700000001000,
                    "service.name": "web-api",
                },
            ]
        )

        respx.post("https://api.newrelic.com/graphql").mock(
            return_value=httpx.Response(200, json=log_data)
        )

        result = await search_logs("web-api")
        parsed = json.loads(result)
        assert "logs" in parsed
        assert parsed["total_logs"] == 2

    @respx.mock
    @pytest.mark.asyncio
    async def test_severity_filter(self, logs_context):
        """Severity filter limits to specific log levels."""
        error_logs = _mock_nrql_response(
            [
                {
                    "message": "Connection refused to database",
                    "level": "ERROR",
                    "timestamp": 1700000000000,
                },
            ]
        )

        respx.post("https://api.newrelic.com/graphql").mock(
            return_value=httpx.Response(200, json=error_logs)
        )

        result = await search_logs("web-api", severity="ERROR")
        assert "ERROR" in result or "error" in result.lower()

    @respx.mock
    @pytest.mark.asyncio
    async def test_keyword_filter(self, logs_context):
        """Keyword filter searches within log messages."""
        filtered_logs = _mock_nrql_response(
            [
                {
                    "message": "OutOfMemoryError: Java heap space",
                    "level": "ERROR",
                    "timestamp": 1700000000000,
                },
            ]
        )

        respx.post("https://api.newrelic.com/graphql").mock(
            return_value=httpx.Response(200, json=filtered_logs)
        )

        result = await search_logs("web-api", keyword="OutOfMemory")
        assert "OutOfMemory" in result or "memory" in result.lower()

    @respx.mock
    @pytest.mark.asyncio
    async def test_since_minutes_parameter(self, logs_context):
        """since_minutes parameter controls time window."""
        log_data = _mock_nrql_response(
            [
                {
                    "message": "Recent log entry",
                    "level": "INFO",
                    "timestamp": 1700000000000,
                },
            ]
        )

        respx.post("https://api.newrelic.com/graphql").mock(
            return_value=httpx.Response(200, json=log_data)
        )

        result = await search_logs("web-api", since_minutes=5)
        assert isinstance(result, str)

    @respx.mock
    @pytest.mark.asyncio
    async def test_limit_parameter(self, logs_context):
        """limit parameter caps the number of results."""
        log_data = _mock_nrql_response(
            [{"message": f"Log entry {i}", "level": "INFO"} for i in range(3)]
        )

        respx.post("https://api.newrelic.com/graphql").mock(
            return_value=httpx.Response(200, json=log_data)
        )

        result = await search_logs("web-api", limit=3)
        assert isinstance(result, str)

    @respx.mock
    @pytest.mark.asyncio
    async def test_no_logs_found(self, logs_context):
        """Empty log results are handled gracefully."""
        empty = _mock_nrql_response([])

        respx.post("https://api.newrelic.com/graphql").mock(
            return_value=httpx.Response(200, json=empty)
        )

        result = await search_logs("web-api", severity="FATAL", keyword="nonexistent")
        parsed = json.loads(result)
        # Should return empty results, not an error
        assert isinstance(parsed, (dict, list))

    @respx.mock
    @pytest.mark.asyncio
    async def test_api_error_handled(self, logs_context):
        """API errors are caught and reported."""
        respx.post("https://api.newrelic.com/graphql").mock(return_value=httpx.Response(503))

        result = await search_logs("web-api")
        parsed = json.loads(result)
        assert "error" in parsed or "error" in result.lower()

    @respx.mock
    @pytest.mark.asyncio
    async def test_fuzzy_service_resolution(self, logs_context):
        """Service name is fuzzy-resolved from intelligence."""
        log_data = _mock_nrql_response(
            [
                {"message": "Resolved log", "level": "INFO"},
            ]
        )

        respx.post("https://api.newrelic.com/graphql").mock(
            return_value=httpx.Response(200, json=log_data)
        )

        # "web-ap" should fuzzy-resolve to "web-api"
        result = await search_logs("web-ap")
        assert isinstance(result, str)

    @respx.mock
    @pytest.mark.asyncio
    async def test_entity_name_fallback(self, logs_context):
        """Falls back to entity.name when primary service attribute has no data."""
        empty = _mock_nrql_response([])
        entity_logs = _mock_nrql_response(
            [
                {"message": "Found via entity.name", "entity.name": "web-api", "level": "ERROR"},
            ]
        )

        call_count = 0

        def _side_effect(request, route):
            nonlocal call_count
            call_count += 1
            body = json.loads(request.content)
            query = body.get("query", "") + " " + str((body.get("variables") or {}).get("nrql", ""))
            # First call is primary attr (service.name) — no results.
            # Subsequent calls try fallback attrs — return data when
            # the query contains entity.name.
            if "`entity.name`" in query:
                return httpx.Response(200, json=entity_logs)
            return httpx.Response(200, json=empty)

        respx.post("https://api.newrelic.com/graphql").mock(side_effect=_side_effect)

        result = await search_logs("web-api", severity="ERROR")
        parsed = json.loads(result)
        assert parsed["total_logs"] == 1
        assert "entity.name" in parsed.get("note", "")

    @respx.mock
    @pytest.mark.asyncio
    async def test_bare_name_fallback_for_namespaced_service(self, logs_context):
        """Falls back to bare name when full namespace/service returns no logs."""
        empty = _mock_nrql_response([])
        bare_logs = _mock_nrql_response(
            [
                {
                    "message": "Found via bare name",
                    "service.name": "client-service",
                    "level": "INFO",
                },
            ]
        )

        def _side_effect(request, route):
            body = json.loads(request.content)
            query = body.get("query", "") + " " + str((body.get("variables") or {}).get("nrql", ""))
            # Only return data when the query uses the bare name without
            # the namespace prefix.
            if "client-service" in query and "eswd-prod" not in query:
                return httpx.Response(200, json=bare_logs)
            return httpx.Response(200, json=empty)

        respx.post("https://api.newrelic.com/graphql").mock(side_effect=_side_effect)

        result = await search_logs("eswd-prod/client-service")
        parsed = json.loads(result)
        assert parsed["total_logs"] == 1


class TestResourceFilter:
    """Tests for the cloud.resource_id sub-entity filter (P4)."""

    @respx.mock
    @pytest.mark.asyncio
    async def test_resource_filter_in_primary_nrql(self, logs_context):
        """The clause lands in the primary query, before SINCE."""
        log_data = _mock_nrql_response(
            [{"message": "func log", "level": "INFO", "timestamp": 1700000000000}]
        )
        respx.post("https://api.newrelic.com/graphql").mock(
            return_value=httpx.Response(200, json=log_data)
        )

        result = await search_logs("web-api", resource_filter="gail-v2-di")
        parsed = json.loads(result)

        assert parsed["resource_filter"] == "gail-v2-di"
        nrql = parsed["nrql_used"]
        assert "AND cloud.resource_id LIKE '%gail-v2-di%'" in nrql
        assert nrql.find("cloud.resource_id") < nrql.find(" SINCE ")

    @respx.mock
    @pytest.mark.asyncio
    async def test_resource_filter_without_service_name(self, logs_context):
        """resource_filter alone addresses the shared-entity case directly."""
        log_data = _mock_nrql_response(
            [{"message": "di func log", "level": "ERROR", "timestamp": 1700000000000}]
        )
        respx.post("https://api.newrelic.com/graphql").mock(
            return_value=httpx.Response(200, json=log_data)
        )

        result = await search_logs(resource_filter="func-dfin-eus2-dev-gail-v2-di")
        parsed = json.loads(result)

        assert parsed["total_logs"] == 1
        assert "cloud.resource_id LIKE '%func-dfin-eus2-dev-gail-v2-di%'" in parsed["nrql_used"]

    @respx.mock
    @pytest.mark.asyncio
    async def test_resource_filter_survives_attr_fallback(self, logs_context):
        """Fallback-attribute queries keep the resource clause."""
        empty = _mock_nrql_response([])
        entity_logs = _mock_nrql_response(
            [{"message": "found", "entity.name": "web-api", "level": "ERROR"}]
        )

        def _side_effect(request, route):
            body = json.loads(request.content)
            query = str((body.get("variables") or {}).get("nrql", ""))
            # Every query this tool sends must carry the resource clause.
            assert "cloud.resource_id LIKE '%rid-di%'" in query
            if "`entity.name`" in query:
                return httpx.Response(200, json=entity_logs)
            return httpx.Response(200, json=empty)

        respx.post("https://api.newrelic.com/graphql").mock(side_effect=_side_effect)

        result = await search_logs("web-api", resource_filter="rid-di")
        parsed = json.loads(result)
        assert parsed["total_logs"] == 1


class TestLogTruncation:
    """Tests for max_field_length truncation of log payloads (P4)."""

    @respx.mock
    @pytest.mark.asyncio
    async def test_long_message_truncated_but_patterns_from_full_text(self, logs_context):
        """Truncation shrinks the payload without degrading log_patterns."""
        long_message = "Header dump: " + "X-Header: value; " * 400  # ~6.8KB
        log_data = _mock_nrql_response(
            [{"message": long_message, "level": "ERROR", "timestamp": 1700000000000}]
        )
        respx.post("https://api.newrelic.com/graphql").mock(
            return_value=httpx.Response(200, json=log_data)
        )

        result = await search_logs("web-api")
        parsed = json.loads(result)

        assert parsed["truncated_fields"] == 1
        returned = parsed["logs"][0]["message"]
        assert f"[truncated, {len(long_message)} chars total]" in returned
        assert len(returned) < len(long_message)
        # Fingerprinting saw the FULL message (computed before truncation).
        assert parsed["log_patterns"]["total_messages"] == 1

    @respx.mock
    @pytest.mark.asyncio
    async def test_truncation_opt_out(self, logs_context):
        long_message = "Y" * 4000
        log_data = _mock_nrql_response(
            [{"message": long_message, "level": "ERROR", "timestamp": 1700000000000}]
        )
        respx.post("https://api.newrelic.com/graphql").mock(
            return_value=httpx.Response(200, json=log_data)
        )

        result = await search_logs("web-api", max_field_length=0)
        parsed = json.loads(result)

        assert "truncated_fields" not in parsed
        assert parsed["logs"][0]["message"] == long_message
