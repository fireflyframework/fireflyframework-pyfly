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
"""The framework MetaData without a database: declarations, custom names, the schema strategy, and the DDL
and upsert fallbacks compiled for SQL Server and Oracle, which the matrix does not run (C069).

The behavior on SQLite, PostgreSQL, MySQL and MariaDB is proven on real databases in
``tests/integration/test_framework_schema_matrix.py`` and ``tests/integration/test_upsert_matrix.py``.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import insert, literal, select
from sqlalchemy.dialects import mssql, oracle
from sqlalchemy.schema import CreateTable

from pyfly.data.relational.framework_schema import (
    CACHE_ENTRIES,
    FRAMEWORK_TABLE_PREFIX,
    LOCKS,
    ORCHESTRATION_STATE,
    USERS,
    cache_entries,
    creates_tables,
    framework_metadata,
    locks,
    orchestration_state,
    orchestration_state_table,
    users,
    users_table,
)
from pyfly.data.relational.upsert import update_statement

FRAMEWORK_TABLES = (orchestration_state, cache_entries, locks, users)


def test_every_framework_table_is_on_the_framework_metadata_under_the_prefix() -> None:
    assert {ORCHESTRATION_STATE, CACHE_ENTRIES, LOCKS, USERS} <= set(framework_metadata.tables)
    for table in FRAMEWORK_TABLES:
        assert table.metadata is framework_metadata
        assert table.name.startswith(FRAMEWORK_TABLE_PREFIX)


def test_a_custom_table_name_is_declared_once_on_the_framework_metadata() -> None:
    custom = orchestration_state_table("wp10a_custom_orchestration")
    try:
        assert orchestration_state_table("wp10a_custom_orchestration") is custom
        assert framework_metadata.tables["wp10a_custom_orchestration"] is custom
        assert [column.name for column in custom.columns] == [column.name for column in orchestration_state.columns]
    finally:
        framework_metadata.remove(custom)


def test_a_name_taken_by_another_framework_table_is_refused() -> None:
    with pytest.raises(ValueError, match="already declared as the framework's 'locks' table"):
        users_table(LOCKS)


def test_a_table_name_that_is_not_an_identifier_is_refused() -> None:
    with pytest.raises(ValueError, match="Invalid framework table name"):
        users_table("users; DROP TABLE x")


@pytest.mark.parametrize(
    ("ddl_auto", "creates"),
    [("create", True), ("create-drop", True), ("update", True), ("none", False), ("validate", False), (None, False)],
)
def test_the_schema_strategy_decides_whether_stores_create_their_tables(ddl_auto: str | None, creates: bool) -> None:
    assert creates_tables(ddl_auto) is creates


def test_the_ddl_compiles_for_sql_server_with_unicode_keys_and_unbounded_payloads() -> None:
    dialect = mssql.dialect(deprecate_large_types=True)
    ddl = {table.name: str(CreateTable(table).compile(dialect=dialect)) for table in FRAMEWORK_TABLES}

    assert "cache_key NVARCHAR(512) NOT NULL" in ddl[CACHE_ENTRIES]
    assert "value VARBINARY(max) NOT NULL" in ddl[CACHE_ENTRIES]
    assert "expires_at DATETIMEOFFSET NULL" in ddl[CACHE_ENTRIES]
    assert "payload NVARCHAR(max) NOT NULL" in ddl[ORCHESTRATION_STATE]
    assert "lock_until DATETIMEOFFSET NOT NULL" in ddl[LOCKS]
    assert " TEXT" not in "".join(ddl.values())  # an unbounded TEXT key cannot be indexed


def test_the_ddl_compiles_for_oracle_with_time_zone_aware_timestamps() -> None:
    dialect = oracle.dialect()
    ddl = {table.name: str(CreateTable(table).compile(dialect=dialect)) for table in FRAMEWORK_TABLES}

    assert "started_at TIMESTAMP WITH TIME ZONE NOT NULL" in ddl[ORCHESTRATION_STATE]
    assert "lock_until TIMESTAMP WITH TIME ZONE NOT NULL" in ddl[LOCKS]
    assert " DATE" not in "".join(ddl.values())  # a DATE keeps whole seconds


@pytest.mark.parametrize("dialect", [mssql.dialect(), oracle.dialect()], ids=["mssql", "oracle"])
def test_the_portable_upsert_statements_compile_where_there_is_no_native_upsert(dialect: object) -> None:
    now = datetime(2026, 9, 27, tzinfo=UTC)
    values = {"name": "job", "lock_until": now, "locked_at": now, "locked_by": "node-a", "fence": 1}

    conditional = update_statement(
        locks, values, key=["name"], where=lambda existing, incoming: existing.lock_until < incoming.lock_until
    )
    updating = str(conditional.compile(dialect=dialect))  # type: ignore[arg-type]
    inserting = str(insert(locks).values(values).compile(dialect=dialect))  # type: ignore[arg-type]
    probe = str(select(literal(1)).compile(dialect=dialect))  # type: ignore[arg-type]

    assert updating.startswith("UPDATE pyfly_locks SET lock_until=")
    assert "WHERE pyfly_locks.name = " in updating and "pyfly_locks.lock_until < " in updating
    assert inserting.startswith("INSERT INTO pyfly_locks (name, lock_until, locked_at, locked_by, fence)")
    assert "ON CONFLICT" not in updating + inserting and "DUPLICATE" not in updating + inserting
    if isinstance(dialect, oracle.dialect):
        assert "FROM DUAL" in probe  # a bare SELECT 1 fails on Oracle
