"""Chroma namespace boundaries exercised with an embedded client."""

import json
from uuid import NAMESPACE_URL, uuid5

import chromadb
import pytest
from chromadb.config import Settings

from pyfly.agentic.exceptions import VectorStoreError
from pyfly.agentic.vectorstores.chroma_store import ChromaVectorStore
from pyfly.agentic.vectorstores.types import VectorDocument


@pytest.fixture
def backend(tmp_path):
    client = chromadb.PersistentClient(path=str(tmp_path / "chroma"), settings=Settings(anonymized_telemetry=False))
    store = ChromaVectorStore(collection_name="tenant-test", client=client)
    return store, client.get_collection("tenant-test")


async def test_same_id_remains_distinct_with_authoritative_metadata_and_scoped_delete(backend):
    store, collection = backend
    for namespace in ("a", "b"):
        await store.upsert(
            [
                VectorDocument(
                    id="shared",
                    text=namespace,
                    embedding=[1.0, 0.0],
                    metadata={
                        "_namespace": "forged",
                        "_document_id": "forged",
                        "tag": "kept",
                    },
                )
            ],
            namespace=namespace,
        )
    for namespace in ("a", "b"):
        result = await store.search([1.0, 0.0], namespace=namespace)
        assert [(r.document.id, r.document.text, r.document.metadata) for r in result] == [
            ("shared", namespace, {"tag": "kept"}),
        ]
    assert await store.search([1.0, 0.0], namespace="forged") == []
    await store.delete(["shared"], namespace="a")
    assert await store.search([1.0, 0.0], namespace="a") == []
    assert collection.count() == 1
    assert (await store.search([1.0, 0.0], namespace="b"))[0].document.id == "shared"


async def test_legacy_reads_work_but_writes_require_explicit_migration(backend):
    store, collection = backend
    collection.upsert(ids=["shared"], embeddings=[[1.0, 0.0]], documents=["legacy b"], metadatas=[{"_namespace": "b"}])
    assert (await store.search([1.0, 0.0], namespace="b"))[0].document.id == "shared"
    with pytest.raises(VectorStoreError, match="migrat"):
        await store.upsert([VectorDocument(id="shared", text="new b", embedding=[1.0, 0.0])], namespace="b")
    with pytest.raises(VectorStoreError, match="migrat"):
        await store.delete(["shared"], namespace="b")
    await store.upsert([VectorDocument(id="shared", text="new a", embedding=[1.0, 0.0])], namespace="a")
    assert (await store.search([1.0, 0.0], namespace="b"))[0].document.text == "legacy b"
    for _ in range(2):
        await store.upsert([VectorDocument(id="shared", text="new a", embedding=[1.0, 0.0])], namespace="a")
    assert collection.get(ids=["shared"])["documents"] == ["legacy b"]
    assert collection.count() == 2
    assert (await store.search([1.0, 0.0], namespace="a"))[0].document.text == "new a"


async def test_legacy_collision_at_new_physical_id_is_rejected(backend):
    store, collection = backend
    physical_id = str(uuid5(NAMESPACE_URL, json.dumps(["a", "shared"], separators=(",", ":"))))
    collection.upsert(
        ids=[physical_id], embeddings=[[1.0, 0.0]], documents=["legacy victim"], metadatas=[{"_namespace": "b"}]
    )
    with pytest.raises(VectorStoreError, match="collision"):
        await store.upsert([VectorDocument(id="shared", text="a", embedding=[1.0, 0.0])], namespace="a")
    assert (await store.search([1.0, 0.0], namespace="b"))[0].document.text == "legacy victim"
