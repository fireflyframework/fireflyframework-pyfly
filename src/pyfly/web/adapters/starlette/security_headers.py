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
"""Security headers middleware for Starlette — pure ASGI."""

from __future__ import annotations

from typing import Any

from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Receive, Scope, Send

from pyfly.web.security_headers import SecurityHeadersConfig


class SecurityHeadersMiddleware:
    """Adds security headers to every HTTP response.

    Uses raw ASGI protocol instead of ``BaseHTTPMiddleware`` to avoid the
    ``anyio`` dependency that causes ``ModuleNotFoundError`` with Granian.
    """

    def __init__(self, app: ASGIApp, config: SecurityHeadersConfig | None = None) -> None:
        self.app = app
        self._config = config or SecurityHeadersConfig()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        cfg = self._config

        async def send_with_headers(message: Any) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                present = {name.lower() for name, _ in message.get("headers", [])}
                defaults = (
                    ("x-content-type-options", cfg.x_content_type_options),
                    ("x-frame-options", cfg.x_frame_options),
                    ("strict-transport-security", cfg.strict_transport_security),
                    ("x-xss-protection", cfg.x_xss_protection),
                    ("referrer-policy", cfg.referrer_policy),
                    ("content-security-policy", cfg.content_security_policy),
                    ("permissions-policy", cfg.permissions_policy),
                )
                for name, value in defaults:
                    if value is not None and name.encode("latin-1") not in present:
                        headers[name] = value
            await send(message)

        await self.app(scope, receive, send_with_headers)
