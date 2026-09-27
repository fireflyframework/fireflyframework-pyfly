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
"""An event-tag eviction that may reach none of a handler's entries is reported.

``@cache_evict(Event)`` on a query handler evicts its group: ``<cache_key_prefix>:``, or ``<QueryClass>:``
for the default query keys. A query that builds its own keys (``get_cache_key()`` overridden) on a handler
without a ``cache_key_prefix`` has entries that need not start with that group, and the eviction then
silently does nothing: the bus says so, once per handler.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

import pytest

from pyfly.cache.adapters.memory import InMemoryCache
from pyfly.cqrs.cache.adapter import QueryCacheAdapter
from pyfly.cqrs.cache.decorators import cache_evict, cacheable
from pyfly.cqrs.command.bus import DefaultCommandBus
from pyfly.cqrs.command.handler import CommandHandler
from pyfly.cqrs.command.registry import HandlerRegistry
from pyfly.cqrs.context.execution_context import ExecutionContextBuilder
from pyfly.cqrs.decorators import command_handler, query_handler
from pyfly.cqrs.query.bus import DefaultQueryBus
from pyfly.cqrs.query.handler import QueryHandler
from pyfly.cqrs.types import Command, Query


@dataclass(frozen=True)
class StockChanged:
    sku: str


@dataclass(frozen=True)
class StockBySku(Query[int]):
    sku: str = ""

    def get_cache_key(self) -> str | None:
        return f"stock:{self.sku}"  # its own key: not under the default "StockBySku:" group


@dataclass(frozen=True)
class StockSummary(Query[int]):
    region: str = ""

    def get_cache_key(self) -> str | None:
        return f"summary:{self.region}"


@cache_evict(StockChanged)
@query_handler(cacheable=True)
class StockBySkuHandler(QueryHandler[StockBySku, int]):
    async def do_handle(self, query: StockBySku) -> int:
        return 3


@cache_evict(StockChanged)
@cacheable(cache_key_prefix="stock-summary")
@query_handler(cacheable=True)
class StockSummaryHandler(QueryHandler[StockSummary, int]):
    calls = 0

    async def do_handle(self, query: StockSummary) -> int:
        type(self).calls += 1
        return 30


@dataclass
class Moved:
    domain_events: list[Any] = field(default_factory=list)


@dataclass(frozen=True)
class MoveStock(Command[Moved]):
    sku: str = ""


@command_handler
class MoveStockHandler(CommandHandler[MoveStock, Moved]):
    async def do_handle(self, command: MoveStock) -> Moved:
        return Moved(domain_events=[StockChanged(command.sku)])


async def test_a_tagged_handler_whose_keys_miss_its_group_is_reported_once(caplog: pytest.LogCaptureFixture) -> None:
    registry = HandlerRegistry()
    for handler in (StockBySkuHandler(), StockSummaryHandler(), MoveStockHandler()):
        if isinstance(handler, QueryHandler):
            registry.register_query_handler(handler)
        else:
            registry.register_command_handler(handler)
    cache = QueryCacheAdapter(InMemoryCache())
    queries = DefaultQueryBus(registry=registry, cache_adapter=cache)
    commands = DefaultCommandBus(registry=registry, query_cache=cache)
    alice = ExecutionContextBuilder().with_tenant_id("acme").with_user_id("alice").build()
    StockSummaryHandler.calls = 0
    for _ in range(2):
        assert await queries.query_with_context(StockSummary(region="eu"), alice) == 30
    assert StockSummaryHandler.calls == 1

    caplog.set_level(logging.WARNING, logger="pyfly.cqrs.command.bus")
    await commands.send(MoveStock(sku="A"))
    await commands.send(MoveStock(sku="B"))

    warnings = [record.getMessage() for record in caplog.records]
    assert len(warnings) == 1
    assert "StockBySkuHandler" in warnings[0] and "cache_key_prefix" in warnings[0]
    assert await queries.query_with_context(StockSummary(region="eu"), alice) == 30
    assert StockSummaryHandler.calls == 2  # the prefixed group was evicted
