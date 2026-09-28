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
"""``outside_transaction()``: a block of the calling task that does not join its units, on a SQLite file.

Inside the block the task's units and repository operation scopes are suspended, as ``NOT_SUPPORTED``
suspends one: ``infrastructure_unit()`` opens a short unit of its own, which commits whatever the caller's
unit does next, ``after_commit`` runs at once, and ``is_transaction_active()`` is false. No task is started,
and the binding comes back when the block exits. The suspended units stay visible to SQLite's one-writer
check: a write unit the block would open beside a write unit this task holds is refused at once.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from pyfly.context.application_context import ApplicationContext
from pyfly.core.config import Config
from pyfly.data.relational.auto_configuration import RelationalAutoConfiguration
from pyfly.data.transaction import (
    IllegalTransactionStateError,
    Propagation,
    TransactionTemplate,
    after_commit,
    current_unit_of_work,
    infrastructure_unit,
    is_current_transaction_read_only,
    is_transaction_active,
    outside_transaction,
)
from pyfly.data.transaction.context import current_state


class Rollback(Exception):
    """Raised to roll the caller's unit back."""


class App:
    def __init__(self, ctx: ApplicationContext, url: str) -> None:
        self.ctx = ctx
        self.url = url

    async def names(self) -> list[str]:
        engine = create_async_engine(self.url, poolclass=NullPool)
        try:
            async with engine.connect() as conn:
                return [row[0] for row in (await conn.execute(text("SELECT name FROM outside_item ORDER BY id")))]
        finally:
            await engine.dispose()


@pytest.fixture
async def app(tmp_path: Path) -> AsyncIterator[App]:
    url = f"sqlite+aiosqlite:///{tmp_path / 'outside.db'}"
    engine = create_async_engine(url, poolclass=NullPool)
    async with engine.begin() as conn:
        await conn.execute(text("CREATE TABLE outside_item (id INTEGER PRIMARY KEY, name VARCHAR(64) NOT NULL)"))
    await engine.dispose()
    ctx = ApplicationContext(Config({"pyfly": {"data": {"relational": {"enabled": "true", "url": url}}}}))
    ctx.register_bean(RelationalAutoConfiguration)
    await ctx.start()
    try:
        yield App(ctx, url)
    finally:
        await ctx.stop()


async def _insert(name: str) -> None:
    async with infrastructure_unit("primary") as session:
        await session.execute(text("INSERT INTO outside_item (name) VALUES (:name)"), {"name": name})


async def test_the_block_runs_outside_the_callers_unit(app: App) -> None:
    with pytest.raises(Rollback):
        async with TransactionTemplate(read_only=True).transaction() as caller:
            task = asyncio.current_task()
            with outside_transaction():
                assert is_transaction_active() is False
                assert current_unit_of_work() is None
                assert asyncio.current_task() is task  # no task is started
                async with infrastructure_unit("primary") as session:
                    own = current_unit_of_work("primary")
                    assert own is not None and own is not caller and own.auto
                    await session.execute(text("INSERT INTO outside_item (name) VALUES ('kept')"))
            assert current_unit_of_work() is caller
            assert is_transaction_active() is True
            raise Rollback
    assert await app.names() == ["kept"]  # the caller's rollback did not undo it


async def test_the_binding_comes_back_after_an_exception(app: App) -> None:
    async with TransactionTemplate().transaction() as caller:
        with pytest.raises(ValueError), outside_transaction():
            raise ValueError("inside the block")
        assert current_unit_of_work() is caller
        await _insert("joined")  # the caller's unit is usable again, and joined
    assert await app.names() == ["joined"]


async def test_after_commit_runs_at_once_inside_the_block(app: App) -> None:
    ran: list[str] = []

    async def record() -> None:
        ran.append("callback")

    async with TransactionTemplate().transaction():
        with outside_transaction():
            await after_commit(record)
        assert ran == ["callback"]  # before the caller's unit commits


async def test_a_repository_operation_scope_is_suspended_too(app: App) -> None:
    async with infrastructure_unit("primary", read_only=True):
        scoped = current_unit_of_work("primary")
        assert scoped is not None
        with outside_transaction():
            assert current_unit_of_work("primary") is None
            async with infrastructure_unit("primary", read_only=True):
                assert current_unit_of_work("primary") not in (None, scoped)
        assert current_unit_of_work("primary") is scoped


async def test_a_boundary_inside_the_block_does_not_join_the_callers_unit(app: App) -> None:
    with pytest.raises(Rollback):
        async with TransactionTemplate(read_only=True).transaction() as caller:
            with outside_transaction():
                async with TransactionTemplate(propagation=Propagation.REQUIRED).transaction() as inner:
                    assert inner is not None and inner is not caller
                    await _insert("committed on its own")
            raise Rollback
    assert await app.names() == ["committed on its own"]


async def test_outside_every_unit_it_changes_nothing(app: App) -> None:
    before = current_state()
    with outside_transaction():
        assert current_state() is before
    await _insert("plain")
    assert await app.names() == ["plain"]


async def test_on_sqlite_a_write_beside_the_callers_write_unit_is_refused_at_once(app: App) -> None:
    # The caller's write unit holds the database's one write lock (BEGIN IMMEDIATE): a write unit of the block
    # would wait busy_timeout (5 s) for a lock its own task holds, so it is refused before it starts.
    async with TransactionTemplate().transaction():
        await _insert("caller")
        started = time.perf_counter()
        with pytest.raises(IllegalTransactionStateError, match="write lock"), outside_transaction():
            await _insert("refused")
        assert time.perf_counter() - started < 1.0
        with outside_transaction():
            async with infrastructure_unit("primary", read_only=True) as session:
                assert (await session.execute(text("SELECT count(*) FROM outside_item"))).scalar_one() == 0
    assert await app.names() == ["caller"]


@pytest.mark.parametrize("propagation", [None, Propagation.SUPPORTS, Propagation.NOT_SUPPORTED])
async def test_on_sqlite_a_write_beside_a_write_scope_of_the_task_is_refused_at_once(
    app: App, propagation: Propagation | None
) -> None:
    # A repository call's write auto unit holds the write lock too. Under SUPPORTS or NOT_SUPPORTED with no unit
    # (a suspension marker without a unit is bound for the datasource) the block must still see it.
    boundary: contextlib.AbstractAsyncContextManager[Any] = (
        contextlib.nullcontext() if propagation is None else TransactionTemplate(propagation=propagation).transaction()
    )
    async with boundary, infrastructure_unit("primary") as session:
        await session.execute(text("INSERT INTO outside_item (name) VALUES ('scope')"))
        started = time.perf_counter()
        with pytest.raises(IllegalTransactionStateError, match="write lock"), outside_transaction():
            await _insert("refused")
        assert time.perf_counter() - started < 1.0
    assert await app.names() == ["scope"]


async def test_the_block_is_never_read_only(app: App) -> None:
    async with TransactionTemplate(read_only=True).transaction():
        with outside_transaction():
            assert is_current_transaction_read_only() is False
    # Only a suspension marker is bound here, and the boundary is read-only: the block is not.
    async with TransactionTemplate(propagation=Propagation.NOT_SUPPORTED, read_only=True).transaction():
        assert is_current_transaction_read_only() is True
        with outside_transaction():
            assert is_current_transaction_read_only() is False
        assert is_current_transaction_read_only() is True
