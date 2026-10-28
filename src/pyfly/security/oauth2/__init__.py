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

from typing import TYPE_CHECKING, Any

from pyfly.security.oauth2.authorization_server import (
    AtomicTokenStore,
    AuthorizationServer,
    GrantOutcome,
    InMemoryTokenStore,
    KeyValueTokenStore,
    TokenRecord,
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
from pyfly.security.oauth2.jwks import AsyncJWKSFetcher, JWKSFetcher
from pyfly.security.oauth2.ports import (
    OAuth2AuthenticationSuccessHandler,
    OAuth2AuthorizationRequestCustomizer,
    OidcTokenValidatorFactory,
)
from pyfly.security.oauth2.properties import ResourceServerProperties
from pyfly.security.oauth2.resource_server import (
    ClaimMappings,
    JWKSTokenValidator,
    OpaqueTokenIntrospector,
    discover_oidc,
)
from pyfly.security.oauth2.session_security_filter import OAuth2SessionSecurityFilter

if TYPE_CHECKING:
    from pyfly.security.oauth2.endpoints import AuthorizationServerEndpoints
    from pyfly.security.oauth2.login import OAuth2LoginHandler

_HTTP_EXPORTS = {
    "AuthorizationServerEndpoints": ("pyfly.security.oauth2.endpoints", "AuthorizationServerEndpoints"),
    "OAuth2LoginHandler": ("pyfly.security.oauth2.login", "OAuth2LoginHandler"),
}


def __getattr__(name: str) -> Any:
    from importlib import import_module

    if name in _HTTP_EXPORTS:
        module, symbol = _HTTP_EXPORTS[name]
        value = getattr(import_module(module), symbol)
        globals()[name] = value
        return value
    raise AttributeError(name)


__all__ = [
    "AsyncJWKSFetcher",
    "AtomicTokenStore",
    "AuthorizationServer",
    "AuthorizationServerEndpoints",
    "ClaimMappings",
    "ClientRegistration",
    "ClientRegistrationRepository",
    "GrantOutcome",
    "InMemoryClientRegistrationRepository",
    "InMemoryTokenStore",
    "JWKSFetcher",
    "JWKSTokenValidator",
    "KeyValueTokenStore",
    "OAuth2LoginHandler",
    "OAuth2AuthenticationSuccessHandler",
    "OAuth2AuthorizationRequestCustomizer",
    "OidcTokenValidatorFactory",
    "OAuth2SessionSecurityFilter",
    "OpaqueTokenIntrospector",
    "ResourceServerProperties",
    "TokenRecord",
    "TokenStore",
    "discover_oidc",
    "github",
    "google",
    "keycloak",
]
