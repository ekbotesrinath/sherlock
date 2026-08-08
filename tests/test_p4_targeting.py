"""
Tests for the P4 field-feedback features:

- apply_resource_filter — cloud.resource_id sub-entity scoping (core/sanitize).
- truncate_result_fields — payload truncation (core/utils).
- resolve_profile_credentials — profile/account lookup (core/credentials).
- resolve_call_target — per-call account targeting (core/target).
- resource_filter plumbing in get_app_metrics / get_slow_traces.
"""

import json
from unittest.mock import MagicMock, patch

import httpx
import pytest
import respx

from sherlock.core.credentials import resolve_profile_credentials
from sherlock.core.exceptions import CredentialError, NotConnectedError
from sherlock.core.sanitize import apply_resource_filter
from sherlock.core.target import resolve_call_target
from sherlock.core.utils import truncate_result_fields


class TestApplyResourceFilter:
    """Tests for the cloud.resource_id NRQL clause injector."""

    def test_none_filter_is_noop(self):
        nrql = "SELECT count(*) FROM Transaction WHERE appName = 'x' SINCE 30 minutes ago"
        assert apply_resource_filter(nrql, None) == nrql

    def test_empty_filter_is_noop(self):
        nrql = "SELECT count(*) FROM Transaction WHERE appName = 'x' SINCE 30 minutes ago"
        assert apply_resource_filter(nrql, "") == nrql

    def test_inserted_before_since(self):
        nrql = "SELECT count(*) FROM Transaction WHERE appName = 'x' SINCE 30 minutes ago"
        out = apply_resource_filter(nrql, "gail-v2-di")
        assert out == (
            "SELECT count(*) FROM Transaction WHERE appName = 'x'"
            " AND cloud.resource_id LIKE '%gail-v2-di%' SINCE 30 minutes ago"
        )

    def test_inserted_before_timeseries(self):
        nrql = (
            "SELECT average(duration) FROM Transaction WHERE appName = 'x' "
            "TIMESERIES 5 minutes SINCE 30 minutes ago"
        )
        out = apply_resource_filter(nrql, "func-dev")
        clause_at = out.find("AND cloud.resource_id")
        assert clause_at != -1
        assert clause_at < out.find("TIMESERIES")

    def test_inserted_before_facet_not_after(self):
        """FACET queries must get the clause inside WHERE, not after FACET."""
        nrql = (
            "SELECT count(*) FROM TransactionError WHERE appName = 'x' "
            "FACET error.class, error.message SINCE 60 minutes ago LIMIT 10"
        )
        out = apply_resource_filter(nrql, "gail-v2-sp")
        clause_at = out.find("AND cloud.resource_id")
        assert clause_at != -1
        assert clause_at < out.find("FACET")

    def test_earliest_keyword_wins(self):
        """With SINCE before FACET, the clause lands before SINCE."""
        nrql = "SELECT count(*) FROM Log WHERE 1=1 SINCE 1 hour ago FACET level"
        out = apply_resource_filter(nrql, "rid")
        clause_at = out.find("AND cloud.resource_id")
        assert clause_at < out.find(" SINCE ")

    def test_appended_when_no_trailing_keywords(self):
        nrql = "SELECT count(*) FROM Transaction WHERE appName = 'x'"
        out = apply_resource_filter(nrql, "rid")
        assert out.endswith("AND cloud.resource_id LIKE '%rid%'")

    def test_filter_value_is_sanitized(self):
        """Quotes and NRQL metacharacters cannot break out of the literal."""
        nrql = "SELECT count(*) FROM Transaction WHERE appName = 'x' SINCE 5 minutes ago"
        out = apply_resource_filter(nrql, "abc' OR '1'='1")
        assert "abc' OR" not in out
        assert "cloud.resource_id LIKE '%abc OR 1=1%'" in out

    def test_all_metacharacters_stripped_leaves_noop(self):
        nrql = "SELECT count(*) FROM Transaction SINCE 5 minutes ago"
        assert apply_resource_filter(nrql, "';--") == nrql


class TestTruncateResultFields:
    """Tests for string-field truncation in result rows."""

    def test_long_string_truncated_with_marker(self):
        rows = [{"message": "x" * 500, "level": "ERROR"}]
        out, count = truncate_result_fields(rows, 100)
        assert count == 1
        assert out[0]["message"].startswith("x" * 100)
        assert "[truncated, 500 chars total]" in out[0]["message"]
        assert out[0]["level"] == "ERROR"

    def test_short_strings_and_numbers_untouched(self):
        rows = [{"message": "short", "count": 12345, "ok": True, "ratio": 1.5}]
        out, count = truncate_result_fields(rows, 100)
        assert count == 0
        assert out == rows

    def test_zero_disables_truncation(self):
        rows = [{"message": "x" * 5000}]
        out, count = truncate_result_fields(rows, 0)
        assert count == 0
        assert out[0]["message"] == "x" * 5000

    def test_non_dict_rows_pass_through(self):
        rows = [{"message": "y" * 300}, "not-a-dict"]
        out, count = truncate_result_fields(rows, 50)
        assert count == 1
        assert out[1] == "not-a-dict"

    def test_empty_rows(self):
        out, count = truncate_result_fields([], 100)
        assert out == []
        assert count == 0


def _profiles_file(tmp_path, profiles):
    path = tmp_path / "profiles.json"
    path.write_text(json.dumps(profiles), encoding="utf-8")
    return path


_PROFILES = [
    {"name": "dfin-ss", "account_id": "3503435", "region": "US", "created_at": "2026-01-01"},
    {"name": "dfin-ad", "account_id": "3007677", "region": "US", "created_at": "2026-01-01"},
]


class TestResolveProfileCredentials:
    """Tests for profile/account-based credential resolution."""

    def test_by_profile_name(self, tmp_path):
        profiles = _profiles_file(tmp_path, _PROFILES)
        mock_keyring = MagicMock()
        mock_keyring.get_password.return_value = "NRAK-ss-key"
        with (
            patch("sherlock.core.credentials.PROFILES_FILE", profiles),
            patch("sherlock.core.credentials.CONFIG_DIR", tmp_path),
            patch("sherlock.core.credentials.keyring", mock_keyring),
        ):
            creds = resolve_profile_credentials(profile_name="dfin-ss")
        assert creds.account_id == "3503435"
        assert creds.api_key == "NRAK-ss-key"

    def test_by_account_id_lookup(self, tmp_path):
        profiles = _profiles_file(tmp_path, _PROFILES)
        mock_keyring = MagicMock()
        mock_keyring.get_password.return_value = "NRAK-ad-key"
        with (
            patch("sherlock.core.credentials.PROFILES_FILE", profiles),
            patch("sherlock.core.credentials.CONFIG_DIR", tmp_path),
            patch("sherlock.core.credentials.keyring", mock_keyring),
        ):
            creds = resolve_profile_credentials(account_id="3007677")
        assert creds.account_id == "3007677"
        mock_keyring.get_password.assert_called_once_with("sherlock", "dfin-ad")

    def test_unknown_account_raises(self, tmp_path):
        profiles = _profiles_file(tmp_path, _PROFILES)
        with (
            patch("sherlock.core.credentials.PROFILES_FILE", profiles),
            patch("sherlock.core.credentials.CONFIG_DIR", tmp_path),
        ):
            with pytest.raises(CredentialError, match="No saved profile"):
                resolve_profile_credentials(account_id="999999")

    def test_neither_argument_raises(self):
        with pytest.raises(CredentialError, match="requires profile_name or account_id"):
            resolve_profile_credentials()


class TestResolveCallTarget:
    """Tests for per-call account targeting."""

    def test_default_uses_active_context(self, mock_context, mock_credentials):
        credentials, intelligence, client, overridden = resolve_call_target()
        assert credentials.account_id == mock_credentials.account_id
        assert intelligence is not None
        assert overridden is False

    def test_no_context_and_no_override_raises(self):
        from sherlock.core.context import AccountContext

        AccountContext.reset_singleton()
        try:
            with pytest.raises(NotConnectedError):
                resolve_call_target()
        finally:
            AccountContext.reset_singleton()

    def test_override_matching_active_account_shortcuts(self, mock_context, mock_credentials):
        """Targeting the already-active account reuses it without keychain access."""
        credentials, intelligence, client, overridden = resolve_call_target(
            account_id=mock_credentials.account_id
        )
        assert credentials is mock_credentials
        assert intelligence is not None
        assert overridden is True

    def test_override_other_account_loads_profile(self, mock_context, tmp_path, monkeypatch):
        profiles = _profiles_file(tmp_path, _PROFILES)
        mock_keyring = MagicMock()
        mock_keyring.get_password.return_value = "NRAK-ss-key"

        fake_cache = MagicMock()
        fake_cache.get.return_value = None
        fake_cache.get_stale.return_value = None
        monkeypatch.setattr("sherlock.core.target._cache", fake_cache)

        with (
            patch("sherlock.core.credentials.PROFILES_FILE", profiles),
            patch("sherlock.core.credentials.CONFIG_DIR", tmp_path),
            patch("sherlock.core.credentials.keyring", mock_keyring),
        ):
            credentials, intelligence, client, overridden = resolve_call_target(
                account_id="3503435"
            )

        assert credentials.account_id == "3503435"
        assert intelligence is None  # never learned/cached
        assert overridden is True
        assert client.account_id == "3503435"

    def test_override_uses_cached_intelligence_when_available(
        self, mock_context, mock_intelligence, tmp_path, monkeypatch
    ):
        profiles = _profiles_file(tmp_path, _PROFILES)
        mock_keyring = MagicMock()
        mock_keyring.get_password.return_value = "NRAK-ss-key"

        cached = mock_intelligence.model_dump(mode="json")
        cached["account_id"] = "3503435"
        fake_cache = MagicMock()
        fake_cache.get.return_value = cached
        monkeypatch.setattr("sherlock.core.target._cache", fake_cache)

        with (
            patch("sherlock.core.credentials.PROFILES_FILE", profiles),
            patch("sherlock.core.credentials.CONFIG_DIR", tmp_path),
            patch("sherlock.core.credentials.keyring", mock_keyring),
        ):
            credentials, intelligence, client, overridden = resolve_call_target(profile="dfin-ss")

        assert credentials.account_id == "3503435"
        assert intelligence is not None
        assert intelligence.apm.service_names == mock_intelligence.apm.service_names
        assert overridden is True


def _nrql_of(request) -> str:
    """Extract the NRQL variable (or raw query) from a NerdGraph request."""
    body = json.loads(request.content)
    return str((body.get("variables") or {}).get("nrql", "")) or body.get("query", "")


class TestResourceFilterPlumbing:
    """resource_filter reaches the NRQL of the service-scoped tools."""

    @respx.mock
    @pytest.mark.asyncio
    async def test_get_app_metrics_scopes_queries(self, mock_context):
        from sherlock.tools.apm import get_app_metrics

        seen: list[str] = []

        def _capture(request, route):
            seen.append(_nrql_of(request))
            return httpx.Response(
                200, json={"data": {"actor": {"account": {"nrql": {"results": []}}}}}
            )

        respx.post("https://api.newrelic.com/graphql").mock(side_effect=_capture)

        result = await get_app_metrics("payment-svc-prod", resource_filter="gail-v2-di")
        parsed = json.loads(result)

        assert parsed["resource_filter"] == "gail-v2-di"
        metric_queries = [q for q in seen if "FROM Transaction" in q or "FROM Span" in q]
        assert metric_queries
        assert all("cloud.resource_id LIKE '%gail-v2-di%'" in q for q in metric_queries)

    @respx.mock
    @pytest.mark.asyncio
    async def test_get_slow_traces_scopes_queries(self, mock_context):
        from sherlock.tools.traces import get_slow_traces

        seen: list[str] = []

        def _capture(request, route):
            seen.append(_nrql_of(request))
            return httpx.Response(
                200, json={"data": {"actor": {"account": {"nrql": {"results": []}}}}}
            )

        respx.post("https://api.newrelic.com/graphql").mock(side_effect=_capture)

        result = await get_slow_traces("payment-svc-prod", resource_filter="func-dev-di")
        parsed = json.loads(result)

        assert parsed["resource_filter"] == "func-dev-di"
        span_queries = [q for q in seen if "FROM Span" in q]
        assert span_queries
        assert all("cloud.resource_id LIKE '%func-dev-di%'" in q for q in span_queries)
        # The clause must sit before FACET in the slow-traces query.
        faceted = [q for q in span_queries if " FACET " in q]
        assert faceted
        for q in faceted:
            assert q.find("cloud.resource_id") < q.find(" FACET ")
