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
"""CQRS caching decorators for query handlers.

Mirrors Java's ``@Cacheable`` and ``@CacheEvict`` annotations on
``@QueryHandlerComponent``.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, TypeVar

from pyfly.cqrs.types import QueryCacheScope

T = TypeVar("T", bound=type)


def cacheable(
    *,
    ttl: int | None = None,
    cache_key_prefix: str | None = None,
    scope: QueryCacheScope | str | None = None,
    cache_none: bool | None = None,
) -> Callable[..., Any]:
    """Mark a query handler class as cacheable.

    Args:
        ttl: Cache TTL in seconds.  ``None`` uses the bus default.
        cache_key_prefix: Prefix of the handler's cache keys (``<prefix>:<query key>``). It also names the
            group of entries an event-tag eviction removes; declare it when the query overrides
            ``get_cache_key()`` and the handler is tagged with :func:`cache_evict`.
        scope: Whose results an entry holds: ``QueryCacheScope.USER`` (the default: tenant, organization and
            user), ``TENANT`` (tenant and organization) or ``GLOBAL`` (shared with every caller; only for
            data that is the same for everyone).
        cache_none: Cache a ``None`` result too (negative caching), so a lookup for a missing row stops
            reaching the database. Off by default.

    Usage::

        @cacheable(ttl=300, scope=QueryCacheScope.TENANT)
        @query_handler
        class GetOrderHandler(QueryHandler[GetOrderQuery, OrderDto]):
            ...

    Apply it above ``@query_handler``: ``@query_handler`` sets the handler's caching switch itself.
    """

    def decorator(cls: T) -> T:
        cls.__pyfly_cacheable__ = True  # type: ignore[attr-defined]
        if ttl is not None:
            cls.__pyfly_cache_ttl__ = ttl  # type: ignore[attr-defined]
        if cache_key_prefix is not None:
            cls.__pyfly_cache_key_prefix__ = cache_key_prefix  # type: ignore[attr-defined]
        if scope is not None:
            cls.__pyfly_cache_scope__ = QueryCacheScope(scope).value  # type: ignore[attr-defined]
        if cache_none is not None:
            cls.__pyfly_cache_none__ = cache_none  # type: ignore[attr-defined]
        return cls

    return decorator


def cache_evict(*event_types: type) -> Callable[..., Any]:
    """Tag a handler with the event types that invalidate cached query results.

    The command bus applies the tags after the command's unit of work commits (at once without one):

    - On a **query handler**, the tags name the events that make its cached results stale. When a command
      produces a domain event of one of these types (``domain_events`` on its result or on the command),
      every cached result of the handler is evicted (the group ``<cache_key_prefix>:``, or
      ``<QueryClass>:`` for the default query keys).
    - On a **command handler**, the tags name the events the command stands for: after it commits, the
      query handlers tagged with the same types (or a base class of them) are evicted, even when the
      command produced no event object.

    Usage::

        @cache_evict(OrderUpdated)
        @query_handler(cacheable=True)
        class GetOrderHandler(QueryHandler[GetOrderQuery, OrderDto]): ...

        @cache_evict(OrderUpdated)
        @command_handler
        class ShipOrderHandler(CommandHandler[ShipOrder, None]): ...
    """

    def decorator(cls: T) -> T:
        cls.__pyfly_cache_evict_events__ = event_types  # type: ignore[attr-defined]
        return cls

    return decorator
