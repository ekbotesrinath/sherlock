"""
Persistent incident memory for Sherlock.

Every InvestigationSnapshot recorded to SessionMemory is appended here as
one JSON line in ``.sherlock/history.jsonl``, so incident knowledge
survives MCP server restarts. find_similar() matches current symptoms
against that history so the agent can say "this looks like the incident
from 3 days ago — root cause was X".
"""

import json
import logging
import threading
from datetime import datetime, timezone

from sherlock.core.session_memory import InvestigationSnapshot
from sherlock.paths import HISTORY_FILE

logger = logging.getLogger("sherlock.incident_history")

_write_lock = threading.Lock()

# Newest records kept when the file is compacted.
MAX_HISTORY_RECORDS = 1000
# Compact only when the file has grown past this, so appends stay O(1)
# most of the time.
_COMPACT_THRESHOLD = int(MAX_HISTORY_RECORDS * 1.2)

# Minimum score for find_similar to report a match.
SIMILARITY_THRESHOLD = 0.3

_STOPWORDS = frozenset(
    {
        "a",
        "an",
        "and",
        "are",
        "for",
        "in",
        "is",
        "it",
        "of",
        "on",
        "or",
        "the",
        "this",
        "to",
        "was",
        "with",
    }
)


def persist_snapshot(snapshot: InvestigationSnapshot) -> None:
    """Append a snapshot to the history file. Never raises.

    The file is compacted down to the newest MAX_HISTORY_RECORDS entries
    once it grows 20% past the cap.
    """
    try:
        with _write_lock:
            HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
            with open(HISTORY_FILE, "a", encoding="utf-8") as f:
                f.write(json.dumps(snapshot.to_dict()) + "\n")

            with open(HISTORY_FILE, encoding="utf-8") as f:
                lines = f.readlines()
            if len(lines) > _COMPACT_THRESHOLD:
                with open(HISTORY_FILE, "w", encoding="utf-8") as f:
                    f.writelines(lines[-MAX_HISTORY_RECORDS:])
    except Exception as exc:
        logger.warning("Could not persist incident snapshot: %s", exc)


def load_history(account_id: str | None = None, limit: int | None = None) -> list[dict]:
    """Load history records, newest first. Corrupt lines are skipped.

    Args:
        account_id: Optional account filter.
        limit: Optional cap on the number of records returned.

    Returns:
        List of snapshot dicts (as written by persist_snapshot).
    """
    records: list[dict] = []
    try:
        with open(HISTORY_FILE, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(record, dict):
                    continue
                if account_id and str(record.get("account_id", "")) != str(account_id):
                    continue
                records.append(record)
    except FileNotFoundError:
        return []
    except Exception as exc:
        logger.warning("Could not read incident history: %s", exc)
        return []

    records.reverse()  # file is append-ordered → newest first
    return records[:limit] if limit else records


def _symptom_tokens(text: str) -> set[str]:
    """Lowercase word tokens minus stopwords and short fragments."""
    tokens = {t for t in "".join(c if c.isalnum() else " " for c in text.lower()).split()}
    return {t for t in tokens if len(t) > 2 and t not in _STOPWORDS}


def score_similarity(
    record: dict,
    service_name: str,
    symptom_tokens: set[str],
) -> tuple[float, list[str]]:
    """Score how similar a past incident is to the current symptoms.

    Signals: same service (full or bare name), symptom keyword overlap
    with the recorded root cause / causal chain, a known causal pattern,
    and severity of the past incident.

    Returns:
        (score in [0, 1], list of human-readable matched-on reasons)
    """
    score = 0.0
    matched: list[str] = []

    name_lower = service_name.lower()
    bare = name_lower.rsplit("/", 1)[-1]
    rec_service = str(record.get("service_name", "")).lower()
    rec_bare = str(record.get("bare_name", "")).lower() or rec_service.rsplit("/", 1)[-1]

    if rec_service and rec_service == name_lower:
        score += 0.45
        matched.append("same service")
    elif bare and rec_bare and (bare == rec_bare or bare in rec_service or rec_bare in name_lower):
        score += 0.35
        matched.append("same service (bare name)")

    if symptom_tokens:
        rec_text = " ".join(
            str(record.get(key, ""))
            for key in ("root_cause", "causal_chain", "causal_pattern", "severity")
        )
        rec_tokens = _symptom_tokens(rec_text)
        overlap = symptom_tokens & rec_tokens
        if overlap:
            fraction = len(overlap) / len(symptom_tokens)
            score += 0.35 * fraction
            matched.append("symptom keywords: " + ", ".join(sorted(overlap)[:5]))

    if str(record.get("causal_pattern", "NONE")) not in ("", "NONE"):
        score += 0.1
        matched.append(f"known pattern: {record['causal_pattern']}")

    if str(record.get("severity", "")) == "CRITICAL":
        score += 0.1
        matched.append("was CRITICAL")

    return min(score, 1.0), matched


def format_age(timestamp_iso: str) -> str:
    """ISO timestamp → '3d 4h ago' style age string ('' on bad input)."""
    try:
        ts = datetime.fromisoformat(timestamp_iso)
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        minutes = int((datetime.now(timezone.utc) - ts).total_seconds() // 60)
        if minutes < 60:
            return f"{minutes}m ago"
        if minutes < 60 * 24:
            return f"{minutes // 60}h {minutes % 60}m ago"
        days = minutes // (60 * 24)
        hours = (minutes % (60 * 24)) // 60
        return f"{days}d {hours}h ago"
    except Exception:
        return ""


def find_similar(
    service_name: str,
    account_id: str,
    symptom: str = "",
    limit: int = 5,
) -> list[dict]:
    """Match current symptoms against past incidents for an account.

    Args:
        service_name: Service being investigated.
        account_id: Account whose history to search.
        symptom: Optional free-text symptom description.
        limit: Maximum matches to return.

    Returns:
        Matches (score >= SIMILARITY_THRESHOLD) sorted by similarity then
        recency; each has similarity, matched_on, when, and the recorded
        incident fields.
    """
    tokens = _symptom_tokens(symptom) if symptom else set()
    matches: list[dict] = []
    for record in load_history(account_id=account_id):
        score, matched_on = score_similarity(record, service_name, tokens)
        if score < SIMILARITY_THRESHOLD:
            continue
        matches.append(
            {
                "similarity": round(score, 2),
                "matched_on": matched_on,
                "when": format_age(str(record.get("timestamp", ""))),
                "timestamp": record.get("timestamp"),
                "service_name": record.get("service_name"),
                "severity": record.get("severity"),
                "root_cause": record.get("root_cause"),
                "causal_chain": record.get("causal_chain"),
                "causal_pattern": record.get("causal_pattern"),
                "error_rate": record.get("error_rate"),
            }
        )

    matches.sort(key=lambda m: (m["similarity"], str(m.get("timestamp") or "")), reverse=True)
    return matches[:limit]
