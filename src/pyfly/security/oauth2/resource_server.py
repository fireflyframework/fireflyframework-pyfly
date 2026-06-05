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
"""OAuth2 Resource Server — JWKS-based JWT validation."""

from __future__ import annotations

from typing import Any

import jwt
from jwt import PyJWKClient

from pyfly.kernel.exceptions import SecurityException
from pyfly.security.context import SecurityContext


class JWKSTokenValidator:
    """Validates RS256-signed JWTs using a remote JWKS endpoint.

    Fetches public keys from the JWKS URI and caches them.
    Extracts claims to build a SecurityContext.

    Args:
        jwks_uri: The JWKS endpoint URL (e.g.,
            ``"https://auth.example.com/.well-known/jwks.json"``).
        issuer: Expected token issuer (optional, validates ``iss`` claim if
            set).
        audience: Expected token audience (optional, validates ``aud`` claim
            if set).
        algorithms: Allowed algorithms (default: ``["RS256"]``).
    """

    def __init__(
        self,
        jwks_uri: str,
        issuer: str | None = None,
        audience: str | None = None,
        algorithms: list[str] | None = None,
    ) -> None:
        self._jwks_client = PyJWKClient(jwks_uri)
        self._issuer = issuer
        self._audience = audience
        self._algorithms = algorithms or ["RS256"]

    def validate(self, token: str) -> dict[str, Any]:
        """Validate a JWT token and return the decoded payload.

        Uses JWKS to fetch the signing key matching the token's ``kid``
        header.

        Raises:
            SecurityException: If the token is invalid, expired, or key not
                found.
        """
        try:
            signing_key = self._jwks_client.get_signing_key_from_jwt(token)
            payload = jwt.decode(
                token,
                signing_key.key,
                algorithms=self._algorithms,
                issuer=self._issuer,
                audience=self._audience,
                options={"require": ["exp"]},
            )
            return payload
        except jwt.PyJWTError as exc:
            raise SecurityException(
                f"Token validation failed: {exc}",
                code="INVALID_TOKEN",
            ) from exc

    def to_security_context(self, token: str) -> SecurityContext:
        """Validate token and build a :class:`SecurityContext` from claims.

        Expects standard claims:

        - ``sub``: maps to *user_id*
        - ``roles`` or ``realm_access.roles``: maps to *roles*
        - ``scope`` or ``permissions``: maps to *permissions*
        """
        payload = self.validate(token)

        # Extract roles — support both flat "roles" claim and Keycloak's
        # nested structure.
        roles = payload.get("roles", [])
        if not roles:
            realm_access = payload.get("realm_access", {})
            if isinstance(realm_access, dict):
                roles = realm_access.get("roles", [])

        # Extract permissions — support "permissions" or "scope"
        # (space-separated).
        permissions = payload.get("permissions", [])
        if not permissions:
            scope = payload.get("scope", "")
            if isinstance(scope, str) and scope:
                permissions = scope.split()

        return SecurityContext(
            user_id=payload.get("sub"),
            roles=roles,
            permissions=permissions,
        )
