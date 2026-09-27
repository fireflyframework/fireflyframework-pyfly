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
"""The immediate operations of a transaction-aware cache never run inside the caller's unit (WP14).

A database-backed cache that follows the framework-adapter contract runs every statement through
``infrastructure_unit()``: it joins the unit bound for its datasource, and opens a short one otherwise.
:class:`JoiningSqlCache` is such a cache, over a table on the application's primary datasource, and it
records the unit each of its statements ran on.

``TransactionAwareCache`` defers ``put``, ``evict``, ``evict_by_prefix`` and ``clear`` to the commit, and runs
everything else at once: reads, ``put_if_absent`` (the CQRS query cache starts a key's generation with it),
``evict_if_present``, ``invalidate``, and the writes of a task that outlived its unit. Run inside the caller's
unit, those would be rolled back with it, take row locks a concurrent plain query waits for, deadlock two
business units against each other, or fail on a unit that already completed. So they run outside it
(``pyfly.data.transaction.outside_transaction``), each statement in a short unit of its own:

- the caller's rollback keeps what an immediate operation did (a read-only unit on every backend, a write
  unit on PostgreSQL);
- a plain query does not wait for another request's business unit, and two business units that run two
  cacheable queries in opposite orders both commit (PostgreSQL);
- the eviction and the put of a task that outlived its committed unit reach the store;
- deferred writes still wait for the commit, and a rollback drops them.

SQLite has one writer, and a write unit holds it from its ``BEGIN IMMEDIATE``: an immediate write to a cache
on the database of the caller's write unit is refused at once (``IllegalTransactionStateError``), not after
``busy_timeout``, and the CQRS query cache logs that and answers the query uncached.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import Column, Delete, MetaData, String, Table, Text, delete, select
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from pyfly.cache.decorators import cache_evict
from pyfly.cache.transaction import TransactionAwareCache
from pyfly.context.application_context import ApplicationContext
from pyfly.cqrs.cache.adapter import QueryCacheAdapter
from pyfly.cqrs.command.registry import HandlerRegistry
from pyfly.cqrs.context.execution_context import ExecutionContext, ExecutionContextBuilder
from pyfly.cqrs.decorators import query_handler
from pyfly.cqrs.query.bus import DefaultQueryBus
from pyfly.cqrs.query.handler import QueryHandler
from pyfly.cqrs.types import Query
from pyfly.data.relational.auto_configuration import RelationalAutoConfiguration
from pyfly.data.transaction import (
    IllegalTransactionStateError,
    TransactionTemplate,
    UnitOfWork,
    current_unit_of_work,
    infrastructure_unit,
    transactional,
)
from tests.support.backend_matrix import PG, SQLITE_FILE, RelationalBackend

pytestmark = pytest.mark.backends(SQLITE_FILE, PG)

CACHE_TABLE = Table(
    "wp14_joining_cache",
    MetaData(),
    Column("cache_key", String(255), primary_key=True),
    Column("value", Text, nullable=False),
)


class Rollback(Exception):
    """Raised to roll the business unit back."""


class JoiningSqlCache:
    """A ``CacheAdapter`` over :data:`CACHE_TABLE` whose every statement runs through ``infrastructure_unit()``.

    It joins the unit bound for its datasource and opens a short one otherwise, like a database-backed cache
    that keeps the framework-adapter contract. :attr:`units` is the unit each statement ran on. Values are
    JSON; TTLs are ignored.
    """

    def __init__(self, datasource: str = "primary") -> None:
        self._datasource = datasource
        self.units: list[UnitOfWork] = []

    async def _run(self, build: Callable[[str], Any], read: Callable[[CursorResult[Any]], Any], *, reads: bool) -> Any:
        async with infrastructure_unit(self._datasource, read_only=reads, single_statement=True) as session:
            unit = current_unit_of_work(self._datasource)
            assert unit is not None
            self.units.append(unit)
            return read(await session.execute(build(session.get_bind().dialect.name)))

    @staticmethod
    def _insert(dialect: str, key: str, value: Any) -> Any:
        from sqlalchemy.dialects import postgresql, sqlite

        insert = postgresql.insert if dialect == "postgresql" else sqlite.insert
        return insert(CACHE_TABLE).values(cache_key=key, value=json.dumps(value))

    async def _delete(self, statement: Delete) -> int:
        return int(await self._run(lambda _dialect: statement, lambda result: result.rowcount, reads=False))

    async def get(self, key: str) -> Any | None:
        raw = await self._run(
            lambda _dialect: select(CACHE_TABLE.c.value).where(CACHE_TABLE.c.cache_key == key),
            lambda result: result.scalar_one_or_none(),
            reads=True,
        )
        return None if raw is None else json.loads(raw)

    async def exists(self, key: str) -> bool:
        found = await self._run(
            lambda _dialect: select(CACHE_TABLE.c.cache_key).where(CACHE_TABLE.c.cache_key == key),
            lambda result: result.scalar_one_or_none(),
            reads=True,
        )
        return found is not None

    async def put(self, key: str, value: Any, ttl: timedelta | None = None) -> None:
        def upsert(dialect: str) -> Any:
            statement = self._insert(dialect, key, value)
            return statement.on_conflict_do_update(
                index_elements=[CACHE_TABLE.c.cache_key], set_={"value": statement.excluded.value}
            )

        await self._run(upsert, lambda _result: None, reads=False)

    async def put_if_absent(self, key: str, value: Any, ttl: timedelta | None = None) -> bool:
        stored = await self._run(
            lambda dialect: self._insert(dialect, key, value).on_conflict_do_nothing(
                index_elements=[CACHE_TABLE.c.cache_key]
            ),
            lambda result: result.rowcount,
            reads=False,
        )
        return bool(stored)

    async def evict(self, key: str) -> bool:
        return await self._delete(delete(CACHE_TABLE).where(CACHE_TABLE.c.cache_key == key)) > 0

    async def evict_by_prefix(self, prefix: str) -> int:
        matching = CACHE_TABLE.c.cache_key.startswith(prefix, autoescape=True)
        return await self._delete(delete(CACHE_TABLE).where(matching))

    async def clear(self) -> None:
        await self._delete(delete(CACHE_TABLE))

    async def start(self) -> None:
        pass

    async def stop(self) -> None:
        pass


@dataclass(frozen=True)
class PriceQuery(Query[int]):
    sku: str = "a"


@dataclass(frozen=True)
class StockQuery(Query[int]):
    sku: str = "a"


@query_handler(cacheable=True)
class PriceHandler(QueryHandler[PriceQuery, int]):
    def __init__(self) -> None:
        super().__init__()
        self.calls = 0

    async def do_handle(self, query: PriceQuery) -> int:
        self.calls += 1
        return 42


@query_handler(cacheable=True)
class StockHandler(QueryHandler[StockQuery, int]):
    async def do_handle(self, query: StockQuery) -> int:
        return 7


ALICE = ExecutionContextBuilder().with_tenant_id("acme").with_user_id("alice").build()
BOB = ExecutionContextBuilder().with_tenant_id("acme").with_user_id("bob").build()


async def _boot(backend: RelationalBackend) -> ApplicationContext:
    await backend.create_tables(CACHE_TABLE)
    ctx = ApplicationContext(backend.config())
    ctx.register_bean(RelationalAutoConfiguration)
    await ctx.start()
    return ctx


async def _stored(backend: RelationalBackend) -> dict[str, Any]:
    """What another process sees: an engine of its own, not the application's pool."""
    engine = create_async_engine(backend.url, poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            rows = (await conn.execute(select(CACHE_TABLE.c.cache_key, CACHE_TABLE.c.value))).all()
    finally:
        await engine.dispose()
    return {key: json.loads(value) for key, value in rows}


def _query_bus(cache: JoiningSqlCache, *handlers: QueryHandler[Any, Any]) -> DefaultQueryBus:
    registry = HandlerRegistry()
    for handler in handlers:
        registry.register_query_handler(handler)
    return DefaultQueryBus(registry=registry, cache_adapter=QueryCacheAdapter(cache))


# ---------------------------------------------------------------------------------------------------------
# The caller's rollback keeps what an immediate operation did
# ---------------------------------------------------------------------------------------------------------


async def test_immediate_operations_run_in_units_of_their_own(relational_backend: RelationalBackend) -> None:
    ctx = await _boot(relational_backend)
    try:
        cache = JoiningSqlCache()
        await cache.put("kept", "v")
        await cache.put("stale", "v")
        tx_cache = TransactionAwareCache(cache)
        cache.units.clear()
        # A read-only unit: the unit a query usually runs in, on every backend (a write unit holds SQLite's one
        # write lock, see the SQLite test below).
        with pytest.raises(Rollback):
            async with TransactionTemplate(read_only=True).transaction() as business:
                assert await tx_cache.get("kept") == "v"
                assert await tx_cache.exists("kept") is True
                assert await tx_cache.put_if_absent("generation", "g1") is True
                assert await tx_cache.evict_if_present("stale") is True
                assert business is not None and all(unit is not business for unit in cache.units)
                raise Rollback
        assert len(cache.units) == 4
        assert await _stored(relational_backend) == {"kept": "v", "generation": "g1"}
    finally:
        await ctx.stop()


async def test_invalidate_is_not_undone_by_the_callers_rollback(relational_backend: RelationalBackend) -> None:
    ctx = await _boot(relational_backend)
    try:
        cache = JoiningSqlCache()
        await cache.put("a", 1)
        await cache.put("b", 2)
        with pytest.raises(Rollback):
            async with TransactionTemplate(read_only=True).transaction():
                await TransactionAwareCache(cache).invalidate()
                raise Rollback
        assert await _stored(relational_backend) == {}
    finally:
        await ctx.stop()


@pytest.mark.backends(PG)
async def test_a_rolled_back_write_unit_keeps_the_immediate_operations(relational_backend: RelationalBackend) -> None:
    ctx = await _boot(relational_backend)
    try:
        cache = JoiningSqlCache()
        await cache.put("stale", "v")
        tx_cache = TransactionAwareCache(cache)
        cache.units.clear()
        with pytest.raises(Rollback):
            async with TransactionTemplate().transaction() as business:
                assert await tx_cache.put_if_absent("generation", "g1") is True
                assert await tx_cache.evict_if_present("stale") is True
                assert business is not None and all(unit is not business for unit in cache.units)
                raise Rollback
        assert await _stored(relational_backend) == {"generation": "g1"}
    finally:
        await ctx.stop()


@pytest.mark.backends(SQLITE_FILE)
async def test_on_sqlite_an_immediate_write_beside_the_callers_write_unit_is_refused_at_once(
    relational_backend: RelationalBackend,
) -> None:
    ctx = await _boot(relational_backend)
    try:
        cache = JoiningSqlCache()
        await cache.put("stale", "v")
        tx_cache = TransactionAwareCache(cache)  # on_write_error="raise"
        async with TransactionTemplate().transaction():
            # The unit holds the database's one write lock: a write in a unit of its own would wait busy_timeout
            # (5 s) for a lock this task holds, so it is refused before it starts.
            started = time.perf_counter()
            with pytest.raises(IllegalTransactionStateError, match="write lock"):
                await tx_cache.evict_if_present("stale")
            assert time.perf_counter() - started < 1.0
            assert await tx_cache.get("stale") == "v"  # reads run beside the writer (WAL)
        assert await _stored(relational_backend) == {"stale": "v"}
    finally:
        await ctx.stop()


def _refusals(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [
        record.getMessage()
        for record in caplog.records
        if record.levelno >= logging.WARNING and "refused" in record.getMessage()
    ]


@pytest.mark.backends(SQLITE_FILE)
async def test_on_sqlite_log_mode_reports_a_refused_immediate_write_once_and_carries_on(
    relational_backend: RelationalBackend, caplog: pytest.LogCaptureFixture
) -> None:
    ctx = await _boot(relational_backend)
    try:
        cache = JoiningSqlCache()
        await cache.put("stale", "v")
        await cache.put("kept", "v")
        tx_cache = TransactionAwareCache(cache, on_write_error="log")
        caplog.set_level(logging.DEBUG, logger="pyfly.cache")
        for _ in range(3):
            async with TransactionTemplate().transaction():
                assert await tx_cache.put_if_absent("generation", "g1") is False  # refused: not stored
                assert await tx_cache.evict_if_present("stale") is False  # refused: evicted after the commit
                assert await _stored(relational_backend) == {"stale": "v", "kept": "v"}
            assert await _stored(relational_backend) == {"kept": "v"}
            await cache.put("stale", "v")
        assert len(_refusals(caplog)) == 1  # once per cache, then at DEBUG
        async with TransactionTemplate().transaction():
            await tx_cache.invalidate()  # refused: the cache is cleared after the commit
            assert await _stored(relational_backend) == {"stale": "v", "kept": "v"}
        assert await _stored(relational_backend) == {}
    finally:
        await ctx.stop()


@pytest.mark.backends(SQLITE_FILE)
async def test_on_sqlite_an_eviction_before_invocation_does_not_stop_the_business_method(
    relational_backend: RelationalBackend, caplog: pytest.LogCaptureFixture
) -> None:
    ctx = await _boot(relational_backend)
    try:
        cache = JoiningSqlCache()
        await cache.put("price:a", 10)
        ran: list[str] = []

        @transactional
        @cache_evict(cache, key="price:{sku}", before_invocation=True)
        async def reprice(sku: str) -> None:
            ran.append(sku)

        caplog.set_level(logging.DEBUG, logger="pyfly.cache")
        await reprice("a")
        await reprice("a")
        assert ran == ["a", "a"]
        assert await _stored(relational_backend) == {}  # the refused eviction ran after the commit
        assert len(_refusals(caplog)) == 1
    finally:
        await ctx.stop()


@pytest.mark.backends(SQLITE_FILE)
async def test_on_sqlite_the_query_cache_answers_uncached_and_reports_the_refusal_once(
    relational_backend: RelationalBackend, caplog: pytest.LogCaptureFixture
) -> None:
    ctx = await _boot(relational_backend)
    try:
        cache = JoiningSqlCache()
        handler = PriceHandler()
        bus = _query_bus(cache, handler)
        caplog.set_level(logging.DEBUG)
        async with TransactionTemplate().transaction():
            for _ in range(3):
                assert await bus.query_with_context(PriceQuery(), ALICE) == 42
        assert handler.calls == 3  # no generation could be started beside the write unit: not cached
        assert len(_refusals(caplog)) == 1
        assert not any("CQRS cache get failed" in record.getMessage() for record in caplog.records)
        assert await _stored(relational_backend) == {}  # no entry under a generation that was never stored
    finally:
        await ctx.stop()


# ---------------------------------------------------------------------------------------------------------
# Plain queries and other business units do not wait for a unit's cache statements (PostgreSQL)
# ---------------------------------------------------------------------------------------------------------


@pytest.mark.backends(PG)
async def test_a_plain_query_does_not_wait_for_another_units_generation(relational_backend: RelationalBackend) -> None:
    ctx = await _boot(relational_backend)
    try:
        bus = _query_bus(JoiningSqlCache(), PriceHandler())
        queried, answered = asyncio.Event(), asyncio.Event()

        @transactional
        async def business() -> None:
            await bus.query_with_context(PriceQuery(), ALICE)  # starts the key's generation
            queried.set()
            await asyncio.wait_for(answered.wait(), timeout=30)  # the rest of the business work

        alice = asyncio.create_task(business())
        try:
            await queried.wait()
            started = time.perf_counter()
            # bob's plain query, outside any transaction, while alice's unit is still open.
            assert await asyncio.wait_for(bus.query_with_context(PriceQuery(), BOB), timeout=5) == 42
            assert time.perf_counter() - started < 1.0
        finally:
            answered.set()
            await alice
    finally:
        await ctx.stop()


@pytest.mark.backends(PG)
async def test_two_units_running_two_queries_in_opposite_orders_both_commit(
    relational_backend: RelationalBackend,
) -> None:
    ctx = await _boot(relational_backend)
    try:
        bus = _query_bus(JoiningSqlCache(), PriceHandler(), StockHandler())
        first_done = [asyncio.Event(), asyncio.Event()]

        @transactional
        async def business(me: int, first: Query[int], second: Query[int], context: ExecutionContext) -> str:
            await bus.query_with_context(first, context)
            first_done[me].set()
            await asyncio.wait_for(first_done[1 - me].wait(), timeout=10)
            await bus.query_with_context(second, context)
            return "committed"

        outcomes = await asyncio.wait_for(
            asyncio.gather(
                business(0, PriceQuery(), StockQuery(), ALICE),
                business(1, StockQuery(), PriceQuery(), BOB),
                return_exceptions=True,
            ),
            timeout=30,
        )
        assert list(outcomes) == ["committed", "committed"]
    finally:
        await ctx.stop()


# ---------------------------------------------------------------------------------------------------------
# A task that outlived its unit
# ---------------------------------------------------------------------------------------------------------


async def test_the_writes_of_a_task_that_outlived_its_committed_unit_reach_the_store(
    relational_backend: RelationalBackend, caplog: pytest.LogCaptureFixture
) -> None:
    ctx = await _boot(relational_backend)
    try:
        cache = JoiningSqlCache()
        await cache.put("stale", "v")
        await cache.put(":cqrs:OrderQuery:1", "cached")
        tx_cache = TransactionAwareCache(cache, on_write_error="log")
        query_cache = QueryCacheAdapter(cache)
        committed = asyncio.Event()
        caplog.set_level(logging.WARNING)

        async def later() -> None:
            await committed.wait()  # the unit that started this task has committed meanwhile
            await tx_cache.evict("stale")
            await tx_cache.put("rate", 1.1)
            await query_cache.evict_keys(["OrderQuery:1"])

        async with TransactionTemplate().transaction():
            task = asyncio.create_task(later())  # started and not awaited: it outlives the unit
        committed.set()
        await task
        assert await _stored(relational_backend) == {"rate": 1.1}
        assert not [record.getMessage() for record in caplog.records if "skipped" in record.getMessage()]
    finally:
        await ctx.stop()


# ---------------------------------------------------------------------------------------------------------
# Deferred writes still wait for the commit
# ---------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("outcome", ["commit", "rollback"])
async def test_deferred_writes_still_wait_for_the_commit(relational_backend: RelationalBackend, outcome: str) -> None:
    ctx = await _boot(relational_backend)
    try:
        cache = JoiningSqlCache()
        await cache.put("name", "old")
        await cache.put("stale", "v")
        tx_cache = TransactionAwareCache(cache)
        before = await _stored(relational_backend)
        try:
            async with TransactionTemplate().transaction():
                await tx_cache.put("name", "new")
                await tx_cache.evict("stale")
                assert await _stored(relational_backend) == before  # nothing is written before the commit
                if outcome == "rollback":
                    raise Rollback
        except Rollback:
            pass
        expected = before if outcome == "rollback" else {"name": "new"}
        assert await _stored(relational_backend) == expected
    finally:
        await ctx.stop()
