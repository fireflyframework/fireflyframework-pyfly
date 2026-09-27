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

An entry is keyed by the caller's scope too (:func:`scoped_key`): a result cached for one tenant or user
is never served to another. Evicting a key evicts it for every scope.
"""

from __future__ import annotations

import hashlib
import logging
from datetime import timedelta
from typing import TYPE_CHECKING, Any

from pyfly.cache.namespaces import PrefixedCache
from pyfly.cache.transaction import TransactionAwareCache
from pyfly.cqrs.types import QueryCacheScope

if TYPE_CHECKING:
    from pyfly.cqrs.context.execution_context import ExecutionContext
    from pyfly.cqrs.query.handler import QueryHandler

_logger = logging.getLogger(__name__)

CQRS_CACHE_PREFIX = ":cqrs:"

SCOPE_SEPARATOR = "|scope="
"""What separates a query's cache key from the digest of the caller's scope."""


def _ambient_tenant() -> str | None:
    from pyfly.observability.correlation import get_tenant_id

    return get_tenant_id()


def _ambient_user() -> str | None:
    from pyfly.context.request_context import RequestContext

    request = RequestContext.current()
    security = request.security_context if request is not None else None
    return security.user_id if security is not None else None


def scope_of(scope: QueryCacheScope, context: ExecutionContext | None) -> tuple[tuple[str, str | None], ...]:
    """The identity an entry is keyed by: the tenant and organization (and the user, for ``USER``) of
    *context*, completed with the ambient tenant (``X-Tenant-Id``) and authenticated user of the running
    request; nothing for ``GLOBAL``."""
    if scope is QueryCacheScope.GLOBAL:
        return ()
    tenant = (context.tenant_id if context is not None else None) or _ambient_tenant()
    organization = context.organization_id if context is not None else None
    parts: list[tuple[str, str | None]] = [("tenant", tenant), ("organization", organization)]
    if scope is QueryCacheScope.USER:
        parts.append(("user", (context.user_id if context is not None else None) or _ambient_user()))
    return tuple(parts)


def query_cache_key(handler: QueryHandler[Any, Any], cache_key: str) -> str:
    """*cache_key* (a query's ``get_cache_key()``) as the bus stores it for *handler*: after the handler's
    ``cache_key_prefix`` when it declares one."""
    prefix = handler.get_cache_key_prefix()
    return f"{prefix}:{cache_key}" if prefix else cache_key


def query_cache_group(handler: QueryHandler[Any, Any]) -> str:
    """The key prefix every cached result of *handler* starts with: its ``cache_key_prefix``, or the query
    class name the default query keys start with."""
    prefix = handler.get_cache_key_prefix()
    if prefix:
        return f"{prefix}:"
    query_type = handler.get_query_type()
    return f"{query_type.__name__ if query_type is not None else type(handler).__name__}:"


def scoped_key(cache_key: str, scope: QueryCacheScope, context: ExecutionContext | None) -> str:
    """*cache_key* for the caller: followed by a digest of its scope (:func:`scope_of`), or unchanged for
    a ``GLOBAL`` handler and for a caller with no tenant, organization or user at all."""
    parts = scope_of(scope, context)
    if all(value is None for _name, value in parts):
        return cache_key
    digest = hashlib.sha256(repr(parts).encode("utf-8")).hexdigest()[:16]
    return f"{cache_key}{SCOPE_SEPARATOR}{digest}"


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

    async def lookup(self, cache_key: str) -> tuple[bool, Any]:
        """``(True, value)`` for a hit, a stored ``None`` included; ``(False, None)`` for a miss."""
        if self._region is None:
            return False, None
        try:
            value = await self._region.get(cache_key)
            if value is not None:
                return True, value
            return await self._region.exists(cache_key), None
        except Exception as exc:
            _logger.warning("CQRS cache get failed for key '%s%s': %s", CQRS_CACHE_PREFIX, cache_key, exc)
            return False, None

    # ── write ──────────────────────────────────────────────────

    async def put(self, cache_key: str, value: Any, ttl: timedelta | None = None) -> None:
        """Store *value* (after the commit inside a unit of work); a failure is logged, never raised."""
        if self._region is None:
            return
        await self._region.put(cache_key, value, ttl=ttl)

    # ── evict ──────────────────────────────────────────────────

    async def evict(self, cache_key: str) -> bool:
        """Evict *cache_key* for every caller's scope (after the commit inside a unit of work, where it
        returns ``False``)."""
        if self._region is None:
            return False
        evicted = await self._region.evict(cache_key)
        scoped = await self._region.evict_by_prefix(f"{cache_key}{SCOPE_SEPARATOR}")
        return evicted or scoped > 0

    async def evict_prefix(self, prefix: str) -> int:
        """Evict every entry whose key starts with *prefix* (a query handler's group), for every scope."""
        if self._region is None:
            return 0
        return await self._region.evict_by_prefix(prefix)

    # ── clear ──────────────────────────────────────────────────

    async def clear(self) -> None:
        """Evict every query-cache entry (the ``:cqrs:`` prefix) and nothing else in the cache."""
        if self._region is None:
            return
        await self._region.clear()

    @property
    def is_available(self) -> bool:
        return self._cache is not None
