"""
NRQL query execution tool for Sherlock.

Provides a raw NRQL query tool that validates, sanitizes, and executes
NRQL queries against the active account via NerdGraph. Supports per-call
account targeting for cross-account investigations and field-level
truncation to keep oversized payloads (header dumps, stack traces) out
of the conversation.
"""

import json
import logging
import time

from sherlock.core.nrql_executor import execute_nrql
from sherlock.core.target import resolve_call_target
from sherlock.core.utils import DEFAULT_MAX_FIELD_LENGTH, truncate_result_fields

logger = logging.getLogger("sherlock.tools.nrql")

# Maximum NRQL query length.
MAX_NRQL_LENGTH = 4000


async def run_nrql_query(
    nrql: str,
    account_id: str | None = None,
    profile: str | None = None,
    max_field_length: int = DEFAULT_MAX_FIELD_LENGTH,
) -> str:
    """Execute a raw NRQL query against a New Relic account.

    Runs against the active account by default. For cross-account work,
    pass ``account_id`` or ``profile`` to target another account for this
    one call — credentials come from the saved keyring profile and the
    active connection is left untouched, so no connect_account round-trip
    is needed.

    Args:
        nrql: The NRQL query string to execute.
        account_id: Optional account ID to run this query against
            (must have a saved profile). Defaults to the active account.
        profile: Optional saved profile name to run this query against.
            Takes precedence over account_id.
        max_field_length: Maximum characters per string field in results.
            Longer values are truncated with a marker showing the original
            length. Pass 0 for full untruncated values.

    Returns:
        JSON string with query results or error information.
    """
    start = time.time()
    try:
        credentials, _, client, overridden = resolve_call_target(
            account_id=account_id,
            profile=profile,
        )

        # Basic validation.
        if not nrql or not nrql.strip():
            return json.dumps(
                {
                    "error": "Empty NRQL query.",
                    "tool": "run_nrql_query",
                    "hint": "Provide a valid NRQL query. Use get_nrql_context() first.",
                    "data_available": False,
                }
            )

        if len(nrql) > MAX_NRQL_LENGTH:
            return json.dumps(
                {
                    "error": f"NRQL query too long ({len(nrql)} chars, max {MAX_NRQL_LENGTH}).",
                    "tool": "run_nrql_query",
                    "hint": "Simplify your query.",
                    "data_available": False,
                }
            )

        # NRQL travels as a GraphQL variable — no escaping needed.
        outcome = await execute_nrql(
            nrql,
            account_id=credentials.account_id,
            client=client,
        )
        nrql_results = outcome.results
        warnings = outcome.errors

        nrql_results, truncated_fields = truncate_result_fields(nrql_results, max_field_length)

        duration_ms = int((time.time() - start) * 1000)
        logger.info("NRQL executed in %dms, %d results", duration_ms, len(nrql_results))

        # Note: NRQL query builder deep links were retired (NR redirects to
        # Notebooks). The NRQL string is included below for copy-paste into
        # the "Query your data" panel. Domain-specific tools (golden_signals,
        # k8s, logs, alerts, synthetics) return proper entity-view links in
        # their `links` dict — prefer those for deep links in reports.

        response: dict = {
            "nrql": nrql,
            "account_id": credentials.account_id,
            "result_count": len(nrql_results),
            "results": nrql_results,
            "open_in_new_relic": None,
            "note": "Copy the NRQL query into the 'Query your data' panel in New Relic. "
            "For clickable deep links, use domain-specific tools "
            "(get_service_golden_signals, get_k8s_health, search_logs, "
            "get_incidents, get_monitor_status) which return verified links.",
            "warnings": warnings,
            "duration_ms": duration_ms,
        }
        if overridden:
            response["account_override"] = True
        if truncated_fields:
            response["truncated_fields"] = truncated_fields
            response["truncation_note"] = (
                f"{truncated_fields} string field(s) longer than {max_field_length} chars "
                "were shortened (original lengths are in the '[truncated, N chars total]' "
                "markers). Re-run with max_field_length=0 for full values."
            )
        return json.dumps(response)

    except Exception as exc:
        duration_ms = int((time.time() - start) * 1000)
        logger.error("NRQL query failed: %s", exc)
        return json.dumps(
            {
                "error": str(exc),
                "tool": "run_nrql_query",
                "hint": "Check NRQL syntax. Use get_nrql_context() for valid attribute names.",
                "data_available": False,
                "duration_ms": duration_ms,
            }
        )
