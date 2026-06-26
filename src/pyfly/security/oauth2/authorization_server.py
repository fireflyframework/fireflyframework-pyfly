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
"""OAuth2 Authorization Server — token endpoint with JWT issuance."""

from __future__ import annotations

import base64
import hashlib
import secrets
import time
from typing import Any, Protocol

import jwt as pyjwt

from pyfly.kernel.exceptions import SecurityException
from pyfly.security.oauth2.client import ClientRegistration, ClientRegistrationRepository


def _s256(verifier: str) -> str:
    """PKCE S256 transform: base64url(SHA-256(verifier)), no padding (RFC 7636)."""
    return base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).rstrip(b"=").decode("ascii")


# ---------------------------------------------------------------------------
# Token Store port and in-memory adapter
# ---------------------------------------------------------------------------


class TokenStore(Protocol):
    """Port for storing and retrieving OAuth2 tokens."""

    async def store(self, token_id: str, token_data: dict[str, Any]) -> None: ...

    async def find(self, token_id: str) -> dict[str, Any] | None: ...

    async def revoke(self, token_id: str) -> None: ...


class InMemoryTokenStore:
    """In-memory token store — suitable for development and testing."""

    def __init__(self) -> None:
        self._tokens: dict[str, dict[str, Any]] = {}

    async def store(self, token_id: str, token_data: dict[str, Any]) -> None:
        self._tokens[token_id] = token_data

    async def find(self, token_id: str) -> dict[str, Any] | None:
        return self._tokens.get(token_id)

    async def revoke(self, token_id: str) -> None:
        self._tokens.pop(token_id, None)


# ---------------------------------------------------------------------------
# Authorization Server
# ---------------------------------------------------------------------------


class AuthorizationServer:
    """OAuth2 Authorization Server — issues JWT access tokens.

    Supports grant types:
    - client_credentials: machine-to-machine authentication
    - refresh_token: exchange a refresh token for a new access token

    Args:
        secret: Secret key for HMAC signing (used when ``algorithm`` is ``HS*``).
        client_repository: Repository to look up client registrations.
        token_store: Store for refresh tokens.
        access_token_ttl: Access token lifetime in seconds (default: 3600 = 1 hour).
        refresh_token_ttl: Refresh token lifetime in seconds (default: 86400 = 24 hours).
        issuer: Token issuer claim (optional).
        audience: Audience the issued tokens are restricted to (``aud`` claim).
            Accepts a single value or a list. When unset, no ``aud`` is emitted
            (backward compatible). Setting it lets resource servers reject tokens
            minted for a different API (RFC 9700 / OAuth 2.1 audience restriction).
        algorithm: JWS algorithm. ``HS256`` (default) signs with ``secret``;
            ``RS256``/``RS384``/``RS512``/``PS*``/``ES256``/``ES384``/``ES512``
            sign with ``private_key`` and publish the matching public key via
            :meth:`jwks`, so a resource server can verify AS-minted tokens.
        private_key: PEM string/bytes or a cryptography private-key object, required
            for asymmetric algorithms.
        key_id: ``kid`` placed in the JWT header and the published JWK.
    """

    def __init__(
        self,
        secret: str,
        client_repository: ClientRegistrationRepository,
        token_store: TokenStore,
        access_token_ttl: int = 3600,
        refresh_token_ttl: int = 86400,
        issuer: str | None = None,
        audience: str | list[str] | None = None,
        algorithm: str = "HS256",
        private_key: Any = None,
        key_id: str | None = None,
        allow_dynamic_registration: bool = False,
        registration_access_token: str | None = None,
        auth_code_ttl: int = 60,
    ) -> None:
        self._secret = secret
        self.allow_dynamic_registration = allow_dynamic_registration
        self.registration_access_token = registration_access_token
        self._auth_code_ttl = auth_code_ttl
        self._client_repository = client_repository
        self._token_store = token_store
        self._access_token_ttl = access_token_ttl
        self._refresh_token_ttl = refresh_token_ttl
        self._issuer = issuer
        self._algorithm = algorithm.upper()
        self._is_asymmetric = self._algorithm[:2] in ("RS", "ES", "PS")
        self._key_id = key_id
        self._private_key: Any = self._coerce_private_key(private_key) if self._is_asymmetric else None
        if self._is_asymmetric and self._private_key is None:
            raise ValueError(f"algorithm {self._algorithm} requires a private_key")
        if audience is None:
            self._audience: str | list[str] | None = None
        elif isinstance(audience, str):
            self._audience = audience
        else:
            aud_list = [a for a in audience if a]
            self._audience = aud_list or None

    @property
    def issuer(self) -> str | None:
        """The configured issuer identifier, if any."""
        return self._issuer

    @property
    def signing_algorithm(self) -> str:
        """The JWS algorithm used to sign tokens (e.g. ``HS256``/``RS256``)."""
        return self._algorithm

    @staticmethod
    def _coerce_private_key(private_key: Any) -> Any:
        """Load a PEM string/bytes into a key object; pass through key objects."""
        if isinstance(private_key, (str, bytes)):
            from cryptography.hazmat.primitives.serialization import load_pem_private_key

            data = private_key.encode("utf-8") if isinstance(private_key, str) else private_key
            return load_pem_private_key(data, password=None)
        return private_key

    def _encode(self, payload: dict[str, Any]) -> str:
        """Sign *payload* with the configured algorithm (HMAC or asymmetric+kid)."""
        if self._is_asymmetric:
            assert self._private_key is not None  # guaranteed by __init__
            headers = {"kid": self._key_id} if self._key_id else None
            return pyjwt.encode(payload, self._private_key, algorithm=self._algorithm, headers=headers)
        return pyjwt.encode(payload, self._secret, algorithm=self._algorithm)

    def jwks(self) -> dict[str, Any]:
        """Return the public JWK Set for token verification (empty for HMAC)."""
        if not self._is_asymmetric or self._private_key is None:
            return {"keys": []}
        import json as _json

        assert self._private_key is not None  # narrowed for mypy
        public_key = self._private_key.public_key()
        if self._algorithm[:2] == "ES":
            jwk = _json.loads(pyjwt.algorithms.ECAlgorithm.to_jwk(public_key))
        else:
            jwk = _json.loads(pyjwt.algorithms.RSAAlgorithm.to_jwk(public_key))
        jwk.update({"use": "sig", "alg": self._algorithm})
        if self._key_id:
            jwk["kid"] = self._key_id
        return {"keys": [jwk]}

    async def register_client(self, metadata: dict[str, Any]) -> dict[str, Any]:
        """Register a new client dynamically (RFC 7591) and return its metadata.

        Requires ``allow_dynamic_registration`` and a client repository that
        supports ``add``. Generates the ``client_id`` / ``client_secret``; the
        endpoint layer enforces any initial access token.
        """
        if not self.allow_dynamic_registration:
            raise SecurityException("Dynamic client registration is disabled", code="REGISTRATION_DISABLED")
        repo = self._client_repository
        add = getattr(repo, "add", None)
        if not callable(add):
            raise SecurityException(
                "The configured client repository does not support registration", code="REGISTRATION_UNSUPPORTED"
            )
        grant_types = metadata.get("grant_types") or ["client_credentials"]
        redirect_uris = metadata.get("redirect_uris") or []
        scope = metadata.get("scope", "")
        scopes = scope.split() if isinstance(scope, str) else list(scope or [])
        client_id = secrets.token_urlsafe(16)
        client_secret = secrets.token_urlsafe(32)
        registration = ClientRegistration(
            registration_id=client_id,
            client_id=client_id,
            client_secret=client_secret,
            authorization_grant_type=str(grant_types[0]),
            redirect_uri=str(redirect_uris[0]) if redirect_uris else "",
            scopes=scopes,
            provider_name=str(metadata.get("client_name", "")),
        )
        add(registration)
        return {
            "client_id": client_id,
            "client_secret": client_secret,
            "client_id_issued_at": int(time.time()),
            "client_secret_expires_at": 0,  # never expires
            "grant_types": list(grant_types),
            "redirect_uris": list(redirect_uris),
            "scope": " ".join(scopes),
            "token_endpoint_auth_method": "client_secret_basic",
            "client_name": str(metadata.get("client_name", "")),
        }

    def authenticate_client(self, client_id: str, client_secret: str) -> ClientRegistration | None:
        """Return the registration iff *client_id*/*client_secret* match (constant time).

        Client authentication requires real credentials: an empty client id or
        secret — or a registration that has no secret configured — never
        authenticates (prevents an empty-credential bypass on the management
        endpoints and for any client that is not a confidential client).
        """
        if not client_id or not client_secret:
            return None
        registration = self._client_repository.find_by_registration_id(client_id)
        if registration is None or not registration.client_secret:
            return None
        if not secrets.compare_digest(registration.client_secret.encode("utf-8"), client_secret.encode("utf-8")):
            return None
        return registration

    def _verification_key(self) -> Any:
        return self._private_key.public_key() if self._is_asymmetric else self._secret

    async def introspect(
        self, token: str, *, requesting_client_id: str | None = None, allow_any_client: bool = False
    ) -> dict[str, Any]:
        """RFC 7662 token introspection for an access (JWT) or refresh token.

        When *requesting_client_id* is given and *allow_any_client* is False, a
        token owned by a different client is reported as inactive — so a client
        cannot scan another client's tokens (information disclosure). Designated
        resource-server clients pass ``allow_any_client=True``.
        """
        result = await self._introspect(token)
        if (
            result.get("active")
            and requesting_client_id is not None
            and not allow_any_client
            and result.get("client_id") != requesting_client_id
        ):
            return {"active": False}
        return result

    async def _introspect(self, token: str) -> dict[str, Any]:
        # Access token: a self-contained, signature-verified JWT.
        try:
            payload = pyjwt.decode(
                token,
                self._verification_key(),
                algorithms=[self._algorithm],
                options={"require": ["exp"], "verify_aud": False},
            )
            active: dict[str, Any] = {"active": True, "token_type": "Bearer"}
            for claim in ("sub", "scope", "iat", "exp", "iss", "aud"):
                if claim in payload:
                    active[claim] = payload[claim]
            active.setdefault("client_id", payload.get("sub"))
            return active
        except pyjwt.PyJWTError:
            pass

        # Refresh token: opaque, looked up in the store; active iff present,
        # unused, unexpired, and its family is still active.
        data = await self._token_store.find(token)
        if data is None or data.get("used") or data.get("exp", 0) < int(time.time()):
            return {"active": False}
        family_id = data.get("family_id")
        if family_id:
            family = await self._token_store.find(self._family_key(family_id))
            if family is not None and not family.get("active", True):
                return {"active": False}
        return {
            "active": True,
            "token_type": "refresh_token",
            "client_id": data.get("client_id"),
            "scope": data.get("scope", ""),
            "exp": data.get("exp"),
        }

    async def token(
        self,
        grant_type: str,
        client_id: str,
        client_secret: str,
        scope: str = "",
        refresh_token: str | None = None,
        confirmation: dict[str, Any] | None = None,
        code: str | None = None,
        redirect_uri: str | None = None,
        code_verifier: str | None = None,
    ) -> dict[str, Any]:
        """Issue tokens based on grant type.

        Args:
            grant_type: "client_credentials", "refresh_token" or "authorization_code"
            client_id: The client's ID
            client_secret: The client's secret (confidential clients)
            scope: Space-separated scopes (for client_credentials)
            refresh_token: The refresh token (for refresh_token grant)
            confirmation: Optional ``cnf`` confirmation claim to bind the access
                token to a key (e.g. ``{"jkt": ...}`` for DPoP, ``{"x5t#S256": ...}``
                for mTLS) — sender-constraining per RFC 9449 / RFC 8705.
            code: Authorization code (for authorization_code grant).
            redirect_uri: Redirect URI used in the authorization request (must match).
            code_verifier: PKCE verifier (for authorization_code grant).

        Returns:
            Token response dict with access_token, token_type, expires_in,
            and optionally refresh_token / id_token.

        Raises:
            SecurityException: If authentication fails or grant type is unsupported.
        """
        registration = self._client_repository.find_by_registration_id(client_id)
        if registration is None:
            raise SecurityException("Invalid client credentials", code="INVALID_CLIENT")

        # Client authentication: a confidential client (one with a registered
        # secret) MUST present it. A public client (no secret) is permitted only
        # for the authorization_code grant, where PKCE provides proof of possession.
        is_public = not registration.client_secret
        if not is_public:
            if self.authenticate_client(client_id, client_secret) is None:
                raise SecurityException("Invalid client credentials", code="INVALID_CLIENT")
        elif grant_type != "authorization_code":
            raise SecurityException("Public clients may not use this grant", code="INVALID_CLIENT")

        if grant_type == "client_credentials":
            # The client must be registered for the client_credentials grant to
            # mint a client_credentials token — prevents grant-type confusion (a
            # client registered only for authorization_code must not use it).
            if registration.authorization_grant_type != "client_credentials":
                raise SecurityException(
                    f"Client '{client_id}' is not authorized for grant type 'client_credentials'",
                    code="UNAUTHORIZED_CLIENT",
                )
            return await self._handle_client_credentials(registration, scope, confirmation)
        elif grant_type == "refresh_token":
            if refresh_token is None:
                raise SecurityException("Refresh token required", code="INVALID_REQUEST")
            return await self._handle_refresh_token(registration, refresh_token, confirmation)
        elif grant_type == "authorization_code":
            if code is None:
                raise SecurityException("Authorization code required", code="INVALID_REQUEST")
            return await self._handle_authorization_code(registration, code, redirect_uri, code_verifier, confirmation)
        else:
            raise SecurityException(
                f"Unsupported grant type: {grant_type}",
                code="UNSUPPORTED_GRANT_TYPE",
            )

    # ------------------------------------------------------------------
    # Authorization Code grant (RFC 6749 §4.1 + PKCE RFC 7636 + OIDC)
    # ------------------------------------------------------------------

    @staticmethod
    def _code_key(code: str) -> str:
        return f"authcode:{code}"

    async def authorize(
        self,
        *,
        client_id: str,
        redirect_uri: str,
        user_id: str,
        response_type: str = "code",
        scope: str = "",
        state: str | None = None,
        code_challenge: str | None = None,
        code_challenge_method: str = "S256",
        nonce: str | None = None,
    ) -> dict[str, Any]:
        """Process an authorization request and issue a single-use authorization code.

        *user_id* is the already-authenticated resource owner. Enforces exact
        redirect-URI matching, scope subset, and **mandatory PKCE (S256)** (OAuth
        2.1 / RFC 9700). Returns ``{code, redirect_uri[, state][, iss]}``.

        Raises:
            SecurityException: ``INVALID_CLIENT`` / ``INVALID_REDIRECT_URI`` must NOT
            be redirected back to the client; other codes are safe to surface to the
            client via a redirect ``error`` parameter.
        """
        registration = self._client_repository.find_by_registration_id(client_id)
        if registration is None:
            raise SecurityException("Unknown client", code="INVALID_CLIENT")
        # Exact redirect-URI match (RFC 9700 / OAuth 2.1) — never redirect on mismatch.
        if redirect_uri != registration.redirect_uri:
            raise SecurityException("redirect_uri does not match the registration", code="INVALID_REDIRECT_URI")
        if response_type != "code":
            raise SecurityException(f"Unsupported response_type: {response_type}", code="UNSUPPORTED_RESPONSE_TYPE")

        requested = scope.split() if scope else list(registration.scopes)
        unregistered = [s for s in requested if s not in registration.scopes]
        if unregistered:
            raise SecurityException(f"Unpermitted scope(s): {' '.join(unregistered)}", code="INVALID_SCOPE")

        # PKCE is mandatory and must be S256 (OAuth 2.1 §4.1.1 / RFC 7636).
        if not code_challenge:
            raise SecurityException("PKCE code_challenge is required", code="INVALID_REQUEST")
        if code_challenge_method != "S256":
            raise SecurityException("Only the S256 PKCE method is supported", code="INVALID_REQUEST")

        code = secrets.token_urlsafe(32)
        await self._token_store.store(
            self._code_key(code),
            {
                "client_id": registration.client_id,
                "redirect_uri": redirect_uri,
                "scope": " ".join(requested),
                "code_challenge": code_challenge,
                "user_id": user_id,
                "nonce": nonce,
                "exp": int(time.time()) + self._auth_code_ttl,
                "used": False,
            },
        )
        result: dict[str, Any] = {"code": code, "redirect_uri": redirect_uri}
        if state is not None:
            result["state"] = state
        if self._issuer:
            result["iss"] = self._issuer  # RFC 9207 mix-up defense
        return result

    # ------------------------------------------------------------------
    # Pushed Authorization Requests (RFC 9126) + request objects (RFC 9101)
    # ------------------------------------------------------------------

    @staticmethod
    def _par_key(request_uri: str) -> str:
        return f"par:{request_uri}"

    async def pushed_authorization_request(self, client_id: str, params: dict[str, Any]) -> dict[str, Any]:
        """Store a pushed authorization request and return its ``request_uri`` (RFC 9126)."""
        request_uri = "urn:ietf:params:oauth:request_uri:" + secrets.token_urlsafe(24)
        await self._token_store.store(
            self._par_key(request_uri),
            {"client_id": client_id, "params": dict(params), "exp": int(time.time()) + 90},
        )
        return {"request_uri": request_uri, "expires_in": 90}

    async def consume_pushed_request(self, request_uri: str, client_id: str) -> dict[str, Any] | None:
        """Return (and one-time consume) the params for *request_uri* if valid for *client_id*."""
        data = await self._token_store.find(self._par_key(request_uri))
        if data is None or data.get("client_id") != client_id or data.get("exp", 0) < int(time.time()):
            return None
        await self._token_store.revoke(self._par_key(request_uri))
        return dict(data.get("params") or {})

    def verify_request_object(self, client_id: str, request_jwt: str) -> dict[str, Any]:
        """Verify a JAR request object (RFC 9101) signed with the client secret (HS256)."""
        registration = self._client_repository.find_by_registration_id(client_id)
        if registration is None or not registration.client_secret:
            raise SecurityException("Request objects require a confidential client", code="INVALID_REQUEST")
        try:
            claims: dict[str, Any] = pyjwt.decode(
                request_jwt, registration.client_secret, algorithms=["HS256"], options={"verify_aud": False}
            )
        except pyjwt.PyJWTError as exc:
            raise SecurityException(f"Invalid request object: {exc}", code="INVALID_REQUEST") from exc
        return claims

    async def _handle_authorization_code(
        self,
        registration: ClientRegistration,
        code: str,
        redirect_uri: str | None,
        code_verifier: str | None,
        confirmation: dict[str, Any] | None,
    ) -> dict[str, Any]:
        key = self._code_key(code)
        data = await self._token_store.find(key)
        if data is None:
            raise SecurityException("Invalid authorization code", code="INVALID_GRANT")

        # Single-use: a replayed code is treated as injection — revoke any tokens
        # already issued from it (RFC 9700) and reject.
        if data.get("used"):
            issued_refresh = data.get("issued_refresh")
            if issued_refresh:
                await self.revoke(issued_refresh)
            raise SecurityException("Authorization code already used", code="INVALID_GRANT")
        if data.get("client_id") != registration.client_id:
            raise SecurityException("Authorization code was issued to another client", code="INVALID_GRANT")
        if redirect_uri is not None and data.get("redirect_uri") != redirect_uri:
            raise SecurityException("redirect_uri mismatch", code="INVALID_GRANT")
        if data.get("exp", 0) < int(time.time()):
            await self._token_store.revoke(key)
            raise SecurityException("Authorization code expired", code="INVALID_GRANT")

        # PKCE verification (mandatory).
        challenge = data.get("code_challenge")
        if not code_verifier or _s256(code_verifier) != challenge:
            raise SecurityException("PKCE verification failed", code="INVALID_GRANT")

        now = int(time.time())
        scope = data.get("scope", "")
        user_id = data.get("user_id")

        access_payload: dict[str, Any] = {
            "sub": user_id,
            "scope": scope,
            "iat": now,
            "exp": now + self._access_token_ttl,
        }
        if self._issuer:
            access_payload["iss"] = self._issuer
        if self._audience is not None:
            access_payload["aud"] = self._audience
        if confirmation:
            access_payload["cnf"] = confirmation
        access_token = self._encode(access_payload)

        refresh_id = await self._issue_refresh_token(registration.client_id, scope)

        # Mark the code consumed and remember the refresh token so a replay can
        # revoke it (authorization-code injection defense).
        data["used"] = True
        data["issued_refresh"] = refresh_id
        await self._token_store.store(key, data)

        response: dict[str, Any] = {
            "access_token": access_token,
            "token_type": "Bearer",
            "expires_in": self._access_token_ttl,
            "refresh_token": refresh_id,
            "scope": scope,
        }
        if "openid" in scope.split():
            response["id_token"] = self._issue_id_token(str(user_id), registration.client_id, data.get("nonce"))
        return response

    def _issue_id_token(self, subject: str, client_id: str, nonce: str | None) -> str:
        """Issue an OIDC ID token (aud = client_id) for the openid scope."""
        now = int(time.time())
        payload: dict[str, Any] = {
            "sub": subject,
            "aud": client_id,
            "iat": now,
            "exp": now + self._access_token_ttl,
        }
        if self._issuer:
            payload["iss"] = self._issuer
        if nonce:
            payload["nonce"] = nonce
        return self._encode(payload)

    async def _handle_client_credentials(
        self, registration: ClientRegistration, scope: str, confirmation: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        now = int(time.time())
        # A client may only ever obtain scopes it is registered for. Requesting an
        # unregistered scope is rejected wholesale (RFC 6749 §5.2 ``invalid_scope``)
        # rather than silently echoed — otherwise any authenticated client could
        # mint an arbitrarily-privileged token (e.g. ``admin``) just by asking.
        if scope:
            requested = scope.split()
            unregistered = [s for s in requested if s not in registration.scopes]
            if unregistered:
                raise SecurityException(
                    f"Requested scope(s) not permitted for this client: {' '.join(unregistered)}",
                    code="INVALID_SCOPE",
                )
            scopes = requested
        else:
            scopes = registration.scopes

        access_payload: dict[str, Any] = {
            "sub": registration.client_id,
            "scope": " ".join(scopes),
            "iat": now,
            "exp": now + self._access_token_ttl,
        }
        if self._issuer:
            access_payload["iss"] = self._issuer
        if self._audience is not None:
            access_payload["aud"] = self._audience
        if confirmation:
            access_payload["cnf"] = confirmation

        access_token = self._encode(access_payload)

        scope_str = " ".join(scopes)
        refresh_token_id = await self._issue_refresh_token(registration.client_id, scope_str)

        return {
            "access_token": access_token,
            "token_type": "Bearer",
            "expires_in": self._access_token_ttl,
            "refresh_token": refresh_token_id,
            "scope": scope_str,
        }

    async def _handle_refresh_token(
        self, registration: ClientRegistration, refresh_token: str, confirmation: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        token_data = await self._token_store.find(refresh_token)
        if token_data is None:
            raise SecurityException("Invalid refresh token", code="INVALID_GRANT")

        family_id = token_data.get("family_id")
        family = await self._token_store.find(self._family_key(family_id)) if family_id else None

        # The family was already revoked (e.g. by a previous reuse) — refuse.
        if family is not None and not family.get("active", True):
            raise SecurityException("Refresh token family revoked", code="INVALID_GRANT")

        # Reuse detection (OAuth 2.1 / RFC 9700): a refresh token that was already
        # rotated is being replayed. The legitimate holder cannot do this, so we
        # treat it as theft and revoke the entire token family.
        if token_data.get("used"):
            await self._revoke_family(family_id, family)
            raise SecurityException("Refresh token reuse detected", code="INVALID_GRANT")

        # Verify client matches
        if token_data.get("client_id") != registration.client_id:
            raise SecurityException("Refresh token client mismatch", code="INVALID_GRANT")

        # Check expiration
        if token_data.get("exp", 0) < int(time.time()):
            await self._token_store.revoke(refresh_token)
            raise SecurityException("Refresh token expired", code="INVALID_GRANT")

        # Mark the presented token consumed (rotation). It is retained — not
        # deleted — so a later replay is detected as reuse rather than "unknown".
        token_data["used"] = True
        await self._token_store.store(refresh_token, token_data)

        # Issue new tokens
        now = int(time.time())
        scope = token_data.get("scope", "")

        access_payload: dict[str, Any] = {
            "sub": registration.client_id,
            "scope": scope,
            "iat": now,
            "exp": now + self._access_token_ttl,
        }
        if self._issuer:
            access_payload["iss"] = self._issuer
        if self._audience is not None:
            access_payload["aud"] = self._audience
        if confirmation:
            access_payload["cnf"] = confirmation

        access_token = self._encode(access_payload)

        new_refresh_id = await self._issue_refresh_token(registration.client_id, scope, family_id)

        return {
            "access_token": access_token,
            "token_type": "Bearer",
            "expires_in": self._access_token_ttl,
            "refresh_token": new_refresh_id,
            "scope": scope,
        }

    # ------------------------------------------------------------------
    # Refresh-token family bookkeeping (rotation + reuse detection)
    # ------------------------------------------------------------------

    @staticmethod
    def _family_key(family_id: str | None) -> str:
        return f"family:{family_id}"

    async def _issue_refresh_token(self, client_id: str, scope: str, family_id: str | None = None) -> str:
        """Mint a refresh token, creating or extending its rotation family."""
        token_id = secrets.token_urlsafe(32)
        if family_id is None:
            family_id = secrets.token_urlsafe(16)
            family: dict[str, Any] = {"client_id": client_id, "active": True, "members": [token_id]}
        else:
            family = await self._token_store.find(self._family_key(family_id)) or {
                "client_id": client_id,
                "active": True,
                "members": [],
            }
            family.setdefault("members", []).append(token_id)
        token_data = {
            "client_id": client_id,
            "scope": scope,
            "exp": int(time.time()) + self._refresh_token_ttl,
            "family_id": family_id,
            "used": False,
        }
        await self._token_store.store(token_id, token_data)
        await self._token_store.store(self._family_key(family_id), family)
        return token_id

    async def _revoke_family(self, family_id: str | None, family: dict[str, Any] | None = None) -> None:
        """Revoke an entire refresh-token family (all rotated descendants)."""
        if family_id is None:
            return
        if family is None:
            family = await self._token_store.find(self._family_key(family_id))
        if family is None:
            return
        family["active"] = False
        await self._token_store.store(self._family_key(family_id), family)
        for token_id in family.get("members", []):
            await self._token_store.revoke(token_id)

    async def revoke(self, token_id: str, *, requesting_client_id: str | None = None) -> None:
        """Revoke a refresh token (and, when known, its whole rotation family).

        Per RFC 7009 §2.1, when *requesting_client_id* is given the token is only
        revoked if it was issued to that client — a client cannot revoke another
        client's tokens. ``requesting_client_id=None`` (internal callers) revokes
        unconditionally.
        """
        token_data = await self._token_store.find(token_id)
        owner = token_data.get("client_id") if isinstance(token_data, dict) else None
        if requesting_client_id is not None and owner is not None and owner != requesting_client_id:
            return  # not the owner — refuse silently (RFC 7009 still returns 200)
        await self._token_store.revoke(token_id)
        family_id = token_data.get("family_id") if isinstance(token_data, dict) else None
        if family_id:
            await self._revoke_family(family_id)
