"""Tenant isolation against the real local Qdrant engine, without network IO."""

import json
from uuid import NAMESPACE_URL, uuid5

import pytest
from qdrant_client import AsyncQdrantClient
from qdrant_client.models import PointStruct

from pyfly.agentic.exceptions import VectorStoreError
from pyfly.agentic.vectorstores.qdrant_store import QdrantVectorStore
from pyfly.agentic.vectorstores.scoped import TenantScopedVectorStore
from pyfly.agentic.vectorstores.types import VectorDocument

DOC_ID = "12345678-1234-5678-1234-567812345678"


@pytest.fixture
async def backend(monkeypatch):
    client = AsyncQdrantClient(location=":memory:")
    monkeypatch.setattr("pyfly.agentic.vectorstores.qdrant_store.AsyncQdrantClient", lambda **kw: client)
    store = QdrantVectorStore(collection_name="tenant-test", vector_size=2)
    await store.initialise()
    try:
        yield store, client
    finally:
        await store.close()


async def test_same_logical_id_roundtrips_and_deletes_only_its_tenant(backend):
    store = TenantScopedVectorStore(backend[0])
    for tenant in ("a", "b"):
        await store.upsert(
            [VectorDocument(id=DOC_ID, text=tenant, embedding=[1.0, 0.0])], tenant_id=tenant, workspace_id="work"
        )
    for tenant in ("a", "b"):
        result = await store.search([1.0, 0.0], tenant_id=tenant, workspace_id="work")
        assert [(r.document.id, r.document.text) for r in result] == [(DOC_ID, tenant)]
    points, _ = await backend[1].scroll(collection_name="tenant-test")
    physical_id = str(next(p.id for p in points if p.payload["_namespace"] == "t/a/w/work"))
    await store.upsert(
        [VectorDocument(id=physical_id, text="collision-shaped", embedding=[1.0, 0.0])],
        tenant_id="a",
        workspace_id="work",
    )
    await store.delete([physical_id], tenant_id="a", workspace_id="work")
    assert (await store.search([1.0, 0.0], tenant_id="a", workspace_id="work"))[0].document.id == DOC_ID
    await store.delete([DOC_ID], tenant_id="a", workspace_id="work")
    assert await store.search([1.0, 0.0], tenant_id="a", workspace_id="work") == []
    assert (await store.search([1.0, 0.0], tenant_id="b", workspace_id="work"))[0].document.id == DOC_ID


async def test_metadata_cannot_override_reserved_payload_fields(backend):
    store = backend[0]
    await store.upsert(
        [
            VectorDocument(
                id=DOC_ID,
                text="owned by a",
                embedding=[1.0, 0.0],
                metadata={
                    "_namespace": "b",
                    "text": "forged",
                    "_document_id": "forged-id",
                    "tag": "kept",
                },
            )
        ],
        namespace="a",
    )
    assert await store.search([1.0, 0.0], namespace="b") == []
    result = await store.search([1.0, 0.0], namespace="a")
    assert [(r.document.id, r.document.text, r.document.metadata) for r in result] == [
        (DOC_ID, "owned by a", {"tag": "kept"}),
    ]


async def test_legacy_upsert_migrates_only_its_namespace_and_is_idempotent(backend):
    store, client = backend
    await client.upsert(
        collection_name="tenant-test",
        points=[
            PointStruct(
                id=DOC_ID,
                vector=[1.0, 0.0],
                payload={"_namespace": "b", "text": "legacy b", "tag": "old"},
            )
        ],
    )
    assert (await store.search([1.0, 0.0], namespace="b"))[0].document.id == DOC_ID
    await store.upsert([VectorDocument(id=DOC_ID, text="new a", embedding=[1.0, 0.0])], namespace="a")
    assert (await store.search([1.0, 0.0], namespace="b"))[0].document.text == "legacy b"
    for _ in range(2):
        await store.upsert([VectorDocument(id=DOC_ID, text="new b", embedding=[1.0, 0.0])], namespace="b")
    assert await client.retrieve(collection_name="tenant-test", ids=[DOC_ID]) == []
    assert (await client.count(collection_name="tenant-test")).count == 2
    assert (await store.search([1.0, 0.0], namespace="b"))[0].document.text == "new b"


async def test_preexisting_legacy_point_at_new_physical_id_is_never_overwritten(backend):
    store, client = backend
    physical_id = str(uuid5(NAMESPACE_URL, json.dumps(["a", DOC_ID], separators=(",", ":"))))
    await client.upsert(
        collection_name="tenant-test",
        points=[
            PointStruct(
                id=physical_id,
                vector=[1.0, 0.0],
                payload={"_namespace": "b", "text": "legacy victim"},
            )
        ],
    )
    with pytest.raises(VectorStoreError, match="collision"):
        await store.upsert([VectorDocument(id=DOC_ID, text="attacker", embedding=[1.0, 0.0])], namespace="a")
    assert (await store.search([1.0, 0.0], namespace="b"))[0].document.text == "legacy victim"


async def test_legacy_reserved_marker_requires_migration_without_duplicate(backend):
    store, client = backend
    await client.upsert(
        collection_name="tenant-test",
        points=[
            PointStruct(
                id=DOC_ID,
                vector=[1.0, 0.0],
                payload={"_namespace": "a", "text": "legacy", "_document_id": "user-metadata"},
            )
        ],
    )
    assert (await store.search([1.0, 0.0], namespace="a"))[0].document.id == DOC_ID
    with pytest.raises(VectorStoreError, match="migrat"):
        await store.upsert([VectorDocument(id=DOC_ID, text="new", embedding=[1.0, 0.0])], namespace="a")
    assert (await client.count(collection_name="tenant-test")).count == 1
