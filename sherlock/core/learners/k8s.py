"""Kubernetes integration learner (namespaces, deployments, clusters, counts)."""

import asyncio
import logging
from typing import Any

from sherlock.core.intelligence_models import AccountIntelligence
from sherlock.core.learners.base import QueryRunner
from sherlock.core.naming import _infer_naming_pattern

logger = logging.getLogger("sherlock.learners.k8s")

NRQL_K8S_NAMESPACES = "SELECT uniques(namespaceName) FROM K8sPodSample SINCE 1 day ago LIMIT 200"

NRQL_K8S_DEPLOYMENTS = (
    "SELECT uniques(deploymentName) FROM K8sDeploymentSample"
    " FACET namespaceName SINCE 1 day ago LIMIT 200"
)

NRQL_K8S_CLUSTERS = "SELECT uniques(clusterName) FROM K8sPodSample SINCE 1 day ago LIMIT 50"

# K8s entity sub-type counts via NRQL
NRQL_K8S_POD_COUNT = "SELECT uniqueCount(entityName) FROM K8sPodSample SINCE 1 day ago"

NRQL_K8S_DAEMONSET_COUNT = (
    "SELECT uniqueCount(daemonsetName) FROM K8sDaemonsetSample SINCE 1 day ago"
)

NRQL_K8S_STATEFULSET_COUNT = (
    "SELECT uniqueCount(statefulsetName) FROM K8sStatefulsetSample SINCE 1 day ago"
)

NRQL_K8S_JOB_COUNT = "SELECT uniqueCount(jobName) FROM K8sJobSample SINCE 1 day ago"

NRQL_K8S_CRONJOB_COUNT = "SELECT uniqueCount(cronJobName) FROM K8sCronJobSample SINCE 1 day ago"

NRQL_K8S_PV_COUNT = "SELECT uniqueCount(volumeName) FROM K8sPersistentVolumeSample SINCE 1 day ago"

NRQL_K8S_PVC_COUNT = (
    "SELECT uniqueCount(pvcName) FROM K8sPersistentVolumeClaimSample SINCE 1 day ago"
)


async def learn(runner: QueryRunner, intel: AccountIntelligence) -> None:
    """Discover K8s namespaces, deployments, clusters, and sub-type counts."""
    results: list[Any] = await asyncio.gather(
        runner.nrql(NRQL_K8S_NAMESPACES),  # 0
        runner.nrql(NRQL_K8S_DEPLOYMENTS),  # 1
        runner.nrql(NRQL_K8S_CLUSTERS),  # 2
        runner.nrql(NRQL_K8S_POD_COUNT),  # 3
        runner.nrql(NRQL_K8S_DAEMONSET_COUNT),  # 4
        runner.nrql(NRQL_K8S_STATEFULSET_COUNT),  # 5
        runner.nrql(NRQL_K8S_JOB_COUNT),  # 6
        runner.nrql(NRQL_K8S_CRONJOB_COUNT),  # 7
        runner.nrql(NRQL_K8S_PV_COUNT),  # 8
        runner.nrql(NRQL_K8S_PVC_COUNT),  # 9
        return_exceptions=True,
    )

    # ── Namespaces ──
    try:
        if not isinstance(results[0], BaseException) and results[0]:
            ns_list = results[0][0].get("uniques.namespaceName", []) if results[0] else []
            intel.k8s.namespaces = [n for n in ns_list if n]
            intel.k8s.namespace_count = len(intel.k8s.namespaces)
            intel.k8s.integrated = len(intel.k8s.namespaces) > 0
            intel.k8s.naming_pattern = _infer_naming_pattern(intel.k8s.namespaces)
            intel.account_meta.k8s_integrated = intel.k8s.integrated
    except Exception:
        pass

    # ── Deployments ──
    try:
        if not isinstance(results[1], BaseException) and results[1]:
            total_deps = 0
            for row in results[1]:
                ns = row.get("namespaceName", "")
                deps = row.get("uniques.deploymentName", [])
                if ns and deps:
                    dep_list = [d for d in deps if d]
                    intel.k8s.deployments[ns] = dep_list
                    total_deps += len(dep_list)
            intel.k8s.deployment_count = total_deps
    except Exception:
        pass

    # ── Clusters ──
    try:
        if not isinstance(results[2], BaseException) and results[2]:
            clusters = results[2][0].get("uniques.clusterName", []) if results[2] else []
            intel.k8s.cluster_names = [c for c in clusters if c]
            intel.k8s.cluster_count = len(intel.k8s.cluster_names)
    except Exception:
        pass

    # ── Sub-type Counts ──
    k8s_count_map = {
        3: "pod_count",
        4: "daemonset_count",
        5: "statefulset_count",
        6: "job_count",
        7: "cronjob_count",
        8: "pv_count",
        9: "pvc_count",
    }
    for idx, attr in k8s_count_map.items():
        try:
            if not isinstance(results[idx], BaseException) and results[idx]:
                # NRQL uniqueCount returns a single result with the count
                row = results[idx][0] if results[idx] else {}
                count_val = 0
                for k, v in row.items():
                    if k.startswith("uniqueCount"):
                        count_val = int(v) if v else 0
                        break
                setattr(intel.k8s, attr, count_val)
        except Exception:
            pass
