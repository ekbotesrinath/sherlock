"""APM + OTel service discovery learner."""

import asyncio
import logging
from typing import Any

from sherlock.core.intelligence_models import AccountIntelligence
from sherlock.core.learners.base import (
    QueryRunner,
    extract_entity_search,
    paginate_entity_search,
)
from sherlock.core.naming import _infer_naming_pattern

logger = logging.getLogger("sherlock.learners.apm")

GQL_APM_ENTITIES = """
{
  actor {
    entitySearch(query: "accountId = %s AND domain = 'APM' AND type = 'APPLICATION'") {
      count
      results {
        nextCursor
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

GQL_OTEL_ENTITIES = """
{
  actor {
    entitySearch(query: "accountId = %s AND domain = 'EXT' AND type = 'SERVICE'") {
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

NRQL_TOP_ERRORS = (
    "SELECT count(*) FROM TransactionError FACET error.class SINCE 7 days ago LIMIT 20"
)


async def learn(runner: QueryRunner, intel: AccountIntelligence) -> None:
    """Discover APM services (all pages), OTel services, and top error classes."""
    results: list[Any] = await asyncio.gather(
        runner.gql(GQL_APM_ENTITIES % runner.account_id),  # 0: APM entities
        runner.gql(GQL_OTEL_ENTITIES % runner.account_id),  # 1: OTel services
        runner.nrql(NRQL_TOP_ERRORS),  # 2: top error classes
        return_exceptions=True,
    )
    apm_result, otel_result, top_errors = results

    # ── APM Services (with pagination) ──
    try:
        if not isinstance(apm_result, BaseException):
            _, api_count = extract_entity_search(apm_result)
            # Paginate to fetch ALL APM entities.
            all_apm_entities = await paginate_entity_search(
                runner,
                "domain = 'APM' AND type = 'APPLICATION'",
                apm_result,
            )
            intel.account_meta.total_apm_services = (
                api_count if api_count else len(all_apm_entities)
            )
            # Rebuild candidates from scratch each refresh.
            # service_guid_candidates holds the full multi-entity picture;
            # service_guids holds the "preferred" GUID for simple lookup.
            intel.apm.service_guid_candidates.clear()
            for ent in all_apm_entities:
                name = ent.get("name", "")
                if not name:
                    continue
                guid = ent.get("guid", "")
                reporting = ent.get("reporting", False) or False
                alert_severity = ent.get("alertSeverity", "") or ""
                tags_raw = ent.get("tags", []) or []
                tags = {t["key"]: t.get("values", []) for t in tags_raw if t.get("key")}

                guid_candidate = {
                    "guid": guid,
                    "reporting": bool(reporting),
                    "tags": tags,
                    "alert_severity": alert_severity,
                }
                intel.apm.service_guid_candidates.setdefault(name, []).append(guid_candidate)

                if name not in intel.apm.service_names:
                    intel.apm.service_names.append(name)

                if guid and reporting:
                    intel.apm.reporting_guids.add(guid)

                lang = tags.get("language", [""])[0] if tags.get("language") else ""
                if lang and name not in intel.apm.service_languages:
                    intel.apm.service_languages[name] = lang
                envs = tags.get("environment", []) or []
                for e in envs:
                    if e and e not in intel.apm.environments:
                        intel.apm.environments.append(e)

            # Build the preferred service_guids map: prefer reporting entities,
            # then the one with the most-severe active alert_severity, then the
            # first one seen.
            _severity_rank = {
                "CRITICAL": 3,
                "WARNING": 2,
                "NOT_ALERTING": 1,
                "": 0,
                "NOT_CONFIGURED": 0,
            }
            intel.apm.service_guids.clear()
            for name, cands in intel.apm.service_guid_candidates.items():
                best = sorted(
                    cands,
                    key=lambda c: (
                        1 if c.get("reporting") else 0,
                        _severity_rank.get(c.get("alert_severity", ""), 0),
                    ),
                    reverse=True,
                )[0]
                if best.get("guid"):
                    intel.apm.service_guids[name] = best["guid"]

            intel.apm.naming_pattern = _infer_naming_pattern(intel.apm.service_names)
    except Exception:
        pass

    # ── OpenTelemetry Services ──
    try:
        if not isinstance(otel_result, BaseException):
            entities, count = extract_entity_search(otel_result)
            intel.otel.service_count = count if count else len(entities)
            intel.otel.service_names = [e["name"] for e in entities if e.get("name")]
            intel.otel.enabled = intel.otel.service_count > 0
            intel.account_meta.otel_services = intel.otel.service_count
    except Exception:
        pass

    # ── Top Error Classes ──
    try:
        if not isinstance(top_errors, BaseException) and top_errors:
            intel.apm.top_error_classes = [
                r.get("error.class", r.get("facet", ""))
                for r in top_errors
                if r.get("error.class") or r.get("facet")
            ][:20]
    except Exception:
        pass
