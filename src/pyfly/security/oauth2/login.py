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
from collections.abc import Awaitable, Callable, Mapping
from typing import TYPE_CHECKING, Any
from urllib.parse import urlencode

from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse, Response
from starlette.routing import Route

from pyfly.kernel.exceptions import SecurityException
from pyfly.security.context import SecurityContext
from pyfly.security.oauth2.client import ClientRegistration, ClientRegistrationRepository
from pyfly.security.oauth2.ports import (
    OAuth2AuthenticationSuccessHandler,
    OAuth2AuthorizationRequestCustomizer,
    OidcTokenValidatorFactory,
)
from pyfly.session.session import HttpSession

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from pyfly.security.oauth2.resource_server import ClaimMappings

_RESERVED_AUTHORIZATION_PARAMETERS = frozenset(
    {
        "response_type",
        "response_mode",
        "client_id",
        "client_secret",
        "redirect_uri",
        "scope",
        "state",
        "nonce",
        "code_challenge",
        "code_challenge_method",
        "code_verifier",
        "request",
        "request_uri",
    }
)

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
        concurrency: An optional ``SessionConcurrencyController``. The callback then saves the logged-in
            session (``request.state.persist_session``) and registers it, enforcing the per-principal cap;
            that save is the login's last write of the session. If the save or the registration fails, the
            session is invalidated.
    """

    def __init__(
        self,
        client_repository: ClientRegistrationRepository,
        concurrency: Any = None,
        *,
        authorization_request_customizer: OAuth2AuthorizationRequestCustomizer | None = None,
        authentication_success_handler: OAuth2AuthenticationSuccessHandler | None = None,
        id_token_validator_factory: OidcTokenValidatorFactory | None = None,
        claim_mappings: ClaimMappings | None = None,
        require_oidc_id_token: bool = False,
        success_uri: str = "/",
    ) -> None:
        self._client_repository = client_repository
        self._concurrency = concurrency  # optional SessionConcurrencyController
        self._authorization_request_customizer = authorization_request_customizer
        self._authentication_success_handler = authentication_success_handler
        self._id_token_validator_factory = id_token_validator_factory
        self._claim_mappings = claim_mappings
        self._require_oidc_id_token = require_oidc_id_token
        self._success_uri = success_uri

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

        extras: dict[str, str] = {}
        if self._authorization_request_customizer is not None:
            supplied = await self._authorization_request_customizer.customize(request, registration)
            if not isinstance(supplied, Mapping) or len(supplied) > 32:
                raise SecurityException("Invalid authorization customization", code="INVALID_OAUTH2_CUSTOMIZATION")
            for key, value in supplied.items():
                if (
                    not isinstance(key, str)
                    or not isinstance(value, str)
                    or not key
                    or len(key) > 128
                    or key.casefold() in _RESERVED_AUTHORIZATION_PARAMETERS
                    or len(value) > 4096
                    or any(char in key + value for char in ("\r", "\n", "\0"))
                ):
                    raise SecurityException("Invalid authorization customization", code="INVALID_OAUTH2_CUSTOMIZATION")
                extras[key] = value
            if len(urlencode(extras).encode("utf-8")) > 8192:
                raise SecurityException(
                    "Authorization customization exceeds its size limit", code="INVALID_OAUTH2_CUSTOMIZATION"
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
        params.update(extras)
        authorization_url = f"{registration.authorization_uri}?{urlencode(params)}"

        logger.debug("Redirecting to OAuth2 provider: %s", registration.provider_name or registration_id)
        return RedirectResponse(url=authorization_url, status_code=302)

    # ------------------------------------------------------------------
    # Route 2: Handle callback from OAuth2 provider
    # ------------------------------------------------------------------

    async def _handle_callback(self, request: Request) -> Response:
        # A claim's continuation remains anonymous until a completed response publishes authentication.
        # On an uncertain claim or cancellation there is no prior authority left on an unadopted ID.
        previous_context: list[SecurityContext] = []
        response = await self._handle_callback_response(request, previous_context)
        if response.status_code >= 400 and previous_context:
            session: HttpSession = request.state.session
            prior = previous_context[0]
            if not session.invalidated and prior.user_id is not None:
                session.set_attribute(_SECURITY_CONTEXT_KEY, prior)
                failure = await self._publish_authentication(request, prior.user_id)
                if failure is not None:
                    return failure
        return response

    async def _handle_callback_response(self, request: Request, previous_context: list[SecurityContext]) -> Response:
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

        # Consume the persisted challenge before any provider I/O, not just this request's snapshot.
        claim = getattr(request.state, "claim_session_attributes", None)
        if claim is None:
            return JSONResponse({"error": "session_claim_unavailable"}, status_code=400)
        try:
            consumed = await claim(
                {_OAUTH2_STATE_KEY: expected_state},
                (_OAUTH2_STATE_KEY, _OAUTH2_NONCE_KEY, _OAUTH2_PKCE_VERIFIER_KEY, _SECURITY_CONTEXT_KEY),
            )
        except Exception:
            logger.warning("oauth2_session_claim_failed")
            return JSONResponse({"error": "session_claim_unavailable"}, status_code=400)
        if consumed is None:
            return JSONResponse(
                {"error": "invalid_state", "message": "OAuth2 challenge is no longer available"}, status_code=400
            )

        prior = consumed.get(_SECURITY_CONTEXT_KEY)
        if isinstance(prior, SecurityContext) and prior.user_id is not None:
            previous_context.append(prior)

        # Check for error response from provider
        error = request.query_params.get("error")
        if error:
            error_description = request.query_params.get("error_description", "")
            logger.warning("OAuth2 provider returned an error")
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

        # RFC 9207 mix-up defense: validate the issuer that produced this response.
        # The ``iss`` param is always rejected on mismatch with the registration's
        # ``issuer_uri``; with ``require_iss`` it must also be present.
        iss_error = self._validate_iss(registration, request.query_params.get("iss"))
        if iss_error is not None:
            return iss_error

        # PKCE: retrieve and consume the one-time verifier stashed at authorization time.
        code_verifier = None
        if _uses_pkce(registration):
            code_verifier = consumed.get(_OAUTH2_PKCE_VERIFIER_KEY)

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
        nonce = consumed.get(_OAUTH2_NONCE_KEY)
        security_context: SecurityContext | None = None
        id_token = token_response.get("id_token")
        can_validate = bool(getattr(registration, "jwks_uri", "")) or self._id_token_validator_factory is not None
        if self._require_oidc_id_token and (
            not isinstance(id_token, str)
            or not id_token
            or not can_validate
            or not getattr(registration, "issuer_uri", "")
            or not nonce
        ):
            return JSONResponse(
                {"error": "oidc_id_token_required", "message": "A verified OIDC ID token is required"}, status_code=401
            )
        if id_token and can_validate:
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

        if self._authentication_success_handler is not None:
            success: Callable[[Request, ClientRegistration, SecurityContext], Awaitable[object]] = (
                self._authentication_success_handler.on_success
            )
            result = await success(request, registration, security_context)
            if result is not None:
                raise SecurityException("Invalid authentication success result", code="INVALID_OAUTH2_SUCCESS_HANDLER")

        # Rotate the session id on successful authentication to prevent session
        # fixation — the pre-auth id (which an attacker may have fixed) is dropped.
        session.rotate_id(on_login=True)
        session.set_attribute(_SECURITY_CONTEXT_KEY, security_context)
        redirect_uri = session.get_attribute(_REDIRECT_URI_KEY) or self._success_uri
        session.remove_attribute(_REDIRECT_URI_KEY)

        failure = await self._publish_authentication(request, security_context.user_id)
        if failure is not None:
            return failure

        logger.info("OAuth2 login successful for user: %s (via %s)", security_context.user_id, registration_id)
        return RedirectResponse(url=str(redirect_uri), status_code=302)

    async def _publish_authentication(self, request: Request, user_id: str) -> Response | None:
        """Publish only a live continuation and register it under the configured session-cap policy."""
        session: HttpSession = request.state.session
        # Enforce per-principal session concurrency (Spring maximumSessions) — the principal
        # is now bound to the (rotated) session id, so this is the one correct enforcement point.
        # The session is saved first: the controller counts a session while the store has it,
        # so a concurrent login of the same principal must find this one there. That save is
        # the login's last write of the session (every change above is made before it, and the
        # SessionFilter does not save an unchanged session again): a concurrent login may evict
        # this session meanwhile, and a later save would bring it back, live and uncounted.
        persist = getattr(request.state, "persist_session", None)
        if persist is None:
            session.invalidate()
            return JSONResponse({"error": "session_claim_unavailable"}, status_code=400)
        try:
            await persist()
        except BaseException:
            # An uncertain commit must not leave a live, unregistered authenticated session behind.
            session.invalidate()
            raise
        if session.invalidated:
            return JSONResponse(
                {"error": "session_ended", "message": "Login session is no longer available"}, status_code=401
            )
        if self._concurrency is not None:
            try:
                allowed = await self._concurrency.on_login(user_id, session.id, session.created_at)
            except BaseException:
                # Saved but perhaps never registered: the SessionFilter deletes it rather than leave a
                # logged-in session the cap does not count.
                session.invalidate()
                raise
            if not allowed:
                session.invalidate()
                return JSONResponse(
                    {"error": "max_sessions", "message": "Maximum concurrent sessions for this user reached"},
                    status_code=401,
                )

        return None

    # ------------------------------------------------------------------
    # Route 3: Logout
    # ------------------------------------------------------------------

    async def _handle_logout(self, request: Request) -> Response:
        """Invalidate the session, deregister it from the concurrency controller, and redirect to the root.

        The session is invalidated first: a deregistration that fails (the registry's database or Redis down,
        a pool timeout) is logged as ``session_deregistration_failed`` and never undoes the logout. The
        controller drops the registration left behind as dead, at the principal's next capped login or
        through the purge.
        """
        session: HttpSession = request.state.session
        user_id = getattr(session.get_attribute(_SECURITY_CONTEXT_KEY), "user_id", None)
        session.set_attribute(_SECURITY_CONTEXT_KEY, None)
        session.invalidate()
        if self._concurrency is not None and user_id is not None:
            try:
                await self._concurrency.on_logout(user_id, session.id)
            except Exception:  # noqa: BLE001 — the session has ended already; a stale registration is dropped later
                logger.warning("session_deregistration_failed", exc_info=True)
        return RedirectResponse(url="/", status_code=302)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _validate_iss(registration: Any, received_iss: str | None) -> Response | None:
        """Validate the RFC 9207 ``iss`` authorization-response parameter.

        Returns a 400 response on a mismatch, or when ``require_iss`` is set and the
        parameter is absent; otherwise ``None`` (validation passed).
        """
        expected = getattr(registration, "issuer_uri", "") or ""
        require = getattr(registration, "require_iss", False)
        if received_iss is None:
            if require:
                logger.warning("OAuth2 callback missing required 'iss' parameter (RFC 9207)")
                return JSONResponse(
                    {"error": "invalid_iss", "message": "Missing required 'iss' parameter"},
                    status_code=400,
                )
            return None
        if expected and received_iss != expected:
            logger.warning("OAuth2 'iss' mismatch: expected %r, got %r", expected, received_iss)
            return JSONResponse(
                {"error": "invalid_iss", "message": "Issuer (iss) does not match the expected provider"},
                status_code=400,
            )
        return None

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
        except httpx.HTTPError:
            logger.error("Token exchange transport error")
            return {}

        if response.status_code != 200:
            logger.error(
                "Token exchange failed (HTTP %d)",
                response.status_code,
            )
            return {}

        return response.json()  # type: ignore[no-any-return]

    def _validate_id_token(self, registration: Any, id_token: str, nonce: str | None) -> SecurityContext | None:
        """Validate an OIDC ID token against the provider JWKS and nonce.

        Returns the SecurityContext built from verified claims, or ``None`` when
        validation fails (bad signature/issuer/audience/nonce).
        """
        from pyfly.kernel.exceptions import SecurityException
        from pyfly.security.oauth2.resource_server import ClaimMappings, JWKSTokenValidator, build_security_context

        validator = (
            self._id_token_validator_factory.create(registration)
            if self._id_token_validator_factory
            else JWKSTokenValidator(
                jwks_uri=registration.jwks_uri,
                issuer=getattr(registration, "issuer_uri", "") or None,
                # An OIDC id_token's audience is the client_id.
                audiences=[registration.client_id],
            )
        )
        try:
            claims = validator.validate(id_token)
        except SecurityException as exc:
            logger.warning("OIDC id_token validation failed: %s", exc)
            return None

        if (self._require_oidc_id_token and not nonce) or (nonce is not None and claims.get("nonce") != nonce):
            logger.warning("OIDC id_token nonce mismatch")
            return None

        expected_issuer = getattr(registration, "issuer_uri", "")
        audience = claims.get("aud")
        if expected_issuer and claims.get("iss") != expected_issuer:
            return None
        if audience != registration.client_id and not (
            isinstance(audience, list) and registration.client_id in audience
        ):
            return None
        if (isinstance(audience, list) and len(audience) > 1 and claims.get("azp") != registration.client_id) or (
            claims.get("azp") is not None and claims["azp"] != registration.client_id
        ):
            return None
        return build_security_context(claims, self._claim_mappings or ClaimMappings())

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
        except httpx.HTTPError:
            logger.warning("User info fetch transport error")
            return {}

        if response.status_code != 200:
            logger.warning(
                "User info fetch failed (HTTP %d)",
                response.status_code,
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
