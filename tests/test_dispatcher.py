"""
Dispatcher-level tests for main.py (FastMCP server).

Drives every registered tool through FastMCP's call_tool exactly as an MCP
client would, so handler/schema signature mismatches are caught here
instead of in production (regression: the resolve_account lambda bug,
where the dispatcher's handler(**arguments) call blew up on a handler
that expected a single positional dict).

Schemas are now DERIVED from the tool functions' type hints by FastMCP,
so these tests also pin the derived schemas' key properties (enums,
required params) against what the Copilot agents rely on.
"""

import asyncio
import inspect
import json
import logging
from unittest.mock import AsyncMock, patch

import pytest
from mcp.server.fastmcp.exceptions import ToolError
from mcp.types import TextContent

from sherlock.core.account_memory import AccountMemory
from sherlock.main import TOOL_HANDLERS, _warn_if_plaintext_key, server

# Materialize the FastMCP-derived tool list once for parametrization.
TOOLS = asyncio.run(server.list_tools())

# Error-message fragments that indicate the dispatcher could not call the
# handler with the schema-declared kwargs. Domain errors (bad NRQL, missing
# entity, etc.) are fine here — signature errors are not.
_SIGNATURE_ERROR_FRAGMENTS = (
    "unexpected keyword argument",
    "positional argument",
    "missing a required argument",
    "required positional",
    "validation error",
)

# Sample values keyed by property name, aligned with the conftest fixtures.
_SAMPLE_STRINGS = {
    "service_name": "payment-svc-prod",
    "app_name": "payment-svc-prod",
    "monitor_name": "Login Flow - Production",
    "nrql": "SELECT count(*) FROM Transaction SINCE 30 minutes ago",
    "keyword": "timeout",
    "namespace": "payments-prod",
    "cluster_name": "main-cluster-prod",
    "prompt": "why is checkout still failing",
    "severity": "ERROR",
    "account_id": "123456",
    "api_key": "NRAK-test123456789abcdef",
    "profile_name": "test-profile",
}


def _sample_value(name: str, spec: dict):
    """Generate a schema-valid sample value for one inputSchema property."""
    if "enum" in spec:
        return spec["enum"][0]
    if "const" in spec:
        return spec["const"]
    if "anyOf" in spec:
        # Optional/union params (e.g. Literal[...] | None): sample the first
        # non-null subschema.
        for sub in spec["anyOf"]:
            if sub.get("type") != "null":
                return _sample_value(name, sub)
    prop_type = spec.get("type", "string")
    if prop_type == "string":
        return _SAMPLE_STRINGS.get(name, "payment-svc-prod")
    if prop_type == "integer":
        return spec.get("default", 5)
    if prop_type == "number":
        return spec.get("default", 5.0)
    if prop_type == "boolean":
        return spec.get("default", False)
    raise AssertionError(f"Unhandled schema type {prop_type!r} for property {name!r}")


def _args_for(tool) -> dict:
    """Build a full kwargs dict covering every property in a tool's schema."""
    properties = tool.inputSchema.get("properties", {})
    return {name: _sample_value(name, spec) for name, spec in properties.items()}


@pytest.fixture(autouse=True)
def _isolated_account_memory(tmp_path):
    """Redirect AccountMemory writes to a temp file for every test here."""
    AccountMemory.reset_singleton()
    original = AccountMemory.MEMORY_FILE
    AccountMemory.MEMORY_FILE = tmp_path / "account_memory.json"
    yield
    AccountMemory.MEMORY_FILE = original
    AccountMemory.reset_singleton()


@pytest.fixture
def _patched_intelligence_singletons(mock_intelligence):
    """Keep connect/learn tools off the network, keyring, and disk cache."""
    with (
        patch("sherlock.tools.intelligence_tools._credential_manager") as cred_mgr,
        patch("sherlock.tools.intelligence_tools._cache") as cache,
        patch("sherlock.tools.intelligence_tools.learn_account", new_callable=AsyncMock) as learn,
    ):
        cred_mgr.validate_credentials = AsyncMock(
            return_value={
                "valid": True,
                "account_name": "Acme Corp",
                "user_name": "test-user",
            }
        )
        cred_mgr.list_profiles.return_value = []
        cache.get.return_value = mock_intelligence.model_dump(mode="json")
        cache.get_stale.return_value = None
        learn.return_value = mock_intelligence
        yield


class TestToolRegistry:
    """Consistency checks between the FastMCP registry and TOOL_HANDLERS."""

    def test_tools_and_handlers_parity(self):
        """Every registered tool has a raw handler and vice versa."""
        tool_names = {tool.name for tool in TOOLS}
        handler_names = set(TOOL_HANDLERS.keys())
        assert tool_names == handler_names
        assert len(TOOLS) == 25

    def test_every_tool_has_description(self):
        """FastMCP registration must preserve the curated descriptions."""
        for tool in TOOLS:
            assert tool.description, f"Tool {tool.name} has no description"

    @pytest.mark.parametrize("tool", TOOLS, ids=lambda t: t.name)
    def test_handler_signature_accepts_schema_properties(self, tool):
        """Each handler must accept its schema's properties as kwargs.

        The schema is derived FROM the handler now, so this is a sanity
        check that derivation produced bindable properties.
        """
        handler = TOOL_HANDLERS[tool.name]
        assert inspect.iscoroutinefunction(handler), (
            f"Handler for {tool.name} must be an async function"
        )
        signature = inspect.signature(handler)
        try:
            signature.bind(**_args_for(tool))
        except TypeError as exc:
            pytest.fail(f"Handler for {tool.name} cannot bind schema kwargs: {exc}")


class TestDerivedSchemas:
    """Pin key properties of the type-hint-derived schemas."""

    def _schema(self, name: str) -> dict:
        return next(t for t in TOOLS if t.name == name).inputSchema

    def _enum_of(self, spec: dict) -> list:
        """Extract the enum values from a (possibly anyOf-wrapped) spec."""
        if "enum" in spec:
            return spec["enum"]
        for sub in spec.get("anyOf", []):
            if "enum" in sub:
                return sub["enum"]
        raise AssertionError(f"No enum found in spec: {spec}")

    def test_connect_account_region_enum(self):
        props = self._schema("connect_account")["properties"]
        assert set(self._enum_of(props["region"])) == {"US", "EU"}

    def test_structured_report_format_enum(self):
        props = self._schema("get_structured_report")["properties"]
        assert set(self._enum_of(props["format"])) == {"full", "summary", "metrics"}

    def test_nrql_context_domain_enum(self):
        props = self._schema("get_nrql_context")["properties"]
        assert set(self._enum_of(props["domain"])) == {
            "apm",
            "k8s",
            "logs",
            "alerts",
            "synthetics",
            "all",
        }

    def test_incidents_state_enum(self):
        props = self._schema("get_incidents")["properties"]
        assert set(self._enum_of(props["state"])) == {"open", "closed"}

    def test_dependencies_direction_enum(self):
        props = self._schema("get_service_dependencies")["properties"]
        assert set(self._enum_of(props["direction"])) == {"downstream", "upstream", "both"}

    def test_monitor_results_filter_enum(self):
        props = self._schema("get_monitor_results")["properties"]
        assert set(self._enum_of(props["result_filter"])) == {"FAILED", "SUCCESS"}

    def test_required_params_derived_from_signatures(self):
        """Params without defaults must be schema-required."""
        assert self._schema("get_service_golden_signals").get("required") == ["service_name"]
        assert self._schema("run_nrql_query").get("required") == ["nrql"]
        assert self._schema("resolve_account").get("required") == ["service_name"]
        assert self._schema("investigate_synthetic").get("required") == ["monitor_name"]


class TestDispatch:
    """Drive every tool through FastMCP call_tool like a real MCP client."""

    @pytest.mark.parametrize("tool", TOOLS, ids=lambda t: t.name)
    async def test_dispatch_all_tools(
        self, tool, mock_context, mock_nerdgraph, _patched_intelligence_singletons
    ):
        """Each tool dispatches cleanly with schema-valid arguments.

        Domain-level errors in the response JSON are acceptable (mocked
        NerdGraph returns empty results); signature errors are not.
        """
        result = await server.call_tool(tool.name, _args_for(tool))

        assert isinstance(result, list) and len(result) == 1
        assert isinstance(result[0], TextContent)
        parsed = json.loads(result[0].text)
        assert isinstance(parsed, dict)

        error = str(parsed.get("error", ""))
        for fragment in _SIGNATURE_ERROR_FRAGMENTS:
            assert fragment not in error, f"Dispatch signature error for tool {tool.name}: {error}"

    async def test_resolve_account_dispatches_with_kwargs(self, mock_context):
        """Regression test for B1: resolve_account must work via the dispatcher.

        The old TOOL_HANDLERS entry was `lambda args: resolve_account(**args)`,
        which the dispatcher called as handler(service_name=...) → TypeError.
        """
        result = await server.call_tool("resolve_account", {"service_name": "payment-svc-prod"})
        parsed = json.loads(result[0].text)

        assert "error" not in parsed
        assert parsed["status"] in ("FOUND", "STALE", "NOT_FOUND")
        assert parsed["service_name"] == "payment-svc-prod"

    async def test_unknown_tool_raises_tool_error(self):
        """Unknown tool names are rejected by the FastMCP tool manager."""
        with pytest.raises(ToolError, match="Unknown tool"):
            await server.call_tool("no_such_tool", {})

    async def test_invalid_enum_value_is_rejected(self, mock_context):
        """FastMCP validates arguments against the derived schema."""
        with pytest.raises(ToolError):
            await server.call_tool("get_incidents", {"state": "bogus"})


class TestPlaintextKeyWarning:
    """Startup warning when an API key is present in plaintext env/.env."""

    @pytest.fixture(autouse=True)
    def _propagate_sherlock_logs(self):
        """Sherlock logs are file-only (propagate=False); caplog captures via
        the root logger, so re-enable propagation for these tests."""
        sherlock_logger = logging.getLogger("sherlock")
        original = sherlock_logger.propagate
        sherlock_logger.propagate = True
        yield
        sherlock_logger.propagate = original

    def test_warns_when_key_present(self, caplog):
        with caplog.at_level(logging.WARNING, logger="sherlock.main"):
            assert _warn_if_plaintext_key("NRAK-test123456789abcdef") is True
        assert any(
            "SECURITY WARNING" in record.message and "plaintext" in record.message
            for record in caplog.records
        )

    def test_warning_never_includes_key_material(self, caplog):
        key = "NRAK-test123456789abcdef"
        with caplog.at_level(logging.WARNING, logger="sherlock.main"):
            _warn_if_plaintext_key(key)
        assert all(key not in record.getMessage() for record in caplog.records)

    def test_silent_when_no_key(self, caplog):
        with caplog.at_level(logging.WARNING, logger="sherlock.main"):
            assert _warn_if_plaintext_key("") is False
        assert not caplog.records
