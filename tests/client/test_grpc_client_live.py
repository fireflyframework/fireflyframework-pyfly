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
"""Live gRPC round-trip tests using an in-process server (no Docker, no codegen).

Approach
--------
We spin up a ``grpc.aio.server()`` with a manually-registered unary-unary
method handler (NO proto codegen) using ``grpc.unary_unary_rpc_method_handler``
and ``grpc.method_handlers_generic_handler``.  The handler simply echoes the
raw bytes back to the caller.  We then exercise ``GrpcClientBuilder`` to open a
channel and call the same method.

Tests skip cleanly when grpcio is not installed.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from typing import Any

import pytest

grpc = pytest.importorskip("grpc")

from grpc import aio  # noqa: E402 – after importorskip

from pyfly.client.protocols.grpc_client import GrpcClientBuilder  # noqa: E402

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _echo_handler(request: bytes, context: Any) -> bytes:  # type: ignore[return]
    """Unary-unary echo: return the raw request bytes unchanged."""
    return request


# ---------------------------------------------------------------------------
# Fixture: in-process gRPC echo server
# ---------------------------------------------------------------------------


@pytest.fixture()
async def grpc_echo_server() -> AsyncGenerator[int, None]:
    """Start an in-process gRPC server on an ephemeral port; yield the port."""
    server = aio.server()

    handler = grpc.unary_unary_rpc_method_handler(_echo_handler)
    generic_handler = grpc.method_handlers_generic_handler("svc", {"Echo": handler})
    server.add_generic_rpc_handlers([generic_handler])

    # add_insecure_port returns the actual bound port when 0 is specified.
    port: int = server.add_insecure_port("127.0.0.1:0")
    await server.start()

    try:
        yield port
    finally:
        # Grace period of 0 means "stop immediately".
        await asyncio.wait_for(server.stop(0), timeout=5.0)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_grpc_builder_channel_echo(grpc_echo_server: int) -> None:
    """GrpcClientBuilder.channel() creates a working channel; raw bytes echo."""
    port = grpc_echo_server
    channel = GrpcClientBuilder().with_target(f"127.0.0.1:{port}").channel()

    stub = channel.unary_unary("/svc/Echo")
    try:
        response = await asyncio.wait_for(stub(b"hello grpc"), timeout=5.0)
        assert response == b"hello grpc"
    finally:
        await channel.close()


@pytest.mark.asyncio
async def test_grpc_echo_multiple_payloads(grpc_echo_server: int) -> None:
    """Multiple sequential calls each return the correct echoed payload."""
    port = grpc_echo_server
    channel = GrpcClientBuilder().with_target(f"127.0.0.1:{port}").channel()

    stub = channel.unary_unary("/svc/Echo")
    try:
        for payload in (b"alpha", b"beta", b"\x00\xff\xfe"):
            response = await asyncio.wait_for(stub(payload), timeout=5.0)
            assert response == payload
    finally:
        await channel.close()
