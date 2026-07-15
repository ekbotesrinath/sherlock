"""
Naming-convention learning for account intelligence.

Statistical analysis of entity names (separator detection, segment-role
classification by cardinality, env/team/service extraction) plus fuzzy
association of synthetic monitors to APM services. No hardcoded
environment keywords — works for any client's naming convention.
"""

import logging
import re
from difflib import SequenceMatcher

from sherlock.core.intelligence_models import NamingConvention

logger = logging.getLogger("sherlock.naming")


def _infer_naming_pattern(names: list[str]) -> str:
    """Infer a naming convention pattern from a list of names.

    Args:
        names: List of entity names.

    Returns:
        Human-readable description of the naming pattern detected.
    """
    if not names:
        return "unknown"

    patterns: list[str] = []

    # Check for common separators.
    dash_count = sum(1 for n in names if "-" in n)
    underscore_count = sum(1 for n in names if "_" in n)
    dot_count = sum(1 for n in names if "." in n)

    total = len(names)
    if dash_count > total * 0.5:
        patterns.append("kebab-case")
    if underscore_count > total * 0.5:
        patterns.append("snake_case")
    if dot_count > total * 0.5:
        patterns.append("dot.separated")

    # Check for environment suffixes.
    env_suffixes = ["-prod", "-staging", "-dev", "-qa", "-uat", "-test"]
    env_count = sum(1 for n in names if any(n.lower().endswith(s) for s in env_suffixes))
    if env_count > total * 0.3:
        patterns.append("env-suffixed")

    # Check for common prefixes.
    if len(names) >= 2:
        prefix = _common_prefix(names)
        if len(prefix) >= 3:
            patterns.append(f"prefix: '{prefix}'")

    return ", ".join(patterns) if patterns else "mixed"


def _common_prefix(names: list[str]) -> str:
    """Find the common prefix among a list of names.

    Args:
        names: List of strings.

    Returns:
        The longest common prefix.
    """
    if not names:
        return ""
    prefix = names[0]
    for name in names[1:]:
        while not name.startswith(prefix):
            prefix = prefix[:-1]
            if not prefix:
                return ""
    return prefix


def _learn_naming_convention(
    apm_service_names: list[str],
    k8s_namespaces: list[str],
    k8s_deployments: dict[str, list[str]],
    *,
    extra_entity_names: list[str] | None = None,
) -> NamingConvention:
    """Learn the naming convention from the account's actual data.

    Uses **statistical segment analysis** — no hardcoded environment keywords.
    Works for ANY client naming convention:
      - "eswd-prod/pdf-export-service" (slash-separated, compound env prefix)
      - "auth-service-prod" (dash-separated, env suffix)
      - "team-alpha.billing.prod" (dot-separated, multi-level)
      - "my_service_production" (underscore-separated)
      - "blue/green/canary" — even unconventional schemes

    Algorithm:
      1. Detect the primary separator by consistency (most names split into
         the same segment count).
      2. For each segment position, compute *cardinality ratio*:
           cardinality_ratio = unique_values_in_segment / total_names
         Low ratio (< 0.4) + values reused across different remaining
         segments → environment/team.  High ratio → service identifier.
      3. Cross-reference env values with K8s namespaces.
      4. Determine K8s deployment name format (bare vs full).

    Args:
        apm_service_names: All APM service names in the account.
        k8s_namespaces: All K8s namespace names.
        k8s_deployments: Map of namespace to deployment names.
        extra_entity_names: Additional entity names from other domains
            (OTel, Synthetics, Browser, etc.) to strengthen detection.

    Returns:
        Populated NamingConvention.
    """
    # Combine all names for analysis (APM is primary, extras supplement).
    all_names = list(apm_service_names)
    if extra_entity_names:
        for n in extra_entity_names:
            if n and n not in all_names:
                all_names.append(n)

    if not all_names:
        return NamingConvention()

    convention = NamingConvention()
    convention.all_entity_names = sorted(set(all_names))

    # ── Step 1: Detect primary separator ──
    # Rank separators by structural consistency: what fraction of names,
    # when split, produce the same number of segments.
    CANDIDATE_SEPARATORS = ["/", "-", "_", ".", ":"]

    best_sep: str | None = None
    best_sep_score = 0.0
    best_seg_count = 0

    for sep in CANDIDATE_SEPARATORS:
        names_with_sep = [n for n in all_names if sep in n]
        if not names_with_sep or len(names_with_sep) < len(all_names) * 0.25:
            continue

        # Count segment lengths and find the mode.
        seg_counts: dict[int, int] = {}
        for n in names_with_sep:
            sc = n.count(sep) + 1
            seg_counts[sc] = seg_counts.get(sc, 0) + 1

        mode_count = max(seg_counts, key=lambda k: seg_counts[k])
        mode_fraction = seg_counts[mode_count] / len(names_with_sep)

        # Score = fraction of names with this sep * consistency of segment count
        coverage = len(names_with_sep) / len(all_names)
        score = coverage * mode_fraction

        # Prefer "/" over "-" at equal score (unambiguous namespace separator).
        if sep == "/" and score > 0:
            score += 0.1

        if score > best_sep_score:
            best_sep_score = score
            best_sep = sep
            best_seg_count = mode_count

    if not best_sep:
        # No clear separator — try to detect env from known K8s namespace overlap.
        convention.bare_service_names = sorted(set(apm_service_names))
        return convention

    convention.separator = best_sep

    # Detect secondary separator within segments (e.g., "-" within "/" names).
    if best_sep == "/":
        for sub_sep in ["-", "_", "."]:
            sub_count = sum(
                1 for n in all_names if best_sep in n and sub_sep in n.split(best_sep, 1)[-1]
            )
            if sub_count > len(all_names) * 0.3:
                convention.secondary_separator = sub_sep
                break

    # ── Step 2: Segment role detection ──
    # Strategy differs by separator type:
    # - "/" is unambiguous (namespace/resource convention) → position-based
    # - "-", "_", "." are ambiguous (compound names) → first/last cardinality

    if best_sep == "/":
        # ── Slash separator: full segment analysis ──
        # Convention: first segment = namespace/env, last = service name.
        # Validate with cross-segment pairing analysis.

        split_names = []
        for n in all_names:
            segments = n.split(best_sep)
            if len(segments) == best_seg_count:
                split_names.append(segments)

        if not split_names or best_seg_count < 2:
            convention.bare_service_names = sorted(set(apm_service_names))
            return convention

        # Compute per-segment cardinality and pairing stats.
        segment_analysis: list[dict] = []
        for pos in range(best_seg_count):
            values = [segs[pos] for segs in split_names]
            unique_values = set(values)
            cardinality = len(unique_values)

            # Compute average "partner count": for each value of this segment,
            # how many distinct values appear in the OTHER segments?
            from collections import Counter, defaultdict

            partners: dict[str, set[str]] = defaultdict(set)
            for segs in split_names:
                val = segs[pos]
                # Combine all other segment values as the "partner" key.
                other = best_sep.join(segs[j] for j in range(best_seg_count) if j != pos)
                partners[val].add(other)
            avg_partners = sum(len(p) for p in partners.values()) / max(cardinality, 1)

            segment_analysis.append(
                {
                    "position": pos,
                    "unique_values": sorted(unique_values),
                    "cardinality": cardinality,
                    "avg_partners": round(avg_partners, 2),
                }
            )

        # Classify: for "/" separator, default first=env, last=service.
        # Override only if pairing analysis strongly disagrees.
        roles = ["unknown"] * best_seg_count

        if best_seg_count == 2:
            # 2-segment case (most common): namespace/service
            roles[0] = "environment"
            roles[-1] = "service"
        else:
            # 3+ segments: first is likely team/org, second is env, last is service.
            # Assign by partner-count descending = more shared = "grouping" segment.
            partner_ranked = sorted(
                range(best_seg_count),
                key=lambda p: segment_analysis[p]["avg_partners"],
                reverse=True,
            )
            # Highest partner count → environment (each env contains many services).
            roles[partner_ranked[0]] = "environment"
            # Lowest partner count → will be assigned below.
            # The position with highest cardinality → service.
            svc_pos = max(
                range(best_seg_count),
                key=lambda p: segment_analysis[p]["cardinality"],
            )
            roles[svc_pos] = "service"
            # Any remaining low-cardinality positions → team.
            for p in range(best_seg_count):
                if roles[p] == "unknown":
                    roles[p] = "team"

        convention.segment_roles = roles

        # Extract env/team/service values.
        env_set: set[str] = set()
        team_set: set[str] = set()
        bare_set: set[str] = set()
        for segs in split_names:
            for pos in range(best_seg_count):
                if roles[pos] == "environment":
                    env_set.add(segs[pos])
                elif roles[pos] == "team":
                    team_set.add(segs[pos])
                elif roles[pos] == "service":
                    bare_set.add(segs[pos])

        convention.env_values = sorted(env_set)
        convention.team_values = sorted(team_set)
        convention.bare_service_names = sorted(bare_set)

        # env_position for backward compatibility.
        env_positions = [i for i, r in enumerate(roles) if r == "environment"]
        if env_positions:
            convention.env_position = "prefix" if env_positions[0] == 0 else "suffix"

        # Names that don't follow the modal pattern get added as bare names.
        for n in apm_service_names:
            if n.count(best_sep) + 1 != best_seg_count and n not in bare_set:
                bare_set.add(n)
        convention.bare_service_names = sorted(bare_set)

    else:
        # ── Ambiguous separator (-, _, .): first/last component analysis ──
        # Compound names like "payment-svc-prod" use internal dashes, so we
        # can't split into all segments. Instead, check if the FIRST or LAST
        # component (split by separator) has low cardinality → environment.

        names_with_sep = [n for n in all_names if best_sep in n]
        if not names_with_sep:
            convention.bare_service_names = sorted(set(apm_service_names))
            return convention

        from collections import Counter

        first_components = []
        last_components = []
        for n in names_with_sep:
            parts = n.split(best_sep)
            if len(parts) >= 2:
                first_components.append(parts[0])
                last_components.append(parts[-1])

        first_counter = Counter(first_components)
        last_counter = Counter(last_components)

        n_names = len(names_with_sep)
        first_unique = len(first_counter)
        last_unique = len(last_counter)
        first_ratio = first_unique / max(n_names, 1)
        last_ratio = last_unique / max(n_names, 1)

        # A segment is "env-like" if it has significantly lower cardinality
        # than the other segment, or overlaps with K8s namespaces.
        # Use both absolute threshold and relative comparison.
        # Also: environments come in sets (prod/dev/staging), so require
        # at least 2 distinct values to be considered an environment.
        first_is_env = False
        last_is_env = False

        # Absolute: low cardinality ratio AND at least 2 distinct values.
        if first_ratio <= 0.5 and first_unique >= 2:
            first_is_env = True
        if last_ratio <= 0.5 and last_unique >= 2:
            last_is_env = True

        # Relative: one side has clearly fewer unique values than the other.
        if last_ratio < first_ratio * 0.75 and last_ratio <= 0.6 and last_unique >= 2:
            last_is_env = True
            first_is_env = False
        elif first_ratio < last_ratio * 0.75 and first_ratio <= 0.6 and first_unique >= 2:
            first_is_env = True
            last_is_env = False

        # Cross-reference with K8s namespaces for disambiguation.
        if k8s_namespaces:
            ns_lookup = {ns.lower() for ns in k8s_namespaces}
            first_k8s_overlap = sum(1 for v in first_counter if v.lower() in ns_lookup)
            last_k8s_overlap = sum(1 for v in last_counter if v.lower() in ns_lookup)
            if first_k8s_overlap > last_k8s_overlap and first_k8s_overlap > 0:
                first_is_env = True
                last_is_env = False
            elif last_k8s_overlap > first_k8s_overlap and last_k8s_overlap > 0:
                last_is_env = True
                first_is_env = False

        env_set = set()
        bare_set = set()

        if last_is_env and (not first_is_env or last_ratio < first_ratio):
            # Suffix environment: "payment-svc-prod"
            convention.env_position = "suffix"
            convention.segment_roles = ["service", "environment"]
            for n in all_names:
                parts = n.rsplit(best_sep, 1)
                if len(parts) == 2 and parts[1] in last_counter:
                    env_set.add(parts[1])
                    bare_set.add(parts[0])
                else:
                    bare_set.add(n)
        elif first_is_env:
            # Prefix environment: "prod-payment-svc"
            convention.env_position = "prefix"
            convention.segment_roles = ["environment", "service"]
            for n in all_names:
                parts = n.split(best_sep, 1)
                if len(parts) == 2 and parts[0] in first_counter:
                    env_set.add(parts[0])
                    bare_set.add(parts[1])
                else:
                    bare_set.add(n)
        else:
            # No clear env pattern — names use separator internally only.
            convention.env_position = None
            bare_set = set(apm_service_names)

        convention.env_values = sorted(env_set)
        convention.bare_service_names = sorted(bare_set)

    # ── Step 4: APM env -> K8s namespace mapping ──
    if convention.env_values and k8s_namespaces:
        for env_val in convention.env_values:
            best_ns: str | None = None
            best_score = 0.0
            env_lower = env_val.lower()

            for ns in k8s_namespaces:
                ns_lower = ns.lower()

                # Exact match.
                if env_lower == ns_lower:
                    best_ns = ns
                    best_score = 1.0
                    break

                # Component match: split env on separators, check parts.
                env_parts = re.split(r"[-_.]", env_lower)
                for part in env_parts:
                    if part and part == ns_lower and len(part) > 1:
                        score = 0.9
                        if score > best_score:
                            best_ns = ns
                            best_score = score

                # Substring match.
                if best_score < 0.9:
                    if ns_lower in env_lower and len(ns_lower) > 2:
                        score = len(ns_lower) / max(len(env_lower), 1)
                        if score > best_score:
                            best_ns = ns
                            best_score = score
                    elif env_lower in ns_lower and len(env_lower) > 2:
                        score = len(env_lower) / max(len(ns_lower), 1)
                        if score > best_score:
                            best_ns = ns
                            best_score = score

            if best_ns and best_score > 0.3:
                convention.apm_to_k8s_namespace_map[env_val] = best_ns

    # ── Step 5: K8s deployment name format ──
    if k8s_deployments and convention.bare_service_names:
        all_deps: list[str] = []
        for dep_list in k8s_deployments.values():
            all_deps.extend(dep_list)

        if all_deps:
            bare_set_lower = {b.lower() for b in convention.bare_service_names}
            apm_set_lower = {n.lower() for n in apm_service_names}

            full_match = sum(1 for d in all_deps if d.lower() in apm_set_lower)
            bare_match = sum(1 for d in all_deps if d.lower() in bare_set_lower)

            convention.k8s_deployment_name_format = "full" if full_match > bare_match else "bare"

    logger.info(
        "Naming convention learned: separator=%s, roles=%s, "
        "envs=%s, teams=%s, bare_names=%d, k8s_map=%s, k8s_format=%s",
        convention.separator,
        convention.segment_roles,
        convention.env_values,
        convention.team_values,
        len(convention.bare_service_names),
        convention.apm_to_k8s_namespace_map,
        convention.k8s_deployment_name_format,
    )

    return convention


def _infer_associated_service(
    monitor_name: str, service_names: list[str], threshold: float = 0.5
) -> str | None:
    """Try to match a synthetic monitor name to an APM service name.

    Uses fuzzy matching because monitor names are often descriptive
    (e.g. 'Login Flow - Production') while service names are technical
    (e.g. 'auth-service-prod').

    Args:
        monitor_name: The synthetic monitor name.
        service_names: Known APM service names.
        threshold: Minimum similarity ratio.

    Returns:
        The best matching service name or None.
    """
    if not service_names:
        return None

    monitor_lower = monitor_name.lower()
    monitor_tokens = set(re.split(r"[\s\-_]+", monitor_lower))

    best_match: str | None = None
    best_score: float = 0.0

    for svc in service_names:
        svc_lower = svc.lower()
        svc_tokens = set(re.split(r"[\s\-_]+", svc_lower))

        # Check direct substring.
        if svc_lower in monitor_lower or monitor_lower in svc_lower:
            return svc

        # Token overlap.
        overlap = len(monitor_tokens & svc_tokens)
        total = max(len(monitor_tokens), len(svc_tokens))
        token_score = overlap / total if total > 0 else 0.0

        # Sequence similarity.
        seq_score = SequenceMatcher(None, monitor_lower, svc_lower).ratio()

        score = max(token_score, seq_score)
        if score > best_score:
            best_score = score
            best_match = svc

    return best_match if best_score >= threshold else None
