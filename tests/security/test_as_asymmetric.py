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
"""Asymmetric (RS256) authorization-server signing + JWKS publication."""

from __future__ import annotations

import jwt as pyjwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from pyfly.security.oauth2.authorization_server import AuthorizationServer, InMemoryTokenStore
from pyfly.security.oauth2.client import ClientRegistration, InMemoryClientRegistrationRepository


def _rsa_pem() -> str:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode("utf-8")


def _repo() -> InMemoryClientRegistrationRepository:
    return InMemoryClientRegistrationRepository(
        ClientRegistration(
            registration_id="c",
            client_id="c",
            client_secret="s3cr3t-value",
            authorization_grant_type="client_credentials",
            scopes=["read"],
        )
    )


def _as_rs256() -> AuthorizationServer:
    return AuthorizationServer(
        secret="",
        client_repository=_repo(),
        token_store=InMemoryTokenStore(),
        algorithm="RS256",
        private_key=_rsa_pem(),
        key_id="k1",
        issuer="https://as.example.com",
    )


class TestAsymmetricSigning:
    @pytest.mark.asyncio
    async def test_token_verifies_against_published_jwks(self) -> None:
        server = _as_rs256()
        result = await server.token(grant_type="client_credentials", client_id="c", client_secret="s3cr3t-value")

        jwks = server.jwks()
        assert len(jwks["keys"]) == 1
        key = pyjwt.PyJWK.from_dict(jwks["keys"][0]).key
        payload = pyjwt.decode(result["access_token"], key, algorithms=["RS256"], issuer="https://as.example.com")
        assert payload["sub"] == "c"
        assert payload["scope"] == "read"

    @pytest.mark.asyncio
    async def test_token_header_carries_kid(self) -> None:
        server = _as_rs256()
        result = await server.token(grant_type="client_credentials", client_id="c", client_secret="s3cr3t-value")
        header = pyjwt.get_unverified_header(result["access_token"])
        assert header["kid"] == "k1"
        assert header["alg"] == "RS256"

    def test_jwks_entry_has_kid_use_alg(self) -> None:
        jwk = _as_rs256().jwks()["keys"][0]
        assert jwk["kid"] == "k1"
        assert jwk["use"] == "sig"
        assert jwk["alg"] == "RS256"
        assert jwk["kty"] == "RSA"

    def test_hs256_jwks_is_empty(self) -> None:
        server = AuthorizationServer(
            secret="symmetric-secret-key-at-least-32b!!",
            client_repository=_repo(),
            token_store=InMemoryTokenStore(),
        )
        assert server.jwks() == {"keys": []}
