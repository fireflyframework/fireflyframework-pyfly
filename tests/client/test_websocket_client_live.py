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
"""Live WebSocket round-trip tests using a local echo server (no Docker).

The echo server runs in-process on an ephemeral port using ``websockets.serve``.
Tests skip cleanly when websockets is not installed.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator

import pytest

websockets = pytest.importorskip("websockets")

from websockets.asyncio.server import serve  # noqa: E402 – after importorskip

from pyfly.client.protocols.websocket_client import WebSocketClientBuilder  # noqa: E402


async def _echo_handler(websocket: object) -> None:
    """Echo every incoming message back to the sender."""
    ws = websocket  # type: ignore[assignment]
    async for message in ws:
        await ws.send(message)


@pytest.fixture()
async def echo_server() -> AsyncGenerator[int, None]:
    """Start a local echo WebSocket server on an OS-assigned ephemeral port; yield its port."""
    # Bind to port 0 and let the server keep the socket (no release-then-rebind TOCTOU window).
    server = await serve(_echo_handler, "127.0.0.1", 0)
    port: int = server.sockets[0].getsockname()[1]
    try:
        yield port
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_websocket_connect_echo(echo_server: int) -> None:
    """WebSocketClient.connect() returns a live connection; send/recv works."""
    port = echo_server
    client = WebSocketClientBuilder().with_url(f"ws://127.0.0.1:{port}").build()

    conn = await asyncio.wait_for(client.connect(), timeout=5.0)
    try:
        await conn.send("hello")
        reply = await asyncio.wait_for(conn.recv(), timeout=5.0)
        assert reply == "hello"
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_websocket_stream_echo(echo_server: int) -> None:
    """WebSocketClient.stream(send=[...]) yields echoed messages."""
    port = echo_server
    client = WebSocketClientBuilder().with_url(f"ws://127.0.0.1:{port}").build()

    received: list[str] = []
    async with asyncio.timeout(5.0):
        async for msg in client.stream(send=["hi"]):
            received.append(str(msg))
            break  # consume exactly one message then exit

    assert received == ["hi"]
