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
"""Tests for the OAuth2 Authorization Server — token endpoint."""

from __future__ import annotations

import jwt as pyjwt
import pytest

from pyfly.kernel.exceptions import SecurityException
from pyfly.security.oauth2.authorization_server import (
    AuthorizationServer,
    InMemoryTokenStore,
)
from pyfly.security.oauth2.client import (
    ClientRegistration,
    InMemoryClientRegistrationRepository,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def client_repo() -> InMemoryClientRegistrationRepository:
    reg = ClientRegistration(
        registration_id="test-client",
        client_id="test-client",
        client_secret="test-secret",
        authorization_grant_type="client_credentials",
        scopes=["read", "write"],
    )
    return InMemoryClientRegistrationRepository(reg)


@pytest.fixture
def token_store() -> InMemoryTokenStore:
    return InMemoryTokenStore()


@pytest.fixture
def auth_server(
    client_repo: InMemoryClientRegistrationRepository,
    token_store: InMemoryTokenStore,
) -> AuthorizationServer:
    return AuthorizationServer(
        secret="test-signing-secret",
        client_repository=client_repo,
        token_store=token_store,
        issuer="https://auth.example.com",
    )


# ---------------------------------------------------------------------------
# client_credentials grant
# ---------------------------------------------------------------------------


class TestClientCredentialsGrant:
    """Tests for the client_credentials grant type."""

    @pytest.mark.asyncio
    async def test_client_credentials_grant(self, auth_server: AuthorizationServer) -> None:
        """client_credentials grant issues access_token, refresh_token, and correct keys."""
        result = await auth_server.token(
            grant_type="client_credentials",
            client_id="test-client",
            client_secret="test-secret",
        )

        assert "access_token" in result
        assert result["token_type"] == "Bearer"
        assert result["expires_in"] == 3600
        assert "refresh_token" in result
        assert "scope" in result

    @pytest.mark.asyncio
    async def test_client_credentials_decodes_valid_jwt(self, auth_server: AuthorizationServer) -> None:
        """Access token decodes to valid JWT with correct sub, scope, and iss claims."""
        result = await auth_server.token(
            grant_type="client_credentials",
            client_id="test-client",
            client_secret="test-secret",
        )

        payload = pyjwt.decode(
            result["access_token"],
            "test-signing-secret",
            algorithms=["HS256"],
        )

        assert payload["sub"] == "test-client"
        assert payload["scope"] == "read write"
        assert payload["iss"] == "https://auth.example.com"
        assert "iat" in payload
        assert "exp" in payload

    @pytest.mark.asyncio
    async def test_client_credentials_requested_scope_subset(self, auth_server: AuthorizationServer) -> None:
        """A requested scope that is a subset of the registration's scopes is honoured."""
        result = await auth_server.token(
            grant_type="client_credentials",
            client_id="test-client",
            client_secret="test-secret",
            scope="read",
        )

        payload = pyjwt.decode(
            result["access_token"],
            "test-signing-secret",
            algorithms=["HS256"],
        )

        assert payload["scope"] == "read"
        assert result["scope"] == "read"

    @pytest.mark.asyncio
    async def test_client_credentials_rejects_unregistered_scope(self, auth_server: AuthorizationServer) -> None:
        """Requesting a scope the client is not registered for is rejected (RFC 6749 §5.2).

        Prevents privilege escalation: a client registered for ``read write`` must not
        be able to mint an ``admin`` token by simply asking for it.
        """
        with pytest.raises(SecurityException) as exc_info:
            await auth_server.token(
                grant_type="client_credentials",
                client_id="test-client",
                client_secret="test-secret",
                scope="admin superuser",
            )
        assert exc_info.value.code == "INVALID_SCOPE"

    @pytest.mark.asyncio
    async def test_client_credentials_partial_unregistered_scope_rejected(
        self, auth_server: AuthorizationServer
    ) -> None:
        """A request mixing a registered and an unregistered scope is rejected wholesale."""
        with pytest.raises(SecurityException) as exc_info:
            await auth_server.token(
                grant_type="client_credentials",
                client_id="test-client",
                client_secret="test-secret",
                scope="read admin",
            )
        assert exc_info.value.code == "INVALID_SCOPE"


# ---------------------------------------------------------------------------
# Audience-restricted tokens
# ---------------------------------------------------------------------------


class TestAudienceClaim:
    """Tokens carry an ``aud`` claim only when an audience is configured."""

    @pytest.fixture
    def auth_server_with_aud(
        self,
        client_repo: InMemoryClientRegistrationRepository,
        token_store: InMemoryTokenStore,
    ) -> AuthorizationServer:
        return AuthorizationServer(
            secret="test-signing-secret",
            client_repository=client_repo,
            token_store=token_store,
            issuer="https://auth.example.com",
            audience="api://lumen",
        )

    @pytest.mark.asyncio
    async def test_client_credentials_token_includes_aud(self, auth_server_with_aud: AuthorizationServer) -> None:
        result = await auth_server_with_aud.token(
            grant_type="client_credentials",
            client_id="test-client",
            client_secret="test-secret",
        )
        payload = pyjwt.decode(
            result["access_token"], "test-signing-secret", algorithms=["HS256"], audience="api://lumen"
        )
        assert payload["aud"] == "api://lumen"

    @pytest.mark.asyncio
    async def test_refreshed_token_includes_aud(self, auth_server_with_aud: AuthorizationServer) -> None:
        initial = await auth_server_with_aud.token(
            grant_type="client_credentials", client_id="test-client", client_secret="test-secret"
        )
        refreshed = await auth_server_with_aud.token(
            grant_type="refresh_token",
            client_id="test-client",
            client_secret="test-secret",
            refresh_token=initial["refresh_token"],
        )
        payload = pyjwt.decode(
            refreshed["access_token"], "test-signing-secret", algorithms=["HS256"], audience="api://lumen"
        )
        assert payload["aud"] == "api://lumen"

    @pytest.mark.asyncio
    async def test_no_aud_claim_when_audience_not_configured(self, auth_server: AuthorizationServer) -> None:
        """Backward-compatible: tokens carry no ``aud`` unless an audience is set."""
        result = await auth_server.token(
            grant_type="client_credentials", client_id="test-client", client_secret="test-secret"
        )
        payload = pyjwt.decode(result["access_token"], "test-signing-secret", algorithms=["HS256"])
        assert "aud" not in payload


# ---------------------------------------------------------------------------
# refresh_token grant
# ---------------------------------------------------------------------------


class TestRefreshTokenGrant:
    """Tests for the refresh_token grant type."""

    @pytest.mark.asyncio
    async def test_refresh_token_grant(self, auth_server: AuthorizationServer) -> None:
        """Refresh token from client_credentials can be exchanged for new tokens."""
        initial = await auth_server.token(
            grant_type="client_credentials",
            client_id="test-client",
            client_secret="test-secret",
        )

        refreshed = await auth_server.token(
            grant_type="refresh_token",
            client_id="test-client",
            client_secret="test-secret",
            refresh_token=initial["refresh_token"],
        )

        assert "access_token" in refreshed
        assert refreshed["token_type"] == "Bearer"
        assert refreshed["expires_in"] == 3600
        assert "refresh_token" in refreshed
        # Refresh token is always new (random); access token may match if
        # issued within the same second (deterministic claims + same iat).
        assert refreshed["refresh_token"] != initial["refresh_token"]

    @pytest.mark.asyncio
    async def test_refresh_token_rotation(
        self,
        auth_server: AuthorizationServer,
        token_store: InMemoryTokenStore,
    ) -> None:
        """Old refresh token is revoked after use (rotation)."""
        initial = await auth_server.token(
            grant_type="client_credentials",
            client_id="test-client",
            client_secret="test-secret",
        )

        old_refresh = initial["refresh_token"]

        # Use the refresh token
        await auth_server.token(
            grant_type="refresh_token",
            client_id="test-client",
            client_secret="test-secret",
            refresh_token=old_refresh,
        )

        # Attempting to reuse the old (rotated) refresh token must fail.
        with pytest.raises(SecurityException) as exc_info:
            await auth_server.token(
                grant_type="refresh_token",
                client_id="test-client",
                client_secret="test-secret",
                refresh_token=old_refresh,
            )
        assert exc_info.value.code == "INVALID_GRANT"


class TestRefreshTokenReuseDetection:
    """OAuth 2.1 / RFC 9700: replaying a rotated refresh token revokes the whole family."""

    @pytest.mark.asyncio
    async def test_reuse_of_rotated_token_revokes_active_descendant(self, auth_server: AuthorizationServer) -> None:
        initial = await auth_server.token(
            grant_type="client_credentials", client_id="test-client", client_secret="test-secret"
        )
        rt1 = initial["refresh_token"]

        # Rotate rt1 -> rt2 (rt2 is the live token).
        second = await auth_server.token(
            grant_type="refresh_token", client_id="test-client", client_secret="test-secret", refresh_token=rt1
        )
        rt2 = second["refresh_token"]

        # Replay the consumed rt1 -> reuse detected.
        with pytest.raises(SecurityException) as exc_info:
            await auth_server.token(
                grant_type="refresh_token", client_id="test-client", client_secret="test-secret", refresh_token=rt1
            )
        assert exc_info.value.code == "INVALID_GRANT"

        # The whole family is now revoked: the previously-live rt2 no longer works.
        with pytest.raises(SecurityException) as exc_info2:
            await auth_server.token(
                grant_type="refresh_token", client_id="test-client", client_secret="test-secret", refresh_token=rt2
            )
        assert exc_info2.value.code == "INVALID_GRANT"


# ---------------------------------------------------------------------------
# Error cases
# ---------------------------------------------------------------------------


class TestAuthorizationServerErrors:
    """Tests for error handling in the authorization server."""

    @pytest.mark.asyncio
    async def test_invalid_client_id(self, auth_server: AuthorizationServer) -> None:
        """Unknown client_id raises SecurityException with INVALID_CLIENT."""
        with pytest.raises(SecurityException) as exc_info:
            await auth_server.token(
                grant_type="client_credentials",
                client_id="unknown-client",
                client_secret="test-secret",
            )
        assert exc_info.value.code == "INVALID_CLIENT"

    @pytest.mark.asyncio
    async def test_invalid_client_secret(self, auth_server: AuthorizationServer) -> None:
        """Wrong client_secret raises SecurityException with INVALID_CLIENT."""
        with pytest.raises(SecurityException) as exc_info:
            await auth_server.token(
                grant_type="client_credentials",
                client_id="test-client",
                client_secret="wrong-secret",
            )
        assert exc_info.value.code == "INVALID_CLIENT"

    @pytest.mark.asyncio
    async def test_unsupported_grant_type(self, auth_server: AuthorizationServer) -> None:
        """Unsupported grant type raises SecurityException with UNSUPPORTED_GRANT_TYPE."""
        with pytest.raises(SecurityException) as exc_info:
            await auth_server.token(
                grant_type="authorization_code",
                client_id="test-client",
                client_secret="test-secret",
            )
        assert exc_info.value.code == "UNSUPPORTED_GRANT_TYPE"

    @pytest.mark.asyncio
    async def test_invalid_refresh_token(self, auth_server: AuthorizationServer) -> None:
        """Unknown refresh token raises SecurityException with INVALID_GRANT."""
        with pytest.raises(SecurityException) as exc_info:
            await auth_server.token(
                grant_type="refresh_token",
                client_id="test-client",
                client_secret="test-secret",
                refresh_token="nonexistent-token",
            )
        assert exc_info.value.code == "INVALID_GRANT"

    @pytest.mark.asyncio
    async def test_refresh_token_required(self, auth_server: AuthorizationServer) -> None:
        """refresh_token grant without a refresh token raises INVALID_REQUEST."""
        with pytest.raises(SecurityException) as exc_info:
            await auth_server.token(
                grant_type="refresh_token",
                client_id="test-client",
                client_secret="test-secret",
            )
        assert exc_info.value.code == "INVALID_REQUEST"


# ---------------------------------------------------------------------------
# Revocation
# ---------------------------------------------------------------------------


class TestTokenRevocation:
    """Tests for token revocation."""

    @pytest.mark.asyncio
    async def test_revoke_token(self, auth_server: AuthorizationServer) -> None:
        """Revoking a refresh token makes subsequent use fail."""
        result = await auth_server.token(
            grant_type="client_credentials",
            client_id="test-client",
            client_secret="test-secret",
        )

        refresh = result["refresh_token"]
        await auth_server.revoke(refresh)

        with pytest.raises(SecurityException) as exc_info:
            await auth_server.token(
                grant_type="refresh_token",
                client_id="test-client",
                client_secret="test-secret",
                refresh_token=refresh,
            )
        assert exc_info.value.code == "INVALID_GRANT"


# ---------------------------------------------------------------------------
# InMemoryTokenStore
# ---------------------------------------------------------------------------


class TestInMemoryTokenStore:
    """Tests for :class:`InMemoryTokenStore` operations."""

    @pytest.mark.asyncio
    async def test_store_and_find(self) -> None:
        """Stored token data can be retrieved by token_id."""
        store = InMemoryTokenStore()
        data = {"client_id": "c1", "scope": "read", "exp": 9999999999}
        await store.store("tok-1", data)

        found = await store.find("tok-1")
        assert found == data

    @pytest.mark.asyncio
    async def test_find_nonexistent(self) -> None:
        """Finding a nonexistent token_id returns None."""
        store = InMemoryTokenStore()

        assert await store.find("missing") is None

    @pytest.mark.asyncio
    async def test_revoke(self) -> None:
        """Revoking a token removes it from the store."""
        store = InMemoryTokenStore()
        await store.store("tok-2", {"client_id": "c1"})

        await store.revoke("tok-2")
        assert await store.find("tok-2") is None

    @pytest.mark.asyncio
    async def test_revoke_nonexistent(self) -> None:
        """Revoking a nonexistent token does not raise."""
        store = InMemoryTokenStore()
        await store.revoke("missing")  # should not raise
