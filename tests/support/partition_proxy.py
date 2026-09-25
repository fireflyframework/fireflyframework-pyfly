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
"""A TCP proxy in front of a real database server that can stop forwarding: a network partition.

Tests put it between the application and a matrix server to reproduce a database that goes silent
(C021): the process is alive, established connections stay open, and nothing comes back.

Usage::

    upstream = make_url(relational_backend.url)
    proxy = PartitionProxy(upstream.host, upstream.port)
    port = await proxy.start()
    url = upstream.set(host="127.0.0.1", port=port)
    ...
    proxy.partition()   # stop forwarding in both directions
    proxy.heal()        # forward again, held-back bytes first
    await proxy.close()
"""

from __future__ import annotations

import asyncio
import contextlib


class PartitionProxy:
    """A TCP proxy to a server that can stop forwarding, like a network partition.

    While partitioned it still accepts connections (the SYN reaches the host) but forwards no byte in
    either direction; :meth:`heal` delivers what was held back. ``connections`` counts the client
    connections it accepted.
    """

    def __init__(self, host: str, port: int) -> None:
        self._upstream = (host, port)
        self._flowing = asyncio.Event()
        self._flowing.set()
        self._tasks: set[asyncio.Task[None]] = set()
        self._writers: list[asyncio.StreamWriter] = []
        self._server: asyncio.Server | None = None
        self.connections = 0

    async def start(self) -> int:
        """Start listening on an ephemeral local port and return it."""
        self._server = await asyncio.start_server(self._accept, "127.0.0.1", 0)
        return int(self._server.sockets[0].getsockname()[1])

    def partition(self) -> None:
        """Stop forwarding."""
        self._flowing.clear()

    def heal(self) -> None:
        """Forward again, held-back bytes first."""
        self._flowing.set()

    async def close(self) -> None:
        """Close every proxied connection and stop listening."""
        self.heal()
        for task in list(self._tasks):
            task.cancel()
        for writer in self._writers:
            writer.close()
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()

    async def _accept(self, client_reader: asyncio.StreamReader, client_writer: asyncio.StreamWriter) -> None:
        self.connections += 1
        server_reader, server_writer = await asyncio.open_connection(*self._upstream)
        self._writers += [client_writer, server_writer]
        for source, target in ((client_reader, server_writer), (server_reader, client_writer)):
            task = asyncio.ensure_future(self._pipe(source, target))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)

    async def _pipe(self, source: asyncio.StreamReader, target: asyncio.StreamWriter) -> None:
        with contextlib.suppress(OSError, asyncio.IncompleteReadError):
            while data := await source.read(65536):
                await self._flowing.wait()
                target.write(data)
                await target.drain()
        target.close()
