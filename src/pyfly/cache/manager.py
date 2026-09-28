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

from pyfly.cache.namespaces import dedicated_cache
from pyfly.cache.ports.outbound import CacheAdapter
from pyfly.cache.serialization import CacheValueError

logger = logging.getLogger("pyfly.cache")

DEFAULT_FALLBACK_TTL = timedelta(seconds=60)


class CacheManager:
    """A shared primary cache (Redis, PostgreSQL) with a per-process fallback for its outages.

    The primary is the only source of truth while it answers. A miss on the primary is a miss: the
    fallback is not consulted, and a successful write is not mirrored into it. Every node therefore sees
    the evictions every other node makes on the primary, instead of serving its own older copy.

    The fallback is used only while the primary *fails* (raises): reads and writes go to it, and what is
    written there expires within *fallback_ttl*, because no other node can evict it. The first operation
    the primary answers again ends the outage and clears the fallback, so nothing written during the
    outage is served during the next one. Evictions always reach both.

    A value the cache refuses (:class:`~pyfly.cache.serialization.CacheValueError`, a live ORM object) is
    not an outage: it propagates and the fallback is left alone.

    Args:
        primary: The shared cache.
        fallback: The per-process cache used while the primary fails (an ``InMemoryCache``).
        fallback_ttl: The longest a value written during an outage lives in the fallback; ``None`` keeps
            the TTL each write asks for.
    """

    def __init__(
        self,
        primary: CacheAdapter,
        fallback: CacheAdapter,
        *,
        fallback_ttl: timedelta | None = DEFAULT_FALLBACK_TTL,
    ) -> None:
        self._primary = primary
        self._fallback = fallback
        self._fallback_ttl = fallback_ttl
        self._primary_down = False

    @property
    def primary_available(self) -> bool:
        """Whether the last operation on the primary succeeded."""
        return not self._primary_down

    # -- outage bookkeeping ---------------------------------------------------------------------------------

    def _outage(self, operation: str, key: str) -> None:
        if not self._primary_down:
            logger.warning("Primary cache failed for %s '%s', using the fallback until it recovers", operation, key)
        self._primary_down = True

    async def _answered(self) -> None:
        if not self._primary_down:
            return
        self._primary_down = False
        logger.info("Primary cache recovered; clearing the fallback written during the outage")
        try:
            await self._fallback.clear()
        except Exception:  # noqa: BLE001 — the fallback is best effort
            logger.warning("Fallback cache failed to clear after the primary recovered")

    def _outage_ttl(self, ttl: timedelta | None) -> timedelta | None:
        if self._fallback_ttl is None:
            return ttl
        if ttl is None:
            return self._fallback_ttl
        return min(ttl, self._fallback_ttl)

    # -- operations -----------------------------------------------------------------------------------------

    async def get(self, key: str) -> Any | None:
        """Get from the primary; from the fallback only while the primary fails."""
        try:
            result = await self._primary.get(key)
        except CacheValueError:
            raise
        except Exception:
            self._outage("GET", key)
            return await self._fallback.get(key)
        await self._answered()
        return result

    async def put(self, key: str, value: Any, ttl: timedelta | None = None) -> None:
        """Write to the primary; to the fallback (for at most *fallback_ttl*) only while it fails."""
        try:
            await self._primary.put(key, value, ttl=ttl)
        except CacheValueError:
            raise
        except Exception:
            self._outage("PUT", key)
            await self._fallback.put(key, value, ttl=self._outage_ttl(ttl))
            return
        await self._answered()

    async def evict(self, key: str) -> bool:
        """Evict from both caches."""
        primary_result = False
        try:
            primary_result = await self._primary.evict(key)
        except Exception:
            self._outage("EVICT", key)
        else:
            await self._answered()
        fallback_result = await self._fallback.evict(key)
        return primary_result or fallback_result

    async def clear(self) -> None:
        """Clear both caches (each its own entries only)."""
        try:
            await self._primary.clear()
        except Exception:
            self._outage("CLEAR", "*")
        else:
            await self._answered()
        await self._fallback.clear()

    async def put_if_absent(self, key: str, value: Any, ttl: timedelta | None = None) -> bool:
        """Store only if absent on the primary; on the fallback only while the primary fails."""
        try:
            stored = await self._primary.put_if_absent(key, value, ttl=ttl)
        except CacheValueError:
            raise
        except Exception:
            self._outage("PUT_IF_ABSENT", key)
            return await self._fallback.put_if_absent(key, value, ttl=self._outage_ttl(ttl))
        await self._answered()
        return stored

    async def evict_by_prefix(self, prefix: str) -> int:
        """Evict matching keys from both caches; return the total removed."""
        primary_count = 0
        try:
            primary_count = await self._primary.evict_by_prefix(prefix)
        except Exception:
            self._outage("EVICT_BY_PREFIX", prefix)
        else:
            await self._answered()
        fallback_count = await self._fallback.evict_by_prefix(prefix)
        return primary_count + fallback_count

    async def exists(self, key: str) -> bool:
        """Whether the primary holds the key; the fallback only while the primary fails."""
        try:
            found = await self._primary.exists(key)
        except Exception:
            self._outage("EXISTS", key)
            return await self._fallback.exists(key)
        await self._answered()
        return found

    def with_namespace(self, name: str) -> CacheManager:
        """A manager over the caches dedicated to *name* on the primary and on the fallback."""
        return CacheManager(
            dedicated_cache(self._primary, name),
            dedicated_cache(self._fallback, name),
            fallback_ttl=self._fallback_ttl,
        )

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
