"""
Tests for read-only enforcement in client/newrelic.py.

The enforcement parses GraphQL documents (graphql-core) and requires
every operation to be a `query`. Regression coverage for B2: the old
keyword blocklist rejected legitimate queries whose embedded NRQL
contained words like 'update' or 'delete'.
"""

import httpx
import pytest
import respx

from client.newrelic import NerdGraphClient
from core.exceptions import ReadOnlyViolation
from core.nrql_executor import GQL_NRQL_EXECUTE, execute_nrql


@pytest.fixture
def client(mock_credentials) -> NerdGraphClient:
    return NerdGraphClient(mock_credentials)


def _ok_response() -> dict:
    return {"data": {"actor": {"account": {"nrql": {"results": []}}}}}


class TestBlockedOperations:
    """Mutations and other non-query operations must never reach the API."""

    async def test_blocks_mutation(self, client):
        with pytest.raises(ReadOnlyViolation) as exc_info:
            await client.query("mutation { alertsMutingRuleDelete(id: 1) }")
        assert exc_info.value.blocked_keyword == "mutation"

    async def test_blocks_named_mutation(self, client):
        with pytest.raises(ReadOnlyViolation):
            await client.query(
                'mutation Cleanup { entityDelete(guids: ["abc"]) { deletedEntities } }'
            )

    async def test_blocks_mutation_hidden_behind_query(self, client):
        """A document mixing a query and a mutation is still blocked."""
        doc = """
        query A { actor { user { name } } }
        mutation B { dashboardDelete(guid: "x") { status } }
        """
        with pytest.raises(ReadOnlyViolation) as exc_info:
            await client.query(doc)
        assert exc_info.value.blocked_keyword == "mutation"

    async def test_blocks_subscription(self, client):
        with pytest.raises(ReadOnlyViolation) as exc_info:
            await client.query("subscription { anything }")
        assert exc_info.value.blocked_keyword == "subscription"

    async def test_blocks_unparseable_document(self, client):
        with pytest.raises(ReadOnlyViolation) as exc_info:
            await client.query("this is not graphql {{{")
        assert exc_info.value.blocked_keyword == "unparseable"

    async def test_blocks_fragment_only_document(self, client):
        with pytest.raises(ReadOnlyViolation) as exc_info:
            await client.query("fragment F on Actor { user { name } }")
        assert exc_info.value.blocked_keyword == "no-operation"

    async def test_mutation_never_hits_network(self, client):
        """The block happens before any HTTP request is attempted."""
        with respx.mock(assert_all_called=False) as router:
            route = router.post("https://api.newrelic.com/graphql")
            with pytest.raises(ReadOnlyViolation):
                await client.query("mutation { x }")
            assert not route.called


class TestAllowedQueries:
    """Legitimate queries must pass — including NRQL with 'write-ish' words."""

    async def test_allows_anonymous_query_shorthand(self, client):
        with respx.mock() as router:
            router.post("https://api.newrelic.com/graphql").mock(
                return_value=httpx.Response(200, json=_ok_response())
            )
            body = await client.query("{ actor { user { name } } }")
        assert "data" in body

    async def test_allows_named_query_operation(self, client):
        with respx.mock() as router:
            router.post("https://api.newrelic.com/graphql").mock(
                return_value=httpx.Response(200, json=_ok_response())
            )
            body = await client.query(
                "query GetUser { actor { user { name email } } }"
            )
        assert "data" in body

    @pytest.mark.parametrize(
        "nrql",
        [
            # B2 regression: these were all blocked by the old keyword list.
            "SELECT message FROM Log WHERE message LIKE '%failed to update order%'",
            "SELECT count(*) FROM Transaction WHERE name = 'Controller/delete_user'",
            "SELECT * FROM Log WHERE message LIKE '%could not remove item%'",
            "SELECT count(*) FROM Span WHERE http.url LIKE '%/api/create%'",
            "SELECT latest(message) FROM Log WHERE message LIKE '%destroy session%'",
        ],
    )
    async def test_allows_nrql_with_write_keywords(self, client, nrql):
        """Embedded NRQL keywords must not trip read-only enforcement."""
        with respx.mock() as router:
            router.post("https://api.newrelic.com/graphql").mock(
                return_value=httpx.Response(200, json=_ok_response())
            )
            outcome = await execute_nrql(nrql, account_id="123456", client=client)
        assert outcome.ok

    async def test_allows_the_shared_nrql_document(self, client):
        """The executor's variables-based document is a valid query op."""
        client._assert_read_only(GQL_NRQL_EXECUTE)
