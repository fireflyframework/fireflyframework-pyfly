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
"""PyFly OAuth2 — Authorization Server, Resource Server, Client Registration, and Login."""

from pyfly.security.oauth2.authorization_server import (
    AuthorizationServer,
    InMemoryTokenStore,
    TokenStore,
)
from pyfly.security.oauth2.client import (
    ClientRegistration,
    ClientRegistrationRepository,
    InMemoryClientRegistrationRepository,
    github,
    google,
    keycloak,
)
from pyfly.security.oauth2.endpoints import AuthorizationServerEndpoints
from pyfly.security.oauth2.login import OAuth2LoginHandler
from pyfly.security.oauth2.properties import ResourceServerProperties
from pyfly.security.oauth2.resource_server import (
    ClaimMappings,
    JWKSTokenValidator,
    OpaqueTokenIntrospector,
    discover_oidc,
)
from pyfly.security.oauth2.session_security_filter import OAuth2SessionSecurityFilter

__all__ = [
    "AuthorizationServer",
    "AuthorizationServerEndpoints",
    "ClaimMappings",
    "ClientRegistration",
    "ClientRegistrationRepository",
    "InMemoryClientRegistrationRepository",
    "InMemoryTokenStore",
    "JWKSTokenValidator",
    "OAuth2LoginHandler",
    "OAuth2SessionSecurityFilter",
    "OpaqueTokenIntrospector",
    "ResourceServerProperties",
    "TokenStore",
    "discover_oidc",
    "github",
    "google",
    "keycloak",
]
