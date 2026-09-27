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
"""Functional test slices (v26.06.51): web_slice / service_slice / data_slice."""

from __future__ import annotations

import pytest

from pyfly.container.exceptions import BeanCreationException, NoSuchBeanError
from pyfly.container.stereotypes import rest_controller, service
from pyfly.testing import data_slice, service_slice, web_slice
from pyfly.web.mappings import get_mapping, request_mapping


class WidgetService:
    def names(self) -> list[str]:
        return ["real-widget"]


class FakeWidgetService:
    def names(self) -> list[str]:
        return ["fake-widget"]


@rest_controller
@request_mapping("/api/widgets")
class WidgetController:
    def __init__(self, widget_service: WidgetService) -> None:
        self._service = widget_service

    @get_mapping("/")
    async def list_widgets(self) -> dict:
        return {"widgets": self._service.names()}


@service
class GreetingService:
    def __init__(self, widget_service: WidgetService) -> None:
        self._service = widget_service

    def greet(self) -> str:
        return f"hello {self._service.names()[0]}"


@pytest.mark.asyncio
async def test_web_slice_serves_controller_with_real_dependency() -> None:
    async with await web_slice(WidgetController, WidgetService) as (ctx, client):
        assert ctx.get_bean(WidgetController) is not None
        client.get("/api/widgets/").assert_status(200)
        assert client.get("/api/widgets/").json() == {"widgets": ["real-widget"]}


@pytest.mark.asyncio
async def test_web_slice_with_overridden_collaborator_instance() -> None:
    async with await web_slice(WidgetController, overrides={WidgetService: FakeWidgetService()}) as (_ctx, client):
        assert client.get("/api/widgets/").json() == {"widgets": ["fake-widget"]}


@pytest.mark.asyncio
async def test_service_slice_with_override_class() -> None:
    async with await service_slice(GreetingService, overrides={WidgetService: FakeWidgetService}) as ctx:
        assert ctx.get_bean(GreetingService).greet() == "hello fake-widget"


@pytest.mark.asyncio
async def test_data_slice_is_minimal_and_stops_cleanly() -> None:
    async with await data_slice(WidgetService) as ctx:
        assert isinstance(ctx.get_bean(WidgetService), WidgetService)
    # context stopped on exit; a fresh slice is independent
    async with await data_slice(WidgetService) as ctx2:
        assert isinstance(ctx2.get_bean(WidgetService), WidgetService)


@pytest.mark.asyncio
async def test_missing_collaborator_fails_loudly() -> None:
    # WidgetController needs WidgetService, which is neither passed nor overridden.
    with pytest.raises((NoSuchBeanError, BeanCreationException)):
        async with await web_slice(WidgetController):
            pass


# ---------------------------------------------------------------------------------------------------------
# Data slices on a real database: fail fast, stop what failed, roll back (C138, C156, C176)
# ---------------------------------------------------------------------------------------------------------

from pathlib import Path  # noqa: E402

from sqlalchemy import Integer, String  # noqa: E402
from sqlalchemy.orm import Mapped, mapped_column  # noqa: E402

from pyfly.container.stereotypes import repository  # noqa: E402
from pyfly.core.config import Config  # noqa: E402
from pyfly.data.relational.datasource_registry import DataSourceRegistry  # noqa: E402
from pyfly.data.relational.sqlalchemy import Base, Repository  # noqa: E402


class _SliceMember(Base):
    __tablename__ = "wp11_slice_member"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    email: Mapped[str] = mapped_column(String(80))


@repository
class SliceMemberRepository(Repository[_SliceMember, int]):
    async def find_by_email(self, email: str) -> _SliceMember | None: ...

    async def exists_by_email(self, email: str) -> bool: ...


class _Missing:
    pass


@service
class _NeedsMissing:
    def __init__(self, missing: _Missing) -> None:
        self._missing = missing


def _relational(url: str, **extra: object) -> Config:
    return Config({"pyfly": {"data": {"relational": {"enabled": "true", "url": url, **extra}}}})


@pytest.mark.asyncio
async def test_a_slice_that_fails_fast_is_stopped(tmp_path: Path) -> None:
    """C176: the started context of a slice whose check fails is stopped, so its pools are closed."""
    config = _relational(f"sqlite+aiosqlite:///{tmp_path / 'app.db'}")
    registry = DataSourceRegistry.for_config(config)
    with pytest.raises((NoSuchBeanError, BeanCreationException)):
        await data_slice(SliceMemberRepository, _NeedsMissing, config=config)
    assert registry.closed


@pytest.mark.asyncio
@pytest.mark.parametrize("relational", [{}, {"url": "sqlite+aiosqlite:///{db}"}])
async def test_a_repository_without_the_relational_layer_fails_the_slice(
    tmp_path: Path, relational: dict[str, str]
) -> None:
    """C138: without pyfly.data.relational.enabled the derived queries are never compiled, and their stubs
    answered None with no database at all; the slice refuses to build instead, naming the flag."""
    tree = {key: value.format(db=tmp_path / "app.db") for key, value in relational.items()}
    config = Config({"pyfly": {"data": {"relational": tree}}})
    with pytest.raises(BeanCreationException, match=r"pyfly\.data\.relational\.enabled"):
        await data_slice(SliceMemberRepository, config=config)


@pytest.mark.asyncio
async def test_a_data_slice_rolls_back_the_test_on_an_in_memory_database() -> None:
    """The single connection of an in-memory SQLite database is the test's connection."""
    config = _relational("sqlite+aiosqlite:///:memory:")
    async with await data_slice(SliceMemberRepository, config=config, rollback=True) as ctx:
        members = ctx.get_bean(SliceMemberRepository)
        await members.save(_SliceMember(email="in@memory"))
        assert (await members.find_by_email("in@memory")) is not None
        assert await members.exists_by_email("in@memory") is True


@pytest.mark.asyncio
async def test_a_file_database_keeps_nothing_a_rolled_back_slice_wrote(tmp_path: Path) -> None:
    url = f"sqlite+aiosqlite:///{tmp_path / 'app.db'}"
    for _test in range(2):
        async with await data_slice(SliceMemberRepository, config=_relational(url), rollback=True) as ctx:
            members = ctx.get_bean(SliceMemberRepository)
            assert await members.count() == 0
            await members.save(_SliceMember(email="once@example.com"))


# ---------------------------------------------------------------------------------------------------------
# The application's own engine, and units that overlap in time
# ---------------------------------------------------------------------------------------------------------

import asyncio  # noqa: E402
import contextlib  # noqa: E402
import sqlite3  # noqa: E402

from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine  # noqa: E402

from pyfly.container.bean import bean  # noqa: E402
from pyfly.container.stereotypes import configuration  # noqa: E402
from pyfly.data.relational.dialect_customizers import uses_sqlite_begin_recipe  # noqa: E402
from pyfly.data.transaction import IllegalTransactionStateError, Propagation, detached, transactional  # noqa: E402

_APPLICATION_ENGINE: dict[str, AsyncEngine] = {}


@configuration
class _ApplicationEngine:
    """An application that brings its own engine: a plain ``create_async_engine``, without PyFly's SQLite
    ``BEGIN`` recipe."""

    @bean
    def async_engine(self) -> AsyncEngine:
        return _APPLICATION_ENGINE["engine"]


@service
class _MemberService:
    def __init__(self, members: SliceMemberRepository) -> None:
        self._members = members

    @transactional
    async def join(self, email: str) -> None:
        await self._members.save(_SliceMember(email=email))

    @transactional
    async def join_slowly(self, email: str) -> None:
        await self._members.save(_SliceMember(email=email))
        await asyncio.sleep(0.05)

    @transactional(propagation=Propagation.REQUIRES_NEW)
    async def join_apart(self, email: str) -> None:
        await self._members.save(_SliceMember(email=email))

    @transactional
    async def join_with_a_child_task(self, email: str) -> None:
        await self._members.save(_SliceMember(email=email))
        await asyncio.create_task(self.join_apart(f"child-{email}"))


def _rows(path: Path) -> int:
    with contextlib.closing(sqlite3.connect(path)) as connection:
        row = connection.execute("SELECT count(*) FROM wp11_slice_member").fetchone()
    return int(row[0])


@pytest.mark.asyncio
@pytest.mark.parametrize("engine_options", [{}, {"isolation_level": "AUTOCOMMIT"}], ids=["plain", "autocommit"])
async def test_an_application_sqlite_engine_rolls_back_too(tmp_path: Path, engine_options: dict[str, str]) -> None:
    """The driver of a plain SQLite engine defers its BEGIN (and an AUTOCOMMIT one never sends it): the first
    unit's SAVEPOINT started the transaction and its RELEASE committed it, so every unit of the test was
    committed for real. The test's connection starts its transaction itself."""
    path = tmp_path / "app.db"
    url = f"sqlite+aiosqlite:///{path}"
    engine = create_async_engine(url, **engine_options)
    _APPLICATION_ENGINE["engine"] = engine
    try:
        async with await data_slice(
            SliceMemberRepository, _MemberService, _ApplicationEngine, config=_relational(url), rollback=True
        ) as ctx:
            assert ctx.get_bean(AsyncEngine) is engine
            assert not uses_sqlite_begin_recipe(engine)
            members = ctx.get_bean(SliceMemberRepository)
            await ctx.get_bean(_MemberService).join("service@example.com")
            await members.save(_SliceMember(email="repository@example.com"))
            assert await members.count() == 2
            assert _rows(path) == 0  # nothing is committed while the test runs
    finally:
        await engine.dispose()
    assert _rows(path) == 0


@pytest.mark.asyncio
async def test_units_that_overlap_in_time_are_refused_and_the_test_goes_on(tmp_path: Path) -> None:
    """Two tasks with a unit each at the same time cannot share the test's connection: their savepoints would
    interleave ('no such savepoint', then a closed connection for the rest of the test). The second unit is
    refused before it touches the connection, naming the limitation."""
    path = tmp_path / "app.db"
    config = _relational(f"sqlite+aiosqlite:///{path}")
    async with await data_slice(SliceMemberRepository, _MemberService, config=config, rollback=True) as ctx:
        members = ctx.get_bean(SliceMemberRepository)
        member_service = ctx.get_bean(_MemberService)
        writes = await asyncio.gather(
            member_service.join_slowly("first@example.com"),
            member_service.join_slowly("second@example.com"),
            return_exceptions=True,
        )
        assert writes[0] is None
        assert isinstance(writes[1], IllegalTransactionStateError)
        assert "overlap" in str(writes[1])
        await member_service.join("third@example.com")
        reads = await asyncio.gather(*(members.count() for _ in range(3)), return_exceptions=True)
        assert reads[0] == 2
        assert all(isinstance(read, IllegalTransactionStateError) for read in reads[1:])
        assert sorted(member.email for member in await members.find_all()) == [
            "first@example.com",
            "third@example.com",
        ]
    assert _rows(path) == 0


@pytest.mark.asyncio
async def test_units_that_wait_for_each_other_share_the_test_connection(tmp_path: Path) -> None:
    """A task that waits for the unit of a task it started, or for its own detached work, runs them one after
    another: nothing overlaps, and nothing is refused."""
    path = tmp_path / "app.db"
    config = _relational(f"sqlite+aiosqlite:///{path}")
    async with await data_slice(SliceMemberRepository, _MemberService, config=config, rollback=True) as ctx:
        members = ctx.get_bean(SliceMemberRepository)
        member_service = ctx.get_bean(_MemberService)
        await member_service.join_with_a_child_task("parent@example.com")
        await detached(member_service.join("detached@example.com"))
        for email in ("one@example.com", "two@example.com"):
            await asyncio.create_task(member_service.join(email))
        assert await members.count() == 5
    assert _rows(path) == 0


@pytest.mark.asyncio
async def test_code_that_runs_in_another_context_takes_part_through_taking_part(tmp_path: Path) -> None:
    """A test runner may run the test body in another context than the fixture that began the transaction:
    ``taking_part()`` binds the transaction there (the plugin's ``data_context`` does)."""
    import contextvars

    from pyfly.testing import RollbackTransaction

    path = tmp_path / "app.db"
    async with await data_slice(SliceMemberRepository, config=_relational(f"sqlite+aiosqlite:///{path}")) as ctx:
        members = ctx.get_bean(SliceMemberRepository)
        rollback = RollbackTransaction(ctx)
        with pytest.raises(RuntimeError, match="not running"), rollback.taking_part():
            pass

        async def write(email: str, *, take_part: bool) -> None:
            if not take_part:
                await members.save(_SliceMember(email=email))
                return
            with rollback.taking_part():
                await members.save(_SliceMember(email=email))

        loop = asyncio.get_running_loop()
        async with rollback:
            # Without it, that context's unit runs on the datasource's own manager, and commits.
            await loop.create_task(write("committed@example.com", take_part=False), context=contextvars.Context())
            assert _rows(path) == 1
            await loop.create_task(write("rolled-back@example.com", take_part=True), context=contextvars.Context())
            assert await members.count() == 2
            assert _rows(path) == 1
    assert _rows(path) == 1
