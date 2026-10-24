"""Google (Generative AI) embedding provider."""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

try:
    from google.ai import generativelanguage as glm
except ImportError:
    glm = None  # type: ignore[assignment]

from pyfly.agentic.embeddings.base import BaseEmbedder
from pyfly.agentic.exceptions import EmbeddingProviderError

logger = logging.getLogger(__name__)


class GoogleEmbedder(BaseEmbedder):
    """Google Generative AI embedding provider.

    Parameters:
        model: Google model. Defaults to ``"models/text-embedding-004"``.
        api_key: Google API key. Falls back to ``GEMINI_API_KEY`` then
            ``GOOGLE_API_KEY`` environment variables.
    """

    def __init__(
        self,
        model: str = "models/text-embedding-004",
        dimensions: int | None = None,
        *,
        api_key: str | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(model=model, dimensions=dimensions, **kwargs)
        if glm is None:
            raise ImportError(
                "The 'google-generativeai' package is required for GoogleEmbedder. "
                "Install it with: pip install google-generativeai"
            )
        key = api_key or os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
        self._client = glm.GenerativeServiceClient(transport="rest", client_options={"api_key": key})

    async def _embed_batch(self, texts: list[str], **kwargs: Any) -> list[list[float]]:
        """Embed a single batch using this adapter's credentials and transport."""
        try:
            model = self._model if "/" in self._model else f"models/{self._model}"
            request = glm.BatchEmbedContentsRequest(
                model=model,
                requests=[
                    glm.EmbedContentRequest(model=model, content=glm.Content(parts=[glm.Part(text=text)]))
                    for text in texts
                ],
            )
            result = await asyncio.to_thread(self._client.batch_embed_contents, request=request)
            return [list(embedding.values) for embedding in result.embeddings]
        except Exception as exc:
            raise EmbeddingProviderError(f"Google embedding failed: {exc}") from exc
