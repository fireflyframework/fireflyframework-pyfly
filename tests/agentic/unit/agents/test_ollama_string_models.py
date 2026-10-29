# Copyright 2026 Firefly Software Foundation
# SPDX-License-Identifier: Apache-2.0
"""Ollama string selectors retain SDK environment behavior and owned clients."""

from __future__ import annotations

import asyncio
import json

import httpx2
import pytest
from pydantic import BaseModel
from pydantic_ai import NativeOutput
from pydantic_ai.exceptions import UserError
from pydantic_ai.messages import ModelRequest, ToolReturnPart

from pyfly.agentic import AgenticConfig, AgenticRuntime
from pyfly.agentic.agents import Agent
from pyfly.agentic.model_utils import normalize_model
from pyfly.agentic.models import ModelFactory, ModelSpec


class Answer(BaseModel):
    value: str


@pytest.fixture
async def ollama_transport(monkeypatch):
    requests, clients = [], []

    class CountingClient(httpx2.AsyncClient):
        closes = 0

        async def aclose(self):
            self.closes += 1
            await super().aclose()

    def respond(request):
        payload = json.loads(request.content)
        requests.append((request, payload))
        if payload.get("stream"):
            events = [
                {
                    "id": "stream",
                    "object": "chat.completion.chunk",
                    "created": 1,
                    "model": payload["model"],
                    "choices": [
                        {"index": 0, "delta": {"role": "assistant", "content": "ready"}, "finish_reason": None}
                    ],
                },
                {
                    "id": "stream",
                    "object": "chat.completion.chunk",
                    "created": 1,
                    "model": payload["model"],
                    "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
                },
            ]
            data = "".join("data: " + json.dumps(event) + "\n\n" for event in events) + "data: [DONE]\n\n"
            return httpx2.Response(200, content=data.encode(), headers={"content-type": "text/event-stream"})
        tools = [tool["function"]["name"] for tool in payload.get("tools", [])]
        if tools:
            returned = any(message["role"] == "tool" for message in payload["messages"])
            name = next(name for name in tools if name != "read_fixture") if returned else "read_fixture"
            arguments = {"value": "ready"} if returned else {}
            message = {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "output" if returned else "read-id",
                        "type": "function",
                        "function": {"name": name, "arguments": json.dumps(arguments)},
                    }
                ],
            }
            finish = "tool_calls"
        else:
            content = '{"value":"ready"}' if payload.get("response_format") else "ready"
            message, finish = {"role": "assistant", "content": content}, "stop"
        return httpx2.Response(
            200,
            json={
                "id": "owned-test",
                "object": "chat.completion",
                "created": 1,
                "model": payload["model"],
                "choices": [{"index": 0, "message": message, "finish_reason": finish}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            },
        )

    def client_factory():
        client = CountingClient(transport=httpx2.MockTransport(respond), trust_env=False)
        clients.append(client)
        return client

    monkeypatch.setenv("OLLAMA_BASE_URL", "https://ollama.example.test/custom/v1")
    monkeypatch.setenv("OLLAMA_API_KEY", "synthetic-ollama-key")
    monkeypatch.setattr("pydantic_ai.providers._openai_compatible.create_async_httpx2_client", client_factory)
    try:
        yield requests, clients
    finally:
        for client in clients:
            if not client.is_closed:
                await client.aclose()


def config():
    return AgenticConfig(
        plugin_auto_discover=False, native_instrumentation_enabled=False, cost_tracking_enabled=False, max_retries=0
    )


def read_fixture() -> str:
    return "ready"


@pytest.mark.parametrize("factory", [False, True])
async def test_ollama_string_agents_send_real_tools_with_supported_profiles_and_env(ollama_transport, factory):
    requests, clients = ollama_transport
    runtime = AgenticRuntime(config()) if factory else None
    kwargs = {
        "model": "ollama:qwen2.5-coder:14b",
        "output_type": Answer,
        "tools": [read_fixture],
        "model_settings": {"max_tokens": 1000},
        "auto_register": False,
    }
    agent = (
        runtime.agent_factory.create("owned-ollama", **kwargs)
        if runtime
        else Agent("standalone-ollama", config=config(), **kwargs)
    )
    try:
        result = await agent.run("Inspect the fixture")
        assert result.output.value == "ready"
        assert any(
            isinstance(part, ToolReturnPart) and part.tool_call_id == "read-id"
            for message in result.all_messages()
            if isinstance(message, ModelRequest)
            for part in message.parts
        )
        assert all(body["max_tokens"] == 1000 and "max_completion_tokens" not in body for _, body in requests)
        assert all(body["tool_choice"] == "auto" for _, body in requests)
        assert all(
            str(request.url) == "https://ollama.example.test/custom/v1/chat/completions" for request, _ in requests
        )
        assert all(request.headers["authorization"] == "Bearer synthetic-ollama-key" for request, _ in requests)
        assert agent.agent.model.profile["supports_forced_tool_choice"] is False
    finally:
        if runtime:
            await runtime.aclose()
            await runtime.aclose()
            assert len(clients) == 1 and clients[0].closes == 1


@pytest.mark.parametrize("mode", ["run", "stream", "sync"])
async def test_ollama_run_overrides_reuse_owned_model_and_close_once(ollama_transport, mode):
    requests, clients = ollama_transport
    runtime = AgenticRuntime(config())
    agent = runtime.agent_factory.create("override", model="test", auto_register=False)
    try:
        for _ in range(2):
            kwargs = {"model": "ollama:qwen2.5-coder:14b", "model_settings": {"max_tokens": 1000}}
            if mode == "stream":
                async with await agent.run_stream("Inspect", **kwargs) as stream:
                    assert await stream.get_output() == "ready"
            elif mode == "sync":
                assert (await asyncio.to_thread(agent.run_sync, "Inspect", **kwargs)).output == "ready"
            else:
                assert (await agent.run("Inspect", **kwargs)).output == "ready"
        assert len(clients) == 1
        assert all(body["max_tokens"] == 1000 and "max_completion_tokens" not in body for _, body in requests)
        assert agent._model_cache["ollama:qwen2.5-coder:14b"].profile["supports_forced_tool_choice"] is False
    finally:
        await runtime.aclose()
        await runtime.aclose()
    assert clients[0].closes == 1


async def test_ollama_string_native_output_stays_native_json(ollama_transport):
    requests, _ = ollama_transport
    agent = Agent(
        "native",
        model="ollama:qwen2.5-coder:14b",
        config=config(),
        output_type=NativeOutput(Answer),
        model_settings={"max_tokens": 1000},
        auto_register=False,
    )
    assert (await agent.run("Inspect")).output.value == "ready"
    body = requests[0][1]
    assert body["response_format"]["type"] == "json_schema" and "tool_choice" not in body
    assert body["max_tokens"] == 1000 and "max_completion_tokens" not in body


async def test_supplied_ollama_model_identity_profile_and_client_lifetime_are_preserved(ollama_transport):
    requests, clients = ollama_transport
    model = ModelFactory().build_with_secret(
        ModelSpec(
            provider="ollama", model="qwen2.5-coder:14b", profile_overrides={"supports_forced_tool_choice": True}
        ),
        None,
    )
    profile, client = dict(model.profile), model.client
    runtime = AgenticRuntime(config())
    async with model:
        agent = runtime.agent_factory.create(
            "borrowed",
            model=model,
            output_type=Answer,
            tools=[read_fixture],
            model_settings={"max_tokens": 1000},
            auto_register=False,
        )
        try:
            assert (await agent.run("Inspect")).output.value == "ready"
            assert agent.agent.model is model and model.client is client and dict(model.profile) == profile
            assert all(body["tool_choice"] == "required" for _, body in requests)
        finally:
            await runtime.aclose()
            await runtime.aclose()
        assert not clients[0].is_closed and clients[0].closes == 0
    assert clients[0].closes == 1


def test_ollama_string_without_sdk_base_url_still_fails_without_creating_a_client(monkeypatch, ollama_transport):
    _, clients = ollama_transport
    monkeypatch.delenv("OLLAMA_BASE_URL")
    with pytest.raises(UserError, match="OLLAMA_BASE_URL"):
        Agent("missing-url", model="ollama:qwen2.5-coder:14b", config=config(), auto_register=False)
    assert not clients
    assert normalize_model("ollama:qwen2.5-coder:14b") == "ollama:qwen2.5-coder:14b"
