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
"""OAuth2 Resource Server — JWKS-based JWT validation.

A config-driven, multi-IdP bearer-token validator. Out of the box it accepts
tokens from **Keycloak**, **Microsoft Entra ID** (v1.0 + v2.0) and **AWS
Cognito** without subclassing, by reading roles/scopes/principal from a
configurable set of claim paths (see :class:`ClaimMappings`).

Spring Security parity: ``issuer-uri`` OIDC discovery, configurable signing
algorithms, clock-skew leeway, a list of accepted audiences (with an opt-out for
Cognito access tokens, which carry no ``aud``), and config-driven authority /
scope / principal claim mapping.

The class stays the single base type returned by the framework auto-config bean,
so an application that registers its own ``JWKSTokenValidator`` subclass (e.g. to
do bespoke claim mapping) transparently overrides the default via
``@conditional_on_missing_bean(JWKSTokenValidator)``.
"""

from __future__ import annotations

import json
import urllib.request
from dataclasses import dataclass, field
from typing import Any

import jwt
from jwt import PyJWKClient

from pyfly.kernel.exceptions import SecurityException
from pyfly.security.context import SecurityContext

# Default clock-skew tolerance, in seconds. Matches Spring Security's
# ``JwtTimestampValidator`` default (60s). Without it, a token whose ``iat`` /
# ``nbf`` is a few seconds ahead of this server's clock — routine with real
# IdPs — is rejected as "not yet valid", causing intermittent 401s.
DEFAULT_CLOCK_SKEW_SECONDS = 60

# Default claim paths searched (in order, all collected) for authorities/roles.
# Covers every mainstream IdP with zero configuration:
#   * ``roles``                       — flat (custom IdPs, Entra app roles)
#   * ``scopes`` / ``authorities``    — common conventions
#   * ``realm_access.roles``          — Keycloak realm roles
#   * ``resource_access.*.roles``     — Keycloak per-client roles (``*`` = any client)
#   * ``groups``                      — Entra group object-ids
#   * ``cognito:groups``              — AWS Cognito groups
# Applications can narrow this list via
# ``pyfly.security.oauth2.resource-server.authorities-claim-names``.
DEFAULT_AUTHORITY_CLAIMS: tuple[str, ...] = (
    "roles",
    "scopes",
    "authorities",
    "realm_access.roles",
    "resource_access.*.roles",
    "groups",
    "cognito:groups",
)

# Default claim names (space-delimited string or list) mapped to *permissions*.
# ``scp`` is Entra's delegated-scope claim; ``scope`` is the Keycloak / Cognito /
# OAuth2 convention.
DEFAULT_SCOPE_CLAIMS: tuple[str, ...] = ("scp", "scope")

# Default principal (user id) claim search order: Entra's stable ``oid`` first,
# then the standard ``sub``.
DEFAULT_PRINCIPAL_CLAIMS: tuple[str, ...] = ("oid", "sub")


@dataclass(frozen=True)
class ClaimMappings:
    """Config-driven mapping from JWT claims onto a :class:`SecurityContext`.

    All claim names support **dotted paths** (``realm_access.roles``) and a
    single-level ``*`` **wildcard** that iterates every key at that level
    (``resource_access.*.roles``). A path segment is split on ``.`` only, so
    colon-bearing claim names such as ``cognito:groups`` are matched verbatim.
    """

    principal_claims: tuple[str, ...] = DEFAULT_PRINCIPAL_CLAIMS
    authority_claims: tuple[str, ...] = DEFAULT_AUTHORITY_CLAIMS
    scope_claims: tuple[str, ...] = DEFAULT_SCOPE_CLAIMS
    # Prefix applied to every extracted authority (Spring uses ``SCOPE_`` /
    # ``ROLE_``). Default empty: authorities are kept as the raw claim value so
    # ``has_role("CdM.Gn")`` matches the token's literal role string.
    authority_prefix: str = ""
    # Claims copied verbatim (string-coerced) into ``SecurityContext.attributes``
    # (e.g. ``tid``, ``preferred_username``, ``employeeid``).
    attribute_claims: tuple[str, ...] = field(default_factory=tuple)


def _resolve_claim_path(payload: dict[str, Any], path: str) -> list[Any]:
    """Resolve a dotted claim *path* (with optional ``*`` wildcard) to a flat
    list of leaf values. Missing paths yield ``[]``."""
    segments = path.split(".")
    # Frontier of nodes currently being walked; starts at the payload root.
    nodes: list[Any] = [payload]
    for seg in segments:
        nxt: list[Any] = []
        for node in nodes:
            if not isinstance(node, dict):
                continue
            if seg == "*":
                nxt.extend(node.values())
            elif seg in node:
                nxt.append(node[seg])
        nodes = nxt
        if not nodes:
            return []
    return nodes


def _flatten_strs(values: list[Any]) -> list[str]:
    """Flatten leaf values (strings or lists of strings) into a string list,
    preserving order and dropping empties / non-strings."""
    out: list[str] = []
    for v in values:
        if isinstance(v, str):
            if v:
                out.append(v)
        elif isinstance(v, (list, tuple)):
            out.extend(str(x) for x in v if isinstance(x, (str, int)) and str(x))
    return out


def discover_oidc(issuer_uri: str, *, timeout: float = 10.0) -> tuple[str, str]:
    """Fetch an OIDC provider's discovery document and return
    ``(jwks_uri, issuer)``.

    Mirrors Spring's ``issuer-uri``: GET ``<issuer_uri>/.well-known/openid-configuration``
    and read ``jwks_uri`` + ``issuer``. The returned ``issuer`` is the
    authoritative value from the document (used to validate the ``iss`` claim).

    Raises:
        SecurityException: If the document cannot be fetched or lacks ``jwks_uri``.
    """
    base = issuer_uri.rstrip("/")
    well_known = f"{base}/.well-known/openid-configuration"
    try:
        with urllib.request.urlopen(well_known, timeout=timeout) as resp:  # noqa: S310 (https config URL)
            doc = json.loads(resp.read().decode("utf-8"))
    except Exception as exc:  # network / JSON / URL errors
        raise SecurityException(
            f"OIDC discovery failed for issuer-uri {issuer_uri!r}: {exc}",
            code="OIDC_DISCOVERY_FAILED",
        ) from exc
    jwks_uri = doc.get("jwks_uri")
    if not jwks_uri:
        raise SecurityException(
            f"OIDC discovery document at {well_known!r} has no 'jwks_uri'.",
            code="OIDC_DISCOVERY_FAILED",
        )
    return str(jwks_uri), str(doc.get("issuer") or base)


class JWKSTokenValidator:
    """Validates JWTs using a remote JWKS endpoint.

    Fetches and caches public keys from the JWKS URI, verifies the signature,
    ``iss``, ``aud`` (when configured), and ``exp`` (with clock-skew leeway), and
    maps claims to a :class:`SecurityContext`.

    Args:
        jwks_uri: The JWKS endpoint URL.
        issuer: Expected ``iss`` (validated when set).
        audiences: Accepted audiences; the token's ``aud`` must match **any**.
            When empty, audience validation is **disabled** (required for AWS
            Cognito *access* tokens, which carry ``client_id`` instead of ``aud``).
        algorithms: Allowed signing algorithms (default: ``["RS256"]``).
        leeway: Clock-skew tolerance in seconds for ``exp`` / ``nbf`` / ``iat``
            (default: 60).
        validate_audience: Set ``False`` to skip ``aud`` validation even when
            audiences are configured.
        claim_mappings: Config-driven claim→context mapping (default:
            multi-IdP defaults).
        jwks_timeout: HTTP timeout (seconds) for JWKS fetches.
        jwks_cache_seconds: JWK-set cache lifespan (seconds).
    """

    def __init__(
        self,
        jwks_uri: str,
        *,
        issuer: str | None = None,
        audiences: list[str] | None = None,
        algorithms: list[str] | None = None,
        leeway: int = DEFAULT_CLOCK_SKEW_SECONDS,
        validate_audience: bool = True,
        claim_mappings: ClaimMappings | None = None,
        jwks_timeout: float = 30.0,
        jwks_cache_seconds: int = 300,
    ) -> None:
        self._jwks_client = PyJWKClient(
            jwks_uri,
            cache_keys=True,
            cache_jwk_set=True,
            lifespan=jwks_cache_seconds,
            timeout=jwks_timeout,
        )
        self._issuer = issuer
        self._audiences = [a for a in (audiences or []) if a]
        self._algorithms = algorithms or ["RS256"]
        self._leeway = leeway
        self._validate_audience = validate_audience
        self._mappings = claim_mappings or ClaimMappings()

    def validate(self, token: str) -> dict[str, Any]:
        """Validate a JWT and return its decoded payload.

        Verifies the signature (via the JWKS key matching the token's ``kid``),
        ``iss``, ``aud`` (only when audiences are configured and audience
        validation is enabled), and ``exp`` — with ``leeway`` seconds of
        clock-skew tolerance.

        Raises:
            SecurityException: If the token is invalid, expired, or its key is
                not found.
        """
        verify_aud = self._validate_audience and bool(self._audiences)
        try:
            signing_key = self._jwks_client.get_signing_key_from_jwt(token)
            payload = jwt.decode(
                token,
                signing_key.key,
                algorithms=self._algorithms,
                issuer=self._issuer,
                # Pass the list when verifying; ``None`` disables PyJWT's own aud check.
                audience=self._audiences if verify_aud else None,
                leeway=self._leeway,
                options={"require": ["exp"], "verify_aud": verify_aud},
            )
            return payload
        except jwt.PyJWTError as exc:
            raise SecurityException(
                f"Token validation failed: {exc}",
                code="INVALID_TOKEN",
            ) from exc

    def to_security_context(self, token: str) -> SecurityContext:
        """Validate *token* and build a :class:`SecurityContext` from its claims,
        using the configured :class:`ClaimMappings` (multi-IdP by default)."""
        payload = self.validate(token)
        return self._build_context(payload)

    def validate_and_context(self, token: str) -> tuple[dict[str, Any], SecurityContext]:
        """Validate *token* once and return both the raw claims and the context.

        Lets a filter inspect claims (e.g. ``cnf`` for sender-constraining) without
        validating the signature twice."""
        payload = self.validate(token)
        return payload, self._build_context(payload)

    def _build_context(self, payload: dict[str, Any]) -> SecurityContext:
        """Map a validated *payload* onto a :class:`SecurityContext` per the
        configured claim mappings. Subclasses may override for bespoke mapping."""
        return build_security_context(payload, self._mappings)


def build_security_context(payload: dict[str, Any], mappings: ClaimMappings) -> SecurityContext:
    """Map a token/introspection *payload* onto a :class:`SecurityContext`.

    Shared by :class:`JWKSTokenValidator` and :class:`OpaqueTokenIntrospector` so
    JWT and opaque-token resource servers map claims identically.
    """
    m = mappings

    # Principal: first non-empty principal claim wins.
    user_id: str | None = None
    for claim in m.principal_claims:
        vals = _flatten_strs(_resolve_claim_path(payload, claim))
        if vals:
            user_id = vals[0]
            break

    # Authorities/roles: collect across every configured path, de-duplicated
    # (order-preserving), with the optional prefix applied.
    roles: list[str] = []
    seen: set[str] = set()
    for claim in m.authority_claims:
        for raw in _flatten_strs(_resolve_claim_path(payload, claim)):
            value = f"{m.authority_prefix}{raw}" if m.authority_prefix else raw
            if value not in seen:
                seen.add(value)
                roles.append(value)

    # Permissions/scopes: scope claims are space-delimited strings or lists.
    permissions: list[str] = []
    perm_seen: set[str] = set()
    for claim in m.scope_claims:
        for raw in _flatten_strs(_resolve_claim_path(payload, claim)):
            for part in raw.split():
                if part and part not in perm_seen:
                    perm_seen.add(part)
                    permissions.append(part)

    # Attributes: copy configured claims verbatim (string-coerced).
    attributes: dict[str, str] = {}
    for claim in m.attribute_claims:
        vals = _flatten_strs(_resolve_claim_path(payload, claim))
        if vals:
            attributes[claim] = vals[0]

    return SecurityContext(
        user_id=user_id,
        roles=roles,
        permissions=permissions,
        attributes=attributes,
    )


class OpaqueTokenIntrospector:
    """Validates opaque access tokens via an RFC 7662 introspection endpoint.

    The resource server posts the token (with its own client credentials) to the
    authorization server's ``/introspect`` endpoint and maps the returned claims
    onto a :class:`SecurityContext` using the same :class:`ClaimMappings` as the
    JWT validator. Use this for opaque (non-JWT) tokens.
    """

    def __init__(
        self,
        introspection_uri: str,
        *,
        client_id: str,
        client_secret: str,
        claim_mappings: ClaimMappings | None = None,
        timeout: float = 10.0,
    ) -> None:
        self._uri = introspection_uri
        self._client_id = client_id
        self._client_secret = client_secret
        self._mappings = claim_mappings or ClaimMappings()
        self._timeout = timeout

    def introspect(self, token: str) -> dict[str, Any]:
        """Return the introspection claims for *token*, or raise if it is inactive."""
        import httpx

        try:
            with httpx.Client(timeout=self._timeout) as client:
                resp = client.post(
                    self._uri,
                    data={"token": token, "token_type_hint": "access_token"},
                    auth=(self._client_id, self._client_secret),
                    headers={"Accept": "application/json"},
                )
        except httpx.HTTPError as exc:
            raise SecurityException(f"Token introspection request failed: {exc}", code="INVALID_TOKEN") from exc
        if resp.status_code != 200:
            raise SecurityException(f"Token introspection failed (HTTP {resp.status_code})", code="INVALID_TOKEN")
        payload: dict[str, Any] = resp.json()
        if not payload.get("active"):
            raise SecurityException("Token is not active", code="INVALID_TOKEN")
        return payload

    def to_security_context(self, token: str) -> SecurityContext:
        return build_security_context(self.introspect(token), self._mappings)
