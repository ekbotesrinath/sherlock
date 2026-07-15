"""
Tests for the shared pooled HTTP client and the 429 retry-wait policy.

The 429 handler used to sleep Retry-After itself AND let tenacity add its
exponential backoff on top (double-wait). Now Retry-After is the single
wait, applied by tenacity's wait strategy.
"""

from unittest.mock import MagicMock

import httpx
import pytest
import respx

from sherlock.client.newrelic import (
    NerdGraphClient,
    _RateLimitedError,
    _RetryableHTTPError,
    _wait_honoring_retry_after,
    close_shared_http_client,
    get_shared_http_client,
)
from sherlock.core.credentials import Credentials

ENDPOINT = "https://api.newrelic.com/graphql"


@pytest.fixture
def credentials() -> Credentials:
    return Credentials(account_id="123456", api_key="NRAK-test123", region="US")


def _retry_state_with(exc: BaseException | None, attempt: int = 1) -> MagicMock:
    """Build a minimal tenacity RetryCallState stand-in."""
    state = MagicMock()
    state.attempt_number = attempt
    if exc is None:
        state.outcome = None
    else:
        state.outcome.exception.return_value = exc
    return state


class TestWaitStrategy:
    """Retry-After must be the ONLY wait applied on 429s."""

    def test_rate_limited_waits_exactly_retry_after(self):
        state = _retry_state_with(_RateLimitedError(7))
        assert _wait_honoring_retry_after(state) == 7.0

    def test_rate_limited_zero_wait(self):
        state = _retry_state_with(_RateLimitedError(0))
        assert _wait_honoring_retry_after(state) == 0.0

    def test_server_error_uses_exponential_backoff(self):
        state = _retry_state_with(_RetryableHTTPError("HTTP 503"), attempt=1)
        wait = _wait_honoring_retry_after(state)
        assert 0.0 <= wait <= 4.0

    def test_no_outcome_falls_back_to_exponential(self):
        state = _retry_state_with(None, attempt=1)
        wait = _wait_honoring_retry_after(state)
        assert 0.0 <= wait <= 4.0


class TestRateLimitRetry:
    @respx.mock
    async def test_429_retries_and_succeeds(self, credentials):
        """A 429 with Retry-After: 0 retries immediately and succeeds."""
        route = respx.post(ENDPOINT)
        route.side_effect = [
            httpx.Response(429, headers={"Retry-After": "0"}),
            httpx.Response(200, json={"data": {"actor": {}}}),
        ]

        client = NerdGraphClient(credentials)
        body = await client.query("{ actor { user { name } } }")

        assert body == {"data": {"actor": {}}}
        assert route.call_count == 2

    @respx.mock
    async def test_429_invalid_retry_after_defaults(self, credentials):
        """A malformed Retry-After header falls back to the 2s default."""
        route = respx.post(ENDPOINT)
        route.side_effect = [
            httpx.Response(429, headers={"Retry-After": "soon"}),
            httpx.Response(200, json={"data": {}}),
        ]

        nr_client = NerdGraphClient(credentials)
        body = await nr_client.query("{ actor { user { name } } }")

        assert body == {"data": {}}
        assert route.call_count == 2


class TestSharedClient:
    async def test_same_instance_within_loop(self):
        first = get_shared_http_client()
        second = get_shared_http_client()
        assert first is second
        assert not first.is_closed

    async def test_close_then_reopen(self):
        first = get_shared_http_client()
        await close_shared_http_client()
        assert first.is_closed
        second = get_shared_http_client()
        assert second is not first
        assert not second.is_closed
