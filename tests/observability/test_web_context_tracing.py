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
"""HTTP server spans use the application's private DI provider without changing globals."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from importlib.util import find_spec
from typing import Any

import pytest

pytest.importorskip("opentelemetry")

from opentelemetry import trace  # noqa: E402
from opentelemetry.sdk.trace import TracerProvider  # noqa: E402
from opentelemetry.sdk.trace.export import SimpleSpanProcessor  # noqa: E402
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter  # noqa: E402
from starlette.requests import Request  # noqa: E402
from starlette.responses import JSONResponse, PlainTextResponse  # noqa: E402
from starlette.routing import Route  # noqa: E402
from starlette.testclient import TestClient  # noqa: E402

from pyfly.context import ApplicationContext  # noqa: E402
from pyfly.core import Config  # noqa: E402
from pyfly.observability.auto_configuration import TracingAutoConfiguration  # noqa: E402
from pyfly.observability.propagation import current_trace_ids, inject_headers  # noqa: E402
from pyfly.web.adapters.starlette.filters.tracing_filter import TracingFilter  # noqa: E402

TRACE_ID = "0af7651916cd43dd8448eb211c80319c"
PARENT_ID = "b7ad6b7169203331"
HEADERS = {"traceparent": f"00-{TRACE_ID}-{PARENT_ID}-01"}


def _context(exclude_patterns: list[str] | None = None) -> ApplicationContext:
    return ApplicationContext(
        Config(
            {
                "pyfly": {
                    "observability": {
                        "tracing": {
                            "register-global": False,
                            "exporter": "none",
                            "exclude-patterns": ["/ready"] if exclude_patterns is None else exclude_patterns,
                        }
                    }
                }
            }
        ),
        auto_configurations=[TracingAutoConfiguration],
    )


@pytest.mark.parametrize("adapter", ["starlette", "fastapi"])
def test_app_built_before_context_boot_exports_http_and_child_spans_through_private_provider(adapter: str) -> None:
    if adapter == "fastapi":
        if find_spec("fastapi") is None:
            pytest.skip("fastapi not installed")
        from pyfly.web.adapters.fastapi.app import create_app
    else:
        from pyfly.web.adapters.starlette import create_app

    original = trace.get_tracer_provider()
    context = _context()
    exporter = InMemorySpanExporter()

    @asynccontextmanager
    async def lifespan(app: Any) -> Any:
        await context.start()
        context.get_bean(trace.TracerProvider).add_span_processor(SimpleSpanProcessor(exporter))
        try:
            yield
        finally:
            await context.stop()

    async def endpoint(request: Request) -> JSONResponse:
        provider = context.get_bean(trace.TracerProvider)
        with provider.get_tracer("private-application").start_as_current_span("application-child"):
            return JSONResponse({"ids": current_trace_ids(), "outbound": inject_headers({})})

    app = create_app(
        context=context,
        lifespan=lifespan,
        docs_enabled=False,
        actuator_enabled=False,
        extra_routes=[Route("/orders", endpoint), Route("/ready", endpoint)],
    )
    with TestClient(app) as client:
        response = client.get("/orders", headers=HEADERS)
        assert response.status_code == 200
        assert response.json()["ids"][0] == TRACE_ID
        assert response.json()["outbound"]["traceparent"].split("-")[1] == TRACE_ID
        assert client.get("/ready").status_code == 200
        spans = exporter.get_finished_spans()
        (server,) = [span for span in spans if span.kind == trace.SpanKind.SERVER]
        (child,) = [
            span for span in spans if span.name == "application-child" and span.context.trace_id == int(TRACE_ID, 16)
        ]
        assert server.name == "GET /orders"
        assert server.parent.span_id == int(PARENT_ID, 16)
        assert server.context.trace_id == int(TRACE_ID, 16)
        assert child.parent.span_id == server.context.span_id
        assert server.attributes["http.response.status_code"] == 200
        assert trace.get_tracer_provider() is original
    assert trace.get_tracer_provider() is original


@pytest.mark.asyncio
async def test_management_listener_uses_same_private_provider_and_configured_excludes() -> None:
    import httpx

    from pyfly.web.adapters.starlette.management_app import create_management_app

    original = trace.get_tracer_provider()
    context = _context([])
    exporter = InMemorySpanExporter()
    await context.start()
    provider = context.get_bean(trace.TracerProvider)
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    try:
        app = create_management_app(
            context,
            health_agg=None,
            http_exchange_recorder=None,
            admin_trace_collector=None,
            actuator_active=True,
            admin_enabled=False,
        )
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://management") as client:
            assert (await client.get("/actuator/health", headers=HEADERS)).status_code == 200
        (server,) = [span for span in exporter.get_finished_spans() if span.kind == trace.SpanKind.SERVER]
        assert server.name == "GET /actuator/health"
        assert server.context.trace_id == int(TRACE_ID, 16)
        assert server.parent.span_id == int(PARENT_ID, 16)
        assert trace.get_tracer_provider() is original
    finally:
        await context.stop()
    assert trace.get_tracer_provider() is original


@pytest.mark.asyncio
async def test_provider_resolver_is_lazy_and_tracks_replaced_providers_without_owning_them() -> None:
    providers = [TracerProvider(), TracerProvider()]
    exporters = [InMemorySpanExporter(), InMemorySpanExporter()]
    for provider, exporter in zip(providers, exporters, strict=True):
        provider.add_span_processor(SimpleSpanProcessor(exporter))
    selected = [0]
    resolved = []

    def resolve() -> trace.TracerProvider:
        resolved.append(selected[0])
        return providers[selected[0]]

    async def endpoint(request: Request) -> PlainTextResponse:
        return PlainTextResponse("ok")

    def request() -> Request:
        return Request({"type": "http", "method": "GET", "path": "/orders", "headers": []})

    try:
        tracing = TracingFilter(tracer_provider_resolver=resolve)
        assert not resolved
        await tracing.do_filter(request(), endpoint)
        selected[0] = 1
        await tracing.do_filter(request(), endpoint)
        assert resolved == [0, 1]
        assert all(len(exporter.get_finished_spans()) == 1 for exporter in exporters)
        # Filters borrow providers: another operation remains possible after the request.
        with providers[0].get_tracer("after-request").start_as_current_span("still-owned-by-application"):
            pass
        assert len(exporters[0].get_finished_spans()) == 2
    finally:
        for provider in providers:
            provider.shutdown()


@pytest.mark.asyncio
async def test_filter_constructor_can_borrow_the_native_di_provider() -> None:
    context = _context()
    context.register_bean(TracingFilter)
    exporter = InMemorySpanExporter()
    try:
        await context.start()
        provider = context.get_bean(trace.TracerProvider)
        provider.add_span_processor(SimpleSpanProcessor(exporter))

        async def endpoint(request: Request) -> PlainTextResponse:
            return PlainTextResponse("ok")

        request = Request({"type": "http", "method": "GET", "path": "/orders", "headers": []})
        await context.get_bean(TracingFilter).do_filter(request, endpoint)
        (server,) = exporter.get_finished_spans()
        assert server.kind == trace.SpanKind.SERVER
        assert server.name == "GET /orders"
    finally:
        await context.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("error_type", [ValueError, asyncio.CancelledError])
async def test_server_errors_export_only_type_and_rethrow_without_message_or_stack(
    error_type: type[BaseException],
) -> None:
    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    error = error_type("PRIVATE-EXCEPTION-CONTENT")

    async def endpoint(request: Request) -> PlainTextResponse:
        raise error

    try:
        request = Request({"type": "http", "method": "GET", "path": "/orders", "headers": []})
        with pytest.raises(error_type) as caught:
            await TracingFilter(tracer_provider=provider).do_filter(request, endpoint)
        assert caught.value is error
        (server,) = exporter.get_finished_spans()
        assert server.status.status_code == trace.StatusCode.ERROR
        assert server.status.description is None
        assert server.attributes["error.type"] == error_type.__name__
        assert not server.events
        assert "PRIVATE-EXCEPTION-CONTENT" not in server.to_json()
    finally:
        provider.shutdown()
