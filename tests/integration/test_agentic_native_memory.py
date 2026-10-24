"""Agentic memory uses the same datasource and unit of work as business writes."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import Column, Integer, MetaData, Table, insert, select

from pyfly.agentic.exceptions import DatabaseStoreError
from pyfly.agentic.memory.types import MemoryEntry
from pyfly.data.transaction import IllegalTransactionStateError, TransactionTemplate, current_unit_of_work
from tests.integration._repository_harness import dml, repository_datasources
from tests.support.backend_matrix import RelationalBackend, create_database, drop_database, new_database_name

pytestmark = pytest.mark.backends("sqlite-file", "pg")


@contextlib.asynccontextmanager
async def memory_database(backend: RelationalBackend, **settings: Any) -> AsyncIterator[tuple[Any, Any, Table]]:
    from pyfly.agentic.memory.relational_store import RelationalMemoryStore
    from pyfly.data.relational.framework_schema import agentic_memory_table

    business = Table("agentic_business", MetaData(), Column("id", Integer, primary_key=True))
    table = agentic_memory_table()
    async with repository_datasources(backend, table, business) as sources:
        store = RelationalMemoryStore(sources.registry.primary, **settings)
        await store.initialize()
        try:
            yield store, sources, business
        finally:
            await store.close()


async def test_memory_identity_is_scoped_and_payload_is_preserved(relational_backend: RelationalBackend) -> None:
    async with memory_database(relational_backend) as (store, _sources, _business):
        entry = MemoryEntry(
            entry_id="shared", key="answer", content={"nested": [1, True, None]}, metadata={"tags": ["a"]}
        )
        await store.async_save("tenant-a", entry)
        await store.async_save("tenant-b", entry.model_copy(update={"content": "other"}))
        assert (await store.async_load("tenant-a"))[0].model_dump() == entry.model_dump()
        assert (await store.async_load_by_key("tenant-b", "answer")).content == "other"
        await store.async_delete("tenant-a", "shared")
        assert await store.async_load("tenant-a") == []
        assert (await store.async_load("tenant-b"))[0].content == "other"
        await store.async_clear("tenant-a")
        assert len(await store.async_load("tenant-b")) == 1


async def test_existing_postgres_entry_payload_encoding_remains_readable(relational_backend: RelationalBackend) -> None:
    from pyfly.data.relational.framework_schema import agentic_memory_table

    now = datetime(2026, 10, 23, tzinfo=UTC)
    persisted = {
        "entry_id": "imported",
        "scope": "working",
        "key": "original",
        "content": {"persisted": True},
        "metadata": {"source": "old-store"},
        "created_at": "2026-10-23T00:00:00Z",
        "expires_at": None,
        "importance": 0.75,
    }
    async with memory_database(relational_backend) as (store, sources, _business):
        async with TransactionTemplate(sources.registry.primary).transaction() as unit:
            await unit.resource.execute(
                insert(agentic_memory_table()).values(
                    entry_id="imported",
                    namespace="one",
                    scope="working",
                    key="original",
                    content=persisted,
                    metadata={"source": "old-store"},
                    created_at=now,
                    expires_at=None,
                    importance=0.75,
                )
            )
        imported = await store.async_load_by_key("one", "original")
        assert imported.content == {"persisted": True}
        assert imported.metadata == {"source": "old-store"}
        assert imported.importance == 0.75


async def test_expiry_latest_key_and_cleanup_use_the_same_utc_boundary(relational_backend: RelationalBackend) -> None:
    now = datetime(2026, 10, 23, 12, tzinfo=UTC)
    async with memory_database(relational_backend, clock=lambda: now) as (store, _sources, _business):
        for identifier, offset in (("old", -2), ("new", -1)):
            await store.async_save(
                "one", MemoryEntry(entry_id=identifier, key="k", created_at=now + timedelta(seconds=offset))
            )
        await store.async_save("one", MemoryEntry(entry_id="expired", key="k", expires_at=now))
        await store.async_save("two", MemoryEntry(entry_id="expired", expires_at=now - timedelta(seconds=1)))
        assert [entry.entry_id for entry in await store.async_load("one")] == ["old", "new"]
        assert (await store.async_load_by_key("one", "k")).entry_id == "new"
        assert await store.cleanup_expired() == 2
        assert await store.cleanup_expired() == 0


async def test_memory_and_business_rows_commit_or_rollback_together(relational_backend: RelationalBackend) -> None:
    async with memory_database(relational_backend) as (store, sources, business):
        template = TransactionTemplate(sources.registry.primary)
        with pytest.raises(RuntimeError, match="abort"):
            async with template.transaction() as unit:
                await unit.resource.execute(insert(business).values(id=1))
                await store.async_save("one", MemoryEntry(entry_id="rolled-back"))
                assert len(await store.async_load("one")) == 1
                raise RuntimeError("abort")
        assert await store.async_load("one") == []
        async with template.transaction() as unit:
            assert (await unit.resource.execute(select(business))).all() == []
            await unit.resource.execute(insert(business).values(id=2))
            await store.async_save("one", MemoryEntry(entry_id="committed"))
        assert (await store.async_load("one"))[0].entry_id == "committed"
        assert sources.checked_out() == 0


async def test_replace_joins_transaction_and_leaves_other_namespaces_alone(
    relational_backend: RelationalBackend,
) -> None:
    async with memory_database(relational_backend) as (store, sources, _business):
        await store.async_save("one", MemoryEntry(entry_id="old", key="k", content="old"))
        await store.async_save("two", MemoryEntry(entry_id="old", key="k", content="other"))
        with pytest.raises(RuntimeError, match="abort"):
            async with TransactionTemplate(sources.registry.primary).transaction():
                await store.async_replace("one", MemoryEntry(entry_id="new", key="k", content="new"))
                assert [entry.content for entry in await store.async_load("one")] == ["new"]
                raise RuntimeError("abort")
        assert [entry.content for entry in await store.async_load("one")] == ["old"]
        await store.async_replace("one", MemoryEntry(entry_id="new", key="k", content="new"))
        assert [entry.content for entry in await store.async_load("one")] == ["new"]
        assert [entry.content for entry in await store.async_load("two")] == ["other"]


async def test_read_only_units_reject_memory_writes(relational_backend: RelationalBackend) -> None:
    async with memory_database(relational_backend) as (store, sources, _business):
        for operation in (store.async_save, store.async_replace):
            with pytest.raises(IllegalTransactionStateError):
                async with TransactionTemplate(sources.registry.primary, read_only=True).transaction():
                    await operation("one", MemoryEntry(key="k"))
        assert await store.async_load("one") == []


async def test_sibling_replacements_do_not_interleave_on_a_shared_unit(relational_backend: RelationalBackend) -> None:
    async with (
        memory_database(relational_backend) as (store, sources, _business),
        TransactionTemplate(sources.registry.primary).transaction(),
    ):
        await asyncio.gather(
            store.async_replace("one", MemoryEntry(entry_id="first", key="k", content="first")),
            store.async_replace("one", MemoryEntry(entry_id="second", key="k", content="second")),
        )
        entries = await store.async_load("one")
        assert len(entries) == 1
        assert entries[0].content in {"first", "second"}


async def test_sync_thread_bridge_preserves_transaction_and_never_blocks_owner_loop(
    relational_backend: RelationalBackend,
) -> None:
    async with memory_database(relational_backend) as (store, sources, _business):
        with pytest.raises(DatabaseStoreError, match="async"):
            store.load("one")
        with pytest.raises(RuntimeError, match="abort"):
            async with TransactionTemplate(sources.registry.primary).transaction():
                await asyncio.to_thread(store.save, "one", MemoryEntry(entry_id="thread"))
                assert (await asyncio.to_thread(store.load, "one"))[0].entry_id == "thread"
                raise RuntimeError("abort")
        assert await store.async_load("one") == []
        with pytest.raises(DatabaseStoreError, match="loop"):
            await asyncio.to_thread(asyncio.run, store.async_load("one"))


async def test_cancelled_memory_unit_rolls_back_and_releases_connection(relational_backend: RelationalBackend) -> None:
    async with memory_database(relational_backend) as (store, sources, _business):
        saved = asyncio.Event()

        async def write() -> None:
            async with TransactionTemplate(sources.registry.primary).transaction():
                await store.async_save("one", MemoryEntry(entry_id="cancelled"))
                saved.set()
                await asyncio.Future()

        task = asyncio.create_task(write())
        await asyncio.wait_for(saved.wait(), timeout=5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert current_unit_of_work() is None
        assert await store.async_load("one") == []
        assert sources.checked_out() == 0


async def test_close_does_not_dispose_shared_datasource_and_store_can_restart(
    relational_backend: RelationalBackend,
) -> None:
    async with memory_database(relational_backend) as (store, sources, business):
        await store.async_save("one", MemoryEntry(entry_id="retained"))
        await store.close()
        await store.close()
        async with sources.engine.connect() as connection:
            assert (await connection.execute(select(business))).all() == []
        await store.initialize()
        assert (await store.async_load("one"))[0].entry_id == "retained"


async def test_legacy_global_id_schema_requires_migration_without_rewriting_rows(
    relational_backend: RelationalBackend,
) -> None:
    from pyfly.agentic.memory.relational_store import RelationalMemoryStore
    from pyfly.data.relational.framework_schema import FrameworkSchemaError

    metadata = MetaData()
    from sqlalchemy import JSON, DateTime, Float, String

    legacy = Table(
        "legacy_agentic_memory",
        metadata,
        Column("entry_id", String(255), primary_key=True),
        Column("namespace", String(255)),
        Column("scope", String(32)),
        Column("key", String(255)),
        Column("content", JSON),
        Column("metadata", JSON),
        Column("created_at", DateTime(timezone=True)),
        Column("expires_at", DateTime(timezone=True)),
        Column("importance", Float),
    )
    async with repository_datasources(relational_backend, legacy) as sources:
        store = RelationalMemoryStore(sources.registry.primary, table=legacy.name, create_table=True)
        with pytest.raises(FrameworkSchemaError, match="migration"):
            await store.initialize()


def test_legacy_urls_require_framework_data_configuration() -> None:
    from pyfly.agentic.memory.database_store import MongoDBStore, PostgreSQLStore

    for constructor, url in (
        (PostgreSQLStore, "postgresql://fictional.invalid/db"),
        (MongoDBStore, "mongodb://fictional.invalid"),
    ):
        with pytest.raises(ValueError, match="pyfly.data"):
            constructor(url)


@pytest.fixture
async def independent_memory_owners(relational_backend: RelationalBackend, tmp_path, request):
    from pyfly.agentic.memory.relational_store import RelationalMemoryStore
    from pyfly.data.relational.datasource_registry import DataSourceRegistry
    from pyfly.data.relational.framework_schema import agentic_memory_table

    name = new_database_name()
    server_url = None
    if relational_backend.is_embedded:
        second_backend = RelationalBackend("sqlite-file", f"sqlite+aiosqlite:///{tmp_path / 'second.db'}")
    else:
        server_url = request.getfixturevalue("pg_server_url")
        second_backend = RelationalBackend("pg", await create_database(server_url, name))
    first_registry = DataSourceRegistry(relational_backend.config())
    second_registry = DataSourceRegistry(second_backend.config())
    first = RelationalMemoryStore(first_registry.primary, create_table=True)
    second = RelationalMemoryStore(second_registry.primary, create_table=True)

    async def snapshot(registry):
        async with registry.engine().connect() as connection:
            table = agentic_memory_table()
            return (await connection.execute(select(table).order_by(table.c.entry_id))).all()

    try:
        await first.initialize()
        await second.initialize()
        for store, label in ((first, "first-owner"), (second, "second-owner")):
            await store.async_save("shared", MemoryEntry(entry_id="retained", key="value", content=label))
            await store.async_save(
                "shared",
                MemoryEntry(entry_id="expired", content=label, expires_at=datetime.now(UTC) - timedelta(days=1)),
            )
        yield first, second, first_registry, second_registry, snapshot
    finally:
        await first.close()
        await second.close()
        await first_registry.close()
        await second_registry.close()
        await second_backend.dispose()
        if server_url is not None:
            await drop_database(server_url, name)


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
async def test_same_named_foreign_memory_unit_is_rejected_before_any_io(independent_memory_owners, operation):
    from pyfly.testing import StatementCounter

    _first, second, first_registry, second_registry, snapshot = independent_memory_owners
    before_first, before_second = await snapshot(first_registry), await snapshot(second_registry)
    arguments = {
        "async_save": ("shared", MemoryEntry(entry_id="new", key="value", content="must-not-write")),
        "async_load": ("shared",),
        "async_load_by_key": ("shared", "value"),
        "async_delete": ("shared", "retained"),
        "async_clear": ("shared",),
        "cleanup_expired": (),
        "async_replace": ("shared", MemoryEntry(entry_id="new", key="value", content="must-not-replace")),
    }
    with (
        StatementCounter(first_registry.engine()) as first_counter,
        StatementCounter(second_registry.engine()) as second_counter,
    ):
        async with TransactionTemplate(first_registry.primary).transaction():
            with pytest.raises(IllegalTransactionStateError, match="another"):
                await getattr(second, operation)(*arguments[operation])
    assert dml(first_counter) == {}
    assert dml(second_counter) == {}
    assert await snapshot(first_registry) == before_first
    assert await snapshot(second_registry) == before_second


async def test_named_memory_datasource_stays_bound_to_its_initial_application(independent_memory_owners):
    from pyfly.agentic.memory.relational_store import RelationalMemoryStore
    from pyfly.data.relational.sqlalchemy.transaction_manager import transaction_managers_for
    from pyfly.data.transaction import install_registry, uninstall_registry
    from pyfly.data.transaction.registry import installed_registry

    _first, _second, first_registry, second_registry, _snapshot = independent_memory_owners
    previous = installed_registry()
    first_managers, second_managers = (
        transaction_managers_for(first_registry),
        transaction_managers_for(second_registry),
    )
    named = RelationalMemoryStore("primary")
    try:
        install_registry(second_managers)
        await named.initialize()
        install_registry(first_managers)
        assert (await named.async_load_by_key("shared", "value")).content == "second-owner"
        async with TransactionTemplate(first_registry.primary).transaction():
            with pytest.raises(IllegalTransactionStateError, match="another"):
                await named.async_save("shared", MemoryEntry(entry_id="forbidden"))
    finally:
        await named.close()
        uninstall_registry(first_managers)
        if previous is not None:
            install_registry(previous)
