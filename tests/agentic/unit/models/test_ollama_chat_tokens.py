# Copyright 2026 Firefly Software Foundation
# SPDX-License-Identifier: Apache-2.0
"""Exercise the factory's real SDK serialization without calling a model provider."""

from __future__ import annotations

import json

import httpx
import pytest
from pydantic_ai.messages import ModelRequest, TextPart, ThinkingPart, ToolCallPart, ToolReturnPart, UserPromptPart
from pydantic_ai.models import ModelRequestParameters
from pydantic_ai.output import OutputObjectDefinition
from pydantic_ai.tools import ToolDefinition

from pyfly.agentic.models import ModelFactory, ModelSpec


@pytest.fixture
async def chat_transport(monkeypatch: pytest.MonkeyPatch):
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if json.loads(request.content).get("response_format", {}).get("type") == "json_schema":
            return httpx.Response(
                200,
                json={
                    "id": "native-test",
                    "object": "chat.completion",
                    "created": 1,
                    "model": "qwen3:4b",
                    "choices": [
                        {
                            "index": 0,
                            "finish_reason": "stop",
                            "message": {"role": "assistant", "content": '{"answer":"ready"}'},
                        }
                    ],
                    "usage": {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30},
                },
            )
        return httpx.Response(
            200,
            json={
                "id": "chat-test",
                "object": "chat.completion",
                "created": 1,
                "model": "qwen3:4b",
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "tool_calls",
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "reasoning": "Inspect the evidence",
                            "tool_calls": [
                                {
                                    "id": "tool-test",
                                    "type": "function",
                                    "function": {
                                        "name": "python",
                                        "arguments": '{"code":"print(1)"}',
                                    },
                                }
                            ],
                        },
                    }
                ],
                "usage": {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30},
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        monkeypatch.setattr("pydantic_ai.providers._openai_compatible.create_async_httpx2_client", lambda: client)
        yield requests


@pytest.mark.parametrize("overrides", [{}, {"supports_json_schema_output": False}])
async def test_ollama_factory_sends_supported_token_budget_and_preserves_native_profiles(chat_transport, overrides):
    factory = ModelFactory()
    spec = ModelSpec(
        provider="ollama",
        model="qwen3:4b",
        base_url="https://ollama.example.test/v1",
        settings={"maxTokens": 1500, "extra_body": {"think": False}},
        profile_overrides=overrides,
    )
    model = factory.build_with_secret(spec, None)
    async with model:
        response = await model.request(
            [ModelRequest(parts=[UserPromptPart("Inspect the evidence")])],
            factory.settings_for(spec),
            ModelRequestParameters(
                allow_text_output=False,
                function_tools=[
                    ToolDefinition(
                        name="python",
                        description="Inspect evidence",
                        strict=True,
                        parameters_json_schema={
                            "type": "object",
                            "properties": {"code": {"type": "string"}},
                            "required": ["code"],
                        },
                    )
                ],
            ),
        )
    body = json.loads(chat_transport[0].content)
    assert body.get("max_tokens") == 1500
    assert "max_completion_tokens" not in body
    assert str(chat_transport[0].url) == "https://ollama.example.test/v1/chat/completions"
    assert body["think"] is False
    assert body["tool_choice"] == "auto"
    assert "strict" not in body["tools"][0]["function"]
    assert any(isinstance(part, ThinkingPart) and part.content == "Inspect the evidence" for part in response.parts)
    assert any(isinstance(part, ToolCallPart) and part.tool_name == "python" for part in response.parts)
    assert model.provider is not None and model.provider.name == "ollama"
    assert model.profile["supports_json_schema_output"] is overrides.get("supports_json_schema_output", True)
    assert model.profile["supports_forced_tool_choice"] is False


async def test_explicit_forced_tool_choice_profile_override_takes_precedence(chat_transport):
    factory = ModelFactory()
    model = factory.build_with_secret(
        ModelSpec(
            provider="ollama",
            model="qwen3:4b",
            base_url="https://gateway.example.test/v1",
            profile_overrides={"supports_forced_tool_choice": True},
        ),
        None,
    )
    async with model:
        await model.request(
            [ModelRequest(parts=[UserPromptPart("Inspect")])],
            {"max_tokens": 1000},
            ModelRequestParameters(allow_text_output=False, function_tools=[ToolDefinition(name="python")]),
        )
    assert json.loads(chat_transport[0].content)["tool_choice"] == "required"
    assert model.profile["supports_forced_tool_choice"] is True
    assert model.profile["openai_chat_supports_max_completion_tokens"] is False


async def test_default_ollama_tool_return_history_retains_the_actual_call_id(chat_transport):
    factory = ModelFactory()
    model = factory.build_with_secret(
        ModelSpec(
            provider="ollama",
            model="qwen3:4b",
            base_url="https://ollama.example.test/v1",
        ),
        None,
    )
    parameters = ModelRequestParameters(allow_text_output=False, function_tools=[ToolDefinition(name="python")])
    history = [ModelRequest(parts=[UserPromptPart("Inspect")])]
    async with model:
        response = await model.request(history, {"max_tokens": 1000}, parameters)
        call = next(part for part in response.parts if isinstance(part, ToolCallPart))
        assert call.tool_call_id == "tool-test" and call.tool_name == "python"
        await model.request(
            [*history, response, ModelRequest(parts=[ToolReturnPart("python", "1", call.tool_call_id)])],
            {"max_tokens": 1000},
            parameters,
        )
    bodies = [json.loads(request.content) for request in chat_transport]
    assert all(body["tool_choice"] == "auto" for body in bodies)
    assert bodies[1]["messages"][-1] == {"role": "tool", "tool_call_id": "tool-test", "content": "1"}


async def test_ollama_native_json_contract_and_token_budget_are_preserved(chat_transport):
    factory = ModelFactory()
    model = factory.build_with_secret(
        ModelSpec(
            provider="ollama",
            model="qwen3:4b",
            base_url="https://ollama.example.test/v1",
        ),
        None,
    )
    async with model:
        response = await model.request(
            [ModelRequest(parts=[UserPromptPart("Inspect")])],
            {"max_tokens": 1000},
            ModelRequestParameters(
                output_mode="native",
                allow_text_output=True,
                function_tools=[],
                output_object=OutputObjectDefinition(
                    name="Answer",
                    json_schema={
                        "type": "object",
                        "properties": {"answer": {"type": "string"}},
                        "required": ["answer"],
                        "additionalProperties": False,
                    },
                ),
            ),
        )
    body = json.loads(chat_transport[0].content)
    assert body["response_format"]["type"] == "json_schema"
    assert body["response_format"]["json_schema"]["name"] == "Answer"
    assert body["max_tokens"] == 1000 and "max_completion_tokens" not in body
    assert "tool_choice" not in body and not body.get("tools")
    assert any(
        isinstance(part, TextPart) and json.loads(part.content) == {"answer": "ready"} for part in response.parts
    )


@pytest.mark.parametrize("provider", ["openai", "azure", "local-gateway"])
async def test_other_compatible_providers_still_force_tool_calls(chat_transport, provider):
    factory = ModelFactory()
    model = factory.build_with_secret(
        ModelSpec(
            provider=provider,
            model="gpt-4o",
            base_url="https://test.openai.azure.com" if provider == "azure" else "https://gateway.example.test/v1",
            api_version="2025-04-01-preview" if provider == "azure" else None,
        ),
        "controlled-test-key",
    )
    async with model:
        await model.request(
            [ModelRequest(parts=[UserPromptPart("Inspect")])],
            {"max_tokens": 1000},
            ModelRequestParameters(allow_text_output=False, function_tools=[ToolDefinition(name="python")]),
        )
    body = json.loads(chat_transport[0].content)
    assert body["tool_choice"] == "required" and body["max_completion_tokens"] == 1000


@pytest.mark.parametrize("provider", ["openai", "azure", "local-gateway"])
async def test_other_compatible_providers_keep_their_token_budget_field(chat_transport, provider):
    factory = ModelFactory()
    spec = ModelSpec(
        provider=provider,
        model="gpt-4o",
        base_url="https://test.openai.azure.com" if provider == "azure" else "https://gateway.example.test/v1",
        api_version="2025-04-01-preview" if provider == "azure" else None,
        settings={"maxTokens": 1500},
    )
    model = factory.build_with_secret(spec, "controlled-test-key")
    async with model:
        await model.request(
            [ModelRequest(parts=[UserPromptPart("Inspect the evidence")])],
            factory.settings_for(spec),
            ModelRequestParameters(),
        )
    body = json.loads(chat_transport[0].content)
    assert body["max_completion_tokens"] == 1500
    assert "max_tokens" not in body


async def test_measured_ollama_token_field_override_takes_precedence(chat_transport):
    factory = ModelFactory()
    spec = ModelSpec(
        provider="ollama",
        model="qwen3:4b",
        base_url="https://ollama.example.test/v1",
        settings={"maxTokens": 1500},
        profile_overrides={"openai_chat_supports_max_completion_tokens": True},
    )
    model = factory.build_with_secret(spec, "controlled-test-key")
    async with model:
        await model.request(
            [ModelRequest(parts=[UserPromptPart("Inspect the evidence")])],
            factory.settings_for(spec),
            ModelRequestParameters(),
        )
    body = json.loads(chat_transport[0].content)
    assert body["max_completion_tokens"] == 1500
    assert "max_tokens" not in body
    assert model.profile["openai_chat_thinking_field"] == "reasoning"
