# Copyright 2026 Firefly Software Foundation & Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
import asyncio
from contextvars import ContextVar

import pytest
from starlette.requests import ClientDisconnect


def test_typed_event_preserves_metadata_empty_data_and_comments():
    from pyfly.web.sse import SseEvent

    wire = SseEvent(data="first\nsecond", event="delta", id="cursor-7", retry=0, comment="observed").encode()
    assert wire == ": observed\nid: cursor-7\nevent: delta\nretry: 0\ndata: first\ndata: second\n\n"
    assert SseEvent(data="").encode() == "data: \n\n"
    assert SseEvent(comment="heartbeat").encode() == ": heartbeat\n\n"


async def test_heartbeat_preserves_one_pending_read_and_source_context():
    from pyfly.web.sse import SseEvent
    from pyfly.web.sse.adapters.starlette import make_sse_response

    actor = ContextVar("sse_test_actor", default="missing")
    gate, closed = asyncio.Event(), asyncio.Event()
    calls = []

    async def source():
        try:
            calls.append(actor.get())
            await gate.wait()
            yield SseEvent(data={"actor": actor.get()}, event="proof", id="persisted-1")
            await asyncio.Event().wait()
        finally:
            closed.set()

    actor.set("verified")
    response = make_sse_response(source(), heartbeat_interval=0.005)
    stream = response.body_iterator
    assert (await anext(stream)).startswith(":")
    assert (await anext(stream)).startswith(":")
    assert calls == ["verified"] and not closed.is_set()
    gate.set()
    assert "id: persisted-1" in await anext(stream)
    await stream.aclose()
    assert closed.is_set()


async def test_one_producer_owns_context_tokens_and_cancel_scope_across_yields_and_close():
    import anyio

    from pyfly.web.sse import SseEvent
    from pyfly.web.sse.adapters.starlette import make_sse_response

    context = ContextVar("native_stream_context", default="outer")
    owners = []
    closed = asyncio.Event()

    async def source():
        with anyio.CancelScope():
            token = context.set("native")
            try:
                owners.append(asyncio.current_task())
                yield SseEvent(data=1)
                owners.append(asyncio.current_task())
                yield SseEvent(data=2)
                await asyncio.Event().wait()
            finally:
                owners.append(asyncio.current_task())
                context.reset(token)
                closed.set()

    response = make_sse_response(source(), heartbeat_interval=0.005)
    stream = response.body_iterator
    assert "data: 1" in await anext(stream)
    assert "data: 2" in await anext(stream)
    await stream.aclose()
    assert closed.is_set() and len(set(owners)) == 1 and context.get() == "outer"


async def test_bounded_live_emitter_wakes_blocked_producers_on_close():
    from pyfly.web.sse import AsyncSseEmitter, SseEvent

    emitter = AsyncSseEmitter(capacity=1)
    await emitter.send(SseEvent(data={"n": 1}))
    blocked = asyncio.create_task(emitter.send(SseEvent(data={"n": 2})))
    await asyncio.sleep(0)
    assert not blocked.done() and emitter.buffered == 1
    stream = aiter(emitter)
    assert '"n": 1' in await anext(stream)
    await blocked
    another = asyncio.create_task(emitter.send(SseEvent(data={"n": 3})))
    await asyncio.sleep(0)
    await emitter.aclose()
    with pytest.raises(RuntimeError):
        await another
    await stream.aclose()
    assert emitter.buffered == 0


@pytest.mark.parametrize(
    "values",
    [{"id": "invalid\nfield"}, {"event": "invalid\rfield"}, {"id": "invalid\0field"}, {"retry": -1}, {"retry": True}],
)
def test_metadata_and_retry_are_validated(values):
    from pyfly.web.sse import SseEvent

    with pytest.raises(ValueError):
        SseEvent(data={}, **values).encode()


async def test_async_factory_denies_before_response_construction():
    from pyfly.web.sse.adapters.starlette import make_sse_response

    async def forbidden():
        raise PermissionError("Denied by application policy")

    with pytest.raises(PermissionError):
        await make_sse_response(forbidden())


async def test_utf8_event_limit_and_pull_backpressure():
    from pyfly.web.sse import SseEvent
    from pyfly.web.sse.adapters.starlette import make_sse_response

    advanced = asyncio.Event()

    async def source():
        yield SseEvent(data={"n": 1})
        advanced.set()
        yield SseEvent(data="é" * 30)

    response = make_sse_response(source(), max_event_bytes=40)
    stream = response.body_iterator
    await anext(stream)
    await asyncio.sleep(0)
    assert not advanced.is_set()
    with pytest.raises(ValueError, match="size"):
        await anext(stream)


async def test_send_failure_closes_the_source_in_its_producer_task():
    from pyfly.web.sse import SseEvent
    from pyfly.web.sse.adapters.starlette import make_sse_response

    closed = asyncio.Event()
    owners = []

    async def source():
        try:
            owners.append(asyncio.current_task())
            yield SseEvent(data={"n": 1})
            await asyncio.Event().wait()
        finally:
            await asyncio.sleep(0)
            owners.append(asyncio.current_task())
            closed.set()

    response = make_sse_response(source())

    async def send(message):
        if message["type"] == "http.response.body":
            raise OSError("Owned test transport closed")

    async def receive():
        await asyncio.Event().wait()

    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.4"},
        "method": "GET",
        "path": "/events",
        "headers": [],
    }
    with pytest.raises(ClientDisconnect):
        await response(scope, receive, send)
    assert closed.is_set() and len(set(owners)) == 1


async def test_asgi_disconnect_drains_native_cancel_scope_and_leaves_no_producer():
    import anyio

    from pyfly.web.sse import SseEvent
    from pyfly.web.sse.adapters.starlette import make_sse_response

    first, closed = asyncio.Event(), asyncio.Event()
    context = ContextVar("disconnect_scope", default="outer")

    async def source():
        with anyio.CancelScope():
            token = context.set("owned")
            try:
                yield SseEvent(data={"n": 1})
                await asyncio.Event().wait()
            finally:
                context.reset(token)
                await asyncio.sleep(0)
                closed.set()

    async def send(message):
        if message["type"] == "http.response.body":
            first.set()

    async def receive():
        await first.wait()
        return {"type": "http.disconnect"}

    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "method": "GET",
        "path": "/events",
        "headers": [],
    }
    await make_sse_response(source(), heartbeat_interval=0.01)(scope, receive, send)
    assert closed.is_set() and context.get() == "outer"
    assert not [task for task in asyncio.all_tasks() if task.get_name() == "pyfly-sse-producer" and not task.done()]


async def test_native_registrar_awaits_preflight_and_preserves_prepared_response():
    import httpx
    from starlette.responses import Response

    from pyfly.web.sse import SseEvent, sse_mapping
    from pyfly.web.sse.adapters.starlette import make_sse_response
    from tests.web.test_sse_e2e import _make_app_from_controller

    class Controller:
        @sse_mapping("/prepared")
        async def events(self) -> Response:
            await asyncio.sleep(0)

            async def source():
                yield SseEvent(data={"authorized": True}, event="verified", id="db-1")

            return make_sse_response(source(), headers={"Cache-Control": "no-store"})

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_make_app_from_controller(Controller())), base_url="http://test"
    ) as client:
        response = await client.get("/prepared")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.text == 'id: db-1\nevent: verified\ndata: {"authorized": true}\n\n'


async def test_header_send_failure_closes_an_unstarted_live_source_and_releases_senders():
    from pyfly.web.sse import AsyncSseEmitter
    from pyfly.web.sse.adapters.starlette import make_sse_response

    source = AsyncSseEmitter(capacity=1)
    await source.send({"first": True})
    blocked = asyncio.create_task(source.send({"second": True}))
    await asyncio.sleep(0)
    assert not blocked.done()
    response = make_sse_response(source)

    async def send(message):
        raise OSError("Owned transport closed before headers")

    async def receive():
        await asyncio.Event().wait()

    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.4"},
        "method": "GET",
        "path": "/events",
        "headers": [],
    }
    try:
        with pytest.raises(ClientDisconnect):
            await response(scope, receive, send)
        assert source.buffered == 0
        with pytest.raises(RuntimeError, match="closed"):
            await asyncio.wait_for(blocked, 0.2)
    finally:
        await source.aclose()
        await asyncio.gather(blocked, return_exceptions=True)


async def test_producer_cancelled_before_start_retains_source_close_fallback(monkeypatch):
    from pyfly.web.sse.adapters.starlette import make_sse_response

    class Source:
        closed = False

        def __aiter__(self):
            return self

        async def __anext__(self):
            await asyncio.Event().wait()

        async def aclose(self):
            self.closed = True

    source = Source()
    original = asyncio.create_task

    def cancel_before_start(coroutine, *, name=None, **kwargs):
        task = original(coroutine, name=name, **kwargs)
        if name == "pyfly-sse-producer":
            task.cancel()
        return task

    monkeypatch.setattr(asyncio, "create_task", cancel_before_start)
    response = make_sse_response(source)

    async def send(message):
        pass

    async def receive():
        await asyncio.Event().wait()

    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.4"},
        "method": "GET",
        "path": "/events",
        "headers": [],
    }
    with pytest.raises(asyncio.CancelledError):
        await response(scope, receive, send)
    assert source.closed
