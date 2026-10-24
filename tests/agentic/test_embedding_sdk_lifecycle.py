"""Offline lifecycle qualification against real optional embedding SDK clients."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any, cast

import httpx
import pytest

from pyfly.agentic.config import AgenticConfig
from pyfly.agentic.embeddings.base import BaseEmbedder
from pyfly.agentic.embeddings.providers.cohere import CohereEmbedder
from pyfly.agentic.embeddings.providers.mistral import MistralEmbedder
from pyfly.agentic.runtime import AgenticRuntime


def _owned_http_client(embedder: BaseEmbedder) -> httpx.AsyncClient:
    sdk = cast(Any, embedder)._client
    if isinstance(embedder, CohereEmbedder):
        client: httpx.AsyncClient = sdk._client_wrapper.httpx_client.httpx_client
    else:
        client = sdk.sdk_configuration.async_client
    return client


@pytest.fixture(params=[CohereEmbedder, MistralEmbedder], ids=["cohere", "mistral"])
def sdk_embedder(request: pytest.FixtureRequest) -> BaseEmbedder:
    factory: Callable[..., BaseEmbedder] = request.param
    pytest.importorskip("cohere" if factory is CohereEmbedder else "mistralai")
    return factory(api_key="fictional-offline-key")


@pytest.mark.parametrize("lifetime", ["explicit", "exception", "runtime"])
async def test_owned_sdk_pool_closes_once(
    sdk_embedder: BaseEmbedder, lifetime: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = _owned_http_client(sdk_embedder)
    close = client.aclose
    calls = 0

    async def counted_close() -> None:
        nonlocal calls
        calls += 1
        await close()

    monkeypatch.setattr(client, "aclose", counted_close)
    assert not client.is_closed
    try:
        if lifetime == "exception":
            with pytest.raises(LookupError, match="application failed"):
                async with sdk_embedder:
                    raise LookupError("application failed")
        elif lifetime == "runtime":
            runtime = AgenticRuntime(AgenticConfig())
            runtime.own(sdk_embedder)
            runtime.own(sdk_embedder)
            await asyncio.gather(runtime.aclose(), runtime.aclose())
        else:
            await asyncio.gather(sdk_embedder.aclose(), sdk_embedder.aclose())
        await sdk_embedder.aclose()
        assert client.is_closed
        assert calls == 1
    finally:
        if not client.is_closed:
            await close()


async def test_cancelled_runtime_waiter_preserves_sdk_cleanup(
    sdk_embedder: BaseEmbedder, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = _owned_http_client(sdk_embedder)
    close = client.aclose
    entered = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def delayed_close() -> None:
        nonlocal calls
        calls += 1
        entered.set()
        await release.wait()
        await close()

    monkeypatch.setattr(client, "aclose", delayed_close)
    runtime = AgenticRuntime(AgenticConfig())
    runtime.own(sdk_embedder)
    shutdown = asyncio.create_task(runtime.aclose())
    try:
        await asyncio.wait_for(entered.wait(), timeout=1)
        waiter = asyncio.create_task(runtime.aclose())
        shutdown.cancel()
        with pytest.raises(asyncio.CancelledError):
            await shutdown
        release.set()
        await waiter
        await runtime.aclose()
        assert client.is_closed
        assert calls == 1
    finally:
        release.set()
        await asyncio.gather(shutdown, return_exceptions=True)
        if not client.is_closed:
            await close()


async def test_external_runtime_reference_keeps_sdk_pool_open(sdk_embedder: BaseEmbedder) -> None:
    client = _owned_http_client(sdk_embedder)
    runtime = AgenticRuntime(AgenticConfig())
    runtime.external(sdk_embedder)
    try:
        await runtime.aclose()
        assert not client.is_closed
    finally:
        await sdk_embedder.aclose()
    assert client.is_closed
