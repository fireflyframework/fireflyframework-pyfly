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
"""An application's primary data bean is the primary of every unit of work (SQLite file and PostgreSQL).

A singleton ``async_sessionmaker``, ``AsyncEngine`` or ``DataSourceRegistry`` bean replaces the
auto-configured one (WP07). The units of work used to be built from the registry's primary whatever the
application declared, so the primary split in two: the ``AsyncSession`` bean and the routing factory ran on
the application's bean, while ``@transactional``, repository calls outside a transaction, ``SessionProvider``
and ``infrastructure_unit()`` ran on ``pyfly.data.relational.url`` (and, with a registry bean, the
``AsyncSession`` bean stayed on the configuration's registry). Every path now lands in the application's
database, whether the override is a plain or a ``@primary`` bean, built at once or deferred until an
auto-configured bean it takes exists. Without an override nothing moves.

The framework disposes an application engine only where it already did (the ``AsyncEngine`` bean, through
the engine lifecycle): the engine under an application's session factory stays the application's.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Identity, Integer, String, event, insert, select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import Mapped, Session, SessionTransaction, mapped_column
from sqlalchemy.pool import NullPool

from pyfly.container import bean, configuration
from pyfly.container.stereotypes import repository, service
from pyfly.context.application_context import ApplicationContext
from pyfly.context.conditions import auto_configuration
from pyfly.core.config import Config
from pyfly.data import transactional
from pyfly.data.relational.datasource_registry import DataSourceRegistry
from pyfly.data.relational.sqlalchemy.entity import Base
from pyfly.data.relational.sqlalchemy.repository import Repository
from pyfly.data.relational.sqlalchemy.session import SessionProvider
from pyfly.data.relational.sqlalchemy.transaction_manager import SqlAlchemyTransactionManager
from pyfly.data.transaction import TransactionManagerRegistry, infrastructure_unit
from tests.support.backend_matrix import (
    PG,
    SQLITE_FILE,
    RelationalBackend,
    create_database,
    drop_database,
    new_database_name,
)


class PboItem(Base):
    __tablename__ = "pbo_item"

    id: Mapped[int] = mapped_column(Integer, Identity(), primary_key=True)
    name: Mapped[str] = mapped_column(String(64))


@repository
class PboItemRepository(Repository[PboItem, int]):
    pass


@service
class PboWriter:
    """Writes through a repository and through an injected ``AsyncSession``, inside ``@transactional``."""

    def __init__(self, items: PboItemRepository, session: AsyncSession) -> None:
        self.items = items
        self.session = session

    @transactional
    async def through_the_repository(self, name: str) -> None:
        await self.items.save(PboItem(name=name))

    @transactional
    async def through_the_injected_session(self, name: str) -> None:
        self.session.add(PboItem(name=name))  # the unit's session: it commits with the unit


# ---------------------------------------------------------------------------------------------------------
# The application's beans
# ---------------------------------------------------------------------------------------------------------

_USER: dict[str, str] = {}
_ENGINES: list[AsyncEngine] = []
_DISPOSED: list[AsyncEngine] = []


class _AppSession(Session):
    """The sync session class of the application's session factory: its transactions are recorded."""


_APP_BEGINS: list[SessionTransaction] = []


@event.listens_for(_AppSession, "after_begin")
def _app_session_began(_session: Session, transaction: SessionTransaction, _connection: Any) -> None:
    _APP_BEGINS.append(transaction)


def _user_engine(url: str) -> AsyncEngine:
    engine = create_async_engine(url)
    event.listen(engine.sync_engine, "engine_disposed", lambda _engine: _DISPOSED.append(engine))
    _ENGINES.append(engine)
    return engine


def _user_sessions(url: str) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(_user_engine(url), sync_session_class=_AppSession, expire_on_commit=False)


@configuration
class _PlainSessions:
    @bean
    def app_sessions(self) -> async_sessionmaker[AsyncSession]:
        return _user_sessions(_USER["url"])


@configuration
class _PrimarySessions:
    @bean(primary=True)
    def app_sessions(self) -> async_sessionmaker[AsyncSession]:
        return _user_sessions(_USER["url"])


class _UserDatabase:
    """Settings an auto-configuration provides: a factory that takes them is deferred until it has run."""

    def __init__(self, url: str) -> None:
        self.url = url


@auto_configuration
class _UserDatabaseAutoConfiguration:
    @bean
    def user_database(self) -> _UserDatabase:
        return _UserDatabase(_USER["url"])


@configuration
class _DeferredPlainSessions:
    @bean
    def app_sessions(self, database: _UserDatabase) -> async_sessionmaker[AsyncSession]:
        return _user_sessions(database.url)


@configuration
class _DeferredPrimarySessions:
    @bean(primary=True)
    def app_sessions(self, database: _UserDatabase) -> async_sessionmaker[AsyncSession]:
        return _user_sessions(database.url)


@configuration
class _UserEngine:
    @bean
    def app_engine(self) -> AsyncEngine:
        return _user_engine(_USER["url"])


@configuration
class _UserRegistry:
    @bean
    def app_datasources(self) -> DataSourceRegistry:
        return DataSourceRegistry(
            Config({"pyfly": {"data": {"relational": {"url": _USER["url"], "ddl-auto": "none"}}}})
        )


_SESSION_FACTORIES = {
    "sessionmaker": (_PlainSessions,),
    "primary-sessionmaker": (_PrimarySessions,),
    "deferred-sessionmaker": (_UserDatabaseAutoConfiguration, _DeferredPlainSessions),
    "deferred-primary-sessionmaker": (_UserDatabaseAutoConfiguration, _DeferredPrimarySessions),
}
_OVERRIDES: dict[str, tuple[type, ...]] = {
    **_SESSION_FACTORIES,
    "engine": (_UserEngine,),
    "registry": (_UserRegistry,),
}


# ---------------------------------------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------------------------------------


@pytest.fixture
async def user_database(
    relational_backend: RelationalBackend, request: pytest.FixtureRequest, tmp_path: Path
) -> AsyncIterator[RelationalBackend]:
    """A second database on the lane of ``relational_backend``: the application's own."""
    if relational_backend.is_embedded:
        yield RelationalBackend(relational_backend.lane, f"sqlite+aiosqlite:///{tmp_path / 'user.db'}")
        return
    server_url: str = request.getfixturevalue(f"{relational_backend.lane}_server_url")
    name = new_database_name()
    backend = RelationalBackend(relational_backend.lane, await create_database(server_url, name))
    try:
        yield backend
    finally:
        await drop_database(server_url, name)


async def _names(backend: RelationalBackend) -> list[str]:
    engine = create_async_engine(backend.url, poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            return sorted((await conn.execute(select(PboItem.name))).scalars())
    finally:
        await engine.dispose()


EVERY_PATH = [
    "auto-unit",
    "infrastructure-unit",
    "injected-session",
    "injected-session-in-transaction",
    "session-provider",
    "transactional",
]


async def _write_through_every_path(ctx: ApplicationContext) -> dict[str, bool]:
    """One row through each path; returns, per path, whether a session of ``_AppSession`` carried it."""
    rows = ctx.get_bean(PboItemRepository)
    writer = ctx.get_bean(PboWriter)
    carried: dict[str, bool] = {}

    async def auto_unit() -> None:
        await rows.save(PboItem(name="auto-unit"))

    async def transactional_write() -> None:
        await writer.through_the_repository("transactional")

    async def session_provider() -> None:
        async with ctx.get_bean(SessionProvider).unit() as session:
            session.add(PboItem(name="session-provider"))

    async def infrastructure() -> None:
        async with infrastructure_unit() as session:
            await session.execute(insert(PboItem).values(name="infrastructure-unit"))

    async def injected_session() -> None:
        session = ctx.get_bean(AsyncSession)  # outside a unit: an ordinary session its owner commits
        try:
            session.add(PboItem(name="injected-session"))
            await session.commit()
        finally:
            await session.close()

    async def injected_session_in_transaction() -> None:
        await writer.through_the_injected_session("injected-session-in-transaction")

    for path, write in (
        ("auto-unit", auto_unit),
        ("transactional", transactional_write),
        ("session-provider", session_provider),
        ("infrastructure-unit", infrastructure),
        ("injected-session", injected_session),
        ("injected-session-in-transaction", injected_session_in_transaction),
    ):
        _APP_BEGINS.clear()
        await write()
        carried[path] = bool(_APP_BEGINS)
    return carried


# ---------------------------------------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------------------------------------


@pytest.mark.backends(SQLITE_FILE, PG)
@pytest.mark.parametrize("override", list(_OVERRIDES))
async def test_every_unit_of_work_runs_on_the_applications_primary_override(
    relational_backend: RelationalBackend, user_database: RelationalBackend, override: str
) -> None:
    await relational_backend.create_tables(PboItem)
    await user_database.create_tables(PboItem)
    _USER["url"] = user_database.url
    _ENGINES.clear()
    _DISPOSED.clear()
    config = relational_backend.config()
    ctx = ApplicationContext(config)
    for candidate in (*_OVERRIDES[override], PboItemRepository, PboWriter):
        ctx.register_bean(candidate)
    await ctx.start()
    try:
        carried = await _write_through_every_path(ctx)
        # A read auto unit on the same primary sees every write.
        assert sorted(item.name for item in await ctx.get_bean(PboItemRepository).find_all()) == EVERY_PATH
        if override in _SESSION_FACTORIES:
            # The application's session factory, not only its engine, carried every path.
            assert carried == dict.fromkeys(EVERY_PATH, True)
        for engine in _ENGINES:
            assert engine.sync_engine.pool.checkedout() == 0
    finally:
        await ctx.stop()
        await DataSourceRegistry.for_config(config).close()

    assert await _names(user_database) == EVERY_PATH
    assert await _names(relational_backend) == []
    # The context disposes the application's engine bean (the engine lifecycle did already); the engine under
    # an application's session factory stays the application's to dispose.
    disposed = list(_DISPOSED)
    assert disposed == (_ENGINES if override == "engine" else [])
    for engine in _ENGINES:
        await engine.dispose()


@pytest.mark.backends(SQLITE_FILE, PG)
async def test_without_an_override_every_unit_of_work_stays_on_the_configured_primary(
    relational_backend: RelationalBackend,
) -> None:
    await relational_backend.create_tables(PboItem)
    ctx = ApplicationContext(relational_backend.config())
    for candidate in (PboItemRepository, PboWriter):
        ctx.register_bean(candidate)
    await ctx.start()
    try:
        carried = await _write_through_every_path(ctx)
        assert carried == dict.fromkeys(EVERY_PATH, False)
        registry = ctx.get_bean(DataSourceRegistry)
        primary = SqlAlchemyTransactionManager.for_datasource(registry.primary)
        assert ctx.get_bean(TransactionManagerRegistry).get() is primary
        assert primary.sessionmaker is registry.primary.sessionmaker
        async with registry.primary.engine.connect() as conn:
            assert (await conn.execute(text("SELECT count(*) FROM pbo_item"))).scalar_one() == len(EVERY_PATH)
    finally:
        await ctx.stop()

    assert await _names(relational_backend) == EVERY_PATH
