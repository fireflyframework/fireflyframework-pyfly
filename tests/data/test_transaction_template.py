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
"""The programmatic ``TransactionTemplate`` (WP01-09, WP01-10), on two SQLite file databases.

The template runs on the datasource it is given, whichever way it is named: as its target
(``TransactionTemplate("reporting")``), as a setting (``TransactionTemplate(datasource="reporting")``) or as
a per-call override (``template.transaction(datasource="reporting")``). A target and a ``datasource`` that
disagree are refused instead of one of them winning silently. Propagation, timeout and the additive
rollback rules behave through ``transaction()`` and ``execute()`` as they do through ``@transactional``.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Identity, Integer, String, text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.pool import NullPool

from pyfly.context.application_context import ApplicationContext
from pyfly.core.config import Config
from pyfly.data.relational.auto_configuration import RelationalAutoConfiguration
from pyfly.data.relational.datasource_registry import DataSourceRegistry
from pyfly.data.relational.sqlalchemy.entity import Base
from pyfly.data.transaction import (
    IllegalTransactionStateError,
    Propagation,
    TransactionTemplate,
    TransactionTimedOutError,
)
from pyfly.data.transaction.unit_of_work import UnitOfWork


class TplItem(Base):
    __tablename__ = "tpl_item"

    id: Mapped[int] = mapped_column(Integer, Identity(), primary_key=True)
    name: Mapped[str] = mapped_column(String(64))


class Databases:
    def __init__(self, ctx: ApplicationContext, urls: dict[str, str]) -> None:
        self.ctx = ctx
        self.urls = urls

    def checked_out(self) -> int:
        registry = self.ctx.get_bean(DataSourceRegistry)
        return sum(ds.engine.sync_engine.pool.checkedout() for ds in registry.all_datasources())

    async def names(self, datasource: str) -> list[str]:
        engine = create_async_engine(self.urls[datasource], poolclass=NullPool)
        try:
            async with engine.connect() as conn:
                return [r[0] for r in (await conn.execute(text("SELECT name FROM tpl_item ORDER BY id"))).all()]
        finally:
            await engine.dispose()


@pytest.fixture
async def databases(tmp_path: Path) -> AsyncIterator[Databases]:
    urls = {name: f"sqlite+aiosqlite:///{tmp_path / f'{name}.db'}" for name in ("primary", "reporting")}
    for url in urls.values():
        engine = create_async_engine(url, poolclass=NullPool)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all, tables=[TplItem.__table__])
        await engine.dispose()
    relational: dict[str, Any] = {
        "enabled": "true",
        "url": urls["primary"],
        "ddl-auto": "none",
        "datasources": {"reporting": {"url": urls["reporting"]}},
    }
    ctx = ApplicationContext(Config({"pyfly": {"data": {"relational": relational}}}))
    ctx.register_bean(RelationalAutoConfiguration)
    await ctx.start()
    started = Databases(ctx, urls)
    try:
        yield started
        assert started.checked_out() == 0
    finally:
        await ctx.stop()


def _session(unit: UnitOfWork | None) -> AsyncSession:
    assert unit is not None
    session = unit.resource
    assert isinstance(session, AsyncSession)
    return session


async def _insert(unit: UnitOfWork | None, name: str) -> None:
    await _session(unit).execute(text("INSERT INTO tpl_item (name) VALUES (:name)"), {"name": name})


class TestDatasource:
    @pytest.mark.parametrize(
        ("template", "overrides"),
        [
            (TransactionTemplate("reporting"), {}),
            (TransactionTemplate(datasource="reporting"), {}),
            (TransactionTemplate(), {"datasource": "reporting"}),
            (TransactionTemplate("reporting", datasource="reporting"), {}),
        ],
        ids=["target", "setting", "override", "both-agree"],
    )
    async def test_the_unit_runs_on_the_named_datasource(
        self, databases: Databases, template: TransactionTemplate, overrides: dict[str, Any]
    ) -> None:
        async with template.transaction(**overrides) as unit:
            assert unit is not None and unit.datasource == "reporting"
            await _insert(unit, "on-reporting")
        assert await databases.names("reporting") == ["on-reporting"]
        assert await databases.names("primary") == []

    async def test_execute_runs_on_the_named_datasource(self, databases: Databases) -> None:
        async def work(name: str) -> str:
            from pyfly.data.transaction import current_unit_of_work

            unit = current_unit_of_work("reporting")
            await _insert(unit, name)
            return name

        assert await TransactionTemplate(datasource="reporting").execute(work, "executed") == "executed"
        assert await databases.names("reporting") == ["executed"]
        assert await databases.names("primary") == []

    async def test_no_datasource_runs_on_the_primary(self, databases: Databases) -> None:
        async with TransactionTemplate().transaction() as unit:
            assert unit is not None and unit.datasource == "primary"
            await _insert(unit, "on-primary")
        assert await databases.names("primary") == ["on-primary"]

    @pytest.mark.parametrize(
        ("template", "overrides"),
        [
            (TransactionTemplate("primary", datasource="reporting"), {}),
            (TransactionTemplate("reporting"), {"datasource": "primary"}),
        ],
        ids=["setting", "override"],
    )
    async def test_a_target_and_a_datasource_that_disagree_are_refused(
        self, databases: Databases, template: TransactionTemplate, overrides: dict[str, Any]
    ) -> None:
        with pytest.raises(IllegalTransactionStateError, match="reporting"):
            async with template.transaction(**overrides):
                pytest.fail("the block must not run")
        assert await databases.names("primary") == []
        assert await databases.names("reporting") == []

    async def test_a_manager_target_that_disagrees_is_refused(self, databases: Databases) -> None:
        primary = TransactionTemplate("primary").manager()
        with pytest.raises(IllegalTransactionStateError, match="reporting"):
            TransactionTemplate(primary, datasource="reporting").manager()


class TestSemantics:
    async def test_requires_new_commits_on_its_own(self, databases: Databases) -> None:
        template = TransactionTemplate()
        # The outer unit only reads: SQLite has one writer, and a suspended writer would hold its lock.
        with pytest.raises(ValueError, match="outer"):
            async with template.transaction(read_only=True) as outer:
                await _session(outer).execute(text("SELECT count(*) FROM tpl_item"))
                async with template.transaction(propagation=Propagation.REQUIRES_NEW) as inner:
                    assert inner is not outer
                    await _insert(inner, "inner")
                raise ValueError("the outer unit fails")
        assert await databases.names("primary") == ["inner"]

    async def test_nested_rolls_back_to_its_savepoint(self, databases: Databases) -> None:
        template = TransactionTemplate()
        async with template.transaction() as outer:
            await _insert(outer, "outer")
            with pytest.raises(KeyError):
                async with template.transaction(propagation=Propagation.NESTED) as nested:
                    assert nested is outer
                    await _insert(nested, "nested")
                    raise KeyError("the nested step fails")
            await _insert(outer, "outer-after")
        assert await databases.names("primary") == ["outer", "outer-after"]

    async def test_mandatory_without_a_unit_is_refused(self, databases: Databases) -> None:
        with pytest.raises(IllegalTransactionStateError, match="MANDATORY"):
            async with TransactionTemplate(propagation=Propagation.MANDATORY).transaction():
                pytest.fail("the block must not run")

    async def test_the_timeout_rolls_back(self, databases: Databases) -> None:
        async def slow() -> None:
            from pyfly.data.transaction import current_unit_of_work

            await _insert(current_unit_of_work("primary"), "slow")
            await asyncio.sleep(5)

        with pytest.raises(TransactionTimedOutError):
            await TransactionTemplate(timeout=0.1).execute(slow)
        with pytest.raises(TransactionTimedOutError):
            async with TransactionTemplate().transaction(timeout=0.1) as unit:
                await _insert(unit, "slow-block")
                await asyncio.sleep(5)
        assert await databases.names("primary") == []

    async def test_no_rollback_for_commits_and_reraises(self, databases: Databases) -> None:
        template = TransactionTemplate(no_rollback_for=(KeyError,))
        with pytest.raises(KeyError):
            async with template.transaction() as unit:
                await _insert(unit, "kept")
                raise KeyError("an expected miss")
        assert await databases.names("primary") == ["kept"]

    async def test_a_narrowed_rollback_for_still_rolls_back_other_exceptions(self, databases: Databases) -> None:
        template = TransactionTemplate(rollback_for=(LookupError,))
        with pytest.raises(RuntimeError):
            async with template.transaction() as unit:
                await _insert(unit, "discarded")
                raise RuntimeError("not a LookupError, still rolls back")
        assert await databases.names("primary") == []

    async def test_per_call_overrides_do_not_change_the_template(self, databases: Databases) -> None:
        template = TransactionTemplate(read_only=True)
        async with template.transaction(read_only=False) as unit:
            await _insert(unit, "written")
        assert template.definition.read_only is True
        assert await databases.names("primary") == ["written"]
