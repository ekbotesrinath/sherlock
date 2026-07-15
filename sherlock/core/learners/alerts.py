"""Alert policy learner."""

import logging

from sherlock.core.intelligence_models import AccountIntelligence
from sherlock.core.learners.base import QueryRunner
from sherlock.core.naming import _infer_naming_pattern

logger = logging.getLogger("sherlock.learners.alerts")

GQL_ALERT_POLICIES = """
{
  actor {
    account(id: %s) {
      alerts {
        policiesSearch {
          policies {
            id
            name
          }
          totalCount
        }
      }
    }
  }
}
"""


async def learn(runner: QueryRunner, intel: AccountIntelligence) -> None:
    """Discover alert policy names."""
    try:
        result = await runner.gql(GQL_ALERT_POLICIES % runner.account_id)
        policy_search = (
            result.get("data", {})
            .get("actor", {})
            .get("account", {})
            .get("alerts", {})
            .get("policiesSearch", {})
        )
        policies = policy_search.get("policies", [])
        intel.alerts.policy_names = [p["name"] for p in policies if p.get("name")]
        intel.alerts.naming_pattern = _infer_naming_pattern(intel.alerts.policy_names)
    except Exception:
        pass
