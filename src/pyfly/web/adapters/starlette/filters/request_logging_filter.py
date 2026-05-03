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
"""Request logging filter — logs method, path, status, and duration."""

from __future__ import annotations

import logging
import time
from typing import cast

from starlette.requests import Request
from starlette.responses import Response

from pyfly.container.ordering import HIGHEST_PRECEDENCE, order
from pyfly.web.filters import OncePerRequestFilter
from pyfly.web.ports.filter import CallNext

try:
    import structlog

    logger = structlog.get_logger("pyfly.web")
except ImportError:
    logger = logging.getLogger("pyfly.web")


@order(HIGHEST_PRECEDENCE + 200)
class RequestLoggingFilter(OncePerRequestFilter):
    """Logs HTTP method, path, status code, and duration for each request."""

    async def do_filter(self, request: Request, call_next: CallNext) -> Response:
        start = time.perf_counter()
        tx_id = getattr(request.state, "transaction_id", None)

        try:
            response = cast(Response, await call_next(request))
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
            status_code=response.status_code,
            duration_ms=round(duration_ms, 2),
            transaction_id=tx_id,
        )
        return response
