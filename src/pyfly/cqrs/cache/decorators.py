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

T = TypeVar("T", bound=type)


def cacheable(
    *,
    ttl: int | None = None,
    cache_key_prefix: str | None = None,
) -> Callable[..., Any]:
    """Mark a query handler class as cacheable.

    Args:
        ttl: Cache TTL in seconds.  ``None`` uses the bus default.
        cache_key_prefix: Optional prefix for generated cache keys.

    Usage::

        @cacheable(ttl=300)
        @query_handler
        class GetOrderHandler(QueryHandler[GetOrderQuery, Order]):
            ...
    """

    def decorator(cls: T) -> T:
        cls.__pyfly_cacheable__ = True  # type: ignore[attr-defined]
        if ttl is not None:
            cls.__pyfly_cache_ttl__ = ttl  # type: ignore[attr-defined]
        if cache_key_prefix is not None:
            cls.__pyfly_cache_key_prefix__ = cache_key_prefix  # type: ignore[attr-defined]
        return cls

    return decorator


def cache_evict(*event_types: type) -> Callable[..., Any]:
    """Mark a handler or listener to evict cache entries when certain events fire.

    Usage::

        @cache_evict(OrderCreatedEvent, OrderUpdatedEvent)
        @command_handler
        class CreateOrderHandler(CommandHandler[CreateOrderCmd, OrderId]):
            ...
    """

    def decorator(cls: T) -> T:
        cls.__pyfly_cache_evict_events__ = event_types  # type: ignore[attr-defined]
        return cls

    return decorator
