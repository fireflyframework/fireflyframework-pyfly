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
"""HTTP Basic authentication filter (RFC 7617).

Parses an ``Authorization: Basic`` header, resolves the user via a
:class:`~pyfly.security.user_details.UserDetailsService`, verifies the password
with a :class:`~pyfly.security.password.PasswordEncoder`, and populates the
request :class:`SecurityContext`.

``error_mode`` mirrors the OAuth2 resource-server filter:

* ``"anonymous"`` (default): bad/missing credentials yield an anonymous context
  and the request proceeds — the ``HttpSecurity`` gate decides.
* ``"401"``: present-but-invalid credentials are rejected here with
  ``401 Unauthorized`` and a ``WWW-Authenticate: Basic realm="…"`` challenge.
  Missing credentials still fall through to the gate.
"""

from __future__ import annotations

import base64
import binascii
import logging
from typing import cast

from anyio import to_thread
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from pyfly.container.ordering import HIGHEST_PRECEDENCE, order
from pyfly.context.request_context import RequestContext
from pyfly.security.context import SecurityContext
from pyfly.security.password import PasswordEncoder
from pyfly.security.user_details import UserDetailsService
from pyfly.web.filters import OncePerRequestFilter
from pyfly.web.ports.filter import CallNext

logger = logging.getLogger(__name__)

ERROR_MODE_ANONYMOUS = "anonymous"
ERROR_MODE_401 = "401"


@order(HIGHEST_PRECEDENCE + 215)
class HttpBasicAuthenticationFilter(OncePerRequestFilter):
    """Authenticates ``Authorization: Basic`` credentials against a UserDetailsService.

    Ordered just before the symmetric JWT ``SecurityFilter`` (``+220``) so it can
    establish a context for credential-based clients while leaving token-based
    auth to the later filters when no Basic header is present.
    """

    def __init__(
        self,
        user_details_service: UserDetailsService,
        password_encoder: PasswordEncoder,
        *,
        realm: str = "Realm",
        error_mode: str = ERROR_MODE_ANONYMOUS,
    ) -> None:
        self._users = user_details_service
        self._encoder = password_encoder
        self._realm = realm
        self._error_mode = error_mode if error_mode in (ERROR_MODE_ANONYMOUS, ERROR_MODE_401) else ERROR_MODE_ANONYMOUS

    async def do_filter(self, request: Request, call_next: CallNext) -> Response:
        credentials = self._extract_basic(request.headers.get("authorization", ""))

        if credentials is None:
            # No Basic credentials presented — leave any existing context alone and
            # default to anonymous so downstream filters/handlers always have one.
            if not hasattr(request.state, "security_context"):
                request.state.security_context = SecurityContext.anonymous()
            return cast(Response, await call_next(request))

        username, password = credentials
        context = await self._authenticate(username, password)

        if context is None:
            logger.warning("HTTP Basic authentication failed for user %r", username)
            if self._error_mode == ERROR_MODE_401:
                return self._challenge()
            context = SecurityContext.anonymous()

        request.state.security_context = context
        req_ctx = RequestContext.current()
        if req_ctx is not None:
            req_ctx.security_context = context
        return cast(Response, await call_next(request))

    async def _authenticate(self, username: str, password: str) -> SecurityContext | None:
        user = await self._users.load_user_by_username(username)
        if user is None or not user.enabled:
            return None
        # bcrypt/argon2 verification is CPU-bound; offload so we never block the loop.
        ok = await to_thread.run_sync(self._encoder.verify, password, user.password_hash)
        if not ok:
            return None
        return SecurityContext(user_id=user.username, roles=list(user.roles), permissions=list(user.permissions))

    @staticmethod
    def _extract_basic(auth_header: str) -> tuple[str, str] | None:
        """Return ``(username, password)`` from a Basic header, or ``None``.

        Returns ``("", "")``-style failures as ``None`` only for a *missing* or
        *non-Basic* header; a malformed Basic payload raises through to a 401 by
        returning a sentinel the caller treats as an auth failure.
        """
        parts = auth_header.split(" ", 1)
        if len(parts) != 2 or parts[0].lower() != "basic" or not parts[1].strip():
            return None
        try:
            decoded = base64.b64decode(parts[1].strip(), validate=True).decode("utf-8")
        except (binascii.Error, ValueError, UnicodeDecodeError):
            # Malformed credentials — treat as a (present) failed attempt.
            return ("\x00invalid", "")
        username, sep, password = decoded.partition(":")
        if not sep:
            return ("\x00invalid", "")
        return (username, password)

    def _challenge(self) -> Response:
        return JSONResponse(
            {"error": "invalid_credentials", "error_description": "Authentication failed."},
            status_code=401,
            headers={"WWW-Authenticate": f'Basic realm="{self._realm}"'},
        )
