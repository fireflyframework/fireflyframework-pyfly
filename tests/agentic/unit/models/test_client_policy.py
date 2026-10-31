"""Native model factories borrow DI HTTP streams without SDK retry effects or client ownership leaks."""

from __future__ import annotations

import asyncio
import json
import socket

import httpx
import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from pydantic_ai.exceptions import ModelUnavailableError

from pyfly.agentic import AgenticRuntime
from pyfly.agentic.auto_configuration import AgenticAutoConfiguration
from pyfly.agentic.models import (
    Credential,
    CredentialResolver,
    ModelBuildError,
    ModelClientPolicy,
    ModelFactory,
    ModelSpec,
)
from pyfly.client import HttpClientPort, HttpxClientAdapter
from pyfly.container import bean, configuration
from pyfly.context.application_context import ApplicationContext
from pyfly.context.conditions import auto_configuration
from pyfly.core.config import Config
from pyfly.observability.auto_configuration import MeterProviderAutoConfiguration, TracingAutoConfiguration


def config() -> Config:
    return Config(
        {
            "pyfly": {
                "agentic": {"enabled": True, "plugin-auto-discover": False, "cost-tracking-enabled": False},
                "observability": {
                    "tracing": {"register-global": False, "exporter": "none"},
                    "metrics": {"register-global": False, "exporter": "none"},
                },
            }
        }
    )


async def test_zero_sdk_retries_send_one_failed_native_http_request():
    requests = []

    def unavailable(request):
        requests.append(request)
        return httpx.Response(503, json={"error": {"message": "unavailable"}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(unavailable)) as client:
        port = HttpxClientAdapter(client=client)
        factory = ModelFactory(client_policy=ModelClientPolicy(max_retries=0, http_client=port))
        model = await factory.build(
            ModelSpec(
                provider="openai-chat",
                model="model",
                base_url="http://broker.invalid/v1",
                credential=Credential.api_key("claim-capability"),
            )
        )
        from pydantic_ai import Agent

        async with model:
            with pytest.raises(ModelUnavailableError):
                await Agent(model).run("prompt")
        assert len(requests) == 1
        assert requests[0].headers["authorization"] == "Bearer claim-capability"
        assert model.provider.client.is_closed()
        assert not client.is_closed


async def test_di_factory_streams_through_native_port_and_owns_only_sdk_wrappers():
    release = asyncio.Event()
    requests = []
    closes = []

    def frame(delta, finish=None, usage=None):
        return (
            "data: "
            + json.dumps(
                {
                    "id": "response",
                    "object": "chat.completion.chunk",
                    "created": 1,
                    "model": "model",
                    "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
                    **({"usage": usage} if usage else {}),
                }
            )
            + "\n\n"
        ).encode()

    class Wire(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield frame({"role": "assistant", "content": "hello"})
            await release.wait()
            yield frame({"content": " world"})
            yield frame({}, "stop", {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5})
            yield b"data: [DONE]\n\n"

        async def aclose(self):
            closes.append("response")

    def serve(request):
        requests.append(request)
        return httpx.Response(200, headers={"Content-Type": "text/event-stream"}, stream=Wire())

    class OwnedClient(httpx.AsyncClient):
        async def aclose(self):
            closes.append("port-client")
            await super().aclose()

    client = OwnedClient(transport=httpx.MockTransport(serve), headers={"Host": "admitted.worker"})

    class Resolver:
        async def resolve(self, reference, version):
            assert (reference, version) == ("claim", 7)
            return "resolved-claim"

    @configuration
    class Infrastructure:
        @bean
        def http(self) -> HttpClientPort:
            return HttpxClientAdapter(client=client, owns_client=True)

        @bean
        def resolver(self) -> CredentialResolver:
            return Resolver()

        @bean
        def policy(self, http: HttpClientPort) -> ModelClientPolicy:
            return ModelClientPolicy(max_retries=0, http_client=http, timeout=120, connect_timeout=10)

    context = ApplicationContext(
        config(),
        auto_configurations=[AgenticAutoConfiguration, TracingAutoConfiguration, MeterProviderAutoConfiguration],
    )
    context.register_bean(Infrastructure)
    await context.start()
    exported = InMemorySpanExporter()
    context.get_bean(trace.TracerProvider).add_span_processor(SimpleSpanProcessor(exported))
    try:
        factory = context.get_bean(ModelFactory)
        runtime = context.get_bean(AgenticRuntime)
        async with runtime.scope() as operation:
            model = operation.own(
                await factory.build(
                    ModelSpec(
                        provider="openai-chat",
                        model="model",
                        base_url="http://broker.invalid/v1",
                        credential=Credential.reference("claim", 7),
                    )
                )
            )
            agent = operation.agent_factory.create("native", model=model, auto_register=False, default_middleware=False)
            async with await agent.run_stream("private prompt") as stream:
                chunks = stream.stream_text(delta=True, debounce_by=None)
                first = await asyncio.wait_for(anext(chunks), timeout=2)
                assert first == "hello" and not release.is_set()
                release.set()
                rest = "".join([chunk async for chunk in chunks])
                assert first + rest == "hello world"
                assert await stream.get_output() == "hello world"
            assert not client.is_closed
        assert model.provider.client.is_closed()
        assert not client.is_closed
        assert len(requests) == 1 and closes.count("response") == 1
        assert requests[0].headers["authorization"] == "Bearer resolved-claim"
        assert requests[0].headers["host"] == "admitted.worker"
        assert requests[0].extensions["timeout"]["connect"] == 10
        chat = next(s for s in exported.get_finished_spans() if s.attributes.get("gen_ai.operation.name") == "chat")
        assert requests[0].headers["traceparent"].split("-")[2] == format(chat.context.span_id, "016x")
        assert chat.attributes["gen_ai.usage.input_tokens"] == 3
        assert chat.attributes["gen_ai.usage.output_tokens"] == 2
        assert "private prompt" not in "".join(s.to_json() for s in exported.get_finished_spans())
    finally:
        release.set()
        await context.stop()
    assert closes.count("port-client") == 1


async def test_default_di_factory_without_credential_resolver_refuses_references():
    context = ApplicationContext(config(), auto_configurations=[AgenticAutoConfiguration])
    await context.start()
    try:
        factory = context.get_bean(ModelFactory)
        with pytest.raises(ModelBuildError, match="no resolver"):
            await factory.build(
                ModelSpec(provider="openai-chat", model="model", credential=Credential.reference("missing"))
            )
    finally:
        await context.stop()


@pytest.mark.parametrize("provider", ["anthropic", "bedrock", "google", "azure"])
async def test_unsupported_provider_client_policy_fails_explicitly(provider):
    factory = ModelFactory(client_policy=ModelClientPolicy(max_retries=0))
    with pytest.raises(ModelBuildError, match="client policy"):
        await factory.build(ModelSpec(provider=provider, model="model", credential=Credential.api_key("test")))


@pytest.mark.parametrize("retries", [-1, True, 1.5])
def test_sdk_retry_policy_rejects_invalid_values(retries):
    with pytest.raises(ValueError, match="non-negative integer"):
        ModelClientPolicy(max_retries=retries)


def test_connect_timeout_requires_native_transport():
    with pytest.raises(ValueError, match="connect_timeout requires"):
        ModelClientPolicy(connect_timeout=3)


async def test_di_optional_model_policy_from_later_auto_configuration_never_uses_network(monkeypatch):
    requests = []

    def no_network(*args, **kwargs):
        raise AssertionError("The configured native model port must handle every request")

    def answer(request):
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "id": "response",
                "object": "chat.completion",
                "created": 1,
                "model": "model",
                "choices": [
                    {"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": "answer"}}
                ],
                "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
            },
        )

    monkeypatch.setattr(socket, "getaddrinfo", no_network)
    async with httpx.AsyncClient(transport=httpx.MockTransport(answer)) as client:

        @configuration
        class UserPolicy:
            @bean
            def policy(self, http: HttpClientPort) -> ModelClientPolicy:
                return ModelClientPolicy(max_retries=0, http_client=http)

        @auto_configuration
        class LateHttp:
            @bean
            def http(self) -> HttpClientPort:
                return HttpxClientAdapter(client=client)

        context = ApplicationContext(config(), auto_configurations=[AgenticAutoConfiguration, LateHttp])
        context.register_bean(UserPolicy)
        try:
            await context.start()
            model = await context.get_bean(ModelFactory).build(
                ModelSpec(
                    provider="openai-chat",
                    model="model",
                    base_url="http://controlled-port.invalid/v1",
                    credential=Credential.api_key("test-capability"),
                )
            )
            from pydantic_ai import Agent

            async with model:
                assert (await Agent(model).run("question")).output == "answer"
            assert len(requests) == 1
            assert str(requests[0].url) == "http://controlled-port.invalid/v1/chat/completions"
            assert requests[0].headers["authorization"] == "Bearer test-capability"
            assert model.provider.client.is_closed()
            assert not client.is_closed
        finally:
            await context.stop()


async def test_ollama_policy_preserves_native_profile_and_does_not_borrow_openai_credentials(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "unrelated-cloud-key")
    monkeypatch.delenv("OLLAMA_API_KEY", raising=False)
    monkeypatch.setenv("OLLAMA_BASE_URL", "http://ollama.invalid/v1")
    requests = []

    def answer(request):
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "id": "response",
                "object": "chat.completion",
                "created": 1,
                "model": "qwen3:4b",
                "choices": [
                    {"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": "answer"}}
                ],
                "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(answer)) as client:
        factory = ModelFactory(client_policy=ModelClientPolicy(http_client=HttpxClientAdapter(client=client)))
        model = await factory.build(ModelSpec(provider="ollama", model="qwen3:4b"))
        from pydantic_ai import Agent

        async with model:
            assert (await Agent(model).run("question")).output == "answer"
        assert model.provider.name == "ollama"
        assert model.profile["supports_forced_tool_choice"] is False
        assert model.profile["openai_chat_thinking_field"] == "reasoning"
        assert requests[0].headers["authorization"] == "Bearer api-key-not-set"
        assert str(requests[0].url) == "http://ollama.invalid/v1/chat/completions"
