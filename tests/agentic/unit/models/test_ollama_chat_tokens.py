"""Exercise the factory's real SDK serialization without calling a model provider."""

from __future__ import annotations

import json

import httpx
import pytest
from pydantic_ai.messages import ModelRequest, ThinkingPart, ToolCallPart, UserPromptPart
from pydantic_ai.models import ModelRequestParameters
from pydantic_ai.tools import ToolDefinition

from pyfly.agentic.models import ModelFactory, ModelSpec


@pytest.fixture
async def chat_transport(monkeypatch: pytest.MonkeyPatch):
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
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
    assert body["tool_choice"] == "required"
    assert "strict" not in body["tools"][0]["function"]
    assert any(isinstance(part, ThinkingPart) and part.content == "Inspect the evidence" for part in response.parts)
    assert any(isinstance(part, ToolCallPart) and part.tool_name == "python" for part in response.parts)
    assert model.provider is not None and model.provider.name == "ollama"
    assert model.profile["supports_json_schema_output"] is overrides.get("supports_json_schema_output", True)


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
