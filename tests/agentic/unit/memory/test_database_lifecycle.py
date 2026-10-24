"""Native Data memory borrows application-loop resources; SQLite exercises real lifecycle.

PostgreSQL and Mongo contracts run in tests/integration/test_agentic_native*_memory.py.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from pyfly.agentic.exceptions import DatabaseStoreError
from pyfly.agentic.memory.relational_store import RelationalMemoryStore
from pyfly.agentic.memory.types import MemoryEntry
from pyfly.core.config import Config
from pyfly.data.relational.datasource_registry import DataSourceRegistry
from pyfly.data.relational.framework_schema import FrameworkSchemaError, agentic_memory_table, ensure_tables
from pyfly.data.transaction import TransactionTemplate


@pytest.fixture
async def database(tmp_path):
    config = Config(
        {
            "pyfly": {
                "data": {
                    "relational": {
                        "enabled": True,
                        "url": f"sqlite+aiosqlite:///{tmp_path / 'memory.db'}",
                        "ddl-auto": "create",
                    }
                }
            }
        }
    )
    registry = DataSourceRegistry(config)
    store = RelationalMemoryStore(registry.primary, create_table=True)
    try:
        yield store, registry
    finally:
        await store.close()
        await registry.close()


async def test_initialized_store_mixes_async_and_external_thread_calls(database):
    store, _registry = database
    await store.initialize()
    entry = MemoryEntry(key="answer", content=42, metadata={"source": "test"})
    await asyncio.to_thread(store.save, "tenant", entry)
    assert (await store.async_load_by_key("tenant", "answer")).content == 42
    assert (await asyncio.to_thread(store.load, "tenant"))[0].entry_id == entry.entry_id
    await asyncio.to_thread(store.delete, "tenant", entry.entry_id)
    assert await store.async_load_by_key("tenant", "answer") is None
    await store.async_save("tenant", entry)
    await asyncio.to_thread(store.clear, "tenant")
    assert await store.async_load("tenant") == []


async def test_store_rejects_different_loop_instead_of_moving_native_sessions(database):
    store, _registry = database
    await store.initialize()
    with pytest.raises(DatabaseStoreError, match="loop"):
        await asyncio.to_thread(asyncio.run, store.async_load("tenant"))
    with pytest.raises(DatabaseStoreError, match="async"):
        store.save("tenant", MemoryEntry())
    assert await store.async_load("tenant") == []


async def test_json_and_expiry_are_encoded_by_the_data_adapter(database):
    store, _registry = database
    live = MemoryEntry(
        key="live",
        content={"nested": [True, None, 2]},
        metadata={"when": datetime(2026, 1, 1, tzinfo=UTC)},
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    await store.async_save("tenant", live)
    await store.async_save("tenant", MemoryEntry(key="expired", expires_at=datetime.now(UTC) - timedelta(seconds=1)))
    loaded = await store.async_load("tenant")
    assert [entry.key for entry in loaded] == ["live"]
    assert loaded[0].model_dump(mode="json") == live.model_dump(mode="json")
    assert await store.cleanup_expired() == 1


async def test_concurrent_initialization_and_shutdown_keep_shared_engine_usable(database):
    store, registry = database
    await asyncio.gather(store.initialize(), store.initialize(), store.initialize())
    await store.async_save("tenant", MemoryEntry(entry_id="retained"))
    await asyncio.to_thread(store.close_sync)
    await store.close()
    async with registry.engine().connect() as connection:
        assert (await connection.execute(select(agentic_memory_table().c.entry_id))).scalar_one() == "retained"
    await store.initialize()
    assert (await store.async_load("tenant"))[0].entry_id == "retained"


async def test_failed_schema_validation_can_retry_without_disposing_datasource(database):
    _original, registry = database
    store = RelationalMemoryStore(registry.primary, table="memory_migrated")
    try:
        with pytest.raises(FrameworkSchemaError, match="does not exist"):
            await store.initialize()
        await ensure_tables(registry.primary, agentic_memory_table("memory_migrated"), create=True)
        await store.initialize()
        await store.async_save("tenant", MemoryEntry(key="recovered", content=True))
        assert (await store.async_load_by_key("tenant", "recovered")).content is True
    finally:
        await store.close()


async def test_thread_bridge_joins_and_rolls_back_the_callers_unit(database):
    store, registry = database
    await store.initialize()
    with pytest.raises(RuntimeError, match="abort"):
        async with TransactionTemplate(registry.primary).transaction():
            await asyncio.to_thread(store.save, "tenant", MemoryEntry(entry_id="rollback"))
            assert (await store.async_load("tenant"))[0].entry_id == "rollback"
            raise RuntimeError("abort")
    assert await store.async_load("tenant") == []


def test_sync_access_requires_a_running_initialized_application_loop():
    store = RelationalMemoryStore("configured-datasource")
    with pytest.raises(DatabaseStoreError, match="initialize"):
        store.initialize_sync()
    with pytest.raises(DatabaseStoreError, match="initialize"):
        store.load("tenant")
