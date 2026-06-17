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
"""Outbound port: best-effort server runtime statistics.

:class:`ServerStatsPort` is an *optional* capability a server adapter may
implement to expose live, server-native runtime numbers (true socket
connections, total requests served) that the ASGI layer cannot see. It mirrors
the :class:`~pyfly.observability.ports.MetricsRecorder` / ``NoOp`` philosophy:
**a ``None`` field means "this server cannot report it"**, so the binder that
consumes a sample never has to guard per-field.

Only servers whose adapter owns the running server object in-process (Uvicorn via
``serve_async``) can populate ``active_connections`` / ``total_requests``. On the
forked ``pyfly run`` production path (``uvicorn.run(workers=N)``), the worker's
adapter bean is not the object running the server, so ``sample()`` returns those
fields as ``None`` and the pure-ASGI server-metrics middleware supplies the
uniform connection/in-flight numbers instead.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class ServerStats:
    """A point-in-time sample of a server's runtime state.

    ``active_connections`` and ``total_requests`` are ``None`` when the server
    exposes no Python-side handle to read them (Granian's Rust runtime,
    Hypercorn's ``serve`` returning nothing, or any forked worker).
    """

    workers: int
    server_uptime_seconds: float
    worker_pid: int
    active_connections: int | None = None
    total_requests: int | None = None


@runtime_checkable
class ServerStatsPort(Protocol):
    """Optional server-statistics capability implemented by server adapters.

    Analogous to Micrometer's ``TomcatMetrics`` binder source — but best-effort:
    a server that cannot introspect itself still satisfies the protocol by
    returning ``None`` fields (or ``None`` from :meth:`sample`).
    """

    def sample(self) -> ServerStats | None:
        """Return a current :class:`ServerStats`, or ``None`` if unavailable."""
        ...

    def on_serve_start(self) -> None:
        """Record the server-bind moment (basis for ``server_uptime_seconds``)."""
        ...

    def on_serve_stop(self) -> None:
        """Release any retained server handle/reference."""
        ...
