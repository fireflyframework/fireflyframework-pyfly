"""Qdrant vector store backend."""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any
from uuid import NAMESPACE_URL, UUID, uuid5

if TYPE_CHECKING:
    from qdrant_client.models import Condition

try:
    from qdrant_client import AsyncQdrantClient
    from qdrant_client.models import (
        Distance,
        FieldCondition,
        Filter,
        FilterSelector,
        HasIdCondition,
        IsEmptyCondition,
        MatchValue,
        PayloadField,
        PointStruct,
        VectorParams,
    )
except ImportError:
    AsyncQdrantClient = None  # type: ignore[assignment,misc]
    PointStruct = None  # type: ignore[assignment,misc]
    VectorParams = None  # type: ignore[assignment,misc]
    Distance = None  # type: ignore[assignment,misc]
    FieldCondition = None  # type: ignore[assignment,misc]
    Filter = None  # type: ignore[assignment,misc]
    FilterSelector = None  # type: ignore[assignment,misc]
    HasIdCondition = None  # type: ignore[assignment,misc]
    IsEmptyCondition = None  # type: ignore[assignment,misc]
    PayloadField = None  # type: ignore[assignment,misc]
    MatchValue = None  # type: ignore[assignment,misc]

from pyfly.agentic.vectorstores.base import BaseVectorStore
from pyfly.agentic.vectorstores.types import SearchFilter, SearchResult, VectorDocument

logger = logging.getLogger(__name__)


class QdrantVectorStore(BaseVectorStore):
    """Qdrant-backed vector store.

    Parameters:
        collection_name: Qdrant collection name.
        url: Qdrant server URL. Defaults to ``"http://localhost:6333"``.
        api_key: Qdrant API key (for Qdrant Cloud).
        vector_size: Dimension of vectors. Required for collection creation.
        embedder: Optional embedder for auto-embedding.
    """

    def __init__(
        self,
        collection_name: str = "default",
        *,
        url: str = "http://localhost:6333",
        api_key: str | None = None,
        vector_size: int = 1536,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        if AsyncQdrantClient is None:
            raise ImportError(
                "qdrant-client package is required for QdrantVectorStore. Install it with: pip install qdrant-client"
            )
        self._client = AsyncQdrantClient(url=url, api_key=api_key)
        self._collection_name = collection_name
        self._vector_size = vector_size

    async def initialise(self) -> None:
        """Create the collection (cosine distance) if it does not exist. Idempotent."""
        existing = await self._client.get_collections()
        names = {c.name for c in existing.collections}
        if self._collection_name not in names:
            await self._client.create_collection(
                collection_name=self._collection_name,
                vectors_config=VectorParams(size=self._vector_size, distance=Distance.COSINE),
            )

    async def close(self) -> None:
        """Close the underlying client connection."""
        await self._client.close()

    async def _upsert(self, documents: list[VectorDocument], namespace: str) -> None:
        points = []
        for doc in documents:
            if doc.embedding is None:
                raise ValueError("Qdrant upsert requires an embedding for every document")
            points.append(
                PointStruct(
                    id=self._point_id(namespace, doc.id),
                    vector=doc.embedding,
                    payload={**doc.metadata, "text": doc.text, "_namespace": namespace, "_document_id": doc.id},
                )
            )
        # A legacy caller-chosen UUID can coincide with a new physical ID.
        # Refuse that collision instead of overwriting an existing tenant's data.
        expected = {point.id: (point.payload or {})["_document_id"] for point in points}
        existing = await self._client.retrieve(
            collection_name=self._collection_name,
            ids=list(expected),
            with_payload=True,
        )
        for point in existing:
            payload = point.payload or {}
            if payload.get("_namespace") != namespace or payload.get("_document_id") != expected[point.id]:
                raise ValueError("Qdrant point ID collision with legacy data; migrate to a new collection")
        legacy = self._legacy_ids([doc.id for doc in documents])
        if legacy:
            legacy_points = await self._client.retrieve(
                collection_name=self._collection_name,
                ids=[id_ for id_ in legacy],
                with_payload=True,
            )
            for point in legacy_points:
                payload = point.payload or {}
                if payload.get("_namespace") == namespace and "_document_id" in payload:
                    logical_id = payload["_document_id"]
                    if not isinstance(logical_id, str) or self._point_id(namespace, logical_id) != str(point.id):
                        raise ValueError(
                            "Legacy Qdrant reserved metadata requires explicit migration to a new collection"
                        )
        await self._client.upsert(
            collection_name=self._collection_name,
            points=points,
        )
        # Lazy migration: only after the new write succeeds, retire same-tenant
        # legacy IDs. The missing marker protects new points whose physical UUID
        # happens to be another document's caller-supplied logical ID.
        if legacy:
            await self._client.delete(
                collection_name=self._collection_name,
                points_selector=FilterSelector(filter=self._legacy_filter(legacy, namespace)),
            )

    @staticmethod
    def _point_id(namespace: str, logical_id: str) -> str:
        return str(uuid5(NAMESPACE_URL, json.dumps([namespace, logical_id], separators=(",", ":"))))

    @staticmethod
    def _legacy_ids(ids: list[str]) -> list[str | int]:
        legacy: list[str | int] = []
        for logical_id in ids:
            try:
                legacy.append(str(UUID(logical_id)))
            except ValueError:
                if logical_id.isascii() and logical_id.isdecimal() and int(logical_id) < 2**64:
                    legacy.append(int(logical_id))
        return legacy

    @staticmethod
    def _legacy_filter(ids: list[str | int], namespace: str) -> Filter:
        return Filter(
            must=[
                FieldCondition(key="_namespace", match=MatchValue(value=namespace)),
                HasIdCondition(has_id=[id_ for id_ in ids]),
                IsEmptyCondition(is_empty=PayloadField(key="_document_id")),
            ]
        )

    async def _search(
        self,
        query_embedding: list[float],
        top_k: int,
        namespace: str,
        filters: list[SearchFilter] | None,
    ) -> list[SearchResult]:
        must_conditions: list[Condition] = [FieldCondition(key="_namespace", match=MatchValue(value=namespace))]
        if filters:
            for f in filters:
                if f.operator == "eq":
                    must_conditions.append(FieldCondition(key=f.field, match=MatchValue(value=f.value)))

        response = await self._client.query_points(
            collection_name=self._collection_name,
            query=query_embedding,
            limit=top_k,
            query_filter=Filter(must=must_conditions),
        )

        search_results = []
        for point in response.points:
            payload = dict(point.payload or {})
            text = payload.pop("text", "")
            payload.pop("_namespace", None)
            logical_id = payload.pop("_document_id", None)
            if not isinstance(logical_id, str) or self._point_id(namespace, logical_id) != str(point.id):
                logical_id = str(point.id)
            doc = VectorDocument(
                id=logical_id,
                text=text,
                metadata=payload,
                namespace=namespace,
            )
            search_results.append(SearchResult(document=doc, score=point.score))
        return search_results

    async def _delete(self, ids: list[str], namespace: str) -> None:
        # Confine the delete to the namespace AND the requested ids, so a delete
        # can never reach across namespaces even if an id is presented out of
        # scope -- mirrors the namespace filter applied on _search.
        new_points = Filter(
            must=[
                FieldCondition(key="_namespace", match=MatchValue(value=namespace)),
                HasIdCondition(has_id=[self._point_id(namespace, id_) for id_ in ids]),
            ],
            must_not=[IsEmptyCondition(is_empty=PayloadField(key="_document_id"))],
        )
        alternatives: list[Condition] = [new_points]
        legacy = self._legacy_ids(ids)
        if legacy:
            alternatives.append(self._legacy_filter(legacy, namespace))
        await self._client.delete(
            collection_name=self._collection_name,
            points_selector=FilterSelector(filter=Filter(should=alternatives)),
        )
