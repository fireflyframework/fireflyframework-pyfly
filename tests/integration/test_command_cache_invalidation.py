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
"""Command-side invalidation of the CQRS query cache, after the commit (C105).

``Command.get_cache_key()``, ``@cache_evict(events)``, ``cache_key_prefix`` and
``pyfly.cqrs.query.caching_enabled`` used to be read by nothing: after a committed update the query kept
returning the old row for ``cache_ttl``. A real ``ApplicationContext`` (relational, cache and CQRS
auto-configuration) on a SQLite file database (foreign keys on) and on PostgreSQL shows:

- the command bus evicts the command's ``get_cache_key()`` once the command's unit of work commits, for
  every tenant and user, and not at all when it rolls back;
- ``@cache_evict(Event)`` on a query handler evicts all of its entries when a command produces that event,
  and the same tag on a command handler evicts them whenever that command commits;
- ``cache_key_prefix`` is part of the key, and ``caching_enabled: false`` turns the query cache off;
- a domain event that fails to publish (``EventFailureStrategy.RAISE``) after the handler committed does
  not leave the committed change's old results cached.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest
from sqlalchemy import Identity, Integer, String, update
from sqlalchemy.orm import Mapped, mapped_column

from pyfly.cache.auto_configuration import CacheAutoConfiguration
from pyfly.cache.ports.outbound import CacheAdapter
from pyfly.container.stereotypes import repository, service
from pyfly.context.application_context import ApplicationContext
from pyfly.cqrs.cache.adapter import QueryCacheAdapter
from pyfly.cqrs.cache.decorators import cache_evict, cacheable
from pyfly.cqrs.command.bus import DefaultCommandBus, EventFailureStrategy
from pyfly.cqrs.command.handler import CommandHandler
from pyfly.cqrs.command.registry import HandlerRegistry
from pyfly.cqrs.config.auto_configuration import CqrsAutoConfiguration
from pyfly.cqrs.context.execution_context import ExecutionContext, ExecutionContextBuilder
from pyfly.cqrs.decorators import command_handler, query_handler
from pyfly.cqrs.exceptions import CommandProcessingException
from pyfly.cqrs.query.bus import DefaultQueryBus
from pyfly.cqrs.query.handler import QueryHandler
from pyfly.cqrs.types import Command, Query
from pyfly.data.relational.auto_configuration import RelationalAutoConfiguration
from pyfly.data.relational.sqlalchemy.entity import Base
from pyfly.data.relational.sqlalchemy.repository import Repository
from pyfly.data.transaction import transactional
from tests.support.backend_matrix import PG, SQLITE_FILE, RelationalBackend

pytestmark = pytest.mark.backends(SQLITE_FILE, PG)


class CqrsOrder(Base):
    __tablename__ = "wp14_cqrs_order"

    id: Mapped[int] = mapped_column(Integer, Identity(), primary_key=True)
    status: Mapped[str] = mapped_column(String(32))


@repository
class CqrsOrderRepository(Repository[CqrsOrder, int]):
    pass


# -- queries --------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class GetOrderQuery(Query[dict[str, Any]]):
    order_id: int = 0


@dataclass(frozen=True)
class OrderStatusQuery(Query[str]):
    order_id: int = 0


@dataclass(frozen=True)
class OrderRenamed:
    order_id: int


@query_handler(cacheable=True)
class GetOrderHandler(QueryHandler[GetOrderQuery, dict[str, Any]]):
    def __init__(self, orders: CqrsOrderRepository) -> None:
        super().__init__()
        self.orders = orders
        self.calls = 0

    async def do_handle(self, query: GetOrderQuery) -> dict[str, Any]:
        self.calls += 1
        order = await self.orders.find_by_id(query.order_id)
        assert order is not None
        return {"id": order.id, "status": order.status}


@cache_evict(OrderRenamed)
@cacheable(cache_key_prefix="order-status")
@query_handler(cacheable=True)
class OrderStatusHandler(QueryHandler[OrderStatusQuery, str]):
    def __init__(self, orders: CqrsOrderRepository) -> None:
        super().__init__()
        self.orders = orders
        self.calls = 0

    async def do_handle(self, query: OrderStatusQuery) -> str:
        self.calls += 1
        order = await self.orders.find_by_id(query.order_id)
        assert order is not None
        return order.status


# -- commands -------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class ShipOrder(Command[None]):
    order_id: int = 0
    fail: bool = False

    def get_cache_key(self) -> str | None:
        return GetOrderQuery(order_id=self.order_id).get_cache_key()


@command_handler
class ShipOrderHandler(CommandHandler[ShipOrder, None]):
    def __init__(self, orders: CqrsOrderRepository) -> None:
        super().__init__()
        self.orders = orders

    @transactional
    async def do_handle(self, command: ShipOrder) -> None:
        await self.orders._session.execute(
            update(CqrsOrder).where(CqrsOrder.id == command.order_id).values(status="SHIPPED")
        )
        if command.fail:
            raise RuntimeError("the carrier refused the parcel")


@dataclass
class RenameResult:
    domain_events: list[Any] = field(default_factory=list)


@dataclass(frozen=True)
class RenameOrder(Command[RenameResult]):
    order_id: int = 0
    status: str = ""


@command_handler
class RenameOrderHandler(CommandHandler[RenameOrder, RenameResult]):
    def __init__(self, orders: CqrsOrderRepository) -> None:
        super().__init__()
        self.orders = orders

    @transactional
    async def do_handle(self, command: RenameOrder) -> RenameResult:
        await self.orders._session.execute(
            update(CqrsOrder).where(CqrsOrder.id == command.order_id).values(status=command.status)
        )
        return RenameResult(domain_events=[OrderRenamed(command.order_id)])


@dataclass(frozen=True)
class ArchiveOrder(Command[None]):
    order_id: int = 0


@cache_evict(OrderRenamed)
@command_handler
class ArchiveOrderHandler(CommandHandler[ArchiveOrder, None]):
    def __init__(self, orders: CqrsOrderRepository) -> None:
        super().__init__()
        self.orders = orders

    @transactional
    async def do_handle(self, command: ArchiveOrder) -> None:
        await self.orders._session.execute(
            update(CqrsOrder).where(CqrsOrder.id == command.order_id).values(status="ARCHIVED")
        )


@service
class Fulfillment:
    """Sends a command inside a wider unit of work."""

    def __init__(self, commands: DefaultCommandBus, cache: CacheAdapter) -> None:
        self.commands = commands
        self.cache = cache
        self.seen_inside: list[bool] = []

    @transactional
    async def ship_and_then(self, order_id: int, *, fail: bool) -> None:
        await self.commands.send(ShipOrder(order_id=order_id))
        # The eviction waits for this unit's commit: until then the committed row is what is cached.
        self.seen_inside.append(bool([k for k in await _keys(self.cache) if "GetOrderQuery" in k]))
        if fail:
            raise RuntimeError("invoicing failed after the order shipped")


async def _keys(cache: CacheAdapter) -> list[str]:
    get_keys: Any = getattr(cache, "get_keys")  # noqa: B009 — InMemoryCache's synchronous helper
    return list(get_keys())


_BEANS = (
    RelationalAutoConfiguration,
    CacheAutoConfiguration,
    CqrsAutoConfiguration,
    CqrsOrderRepository,
    GetOrderHandler,
    OrderStatusHandler,
    ShipOrderHandler,
    RenameOrderHandler,
    ArchiveOrderHandler,
    Fulfillment,
)


async def _boot(backend: RelationalBackend, overrides: dict[str, Any] | None = None) -> ApplicationContext:
    await backend.create_tables(CqrsOrder)
    config = {
        "pyfly.cqrs.enabled": "true",
        "pyfly.cache.enabled": "true",
        "pyfly.cache.provider": "memory",
        **(overrides or {}),
    }
    ctx = ApplicationContext(backend.config(config))
    for bean in _BEANS:
        ctx.register_bean(bean)
    await ctx.start()
    await ctx.get_bean(CqrsOrderRepository).save(CqrsOrder(status="PENDING"))
    return ctx


def _as(tenant: str, user: str) -> ExecutionContext:
    return ExecutionContextBuilder().with_tenant_id(tenant).with_user_id(user).build()


async def test_a_committed_command_evicts_its_cache_key_for_everyone(relational_backend: RelationalBackend) -> None:
    ctx = await _boot(relational_backend)
    try:
        queries = ctx.get_bean(DefaultQueryBus)
        handler = ctx.get_bean(GetOrderHandler)
        for who in (_as("acme", "alice"), _as("acme", "bob")):
            assert await queries.query_with_context(GetOrderQuery(order_id=1), who) == {"id": 1, "status": "PENDING"}
        assert handler.calls == 2

        await ctx.get_bean(DefaultCommandBus).send(ShipOrder(order_id=1))
        for who in (_as("acme", "alice"), _as("acme", "bob")):
            assert await queries.query_with_context(GetOrderQuery(order_id=1), who) == {"id": 1, "status": "SHIPPED"}
        assert handler.calls == 4
    finally:
        await ctx.stop()


async def test_a_failed_command_evicts_nothing(relational_backend: RelationalBackend) -> None:
    ctx = await _boot(relational_backend)
    try:
        queries = ctx.get_bean(DefaultQueryBus)
        assert await queries.query(GetOrderQuery(order_id=1)) == {"id": 1, "status": "PENDING"}
        with pytest.raises(CommandProcessingException, match="carrier refused"):
            await ctx.get_bean(DefaultCommandBus).send(ShipOrder(order_id=1, fail=True))
        assert await queries.query(GetOrderQuery(order_id=1)) == {"id": 1, "status": "PENDING"}
        assert ctx.get_bean(GetOrderHandler).calls == 1  # still cached, and still true
    finally:
        await ctx.stop()


async def test_inside_a_wider_unit_the_eviction_waits_for_its_commit(relational_backend: RelationalBackend) -> None:
    ctx = await _boot(relational_backend)
    try:
        queries = ctx.get_bean(DefaultQueryBus)
        fulfillment = ctx.get_bean(Fulfillment)
        assert await queries.query(GetOrderQuery(order_id=1)) == {"id": 1, "status": "PENDING"}

        with pytest.raises(RuntimeError, match="invoicing failed"):
            await fulfillment.ship_and_then(1, fail=True)
        assert await queries.query(GetOrderQuery(order_id=1)) == {"id": 1, "status": "PENDING"}

        await fulfillment.ship_and_then(1, fail=False)
        assert fulfillment.seen_inside == [True, True]
        assert await queries.query(GetOrderQuery(order_id=1)) == {"id": 1, "status": "SHIPPED"}
        assert ctx.get_bean(GetOrderHandler).calls == 2
    finally:
        await ctx.stop()


async def test_an_event_evicts_every_entry_of_the_handlers_tagged_with_it(
    relational_backend: RelationalBackend,
) -> None:
    ctx = await _boot(relational_backend)
    try:
        queries = ctx.get_bean(DefaultQueryBus)
        handler = ctx.get_bean(OrderStatusHandler)
        for who in (_as("acme", "alice"), _as("globex", "carol")):
            assert await queries.query_with_context(OrderStatusQuery(order_id=1), who) == "PENDING"
        cache = ctx.get_bean(CacheAdapter)
        assert all(":cqrs:order-status:OrderStatusQuery:" in key for key in await _keys(cache))  # prefix applied

        await ctx.get_bean(DefaultCommandBus).send(RenameOrder(order_id=1, status="ON_HOLD"))
        for who in (_as("acme", "alice"), _as("globex", "carol")):
            assert await queries.query_with_context(OrderStatusQuery(order_id=1), who) == "ON_HOLD"
        assert handler.calls == 4

        # The same tag on a command handler: that command stands for the event, whatever it returns.
        await ctx.get_bean(DefaultCommandBus).send(ArchiveOrder(order_id=1))
        assert await queries.query_with_context(OrderStatusQuery(order_id=1), _as("acme", "alice")) == "ARCHIVED"
        assert handler.calls == 5
    finally:
        await ctx.stop()


async def test_the_query_cache_can_be_switched_off(relational_backend: RelationalBackend) -> None:
    ctx = await _boot(relational_backend, {"pyfly.cqrs.query.caching_enabled": "false"})
    try:
        queries = ctx.get_bean(DefaultQueryBus)
        for _ in range(3):
            assert await queries.query(GetOrderQuery(order_id=1)) == {"id": 1, "status": "PENDING"}
        assert ctx.get_bean(GetOrderHandler).calls == 3
        assert await _keys(ctx.get_bean(CacheAdapter)) == []
    finally:
        await ctx.stop()


class _BrokerDown:
    async def publish(self, event: Any, *, destination: str | None = None) -> None:
        raise ConnectionError("the broker is unreachable")


async def test_a_failed_publication_after_the_commit_still_evicts(relational_backend: RelationalBackend) -> None:
    ctx = await _boot(relational_backend)
    try:
        queries = ctx.get_bean(DefaultQueryBus)
        commands = DefaultCommandBus(
            registry=ctx.get_bean(HandlerRegistry),
            event_publisher=_BrokerDown(),
            event_failure_strategy=EventFailureStrategy.RAISE,
            query_cache=ctx.get_bean(QueryCacheAdapter),
        )
        assert await queries.query(OrderStatusQuery(order_id=1)) == "PENDING"

        # The handler's own unit committed; only the publication of OrderRenamed failed.
        with pytest.raises(CommandProcessingException, match="failed to publish"):
            await commands.send(RenameOrder(order_id=1, status="ON_HOLD"))
        assert await queries.query(OrderStatusQuery(order_id=1)) == "ON_HOLD"
        assert ctx.get_bean(OrderStatusHandler).calls == 2
    finally:
        await ctx.stop()
