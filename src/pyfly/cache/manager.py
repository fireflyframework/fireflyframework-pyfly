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
