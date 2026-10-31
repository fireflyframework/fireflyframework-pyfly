# Copyright 2026 Firefly Software Foundation
# SPDX-License-Identifier: Apache-2.0
"""Native backend units reject a different same-named manager before work."""

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from pyfly.data.relational.sqlalchemy.transaction_manager import SqlAlchemyTransactionManager
from pyfly.data.transaction import (
    IllegalTransactionStateError,
    Propagation,
    TransactionTemplate,
    UnexpectedRollbackError,
    infrastructure_unit,
)
from pyfly.data.transaction.template import AutoUnit
from tests.support.backend_matrix import RelationalBackend

pytestmark = pytest.mark.backends("sqlite-file", "pg")


@pytest.mark.parametrize("seam", ["infrastructure.read", "infrastructure.write", "auto.read", "auto.write", "boundary"])
async def test_caught_same_name_manager_refusal_prevents_partial_commit(
    relational_backend: RelationalBackend,
    seam: str,
) -> None:
    engine = relational_backend.create_engine()
    a, b = (SqlAlchemyTransactionManager(sessionmaker=async_sessionmaker(engine), name="primary") for _ in range(2))
    async with engine.begin() as conn:
        await conn.execute(text("CREATE TABLE identity_items (id VARCHAR(64) PRIMARY KEY)"))
    with pytest.raises(UnexpectedRollbackError):
        async with TransactionTemplate(a).transaction() as unit:
            await unit.resource.execute(text("INSERT INTO identity_items VALUES ('must-rollback')"))
            with pytest.raises(
                IllegalTransactionStateError, match="^A native Data unit belongs to a different transaction manager$"
            ):
                if seam.startswith("infrastructure"):
                    async with infrastructure_unit(b, read_only=seam.endswith("read")):
                        pytest.fail("Foreign infrastructure unit admitted")
                elif seam.startswith("auto"):
                    async with AutoUnit(b, read_only=seam.endswith("read")):
                        pytest.fail("Foreign auto unit admitted")
                else:
                    async with TransactionTemplate(b, propagation=Propagation.REQUIRED).transaction():
                        pytest.fail("Foreign boundary admitted")
    async with engine.connect() as conn:
        assert await conn.scalar(text("SELECT COUNT(*) FROM identity_items")) == 0
    async with TransactionTemplate(a).transaction() as unit:
        await unit.resource.execute(text("INSERT INTO identity_items VALUES ('own-manager')"))
    async with infrastructure_unit(b, read_only=True) as session:
        assert list(await session.scalars(text("SELECT id FROM identity_items"))) == ["own-manager"]
