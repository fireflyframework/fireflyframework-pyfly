# Security Guide

The PyFly security module is a full Spring-Security-style stack for async Python: a request-scoped `SecurityContext`, URL-level (`HttpSecurity`) and method-level (`@pre_authorize`/`@post_authorize`/`@pre_filter`/`@post_filter`) authorization, pluggable authentication mechanisms (form login, HTTP Basic, X.509, a `UserDetailsService`/`AuthenticationManager` SPI, run-as), password encoders (bcrypt, PBKDF2, scrypt, Argon2 behind a delegating encoder), CSRF protection, security headers, and a complete OAuth 2.1 / OpenID Connect implementation (resource server, client & login, and a full authorization server). Like all PyFly modules it follows hexagonal principles, with ports (protocols) and swappable adapters, and a `SecurityContext` that is a plain dataclass with no framework coupling.

> The OAuth 2.1 / OIDC surface (resource server, client/login, authorization server, DPoP/mTLS, dynamic client registration, PAR/JAR, introspection, discovery) is large enough to have its own page — see the **[OAuth 2.1 & OpenID Connect guide](oauth2.md)**.

---

## Table of Contents

- [Architecture Overview](#architecture-overview)
- [SecurityContext](#securitycontext)
  - [Creating a SecurityContext](#creating-a-securitycontext)
  - [Authentication Check](#authentication-check)
  - [Role Checking](#role-checking)
  - [Permission Checking](#permission-checking)
  - [Anonymous Context](#anonymous-context)
  - [Full API Reference](#securitycontext-api-reference)
- [JWT Authentication](#jwt-authentication)
  - [JWTService](#jwtservice)
  - [Encoding Tokens](#encoding-tokens)
  - [Decoding Tokens](#decoding-tokens)
  - [Token-to-SecurityContext Conversion](#token-to-securitycontext-conversion)
  - [Token Payload Convention](#token-payload-convention)
  - [Error Handling](#jwt-error-handling)
- [Password Encoding](#password-encoding)
  - [PasswordEncoder Protocol](#passwordencoder-protocol)
  - [BcryptPasswordEncoder](#bcryptpasswordencoder)
  - [Custom Password Encoders](#custom-password-encoders)
  - [Delegating & Modern Encoders](#delegating-modern-encoders)
- [SecurityMiddleware](#securitymiddleware)
  - [How It Works](#how-the-middleware-works)
  - [Excluding Paths](#excluding-paths)
  - [Integration with create_app()](#integration-with-create_app)
- [The @secure Decorator](#the-secure-decorator)
  - [Role-Based Access Control](#role-based-access-control)
  - [Permission-Based Access Control](#permission-based-access-control)
  - [Combined Role and Permission Checks](#combined-role-and-permission-checks)
  - [How @secure Works Internally](#how-secure-works-internally)
  - [Error Responses](#secure-error-responses)
  - [Expression-Based Access Control](#expression-based-access-control)
- [CSRF Protection](#csrf-protection)
  - [How Double-Submit Cookie Works](#how-double-submit-cookie-works)
  - [CSRF Utilities](#csrf-utilities)
  - [CsrfFilter](#csrffilter)
  - [JavaScript Integration](#javascript-integration)
- [HttpSecurity DSL](#httpsecurity-dsl)
  - [Building URL-Level Access Rules](#building-url-level-access-rules)
  - [Access Rule Types](#access-rule-types)
  - [HttpSecurityFilter](#httpsecurityfilter)
  - [Integration with create_app()](#integration-with-create_app_1)
- [Method-Level Security](#method-level-security)
  - [@pre_authorize](#pre_authorize-check-before-execution)
  - [@post_authorize](#post_authorize-check-after-execution)
  - [Expression Vocabulary](#expression-vocabulary)
  - [Method Arguments and returnObject](#method-arguments-and-returnobject)
  - [Role Hierarchy](#role-hierarchy)
- [Authentication Mechanisms](#authentication-mechanisms)
  - [UserDetailsService SPI](#userdetails-and-the-userdetailsservice-spi)
  - [AuthenticationManager](#authenticationmanager-providermanager-and-daoauthenticationprovider)
  - [Form Login](#form-login)
  - [HTTP Basic](#http-basic)
  - [X.509 Client Certificates](#x509-client-certificate-authentication)
  - [Logout](#logout)
  - [switch-user / run-as](#switch-user-run-as-impersonation)
- [Security Headers](#security-headers)
- [OAuth 2.1 & OpenID Connect](#oauth-21-openid-connect) — see the [OAuth2 guide](oauth2.md)
- [Secure-by-Default & Hardening](#secure-by-default-hardening)
- [Exception Hierarchy](#exception-hierarchy)
- [Auto-Configuration](#auto-configuration)
- [Putting It All Together](#putting-it-all-together)
  - [Configuration Layer](#configuration-layer)
  - [User Entity and Repository](#user-entity-and-repository)
  - [Authentication Service](#authentication-service)
  - [Auth Controller: Login and Register](#auth-controller-login-and-register)
  - [Protected Controller: Role-Based Endpoints](#protected-controller-role-based-endpoints)
  - [Application Assembly](#application-assembly)
  - [Testing the Flow](#testing-the-flow)

---

## Architecture Overview

The security module consists of the following components:

| Component              | File                          | Purpose                                        |
|------------------------|-------------------------------|------------------------------------------------|
| `SecurityContext`      | `pyfly.security.context`      | Immutable dataclass holding auth/authz data     |
| `JWTService`           | `pyfly.security.jwt`          | Encode, decode, and validate JWT tokens         |
| `PasswordEncoder`      | `pyfly.security.password`     | Protocol for password hashing                   |
| `BcryptPasswordEncoder`| `pyfly.security.password`     | Bcrypt implementation of PasswordEncoder        |
| `SecurityMiddleware`   | `pyfly.web.adapters.starlette.security_middleware` | Starlette middleware for token extraction (re-exported from `pyfly.security.middleware` and `pyfly.security`) |
| `@secure`              | `pyfly.security.decorators`   | Decorator for role/permission/expression enforcement |
| `CsrfFilter`          | `pyfly.web.adapters.starlette.filters.csrf_filter` | Double-submit cookie CSRF protection |
| `JWKSTokenValidator`   | `pyfly.security.oauth2.resource_server` | RS256 JWT validation via remote JWKS |
| `ClientRegistration`   | `pyfly.security.oauth2.client` | OAuth2 provider configuration dataclass        |
| `AuthorizationServer`  | `pyfly.security.oauth2.authorization_server` | Token issuance and refresh token management |
| `HttpSecurity`         | `pyfly.security.http_security`    | URL-level access control builder (DSL)       |
| `HttpSecurityFilter`   | `pyfly.web.adapters.starlette.filters.http_security_filter` | Evaluates HttpSecurity rules at filter layer |
| `OAuth2LoginHandler`   | `pyfly.security.oauth2.login`     | Browser-facing authorization_code login flow |
| `OAuth2SessionSecurityFilter` | `pyfly.security.oauth2.session_security_filter` | Restores SecurityContext from HTTP session |
| `UserDetailsService`   | `pyfly.security.user_details` | Credential-lookup SPI (`InMemoryUserDetailsService`, `SqlUserDetailsService`) |
| `ProviderManager` / `DaoAuthenticationProvider` | `pyfly.security.authentication` | `AuthenticationManager` SPI |
| `FormLoginFilter` / `LogoutFilter` | `pyfly.web.adapters.starlette.filters.*` | Form login + generic logout |
| `HttpBasicAuthenticationFilter` | `pyfly.web.adapters.starlette.filters.http_basic_filter` | HTTP Basic auth (RFC 7617) |
| `X509AuthenticationFilter` / `SwitchUserFilter` | `pyfly.web.adapters.starlette.filters.*` | Client-cert auth + run-as impersonation |
| `DelegatingPasswordEncoder` | `pyfly.security.password` | `{id}`-prefixed multi-algorithm encoder (bcrypt/PBKDF2/scrypt/Argon2) |
| `PermissionEvaluator`  | `pyfly.security.permission` | ACL-style `hasPermission` SPI |
| `SecurityHeadersFilter`| `pyfly.web.adapters.starlette.filters.security_headers_filter` | OWASP response headers |
| `AuthorizationServerEndpoints` | `pyfly.security.oauth2.endpoints` | OAuth2/OIDC HTTP routes (token, authorize, jwks, introspect, …) |
| `OpaqueTokenIntrospector` | `pyfly.security.oauth2.resource_server` | RFC 7662 opaque-token validation |

All components are exported from the top-level `pyfly.security` package:

```python
from pyfly.security import (
    SecurityContext,
    HttpSecurity,
    pre_authorize, post_authorize, pre_filter, post_filter, secure,
    RoleHierarchy, set_role_hierarchy, get_role_hierarchy,
    PermissionEvaluator, set_permission_evaluator, get_permission_evaluator,
    JWTService, SecurityMiddleware,
    # Password encoders
    PasswordEncoder, BcryptPasswordEncoder, Pbkdf2PasswordEncoder,
    ScryptPasswordEncoder, Argon2PasswordEncoder,
    DelegatingPasswordEncoder, create_delegating_password_encoder,
    # Authentication SPI
    UserDetails, UserDetailsService, InMemoryUserDetailsService,
    Authentication, AuthenticationProvider, DaoAuthenticationProvider, ProviderManager,
    AuthenticationException, BadCredentialsException, DisabledException, ProviderNotFoundException,
)

# CSRF utilities
from pyfly.security.csrf import generate_csrf_token, validate_csrf_token

# OAuth2 / OIDC (see the OAuth2 guide)
from pyfly.security.oauth2 import (
    JWKSTokenValidator, OpaqueTokenIntrospector, ClaimMappings,
    ClientRegistration, InMemoryClientRegistrationRepository,
    AuthorizationServer, AuthorizationServerEndpoints,
    TokenStore, InMemoryTokenStore, OAuth2LoginHandler,
    google, github, keycloak,
)
```

---

## SecurityContext

`SecurityContext` is a frozen dataclass that holds authentication and authorization data for the current request. It is the central data structure that the middleware populates and the `@secure` decorator inspects.

### Creating a SecurityContext

```python
from pyfly.security import SecurityContext

ctx = SecurityContext(
    user_id="user-123",
    roles=["ADMIN", "USER"],
    permissions=["order:read", "order:write", "order:delete"],
    attributes={"department": "engineering", "team": "platform"},
)
```

**Fields:**

| Field          | Type               | Default | Description                              |
|----------------|--------------------|---------|------------------------------------------|
| `user_id`      | `str \| None`      | `None`  | Authenticated user's identifier          |
| `roles`        | `list[str]`        | `[]`    | User's assigned roles                    |
| `permissions`  | `list[str]`        | `[]`    | User's granted permissions               |
| `attributes`   | `dict[str, str]`   | `{}`    | Additional key-value attributes          |

Because `SecurityContext` is a frozen dataclass, it is immutable once created. This prevents accidental modification during request processing.

### Authentication Check

```python
ctx = SecurityContext(user_id="user-123")
ctx.is_authenticated  # True

anon = SecurityContext()
anon.is_authenticated  # False
```

The `is_authenticated` property returns `True` if and only if `user_id` is not `None`.

### Role Checking

```python
ctx = SecurityContext(user_id="user-123", roles=["ADMIN", "USER"])

ctx.has_role("ADMIN")                       # True
ctx.has_role("MANAGER")                     # False

ctx.has_any_role(["ADMIN", "MANAGER"])      # True  (has ADMIN)
ctx.has_any_role(["MANAGER", "DIRECTOR"])   # False (has neither)
```

- `has_role(role)` -- exact match against the roles list.
- `has_any_role(roles)` -- returns `True` if the user has at least one of the given roles (set intersection).

### Permission Checking

```python
ctx = SecurityContext(
    user_id="user-123",
    permissions=["order:read", "order:write"],
)

ctx.has_permission("order:read")     # True
ctx.has_permission("order:delete")   # False
```

### Anonymous Context

```python
anon = SecurityContext.anonymous()
anon.user_id           # None
anon.roles             # []
anon.permissions       # []
anon.is_authenticated  # False
```

The `anonymous()` class method creates a context with all defaults, representing an unauthenticated user.

### SecurityContext API Reference

| Method / Property          | Return Type | Description                                     |
|----------------------------|-------------|-------------------------------------------------|
| `is_authenticated`         | `bool`      | `True` if `user_id` is not `None`               |
| `has_role(role)`           | `bool`      | `True` if the user has the specified role        |
| `has_any_role(roles)`      | `bool`      | `True` if the user has any of the given roles    |
| `has_permission(permission)` | `bool`    | `True` if the user has the specified permission  |
| `anonymous()` (classmethod)| `SecurityContext` | Create an anonymous (unauthenticated) context |

---

## JWT Authentication

### JWTService

`JWTService` handles JWT token encoding, decoding, validation, and conversion to `SecurityContext`. It wraps the PyJWT library.

```python
from pyfly.security import JWTService

jwt_service = JWTService(secret="my-secret-key", algorithm="HS256")
```

**Constructor parameters:**

| Parameter   | Type  | Default   | Description                              |
|-------------|-------|-----------|------------------------------------------|
| `secret`    | `str` | required  | Secret key for HMAC-based token signing  |
| `algorithm` | `str` | `"HS256"` | JWT algorithm (e.g., HS256, HS384, RS256)|

### Encoding Tokens

Create a JWT token from a payload dictionary:

```python
from datetime import datetime, timedelta, UTC

token = jwt_service.encode({
    "sub": "user-123",
    "roles": ["ADMIN", "USER"],
    "permissions": ["order:read", "order:write"],
    "exp": datetime.now(UTC) + timedelta(hours=1),
    "iat": datetime.now(UTC),
})
# Returns: "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9...."
```

The payload is a standard Python dictionary. PyJWT handles serialization of common types like `datetime`. You are responsible for including standard JWT claims like `exp` (expiration), `iat` (issued at), and `sub` (subject).

### Decoding Tokens

Decode and validate a JWT token:

```python
payload = jwt_service.decode(token)
# Returns: {"sub": "user-123", "roles": ["ADMIN", "USER"], ...}
```

Validation includes:
- Signature verification using the configured secret and algorithm
- Expiration check (if `exp` claim is present)
- All standard PyJWT validations

If the token is invalid, expired, or tampered with, a `SecurityException` is raised:

```python
from pyfly.kernel.exceptions import SecurityException

try:
    payload = jwt_service.decode("invalid-token")
except SecurityException as exc:
    print(exc)       # "Invalid token: ..."
    print(exc.code)  # "INVALID_TOKEN"
```

### Token-to-SecurityContext Conversion

The `to_security_context()` method is a convenience that decodes a token and builds a `SecurityContext` directly:

```python
ctx = jwt_service.to_security_context(token)
# SecurityContext(
#     user_id="user-123",
#     roles=["ADMIN", "USER"],
#     permissions=["order:read", "order:write"],
# )
```

### Token Payload Convention

`to_security_context()` extracts these claims from the JWT payload:

| JWT Claim     | SecurityContext Field | Required | Default |
|---------------|----------------------|----------|---------|
| `sub`         | `user_id`            | Yes      | --      |
| `roles`       | `roles`              | No       | `[]`    |
| `permissions` | `permissions`        | No       | `[]`    |

Any additional claims in the payload are ignored by `to_security_context()`. If you need them, decode the token manually with `decode()` and build the context yourself.

### JWT Error Handling

All PyJWT errors (`jwt.PyJWTError` and its subclasses) are caught and wrapped in a `SecurityException` with code `"INVALID_TOKEN"`:

| PyJWT Error                  | Cause                                 |
|------------------------------|---------------------------------------|
| `jwt.ExpiredSignatureError`  | Token has expired (past `exp` claim)  |
| `jwt.InvalidSignatureError`  | Signature does not match              |
| `jwt.DecodeError`            | Token is malformed                    |
| `jwt.InvalidTokenError`      | Other token validation failures       |

---

## Password Encoding

### PasswordEncoder Protocol

`PasswordEncoder` is a runtime-checkable protocol that defines the contract for password hashing:

```python
from pyfly.security import PasswordEncoder

class PasswordEncoder(Protocol):
    def hash(self, raw_password: str) -> str:
        """Hash a raw password. Returns the hashed string."""
        ...

    def verify(self, raw_password: str, hashed_password: str) -> bool:
        """Verify a raw password against a hashed password."""
        ...
```

This protocol allows you to swap out the hashing implementation (e.g., bcrypt, argon2, scrypt) without changing your service layer.

### BcryptPasswordEncoder

The default production-ready implementation using bcrypt:

```python
from pyfly.security import BcryptPasswordEncoder

encoder = BcryptPasswordEncoder(rounds=12)

# Hash a password
hashed = encoder.hash("my-secure-password")
# "$2b$12$LJ3m4ys3Lk..."

# Verify a password
encoder.verify("my-secure-password", hashed)    # True
encoder.verify("wrong-password", hashed)         # False
```

**Constructor parameters:**

| Parameter | Type  | Default | Description                                          |
|-----------|-------|---------|------------------------------------------------------|
| `rounds`  | `int` | `12`    | Bcrypt cost factor (higher = slower but more secure)  |

The cost factor controls how computationally expensive the hashing operation is. Each increment roughly doubles the time. A value of 12 is considered a good default for production use.

**Methods:**

| Method                          | Return Type | Description                                      |
|---------------------------------|-------------|--------------------------------------------------|
| `hash(raw_password)`            | `str`       | Generate a bcrypt hash with a random salt        |
| `verify(raw_password, hashed)`  | `bool`      | Check if the raw password matches the hash       |

### Custom Password Encoders

You can create custom password encoders by implementing the `PasswordEncoder` protocol:

```python
import hashlib

class SHA256PasswordEncoder:
    """Simple SHA-256 encoder (NOT recommended for production)."""

    def hash(self, raw_password: str) -> str:
        return hashlib.sha256(raw_password.encode()).hexdigest()

    def verify(self, raw_password: str, hashed_password: str) -> bool:
        return self.hash(raw_password) == hashed_password
```

Because `PasswordEncoder` is a `runtime_checkable` protocol, you can use `isinstance()` checks:

```python
encoder = BcryptPasswordEncoder()
isinstance(encoder, PasswordEncoder)  # True
```

### Delegating & Modern Encoders

Beyond `BcryptPasswordEncoder`, the password module ships PBKDF2, scrypt, and Argon2 adapters plus a `DelegatingPasswordEncoder` that prefixes each stored hash with a `{id}` tag so the active algorithm can be migrated over time without invalidating existing credentials (Spring Security's `DelegatingPasswordEncoder` / `PasswordEncoderFactories`).

#### DelegatingPasswordEncoder

`DelegatingPasswordEncoder` wraps a map of `{id -> PasswordEncoder}` and a default `encoding_id`. `hash()` produces `{<encoding_id>}<inner-hash>` using the default encoder; `verify()` reads the `{id}` prefix and dispatches to the matching encoder. A stored value whose prefix is unknown or missing never matches. `upgrade_encoding()` reports whether a stored hash should be re-hashed with the current default — the hook for transparent on-login migration.

```python
from pyfly.security import (
    DelegatingPasswordEncoder,
    BcryptPasswordEncoder,
    Pbkdf2PasswordEncoder,
)

encoder = DelegatingPasswordEncoder(
    {
        "bcrypt": BcryptPasswordEncoder(rounds=12),
        "pbkdf2": Pbkdf2PasswordEncoder(),
    },
    encoding_id="bcrypt",
)

stored = encoder.hash("s3cret")          # "{bcrypt}$2b$12$..."
encoder.verify("s3cret", stored)          # True

# A legacy PBKDF2 hash still verifies, and is flagged for upgrade:
legacy = "{pbkdf2}sha256$600000$<salt>$<digest>"
encoder.verify("s3cret", legacy)          # True (dispatched to the pbkdf2 encoder)
encoder.upgrade_encoding(legacy)          # True  -> re-hash with the default (bcrypt)
encoder.upgrade_encoding(stored)          # False -> already the default encoding
```

The constructor raises `ValueError` if `encoding_id` is not present in the encoders map.

#### create_delegating_password_encoder

`create_delegating_password_encoder()` builds a ready-made delegating encoder with bcrypt as the default id, while `{pbkdf2}`, `{scrypt}`, and `{argon2}` hashes remain recognised for verification and migration (Spring's `PasswordEncoderFactories.createDelegatingPasswordEncoder()`):

```python
from pyfly.security import create_delegating_password_encoder

encoder = create_delegating_password_encoder(bcrypt_rounds=12)
encoder.hash("s3cret")    # "{bcrypt}$2b$12$..."
```

#### Argon2 / PBKDF2 / scrypt adapters

Each modern adapter implements the `PasswordEncoder` protocol and produces a self-describing hash string, so its parameters travel with the value.

| Encoder | Backing | Stored format | Defaults |
|---|---|---|---|
| `Argon2PasswordEncoder` | Argon2id (`argon2-cffi`) | argon2-cffi PHC string | `time_cost=3`, `memory_cost=65536`, `parallelism=4` |
| `Pbkdf2PasswordEncoder` | stdlib `hashlib.pbkdf2_hmac` | `<algorithm>$<iterations>$<salt_b64>$<hash_b64>` | `iterations=600_000`, `algorithm="sha256"`, `salt_bytes=16` |
| `ScryptPasswordEncoder` | stdlib `hashlib.scrypt` | `<n>$<r>$<p>$<salt_b64>$<hash_b64>` | `n=2**14`, `r=8`, `p=1`, `salt_bytes=16`, `dklen=32` |

```python
from pyfly.security import Argon2PasswordEncoder, Pbkdf2PasswordEncoder, ScryptPasswordEncoder

argon2 = Argon2PasswordEncoder()              # OWASP-preferred; Argon2id
pbkdf2 = Pbkdf2PasswordEncoder()              # FIPS-friendly; 600k SHA-256 iterations
scrypt = ScryptPasswordEncoder()              # memory-hard
```

`Argon2PasswordEncoder` imports `argon2-cffi` lazily, so the rest of the security module works without it; install with `pip install pyfly[argon2]`. Calling `hash()`/`verify()` without the dependency raises `ImportError`.

#### Opt-in delegating bean

The auto-configuration always exposes a `BcryptPasswordEncoder` bean when `pyfly.security.enabled=true` and bcrypt is installed. Setting `pyfly.security.password.delegating.enabled=true` additionally registers a `DelegatingPasswordEncoder` bean built via `create_delegating_password_encoder()`, reusing `pyfly.security.password.bcrypt-rounds` for the default encoder:

```yaml
pyfly:
  security:
    enabled: true
    password:
      bcrypt-rounds: 12
      delegating:
        enabled: true        # registers the {id}-prefixed DelegatingPasswordEncoder bean
```

#### SqlUserDetailsService note

`SqlUserDetailsService` (`pyfly.security.adapters.sql_user_details`) stores `password_hash` verbatim in a `TEXT` column, so `{id}`-prefixed delegating hashes round-trip unchanged. This makes on-login migration straightforward: after a successful `verify()`, call `upgrade_encoding()` on the stored hash and, when it returns `True`, re-hash with the delegating encoder and persist via `SqlUserDetailsService.save(...)`.

---

## SecurityMiddleware

The `SecurityMiddleware` is a Starlette middleware that automatically extracts JWT tokens from incoming requests and populates the `SecurityContext` on `request.state`. Its canonical location is `pyfly.web.adapters.starlette.security_middleware`, and it is re-exported from `pyfly.security.middleware` and the top-level `pyfly.security` package for convenience.

### How the Middleware Works

For every incoming request, the middleware:

1. Checks if the request path is in the `exclude_paths` set. If so, sets an anonymous context and continues.
2. Reads the `Authorization` header.
3. If the header starts with `"Bearer "`, extracts the token string.
4. Attempts to decode the token via `JWTService.to_security_context()`.
5. On success, sets `request.state.security_context` to the authenticated context.
6. On failure (invalid/expired token), logs a debug message and sets an anonymous context.
7. If no `Authorization` header is present, sets an anonymous context.

**The middleware never rejects requests.** It only populates the security context. Authorization enforcement is the job of the `@secure` decorator or your own logic.

```python
from pyfly.security import SecurityMiddleware, JWTService

jwt_service = JWTService(secret="my-secret")

# As Starlette middleware
from starlette.applications import Starlette

app = Starlette()
app.add_middleware(
    SecurityMiddleware,
    jwt_service=jwt_service,
    exclude_paths=["/docs", "/openapi.json", "/actuator/health"],
)
```

**Constructor parameters:**

| Parameter       | Type              | Default | Description                                    |
|-----------------|-------------------|---------|------------------------------------------------|
| `app`           | `ASGIApp`         | required| The ASGI application                           |
| `jwt_service`   | `JWTService`      | required| JWT service for token validation               |
| `exclude_paths` | `Sequence[str]`   | `()`    | Paths to skip (set anonymous context directly) |

### Excluding Paths

Public endpoints like documentation, health checks, and login should be excluded from JWT processing. While the middleware does not reject requests, excluding paths avoids unnecessary token parsing:

```python
app.add_middleware(
    SecurityMiddleware,
    jwt_service=jwt_service,
    exclude_paths=[
        "/docs",
        "/redoc",
        "/openapi.json",
        "/actuator/health",
        "/api/auth/login",
        "/api/auth/register",
    ],
)
```

### Integration with create_app()

The `SecurityMiddleware` is not included automatically by `create_app()`. You add it to the application after creation:

```python
from pyfly.web.adapters.starlette import create_app
from pyfly.security import SecurityMiddleware, JWTService

app = create_app(title="My API", context=ctx)
app.add_middleware(
    SecurityMiddleware,
    jwt_service=JWTService(secret="my-secret"),
    exclude_paths=["/docs", "/openapi.json"],
)
```

---

## The @secure Decorator

The `@secure` decorator enforces authentication and authorization on individual handler functions.

### Role-Based Access Control

Require the user to have at least one of the specified roles:

```python
from pyfly.security import secure, SecurityContext


@secure(roles=["ADMIN"])
async def admin_only(security_context: SecurityContext) -> dict:
    return {"message": "Admin access granted"}


@secure(roles=["ADMIN", "MANAGER"])
async def admin_or_manager(security_context: SecurityContext) -> dict:
    # User must have ADMIN *or* MANAGER role (at least one)
    return {"message": "Access granted"}
```

### Permission-Based Access Control

Require the user to have all of the specified permissions:

```python
@secure(permissions=["order:read"])
async def read_orders(security_context: SecurityContext) -> list:
    return [{"id": "1", "status": "active"}]


@secure(permissions=["order:read", "order:write"])
async def manage_orders(security_context: SecurityContext) -> dict:
    # User must have BOTH order:read AND order:write
    return {"message": "Full order access"}
```

### Combined Role and Permission Checks

When both `roles` and `permissions` are specified, the user must satisfy both conditions:

```python
@secure(roles=["ADMIN", "MANAGER"], permissions=["order:delete"])
async def delete_order(order_id: str, security_context: SecurityContext) -> None:
    # User must have (ADMIN or MANAGER) AND order:delete permission
    ...
```

### How @secure Works Internally

The `@secure` decorator wraps the function in an async wrapper that:

1. Extracts the `security_context` keyword argument from the call.
2. If `security_context` is `None`, raises `SecurityException(code="AUTH_REQUIRED")`.
3. If `security_context.is_authenticated` is `False`, raises `SecurityException(code="AUTH_REQUIRED")`.
4. If `roles` are specified and the user has none of them, raises `SecurityException(code="FORBIDDEN")`.
5. If `permissions` are specified and the user is missing any, raises `SecurityException(code="FORBIDDEN")`.
6. If all checks pass, calls the original function.

**The decorated function must accept a `security_context: SecurityContext` keyword argument.** This is how the decorator accesses the current user's context.

### @secure Error Responses

| Check Failed            | Exception                                             | HTTP Status |
|-------------------------|-------------------------------------------------------|-------------|
| No security context     | `SecurityException("Authentication required", code="AUTH_REQUIRED")` | 401 |
| Not authenticated       | `SecurityException("Authentication required", code="AUTH_REQUIRED")` | 401 |
| Insufficient roles      | `SecurityException("Insufficient roles: ...", code="FORBIDDEN")`     | 403 |
| Insufficient permissions| `SecurityException("Insufficient permissions: ...", code="FORBIDDEN")`| 403 |

These exceptions are caught by the global exception handler and converted to structured JSON error responses.

### Expression-Based Access Control

The `expression` parameter enables Spring Security-style security expressions for more complex authorization logic:

```python
@secure(expression="hasRole('ADMIN') and hasPermission('order:delete')")
async def delete_order(order_id: str, security_context: SecurityContext) -> None:
    ...
```

**Supported expressions:**

The `expression` parameter shares the full SpEL-subset vocabulary documented under [Method-Level Security](#method-level-security). The most common functions:

| Expression | Description | Example |
|---|---|---|
| `hasRole('X')` | User has role X (honours the [role hierarchy](#role-hierarchy)) | `hasRole('ADMIN')` |
| `hasAnyRole('X', 'Y')` | User has at least one of the roles | `hasAnyRole('ADMIN', 'MANAGER')` |
| `hasAuthority('X')` | User has role **or** permission X | `hasAuthority('order:read')` |
| `hasAnyAuthority('X', 'Y')` | User has at least one role/permission | `hasAnyAuthority('ADMIN', 'order:read')` |
| `hasPermission('X')` | User has permission X | `hasPermission('user:read')` |
| `isAuthenticated` | User is authenticated | `isAuthenticated` |
| `isAnonymous` | User is **not** authenticated | `isAnonymous` |
| `permitAll` / `denyAll` | Always allow / always deny | `denyAll` |
| `principal` / `authentication` | The current `SecurityContext` | `principal.user_id == 'system'` |
| `and` | Boolean AND | `hasRole('ADMIN') and hasPermission('write')` |
| `or` | Boolean OR | `hasRole('ADMIN') or hasRole('MANAGER')` |
| `not` | Boolean NOT | `not hasRole('GUEST')` |
| `(...)` | Grouping | `(hasRole('ADMIN') or hasRole('MANAGER')) and hasPermission('write')` |

Each function is usable bare (`isAuthenticated`) or called (`isAuthenticated()`). `@secure` does not bind method arguments, so `#paramName` and `returnObject` references are only available on `@pre_authorize` / `@post_authorize`.

**Complex expression examples:**

```python
# Require ADMIN role AND write permission
@secure(expression="hasRole('ADMIN') and hasPermission('order:write')")
async def update_order(order_id: str, security_context: SecurityContext) -> dict:
    ...

# Allow ADMIN or MANAGER with write permission
@secure(expression="(hasRole('ADMIN') or hasRole('MANAGER')) and hasPermission('write')")
async def approve_order(order_id: str, security_context: SecurityContext) -> dict:
    ...

# Deny guests
@secure(expression="not hasRole('GUEST')")
async def member_content(security_context: SecurityContext) -> dict:
    ...
```

**Safety:** Expressions are evaluated using safe AST parsing -- no `eval()` or `exec()` is used. The expression is first reduced to a boolean-only string (`True`/`False`/`and`/`or`/`not`/parentheses), then evaluated via recursive AST walking.

**Invalid expressions** (containing unsafe tokens like function calls, imports, or arithmetic) raise `SecurityException` with code `"INVALID_EXPRESSION"`.

**Source:** `src/pyfly/security/decorators.py`

---

## CSRF Protection

PyFly provides stateless CSRF protection using the double-submit cookie pattern. This is implemented as a `WebFilter` that integrates into the filter chain.

### How Double-Submit Cookie Works

1. On **safe requests** (GET, HEAD, OPTIONS, TRACE), the filter sets an `XSRF-TOKEN` cookie on the response.
2. JavaScript reads the cookie and includes its value as an `X-XSRF-TOKEN` header on subsequent unsafe requests.
3. On **unsafe requests** (POST, PUT, DELETE, PATCH), the filter validates that the header value matches the cookie value using a timing-safe comparison.
4. If either token is missing or they don't match, the filter returns HTTP 403.

Since cross-origin requests cannot read cookies from another domain, this proves the request originated from the same site.

### CSRF Utilities

Token generation and validation are provided by `pyfly.security.csrf`:

```python
from pyfly.security.csrf import (
    generate_csrf_token,
    validate_csrf_token,
    CSRF_COOKIE_NAME,    # "XSRF-TOKEN"
    CSRF_HEADER_NAME,    # "X-XSRF-TOKEN"
    SAFE_METHODS,        # frozenset({"GET", "HEAD", "OPTIONS", "TRACE"})
)

# Generate a cryptographically-secure token
token = generate_csrf_token()  # URL-safe base64 string (43 chars)

# Timing-safe validation
is_valid = validate_csrf_token(cookie_token, header_token)
```

| Function | Description |
|---|---|
| `generate_csrf_token()` | Generates a URL-safe token using `secrets.token_urlsafe(32)` |
| `validate_csrf_token(cookie, header)` | Timing-safe comparison using `secrets.compare_digest` |

**Source:** `src/pyfly/security/csrf.py`

### CsrfFilter

The `CsrfFilter` extends `OncePerRequestFilter` and runs in the WebFilter chain:

```python
from pyfly.web.adapters.starlette.filters.csrf_filter import CsrfFilter
```

| Property | Value | Description |
|---|---|---|
| `__pyfly_order__` | `HIGHEST_PRECEDENCE + 210` | Runs before the JWT `SecurityFilter` (HP+220) |
| `exclude_patterns` | `["/actuator/*", "/health", "/ready"]` | Paths excluded from CSRF |

**Bearer bypass:** Requests with an `Authorization: Bearer ...` header skip CSRF validation entirely. JWT-based API clients are already immune to CSRF attacks because tokens are not sent automatically by browsers.

**Cookie properties:**

| Property | Value | Reason |
|---|---|---|
| `httponly` | `False` | JavaScript must read the cookie to send it as a header |
| `samesite` | `lax` | Prevents cookies from being sent on cross-site requests |
| `secure` | `True` | Cookie only sent over HTTPS |
| `path` | `/` | Available to all paths |

### JavaScript Integration

To use CSRF protection with a JavaScript frontend:

```javascript
// Read the XSRF-TOKEN cookie
function getCsrfToken() {
    const match = document.cookie.match(/XSRF-TOKEN=([^;]+)/);
    return match ? match[1] : null;
}

// Include in requests
fetch('/api/orders', {
    method: 'POST',
    headers: {
        'Content-Type': 'application/json',
        'X-XSRF-TOKEN': getCsrfToken(),
    },
    body: JSON.stringify({ item: 'Widget' }),
    credentials: 'include',
});
```

**Source:** `src/pyfly/web/adapters/starlette/filters/csrf_filter.py`

### Enabled by Default (cookie-gated)

CSRF protection is **secure-by-default**: `CsrfFilterAutoConfiguration` registers the `CsrfFilter` unless `pyfly.security.csrf.enabled=false` (the property is treated as enabled when missing). The filter runs in **cookie-gated** mode by default, which lets it be on without breaking stateless/token clients:

- **Safe methods** (GET, HEAD, OPTIONS, TRACE) pass through and the response sets/refreshes the `XSRF-TOKEN` cookie.
- **Bearer requests** (`Authorization: Bearer ...`) are exempt — JWT API clients carry no ambient browser authority to forge.
- **Cookie-gated exemption** — when `cookie-gated` is true and the request carries no cookies, there is no ambient authority a cross-site request could abuse, so it is exempt. This is what makes default-on safe for stateless API clients.
- **Unsafe methods** with cookies are validated by comparing the `X-XSRF-TOKEN` header against the `XSRF-TOKEN` cookie (timing-safe); a missing or mismatched value returns HTTP 403.

Set `cookie-gated: false` for **strict mode**, which validates every unsafe request regardless of cookies. Disable CSRF entirely with `enabled: false`. The filter's exclude patterns default to `/actuator/*`, `/health`, `/ready` and can be overridden:

```yaml
pyfly:
  security:
    csrf:
      enabled: true                          # default; set false to disable entirely
      cookie-gated: true                     # default; false = strict (validate every unsafe request)
      exclude-patterns: "/actuator/*,/webhooks/**"
```

**Source:** `src/pyfly/web/security_filters_auto_configuration.py`, `src/pyfly/web/adapters/starlette/filters/csrf_filter.py`

---

## HttpSecurity DSL

The `HttpSecurity` builder provides a fluent API for defining URL-level access control rules. Instead of scattering `@secure` decorators on every handler, you declare authorization rules centrally and the `HttpSecurityFilter` enforces them at the filter layer -- before the route handler is reached.

```python
from pyfly.security.http_security import HttpSecurity
```

### Building URL-Level Access Rules

`HttpSecurity` follows a builder pattern inspired by Spring Security's `HttpSecurity`:

```python
from pyfly.security.http_security import HttpSecurity

http_security = HttpSecurity()
http_security.authorize_requests() \
    .request_matchers("/api/admin/**").has_role("ADMIN") \
    .request_matchers("/api/**").authenticated() \
    .request_matchers("/health", "/docs", "/openapi.json").permit_all() \
    .any_request().deny_all()

# Build the filter
http_security_filter = http_security.build()
```

The builder chain works as follows:

1. `authorize_requests()` -- returns an `_AuthorizeRequestsBuilder` to start defining rules.
2. `request_matchers(*patterns)` -- begins a rule for one or more URL glob patterns (fnmatch-style).
3. A terminal method (`permit_all()`, `authenticated()`, `has_role()`, etc.) -- sets the access rule for the matched patterns and returns back to the builder for chaining.
4. `any_request()` -- a catch-all that matches any path not matched by previous rules. Should be the last rule in the chain.
5. `build()` -- creates an `HttpSecurityFilter` configured with all accumulated rules.

Rules are evaluated **in declaration order** -- first match wins. If no rule matches a given request path, the request is allowed through (open by default).

### Access Rule Types

| Terminal Method | Rule Type | Description |
|---|---|---|
| `permit_all()` | `PERMIT_ALL` | Allow all requests (no authentication required) |
| `deny_all()` | `DENY_ALL` | Reject all requests with HTTP 403 |
| `authenticated()` | `AUTHENTICATED` | Require an authenticated user (any role) |
| `has_role(role)` | `HAS_ROLE` | Require the user to have the specified role |
| `has_any_role(roles)` | `HAS_ANY_ROLE` | Require the user to have at least one of the listed roles |
| `has_permission(perm)` | `HAS_PERMISSION` | Require the user to have the specified permission |

### HttpSecurityFilter

The `HttpSecurityFilter` is an `OncePerRequestFilter` ordered at `HIGHEST_PRECEDENCE + 350`. It runs **after** authentication filters (SecurityFilter at +220, OAuth2SessionSecurityFilter at +225) and **before** the route handler. This means the `SecurityContext` is already populated on `request.state` when the rules are evaluated.

```python
from pyfly.web.adapters.starlette.filters.http_security_filter import HttpSecurityFilter
```

**Evaluation logic:**

1. For each incoming request, the filter iterates through the rules in order.
2. The first rule whose URL patterns match the request path is applied.
3. If the rule requires authentication or specific roles/permissions and the user does not satisfy the requirement, the filter returns an RFC 7807 problem-detail JSON response (HTTP 401 or 403).
4. If no rule matches, the request passes through.

**Error responses** follow RFC 7807 with `Content-Type: application/problem+json`:

```json
{
  "type": "about:blank",
  "title": "Forbidden",
  "status": 403,
  "detail": "Required role 'ADMIN' is not granted.",
  "instance": "/api/admin/users"
}
```

### Integration with create_app()

Register the `HttpSecurityFilter` as a DI bean so it is auto-discovered by `create_app()`:

```python
from pyfly.container import configuration, bean
from pyfly.security.http_security import HttpSecurity


@configuration
class SecurityConfig:

    @bean
    def http_security_filter(self):
        http_security = HttpSecurity()
        http_security.authorize_requests() \
            .request_matchers("/api/admin/**").has_role("ADMIN") \
            .request_matchers("/api/**").authenticated() \
            .request_matchers("/health", "/docs", "/redoc", "/openapi.json").permit_all() \
            .any_request().permit_all()
        return http_security.build()
```

The filter is automatically included in the WebFilter chain and sorted by its `@order` value (`HIGHEST_PRECEDENCE + 350`).

**Source:** `src/pyfly/security/http_security.py`, `src/pyfly/web/adapters/starlette/filters/http_security_filter.py`

#### HTTP-Method-Scoped Rules

`request_matchers(...)` accepts an optional `methods` argument to scope a rule to specific HTTP verbs, mirroring Spring's `requestMatchers(HttpMethod.X, ...)`. Pass a single method as a string or several as a list/tuple; values are upper-cased. When `methods` is omitted the rule matches any method.

```python
from pyfly.security.http_security import HttpSecurity

http_security = HttpSecurity()
http_security.authorize_requests() \
    .request_matchers("/api/orders/**", methods="GET").authenticated() \
    .request_matchers("/api/orders/**", methods="POST").has_role("ADMIN") \
    .request_matchers("/api/orders/**", methods=["PUT", "DELETE"]).has_role("ADMIN") \
    .any_request().permit_all()

http_security_filter = http_security.build()
```

`any_request()` takes the same keyword to restrict the catch-all to specific methods:

```python
http_security.authorize_requests() \
    .any_request(methods=["PUT", "PATCH", "DELETE"]).authenticated()
```

A rule with an empty method list (the default) applies to every method; otherwise it applies only when the request method is one of the listed (upper-cased) verbs.

---

### Method-Level Security

PyFly provides Spring Security-style method-level authorization via `@pre_authorize` and `@post_authorize` decorators. These evaluate SpEL-style security expressions against the current `RequestContext.security_context`.

#### `@pre_authorize` — Check Before Execution

```python
from pyfly.security import pre_authorize

@service
class OrderService:

    @pre_authorize("hasRole('ADMIN') or hasPermission('order:write')")
    async def delete_order(self, order_id: str) -> None:
        ...

    @pre_authorize("isAuthenticated")
    async def list_orders(self) -> list[Order]:
        ...
```

#### `@post_authorize` — Check After Execution

```python
from pyfly.security import post_authorize

@service
class OrderService:

    @post_authorize("hasPermission('order:read')")
    async def get_order(self, order_id: str) -> Order:
        # Method body runs first; authorization checked on return
        return await self.repo.find_by_id(order_id)
```

#### Expression Vocabulary

`@pre_authorize`, `@post_authorize`, and `@secure(expression=...)` all share the same Spring Security SpEL subset, evaluated by `pyfly.security.expression.evaluate_security_expression`. Every function can be written bare (`isAuthenticated`) or called (`isAuthenticated()`).

| Expression | Description |
|-----------|-------------|
| `isAuthenticated` | User is authenticated (`user_id` is set) |
| `isAnonymous` | User is **not** authenticated |
| `permitAll` | Always `True` |
| `denyAll` | Always `False` |
| `hasRole('ADMIN')` | User has the ADMIN role (consults the [role hierarchy](#role-hierarchy)) |
| `hasAnyRole('ADMIN', 'MANAGER')` | User has at least one of the listed roles |
| `hasAuthority('order:read')` | User has the authority as a **role or a permission** |
| `hasAnyAuthority('ADMIN', 'order:read')` | User has at least one of the listed roles/permissions |
| `hasPermission('order:read')` | User has the permission. The 2-arg `hasPermission(target, 'perm')` form is also accepted; the last argument is the permission (target-based ACLs are not modelled) |
| `principal` / `authentication` | The current `SecurityContext` (e.g. `principal.user_id`, `authentication.roles`) |
| `and` / `or` / `not` | Boolean operators |
| `==`, `!=`, `<`, `<=`, `>`, `>=`, `in`, `not in` | Comparisons |
| `(...)` | Grouping |

```python
@pre_authorize("isAnonymous or hasAuthority('order:read')")
async def read_order(self, order_id: str) -> Order: ...

@pre_authorize("principal.user_id == 'system' and not isAnonymous")
async def run_batch(self) -> None: ...
```

The expression is parsed with `ast` and walked against a whitelist of node types — `eval`/`exec` are never used, only the security functions are callable, and attribute names beginning with `_` are rejected. Unsafe or unparseable expressions raise `SecurityException` with code `"INVALID_EXPRESSION"`.

#### Method Arguments and returnObject

Unlike `@secure`, the method-security decorators bind the wrapped call's arguments so expressions can reference them. Use `#paramName` to reference an argument by name, and `returnObject` (in `@post_authorize` only) to reference the method's return value:

```python
@service
class DocumentService:

    # Owners may delete their own document; ADMINs may delete any.
    @pre_authorize("hasRole('ADMIN') or #owner_id == principal.user_id")
    async def delete_document(self, doc_id: str, owner_id: str) -> None:
        ...

    # Only return the document if the caller owns it (checked after load).
    @post_authorize("returnObject.owner_id == principal.user_id")
    async def get_document(self, doc_id: str) -> Document:
        return await self._repo.find_by_id(doc_id)
```

Arguments are bound by name via `inspect.signature(...).bind_partial`, so positional and keyword calls both resolve. `returnObject` is `None` for `@pre_authorize`.

Both decorators raise `UnauthorizedException` (401) when no `SecurityContext` is available on the current `RequestContext`, and `ForbiddenException` (403, code `"FORBIDDEN"`) when the expression evaluates to `False`.

#### Role Hierarchy

A `RoleHierarchy` declares that higher roles imply lower ones — an `ADMIN` automatically has every authority of a `USER`. When a hierarchy is installed, `hasRole`, `hasAnyRole`, and `hasAuthority` expand the principal's roles transitively before checking.

```python
from pyfly.security import RoleHierarchy, set_role_hierarchy, get_role_hierarchy

# One "HIGHER > LOWER" rule per line (or ';'-separated):
hierarchy = RoleHierarchy.from_string("ADMIN > MANAGER\nMANAGER > USER")

hierarchy.expand(["ADMIN"])   # {"ADMIN", "MANAGER", "USER"}

# Install process-wide at startup (Spring's RoleHierarchy bean):
set_role_hierarchy(hierarchy)
get_role_hierarchy()          # -> the installed RoleHierarchy
set_role_hierarchy(None)      # disable
```

With the hierarchy above installed, a principal holding only `ADMIN` satisfies `hasRole('USER')`:

```python
@pre_authorize("hasRole('USER')")   # ADMIN passes via ADMIN > MANAGER > USER
async def list_orders(self) -> list[Order]: ...
```

`set_role_hierarchy()` sets a single process-wide hierarchy consulted by all method-security and `@secure` role checks; call it once during startup. With no hierarchy installed (the default), role checks are exact-match only.

**Source:** `src/pyfly/security/method_security.py`, `src/pyfly/security/expression.py`, `src/pyfly/security/role_hierarchy.py`

---

#### @pre_filter / @post_filter and PermissionEvaluator

`@pre_filter` and `@post_filter` filter *collections* element-by-element against a security expression, binding each element to `filterObject` (Spring's `@PreFilter` / `@PostFilter`). They complement the all-or-nothing `@pre_authorize` / `@post_authorize` checks.

`@post_filter(expression)` filters the method's returned collection after it runs; non-collection results are returned unchanged. `@pre_filter(expression, filter_target=None)` filters a collection *argument* before the method runs — `filter_target` names the parameter to filter; when omitted, the first collection-valued argument is used. Both preserve the collection's concrete type (`list` / `tuple` / `set`) and drop elements for which the expression is `False`.

```python
from pyfly.security import pre_filter, post_filter


@service
class DocumentService:

    # Return only the documents the caller owns.
    @post_filter("filterObject.owner_id == principal.user_id")
    async def list_documents(self) -> list[Document]:
        return await self._repo.find_all()

    # Keep only non-draft documents from the incoming batch before publishing.
    @pre_filter("filterObject.draft == False", filter_target="documents")
    async def publish(self, documents: list[Document]) -> None:
        ...
```

##### PermissionEvaluator (ACL-style hasPermission)

`PermissionEvaluator` is the SPI behind domain-object `hasPermission(...)` checks. It is a runtime-checkable `Protocol` with a single method:

```python
def has_permission(
    self,
    context: Any,          # the active SecurityContext
    target: Any,           # the domain object, or its identifier (3-arg form)
    permission: str,
    *,
    target_type: str | None = None,
) -> bool: ...
```

Install one process-wide with `set_permission_evaluator()`; `get_permission_evaluator()` returns the current one and `set_permission_evaluator(None)` disables it. When an evaluator is installed, the `hasPermission` function in security expressions dispatches to it by argument shape:

- `hasPermission('perm')` — flat check: `has_permission(ctx, None, 'perm')`
- `hasPermission(target, 'perm')` — domain object: `has_permission(ctx, target, 'perm')`
- `hasPermission(id, 'Type', 'perm')` — identifier + type: `has_permission(ctx, id, 'perm', target_type='Type')`

When **no** evaluator is installed, `hasPermission` falls back to a flat permission check on the `SecurityContext` (the principal's granted permissions), using the last argument as the permission name.

```python
from pyfly.security import PermissionEvaluator, set_permission_evaluator


class AclPermissionEvaluator:
    def has_permission(self, context, target, permission, *, target_type=None) -> bool:
        # Consult your ACL store using context.user_id, target/target_type, permission.
        ...


set_permission_evaluator(AclPermissionEvaluator())
```

```python
@service
class OrderService:

    @pre_authorize("hasPermission(#order, 'order:write')")
    async def update(self, order: Order) -> None: ...

    @pre_authorize("hasPermission(#order_id, 'Order', 'write')")
    async def update_by_id(self, order_id: str) -> None: ...
```

**Source:** `src/pyfly/security/method_security.py`, `src/pyfly/security/expression.py`, `src/pyfly/security/permission.py`

---

## Authentication Mechanisms

Beyond stateless JWT processing, PyFly ships the Spring Security authentication SPI: a `UserDetailsService` that resolves a username to a stored credential, an `AuthenticationManager` (`ProviderManager`) that delegates to one or more `AuthenticationProvider`s, and a family of `WebFilter`s that establish a `SecurityContext` from HTTP Basic credentials, a login form, a client certificate, or an impersonation request. Each filter populates `request.state.security_context`; the `HttpSecurity` gate and `@secure` decorator then enforce access. Config-driven HTTP Basic and form login store their users with **pre-hashed bcrypt password hashes** — plaintext passwords never appear in configuration.

### UserDetails and the UserDetailsService SPI

A `UserDetailsService` is the credential-lookup port: it resolves a username to a `UserDetails` (a stored password hash plus authorities) or `None`. The HTTP Basic / form-login / X.509 filters verify the supplied password against that hash using a `PasswordEncoder`.

`UserDetails` is a frozen dataclass:

| Field | Type | Default | Description |
|---|---|---|---|
| `username` | `str` | required | The principal's identifier |
| `password_hash` | `str` | required | Stored credential (e.g. a bcrypt hash) |
| `roles` | `list[str]` | `[]` | Granted roles |
| `permissions` | `list[str]` | `[]` | Granted permissions |
| `enabled` | `bool` | `True` | Whether the account may authenticate |

The port is a single async method:

```python
from typing import Protocol, runtime_checkable
from pyfly.security import UserDetails

@runtime_checkable
class UserDetailsService(Protocol):
    async def load_user_by_username(self, username: str) -> UserDetails | None: ...
```

#### InMemoryUserDetailsService

`InMemoryUserDetailsService` is a dict-backed store for development and testing. It takes any number of `UserDetails` and exposes `load_user_by_username()` plus an `add()` mutator:

```python
from pyfly.security import (
    InMemoryUserDetailsService,
    UserDetails,
    BcryptPasswordEncoder,
)

encoder = BcryptPasswordEncoder(rounds=12)
users = InMemoryUserDetailsService(
    UserDetails(
        username="alice",
        password_hash=encoder.hash("s3cret"),   # store the hash, not the password
        roles=["ADMIN", "USER"],
        permissions=["order:read", "order:write"],
    ),
)
users.add(UserDetails(username="bob", password_hash=encoder.hash("hunter2"), roles=["USER"]))

await users.load_user_by_username("alice")   # -> UserDetails(...)
await users.load_user_by_username("nobody")  # -> None
```

#### SqlUserDetailsService

`SqlUserDetailsService` is a durable, table-backed `UserDetailsService` for HTTP Basic / form login, backed by any SQLAlchemy `AsyncEngine`. It is hexagonal: the engine is supplied lazily via an `engine_factory` callable (the composition root injects it), and SQLAlchemy is never imported at module scope. The table is created lazily and idempotently on first use, with columns `username` (PK), `password_hash`, `roles` (JSON), `permissions` (JSON), and `enabled` (int). It works on PostgreSQL and SQLite via an `ON CONFLICT` upsert.

```python
from pyfly.container import configuration, bean
from pyfly.security.adapters.sql_user_details import SqlUserDetailsService
from pyfly.security import UserDetails, UserDetailsService, BcryptPasswordEncoder
from sqlalchemy.ext.asyncio import AsyncEngine


@configuration
class UserStoreConfig:

    @bean
    def user_details_service(self, engine: AsyncEngine) -> UserDetailsService:
        # The engine is resolved from the container; the table defaults to "pyfly_users".
        return SqlUserDetailsService(lambda: engine, table="pyfly_users")
```

```python
# Provisioning and managing users (save() upserts by username; delete() removes one):
store = SqlUserDetailsService(lambda: engine)
await store.save(
    UserDetails(
        username="alice",
        password_hash=BcryptPasswordEncoder().hash("s3cret"),
        roles=["ADMIN"],
        permissions=["order:write"],
        enabled=True,
    )
)
await store.load_user_by_username("alice")  # -> UserDetails(...)
await store.delete("alice")
```

The constructor rejects an invalid SQL identifier as the table name (it must match `^[A-Za-z_][A-Za-z0-9_]*$`), raising `ValueError`.

**Source:** `src/pyfly/security/user_details.py`, `src/pyfly/security/adapters/sql_user_details.py`

### AuthenticationManager: ProviderManager and DaoAuthenticationProvider

`ProviderManager` is PyFly's `AuthenticationManager`: it holds an ordered list of `AuthenticationProvider`s and authenticates an `Authentication` request by delegating to the first provider that `supports()` it. The built-in `DaoAuthenticationProvider` checks a username/password against a `UserDetailsService` and a `PasswordEncoder`.

An `Authentication` is both the request and the result. Before authentication, `principal` and `credentials` carry the submitted username/password; after a successful authentication, `authenticated` is `True`, `roles` / `permissions` / `authorities` are populated, and `credentials` is erased. `to_security_context()` converts the (authenticated) result into a `SecurityContext`.

```python
from pyfly.security import (
    Authentication,
    DaoAuthenticationProvider,
    ProviderManager,
    InMemoryUserDetailsService,
    UserDetails,
    BcryptPasswordEncoder,
)

encoder = BcryptPasswordEncoder(rounds=12)
users = InMemoryUserDetailsService(
    UserDetails(username="alice", password_hash=encoder.hash("s3cret"), roles=["ADMIN"]),
)

manager = ProviderManager(DaoAuthenticationProvider(users, encoder))

result = await manager.authenticate(Authentication(principal="alice", credentials="s3cret"))
result.authenticated   # True
result.credentials     # None  -> erased on success
result.authorities     # ["ADMIN"]  (roles + permissions)
ctx = result.to_security_context()   # SecurityContext(user_id="alice", roles=["ADMIN"], ...)
```

`DaoAuthenticationProvider` behaviour, verified in source:

- **Credential erasure.** A successful `authenticate()` returns an `Authentication` with `credentials=None`; `ProviderManager` also clears `credentials` on the returned result. `authorities` is the concatenation of `roles` and `permissions`.
- **Timing equalisation.** When the username is unknown, the provider still runs `PasswordEncoder.verify()` against a throw-away dummy hash before raising, so request timing cannot be used to enumerate valid usernames.
- **Failure modes.** An unknown user or a wrong password raises `BadCredentialsException` (code `"BAD_CREDENTIALS"`). The password is verified *before* the `enabled` check, so only a *correct* password against a disabled account raises `DisabledException` (code `"ACCOUNT_DISABLED"`); a wrong password on a disabled account still yields `BadCredentialsException`.
- **`supports()`** returns `True` only when `principal` is non-empty and `credentials` is not `None`.

`ProviderManager.authenticate()` iterates providers in order: it skips providers that do not `supports()` the request; if a supporting provider raises an `AuthenticationException` it remembers it and tries the next; the first authenticated result wins. If every supporting provider failed it re-raises the last error, and if no provider supported the request it raises `ProviderNotFoundException` (code `"PROVIDER_NOT_FOUND"`). Construct one from an iterable with `ProviderManager.of([...])`.

All of these derive from `AuthenticationException` (a `SecurityException` subclass):

| Exception | Code | Raised when |
|---|---|---|
| `BadCredentialsException` | `BAD_CREDENTIALS` | Unknown principal or wrong password |
| `DisabledException` | `ACCOUNT_DISABLED` | Correct password but `enabled=False` |
| `ProviderNotFoundException` | `PROVIDER_NOT_FOUND` | No provider `supports()` the request |

**Source:** `src/pyfly/security/authentication.py`

### Form Login

`FormLoginFilter` processes a POST of username/password to the login URL, authenticates via a `ProviderManager`, and on success **rotates the session id** (session-fixation defense) before storing the `SecurityContext` in the session — where `OAuth2SessionSecurityFilter` restores it on later requests. It runs at `HIGHEST_PRECEDENCE + 230` (after the session-restoring filter), so a successful login overrides any prior anonymous context. Both browser (302 redirect) and API (JSON) responses are supported via `use_redirect`.

Enable config-driven form login by declaring **pre-hashed** users under `pyfly.security.form-login.users` (requires `starlette` and `bcrypt`). The auto-configuration builds a `ProviderManager(DaoAuthenticationProvider(InMemoryUserDetailsService(...), BcryptPasswordEncoder(...)))` from those users:

```yaml
pyfly:
  security:
    enabled: true
    password:
      bcrypt-rounds: 12              # cost factor for the encoder
    form-login:
      enabled: true
      login-url: "/login"           # POST target this filter intercepts
      username-param: "username"
      password-param: "password"
      success-url: "/"
      failure-url: "/login?error"
      use-redirect: true            # false -> JSON {"authenticated": true} / 401
      users:
        alice:
          password-hash: "$2b$12$..."   # bcrypt hash, never plaintext
          roles: "ADMIN,USER"           # comma-separated or a YAML list
          permissions: "order:read,order:write"
          enabled: true
```

For a dynamic user store (e.g. `SqlUserDetailsService`), register your own `FormLoginFilter` bean instead of using the config users:

```python
from pyfly.container import configuration, bean
from pyfly.web.ports.filter import WebFilter
from pyfly.web.adapters.starlette.filters.form_login_filter import FormLoginFilter
from pyfly.security import ProviderManager, DaoAuthenticationProvider, BcryptPasswordEncoder, UserDetailsService


@configuration
class FormLoginConfig:

    @bean
    def form_login_filter(self, users: UserDetailsService) -> WebFilter:
        manager = ProviderManager(DaoAuthenticationProvider(users, BcryptPasswordEncoder(rounds=12)))
        return FormLoginFilter(
            manager,
            login_url="/login",
            success_url="/dashboard",
            failure_url="/login?error",
            use_redirect=True,
        )
```

On a failed login the filter catches `AuthenticationException` and returns the failure response (a redirect to `failure_url`, or `401` `{"error": "invalid_credentials"}` in API mode).

**Source:** `src/pyfly/web/adapters/starlette/filters/form_login_filter.py`

### HTTP Basic

`HttpBasicAuthenticationFilter` parses an `Authorization: Basic` header (RFC 7617), resolves the user via a `UserDetailsService`, and verifies the password with a `PasswordEncoder` (offloaded to a worker thread, since bcrypt/argon2 verification is CPU-bound). It runs at `HIGHEST_PRECEDENCE + 215`, just before the symmetric JWT filter, so credential-based clients get a context while token-based auth falls through when no Basic header is present.

`error_mode` controls what happens on a *present-but-invalid* credential:

- `"anonymous"` (default): a bad credential yields an anonymous context and the request proceeds — the `HttpSecurity` gate decides.
- `"401"`: a present-but-invalid credential is rejected here with `401 Unauthorized`, a `WWW-Authenticate: Basic realm="…"` challenge, and body `{"error": "invalid_credentials", "error_description": "Authentication failed."}`.

In either mode, a *missing* Basic header always falls through to the gate. The filter treats an unknown user, a disabled account (`enabled=False`), and a wrong password uniformly as an authentication failure.

Enable config-driven HTTP Basic by declaring **pre-hashed** users under `pyfly.security.http-basic.users` (requires `starlette` and `bcrypt`):

```yaml
pyfly:
  security:
    enabled: true
    password:
      bcrypt-rounds: 12
    http-basic:
      enabled: true
      realm: "PyFly"
      error-mode: "401"             # or "anonymous" (default)
      users:
        alice:
          password-hash: "$2b$12$..."   # bcrypt hash, never plaintext
          roles: "ADMIN,USER"
          permissions: "order:read"
          enabled: true
```

For a dynamic user store, register the filter directly as a `WebFilter` bean:

```python
from pyfly.container import configuration, bean
from pyfly.web.ports.filter import WebFilter
from pyfly.web.adapters.starlette.filters.http_basic_filter import HttpBasicAuthenticationFilter
from pyfly.security import BcryptPasswordEncoder, UserDetailsService


@configuration
class HttpBasicConfig:

    @bean
    def http_basic_filter(self, users: UserDetailsService) -> WebFilter:
        return HttpBasicAuthenticationFilter(
            users,
            BcryptPasswordEncoder(rounds=12),
            realm="PyFly",
            error_mode="401",       # or "anonymous"
        )
```

You can generate a bcrypt hash for the config `password-hash` values with the built-in encoder:

```bash
python -c "from pyfly.security import BcryptPasswordEncoder; print(BcryptPasswordEncoder().hash('s3cret'))"
```

**Source:** `src/pyfly/web/adapters/starlette/filters/http_basic_filter.py`

### X.509 Client-Certificate Authentication

`X509AuthenticationFilter` authenticates a request by the client certificate forwarded by a TLS-terminating proxy in a header (PEM, possibly URL-encoded). It runs at `HIGHEST_PRECEDENCE + 218`. The certificate subject's Common Name becomes the principal; alternatively a `subject_regex` with a capturing group extracts the principal from the subject's RFC 4514 string (the first capture group is used). There is no auto-configuration for X.509 — register the filter as a `WebFilter` bean.

Behaviour:

- **No `UserDetailsService`** — certificate presence *is* the credential: the principal authenticates with no authority lookup (`SecurityContext(user_id=<CN>)`).
- **With a `UserDetailsService`** — the extracted principal must resolve to an enabled user, whose roles/permissions are applied; an unknown or disabled user fails.
- On failure, `error_mode="401"` returns `401` `{"error": "invalid_client_certificate"}` with a `WWW-Authenticate: X509` header; `"anonymous"` (default) sets an anonymous context and proceeds. A *missing* certificate header always falls through.

```python
from pyfly.container import configuration, bean
from pyfly.web.ports.filter import WebFilter
from pyfly.web.adapters.starlette.filters.x509_filter import X509AuthenticationFilter
from pyfly.security import UserDetailsService


@configuration
class X509Config:

    @bean
    def x509_filter(self, users: UserDetailsService) -> WebFilter:
        return X509AuthenticationFilter(
            cert_header="x-client-cert",       # header the proxy forwards (PEM)
            user_details_service=users,        # omit to authenticate on cert presence alone
            subject_regex=r"CN=([^,]+)",       # optional; default extracts the CN
            error_mode="401",                  # or "anonymous"
        )
```

**Source:** `src/pyfly/web/adapters/starlette/filters/x509_filter.py`

### Logout

`LogoutFilter` handles a POST to the logout URL — independent of OAuth2 — by invalidating the HTTP session, clearing the security context to anonymous, and deleting configured cookies. It runs at `HIGHEST_PRECEDENCE + 235` (after form login). With `use_redirect=True` it returns a `302` to the success URL; otherwise it returns `204 No Content`.

Enable config-driven logout (requires `starlette`):

```yaml
pyfly:
  security:
    logout:
      enabled: true
      logout-url: "/logout"              # POST target this filter intercepts
      success-url: "/login?logout"       # redirect target (use-redirect=true)
      delete-cookies: "SESSION,XSRF-TOKEN"   # comma-separated or a YAML list
      use-redirect: true                 # false -> 204 No Content
```

Or register the filter programmatically:

```python
from pyfly.container import configuration, bean
from pyfly.web.ports.filter import WebFilter
from pyfly.web.adapters.starlette.filters.logout_filter import LogoutFilter


@configuration
class LogoutConfig:

    @bean
    def logout_filter(self) -> WebFilter:
        return LogoutFilter(
            logout_url="/logout",
            logout_success_url="/login?logout",
            delete_cookies=["SESSION", "XSRF-TOKEN"],
            use_redirect=True,
        )
```

Each deleted cookie is cleared with `path="/"`.

**Source:** `src/pyfly/web/adapters/starlette/filters/logout_filter.py`

### switch-user / run-as Impersonation

`SwitchUserFilter` lets an authorized principal impersonate another user and switch back, mirroring Spring's `SwitchUserFilter`. It runs at `HIGHEST_PRECEDENCE + 232` (after form login, before logout) and matches on path (the target username comes from a query parameter). There is no auto-configuration — register it as a `WebFilter` bean with a `UserDetailsService`.

Flow:

1. The acting principal visits the **switch URL** (default `/login/impersonate`) with `?username=<target>`. They must be authenticated, and must hold the **switch authority** (default `ADMIN`) as either a role or a permission; otherwise the filter returns `401` (`authentication_required`) or `403` (`forbidden`).
2. The target must resolve to an enabled user, else `404` (`user_not_found`).
3. On success the filter builds an impersonated `SecurityContext` carrying the target's roles **plus** the marker role `PREVIOUS_ADMINISTRATOR` (the value of `PREVIOUS_PRINCIPAL_ROLE`). It stashes the full original `SecurityContext` in the session (under the internal `SWITCH_USER_ORIGINAL` key) so it can be restored, and records the original principal id on the impersonated context's `switch_user_original` attribute. It then redirects to `success_url`. The marker lets the application detect run-as and offer an "exit" action.
4. Visiting the **exit URL** (default `/logout/impersonate`) restores the original context and redirects to `success_url`; if there is no stashed original it returns `400` (`not_impersonating`).

```python
from pyfly.container import configuration, bean
from pyfly.web.ports.filter import WebFilter
from pyfly.web.adapters.starlette.filters.switch_user_filter import SwitchUserFilter
from pyfly.security import UserDetailsService


@configuration
class SwitchUserConfig:

    @bean
    def switch_user_filter(self, users: UserDetailsService) -> WebFilter:
        return SwitchUserFilter(
            users,
            switch_url="/login/impersonate",   # GET ?username=<target>
            exit_url="/logout/impersonate",
            username_param="username",
            switch_authority="ADMIN",          # required role OR permission
            success_url="/",
        )
```

An impersonated request can be recognised with `security_context.has_role("PREVIOUS_ADMINISTRATOR")`, and the original principal read from `security_context.attributes["switch_user_original"]`.

**Source:** `src/pyfly/web/adapters/starlette/filters/switch_user_filter.py`

---

## Security Headers

`SecurityHeadersFilter` adds OWASP-recommended response headers to **every** response. It is an `OncePerRequestFilter` ordered at `HIGHEST_PRECEDENCE + 300`, and appends a precomputed, static set of header pairs after the downstream handler returns. Header names and values come from `SecurityHeadersConfig` (a frozen dataclass); the table below lists the exact headers emitted with their defaults:

| Header | Default value | Notes |
|---|---|---|
| `x-content-type-options` | `nosniff` | always emitted |
| `x-frame-options` | `DENY` | always emitted |
| `strict-transport-security` | `max-age=31536000; includeSubDomains` | always emitted |
| `x-xss-protection` | `0` | always emitted (modern browsers: disable the legacy XSS auditor) |
| `referrer-policy` | `strict-origin-when-cross-origin` | always emitted |
| `content-security-policy` | *(unset)* | only emitted when `content_security_policy` is configured (default `None` = not added — CSP is too app-specific) |
| `permissions-policy` | *(unset)* | only emitted when `permissions_policy` is configured (default `None` = not added) |

To customise, construct the filter with a `SecurityHeadersConfig`:

```python
from pyfly.web.adapters.starlette.filters.security_headers_filter import SecurityHeadersFilter
from pyfly.web.security_headers import SecurityHeadersConfig

filter_ = SecurityHeadersFilter(
    SecurityHeadersConfig(
        x_frame_options="SAMEORIGIN",
        content_security_policy="default-src 'self'",
        permissions_policy="geolocation=(), camera=()",
    )
)
```

**Source:** `src/pyfly/web/adapters/starlette/filters/security_headers_filter.py`, `src/pyfly/web/security_headers.py`

---

## OAuth 2.1 & OpenID Connect

PyFly ships a complete OAuth 2.1 / OpenID Connect implementation across all three roles — **resource server** (validate inbound tokens), **client & login** (the browser `authorization_code` flow with PKCE), and a full **authorization server** (issue tokens; `client_credentials`, `refresh_token`, and `authorization_code` grants, OIDC id tokens, JWKS, introspection/revocation, Dynamic Client Registration, PAR, JAR, metadata/discovery) — plus sender-constrained (DPoP / mTLS) tokens.

That surface is documented in its own guide:

**→ [OAuth 2.1 & OpenID Connect](oauth2.md)**

```yaml
pyfly:
  security:
    oauth2:
      resource-server:        # validate JWTs from any OIDC IdP
        enabled: true
        issuer-uri: "https://login.example.com/realms/app"
        audiences: "my-api"
```

The resource server validates bearer tokens against a remote JWKS with config-driven claim mapping; the client supports declarative `ClientRegistration`s with PKCE on by default; the authorization server issues and manages tokens. See the [OAuth2 guide](oauth2.md) for the resource server, client/login, authorization server, DPoP/mTLS, and the full configuration reference.

---

## Secure-by-Default & Hardening

PyFly's security defaults are chosen to fail closed. The behaviours below are active without extra configuration; operators should understand them before deploying.

**Signing-secret fail-fast.** The composition root refuses to start when a token-signing secret is left at the built-in placeholder `change-me-in-production`, raising `SecurityException` with code `INSECURE_SIGNING_SECRET`. For HMAC (`HS*`) algorithms it additionally requires at least 32 bytes (RFC 7518 §3.2), raising `WEAK_SIGNING_SECRET` otherwise. This is enforced for the authorization-server secret (`pyfly.security.oauth2.authorization-server.secret`) unconditionally. The symmetric `JWTService` secret (`pyfly.security.jwt.secret`) is only enforced when the symmetric JWT filter is enabled (`pyfly.security.jwt.filter.enabled=true`) — a resource-server-only app validates JWTs via JWKS and never needs a symmetric signing secret.

```bash
# Generate a strong secret:
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

**CSRF on by default (cookie-gated).** CSRF protection is enabled unless `pyfly.security.csrf.enabled=false`; cookie-gated mode keeps stateless/Bearer clients unaffected. Set `pyfly.security.csrf.cookie-gated=false` for strict enforcement of every unsafe request. (See [Enabled by Default](#enabled-by-default-cookie-gated).)

**PKCE on by default.** `ClientRegistration.use_pkce` defaults to `True` for the `authorization_code` flow (RFC 9700 / OAuth 2.1). A public client (empty `client_secret`) always uses PKCE with `S256` even if `use_pkce=False`, since it has no other defense against code injection; only set `use_pkce=False` for a confidential client talking to an authorization server that rejects PKCE. The RFC 9207 `iss` authorization-response parameter is validated whenever present; set `require_iss=true` (per registration) to also reject providers that omit it.

**ROPC opt-in.** The Resource Owner Password Credentials grant (`grant_type=password`) against external IdPs (`keycloak` / `cognito` / `azure-ad`) is disabled unless `pyfly.idp.allow-password-grant=true`.

**client_credentials scope validation.** The `AuthorizationServer` rejects a `client_credentials` request that asks for scopes not registered for the client, returning the `INVALID_SCOPE` error.

**Refresh-token rotation + reuse detection.** Refresh tokens are single-use and rotated on every refresh; the old token is revoked when a new one is issued. Reusing an already-rotated (revoked) token triggers family reuse detection — the token family is revoked — and the request is rejected with `INVALID_GRANT`.

**Source:** `src/pyfly/security/auto_configuration.py`, `src/pyfly/web/security_filters_auto_configuration.py`, `src/pyfly/security/oauth2/client.py`, `src/pyfly/security/oauth2/authorization_server.py`

---

## Exception Hierarchy

The security module uses exceptions from `pyfly.kernel.exceptions`:

| Exception              | HTTP Status | Description                                       |
|------------------------|-------------|---------------------------------------------------|
| `SecurityException`    | 401         | Base security error (auth failures)                |
| `UnauthorizedException`| 401         | Authentication required but not provided/invalid   |
| `ForbiddenException`   | 403         | Authenticated but lacks permission                 |

The `@secure` decorator raises `SecurityException` directly with appropriate codes. The `JWTService.decode()` method raises `SecurityException` with code `"INVALID_TOKEN"` for any token validation failure.

---

## Auto-Configuration

When `pyfly.security.enabled` is set to `true` in your configuration, PyFly automatically wires the security beans through two auto-configuration classes. No manual bean registration is needed.

### JwtAutoConfiguration

**Conditions:** `pyfly.security.enabled=true` AND `pyjwt` library installed.

| Bean | Type | Config Keys |
|------|------|-------------|
| `jwt_service` | `JWTService` | `pyfly.security.jwt.secret`, `pyfly.security.jwt.algorithm` |
| `security_filter` | `WebFilter` (opt-in) | `pyfly.security.jwt.filter.enabled=true`, `pyfly.security.jwt.exclude-patterns` |

The auto-configured `JWTService` reads its secret and algorithm from the configuration. The `SecurityFilter` bean is opt-in — it is only created when `pyfly.security.jwt.filter.enabled=true` (and `starlette` is installed), allowing the filter to be auto-discovered by `create_app()` without manual registration:

```yaml
pyfly:
  security:
    enabled: true
    jwt:
      secret: "my-production-secret"   # REQUIRED: change from default
      algorithm: "HS256"               # Default: HS256
      filter:
        enabled: true                  # Opt-in: register SecurityFilter bean
        exclude-patterns: "/docs,/openapi.json,/actuator/health"
```

### PasswordEncoderAutoConfiguration

**Conditions:** `pyfly.security.enabled=true` AND `bcrypt` library installed.

| Bean | Type | Config Keys |
|------|------|-------------|
| `password_encoder` | `BcryptPasswordEncoder` | `pyfly.security.password.bcrypt-rounds` |

```yaml
pyfly:
  security:
    enabled: true
    password:
      bcrypt-rounds: 12   # Default: 12
```

### Overriding Auto-Configured Beans

Both auto-configuration classes use `@conditional_on_missing_bean`, so providing your own `JWTService` or `BcryptPasswordEncoder` via a `@configuration` + `@bean` method silently skips the auto-configured version:

```python
from pyfly.container.bean import bean
from pyfly.container import configuration
from pyfly.security import JWTService

@configuration
class MySecurityConfig:
    @bean
    def jwt_service(self) -> JWTService:
        return JWTService(secret="custom-secret", algorithm="RS256")
```

**Source:** `src/pyfly/security/auto_configuration.py`

---

## Putting It All Together

This complete example demonstrates a login/register flow with JWT authentication, password hashing, and role-based endpoint protection.

### Configuration Layer

```python
from pyfly.container import configuration, bean
from pyfly.security import JWTService, BcryptPasswordEncoder


@configuration
class SecurityConfig:
    """Wires security beans into the DI container."""

    @bean
    def jwt_service(self) -> JWTService:
        # In production, load the secret from environment/config
        return JWTService(secret="change-me-in-production", algorithm="HS256")

    @bean
    def password_encoder(self) -> BcryptPasswordEncoder:
        return BcryptPasswordEncoder(rounds=12)
```

### User Entity and Repository

```python
from pyfly.data.relational.sqlalchemy import BaseEntity, Repository
from pyfly.container import repository as repo_stereotype
from sqlalchemy import String
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.ext.asyncio import AsyncSession


class User(BaseEntity):
    __tablename__ = "users"

    username: Mapped[str] = mapped_column(String(255), unique=True)
    email: Mapped[str] = mapped_column(String(255), unique=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    role: Mapped[str] = mapped_column(String(50), default="USER")


@repo_stereotype
class UserRepository(Repository[User]):
    def __init__(self, session: AsyncSession) -> None:
        super().__init__(User, session)

    async def find_by_username(self, username: str) -> list[User]: ...
    async def exists_by_username(self, username: str) -> bool: ...
    async def exists_by_email(self, email: str) -> bool: ...
```

### Authentication Service

```python
from datetime import datetime, timedelta, UTC

from pyfly.container import service
from pyfly.kernel.exceptions import (
    UnauthorizedException,
    ConflictException,
    ResourceNotFoundException,
)
from pyfly.security import JWTService, BcryptPasswordEncoder, SecurityContext


@service
class AuthService:
    def __init__(
        self,
        user_repo: UserRepository,
        jwt_service: JWTService,
        password_encoder: BcryptPasswordEncoder,
    ) -> None:
        self._users = user_repo
        self._jwt = jwt_service
        self._encoder = password_encoder

    async def register(self, username: str, email: str, password: str) -> str:
        """Register a new user and return a JWT token."""
        if await self._users.exists_by_username(username):
            raise ConflictException(
                f"Username '{username}' is already taken",
                code="USERNAME_TAKEN",
            )
        if await self._users.exists_by_email(email):
            raise ConflictException(
                f"Email '{email}' is already registered",
                code="EMAIL_TAKEN",
            )

        user = User(
            username=username,
            email=email,
            password_hash=self._encoder.hash(password),
            role="USER",
        )
        saved = await self._users.save(user)
        return self._create_token(saved)

    async def login(self, username: str, password: str) -> str:
        """Authenticate a user and return a JWT token."""
        users = await self._users.find_by_username(username)
        if not users:
            raise UnauthorizedException(
                "Invalid credentials",
                code="INVALID_CREDENTIALS",
            )

        user = users[0]
        if not self._encoder.verify(password, user.password_hash):
            raise UnauthorizedException(
                "Invalid credentials",
                code="INVALID_CREDENTIALS",
            )

        return self._create_token(user)

    async def get_current_user(self, user_id: str) -> dict:
        """Get the current user's profile."""
        from uuid import UUID
        user = await self._users.find_by_id(UUID(user_id))
        if not user:
            raise ResourceNotFoundException(
                "User not found", code="USER_NOT_FOUND"
            )
        return {
            "id": str(user.id),
            "username": user.username,
            "email": user.email,
            "role": user.role,
        }

    def _create_token(self, user: User) -> str:
        """Create a JWT token for the given user."""
        return self._jwt.encode({
            "sub": str(user.id),
            "username": user.username,
            "roles": [user.role],
            "permissions": self._get_permissions(user.role),
            "exp": datetime.now(UTC) + timedelta(hours=24),
            "iat": datetime.now(UTC),
        })

    @staticmethod
    def _get_permissions(role: str) -> list[str]:
        """Map roles to permissions."""
        permission_map = {
            "USER": ["profile:read", "order:read", "order:create"],
            "ADMIN": [
                "profile:read", "profile:write",
                "order:read", "order:create", "order:delete",
                "user:read", "user:write", "user:delete",
            ],
        }
        return permission_map.get(role, [])
```

### Auth Controller: Login and Register

```python
from pydantic import BaseModel, Field

from pyfly.container import rest_controller
from pyfly.kernel.exceptions import UnauthorizedException, ConflictException
from pyfly.web import (
    request_mapping, get_mapping, post_mapping,
    exception_handler, Body,
)
from pyfly.security import SecurityContext, secure


class RegisterRequest(BaseModel):
    username: str = Field(..., min_length=3, max_length=50)
    email: str = Field(..., min_length=5)
    password: str = Field(..., min_length=8)


class LoginRequest(BaseModel):
    username: str
    password: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int = 86400  # 24 hours in seconds


@rest_controller
@request_mapping("/api/auth")
class AuthController:

    def __init__(self, auth_service: AuthService) -> None:
        self._auth = auth_service

    @post_mapping("/register", status_code=201)
    async def register(self, body: Body[RegisterRequest]) -> TokenResponse:
        token = await self._auth.register(
            username=body.username,
            email=body.email,
            password=body.password,
        )
        return TokenResponse(access_token=token)

    @post_mapping("/login")
    async def login(self, body: Body[LoginRequest]) -> TokenResponse:
        token = await self._auth.login(
            username=body.username,
            password=body.password,
        )
        return TokenResponse(access_token=token)

    @get_mapping("/me")
    @secure(roles=["USER", "ADMIN"])
    async def me(self, security_context: SecurityContext) -> dict:
        return await self._auth.get_current_user(security_context.user_id)

    # --- Exception Handlers ---

    @exception_handler(UnauthorizedException)
    async def handle_unauthorized(self, exc: UnauthorizedException):
        return 401, {
            "error": {
                "message": str(exc),
                "code": exc.code or "UNAUTHORIZED",
            }
        }

    @exception_handler(ConflictException)
    async def handle_conflict(self, exc: ConflictException):
        return 409, {
            "error": {
                "message": str(exc),
                "code": exc.code or "CONFLICT",
            }
        }
```

### Protected Controller: Role-Based Endpoints

```python
from pyfly.web import delete_mapping, PathVar


@rest_controller
@request_mapping("/api/admin/users")
class AdminUserController:

    def __init__(self, user_repo: UserRepository) -> None:
        self._users = user_repo

    @get_mapping("/")
    @secure(roles=["ADMIN"])
    async def list_users(self, security_context: SecurityContext) -> list[dict]:
        users = await self._users.find_all()
        return [
            {"id": str(u.id), "username": u.username, "role": u.role}
            for u in users
        ]

    @delete_mapping("/{user_id}", status_code=204)
    @secure(roles=["ADMIN"], permissions=["user:delete"])
    async def delete_user(
        self,
        user_id: PathVar[str],
        security_context: SecurityContext,
    ) -> None:
        from uuid import UUID
        await self._users.delete(UUID(user_id))
```

### Application Assembly

```python
from pyfly.web import CORSConfig
from pyfly.web.adapters.starlette import create_app
from pyfly.security import SecurityMiddleware, JWTService


def build_app(context):
    """Build the fully configured application."""
    app = create_app(
        title="My Application",
        version="1.0.0",
        description="Application with JWT authentication",
        context=context,
        docs_enabled=True,
        cors=CORSConfig(
            allowed_origins=["http://localhost:3000"],
            allowed_methods=["GET", "POST", "PUT", "DELETE"],
            allow_credentials=True,
        ),
    )

    # Add security middleware
    jwt_service = context.get_bean(JWTService)
    app.add_middleware(
        SecurityMiddleware,
        jwt_service=jwt_service,
        exclude_paths=[
            "/docs",
            "/redoc",
            "/openapi.json",
            "/api/auth/login",
            "/api/auth/register",
        ],
    )

    return app
```

### Testing the Flow

**1. Register a new user:**

```
POST /api/auth/register
Content-Type: application/json

{
    "username": "alice",
    "email": "alice@example.com",
    "password": "securepassword123"
}

Response 201:
{
    "access_token": "eyJhbGciOiJIUzI1NiI...",
    "token_type": "bearer",
    "expires_in": 86400
}
```

**2. Log in:**

```
POST /api/auth/login
Content-Type: application/json

{
    "username": "alice",
    "password": "securepassword123"
}

Response 200:
{
    "access_token": "eyJhbGciOiJIUzI1NiI...",
    "token_type": "bearer",
    "expires_in": 86400
}
```

**3. Access a protected endpoint:**

```
GET /api/auth/me
Authorization: Bearer eyJhbGciOiJIUzI1NiI...

Response 200:
{
    "id": "a1b2c3d4-...",
    "username": "alice",
    "email": "alice@example.com",
    "role": "USER"
}
```

**4. Access without a token:**

```
GET /api/auth/me

Response 401:
{
    "error": {
        "message": "Authentication required",
        "code": "AUTH_REQUIRED",
        "status": 401,
        "path": "/api/auth/me",
        "timestamp": "2026-02-14T10:30:00+00:00",
        "transaction_id": "..."
    }
}
```

**5. Access an admin-only endpoint without the ADMIN role:**

```
GET /api/admin/users/
Authorization: Bearer eyJhbGciOiJIUzI1NiI...  (token with role=USER)

Response 401:
{
    "error": {
        "message": "Insufficient roles: requires one of ['ADMIN']",
        "code": "FORBIDDEN",
        "status": 401,
        "path": "/api/admin/users/",
        "timestamp": "2026-02-14T10:30:00+00:00",
        "transaction_id": "..."
    }
}
```
