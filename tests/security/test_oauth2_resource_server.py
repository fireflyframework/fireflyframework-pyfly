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
"""OAuth2 resource-server JWKS validation — hermetic, multi-IdP.

These tests run a **real** JWKS endpoint over localhost HTTP and mint **real**
RS256 tokens shaped like Keycloak, Microsoft Entra ID (v2.0) and AWS Cognito —
no mocks of PyJWKClient. They pin the full validation contract (signature, iss,
aud, exp with clock-skew leeway), config-driven multi-IdP claim mapping, JWKS key
rotation, and OIDC discovery.
"""

from __future__ import annotations

import http.server
import json
import threading
import time
from collections.abc import Callable, Iterator
from typing import Any

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from pyfly.kernel.exceptions import SecurityException
from pyfly.security.context import SecurityContext
from pyfly.security.oauth2.resource_server import (
    ClaimMappings,
    JWKSTokenValidator,
    _flatten_strs,
    _resolve_claim_path,
    discover_oidc,
)

# ---------------------------------------------------------------------------
# Keys + a real localhost JWKS server
# ---------------------------------------------------------------------------
KEY1 = rsa.generate_private_key(public_exponent=65537, key_size=2048)
KEY2 = rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _jwk(pubkey: Any, kid: str) -> dict[str, Any]:
    data = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(pubkey))
    data.update({"kid": kid, "use": "sig", "alg": "RS256"})
    return data


class _JwksState:
    """Mutable JWKS document served by the localhost endpoint (supports rotation)."""

    def __init__(self) -> None:
        self.keys = [_jwk(KEY1.public_key(), "k1")]
        self.issuer = ""  # set by the fixture once the port is known

    def document(self) -> dict[str, Any]:
        return {"keys": self.keys}

    def discovery(self) -> dict[str, Any]:
        return {"issuer": self.issuer, "jwks_uri": f"{self.issuer}/jwks"}


@pytest.fixture()
def jwks() -> Iterator[tuple[str, str, _JwksState]]:
    """Yield ``(jwks_uri, issuer, state)`` for a live localhost JWKS server."""
    state = _JwksState()

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            is_discovery = self.path.endswith("/.well-known/openid-configuration")
            payload = state.discovery() if is_discovery else state.document()
            body = json.dumps(payload).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args: Any) -> None:  # silence test server
            pass

    httpd = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    port = httpd.server_address[1]
    state.issuer = f"http://127.0.0.1:{port}"
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        yield f"{state.issuer}/jwks", state.issuer, state
    finally:
        httpd.shutdown()


def _mint(payload: dict[str, Any], *, key: Any = KEY1, kid: str = "k1") -> str:
    body = {"iat": int(time.time()), "exp": int(time.time()) + 3600, **payload}
    return jwt.encode(body, key, algorithm="RS256", headers={"kid": kid})


Mint = Callable[..., str]


# ---------------------------------------------------------------------------
# Claim-path resolver (pure unit)
# ---------------------------------------------------------------------------
class TestClaimPathResolver:
    def test_dotted_and_wildcard_and_colon(self) -> None:
        payload = {
            "roles": "flat",
            "realm_access": {"roles": ["a", "b"]},
            "resource_access": {"c1": {"roles": ["x"]}, "c2": {"roles": ["y"]}},
            "cognito:groups": ["g1", "g2"],
        }
        assert _flatten_strs(_resolve_claim_path(payload, "roles")) == ["flat"]
        assert _flatten_strs(_resolve_claim_path(payload, "realm_access.roles")) == ["a", "b"]
        assert _flatten_strs(_resolve_claim_path(payload, "resource_access.*.roles")) == ["x", "y"]
        assert _flatten_strs(_resolve_claim_path(payload, "cognito:groups")) == ["g1", "g2"]
        assert _resolve_claim_path(payload, "missing.path") == []


# ---------------------------------------------------------------------------
# Multi-IdP token shapes
# ---------------------------------------------------------------------------
class TestKeycloak:
    def test_realm_and_resource_roles_and_scope(self, jwks: tuple[str, str, _JwksState]) -> None:
        jwks_uri, _, _ = jwks
        iss = "https://kc.example.com/realms/cdm"
        v = JWKSTokenValidator(jwks_uri=jwks_uri, issuer=iss, audiences=["cdm-api"])
        token = _mint(
            {
                "iss": iss,
                "aud": "cdm-api",
                "sub": "kc-user",
                "realm_access": {"roles": ["CdM.Gd", "offline_access"]},
                "resource_access": {"cdm-api": {"roles": ["client-role-x"]}},
                "scope": "openid profile",
            }
        )
        ctx = v.to_security_context(token)
        assert ctx.user_id == "kc-user"
        # Both realm AND per-client (resource_access) roles are extracted.
        assert "CdM.Gd" in ctx.roles
        assert "client-role-x" in ctx.roles
        assert ctx.permissions == ["openid", "profile"]


class TestEntraID:
    def test_roles_groups_scp_and_attributes(self, jwks: tuple[str, str, _JwksState]) -> None:
        jwks_uri, _, _ = jwks
        tid = "11111111-2222-3333-4444-555555555555"
        iss = f"https://login.microsoftonline.com/{tid}/v2.0"
        mappings = ClaimMappings(attribute_claims=("tid", "preferred_username"))
        v = JWKSTokenValidator(jwks_uri=jwks_uri, issuer=iss, audiences=["api://cdm-backend"], claim_mappings=mappings)
        token = _mint(
            {
                "iss": iss,
                "aud": "api://cdm-backend",
                "sub": "entra-sub",
                "oid": "oid-abc",
                "tid": tid,
                "roles": ["CdM.Gn"],
                "groups": ["group-guid-1"],
                "scp": "Data.Read Data.Write",
                "preferred_username": "ana@faes.mx",
            }
        )
        ctx = v.to_security_context(token)
        # oid is the default principal preference over sub.
        assert ctx.user_id == "oid-abc"
        assert "CdM.Gn" in ctx.roles  # app roles
        assert "group-guid-1" in ctx.roles  # groups merged into authorities
        assert ctx.permissions == ["Data.Read", "Data.Write"]  # scp -> permissions
        assert ctx.attributes["tid"] == tid
        assert ctx.attributes["preferred_username"] == "ana@faes.mx"


class TestCdMMexicoUseCase:
    """cdm-mexico (FAES México) Entra ID resource-server contract.

    Proves the use case is covered by **pure configuration** — the framework now
    reproduces what cdm's ``EntraClaimsValidator`` subclass did (roles + groups,
    ``scp`` scopes, ``oid`` principal, ``tid``/``cdm_entidad_id`` attributes), so
    an adopter can either configure claim mapping or still subclass.
    """

    def test_entra_token_maps_like_entra_claims_validator(self, jwks: tuple[str, str, _JwksState]) -> None:
        jwks_uri, _, _ = jwks
        tid = "11111111-2222-3333-4444-555555555555"
        iss = f"https://login.microsoftonline.com/{tid}/v2.0"
        # The cdm-mexico claim mapping, expressed as config (no subclass needed).
        mappings = ClaimMappings(
            principal_claims=("oid", "sub"),
            authority_claims=("roles", "groups"),  # cdm appends groups to roles
            scope_claims=("scp",),
            attribute_claims=("tid", "preferred_username", "cdm_entidad_id", "employeeid", "oid"),
        )
        v = JWKSTokenValidator(jwks_uri=jwks_uri, issuer=iss, audiences=["api://cdm-backend"], claim_mappings=mappings)
        token = _mint(
            {
                "iss": iss,
                "aud": "api://cdm-backend",
                "sub": "entra-sub",
                "oid": "oid-stable",
                "tid": tid,
                "roles": ["CdM.Gn"],
                "groups": ["grp-guid-1"],
                "scp": "Cdm.Read",
                "preferred_username": "director@faes.mx",
                "cdm_entidad_id": "MX0000064",
            }
        )
        ctx = v.to_security_context(token)

        # Principal prefers the stable Entra object id.
        assert ctx.user_id == "oid-stable"
        # Raw role claim is preserved verbatim, and the admin gate's exact-match
        # check (cdm checks the raw "CdM.Gn") works.
        assert ctx.has_role("CdM.Gn")
        assert "grp-guid-1" in ctx.roles  # group object-ids drive role mapping too
        # Entra delegated scopes (scp) become permissions.
        assert ctx.permissions == ["Cdm.Read"]
        # Row-scope attributes are carried through.
        assert ctx.attributes["cdm_entidad_id"] == "MX0000064"
        assert ctx.attributes["tid"] == tid
        assert ctx.attributes["preferred_username"] == "director@faes.mx"

    def test_gn_admin_gate_denies_non_gn_principal(self, jwks: tuple[str, str, _JwksState]) -> None:
        jwks_uri, _, _ = jwks
        iss = "https://login.microsoftonline.com/tid/v2.0"
        v = JWKSTokenValidator(
            jwks_uri=jwks_uri,
            issuer=iss,
            audiences=["api://cdm-backend"],
            claim_mappings=ClaimMappings(authority_claims=("roles",)),
        )
        token = _mint({"iss": iss, "aud": "api://cdm-backend", "sub": "rep", "roles": ["CdM.Rep"]})
        ctx = v.to_security_context(token)
        # The admin URL gate / @pre_authorize checks the raw "CdM.Gn"; a rep must fail it.
        assert ctx.has_role("CdM.Rep")
        assert not ctx.has_role("CdM.Gn")


class TestCognito:
    def test_access_token_no_audience(self, jwks: tuple[str, str, _JwksState]) -> None:
        jwks_uri, _, _ = jwks
        iss = "https://cognito-idp.us-east-1.amazonaws.com/us-east-1_AbCdEf"
        # Cognito access tokens carry no 'aud': validate without configuring audiences.
        v = JWKSTokenValidator(jwks_uri=jwks_uri, issuer=iss)
        token = _mint(
            {
                "iss": iss,
                "sub": "cog-sub",
                "client_id": "cog-client",
                "token_use": "access",
                "cognito:groups": ["CdM.Gr"],
                "scope": "aws.cognito.signin.user.admin",
            }
        )
        ctx = v.to_security_context(token)
        assert ctx.user_id == "cog-sub"
        assert "CdM.Gr" in ctx.roles  # cognito:groups extracted

    def test_audience_required_rejects_aud_less_token(self, jwks: tuple[str, str, _JwksState]) -> None:
        jwks_uri, _, _ = jwks
        iss = "https://cognito-idp.us-east-1.amazonaws.com/us-east-1_AbCdEf"
        # Configuring audiences makes the aud-less access token fail — the
        # documented Cognito gotcha. validate_audience=False is the escape hatch.
        v = JWKSTokenValidator(jwks_uri=jwks_uri, issuer=iss, audiences=["cog-client"])
        token = _mint({"iss": iss, "sub": "cog-sub", "token_use": "access"})
        with pytest.raises(SecurityException):
            v.validate(token)

        lenient = JWKSTokenValidator(jwks_uri=jwks_uri, issuer=iss, audiences=["cog-client"], validate_audience=False)
        assert lenient.validate(token)["sub"] == "cog-sub"


# ---------------------------------------------------------------------------
# Audience handling
# ---------------------------------------------------------------------------
class TestAudience:
    def test_audiences_list_matches_any(self, jwks: tuple[str, str, _JwksState]) -> None:
        jwks_uri, _, _ = jwks
        v = JWKSTokenValidator(jwks_uri=jwks_uri, audiences=["a", "b", "c"])
        assert v.validate(_mint({"sub": "u", "aud": "b"}))["sub"] == "u"
        with pytest.raises(SecurityException):
            v.validate(_mint({"sub": "u", "aud": "z"}))

    def test_no_audiences_skips_aud_check(self, jwks: tuple[str, str, _JwksState]) -> None:
        jwks_uri, _, _ = jwks
        v = JWKSTokenValidator(jwks_uri=jwks_uri)
        # A token WITH an aud still passes when no audiences are configured.
        assert v.validate(_mint({"sub": "u", "aud": "whatever"}))["sub"] == "u"


# ---------------------------------------------------------------------------
# Clock-skew leeway
# ---------------------------------------------------------------------------
class TestClockSkew:
    def test_default_leeway_accepts_small_future_skew(self, jwks: tuple[str, str, _JwksState]) -> None:
        jwks_uri, _, _ = jwks
        v = JWKSTokenValidator(jwks_uri=jwks_uri)  # default leeway = 60s
        future = int(time.time()) + 30
        token = jwt.encode(
            {"sub": "u", "iat": future, "nbf": future, "exp": future + 3600},
            KEY1,
            algorithm="RS256",
            headers={"kid": "k1"},
        )
        assert v.validate(token)["sub"] == "u"

    def test_zero_leeway_rejects_future_skew(self, jwks: tuple[str, str, _JwksState]) -> None:
        jwks_uri, _, _ = jwks
        v = JWKSTokenValidator(jwks_uri=jwks_uri, leeway=0)
        future = int(time.time()) + 30
        token = jwt.encode(
            {"sub": "u", "iat": future, "nbf": future, "exp": future + 3600},
            KEY1,
            algorithm="RS256",
            headers={"kid": "k1"},
        )
        with pytest.raises(SecurityException):
            v.validate(token)


# ---------------------------------------------------------------------------
# Negative cases
# ---------------------------------------------------------------------------
class TestRejections:
    def test_expired(self, jwks: tuple[str, str, _JwksState]) -> None:
        jwks_uri, _, _ = jwks
        v = JWKSTokenValidator(jwks_uri=jwks_uri)
        token = jwt.encode({"sub": "u", "exp": int(time.time()) - 120}, KEY1, algorithm="RS256", headers={"kid": "k1"})
        with pytest.raises(SecurityException) as exc:
            v.validate(token)
        assert exc.value.code == "INVALID_TOKEN"

    def test_missing_exp_rejected(self, jwks: tuple[str, str, _JwksState]) -> None:
        jwks_uri, _, _ = jwks
        v = JWKSTokenValidator(jwks_uri=jwks_uri)
        token = jwt.encode({"sub": "u"}, KEY1, algorithm="RS256", headers={"kid": "k1"})
        with pytest.raises(SecurityException):
            v.validate(token)

    def test_bad_signature(self, jwks: tuple[str, str, _JwksState]) -> None:
        jwks_uri, _, _ = jwks
        v = JWKSTokenValidator(jwks_uri=jwks_uri)
        # Signed with KEY2 but presented under kid k1 (which maps to KEY1).
        token = _mint({"sub": "u"}, key=KEY2, kid="k1")
        with pytest.raises(SecurityException):
            v.validate(token)

    def test_wrong_issuer(self, jwks: tuple[str, str, _JwksState]) -> None:
        jwks_uri, _, _ = jwks
        v = JWKSTokenValidator(jwks_uri=jwks_uri, issuer="https://good.example")
        with pytest.raises(SecurityException):
            v.validate(_mint({"sub": "u", "iss": "https://evil.example"}))

    def test_unknown_kid(self, jwks: tuple[str, str, _JwksState]) -> None:
        jwks_uri, _, _ = jwks
        v = JWKSTokenValidator(jwks_uri=jwks_uri)
        token = _mint({"sub": "u"}, key=KEY2, kid="nope")
        with pytest.raises(SecurityException):
            v.validate(token)


# ---------------------------------------------------------------------------
# Key rotation + OIDC discovery
# ---------------------------------------------------------------------------
class TestRotationAndDiscovery:
    def test_key_rotation(self, jwks: tuple[str, str, _JwksState]) -> None:
        jwks_uri, _, state = jwks
        state.keys.append(_jwk(KEY2.public_key(), "k2"))  # rotate in a new key
        v = JWKSTokenValidator(jwks_uri=jwks_uri)
        token = _mint({"sub": "rotated"}, key=KEY2, kid="k2")
        assert v.validate(token)["sub"] == "rotated"

    def test_oidc_discovery(self, jwks: tuple[str, str, _JwksState]) -> None:
        _, issuer, _ = jwks
        discovered_jwks, discovered_issuer = discover_oidc(issuer)
        assert discovered_jwks == f"{issuer}/jwks"
        assert discovered_issuer == issuer
        v = JWKSTokenValidator(jwks_uri=discovered_jwks, issuer=discovered_issuer)
        assert v.validate(_mint({"sub": "u", "iss": issuer}))["sub"] == "u"

    def test_oidc_discovery_failure(self) -> None:
        with pytest.raises(SecurityException) as exc:
            discover_oidc("http://127.0.0.1:1/nope", timeout=1.0)
        assert exc.value.code == "OIDC_DISCOVERY_FAILED"


# ---------------------------------------------------------------------------
# Claim-mapping options
# ---------------------------------------------------------------------------
class TestClaimMappingOptions:
    def test_authority_prefix(self, jwks: tuple[str, str, _JwksState]) -> None:
        jwks_uri, _, _ = jwks
        v = JWKSTokenValidator(
            jwks_uri=jwks_uri,
            claim_mappings=ClaimMappings(authority_claims=("roles",), authority_prefix="ROLE_"),
        )
        ctx = v.to_security_context(_mint({"sub": "u", "roles": ["admin"]}))
        assert ctx.roles == ["ROLE_admin"]

    def test_principal_falls_back_to_sub(self, jwks: tuple[str, str, _JwksState]) -> None:
        jwks_uri, _, _ = jwks
        v = JWKSTokenValidator(jwks_uri=jwks_uri)  # default principal ("oid","sub")
        ctx = v.to_security_context(_mint({"sub": "only-sub"}))
        assert ctx.user_id == "only-sub"

    def test_returns_security_context_instance(self, jwks: tuple[str, str, _JwksState]) -> None:
        jwks_uri, _, _ = jwks
        v = JWKSTokenValidator(jwks_uri=jwks_uri)
        assert isinstance(v.to_security_context(_mint({"sub": "u"})), SecurityContext)
