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
"""CsrfFilter — double-submit cookie CSRF protection.

Implements the `double-submit cookie`_ pattern:

* **Safe methods** (GET, HEAD, OPTIONS, TRACE) — the filter passes the
  request through and sets (or refreshes) the ``XSRF-TOKEN`` cookie on
  the response so that JavaScript can read it.
* **Unsafe methods** (POST, PUT, DELETE, PATCH) — the filter compares the
  ``XSRF-TOKEN`` cookie value against the ``X-XSRF-TOKEN`` request header
  using a timing-safe comparison.  A mismatch (or a missing value) results
  in an HTTP 403 response.

**Bearer bypass**: requests that carry an ``Authorization: Bearer …`` header
are assumed to be API clients using stateless JWT authentication and are
therefore exempt from CSRF validation.

.. _double-submit cookie:
   https://cheatsheetseries.owasp.org/cheatsheets/Cross-Site_Request_Forgery_Prevention_Cheat_Sheet.html#double-submit-cookie
"""

from __future__ import annotations

from typing import Any

from starlette.responses import JSONResponse

from pyfly.container.ordering import HIGHEST_PRECEDENCE
from pyfly.security.csrf import (
    CSRF_COOKIE_NAME,
    CSRF_HEADER_NAME,
    SAFE_METHODS,
    generate_csrf_token,
    validate_csrf_token,
)
from pyfly.web.filters import OncePerRequestFilter
from pyfly.web.ports.filter import CallNext


def _set_csrf_cookie(response: Any, token: str, *, secure: bool = True) -> None:
    """Set the CSRF cookie on *response*."""
    response.set_cookie(
        key=CSRF_COOKIE_NAME,
        value=token,
        httponly=False,  # JS must be able to read the token
        samesite="lax",
        secure=secure,
        path="/",
    )


class CsrfFilter(OncePerRequestFilter):
    """Double-submit cookie CSRF filter.

    Ordering: runs after the request-context/correlation filters but before
    the JWT ``SecurityFilter`` (``HIGHEST_PRECEDENCE + 220``) so CSRF is
    validated before authentication/authorization.
    """

    __pyfly_order__ = HIGHEST_PRECEDENCE + 210

    exclude_patterns = ["/actuator/*", "/health", "/ready"]

    def __init__(self, *, cookie_gated: bool = True, cookie_secure: bool = True, form_field: str = "_csrf") -> None:
        # ``cookie_gated`` (default): only enforce CSRF on unsafe requests that
        # carry cookies — i.e. requests with ambient authority a cross-site forgery
        # could abuse. A request with no cookies (a stateless API client) has no
        # CSRF surface and is exempt, so CSRF can be on by default without breaking
        # token/stateless clients. Set ``cookie_gated=False`` for strict enforcement
        # of every unsafe request regardless of cookies.
        self._cookie_gated = cookie_gated
        self._cookie_secure = cookie_secure
        self._form_field = form_field

    async def do_filter(self, request: Any, call_next: CallNext) -> Any:
        method: str = request.method
        token = request.cookies.get(CSRF_COOKIE_NAME) or generate_csrf_token()
        state = getattr(request, "state", None)
        if state is not None:
            state.csrf_token = token
            state.csrf_field = self._form_field

        def response_token() -> str:
            session = getattr(state, "session", None) if state is not None else None
            if session is not None and (session.previous_id is not None or session.invalidated):
                return generate_csrf_token()
            return token

        # -----------------------------------------------------------------
        # Safe methods — pass through and set/refresh the CSRF cookie.
        # -----------------------------------------------------------------
        if method in SAFE_METHODS:
            response = await call_next(request)
            _set_csrf_cookie(response, response_token(), secure=self._cookie_secure)
            return response

        # -----------------------------------------------------------------
        # Bearer bypass — JWT API clients don't need CSRF.
        # -----------------------------------------------------------------
        auth_header: str | None = request.headers.get("authorization")
        if auth_header and auth_header.startswith("Bearer "):
            return await call_next(request)

        # -----------------------------------------------------------------
        # Cookie-gated exemption — no cookies means no ambient authority for a
        # cross-site request to abuse, so there is nothing to protect.
        # -----------------------------------------------------------------
        if self._cookie_gated and not request.cookies:
            return await call_next(request)

        # -----------------------------------------------------------------
        # Unsafe methods — validate double-submit cookie.
        # -----------------------------------------------------------------
        cookie_token: str | None = request.cookies.get(CSRF_COOKIE_NAME)
        header_token: str | None = request.headers.get(CSRF_HEADER_NAME)

        if not header_token and request.headers.get("content-type", "").split(";", 1)[0] in (
            "application/x-www-form-urlencoded",
            "multipart/form-data",
        ):
            from pyfly.web.adapters.starlette.form_binding import parse_form

            form = await parse_form(request)
            candidates = form.getlist(self._form_field)
            if len(candidates) == 1 and isinstance(candidates[0], str):
                header_token = candidates[0]

        if not cookie_token or not header_token:
            return JSONResponse({"error": "CSRF token missing"}, status_code=403)

        if not validate_csrf_token(cookie_token, header_token):
            return JSONResponse({"error": "CSRF token invalid"}, status_code=403)

        # Preserve open forms; rotate only when authentication changes the session.
        response = await call_next(request)
        _set_csrf_cookie(response, response_token(), secure=self._cookie_secure)
        return response
