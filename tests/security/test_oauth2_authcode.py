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
"""OAuth2 authorization_code grant (PKCE, single-use codes, OIDC id_token)."""

from __future__ import annotations

import base64
import hashlib

import jwt as pyjwt
import pytest

from pyfly.kernel.exceptions import SecurityException
from pyfly.security.oauth2.authorization_server import AuthorizationServer, InMemoryTokenStore
from pyfly.security.oauth2.client import ClientRegistration, InMemoryClientRegistrationRepository

_SECRET = "authorization-server-secret-32bytes!!"


def _s256(verifier: str) -> str:
    return base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).rstrip(b"=").decode("ascii")


def _repo(*, public: bool = False) -> InMemoryClientRegistrationRepository:
    return InMemoryClientRegistrationRepository(
        ClientRegistration(
            registration_id="web",
            client_id="web",
            client_secret="" if public else "web-secret",
            authorization_grant_type="authorization_code",
            redirect_uri="https://app.example.com/cb",
            scopes=["openid", "profile", "read"],
        )
    )


def _server(*, public: bool = False) -> AuthorizationServer:
    return AuthorizationServer(
        secret=_SECRET,
        client_repository=_repo(public=public),
        token_store=InMemoryTokenStore(),
        issuer="https://as.example.com",
    )


async def _authorize(
    server: AuthorizationServer, *, challenge: str, scope: str = "openid read", **over: object
) -> dict:
    kwargs: dict = dict(
        client_id="web",
        redirect_uri="https://app.example.com/cb",
        response_type="code",
        scope=scope,
        state="xyz",
        code_challenge=challenge,
        code_challenge_method="S256",
        user_id="user-1",
        nonce="n-123",
    )
    kwargs.update(over)
    return await server.authorize(**kwargs)


class TestAuthorize:
    @pytest.mark.asyncio
    async def test_issues_code_with_state_and_iss(self) -> None:
        result = await _authorize(_server(), challenge=_s256("v" * 64))
        assert result["code"]
        assert result["state"] == "xyz"
        assert result["iss"] == "https://as.example.com"  # RFC 9207

    @pytest.mark.asyncio
    async def test_redirect_uri_must_match_exactly(self) -> None:
        with pytest.raises(SecurityException) as exc:
            await _authorize(_server(), challenge=_s256("v" * 64), redirect_uri="https://app.example.com/evil")
        assert exc.value.code == "INVALID_REDIRECT_URI"

    @pytest.mark.asyncio
    async def test_pkce_is_required(self) -> None:
        with pytest.raises(SecurityException) as exc:
            await _authorize(_server(), challenge="")
        assert exc.value.code == "INVALID_REQUEST"

    @pytest.mark.asyncio
    async def test_plain_pkce_method_rejected(self) -> None:
        with pytest.raises(SecurityException) as exc:
            await _authorize(_server(), challenge=_s256("v" * 64), code_challenge_method="plain")
        assert exc.value.code == "INVALID_REQUEST"

    @pytest.mark.asyncio
    async def test_scope_must_be_subset(self) -> None:
        with pytest.raises(SecurityException) as exc:
            await _authorize(_server(), challenge=_s256("v" * 64), scope="openid admin")
        assert exc.value.code == "INVALID_SCOPE"

    @pytest.mark.asyncio
    async def test_unsupported_response_type(self) -> None:
        with pytest.raises(SecurityException) as exc:
            await _authorize(_server(), challenge=_s256("v" * 64), response_type="token")
        assert exc.value.code == "UNSUPPORTED_RESPONSE_TYPE"


class TestCodeExchange:
    @pytest.mark.asyncio
    async def test_exchange_mints_tokens_and_id_token(self) -> None:
        server = _server()
        verifier = "verifier-" + "v" * 56
        issued = await _authorize(server, challenge=_s256(verifier), scope="openid read")
        result = await server.token(
            grant_type="authorization_code",
            client_id="web",
            client_secret="web-secret",
            code=issued["code"],
            redirect_uri="https://app.example.com/cb",
            code_verifier=verifier,
        )
        assert "access_token" in result and "refresh_token" in result
        access = pyjwt.decode(result["access_token"], _SECRET, algorithms=["HS256"], options={"verify_aud": False})
        assert access["sub"] == "user-1"
        assert "read" in access["scope"]
        # OIDC id_token present for the openid scope.
        idt = pyjwt.decode(result["id_token"], _SECRET, algorithms=["HS256"], audience="web")
        assert idt["sub"] == "user-1" and idt["aud"] == "web" and idt["nonce"] == "n-123"

    @pytest.mark.asyncio
    async def test_wrong_verifier_rejected(self) -> None:
        server = _server()
        issued = await _authorize(server, challenge=_s256("v" * 64))
        with pytest.raises(SecurityException) as exc:
            await server.token(
                grant_type="authorization_code",
                client_id="web",
                client_secret="web-secret",
                code=issued["code"],
                redirect_uri="https://app.example.com/cb",
                code_verifier="wrong-verifier",
            )
        assert exc.value.code == "INVALID_GRANT"

    @pytest.mark.asyncio
    async def test_redirect_uri_mismatch_on_exchange_rejected(self) -> None:
        server = _server()
        verifier = "v" * 64
        issued = await _authorize(server, challenge=_s256(verifier))
        with pytest.raises(SecurityException) as exc:
            await server.token(
                grant_type="authorization_code",
                client_id="web",
                client_secret="web-secret",
                code=issued["code"],
                redirect_uri="https://app.example.com/other",
                code_verifier=verifier,
            )
        assert exc.value.code == "INVALID_GRANT"

    @pytest.mark.asyncio
    async def test_code_is_single_use_and_reuse_revokes_tokens(self) -> None:
        server = _server()
        verifier = "v" * 64
        issued = await _authorize(server, challenge=_s256(verifier))
        first = await server.token(
            grant_type="authorization_code",
            client_id="web",
            client_secret="web-secret",
            code=issued["code"],
            redirect_uri="https://app.example.com/cb",
            code_verifier=verifier,
        )
        # Replaying the code fails...
        with pytest.raises(SecurityException) as exc:
            await server.token(
                grant_type="authorization_code",
                client_id="web",
                client_secret="web-secret",
                code=issued["code"],
                redirect_uri="https://app.example.com/cb",
                code_verifier=verifier,
            )
        assert exc.value.code == "INVALID_GRANT"
        # ...and the refresh token issued from the first exchange is revoked.
        with pytest.raises(SecurityException):
            await server.token(
                grant_type="refresh_token",
                client_id="web",
                client_secret="web-secret",
                refresh_token=first["refresh_token"],
            )

    @pytest.mark.asyncio
    async def test_public_client_uses_pkce_without_secret(self) -> None:
        server = _server(public=True)
        verifier = "v" * 64
        issued = await _authorize(server, challenge=_s256(verifier), scope="read")
        result = await server.token(
            grant_type="authorization_code",
            client_id="web",
            client_secret="",
            code=issued["code"],
            redirect_uri="https://app.example.com/cb",
            code_verifier=verifier,
        )
        assert "access_token" in result

    @pytest.mark.asyncio
    async def test_public_client_cannot_use_client_credentials(self) -> None:
        server = _server(public=True)
        with pytest.raises(SecurityException) as exc:
            await server.token(grant_type="client_credentials", client_id="web", client_secret="")
        assert exc.value.code == "INVALID_CLIENT"
