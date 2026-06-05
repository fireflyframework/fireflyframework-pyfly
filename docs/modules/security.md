# Security Guide

The PyFly security module provides a complete authentication and authorization system built around JWT tokens, password hashing, a request-scoped security context, middleware for automatic token processing, and a decorator for role- and permission-based access control. Like all PyFly modules, it follows hexagonal principles: the password encoder is defined as a protocol (port) with a bcrypt adapter, and the security context is a plain dataclass with no framework coupling.

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
  - [Integration with create_app()](#integration-with-create_app-1)
- [OAuth2](#oauth2)
  - [OAuth2 Resource Server (JWKS)](#oauth2-resource-server-jwks)
  - [OAuth2 Client Registration](#oauth2-client-registration)
  - [Built-in Provider Factories](#built-in-provider-factories)
  - [ClientRegistrationRepository](#clientregistrationrepository)
  - [OAuth2 Authorization Server](#oauth2-authorization-server)
  - [Issuing Tokens](#issuing-tokens)
  - [TokenStore Protocol](#tokenstore-protocol)
  - [Error Codes](#error-codes)
  - [OAuth2 Login Flow](#oauth2-login-flow)
  - [OAuth2LoginHandler](#oauth2loginhandler)
  - [OAuth2SessionSecurityFilter](#oauth2sessionsecurityfilter)
  - [Login Flow Configuration Example](#login-flow-configuration-example)
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

All components are exported from the top-level `pyfly.security` package:

```python
from pyfly.security import (
    SecurityContext,
    JWTService,
    PasswordEncoder,
    BcryptPasswordEncoder,
    SecurityMiddleware,
    secure,
)

# CSRF utilities
from pyfly.security.csrf import generate_csrf_token, validate_csrf_token
from pyfly.web.adapters.starlette.filters.csrf_filter import CsrfFilter

# OAuth2
from pyfly.security.oauth2 import (
    JWKSTokenValidator,
    ClientRegistration,
    ClientRegistrationRepository,
    InMemoryClientRegistrationRepository,
    AuthorizationServer,
    TokenStore,
    InMemoryTokenStore,
    google,
    github,
    keycloak,
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

| Expression | Description | Example |
|---|---|---|
| `hasRole('X')` | User has role X | `hasRole('ADMIN')` |
| `hasAnyRole('X', 'Y')` | User has at least one of the roles | `hasAnyRole('ADMIN', 'MANAGER')` |
| `hasPermission('X')` | User has permission X | `hasPermission('user:read')` |
| `isAuthenticated` | User is authenticated | `isAuthenticated` |
| `and` | Boolean AND | `hasRole('ADMIN') and hasPermission('write')` |
| `or` | Boolean OR | `hasRole('ADMIN') or hasRole('MANAGER')` |
| `not` | Boolean NOT | `not hasRole('GUEST')` |
| `(...)` | Grouping | `(hasRole('ADMIN') or hasRole('MANAGER')) and hasPermission('write')` |

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

#### Supported Expressions

| Expression | Description |
|-----------|-------------|
| `isAuthenticated` | User is authenticated |
| `hasRole('ADMIN')` | User has the ADMIN role |
| `hasAnyRole('ADMIN', 'MANAGER')` | User has at least one role |
| `hasPermission('order:read')` | User has the permission |
| `and` / `or` / `not` | Boolean operators |
| `(...)` | Grouping |

Both decorators raise `UnauthorizedException` (401) when no `SecurityContext` is available, and `ForbiddenException` (403) when the expression evaluates to `False`.

---

## OAuth2

PyFly provides a complete OAuth2 implementation following hexagonal architecture. The module includes a Resource Server for validating external tokens, Client Registration for connecting to OAuth2 providers, and an Authorization Server for issuing tokens.

```python
from pyfly.security.oauth2 import (
    # Resource Server
    JWKSTokenValidator,
    # Client Registration
    ClientRegistration,
    ClientRegistrationRepository,
    InMemoryClientRegistrationRepository,
    google,
    github,
    keycloak,
    # Authorization Server
    AuthorizationServer,
    TokenStore,
    InMemoryTokenStore,
)
```

### OAuth2 Resource Server (JWKS)

The `JWKSTokenValidator` validates RS256-signed JWTs using a remote JWKS (JSON Web Key Set) endpoint. This is used when your application acts as an **OAuth2 Resource Server** -- it receives tokens issued by an external authorization server and validates them.

```python
from pyfly.security.oauth2 import JWKSTokenValidator

validator = JWKSTokenValidator(
    jwks_uri="https://auth.example.com/.well-known/jwks.json",
    issuer="https://auth.example.com",
    audience="my-api",
)
```

**Constructor parameters:**

| Parameter | Type | Default | Description |
|---|---|---|---|
| `jwks_uri` | `str` | required | URL of the JWKS endpoint |
| `issuer` | `str \| None` | `None` | Expected `iss` claim (validates if set) |
| `audience` | `str \| None` | `None` | Expected `aud` claim (validates if set) |
| `algorithms` | `list[str] \| None` | `["RS256"]` | Allowed signing algorithms |

**Validating tokens:**

```python
# Validate and get raw payload
payload = validator.validate(token)
# {"sub": "user-123", "roles": ["ADMIN"], "scope": "read write", ...}

# Validate and build SecurityContext directly
ctx = validator.to_security_context(token)
# SecurityContext(user_id="user-123", roles=["ADMIN"], permissions=["read", "write"])
```

**Claim mapping for `to_security_context()`:**

| JWT Claim | SecurityContext Field | Notes |
|---|---|---|
| `sub` | `user_id` | Standard subject claim |
| `roles` | `roles` | Flat roles array |
| `realm_access.roles` | `roles` | Keycloak-style nested roles (fallback) |
| `permissions` | `permissions` | Flat permissions array |
| `scope` | `permissions` | Space-separated scopes (fallback, split on spaces) |

**Source:** `src/pyfly/security/oauth2/resource_server.py`

### OAuth2 Client Registration

`ClientRegistration` is a frozen dataclass that holds the configuration needed to interact with an OAuth2 provider.

```python
from pyfly.security.oauth2 import ClientRegistration

registration = ClientRegistration(
    registration_id="my-app",
    client_id="client-id-from-provider",
    client_secret="client-secret-from-provider",
    authorization_grant_type="authorization_code",
    redirect_uri="https://myapp.com/callback",
    scopes=["openid", "profile", "email"],
    authorization_uri="https://provider.com/authorize",
    token_uri="https://provider.com/token",
    user_info_uri="https://provider.com/userinfo",
    jwks_uri="https://provider.com/.well-known/jwks.json",
    issuer_uri="https://provider.com",
    provider_name="Custom Provider",
)
```

**Fields:**

| Field | Type | Default | Description |
|---|---|---|---|
| `registration_id` | `str` | required | Unique identifier for this registration |
| `client_id` | `str` | required | OAuth2 client ID |
| `client_secret` | `str` | `""` | OAuth2 client secret |
| `authorization_grant_type` | `str` | `"authorization_code"` | Grant type |
| `redirect_uri` | `str` | `""` | Redirect URI for auth code flow |
| `scopes` | `list[str]` | `[]` | Requested scopes |
| `authorization_uri` | `str` | `""` | Provider's authorization endpoint |
| `token_uri` | `str` | `""` | Provider's token endpoint |
| `user_info_uri` | `str` | `""` | Provider's userinfo endpoint |
| `jwks_uri` | `str` | `""` | Provider's JWKS endpoint |
| `issuer_uri` | `str` | `""` | Provider's issuer URI |
| `provider_name` | `str` | `""` | Human-readable provider name |

#### Built-in Provider Factories

Pre-configured factories for common OAuth2 providers:

```python
from pyfly.security.oauth2 import google, github, keycloak

# Google OAuth2
google_reg = google(
    client_id="your-google-client-id",
    client_secret="your-google-client-secret",
    redirect_uri="https://myapp.com/callback/google",
)

# GitHub OAuth2
github_reg = github(
    client_id="your-github-client-id",
    client_secret="your-github-client-secret",
)

# Keycloak (derives all endpoints from the issuer URI)
keycloak_reg = keycloak(
    client_id="your-keycloak-client-id",
    client_secret="your-keycloak-client-secret",
    issuer_uri="https://keycloak.example.com/realms/myrealm",
)
```

| Factory | Scopes | Grant Type |
|---|---|---|
| `google()` | `openid`, `profile`, `email` | `authorization_code` |
| `github()` | `read:user`, `user:email` | `authorization_code` |
| `keycloak()` | `openid`, `profile`, `email` | `authorization_code` |

#### ClientRegistrationRepository

The `ClientRegistrationRepository` protocol defines the port for looking up registrations:

```python
from pyfly.security.oauth2 import (
    ClientRegistrationRepository,
    InMemoryClientRegistrationRepository,
)

# Create a repository with registrations
repo = InMemoryClientRegistrationRepository(google_reg, github_reg, keycloak_reg)

# Look up by registration ID
reg = repo.find_by_registration_id("google")  # Returns ClientRegistration or None

# Add registrations after construction
repo.add(custom_registration)

# List all registrations
all_regs = repo.registrations  # list[ClientRegistration]
```

**Source:** `src/pyfly/security/oauth2/client.py`

### OAuth2 Authorization Server

The `AuthorizationServer` issues JWT access tokens and manages refresh tokens. It supports `client_credentials` (machine-to-machine) and `refresh_token` grant types.

```python
from pyfly.security.oauth2 import (
    AuthorizationServer,
    InMemoryTokenStore,
    InMemoryClientRegistrationRepository,
    ClientRegistration,
)

# Set up client registration
client = ClientRegistration(
    registration_id="my-service",
    client_id="my-service",
    client_secret="service-secret",
    scopes=["read", "write"],
)
client_repo = InMemoryClientRegistrationRepository(client)

# Create authorization server
auth_server = AuthorizationServer(
    secret="jwt-signing-secret",
    client_repository=client_repo,
    token_store=InMemoryTokenStore(),
    access_token_ttl=3600,       # 1 hour
    refresh_token_ttl=86400,     # 24 hours
    issuer="https://auth.myapp.com",
)
```

**Constructor parameters:**

| Parameter | Type | Default | Description |
|---|---|---|---|
| `secret` | `str` | required | Secret key for HS256 token signing |
| `client_repository` | `ClientRegistrationRepository` | required | Repository for client lookup |
| `token_store` | `TokenStore` | required | Storage for refresh tokens |
| `access_token_ttl` | `int` | `3600` | Access token lifetime (seconds) |
| `refresh_token_ttl` | `int` | `86400` | Refresh token lifetime (seconds) |
| `issuer` | `str \| None` | `None` | Token issuer (`iss` claim) |

#### Issuing Tokens

```python
# Client credentials grant (machine-to-machine)
response = await auth_server.token(
    grant_type="client_credentials",
    client_id="my-service",
    client_secret="service-secret",
    scope="read write",
)
# {
#     "access_token": "eyJhbGciOiJIUzI1NiI...",
#     "token_type": "Bearer",
#     "expires_in": 3600,
#     "refresh_token": "dGhpcyBpcyBhIHJlZnJlc2g...",
#     "scope": "read write"
# }

# Refresh token grant
new_response = await auth_server.token(
    grant_type="refresh_token",
    client_id="my-service",
    client_secret="service-secret",
    refresh_token=response["refresh_token"],
)
```

**Refresh token rotation:** When a refresh token is used, the old token is automatically revoked and a new one is issued. This limits the window of vulnerability if a token is compromised.

#### TokenStore Protocol

The `TokenStore` protocol defines the port for token persistence:

```python
class TokenStore(Protocol):
    async def store(self, token_id: str, token_data: dict[str, Any]) -> None: ...
    async def find(self, token_id: str) -> dict[str, Any] | None: ...
    async def revoke(self, token_id: str) -> None: ...
```

`InMemoryTokenStore` is the built-in adapter for development and testing. In production, implement `TokenStore` with Redis or a database backend.

#### Error Codes

| Error Code | Cause |
|---|---|
| `INVALID_CLIENT` | Unknown client ID or wrong secret |
| `INVALID_REQUEST` | Missing required parameter (e.g., refresh_token) |
| `UNSUPPORTED_GRANT_TYPE` | Grant type not supported |
| `INVALID_GRANT` | Invalid, expired, or mismatched refresh token |

**Source:** `src/pyfly/security/oauth2/authorization_server.py`

### OAuth2 Login Flow

The `OAuth2LoginHandler` implements the full browser-facing OAuth2 `authorization_code` flow. It creates Starlette routes that handle the redirect-to-provider, callback-with-code, and logout steps. The `OAuth2SessionSecurityFilter` complements it by restoring the `SecurityContext` from the HTTP session on subsequent requests.

```python
from pyfly.security.oauth2.login import OAuth2LoginHandler
from pyfly.security.oauth2.session_security_filter import OAuth2SessionSecurityFilter
```

#### OAuth2LoginHandler

`OAuth2LoginHandler` creates three routes:

| Route | Method | Description |
|---|---|---|
| `/oauth2/authorization/{registration_id}` | GET | Redirects the browser to the OAuth2 provider's authorization endpoint with a CSRF `state` parameter |
| `/login/oauth2/code/{registration_id}` | GET | Handles the provider callback: validates state, exchanges the authorization code for tokens, fetches user info, builds a `SecurityContext`, and stores it in the session |
| `/logout` | POST | Invalidates the HTTP session and redirects to `/` |

**Constructor parameters:**

| Parameter | Type | Description |
|---|---|---|
| `client_repository` | `ClientRegistrationRepository` | Repository to look up OAuth2 client registrations |

**Authorization flow:**

1. The user visits `/oauth2/authorization/google` (or any registration ID).
2. The handler looks up the `ClientRegistration`, generates a random `state` token, stores it in the session, and redirects the browser to the provider's `authorization_uri` with `response_type=code`, `client_id`, `redirect_uri`, `scope`, and `state` parameters.
3. The provider authenticates the user and redirects back to `/login/oauth2/code/google?code=...&state=...`.
4. The callback handler validates the `state` parameter (CSRF protection), exchanges the authorization code for tokens via the provider's `token_uri`, fetches user info from `user_info_uri`, builds a `SecurityContext`, and stores it in the session.
5. The user is redirected to the original page (or `/`).

```python
from pyfly.security.oauth2 import (
    ClientRegistrationRepository,
    InMemoryClientRegistrationRepository,
    google,
)
from pyfly.security.oauth2.login import OAuth2LoginHandler

# Set up client registrations
google_reg = google(
    client_id="your-google-client-id",
    client_secret="your-google-client-secret",
    redirect_uri="http://localhost:8080/login/oauth2/code/google",
)
client_repo = InMemoryClientRegistrationRepository(google_reg)

# Create the login handler
login_handler = OAuth2LoginHandler(client_repository=client_repo)

# Get the routes for mounting in create_app()
oauth2_routes = login_handler.routes()
```

**Source:** `src/pyfly/security/oauth2/login.py`

#### OAuth2SessionSecurityFilter

The `OAuth2SessionSecurityFilter` is a `OncePerRequestFilter` that restores the `SecurityContext` from the HTTP session on every request. It runs at `HIGHEST_PRECEDENCE + 225`, which is **after** the JWT-based `SecurityFilter` (at +220), so a session-established context overwrites a token-established one — session-based authentication takes priority over symmetric-token auth.

```python
from pyfly.security.oauth2.session_security_filter import OAuth2SessionSecurityFilter
```

**Behavior:**

1. Reads the session from `request.state.session`.
2. If a `SECURITY_CONTEXT` attribute is stored in the session (set by `OAuth2LoginHandler` during login), restores it to `request.state.security_context`.
3. If no session-based context is found and no `security_context` has been set by an earlier filter, sets an anonymous context.

This filter is complementary to the JWT `SecurityFilter`. In applications that use both OAuth2 login (session-based) and API tokens (JWT-based), the session filter runs first. If the user has an active session, the session context is used. If not, the JWT `SecurityFilter` gets its turn to check for a Bearer token.

| Property | Value |
|---|---|
| `__pyfly_order__` | `HIGHEST_PRECEDENCE + 225` |
| Runs after | `SecurityFilter` (HP+220) |
| Runs before | `HttpSecurityFilter` (HP+350) |

**Source:** `src/pyfly/security/oauth2/session_security_filter.py`

#### Login Flow Configuration Example

A complete example wiring OAuth2 login into a PyFly application:

```python
from pyfly.container import configuration, bean
from pyfly.security.oauth2 import (
    InMemoryClientRegistrationRepository,
    google, github,
)
from pyfly.security.oauth2.login import OAuth2LoginHandler
from pyfly.security.oauth2.session_security_filter import OAuth2SessionSecurityFilter
from pyfly.security.http_security import HttpSecurity


@configuration
class OAuth2Config:

    @bean
    def client_repository(self) -> InMemoryClientRegistrationRepository:
        return InMemoryClientRegistrationRepository(
            google(
                client_id="google-client-id",
                client_secret="google-client-secret",
                redirect_uri="http://localhost:8080/login/oauth2/code/google",
            ),
            github(
                client_id="github-client-id",
                client_secret="github-client-secret",
                redirect_uri="http://localhost:8080/login/oauth2/code/github",
            ),
        )

    @bean
    def oauth2_login_handler(self, client_repository: InMemoryClientRegistrationRepository) -> OAuth2LoginHandler:
        return OAuth2LoginHandler(client_repository=client_repository)

    @bean
    def oauth2_session_filter(self) -> OAuth2SessionSecurityFilter:
        return OAuth2SessionSecurityFilter()

    @bean
    def http_security_filter(self):
        http_security = HttpSecurity()
        http_security.authorize_requests() \
            .request_matchers("/oauth2/**", "/login/**", "/logout").permit_all() \
            .request_matchers("/api/**").authenticated() \
            .any_request().permit_all()
        return http_security.build()
```

Then mount the OAuth2 routes via `extra_routes` in `create_app()`:

```python
from pyfly.web.adapters.starlette import create_app

login_handler = context.get_bean(OAuth2LoginHandler)
app = create_app(
    title="My App",
    context=context,
    extra_routes=login_handler.routes(),
)
```

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
