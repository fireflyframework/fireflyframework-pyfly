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

An entry is keyed by the caller's scope too (:func:`scope_digest`): a result cached for one tenant or
user is never served to another. Evicting a key evicts it for every scope at the cost of one write: the
scoped entries of a key live under its current *generation* (``<key>|<generation>|scope=<digest>``), and
eviction replaces the generation, which leaves the old entries unreachable until their TTL expires. No
eviction scans the keyspace.

A generation is an entry too (``<key>|generation``), and it expires: it is created with the TTL of the
entries looked up under it, and an eviction's new generation lives as long as the longest entry TTL this
adapter has seen (``generation_ttl`` before it has seen one). It is never refreshed: rewriting it could
race an eviction's new generation and bring the evicted entries back. An entry stored late in its
generation's life can therefore become unreachable before its own TTL, which costs one extra miss and
never serves a stale value: generations are random and never reused, so an expired one is replaced by a
fresh one that reaches no older entry.
"""

from __future__ import annotations

import hashlib
import logging
import uuid
from datetime import timedelta
from typing import TYPE_CHECKING, Any

from pyfly.cache.namespaces import PrefixedCache
from pyfly.cache.transaction import TransactionAwareCache
from pyfly.cqrs.types import QueryCacheScope

if TYPE_CHECKING:
    from pyfly.cqrs.command.registry import HandlerRegistry
    from pyfly.cqrs.context.execution_context import ExecutionContext
    from pyfly.cqrs.query.handler import QueryHandler

_logger = logging.getLogger(__name__)

CQRS_CACHE_PREFIX = ":cqrs:"

SCOPE_SEPARATOR = "|scope="
"""What separates a scoped entry's key and generation from the digest of the caller's scope."""

GENERATION_SUFFIX = "|generation"
"""The suffix of the entry holding a key's current generation."""

DEFAULT_GENERATION_TTL = timedelta(seconds=900)
"""How long a generation lives when the TTL of its entries is unknown: the query bus's default cache TTL."""


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


def scope_digest(scope: QueryCacheScope, context: ExecutionContext | None) -> str | None:
    """A digest of the caller's scope (:func:`scope_of`), or ``None`` for a ``GLOBAL`` handler and for a
    caller with no tenant, organization or user at all (their entries are not scoped)."""
    parts = scope_of(scope, context)
    if all(value is None for _name, value in parts):
        return None
    return hashlib.sha256(repr(parts).encode("utf-8")).hexdigest()[:16]


class QueryCacheAdapter:
    """Thin wrapper around pyfly's :class:`CacheAdapter` with CQRS-specific key prefixing.

    If no underlying cache is provided, all operations are silent no-ops.

    Args:
        cache: The application's cache; the query cache is its ``:cqrs:`` region.
        generation_ttl: How long a key's generation lives until the adapter has seen the TTL of an entry
            (see the module docs); :data:`DEFAULT_GENERATION_TTL` when ``None``. The auto-configuration
            sets it to ``pyfly.cqrs.query.cache_ttl``.
    """

    def __init__(self, cache: Any = None, *, generation_ttl: timedelta | None = None) -> None:
        self._cache = cache
        self._region: TransactionAwareCache | None = None
        if cache is not None:
            self._region = TransactionAwareCache(PrefixedCache(cache, CQRS_CACHE_PREFIX), on_write_error="log")
        self._generation_ttl = generation_ttl if generation_ttl is not None else DEFAULT_GENERATION_TTL
        self._longest_ttl: timedelta | None = None

    # ── keys ───────────────────────────────────────────────────

    async def entry_key(self, cache_key: str, digest: str | None, ttl: timedelta | None = None) -> str | None:
        """The key the entry of *cache_key* lives under for the caller whose scope is *digest*
        (:func:`scope_digest`): *cache_key* itself when unscoped, else a key under the current generation of
        *cache_key*. ``None`` when the cache cannot be read (the call is then not cached).

        *ttl* is the TTL the entry will be stored with: a generation this call starts expires with it
        (``generation_ttl`` when ``None``).
        """
        lifetime = self._lifetime(ttl)
        if digest is None or self._region is None:
            return cache_key
        try:
            generation = await self._generation(cache_key, lifetime)
        except Exception as exc:
            _logger.warning("CQRS cache get failed for key '%s%s': %s", CQRS_CACHE_PREFIX, cache_key, exc)
            return None
        return f"{cache_key}|{generation}{SCOPE_SEPARATOR}{digest}"

    def _lifetime(self, ttl: timedelta | None) -> timedelta:
        """*ttl*, noted as an entry TTL (the next eviction's generation lives as long as the longest), or
        ``generation_ttl`` when it is unknown."""
        if ttl is None:
            return self._generation_ttl
        if self._longest_ttl is None or ttl > self._longest_ttl:
            self._longest_ttl = ttl
        return ttl

    async def _generation(self, cache_key: str, ttl: timedelta) -> str:
        assert self._region is not None
        key = cache_key + GENERATION_SUFFIX
        current = await self._region.get(key)
        if current is not None:
            return str(current)
        # No generation yet (or it expired): start a fresh one, so no older entry can be reached again.
        fresh = uuid.uuid4().hex[:12]
        if await self._region.put_if_absent(key, fresh, ttl=ttl):
            return fresh
        return str(await self._region.get(key) or fresh)

    def _new_generation_ttl(self) -> timedelta:
        return self._longest_ttl if self._longest_ttl is not None else self._generation_ttl

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
        if ttl is not None:
            self._lifetime(ttl)
        await self._region.put(cache_key, value, ttl=ttl)

    # ── evict ──────────────────────────────────────────────────

    async def evict(self, cache_key: str) -> bool:
        """Evict *cache_key* for every caller's scope: its unscoped entry, and its scoped entries by moving it
        to a new generation. After the commit inside a unit of work, where it returns ``False``. Both writes
        are one step: they run together, to completion even when the calling task is cancelled meanwhile."""
        region = self._region
        if region is None:
            return False
        store = region.delegate
        generation_ttl = self._new_generation_ttl()

        async def evict_every_scope() -> bool:
            evicted = await store.evict(cache_key)
            await store.put(cache_key + GENERATION_SUFFIX, uuid.uuid4().hex[:12], ttl=generation_ttl)
            return evicted

        return bool(await region.apply("evict", cache_key, evict_every_scope))

    async def evict_prefix(self, prefix: str) -> int:
        """Evict every entry whose key starts with *prefix* (a query handler's group), for every scope. This
        one scans the query cache's keys (a ``SCAN`` on Redis)."""
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


async def evict_query_key(cache: QueryCacheAdapter, registry: HandlerRegistry, cache_key: str) -> None:
    """Evict *cache_key* (a query's ``get_cache_key()``) for every caller's scope, as stored by a handler
    without a ``cache_key_prefix`` and by each registered handler that declares one."""
    await cache.evict(cache_key)
    prefixes = {
        prefix
        for query_type in registry.get_registered_query_types()
        if (prefix := registry.find_query_handler(query_type).get_cache_key_prefix()) is not None
    }
    for prefix in sorted(prefixes):
        await cache.evict(f"{prefix}:{cache_key}")
