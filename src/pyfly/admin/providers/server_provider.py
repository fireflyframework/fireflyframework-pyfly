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
    """Provides ASGI server runtime info for the admin dashboard."""

    def __init__(self, server: Any) -> None:
        self._server = server

    async def get_server_info(self) -> dict[str, Any]:
        if self._server is None:
            return {
                "name": "unknown",
                "version": "unknown",
                "workers": 0,
                "event_loop": "unknown",
                "http_protocol": "unknown",
                "host": "unknown",
                "port": 0,
            }

        info = self._server.server_info
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
