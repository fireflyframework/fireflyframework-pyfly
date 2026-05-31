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
"""Server data provider — ASGI server runtime info and metrics."""

from __future__ import annotations

import os
import platform
import time
from typing import Any


class ServerProvider:
    """Provides ASGI server runtime info for the admin dashboard.

    The server adapter (``ApplicationServerPort``) is instantiated during
    ``ApplicationContext.start()`` — after the web app is assembled — so it is
    resolved lazily from the context on each request rather than captured at
    construction time. An explicit ``server`` argument still takes precedence
    when supplied (e.g. in tests).
    """

    def __init__(self, server: Any = None, context: Any = None) -> None:
        self._server = server
        self._context = context

    def _resolve_server(self) -> Any:
        if self._server is not None:
            return self._server
        if self._context is None:
            return None
        try:
            from pyfly.server.ports.outbound import ApplicationServerPort
        except ImportError:
            return None
        for _cls, reg in self._context.container._registrations.items():
            if reg.instance is not None and isinstance(reg.instance, ApplicationServerPort):
                return reg.instance
        return None

    async def get_server_info(self) -> dict[str, Any]:
        server = self._resolve_server()
        if server is None:
            return {
                "name": "unknown",
                "version": "unknown",
                "workers": 0,
                "event_loop": "unknown",
                "http_protocol": "unknown",
                "host": "unknown",
                "port": 0,
            }

        info = server.server_info
        return {
            "name": info.name,
            "version": info.version,
            "workers": info.workers,
            "event_loop": info.event_loop,
            "http_protocol": info.http_protocol,
            "host": info.host,
            "port": info.port,
            "platform": {
                "system": platform.system(),
                "machine": platform.machine(),
                "python": platform.python_version(),
                "cpu_count": os.cpu_count(),
            },
            "timestamp": time.time(),
        }
