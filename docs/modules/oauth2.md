# OAuth 2.1 & OpenID Connect

PyFly ships a complete, standards-driven OAuth2 / OpenID Connect stack that plays
all three roles in the protocol: a **resource server** that validates bearer
tokens from any mainstream IdP, an **OAuth2 client / OIDC relying party** that logs
users in via the `authorization_code` flow, and a first-party **authorization
server** that issues and manages its own tokens. Every piece follows hexagonal
principles — token stores, client repositories and claim mapping are ports with
swappable adapters — and defaults to the hardened behaviour mandated by
[RFC 9700](https://www.rfc-editor.org/rfc/rfc9700) (OAuth 2.0 Security BCP) and
OAuth 2.1: PKCE, exact redirect matching, refresh-token rotation with reuse
detection, audience restriction, and sender-constrained tokens.

This guide is the definitive reference for the OAuth2 module. For the surrounding
authentication/authorization machinery (`SecurityContext`, `HttpSecurity`,
`@pre_authorize`, CSRF, password encoding) see the [Security Guide](security.md).

---

## Table of Contents

- [Overview](#overview)
- [Resource Server](#resource-server)
  - [Enable via configuration](#enable-via-configuration)
  - [JWKS validation](#jwks-validation)
  - [OIDC discovery (issuer-uri)](#oidc-discovery-issuer-uri)
  - [Multi-IdP claim mapping (ClaimMappings)](#multi-idp-claim-mapping-claimmappings)
  - [Error modes](#error-modes)
  - [Resource-server configuration reference](#resource-server-configuration-reference)
  - [Programmatic use](#programmatic-use)
  - [Opaque tokens (OpaqueTokenIntrospector)](#opaque-tokens-opaquetokenintrospector)
- [OAuth2 Client & Login](#oauth2-client-login)
  - [The authorization_code login flow](#the-authorization_code-login-flow)
  - [PKCE (on by default)](#pkce-on-by-default)
  - [state and nonce](#state-and-nonce)
  - [RFC 9207 issuer identification (require_iss)](#rfc-9207-issuer-identification-require_iss)
  - [ID token validation](#id-token-validation)
  - [Built-in providers](#built-in-providers)
  - [Config-driven registrations](#config-driven-registrations)
- [Authorization Server](#authorization-server)
  - [Grant types](#grant-types)
  - [client_credentials](#client_credentials)
  - [refresh_token (rotation + reuse detection)](#refresh_token-rotation-reuse-detection)
  - [authorization_code + PKCE](#authorization_code-pkce)
  - [OIDC ID tokens](#oidc-id-tokens)
  - [Symmetric vs asymmetric signing](#symmetric-vs-asymmetric-signing)
  - [HTTP endpoints (AuthorizationServerEndpoints)](#http-endpoints-authorizationserverendpoints)
  - [Dynamic Client Registration (RFC 7591)](#dynamic-client-registration-rfc-7591)
  - [PAR (RFC 9126) and JAR (RFC 9101)](#par-rfc-9126-and-jar-rfc-9101)
  - [Introspection (RFC 7662) & revocation (RFC 7009)](#introspection-rfc-7662-revocation-rfc-7009)
  - [Metadata & discovery (RFC 8414 + OIDC)](#metadata-discovery-rfc-8414-oidc)
  - [Token stores (memory / redis / postgres)](#token-stores-memory-redis-postgres)
  - [Signing-secret hardening](#signing-secret-hardening)
- [Sender-Constrained Tokens (DPoP & mTLS)](#sender-constrained-tokens-dpop-mtls)
  - [DPoP (RFC 9449)](#dpop-rfc-9449)
  - [mTLS (RFC 8705)](#mtls-rfc-8705)
  - [Resource-server enforcement](#resource-server-enforcement)
  - [DPoP / mTLS helpers](#dpop-mtls-helpers)
- [Standards & Compliance](#standards-compliance)
- [Configuration Reference](#configuration-reference)

---

## Overview

```python
from pyfly.security.oauth2 import (
    # Resource server
    JWKSTokenValidator,
    OpaqueTokenIntrospector,
    ClaimMappings,
    ResourceServerProperties,
    discover_oidc,
    # Client & login
    ClientRegistration,
    ClientRegistrationRepository,
    InMemoryClientRegistrationRepository,
    OAuth2LoginHandler,
    OAuth2SessionSecurityFilter,
    google, github, keycloak,
    # Authorization server
    AuthorizationServer,
    AuthorizationServerEndpoints,
    TokenStore,
    InMemoryTokenStore,
)
```

| Role | What it does | Core types | Turn on with |
|---|---|---|---|
| **Resource server** | Validates incoming bearer JWTs against a JWKS endpoint and maps claims to a `SecurityContext` | `JWKSTokenValidator`, `OpaqueTokenIntrospector`, `ClaimMappings` | `pyfly.security.oauth2.resource-server.enabled=true` |
| **Client / OIDC relying party** | Logs users in via the browser `authorization_code` flow against Google / GitHub / Keycloak / any provider | `ClientRegistration`, `OAuth2LoginHandler`, `OAuth2SessionSecurityFilter` | `pyfly.security.oauth2.client.enabled=true` + `pyfly.security.oauth2.login.enabled=true` |
| **Authorization server** | Issues and manages first-party tokens (`client_credentials`, `refresh_token`, `authorization_code`) and exposes the standard OAuth2/OIDC endpoints | `AuthorizationServer`, `AuthorizationServerEndpoints`, `TokenStore` | `pyfly.security.oauth2.authorization-server.enabled=true` |

The three roles are independent — enable any subset. A typical microservice is a
resource server only; an edge/BFF service adds the client/login role; an internal
identity service runs the authorization server.

---

## Resource Server

When PyFly acts as a resource server it receives `Authorization: Bearer <jwt>`
tokens minted by an external authorization server (Keycloak, Microsoft Entra ID,
AWS Cognito, Auth0, or PyFly's own AS) and validates each one before the request
reaches your handlers. `JWKSTokenValidator` verifies the signature against the
provider's published JSON Web Key Set, checks `iss`, `aud` (when configured) and
`exp` (with clock-skew leeway), and maps the claims onto a `SecurityContext`.

### Enable via configuration

The resource-server filter auto-wires when
`pyfly.security.oauth2.resource-server.enabled=true` and `pyjwt` is installed
(`OAuth2ResourceServerAutoConfiguration`). It binds `ResourceServerProperties`,
builds a `JWKSTokenValidator`, and adds an `OAuth2ResourceServerFilter` to the
chain.

```yaml
pyfly:
  security:
    enabled: true
    oauth2:
      resource-server:
        enabled: true
        # Provide a JWKS URI directly, OR an issuer-uri for OIDC discovery:
        issuer-uri: "https://login.microsoftonline.com/<tenant>/v2.0"   # discovers jwks-uri + issuer
        # jwks-uri: "https://login.microsoftonline.com/<tenant>/discovery/v2.0/keys"
        audiences: "api://my-backend"      # comma-separated; the token aud must match ANY
        validate-audience: true            # set false for Cognito ACCESS tokens (no aud)
        algorithms: "RS256"
        clock-skew-seconds: 60             # leeway for iat/nbf/exp (default 60)
        jwks-timeout-seconds: 30
        jwks-cache-seconds: 300
        # Config-driven claim mapping (dotted paths, '*' wildcard, colon-safe):
        principal-claim-names: "oid,sub"
        authorities-claim-names: "roles,realm_access.roles,resource_access.*.roles,groups,cognito:groups"
        scope-claim-names: "scp,scope"     # Entra uses scp; Keycloak/Cognito use scope
        attribute-claims: "tid,preferred_username"
        authority-prefix: ""               # e.g. "ROLE_" / "SCOPE_" for Spring-style authorities
        exclude-patterns: "/actuator/**,/api/v1/version"
        authenticate-error-mode: "anonymous"   # or "401"
```

The filter runs at `HIGHEST_PRECEDENCE + 250` and honours `exclude-patterns`
(fnmatch globs) so public paths skip token validation. JWKS key lookup can do
blocking network I/O on a cache miss, so validation is offloaded to a worker
thread (`anyio.to_thread`) to avoid stalling the event loop. The
`Authorization` header is accepted with either the `Bearer` or `DPoP` scheme
(case-insensitive).

### JWKS validation

`JWKSTokenValidator.validate(token)` performs the full check and returns the
decoded payload:

- fetches (and caches) the signing key whose `kid` matches the token header from
  the JWKS endpoint (`PyJWKClient`, cached for `jwks-cache-seconds`);
- verifies the signature with one of the allowed `algorithms` (default
  `["RS256"]`);
- validates `iss` when an issuer is configured;
- validates `aud` **only** when `audiences` is non-empty and
  `validate-audience` is `true` — the token's `aud` must match any configured
  audience;
- requires and validates `exp`, applying `clock-skew-seconds` of `leeway` to
  `iat` / `nbf` / `exp` (default 60s, matching Spring Security's
  `JwtTimestampValidator`).

A failed check raises `SecurityException(code="INVALID_TOKEN")`.

### OIDC discovery (issuer-uri)

Instead of hard-coding a `jwks-uri`, set `issuer-uri` and PyFly performs OIDC
discovery at startup: it GETs `<issuer-uri>/.well-known/openid-configuration` and
reads `jwks_uri` and the authoritative `issuer` from the document. The discovered
`issuer` is what the validator enforces against the token's `iss` claim. This is
the `discover_oidc(issuer_uri)` helper, which returns `(jwks_uri, issuer)` and
raises `SecurityException(code="OIDC_DISCOVERY_FAILED")` if the document cannot be
fetched or lacks `jwks_uri`.

```python
from pyfly.security.oauth2 import discover_oidc

jwks_uri, issuer = discover_oidc("https://accounts.google.com")
# ("https://www.googleapis.com/oauth2/v3/certs", "https://accounts.google.com")
```

### Multi-IdP claim mapping (ClaimMappings)

`ClaimMappings` is a frozen dataclass that drives how a validated payload becomes
a `SecurityContext`. Claim names support **dotted paths** (`realm_access.roles`)
and a single-level `*` **wildcard** that iterates every key at that level
(`resource_access.*.roles`). Paths split on `.` only, so colon-bearing claims like
`cognito:groups` match verbatim. This is what makes the resource server work with
Keycloak, Entra ID (v1.0 + v2.0) and Cognito with zero subclassing.

| Field | Config key | Default | Maps to |
|---|---|---|---|
| `principal_claims` | `principal-claim-names` | `("oid", "sub")` | `SecurityContext.user_id` (first non-empty wins) |
| `authority_claims` | `authorities-claim-names` | `roles`, `scopes`, `authorities`, `realm_access.roles`, `resource_access.*.roles`, `groups`, `cognito:groups` | `SecurityContext.roles` (collected across all paths, de-duplicated) |
| `scope_claims` | `scope-claim-names` | `("scp", "scope")` | `SecurityContext.permissions` (space-delimited strings split into individual scopes) |
| `authority_prefix` | `authority-prefix` | `""` | prepended to every authority (e.g. `ROLE_` / `SCOPE_` for Spring-style) |
| `attribute_claims` | `attribute-claims` | `()` | copied verbatim (string-coerced) into `SecurityContext.attributes` |

Per-IdP quick reference:

| IdP | `issuer` | Roles claim(s) | Scopes | Audience |
|---|---|---|---|---|
| **Keycloak** | `https://<host>/realms/<r>` | `realm_access.roles`, `resource_access.*.roles` | `scope` | client / `account` |
| **Entra ID v2.0** | `https://login.microsoftonline.com/<tid>/v2.0` | `roles`, `groups` | `scp` | `api://…` or client GUID |
| **Cognito (access)** | `https://cognito-idp.<region>.amazonaws.com/<pool>` | `cognito:groups` | `scope` | **none** → set `validate-audience: false` |

An application that needs bespoke mapping can subclass `JWKSTokenValidator` and
override `_build_context`, registering it as a bean — the auto-config backs off
via `@conditional_on_missing_bean(JWKSTokenValidator)`.

### Error modes

`authenticate-error-mode` governs what happens when a token is present but fails
validation:

| Mode | Missing token | Present but invalid token |
|---|---|---|
| `anonymous` (default) | anonymous `SecurityContext`, request proceeds — the `HttpSecurity` gate / `@pre_authorize` decides | anonymous `SecurityContext`, request proceeds (the gate decides) |
| `401` | falls through to the gate (public endpoints stay reachable) | rejected at the filter with `401 Unauthorized` + `WWW-Authenticate: Bearer error="invalid_token"` (RFC 6750) |

The `anonymous` default keeps the resource-server filter composable with
permit-all public endpoints; choose `401` when every protected route is a pure
API that should reject bad credentials immediately.

### Resource-server configuration reference

All keys nest under `pyfly.security.oauth2.resource-server`:

| Key | Default | Meaning |
|---|---|---|
| `enabled` | `false` | Activate the resource server |
| `jwks-uri` | `""` | JWKS endpoint URL (skip when using `issuer-uri`) |
| `issuer-uri` | `""` | OIDC discovery base — derives `jwks-uri` + `issuer` |
| `issuer` | `""` | Expected `iss` claim (validated when set) |
| `audiences` | `""` | Comma-separated accepted audiences; `aud` must match any |
| `validate-audience` | `true` | Set `false` to skip `aud` validation (Cognito access tokens) |
| `algorithms` | `RS256` | Comma-separated allowed signing algorithms |
| `clock-skew-seconds` | `60` | Leeway for `iat` / `nbf` / `exp` |
| `jwks-timeout-seconds` | `30` | HTTP timeout for JWKS fetches |
| `jwks-cache-seconds` | `300` | JWK-set cache lifespan |
| `principal-claim-names` | `oid,sub` | Principal (user id) claim search order |
| `authorities-claim-names` | `roles,scopes,authorities,realm_access.roles,resource_access.*.roles,groups,cognito:groups` | Authority/role claim paths |
| `authority-prefix` | `""` | Prefix applied to each authority |
| `scope-claim-names` | `scp,scope` | Scope/permission claim names |
| `attribute-claims` | `""` | Claims copied verbatim into `attributes` |
| `enforce-sender-constraints` | `false` | Require DPoP/mTLS proof when a token carries `cnf` (see [Sender-Constrained Tokens](#sender-constrained-tokens-dpop-mtls)) |
| `mtls-cert-header` | `x-client-cert` | Header carrying the client certificate (mTLS) |
| `exclude-patterns` | `""` | Comma-separated fnmatch globs skipped by the filter |
| `authenticate-error-mode` | `anonymous` | `anonymous` or `401` |

### Programmatic use

```python
from pyfly.security.oauth2 import JWKSTokenValidator, ClaimMappings

validator = JWKSTokenValidator(
    jwks_uri="https://auth.example.com/.well-known/jwks.json",
    issuer="https://auth.example.com",
    audiences=["my-api"],
    algorithms=["RS256"],
    leeway=60,
    claim_mappings=ClaimMappings(attribute_claims=("tid",)),
)

ctx = validator.to_security_context(token)
# SecurityContext(user_id=..., roles=[...], permissions=[...], attributes={...})

# Validate once and get both raw claims and the context (e.g. to inspect cnf):
claims, ctx = validator.validate_and_context(token)
```

**Constructor parameters:**

| Parameter | Type | Default | Description |
|---|---|---|---|
| `jwks_uri` | `str` | required | URL of the JWKS endpoint |
| `issuer` | `str \| None` | `None` | Expected `iss` (validated when set) |
| `audiences` | `list[str] \| None` | `None` | Accepted audiences; empty disables `aud` validation |
| `algorithms` | `list[str] \| None` | `["RS256"]` | Allowed signing algorithms |
| `leeway` | `int` | `60` | Clock-skew tolerance (seconds) for `iat` / `nbf` / `exp` |
| `validate_audience` | `bool` | `True` | Skip `aud` validation when `False` |
| `claim_mappings` | `ClaimMappings \| None` | multi-IdP defaults | Claim→context mapping |
| `jwks_timeout` | `float` | `30.0` | HTTP timeout (seconds) for JWKS fetches |
| `jwks_cache_seconds` | `int` | `300` | JWK-set cache lifespan |

### Opaque tokens (OpaqueTokenIntrospector)

For **opaque** (non-JWT) access tokens, use `OpaqueTokenIntrospector`. The
resource server posts the token — authenticated with its own client credentials —
to the authorization server's RFC 7662 `/introspect` endpoint and maps the
returned claims onto a `SecurityContext` using the same `ClaimMappings` as the JWT
validator.

```python
from pyfly.security.oauth2 import OpaqueTokenIntrospector, ClaimMappings

introspector = OpaqueTokenIntrospector(
    introspection_uri="https://auth.example.com/oauth2/introspect",
    client_id="my-resource-server",
    client_secret="rs-secret",
    claim_mappings=ClaimMappings(),
)

ctx = introspector.to_security_context(token)  # raises if the token is inactive
```

`introspect(token)` returns the raw introspection claims, or raises
`SecurityException(code="INVALID_TOKEN")` if the endpoint reports the token as not
`active` (or the request fails).

**Source:** `src/pyfly/security/oauth2/resource_server.py`,
`src/pyfly/security/oauth2/properties.py`,
`src/pyfly/web/adapters/starlette/filters/oauth2_resource_filter.py`

---

## OAuth2 Client & Login

When PyFly acts as an OAuth2 client / OIDC relying party, `OAuth2LoginHandler`
implements the browser-facing `authorization_code` flow end to end:
redirect-to-provider, callback-with-code, token exchange, identity establishment,
and logout. A `ClientRegistration` describes each provider; the
`OAuth2SessionSecurityFilter` restores the logged-in `SecurityContext` from the
HTTP session on subsequent requests (see the
[Security Guide](security.md#authentication-mechanisms)).

### The authorization_code login flow

`OAuth2LoginHandler` creates three Starlette routes:

| Route | Method | Description |
|---|---|---|
| `/oauth2/authorization/{registration_id}` | GET | Redirects the browser to the provider's authorization endpoint with `state`, `nonce` and (when applicable) the PKCE `code_challenge` |
| `/login/oauth2/code/{registration_id}` | GET | Handles the callback: validates `state` and `iss`, exchanges the code for tokens, establishes identity, rotates the session id, and stores the `SecurityContext` |
| `/logout` | POST | Invalidates the HTTP session and redirects to `/` |

The callback (`_handle_callback`) is the security-critical step. In order it:

1. validates the `state` parameter against the session value (CSRF / fixation
   defense) and consumes it (one-time use);
2. surfaces any provider `error` response as a 400;
3. validates the RFC 9207 `iss` parameter (see
   [require_iss](#rfc-9207-issuer-identification-require_iss));
4. retrieves and consumes the one-time PKCE `code_verifier`;
5. exchanges the code for tokens at `token_uri`, sending the `code_verifier`;
6. establishes identity from the verified `id_token` (preferred) or the
   `user_info_uri`;
7. **rotates the session id** (`session.rotate_id()`) to defeat session fixation,
   then stores the `SecurityContext` under the session;
8. fails with `401` if no authenticated principal could be determined (no silent
   anonymous session).

### PKCE (on by default)

PKCE (RFC 7636, S256) is **enabled by default** on the `authorization_code` flow —
`ClientRegistration.use_pkce` defaults to `True`, in line with RFC 9700 / OAuth
2.1, which require PKCE for the authorization code grant for *all* client types.
A **public client** (empty `client_secret`) **always** uses PKCE even if
`use_pkce` is explicitly disabled, because it has no other defense against
authorization-code injection. Set `use_pkce=False` only for a confidential client
talking to an AS that rejects PKCE.

When PKCE applies, the handler:

1. generates a high-entropy `code_verifier` and its SHA-256 `code_challenge`;
2. adds `code_challenge` + `code_challenge_method=S256` to the authorization
   redirect and stashes the one-time `code_verifier` in the session;
3. sends the stored `code_verifier` on the token exchange.

No extra wiring is needed — the built-in `google()`, `github()` and `keycloak()`
factories all inherit `use_pkce=True`.

### state and nonce

On every authorization request the handler generates and stores a random `state`
(32-byte URL-safe token) and a random OIDC `nonce` in the session. `state` is
validated and consumed on callback (mismatch → 400 `invalid_state`); `nonce` is
bound into the ID token and checked during [ID token validation](#id-token-validation).

### RFC 9207 issuer identification (require_iss)

[RFC 9207](https://www.rfc-editor.org/rfc/rfc9207) adds an `iss` parameter to the
authorization response to defend against mix-up attacks. PyFly always **rejects a
mismatch** between the received `iss` and the registration's `issuer_uri`. The
`require_iss` flag (default `False`) additionally makes the parameter
*mandatory* — when `True`, a provider that omits `iss` is rejected. With the
default, `iss` is validated when present but a provider that omits it is
tolerated.

### ID token validation

When the token response contains an `id_token` **and** the registration has a
`jwks_uri`, identity is taken from the verified ID token rather than userinfo. The
handler validates the ID token via a `JWKSTokenValidator` configured with the
provider's `jwks_uri`, `issuer = issuer_uri`, and `audiences = [client_id]` (an
OIDC ID token's audience is the client id), and additionally checks that the
token's `nonce` matches the session nonce. Any failure → `401 invalid_id_token`.
Otherwise identity falls back to the `user_info_uri` response.

### Built-in providers

Pre-configured factories return a ready-to-use `ClientRegistration`:

```python
from pyfly.security.oauth2 import google, github, keycloak

google_reg = google(
    client_id="...", client_secret="...",
    redirect_uri="https://myapp.com/login/oauth2/code/google",
)
github_reg = github(client_id="...", client_secret="...")
keycloak_reg = keycloak(
    client_id="...", client_secret="...",
    issuer_uri="https://keycloak.example.com/realms/myrealm",  # derives all endpoints
)
```

| Factory | `registration_id` | Scopes | Endpoints |
|---|---|---|---|
| `google()` | `google` | `openid`, `profile`, `email` | Google authorize/token/userinfo + `jwks_uri` + `issuer_uri` (full OIDC) |
| `github()` | `github` | `read:user`, `user:email` | GitHub authorize/token/userinfo (no JWKS — identity via userinfo) |
| `keycloak()` | `keycloak` | `openid`, `profile`, `email` | Derived from the realm `issuer_uri` (`/protocol/openid-connect/*`) |

All three use `authorization_grant_type="authorization_code"` and inherit
`use_pkce=True`.

### Config-driven registrations

`OAuth2ClientAutoConfiguration` builds an `InMemoryClientRegistrationRepository`
from `pyfly.security.oauth2.client.registrations.<id>` when
`pyfly.security.oauth2.client.enabled=true`. `OAuth2LoginAutoConfiguration` wires
the `OAuth2LoginHandler` and `OAuth2SessionSecurityFilter` when
`pyfly.security.oauth2.login.enabled=true`.

```yaml
pyfly:
  security:
    oauth2:
      client:
        enabled: true
        registrations:
          my-app:
            client-id: "${OAUTH_CLIENT_ID}"
            client-secret: "${OAUTH_CLIENT_SECRET}"
            authorization-grant-type: "authorization_code"
            redirect-uri: "https://myapp.com/login/oauth2/code/my-app"
            scopes: "openid,profile,email"
            authorization-uri: "https://provider.example.com/authorize"
            token-uri: "https://provider.example.com/token"
            user-info-uri: "https://provider.example.com/userinfo"
            jwks-uri: "https://provider.example.com/.well-known/jwks.json"
            issuer-uri: "https://provider.example.com"
            provider-name: "My Provider"
            use-pkce: true        # default true; opt out per registration
            require-iss: false    # RFC 9207; iss validated when present regardless
      login:
        enabled: true
```

**`ClientRegistration` fields:**

| Field | Config key | Default | Description |
|---|---|---|---|
| `registration_id` | (map key) | required | Unique registration identifier |
| `client_id` | `client-id` | required | OAuth2 client id |
| `client_secret` | `client-secret` | `""` | Client secret (empty ⇒ public client ⇒ PKCE forced) |
| `authorization_grant_type` | `authorization-grant-type` | `authorization_code` | Grant type |
| `redirect_uri` | `redirect-uri` | `""` | Callback URI |
| `scopes` | `scopes` | `[]` | Requested scopes (comma-separated or list) |
| `authorization_uri` | `authorization-uri` | `""` | Provider authorization endpoint |
| `token_uri` | `token-uri` | `""` | Provider token endpoint |
| `user_info_uri` | `user-info-uri` | `""` | Provider userinfo endpoint |
| `jwks_uri` | `jwks-uri` | `""` | Provider JWKS (enables ID-token validation) |
| `issuer_uri` | `issuer-uri` | `""` | Provider issuer (also the expected RFC 9207 `iss`) |
| `provider_name` | `provider-name` | `""` | Human-readable name |
| `use_pkce` | `use-pkce` | `True` | Enable PKCE (always forced for public clients) |
| `require_iss` | `require-iss` | `False` | Require the RFC 9207 `iss` parameter |
| `allow_introspection` | — (programmatic) | `False` | Mark a resource-server client permitted to introspect tokens it does not own (RFC 7662) |

Mount the login routes via `extra_routes`:

```python
from pyfly.web.adapters.starlette import create_app

login_handler = context.get_bean(OAuth2LoginHandler)
app = create_app(title="My App", context=context, extra_routes=login_handler.routes())
```

**Source:** `src/pyfly/security/oauth2/client.py`,
`src/pyfly/security/oauth2/login.py`

---

## Authorization Server

`AuthorizationServer` is a first-party OAuth2 authorization server that issues
JWT access tokens and opaque refresh tokens. It is auto-configured when
`pyfly.security.oauth2.authorization-server.enabled=true` and `pyjwt` is
installed (`OAuth2AuthorizationServerAutoConfiguration`), using the configured
`InMemoryClientRegistrationRepository` and the selected
[token store](#token-stores-memory-redis-postgres).

```yaml
pyfly:
  security:
    oauth2:
      authorization-server:
        enabled: true
        secret: "${OAUTH2_SIGNING_SECRET}"   # required; must be strong (see hardening)
        issuer: "https://auth.myapp.com"
        audience: "https://api.myapp.com"     # comma-separated or list; omitted when unset
        access-token-ttl: 3600                # seconds (default 1h)
        refresh-token-ttl: 86400              # seconds (default 24h)
      token-store:
        provider: memory                      # memory | redis | postgres
```

**Constructor parameters** (for programmatic construction):

| Parameter | Type | Default | Description |
|---|---|---|---|
| `secret` | `str` | required | HMAC signing key (used for `HS*` algorithms) |
| `client_repository` | `ClientRegistrationRepository` | required | Client lookup |
| `token_store` | `TokenStore` | required | Refresh-token / code / family / PAR storage |
| `access_token_ttl` | `int` | `3600` | Access token lifetime (seconds) |
| `refresh_token_ttl` | `int` | `86400` | Refresh token lifetime (seconds) |
| `issuer` | `str \| None` | `None` | `iss` claim + RFC 9207 `iss` on authorize results |
| `audience` | `str \| list[str] \| None` | `None` | `aud` claim restricting where tokens are valid |
| `algorithm` | `str` | `HS256` | JWS algorithm (`HS*` symmetric, or `RS*`/`PS*`/`ES*` asymmetric) |
| `private_key` | PEM/key object | `None` | Required for asymmetric algorithms |
| `key_id` | `str \| None` | `None` | `kid` in the JWT header and published JWK |
| `allow_dynamic_registration` | `bool` | `False` | Enable RFC 7591 dynamic client registration |
| `registration_access_token` | `str \| None` | `None` | Required initial access token for registration |
| `auth_code_ttl` | `int` | `60` | Authorization-code lifetime (seconds) |

> The config-driven auto-configuration always builds an **HS256** server. To use
> asymmetric signing, dynamic registration, or a custom auth-code TTL, construct
> `AuthorizationServer` yourself and register it as a bean —
> `@conditional_on_missing_bean(AuthorizationServer)` backs the default off.

### Grant types

`AuthorizationServer.token(...)` dispatches on `grant_type`. Client
authentication is enforced first: a **confidential** client (one with a
registered secret) MUST present it (constant-time comparison); a **public**
client (no secret) is permitted only for the `authorization_code` grant, where
PKCE provides proof of possession. An unsupported grant raises
`UNSUPPORTED_GRANT_TYPE` (there is no implicit grant and no resource-owner
password grant).

### client_credentials

Machine-to-machine. The client must be registered for the `client_credentials`
grant (`authorization_grant_type == "client_credentials"`) or the request is
rejected with `UNAUTHORIZED_CLIENT` — preventing grant-type confusion. Requested
scopes must be a subset of the client's registered scopes; an unregistered scope
is rejected wholesale with `INVALID_SCOPE` (a client can never mint a more
privileged token just by asking).

```python
response = await auth_server.token(
    grant_type="client_credentials",
    client_id="my-service",
    client_secret="service-secret",
    scope="read write",
)
# {"access_token": "...", "token_type": "Bearer", "expires_in": 3600,
#  "refresh_token": "...", "scope": "read write"}
```

### refresh_token (rotation + reuse detection)

Refresh tokens are **rotated** on every use: the presented token is marked
consumed (but retained), and a new refresh token is issued in the same *family*.
PyFly implements full **reuse detection** per RFC 9700 / OAuth 2.1: presenting an
already-rotated (used) refresh token is treated as theft and **revokes the entire
token family** (`INVALID_GRANT`). A token whose family was already revoked is
likewise refused.

```python
new_response = await auth_server.token(
    grant_type="refresh_token",
    client_id="my-service",
    client_secret="service-secret",
    refresh_token=response["refresh_token"],
)
```

### authorization_code + PKCE

The authorization code grant is split across `authorize()` (issues the code) and
`token()` (redeems it). `authorize()` enforces the OAuth 2.1 / RFC 9700 hard
requirements:

- **exact redirect-URI match** — `redirect_uri` must equal the registration's
  exactly, or `INVALID_REDIRECT_URI` (never redirected back to the client);
- only `response_type=code` (`UNSUPPORTED_RESPONSE_TYPE` otherwise — no implicit);
- requested scopes must be a subset of the registration's (`INVALID_SCOPE`);
- **mandatory PKCE** — a `code_challenge` is required and the method must be
  `S256` (`INVALID_REQUEST` otherwise).

```python
result = await auth_server.authorize(
    client_id="web-app",
    redirect_uri="https://app.example.com/callback",
    user_id="alice",                  # already-authenticated resource owner
    scope="openid profile",
    state="xyz",
    code_challenge="<S256-challenge>",
    code_challenge_method="S256",
    nonce="<nonce>",
)
# {"code": "...", "redirect_uri": "...", "state": "xyz", "iss": "https://auth.myapp.com"}

tokens = await auth_server.token(
    grant_type="authorization_code",
    client_id="web-app",
    client_secret="web-secret",
    code=result["code"],
    redirect_uri="https://app.example.com/callback",
    code_verifier="<verifier>",
)
```

The code is **single-use**: redeeming a code marks it consumed and remembers the
refresh token it issued. Replaying a used code is treated as injection — any
refresh token already issued from it is revoked and the request fails with
`INVALID_GRANT`. The code also expires after `auth_code_ttl` (default 60s), is
bound to the issuing client, and PKCE verification (`S256(code_verifier) ==
code_challenge`) is mandatory.

### OIDC ID tokens

When the redeemed code's scope contains `openid`, the token response also
includes an OIDC `id_token` (`aud = client_id`, with `iss`, `iat`, `exp`, and the
`nonce` captured at authorization time), signed with the server's algorithm.

### Symmetric vs asymmetric signing

By default the server signs with **HS256** using `secret`. For asymmetric signing
(so resource servers can verify tokens via a public JWKS), construct it with an
asymmetric `algorithm` and a `private_key`:

```python
from pyfly.security.oauth2 import AuthorizationServer, InMemoryTokenStore

auth_server = AuthorizationServer(
    secret="unused-for-asymmetric",
    client_repository=client_repo,
    token_store=InMemoryTokenStore(),
    issuer="https://auth.myapp.com",
    algorithm="RS256",                 # or RS384/RS512, PS256/.., ES256/ES384/ES512
    private_key=pem_private_key,        # PEM str/bytes or a cryptography key object
    key_id="key-1",
)

auth_server.jwks()
# {"keys": [{"kty": "RSA", "use": "sig", "alg": "RS256", "kid": "key-1", ...}]}
```

`jwks()` returns the public JWK Set for asymmetric algorithms (the `kid` is
included when set) and `{"keys": []}` for HMAC. The `/oauth2/jwks` endpoint
serves this document.

### HTTP endpoints (AuthorizationServerEndpoints)

`AuthorizationServerEndpoints(server, login_url="/login")` exposes the server as
Starlette routes. Mount them via `extra_routes`:

```python
from pyfly.security.oauth2 import AuthorizationServerEndpoints

endpoints = AuthorizationServerEndpoints(auth_server, login_url="/login")
app = create_app(title="Auth Server", context=context, extra_routes=endpoints.routes())
```

| Route | Method | RFC | Purpose |
|---|---|---|---|
| `/oauth2/authorize` | GET | 6749 §4.1 | Authorization endpoint; bounces to `login_url` if the resource owner is not authenticated, then issues a code (resolving PAR `request_uri` / JAR `request` first) |
| `/oauth2/par` | POST | 9126 | Pushed Authorization Request; client-authenticated; returns a one-time `request_uri` |
| `/oauth2/token` | POST | 6749 §3.2 | Token endpoint; client credentials via HTTP Basic or form params; binds DPoP `cnf.jkt` when a `DPoP` proof header is present |
| `/oauth2/introspect` | POST | 7662 | Token introspection; client-authenticated |
| `/oauth2/revoke` | POST | 7009 | Token revocation; client-authenticated; always responds `200` |
| `/oauth2/register` | POST | 7591 | Dynamic client registration |
| `/oauth2/jwks` | GET | 7517 | Public JWK Set (asymmetric signing) |
| `/.well-known/oauth-authorization-server` | GET | 8414 | Authorization server metadata |
| `/.well-known/openid-configuration` | GET | — | OIDC discovery document |

Authorization-endpoint errors that may **not** be redirected to the client
(`INVALID_CLIENT`, `INVALID_REDIRECT_URI`) are returned as a 400 directly; safe
errors (`INVALID_SCOPE`, `UNSUPPORTED_RESPONSE_TYPE`, `INVALID_REQUEST`) are
redirected back as `error` parameters (with `state` echoed). Token/management
errors map to a 400, except `INVALID_CLIENT`, which is a `401` with
`WWW-Authenticate: Basic realm="oauth2"`.

### Dynamic Client Registration (RFC 7591)

`POST /oauth2/register` registers a client at runtime. It requires the server to
be built with `allow_dynamic_registration=True` and a repository that supports
`add()` (e.g. `InMemoryClientRegistrationRepository`); otherwise `register_client`
raises `REGISTRATION_DISABLED` / `REGISTRATION_UNSUPPORTED` (returned as 403). The
server generates the `client_id` and `client_secret` and returns RFC 7591
metadata. If a `registration_access_token` is configured, the request MUST present
it as a bearer token (RFC 7591 §3) or it is rejected with `401`.

```python
auth_server = AuthorizationServer(
    secret="...", client_repository=repo, token_store=InMemoryTokenStore(),
    allow_dynamic_registration=True,
    registration_access_token="initial-access-token",  # optional gate
)
```

### PAR (RFC 9126) and JAR (RFC 9101)

- **PAR** — a client POSTs its authorization parameters to `/oauth2/par`
  (client-authenticated) and receives a one-time `request_uri`
  (`urn:ietf:params:oauth:request_uri:...`, 90-second TTL). It then calls
  `/oauth2/authorize?request_uri=...`; the server consumes the stored params
  (one-time use, bound to the client) and proceeds. This keeps authorization
  parameters off the front channel.
- **JAR** — alternatively the client passes a signed `request` object (a JWT
  signed with its client secret, HS256) to `/oauth2/authorize`. The server
  verifies it via `verify_request_object` (confidential clients only) and merges
  its claims into the request parameters.

### Introspection (RFC 7662) & revocation (RFC 7009)

`/oauth2/introspect` reports whether a token is `active`. Access tokens are
self-contained JWTs (signature-verified); refresh tokens are looked up in the
store and are active only if present, unused, unexpired, and their family is still
active. A client may introspect only **its own** tokens unless its registration
sets `allow_introspection=True` — so one client cannot scan another's tokens
(`introspect(..., allow_any_client=...)`).

`/oauth2/revoke` revokes a refresh token and, when known, its whole rotation
family. Per RFC 7009 §2.1 only the owning client may revoke a token; per §2.2 the
endpoint always returns `200` regardless of whether the token existed.

### Metadata & discovery (RFC 8414 + OIDC)

`/.well-known/oauth-authorization-server` and `/.well-known/openid-configuration`
publish the server metadata, including:

- `response_types_supported`: `["code"]` (no implicit);
- `grant_types_supported`: `["authorization_code", "client_credentials", "refresh_token"]`;
- `token_endpoint_auth_methods_supported`: `["client_secret_basic", "client_secret_post", "none"]`;
- `code_challenge_methods_supported`: `["S256"]`;
- the OIDC document additionally advertises `id_token_signing_alg_values_supported`
  (the server's signing algorithm), `subject_types_supported`, `scopes_supported`
  and `claims_supported`.

### Token stores (memory / redis / postgres)

The `TokenStore` port persists refresh tokens, authorization codes, rotation
families and PAR requests:

```python
class TokenStore(Protocol):
    async def store(self, token_id: str, token_data: dict[str, Any]) -> None: ...
    async def find(self, token_id: str) -> dict[str, Any] | None: ...
    async def revoke(self, token_id: str) -> None: ...
```

`OAuth2AuthorizationServerAutoConfiguration._build_token_store()` selects the
backend from `pyfly.security.oauth2.token-store.provider` (case-insensitive):

| Provider | Adapter | Persistence | When to use |
|---|---|---|---|
| `memory` (default) | `InMemoryTokenStore` | Process-local; **lost on restart**, not shared across instances | Development / testing, single instance |
| `redis` | `RedisTokenStore` (`pyfly.security.adapters.redis_token_store`) | Cross-instance, fast distributed revocation; tokens self-evict at `refresh-token-ttl` | Multi-instance servers wanting fast revocation |
| `postgres` | `PostgresTokenStore` (`pyfly.security.adapters.postgres_token_store`) | Durable + auditable in a SQL table | Multi-instance servers needing durable, auditable storage |

```yaml
pyfly:
  security:
    oauth2:
      authorization-server:
        enabled: true
        secret: "${OAUTH2_SECRET}"
        refresh-token-ttl: 86400          # also the Redis token TTL
      token-store:
        provider: redis                   # memory (default) | redis | postgres
        redis:
          url: "redis://localhost:6379/0" # falls back to pyfly.session.redis.url
```

The Redis adapter is wired only when the `redis.asyncio` driver is available (it
falls back to `InMemoryTokenStore` otherwise); the Postgres adapter resolves a
SQLAlchemy `AsyncEngine` bean from the container. Both are hexagonal — the
client/engine is injected by the composition root, never imported at module
scope.

### Signing-secret hardening

The authorization server refuses to start with an insecure signing secret. At the
composition root, `_resolve_signing_secret` reads
`pyfly.security.oauth2.authorization-server.secret` and:

- raises `SecurityException(code="INSECURE_SIGNING_SECRET")` if the secret is unset
  (i.e. the built-in placeholder `change-me-in-production` would be used);
- raises `SecurityException(code="WEAK_SIGNING_SECRET")` if, for an HMAC (`HS*`)
  algorithm, the key is shorter than 32 bytes (RFC 7518 §3.2 requires a key at
  least as long as the hash output — 256 bits for HS256).

Generate a strong value:

```bash
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

The same hardening guards the symmetric `JWTService` secret, but **only** when
`pyfly.security.jwt.filter.enabled=true` — a resource-server-only app (which
verifies via JWKS and never signs symmetric tokens) is not forced to invent a
secret.

**Error codes** (raised as `SecurityException`, mapped to OAuth2 errors at the
endpoint layer):

| Code | Cause |
|---|---|
| `INVALID_CLIENT` | Unknown client id or wrong secret (→ 401 at the endpoint) |
| `INVALID_REQUEST` | Missing/invalid parameter (e.g. no refresh token, missing PKCE challenge) |
| `INVALID_GRANT` | Invalid/expired/replayed code or refresh token; family revoked |
| `INVALID_SCOPE` | Requested scope not registered for the client |
| `INVALID_REDIRECT_URI` | `redirect_uri` does not exactly match the registration |
| `UNAUTHORIZED_CLIENT` | Client not authorized for the requested grant |
| `UNSUPPORTED_GRANT_TYPE` | Grant type not supported |
| `UNSUPPORTED_RESPONSE_TYPE` | `response_type` other than `code` |
| `REGISTRATION_DISABLED` / `REGISTRATION_UNSUPPORTED` | Dynamic registration off / repository cannot add |

**Source:** `src/pyfly/security/oauth2/authorization_server.py`,
`src/pyfly/security/oauth2/endpoints.py`,
`src/pyfly/security/auto_configuration.py`

---

## Sender-Constrained Tokens (DPoP & mTLS)

A plain bearer token can be replayed by anyone who steals it. Sender-constraining
binds the token to a key the legitimate client holds, so a stolen token alone is
useless. PyFly supports both standard mechanisms: **DPoP** (RFC 9449) and **mTLS**
(RFC 8705), via a `cnf` (confirmation) claim on the access token.

### DPoP (RFC 9449)

The client signs a per-request *proof* JWT (`typ: dpop+jwt`) with its private key
and sends it in a `DPoP` header. When the client presents a DPoP proof on the
token request, the authorization server's `/oauth2/token` endpoint validates it
and binds the issued access token to the client's key via `cnf.jkt` — the RFC 7638
SHA-256 JWK thumbprint.

`DPoPProofValidator.validate(proof, http_method=..., http_url=..., access_token=...)`
verifies the proof and returns the bound thumbprint, checking that:

- `typ` is `dpop+jwt` and `alg` is asymmetric (`RS*`/`ES*`/`PS*`/`Ed*`);
- the embedded `jwk` is present and contains **no** private material;
- the signature verifies against that `jwk`;
- `htm` matches the request method and `htu` matches the request URL (normalized
  to `scheme://host/path`);
- `iat` is within `max_age_seconds` (default 60);
- `jti` is unseen, when a `replay_cache` is supplied;
- `ath` equals `base64url(SHA-256(access_token))`, when an access token is given.

### mTLS (RFC 8705)

The access token carries `cnf["x5t#S256"]`, the SHA-256 thumbprint of the client
certificate. The resource server compares it to the certificate the client
presents (forwarded by the TLS-terminating proxy in the configured header).

### Resource-server enforcement

The resource server enforces proof-of-possession when **both**
`pyfly.security.oauth2.resource-server.enforce-sender-constraints=true` **and** the
validated token actually carries a `cnf` claim. Plain bearer tokens (no `cnf`) are
unaffected, so enabling enforcement is non-breaking for callers that present
ordinary tokens.

```yaml
pyfly:
  security:
    oauth2:
      resource-server:
        enabled: true
        issuer-uri: "https://auth.myapp.com"
        enforce-sender-constraints: true
        mtls-cert-header: "x-client-cert"   # header carrying the client cert (URL-decoded)
```

On a `cnf.jkt` token the filter requires a `DPoP` header, validates the proof
(method, URL, `ath`) and asserts the proof's thumbprint equals `cnf.jkt`
(`confirm_dpop_binding`). On a `cnf["x5t#S256"]` token it requires the
`mtls-cert-header`, URL-decodes it, and asserts the certificate thumbprint equals
`cnf["x5t#S256"]` (`confirm_mtls_binding`). A missing proof/certificate, or a
mismatch, fails as `INVALID_TOKEN` (rejected outright in `401` error mode, or
yielding an anonymous context in `anonymous` mode).

### DPoP / mTLS helpers

`pyfly.security.oauth2.dpop` exposes the building blocks:

| Symbol | Purpose |
|---|---|
| `DPoPProofValidator` | Validate a DPoP proof JWT (with optional `jti` replay cache) and return its `jkt` |
| `jwk_thumbprint(jwk)` | RFC 7638 JWK SHA-256 thumbprint (RSA/EC/OKP) |
| `access_token_hash(token)` | The DPoP `ath` value: `base64url(SHA-256(token))` |
| `confirm_dpop_binding(claims, jkt)` | Assert the token's `cnf.jkt` matches `jkt` |
| `certificate_thumbprint(cert)` | RFC 8705 `x5t#S256` thumbprint of a PEM/DER cert |
| `confirm_mtls_binding(claims, cert)` | Assert the token's `cnf["x5t#S256"]` matches the cert |

**Source:** `src/pyfly/security/oauth2/dpop.py`,
`src/pyfly/web/adapters/starlette/filters/oauth2_resource_filter.py`

---

## Standards & Compliance

PyFly's OAuth2 defaults follow [RFC 9700](https://www.rfc-editor.org/rfc/rfc9700)
(OAuth 2.0 Security Best Current Practice) and OAuth 2.1.

| Requirement | How PyFly satisfies it |
|---|---|
| **PKCE for the authorization code grant** | AS `authorize()` *requires* a `code_challenge` and only accepts `S256`. The OAuth2 client enables PKCE by default (`use_pkce=True`) and **forces** it for public clients regardless of the flag. |
| **Exact redirect-URI matching** | `authorize()` rejects any `redirect_uri` that is not character-for-character equal to the registration (`INVALID_REDIRECT_URI`, never redirected). |
| **No implicit grant** | Only `response_type=code` is accepted (`UNSUPPORTED_RESPONSE_TYPE`); metadata advertises `response_types_supported: ["code"]`. |
| **No resource-owner password (ROPC)** | The AS implements no password grant. The IdP module's ROPC (`grant_type=password`) against keycloak/cognito/azure-ad is disabled unless `pyfly.idp.allow-password-grant=true`. |
| **Refresh-token rotation** | Every refresh use rotates the token within its family. |
| **Refresh-token reuse / replay detection** | Replaying a used refresh token revokes the entire token family (`INVALID_GRANT`). |
| **Authorization-code single use + injection defense** | Codes are single-use; replaying a used code revokes any refresh token already issued from it. |
| **Sender-constrained tokens** | DPoP (`cnf.jkt`, RFC 9449) and mTLS (`cnf["x5t#S256"]`, RFC 8705), enforced by the resource server when `enforce-sender-constraints` is on. |
| **Audience restriction** | The AS emits an `aud` claim when `audience` is set; the resource server validates `aud` against its configured `audiences`. |
| **Issuer identification (mix-up defense)** | RFC 9207 `iss` on authorize responses; the client validates `iss` (mandatory with `require_iss`); the resource server validates the token `iss`. |
| **Client authentication** | Constant-time secret comparison; empty credentials never authenticate; public clients are restricted to the `authorization_code` grant. |
| **Strong token-signing keys** | Startup fails on the placeholder secret or an HMAC key shorter than 32 bytes (RFC 7518 §3.2). |
| **PAR / JAR** | Pushed Authorization Requests (RFC 9126) and signed request objects (RFC 9101) keep authorization parameters off the front channel. |

---

## Configuration Reference

Every key below nests under `pyfly:`. Defaults reflect the source.

**Resource server** — `pyfly.security.oauth2.resource-server.*`

| Key | Default | Meaning |
|---|---|---|
| `enabled` | `false` | Activate the resource server |
| `jwks-uri` | `""` | JWKS endpoint URL |
| `issuer-uri` | `""` | OIDC discovery base (derives `jwks-uri` + `issuer`) |
| `issuer` | `""` | Expected `iss` claim |
| `audiences` | `""` | Comma-separated accepted audiences |
| `validate-audience` | `true` | Skip `aud` validation when `false` |
| `algorithms` | `RS256` | Allowed signing algorithms |
| `clock-skew-seconds` | `60` | Leeway for `iat`/`nbf`/`exp` |
| `jwks-timeout-seconds` | `30` | JWKS fetch HTTP timeout |
| `jwks-cache-seconds` | `300` | JWK-set cache lifespan |
| `principal-claim-names` | `oid,sub` | Principal claim search order |
| `authorities-claim-names` | `roles,scopes,authorities,realm_access.roles,resource_access.*.roles,groups,cognito:groups` | Authority/role claim paths |
| `authority-prefix` | `""` | Prefix applied to each authority |
| `scope-claim-names` | `scp,scope` | Scope/permission claim names |
| `attribute-claims` | `""` | Claims copied into `attributes` |
| `enforce-sender-constraints` | `false` | Require DPoP/mTLS proof for `cnf` tokens |
| `mtls-cert-header` | `x-client-cert` | Header carrying the client certificate |
| `exclude-patterns` | `""` | Fnmatch globs skipped by the filter |
| `authenticate-error-mode` | `anonymous` | `anonymous` or `401` |

**Client & login** — `pyfly.security.oauth2.client.*` / `pyfly.security.oauth2.login.*`

| Key | Default | Meaning |
|---|---|---|
| `client.enabled` | `false` | Build the client registration repository |
| `client.registrations.<id>.client-id` | — | OAuth2 client id |
| `client.registrations.<id>.client-secret` | `""` | Client secret (empty ⇒ public client) |
| `client.registrations.<id>.authorization-grant-type` | `authorization_code` | Grant type |
| `client.registrations.<id>.redirect-uri` | `""` | Callback URI |
| `client.registrations.<id>.scopes` | `""` | Requested scopes (comma-separated or list) |
| `client.registrations.<id>.authorization-uri` | `""` | Provider authorization endpoint |
| `client.registrations.<id>.token-uri` | `""` | Provider token endpoint |
| `client.registrations.<id>.user-info-uri` | `""` | Provider userinfo endpoint |
| `client.registrations.<id>.jwks-uri` | `""` | Provider JWKS (enables ID-token validation) |
| `client.registrations.<id>.issuer-uri` | `""` | Provider issuer / expected RFC 9207 `iss` |
| `client.registrations.<id>.provider-name` | `""` | Human-readable name |
| `client.registrations.<id>.use-pkce` | `true` | Enable PKCE (forced for public clients) |
| `client.registrations.<id>.require-iss` | `false` | Require the RFC 9207 `iss` parameter |
| `login.enabled` | `false` | Wire `OAuth2LoginHandler` + `OAuth2SessionSecurityFilter` |

**Authorization server** — `pyfly.security.oauth2.authorization-server.*` / `pyfly.security.oauth2.token-store.*`

| Key | Default | Meaning |
|---|---|---|
| `authorization-server.enabled` | `false` | Activate the authorization server |
| `authorization-server.secret` | (none — required) | HMAC signing secret (hardened at startup) |
| `authorization-server.issuer` | (unset) | `iss` claim + RFC 9207 `iss` |
| `authorization-server.audience` | (unset) | `aud` claim (comma-separated or list) |
| `authorization-server.access-token-ttl` | `3600` | Access token lifetime (seconds) |
| `authorization-server.refresh-token-ttl` | `86400` | Refresh token lifetime (seconds) |
| `token-store.provider` | `memory` | `memory`, `redis`, or `postgres` |
| `token-store.redis.url` | falls back to `pyfly.session.redis.url`, then `redis://localhost:6379/0` | Redis URL (redis provider) |

**IdP module** — `pyfly.idp.*` (external identity providers; see ROPC note above)

| Key | Default | Meaning |
|---|---|---|
| `enabled` | `false` | Activate the IdP module |
| `provider` | `internal-db` | `internal-db`, `keycloak`, `cognito`, or `azure-ad` |
| `allow-password-grant` | `false` | Permit ROPC (`grant_type=password`) against an external IdP |
| `keycloak.base-url` / `keycloak.realm` / `keycloak.client-id` / `keycloak.client-secret` | `""` | Keycloak connection |
| `cognito.user-pool-id` / `cognito.client-id` / `cognito.region` / `cognito.client-secret` | `""` | AWS Cognito connection |
| `azure.tenant-id` / `azure.client-id` / `azure.client-secret` | `""` | Microsoft Entra (Azure AD) connection |

---

**See also:** [Security Guide](security.md) for `SecurityContext`,
`HttpSecurity`, method-level security, CSRF, and password encoding.
