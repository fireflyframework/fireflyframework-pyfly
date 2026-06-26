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
"""Pushed Authorization Requests (RFC 9126) + JWT-Secured Authz Requests (RFC 9101)."""

from __future__ import annotations

import base64
import hashlib
from urllib.parse import parse_qs, urlencode, urlparse

import jwt as pyjwt
import pytest
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.testclient import TestClient

from pyfly.security.context import SecurityContext
from pyfly.security.oauth2.authorization_server import AuthorizationServer, InMemoryTokenStore
from pyfly.security.oauth2.client import ClientRegistration, InMemoryClientRegistrationRepository
from pyfly.security.oauth2.endpoints import AuthorizationServerEndpoints

_SECRET = "authorization-server-secret-32bytes!!"
_CLIENT_SECRET = "web-secret-at-least-32-bytes-long!!!"
_CHALLENGE = base64.urlsafe_b64encode(hashlib.sha256(b"v" * 64).digest()).rstrip(b"=").decode("ascii")


def _endpoints() -> AuthorizationServerEndpoints:
    repo = InMemoryClientRegistrationRepository(
        ClientRegistration(
            registration_id="web",
            client_id="web",
            client_secret=_CLIENT_SECRET,
            authorization_grant_type="authorization_code",
            redirect_uri="https://app.example.com/cb",
            scopes=["openid", "read"],
        )
    )
    server = AuthorizationServer(
        secret=_SECRET, client_repository=repo, token_store=InMemoryTokenStore(), issuer="https://as"
    )
    return AuthorizationServerEndpoints(server)


def _authorize_request(query: dict[str, str], *, user: str = "alice") -> Request:
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/oauth2/authorize",
        "headers": [],
        "query_string": urlencode(query).encode(),
    }
    request = Request(scope)
    request.state.security_context = SecurityContext(user_id=user)
    return request


_AUTHZ_PARAMS = {
    "response_type": "code",
    "redirect_uri": "https://app.example.com/cb",
    "scope": "read",
    "state": "st-1",
    "code_challenge": _CHALLENGE,
    "code_challenge_method": "S256",
}


class TestPAR:
    def test_par_requires_client_auth(self) -> None:
        endpoints = _endpoints()
        client = TestClient(Starlette(routes=endpoints.routes()))
        resp = client.post("/oauth2/par", data={**_AUTHZ_PARAMS, "client_id": "web"})
        assert resp.status_code == 401  # no client secret

    @pytest.mark.asyncio
    async def test_par_then_authorize(self) -> None:
        endpoints = _endpoints()
        client = TestClient(Starlette(routes=endpoints.routes()))
        pushed = client.post("/oauth2/par", data={**_AUTHZ_PARAMS, "client_id": "web", "client_secret": _CLIENT_SECRET})
        assert pushed.status_code == 201
        request_uri = pushed.json()["request_uri"]
        assert request_uri.startswith("urn:ietf:params:oauth:request_uri:")

        resp = await endpoints._authorize(_authorize_request({"client_id": "web", "request_uri": request_uri}))
        assert resp.status_code == 302
        q = parse_qs(urlparse(resp.headers["location"]).query)
        assert q["code"] and q["state"] == ["st-1"]

    @pytest.mark.asyncio
    async def test_request_uri_is_single_use(self) -> None:
        endpoints = _endpoints()
        client = TestClient(Starlette(routes=endpoints.routes()))
        request_uri = client.post(
            "/oauth2/par", data={**_AUTHZ_PARAMS, "client_id": "web", "client_secret": _CLIENT_SECRET}
        ).json()["request_uri"]
        await endpoints._authorize(_authorize_request({"client_id": "web", "request_uri": request_uri}))
        # Second use is rejected.
        resp = await endpoints._authorize(_authorize_request({"client_id": "web", "request_uri": request_uri}))
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_unknown_request_uri_rejected(self) -> None:
        endpoints = _endpoints()
        resp = await endpoints._authorize(
            _authorize_request({"client_id": "web", "request_uri": "urn:ietf:params:oauth:request_uri:nope"})
        )
        assert resp.status_code == 400


class TestJAR:
    @pytest.mark.asyncio
    async def test_signed_request_object_accepted(self) -> None:
        endpoints = _endpoints()
        request_jwt = pyjwt.encode({**_AUTHZ_PARAMS, "client_id": "web"}, _CLIENT_SECRET, algorithm="HS256")
        resp = await endpoints._authorize(_authorize_request({"client_id": "web", "request": request_jwt}))
        assert resp.status_code == 302
        q = parse_qs(urlparse(resp.headers["location"]).query)
        assert q["code"]

    @pytest.mark.asyncio
    async def test_tampered_request_object_rejected(self) -> None:
        endpoints = _endpoints()
        request_jwt = pyjwt.encode(
            {**_AUTHZ_PARAMS, "client_id": "web"}, "WRONG-KEY-32-bytes-or-more-here!!", algorithm="HS256"
        )
        resp = await endpoints._authorize(_authorize_request({"client_id": "web", "request": request_jwt}))
        assert resp.status_code == 400
        assert b"invalid_request_object" in bytes(resp.body)
