# Copyright 2026 Firefly Software Foundation & Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
"""Tests for the Ollama embedding provider."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from pyfly.agentic.embeddings.providers.ollama import OllamaEmbedder
from pyfly.agentic.embeddings.types import EmbeddingResult


class TestOllamaEmbedder:
    @patch("pyfly.agentic.embeddings.providers.ollama.httpx")
    async def test_embed_batch(self, mock_httpx):
        mock_client = MagicMock()
        mock_httpx.AsyncClient.return_value = mock_client

        mock_response = MagicMock()
        mock_response.json.return_value = {"embeddings": [[0.1, 0.2, 0.3], [0.4, 0.5, 0.6]]}
        mock_response.raise_for_status = MagicMock()
        mock_client.post = AsyncMock(return_value=mock_response)

        embedder = OllamaEmbedder()
        result = await embedder.embed(["hello", "world"])

        assert isinstance(result, EmbeddingResult)
        assert len(result.embeddings) == 2
        assert result.model == "nomic-embed-text"

    @patch("pyfly.agentic.embeddings.providers.ollama.httpx")
    async def test_embed_one(self, mock_httpx):
        mock_client = MagicMock()
        mock_httpx.AsyncClient.return_value = mock_client

        mock_response = MagicMock()
        mock_response.json.return_value = {"embeddings": [[0.1, 0.2, 0.3]]}
        mock_response.raise_for_status = MagicMock()
        mock_client.post = AsyncMock(return_value=mock_response)

        embedder = OllamaEmbedder()
        vec = await embedder.embed_one("hello")

        assert isinstance(vec, list)
        assert len(vec) == 3

    @patch("pyfly.agentic.embeddings.providers.ollama.httpx")
    def test_default_model(self, mock_httpx):
        embedder = OllamaEmbedder()
        assert embedder.model == "nomic-embed-text"


@pytest.mark.parametrize("key", [None, "selected-test-credential"])
async def test_selected_endpoint_and_credential_are_forwarded_and_client_is_closed(monkeypatch, key):
    original_client = httpx.AsyncClient
    clients, requests = [], []

    def respond(request):
        requests.append(request)
        return httpx.Response(200, json={"embeddings": [[0.5] * 64]})

    def client(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(respond)
        instance = original_client(*args, **kwargs)
        clients.append(instance)
        return instance

    monkeypatch.setenv("OLLAMA_API_KEY", "ambient-key-must-not-be-used")
    monkeypatch.setenv("OPENAI_API_KEY", "another-provider-key-must-not-be-used")
    monkeypatch.setattr("pyfly.agentic.embeddings.providers.ollama.httpx.AsyncClient", client)
    async with OllamaEmbedder(
        model="selected-model", dimensions=64, base_url="https://selected-ollama.example.test/custom/", api_key=key
    ) as embedder:
        result = await embedder.embed(["Controlled knowledge"])
        assert result.embeddings == [[0.5] * 64]
    assert len(requests) == 1
    assert str(requests[0].url) == "https://selected-ollama.example.test/custom/api/embed"
    assert requests[0].headers.get("Authorization") == ("Bearer " + key if key else None)
    assert clients[0].is_closed
