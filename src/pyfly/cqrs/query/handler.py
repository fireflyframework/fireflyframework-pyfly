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
"""Enhanced query handler with lifecycle hooks and caching support.

Mirrors Java's ``QueryHandler`` abstract class.
"""

from __future__ import annotations

import logging
from types import get_original_bases as get_orig_bases
from typing import Any, Generic, TypeVar, get_args

from pyfly.cqrs.context.execution_context import ExecutionContext
from pyfly.cqrs.types import QueryCacheScope
from pyfly.kernel.exceptions import is_expected_error

Q = TypeVar("Q")  # Query type
R = TypeVar("R")  # Result type

_logger = logging.getLogger(__name__)


class QueryHandler(Generic[Q, R]):
    """Base class for query handlers with lifecycle hooks.

    Subclasses **must** implement :meth:`do_handle`.

    Example::

        @query_handler
        @service
        class GetOrderHandler(QueryHandler[GetOrderQuery, Order | None]):
            def __init__(self, repo: OrderRepository) -> None:
                self._repo = repo

            async def do_handle(self, query: GetOrderQuery) -> Order | None:
                return await self._repo.find_by_id(query.order_id)
    """

    def __init__(self) -> None:
        self._query_type: type | None = self._resolve_query_type()

    def _handler_type_args(self) -> tuple[Any, ...]:
        for base in get_orig_bases(type(self)):
            origin = getattr(base, "__origin__", None)
            if origin is not None and (
                origin is QueryHandler or (isinstance(origin, type) and issubclass(origin, QueryHandler))
            ):
                args = get_args(base)
                if args:
                    return args
        return ()

    def _resolve_query_type(self) -> type | None:
        args = self._handler_type_args()
        if args:
            return args[0] if isinstance(args[0], type) else None
        return None

    def get_query_type(self) -> type | None:
        return self._query_type

    def get_result_type(self) -> Any:
        """The declared result type ``R`` of ``QueryHandler[Q, R]`` (``Any`` when not declared).

        The bus rebuilds a cache hit as this type (a JSON cache returns plain JSON types) and refuses to
        cache a result type that is an ORM-mapped class or a Beanie document.
        """
        args = self._handler_type_args()
        return args[1] if len(args) > 1 else Any

    # ── caching metadata ───────────────────────────────────────

    def supports_caching(self) -> bool:
        """Whether this handler supports result caching.

        Reads from ``@query_handler(cacheable=True)`` decorator metadata.
        """
        return bool(getattr(type(self), "__pyfly_cacheable__", False))

    def get_cache_ttl_seconds(self) -> int | None:
        """Cache TTL in seconds, or *None* for the default."""
        return getattr(type(self), "__pyfly_cache_ttl__", None)

    def get_cache_key_prefix(self) -> str | None:
        """The prefix of this handler's cache keys (``cache_key_prefix``), or *None*.

        The bus stores a result under ``<prefix>:<query cache key>``; the prefix also names the group of
        entries an event-tag eviction removes (see :meth:`get_cache_evict_events`).
        """
        prefix = getattr(type(self), "__pyfly_cache_key_prefix__", None)
        return str(prefix) if prefix else None

    def get_cache_scope(self) -> QueryCacheScope:
        """Whose results a cached entry holds (``USER`` unless the handler declares otherwise).

        ``USER`` keys an entry by tenant, organization and user, ``TENANT`` by tenant and organization (and
        by user when neither is visible), and ``GLOBAL`` shares it with every caller (declare it only for data
        that is the same for everyone).
        """
        scope = getattr(type(self), "__pyfly_cache_scope__", None)
        return QueryCacheScope(scope) if scope is not None else QueryCacheScope.USER

    def caches_none(self) -> bool:
        """Whether a ``None`` result is cached (negative caching); off unless the handler declares it."""
        return bool(getattr(type(self), "__pyfly_cache_none__", False))

    def get_cache_evict_events(self) -> tuple[type, ...]:
        """The event types that invalidate this handler's cached entries (``@cache_evict(...)``)."""
        return tuple(t for t in getattr(type(self), "__pyfly_cache_evict_events__", ()) if isinstance(t, type))

    # ── template method ────────────────────────────────────────

    async def handle(self, query: Q) -> R:
        """Execute the full handler pipeline.  Do not override."""
        try:
            await self.pre_process(query)
            result = await self.do_handle(query)
            await self.post_process(query, result)
            await self.on_success(query, result)
            return result
        except Exception as exc:
            await self.on_error(query, exc)
            raise self.map_error(query, exc) from exc

    async def handle_with_context(self, query: Q, context: ExecutionContext) -> R:
        """Execute with an :class:`ExecutionContext`."""
        try:
            await self.pre_process(query)
            result = await self.do_handle_with_context(query, context)
            await self.post_process(query, result)
            await self.on_success(query, result)
            return result
        except Exception as exc:
            await self.on_error(query, exc)
            raise self.map_error(query, exc) from exc

    # ── abstract ───────────────────────────────────────────────

    async def do_handle(self, query: Q) -> R:
        raise NotImplementedError

    async def do_handle_with_context(self, query: Q, context: ExecutionContext) -> R:
        return await self.do_handle(query)

    # ── lifecycle hooks ────────────────────────────────────────

    async def pre_process(self, query: Q) -> None:
        """Called before ``do_handle``."""

    async def post_process(self, query: Q, result: R) -> None:
        """Called after ``do_handle`` on success."""

    async def on_success(self, query: Q, result: R) -> None:
        """Called after ``post_process``."""

    async def on_error(self, query: Q, error: Exception) -> None:
        # Expected client/domain faults (4xx) log cleanly at WARNING; only
        # unexpected infrastructure/5xx errors get a full traceback.
        if is_expected_error(error):
            _logger.warning("Query %s failed: %s", type(query).__name__, error)
        else:
            _logger.error("Query %s failed: %s", type(query).__name__, error, exc_info=True)

    def map_error(self, query: Q, error: Exception) -> Exception:
        return error


class ContextAwareQueryHandler(QueryHandler[Q, R]):
    """Base class for handlers that **require** an :class:`ExecutionContext`."""

    async def do_handle(self, query: Q) -> R:
        raise RuntimeError(
            f"{type(self).__name__} requires an ExecutionContext. Use handle_with_context() instead of handle()."
        )

    async def do_handle_with_context(self, query: Q, context: ExecutionContext) -> R:
        raise NotImplementedError
