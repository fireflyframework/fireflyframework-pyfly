"""Provider SDK parsing with only the outbound request replaced."""

from __future__ import annotations

import pytest


@pytest.mark.parametrize("texts", [["first"], ["first", "second"]])
async def test_google_real_sdk_returns_one_vector_per_text(monkeypatch, texts):
    import json

    import requests

    pytest.importorskip("google.ai.generativelanguage")
    from pyfly.agentic.embeddings.providers.google import GoogleEmbedder

    embedder = GoogleEmbedder(api_key="fictional-offline-key")

    def local_request(method, url, **kwargs):
        payload = json.loads(kwargs["data"])
        assert [item["content"]["parts"][0]["text"] for item in payload["requests"]] == texts
        assert all(item["model"] == "models/text-embedding-004" for item in payload["requests"])
        response = requests.Response()
        response.status_code = 200
        response._content = json.dumps(
            {"embeddings": [{"values": [float(i), 1.0]} for i in range(len(texts))]}
        ).encode()
        return response

    monkeypatch.setattr(embedder._client.transport._session, "request", local_request)
    async with embedder:
        result = await embedder.embed(texts)
    assert result.embeddings == ([[0.0, 1.0]] if len(texts) == 1 else [[0.0, 1.0], [1.0, 1.0]])


async def test_voyage_real_client_parses_embeddings_without_network(monkeypatch):
    voyageai = pytest.importorskip("voyageai")
    from voyageai.api_resources.response import VoyageResponse

    from pyfly.agentic.embeddings.providers.voyage import VoyageEmbedder

    async def local_response(**kwargs):
        assert kwargs["input"] == ["first", "second"]
        assert kwargs["model"] == "voyage-3"
        return VoyageResponse.construct_from(
            {"data": [{"embedding": [0.0, 1.0]}, {"embedding": [1.0, 0.0]}], "usage": {"total_tokens": 2}}
        )

    monkeypatch.setattr(voyageai.Embedding, "acreate", local_response)
    result = await VoyageEmbedder(api_key="test-key").embed(["first", "second"])
    assert result.embeddings == [[0.0, 1.0], [1.0, 0.0]]
