# Copyright 2026 Firefly Software Foundation
# SPDX-License-Identifier: Apache-2.0
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Integration tests for vector and memory stores against a real Postgres.

Uses Testcontainers with the ``pgvector/pgvector`` image, so these exercise the
real schema bootstrap, HNSW index, ANN ordering, namespace isolation, metadata
filtering, and the ``_prepare_session`` extension hook -- no mocks. Memory-store
tests also verify mixed sync/async access, native JSONB metadata, and TTL cleanup.

Marked ``integration``; deselect with ``-m "not integration"`` when Docker is
unavailable.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest

from pyfly.agentic.exceptions import VectorStoreError
from pyfly.agentic.vectorstores.pgvector_store import PgVectorVectorStore
from pyfly.agentic.vectorstores.scoped import TenantScopedVectorStore
from pyfly.agentic.vectorstores.types import SearchFilter, VectorDocument
from pyfly.core.config import Config
from pyfly.data.relational.datasource_registry import DataSourceRegistry

pytestmark = pytest.mark.integration

_DIM = 4


def _asyncpg_url(raw: str) -> str:
    """Strip any SQLAlchemy driver suffix so asyncpg accepts the URL."""
    return raw.replace("+psycopg2", "").replace("+psycopg", "")


@pytest.fixture(scope="session")
def _testcontainers_cleanup() -> Iterator[None]:
    from testcontainers.core.container import Reaper

    try:
        yield
    finally:
        Reaper.delete_instance()


@pytest.fixture(scope="module")
def pg_url(_testcontainers_cleanup: None) -> Iterator[str]:
    from testcontainers.postgres import PostgresContainer

    with PostgresContainer("pgvector/pgvector:pg16") as pg:
        yield _asyncpg_url(pg.get_connection_url())


@pytest.fixture
async def pg_data(pg_url: str):
    registry = DataSourceRegistry(
        Config(
            {
                "pyfly": {
                    "data": {
                        "relational": {
                            "enabled": True,
                            "url": pg_url.replace("postgresql://", "postgresql+asyncpg://"),
                            "ddl-auto": "create",
                        }
                    }
                }
            }
        )
    )
    try:
        yield registry
        assert registry.engine().sync_engine.pool.checkedout() == 0
    finally:
        await registry.close()


@pytest.fixture
async def store(pg_data: DataSourceRegistry):
    """A freshly-initialised store on a unique table per test."""
    table = f"vec_{uuid.uuid4().hex[:8]}"
    s = PgVectorVectorStore(datasource=pg_data.primary, dimension=_DIM, table_name=table)
    await s.initialise()
    try:
        yield s
    finally:
        await s.close()


def _doc(id_: str, vec: list[float], **metadata: object) -> VectorDocument:
    return VectorDocument(id=id_, text=f"doc-{id_}", embedding=vec, metadata=metadata)


class TestPgVectorIntegration:
    async def test_initialise_is_idempotent(self, store: PgVectorVectorStore) -> None:
        # Already initialised by the fixture; a second call must not raise.
        await store.initialise()

    async def test_upsert_then_search_returns_doc(self, store: PgVectorVectorStore) -> None:
        await store.upsert([_doc("1", [1.0, 0.0, 0.0, 0.0])], namespace="ns")
        results = await store.search([1.0, 0.0, 0.0, 0.0], top_k=5, namespace="ns")
        assert [r.document.id for r in results] == ["1"]
        assert results[0].document.text == "doc-1"
        assert results[0].score == pytest.approx(1.0, abs=1e-3)

    async def test_search_orders_by_cosine_similarity(self, store: PgVectorVectorStore) -> None:
        await store.upsert(
            [_doc("near", [1.0, 0.0, 0.0, 0.0]), _doc("far", [0.0, 1.0, 0.0, 0.0])],
            namespace="ns",
        )
        results = await store.search([1.0, 0.1, 0.0, 0.0], top_k=2, namespace="ns")
        assert [r.document.id for r in results] == ["near", "far"]

    async def test_namespace_isolation(self, store: PgVectorVectorStore) -> None:
        await store.upsert([_doc("a", [1.0, 0.0, 0.0, 0.0])], namespace="ns_a")
        await store.upsert([_doc("b", [1.0, 0.0, 0.0, 0.0])], namespace="ns_b")
        results = await store.search([1.0, 0.0, 0.0, 0.0], top_k=5, namespace="ns_a")
        assert [r.document.id for r in results] == ["a"]
        # A namespace that was never written returns nothing.
        empty = await store.search([1.0, 0.0, 0.0, 0.0], top_k=5, namespace="ns_none")
        assert empty == []

    async def test_upsert_overwrites_by_id(self, store: PgVectorVectorStore) -> None:
        await store.upsert([_doc("1", [1.0, 0.0, 0.0, 0.0], v="old")], namespace="ns")
        await store.upsert([_doc("1", [1.0, 0.0, 0.0, 0.0], v="new")], namespace="ns")
        results = await store.search([1.0, 0.0, 0.0, 0.0], top_k=5, namespace="ns")
        assert len(results) == 1
        assert results[0].document.metadata["v"] == "new"

    async def test_upsert_rejects_foreign_namespace_without_disclosing_data(self, store: PgVectorVectorStore) -> None:
        document = VectorDocument(id="private-id-'\n", text="private document", embedding=[1.0, 0.0, 0.0, 0.0])
        await store.upsert([document], namespace="private-namespace")

        with pytest.raises(VectorStoreError) as error:
            await store.upsert([document.model_copy(update={"text": "replacement"})], namespace="other")

        for private_value in ("private-id", document.text, "private-namespace"):
            assert private_value not in str(error.value)
            assert private_value not in repr(error.value)
        owner = await store.search([1.0, 0.0, 0.0, 0.0], namespace="private-namespace")
        assert [(result.document.id, result.document.text) for result in owner] == [(document.id, "private document")]
        assert await store.search([1.0, 0.0, 0.0, 0.0], namespace="other") == []
        await store.delete([document.id], namespace="other")
        assert len(await store.search([1.0, 0.0, 0.0, 0.0], namespace="private-namespace")) == 1

    @pytest.mark.parametrize("conflict_position", [0, 1, 2])
    async def test_namespace_conflict_rolls_back_whole_batch(
        self, store: PgVectorVectorStore, conflict_position: int
    ) -> None:
        await store.upsert([_doc("foreign", [1.0, 0.0, 0.0, 0.0], value="private")], namespace="owner")
        await store.upsert([_doc("local", [1.0, 0.0, 0.0, 0.0], value="before")], namespace="caller")
        batch = [
            _doc("local", [1.0, 0.0, 0.0, 0.0], value="after"),
            _doc("new", [1.0, 0.0, 0.0, 0.0]),
        ]
        batch.insert(conflict_position, _doc("foreign", [1.0, 0.0, 0.0, 0.0], value="replacement"))

        with pytest.raises(VectorStoreError):
            await store.upsert(batch, namespace="caller")

        caller = await store.search([1.0, 0.0, 0.0, 0.0], namespace="caller")
        assert [(result.document.id, result.document.metadata) for result in caller] == [("local", {"value": "before"})]
        owner = await store.search([1.0, 0.0, 0.0, 0.0], namespace="owner")
        assert [(result.document.id, result.document.metadata) for result in owner] == [
            ("foreign", {"value": "private"})
        ]

    async def test_same_namespace_duplicate_ids_in_batch_keep_last_update(self, store: PgVectorVectorStore) -> None:
        await store.upsert(
            [_doc("same", [1.0, 0.0, 0.0, 0.0], value="first"), _doc("same", [0.0, 1.0, 0.0, 0.0], value="last")],
            namespace="owner",
        )
        result = await store.search([0.0, 1.0, 0.0, 0.0], namespace="owner")
        assert len(result) == 1
        assert result[0].document.metadata == {"value": "last"}
        assert result[0].score == pytest.approx(1.0)

    @pytest.mark.parametrize("other_scope", [("other", "main"), ("owner", "other")])
    async def test_scoped_upsert_cannot_move_document(
        self, store: PgVectorVectorStore, other_scope: tuple[str, str]
    ) -> None:
        scoped = TenantScopedVectorStore(store)
        await scoped.upsert([_doc("same", [1.0, 0.0, 0.0, 0.0])], tenant_id="owner", workspace_id="main")
        with pytest.raises(VectorStoreError):
            await scoped.upsert(
                [_doc("same", [0.0, 1.0, 0.0, 0.0])], tenant_id=other_scope[0], workspace_id=other_scope[1]
            )
        result = await scoped.search([1.0, 0.0, 0.0, 0.0], tenant_id="owner", workspace_id="main")
        assert len(result) == 1
        assert result[0].document.metadata == {"tenant_id": "owner", "workspace_id": "main"}
        assert result[0].score == pytest.approx(1.0)
        assert await scoped.search([1.0, 0.0, 0.0, 0.0], tenant_id=other_scope[0], workspace_id=other_scope[1]) == []

    @pytest.mark.parametrize("existing", [False, True])
    async def test_concurrent_namespace_conflict_has_one_owner(
        self, store: PgVectorVectorStore, pg_data: DataSourceRegistry, existing: bool
    ) -> None:
        if existing:
            await store.upsert([_doc("same", [1.0, 0.0, 0.0, 0.0], value="original")], namespace="owner")
        rendezvous = asyncio.Barrier(2)

        class ConcurrentStore(PgVectorVectorStore):
            async def _prepare_session(self, conn, *, namespace: str) -> None:
                await rendezvous.wait()

        writers = [
            ConcurrentStore(datasource=pg_data.primary, dimension=_DIM, table_name=store._table, create_schema=False)
            for _ in range(2)
        ]
        try:
            for writer in writers:
                await writer.initialise()
            outcomes = await asyncio.wait_for(
                asyncio.gather(
                    writers[0].upsert([_doc("same", [1.0, 0.0, 0.0, 0.0], value="owner")], namespace="owner"),
                    writers[1].upsert([_doc("same", [0.0, 1.0, 0.0, 0.0], value="other")], namespace="other"),
                    return_exceptions=True,
                ),
                timeout=10,
            )
        finally:
            for writer in writers:
                await writer.close()

        assert sum(outcome is None for outcome in outcomes) == 1
        assert sum(isinstance(outcome, VectorStoreError) for outcome in outcomes) == 1
        winner = "owner" if outcomes[0] is None else "other"
        if existing:
            assert winner == "owner"
        loser = "other" if winner == "owner" else "owner"
        result = await store.search([1.0, 0.0, 0.0, 0.0], namespace=winner)
        assert len(result) == 1
        assert result[0].document.metadata == {"value": winner}
        assert await store.search([1.0, 0.0, 0.0, 0.0], namespace=loser) == []

    async def test_delete_removes(self, store: PgVectorVectorStore) -> None:
        await store.upsert([_doc("1", [1.0, 0.0, 0.0, 0.0])], namespace="ns")
        await store.delete(["1"], namespace="ns")
        results = await store.search([1.0, 0.0, 0.0, 0.0], top_k=5, namespace="ns")
        assert results == []

    async def test_metadata_filter_eq(self, store: PgVectorVectorStore) -> None:
        await store.upsert(
            [
                _doc("1", [1.0, 0.0, 0.0, 0.0], source_id="s1"),
                _doc("2", [1.0, 0.0, 0.0, 0.0], source_id="s2"),
            ],
            namespace="ns",
        )
        results = await store.search(
            [1.0, 0.0, 0.0, 0.0],
            top_k=5,
            namespace="ns",
            filters=[SearchFilter(field="source_id", operator="eq", value="s1")],
        )
        assert [r.document.id for r in results] == ["1"]

    async def test_metadata_filter_in(self, store: PgVectorVectorStore) -> None:
        await store.upsert(
            [
                _doc("1", [1.0, 0.0, 0.0, 0.0], source_id="s1"),
                _doc("2", [1.0, 0.0, 0.0, 0.0], source_id="s2"),
                _doc("3", [1.0, 0.0, 0.0, 0.0], source_id="s3"),
            ],
            namespace="ns",
        )
        results = await store.search(
            [1.0, 0.0, 0.0, 0.0],
            top_k=5,
            namespace="ns",
            filters=[SearchFilter(field="source_id", operator="in", value=["s1", "s3"])],
        )
        assert {r.document.id for r in results} == {"1", "3"}

    async def test_metadata_filter_ne(self, store: PgVectorVectorStore) -> None:
        await store.upsert(
            [
                _doc("1", [1.0, 0.0, 0.0, 0.0], source_id="s1"),
                _doc("2", [1.0, 0.0, 0.0, 0.0], source_id="s2"),
            ],
            namespace="ns",
        )
        results = await store.search(
            [1.0, 0.0, 0.0, 0.0],
            top_k=5,
            namespace="ns",
            filters=[SearchFilter(field="source_id", operator="ne", value="s1")],
        )
        assert [r.document.id for r in results] == ["2"]

    async def test_prepare_session_hook_is_invoked(self, pg_data: DataSourceRegistry) -> None:
        """A subclass can observe/augment the per-operation session (RLS seam)."""
        seen: list[str] = []
        table = f"vec_{uuid.uuid4().hex[:8]}"

        class _HookedStore(PgVectorVectorStore):
            async def _prepare_session(self, conn, *, namespace: str) -> None:
                seen.append(namespace)

        s = _HookedStore(datasource=pg_data.primary, dimension=_DIM, table_name=table)
        await s.initialise()
        try:
            await s.upsert([_doc("1", [1.0, 0.0, 0.0, 0.0])], namespace="ns_hook")
            await s.search([1.0, 0.0, 0.0, 0.0], namespace="ns_hook")
            assert "ns_hook" in seen
        finally:
            await s.close()


async def test_postgres_memory_mixes_sync_async_operations_and_native_jsonb_ttl(pg_url: str) -> None:
    import json

    from sqlalchemy import text

    from pyfly.agentic.memory.database_store import PostgreSQLStore
    from pyfly.agentic.memory.types import MemoryEntry
    from pyfly.core.config import Config
    from pyfly.data.relational.datasource_registry import DataSourceRegistry
    from pyfly.data.relational.framework_schema import framework_metadata

    schema = f"memory_{uuid.uuid4().hex}"
    namespace = f"tenant_{uuid.uuid4().hex}"
    config = Config(
        {
            "pyfly": {
                "data": {
                    "relational": {
                        "enabled": True,
                        "url": pg_url.replace("postgresql://", "postgresql+asyncpg://"),
                        "ddl-auto": "create",
                    }
                }
            }
        }
    )
    registry = DataSourceRegistry(config)
    store = PostgreSQLStore(registry.primary, schema_name=schema, create_table=True)
    live = MemoryEntry(
        key="live",
        content={"values": [1, 2, 3]},
        metadata={"tags": ["verified"], "created": datetime(2026, 1, 1, tzinfo=UTC)},
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    expired = MemoryEntry(key="expired", expires_at=datetime.now(UTC) - timedelta(hours=1))
    try:
        await store.initialize()
        await asyncio.to_thread(store.save, namespace, live)
        await store.async_save(namespace, expired)
        loaded = await store.async_load(namespace)
        assert [entry.key for entry in loaded] == ["live"]
        assert loaded[0].model_dump(mode="json") == live.model_dump(mode="json")
        assert loaded[0].is_expired is False
        assert (await asyncio.to_thread(store.load_by_key, namespace, "live")).content == live.content
        assert await store.async_load_by_key(namespace, "expired") is None
        assert await asyncio.to_thread(store.load, f"{namespace}_other") == []
        async with registry.engine().connect() as inspection:
            row = (
                (
                    await inspection.execute(
                        text(
                            f"SELECT metadata, jsonb_typeof(metadata) AS kind, expires_at "
                            f"FROM {schema}.memory_entries WHERE entry_id = :id"
                        ),
                        {"id": live.entry_id},
                    )
                )
                .mappings()
                .one()
            )
            assert row["kind"] == "object"
            decoded = json.loads(row["metadata"]) if isinstance(row["metadata"], str) else row["metadata"]
            assert decoded == live.model_dump(mode="json")["metadata"]
            assert row["expires_at"] == live.expires_at
        assert await store.cleanup_expired() == 1
        assert await store.cleanup_expired() == 0
        await store.async_delete(namespace, live.entry_id)
        assert await asyncio.to_thread(store.load, namespace) == []
        await store.close()
        async with registry.engine().connect() as inspection:
            assert (await inspection.execute(text(f"SELECT COUNT(*) FROM {schema}.memory_entries"))).scalar_one() == 0
    finally:
        await store.close()
        await registry.close()
        framework_metadata.remove(store._table)


async def test_postgres_configured_memory_restores_facts_through_a_second_store(pg_url: str) -> None:
    from pyfly.agentic.config import AgenticConfig
    from pyfly.agentic.memory import MemoryManager
    from pyfly.agentic.memory.database_store import PostgreSQLStore
    from pyfly.core.config import Config
    from pyfly.data.relational.datasource_registry import DataSourceRegistry
    from pyfly.data.relational.framework_schema import framework_metadata

    schema = f"facts_{uuid.uuid4().hex}"
    config = Config(
        {
            "pyfly": {
                "data": {
                    "relational": {
                        "enabled": True,
                        "url": pg_url.replace("postgresql://", "postgresql+asyncpg://"),
                        "ddl-auto": "create",
                    }
                }
            }
        }
    )
    registry = DataSourceRegistry(config)
    cfg = AgenticConfig(memory_backend="postgres", memory_postgres_schema=schema)
    store = PostgreSQLStore(registry.primary, schema_name=schema, create_table=True)
    await store.initialize()
    original = MemoryManager.from_config(cfg, store=store, initialize=False)
    scope = f"case_{uuid.uuid4().hex}"
    value = {"items": ["one", "two"], "completed": True}
    try:
        scoped = original.fork(working_scope_id=scope)
        await scoped.async_set_fact("result", value)
        assert await scoped.async_get_fact("result") == value
        await original.aclose()
        second_store = PostgreSQLStore(registry.primary, schema_name=schema)
        await second_store.initialize()
        restored = MemoryManager.from_config(cfg, store=second_store, initialize=False)
        try:
            assert restored.store is not original.store
            restored_scope = restored.fork(working_scope_id=scope)
            assert await restored_scope.async_get_fact("result") == value
            assert await restored.fork(working_scope_id=f"{scope}_other").async_get_fact("result") is None
            await restored_scope.async_set_fact("result", {"updated": True})
            assert await restored_scope.async_get_fact("result") == {"updated": True}
        finally:
            await restored.aclose()
    finally:
        await store.close()
        await registry.close()
        framework_metadata.remove(store._table)
