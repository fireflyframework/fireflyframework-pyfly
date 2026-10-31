# Copyright 2026 Firefly Software Foundation
# SPDX-License-Identifier: Apache-2.0
"""Returned synchronizations guard real SQLite commits and release on every outcome."""

import asyncio

import pytest
from sqlalchemy import text

from pyfly.core import Config
from pyfly.data.relational.datasource_registry import DataSourceRegistry
from pyfly.data.transaction import TransactionSynchronizationAdapter, infrastructure_unit


@pytest.fixture
async def source(tmp_path):
    registry = DataSourceRegistry.for_config(
        Config(
            {
                "pyfly": {
                    "data": {"relational": {"url": f"sqlite+aiosqlite:///{tmp_path / 'hooks.db'}", "ddl-auto": "none"}}
                }
            }
        )
    )
    async with registry.primary.engine.begin() as connection:
        await connection.execute(text("CREATE TABLE notes (body TEXT NOT NULL)"))
    yield registry.primary, registry
    await registry.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["commit", "body failure", "commit refusal", "cancel"])
async def test_returned_synchronization_guards_commit_and_completes_on_every_outcome(source, outcome):
    datasource, registry = source
    events = []

    class Synchronization(TransactionSynchronizationAdapter):
        async def before_commit(self, read_only):
            events.append("before commit")
            if outcome == "commit refusal":
                raise ValueError("current authority denied")

        async def before_completion(self):
            events.append("before completion")

        async def after_commit(self):
            events.append("after commit")

        async def after_completion(self, status):
            events.append(status.value)

    class Customizer:
        async def after_begin(self, connection, datasource):
            events.append("begin")
            return Synchronization()

    registry.add_customizer(Customizer())

    async def operation():
        async with infrastructure_unit(datasource) as session:
            events.append("body")
            await session.execute(text("INSERT INTO notes VALUES ('retained')"))
            if outcome == "body failure":
                raise ValueError("body failed")
            if outcome == "cancel":
                raise asyncio.CancelledError()

    if outcome == "commit":
        await operation()
    else:
        with pytest.raises(asyncio.CancelledError if outcome == "cancel" else ValueError):
            await operation()
    expected = ["begin", "body"]
    if outcome in {"commit", "commit refusal"}:
        expected.append("before commit")
    expected.append("before completion")
    if outcome == "commit":
        expected.extend(["after commit", "COMMITTED"])
    else:
        expected.append("ROLLED_BACK")
    assert events == expected
    async with datasource.engine.connect() as connection:
        assert await connection.scalar(text("SELECT count(*) FROM notes")) == (1 if outcome == "commit" else 0)
    assert datasource.engine.pool.checkedout() == 0


@pytest.mark.asyncio
async def test_later_begin_failure_completes_prior_synchronization_and_releases_connection(source):
    datasource, registry = source
    events = []

    class Synchronization(TransactionSynchronizationAdapter):
        async def before_completion(self):
            events.append("before completion")

        async def after_completion(self, status):
            events.append(status.value)

    class First:
        async def after_begin(self, connection, datasource):
            events.append("begin")
            return Synchronization()

    class Failure:
        async def after_begin(self, connection, datasource):
            raise ValueError("begin failed")

    registry.add_customizer(First())
    registry.add_customizer(Failure())
    with pytest.raises(ValueError, match="begin failed"):
        async with infrastructure_unit(datasource):
            raise AssertionError("Body must not start")
    assert events == ["begin", "before completion", "ROLLED_BACK"]
    assert datasource.engine.pool.checkedout() == 0
