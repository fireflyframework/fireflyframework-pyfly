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
"""Starlette SSE route builder and response helpers.

Discovers ``@sse_mapping`` methods on ``@rest_controller`` and
``@controller`` beans and creates Starlette ``Route`` objects,
following the same lazy-resolution pattern used by
:class:`~pyfly.web.adapters.starlette.controller.ControllerRegistrar`
and :class:`~pyfly.websocket.adapters.starlette.WebSocketRegistrar`.
"""

from __future__ import annotations

import asyncio
import inspect
import math
from collections.abc import AsyncGenerator, AsyncIterable, Awaitable, Mapping
from dataclasses import dataclass
from typing import Any, overload

import anyio
from starlette.requests import Request
from starlette.responses import Response, StreamingResponse
from starlette.routing import Route
from starlette.types import Receive, Scope, Send

from pyfly.web.adapters.starlette.resolver import ParameterResolver
from pyfly.web.sse.response import SSE_HEADERS, SseEvent, format_sse_event

_CONTROLLER_STEREOTYPES = ("rest_controller", "controller")


@dataclass
class _SourceLifecycle:
    source: AsyncIterable[Any]
    started: bool = False


async def _wrap_generator(
    lifecycle: _SourceLifecycle, *, heartbeat_interval: float | None, max_event_bytes: int
) -> AsyncGenerator[str, None]:
    requests: asyncio.Queue[asyncio.Future[tuple[str | None, BaseException | None, bool]]] = asyncio.Queue(1)

    async def produce() -> None:
        iterator = aiter(lifecycle.source)
        lifecycle.started = True
        try:
            while True:
                slot = await requests.get()
                try:
                    item = await anext(iterator)
                    frame = (
                        item.encode(max_bytes=max_event_bytes)
                        if isinstance(item, SseEvent)
                        else (item if isinstance(item, str) else format_sse_event(item))
                    )
                    if len(frame.encode("utf-8")) > max_event_bytes:
                        raise ValueError("SSE event exceeds the configured size limit")
                except StopAsyncIteration:
                    slot.set_result((None, None, True))
                    return
                except BaseException as error:
                    slot.set_result((None, error, True))
                    return
                slot.set_result((frame, None, False))
        finally:
            close = getattr(iterator, "aclose", None)
            if close is not None:
                await close()

    producer = asyncio.create_task(produce(), name="pyfly-sse-producer")
    terminal = False
    try:
        while True:
            if producer.done():
                await producer
                return
            slot = asyncio.get_running_loop().create_future()
            requests.put_nowait(slot)
            while not slot.done():
                done, _ = await asyncio.wait(
                    {slot, producer}, timeout=heartbeat_interval, return_when=asyncio.FIRST_COMPLETED
                )
                if not done:
                    yield ": heartbeat\n\n"
                elif producer.done() and not slot.done():
                    await producer
                    return
            frame, error, terminal = slot.result()
            if error is not None:
                raise error
            if terminal:
                return
            assert frame is not None
            yield frame
    finally:
        with anyio.CancelScope(shield=True):
            if not terminal:
                producer.cancel()
            await asyncio.gather(producer, return_exceptions=True)


class _ClosingSseResponse(StreamingResponse):
    def __init__(self, *args: Any, lifecycle: _SourceLifecycle, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._source_lifecycle = lifecycle

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            with anyio.CancelScope(shield=True):
                close = getattr(self.body_iterator, "aclose", None)
                if close is not None:
                    await close()
                if not self._source_lifecycle.started:
                    close_source = getattr(self._source_lifecycle.source, "aclose", None)
                    if close_source is not None:
                        await close_source()


@overload
def make_sse_response(
    generator: AsyncIterable[Any],
    *,
    heartbeat_interval: float | None = None,
    max_event_bytes: int = 1048576,
    headers: Mapping[str, str] | None = None,
) -> StreamingResponse: ...


@overload
def make_sse_response(
    generator: Awaitable[AsyncIterable[Any]],
    *,
    heartbeat_interval: float | None = None,
    max_event_bytes: int = 1048576,
    headers: Mapping[str, str] | None = None,
) -> Awaitable[StreamingResponse]: ...


def make_sse_response(
    generator: AsyncIterable[Any] | Awaitable[AsyncIterable[Any]],
    *,
    heartbeat_interval: float | None = None,
    max_event_bytes: int = 1048576,
    headers: Mapping[str, str] | None = None,
) -> StreamingResponse | Awaitable[StreamingResponse]:
    """Wrap an async generator in a ``StreamingResponse`` with SSE headers.

    Yields that are already strings are passed through unchanged (the caller
    is responsible for SSE formatting).  All other yields are auto-wrapped
    with :func:`~pyfly.web.sse.response.format_sse_event`.

    Parameters
    ----------
    generator:
        An async generator producing SSE event data.

    Returns
    -------
    StreamingResponse
        A streaming response with ``text/event-stream`` media type.
    """
    if heartbeat_interval is not None and (
        isinstance(heartbeat_interval, bool) or not math.isfinite(heartbeat_interval) or heartbeat_interval <= 0
    ):
        raise ValueError("SSE heartbeat interval must be finite and positive")
    if type(max_event_bytes) is not int or max_event_bytes < 1:
        raise ValueError("SSE maximum event size must be a positive integer")
    if inspect.isawaitable(generator):

        async def prepare() -> StreamingResponse:
            source = await generator
            response = make_sse_response(
                source, heartbeat_interval=heartbeat_interval, max_event_bytes=max_event_bytes, headers=headers
            )
            assert isinstance(response, StreamingResponse)
            return response

        return prepare()
    if not hasattr(generator, "__aiter__"):
        raise TypeError("An SSE source must be an async iterable")
    response_headers = {**SSE_HEADERS, **dict(headers or {})}
    for key, value in response_headers.items():
        if key.lower() == "content-length" or (
            key.lower() == "content-type" and value.split(";", 1)[0] != "text/event-stream"
        ):
            raise ValueError("SSE content headers are managed by the response")
    lifecycle = _SourceLifecycle(generator)
    return _ClosingSseResponse(
        _wrap_generator(lifecycle, heartbeat_interval=heartbeat_interval, max_event_bytes=max_event_bytes),
        lifecycle=lifecycle,
        media_type="text/event-stream",
        headers=response_headers,
    )


class SSERegistrar:
    """Discovers ``@sse_mapping`` methods and builds Starlette SSE routes.

    For each controller:
    1. Reads ``@request_mapping`` base path from the class.
    2. Finds ``@sse_mapping`` handler methods.
    3. Creates ``Route`` objects with lazy bean resolution.
    """

    def collect_routes(self, ctx: Any) -> list[Route]:
        """Collect all SSE routes from controllers in the application context.

        Bean resolution is deferred until the first request, matching the
        lazy pattern used for HTTP and WebSocket routes.
        """
        routes: list[Route] = []

        for cls, _reg in ctx.container._registrations.items():
            if getattr(cls, "__pyfly_stereotype__", "") not in _CONTROLLER_STEREOTYPES:
                continue

            base_path = getattr(cls, "__pyfly_request_mapping__", "")

            for attr_name in dir(cls):
                method_obj = getattr(cls, attr_name, None)
                if method_obj is None:
                    continue

                sse_mapping = getattr(method_obj, "__pyfly_sse_mapping__", None)
                if sse_mapping is None:
                    continue

                full_path = base_path + sse_mapping["path"]
                handler = self._make_lazy_handler(ctx, cls, attr_name)
                routes.append(Route(full_path, handler, methods=["GET"]))

        return routes

    @staticmethod
    def _make_lazy_handler(ctx: Any, controller_cls: type, method_name: str) -> Any:
        """Create a Starlette endpoint that lazily resolves the controller bean.

        On the first request the controller is resolved from the application
        context and a ``ParameterResolver`` is built for the handler method.
        Subsequent requests reuse the cached instances.

        The handler method is expected to be an async generator.  Its return
        value is wrapped with :func:`make_sse_response` to produce a
        ``StreamingResponse`` with the correct SSE headers.
        """
        _cache: dict[str, Any] = {}

        async def lazy_sse_endpoint(request: Request) -> Response:
            if "instance" not in _cache:
                _cache["instance"] = ctx.get_bean(controller_cls)
                bound_method = getattr(_cache["instance"], method_name)
                _cache["resolver"] = ParameterResolver(bound_method)
                _cache["method"] = bound_method

            kwargs = await _cache["resolver"].resolve(request)
            generator = _cache["method"](**kwargs)
            if inspect.isawaitable(generator):
                generator = await generator
            if isinstance(generator, Response):
                return generator
            response = make_sse_response(generator)
            assert isinstance(response, Response)
            return response

        return lazy_sse_endpoint
