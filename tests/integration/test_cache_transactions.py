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
"""The cache decorators against real transactions (WP14: C022, C023, C025).

A real ``ApplicationContext`` with the relational auto-configuration, ``@repository`` beans and
``@transactional`` services, on a SQLite file database (foreign keys on) and on PostgreSQL. What the
database committed is read through an engine of its own.

- C023: ``@cache_put``/``@cacheable`` writes and ``@cache_evict`` evictions made inside a unit of work
  wait for its commit and are dropped when it rolls back, including a commit that fails on a deferred
  foreign key.
- C022: a live entity is never cached, so a rollback cannot leave a broken entry behind.
- C025: a value the cache refuses at run time never changes the outcome of the call (no retry, no
  duplicate row). The decoration-time checks are in ``tests/cache/test_cache.py``.
- A task a unit's body started and did not await, which outlives the unit, still gets its result: the
  write it makes is applied at once when the unit committed, and dropped (logged) when it rolled back.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import pytest
from sqlalchemy import ForeignKey, Identity, Integer, String, text, update
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.pool import NullPool

from pyfly.cache.adapters.memory import InMemoryCache
from pyfly.cache.decorators import cache_evict, cache_put, cacheable
from pyfly.container.stereotypes import repository, service
from pyfly.context.application_context import ApplicationContext
from pyfly.data.relational.auto_configuration import RelationalAutoConfiguration
from pyfly.data.relational.sqlalchemy.entity import Base
from pyfly.data.relational.sqlalchemy.repository import Repository
from pyfly.data.transaction import Propagation, detached, transactional
from tests.support.backend_matrix import PG, SQLITE_FILE, RelationalBackend

pytestmark = pytest.mark.backends(SQLITE_FILE, PG)

CACHE = InMemoryCache()


class CacheTxItem(Base):
    __tablename__ = "wp14_cache_tx_item"

    id: Mapped[int] = mapped_column(Integer, Identity(), primary_key=True)
    name: Mapped[str] = mapped_column(String(64))


class CacheTxChild(Base):
    __tablename__ = "wp14_cache_tx_child"

    id: Mapped[int] = mapped_column(Integer, Identity(), primary_key=True)
    parent_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("wp14_cache_tx_item.id", deferrable=True, initially="DEFERRED")
    )
    label: Mapped[str] = mapped_column(String(64))


@repository
class CacheTxItemRepository(Repository[CacheTxItem, int]):
    pass


@repository
class CacheTxChildRepository(Repository[CacheTxChild, int]):
    pass


@service
class Catalog:
    def __init__(self, items: CacheTxItemRepository, children: CacheTxChildRepository) -> None:
        self.items = items
        self.children = children
        self.reads = 0

    @cacheable(CACHE, key="name:{item_id}")
    async def get_name(self, item_id: int) -> str | None:
        self.reads += 1
        item = await self.items.find_by_id(item_id)
        return item.name if item else None

    @cache_put(CACHE, key="name:{item_id}")
    async def rename(self, item_id: int, name: str) -> str:
        await self.items._session.execute(update(CacheTxItem).where(CacheTxItem.id == item_id).values(name=name))
        return name

    @cache_evict(CACHE, key="name:{item_id}")
    async def touch_nothing(self, item_id: int) -> None:
        pass

    @cache_evict(CACHE, key="name:{item_id}")
    async def touch(self, item_id: int, name: str) -> None:
        await self.items._session.execute(update(CacheTxItem).where(CacheTxItem.id == item_id).values(name=name))

    @transactional
    @cache_put(CACHE, key="child:{label}")
    async def add_child(self, parent_id: int, label: str) -> str:
        await self.children.save(CacheTxChild(parent_id=parent_id, label=label))
        return f"{label} -> parent {parent_id}"

    @cacheable(CACHE, key="entity:{item_id}")
    async def get_entity(self, item_id: int):  # unannotated on purpose: refused at run time (C022)
        return await self.items.find_by_id(item_id)

    @cache_put(CACHE, key="created:{name}")
    async def create(self, name: str):  # unannotated on purpose: refused at run time (C025)
        return await self.items.save(CacheTxItem(name=name))

    @cacheable(CACHE, key="rate:{currency}")
    async def rate(self, currency: str, answered: asyncio.Event) -> float:
        await answered.wait()  # a call to a rates API, which answers after the caller's unit completed
        return 1.1


@service
class Flow:
    def __init__(self, catalog: Catalog) -> None:
        self.catalog = catalog
        self.seen_inside: list[Any] = []
        self.background: list[asyncio.Task[float]] = []

    @transactional
    async def rename_then(self, item_id: int, name: str, *, fail: bool) -> None:
        await self.catalog.rename(item_id, name)
        self.seen_inside.append(await CACHE.exists(f"name:{item_id}"))
        if fail:
            raise RuntimeError("a later step fails")

    @transactional
    async def change_then_read(self, item_id: int, name: str, *, fail: bool) -> str | None:
        await self.catalog.items._session.execute(
            update(CacheTxItem).where(CacheTxItem.id == item_id).values(name=name)
        )
        seen = await self.catalog.get_name(item_id)
        self.seen_inside.append(await CACHE.exists(f"name:{item_id}"))
        if fail:
            raise RuntimeError("a later step fails")
        return seen

    @transactional
    async def touch_then(self, item_id: int, name: str, *, fail: bool) -> None:
        await self.catalog.touch(item_id, name)
        # A reader outside this transaction still gets the committed value until the commit.
        self.seen_inside.append(await detached(self.catalog.get_name(item_id)))
        if fail:
            raise RuntimeError("a later step fails")

    @transactional(propagation=Propagation.NESTED)
    async def try_rename(self, item_id: int, name: str) -> None:
        await self.catalog.rename(item_id, name)
        raise RuntimeError("the step fails and rolls back to its savepoint")

    @transactional
    async def rename_in_a_failing_step(self, item_id: int, name: str) -> None:
        try:
            await self.try_rename(item_id, name)
        except RuntimeError:
            # The put the step registered belongs to this unit and runs when it commits: evict the key here,
            # after it, as caching.md advises.
            await self.catalog.touch_nothing(item_id)

    @transactional
    async def look_up_rate_in_the_background(self, currency: str, answered: asyncio.Event, *, fail: bool) -> None:
        # Started and not awaited: the task outlives this unit.
        self.background.append(asyncio.create_task(self.catalog.rate(currency, answered)))
        await asyncio.sleep(0)
        if fail:
            raise RuntimeError("a later step fails")

    @transactional
    async def read_entity_then_fail(self, item_id: int) -> None:
        item = await self.catalog.get_entity(item_id)
        assert item is not None
        raise RuntimeError(f"out of stock: {item.name}")


_BEANS = (RelationalAutoConfiguration, CacheTxItemRepository, CacheTxChildRepository, Catalog, Flow)


async def _boot(backend: RelationalBackend) -> ApplicationContext:
    await backend.create_tables(CacheTxItem, CacheTxChild)
    await CACHE.clear()
    ctx = ApplicationContext(backend.config())
    for bean in _BEANS:
        ctx.register_bean(bean)
    await ctx.start()
    return ctx


async def _rows(backend: RelationalBackend, table: str, column: str) -> list[Any]:
    """What another process sees: an engine of its own, not the application's pool."""
    engine = create_async_engine(backend.url, poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            return [row[0] for row in (await conn.execute(text(f"SELECT {column} FROM {table} ORDER BY id"))).all()]
    finally:
        await engine.dispose()


async def _seed(backend: RelationalBackend, *names: str) -> None:
    engine = create_async_engine(backend.url, poolclass=NullPool)
    try:
        async with engine.begin() as conn:
            for name in names:
                await conn.execute(text("INSERT INTO wp14_cache_tx_item (name) VALUES (:n)"), {"n": name})
    finally:
        await engine.dispose()


# ---------------------------------------------------------------------------------------------------------
# C023: puts and evictions wait for the commit
# ---------------------------------------------------------------------------------------------------------


async def test_a_put_inside_a_transaction_that_rolls_back_is_dropped(relational_backend: RelationalBackend) -> None:
    ctx = await _boot(relational_backend)
    try:
        await _seed(relational_backend, "original")
        flow = ctx.get_bean(Flow)
        with pytest.raises(RuntimeError, match="later step"):
            await flow.rename_then(1, "ghost", fail=True)
        assert await _rows(relational_backend, "wp14_cache_tx_item", "name") == ["original"]
        assert flow.seen_inside == [False]
        assert await CACHE.exists("name:1") is False
        assert await ctx.get_bean(Catalog).get_name(1) == "original"
    finally:
        await ctx.stop()


async def test_a_put_inside_a_transaction_is_applied_after_the_commit(relational_backend: RelationalBackend) -> None:
    ctx = await _boot(relational_backend)
    try:
        await _seed(relational_backend, "original")
        flow = ctx.get_bean(Flow)
        await flow.rename_then(1, "renamed", fail=False)
        assert flow.seen_inside == [False]  # nothing is written before the commit
        assert await _rows(relational_backend, "wp14_cache_tx_item", "name") == ["renamed"]
        assert await CACHE.get("name:1") == "renamed"
    finally:
        await ctx.stop()


async def test_a_read_cached_inside_a_rolled_back_transaction_is_dropped(
    relational_backend: RelationalBackend,
) -> None:
    ctx = await _boot(relational_backend)
    try:
        await _seed(relational_backend, "original")
        flow = ctx.get_bean(Flow)
        with pytest.raises(RuntimeError, match="later step"):
            await flow.change_then_read(1, "draft", fail=True)
        assert flow.seen_inside == [False]
        assert await CACHE.exists("name:1") is False
        assert await _rows(relational_backend, "wp14_cache_tx_item", "name") == ["original"]

        assert await flow.change_then_read(1, "published", fail=False) == "published"
        assert flow.seen_inside == [False, False]  # cached only once the commit made it true
        assert await CACHE.get("name:1") == "published"
        assert await ctx.get_bean(Catalog).get_name(1) == "published"
    finally:
        await ctx.stop()


async def test_an_eviction_waits_for_the_commit(relational_backend: RelationalBackend) -> None:
    ctx = await _boot(relational_backend)
    try:
        await _seed(relational_backend, "v1")
        catalog = ctx.get_bean(Catalog)
        flow = ctx.get_bean(Flow)
        assert await catalog.get_name(1) == "v1"
        await flow.touch_then(1, "v2", fail=False)
        # Before the commit a concurrent reader was served the committed v1 (and could re-cache it); the
        # eviction ran after the commit, so the next read is fresh instead of stale forever.
        assert flow.seen_inside == ["v1"]
        assert await CACHE.exists("name:1") is False
        assert await catalog.get_name(1) == "v2"
    finally:
        await ctx.stop()


async def test_an_eviction_inside_a_rolled_back_transaction_is_dropped(
    relational_backend: RelationalBackend,
) -> None:
    ctx = await _boot(relational_backend)
    try:
        await _seed(relational_backend, "v1")
        catalog = ctx.get_bean(Catalog)
        assert await catalog.get_name(1) == "v1"
        with pytest.raises(RuntimeError, match="later step"):
            await ctx.get_bean(Flow).touch_then(1, "v2", fail=True)
        assert await _rows(relational_backend, "wp14_cache_tx_item", "name") == ["v1"]
        assert await CACHE.get("name:1") == "v1"
        assert catalog.reads == 1
    finally:
        await ctx.stop()


async def test_a_put_is_dropped_when_the_commit_fails(relational_backend: RelationalBackend) -> None:
    ctx = await _boot(relational_backend)
    try:
        await _seed(relational_backend, "parent")
        catalog = ctx.get_bean(Catalog)
        # The foreign key is deferred, so the missing parent fails the COMMIT, after the method returned.
        with pytest.raises(Exception) as failure:
            await catalog.add_child(999, "orphan")
        assert "foreign key" in str(failure.value).lower()
        assert await _rows(relational_backend, "wp14_cache_tx_child", "label") == []
        assert await CACHE.exists("child:orphan") is False

        assert await catalog.add_child(1, "adopted") == "adopted -> parent 1"
        assert await CACHE.get("child:adopted") == "adopted -> parent 1"
    finally:
        await ctx.stop()


# ---------------------------------------------------------------------------------------------------------
# C022 and C025: live entities are never cached, and a refused value never changes the call's outcome
# ---------------------------------------------------------------------------------------------------------


async def test_a_rollback_leaves_no_broken_entry_behind(
    relational_backend: RelationalBackend, caplog: pytest.LogCaptureFixture
) -> None:
    ctx = await _boot(relational_backend)
    try:
        await _seed(relational_backend, "widget")
        flow = ctx.get_bean(Flow)
        caplog.set_level(logging.WARNING, logger="pyfly.cache")
        with pytest.raises(RuntimeError, match="out of stock: widget"):
            await flow.read_entity_then_fail(1)
        assert await CACHE.exists("entity:1") is False
        # Every later request loads its own entity: no DetachedInstanceError from a poisoned entry.
        for _ in range(3):
            item = await ctx.get_bean(Catalog).get_entity(1)
            assert item is not None
            assert item.name == "widget"
        assert await CACHE.exists("entity:1") is False
        assert any("CacheTxItem" in record.getMessage() for record in caplog.records)
    finally:
        await ctx.stop()


async def test_a_refused_value_never_fails_a_committed_write(
    relational_backend: RelationalBackend, caplog: pytest.LogCaptureFixture
) -> None:
    ctx = await _boot(relational_backend)
    try:
        catalog = ctx.get_bean(Catalog)
        caplog.set_level(logging.WARNING, logger="pyfly.cache")
        created = await catalog.create("order-1")
        assert created.name == "order-1"
        # The caller got its result: no TypeError after the commit, so no retry and no duplicate row.
        assert await _rows(relational_backend, "wp14_cache_tx_item", "name") == ["order-1"]
        assert await CACHE.exists("created:order-1") is False
        assert any("cache_put_skipped" in record.getMessage() for record in caplog.records)
    finally:
        await ctx.stop()


async def test_evicting_where_a_nested_step_failed_leaves_no_uncommitted_value_cached(
    relational_backend: RelationalBackend,
) -> None:
    ctx = await _boot(relational_backend)
    try:
        await _seed(relational_backend, "original")
        await ctx.get_bean(Flow).rename_in_a_failing_step(1, "ghost")
        assert await _rows(relational_backend, "wp14_cache_tx_item", "name") == ["original"]
        assert await CACHE.exists("name:1") is False
        assert await ctx.get_bean(Catalog).get_name(1) == "original"
    finally:
        await ctx.stop()


# ---------------------------------------------------------------------------------------------------------
# A task that outlived its unit
# ---------------------------------------------------------------------------------------------------------


async def test_a_task_that_outlived_its_committed_unit_caches_its_result(relational_backend: RelationalBackend) -> None:
    ctx = await _boot(relational_backend)
    try:
        flow = ctx.get_bean(Flow)
        answered = asyncio.Event()
        await flow.look_up_rate_in_the_background("USD", answered, fail=False)
        answered.set()
        assert await flow.background[-1] == 1.1  # no IllegalTransactionStateError after the method ran
        assert await CACHE.get("rate:USD") == 1.1
    finally:
        await ctx.stop()


async def test_a_task_that_outlived_its_rolled_back_unit_caches_nothing_and_never_fails(
    relational_backend: RelationalBackend, caplog: pytest.LogCaptureFixture
) -> None:
    ctx = await _boot(relational_backend)
    try:
        flow = ctx.get_bean(Flow)
        answered = asyncio.Event()
        with pytest.raises(RuntimeError, match="later step"):
            await flow.look_up_rate_in_the_background("GBP", answered, fail=True)
        caplog.set_level(logging.WARNING, logger="pyfly.cache")
        answered.set()
        assert await flow.background[-1] == 1.1
        assert await CACHE.exists("rate:GBP") is False
        assert any(record.getMessage().startswith("cache_put_skipped") for record in caplog.records)
    finally:
        await ctx.stop()
