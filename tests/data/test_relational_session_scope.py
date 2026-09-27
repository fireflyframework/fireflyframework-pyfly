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
"""The relational auto-configuration hands out ONE ``AsyncSession`` PER INJECTION, and it joins the unit.

``async_session`` used to be a singleton: every repository, every user bean and the engine
lifecycle shared one SQLAlchemy session, which is one transaction, one identity map and one
connection-local state (``SET LOCAL``, a tenant GUC, a search_path) for the whole process.
Anything multi-tenant or concurrent had to refuse the bean and build its own sessions. The
bean is now transient — the factory is the unit of sharing, the session is the unit of work —
and a user ``@bean`` that wants the framework's ``async_sessionmaker`` is deferred until the
auto-configuration has registered it (see ``tests/context/test_bean_method_after_auto_configuration.py``).

The unit of work (WP01) adds:

- the injected session is a ``ScopedAsyncSession``: inside ``@transactional`` it delegates to the unit's
  session (a DAO that injects ``AsyncSession`` joins the transaction, C006), and its ``commit``/``rollback``
  raise there; outside a unit it is an owned session;
- repositories never receive it (their ``session`` parameter is ``NoAutowire``) and resolve their session
  per call (F3, F4);
- the ``session_provider`` bean: ``current()`` and ``async with provider.unit(...)``;
- ``infrastructure_unit()`` joins the bound unit or opens a short one.

Every scenario runs on a SQLite file database, read back through an engine of its own.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Annotated

import pytest

pytest.importorskip("sqlalchemy")

from sqlalchemy import Identity, Integer, String, text  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine  # noqa: E402
from sqlalchemy.orm import Mapped, mapped_column  # noqa: E402
from sqlalchemy.pool import NullPool  # noqa: E402

from pyfly.container.bean import bean  # noqa: E402
from pyfly.container.container import Container  # noqa: E402
from pyfly.container.exceptions import NoSuchBeanError  # noqa: E402
from pyfly.container.stereotypes import configuration, repository, service  # noqa: E402
from pyfly.container.types import NoAutowire  # noqa: E402
from pyfly.context.application_context import ApplicationContext  # noqa: E402
from pyfly.core.config import Config  # noqa: E402
from pyfly.data import transactional  # noqa: E402
from pyfly.data.relational.auto_configuration import EngineLifecycle, RelationalAutoConfiguration  # noqa: E402
from pyfly.data.relational.datasource_registry import DataSourceRegistry  # noqa: E402
from pyfly.data.relational.sqlalchemy.entity import Base  # noqa: E402
from pyfly.data.relational.sqlalchemy.repository import Repository  # noqa: E402
from pyfly.data.relational.sqlalchemy.session import ScopedAsyncSession, SessionProvider  # noqa: E402
from pyfly.data.transaction import IllegalTransactionStateError, infrastructure_unit  # noqa: E402


def _relational_config() -> Config:
    return Config(
        {
            "pyfly": {
                "data": {
                    "relational": {
                        "enabled": "true",
                        "url": "sqlite+aiosqlite:///:memory:",
                        "ddl-auto": "none",
                    }
                }
            }
        }
    )


class UnitOfWork:
    def __init__(self, factory: async_sessionmaker[AsyncSession]) -> None:
        self.factory = factory


class TestTransientSession:
    async def test_two_injections_receive_two_sessions(self) -> None:
        @service
        class FirstConsumer:
            def __init__(self, session: AsyncSession) -> None:
                self.session = session

        @service
        class SecondConsumer:
            def __init__(self, session: AsyncSession) -> None:
                self.session = session

        ctx = ApplicationContext(_relational_config())
        ctx.register_bean(RelationalAutoConfiguration)
        ctx.register_bean(FirstConsumer)
        ctx.register_bean(SecondConsumer)
        await ctx.start()
        try:
            first = ctx.get_bean(FirstConsumer).session
            second = ctx.get_bean(SecondConsumer).session
            assert isinstance(first, AsyncSession)
            assert first is not second
            assert ctx.get_bean(AsyncSession) is not first
        finally:
            await ctx.stop()

    async def test_the_engine_lifecycle_still_closes_its_own_session(self) -> None:
        ctx = ApplicationContext(_relational_config())
        ctx.register_bean(RelationalAutoConfiguration)
        await ctx.start()
        lifecycle = ctx.get_bean(EngineLifecycle)
        await ctx.stop()
        # A transient session was handed to the lifecycle at construction and closed at stop —
        # closing twice is what would break, and it does not.
        await lifecycle.stop()

    async def test_a_user_bean_can_take_the_framework_session_factory(self) -> None:
        """The real-world case the deferral exists for: a per-unit-of-work session wrapper."""

        @configuration
        class UserConfiguration:
            @bean
            def unit_of_work(self, factory: async_sessionmaker[AsyncSession]) -> UnitOfWork:
                return UnitOfWork(factory)

        ctx = ApplicationContext(_relational_config())
        ctx.register_bean(UserConfiguration)
        ctx.register_bean(RelationalAutoConfiguration)
        await ctx.start()
        try:
            uow = ctx.get_bean(UnitOfWork)
            assert uow.factory is ctx.get_bean(async_sessionmaker)
            session = uow.factory()
            assert isinstance(session, AsyncSession)
            await session.close()
        finally:
            await ctx.stop()


# ---------------------------------------------------------------------------------------------------------
# The unit of work: the injected session joins it, repositories are never injected one
# ---------------------------------------------------------------------------------------------------------


class ScopeRow(Base):
    __tablename__ = "scope_row"

    id: Mapped[int] = mapped_column(Integer, Identity(), primary_key=True)
    name: Mapped[str] = mapped_column(String(64))


@repository
class ScopeRowRepository(Repository[ScopeRow, int]):
    pass


@service
class ScopeDao:
    """A DAO that injects ``AsyncSession`` (the C006 shape the old patching never reached)."""

    def __init__(self, session: AsyncSession, rows: ScopeRowRepository, provider: SessionProvider) -> None:
        self.session = session
        self.rows = rows
        self.provider = provider

    @transactional
    async def write_both(self, *, fail: bool) -> tuple[bool, bool]:
        await self.rows.save(ScopeRow(name="through-the-repository"))
        self.session.add(ScopeRow(name="through-the-injected-session"))
        await self.session.flush()
        same = self.provider.current() is self.rows._session
        if fail:
            raise ValueError("both writes roll back")
        return self.session.in_transaction(), same

    @transactional
    async def commit_by_hand(self) -> None:
        await self.session.commit()


class _Holder:
    pass


class _NeedsNothing:
    def __init__(self, holder: Annotated[_Holder | None, NoAutowire] = None) -> None:
        self.holder = holder


class _NeedsItAnyway:
    def __init__(self, holder: Annotated[_Holder, NoAutowire]) -> None:
        self.holder = holder


@pytest.fixture
async def scoped(tmp_path: Path) -> AsyncIterator[tuple[ApplicationContext, str]]:
    url = f"sqlite+aiosqlite:///{tmp_path / 'scope.db'}"
    engine = create_async_engine(url, poolclass=NullPool)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all, tables=[ScopeRow.__table__])
    await engine.dispose()
    config = Config({"pyfly": {"data": {"relational": {"enabled": "true", "url": url, "ddl-auto": "none"}}}})
    ctx = ApplicationContext(config)
    for candidate in (RelationalAutoConfiguration, ScopeRowRepository, ScopeDao):
        ctx.register_bean(candidate)
    await ctx.start()
    try:
        yield ctx, url
        registry = ctx.get_bean(DataSourceRegistry)
        assert sum(ds.engine.sync_engine.pool.checkedout() for ds in registry.all_datasources()) == 0
    finally:
        await ctx.stop()


async def _names(url: str) -> list[str]:
    engine = create_async_engine(url, poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            return [r[0] for r in (await conn.execute(text("SELECT name FROM scope_row ORDER BY id"))).all()]
    finally:
        await engine.dispose()


class TestTheInjectedSessionJoinsTheUnit:
    async def test_it_is_a_scoped_session(self, scoped: tuple[ApplicationContext, str]) -> None:
        ctx, _url = scoped
        dao = ctx.get_bean(ScopeDao)
        assert isinstance(dao.session, ScopedAsyncSession)
        assert dao.session.datasource == "primary"

    async def test_it_writes_inside_the_unit(self, scoped: tuple[ApplicationContext, str]) -> None:
        ctx, url = scoped
        dao = ctx.get_bean(ScopeDao)
        with pytest.raises(ValueError):
            await dao.write_both(fail=True)
        assert await _names(url) == []
        assert await dao.write_both(fail=False) == (True, True)
        assert await _names(url) == ["through-the-repository", "through-the-injected-session"]

    async def test_commit_inside_a_unit_is_refused(self, scoped: tuple[ApplicationContext, str]) -> None:
        ctx, _url = scoped
        with pytest.raises(IllegalTransactionStateError, match="shared EntityManager"):
            await ctx.get_bean(ScopeDao).commit_by_hand()

    async def test_outside_a_unit_it_is_an_owned_session(self, scoped: tuple[ApplicationContext, str]) -> None:
        ctx, url = scoped
        session = ctx.get_bean(ScopeDao).session
        assert session.in_transaction() is False
        session.add(ScopeRow(name="owned"))
        await session.commit()
        await session.close()
        assert await _names(url) == ["owned"]


class TestRepositoriesResolveTheirSession:
    async def test_a_di_built_repository_holds_no_session(self, scoped: tuple[ApplicationContext, str]) -> None:
        ctx, _url = scoped
        rows = ctx.get_bean(ScopeRowRepository)
        assert rows._manual_session is None
        assert rows.datasource == "primary"
        with pytest.raises(IllegalTransactionStateError, match="resolves one per call"):
            _ = rows._session

    async def test_an_explicit_session_is_manual_mode(self, scoped: tuple[ApplicationContext, str]) -> None:
        ctx, url = scoped
        factory: async_sessionmaker[AsyncSession] = ctx.get_bean(async_sessionmaker)
        async with factory() as session:
            rows = ScopeRowRepository(session=session)
            await rows.save(ScopeRow(name="manual"))
            assert rows._session is session
            await session.commit()
        assert await _names(url) == ["manual"]

    def test_the_container_leaves_a_no_autowire_parameter_alone(self) -> None:
        container = Container()
        container.register(_Holder)
        container.register(_NeedsNothing)
        container.register(_NeedsItAnyway)
        assert container.resolve(_NeedsNothing).holder is None
        with pytest.raises(NoSuchBeanError):
            container.resolve(_NeedsItAnyway)


class TestSessionProviderAndInfrastructureUnits:
    async def test_current_is_the_units_session(self, scoped: tuple[ApplicationContext, str]) -> None:
        ctx, _url = scoped
        provider = ctx.get_bean(SessionProvider)
        assert provider.current() is None

        @transactional
        async def inside() -> bool:
            session = provider.current()
            return session is not None and session is ctx.get_bean(ScopeRowRepository)._session

        assert await inside() is True

    async def test_unit_opens_a_short_unit_that_commits(self, scoped: tuple[ApplicationContext, str]) -> None:
        ctx, url = scoped
        provider = ctx.get_bean(SessionProvider)
        async with provider.unit() as session:
            await session.execute(text("INSERT INTO scope_row (name) VALUES ('provider-unit')"))
        async with provider.unit(read_only=True) as session:
            assert (await session.execute(text("SELECT count(*) FROM scope_row"))).scalar_one() == 1
        assert await _names(url) == ["provider-unit"]

    async def test_infrastructure_unit_joins_the_business_transaction(
        self, scoped: tuple[ApplicationContext, str]
    ) -> None:
        ctx, url = scoped
        rows = ctx.get_bean(ScopeRowRepository)

        @transactional
        async def business(*, fail: bool) -> None:
            await rows.save(ScopeRow(name="order"))
            async with infrastructure_unit("primary", single_statement=True) as session:
                await session.execute(text("INSERT INTO scope_row (name) VALUES ('event')"))
            if fail:
                raise ValueError("no dual write: the event rolls back with the order")

        with pytest.raises(ValueError):
            await business(fail=True)
        assert await _names(url) == []
        await business(fail=False)
        assert await _names(url) == ["order", "event"]

    async def test_infrastructure_unit_owns_a_short_unit_outside_one(
        self, scoped: tuple[ApplicationContext, str]
    ) -> None:
        ctx, url = scoped
        registry = ctx.get_bean(DataSourceRegistry)
        with pytest.raises(RuntimeError):
            async with infrastructure_unit(registry.primary) as session:
                await session.execute(text("INSERT INTO scope_row (name) VALUES ('rolled-back')"))
                raise RuntimeError("the adapter failed")
        async with infrastructure_unit(registry.primary) as session:
            await session.execute(text("INSERT INTO scope_row (name) VALUES ('committed')"))
        assert await _names(url) == ["committed"]


class TestTheInstalledTransactionManagers:
    async def test_a_context_installs_its_managers_while_it_runs(self, tmp_path: Path) -> None:
        from pyfly.data.transaction import TransactionManagerRegistry, installed_registry

        def _context(name: str) -> ApplicationContext:
            url = f"sqlite+aiosqlite:///{tmp_path / name}"
            config = Config({"pyfly": {"data": {"relational": {"enabled": "true", "url": url, "ddl-auto": "none"}}}})
            ctx = ApplicationContext(config)
            ctx.register_bean(RelationalAutoConfiguration)
            return ctx

        first, second = _context("first.db"), _context("second.db")
        await first.start()
        first_managers = first.get_bean(TransactionManagerRegistry)
        assert installed_registry() is first_managers
        await second.start()
        second_managers = second.get_bean(TransactionManagerRegistry)
        assert installed_registry() is second_managers
        await first.stop()
        assert installed_registry() is second_managers  # only the installed registry is removed
        await second.stop()
        assert installed_registry() is None
