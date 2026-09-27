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
"""Cache writes after a commit that a cancellation lands on (WP14: C023, C105).

A client disconnect (Starlette cancels the request's anyio scope), ``anyio.move_on_after`` or a closing SSE
stream can cancel a task just as a ``@transactional`` body returns. The unit still commits (the commit is
shielded), and an anyio cancel scope then cancels every await that follows: the network round trip of the
cache writes that waited for the commit. An eviction lost there leaves the old value cached for its whole
TTL, or forever without one, although the database committed the new one.

A real ``ApplicationContext`` on a SQLite file database (foreign keys on) and on PostgreSQL, a cache whose
writes await like a Redis round trip, and the cancellation delivered through an ``anyio.CancelScope``:

- every ``after_commit``/``after_completion`` callback of a cancelled boundary runs after its commit, the
  deferred cache writes (evictions, puts, the command bus's query-cache invalidation) included, and the
  cancellation is still delivered afterwards;
- so do the evictions a ``@cache_evict`` method or the command bus makes outside a unit of work, after
  the method's own auto units or ``@transactional`` handler committed.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

import anyio
import pytest
from sqlalchemy import Identity, Integer, String, text, update
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.pool import NullPool

from pyfly.cache.adapters.memory import InMemoryCache
from pyfly.cache.decorators import cache_evict, cache_put, cacheable
from pyfly.container.stereotypes import repository, service
from pyfly.context.application_context import ApplicationContext
from pyfly.cqrs.cache.adapter import QueryCacheAdapter
from pyfly.cqrs.command.bus import DefaultCommandBus
from pyfly.cqrs.command.handler import CommandHandler
from pyfly.cqrs.command.registry import HandlerRegistry
from pyfly.cqrs.context.execution_context import ExecutionContextBuilder
from pyfly.cqrs.decorators import command_handler, query_handler
from pyfly.cqrs.query.bus import DefaultQueryBus
from pyfly.cqrs.query.handler import QueryHandler
from pyfly.cqrs.types import Command, Query
from pyfly.data.relational.auto_configuration import RelationalAutoConfiguration
from pyfly.data.relational.sqlalchemy.entity import Base
from pyfly.data.relational.sqlalchemy.repository import Repository
from pyfly.data.transaction import TransactionPhase, after_commit, on_phase, transactional
from tests.support.backend_matrix import PG, SQLITE_FILE, RelationalBackend

pytestmark = pytest.mark.backends(SQLITE_FILE, PG)


class NetworkCache(InMemoryCache):
    """An in-memory cache whose writes take a network round trip, as Redis's do."""

    async def put(self, key: str, value: Any, ttl: timedelta | None = None) -> None:
        await asyncio.sleep(0.005)
        await super().put(key, value, ttl=ttl)

    async def evict(self, key: str) -> bool:
        await asyncio.sleep(0.005)
        return await super().evict(key)

    async def evict_by_prefix(self, prefix: str) -> int:
        await asyncio.sleep(0.005)
        return await super().evict_by_prefix(prefix)


CACHE = NetworkCache()


class CancelPrice(Base):
    __tablename__ = "wp14_cancel_price"

    id: Mapped[int] = mapped_column(Integer, Identity(), primary_key=True)
    amount: Mapped[int] = mapped_column(Integer)
    label: Mapped[str] = mapped_column(String(64))


@repository
class CancelPriceRepository(Repository[CancelPrice, int]):
    pass


@service
class PriceBook:
    def __init__(self, prices: CancelPriceRepository) -> None:
        self.prices = prices
        self.on_return: Callable[[], None] = lambda: None
        self.reads = 0

    @cacheable(CACHE, key="price:{price_id}")
    async def amount(self, price_id: int) -> int:
        self.reads += 1
        price = await self.prices.find_by_id(price_id)
        assert price is not None
        return price.amount

    @cache_evict(CACHE, key="price:{price_id}")
    async def reprice(self, price_id: int, amount: int) -> None:
        await self.prices._session.execute(update(CancelPrice).where(CancelPrice.id == price_id).values(amount=amount))

    @cache_put(CACHE, key="label:{price_id}")
    async def relabel(self, price_id: int, label: str) -> str:
        await self.prices._session.execute(update(CancelPrice).where(CancelPrice.id == price_id).values(label=label))
        return label

    @transactional
    async def reprice_both(self, amount: int, label: str) -> None:
        await self.reprice(1, amount)
        await self.relabel(1, label)
        await self.reprice(2, amount)
        self.on_return()  # the client disconnects as the body returns

    @transactional
    async def reprice_then_notify(self, amount: int, notify: Callable[[str], Awaitable[None]]) -> None:
        await self.prices._session.execute(update(CancelPrice).where(CancelPrice.id == 1).values(amount=amount))
        await after_commit(lambda: notify("first"))
        await after_commit(lambda: notify("second"))
        await on_phase(TransactionPhase.AFTER_COMPLETION, lambda: notify("completed"))
        self.on_return()

    @cache_evict(CACHE, key="price:{price_id}")
    async def reprice_in_its_own_unit(self, price_id: int, amount: int) -> None:
        price = await self.prices.find_by_id(price_id)
        assert price is not None
        price.amount = amount
        await self.prices.save(price)  # an auto unit that commits
        self.on_return()


# -- CQRS -----------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class GetPriceQuery(Query[int]):
    price_id: int = 0


@dataclass(frozen=True)
class SetPrice(Command[None]):
    price_id: int = 0
    amount: int = 0

    def get_cache_key(self) -> str | None:
        return GetPriceQuery(price_id=self.price_id).get_cache_key()


@query_handler(cacheable=True)
class GetPriceHandler(QueryHandler[GetPriceQuery, int]):
    def __init__(self, prices: CancelPriceRepository) -> None:
        super().__init__()
        self.prices = prices
        self.calls = 0

    async def do_handle(self, query: GetPriceQuery) -> int:
        self.calls += 1
        price = await self.prices.find_by_id(query.price_id)
        assert price is not None
        return price.amount


@command_handler
class SetPriceHandler(CommandHandler[SetPrice, None]):
    def __init__(self, prices: CancelPriceRepository) -> None:
        super().__init__()
        self.prices = prices
        self.on_return: Callable[[], None] = lambda: None

    @transactional
    async def do_handle(self, command: SetPrice) -> None:
        await self.prices._session.execute(
            update(CancelPrice).where(CancelPrice.id == command.price_id).values(amount=command.amount)
        )
        self.on_return()


@service
class Pricing:
    """Sends the command inside a wider unit of work: the bus's evictions wait for its commit."""

    def __init__(self) -> None:
        self.commands: DefaultCommandBus | None = None
        self.on_return: Callable[[], None] = lambda: None

    @transactional
    async def set_price(self, price_id: int, amount: int) -> None:
        assert self.commands is not None
        await self.commands.send(SetPrice(price_id=price_id, amount=amount))
        self.on_return()


_BEANS = (RelationalAutoConfiguration, CancelPriceRepository, PriceBook, Pricing)


async def _boot(backend: RelationalBackend) -> ApplicationContext:
    await backend.create_tables(CancelPrice)
    await CACHE.clear()
    ctx = ApplicationContext(backend.config())
    for bean in _BEANS:
        ctx.register_bean(bean)
    await ctx.start()
    engine = create_async_engine(backend.url, poolclass=NullPool)
    try:
        async with engine.begin() as conn:
            for label in ("first", "second"):
                await conn.execute(text("INSERT INTO wp14_cancel_price (amount, label) VALUES (10, :l)"), {"l": label})
    finally:
        await engine.dispose()
    return ctx


async def _committed(backend: RelationalBackend, column: str) -> list[Any]:
    engine = create_async_engine(backend.url, poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            rows = await conn.execute(text(f"SELECT {column} FROM wp14_cancel_price ORDER BY id"))
            return [row[0] for row in rows.all()]
    finally:
        await engine.dispose()


def _cqrs(ctx: ApplicationContext) -> tuple[DefaultCommandBus, DefaultQueryBus, GetPriceHandler, SetPriceHandler]:
    prices = ctx.get_bean(CancelPriceRepository)
    registry = HandlerRegistry()
    queries, commands = GetPriceHandler(prices), SetPriceHandler(prices)
    registry.register_query_handler(queries)
    registry.register_command_handler(commands)
    cache = QueryCacheAdapter(CACHE)
    return (
        DefaultCommandBus(registry=registry, query_cache=cache),
        DefaultQueryBus(registry=registry, cache_adapter=cache),
        queries,
        commands,
    )


async def test_every_synchronization_callback_runs_when_a_cancellation_lands_at_commit(
    relational_backend: RelationalBackend,
) -> None:
    ctx = await _boot(relational_backend)
    try:
        book = ctx.get_bean(PriceBook)
        notified: list[str] = []

        async def notify(what: str) -> None:
            await asyncio.sleep(0.005)  # a broker or cache round trip
            notified.append(what)

        with anyio.CancelScope() as scope:
            book.on_return = scope.cancel
            await book.reprice_then_notify(15, notify)
            await anyio.sleep(0)
        assert scope.cancelled_caught

        assert await _committed(relational_backend, "amount") == [15, 10]
        assert notified == ["first", "second", "completed"]
    finally:
        await ctx.stop()


async def test_every_deferred_write_runs_when_a_cancellation_lands_at_commit(
    relational_backend: RelationalBackend,
) -> None:
    ctx = await _boot(relational_backend)
    try:
        book = ctx.get_bean(PriceBook)
        assert [await book.amount(1), await book.amount(2)] == [10, 10]

        with anyio.CancelScope() as scope:
            book.on_return = scope.cancel
            await book.reprice_both(25, "sale")
            await anyio.sleep(0)
        assert scope.cancelled_caught  # the cancellation is still delivered, after the writes

        assert await _committed(relational_backend, "amount") == [25, 25]
        assert await CACHE.exists("price:1") is False
        assert await CACHE.exists("price:2") is False
        assert await CACHE.get("label:1") == "sale"
        book.on_return = lambda: None
        assert [await book.amount(1), await book.amount(2)] == [25, 25]
        assert book.reads == 4
    finally:
        await ctx.stop()


async def test_a_task_cancelled_as_it_commits_runs_every_deferred_write_and_stays_cancelled(
    relational_backend: RelationalBackend,
) -> None:
    ctx = await _boot(relational_backend)
    try:
        book = ctx.get_bean(PriceBook)
        assert [await book.amount(1), await book.amount(2)] == [10, 10]

        def cancel_this_task() -> None:
            task = asyncio.current_task()
            assert task is not None
            task.cancel()  # asyncio's own cancellation: delivered once, at the next await (the commit)

        book.on_return = cancel_this_task
        task = asyncio.create_task(book.reprice_both(35, "clearance"))
        with pytest.raises(asyncio.CancelledError):
            await task
        assert task.cancelled()

        assert await _committed(relational_backend, "amount") == [35, 35]
        assert [await CACHE.exists("price:1"), await CACHE.exists("price:2")] == [False, False]
        assert await CACHE.get("label:1") == "clearance"
    finally:
        await ctx.stop()


async def test_an_eviction_after_the_method_committed_on_its_own_survives_a_cancellation(
    relational_backend: RelationalBackend,
) -> None:
    ctx = await _boot(relational_backend)
    try:
        book = ctx.get_bean(PriceBook)
        assert await book.amount(1) == 10

        with anyio.CancelScope() as scope:
            book.on_return = scope.cancel
            await book.reprice_in_its_own_unit(1, 30)
            await anyio.sleep(0)
        assert scope.cancelled_caught

        assert await _committed(relational_backend, "amount") == [30, 10]
        assert await CACHE.exists("price:1") is False
        book.on_return = lambda: None
        assert await book.amount(1) == 30
    finally:
        await ctx.stop()


async def test_the_command_bus_invalidation_survives_a_cancellation_at_commit(
    relational_backend: RelationalBackend,
) -> None:
    ctx = await _boot(relational_backend)
    try:
        commands, queries, handler, _ = _cqrs(ctx)
        pricing = ctx.get_bean(Pricing)
        pricing.commands = commands
        caller = ExecutionContextBuilder().with_tenant_id("acme").with_user_id("alice").build()
        assert await queries.query_with_context(GetPriceQuery(price_id=1), caller) == 10

        with anyio.CancelScope() as scope:
            pricing.on_return = scope.cancel
            await pricing.set_price(1, 40)
            await anyio.sleep(0)
        assert scope.cancelled_caught

        assert await _committed(relational_backend, "amount") == [40, 10]
        assert await queries.query_with_context(GetPriceQuery(price_id=1), caller) == 40
        assert handler.calls == 2
    finally:
        await ctx.stop()


async def test_the_command_bus_invalidation_after_a_handlers_own_commit_survives_a_cancellation(
    relational_backend: RelationalBackend,
) -> None:
    ctx = await _boot(relational_backend)
    try:
        commands, queries, handler, command_handler_ = _cqrs(ctx)
        caller = ExecutionContextBuilder().with_tenant_id("acme").with_user_id("alice").build()
        assert await queries.query_with_context(GetPriceQuery(price_id=1), caller) == 10

        with anyio.CancelScope() as scope:
            command_handler_.on_return = scope.cancel
            await commands.send(SetPrice(price_id=1, amount=50))
            await anyio.sleep(0)
        assert scope.cancelled_caught

        assert await _committed(relational_backend, "amount") == [50, 10]
        assert await queries.query_with_context(GetPriceQuery(price_id=1), caller) == 50
        assert handler.calls == 2
    finally:
        await ctx.stop()
