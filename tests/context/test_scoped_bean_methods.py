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
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine  # noqa: E402

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
