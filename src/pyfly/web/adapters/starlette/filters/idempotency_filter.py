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
"""IdempotencyWebFilter — HTTP idempotency via ``Idempotency-Key`` header.

For mutating methods (POST, PUT, PATCH, DELETE) that carry an
``Idempotency-Key`` request header the filter:

1. Computes a cache key ``idem:{method}:{path}:{idempotency_key}``.
2. If a stored response payload is found in the cache the original response
   is reconstructed and returned immediately (short-circuit) with an
   ``Idempotency-Replayed: true`` header.
3. Otherwise the request is forwarded to the handler, the response is cached
   (when it is a finite, buffered response), and the response is returned to
   the caller.

Streaming / unbounded responses (detected by an absent or empty ``.body``
attribute) are passed through without caching so SSE / chunked downloads are
never broken.

Routes decorated with ``@disable_idempotency`` are excluded: the filter
checks ``request.scope.get("endpoint")`` for the
``__pyfly_disable_idempotency__`` sentinel attribute.
"""

from __future__ import annotations

import json
import logging
from datetime import timedelta
from typing import Any

from starlette.responses import Response

from pyfly.cache.ports.outbound import CacheAdapter
from pyfly.container.ordering import HIGHEST_PRECEDENCE
from pyfly.web.filters import OncePerRequestFilter
from pyfly.web.idempotency import DISABLE_IDEMPOTENCY_ATTR
from pyfly.web.ports.filter import CallNext

_logger = logging.getLogger(__name__)

#: HTTP methods that mutate server state — the only ones subject to idempotency
#: caching.  Safe methods (GET, HEAD, OPTIONS, TRACE) are always passed through.
_MUTATING_METHODS: frozenset[str] = frozenset({"POST", "PUT", "PATCH", "DELETE"})

#: Response header written on replayed (cache-hit) responses.
IDEMPOTENCY_REPLAYED_HEADER: str = "Idempotency-Replayed"

#: Header sent by clients to enable idempotent request processing.
IDEMPOTENCY_KEY_HEADER: str = "Idempotency-Key"

# Subset of response headers preserved across cache round-trips.
# We intentionally exclude transport-level headers (Transfer-Encoding,
# Content-Length) that are not meaningful once the body is buffered.
_CACHEABLE_HEADERS = frozenset(
    {
        "content-type",
        "cache-control",
        "etag",
        "last-modified",
        "location",
        "x-request-id",
        "x-correlation-id",
        "x-transaction-id",
    }
)

# Payload stored in the cache for each idempotent response.
# Keys: "status" (int), "headers" (dict[str, str]), "body" (str — base64 or text).
_CachePayload = dict[str, Any]


def _build_cache_key(method: str, path: str, idempotency_key: str) -> str:
    return f"idem:{method}:{path}:{idempotency_key}"


def _extract_body(response: Response) -> bytes | None:
    """Return the buffered body bytes from a Starlette ``Response``.

    For ``Response`` / ``JSONResponse`` the body is available on the ``.body``
    attribute once the response object has been constructed.  When the attribute
    is absent or empty (streaming / pathsend responses) we return ``None`` to
    signal that caching should be skipped.
    """
    body: bytes | None = getattr(response, "body", None)
    if not body:
        # Empty bytes b"" is falsy — treat as non-cacheable rather than caching
        # an empty payload that might mask a streaming response.
        return None
    return body


def _serialize(response: Response, body: bytes) -> _CachePayload:
    """Build the JSON-safe payload stored in the cache."""
    raw_headers: list[tuple[bytes, bytes]] = getattr(response, "raw_headers", [])
    headers: dict[str, str] = {}
    for k, v in raw_headers:
        key = k.decode("latin-1").lower()
        if key in _CACHEABLE_HEADERS:
            headers[key] = v.decode("latin-1")

    # body is raw bytes — store as a latin-1 string so it survives a JSON
    # round-trip through any serializable cache backend (Redis, memory, etc.)
    # without corrupting binary content-types.
    return {
        "status": response.status_code,
        "headers": headers,
        "body": body.decode("latin-1"),
    }


def _reconstruct(payload: _CachePayload) -> Response:
    """Reconstruct a :class:`~starlette.responses.Response` from a cached payload."""
    body: bytes = payload["body"].encode("latin-1")
    status: int = int(payload["status"])
    headers: dict[str, str] = dict(payload.get("headers", {}))
    response = Response(content=body, status_code=status)
    for k, v in headers.items():
        response.headers[k] = v
    response.headers[IDEMPOTENCY_REPLAYED_HEADER] = "true"
    return response


def _handler_disabled(request: Any) -> bool:
    """Return True when the matched route handler carries ``@disable_idempotency``."""
    scope: dict[str, Any] = getattr(request, "scope", {})
    endpoint = scope.get("endpoint")
    if endpoint is None:
        return False
    return bool(getattr(endpoint, DISABLE_IDEMPOTENCY_ATTR, False))


class IdempotencyWebFilter(OncePerRequestFilter):
    """Cache-backed idempotency filter for mutating HTTP requests.

    Ordering: runs after CSRF/security (``HIGHEST_PRECEDENCE + 230``) but
    before any application-layer handler so the idempotency decision is made
    early in the filter chain and cannot be bypassed by other filters.

    Args:
        cache: A :class:`~pyfly.cache.ports.outbound.CacheAdapter` used to
            store and retrieve cached response payloads.
        ttl_seconds: Time-to-live for cached responses in seconds.
            Defaults to 86 400 (24 hours).
    """

    __pyfly_order__ = HIGHEST_PRECEDENCE + 230

    exclude_patterns = ["/actuator/*", "/health", "/ready"]

    def __init__(self, cache: CacheAdapter, ttl_seconds: int = 86400) -> None:
        self._cache = cache
        self._ttl = timedelta(seconds=ttl_seconds)

    async def do_filter(self, request: Any, call_next: CallNext) -> Any:
        method: str = request.method

        # -- Pass through safe methods immediately --------------------------------
        if method not in _MUTATING_METHODS:
            return await call_next(request)

        idempotency_key: str | None = request.headers.get(IDEMPOTENCY_KEY_HEADER)

        # -- No Idempotency-Key → pass through ------------------------------------
        if not idempotency_key:
            return await call_next(request)

        path: str = request.url.path
        cache_key: str = _build_cache_key(method, path, idempotency_key)

        # -- Cache hit → replay ---------------------------------------------------
        # Note: a response is only stored in the cache when the matched endpoint
        # does NOT carry @disable_idempotency (checked below after call_next).
        # Therefore a cache-hit here implies the endpoint allows caching.
        cached: _CachePayload | None = await self._cache.get(cache_key)
        if cached is not None:
            _logger.debug("idempotency replay: %s %s key=%s", method, path, idempotency_key)
            return _reconstruct(cached)

        # -- Cache miss → call the handler ----------------------------------------
        response: Response = await call_next(request)

        # After routing, request.scope["endpoint"] is populated by Starlette's
        # router.  Check the @disable_idempotency marker NOW so we never persist
        # a response for a route that opts out.
        if _handler_disabled(request):
            _logger.debug(
                "idempotency: skipping cache for %s %s — @disable_idempotency",
                method,
                path,
            )
            return response

        body = _extract_body(response)
        if body is not None:
            payload = _serialize(response, body)
            try:
                # Validate the payload is JSON-serializable before storing it.
                json.dumps(payload)
                await self._cache.put(cache_key, payload, ttl=self._ttl)
            except (TypeError, ValueError) as exc:
                _logger.warning(
                    "idempotency: skipping cache for %s %s — payload not serializable: %s",
                    method,
                    path,
                    exc,
                )
        else:
            _logger.debug(
                "idempotency: skipping cache for %s %s — streaming/empty response",
                method,
                path,
            )

        return response
