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
"""The repository seam of the unit of work (WP01-03, F3, F4, F5): the session is resolved per call.

A DI-built repository holds no session. Outside a transaction every call runs in a short auto unit that
commits (writes) or ends without writing (reads) and always returns its connection; inside a unit it joins.
This covers the inherited methods, ``SoftDeleteRepository``, a subclass's own methods, the derived and
``@query`` methods the post-processor compiles, datasource affinity, ``stream_all`` owning its connection,
the after-begin customizers of auto units, and entities outliving their auto unit. Everything runs on a
SQLite file database with the registry's settings, through a real ApplicationContext.
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import DateTime, Identity, Integer, String, select, text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession, create_async_engine
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.pool import NullPool

from pyfly.container.stereotypes import component, repository
from pyfly.context.application_context import ApplicationContext
from pyfly.core.config import Config
from pyfly.data import transactional
from pyfly.data.query import query
from pyfly.data.relational.auto_configuration import RelationalAutoConfiguration
from pyfly.data.relational.datasource_registry import DataSource, DataSourceRegistry
from pyfly.data.relational.sqlalchemy.entity import Base
from pyfly.data.relational.sqlalchemy.repository import Repository, is_read_method
from pyfly.data.relational.sqlalchemy.soft_delete import SoftDeleteRepository
from pyfly.data.transaction import IllegalTransactionStateError, is_transaction_active
from pyfly.testing.statement_counter import StatementCounter


class SeamItem(Base):
    __tablename__ = "seam_item"

    id: Mapped[int] = mapped_column(Integer, Identity(), primary_key=True)
    name: Mapped[str] = mapped_column(String(64))


class SeamSoft(Base):
    __tablename__ = "seam_soft"

    id: Mapped[int] = mapped_column(Integer, Identity(), primary_key=True)
    name: Mapped[str] = mapped_column(String(64))
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None, nullable=True)


class SeamReport(Base):
    __tablename__ = "seam_report"

    id: Mapped[int] = mapped_column(Integer, Identity(), primary_key=True)
    name: Mapped[str] = mapped_column(String(64))


@repository
class SeamItemRepository(Repository[SeamItem, int]):
    async def find_by_name(self, name: str) -> list[SeamItem]: ...

    async def count_by_name(self, name: str) -> int: ...

    async def delete_by_name(self, name: str) -> int: ...

    @query("SELECT * FROM seam_item WHERE name = :name", native=True)
    async def load_named(self, name: str) -> list[SeamItem]: ...

    async def rename_all(self, name: str) -> None:
        """A subclass write method: it runs in a write auto unit and commits."""
        for item in await self.find_all():
            item.name = name
        await self._require_session().flush()

    async def find_and_touch(self) -> None:
        """Misnamed: a read-named method that writes is refused instead of silently rolled back."""
        for item in await self.find_all():
            item.name = "touched"
        await self._session.flush()

    async def find_twice(self) -> tuple[int, bool]:
        """A subclass read method calling two inherited reads (the second one nests a third)."""
        return await self.count(), await self.exists_by_id(1)


@repository
class SeamSoftRepository(SoftDeleteRepository[SeamSoft, int]):
    pass


@repository
class SeamReportRepository(Repository[SeamReport, int]):
    __datasource__ = "reporting"


@component
class RecordingCustomizer:
    """An after-begin customizer bean: it runs inside every unit on its datasource, auto units included."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def after_begin(self, connection: AsyncSession | AsyncConnection, datasource: DataSource) -> None:
        self.calls.append(datasource.qualified_name)


class Harness:
    def __init__(self, ctx: ApplicationContext, url: str, reporting_url: str) -> None:
        self.ctx = ctx
        self.url = url
        self.reporting_url = reporting_url

    def checked_out(self) -> int:
        registry = self.ctx.get_bean(DataSourceRegistry)
        return sum(ds.engine.sync_engine.pool.checkedout() for ds in registry.all_datasources())

    async def names(self, table: str = "seam_item", *, url: str | None = None) -> list[str]:
        engine = create_async_engine(url or self.url, poolclass=NullPool)
        try:
            async with engine.connect() as conn:
                return [r[0] for r in (await conn.execute(text(f"SELECT name FROM {table} ORDER BY id"))).all()]
        finally:
            await engine.dispose()


@pytest.fixture
async def seam(tmp_path: Path) -> AsyncIterator[Harness]:
    url = f"sqlite+aiosqlite:///{tmp_path / 'seam.db'}"
    reporting_url = f"sqlite+aiosqlite:///{tmp_path / 'reporting.db'}"
    for target, models in ((url, (SeamItem, SeamSoft)), (reporting_url, (SeamReport,))):
        engine = create_async_engine(target, poolclass=NullPool)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all, tables=[model.__table__ for model in models])
        await engine.dispose()
    relational: dict[str, Any] = {
        "enabled": "true",
        "url": url,
        "ddl-auto": "none",
        "datasources": {"reporting": {"url": reporting_url}},
    }
    ctx = ApplicationContext(Config({"pyfly": {"data": {"relational": relational}}}))
    for bean in (
        RelationalAutoConfiguration,
        SeamItemRepository,
        SeamSoftRepository,
        SeamReportRepository,
        RecordingCustomizer,
    ):
        ctx.register_bean(bean)
    await ctx.start()
    harness = Harness(ctx, url, reporting_url)
    try:
        yield harness
        assert harness.checked_out() == 0
    finally:
        await ctx.stop()


def test_read_methods_are_recognized_by_name() -> None:
    assert all(is_read_method(name) for name in ("find_all", "count", "exists_by_id", "stream_all", "get_x"))
    assert not any(is_read_method(name) for name in ("save", "delete_by_id", "restore", "rename_all"))


class TestAutoUnits:
    async def test_a_write_outside_a_transaction_commits(self, seam: Harness) -> None:
        items = seam.ctx.get_bean(SeamItemRepository)
        saved = await items.save(SeamItem(name="saved"))
        assert saved.id is not None and saved.name == "saved"  # detached with its state intact
        assert await seam.names() == ["saved"]

    async def test_a_read_ends_its_unit_and_keeps_the_entity(self, seam: Harness) -> None:
        items = seam.ctx.get_bean(SeamItemRepository)
        await items.save(SeamItem(name="kept"))
        engine = seam.ctx.get_bean(DataSourceRegistry).primary.engine
        with StatementCounter(engine) as counter:
            found = await items.find_by_id(1)
        assert found is not None and found.name == "kept"  # no DetachedInstanceError after the unit ended
        assert counter.commits == 0
        assert counter.rollbacks == 1

    async def test_nested_repository_calls_share_one_unit(self, seam: Harness) -> None:
        items = seam.ctx.get_bean(SeamItemRepository)
        await items.save(SeamItem(name="one"))
        engine = seam.ctx.get_bean(DataSourceRegistry).primary.engine
        with StatementCounter(engine) as counter:
            assert await items.find_twice() == (1, True)  # count, then exists_by_id -> find_by_id
        assert counter.rollbacks == 1  # one read unit for the whole call
        assert counter.counts().get("BEGIN") == 1

    async def test_a_subclass_write_method_commits(self, seam: Harness) -> None:
        items = seam.ctx.get_bean(SeamItemRepository)
        await items.save_all([SeamItem(name="a"), SeamItem(name="b")])
        await items.rename_all("renamed")
        assert await seam.names() == ["renamed", "renamed"]

    async def test_a_read_named_method_that_writes_is_refused(self, seam: Harness) -> None:
        items = seam.ctx.get_bean(SeamItemRepository)
        await items.save(SeamItem(name="original"))
        with pytest.raises(IllegalTransactionStateError, match="read-only auto unit"):
            await items.find_and_touch()
        assert await seam.names() == ["original"]

    async def test_derived_and_query_methods_run_in_auto_units(self, seam: Harness) -> None:
        items = seam.ctx.get_bean(SeamItemRepository)
        await items.save_all([SeamItem(name="x"), SeamItem(name="x"), SeamItem(name="y")])
        assert [item.name for item in await items.find_by_name("x")] == ["x", "x"]
        assert await items.count_by_name("x") == 2
        assert [row.name for row in await items.load_named(name="y")] == ["y"]
        assert await items.delete_by_name("x") == 2
        assert await seam.names() == ["y"]

    async def test_soft_delete_writes_commit(self, seam: Harness) -> None:
        soft = seam.ctx.get_bean(SeamSoftRepository)
        saved = await soft.save(SeamSoft(name="soft"))
        await soft.delete_by_id(saved.id)
        assert await soft.find_by_id(saved.id) is None
        assert await soft.count() == 0
        restored = await soft.restore(saved.id)
        assert restored is not None and restored.deleted_at is None
        assert await soft.count() == 1

    async def test_datasource_affinity(self, seam: Harness) -> None:
        reports = seam.ctx.get_bean(SeamReportRepository)
        await reports.save(SeamReport(name="on-reporting"))
        assert await seam.names("seam_report", url=seam.reporting_url) == ["on-reporting"]
        explicit = Repository(SeamReport, datasource="reporting")
        assert await explicit.count() == 1

    async def test_the_after_begin_customizer_runs_in_every_unit(self, seam: Harness) -> None:
        customizer = seam.ctx.get_bean(RecordingCustomizer)
        customizer.calls.clear()
        items = seam.ctx.get_bean(SeamItemRepository)
        await items.save(SeamItem(name="w"))
        await items.count()
        await seam.ctx.get_bean(SeamReportRepository).count()

        @transactional
        async def unit() -> None:
            await items.count()

        await unit()
        assert customizer.calls == ["primary", "primary", "reporting", "primary"]

    async def test_inside_a_unit_every_call_joins_it(self, seam: Harness) -> None:
        items = seam.ctx.get_bean(SeamItemRepository)

        @transactional
        async def work() -> bool:
            await items.save(SeamItem(name="joined"))
            await items.rename_all("joined-renamed")
            return is_transaction_active()

        engine = seam.ctx.get_bean(DataSourceRegistry).primary.engine
        with StatementCounter(engine) as counter:
            assert await work() is True
        assert counter.commits == 1
        assert await seam.names() == ["joined-renamed"]

    async def test_without_a_context_a_managed_repository_says_why(self) -> None:
        with pytest.raises(IllegalTransactionStateError, match="no application context"):
            await Repository(SeamItem).count()


class TestStreamAll:
    async def test_it_owns_its_connection_until_exhausted(self, seam: Harness) -> None:
        items = seam.ctx.get_bean(SeamItemRepository)
        await items.save_all([SeamItem(name=f"s{i}") for i in range(3)])
        seen: list[str] = []
        async for item in items.stream_all():
            seen.append(item.name)
            assert seam.checked_out() == 1
            assert not is_transaction_active()  # the stream's unit is not bound around the consumer's code
        assert seen == ["s0", "s1", "s2"]
        assert seam.checked_out() == 0

    async def test_an_early_close_releases_the_connection(self, seam: Harness) -> None:
        items = seam.ctx.get_bean(SeamItemRepository)
        await items.save_all([SeamItem(name=f"s{i}") for i in range(3)])
        async with contextlib.aclosing(items.stream_all()) as stream:
            async for _item in stream:
                break
        assert seam.checked_out() == 0

    async def test_inside_a_unit_it_uses_the_unit(self, seam: Harness) -> None:
        items = seam.ctx.get_bean(SeamItemRepository)

        @transactional
        async def work() -> list[str]:
            await items.save(SeamItem(name="pending"))  # visible to the stream: same unit, not committed yet
            return [item.name async for item in items.stream_all()]

        assert await work() == ["pending"]

    async def test_iterating_after_the_unit_completed_fails_loudly(self, seam: Harness) -> None:
        items = seam.ctx.get_bean(SeamItemRepository)
        await items.save_all([SeamItem(name=f"s{i}") for i in range(3)])

        @transactional
        async def open_stream() -> AsyncIterator[SeamItem]:
            stream = items.stream_all()
            await stream.__anext__()
            return stream

        stream = await open_stream()
        with pytest.raises(IllegalTransactionStateError, match="already"):
            await stream.__anext__()
        await stream.aclose()

    async def test_select_through_the_session_of_a_custom_method(self, seam: Harness) -> None:
        items = seam.ctx.get_bean(SeamItemRepository)
        await items.save(SeamItem(name="custom"))

        @transactional(read_only=True)
        async def work() -> list[str]:
            result = await items._session.stream_scalars(select(SeamItem))
            return [item.name async for item in result]

        assert await work() == ["custom"]
