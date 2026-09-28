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
from sqlalchemy.engine import make_url
from sqlalchemy.schema import CreateTable

from pyfly.config.properties.data import RelationalProperties, ddl_auto_strategy
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
    [("create", True), ("create-drop", True), ("none", False), ("validate", False), (None, False)],
)
def test_the_schema_strategy_decides_whether_stores_create_their_tables(ddl_auto: str | None, creates: bool) -> None:
    assert creates_tables(ddl_auto) is creates


def test_update_is_no_strategy_a_store_is_ever_given() -> None:
    """The effective strategy never is ``update`` (PyFly never alters a table at startup), so a store never gets it
    from its registry's properties; passed by hand, it is an unknown value and creates nothing."""
    with pytest.raises(ValueError, match="update is not supported"):
        ddl_auto_strategy("update", url="sqlite+aiosqlite:///app.db")
    with pytest.raises(ValueError, match="update is not supported"):
        RelationalProperties(url="postgresql+asyncpg://db/app", ddl_auto="update")
    assert creates_tables("update") is False


def test_the_ddl_compiles_for_sql_server_with_unicode_keys_and_unbounded_payloads() -> None:
    dialect = mssql.dialect(deprecate_large_types=True)
    ddl = {table.name: str(CreateTable(table).compile(dialect=dialect)) for table in FRAMEWORK_TABLES}

    assert "cache_key NVARCHAR(512) NOT NULL" in ddl[CACHE_ENTRIES]
    assert "value VARBINARY(max) NOT NULL" in ddl[CACHE_ENTRIES]
    assert "expires_at DATETIMEOFFSET NULL" in ddl[CACHE_ENTRIES]
    assert "payload NVARCHAR(max) NOT NULL" in ddl[ORCHESTRATION_STATE]
    assert "lock_until DATETIMEOFFSET NOT NULL" in ddl[LOCKS]
    assert " TEXT" not in "".join(ddl.values())  # an unbounded TEXT key cannot be indexed


@pytest.mark.parametrize(
    ("url", "server_version", "collation"),
    [
        ("mysql://", None, "utf8mb4_0900_bin"),
        ("mysql://", (8, 0, 36), "utf8mb4_0900_bin"),
        ("mysql://", (5, 7, 44), "utf8mb4_bin"),
        ("mariadb://", None, "utf8mb4_nopad_bin"),
        ("mariadb://", (11, 4, 2, "MariaDB"), "utf8mb4_nopad_bin"),
        ("mariadb://", (10, 1, 48, "MariaDB"), "utf8mb4_bin"),
    ],
)
def test_keys_get_a_binary_collation_on_mysql_and_mariadb(
    url: str, server_version: tuple[object, ...] | None, collation: str
) -> None:
    """Their default collations ignore case and accents: ``User:1`` and ``user:1`` were one key there. The
    matrix proves the behavior on MySQL 8 and MariaDB 11; older servers get the binary collation they have."""
    dialect = make_url(url).get_dialect()()
    dialect.server_version_info = server_version

    ddl = str(CreateTable(locks).compile(dialect=dialect))

    assert f"name VARCHAR(255) CHARACTER SET utf8mb4 COLLATE {collation} NOT NULL" in ddl
    assert f"locked_by VARCHAR(255) CHARACTER SET utf8mb4 COLLATE {collation} NOT NULL" in ddl


def test_a_mysql_url_on_a_mariadb_server_gets_the_mariadb_collation() -> None:
    dialect = make_url("mysql://").get_dialect()()
    dialect.is_mariadb = True
    dialect.server_version_info = (11, 4, 2, "MariaDB")

    assert "COLLATE utf8mb4_nopad_bin" in str(CreateTable(cache_entries).compile(dialect=dialect))


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


def test_two_datasource_registry_beans_without_a_primary_are_an_error_not_a_third_registry() -> None:
    """The store's auto-configuration swallowed the ambiguity and built the configuration's registry, a third
    set of pools beside the application's two."""
    from pyfly.container.container import Container
    from pyfly.container.exceptions import NoUniqueBeanError
    from pyfly.core.config import Config
    from pyfly.data.relational.datasource_registry import DataSourceRegistry
    from pyfly.data.relational.framework_schema import context_datasource_registry

    config = Config({"pyfly": {"data": {"relational": {"url": "sqlite+aiosqlite:///:memory:"}}}})
    container = Container()
    container.register_instance(DataSourceRegistry, DataSourceRegistry(config), name="first")
    container.register_instance(DataSourceRegistry, DataSourceRegistry(config), name="second")

    with pytest.raises(NoUniqueBeanError):
        context_datasource_registry(config, container)


def test_without_a_registry_bean_the_configuration_s_registry_is_used() -> None:
    from pyfly.container.container import Container
    from pyfly.core.config import Config
    from pyfly.data.relational.datasource_registry import DataSourceRegistry
    from pyfly.data.relational.framework_schema import context_datasource_registry

    config = Config({"pyfly": {"data": {"relational": {"url": "sqlite+aiosqlite:///:memory:"}}}})
    assert context_datasource_registry(config, Container()) is DataSourceRegistry.for_config(config)
    assert context_datasource_registry(config) is DataSourceRegistry.for_config(config)
