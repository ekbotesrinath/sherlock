"""
Tests for the distributed-trace tools (get_slow_traces / summarize_trace).
"""

import json

import httpx
import pytest
import respx

from sherlock.tools.traces import (
    compute_exclusive_times,
    find_hot_span,
    get_slow_traces,
    summarize_services,
    summarize_trace,
    trace_duration_ms,
)

BASE_MS = 1_750_000_000_000
TRACE_ID = "a1b2c3d4e5f60718a1b2c3d4e5f60718"


def _mock_nrql_response(results):
    return {"data": {"actor": {"account": {"nrql": {"results": results}}}}}


def _span(
    span_id,
    parent_id=None,
    name="span",
    duration_ms=100.0,
    app="payment-svc-prod",
    error_class=None,
    otel_status=None,
    ts=BASE_MS,
):
    return {
        "id": span_id,
        "parent_id": parent_id,
        "name": name,
        "duration_ms": duration_ms,
        "timestamp": ts,
        "appName": app,
        "entity_name": None,
        "otel_service": None,
        "error_class": error_class,
        "error_message": "boom" if error_class else None,
        "otel_status": otel_status,
        "span_kind": "server" if parent_id is None else "client",
    }


def _sample_trace():
    """Root (2500ms) → db query (1800ms self) + downstream call (500ms)."""
    return [
        _span("root", None, "WebTransaction/orders", 2500.0),
        _span("db", "root", "SELECT orders", 1800.0),
        _span("http", "root", "External/inventory-svc", 500.0, app="inventory-svc-prod"),
        _span(
            "err",
            "http",
            "InventoryLookup",
            400.0,
            app="inventory-svc-prod",
            error_class="TimeoutError",
        ),
    ]


class TestExclusiveTimes:
    """Unit tests for self-time computation."""

    def test_parent_excludes_direct_children(self):
        spans = compute_exclusive_times(_sample_trace())
        by_id = {s["id"]: s for s in spans}
        # root 2500 - (1800 + 500) = 200; db has no children → 1800.
        assert by_id["root"]["exclusive_ms"] == 200.0
        assert by_id["db"]["exclusive_ms"] == 1800.0
        assert by_id["http"]["exclusive_ms"] == 100.0

    def test_overlapping_children_clamped_to_zero(self):
        spans = compute_exclusive_times(
            [
                _span("a", None, duration_ms=100.0),
                _span("b", "a", duration_ms=80.0),
                _span("c", "a", duration_ms=80.0),  # async siblings overlap
            ]
        )
        root = next(s for s in spans if s["id"] == "a")
        assert root["exclusive_ms"] == 0.0

    def test_missing_durations_treated_as_zero(self):
        spans = compute_exclusive_times([_span("a", None, duration_ms=None)])
        assert spans[0]["exclusive_ms"] == 0.0

    def test_hot_span_is_largest_self_time(self):
        spans = compute_exclusive_times(_sample_trace())
        hot = find_hot_span(spans)
        assert hot["id"] == "db"

    def test_hot_span_empty_list(self):
        assert find_hot_span([]) is None


class TestServiceBreakdown:
    """Unit tests for the per-service summary."""

    def test_grouped_and_sorted_hottest_first(self):
        spans = compute_exclusive_times(_sample_trace())
        breakdown = summarize_services(spans, 2500.0)
        assert breakdown[0]["service"] == "payment-svc-prod"
        assert breakdown[0]["exclusive_ms"] == 2000.0  # 200 root + 1800 db
        assert breakdown[0]["pct_of_trace"] == 80.0
        assert breakdown[1]["service"] == "inventory-svc-prod"
        assert breakdown[1]["span_count"] == 2

    def test_zero_duration_gives_zero_pct(self):
        spans = compute_exclusive_times([_span("a", None, duration_ms=50.0)])
        breakdown = summarize_services(spans, 0.0)
        assert breakdown[0]["pct_of_trace"] == 0.0


class TestTraceDuration:
    """Unit tests for trace-duration derivation."""

    def test_uses_root_span_duration(self):
        total, root = trace_duration_ms(_sample_trace())
        assert total == 2500.0
        assert root["id"] == "root"

    def test_no_root_falls_back_to_envelope(self):
        spans = [
            _span("a", "missing-parent", duration_ms=100.0, ts=BASE_MS),
            _span("b", "missing-parent", duration_ms=200.0, ts=BASE_MS + 300),
        ]
        total, root = trace_duration_ms(spans)
        assert root is None
        assert total == 500.0  # (BASE+300+200) - BASE

    def test_empty_spans(self):
        total, root = trace_duration_ms([])
        assert total == 0.0
        assert root is None


class TestGetSlowTraces:
    """Integration tests (mocked NerdGraph)."""

    def _route(self, trace_rows, pct_rows):
        def _respond(request):
            body = request.content.decode()
            if "FACET trace.id" in body:
                return httpx.Response(200, json=_mock_nrql_response(trace_rows))
            if "percentile" in body:
                return httpx.Response(200, json=_mock_nrql_response(pct_rows))
            return httpx.Response(200, json=_mock_nrql_response([]))

        return _respond

    def _trace_row(self, trace_id=TRACE_ID, slowest=4200.0):
        return {
            "facet": trace_id,
            "trace.id": trace_id,
            "slowest_span_ms": slowest,
            "total_span_ms": slowest * 1.5,
            "span_count": 12,
            "last_seen": BASE_MS,
            "slowest_seen_in": "WebTransaction/orders",
        }

    def _pct_row(self, p50=100.0, p95=500.0, p99=900.0):
        return {"percentile.duration.ms": {"50": p50, "95": p95, "99": p99}}

    @respx.mock
    @pytest.mark.asyncio
    async def test_slow_traces_with_percentile_context(self, mock_context):
        respx.post("https://api.newrelic.com/graphql").mock(
            side_effect=self._route([self._trace_row()], [self._pct_row()])
        )

        result = await get_slow_traces("payment-svc-prod")
        parsed = json.loads(result)

        assert parsed["trace_count"] == 1
        assert parsed["traces"][0]["trace_id"] == TRACE_ID
        assert parsed["traces"][0]["vs_p95"] == "8.4x"
        assert parsed["span_duration_percentiles_ms"] == {"p50": 100.0, "p95": 500.0, "p99": 900.0}
        assert "8.4x the service's p95" in parsed["verdict"]
        assert TRACE_ID in parsed["verdict"]

    @respx.mock
    @pytest.mark.asyncio
    async def test_only_errors_adds_filter(self, mock_context):
        respx.post("https://api.newrelic.com/graphql").mock(side_effect=self._route([], []))

        result = await get_slow_traces("payment-svc-prod", only_errors=True)
        parsed = json.loads(result)
        assert "error.class IS NOT NULL" in parsed["nrql_used"]["slow_traces"]

    @respx.mock
    @pytest.mark.asyncio
    async def test_fuzzy_resolution(self, mock_context):
        respx.post("https://api.newrelic.com/graphql").mock(side_effect=self._route([], []))

        result = await get_slow_traces("payment-svc")
        parsed = json.loads(result)
        assert parsed["service_name"] == "payment-svc-prod"
        assert parsed["resolved_from"] == "payment-svc"

    @respx.mock
    @pytest.mark.asyncio
    async def test_limit_clamped(self, mock_context):
        respx.post("https://api.newrelic.com/graphql").mock(side_effect=self._route([], []))

        result = await get_slow_traces("payment-svc-prod", limit=500)
        parsed = json.loads(result)
        assert "LIMIT 50" in parsed["nrql_used"]["slow_traces"]

    @respx.mock
    @pytest.mark.asyncio
    async def test_no_spans_gives_hint_verdict(self, mock_context):
        respx.post("https://api.newrelic.com/graphql").mock(side_effect=self._route([], []))

        result = await get_slow_traces("payment-svc-prod")
        parsed = json.loads(result)
        assert parsed["trace_count"] == 0
        assert "No spans found" in parsed["verdict"]

    @respx.mock
    @pytest.mark.asyncio
    async def test_api_errors_degrade_gracefully(self, mock_context):
        respx.post("https://api.newrelic.com/graphql").mock(return_value=httpx.Response(500))

        result = await get_slow_traces("payment-svc-prod")
        parsed = json.loads(result)
        assert "error" not in parsed
        assert parsed["trace_count"] == 0


class TestSummarizeTrace:
    """Integration tests (mocked NerdGraph)."""

    def _nrql_rows(self):
        rows = []
        for s in _sample_trace():
            row = dict(s)
            # NerdGraph returns the aliased column names used in the NRQL.
            rows.append(row)
        return rows

    @respx.mock
    @pytest.mark.asyncio
    async def test_identifies_hot_span_and_errors(self, mock_context):
        respx.post("https://api.newrelic.com/graphql").mock(
            return_value=httpx.Response(200, json=_mock_nrql_response(self._nrql_rows()))
        )

        result = await summarize_trace(TRACE_ID)
        parsed = json.loads(result)

        assert parsed["trace_duration_ms"] == 2500.0
        assert parsed["span_count"] == 4
        assert parsed["hot_span"]["name"] == "SELECT orders"
        assert parsed["hot_span"]["exclusive_ms"] == 1800.0
        assert parsed["root_span"]["name"] == "WebTransaction/orders"
        assert parsed["services"][0]["service"] == "payment-svc-prod"
        assert len(parsed["error_spans"]) == 1
        assert parsed["error_spans"][0]["error"] == "TimeoutError"
        assert any("hot span" in v for v in parsed["verdicts"])
        assert any("errored span" in v for v in parsed["verdicts"])

    @pytest.mark.asyncio
    async def test_invalid_trace_id_rejected_without_query(self, mock_context):
        result = await summarize_trace("'; DROP TABLE spans; --")
        parsed = json.loads(result)
        assert "invalid trace_id" in parsed["error"]
        assert parsed["data_available"] is False

    @respx.mock
    @pytest.mark.asyncio
    async def test_no_spans_returns_hint(self, mock_context):
        respx.post("https://api.newrelic.com/graphql").mock(
            return_value=httpx.Response(200, json=_mock_nrql_response([]))
        )

        result = await summarize_trace(TRACE_ID)
        parsed = json.loads(result)
        assert "no spans found" in parsed["error"]
        assert "since_minutes" in parsed["hint"]

    @respx.mock
    @pytest.mark.asyncio
    async def test_otel_status_counts_as_error(self, mock_context):
        rows = [
            _span("root", None, "GET /orders", 300.0),
            _span("bad", "root", "downstream", 100.0, otel_status="ERROR"),
        ]
        respx.post("https://api.newrelic.com/graphql").mock(
            return_value=httpx.Response(200, json=_mock_nrql_response(rows))
        )

        result = await summarize_trace(TRACE_ID)
        parsed = json.loads(result)
        assert parsed["error_spans"][0]["error"] == "OTel status ERROR"
