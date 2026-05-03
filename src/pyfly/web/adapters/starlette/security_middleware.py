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
"""Security middleware for automatic JWT authentication — pure ASGI."""

from __future__ import annotations

import logging
from collections.abc import Sequence

from starlette.requests import Request
from starlette.types import ASGIApp, Receive, Scope, Send

from pyfly.security.context import SecurityContext
from pyfly.security.jwt import JWTService

logger = logging.getLogger(__name__)


class SecurityMiddleware:
    """Extracts Bearer token from Authorization header and populates request.state.security_context.

    For missing or invalid tokens, sets an anonymous SecurityContext (unauthenticated).
    Configurable exclude_paths for public endpoints that skip processing.

    Uses raw ASGI protocol instead of ``BaseHTTPMiddleware`` to avoid the
    ``anyio`` dependency that causes ``ModuleNotFoundError`` with Granian.
    """

    def __init__(
        self,
        app: ASGIApp,
        jwt_service: JWTService,
        exclude_paths: Sequence[str] = (),
    ) -> None:
        self.app = app
        self._jwt_service = jwt_service
        self._exclude_paths = set(exclude_paths)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request = Request(scope, receive, send)

        # Skip excluded paths (docs, health, etc.)
        if request.url.path in self._exclude_paths:
            security_context = SecurityContext.anonymous()
        else:
            # Extract Bearer token
            auth_header = request.headers.get("authorization", "")
            if auth_header.startswith("Bearer "):
                token = auth_header[7:]  # len("Bearer ") == 7
                try:
                    security_context = self._jwt_service.to_security_context(token)
                except Exception:
                    logger.debug("Invalid JWT token, using anonymous context")
                    security_context = SecurityContext.anonymous()
            else:
                security_context = SecurityContext.anonymous()

        request.state.security_context = security_context

        # Bridge to RequestContext for @pre_authorize / @post_authorize
        from pyfly.context.request_context import RequestContext

        req_ctx = RequestContext.current()
        if req_ctx is not None:
            req_ctx.security_context = security_context

        await self.app(scope, receive, send)
