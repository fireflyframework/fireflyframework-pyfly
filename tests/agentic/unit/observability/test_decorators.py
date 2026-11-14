"""Tests for observability/decorators.py."""

from __future__ import annotations

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode

from pyfly.agentic.observability.decorators import metered, traced
from pyfly.agentic.observability.tracer import AgentTracer


class TestTracedDecorator:
    async def test_async_function_traced(self) -> None:
        @traced("test-span")
        async def my_func() -> str:
            return "ok"

        result = await my_func()
        assert result == "ok"

    def test_sync_function_traced(self) -> None:
        @traced("sync-span")
        def my_func() -> str:
            return "ok"

        result = my_func()
        assert result == "ok"


class TestMeteredDecorator:
    async def test_async_function_metered(self) -> None:
        @metered("test-op")
        async def my_func() -> str:
            return "metered"

        result = await my_func()
        assert result == "metered"

    def test_sync_function_metered(self) -> None:
        @metered("sync-op")
        def my_func() -> int:
            return 42

        result = my_func()
        assert result == 42


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("include_content", [False, True])
async def test_traced_exception_is_recorded_once_with_content_policy(monkeypatch, asynchronous, include_content):
    exporter = InMemorySpanExporter()
    provider = TracerProvider(shutdown_on_exit=False)
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr(
        "pyfly.agentic.observability.decorators.default_tracer",
        AgentTracer(tracer_provider=provider, include_content=include_content),
    )
    error = RuntimeError("private-exception-content")

    @traced("failed-work")
    def fail_sync():
        raise error

    @traced("failed-work")
    async def fail_async():
        raise error

    try:
        with pytest.raises(RuntimeError) as caught:
            if asynchronous:
                await fail_async()
            else:
                fail_sync()
        assert caught.value is error
        spans = exporter.get_finished_spans()
        assert len(spans) == 1
        span = spans[0]
        assert span.status.status_code is StatusCode.ERROR
        assert len(span.events) == 1
        event = span.events[0]
        assert event.name == "exception"
        assert event.attributes["exception.type"] == "RuntimeError"
        if include_content:
            assert event.attributes["exception.message"] == "private-exception-content"
            assert "private-exception-content" in event.attributes["exception.stacktrace"]
            assert span.status.description == "private-exception-content"
        else:
            assert "private-exception-content" not in span.to_json()
            assert "exception.stacktrace" not in event.attributes
            assert span.status.description is None
    finally:
        provider.shutdown()
