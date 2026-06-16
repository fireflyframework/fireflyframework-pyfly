<span class="eyebrow">Chapter 13</span>

# Caching & Resilience {.chtitle}

::: figure art/openers/ch13.svg | &nbsp;

In Chapter 11 you split Lumen into separate services and taught its wallet
handler to call a downstream `AccountService` over HTTP. In Chapter 12 you
added a `DepositSaga` to coordinate multi-step operations across service
boundaries, with compensating transactions ready to fire when any step goes
wrong.

Those two chapters introduced a new class of problem: latency and failure
propagation. Every HTTP hop to `AccountService` is a round trip that could
be slow on a busy network, and every call to Lumen's own database competes
with concurrent saga participants. In a distributed system, failures are not
exceptional events — they are scheduled maintenance. `AccountService` will
be upgraded mid-traffic. Redis will hiccup. A payment gateway will spike to
three-second response times during peak settlement.

Without protection, Lumen propagates those failures upstream. A slow
`AccountService` ties up coroutines, blocking wallet reads for unrelated
users. A brief Redis outage wipes cached balances and sends every request
straight to the database, multiplying load at exactly the wrong moment.

This chapter makes Lumen **fast** and **fault-tolerant**. The first half
covers PyFly's declarative caching layer — **`@cacheable`**,
**`@cache_put`**, and **`@cache_evict`** — and shows how to back them with
an in-process `InMemoryCache` for development and a shared
`RedisCacheAdapter` with automatic failover for production. The second half
layers in the resilience toolkit: a token-bucket **rate limiter** that caps
inbound traffic, a semaphore **bulkhead** that isolates concurrency, a
**time limiter** that cancels hanging coroutines, a **fallback** that
degrades gracefully, and **retry** and **circuit-breaker** patterns that
protect outbound calls. A closing section shows how to stack all of them
in the right order.

By the end of the chapter, every hot path in Lumen will be cached and every
outbound dependency wrapped in a resilience fence.

!!! note "What you will build, in plain terms"
    This chapter introduces a lot of vocabulary — *cache*, *token bucket*,
    *bulkhead*, *circuit breaker*. Do not let the jargon intimidate you. Every
    one of these is a small, self-contained tool that you bolt onto a function
    with a single decorator. We will introduce each tool one at a time, build
    it into Lumen step by step, run it, and watch what changes. By the end you
    will have a mental checklist: *is this read hot? cache it; is this call
    going over the network? fence it.* The version of PyFly used throughout is
    **v26.6.110** — every command and config key below matches that release.

---

## Caching the read path

### Why cache wallet reads?

!!! note "New term: cache"
    A *cache* is a small, fast holding area where you keep the answer to an
    expensive question so you can hand it back instantly the next time someone
    asks. The first time Lumen computes wallet `w-001`'s balance it stores the
    result in the cache; subsequent reads return that stored copy without
    re-running the database query. The trade-off — and there is always a
    trade-off — is that the stored copy can be slightly out of date. The rest
    of this section is about keeping that staleness inside acceptable bounds.

Lumen's most frequent operation is the balance query: "what is wallet
`w-001`'s current balance?" Under normal load that query hits the read
replica. Under heavy load it competes with deposit commands, saga
participants, and snapshot writes. A cached balance costs one Redis lookup
— one co-located network round trip — compared with a full SQL query that
the read replica must also parse, plan, and execute.

The economics are compelling, but caching introduces a correctness concern:
the cached balance may lag the committed balance by up to the TTL. For
Lumen, a five-second stale balance is an acceptable trade-off for normal
query traffic. When a deposit completes, the handler invalidates the cache
entry immediately, so the next balance read reflects the change. Updates
that go through the saga use `@cache_put` to refresh the cached value as a
side-effect of the write, eliminating any visible staleness window.

::: figure art/figures/13-cache.svg | Figure 13.1 — Cache decorators sit in front of the service layer. On a hit the function body never executes; on a miss it runs and the result is stored.

### The cache abstraction

PyFly's cache layer follows the hexagonal principle you have seen throughout
the book: business logic depends on a **`CacheAdapter`** protocol, not on
any specific backend. Concrete implementations — `InMemoryCache` for
development and `RedisCacheAdapter` for production — are wired in through
the DI container. Swapping backends requires no changes to business logic.

The `CacheAdapter` protocol defines the full contract:

| Method | Returns | Description |
|---|---|---|
| `get(key)` | `Any \| None` | Return the cached value, or `None` if absent or expired. |
| `put(key, value, ttl=None)` | `None` | Store a value; `ttl` is a `timedelta` or `None` for no expiry. |
| `evict(key)` | `bool` | Remove one key; returns `True` if it existed. |
| `exists(key)` | `bool` | Check presence without fetching the value. |
| `clear()` | `None` | Flush the entire cache. |
| `start()` | `None` | Called once at application startup. |
| `stop()` | `None` | Called once at application shutdown. |

Both `InMemoryCache` and `RedisCacheAdapter` implement this contract.
`InMemoryCache` stores entries in an `OrderedDict` with lazy TTL expiry and
optional LRU bounding; it is ideal for single-process development and test
suites because it has no external dependencies. `RedisCacheAdapter` wraps a
`redis.asyncio.Redis` client, serialises values to JSON before storage, and
delegates TTL management to Redis itself — expired keys disappear server-side
with zero cleanup overhead on your side.

### Setting up a cache backend

We will wire up two backends: an in-process one for development and a shared
Redis-backed one for production. Take them one at a time.

**Step 1 — Pick a development backend.** For development, a single import is
all you need:

::: listing lumen/cache/config_dev.py | Listing 13.1 — InMemoryCache for development
from pyfly.cache.adapters.memory import InMemoryCache

wallet_cache = InMemoryCache(max_size=1000)
:::

`max_size=1000` bounds the LRU eviction window: once the cache holds 1,000
entries, the least-recently-used entry is dropped to make room. Pass `None`
(the default) to leave the cache unbounded and rely entirely on TTLs.

!!! note "New term: LRU and TTL"
    *LRU* stands for *least-recently-used* — when the cache is full, the entry
    nobody has touched for the longest is the one evicted to make room. *TTL*
    stands for *time-to-live* — how long an entry stays valid before it expires
    on its own. `InMemoryCache` supports both: `max_size` caps how many entries
    it holds (LRU); each `put` can carry a TTL that ages the entry out.

**Step 2 — Pick a production backend.** For production, point
`RedisCacheAdapter` at a `redis.asyncio.Redis` client, and wrap it with an
in-memory fallback so a Redis hiccup never takes Lumen down:

::: listing lumen/cache/config_prod.py | Listing 13.2 — RedisCacheAdapter for production
import redis.asyncio as aioredis

from pyfly.cache import CacheAdapter, CacheManager
from pyfly.cache.adapters.memory import InMemoryCache
from pyfly.cache.adapters.redis import RedisCacheAdapter
from pyfly.container import bean, configuration


@configuration
class CacheConfig:

    @bean
    def wallet_cache(self) -> CacheAdapter:
        client = aioredis.from_url("redis://localhost:6379/0")
        primary = RedisCacheAdapter(client)
        fallback = InMemoryCache(max_size=500)
        return CacheManager(primary=primary, fallback=fallback)
:::

**How it works:** `CacheManager` wraps a primary Redis backend and an
in-memory fallback. Every write goes to both caches, keeping the fallback
warm. On reads, the manager tries Redis first; if Redis raises an exception
it logs a `WARNING` and falls back to the in-process store silently. When
Redis recovers, new writes immediately repopulate it — no manual intervention
required. The `@bean` method tells PyFly's DI container to create a
singleton and inject it wherever `CacheAdapter` is declared as a
dependency.

**What just happened.** You now have one `CacheAdapter` interface and two
ways to satisfy it. In development you hand the DI container an
`InMemoryCache`; in production you hand it a `CacheManager` that fronts Redis
and quietly falls back to memory when Redis is unreachable. Every handler in
the rest of this chapter asks for `cache: CacheAdapter` in its constructor and
never knows or cares which one it got — that is the hexagonal payoff.

!!! tip "Auto-configuration"
    You do not have to write the `@configuration` class at all. Add the
    following to `pyfly.yaml` and PyFly's `CacheAutoConfiguration` builds a
    `CacheAdapter` bean for you at startup:

    ```yaml
    pyfly:
      cache:
        enabled: true        # required to switch the subsystem on
        provider: redis      # redis | postgres | memory | auto
        redis:
          url: redis://localhost:6379/0
        max-size: 1000       # used by the memory provider
    ```

    With `provider: redis` (or `auto`, which detects an installed
    `redis.asyncio`) the auto-config wires a `RedisCacheAdapter` pointed at
    `pyfly.cache.redis.url`. It registers that single adapter as the
    `CacheAdapter` bean — it does **not** add the in-memory failover layer.
    When you want Redis *plus* the transparent in-process fallback shown in
    Listing 13.2, declare the `CacheManager` yourself in a `@configuration`
    class as above. The auto-config also backs off entirely if you have
    already defined your own `CacheAdapter` bean (`@conditional_on_missing_bean`),
    so the two approaches never collide.

### @cacheable — skip execution on a hit

**`@cacheable`** is the most common decorator. On the first call it executes
the function body and stores the return value. On every subsequent call with
the same key it returns the stored value *without executing the function body
at all*.

Lumen's `GetBalanceHandler` is a natural fit: balance reads are frequent,
cheap to cache, and tolerate a few seconds of staleness. We will add caching
to it in three small moves.

**Step 1 — Accept the cache.** Add a `cache: CacheAdapter` parameter to the
constructor. PyFly's DI container sees the type and injects whichever backend
you wired up in the previous section.

**Step 2 — Move the real work into a private method.** Rename the body that
hits the database to `_fetch`. This is the function the cache will wrap.

**Step 3 — Wrap it at construction time.** Inside `__init__`, set
`self.do_handle = cacheable(...)(self._fetch)`. We wrap inside `__init__`
(rather than as a `@cacheable` decorator on the method) for one reason: the
`backend=cache` argument only exists once `cache` has been injected, and that
does not happen until `__init__` runs.

The handler receives `CacheAdapter` through its constructor — injected by
PyFly — and wraps `do_handle` at construction time:

::: listing lumen/core/services/wallets/get_balance_handler.py | Listing 13.3 — @cacheable on GetBalanceHandler
from datetime import timedelta

from lumen.core.mappers.wallet_mapper import entity_to_balance_dto
from lumen.core.services.wallets.get_balance_query import GetBalance
from lumen.interfaces.dtos.v1.balance_dto import BalanceDto
from lumen.models.repositories.wallet_repository import WalletRepository
from pyfly.cache import CacheAdapter, cacheable
from pyfly.container import service
from pyfly.cqrs import QueryHandler, query_handler


@query_handler
@service
class GetBalanceHandler(QueryHandler[GetBalance, BalanceDto | None]):
    """Return a cached :class:`BalanceDto`; bypass the DB on a hit."""

    def __init__(
        self,
        repository: WalletRepository,
        cache: CacheAdapter,
    ) -> None:
        super().__init__()
        self._repository = repository
        # Wrap do_handle at construction time so `cache` is in scope.
        self.do_handle = cacheable(
            backend=cache,
            key="wallet:balance:{query.wallet_id}",
            ttl=timedelta(seconds=5),
        )(self._fetch)

    async def _fetch(
        self, query: GetBalance
    ) -> BalanceDto | None:
        entity = await self._repository.find_by_id(query.wallet_id)
        return entity_to_balance_dto(entity) if entity is not None else None
:::

!!! note "Key template and `self`"
    The `key` template `"wallet:balance:{query.wallet_id}"` uses Python's
    `str.format` syntax. PyFly binds the actual call arguments with
    `inspect.signature(func).bind(*args, **kwargs)`, then calls
    `key.format(**bound.arguments)`. Because `_fetch` is wrapped inside
    `__init__`, the first positional argument is `query` — so
    `{query.wallet_id}` expands to the wallet id. Calling with
    `GetBalance(wallet_id="wlt-001")` produces the cache key
    `"wallet:balance:wlt-001"`. The mapper function
    `entity_to_balance_dto` goes through `Mapper.project` against the
    `@projection`-marked `BalanceView` interface, copying only the fields
    the balance view declares and computing `balance` from `balance_minor`.

**`ttl=timedelta(seconds=5)`** means the cache entry expires five seconds
after it is written. After expiry, the next call re-executes the function
body and refreshes the entry. A TTL of `None` (the default) means the entry
never expires — appropriate only for truly immutable data.

**Null caching:** When the function returns `None`, PyFly still stores the
entry and records that the key *exists*. A subsequent call finds the key and
returns `None` without touching the database. This prevents cache-penetration
attacks where an adversary floods requests for non-existent keys, each of
which would otherwise fall through to the database.

**`condition` and `unless`:** Both `@cache` and `@cacheable` accept optional
predicates. `condition` is a callable with the same signature as the
decorated function; if it returns `False`, caching is bypassed for that call.
`unless` is a callable that receives the *result*; if it returns `True`, the
result is returned but not stored. Both are keyword-only:

```python
cacheable(
    backend=cache,
    key="wallet:balance:{query.wallet_id}",
    ttl=timedelta(seconds=5),
    condition=lambda query: not query.wallet_id.startswith("test-"),
    unless=lambda result: result is None,
)(self._fetch)
```

#### Run it — prove the second read skips the database

The cleanest way to *see* a cache hit is a unit test that counts how many
times the repository is called. Use a real `InMemoryCache` (no Redis needed)
and a tiny stub repository:

::: listing tests/cache/test_get_balance_cache.py | Listing 13.3a — A test that proves the second read is a hit
from datetime import timedelta

import pytest

from pyfly.cache import cacheable
from pyfly.cache.adapters.memory import InMemoryCache


class _CountingRepo:
    """Stub repository that records how many times it is queried."""

    def __init__(self) -> None:
        self.calls = 0

    async def find_by_id(self, wallet_id: str) -> dict:
        self.calls += 1
        return {"wallet_id": wallet_id, "balance_minor": 500}


@pytest.mark.asyncio
async def test_second_read_is_a_cache_hit() -> None:
    repo = _CountingRepo()
    cache = InMemoryCache(max_size=10)

    fetch = cacheable(
        backend=cache,
        key="wallet:balance:{wallet_id}",
        ttl=timedelta(seconds=5),
    )(repo.find_by_id)

    first = await fetch("wlt-001")   # miss -> runs the repo
    second = await fetch("wlt-001")  # hit  -> repo NOT called again

    assert first == second
    assert repo.calls == 1           # the body ran exactly once
:::

Run just this test:

```console
$ uv run --extra dev pytest tests/cache/test_get_balance_cache.py -q
.                                                                        [100%]
1 passed in 0.04s
```

The single `.` and `1 passed` confirm it: the second call returned the cached
value and `repo.calls` stayed at `1`, so the database was touched exactly once
across two reads. That is a cache hit, demonstrated rather than asserted in
prose.

**What just happened.** You wrapped a plain async function with `cacheable`,
backed it with an `InMemoryCache`, and confirmed that identical keys
short-circuit the body. In `GetBalanceHandler` the wrapped function is
`_fetch` and the backend is the injected `CacheAdapter`, but the mechanics are
exactly what you just ran.

!!! spring "Spring parity"
    `@cacheable` mirrors Spring's `@Cacheable`. The `key` template uses Python's `str.format` syntax instead of SpEL, but the semantics — skip-on-hit, store-on-miss, `condition`, `unless` — are identical. `@cache` is a lower-level alias that behaves the same way; use whichever name reads better in your codebase.

### @cache_put — always execute, always store

`@cacheable` is for reads: it short-circuits the function when the cache
already holds a value. **`@cache_put`** is for writes: it *always* executes
the function and *always* stores the result. Use it when the function is the
source of truth — a command handler that modifies the wallet and must keep
the cache current.

`DepositFundsHandler` is the canonical example. After a deposit succeeds,
the new balance must be visible to the next read without waiting for the TTL
to expire. The wiring mirrors what you did for `@cacheable`, with one critical
detail to watch:

**Step 1 — Accept the cache** in the constructor, exactly as before.

**Step 2 — Move the deposit logic into `_deposit`** and keep its
`@transactional()` decorator so the write still commits as one unit of work.

**Step 3 — Wrap with `cache_put`, reusing the *same* key shape.** The deposit
handler must write to the very cache slot the balance reader looks up.
`@cacheable` uses `"wallet:balance:{query.wallet_id}"`; here the argument is
named `command`, so the template is `"wallet:balance:{command.wallet_id}"`.
Different parameter names, but both resolve to `wallet:balance:wlt-001`.

Wrapping `do_handle` with `@cache_put` refreshes the cache entry atomically
with the write:

::: listing lumen/core/services/wallets/deposit_funds_handler.py | Listing 13.4 — @cache_put refreshes the cache on a deposit
from datetime import timedelta

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from lumen.core.mappers.wallet_mapper import to_aggregate, to_entity
from lumen.core.services.wallets.deposit_funds_command import DepositFunds
from lumen.core.services.wallets.event_publishing import publish_domain_events
from lumen.models.entities.v1.money import Money
from lumen.models.repositories.wallet_repository import WalletRepository
from pyfly.cache import CacheAdapter, cache_put
from pyfly.container import service
from pyfly.cqrs import CommandHandler, command_handler
from pyfly.data.relational.sqlalchemy import transactional
from pyfly.domain import AggregateNotFound
from pyfly.eda import EventPublisher


@command_handler
@service
class DepositFundsHandler(CommandHandler[DepositFunds, int]):
    """Credit funds to an existing wallet; returns the new balance
    in minor units and refreshes the cached balance entry."""

    def __init__(
        self,
        repository: WalletRepository,
        events: EventPublisher,
        session_factory: async_sessionmaker[AsyncSession],
        cache: CacheAdapter,
    ) -> None:
        super().__init__()
        self._repository = repository
        self._events = events
        self._session_factory = session_factory
        # Wrap at construction time so `cache` is in scope.
        self.do_handle = cache_put(
            backend=cache,
            key="wallet:balance:{command.wallet_id}",
            ttl=timedelta(seconds=5),
        )(self._deposit)

    @transactional()
    async def _deposit(self, command: DepositFunds) -> int:
        entity = await self._repository.find_by_id(command.wallet_id)
        if entity is None:
            raise AggregateNotFound("Wallet", command.wallet_id)

        wallet = to_aggregate(entity)
        wallet.deposit(Money(amount=command.amount, currency=wallet.currency))
        await self._repository.upsert(to_entity(wallet))
        await publish_domain_events(self._events, wallet.clear_events())
        return wallet.balance.amount
:::

**How it works:** `@cache_put` awaits the wrapped function, then calls
`backend.put(resolved_key, result, ttl=ttl)`. Because the function always
runs, the cached value after a `DepositFunds` command is the freshly
committed balance — not a stale pre-deposit snapshot. `_deposit` runs inside
`@transactional()`, so the `find_by_id → to_aggregate → mutate → upsert`
sequence is committed as one unit of work before the cache is refreshed. The
next `@cacheable` read in `GetBalanceHandler` picks up this fresh value
without touching the database.

!!! note "Cache key must match"
    The `@cache_put` key `"wallet:balance:{command.wallet_id}"` must match the `@cacheable` key `"wallet:balance:{query.wallet_id}"` when both resolve to the same wallet id. Mismatched keys mean the deposit writes to a different cache slot than the balance read looks up — staleness returns.

| Decorator | Function executes? | On hit |
|---|---|---|
| `@cacheable` / `@cache` | Only on a miss | Returns cached value |
| `@cache_put` | Always | Replaces cached value with fresh result |

### @cache_evict — remove after deletion

When you close a wallet or roll back a transaction, the associated cache
entry must be removed. **`@cache_evict`** runs the function body first, then
removes the named key — or clears the entire cache when
`all_entries=True`.

::: listing lumen/core/services/wallets/close_wallet_handler.py | Listing 13.5 — @cache_evict after removing a wallet
from lumen.models.repositories.wallet_repository import WalletRepository
from pyfly.cache import CacheAdapter, cache_evict
from pyfly.container import service
from pyfly.cqrs import CommandHandler, command_handler
from pyfly.data.relational.sqlalchemy import transactional


@command_handler
@service
class CloseWalletHandler(CommandHandler["CloseWallet", None]):
    """Close a wallet and evict its cached balance entry."""

    def __init__(
        self,
        repository: WalletRepository,
        cache: CacheAdapter,
    ) -> None:
        super().__init__()
        self._repository = repository
        self.do_handle = cache_evict(
            backend=cache,
            key="wallet:balance:{command.wallet_id}",
        )(self._close)

    @transactional()
    async def _close(self, command) -> None:
        entity = await self._repository.find_by_id(command.wallet_id)
        if entity is not None:
            await self._repository.delete(entity)
:::

To flush every cached balance at once — useful for an administrative reset
— pass `all_entries=True`:

```python
self.do_handle = cache_evict(
    backend=cache,
    all_entries=True,
)(self._reset_all)
```

**How it works:** The function body runs first — `repository.delete(entity)`
removes the row before eviction, so a failure does not prematurely drop the
cache entry. Then either `backend.evict(resolved_key)` removes one key or
`backend.clear()` flushes everything. With `CacheManager`, the evict
propagates to both primary and fallback caches so no stale entry lingers in
either tier.

`all_entries=True` is a blunt instrument reserved for administrative resets.
In normal operation, prefer targeted eviction by key.

### Invalidation strategy

A coherent strategy matches each operation to the right decorator:

| Operation | Decorator | Rationale |
|---|---|---|
| `GetBalance` query | `@cacheable` | Skip DB on hit; 5 s TTL bounds staleness |
| `DepositFunds` command | `@cache_put` | Refresh the cache entry atomically with the write |
| `WithdrawFunds` command | `@cache_put` | Same — keep the post-withdrawal balance warm |
| Close wallet | `@cache_evict` | Remove the entry; the next read rebuilds it from DB |
| Admin truncate | `@cache_evict(all_entries=True)` | Bulk reset; full cache flush is correct |

!!! warning "Async requirement"
    All three decorators require the wrapped function to be declared `async`. Cache adapters are fully async (they `await` backend operations), so a synchronous target will fail with a `TypeError` at decoration time — PyFly raises the error immediately so you catch the mistake at startup rather than at runtime.

---

## Resilience patterns

### Why protection matters

!!! note "New term: resilience"
    *Resilience* here means the system keeps serving requests it *can* serve
    even when a dependency it relies on is slow, overloaded, or down. The tools
    in this section do not make the downstream faster — they stop one sick
    dependency from making the whole of Lumen sick. Each tool is, again, a
    decorator you stack on the function that makes the risky call.

Caching makes the happy path fast. Resilience patterns protect Lumen when
the happy path is unavailable. Without protection, a slow `AccountService`
triggers a cascade:

1. Requests from wallet handlers pile up, each waiting on an HTTP response.
2. Lumen's asyncio event loop — single-threaded by default — processes
   pending tasks in order; a backlog of slow HTTP calls delays every other
   operation.
3. Memory and open file-descriptors climb as coroutines stack up.
4. Lumen becomes unavailable to requests that have nothing to do with
   `AccountService`.

Four complementary patterns break this cascade before it starts:

::: figure art/figures/13-resilience.svg | Figure 13.2 — Four resilience layers guard the outbound call. Rate limiter drops excess traffic before it enters the system; bulkhead limits concurrency; timeout cancels slow operations; fallback provides a safe response when all else fails.

| Pattern | Protects against | Fail-fast or wait? |
|---|---|---|
| **Rate limiter** | Traffic spikes overwhelming the downstream | Fail-fast (reject excess) |
| **Bulkhead** | Too many concurrent calls tying up resources | Fail-fast (reject over limit) |
| **Time limiter** | Hanging calls that never return | Cancels after timeout |
| **Fallback** | Any failure reaching the caller | Returns degraded value |

All four are in `pyfly.resilience`:

```python
from pyfly.resilience import (
    RateLimiter, rate_limiter,
    Bulkhead, bulkhead,
    time_limiter,
    fallback,
)
```

### Rate limiter — token bucket

`RateLimiter` uses a **token bucket**: the bucket holds up to `max_tokens`
tokens and refills at `refill_rate` tokens per second. Each call consumes
one token. When the bucket is empty, `RateLimitException` is raised
immediately — no queuing, no waiting.

::: listing lumen/resilience/rate_example.py | Listing 13.6 — Token-bucket rate limiter on account lookups
from pyfly.resilience import RateLimiter, rate_limiter

# Sustained: 20 calls/s; burst: up to 40
account_limiter = RateLimiter(max_tokens=40, refill_rate=20.0)


@rate_limiter(account_limiter)
async def fetch_account(account_id: str) -> dict:
    # This body is reached only when a token is available.
    ...
:::

**How it works:** `@rate_limiter(limiter)` calls `await limiter.acquire()`
before every invocation. `acquire()` refills the bucket based on elapsed
wall-clock time (using `time.monotonic()`), then atomically checks and
decrements the token count under a `threading.Lock` — not an asyncio lock
— so that both async tasks and sync callers share the same count without
races. If fewer than 1.0 tokens remain, `RateLimitException` propagates to
the caller.

The token-bucket shape allows controlled bursting: a service that typically
sees 10 calls per second can absorb a burst of 40 calls immediately
(drawing on saved tokens), then sustains 20 calls per second afterwards.
Fixed-window rate limiters cannot express this nuance.

#### Run it — watch the bucket run dry

A small script makes the behaviour concrete. Create a limiter with a tiny
bucket, call past its capacity, and observe the rejection:

::: listing scratch/rate_demo.py | Listing 13.6a — Draining the token bucket on purpose
import asyncio

from pyfly.kernel.exceptions import RateLimitException
from pyfly.resilience import RateLimiter, rate_limiter

# 3 tokens, refilling slowly so the burst is what we observe.
limiter = RateLimiter(max_tokens=3, refill_rate=1.0)


@rate_limiter(limiter)
async def ping(n: int) -> str:
    return f"ok-{n}"


async def main() -> None:
    for n in range(5):
        try:
            print(await ping(n))
        except RateLimitException:
            print(f"rejected-{n}")


asyncio.run(main())
:::

Run it directly:

```console
$ uv run python scratch/rate_demo.py
ok-0
ok-1
ok-2
rejected-3
rejected-4
```

The first three calls each spend a token; the fourth and fifth arrive with an
empty bucket and are rejected immediately with `RateLimitException` — no
queuing, no waiting. Slow the loop down (or raise `refill_rate`) and the
rejections disappear because tokens refill between calls.

**What just happened.** You did not change the function `ping` at all — you
decorated it. The decorator inserted an `await limiter.acquire()` before every
call, and `acquire()` raised when the bucket was empty. This is the shape every
resilience tool in this chapter takes: a decorator that guards the call without
the function body knowing it exists.

Multiple functions sharing one `RateLimiter` instance enforce a *global*
rate across all of them — useful for capping total traffic to a downstream
service regardless of which internal method initiates the call.

### Bulkhead — concurrency isolation

!!! note "New term: bulkhead"
    The name comes from shipbuilding: a ship's hull is divided into sealed
    compartments (*bulkheads*) so that a breach in one does not flood the whole
    vessel. A software bulkhead caps how many calls to one dependency can run at
    once, so a flood of slow calls to `AccountService` cannot consume every
    coroutine and sink unrelated requests.

`Bulkhead` is a semaphore: it limits the number of calls *in-flight at the
same time*. Calls beyond `max_concurrent` are rejected immediately with
`BulkheadException`.

::: listing lumen/resilience/bulkhead_example.py | Listing 13.7 — Bulkhead limiting concurrent account service calls
from pyfly.resilience import Bulkhead, bulkhead

# At most 5 concurrent calls to AccountService
account_bulkhead = Bulkhead(max_concurrent=5)


@bulkhead(account_bulkhead)
async def fetch_account(account_id: str) -> dict:
    ...
:::

**How it works:** The decorator acquires a permit (`_acquire_slot`) before
entering the function and releases it (`_release_slot`) in a `finally`
block, so the slot is always returned even when the function raises. Slots
are tracked by a single lock-guarded integer counter shared by async and
sync call paths, so one `Bulkhead` instance safely decorates a mix of
coroutines and regular functions.

This fail-fast behaviour is intentional: when 5 concurrent calls are
in-flight and a 6th arrives, rejecting it immediately lets the caller retry
or invoke a fallback — far better than queuing it indefinitely and causing
cascading backpressure.

!!! tip "Monitoring bulkhead utilization"
    `account_bulkhead.available_slots` returns the number of free permits at any moment. Expose this in a health endpoint or feed it to your observability stack to detect persistent saturation before it becomes an outage.

### Time limiter — enforcing a deadline

A slow downstream is sometimes worse than a crashed one: indefinitely
blocking calls consume resources without bound. **`@time_limiter`** cancels
the coroutine if it does not complete within a `timedelta`:

::: listing lumen/resilience/timeout_example.py | Listing 13.8 — 2-second deadline on account lookup
from datetime import timedelta

from pyfly.resilience import time_limiter


@time_limiter(timeout=timedelta(seconds=2))
async def fetch_account(account_id: str) -> dict:
    ...
:::

**How it works:** Internally, `time_limiter` calls
`asyncio.wait_for(func(*args, **kwargs), timeout=timeout_seconds)`. When
the deadline passes, `asyncio.wait_for` cancels the underlying task,
causing any `await` inside the function to raise `asyncio.CancelledError`.
The decorator catches `TimeoutError` and re-raises it as
`OperationTimeoutException` with a descriptive message:

```
OperationTimeoutException: fetch_account exceeded timeout of 2.0s
```

Resources acquired inside the timed function should be guarded with
`try/finally` so they are released even on cancellation:

```python
@time_limiter(timeout=timedelta(seconds=2))
async def fetch_account(account_id: str) -> dict:
    conn = await pool.acquire()
    try:
        return await conn.execute(query, account_id)
    finally:
        await pool.release(conn)
```

### Fallback — graceful degradation

**`@fallback`** is the safety net at the outermost layer: it catches
exceptions and returns an alternative response rather than propagating the
error to the caller. Lumen's balance summary endpoint can return a degraded
response — last known balance, marked as potentially stale — rather than an
HTTP 500 when `AccountService` is down.

Two modes are available. The first returns a **static value**:

::: listing lumen/resilience/fallback_static.py | Listing 13.9 — Static fallback value
from pyfly.resilience import fallback


@fallback(fallback_value={"balance_minor": 0, "source": "fallback"})
async def fetch_account(account_id: str) -> dict:
    ...
:::

The second invokes a **fallback method** that receives the original arguments plus the exception:

::: listing lumen/resilience/fallback_method.py | Listing 13.10 — Fallback method with cached data
from pyfly.cache import CacheAdapter
from pyfly.resilience import fallback


_cache: CacheAdapter  # injected elsewhere


async def account_from_cache(
    account_id: str,
    exc: Exception = None,
) -> dict:
    cached = await _cache.get(f"account:{account_id}")
    if cached:
        return {**cached, "source": "cache"}
    return {"account_id": account_id, "balance_minor": 0, "source": "fallback"}


@fallback(fallback_method=account_from_cache)
async def fetch_account(account_id: str) -> dict:
    ...
:::

**How it works:** When the primary function raises one of the exception types
listed in `on` (default: all `Exception` subclasses), the decorator calls
`fallback_method(*args, exc=exc, **kwargs)`. The `exc` keyword argument
carries the caught exception so the fallback can log it, inspect its type,
or return different values for different failure modes. If the fallback
method returns a coroutine, PyFly awaits it automatically. Narrow the
exception filter with `on=(OperationTimeoutException, CircuitBreakerException)`
to let programming errors propagate normally.

!!! warning "Fallback method signature"
    The fallback method must accept `exc` as a keyword argument. PyFly passes the caught exception as `exc=<exception>`. If your fallback method's signature does not include `exc`, you will see a `TypeError` with a clear message at the first failure — not at decoration time.

---

## Retry and circuit breaker

### @retry — bounded re-attempts with backoff

Network errors are often transient: a packet is lost, a connection pool is
momentarily exhausted, a downstream pod restarts. **`@retry`** re-invokes
the decorated function up to `max_attempts` times with exponential backoff
between attempts.

`max_attempts` is the only positional argument; every other parameter is
keyword-only:

::: listing lumen/resilience/retry_example.py | Listing 13.11 — Retry with exponential backoff
from pyfly.resilience import retry


@retry(
    max_attempts=3,
    delay=0.1,
    backoff=2.0,
    max_delay=2.0,
    exceptions=(IOError, TimeoutError),
)
async def fetch_account(account_id: str) -> dict:
    ...
:::

**How it works:** The decorator executes the function, catches exceptions
matching `exceptions`, sleeps `delay * backoff ** attempt` seconds (capped
at `max_delay`), and tries again. On the final attempt it re-raises the last
exception. The sleep uses `await asyncio.sleep(...)` for async functions and
`time.sleep(...)` for sync functions — the same implementation handles both.
The `jitter` parameter adds randomisation to avoid thundering-herd retries
when many instances restart simultaneously.

| Parameter | Default | Description |
|---|---|---|
| `max_attempts` | `3` | Total attempts including the first (≥ 1). Positional. |
| `delay` | `0.0` | Base sleep in seconds before the first retry. Keyword-only. |
| `backoff` | `1.0` | Multiplier applied to `delay` each attempt. Keyword-only. |
| `max_delay` | `None` | Cap on per-attempt sleep. `None` means no cap. Keyword-only. |
| `jitter` | `0.0` | Randomisation fraction `[0, 1]` applied to each wait. Keyword-only. |
| `exceptions` | `(Exception,)` | Exception types that trigger a retry; others propagate immediately. Keyword-only. |

!!! warning "Idempotency is your responsibility"
    `@retry` will call the function body multiple times. If the operation is not idempotent — if calling it twice has a different effect than calling it once — you can apply changes more than once. Wallet deposits are not safe to retry naively: retrying a failed deposit could credit the same amount twice. Wrap non-idempotent operations in an idempotency key check (store the operation ID before executing; skip if the ID already exists) or limit `exceptions` to errors that are definitely pre-execution (connection errors, timeouts during the request phase) rather than post-execution ambiguity.

### @circuit_breaker — fast failure under sustained outage

!!! note "New term: circuit breaker"
    Borrowed from electrical wiring: a circuit breaker *trips* (opens) when too
    much current flows, cutting the circuit before the wiring overheats. A
    software circuit breaker trips after too many failures, cutting off calls to
    a failing dependency so you stop hammering it — and so your own callers fail
    fast instead of waiting on calls that are doomed to error anyway.

Retrying a genuinely unavailable service amplifies load at exactly the
moment that service most needs relief. The circuit-breaker pattern solves
this: after a threshold of consecutive failures the circuit **opens** and
subsequent calls are rejected immediately — without attempting the remote
call — until a recovery timeout elapses.

PyFly's circuit breaker has three states:

| State | Behaviour |
|---|---|
| **CLOSED** | Normal operation. Every call goes through; failures are counted. |
| **OPEN** | All calls raise `CircuitBreakerException` immediately, without network I/O. |
| **HALF_OPEN** | After `recovery_timeout` seconds, a limited probe call is admitted. If it succeeds the circuit closes; if it fails the circuit reopens. |

`@circuit_breaker` takes a `CircuitBreaker` **instance** — not keyword
arguments. Construct the `CircuitBreaker` separately and pass it in:

::: listing lumen/resilience/cb_example.py | Listing 13.12 — Circuit breaker around AccountService
from pyfly.resilience import CircuitBreaker, circuit_breaker

account_cb = CircuitBreaker(
    failure_threshold=5,
    recovery_timeout=30.0,
    expected=(IOError, TimeoutError),
)


@circuit_breaker(account_cb)
async def fetch_account(account_id: str) -> dict:
    ...
:::

**How it works:** Before each call, `breaker.before_call()` checks the
current state. If OPEN, it raises `CircuitBreakerException` immediately.
If HALF_OPEN and the probe budget is exhausted, it also raises. Otherwise
the call proceeds. On success, `breaker.on_success()` resets the
consecutive-failure counter (or, in HALF_OPEN, closes the circuit once
enough probes succeed). On failure, `breaker.on_failure()` increments the
counter and opens the circuit when `failure_threshold` is reached.

Only exceptions in `expected` trip the breaker. Business exceptions —
`ValueError`, `PermissionError` — propagate normally without affecting
the circuit state.

**`CircuitBreaker` constructor parameters** (`failure_rate_threshold`,
`window_size`, and `half_open_max_calls` are keyword-only):

| Parameter | Default | Description |
|---|---|---|
| `failure_threshold` | `5` | Consecutive failures that trip the circuit. |
| `recovery_timeout` | `30.0` | Seconds in OPEN before moving to HALF_OPEN. |
| `expected` | `(Exception,)` | Exception types that count as failures. |
| `failure_rate_threshold` | `None` | Switch to windowed-rate mode when set (e.g. `0.5`). |
| `window_size` | `10` | Outcome window size for rate-based tripping. |
| `half_open_max_calls` | `1` | Probe calls required to close from HALF_OPEN. |

The `failure_rate_threshold` and `window_size` parameters switch from
consecutive-count mode to windowed-rate mode, matching Resilience4j's
COUNT_BASED sliding window. Set `failure_rate_threshold=0.5` and
`window_size=10` to open the circuit when more than half of the last 10
calls fail.

!!! spring "Spring parity"
    `@retry` mirrors Spring Retry's `@Retryable` (with `maxAttempts`, `backoff`, `include`). `CircuitBreaker` mirrors Resilience4j's `CircuitBreaker` (failure threshold, recovery timeout, CLOSED/OPEN/HALF_OPEN state machine, half-open probe calls, expected-exception filter). PyFly does not use the Resilience4j Java library — it is a pure-Python re-implementation with the same semantics.

### Configuring resilience from `pyfly.yaml`

So far you have constructed every `RateLimiter`, `Bulkhead`, and
`CircuitBreaker` in code. That is perfect for a single gateway, but operations
teams usually want to tune these thresholds *without a code change* — bump a
timeout, widen a rate limit — and they want one obvious place to read the
current settings. PyFly v26.6.110 ships a config-driven **`ResilienceRegistry`**
for exactly this, giving parity with Resilience4j's named-registry model.

**Step 1 — Declare named instances in `pyfly.yaml`.** Each entry under
`pyfly.resilience.*` becomes a named instance. Names are yours to choose;
group them by the downstream they protect:

```yaml
pyfly:
  resilience:
    circuit-breaker:
      account-api:
        failure-threshold: 5
        recovery-timeout: 30s
        # or switch to windowed-rate mode:
        # failure-rate-threshold: 0.5
        # window-size: 10
    rate-limiter:
      account-api:
        max-tokens: 50
        refill-rate: 20.0
    bulkhead:
      account-api:
        max-concurrent: 8
    time-limiter:
      account-api:
        timeout: 2s
```

Durations accept friendly suffixes — `30s`, `500ms`, `1m`, `2h` — or a bare
number read as seconds. Keys use kebab-case (`failure-threshold`); PyFly's
relaxed binding accepts snake_case too.

**Step 2 — Inject the registry and look instances up by name.** PyFly's
`ResilienceAutoConfiguration` registers a single `ResilienceRegistry` bean
built from those keys (it is always on, and returns an empty registry when no
keys are present). Ask for it in any `@service` constructor:

::: listing lumen/account/gateway_configured.py | Listing 13.12a — Pulling resilience instances from the registry
from pyfly.container import service
from pyfly.resilience import (
    ResilienceRegistry,
    bulkhead,
    circuit_breaker,
    rate_limiter,
)


@service
class AccountGateway:

    def __init__(self, http_client, registry: ResilienceRegistry) -> None:
        self._http = http_client
        # Look up the named instances declared in pyfly.yaml.
        cb = registry.circuit_breaker("account-api")
        rl = registry.rate_limiter("account-api")
        bh = registry.bulkhead("account-api")

        # Wrap the real call with the config-driven instances.
        guarded = circuit_breaker(cb)(self._raw_get)
        guarded = bulkhead(bh)(guarded)
        self.get_account = rate_limiter(rl)(guarded)

    async def _raw_get(self, account_id: str) -> dict:
        resp = await self._http.get(f"/accounts/{account_id}")
        return resp.json()
:::

**What just happened.** The thresholds now live in configuration, not in
Python literals. A `CircuitBreaker` named `account-api` is materialised once at
startup and shared by everything that looks it up — so the failure counts and
OPEN/CLOSED state are *global* across all callers of that name, exactly like a
shared in-code instance. Looking up an unknown name raises `KeyError` with the
list of available names, so a typo fails loudly at startup rather than silently
creating an unprotected path.

!!! tip "Time limiter returns a timedelta"
    `registry.time_limiter("account-api")` returns the configured **`timedelta`**, not a decorator — feed it straight into `time_limiter(timeout=registry.time_limiter("account-api"))`. The other three accessors (`circuit_breaker`, `rate_limiter`, `bulkhead`) return the instance you pass to the matching decorator.

!!! spring "Spring parity"
    The `ResilienceRegistry` mirrors Resilience4j's `CircuitBreakerRegistry`, `RateLimiterRegistry`, and `BulkheadRegistry` — named instances declared in configuration and looked up at runtime. Spring Boot's `resilience4j.circuitbreaker.instances.<name>.*` becomes `pyfly.resilience.circuit-breaker.<name>.*`; the property names line up one-for-one.

---

## Composing the layers

### Decorator order

PyFly's resilience decorators compose by stacking. Python applies decorators
bottom-up at decoration time but executes them top-down at call time. The
recommended order, outermost to innermost:

```
@fallback           ← 1. Catch any exception; return degraded response
@rate_limiter       ← 2. Reject excess traffic before it acquires resources
@bulkhead           ← 3. Limit concurrency of rate-limited calls
@time_limiter       ← 4. Cancel if execution takes too long
async def func()    ← 5. The actual operation
```

This ordering ensures:

1. **Fallback** catches exceptions from every inner layer — including
   `RateLimitException`, `BulkheadException`, and
   `OperationTimeoutException` — so the caller always receives a usable
   response.
2. **Rate limiter** drops excess requests before they consume a bulkhead
   slot, preventing a traffic flood from exhausting the concurrency budget.
3. **Bulkhead** limits how many rate-permitted calls run concurrently,
   protecting the downstream from overload.
4. **Time limiter** applies only to actual execution; when it fires, the
   bulkhead `finally` block releases the slot correctly.

Add `@retry` and `@circuit_breaker` on the innermost side — wrapping only
the actual I/O call — so the fallback absorbs their exceptions and the rate
limiter and bulkhead account for retried calls correctly:

```
@fallback
@rate_limiter
@bulkhead
@time_limiter
@circuit_breaker(account_cb)
@retry(max_attempts=2, delay=0.05, backoff=2.0, exceptions=(IOError,))
async def fetch_account(account_id: str) -> dict: ...
```

With `@retry` below `@time_limiter`, the timeout budget covers the entire
retry sequence, not each individual attempt. To bound each attempt
independently, move `@time_limiter` below `@retry`.

### Putting it all together — Lumen's account gateway

Here is the full pattern assembled into a realistic `AccountGateway` that
Lumen's wallet handlers use to look up account information:

::: listing lumen/account/gateway.py | Listing 13.13 — AccountGateway with full resilience stack
from datetime import timedelta

from pyfly.cache import CacheAdapter, cacheable
from pyfly.container import service
from pyfly.kernel.exceptions import CircuitBreakerException, OperationTimeoutException
from pyfly.resilience import (
    Bulkhead,
    CircuitBreaker,
    RateLimiter,
    bulkhead,
    circuit_breaker,
    fallback,
    rate_limiter,
    retry,
    time_limiter,
)

_limiter = RateLimiter(max_tokens=50, refill_rate=20.0)
_bh = Bulkhead(max_concurrent=8)
_cb = CircuitBreaker(
    failure_threshold=5,
    recovery_timeout=30.0,
    expected=(IOError, TimeoutError),
)

DEGRADED = {"status": "degraded", "balance_minor": None}


@service
class AccountGateway:

    def __init__(self, http_client, cache: CacheAdapter) -> None:
        self._http = http_client
        self._cache = cache

    @cacheable(
        backend=None,  # pass self._cache at runtime (see note below)
        key="account:{account_id}",
        ttl=timedelta(seconds=30),
    )
    @fallback(
        fallback_value=DEGRADED,
        on=(OperationTimeoutException, CircuitBreakerException, IOError),
    )
    @rate_limiter(_limiter)
    @bulkhead(_bh)
    @time_limiter(timeout=timedelta(seconds=2))
    @circuit_breaker(_cb)
    @retry(max_attempts=2, delay=0.05, backoff=2.0, exceptions=(IOError,))
    async def get_account(self, account_id: str) -> dict:
        resp = await self._http.get(f"/accounts/{account_id}")
        return resp.json()
:::

!!! note "Wiring `backend` in a class method"
    Because Python evaluates class-body decorators before `__init__` runs, `self._cache` is not yet available there. The listing above passes `backend=None` as a placeholder to illustrate the stacking order. In practice, wrap `get_account` in `__init__` the same way as the handler examples: `self.get_account = cacheable(backend=cache, key=..., ttl=...)(self._do_get_account)`. Alternatively, use a module-level `InMemoryCache` instance for testing and swap it via the DI container in production.

**How a call flows through the layers:**

1. `@cacheable` checks the cache. On a hit, every inner layer is skipped entirely.
2. On a miss, `@fallback` becomes the outermost safety net.
3. `@rate_limiter` checks the token bucket; rejects the call if empty.
4. `@bulkhead` checks the permit counter; rejects if at capacity.
5. `@time_limiter` sets a two-second deadline for the layers below.
6. `@circuit_breaker` rejects immediately if the circuit is OPEN.
7. `@retry` attempts the HTTP call up to two times on `IOError`.
8. On success, `@cacheable` stores the response for 30 seconds.
9. If `IOError`, `OperationTimeoutException`, or `CircuitBreakerException`
   escapes, `@fallback` catches it and returns `DEGRADED`.

Note that `@cacheable` sits *above* `@fallback`. That means:

- A cached `DEGRADED` response from a previous failure cycle is returned
  as-is for up to 30 seconds without hitting the network.
- If you do not want to cache degraded responses, move `@cacheable` below
  `@fallback`, or use the `unless` predicate:
  `unless=lambda r: r.get("status") == "degraded"`.

#### Run it — make the downstream fail and watch the stack degrade

You do not need a live `AccountService` to verify the stack. Wire the
resilience layers around a stub HTTP client that always raises, and assert
that the caller still gets a usable `DEGRADED` response instead of an
exception:

::: listing tests/resilience/test_gateway_stack.py | Listing 13.13a — The stack degrades instead of throwing
import pytest

from pyfly.resilience import fallback, retry

DEGRADED = {"status": "degraded", "balance_minor": None}


class _BrokenClient:
    async def get(self, path: str) -> dict:
        raise IOError("AccountService unreachable")


@fallback(fallback_value=DEGRADED, on=(IOError,))
@retry(max_attempts=2, delay=0.0, exceptions=(IOError,))
async def get_account(client: _BrokenClient, account_id: str) -> dict:
    resp = await client.get(f"/accounts/{account_id}")
    return resp


@pytest.mark.asyncio
async def test_degrades_instead_of_raising() -> None:
    result = await get_account(_BrokenClient(), "acc-1")
    assert result == DEGRADED
:::

Run it:

```console
$ uv run --extra dev pytest tests/resilience/test_gateway_stack.py -q
.                                                                        [100%]
1 passed in 0.05s
```

`@retry` tried the call twice, both attempts raised `IOError`, and `@fallback`
caught the final exception and returned `DEGRADED`. The caller never saw the
error — exactly the behaviour you want when `AccountService` is having a bad
day.

**What just happened.** You assembled a slice of the full stack — retry on the
inside, fallback on the outside — and proved that a hard failure surfaces as a
degraded-but-valid response. Add `@rate_limiter`, `@bulkhead`,
`@time_limiter`, and `@circuit_breaker` between them in the order shown above
and each one folds into the same flow: every exception they raise is caught by
the outer `@fallback`, so the caller always receives a response it can use.

---

## What you built {.recap}

This chapter closes Part IV. In Chapter 11 you split Lumen into independent
services with typed HTTP clients. In Chapter 12 you added `DepositSaga` to
coordinate multi-step operations with compensating transactions. Here you
made the whole system fast and fault-tolerant.

Concretely, you learned:

- **`@cacheable`** short-circuits balance reads on a cache hit; the
  five-second TTL bounds staleness to an acceptable window. Applied to
  `GetBalanceHandler` by wrapping `_fetch` at construction time — `_fetch`
  calls `repository.find_by_id` and projects the resulting `WalletEntity`
  onto `BalanceDto` via `entity_to_balance_dto` (`Mapper.project` + the
  `@projection`-marked `BalanceView`).
- **`@cache_put`** refreshes the cache as a side-effect of each
  `DepositFunds` command. `_deposit` is decorated with `@transactional()`;
  it does `find_by_id → to_aggregate → mutate → upsert` as one committed
  unit of work, then updates the cache with the returned balance. The key
  template must match the `@cacheable` key to hit the same slot.
- **`@cache_evict`** removes entries on wallet closure or administrative
  resets; `all_entries=True` flushes the entire cache in a single call.
- **`CacheManager`** mirrors writes to both Redis (primary) and
  `InMemoryCache` (fallback) and fails over transparently; it is the right
  default for any production deployment.
- **`RateLimiter`** + `@rate_limiter` cap inbound traffic with a token-
  bucket algorithm that allows controlled bursting.
- **`Bulkhead`** + `@bulkhead` isolate concurrency with a fail-fast
  semaphore that prevents one slow dependency from consuming all available
  resources.
- **`@time_limiter`** enforces deadlines using `asyncio.wait_for`, turning
  indefinitely hanging calls into bounded `OperationTimeoutException` errors.
- **`@fallback`** provides a degraded but functional response when every
  other layer has failed; the fallback method receives the original arguments
  and the caught exception via the `exc` keyword argument.
- **`@retry`** takes `max_attempts` as its only positional argument; all
  other parameters (`delay`, `backoff`, `max_delay`, `jitter`, `exceptions`)
  are keyword-only. It re-invokes operations a bounded number of times with
  exponential backoff.
- **`@circuit_breaker`** takes a `CircuitBreaker` **instance** — not keyword
  arguments — and opens the circuit after a failure threshold, short-
  circuiting subsequent calls during the recovery window so the downstream
  has time to recover.
- **`ResilienceRegistry`** (PyFly v26.6.110) materialises named
  `CircuitBreaker`, `RateLimiter`, `Bulkhead`, and time-limiter instances from
  `pyfly.resilience.*` config keys, so operations can tune thresholds in
  `pyfly.yaml` and inject the registry to look instances up by name — Spring
  Boot's `resilience4j.*.instances.<name>.*` parity.
- **Decorator order** matters: fallback outermost, then rate limiter,
  bulkhead, time limiter, circuit breaker, and retry innermost — with caching
  above the fallback to cache even degraded responses.

Lumen is now a multi-service, saga-coordinated, cached, and resilient system.
Part V adds the final production concerns: observability — metrics, distributed
tracing, and health endpoints — so you can see exactly what Lumen is doing in
production.

---

## Try it yourself {.exercises}

**Exercise 1 — Conditional caching.** The `GetBalance` handler is called far more often for active wallets than for test wallets. Add `condition=lambda query: not query.wallet_id.startswith("test-")` to the `cacheable(...)` call inside `GetBalanceHandler.__init__` and verify with a unit test using `InMemoryCache` that queries for test wallet ids always reach the repository.

**Exercise 2 — Circuit breaker with rate-based threshold.** Replace the consecutive-count circuit breaker in `AccountGateway` with a rate-based one: open the circuit when at least 60% of the last 20 calls fail. Construct `CircuitBreaker(failure_rate_threshold=0.6, window_size=20, recovery_timeout=60.0, expected=(IOError, TimeoutError))`. Two subtleties drive the test design. First, in rate mode the breaker stays not-tripped until the window is *full* — it requires a complete 20-call window before judging the rate — so a burst of failures alone never opens it. Second, the breaker only re-evaluates its trip condition on a *failure* (a success never opens it), so the call that crosses the threshold must itself be a failing call. Write a test that fires 8 succeeding calls followed by 12 failing ones (20 calls total = one full window, ending on a failure). Assert that the circuit stays `CLOSED` through call 19 (the window is still partial), then `OPENS` on the 20th call, when the window fills and the failure rate reaches exactly 12 / 20 = 0.60.

**Exercise 3 — Evict by prefix.** Lumen sometimes needs to invalidate all cache entries for a given wallet owner (GDPR deletion). Add a `purge_owner(owner_id: str)` method to a wallet admin service that calls `backend.evict_by_prefix(f"wallet:balance:{owner_id}:")` directly (without a decorator), and write a test that pre-populates three wallet keys for one owner and one for another, calls `purge_owner`, and asserts that only the target owner's entries are gone.

**Exercise 4 — Config-driven resilience.** Move `AccountGateway`'s hard-coded thresholds into `pyfly.yaml` under `pyfly.resilience.circuit-breaker.account-api`, `pyfly.resilience.rate-limiter.account-api`, and `pyfly.resilience.bulkhead.account-api`. Inject `ResilienceRegistry` into the gateway, look the three instances up by name, and write a test that asserts the materialised `CircuitBreaker.failure_threshold` matches the value you set in config. Note that `ResilienceRegistry.from_config(...)` expects a pyfly `Config`, not a plain dict — it calls `config.get_section("pyfly.resilience.circuit-breaker")` internally. Build one in the test from a nested dict, e.g. `Config({"pyfly": {"resilience": {"circuit-breaker": {"account-api": {"failure-threshold": 5}}}}})` (import `from pyfly.core.config import Config`), then pass that `Config` to `from_config`. Confirm that looking up a misspelled name raises `KeyError` with the list of available names.
