# Copyright 2026 Firefly Software Foundation
# SPDX-License-Identifier: Apache-2.0
"""Every native engine hides bound values in exception strings by default."""

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from pyfly.config.properties.data import RelationalProperties
from pyfly.core.config import Config
from pyfly.data.relational.datasource_registry import DataSourceRegistry


def test_hide_parameters_is_typed_inherited_and_explicitly_overridable(tmp_path):
    values = {
        "url": "sqlite+aiosqlite:///" + str(tmp_path / "primary.db"),
        "hide-parameters": "false",
        "datasources": {
            "audit": {"url": "sqlite+aiosqlite:///" + str(tmp_path / "audit.db")},
            "private": {"url": "sqlite+aiosqlite:///" + str(tmp_path / "private.db"), "hide-parameters": True},
        },
    }
    props = RelationalProperties.from_config(Config({"pyfly": {"data": {"relational": values}}}))
    assert props.hide_parameters is False
    assert props.primary().hide_parameters is False
    assert props.derived("sqlite+aiosqlite:///:memory:").hide_parameters is False
    assert props.datasources["audit"].hide_parameters is False
    assert props.datasources["private"].hide_parameters is True
    with pytest.raises(ValueError, match="hide-parameters"):
        RelationalProperties.from_config(Config({"pyfly": {"data": {"relational": {"hide-parameters": "unsafe"}}}}))


@pytest.mark.asyncio
async def test_primary_replica_named_and_derived_errors_withhold_bound_values(tmp_path, caplog):
    def url(name):
        return "sqlite+aiosqlite:///" + str(tmp_path / (name + ".db"))

    values = {
        "url": url("primary"),
        "read-replica": {"url": url("replica")},
        "datasources": {"audit": {"url": url("audit")}},
    }
    registry = DataSourceRegistry(Config({"pyfly": {"data": {"relational": values}}}))
    registry.register("module", url("module"))
    try:
        for datasource in registry.all_datasources():
            assert datasource.engine.sync_engine.hide_parameters is True
            assert datasource.engine.sync_engine.echo is False
            async with datasource.engine.begin() as connection:
                await connection.execute(text("CREATE TABLE private_values (value TEXT UNIQUE)"))
                await connection.execute(
                    text("INSERT INTO private_values (value) VALUES (:value)"),
                    {"value": "PRIVATE_BOUND_BEARER_PROMPT_SOURCE"},
                )
                with pytest.raises(IntegrityError) as caught:
                    await connection.execute(
                        text("INSERT INTO private_values (value) VALUES (:value)"),
                        {"value": "PRIVATE_BOUND_BEARER_PROMPT_SOURCE"},
                    )
                assert "PRIVATE_BOUND_BEARER_PROMPT_SOURCE" not in str(caught.value)
                assert "hidden" in str(caught.value)
        assert "PRIVATE_BOUND_BEARER_PROMPT_SOURCE" not in caplog.text
    finally:
        await registry.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("application_database", [True, False])
async def test_migration_engines_inherit_safe_parameter_diagnostics(tmp_path, application_database):
    from alembic.config import Config as AlembicConfig

    from pyfly.data.relational.migrations import PYFLY_CONFIG_ATTRIBUTE, migration_connection

    url = "sqlite+aiosqlite:///" + str(tmp_path / "migration.db")
    config = AlembicConfig()
    values = {"url": url} if application_database else {}
    config.attributes[PYFLY_CONFIG_ATTRIBUTE] = Config({"pyfly": {"data": {"relational": values}}})
    if not application_database:
        config.set_main_option("sqlalchemy.url", url)
    async with migration_connection(config) as connection:
        assert connection.engine.sync_engine.hide_parameters is True
        await connection.execute(text("CREATE TABLE migration_private (value TEXT UNIQUE)"))
        await connection.execute(
            text("INSERT INTO migration_private (value) VALUES (:value)"), {"value": "PRIVATE_MIGRATION_BOUND_SENTINEL"}
        )
        with pytest.raises(IntegrityError) as caught:
            await connection.execute(
                text("INSERT INTO migration_private (value) VALUES (:value)"),
                {"value": "PRIVATE_MIGRATION_BOUND_SENTINEL"},
            )
        assert "PRIVATE_MIGRATION_BOUND_SENTINEL" not in str(caught.value)
