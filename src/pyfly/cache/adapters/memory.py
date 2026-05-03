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

    async def get(self, key: str) -> Any | None:
        """Get a value by key. Returns None if missing or expired."""
        entry = self._store.get(key)
        if entry is None:
            return None

        value, expires_at = entry
        if expires_at is not None and time.monotonic() > expires_at:
            del self._store[key]
            return None

        return value

    async def put(self, key: str, value: Any, ttl: timedelta | None = None) -> None:
        """Store a value with optional TTL."""
        expires_at = None
        if ttl is not None:
            expires_at = time.monotonic() + ttl.total_seconds()
        self._store[key] = (value, expires_at)

    async def evict(self, key: str) -> bool:
        """Remove a key. Returns True if the key existed."""
        if key in self._store:
            del self._store[key]
            return True
        return False

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
        """Return cache statistics, excluding expired entries."""
        now = time.monotonic()
        active = sum(1 for _, (_, exp) in self._store.items() if exp is None or exp > now)
        return {"size": active, "type": "memory", "max_size": None}

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
