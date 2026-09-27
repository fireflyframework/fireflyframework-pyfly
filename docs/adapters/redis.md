# Redis Adapter

> **Module:** Caching — [Module Guide](../modules/caching.md)
> **Package:** `pyfly.cache.adapters.redis`
> **Backend:** redis 7.4+ (with hiredis C parser)

## Quick Start

### Installation

```bash
uv add "pyfly[cache]"

# Or just the Redis client
uv add "pyfly[redis]"
```

### Minimal Configuration

```yaml
# pyfly.yaml
pyfly:
  cache:
    enabled: true
    provider: "redis"
    redis:
      url: "redis://localhost:6379/0"
```

### Minimal Example

```python
import redis.asyncio as redis

from pyfly.cache import cacheable, cache_evict
from pyfly.cache.adapters.redis import RedisCacheAdapter

cache = RedisCacheAdapter(redis.from_url("redis://localhost:6379/0"))

@cacheable(backend=cache, key="order:{id}")
async def find_by_id(self, id: int) -> OrderDto | None:
    # Cache a DTO: an ORM entity return type is refused when the method is decorated.
    order = await self._repo.find_by_id(id)
    return OrderDto.model_validate(order, from_attributes=True) if order else None

@cache_evict(backend=cache, key="order:{id}")
async def delete_order(self, id: int) -> None:
    await self._repo.delete(id)   # evicted after the commit
```

A hit comes back as the declared `OrderDto`, not as the JSON `dict` Redis stores.

---

## Configuration Reference

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `pyfly.cache.enabled` | `bool` | `false` | Enable caching |
| `pyfly.cache.provider` | `str` | `"memory"` | Adapter selection (`auto`, `redis`, `memory`) |
| `pyfly.cache.redis.url` | `str` | `"redis://localhost:6379/0"` | Redis connection URL |
| `pyfly.cache.ttl` | `int` | `300` | Default TTL in seconds |

When `provider` is `"auto"`, PyFly uses Redis if the `redis` library is installed, otherwise falls back to `InMemoryCache`.

---

## Adapter-Specific Features

### RedisCacheAdapter

Implements `CacheAdapter` using `redis.asyncio.Redis`.

- **Serialization:** Values are JSON-serialized before storage and deserialized on retrieval; a live ORM
  object is refused with `CacheValueError`
- **Namespace:** Entries live under `pyfly:cache:` (the `namespace` argument), so the cache can share a
  database with sessions, locks and other data
- **TTL:** Supports per-key TTL via `timedelta` or the global default
- **Connection validation:** Calls `ping()` on `start()` to verify connectivity

### Operations

| Method | Description |
|--------|-------------|
| `get(key)` | Retrieve and deserialize a cached value |
| `put(key, value, ttl)` | Serialize and store a value with optional TTL |
| `evict(key)` | Remove a key from the cache |
| `exists(key)` | Check if a key exists |
| `clear()` | Delete the cache's own keys (its namespace, through `SCAN`); never `FLUSHDB` |
| `with_namespace(name)` | A cache dedicated to `name` (`pyfly:cache.<name>:`) that `clear()` never touches |

### In-Memory Fallback

When Redis is not available, `InMemoryCache` (`pyfly.cache.adapters.memory`) provides the same `CacheAdapter` interface using an in-process dict with TTL support. This is auto-configured when the `redis` library is not installed.

---

## Testing

Use the in-memory adapter for tests — no Redis server needed:

```yaml
# pyfly-test.yaml
pyfly:
  cache:
    provider: "memory"
```

---

## See Also

- [Caching Module Guide](../modules/caching.md) — Full API reference: `@cacheable`, `@cache_evict`, `@cache_put`, cache management
- [Adapter Catalog](README.md)
