"""Account-wide entity learner.

Covers account metadata, full entity-type counts, and the auxiliary
domains that are counts/name-lists only: infrastructure hosts, containers,
browser apps, mobile apps, workloads, and key transactions.
"""

import asyncio
import logging
from typing import Any

from sherlock.core.intelligence_models import AccountIntelligence, EntityTypeSummary
from sherlock.core.learners.base import QueryRunner, extract_entity_search

logger = logging.getLogger("sherlock.learners.entities")

GQL_ACCOUNT_META = """
{
  actor {
    account(id: %s) {
      name
    }
  }
}
"""

# Single query to get entity counts by domain/type for the entire account.
GQL_ENTITY_TYPE_COUNTS = """
{
  actor {
    entitySearch(query: "accountId = %s") {
      count
      types {
        domain
        type
        count
      }
    }
  }
}
"""

# Paginated entity search template — use cursor for pagination.
# %s = account_id, %s = domain/type filter, %s = cursor clause

GQL_BROWSER_ENTITIES = """
{
  actor {
    entitySearch(query: "accountId = %s AND domain = 'BROWSER' AND type = 'APPLICATION'") {
      count
      results {
        entities {
          guid
          name
        }
      }
    }
  }
}
"""

GQL_MOBILE_ENTITIES = """
{
  actor {
    entitySearch(query: "accountId = %s AND domain = 'MOBILE' AND type = 'APPLICATION'") {
      count
      results {
        entities {
          guid
          name
        }
      }
    }
  }
}
"""

GQL_INFRA_ENTITIES = """
{
  actor {
    entitySearch(query: "accountId = %s AND domain = 'INFRA' AND type = 'HOST'") {
      count
      results {
        entities {
          guid
          name
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

GQL_CONTAINER_ENTITIES = """
{
  actor {
    entitySearch(query: "accountId = %s AND domain = 'INFRA' AND type = 'CONTAINER'") {
      count
      results {
        entities {
          guid
          name
        }
      }
    }
  }
}
"""

GQL_WORKLOAD_ENTITIES = """
{
  actor {
    entitySearch(query: "accountId = %s AND domain = 'NR1' AND type = 'WORKLOAD'") {
      count
      results {
        entities {
          guid
          name
        }
      }
    }
  }
}
"""

GQL_KEY_TRANSACTION_ENTITIES = """
{
  actor {
    entitySearch(query: "accountId = %s AND type = 'KEY_TRANSACTION'") {
      count
      results {
        entities {
          guid
          name
        }
      }
    }
  }
}
"""


async def learn(runner: QueryRunner, intel: AccountIntelligence) -> None:
    """Discover account meta, entity counts, and auxiliary entity domains."""
    results: list[Any] = await asyncio.gather(
        runner.gql(GQL_ACCOUNT_META % runner.account_id),  # 0: account meta
        runner.gql(GQL_ENTITY_TYPE_COUNTS % runner.account_id),  # 1: type counts
        runner.gql(GQL_INFRA_ENTITIES % runner.account_id),  # 2: infra hosts
        runner.gql(GQL_CONTAINER_ENTITIES % runner.account_id),  # 3: containers
        runner.gql(GQL_BROWSER_ENTITIES % runner.account_id),  # 4: browser apps
        runner.gql(GQL_MOBILE_ENTITIES % runner.account_id),  # 5: mobile apps
        runner.gql(GQL_WORKLOAD_ENTITIES % runner.account_id),  # 6: workloads
        runner.gql(GQL_KEY_TRANSACTION_ENTITIES % runner.account_id),  # 7: key txns
        return_exceptions=True,
    )

    # ── Account Meta ──
    try:
        if not isinstance(results[0], BaseException):
            account_data = results[0].get("data", {}).get("actor", {}).get("account", {})
            intel.account_meta.name = account_data.get("name", "")
    except Exception:
        pass

    # ── Entity Type Counts ──
    try:
        if not isinstance(results[1], BaseException):
            search = results[1].get("data", {}).get("actor", {}).get("entitySearch", {})
            intel.entity_counts.total_entities = search.get("count", 0)
            intel.account_meta.total_entities = intel.entity_counts.total_entities

            azure_count = 0
            azure_types: list[str] = []
            for t in search.get("types", []):
                domain = t.get("domain", "")
                etype = t.get("type", "")
                ecount = t.get("count", 0)
                intel.entity_counts.type_breakdown.append(
                    EntityTypeSummary(domain=domain, type=etype, count=ecount)
                )
                # Identify Azure resources
                if domain == "INFRA" and etype.startswith("AZURE"):
                    azure_count += ecount
                    azure_types.append(etype)
                # Service levels
                if etype == "SERVICE_LEVEL":
                    intel.entity_counts.service_level_count = ecount
                # Issues
                if domain == "AIOPS" or etype == "ISSUE":
                    intel.entity_counts.issue_count += ecount

            intel.entity_counts.azure_resource_count = azure_count
            intel.entity_counts.azure_resource_types = sorted(azure_types)
            intel.account_meta.azure_resource_count = azure_count
    except Exception:
        pass

    # ── Infrastructure ──
    try:
        if not isinstance(results[2], BaseException):
            entities, count = extract_entity_search(results[2])
            intel.infra.host_count = count if count else len(entities)
            providers = set()
            regions = set()
            for host in entities:
                tags = {t["key"]: t.get("values", []) for t in host.get("tags", [])}
                if "aws" in str(tags).lower():
                    providers.add("AWS")
                elif "azure" in str(tags).lower():
                    providers.add("Azure")
                elif "gcp" in str(tags).lower() or "google" in str(tags).lower():
                    providers.add("GCP")
                for r in tags.get("region", []):
                    if r:
                        regions.add(r)

            intel.infra.cloud_provider = ", ".join(sorted(providers)) if providers else None
            intel.infra.regions = sorted(regions)
    except Exception:
        pass

    # ── Containers ──
    try:
        if not isinstance(results[3], BaseException):
            _, count = extract_entity_search(results[3])
            intel.infra.container_count = count
            intel.account_meta.container_count = count
    except Exception:
        pass

    # ── Browser Apps ──
    try:
        if not isinstance(results[4], BaseException):
            entities, count = extract_entity_search(results[4])
            intel.browser.app_names = [e["name"] for e in entities if e.get("name")]
            intel.browser.enabled = (count or len(intel.browser.app_names)) > 0
    except Exception:
        pass

    # ── Mobile Apps ──
    try:
        if not isinstance(results[5], BaseException):
            entities, count = extract_entity_search(results[5])
            intel.mobile.app_names = [e["name"] for e in entities if e.get("name")]
            intel.mobile.app_count = count if count else len(intel.mobile.app_names)
            intel.mobile.enabled = intel.mobile.app_count > 0
            intel.account_meta.mobile_apps = intel.mobile.app_count
    except Exception:
        pass

    # ── Workloads ──
    try:
        if not isinstance(results[6], BaseException):
            entities, count = extract_entity_search(results[6])
            intel.workloads.workload_names = [e["name"] for e in entities if e.get("name")]
            intel.workloads.workload_count = count if count else len(intel.workloads.workload_names)
            intel.workloads.enabled = intel.workloads.workload_count > 0
            intel.account_meta.workload_count = intel.workloads.workload_count
    except Exception:
        pass

    # ── Key Transactions ──
    try:
        if not isinstance(results[7], BaseException):
            _, count = extract_entity_search(results[7])
            intel.entity_counts.key_transaction_count = count
            intel.account_meta.key_transaction_count = count
    except Exception:
        pass
