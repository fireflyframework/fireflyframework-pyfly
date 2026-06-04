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
"""In-memory HTTP exchange recorder + filter for ``/actuator/httpexchanges``.

Mirrors Spring Boot's ``InMemoryHttpExchangeRepository`` — a bounded ring buffer
of the most recent request/response exchanges, populated by a WebFilter.
"""

from __future__ import annotations

import datetime
import time
from collections import deque
from typing import Any

from pyfly.web.filters import OncePerRequestFilter
from pyfly.web.ports.filter import CallNext

_CAPTURE_REQUEST_HEADERS = ("host", "user-agent", "accept", "content-type")
_CAPTURE_RESPONSE_HEADERS = ("content-type", "content-length")
_SENSITIVE_HEADERS = ("authorization", "cookie", "set-cookie", "proxy-authorization")


class HttpExchangeRecorder:
    """Bounded, newest-first store of recent HTTP exchanges."""

    def __init__(self, capacity: int = 100) -> None:
        self._exchanges: deque[dict[str, Any]] = deque(maxlen=capacity)

    def record(self, exchange: dict[str, Any]) -> None:
        self._exchanges.append(exchange)

    def recent(self) -> list[dict[str, Any]]:
        # Most recent first, matching Spring Boot.
        return list(reversed(self._exchanges))

    def clear(self) -> None:
        self._exchanges.clear()


def _headers(raw: Any, keep: tuple[str, ...]) -> dict[str, list[str]]:
    """Capture a safe subset of headers (Spring shape: name -> [values]), masking secrets."""
    result: dict[str, list[str]] = {}
    try:
        items = raw.items()
    except AttributeError:
        return result
    for name, value in items:
        lname = name.lower()
        if lname in _SENSITIVE_HEADERS:
            result[lname] = ["******"]
        elif lname in keep:
            result[lname] = [value]
    return result


class HttpExchangeRecorderFilter(OncePerRequestFilter):
    """Records each request/response into the shared :class:`HttpExchangeRecorder`."""

    __pyfly_order__ = -90  # Just after the metrics filter.

    exclude_patterns = ["/actuator/prometheus", "/admin/api/sse/*"]

    def __init__(self, recorder: HttpExchangeRecorder) -> None:
        self._recorder = recorder

    async def do_filter(self, request: Any, call_next: CallNext) -> Any:
        start = time.perf_counter()
        status_code = 500
        response = None
        try:
            response = await call_next(request)
            status_code = int(getattr(response, "status_code", 200))
            return response
        finally:
            took = time.perf_counter() - start
            self._recorder.record(
                {
                    "timestamp": datetime.datetime.now(datetime.UTC).isoformat(),
                    "request": {
                        "method": request.method,
                        "uri": str(request.url),
                        "headers": _headers(request.headers, _CAPTURE_REQUEST_HEADERS),
                    },
                    "response": {
                        "status": status_code,
                        "headers": _headers(getattr(response, "headers", {}), _CAPTURE_RESPONSE_HEADERS),
                    },
                    "timeTaken": f"PT{took:.3f}S",
                }
            )
