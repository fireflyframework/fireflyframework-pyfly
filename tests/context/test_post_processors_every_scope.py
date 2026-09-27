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
"""Every bean the container creates goes through the BeanPostProcessors and ``@post_construct`` (C029).

Only singletons created during the batched startup passes, or after ``start()`` returned, were
post-processed. A TRANSIENT, REQUEST or custom-scoped bean never was, and neither was a ``@lazy``
singleton first resolved while the context was still starting (from a ``@post_construct``, an
``ApplicationReadyEvent`` listener or a runner). For a repository that means the
``RepositoryBeanPostProcessor`` never compiled its derived and ``@query`` stubs: ``find_by_name``
ran the stub body and returned ``None`` ("not found"), and nothing raised.

The repositories here run on a SQLite file database that holds three people, two named alice.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("sqlalchemy")

from sqlalchemy import Integer, String, text  # noqa: E402
from sqlalchemy.ext.asyncio import create_async_engine  # noqa: E402
from sqlalchemy.orm import Mapped, mapped_column  # noqa: E402

from pyfly.container import Provider, lazy, service  # noqa: E402
from pyfly.container.types import Scope  # noqa: E402
from pyfly.context.application_context import ApplicationContext  # noqa: E402
from pyfly.context.events import ApplicationReadyEvent, app_event_listener  # noqa: E402
from pyfly.context.lifecycle import post_construct  # noqa: E402
from pyfly.context.request_context import RequestContext  # noqa: E402
from pyfly.core.config import Config  # noqa: E402
from pyfly.data.query import query  # noqa: E402
from pyfly.data.relational.sqlalchemy.entity import Base  # noqa: E402
from pyfly.data.relational.sqlalchemy.repository import Repository  # noqa: E402


class _Person(Base):
    __tablename__ = "wp07_scoped_person"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(50))


class _PersonRepository(Repository[_Person, int]):
    async def find_by_name(self, name: str) -> list[_Person]: ...

    @query("SELECT p FROM _Person p WHERE p.name = :name")
    async def by_name(self, name: str) -> list[_Person]: ...


@pytest.fixture
async def database(tmp_path: Path) -> AsyncIterator[str]:
    url = f"sqlite+aiosqlite:///{tmp_path / 'people.db'}"
    engine = create_async_engine(url)
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all, tables=[_Person.__table__])
            for name in ("alice", "alice", "bob"):
                await conn.execute(text("INSERT INTO wp07_scoped_person (name) VALUES (:n)"), {"n": name})
    finally:
        await engine.dispose()
    yield url


@pytest.fixture(autouse=True)
def _clean_request_context() -> Iterator[None]:
    RequestContext.clear()
    yield
    RequestContext.clear()


def _context(url: str) -> ApplicationContext:
    return ApplicationContext(
        Config({"pyfly": {"data": {"relational": {"enabled": "true", "url": url, "ddl-auto": "none"}}}})
    )


async def _names(repository: Any) -> tuple[Any, Any]:
    derived = await repository.find_by_name("alice")
    queried = await repository.by_name(name="alice")
    return (
        None if derived is None else [person.name for person in derived],
        None if queried is None else [person.name for person in queried],
    )


async def test_a_transient_repository_has_its_queries_compiled(database: str) -> None:
    ctx = _context(database)
    ctx.register_bean(_PersonRepository, scope=Scope.TRANSIENT)
    await ctx.start()
    try:
        assert await _names(ctx.get_bean(_PersonRepository)) == (["alice", "alice"], ["alice", "alice"])
    finally:
        await ctx.stop()


class _Directory:
    def __init__(self, people: _PersonRepository) -> None:
        self.people = people


async def test_a_transient_repository_injected_at_startup_has_its_queries_compiled(database: str) -> None:
    ctx = _context(database)
    ctx.register_bean(_PersonRepository, scope=Scope.TRANSIENT)
    ctx.register_bean(_Directory)
    await ctx.start()
    try:
        assert await _names(ctx.get_bean(_Directory).people) == (["alice", "alice"], ["alice", "alice"])
    finally:
        await ctx.stop()


async def test_a_request_scoped_repository_has_its_queries_compiled(database: str) -> None:
    ctx = _context(database)
    ctx.register_bean(_PersonRepository, scope=Scope.REQUEST)
    await ctx.start()
    try:
        RequestContext.init()
        assert await _names(ctx.get_bean(_PersonRepository)) == (["alice", "alice"], ["alice", "alice"])
    finally:
        await ctx.stop()


@lazy
class _LazyPersonRepository(Repository[_Person, int]):
    async def find_by_name(self, name: str) -> list[_Person]: ...

    @query("SELECT p FROM _Person p WHERE p.name = :name")
    async def by_name(self, name: str) -> list[_Person]: ...


_WARM_UP: list[Any] = []


@service
class _WarmUp:
    def __init__(self, people: Provider[_LazyPersonRepository]) -> None:
        self._people = people

    @app_event_listener
    async def on_ready(self, event: ApplicationReadyEvent) -> None:
        _WARM_UP.append(await _names(self._people.get()))


async def test_a_lazy_repository_first_resolved_during_startup_has_its_queries_compiled(database: str) -> None:
    _WARM_UP.clear()
    ctx = _context(database)
    ctx.register_bean(_LazyPersonRepository)
    ctx.register_bean(_WarmUp)
    await ctx.start()
    try:
        assert _WARM_UP == [(["alice", "alice"], ["alice", "alice"])]
        assert await _names(ctx.get_bean(_LazyPersonRepository)) == (["alice", "alice"], ["alice", "alice"])
    finally:
        await ctx.stop()


_CALLS: list[str] = []


class _Transient:
    @post_construct
    def ready(self) -> None:
        _CALLS.append("transient")


@lazy
class _LazyHelper:
    @post_construct
    def ready(self) -> None:
        _CALLS.append("lazy")


class _Eager:
    def __init__(self, helper: Provider[_LazyHelper]) -> None:
        self._helper = helper

    @post_construct
    def ready(self) -> None:
        _CALLS.append("eager")
        self._helper.get()


async def test_post_construct_runs_on_transient_beans_and_on_lazy_beans_created_during_startup() -> None:
    _CALLS.clear()
    ctx = ApplicationContext(Config({}))
    ctx.register_bean(_Transient, scope=Scope.TRANSIENT)
    ctx.register_bean(_LazyHelper)
    ctx.register_bean(_Eager)
    await ctx.start()
    try:
        ctx.get_bean(_Transient)
        ctx.get_bean(_Transient)
        assert sorted(_CALLS) == ["eager", "lazy", "transient", "transient"]
    finally:
        await ctx.stop()


# ---------------------------------------------------------------------------
# A post-processor that registers the beans it sees somewhere that outlives them declares
# ``singletons_only``: it is given singletons only, and told about each other instance it skipped.
# ---------------------------------------------------------------------------


class _SingletonRegistrar:
    singletons_only = True

    def __init__(self) -> None:
        self.registered: list[str] = []
        self.skipped: list[tuple[str, Any]] = []

    def before_init(self, bean: Any, bean_name: str) -> Any:
        del bean_name
        self.registered.append(f"before:{type(bean).__name__}")
        return bean

    def after_init(self, bean: Any, bean_name: str) -> Any:
        del bean_name
        self.registered.append(f"after:{type(bean).__name__}")
        return bean

    def non_singleton_skipped(self, bean: Any, bean_name: str, scope: Any) -> None:
        self.skipped.append((bean_name, scope))
        assert bean is not None


class _Listener:
    pass


class _PerRequest:
    pass


class _ListenerHolder:
    def __init__(self, listener: _Listener) -> None:
        self.listener = listener


async def test_a_singletons_only_post_processor_is_given_singletons_only() -> None:
    registrar = _SingletonRegistrar()
    ctx = ApplicationContext(Config({}))
    ctx.register_post_processor(registrar)
    ctx.register_bean(_Listener, scope=Scope.TRANSIENT)
    ctx.register_bean(_PerRequest, scope=Scope.REQUEST)
    ctx.register_bean(_ListenerHolder)
    await ctx.start()
    try:
        ctx.get_bean(_Listener)
        RequestContext.init()
        ctx.get_bean(_PerRequest)
    finally:
        await ctx.stop()

    assert "after:_ListenerHolder" in registrar.registered
    assert [entry for entry in registrar.registered if "_Listener" in entry and "Holder" not in entry] == []
    assert "after:_PerRequest" not in registrar.registered
    assert registrar.skipped == [
        ("_Listener", Scope.TRANSIENT),  # the one injected at startup (the batched pass)
        ("_Listener", Scope.TRANSIENT),  # the one resolved after start
        ("_PerRequest", Scope.REQUEST),
    ]
