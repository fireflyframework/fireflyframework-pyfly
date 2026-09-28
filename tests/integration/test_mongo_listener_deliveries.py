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
"""Message deliveries in units of work when the application's default datasource is MongoDB.

A listener container opens a unit per delivery on the default transaction manager (WP12). In an application
whose only datasource is MongoDB that manager is the MongoDB one: on a replica set a delivery's repository
writes commit with it, or roll back with a failure; on a standalone server (no transactions) the container
refuses the unit with a message that names ``listener.transactional``, and delivers without one when it is off.
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator

import pytest

from pyfly.data.document.mongodb.document import BaseDocument
from pyfly.data.document.mongodb.repository import MongoRepository
from pyfly.data.document.mongodb.transaction_manager import MongoTransactionManager
from pyfly.data.transaction import (
    IllegalTransactionStateError,
    TransactionManagerRegistry,
    install_registry,
    is_transaction_active,
    uninstall_registry,
)
from pyfly.messaging.listener_container import DeliveryState, ListenerContainerSettings, ListenerInvoker
from tests.support.mongo import BeanieDatabase, beanie_database


class Delivered(BaseDocument):
    payload: str

    class Settings:
        name = "delivered"


@pytest.fixture
async def replica_set(mongo_rs_url: str) -> AsyncIterator[BeanieDatabase]:
    async with _installed(mongo_rs_url) as db:
        yield db


@pytest.fixture
async def standalone(mongo_url: str) -> AsyncIterator[BeanieDatabase]:
    async with _installed(mongo_url) as db:
        yield db


@contextlib.asynccontextmanager
async def _installed(url: str) -> AsyncIterator[BeanieDatabase]:
    """The documents on a database of their own, and a registry whose default datasource is MongoDB."""
    async with beanie_database(url, [Delivered]) as db:
        manager = MongoTransactionManager(db.client)
        registry = TransactionManagerRegistry(default=manager.datasource)
        registry.register(manager)
        install_registry(registry)
        try:
            yield db
        finally:
            uninstall_registry(registry)


async def _payloads(db: BeanieDatabase) -> list[str]:
    return sorted(row["payload"] for row in await db.database["delivered"].find({}).to_list())


async def test_a_delivery_commits_or_rolls_back_with_its_mongo_unit(replica_set: BeanieDatabase) -> None:
    invoker = ListenerInvoker(ListenerContainerSettings(), name="orders")
    repository: MongoRepository[Delivered, str] = MongoRepository(Delivered)
    seen: list[bool] = []

    async def handle(payload: str, fail: bool) -> None:
        seen.append(is_transaction_active("document"))
        await repository.save(Delivered(payload=payload))
        await repository.save(Delivered(payload=f"{payload}-audit"))
        if fail:
            raise ValueError("the listener failed")

    committed = DeliveryState()
    await invoker.invoke(lambda: handle("ok", False), committed)
    failed = DeliveryState()
    with pytest.raises(ValueError):
        await invoker.invoke(lambda: handle("bad", True), failed)
    assert seen == [True, True]
    assert committed.committed is True and failed.committed is False
    assert await _payloads(replica_set) == ["ok", "ok-audit"]


async def test_a_standalone_server_needs_listener_transactional_off(standalone: BeanieDatabase) -> None:
    repository: MongoRepository[Delivered, str] = MongoRepository(Delivered)

    async def handle() -> None:
        await repository.save(Delivered(payload="plain"))

    transactional = ListenerInvoker(ListenerContainerSettings(), name="orders")
    with pytest.raises(IllegalTransactionStateError, match="listener.transactional"):
        await transactional.invoke(handle, DeliveryState())
    plain = ListenerInvoker(ListenerContainerSettings(transactional=False), name="orders")
    state = DeliveryState()
    await plain.invoke(handle, state)
    assert state.committed is True
    assert await _payloads(standalone) == ["plain"]
