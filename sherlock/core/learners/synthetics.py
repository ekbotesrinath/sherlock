"""Synthetic monitoring learner (monitors + per-monitor locations).

Note: monitor→APM-service association is a cross-domain concern and is
performed by the orchestrator after all learners complete.
"""

import asyncio
import logging
from typing import Any

from sherlock.core.intelligence_models import AccountIntelligence, SyntheticMonitorMeta
from sherlock.core.learners.base import QueryRunner
from sherlock.core.naming import _infer_naming_pattern

logger = logging.getLogger("sherlock.learners.synthetics")

GQL_SYNTHETIC_MONITORS = """
{
  actor {
    entitySearch(query: "accountId = %s AND domain = 'SYNTH' AND type = 'MONITOR'") {
      count
      results {
        nextCursor
        entities {
          guid
          name
          alertSeverity
          ... on SyntheticMonitorEntityOutline {
            monitorType
            period
            monitoredUrl
          }
        }
      }
    }
  }
}
"""

NRQL_SYNTHETIC_LOCATIONS = (
    "SELECT latest(locationLabel), latest(result), latest(duration), latest(error)"
    " FROM SyntheticCheck WHERE monitorName = '%s'"
    " FACET locationLabel SINCE 1 hour ago LIMIT 20"
)


async def learn(runner: QueryRunner, intel: AccountIntelligence) -> None:
    """Discover synthetic monitors, their metadata, and per-monitor locations."""
    try:
        monitors_result = await runner.gql(GQL_SYNTHETIC_MONITORS % runner.account_id)
    except Exception as synth_exc:
        logger.warning("Synthetics discovery failed: %s", synth_exc)
        return

    try:
        search = monitors_result.get("data", {}).get("actor", {}).get("entitySearch", {})
        synth_count = search.get("count", 0)
        synth_entities = search.get("results", {}).get("entities", [])

        monitor_types_seen: set[str] = set()
        for ent in synth_entities:
            name = ent.get("name", "")
            if not name:
                continue
            mon_type = ent.get("monitorType", "UNKNOWN")
            monitor_types_seen.add(mon_type)

            meta = SyntheticMonitorMeta(
                guid=ent.get("guid", ""),
                name=name,
                type=mon_type,
                status=ent.get("monitoredUrl") or "ENABLED",
                period=ent.get("period") or "",
                locations=[],
                associated_service=None,  # filled by the orchestrator
            )
            intel.synthetics.monitor_names.append(name)
            intel.synthetics.monitor_map[name] = meta

        intel.synthetics.monitor_types = sorted(monitor_types_seen)
        intel.synthetics.total_count = (
            synth_count if synth_count else len(intel.synthetics.monitor_names)
        )
        intel.synthetics.enabled = intel.synthetics.total_count > 0
        intel.synthetics.naming_pattern = _infer_naming_pattern(intel.synthetics.monitor_names)
        intel.account_meta.synthetics_enabled = intel.synthetics.enabled
        intel.account_meta.synthetics_count = intel.synthetics.total_count

        # Fetch location data for each monitor (parallel, up to 50).
        if intel.synthetics.monitor_names:
            location_tasks = []
            for mon_name in intel.synthetics.monitor_names[:50]:
                location_tasks.append(runner.nrql(NRQL_SYNTHETIC_LOCATIONS % mon_name))

            location_results: list[Any] = await asyncio.gather(
                *location_tasks, return_exceptions=True
            )
            for i, mon_name in enumerate(intel.synthetics.monitor_names[:50]):
                try:
                    if not isinstance(location_results[i], BaseException):
                        locations = []
                        for row in location_results[i]:
                            loc = row.get("locationLabel", row.get("facet", ""))
                            if loc:
                                locations.append(loc)
                        if mon_name in intel.synthetics.monitor_map:
                            intel.synthetics.monitor_map[mon_name].locations = locations
                except Exception:
                    pass
    except Exception as synth_exc:
        logger.warning("Synthetics processing failed: %s", synth_exc)
