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
"""Dynamic Client Registration (RFC 7591)."""

from __future__ import annotations

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from pyfly.kernel.exceptions import SecurityException
from pyfly.security.oauth2.authorization_server import AuthorizationServer, InMemoryTokenStore
from pyfly.security.oauth2.client import InMemoryClientRegistrationRepository
from pyfly.security.oauth2.endpoints import AuthorizationServerEndpoints

_SECRET = "authorization-server-secret-32bytes!!"


def _server(**kwargs: object) -> AuthorizationServer:
    return AuthorizationServer(
        secret=_SECRET,
        client_repository=InMemoryClientRegistrationRepository(),
        token_store=InMemoryTokenStore(),
        **kwargs,  # type: ignore[arg-type]
    )


class TestRegisterClientMethod:
    @pytest.mark.asyncio
    async def test_registration_creates_usable_client(self) -> None:
        server = _server(allow_dynamic_registration=True)
        result = await server.register_client(
            {"client_name": "app", "grant_types": ["client_credentials"], "scope": "read"}
        )
        assert result["client_id"] and result["client_secret"]
        assert result["client_secret_expires_at"] == 0
        # The new client can now authenticate.
        assert server.authenticate_client(result["client_id"], result["client_secret"]) is not None

    @pytest.mark.asyncio
    async def test_registration_disabled_raises(self) -> None:
        with pytest.raises(SecurityException) as exc:
            await _server(allow_dynamic_registration=False).register_client({"client_name": "x"})
        assert exc.value.code == "REGISTRATION_DISABLED"


class TestRegisterEndpoint:
    def _client(self, server: AuthorizationServer) -> TestClient:
        return TestClient(Starlette(routes=AuthorizationServerEndpoints(server).routes()))

    def test_open_registration_when_enabled(self) -> None:
        client = self._client(_server(allow_dynamic_registration=True))
        resp = client.post("/oauth2/register", json={"client_name": "app", "scope": "read"})
        assert resp.status_code == 201
        assert resp.json()["client_id"]

    def test_protected_registration_requires_initial_token(self) -> None:
        server = _server(allow_dynamic_registration=True, registration_access_token="secret-iat")
        client = self._client(server)
        # No / wrong initial access token -> 401.
        assert client.post("/oauth2/register", json={"client_name": "x"}).status_code == 401
        assert (
            client.post(
                "/oauth2/register", json={"client_name": "x"}, headers={"Authorization": "Bearer WRONG"}
            ).status_code
            == 401
        )
        # Correct token -> 201.
        ok = client.post("/oauth2/register", json={"client_name": "x"}, headers={"Authorization": "Bearer secret-iat"})
        assert ok.status_code == 201

    def test_registration_disabled_returns_error(self) -> None:
        client = self._client(_server(allow_dynamic_registration=False))
        resp = client.post("/oauth2/register", json={"client_name": "x"})
        assert resp.status_code in (400, 403)
