"""
Tests for intelligence management tools.
"""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
import respx

from sherlock.core.context import AccountContext
from sherlock.tools.intelligence_tools import (
    connect_account,
    get_account_summary,
    get_nrql_context,
    learn_account_tool,
    list_profiles,
)


class TestConnectAccount:
    """Tests for connect_account."""

    @pytest.mark.asyncio
    @patch("sherlock.tools.intelligence_tools._credential_manager")
    @patch("sherlock.tools.intelligence_tools.learn_account", new_callable=AsyncMock)
    @patch("sherlock.tools.intelligence_tools._cache")
    async def test_successful_connection(
        self, mock_cache, mock_learn, mock_cred_mgr, mock_intelligence
    ):
        """Successful connection returns status=connected."""
        AccountContext.reset_singleton()
        mock_cred_mgr.validate_credentials = AsyncMock(
            return_value={
                "valid": True,
                "account_name": "Acme Corp",
                "user_name": "test-user",
            }
        )
        mock_cache.get.return_value = None
        mock_cache.get_stale.return_value = None
        mock_learn.return_value = mock_intelligence

        result = await connect_account("123456", "NRAK-key", "US")
        parsed = json.loads(result)

        assert parsed["status"] == "connected"
        assert parsed["account_id"] == "123456"
        assert parsed["account_name"] == "Acme Corp"
        assert "summary" in parsed
        AccountContext.reset_singleton()

    @pytest.mark.asyncio
    @patch("sherlock.tools.intelligence_tools._credential_manager")
    async def test_invalid_credentials(self, mock_cred_mgr):
        """Invalid credentials return error."""
        mock_cred_mgr.validate_credentials = AsyncMock(
            return_value={
                "valid": False,
                "error": "Invalid API key",
            }
        )

        result = await connect_account("123456", "bad-key", "US")
        parsed = json.loads(result)

        assert "error" in parsed
        assert "Credential validation failed" in parsed["error"]
        assert parsed["data_available"] is False

    @pytest.mark.asyncio
    @patch("sherlock.tools.intelligence_tools._credential_manager")
    @patch("sherlock.tools.intelligence_tools._cache")
    async def test_uses_cached_intelligence(self, mock_cache, mock_cred_mgr, mock_intelligence):
        """Uses cached intelligence when available."""
        AccountContext.reset_singleton()
        mock_cred_mgr.validate_credentials = AsyncMock(
            return_value={
                "valid": True,
                "account_name": "Acme",
                "user_name": "user",
            }
        )
        mock_cache.get.return_value = mock_intelligence.model_dump(mode="json")

        result = await connect_account("123456", "NRAK-key", "US")
        parsed = json.loads(result)

        assert parsed["status"] == "connected"
        AccountContext.reset_singleton()

    @pytest.mark.asyncio
    @patch("sherlock.tools.intelligence_tools._credential_manager")
    @patch("sherlock.tools.intelligence_tools._cache")
    @patch("sherlock.tools.intelligence_tools.learn_account", new_callable=AsyncMock)
    async def test_saves_profile_when_requested(
        self, mock_learn, mock_cache, mock_cred_mgr, mock_intelligence
    ):
        """Profile is saved when profile_name is provided."""
        AccountContext.reset_singleton()
        mock_cred_mgr.validate_credentials = AsyncMock(
            return_value={
                "valid": True,
                "account_name": "Acme",
                "user_name": "user",
            }
        )
        mock_cache.get.return_value = None
        mock_cache.get_stale.return_value = None
        mock_learn.return_value = mock_intelligence

        result = await connect_account("123456", "NRAK-key", "US", profile_name="prod")
        parsed = json.loads(result)

        assert parsed["profile_saved"] is True
        mock_cred_mgr.save_profile.assert_called_once_with("prod", "123456", "NRAK-key", "US")
        AccountContext.reset_singleton()

    @pytest.mark.asyncio
    @patch("sherlock.tools.intelligence_tools._credential_manager")
    @patch("sherlock.tools.intelligence_tools.learn_account", new_callable=AsyncMock)
    @patch("sherlock.tools.intelligence_tools._cache")
    async def test_explicit_api_key_returns_deprecation_warning(
        self, mock_cache, mock_learn, mock_cred_mgr, mock_intelligence
    ):
        """S2: passing api_key still connects but flags the deprecation."""
        AccountContext.reset_singleton()
        mock_cred_mgr.validate_credentials = AsyncMock(
            return_value={
                "valid": True,
                "account_name": "Acme Corp",
                "user_name": "test-user",
            }
        )
        mock_cache.get.return_value = None
        mock_cache.get_stale.return_value = None
        mock_learn.return_value = mock_intelligence

        result = await connect_account("123456", "NRAK-key", "US")
        parsed = json.loads(result)

        assert parsed["status"] == "connected"
        assert "DEPRECATED" in parsed["deprecation_warning"]
        assert "profile_name" in parsed["deprecation_warning"]
        # The warning must never echo the key itself.
        assert "NRAK-key" not in parsed["deprecation_warning"]
        AccountContext.reset_singleton()

    @pytest.mark.asyncio
    @patch("sherlock.tools.intelligence_tools._credential_manager")
    @patch("sherlock.tools.intelligence_tools._cache")
    async def test_profile_connection_has_no_deprecation_warning(
        self, mock_cache, mock_cred_mgr, mock_intelligence
    ):
        """Connecting via profile_name (keychain) is the clean path."""
        AccountContext.reset_singleton()
        loaded = MagicMock()
        loaded.account_id = "123456"
        loaded.api_key = "NRAK-from-keychain"
        loaded.region = "US"
        mock_cred_mgr.load_profile.return_value = loaded
        mock_cred_mgr.validate_credentials = AsyncMock(
            return_value={
                "valid": True,
                "account_name": "Acme",
                "user_name": "user",
            }
        )
        mock_cache.get.return_value = mock_intelligence.model_dump(mode="json")

        result = await connect_account(profile_name="prod")
        parsed = json.loads(result)

        assert parsed["status"] == "connected"
        assert "deprecation_warning" not in parsed
        AccountContext.reset_singleton()

    @pytest.mark.asyncio
    async def test_missing_credentials_hint_points_to_cli(self):
        """The no-credentials error steers toward the profile CLI."""
        result = await connect_account()
        parsed = json.loads(result)

        assert "error" in parsed
        assert "profile_name" in parsed["error"]
        assert "sherlock-validate" in parsed["hint"]


class TestLearnAccountTool:
    """Tests for learn_account_tool."""

    @pytest.mark.asyncio
    @patch("sherlock.tools.intelligence_tools.learn_account", new_callable=AsyncMock)
    @patch("sherlock.tools.intelligence_tools._cache")
    async def test_successful_refresh(
        self, mock_cache, mock_learn, mock_context, mock_intelligence
    ):
        """Re-learn returns refreshed status."""
        mock_cache.get.return_value = None  # No cache → full learn path
        mock_learn.return_value = mock_intelligence

        result = await learn_account_tool()
        parsed = json.loads(result)

        assert parsed["status"] == "refreshed"
        assert parsed["account_id"] == "123456"
        mock_cache.invalidate.assert_called_once_with("123456")

    @pytest.mark.asyncio
    async def test_not_connected_returns_error(self):
        """Error when no active context."""
        AccountContext.reset_singleton()
        ctx = AccountContext()
        ctx.clear()

        result = await learn_account_tool()
        parsed = json.loads(result)

        assert "error" in parsed
        AccountContext.reset_singleton()


class TestGetAccountSummary:
    """Tests for get_account_summary."""

    @pytest.mark.asyncio
    async def test_returns_full_summary(self, mock_context):
        """Returns complete intelligence summary."""
        result = await get_account_summary()
        parsed = json.loads(result)

        assert parsed["account_id"] == "123456"
        assert parsed["account_name"] == "Acme Corp Production"
        assert "apm" in parsed
        assert "k8s" in parsed
        assert "alerts" in parsed
        assert "logs" in parsed
        assert "synthetics" in parsed
        assert "infra" in parsed
        assert "browser" in parsed
        assert parsed["apm"]["service_names"] == [
            "payment-svc-prod",
            "auth-service-prod",
            "export-worker-prod",
        ]

    @pytest.mark.asyncio
    async def test_not_connected_returns_error(self):
        """Error when no active context."""
        AccountContext.reset_singleton()
        ctx = AccountContext()
        ctx.clear()

        result = await get_account_summary()
        parsed = json.loads(result)

        assert "error" in parsed
        AccountContext.reset_singleton()


class TestListProfiles:
    """Tests for list_profiles."""

    @pytest.mark.asyncio
    @patch("sherlock.tools.intelligence_tools._credential_manager")
    async def test_returns_profiles(self, mock_cred_mgr):
        """Returns profile list."""
        mock_cred_mgr.list_profiles.return_value = [
            {"name": "prod", "account_id": "123456"},
            {"name": "staging", "account_id": "789012"},
        ]

        result = await list_profiles()
        parsed = json.loads(result)

        assert parsed["total_profiles"] == 2
        assert len(parsed["profiles"]) == 2

    @pytest.mark.asyncio
    @patch("sherlock.tools.intelligence_tools._credential_manager")
    async def test_empty_profiles(self, mock_cred_mgr):
        """No profiles returns empty list."""
        mock_cred_mgr.list_profiles.return_value = []

        result = await list_profiles()
        parsed = json.loads(result)

        assert parsed["total_profiles"] == 0
        assert parsed["profiles"] == []


class TestGetNrqlContext:
    """Tests for get_nrql_context."""

    @pytest.mark.asyncio
    async def test_all_domains(self, mock_context, mock_nerdgraph):
        """Domain='all' returns all sections."""
        result = await get_nrql_context("all")
        parsed = json.loads(result)

        assert parsed["domain"] == "all"
        assert "apm" in parsed
        assert "k8s" in parsed
        assert "logs" in parsed
        assert "alerts" in parsed
        assert "synthetics" in parsed
        # Live discovery ran against the (empty-result) mock NerdGraph and
        # degraded gracefully rather than erroring or being omitted.
        assert parsed["logs"]["live_schema"]["available"] is False

    @pytest.mark.asyncio
    async def test_apm_domain_only(self, mock_context):
        """Domain='apm' returns only apm section."""
        result = await get_nrql_context("apm")
        parsed = json.loads(result)

        assert parsed["domain"] == "apm"
        assert "apm" in parsed
        assert "k8s" not in parsed
        assert "payment-svc-prod" in parsed["apm"]["service_names"]

    @pytest.mark.asyncio
    async def test_k8s_domain(self, mock_context):
        """Domain='k8s' returns k8s context."""
        result = await get_nrql_context("k8s")
        parsed = json.loads(result)

        assert "k8s" in parsed
        assert "payments-prod" in parsed["k8s"]["namespaces"]
        assert "main-cluster-prod" in parsed["k8s"]["cluster_names"]

    @pytest.mark.asyncio
    async def test_synthetics_domain(self, mock_context):
        """Domain='synthetics' returns monitor context."""
        result = await get_nrql_context("synthetics")
        parsed = json.loads(result)

        assert "synthetics" in parsed
        assert "Login Flow - Production" in parsed["synthetics"]["monitor_names"]
        assert "SCRIPT_BROWSER" in parsed["synthetics"]["monitor_types"]
        assert len(parsed["synthetics"]["available_locations"]) > 0

    @pytest.mark.asyncio
    async def test_not_connected_returns_error(self):
        """Error when no active context."""
        AccountContext.reset_singleton()
        ctx = AccountContext()
        ctx.clear()

        result = await get_nrql_context()
        parsed = json.loads(result)

        assert "error" in parsed
        AccountContext.reset_singleton()


class TestLiveLogSchemaDiscovery:
    """Tests for get_nrql_context's live Log attribute discovery (P4)."""

    def _nrql_of(self, request) -> str:
        body = json.loads(request.content)
        return str((body.get("variables") or {}).get("nrql", ""))

    @respx.mock
    @pytest.mark.asyncio
    async def test_allkeys_shape_with_sample_values(self, mock_context):
        """keyset() allKeys shape → keys, types, and candidate sample values."""

        def _side_effect(request, route):
            nrql = self._nrql_of(request)
            if "keyset()" in nrql:
                results = [
                    {
                        "allKeys": ["message", "timestamp", "entity.name", "severity.text"],
                        "stringKeys": ["message", "entity.name", "severity.text"],
                        "numericKeys": ["timestamp"],
                        "booleanKeys": [],
                    }
                ]
            elif "uniques" in nrql:
                results = [
                    {
                        "entity.name": ["eswd-dev/client-service"],
                        "severity.text": ["Information", "Warning", "Error"],
                    }
                ]
            else:
                results = []
            return httpx.Response(
                200, json={"data": {"actor": {"account": {"nrql": {"results": results}}}}}
            )

        respx.post("https://api.newrelic.com/graphql").mock(side_effect=_side_effect)

        result = await get_nrql_context("logs")
        parsed = json.loads(result)
        live = parsed["logs"]["live_schema"]

        assert live["available"] is True
        assert live["total_attributes"] == 4
        assert "entity.name" in live["attribute_keys"]
        assert live["service_attribute_candidates"]["entity.name"] == ["eswd-dev/client-service"]
        assert live["severity_attribute_candidates"]["severity.text"] == [
            "Information",
            "Warning",
            "Error",
        ]
        assert live["attribute_types"]["severity.text"] == "string"

    @respx.mock
    @pytest.mark.asyncio
    async def test_key_type_rows_shape(self, mock_context):
        """keyset() one-row-per-key shape is handled too."""

        def _side_effect(request, route):
            nrql = self._nrql_of(request)
            if "keyset()" in nrql:
                results = [
                    {"key": "message", "type": "string"},
                    {"key": "level", "type": "string"},
                    {"key": "service.name", "type": "string"},
                ]
            elif "uniques" in nrql:
                results = [{"service.name": ["web-api"], "level": ["INFO", "ERROR"]}]
            else:
                results = []
            return httpx.Response(
                200, json={"data": {"actor": {"account": {"nrql": {"results": results}}}}}
            )

        respx.post("https://api.newrelic.com/graphql").mock(side_effect=_side_effect)

        result = await get_nrql_context("logs")
        parsed = json.loads(result)
        live = parsed["logs"]["live_schema"]

        assert live["available"] is True
        assert live["service_attribute_candidates"]["service.name"] == ["web-api"]
        assert live["severity_attribute_candidates"]["level"] == ["INFO", "ERROR"]

    @respx.mock
    @pytest.mark.asyncio
    async def test_no_log_data_degrades_gracefully(self, mock_context):
        """Empty keyset → available=False with a reason, context still returned."""
        respx.post("https://api.newrelic.com/graphql").mock(
            return_value=httpx.Response(
                200, json={"data": {"actor": {"account": {"nrql": {"results": []}}}}}
            )
        )

        result = await get_nrql_context("logs")
        parsed = json.loads(result)
        live = parsed["logs"]["live_schema"]

        assert live["available"] is False
        assert "no Log data" in live["reason"]
        # The static context is intact regardless.
        assert parsed["logs"]["event_type"] == "Log"

    @respx.mock
    @pytest.mark.asyncio
    async def test_discover_false_skips_queries(self, mock_context):
        """discover=False makes zero network calls and omits live_schema."""
        route = respx.post("https://api.newrelic.com/graphql").mock(
            return_value=httpx.Response(
                200, json={"data": {"actor": {"account": {"nrql": {"results": []}}}}}
            )
        )

        result = await get_nrql_context("logs", discover=False)
        parsed = json.loads(result)

        assert "live_schema" not in parsed["logs"]
        assert route.call_count == 0


class TestConnectAccountIntelligenceSource:
    """Tests for intelligence_source, skip_learn_account, and naming_convention
    fields added to connect_account response."""

    @pytest.mark.asyncio
    @patch("sherlock.tools.intelligence_tools._credential_manager")
    @patch("sherlock.tools.intelligence_tools._cache")
    async def test_cached_returns_source_cached(self, mock_cache, mock_cred_mgr, mock_intelligence):
        """connect_account with fresh cache returns intelligence_source='cached'."""
        AccountContext.reset_singleton()
        mock_cred_mgr.validate_credentials = AsyncMock(
            return_value={
                "valid": True,
                "account_name": "Acme Corp",
                "user_name": "test-user",
            }
        )
        mock_cache.get.return_value = mock_intelligence.model_dump(mode="json")

        result = await connect_account("123456", "NRAK-key", "US")
        parsed = json.loads(result)

        assert parsed["intelligence_source"] == "cached"
        AccountContext.reset_singleton()

    @pytest.mark.asyncio
    @patch("sherlock.tools.intelligence_tools._credential_manager")
    @patch("sherlock.tools.intelligence_tools._cache")
    async def test_stale_returns_source_stale(self, mock_cache, mock_cred_mgr, mock_intelligence):
        """connect_account with stale cache returns intelligence_source='stale_with_bg_refresh'."""
        AccountContext.reset_singleton()
        mock_cred_mgr.validate_credentials = AsyncMock(
            return_value={
                "valid": True,
                "account_name": "Acme Corp",
                "user_name": "test-user",
            }
        )
        mock_cache.get.return_value = None
        mock_cache.get_stale.return_value = mock_intelligence.model_dump(mode="json")

        result = await connect_account("123456", "NRAK-key", "US")
        parsed = json.loads(result)

        assert parsed["intelligence_source"] == "stale_with_bg_refresh"
        AccountContext.reset_singleton()

    @pytest.mark.asyncio
    @patch("sherlock.tools.intelligence_tools._credential_manager")
    @patch("sherlock.tools.intelligence_tools.learn_account", new_callable=AsyncMock)
    @patch("sherlock.tools.intelligence_tools._cache")
    async def test_no_cache_returns_source_fresh(
        self, mock_cache, mock_learn, mock_cred_mgr, mock_intelligence
    ):
        """connect_account with no cache returns intelligence_source='fresh_learn'."""
        AccountContext.reset_singleton()
        mock_cred_mgr.validate_credentials = AsyncMock(
            return_value={
                "valid": True,
                "account_name": "Acme Corp",
                "user_name": "test-user",
            }
        )
        mock_cache.get.return_value = None
        mock_cache.get_stale.return_value = None
        mock_learn.return_value = mock_intelligence

        result = await connect_account("123456", "NRAK-key", "US")
        parsed = json.loads(result)

        assert parsed["intelligence_source"] == "fresh_learn"
        AccountContext.reset_singleton()

    @pytest.mark.asyncio
    @patch("sherlock.tools.intelligence_tools._credential_manager")
    @patch("sherlock.tools.intelligence_tools._cache")
    async def test_skip_learn_true_for_cached(self, mock_cache, mock_cred_mgr, mock_intelligence):
        """connect_account returns skip_learn_account=True when cached."""
        AccountContext.reset_singleton()
        mock_cred_mgr.validate_credentials = AsyncMock(
            return_value={
                "valid": True,
                "account_name": "Acme",
                "user_name": "user",
            }
        )
        mock_cache.get.return_value = mock_intelligence.model_dump(mode="json")

        result = await connect_account("123456", "NRAK-key", "US")
        parsed = json.loads(result)

        assert parsed["skip_learn_account"] is True
        assert "skip_learn_reason" in parsed
        AccountContext.reset_singleton()

    @pytest.mark.asyncio
    @patch("sherlock.tools.intelligence_tools._credential_manager")
    @patch("sherlock.tools.intelligence_tools.learn_account", new_callable=AsyncMock)
    @patch("sherlock.tools.intelligence_tools._cache")
    async def test_skip_learn_false_for_fresh(
        self, mock_cache, mock_learn, mock_cred_mgr, mock_intelligence
    ):
        """connect_account returns skip_learn_account=False when fresh learn."""
        AccountContext.reset_singleton()
        mock_cred_mgr.validate_credentials = AsyncMock(
            return_value={
                "valid": True,
                "account_name": "Acme",
                "user_name": "user",
            }
        )
        mock_cache.get.return_value = None
        mock_cache.get_stale.return_value = None
        mock_learn.return_value = mock_intelligence

        result = await connect_account("123456", "NRAK-key", "US")
        parsed = json.loads(result)

        assert parsed["skip_learn_account"] is False
        assert "skip_learn_reason" not in parsed
        AccountContext.reset_singleton()

    @pytest.mark.asyncio
    @patch("sherlock.tools.intelligence_tools._credential_manager")
    @patch("sherlock.tools.intelligence_tools._cache")
    async def test_naming_convention_in_response(
        self, mock_cache, mock_cred_mgr, mock_intelligence
    ):
        """connect_account response includes naming_convention summary."""
        AccountContext.reset_singleton()
        mock_cred_mgr.validate_credentials = AsyncMock(
            return_value={
                "valid": True,
                "account_name": "Acme",
                "user_name": "user",
            }
        )
        mock_cache.get.return_value = mock_intelligence.model_dump(mode="json")

        result = await connect_account("123456", "NRAK-key", "US")
        parsed = json.loads(result)

        assert "naming_convention" in parsed
        nc = parsed["naming_convention"]
        assert "separator" in nc
        assert "env_position" in nc
        assert "env_values" in nc
        assert "apm_to_k8s_namespace_map" in nc
        assert "k8s_deployment_name_format" in nc
        assert "segment_roles" in nc
        AccountContext.reset_singleton()


class TestLearnAccountShortCircuit:
    """Tests for learn_account_tool server-side cache guardrail."""

    @pytest.mark.asyncio
    @patch("sherlock.tools.intelligence_tools._cache")
    async def test_returns_already_learned_when_cached(
        self, mock_cache, mock_context, mock_intelligence
    ):
        """learn_account short-circuits with 'already_learned' when cache has data."""
        mock_cache.get.return_value = mock_intelligence.model_dump(mode="json")

        result = await learn_account_tool()
        parsed = json.loads(result)

        assert parsed["status"] == "already_learned"
        assert parsed["account_id"] == "123456"
        assert "hint" in parsed
        mock_cache.invalidate.assert_not_called()

    @pytest.mark.asyncio
    @patch("sherlock.tools.intelligence_tools.learn_account", new_callable=AsyncMock)
    @patch("sherlock.tools.intelligence_tools._cache")
    async def test_force_true_bypasses_cache(
        self, mock_cache, mock_learn, mock_context, mock_intelligence
    ):
        """learn_account with force=True always re-learns even with cache."""
        mock_cache.get.return_value = mock_intelligence.model_dump(mode="json")
        mock_learn.return_value = mock_intelligence

        result = await learn_account_tool(force=True)
        parsed = json.loads(result)

        assert parsed["status"] == "refreshed"
        mock_cache.invalidate.assert_called_once_with("123456")

    @pytest.mark.asyncio
    @patch("sherlock.tools.intelligence_tools.learn_account", new_callable=AsyncMock)
    @patch("sherlock.tools.intelligence_tools._cache")
    async def test_no_cache_does_full_learn(
        self, mock_cache, mock_learn, mock_context, mock_intelligence
    ):
        """learn_account without cache performs full learn."""
        mock_cache.get.return_value = None
        mock_learn.return_value = mock_intelligence

        result = await learn_account_tool()
        parsed = json.loads(result)

        assert parsed["status"] == "refreshed"
        mock_cache.invalidate.assert_called_once_with("123456")
