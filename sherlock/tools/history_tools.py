"""
Incident history tool for Sherlock.

Exposes the persistent incident memory (.sherlock/history.jsonl) as an
MCP tool: match the service and symptoms being investigated right now
against past incidents, across MCP server restarts.
"""

import json
import logging
import time

from sherlock.core.context import AccountContext
from sherlock.core.incident_history import find_similar, load_history
from sherlock.core.sanitize import fuzzy_resolve_service, sanitize_service_name

logger = logging.getLogger("sherlock.tools.history")


async def find_similar_incidents(
    service_name: str,
    symptom: str = "",
    limit: int = 5,
) -> str:
    """Find past incidents similar to what is being investigated now.

    Searches the persistent incident history (survives restarts) for the
    active account, scoring matches on service name, symptom keyword
    overlap with recorded root causes, and known causal patterns.

    Args:
        service_name: Service being investigated (fuzzy resolved).
        symptom: Optional free-text symptoms (e.g. 'error spike after
            deploy, pods restarting').
        limit: Maximum matches to return.

    Returns:
        JSON string with scored matches and a verdict line.
    """
    start = time.time()
    try:
        ctx = AccountContext()
        credentials, intelligence = ctx.get_active()

        safe_name = sanitize_service_name(service_name)
        resolved_name = safe_name
        was_fuzzy = False
        try:
            resolved_name, was_fuzzy, _ = fuzzy_resolve_service(
                safe_name,
                intelligence.apm.service_names,
                threshold=0.5,
                naming_convention=intelligence.naming_convention,
            )
        except Exception:
            resolved_name = safe_name

        account_id = str(credentials.account_id)
        history_size = len(load_history(account_id=account_id))
        matches = find_similar(
            resolved_name,
            account_id=account_id,
            symptom=symptom,
            limit=max(1, min(limit, 20)),
        )

        if matches:
            top = matches[0]
            root = top.get("root_cause") or "no root cause was recorded"
            verdict = (
                f"Most similar: {top.get('service_name')} {top.get('severity')} "
                f"{top.get('when')} — {root}"
            )
        elif history_size:
            verdict = (
                f"No similar past incidents among {history_size} history "
                f"record(s) for this account."
            )
        else:
            verdict = (
                "No incident history for this account yet — history builds "
                "up as investigations run."
            )

        duration_ms = int((time.time() - start) * 1000)
        response: dict = {
            "service_name": resolved_name,
            "symptom": symptom or None,
            "history_records_searched": history_size,
            "total_matches": len(matches),
            "matches": matches,
            "verdict": verdict,
            "duration_ms": duration_ms,
        }
        if was_fuzzy:
            response["resolved_from"] = service_name
            response["note"] = f"Fuzzy matched '{service_name}' → '{resolved_name}'"
        return json.dumps(response)

    except Exception as exc:
        return json.dumps(
            {
                "error": str(exc),
                "tool": "find_similar_incidents",
                "hint": "Ensure you are connected to an account first.",
                "data_available": False,
            }
        )
