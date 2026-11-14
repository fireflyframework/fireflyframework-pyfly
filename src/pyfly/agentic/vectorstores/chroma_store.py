"""ChromaDB vector store backend."""

from __future__ import annotations

import json
import logging
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any
from uuid import NAMESPACE_URL, uuid5

if TYPE_CHECKING:
    from chromadb.api.types import Metadata, Where  # pyright: ignore[reportMissingImports]

try:
    import chromadb
except ImportError:
    chromadb = None  # type: ignore[assignment]

from pyfly.agentic.vectorstores.base import BaseVectorStore
from pyfly.agentic.vectorstores.types import SearchFilter, SearchResult, VectorDocument

logger = logging.getLogger(__name__)


class ChromaVectorStore(BaseVectorStore):
    """ChromaDB-backed vector store.

    Parameters:
        collection_name: Name of the ChromaDB collection.
        client: Optional pre-configured ChromaDB client. Creates an ephemeral one if not provided.
        embedder: Optional embedder for auto-embedding.
    """

    def __init__(
        self,
        collection_name: str = "default",
        *,
        client: Any | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        if chromadb is None:
            raise ImportError(
                "chromadb package is required for ChromaVectorStore. Install it with: pip install chromadb"
            )
        self._client = client or chromadb.Client()
        self._collection_name = collection_name
        self._collection = self._client.get_or_create_collection(name=collection_name)

    async def _upsert(self, documents: list[VectorDocument], namespace: str) -> None:
        self._reject_legacy_mutation([doc.id for doc in documents], namespace)
        ids = [self._point_id(namespace, doc.id) for doc in documents]
        expected = dict(zip(ids, [doc.id for doc in documents], strict=True))
        existing = self._collection.get(ids=ids, include=["metadatas"])
        for point_id, metadata in zip(existing["ids"], existing["metadatas"] or [], strict=True):
            if (metadata or {}).get("_namespace") != namespace or (metadata or {}).get("_document_id") != expected[
                point_id
            ]:
                raise ValueError("Chroma point ID collision with legacy data; back up and migrate to a new collection")
        embeddings = [doc.embedding for doc in documents]
        texts = [doc.text for doc in documents]
        metadatas: list[Metadata] = [
            {**doc.metadata, "_namespace": namespace, "_document_id": doc.id} for doc in documents
        ]
        self._collection.upsert(
            ids=ids,
            embeddings=embeddings,  # type: ignore[arg-type]
            documents=texts,
            metadatas=metadatas,
        )

    @staticmethod
    def _point_id(namespace: str, logical_id: str) -> str:
        return str(uuid5(NAMESPACE_URL, json.dumps([namespace, logical_id], separators=(",", ":"))))

    def _reject_legacy_mutation(self, ids: list[str], namespace: str) -> None:
        # Chroma cannot atomically delete only records missing a marker. Never
        # race a legacy get/delete migration against new writers, or leave two
        # versions of a document by silently changing its identity.
        existing = self._collection.get(ids=ids, where={"_namespace": namespace}, include=["metadatas"])
        for point_id, metadata in zip(existing["ids"], existing["metadatas"] or [], strict=True):
            logical_id = (metadata or {}).get("_document_id")
            if not isinstance(logical_id, str) or self._point_id(namespace, logical_id) != point_id:
                raise ValueError(
                    "Legacy Chroma data requires explicit migration: back up and reindex into a new collection"
                )

    async def _search(
        self,
        query_embedding: list[float],
        top_k: int,
        namespace: str,
        filters: list[SearchFilter] | None,
    ) -> list[SearchResult]:
        conditions: list[Where] = [{"_namespace": namespace}]
        if filters:
            for f in filters:
                if f.operator == "eq":
                    conditions.append({f.field: f.value})
        where: Where = {"$and": conditions} if len(conditions) > 1 else conditions[0]
        query_embeddings: list[Sequence[float] | Sequence[int]] = [query_embedding]
        results = self._collection.query(
            query_embeddings=query_embeddings,
            n_results=top_k,
            where=where,
        )
        documents = results["documents"]
        metadatas = results["metadatas"]
        distances = results["distances"]
        search_results = []
        if results["ids"] and results["ids"][0]:
            for i, doc_id in enumerate(results["ids"][0]):
                metadata = dict((metadatas[0][i] or {}) if metadatas else {})
                logical_id = metadata.pop("_document_id", None)
                metadata.pop("_namespace", None)
                if not isinstance(logical_id, str) or self._point_id(namespace, logical_id) != doc_id:
                    logical_id = doc_id
                doc = VectorDocument(
                    id=logical_id,
                    text=(documents[0][i] or "") if documents else "",
                    metadata=metadata,
                    namespace=namespace,
                )
                score = 1.0 - (distances[0][i] if distances else 0.0)
                search_results.append(SearchResult(document=doc, score=score))
        return search_results

    async def _delete(self, ids: list[str], namespace: str) -> None:
        self._reject_legacy_mutation(ids, namespace)
        for logical_id in ids:
            self._collection.delete(
                ids=[self._point_id(namespace, logical_id)],
                where={"$and": [{"_namespace": namespace}, {"_document_id": logical_id}]},
            )
