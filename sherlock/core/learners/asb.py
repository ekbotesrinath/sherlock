"""Azure Service Bus learner (queues, topics, DLQ detection)."""

import asyncio
import logging
from typing import Any

from sherlock.core.intelligence_models import (
    AccountIntelligence,
    AzureServiceBusIntelligence,
    AzureServiceBusQueueMeta,
    AzureServiceBusTopicMeta,
)
from sherlock.core.learners.base import QueryRunner

logger = logging.getLogger("sherlock.learners.asb")

NRQL_ASB_QUEUES = """
SELECT latest(activeMessages.Average) as active_msgs,
       latest(deadLetterMessages) as dlq_msgs,
       latest(incomingMessages.Total) as incoming_msgs,
       latest(messages) as total_msgs
FROM AzureServiceBusQueueSample
SINCE 1 hour ago
FACET entityName, namespace
LIMIT 100
"""

NRQL_ASB_TOPICS = """
SELECT latest(incomingMessages.Total) as incoming_msgs
FROM AzureServiceBusTopicSample
SINCE 1 hour ago
FACET entityName, namespace
LIMIT 100
"""


def _parse_asb_intelligence(
    queue_rows: list,
    topic_rows: list,
) -> "AzureServiceBusIntelligence":
    """Parse ASB NRQL discovery results into AzureServiceBusIntelligence.

    Called from learn_account after the parallel gather completes.
    Accepts the raw results lists returned by _nrql().
    Handles accounts with no ASB gracefully (returns configured=False).
    """
    intel = AzureServiceBusIntelligence()

    # ── Parse queues ─────────────────────────────────────────────────────
    namespaces_seen: set[str] = set()

    for row in queue_rows or []:
        try:
            facets = row.get("facets", [])
            if len(facets) < 2:
                continue
            entity_name = str(facets[0] or "")
            namespace = str(facets[1] or "")
            if not entity_name or not namespace:
                continue

            namespaces_seen.add(namespace)
            prefix = entity_name.split("-")[0] if "-" in entity_name else ""

            # Attribute names vary by account — try both formats
            active = float(
                row.get("latest.active_msgs")
                or row.get("latest.activeMessages.Average")
                or row.get("activeMessages.Average")
                or 0
            )
            dlq = float(
                row.get("latest.dlq_msgs")
                or row.get("latest.deadLetterMessages")
                or row.get("deadLetterMessages")
                or 0
            )
            incoming = float(
                row.get("latest.incoming_msgs")
                or row.get("latest.incomingMessages.Total")
                or row.get("incomingMessages.Total")
                or 0
            )

            intel.queues.append(
                AzureServiceBusQueueMeta(
                    entity_name=entity_name,
                    namespace=namespace,
                    active_messages=active,
                    dead_letter_messages=dlq,
                    incoming_messages=incoming,
                    has_dlq=dlq > 0,
                    prefix=prefix,
                )
            )
        except Exception:
            continue

    # ── Parse topics ─────────────────────────────────────────────────────
    for row in topic_rows or []:
        try:
            facets = row.get("facets", [])
            if len(facets) < 2:
                continue
            entity_name = str(facets[0] or "")
            namespace = str(facets[1] or "")
            if not entity_name or not namespace:
                continue
            namespaces_seen.add(namespace)
            incoming = float(
                row.get("latest.incoming_msgs") or row.get("latest.incomingMessages.Total") or 0
            )
            intel.topics.append(
                AzureServiceBusTopicMeta(
                    entity_name=entity_name,
                    namespace=namespace,
                    incoming_messages=incoming,
                )
            )
        except Exception:
            continue

    # ── Aggregate ────────────────────────────────────────────────────────
    intel.namespaces = sorted(namespaces_seen)
    intel.queue_count = len(intel.queues)
    intel.topic_count = len(intel.topics)
    intel.dlq_count = sum(1 for q in intel.queues if q.has_dlq)
    intel.total_dlq_messages = sum(q.dead_letter_messages for q in intel.queues)
    intel.queue_prefixes = sorted({q.prefix for q in intel.queues if q.prefix})
    intel.configured = intel.queue_count > 0 or intel.topic_count > 0

    if intel.queues and intel.queue_prefixes:
        intel.naming_pattern = f"{intel.queue_prefixes[0]}-{{service}}-queue"

    return intel


async def learn(runner: QueryRunner, intel: AccountIntelligence) -> None:
    """Discover Azure Service Bus queues and topics."""
    results: list[Any] = await asyncio.gather(
        runner.nrql(NRQL_ASB_QUEUES),  # 0: queues
        runner.nrql(NRQL_ASB_TOPICS),  # 1: topics
        return_exceptions=True,
    )
    try:
        queue_rows = results[0] if not isinstance(results[0], BaseException) else []
        topic_rows = results[1] if not isinstance(results[1], BaseException) else []
        intel.azure_service_bus = _parse_asb_intelligence(
            queue_rows=queue_rows,
            topic_rows=topic_rows,
        )
    except Exception:
        pass  # Non-Azure accounts silently get configured=False
