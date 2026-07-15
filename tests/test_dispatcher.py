"""
Dispatcher-level tests for main.py.

Drives every registered tool through handle_call_tool exactly as the MCP
server would, so handler/schema signature mismatches are caught here
instead of in production (regression: the resolve_account lambda bug,
where the dispatcher's handler(**arguments) call blew up on a handler
that expected a single positional dict).
"""

import inspect
import json
import logging
from unittest.mock import AsyncMock, patch

import pytest
from mcp.types import TextContent

from sherlock.core.account_memory import AccountMemory
from sherlock.main import TOOL_HANDLERS, TOOLS, _warn_if_plaintext_key, handle_call_tool

# Error-message fragments that indicate the dispatcher could not call the
# handler with the schema-declared kwargs. Domain errors (bad NRQL, missing
# entity, etc.) are fine here — signature errors are not.
_SIGNATURE_ERROR_FRAGMENTS = (
    "unexpected keyword argument",
    "positional argument",
    "missing a required argument",
    "required positional",
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
    """Static consistency checks between TOOLS and TOOL_HANDLERS."""

    def test_tools_and_handlers_parity(self):
        """Every declared tool has a handler and vice versa."""
        tool_names = {tool.name for tool in TOOLS}
        handler_names = set(TOOL_HANDLERS.keys())
        assert tool_names == handler_names
        assert len(TOOLS) == 24

    @pytest.mark.parametrize("tool", TOOLS, ids=lambda t: t.name)
    def test_handler_signature_accepts_schema_properties(self, tool):
        """Each handler must accept its schema's properties as kwargs.

        This is exactly what the dispatcher does (handler(**arguments)),
        so a handler that cannot bind these kwargs is broken over MCP even
        if direct-call unit tests pass.
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


class TestDispatch:
    """Drive every tool through handle_call_tool like a real MCP client."""

    @pytest.mark.parametrize("tool", TOOLS, ids=lambda t: t.name)
    async def test_dispatch_all_tools(
        self, tool, mock_context, mock_nerdgraph, _patched_intelligence_singletons
    ):
        """Each tool dispatches cleanly with schema-valid arguments.

        Domain-level errors in the response JSON are acceptable (mocked
        NerdGraph returns empty results); signature errors are not.
        """
        result = await handle_call_tool(tool.name, _args_for(tool))

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
        result = await handle_call_tool("resolve_account", {"service_name": "payment-svc-prod"})
        parsed = json.loads(result[0].text)

        assert "error" not in parsed
        assert parsed["status"] in ("FOUND", "STALE", "NOT_FOUND")
        assert parsed["service_name"] == "payment-svc-prod"

    async def test_unknown_tool_returns_error_with_available_tools(self):
        """Unknown tool names return a helpful error, not an exception."""
        result = await handle_call_tool("no_such_tool", {})
        parsed = json.loads(result[0].text)

        assert parsed["error"] == "Unknown tool: no_such_tool"
        assert set(parsed["available_tools"]) == set(TOOL_HANDLERS.keys())


class TestPlaintextKeyWarning:
    """Startup warning when an API key is present in plaintext env/.env."""

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
