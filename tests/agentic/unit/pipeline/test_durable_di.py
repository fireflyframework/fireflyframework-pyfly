# Copyright 2026 Firefly Software Foundation
# SPDX-License-Identifier: Apache-2.0
import pytest

from pyfly.agentic.auto_configuration import AgenticAutoConfiguration
from pyfly.agentic.pipeline.durable import DurableRunStore, DurableStateError
from pyfly.agentic.pipeline.durable.sql import SqlDurableRunStore
from pyfly.audit import AuditJournal
from pyfly.audit.auto_configuration import AuditAutoConfiguration
from pyfly.context import ApplicationContext
from pyfly.core import Config
from pyfly.data.relational.auto_configuration import DataSourceAutoConfiguration, RelationalAutoConfiguration
from pyfly.data.relational.sqlalchemy import SessionProvider, SqlAlchemyTransactionManager
from tests.agentic.unit.pipeline.test_durable_sql import AUTHOR, BINDING, INPUT, KEY, opened


async def test_native_di_registers_and_starts_same_primary_durable_store(tmp_path):
    config = Config(
        {
            "pyfly": {
                "agentic": {"enabled": True, "durable": {"enabled": True}},
                "audit": {"enabled": True},
                "data": {
                    "relational": {
                        "enabled": True,
                        "url": "sqlite+aiosqlite:///" + str(tmp_path / "di.db"),
                        "ddl-auto": "create",
                    }
                },
            }
        }
    )
    context = ApplicationContext(
        config,
        auto_configurations=[
            DataSourceAutoConfiguration,
            RelationalAutoConfiguration,
            AuditAutoConfiguration,
            AgenticAutoConfiguration,
        ],
    )
    await context.start()
    try:
        store = context.get_bean(DurableRunStore)
        assert isinstance(store, SqlDurableRunStore)
        assert store.transaction_manager is context.get_bean(SqlAlchemyTransactionManager)
        assert store.sessions is context.get_bean(SessionProvider)
        assert store.audit is context.get_bean(AuditJournal)
        value = await store.create(KEY, BINDING, INPUT, actor=AUTHOR)
        assert (await store.load(KEY, BINDING)) == value
    finally:
        await context.stop()


async def test_split_audit_datasource_is_refused_before_run_storage(tmp_path):
    async with (
        opened(tmp_path / "primary.db") as (first, sessions, _),
        opened(tmp_path / "different.db") as (_, _, other_audit),
    ):
        with pytest.raises(DurableStateError, match="same native Data"):
            SqlDurableRunStore(first.transaction_manager, sessions, other_audit)


async def test_migrations_are_required_when_ddl_creation_is_disabled(tmp_path):
    from sqlalchemy.ext.asyncio import create_async_engine

    from pyfly.audit.sql import SqlAuditJournal
    from pyfly.data.relational.framework_schema import FrameworkSchemaError
    from pyfly.data.relational.sqlalchemy.transaction_manager import SqlAlchemyTransactionManager

    engine = create_async_engine("sqlite+aiosqlite:///" + str(tmp_path / "missing.db"))
    manager = SqlAlchemyTransactionManager.for_engine(engine)
    journal = SqlAuditJournal(manager)
    try:
        store = SqlDurableRunStore(manager, SessionProvider(), journal, create_tables=False)
        with pytest.raises(FrameworkSchemaError):
            await store.start()
    finally:
        await engine.dispose()


@pytest.mark.parametrize("budget", [1048576.5, "1048576", True])
async def test_native_durable_budget_configuration_is_not_coerced(tmp_path, budget):
    config = Config(
        {
            "pyfly": {
                "agentic": {"enabled": True, "durable": {"enabled": True, "max-bytes": budget}},
                "audit": {"enabled": True},
                "data": {
                    "relational": {
                        "enabled": True,
                        "url": "sqlite+aiosqlite:///" + str(tmp_path / "invalid.db"),
                        "ddl-auto": "create",
                    }
                },
            }
        }
    )
    context = ApplicationContext(
        config,
        auto_configurations=[
            DataSourceAutoConfiguration,
            RelationalAutoConfiguration,
            AuditAutoConfiguration,
            AgenticAutoConfiguration,
        ],
    )
    try:
        with pytest.raises(Exception) as error:
            await context.start()
        assert "budget" in str(error.value).lower()
    finally:
        await context.stop()
