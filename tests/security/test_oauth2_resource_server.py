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
from concurrent.futures import ThreadPoolExecutor
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
        self.requests = 0
        self.status = 200
        self.discovery_override: Any = None
        self.raw_body: bytes | None = None
        self.redirect: str | None = None

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
            state.requests += 1
            is_discovery = self.path.endswith("/.well-known/openid-configuration")
            payload = state.discovery() if is_discovery else state.document()
            if is_discovery and state.discovery_override is not None:
                payload = state.discovery_override
            body = state.raw_body if state.raw_body is not None else json.dumps(payload).encode()
            self.send_response(state.status)
            if state.redirect:
                self.send_header("Location", state.redirect)
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
        httpd.server_close()


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


class TestBoundedJWKS:
    def test_removed_cached_key_expires(self, jwks: tuple[str, str, _JwksState]) -> None:
        uri, _, state = jwks
        validator = JWKSTokenValidator(uri, jwks_cache_seconds=0.02)
        token = _mint({"sub": "u"})
        assert validator.validate(token)["sub"] == "u"
        state.keys = [_jwk(KEY2.public_key(), "k2")]
        time.sleep(0.03)
        with pytest.raises(SecurityException):
            validator.validate(token)

    def test_unknown_kids_share_refresh_budget(self, jwks: tuple[str, str, _JwksState]) -> None:
        uri, _, state = jwks
        validator = JWKSTokenValidator(uri)
        validator.validate(_mint({}))
        for index in range(12):
            with pytest.raises(SecurityException):
                validator.validate(_mint({}, kid=f"unknown-{index}"))
        assert state.requests == 2

    def test_concurrent_cold_cache_fetches_once(self, jwks: tuple[str, str, _JwksState]) -> None:
        uri, _, state = jwks
        validator = JWKSTokenValidator(uri)
        token = _mint({"sub": "u"})
        barrier = threading.Barrier(8)

        def validate(_: int) -> str:
            barrier.wait()
            return str(validator.validate(token)["sub"])

        with ThreadPoolExecutor(max_workers=8) as pool:
            assert list(pool.map(validate, range(8))) == ["u"] * 8
        assert state.requests == 1

    def test_outage_preserves_valid_keys_but_never_extends_expiry(self, jwks: tuple[str, str, _JwksState]) -> None:
        uri, _, state = jwks
        validator = JWKSTokenValidator(uri, jwks_cache_seconds=0.1)
        token = _mint({"sub": "u"})
        validator.validate(token)
        state.status = 503
        with pytest.raises(SecurityException):
            validator.validate(_mint({}, kid="unknown"))
        assert validator.validate(token)["sub"] == "u"
        time.sleep(0.11)
        with pytest.raises(SecurityException):
            validator.validate(token)
        assert state.requests == 2

    def test_initial_outage_is_rate_limited(self, jwks: tuple[str, str, _JwksState]) -> None:
        uri, _, state = jwks
        validator = JWKSTokenValidator(uri)
        state.status = 503
        for _ in range(5):
            with pytest.raises(SecurityException):
                validator.validate(_mint({}))
        assert state.requests == 1

    def test_rotation_refreshes_once_and_drops_removed_keys(self, jwks: tuple[str, str, _JwksState]) -> None:
        uri, _, state = jwks
        validator = JWKSTokenValidator(uri)
        validator.validate(_mint({}))
        state.keys = [_jwk(KEY2.public_key(), "k2")]
        assert validator.validate(_mint({"sub": "rotated"}, key=KEY2, kid="k2"))["sub"] == "rotated"
        with pytest.raises(SecurityException):
            validator.validate(_mint({}))
        assert state.requests == 2

    def test_document_byte_limit(self, jwks: tuple[str, str, _JwksState]) -> None:
        uri, _, state = jwks
        state.raw_body = json.dumps({"keys": state.keys, "padding": "x" * 262144}).encode()
        with pytest.raises(SecurityException):
            JWKSTokenValidator(uri).validate(_mint({}))

    def test_key_count_limit(self, jwks: tuple[str, str, _JwksState]) -> None:
        uri, _, state = jwks
        state.keys += [_jwk(KEY2.public_key(), f"k{i}") for i in range(101)]
        with pytest.raises(SecurityException):
            JWKSTokenValidator(uri).validate(_mint({}))

    def test_token_byte_limit_before_fetch(self, jwks: tuple[str, str, _JwksState]) -> None:
        uri, _, state = jwks
        with pytest.raises(SecurityException):
            JWKSTokenValidator(uri).validate(_mint({"padding": "x" * 16384}))
        assert state.requests == 0

    def test_disallowed_algorithm_does_not_fetch(self, jwks: tuple[str, str, _JwksState]) -> None:
        uri, _, state = jwks
        token = jwt.encode({"exp": int(time.time()) + 60}, KEY1, algorithm="RS384", headers={"kid": "k1"})
        with pytest.raises(SecurityException):
            JWKSTokenValidator(uri).validate(token)
        assert state.requests == 0

    def test_optional_token_class_and_client_policy(self, jwks: tuple[str, str, _JwksState]) -> None:
        uri, _, _ = jwks
        validator = JWKSTokenValidator(uri, required_token_use="access", allowed_client_ids=["app"])
        assert validator.validate(_mint({"token_use": "access", "client_id": "app"}))["client_id"] == "app"
        for claims in ({}, {"token_use": "id", "client_id": "app"}, {"token_use": "access", "client_id": "other"}):
            with pytest.raises(SecurityException):
                validator.validate(_mint(claims))

    def test_optional_header_type_policy(self, jwks: tuple[str, str, _JwksState]) -> None:
        uri, _, state = jwks
        validator = JWKSTokenValidator(uri, allowed_token_types=["at+jwt"])
        with pytest.raises(SecurityException):
            validator.validate(_mint({}))
        assert state.requests == 0
        token = jwt.encode(
            {"exp": int(time.time()) + 60}, KEY1, algorithm="RS256", headers={"kid": "k1", "typ": "at+jwt"}
        )
        assert validator.validate(token)["exp"]

    @pytest.mark.parametrize(
        "document", [[], {}, {"issuer": "https://other.example", "jwks_uri": "https://other.example/jwks"}]
    )
    def test_discovery_requires_exact_issuer(self, jwks: tuple[str, str, _JwksState], document: Any) -> None:
        _, issuer, state = jwks
        state.discovery_override = document
        with pytest.raises(SecurityException) as error:
            discover_oidc(issuer)
        assert error.value.code == "OIDC_DISCOVERY_FAILED"

    def test_discovery_is_bounded(self, jwks: tuple[str, str, _JwksState]) -> None:
        _, issuer, state = jwks
        state.discovery_override = {**state.discovery(), "padding": "x" * 65536}
        with pytest.raises(SecurityException):
            discover_oidc(issuer)

    @pytest.mark.parametrize(
        "endpoint",
        [
            "file:///etc/passwd",
            "http://example.com/jwks",
            "http://127.0.0.1:1/jwks",
            "https://user:pass@example.com/jwks",
        ],
    )
    def test_discovery_rejects_untrusted_endpoint(self, jwks: tuple[str, str, _JwksState], endpoint: str) -> None:
        _, issuer, state = jwks
        state.discovery_override = {"issuer": issuer, "jwks_uri": endpoint}
        with pytest.raises(SecurityException):
            discover_oidc(issuer)

    def test_redirect_is_not_followed(self, jwks: tuple[str, str, _JwksState]) -> None:
        uri, issuer, state = jwks
        state.status = 302
        state.redirect = f"{issuer}/redirect-target"
        with pytest.raises(SecurityException):
            JWKSTokenValidator(uri).validate(_mint({}))
        assert state.requests == 1

    def test_concurrent_unknown_keys_share_one_refresh(self, jwks: tuple[str, str, _JwksState]) -> None:
        uri, _, state = jwks
        validator = JWKSTokenValidator(uri)
        validator.validate(_mint({}))
        tokens = [_mint({}, kid=f"unknown-{i}") for i in range(8)]
        barrier = threading.Barrier(8)

        def validate(token: str) -> bool:
            barrier.wait()
            with pytest.raises(SecurityException):
                validator.validate(token)
            return True

        with ThreadPoolExecutor(max_workers=8) as pool:
            assert all(pool.map(validate, tokens))
        assert state.requests == 2

    def test_refresh_recovers_after_cooldown(self, jwks: tuple[str, str, _JwksState]) -> None:
        uri, _, state = jwks
        validator = JWKSTokenValidator(uri, jwks_min_refresh_seconds=0.02)
        state.status = 503
        token = _mint({"sub": "u"})
        with pytest.raises(SecurityException):
            validator.validate(token)
        state.status = 200
        time.sleep(0.03)
        assert validator.validate(token)["sub"] == "u"
        assert state.requests == 2

    @pytest.mark.parametrize("body", [b"[]", b"not json", b'{"keys":null}', b'{"keys":[null]}'])
    def test_malformed_jwks_fails_closed(self, jwks: tuple[str, str, _JwksState], body: bytes) -> None:
        uri, _, state = jwks
        state.raw_body = body
        with pytest.raises(SecurityException) as error:
            JWKSTokenValidator(uri).validate(_mint({}))
        assert error.value.code == "INVALID_TOKEN"

    @pytest.mark.parametrize("change", [{"alg": "RS384"}, {"use": "enc"}, {"key_ops": ["sign"]}])
    def test_signing_key_constraints_are_enforced(
        self, jwks: tuple[str, str, _JwksState], change: dict[str, Any]
    ) -> None:
        uri, _, state = jwks
        state.keys[0].update(change)
        with pytest.raises(SecurityException):
            JWKSTokenValidator(uri).validate(_mint({}))

    def test_jwk_without_alg_supports_configured_algorithm(self, jwks: tuple[str, str, _JwksState]) -> None:
        uri, _, state = jwks
        del state.keys[0]["alg"]
        token = jwt.encode({"exp": int(time.time()) + 60}, KEY1, algorithm="RS384", headers={"kid": "k1"})
        assert JWKSTokenValidator(uri, algorithms=["RS384"]).validate(token)["exp"]

    def test_token_key_urls_never_select_endpoint(self, jwks: tuple[str, str, _JwksState]) -> None:
        uri, _, state = jwks
        token = jwt.encode(
            {"exp": int(time.time()) + 60},
            KEY1,
            algorithm="RS256",
            headers={"kid": "k1", "jku": "http://127.0.0.1:1/jwks", "x5u": "file:///etc/passwd"},
        )
        assert JWKSTokenValidator(uri).validate(token)["exp"]
        assert state.requests == 1

    def test_discovery_accepts_cross_origin_https_from_trusted_issuer(self, jwks: tuple[str, str, _JwksState]) -> None:
        _, issuer, state = jwks
        state.discovery_override = {"issuer": issuer, "jwks_uri": "https://keys.example.com/jwks"}
        assert discover_oidc(issuer) == ("https://keys.example.com/jwks", issuer)

    def test_discovery_does_not_strip_issuer_trailing_slash(self, jwks: tuple[str, str, _JwksState]) -> None:
        _, issuer, state = jwks
        with pytest.raises(SecurityException):
            discover_oidc(issuer + "/")
        state.discovery_override = {"issuer": issuer + "/", "jwks_uri": f"{issuer}/jwks"}
        assert discover_oidc(issuer + "/")[1] == issuer + "/"

    @pytest.mark.parametrize("uri", ["file:///etc/passwd", "http://example.com/jwks", "https://u:p@x/jwks"])
    def test_invalid_configured_endpoints_rejected(self, uri: str) -> None:
        with pytest.raises(ValueError):
            JWKSTokenValidator(uri)

    @pytest.mark.parametrize(
        "options",
        [
            {"jwks_cache_seconds": 0},
            {"jwks_min_refresh_seconds": -1},
            {"jwks_timeout": float("inf")},
            {"jwks_max_bytes": 0},
            {"jwks_max_keys": 0},
            {"max_token_bytes": -1},
            {"algorithms": ["HS256"]},
            {"algorithms": ["none"]},
        ],
    )
    def test_invalid_limits_and_symmetric_algorithms_rejected(self, options: dict[str, Any]) -> None:
        with pytest.raises(ValueError):
            JWKSTokenValidator("https://issuer.example/jwks", **options)

    def test_auto_configuration_pins_issuer_with_explicit_jwks(self, jwks: tuple[str, str, _JwksState]) -> None:
        from pyfly.core.config import Config
        from pyfly.security.auto_configuration import OAuth2ResourceServerAutoConfiguration

        uri, issuer, _ = jwks
        config = Config(
            {
                "pyfly": {
                    "security": {
                        "oauth2": {
                            "resource-server": {
                                "jwks-uri": uri,
                                "issuer-uri": issuer,
                                "required-token-use": "access",
                                "allowed-client-ids": "app",
                                "client-id-claim": "azp",
                                "allowed-token-types": "JWT",
                            }
                        }
                    }
                }
            }
        )
        validator = OAuth2ResourceServerAutoConfiguration().jwks_token_validator(config)
        valid = {"iss": issuer, "token_use": "access", "azp": "app"}
        assert validator.validate(_mint(valid))["azp"] == "app"
        for change in ({"iss": "wrong"}, {"token_use": "id"}, {"azp": "other"}):
            with pytest.raises(SecurityException):
                validator.validate(_mint({**valid, **change}))

    def test_auto_configuration_rejects_conflicting_issuers(self) -> None:
        from pyfly.core.config import Config
        from pyfly.security.auto_configuration import OAuth2ResourceServerAutoConfiguration

        config = Config(
            {
                "pyfly": {
                    "security": {
                        "oauth2": {
                            "resource-server": {
                                "jwks-uri": "https://issuer.example/jwks",
                                "issuer-uri": "https://issuer.example",
                                "issuer": "https://other.example",
                            }
                        }
                    }
                }
            }
        )
        with pytest.raises(SecurityException):
            OAuth2ResourceServerAutoConfiguration().jwks_token_validator(config)

    @pytest.mark.parametrize("discovery", [False, True])
    def test_malformed_http_response_is_a_security_failure(
        self, monkeypatch: pytest.MonkeyPatch, discovery: bool
    ) -> None:
        from http.client import BadStatusLine
        from urllib.request import OpenerDirector

        def invalid_response(*args: Any, **kwargs: Any) -> Any:
            raise BadStatusLine("invalid status")

        monkeypatch.setattr(OpenerDirector, "open", invalid_response)
        with pytest.raises(SecurityException):
            if discovery:
                discover_oidc("https://issuer.example")
            else:
                JWKSTokenValidator("https://issuer.example/jwks").validate(_mint({}))

    def test_drip_body_exceeds_fetch_deadline(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from types import SimpleNamespace
        from urllib.request import OpenerDirector

        from pyfly.security.oauth2 import _jwks

        now = [0.0]
        monkeypatch.setattr(_jwks, "time", SimpleNamespace(monotonic=lambda: now[0]))
        body = json.dumps({"keys": [_jwk(KEY1.public_key(), "k1")]}).encode()

        class SlowResponse:
            status = 200
            headers = {"Content-Encoding": "identity"}

            def __enter__(self) -> SlowResponse:
                return self

            def __exit__(self, *args: Any) -> None:
                pass

            def read(self, amount: int) -> bytes:
                now[0] += 2
                return body[:amount]

            def read1(self, amount: int) -> bytes:
                return self.read(amount)

        monkeypatch.setattr(OpenerDirector, "open", lambda *args, **kwargs: SlowResponse())
        with pytest.raises(SecurityException):
            JWKSTokenValidator("https://issuer.example/jwks", jwks_timeout=1).validate(_mint({}))

    @pytest.mark.parametrize("outage", [False, True])
    def test_slow_refresh_cooldown_starts_on_completion(
        self, jwks: tuple[str, str, _JwksState], monkeypatch: pytest.MonkeyPatch, outage: bool
    ) -> None:
        from types import SimpleNamespace

        from pyfly.security.oauth2 import _jwks

        uri, _, state = jwks
        now = [0.0]
        monkeypatch.setattr(_jwks, "time", SimpleNamespace(monotonic=lambda: now[0]))
        fetch = _jwks.fetch_json

        def slow_fetch(*args: Any, **kwargs: Any) -> dict[str, Any]:
            try:
                return fetch(*args, **kwargs)
            finally:
                now[0] += 60

        monkeypatch.setattr(_jwks, "fetch_json", slow_fetch)
        validator = JWKSTokenValidator(uri)
        if outage:
            state.status = 503
        for _ in range(3):
            with pytest.raises(SecurityException):
                validator.validate(_mint({}, kid="unknown"))
        assert state.requests == 1

    @pytest.mark.parametrize(
        "leeway",
        [float("nan"), float("inf"), -1, True, 10**1000],
        ids=["nan", "infinity", "negative", "bool", "overflow"],
    )
    def test_invalid_clock_skew_rejected(self, leeway: Any) -> None:
        with pytest.raises(ValueError):
            JWKSTokenValidator("https://issuer.example/jwks", leeway=leeway)

    @pytest.mark.parametrize("option", ["jwks_max_bytes", "jwks_max_keys", "max_token_bytes"])
    @pytest.mark.parametrize("value", [True, 1.5, 10**1000], ids=["bool", "fraction", "overflow"])
    def test_byte_and_count_limits_require_finite_integers(self, option: str, value: Any) -> None:
        with pytest.raises(ValueError):
            JWKSTokenValidator("https://issuer.example/jwks", **{option: value})
