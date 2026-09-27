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
"""An application can replace the auto-configured data beans (C042).

The relational and document auto-configurations declared their engine, session factory, routing
factory, registry and Mongo client unconditionally, and the container kept the LAST registration of
a concrete type. A user ``AsyncEngine`` (the only way to pass driver arguments before 26.09.08) or
``async_sessionmaker``, even ``@bean(primary=True)``, was therefore shadowed by the framework's:
repositories, health and DDL used the configured URL, and the user's engine was never disposed.
Each of those beans now backs off when the application declares its own, and resolving a type that
two beans share picks the ``@primary`` one or fails instead of returning the last registered.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Annotated, Any

import pytest

pytest.importorskip("sqlalchemy")

from sqlalchemy import Integer, String, event, text  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine  # noqa: E402
from sqlalchemy.orm import Mapped, ORMExecuteState, Session, mapped_column  # noqa: E402

from pyfly.container import NoUniqueBeanError, Qualifier, bean, configuration, repository  # noqa: E402
from pyfly.container.refresh_scope import REFRESH_SCOPE_NAME, scoped_proxy  # noqa: E402
from pyfly.container.types import Scope  # noqa: E402
from pyfly.context.application_context import ApplicationContext  # noqa: E402
from pyfly.context.conditions import auto_configuration  # noqa: E402
from pyfly.context.refresh import ContextRefresher  # noqa: E402
from pyfly.context.request_context import RequestContext  # noqa: E402
from pyfly.core.config import Config  # noqa: E402
from pyfly.data.relational.auto_configuration import EngineLifecycle  # noqa: E402
from pyfly.data.relational.datasource_registry import DataSourceRegistry, datasource_of  # noqa: E402
from pyfly.data.relational.health import SqlAlchemyHealthIndicator  # noqa: E402
from pyfly.data.relational.routing import RoutingSessionFactory  # noqa: E402
from pyfly.data.relational.sqlalchemy.entity import Base  # noqa: E402
from pyfly.data.relational.sqlalchemy.repository import Repository  # noqa: E402

try:
    from pymongo import AsyncMongoClient
except ImportError:  # only the Mongo test needs the driver, and it skips itself without it
    AsyncMongoClient = None  # type: ignore[assignment,misc]

_URLS: dict[str, str] = {}
_DISPOSED: list[str] = []


def _config(tmp_path: Path) -> Config:
    _URLS.update(
        {
            "auto": f"sqlite+aiosqlite:///{tmp_path / 'auto.db'}",
            "user": f"sqlite+aiosqlite:///{tmp_path / 'user.db'}",
            "other": f"sqlite+aiosqlite:///{tmp_path / 'other.db'}",
        }
    )
    return Config({"pyfly": {"data": {"relational": {"enabled": "true", "url": _URLS["auto"], "ddl-auto": "none"}}}})


def _engine(name: str) -> AsyncEngine:
    engine = create_async_engine(_URLS[name], connect_args={"timeout": 7})
    event.listen(engine.sync_engine, "engine_disposed", lambda _engine: _DISPOSED.append(name))
    return engine


async def _database_of(factory: async_sessionmaker[AsyncSession]) -> str:
    async with factory() as session:
        rows = (await session.execute(text("PRAGMA database_list"))).all()
    return Path(rows[0][2]).name


@configuration
class _UserEngine:
    @bean
    def user_engine(self) -> AsyncEngine:
        return _engine("user")


async def test_a_user_engine_replaces_the_auto_configured_one(tmp_path: Path) -> None:
    _DISPOSED.clear()
    ctx = ApplicationContext(_config(tmp_path))
    ctx.register_bean(_UserEngine)
    await ctx.start()
    try:
        engine = ctx.get_bean(AsyncEngine)
        assert str(engine.url) == _URLS["user"]
        assert [str(each.url) for each in ctx.get_beans_of_type(AsyncEngine)] == [_URLS["user"]]
        factory = ctx.get_bean(async_sessionmaker)
        assert factory.kw["bind"] is engine
        assert await _database_of(factory) == "user.db"
        assert ctx.get_bean(RoutingSessionFactory) is not None
        assert ctx.get_bean(EngineLifecycle)._engine is engine
        assert (await ctx.get_bean(SqlAlchemyHealthIndicator).health()).status == "UP"
    finally:
        await ctx.stop()

    assert _DISPOSED == ["user"]  # the application's engine is disposed at stop, once


def _split_warnings(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [record for record in caplog.records if record.getMessage() == "relational_engine_not_in_registry"]


async def test_a_user_engine_beside_a_configured_url_warns_that_the_primary_is_split(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The registry keeps building its primary from the URL for every module that looks it up, while the
    sessions and repositories use the application's engine: two primaries, and nothing said so."""
    ctx = ApplicationContext(_config(tmp_path))
    ctx.register_bean(_UserEngine)
    with caplog.at_level(logging.WARNING, logger="pyfly.data.relational.auto_configuration"):
        await ctx.start()
    try:
        warnings = _split_warnings(caplog)
        assert len(warnings) == 1
        assert warnings[0].engine == _URLS["user"]  # type: ignore[attr-defined]
        assert "pyfly.data.relational.datasources.<name>" in warnings[0].hint  # type: ignore[attr-defined]
        # The split it reports.
        assert str(ctx.get_bean(DataSourceRegistry).primary.engine.url) == _URLS["auto"]
        assert await _database_of(ctx.get_bean(async_sessionmaker)) == "user.db"
    finally:
        await ctx.stop()


async def test_a_user_engine_without_a_configured_url_is_the_only_primary(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    _config(tmp_path)
    ctx = ApplicationContext(Config({"pyfly": {"data": {"relational": {"enabled": "true", "ddl-auto": "none"}}}}))
    ctx.register_bean(_UserEngine)
    with caplog.at_level(logging.WARNING, logger="pyfly.data.relational.auto_configuration"):
        await ctx.start()
    try:
        assert _split_warnings(caplog) == []
        assert await _database_of(ctx.get_bean(async_sessionmaker)) == "user.db"
    finally:
        await ctx.stop()


async def test_the_registry_engine_is_no_split(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    ctx = ApplicationContext(_config(tmp_path))
    with caplog.at_level(logging.WARNING, logger="pyfly.data.relational.auto_configuration"):
        await ctx.start()
    try:
        assert _split_warnings(caplog) == []
        assert await _database_of(ctx.get_bean(async_sessionmaker)) == "auto.db"
    finally:
        await ctx.stop()


async def test_a_connection_in_use_on_the_user_engine_at_stop_is_closed_when_returned(tmp_path: Path) -> None:
    """The engine lifecycle disposes the application's engine; a connection in use then is closed when it is
    returned, instead of going back into the disposed pool."""
    ctx = ApplicationContext(_config(tmp_path))
    ctx.register_bean(_UserEngine)
    await ctx.start()
    engine = ctx.get_bean(AsyncEngine)
    closed: list[Any] = []
    event.listen(engine.sync_engine, "close", lambda dbapi_connection, _record: closed.append(dbapi_connection))
    async with engine.connect() as conn:
        in_use = (await conn.get_raw_connection()).dbapi_connection
        await ctx.stop()
        assert (await conn.execute(text("SELECT 1"))).scalar_one() == 1
        assert in_use not in closed

    assert in_use in closed


_BUILT: list[AsyncEngine] = []


@configuration
class _TwoUserEngines:
    @bean
    def other_engine(self) -> AsyncEngine:
        _BUILT.append(_engine("other"))
        return _BUILT[-1]

    @bean(primary=True)
    def main_engine(self) -> AsyncEngine:
        _BUILT.append(_engine("user"))
        return _BUILT[-1]


async def test_the_primary_of_two_user_engines_is_injected(tmp_path: Path) -> None:
    _BUILT.clear()
    ctx = ApplicationContext(_config(tmp_path))
    ctx.register_bean(_TwoUserEngines)
    await ctx.start()
    try:
        assert str(ctx.get_bean(AsyncEngine).url) == _URLS["user"]
        assert await _database_of(ctx.get_bean(async_sessionmaker)) == "user.db"
    finally:
        await ctx.stop()
        for engine in _BUILT:
            await engine.dispose()


class _Widget:
    def __init__(self, label: str) -> None:
        self.label = label


@configuration
class _TwoWidgets:
    @bean
    def first_widget(self) -> _Widget:
        return _Widget("first")

    @bean
    def second_widget(self) -> _Widget:
        return _Widget("second")


async def test_two_beans_of_one_type_without_a_primary_are_ambiguous() -> None:
    ctx = ApplicationContext(Config({}))
    ctx.register_bean(_TwoWidgets)
    await ctx.start()
    try:
        with pytest.raises(NoUniqueBeanError):
            ctx.get_bean(_Widget)
        assert ctx.get_bean_by_name("first_widget").label == "first"
        assert sorted(widget.label for widget in ctx.get_beans_of_type(_Widget)) == ["first", "second"]
    finally:
        await ctx.stop()


@configuration
class _UserSessionFactory:
    @bean
    def user_sessions(self) -> async_sessionmaker[AsyncSession]:
        return async_sessionmaker(_engine("user"), expire_on_commit=False)


async def test_a_user_session_factory_replaces_the_auto_configured_one(tmp_path: Path) -> None:
    _DISPOSED.clear()
    ctx = ApplicationContext(_config(tmp_path))
    ctx.register_bean(_UserSessionFactory)
    await ctx.start()
    try:
        factory = ctx.get_bean(async_sessionmaker)
        assert await _database_of(factory) == "user.db"
        assert ctx.get_bean(RoutingSessionFactory)._primary is factory
    finally:
        await ctx.stop()
        await factory.kw["bind"].dispose()


class _UserRegistry(DataSourceRegistry):
    pass


@configuration
class _UserRegistryConfiguration:
    @bean
    def user_registry(self, config: Config) -> DataSourceRegistry:
        return _UserRegistry(config)


async def test_a_user_datasource_registry_replaces_the_auto_configured_one(tmp_path: Path) -> None:
    ctx = ApplicationContext(_config(tmp_path))
    ctx.register_bean(_UserRegistryConfiguration)
    await ctx.start()
    registry = ctx.get_bean(DataSourceRegistry)
    # The relational beans still take their engine from the configuration's own registry.
    engine = ctx.get_bean(AsyncEngine)
    try:
        assert isinstance(registry, _UserRegistry)
        async with registry.primary.engine.connect() as conn:
            assert (await conn.execute(text("SELECT 1"))).scalar_one() == 1
        async with engine.connect() as conn:
            assert (await conn.execute(text("SELECT 1"))).scalar_one() == 1
    finally:
        await ctx.stop()

    assert registry.closed  # the context disposed the application's registry
    shared = datasource_of(engine)
    assert shared is not None and shared.registry is not None
    assert shared.registry.closed  # and the configuration's, which nothing else would close


@configuration
class _UserMongo:
    @bean
    def user_mongo_client(self) -> AsyncMongoClient:  # type: ignore[type-arg]
        return AsyncMongoClient("mongodb://user-mongo.invalid:27017", connect=False)


async def test_a_user_mongo_client_replaces_the_auto_configured_one() -> None:
    pytest.importorskip("pymongo")
    pytest.importorskip("beanie")
    from pyfly.data.document.mongodb.initializer import BeanieInitializer

    ctx = ApplicationContext(Config({"pyfly": {"data": {"document": {"enabled": "true"}}}}))
    ctx.register_bean(_UserMongo)
    await ctx.start()
    try:
        client = ctx.get_bean(AsyncMongoClient)
        assert client.topology_description.server_descriptions()  # the user's seed list
        assert ("user-mongo.invalid", 27017) in client.topology_description.server_descriptions()
        assert len(ctx.get_beans_of_type(AsyncMongoClient)) == 1
        assert ctx.get_bean(BeanieInitializer)._motor_client is client
    finally:
        await ctx.stop()


# ---------------------------------------------------------------------------
# A request- or refresh-scoped bean of a data type is a SECOND database, never a replacement for the
# primary. The back-off counted registrations of every scope, so the application's own patterns (two
# refresh-scoped session factories, a request-scoped tenant factory, a proxied refresh-scoped engine)
# switched the primary beans off in a relational application: start() failed ("No matching bean is
# registered", "No active request context") or every session, the routing factory and each repository
# silently moved to the secondary database. Only a singleton replaces a data bean now, and the
# auto-configured beans are the primary candidates of their type.
# ---------------------------------------------------------------------------


class _DatabaseOwner(Base):
    __tablename__ = "wp07_database_owner"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(20))


@repository
class _DatabaseOwnerRepository(Repository[_DatabaseOwner, int]):
    pass


async def _own(tmp_path: Path, name: str, owner: str) -> str:
    """Create database ``<name>.db`` holding one owner row, and return its URL."""
    url = f"sqlite+aiosqlite:///{tmp_path / f'{name}.db'}"
    engine = create_async_engine(url)
    try:
        async with engine.begin() as conn:
            table = Base.metadata.tables[_DatabaseOwner.__tablename__]
            await conn.run_sync(table.create)
            await conn.execute(table.insert().values(id=1, name=owner))
    finally:
        await engine.dispose()
    _URLS[name] = url
    return url


async def _relational_app(tmp_path: Path, *secondaries: str) -> Config:
    """A relational application on ``auto.db`` (owner "primary"), plus one database per secondary."""
    config = _config(tmp_path)
    await _own(tmp_path, "auto", "primary")
    for name in secondaries:
        await _own(tmp_path, name, name)
    return config


async def _session_database(session: AsyncSession) -> str:
    async with session:
        rows = (await session.execute(text("PRAGMA database_list"))).all()
    return Path(rows[0][2]).name


async def _assert_the_primary_stays(ctx: ApplicationContext) -> None:
    """Every primary data bean, and a repository, still use ``pyfly.data.relational.url``."""
    assert str(ctx.get_bean(AsyncEngine).url) == _URLS["auto"]
    assert await _database_of(ctx.get_bean(async_sessionmaker)) == "auto.db"
    assert await _session_database(ctx.get_bean(AsyncSession)) == "auto.db"
    assert await _session_database(ctx.get_bean(RoutingSessionFactory).primary()) == "auto.db"
    assert ctx.get_bean(EngineLifecycle)._engine is ctx.get_bean(AsyncEngine)
    owners = await ctx.get_bean(_DatabaseOwnerRepository).find_all()
    assert [owner.name for owner in owners] == ["primary"]


_SCOPED_ENGINES: list[AsyncEngine] = []


def _scoped_sessions(name: str) -> async_sessionmaker[AsyncSession]:
    engine = create_async_engine(_URLS[name])
    _SCOPED_ENGINES.append(engine)
    return async_sessionmaker(engine, expire_on_commit=False)


async def _owner_of(sessions: async_sessionmaker[AsyncSession]) -> str:
    async with sessions() as session:
        return str((await session.execute(text("SELECT name FROM wp07_database_owner"))).scalar_one())


async def _engine_owner(engine: AsyncEngine) -> str:
    async with engine.connect() as conn:
        return str((await conn.execute(text("SELECT name FROM wp07_database_owner"))).scalar_one())


@configuration
class _TenantSessions:
    """The request-scoped tenant factory of the scoped ``@bean`` guide, by its parametrized hint."""

    @bean(scope=Scope.REQUEST)
    def tenant_sessions(self) -> async_sessionmaker[AsyncSession]:
        context = RequestContext.current()
        assert context is not None, "a REQUEST-scoped factory ran outside a request"
        return _scoped_sessions(str(context.get("tenant")))


class _TenantReader:
    def __init__(self, sessions: Annotated[async_sessionmaker[AsyncSession], Qualifier("tenant_sessions")]) -> None:
        self.sessions = sessions


async def test_a_request_scoped_session_factory_leaves_the_primary_in_place(tmp_path: Path) -> None:
    _SCOPED_ENGINES.clear()
    RequestContext.clear()
    ctx = ApplicationContext(await _relational_app(tmp_path, "acme", "globex"))
    ctx.register_bean(_TenantSessions)
    ctx.register_bean(_TenantReader, scope=Scope.REQUEST)
    ctx.register_bean(_DatabaseOwnerRepository)
    await ctx.start()
    try:
        await _assert_the_primary_stays(ctx)
        for tenant in ("acme", "globex"):
            RequestContext.init().set("tenant", tenant)
            assert await _owner_of(ctx.get_bean(_TenantReader).sessions) == tenant
            assert await _owner_of(ctx.get_bean(async_sessionmaker)) == "primary"  # by type: the primary
    finally:
        RequestContext.clear()
        await ctx.stop()
        for engine in _SCOPED_ENGINES:
            await engine.dispose()


@configuration
class _AuditSessions:
    @bean(name="audit_sessions", scope=REFRESH_SCOPE_NAME)
    def audit_sessions(self) -> async_sessionmaker[AsyncSession]:
        return _scoped_sessions("audit")


async def test_a_refresh_scoped_session_factory_leaves_the_primary_in_place(tmp_path: Path) -> None:
    _SCOPED_ENGINES.clear()
    ctx = ApplicationContext(await _relational_app(tmp_path, "audit"))
    ctx.register_bean(_AuditSessions)
    ctx.register_bean(_DatabaseOwnerRepository)
    await ctx.start()
    try:
        await _assert_the_primary_stays(ctx)
        assert await _owner_of(ctx.get_bean_by_name("audit_sessions")) == "audit"
        await ctx.get_bean(ContextRefresher).refresh()
        await _assert_the_primary_stays(ctx)
        assert await _owner_of(ctx.get_bean_by_name("audit_sessions")) == "audit"
    finally:
        await ctx.stop()
        for engine in _SCOPED_ENGINES:
            await engine.dispose()


@configuration
class _TwoRefreshScopedSessionFactories:
    """The C030 case: two refresh-scoped factories of one type, beside the relational primary."""

    @bean(name="reporting_sessions", scope=REFRESH_SCOPE_NAME)
    def reporting_sessions(self) -> async_sessionmaker[AsyncSession]:
        return _scoped_sessions("reporting")

    @bean(name="analytics_sessions", scope=REFRESH_SCOPE_NAME)
    def analytics_sessions(self) -> async_sessionmaker[AsyncSession]:
        return _scoped_sessions("analytics")


async def test_two_refresh_scoped_session_factories_leave_the_primary_in_place(tmp_path: Path) -> None:
    _SCOPED_ENGINES.clear()
    ctx = ApplicationContext(await _relational_app(tmp_path, "reporting", "analytics"))
    ctx.register_bean(_TwoRefreshScopedSessionFactories)
    ctx.register_bean(_DatabaseOwnerRepository)
    await ctx.start()
    try:
        for _refresh in range(2):
            await _assert_the_primary_stays(ctx)
            assert await _owner_of(ctx.get_bean_by_name("analytics_sessions")) == "analytics"
            assert await _owner_of(ctx.get_bean_by_name("reporting_sessions")) == "reporting"
            await ctx.get_bean(ContextRefresher).refresh()
    finally:
        await ctx.stop()
        for engine in _SCOPED_ENGINES:
            await engine.dispose()


@configuration
class _ReportingEngine:
    @scoped_proxy
    @bean(scope=REFRESH_SCOPE_NAME)
    def reporting_engine(self) -> AsyncEngine:
        return create_async_engine(_URLS["reporting"])


async def test_a_proxied_refresh_scoped_engine_leaves_the_primary_in_place(tmp_path: Path) -> None:
    ctx = ApplicationContext(await _relational_app(tmp_path, "reporting"))
    ctx.register_bean(_ReportingEngine)
    ctx.register_bean(_DatabaseOwnerRepository)
    await ctx.start()
    try:
        await _assert_the_primary_stays(ctx)
        assert (await ctx.get_bean(SqlAlchemyHealthIndicator).health()).status == "UP"
        reporting = ctx.get_bean_by_name("reporting_engine")
        assert await _engine_owner(reporting) == "reporting"
        await ctx.get_bean(ContextRefresher).refresh()
        assert await _engine_owner(reporting) == "reporting"  # the proxy follows the refresh
        await _assert_the_primary_stays(ctx)
    finally:
        await ctx.stop()


# ---------------------------------------------------------------------------
# A user factory that takes an auto-configured bean is DEFERRED until the auto-configurations have run.
# Its declared type is claimed right away so the auto-configured bean backs off, but a parametrized
# hint (``-> async_sessionmaker[AsyncSession]``, the idiomatic one) claimed nothing: the auto-configured
# factory was registered beside the user's. Without primary=True it was silently injected everywhere (the
# C042 defect), and with primary=True two primaries made every injection of the type fail.
# ---------------------------------------------------------------------------


class _UserSyncSession(Session):
    """The sessions of the application's own factory."""


_USER_STATEMENTS: list[str] = []


@event.listens_for(_UserSyncSession, "do_orm_execute")
def _record_user_statement(state: ORMExecuteState) -> None:
    _USER_STATEMENTS.append(str(state.statement))


_USER_FACTORIES: list[async_sessionmaker[AsyncSession]] = []


def _user_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    factory = async_sessionmaker(engine, sync_session_class=_UserSyncSession, expire_on_commit=True)
    _USER_FACTORIES.append(factory)
    return factory


@configuration
class _DeferredSessionFactory:
    """Other session options over the primary engine, which is auto-configured: the method is deferred."""

    @bean
    def app_sessions(self, async_engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
        return _user_factory(async_engine)


@configuration
class _DeferredPrimarySessionFactory:
    @bean(primary=True)
    def app_sessions(self, async_engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
        return _user_factory(async_engine)


@pytest.mark.parametrize(
    "configuration_class", [_DeferredSessionFactory, _DeferredPrimarySessionFactory], ids=["plain", "primary"]
)
async def test_a_deferred_user_session_factory_replaces_the_auto_configured_one(
    tmp_path: Path, configuration_class: type
) -> None:
    _USER_FACTORIES.clear()
    ctx = ApplicationContext(await _relational_app(tmp_path))
    ctx.register_bean(configuration_class)
    ctx.register_bean(_DatabaseOwnerRepository)
    await ctx.start()
    try:
        factory = ctx.get_bean(async_sessionmaker)
        assert [factory] == _USER_FACTORIES  # the user's, built once
        assert ctx.get_beans_of_type(async_sessionmaker) == [factory]  # the auto-configured one backed off
        session = ctx.get_bean(AsyncSession)
        assert isinstance(session.sync_session, _UserSyncSession)
        await session.close()
        routed = ctx.get_bean(RoutingSessionFactory).primary()
        assert isinstance(routed.sync_session, _UserSyncSession)
        await routed.close()

        _USER_STATEMENTS.clear()
        owners = await ctx.get_bean(_DatabaseOwnerRepository).find_all()
        assert [owner.name for owner in owners] == ["primary"]
        assert len(_USER_STATEMENTS) == 1  # the repository's query ran on a session of the user's factory
    finally:
        await ctx.stop()


class _MongoSettings:
    def __init__(self, uri: str) -> None:
        self.uri = uri


@auto_configuration
class _MongoSettingsAutoConfiguration:
    @bean
    def mongo_settings(self) -> _MongoSettings:
        return _MongoSettings("mongodb://deferred-mongo.invalid:27017")


@configuration
class _DeferredMongoClient:
    """A client built from a bean an auto-configuration provides (deferred), with a parametrized hint."""

    @bean
    def user_mongo_client(self, settings: _MongoSettings) -> AsyncMongoClient[dict[str, Any]]:
        return AsyncMongoClient(settings.uri, connect=False)


async def test_a_deferred_user_mongo_client_with_a_parametrized_hint_replaces_the_auto_configured_one() -> None:
    pytest.importorskip("pymongo")
    pytest.importorskip("beanie")
    from pyfly.data.document.mongodb.initializer import BeanieInitializer

    ctx = ApplicationContext(Config({"pyfly": {"data": {"document": {"enabled": "true"}}}}))
    ctx.register_bean(_MongoSettingsAutoConfiguration)
    ctx.register_bean(_DeferredMongoClient)
    await ctx.start()
    try:
        client = ctx.get_bean(AsyncMongoClient)
        assert ("deferred-mongo.invalid", 27017) in client.topology_description.server_descriptions()
        assert ctx.get_beans_of_type(AsyncMongoClient) == [client]
        assert ctx.get_bean(BeanieInitializer)._motor_client is client
    finally:
        await ctx.stop()
