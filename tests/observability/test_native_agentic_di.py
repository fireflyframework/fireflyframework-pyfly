"""Real native Agentic telemetry through selected DI beans and their owning lifecycle."""

from __future__ import annotations

import builtins
import queue
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest
from opentelemetry import metrics, trace
from opentelemetry.proto.collector.metrics.v1.metrics_service_pb2 import ExportMetricsServiceRequest
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest
from pydantic_ai.messages import ModelResponse, TextPart
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.usage import RequestUsage

from pyfly.agentic import Agent, AgenticRuntime
from pyfly.agentic.auto_configuration import AgenticAutoConfiguration
from pyfly.container import bean, configuration
from pyfly.context.application_context import ApplicationContext
from pyfly.core.config import Config
from pyfly.observability.auto_configuration import MeterProviderAutoConfiguration, TracingAutoConfiguration


@pytest.mark.parametrize("agentic_first", [True, False])
async def test_native_di_providers_export_real_spans_and_metrics_and_close_once(monkeypatch, agentic_first):
    received: queue.Queue[tuple[str, str | None, bytes]] = queue.Queue()

    class Receiver(BaseHTTPRequestHandler):
        def do_POST(self):
            received.put(
                (self.path, self.headers.get("Authorization"), self.rfile.read(int(self.headers["Content-Length"])))
            )
            self.send_response(200)
            self.end_headers()

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Receiver)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    endpoint = f"http://127.0.0.1:{server.server_port}"
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_TRACES_HEADERS", "Authorization=Bearer%20trace-test")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_METRICS_HEADERS", "Authorization=Bearer%20metric-test")
    before = trace.get_tracer_provider(), metrics.get_meter_provider()
    selections = [AgenticAutoConfiguration, TracingAutoConfiguration, MeterProviderAutoConfiguration]
    if not agentic_first:
        selections.reverse()
    context = ApplicationContext(
        Config(
            {
                "pyfly": {
                    "app": {"name": "native-di"},
                    "agentic": {"enabled": True, "plugin-auto-discover": False, "cost-tracking-enabled": False},
                    "observability": {
                        "tracing": {"register-global": False, "exporter": "otlp", "otlp": {"endpoint": endpoint}},
                        "metrics": {
                            "register-global": False,
                            "exporter": "otlp",
                            "otlp": {"endpoint": endpoint + "/v1/metrics"},
                        },
                    },
                }
            }
        ),
        auto_configurations=selections,
    )
    constructed_with: list[Any] = []

    def answer(messages: Any, info: Any) -> ModelResponse:
        return ModelResponse(parts=[TextPart("private output")], usage=RequestUsage(input_tokens=5, output_tokens=2))

    @configuration
    class EarlyAgent:
        @bean
        def worker(self, runtime: AgenticRuntime) -> Agent[Any, Any]:
            constructed_with.append(runtime.tracer_provider)
            return Agent("worker", model=FunctionModel(answer), runtime=runtime, auto_register=False)

    context.register_bean(EarlyAgent)
    stopped = []
    try:
        await context.start()
        runtime = context.get_bean(AgenticRuntime)
        tracer = context.get_bean(trace.TracerProvider)
        meter = context.get_bean(metrics.MeterProvider)
        assert runtime.tracer_provider is tracer and runtime.meter_provider is meter
        assert runtime._started
        if agentic_first:
            assert constructed_with == [None], "Regression must exercise late provider selection"
        agent = context.get_bean_by_name("worker")
        with runtime.tracer.custom_span("request") as parent:
            result = await agent.run("private prompt")
        assert result.output == "private output"
        assert tracer.force_flush(timeout_millis=5000)
        assert meter.force_flush(timeout_millis=5000)
        batches = []
        while not received.empty():
            batches.append(received.get_nowait())
        traces, metric_names = [], []
        for path, authority, payload in batches:
            if path == "/v1/traces":
                assert authority == "Bearer trace-test"
                batch = ExportTraceServiceRequest.FromString(payload)
                traces.extend(s for r in batch.resource_spans for scope in r.scope_spans for s in scope.spans)
                assert all(
                    any(a.key == "service.name" and a.value.string_value == "native-di" for a in r.resource.attributes)
                    for r in batch.resource_spans
                )
                assert b"private prompt" not in payload and b"private output" not in payload
            elif path == "/v1/metrics":
                assert authority == "Bearer metric-test"
                metric_batch = ExportMetricsServiceRequest.FromString(payload)
                metric_names.extend(
                    m.name for r in metric_batch.resource_metrics for s in r.scope_metrics for m in s.metrics
                )
        assert len(traces) == 4
        assert all(int.from_bytes(s.trace_id) == parent.get_span_context().trace_id for s in traces)
        assert "gen_ai.client.token.usage" in metric_names
        assert (trace.get_tracer_provider(), metrics.get_meter_provider()) == before
        for provider in (tracer, meter):
            original = provider.shutdown

            def shutdown(*args, _original=original, _provider=provider, **kwargs):
                stopped.append(id(_provider))
                return _original(*args, **kwargs)

            monkeypatch.setattr(provider, "shutdown", shutdown)
        await context.stop()
        await context.stop()
        assert stopped.count(id(tracer)) == stopped.count(id(meter)) == 1
        assert runtime._closed
    finally:
        await context.stop()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


async def test_metrics_none_uses_di_provider_without_ambient_otlp_export(monkeypatch):
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://ambient.invalid:4318")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_METRICS_ENDPOINT", "http://ambient.invalid:4318/v1/metrics")
    context = ApplicationContext(
        Config({"pyfly": {"observability": {"metrics": {"register-global": False, "exporter": "none"}}}}),
        auto_configurations=[MeterProviderAutoConfiguration],
    )
    await context.start()
    try:
        provider = context.get_bean(metrics.MeterProvider)
        assert not provider._metric_readers
    finally:
        await context.stop()


@pytest.mark.parametrize("signal", ["tracing", "metrics"])
async def test_explicit_otlp_without_exporter_fails_context_start_clearly(monkeypatch, signal):
    original_import = builtins.__import__

    def without_exporter(name, *args, **kwargs):
        if name.startswith("opentelemetry.exporter.otlp"):
            raise ImportError("Deliberately unavailable optional exporter")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", without_exporter)
    selected = TracingAutoConfiguration if signal == "tracing" else MeterProviderAutoConfiguration
    context = ApplicationContext(
        Config(
            {
                "pyfly": {
                    "observability": {
                        signal: {
                            "register-global": False,
                            "exporter": "otlp",
                            "otlp": {"endpoint": "http://collector:4318"},
                        }
                    }
                }
            }
        ),
        auto_configurations=[selected],
    )
    with pytest.raises(Exception, match="opentelemetry-exporter-otlp"):
        await context.start()
    await context.stop()


async def test_di_owned_runtime_scopes_release_per_operation_models_without_closing_shared_providers():
    import httpx
    from openai import AsyncOpenAI
    from pydantic_ai.models.openai import OpenAIChatModel
    from pydantic_ai.providers.openai import OpenAIProvider

    from pyfly.agentic._runtime_context import selected_runtime

    context = ApplicationContext(
        Config(
            {
                "pyfly": {
                    "agentic": {"enabled": True, "plugin-auto-discover": False, "cost-tracking-enabled": False},
                    "observability": {
                        "tracing": {"register-global": False, "exporter": "none"},
                        "metrics": {"register-global": False, "exporter": "none"},
                    },
                }
            }
        ),
        auto_configurations=[AgenticAutoConfiguration, TracingAutoConfiguration, MeterProviderAutoConfiguration],
    )
    await context.start()
    parent = context.get_bean(AgenticRuntime)
    tracer, meter = context.get_bean(trace.TracerProvider), context.get_bean(metrics.MeterProvider)

    def respond(request):
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

    try:
        for _ in range(3):
            async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
                model = OpenAIChatModel(
                    "model",
                    provider=OpenAIProvider(
                        openai_client=AsyncOpenAI(
                            base_url="http://model.invalid/v1", api_key="test-scope", http_client=client
                        )
                    ),
                )
                async with parent.scope() as scoped:
                    assert selected_runtime() is scoped
                    assert scoped.tracer_provider is tracer and scoped.meter_provider is meter
                    assert scoped.usage_tracker is parent.usage_tracker
                    assert scoped.memory is parent.memory
                    agent = scoped.agent_factory.create("scoped", model=model, auto_register=False)
                    assert (await agent.run("question")).output == "answer"
                    assert len(scoped._model_states) == 1
                assert scoped._closed
                assert scoped not in parent._resources.values()
                assert not parent._model_states
                assert not client.is_closed
                assert not meter._shutdown
        assert selected_runtime() is None
    finally:
        await context.stop()
