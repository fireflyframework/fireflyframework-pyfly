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
"""Tests for the OAuth2 Resource Server JWKS-based token validation."""

from __future__ import annotations

import time
from unittest.mock import MagicMock, patch

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from pyfly.kernel.exceptions import SecurityException
from pyfly.security.context import SecurityContext
from pyfly.security.oauth2.resource_server import JWKSTokenValidator

# ---------------------------------------------------------------------------
# Test RSA key pair (generated once per module)
# ---------------------------------------------------------------------------
_private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
_public_key = _private_key.public_key()


def _create_test_token(payload: dict, kid: str = "test-kid") -> str:
    """Create an RS256-signed JWT for testing (adds an ``exp`` claim by default)."""
    if "exp" not in payload:
        payload = {**payload, "exp": int(time.time()) + 3600}
    private_pem = _private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    return jwt.encode(payload, private_pem, algorithm="RS256", headers={"kid": kid})


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def mock_jwks_client():
    """Mock PyJWKClient to return our test public key."""
    with patch("pyfly.security.oauth2.resource_server.PyJWKClient") as mock_cls:
        mock_instance = MagicMock()
        mock_cls.return_value = mock_instance

        # Create a signing key mock that returns the test public key.
        mock_signing_key = MagicMock()
        mock_signing_key.key = _public_key
        mock_instance.get_signing_key_from_jwt.return_value = mock_signing_key

        yield mock_instance


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestJWKSTokenValidator:
    """Tests for :class:`JWKSTokenValidator`."""

    def test_validate_valid_token(self, mock_jwks_client: MagicMock) -> None:
        """A valid RS256 token should decode successfully."""
        validator = JWKSTokenValidator(jwks_uri="https://auth.example.com/.well-known/jwks.json")
        token = _create_test_token({"sub": "user-1", "name": "Alice"})

        payload = validator.validate(token)

        assert payload["sub"] == "user-1"
        assert payload["name"] == "Alice"
        mock_jwks_client.get_signing_key_from_jwt.assert_called_once_with(token)

    def test_validate_invalid_token(self, mock_jwks_client: MagicMock) -> None:
        """An invalid token should raise SecurityException."""
        mock_jwks_client.get_signing_key_from_jwt.side_effect = jwt.PyJWTError(
            "Key not found",
        )
        validator = JWKSTokenValidator(jwks_uri="https://auth.example.com/.well-known/jwks.json")

        with pytest.raises(SecurityException, match="Token validation failed") as exc_info:
            validator.validate("garbage.token.value")

        assert exc_info.value.code == "INVALID_TOKEN"

    def test_to_security_context_basic(self, mock_jwks_client: MagicMock) -> None:
        """Token with sub/roles/permissions maps to a SecurityContext."""
        validator = JWKSTokenValidator(jwks_uri="https://auth.example.com/.well-known/jwks.json")
        token = _create_test_token(
            {
                "sub": "user-42",
                "roles": ["admin", "editor"],
                "permissions": ["read", "write"],
            },
        )

        ctx = validator.to_security_context(token)

        assert isinstance(ctx, SecurityContext)
        assert ctx.user_id == "user-42"
        assert ctx.roles == ["admin", "editor"]
        assert ctx.permissions == ["read", "write"]
        assert ctx.is_authenticated is True

    def test_to_security_context_keycloak_roles(self, mock_jwks_client: MagicMock) -> None:
        """Keycloak-style realm_access.roles should be extracted."""
        validator = JWKSTokenValidator(jwks_uri="https://auth.example.com/.well-known/jwks.json")
        token = _create_test_token(
            {
                "sub": "kc-user",
                "realm_access": {"roles": ["realm-admin", "realm-viewer"]},
            },
        )

        ctx = validator.to_security_context(token)

        assert ctx.user_id == "kc-user"
        assert ctx.roles == ["realm-admin", "realm-viewer"]

    def test_to_security_context_scope_as_permissions(
        self,
        mock_jwks_client: MagicMock,
    ) -> None:
        """Space-separated 'scope' claim should be split into permissions."""
        validator = JWKSTokenValidator(jwks_uri="https://auth.example.com/.well-known/jwks.json")
        token = _create_test_token({"sub": "scope-user", "scope": "read write delete"})

        ctx = validator.to_security_context(token)

        assert ctx.permissions == ["read", "write", "delete"]

    def test_validate_with_issuer(self, mock_jwks_client: MagicMock) -> None:
        """Issuer claim must match when issuer is configured."""
        validator = JWKSTokenValidator(
            jwks_uri="https://auth.example.com/.well-known/jwks.json",
            issuer="https://auth.example.com",
        )

        # Valid issuer should succeed.
        valid_token = _create_test_token(
            {"sub": "user-1", "iss": "https://auth.example.com"},
        )
        payload = validator.validate(valid_token)
        assert payload["sub"] == "user-1"

        # Mismatched issuer should raise SecurityException.
        bad_token = _create_test_token(
            {"sub": "user-1", "iss": "https://evil.example.com"},
        )
        with pytest.raises(SecurityException, match="Token validation failed"):
            validator.validate(bad_token)

    def test_validate_with_audience(self, mock_jwks_client: MagicMock) -> None:
        """Audience claim must match when audience is configured."""
        validator = JWKSTokenValidator(
            jwks_uri="https://auth.example.com/.well-known/jwks.json",
            audience="my-api",
        )

        # Valid audience should succeed.
        valid_token = _create_test_token({"sub": "user-1", "aud": "my-api"})
        payload = validator.validate(valid_token)
        assert payload["sub"] == "user-1"

        # Mismatched audience should raise SecurityException.
        bad_token = _create_test_token({"sub": "user-1", "aud": "other-api"})
        with pytest.raises(SecurityException, match="Token validation failed"):
            validator.validate(bad_token)
