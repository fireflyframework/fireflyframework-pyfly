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
"""Outbound port: application server interface."""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from pyfly.server.types import ServerInfo


@runtime_checkable
class ApplicationServerPort(Protocol):
    """Abstract application server interface.

    Any ASGI server (Granian, Uvicorn, Hypercorn) must implement this protocol.
    Analogous to Spring Boot's WebServer interface.
    """

    def serve(self, app: str | Any, config: Any) -> None:
        """Start the server (blocking). Called from ``pyfly run``."""
        ...

    async def serve_async(self, app: str | Any, config: Any) -> None:
        """Start the server (async). For embedding in existing event loops."""
        ...

    def shutdown(self) -> None:
        """Request graceful shutdown."""
        ...

    @property
    def server_info(self) -> ServerInfo:
        """Return runtime info (name, version, workers, event loop, protocol)."""
        ...
