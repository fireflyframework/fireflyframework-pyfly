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

import secrets
import time
from typing import Any, Protocol

import jwt as pyjwt

from pyfly.kernel.exceptions import SecurityException
from pyfly.security.oauth2.client import ClientRegistration, ClientRegistrationRepository

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
        secret: Secret key for signing tokens (HS256).
        client_repository: Repository to look up client registrations.
        token_store: Store for refresh tokens.
        access_token_ttl: Access token lifetime in seconds (default: 3600 = 1 hour).
        refresh_token_ttl: Refresh token lifetime in seconds (default: 86400 = 24 hours).
        issuer: Token issuer claim (optional).
    """

    def __init__(
        self,
        secret: str,
        client_repository: ClientRegistrationRepository,
        token_store: TokenStore,
        access_token_ttl: int = 3600,
        refresh_token_ttl: int = 86400,
        issuer: str | None = None,
    ) -> None:
        self._secret = secret
        self._client_repository = client_repository
        self._token_store = token_store
        self._access_token_ttl = access_token_ttl
        self._refresh_token_ttl = refresh_token_ttl
        self._issuer = issuer

    async def token(
        self,
        grant_type: str,
        client_id: str,
        client_secret: str,
        scope: str = "",
        refresh_token: str | None = None,
    ) -> dict[str, Any]:
        """Issue tokens based on grant type.

        Args:
            grant_type: "client_credentials" or "refresh_token"
            client_id: The client's ID
            client_secret: The client's secret
            scope: Space-separated scopes (for client_credentials)
            refresh_token: The refresh token (for refresh_token grant)

        Returns:
            Token response dict with access_token, token_type, expires_in,
            and optionally refresh_token.

        Raises:
            SecurityException: If authentication fails or grant type is unsupported.
        """
        # Authenticate client
        registration = self._client_repository.find_by_registration_id(client_id)
        if registration is None or registration.client_secret != client_secret:
            raise SecurityException("Invalid client credentials", code="INVALID_CLIENT")

        if grant_type == "client_credentials":
            return await self._handle_client_credentials(registration, scope)
        elif grant_type == "refresh_token":
            if refresh_token is None:
                raise SecurityException("Refresh token required", code="INVALID_REQUEST")
            return await self._handle_refresh_token(registration, refresh_token)
        else:
            raise SecurityException(
                f"Unsupported grant type: {grant_type}",
                code="UNSUPPORTED_GRANT_TYPE",
            )

    async def _handle_client_credentials(self, registration: ClientRegistration, scope: str) -> dict[str, Any]:
        now = int(time.time())
        scopes = scope.split() if scope else registration.scopes

        access_payload: dict[str, Any] = {
            "sub": registration.client_id,
            "scope": " ".join(scopes),
            "iat": now,
            "exp": now + self._access_token_ttl,
        }
        if self._issuer:
            access_payload["iss"] = self._issuer

        access_token = pyjwt.encode(access_payload, self._secret, algorithm="HS256")

        # Generate refresh token
        refresh_token_id = secrets.token_urlsafe(32)
        refresh_data = {
            "client_id": registration.client_id,
            "scope": " ".join(scopes),
            "exp": now + self._refresh_token_ttl,
        }
        await self._token_store.store(refresh_token_id, refresh_data)

        return {
            "access_token": access_token,
            "token_type": "Bearer",
            "expires_in": self._access_token_ttl,
            "refresh_token": refresh_token_id,
            "scope": " ".join(scopes),
        }

    async def _handle_refresh_token(self, registration: ClientRegistration, refresh_token: str) -> dict[str, Any]:
        token_data = await self._token_store.find(refresh_token)
        if token_data is None:
            raise SecurityException("Invalid refresh token", code="INVALID_GRANT")

        # Verify client matches
        if token_data.get("client_id") != registration.client_id:
            raise SecurityException("Refresh token client mismatch", code="INVALID_GRANT")

        # Check expiration
        if token_data.get("exp", 0) < int(time.time()):
            await self._token_store.revoke(refresh_token)
            raise SecurityException("Refresh token expired", code="INVALID_GRANT")

        # Revoke old refresh token (rotation)
        await self._token_store.revoke(refresh_token)

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

        access_token = pyjwt.encode(access_payload, self._secret, algorithm="HS256")

        # New refresh token
        new_refresh_id = secrets.token_urlsafe(32)
        new_refresh_data = {
            "client_id": registration.client_id,
            "scope": scope,
            "exp": now + self._refresh_token_ttl,
        }
        await self._token_store.store(new_refresh_id, new_refresh_data)

        return {
            "access_token": access_token,
            "token_type": "Bearer",
            "expires_in": self._access_token_ttl,
            "refresh_token": new_refresh_id,
            "scope": scope,
        }

    async def revoke(self, token_id: str) -> None:
        """Revoke a refresh token."""
        await self._token_store.revoke(token_id)
