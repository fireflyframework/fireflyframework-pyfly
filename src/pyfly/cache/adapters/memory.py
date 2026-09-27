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
"""Built-in cache adapter implementations."""

from __future__ import annotations

import time
from collections import OrderedDict
from datetime import timedelta
from typing import Any

from pyfly.cache.serialization import Copy, encode_copy


class InMemoryCache:
    """In-memory cache with optional TTL and LRU bounding.

    Suitable for development, testing, and single-process applications.
    Also serves as the default fallback in CacheManager.

    Entries are copies (the same value semantics as the Redis and PostgreSQL adapters): ``put`` stores a
    detached copy of the value and every ``get`` returns a new copy, so callers never share one object,
    and a change to a value after it was put, or to a hit, never reaches the cache. A live ORM object (a
    SQLAlchemy-mapped instance or a Beanie document, also inside a container or a DTO) is refused with
    :class:`~pyfly.cache.serialization.CacheValueError`: cache a DTO built from it.

    Args:
        max_size: When set, the cache holds at most this many entries and evicts
            the least-recently-used entry on overflow. ``None`` (default) leaves
            the cache unbounded — rely on TTLs to bound memory.
    """

    def __init__(self, max_size: int | None = None) -> None:
        self._store: OrderedDict[str, tuple[Copy, float | None]] = OrderedDict()
        self._max_size = max_size
        self._hits = 0
        self._misses = 0
        self._evictions = 0
        self._dedicated: dict[str, InMemoryCache] = {}

    async def get(self, key: str) -> Any | None:
        """Get a value by key. Returns None if missing or expired."""
        entry = self._store.get(key)
        if entry is None:
            self._misses += 1
            return None

        value, expires_at = entry
        if expires_at is not None and time.monotonic() > expires_at:
            del self._store[key]
            self._misses += 1
            return None

        self._store.move_to_end(key)  # mark most-recently-used (LRU)
        self._hits += 1
        return value.value()

    async def put(self, key: str, value: Any, ttl: timedelta | None = None) -> None:
        """Store a copy of *value* with optional TTL, evicting the LRU entry when full.

        Raises :class:`~pyfly.cache.serialization.CacheValueError` for a live ORM object.
        """
        stored = encode_copy(value)
        expires_at = None
        if ttl is not None:
            expires_at = time.monotonic() + ttl.total_seconds()
        self._store[key] = (stored, expires_at)
        self._store.move_to_end(key)
        if self._max_size is not None:
            while len(self._store) > self._max_size:
                self._store.popitem(last=False)  # evict least-recently-used
                self._evictions += 1

    async def put_if_absent(self, key: str, value: Any, ttl: timedelta | None = None) -> bool:
        """Store a copy of *value* only if *key* is absent — atomic under asyncio (audit #75)."""
        if await self.exists(key):
            return False
        await self.put(key, value, ttl)
        return True

    async def evict(self, key: str) -> bool:
        """Remove a key. Returns True if the key existed."""
        if key in self._store:
            del self._store[key]
            self._evictions += 1
            return True
        return False

    async def evict_by_prefix(self, prefix: str) -> int:
        """Evict every key starting with *prefix* (audit #78)."""
        matches = [k for k in self._store if k.startswith(prefix)]
        for k in matches:
            del self._store[k]
        self._evictions += len(matches)
        return len(matches)

    async def exists(self, key: str) -> bool:
        """Check if a key exists and is not expired."""
        entry = self._store.get(key)
        if entry is None:
            return False
        _, expires_at = entry
        if expires_at is not None and time.monotonic() > expires_at:
            del self._store[key]
            return False
        return True

    def get_stats(self) -> dict[str, Any]:
        """Return cache statistics including hit-rate (audit #76)."""
        now = time.monotonic()
        active = sum(1 for _, (_, exp) in self._store.items() if exp is None or exp > now)
        requests = self._hits + self._misses
        return {
            "size": active,
            "type": "memory",
            "max_size": self._max_size,
            "requests": requests,
            "hits": self._hits,
            "misses": self._misses,
            "evictions": self._evictions,
            "hit_rate": (self._hits / requests) if requests else 0.0,
        }

    def get_keys(self) -> list[str]:
        """Return keys of non-expired entries."""
        now = time.monotonic()
        return [k for k, (_, exp) in self._store.items() if exp is None or exp > now]

    async def clear(self) -> None:
        """Remove every entry of this cache (a :meth:`with_namespace` cache keeps its own)."""
        self._store.clear()

    def with_namespace(self, name: str) -> InMemoryCache:
        """A cache of its own for *name*, disjoint from this one: :meth:`clear` never touches it.

        Durable consumers (idempotency records, orchestration state) keep their entries there, so clearing
        the evictable cache cannot drop them. The same name always gives the same cache.
        """
        dedicated = self._dedicated.get(name)
        if dedicated is None:
            dedicated = InMemoryCache(max_size=self._max_size)
            self._dedicated[name] = dedicated
        return dedicated

    async def start(self) -> None:
        """No-op for in-memory cache."""

    async def stop(self) -> None:
        """No-op for in-memory cache."""
