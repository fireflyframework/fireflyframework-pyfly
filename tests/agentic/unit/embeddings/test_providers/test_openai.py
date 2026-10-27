"""Tests for the OpenAI embedding provider."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

from pyfly.agentic.embeddings.providers.openai import OpenAIEmbedder
from pyfly.agentic.embeddings.types import EmbeddingResult


class TestOpenAIEmbedder:
    @patch("pyfly.agentic.embeddings.providers.openai.AsyncOpenAI")
    async def test_embed_batch(self, mock_client_cls):
        mock_client = MagicMock()
        mock_client_cls.return_value = mock_client

        mock_response = MagicMock()
        mock_response.data = [
            MagicMock(embedding=[0.1, 0.2, 0.3], index=0),
            MagicMock(embedding=[0.4, 0.5, 0.6], index=1),
        ]
        mock_response.usage = MagicMock(total_tokens=10)
        mock_client.embeddings.create = AsyncMock(return_value=mock_response)

        embedder = OpenAIEmbedder(model="text-embedding-3-small")
        result = await embedder.embed(["hello", "world"])

        assert isinstance(result, EmbeddingResult)
        assert len(result.embeddings) == 2
        assert result.model == "text-embedding-3-small"

    @patch("pyfly.agentic.embeddings.providers.openai.AsyncOpenAI")
    async def test_embed_one(self, mock_client_cls):
        mock_client = MagicMock()
        mock_client_cls.return_value = mock_client

        mock_response = MagicMock()
        mock_response.data = [MagicMock(embedding=[0.1, 0.2], index=0)]
        mock_response.usage = MagicMock(total_tokens=5)
        mock_client.embeddings.create = AsyncMock(return_value=mock_response)

        embedder = OpenAIEmbedder(model="text-embedding-3-small")
        vec = await embedder.embed_one("hello")

        assert isinstance(vec, list)
        assert len(vec) == 2

    @patch("pyfly.agentic.embeddings.providers.openai.AsyncOpenAI")
    def test_default_model(self, mock_client_cls):
        embedder = OpenAIEmbedder()
        assert embedder.model == "text-embedding-3-small"


@patch("pyfly.agentic.embeddings.providers.openai.AsyncOpenAI")
def test_explicit_endpoint_and_credentials_are_forwarded_without_ambient_routing(mock_client_cls, monkeypatch):
    monkeypatch.setenv("OPENAI_BASE_URL", "https://ambient.invalid/v1")
    monkeypatch.setenv("OPENAI_API_KEY", "ambient-test-value")
    OpenAIEmbedder(api_key="explicit-test-value", base_url="http://127.0.0.1:11434/v1")
    mock_client_cls.assert_called_once_with(api_key="explicit-test-value", base_url="http://127.0.0.1:11434/v1")


async def test_real_sdk_uses_explicit_embedding_endpoint_and_closes_its_client(monkeypatch):
    import httpx
    from openai import AsyncOpenAI

    observed = []

    async def respond(request):
        observed.append((str(request.url), request.headers["authorization"]))
        return httpx.Response(
            200,
            json={
                "object": "list",
                "model": "owned-test",
                "data": [{"object": "embedding", "index": 0, "embedding": [0.1, 0.2]}],
                "usage": {"prompt_tokens": 1, "total_tokens": 1},
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(respond))

    def construct(**kwargs):
        return AsyncOpenAI(**kwargs, http_client=client, max_retries=0)

    monkeypatch.setenv("OPENAI_BASE_URL", "https://ambient.invalid/v1")
    monkeypatch.setenv("OPENAI_API_KEY", "ambient-test-value")
    monkeypatch.setattr("pyfly.agentic.embeddings.providers.openai.AsyncOpenAI", construct)
    async with OpenAIEmbedder(api_key="explicit-test-value", base_url="http://127.0.0.1:11434/prefix/v1") as embedder:
        assert await embedder.embed_one("owned fixture") == [0.1, 0.2]
    assert observed == [("http://127.0.0.1:11434/prefix/v1/embeddings", "Bearer explicit-test-value")]
    assert client.is_closed
