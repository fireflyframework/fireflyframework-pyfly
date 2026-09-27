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
"""Scoped instances are cached per bean definition, not per class name (C030).

The REQUEST, SESSION and custom-scope resolvers cached an instance under
``__pyfly_bean_<class qualname>``. The container keeps two ``@bean`` methods that return the same
class as two named registrations, but both mapped to that one cache slot: the second name received
the first one's object. Two request- or refresh-scoped datasources of one type therefore shared a
database, the writes of one landed in the other, and after a refresh the mapping flipped to
whichever name was resolved first. Two classes with one ``__qualname__`` in different modules
collided the same way.

These tests run on two SQLite file databases and check where the rows land.
"""

from __future__ import annotations

import types
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Annotated, Any

import pytest

pytest.importorskip("sqlalchemy")

from sqlalchemy import text  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine  # noqa: E402

from pyfly.container import Qualifier, bean, configuration  # noqa: E402
from pyfly.container.container import Container  # noqa: E402
from pyfly.container.refresh_scope import REFRESH_SCOPE_NAME, RefreshScope  # noqa: E402
from pyfly.container.types import Scope  # noqa: E402
from pyfly.context.application_context import ApplicationContext  # noqa: E402
from pyfly.context.refresh import ContextRefresher  # noqa: E402
from pyfly.context.request_context import HTTP_SESSION_KEY, RequestContext  # noqa: E402
from pyfly.core.config import Config  # noqa: E402
from pyfly.data.relational.routing import RoutingSessionFactory  # noqa: E402
from pyfly.session.session import HttpSession  # noqa: E402

_ENGINES: dict[str, AsyncEngine] = {}


@pytest.fixture
async def databases(tmp_path: Path) -> AsyncIterator[dict[str, AsyncEngine]]:
    for name in ("reporting", "analytics"):
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / f'{name}.db'}")
        async with engine.begin() as conn:
            await conn.execute(text("CREATE TABLE note (body TEXT NOT NULL)"))
        _ENGINES[name] = engine
    try:
        yield _ENGINES
    finally:
        for engine in _ENGINES.values():
            await engine.dispose()
        _ENGINES.clear()


@pytest.fixture(autouse=True)
def _clean_request_context() -> Iterator[None]:
    RequestContext.clear()
    yield
    RequestContext.clear()


async def _rows(engine: AsyncEngine) -> list[str]:
    async with engine.connect() as conn:
        return [row[0] for row in (await conn.execute(text("SELECT body FROM note ORDER BY rowid")))]


async def _write(factory: async_sessionmaker[AsyncSession], body: str) -> None:
    async with factory() as session, session.begin():
        await session.execute(text("INSERT INTO note (body) VALUES (:body)"), {"body": body})


@configuration
class _RefreshScopedFactories:
    @bean(name="reporting_sessions", scope=REFRESH_SCOPE_NAME)  # type: ignore[arg-type]
    def reporting_sessions(self) -> async_sessionmaker:  # type: ignore[type-arg]
        return async_sessionmaker(_ENGINES["reporting"], expire_on_commit=False)

    @bean(name="analytics_sessions", scope=REFRESH_SCOPE_NAME)  # type: ignore[arg-type]
    def analytics_sessions(self) -> async_sessionmaker:  # type: ignore[type-arg]
        return async_sessionmaker(_ENGINES["analytics"], expire_on_commit=False)


async def test_two_refresh_scoped_factories_of_one_type_keep_their_own_database(
    databases: dict[str, AsyncEngine],
) -> None:
    ctx = ApplicationContext(Config({}))
    ctx.register_bean(_RefreshScopedFactories)
    await ctx.start()
    try:
        reporting = ctx.get_bean_by_name("reporting_sessions")
        analytics = ctx.get_bean_by_name("analytics_sessions")
        assert reporting is not analytics
        await _write(reporting, "r1-reporting")
        await _write(analytics, "r1-analytics")

        await ctx.get_bean(ContextRefresher).refresh()
        # After the refresh the order of the first resolution must not decide the mapping.
        await _write(ctx.get_bean_by_name("analytics_sessions"), "r2-analytics")
        await _write(ctx.get_bean_by_name("reporting_sessions"), "r2-reporting")
    finally:
        await ctx.stop()

    assert await _rows(databases["reporting"]) == ["r1-reporting", "r2-reporting"]
    assert await _rows(databases["analytics"]) == ["r1-analytics", "r2-analytics"]


def _relational(tmp_path: Path) -> Config:
    """An application with the relational auto-configuration on its own primary database."""
    url = f"sqlite+aiosqlite:///{tmp_path / 'primary.db'}"
    return Config({"pyfly": {"data": {"relational": {"enabled": "true", "url": url, "ddl-auto": "none"}}}})


async def _database_of(session: AsyncSession) -> str:
    async with session:
        rows = (await session.execute(text("PRAGMA database_list"))).all()
    return Path(rows[0][2]).name


async def test_two_refresh_scoped_factories_keep_their_database_beside_the_relational_primary(
    databases: dict[str, AsyncEngine], tmp_path: Path
) -> None:
    """The C030 scenario as an application writes it: the relational auto-configuration is on, so the
    two scoped factories share their class with the primary ``async_session_factory``. start() used to
    fail on the ambiguous primary, or the primary was replaced by a scoped factory."""
    ctx = ApplicationContext(_relational(tmp_path))
    ctx.register_bean(_RefreshScopedFactories)
    await ctx.start()
    try:
        assert await _database_of(ctx.get_bean(AsyncSession)) == "primary.db"
        assert await _database_of(ctx.get_bean(RoutingSessionFactory).primary()) == "primary.db"
        await _write(ctx.get_bean_by_name("reporting_sessions"), "r1-reporting")
        await _write(ctx.get_bean_by_name("analytics_sessions"), "r1-analytics")

        await ctx.get_bean(ContextRefresher).refresh()

        await _write(ctx.get_bean_by_name("analytics_sessions"), "r2-analytics")
        await _write(ctx.get_bean_by_name("reporting_sessions"), "r2-reporting")
        assert await _database_of(ctx.get_bean(AsyncSession)) == "primary.db"
    finally:
        await ctx.stop()

    assert await _rows(databases["reporting"]) == ["r1-reporting", "r2-reporting"]
    assert await _rows(databases["analytics"]) == ["r1-analytics", "r2-analytics"]


@configuration
class _RefreshScopedEngines:
    @bean(name="reporting_engine", scope=REFRESH_SCOPE_NAME)  # type: ignore[arg-type]
    def reporting_engine(self) -> AsyncEngine:
        return create_async_engine(_ENGINES["reporting"].url)

    @bean(name="analytics_engine", scope=REFRESH_SCOPE_NAME)  # type: ignore[arg-type]
    def analytics_engine(self) -> AsyncEngine:
        return create_async_engine(_ENGINES["analytics"].url)


async def test_two_refresh_scoped_engines_keep_their_database_beside_the_relational_primary(
    databases: dict[str, AsyncEngine], tmp_path: Path
) -> None:
    ctx = ApplicationContext(_relational(tmp_path))
    ctx.register_bean(_RefreshScopedEngines)
    await ctx.start()
    try:
        assert ctx.get_bean(AsyncEngine).url.database == str(tmp_path / "primary.db")
        assert await _database_of(ctx.get_bean(AsyncSession)) == "primary.db"
        for cycle in ("r1", "r2"):
            for name in ("analytics", "reporting"):
                engine = ctx.get_bean_by_name(f"{name}_engine")
                await _write(async_sessionmaker(engine), f"{cycle}-{name}")
            await ctx.get_bean(ContextRefresher).refresh()
    finally:
        await ctx.stop()

    assert await _rows(databases["reporting"]) == ["r1-reporting", "r2-reporting"]
    assert await _rows(databases["analytics"]) == ["r1-analytics", "r2-analytics"]


@configuration
class _RequestScopedSessions:
    @bean(name="orders_session", scope=Scope.REQUEST)
    def orders_session(self) -> AsyncSession:
        return AsyncSession(_ENGINES["reporting"], expire_on_commit=False)

    @bean(name="audit_session", scope=Scope.REQUEST)
    def audit_session(self) -> AsyncSession:
        return AsyncSession(_ENGINES["analytics"], expire_on_commit=False)


class _OrderRecorder:
    def __init__(
        self,
        orders: Annotated[AsyncSession, Qualifier("orders_session")],
        audit: Annotated[AsyncSession, Qualifier("audit_session")],
    ) -> None:
        self.orders = orders
        self.audit = audit

    async def record(self, order: str) -> None:
        await self.orders.execute(text("INSERT INTO note (body) VALUES (:body)"), {"body": order})
        await self.audit.execute(text("INSERT INTO note (body) VALUES (:body)"), {"body": f"audit: {order}"})
        await self.orders.commit()
        await self.audit.commit()
        await self.orders.close()
        await self.audit.close()


async def test_two_request_scoped_sessions_of_one_type_keep_their_own_database(
    databases: dict[str, AsyncEngine],
) -> None:
    ctx = ApplicationContext(Config({}))
    ctx.register_bean(_RequestScopedSessions)
    ctx.register_bean(_OrderRecorder, scope=Scope.REQUEST)
    await ctx.start()
    try:
        RequestContext.init()
        recorder = ctx.get_bean(_OrderRecorder)
        assert recorder.orders is not recorder.audit
        await recorder.record("order-42")
    finally:
        await ctx.stop()

    assert await _rows(databases["reporting"]) == ["order-42"]
    assert await _rows(databases["analytics"]) == ["audit: order-42"]


def _settings_class(module: str) -> type:
    """A ``DataSourceSettings`` class declared in *module*: same ``__qualname__``, another module."""

    def body(namespace: dict[str, Any]) -> None:
        namespace["__module__"] = module
        namespace["origin"] = module

    return types.new_class("DataSourceSettings", (), exec_body=body)


def test_refresh_scoped_classes_with_one_qualname_in_two_modules_do_not_collide() -> None:
    billing = _settings_class("billing.config")
    reporting = _settings_class("reporting.config")
    container = Container()
    container.register_scope(REFRESH_SCOPE_NAME, RefreshScope())
    container.register(billing, scope=REFRESH_SCOPE_NAME)
    container.register(reporting, scope=REFRESH_SCOPE_NAME)

    assert isinstance(container.resolve(billing), billing)
    assert isinstance(container.resolve(reporting), reporting)


class _Cart:
    pass


def test_a_session_bean_stored_under_the_previous_key_is_found_and_moved() -> None:
    container = Container()
    container.register(_Cart, scope=Scope.SESSION)
    stored = _Cart()
    session = HttpSession("sid-legacy", {f"__pyfly_bean_{_Cart.__qualname__}": stored})
    RequestContext.init().set(HTTP_SESSION_KEY, session)

    assert container.resolve(_Cart) is stored
    assert session.get_attribute(f"__pyfly_bean_{_Cart.__qualname__}") is None
    assert container.resolve(_Cart) is stored  # now cached under the per-definition key


def test_a_previous_key_shared_by_two_session_beans_is_not_guessed() -> None:
    container = Container()
    container.register(_Cart, scope=Scope.SESSION, name="guest_cart")
    container.register(_Cart, scope=Scope.SESSION, name="member_cart")
    stored = _Cart()
    session = HttpSession("sid-ambiguous", {f"__pyfly_bean_{_Cart.__qualname__}": stored})
    RequestContext.init().set(HTTP_SESSION_KEY, session)

    guest = container.resolve_by_name("guest_cart")
    member = container.resolve_by_name("member_cart")

    assert guest is not stored
    assert member is not stored
    assert guest is not member
