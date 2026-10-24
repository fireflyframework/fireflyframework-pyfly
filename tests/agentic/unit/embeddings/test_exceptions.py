"""Tests for embedding and vector store exceptions."""

from __future__ import annotations

from pyfly.agentic.exceptions import (
    AgenticError,
    EmbeddingError,
    EmbeddingProviderError,
    VectorStoreConnectionError,
    VectorStoreError,
)


class TestEmbeddingExceptions:
    def test_embedding_error_inherits_base(self):
        assert issubclass(EmbeddingError, AgenticError)

    def test_embedding_provider_error_inherits_embedding(self):
        assert issubclass(EmbeddingProviderError, EmbeddingError)

    def test_vector_store_error_inherits_base(self):
        assert issubclass(VectorStoreError, AgenticError)

    def test_vector_store_connection_inherits(self):
        assert issubclass(VectorStoreConnectionError, VectorStoreError)

    def test_embedding_error_message(self):
        err = EmbeddingError("test error")
        assert str(err) == "test error"
