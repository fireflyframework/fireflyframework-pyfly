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
"""The outbox store contract (``tests/support/outbox_contract.py``) on the SQL store, on every relational lane:
SQLite file (foreign keys on), PostgreSQL, MySQL 8 and MariaDB 11.

Every store of a test is a :class:`~pyfly.eda.outbox.SqlOutboxStore` of its own on the test's database (the
nodes of a cluster), and a business unit is a ``TransactionTemplate`` unit on the same engine.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from pyfly.data.relational.sqlalchemy.transaction_manager import SqlAlchemyTransactionManager
from pyfly.data.transaction import TransactionTemplate
from pyfly.eda.outbox import SqlOutboxStore
from pyfly.eda.ports.outbox import OutboxStore
from tests.support.backend_matrix import RelationalBackend
from tests.support.outbox_contract import ManualClock, OutboxStoreContract, OutboxStoreHarness, started, stopping


@pytest.fixture
async def outbox_harness(relational_backend: RelationalBackend) -> AsyncIterator[OutboxStoreHarness]:
    engine = relational_backend.create_engine()
    manager = SqlAlchemyTransactionManager.for_engine(engine)

    async with stopping([]) as stores:

        async def new_store(clock: ManualClock) -> OutboxStore:
            store = await started(SqlOutboxStore(engine, clock=clock))
            stores.append(store)
            return store

        yield OutboxStoreHarness(
            new_store=new_store,
            unit=lambda: TransactionTemplate(manager).transaction(),
            name=relational_backend.lane,
        )


class TestSqlOutboxStore(OutboxStoreContract):
    """The SQL store passes the outbox store contract on every relational lane."""
