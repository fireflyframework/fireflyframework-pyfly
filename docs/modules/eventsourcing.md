# Event Sourcing

`pyfly.eventsourcing` is a port of `org.fireflyframework.eventsourcing`.
Aggregates emit `DomainEvent`s; an `EventStore` persists them; a
repository replays the stream to reconstruct state; a snapshot store
truncates replay cost; a table-backed `TransactionalOutbox` provides transactional, at-least-once
delivery to a broker; `ProjectionRunner` updates read models from the
store's global stream, keeping its place in a `CheckpointStore`.

## Defining an aggregate

```python
from dataclasses import dataclass
from pyfly.eventsourcing import AggregateRoot, DomainEvent

@dataclass
class OrderPlaced(DomainEvent):
    order_id: str = ""
    amount: int = 0

class Order(AggregateRoot):
    def __init__(self) -> None:
        super().__init__()
        self.amount = 0
        self.when(OrderPlaced, lambda agg, evt: setattr(agg, "amount", evt.amount))
```

`AggregateRoot._dispatch` routes each event to its registered handler
in the following order:

1. A handler registered via `when(EventType, fn)`.
2. A method named `on_{event_type}` on the aggregate class.
3. If neither exists, `EventHandlerException` is raised — a missing
   handler would silently corrupt reconstructed state, so the aggregate
   fails loudly rather than swallowing the event.

The `version` counter is incremented after each successfully dispatched
event regardless of which of the two handler paths was used.

## Saving and loading

```python
from pyfly.eventsourcing import (
    InMemoryEventStore, InMemorySnapshotStore,
)
from pyfly.eventsourcing.repository import EventSourcedRepository

store = InMemoryEventStore()
snapshots = InMemorySnapshotStore()
repo = EventSourcedRepository(store, factory=Order, snapshots=snapshots)

order = Order()
order.id = "o-1"
order.apply(OrderPlaced(order_id="o-1", amount=99))
await repo.save(order)

# Restart, then:
recovered = await repo.load("o-1")
assert recovered.amount == 99
```

`save` appends the aggregate's pending events with the version it was loaded at as the expected version. When
another writer appended to the aggregate since, the store raises `ConcurrencyError`, which is the kernel's
`OptimisticLockingFailureException` (HTTP 409, and a transient failure a message listener retries): reload the
aggregate and retry the command.

### Events, snapshots and the business transaction

The SQL stores run every call through the unit of work of their datasource
(`pyfly.data.transaction.infrastructure_unit`). Inside `@transactional` (or any unit of work on that
datasource) an aggregate's events, its snapshot and the rest of the business transaction's writes commit or roll
back together; outside one, each call is a short transaction of its own.

```python
@transactional
async def place(self, command: PlaceOrder) -> None:
    order = Order.place(command)
    await self.orders_view.save(OrderSummary.of(order))   # a repository write
    await self.order_events.save(order)                    # its events, and a snapshot when one is due
    # an exception here rolls back the summary, the events and the snapshot
```

Inside the unit, `load()` sees the unit's own events; the global stream (`stream_all`) shows them once the unit
has committed. When the unit rolls back, the aggregate object still counts its events as committed: load it again
before retrying.

### Envelope validation on load

`EventSourcedRepository.load` validates every replayed envelope:

* If `envelope.aggregate_id` does not match the requested aggregate ID,
  `EventHandlerException` is raised — this indicates a store bug or
  cross-aggregate data corruption.
* If `envelope.aggregate_type` is set and does not match the aggregate's
  class name, `EventHandlerException` is raised.

### Snapshot interval crossing

Snapshots are taken when saving a batch **crosses** a multiple of
`snapshot_interval` (default `100`), rather than on exact divisibility.
This handles the case where a single batch straddles the threshold:

```python
# batch pushes version from 95 to 105: crosses the 100 boundary → snapshot taken
crossed_interval = (aggregate.version // snapshot_interval) > (previous_version // snapshot_interval)
```

## Outbox pattern

`TransactionalOutbox` is a table-backed transactional outbox (the framework table
`pyfly_outbox_events`, see [the transactional outbox](events.md#the-transactional-outbox-postgres-and-database)).
`enqueue()` writes the event in the unit of work bound for the outbox's datasource: enqueued in the unit that
appends the events or saves the aggregate, it exists exactly when that unit commits, and a rollback takes it
back. A relay (a `CONSUMER_PHASE` lifecycle bean) hands every committed event to `publish`, attempts a failed
one again after a back-off, keeps it in the dead letters after `max_attempts`, and prunes delivered events.
Pending events survive a restart, and several processes may run the same outbox: each event goes to one.

```python
from pyfly.eventsourcing import TransactionalOutbox

async def publish(envelope):
    await broker.publish(envelope)

outbox = TransactionalOutbox(publish=publish, datasource="primary", max_attempts=5)
await outbox.start()

@transactional
async def open_account(command):
    ...
    await outbox.enqueue(envelope_for_event)   # published once this unit commits

await outbox.pending()        # not delivered yet
await outbox.dead_letters()   # failed on every attempt
```

| Argument | Default | Meaning |
|---|---|---|
| `datasource` | the default datasource | Where the outbox table lives: a name, a `DataSource` or an `AsyncEngine`. |
| `name` | `default` | Outboxes with different names on one datasource deliver apart. |
| `max_attempts` | `5` | Attempts before an event goes to the dead letters. |
| `backoff` | exponential, 1 s to 30 s | The delay before the next attempt of a failed event. |
| `poll_interval_s` | `1.0` | How often the relay looks for events enqueued by other processes; an enqueue in this process wakes it when its unit commits. |
| `publish_timeout` | `60.0` | Seconds a publish may take before the attempt counts as failed. |
| `create_tables` | `True` | Create the outbox tables when they are missing (otherwise only check them). |
| `store` | a `SqlOutboxStore` on `datasource` | The outbox store to run on instead: any `OutboxStore` (`pyfly.eda.ports.outbox`); then `datasource`, `tables` and `create_tables` do not apply. |

Before 26.09.08 the outbox was a dictionary in the process: an event enqueued by a unit that then rolled back
was published anyway, a restart lost everything pending, and nothing was ever removed. It now needs a data
layer: an application without one gets `IllegalTransactionStateError` from `start()`. A delivered event leaves
the table rather than being flagged: `OutboxRecord.delivered` stays in the record for compatibility and is always
`False` in what `pending()` and `dead_letters()` return.

## Projections

A `ProjectionRunner` feeds the events of the store's global stream to a projection, in order, in batches:

```python
from pyfly.eventsourcing import FunctionProjection, ProjectionRunner

async def summarize(envelope):
    await summaries.apply(envelope)          # repository calls on the checkpoints' datasource join the batch

runner = ProjectionRunner(
    FunctionProjection("orders_view", summarize),
    store,                                   # the EventStore bean
    checkpoints=checkpoints,                 # the CheckpointStore bean
)
await runner.start()                         # or declare the runner as a bean: the context starts and stops it
```

### The global stream

Every event gets a **global position** on the store's global stream, and `stream_all(after_position=p,
limit=n)` returns the committed events after position `p`, in position order, each with its
`global_position` set. Positions only move forward for a reader: once it has seen position `p`, no event appears
below it later, and an aggregate's events are on the stream in sequence order (except the events an earlier
release stored: see [Upgrading from 26.09.07](#upgrading-from-260907)). A projection therefore keeps one
number as its place, and never skips an event whose transaction committed late, never gets an event twice from
paging, and never stalls on events that share a timestamp. `occurred_at` is the event's data (the clock of the
process that built it), never a cursor. The order across aggregates depends on the
[position strategy](#position-strategies): with the default one an event appended after another one committed
comes after it on the stream.

`stream_all(after_event_id=...)`, the cursor of earlier releases, still works: it pages from that event's
position, and raises `ValueError` for an id that is not on the stream (it used to return nothing, forever).
`last_position()` is the position of the last event on the stream now; with `head-row` it first numbers the
events that were waiting when it was called (not those that commit meanwhile, so it returns under any write
rate). An `EventStore` written against the earlier SPI (a `stream_all` without `after_position`) is still
projected, with its place kept in memory by event id, without checkpoints or a lease (a
`projection_store_without_positions` WARNING says so).

### Checkpoints

A `CheckpointStore` keeps the position each projection (by its `name`) has applied the stream up to. The runner
loads it when it starts and moves it with every batch, so a restart or a deploy resumes where the last batch
ended instead of replaying the whole store. Without `checkpoints`, the runner keeps its position in memory and a
new runner starts from the beginning, on every replica; over a durable store (`SqlAlchemyEventStore`) its start
logs a `projection_checkpoints_in_memory` WARNING.

The `projection_checkpoint_store` bean follows the event store's provider unless
`pyfly.eventsourcing.projection.checkpoint.provider` is set. It runs on
`pyfly.eventsourcing.projection.checkpoint.datasource` (or `.url`), else on the primary datasource, else (an
application whose only database is the event store's `url` or `datasource`) on the event store's datasource.
Following the event store's provider, it creates or checks its tables on first use, so an application with no
projection needs neither `pyfly_projection_checkpoints` nor `pyfly_locks`; with the provider set, it does so at
start.

With `SqlAlchemyCheckpointStore` (the table `pyfly_projection_checkpoints`) each batch is one unit of work on
the checkpoints' datasource:

1. the checkpoint moves from the position the batch started at to the batch's last position, with a conditional
   `UPDATE ... WHERE position = :expected` (the row stays locked until the unit ends);
2. the projection handles the batch's events; its repository calls, `@transactional` methods and
   `infrastructure_unit()` calls on the same datasource join the unit;
3. the unit commits the read model's writes and the checkpoint together, or rolls both back.

A read model on the checkpoints' datasource therefore gets **every event exactly once**, across restarts,
failures and replicas. Effects elsewhere (another database, a broker, an email) happen **at least once**: a batch
that fails after one runs again. `InMemoryCheckpointStore` keeps the positions in the process (tests and
single-process development); it is not transactional, so a failed batch keeps what it applied before its
failure.

### One active replica

Every replica may run the same runner. With a lease, only the runner that holds the projection's lease
(`pyfly.projection.<name>`) applies events; the others poll the lease and take over when the holder stops or its
lease runs out. By default the runner takes the lease the checkpoint store offers: `SqlAlchemyCheckpointStore`
gives a `LeaseLock` on the portable lease table `pyfly_locks` of its datasource (it creates or checks that table
with its own). Pass `lease=` to use another
(anything with `try_acquire`, `extend` and `release`), or `lease=False` for none. A lease lasts `lease_ttl_s`
(30 s) and the runner renews it after a third of that; a runner that loses it stops applying events and reloads
the checkpoint when it takes it again. The checkpoint is fenced on its own too: a batch whose starting position
another runner has moved meanwhile applies nothing, and its runner carries on from where the checkpoint is.

### Throughput and failures

- **Full speed while behind.** The runner reads the next page as soon as a full one is applied, and sleeps
  `poll_interval_s` (1 s) only on a short page, once it has caught up. `batch_size` (100) is the page and the
  batch, of any size: a page read numbers as many committed events as the page needs, so a page is short only
  when the stream has nothing more. Before 26.09.08 the runner slept after every page, 100 events a second at the
  defaults.
- **In order, never past a failure.** A handler that raises stops its batch. The events before it are applied
  (in a batch of their own when the failed batch rolled back), and the failed event is retried after
  `poll_interval_s` until it succeeds; `projection_event_failed` is logged at ERROR with the event's id. The
  events of a batch whose commit fails are retried one at a time until the runner is past them, which finds the
  event that breaks it and lets the others through.
- **Detached.** The runner works in a task of its own, outside any unit of work of the code that started it
  (`pyfly.data.transaction.detached`). It is a lifecycle bean of the consumer phase: it stops, finishing the batch
  in flight, before any `@pre_destroy`.

### New projections and rebuilds

`start_from="latest"` makes a projection that has no checkpoint yet start after the events appended before the
runner starts, instead of applying the whole history; use it for a projection whose read model already reflects
the history (a rollout of checkpoints under a projection that used to replay on every start).

A rebuild is explicit: stop the projection's runners, clear its read model, reset its checkpoint and start the
runners again.

```python
await checkpoints.reset("orders_view")            # position 0: the next runner replays the whole stream
await checkpoints.reset("orders_view", position=await store.last_position())   # skip the history instead
```

`await checkpoints.position("orders_view")` reads the checkpoint without creating it: compare it with
`store.last_position()` to monitor a projection's lag.

## Durable event store providers

The event store adapter is chosen via `pyfly.eventsourcing.store.provider`
(default `memory`).

| Value | Class | Durable | Notes |
|-------|-------|---------|-------|
| `memory` | `InMemoryEventStore` | No | Default; no extra deps. |
| `sqlalchemy` | `SqlAlchemyEventStore` | Yes | Requires `sqlalchemy[asyncio]` + async driver. |

### Memory

The default; suitable for development and tests. All events are lost on
process restart.

### SQLAlchemy

```yaml
pyfly:
  eventsourcing:
    store:
      provider: sqlalchemy
      datasource: events                            # optional: a named datasource
      # url: postgresql+asyncpg://user:pass@host/db # or a URL (not both)
      # position-strategy: auto                     # auto (head-row for a new table) | head-row | xid8
```

The store runs on a datasource of the context's
[datasource registry](data-relational.md#module-datasources), which builds every engine:

- `pyfly.eventsourcing.store.datasource` names one (`primary` or a datasource under
  `pyfly.data.relational.datasources`).
- `pyfly.eventsourcing.store.url` is an alias resolved through the registry: a URL identical to a registered
  datasource's (the password aside) reuses that datasource's engine, so the event store and the repositories
  share one pool; another URL registers the `event-store` datasource, with the same pool settings, connect
  arguments, SQLite setup and credential hook as the primary. The registry disposes it on shutdown.
- With neither, the store uses the primary datasource (`pyfly.data.relational.url`). With no primary either,
  startup fails with a `DataSourceConfigurationError` that names the keys. Before 26.09.08 it silently opened
  `sqlite+aiosqlite:///./app.db`.

Setting both `datasource` and `url` is refused. A store on another datasource than the business transaction's
writes its events in a transaction of its own: put the events where the aggregates' other tables are when they
must commit together.

`SqlAlchemyEventStore` keeps the events in the framework table `pyfly_event_store` and the last position it gave
out in `pyfly_event_store_head` (one row per event table), both declared on the framework metadata
(`pyfly.data.relational.framework_schema`: `event_store_table()`, `event_store_head_table()`; list
`framework_metadata` in Alembic's `target_metadata`). The context starts the store: it creates the tables when
`pyfly.data.relational.ddl-auto` allows it (`create`, `create-drop`, `update`) and otherwise only checks them,
failing the startup with a `FrameworkSchemaError` that names what is missing. A store built by hand starts on
first use, or with `await store.start()`. On SQLite a store whose tables do not exist yet cannot create them when
it is first used inside a unit of work that holds the write lock (until the unit ends): start it before the unit,
as the `FrameworkSchemaError` then says.

| Column | Type | |
|--------|------|-|
| `event_id` | `VARCHAR(64)` | primary key |
| `aggregate_id`, `aggregate_type`, `event_type` | `VARCHAR(255)` | compared exactly on every backend |
| `sequence` | `INTEGER` | the aggregate's version; `UNIQUE (aggregate_id, sequence)` |
| `payload`, `metadata` | `TEXT` (`LONGTEXT` on MySQL/MariaDB) | the envelope's JSON, its metadata's JSON |
| `occurred_at` | UTC instant | the envelope's `occurred_at` |
| `version`, `tenant_id` | `INTEGER`, `VARCHAR(64)` | |
| `recorded_at` | UTC instant | when the database recorded the event, by its clock |
| `global_position` | `BIGINT`, unique | the event's place on the global stream |

An append reads the aggregate's version inside its unit, compares it with the expected version, and sends every
event in one `INSERT`; the `UNIQUE (aggregate_id, sequence)` constraint turns a concurrent writer's race into a
`ConcurrencyError`.

#### Position strategies

The first store that starts on an event table records the table's strategy in its head row. `auto` (the
default) follows what the table recorded, and records `head-row` for a new table, on every backend; an explicit
strategy that differs from the recorded one is refused at start, since two strategies on one table would let
readers skip events. Changing a table's strategy is a migration with every writer stopped.

- **`head-row`** (every backend; the default). An event is inserted without a position. Reading the stream first
  numbers the events that have committed since the last read (as many as the page needs), in short `READ
  COMMITTED` units of their own that lock the head row: a position only ever goes to a committed event, above
  every position given before, and the positions follow one another without gaps (unless events are deleted).
  SQLite has no row locks (it ignores `FOR UPDATE`), so there a numbering unit takes the database's write lock
  before it reads the head row: the datasource registry's engines begin every write unit with `BEGIN IMMEDIATE`,
  and on an engine built by hand, whose driver defers `BEGIN` until the first write, the unit begins with it
  itself. On every backend a unit moves the head row on only from the position it read
  (`UPDATE ... WHERE position = :read`); one that finds it moved (something wrote it without the lock) is rolled
  back and runs again, logging `event_store_head_row_moved` at WARNING, and after five such units in a row the
  read fails with a `ConcurrencyException` rather than give out a position twice.
  The events of one round are ordered by `recorded_at` (one clock, the database's), then by aggregate and
  sequence, so an aggregate's events keep their order and an event appended after another one committed comes
  after it. On SQLite `recorded_at` counts milliseconds: two events of different aggregates recorded in the same
  millisecond and numbered in the same round are ordered by aggregate id. No index serves that order, so a
  backlog (events no reader has read for a while, or an earlier release's) is sorted once per 100 000 events,
  and the rounds number that list in turn. An append never touches the head row: business transactions never
  wait for one another there, and none fails on it under snapshot isolation (MariaDB 11's `REPEATABLE READ`,
  PostgreSQL's). The reader needs write access to the tables; on SQLite a reader that numbers queues for the
  write lock like any writer (up to `busy_timeout`, 5 s by default, then `database is locked`). A read inside
  a unit of work on the store's datasource shows only what is already numbered: a reader that only ever runs in
  one (a `@transactional(read_only=True)` endpoint listing recent events) sees new events once a reader outside
  one (a projection runner, `last_position()`) has numbered them. What a read costs: one that finds new events to
  number runs a probe, a numbering unit and the page read, where 26.09.07 ran one `SELECT` that sorted the whole
  table. On PostgreSQL 17 (in a container on a laptop), reading the stream after appending one event took 4.7 ms
  against 26.09.07's 1.3 ms with 1 000 events stored, and 5.6 ms against 5.3 ms with 50 000. A reader behind a
  numbered page (a projection catching up) reads it without numbering.
- **`xid8`** (PostgreSQL 13 or later; opt-in, an accelerator whose reads write nothing). An event's position is
  its writer's transaction id times 2^20 plus its place among that transaction's events, set as it is inserted;
  a reader sees only the positions below its snapshot's horizon (`pg_snapshot_xmin(pg_current_snapshot())`).
  Every transaction below the horizon has ended, so nothing can appear below what a reader has seen. A server
  without these functions (one that speaks PostgreSQL's protocol) refuses `xid8` at start. The stream follows the
  order in which the writers took their transaction ids, not the order they committed in. An aggregate's order
  is kept by a guard: an append whose transaction's id is below the id of an event the aggregate already has (a
  unit that wrote something first, then appended to an aggregate another unit had appended to and committed
  meanwhile) raises `ConcurrencyError`, and the command runs again in a new unit, with a new id. The order across
  aggregates is not kept: an event appended after reading another aggregate's committed event can stream before
  that event, so a projection that relates aggregates needs `head-row`. And a transaction left open anywhere on
  the server (idle in transaction) holds the stream back until it ends: delivery waits, nothing is skipped; keep
  `idle_in_transaction_session_timeout` set.

#### Upgrading from 26.09.07

Earlier releases created `pyfly_event_store` with `occurred_at TIMESTAMP` and no global position, and ordered the
stream by `occurred_at`. The store refuses such a table at start (`pyfly_event_store.global_position does not
exist`). Stop the writers of the earlier release (an event they insert later has no position; on an `xid8` table
the next start places it below what projections may have passed, with an `event_store_positions_below_readers`
WARNING), then add the columns (and on PostgreSQL convert the timestamps to UTC instants):

```sql
-- PostgreSQL
ALTER TABLE pyfly_event_store ADD COLUMN recorded_at TIMESTAMP WITH TIME ZONE NULL;
ALTER TABLE pyfly_event_store ADD COLUMN global_position BIGINT NULL;
ALTER TABLE pyfly_event_store ALTER COLUMN occurred_at TYPE TIMESTAMP WITH TIME ZONE
    USING occurred_at AT TIME ZONE 'UTC';
ALTER TABLE pyfly_snapshots ALTER COLUMN created_at TYPE TIMESTAMP WITH TIME ZONE
    USING created_at AT TIME ZONE 'UTC';

-- MySQL and MariaDB
SET time_zone = '+00:00';
ALTER TABLE pyfly_event_store ADD COLUMN recorded_at DATETIME(6) NULL,
    ADD COLUMN global_position BIGINT NULL, MODIFY occurred_at DATETIME(6) NOT NULL,
    MODIFY payload LONGTEXT NOT NULL, MODIFY metadata LONGTEXT NOT NULL;

-- SQLite
ALTER TABLE pyfly_event_store ADD COLUMN recorded_at DATETIME;
ALTER TABLE pyfly_event_store ADD COLUMN global_position BIGINT;
```

With migrations owning the schema (`ddl-auto: none`), also create the head table `pyfly_event_store_head` and the
index (`CREATE UNIQUE INDEX ix_pyfly_event_store_global_position ON pyfly_event_store (global_position)`), and,
for projections with checkpoints, the tables of the `projection_checkpoint_store` bean:
`pyfly_projection_checkpoints` and the lease table `pyfly_locks`. Alembic autogenerates all four from
`framework_metadata`; otherwise the stores create them when they start (the checkpoint store, when it only
follows the event store's provider, on first use).

The events already stored have no global position yet. They get theirs oldest `occurred_at` first, before any
event appended since. `occurred_at` is the clock of the process that built the event, and 26.09.07 ordered its
stream by it: where the clocks of those processes disagreed, an aggregate's earlier event is placed after its
later one, as that release streamed them. With `head-row` they are numbered as the stream is read (a start does
not wait for them; a projection's page read numbers what its page needs, and `last_position()`, which a runner
with `start_from="latest"` calls, numbers all of them before it returns), with `xid8` at start. The store sorts
such a backlog once per 100 000 events and numbers it in rounds of 1000, at roughly 10 000 to 30 000 events a
second: in containers on a laptop, a million events took 54 s on PostgreSQL 17 and 93 s on MySQL 8 (100 000 took
3 s on both). A large table can be numbered in one statement instead, before the upgraded application first
starts, while no event has a position yet (the set-based `UPDATE` took 18 to 39 s for a million events on
PostgreSQL and 110 s on MySQL, in one transaction):

```sql
-- PostgreSQL, and SQLite 3.33 or later
UPDATE pyfly_event_store AS e SET global_position = n.position
FROM (SELECT event_id, ROW_NUMBER() OVER (ORDER BY occurred_at, aggregate_id, sequence) AS position
      FROM pyfly_event_store) AS n
WHERE e.event_id = n.event_id;

-- MySQL 8 and MariaDB 10.2 or later
UPDATE pyfly_event_store AS e
JOIN (SELECT event_id, ROW_NUMBER() OVER (ORDER BY occurred_at, aggregate_id, sequence) AS position
      FROM pyfly_event_store) AS n ON e.event_id = n.event_id
SET e.global_position = n.position;

-- Every backend: the head row (when a store has created it already) takes the positions on from there
UPDATE pyfly_event_store_head SET position = (SELECT MAX(global_position) FROM pyfly_event_store)
WHERE store = 'pyfly_event_store';
```

Projections built by the earlier runner replayed the store at every start: give them `start_from="latest"` (or
reset their checkpoint to `last_position()`) on the first start with checkpoints, so they do not replay it once
more.

## Durable snapshot store providers

The snapshot store adapter is chosen via `pyfly.eventsourcing.snapshot.provider`
(default `memory`).

| Value | Class | Durable | Notes |
|-------|-------|---------|-------|
| `memory` | `InMemorySnapshotStore` | No | Default; no extra deps. |
| `sqlalchemy` | `SqlAlchemySnapshotStore` | Yes | Requires `sqlalchemy[asyncio]` + async driver. |

### SQLAlchemy snapshot store

```yaml
pyfly:
  eventsourcing:
    snapshot:
      provider: sqlalchemy
      datasource: events                            # optional; or url; the primary datasource when neither
```

The snapshot store resolves its datasource like the event store: `pyfly.eventsourcing.snapshot.datasource`
names one, `pyfly.eventsourcing.snapshot.url` is an alias resolved through the registry (another URL registers
the `snapshot-store` datasource), and with neither it is the primary (no primary is a startup error naming the
keys).

`SqlAlchemySnapshotStore` keeps the latest snapshot of each aggregate in the framework table `pyfly_snapshots`
(`aggregate_id` key, `aggregate_type`, `sequence`, the JSON `payload`, and `created_at`, a UTC instant), created
at start when `ddl-auto` allows it and checked otherwise.

A save is the dialect's conditional upsert: a snapshot replaces the stored one only when its `sequence` is
**newer** (`ON CONFLICT ... DO UPDATE ... WHERE` on PostgreSQL and SQLite, one statement; on MySQL and MariaDB an
`INSERT ... ON DUPLICATE KEY UPDATE` and a conditional `UPDATE`). An older snapshot never overwrites a newer one
under concurrent saves. Saves, loads and deletes join the ambient unit of work, as the event store's calls do.

## EDA bridge — EventSourcingPublisher

`EventSourcingPublisher` forwards stored-event envelopes onto the EDA bus.
It is wired automatically when an `EventPublisher` bean is present in the
application context; when EDA is not configured the bean is absent (returns
`None`) and is silently skipped.

```yaml
pyfly:
  eventsourcing:
    eda:
      destination: pyfly.events   # default
```

`pyfly.eventsourcing.eda.destination` sets the routing destination (topic /
exchange / subject). Each envelope is published with headers carrying
`aggregate_id`, `aggregate_type`, `sequence`, `version`, and optionally
`tenant_id`. String-valued entries from `StoredEventEnvelope.metadata` are
also promoted to headers.

Usage:

```python
from pyfly.eventsourcing.publisher import EventSourcingPublisher

# Inject from the DI container (created automatically when EDA is active):
publisher: EventSourcingPublisher = container.get(EventSourcingPublisher)

await publisher.publish(envelope)
await publisher.publish_all(envelopes)
```

## Auto-configuration

`EventSourcingAutoConfiguration` activates when
`pyfly.eventsourcing.enabled=true` and wires the following beans:

| Bean | Type | Description |
|------|------|-------------|
| `event_store` | `EventStore` | Adapter selected by `pyfly.eventsourcing.store.provider`. |
| `snapshot_store` | `SnapshotStore` | Adapter selected by `pyfly.eventsourcing.snapshot.provider`. |
| `projection_checkpoint_store` | `CheckpointStore` | Adapter selected by `pyfly.eventsourcing.projection.checkpoint.provider`; pass it to `ProjectionRunner(checkpoints=...)`. |
| `event_sourcing_publisher` | `EventSourcingPublisher \| None` | EDA bridge; `None` when no `EventPublisher` bean is present. |

The SQL stores are lifecycle beans: the context starts them (creating or checking their tables; the checkpoint
store that only follows the event store's provider leaves that to its first use) before the consumers, and every
store resolves its datasource in the context's `DataSourceRegistry` bean.

## Configuration reference

| Key | Default | Description |
|-----|---------|-------------|
| `pyfly.eventsourcing.enabled` | `false` | Enable the event-sourcing module. |
| `pyfly.eventsourcing.store.provider` | `memory` | Event store backend: `memory` or `sqlalchemy`. |
| `pyfly.eventsourcing.store.datasource` | *(none)* | The datasource the event store runs on (`primary` or a named datasource). Not with `url`. |
| `pyfly.eventsourcing.store.url` | *(none)* | Async SQLAlchemy URL for the event store. None (and no `datasource`): the primary datasource (no primary is a startup error). The same URL as a registered datasource reuses its engine; another URL registers the `event-store` datasource. |
| `pyfly.eventsourcing.store.position-strategy` | `auto` | How global positions are given out: `auto` (what the table recorded; `head-row` for a new table), `head-row` or `xid8` (PostgreSQL, opt-in; see [Position strategies](#position-strategies)). |
| `pyfly.eventsourcing.snapshot.provider` | `memory` | Snapshot store backend: `memory` or `sqlalchemy`. |
| `pyfly.eventsourcing.snapshot.datasource` | *(none)* | The datasource the snapshot store runs on. Not with `url`. |
| `pyfly.eventsourcing.snapshot.url` | *(none)* | Async SQLAlchemy URL for the snapshot store. None (and no `datasource`): the primary datasource (no primary is a startup error). The same URL as a registered datasource reuses its engine; another URL registers the `snapshot-store` datasource. |
| `pyfly.eventsourcing.projection.checkpoint.provider` | the event store's | Checkpoint store backend: `memory` or `sqlalchemy`. Set, the SQL store checks its tables at start; following the event store's, on first use. |
| `pyfly.eventsourcing.projection.checkpoint.datasource` | *(none)* | The datasource of the checkpoints: the read models' datasource. Not with `url`. None (and no `url`): the primary, or the event store's datasource when there is no primary. |
| `pyfly.eventsourcing.projection.checkpoint.url` | *(none)* | Async SQLAlchemy URL for the checkpoints, resolved like the others (another URL registers the `projection-checkpoints` datasource). |
| `pyfly.eventsourcing.eda.destination` | `pyfly.events` | EDA routing destination for `EventSourcingPublisher`. |

## Testing

The in-memory adapters and the runner's paging, failure policy and SPI compatibility are covered in
`tests/eventsourcing/`. Every SQL adapter runs on every relational lane of the backend matrix (SQLite file in
the fast suite; PostgreSQL, MySQL 8 and MariaDB 11 with `-m integration`; PostgreSQL runs the event store and
projection scenarios once per position strategy):

- `tests/integration/test_event_store_matrix.py`: commit order, ties, a transaction committing late, concurrent
  appends, an aggregate appended to across units, the business transaction's rollback (proof p10), one `INSERT`
  per append, the upgrade of an earlier release's table, a backlog sorted once per window (with another store
  numbering part of it meanwhile, and a round failing), the set-based numbering SQL;
- `tests/integration/test_snapshot_store_matrix.py`: the conditional upsert, UTC instants, snapshots and events
  committing together;
- `tests/integration/test_projection_matrix.py`: restart without replay, two replicas on one non-idempotent
  read model, a handler failing between its two writes, a batch failing at commit, a late-committing writer, an
  aggregate's events in order across units, catch-up of 1000 events at the default settings and of 5000 with
  pages larger than a numbering round, rebuilds;
- `tests/integration/test_eventsourcing_postgres_integration.py`: the `xid8` horizon, events left without a
  position on an `xid8` table, and the PostgreSQL upgrade.

```
uv run pytest tests/eventsourcing tests/integration/test_event_store_matrix.py -q        # SQLite lane
PYFLY_INTEGRATION_REQUIRE_DOCKER=1 uv run pytest -m integration \
    tests/integration/test_event_store_matrix.py tests/integration/test_projection_matrix.py -q
```
