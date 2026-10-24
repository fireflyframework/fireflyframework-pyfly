"""Owned native model lifecycles survive runtime stop/start and start retries."""

import asyncio
import os
from urllib.request import urlopen

import httpx2
import pytest

from pyfly.agentic import AgenticConfig, AgenticRuntime


@pytest.fixture
def native_model_transport(monkeypatch):
    import pydantic_ai.providers._openai_compatible as compatible

    clients = []

    def respond(request):
        return httpx2.Response(
            200,
            json={
                "id": "fictional-request",
                "object": "chat.completion",
                "created": 0,
                "model": "fictional",
                "choices": [{"index": 0, "message": {"role": "assistant", "content": "OK"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            },
        )

    def client_factory():
        client = httpx2.AsyncClient(transport=httpx2.MockTransport(respond))
        clients.append(client)
        return client

    monkeypatch.setenv("OPENAI_API_KEY", "fictional")
    monkeypatch.setattr(compatible, "create_async_httpx2_client", client_factory)
    return clients


@pytest.mark.parametrize("synchronous", [False, True])
async def test_native_agent_and_registry_identity_survive_stop_start(native_model_transport, synchronous):
    runtime = AgenticRuntime(AgenticConfig(plugin_auto_discover=False))
    await runtime.start()
    agent = runtime.agent_factory.create("retained-native", model="openai:fictional")

    async def invoke():
        return await asyncio.to_thread(agent.run_sync, "hello") if synchronous else await agent.run("hello")

    try:
        assert (await invoke()).output == "OK"
        first_loop = runtime.sync_worker.loop
        await runtime.aclose()
        assert native_model_transport[0].is_closed
        if synchronous:
            assert first_loop.is_closed()
        await runtime.start()
        assert runtime.agent_registry.get("retained-native") is agent
        assert (await invoke()).output == "OK"
        assert len(native_model_transport) == 2
        assert not native_model_transport[1].is_closed
        if synchronous:
            assert runtime.sync_worker.loop is not first_loop
    finally:
        await runtime.aclose()
    assert all(client.is_closed for client in native_model_transport)


async def test_native_agent_is_reopened_after_partial_start_failure(native_model_transport):
    runtime = AgenticRuntime(AgenticConfig(plugin_auto_discover=False))
    agent = runtime.agent_factory.create("retry-native", model="openai:fictional")
    attempts = 0

    def select_providers():
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("fictional startup failure")
        return None, None

    runtime._provider_selector = select_providers
    with pytest.raises(RuntimeError, match="fictional startup failure"):
        await runtime.start()
    assert native_model_transport[0].is_closed
    try:
        await runtime.start()
        assert runtime.agent_registry.get("retry-native") is agent
        assert (await agent.run("hello")).output == "OK"
        assert len(native_model_transport) == 2
    finally:
        await runtime.aclose()
    assert all(client.is_closed for client in native_model_transport)


@pytest.mark.ollama
@pytest.mark.parametrize("synchronous", [False, True])
async def test_real_ollama_native_agent_survives_runtime_restart(monkeypatch, synchronous):
    base = os.environ.get("PYFLY_OLLAMA_TEST_BASE_URL", "http://127.0.0.1:11434").rstrip("/")
    model = os.environ.get("PYFLY_OLLAMA_TEST_MODEL", "qwen2.5:3b")
    with urlopen(f"{base}/api/version", timeout=5) as response:
        assert response.status == 200
    monkeypatch.setenv("OPENAI_BASE_URL", f"{base}/v1")
    monkeypatch.setenv("OPENAI_API_KEY", "fictional-local-key")
    runtime = AgenticRuntime(AgenticConfig(plugin_auto_discover=False, rate_limit_max_retries=0))
    await runtime.start()
    agent = runtime.agent_factory.create("live-restart-native", model=f"openai:{model}")

    async def invoke():
        kwargs = {"model_settings": {"temperature": 0, "max_tokens": 16, "timeout": 20}}
        if synchronous:
            return await asyncio.to_thread(agent.run_sync, "Reply with exactly: ORCHID", **kwargs)
        return await agent.run("Reply with exactly: ORCHID", **kwargs)

    async with asyncio.timeout(60):
        try:
            assert "ORCHID" in (await invoke()).output
            first_client = agent.agent.model.provider.client._client
            first_loop = runtime.sync_worker.loop
            await runtime.aclose()
            assert first_client.is_closed
            await runtime.start()
            assert runtime.agent_registry.get("live-restart-native") is agent
            assert "ORCHID" in (await invoke()).output
            second_client = agent.agent.model.provider.client._client
            assert second_client is not first_client
            assert not second_client.is_closed
            if synchronous:
                assert first_loop.is_closed()
                assert runtime.sync_worker.loop is not first_loop
        finally:
            await runtime.aclose()
        assert second_client.is_closed


@pytest.mark.parametrize("transfer_ownership", [False, True])
async def test_restart_does_not_rebind_supplied_models_from_closed_external_owners(
    native_model_transport, transfer_ownership
):
    from pydantic_ai.models.openai import OpenAIChatModel
    from pydantic_ai.providers.openai import OpenAIProvider

    runtime = AgenticRuntime(AgenticConfig(plugin_auto_discover=False))
    model = OpenAIChatModel("fictional", provider=OpenAIProvider(api_key="fictional"))
    if transfer_ownership:
        runtime.own(model)
    agent = runtime.agent_factory.create("supplied", model=model)
    await runtime.start()
    assert (await asyncio.to_thread(agent.run_sync, "hello")).output == "OK"
    await runtime.aclose()
    assert native_model_transport[0].is_closed is transfer_ownership
    try:
        await runtime.start()
        with pytest.raises(Exception, match="owning event loop is closed"):
            await agent.run("hello")
        assert runtime.sync_worker.loop is None
    finally:
        await runtime.aclose()
        await native_model_transport[0].aclose()
