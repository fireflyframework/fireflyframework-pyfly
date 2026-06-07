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
5. [CacheManager: Failover and Resilience](#cachemanager-failover-and-resilience)
6. [Declarative Caching Decorators](#declarative-caching-decorators)
   - [@cache](#cache)
   - [@cacheable](#cacheable)
     - [Conditional Caching: condition and unless](#conditional-caching-condition-and-unless)
   - [@cache_put](#cache_put)
   - [@cache_evict](#cache_evict)
7. [Key Templates](#key-templates)
8. [Auto-Configuration](#auto-configuration)
9. [Configuration Reference](#configuration-reference)
10. [Complete Example: Product Catalog Service](#complete-example-product-catalog-service)
11. [Testing with InMemoryCache](#testing-with-inmemorycache)

---

## Architecture Overview

```
Application Code (decorators / direct calls)
          |
          v
    CacheAdapter  (protocol / port)
          |
          +-- InMemoryCache       (dev / test, single-process)
          +-- RedisCacheAdapter   (production, via redis.asyncio)
          |
          v
    CacheManager  (optional: primary + fallback with auto-failover)
```

Your application depends only on the `CacheAdapter` protocol. You can swap
backends (in-memory to Redis) without changing a single line of business logic.
The `CacheManager` adds a resilience layer by mirroring writes and falling back
on read failures.

---

## CacheAdapter Protocol

The `CacheAdapter` is a `@runtime_checkable` `Protocol` that all cache
backends must implement:

```python
from pyfly.cache import CacheAdapter

class CacheAdapter(Protocol):
    async def get(self, key: str) -> Any | None: ...
    async def put(self, key: str, value: Any, ttl: timedelta | None = None) -> None: ...
    async def evict(self, key: str) -> bool: ...
    async def exists(self, key: str) -> bool: ...
    async def clear(self) -> None: ...
    async def start(self) -> None: ...
    async def stop(self) -> None: ...
```

### Method Reference

| Method                           | Return Type   | Description |
|----------------------------------|---------------|-------------|
| `get(key)`                       | `Any \| None` | Retrieve a cached value by key. Returns `None` if the key does not exist or has expired. |
| `put(key, value, ttl=None)`      | `None`        | Store a value under the given key. If `ttl` is provided, the entry expires after the specified duration. |
| `evict(key)`                     | `bool`        | Remove a specific key. Returns `True` if the key existed, `False` otherwise. |
| `exists(key)`                    | `bool`        | Check whether a key exists and has not expired. |
| `clear()`                        | `None`        | Remove all entries from the cache. |
| `start()`                        | `None`        | Initialize the cache backend (called during application startup). |
| `stop()`                         | `None`        | Shut down the cache backend (called during application shutdown). |

---

## InMemoryCache

The `InMemoryCache` is a simple dictionary-backed cache with optional TTL
support. It is suitable for **development, testing, and single-process
applications**.

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

Internally, `InMemoryCache` stores each entry as a `(value, expires_at)` tuple.
The `expires_at` is computed using `time.monotonic()` plus the TTL in seconds.

* On `get()`, if the current monotonic time exceeds `expires_at`, the entry is
  lazily deleted and `None` is returned.
* On `exists()`, the same expiration check is performed.
* If `ttl` is `None`, the entry never expires.

This is a **lazy expiration** strategy -- expired entries are not removed until
they are accessed. This keeps the implementation simple and fast, at the cost
of entries consuming memory until their next read.

---

## RedisCacheAdapter

The `RedisCacheAdapter` is the production cache backend. It delegates to a
`redis.asyncio.Redis` client and transparently handles JSON serialization.

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

# Clear the entire Redis database
await cache.clear()

await cache.start()   # Validate Redis connectivity
# ... use cache ...
await cache.stop()    # Close Redis connection
```

### Constructor

| Parameter | Type                     | Description |
|-----------|--------------------------|-------------|
| `client`  | `redis.asyncio.Redis`    | An async Redis client instance. |

### Serialization

Values are serialized to JSON with `json.dumps()` before storage and
deserialized with `json.loads()` on retrieval. This means any JSON-compatible
Python object (dicts, lists, strings, numbers, booleans, `None`) can be cached
transparently.

### TTL Handling

When `ttl` is provided, the adapter passes `ex=int(ttl.total_seconds())` to
the Redis `SET` command. Redis handles expiration natively, so expired keys are
removed server-side without any lazy-deletion overhead.

### Additional Methods

| Method    | Description |
|-----------|-------------|
| `start()` | Validates connectivity by pinging Redis (`await client.ping()`). Called automatically during application startup. |
| `stop()` | Closes the underlying Redis connection (`await client.aclose()`). Called automatically during application shutdown. |

> **Note:** When using auto-configuration, `start()` and `stop()` are called automatically
> by the `ApplicationContext` during startup and shutdown. You only need to call them
> manually if you create a cache adapter outside the DI container.

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

| Parameter  | Type           | Description |
|------------|----------------|-------------|
| `primary`  | `CacheAdapter` | The primary cache backend (typically Redis). |
| `fallback` | `CacheAdapter` | The fallback cache backend (typically in-memory). |

### Behavior

| Operation | Behavior |
|-----------|----------|
| `get(key)` | Try the primary. If the primary returns a value, return it. If the primary raises an exception, log a warning and try the fallback. If the primary returns `None`, also check the fallback. |
| `put(key, value, ttl)` | Write to the primary (catching exceptions). **Always** write to the fallback as well, keeping it warm. |
| `evict(key)` | Evict from both primary and fallback. Returns `True` if either had the key. |
| `clear()` | Clear both primary and fallback. |

This design means that:

* If Redis goes down, reads seamlessly degrade to the in-memory fallback.
* The fallback is always warm because every write is mirrored.
* When Redis comes back up, new writes immediately go to both caches.

### Logging

Failover events are logged at `WARNING` level via the `pyfly.cache` logger:

```
WARNING  Primary cache failed for GET 'user:123', falling back
WARNING  Primary cache failed for PUT 'user:123', using fallback only
```

---

## Declarative Caching Decorators

PyFly provides four decorators for declarative caching. They handle cache key
resolution, lookup, and storage automatically based on function arguments.

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

#### @cache vs. @cache_put

| Aspect              | `@cache` / `@cacheable` | `@cache_put` |
|---------------------|-------------------------|--------------|
| Cache hit behavior  | Returns cached value; function **not** called. | Function **always** called; result replaces cache entry. |
| Cache miss behavior | Calls function; caches result. | Calls function; caches result. |
| Best for            | Read operations (lookups). | Write/update operations. |

### @cache_evict

Removes a cache entry (or clears the entire cache) **after** the decorated
function executes.

```python
from pyfly.cache import cache_evict

# Evict a specific key
@cache_evict(backend=backend, key="user:{user_id}")
async def delete_user(user_id: str) -> None:
    await database.delete_user(user_id)
    # After this returns, cache entry "user:{user_id}" is evicted

# Clear all entries
@cache_evict(backend=backend, all_entries=True)
async def purge_all_users() -> None:
    await database.delete_all_users()
    # After this returns, the entire cache is cleared
```

#### Parameters

| Parameter     | Type           | Default    | Description |
|---------------|----------------|------------|-------------|
| `backend`     | `CacheAdapter` | *required* | The cache backend to use. |
| `key`         | `str`          | `""`       | Key template with `{param}` placeholders. Ignored when `all_entries=True`. |
| `all_entries` | `bool`         | `False`    | When `True`, calls `backend.clear()` instead of evicting a single key. |

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

When using automatic configuration, PyFly detects the available cache library
and selects the appropriate adapter:

| Detection Order | Library Checked    | Adapter Selected      |
|-----------------|--------------------|-----------------------|
| 1               | `redis.asyncio`    | `RedisCacheAdapter`   |
| 2               | *(fallback)*       | `InMemoryCache`       |

When Redis is detected, the `CacheManager` can be configured with
`RedisCacheAdapter` as the primary and `InMemoryCache` as the fallback for
automatic failover.

---

## Configuration Reference

Configure caching in your `pyfly.yaml`:

```yaml
pyfly:
  cache:
    enabled: false
    provider: memory      # "redis" or "memory"
    ttl: 300              # Default TTL in seconds (5 minutes)

    redis:
      url: redis://localhost:6379/0
```

| Property                 | Default                      | Description |
|--------------------------|------------------------------|-------------|
| `pyfly.cache.enabled`   | `false`                      | Enable or disable caching globally. |
| `pyfly.cache.provider`  | `"memory"`                   | Cache provider: `"redis"` or `"memory"`. |
| `pyfly.cache.ttl`       | `300`                        | Default TTL in seconds, applied when decorators do not specify their own TTL. |
| `pyfly.cache.redis.url` | `"redis://localhost:6379/0"` | Redis connection URL (only used when provider is `"redis"` or auto-detected). |

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
        """Remove all products and clear the entire cache."""
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
