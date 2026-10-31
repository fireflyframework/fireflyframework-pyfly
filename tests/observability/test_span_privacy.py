# Copyright 2026 Firefly Software Foundation
# SPDX-License-Identifier: Apache-2.0
"""Generic spans must not export exception payloads by default."""

from __future__ import annotations

import asyncio
import importlib

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter


@pytest.fixture
def spans(monkeypatch):
    module = importlib.import_module("pyfly.observability.tracing")
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr(module, "_tracer", provider.get_tracer("privacy-proof"))
    yield module.span, exporter
    provider.shutdown()


@pytest.mark.parametrize("include_content", [False, True])
def test_sync_error_content_is_explicit_opt_in(spans, include_content):
    span, exporter = spans
    sentinel = "PRIVATE_BEARER_PROMPT_SOURCE_SENTINEL"

    @span("operation", include_content=include_content)
    def fail():
        raise ValueError(sentinel)

    with pytest.raises(ValueError, match=sentinel):
        fail()
    recorded = exporter.get_finished_spans()
    assert len(recorded) == 1
    assert recorded[0].status.status_code.name == "ERROR"
    serialized = str(recorded[0].to_json())
    assert (sentinel in serialized) is include_content
    assert ("exception.stacktrace" in serialized) is include_content
    assert "ValueError" in serialized


@pytest.mark.asyncio
@pytest.mark.parametrize("exception", [ValueError, asyncio.CancelledError])
async def test_async_default_withholds_error_and_cancellation_content(spans, exception):
    span, exporter = spans
    sentinel = "PRIVATE_BEARER_PROMPT_SOURCE_SENTINEL"

    @span("operation")
    async def fail():
        raise exception(sentinel)

    with pytest.raises(exception):
        await fail()
    recorded = exporter.get_finished_spans()
    assert len(recorded) == 1
    serialized = str(recorded[0].to_json())
    assert sentinel not in serialized
    assert "exception.stacktrace" not in serialized
    assert exception.__name__ in serialized


def test_content_opt_in_requires_actual_boolean(spans):
    span, _ = spans
    with pytest.raises(ValueError, match="boolean"):
        span("operation", include_content="false")
