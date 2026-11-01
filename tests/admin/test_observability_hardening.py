# Copyright 2026 Firefly Software Foundation
# SPDX-License-Identifier: Apache-2.0
"""Native Admin configuration, privacy, live progress and cost coverage boundaries."""

import asyncio
import importlib
import json

import pytest
from starlette.requests import Request
from starlette.responses import PlainTextResponse

from pyfly.admin.api.sse import traces_stream
from pyfly.admin.middleware.trace_collector import TraceCollectorFilter
from pyfly.admin.providers.traces_provider import TracesProvider
from pyfly.context import ApplicationContext
from pyfly.core import Config


def request(path="/business", query=b""):
    return Request(
        {
            "type": "http",
            "method": "GET",
            "scheme": "http",
            "server": ("fixture", 80),
            "path": path,
            "query_string": query,
            "headers": [],
        }
    )


async def capture(collector, path="/business", query=b""):
    async def next_handler(current):
        return PlainTextResponse("fixture")

    await collector.do_filter(request(path, query), next_handler)


def payload(frame):
    return json.loads(next(line.removeprefix("data: ") for line in frame.splitlines() if line.startswith("data: ")))


@pytest.mark.parametrize("adapter", ["starlette", "fastapi"])
def test_invalid_admin_properties_cannot_turn_explicit_auth_into_anonymous_defaults(adapter):
    context = ApplicationContext(
        Config(
            {
                "pyfly": {
                    "admin": {
                        "enabled": True,
                        "require-auth": True,
                        "refresh-interval": "not-an-integer",
                    }
                }
            }
        ),
        auto_configurations=[],
    )
    factory = importlib.import_module(f"pyfly.web.adapters.{adapter}.app").create_app
    with pytest.raises(ValueError):
        factory(context=context, docs_enabled=False, actuator_enabled=False)


async def test_query_values_are_removed_before_admin_trace_retention_and_readout():
    collector = TraceCollectorFilter()
    await capture(collector, query=b"api_key=PRIVATE_QUERY_CANARY&search=PRIVATE_SEARCH_CANARY")
    assert "PRIVATE" not in json.dumps(collector.get_traces())
    assert "PRIVATE" not in json.dumps(await TracesProvider(collector).get_traces())
    stream = traces_stream(collector, interval=0.001)
    try:
        assert "PRIVATE" not in await anext(stream)
    finally:
        await stream.aclose()


def test_collector_respects_custom_admin_prefix_without_excluding_similar_application_paths():
    collector = TraceCollectorFilter(admin_path="/operations")
    assert collector.should_not_filter(request("/operations/api/audit"))
    assert collector.should_not_filter(request("/operations"))
    assert not collector.should_not_filter(request("/operations-jobs"))


async def test_trace_sse_advances_after_the_ring_buffer_is_full():
    collector = TraceCollectorFilter(max_traces=2)
    await capture(collector, "/first")
    await capture(collector, "/second")
    stream = traces_stream(collector, interval=0.001)
    try:
        assert payload(await anext(stream))["path"] == "/first"
        assert payload(await anext(stream))["path"] == "/second"
        await capture(collector, "/third")
        try:
            frame = await asyncio.wait_for(anext(stream), 0.1)
        except (TimeoutError, StopAsyncIteration):
            frame = None
        assert frame is not None, "A full retained buffer must still emit newly completed requests"
        assert payload(frame)["path"] == "/third"
    finally:
        await stream.aclose()


async def test_trace_sse_reports_eviction_and_clear_gaps_without_resetting_ids():
    collector = TraceCollectorFilter(max_traces=2)
    await capture(collector)
    stream = traces_stream(collector, interval=0.001)
    try:
        assert payload(await anext(stream))["id"] == 1
        for _ in range(4):
            await capture(collector)
        gap = await anext(stream)
        assert "event: trace-gap" in gap
        assert payload(gap)["missing_count"] == 2
        assert payload(await anext(stream))["id"] == 4
        assert payload(await anext(stream))["id"] == 5
        await capture(collector)
        collector.clear_traces()
        await capture(collector)
        assert payload(await anext(stream))["missing_count"] == 1
        assert payload(await anext(stream))["id"] == 7
    finally:
        await stream.aclose()


async def test_native_stream_start_status_is_not_replaced_by_the_filter_sentinel():
    import httpx
    from starlette.applications import Starlette
    from starlette.middleware import Middleware
    from starlette.responses import StreamingResponse
    from starlette.routing import Route

    from pyfly.web.adapters.starlette.filter_chain import WebFilterChainMiddleware

    collector = TraceCollectorFilter()

    async def handler(current):
        async def content():
            yield b"first"
            yield b"last"

        return StreamingResponse(content(), status_code=207)

    app = Starlette(
        routes=[Route("/stream", handler)], middleware=[Middleware(WebFilterChainMiddleware, filters=[collector])]
    )
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://fixture") as client:
        result = await client.get("/stream")
    assert result.status_code == 207 and result.content == b"firstlast"
    assert collector.get_traces()[0]["status"] == 207


@pytest.mark.parametrize("resolved", [False, True])
async def test_agentic_cost_distinguishes_unresolved_usage_from_a_known_zero(resolved):
    from pyfly.admin.providers.agentic_provider import AgenticProvider
    from pyfly.agentic import AgenticConfig, AgenticRuntime
    from pyfly.agentic.observability.usage import UsageTracker

    tracker = UsageTracker(resolver=lambda context: 0.0 if resolved else None)
    runtime = AgenticRuntime(AgenticConfig(plugin_auto_discover=False), usage_tracker=tracker)
    context = ApplicationContext(Config({}), auto_configurations=[])
    context.container.register_instance(AgenticRuntime, runtime)
    try:
        tracker.record_call(model="fixture-unpriced", input_tokens=2, output_tokens=3, request_count=1)
        usage = (await AgenticProvider(context).get_overview())["usage"]
        assert usage.get("cost_status") == ("known" if resolved else "unknown")
        assert usage.get("known_cost_usd") == (0.0 if resolved else None)
        assert usage["total_cost_usd"] == 0.0
    finally:
        await runtime.aclose()
