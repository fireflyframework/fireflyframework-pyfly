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
"""WebSocket end-to-end tests using Starlette's TestClient.

These tests exercise real WebSocket round-trips through the
``WebSocketRegistrar`` and ``WebSocketSession`` — not a fake socket.
The ASGI app is built by wiring an echo controller through the registrar,
exactly as the framework would do at runtime.
"""

from __future__ import annotations

from typing import Any

from starlette.applications import Starlette
from starlette.routing import WebSocketRoute
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from pyfly.websocket.handler import WebSocketSession

# ---------------------------------------------------------------------------
# Echo controller (mirrors _EchoController from test_ws_lifecycle.py)
# ---------------------------------------------------------------------------


class _EchoController:
    """Simple echo controller that records lifecycle events."""

    def __init__(self) -> None:
        self.events: list[str] = []

    async def chat(self, session: WebSocketSession) -> None:
        await session.accept()
        self.events.append("accept")
        try:
            while True:
                msg = await session.receive_text()
                await session.send_text(f"echo:{msg}")
        except WebSocketDisconnect:
            pass

    async def on_disconnect(self, session: WebSocketSession) -> None:
        self.events.append("disconnect")


# ---------------------------------------------------------------------------
# App builder via WebSocketRegistrar's _make_lazy_handler
# ---------------------------------------------------------------------------


def _make_app(controller: _EchoController) -> Starlette:
    """Build a Starlette app wired through WebSocketRegistrar's lazy handler.

    Uses the same ``_make_lazy_handler`` factory the registrar uses at runtime,
    so this is a real end-to-end test through the registrar — not a bypass.
    """
    from pyfly.websocket.adapters.starlette import WebSocketRegistrar

    # Minimal fake context that satisfies get_bean() used by _make_lazy_handler
    class _FakeCtx:
        def get_bean(self, cls: type) -> Any:
            return controller

    handler = WebSocketRegistrar._make_lazy_handler(_FakeCtx(), type(controller), "chat")
    return Starlette(routes=[WebSocketRoute("/ws/echo", handler)])


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestWebSocketE2E:
    def test_echo_single_message(self) -> None:
        """One send/receive pair through a real WS connection."""
        controller = _EchoController()
        app = _make_app(controller)

        with TestClient(app) as client, client.websocket_connect("/ws/echo") as ws:
            ws.send_text("hello")
            assert ws.receive_text() == "echo:hello"

        assert "accept" in controller.events

    def test_echo_multiple_messages(self) -> None:
        """Multiple round-trips in a single connection."""
        controller = _EchoController()
        app = _make_app(controller)

        messages = ["alpha", "beta", "gamma"]

        with TestClient(app) as client, client.websocket_connect("/ws/echo") as ws:
            for msg in messages:
                ws.send_text(msg)
                assert ws.receive_text() == f"echo:{msg}"

    def test_disconnect_callback_fires(self) -> None:
        """``on_disconnect`` must run after the connection closes (accepted path)."""
        controller = _EchoController()
        app = _make_app(controller)

        with TestClient(app) as client, client.websocket_connect("/ws/echo") as ws:
            ws.send_text("ping")
            ws.receive_text()
            # Connection closed here; on_disconnect should have been called.

        assert controller.events == ["accept", "disconnect"]

    def test_clean_close_from_client(self) -> None:
        """Client closes the WebSocket; handler terminates without raising."""
        controller = _EchoController()
        app = _make_app(controller)

        with TestClient(app) as client, client.websocket_connect("/ws/echo") as ws:
            ws.send_text("one")
            assert ws.receive_text() == "echo:one"
            ws.send_text("two")
            assert ws.receive_text() == "echo:two"
            # Exiting the context manager closes the socket cleanly.

        # Both lifecycle events should have fired.
        assert "accept" in controller.events
        assert "disconnect" in controller.events
