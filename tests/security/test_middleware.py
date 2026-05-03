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
"""Tests for SecurityMiddleware JWT authentication."""

from __future__ import annotations

import time

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from pyfly.security.jwt import JWTService
from pyfly.security.middleware import SecurityMiddleware

TEST_SECRET = "test-secret-key-minimum-32-chars!"


def _make_whoami_endpoint():
    """Create a test endpoint that returns security context info."""

    async def whoami(request: Request) -> JSONResponse:
        ctx = request.state.security_context
        return JSONResponse(
            {
                "authenticated": ctx.is_authenticated,
                "user_id": ctx.user_id,
                "roles": ctx.roles,
                "permissions": ctx.permissions,
            }
        )

    return whoami


def _create_test_app(
    jwt_service: JWTService | None = None,
    exclude_paths: tuple[str, ...] = (),
) -> Starlette:
    """Create a minimal Starlette app with SecurityMiddleware for testing."""
    if jwt_service is None:
        jwt_service = JWTService(secret=TEST_SECRET)

    return Starlette(
        routes=[
            Route("/whoami", _make_whoami_endpoint()),
            Route("/health", _make_whoami_endpoint()),
        ],
        middleware=[
            Middleware(SecurityMiddleware, jwt_service=jwt_service, exclude_paths=exclude_paths),
        ],
    )


class TestSecurityMiddleware:
    def test_valid_bearer_token(self):
        """Valid JWT with sub/roles should produce an authenticated context."""
        jwt_service = JWTService(secret=TEST_SECRET)
        app = _create_test_app(jwt_service=jwt_service)
        client = TestClient(app)

        token = jwt_service.encode(
            {
                "sub": "user-42",
                "roles": ["ADMIN", "USER"],
                "permissions": ["order:read", "order:write"],
            }
        )

        response = client.get("/whoami", headers={"Authorization": f"Bearer {token}"})
        assert response.status_code == 200

        body = response.json()
        assert body["authenticated"] is True
        assert body["user_id"] == "user-42"
        assert body["roles"] == ["ADMIN", "USER"]
        assert body["permissions"] == ["order:read", "order:write"]

    def test_missing_authorization_header(self):
        """No Authorization header should produce an anonymous context."""
        app = _create_test_app()
        client = TestClient(app)

        response = client.get("/whoami")
        assert response.status_code == 200

        body = response.json()
        assert body["authenticated"] is False
        assert body["user_id"] is None
        assert body["roles"] == []

    def test_invalid_token(self):
        """A garbage JWT string should produce an anonymous context (no 401)."""
        app = _create_test_app()
        client = TestClient(app)

        response = client.get("/whoami", headers={"Authorization": "Bearer not.a.valid.jwt"})
        assert response.status_code == 200

        body = response.json()
        assert body["authenticated"] is False
        assert body["user_id"] is None

    def test_expired_token(self):
        """An expired JWT should produce an anonymous context."""
        jwt_service = JWTService(secret=TEST_SECRET)
        app = _create_test_app(jwt_service=jwt_service)
        client = TestClient(app)

        # Create a token that expired 1 hour ago
        token = jwt_service.encode(
            {
                "sub": "user-expired",
                "roles": ["USER"],
                "exp": int(time.time()) - 3600,
            }
        )

        response = client.get("/whoami", headers={"Authorization": f"Bearer {token}"})
        assert response.status_code == 200

        body = response.json()
        assert body["authenticated"] is False
        assert body["user_id"] is None

    def test_malformed_auth_header(self):
        """'Basic xyz' instead of 'Bearer ...' should produce an anonymous context."""
        app = _create_test_app()
        client = TestClient(app)

        response = client.get("/whoami", headers={"Authorization": "Basic dXNlcjpwYXNz"})
        assert response.status_code == 200

        body = response.json()
        assert body["authenticated"] is False
        assert body["user_id"] is None

    def test_exclude_paths(self):
        """Requests to excluded paths should get anonymous context even with a valid token."""
        jwt_service = JWTService(secret=TEST_SECRET)
        app = _create_test_app(jwt_service=jwt_service, exclude_paths=("/health",))
        client = TestClient(app)

        token = jwt_service.encode(
            {
                "sub": "user-42",
                "roles": ["ADMIN"],
            }
        )

        # The /health path is excluded — should be anonymous even with valid token
        response = client.get("/health", headers={"Authorization": f"Bearer {token}"})
        assert response.status_code == 200

        body = response.json()
        assert body["authenticated"] is False
        assert body["user_id"] is None

        # The /whoami path is NOT excluded — should be authenticated
        response = client.get("/whoami", headers={"Authorization": f"Bearer {token}"})
        assert response.status_code == 200

        body = response.json()
        assert body["authenticated"] is True
        assert body["user_id"] == "user-42"

    def test_bearer_case_sensitivity(self):
        """'bearer ' (lowercase) should NOT match per RFC 6750 — result is anonymous."""
        jwt_service = JWTService(secret=TEST_SECRET)
        app = _create_test_app(jwt_service=jwt_service)
        client = TestClient(app)

        token = jwt_service.encode({"sub": "user-1", "roles": ["USER"]})

        response = client.get("/whoami", headers={"Authorization": f"bearer {token}"})
        assert response.status_code == 200

        body = response.json()
        assert body["authenticated"] is False
        assert body["user_id"] is None
