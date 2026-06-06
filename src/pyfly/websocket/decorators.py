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
"""WebSocket mapping decorator for class-based controllers.

Mirrors the ``@get_mapping`` / ``@post_mapping`` pattern from
:mod:`pyfly.web.mappings`, but marks a method as a WebSocket handler
instead of an HTTP endpoint.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, TypeVar

F = TypeVar("F", bound=Callable[..., Any])


def websocket_mapping(path: str = "") -> Callable[[F], F]:
    """Mark a controller method as a WebSocket endpoint.

    The decorated method must accept a single ``WebSocketSession`` argument
    and manage the full connection lifecycle (accept, message loop, close).

    The controller instance is a process-wide singleton shared across **all**
    connections — keep per-connection state in local variables or on the
    ``WebSocketSession``, never on ``self`` (it would leak/race across clients).

    Usage::

        @rest_controller
        @request_mapping("/ws")
        class ChatController:

            @websocket_mapping("/chat")
            async def handle_chat(self, session: WebSocketSession) -> None:
                await session.accept()
                while True:
                    msg = await session.receive_text()
                    await session.send_text(f"echo: {msg}")
    """

    def decorator(func: F) -> F:
        func.__pyfly_ws_mapping__ = {"path": path}  # type: ignore[attr-defined]
        return func

    return decorator
