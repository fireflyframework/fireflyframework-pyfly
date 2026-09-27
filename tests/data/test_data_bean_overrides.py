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

from pathlib import Path

import pytest

pytest.importorskip("sqlalchemy")

from sqlalchemy import event, text  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine  # noqa: E402

from pyfly.container import NoUniqueBeanError, bean, configuration  # noqa: E402
from pyfly.context.application_context import ApplicationContext  # noqa: E402
from pyfly.core.config import Config  # noqa: E402
from pyfly.data.relational.auto_configuration import EngineLifecycle  # noqa: E402
from pyfly.data.relational.datasource_registry import DataSourceRegistry, datasource_of  # noqa: E402
from pyfly.data.relational.health import SqlAlchemyHealthIndicator  # noqa: E402
from pyfly.data.relational.routing import RoutingSessionFactory  # noqa: E402

pymongo = pytest.importorskip("pymongo")
AsyncMongoClient = pymongo.AsyncMongoClient

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
        assert ctx.get_bean(RoutingSessionFactory)._primary is factory  # type: ignore[attr-defined]
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
