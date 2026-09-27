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
"""A non-singleton ``@bean`` method runs when its scope asks for an instance, never at startup (C104).

``start()`` used to call every ``@bean`` method once, whatever its scope, to learn the concrete type,
and then kept the product only for singletons. A refresh-scoped or transient factory therefore built
one object at startup for nothing (an extra engine per scoped datasource ``@bean``), and a
REQUEST-scoped factory ran outside any request: one that read the request (a per-tenant session) or
took another request-scoped bean made ``ctx.start()`` fail. A non-singleton ``@bean`` with a declared
return class is now registered under that class and built only on resolution, as Spring does.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

pytest.importorskip("sqlalchemy")

from sqlalchemy import text  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine  # noqa: E402

from pyfly.container import NoSuchBeanError, Provider, bean, configuration, service  # noqa: E402
from pyfly.container.refresh_scope import REFRESH_SCOPE_NAME  # noqa: E402
from pyfly.container.types import Scope  # noqa: E402
from pyfly.context.application_context import ApplicationContext  # noqa: E402
from pyfly.context.refresh import ContextRefresher  # noqa: E402
from pyfly.context.request_context import RequestContext  # noqa: E402
from pyfly.core.config import Config  # noqa: E402

_CALLS: list[str] = []
_BUILT: list[AsyncEngine] = []


@pytest.fixture(autouse=True)
def _reset() -> Iterator[None]:
    _CALLS.clear()
    RequestContext.clear()
    yield
    RequestContext.clear()


class TenantId:
    def __init__(self, value: str) -> None:
        self.value = value


class TenantSession:
    def __init__(self, tenant: TenantId) -> None:
        self.tenant = tenant


class Snapshot:
    pass


@configuration
class _ScopedFactories:
    @bean(scope=Scope.REQUEST)
    def tenant_id(self) -> TenantId:
        _CALLS.append("tenant_id")
        context = RequestContext.current()
        assert context is not None, "a REQUEST-scoped factory ran outside a request"
        return TenantId(str(context.get("tenant")))

    @bean(scope=Scope.REQUEST)
    def tenant_session(self, tenant: TenantId) -> TenantSession:
        _CALLS.append("tenant_session")
        return TenantSession(tenant)

    @bean(scope=Scope.TRANSIENT)
    def snapshot(self) -> Snapshot:
        _CALLS.append("snapshot")
        return Snapshot()


async def test_request_scoped_factories_run_only_inside_a_request() -> None:
    ctx = ApplicationContext(Config({}))
    ctx.register_bean(_ScopedFactories)
    await ctx.start()
    try:
        assert _CALLS == []

        RequestContext.init().set("tenant", "acme")
        session = ctx.get_bean(TenantSession)
        assert session.tenant.value == "acme"
        assert ctx.get_bean(TenantSession) is session  # one per request

        RequestContext.init().set("tenant", "globex")
        assert ctx.get_bean(TenantSession).tenant.value == "globex"
    finally:
        await ctx.stop()


async def test_a_transient_factory_runs_once_per_resolution() -> None:
    ctx = ApplicationContext(Config({}))
    ctx.register_bean(_ScopedFactories)
    await ctx.start()
    try:
        assert "snapshot" not in _CALLS
        first = ctx.get_bean(Snapshot)
        second = ctx.get_bean(Snapshot)
        assert first is not second
        assert _CALLS.count("snapshot") == 2
    finally:
        await ctx.stop()


@configuration
class _ReportingConfiguration:
    @bean(scope=REFRESH_SCOPE_NAME)  # type: ignore[arg-type]
    def reporting_engine(self, config: Config) -> AsyncEngine:
        engine = create_async_engine(str(config.get("reporting.url")))
        _BUILT.append(engine)
        return engine


async def test_a_refresh_scoped_engine_factory_builds_no_engine_until_it_is_resolved(tmp_path: Path) -> None:
    _BUILT.clear()
    ctx = ApplicationContext(Config({"reporting": {"url": f"sqlite+aiosqlite:///{tmp_path / 'reporting.db'}"}}))
    ctx.register_bean(_ReportingConfiguration)
    await ctx.start()
    try:
        assert _BUILT == [], "start() built an engine nobody asked for"
        engine = ctx.get_bean(AsyncEngine)
        async with engine.connect() as conn:
            assert (await conn.execute(text("SELECT 1"))).scalar_one() == 1
        assert len(_BUILT) == 1
        assert _BUILT[0] is engine
        await ctx.get_bean(ContextRefresher).refresh()
        assert ctx.get_bean(AsyncEngine) is not engine
        assert len(_BUILT) == 2
    finally:
        await ctx.stop()
        for each in _BUILT:
            await each.dispose()
        _BUILT.clear()


class _Port:
    pass


class _Adapter(_Port):
    pass


_ENABLED: list[bool] = []


@configuration
class _DecliningFactories:
    """``-> Port | None``: a factory that may decline. A scoped one is asked at each resolution."""

    @bean(scope=Scope.TRANSIENT)
    def port(self) -> _Port | None:
        _CALLS.append("port")
        return _Adapter() if _ENABLED[0] else None


@service
class _OptionalUser:
    def __init__(self, ports: Provider[_Port]) -> None:
        self.ports = ports


async def test_a_scoped_factory_that_declines_is_no_bean() -> None:
    """It used to be registered without being called and then injected ``None`` as the bean."""
    _ENABLED[:] = [False]
    ctx = ApplicationContext(Config({}))
    ctx.register_bean(_DecliningFactories)
    ctx.register_bean(_OptionalUser)
    await ctx.start()
    try:
        with pytest.raises(NoSuchBeanError, match="returned None"):
            ctx.get_bean(_Port)
        assert ctx.container._resolve_param(_Port | None) is None  # an Optional parameter gets None
        _ENABLED[:] = [True]
        assert isinstance(ctx.get_bean(_OptionalUser).ports.get(), _Adapter)
    finally:
        await ctx.stop()
    assert _CALLS == ["port", "port", "port"]


# ---------------------------------------------------------------------------
# The idiomatic hint is parametrized: ``-> async_sessionmaker[AsyncSession]``. It is not a class, and
# a factory declared with it was still called at startup (a REQUEST-scoped one outside any request).
# It declares its origin class, which is what an injection of the parametrized type resolves.
# ---------------------------------------------------------------------------


async def _owner(url: str, name: str) -> None:
    """Create database *url* holding one row that names it."""
    engine = create_async_engine(url)
    try:
        async with engine.begin() as conn:
            await conn.execute(text("CREATE TABLE wp07_owner (name VARCHAR(20))"))
            await conn.execute(text("INSERT INTO wp07_owner (name) VALUES (:name)"), {"name": name})
    finally:
        await engine.dispose()


async def _owner_of(sessions: async_sessionmaker[AsyncSession]) -> str:
    async with sessions() as session:
        return str((await session.execute(text("SELECT name FROM wp07_owner"))).scalar_one())


def _sessions(url: str) -> async_sessionmaker[AsyncSession]:
    engine = create_async_engine(url)
    _BUILT.append(engine)
    return async_sessionmaker(engine, expire_on_commit=False)


@configuration
class _TenantSessionFactories:
    @bean(scope=Scope.REQUEST)
    def tenant_sessions(self, config: Config) -> async_sessionmaker[AsyncSession]:
        _CALLS.append("tenant_sessions")
        context = RequestContext.current()
        assert context is not None, "a REQUEST-scoped factory ran outside a request"
        return _sessions(str(config.get(f"tenants.{context.get('tenant')}")))


class _TenantReader:
    """Takes the request's session factory by its parametrized type."""

    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self.sessions = sessions


async def test_a_request_scoped_factory_with_a_parametrized_hint_runs_only_inside_a_request(tmp_path: Path) -> None:
    _BUILT.clear()
    urls = {tenant: f"sqlite+aiosqlite:///{tmp_path / f'{tenant}.db'}" for tenant in ("acme", "globex")}
    for tenant, url in urls.items():
        await _owner(url, tenant)
    ctx = ApplicationContext(Config({"tenants": urls}))
    ctx.register_bean(_TenantSessionFactories)
    ctx.register_bean(_TenantReader, scope=Scope.REQUEST)
    await ctx.start()
    try:
        assert _CALLS == [], "start() called a REQUEST-scoped factory"
        for tenant in ("acme", "globex"):
            RequestContext.init().set("tenant", tenant)
            reader = ctx.get_bean(_TenantReader)
            assert await _owner_of(reader.sessions) == tenant
            assert ctx.get_bean(async_sessionmaker) is reader.sessions  # one per request
        assert _CALLS == ["tenant_sessions", "tenant_sessions"]
    finally:
        await ctx.stop()
        for each in _BUILT:
            await each.dispose()
        _BUILT.clear()


@configuration
class _ReportingSessionFactories:
    @bean(scope=REFRESH_SCOPE_NAME)  # type: ignore[arg-type]
    def reporting_sessions(self, config: Config) -> async_sessionmaker[AsyncSession]:
        _CALLS.append("reporting_sessions")
        return _sessions(str(config.get("reporting.url")))


@service
class _ReportQueries:
    """A singleton that follows the refresh through a ``Provider`` of the parametrized type."""

    def __init__(self, sessions: Provider[async_sessionmaker[AsyncSession]]) -> None:
        self.sessions = sessions


async def test_a_refresh_scoped_factory_with_a_parametrized_hint_builds_nothing_until_it_is_resolved(
    tmp_path: Path,
) -> None:
    _BUILT.clear()
    url = f"sqlite+aiosqlite:///{tmp_path / 'reporting.db'}"
    await _owner(url, "reporting")
    ctx = ApplicationContext(Config({"reporting": {"url": url}}))
    ctx.register_bean(_ReportingSessionFactories)
    ctx.register_bean(_ReportQueries)
    await ctx.start()
    try:
        assert _CALLS == [], "start() called a refresh-scoped factory"
        queries = ctx.get_bean(_ReportQueries)
        before = queries.sessions.get()
        assert await _owner_of(before) == "reporting"
        assert queries.sessions.get() is before

        await ctx.get_bean(ContextRefresher).refresh()

        after = queries.sessions.get()
        assert after is not before
        assert await _owner_of(after) == "reporting"
        assert _CALLS == ["reporting_sessions", "reporting_sessions"]
    finally:
        await ctx.stop()
        for each in _BUILT:
            await each.dispose()
        _BUILT.clear()


@configuration
class _AuditSessionFactories:
    @bean(scope=Scope.TRANSIENT)
    def audit_sessions(self) -> async_sessionmaker[AsyncSession] | None:
        _CALLS.append("audit_sessions")
        return None  # auditing is off


async def test_a_transient_factory_with_an_optional_parametrized_hint_is_asked_only_on_resolution() -> None:
    ctx = ApplicationContext(Config({}))
    ctx.register_bean(_AuditSessionFactories)
    await ctx.start()
    try:
        assert _CALLS == []
        with pytest.raises(NoSuchBeanError, match="returned None"):
            ctx.get_bean(async_sessionmaker)
        assert ctx.container._resolve_param(async_sessionmaker[AsyncSession] | None) is None
        assert _CALLS == ["audit_sessions", "audit_sessions"]
    finally:
        await ctx.stop()
