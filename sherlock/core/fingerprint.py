"""
Drain-style log template clustering for Sherlock.

Collapses thousands of log messages into a handful of distinct patterns
with counts, so an agent (or engineer) sees "3 patterns, 1 dominating"
instead of 500 raw lines. Simplified Drain: variable-looking tokens
(numbers, UUIDs, IPs, hex, timestamps, paths, emails) are masked first,
then messages are grouped by token count and merged position-wise when
similar enough — mismatching positions become the ``<*>`` wildcard.

Pure stdlib, no external dependencies.
"""

import re

# Wildcard used for positions that differ between merged messages.
WILDCARD = "<*>"

# Masking passes, applied in order (most specific first).
_MASK_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (
        re.compile(
            r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b",
            re.IGNORECASE,
        ),
        "<UUID>",
    ),
    (
        re.compile(r"\b\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?"),
        "<TIMESTAMP>",
    ),
    (re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}(?::\d+)?\b"), "<IP>"),
    (re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+"), "<EMAIL>"),
    (re.compile(r"\b0x[0-9a-fA-F]+\b"), "<HEX>"),
    (re.compile(r"\b[0-9a-f]{12,}\b", re.IGNORECASE), "<HEX>"),
    (re.compile(r"(?:/[\w.\-]+){2,}/?"), "<PATH>"),
    (re.compile(r"\b\d+(?:\.\d+)?\b"), "<NUM>"),
]

# Minimum fraction of same-position tokens for two messages to share a
# template (the classic Drain similarity threshold).
DEFAULT_SIMILARITY = 0.5
DEFAULT_MAX_PATTERNS = 20


def mask_message(message: str) -> str:
    """Replace variable-looking substrings with typed placeholders."""
    masked = message
    for pattern, placeholder in _MASK_PATTERNS:
        masked = pattern.sub(placeholder, masked)
    return masked


def _similarity(a: list[str], b: list[str]) -> float:
    """Fraction of positions with identical tokens (same-length lists)."""
    if not a:
        return 0.0
    same = sum(1 for x, y in zip(a, b) if x == y or x == WILDCARD or y == WILDCARD)
    return same / len(a)


def _merge(template: list[str], tokens: list[str]) -> list[str]:
    """Position-wise merge: keep matching tokens, wildcard the rest."""
    return [t if t == u else WILDCARD for t, u in zip(template, tokens)]


class _Cluster:
    """One log template cluster."""

    __slots__ = ("template", "count", "example")

    def __init__(self, tokens: list[str], example: str) -> None:
        self.template = tokens
        self.count = 1
        self.example = example


def fingerprint_logs(
    messages: list[str],
    similarity: float = DEFAULT_SIMILARITY,
    max_patterns: int = DEFAULT_MAX_PATTERNS,
) -> list[dict]:
    """Cluster log messages into distinct templates with counts.

    Args:
        messages: Raw log message strings.
        similarity: Minimum same-position token fraction to join a cluster.
        max_patterns: Maximum number of patterns to return.

    Returns:
        Patterns sorted by count (desc): each has ``template``, ``count``,
        ``percentage`` (of all messages), and one raw ``example``.
    """
    total = 0
    # Buckets keyed by token count — Drain's first partition level. Only
    # same-length messages can share a template (position-wise merge).
    buckets: dict[int, list[_Cluster]] = {}

    for raw in messages:
        if not raw or not str(raw).strip():
            continue
        total += 1
        tokens = mask_message(str(raw)).split()
        bucket = buckets.setdefault(len(tokens), [])

        best: _Cluster | None = None
        best_score = 0.0
        for cluster in bucket:
            score = _similarity(cluster.template, tokens)
            if score > best_score:
                best, best_score = cluster, score

        if best is not None and best_score >= similarity:
            best.template = _merge(best.template, tokens)
            best.count += 1
        else:
            bucket.append(_Cluster(tokens, str(raw)))

    clusters = [c for bucket in buckets.values() for c in bucket]
    clusters.sort(key=lambda c: c.count, reverse=True)

    return [
        {
            "template": " ".join(c.template),
            "count": c.count,
            "percentage": round(c.count / total * 100, 1) if total else 0.0,
            "example": c.example[:500],
        }
        for c in clusters[:max_patterns]
    ]
