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
"""OAuth2 authorization-server HTTP endpoints (token / introspect / revoke / jwks)."""

from __future__ import annotations

from typing import Any

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from pyfly.security.oauth2.authorization_server import AuthorizationServer, InMemoryTokenStore
from pyfly.security.oauth2.client import ClientRegistration, InMemoryClientRegistrationRepository
from pyfly.security.oauth2.endpoints import AuthorizationServerEndpoints

_SECRET = "authorization-server-secret-32bytes!!"


def _server() -> AuthorizationServer:
    repo = InMemoryClientRegistrationRepository(
        ClientRegistration(
            registration_id="svc",
            client_id="svc",
            client_secret="svc-secret",
            authorization_grant_type="client_credentials",
            scopes=["read", "write"],
        )
    )
    return AuthorizationServer(
        secret=_SECRET, client_repository=repo, token_store=InMemoryTokenStore(), issuer="https://as"
    )


def _client(server: AuthorizationServer | None = None) -> TestClient:
    server = server or _server()
    app = Starlette(routes=AuthorizationServerEndpoints(server).routes())
    return TestClient(app)


class TestIntrospectMethod:
    @pytest.mark.asyncio
    async def test_active_access_token(self) -> None:
        server = _server()
        tok = await server.token(grant_type="client_credentials", client_id="svc", client_secret="svc-secret")
        result = await server.introspect(tok["access_token"])
        assert result["active"] is True
        assert result["sub"] == "svc"
        assert result["scope"] == "read write"

    @pytest.mark.asyncio
    async def test_active_refresh_token(self) -> None:
        server = _server()
        tok = await server.token(grant_type="client_credentials", client_id="svc", client_secret="svc-secret")
        result = await server.introspect(tok["refresh_token"])
        assert result["active"] is True
        assert result["token_type"] == "refresh_token"

    @pytest.mark.asyncio
    async def test_unknown_token_inactive(self) -> None:
        assert (await _server().introspect("garbage"))["active"] is False


class TestEndpoints:
    def test_token_endpoint_issues_token(self) -> None:
        resp = _client().post(
            "/oauth2/token",
            data={"grant_type": "client_credentials", "client_id": "svc", "client_secret": "svc-secret"},
        )
        assert resp.status_code == 200
        assert "access_token" in resp.json()

    def test_token_endpoint_bad_secret(self) -> None:
        resp = _client().post(
            "/oauth2/token",
            data={"grant_type": "client_credentials", "client_id": "svc", "client_secret": "WRONG"},
        )
        assert resp.status_code == 401
        assert resp.json()["error"] == "invalid_client"

    def test_jwks_endpoint(self) -> None:
        resp = _client().get("/oauth2/jwks")
        assert resp.status_code == 200
        assert resp.json() == {"keys": []}  # HS256 server publishes no keys

    def test_introspect_requires_client_auth(self) -> None:
        resp = _client().post("/oauth2/introspect", data={"token": "x"})
        assert resp.status_code == 401

    def test_introspect_active_then_revoke(self) -> None:
        server = _server()
        client = _client(server)
        issued = client.post(
            "/oauth2/token",
            data={"grant_type": "client_credentials", "client_id": "svc", "client_secret": "svc-secret"},
        ).json()
        rt = issued["refresh_token"]
        auth = {"client_id": "svc", "client_secret": "svc-secret"}

        introspected = client.post("/oauth2/introspect", data={"token": rt, **auth})
        assert introspected.status_code == 200
        assert introspected.json()["active"] is True

        revoked = client.post("/oauth2/revoke", data={"token": rt, **auth})
        assert revoked.status_code == 200

        again = client.post("/oauth2/introspect", data={"token": rt, **auth})
        assert again.json()["active"] is False


class TestOpaqueTokenIntrospector:
    def test_active_token_builds_context(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from pyfly.security.oauth2.resource_server import OpaqueTokenIntrospector

        introspector = OpaqueTokenIntrospector(
            "https://as/oauth2/introspect", client_id="rs", client_secret="rs-secret"
        )

        class _Resp:
            status_code = 200

            def json(self) -> dict[str, Any]:
                return {"active": True, "sub": "user-1", "scope": "read write", "roles": ["ADMIN"]}

        class _C:
            def __enter__(self) -> _C:
                return self

            def __exit__(self, *a: object) -> None:
                return None

            def post(self, *a: Any, **k: Any) -> _Resp:
                return _Resp()

        import httpx

        monkeypatch.setattr(httpx, "Client", lambda *a, **k: _C())
        ctx = introspector.to_security_context("opaque-token")
        assert ctx.user_id == "user-1"
        assert "read" in ctx.permissions

    def test_inactive_token_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from pyfly.kernel.exceptions import SecurityException
        from pyfly.security.oauth2.resource_server import OpaqueTokenIntrospector

        introspector = OpaqueTokenIntrospector("https://as/introspect", client_id="rs", client_secret="s")

        class _Resp:
            status_code = 200

            def json(self) -> dict[str, Any]:
                return {"active": False}

        class _C:
            def __enter__(self) -> _C:
                return self

            def __exit__(self, *a: object) -> None:
                return None

            def post(self, *a: Any, **k: Any) -> _Resp:
                return _Resp()

        import httpx

        monkeypatch.setattr(httpx, "Client", lambda *a, **k: _C())
        with pytest.raises(SecurityException):
            introspector.introspect("opaque-token")
