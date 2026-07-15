"""
Tests for core/nrql_executor.py — the single NRQL execution path.

Verifies that NRQL travels as a GraphQL variable (never string-interpolated
into the document), that account IDs are validated, and that NerdGraph
errors surface on the result object.
"""

import json

import httpx
import pytest
import respx

from client.newrelic import NerdGraphClient
from core.credentials import Credentials
from core.nrql_executor import GQL_NRQL_EXECUTE, execute_nrql


def _nrql_response(results: list) -> dict:
    return {"data": {"actor": {"account": {"nrql": {"results": results}}}}}


class TestExecuteNrql:
    """Behavior of execute_nrql with an explicit client."""

    async def test_sends_nrql_as_graphql_variable(self, mock_credentials):
        """The NRQL string must be in variables, not in the document."""
        client = NerdGraphClient(mock_credentials)
        nrql = "SELECT count(*) FROM Transaction WHERE appName = 'payment \"svc\"'"

        with respx.mock() as router:
            route = router.post("https://api.newrelic.com/graphql").mock(
                return_value=httpx.Response(200, json=_nrql_response([{"count": 7}]))
            )
            outcome = await execute_nrql(nrql, account_id="123456", client=client)

        payload = json.loads(route.calls.last.request.content)
        assert payload["query"] == GQL_NRQL_EXECUTE
        assert payload["variables"] == {"accountId": 123456, "nrql": nrql}
        # The raw NRQL (quotes included) never appears in the document.
        assert "payment" not in payload["query"]
        assert outcome.results == [{"count": 7}]
        assert outcome.ok

    async def test_defaults_account_id_from_client(self, mock_credentials):
        client = NerdGraphClient(mock_credentials)

        with respx.mock() as router:
            route = router.post("https://api.newrelic.com/graphql").mock(
                return_value=httpx.Response(200, json=_nrql_response([]))
            )
            await execute_nrql("SELECT 1 FROM Transaction", client=client)

        payload = json.loads(route.calls.last.request.content)
        assert payload["variables"]["accountId"] == 123456

    async def test_rejects_non_numeric_account_id(self, mock_credentials):
        client = NerdGraphClient(mock_credentials)
        with pytest.raises(ValueError, match="account_id must be numeric"):
            await execute_nrql(
                "SELECT 1 FROM Transaction",
                account_id="123) { } mutation {",
                client=client,
            )

    async def test_surfaces_nerdgraph_errors(self, mock_credentials):
        client = NerdGraphClient(mock_credentials)
        body = {
            "data": {"actor": {"account": {"nrql": None}}},
            "errors": [{"message": "NRQL Syntax Error at line 1"}],
        }

        with respx.mock() as router:
            router.post("https://api.newrelic.com/graphql").mock(
                return_value=httpx.Response(200, json=body)
            )
            outcome = await execute_nrql(
                "SELEC oops", account_id="123456", client=client
            )

        assert outcome.results == []
        assert outcome.errors == ["NRQL Syntax Error at line 1"]
        assert not outcome.ok

    async def test_uses_active_context_client_by_default(
        self, mock_context, mock_nerdgraph
    ):
        """Without an explicit client, the active account context is used."""
        outcome = await execute_nrql("SELECT count(*) FROM Transaction")
        assert outcome.results == []
        assert outcome.ok


class TestCredentialsAccountIdValidation:
    """Credentials must reject account IDs that could alter GraphQL documents."""

    def test_accepts_numeric_account_id(self):
        creds = Credentials(account_id="123456", api_key="NRAK-x")
        assert creds.account_id == "123456"

    def test_strips_whitespace(self):
        creds = Credentials(account_id=" 123456 ", api_key="NRAK-x")
        assert creds.account_id == "123456"

    @pytest.mark.parametrize(
        "bad_id", ["", "abc", "123abc", "123) { name }", "1 OR 1=1", "-1"]
    )
    def test_rejects_non_numeric_account_id(self, bad_id):
        with pytest.raises(ValueError, match="account_id must be numeric"):
            Credentials(account_id=bad_id, api_key="NRAK-x")
