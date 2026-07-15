"""
Pydantic models for account intelligence.

Extracted from core/intelligence.py so per-domain learners can share them
without importing the orchestrator.
"""

from datetime import datetime, timezone

from pydantic import BaseModel, Field


class APMIntelligence(BaseModel):
    """Intelligence about APM services in the account."""

    service_names: list[str] = Field(default_factory=list)
    service_guids: dict[str, str] = Field(default_factory=dict)
    """Preferred GUID per service name. When multiple reporters share a name,
    this stores the one most likely to be the user's intent (currently-reporting,
    most recent alertSeverity event). Use service_guid_candidates for the full
    list when disambiguation matters."""

    service_guid_candidates: dict[str, list[dict]] = Field(default_factory=dict)
    """Full candidate list per service name. Each candidate is a dict with:
        - guid: str
        - reporting: bool
        - tags: dict[str, list[str]]  (for downstream disambiguation)
        - alert_severity: str  (NOT_CONFIGURED | NOT_ALERTING | WARNING | CRITICAL)
    Populated even when there's only one match; consumers that don't care about
    duplicates can keep using service_guids."""

    reporting_guids: set[str] = Field(default_factory=set)
    """Fast-lookup set of GUIDs currently reporting data. Used by
    resolve_apm_guid() to validate before returning."""

    service_languages: dict[str, str] = Field(default_factory=dict)
    naming_pattern: str = ""
    top_error_classes: list[str] = Field(default_factory=list)
    environments: list[str] = Field(default_factory=list)


class K8sIntelligence(BaseModel):
    """Intelligence about Kubernetes integration in the account."""

    integrated: bool = False
    namespaces: list[str] = Field(default_factory=list)
    deployments: dict[str, list[str]] = Field(default_factory=dict)
    cluster_names: list[str] = Field(default_factory=list)
    naming_pattern: str = ""
    # Entity sub-type counts
    cluster_count: int = 0
    namespace_count: int = 0
    deployment_count: int = 0
    pod_count: int = 0
    daemonset_count: int = 0
    statefulset_count: int = 0
    job_count: int = 0
    cronjob_count: int = 0
    pv_count: int = 0
    pvc_count: int = 0


class AlertsIntelligence(BaseModel):
    """Intelligence about alert policies in the account."""

    policy_names: list[str] = Field(default_factory=list)
    naming_pattern: str = ""


class LogsIntelligence(BaseModel):
    """Intelligence about logging configuration in the account."""

    enabled: bool = False
    service_attribute: str = ""
    severity_attribute: str = ""
    top_error_messages: list[str] = Field(default_factory=list)

    # Platform log discovery (Step 0c)
    namespace_attribute: str = ""
    """Tenant's log namespace attribute spelling — one of:
    'namespace_name' (Fluent Bit), 'k8s.namespace.name' (OTel k8sattributes),
    'kubernetes.namespace_name' (older Fluentd), or '' if no K8s logs."""

    cluster_attribute: str = ""
    """Tenant's log cluster attribute spelling — paired with namespace_attribute.
    One of: 'cluster_name', 'k8s.cluster.name', 'kubernetes.cluster_name', or ''."""

    platform_namespaces: list[str] = Field(default_factory=list)
    """Kubernetes platform namespaces discovered in this tenant's logs.
    Intersected against KNOWN_PLATFORM_NAMESPACES. Empty when no K8s logs
    or no platform components forward logs to NR."""

    all_discovered_namespaces: list[str] = Field(default_factory=list)
    """Every K8s namespace name observed in Log events during learn_account.
    Superset of platform_namespaces. Useful for debugging why a namespace
    was not classified as platform. Capped at 50 for response-size safety."""


class SyntheticMonitorMeta(BaseModel):
    """Metadata for a single synthetic monitor."""

    guid: str = ""
    name: str = ""
    type: str = ""
    status: str = ""
    period: int | str = ""
    locations: list[str] = Field(default_factory=list)
    associated_service: str | None = None


class SyntheticsIntelligence(BaseModel):
    """Intelligence about synthetic monitoring in the account."""

    enabled: bool = False
    monitor_names: list[str] = Field(default_factory=list)
    monitor_map: dict[str, SyntheticMonitorMeta] = Field(default_factory=dict)
    monitor_types: list[str] = Field(default_factory=list)
    naming_pattern: str = ""
    total_count: int = 0


class InfraIntelligence(BaseModel):
    """Intelligence about infrastructure monitoring in the account."""

    cloud_provider: str | None = None
    regions: list[str] = Field(default_factory=list)
    host_count: int = 0
    container_count: int = 0


class BrowserIntelligence(BaseModel):
    """Intelligence about browser monitoring in the account."""

    enabled: bool = False
    app_names: list[str] = Field(default_factory=list)


class MobileIntelligence(BaseModel):
    """Intelligence about mobile monitoring in the account."""

    enabled: bool = False
    app_names: list[str] = Field(default_factory=list)
    app_count: int = 0


class OTelIntelligence(BaseModel):
    """Intelligence about OpenTelemetry services in the account."""

    enabled: bool = False
    service_names: list[str] = Field(default_factory=list)
    service_count: int = 0


class WorkloadIntelligence(BaseModel):
    """Intelligence about workloads in the account."""

    enabled: bool = False
    workload_names: list[str] = Field(default_factory=list)
    workload_count: int = 0


class AzureServiceBusQueueMeta(BaseModel):
    """Metadata for a single discovered ASB queue."""

    entity_name: str = ""  # e.g. 'prod-arelle-validation-queue'
    namespace: str = ""  # e.g. 'sbns-eus2-prd-eswd-prdtngo'
    active_messages: float = 0.0
    dead_letter_messages: float = 0.0
    incoming_messages: float = 0.0
    has_dlq: bool = False  # True if dead_letter_messages > 0
    prefix: str = ""  # e.g. 'prod' (text before first '-')


class AzureServiceBusTopicMeta(BaseModel):
    """Metadata for a single discovered ASB topic."""

    entity_name: str = ""
    namespace: str = ""
    incoming_messages: float = 0.0


class AzureServiceBusIntelligence(BaseModel):
    """Intelligence about Azure Service Bus resources discovered during learn_account."""

    configured: bool = False  # True if any ASB data found in account
    namespaces: list[str] = Field(default_factory=list)
    queues: list[AzureServiceBusQueueMeta] = Field(default_factory=list)
    topics: list[AzureServiceBusTopicMeta] = Field(default_factory=list)
    queue_count: int = 0
    topic_count: int = 0
    dlq_count: int = 0  # queues with dead_letter_messages > 0
    total_dlq_messages: float = 0.0  # sum across all queues
    queue_prefixes: list[str] = Field(default_factory=list)  # e.g. ['prod', 'dev']
    naming_pattern: str = ""  # inferred e.g. '{prefix}-{service}-queue'


class CrossAccountEntity(BaseModel):
    """An entity whose GUID encodes a different account than the connected one."""

    name: str = ""
    guid: str = ""
    entity_type: str = ""  # APM, EXT, INFRA, SYNTH, etc.
    home_account_id: str = ""  # account the entity actually lives in
    connected_account_id: str = ""  # account we're connected to


class EntityTypeSummary(BaseModel):
    """A single entity type with its domain, type, and count."""

    domain: str = ""
    type: str = ""
    count: int = 0


class EntityCountsSummary(BaseModel):
    """Summary of all entity type counts in the account."""

    total_entities: int = 0
    type_breakdown: list[EntityTypeSummary] = Field(default_factory=list)
    # Convenience aggregates for common categories
    azure_resource_count: int = 0
    azure_resource_types: list[str] = Field(default_factory=list)
    key_transaction_count: int = 0
    service_level_count: int = 0
    issue_count: int = 0


class AccountMeta(BaseModel):
    """High-level account metadata summary."""

    name: str = ""
    total_entities: int = 0
    total_apm_services: int = 0
    otel_services: int = 0
    k8s_integrated: bool = False
    logs_enabled: bool = False
    synthetics_enabled: bool = False
    synthetics_count: int = 0
    container_count: int = 0
    mobile_apps: int = 0
    workload_count: int = 0
    key_transaction_count: int = 0
    azure_resource_count: int = 0


class NamingConvention(BaseModel):
    """Learned naming convention for the account's services.

    Discovered automatically from ALL entity names (APM, K8s, Synthetics,
    Browser, Infra, OTel, etc.) during learn_account(). Uses statistical
    analysis — no hardcoded environment keywords — so it works for ANY
    client's naming convention without manual configuration.

    The learning algorithm:
      1. Detects the primary separator (/, -, _, .) from all entity names.
      2. Splits names into segments by that separator.
      3. Classifies each segment position by *cardinality ratio*:
         - Low cardinality (same values reused across many services)
           → environment/team/namespace segment.
         - High cardinality (unique per service) → service identifier.
      4. Cross-references with K8s namespaces and deployments.

    Used by fuzzy resolution, K8s query building, and other tools.
    """

    separator: str | None = None
    env_position: str | None = None  # "prefix" or "suffix" or None
    env_values: list[str] = Field(default_factory=list)
    bare_service_names: list[str] = Field(default_factory=list)
    apm_to_k8s_namespace_map: dict[str, str] = Field(default_factory=dict)
    k8s_deployment_name_format: str = "bare"  # "bare" or "full"
    # Enhanced fields for universal naming support
    segment_roles: list[str] = Field(default_factory=list)
    """Ordered list of segment roles by position, e.g. ["environment", "service"].
    Possible roles: "environment", "team", "service", "component", "unknown"."""
    team_values: list[str] = Field(default_factory=list)
    """Discovered team/org/namespace values (when separate from environment)."""
    secondary_separator: str | None = None
    """Sub-separator within segments, e.g. '-' within '/'-separated names."""
    all_entity_names: list[str] = Field(default_factory=list)
    """Union of all discovered entity names across domains. Used by
    fuzzy resolver for cross-domain matching."""


class AccountIntelligence(BaseModel):
    """Complete intelligence profile for a New Relic account."""

    account_id: str
    learned_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    apm: APMIntelligence = Field(default_factory=APMIntelligence)
    otel: OTelIntelligence = Field(default_factory=OTelIntelligence)
    k8s: K8sIntelligence = Field(default_factory=K8sIntelligence)
    alerts: AlertsIntelligence = Field(default_factory=AlertsIntelligence)
    logs: LogsIntelligence = Field(default_factory=LogsIntelligence)
    synthetics: SyntheticsIntelligence = Field(default_factory=SyntheticsIntelligence)
    infra: InfraIntelligence = Field(default_factory=InfraIntelligence)
    browser: BrowserIntelligence = Field(default_factory=BrowserIntelligence)
    mobile: MobileIntelligence = Field(default_factory=MobileIntelligence)
    workloads: WorkloadIntelligence = Field(default_factory=WorkloadIntelligence)
    entity_counts: EntityCountsSummary = Field(default_factory=EntityCountsSummary)
    account_meta: AccountMeta = Field(default_factory=AccountMeta)
    naming_convention: NamingConvention = Field(default_factory=NamingConvention)
    azure_service_bus: AzureServiceBusIntelligence = Field(
        default_factory=AzureServiceBusIntelligence
    )
    cross_account_entities: list[CrossAccountEntity] = Field(default_factory=list)


class AccessibleAccount(BaseModel):
    """An account accessible to the API key."""

    id: str
    name: str
    entity_count: int = 0
