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
"""Cache manager with automatic failover."""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

from pyfly.cache.ports.outbound import CacheAdapter

logger = logging.getLogger("pyfly.cache")


class CacheManager:
    """Manages primary and fallback cache adapters with automatic failover.

    On primary cache failures, operations gracefully degrade to the
    fallback cache. Write operations are mirrored to both caches
    to keep the fallback warm.
    """

    def __init__(self, primary: CacheAdapter, fallback: CacheAdapter) -> None:
        self._primary = primary
        self._fallback = fallback

    async def get(self, key: str) -> Any | None:
        """Get from primary; fall back on failure."""
        try:
            result = await self._primary.get(key)
            if result is not None:
                return result
        except Exception:
            logger.warning("Primary cache failed for GET '%s', falling back", key)

        return await self._fallback.get(key)

    async def put(self, key: str, value: Any, ttl: timedelta | None = None) -> None:
        """Write to both primary and fallback."""
        try:
            await self._primary.put(key, value, ttl=ttl)
        except Exception:
            logger.warning("Primary cache failed for PUT '%s', using fallback only", key)

        await self._fallback.put(key, value, ttl=ttl)

    async def evict(self, key: str) -> bool:
        """Evict from both caches."""
        primary_result = False
        try:
            primary_result = await self._primary.evict(key)
        except Exception:
            logger.warning("Primary cache failed for EVICT '%s'", key)

        fallback_result = await self._fallback.evict(key)
        return primary_result or fallback_result

    async def clear(self) -> None:
        """Clear both caches."""
        try:
            await self._primary.clear()
        except Exception:
            logger.warning("Primary cache failed for CLEAR")

        await self._fallback.clear()

    async def put_if_absent(self, key: str, value: Any, ttl: timedelta | None = None) -> bool:
        """Store only if absent; mirror to both caches."""
        result = False
        try:
            result = await self._primary.put_if_absent(key, value, ttl=ttl)
        except Exception:
            logger.warning("Primary cache failed for PUT_IF_ABSENT '%s', using fallback only", key)

        fallback_result = await self._fallback.put_if_absent(key, value, ttl=ttl)
        return result or fallback_result

    async def evict_by_prefix(self, prefix: str) -> int:
        """Evict matching keys from both caches; return the total removed."""
        primary_count = 0
        try:
            primary_count = await self._primary.evict_by_prefix(prefix)
        except Exception:
            logger.warning("Primary cache failed for EVICT_BY_PREFIX '%s'", prefix)

        fallback_count = await self._fallback.evict_by_prefix(prefix)
        return primary_count + fallback_count

    async def exists(self, key: str) -> bool:
        """True if either cache holds the key."""
        try:
            if await self._primary.exists(key):
                return True
        except Exception:
            logger.warning("Primary cache failed for EXISTS '%s', falling back", key)

        return await self._fallback.exists(key)

    async def start(self) -> None:
        """Start both cache adapters."""
        for adapter in (self._primary, self._fallback):
            try:
                await adapter.start()
            except Exception:
                logger.warning("A cache adapter failed to start")

    async def stop(self) -> None:
        """Stop both cache adapters."""
        for adapter in (self._primary, self._fallback):
            try:
                await adapter.stop()
            except Exception:
                logger.warning("A cache adapter failed to stop")
