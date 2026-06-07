# Session Management

The `pyfly.session` module provides server-side HTTP session management with a
pluggable store backend. It mirrors the Spring Session model: a `SessionFilter`
reads a session cookie on every request, loads (or creates) an `HttpSession`
from a `SessionStore`, attaches it to `request.state.session`, and persists
changes after the response. Two stores ship out of the box — in-memory for
development and Redis for production.

---

## Quick Example

```python
from pyfly.session import HttpSession, SessionFilter, SessionStore
from pyfly.session.adapters.memory import InMemorySessionStore

store = InMemorySessionStore()
filter_ = SessionFilter(store=store, cookie_name="PYFLY_SESSION", ttl=1800)

# Inside a request handler, once the filter has run:
# session = request.state.session  # HttpSession instance
# session.set_attribute("user_id", "alice")
# session.get_attribute("user_id")   # "alice"
# session.invalidate()               # marks for deletion
```

---

## Configuration

Enable sessions in `pyfly.yaml`. Auto-configuration wires the store and filter
automatically:

```yaml
pyfly:
  session:
    enabled: true
    store: memory         # memory (default) | redis
    cookie-name: PYFLY_SESSION   # default
    ttl: 1800             # seconds (default: 30 minutes)
    cookie:
      secure: false       # set true in production (HTTPS only)
    redis:
      url: redis://localhost:6379/0
```

| Key | Default | Description |
|-----|---------|-------------|
| `pyfly.session.enabled` | — | Must be `true` to activate session support |
| `pyfly.session.store` | `memory` | Store backend: `memory` or `redis` |
| `pyfly.session.cookie-name` | `PYFLY_SESSION` | Name of the session cookie |
| `pyfly.session.ttl` | `1800` | Session lifetime in seconds |
| `pyfly.session.cookie.secure` | `false` | Set `true` to mark the cookie `Secure` (HTTPS only) |
| `pyfly.session.redis.url` | `redis://localhost:6379/0` | Redis connection URL (used when `store=redis`) |

The `redis` store requires `redis.asyncio` to be installed
(`pip install redis`). If it is not available, the auto-configuration falls
back silently to the in-memory store.

---

## Key APIs

### `HttpSession`

`HttpSession` wraps the session data dictionary with typed accessors and tracks
mutation state so the filter knows when to persist.

```python
from pyfly.session import HttpSession
```

| Property / Method | Description |
|---|---|
| `id` | Unique session identifier (UUID hex string) |
| `is_new` | `True` if the session was created during this request |
| `created_at` | Unix timestamp of session creation (`float`) |
| `last_accessed` | Unix timestamp of the most recent access (`float`) |
| `invalidated` | `True` if `invalidate()` has been called |
| `modified` | `True` if any attribute was set or removed (or session is new) |
| `get_attribute(name)` | Return attribute value or `None` |
| `set_attribute(name, value)` | Set an attribute; marks session as modified |
| `remove_attribute(name)` | Remove an attribute if present |
| `get_attribute_names()` | List of all user-set attribute names (excludes internal `_*` keys) |
| `invalidate()` | Mark the session for deletion; filter will delete cookie and store entry |
| `get_data()` | Raw session dict (includes internal metadata) |

### `SessionStore` protocol

```python
from pyfly.session import SessionStore
```

All session backends implement this `runtime_checkable` Protocol:

```python
class SessionStore(Protocol):
    async def get(self, session_id: str) -> dict[str, Any] | None: ...
    async def save(self, session_id: str, data: dict[str, Any], ttl: int) -> None: ...
    async def delete(self, session_id: str) -> None: ...
    async def exists(self, session_id: str) -> bool: ...
```

### `InMemorySessionStore`

```python
from pyfly.session.adapters.memory import InMemorySessionStore
```

Thread-safe in-memory store with TTL-based expiry. Uses `asyncio.Lock`.
Suitable for development, testing, and single-process deployments. Data is
lost on restart.

### `RedisSessionStore`

```python
from pyfly.session.adapters.redis import RedisSessionStore
```

Redis-backed store. Values are JSON-serialized; dataclass attributes (such as
`SecurityContext`) are round-tripped via a type-tag mechanism so OAuth2 session
login persists correctly. Keys are prefixed with `pyfly:session:`.

```python
import redis.asyncio as aioredis
from pyfly.session.adapters.redis import RedisSessionStore

client = aioredis.from_url("redis://localhost:6379/0")
store = RedisSessionStore(client=client)
```

### `SessionFilter`

```python
from pyfly.session import SessionFilter
```

An `OncePerRequestFilter` ordered at `HIGHEST_PRECEDENCE + 150`. It runs
**before** authentication filters so the session is available when
`OAuth2SessionSecurityFilter` (HP+225) reads `request.state.session`.

| Constructor parameter | Default | Description |
|---|---|---|
| `store` | required | `SessionStore` instance |
| `cookie_name` | `PYFLY_SESSION` | Session cookie name |
| `ttl` | `1800` | Session TTL in seconds |
| `secure` | `False` | Whether to set the `Secure` cookie flag |

Cookie properties set by the filter:

| Property | Value | Reason |
|---|---|---|
| `httponly` | `True` | Prevents JavaScript access (XSS mitigation) |
| `samesite` | `lax` | Blocks cross-site request forgery for most flows |
| `secure` | configurable | Should be `True` in production |
| `max_age` | `ttl` | Slides forward on every request (rolling TTL) |

On invalidation, the filter deletes the cookie and removes the store entry.

---

## Auto-Configuration

Two auto-configuration classes activate when `pyfly.session.enabled=true`:

| Class | Bean | Condition |
|---|---|---|
| `SessionStoreAutoConfiguration` | `session_store` | `SessionStore` bean not already present |
| `SessionFilterAutoConfiguration` | `session_filter` | always (when enabled) |

A third class, `SessionConcurrencyAutoConfiguration`, activates independently
when `pyfly.session.concurrency.enabled=true` — see
[Concurrency Control](#concurrency-control).

`SessionStoreAutoConfiguration` checks `pyfly.session.store`:

- `redis` → `RedisSessionStore` (requires `redis.asyncio`; falls back to memory if unavailable)
- any other value → `InMemorySessionStore`

Provide your own `SessionStore` bean to bypass auto-configuration entirely.

---

## Integration with OAuth2 Login

`OAuth2LoginHandler` writes the authenticated `SecurityContext` into the
session under the key `SECURITY_CONTEXT`. On subsequent requests,
`OAuth2SessionSecurityFilter` (ordered at HP+225, after SessionFilter at
HP+150) reads this attribute and restores the `SecurityContext` onto
`request.state.security_context`.

This means browser-based OAuth2 login works without any extra wiring: enable
sessions, enable OAuth2 login, and the two filters cooperate automatically.

---

## Concurrency Control

Mirroring Spring Security's `maximumSessions`, PyFly can cap the number of
concurrent sessions per authenticated principal. The cap is enforced at the
single point where a principal becomes bound to a session — OAuth2 login —
after the session id has been rotated. With no cap configured, the registry is
unused and behavior is unchanged.

### Configuration

```yaml
pyfly:
  session:
    concurrency:
      enabled: true
      max-sessions: 1            # -1 = unlimited (default)
      strategy: evict-oldest     # evict-oldest (default) | reject-new
```

| Key | Default | Description |
|-----|---------|-------------|
| `pyfly.session.concurrency.enabled` | — | Must be `true` to activate concurrency control |
| `pyfly.session.concurrency.max-sessions` | `-1` | Maximum live sessions per principal; `-1` means unlimited |
| `pyfly.session.concurrency.strategy` | `evict-oldest` | What to do when the cap is exceeded: `evict-oldest` or `reject-new` |

**Strategies**

- `evict-oldest` — the new login succeeds; the oldest session(s) for that
  principal are removed from the registry and deleted from the session store.
- `reject-new` — the new login is refused. The handler invalidates the
  pending session and responds with HTTP `401` and body
  `{"error": "max_sessions", ...}`.

### Auto-Configuration

When `pyfly.session.concurrency.enabled=true`,
`SessionConcurrencyAutoConfiguration` registers a
`SessionConcurrencyController` bean backed by an `InMemorySessionRegistry`.
The OAuth2 login auto-configuration resolves this bean (if present) and passes
it to `OAuth2LoginHandler`, so no manual wiring is required:

| Class | Bean | Condition |
|---|---|---|
| `SessionConcurrencyAutoConfiguration` | `session_concurrency_controller` | `pyfly.session.concurrency.enabled=true` |

The controller's `session_deleter` is wired to `SessionStore.delete`, so an
evicted session is purged from whichever store backend is active (memory or
Redis).

### Key APIs

```python
from pyfly.session import (
    ConcurrencyControlPolicy,
    InMemorySessionRegistry,
    SessionConcurrencyController,
    SessionRegistry,
)
```

`ConcurrencyControlPolicy` is a frozen dataclass holding the cap configuration:

```python
policy = ConcurrencyControlPolicy(max_sessions=1, strategy="reject-new")
```

| Field | Default | Description |
|---|---|---|
| `max_sessions` | `-1` | Cap per principal; `-1` (negative) means unlimited |
| `strategy` | `"evict-oldest"` | `"evict-oldest"` or `"reject-new"` |

`SessionRegistry` is a `runtime_checkable` Protocol — a per-principal index of
live session ids, kept separate from the `SessionStore`:

```python
class SessionRegistry(Protocol):
    async def register(self, principal: str, session_id: str, created_at: float) -> None: ...
    async def deregister(self, principal: str, session_id: str) -> None: ...
    async def list_sessions(self, principal: str) -> list[tuple[str, float]]: ...  # oldest first
    async def count(self, principal: str) -> int: ...
```

`InMemorySessionRegistry` is the in-process implementation (guarded by an
`asyncio.Lock`), the default used by auto-configuration. Provide your own
`SessionRegistry` implementation for a distributed registry.

`SessionConcurrencyController` enforces the policy:

| Method | Description |
|---|---|
| `__init__(registry, policy, *, session_deleter=None)` | `session_deleter` is an `async (session_id) -> None` callable used to evict store entries |
| `on_login(principal, session_id, created_at)` | Registers the session, enforcing the cap. Returns `False` if rejected (`reject-new`), `True` otherwise |
| `on_logout(principal, session_id)` | Deregisters the session |

Constructing a controller manually:

```python
from pyfly.session import (
    ConcurrencyControlPolicy,
    InMemorySessionRegistry,
    SessionConcurrencyController,
)
from pyfly.session.adapters.memory import InMemorySessionStore

store = InMemorySessionStore()
controller = SessionConcurrencyController(
    InMemorySessionRegistry(),
    ConcurrencyControlPolicy(max_sessions=1, strategy="reject-new"),
    session_deleter=store.delete,
)

# allowed is False once the cap is exceeded under "reject-new"
allowed = await controller.on_login("alice", session_id="abc123", created_at=1717000000.0)
```

---

## See Also

- [Security](security.md) — JWT authentication, `@secure` decorator, `OAuth2SessionSecurityFilter`
- [Web Filters](web-filters.md) — `OncePerRequestFilter`, filter ordering, `WebFilterChainMiddleware`
