# Copyright 2026 Firefly Software Foundation
# SPDX-License-Identifier: Apache-2.0
from pathlib import Path

import pytest

from pyfly.audit import AuditJournal
from pyfly.audit.auto_configuration import AuditAutoConfiguration
from pyfly.audit.sql import SqlAuditJournal
from pyfly.context import ApplicationContext
from pyfly.core import Config
from pyfly.data.transaction import infrastructure_unit
from tests.integration.test_audit_journal import SCOPE, event


async def test_opt_in_journal_is_native_di_lifecycle_and_joins_primary_transaction(tmp_path: Path) -> None:
    config = Config(
        {
            "pyfly": {
                "audit": {"enabled": True},
                "data": {
                    "relational": {
                        "enabled": False,
                        "url": f"sqlite+aiosqlite:///{tmp_path / 'app.db'}",
                        "ddl-auto": "create",
                    }
                },
            }
        }
    )
    context = ApplicationContext(config, auto_configurations=[AuditAutoConfiguration])
    await context.start()
    try:
        journal = context.get_bean(AuditJournal)
        assert isinstance(journal, SqlAuditJournal)
        async with infrastructure_unit(journal.engine):
            await journal.append(event())
        assert len(await journal.read(SCOPE)) == 1
    finally:
        await context.stop()
    with pytest.raises(RuntimeError, match="Start"):
        await journal.read(SCOPE)


async def test_ddl_none_refuses_missing_guards_and_does_not_silently_install_them(tmp_path: Path) -> None:
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    from pyfly.audit import AuditError
    from pyfly.data.relational.framework_schema import audit_entries, audit_heads

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'no-guards.db'}")
    try:
        async with engine.begin() as conn:
            await conn.run_sync(lambda sync: audit_entries.create(sync))
            await conn.run_sync(lambda sync: audit_heads.create(sync))
        journal = SqlAuditJournal(engine, create_tables=False)
        with pytest.raises(AuditError, match="guards"):
            await journal.start()
        async with engine.connect() as conn:
            assert not (await conn.execute(text("SELECT name FROM sqlite_master WHERE type='trigger'"))).all()
    finally:
        await engine.dispose()


async def test_journal_refuses_volatile_in_memory_sqlite() -> None:
    from sqlalchemy.ext.asyncio import create_async_engine

    from pyfly.audit import AuditError

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        with pytest.raises(AuditError, match="file-backed"):
            await SqlAuditJournal(engine).start()
    finally:
        await engine.dispose()
