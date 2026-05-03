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
_SECURITY_CONTEXT_KEY = "SECURITY_CONTEXT"
_REDIRECT_URI_KEY = "oauth2_redirect_uri"


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

    def __init__(self, client_repository: ClientRegistrationRepository) -> None:
        self._client_repository = client_repository

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

        params = {
            "response_type": "code",
            "client_id": registration.client_id,
            "redirect_uri": registration.redirect_uri,
            "scope": " ".join(registration.scopes),
            "state": state,
        }
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

        # Exchange authorization code for tokens
        token_response = await self._exchange_code(registration, code)
        access_token = token_response.get("access_token")
        if not access_token:
            logger.error("Token exchange did not return an access_token for %s", registration_id)
            return JSONResponse(
                {"error": "token_exchange_failed", "message": "Failed to obtain access token"},
                status_code=502,
            )

        # Fetch user info from provider
        user_info = await self._fetch_user_info(registration, access_token)

        # Build SecurityContext from user info
        security_context = self._build_security_context(user_info)
        session.set_attribute(_SECURITY_CONTEXT_KEY, security_context)

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
        session.invalidate()
        return RedirectResponse(url="/", status_code=302)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _exchange_code(self, registration: Any, code: str) -> dict[str, Any]:
        """Exchange an authorization code for tokens via the token endpoint."""
        data = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": registration.redirect_uri,
            "client_id": registration.client_id,
            "client_secret": registration.client_secret,
        }

        import httpx

        async with httpx.AsyncClient() as client:
            response = await client.post(
                registration.token_uri,
                data=data,
                headers={"Accept": "application/json"},
            )

        if response.status_code != 200:
            logger.error(
                "Token exchange failed (HTTP %d): %s",
                response.status_code,
                response.text,
            )
            return {}

        return response.json()  # type: ignore[no-any-return]

    async def _fetch_user_info(self, registration: Any, access_token: str) -> dict[str, Any]:
        """Fetch user info from the OAuth2 provider's userinfo endpoint."""
        if not registration.user_info_uri:
            logger.debug("No user_info_uri configured for %s, skipping userinfo fetch", registration.registration_id)
            return {}

        import httpx

        async with httpx.AsyncClient() as client:
            response = await client.get(
                registration.user_info_uri,
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Accept": "application/json",
                },
            )

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
