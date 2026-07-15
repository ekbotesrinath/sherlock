"""
Change correlation tool for Sherlock.

Answers the first question of any incident: "what changed?"

Builds a unified timeline of APM deployment markers (``Deployment`` events)
and Kubernetes rollout events (``InfrastructureEvent``), then correlates
the timeline against the service's error-rate onset so the agent can say
"deploy of payment-svc landed 4 minutes before the error spike".
"""

import asyncio
import json
import logging
import time
from datetime import datetime, timezone

from sherlock.client.newrelic import get_client
from sherlock.core.context import AccountContext
from sherlock.core.deeplinks import get_builder as _get_deeplink_builder
from sherlock.core.deeplinks import resolve_apm_guid
from sherlock.core.nrql_executor import execute_nrql
from sherlock.core.sanitize import check_env_mismatch, fuzzy_resolve_service, sanitize_service_name

logger = logging.getLogger("sherlock.tools.changes")

# ── NRQL templates ───────────────────────────────────────────────────────

NRQL_DEPLOY_MARKERS = (
    "SELECT timestamp, appName, description, revision, changelog, user "
    "FROM Deployment "
    "%s"  # optional WHERE appName = 'X'
    "SINCE %d minutes ago LIMIT 100"
)

# Kubernetes rollout signals come in via the kube-events integration as
# InfrastructureEvent rows. ScalingReplicaSet on a Deployment is the
# canonical rollout marker; ReplicaSet create/rescale catches the rest.
NRQL_K8S_DEPLOY_EVENTS = (
    "SELECT timestamp, `event.reason` as reason, "
    "`event.involvedObject.kind` as object_kind, "
    "`event.involvedObject.name` as object_name, "
    "`event.message` as message, clusterName, namespaceName "
    "FROM InfrastructureEvent "
    "WHERE category = 'kubernetes' "
    "AND `event.involvedObject.kind` IN ('Deployment', 'ReplicaSet') "
    "AND `event.reason` IN ('ScalingReplicaSet', 'SuccessfulCreate', "
    "'SuccessfulRescale', 'DeploymentRollback') "
    "%s"  # optional AND object_name LIKE / namespace filter
    "SINCE %d minutes ago LIMIT 100"
)

NRQL_ERROR_ONSET_TS = (
    "SELECT percentage(count(*), WHERE error IS true) as error_rate "
    "FROM Transaction WHERE appName = '%s' "
    "TIMESERIES %d minutes SINCE %d minutes ago"
)

# Onset detection thresholds.
ERROR_ONSET_MIN_PCT = 5.0  # a bucket must reach this to count as onset
ERROR_ONSET_BASELINE_FACTOR = 3.0  # ... and be 3x the pre-onset baseline
CORRELATION_WINDOW_MINUTES = 60  # change → onset gap that counts as correlated


def _timeseries_bucket(since_minutes: int) -> int:
    """Auto-scale TIMESERIES bucket to stay within NRQL 366-bucket limit."""
    return max(2, -(-since_minutes // 366))  # ceil division


def _iso(timestamp_ms: float | int | None) -> str:
    """Millisecond epoch → ISO-8601 UTC string (empty on bad input)."""
    try:
        if not timestamp_ms:
            return ""
        return datetime.fromtimestamp(float(timestamp_ms) / 1000, tz=timezone.utc).isoformat()
    except Exception:
        return ""


def _median(values: list[float]) -> float:
    """Median of a non-empty list (0.0 for empty)."""
    if not values:
        return 0.0
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2


def detect_error_onset(timeseries: list[dict]) -> dict:
    """Find the first sustained error-rate spike in a TIMESERIES result.

    A bucket is the onset when its rate reaches both ERROR_ONSET_MIN_PCT
    and ERROR_ONSET_BASELINE_FACTOR x the median of all preceding buckets,
    and the following bucket (if any) stays elevated. If the window opens
    already spiking, the first bucket is reported with a note.

    Args:
        timeseries: NRQL TIMESERIES rows with beginTimeSeconds + error_rate.

    Returns:
        Dict with detected flag and, when detected, timestamp_ms / time /
        error_rate_pct / baseline_pct (+ optional note).
    """
    buckets: list[tuple[float, float]] = []
    for row in timeseries:
        if not isinstance(row, dict):
            continue
        begin = row.get("beginTimeSeconds")
        if begin is None:
            continue
        rate = row.get("error_rate")
        buckets.append((float(begin) * 1000, float(rate) if rate is not None else 0.0))

    if len(buckets) < 2:
        return {"detected": False, "reason": "not enough timeseries data"}

    rates = [rate for _, rate in buckets]

    # Window already elevated at the start — spike predates the window.
    if rates[0] >= ERROR_ONSET_MIN_PCT:
        return {
            "detected": True,
            "timestamp_ms": buckets[0][0],
            "time": _iso(buckets[0][0]),
            "error_rate_pct": round(rates[0], 2),
            "baseline_pct": None,
            "note": (
                "Error rate was already elevated at the start of the window — "
                "widen since_minutes to find the true onset."
            ),
        }

    for i in range(1, len(buckets)):
        baseline = _median(rates[:i])
        threshold = max(ERROR_ONSET_MIN_PCT, baseline * ERROR_ONSET_BASELINE_FACTOR)
        if rates[i] < threshold:
            continue
        # Sustained: the next bucket stays elevated (or this is the last one).
        is_last = i == len(buckets) - 1
        if is_last or rates[i + 1] >= threshold * 0.8:
            return {
                "detected": True,
                "timestamp_ms": buckets[i][0],
                "time": _iso(buckets[i][0]),
                "error_rate_pct": round(rates[i], 2),
                "baseline_pct": round(baseline, 2),
            }

    return {"detected": False, "reason": "no error-rate spike found in window"}


def correlate_changes(timeline: list[dict], onset: dict) -> dict:
    """Match timeline changes against the error onset.

    A change correlates when it landed 0..CORRELATION_WINDOW_MINUTES before
    the onset. The nearest preceding change becomes the prime suspect.

    Args:
        timeline: Normalized change events (each with timestamp_ms + label).
        onset: Result of :func:`detect_error_onset`.

    Returns:
        Dict with error_onset, correlated change list, and a verdict line.
    """
    result: dict = {"error_onset": onset, "correlated_changes": [], "verdict": None}
    if not onset.get("detected"):
        result["verdict"] = (
            "No error onset detected in the window — changes listed for context only."
        )
        return result

    onset_ms = onset["timestamp_ms"]
    correlated: list[dict] = []
    for event in timeline:
        ts = event.get("timestamp_ms")
        if ts is None:
            continue
        delta_min = (onset_ms - ts) / 60000
        if 0 <= delta_min <= CORRELATION_WINDOW_MINUTES:
            entry = dict(event)
            entry["minutes_before_onset"] = round(delta_min, 1)
            correlated.append(entry)

    correlated.sort(key=lambda e: e["minutes_before_onset"])
    result["correlated_changes"] = correlated

    if correlated:
        prime = correlated[0]
        result["verdict"] = (
            f"🎯 {prime['label']} landed {prime['minutes_before_onset']:g}m "
            f"before the error onset — prime suspect."
        )
    else:
        result["verdict"] = (
            f"Error onset at {onset.get('time', '?')} but no change landed in the "
            f"{CORRELATION_WINDOW_MINUTES}m before it — likely not deploy-related."
        )
    return result


def _normalize_deploy_markers(rows: list) -> list[dict]:
    """APM Deployment rows → normalized timeline events."""
    events: list[dict] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        app = row.get("appName", "")
        revision = row.get("revision", "")
        label = f"deploy of {app or 'unknown app'}"
        if revision:
            label += f" (rev {revision})"
        events.append(
            {
                "type": "deployment_marker",
                "timestamp_ms": row.get("timestamp"),
                "time": _iso(row.get("timestamp")),
                "label": label,
                "service": app,
                "revision": revision,
                "description": row.get("description", ""),
                "user": row.get("user", ""),
            }
        )
    return events


def _normalize_k8s_events(rows: list) -> list[dict]:
    """InfrastructureEvent rows → normalized timeline events."""
    events: list[dict] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        kind = row.get("object_kind", "")
        name = row.get("object_name", "")
        reason = row.get("reason", "")
        events.append(
            {
                "type": "k8s_deployment_event",
                "timestamp_ms": row.get("timestamp"),
                "time": _iso(row.get("timestamp")),
                "label": f"K8s {reason or 'event'} on {kind} {name}".strip(),
                "object_kind": kind,
                "object_name": name,
                "reason": reason,
                "message": row.get("message", ""),
                "cluster": row.get("clusterName", ""),
                "namespace": row.get("namespaceName", ""),
            }
        )
    return events


def _k8s_namespace_for(intelligence: object, bare_name: str) -> str | None:
    """Find the K8s namespace whose deployment list contains *bare_name*."""
    try:
        deployments = getattr(getattr(intelligence, "k8s", None), "deployments", None) or {}
        for namespace, names in deployments.items():
            for deploy in names:
                bare_deploy = deploy.rsplit("/", 1)[-1]
                if bare_deploy == bare_name or bare_name in bare_deploy:
                    return str(namespace)
    except Exception:
        pass
    return None


async def get_recent_changes(
    service_name: str | None = None,
    since_minutes: int = 240,
) -> str:
    """Get recent deployments and K8s rollout events on one timeline.

    Combines APM deployment markers and Kubernetes deployment events, and
    (when a service is given) auto-correlates them against the service's
    error-rate onset: "deploy X landed 4m before the spike".

    Args:
        service_name: Optional APM service to scope changes and correlate
            against (fuzzy resolved). Omit for account-wide changes.
        since_minutes: Time window in minutes (default 4 hours).

    Returns:
        JSON string with the change timeline and error-onset correlation.
    """
    start = time.time()
    try:
        ctx = AccountContext()
        credentials, intelligence = ctx.get_active()
        client = get_client()

        resolved_name: str | None = None
        was_fuzzy = False
        if service_name:
            safe_name = sanitize_service_name(service_name)
            try:
                resolved_name, was_fuzzy, _ = fuzzy_resolve_service(
                    safe_name,
                    intelligence.apm.service_names,
                    threshold=0.5,
                    naming_convention=intelligence.naming_convention,
                )
            except Exception:
                resolved_name = safe_name

        async def _run(nrql_str: str) -> list:
            outcome = await execute_nrql(
                nrql_str,
                account_id=credentials.account_id,
                client=client,
                timeout_override=20,
            )
            return outcome.results

        # ── Build the three queries ──
        deploy_filter = f"WHERE appName = '{resolved_name}' " if resolved_name else ""
        deploy_nrql = NRQL_DEPLOY_MARKERS % (deploy_filter, since_minutes)

        k8s_filter = ""
        bare_name = ""
        if resolved_name:
            bare_name = resolved_name.rsplit("/", 1)[-1]
            k8s_filter = f"AND `event.involvedObject.name` LIKE '%{bare_name}%' "
            namespace = _k8s_namespace_for(intelligence, bare_name)
            if namespace:
                k8s_filter += f"AND namespaceName = '{namespace}' "
        k8s_nrql = NRQL_K8S_DEPLOY_EVENTS % (k8s_filter, since_minutes)

        tasks = [_run(deploy_nrql), _run(k8s_nrql)]
        error_ts_nrql = None
        if resolved_name:
            bucket = _timeseries_bucket(since_minutes)
            error_ts_nrql = NRQL_ERROR_ONSET_TS % (resolved_name, bucket, since_minutes)
            tasks.append(_run(error_ts_nrql))

        gathered: list = await asyncio.gather(*tasks, return_exceptions=True)
        deploy_rows = gathered[0] if not isinstance(gathered[0], BaseException) else []
        k8s_rows = gathered[1] if not isinstance(gathered[1], BaseException) else []
        error_ts = []
        if len(gathered) > 2 and not isinstance(gathered[2], BaseException):
            error_ts = gathered[2]

        # ── Unified timeline (newest first) ──
        timeline = _normalize_deploy_markers(deploy_rows) + _normalize_k8s_events(k8s_rows)
        timeline.sort(key=lambda e: e.get("timestamp_ms") or 0, reverse=True)

        # ── Error-onset correlation ──
        correlation: dict | None = None
        if resolved_name:
            onset = detect_error_onset(error_ts)
            correlation = correlate_changes(timeline, onset)

        duration_ms = int((time.time() - start) * 1000)
        response: dict = {
            "service_name": resolved_name,
            "since_minutes": since_minutes,
            "total_changes": len(timeline),
            "deployment_markers": len(deploy_rows),
            "k8s_deployment_events": len(k8s_rows),
            "timeline": timeline,
            "nrql_used": {
                "deployment_markers": deploy_nrql,
                "k8s_events": k8s_nrql,
            },
            "duration_ms": duration_ms,
        }
        if correlation is not None:
            response["correlation"] = correlation
            if error_ts_nrql:
                response["nrql_used"]["error_timeseries"] = error_ts_nrql
        if not timeline:
            response["hint"] = (
                "No changes found in the window. Deployment markers require the "
                "change-tracking/deployment API; K8s events require the "
                "kube-events integration. Try a larger since_minutes."
            )

        # Deep links.
        try:
            _builder = _get_deeplink_builder()
            if _builder and resolved_name:
                _guid = resolve_apm_guid(resolved_name, intelligence)
                if _guid:
                    response["links"] = {
                        "service_overview": _builder.apm_overview(_guid, since_minutes),
                    }
        except Exception:
            pass

        if was_fuzzy and resolved_name and service_name:
            response["resolved_from"] = service_name
            response["note"] = f"Fuzzy matched '{service_name}' → '{resolved_name}'"
            env_warn = check_env_mismatch(
                service_name,
                resolved_name,
                intelligence.naming_convention,
            )
            if env_warn:
                response["warnings"] = [env_warn]

        return json.dumps(response)

    except Exception as exc:
        return json.dumps(
            {
                "error": str(exc),
                "tool": "get_recent_changes",
                "hint": "Ensure you are connected. Omit service_name for account-wide changes.",
                "data_available": False,
            }
        )
