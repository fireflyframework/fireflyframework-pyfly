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
"""The outbox store contract (``tests/support/outbox_contract.py``) on the MongoDB store, on a real replica set.

Every store of a test is a :class:`~pyfly.eda.adapters.mongo_outbox.MongoOutboxStore` of its own on the test's
database (the nodes of a cluster), and a business unit is a ``TransactionTemplate`` unit of the client's
:class:`~pyfly.data.document.mongodb.transaction_manager.MongoTransactionManager`: a MongoDB transaction.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest
from pymongo import AsyncMongoClient

from pyfly.data.document.mongodb.transaction_manager import MongoTransactionManager
from pyfly.data.transaction import TransactionTemplate
from pyfly.eda.adapters.mongo_outbox import MongoOutboxStore
from pyfly.eda.ports.outbox import OutboxStore
from tests.support.backend_matrix import MongoBackend
from tests.support.outbox_contract import ManualClock, OutboxStoreContract, OutboxStoreHarness, started, stopping

pytestmark = [pytest.mark.integration, pytest.mark.docker, pytest.mark.mongo]


@pytest.fixture
async def outbox_harness(mongo_backend: MongoBackend) -> AsyncIterator[OutboxStoreHarness]:
    client: AsyncMongoClient[Any] = AsyncMongoClient(mongo_backend.url)
    manager = MongoTransactionManager.for_client(client)
    try:
        async with stopping([]) as stores:

            async def new_store(clock: ManualClock) -> OutboxStore:
                store = await started(MongoOutboxStore(manager, database=mongo_backend.database, clock=clock))
                stores.append(store)
                return store

            yield OutboxStoreHarness(
                new_store=new_store,
                unit=lambda: TransactionTemplate(manager).transaction(),
                name="mongo",
            )
    finally:
        await client.close()


class TestMongoOutboxStore(OutboxStoreContract):
    """The MongoDB store passes the outbox store contract on a replica set."""
