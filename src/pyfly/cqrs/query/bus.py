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
"""QueryBus — central mediator for query processing with caching.

Mirrors Java's ``QueryBus`` interface and ``DefaultQueryBus``
implementation.  The full pipeline is:

    correlate → validate → authorize → cache check → execute → cache put → metrics

Caching (a ``@query_handler(cacheable=True)`` handler, a cacheable query, a cache adapter):

- **Keys carry the caller's scope, and the cache fails closed.** An entry is keyed by the query's
  ``get_cache_key()`` (after the handler's ``cache_key_prefix``) and by the caller: the tenant,
  organization and user of the ``ExecutionContext`` of :meth:`DefaultQueryBus.query_with_context`, and the
  authenticated principal of the request (see :func:`~pyfly.cqrs.cache.adapter.scope_of`). A result cached
  for one tenant or user is never served to another. The handler's cache scope decides who shares an
  entry: one user (``USER``, the default), the users of a tenant (``TENANT``), or everyone (``GLOBAL``, for
  data that is the same for everyone). A call whose caller the scope cannot identify (no user for
  ``USER``; no tenant in the context, and no user, for ``TENANT``) is not cached, with one WARNING per
  handler: the ``X-Tenant-Id`` header narrows an entry but never identifies a caller, since any client can
  send it. A ``ContextAwareQueryHandler`` is never served from the cache without a context: it refuses
  such a call.
- **Writes wait for the commit.** Inside a unit of work the result is stored after the commit, and not
  at all on rollback (it may be a row that never commits).
- **Hits have the declared type.** A hit is rebuilt as the handler's result type ``R``, so a JSON cache
  (Redis, PostgreSQL) returns the DTO, not a ``dict`` (fields by name or alias, see
  :func:`~pyfly.cache.serialization.restore`). A result type that is an ORM-mapped class or a Beanie
  document is never cached (a WARNING names the handler once).
- **None** is cached only when the handler opts in (``cache_none``); otherwise a ``None`` result is not
  stored, and the next call runs the handler again.
- ``pyfly.cqrs.query.caching_enabled: false`` turns the query cache off.
"""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any, Protocol, runtime_checkable

from pyfly.cache.serialization import restore, uncacheable_type
from pyfly.cqrs.authorization.service import AuthorizationService
from pyfly.cqrs.cache.adapter import QueryCacheAdapter, evict_query_key, query_cache_key, scope_digest, scope_of
from pyfly.cqrs.command.metrics import CqrsMetricsService
from pyfly.cqrs.command.registry import HandlerRegistry
from pyfly.cqrs.command.validation import CommandValidationService
from pyfly.cqrs.context.execution_context import ExecutionContext
from pyfly.cqrs.exceptions import QueryProcessingException
from pyfly.cqrs.query.handler import ContextAwareQueryHandler, QueryHandler
from pyfly.cqrs.tracing.correlation import CorrelationContext
from pyfly.cqrs.types import Query, QueryCacheScope

_logger = logging.getLogger(__name__)

_CACHE_MISS = object()


@runtime_checkable
class QueryBus(Protocol):
    """Port for dispatching queries through the CQRS pipeline."""

    async def query(self, query: Query[Any]) -> Any: ...

    async def query_with_context(self, query: Query[Any], context: ExecutionContext) -> Any: ...

    def register_handler(self, handler: QueryHandler[Any, Any]) -> None: ...

    def unregister_handler(self, query_type: type) -> None: ...

    def has_handler(self, query_type: type) -> bool: ...

    async def clear_cache(self, cache_key: str) -> None: ...

    async def clear_all_cache(self) -> None: ...


class DefaultQueryBus:
    """Production-ready implementation of :class:`QueryBus`.

    Pipeline:
    1. Set correlation context
    2. Validate query
    3. Authorize query
    4. Check cache (if enabled & handler supports caching)
    5. Execute handler on cache miss
    6. Store result in cache (after the commit inside a unit of work)
    7. Record metrics

    *cache_adapter* is a :class:`~pyfly.cqrs.cache.adapter.QueryCacheAdapter`; a plain
    :class:`~pyfly.cache.ports.outbound.CacheAdapter` is wrapped in one.
    """

    def __init__(
        self,
        registry: HandlerRegistry,
        validation: CommandValidationService | None = None,
        authorization: AuthorizationService | None = None,
        metrics: CqrsMetricsService | None = None,
        cache_adapter: Any | None = None,
        default_cache_ttl: int = 900,
        *,
        caching_enabled: bool = True,
    ) -> None:
        self._registry = registry
        self._validation = validation
        self._authorization = authorization
        self._metrics = metrics or CqrsMetricsService()
        self._cache: QueryCacheAdapter | None = (
            cache_adapter
            if cache_adapter is None or isinstance(cache_adapter, QueryCacheAdapter)
            else QueryCacheAdapter(cache_adapter, generation_ttl=timedelta(seconds=default_cache_ttl))
        )
        self._default_cache_ttl = default_cache_ttl
        self._caching_enabled = caching_enabled
        self._uncacheable: dict[type, bool] = {}
        self._misfits: set[type] = set()
        self._unidentified: set[type] = set()
        self._unkeyable: set[type] = set()

    # ── QueryBus protocol ──────────────────────────────────────

    async def query(self, query: Query[Any]) -> Any:
        return await self._execute(query, context=None)

    async def query_with_context(self, query: Query[Any], context: ExecutionContext) -> Any:
        return await self._execute(query, context=context)

    def register_handler(self, handler: QueryHandler[Any, Any]) -> None:
        self._registry.register_query_handler(handler)

    def unregister_handler(self, query_type: type) -> None:
        self._registry.unregister_query_handler(query_type)

    def has_handler(self, query_type: type) -> bool:
        return self._registry.has_query_handler(query_type)

    async def clear_cache(self, cache_key: str) -> None:
        """Evict the entries of *cache_key* (a query's ``get_cache_key()``) for every caller, under every
        registered handler's ``cache_key_prefix`` too; after the commit inside a unit of work."""
        if self._cache:
            await evict_query_key(self._cache, self._registry, cache_key)

    async def clear_all_cache(self) -> None:
        """Evict every query-cache entry, and nothing else the cache holds."""
        if self._cache:
            await self._cache.clear()

    # ── pipeline ───────────────────────────────────────────────

    async def _execute(self, query: Query[Any], context: ExecutionContext | None) -> Any:
        start = self._metrics.now()
        query_name = type(query).__name__

        try:
            # 1. Correlation
            cid = query.get_correlation_id() or CorrelationContext.get_or_create_correlation_id()
            CorrelationContext.set_correlation_id(cid)
            query.set_correlation_id(cid)

            # 2. Validate
            if self._validation:
                await self._validation.validate_query(query)

            # 3. Authorize
            if self._authorization:
                await self._authorization.authorize_query(query, context)

            # 4. Find handler
            handler = self._registry.find_query_handler(type(query))

            # 5. Cache check (keyed by the caller's scope)
            cache_key = await self._cache_key(query, handler, context)
            cached_result = await self._try_cache_get(cache_key, handler)
            if cached_result is not _CACHE_MISS:
                duration = self._metrics.now() - start
                self._metrics.record_query_success(query, duration)
                _logger.debug("Query %s served from cache in %.3fs", query_name, duration)
                return cached_result

            # 6. Execute
            if context is not None:
                result = await handler.handle_with_context(query, context)
            else:
                result = await handler.handle(query)

            # 7. Cache put
            await self._try_cache_put(cache_key, handler, result)

            # 8. Metrics
            duration = self._metrics.now() - start
            self._metrics.record_query_success(query, duration)

            _logger.debug("Query %s processed in %.3fs", query_name, duration)
            return result

        except Exception as exc:
            duration = self._metrics.now() - start
            self._metrics.record_query_failure(query, exc, duration)
            if not isinstance(exc, QueryProcessingException):
                raise QueryProcessingException(
                    message=f"Failed to process query {query_name}: {exc}",
                    query_type=type(query),
                    cause=exc,
                ) from exc
            raise

    # ── caching helpers ────────────────────────────────────────

    async def _cache_key(
        self, query: Query[Any], handler: QueryHandler[Any, Any], context: ExecutionContext | None
    ) -> str | None:
        """The cache key of this call, or ``None`` when it is not cached."""
        if not self._cache or not self._caching_enabled:
            return None
        if not query.is_cacheable() or not handler.supports_caching():
            return None
        if context is None and isinstance(handler, ContextAwareQueryHandler):
            return None  # the handler refuses a call without a context; a hit must not bypass that
        if not self._result_type_cacheable(handler):
            return None
        try:
            raw = query.get_cache_key()
        except Exception as error:  # noqa: BLE001 — a cache problem never fails the query: fail closed
            self._report_unkeyable(handler, error)
            return None
        if not raw:
            return None
        scope = handler.get_cache_scope()
        caller = scope_of(scope, context)
        if caller is None:
            self._report_unidentified(handler, scope)
            return None  # fail closed: the entry would be shared by every caller the cache cannot tell apart
        try:
            digest = scope_digest(caller)
        except Exception as error:  # noqa: BLE001 — a cache problem never fails the query: fail closed
            self._report_unkeyable(handler, error)
            return None
        return await self._cache.entry_key(query_cache_key(handler, raw), digest, ttl=self._ttl(handler))

    def _report_unkeyable(self, handler: QueryHandler[Any, Any], error: Exception) -> None:
        handler_type = type(handler)
        if handler_type in self._unkeyable:
            return
        self._unkeyable.add(handler_type)
        _logger.warning(
            "query_cache_skipped handler=%s: the call cannot be keyed (%s: %s), so the result is not cached; fix "
            "the query's get_cache_key(), or pass the tenant and user as strings in the ExecutionContext",
            handler_type.__qualname__,
            type(error).__name__,
            error,
        )

    def _report_unidentified(self, handler: QueryHandler[Any, Any], scope: QueryCacheScope) -> None:
        handler_type = type(handler)
        if handler_type in self._unidentified:
            return
        self._unidentified.add(handler_type)
        needs = "a user" if scope is QueryCacheScope.USER else "a tenant or organization in the context, or a user"
        _logger.warning(
            "query_cache_skipped handler=%s scope=%s: no caller identity is visible to the query cache (it needs "
            "%s), so the result is not cached; pass an ExecutionContext with the tenant and user to "
            "query_with_context(), or declare @cacheable(scope=QueryCacheScope.GLOBAL) for data that is the same "
            "for everyone",
            handler_type.__qualname__,
            scope.name,
            needs,
        )

    def _ttl(self, handler: QueryHandler[Any, Any]) -> timedelta:
        return timedelta(seconds=handler.get_cache_ttl_seconds() or self._default_cache_ttl)

    def _result_type_cacheable(self, handler: QueryHandler[Any, Any]) -> bool:
        handler_type = type(handler)
        cacheable = self._uncacheable.get(handler_type)
        if cacheable is None:
            reason = uncacheable_type(handler.get_result_type())
            cacheable = reason is None
            self._uncacheable[handler_type] = cacheable
            if reason is not None:
                _logger.warning(
                    "query_cache_disabled handler=%s: its result type is %s, which is never cached (a cached ORM "
                    "object would be shared across requests and outlive its session); return a DTO instead",
                    handler_type.__qualname__,
                    reason,
                )
        return cacheable

    async def _try_cache_get(self, cache_key: str | None, handler: QueryHandler[Any, Any]) -> Any:
        if cache_key is None or self._cache is None:
            return _CACHE_MISS
        caches_none = handler.caches_none()
        found, value = await self._cache.lookup(cache_key, none_cached=caches_none)
        if not found:
            return _CACHE_MISS
        if value is None:
            return None if caches_none else _CACHE_MISS
        try:
            return restore(value, handler.get_result_type())
        except Exception as exc:  # noqa: BLE001 — an entry that does not fit the result type is a miss
            if type(handler) not in self._misfits:
                self._misfits.add(type(handler))
                _logger.warning(
                    "Cached result for %s does not fit the result type of %s (an entry written by an older "
                    "version of the type, or a handler that returns another type than it declares); it is "
                    "treated as a miss: %s",
                    cache_key,
                    type(handler).__name__,
                    exc,
                )
            return _CACHE_MISS

    async def _try_cache_put(self, cache_key: str | None, handler: QueryHandler[Any, Any], result: Any) -> None:
        if cache_key is None or self._cache is None:
            return
        if result is None and not handler.caches_none():
            return  # no negative caching unless the handler opts in
        await self._cache.put(cache_key, result, ttl=self._ttl(handler))
