"""Pluggable vector storage and retrieval backends."""

from pyfly.agentic.vectorstores.base import BaseVectorStore, VectorStoreProtocol
from pyfly.agentic.vectorstores.chroma_store import ChromaVectorStore
from pyfly.agentic.vectorstores.memory_store import InMemoryVectorStore
from pyfly.agentic.vectorstores.pgvector_store import PgVectorVectorStore
from pyfly.agentic.vectorstores.pinecone_store import PineconeVectorStore
from pyfly.agentic.vectorstores.qdrant_store import QdrantVectorStore
from pyfly.agentic.vectorstores.registry import VectorStoreRegistry
from pyfly.agentic.vectorstores.scoped import (
    ScopedVectorStore,
    TenantScopedVectorStore,
    parse_scope_namespace,
    scope_namespace,
)
from pyfly.agentic.vectorstores.sqlite_vec_store import SqliteVecVectorStore
from pyfly.agentic.vectorstores.types import SearchFilter, SearchResult, VectorDocument

__all__ = [
    "BaseVectorStore",
    "ChromaVectorStore",
    "InMemoryVectorStore",
    "PgVectorVectorStore",
    "PineconeVectorStore",
    "QdrantVectorStore",
    "ScopedVectorStore",
    "SearchFilter",
    "SearchResult",
    "SqliteVecVectorStore",
    "TenantScopedVectorStore",
    "VectorDocument",
    "VectorStoreProtocol",
    "VectorStoreRegistry",
    "parse_scope_namespace",
    "scope_namespace",
]
