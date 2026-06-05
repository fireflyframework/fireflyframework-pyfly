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
"""Redis-backed cache adapter."""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any, cast

from pyfly.cache.serialization import cache_dumps, cache_loads

_logger = logging.getLogger(__name__)


class RedisCacheAdapter:
    """Cache adapter that delegates to a ``redis.asyncio.Redis``-like client.

    Values are JSON-serialized before storage so that any JSON-compatible
    Python object can be cached transparently.
    """

    def __init__(self, client: Any) -> None:
        self._client = client
        self._hits = 0
        self._misses = 0
        self._evictions = 0
        self._available = True

    async def get(self, key: str) -> Any | None:
        """Retrieve and deserialize a cached value."""
        raw = await self._client.get(key)
        if raw is None:
            self._misses += 1
            return None
        try:
            value = cache_loads(raw)
            self._hits += 1
            return value
        except (ValueError, TypeError):
            self._misses += 1
            _logger.warning("Failed to deserialize cached value for key '%s'", key)
            return None

    async def put(self, key: str, value: Any, ttl: timedelta | None = None) -> None:
        """Serialize and store a value with optional TTL."""
        raw = cache_dumps(value)
        ex = int(ttl.total_seconds()) if ttl is not None else None
        await self._client.set(key, raw, ex=ex)

    async def put_if_absent(self, key: str, value: Any, ttl: timedelta | None = None) -> bool:
        """Atomically store *value* only if *key* is absent (audit #75)."""
        ex = int(ttl.total_seconds()) if ttl is not None else None
        stored = await self._client.set(key, cache_dumps(value), ex=ex, nx=True)
        return bool(stored)

    async def evict(self, key: str) -> bool:
        """Remove a key. Returns True if the key existed."""
        count = await self._client.delete(key)
        if count:
            self._evictions += 1
        return cast(bool, count > 0)

    async def evict_by_prefix(self, prefix: str) -> int:
        """Evict every key starting with *prefix* (audit #78)."""
        removed = 0
        async for key in self._client.scan_iter(match=f"{prefix}*"):
            if await self._client.delete(key):
                removed += 1
        self._evictions += removed
        return removed

    async def exists(self, key: str) -> bool:
        """Check whether a key exists."""
        count = await self._client.exists(key)
        return cast(bool, count > 0)

    async def get_stats(self) -> dict[str, Any]:
        """Return cache statistics including hit-rate (audit #76)."""
        dbsize = await self._client.dbsize()
        requests = self._hits + self._misses
        return {
            "size": dbsize,
            "type": "redis",
            "requests": requests,
            "hits": self._hits,
            "misses": self._misses,
            "evictions": self._evictions,
            "hit_rate": (self._hits / requests) if requests else 0.0,
        }

    async def get_keys(self, pattern: str = "*", limit: int = 100) -> list[str]:
        """Return up to *limit* keys matching *pattern* via SCAN."""
        keys: list[str] = []
        async for key in self._client.scan_iter(match=pattern, count=limit):
            keys.append(key.decode() if isinstance(key, bytes) else key)
            if len(keys) >= limit:
                break
        return keys

    async def clear(self) -> None:
        """Flush the entire database."""
        await self._client.flushdb()

    async def start(self) -> None:
        """Ping Redis, but degrade gracefully if it is unreachable (audit #79).

        A cold/absent Redis must not abort the whole application startup; the
        adapter marks itself unavailable and operations fail soft instead.
        """
        try:
            await self._client.ping()
            self._available = True
        except Exception as exc:  # noqa: BLE001
            self._available = False
            _logger.warning("Redis cache unavailable at startup; degrading: %s", exc)

    async def is_available(self) -> bool:
        try:
            await self._client.ping()
            self._available = True
        except Exception:  # noqa: BLE001
            self._available = False
        return self._available

    async def stop(self) -> None:
        """Close the underlying Redis connection."""
        await self._client.aclose()
