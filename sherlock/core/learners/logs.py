"""Log configuration learner (attributes, enablement, platform namespaces)."""

import asyncio
import logging
from typing import Any

from sherlock.core.intelligence_models import AccountIntelligence
from sherlock.core.learners.base import QueryRunner

logger = logging.getLogger("sherlock.learners.logs")

NRQL_LOG_KEYSET = "SELECT keyset() FROM Log SINCE 1 day ago"
NRQL_LOG_COUNT = "SELECT count(*) FROM Log SINCE 1 day ago"
# Probe which service/severity attributes exist in logs (fallback when keyset fails).
NRQL_LOG_ATTR_PROBE = (
    "SELECT "
    + ", ".join(
        f"uniqueCount(`{attr}`) as `has_{attr.replace('.', '_')}`"
        for attr in [
            "service.name",
            "serviceName",
            "service",
            "app.name",
            "application",
            "appName",
            "entity.name",
            "level",
            "severity",
            "log_severity",
            "log.level",
            "loglevel",
            "priority",
        ]
    )
    + " FROM Log SINCE 1 day ago"
)

# Platform log namespace/cluster attribute probe — detects which attribute
# spelling the tenant's log forwarder uses (Fluent Bit, OTel, or Fluentd).
NRQL_LOG_NAMESPACE_PROBE = (
    "SELECT count(*) as total,"
    " filter(count(*), WHERE namespace_name IS NOT NULL) as has_plain,"
    " filter(count(*), WHERE `k8s.namespace.name` IS NOT NULL) as has_k8s_prefix,"
    " filter(count(*), WHERE `kubernetes.namespace_name` IS NOT NULL) as has_kubernetes_prefix"
    " FROM Log SINCE 1 hour ago LIMIT 1"
)

# Platform log namespace enumeration — discovers all namespaces present in
# Log events using all three attribute conventions simultaneously.
NRQL_LOG_NAMESPACE_ENUMERATION = (
    "SELECT uniques(namespace_name, 50) as plain_ns,"
    " uniques(`k8s.namespace.name`, 50) as k8s_ns,"
    " uniques(`kubernetes.namespace_name`, 50) as kubernetes_ns"
    " FROM Log SINCE 1 hour ago LIMIT 1"
)

# ── Service attribute heuristics ─────────────────────────────────────────

SERVICE_ATTR_CANDIDATES = [
    "service.name",
    "entity.name",
    "serviceName",
    "service",
    "app.name",
    "application",
    "appName",
]

SEVERITY_ATTR_CANDIDATES = [
    "level",
    "severity",
    "log_severity",
    "log.level",
    "loglevel",
    "priority",
]

# Canonical Kubernetes platform-component namespaces. Tenant-agnostic:
# every tenant running Istio has 'istio-system'; every tenant running
# Linkerd has 'linkerd' / 'linkerd-control-plane'; etc. This list is a
# filter over discovered namespaces, NOT a configuration — a tenant
# that does not run a given platform component will simply not have
# that namespace in their discovered set.
KNOWN_PLATFORM_NAMESPACES: frozenset[str] = frozenset(
    {
        # Service meshes
        "istio-system",
        "istio-gateway",
        "linkerd",
        "linkerd-control-plane",
        "linkerd-viz",
        "linkerd-jaeger",
        "consul",
        "consul-system",
        "cilium",
        "cilium-system",
        "kuma-system",
        # Ingress controllers
        "ingress-nginx",
        "nginx-ingress",
        "kong",
        "kong-system",
        "traefik",
        "traefik-system",
        "contour",
        "projectcontour",
        "gloo-system",
        "haproxy-controller",
        # Core Kubernetes
        "kube-system",
        "kube-public",
        "kube-node-lease",
        # Observability / platform
        "monitoring",
        "observability",
        "cert-manager",
        "external-dns",
        "argocd",
        "flux-system",
    }
)


def _parse_log_namespace_intelligence(
    probe_row: dict,
    enumeration_row: dict,
) -> tuple[str, str, list[str], list[str]]:
    """Parse log namespace/cluster attribute probe and enumeration results.

    Called from learn_account after the parallel gather completes.
    Returns the discovered namespace_attribute, cluster_attribute,
    platform_namespaces, and all_discovered_namespaces.

    Args:
        probe_row: First row from NRQL_LOG_NAMESPACE_PROBE query.
        enumeration_row: First row from NRQL_LOG_NAMESPACE_ENUMERATION query.

    Returns:
        Tuple of (namespace_attribute, cluster_attribute,
        platform_namespaces, all_discovered_namespaces).
    """
    # Pick whichever attribute convention has the highest non-null count.
    candidates = [
        ("namespace_name", "cluster_name", probe_row.get("has_plain", 0) or 0),
        ("k8s.namespace.name", "k8s.cluster.name", probe_row.get("has_k8s_prefix", 0) or 0),
        (
            "kubernetes.namespace_name",
            "kubernetes.cluster_name",
            probe_row.get("has_kubernetes_prefix", 0) or 0,
        ),
    ]
    best = max(candidates, key=lambda c: c[2])

    namespace_attr = ""
    cluster_attr = ""
    if best[2] > 0:
        namespace_attr = best[0]
        cluster_attr = best[1]

    # Pick the namespace list matching the chosen attribute.
    if namespace_attr == "namespace_name":
        discovered_ns = enumeration_row.get("plain_ns", []) or []
    elif namespace_attr == "k8s.namespace.name":
        discovered_ns = enumeration_row.get("k8s_ns", []) or []
    elif namespace_attr == "kubernetes.namespace_name":
        discovered_ns = enumeration_row.get("kubernetes_ns", []) or []
    else:
        discovered_ns = []

    # Dedupe, cap at 50, filter against allowlist.
    discovered_ns = list(dict.fromkeys(str(n) for n in discovered_ns if n))[:50]
    platform_ns = sorted(n for n in discovered_ns if n in KNOWN_PLATFORM_NAMESPACES)

    return namespace_attr, cluster_attr, platform_ns, discovered_ns


async def learn(runner: QueryRunner, intel: AccountIntelligence) -> None:
    """Discover log enablement, service/severity attributes, and platform namespaces."""
    results: list[Any] = await asyncio.gather(
        runner.nrql(NRQL_LOG_KEYSET),  # 0: keyset
        runner.nrql(NRQL_LOG_COUNT),  # 1: count fallback
        runner.nrql(NRQL_LOG_ATTR_PROBE),  # 2: attribute probe
        runner.nrql(NRQL_LOG_NAMESPACE_PROBE),  # 3: ns attr probe
        runner.nrql(NRQL_LOG_NAMESPACE_ENUMERATION),  # 4: ns enumeration
        return_exceptions=True,
    )

    # ── Log Attribute Discovery (keyset) ──
    try:
        if not isinstance(results[0], BaseException) and results[0]:
            keyset = results[0][0].get("allKeys", []) if results[0] else []
            if not keyset and results[0]:
                keyset = results[0][0].get("uniques.key", []) if results[0] else []
            # Handle key-per-row format: [{"key": "attr1"}, {"key": "attr2"}, ...]
            if not keyset and results[0] and isinstance(results[0], list):
                row_keys = [r.get("key") for r in results[0] if isinstance(r, dict) and "key" in r]
                if row_keys:
                    keyset = row_keys
            if keyset:
                intel.logs.enabled = True
                intel.account_meta.logs_enabled = True
                for candidate in SERVICE_ATTR_CANDIDATES:
                    if candidate in keyset:
                        intel.logs.service_attribute = candidate
                        break
                for candidate in SEVERITY_ATTR_CANDIDATES:
                    if candidate in keyset:
                        intel.logs.severity_attribute = candidate
                        break
    except Exception:
        pass

    # ── Log Count Fallback ──
    # If keyset() failed to detect logs (e.g. partitioned logs), use count(*)
    # and probe for actual attribute names.
    if not intel.logs.enabled:
        try:
            if not isinstance(results[1], BaseException) and results[1]:
                log_count = results[1][0].get("count", 0) if results[1] else 0
                if log_count > 0:
                    intel.logs.enabled = True
                    intel.account_meta.logs_enabled = True

                    # Try to determine actual attributes from the probe query.
                    probe: dict = {}
                    try:
                        if not isinstance(results[2], BaseException) and results[2]:
                            probe = results[2][0] if results[2] else {}
                    except Exception:
                        pass

                    # Pick the first service attribute with data.
                    for candidate in SERVICE_ATTR_CANDIDATES:
                        key = f"has_{candidate.replace('.', '_')}"
                        if probe.get(key, 0) > 0:
                            intel.logs.service_attribute = candidate
                            break
                    if not intel.logs.service_attribute:
                        intel.logs.service_attribute = "service.name"

                    # Pick the first severity attribute with data.
                    for candidate in SEVERITY_ATTR_CANDIDATES:
                        key = f"has_{candidate.replace('.', '_')}"
                        if probe.get(key, 0) > 0:
                            intel.logs.severity_attribute = candidate
                            break
                    if not intel.logs.severity_attribute:
                        intel.logs.severity_attribute = "level"

                    logger.info(
                        "Log keyset() returned empty but %d logs found via count; "
                        "enabled with service_attr=%s, severity_attr=%s",
                        log_count,
                        intel.logs.service_attribute,
                        intel.logs.severity_attribute,
                    )
        except Exception:
            pass

    # ── Platform Log Namespace Discovery ──
    try:
        probe_row: dict = {}
        if not isinstance(results[3], BaseException) and results[3]:
            probe_row = results[3][0] if results[3] else {}

        enum_row: dict = {}
        if not isinstance(results[4], BaseException) and results[4]:
            enum_row = results[4][0] if results[4] else {}

        ns_attr, cl_attr, platform_ns, all_ns = _parse_log_namespace_intelligence(
            probe_row=probe_row,
            enumeration_row=enum_row,
        )
        intel.logs.namespace_attribute = ns_attr
        intel.logs.cluster_attribute = cl_attr
        intel.logs.platform_namespaces = platform_ns
        intel.logs.all_discovered_namespaces = all_ns

        if platform_ns:
            logger.info(
                "Platform log discovery: namespace_attr=%s, %d platform namespaces: %s",
                ns_attr,
                len(platform_ns),
                ", ".join(platform_ns),
            )
        elif ns_attr:
            logger.debug(
                "Platform log discovery: namespace_attr=%s, no platform namespaces found",
                ns_attr,
            )
    except Exception:
        pass  # Platform log discovery failure must not abort the learn
