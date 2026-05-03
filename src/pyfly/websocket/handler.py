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
"""WebSocket handler protocol and session wrapper.

Defines the framework-agnostic ``WebSocketHandler`` protocol that controller
methods implement, and the ``WebSocketSession`` wrapper that provides a
clean async API over the underlying WebSocket connection.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class WebSocketHandler(Protocol):
    """Protocol for WebSocket handler lifecycle methods.

    Implement any combination of these methods on a controller to handle
    WebSocket events.  All methods are optional — unimplemented hooks are
    simply skipped.
    """

    async def on_connect(self, session: WebSocketSession) -> None:
        """Called when a client initiates a WebSocket connection.

        The connection is *not* yet accepted — call ``await session.accept()``
        to complete the handshake.
        """
        ...

    async def on_message(self, session: WebSocketSession, data: str) -> None:
        """Called when a text message is received from the client."""
        ...

    async def on_disconnect(self, session: WebSocketSession) -> None:
        """Called when the WebSocket connection is closed."""
        ...


class WebSocketSession:
    """Framework-agnostic wrapper around a raw WebSocket connection.

    Provides a clean async interface for accepting, sending, receiving,
    and closing WebSocket connections.  Currently backed by Starlette's
    ``WebSocket``, but the public API avoids leaking implementation details.
    """

    def __init__(self, raw: Any) -> None:
        self._ws = raw

    @property
    def path_params(self) -> dict[str, Any]:
        """Path parameters extracted from the WebSocket URL."""
        return dict(self._ws.path_params)

    @property
    def query_params(self) -> Any:
        """Query parameters from the WebSocket URL."""
        return self._ws.query_params

    @property
    def headers(self) -> Any:
        """Request headers from the WebSocket handshake."""
        return self._ws.headers

    async def accept(self, subprotocol: str | None = None) -> None:
        """Accept the WebSocket connection handshake."""
        await self._ws.accept(subprotocol=subprotocol)

    async def send_text(self, data: str) -> None:
        """Send a text message to the client."""
        await self._ws.send_text(data)

    async def send_json(self, data: Any, mode: str = "text") -> None:
        """Send a JSON-serializable object to the client."""
        await self._ws.send_json(data, mode=mode)

    async def send_bytes(self, data: bytes) -> None:
        """Send binary data to the client."""
        await self._ws.send_bytes(data)

    async def receive_text(self) -> str:
        """Receive a text message from the client."""
        result: str = await self._ws.receive_text()
        return result

    async def receive_json(self, mode: str = "text") -> Any:
        """Receive and decode a JSON message from the client."""
        return await self._ws.receive_json(mode=mode)

    async def receive_bytes(self) -> bytes:
        """Receive binary data from the client."""
        result: bytes = await self._ws.receive_bytes()
        return result

    async def close(self, code: int = 1000, reason: str | None = None) -> None:
        """Close the WebSocket connection."""
        await self._ws.close(code=code, reason=reason)
