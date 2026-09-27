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
"""Savepoints the application opens itself inside a unit of work (WP01-04).

A statement that fails inside a unit marks it rollback-only, but not when it fails inside a savepoint that
is then rolled back: after ``ROLLBACK TO SAVEPOINT`` the transaction is healthy on every backend. The
SQLAlchemy idiom ``async with session.begin_nested(): ...`` around an insert that may be a duplicate
therefore commits the rest, through the repository's session, an injected ``AsyncSession`` and
``SessionProvider.current()``, with ORM flushes and with raw statements.

A failure the application catches without rolling its savepoint back still dooms the unit (on PostgreSQL
the transaction is dead until then, and the same rule holds on every backend): the unit rolls back and
``UnexpectedRollbackError`` is raised. Inside ``Propagation.NESTED``, such a failure rolls the NESTED
scope back to its own savepoint instead, and the outer unit commits.
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator

import pytest
from sqlalchemy import Identity, Integer, String, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.pool import NullPool

from pyfly.container.stereotypes import repository, service
from pyfly.context.application_context import ApplicationContext
from pyfly.data import Propagation, transactional
from pyfly.data.relational.auto_configuration import RelationalAutoConfiguration
from pyfly.data.relational.datasource_registry import DataSourceRegistry
from pyfly.data.relational.sqlalchemy.entity import Base
from pyfly.data.relational.sqlalchemy.repository import Repository
from pyfly.data.relational.sqlalchemy.session import SessionProvider
from pyfly.data.transaction import UnexpectedRollbackError
from tests.support.backend_matrix import MYSQL, PG, SQLITE_FILE, RelationalBackend

pytestmark = pytest.mark.backends(SQLITE_FILE, PG, MYSQL)


class SpItem(Base):
    __tablename__ = "sp_item"

    id: Mapped[int] = mapped_column(Integer, Identity(), primary_key=True)
    name: Mapped[str] = mapped_column(String(64), unique=True)


@repository
class SpItemRepository(Repository[SpItem, int]):
    pass


_DUPLICATE_SAFE_INSERT = "INSERT INTO sp_item (name) VALUES (:name)"


@service
class SpInner:
    def __init__(self, items: SpItemRepository) -> None:
        self.items = items

    @transactional(propagation=Propagation.NESTED)
    async def nested_with_a_savepoint_left_open(self) -> None:
        await self.items.save(SpItem(name="nested"))
        session = self.items._session
        await session.begin_nested()
        with contextlib.suppress(IntegrityError):  # caught, and the savepoint is left open
            await session.execute(text(_DUPLICATE_SAFE_INSERT), {"name": "nested"})


@service
class SpService:
    def __init__(
        self, items: SpItemRepository, session: AsyncSession, sessions: SessionProvider, inner: SpInner
    ) -> None:
        self.items = items
        self.session = session
        self.sessions = sessions
        self.inner = inner

    def _session_via(self, via: str) -> AsyncSession:
        if via == "repository":
            return self.items._session
        if via == "injected":
            return self.session
        current = self.sessions.current()
        assert current is not None
        return current

    @transactional
    async def insert_ignoring_duplicates(self, names: list[str], via: str) -> None:
        session = self._session_via(via)
        for name in names:
            try:
                async with session.begin_nested():
                    session.add(SpItem(name=name))
                    await session.flush()
            except IntegrityError:
                pass  # already there: the savepoint rolled back, and the transaction goes on

    @transactional
    async def insert_raw_ignoring_duplicates(self, names: list[str]) -> None:
        session = self.items._session
        for name in names:
            try:
                async with session.begin_nested():
                    await session.execute(text(_DUPLICATE_SAFE_INSERT), {"name": name})
            except IntegrityError:
                pass

    @transactional
    async def catch_without_rolling_back_the_savepoint(self) -> None:
        await self.items.save(SpItem(name="first"))
        session = self.items._session
        await session.begin_nested()
        with contextlib.suppress(IntegrityError):  # neither rolled back nor released
            await session.execute(text(_DUPLICATE_SAFE_INSERT), {"name": "first"})

    @transactional
    async def catch_a_failed_flush_without_rolling_back_the_savepoint(self) -> None:
        await self.items.save(SpItem(name="first"))
        session = self.items._session
        await session.begin_nested()
        session.add(SpItem(name="first"))
        with contextlib.suppress(IntegrityError):
            await session.flush()

    @transactional
    async def outer_around_a_nested_scope(self) -> None:
        await self.items.save(SpItem(name="outer"))
        await self.inner.nested_with_a_savepoint_left_open()
        await self.items.save(SpItem(name="outer-after"))


class Savepoints:
    def __init__(self, backend: RelationalBackend, ctx: ApplicationContext) -> None:
        self.backend = backend
        self.ctx = ctx

    @property
    def service(self) -> SpService:
        return self.ctx.get_bean(SpService)

    def checked_out(self) -> int:
        registry = self.ctx.get_bean(DataSourceRegistry)
        return sum(ds.engine.sync_engine.pool.checkedout() for ds in registry.all_datasources())

    async def committed(self) -> list[str]:
        engine = create_async_engine(self.backend.url, poolclass=NullPool)
        try:
            async with engine.connect() as conn:
                return [r[0] for r in (await conn.execute(text("SELECT name FROM sp_item ORDER BY name"))).all()]
        finally:
            await engine.dispose()


@pytest.fixture
async def savepoints(relational_backend: RelationalBackend) -> AsyncIterator[Savepoints]:
    await relational_backend.create_tables(SpItem)
    ctx = ApplicationContext(relational_backend.config())
    for bean in (RelationalAutoConfiguration, SpItemRepository, SpInner, SpService):
        ctx.register_bean(bean)
    await ctx.start()
    try:
        yield Savepoints(relational_backend, ctx)
        assert Savepoints(relational_backend, ctx).checked_out() == 0
    finally:
        await ctx.stop()


@pytest.mark.parametrize("via", ["repository", "injected", "provider"])
async def test_the_savepoint_idiom_commits_everything_but_the_duplicates(savepoints: Savepoints, via: str) -> None:
    await savepoints.service.insert_ignoring_duplicates(["a", "b", "a", "c", "b"], via)
    assert await savepoints.committed() == ["a", "b", "c"]


async def test_the_savepoint_idiom_with_raw_statements(savepoints: Savepoints) -> None:
    await savepoints.service.insert_raw_ignoring_duplicates(["a", "b", "a", "c"])
    assert await savepoints.committed() == ["a", "b", "c"]


@pytest.mark.parametrize(
    "method", ["catch_without_rolling_back_the_savepoint", "catch_a_failed_flush_without_rolling_back_the_savepoint"]
)
async def test_a_failure_caught_without_rolling_back_its_savepoint_dooms_the_unit(
    savepoints: Savepoints, method: str
) -> None:
    with pytest.raises(UnexpectedRollbackError) as raised:
        await getattr(savepoints.service, method)()
    assert isinstance(raised.value.__cause__, IntegrityError)
    assert await savepoints.committed() == []


async def test_a_savepoint_left_open_with_a_failure_inside_nested_rolls_back_only_the_nested_scope(
    savepoints: Savepoints,
) -> None:
    await savepoints.service.outer_around_a_nested_scope()
    assert await savepoints.committed() == ["outer", "outer-after"]
