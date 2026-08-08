"""
Golden signals tool for Sherlock.

Retrieves the four golden signals (latency, traffic, errors, saturation)
for a given APM service, plus timeseries data for trend detection.
Uses direct NRQL queries for reliability and simplicity.
"""

import asyncio
import json
import logging
import time
from typing import TYPE_CHECKING

from sherlock.client.newrelic import get_client
from sherlock.core.context import AccountContext
from sherlock.core.deeplinks import get_builder as _get_deeplink_builder
from sherlock.core.deeplinks import resolve_apm_guid
from sherlock.core.nrql_executor import execute_nrql, execute_nrql_compare
from sherlock.core.sanitize import (
    apply_resource_filter,
    check_env_mismatch,
    fuzzy_resolve_service,
    sanitize_service_name,
)
from sherlock.core.utils import strip_null_timeseries as _strip_null_timeseries

if TYPE_CHECKING:
    from sherlock.client.newrelic import NerdGraphClient

logger = logging.getLogger("sherlock.tools.golden_signals")

# Golden signal NRQL queries (legacy — kept for fallback).
NRQL_LATENCY = (
    "SELECT average(duration) as avg_duration, "
    "percentile(duration, 50, 90, 95, 99) "
    "FROM Transaction WHERE appName = '%s' "
    "SINCE %d minutes ago"
)

NRQL_THROUGHPUT = (
    "SELECT rate(count(*), 1 minute) as rpm "
    "FROM Transaction WHERE appName = '%s' "
    "SINCE %d minutes ago"
)

NRQL_ERROR_RATE = (
    "SELECT percentage(count(*), WHERE error IS true) as error_rate, "
    "count(*) as total_transactions "
    "FROM Transaction WHERE appName = '%s' "
    "SINCE %d minutes ago"
)

NRQL_SATURATION = (
    "SELECT average(cpuPercent) as avg_cpu, "
    "average(memoryResidentSizeBytes/1024/1024) as avg_memory_mb "
    "FROM Transaction WHERE appName = '%s' "
    "SINCE %d minutes ago"
)

NRQL_LATENCY_TIMESERIES = (
    "SELECT average(duration) as avg_duration "
    "FROM Transaction WHERE appName = '%s' "
    "TIMESERIES %d minutes SINCE %d minutes ago"
)

NRQL_ERROR_TIMESERIES = (
    "SELECT percentage(count(*), WHERE error IS true) as error_rate "
    "FROM Transaction WHERE appName = '%s' "
    "TIMESERIES %d minutes SINCE %d minutes ago"
)


def _timeseries_bucket(since_minutes: int) -> int:
    """Auto-scale TIMESERIES bucket to stay within NRQL 366-bucket limit."""
    return max(5, -(-since_minutes // 366))  # ceil division


async def _is_otel_service(
    service_name: str,
    account_id: str,
    client: "NerdGraphClient",
    resource_filter: str | None = None,
) -> bool:
    """Detect whether *service_name* is OTel-instrumented (Span, not Transaction).

    Makes two lightweight count(*) queries — one against Span (OTel) and one
    against Transaction (APM agent). If span data exists but transaction data
    does not, the service is OTel-only. When ``resource_filter`` is given
    the counts are scoped to that ``cloud.resource_id``, so detection
    reflects the targeted sub-resource rather than the whole shared entity.

    Returns ``True`` for OTel services, ``False`` otherwise.
    Falls back to ``False`` on any error so the caller uses the standard path.
    """
    try:

        async def _count(nrql_template: str) -> int:
            nrql = apply_resource_filter(nrql_template % service_name, resource_filter)
            outcome = await execute_nrql(
                nrql,
                account_id=account_id,
                client=client,
                timeout_override=10,
            )
            rows = outcome.results
            if rows and isinstance(rows[0], dict):
                return rows[0].get("event_count", 0) or 0
            return 0

        span_count, txn_count = await asyncio.gather(
            _count(NRQL_OTEL_CHECK_SPANS),
            _count(NRQL_OTEL_CHECK_TXNS),
        )
        return span_count > 0 and txn_count == 0
    except Exception:
        return False


NRQL_TOP_ERRORS = (
    "SELECT count(*) FROM TransactionError WHERE appName = '%s' "
    "FACET error.class, error.message "
    "SINCE %d minutes ago LIMIT 10"
)

# ── OTel (OpenTelemetry) NRQL variants ──────────────────────────────────
# OTel services use Span events instead of Transaction. These queries are
# used when _is_otel_service() detects a Span-only service.

NRQL_OTEL_CHECK_SPANS = (
    "SELECT count(*) as event_count FROM Span WHERE entity.name = '%s' SINCE 15 minutes ago"
)

NRQL_OTEL_CHECK_TXNS = (
    "SELECT count(*) as event_count FROM Transaction WHERE appName = '%s' SINCE 15 minutes ago"
)

NRQL_OTEL_GOLDEN = (
    "SELECT "
    "percentage(count(*), WHERE otel.status_code = 'ERROR') as error_rate, "
    "average(duration) as avg_duration, "
    "percentile(duration, 50, 90, 95, 99), "
    "rate(count(*), 1 minute) as rpm "
    "FROM Span "
    "WHERE entity.name = '%s' AND span.kind = 'SERVER' "
    "SINCE %d minutes ago"
)

NRQL_OTEL_LATENCY_TIMESERIES = (
    "SELECT average(duration) as avg_duration "
    "FROM Span "
    "WHERE entity.name = '%s' AND span.kind = 'SERVER' "
    "TIMESERIES %d minutes SINCE %d minutes ago"
)

NRQL_OTEL_ERROR_TIMESERIES = (
    "SELECT percentage(count(*), WHERE otel.status_code = 'ERROR') as error_rate "
    "FROM Span "
    "WHERE entity.name = '%s' AND span.kind = 'SERVER' "
    "TIMESERIES %d minutes SINCE %d minutes ago"
)

NRQL_OTEL_TOP_ERRORS = (
    "SELECT count(*) FROM Span "
    "WHERE entity.name = '%s' AND otel.status_code = 'ERROR' "
    "FACET otel.status_description "
    "SINCE %d minutes ago LIMIT 10"
)

# ── Baseline comparison (COMPARE WITH) ──────────────────────────────────
# One combined query per service compares the current window against the
# same window one week ago; NerdGraph returns currentResults/previousResults.

NRQL_BASELINE_COMPARE = (
    "SELECT average(duration) as avg_duration, "
    "percentage(count(*), WHERE error IS true) as error_rate, "
    "rate(count(*), 1 minute) as rpm "
    "FROM Transaction WHERE appName = '%s' "
    "SINCE %d minutes ago COMPARE WITH 1 week ago"
)

NRQL_OTEL_BASELINE_COMPARE = (
    "SELECT average(duration) as avg_duration, "
    "percentage(count(*), WHERE otel.status_code = 'ERROR') as error_rate, "
    "rate(count(*), 1 minute) as rpm "
    "FROM Span WHERE entity.name = '%s' AND span.kind = 'SERVER' "
    "SINCE %d minutes ago COMPARE WITH 1 week ago"
)

# Thresholds for signal detection.
LATENCY_P99_WARN_MS = 5.0
ERROR_RATE_WARN_PCT = 5.0
ERROR_RATE_CRITICAL_PCT = 20.0
CPU_WARN_PCT = 80.0

# Baseline verdict thresholds (percent change vs last week).
BASELINE_LATENCY_DELTA_PCT = 20.0
BASELINE_TRAFFIC_DELTA_PCT = 30.0
BASELINE_TRAFFIC_DROP_CRITICAL_PCT = 50.0
BASELINE_ERROR_RATIO_WARN = 1.5
BASELINE_ERROR_RATIO_CRITICAL = 3.0


def _delta_pct(current: float | None, previous: float | None) -> float | None:
    """Percent change vs baseline; None when undefined (no/zero baseline)."""
    if current is None or previous is None or previous == 0:
        return None
    return (current - previous) / previous * 100


def build_baseline_comparison(current: dict, previous: dict) -> dict:
    """Turn a COMPARE WITH row pair into per-metric deltas and verdicts.

    Args:
        current: Row from currentResults (avg_duration/error_rate/rpm).
        previous: Matching row from previousResults (one week earlier).

    Returns:
        Dict with available flag, per-metric current/previous/delta_pct/
        verdict, and a flat verdicts list for report rendering.
    """
    if not current and not previous:
        return {"available": False, "reason": "COMPARE WITH query returned no data"}

    cur_lat, prev_lat = current.get("avg_duration"), previous.get("avg_duration")
    cur_err, prev_err = current.get("error_rate"), previous.get("error_rate")
    cur_rpm, prev_rpm = current.get("rpm"), previous.get("rpm")

    # Latency.
    lat_delta = _delta_pct(cur_lat, prev_lat)
    if lat_delta is None:
        lat_verdict = "no latency baseline for last week"
    elif lat_delta >= 100:
        lat_verdict = f"🔴 latency +{lat_delta:.0f}% vs last week"
    elif lat_delta >= BASELINE_LATENCY_DELTA_PCT:
        lat_verdict = f"⚠️ latency +{lat_delta:.0f}% vs last week"
    elif lat_delta <= -BASELINE_LATENCY_DELTA_PCT:
        lat_verdict = f"✅ latency {lat_delta:.0f}% vs last week (improved)"
    else:
        lat_verdict = "✅ latency in line with last week"

    # Error rate — compare as a ratio; a jump from ~0 is its own case.
    cur_err_v = cur_err or 0.0
    prev_err_v = prev_err or 0.0
    if prev_err_v < 0.1:
        if cur_err_v >= 1.0:
            err_verdict = f"🔴 error rate {cur_err_v:.1f}% — was ~0% last week"
        else:
            err_verdict = "✅ error rate in line with last week (~0%)"
    else:
        ratio = cur_err_v / prev_err_v
        if ratio >= BASELINE_ERROR_RATIO_CRITICAL:
            err_verdict = (
                f"🔴 error rate {ratio:.1f}x last week ({prev_err_v:.1f}% → {cur_err_v:.1f}%)"
            )
        elif ratio >= BASELINE_ERROR_RATIO_WARN:
            err_verdict = (
                f"⚠️ error rate {ratio:.1f}x last week ({prev_err_v:.1f}% → {cur_err_v:.1f}%)"
            )
        elif ratio <= 0.5:
            err_verdict = (
                f"✅ error rate improved vs last week ({prev_err_v:.1f}% → {cur_err_v:.1f}%)"
            )
        else:
            err_verdict = "✅ error rate in line with last week"

    # Throughput.
    rpm_delta = _delta_pct(cur_rpm, prev_rpm)
    if rpm_delta is None:
        rpm_verdict = "no traffic baseline for last week"
    elif rpm_delta <= -BASELINE_TRAFFIC_DROP_CRITICAL_PCT:
        rpm_verdict = (
            f"🔴 traffic {rpm_delta:.0f}% vs last week — possible upstream outage or traffic shift"
        )
    elif rpm_delta <= -BASELINE_TRAFFIC_DELTA_PCT:
        rpm_verdict = f"⚠️ traffic {rpm_delta:.0f}% vs last week"
    elif rpm_delta >= 100:
        rpm_verdict = f"⚠️ traffic +{rpm_delta:.0f}% vs last week — possible flood"
    else:
        rpm_verdict = "✅ traffic in line with last week"

    err_delta = _delta_pct(cur_err, prev_err)
    return {
        "available": True,
        "compared_with": "1 week ago",
        "latency": {
            "current_s": cur_lat,
            "previous_s": prev_lat,
            "delta_pct": round(lat_delta, 1) if lat_delta is not None else None,
            "verdict": lat_verdict,
        },
        "error_rate": {
            "current_pct": cur_err,
            "previous_pct": prev_err,
            "delta_pct": round(err_delta, 1) if err_delta is not None else None,
            "verdict": err_verdict,
        },
        "throughput": {
            "current_rpm": cur_rpm,
            "previous_rpm": prev_rpm,
            "delta_pct": round(rpm_delta, 1) if rpm_delta is not None else None,
            "verdict": rpm_verdict,
        },
        "verdicts": [lat_verdict, err_verdict, rpm_verdict],
    }


async def get_service_golden_signals(
    service_name: str,
    since_minutes: int = 30,
    resource_filter: str | None = None,
) -> str:
    """Get the four golden signals for an APM service.

    Runs direct NRQL queries for latency, throughput, errors, and
    saturation in parallel, plus a COMPARE WITH baseline against the same
    window one week ago. Returns health assessment with deep links.

    Args:
        service_name: APM service name (fuzzy resolved).
        since_minutes: Time window in minutes.
        resource_filter: Optional ``cloud.resource_id`` substring filter.
            Use when multiple deployed apps (e.g. Azure Function Apps)
            share one New Relic entity name — scopes every signal to the
            matching sub-resource.

    Returns:
        JSON string with golden signals data and health assessment.
    """
    start = time.time()
    try:
        ctx = AccountContext()
        credentials, intelligence = ctx.get_active()
        client = get_client()

        safe_name = sanitize_service_name(service_name)
        resolved_name, was_fuzzy, confidence = fuzzy_resolve_service(
            safe_name,
            intelligence.apm.service_names,
            naming_convention=intelligence.naming_convention,
        )

        async def _nrql(nrql_str: str) -> list:
            outcome = await execute_nrql(
                apply_resource_filter(nrql_str, resource_filter),
                account_id=credentials.account_id,
                client=client,
                timeout_override=20,
            )
            return outcome.results

        async def _baseline_rows(nrql_str: str) -> tuple[dict, dict]:
            outcome = await execute_nrql_compare(
                apply_resource_filter(nrql_str, resource_filter),
                account_id=credentials.account_id,
                client=client,
                timeout_override=20,
            )
            cur = outcome.current[0] if outcome.current else {}
            prev = outcome.previous[0] if outcome.previous else {}
            return cur, prev

        # ── OTel detection (Span vs Transaction) ──
        # If the service only has Span data, switch to OTel query variants.
        is_otel = False
        try:
            is_otel = await _is_otel_service(
                resolved_name,
                credentials.account_id,
                client,
                resource_filter=resource_filter,
            )
        except Exception:
            pass  # Fall back to standard APM queries on detection error.

        # Run all golden signal queries in parallel.
        bucket = _timeseries_bucket(since_minutes)

        if is_otel:
            # OTel path: query FROM Span with entity.name and span.kind='SERVER'.
            golden_task = _nrql(NRQL_OTEL_GOLDEN % (resolved_name, since_minutes))
            latency_ts_task = _nrql(
                NRQL_OTEL_LATENCY_TIMESERIES % (resolved_name, bucket, since_minutes)
            )
            error_ts_task = _nrql(
                NRQL_OTEL_ERROR_TIMESERIES % (resolved_name, bucket, since_minutes)
            )
            top_errors_task = _nrql(NRQL_OTEL_TOP_ERRORS % (resolved_name, since_minutes))
            baseline_task = _baseline_rows(
                NRQL_OTEL_BASELINE_COMPARE % (resolved_name, since_minutes)
            )

            otel_results: list = await asyncio.gather(
                golden_task,
                latency_ts_task,
                error_ts_task,
                top_errors_task,
                baseline_task,
                return_exceptions=True,
            )

            golden = otel_results[0] if not isinstance(otel_results[0], BaseException) else []
            latency_ts = otel_results[1] if not isinstance(otel_results[1], BaseException) else []
            error_ts = otel_results[2] if not isinstance(otel_results[2], BaseException) else []
            top_errors = otel_results[3] if not isinstance(otel_results[3], BaseException) else []
            baseline_outcome = otel_results[4]

            golden_data = golden[0] if golden else {}
            latency_data = {
                "avg_duration": golden_data.get("avg_duration", 0),
                "percentile.duration.50": golden_data.get("percentile.duration.50"),
                "percentile.duration.90": golden_data.get("percentile.duration.90"),
                "percentile.duration.95": golden_data.get("percentile.duration.95"),
                "percentile.duration.99": golden_data.get("percentile.duration.99", 0),
            }
            throughput_data = {"rpm": golden_data.get("rpm", 0)}
            error_data = {
                "error_rate": golden_data.get("error_rate", 0),
                "total_transactions": 0,  # N/A for OTel
            }
            saturation_data: dict = {}  # OTel spans don't carry host CPU/mem
        else:
            # Standard APM path: query FROM Transaction.
            latency_task = _nrql(NRQL_LATENCY % (resolved_name, since_minutes))
            throughput_task = _nrql(NRQL_THROUGHPUT % (resolved_name, since_minutes))
            error_task = _nrql(NRQL_ERROR_RATE % (resolved_name, since_minutes))
            saturation_task = _nrql(NRQL_SATURATION % (resolved_name, since_minutes))
            latency_ts_task = _nrql(
                NRQL_LATENCY_TIMESERIES % (resolved_name, bucket, since_minutes)
            )
            error_ts_task = _nrql(NRQL_ERROR_TIMESERIES % (resolved_name, bucket, since_minutes))
            top_errors_task = _nrql(NRQL_TOP_ERRORS % (resolved_name, since_minutes))
            baseline_task = _baseline_rows(NRQL_BASELINE_COMPARE % (resolved_name, since_minutes))

            results: list = await asyncio.gather(
                latency_task,
                throughput_task,
                error_task,
                saturation_task,
                latency_ts_task,
                error_ts_task,
                top_errors_task,
                baseline_task,
                return_exceptions=True,
            )

            latency = results[0] if not isinstance(results[0], BaseException) else []
            throughput = results[1] if not isinstance(results[1], BaseException) else []
            errors = results[2] if not isinstance(results[2], BaseException) else []
            saturation = results[3] if not isinstance(results[3], BaseException) else []
            latency_ts = results[4] if not isinstance(results[4], BaseException) else []
            error_ts = results[5] if not isinstance(results[5], BaseException) else []
            top_errors = results[6] if not isinstance(results[6], BaseException) else []
            baseline_outcome = results[7]

            latency_data = latency[0] if latency else {}
            throughput_data = throughput[0] if throughput else {}
            error_data = errors[0] if errors else {}
            saturation_data = saturation[0] if saturation else {}

        # Derive health signals.
        signals: list[str] = []
        error_rate = error_data.get("error_rate", 0) or 0
        avg_duration = latency_data.get("avg_duration", 0) or 0
        p99 = latency_data.get("percentile.duration.99", 0) or 0
        avg_cpu = saturation_data.get("avg_cpu", 0) or 0

        if error_rate >= ERROR_RATE_CRITICAL_PCT:
            signals.append(f"🔴 CRITICAL error rate: {error_rate:.1f}%")
        elif error_rate >= ERROR_RATE_WARN_PCT:
            signals.append(f"⚠️ Elevated error rate: {error_rate:.1f}%")

        if p99 >= LATENCY_P99_WARN_MS:
            signals.append(f"⚠️ High P99 latency: {p99:.2f}s")

        if avg_cpu >= CPU_WARN_PCT:
            signals.append(f"⚠️ High CPU saturation: {avg_cpu:.1f}%")

        rpm = throughput_data.get("rpm", 0) or 0
        if rpm == 0:
            signals.append("🔴 ZERO throughput — service may be down")

        # Overall status.
        if any("🔴" in s for s in signals):
            overall = "CRITICAL"
        elif any("⚠️" in s for s in signals):
            overall = "WARNING"
        else:
            overall = "HEALTHY"

        duration_ms = int((time.time() - start) * 1000)
        response: dict = {
            "service_name": resolved_name,
            "since_minutes": since_minutes,
            "overall_status": overall,
            "health_signals": signals,
            "instrumentation": "otel" if is_otel else "apm",
            **({"resource_filter": resource_filter} if resource_filter else {}),
            "latency": {
                "avg_duration_s": avg_duration,
                "p50": latency_data.get("percentile.duration.50"),
                "p90": latency_data.get("percentile.duration.90"),
                "p95": latency_data.get("percentile.duration.95"),
                "p99": p99,
            },
            "throughput": {
                "rpm": rpm,
            },
            "errors": {
                "error_rate_pct": error_rate,
                "total_transactions": error_data.get("total_transactions", 0),
                "top_errors": top_errors,
            },
            "saturation": {
                "avg_cpu_pct": avg_cpu,
                "avg_memory_mb": saturation_data.get("avg_memory_mb"),
            },
            "latency_timeseries": _strip_null_timeseries(latency_ts),
            "error_timeseries": _strip_null_timeseries(error_ts),
            "duration_ms": duration_ms,
        }

        # Baseline comparison vs the same window last week (never blocks).
        if isinstance(baseline_outcome, BaseException):
            response["baseline"] = {"available": False, "reason": str(baseline_outcome)}
        else:
            response["baseline"] = build_baseline_comparison(*baseline_outcome)

        # Deep links.
        try:
            _builder = _get_deeplink_builder()
            if _builder:
                _guid = resolve_apm_guid(resolved_name, intelligence)
                if is_otel:
                    _err_nrql = (
                        f"SELECT percentage(count(*), WHERE otel.status_code = 'ERROR') "
                        f"as error_rate "
                        f"FROM Span WHERE entity.name='{resolved_name}' AND span.kind='SERVER' "
                        f"TIMESERIES 5 minutes SINCE {since_minutes} minutes ago"
                    )
                    _p95_nrql = (
                        f"SELECT percentile(duration, 95) as p95 "
                        f"FROM Span WHERE entity.name='{resolved_name}' AND span.kind='SERVER' "
                        f"TIMESERIES 5 minutes SINCE {since_minutes} minutes ago"
                    )
                    _tput_nrql = (
                        f"SELECT rate(count(*), 1 minute) as rpm "
                        f"FROM Span WHERE entity.name='{resolved_name}' AND span.kind='SERVER' "
                        f"TIMESERIES 5 minutes SINCE {since_minutes} minutes ago"
                    )
                else:
                    _err_nrql = (
                        f"SELECT percentage(count(*), WHERE error IS true) as error_rate "
                        f"FROM Transaction WHERE appName='{resolved_name}' "
                        f"TIMESERIES 5 minutes SINCE {since_minutes} minutes ago"
                    )
                    _p95_nrql = (
                        f"SELECT percentile(duration, 95) as p95 "
                        f"FROM Transaction WHERE appName='{resolved_name}' "
                        f"TIMESERIES 5 minutes SINCE {since_minutes} minutes ago"
                    )
                    _tput_nrql = (
                        f"SELECT rate(count(*), 1 minute) as rpm "
                        f"FROM Transaction WHERE appName='{resolved_name}' "
                        f"TIMESERIES 5 minutes SINCE {since_minutes} minutes ago"
                    )
                # Chart links must show the same slice the verdicts were
                # computed from — scope them to the sub-resource too.
                _err_nrql = apply_resource_filter(_err_nrql, resource_filter)
                _p95_nrql = apply_resource_filter(_p95_nrql, resource_filter)
                _tput_nrql = apply_resource_filter(_tput_nrql, resource_filter)
                response["links"] = {
                    "service_overview": _builder.apm_overview(_guid, since_minutes)
                    if _guid
                    else None,
                    "errors_inbox": _builder.apm_errors(_guid, since_minutes) if _guid else None,
                    "transactions": _builder.apm_transactions(_guid, since_minutes)
                    if _guid
                    else None,
                    # Chart links filter by appName string, not GUID — safe under ambiguity.
                    "error_chart": _builder.nrql_chart(_err_nrql, since_minutes),
                    "latency_chart": _builder.nrql_chart(_p95_nrql, since_minutes),
                    "throughput_chart": _builder.nrql_chart(_tput_nrql, since_minutes),
                }
                response["nrql"] = {
                    "error_rate": _err_nrql,
                    "latency_p95": _p95_nrql,
                    "throughput": _tput_nrql,
                }
                # If GUID was ambiguous, surface that to the agent.
                if not _guid and resolved_name in intelligence.apm.service_names:
                    response.setdefault("warnings", []).append(
                        f"APM GUID for '{resolved_name}' is ambiguous "
                        f"(multiple reporters, no unique reporting match). "
                        f"Entity-view links omitted; use chart links for evidence."
                    )
        except Exception:
            pass

        if was_fuzzy:
            response["resolved_from"] = service_name
            response["note"] = f"Fuzzy matched '{service_name}' → '{resolved_name}'"
            env_warn = check_env_mismatch(
                service_name,
                resolved_name,
                intelligence.naming_convention,
            )
            if env_warn:
                response["warnings"] = [env_warn]

        if is_otel:
            response.setdefault("warnings", []).append(
                "⚠️ OTel service detected — using Span events instead of Transaction"
            )

        # Auto-save to session memory
        try:
            from datetime import datetime
            from datetime import timezone as _tz

            from sherlock.core.session_memory import InvestigationSnapshot, SessionMemory

            snap = InvestigationSnapshot(
                timestamp=datetime.now(_tz.utc),
                account_id=str(credentials.account_id),
                account_name=getattr(intelligence, "account_meta", None)
                and getattr(intelligence.account_meta, "name", "")
                or "",
                service_name=resolved_name,
                bare_name=resolved_name.split("/")[-1] if "/" in resolved_name else resolved_name,
                namespace=resolved_name.split("/")[0] if "/" in resolved_name else "",
                severity=overall,
                root_cause="",
                causal_chain="",
                causal_pattern="NONE",
                error_rate=error_rate,
                is_otel=is_otel,
                since_minutes=since_minutes,
            )
            SessionMemory().record(snap)
        except Exception:
            pass  # Never let session memory break the main tool response

        return json.dumps(response)

    except Exception as exc:
        return json.dumps(
            {
                "error": str(exc),
                "tool": "get_service_golden_signals",
                "hint": "Check service name. Use get_apm_applications() to list all.",
                "data_available": False,
            }
        )
