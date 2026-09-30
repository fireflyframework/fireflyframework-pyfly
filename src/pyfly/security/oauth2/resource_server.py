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

import time
from dataclasses import dataclass, field
from http.client import HTTPException
from typing import Any
from urllib.parse import urlsplit

import jwt

from pyfly.kernel.exceptions import SecurityException
from pyfly.security.context import SecurityContext
from pyfly.security.oauth2._jwks import (
    AsyncBoundedJWKSClient,
    BoundedJWKSClient,
    fetch_json,
    remaining_seconds,
    validate_endpoint,
    validate_limit,
)
from pyfly.security.oauth2.jwks import AsyncJWKSFetcher, JWKSFetcher

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


def discover_oidc(issuer_uri: str, *, timeout: float = 10.0, max_bytes: int = 65536) -> tuple[str, str]:
    """Fetch bounded OIDC metadata from the configured issuer without redirects.

    The metadata issuer must match *issuer_uri* exactly. HTTPS JWKS endpoints
    advertised by that trusted issuer may use a different origin; loopback HTTP
    development endpoints must share the issuer's origin.
    """
    try:
        validate_endpoint(issuer_uri)
        if urlsplit(issuer_uri).query:
            raise ValueError("Issuer must not contain a query")
        well_known = f"{issuer_uri.rstrip('/')}/.well-known/openid-configuration"
        doc = fetch_json(well_known, timeout=timeout, max_bytes=max_bytes)
        if doc.get("issuer") != issuer_uri:
            raise ValueError("Discovery issuer does not exactly match the configured issuer")
        jwks_uri = doc.get("jwks_uri")
        if not isinstance(jwks_uri, str) or not jwks_uri:
            raise ValueError("Discovery document has no valid jwks_uri")
        validate_endpoint(jwks_uri)
        endpoint, issuer = urlsplit(jwks_uri), urlsplit(issuer_uri)
        if endpoint.scheme == "http" and (endpoint.scheme, endpoint.netloc) != (issuer.scheme, issuer.netloc):
            raise ValueError("HTTP JWKS endpoint must share the loopback issuer origin")
        return jwks_uri, issuer_uri
    except (OSError, HTTPException, ValueError, TypeError, RecursionError) as exc:
        raise SecurityException("OIDC discovery failed", code="OIDC_DISCOVERY_FAILED") from exc


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
        jwks_timeout: Cooperative timeout (seconds) for each JWKS refresh.
        jwks_cache_seconds: JWK-set and signing-key lifespan (seconds).
        jwks_min_refresh_seconds: Minimum interval between refresh attempts
            after the initial successful load (also throttles outages).
        jwks_max_bytes: Maximum JWKS response bytes (default: 262144).
        jwks_max_keys: Maximum number of keys in a JWKS (default: 100).
        max_token_bytes: Maximum ASCII compact-token bytes (default: 16384).
        allowed_token_types: Optional exact JOSE ``typ`` allowlist.
        required_token_use: Optional required ``token_use`` claim, e.g. ``access``.
        allowed_client_ids: Optional client claim allowlist.
        client_id_claim: Claim checked against ``allowed_client_ids``; default
            ``client_id``. Set ``azp`` for providers using that claim.
        jwks_fetcher: Optional borrowed synchronous raw-byte fetcher.
        async_jwks_fetcher: Borrowed async raw-byte fetcher, required for
            ``validate_async``. Used on a single event loop with a separate cache.
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
        jwks_cache_seconds: float = 300,
        jwks_min_refresh_seconds: float = 30,
        jwks_max_bytes: int = 262144,
        jwks_max_keys: int = 100,
        max_token_bytes: int = 16384,
        allowed_token_types: list[str] | None = None,
        required_token_use: str | None = None,
        allowed_client_ids: list[str] | None = None,
        client_id_claim: str = "client_id",
        jwks_fetcher: JWKSFetcher | None = None,
        async_jwks_fetcher: AsyncJWKSFetcher | None = None,
    ) -> None:
        self._jwks_client = BoundedJWKSClient(
            jwks_uri,
            lifespan=jwks_cache_seconds,
            timeout=jwks_timeout,
            min_refresh_seconds=jwks_min_refresh_seconds,
            max_bytes=jwks_max_bytes,
            max_keys=jwks_max_keys,
            fetcher=jwks_fetcher,
        )
        self._async_jwks_client = (
            AsyncBoundedJWKSClient(
                jwks_uri,
                lifespan=jwks_cache_seconds,
                timeout=jwks_timeout,
                min_refresh_seconds=jwks_min_refresh_seconds,
                max_bytes=jwks_max_bytes,
                max_keys=jwks_max_keys,
                async_fetcher=async_jwks_fetcher,
            )
            if async_jwks_fetcher is not None
            else None
        )
        validate_limit(max_token_bytes, "max_token_bytes", integer=True)
        validate_limit(leeway, "leeway", allow_zero=True)
        self._max_token_bytes = max_token_bytes
        self._allowed_token_types = tuple(allowed_token_types or [])
        self._required_token_use = required_token_use
        self._allowed_client_ids = tuple(allowed_client_ids or [])
        self._client_id_claim = client_id_claim
        self._issuer = issuer
        self._audiences = [a for a in (audiences or []) if a]
        self._algorithms = list(algorithms or ["RS256"])
        if any(
            alg not in jwt.algorithms.get_default_algorithms() or alg == "none" or alg.startswith("HS")
            for alg in self._algorithms
        ):
            raise ValueError("JWKS validation requires asymmetric signing algorithms")
        self._leeway = leeway
        self._validate_audience = validate_audience
        self._mappings = claim_mappings or ClaimMappings()

    def validate(self, token: str, *, timeout: float | None = None) -> dict[str, Any]:
        """Validate a JWT and return its decoded payload.

        Verifies the signature (via the JWKS key matching the token's ``kid``),
        ``iss``, ``aud`` (only when audiences are configured and audience
        validation is enabled), and ``exp`` — with ``leeway`` seconds of
        clock-skew tolerance.

        ``timeout`` optionally supplies a positive finite cooperative budget
        covering lock waiting, retrieval, parsing and JWT verification. Synchronous
        work cannot be forcibly cancelled; late results are rejected. ``None``
        retains the per-refresh timeout without a total validation deadline.

        Raises:
            SecurityException: If the token is invalid, expired, or its key is
                not found.
        """
        deadline = None
        if timeout is not None:
            validate_limit(timeout, "timeout")
            deadline = time.monotonic() + timeout
        try:
            kid, algorithm = self._token_header(token)
            signing_key = self._jwks_client.get_signing_key(kid, algorithm, deadline=deadline)
            return self._decode_token(token, signing_key, deadline)
        except (jwt.PyJWTError, ValueError, TypeError, OverflowError, RecursionError, TimeoutError) as exc:
            raise SecurityException(f"Token validation failed: {exc}", code="INVALID_TOKEN") from exc

    async def validate_async(self, token: str, *, timeout: float | None = None) -> dict[str, Any]:
        """Validate with an explicit borrowed async fetcher on one event loop.

        The optional positive finite timeout covers lock waiting, fetching and
        validation. Cancellation propagates through the directly awaited fetcher
        and waits for its cleanup. Blocking work and cleanup remain cooperative;
        late results cannot publish keys or return successfully. Sync and async
        validation use independent caches. Context helpers remain synchronous.
        """
        if self._async_jwks_client is None:
            raise ValueError("validate_async requires async_jwks_fetcher")
        deadline = None
        if timeout is not None:
            validate_limit(timeout, "timeout")
            deadline = time.monotonic() + timeout
        try:
            kid, algorithm = self._token_header(token)
            signing_key = await self._async_jwks_client.get_signing_key_async(kid, algorithm, deadline=deadline)
            return self._decode_token(token, signing_key, deadline)
        except (jwt.PyJWTError, ValueError, TypeError, OverflowError, RecursionError, TimeoutError) as exc:
            raise SecurityException(f"Token validation failed: {exc}", code="INVALID_TOKEN") from exc

    def _token_header(self, token: str) -> tuple[str, str]:
        if not isinstance(token, str) or len(token) > self._max_token_bytes or not token.isascii():
            raise jwt.InvalidTokenError("Token exceeds configured limit or is not ASCII")
        header = jwt.get_unverified_header(token)
        algorithm, kid = header.get("alg"), header.get("kid")
        if not isinstance(algorithm, str) or algorithm not in self._algorithms:
            raise jwt.InvalidAlgorithmError("Token algorithm is not allowed")
        if not isinstance(kid, str) or not kid:
            raise jwt.InvalidTokenError("Token requires a signing key identifier")
        if self._allowed_token_types and header.get("typ") not in self._allowed_token_types:
            raise jwt.InvalidTokenError("Token type is not allowed")
        return kid, algorithm

    def _decode_token(self, token: str, signing_key: jwt.PyJWK, deadline: float | None) -> dict[str, Any]:
        verify_aud = self._validate_audience and bool(self._audiences)
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
        if self._required_token_use is not None and payload.get("token_use") != self._required_token_use:
            raise jwt.InvalidTokenError("Token use is not allowed")
        if self._allowed_client_ids and payload.get(self._client_id_claim) not in self._allowed_client_ids:
            raise jwt.InvalidTokenError("Token client is not allowed")
        if deadline is not None:
            remaining_seconds(deadline)
        return payload

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
