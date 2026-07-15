"""
Single NRQL execution path for Sherlock.

Every tool that runs NRQL goes through execute_nrql(), which sends the
NRQL string as a GraphQL VARIABLE instead of interpolating it into the
GraphQL document. This removes the whole class of GraphQL-injection /
quote-escaping bugs that the old per-file `GQL_NRQL_QUERY % (...)`
copy-paste pattern had.
"""

import logging
from dataclasses import dataclass, field
from typing import Any

from sherlock.client.newrelic import NerdGraphClient, get_client

logger = logging.getLogger("sherlock.nrql_executor")

# The ONE GraphQL document used for all NRQL execution. The account id and
# NRQL string travel as typed variables — never string-formatted in.
GQL_NRQL_EXECUTE = """
query SherlockNrql($accountId: Int!, $nrql: Nrql!) {
  actor {
    account(id: $accountId) {
      nrql(query: $nrql) {
        results
      }
    }
  }
}
"""


# COMPARE WITH queries return their data via currentResults/previousResults
# instead of results — NerdGraph leaves `results` empty for them.
GQL_NRQL_COMPARE = """
query SherlockNrqlCompare($accountId: Int!, $nrql: Nrql!) {
  actor {
    account(id: $accountId) {
      nrql(query: $nrql) {
        currentResults
        previousResults
      }
    }
  }
}
"""


@dataclass
class NrqlResult:
    """Outcome of one NRQL execution via NerdGraph."""

    results: list[dict] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        """True when the query produced no NerdGraph errors."""
        return not self.errors


@dataclass
class NrqlCompareResult:
    """Outcome of one COMPARE WITH NRQL execution via NerdGraph."""

    current: list[dict] = field(default_factory=list)
    previous: list[dict] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        """True when the query produced no NerdGraph errors."""
        return not self.errors


async def execute_nrql(
    nrql: str,
    account_id: str | int | None = None,
    client: NerdGraphClient | None = None,
    timeout_override: int | None = None,
) -> NrqlResult:
    """Execute an NRQL query against a New Relic account via NerdGraph.

    Args:
        nrql: The NRQL query string. Sent as a GraphQL variable, so it
            needs no quote escaping.
        account_id: Numeric account ID. Defaults to the active account.
        client: Optional NerdGraphClient. Defaults to the active-context
            client (read-only enforcement still applies inside it).
        timeout_override: Optional per-request timeout in seconds.

    Returns:
        NrqlResult with results, NerdGraph error messages, and raw body.

    Raises:
        ValueError: If account_id is not numeric.
        NotConnectedError: If no account is active and none is given.
        ReadOnlyViolation: If the GraphQL document is not read-only.
    """
    if client is None:
        client = get_client()
    if account_id is None:
        account_id = client.account_id

    account_id_str = str(account_id).strip()
    if not account_id_str.isdigit():
        raise ValueError(f"account_id must be numeric, got {account_id!r}")

    body = await client.query(
        GQL_NRQL_EXECUTE,
        variables={"accountId": int(account_id_str), "nrql": nrql},
        timeout_override=timeout_override,
    )

    results = (body.get("data", {}).get("actor", {}).get("account", {}).get("nrql", {}) or {}).get(
        "results"
    ) or []

    errors = [e.get("message", str(e)) for e in body.get("errors", [])]
    if errors:
        logger.warning("NRQL execution returned NerdGraph errors: %s", errors[0])

    return NrqlResult(results=results, errors=errors, raw=body)


async def execute_nrql_compare(
    nrql: str,
    account_id: str | int | None = None,
    client: NerdGraphClient | None = None,
    timeout_override: int | None = None,
) -> NrqlCompareResult:
    """Execute a COMPARE WITH NRQL query via NerdGraph.

    Same contract as :func:`execute_nrql`, but reads the
    ``currentResults``/``previousResults`` fields that NerdGraph populates
    for COMPARE WITH queries (``results`` is empty for them).

    Args:
        nrql: NRQL query string containing a COMPARE WITH clause.
        account_id: Numeric account ID. Defaults to the active account.
        client: Optional NerdGraphClient. Defaults to the active-context
            client (read-only enforcement still applies inside it).
        timeout_override: Optional per-request timeout in seconds.

    Returns:
        NrqlCompareResult with current/previous rows, errors, and raw body.

    Raises:
        ValueError: If account_id is not numeric.
        NotConnectedError: If no account is active and none is given.
        ReadOnlyViolation: If the GraphQL document is not read-only.
    """
    if client is None:
        client = get_client()
    if account_id is None:
        account_id = client.account_id

    account_id_str = str(account_id).strip()
    if not account_id_str.isdigit():
        raise ValueError(f"account_id must be numeric, got {account_id!r}")

    body = await client.query(
        GQL_NRQL_COMPARE,
        variables={"accountId": int(account_id_str), "nrql": nrql},
        timeout_override=timeout_override,
    )

    container = body.get("data", {}).get("actor", {}).get("account", {}).get("nrql", {}) or {}
    current = container.get("currentResults") or []
    previous = container.get("previousResults") or []

    errors = [e.get("message", str(e)) for e in body.get("errors", [])]
    if errors:
        logger.warning("COMPARE WITH execution returned NerdGraph errors: %s", errors[0])

    return NrqlCompareResult(current=current, previous=previous, errors=errors, raw=body)
