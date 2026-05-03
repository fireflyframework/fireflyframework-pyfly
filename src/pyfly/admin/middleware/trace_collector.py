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
"""HTTP trace collector -- WebFilter that captures request/response metadata."""

from __future__ import annotations

import contextlib
import time
from collections import deque
from datetime import UTC, datetime
from typing import Any

from pyfly.web.filters import OncePerRequestFilter
from pyfly.web.ports.filter import CallNext


class TraceCollectorFilter(OncePerRequestFilter):
    """Captures HTTP request/response traces for the admin dashboard.

    Stores traces in a fixed-size ring buffer (deque with maxlen).
    Excludes admin and actuator paths by default.
    """

    exclude_patterns = ["/admin/*", "/actuator/*"]

    def __init__(self, max_traces: int = 500) -> None:
        self._traces: deque[dict[str, Any]] = deque(maxlen=max_traces)

    async def do_filter(self, request: Any, call_next: CallNext) -> Any:
        start = time.monotonic()
        response = await call_next(request)
        duration_ms = round((time.monotonic() - start) * 1000, 2)

        # Extract additional request metadata
        query_string = str(request.url.query) if request.url.query else ""
        client_host = request.client.host if request.client else None
        content_type = request.headers.get("content-type")
        user_agent = request.headers.get("user-agent", "")
        if len(user_agent) > 100:
            user_agent = user_agent[:100]

        # Extract response content-length if available
        content_length: int | None = None
        if hasattr(response, "headers"):
            cl = response.headers.get("content-length")
            if cl is not None:
                with contextlib.suppress(ValueError, TypeError):
                    content_length = int(cl)

        self._traces.append(
            {
                "timestamp": datetime.now(UTC).isoformat(),
                "method": request.method,
                "path": request.url.path,
                "query_string": query_string,
                "status": response.status_code,
                "duration_ms": duration_ms,
                "client_host": client_host,
                "content_type": content_type,
                "user_agent": user_agent,
                "content_length": content_length,
            }
        )
        return response

    def get_traces(self) -> list[dict[str, Any]]:
        return list(self._traces)

    def clear_traces(self) -> None:
        self._traces.clear()
