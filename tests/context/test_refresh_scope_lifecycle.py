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
"""Refresh-scoped beans are destroyed when evicted, and can be injected behind a scoped proxy.

- C099: a refresh only dropped the evicted instances. A refresh-scoped bean that owns an engine
  leaked one pool per ``POST /actuator/refresh``, and ``ctx.stop()`` never destroyed the live one
  (nor emptied the cache, so a restart handed the previous run's instance back). Evicted instances
  now get their ``@pre_destroy`` after the swap, and ``stop()`` destroys the cached ones.
- C031: a singleton that injected a refresh-scoped bean kept the pre-refresh instance forever, so a
  datasource refresh never reached it. ``@refresh_scope(proxy=True)`` (or ``@scoped_proxy`` on a
  ``@bean`` method) injects a proxy that resolves the current instance on every use, as Spring
  Cloud's scoped proxy does; ``Provider[T]`` stays the explicit alternative.
- A destroyed scoped instance gets the whole contract, not only ``@pre_destroy``: a lifecycle bean
  is stopped, and the product of a ``@bean`` method gets its destroy method, declared
  (``@bean(destroy_method=...)``) or inferred (``dispose()``, ``aclose()`` or ``close()``). The
  documented ``@scoped_proxy @bean(scope="refresh") -> AsyncEngine`` has no ``@pre_destroy`` to write,
  and every refresh leaked the evicted engine's pool.

Everything runs on SQLite file databases; the URL of the reporting datasource comes from an
environment variable that the tests switch between two files.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

pytest.importorskip("sqlalchemy")

from sqlalchemy import event, text  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine  # noqa: E402

from pyfly.container import Provider, bean, component, configuration, service  # noqa: E402
from pyfly.container.bean import INFER_DESTROY_METHOD  # noqa: E402
from pyfly.container.refresh_scope import REFRESH_SCOPE_NAME, refresh_scope, scoped_proxy  # noqa: E402
from pyfly.container.types import Scope  # noqa: E402
from pyfly.context.application_context import ApplicationContext  # noqa: E402
from pyfly.context.lifecycle import pre_destroy  # noqa: E402
from pyfly.context.refresh import ContextRefresher  # noqa: E402
from pyfly.context.request_context import RequestContext  # noqa: E402
from pyfly.core.config import Config  # noqa: E402

DESTROYED: list[int] = []
DISPOSED: list[int] = []


@pytest.fixture(autouse=True)
def _reset() -> Iterator[None]:
    DESTROYED.clear()
    DISPOSED.clear()
    _Reporting.built = 0
    yield


@pytest.fixture
def urls(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    paths = {name: f"sqlite+aiosqlite:///{tmp_path / f'{name}.db'}" for name in ("a", "b")}
    monkeypatch.setenv("WP07_REPORTING_URL", paths["a"])
    return paths


def _switch(monkeypatch: pytest.MonkeyPatch, url: str) -> None:
    monkeypatch.setenv("WP07_REPORTING_URL", url)


async def _rows(url: str) -> list[str]:
    engine = create_async_engine(url)
    try:
        async with engine.connect() as conn:
            return [row[0] for row in await conn.execute(text("SELECT body FROM note ORDER BY rowid"))]
    finally:
        await engine.dispose()


class _Reporting:
    """A refresh-scoped datasource: it owns an engine built from the live configuration."""

    built = 0

    def __init__(self, config: Config) -> None:
        type(self).built += 1
        self.seq = type(self).built
        self.engine = create_async_engine(str(config.get("reporting.url")))
        event.listen(self.engine.sync_engine, "engine_disposed", lambda _engine: DISPOSED.append(self.seq))

    async def write(self, body: str) -> None:
        async with self.engine.begin() as conn:
            await conn.execute(text("CREATE TABLE IF NOT EXISTS note (body TEXT NOT NULL)"))
            await conn.execute(text("INSERT INTO note (body) VALUES (:body)"), {"body": body})

    @pre_destroy
    async def close(self) -> None:
        DESTROYED.append(self.seq)
        await self.engine.dispose()


@refresh_scope
class _PlainReporting(_Reporting):
    pass


@refresh_scope(proxy=True)
class _ProxiedReporting(_Reporting):
    pass


@component
@refresh_scope
class _StereotypedReporting(_Reporting):
    """``@component`` above ``@refresh_scope``: the stereotype used to turn it back into a singleton."""


def _config() -> Config:
    return Config({"reporting": {"url": "${WP07_REPORTING_URL}"}})


async def test_an_evicted_instance_is_destroyed_after_each_refresh(
    urls: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx = ApplicationContext(_config())
    ctx.register_bean(_PlainReporting)
    await ctx.start()
    refresher = ctx.get_bean(ContextRefresher)
    for refresh in range(3):
        await ctx.get_bean(_PlainReporting).write(f"refresh-{refresh}")
        await refresher.refresh()
        assert len(DESTROYED) == refresh + 1  # the evicted one, after the swap
        assert DESTROYED[-1] == refresh + 1
    live = ctx.get_bean(_PlainReporting)
    await live.write("live")

    await ctx.stop()

    assert DESTROYED == [1, 2, 3, 4]  # stop() destroyed the cached instance too
    assert DISPOSED == [1, 2, 3, 4]
    assert live.engine.pool.checkedin() == 0

    await ctx.start()  # a restart builds a new instance, not the previous run's
    try:
        assert ctx.get_bean(_PlainReporting).seq == 5
    finally:
        await ctx.stop()


@service
class _ProxiedConsumer:
    def __init__(self, reporting: _ProxiedReporting) -> None:
        self.reporting = reporting


@service
class _ProviderConsumer:
    def __init__(self, reporting: Provider[_PlainReporting]) -> None:
        self.reporting = reporting


async def test_a_proxied_refresh_scoped_bean_follows_the_refresh(
    urls: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx = ApplicationContext(_config())
    ctx.register_bean(_ProxiedReporting)
    ctx.register_bean(_ProxiedConsumer)
    await ctx.start()
    try:
        consumer = ctx.get_bean(_ProxiedConsumer)
        assert isinstance(consumer.reporting, _ProxiedReporting)
        await consumer.reporting.write("before")

        _switch(monkeypatch, urls["b"])
        await ctx.get_bean(ContextRefresher).refresh()
        await consumer.reporting.write("after")
        assert consumer.reporting.seq == 2
    finally:
        await ctx.stop()

    assert await _rows(urls["a"]) == ["before"]
    assert await _rows(urls["b"]) == ["after"]
    assert DISPOSED == [1, 2]


async def test_a_provider_follows_the_refresh(urls: dict[str, str], monkeypatch: pytest.MonkeyPatch) -> None:
    ctx = ApplicationContext(_config())
    ctx.register_bean(_PlainReporting)
    ctx.register_bean(_ProviderConsumer)
    await ctx.start()
    try:
        consumer = ctx.get_bean(_ProviderConsumer)
        await consumer.reporting.get().write("before")
        _switch(monkeypatch, urls["b"])
        await ctx.get_bean(ContextRefresher).refresh()
        await consumer.reporting.get().write("after")
    finally:
        await ctx.stop()

    assert await _rows(urls["a"]) == ["before"]
    assert await _rows(urls["b"]) == ["after"]


async def test_a_stereotype_above_refresh_scope_keeps_the_refresh_scope(urls: dict[str, str]) -> None:
    ctx = ApplicationContext(_config())
    ctx.register_bean(_StereotypedReporting)
    await ctx.start()
    try:
        first = ctx.get_bean(_StereotypedReporting)
        await ctx.get_bean(ContextRefresher).refresh()
        assert ctx.get_bean(_StereotypedReporting) is not first
    finally:
        await ctx.stop()


class _EngineHolder:
    def __init__(self, engine: AsyncEngine) -> None:
        self.engine = engine


_BUILT: list[AsyncEngine] = []


@configuration
class _ScopedEngineConfiguration:
    @scoped_proxy
    @bean(scope=REFRESH_SCOPE_NAME)  # type: ignore[arg-type]
    def reporting_engine(self, config: Config) -> AsyncEngine:
        engine = create_async_engine(str(config.get("reporting.url")))
        _BUILT.append(engine)
        return engine

    @bean
    def holder(self, reporting_engine: AsyncEngine) -> _EngineHolder:
        return _EngineHolder(reporting_engine)


async def test_a_scoped_proxy_bean_method_follows_the_refresh(
    urls: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    _BUILT.clear()
    ctx = ApplicationContext(_config())
    ctx.register_bean(_ScopedEngineConfiguration)
    await ctx.start()
    try:
        holder = ctx.get_bean(_EngineHolder)
        async with holder.engine.begin() as conn:
            await conn.execute(text("CREATE TABLE note (body TEXT NOT NULL)"))
            await conn.execute(text("INSERT INTO note (body) VALUES ('before')"))
        _switch(monkeypatch, urls["b"])
        await ctx.get_bean(ContextRefresher).refresh()
        async with holder.engine.begin() as conn:
            await conn.execute(text("CREATE TABLE note (body TEXT NOT NULL)"))
            await conn.execute(text("INSERT INTO note (body) VALUES ('after')"))
    finally:
        await ctx.stop()
        for engine in _BUILT:
            await engine.dispose()

    assert await _rows(urls["a"]) == ["before"]
    assert await _rows(urls["b"]) == ["after"]


def test_a_transient_bean_cannot_be_proxied() -> None:
    with pytest.raises(TypeError, match="scoped proxy"):

        @scoped_proxy
        @bean(scope=Scope.TRANSIENT)
        def snapshot() -> object:
            return object()


@scoped_proxy
class _Tenant:
    def __init__(self) -> None:
        context = RequestContext.current()
        assert context is not None, "a request-scoped bean was built outside a request"
        self.name = str(context.get("tenant"))


@service
class _TenantGreeter:
    def __init__(self, tenant: _Tenant) -> None:
        self.tenant = tenant

    def greet(self) -> str:
        return f"hello {self.tenant.name}"


async def test_a_singleton_can_take_a_proxied_request_scoped_bean() -> None:
    ctx = ApplicationContext(Config({}))
    ctx.register_bean(_Tenant, scope=Scope.REQUEST)
    ctx.register_bean(_TenantGreeter)
    await ctx.start()  # used to fail: no request at startup
    try:
        greeter = ctx.get_bean(_TenantGreeter)
        RequestContext.init().set("tenant", "acme")
        assert greeter.greet() == "hello acme"
        RequestContext.init().set("tenant", "globex")
        assert greeter.greet() == "hello globex"
    finally:
        RequestContext.clear()
        await ctx.stop()


# ---------------------------------------------------------------------------
# The whole destruction contract for the instances a scope destroys.
# ---------------------------------------------------------------------------

EVENTS: list[str] = []
_ENGINES: list[AsyncEngine] = []


@configuration
class _DocumentedScopedEngine:
    """The example of the dependency-injection guide, verbatim: no ``@pre_destroy`` anywhere."""

    @scoped_proxy
    @bean(scope=REFRESH_SCOPE_NAME)  # type: ignore[arg-type]
    def reporting_engine(self, config: Config) -> AsyncEngine:
        engine = create_async_engine(str(config.get("reporting.url")))
        seq = len(_ENGINES) + 1
        event.listen(engine.sync_engine, "engine_disposed", lambda _engine: DISPOSED.append(seq))
        _ENGINES.append(engine)
        return engine


async def test_a_refresh_scoped_engine_bean_is_disposed_on_every_refresh_and_at_stop(urls: dict[str, str]) -> None:
    _ENGINES.clear()
    ctx = ApplicationContext(_config())
    ctx.register_bean(_DocumentedScopedEngine)
    await ctx.start()
    engine = ctx.get_bean(AsyncEngine)  # the proxy
    refresher = ctx.get_bean(ContextRefresher)
    try:
        for refresh in range(3):
            async with engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
            await refresher.refresh()
            assert len(DISPOSED) == refresh + 1  # the evicted engine, right away
            assert DISPOSED[-1] == refresh + 1
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
    finally:
        await ctx.stop()

    assert len(_ENGINES) == 4
    assert DISPOSED == [1, 2, 3, 4]
    assert [built.pool.checkedin() for built in _ENGINES] == [0, 0, 0, 0]


@refresh_scope
class _ScopedPoller:
    """A refresh-scoped lifecycle bean: the context never starts it (it is built on demand)."""

    built = 0

    def __init__(self) -> None:
        type(self).built += 1
        self.seq = type(self).built

    async def start(self) -> None:
        EVENTS.append(f"poller#{self.seq}.start")

    async def stop(self) -> None:
        EVENTS.append(f"poller#{self.seq}.stop")


async def test_a_refresh_scoped_lifecycle_bean_is_stopped_when_it_is_destroyed() -> None:
    EVENTS.clear()
    _ScopedPoller.built = 0
    ctx = ApplicationContext(Config({}))
    ctx.register_bean(_ScopedPoller)
    await ctx.start()
    assert ctx.get_bean(_ScopedPoller).seq == 1
    await ctx.get_bean(ContextRefresher).refresh()
    assert EVENTS == ["poller#1.stop"]
    assert ctx.get_bean(_ScopedPoller).seq == 2

    await ctx.stop()

    assert EVENTS == ["poller#1.stop", "poller#2.stop"]


class _Resource:
    """Records which of its release methods the container calls."""

    def __init__(self, label: str) -> None:
        self.label = label

    async def dispose(self) -> None:
        EVENTS.append(f"{self.label}.dispose")

    def close(self) -> None:
        EVENTS.append(f"{self.label}.close")

    async def shutdown(self) -> None:
        EVENTS.append(f"{self.label}.shutdown")


class _Closeable:
    def __init__(self, label: str) -> None:
        self.label = label

    async def aclose(self) -> None:
        EVENTS.append(f"{self.label}.aclose")

    def close(self) -> None:
        EVENTS.append(f"{self.label}.close")


class _NeedsArgument:
    def __init__(self, label: str) -> None:
        self.label = label

    def dispose(self, reason: str) -> None:
        EVENTS.append(f"{self.label}.dispose({reason})")

    def close(self) -> None:
        EVENTS.append(f"{self.label}.close")


class _WithPreDestroy(_Resource):
    @pre_destroy
    def release(self) -> None:
        EVENTS.append(f"{self.label}.pre_destroy")


class _Declared:
    """One class per bean: a scoped bean is looked up by the class its method declares."""


class _Explicit(_Declared, _Resource):
    pass


class _Disabled(_Declared, _Resource):
    pass


class _Inferred(_Declared, _Resource):
    pass


class _InferredAclose(_Declared, _Closeable):
    pass


class _InferredSkipsArguments(_Declared, _NeedsArgument):
    pass


class _OwnPreDestroy(_Declared, _WithPreDestroy):
    pass


@configuration
class _ScopedResources:
    @bean(scope=REFRESH_SCOPE_NAME, destroy_method="shutdown")  # type: ignore[arg-type]
    def explicit(self) -> _Explicit:
        return _Explicit("explicit")

    @bean(scope=REFRESH_SCOPE_NAME, destroy_method="")  # type: ignore[arg-type]
    def disabled(self) -> _Disabled:
        return _Disabled("disabled")

    @bean(scope=REFRESH_SCOPE_NAME)  # type: ignore[arg-type]
    def inferred(self) -> _Inferred:
        return _Inferred("inferred")

    @bean(scope=REFRESH_SCOPE_NAME)  # type: ignore[arg-type]
    def inferred_aclose(self) -> _InferredAclose:
        return _InferredAclose("aclose")

    @bean(scope=REFRESH_SCOPE_NAME)  # type: ignore[arg-type]
    def inferred_skips_arguments(self) -> _InferredSkipsArguments:
        return _InferredSkipsArguments("arguments")

    @bean(scope=REFRESH_SCOPE_NAME)  # type: ignore[arg-type]
    def own_pre_destroy(self) -> _OwnPreDestroy:
        return _OwnPreDestroy("own")


async def test_a_scoped_bean_gets_its_declared_or_inferred_destroy_method() -> None:
    EVENTS.clear()
    ctx = ApplicationContext(Config({}))
    ctx.register_bean(_ScopedResources)
    await ctx.start()
    for bean_type in (_Explicit, _Disabled, _Inferred, _InferredAclose, _InferredSkipsArguments, _OwnPreDestroy):
        ctx.get_bean(bean_type)
    await ctx.get_bean(ContextRefresher).refresh()

    assert sorted(EVENTS) == [
        "aclose.aclose",  # inferred: dispose(), then aclose(), then close()
        "arguments.close",  # a dispose() that needs an argument is not a destroy method
        "explicit.shutdown",  # declared: that method only
        "inferred.dispose",
        "own.pre_destroy",  # its own @pre_destroy: nothing is inferred
    ]
    EVENTS.clear()
    await ctx.stop()  # nothing was rebuilt since the refresh: nothing to destroy
    assert EVENTS == []


def test_the_default_destroy_method_is_inferred() -> None:
    @bean
    def factory() -> object:
        return object()

    assert factory.__pyfly_bean_destroy_method__ == INFER_DESTROY_METHOD  # type: ignore[attr-defined]


_SINGLETON_ENGINES: list[AsyncEngine] = []


class _Writer:
    def __init__(self, engine: AsyncEngine) -> None:
        self.engine = engine

    @pre_destroy
    async def flush(self) -> None:
        async with self.engine.begin() as conn:
            await conn.execute(text("CREATE TABLE IF NOT EXISTS note (body TEXT NOT NULL)"))
            await conn.execute(text("INSERT INTO note (body) VALUES ('final')"))
        EVENTS.append("writer.pre_destroy")


@configuration
class _SingletonResources:
    @bean(destroy_method="dispose")
    def own_engine(self, config: Config) -> AsyncEngine:
        engine = create_async_engine(str(config.get("reporting.url")))
        event.listen(engine.sync_engine, "engine_disposed", lambda _engine: EVENTS.append("engine.disposed"))
        _SINGLETON_ENGINES.append(engine)
        return engine

    @bean
    def writer(self, own_engine: AsyncEngine) -> _Writer:
        return _Writer(own_engine)

    @bean
    def left_alone(self) -> _Resource:
        return _Resource("singleton")


async def test_a_singleton_gets_its_declared_destroy_method_at_stop_and_nothing_inferred(
    urls: dict[str, str],
) -> None:
    """A singleton's product infers nothing: the auto-configured ones are views over what a registry or
    a lifecycle bean releases in order. Naming the method is how a singleton opts in."""
    EVENTS.clear()
    _SINGLETON_ENGINES.clear()
    ctx = ApplicationContext(_config())
    ctx.register_bean(_SingletonResources)
    await ctx.start()
    await ctx.stop()

    assert EVENTS == ["writer.pre_destroy", "engine.disposed"]  # after the bean that used it
    assert await _rows(urls["a"]) == ["final"]
    assert _SINGLETON_ENGINES[0].pool.checkedin() == 0
