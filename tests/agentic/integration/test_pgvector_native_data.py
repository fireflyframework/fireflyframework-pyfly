"""Real pgvector operations share PyFly Data units and borrowed resource ownership."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
from sqlalchemy import text

from pyfly.agentic.exceptions import VectorStoreError
from pyfly.agentic.vectorstores.pgvector_store import PgVectorVectorStore
from pyfly.agentic.vectorstores.types import VectorDocument
from pyfly.core.config import Config
from pyfly.data.relational.datasource_registry import DataSourceRegistry
from pyfly.data.relational.sqlalchemy.transaction_manager import transaction_managers_for
from pyfly.data.transaction import TransactionTemplate
from pyfly.data.transaction.errors import IllegalTransactionStateError
from tests.agentic.integration.test_pgvector_store import _testcontainers_cleanup, pg_url  # noqa: F401

pytestmark = pytest.mark.integration


def _registry(url: str, *, policy: str = "create") -> DataSourceRegistry:
    return DataSourceRegistry(
        Config(
            {
                "pyfly": {
                    "data": {
                        "relational": {
                            "enabled": True,
                            "url": url.replace("postgresql://", "postgresql+asyncpg://"),
                            "ddl-auto": policy,
                        }
                    }
                }
            }
        )
    )


def _doc(identifier: str) -> VectorDocument:
    return VectorDocument(id=identifier, text=identifier, embedding=[1.0, 0.0, 0.0, 0.0])


@pytest.fixture
async def native_data(pg_url: str) -> AsyncIterator[DataSourceRegistry]:  # noqa: F811
    registry = _registry(pg_url)
    try:
        yield registry
        assert registry.engine().sync_engine.pool.checkedout() == 0
    finally:
        await registry.close()


@pytest.fixture
async def native_store(native_data: DataSourceRegistry) -> AsyncIterator[PgVectorVectorStore]:
    store = PgVectorVectorStore(native_data.primary, dimension=4, table_name=f"native_{uuid.uuid4().hex}")
    await store.initialise()
    try:
        yield store
    finally:
        await store.close()


async def test_business_and_vector_writes_roll_back_together(
    native_data: DataSourceRegistry, native_store: PgVectorVectorStore
) -> None:
    business = f"business_{uuid.uuid4().hex}"
    async with native_data.engine().begin() as connection:
        await connection.execute(text(f"CREATE TABLE {business} (id TEXT PRIMARY KEY)"))
    manager = transaction_managers_for(native_data).get("primary")
    with pytest.raises(RuntimeError, match="abort"):
        async with TransactionTemplate(manager).transaction() as unit:
            await unit.resource.execute(text(f"INSERT INTO {business} VALUES ('business')"))
            await native_store.upsert([_doc("vector")], namespace="owner")
            assert len(await native_store.search([1.0, 0.0, 0.0, 0.0], namespace="owner")) == 1
            raise RuntimeError("abort")
    assert await native_store.search([1.0, 0.0, 0.0, 0.0], namespace="owner") == []
    async with native_data.engine().connect() as connection:
        assert (await connection.execute(text(f"SELECT COUNT(*) FROM {business}"))).scalar_one() == 0


async def test_read_only_unit_rejects_raw_vector_writes(
    native_data: DataSourceRegistry, native_store: PgVectorVectorStore
) -> None:
    manager = transaction_managers_for(native_data).get("primary")
    async with TransactionTemplate(manager).transaction(read_only=True):
        with pytest.raises(VectorStoreError) as error:
            await native_store.upsert([_doc("blocked")])
        assert isinstance(error.value.__cause__, IllegalTransactionStateError)
    assert await native_store.search([1.0, 0.0, 0.0, 0.0]) == []


async def test_close_and_restart_keep_borrowed_datasource_usable(
    native_data: DataSourceRegistry, native_store: PgVectorVectorStore
) -> None:
    await native_store.upsert([_doc("saved")])
    await native_store.close()
    await native_store.close()
    async with native_data.engine().connect() as connection:
        assert (await connection.execute(text("SELECT 42"))).scalar_one() == 42
    await native_store.initialise()
    assert [row.document.id for row in await native_store.search([1.0, 0.0, 0.0, 0.0])] == ["saved"]


async def test_two_datasource_owners_do_not_join_each_others_units(
    native_data: DataSourceRegistry,
    native_store: PgVectorVectorStore,
    pg_url: str,  # noqa: F811
) -> None:
    other = _registry(pg_url)
    second = PgVectorVectorStore(other.primary, dimension=4, table_name=f"other_{uuid.uuid4().hex}")
    try:
        await second.initialise()
        await native_store.upsert([_doc("first")])
        await second.upsert([_doc("second")])
        manager = transaction_managers_for(native_data).get("primary")
        async with TransactionTemplate(manager).transaction():
            with pytest.raises(VectorStoreError, match="another"):
                await second.search([1.0, 0.0, 0.0, 0.0])
        await native_store.close()
        assert [row.document.id for row in await second.search([1.0, 0.0, 0.0, 0.0])] == ["second"]
        async with other.engine().connect() as connection:
            assert (await connection.execute(text("SELECT 1"))).scalar_one() == 1
    finally:
        await second.close()
        await other.close()


async def test_schema_bootstrap_is_rejected_inside_business_unit(native_data: DataSourceRegistry) -> None:
    store = PgVectorVectorStore(native_data.primary, dimension=4, table_name=f"late_{uuid.uuid4().hex}")
    manager = transaction_managers_for(native_data).get("primary")
    async with TransactionTemplate(manager).transaction():
        with pytest.raises(VectorStoreError, match="initialise"):
            await store.upsert([_doc("late")])
    async with native_data.engine().connect() as connection:
        assert (
            await connection.execute(text("SELECT to_regclass(:table)"), {"table": store._table})
        ).scalar_one() is None


async def test_cross_loop_misuse_fails_before_touching_pool(native_store: PgVectorVectorStore) -> None:
    def on_other_loop() -> None:
        with pytest.raises(VectorStoreError, match="event loop"):
            asyncio.run(native_store.search([1.0, 0.0, 0.0, 0.0]))

    await asyncio.to_thread(on_other_loop)


async def test_rls_hook_uses_same_transaction_and_resets_after_operation(native_data: DataSourceRegistry) -> None:
    table = f"rls_vec_{uuid.uuid4().hex}"
    role = f"rls_role_{uuid.uuid4().hex}"

    class RlsStore(PgVectorVectorStore):
        async def _prepare_session(self, conn: Any, *, namespace: str) -> None:
            await conn.execute(f"SET LOCAL ROLE {role}")
            await conn.execute(
                "SELECT set_config('agentic.namespace', $1, true)", "owner" if namespace == "denied" else namespace
            )

    store = RlsStore(native_data.primary, dimension=4, table_name=table)
    await store.initialise()
    async with native_data.engine().begin() as connection:
        await connection.execute(text(f"CREATE ROLE {role}"))
        await connection.execute(text(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {table} TO {role}"))
        await connection.execute(text(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY"))
        await connection.execute(
            text(
                f"CREATE POLICY namespace ON {table} "
                "USING (namespace = current_setting('agentic.namespace', true)) "
                "WITH CHECK (namespace = current_setting('agentic.namespace', true))"
            )
        )
    try:
        await store.upsert([_doc("private")], namespace="owner")
        assert len(await store.search([1.0, 0.0, 0.0, 0.0], namespace="owner")) == 1
        assert await store.search([1.0, 0.0, 0.0, 0.0], namespace="other") == []
        with pytest.raises(VectorStoreError, match="row-level security"):
            await store.upsert([_doc("denied-document")], namespace="denied")
        await store.delete(["private"], namespace="other")
        assert len(await store.search([1.0, 0.0, 0.0, 0.0], namespace="owner")) == 1
        async with native_data.engine().connect() as connection:
            assert (await connection.execute(text("SELECT current_user"))).scalar_one() != role
            assert (
                await connection.execute(text("SELECT current_setting('agentic.namespace', true)"))
            ).scalar_one() in (None, "")
    finally:
        await store.close()


async def test_caught_namespace_conflict_marks_business_unit_for_rollback(
    native_data: DataSourceRegistry,
    native_store: PgVectorVectorStore,
) -> None:
    from pyfly.data.transaction.errors import UnexpectedRollbackError

    await native_store.upsert([_doc("foreign")], namespace="owner")
    manager = transaction_managers_for(native_data).get("primary")
    with pytest.raises(UnexpectedRollbackError):
        async with TransactionTemplate(manager).transaction():
            await native_store.upsert([_doc("earlier")], namespace="caller")
            with pytest.raises(VectorStoreError):
                await native_store.upsert([_doc("local"), _doc("foreign")], namespace="caller")
    assert await native_store.search([1.0, 0.0, 0.0, 0.0], namespace="caller") == []
    assert len(await native_store.search([1.0, 0.0, 0.0, 0.0], namespace="owner")) == 1


async def test_two_adapters_share_datasource_without_duplicate_resource_shutdown(
    native_data: DataSourceRegistry,
    native_store: PgVectorVectorStore,
) -> None:
    second = PgVectorVectorStore(native_data.primary, dimension=4, table_name=native_store._table, create_schema=False)
    try:
        await second.initialise()
        await native_store.upsert([_doc("shared")])
        await native_store.close()
        assert [row.document.id for row in await second.search([1.0, 0.0, 0.0, 0.0])] == ["shared"]
        await second.delete(["shared"])
        assert await second.search([1.0, 0.0, 0.0, 0.0]) == []
    finally:
        await second.close()


async def test_named_store_keeps_its_startup_manager_when_another_context_is_installed(
    native_data: DataSourceRegistry,
    pg_url: str,  # noqa: F811
) -> None:
    from pyfly.data.transaction import install_registry, uninstall_registry
    from pyfly.data.transaction.registry import installed_registry

    previous = installed_registry()
    first = transaction_managers_for(native_data)
    other_data = _registry(pg_url)
    other = transaction_managers_for(other_data)
    install_registry(first)
    store = PgVectorVectorStore("primary", dimension=4, table_name=f"named_{uuid.uuid4().hex}")
    try:
        await store.initialise()
        install_registry(other)
        async with TransactionTemplate(first.get("primary")).transaction():
            await store.upsert([_doc("owner-context")])
        assert [row.document.id for row in await store.search([1.0, 0.0, 0.0, 0.0])] == ["owner-context"]
    finally:
        await store.close()
        uninstall_registry(other)
        if previous is not None:
            install_registry(previous)
        await other_data.close()


async def test_production_schema_policy_only_validates_and_failed_start_can_retry(pg_url: str) -> None:  # noqa: F811
    registry = _registry(pg_url, policy="none")
    table = f"migrated_{uuid.uuid4().hex}"
    store = PgVectorVectorStore(registry.primary, dimension=4, table_name=table)
    creator = PgVectorVectorStore(registry.primary, dimension=4, table_name=table, create_schema=True)
    try:
        with pytest.raises(VectorStoreError, match="create_schema=False"):
            await store.initialise()

        def retry_from_another_loop() -> None:
            with pytest.raises(VectorStoreError, match="event loop"):
                asyncio.run(store.initialise())

        await asyncio.to_thread(retry_from_another_loop)
        async with registry.engine().connect() as connection:
            assert (await connection.execute(text("SELECT to_regclass(:table)"), {"table": table})).scalar_one() is None
        await creator.initialise()
        await creator.close()
        await store.initialise()
        await store.upsert([_doc("migrated")])
        assert len(await store.search([1.0, 0.0, 0.0, 0.0])) == 1
    finally:
        await creator.close()
        await store.close()
        await registry.close()
