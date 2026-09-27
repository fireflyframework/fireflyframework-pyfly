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
"""The framework tables on every relational lane: creation, verification, UTC instants, and what Alembic's
autogenerate makes of them (C027, F12).

Before the framework MetaData, every ``pyfly_*`` table was raw DDL outside any MetaData, so
``pyfly db migrate`` generated a ``DROP TABLE`` for each one, and the SQL stores never created theirs.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta, timezone

import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import Column, Integer, MetaData, String, Table, insert, inspect, select, text
from sqlalchemy.engine import Connection, make_url
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from pyfly.data.relational.framework_schema import (
    FrameworkSchemaError,
    UtcTimestamp,
    cache_entries,
    ensure_tables,
    framework_metadata,
    locks,
    orchestration_state,
    users,
)
from tests.support.backend_matrix import PG, RelationalBackend

FRAMEWORK_TABLES = (orchestration_state, cache_entries, locks, users)


def _table_names(connection: Connection) -> set[str]:
    return {name.lower() for name in inspect(connection).get_table_names()}


async def test_ensure_tables_creates_every_framework_table_once(relational_backend: RelationalBackend) -> None:
    engine = relational_backend.create_engine()

    await ensure_tables(engine, *FRAMEWORK_TABLES)
    await ensure_tables(engine, *FRAMEWORK_TABLES)  # a second start finds them and changes nothing

    async with engine.connect() as connection:
        names = await connection.run_sync(_table_names)
    assert {table.name for table in FRAMEWORK_TABLES} <= names


async def test_two_processes_creating_the_tables_together_both_start(relational_backend: RelationalBackend) -> None:
    first, second = relational_backend.create_engine(), relational_backend.create_engine()

    await asyncio.gather(ensure_tables(first, *FRAMEWORK_TABLES), ensure_tables(second, *FRAMEWORK_TABLES))

    async with first.connect() as connection:
        assert {table.name for table in FRAMEWORK_TABLES} <= await connection.run_sync(_table_names)


async def test_a_missing_table_fails_fast_when_the_store_may_not_create_it(
    relational_backend: RelationalBackend,
) -> None:
    engine = relational_backend.create_engine()

    with pytest.raises(FrameworkSchemaError, match="table pyfly_locks does not exist"):
        await ensure_tables(engine, locks, create=False)


async def test_a_table_without_a_declared_column_fails_fast(relational_backend: RelationalBackend) -> None:
    engine = relational_backend.create_engine()
    async with engine.begin() as connection:
        await connection.execute(text("CREATE TABLE pyfly_locks (name VARCHAR(255) PRIMARY KEY)"))

    with pytest.raises(FrameworkSchemaError, match=r"pyfly_locks\.lock_until does not exist"):
        await ensure_tables(engine, locks)


@pytest.mark.backends(PG)
async def test_a_timestamp_without_time_zone_on_postgresql_fails_fast_with_the_fix(
    relational_backend: RelationalBackend,
) -> None:
    """The saga table the old DDL created had TIMESTAMP columns, where aware values fail to bind (C080)."""
    engine = relational_backend.create_engine()
    async with engine.begin() as connection:
        await connection.execute(
            text(
                "CREATE TABLE pyfly_locks (name VARCHAR(255) PRIMARY KEY, lock_until TIMESTAMP NOT NULL, "
                "locked_at TIMESTAMPTZ NOT NULL, locked_by VARCHAR(255) NOT NULL, fence BIGINT NOT NULL)"
            )
        )

    with pytest.raises(FrameworkSchemaError, match="ALTER TABLE pyfly_locks ALTER COLUMN lock_until TYPE TIMESTAMPTZ"):
        await ensure_tables(engine, locks)


def _autogenerate(connection: Connection, target: MetaData | list[MetaData]) -> list[tuple[object, ...]]:
    context = MigrationContext.configure(connection)
    return [diff for diff in compare_metadata(context, target) if isinstance(diff, tuple)]


async def test_autogenerate_keeps_the_framework_tables_listed_in_target_metadata(
    relational_backend: RelationalBackend,
) -> None:
    """C027: an env.py whose target_metadata holds only the application's models drops every framework table;
    with the framework MetaData listed too, autogenerate finds nothing to change (no drop, no spurious alter)."""
    application = MetaData()
    orders = Table("wp10a_orders", application, Column("id", Integer, primary_key=True), Column("name", String(64)))
    engine = relational_backend.create_engine()
    await relational_backend.create_tables(orders)
    # Every table declared so far: a store built with a custom table name declares it here as well.
    await ensure_tables(engine, *framework_metadata.sorted_tables)

    async with engine.connect() as connection:
        application_only = await connection.run_sync(_autogenerate, application)
        both = await connection.run_sync(_autogenerate, [application, framework_metadata])

    dropped = {diff[1].name for diff in application_only if diff[0] == "remove_table"}  # type: ignore[attr-defined]
    assert {table.name for table in FRAMEWORK_TABLES} <= dropped
    assert both == []


async def test_utc_timestamp_keeps_the_instant_and_its_microseconds(relational_backend: RelationalBackend) -> None:
    instants = Table(
        "wp10a_instants", MetaData(), Column("id", Integer, primary_key=True), Column("at", UtcTimestamp())
    )
    await relational_backend.create_tables(instants)
    engine = relational_backend.create_engine()
    madrid = timezone(timedelta(hours=2))
    aware = datetime(2026, 3, 29, 3, 30, 15, 123456, tzinfo=madrid)
    naive = datetime(2026, 3, 29, 1, 0, 0, 654321)  # taken as UTC

    async with engine.begin() as connection:
        await connection.execute(insert(instants), [{"id": 1, "at": aware}, {"id": 2, "at": naive}])
    async with engine.connect() as connection:
        rows = dict((await connection.execute(select(instants.c.id, instants.c.at))).all())
        # Bound values in another zone compare as instants in SQL too.
        window = (
            await connection.execute(
                select(instants.c.id).where(
                    instants.c.at.between(
                        datetime(2026, 3, 29, 3, 0, tzinfo=madrid), datetime(2026, 3, 29, 3, 1, tzinfo=madrid)
                    )
                )
            )
        ).scalars()
        in_window = list(window)

    assert rows[1] == aware and rows[1].tzinfo == UTC and rows[1].microsecond == 123456
    assert rows[2] == naive.replace(tzinfo=UTC) and rows[2].tzinfo == UTC and rows[2].microsecond == 654321
    assert in_window == [2]  # 01:00:00.654321 UTC is 03:00:00.654321 at +02:00


async def test_a_table_an_earlier_release_created_gets_its_missing_indexes(
    relational_backend: RelationalBackend,
) -> None:
    """The cache table used to be created without the ``expires_at`` index its purge now filters on."""
    earlier = MetaData()
    cache_entries.to_metadata(earlier).indexes.clear()
    engine = relational_backend.create_engine()
    async with engine.begin() as connection:
        await connection.run_sync(earlier.create_all)

    await ensure_tables(engine, cache_entries)

    def indexes(connection: Connection) -> set[str]:
        return {index["name"] for index in inspect(connection).get_indexes("pyfly_cache_entries")}

    async with engine.connect() as connection:
        assert "ix_pyfly_cache_entries_expires_at" in await connection.run_sync(indexes)


@pytest.mark.backends(PG)
async def test_on_postgresql_a_missing_index_is_built_while_the_old_nodes_keep_writing(
    relational_backend: RelationalBackend,
) -> None:
    """A plain ``CREATE INDEX`` takes a ``SHARE`` lock: on a large cache table it held up every write of the
    nodes still running the earlier release, for as long as the build took, during a rolling deploy. The index
    is now built ``CONCURRENTLY``: it waits for the transactions already open, and writes go on meanwhile."""
    earlier = MetaData()
    cache_entries.to_metadata(earlier).indexes.clear()
    engine = relational_backend.create_engine()
    async with engine.begin() as connection:
        await connection.run_sync(earlier.create_all)
    admin = relational_backend.create_engine(isolation_level="AUTOCOMMIT")
    old_node = relational_backend.create_engine()
    insert_entry = text("INSERT INTO pyfly_cache_entries (cache_key, value) VALUES (:key, decode('00', 'hex'))")

    async with old_node.connect() as open_transaction:
        await open_transaction.execute(insert_entry, {"key": "written-before-the-deploy"})
        building = asyncio.create_task(ensure_tables(engine, cache_entries))
        for _ in range(200):  # until the build waits for the open transaction
            async with admin.connect() as connection:
                waiting = await connection.execute(
                    text(
                        "SELECT 1 FROM pg_stat_activity WHERE datname = current_database() "
                        "AND query ILIKE 'CREATE INDEX%' AND wait_event_type = 'Lock'"
                    )
                )
                if waiting.first() is not None:
                    break
            await asyncio.sleep(0.05)
        else:
            pytest.fail("the index build never started")

        async with old_node.begin() as writer:  # another old node's write goes through meanwhile
            await writer.execute(text("SET LOCAL lock_timeout = '2s'"))
            await writer.execute(insert_entry, {"key": "written-during-the-build"})
        await open_transaction.commit()

    await asyncio.wait_for(building, timeout=30)
    async with admin.connect() as connection:
        valid = await connection.execute(
            text("SELECT indisvalid FROM pg_index WHERE indexrelid = 'ix_pyfly_cache_entries_expires_at'::regclass")
        )
        assert valid.scalar_one() is True


@contextlib.asynccontextmanager
async def _engine_that_cannot_build_indexes(backend: RelationalBackend) -> AsyncIterator[AsyncEngine]:
    """An engine on the lane's database that reads and writes its tables but may not build an index: a
    read-only file on SQLite, a role that does not own the tables on PostgreSQL, a user without the ``INDEX``
    privilege on MySQL and MariaDB."""
    url = make_url(backend.url)
    if backend.is_embedded:
        engine = create_async_engine(f"sqlite+aiosqlite:///file:{url.database}?mode=ro&uri=true")
        try:
            yield engine
        finally:
            await engine.dispose()
        return
    user = f"wp10a_ix_{uuid.uuid4().hex[:8]}"
    admin = backend.create_engine(isolation_level="AUTOCOMMIT")
    postgresql = backend.dialect == "postgresql"
    async with admin.connect() as connection:
        if postgresql:
            await connection.execute(text(f"CREATE ROLE {user} LOGIN PASSWORD 'pw'"))
            await connection.execute(
                text(f"GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO {user}")
            )
        else:
            await connection.execute(text(f"CREATE USER '{user}'@'%' IDENTIFIED BY 'pw'"))
            await connection.execute(
                text(f"GRANT SELECT, INSERT, UPDATE, DELETE ON `{url.database}`.* TO '{user}'@'%'")
            )
    engine = create_async_engine(url.set(username=user, password="pw"), **backend.engine_options())
    try:
        yield engine
    finally:
        await engine.dispose()
        async with admin.connect() as connection:
            if postgresql:
                await connection.execute(text(f"DROP OWNED BY {user}"))
                await connection.execute(text(f"DROP ROLE {user}"))
            else:
                await connection.execute(text(f"DROP USER '{user}'@'%'"))


async def test_a_missing_index_that_could_not_be_built_is_reported(
    relational_backend: RelationalBackend, caplog: pytest.LogCaptureFixture
) -> None:
    """Every attempt to build the missing index failed (a user that may not build one): the failure was logged
    at DEBUG, and then an INFO said the tables had been created by another process. The purge ran without its
    index and nothing said so."""
    earlier = MetaData()
    cache_entries.to_metadata(earlier).indexes.clear()
    async with relational_backend.create_engine().begin() as connection:
        await connection.run_sync(earlier.create_all)

    async with _engine_that_cannot_build_indexes(relational_backend) as engine:
        with caplog.at_level(logging.INFO, logger="pyfly.data.relational.framework_schema"):
            await ensure_tables(engine, cache_entries)  # the table and its columns are there: it starts

    messages = [record.getMessage() for record in caplog.records]
    assert "framework_tables_created_concurrently" not in messages
    assert "framework_schema_changes_failed" in messages
    [missing] = [record for record in caplog.records if record.getMessage() == "framework_index_missing"]
    assert missing.levelno == logging.WARNING
    assert missing.__dict__["index"] == "ix_pyfly_cache_entries_expires_at"
    assert missing.__dict__["table"] == "pyfly_cache_entries"


async def test_a_missing_index_is_reported_when_migrations_own_the_schema(
    relational_backend: RelationalBackend, caplog: pytest.LogCaptureFixture
) -> None:
    earlier = MetaData()
    cache_entries.to_metadata(earlier).indexes.clear()
    engine = relational_backend.create_engine()
    async with engine.begin() as connection:
        await connection.run_sync(earlier.create_all)

    with caplog.at_level(logging.WARNING, logger="pyfly.data.relational.framework_schema"):
        await ensure_tables(engine, cache_entries, create=False)

    [missing] = [record for record in caplog.records if record.getMessage() == "framework_index_missing"]
    assert missing.__dict__["index"] == "ix_pyfly_cache_entries_expires_at"
    assert "migration" in missing.__dict__["hint"]


@pytest.mark.backends(PG)
async def test_an_invalid_index_an_interrupted_concurrent_build_left_is_reported(
    relational_backend: RelationalBackend, caplog: pytest.LogCaptureFixture
) -> None:
    """A concurrent build that fails or is interrupted leaves an INVALID index, which ``CREATE INDEX
    CONCURRENTLY IF NOT EXISTS`` skips: the index was never used, never rebuilt, and nothing said so."""
    earlier = MetaData()
    cache_entries.to_metadata(earlier).indexes.clear()
    engine = relational_backend.create_engine()
    async with engine.begin() as connection:
        await connection.run_sync(earlier.create_all)
        await connection.execute(
            text(
                "INSERT INTO pyfly_cache_entries (cache_key, value, expires_at) VALUES "
                "('a', decode('00', 'hex'), '2026-01-01T00:00:00Z'), ('b', decode('00', 'hex'), '2026-01-01T00:00:00Z')"
            )
        )
    admin = relational_backend.create_engine(isolation_level="AUTOCOMMIT")
    async with admin.connect() as connection:
        with pytest.raises(Exception, match="could not create unique index"):
            await connection.execute(
                text(
                    "CREATE UNIQUE INDEX CONCURRENTLY ix_pyfly_cache_entries_expires_at "
                    "ON pyfly_cache_entries (expires_at)"
                )
            )

    with caplog.at_level(logging.WARNING, logger="pyfly.data.relational.framework_schema"):
        await ensure_tables(engine, cache_entries)

    [invalid] = [record for record in caplog.records if record.getMessage() == "framework_index_invalid"]
    assert invalid.__dict__["index"] == "ix_pyfly_cache_entries_expires_at"
    assert "REINDEX INDEX CONCURRENTLY ix_pyfly_cache_entries_expires_at" in invalid.__dict__["hint"]
