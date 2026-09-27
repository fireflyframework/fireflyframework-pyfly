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
"""A second boot of the outbox bus issues no DDL, and ``auto_create_tables=False`` never does.

The adapter used to replay ``CREATE TABLE/INDEX IF NOT EXISTS`` in every process at every boot, so a serving
role needed schema-creation rights for work it never did. The outbox tables are framework tables now: the bus
creates the missing ones through the framework metadata (``ensure_tables``), and when they all exist it only
reads the catalog. These tests count the statements a real SQLite file database receives; the privilege
behaviour itself is proved against PostgreSQL in ``tests/integration/test_eda_postgres_least_privilege.py``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from sqlalchemy import inspect
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from pyfly.data.relational.framework_schema import FrameworkSchemaError
from pyfly.eda.adapters.postgres import PostgresEventBus
from pyfly.testing import StatementCounter
from tests.support.backend_matrix import enable_sqlite_foreign_keys

OUTBOX_TABLES = {
    "pyfly_outbox_events",
    "pyfly_outbox_deliveries",
    "pyfly_outbox_consumers",
    "pyfly_outbox_dead_letters",
}
DDL = ("CREATE", "ALTER", "DROP")


@pytest.fixture
async def engine(tmp_path: Path) -> AsyncIterator[AsyncEngine]:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'outbox.db'}")
    enable_sqlite_foreign_keys(engine)
    try:
        yield engine
    finally:
        await engine.dispose()


async def _tables(engine: AsyncEngine) -> set[str]:
    def names(connection: Connection) -> set[str]:
        return set(inspect(connection).get_table_names())

    async with engine.connect() as connection:
        return await connection.run_sync(names)


async def _boot(engine: AsyncEngine, **options: object) -> StatementCounter:
    bus = PostgresEventBus(datasource=engine, **options)  # type: ignore[arg-type]
    with StatementCounter(engine) as counter:
        try:
            await bus.start()
        finally:
            await bus.stop()
    return counter


async def test_a_first_boot_creates_the_outbox_tables(engine: AsyncEngine) -> None:
    counter = await _boot(engine)
    assert await _tables(engine) >= OUTBOX_TABLES
    assert counter.count("CREATE") >= 4


async def test_a_second_boot_issues_no_ddl(engine: AsyncEngine) -> None:
    await _boot(engine)
    counter = await _boot(engine)
    assert [statement.sql for statement in counter.statements if statement.verb in DDL] == []


async def test_auto_create_tables_false_issues_no_ddl_when_the_tables_exist(engine: AsyncEngine) -> None:
    await _boot(engine)
    counter = await _boot(engine, auto_create_tables=False)
    assert [statement.sql for statement in counter.statements if statement.verb in DDL] == []


async def test_auto_create_tables_false_fails_fast_when_a_table_is_missing(engine: AsyncEngine) -> None:
    with pytest.raises(FrameworkSchemaError, match="pyfly_outbox_events does not exist"):
        await _boot(engine, auto_create_tables=False)
    assert await _tables(engine) == set()


async def test_a_publisher_issues_no_ddl(engine: AsyncEngine) -> None:
    """``publish()`` starts nothing (it used to start the bus, DDL included, on a bus nobody started)."""
    await _boot(engine)
    bus = PostgresEventBus(datasource=engine)
    with StatementCounter(engine) as counter:
        await bus.publish("pyfly.events", "order.created", {"id": 1})
    assert counter.counts() == {"INSERT": 2}  # the event, and what it is owed (an INSERT ... SELECT)
    assert [statement.sql for statement in counter.statements if statement.verb in DDL] == []
    assert bus.running is False


def test_auto_create_tables_defaults_to_on() -> None:
    """A fresh database still just works — the flag is an opt-OUT."""
    assert PostgresEventBus(dsn="postgresql://x/y").outbox.creates_tables is True
