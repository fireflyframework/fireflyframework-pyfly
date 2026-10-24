"""Google credentials and REST request parsing without outbound network access."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest
import requests
from requests.adapters import BaseAdapter

from pyfly.agentic.embeddings.providers.google import GoogleEmbedder
from pyfly.agentic.exceptions import EmbeddingProviderError


@pytest.fixture(autouse=True)
def _require_google_sdk() -> None:
    pytest.importorskip("google.ai.generativelanguage")


class LocalEmbeddingTransport(BaseAdapter):
    def __init__(self, status: int = 200) -> None:
        self.status = status
        self.requests: list[requests.PreparedRequest] = []
        self.close_calls = 0

    def send(self, request: requests.PreparedRequest, *args: Any, **kwargs: Any) -> requests.Response:
        self.requests.append(request)
        response = requests.Response()
        response.status_code = self.status
        response.request = request
        response.headers["Content-Type"] = "application/json"
        payload = json.loads(request.body or "{}")
        body: dict[str, Any]
        if self.status == 200:
            body = {"embeddings": [{"values": [float(i), 1.0]} for i, _ in enumerate(payload["requests"])]}
        else:
            body = {"error": {"code": self.status, "message": "offline rejection", "status": "INVALID_ARGUMENT"}}
        response._content = json.dumps(body).encode()
        return response

    def close(self) -> None:
        self.close_calls += 1


def _attach_transport(embedder: GoogleEmbedder, status: int = 200) -> LocalEmbeddingTransport:
    transport = LocalEmbeddingTransport(status)
    sdk: Any = embedder._client
    sdk.transport._session.mount("https://", transport)
    return transport


@pytest.mark.filterwarnings("ignore::FutureWarning")
async def test_google_instance_keys_remain_isolated_during_interleaved_batches(monkeypatch: pytest.MonkeyPatch) -> None:
    import google.generativeai.client as legacy_client

    def reject_global_configuration(**kwargs: Any) -> None:
        raise AssertionError("Embedding construction must not mutate global SDK credentials")

    monkeypatch.setattr(legacy_client, "configure", reject_global_configuration)
    first = GoogleEmbedder(api_key="fictional-first", batch_size=1)
    second = GoogleEmbedder(api_key="fictional-second")
    first_transport = _attach_transport(first)
    second_transport = _attach_transport(second)
    try:
        first_result, second_result = await asyncio.gather(first.embed(["one", "two"]), second.embed(["three", "four"]))
        assert first_result.embeddings == [[0.0, 1.0], [0.0, 1.0]]
        assert second_result.embeddings == [[0.0, 1.0], [1.0, 1.0]]
        assert first_result.model == "models/text-embedding-004"
        assert first_result.dimensions == 2
        for transport, key, texts in [
            (first_transport, "fictional-first", ["one", "two"]),
            (second_transport, "fictional-second", ["three", "four"]),
        ]:
            assert {request.headers["x-goog-api-key"] for request in transport.requests} == {key}
            assert {(request.url or "").partition("?")[0] for request in transport.requests} == {
                "https://generativelanguage.googleapis.com/v1beta/models/text-embedding-004:batchEmbedContents"
            }
            emitted = [item for request in transport.requests for item in json.loads(request.body or "{}")["requests"]]
            assert [item["model"] for item in emitted] == ["models/text-embedding-004"] * 2
            assert [item["content"]["parts"][0]["text"] for item in emitted] == texts
    finally:
        await asyncio.gather(first.aclose(), second.aclose())
    await first.aclose()
    assert first_transport.close_calls == 1
    assert second_transport.close_calls == 1


@pytest.mark.parametrize("variable", ["GOOGLE_API_KEY", "GEMINI_API_KEY"])
async def test_google_environment_key_is_captured_per_instance(variable: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.setenv(variable, "fictional-environment-first")
    first = GoogleEmbedder()
    monkeypatch.setenv(variable, "fictional-environment-second")
    second = GoogleEmbedder(model="text-embedding-004")
    transports = [_attach_transport(first), _attach_transport(second)]
    try:
        assert await first.embed_one("first") == [0.0, 1.0]
        assert await second.embed_one("second") == [0.0, 1.0]
        assert [transport.requests[0].headers["x-goog-api-key"] for transport in transports] == [
            "fictional-environment-first",
            "fictional-environment-second",
        ]
        assert json.loads(transports[1].requests[0].body or "{}")["requests"][0]["model"] == "models/text-embedding-004"
    finally:
        await asyncio.gather(first.aclose(), second.aclose())


async def test_google_request_failure_is_typed_and_context_closes_owned_transport() -> None:
    embedder = GoogleEmbedder(api_key="fictional-failure")
    transport = _attach_transport(embedder, status=400)
    with pytest.raises(EmbeddingProviderError, match="Google embedding failed"):
        async with embedder:
            await embedder.embed(["offline failure"])
    assert len(transport.requests) == 1
    assert transport.close_calls == 1
