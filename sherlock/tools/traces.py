"""
Distributed-trace tools for Sherlock.

Two tools that turn New Relic ``Span`` events into answers:

- ``get_slow_traces`` — find the slowest distributed traces touching a
  service (APM agent or OTel), with percentile context ("4.2s = 8x p95").
- ``summarize_trace`` — fetch every span of one trace and identify the
  hot span (largest self-time), per-service time breakdown, and errored
  spans, so the agent can say WHERE the time went without eyeballing
  hundreds of spans.
"""

import asyncio
import json
import logging
import re
import time
from datetime import datetime, timezone

from sherlock.client.newrelic import get_client
from sherlock.core.context import AccountContext
from sherlock.core.nrql_executor import execute_nrql
from sherlock.core.sanitize import (
    apply_resource_filter,
    check_env_mismatch,
    fuzzy_resolve_service,
    sanitize_service_name,
)

logger = logging.getLogger("sherlock.tools.traces")

# Trace ids are hex (W3C traceparent) or GUID-ish; allow only chars that
# cannot break out of an NRQL string literal.
TRACE_ID_RE = re.compile(r"^[0-9a-fA-F\-]{8,64}$")

MAX_TRACE_LIMIT = 50

# ── NRQL templates ───────────────────────────────────────────────────────

# Slowest traces touching the service. FACET queries sort by the first
# aggregate descending, so max(duration.ms) first = slowest trace first.
# Spans are matched on appName (APM agent) OR entity.name (OTel) so the
# tool works for both instrumentation types without a probe query.
NRQL_SLOW_TRACES = (
    "SELECT max(duration.ms) as slowest_span_ms, "
    "sum(duration.ms) as total_span_ms, "
    "count(*) as span_count, "
    "latest(timestamp) as last_seen, "
    "latest(name) as slowest_seen_in "
    "FROM Span WHERE (appName = '%s' OR entity.name = '%s') "
    "%s"  # optional error filter
    "SINCE %d minutes ago FACET trace.id LIMIT %d"
)

TRACE_ERROR_FILTER = "AND (error.class IS NOT NULL OR otel.status_code = 'ERROR') "

NRQL_SPAN_PERCENTILES = (
    "SELECT percentile(duration.ms, 50, 95, 99) "
    "FROM Span WHERE (appName = '%s' OR entity.name = '%s') "
    "SINCE %d minutes ago"
)

# Every span of one trace. LIMIT MAX = 2000 rows, enough for all but
# pathological traces (span_count in the response exposes truncation).
NRQL_TRACE_SPANS = (
    "SELECT id, `parent.id` as parent_id, name, "
    "`duration.ms` as duration_ms, timestamp, "
    "appName, `entity.name` as entity_name, `service.name` as otel_service, "
    "`error.class` as error_class, `error.message` as error_message, "
    "`otel.status_code` as otel_status, `span.kind` as span_kind "
    "FROM Span WHERE trace.id = '%s' "
    "SINCE %d minutes ago LIMIT MAX"
)


def _iso(timestamp_ms: float | int | None) -> str:
    """Millisecond epoch → ISO-8601 UTC string (empty on bad input)."""
    try:
        if not timestamp_ms:
            return ""
        return datetime.fromtimestamp(float(timestamp_ms) / 1000, tz=timezone.utc).isoformat()
    except Exception:
        return ""


def _span_service(span: dict) -> str:
    """Best-available service name for a span row."""
    return span.get("appName") or span.get("entity_name") or span.get("otel_service") or "unknown"


def _span_errored(span: dict) -> bool:
    """True when the span carries an APM or OTel error signal."""
    return bool(span.get("error_class")) or span.get("otel_status") == "ERROR"


def compute_exclusive_times(spans: list[dict]) -> list[dict]:
    """Annotate spans with self-time (``exclusive_ms``).

    A span's exclusive time is its duration minus the summed durations of
    its DIRECT children — the time spent in the span's own code rather
    than waiting on callees. Clamped at 0 for async/overlapping children.

    Args:
        spans: Raw span rows with id / parent_id / duration_ms.

    Returns:
        New list of span dicts, each with an added ``exclusive_ms`` key.
    """
    children_ms: dict[str, float] = {}
    for span in spans:
        parent = span.get("parent_id")
        if parent:
            children_ms[parent] = children_ms.get(parent, 0.0) + float(
                span.get("duration_ms") or 0.0
            )

    annotated = []
    for span in spans:
        entry = dict(span)
        own = float(span.get("duration_ms") or 0.0)
        entry["exclusive_ms"] = round(max(0.0, own - children_ms.get(span.get("id", ""), 0.0)), 2)
        annotated.append(entry)
    return annotated


def find_hot_span(spans: list[dict]) -> dict | None:
    """The span with the largest self-time (must have ``exclusive_ms``)."""
    best: dict | None = None
    for span in spans:
        if best is None or span.get("exclusive_ms", 0.0) > best.get("exclusive_ms", 0.0):
            best = span
    return best


def summarize_services(spans: list[dict], trace_duration_ms: float) -> list[dict]:
    """Per-service breakdown by self-time, sorted hottest first.

    Args:
        spans: Spans annotated with ``exclusive_ms``.
        trace_duration_ms: Total trace duration for percentage math.

    Returns:
        List of {service, span_count, exclusive_ms, pct_of_trace}.
    """
    by_service: dict[str, dict] = {}
    for span in spans:
        service = _span_service(span)
        bucket = by_service.setdefault(
            service, {"service": service, "span_count": 0, "exclusive_ms": 0.0}
        )
        bucket["span_count"] += 1
        bucket["exclusive_ms"] += span.get("exclusive_ms", 0.0)

    breakdown = sorted(by_service.values(), key=lambda b: b["exclusive_ms"], reverse=True)
    for bucket in breakdown:
        bucket["exclusive_ms"] = round(bucket["exclusive_ms"], 2)
        bucket["pct_of_trace"] = (
            round(100.0 * bucket["exclusive_ms"] / trace_duration_ms, 1)
            if trace_duration_ms > 0
            else 0.0
        )
    return breakdown


def trace_duration_ms(spans: list[dict]) -> tuple[float, dict | None]:
    """Total trace duration and the root span (if captured).

    Uses the root span's duration when a root exists; otherwise falls
    back to the wall-clock envelope of all spans (max end − min start).

    Args:
        spans: Raw span rows.

    Returns:
        (duration_ms, root_span_or_None).
    """
    root = next((s for s in spans if not s.get("parent_id")), None)
    if root and root.get("duration_ms"):
        return float(root["duration_ms"]), root

    starts = [float(s["timestamp"]) for s in spans if s.get("timestamp")]
    ends = [
        float(s["timestamp"]) + float(s.get("duration_ms") or 0.0)
        for s in spans
        if s.get("timestamp")
    ]
    if starts and ends:
        return round(max(ends) - min(starts), 2), root
    return 0.0, root


def _span_view(span: dict) -> dict:
    """Trim a span row to the fields worth returning to the agent."""
    view = {
        "name": span.get("name", ""),
        "service": _span_service(span),
        "duration_ms": span.get("duration_ms"),
        "exclusive_ms": span.get("exclusive_ms"),
        "span_kind": span.get("span_kind") or "",
        "time": _iso(span.get("timestamp")),
    }
    if _span_errored(span):
        view["error"] = span.get("error_class") or "OTel status ERROR"
        if span.get("error_message"):
            view["error_message"] = span["error_message"]
    return view


async def get_slow_traces(
    service_name: str,
    since_minutes: int = 60,
    limit: int = 10,
    only_errors: bool = False,
    resource_filter: str | None = None,
) -> str:
    """Find the slowest distributed traces touching an APM/OTel service.

    Each returned trace id can be fed to ``summarize_trace`` to see where
    the time went. Includes the service's span-duration percentiles so
    slowness is relative ("4.2s = 8x the p95"), not absolute.

    Args:
        service_name: Service to search traces for (fuzzy resolved).
        since_minutes: Time window in minutes (default 60).
        limit: Max traces to return (default 10, capped at 50).
        only_errors: Only return traces containing an errored span.
        resource_filter: Optional ``cloud.resource_id`` substring filter.
            Use when multiple deployed apps (e.g. Azure Function Apps)
            share one New Relic entity name — scopes trace search and
            percentiles to the matching sub-resource.

    Returns:
        JSON string with the slowest traces and percentile context.
    """
    start = time.time()
    try:
        ctx = AccountContext()
        credentials, intelligence = ctx.get_active()
        client = get_client()

        safe_name = sanitize_service_name(service_name)
        candidates = list(intelligence.apm.service_names) + [
            name
            for name in getattr(intelligence.otel, "service_names", [])
            if name not in intelligence.apm.service_names
        ]
        try:
            resolved_name, was_fuzzy, _ = fuzzy_resolve_service(
                safe_name,
                candidates,
                threshold=0.5,
                naming_convention=intelligence.naming_convention,
            )
        except Exception:
            resolved_name, was_fuzzy = safe_name, False

        limit = max(1, min(int(limit), MAX_TRACE_LIMIT))
        error_filter = TRACE_ERROR_FILTER if only_errors else ""
        traces_nrql = apply_resource_filter(
            NRQL_SLOW_TRACES
            % (
                resolved_name,
                resolved_name,
                error_filter,
                since_minutes,
                limit,
            ),
            resource_filter,
        )
        pct_nrql = apply_resource_filter(
            NRQL_SPAN_PERCENTILES % (resolved_name, resolved_name, since_minutes),
            resource_filter,
        )

        async def _run(nrql_str: str) -> list:
            outcome = await execute_nrql(
                nrql_str,
                account_id=credentials.account_id,
                client=client,
                timeout_override=20,
            )
            return outcome.results

        gathered = await asyncio.gather(_run(traces_nrql), _run(pct_nrql), return_exceptions=True)
        trace_rows = gathered[0] if not isinstance(gathered[0], BaseException) else []
        pct_rows = gathered[1] if not isinstance(gathered[1], BaseException) else []

        # percentile() rows come back as {"percentile.duration.ms": {"50": ..}}.
        percentiles: dict[str, float] = {}
        if pct_rows and isinstance(pct_rows[0], dict):
            for value in pct_rows[0].values():
                if isinstance(value, dict):
                    for pct_key, pct_val in value.items():
                        if pct_val is not None:
                            percentiles[f"p{pct_key}"] = round(float(pct_val), 2)

        p95 = percentiles.get("p95") or 0.0
        traces = []
        for row in trace_rows:
            if not isinstance(row, dict):
                continue
            slowest = float(row.get("slowest_span_ms") or 0.0)
            entry = {
                "trace_id": row.get("trace.id") or row.get("facet"),
                "slowest_span_ms": round(slowest, 2),
                "total_span_ms": round(float(row.get("total_span_ms") or 0.0), 2),
                "span_count": row.get("span_count", 0),
                "slowest_seen_in": row.get("slowest_seen_in", ""),
                "last_seen": _iso(row.get("last_seen")),
            }
            if p95 > 0:
                entry["vs_p95"] = f"{slowest / p95:.1f}x"
            traces.append(entry)

        verdict = None
        if traces and p95 > 0:
            worst = traces[0]
            verdict = (
                f"🐌 slowest trace has a {worst['slowest_span_ms']:g}ms span — "
                f"{worst['slowest_span_ms'] / p95:.1f}x the service's p95 "
                f"({p95:g}ms). Run summarize_trace on "
                f"{worst['trace_id']} to find the hot span."
            )
        elif not traces:
            verdict = (
                "No spans found for this service in the window — the service "
                "may not emit distributed traces, or try a larger since_minutes."
            )

        response: dict = {
            "service_name": resolved_name,
            "since_minutes": since_minutes,
            "only_errors": only_errors,
            **({"resource_filter": resource_filter} if resource_filter else {}),
            "trace_count": len(traces),
            "traces": traces,
            "span_duration_percentiles_ms": percentiles,
            "verdict": verdict,
            "nrql_used": {"slow_traces": traces_nrql, "percentiles": pct_nrql},
            "duration_ms": int((time.time() - start) * 1000),
        }

        if was_fuzzy and resolved_name:
            response["resolved_from"] = service_name
            response["note"] = f"Fuzzy matched '{service_name}' → '{resolved_name}'"
            env_warn = check_env_mismatch(
                service_name, resolved_name, intelligence.naming_convention
            )
            if env_warn:
                response["warnings"] = [env_warn]

        return json.dumps(response)

    except Exception as exc:
        return json.dumps(
            {
                "error": str(exc),
                "tool": "get_slow_traces",
                "hint": "Ensure you are connected and the service name is valid.",
                "data_available": False,
            }
        )


async def summarize_trace(trace_id: str, since_minutes: int = 1440) -> str:
    """Summarize one distributed trace and identify the hot span.

    Fetches every span of the trace and computes self-time per span
    (duration minus direct children), so the answer is "the time went to
    THIS span" rather than a wall of raw spans. Also returns a
    per-service breakdown and any errored spans.

    Args:
        trace_id: The trace id (from get_slow_traces or a NR deep link).
        since_minutes: How far back to look for the trace's spans
            (default 24 hours — must cover the trace's timestamp).

    Returns:
        JSON string with trace duration, hot span, per-service breakdown,
        and errored spans.
    """
    start = time.time()
    try:
        trace_id = (trace_id or "").strip()
        if not TRACE_ID_RE.match(trace_id):
            return json.dumps(
                {
                    "error": f"invalid trace_id: {trace_id!r}",
                    "tool": "summarize_trace",
                    "hint": "Pass a trace id from get_slow_traces (hex string).",
                    "data_available": False,
                }
            )

        ctx = AccountContext()
        credentials, _ = ctx.get_active()
        client = get_client()

        spans_nrql = NRQL_TRACE_SPANS % (trace_id, since_minutes)
        outcome = await execute_nrql(
            spans_nrql,
            account_id=credentials.account_id,
            client=client,
            timeout_override=25,
        )
        raw_spans = [row for row in outcome.results if isinstance(row, dict)]

        if not raw_spans:
            return json.dumps(
                {
                    "error": f"no spans found for trace {trace_id}",
                    "tool": "summarize_trace",
                    "hint": (
                        "The trace may be older than since_minutes (default 24h) "
                        "— retry with a larger window, or re-run get_slow_traces "
                        "for a fresh trace id."
                    ),
                    "nrql_used": spans_nrql,
                    "data_available": False,
                }
            )

        total_ms, root = trace_duration_ms(raw_spans)
        spans = compute_exclusive_times(raw_spans)
        hot = find_hot_span(spans)
        services = summarize_services(spans, total_ms)
        error_spans = [_span_view(s) for s in spans if _span_errored(s)][:10]

        verdicts: list[str] = []
        if hot:
            hot_pct = 100.0 * hot["exclusive_ms"] / total_ms if total_ms > 0 else 0.0
            verdicts.append(
                f"🔥 hot span: '{hot.get('name', '?')}' in {_span_service(hot)} — "
                f"{hot['exclusive_ms']:g}ms self-time "
                f"({hot_pct:.0f}% of the {total_ms:g}ms trace)."
            )
        if error_spans:
            first = error_spans[0]
            verdicts.append(
                f"❌ {len(error_spans)} errored span(s) — first: "
                f"'{first['name']}' in {first['service']} ({first.get('error', '?')})."
            )

        response: dict = {
            "trace_id": trace_id,
            "trace_duration_ms": round(total_ms, 2),
            "span_count": len(spans),
            "root_span": _span_view(root) if root else None,
            "hot_span": _span_view(hot) if hot else None,
            "services": services,
            "error_spans": error_spans,
            "verdicts": verdicts,
            "nrql_used": spans_nrql,
            "duration_ms": int((time.time() - start) * 1000),
        }
        if len(spans) >= 2000:
            response["warning"] = (
                "Trace has 2000+ spans — NRQL LIMIT MAX truncated the fetch; "
                "self-times may undercount."
            )
        if root is None:
            response["note"] = (
                "Root span not captured (sampled out or outside the window) — "
                "trace duration estimated from the span envelope."
            )
        return json.dumps(response)

    except Exception as exc:
        return json.dumps(
            {
                "error": str(exc),
                "tool": "summarize_trace",
                "hint": "Ensure you are connected and pass a trace id from get_slow_traces.",
                "data_available": False,
            }
        )
