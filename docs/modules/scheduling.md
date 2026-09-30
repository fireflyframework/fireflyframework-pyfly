# Scheduling Guide

Schedule periodic tasks, cron jobs, and asynchronous method execution with the
PyFly scheduling module.

---

## Table of Contents

1. [Introduction](#introduction)
2. [The @scheduled Decorator](#the-scheduled-decorator)
   - [fixed_rate](#fixed_rate)
   - [fixed_delay](#fixed_delay)
   - [cron](#cron)
   - [initial_delay](#initial_delay)
   - [zone (time-zone-aware cron)](#zone-time-zone-aware-cron)
   - [lock (distributed locking)](#lock-distributed-locking)
3. [CronExpression](#cronexpression)
   - [5-Field Format](#5-field-format)
   - [6-Field (Spring) Format](#6-field-spring-format)
   - [Time Zones](#time-zones)
   - [next_fire_time()](#next_fire_time)
   - [previous_fire_time()](#previous_fire_time)
   - [next_n_fire_times()](#next_n_fire_times)
   - [seconds_until_next()](#seconds_until_next)
   - [Cron Examples](#cron-examples)
4. [TaskScheduler](#taskscheduler)
   - [Creating a TaskScheduler](#creating-a-taskscheduler)
   - [Discovering Scheduled Methods](#discovering-scheduled-methods)
   - [Starting and Stopping](#starting-and-stopping)
   - [How Loops Work Internally](#how-loops-work-internally)
5. [TaskExecutorPort](#taskexecutorport)
6. [AsyncIOTaskExecutor](#asynciotaskexecutor)
7. [ThreadPoolTaskExecutor](#threadpooltaskexecutor)
8. [Distributed Locking with DistributedLock](#distributed-locking-with-distributedlock)
   - [Built-in Lock Providers](#built-in-lock-providers)
   - [Cross-Process Coordination (custom)](#cross-process-coordination-custom)
   - [Registering a DistributedLock Bean](#registering-a-distributedlock-bean)
9. [The @async_method Decorator](#the-async_method-decorator)
10. [Configuration](#configuration)
    - [Selecting the Executor](#selecting-the-executor)
    - [Selecting the Lock Provider](#selecting-the-lock-provider)
11. [Auto-Configuration](#auto-configuration)
12. [Complete Example](#complete-example)

---

## Introduction

Most non-trivial applications need to run work on a schedule: syncing data from
an upstream API every five minutes, purging stale records at midnight, or
publishing health-check heartbeats every ten seconds. The PyFly scheduling
module gives you a declarative, decorator-driven way to define these tasks
without manually managing threads, event loops, or timer wheels.

The module is built around a hexagonal architecture:

- **Decorators** (`@scheduled`, `@async_method`) mark methods for scheduling.
- **CronExpression** provides next-fire-time calculations via standard 5-field
  cron syntax.
- **TaskScheduler** is the engine that discovers decorated methods, creates
  execution loops, and manages their lifecycle.
- **TaskExecutorPort** is the outbound port abstraction, allowing you to swap
  execution strategies; **AsyncIOTaskExecutor** and **ThreadPoolTaskExecutor**
  are the built-in adapters, selectable via `pyfly.scheduling.executor.type`.
- **DistributedLock** coordinates `@scheduled(lock=...)` jobs across instances;
  **LocalLock**, **InProcessDistributedLock**, **RedisDistributedLock**,
  **LeaseLock** (the database lease table) and **PostgresAdvisoryLock** are the
  built-in providers, selectable via `pyfly.scheduling.lock.provider`.

All public types are available from a single import:

```python
from pyfly.scheduling import (
    scheduled,
    async_method,
    CronExpression,
    TaskScheduler,
    TaskExecutorPort,
    DistributedLock,
    LocalLock,
    InProcessDistributedLock,
)
from pyfly.scheduling.adapters.asyncio_executor import AsyncIOTaskExecutor
from pyfly.scheduling.adapters.thread_executor import ThreadPoolTaskExecutor
# Built-in cluster-coordination lock adapters (normally selected via config):
from pyfly.scheduling.adapters.redis_lock import RedisDistributedLock
from pyfly.scheduling.adapters.lease_lock import LeaseLock
from pyfly.scheduling.adapters.postgres_lock import PostgresAdvisoryLock
```

---

## The @scheduled Decorator

`@scheduled` marks a bean method for periodic execution. It is a keyword-only
decorator that accepts exactly one trigger parameter: `fixed_rate`,
`fixed_delay`, or `cron`. Providing zero or more than one trigger raises a
`ValueError` at decoration time.

```python
from pyfly.scheduling import scheduled
```

### fixed_rate

Runs the method at a fixed interval, measured from the **start** of each
invocation. A job never runs concurrently with itself (Spring's scheduler
contract): if a run takes longer than the interval, the next run starts as soon
as it ends, late but never alongside it, and the following one an interval after
that start. Each run of a `@transactional` job holds one pooled connection, so a
database slowdown can no longer pile up one run (and one connection) per period.

The parameter accepts a `datetime.timedelta`:

```python
from datetime import timedelta

class MetricsCollector:
    @scheduled(fixed_rate=timedelta(seconds=30))
    async def collect(self):
        """Collect system metrics every 30 seconds."""
        await self.scrape_metrics()
```

### fixed_delay

Runs the method repeatedly with a fixed delay **between the end of one
execution and the start of the next**. This guarantees a minimum gap between
runs, regardless of how long each execution takes.

```python
class DataSyncer:
    @scheduled(fixed_delay=timedelta(minutes=5))
    async def sync(self):
        """Sync data, then wait 5 minutes before the next sync."""
        await self.pull_upstream_changes()
```

The key difference from `fixed_rate`: with `fixed_delay` the gap is measured
from the end of a run, with `fixed_rate` from its start.

### concurrent

`@scheduled(concurrent=N)` lets up to *N* runs of a `fixed_rate` or `cron` job be
in flight at once (the default is 1: no overlap). Keep it well below the
connection pool's size when the job is `@transactional`. `fixed_delay` takes no
`concurrent` (a `ValueError`): it starts a run after the previous one ended.

Through 26.09.07 `fixed_rate` and `cron` submitted a run every period whether the
previous one had ended or not, so a slow job overlapped itself without bound.

### cron

Runs the method according to a cron expression. The scheduler calculates
`seconds_until_next()` via `CronExpression`, sleeps that long, then executes
the method. The next fire time is computed once the previous run has ended, as
Spring's cron trigger does: fire times that pass while a run is going are
skipped, never run late or concurrently. Both the standard 5-field format and the
Spring-style 6-field (seconds-first) format are accepted.

```python
class ReportGenerator:
    @scheduled(cron="0 2 * * 1")  # Every Monday at 02:00
    async def generate_weekly_report(self):
        await self.build_and_email_report()
```

### initial_delay

An optional `timedelta` that delays the very first execution. Applies to both
`fixed_rate` and `fixed_delay` triggers. Ignored for `cron` triggers (the first
execution always waits for the next matching cron time).

```python
class CacheWarmer:
    @scheduled(fixed_rate=timedelta(minutes=10), initial_delay=timedelta(seconds=30))
    async def warm_cache(self):
        """Wait 30 seconds after startup, then warm cache every 10 minutes."""
        await self.preload_hot_keys()
```

### zone (time-zone-aware cron)

By default, `cron` expressions are evaluated in **UTC**. Pass `zone` with an
IANA time-zone name to evaluate fire times in that zone instead (this mirrors
Spring's `@Scheduled(zone=...)`). DST transitions are handled by the underlying
`zoneinfo` database.

```python
class BillingService:
    # 02:00 every day in New York local time, regardless of server TZ
    @scheduled(cron="0 2 * * *", zone="America/New_York")
    async def run_nightly_billing(self):
        await self.close_books()
```

`zone` only affects `cron` triggers; it is ignored for `fixed_rate` and
`fixed_delay` (which measure elapsed wall-clock time, not calendar instants).

### lock (distributed locking)

When you run multiple instances of the same service, every instance schedules
the same `@scheduled` method — so without coordination a midnight job would
fire once *per instance*. The `lock` parameter provides ShedLock / Spring
`@SchedulerLock` parity: before each tick the scheduler tries to acquire a
named lock, and **skips the run** if it is already held elsewhere, so only one
instance executes the job per fire. The lock is always released when the body
finishes, and `lock_ttl` is the safety valve: every built-in provider ends a
lock after `lock_ttl` even when its holder hangs or crashes mid-run, so the job
runs elsewhere after at most `lock_ttl`.

```python
class ReportService:
    # lock=True auto-derives the name "ReportService.daily_rollup"
    @scheduled(cron="0 0 * * *", lock=True)
    async def daily_rollup(self):
        await self.aggregate_yesterday()
```

- `lock=True` — derives the lock name from the class and method as
  `"ClassName.method_name"`.
- `lock="some-name"` — uses an explicit shared name (useful when two different
  methods must be mutually exclusive across the cluster).
- `lock=None` (default) — no locking.
- `lock_ttl` — a `timedelta` for the maximum time the lock may be held before
  it auto-expires. Defaults to 60 seconds. Set it comfortably longer than the
  job's worst-case runtime: the run is time-boxed to it. The lock ends at its
  TTL whatever the run does, and another instance may then start the job, so a
  run still going at the TTL is cancelled (its `@transactional` work rolls back)
  and logged at `ERROR` with the job's name. A synchronous job's thread cannot be
  cancelled, so its run is not time-boxed: it waits for the thread, keeping the
  job's slot (the next tick never starts a second thread of the job beside it),
  and is logged at `ERROR` once it ends past the TTL. The run releases its lock from its own task once it has
  ended: the built-in providers tell holders apart per task (or per acquisition),
  so a run cancelled at its TTL that ends after the job's next run took the lock
  leaves that run's lock alone.

A failure to take or release the lock (the lock's database or Redis is down, a
misconfigured provider) is logged at `ERROR` with the job's name, like a
failure of the job itself, and the tick is skipped; it used to escape as an
unnamed "Task exception was never retrieved". A provider that cannot work on the
configured datasource fails at startup (the lease table is checked, the advisory
lock needs PostgreSQL).

```python
from datetime import timedelta

class ImportService:
    @scheduled(fixed_rate=timedelta(minutes=5), lock="upstream-import", lock_ttl=timedelta(minutes=10))
    async def import_batch(self):
        await self.pull_and_load()
```

`lock` works with all three trigger types (`cron`, `fixed_rate`,
`fixed_delay`). Out of the box the scheduler uses an in-process `LocalLock`
that always acquires — so single-instance behavior is unchanged. For real
coordination, select a built-in lock provider with
`pyfly.scheduling.lock.provider` (`memory`, `redis`, `database` or `postgres`) — no custom
code required — or register your own `DistributedLock` bean. See
[Distributed Locking with DistributedLock](#distributed-locking-with-distributedlock).

### Decorator Metadata

Under the hood, `@scheduled` attaches metadata attributes to the decorated
function:

| Attribute | Value |
|---|---|
| `__pyfly_scheduled__` | `True` |
| `__pyfly_scheduled_cron__` | The cron expression string, or `None` |
| `__pyfly_scheduled_fixed_rate__` | The `timedelta`, or `None` |
| `__pyfly_scheduled_fixed_delay__` | The `timedelta`, or `None` |
| `__pyfly_scheduled_initial_delay__` | The `timedelta`, or `None` |
| `__pyfly_scheduled_zone__` | The IANA zone string, or `None` |
| `__pyfly_scheduled_lock__` | `True`, the lock-name string, or `None` |
| `__pyfly_scheduled_lock_ttl__` | The TTL in seconds (`float`), or `None` |
| `__pyfly_scheduled_concurrent__` | How many runs may be in flight at once |

The `TaskScheduler` reads these attributes during its discovery phase. A
`lock=True` value is resolved to the `"ClassName.method"` name at discovery
time.

---

## CronExpression

`CronExpression` is an immutable dataclass that wraps a cron expression string
and provides fire-time calculation methods. It delegates parsing and iteration
to the [croniter](https://github.com/kiorky/croniter) library.

```python
from pyfly.scheduling import CronExpression
```

### 5-Field Format

PyFly uses the standard 5-field cron format:

```
 +------------- minute       (0-59)
 |  +---------- hour         (0-23)
 |  |  +------- day of month (1-31)
 |  |  |  +---- month        (1-12)
 |  |  |  |  +- day of week  (0-6, 0 = Sunday)
 |  |  |  |  |
 *  *  *  *  *
```

Special characters: `*` (any), `,` (list), `-` (range), `/` (step).

Invalid expressions raise `ValueError` during construction:

```python
CronExpression("invalid")  # ValueError: Invalid cron expression: invalid
```

### 6-Field (Spring) Format

`CronExpression` also accepts the Spring-style 6-field format, where the first
field is **seconds**. The field count is detected automatically:

```
 +---------------- second       (0-59)
 |  +------------- minute       (0-59)
 |  |  +---------- hour         (0-23)
 |  |  |  +------- day of month (1-31)
 |  |  |  |  +---- month        (1-12)
 |  |  |  |  |  +- day of week  (0-6)
 *  *  *  *  *  *
```

```python
from pyfly.scheduling import CronExpression

# Every day at 12:00:00 (Spring 6-field, seconds-first)
cron = CronExpression("0 0 12 * * *")
```

The Spring `?` "no specific value" placeholder is also accepted in the
day-of-month and day-of-week fields (it is normalized to `*`):

```python
CronExpression("0 0 12 ? * *")  # noon every day
```

### Time Zones

By default fire times are computed in **UTC**. Pass `zone` with an IANA
time-zone name to compute them in that zone instead; the returned `datetime`
values are zone-aware:

```python
from pyfly.scheduling import CronExpression

cron = CronExpression("0 9 * * *", zone="America/New_York")
next_run = cron.next_fire_time()
print(next_run.tzinfo)  # America/New_York
```

This is the same `zone` value accepted by `@scheduled(cron=..., zone=...)`.

### next_fire_time()

Returns the next `datetime` after a given reference point (default: `now()`):

```python
from datetime import datetime
from pyfly.scheduling import CronExpression

cron = CronExpression("0 9 * * *")  # Daily at 09:00
next_run = cron.next_fire_time()
print(next_run)  # e.g., 2026-02-15 09:00:00

# With an explicit reference time
ref = datetime(2026, 3, 1, 8, 0)
next_run = cron.next_fire_time(after=ref)
print(next_run)  # 2026-03-01 09:00:00
```

### previous_fire_time()

Returns the most recent fire time before a given reference point:

```python
cron = CronExpression("0 */6 * * *")  # Every 6 hours
prev = cron.previous_fire_time()
```

### next_n_fire_times()

Returns a list of the next N fire times:

```python
cron = CronExpression("30 8 * * 1-5")  # Weekdays at 08:30
upcoming = cron.next_n_fire_times(5)
for t in upcoming:
    print(t)
```

### seconds_until_next()

Returns the number of seconds (as `float`) until the next fire time. This is
the method the `TaskScheduler` uses to determine how long to sleep in a cron
loop:

```python
cron = CronExpression("0 0 * * *")  # Midnight
delay = cron.seconds_until_next()
print(f"Next midnight in {delay:.0f} seconds")
```

### Cron Examples

| Expression | Description |
|---|---|
| `* * * * *` | Every minute |
| `0 * * * *` | Every hour, on the hour |
| `0 0 * * *` | Every day at midnight |
| `0 9 * * 1-5` | Weekdays at 09:00 |
| `30 2 1 * *` | 1st of each month at 02:30 |
| `*/15 * * * *` | Every 15 minutes |
| `0 0 * * 0` | Every Sunday at midnight |
| `0 8,12,18 * * *` | Daily at 08:00, 12:00, and 18:00 |

---

## TaskScheduler

`TaskScheduler` is the engine that ties everything together. It scans beans for
`@scheduled` methods, creates async loops for each, and manages start/stop
lifecycle.

```python
from pyfly.scheduling import TaskScheduler
```

### Creating a TaskScheduler

The constructor takes an optional `executor: TaskExecutorPort` (defaults to
`AsyncIOTaskExecutor`) and an optional `lock: DistributedLock` (defaults to
`LocalLock`, used for `@scheduled(lock=...)` coordination):

```python
# Default: AsyncIOTaskExecutor + in-process LocalLock
scheduler = TaskScheduler()

# Custom: use ThreadPoolTaskExecutor for CPU-bound tasks
from pyfly.scheduling.adapters.thread_executor import ThreadPoolTaskExecutor
scheduler = TaskScheduler(executor=ThreadPoolTaskExecutor(max_workers=8))

# Cross-process locking for @scheduled(lock=...) — see Distributed Locking below
scheduler = TaskScheduler(lock=RedisLock(redis_client))
```

### Discovering Scheduled Methods

Call `discover()` with a list of bean instances. It scans every public attribute
(names not starting with `_`) and records those marked with
`__pyfly_scheduled__ = True`. Since v26.09.11, discovery skips custom
descriptors as well as properties and cached properties before binding methods.
Pydantic settings can be included without instance-field deprecation warnings.
Ordinary, inherited, static and class methods remain discoverable; expose jobs
as methods rather than descriptor-produced callables. Returns the number of
scheduled methods found:

```python
beans = [metrics_collector, data_syncer, report_generator]
count = scheduler.discover(beans)
print(f"Found {count} scheduled methods")
```

### Starting and Stopping

`start()` and `stop()` are async methods. `start()` creates an
`asyncio.Task` loop for each entry discovered since the last start (calling it
again starts only the new ones). `stop()` stops the loops and then drains the
runs in flight through the executor:

```python
await scheduler.start()
# ... application runs ...
await scheduler.stop()
```

A loop only ever waits (for its next fire time, a free slot or its run's end),
so stopping it never cancels a run: every run in flight finishes, whatever its
trigger. When the caller cuts the drain short (the application context bounds
it by `pyfly.context.shutdown-timeout`), the runs still going are cancelled and
awaited. Through 26.09.07 `stop()` cancelled an in-flight `fixed_delay` run at once
(rolling its transaction back) while it let `fixed_rate` and `cron` runs finish.

`TaskScheduler` is a lifecycle bean of `CONSUMER_PHASE`: the application context
stops it before any `@pre_destroy`, while the beans its jobs use still work.

### How Loops Work Internally

Each trigger type has its own loop coroutine inside `TaskScheduler`, and each
entry has as many slots as its `concurrent` (1 by default); a run holds a slot
until it ends:

- **Cron loop** (`_run_cron_loop`): waits for a free slot, calculates
  `seconds_until_next()` from a `CronExpression` (so, with one slot, after the
  previous run ended), sleeps that duration, submits a run, then repeats.
- **Fixed-rate loop** (`_run_fixed_rate_loop`): optionally sleeps for
  `initial_delay`, then sleeps until the next start time, waits for a free slot,
  submits a run and sets the next start time one interval later.
- **Fixed-delay loop** (`_run_fixed_delay_loop`): optionally sleeps for
  `initial_delay`, then submits a run, waits for it to end, sleeps for the delay,
  then repeats.

Runs are submitted as tasks started with the transaction state cleared
(`pyfly.data.transaction.detached`): a run's `@transactional` work is a unit of
its own. Both sync and async methods are supported: an async method is awaited,
a sync one runs in the executor's thread pool (`ThreadPoolTaskExecutor.run_sync`)
or, with the asyncio executor, `asyncio.to_thread`.

---

## TaskExecutorPort

`TaskExecutorPort` is a `Protocol` (runtime-checkable) that defines the
contract for task execution:

```python
from pyfly.scheduling import TaskExecutorPort

@runtime_checkable
class TaskExecutorPort(Protocol):
    async def submit(self, coro: Coroutine[Any, Any, T]) -> asyncio.Task[T]: ...
    async def start(self) -> None: ...
    async def stop(self) -> None: ...
```

You can implement this protocol to create custom executors -- for example, one
that publishes tasks to a distributed queue or logs execution metrics. Two
rules keep the scheduler's guarantees: `submit()` runs the coroutine outside the
submitter's unit of work (start the task with `pyfly.data.transaction.detached`,
as the built-in executors do: an `@async_method` call made inside
`@transactional` must not join, or outlive, the caller's transaction), and
`stop()` waits for the tasks in flight, cancelling and awaiting them when it is
cancelled itself.

---

## AsyncIOTaskExecutor

The default executor. Starts each coroutine in a task of its own with the
transaction state cleared (`pyfly.data.transaction.detached`) and tracks the
running tasks in a `set` for clean shutdown:

```python
from pyfly.scheduling.adapters.asyncio_executor import AsyncIOTaskExecutor

executor = AsyncIOTaskExecutor()
task = await executor.submit(some_coroutine())
await executor.stop()  # Wait for all pending tasks
```

- **submit()**: Creates an `asyncio.Task` via `detached()`, adds it to an
  internal tracking set, and registers a done-callback that removes it.
- **start()**: No-op (ready after construction).
- **stop()**: Waits for all pending tasks to complete (tasks submitted meanwhile
  included), then clears the task set. Cancelled while it waits, it cancels the
  pending tasks and waits for them to end before the cancellation propagates.

This executor is ideal for I/O-bound tasks that use `async`/`await`.

---

## ThreadPoolTaskExecutor

For CPU-bound or blocking work, `ThreadPoolTaskExecutor` wraps a standard
`concurrent.futures.ThreadPoolExecutor`:

```python
from pyfly.scheduling.adapters.thread_executor import ThreadPoolTaskExecutor

executor = ThreadPoolTaskExecutor(max_workers=4)
```

It exposes three submission methods:

- **submit(coro)**: Works identically to `AsyncIOTaskExecutor.submit()` --
  creates a detached `asyncio.Task` for async coroutines.
- **submit_sync(func, *args)**: Runs a synchronous function in the thread pool
  via `loop.run_in_executor()`, wraps the result with `asyncio.ensure_future()`.
- **run_sync(func, *args)**: Awaits a synchronous function run in the thread
  pool. The `TaskScheduler` runs synchronous `@scheduled` methods through it, so
  `max_workers` bounds their threads.

```python
# Async coroutine
task = await executor.submit(async_work())

# Sync function in thread pool
task = executor.submit_sync(cpu_heavy_function, arg1, arg2)
```

**Constructor:**

| Parameter | Type | Default | Description |
|---|---|---|---|
| `max_workers` | `int` | `4` | Number of threads in the pool |

**API:**

- **start()**: No-op (ready after construction).
- **stop()**: Waits for all pending tasks (as `AsyncIOTaskExecutor.stop()`), clears task set, shuts down the
  thread pool and waits for its threads off the event loop: a thread may still be running a function whose
  task was cancelled (a synchronous `@async_method` call its caller cancelled, say), and the application goes on
  meanwhile. When the wait is cut short
  (`pyfly.context.shutdown-timeout`), the functions still queued never start and a running one finishes on its
  thread.

---

## Distributed Locking with DistributedLock

The `@scheduled(lock=...)` feature ([above](#lock-distributed-locking)) relies
on a lock implementation supplied to the `TaskScheduler`. There are two pieces:

```python
from pyfly.scheduling import DistributedLock, LocalLock
```

- **`DistributedLock`** — a `runtime_checkable` `Protocol` describing a
  best-effort, TTL-bounded named lock:

  ```python
  @runtime_checkable
  class DistributedLock(Protocol):
      async def try_acquire(self, name: str, ttl: float) -> bool: ...
      async def release(self, name: str) -> None: ...
  ```

- **`LocalLock`** — the default in-process implementation whose `try_acquire`
  **always returns `True`**. It performs no cross-process coordination, so a
  single-instance deployment behaves exactly as if no lock were declared.

The `TaskScheduler` accepts the lock via its constructor; if none is given it
falls back to `LocalLock`:

```python
from pyfly.scheduling import TaskScheduler, LocalLock

scheduler = TaskScheduler(lock=LocalLock())  # default behavior
```

### Built-in Lock Providers

You do **not** need to write a lock to coordinate across instances. The
`SchedulingAutoConfiguration.distributed_lock` bean selects a provider from
`pyfly.scheduling.lock.provider`, and the auto-wired `TaskScheduler` uses it for
every `@scheduled(lock=...)` job:

| `pyfly.scheduling.lock.provider` | Implementation | Scope | Extra infra |
|---|---|---|---|
| `none` *(default)* | `LocalLock` | single instance (always acquires) | none |
| `memory` | `InProcessDistributedLock` | one process (real mutual exclusion within the process) | none |
| `redis` | `RedisDistributedLock` | cross-process / cluster | Redis |
| `database` | `LeaseLock` | cross-process / cluster | none beyond the application's database (any backend) |
| `postgres` | `LeaseLock`, or `PostgresAdvisoryLock` with `postgres.advisory: true` | cross-process / cluster | none beyond an existing Postgres |

```yaml
# pyfly.yaml
pyfly:
  scheduling:
    lock:
      provider: database   # none | memory | redis | database | postgres
      datasource: primary  # the datasource of the lease table (default: the primary)
```

- **`none`** — `LocalLock`; `try_acquire` always returns `True`. Single-instance
  default; `lock=` declarations are effectively no-ops.
- **`memory`** — `InProcessDistributedLock`; real mutual exclusion **within one
  process** (with a TTL self-heal so a crashed/never-released name auto-frees
  after `lock_ttl`). Prevents a slow tick from overlapping its next tick in the
  same process, but does **not** coordinate across processes. A tick whose lock
  ended at `lock_ttl` does not release the lock the next tick took since.
- **`redis`** — `RedisDistributedLock`; cross-process via an atomic Redis
  `SET key value NX PX <ttl-ms>`, with an owner-token compare-and-delete release
  (an instance only releases a lock it still owns). The async Redis client is
  built by the auto-config from `pyfly.scheduling.lock.redis.url` (default
  `redis://localhost:6379/0`) and injected — the adapter never imports `redis`
  itself. Selected only when `redis.asyncio` is importable; otherwise the bean
  falls back to `LocalLock`. Keys are prefixed `pyfly:schedlock:`.
- **`database`** — `LeaseLock`; a ShedLock-style **lease table**
  (`pyfly_locks`: `name`, `lock_until`, `locked_at`, `locked_by`, `fence`) on
  the datasource named by `pyfly.scheduling.lock.datasource` (or given by
  `pyfly.scheduling.lock.url`), by default the primary. Taking a lock is one
  statement on PostgreSQL and SQLite, `INSERT ... ON CONFLICT (name) DO UPDATE
  ... WHERE lock_until <= now` (it inserts the row, takes an ended lease over,
  or leaves a live one alone); on MySQL and MariaDB a conditional `UPDATE ...
  WHERE lock_until <= now`, after an `INSERT IGNORE` when the node has not
  found the row yet. Each runs in a short unit of its own that commits at once
  (an autocommit statement on PostgreSQL); releasing it sets `lock_until` to
  now. It works on every backend,
  holds **no connection** while the job runs, and honors `lock_ttl`: a hung job
  blocks its schedule for at most `lock_ttl`. When the hung job finally ends,
  it does not release the lease another node took since, and a WARNING
  (`scheduler_lease_expired_before_release`) says the job outlived its TTL. The
  nodes compare `lock_until` with their own clocks: keep them synchronized (NTP).
  The table is created at startup if `pyfly.data.relational.ddl-auto` is
  `create` (the default on an embedded database) or `create-drop`, and only
  checked with `none` (the default on a database server and beside startup
  migrations) or `validate`. Lock names match exactly on every backend (a binary
  collation on MySQL and MariaDB, so `Nightly` and `nightly` are two leases). A
  lock name is at most 255 characters
  (`LeaseLock.MAX_NAME_LENGTH`, the length of `pyfly_locks.name`): a longer one
  raises `ValueError` before any statement runs, where MySQL and MariaDB would
  otherwise truncate it into a lease nobody could release. `LeaseLock.acquire(name, ttl, wait=...)`,
  `extend()` and `holder()` (with a fencing token that grows at every
  acquisition) serve other work that must run on one node at a time.
- **`postgres`** — the same lease table on a PostgreSQL datasource. With
  `pyfly.scheduling.lock.postgres.advisory: true` it is `PostgresAdvisoryLock`
  instead, an opt-in accelerator on Postgres **session-level advisory locks**
  (`pg_try_advisory_lock` / `pg_advisory_unlock`): the server releases the lock
  the moment the holder's connection goes away. The lock name is mapped to a
  stable signed 64-bit key (blake2b, deterministic across processes). The lock
  holds a pooled connection for the whole job, in `AUTOCOMMIT` (idle, never
  idle in transaction, so `idle_in_transaction_session_timeout` cannot drop it
  mid-job); a watchdog ends the lock at `lock_ttl` with a WARNING
  (`scheduler_advisory_lock_expired`) but does not cancel the job itself, as
  with the lease table (the `TaskScheduler` cancels an async `@scheduled` run at
  `lock_ttl`, see [lock](#lock-distributed-locking)); and an acquisition or an unlock that fails (a cancellation
  included) discards the connection instead of returning a session that may
  hold the lock to the pool. An acquisition belongs to the task that took it:
  a tick whose lock ended at `lock_ttl` (or whose session the server ended,
  logged as `scheduler_advisory_lock_lost`) does not release the lock the next
  tick of the process took since. The datasource must be PostgreSQL, or the
  startup fails.

**When to use which:**

- Single instance, no cluster → leave the default `none` (or `memory` if you
  want to prevent in-process overlap of a slow job).
- Multiple instances and you already run Redis → `redis`.
- Multiple instances on a relational database (but no Redis) → `database`, to
  avoid standing up new infrastructure just for scheduling.

### Cross-Process Coordination (custom)

If none of the built-in providers fit, implement `DistributedLock` against any
shared store and pass it to the scheduler (or register it as a bean — see
below). Any object with conforming `try_acquire` / `release` coroutines
satisfies the protocol:

```python
from pyfly.scheduling import DistributedLock, TaskScheduler


class RedisLock:
    """Best-effort lock backed by Redis SET NX PX."""

    def __init__(self, redis):
        self._redis = redis

    async def try_acquire(self, name: str, ttl: float) -> bool:
        # SET key value NX PX <ttl-ms> returns None when the key already exists.
        ok = await self._redis.set(f"pyfly:lock:{name}", "1", nx=True, px=int(ttl * 1000))
        return ok is True

    async def release(self, name: str) -> None:
        await self._redis.delete(f"pyfly:lock:{name}")


scheduler = TaskScheduler(lock=RedisLock(redis_client))
```

### Registering a DistributedLock Bean

The built-in providers above (`pyfly.scheduling.lock.provider`) are themselves
registered as the `distributed_lock` bean, so in most cases a YAML setting is
all you need. If you want a fully custom lock, declare your own bean of type
`DistributedLock`; the auto-wired `TaskScheduler` looks it up from the container
and uses it for `@scheduled(lock=...)` coordination (falling back to `LocalLock`
if none is registered):

```python
from pyfly.container.bean import bean
from pyfly.container import configuration
from pyfly.scheduling import DistributedLock


@configuration
class LockConfig:
    @bean
    def distributed_lock(self) -> DistributedLock:
        return RedisLock(redis_client)
```

The scheduler will then skip any locked tick whose lock is already held by
another instance, giving you cluster-wide single-firing of scheduled jobs.

**Source:** `src/pyfly/scheduling/lock.py`, `src/pyfly/scheduling/task_scheduler.py`,
`src/pyfly/scheduling/auto_configuration.py`

---

## The @async_method Decorator

`@async_method` marks a method to execute asynchronously via a
`TaskExecutorPort`. The caller returns immediately -- the actual execution is
offloaded to the executor:

```python
from pyfly.scheduling import async_method

class NotificationService:
    @async_method
    async def send_email(self, to: str, subject: str, body: str):
        """This runs asynchronously -- caller does not wait."""
        await self.email_client.send(to, subject, body)
```

Under the hood, `@async_method` sets `__pyfly_async__ = True` on the function,
and the application context replaces the bean's method with a dispatcher
(`pyfly.scheduling.async_methods.dispatching`), Spring `@Async` style:

- **The caller returns at once.** `await notifier.send_email(...)` submits the
  call through the `TaskScheduler`'s `TaskExecutorPort` and gives back the
  `asyncio.Task` running it. Await that task when you need the result:
  `result = await (await service.compute(order))`.
- **Its own unit of work.** The task starts with the transaction state cleared
  (`pyfly.data.transaction.detached`): a `@transactional` method opens its own
  transaction. Its failure never rolls the caller back, a caller that fails
  after the call does not discard its writes, and the caller's commit does not
  wait for it.
- **Uncaught exceptions** go to the `AsyncUncaughtExceptionHandler` bean
  (`handle_uncaught_exception(error, method, args, kwargs)`), by default
  `LoggingAsyncUncaughtExceptionHandler`, which logs `async_method_failed` at
  `ERROR`. A caller that awaits the task gets the exception as well.
- **Synchronous methods** run in the executor's thread pool
  (`ThreadPoolTaskExecutor.run_sync`), or through `asyncio.to_thread`.
- **Shutdown** waits for the calls in flight: they run on the `TaskScheduler`'s
  executor, which the context stops before any `@pre_destroy`. A call submitted
  once the scheduler has stopped (by another consumer-phase bean still draining,
  such as a message listener) is not waited for, and a synchronous method's call
  fails then with `RuntimeError` when the executor is a `ThreadPoolTaskExecutor`
  (its pool has shut down, and rejects new work as Spring's does).

```python
from pyfly.scheduling.async_methods import AsyncUncaughtExceptionHandler

@component
class AlertingHandler(AsyncUncaughtExceptionHandler):
    def handle_uncaught_exception(self, error, method, args, kwargs) -> None:
        alerts.raise_incident(f"{method.__qualname__} failed: {error}")
```

Through 26.09.07 `@async_method` awaited coroutine methods inline: the call ran in
the caller's transaction, blocked the caller for its whole duration, and its
failure rolled the caller back.

---

## Configuration

Scheduling behavior is configured in `pyfly.yaml`. The auto-configured
`task_scheduler` and `distributed_lock` beans read these keys to pick the
executor and lock backend:

```yaml
pyfly:
  scheduling:
    enabled: true
    executor:
      type: asyncio       # asyncio | thread
      max-workers: 4      # thread-pool size when type=thread
    lock:
      provider: none      # none | memory | redis | database | postgres
      redis:
        url: redis://localhost:6379/0   # used when provider=redis
      datasource: primary # provider=database/postgres: a datasource of the registry (or url:)
```

| Key | Description | Default |
|---|---|---|
| `pyfly.scheduling.enabled` | Convention flag set by the `application`/`data` starters (see [Auto-Configuration](#auto-configuration)) | `true` |
| `pyfly.scheduling.executor.type` | Executor backend: `asyncio` (in-loop) or `thread` (`ThreadPoolTaskExecutor`) | `asyncio` |
| `pyfly.scheduling.executor.max-workers` | Thread-pool size when `executor.type=thread` | `4` |
| `pyfly.scheduling.lock.provider` | Distributed-lock backend: `none` / `memory` / `redis` / `database` / `postgres` | `none` |
| `pyfly.scheduling.lock.redis.url` | Redis URL when `lock.provider=redis` | `redis://localhost:6379/0` |
| `pyfly.scheduling.lock.datasource` | The datasource of the `database`/`postgres` lock (a name in the registry); not together with `lock.url` | the primary |
| `pyfly.scheduling.lock.url` | The URL of that datasource, resolved through the registry | the primary |
| `pyfly.scheduling.lock.postgres.advisory` | With `provider=postgres`, PostgreSQL advisory locks instead of the lease table (needs a PostgreSQL datasource) | `false` |

**Requires:** `uv add "pyfly[scheduling]"` (installs `croniter` for cron
expression parsing). The `redis` lock provider additionally needs
`redis.asyncio` importable; the `database` and `postgres` providers need a
relational datasource (the primary, or `pyfly.scheduling.lock.datasource` /
`.url`), and the advisory lock needs it on PostgreSQL.

### Selecting the Executor

The scheduler submits each run through a `TaskExecutorPort`. The auto-config
chooses the adapter from `pyfly.scheduling.executor.type`:

- `asyncio` *(default)* — `AsyncIOTaskExecutor`. Ideal for I/O-bound
  `async`/`await` tasks; runs work on the event loop.
- `thread` — `ThreadPoolTaskExecutor(max_workers=pyfly.scheduling.executor.max-workers)`.
  Offloads blocking/CPU-bound jobs to a worker-thread pool.

```yaml
pyfly:
  scheduling:
    executor:
      type: thread
      max-workers: 8
```

This is equivalent to constructing
`TaskScheduler(executor=ThreadPoolTaskExecutor(max_workers=8))` yourself (see
[ThreadPoolTaskExecutor](#threadpooltaskexecutor)). To swap in a fully custom
executor, override the `task_scheduler` bean (see
[Overriding the Auto-Configured Scheduler](#overriding-the-auto-configured-scheduler)).

### Selecting the Lock Provider

`pyfly.scheduling.lock.provider` chooses the `distributed_lock` bean used for
`@scheduled(lock=...)` coordination. See
[Built-in Lock Providers](#built-in-lock-providers) for the full matrix and
guidance on `none` / `memory` / `redis` / `database` / `postgres`.

```yaml
pyfly:
  scheduling:
    lock:
      provider: redis
      redis:
        url: redis://cache:6379/0
```

---

## Auto-Configuration

When `croniter` is installed, PyFly automatically registers a `TaskScheduler` bean through the `SchedulingAutoConfiguration` class. This eliminates the need to manually create and manage a `TaskScheduler` instance.

### SchedulingAutoConfiguration

**Conditions:** `croniter` library installed.

| Bean | Type | Description |
|------|------|-------------|
| `distributed_lock` | `DistributedLock` | Lock backend for `@scheduled(lock=...)`, selected by `pyfly.scheduling.lock.provider` (`none`/`memory`/`redis`/`database`/`postgres`) |
| `task_scheduler` | `TaskScheduler` | Container-managed scheduler that discovers and runs `@scheduled` methods; uses the executor from `pyfly.scheduling.executor.type` and resolves the `distributed_lock` bean |

With auto-configuration, you no longer need a `SchedulerManager` service. The `ApplicationContext` automatically:

1. Creates a `TaskScheduler` bean (from auto-config, or uses one you provide)
2. Discovers all `@scheduled` methods across all beans
3. Starts the scheduler during context startup
4. Stops the scheduler during context shutdown

### Before Auto-Configuration (Manual)

```python
@service
class SchedulerManager:
    def __init__(self, sync_service: DataSyncService):
        self._scheduler = TaskScheduler()  # Manual creation
        self._beans = [sync_service]

    @post_construct
    async def start(self):
        self._scheduler.discover(self._beans)
        await self._scheduler.start()

    @pre_destroy
    async def stop(self):
        await self._scheduler.stop()
```

### After Auto-Configuration (Automatic)

```python
# Just declare your scheduled beans — no SchedulerManager needed!
@service
class DataSyncService:
    @scheduled(fixed_rate=timedelta(minutes=5))
    async def sync(self):
        ...
```

The `TaskScheduler` is auto-wired as a container bean and the `ApplicationContext` handles discovery and lifecycle.

### Overriding the Auto-Configured Scheduler

For the common cases — switching the executor to a thread pool, or picking a
lock provider — you do **not** need a custom bean; just set
`pyfly.scheduling.executor.type` / `pyfly.scheduling.lock.provider` in
`pyfly.yaml` (see [Configuration](#configuration)). Provide your own
`TaskScheduler` bean only when you need a fully custom executor or lock:

```python
from pyfly.container.bean import bean
from pyfly.container import configuration
from pyfly.scheduling import TaskScheduler
from pyfly.scheduling.adapters.thread_executor import ThreadPoolTaskExecutor

@configuration
class MySchedulingConfig:
    @bean
    def task_scheduler(self) -> TaskScheduler:
        return TaskScheduler(executor=ThreadPoolTaskExecutor(max_workers=4))
```

**Source:** `src/pyfly/scheduling/auto_configuration.py`

---

## Complete Example

Below is a full example that demonstrates all three trigger types working
together in a single application: a periodic data sync (fixed delay), a
cron-based nightly cleanup, and a fixed-rate health heartbeat.

```python
from datetime import timedelta

from pyfly.container import service
from pyfly.context import post_construct, pre_destroy
from pyfly.scheduling import (
    CronExpression,
    TaskScheduler,
    scheduled,
)
from pyfly.scheduling.adapters.asyncio_executor import AsyncIOTaskExecutor


@service
class DataSyncService:
    """Pulls data from an upstream API with a guaranteed gap between runs."""

    @scheduled(fixed_delay=timedelta(minutes=5))
    async def sync_upstream(self):
        print("Starting data sync...")
        # Simulate work
        import asyncio
        await asyncio.sleep(2)
        print("Data sync complete.")


@service
class CleanupService:
    """Purges stale records every night at 02:00."""

    @scheduled(cron="0 2 * * *")
    async def purge_stale_records(self):
        print("Running nightly cleanup...")
        # Delete records older than 90 days
        await self.repository.delete_older_than(days=90)
        print("Cleanup done.")


@service
class HealthMonitor:
    """Publishes a heartbeat every 10 seconds, starting after a 5-second delay."""

    @scheduled(fixed_rate=timedelta(seconds=10), initial_delay=timedelta(seconds=5))
    async def heartbeat(self):
        print("Heartbeat: OK")


# With auto-configuration (recommended), no SchedulerManager is needed.
# The ApplicationContext automatically discovers @scheduled methods
# and manages the TaskScheduler lifecycle.
#
# If you need a manual SchedulerManager (e.g., for custom executor):
@service
class SchedulerManager:
    """Manages the lifecycle of the TaskScheduler (manual approach)."""

    def __init__(
        self,
        sync_service: DataSyncService,
        cleanup_service: CleanupService,
        health_monitor: HealthMonitor,
    ):
        self._scheduler = TaskScheduler()  # Uses AsyncIOTaskExecutor by default
        self._beans = [sync_service, cleanup_service, health_monitor]

    @post_construct
    async def start(self):
        count = self._scheduler.discover(self._beans)
        print(f"Discovered {count} scheduled tasks")
        await self._scheduler.start()

    @pre_destroy
    async def stop(self):
        await self._scheduler.stop()
        print("Scheduler stopped.")
```

### Using CronExpression Standalone

You can also use `CronExpression` independently for any cron-related
calculation:

```python
from datetime import datetime
from pyfly.scheduling import CronExpression

# When is the next weekday at 09:00?
cron = CronExpression("0 9 * * 1-5")
print(f"Next working-day start: {cron.next_fire_time()}")
print(f"Seconds to wait: {cron.seconds_until_next():.0f}")

# Show the next 5 fire times
for t in cron.next_n_fire_times(5):
    print(f"  {t}")

# What was the last fire time?
print(f"Previous fire: {cron.previous_fire_time()}")
```

### Custom Executor

Implementing a custom executor is straightforward -- just satisfy the
`TaskExecutorPort` protocol:

```python
import asyncio
import logging
from typing import Any, Coroutine, TypeVar

from pyfly.data.transaction import detached
from pyfly.scheduling import TaskExecutorPort

T = TypeVar("T")
logger = logging.getLogger(__name__)


class LoggingTaskExecutor:
    """Custom executor that logs every task submission."""

    def __init__(self):
        self._tasks: set[asyncio.Task[Any]] = set()

    async def submit(self, coro: Coroutine[Any, Any, T]) -> asyncio.Task[T]:
        logger.info("Submitting task: %s", coro.__qualname__)
        task = detached(coro)          # outside the submitter's unit of work
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    async def start(self) -> None:
        pass  # Ready after construction

    async def stop(self) -> None:
        tasks = list(self._tasks)
        try:
            await asyncio.gather(*tasks, return_exceptions=True)
        except asyncio.CancelledError:  # the stop itself was cancelled: cancel and await the tasks
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise
        finally:
            self._tasks.clear()


# Use it with the scheduler
scheduler = TaskScheduler(executor=LoggingTaskExecutor())
```

This architecture makes it easy to plug in metrics collection, distributed
execution, or any other cross-cutting concern without modifying your scheduled
tasks.
