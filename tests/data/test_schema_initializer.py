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
"""The schema strategy (``pyfly.data.relational.ddl-auto``) on SQLite files, through the real classes.

- C115: the strategy defaulted to ``create`` everywhere and any typo (``validate``, ``update``, ``NONE ``,
  YAML ``false``) silently became ``create``; replicas starting together raced on the DDL.
- C118: ``create_all`` ran after the startup migrations and supplied the tables a migration forgot.
- C117: ``create-drop`` teardown waited on another connection's lock, raised at DEBUG, and skipped the
  engine's disposal.

The server lanes (PostgreSQL, MySQL, MariaDB) are in ``tests/integration/test_schema_initializer_matrix.py``.
"""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Column, Integer, MetaData, String, Table, inspect, text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.orm import Mapped, mapped_column

from pyfly.config.properties.data import RelationalProperties
from pyfly.container.exceptions import BeanCreationException
from pyfly.context.application_context import ApplicationContext
from pyfly.core.config import Config
from pyfly.data.relational.datasource_registry import DataSourceRegistry
from pyfly.data.relational.sqlalchemy.entity import Base


def _sqlite(path: Path) -> str:
    return f"sqlite+aiosqlite:///{path}"


def _relational(**relational: Any) -> Config:
    return Config({"pyfly": {"data": {"relational": {"enabled": "true", **relational}}}})


def _metadata() -> tuple[MetaData, Table]:
    metadata = MetaData()
    table = Table(
        "wp11_schema_account",
        metadata,
        Column("id", Integer, primary_key=True),
        Column("email", String(100), nullable=False),
    )
    return metadata, table


async def _tables(engine: AsyncEngine) -> set[str]:
    async with engine.connect() as connection:
        return set(await connection.run_sync(lambda sync: inspect(sync).get_table_names()))


def _registry_engine(url: str) -> tuple[DataSourceRegistry, AsyncEngine]:
    """An engine the way the application builds it (SQLite pragmas, the BEGIN recipe)."""
    registry = DataSourceRegistry(Config({"pyfly": {"data": {"relational": {"url": url}}}}))
    return registry, registry.primary.engine


# ---------------------------------------------------------------------------------------------------------
# The strategy's value (C115)
# ---------------------------------------------------------------------------------------------------------


class TestStrategyValue:
    @pytest.mark.parametrize(
        ("url", "expected"),
        [
            (None, "create"),  # the dev profile's SQLite file is the only primary without a URL
            ("sqlite+aiosqlite:///app.db", "create"),
            ("sqlite+aiosqlite:///:memory:", "create"),
            ("postgresql+asyncpg://app:secret@db:5432/app", "none"),
            ("mysql+asyncmy://app:secret@db/app", "none"),
            ("mariadb+asyncmy://app:secret@db/app", "none"),
        ],
    )
    def test_unset_it_creates_on_an_embedded_database_only(self, url: str | None, expected: str) -> None:
        relational: dict[str, Any] = {} if url is None else {"url": url}
        assert RelationalProperties.from_config(_relational(**relational)).ddl_auto == expected

    def test_unset_it_leaves_the_schema_to_the_startup_migrations(self) -> None:
        config = _relational(url="sqlite+aiosqlite:///app.db", migrations={"enabled": "true"})
        assert RelationalProperties.from_config(config).ddl_auto == "none"

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            ("none", "none"),
            ("NONE ", "none"),
            ("validate", "validate"),
            ("Create", "create"),
            ("create-drop", "create-drop"),
            (False, "none"),  # YAML reads `ddl-auto: false` and `ddl-auto: off` as a boolean
            ("off", "none"),
        ],
    )
    def test_every_accepted_spelling(self, value: Any, expected: str) -> None:
        config = _relational(url="postgresql+asyncpg://app@db/app", **{"ddl-auto": value})
        assert RelationalProperties.from_config(config).ddl_auto == expected

    @pytest.mark.parametrize("value", ["bogus", "create_drop", "creat", True])
    def test_an_unknown_value_fails(self, value: Any) -> None:
        with pytest.raises(ValueError, match="ddl-auto must be one of none, validate, create, create-drop"):
            RelationalProperties.from_config(_relational(url="sqlite+aiosqlite:///app.db", **{"ddl-auto": value}))

    def test_update_fails_and_points_at_migrations(self) -> None:
        with pytest.raises(ValueError, match="update is not supported.*pyfly db migrate"):
            RelationalProperties.from_config(_relational(url="sqlite+aiosqlite:///app.db", **{"ddl-auto": "update"}))

    @pytest.mark.parametrize("value", ["create", "create-drop"])
    def test_creating_tables_beside_startup_migrations_fails(self, value: str) -> None:
        config = _relational(url="sqlite+aiosqlite:///app.db", migrations={"enabled": "true"}, **{"ddl-auto": value})
        with pytest.raises(ValueError, match="beside pyfly.data.relational.migrations.enabled=true"):
            RelationalProperties.from_config(config)

    def test_validate_beside_startup_migrations_is_the_recommended_pairing(self) -> None:
        config = _relational(
            url="sqlite+aiosqlite:///app.db", migrations={"enabled": "true"}, **{"ddl-auto": "validate"}
        )
        assert RelationalProperties.from_config(config).ddl_auto == "validate"

    def test_the_environment_can_switch_it_off(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("PYFLY_DATA_RELATIONAL_DDL_AUTO", "None")
        assert RelationalProperties.from_config(_relational(url="sqlite+aiosqlite:///app.db")).ddl_auto == "none"

    async def test_a_context_with_an_unknown_value_does_not_start(self, tmp_path: Path) -> None:
        context = ApplicationContext(_relational(url=_sqlite(tmp_path / "app.db"), **{"ddl-auto": "validat"}))
        with pytest.raises(BeanCreationException, match="ddl-auto must be one of"):
            await context.start()
        assert not (tmp_path / "app.db").exists() or not await _file_tables(tmp_path / "app.db")

    async def test_an_embedded_database_gets_its_tables_by_default(self, tmp_path: Path) -> None:
        context = ApplicationContext(_relational(url=_sqlite(tmp_path / "app.db")))
        await context.start()
        try:
            engine = context.get_bean(AsyncEngine)
            assert set(Base.metadata.tables) <= await _tables(engine)
        finally:
            await context.stop()


async def _file_tables(path: Path) -> set[str]:
    engine = create_async_engine(_sqlite(path))
    try:
        return await _tables(engine)
    finally:
        await engine.dispose()


# ---------------------------------------------------------------------------------------------------------
# validate (C115, C118)
# ---------------------------------------------------------------------------------------------------------


class TestValidate:
    async def test_a_missing_table_fails_the_start_naming_it(self, tmp_path: Path) -> None:
        from pyfly.data.relational.schema import SchemaInitializer, SchemaValidationError

        metadata, _table = _metadata()
        registry, engine = _registry_engine(_sqlite(tmp_path / "app.db"))
        try:
            schema = SchemaInitializer(engine, ddl_auto="validate", metadata=metadata)
            with pytest.raises(SchemaValidationError, match="table wp11_schema_account"):
                await schema.start()
            assert await _tables(engine) == set()  # validate never creates
        finally:
            await registry.close()

    async def test_a_missing_column_fails_the_start_naming_it(self, tmp_path: Path) -> None:
        from pyfly.data.relational.schema import SchemaInitializer, SchemaValidationError

        metadata, _table = _metadata()
        registry, engine = _registry_engine(_sqlite(tmp_path / "app.db"))
        try:
            async with engine.begin() as connection:
                await connection.execute(text("CREATE TABLE wp11_schema_account (id INTEGER PRIMARY KEY)"))
            schema = SchemaInitializer(engine, ddl_auto="validate", metadata=metadata)
            with pytest.raises(SchemaValidationError, match=r"column wp11_schema_account\.email"):
                await schema.start()
        finally:
            await registry.close()

    async def test_a_matching_schema_starts(self, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
        from pyfly.data.relational.schema import SchemaInitializer

        metadata, _table = _metadata()
        registry, engine = _registry_engine(_sqlite(tmp_path / "app.db"))
        try:
            async with engine.begin() as connection:
                await connection.run_sync(metadata.create_all)
            with caplog.at_level(logging.WARNING, logger="pyfly.data.relational.schema"):
                await SchemaInitializer(engine, ddl_auto="validate", metadata=metadata).start()
            assert "schema_validation_differences" not in caplog.text
        finally:
            await registry.close()

    async def test_a_different_column_type_is_logged_not_fatal(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        from pyfly.data.relational.schema import SchemaInitializer

        metadata, _table = _metadata()
        registry, engine = _registry_engine(_sqlite(tmp_path / "app.db"))
        try:
            async with engine.begin() as connection:
                await connection.execute(
                    text("CREATE TABLE wp11_schema_account (id INTEGER PRIMARY KEY, email INTEGER NOT NULL)")
                )
            with caplog.at_level(logging.WARNING, logger="pyfly.data.relational.schema"):
                await SchemaInitializer(engine, ddl_auto="validate", metadata=metadata).start()
            assert "schema_validation_differences" in caplog.text
        finally:
            await registry.close()

    async def test_a_context_validates_the_models_it_maps(self, tmp_path: Path) -> None:
        """Through the context: every model table exists but one lost a column, and the start fails."""
        url = _sqlite(tmp_path / "app.db")
        seed = create_async_engine(url)
        try:
            async with seed.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
                await connection.execute(text("DROP TABLE wp11_schema_note"))
                await connection.execute(text("CREATE TABLE wp11_schema_note (id INTEGER PRIMARY KEY)"))
        finally:
            await seed.dispose()
        context = ApplicationContext(_relational(url=url, **{"ddl-auto": "validate"}))
        with pytest.raises(BeanCreationException, match=r"column wp11_schema_note\.body"):
            await context.start()


class _SchemaNote(Base):
    """A model of Base, so the context-level tests see it in Base.metadata."""

    __tablename__ = "wp11_schema_note"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    body: Mapped[str] = mapped_column(String(50))


# ---------------------------------------------------------------------------------------------------------
# Order and phases (C118)
# ---------------------------------------------------------------------------------------------------------


class TestOrder:
    def test_migrations_run_first_then_the_strategy_then_every_other_bean(self) -> None:
        from pyfly.data.relational.auto_configuration import EngineLifecycle
        from pyfly.data.relational.migrations import MIGRATION_PHASE, MigrationRunner
        from pyfly.data.relational.schema import SCHEMA_PHASE, SchemaInitializer
        from pyfly.kernel.lifecycle import DEFAULT_PHASE, lifecycle_phase

        assert MIGRATION_PHASE < SCHEMA_PHASE < DEFAULT_PHASE
        assert lifecycle_phase(MigrationRunner()) == MIGRATION_PHASE
        assert EngineLifecycle.phase == SCHEMA_PHASE == SchemaInitializer.phase

    async def test_startup_migrations_no_longer_hide_a_missing_migration(self, tmp_path: Path) -> None:
        """C118 (a): the model has a table no migration creates. With migrations enabled the strategy is
        ``none`` by default, so create_all no longer supplies it; ``validate`` names it at startup."""
        url = _sqlite(tmp_path / "app.db")
        ini = _alembic_environment(tmp_path, url)
        context = ApplicationContext(
            _relational(url=url, migrations={"enabled": "true", "config": str(ini)}, **{"ddl-auto": "validate"})
        )
        with pytest.raises(BeanCreationException, match="table wp11_schema_note"):
            await context.start()
        # The migration ran before the validation.
        assert "wp11_migrated_only" in await _file_tables(tmp_path / "app.db")

        default = ApplicationContext(_relational(url=url, migrations={"enabled": "true", "config": str(ini)}))
        await default.start()
        try:
            assert "wp11_schema_note" not in await _tables(default.get_bean(AsyncEngine))
        finally:
            await default.stop()


def _alembic_environment(root: Path, url: str) -> Path:
    """An Alembic environment whose one revision creates ``wp11_migrated_only`` (the env.py of ``pyfly db
    init``)."""
    from pyfly.cli.db import _ENV_PY_TEMPLATE

    migrations = root / "migrations"
    (migrations / "versions").mkdir(parents=True)
    (migrations / "env.py").write_text(_ENV_PY_TEMPLATE)
    (migrations / "script.py.mako").write_text("")
    (migrations / "versions" / "r001.py").write_text(
        "import sqlalchemy as sa\nfrom alembic import op\n\nrevision = 'r001'\ndown_revision = None\n"
        "branch_labels = None\ndepends_on = None\n\n\ndef upgrade() -> None:\n"
        "    op.create_table('wp11_migrated_only', sa.Column('id', sa.Integer(), primary_key=True))\n"
    )
    ini = root / "alembic.ini"
    ini.write_text(f"[alembic]\nscript_location = {migrations}\nsqlalchemy.url = {url.replace('%', '%%')}\n")
    return ini


# ---------------------------------------------------------------------------------------------------------
# Instances starting together (C115)
# ---------------------------------------------------------------------------------------------------------


class TestReplicas:
    @pytest.mark.parametrize("lock", ["auto", "lease"])
    async def test_instances_that_start_together_all_start(self, tmp_path: Path, lock: str) -> None:
        from pyfly.data.relational.schema import SchemaInitializer

        url = _sqlite(tmp_path / "app.db")
        engines = [_registry_engine(url) for _ in range(4)]
        try:
            schemas = [
                SchemaInitializer(engine, ddl_auto="create", metadata=_metadata()[0], lock=lock)  # type: ignore[arg-type]
                for _registry, engine in engines
            ]
            await asyncio.gather(*(schema.start() for schema in schemas))
            assert "wp11_schema_account" in await _tables(engines[0][1])
        finally:
            for registry, _engine in engines:
                await registry.close()


# ---------------------------------------------------------------------------------------------------------
# create-drop teardown (C117)
# ---------------------------------------------------------------------------------------------------------


class TestCreateDrop:
    async def test_the_tables_are_dropped_when_the_context_stops(self, tmp_path: Path) -> None:
        url = _sqlite(tmp_path / "app.db")
        context = ApplicationContext(_relational(url=url, **{"ddl-auto": "create-drop"}))
        await context.start()
        assert "wp11_schema_note" in await _tables(context.get_bean(AsyncEngine))
        await context.stop()
        assert "wp11_schema_note" not in await _file_tables(tmp_path / "app.db")

    async def test_a_locked_database_bounds_the_drop_and_the_engine_is_still_disposed(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Another process holds the write lock: the drop waits at most the drop timeout, logs a WARNING instead
        of raising, and the standalone engine is disposed anyway."""
        from pyfly.data.relational.auto_configuration import EngineLifecycle
        from pyfly.data.relational.schema import SchemaInitializer

        path = tmp_path / "app.db"
        metadata, table = _metadata()
        registry, engine = _registry_engine(_sqlite(path))
        session = registry.primary.sessionmaker()
        schema = SchemaInitializer(engine, ddl_auto="create-drop", metadata=metadata, drop_timeout=1.0)
        lifecycle = EngineLifecycle(engine, session, schema=schema, dispose_engine=True)
        await lifecycle.start()
        other = create_async_engine(_sqlite(path))
        try:
            async with other.connect() as blocker:
                await blocker.exec_driver_sql("BEGIN IMMEDIATE")
                await blocker.execute(table.insert().values(id=1, email="held@example.com"))
                pool = engine.pool
                began = time.monotonic()
                with caplog.at_level(logging.WARNING, logger="pyfly.data.relational.schema"):
                    await lifecycle.stop()
                elapsed = time.monotonic() - began
                await blocker.rollback()
            assert elapsed < 1.0 + 5.0
            assert "schema_drop_failed" in caplog.text
            assert engine.pool is not pool  # disposed: a new, empty pool replaced the old one
            assert "wp11_schema_account" in await _tables(other)  # the schema is left as it is
        finally:
            await other.dispose()
            await registry.close()

    def test_an_invalid_strategy_fails_when_the_lifecycle_is_built(self) -> None:
        from pyfly.data.relational.auto_configuration import EngineLifecycle

        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        with pytest.raises(ValueError, match="ddl-auto must be one of"):
            EngineLifecycle(engine, object(), ddl_auto="bogus_value")  # type: ignore[arg-type]
