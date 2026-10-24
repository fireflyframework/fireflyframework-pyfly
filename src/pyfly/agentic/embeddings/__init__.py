"""Embedding generation: protocol, base class, providers, and similarity utilities."""

from pyfly.agentic.embeddings.base import BaseEmbedder, EmbeddingProtocol
from pyfly.agentic.embeddings.registry import EmbedderRegistry
from pyfly.agentic.embeddings.similarity import (
    cosine_similarity,
    dot_product,
    euclidean_distance,
)
from pyfly.agentic.embeddings.types import EmbeddingResult, EmbeddingUsage

__all__ = [
    "BaseEmbedder",
    "EmbedderRegistry",
    "EmbeddingProtocol",
    "EmbeddingResult",
    "EmbeddingUsage",
    "cosine_similarity",
    "dot_product",
    "euclidean_distance",
]
