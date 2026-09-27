# Caching Guide

PyFly's caching module provides a declarative, decorator-based caching system
with pluggable backends. Following the hexagonal architecture pattern, a
`CacheAdapter` protocol defines the interface, and concrete adapters
(`InMemoryCache`, `RedisCacheAdapter`) supply the implementation. A
`CacheManager` adds automatic failover between a primary and fallback cache.

---

## Table of Contents

1. [Architecture Overview](#architecture-overview)
2. [CacheAdapter Protocol](#cacheadapter-protocol)
3. [InMemoryCache](#inmemorycache)
4. [RedisCacheAdapter](#rediscacheadapter)
5. [PostgresCacheAdapter](#postgrescacheadapter)
6. [CacheManager: Failover and Resilience](#cachemanager-failover-and-resilience)
7. [Named Caches: Regions and Dedicated Caches](#named-caches-regions-and-dedicated-caches)
8. [Caching and Transactions](#caching-and-transactions)
9. [Declarative Caching Decorators](#declarative-caching-decorators)
   - [@cache](#cache)
   - [@cacheable](#cacheable)
     - [Conditional Caching: condition and unless](#conditional-caching-condition-and-unless)
   - [@cache_put](#cache_put)
   - [@cache_evict](#cache_evict)
   - [Return Types, Hits and Failures](#return-types-hits-and-failures)
10. [Key Templates](#key-templates)
11. [Auto-Configuration](#auto-configuration)
12. [Configuration Reference](#configuration-reference)
13. [Complete Example: Product Catalog Service](#complete-example-product-catalog-service)
14. [Testing with InMemoryCache](#testing-with-inmemorycache)

---

## Architecture Overview

```
Application Code (decorators / direct calls)
          |
          v
    CacheAdapter  (protocol / port)
          |
          +-- InMemoryCache         (dev / test, single-process)
          +-- RedisCacheAdapter     (production, via redis.asyncio)
          +-- PostgresCacheAdapter  (production, via SQLAlchemy async + asyncpg)
          |
          v
    CacheManager  (optional: primary + fallback with auto-failover)
```

Your application depends only on the `CacheAdapter` protocol. You can swap
backends (in-memory to Redis) without changing a single line of business logic.
The `CacheManager` adds a resilience layer: a per-process fallback that serves
only while the shared primary fails.

---

## CacheAdapter Protocol

The `CacheAdapter` is a `@runtime_checkable` `Protocol` that all cache
backends must implement:

```python
from pyfly.cache import CacheAdapter

class CacheAdapter(Protocol):
    async def get(self, key: str) -> Any | None: ...
    async def put(self, key: str, value: Any, ttl: timedelta | None = None) -> None: ...
    async def put_if_absent(self, key: str, value: Any, ttl: timedelta | None = None) -> bool: ...
    async def evict(self, key: str) -> bool: ...
    async def evict_by_prefix(self, prefix: str) -> int: ...
    async def exists(self, key: str) -> bool: ...
    async def clear(self) -> None: ...
    async def start(self) -> None: ...
    async def stop(self) -> None: ...
```

### The Contract

Every built-in adapter keeps these rules, and a custom adapter should too:

* **Values are copies.** `put` stores a copy of the value, and `get` returns a
  value that belongs to the caller. Changing either never changes the entry, on
  any backend.
* **No live ORM objects.** `put` and `put_if_absent` raise `CacheValueError`
  (from `pyfly.cache`, a `TypeError`) for a SQLAlchemy-mapped instance or a
  Beanie document, also inside a list, a dict or a DTO, and for a value the
  backend cannot encode. Nothing is written. A cached entity would be shared by
  every request and outlive the session that loaded it: one request would see
  another's uncommitted edits, and after a rollback every hit would fail with
  `DetachedInstanceError`. Cache a DTO built from the entity instead.
* **`clear()` is this cache's own.** It removes this cache's entries and nothing
  else. An adapter over a store that other data may share (a Redis database, a
  database table) deletes its own namespace and never flushes the store.
* **Expired entries do not accumulate.** An entry past its TTL is never
  returned, and the store drops it even when no one reads it again (Redis
  expires keys itself, the in-memory cache sweeps them). The CQRS query cache
  writes one entry per caller's scope and relies on TTLs to bound its size.
* **Named caches (optional).** `with_namespace(name)` returns a cache disjoint
  from this one, which this cache's `clear()` never touches. The name cannot be
  empty or contain `:`, which ends a namespace (`with_namespace("a:b")` would
  live inside what `with_namespace("a").clear()` deletes): every built-in
  adapter raises `ValueError` for it. See
  [Named Caches](#named-caches-regions-and-dedicated-caches).

Every built-in adapter keeps these rules: the in-memory, Redis and PostgreSQL
ones (the PostgreSQL adapter keeps its entries under a namespace of the cache
table, and purges expired rows).

### Method Reference

| Method                           | Return Type   | Description |
|----------------------------------|---------------|-------------|
| `get(key)`                       | `Any \| None` | Retrieve a cached value by key. Returns `None` if the key does not exist or has expired. |
| `put(key, value, ttl=None)`      | `None`        | Store a value under the given key. If `ttl` is provided, the entry expires after the specified duration. |
| `put_if_absent(key, value, ttl=None)` | `bool`   | Store the value only when the key is absent, atomically. Returns whether it was stored. |
| `evict(key)`                     | `bool`        | Remove a specific key. Returns `True` if the key existed, `False` otherwise. |
| `evict_by_prefix(prefix)`        | `int`         | Remove every key of this cache that starts with `prefix` (taken literally). Returns how many were removed. |
| `exists(key)`                    | `bool`        | Check whether a key exists and has not expired. A stored `None` counts. |
| `clear()`                        | `None`        | Remove every entry of this cache, and nothing else in its store. |
| `start()`                        | `None`        | Initialize the cache backend (called during application startup). |
| `stop()`                         | `None`        | Shut down the cache backend (called during application shutdown). |

---

## InMemoryCache

The `InMemoryCache` is a simple dictionary-backed cache with optional TTL
support. It is suitable for **development, testing, and single-process
applications**.

It stores copies. `put` pickles the value (or deep-copies it when pickle cannot
handle it, for example an instance of a class defined inside a function), and
every `get` returns a new copy. Two requests never share one cached object, and
the in-memory cache behaves like the Redis and PostgreSQL ones. A live ORM
object is refused with `CacheValueError`.

The copies cost time on every call: a hit unpickles the entry, and a put
pickles it. For a page of 50 small Pydantic DTOs that is about 25 µs per hit and
40 µs per put, against a few microseconds for a shared reference. That is still
far below a database round trip, but cache small, flat DTOs rather than large
object graphs.

```python
from datetime import timedelta
from pyfly.cache.adapters.memory import InMemoryCache

cache = InMemoryCache()

# Store a value with a 5-minute TTL
await cache.put("user:123", {"name": "Alice", "email": "alice@example.com"}, ttl=timedelta(minutes=5))

# Retrieve it
user = await cache.get("user:123")  # {"name": "Alice", "email": "alice@example.com"}

# Check existence
exists = await cache.exists("user:123")  # True

# Evict a single key
removed = await cache.evict("user:123")  # True

# Clear everything
await cache.clear()
```

### How TTL Works

Internally, `InMemoryCache` stores each entry as a `(copy, expires_at)` tuple.
The `expires_at` is computed using `time.monotonic()` plus the TTL in seconds.

* On `get()`, if the current monotonic time exceeds `expires_at`, the entry is
  lazily deleted and `None` is returned.
* On `exists()`, the same expiration check is performed.
* If `ttl` is `None`, the entry never expires.

Expired entries are also swept from memory whenever the number of entries has
doubled since the last sweep (from 1024 entries on), a cost amortized over the
puts. Entries that expire and are never read again do not accumulate, so an
unbounded cache (`max_size=None`) is bounded by its TTLs.

---

## RedisCacheAdapter

The `RedisCacheAdapter` is the production cache backend. It delegates to a
`redis.asyncio.Redis` client and transparently handles JSON serialization.

The cache usually shares its Redis database with sessions, locks, the event bus
or other applications, so its entries live under a namespace, `pyfly:cache:` by
default. The keys you pass and get back are the cache's own: `user:123` is
stored as `pyfly:cache:user:123`. `clear()` deletes that namespace and nothing
else. The adapter never runs `FLUSHDB`.

**Install:** `uv add "pyfly[redis]"` (this pulls in `redis`).

```python
import redis.asyncio as redis
from pyfly.cache.adapters.redis import RedisCacheAdapter

client = redis.from_url("redis://localhost:6379/0")
cache = RedisCacheAdapter(client)

# Store
await cache.put("user:123", {"name": "Alice"}, ttl=timedelta(hours=1))

# Retrieve (JSON-deserialized automatically)
user = await cache.get("user:123")  # {"name": "Alice"}

# Evict
await cache.evict("user:123")

# Check existence
await cache.exists("user:123")  # False

# Clear the cache's own entries (the pyfly:cache: namespace), nothing else
await cache.clear()

await cache.start()   # Validate Redis connectivity
# ... use cache ...
await cache.stop()    # Close Redis connection
```

### Constructor

| Parameter   | Type                  | Default          | Description |
|-------------|-----------------------|------------------|-------------|
| `client`    | `redis.asyncio.Redis` | *required*       | An async Redis client instance. |
| `namespace` | `str`                 | `"pyfly:cache:"` | Keyword-only. The key prefix of the cache's entries; a `:` is appended when it does not end with one (`myapp` becomes `myapp:`), so `clear()` never reaches a namespace that merely starts with it. An empty namespace declares that the cache owns the whole database; `clear()` then deletes every key in it, the [dedicated caches](#named-caches-regions-and-dedicated-caches) of `with_namespace()` included (a `cache_not_dedicated` warning says so). |

### Serialization

Values are serialized to JSON before storage and deserialized on retrieval. The
encoder also handles datetimes, dates, `Decimal`, `UUID`, sets, bytes,
dataclasses and Pydantic models (their field names, without computed fields). A
hit comes back as JSON types (a Pydantic model as a `dict`), and the
[decorators](#return-types-hits-and-failures) and the CQRS query bus rebuild the
declared type, accepting field names and aliases. A live ORM object, or a value
JSON cannot represent, raises `CacheValueError` before anything is written.

### TTL Handling

When `ttl` is provided, the adapter passes `ex=int(ttl.total_seconds())` to
the Redis `SET` command. Redis handles expiration natively, so expired keys are
removed server-side without any lazy-deletion overhead.

### Additional Methods

| Method    | Description |
|-----------|-------------|
| `start()` | Validates connectivity by pinging Redis (`await client.ping()`). Called automatically during application startup. |
| `stop()` | Closes the underlying Redis connection (`await client.aclose()`). Called automatically during application shutdown. A `with_namespace` cache leaves the client to its source. |
| `get_keys(pattern, limit)` | Up to `limit` of the cache's keys matching a glob `pattern`, through `SCAN`. |
| `with_namespace(name)` | A cache dedicated to `name` on the same client (`pyfly:cache.<name>:`), which `clear()` never touches. A name that is empty or contains `:` raises `ValueError`. |

`evict_by_prefix` and `clear` walk the database once with `SCAN` (`COUNT 1000`)
and delete in batches. Glob characters in a prefix are taken literally.

> **Note:** When using auto-configuration, `start()` and `stop()` are called automatically
> by the `ApplicationContext` during startup and shutdown. You only need to call them
> manually if you create a cache adapter outside the DI container.

---

## PostgresCacheAdapter

The `PostgresCacheAdapter` is a **durable, production-grade** cache backend
backed by a SQL table on one of the application's datasources. It is suited for
environments where Redis is not available but PostgreSQL already is, or when
cache durability across process restarts is required. PostgreSQL is its main
target; the table and its statements are portable, so it also runs on SQLite,
MySQL and MariaDB.

**Install:** `uv add "pyfly[data-relational,postgresql]"` (this pulls in
`sqlalchemy[asyncio]` and `asyncpg`). A clear `ValueError` is raised at
startup if `sqlalchemy.ext.asyncio` is not importable.

```python
from sqlalchemy.ext.asyncio import create_async_engine
from pyfly.cache.adapters.postgres import PostgresCacheAdapter

engine = create_async_engine("postgresql+asyncpg://user:pass@host/db")
cache = PostgresCacheAdapter(engine)

# Creates the table if it is missing (the first operation does it too):
await cache.start()

# Store a value with a 10-minute TTL
from datetime import timedelta
await cache.put("user:123", {"name": "Alice"}, ttl=timedelta(minutes=10))

# Retrieve (deserialized automatically)
user = await cache.get("user:123")   # {"name": "Alice"}

# Evict a single key
await cache.evict("user:123")

# Evict all keys sharing a prefix
count = await cache.evict_by_prefix("user:")

# Clear this cache's entries (its namespace), nothing else in the table
await cache.clear()
```

The entries live under a **namespace** of the table, `pyfly:cache:` by default,
as on Redis: keys passed to and returned by the adapter are the cache's own
(`user:123` is stored as `pyfly:cache:user:123`), and `clear()` deletes that
namespace and nothing else in the table, which the
[dedicated caches](#named-caches-regions-and-dedicated-caches) of
`with_namespace()`, other applications and older entries may share.
`with_namespace(name)` returns a cache of its own on the same table and
datasource (`pyfly:cache.<name>:`), which the root cache's `clear()` never
touches. An empty namespace declares that the cache owns the whole table:
`clear()` then empties it, the dedicated caches included (a
`cache_not_dedicated` warning says so when the first one is made).

Entries written before 26.09.08 have no namespace: they are no longer read
(each key misses once), and those without a TTL stay in the table until you
delete them (`DELETE FROM pyfly_cache_entries WHERE cache_key NOT LIKE
'pyfly:cache%'`, adjusted to the namespaces you use).

### Constructor

| Parameter | Type | Description |
|-----------|------|-------------|
| `engine` | `AsyncEngine`, `DataSource` or datasource name | Where the entries live. The adapter does not dispose it on `stop()`: the engine belongs to the datasource registry (or to the caller). |
| `create_table` | `bool` (default `True`) | Create the table on `start()` when it is missing; with `False` the table is only checked, and a missing one fails the start. |
| `purge_interval` | `timedelta \| None` (default 60 s) | How often a write purges a batch of expired rows; `None` leaves purging to `purge_expired()`. |
| `table_name` | `str` (default `pyfly_cache_entries`) | The table. |
| `namespace` | `str` (default `pyfly:cache:`) | Keyword-only. The key prefix of the cache's entries; a `:` is appended when it does not end with one, so `clear()` never reaches a namespace that merely starts with it. An empty namespace declares that the cache owns the whole table. |

### Table schema

The table `pyfly_cache_entries` is a framework table
(`pyfly.data.relational.framework_schema.cache_entries`), so an Alembic
`env.py` that lists `framework_metadata` in `target_metadata` migrates it and
never drops it. On PostgreSQL it is:

```sql
CREATE TABLE pyfly_cache_entries (
    cache_key   TEXT NOT NULL PRIMARY KEY,
    value       BYTEA NOT NULL,
    expires_at  TIMESTAMP WITH TIME ZONE
);
CREATE INDEX ix_pyfly_cache_entries_expires_at ON pyfly_cache_entries (expires_at);
```

On MySQL and MariaDB the key is `VARCHAR(512)` (keys are limited to 512
characters there, the namespace included: a longer one is refused with
`ValueError` before anything is written, which the cache decorators and the
CQRS query cache log and skip, so the call is answered uncached; a CQRS query
key is about 170 characters plus the query class name) with a binary collation, the value `LONGBLOB` and the expiry
`DATETIME(6)` in UTC. Keys match exactly on every backend: `User:1` and
`user:1` are two entries (the default MySQL and MariaDB collations ignore case
and accents), and so are `get_keys()` patterns and `evict_by_prefix()`
prefixes, which use `GLOB` on SQLite, whose `LIKE` ignores case.

Values are serialized to bytes before storage and deserialized on retrieval,
so any serializable Python object can be cached transparently.

### TTL and expiry

When `ttl` is provided the adapter stores the expiry as a UTC instant in
`expires_at`, bound as a typed value: an entry expires after its TTL whatever
the process's time zone, and nodes in different zones agree. Expiry is
enforced at read time (`get()`, `exists()`, `get_keys()` and `get_stats()`
skip expired rows), and expired rows are **purged**: at most once per
`pyfly.cache.postgres.purge-interval` (60 s by default) a write deletes one
batch of them (`PURGE_BATCH`, 1000 rows), after its transaction commits. While
a batch comes back full the purge stays due and the next write deletes the
next batch, so a backlog (the expired rows of a release that never purged) is
worked off a batch per write instead of landing on one request.
`purge_expired()` deletes them all on demand, for a scheduled sweep.

### Write semantics

`put()` is the dialect's upsert (`INSERT ... ON CONFLICT (cache_key) DO UPDATE`
on PostgreSQL and SQLite, `ON DUPLICATE KEY UPDATE` on MySQL and MariaDB).
`put_if_absent()` stores the value when the key is absent **or its entry
expired**, and returns `True` only when this call stored it: on PostgreSQL and
SQLite it is one `INSERT ... ON CONFLICT ... DO UPDATE ... WHERE expires_at <= now`.
A lock or a dedupe marker with a TTL therefore comes free once it expires, as
with Redis `SET NX`.

Outside a transaction every operation is a single statement on an autocommit
connection on PostgreSQL: one round trip instead of `BEGIN`, the statement and
`COMMIT`. Called directly inside a unit of work on the cache's datasource, the
adapter joins it, so an entry written by a transaction that rolls back is
rolled back too; a write made inside a read-only unit gets a unit of its own.

Through the cache decorators and the CQRS query cache (both wrap it in
`TransactionAwareCache`, see [Caching and Transactions](#caching-and-transactions))
it does not join the caller's unit: writes wait for the commit, and what runs at
once (reads, `put_if_absent`, `evict_if_present`, `invalidate`) runs outside the
caller's unit, each statement in a short unit of its own. **Pool sizing:** while
a business unit holds a connection of the cache's datasource, each such cache
statement checks out one more pooled connection of it. Size
`pyfly.data.relational.pool.*` (or the cache datasource's pool) for one extra
connection per request that uses the cache inside a transaction, or put the
cache on a datasource of its own (`pyfly.cache.postgres.datasource`).

Prefix eviction (`evict_by_prefix`) takes the prefix literally, translates it
to a SQL `LIKE` pattern (a `GLOB` on SQLite) inside the cache's namespace, and
deletes all matching rows in a single statement.

### Additional methods

| Method | Description |
|--------|-------------|
| `get_keys(pattern, limit)` | Return up to `limit` of this cache's non-expired keys matching a glob pattern (`*` / `?`; a backslash makes the next character literal), without the namespace. |
| `get_stats()` | Return a `dict` with `size` (this cache's live entries), `type`, `namespace`, `requests`, `hits`, `misses`, `evictions`, `hit_rate`. |
| `with_namespace(name)` | A cache dedicated to `name` on the same table (`pyfly:cache.<name>:`), which `clear()` never touches. A name that is empty or contains `:` raises `ValueError`. |
| `purge_expired()` | Delete every expired entry now; returns how many were deleted. |

### Auto-configuration

Set `pyfly.cache.provider=postgres`. The cache is on the primary datasource
unless you name another one or give its URL:

```yaml
pyfly:
  cache:
    enabled: true
    provider: postgres
    postgres:
      datasource: caching          # a datasource of pyfly.data.relational.datasources
      # url: postgresql+asyncpg://user:pass@host/db   (or its URL; not both)
      purge-interval: 60s          # seconds or a duration (500ms, 2m, 1h); 0 turns the purge on writes off
```

The table is created at startup if `pyfly.data.relational.ddl-auto` is
`create` (the default), `create-drop` or `update`; with `none` or `validate` it
must exist (a migration creates it) or the startup fails, naming it.

If `sqlalchemy.ext.asyncio` is not installed, a `ValueError` is raised
immediately at startup with a message directing you to install
`pyfly[data-relational,postgresql]`.

---

## CacheManager: Failover and Resilience

The `CacheManager` wraps a **primary** cache and a **fallback** cache, adding
automatic failover:

```python
from pyfly.cache import CacheManager
from pyfly.cache.adapters.memory import InMemoryCache
from pyfly.cache.adapters.redis import RedisCacheAdapter

primary = RedisCacheAdapter(redis_client)
fallback = InMemoryCache()

manager = CacheManager(primary=primary, fallback=fallback)
```

### Constructor

| Parameter      | Type                | Default          | Description |
|----------------|---------------------|------------------|-------------|
| `primary`      | `CacheAdapter`      | *required*       | The shared cache backend (typically Redis). |
| `fallback`     | `CacheAdapter`      | *required*       | The per-process fallback (typically in-memory). |
| `fallback_ttl` | `timedelta \| None` | 60 seconds       | Keyword-only. The longest a value written during an outage lives in the fallback. `None` keeps the TTL each write asks for. |

### Behavior

The primary is the only source of truth while it answers. The fallback is used
only while the primary **fails** (raises).

| Operation | Behavior |
|-----------|----------|
| `get(key)` | Read the primary. A miss is a miss: the fallback is not consulted. If the primary raises, read the fallback. |
| `put(key, value, ttl)` | Write the primary only. If it raises, write the fallback, with the TTL capped at `fallback_ttl`. |
| `evict(key)` | Evict from both primary and fallback. Returns `True` if either had the key. |
| `clear()` | Clear both primary and fallback (each its own entries). |
| `exists`, `put_if_absent` | The primary; the fallback only while the primary raises. |

This design means that:

* An eviction on one node reaches every node. Each node used to keep its own
  copy in its fallback and serve it after another node evicted the entry, so
  different replicas served different, old versions.
* If Redis goes down, reads and writes degrade to the in-memory fallback, whose
  entries expire within `fallback_ttl` because no other node can evict them.
* The first operation Redis answers again ends the outage and clears the
  fallback, so nothing written during one outage is served during the next.
* A value the cache refuses (`CacheValueError`) is not an outage. It propagates,
  and the fallback is left alone.

### Logging

Failover events are logged via the `pyfly.cache` logger:

```
WARNING  Primary cache failed for GET 'user:123', using the fallback until it recovers
INFO     Primary cache recovered; clearing the fallback written during the outage
```

---

## Named Caches: Regions and Dedicated Caches

One `CacheAdapter` bean serves several consumers: the decorators, the CQRS query
cache, HTTP idempotency records and orchestration state. They must not clear
each other's entries, so PyFly gives them named caches:

* A **region** is a named part of a cache. `cache_region(cache, "users")`
  returns a `PrefixedCache` whose keys live under `users::` and whose `clear()`
  evicts that prefix only. Clearing the cache it belongs to clears the region
  too, which is right for evictable data. The CQRS query cache is the `:cqrs:`
  region of the application cache, and the decorators' `cache_name` parameter
  names a region.
* A **dedicated cache** is disjoint from the cache it comes from.
  `dedicated_cache(cache, "idempotency")` calls the adapter's
  `with_namespace("idempotency")`: a separate store in memory, the
  `pyfly:cache.idempotency:` namespace on Redis and in the PostgreSQL adapter's
  table. Clearing the application cache
  never touches it. The HTTP idempotency filter and the cache-backed
  orchestration persistence keep their records in dedicated caches.

```python
from pyfly.cache import cache_region, dedicated_cache

users = cache_region(cache, "users")          # cleared with the cache
tokens = dedicated_cache(cache, "tokens")     # survives cache.clear()
await users.clear()                           # evicts users:: only
```

For an adapter without `with_namespace`, `dedicated_cache` falls back to the
region `pyfly.<name>::` and logs a warning once: its entries are then cleared
with the cache.

### Upgrading from 26.09.07 or earlier

Data written by earlier versions is not where this version reads it:

* **Redis cache entries** now live under `pyfly:cache:`. Entries written
  before, under their bare keys, are never read again: each costs one miss.
  Entries written without a TTL stay in Redis until you delete them.
* **PostgreSQL cache entries** (`pyfly_cache_entries`) now live under the
  `pyfly:cache:` namespace too, with the same consequences; see
  [PostgresCacheAdapter](#postgrescacheadapter).
* **Orchestration state** kept by `CachePersistenceProvider` and **idempotency
  records** kept by the HTTP idempotency filter moved to their dedicated caches
  (`orchestration`, `idempotency`), and records written before the upgrade are
  not read. Drain the sagas and workflows in flight before you upgrade: one
  persisted before it is orphaned from recovery and compensation. A request
  retried with an idempotency key first used before the upgrade runs again
  instead of being replayed; deploy when no such retry is expected.
* **CQRS query-cache entries** are keyed by the caller's scope, and the default
  query keys (`<QueryClass>:<digest>`, now also for a query that is not a
  dataclass) and scope digests are full SHA-256 digests (64 hex characters, 16
  before) that cover the query class's module. Entries written before are never
  read again: each costs one miss and expires with its TTL.

---

## Caching and Transactions

Writes to a cache inside a unit of work (`@transactional`, a
`TransactionTemplate` block, a repository call) wait for its commit.
`TransactionAwareCache` (Spring's `TransactionAwareCacheDecorator`) registers
`put`, `evict`, `evict_by_prefix` and `clear` as
[after-commit synchronizations](data-relational.md#synchronizations):

* they run once the unit has committed, so no request is served a value that was
  never committed, and no concurrent reader re-caches the old value after an
  eviction that ran too early;
* they are dropped when the unit rolls back or its commit fails, which leaves the
  cache consistent with the database.

Outside a unit they run at once. Reads, `put_if_absent`, and the explicitly
immediate `evict_if_present(key)` and `invalidate()` always run at once. A
deferred `put` stores a copy of the value taken when it was registered, so a
value the cache refuses is refused right there, before the commit.

**What runs at once runs outside the caller's unit.** A cache on a database
that joins the unit bound for its datasource (an adapter that runs through
`infrastructure_unit()`) would otherwise run a read, a `put_if_absent` or an
`evict_if_present` inside the business transaction: the caller's rollback
would undo it, a plain query of another request would wait for that unit's row
locks on the cache table, and two business units reading two keys in opposite
orders could deadlock and roll back. `TransactionAwareCache` runs every
immediate operation, and the writes of a task that outlived its unit, inside
[`outside_transaction()`](data-relational.md#work-outside-the-callers-unit):
each statement gets a short unit of its own, in the calling task.

* **One more pooled connection.** During a business unit, each statement on a
  database-backed cache checks out another connection of the cache's
  datasource while the unit keeps its own. Size the pool for it, or put the
  cache on a datasource of its own. Redis and in-memory caches are not
  affected.
* **SQLite has one writer.** A write unit holds the database's write lock from
  its `BEGIN IMMEDIATE`, so an immediate write to a cache on that same database
  cannot run beside it: it is refused at once with
  `IllegalTransactionStateError` instead of waiting `busy_timeout` for a lock
  its own task holds; reads still run. With `on_write_error="raise"` (the
  default of `TransactionAwareCache`) the refusal propagates. With `"log"` (the
  declarative decorators and the CQRS query cache) the business call goes on:
  the refusal is logged once per cache (`cache_<operation>_refused` at
  `WARNING`, later ones at `DEBUG`), `put_if_absent` answers `False` (the query
  is answered uncached), and a refused `evict_if_present` or `invalidate`
  (`@cache_evict(before_invocation=True)`) runs after the commit instead.

**Cancellation.** A client disconnect cancels the request's anyio scope, which
cancels every await that follows, and it often lands just as a `@transactional`
body returns. The unit still commits (the commit is shielded), and so do the
writes that waited for it: the unit runs every after-commit callback to
completion, and the cancellation is re-raised once they all ran. Outside a unit,
an eviction or a clear runs to completion in a shielded task of its own. An
eviction lost there would leave the old value cached for its whole TTL although
the database committed the new one. A shielded write cannot be cancelled either:
give the cache client a timeout (`socket_timeout` on a `redis.asyncio` client,
which has none by default), or a write that hangs during a network partition
holds the request, its timeouts and shutdown until it returns.

**A task that outlived its unit** (one the unit's body started and did not
await) has no commit left to wait for. Its `put` runs at once when the unit
committed, and is dropped, with a `cache_put_skipped` log line, otherwise. Its
eviction or clear runs at once unless the unit rolled back, also while the unit
is still completing or when its outcome is unknown: dropping it would leave the
old value cached for its TTL if the commit succeeds. An eviction that runs while
the unit is still completing runs before the commit, though, and a concurrent
reader can then re-cache the old value until its TTL expires: await such work
inside the unit, or evict again once it has committed. The call itself never
fails for it, and its writes run outside the unit it outlived.

`apply(operation, key, write)` makes several writes on the delegate one
deferred step: they run after the commit together, or are dropped together (the
CQRS query cache evicts a key under every prefix, with its generation, this
way).

Two consequences of writes that wait for the commit:

* **The unit does not see its own writes.** A `@cacheable` read after a
  `@cache_evict` in the same transaction is still served the old entry, and a
  value a `@cache_put` stored is not there yet. Read from the database inside
  the unit when you need what it changed.
* **Synchronizations belong to the unit, not to a savepoint.** A write made
  inside a `Propagation.NESTED` step that rolls back to its savepoint still runs
  when the outer unit commits, as in Spring: a `@cache_put` there caches a value
  that was never committed. Keep cache writes out of `NESTED` steps that may
  fail, or evict the key where you handle the failed step: that eviction is
  registered after the put, so it runs after it.

The [decorators](#declarative-caching-decorators) and the CQRS query cache are
transaction-aware. Wrap an adapter yourself to get the same behavior from direct
calls:

```python
from pyfly.cache import TransactionAwareCache

tx_cache = TransactionAwareCache(cache)

@transactional
async def rename(self, product_id: int, name: str) -> None:
    await self.products.rename(product_id, name)
    await tx_cache.evict(f"product:{product_id}")   # runs after the commit
```

With `on_write_error="log"`, a failing write is logged (a put at `WARNING`, an
eviction at `ERROR`) instead of raised.

---

## Declarative Caching Decorators

PyFly provides four decorators for declarative caching. They handle cache key
resolution, lookup, and storage automatically based on function arguments.

The decorators are [transaction-aware](#caching-and-transactions): inside a unit
of work, the value `@cache`/`@cacheable`/`@cache_put` store and the eviction
`@cache_evict` makes wait for the commit, and are dropped on rollback. Every
decorator accepts `cache_name`, a named [region](#named-caches-regions-and-dedicated-caches)
of the backend that `@cache_evict(all_entries=True, cache_name=...)` clears on
its own.

### @cache

The primary caching decorator. On a **cache hit**, the decorated function is
**not executed** -- the cached value is returned directly. On a cache miss, the
function executes and the result is stored.

```python
from datetime import timedelta
from pyfly.cache import cache
from pyfly.cache.adapters.memory import InMemoryCache

backend = InMemoryCache()

@cache(backend=backend, key="user:{user_id}", ttl=timedelta(minutes=10))
async def get_user(user_id: str) -> dict:
    # This body only executes on cache miss
    return await database.find_user(user_id)

# First call: cache miss -> executes function, stores result
user = await get_user("123")

# Second call: cache hit -> returns cached value, function not called
user = await get_user("123")
```

#### Parameters

| Parameter | Type                  | Default    | Description |
|-----------|-----------------------|------------|-------------|
| `backend` | `CacheAdapter`        | *required* | The cache backend to use. |
| `key`     | `str`                 | *required* | Key template with `{param}` placeholders (see [Key Templates](#key-templates)). |
| `ttl`     | `timedelta \| None`   | `None`     | Time-to-live. `None` means the entry never expires. |

### @cacheable

An alias for `@cache`. They are functionally identical:

```python
from pyfly.cache import cacheable

@cacheable(backend=backend, key="order:{order_id}", ttl=timedelta(minutes=5))
async def get_order(order_id: str) -> dict:
    return await database.find_order(order_id)
```

Use whichever name reads better in your codebase. `@cacheable` may feel more
natural if you are coming from Spring Framework.

#### Conditional Caching: `condition` and `unless`

Both `@cache` and `@cacheable` accept two keyword-only predicates that mirror
Spring's `@Cacheable(condition=..., unless=...)`:

* **`condition`** — a callable with the **same signature as the decorated
  function**, evaluated on the call arguments *before* the cache is touched.
  When it returns `False`, caching is bypassed entirely: the function runs and
  nothing is read from or written to the cache.
* **`unless`** — a callable that receives the **result**, evaluated *after* the
  function executes. When it returns `True`, the result is returned to the
  caller but **not stored**.

```python
from pyfly.cache import cacheable

@cacheable(
    backend=backend,
    key="user:{user_id}",
    ttl=timedelta(minutes=10),
    condition=lambda user_id, include_drafts=False: not include_drafts,
    unless=lambda result: result is None,
)
async def get_user(user_id: str, include_drafts: bool = False) -> dict | None:
    return await database.find_user(user_id, include_drafts)

# include_drafts=True  -> condition False, cache bypassed (always executes)
await get_user("123", include_drafts=True)

# result is None       -> unless True, returned but not stored
await get_user("missing")

# normal call          -> cached; second call is a hit (body not executed)
await get_user("123")
await get_user("123")
```

> **Note:** `condition` is called with the *same positional and keyword
> arguments* as the wrapped function, so for methods it also receives `self` as
> the first argument. Accept it explicitly (e.g. `lambda self, user_id: ...`)
> when decorating instance methods.

#### Parameters

| Parameter   | Type                          | Default    | Description |
|-------------|-------------------------------|------------|-------------|
| `backend`   | `CacheAdapter`                | *required* | The cache backend to use. |
| `key`       | `str`                         | *required* | Key template with `{param}` placeholders. |
| `ttl`       | `timedelta \| None`           | `None`     | Time-to-live. `None` means the entry never expires. |
| `condition` | `Callable[..., bool] \| None` | `None`     | Keyword-only. Predicate over the call arguments; returning `False` bypasses the cache (no read or write). |
| `unless`    | `Callable[[Any], bool] \| None` | `None`   | Keyword-only. Predicate over the result; returning `True` returns the value without storing it. |
| `cache_name` | `str \| None`               | `None`     | Keyword-only. A named region of `backend` (keys under `<cache_name>::`). |

### @cache_put

Always executes the function and stores the result in the cache. Unlike
`@cache`/`@cacheable`, it **never** skips function execution -- the function
body runs every time.

This is ideal for **update operations** where you want to refresh the cached
value to match the latest state.

```python
from pyfly.cache import cache_put

@cache_put(backend=backend, key="user:{user_id}", ttl=timedelta(minutes=10))
async def update_user(user_id: str, data: dict) -> dict:
    # Always executes, then caches the returned value
    updated = await database.update_user(user_id, data)
    return updated
```

#### Parameters

| Parameter | Type                  | Default    | Description |
|-----------|-----------------------|------------|-------------|
| `backend` | `CacheAdapter`        | *required* | The cache backend to use. |
| `key`     | `str`                 | *required* | Key template with `{param}` placeholders. |
| `ttl`     | `timedelta \| None`   | `None`     | Time-to-live for the updated cache entry. |
| `cache_name` | `str \| None`      | `None`     | Keyword-only. A named region of `backend`. |

#### @cache vs. @cache_put

| Aspect              | `@cache` / `@cacheable` | `@cache_put` |
|---------------------|-------------------------|--------------|
| Cache hit behavior  | Returns cached value; function **not** called. | Function **always** called; result replaces cache entry. |
| Cache miss behavior | Calls function; caches result. | Calls function; caches result. |
| Best for            | Read operations (lookups). | Write/update operations. |

### @cache_evict

Removes a cache entry (or clears the cache) **after** the decorated function
executes, and after the commit when it runs inside a unit of work. An eviction
before the commit would let a concurrent reader re-cache the old row for good.

```python
from pyfly.cache import cache_evict

# Evict a specific key
@cache_evict(backend=backend, key="user:{user_id}")
async def delete_user(user_id: str) -> None:
    await database.delete_user(user_id)
    # After this returns, cache entry "user:{user_id}" is evicted

# Clear the "users" region only
@cache_evict(backend=backend, all_entries=True, cache_name="users")
async def purge_all_users() -> None:
    await database.delete_all_users()
    # After this returns (after the commit), the users:: entries are evicted
```

#### Parameters

| Parameter     | Type           | Default    | Description |
|---------------|----------------|------------|-------------|
| `backend`     | `CacheAdapter` | *required* | The cache backend to use. |
| `key`         | `str`          | `""`       | Key template with `{param}` placeholders. Ignored when `all_entries=True`. |
| `all_entries` | `bool`         | `False`    | When `True`, clears the `cache_name` region, or `backend` itself (its own entries only) when no region is named. |
| `before_invocation` | `bool`   | `False`    | Keyword-only. Evict at once, before the method runs, even inside a unit of work (Spring's `beforeInvocation`). A failure is logged and the method still runs; an eviction that cannot run beside the caller's unit of work (SQLite, a cache on the same database) runs after its commit instead. |
| `cache_name`  | `str \| None`  | `None`     | Keyword-only. A named region of `backend`. |

### Return Types, Hits and Failures

* **The declared return type is checked when a method is decorated.** A type the
  cache cannot hold, an ORM-mapped class or a Beanie document (also as
  `list[Order]` or `Order | None`), raises `TypeError` right there. A forward
  reference that cannot be resolved yet is checked at the first call, before the
  method runs.
* **A hit has the declared type.** The decorators rebuild a hit with a Pydantic
  `TypeAdapter` of the return annotation, so a Redis or PostgreSQL cache returns
  the DTO, dataclass, `datetime` or `Decimal` the method declares, not a `dict` or
  a string. The JSON encoder stores a model's field names and leaves computed
  fields out; the hit is validated by field name and by alias, so models with
  camelCase aliases (`alias_generator=to_camel`), `Field(alias=...)` or a
  `computed_field` on an `extra="forbid"` model come back as they went in. An
  `Iterable[X]` comes back as the `list[X]` that was stored. A structural type
  (a `typing.Protocol`) cannot be rebuilt, so its hit is returned as stored: the
  copy on the in-memory cache, plain JSON types on Redis or PostgreSQL.
* **A hit is validated against the annotation.** It is coerced like any Pydantic
  input: a method declared `-> int` that returned `"42"` gets `42` from a hit. An
  entry that does not fit the type is treated as a miss and overwritten, with one
  warning per method (`cache_hit_discarded`): an entry written by an older version
  of the model, a method that returns something other than its declared type (it
  then runs on every call), or a model field declared `Field(exclude=True)`
  without a default, which the encoder leaves out. Annotate cached methods with
  what they return.
* **A cache failure never changes the outcome of the call.** Once the method has
  run (and may have committed), a put or an eviction that fails, including a value
  the cache refuses, is logged (`cache_put_skipped`/`cache_evict_skipped` on the
  `pyfly.cache` logger) and the result is returned. Raising there made the
  client retry a write that had already committed. A failing read, before the
  method runs, still propagates. A bad key template raises before the method
  runs.

---

## Key Templates

All caching decorators support **key templates** with `{param}` placeholders.
Placeholders are resolved from the decorated function's argument names.

```python
@cache(backend=backend, key="order:{customer_id}:{order_id}")
async def get_order(customer_id: str, order_id: str) -> dict:
    ...

# get_order("abc", "123") -> cache key is "order:abc:123"
# get_order("xyz", "456") -> cache key is "order:xyz:456"
```

### How Resolution Works

1. The decorator inspects the function signature with `inspect.signature()`.
2. It binds the actual call arguments with `sig.bind(*args, **kwargs)`.
3. It applies defaults with `bound.apply_defaults()`.
4. It calls `key.format(**bound.arguments)` to produce the resolved key.

This means you can reference any parameter by name, including keyword-only
arguments and arguments with default values:

```python
@cache(backend=backend, key="search:{query}:page:{page}")
async def search_products(query: str, page: int = 1) -> list[dict]:
    ...

# search_products("shoes")      -> key "search:shoes:page:1"
# search_products("shoes", 3)   -> key "search:shoes:page:3"
```

### Self Parameter

When decorating methods on a class, `self` is included in the bound arguments.
Avoid using `{self}` in your key template -- it would produce the object's
`repr`, which is not useful. Instead, reference only the meaningful parameters:

```python
class ProductService:
    @cache(backend=backend, key="product:{product_id}")
    async def get_product(self, product_id: str) -> dict:
        ...
```

---

## Auto-Configuration

When using automatic configuration, PyFly selects the cache adapter based on
the `pyfly.cache.provider` setting (default `auto`). When `auto` is selected,
the best available adapter is detected at startup:

| Detection Order | Library Checked         | Adapter Selected        |
|-----------------|-------------------------|-------------------------|
| 1               | `redis.asyncio`         | `RedisCacheAdapter`     |
| 2               | *(fallback)*            | `InMemoryCache`         |

You can also pin a provider explicitly:

| `pyfly.cache.provider` value | Adapter Selected      | Required packages |
|------------------------------|-----------------------|-------------------|
| `auto`                       | Detected (see above)  | —                 |
| `memory`                     | `InMemoryCache`       | None              |
| `redis`                      | `RedisCacheAdapter`   | `redis[hiredis]`  |
| `postgres`                   | `PostgresCacheAdapter` | `sqlalchemy[asyncio]` + `asyncpg` |

When Redis is detected in auto mode, the `CacheManager` can be configured with
`RedisCacheAdapter` as the primary and `InMemoryCache` as the fallback for
automatic failover.

---

## Configuration Reference

Configure caching in your `pyfly.yaml`:

```yaml
pyfly:
  cache:
    enabled: false
    provider: auto      # auto | memory | redis | postgres
    ttl: 300            # Default TTL in seconds (5 minutes)

    redis:
      url: redis://localhost:6379/0

    postgres:
      url: postgresql+asyncpg://user:pass@host/db
```

| Property                    | Default                      | Description |
|-----------------------------|------------------------------|-------------|
| `pyfly.cache.enabled`      | `false`                      | Enable or disable caching globally. |
| `pyfly.cache.provider`     | `"auto"`                     | Cache provider: `"auto"`, `"memory"`, `"redis"`, or `"postgres"`. |
| `pyfly.cache.ttl`          | `300`                        | Default TTL in seconds, applied when decorators do not specify their own TTL. |
| `pyfly.cache.redis.url`    | `"redis://localhost:6379/0"` | Redis connection URL (used when provider is `"redis"` or auto-detected). |
| `pyfly.cache.postgres.url` | *(none)*: the primary datasource | PostgreSQL connection URL (used when provider is `"postgres"`). See below. |
| `pyfly.cache.postgres.datasource` | *(none)*: the primary datasource | Name of the datasource the cache table lives on (instead of a URL). |
| `pyfly.cache.postgres.purge-interval` | `60` | Time between purges of expired rows made by writes: seconds or a duration (`90s`, `500ms`, `2m`, `1h`); `0` turns them off. |

`pyfly.cache.postgres.url` resolves through the
[datasource registry](data-relational.md#module-datasources) (the application's `DataSourceRegistry` bean
when it defines one), and `pyfly.cache.postgres.datasource` names one of its datasources; setting both is
an error. With neither, the cache uses the primary datasource (`pyfly.data.relational.url`), and with no
primary either, startup fails with an error that names both keys. Before 26.09.08 it connected to `postgresql+asyncpg://localhost:5432/cache`. A URL
identical to a registered datasource's reuses that datasource's engine. Another URL registers the
`cache` datasource, which gets the primary's pool settings and is disposed on shutdown.

---

## Complete Example: Product Catalog Service

This example demonstrates a realistic service that caches product lookups,
updates the cache on product modifications, and evicts entries on deletion.

```python
from dataclasses import dataclass
from datetime import timedelta

from pyfly.container import service, configuration, bean
from pyfly.cache import (
    CacheAdapter,
    CacheManager,
    cache,
    cache_evict,
    cache_put,
)
from pyfly.cache.adapters.memory import InMemoryCache
from pyfly.cache.adapters.redis import RedisCacheAdapter


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

@configuration
class CacheConfig:
    """Wire up caching with Redis primary + in-memory fallback."""

    @bean
    def cache_backend(self) -> CacheAdapter:
        # For production: use CacheManager with Redis + fallback
        # For local development: just use InMemoryCache()
        return InMemoryCache()

    @bean
    def cache_with_failover(self) -> CacheManager:
        import redis.asyncio as redis

        primary = RedisCacheAdapter(redis.from_url("redis://localhost:6379/0"))
        fallback = InMemoryCache()
        return CacheManager(primary=primary, fallback=fallback)


# ---------------------------------------------------------------------------
# Domain
# ---------------------------------------------------------------------------

@dataclass
class Product:
    product_id: str
    name: str
    price: float
    category: str


# ---------------------------------------------------------------------------
# Service with Declarative Caching
# ---------------------------------------------------------------------------

@service
class ProductService:
    """Product catalog with full caching support."""

    def __init__(self, cache_backend: CacheAdapter) -> None:
        self._cache = cache_backend
        self._db: dict[str, dict] = {}  # Simulated database

    @cache(backend=None, key="product:{product_id}", ttl=timedelta(minutes=15))
    async def get_product(self, product_id: str) -> dict | None:
        """Fetch a product by ID. Cached for 15 minutes.

        On cache hit, this method body does not execute.
        On cache miss, the product is fetched from the database and cached.
        """
        return self._db.get(product_id)

    @cache(backend=None, key="products:category:{category}", ttl=timedelta(minutes=5))
    async def list_by_category(self, category: str) -> list[dict]:
        """List products in a category. Cached for 5 minutes."""
        return [p for p in self._db.values() if p["category"] == category]

    @cache_put(backend=None, key="product:{product_id}", ttl=timedelta(minutes=15))
    async def create_product(self, product_id: str, name: str, price: float, category: str) -> dict:
        """Create a product and cache the result.

        Uses @cache_put because we always want to execute the creation
        and then store the result in cache.
        """
        product = {
            "product_id": product_id,
            "name": name,
            "price": price,
            "category": category,
        }
        self._db[product_id] = product
        return product

    @cache_put(backend=None, key="product:{product_id}", ttl=timedelta(minutes=15))
    async def update_product(self, product_id: str, data: dict) -> dict:
        """Update a product. Always executes, then refreshes the cache."""
        existing = self._db.get(product_id)
        if existing is None:
            raise ValueError(f"Product {product_id} not found")
        existing.update(data)
        return existing

    @cache_evict(backend=None, key="product:{product_id}")
    async def delete_product(self, product_id: str) -> None:
        """Delete a product and evict its cache entry."""
        self._db.pop(product_id, None)

    @cache_evict(backend=None, all_entries=True)
    async def clear_catalog(self) -> None:
        """Remove all products and clear the cache (its own entries, never a shared store)."""
        self._db.clear()
```

> **Note:** In the example above, the `backend` parameter on decorators is shown
> as `None` for brevity. In practice, you would pass the actual `CacheAdapter`
> instance. When using PyFly's container, this wiring is handled automatically.

### Usage Flow

```python
product_service = ProductService(cache_backend=InMemoryCache())

# 1. Create a product (always executes, caches the result)
await product_service.create_product("p1", "Widget", 29.99, "gadgets")

# 2. Get the product (cache hit -- function body does not execute)
product = await product_service.get_product("p1")

# 3. Update the product (always executes, refreshes cache)
await product_service.update_product("p1", {"price": 24.99})

# 4. Get again (cache hit with updated value)
product = await product_service.get_product("p1")
assert product["price"] == 24.99

# 5. Delete (removes from DB and evicts from cache)
await product_service.delete_product("p1")

# 6. Get again (cache miss, DB returns None)
product = await product_service.get_product("p1")
assert product is None
```

---

## Testing with InMemoryCache

The `InMemoryCache` makes it easy to write fast, deterministic tests without
Redis.

### Basic Cache Operations

```python
import pytest
from datetime import timedelta
from pyfly.cache.adapters.memory import InMemoryCache


@pytest.fixture
def cache_backend() -> InMemoryCache:
    return InMemoryCache()


@pytest.mark.asyncio
async def test_put_and_get(cache_backend: InMemoryCache) -> None:
    await cache_backend.put("key", {"data": "value"})
    result = await cache_backend.get("key")
    assert result == {"data": "value"}


@pytest.mark.asyncio
async def test_get_missing_key(cache_backend: InMemoryCache) -> None:
    result = await cache_backend.get("nonexistent")
    assert result is None


@pytest.mark.asyncio
async def test_evict(cache_backend: InMemoryCache) -> None:
    await cache_backend.put("key", "value")

    removed = await cache_backend.evict("key")
    assert removed is True

    removed_again = await cache_backend.evict("key")
    assert removed_again is False


@pytest.mark.asyncio
async def test_exists(cache_backend: InMemoryCache) -> None:
    assert await cache_backend.exists("key") is False

    await cache_backend.put("key", "value")
    assert await cache_backend.exists("key") is True


@pytest.mark.asyncio
async def test_clear(cache_backend: InMemoryCache) -> None:
    await cache_backend.put("a", 1)
    await cache_backend.put("b", 2)

    await cache_backend.clear()

    assert await cache_backend.get("a") is None
    assert await cache_backend.get("b") is None
```

### Testing Decorators

```python
from pyfly.cache import cache, cache_evict, cache_put


@pytest.mark.asyncio
async def test_cache_decorator_skips_on_hit(cache_backend: InMemoryCache) -> None:
    call_count = 0

    @cache(backend=cache_backend, key="item:{item_id}")
    async def get_item(item_id: str) -> dict:
        nonlocal call_count
        call_count += 1
        return {"id": item_id, "name": f"Item {item_id}"}

    # First call: cache miss
    result1 = await get_item("1")
    assert call_count == 1
    assert result1 == {"id": "1", "name": "Item 1"}

    # Second call: cache hit -- function not called
    result2 = await get_item("1")
    assert call_count == 1  # Still 1
    assert result2 == result1


@pytest.mark.asyncio
async def test_cache_put_always_executes(cache_backend: InMemoryCache) -> None:
    call_count = 0

    @cache_put(backend=cache_backend, key="item:{item_id}")
    async def update_item(item_id: str, name: str) -> dict:
        nonlocal call_count
        call_count += 1
        return {"id": item_id, "name": name}

    await update_item("1", "First")
    await update_item("1", "Updated")
    assert call_count == 2  # Called both times

    cached = await cache_backend.get("item:1")
    assert cached == {"id": "1", "name": "Updated"}


@pytest.mark.asyncio
async def test_cache_evict_removes_entry(cache_backend: InMemoryCache) -> None:
    await cache_backend.put("item:1", {"id": "1"})

    @cache_evict(backend=cache_backend, key="item:{item_id}")
    async def remove_item(item_id: str) -> None:
        pass

    await remove_item("1")
    assert await cache_backend.get("item:1") is None


@pytest.mark.asyncio
async def test_cache_evict_all_entries(cache_backend: InMemoryCache) -> None:
    await cache_backend.put("a", 1)
    await cache_backend.put("b", 2)

    @cache_evict(backend=cache_backend, all_entries=True)
    async def purge() -> None:
        pass

    await purge()
    assert await cache_backend.get("a") is None
    assert await cache_backend.get("b") is None
```

---

## Adapters

- [Redis Adapter](../adapters/redis.md) — Setup, configuration reference, and adapter-specific features for the Redis cache backend
