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
"""Tests for IdempotencyWebFilter — caching, replay, disable marker, method gating."""

from __future__ import annotations

import pytest
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from pyfly.cache.adapters.memory import InMemoryCache
from pyfly.container.ordering import HIGHEST_PRECEDENCE
from pyfly.web.adapters.starlette.filter_chain import WebFilterChainMiddleware
from pyfly.web.adapters.starlette.filters.idempotency_filter import (
    IDEMPOTENCY_KEY_HEADER,
    IDEMPOTENCY_REPLAYED_HEADER,
    IdempotencyWebFilter,
)
from pyfly.web.idempotency import disable_idempotency

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_handler_call_count = 0


def _reset_count() -> None:
    global _handler_call_count
    _handler_call_count = 0


async def _post_handler(request: Request) -> JSONResponse:
    global _handler_call_count
    _handler_call_count += 1
    return JSONResponse({"count": _handler_call_count}, status_code=200)


async def _get_handler(request: Request) -> JSONResponse:
    global _handler_call_count
    _handler_call_count += 1
    return JSONResponse({"count": _handler_call_count}, status_code=200)


@disable_idempotency
async def _disabled_handler(request: Request) -> JSONResponse:
    global _handler_call_count
    _handler_call_count += 1
    return JSONResponse({"count": _handler_call_count}, status_code=200)


def _make_app(cache: InMemoryCache, ttl_seconds: int = 86400) -> tuple[Starlette, list[Route]]:
    """Build a test Starlette app with the IdempotencyWebFilter installed."""
    idem_filter = IdempotencyWebFilter(cache=cache, ttl_seconds=ttl_seconds)
    routes = [
        Route("/resource", _post_handler, methods=["POST", "GET"]),
        Route("/no-idem", _disabled_handler, methods=["POST"]),
    ]
    app = Starlette(
        routes=routes,
        middleware=[Middleware(WebFilterChainMiddleware, filters=[idem_filter])],
    )
    return app, routes


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestIdempotencyWebFilter:
    """IdempotencyWebFilter — behaviour tests."""

    def setup_method(self) -> None:
        _reset_count()

    # ------------------------------------------------------------------
    # Filter metadata
    # ------------------------------------------------------------------

    def test_order_after_csrf(self) -> None:
        # Must run after CSRF (HIGHEST_PRECEDENCE + 210) so order > 210.
        assert IdempotencyWebFilter.__pyfly_order__ > HIGHEST_PRECEDENCE + 210

    def test_exclude_actuator_health_ready(self) -> None:
        # Actuator paths are excluded
        assert "/actuator/health" not in IdempotencyWebFilter.exclude_patterns
        # But the glob pattern is present
        assert "/actuator/*" in IdempotencyWebFilter.exclude_patterns
        assert "/health" in IdempotencyWebFilter.exclude_patterns
        assert "/ready" in IdempotencyWebFilter.exclude_patterns

    # ------------------------------------------------------------------
    # Cache-miss → handler called, response cached
    # ------------------------------------------------------------------

    def test_post_with_key_hits_handler_on_first_request(self) -> None:
        cache = InMemoryCache()
        app, _ = _make_app(cache)
        client = TestClient(app)

        resp = client.post("/resource", headers={IDEMPOTENCY_KEY_HEADER: "key-001"})

        assert resp.status_code == 200
        assert resp.json()["count"] == 1
        assert _handler_call_count == 1
        # No replay header on first call
        assert IDEMPOTENCY_REPLAYED_HEADER not in resp.headers

    # ------------------------------------------------------------------
    # Cache-hit → handler NOT called, replay header present
    # ------------------------------------------------------------------

    def test_second_post_with_same_key_returns_cached_response(self) -> None:
        cache = InMemoryCache()
        app, _ = _make_app(cache)
        client = TestClient(app)

        # First request
        resp1 = client.post("/resource", headers={IDEMPOTENCY_KEY_HEADER: "key-002"})
        assert resp1.status_code == 200
        assert _handler_call_count == 1

        # Second request with SAME key
        resp2 = client.post("/resource", headers={IDEMPOTENCY_KEY_HEADER: "key-002"})
        assert resp2.status_code == 200
        # Handler NOT called again
        assert _handler_call_count == 1, "Handler must not be called for a replayed idempotent request"
        # Response body is the same as first call
        assert resp2.json()["count"] == 1
        # Replay header is present
        assert resp2.headers.get(IDEMPOTENCY_REPLAYED_HEADER) == "true"

    def test_different_keys_produce_independent_cache_entries(self) -> None:
        cache = InMemoryCache()
        app, _ = _make_app(cache)
        client = TestClient(app)

        resp_a = client.post("/resource", headers={IDEMPOTENCY_KEY_HEADER: "key-A"})
        resp_b = client.post("/resource", headers={IDEMPOTENCY_KEY_HEADER: "key-B"})

        assert _handler_call_count == 2
        assert resp_a.json()["count"] == 1
        assert resp_b.json()["count"] == 2

    # ------------------------------------------------------------------
    # No Idempotency-Key → always hits handler
    # ------------------------------------------------------------------

    def test_post_without_key_always_hits_handler(self) -> None:
        cache = InMemoryCache()
        app, _ = _make_app(cache)
        client = TestClient(app)

        for i in range(3):
            resp = client.post("/resource")
            assert resp.status_code == 200
            assert resp.json()["count"] == i + 1

        assert _handler_call_count == 3

    # ------------------------------------------------------------------
    # @disable_idempotency route — never cached
    # ------------------------------------------------------------------

    def test_disable_idempotency_route_never_cached(self) -> None:
        cache = InMemoryCache()
        app, _ = _make_app(cache)
        client = TestClient(app)

        key = "key-disabled"
        # First call
        resp1 = client.post("/no-idem", headers={IDEMPOTENCY_KEY_HEADER: key})
        assert resp1.status_code == 200
        assert _handler_call_count == 1

        # Second call with same key — handler MUST be called again
        resp2 = client.post("/no-idem", headers={IDEMPOTENCY_KEY_HEADER: key})
        assert resp2.status_code == 200
        assert _handler_call_count == 2, "@disable_idempotency route must never cache"
        # No replay header
        assert IDEMPOTENCY_REPLAYED_HEADER not in resp2.headers
        # The cache must be completely empty — nothing was ever stored
        assert len(cache._store) == 0, "@disable_idempotency must never write to the cache"

    # ------------------------------------------------------------------
    # Safe methods (GET) — never cached
    # ------------------------------------------------------------------

    def test_get_request_never_cached(self) -> None:
        cache = InMemoryCache()
        app, _ = _make_app(cache)
        client = TestClient(app)

        key = "key-get"
        resp1 = client.get("/resource", headers={IDEMPOTENCY_KEY_HEADER: key})
        resp2 = client.get("/resource", headers={IDEMPOTENCY_KEY_HEADER: key})

        assert resp1.status_code == 200
        assert resp2.status_code == 200
        # Handler called both times — GET is idempotent by definition, not cached
        assert _handler_call_count == 2
        assert IDEMPOTENCY_REPLAYED_HEADER not in resp1.headers
        assert IDEMPOTENCY_REPLAYED_HEADER not in resp2.headers

    # ------------------------------------------------------------------
    # PUT and PATCH are also cached
    # ------------------------------------------------------------------

    @pytest.mark.parametrize("method", ["PUT", "PATCH", "DELETE"])
    def test_mutating_methods_are_cached(self, method: str) -> None:
        """PUT, PATCH, and DELETE are mutating and should also be cached."""

        async def _generic_handler(request: Request) -> JSONResponse:
            global _handler_call_count
            _handler_call_count += 1
            return JSONResponse({"count": _handler_call_count}, status_code=200)

        _reset_count()
        cache = InMemoryCache()
        idem_filter = IdempotencyWebFilter(cache=cache)
        routes = [Route("/item", _generic_handler, methods=[method])]
        app = Starlette(
            routes=routes,
            middleware=[Middleware(WebFilterChainMiddleware, filters=[idem_filter])],
        )
        client = TestClient(app)

        key = f"key-{method}"
        r1 = client.request(method, "/item", headers={IDEMPOTENCY_KEY_HEADER: key})
        r2 = client.request(method, "/item", headers={IDEMPOTENCY_KEY_HEADER: key})

        assert r1.status_code == 200
        assert r2.status_code == 200
        assert _handler_call_count == 1, f"{method} with same key should be replayed from cache"
        assert r2.headers.get(IDEMPOTENCY_REPLAYED_HEADER) == "true"

    # ------------------------------------------------------------------
    # 5xx responses — never cached; retries re-execute the handler
    # ------------------------------------------------------------------

    def test_5xx_response_not_cached_and_retry_hits_handler(self) -> None:
        """A 500 response must NOT be stored; a second identical-key call re-executes."""
        call_log: list[int] = []

        async def _flaky_handler(request: Request) -> JSONResponse:
            call_log.append(len(call_log) + 1)
            if len(call_log) == 1:
                return JSONResponse({"error": "transient"}, status_code=500)
            return JSONResponse({"ok": True}, status_code=200)

        cache = InMemoryCache()
        idem_filter = IdempotencyWebFilter(cache=cache)
        routes = [Route("/flaky", _flaky_handler, methods=["POST"])]
        app = Starlette(
            routes=routes,
            middleware=[Middleware(WebFilterChainMiddleware, filters=[idem_filter])],
        )
        client = TestClient(app, raise_server_exceptions=False)

        key = "key-flaky"
        r1 = client.post("/flaky", headers={IDEMPOTENCY_KEY_HEADER: key})
        assert r1.status_code == 500
        # The 500 must NOT have been cached — no replay header
        assert IDEMPOTENCY_REPLAYED_HEADER not in r1.headers

        # Second call with the SAME key — handler must be called again (not replayed)
        r2 = client.post("/flaky", headers={IDEMPOTENCY_KEY_HEADER: key})
        assert r2.status_code == 200
        assert len(call_log) == 2, "Handler must be called again after a 5xx — not replayed from cache"
        # The second (200) response is now cached for future replays
        r3 = client.post("/flaky", headers={IDEMPOTENCY_KEY_HEADER: key})
        assert r3.status_code == 200
        assert r3.headers.get(IDEMPOTENCY_REPLAYED_HEADER) == "true"
        assert len(call_log) == 2  # third call served from cache

    # ------------------------------------------------------------------
    # Exclude-patterns (actuator, health, ready) — filter skips these
    # ------------------------------------------------------------------

    def test_health_route_skips_filter(self) -> None:
        async def _health(request: Request) -> JSONResponse:
            global _handler_call_count
            _handler_call_count += 1
            return JSONResponse({"status": "up"})

        _reset_count()
        cache = InMemoryCache()
        idem_filter = IdempotencyWebFilter(cache=cache)
        routes = [Route("/health", _health, methods=["POST"])]
        app = Starlette(
            routes=routes,
            middleware=[Middleware(WebFilterChainMiddleware, filters=[idem_filter])],
        )
        client = TestClient(app)

        key = "key-health"
        client.post("/health", headers={IDEMPOTENCY_KEY_HEADER: key})
        client.post("/health", headers={IDEMPOTENCY_KEY_HEADER: key})

        # Both hits went to the handler — /health is excluded from the filter
        assert _handler_call_count == 2


# ---------------------------------------------------------------------------
# @disable_idempotency decorator unit test
# ---------------------------------------------------------------------------


class TestDisableIdempotencyDecorator:
    def test_sets_sentinel_attribute(self) -> None:
        from pyfly.web.idempotency import DISABLE_IDEMPOTENCY_ATTR

        @disable_idempotency
        async def my_handler(request: Request) -> JSONResponse:
            return JSONResponse({})

        assert getattr(my_handler, DISABLE_IDEMPOTENCY_ATTR, False) is True

    def test_handler_without_decorator_has_no_attribute(self) -> None:
        from pyfly.web.idempotency import DISABLE_IDEMPOTENCY_ATTR

        async def my_handler(request: Request) -> JSONResponse:
            return JSONResponse({})

        assert not getattr(my_handler, DISABLE_IDEMPOTENCY_ATTR, False)
