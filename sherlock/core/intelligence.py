"""
Account intelligence orchestration for Sherlock.

learn_account() runs all per-domain learners (sherlock.core.learners.*)
concurrently, then applies the cross-domain passes that need results from
multiple domains: monitor→service association, naming-convention learning,
and cross-account entity detection.

This module also re-exports the intelligence models and helpers so
existing imports (``from sherlock.core.intelligence import ...``) keep
working after the split.
"""

import asyncio
import base64
import logging

from sherlock.core.credentials import Credentials
from sherlock.core.exceptions import IntelligenceError
from sherlock.core.intelligence_models import (
    AccessibleAccount,
    AccountIntelligence,
    AccountMeta,
    AlertsIntelligence,
    APMIntelligence,
    AzureServiceBusIntelligence,
    AzureServiceBusQueueMeta,
    AzureServiceBusTopicMeta,
    BrowserIntelligence,
    CrossAccountEntity,
    EntityCountsSummary,
    EntityTypeSummary,
    InfraIntelligence,
    K8sIntelligence,
    LogsIntelligence,
    MobileIntelligence,
    NamingConvention,
    OTelIntelligence,
    SyntheticMonitorMeta,
    SyntheticsIntelligence,
    WorkloadIntelligence,
)
from sherlock.core.learners import (
    alerts as alerts_learner,
)
from sherlock.core.learners import (
    apm as apm_learner,
)
from sherlock.core.learners import (
    asb as asb_learner,
)
from sherlock.core.learners import (
    entities as entities_learner,
)
from sherlock.core.learners import (
    k8s as k8s_learner,
)
from sherlock.core.learners import (
    logs as logs_learner,
)
from sherlock.core.learners import (
    synthetics as synthetics_learner,
)
from sherlock.core.learners.asb import _parse_asb_intelligence
from sherlock.core.learners.base import QueryRunner
from sherlock.core.learners.entities import GQL_ENTITY_TYPE_COUNTS
from sherlock.core.learners.logs import (
    KNOWN_PLATFORM_NAMESPACES,
    SERVICE_ATTR_CANDIDATES,
    SEVERITY_ATTR_CANDIDATES,
    _parse_log_namespace_intelligence,
)
from sherlock.core.naming import (
    _common_prefix,
    _infer_associated_service,
    _infer_naming_pattern,
    _learn_naming_convention,
)

__all__ = [
    "AccessibleAccount",
    "AccountIntelligence",
    "AccountMeta",
    "AlertsIntelligence",
    "APMIntelligence",
    "AzureServiceBusIntelligence",
    "AzureServiceBusQueueMeta",
    "AzureServiceBusTopicMeta",
    "BrowserIntelligence",
    "CrossAccountEntity",
    "EntityCountsSummary",
    "EntityTypeSummary",
    "InfraIntelligence",
    "K8sIntelligence",
    "KNOWN_PLATFORM_NAMESPACES",
    "LogsIntelligence",
    "MobileIntelligence",
    "NamingConvention",
    "OTelIntelligence",
    "SERVICE_ATTR_CANDIDATES",
    "SEVERITY_ATTR_CANDIDATES",
    "SyntheticMonitorMeta",
    "SyntheticsIntelligence",
    "WorkloadIntelligence",
    "_common_prefix",
    "_infer_associated_service",
    "_infer_naming_pattern",
    "_learn_naming_convention",
    "_parse_asb_intelligence",
    "_parse_log_namespace_intelligence",
    "decode_entity_guid",
    "detect_cross_account_entities",
    "discover_accounts",
    "learn_account",
]

logger = logging.getLogger("sherlock.intelligence")

GQL_ACCESSIBLE_ACCOUNTS = """
{
  actor {
    accounts {
      id
      name
    }
  }
}
"""


async def discover_accounts(credentials: Credentials) -> list[AccessibleAccount]:
    """Discover all accounts accessible to the given API key.

    Makes a single NerdGraph call to list accounts, then optionally
    fetches entity counts for each.

    Args:
        credentials: Credentials (any valid account_id + api_key).

    Returns:
        List of accessible accounts with entity counts.
    """
    from sherlock.client.newrelic import NerdGraphClient

    client = NerdGraphClient(credentials)

    async def _gql(query: str) -> dict:
        return await client.query(query)

    # Get all accessible accounts
    result = await _gql(GQL_ACCESSIBLE_ACCOUNTS)
    raw_accounts = result.get("data", {}).get("actor", {}).get("accounts", [])

    accounts = [
        AccessibleAccount(id=str(a["id"]), name=a.get("name", ""))
        for a in raw_accounts
        if a.get("id")
    ]

    # Fetch entity counts for each account in parallel
    if accounts:
        count_tasks = [_gql(GQL_ENTITY_TYPE_COUNTS % a.id) for a in accounts]
        count_results = await asyncio.gather(*count_tasks, return_exceptions=True)
        for i, acct in enumerate(accounts):
            try:
                count_result = count_results[i]
                if not isinstance(count_result, BaseException):
                    count = (
                        count_result.get("data", {})
                        .get("actor", {})
                        .get("entitySearch", {})
                        .get("count", 0)
                    )
                    acct.entity_count = count
            except Exception:
                pass

    # Sort by entity count descending
    accounts.sort(key=lambda a: a.entity_count, reverse=True)
    return accounts


def decode_entity_guid(guid: str) -> dict:
    """Decode a New Relic entity GUID to its components.

    Entity GUIDs are base64-encoded strings in the format:
    ``account_id|entity_type|domain|entity_id``

    Args:
        guid: The base64-encoded entity GUID.

    Returns:
        Dict with ``account_id``, ``entity_type``, and ``domain`` keys,
        or empty dict if decoding fails.
    """
    try:
        padded = guid + "=" * (-len(guid) % 4)
        decoded = base64.b64decode(padded.encode()).decode("utf-8")
        parts = decoded.split("|")
        if len(parts) >= 3:
            return {
                "account_id": parts[0],
                "entity_type": parts[1],
                "domain": parts[2],
            }
    except Exception:
        pass
    return {}


def detect_cross_account_entities(
    intelligence: "AccountIntelligence",
) -> list[CrossAccountEntity]:
    """Scan all discovered entity GUIDs for cross-account references.

    Compares the account ID encoded in each entity GUID against the
    connected account ID. Any mismatch indicates the entity lives in a
    different New Relic account.

    Args:
        intelligence: The learned AccountIntelligence to scan.

    Returns:
        List of CrossAccountEntity objects for mismatched entities.
    """
    connected_id = intelligence.account_id
    cross: list[CrossAccountEntity] = []

    # Collect all (name, guid) pairs from every domain.
    guid_pairs: list[tuple[str, str, str]] = []  # (name, guid, source_type)

    for name, guid in intelligence.apm.service_guids.items():
        guid_pairs.append((name, guid, "APM"))

    for name in intelligence.otel.service_names:
        # OTel entities may not have GUIDs stored in a separate map,
        # but they appear in entity search results. Check via name match
        # against APM guids or deduce from the entity search data.
        pass

    for name, meta in intelligence.synthetics.monitor_map.items():
        if meta.guid:
            guid_pairs.append((name, meta.guid, "SYNTH"))

    for name, guid, source_type in guid_pairs:
        decoded = decode_entity_guid(guid)
        if not decoded:
            continue
        home_account = decoded.get("account_id", "")
        if home_account and home_account != connected_id:
            entity_type = decoded.get("entity_type", "UNKNOWN")
            domain = decoded.get("domain", "")
            cross.append(
                CrossAccountEntity(
                    name=name,
                    guid=guid,
                    entity_type=f"{entity_type}|{domain}" if domain else entity_type,
                    home_account_id=home_account,
                    connected_account_id=connected_id,
                )
            )

    return cross


# ── Main learn function ──────────────────────────────────────────────────


async def learn_account(credentials: Credentials) -> AccountIntelligence:
    """Learn everything about a New Relic account in parallel.

    Runs all per-domain learners concurrently — each learner fires its own
    discovery queries in parallel, so total concurrency matches the old
    single-gather design. Learners fill their slice of AccountIntelligence
    in place and never raise.

    After the learners complete, the cross-domain passes run: synthetic
    monitor→APM service association, naming-convention learning, and
    cross-account entity detection.

    Args:
        credentials: Validated credentials for the target account.

    Returns:
        Complete AccountIntelligence for the account.

    Raises:
        IntelligenceError: If learning fails critically.
    """
    account_id = credentials.account_id
    runner = QueryRunner(credentials)
    intel = AccountIntelligence(account_id=account_id)

    try:
        await asyncio.gather(
            apm_learner.learn(runner, intel),
            k8s_learner.learn(runner, intel),
            logs_learner.learn(runner, intel),
            synthetics_learner.learn(runner, intel),
            alerts_learner.learn(runner, intel),
            asb_learner.learn(runner, intel),
            entities_learner.learn(runner, intel),
            return_exceptions=True,
        )
    except Exception as exc:
        raise IntelligenceError(
            f"Failed to learn account {account_id}: {exc}",
            account_id=account_id,
            partial_result=None,
        )

    # ── Associate synthetic monitors with APM services (cross-domain) ──
    try:
        for mon_name, meta in intel.synthetics.monitor_map.items():
            meta.associated_service = _infer_associated_service(mon_name, intel.apm.service_names)
    except Exception:
        pass

    # ── Learn Naming Convention (feed ALL entity names) ──
    try:
        # Collect entity names from every domain for stronger signal.
        extra_names: list[str] = []
        extra_names.extend(intel.otel.service_names)
        extra_names.extend(intel.synthetics.monitor_names)
        extra_names.extend(intel.browser.app_names)
        extra_names.extend(intel.mobile.app_names)
        extra_names.extend(intel.workloads.workload_names)

        intel.naming_convention = _learn_naming_convention(
            apm_service_names=intel.apm.service_names,
            k8s_namespaces=intel.k8s.namespaces,
            k8s_deployments=intel.k8s.deployments,
            extra_entity_names=extra_names,
        )
    except Exception as nc_exc:
        logger.warning("Naming convention learning failed: %s", nc_exc)

    logger.info(
        "Account %s learned: %d total entities, %d APM services, %d OTel services, "
        "%d K8s namespaces, %d alert policies, %d synthetic monitors, "
        "%d hosts, %d containers, %d browser apps, %d mobile apps, "
        "%d workloads, logs=%s",
        account_id,
        intel.entity_counts.total_entities,
        intel.account_meta.total_apm_services,
        intel.otel.service_count,
        len(intel.k8s.namespaces),
        len(intel.alerts.policy_names),
        intel.synthetics.total_count,
        intel.infra.host_count,
        intel.infra.container_count,
        len(intel.browser.app_names),
        intel.mobile.app_count,
        intel.workloads.workload_count,
        intel.logs.enabled,
    )

    # ── Cross-Account Entity Detection ──
    try:
        intel.cross_account_entities = detect_cross_account_entities(intel)
        if intel.cross_account_entities:
            logger.warning(
                "Cross-account entities detected in account %s: %s",
                account_id,
                [f"{e.name} (home={e.home_account_id})" for e in intel.cross_account_entities],
            )
    except Exception as xacc_exc:
        logger.warning("Cross-account detection failed: %s", xacc_exc)

    return intel
