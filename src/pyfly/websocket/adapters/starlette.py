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
"""Starlette WebSocket route builder.

Discovers ``@websocket_mapping`` methods on ``@rest_controller`` and
``@controller`` beans and creates Starlette ``WebSocketRoute`` objects,
following the same lazy-resolution pattern used by
:class:`~pyfly.web.adapters.starlette.controller.ControllerRegistrar`.
"""

from __future__ import annotations

import contextlib
import inspect
import logging
from typing import Any

from starlette.routing import WebSocketRoute
from starlette.websockets import WebSocket, WebSocketDisconnect

from pyfly.websocket.handler import WebSocketSession

_logger = logging.getLogger(__name__)

_CONTROLLER_STEREOTYPES = ("rest_controller", "controller")


class WebSocketRegistrar:
    """Discovers ``@websocket_mapping`` methods and builds Starlette WebSocket routes.

    For each controller:
    1. Reads ``@request_mapping`` base path from the class.
    2. Finds ``@websocket_mapping`` handler methods.
    3. Creates ``WebSocketRoute`` objects with lazy bean resolution.
    """

    def collect_routes(self, ctx: Any) -> list[WebSocketRoute]:
        """Collect all WebSocket routes from controllers in the application context.

        Bean resolution is deferred until the first WebSocket connection,
        matching the lazy pattern used for HTTP routes.
        """
        routes: list[WebSocketRoute] = []

        for cls, _reg in ctx.container._registrations.items():
            if getattr(cls, "__pyfly_stereotype__", "") not in _CONTROLLER_STEREOTYPES:
                continue

            base_path = getattr(cls, "__pyfly_request_mapping__", "")

            for attr_name in dir(cls):
                method_obj = getattr(cls, attr_name, None)
                if method_obj is None:
                    continue

                ws_mapping = getattr(method_obj, "__pyfly_ws_mapping__", None)
                if ws_mapping is None:
                    continue

                full_path = base_path + ws_mapping["path"]
                handler = self._make_lazy_handler(ctx, cls, attr_name)
                routes.append(WebSocketRoute(full_path, handler))

        return routes

    @staticmethod
    def _make_lazy_handler(ctx: Any, controller_cls: type, method_name: str) -> Any:
        """Create a Starlette WebSocket endpoint that lazily resolves the controller bean."""
        _cache: dict[str, Any] = {}

        async def lazy_ws_endpoint(websocket: WebSocket) -> None:
            if "instance" not in _cache:
                _cache["instance"] = ctx.get_bean(controller_cls)
                _cache["method"] = getattr(_cache["instance"], method_name)

            session = WebSocketSession(websocket)
            try:
                await _cache["method"](session)
            except WebSocketDisconnect:
                pass
            except Exception:
                # Unexpected handler failures are logged rather than swallowed
                # silently (audit #232).
                _logger.warning("websocket handler '%s' raised", method_name, exc_info=True)
            finally:
                # Invoke an on_disconnect lifecycle hook if the controller
                # defines one, so handlers can clean up (audit #232).
                on_disconnect = getattr(_cache["instance"], "on_disconnect", None)
                if callable(on_disconnect):
                    with contextlib.suppress(Exception):
                        result = on_disconnect(session)
                        if inspect.isawaitable(result):
                            await result

        return lazy_ws_endpoint
