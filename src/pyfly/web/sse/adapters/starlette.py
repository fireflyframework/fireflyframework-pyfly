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

from collections.abc import AsyncGenerator
from typing import Any

from starlette.requests import Request
from starlette.responses import Response, StreamingResponse
from starlette.routing import Route

from pyfly.web.adapters.starlette.resolver import ParameterResolver
from pyfly.web.sse.response import SSE_HEADERS, format_sse_event

_CONTROLLER_STEREOTYPES = ("rest_controller", "controller")


async def _wrap_generator(generator: AsyncGenerator[Any, None]) -> AsyncGenerator[str, None]:
    """Wrap an async generator, auto-formatting non-string yields as SSE events."""
    async for item in generator:
        if isinstance(item, str):
            yield item
        else:
            yield format_sse_event(item)


def make_sse_response(generator: AsyncGenerator[Any, None]) -> StreamingResponse:
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
    return StreamingResponse(
        _wrap_generator(generator),
        media_type="text/event-stream",
        headers=SSE_HEADERS,
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
            return make_sse_response(generator)

        return lazy_sse_endpoint
