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
"""The result type ``R`` a handler declares, wherever it declares it, and what a hit of it costs.

The bus rebuilds a hit as ``R`` and refuses to cache an ORM result type, so ``R`` must be the type the
handler really returns: ``QueryHandler[Q, R]`` may be parametrized through a generic base of the
application's own (``PagedHandler[Q, T]`` extending ``QueryHandler[Q, Page[T]]``), or inherited from a
concrete handler. A type the bus reads wrong makes every hit a misfit, which silently disables the cache.
A structural (``Protocol``) result type cannot be rebuilt: its hits are served as stored.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Generic, Protocol, TypeVar

import pytest
from pydantic import BaseModel

from pyfly.cache.adapters.memory import InMemoryCache
from pyfly.cache.adapters.redis import RedisCacheAdapter
from pyfly.cache.ports.outbound import CacheAdapter
from pyfly.cqrs.cache.adapter import QueryCacheAdapter
from pyfly.cqrs.command.registry import HandlerRegistry
from pyfly.cqrs.context.execution_context import ExecutionContextBuilder
from pyfly.cqrs.decorators import query_handler
from pyfly.cqrs.query.bus import DefaultQueryBus
from pyfly.cqrs.query.handler import ContextAwareQueryHandler, QueryHandler
from pyfly.cqrs.types import Query
from pyfly.data.relational.sqlalchemy.entity import Base
from tests.cache.test_cache import CachedProduct, RedisBytesStub

Q = TypeVar("Q")
T = TypeVar("T")
E = TypeVar("E")

CALLER = ExecutionContextBuilder().with_tenant_id("acme").with_user_id("alice").build()


class Page(BaseModel, Generic[T]):
    items: list[T]
    total: int


class ItemDto(BaseModel):
    id: int


class Priced(Protocol):
    price: int


@dataclass
class PricedItem:
    price: int


class PagedHandler(QueryHandler[Q, Page[T]], Generic[Q, T]):
    """An application's own generic base: ``R`` is ``Page[T]``."""


class ContextPagedHandler(ContextAwareQueryHandler[Q, Page[T]], Generic[Q, T]):
    """The same through ``ContextAwareQueryHandler``."""


class ReorderedHandler(QueryHandler[Q, list[E]], Generic[E, Q]):
    """Parameters declared in another order than ``QueryHandler``'s."""


@dataclass(frozen=True)
class ListItems(Query[Page[ItemDto]]):
    pass


@dataclass(frozen=True)
class ListItemsInContext(Query[Page[ItemDto]]):
    pass


@dataclass(frozen=True)
class ListIds(Query[list[ItemDto]]):
    pass


@dataclass(frozen=True)
class GetPriced(Query[Priced]):
    pass


@dataclass(frozen=True)
class ListProducts(Query[Any]):
    pass


@query_handler(cacheable=True)
class ListItemsHandler(PagedHandler[ListItems, ItemDto]):
    def __init__(self) -> None:
        super().__init__()
        self.calls = 0

    async def do_handle(self, query: ListItems) -> Page[ItemDto]:
        self.calls += 1
        return Page[ItemDto](items=[ItemDto(id=1), ItemDto(id=2)], total=2)


class AuditedListItemsHandler(ListItemsHandler):
    """A plain subclass of a concrete handler: it inherits ``R``."""


@query_handler(cacheable=True)
class ListItemsInContextHandler(ContextPagedHandler[ListItemsInContext, ItemDto]):
    def __init__(self) -> None:
        super().__init__()
        self.calls = 0

    async def do_handle_with_context(self, query: ListItemsInContext, context: Any) -> Page[ItemDto]:
        self.calls += 1
        return Page[ItemDto](items=[ItemDto(id=3)], total=1)


@query_handler(cacheable=True)
class ListIdsHandler(ReorderedHandler[ItemDto, ListIds]):
    def __init__(self) -> None:
        super().__init__()
        self.calls = 0

    async def do_handle(self, query: ListIds) -> list[ItemDto]:
        self.calls += 1
        return [ItemDto(id=4)]


@query_handler(cacheable=True)
class GetPricedHandler(QueryHandler[GetPriced, Priced]):
    def __init__(self) -> None:
        super().__init__()
        self.calls = 0

    async def do_handle(self, query: GetPriced) -> Priced:
        self.calls += 1
        return PricedItem(price=5)


class EntityHandler(QueryHandler[Q, list[E]], Generic[Q, E]):
    pass


@query_handler(cacheable=True)
class ListProductsHandler(EntityHandler[ListProducts, CachedProduct]):
    """Its result type is ``list[CachedProduct]``, an ORM entity: never cached, even through a generic base."""

    def __init__(self) -> None:
        super().__init__()
        self.calls = 0

    async def do_handle(self, query: ListProducts) -> list[CachedProduct]:
        self.calls += 1
        return []


def test_the_result_type_is_resolved_through_generic_bases_and_subclasses() -> None:
    assert ListItemsHandler().get_result_type() == Page[ItemDto]
    assert ListItemsHandler().get_query_type() is ListItems
    assert AuditedListItemsHandler().get_result_type() == Page[ItemDto]
    assert AuditedListItemsHandler().get_query_type() is ListItems
    assert ListItemsInContextHandler().get_result_type() == Page[ItemDto]
    assert ListIdsHandler().get_result_type() == list[ItemDto]
    assert ListIdsHandler().get_query_type() is ListIds
    assert ListProductsHandler().get_result_type() == list[CachedProduct]
    assert issubclass(CachedProduct, Base)


def _cache(json: bool) -> CacheAdapter:
    return RedisCacheAdapter(RedisBytesStub()) if json else InMemoryCache()


@pytest.mark.parametrize("json", [False, True], ids=["in_memory", "json"])
@pytest.mark.parametrize(
    "handler_type",
    [ListItemsHandler, AuditedListItemsHandler, ListItemsInContextHandler, ListIdsHandler, GetPricedHandler],
)
async def test_repeated_identical_queries_run_the_handler_once(
    handler_type: type[Any], json: bool, caplog: pytest.LogCaptureFixture
) -> None:
    handler = handler_type()
    registry = HandlerRegistry()
    registry.register_query_handler(handler)
    bus = DefaultQueryBus(registry=registry, cache_adapter=QueryCacheAdapter(_cache(json)))
    query = handler.get_query_type()()

    caplog.set_level(logging.WARNING)
    results = [await bus.query_with_context(query, CALLER) for _ in range(3)]

    assert handler.calls == 1
    assert [record.getMessage() for record in caplog.records] == []
    if handler_type is not GetPricedHandler or not json:  # a Protocol hit from JSON comes back as stored
        assert results[1] == results[0] and results[2] == results[0]
        assert type(results[-1]) is type(results[0])


async def test_an_entity_result_type_behind_a_generic_base_is_never_cached(caplog: pytest.LogCaptureFixture) -> None:
    handler = ListProductsHandler()
    registry = HandlerRegistry()
    registry.register_query_handler(handler)
    cache = InMemoryCache()
    bus = DefaultQueryBus(registry=registry, cache_adapter=QueryCacheAdapter(cache))
    caplog.set_level(logging.WARNING, logger="pyfly.cqrs.query.bus")
    for _ in range(2):
        await bus.query_with_context(ListProducts(), CALLER)
    assert handler.calls == 2
    assert cache.get_keys() == []
    assert any("CachedProduct" in record.getMessage() for record in caplog.records)
