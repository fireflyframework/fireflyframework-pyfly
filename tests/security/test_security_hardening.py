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
"""Regression tests for security hardening (v26.06.12).

- JWTService requires an ``exp`` claim on decode and auto-adds one on encode
  (a token minted without ``exp`` would otherwise never expire).
- OAuth2 AuthorizationServer enforces the client's registered grant type — a
  client registered for ``authorization_code`` cannot mint ``client_credentials``
  tokens — and compares the client secret in constant time.
- HttpSecurity.build() warns when rules are configured without a terminal
  ``any_request()`` rule (unmatched paths fall through allowed).
"""

from __future__ import annotations

import logging
import time

import jwt
import pytest

from pyfly.kernel.exceptions import SecurityException
from pyfly.security.http_security import HttpSecurity
from pyfly.security.jwt import JWTService
from pyfly.security.oauth2.authorization_server import AuthorizationServer, InMemoryTokenStore
from pyfly.security.oauth2.client import ClientRegistration, InMemoryClientRegistrationRepository

_SECRET = "test-secret-key-minimum-32-chars!"


class TestJWTExpRequired:
    def test_encode_adds_exp_claim(self) -> None:
        svc = JWTService(secret=_SECRET)
        payload = svc.decode(svc.encode({"sub": "u1"}))
        assert "exp" in payload

    def test_decode_rejects_token_without_exp(self) -> None:
        svc = JWTService(secret=_SECRET)
        # Minted directly, bypassing encode()'s auto-exp — must be rejected.
        no_exp = jwt.encode({"sub": "u1"}, _SECRET, algorithm="HS256")
        with pytest.raises(SecurityException, match="Invalid token"):
            svc.decode(no_exp)

    def test_explicit_exp_is_preserved(self) -> None:
        svc = JWTService(secret=_SECRET)
        exp = int(time.time()) + 99
        payload = svc.decode(svc.encode({"sub": "u1", "exp": exp}))
        assert payload["exp"] == exp


def _server(grant_type: str) -> AuthorizationServer:
    reg = ClientRegistration(
        registration_id="c",
        client_id="c",
        client_secret="s3cr3t-value",
        authorization_grant_type=grant_type,
        scopes=["read"],
    )
    return AuthorizationServer(
        secret=_SECRET,
        client_repository=InMemoryClientRegistrationRepository(reg),
        token_store=InMemoryTokenStore(),
        issuer="https://auth.example.com",
    )


class TestOAuth2GrantTypeEnforcement:
    @pytest.mark.asyncio
    async def test_registered_client_can_use_client_credentials(self) -> None:
        result = await _server("client_credentials").token(
            grant_type="client_credentials", client_id="c", client_secret="s3cr3t-value"
        )
        assert "access_token" in result

    @pytest.mark.asyncio
    async def test_authorization_code_client_cannot_mint_client_credentials(self) -> None:
        with pytest.raises(SecurityException, match="not authorized for grant type"):
            await _server("authorization_code").token(
                grant_type="client_credentials", client_id="c", client_secret="s3cr3t-value"
            )

    @pytest.mark.asyncio
    async def test_wrong_secret_rejected(self) -> None:
        with pytest.raises(SecurityException, match="Invalid client credentials"):
            await _server("client_credentials").token(
                grant_type="client_credentials", client_id="c", client_secret="wrong-secret"
            )


class TestHttpSecurityTerminalWarning:
    def test_warns_without_any_request_rule(self, caplog: pytest.LogCaptureFixture) -> None:
        sec = HttpSecurity()
        sec.authorize_requests().request_matchers("/api/**").authenticated()
        with caplog.at_level(logging.WARNING, logger="pyfly.security.http_security"):
            sec.build()
        assert any("no terminal any_request" in r.getMessage() for r in caplog.records)

    def test_no_warning_with_any_request_terminal(self, caplog: pytest.LogCaptureFixture) -> None:
        sec = HttpSecurity()
        builder = sec.authorize_requests()
        builder.request_matchers("/api/**").authenticated()
        builder.any_request().deny_all()
        with caplog.at_level(logging.WARNING, logger="pyfly.security.http_security"):
            sec.build()
        assert not any("no terminal any_request" in r.getMessage() for r in caplog.records)
