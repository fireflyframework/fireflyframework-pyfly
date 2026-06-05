# WebFilter Chain Guide

PyFly uses a WebFilter chain architecture for HTTP request/response processing.
Instead of registering multiple Starlette middlewares independently, all filters
run inside a single `WebFilterChainMiddleware`, reducing per-middleware task-context
overhead and enabling centralized ordering and URL-pattern matching.

---

## Table of Contents

1. [Architecture](#architecture)
2. [WebFilter Protocol](#webfilter-protocol)
3. [OncePerRequestFilter Base Class](#onceperrequestfilter-base-class)
4. [Built-in Filters](#built-in-filters)
   - [TransactionIdFilter](#transactionidfilter)
   - [RequestLoggingFilter](#requestloggingfilter)
   - [SecurityHeadersFilter](#securityheadersfilter)
   - [CsrfFilter](#csrffilter)
   - [OAuth2SessionSecurityFilter](#oauth2sessionsecurityfilter)
   - [SecurityFilter](#securityfilter)
   - [HttpSecurityFilter](#httpsecurityfilter)
5. [Filter Ordering with @order](#filter-ordering-with-order)
6. [URL Pattern Matching](#url-pattern-matching)
7. [Creating Custom Filters](#creating-custom-filters)
   - [Extending OncePerRequestFilter](#extending-onceperrequestfilter)
   - [Implementing WebFilter Directly](#implementing-webfilter-directly)
8. [Auto-Discovery from DI](#auto-discovery-from-di)
9. [WebFilterChainMiddleware Internals](#webfilterchainmiddleware-internals)
10. [Complete Example](#complete-example)

---

## Architecture

The WebFilter chain replaces direct middleware registration:

```
Request
   |
   v
WebFilterChainMiddleware (pure ASGI middleware)
   |
   +-- RequestContextFilter          (__pyfly_order__ = HIGHEST_PRECEDENCE)
   +-- CorrelationFilter             (@order HIGHEST_PRECEDENCE + 50)
   +-- TransactionIdFilter           (@order HIGHEST_PRECEDENCE + 100)
   +-- RequestLoggingFilter          (@order HIGHEST_PRECEDENCE + 200)
   +-- SecurityFilter                (@order HIGHEST_PRECEDENCE + 220, opt-in JWT auth)
   +-- OAuth2SessionSecurityFilter   (@order HIGHEST_PRECEDENCE + 225, opt-in)
   +-- SecurityHeadersFilter         (@order HIGHEST_PRECEDENCE + 300)
   +-- HttpSecurityFilter            (@order HIGHEST_PRECEDENCE + 350, opt-in)
   +-- CsrfFilter                    (__pyfly_order__ = -50, opt-in)
   +-- [User WebFilter beans, sorted by @order]
   |
   v
Route Handler
   |
   v
Response (filters execute in reverse on the way out)
```

All filters run within a single middleware boundary. The chain is built from right
to left: the first filter (lowest `@order` value) wraps the outermost layer.

**Source:** `src/pyfly/web/adapters/starlette/filter_chain.py`

---

## WebFilter Protocol

The `WebFilter` protocol (`pyfly.web.ports.filter`) defines the contract for all
filters. It is framework-agnostic, using `Any` for request/response types:

```python
from pyfly.web.ports.filter import WebFilter, CallNext

@runtime_checkable
class WebFilter(Protocol):
    async def do_filter(self, request: Any, call_next: CallNext) -> Any:
        """Execute filter logic. Must call await call_next(request) to proceed."""
        ...

    def should_not_filter(self, request: Any) -> bool:
        """Return True to skip this filter for the given request."""
        ...
```

| Method | Purpose |
|---|---|
| `do_filter(request, call_next)` | Core filter logic. Call `await call_next(request)` to invoke the next filter or the route handler. |
| `should_not_filter(request)` | Return `True` to bypass this filter for the current request. |

The `CallNext` type alias is `Callable[..., Coroutine[Any, Any, Any]]`.

**Source:** `src/pyfly/web/ports/filter.py`

---

## OncePerRequestFilter Base Class

`OncePerRequestFilter` (`pyfly.web.filters`) is the recommended base class for
custom filters. It provides automatic URL-pattern matching via `url_patterns` and
`exclude_patterns`, so subclasses only need to implement `do_filter()`.

```python
from pyfly.web.filters import OncePerRequestFilter

class OncePerRequestFilter(abc.ABC):
    url_patterns: list[str] = []       # Glob patterns to include
    exclude_patterns: list[str] = []   # Glob patterns to exclude

    def should_not_filter(self, request: Any) -> bool:
        """Automatic path matching using fnmatch."""
        ...

    @abc.abstractmethod
    async def do_filter(self, request: Any, call_next: CallNext) -> Any:
        """Subclasses implement this."""
        ...
```

| Attribute | Type | Default | Description |
|---|---|---|---|
| `url_patterns` | `list[str]` | `[]` | If set, at least one pattern must match the request path. Empty = match all. |
| `exclude_patterns` | `list[str]` | `[]` | If any pattern matches, the filter is skipped. Checked after `url_patterns`. |

Patterns use `fnmatch` glob syntax: `*` matches any sequence, `?` matches a single
character, `[seq]` matches character sets.

**Source:** `src/pyfly/web/filters.py`

---

## Built-in Filters

### TransactionIdFilter

Propagates or generates a unique `X-Transaction-Id` header for distributed tracing.

```python
@order(HIGHEST_PRECEDENCE + 100)
class TransactionIdFilter(OncePerRequestFilter):
    async def do_filter(self, request, call_next):
        tx_id = request.headers.get("X-Transaction-Id") or str(uuid.uuid4())
        request.state.transaction_id = tx_id
        response = await call_next(request)
        response.headers["X-Transaction-Id"] = tx_id
        return response
```

- Checks for an incoming `X-Transaction-Id` header; generates a UUID if absent.
- Stores the ID on `request.state.transaction_id` for downstream access.
- Adds the header to the response.

**Source:** `src/pyfly/web/adapters/starlette/filters/transaction_id_filter.py`

### RequestLoggingFilter

Logs every HTTP request with structured fields using `structlog`.

```python
@order(HIGHEST_PRECEDENCE + 200)
class RequestLoggingFilter(OncePerRequestFilter):
    async def do_filter(self, request, call_next):
        start = time.perf_counter()
        response = await call_next(request)
        duration_ms = (time.perf_counter() - start) * 1000
        logger.info("http_request", method=..., path=..., status_code=..., duration_ms=...)
        return response
```

Logged fields: `method`, `path`, `status_code`, `duration_ms`, `transaction_id`.
Failed requests are logged at `error` level with `error` and `error_type` fields.

**Source:** `src/pyfly/web/adapters/starlette/filters/request_logging_filter.py`

### SecurityHeadersFilter

Adds OWASP-recommended security headers to every response.

```python
@order(HIGHEST_PRECEDENCE + 300)
class SecurityHeadersFilter(OncePerRequestFilter):
    def __init__(self, config: SecurityHeadersConfig | None = None):
        self._config = config or SecurityHeadersConfig()

    async def do_filter(self, request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        # ... more headers from config
        return response
```

Uses `SecurityHeadersConfig` defaults if no config is provided.

**Source:** `src/pyfly/web/adapters/starlette/filters/security_headers_filter.py`

### CsrfFilter

Implements the [double-submit cookie](https://cheatsheetseries.owasp.org/cheatsheets/Cross-Site_Request_Forgery_Prevention_Cheat_Sheet.html#double-submit-cookie) pattern for CSRF protection.

```python
class CsrfFilter(OncePerRequestFilter):
    __pyfly_order__ = -50
    exclude_patterns = ["/actuator/*", "/health", "/ready"]

    async def do_filter(self, request, call_next):
        # Safe methods (GET, HEAD, OPTIONS, TRACE): pass through + set XSRF-TOKEN cookie
        # Bearer bypass: requests with "Authorization: Bearer ..." skip CSRF
        # Unsafe methods (POST, PUT, DELETE, PATCH): validate cookie vs header
        ...
```

**How it works:**

| Request Type | Behaviour |
|---|---|
| Safe methods (`GET`, `HEAD`, `OPTIONS`, `TRACE`) | Pass through; set/refresh `XSRF-TOKEN` cookie on the response |
| Bearer token present (`Authorization: Bearer …`) | Skip CSRF validation (stateless JWT clients are exempt) |
| Unsafe methods (`POST`, `PUT`, `DELETE`, `PATCH`) | Compare `XSRF-TOKEN` cookie against `X-XSRF-TOKEN` header using `secrets.compare_digest`; return 403 on mismatch or missing token |

**Cookie settings:** `httponly=False` (JavaScript must read the token), `samesite="lax"`, `secure=True`, `path="/"`.

After successful validation on unsafe methods, the token is rotated — a fresh token is set on the response cookie.

**Source:** `src/pyfly/web/adapters/starlette/filters/csrf_filter.py`

### OAuth2SessionSecurityFilter

Restores a `SecurityContext` from the HTTP session for OAuth2 login-based authentication.

```python
from pyfly.security.oauth2.session_security_filter import OAuth2SessionSecurityFilter

class OAuth2SessionSecurityFilter(OncePerRequestFilter):
    __pyfly_order__ = HIGHEST_PRECEDENCE + 225

    async def do_filter(self, request, call_next):
        session = request.state.session
        stored_ctx = session.get_attribute("SECURITY_CONTEXT")
        if isinstance(stored_ctx, SecurityContext) and stored_ctx.is_authenticated:
            request.state.security_context = stored_ctx
            return await call_next(request)
        # No session context — set anonymous if no other filter has set one
        if not hasattr(request.state, "security_context"):
            request.state.security_context = SecurityContext.anonymous()
        return await call_next(request)
```

- Runs at `HIGHEST_PRECEDENCE + 225`, **after** the JWT `SecurityFilter` (at +220) but before the `OAuth2ResourceServerFilter` (at +250).
- Reads the `SECURITY_CONTEXT` attribute from the HTTP session (stored by `OAuth2LoginHandler` during login).
- If a valid authenticated context is found, sets it on `request.state.security_context`.
- Otherwise, sets an anonymous context so downstream filters always have a context available.
- Used in applications with browser-based OAuth2 login flows (authorization_code grant).

**Source:** `src/pyfly/security/oauth2/session_security_filter.py`

### SecurityFilter

Extracts JWT Bearer tokens and populates `request.state.security_context`.

```python
class SecurityFilter(OncePerRequestFilter):
    def __init__(self, jwt_service: JWTService, exclude_patterns: Sequence[str] = ()):
        self._jwt_service = jwt_service
        self.exclude_patterns = list(exclude_patterns)

    async def do_filter(self, request, call_next):
        auth_header = request.headers.get("authorization", "")
        if auth_header.startswith("Bearer "):
            token = auth_header[7:]
            security_context = self._jwt_service.to_security_context(token)
        else:
            security_context = SecurityContext.anonymous()
        request.state.security_context = security_context
        return await call_next(request)
```

Uses `exclude_patterns` to skip public endpoints (e.g., `/actuator/*`, `/docs`).

**Source:** `src/pyfly/web/adapters/starlette/filters/security_filter.py`

### HttpSecurityFilter

Enforces URL-pattern authorization rules configured via the `HttpSecurity` DSL. Returns RFC 7807 problem-detail responses for unauthorized or forbidden requests.

```python
from pyfly.web.adapters.starlette.filters.http_security_filter import HttpSecurityFilter

@order(HIGHEST_PRECEDENCE + 350)
class HttpSecurityFilter(OncePerRequestFilter):
    def __init__(self, rules: Sequence[SecurityRule]) -> None:
        self._rules = list(rules)

    async def do_filter(self, request, call_next):
        path = request.url.path
        security_context = getattr(request.state, "security_context", SecurityContext.anonymous())
        for rule in self._rules:
            if _matches(path, rule.patterns):
                # Evaluate rule type: PERMIT_ALL, DENY_ALL, AUTHENTICATED, HAS_ROLE, etc.
                ...
        # No rule matched — allow through
        return await call_next(request)
```

- Runs at `HIGHEST_PRECEDENCE + 350`, **after** all authentication filters (SecurityFilter, OAuth2SessionSecurityFilter).
- Evaluates rules in declaration order; **first match wins**.
- If no rule matches, the request is allowed through (open by default).
- Constructed via `HttpSecurity.build()` rather than directly.

**Rule evaluation:**

| Rule Type | Unauthenticated | Authenticated (no matching role/perm) | Authenticated (matching) |
|---|---|---|---|
| `PERMIT_ALL` | 200 (pass) | 200 (pass) | 200 (pass) |
| `DENY_ALL` | 403 | 403 | 403 |
| `AUTHENTICATED` | 401 | 200 (pass) | 200 (pass) |
| `HAS_ROLE` | 401 | 403 | 200 (pass) |
| `HAS_ANY_ROLE` | 401 | 403 | 200 (pass) |
| `HAS_PERMISSION` | 401 | 403 | 200 (pass) |

**Source:** `src/pyfly/web/adapters/starlette/filters/http_security_filter.py`

---

## Filter Ordering with @order

Filters execute in `@order` value order (lower = runs first). Built-in filters use
`HIGHEST_PRECEDENCE` (a large negative number) to ensure they run before user filters.

```python
from pyfly.container.ordering import order, HIGHEST_PRECEDENCE

# Built-in order values:
# RequestContextFilter:          HIGHEST_PRECEDENCE         (request-scoped context)
# CorrelationFilter:             HIGHEST_PRECEDENCE + 50    (W3C/correlation headers)
# TransactionIdFilter:           HIGHEST_PRECEDENCE + 100
# RequestLoggingFilter:          HIGHEST_PRECEDENCE + 200
# SecurityFilter:                HIGHEST_PRECEDENCE + 220   (opt-in, JWT authentication)
# OAuth2SessionSecurityFilter:   HIGHEST_PRECEDENCE + 225   (opt-in)
# SecurityHeadersFilter:         HIGHEST_PRECEDENCE + 300
# HttpSecurityFilter:            HIGHEST_PRECEDENCE + 350   (opt-in)
# CsrfFilter:                    -50                        (opt-in)

# User filters default to order 0 (run after built-ins)

@order(10)
class RateLimitFilter(OncePerRequestFilter):
    """Runs after built-in filters but before other user filters."""
    ...

@order(50)
class AuditLogFilter(OncePerRequestFilter):
    """Runs after RateLimitFilter."""
    ...
```

If no `@order` is specified, the default order value is `0`.

---

## URL Pattern Matching

`OncePerRequestFilter` supports glob-based URL pattern matching:

```python
class ApiOnlyFilter(OncePerRequestFilter):
    url_patterns = ["/api/*"]            # Only run on /api/* paths
    exclude_patterns = ["/api/public/*"] # But skip /api/public/*

    async def do_filter(self, request, call_next):
        # Only executes for /api/* paths (excluding /api/public/*)
        return await call_next(request)
```

**Pattern evaluation:**
1. If `url_patterns` is non-empty, at least one pattern must match → otherwise skip.
2. If `exclude_patterns` is non-empty, any matching pattern → skip.
3. If both are empty → filter runs on all requests.

---

## Creating Custom Filters

### Extending OncePerRequestFilter

The recommended approach — gives you automatic URL-pattern matching:

```python
from pyfly.container import component
from pyfly.container.ordering import order
from pyfly.web.filters import OncePerRequestFilter
from pyfly.web.ports.filter import CallNext


@component
@order(20)
class RateLimitFilter(OncePerRequestFilter):
    """Rate limits API requests to 100 req/min per IP."""

    url_patterns = ["/api/*"]
    exclude_patterns = ["/api/health"]

    async def do_filter(self, request, call_next: CallNext):
        client_ip = request.client.host
        # Rate limiting logic here...
        response = await call_next(request)
        response.headers["X-RateLimit-Remaining"] = str(remaining)
        return response
```

### Implementing WebFilter Directly

For full control without the `OncePerRequestFilter` base class:

```python
@component
@order(30)
class CorrelationIdFilter:
    """Implements WebFilter protocol directly."""

    async def do_filter(self, request, call_next):
        correlation_id = request.headers.get("X-Correlation-Id", str(uuid.uuid4()))
        request.state.correlation_id = correlation_id
        response = await call_next(request)
        response.headers["X-Correlation-Id"] = correlation_id
        return response

    def should_not_filter(self, request) -> bool:
        return False  # Run on all requests
```

---

## Auto-Discovery from DI

When `create_app()` is called with an `ApplicationContext`, it automatically
discovers any beans implementing the `WebFilter` protocol:

```python
# In create_app():
builtin_filter_types = (
    RequestContextFilter, CorrelationFilter,
    TransactionIdFilter, RequestLoggingFilter, SecurityHeadersFilter,
)
for _cls, reg in context.container._registrations.items():
    inst = reg.instance
    if inst is not None and isinstance(inst, WebFilter) and not isinstance(inst, builtin_filter_types):
        filters.append(inst)

# Sort all filters (built-in + user) by @order; done in-place so the live list
# (shared with WebFilterChainMiddleware) stays ordered.
filters.sort(key=lambda f: get_order(type(f)))
```

This means you only need to:
1. Decorate your filter with `@component` (or any stereotype).
2. Ensure it implements `WebFilter` (either directly or via `OncePerRequestFilter`).
3. Optionally set `@order` for execution priority.

The filter will be automatically included in the chain.

---

## WebFilterChainMiddleware Internals

`WebFilterChainMiddleware` is a pure ASGI middleware (not Starlette's
`BaseHTTPMiddleware`). This avoids the anyio dependency that caused
`ModuleNotFoundError` with Granian. The chain is built at request time from right
to left:

```python
class WebFilterChainMiddleware:
    def __init__(self, app: ASGIApp, filters: Sequence[WebFilter] = ()) -> None:
        self.app = app
        self._filters = list(filters)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        request = Request(scope, receive, send)

        async def _call_app(req: Any) -> Response:
            # Intercept ASGI send to capture status, headers, and body
            ...
            await self.app(scope, receive, _intercept)
            response = Response(content=..., status_code=status_code)
            response.raw_headers[:] = raw_headers
            return response

        chain: CallNext = _call_app
        for f in reversed(self._filters):
            chain = _wrap(f, chain)
        response = await chain(request)
        await response(scope, receive, send)

def _wrap(web_filter, next_call):
    async def _inner(request):
        if web_filter.should_not_filter(request):
            return await next_call(request)
        return await web_filter.do_filter(request, next_call)
    return _inner
```

The terminal `_call_app` function captures the downstream ASGI response by
intercepting `send` calls, buffering body parts, and reconstructing a Starlette
`Response` object. This preserves the `call_next(request) -> Response` contract
that `WebFilter.do_filter()` expects.

The reversed iteration means the first filter in the sorted list becomes the
outermost wrapper, executing first on the request and last on the response.

**Source:** `src/pyfly/web/adapters/starlette/filter_chain.py`

---

## Complete Example

```python
from pyfly.container import component
from pyfly.container.ordering import order
from pyfly.web.filters import OncePerRequestFilter
from pyfly.web.ports.filter import CallNext


@component
@order(10)
class TenantFilter(OncePerRequestFilter):
    """Extracts tenant ID from X-Tenant-Id header for multi-tenancy."""

    url_patterns = ["/api/*"]

    async def do_filter(self, request, call_next: CallNext):
        tenant_id = request.headers.get("x-tenant-id")
        if not tenant_id:
            from starlette.responses import JSONResponse
            return JSONResponse(
                {"error": "X-Tenant-Id header is required"},
                status_code=400,
            )
        request.state.tenant_id = tenant_id
        return await call_next(request)


@component
@order(50)
class RequestTimingFilter(OncePerRequestFilter):
    """Adds X-Response-Time header to all responses."""

    async def do_filter(self, request, call_next: CallNext):
        import time
        start = time.perf_counter()
        response = await call_next(request)
        duration_ms = (time.perf_counter() - start) * 1000
        response.headers["X-Response-Time"] = f"{duration_ms:.2f}ms"
        return response
```

With these beans registered, `create_app()` produces this filter chain:

```
RequestContextFilter          (HIGHEST_PRECEDENCE)
CorrelationFilter             (HIGHEST_PRECEDENCE + 50)
TransactionIdFilter           (HIGHEST_PRECEDENCE + 100)
RequestLoggingFilter          (HIGHEST_PRECEDENCE + 200)
SecurityFilter                (HIGHEST_PRECEDENCE + 220)   [if registered]
OAuth2SessionSecurityFilter   (HIGHEST_PRECEDENCE + 225)   [if registered]
SecurityHeadersFilter         (HIGHEST_PRECEDENCE + 300)
HttpSecurityFilter            (HIGHEST_PRECEDENCE + 350)   [if registered]
CsrfFilter                    (-50)                        [if registered]
TenantFilter                  (10)
RequestTimingFilter           (50)
```
