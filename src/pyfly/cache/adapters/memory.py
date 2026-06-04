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
from datetime import timedelta
from typing import Any


class InMemoryCache:
    """In-memory cache with optional TTL support.

    Suitable for development, testing, and single-process applications.
    Also serves as the default fallback in CacheManager.
    """

    def __init__(self) -> None:
        self._store: dict[str, tuple[Any, float | None]] = {}
        self._hits = 0
        self._misses = 0
        self._evictions = 0

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

        self._hits += 1
        return value

    async def put(self, key: str, value: Any, ttl: timedelta | None = None) -> None:
        """Store a value with optional TTL."""
        expires_at = None
        if ttl is not None:
            expires_at = time.monotonic() + ttl.total_seconds()
        self._store[key] = (value, expires_at)

    async def put_if_absent(self, key: str, value: Any, ttl: timedelta | None = None) -> bool:
        """Store *value* only if *key* is absent — atomic under asyncio (audit #75)."""
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
            "max_size": None,
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
        """Remove all entries."""
        self._store.clear()

    async def start(self) -> None:
        """No-op for in-memory cache."""

    async def stop(self) -> None:
        """No-op for in-memory cache."""
