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
"""Request logging middleware — pure ASGI, logs method, path, status, and duration."""

from __future__ import annotations

import time
from typing import Any

from starlette.requests import Request
from starlette.types import ASGIApp, Receive, Scope, Send

from pyfly.logging import get_logger

logger = get_logger("pyfly.web")


class RequestLoggingMiddleware:
    """Logs HTTP method, path, status code, and duration for each request.

    Uses raw ASGI protocol instead of ``BaseHTTPMiddleware`` to avoid the
    ``anyio`` dependency that causes ``ModuleNotFoundError`` with Granian.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request = Request(scope, receive, send)
        start = time.perf_counter()
        tx_id = getattr(request.state, "transaction_id", None)
        status_code = 500

        async def send_with_logging(message: Any) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = message["status"]
            await send(message)

        try:
            await self.app(scope, receive, send_with_logging)
        except Exception as exc:
            duration_ms = (time.perf_counter() - start) * 1000
            logger.error(
                "http_request_failed",
                method=request.method,
                path=request.url.path,
                duration_ms=round(duration_ms, 2),
                transaction_id=tx_id,
                error=str(exc),
                error_type=type(exc).__name__,
            )
            raise

        duration_ms = (time.perf_counter() - start) * 1000
        logger.info(
            "http_request",
            method=request.method,
            path=request.url.path,
            status_code=status_code,
            duration_ms=round(duration_ms, 2),
            transaction_id=tx_id,
        )
