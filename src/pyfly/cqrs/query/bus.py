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
"""

from __future__ import annotations

import logging
from typing import Any, Protocol, runtime_checkable

from pyfly.cqrs.authorization.service import AuthorizationService
from pyfly.cqrs.command.metrics import CqrsMetricsService
from pyfly.cqrs.command.registry import HandlerRegistry
from pyfly.cqrs.command.validation import CommandValidationService
from pyfly.cqrs.context.execution_context import ExecutionContext
from pyfly.cqrs.exceptions import QueryProcessingException
from pyfly.cqrs.query.handler import QueryHandler
from pyfly.cqrs.tracing.correlation import CorrelationContext
from pyfly.cqrs.types import Query

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
    6. Store result in cache
    7. Record metrics
    """

    def __init__(
        self,
        registry: HandlerRegistry,
        validation: CommandValidationService | None = None,
        authorization: AuthorizationService | None = None,
        metrics: CqrsMetricsService | None = None,
        cache_adapter: Any | None = None,
        default_cache_ttl: int = 900,
    ) -> None:
        self._registry = registry
        self._validation = validation
        self._authorization = authorization
        self._metrics = metrics or CqrsMetricsService()
        self._cache = cache_adapter
        self._default_cache_ttl = default_cache_ttl

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
        if self._cache:
            await self._cache.evict(cache_key)

    async def clear_all_cache(self) -> None:
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

            # 5. Cache check
            cached_result = await self._try_cache_get(query, handler)
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
            await self._try_cache_put(query, handler, result)

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

    async def _try_cache_get(self, query: Query[Any], handler: QueryHandler[Any, Any]) -> Any:
        if not self._cache:
            return _CACHE_MISS
        if not query.is_cacheable():
            return _CACHE_MISS
        if not handler.supports_caching():
            return _CACHE_MISS
        cache_key = self._build_cache_key(query)
        if cache_key is None:
            return _CACHE_MISS
        try:
            result = await self._cache.get(cache_key)
            if result is None:
                return _CACHE_MISS
            return result
        except Exception as exc:
            _logger.warning("Cache get failed for %s: %s", cache_key, exc)
            return _CACHE_MISS

    async def _try_cache_put(self, query: Query[Any], handler: QueryHandler[Any, Any], result: Any) -> None:
        if not self._cache:
            return
        if not query.is_cacheable():
            return
        if not handler.supports_caching():
            return
        cache_key = self._build_cache_key(query)
        if cache_key is None:
            return
        ttl_seconds = handler.get_cache_ttl_seconds() or self._default_cache_ttl
        try:
            from datetime import timedelta

            await self._cache.put(cache_key, result, ttl=timedelta(seconds=ttl_seconds))
        except Exception as exc:
            _logger.warning("Cache put failed for %s: %s", cache_key, exc)

    @staticmethod
    def _build_cache_key(query: Query[Any]) -> str | None:
        key = query.get_cache_key()
        if key:
            return f":cqrs:{key}"
        return None
