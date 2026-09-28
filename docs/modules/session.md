# Session Management

The `pyfly.session` module provides server-side HTTP session management with a
pluggable store backend. It mirrors the Spring Session model: a `SessionFilter`
reads a session cookie on every request, loads (or creates) an `HttpSession`
from a `SessionStore`, attaches it to `request.state.session`, and persists
changes after the response. Three stores ship out of the box — in-memory for
development, and Redis or a SQL table (any relational backend) for production.

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
    store: memory         # memory (default) | redis | postgres
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
| `pyfly.session.store` | `memory` | Store backend: `memory`, `redis` or `postgres` (case-insensitive); any other value raises `ValueError` at startup |
| `pyfly.session.cookie-name` | `PYFLY_SESSION` | Name of the session cookie |
| `pyfly.session.ttl` | `1800` | Session lifetime in seconds |
| `pyfly.session.cookie.secure` | `false` | Set `true` to mark the cookie `Secure` (HTTPS only) |
| `pyfly.session.redis.url` | `redis://localhost:6379/0` | Redis connection URL (used when `store=redis`) |
| `pyfly.session.postgres.datasource` | primary | The datasource of the SQL store (`store=postgres`) |
| `pyfly.session.postgres.url` | — | Alias resolved through the `DataSourceRegistry`: the registered datasource with that URL, or a new `session` datasource |

The `redis` store requires `redis.asyncio` to be installed
(`pip install redis`). If it is not available, the auto-configuration falls
back to the in-memory store and logs a `session_store_fallback` WARNING.

> **Before 26.09.08** any `store` value other than `redis` (`jdbc`, `postgres`,
> a typo) silently became the in-memory store.

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
| `stored_id` | The id the store is known to hold the session under: the id it was loaded with, then the id of its last save (`None` for a new session not saved yet) |
| `rotate_id(*, on_login=False)` | Assign a fresh id (session-fixation defense). `on_login=True` for a login: the new id is stored even if the old entry is gone meanwhile. Otherwise (a privilege elevation) the filter moves the stored session to the new id only while the store still holds it, so a session logged out or evicted meanwhile is not brought back |
| `previous_id` | The id before the last `rotate_id()` |
| `rotated_on_login` | `True` when `rotate_id(on_login=True)` was called since the session was last persisted |
| `created_at` | Unix timestamp of session creation (`float`) |
| `last_accessed` | Unix timestamp of the most recent access (`float`) |
| `invalidated` | `True` if `invalidate()` has been called |
| `modified` | `True` if any attribute was set or removed (or session is new) |
| `get_attribute(name)` | Return attribute value or `None` |
| `set_attribute(name, value)` | Set an attribute; marks session as modified |
| `remove_attribute(name)` | Remove an attribute if present |
| `get_attribute_names()` | List of all user-set attribute names (excludes internal `_*` keys) |
| `invalidate()` | Mark the session for deletion; filter will delete cookie and store entry |
| `mark_persisted()` | Record that the store holds the session as it is now, under its current id: `modified` is `False` until the next change, `stored_id` is the current id and `rotated_on_login` is `False` (the filter calls it after each save; `previous_id` is kept) |
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

`save` inserts or replaces. A store that can also write over a session, or move it to a new id, only while it
holds it is a `ConditionalSessionStore` (`from pyfly.session import ConditionalSessionStore`), with two more
methods:

```python
class ConditionalSessionStore(SessionStore, Protocol):
    async def replace(self, session_id: str, data: dict[str, Any], ttl: int) -> bool: ...
    async def rename(self, old_id: str, new_id: str, data: dict[str, Any], ttl: int) -> bool: ...
```

`replace` writes the data and moves the expiry *ttl* seconds on, and `rename` moves the session to a new id with
the data and the new expiry, only if the store holds the session and it has not expired, in one atomic step;
each returns `False` (writing nothing) otherwise. The three shipped stores implement them: the in-memory store
under its lock, Redis with `SET ... XX` and a Lua script (both keys of a rename must be on one node: in a Redis
Cluster, in one hash slot), the SQL store with one conditional `UPDATE` each (a rename updates the key in place,
as Spring Session JDBC does). The `SessionFilter` uses it so that a request never brings back a session that was logged out,
evicted or expired while the request ran (see [`SessionFilter`](#sessionfilter)). A custom store without
`replace` keeps working: every change goes through `save`, and such a store cannot tell a revoked session from a
live one: the `SessionFilter` logs `session_store_without_replace` (a WARNING) when it is built on such a store.
A subclass of a shipped store that overrides `save` (to encrypt the data, say) must override `replace` and
`rename` the same way: the filter writes every change of a session the store already holds through them,
bypassing `save`.

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

### `SqlSessionStore`

```python
from pyfly.session.adapters.sql_session_store import SqlSessionStore
```

A session store in the application's relational database (`store=postgres`), Spring Session JDBC's
place: every instance shares the sessions with no Redis. It runs on every backend SQLAlchemy supports
(PostgreSQL, MySQL, MariaDB, SQLite) on the framework table `pyfly_sessions` (`session_id`, the attributes
as JSON with the Redis store's type tags, `expires_at` as a UTC instant, indexed).
`SqlSessionStore(engine_factory, *, table="pyfly_sessions", create_table=True, purge_interval=timedelta(seconds=60), clock=None)`:
`engine_factory` is the datasource (an `AsyncEngine`, a registry `DataSource`, a datasource name, or a
callable returning one). `start()` creates the table when the schema strategy allows it
(`pyfly.data.relational.ddl-auto`), otherwise checks it and fails fast. A session is read only before it
expires; a write purges a batch of expired sessions at most once per `purge_interval`, after its commit,
and `purge_expired()` deletes them all. Each operation joins the unit of work bound for the store's
datasource, and outside one is a single statement.

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

**What the filter saves.** A new session, and a session changed through the `HttpSession` API
(`set_attribute`, `remove_attribute`, `rotate_id`; `invalidate` deletes it). A value mutated in place (an item
appended to a list attribute, say) is saved only along with such a change: once the session was saved, an
in-place mutation alone is not saved again. Call `set_attribute` with the mutated value to save it.

**A revoked session stays revoked.** A session the store is known to hold (`stored_id`: loaded by the request,
or already saved by it) is written back through the store's `replace`, and moved to its new id through `rename`
when the request rotated it (`rotate_id()`, a privilege elevation), only while the store still holds it.
When the session was logged out (by another request of the same browser), evicted (by a login elsewhere under
`evict-oldest`) or expired while the request ran, its change is dropped, the session counts as invalidated and
the response sets no session cookie at all: the request neither brings the session back nor sends its cookie
again, and it does not clear the cookie either, since another request of the same browser (a login in another
tab, rotating the session) may have set a new one meanwhile, and it does not bring the session back under a new
id. A new session, and one rotated by a login (`rotate_id(on_login=True)`: a fresh authentication stands even
if the old entry is gone), is inserted with `save`, and the entry the store held the session under
(`stored_id`) is deleted, however many rotations came before, so no earlier id resolves to the session. With a
custom store that has no `replace` and `rename`, every change goes through `save`, which brings such a session
back.

**Deletions run to their end.** The deletion of an invalidated session (a logout, a refused or failed login)
and of a rotated session's old id runs in a task of its own, shielded from the request's cancellation: a
level-triggered cancel scope, as anyio's, cancels every await of the request's cleanup too, and a logout
cancelled that way (the client went away) left the session live. A deletion that fails after its request was
cancelled is logged as `session_delete_failed`.

`request.state.persist_session` (a coroutine function taking no arguments) saves the session at once. Every
save leaves the session unmodified (`mark_persisted()`), so the persist that runs when the handler returns
writes it again only for a later change, and then only while the store still holds it. The OAuth2 login
handler relies on it: it saves the session it logs in before registering it, and a concurrent login that evicts
that session meanwhile is not undone when the login's request ends, even if the application changes the
session after the handler returned (the change is dropped with the session).

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

- `redis` → `RedisSessionStore` (requires `redis.asyncio`; falls back to memory, with a WARNING, if unavailable)
- `postgres` → `SqlSessionStore` on the datasource `pyfly.session.postgres.*` names (the primary by default),
  resolved in the context's `DataSourceRegistry`
- `memory` → `InMemorySessionStore`; any other value raises `ValueError`

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
  The registry commits the eviction first; the deletion then runs outside the
  caller's unit of work and is shielded from the login's cancellation (the
  controller's `stop()` waits for deletions in flight, up to
  `EVICTION_STOP_TIMEOUT`, 30 seconds, and logs what is left as
  `session_eviction_unfinished`). A deletion that fails is
  logged as `session_eviction_failed` and the login goes on: that session is no
  longer counted and stays usable until it expires or is invalidated.
- `reject-new` — the new login is refused. The handler invalidates the
  pending session and responds with HTTP `401` and body
  `{"error": "max_sessions", ...}`.

**Only live sessions count, and the cap holds under concurrency.**

- A session counts toward the cap while the session store has it. At each login the controller drops the
  registrations of the principal's sessions the store no longer has (expired, invalidated by the
  application, lost in a restart), as Spring's `getAllSessions(principal, false)` leaves expired ones out, so
  a user whose sessions ended without a logout is never locked out. The login handler saves the session it
  logs in (`request.state.persist_session`, set by the `SessionFilter`) before registering it, so a
  concurrent login never takes it for a dead one. The handler makes every change (the security context, the
  redirect it consumes) before that save; afterwards the filter writes the session only for a change made
  through the `HttpSession` API, and only while the store still holds it (`replace`, see
  [`SessionFilter`](#sessionfilter)). So a session a concurrent login evicted stays evicted, and so does one
  evicted or logged out while any other request of it was running. If the save or the registration fails, the
  handler invalidates the session and the filter deletes it, so no logged-in session is left that the cap does
  not count. A logout deregisters the session: the OAuth2 login handler's logout, and the generic
  `LogoutFilter` when a controller is configured. Both invalidate the session first: a deregistration that
  fails (the registry's database or Redis down) is logged as `session_deregistration_failed` and never undoes
  the logout, and the controller drops the registration left behind as dead. This needs a store that holds every session the registry
  counts: beside a cross-process registry (`redis` or `postgres`), only a shared store (`store=postgres` or
  `redis`) does. With the in-memory store there, the auto-configuration does not give the store to the
  controller (it would take the other instances' live sessions for dead ones and admit logins over the cap),
  so every registered session counts until it logs out or is evicted (see [Registry Backends](#registry-backends)).
- Counting, evicting and registering is one atomic step of the registry
  (`AtomicSessionRegistry.register_limited`): one unit of work that holds the principal's row on SQL, one Lua
  script on Redis, one lock in memory. Concurrent logins of one principal, on one instance or several, never
  exceed the cap. A custom registry without that method is serialized per principal within the process.
  Logouts and the purge change SQL registrations without the principal's lock; on MariaDB, whose snapshot
  isolation refuses to evict a registration that changed after the login read it ("Record has changed since
  last read", error 1020), the login runs its unit again on a fresh read, up to three times, and then raises
  `OptimisticLockingFailureException`.
- The SQL and in-memory registries are purged when the controller has the store, whatever the cap (with
  max-sessions `-1` nothing else removes a registration but a logout): a registration comes due for a liveness
  check one session TTL after it was registered or renewed; `controller.purge_expired()` drops the due
  registrations whose session is gone and renews the others. A login also checks up to
  `LOGIN_PURGE_BATCH` (50) due registrations, at most once a minute per instance, and the next logins take the
  rest of a backlog; schedule `purge_expired()` to keep a large registry clean without them.

> **Before 26.09.08** the cap was a list and a register in separate transactions (max-sessions=1 let
> concurrent logins all in), and no registration was ever removed but by logout or eviction: with
> `reject-new`, a user whose sessions expired could never log in again. With `evict-oldest`, the filter
> also saved each login's session again when its request ended, bringing back the sessions concurrent logins
> had evicted: eight concurrent logins under a cap of one left eight logged-in sessions. And any request that
> changed its session saved it back when it ended (an insert-or-replace), so a session logged out or evicted
> while one of its requests ran was live again, authenticated its cookie, and the response sent that cookie
> anew.

### Registry Backends

The cap is enforced against a `SessionRegistry` — a per-principal index of live
session ids, kept separate from the `SessionStore`. Three backends ship out of
the box, selected by `pyfly.session.concurrency.registry`:

| `registry` | Implementation | Scope | Requirements |
|---|---|---|---|
| `memory` (default) | `InMemorySessionRegistry` | Single process only | none |
| `redis` | `RedisSessionRegistry` | Cross-process / multi-instance | `redis.asyncio` installed |
| `postgres` | `PostgresSessionRegistry` | Cross-process / durable | a relational datasource (any SQL backend) |

- **`memory`** — in-process index guarded by an `asyncio.Lock` (mirrors
  `InMemorySessionStore`). Each app instance counts only its own sessions, so
  the cap is **not** enforced across multiple processes. Suitable for
  single-node deployments, development, and testing. State is lost on restart.
  Its registrations come due for a liveness check one session TTL
  (`pyfly.session.ttl`) after they were registered or renewed, so the purge
  drops those of sessions that ended without a logout.
- **`redis`** — a cross-process index shared by all app instances. Each
  principal's live sessions are stored in a Redis sorted set (score =
  `created_at`, member = `session_id`), so `list_sessions` is naturally
  oldest-first. Requires `redis.asyncio`; if it is unavailable the
  auto-configuration falls back to the in-memory registry and logs a
  `session_registry_fallback` WARNING. The connection URL
  comes from `pyfly.session.concurrency.redis.url`, falling back to
  `pyfly.session.redis.url`, then `redis://localhost:6379/0`.
- **`postgres`** — a durable, queryable, cross-process index for
  relational-only deployments (no Redis required), on every backend SQLAlchemy
  supports. Registrations live in the framework table
  `pyfly_session_registrations` (`session_id` PK, `principal`, `created_at`,
  `expires_at`: when the registration comes due for a liveness check), and
  `pyfly_session_principals` holds one row per principal, which a capped login
  locks. A principal's row stays after its last session ends (the table grows
  with the number of principals who ever logged in under a cap, not with their
  logins): deleting it would race the next login that locks it. The tables are
  created when the controller starts if the schema
  strategy allows it, otherwise checked. The datasource is the one
  `pyfly.session.concurrency.postgres.datasource` names, or the one
  `pyfly.session.concurrency.postgres.url` resolves to in the context's
  `DataSourceRegistry`, or the primary. Registrations an earlier release wrote
  to `pyfly_session_registry` are not read (drop that table).

Pair a cross-process registry (`redis` or `postgres`) with a shared session
store (`store=postgres` or `redis`). With the in-memory store, each instance
knows only its own sessions, and the auto-configuration logs a
`session_registry_not_shared` WARNING. The cap still counts every instance's
registrations, but:

- evicting a session another instance holds removes its registration and leaves
  the session usable on that instance;
- dead sessions are not dropped: the controller does not ask a process-local
  store about the registrations (it would take the other instances' live
  sessions for dead ones), so a session that ends without a logout (expired,
  lost in a restart) counts until it logs out or is evicted (on Redis, at most
  until the principal's set expires, its `ttl` after the last login). With
  `reject-new`, such sessions can lock the principal out, as before 26.09.08.

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
| `pyfly.session.concurrency.registry` | `memory` | Registry backend: `memory`, `redis`, or `postgres` (case-insensitive); any other value raises `ValueError` |
| `pyfly.session.concurrency.postgres.datasource` | primary | The datasource of the SQL registry |
| `pyfly.session.concurrency.postgres.url` | — | Alias resolved through the `DataSourceRegistry` (a new `session-registry` datasource for another URL) |
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

The Redis client and the datasource are obtained in the auto-configuration
(the composition root) and injected into the adapters — the adapters never
import their driver at module scope (hexagonal wiring).

The controller gets the `SessionStore` (to tell live sessions from dead ones)
and its `delete` as the `session_deleter`, so an evicted session is deleted from
whichever store backend is active; it ends for every instance when the store is
shared (Redis or SQL). Beside a `redis` or `postgres` registry, the in-memory
store is passed as the `session_deleter` only (see
[Registry Backends](#registry-backends)). The controller is a lifecycle bean:
its start creates or checks the SQL registry's tables.

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

`AtomicSessionRegistry` adds the capped registration the controller uses when a
registry has it:

```python
from pyfly.session.concurrency import AtomicSessionRegistry, SessionRegistration

class AtomicSessionRegistry(Protocol):
    async def register_limited(
        self, principal: str, session_id: str, created_at: float, *, max_sessions: int, evict_oldest: bool
    ) -> SessionRegistration: ...  # SessionRegistration(accepted, evicted)
```

`InMemorySessionRegistry(*, ttl=timedelta(seconds=1800), clock=None)` is the
in-process implementation (guarded by an `asyncio.Lock`), the default used by
auto-configuration when `registry=memory` (with `ttl=pyfly.session.ttl`). Like
the SQL registry it is an `ExpiringSessionRegistry`: `ttl` (positive: anything
else raises `ValueError`) is how long a registration goes before its liveness
is checked again. Two cross-process implementations ship as adapters; both
have their driver/datasource injected by the composition root, and both
implement `register_limited`:

```python
from pyfly.session.adapters.redis_registry import RedisSessionRegistry
from pyfly.session.adapters.postgres_registry import PostgresSessionRegistry
```

`RedisSessionRegistry(client, *, key_prefix="pyfly:session:user:", ttl=86400)`
stores each principal's sessions in a Redis sorted set (oldest-first by
`created_at`). The `ttl` (seconds) bounds orphan growth and slides forward on
each `register`. Its `register_limited` is one Lua script over the set. Used
when `registry=redis`.

`PostgresSessionRegistry(engine_factory, *, table="pyfly_session_registrations", principals_table="pyfly_session_principals", ttl=timedelta(seconds=1800), create_table=True, clock=None)`
stores sessions in SQL tables. `engine_factory` is the datasource (an
`AsyncEngine`, a registry `DataSource`, a datasource name, or a callable
returning one, resolved on first use); the table names are validated as SQL
identifiers; `ttl` (the session timeout, positive: anything else raises
`ValueError`) is how long a registration goes before its liveness is checked
again. Its operations never join a unit of work of the caller. On SQLite, which
allows one writer at a time, an operation called inside a unit of work that has
written on the registry's datasource raises `IllegalTransactionStateError` (its
own unit would wait for the caller's write lock): call it outside that unit, or
give the registry a datasource of its own. Used when `registry=postgres`.

You may still provide your own `SessionRegistry` bean to override the
auto-configured one entirely.

`SessionConcurrencyController` enforces the policy:

| Method | Description |
|---|---|
| `__init__(registry, policy, *, session_deleter=None, session_store=None, purge_interval=timedelta(seconds=60))` | `session_store` tells live sessions from dead ones; `session_deleter` (by default the store's `delete`) is an `async (session_id) -> None` callable used to evict store entries |
| `on_login(principal, session_id, created_at)` | Under a cap (`max-sessions` >= 0), drops the principal's dead sessions, then registers the session atomically, enforcing the cap; with no cap, registers it. Returns `False` if rejected (`reject-new`), `True` otherwise. Register a session after saving it, and do not save it again (an eviction may have deleted it) |
| `on_logout(principal, session_id)` | Deregisters the session |
| `purge_expired()` | Drops the due registrations whose session is gone and renews the others (an `ExpiringSessionRegistry`: the SQL and in-memory registries); returns how many were dropped |

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
    session_store=store,
)

# allowed is False once the cap is exceeded under "reject-new"
allowed = await controller.on_login("alice", session_id="abc123", created_at=1717000000.0)
```

---

## See Also

- [Security](security.md) — JWT authentication, `@secure` decorator, `OAuth2SessionSecurityFilter`
- [Web Filters](web-filters.md) — `OncePerRequestFilter`, filter ordering, `WebFilterChainMiddleware`
