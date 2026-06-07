<span class="eyebrow">Chapter 14</span>

# Security, Sessions & Identity {.chtitle}

::: figure art/openers/ch14.svg | &nbsp;

In Chapter 13 you made Lumen fast and fault-tolerant with caching and resilience
decorators. Lumen's API now handles high concurrency without breaking under
pressure — but it is wide open. Any caller can create wallets, read balances, or
trigger deposits. Before you can ship Part V's remaining production concerns, you
need to close that door.

This chapter locks Lumen down. You will:

- **Authenticate** every request with a signed JWT, using `JWTService` to issue
  and validate tokens and `SecurityMiddleware` to propagate the `SecurityContext`
  across the request scope.
- **Authorise** individual handlers and commands with the `@secure` decorator,
  specifying roles, permissions, or full security expressions.
- **Hash passwords** safely with `BcryptPasswordEncoder` so your user store is
  never a liability.
- **Manage server-side sessions** with `HttpSession`, a pluggable `SessionStore`,
  and a Redis backend for horizontal scaling.
- **Federate identity** to an external provider — Keycloak, AWS Cognito, or
  Azure AD — through the `IdpAdapter` port without changing a line of
  business logic.

The path mirrors what you would do in Spring Boot: start with a
`SecurityFilterChain`, add method-level annotations, and swap the
`UserDetailsService` for an IDP. PyFly calls them
`SecurityMiddleware + HttpSecurity`, `@secure`, and `IdpAdapter`, but the
concepts are identical.

::: figure art/figures/14-security.svg | Figure 14.1 — Lumen's security layers. A JWT filter populates the SecurityContext; HttpSecurity enforces URL-level rules; @secure enforces handler-level rules; the IDP port delegates identity to an external provider.

---

## Authentication with JWT

### Why JSON Web Tokens?

Lumen is a reactive, stateless API. HTTP sessions would require sticky routing
or a shared session store for every request. JWT tokens let each service
validate credentials independently — no shared state, no coordination between
replicas, horizontal scaling by default.

A JWT is a signed JSON payload. Lumen's auth service issues a token on login;
every subsequent request carries that token in the `Authorization` header; the
`SecurityMiddleware` validates the signature and unpacks the token into a
`SecurityContext` that the rest of the request can read.

### JWTService

`JWTService` wraps PyJWT with three focused operations:

| Method | Description |
|---|---|
| `encode(payload)` | Sign a payload dict, adding `exp` if absent |
| `decode(token)` | Validate signature + `exp`; raises `SecurityException` on failure |
| `to_security_context(token)` | Decode and extract `sub`, `roles`, `permissions` into a `SecurityContext` |

The service always requires an `exp` claim — a token with no expiry is rejected
at decode time. That invariant means every token in circulation has a bounded
lifetime.

::: listing lumen/core/services/auth/auth_service.py | Listing 14.1 — Issuing a JWT on successful login
from pyfly.container import service
from pyfly.kernel.exceptions import UnauthorizedException
from pyfly.security import BcryptPasswordEncoder, JWTService, SecurityContext


@service
class AuthService:

    def __init__(
        self,
        jwt: JWTService,
        encoder: BcryptPasswordEncoder,
        user_repo,
    ) -> None:
        self._jwt = jwt
        self._encoder = encoder
        self._users = user_repo

    async def login(self, username: str, password: str) -> str:
        user = await self._users.find_by_username(username)
        if user is None or not self._encoder.verify(
            password, user.password_hash
        ):
            raise UnauthorizedException(
                "Invalid credentials", code="INVALID_CREDENTIALS"
            )
        # encode() auto-appends exp (default: 3 600 s from now)
        return self._jwt.encode({
            "sub": str(user.id),
            "roles": [user.role],
            "permissions": _permissions_for(user.role),
        })

    async def me(self, ctx: SecurityContext) -> dict:
        user = await self._users.find_by_id(ctx.user_id)
        return {
            "id": str(user.id),
            "username": user.username,
            "role": user.role,
        }


def _permissions_for(role: str) -> list[str]:
    MAP = {
        "USER":  ["wallet:read", "wallet:deposit"],
        "ADMIN": [
            "wallet:read", "wallet:deposit",
            "wallet:create", "wallet:delete",
            "user:read", "user:write",
        ],
    }
    return MAP.get(role, [])

:::
:::

**How it works.** `login` fetches the user record and uses
`BcryptPasswordEncoder.verify` to compare the supplied password with the stored
hash. On success it calls `jwt.encode`, which auto-appends an `exp` claim
`expiration_seconds` seconds from now (default `3600` — one hour). There is no
need to import `datetime`: the service adds a Unix-timestamp `exp` using
`int(time.time()) + expiration_seconds` internally. The caller receives a
compact, self-contained token string.

### The SecurityContext

`SecurityContext` is a frozen dataclass that holds authentication and
authorisation data for one request. The middleware creates it from a token; your
handlers receive it as an injected parameter.

| Field | Type | Description |
|---|---|---|
| `user_id` | `str \| None` | Authenticated user's id; `None` for anonymous |
| `roles` | `list[str]` | Roles granted in the token |
| `permissions` | `list[str]` | Fine-grained permissions |
| `attributes` | `dict[str, str]` | Extra claims (department, tenant, …) |

Key methods:

| Method / Property | Returns | Description |
|---|---|---|
| `is_authenticated` | `bool` | `True` when `user_id` is not `None` |
| `has_role(role)` | `bool` | Exact match against `roles` list |
| `has_any_role(roles)` | `bool` | Set intersection — any of the listed roles |
| `has_permission(perm)` | `bool` | Exact match against `permissions` list |
| `SecurityContext.anonymous()` | `SecurityContext` | Create an unauthenticated context |

### The security filter

The `SecurityMiddleware` (canonical location
`pyfly.web.adapters.starlette.security_middleware`, re-exported from
`pyfly.security`) sits at the Starlette middleware layer. On every request it:

1. Checks whether the path is in `exclude_paths`; if so, sets an anonymous
   context and continues.
2. Reads the `Authorization` header; strips the `Bearer ` prefix.
3. Calls `jwt_service.to_security_context(token)`.
4. On success sets `request.state.security_context` to the authenticated
   context.
5. On any `SecurityException` (expired, tampered, missing `exp`) logs at DEBUG
   and sets an anonymous context.

The middleware **never rejects requests** — that is the job of `@secure` and
`HttpSecurity`. A handler that needs the user can choose to require
authentication; a health-check endpoint can ignore it entirely.

::: listing lumen/app.py | Listing 14.2 — Adding the security middleware
from pyfly.security import JWTService, SecurityMiddleware
from pyfly.web.adapters.starlette import create_app


def build_app(context):
    app = create_app(title="Lumen", context=context)

    jwt = context.get_bean(JWTService)
    app.add_middleware(
        SecurityMiddleware,
        jwt_service=jwt,
        exclude_paths=[
            "/docs",
            "/openapi.json",
            "/api/auth/login",
            "/api/auth/register",
        ],
    )
    return app

:::
:::

**How it works.** `exclude_paths` contains paths where no token is expected —
the login and register endpoints cannot require authentication because the token
does not exist yet. Docs paths are excluded so your API explorer works without
credentials. Every other path goes through token validation.

### URL-level rules with HttpSecurity

`@secure` guards individual handler methods. `HttpSecurity` guards whole URL
subtrees at the filter layer — before the route dispatcher even runs. The two
are complementary: `HttpSecurity` provides fast, central policy; `@secure`
provides fine-grained, per-handler policy.

::: listing lumen/config/security_config.py | Listing 14.3 — HttpSecurity DSL
from pyfly.container import bean, configuration
from pyfly.security.http_security import HttpSecurity


@configuration
class SecurityConfig:

    @bean
    def http_security_filter(self):
        hs = HttpSecurity()
        hs.authorize_requests() \
            .request_matchers("/idp/admin/**").has_role("ADMIN") \
            .request_matchers("/api/v1/wallets/**").authenticated() \
            .request_matchers(
                "/health", "/docs", "/openapi.json",
                "/idp/login", "/idp/refresh",
            ).permit_all() \
            .any_request().permit_all()
        return hs.build()

:::
:::

**How it works.** Rules are evaluated in declaration order — first match wins.
The `HttpSecurityFilter` runs at `HIGHEST_PRECEDENCE + 350`, after
authentication filters have populated `request.state.security_context`, so the
role and permission checks have a fully hydrated context to inspect. The
`has_role`, `has_any_role`, `has_permission`, `authenticated`, `permit_all`, and
`deny_all` terminal methods cover every common policy; unsatisfied rules return
RFC 7807 problem-detail JSON (`application/problem+json`) with the appropriate
HTTP status.

!!! note "Two-layer defense"
    `HttpSecurity` provides fast URL-level policy before routes are even
    dispatched — good for blanket rules like "everything under `/api/v1/wallets`
    needs authentication." The `@secure` decorators on individual methods are
    the second, finer-grained layer. Use both together for defense in depth.

!!! spring "Spring parity"
    `HttpSecurity` mirrors Spring Security's `HttpSecurity.authorizeHttpRequests()` chain. `request_matchers` corresponds to `requestMatchers`, `authenticated()` to `.authenticated()`, `has_role` to `hasRole`, and `build()` triggers registration of the underlying filter just as `build()` finalises the Spring filter chain. The fnmatch glob patterns (`/api/admin/**`) behave identically to Spring's Ant-style path matching.

---

## Auto-configuration

You do not need to register `JWTService` or `BcryptPasswordEncoder` by hand.
Add two properties to `pyfly.yaml` and auto-configuration wires everything:

::: listing lumen/resources/pyfly.yaml | Listing 14.4 — Security auto-configuration
pyfly:
  security:
    enabled: true
    jwt:
      secret: "${JWT_SECRET}"
      algorithm: HS256
      filter:
        enabled: true
        exclude-patterns: >-
          /docs,/openapi.json,/actuator/health,
          /api/auth/login,/api/auth/register
    password:
      bcrypt-rounds: 12

:::
:::

| Property | Default | Description |
|---|---|---|
| `pyfly.security.jwt.secret` | `change-me-in-production` | HMAC signing key — **must** be overridden |
| `pyfly.security.jwt.algorithm` | `HS256` | Signing algorithm |
| `pyfly.security.jwt.filter.enabled` | *(absent)* | Set `true` to register `SecurityFilter` bean automatically |
| `pyfly.security.jwt.exclude-patterns` | *(absent)* | Comma-separated paths to skip |
| `pyfly.security.password.bcrypt-rounds` | `12` | Bcrypt cost factor |

!!! warning "Production secret"
    Never commit the real JWT secret to source control. Use `${JWT_SECRET}` and
    inject the value from an environment variable or a secrets manager at
    deploy time.

---

## Authorization with @secure

`@secure` is a function decorator that enforces authentication and authorisation
on individual handlers and commands. It reads the `security_context` keyword
argument that the middleware has already injected and evaluates role, permission,
and expression checks before the function body runs.

### Signature

```python
def secure(
    roles: list[str] | None = None,
    permissions: list[str] | None = None,
    expression: str | None = None,
) -> Callable: ...
```

The decorated function must accept `security_context: SecurityContext` as a
keyword argument — that is how `@secure` accesses the current user.

### Role-based protection

Lumen's wallet endpoints (`/api/v1/wallets`) are the natural place to apply
`@secure`. The real `WalletController` injects the command and query buses; you
add `security_context: SecurityContext` to each method that needs protection
and stack `@secure` on top.

::: listing lumen/web/controllers/wallet_controller.py | Listing 14.5 — Role and permission guards on real Lumen endpoints
from lumen.core.services.wallets.deposit_funds_command import DepositFunds
from lumen.core.services.wallets.get_balance_query import GetBalance
from lumen.core.services.wallets.get_wallet_query import GetWallet
from lumen.core.services.wallets.open_wallet_command import OpenWallet
from lumen.interfaces.dtos.v1.balance_dto import BalanceDto
from lumen.interfaces.dtos.v1.deposit_request import DepositRequest
from lumen.interfaces.dtos.v1.open_wallet_request import OpenWalletRequest
from lumen.interfaces.dtos.v1.wallet_dto import WalletDto
from pyfly.container import rest_controller
from pyfly.cqrs import DefaultCommandBus, DefaultQueryBus
from pyfly.kernel import ResourceNotFoundException
from pyfly.security import SecurityContext, secure
from pyfly.web import Body, PathVar, Valid, get_mapping, post_mapping, request_mapping


@rest_controller
@request_mapping("/api/v1/wallets")
class WalletController:
    """Digital-wallet REST API: open, deposit, withdraw, inspect."""

    def __init__(
        self,
        commands: DefaultCommandBus,
        queries: DefaultQueryBus,
    ) -> None:
        self._commands = commands
        self._queries = queries

    # Any authenticated user (USER or ADMIN) may open a wallet.
    @secure(roles=["USER", "ADMIN"])
    @post_mapping("", status_code=201)
    async def open_wallet(
        self,
        request: Valid[Body[OpenWalletRequest]],
        security_context: SecurityContext,
    ) -> dict[str, str]:
        wallet_id = await self._commands.send(
            OpenWallet(
                owner_id=request.owner_id,
                currency=request.currency,
            )
        )
        return {"wallet_id": wallet_id}

    # Deposit: USER role plus the wallet:deposit permission.
    @secure(roles=["USER", "ADMIN"], permissions=["wallet:deposit"])
    @post_mapping("/{wallet_id}/deposit")
    async def deposit(
        self,
        wallet_id: PathVar[str],
        request: Valid[Body[DepositRequest]],
        security_context: SecurityContext,
    ) -> dict[str, int | str]:
        # DepositRequest.amount is in minor units (cents).
        balance = await self._commands.send(
            DepositFunds(wallet_id=wallet_id, amount=request.amount)
        )
        return {"wallet_id": wallet_id, "balance_minor": balance}

    # Read balance: any authenticated user.
    @secure(roles=["USER", "ADMIN"])
    @get_mapping("/{wallet_id}/balance")
    async def get_balance(
        self,
        wallet_id: PathVar[str],
        security_context: SecurityContext,
    ) -> BalanceDto:
        result = await self._queries.query(GetBalance(wallet_id=wallet_id))
        if result is None:
            raise ResourceNotFoundException(
                f"Wallet {wallet_id!r} not found",
                code="WALLET_NOT_FOUND",
                context={"wallet_id": wallet_id},
            )
        return result

    # Full wallet view: ADMIN only.
    @secure(roles=["ADMIN"])
    @get_mapping("/{wallet_id}")
    async def get_wallet(
        self,
        wallet_id: PathVar[str],
        security_context: SecurityContext,
    ) -> WalletDto:
        result = await self._queries.query(GetWallet(wallet_id=wallet_id))
        if result is None:
            raise ResourceNotFoundException(
                f"Wallet {wallet_id!r} not found",
                code="WALLET_NOT_FOUND",
                context={"wallet_id": wallet_id},
            )
        return result

:::
:::

**How it works.** `@secure` is stacked **above** `@post_mapping` /
`@get_mapping` so authorization runs before the route binding. The decorated
method must accept `security_context: SecurityContext` as a keyword argument —
the framework injects it from `request.state.security_context`, which
`SecurityMiddleware` already populated.

When multiple roles are listed, the user needs **at least one** (OR semantics).
When multiple permissions are listed the user needs **all** of them (AND
semantics). When both `roles` and `permissions` are supplied, both checks must
pass.

!!! note "Amounts in minor units"
    `DepositRequest.amount` is an `int` in **minor units** (cents). €10.50 is
    `1050`. This convention avoids floating-point rounding errors throughout
    the Money domain. `WalletDto.balance_minor` carries the same integer;
    `WalletDto.balance` is a `float` rendered for display only.

### Expression-based authorization

For policies that cannot be expressed with a flat role list, use the
`expression` parameter. PyFly evaluates the expression with safe AST parsing —
no `eval()` or `exec()` is used anywhere in the chain.

::: listing lumen/web/controllers/wallet_controller.py | Listing 14.6 — Security expressions on wallet endpoints
from pyfly.security import SecurityContext, secure


# ADMIN, or a MANAGER who also holds wallet:write.
@secure(
    expression=(
        "hasRole('ADMIN')"
        " or (hasRole('MANAGER') and hasPermission('wallet:write'))"
    )
)
async def approve_large_deposit(
    self,
    deposit_id: str,
    security_context: SecurityContext,
) -> None:
    ...


# Any authenticated, non-guest user can see the wallet dashboard.
@secure(expression="isAuthenticated and not hasRole('GUEST')")
async def dashboard(
    self,
    security_context: SecurityContext,
) -> dict:
    ...

:::
:::

Supported expression vocabulary (full set):

| Token | Description |
|---|---|
| `hasRole('X')` | User has role `X` |
| `hasAnyRole('X', 'Y')` | User has at least one of the listed roles |
| `hasAuthority('X')` | User has role **or** permission `X` |
| `hasAnyAuthority('X', 'Y')` | At least one of the listed roles/permissions |
| `hasPermission('X')` | User has permission `X` |
| `isAuthenticated` | User is authenticated |
| `isAnonymous` | User is **not** authenticated |
| `permitAll` | Always allow |
| `denyAll` | Always deny |
| `principal` / `authentication` | The current `SecurityContext` object |
| `and` / `or` / `not` | Boolean operators |
| `(...)` | Grouping |

!!! note "Expressions are safe"
    PyFly reduces each construct to `True` or `False`, then evaluates only a
    pure boolean AST. Any node that is not a constant, `BoolOp`, or
    `UnaryOp(Not)` raises `SecurityException(code="INVALID_EXPRESSION")`.
    This eliminates injection risks entirely.

### Applying @secure to CQRS handlers

`@secure` is not limited to REST controllers. You can protect CQRS command
handlers in exactly the same way — it fires before the handler body runs
because the DI container injects `security_context` from
`request.state.security_context` when it resolves the handler:

::: listing lumen/core/services/wallets/deposit_funds_handler.py | Listing 14.7 — @secure on a CQRS command handler
from pyfly.cqrs import command_handler
from pyfly.security import SecurityContext, secure

from lumen.core.services.wallets.deposit_funds_command import DepositFunds


@command_handler
class DepositFundsHandler:

    @secure(roles=["USER", "ADMIN"], permissions=["wallet:deposit"])
    async def handle(
        self,
        command: DepositFunds,
        security_context: SecurityContext,
    ) -> int:
        # command.amount is in minor units (cents)
        ...

:::
:::

The check fires before any business logic because the DI container injects
`security_context` from `request.state.security_context` when it resolves the
handler.

---

## Passwords

### Why bcrypt?

MD5 and SHA-256 are designed to be fast — ideal for data integrity, catastrophic
for passwords. An attacker who steals your user table can try billions of SHA-256
guesses per second on commodity hardware. Bcrypt is designed to be slow and
adjustably expensive: the cost factor (rounds) lets you tune the algorithm so
that an attack requires orders of magnitude more time.

### BcryptPasswordEncoder

`BcryptPasswordEncoder` implements the `PasswordEncoder` protocol (a
`runtime_checkable` Protocol with `hash` and `verify` methods):

::: listing lumen/auth/password_service.py | Listing 14.8 — Hashing and verifying passwords
from pyfly.security import BcryptPasswordEncoder

encoder = BcryptPasswordEncoder(rounds=12)

# During registration — store only the hash, never the raw password
hashed = encoder.hash("correct-horse-battery-staple")

# During login — verify without storing the plaintext
is_match = encoder.verify("correct-horse-battery-staple", hashed)
# True

is_match = encoder.verify("wrong-password", hashed)
# False

:::
:::

| Parameter | Default | Notes |
|---|---|---|
| `rounds` | `12` | Each increment doubles hashing time. 12 is the recommended production default. |

**How it works.** `hash` calls `bcrypt.gensalt(rounds=self._rounds)` to
generate a new random salt, then `bcrypt.hashpw` to produce the hash. Both the
salt and the hash are encoded in the returned string — the `$2b$12$…` prefix
encodes the algorithm version and the cost factor, so the stored hash is fully
self-describing. `verify` calls `bcrypt.checkpw`, which re-derives the hash from
the raw password and the embedded salt and compares with a timing-safe equality
check to prevent timing-oracle attacks.

### The PasswordEncoder protocol

`PasswordEncoder` is a `runtime_checkable` Protocol. Any class that implements
`hash(raw: str) -> str` and `verify(raw: str, hashed: str) -> bool` satisfies
it — including `BcryptPasswordEncoder`. This lets you swap in argon2 or scrypt
for future-proofing without touching any service code:

```python
from pyfly.security import PasswordEncoder

class Argon2PasswordEncoder:
    def hash(self, raw: str) -> str: ...
    def verify(self, raw: str, hashed: str) -> bool: ...

isinstance(Argon2PasswordEncoder(), PasswordEncoder)  # True
```

!!! tip "Auto-configuration"
    When `pyfly.security.enabled=true` and `bcrypt` is installed, PyFly
    auto-configures a `BcryptPasswordEncoder` bean with `rounds` read from
    `pyfly.security.password.bcrypt-rounds`. Declare the bean yourself in a
    `@configuration` class to override it without touching auto-configuration.

---

## Sessions

### Why server-side sessions?

JWT tokens are stateless — once issued, they cannot be revoked before they
expire. If a user logs out, their token is still valid until `exp`. For many
APIs that trade-off is acceptable. For browser-facing applications — Lumen's
admin dashboard, for example — you want the server to be the authoritative
source: log out means log out.

Server-side sessions give you that control. A `SessionStore` holds the session
data keyed by a random session id. The browser receives only the session id in
a cookie. The server can revoke a session instantly by deleting its store entry.

### HttpSession

`HttpSession` wraps a session's data dictionary with typed accessors and tracks
mutation state so the filter knows when to persist:

| Property / Method | Description |
|---|---|
| `id` | UUID hex string session identifier |
| `is_new` | `True` if created during this request |
| `created_at` | Unix timestamp (`float`) of creation |
| `last_accessed` | Unix timestamp (`float`) of most recent access |
| `modified` | `True` if any attribute was written or session is new |
| `invalidated` | `True` if `invalidate()` was called |
| `previous_id` | Former id after `rotate_id()` (used by filter to clean up old entry) |
| `get_attribute(name)` | Return attribute value or `None` |
| `set_attribute(name, value)` | Write an attribute; marks session modified |
| `remove_attribute(name)` | Remove attribute if present |
| `get_attribute_names()` | List user-set attribute names (excludes internal `_*` keys) |
| `rotate_id()` | Assign a fresh session id, preserving all data |
| `invalidate()` | Mark for deletion on next filter pass |
| `get_data()` | Return the raw session dict (includes internal metadata) |

::: listing lumen/core/services/auth/session_handler.py | Listing 14.9 — Using the session after login
from pyfly.session import HttpSession


async def post_login(
    username: str,
    password: str,
    session: HttpSession,
    auth_service,
) -> dict:
    user = await auth_service.authenticate(username, password)

    # Rotate the id before writing auth state (session-fixation prevention)
    session.rotate_id()
    session.set_attribute("user_id", str(user.id))
    session.set_attribute("role", user.role)

    return {"message": "Logged in"}


async def logout(session: HttpSession) -> dict:
    session.invalidate()
    return {"message": "Logged out"}

:::
:::

**How it works.** `rotate_id()` generates a new UUID session id and stores the
old id in `session.previous_id`. When `SessionFilter` persists the session at
the end of the request it deletes the old store entry (the previous id can no
longer resolve to this session) and saves the new one. An attacker who obtained
the pre-auth session id cannot ride it into the authenticated session — the
classic session-fixation mitigation.

### The SessionStore protocol

All backends implement the `SessionStore` protocol:

```python
class SessionStore(Protocol):
    async def get(
        self, session_id: str
    ) -> dict[str, Any] | None: ...

    async def save(
        self, session_id: str,
        data: dict[str, Any],
        ttl: int,
    ) -> None: ...

    async def delete(self, session_id: str) -> None: ...

    async def exists(self, session_id: str) -> bool: ...
```

Two adapters ship out of the box:

| Adapter | Module | Notes |
|---|---|---|
| `InMemorySessionStore` | `pyfly.session.adapters.memory` | `asyncio.Lock`-guarded; single-process only; data lost on restart |
| `RedisSessionStore` | `pyfly.session.adapters.redis` | JSON-serialized; keys prefixed `pyfly:session:`; TTL managed by Redis |

### SessionFilter

`SessionFilter` is an `OncePerRequestFilter` ordered at
`HIGHEST_PRECEDENCE + 150`. It runs before every authentication filter and after
every response:

1. Reads the session cookie (`PYFLY_SESSION` by default).
2. Loads the session from the store, or creates a new one.
3. Attaches it to `request.state.session`.
4. Calls `call_next(request)` — the rest of the filter chain and the handler run.
5. On response, persists a modified or new session, deletes an invalidated one,
   and re-issues the cookie with a rolling `max_age` (the TTL slides forward on
   every request).

Cookie attributes set by `SessionFilter`:

| Attribute | Value | Reason |
|---|---|---|
| `httponly` | `True` | Prevents JavaScript access; XSS mitigation |
| `samesite` | `lax` | Blocks most cross-site request forgery flows |
| `secure` | configurable | Set `True` in production (HTTPS only) |
| `max_age` | `ttl` | Rolling expiry |

### Redis sessions in production

::: listing lumen/config/session_config.py | Listing 14.10 — Redis session store
import redis.asyncio as aioredis

from pyfly.container import bean, configuration
from pyfly.session import SessionFilter
from pyfly.session.adapters.redis import RedisSessionStore


@configuration
class SessionConfig:

    @bean
    def session_store(self) -> RedisSessionStore:
        client = aioredis.from_url(
            "redis://localhost:6379/0"
        )
        return RedisSessionStore(client=client)

    @bean
    def session_filter(
        self,
        store: RedisSessionStore,
    ) -> SessionFilter:
        return SessionFilter(
            store=store,
            cookie_name="LUMEN_SESSION",
            ttl=1800,
            secure=True,
        )

:::
:::

**How it works.** `RedisSessionStore.save` JSON-serializes the session
dictionary (including any dataclass attributes such as `SecurityContext`, which
are round-tripped via an allowlisted type-tag mechanism) and calls
`client.set(key, raw, ex=ttl)` — the TTL is managed entirely by Redis, so
expired sessions disappear server-side with zero cleanup overhead. Keys use the
prefix `pyfly:session:` for namespace isolation. Reading back deserializes only
types on the allowlist, eliminating arbitrary-object instantiation risks.

!!! tip "Auto-configuration"
    Add `pyfly.session.enabled: true` and `pyfly.session.store: redis` to
    `pyfly.yaml`. PyFly auto-configures `RedisSessionStore` (when
    `redis.asyncio` is installed) and `SessionFilter` for you. The
    `redis.url` defaults to `redis://localhost:6379/0`; override with
    `pyfly.session.redis.url`.

!!! spring "Spring parity"
    `SessionFilter` mirrors Spring Session's `SessionRepositoryFilter`.
    `HttpSession` mirrors `javax.servlet.http.HttpSession` (or
    `jakarta.servlet.http.HttpSession` in Boot 3). `InMemorySessionStore`
    is equivalent to Spring Session's `MapSessionRepository`;
    `RedisSessionStore` is equivalent to `RedisSessionRepository`.
    The cookie attributes (`HttpOnly`, `SameSite=Lax`, rolling `Max-Age`)
    match Spring Session's defaults exactly.

---

## External identity (IDP)

### The problem with managing identity in-house

Lumen currently stores credentials in its own database. That means Lumen must
implement password resets, MFA, email verification, account lockout, GDPR
deletion, social login, and SSO — all undifferentiated work that exists in every
service. The industry answer is to delegate identity to a dedicated provider:
Keycloak for on-premises, AWS Cognito for AWS-native, Azure AD for Microsoft
environments.

PyFly's `IdpAdapter` port makes that delegation pluggable behind a single
interface. Swap the adapter and the business layer never knows.

### IdpAdapter — the port

Every adapter must satisfy the `IdpAdapter` protocol:

```python
class IdpAdapter(Protocol):
    name: str

    # User management
    async def create_user(
        self, user: IdpUser, password: str
    ) -> IdpUser: ...
    async def get_user(self, user_id: str) -> IdpUser | None: ...
    async def find_by_username(
        self, username: str
    ) -> IdpUser | None: ...
    async def update_user(self, user: IdpUser) -> IdpUser: ...
    async def delete_user(self, user_id: str) -> bool: ...
    async def list_users(self, *, limit: int = 100) -> list[IdpUser]: ...

    # Authentication
    async def login(
        self, request: LoginRequest
    ) -> AuthResult: ...
    async def logout(self, access_token: str) -> bool: ...
    async def refresh(
        self, refresh_token: str
    ) -> AuthResult: ...
    async def introspect(
        self, access_token: str
    ) -> SessionIntrospection: ...

    # Password / MFA
    async def change_password(
        self, request: PasswordChangeRequest
    ) -> bool: ...
    async def reset_password(self, user_id: str) -> str: ...

    # Roles
    async def assign_role(
        self, user_id: str, role: str
    ) -> bool: ...
    async def revoke_role(
        self, user_id: str, role: str
    ) -> bool: ...
    async def list_roles(self) -> list[IdpRole]: ...
```

Key DTOs:

| Class | Purpose |
|---|---|
| `IdpUser` | User record: `id`, `username`, `email`, `roles`, `attributes`, … |
| `LoginRequest` | `username`, `password`, `mfa_code` (optional) |
| `AuthResult` | `user`, `access_token`, `refresh_token`, `expires_in`, `token_type` |
| `SessionIntrospection` | `active`, `user_id`, `username`, `scopes`, `expires_at` |
| `PasswordChangeRequest` | `user_id`, `old_password`, `new_password` |
| `IdpRole` | `name`, `description`, `scopes` |

### Keycloak adapter

::: listing lumen/config/idp_config.py | Listing 14.11 — Wiring the Keycloak adapter
from pyfly.container import bean, configuration
from pyfly.idp import IdpAdapter, KeycloakIdpAdapter


@configuration
class IdpConfig:

    @bean
    def idp_adapter(self) -> IdpAdapter:
        return KeycloakIdpAdapter(
            base_url="https://keycloak.example.com",
            realm="lumen",
            client_id="lumen-backend",
            client_secret="${KEYCLOAK_SECRET}",
            verify_ssl=True,
        )

:::
:::

**How it works.** `KeycloakIdpAdapter` talks to Keycloak's Admin REST API
(`/admin/realms/{realm}/users`) and token endpoint
(`/realms/{realm}/protocol/openid-connect/token`) via `httpx`. It caches a
`client_credentials` admin token internally, re-fetching it within a ten-second
safety margin of expiry — Keycloak's default client-credentials TTL is 60 s,
so without this cache every admin call would make two network round trips.

### Using the IDP in a service

::: listing lumen/core/services/auth/idp_auth_service.py | Listing 14.12 — Using IdpAdapter in the auth service
from pyfly.container import service
from pyfly.idp import IdpAdapter, IdpUser, LoginRequest
from pyfly.kernel.exceptions import UnauthorizedException


@service
class IdpAuthService:

    def __init__(self, idp: IdpAdapter) -> None:
        self._idp = idp

    async def register(
        self,
        username: str,
        email: str,
        password: str,
        role: str = "USER",
    ) -> str:
        user = IdpUser(
            username=username,
            email=email,
            roles=[role],
        )
        created = await self._idp.create_user(user, password)
        result = await self._idp.login(
            LoginRequest(
                username=username, password=password
            )
        )
        return result.access_token

    async def login(
        self, username: str, password: str
    ) -> str:
        try:
            result = await self._idp.login(
                LoginRequest(
                    username=username, password=password
                )
            )
        except PermissionError as exc:
            raise UnauthorizedException(
                "Invalid credentials",
                code="INVALID_CREDENTIALS",
            ) from exc
        return result.access_token

    async def introspect(self, token: str) -> dict:
        info = await self._idp.introspect(token)
        return {
            "active": info.active,
            "user_id": info.user_id,
            "username": info.username,
            "scopes": info.scopes,
        }

:::
:::

**How it works.** `IdpAuthService` depends only on `IdpAdapter` — the DI
container resolves the concrete `KeycloakIdpAdapter` at startup. The service
layer never imports Keycloak, Cognito, or Azure-specific code. Switch provider
by changing one line in `IdpConfig`; the service stays identical.

### Auto-configuration and the built-in HTTP routes

Enable the IDP subsystem in `pyfly.yaml` and PyFly wires the adapter and a
REST controller automatically:

::: listing lumen/resources/pyfly.yaml | Listing 14.13 — IDP auto-configuration
pyfly:
  idp:
    enabled: true
    provider: keycloak
    keycloak:
      base-url: https://keycloak.example.com
      realm: lumen
      client-id: lumen-backend
      client-secret: "${KEYCLOAK_SECRET}"

:::
:::

| `provider` value | Adapter |
|---|---|
| `internal-db` | `InternalDbIdpAdapter` (bcrypt in-memory store) |
| `keycloak` | `KeycloakIdpAdapter` |
| `cognito` / `aws-cognito` | `AwsCognitoIdpAdapter` |
| `azure-ad` / `azuread` / `entra` | `AzureAdIdpAdapter` |

When Starlette is present, `IdpAutoConfiguration` also registers an
`IdpController` bean that exposes the full IDP over HTTP under `/idp`:

| Route | Method | Description |
|---|---|---|
| `/idp/login` | POST | Authenticate (username + password + optional MFA) |
| `/idp/refresh` | POST | Refresh an access token |
| `/idp/logout` | POST | Revoke a token |
| `/idp/introspect` | POST | Inspect an active session |
| `/idp/admin/users` | POST | Create a user |
| `/idp/admin/users` | GET | List users |
| `/idp/admin/users/{user_id}` | GET / DELETE | Get or delete a user |
| `/idp/admin/users/{user_id}/roles/{role}` | POST / DELETE | Assign or revoke a role |
| `/idp/admin/roles` | GET | List all roles |

!!! tip "Custom adapters"
    Any class that satisfies the `IdpAdapter` Protocol can be wired in as the
    `IdpAdapter` bean. Register it in a `@configuration` class and PyFly's
    `@conditional_on_missing_bean` skips auto-configuration entirely. This
    is the standard extension point for on-premises LDAP, in-house SSO, or
    test-double adapters.

!!! spring "Spring parity"
    `IdpAdapter` is PyFly's equivalent of Spring Security's
    `UserDetailsService` + `AuthenticationProvider` combination. `IdpUser`
    maps to `UserDetails`; `AuthResult` maps to the `Authentication` object
    returned by `AuthenticationManager.authenticate()`. `KeycloakIdpAdapter`
    plays the role of the Keycloak Spring Security adapter.

---

## Putting it together — Lumen's auth layer

Here is the complete wiring for Lumen using the IDP adapter, the JWT filter,
URL-level rules, and a Redis session store for the admin dashboard:

::: listing lumen/config/security_full.py | Listing 14.14 — Full security configuration
from pyfly.container import bean, configuration
from pyfly.idp import IdpAdapter, KeycloakIdpAdapter
from pyfly.security.http_security import HttpSecurity


@configuration
class LumenSecurityConfig:

    @bean
    def idp_adapter(self) -> IdpAdapter:
        return KeycloakIdpAdapter(
            base_url="https://keycloak.example.com",
            realm="lumen",
            client_id="lumen-backend",
            client_secret="${KEYCLOAK_SECRET}",
        )

    @bean
    def http_security_filter(self):
        hs = HttpSecurity()
        hs.authorize_requests() \
            .request_matchers(
                "/idp/login", "/idp/refresh",
                "/docs", "/openapi.json",
            ).permit_all() \
            .request_matchers(
                "/idp/admin/**"
            ).has_role("ADMIN") \
            .request_matchers(
                "/api/v1/wallets/**"
            ).authenticated() \
            .any_request().permit_all()
        return hs.build()

:::
:::

With `pyfly.security.enabled=true`, `pyfly.session.enabled=true` and
`pyfly.session.store=redis` in `pyfly.yaml`, auto-configuration handles
`JWTService`, `BcryptPasswordEncoder`, `SessionFilter`, and `RedisSessionStore`.
The `@configuration` class above only supplies what auto-configuration cannot
know: the Keycloak coordinates and the URL policy.

---

## What you built {.recap}

This chapter opened Part V by closing Lumen's open front door. You:

- Used **`JWTService`** to issue signed tokens at login and to decode them back
  into a `SecurityContext` on every subsequent request. The `exp` claim is
  mandatory — `encode()` auto-adds it using a Unix timestamp so you never
  need to import `datetime`; tokens without `exp` are rejected at the boundary.
- Added **`SecurityMiddleware`** to the Starlette application so every request
  carries a populated `SecurityContext` by the time it reaches a handler.
- Declared URL-level policy with the **`HttpSecurity`** builder — a fluent DSL
  that produces an `HttpSecurityFilter` evaluated before the route dispatcher,
  covering Lumen's real `/api/v1/wallets/**` tree and the IDP admin routes.
- Protected Lumen's real wallet endpoints in `WalletController` with
  **`@secure`**, specifying roles, permissions, or full security expressions.
  Authorization failures raise `SecurityException` (401, code `AUTH_REQUIRED`)
  for unauthenticated callers and `ForbiddenException` (403, code `FORBIDDEN`)
  for authenticated callers who lack the required role or permission.
- Hashed passwords with **`BcryptPasswordEncoder`**, the default adapter for the
  `PasswordEncoder` protocol. The cost factor is tunable; the stored hash is
  self-describing; verification is timing-safe.
- Managed server-side sessions with **`HttpSession`** and the pluggable
  **`SessionStore`** protocol. In development, `InMemorySessionStore` requires
  no dependencies; in production, `RedisSessionStore` serialises to JSON and
  lets Redis manage TTL. The `SessionFilter` rolls the cookie TTL on every
  request and deletes it on invalidation.
- Delegated identity to an external provider via the **`IdpAdapter`** port and
  the **`KeycloakIdpAdapter`** implementation. The auto-configured
  `IdpController` exposes login, refresh, logout, introspect, and admin user
  management under `/idp` with no extra code.

---

## Try it yourself {.exercises}

**Exercise 1 — Role hierarchy.** Lumen currently treats `ADMIN` and `USER` as
independent roles. Add a "super-user" role `SUPER` that implicitly holds every
`ADMIN` privilege. Implement a `RoleHierarchy` wrapper that pre-expands roles
before storing them in `SecurityContext`, and update `_permissions_for` in
Listing 14.1 so that a `SUPER`-role token passes every `@secure(roles=["ADMIN"])`
check without explicitly carrying the `ADMIN` role. Write a unit test that
creates a `SecurityContext(roles=["SUPER"])` after expansion and asserts
`has_any_role(["ADMIN"])` returns `True`.

**Exercise 2 — Session concurrency control.** Lumen's admin users must not be
logged in from more than two devices at the same time (a common financial
compliance requirement). Enable `pyfly.session.concurrency.enabled: true` with
`max-sessions: 2` and `strategy: evict-oldest` in `pyfly.yaml`. Write an
integration test using `InMemorySessionStore` that creates three sessions for
the same `user_id`, calls the `SessionConcurrencyController`, and asserts that
the oldest session has been evicted while the two newest remain valid.

**Exercise 3 — Custom IDP adapter.** Lumen's staging environment uses a
homegrown OAuth2 server. Implement a `StagingIdpAdapter` that satisfies
`IdpAdapter`, backed by an in-memory user dictionary. The `login` method should
issue a signed JWT using a `JWTService` injected through the constructor. Wire
it as the `IdpAdapter` bean in a `@configuration` class tagged
`@conditional_on_property("lumen.env", having_value="staging")` and confirm
that the production `KeycloakIdpAdapter` bean is not created when that property
is active.
