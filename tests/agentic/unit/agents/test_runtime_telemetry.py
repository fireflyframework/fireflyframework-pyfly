"""Native model telemetry must follow the application-owned PyFly providers."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import pytest
from opentelemetry import trace
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from pydantic_ai.messages import ModelResponse, TextPart, ThinkingPart
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.usage import RequestUsage

from pyfly.agentic import Agent
from pyfly.agentic.agents.context import AgentContext
from pyfly.agentic.config import AgenticConfig
from pyfly.agentic.runtime import AgenticRuntime


def _providers(name: str) -> tuple[TracerProvider, InMemorySpanExporter, MeterProvider, InMemoryMetricReader]:
    exporter = InMemorySpanExporter()
    tracer = TracerProvider(resource=Resource.create({"service.name": name}), shutdown_on_exit=False)
    tracer.add_span_processor(SimpleSpanProcessor(exporter))
    reader = InMemoryMetricReader()
    return tracer, exporter, MeterProvider(metric_readers=[reader], shutdown_on_exit=False), reader


def _config() -> AgenticConfig:
    return AgenticConfig(plugin_auto_discover=False, cost_tracking_enabled=False, max_retries=0)


def _response(messages: Any, info: Any) -> ModelResponse:
    return ModelResponse(
        parts=[ThinkingPart("private reasoning"), TextPart("answer")],
        usage=RequestUsage(
            input_tokens=11, output_tokens=7, output_reasoning_tokens=3, details={"reasoning_tokens": 3}
        ),
    )


@pytest.mark.asyncio
async def test_late_provider_binding_moves_native_spans_and_metrics_without_global_registration():
    old, old_exporter, old_meter, old_reader = _providers("old")
    new, exporter, meter, reader = _providers("bound-pyfly")
    global_before = trace.get_tracer_provider()
    runtime = AgenticRuntime(_config(), tracer_provider=old, meter_provider=old_meter)
    agent = Agent("bound", model=FunctionModel(_response), runtime=runtime, auto_register=False)
    try:
        runtime.bind_providers(new, meter)
        with runtime.tracer.custom_span("workload", request_id="request-1") as parent:
            result = await agent.run("private prompt", context=AgentContext(correlation_id="request-1"))
        assert result.output == "answer"
        spans = exporter.get_finished_spans()
        assert {s.name for s in spans} >= {"workload", "agent.bound"}
        chats = [s for s in spans if s.attributes.get("gen_ai.operation.name") == "chat"]
        assert len(chats) == 1
        chat = chats[0]
        assert chat.attributes["gen_ai.usage.input_tokens"] == 11
        assert chat.attributes["gen_ai.usage.output_tokens"] == 7
        assert chat.attributes["gen_ai.usage.details.reasoning_tokens"] == 3
        assert all(s.context.trace_id == parent.get_span_context().trace_id for s in spans)
        assert all(s.resource.attributes["service.name"] == "bound-pyfly" for s in spans)
        by_id = {s.context.span_id: s for s in spans}
        assert by_id[chat.parent.span_id].attributes["gen_ai.operation.name"] == "invoke_agent"
        invoke = by_id[chat.parent.span_id]
        assert by_id[invoke.parent.span_id].name == "agent.bound"
        assert by_id[invoke.parent.span_id].parent.span_id == parent.get_span_context().span_id
        assert old_exporter.get_finished_spans() == ()
        metrics = reader.get_metrics_data()
        assert metrics is not None
        native = [
            m
            for r in metrics.resource_metrics
            for s in r.scope_metrics
            for m in s.metrics
            if m.name == "gen_ai.client.token.usage"
        ]
        assert len(native) == 1
        assert sorted(point.sum for point in native[0].data.data_points) == [7, 11]
        assert old_reader.get_metrics_data() is None
        assert trace.get_tracer_provider() is global_before
    finally:
        await runtime.aclose()
        old.shutdown()
        new.shutdown()
        old_meter.shutdown()
        meter.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["buffered", "incremental"])
async def test_stream_startup_failure_ends_framework_span_and_restores_parent(mode: str):
    provider, exporter, meter, _ = _providers("stream")
    runtime = AgenticRuntime(_config(), tracer_provider=provider, meter_provider=meter)

    async def fail(messages: Any, info: Any) -> AsyncIterator[str]:
        raise RuntimeError("stream startup failed")
        yield "unreachable"

    agent = Agent("stream-error", model=FunctionModel(stream_function=fail), runtime=runtime, auto_register=False)
    try:
        with runtime.tracer.custom_span("parent") as parent:
            with pytest.raises(RuntimeError, match="stream startup failed"):
                async with await agent.run_stream("prompt", streaming_mode=mode):
                    pass
            assert trace.get_current_span().get_span_context() == parent.get_span_context()
        spans = exporter.get_finished_spans()
        framework = [s for s in spans if s.name == "agent.stream-error"]
        assert len(framework) == 1
        assert framework[0].status.status_code is trace.StatusCode.ERROR
        assert not runtime._streams
    finally:
        await runtime.aclose()
        provider.shutdown()
        meter.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["buffered", "incremental"])
@pytest.mark.parametrize("timeout", [None, 1.0])
async def test_native_stream_finalizes_under_workload_parent(mode: str, timeout: float | None):
    provider, exporter, meter, _ = _providers("stream")
    runtime = AgenticRuntime(_config(), tracer_provider=provider, meter_provider=meter)

    async def chunks(messages: Any, info: Any) -> AsyncIterator[str]:
        yield "one"
        await asyncio.sleep(0)
        yield " two"

    agent = Agent("stream", model=FunctionModel(stream_function=chunks), runtime=runtime, auto_register=False)
    try:
        with runtime.tracer.custom_span("workload") as parent:
            async with await agent.run_stream("private prompt", streaming_mode=mode, timeout=timeout) as stream:
                assert not exporter.get_finished_spans()
                if mode == "incremental":
                    output = "".join([token async for token in stream.stream_tokens()])
                else:
                    output = "".join([token async for token in stream.stream_text(delta=True, debounce_by=None)])
                assert output == "one two"
                assert await stream.get_output() == "one two"
            assert trace.get_current_span().get_span_context() == parent.get_span_context()
        spans = exporter.get_finished_spans()
        assert len([s for s in spans if s.attributes.get("gen_ai.operation.name") == "chat"]) == 1
        assert all(s.context.trace_id == parent.get_span_context().trace_id for s in spans)
        assert len([s for s in spans if s.name == "agent.stream"]) == 1
        assert "private prompt" not in str([s.attributes for s in spans])
        assert not runtime._streams
    finally:
        await runtime.aclose()
        provider.shutdown()
        meter.shutdown()


@pytest.mark.asyncio
async def test_default_failure_telemetry_withholds_credentials_and_counts_one_error():
    provider, exporter, meter, reader = _providers("privacy")
    runtime = AgenticRuntime(_config(), tracer_provider=provider, meter_provider=meter)
    secret = "Bearer credential-should-not-export"

    def fail(messages: Any, info: Any) -> ModelResponse:
        raise RuntimeError(secret)

    agent = Agent("private-error", model=FunctionModel(fail), runtime=runtime, auto_register=False)
    try:
        with pytest.raises(RuntimeError, match="credential-should-not-export"):
            await agent.run("prompt-containing-api-key")
        spans = exporter.get_finished_spans()
        assert len(spans) == 3
        assert all(s.status.status_code is trace.StatusCode.ERROR for s in spans)
        payload = " ".join(s.to_json() for s in spans)
        assert "credential-should-not-export" not in payload
        assert "prompt-containing-api-key" not in payload
        metric_data = reader.get_metrics_data()
        assert metric_data is not None
        errors = [
            m
            for r in metric_data.resource_metrics
            for scope in r.scope_metrics
            for m in scope.metrics
            if m.name == "pyfly.agentic.errors.total"
        ]
        assert len(errors) == 1
        assert sum(p.value for p in errors[0].data.data_points) == 1
        assert errors[0].data.data_points[0].attributes["error_type"] == "RuntimeError"
    finally:
        await runtime.aclose()
        provider.shutdown()
        meter.shutdown()


def test_workload_span_error_does_not_export_credentials_by_default():
    from pyfly.agentic.observability.tracer import AgentTracer

    provider, exporter, meter, _ = _providers("workload")
    tracer = AgentTracer(tracer_provider=provider)
    try:
        with pytest.raises(RuntimeError, match="secret-password"), tracer.custom_span("workload"):
            raise RuntimeError("secret-password")
        span = exporter.get_finished_spans()[0]
        assert span.status.status_code is trace.StatusCode.ERROR
        assert span.status.description is None
        assert span.events[0].attributes["exception.type"] == "RuntimeError"
        assert "secret-password" not in span.to_json()
    finally:
        provider.shutdown()
        meter.shutdown()


@pytest.mark.asyncio
async def test_reasoning_uses_native_model_spans_under_reasoning_run():
    from pydantic_ai.messages import ToolCallPart

    from pyfly.agentic.reasoning.chain_of_thought import ChainOfThoughtPattern

    provider, exporter, meter, _ = _providers("reasoning")
    runtime = AgenticRuntime(_config(), tracer_provider=provider, meter_provider=meter)

    def thought(messages: Any, info: Any) -> ModelResponse:
        return ModelResponse(
            parts=[
                ToolCallPart(
                    info.output_tools[0].name,
                    {"content": "private thought", "is_final": True, "final_answer": "solved"},
                )
            ],
            usage=RequestUsage(input_tokens=9, output_tokens=5),
        )

    agent = Agent("reasoner", model=FunctionModel(thought), runtime=runtime, auto_register=False)
    try:
        with runtime.tracer.custom_span("workload") as parent:
            result = await agent.run_with_reasoning(ChainOfThoughtPattern(max_steps=1), "private problem")
        assert result.output == "solved"
        spans = exporter.get_finished_spans()
        assert all(s.context.trace_id == parent.get_span_context().trace_id for s in spans)
        reasoning = next(s for s in spans if s.attributes.get("pyfly.agentic.agent.method") == "run_with_reasoning")
        chat = next(s for s in spans if s.attributes.get("gen_ai.operation.name") == "chat")
        by_id = {s.context.span_id: s for s in spans}
        current = chat
        ancestors = []
        while current.parent is not None:
            current = by_id[current.parent.span_id]
            ancestors.append(current.context.span_id)
        assert reasoning.context.span_id in ancestors
        assert "private thought" not in str([s.attributes for s in spans])
        assert "private problem" not in str([s.attributes for s in spans])
    finally:
        await runtime.aclose()
        provider.shutdown()
        meter.shutdown()


@pytest.mark.asyncio
async def test_concurrent_native_runs_keep_separate_workload_contexts():
    from pyfly.observability.propagation import current_trace_ids

    provider, exporter, meter, _ = _providers("concurrent")
    runtime = AgenticRuntime(_config(), tracer_provider=provider, meter_provider=meter)

    async def respond(messages: Any, info: Any) -> ModelResponse:
        await asyncio.sleep(0)
        return _response(messages, info)

    agent = Agent("concurrent", model=FunctionModel(respond), runtime=runtime, auto_register=False)

    async def run(name: str) -> tuple[str, str]:
        with runtime.tracer.custom_span(name):
            before = current_trace_ids()
            await agent.run(name)
            assert current_trace_ids() == before
            assert before is not None
            return before

    try:
        ids = await asyncio.gather(run("first"), run("second"))
        assert ids[0][0] != ids[1][0]
        spans = exporter.get_finished_spans()
        for trace_id, span_id in ids:
            group = [s for s in spans if format(s.context.trace_id, "032x") == trace_id]
            assert len(group) == 4
            assert any(format(s.context.span_id, "016x") == span_id for s in group)
            assert len([s for s in group if s.attributes.get("gen_ai.operation.name") == "chat"]) == 1
        assert current_trace_ids() is None
    finally:
        await runtime.aclose()
        provider.shutdown()
        meter.shutdown()


@pytest.mark.asyncio
async def test_pyfly_configured_otlp_exporter_receives_native_model_spans(monkeypatch: pytest.MonkeyPatch):
    import queue
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    pytest.importorskip("opentelemetry.exporter.otlp.proto.http.trace_exporter")
    from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest

    from pyfly.core.config import Config
    from pyfly.observability.auto_configuration import TracingAutoConfiguration

    received: queue.Queue[tuple[str, bytes]] = queue.Queue()

    class Receiver(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            received.put((self.path, self.rfile.read(int(self.headers["Content-Length"]))))
            self.send_response(200)
            self.end_headers()

        def log_message(self, *args: Any) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Receiver)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    # A worker's standard exporter configuration must not register globals or create a second exporter.
    global_before = trace.get_tracer_provider()
    config = Config(
        {
            "pyfly": {
                "app": {"name": "native-exporter-proof"},
                "observability": {
                    "tracing": {
                        "register-global": False,
                        "exporter": "otlp",
                        "otlp": {"endpoint": f"http://127.0.0.1:{server.server_port}"},
                    }
                },
            }
        }
    )
    provider = TracingAutoConfiguration().tracer_provider(config)
    meter = MeterProvider(shutdown_on_exit=False)
    runtime = AgenticRuntime(_config(), tracer_provider=provider, meter_provider=meter)
    agent = Agent("exported", model=FunctionModel(_response), runtime=runtime, auto_register=False)
    try:
        with runtime.tracer.custom_span("workload") as parent:
            await agent.run("never export this prompt")
        assert provider.force_flush(timeout_millis=5000)
        path, body = received.get(timeout=2)
        request = ExportTraceServiceRequest.FromString(body)
        assert path == "/v1/traces"
        assert len(request.resource_spans) == 1
        resource_spans = request.resource_spans[0]
        assert any(
            a.key == "service.name" and a.value.string_value == "native-exporter-proof"
            for a in resource_spans.resource.attributes
        )
        spans = [s for scope in resource_spans.scope_spans for s in scope.spans]
        assert len(spans) == 4
        assert all(int.from_bytes(s.trace_id) == parent.get_span_context().trace_id for s in spans)
        chat = [
            s
            for s in spans
            if any(a.key == "gen_ai.operation.name" and a.value.string_value == "chat" for a in s.attributes)
        ]
        assert len(chat) == 1
        assert b"never export this prompt" not in body
        assert b"private reasoning" not in body
        assert trace.get_tracer_provider() is global_before
        assert len(provider._active_span_processor._span_processors) == 1
        assert received.empty()
    finally:
        await runtime.aclose()
        provider.shutdown()
        meter.shutdown()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["buffered", "incremental"])
async def test_native_stream_mid_response_failure_ends_all_spans(mode: str):
    provider, exporter, meter, _ = _providers("broken-stream")
    runtime = AgenticRuntime(_config(), tracer_provider=provider, meter_provider=meter)

    async def broken(messages: Any, info: Any) -> AsyncIterator[str]:
        yield "first token"
        await asyncio.sleep(0)
        raise RuntimeError("provider-secret-error")

    agent = Agent("broken", model=FunctionModel(stream_function=broken), runtime=runtime, auto_register=False)
    try:
        with runtime.tracer.custom_span("parent") as parent:
            with pytest.raises(RuntimeError, match="provider-secret-error"):
                async with await agent.run_stream("private prompt", streaming_mode=mode) as stream:
                    await stream.get_output()
            assert trace.get_current_span().get_span_context() == parent.get_span_context()
        spans = exporter.get_finished_spans()
        assert len(spans) == 4
        failed = [s for s in spans if s.name != "parent"]
        assert all(
            s.status.status_code is trace.StatusCode.ERROR
            for s in failed
            if s.name == "agent.broken" or s.attributes.get("gen_ai.operation.name") == "invoke_agent"
        )
        # Upstream currently leaves a failed mid-response chat UNSET; its enclosing run owns the error.
        assert len([s for s in failed if s.attributes.get("gen_ai.operation.name") == "chat"]) == 1
        assert "provider-secret-error" not in " ".join(s.to_json() for s in spans)
        assert not runtime._streams
    finally:
        await runtime.aclose()
        provider.shutdown()
        meter.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["buffered", "incremental"])
async def test_stream_usage_finalization_failure_closes_framework_span(mode: str):
    from pyfly.agentic.observability.cost_resolvers import UnknownModelCostError

    provider, exporter, meter, _ = _providers("usage-failure")
    config = _config().model_copy(update={"cost_tracking_enabled": True, "cost_strict": True})
    runtime = AgenticRuntime(config, tracer_provider=provider, meter_provider=meter)

    async def chunks(messages: Any, info: Any) -> AsyncIterator[str]:
        yield "finished"

    agent = Agent("unpriced", model=FunctionModel(stream_function=chunks), runtime=runtime, auto_register=False)
    try:
        with runtime.tracer.custom_span("parent") as parent:
            with pytest.raises(UnknownModelCostError):
                async with await agent.run_stream("private prompt", streaming_mode=mode) as stream:
                    await stream.get_output()
            assert trace.get_current_span().get_span_context() == parent.get_span_context()
        framework = next(s for s in exporter.get_finished_spans() if s.name == "agent.unpriced")
        assert framework.status.status_code is trace.StatusCode.ERROR
        assert not runtime._streams
    finally:
        await runtime.aclose()
        provider.shutdown()
        meter.shutdown()
