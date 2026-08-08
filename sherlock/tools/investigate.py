"""
Composite investigation tool for Sherlock.

Runs every investigation domain concurrently on the server side and
returns one structured bundle. This makes the full sweep deterministic
for ANY MCP client — the LLM no longer has to remember to call six tools
in the right order; one call fans out to golden signals, recent changes,
error logs, Kubernetes health, incidents, dependencies, and (when an
associated monitor exists) synthetics.
"""

import asyncio
import json
import logging
import time
from datetime import datetime, timezone

from sherlock.core.context import AccountContext
from sherlock.core.sanitize import check_env_mismatch, fuzzy_resolve_service, sanitize_service_name
from sherlock.core.session_memory import InvestigationSnapshot, SessionMemory
from sherlock.tools.alerts import get_service_incidents
from sherlock.tools.changes import get_recent_changes
from sherlock.tools.dependencies import get_service_dependencies
from sherlock.tools.golden_signals import get_service_golden_signals
from sherlock.tools.k8s import get_k8s_health
from sherlock.tools.logs import search_logs
from sherlock.tools.synthetics import get_monitor_status

logger = logging.getLogger("sherlock.tools.investigate")

# The change timeline needs a wider lookback than the health window itself
# — a deploy 3 hours ago can explain a spike 20 minutes ago.
CHANGES_WINDOW_MIN_MINUTES = 240


def _parse_domain(raw: object) -> dict:
    """Parse one sub-tool result into a dict; never raises."""
    if isinstance(raw, BaseException):
        return {"error": str(raw), "data_available": False}
    try:
        parsed = json.loads(str(raw))
        if isinstance(parsed, dict):
            return parsed
        return {"error": "unexpected response shape", "data_available": False}
    except (json.JSONDecodeError, TypeError):
        return {"error": "unparseable tool response", "data_available": False}


def _associated_monitor(intelligence: object, resolved_name: str) -> str | None:
    """Find a synthetic monitor associated with the service, if any."""
    try:
        synthetics = getattr(intelligence, "synthetics", None)
        monitor_map = getattr(synthetics, "monitor_map", None) or {}
        for name, meta in monitor_map.items():
            if getattr(meta, "associated_service", None) == resolved_name:
                return str(name)
    except Exception:
        pass
    return None


def _summarize(domains: dict) -> tuple[str, list[str]]:
    """Derive (overall_status, summary verdict lines) from the domain bundle.

    Starts from the golden-signals status and escalates on 🔴/⚠️ signals
    surfaced by any other domain.
    """
    summary: list[str] = []

    gs = domains.get("golden_signals", {})
    overall = gs.get("overall_status", "UNKNOWN")
    summary.extend(gs.get("health_signals", []) or [])
    baseline = gs.get("baseline") or {}
    for verdict in baseline.get("verdicts", []) or []:
        if not str(verdict).startswith("✅"):
            summary.append(f"{verdict} (vs last week)")

    correlation = domains.get("recent_changes", {}).get("correlation") or {}
    if correlation.get("correlated_changes"):
        summary.append(str(correlation.get("verdict", "")))

    summary.extend(domains.get("kubernetes", {}).get("health_signals", []) or [])

    total_incidents = domains.get("incidents", {}).get("total_incidents", 0) or 0
    if total_incidents:
        summary.append(f"⚠️ {total_incidents} incident(s) involving this service")

    total_logs = domains.get("logs", {}).get("total_logs", 0) or 0
    if total_logs:
        summary.append(f"⚠️ {total_logs} ERROR-level log line(s) in the window")

    synth = domains.get("synthetics") or {}
    diagnosis = synth.get("diagnosis")
    if diagnosis and diagnosis != "PASSING":
        summary.append(
            f"⚠️ synthetic monitor '{synth.get('monitor_name', '?')}' diagnosis: {diagnosis}"
        )

    for warning in (domains.get("dependencies", {}).get("warnings") or [])[:3]:
        summary.append(f"⚠️ dependency: {warning}")

    summary = [s for s in summary if s]

    has_critical = any(s.startswith("🔴") for s in summary)
    has_warning = any(s.startswith("⚠️") for s in summary)
    if overall == "CRITICAL" or has_critical:
        overall = "CRITICAL"
    elif overall == "WARNING" or has_warning:
        overall = "WARNING"
    elif overall not in ("HEALTHY",):
        overall = "UNKNOWN"
    return overall, summary


async def investigate_service(
    service_name: str,
    since_minutes: int = 60,
    resource_filter: str | None = None,
) -> str:
    """Run a full multi-domain investigation of a service in one call.

    Fans out concurrently to golden signals (with baseline), recent
    changes (with error-onset correlation), error logs, K8s health,
    incidents, dependencies, and — when the account has an associated
    synthetic monitor — monitor status. Returns one structured bundle
    with an aggregated verdict.

    Args:
        service_name: APM service name (fuzzy resolved).
        since_minutes: Health window in minutes (change timeline uses at
            least 240 minutes regardless).
        resource_filter: Optional ``cloud.resource_id`` substring filter.
            Use when multiple deployed apps (e.g. Azure Function Apps)
            share one New Relic entity name — golden signals and logs are
            scoped to the matching sub-resource (other domains are
            entity-level and ignore it).

    Returns:
        JSON string with overall_status, summary verdicts, and a
        ``domains`` object holding each domain's full tool response.
    """
    start = time.time()
    try:
        ctx = AccountContext()
        credentials, intelligence = ctx.get_active()

        safe_name = sanitize_service_name(service_name)
        try:
            resolved_name, was_fuzzy, _ = fuzzy_resolve_service(
                safe_name,
                intelligence.apm.service_names,
                naming_convention=intelligence.naming_convention,
            )
        except Exception:
            resolved_name, was_fuzzy = safe_name, False

        changes_window = max(since_minutes, CHANGES_WINDOW_MIN_MINUTES)
        names = [
            "golden_signals",
            "recent_changes",
            "logs",
            "kubernetes",
            "incidents",
            "dependencies",
        ]
        coros = [
            get_service_golden_signals(
                resolved_name,
                since_minutes,
                resource_filter=resource_filter,
            ),
            get_recent_changes(resolved_name, since_minutes=changes_window),
            search_logs(
                service_name=resolved_name,
                severity="ERROR,FATAL,CRITICAL",
                since_minutes=since_minutes,
                resource_filter=resource_filter,
            ),
            get_k8s_health(service_name=resolved_name, since_minutes=since_minutes),
            get_service_incidents(resolved_name),
            get_service_dependencies(resolved_name),
        ]
        monitor_name = _associated_monitor(intelligence, resolved_name)
        if monitor_name:
            names.append("synthetics")
            coros.append(get_monitor_status(monitor_name, since_minutes))

        raw_results = await asyncio.gather(*coros, return_exceptions=True)
        domains = {name: _parse_domain(raw) for name, raw in zip(names, raw_results)}
        failed_domains = sorted(name for name, d in domains.items() if "error" in d)

        overall, summary = _summarize(domains)

        correlation = domains.get("recent_changes", {}).get("correlation") or {}
        root_cause_hint = ""
        if correlation.get("correlated_changes"):
            root_cause_hint = str(correlation.get("verdict") or "")

        duration_ms = int((time.time() - start) * 1000)
        response: dict = {
            "service_name": resolved_name,
            "since_minutes": since_minutes,
            **({"resource_filter": resource_filter} if resource_filter else {}),
            "overall_status": overall,
            "summary": summary,
            "root_cause_hint": root_cause_hint or None,
            "domains_run": names,
            "failed_domains": failed_domains,
            "domains": domains,
            "duration_ms": duration_ms,
        }
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

        # Record the composite result as the session's investigation of record.
        try:
            error_rate = domains.get("golden_signals", {}).get("errors", {}).get("error_rate_pct")
            account_meta = getattr(intelligence, "account_meta", None)
            snap = InvestigationSnapshot(
                timestamp=datetime.now(timezone.utc),
                account_id=str(credentials.account_id),
                account_name=getattr(account_meta, "name", "") or "",
                service_name=resolved_name,
                bare_name=resolved_name.rsplit("/", 1)[-1],
                namespace=resolved_name.split("/", 1)[0] if "/" in resolved_name else "",
                severity=overall,
                root_cause=root_cause_hint,
                causal_chain="",
                causal_pattern=(
                    "DEPLOY_REGRESSION" if correlation.get("correlated_changes") else "NONE"
                ),
                error_rate=error_rate,
                is_otel=domains.get("golden_signals", {}).get("instrumentation") == "otel",
                since_minutes=since_minutes,
            )
            SessionMemory().record(snap)
        except Exception:
            pass  # Never let session memory break the main tool response.

        return json.dumps(response)

    except Exception as exc:
        return json.dumps(
            {
                "error": str(exc),
                "tool": "investigate_service",
                "hint": "Ensure you are connected. Use get_apm_applications() to list services.",
                "data_available": False,
            }
        )
