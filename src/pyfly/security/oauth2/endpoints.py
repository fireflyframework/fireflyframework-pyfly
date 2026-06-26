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
from secrets import compare_digest as _consteq
from typing import Any
from urllib.parse import urlencode

from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse, Response
from starlette.routing import Route

from pyfly.kernel.exceptions import SecurityException
from pyfly.security.context import SecurityContext
from pyfly.security.oauth2.authorization_server import AuthorizationServer
from pyfly.security.oauth2.client import ClientRegistration

# OAuth2 error codes that map to a 401 (client authentication failed); the rest
# are request/grant errors returned as 400 (RFC 6749 §5.2).
_UNAUTHORIZED_ERRORS = {"INVALID_CLIENT"}

# Authorization-endpoint error codes that may NOT be redirected back to the client
# (the client/redirect is untrusted), per RFC 6749 §4.1.2.1.
_NON_REDIRECTABLE = {"INVALID_CLIENT", "INVALID_REDIRECT_URI"}

# Map internal SecurityException codes to RFC 6749 authorization-response errors.
_AUTHZ_ERROR = {
    "INVALID_SCOPE": "invalid_scope",
    "UNSUPPORTED_RESPONSE_TYPE": "unsupported_response_type",
    "INVALID_REQUEST": "invalid_request",
}


class AuthorizationServerEndpoints:
    """Builds Starlette routes that expose an :class:`AuthorizationServer`."""

    def __init__(self, server: AuthorizationServer, *, login_url: str = "/login") -> None:
        self._server = server
        self._login_url = login_url

    def routes(self) -> list[Route]:
        return [
            Route("/oauth2/authorize", self._authorize, methods=["GET"]),
            Route("/oauth2/par", self._par, methods=["POST"]),
            Route("/oauth2/token", self._token, methods=["POST"]),
            Route("/oauth2/introspect", self._introspect, methods=["POST"]),
            Route("/oauth2/revoke", self._revoke, methods=["POST"]),
            Route("/oauth2/register", self._register, methods=["POST"]),
            Route("/oauth2/jwks", self._jwks, methods=["GET"]),
            Route("/.well-known/oauth-authorization-server", self._oauth_metadata, methods=["GET"]),
            Route("/.well-known/openid-configuration", self._openid_metadata, methods=["GET"]),
        ]

    # -- metadata / discovery (RFC 8414 + OIDC discovery) -----------------

    def _metadata(self, request: Request) -> dict[str, Any]:
        base = str(request.base_url).rstrip("/")
        return {
            "issuer": self._server.issuer or base,
            "authorization_endpoint": f"{base}/oauth2/authorize",
            "token_endpoint": f"{base}/oauth2/token",
            "introspection_endpoint": f"{base}/oauth2/introspect",
            "revocation_endpoint": f"{base}/oauth2/revoke",
            "registration_endpoint": f"{base}/oauth2/register",
            "jwks_uri": f"{base}/oauth2/jwks",
            "response_types_supported": ["code"],
            "grant_types_supported": ["authorization_code", "client_credentials", "refresh_token"],
            "token_endpoint_auth_methods_supported": ["client_secret_basic", "client_secret_post", "none"],
            "code_challenge_methods_supported": ["S256"],
        }

    async def _oauth_metadata(self, request: Request) -> Response:
        return JSONResponse(self._metadata(request))

    async def _openid_metadata(self, request: Request) -> Response:
        doc = self._metadata(request)
        doc.update(
            {
                "subject_types_supported": ["public"],
                "id_token_signing_alg_values_supported": [self._server.signing_algorithm],
                "scopes_supported": ["openid"],
                "claims_supported": ["sub", "aud", "iss", "exp", "iat", "nonce"],
            }
        )
        return JSONResponse(doc)

    # -- authorization endpoint (RFC 6749 §4.1.1) -------------------------

    async def _authorize(self, request: Request) -> Response:
        ctx = getattr(getattr(request, "state", None), "security_context", None)
        user_id = ctx.user_id if isinstance(ctx, SecurityContext) and ctx.is_authenticated else None
        if not user_id:
            # The resource owner must authenticate first; bounce to login and come back.
            return RedirectResponse(f"{self._login_url}?{urlencode({'next': str(request.url)})}", status_code=302)

        params: dict[str, str] = dict(request.query_params)
        client_id = params.get("client_id", "")
        # PAR (RFC 9126): resolve a one-time request_uri to its pushed params.
        if params.get("request_uri"):
            stored = await self._server.consume_pushed_request(params["request_uri"], client_id)
            if stored is None:
                return JSONResponse({"error": "invalid_request_uri"}, status_code=400)
            params = {**{k: str(v) for k, v in stored.items()}, "client_id": client_id}
        # JAR (RFC 9101): a signed request object supplies the parameters.
        elif params.get("request"):
            try:
                claims = self._server.verify_request_object(client_id, params["request"])
            except SecurityException as exc:
                return JSONResponse({"error": "invalid_request_object", "error_description": str(exc)}, status_code=400)
            params = {**params, **{k: str(v) for k, v in claims.items()}}

        redirect_uri = params.get("redirect_uri", "")
        state = params.get("state")
        try:
            result = await self._server.authorize(
                client_id=params.get("client_id", ""),
                redirect_uri=redirect_uri,
                user_id=user_id,
                response_type=params.get("response_type", "code"),
                scope=params.get("scope", ""),
                state=state,
                code_challenge=params.get("code_challenge"),
                code_challenge_method=params.get("code_challenge_method", "S256"),
                nonce=params.get("nonce"),
            )
        except SecurityException as exc:
            code = exc.code or "INVALID_REQUEST"
            if code in _NON_REDIRECTABLE:
                return JSONResponse({"error": code.lower(), "error_description": str(exc)}, status_code=400)
            query = {"error": _AUTHZ_ERROR.get(code, "invalid_request")}
            if state is not None:
                query["state"] = state
            return RedirectResponse(f"{redirect_uri}?{urlencode(query)}", status_code=302)

        query = {"code": result["code"]}
        if "state" in result:
            query["state"] = result["state"]
        if "iss" in result:
            query["iss"] = result["iss"]
        return RedirectResponse(f"{result['redirect_uri']}?{urlencode(query)}", status_code=302)

    # -- dynamic client registration (RFC 7591) ---------------------------

    async def _register(self, request: Request) -> Response:
        # When an initial access token is configured, it MUST be presented as a
        # bearer token (RFC 7591 §3); otherwise registration is open.
        required = self._server.registration_access_token
        if required:
            header = request.headers.get("authorization", "")
            parts = header.split(" ", 1)
            presented = parts[1].strip() if len(parts) == 2 and parts[0].lower() == "bearer" else ""
            if not presented or not _consteq(presented, required):
                return JSONResponse(
                    {"error": "invalid_token"},
                    status_code=401,
                    headers={"WWW-Authenticate": 'Bearer error="invalid_token"'},
                )
        try:
            metadata = await request.json()
        except Exception:  # malformed JSON body
            metadata = {}
        try:
            result = await self._server.register_client(metadata if isinstance(metadata, dict) else {})
        except SecurityException as exc:
            return JSONResponse({"error": (exc.code or "invalid_request").lower()}, status_code=403)
        return JSONResponse(result, status_code=201)

    # -- pushed authorization requests (RFC 9126) -------------------------

    async def _par(self, request: Request) -> Response:
        form = await request.form()
        registration = self._authenticate(request, form)
        if registration is None:
            return self._error(SecurityException("Invalid client", code="INVALID_CLIENT"))
        params = {k: str(v) for k, v in form.items() if k not in ("client_id", "client_secret")}
        result = await self._server.pushed_authorization_request(registration.client_id, params)
        return JSONResponse(result, status_code=201)

    # -- token endpoint ----------------------------------------------------

    async def _token(self, request: Request) -> Response:
        form = await request.form()
        client_id, client_secret = self._client_credentials(request, form)
        # DPoP (RFC 9449): if the client presents a proof on the token request, bind
        # the issued access token to its key via a cnf.jkt confirmation claim.
        confirmation: dict[str, Any] | None = None
        dpop_proof = request.headers.get("dpop")
        if dpop_proof:
            from pyfly.security.oauth2.dpop import DPoPProofValidator

            try:
                jkt = DPoPProofValidator().validate(dpop_proof, http_method="POST", http_url=str(request.url))
            except SecurityException as exc:
                return self._error(exc)
            confirmation = {"jkt": jkt}
        try:
            result = await self._server.token(
                grant_type=str(form.get("grant_type", "")),
                client_id=client_id,
                client_secret=client_secret,
                scope=str(form.get("scope", "")),
                refresh_token=(str(form["refresh_token"]) if form.get("refresh_token") else None),
                confirmation=confirmation,
            )
        except SecurityException as exc:
            return self._error(exc)
        return JSONResponse(result)

    # -- introspection (RFC 7662) -----------------------------------------

    async def _introspect(self, request: Request) -> Response:
        form = await request.form()
        registration = self._authenticate(request, form)
        if registration is None:
            return self._error(SecurityException("Invalid client", code="INVALID_CLIENT"))
        token = str(form.get("token", ""))
        if not token:
            return JSONResponse({"active": False})
        result = await self._server.introspect(
            token,
            requesting_client_id=registration.client_id,
            allow_any_client=getattr(registration, "allow_introspection", False),
        )
        return JSONResponse(result)

    # -- revocation (RFC 7009) --------------------------------------------

    async def _revoke(self, request: Request) -> Response:
        form = await request.form()
        registration = self._authenticate(request, form)
        if registration is None:
            return self._error(SecurityException("Invalid client", code="INVALID_CLIENT"))
        token = str(form.get("token", ""))
        if token:
            # RFC 7009 §2.1: only the owning client may revoke the token.
            await self._server.revoke(token, requesting_client_id=registration.client_id)
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

    def _authenticate(self, request: Request, form: Any) -> ClientRegistration | None:
        client_id, client_secret = self._client_credentials(request, form)
        return self._server.authenticate_client(client_id, client_secret)

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
