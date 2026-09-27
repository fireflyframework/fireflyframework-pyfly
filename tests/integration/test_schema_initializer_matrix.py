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
"""The schema strategy and the startup migrations on every backend of the matrix.

- C115: an unset ``ddl-auto`` created the tables on a database server too, and instances that started
  together raced on the DDL (``UniqueViolation`` on ``pg_type``, ``DuplicateTable``) and on the startup
  migrations (``alembic_version``): all but one failed to start.
- C117: ``create-drop`` with another connection reading the tables hung ``ctx.stop()`` for 30 s on
  PostgreSQL and MySQL, and on MySQL the abandoned ``DROP`` kept waiting on the server and dropped the table
  later, under every other process.
- ``validate`` finds nothing to report on a schema ``create_all`` built, with the framework's column types.
"""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Boolean, Integer, MetaData, Numeric, String, Unicode, inspect, text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.pool import NullPool

from pyfly.context.application_context import ApplicationContext
from pyfly.core.config import Config
from pyfly.data.relational.datasource_registry import DataSourceRegistry
from pyfly.data.relational.sqlalchemy.entity import Base, BaseEntity, SoftDeleteMixin
from tests.support.backend_matrix import SQLITE_FILE, RelationalBackend


class _MatrixLedger(Base):
    __tablename__ = "wp11_matrix_ledger"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    label: Mapped[str] = mapped_column(String(50))


class _MatrixInvoice(SoftDeleteMixin, BaseEntity):
    """The framework's column types (UtcDateTime, Unicode audit columns, the random key) plus common ones."""

    __tablename__ = "wp11_matrix_invoice"

    number: Mapped[str] = mapped_column(Unicode(40))
    paid: Mapped[bool] = mapped_column(Boolean, default=False)
    amount: Mapped[float] = mapped_column(Numeric(12, 2))


def _config(backend: RelationalBackend, **overrides: Any) -> Config:
    """The lane's configuration with ``ddl-auto`` unset unless *overrides* set it (flat dotted keys)."""
    return backend.config({"pyfly.data.relational.ddl-auto": None, **overrides})


async def _tables(backend: RelationalBackend) -> set[str]:
    engine = create_async_engine(backend.url, poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            return set(await connection.run_sync(lambda sync: inspect(sync).get_table_names()))
    finally:
        await engine.dispose()


# ---------------------------------------------------------------------------------------------------------
# The default strategy (C115)
# ---------------------------------------------------------------------------------------------------------


async def test_an_unset_strategy_creates_tables_on_an_embedded_database_only(
    relational_backend: RelationalBackend,
) -> None:
    context = ApplicationContext(_config(relational_backend))
    await context.start()
    await context.stop()
    created = _MatrixLedger.__tablename__ in await _tables(relational_backend)
    assert created is (relational_backend.lane == SQLITE_FILE)


# ---------------------------------------------------------------------------------------------------------
# Instances starting together (C115)
# ---------------------------------------------------------------------------------------------------------


async def test_instances_that_start_together_all_create_the_schema(relational_backend: RelationalBackend) -> None:
    """Four instances, each with an engine of its own, create the schema at once: every one starts."""
    from pyfly.data.relational.schema import SchemaInitializer

    engines = [_application_engine(relational_backend) for _ in range(4)]
    try:
        schemas = [SchemaInitializer(engine, ddl_auto="create", metadata=_models()) for engine, _registry in engines]
        results = await asyncio.gather(*(schema.start() for schema in schemas), return_exceptions=True)
        assert [result for result in results if isinstance(result, BaseException)] == []
        assert {_MatrixLedger.__tablename__, _MatrixInvoice.__tablename__} <= await _tables(relational_backend)
    finally:
        for _engine, registry in engines:
            await registry.close()


def _models() -> MetaData:
    """The two models of this module alone (``Base.metadata`` holds every model of the test run)."""
    metadata = MetaData()
    for table in (_MatrixLedger.__table__, _MatrixInvoice.__table__):
        table.to_metadata(metadata)  # type: ignore[attr-defined]
    return metadata


def _application_engine(backend: RelationalBackend) -> tuple[AsyncEngine, DataSourceRegistry]:
    """An engine built the way the application builds its primary (the lane's settings included)."""
    registry = DataSourceRegistry(_config(backend))
    return registry.primary.engine, registry


@pytest.mark.backends("sqlite-file", "pg", "mysql")
async def test_the_lease_serializes_instances_where_the_backend_has_no_lock(
    relational_backend: RelationalBackend,
) -> None:
    """The portable lock (a lease of ``pyfly_locks``), which backends without a lock of their own use."""
    from pyfly.data.relational.schema import SchemaInitializer

    engines = [relational_backend.create_engine() for _ in range(3)]
    schemas = [SchemaInitializer(engine, ddl_auto="create", metadata=_models(), lock="lease") for engine in engines]
    await asyncio.gather(*(schema.start() for schema in schemas))
    tables = await _tables(relational_backend)
    assert {_MatrixLedger.__tablename__, "pyfly_locks"} <= tables


async def test_startup_migrations_of_instances_that_start_together_all_apply(
    relational_backend: RelationalBackend, tmp_path: Path
) -> None:
    """Three instances with startup migrations start at once: one applies the revision, the others find it
    applied."""
    ini = _environment(tmp_path)
    flat = {"pyfly.data.relational.migrations.enabled": "true", "pyfly.data.relational.migrations.config": str(ini)}
    contexts = [ApplicationContext(_config(relational_backend, **flat)) for _ in range(3)]
    results = await asyncio.gather(*(context.start() for context in contexts), return_exceptions=True)
    try:
        assert [result for result in results if isinstance(result, BaseException)] == []
        engine = create_async_engine(relational_backend.url, poolclass=NullPool)
        try:
            async with engine.connect() as connection:
                versions = (await connection.execute(text("SELECT version_num FROM alembic_version"))).all()
        finally:
            await engine.dispose()
        assert [row[0] for row in versions] == ["w11m001"]
    finally:
        for context, result in zip(contexts, results, strict=True):
            if not isinstance(result, BaseException):
                await context.stop()


def _environment(root: Path) -> Path:
    """The Alembic environment of ``pyfly db init`` with one revision (a table and seed rows)."""
    from pyfly.cli.db import _ENV_PY_TEMPLATE

    migrations = root / "migrations"
    (migrations / "versions").mkdir(parents=True)
    (migrations / "env.py").write_text(_ENV_PY_TEMPLATE)
    (migrations / "versions" / "w11m001.py").write_text(
        "import sqlalchemy as sa\nfrom alembic import op\n\nrevision = 'w11m001'\ndown_revision = None\n"
        "branch_labels = None\ndepends_on = None\n\n\ndef upgrade() -> None:\n"
        "    table = op.create_table('wp11_migrated_seed', sa.Column('id', sa.Integer(), primary_key=True))\n"
        "    op.bulk_insert(table, [{'id': 1}, {'id': 2}])\n"
    )
    ini = root / "alembic.ini"
    ini.write_text(f"[alembic]\nscript_location = {migrations}\n")
    return ini


# ---------------------------------------------------------------------------------------------------------
# create-drop teardown (C117)
# ---------------------------------------------------------------------------------------------------------


@pytest.mark.backends("pg", "mysql", "mariadb")
async def test_create_drop_beside_an_open_reader_gives_up_on_time_and_never_drops_later(
    relational_backend: RelationalBackend, caplog: pytest.LogCaptureFixture
) -> None:
    from pyfly.data.relational.schema import SchemaInitializer

    engine, registry = _application_engine(relational_backend)
    schema = SchemaInitializer(engine, ddl_auto="create-drop", metadata=_models(), drop_timeout=2.0)
    await schema.start()
    reader = create_async_engine(relational_backend.url, poolclass=NullPool)
    try:
        async with reader.connect() as connection:
            await connection.execute(text(f"SELECT count(*) FROM {_MatrixLedger.__tablename__}"))  # holds its lock
            began = time.monotonic()
            with caplog.at_level(logging.WARNING, logger="pyfly.data.relational.schema"):
                await schema.stop()
            elapsed = time.monotonic() - began
            assert "schema_drop_failed" in caplog.text
            assert await _pending_drops(relational_backend) == 0  # the database gave the DROP up itself
            await connection.rollback()
        await asyncio.sleep(1.0)
        assert _MatrixLedger.__tablename__ in await _tables(relational_backend)  # nothing dropped it later
        assert elapsed < 2.0 + 5.0 + 3.0
    finally:
        await reader.dispose()
        await registry.close()


async def _pending_drops(backend: RelationalBackend) -> int:
    engine = create_async_engine(backend.url, poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            if backend.dialect == "postgresql":
                sql = "SELECT count(*) FROM pg_stat_activity WHERE query ILIKE 'DROP TABLE%' AND state = 'active'"
            else:
                sql = "SELECT count(*) FROM information_schema.processlist WHERE info LIKE 'DROP TABLE%'"
            return int((await connection.execute(text(sql))).scalar() or 0)
    finally:
        await engine.dispose()


async def test_create_drop_drops_the_tables_when_nothing_holds_them(relational_backend: RelationalBackend) -> None:
    from pyfly.data.relational.schema import SchemaInitializer

    engine, registry = _application_engine(relational_backend)
    try:
        schema = SchemaInitializer(engine, ddl_auto="create-drop", metadata=_models())
        await schema.start()
        assert _MatrixLedger.__tablename__ in await _tables(relational_backend)
        await schema.stop()
        tables = await _tables(relational_backend)
        assert _MatrixLedger.__tablename__ not in tables
        assert _MatrixInvoice.__tablename__ not in tables
    finally:
        await registry.close()


# ---------------------------------------------------------------------------------------------------------
# validate
# ---------------------------------------------------------------------------------------------------------


async def test_validate_reports_nothing_on_a_schema_create_all_built(
    relational_backend: RelationalBackend, caplog: pytest.LogCaptureFixture
) -> None:
    from pyfly.data.relational.schema import SchemaInitializer

    metadata = _models()
    engine: AsyncEngine = relational_backend.create_engine()
    async with engine.begin() as connection:
        await connection.run_sync(metadata.create_all)
    with caplog.at_level(logging.WARNING, logger="pyfly.data.relational.schema"):
        await SchemaInitializer(engine, ddl_auto="validate", metadata=metadata).start()
    assert "schema_validation_differences" not in caplog.text, caplog.text
