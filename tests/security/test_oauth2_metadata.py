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
"""Authorization-server metadata (RFC 8414) + OIDC discovery."""

from __future__ import annotations

from starlette.applications import Starlette
from starlette.testclient import TestClient

from pyfly.security.oauth2.authorization_server import AuthorizationServer, InMemoryTokenStore
from pyfly.security.oauth2.client import InMemoryClientRegistrationRepository
from pyfly.security.oauth2.endpoints import AuthorizationServerEndpoints

_SECRET = "authorization-server-secret-32bytes!!"


def _client() -> TestClient:
    server = AuthorizationServer(
        secret=_SECRET,
        client_repository=InMemoryClientRegistrationRepository(),
        token_store=InMemoryTokenStore(),
        issuer="https://as.example.com",
    )
    return TestClient(Starlette(routes=AuthorizationServerEndpoints(server).routes()))


class TestAuthorizationServerMetadata:
    def test_oauth_metadata_document(self) -> None:
        doc = _client().get("/.well-known/oauth-authorization-server").json()
        assert doc["issuer"] == "https://as.example.com"
        assert doc["token_endpoint"].endswith("/oauth2/token")
        assert doc["authorization_endpoint"].endswith("/oauth2/authorize")
        assert doc["jwks_uri"].endswith("/oauth2/jwks")
        assert doc["introspection_endpoint"].endswith("/oauth2/introspect")
        assert doc["revocation_endpoint"].endswith("/oauth2/revoke")
        assert doc["registration_endpoint"].endswith("/oauth2/register")
        assert doc["code_challenge_methods_supported"] == ["S256"]
        assert "authorization_code" in doc["grant_types_supported"]
        assert doc["response_types_supported"] == ["code"]

    def test_openid_configuration_document(self) -> None:
        doc = _client().get("/.well-known/openid-configuration").json()
        assert doc["issuer"] == "https://as.example.com"
        assert doc["subject_types_supported"] == ["public"]
        assert "HS256" in doc["id_token_signing_alg_values_supported"]
        assert "sub" in doc["claims_supported"]
