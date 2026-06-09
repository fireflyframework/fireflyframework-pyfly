# Copyright 2026 Firefly Software Foundation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Tests for make_httpx_sender — no Docker, uses respx mock transport."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
import respx

from pyfly.callbacks.adapters.httpx_sender import make_httpx_sender
from pyfly.callbacks.dispatcher import CallbackDispatcher
from pyfly.callbacks.models import (
    AuthorizedDomain,
    CallbackConfig,
    CallbackStatus,
    CallbackSubscription,
)
from pyfly.callbacks.repository import (
    InMemoryCallbackConfigRepository,
    InMemoryCallbackExecutionRepository,
)
from pyfly.kernel.exceptions import CircuitBreakerException
from pyfly.resilience.circuit_breaker import CircuitBreaker

# ---------------------------------------------------------------------------
# Item 3a — basic sender: status pass-through + body/header assertions
# ---------------------------------------------------------------------------


@respx.mock
@pytest.mark.asyncio
async def test_sender_returns_200_on_success() -> None:
    """Sender returns the upstream status code (200) and sends JSON body + headers."""
    url = "https://example.com/callback"
    payload: dict[str, Any] = {"event": "OrderPlaced", "id": 42}
    extra_headers = {"X-Tenant": "acme", "Content-Type": "application/json"}

    route = respx.post(url).mock(return_value=httpx.Response(200))

    sender = make_httpx_sender()
    status = await sender(url, payload, extra_headers)

    assert status == 200
    assert route.called

    request = route.calls.last.request
    assert request.headers["X-Tenant"] == "acme"
    assert json.loads(request.content) == payload


@respx.mock
@pytest.mark.asyncio
async def test_sender_returns_500_on_server_error() -> None:
    """Sender transparently returns a 500 status from the upstream server."""
    url = "https://example.com/failing"
    respx.post(url).mock(return_value=httpx.Response(500))

    sender = make_httpx_sender()
    status = await sender(url, {"x": 1}, {})

    assert status == 500


@respx.mock
@pytest.mark.asyncio
async def test_sender_propagates_custom_headers() -> None:
    """All headers supplied by the dispatcher are forwarded to the remote endpoint."""
    url = "https://example.com/signed"
    headers = {
        "X-Pyfly-Signature": "sha256=abc123",
        "Content-Type": "application/json",
        "X-Custom": "value",
    }

    route = respx.post(url).mock(return_value=httpx.Response(201))
    sender = make_httpx_sender()
    status = await sender(url, {}, headers)

    assert status == 201
    req = route.calls.last.request
    for key, val in headers.items():
        assert req.headers[key.lower()] == val


# ---------------------------------------------------------------------------
# Item 3b — end-to-end dispatcher + respx: HMAC signing, retry, SSRF
# ---------------------------------------------------------------------------


@respx.mock
@pytest.mark.asyncio
async def test_dispatcher_posts_and_signs_with_hmac() -> None:
    """End-to-end: dispatcher POSTs via real httpx (respx-mocked) and attaches HMAC sig."""
    url = "https://receiver.example.com/events"
    route = respx.post(url).mock(return_value=httpx.Response(200))

    configs = InMemoryCallbackConfigRepository()
    executions = InMemoryCallbackExecutionRepository()

    config = CallbackConfig(
        tenant_id="t1",
        name="signed-e2e",
        secret="mysecret",
        subscriptions=[CallbackSubscription(event_type="OrderPlaced", target_url=url)],
    )
    await configs.save(config)

    sender = make_httpx_sender()
    dispatcher = CallbackDispatcher(configs, executions, http=sender)

    results = await dispatcher.dispatch("t1", "OrderPlaced", {"order_id": 99})

    assert len(results) == 1
    assert results[0].status == CallbackStatus.DELIVERED
    assert route.call_count == 1

    req = route.calls.last.request
    sig_header = req.headers.get("x-pyfly-signature", "")
    assert sig_header.startswith("sha256=")

    # Verify the signature matches the canonical JSON body
    import hashlib
    import hmac as hmac_mod

    body = req.content
    canonical = json.dumps({"order_id": 99}, separators=(",", ":"), sort_keys=True).encode()
    assert body == canonical
    expected_sig = hmac_mod.new(b"mysecret", canonical, hashlib.sha256).hexdigest()
    assert sig_header == f"sha256={expected_sig}"


@respx.mock
@pytest.mark.asyncio
async def test_dispatcher_retries_on_503_then_200() -> None:
    """Dispatcher retries a 503 and succeeds on the second attempt (200)."""
    url = "https://flaky.example.com/hook"

    # First call → 503, second call → 200
    route = respx.post(url).mock(
        side_effect=[
            httpx.Response(503),
            httpx.Response(200),
        ]
    )

    configs = InMemoryCallbackConfigRepository()
    executions = InMemoryCallbackExecutionRepository()

    config = CallbackConfig(
        tenant_id="t2",
        name="retried",
        max_attempts=3,
        backoff_ms=1,  # 1 ms — keeps the test fast
        subscriptions=[CallbackSubscription(event_type="E", target_url=url)],
    )
    await configs.save(config)

    sender = make_httpx_sender()
    dispatcher = CallbackDispatcher(configs, executions, http=sender)

    results = await dispatcher.dispatch("t2", "E", {"k": "v"})

    assert results[0].status == CallbackStatus.DELIVERED
    assert results[0].attempts == 2
    assert route.call_count == 2


@respx.mock
@pytest.mark.asyncio
async def test_dispatcher_ssrf_allowlist_blocks_disallowed_domain() -> None:
    """SSRF guard: a domain NOT in the allowlist is never POSTed."""
    allowed_url = "https://allowed.example.com/hook"
    blocked_url = "https://evil.internal/hook"

    allowed_route = respx.post(allowed_url).mock(return_value=httpx.Response(200))
    # Register but do NOT expect the blocked route to be called
    blocked_route = respx.post(blocked_url).mock(return_value=httpx.Response(200))

    configs = InMemoryCallbackConfigRepository()
    executions = InMemoryCallbackExecutionRepository()

    config = CallbackConfig(
        tenant_id="t3",
        name="guarded",
        authorized_domains=[AuthorizedDomain(domain="allowed.example.com")],
        subscriptions=[
            CallbackSubscription(event_type="E", target_url=allowed_url),
            CallbackSubscription(event_type="E", target_url=blocked_url),
        ],
    )
    await configs.save(config)

    sender = make_httpx_sender()
    dispatcher = CallbackDispatcher(configs, executions, http=sender)
    results = await dispatcher.dispatch("t3", "E", {})

    # Allowed domain → DELIVERED; blocked domain → FAILED (never POSTed)
    statuses = {r.target_url: r.status for r in results}
    assert statuses[allowed_url] == CallbackStatus.DELIVERED
    assert statuses[blocked_url] == CallbackStatus.FAILED
    assert results[1].last_error == "Domain not authorized"

    assert allowed_route.call_count == 1
    assert blocked_route.call_count == 0


# ---------------------------------------------------------------------------
# Item 3c — circuit breaker integration
# ---------------------------------------------------------------------------


@respx.mock
@pytest.mark.asyncio
async def test_circuit_breaker_opens_after_repeated_failures() -> None:
    """After failure_threshold 500s the breaker opens; subsequent calls fast-fail."""
    url = "https://broken.example.com/hook"

    # Every call returns 500 — but the circuit breaker counts *transport* failures
    # (exceptions), not HTTP error statuses.  We therefore make respx raise a
    # connection error so on_failure() is triggered correctly.
    respx.post(url).mock(side_effect=httpx.ConnectError("refused"))

    breaker = CircuitBreaker(failure_threshold=2, recovery_timeout=9999.0)
    sender = make_httpx_sender(breaker=breaker)

    # First two calls trip the circuit (ConnectError → on_failure)
    for _ in range(2):
        with pytest.raises(httpx.ConnectError):
            await sender(url, {}, {})

    from pyfly.resilience.circuit_breaker import CircuitState

    assert breaker.state == CircuitState.OPEN

    # Third call raises CircuitBreakerException immediately — no HTTP round-trip
    with pytest.raises(CircuitBreakerException):
        await sender(url, {}, {})

    # The blocked_route should have been called exactly twice (the two real attempts)
    assert respx.calls.call_count == 2


@respx.mock
@pytest.mark.asyncio
async def test_circuit_breaker_does_not_crash_dispatch() -> None:
    """Open circuit causes FAILED execution, not an unhandled exception in dispatch."""
    url = "https://open-circuit.example.com/hook"

    # Immediately open the circuit breaker
    breaker = CircuitBreaker(failure_threshold=1, recovery_timeout=9999.0)
    # Record one failure to open the circuit
    breaker.on_failure()

    from pyfly.resilience.circuit_breaker import CircuitState

    assert breaker.state == CircuitState.OPEN

    # Sender should NOT be called (circuit open)
    respx.post(url).mock(return_value=httpx.Response(200))

    configs = InMemoryCallbackConfigRepository()
    executions = InMemoryCallbackExecutionRepository()

    config = CallbackConfig(
        tenant_id="t4",
        name="open-circuit",
        max_attempts=2,
        backoff_ms=1,
        subscriptions=[CallbackSubscription(event_type="E", target_url=url)],
    )
    await configs.save(config)

    sender = make_httpx_sender(breaker=breaker)
    dispatcher = CallbackDispatcher(configs, executions, http=sender)

    # dispatch() must complete without raising
    results = await dispatcher.dispatch("t4", "E", {})

    assert len(results) == 1
    assert results[0].status == CallbackStatus.FAILED
    # The error message should mention the circuit breaker
    assert results[0].last_error is not None
    assert "circuit" in (results[0].last_error or "").lower()
    # The HTTP endpoint was never reached
    assert respx.calls.call_count == 0
