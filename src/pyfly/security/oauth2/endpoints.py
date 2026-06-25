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
"""OAuth2 Authorization Server HTTP endpoints.

Exposes, as Starlette routes, the token endpoint plus the standard OAuth2
management endpoints:

* ``POST /oauth2/token``       — issue tokens (RFC 6749)
* ``POST /oauth2/introspect``  — token introspection (RFC 7662), client-authenticated
* ``POST /oauth2/revoke``      — token revocation (RFC 7009), client-authenticated
* ``GET  /oauth2/jwks``        — public JWK Set (for asymmetric signing)
"""

from __future__ import annotations

import base64
import binascii
from typing import Any

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from pyfly.kernel.exceptions import SecurityException
from pyfly.security.oauth2.authorization_server import AuthorizationServer

# OAuth2 error codes that map to a 401 (client authentication failed); the rest
# are request/grant errors returned as 400 (RFC 6749 §5.2).
_UNAUTHORIZED_ERRORS = {"INVALID_CLIENT"}


class AuthorizationServerEndpoints:
    """Builds Starlette routes that expose an :class:`AuthorizationServer`."""

    def __init__(self, server: AuthorizationServer) -> None:
        self._server = server

    def routes(self) -> list[Route]:
        return [
            Route("/oauth2/token", self._token, methods=["POST"]),
            Route("/oauth2/introspect", self._introspect, methods=["POST"]),
            Route("/oauth2/revoke", self._revoke, methods=["POST"]),
            Route("/oauth2/jwks", self._jwks, methods=["GET"]),
        ]

    # -- token endpoint ----------------------------------------------------

    async def _token(self, request: Request) -> Response:
        form = await request.form()
        client_id, client_secret = self._client_credentials(request, form)
        try:
            result = await self._server.token(
                grant_type=str(form.get("grant_type", "")),
                client_id=client_id,
                client_secret=client_secret,
                scope=str(form.get("scope", "")),
                refresh_token=(str(form["refresh_token"]) if form.get("refresh_token") else None),
            )
        except SecurityException as exc:
            return self._error(exc)
        return JSONResponse(result)

    # -- introspection (RFC 7662) -----------------------------------------

    async def _introspect(self, request: Request) -> Response:
        form = await request.form()
        if not self._authenticate(request, form):
            return self._error(SecurityException("Invalid client", code="INVALID_CLIENT"))
        token = str(form.get("token", ""))
        if not token:
            return JSONResponse({"active": False})
        return JSONResponse(await self._server.introspect(token))

    # -- revocation (RFC 7009) --------------------------------------------

    async def _revoke(self, request: Request) -> Response:
        form = await request.form()
        if not self._authenticate(request, form):
            return self._error(SecurityException("Invalid client", code="INVALID_CLIENT"))
        token = str(form.get("token", ""))
        if token:
            await self._server.revoke(token)
        # RFC 7009 §2.2: the AS responds 200 regardless of whether the token existed.
        return JSONResponse({})

    # -- JWKS --------------------------------------------------------------

    async def _jwks(self, request: Request) -> Response:
        return JSONResponse(self._server.jwks())

    # -- helpers -----------------------------------------------------------

    @staticmethod
    def _client_credentials(request: Request, form: Any) -> tuple[str, str]:
        """Resolve client credentials from HTTP Basic or form params (post)."""
        basic = AuthorizationServerEndpoints._basic_auth(request)
        if basic is not None:
            return basic
        return str(form.get("client_id", "")), str(form.get("client_secret", ""))

    def _authenticate(self, request: Request, form: Any) -> bool:
        client_id, client_secret = self._client_credentials(request, form)
        return self._server.authenticate_client(client_id, client_secret) is not None

    @staticmethod
    def _basic_auth(request: Request) -> tuple[str, str] | None:
        header = request.headers.get("authorization", "")
        parts = header.split(" ", 1)
        if len(parts) != 2 or parts[0].lower() != "basic":
            return None
        try:
            decoded = base64.b64decode(parts[1].strip(), validate=True).decode("utf-8")
        except (binascii.Error, ValueError, UnicodeDecodeError):
            return None
        cid, sep, secret = decoded.partition(":")
        return (cid, secret) if sep else None

    @staticmethod
    def _error(exc: SecurityException) -> JSONResponse:
        code = getattr(exc, "code", "INVALID_REQUEST") or "INVALID_REQUEST"
        status = 401 if code in _UNAUTHORIZED_ERRORS else 400
        headers = {"WWW-Authenticate": 'Basic realm="oauth2"'} if status == 401 else None
        return JSONResponse({"error": code.lower(), "error_description": str(exc)}, status_code=status, headers=headers)
