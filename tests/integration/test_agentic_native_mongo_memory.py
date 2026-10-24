"""Native Agentic Mongo memory shares the framework document session and client."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from pymongo import AsyncMongoClient

from pyfly.agentic.exceptions import DatabaseStoreError
from pyfly.agentic.memory.database_store import MongoDBStore
from pyfly.agentic.memory.types import MemoryEntry
from pyfly.data.document.mongodb.transaction_manager import MongoTransactionManager
from pyfly.data.transaction import IllegalTransactionStateError, TransactionTemplate, current_unit_of_work
from tests.support.backend_matrix import MongoBackend, new_database_name
from tests.support.mongo import CommandLog

pytestmark = [pytest.mark.integration, pytest.mark.docker, pytest.mark.mongo]


@pytest.fixture
async def native_memory(mongo_backend: MongoBackend):
    client: AsyncMongoClient[Any] = AsyncMongoClient(mongo_backend.url, tz_aware=True)
    manager = MongoTransactionManager(client, datasource="agentic-document")
    store = MongoDBStore(client, manager=manager, database=mongo_backend.database)
    await store.initialize()
    try:
        yield store, client, manager
    finally:
        await store.close()
        await client.close()


async def test_native_mongo_namespace_payload_expiration_and_latest_key(native_memory) -> None:
    store, _client, _manager = native_memory
    now = datetime.now(UTC).replace(microsecond=0)
    original = MemoryEntry(
        entry_id="same", key="k", content={"list": [None, True, 3]}, metadata={"nested": {"a": 1}}, created_at=now
    )
    await store.async_save("one", original)
    await store.async_save("two", original.model_copy(update={"content": "other"}))
    assert (await store.async_load("one"))[0].model_dump() == original.model_dump()
    await store.async_save(
        "one", MemoryEntry(entry_id="new", key="k", content="new", created_at=now + timedelta(seconds=1))
    )
    await store.async_save("one", MemoryEntry(entry_id="expired", key="k", expires_at=now - timedelta(seconds=1)))
    assert (await store.async_load_by_key("one", "k")).content == "new"
    assert await store.cleanup_expired() == 1
    await store.async_delete("one", "same")
    await store.async_clear("one")
    assert await store.async_load("one") == []
    assert (await store.async_load("two"))[0].content == "other"


async def test_native_mongo_business_and_memory_rollback_and_replace_are_atomic(native_memory) -> None:
    store, client, manager = native_memory
    business = client[store.database]["business"]
    await business.insert_one({"_id": "seed"})
    await store.async_save("one", MemoryEntry(entry_id="old", key="k", content="old"))
    with pytest.raises(RuntimeError, match="abort"):
        async with TransactionTemplate(manager).transaction() as unit:
            async with unit.operation():
                await business.insert_one({"_id": "rollback"}, session=unit.resource)
            await store.async_replace("one", MemoryEntry(entry_id="new", key="k", content="new"))
            assert (await store.async_load_by_key("one", "k")).content == "new"
            raise RuntimeError("abort")
    assert await business.find_one({"_id": "rollback"}) is None
    assert (await store.async_load_by_key("one", "k")).content == "old"
    async with TransactionTemplate(manager).transaction() as unit:
        async with unit.operation():
            await business.insert_one({"_id": "commit"}, session=unit.resource)
        await store.async_replace("one", MemoryEntry(entry_id="new", key="k", content="new"))
    assert await business.find_one({"_id": "commit"}) is not None
    assert [entry.content for entry in await store.async_load("one")] == ["new"]


async def test_native_mongo_read_only_owner_loop_and_client_ownership(native_memory) -> None:
    store, client, manager = native_memory
    with pytest.raises(IllegalTransactionStateError):
        async with TransactionTemplate(manager, read_only=True).transaction():
            await store.async_save("one", MemoryEntry())
    with pytest.raises(DatabaseStoreError, match="async"):
        store.load("one")
    with pytest.raises(RuntimeError, match="abort"):
        async with TransactionTemplate(manager).transaction():
            await asyncio.to_thread(store.save, "one", MemoryEntry(entry_id="thread"))
            assert (await asyncio.to_thread(store.load, "one"))[0].entry_id == "thread"
            raise RuntimeError("abort")
    assert await store.async_load("one") == []
    with pytest.raises(DatabaseStoreError, match="loop"):
        await asyncio.to_thread(asyncio.run, store.async_load("one"))
    await store.close()
    await store.close()
    assert (await client.admin.command("ping"))["ok"] == 1
    await store.initialize()
    assert await store.async_load("one") == []


async def test_native_mongo_cancellation_rolls_back_memory(native_memory) -> None:
    store, _client, manager = native_memory
    saved = asyncio.Event()

    async def write() -> None:
        async with TransactionTemplate(manager).transaction():
            await store.async_save("one", MemoryEntry(entry_id="cancelled"))
            saved.set()
            await asyncio.Future()

    task = asyncio.create_task(write())
    await asyncio.wait_for(saved.wait(), timeout=10)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert current_unit_of_work() is None
    assert await store.async_load("one") == []


async def test_legacy_mongo_unique_id_index_requires_explicit_migration(mongo_backend: MongoBackend) -> None:
    client: AsyncMongoClient[Any] = AsyncMongoClient(mongo_backend.url)
    try:
        collection = client[mongo_backend.database]["entries"]
        await collection.create_index("entry_id", unique=True)
        await collection.insert_one({"entry_id": "retained", "namespace": "one"})
        store = MongoDBStore(client, database=mongo_backend.database)
        with pytest.raises(DatabaseStoreError, match="migration"):
            await store.initialize()
        assert await collection.find_one({"entry_id": "retained"}) is not None
        indexes = await collection.index_information()
        assert indexes["entry_id_1"]["unique"] is True
    finally:
        await client.close()


@pytest.fixture(params=["different-client", "same-client"])
async def independent_document_owners(mongo_backend: MongoBackend, request):
    first_log, second_log = CommandLog(), CommandLog()
    first_client: AsyncMongoClient[Any] = AsyncMongoClient(
        mongo_backend.url, tz_aware=True, event_listeners=[first_log]
    )
    if request.param == "same-client":
        second_client, second_log = first_client, first_log
    else:
        second_client = AsyncMongoClient(mongo_backend.url, tz_aware=True, event_listeners=[second_log])
    first_manager = MongoTransactionManager(first_client, datasource="same-document")
    second_manager = MongoTransactionManager(second_client, datasource="same-document")
    second_database = new_database_name()
    first = MongoDBStore(first_client, manager=first_manager, database=mongo_backend.database)
    second = MongoDBStore(second_client, manager=second_manager, database=second_database)

    async def snapshot(client, database):
        return await client[database]["entries"].find({}).sort("entry_id", 1).to_list()

    try:
        await first.initialize()
        await second.initialize()
        for store, label in ((first, "first-owner"), (second, "second-owner")):
            await store.async_save("shared", MemoryEntry(entry_id="retained", key="value", content=label))
            await store.async_save(
                "shared",
                MemoryEntry(entry_id="expired", content=label, expires_at=datetime.now(UTC) - timedelta(days=1)),
            )
        yield first, second, first_manager, first_client, second_client, first_log, second_log, snapshot
    finally:
        await first.close()
        await second.close()
        await second_client.drop_database(second_database)
        if second_client is not first_client:
            await second_client.close()
        await first_client.close()


@pytest.mark.parametrize(
    "operation",
    [
        "async_save",
        "async_load",
        "async_load_by_key",
        "async_delete",
        "async_clear",
        "cleanup_expired",
        "async_replace",
    ],
)
async def test_same_named_document_unit_cannot_access_another_memory_owner(independent_document_owners, operation):
    first, second, manager, first_client, second_client, first_log, second_log, snapshot = independent_document_owners
    before_first = await snapshot(first_client, first.database)
    before_second = await snapshot(second_client, second.database)
    arguments = {
        "async_save": ("shared", MemoryEntry(entry_id="new", key="value", content="must-not-write")),
        "async_load": ("shared",),
        "async_load_by_key": ("shared", "value"),
        "async_delete": ("shared", "retained"),
        "async_clear": ("shared",),
        "cleanup_expired": (),
        "async_replace": ("shared", MemoryEntry(entry_id="new", key="value", content="must-not-replace")),
    }
    first_log.clear()
    second_log.clear()
    async with TransactionTemplate(manager).transaction():
        with pytest.raises(IllegalTransactionStateError, match="another"):
            await getattr(second, operation)(*arguments[operation])
    for log in (first_log, second_log):
        assert [name for name in log.names() if not name.endswith("Transaction")] == []
    assert await snapshot(first_client, first.database) == before_first
    assert await snapshot(second_client, second.database) == before_second
