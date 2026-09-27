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
"""Query cache adapter — bridges pyfly.cache with CQRS.

Mirrors Java's ``QueryCacheAdapter`` with the `:cqrs:` key prefix.

The query cache is the ``:cqrs:`` region of the application's cache
(:class:`~pyfly.cache.namespaces.PrefixedCache`): :meth:`QueryCacheAdapter.clear` evicts that prefix and
nothing else, so resetting the query cache never drops the orchestration state, idempotency records or
``@cacheable`` entries that share the cache bean. It is transaction-aware
(:class:`~pyfly.cache.transaction.TransactionAwareCache`): inside a unit of work, puts and evictions wait
for the commit and are dropped on rollback. A cache failure is logged and never fails the query or the
command that caused it.
"""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

from pyfly.cache.namespaces import PrefixedCache
from pyfly.cache.transaction import TransactionAwareCache

_logger = logging.getLogger(__name__)

CQRS_CACHE_PREFIX = ":cqrs:"


class QueryCacheAdapter:
    """Thin wrapper around pyfly's :class:`CacheAdapter` with CQRS-specific key prefixing.

    If no underlying cache is provided, all operations are silent no-ops.
    """

    def __init__(self, cache: Any = None) -> None:
        self._cache = cache
        self._region: TransactionAwareCache | None = None
        if cache is not None:
            self._region = TransactionAwareCache(PrefixedCache(cache, CQRS_CACHE_PREFIX), on_write_error="log")

    # ── read ───────────────────────────────────────────────────

    async def get(self, cache_key: str) -> Any | None:
        if self._region is None:
            return None
        try:
            return await self._region.get(cache_key)
        except Exception as exc:
            _logger.warning("CQRS cache get failed for key '%s%s': %s", CQRS_CACHE_PREFIX, cache_key, exc)
            return None

    # ── write ──────────────────────────────────────────────────

    async def put(self, cache_key: str, value: Any, ttl: timedelta | None = None) -> None:
        """Store *value* (after the commit inside a unit of work); a failure is logged, never raised."""
        if self._region is None:
            return
        await self._region.put(cache_key, value, ttl=ttl)

    # ── evict ──────────────────────────────────────────────────

    async def evict(self, cache_key: str) -> bool:
        """Evict *cache_key* (after the commit inside a unit of work, where it returns ``False``)."""
        if self._region is None:
            return False
        return await self._region.evict(cache_key)

    # ── clear ──────────────────────────────────────────────────

    async def clear(self) -> None:
        """Evict every query-cache entry (the ``:cqrs:`` prefix) and nothing else in the cache."""
        if self._region is None:
            return
        await self._region.clear()

    @property
    def is_available(self) -> bool:
        return self._cache is not None
