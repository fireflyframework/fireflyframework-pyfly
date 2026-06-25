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
"""OAuth2 Login Handler — browser-facing authorization_code flow."""

from __future__ import annotations

import base64
import hashlib
import logging
import secrets
from typing import Any
from urllib.parse import urlencode

from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse, Response
from starlette.routing import Route

from pyfly.security.context import SecurityContext
from pyfly.security.oauth2.client import ClientRegistrationRepository
from pyfly.session.session import HttpSession

logger = logging.getLogger(__name__)

_OAUTH2_STATE_KEY = "oauth2_state"
_OAUTH2_NONCE_KEY = "oauth2_nonce"
_OAUTH2_PKCE_VERIFIER_KEY = "oauth2_pkce_verifier"
_SECURITY_CONTEXT_KEY = "SECURITY_CONTEXT"
_REDIRECT_URI_KEY = "oauth2_redirect_uri"


def _generate_pkce() -> tuple[str, str]:
    """Return a (code_verifier, code_challenge) pair for PKCE S256 (RFC 7636)."""
    verifier = secrets.token_urlsafe(64)  # 43–128 unreserved chars
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


def _uses_pkce(registration: Any) -> bool:
    """Whether PKCE applies to this registration's authorization_code flow.

    PKCE is on by default (RFC 9700 / OAuth 2.1). It is always enforced for a
    public client (no ``client_secret``) — which has no other defense against
    authorization-code injection — even if ``use_pkce`` was explicitly disabled.
    """
    return bool(getattr(registration, "use_pkce", True)) or not getattr(registration, "client_secret", "")


class OAuth2LoginHandler:
    """Creates Starlette routes for the OAuth2 authorization_code login flow.

    Provides three routes:

    - ``GET /oauth2/authorization/{registration_id}`` — redirects the browser
      to the OAuth2 provider's authorization endpoint.
    - ``GET /login/oauth2/code/{registration_id}`` — handles the provider
      callback, exchanges the authorization code for tokens, fetches user
      info, and stores the :class:`SecurityContext` in the session.
    - ``POST /logout`` — invalidates the session and redirects to ``/``.

    Args:
        client_repository: Repository to look up client registrations.
    """

    def __init__(
        self,
        client_repository: ClientRegistrationRepository,
        concurrency: Any = None,
    ) -> None:
        self._client_repository = client_repository
        self._concurrency = concurrency  # optional SessionConcurrencyController

    def routes(self) -> list[Route]:
        """Return the Starlette routes for the OAuth2 login flow."""
        return [
            Route("/oauth2/authorization/{registration_id}", self._handle_authorization, methods=["GET"]),
            Route("/login/oauth2/code/{registration_id}", self._handle_callback, methods=["GET"]),
            Route("/logout", self._handle_logout, methods=["POST"]),
        ]

    # ------------------------------------------------------------------
    # Route 1: Redirect to OAuth2 provider
    # ------------------------------------------------------------------

    async def _handle_authorization(self, request: Request) -> Response:
        """Redirect the user to the OAuth2 provider's authorization endpoint."""
        registration_id = request.path_params["registration_id"]
        registration = self._client_repository.find_by_registration_id(registration_id)

        if registration is None:
            logger.warning("Unknown client registration: %s", registration_id)
            return JSONResponse(
                {"error": "unknown_registration", "message": f"No registration found for '{registration_id}'"},
                status_code=400,
            )

        session: HttpSession = request.state.session
        state = secrets.token_urlsafe(32)
        session.set_attribute(_OAUTH2_STATE_KEY, state)
        nonce = secrets.token_urlsafe(32)
        session.set_attribute(_OAUTH2_NONCE_KEY, nonce)

        params = {
            "response_type": "code",
            "client_id": registration.client_id,
            "redirect_uri": registration.redirect_uri,
            "scope": " ".join(registration.scopes),
            "state": state,
            "nonce": nonce,
        }
        # PKCE (RFC 7636): stash the verifier in the session, send only the S256 challenge.
        if _uses_pkce(registration):
            verifier, challenge = _generate_pkce()
            session.set_attribute(_OAUTH2_PKCE_VERIFIER_KEY, verifier)
            params["code_challenge"] = challenge
            params["code_challenge_method"] = "S256"
        authorization_url = f"{registration.authorization_uri}?{urlencode(params)}"

        logger.debug("Redirecting to OAuth2 provider: %s", registration.provider_name or registration_id)
        return RedirectResponse(url=authorization_url, status_code=302)

    # ------------------------------------------------------------------
    # Route 2: Handle callback from OAuth2 provider
    # ------------------------------------------------------------------

    async def _handle_callback(self, request: Request) -> Response:
        """Handle the OAuth2 provider callback and exchange the code for tokens."""
        registration_id = request.path_params["registration_id"]
        registration = self._client_repository.find_by_registration_id(registration_id)

        if registration is None:
            logger.warning("Unknown client registration on callback: %s", registration_id)
            return JSONResponse(
                {"error": "unknown_registration", "message": f"No registration found for '{registration_id}'"},
                status_code=400,
            )

        # Validate state parameter (CSRF protection)
        session: HttpSession = request.state.session
        expected_state = session.get_attribute(_OAUTH2_STATE_KEY)
        received_state = request.query_params.get("state")

        if not expected_state or expected_state != received_state:
            logger.warning("OAuth2 state mismatch for registration: %s", registration_id)
            return JSONResponse(
                {"error": "invalid_state", "message": "OAuth2 state parameter mismatch"},
                status_code=400,
            )

        # Consume state (one-time use)
        session.remove_attribute(_OAUTH2_STATE_KEY)

        # Check for error response from provider
        error = request.query_params.get("error")
        if error:
            error_description = request.query_params.get("error_description", "")
            logger.warning("OAuth2 provider returned error: %s — %s", error, error_description)
            return JSONResponse(
                {"error": error, "message": error_description or error},
                status_code=400,
            )

        code = request.query_params.get("code")
        if not code:
            return JSONResponse(
                {"error": "missing_code", "message": "No authorization code in callback"},
                status_code=400,
            )

        # PKCE: retrieve and consume the one-time verifier stashed at authorization time.
        code_verifier = None
        if _uses_pkce(registration):
            code_verifier = session.get_attribute(_OAUTH2_PKCE_VERIFIER_KEY)
            session.remove_attribute(_OAUTH2_PKCE_VERIFIER_KEY)

        # Exchange authorization code for tokens
        token_response = await self._exchange_code(registration, code, code_verifier)
        access_token = token_response.get("access_token")
        if not access_token:
            logger.error("Token exchange did not return an access_token for %s", registration_id)
            return JSONResponse(
                {"error": "token_exchange_failed", "message": "Failed to obtain access token"},
                status_code=502,
            )

        # Prefer verified OIDC ID-token claims when present; the id_token is
        # signature/issuer/audience/nonce validated against the provider JWKS
        # before any claim is trusted (audit #48).
        nonce = session.get_attribute(_OAUTH2_NONCE_KEY)
        session.remove_attribute(_OAUTH2_NONCE_KEY)
        security_context: SecurityContext | None = None
        id_token = token_response.get("id_token")
        if id_token and getattr(registration, "jwks_uri", ""):
            security_context = self._validate_id_token(registration, id_token, nonce)
            if security_context is None:
                return JSONResponse(
                    {"error": "invalid_id_token", "message": "ID token validation failed"},
                    status_code=401,
                )

        # Otherwise build identity from the userinfo endpoint.
        if security_context is None:
            user_info = await self._fetch_user_info(registration, access_token)
            security_context = self._build_security_context(user_info)

        # A configured userinfo/OIDC flow that yields no principal is a hard
        # failure, not a silently-stored anonymous session (audit #49).
        if security_context.user_id is None:
            logger.warning("OAuth2 login produced no authenticated principal for %s", registration_id)
            return JSONResponse(
                {"error": "login_failed", "message": "Could not determine the authenticated user"},
                status_code=401,
            )

        # Rotate the session id on successful authentication to prevent session
        # fixation — the pre-auth id (which an attacker may have fixed) is dropped.
        session.rotate_id()
        session.set_attribute(_SECURITY_CONTEXT_KEY, security_context)

        # Enforce per-principal session concurrency (Spring maximumSessions) — the principal
        # is now bound to the (rotated) session id, so this is the one correct enforcement point.
        if self._concurrency is not None:
            allowed = await self._concurrency.on_login(security_context.user_id, session.id, session.created_at)
            if not allowed:
                session.invalidate()
                return JSONResponse(
                    {"error": "max_sessions", "message": "Maximum concurrent sessions for this user reached"},
                    status_code=401,
                )

        logger.info("OAuth2 login successful for user: %s (via %s)", security_context.user_id, registration_id)

        redirect_uri = session.get_attribute(_REDIRECT_URI_KEY) or "/"
        session.remove_attribute(_REDIRECT_URI_KEY)
        return RedirectResponse(url=str(redirect_uri), status_code=302)

    # ------------------------------------------------------------------
    # Route 3: Logout
    # ------------------------------------------------------------------

    async def _handle_logout(self, request: Request) -> Response:
        """Invalidate the session and redirect to the root."""
        session: HttpSession = request.state.session
        if self._concurrency is not None:
            principal = session.get_attribute(_SECURITY_CONTEXT_KEY)
            user_id = getattr(principal, "user_id", None)
            if user_id is not None:
                await self._concurrency.on_logout(user_id, session.id)
        session.invalidate()
        return RedirectResponse(url="/", status_code=302)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _exchange_code(self, registration: Any, code: str, code_verifier: str | None = None) -> dict[str, Any]:
        """Exchange an authorization code for tokens via the token endpoint."""
        data = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": registration.redirect_uri,
            "client_id": registration.client_id,
            "client_secret": registration.client_secret,
        }
        if code_verifier:  # PKCE proof of possession
            data["code_verifier"] = code_verifier

        import httpx

        try:
            async with httpx.AsyncClient() as client:
                response = await client.post(
                    registration.token_uri,
                    data=data,
                    headers={"Accept": "application/json"},
                )
        except httpx.HTTPError as exc:
            logger.error("Token exchange transport error: %s", exc)
            return {}

        if response.status_code != 200:
            logger.error(
                "Token exchange failed (HTTP %d): %s",
                response.status_code,
                response.text,
            )
            return {}

        return response.json()  # type: ignore[no-any-return]

    def _validate_id_token(self, registration: Any, id_token: str, nonce: str | None) -> SecurityContext | None:
        """Validate an OIDC ID token against the provider JWKS and nonce.

        Returns the SecurityContext built from verified claims, or ``None`` when
        validation fails (bad signature/issuer/audience/nonce).
        """
        from pyfly.kernel.exceptions import SecurityException
        from pyfly.security.oauth2.resource_server import JWKSTokenValidator

        validator = JWKSTokenValidator(
            jwks_uri=registration.jwks_uri,
            issuer=getattr(registration, "issuer_uri", "") or None,
            # An OIDC id_token's audience is the client_id.
            audiences=[registration.client_id],
        )
        try:
            claims = validator.validate(id_token)
        except SecurityException as exc:
            logger.warning("OIDC id_token validation failed: %s", exc)
            return None

        if nonce is not None and claims.get("nonce") != nonce:
            logger.warning("OIDC id_token nonce mismatch")
            return None

        return validator.to_security_context(id_token)

    async def _fetch_user_info(self, registration: Any, access_token: str) -> dict[str, Any]:
        """Fetch user info from the OAuth2 provider's userinfo endpoint."""
        if not registration.user_info_uri:
            logger.debug("No user_info_uri configured for %s, skipping userinfo fetch", registration.registration_id)
            return {}

        import httpx

        try:
            async with httpx.AsyncClient() as client:
                response = await client.get(
                    registration.user_info_uri,
                    headers={
                        "Authorization": f"Bearer {access_token}",
                        "Accept": "application/json",
                    },
                )
        except httpx.HTTPError as exc:
            logger.warning("User info fetch transport error: %s", exc)
            return {}

        if response.status_code != 200:
            logger.warning(
                "User info fetch failed (HTTP %d): %s",
                response.status_code,
                response.text,
            )
            return {}

        return response.json()  # type: ignore[no-any-return]

    @staticmethod
    def _build_security_context(user_info: dict[str, Any]) -> SecurityContext:
        """Build a SecurityContext from the OAuth2 user info response."""
        user_id = user_info.get("sub") or user_info.get("id") or user_info.get("login")

        # Collect all string-valued user info fields as attributes
        attributes: dict[str, str] = {}
        for key, value in user_info.items():
            if isinstance(value, str) and key not in ("sub", "id"):
                attributes[key] = value

        return SecurityContext(
            user_id=str(user_id) if user_id is not None else None,
            attributes=attributes,
        )
