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
"""Typed configuration for the OAuth2 resource server.

Bound from ``pyfly.security.oauth2.resource-server.*`` (Spring-Boot-style relaxed
binding: kebab-case YAML → snake_case fields, ``${...}`` + ``PYFLY_*`` env
overrides). Multi-value fields are comma-separated strings (or YAML lists) parsed
by the ``*_list`` helpers — the same convention as other PyFly filter
``exclude-patterns``.

YAML structure::

    pyfly:
      security:
        oauth2:
          resource-server:
            enabled: true
            # Provide a JWKS URI directly, OR an issuer-uri for OIDC discovery.
            jwks-uri: "https://login.microsoftonline.com/<tid>/discovery/v2.0/keys"
            issuer-uri: "https://login.microsoftonline.com/<tid>/v2.0"   # OIDC discovery
            issuer: "https://login.microsoftonline.com/<tid>/v2.0"       # explicit iss
            audiences: "api://cdm-backend,cdm-api"
            validate-audience: true        # set false for Cognito access tokens (no aud)
            algorithms: "RS256"
            clock-skew-seconds: 60
            jwks-timeout-seconds: 30
            jwks-cache-seconds: 300
            # Config-driven claim mapping (dotted paths, '*' wildcard):
            principal-claim-names: "oid,sub"
            authorities-claim-names: "roles,realm_access.roles,resource_access.*.roles,groups,cognito:groups"
            authority-prefix: ""
            scope-claim-names: "scp,scope"
            attribute-claims: "tid,preferred_username"
            exclude-patterns: "/actuator/**,/api/v1/version"
            authenticate-error-mode: "anonymous"   # or "401"
"""

from __future__ import annotations

from dataclasses import dataclass

from pyfly.core.config import config_properties


def _csv(value: str) -> list[str]:
    """Split a comma-separated config string into a trimmed, non-empty list."""
    return [item.strip() for item in value.split(",") if item.strip()]


@config_properties(prefix="pyfly.security.oauth2.resource-server")
@dataclass
class ResourceServerProperties:
    """``pyfly.security.oauth2.resource-server.*`` — OAuth2 resource server."""

    enabled: bool = False

    # --- key source -------------------------------------------------------
    jwks_uri: str = ""
    # OIDC discovery: when set and jwks-uri is empty, the framework fetches
    # ``<issuer-uri>/.well-known/openid-configuration`` to learn jwks-uri + issuer.
    issuer_uri: str = ""
    issuer: str = ""

    # --- audience ---------------------------------------------------------
    audiences: str = ""
    validate_audience: bool = True

    # --- signature / time -------------------------------------------------
    algorithms: str = "RS256"
    clock_skew_seconds: int = 60
    jwks_timeout_seconds: int = 30
    jwks_cache_seconds: int = 300

    # --- claim mapping ----------------------------------------------------
    principal_claim_names: str = "oid,sub"
    authorities_claim_names: str = (
        "roles,scopes,authorities,realm_access.roles,resource_access.*.roles,groups,cognito:groups"
    )
    authority_prefix: str = ""
    scope_claim_names: str = "scp,scope"
    attribute_claims: str = ""

    # --- filter -----------------------------------------------------------
    exclude_patterns: str = ""
    # "anonymous" (default, non-breaking): an invalid/missing token yields an
    # anonymous context and the request proceeds — the HttpSecurity gate decides.
    # "401": a *present but invalid* token is rejected at the filter with a 401 +
    # ``WWW-Authenticate: Bearer error="invalid_token"`` (RFC 6750). A missing
    # token still falls through to the gate.
    authenticate_error_mode: str = "anonymous"

    # --- parsed-list accessors -------------------------------------------
    def audience_list(self) -> list[str]:
        return _csv(self.audiences)

    def algorithm_list(self) -> list[str]:
        return _csv(self.algorithms) or ["RS256"]

    def principal_claim_list(self) -> list[str]:
        return _csv(self.principal_claim_names) or ["oid", "sub"]

    def authorities_claim_list(self) -> list[str]:
        return _csv(self.authorities_claim_names)

    def scope_claim_list(self) -> list[str]:
        return _csv(self.scope_claim_names)

    def attribute_claim_list(self) -> list[str]:
        return _csv(self.attribute_claims)

    def exclude_pattern_list(self) -> list[str]:
        return _csv(self.exclude_patterns)
