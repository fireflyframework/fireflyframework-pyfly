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

### Registry Backends

The cap is enforced against a `SessionRegistry` — a per-principal index of live
session ids, kept separate from the `SessionStore`. Three backends ship out of
the box, selected by `pyfly.session.concurrency.registry`:

| `registry` | Implementation | Scope | Requirements |
|---|---|---|---|
| `memory` (default) | `InMemorySessionRegistry` | Single process only | none |
| `redis` | `RedisSessionRegistry` | Cross-process / multi-instance | `redis.asyncio` installed |
| `postgres` | `PostgresSessionRegistry` | Cross-process / durable | SQLAlchemy `AsyncEngine` bean |

- **`memory`** — in-process index guarded by an `asyncio.Lock` (mirrors
  `InMemorySessionStore`). Each app instance counts only its own sessions, so
  the cap is **not** enforced across multiple processes. Suitable for
  single-node deployments, development, and testing. State is lost on restart.
- **`redis`** — a cross-process index shared by all app instances. Each
  principal's live sessions are stored in a Redis sorted set (score =
  `created_at`, member = `session_id`), so `list_sessions` is naturally
  oldest-first. Requires `redis.asyncio`; if it is unavailable the
  auto-configuration falls back to the in-memory registry. The connection URL
  comes from `pyfly.session.concurrency.redis.url`, falling back to
  `pyfly.session.redis.url`, then `redis://localhost:6379/0`.
- **`postgres`** — a durable, queryable, cross-process index for
  relational-only deployments (no Redis required). Session ids are stored in a
  Postgres table (`session_id` PK, `principal`, `created_at`), created lazily
  and idempotently on first use. Resolves a SQLAlchemy `AsyncEngine` bean from
  the container; this requires the data module / an `AsyncEngine` to be
  configured.

### Configuration (Registry Backend)

```yaml
pyfly:
  session:
    concurrency:
      enabled: true
      max-sessions: 1
      strategy: evict-oldest
      registry: redis                          # memory (default) | redis | postgres
      redis:
        url: redis://localhost:6379/0          # optional; falls back to pyfly.session.redis.url
```

| Key | Default | Description |
|-----|---------|-------------|
| `pyfly.session.concurrency.registry` | `memory` | Registry backend: `memory`, `redis`, or `postgres` (case-insensitive) |
| `pyfly.session.concurrency.redis.url` | falls back to `pyfly.session.redis.url`, then `redis://localhost:6379/0` | Redis connection URL (used when `registry=redis`) |

### Auto-Configuration

When `pyfly.session.concurrency.enabled=true`,
`SessionConcurrencyAutoConfiguration` registers a
`SessionConcurrencyController` bean backed by the registry selected via
`pyfly.session.concurrency.registry` (`InMemorySessionRegistry` by default).
The OAuth2 login auto-configuration resolves this bean (if present) and passes
it to `OAuth2LoginHandler`, so no manual wiring is required:

| Class | Bean | Condition |
|---|---|---|
| `SessionConcurrencyAutoConfiguration` | `session_concurrency_controller` | `pyfly.session.concurrency.enabled=true` |

The Redis client and SQLAlchemy `AsyncEngine` are obtained in the
auto-configuration (the composition root) and injected into the adapters — the
adapters never import their driver at module scope (hexagonal wiring).

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
live session ids, kept separate from the `SessionStore`. It is also exported
from `pyfly.session.ports`:

```python
from pyfly.session.ports import SessionRegistry

class SessionRegistry(Protocol):
    async def register(self, principal: str, session_id: str, created_at: float) -> None: ...
    async def deregister(self, principal: str, session_id: str) -> None: ...
    async def list_sessions(self, principal: str) -> list[tuple[str, float]]: ...  # oldest first
    async def count(self, principal: str) -> int: ...
```

`InMemorySessionRegistry` is the in-process implementation (guarded by an
`asyncio.Lock`), the default used by auto-configuration when
`registry=memory`. Two cross-process implementations ship as adapters; both
have their driver/engine injected by the composition root:

```python
from pyfly.session.adapters.redis_registry import RedisSessionRegistry
from pyfly.session.adapters.postgres_registry import PostgresSessionRegistry
```

`RedisSessionRegistry(client, *, key_prefix="pyfly:session:user:", ttl=86400)`
stores each principal's sessions in a Redis sorted set (oldest-first by
`created_at`). The `ttl` (seconds) bounds orphan growth and slides forward on
each `register`. Used when `registry=redis`.

`PostgresSessionRegistry(engine_factory, *, table="pyfly_session_registry")`
stores sessions in a Postgres table. `engine_factory` is a zero-arg callable
returning a SQLAlchemy `AsyncEngine` (resolved lazily on first use); the table
name is validated as a SQL identifier. Used when `registry=postgres`.

You may still provide your own `SessionRegistry` bean to override the
auto-configured one entirely.

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
