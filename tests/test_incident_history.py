"""
Tests for persistent incident memory (core/incident_history.py), the
snapshot (de)serialization, and the find_similar_incidents tool.

The autouse conftest fixture redirects HISTORY_FILE to a per-test temp
file, so these tests never touch the repo's .sherlock/ directory.
"""

import json
from datetime import datetime, timedelta, timezone

import pytest

from sherlock.core import incident_history
from sherlock.core.incident_history import (
    find_similar,
    format_age,
    load_history,
    persist_snapshot,
    score_similarity,
)
from sherlock.core.session_memory import InvestigationSnapshot, SessionMemory
from sherlock.tools.history_tools import find_similar_incidents


def _snapshot(**overrides) -> InvestigationSnapshot:
    defaults = dict(
        timestamp=datetime.now(timezone.utc),
        account_id="123456",
        account_name="Acme Corp Production",
        service_name="payment-svc-prod",
        bare_name="payment-svc-prod",
        namespace="",
        severity="CRITICAL",
        root_cause="deploy of payment-svc-prod landed 4m before the error onset",
        causal_chain="deploy → error spike",
        causal_pattern="DEPLOY_REGRESSION",
        error_rate=12.0,
        is_otel=False,
        since_minutes=60,
    )
    defaults.update(overrides)
    return InvestigationSnapshot(**defaults)


class TestSnapshotSerialization:
    """to_dict / from_dict roundtrip."""

    def test_roundtrip(self):
        snap = _snapshot()
        rebuilt = InvestigationSnapshot.from_dict(snap.to_dict())
        assert rebuilt.service_name == snap.service_name
        assert rebuilt.severity == snap.severity
        assert rebuilt.causal_pattern == snap.causal_pattern
        assert rebuilt.error_rate == snap.error_rate
        assert rebuilt.timestamp == snap.timestamp

    def test_to_dict_is_json_safe(self):
        payload = json.dumps(_snapshot().to_dict())
        assert "payment-svc-prod" in payload

    def test_from_dict_tolerates_missing_and_extra_keys(self):
        rebuilt = InvestigationSnapshot.from_dict(
            {"service_name": "auth-service-prod", "future_field": 42}
        )
        assert rebuilt.service_name == "auth-service-prod"
        assert rebuilt.causal_pattern == "NONE"
        assert rebuilt.timestamp.tzinfo is not None


class TestPersistAndLoad:
    """history.jsonl append/read behavior."""

    def test_roundtrip_newest_first(self):
        persist_snapshot(_snapshot(service_name="a-svc"))
        persist_snapshot(_snapshot(service_name="b-svc"))
        records = load_history(account_id="123456")
        assert [r["service_name"] for r in records] == ["b-svc", "a-svc"]

    def test_account_filter(self):
        persist_snapshot(_snapshot(account_id="123456"))
        persist_snapshot(_snapshot(account_id="999999"))
        assert len(load_history(account_id="123456")) == 1
        assert len(load_history()) == 2

    def test_missing_file_returns_empty(self):
        assert load_history() == []

    def test_corrupt_lines_skipped(self):
        persist_snapshot(_snapshot())
        with open(incident_history.HISTORY_FILE, "a", encoding="utf-8") as f:
            f.write("{not valid json\n")
            f.write('"a bare string"\n')
        persist_snapshot(_snapshot(service_name="after-corruption"))
        records = load_history()
        assert len(records) == 2
        assert records[0]["service_name"] == "after-corruption"

    def test_compaction_keeps_newest(self, monkeypatch):
        monkeypatch.setattr(incident_history, "MAX_HISTORY_RECORDS", 10)
        monkeypatch.setattr(incident_history, "_COMPACT_THRESHOLD", 12)
        for i in range(13):
            persist_snapshot(_snapshot(service_name=f"svc-{i}"))
        records = load_history()
        assert len(records) == 10
        assert records[0]["service_name"] == "svc-12"
        assert records[-1]["service_name"] == "svc-3"

    def test_session_memory_record_persists(self):
        SessionMemory.reset()
        try:
            SessionMemory().record(_snapshot())
        finally:
            SessionMemory.reset()
        records = load_history(account_id="123456")
        assert len(records) == 1
        assert records[0]["service_name"] == "payment-svc-prod"


class TestScoring:
    """score_similarity signals."""

    def test_same_service_scores_high(self):
        record = _snapshot().to_dict()
        score, matched = score_similarity(record, "payment-svc-prod", set())
        assert score >= 0.45
        assert "same service" in matched

    def test_bare_name_match(self):
        record = _snapshot(
            service_name="payments-prod/payment-svc", bare_name="payment-svc"
        ).to_dict()
        score, matched = score_similarity(record, "payment-svc", set())
        assert any("bare name" in m for m in matched)

    def test_symptom_overlap_adds_score(self):
        record = _snapshot().to_dict()
        base, _ = score_similarity(record, "payment-svc-prod", set())
        with_symptom, matched = score_similarity(
            record, "payment-svc-prod", {"deploy", "error", "spike"}
        )
        assert with_symptom > base
        assert any("symptom keywords" in m for m in matched)

    def test_unrelated_record_below_threshold(self):
        record = _snapshot(
            service_name="totally-different",
            bare_name="totally-different",
            root_cause="disk full",
            causal_pattern="NONE",
            severity="WARNING",
        ).to_dict()
        score, _ = score_similarity(record, "payment-svc-prod", {"timeout"})
        assert score < incident_history.SIMILARITY_THRESHOLD


class TestFindSimilar:
    """End-to-end matching over the history file."""

    def test_best_match_first(self):
        persist_snapshot(
            _snapshot(
                service_name="auth-service-prod",
                bare_name="auth-service-prod",
                root_cause="keystore rotation broke tokens",
                causal_pattern="NONE",
                severity="WARNING",
            )
        )
        persist_snapshot(_snapshot())  # payment-svc-prod DEPLOY_REGRESSION CRITICAL
        matches = find_similar("payment-svc-prod", account_id="123456", symptom="deploy spike")
        assert matches
        assert matches[0]["service_name"] == "payment-svc-prod"
        assert matches[0]["similarity"] >= 0.5
        assert "when" in matches[0]

    def test_threshold_filters_noise(self):
        persist_snapshot(
            _snapshot(
                service_name="unrelated-svc",
                bare_name="unrelated-svc",
                root_cause="",
                causal_pattern="NONE",
                severity="HEALTHY",
            )
        )
        assert find_similar("payment-svc-prod", account_id="123456") == []

    def test_other_accounts_ignored(self):
        persist_snapshot(_snapshot(account_id="999999"))
        assert find_similar("payment-svc-prod", account_id="123456") == []


class TestFormatAge:
    def test_minutes(self):
        ts = (datetime.now(timezone.utc) - timedelta(minutes=45)).isoformat()
        assert format_age(ts) == "45m ago"

    def test_hours(self):
        ts = (datetime.now(timezone.utc) - timedelta(hours=2, minutes=15)).isoformat()
        assert format_age(ts) == "2h 15m ago"

    def test_days(self):
        ts = (datetime.now(timezone.utc) - timedelta(days=3, hours=4)).isoformat()
        assert format_age(ts) == "3d 4h ago"

    def test_bad_input(self):
        assert format_age("not-a-timestamp") == ""


class TestFindSimilarIncidentsTool:
    """The MCP tool over the persistent history."""

    @pytest.mark.asyncio
    async def test_finds_past_incident(self, mock_context):
        persist_snapshot(_snapshot(timestamp=datetime.now(timezone.utc) - timedelta(days=3)))
        result = await find_similar_incidents(
            "payment-svc-prod", symptom="error spike after deploy"
        )
        parsed = json.loads(result)
        assert parsed["total_matches"] == 1
        assert parsed["matches"][0]["service_name"] == "payment-svc-prod"
        assert "Most similar" in parsed["verdict"]
        assert "3d" in parsed["verdict"]
        assert "deploy of payment-svc-prod" in parsed["verdict"]

    @pytest.mark.asyncio
    async def test_fuzzy_resolves_service(self, mock_context):
        persist_snapshot(_snapshot())
        result = await find_similar_incidents("payment-svc")
        parsed = json.loads(result)
        assert parsed["service_name"] == "payment-svc-prod"
        assert parsed["resolved_from"] == "payment-svc"
        assert parsed["total_matches"] == 1

    @pytest.mark.asyncio
    async def test_empty_history_verdict(self, mock_context):
        result = await find_similar_incidents("payment-svc-prod")
        parsed = json.loads(result)
        assert parsed["total_matches"] == 0
        assert "No incident history" in parsed["verdict"]

    @pytest.mark.asyncio
    async def test_not_connected_returns_error(self):
        from sherlock.core.context import AccountContext

        AccountContext.reset_singleton()
        try:
            result = await find_similar_incidents("payment-svc-prod")
        finally:
            AccountContext.reset_singleton()
        parsed = json.loads(result)
        assert "error" in parsed
        assert parsed["data_available"] is False
