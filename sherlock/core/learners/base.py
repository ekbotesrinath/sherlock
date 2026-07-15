"""
Shared query plumbing for the per-domain learners.

QueryRunner executes NerdGraph GraphQL documents and NRQL (as a GraphQL
variable) against one account. Entity-search helpers extract results and
paginate past the ~200-entity page limit.
"""

import logging
from typing import Any

from sherlock.client.newrelic import NerdGraphClient
from sherlock.core.credentials import Credentials
from sherlock.core.nrql_executor import execute_nrql

logger = logging.getLogger("sherlock.learners")

GQL_ENTITY_SEARCH_PAGINATED = """
{
  actor {
    entitySearch(query: "accountId = %s AND %s") {
      results%s {
        nextCursor
        entities {
          guid
          name
          type
          domain
          tags {
            key
            values
          }
        }
      }
    }
  }
}
"""


class QueryRunner:
    """Executes NerdGraph queries for one account's credentials.

    Delegates to NerdGraphClient, so learner queries get the same pooled
    HTTP connections, retry policy, and read-only enforcement as tools.
    """

    def __init__(self, credentials: Credentials) -> None:
        self.account_id = credentials.account_id
        self._client = NerdGraphClient(credentials)

    async def gql(self, query: str, variables: dict | None = None) -> dict:
        """Execute a NerdGraph GraphQL document."""
        return await self._client.query(query, variables=variables)

    async def nrql(self, nrql: str) -> list:
        """Execute an NRQL query via NerdGraph (NRQL travels as a variable)."""
        outcome = await execute_nrql(
            nrql,
            account_id=self.account_id,
            client=self._client,
        )
        return outcome.results


def extract_entity_search(result: dict) -> tuple[list, int]:
    """Extract entities list and count from an entitySearch result."""
    search = result.get("data", {}).get("actor", {}).get("entitySearch", {})
    count = search.get("count", 0)
    entities = search.get("results", {}).get("entities", [])
    return entities, count


def extract_cursor(result: dict) -> str | None:
    """Extract nextCursor from an entitySearch result, if present."""
    try:
        cursor = (
            result.get("data", {})
            .get("actor", {})
            .get("entitySearch", {})
            .get("results", {})
            .get("nextCursor")
        )
        return str(cursor) if cursor is not None else None
    except Exception:
        return None


async def paginate_entity_search(
    runner: QueryRunner,
    filter_str: str,
    first_page_result: dict,
) -> list[dict]:
    """Fetch ALL entities across pages (first page already fetched).

    New Relic returns ~200 entities per page. For accounts with more
    entities this ensures we don't silently miss any.
    """
    entities, _ = extract_entity_search(first_page_result)
    all_entities: list[Any] = list(entities)
    cursor = extract_cursor(first_page_result)
    max_pages = 20  # safety limit: 20 * 200 = 4000 entities

    page = 1
    while cursor and page < max_pages:
        cursor_clause = '(cursor: "%s")' % cursor
        query = GQL_ENTITY_SEARCH_PAGINATED % (
            runner.account_id,
            filter_str,
            cursor_clause,
        )
        try:
            result = await runner.gql(query)
            page_entities, _ = extract_entity_search(result)
            if not page_entities:
                break
            all_entities.extend(page_entities)
            cursor = extract_cursor(result)
            page += 1
        except Exception:
            logger.debug("Entity pagination stopped at page %d", page)
            break

    logger.info(
        "Entity pagination: loaded %d entities across %d page(s)",
        len(all_entities),
        page,
    )
    return all_entities
