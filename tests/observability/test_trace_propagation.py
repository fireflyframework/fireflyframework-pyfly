# Copyright 2026 Firefly Software Foundation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""OpenTelemetry distributed-trace context propagation (v26.06.38)."""

from __future__ import annotations

from typing import Any

import pytest

pytest.importorskip("opentelemetry")

from opentelemetry import trace  # noqa: E402
from opentelemetry.sdk.trace import TracerProvider  # noqa: E402
from starlette.requests import Request  # noqa: E402
from starlette.responses import PlainTextResponse, Response  # noqa: E402

from pyfly.logging.structlog_adapter import _add_trace_ids  # noqa: E402
from pyfly.observability.propagation import (  # noqa: E402
    current_trace_ids,
    extract_context,
    inject_headers,
)
from pyfly.web.adapters.starlette.filters.tracing_filter import TracingFilter  # noqa: E402

# Ensure a recording SDK provider so spans have valid contexts (reused if already set).
if not isinstance(trace.get_tracer_provider(), TracerProvider):
    trace.set_tracer_provider(TracerProvider())
_tracer = trace.get_tracer("pyfly-test")


def test_inject_extract_round_trip_preserves_trace() -> None:
    with _tracer.start_as_current_span("parent") as parent:
        headers: dict[str, str] = {}
        inject_headers(headers)
        assert "traceparent" in headers  # current context injected outbound
        parent_trace = parent.get_span_context().trace_id

    parent_ctx = extract_context(headers)
    with _tracer.start_as_current_span("child", context=parent_ctx) as child:
        # The child (built from the injected headers) shares the parent's trace.
        assert child.get_span_context().trace_id == parent_trace


def test_current_trace_ids_and_log_processor() -> None:
    assert current_trace_ids() is None  # no active span
    with _tracer.start_as_current_span("s") as span:
        ids = current_trace_ids()
        assert ids is not None
        assert ids[0] == format(span.get_span_context().trace_id, "032x")
        assert ids[1] == format(span.get_span_context().span_id, "016x")
        event = _add_trace_ids(None, "info", {"event": "hello"})
        assert event["trace_id"] == ids[0]
        assert event["span_id"] == ids[1]


def _request(headers: dict[str, str]) -> Request:
    raw = [(k.encode(), v.encode()) for k, v in headers.items()]
    return Request({"type": "http", "method": "GET", "path": "/x", "headers": raw})


@pytest.mark.asyncio
async def test_tracing_filter_inherits_inbound_trace() -> None:
    inbound_trace = "0af7651916cd43dd8448eb211c80319c"
    traceparent = f"00-{inbound_trace}-b7ad6b7169203331-01"
    captured: dict[str, Any] = {}

    async def call_next(_req: Request) -> Response:
        captured["ids"] = current_trace_ids()
        return PlainTextResponse("ok")

    await TracingFilter().do_filter(_request({"traceparent": traceparent}), call_next)
    assert captured["ids"] is not None
    # The server span opened by the filter is a child of the upstream trace.
    assert captured["ids"][0] == inbound_trace
