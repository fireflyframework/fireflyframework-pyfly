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
"""A query-bus hit from a JSON cache comes back as the handler's result type (C025).

The Redis and PostgreSQL caches store what the JSON encoder writes: field names, never aliases or
computed fields. A result type with camelCase aliases (``alias_generator``), a ``Field(alias=...)`` or a
computed field on an ``extra="forbid"`` model must still validate from it, or every query is a miss that
runs the handler again, rewrites the entry and logs a WARNING.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import pytest
from pydantic import BaseModel

from pyfly.cache.adapters.redis import RedisCacheAdapter
from pyfly.cqrs.cache.adapter import QueryCacheAdapter
from pyfly.cqrs.command.registry import HandlerRegistry
from pyfly.cqrs.context.execution_context import ExecutionContextBuilder
from pyfly.cqrs.decorators import query_handler
from pyfly.cqrs.query.bus import DefaultQueryBus
from pyfly.cqrs.query.handler import QueryHandler
from pyfly.cqrs.types import Query
from tests.cache.test_cache import AliasedOrderDto, CamelDto, PricedLineDto, RedisBytesStub


@dataclass(frozen=True)
class GetUser(Query[CamelDto]):
    user_id: int = 0


@dataclass(frozen=True)
class GetOrder(Query[AliasedOrderDto]):
    number: str = ""


@dataclass(frozen=True)
class GetLine(Query[PricedLineDto]):
    quantity: int = 0


@query_handler(cacheable=True)
class GetUserHandler(QueryHandler[GetUser, CamelDto]):
    def __init__(self) -> None:
        super().__init__()
        self.calls = 0

    async def do_handle(self, query: GetUser) -> CamelDto:
        self.calls += 1
        return CamelDto(userId=query.user_id, displayName="Ada")


@query_handler(cacheable=True)
class GetOrderHandler(QueryHandler[GetOrder, AliasedOrderDto]):
    def __init__(self) -> None:
        super().__init__()
        self.calls = 0

    async def do_handle(self, query: GetOrder) -> AliasedOrderDto:
        self.calls += 1
        return AliasedOrderDto(orderNo=query.number)


@query_handler(cacheable=True)
class GetLineHandler(QueryHandler[GetLine, PricedLineDto]):
    def __init__(self) -> None:
        super().__init__()
        self.calls = 0

    async def do_handle(self, query: GetLine) -> PricedLineDto:
        self.calls += 1
        return PricedLineDto(quantity=query.quantity, unit_price=Decimal("2.50"))


@pytest.mark.parametrize(
    ("handler_type", "query"),
    [
        pytest.param(GetUserHandler, GetUser(user_id=7), id="alias_generator"),
        pytest.param(GetOrderHandler, GetOrder(number="A-1"), id="field_alias"),
        pytest.param(GetLineHandler, GetLine(quantity=3), id="computed_field"),
    ],
)
@pytest.mark.parametrize("with_context", [False, True], ids=["unscoped", "scoped"])
async def test_a_json_hit_of_an_aliased_or_computed_dto_is_served(
    handler_type: type[Any], query: Query[Any], with_context: bool, caplog: pytest.LogCaptureFixture
) -> None:
    registry = HandlerRegistry()
    handler = handler_type()
    registry.register_query_handler(handler)
    bus = DefaultQueryBus(registry=registry, cache_adapter=QueryCacheAdapter(RedisCacheAdapter(RedisBytesStub())))
    context = ExecutionContextBuilder().with_tenant_id("acme").with_user_id("alice").build()

    caplog.set_level(logging.WARNING)
    results = [await (bus.query_with_context(query, context) if with_context else bus.query(query)) for _ in range(3)]

    assert handler.calls == 1
    assert results[1] == results[0] and results[2] == results[0]
    assert isinstance(results[-1], BaseModel) and type(results[-1]) is type(results[0])
    assert [record.getMessage() for record in caplog.records] == []


@dataclass(frozen=True)
class GetCode(Query[int]):
    name: str = ""


@query_handler(cacheable=True)
class MisannotatedHandler(QueryHandler[GetCode, int]):
    """Declares ``int`` and returns a string that is not one: its entries never fit."""

    def __init__(self) -> None:
        super().__init__()
        self.calls = 0

    async def do_handle(self, query: GetCode) -> Any:
        self.calls += 1
        return "not a number"


async def test_an_entry_that_does_not_fit_is_a_miss_with_one_warning(caplog: pytest.LogCaptureFixture) -> None:
    registry = HandlerRegistry()
    handler = MisannotatedHandler()
    registry.register_query_handler(handler)
    bus = DefaultQueryBus(registry=registry, cache_adapter=QueryCacheAdapter(RedisCacheAdapter(RedisBytesStub())))

    caplog.set_level(logging.WARNING, logger="pyfly.cqrs.query.bus")
    for _ in range(3):
        assert await bus.query(GetCode(name="x")) == "not a number"

    assert handler.calls == 3
    warnings = [record.getMessage() for record in caplog.records]
    assert len(warnings) == 1
    assert "MisannotatedHandler" in warnings[0]
