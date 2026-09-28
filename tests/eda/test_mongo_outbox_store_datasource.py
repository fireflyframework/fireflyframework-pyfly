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
"""Which document datasource a :class:`~pyfly.eda.adapters.mongo_outbox.MongoOutboxStore` runs on.

The auto-configuration builds the store before the context registers the document datasource's transaction
manager, so a store given a datasource name resolves it at each use, as the SQL store resolves its own. No server
is contacted here: resolving a manager and naming a database send nothing.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from pymongo import AsyncMongoClient
from sqlalchemy.ext.asyncio import create_async_engine

from pyfly.data.document.mongodb.transaction_manager import MongoTransactionManager
from pyfly.data.relational.sqlalchemy.transaction_manager import SqlAlchemyTransactionManager
from pyfly.data.transaction import (
    IllegalTransactionStateError,
    TransactionManagerRegistry,
    install_registry,
    uninstall_registry,
)
from pyfly.eda.adapters.mongo_outbox import MongoOutboxStore


@pytest.fixture
def registry() -> Iterator[TransactionManagerRegistry]:
    registry = TransactionManagerRegistry()
    install_registry(registry)
    try:
        yield registry
    finally:
        uninstall_registry(registry)


def _client(uri: str = "mongodb://localhost:1") -> AsyncMongoClient[Any]:
    return AsyncMongoClient(uri, connect=False)


def test_a_datasource_name_is_resolved_when_the_store_runs(registry: TransactionManagerRegistry) -> None:
    store = MongoOutboxStore("catalog", database="shop")
    with pytest.raises(IllegalTransactionStateError, match="catalog"):
        store.manager()  # nothing registered under it yet: the context registers it when it starts

    manager = MongoTransactionManager(_client(), datasource="catalog")
    registry.register(manager)
    assert store.manager() is manager
    assert store.client is manager.client
    assert (store.datasource, store.database) == ("catalog", "shop")


def test_a_relational_datasource_is_refused(registry: TransactionManagerRegistry) -> None:
    registry.register(SqlAlchemyTransactionManager.for_engine(create_async_engine("sqlite+aiosqlite://")))
    with pytest.raises(IllegalTransactionStateError, match="runs on a document datasource"):
        MongoOutboxStore(registry.default_name).manager()


def test_a_client_or_a_manager_is_its_own_datasource() -> None:
    client = _client("mongodb://localhost:1/orders")
    assert MongoOutboxStore(client).manager() is MongoTransactionManager.for_client(client)
    assert MongoOutboxStore(client).database == "orders"  # the URI's database
    manager = MongoTransactionManager(_client())
    assert MongoOutboxStore(manager).manager() is manager
    assert MongoOutboxStore(manager).database == "pyfly"  # a URI that names none
    with pytest.raises(TypeError, match="runs on a document datasource"):
        MongoOutboxStore(object())  # type: ignore[arg-type]
