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
"""JWT token encoding, decoding, and SecurityContext extraction."""

from __future__ import annotations

from typing import Any

import jwt

from pyfly.kernel.exceptions import SecurityException
from pyfly.security.context import SecurityContext


class JWTService:
    """Handles JWT token operations.

    Args:
        secret: Secret key for HMAC-based signing.
        algorithm: JWT algorithm (default: HS256).
    """

    def __init__(self, secret: str, algorithm: str = "HS256") -> None:
        self._secret = secret
        self._algorithm = algorithm

    def encode(self, payload: dict[str, Any]) -> str:
        """Encode a payload into a JWT token."""
        return jwt.encode(payload, self._secret, algorithm=self._algorithm)

    def decode(self, token: str) -> dict[str, Any]:
        """Decode and validate a JWT token.

        Raises:
            SecurityException: If the token is invalid or expired.
        """
        try:
            return jwt.decode(token, self._secret, algorithms=[self._algorithm])
        except jwt.PyJWTError as exc:
            raise SecurityException(
                f"Invalid token: {exc}",
                code="INVALID_TOKEN",
            ) from exc

    def to_security_context(self, token: str) -> SecurityContext:
        """Decode a JWT token and build a SecurityContext.

        Expects the token payload to contain:
        - sub: User ID
        - roles: List of role strings (optional)
        - permissions: List of permission strings (optional)
        """
        payload = self.decode(token)
        return SecurityContext(
            user_id=payload.get("sub"),
            roles=payload.get("roles", []),
            permissions=payload.get("permissions", []),
        )
