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
"""Security headers filter — adds OWASP-recommended response headers."""

from __future__ import annotations

from typing import cast

from starlette.requests import Request
from starlette.responses import Response

from pyfly.container.ordering import HIGHEST_PRECEDENCE, order
from pyfly.web.filters import OncePerRequestFilter
from pyfly.web.ports.filter import CallNext
from pyfly.web.security_headers import SecurityHeadersConfig


@order(HIGHEST_PRECEDENCE + 300)
class SecurityHeadersFilter(OncePerRequestFilter):
    """Adds security headers to every response."""

    def __init__(self, config: SecurityHeadersConfig | None = None) -> None:
        self._config = config or SecurityHeadersConfig()
        # Precompute the encoded (name, value) header pairs ONCE — they are static config, so
        # there is no need to re-encode or re-test the optional headers on every request.
        cfg = self._config
        pairs: list[tuple[str, str]] = [
            ("x-content-type-options", cfg.x_content_type_options),
            ("x-frame-options", cfg.x_frame_options),
            ("strict-transport-security", cfg.strict_transport_security),
            ("x-xss-protection", cfg.x_xss_protection),
            ("referrer-policy", cfg.referrer_policy),
        ]
        if cfg.content_security_policy is not None:
            pairs.append(("content-security-policy", cfg.content_security_policy))
        if cfg.permissions_policy is not None:
            pairs.append(("permissions-policy", cfg.permissions_policy))
        self._encoded: list[tuple[bytes, bytes]] = [
            (name.encode("latin-1"), value.encode("latin-1")) for name, value in pairs
        ]

    async def do_filter(self, request: Request, call_next: CallNext) -> Response:
        response = cast(Response, await call_next(request))
        # Bulk-append the precomputed headers in one extend rather than N MutableHeaders
        # setitems (each re-scans the raw list). Security headers don't pre-exist on framework
        # responses, so appending is correct.
        response.raw_headers.extend(self._encoded)
        return response
