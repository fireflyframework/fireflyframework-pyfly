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
for the commit and are dropped on rollback, and a lookup (its reads, and the ``put_if_absent`` that starts a
key's generation) runs at once outside the unit, so a database-backed cache never holds a lock of the
business transaction on the cache's rows. A cache failure is logged and never fails the query or the
command that caused it.

An entry is keyed by the caller's scope too (:func:`scope_of`, :func:`scope_digest`): a result cached for
one tenant or user is never served to another, and a call whose caller the cache cannot identify is not
cached at all (it fails closed). Evicting a key evicts it for every scope with two deletes and no write:
the scoped entries of a key live under its current *generation* (``<key>|<generation>|scope=<digest>``),
and eviction deletes the generation (and the key's unscoped entry), which leaves the old entries
unreachable until their TTL expires. The next lookup of the key starts a fresh generation. No eviction by
key scans the keyspace, and a key that was never cached costs no write.

A generation is an entry too (``<key>|generation``), and it expires: it is created with the TTL of the
entries looked up under it. It is never refreshed: rewriting it could race an eviction and bring the
evicted entries back. An entry stored late in its generation's life can therefore become unreachable
before its own TTL, which costs one extra miss and never serves a stale value: generations are random and
never reused, so a deleted or expired one is replaced by a fresh one that reaches no older entry.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Iterable
from datetime import timedelta
from typing import TYPE_CHECKING, Any

from pyfly.cache.namespaces import PrefixedCache
from pyfly.cache.transaction import TransactionAwareCache
from pyfly.cqrs.types import QueryCacheScope, cache_key_digest

if TYPE_CHECKING:
    from pyfly.cqrs.command.registry import HandlerRegistry
    from pyfly.cqrs.context.execution_context import ExecutionContext
    from pyfly.cqrs.query.handler import QueryHandler

_logger = logging.getLogger(__name__)

CQRS_CACHE_PREFIX = ":cqrs:"

SCOPE_SEPARATOR = "|scope="
"""What separates a scoped entry's key and generation from the digest of the caller's scope. A query key
that contains it is never cached."""

GENERATION_SUFFIX = "|generation"
"""The suffix of the entry holding a key's current generation. A query key that ends with it is never
cached."""

DEFAULT_GENERATION_TTL = timedelta(seconds=900)
"""How long a generation lives when the TTL of its entries is unknown: the query bus's default cache TTL."""

EVICTION_CONCURRENCY = 4
"""How many deletes one eviction (:meth:`QueryCacheAdapter.evict_keys`) runs at a time. A key is deleted
under every ``cache_key_prefix`` with its generation, and on a database-backed cache each delete checks out
a pooled connection: unbounded, one command could take the whole pool."""


def _ambient_tenant() -> str | None:
    """The ``X-Tenant-Id`` of the running request: client-supplied and never authenticated."""
    from pyfly.observability.correlation import get_tenant_id

    return get_tenant_id()


def _ambient_user() -> str | None:
    """The authenticated principal of the running request (``RequestContext.security_context``)."""
    from pyfly.context.request_context import RequestContext

    request = RequestContext.current()
    security = request.security_context if request is not None else None
    return (security.user_id or None) if security is not None and security.is_authenticated else None


Scope = tuple[tuple[str, str | None], ...]
"""The identity an entry is keyed by (:func:`scope_of`): ``(name, value)`` pairs."""


def scope_of(scope: QueryCacheScope, context: ExecutionContext | None) -> Scope | None:
    """The identity an entry of a *scope* handler is keyed by for the running caller; ``()`` for ``GLOBAL``,
    and ``None`` when the identity the scope needs is not visible, so the call must not be cached.

    Only a trusted identity lets a call be cached: the tenant, organization and user of *context* (the
    application built it), and the authenticated principal of the running request. ``USER`` needs a user
    (the context's or the principal), ``TENANT`` a tenant or organization of the context, or else a user
    (an application may take the tenant from somewhere the cache cannot see, such as a claim of the
    principal). The ``X-Tenant-Id`` header of the request is client-supplied and never authenticated: its
    value is part of every scoped key, so it can only narrow an entry, but it never identifies a caller.
    Every identity that is visible narrows the key; a ``TENANT`` entry keyed by a trusted tenant leaves
    the users out, so the users of a tenant share it. An empty identifier counts as none.
    """
    if scope is QueryCacheScope.GLOBAL:
        return ()
    tenant = (context.tenant_id if context is not None else None) or None
    organization = (context.organization_id if context is not None else None) or None
    parts: list[tuple[str, str | None]] = [
        ("tenant", tenant),
        ("organization", organization),
        ("tenant_header", _ambient_tenant() or None),
    ]
    if scope is QueryCacheScope.TENANT and (tenant is not None or organization is not None):
        return tuple(parts)
    user = (context.user_id if context is not None else None) or None
    principal = _ambient_user()
    if user is None and principal is None:
        return None
    parts += [("user", user), ("principal", principal)]
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


def scope_digest(scope: Scope) -> str | None:
    """The digest of *scope* (:func:`scope_of`) to key an entry by; ``None`` for ``()`` (a ``GLOBAL`` entry is
    not scoped).

    It is the full SHA-256 of the scope's names and values, each length-prefixed
    (:func:`~pyfly.cqrs.types.cache_key_digest`; an identifier that is not a ``str``, such as a UUID, counts as
    its ``str()``): the digest is all that separates one caller's entry from another's under a key's
    generation, and the scope holds the ``X-Tenant-Id`` header, which the client chooses. A truncated digest
    would let a client search offline for a header value that collides with another caller's scope. It raises
    what ``str()`` of an identifier raises; the query bus then does not cache the call.
    """
    if not scope:
        return None
    return cache_key_digest(*(part for pair in scope for part in pair))


class QueryCacheAdapter:
    """Thin wrapper around pyfly's :class:`CacheAdapter` with CQRS-specific key prefixing.

    If no underlying cache is provided, all operations are silent no-ops.

    Args:
        cache: The application's cache; the query cache is its ``:cqrs:`` region.
        generation_ttl: How long a key's generation lives when the TTL of the entries looked up under it is
            not known (see the module docs); :data:`DEFAULT_GENERATION_TTL` when ``None``. The
            auto-configuration sets it to ``pyfly.cqrs.query.cache_ttl``.
    """

    def __init__(self, cache: Any = None, *, generation_ttl: timedelta | None = None) -> None:
        self._cache = cache
        self._region: TransactionAwareCache | None = None
        if cache is not None:
            self._region = TransactionAwareCache(PrefixedCache(cache, CQRS_CACHE_PREFIX), on_write_error="log")
        self._generation_ttl = generation_ttl if generation_ttl is not None else DEFAULT_GENERATION_TTL
        self._reserved_key_reported = False

    # ── keys ───────────────────────────────────────────────────

    async def entry_key(self, cache_key: str, digest: str | None, ttl: timedelta | None = None) -> str | None:
        """The key the entry of *cache_key* lives under for the caller whose scope is *digest*
        (:func:`scope_digest`): *cache_key* itself when unscoped, else a key under the current generation of
        *cache_key*. ``None`` when the call is not cached: the cache cannot be read, no generation could be
        started (the cache refused to store one; the region logged it), or *cache_key* contains
        :data:`SCOPE_SEPARATOR` or ends with :data:`GENERATION_SUFFIX` (it could then address another scope's
        entry, or a key's generation; such a key is reported once).

        *ttl* is the TTL the entry will be stored with: a generation this call starts expires with it
        (``generation_ttl`` when ``None``).
        """
        if SCOPE_SEPARATOR in cache_key or cache_key.endswith(GENERATION_SUFFIX):
            self._report_reserved(cache_key)
            return None
        lifetime = ttl if ttl is not None else self._generation_ttl
        if digest is None or self._region is None:
            return cache_key
        try:
            generation = await self._generation(cache_key, lifetime)
        except Exception as exc:
            _logger.warning("CQRS cache get failed for key '%s%s': %s", CQRS_CACHE_PREFIX, cache_key, exc)
            return None
        if generation is None:
            return None
        return f"{cache_key}|{generation}{SCOPE_SEPARATOR}{digest}"

    def _report_reserved(self, cache_key: str) -> None:
        if self._reserved_key_reported:
            return
        self._reserved_key_reported = True
        _logger.warning(
            "The query cache key %r contains %r or ends with %r, which the query cache uses to separate scopes "
            "and generations: queries with such keys are never cached",
            cache_key,
            SCOPE_SEPARATOR,
            GENERATION_SUFFIX,
        )

    async def _generation(self, cache_key: str, ttl: timedelta) -> str | None:
        """The current generation of *cache_key*, started now when it has none; ``None`` when none could be
        started (an entry stored under a generation that is not in the cache could never be reached)."""
        assert self._region is not None
        key = cache_key + GENERATION_SUFFIX
        current = await self._region.get(key)
        if current is not None:
            return str(current)
        # No generation yet (or it expired): start a fresh one, so no older entry can be reached again.
        fresh = uuid.uuid4().hex[:12]
        if await self._region.put_if_absent(key, fresh, ttl=ttl):
            return fresh
        current = await self._region.get(key)  # another caller started one meanwhile, or the put was refused
        return None if current is None else str(current)

    # ── read ───────────────────────────────────────────────────

    async def get(self, cache_key: str) -> Any | None:
        if self._region is None:
            return None
        try:
            return await self._region.get(cache_key)
        except Exception as exc:
            _logger.warning("CQRS cache get failed for key '%s%s': %s", CQRS_CACHE_PREFIX, cache_key, exc)
            return None

    async def lookup(self, cache_key: str, *, none_cached: bool = True) -> tuple[bool, Any]:
        """``(True, value)`` for a hit, a stored ``None`` included; ``(False, None)`` for a miss.

        With *none_cached* ``False`` (the caller never stores ``None``) a stored ``None`` is a miss too, and
        a miss costs no existence check.
        """
        if self._region is None:
            return False, None
        try:
            value = await self._region.get(cache_key)
            if value is not None:
                return True, value
            if not none_cached:
                return False, None
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
        """Evict *cache_key* for every caller's scope (see :meth:`evict_keys`); whether anything was cached
        for it. After the commit inside a unit of work, where it returns ``False``."""
        return await self.evict_keys([cache_key]) > 0

    async def evict_keys(self, cache_keys: Iterable[str]) -> int:
        """Evict each of *cache_keys* for every caller's scope: its unscoped entry, and its scoped entries by
        deleting its generation; how many of the keys had anything cached. Nothing is written.

        The deletes are one step: they run concurrently (at most :data:`EVICTION_CONCURRENCY` at a time),
        after the commit inside a unit of work (``0`` is returned then) and at once outside one, outside the
        caller's unit either way, and to completion even when the calling task is cancelled meanwhile. A
        failing delete is logged once the others have run."""
        region = self._region
        targets = [key for cache_key in dict.fromkeys(cache_keys) for key in (cache_key, cache_key + GENERATION_SUFFIX)]
        if region is None or not targets:
            return 0
        store = region.delegate

        async def evict_every_scope() -> int:
            slots = asyncio.Semaphore(EVICTION_CONCURRENCY)

            async def evict(key: str) -> bool:
                async with slots:
                    return await store.evict(key)

            outcomes = await asyncio.gather(*(evict(key) for key in targets), return_exceptions=True)
            for outcome in outcomes:
                if isinstance(outcome, BaseException):
                    raise outcome
            found = {
                key.removesuffix(GENERATION_SUFFIX) for key, existed in zip(targets, outcomes, strict=True) if existed
            }
            return len(found)

        return int(await region.apply("evict", targets[0], evict_every_scope) or 0)

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
    without a ``cache_key_prefix`` and by each registered handler that declares one: one step of concurrent
    deletes (:meth:`QueryCacheAdapter.evict_keys`)."""
    prefixes = {
        prefix
        for query_type in registry.get_registered_query_types()
        if (prefix := registry.find_query_handler(query_type).get_cache_key_prefix()) is not None
    }
    await cache.evict_keys([cache_key, *(f"{prefix}:{cache_key}" for prefix in sorted(prefixes))])
