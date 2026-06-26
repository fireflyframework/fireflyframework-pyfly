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
"""/oauth2/authorize endpoint (resource-owner gate + redirect)."""

from __future__ import annotations

import base64
import hashlib
from urllib.parse import parse_qs, urlencode, urlparse

import pytest
from starlette.requests import Request

from pyfly.security.context import SecurityContext
from pyfly.security.oauth2.authorization_server import AuthorizationServer, InMemoryTokenStore
from pyfly.security.oauth2.client import ClientRegistration, InMemoryClientRegistrationRepository
from pyfly.security.oauth2.endpoints import AuthorizationServerEndpoints

_SECRET = "authorization-server-secret-32bytes!!"
_CHALLENGE = base64.urlsafe_b64encode(hashlib.sha256(b"v" * 64).digest()).rstrip(b"=").decode("ascii")


def _endpoints() -> AuthorizationServerEndpoints:
    repo = InMemoryClientRegistrationRepository(
        ClientRegistration(
            registration_id="web",
            client_id="web",
            client_secret="web-secret",
            authorization_grant_type="authorization_code",
            redirect_uri="https://app.example.com/cb",
            scopes=["openid", "read"],
        )
    )
    server = AuthorizationServer(
        secret=_SECRET, client_repository=repo, token_store=InMemoryTokenStore(), issuer="https://as"
    )
    return AuthorizationServerEndpoints(server)


def _authorize_request(query: dict[str, str], *, user: str | None) -> Request:
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/oauth2/authorize",
        "headers": [],
        "query_string": urlencode(query).encode(),
    }
    request = Request(scope)
    request.state.security_context = SecurityContext(user_id=user) if user else SecurityContext.anonymous()
    return request


def _base_query(**over: str) -> dict[str, str]:
    q = {
        "response_type": "code",
        "client_id": "web",
        "redirect_uri": "https://app.example.com/cb",
        "scope": "openid read",
        "state": "st-1",
        "code_challenge": _CHALLENGE,
        "code_challenge_method": "S256",
    }
    q.update(over)
    return q


class TestAuthorizeEndpoint:
    @pytest.mark.asyncio
    async def test_authenticated_user_gets_code_redirect(self) -> None:
        resp = await _endpoints()._authorize(_authorize_request(_base_query(), user="alice"))
        assert resp.status_code == 302
        loc = urlparse(resp.headers["location"])
        assert f"{loc.scheme}://{loc.netloc}{loc.path}" == "https://app.example.com/cb"
        q = parse_qs(loc.query)
        assert q["code"] and q["state"] == ["st-1"] and q["iss"] == ["https://as"]

    @pytest.mark.asyncio
    async def test_anonymous_user_redirected_to_login(self) -> None:
        resp = await _endpoints()._authorize(_authorize_request(_base_query(), user=None))
        assert resp.status_code == 302
        assert resp.headers["location"].startswith("/login?")
        assert "next=" in resp.headers["location"]

    @pytest.mark.asyncio
    async def test_bad_redirect_uri_is_not_redirected(self) -> None:
        req = _authorize_request(_base_query(redirect_uri="https://evil.example.com/cb"), user="alice")
        resp = await _endpoints()._authorize(req)
        assert resp.status_code == 400
        assert b"invalid_redirect_uri" in bytes(resp.body)

    @pytest.mark.asyncio
    async def test_invalid_scope_redirects_error_to_client(self) -> None:
        req = _authorize_request(_base_query(scope="openid admin"), user="alice")
        resp = await _endpoints()._authorize(req)
        assert resp.status_code == 302
        q = parse_qs(urlparse(resp.headers["location"]).query)
        assert q["error"] == ["invalid_scope"] and q["state"] == ["st-1"]

    @pytest.mark.asyncio
    async def test_missing_pkce_redirects_invalid_request(self) -> None:
        req = _authorize_request(_base_query(code_challenge=""), user="alice")
        resp = await _endpoints()._authorize(req)
        assert resp.status_code == 302
        q = parse_qs(urlparse(resp.headers["location"]).query)
        assert q["error"] == ["invalid_request"]

    @pytest.mark.asyncio
    async def test_end_to_end_code_is_exchangeable(self) -> None:
        endpoints = _endpoints()
        resp = await endpoints._authorize(_authorize_request(_base_query(scope="read"), user="alice"))
        code = parse_qs(urlparse(resp.headers["location"]).query)["code"][0]
        result = await endpoints._server.token(
            grant_type="authorization_code",
            client_id="web",
            client_secret="web-secret",
            code=code,
            redirect_uri="https://app.example.com/cb",
            code_verifier="v" * 64,
        )
        assert "access_token" in result
