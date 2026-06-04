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
"""WebFilterChainMiddleware — pure ASGI middleware wrapping all WebFilters."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, cast

from starlette.requests import Request
from starlette.responses import Response
from starlette.types import ASGIApp, Receive, Scope, Send

from pyfly.web.ports.filter import CallNext, WebFilter

MAX_RESPONSE_BODY_SIZE = 100 * 1024 * 1024  # 100 MB


class _AlreadySentResponse(Response):
    """Sentinel returned by the terminal when a streaming response has already
    been forwarded to the client. Sending it again is a no-op.

    Filters that post-process the response (e.g. add headers) operate on this
    object harmlessly — for streaming responses the headers were already
    flushed, so mutations are silently ignored, matching the behaviour of any
    pass-through ASGI middleware.
    """

    def __init__(self) -> None:
        super().__init__(b"", status_code=200)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:  # noqa: D401
        return


class WebFilterChainMiddleware:
    """Pure ASGI middleware that executes a sorted chain of :class:`WebFilter` instances.

    Each filter's ``should_not_filter()`` is checked before invocation — if it
    returns ``True``, the filter is skipped and the next one in the chain runs.

    Uses raw ASGI protocol instead of ``BaseHTTPMiddleware`` to avoid the
    ``anyio`` dependency that causes ``ModuleNotFoundError`` with ASGI servers
    that don't register with sniffio (e.g. Granian).
    """

    def __init__(self, app: ASGIApp, filters: Sequence[WebFilter] = ()) -> None:
        self.app = app
        # Keep the *live* list reference when a list is passed so filters
        # discovered after ApplicationContext.start() (security / session / CSRF
        # bean filters) can be appended in place and picked up per request
        # (audit #40). A non-list (e.g. the tuple default) is copied.
        self._filters = filters if isinstance(filters, list) else list(filters)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        if not self._filters:
            await self.app(scope, receive, send)
            return

        request = Request(scope, receive, send)

        async def _call_app(req: Any) -> Response:
            """Terminal: run the downstream ASGI app and adapt its response.

            Single-shot responses are buffered into a :class:`Response` so filters
            can inspect/modify them. Streaming responses (SSE, chunked downloads —
            anything that emits a body frame with ``more_body=True``) are detected
            on their first chunk and forwarded to the client **live**, so they are
            never buffered in memory and reach the client incrementally.
            """
            status_code = 200
            raw_headers: list[tuple[bytes, bytes]] = []
            body_parts: list[bytes] = []
            state = {"streaming": False}

            async def _intercept(message: Any) -> None:
                nonlocal status_code, raw_headers
                msg_type = message["type"]
                if msg_type == "http.response.start":
                    status_code = message["status"]
                    raw_headers = list(message.get("headers", []))
                elif msg_type == "http.response.body":
                    body = message.get("body", b"")
                    more_body = message.get("more_body", False)
                    # Detect streaming on the first body frame that signals more to
                    # come. Once detected, flush the captured start line and forward
                    # every frame (including this one) straight to the client.
                    if not state["streaming"] and more_body:
                        state["streaming"] = True
                        await send(
                            {
                                "type": "http.response.start",
                                "status": status_code,
                                "headers": raw_headers,
                            }
                        )
                    if state["streaming"]:
                        await send({"type": "http.response.body", "body": body, "more_body": more_body})
                        return
                    if body:
                        body_parts.append(body)
                        if sum(len(p) for p in body_parts) > MAX_RESPONSE_BODY_SIZE:
                            raise RuntimeError(
                                f"Response body exceeds {MAX_RESPONSE_BODY_SIZE} bytes. "
                                "Consider excluding this route from the filter chain."
                            )
                elif msg_type == "http.response.pathsend":
                    # ASGI pathsend extension (Granian zero-copy file serving).
                    # Stream the file in chunks to avoid OOM on large files.
                    from pathlib import Path

                    path = message.get("path", "")
                    if path:
                        file_path = Path(path)
                        chunk_size = 64 * 1024  # 64 KB chunks
                        with file_path.open("rb") as fh:
                            while True:
                                chunk = fh.read(chunk_size)
                                if not chunk:
                                    break
                                body_parts.append(chunk)

            await self.app(scope, receive, _intercept)

            if state["streaming"]:
                # Body already forwarded live; nothing left to send.
                return _AlreadySentResponse()

            response = Response(content=b"".join(body_parts), status_code=status_code)
            response.raw_headers[:] = raw_headers
            return response

        chain: CallNext = _call_app
        for f in reversed(self._filters):
            chain = _wrap(f, chain)

        response = cast(Response, await chain(request))
        await response(scope, receive, send)


def _wrap(web_filter: WebFilter, next_call: CallNext) -> CallNext:
    """Create a closure that conditionally invokes *web_filter*."""

    async def _inner(request: Request) -> Response:
        if web_filter.should_not_filter(request):
            return cast(Response, await next_call(request))
        return cast(Response, await web_filter.do_filter(request, next_call))

    return _inner
