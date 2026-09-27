# Event Sourcing

`pyfly.eventsourcing` is a port of `org.fireflyframework.eventsourcing`.
Aggregates emit `DomainEvent`s; an `EventStore` persists them; a
repository replays the stream to reconstruct state; a snapshot store
truncates replay cost; a table-backed `TransactionalOutbox` provides transactional, at-least-once
delivery to a broker; `ProjectionRunner` updates read models.

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

Before 26.09.08 the outbox was a dictionary in the process: an event enqueued by a unit that then rolled back
was published anyway, a restart lost everything pending, and nothing was ever removed. It now needs a data
layer: an application without one gets `IllegalTransactionStateError` from `start()`.

## Projections

```python
from pyfly.eventsourcing.projection import FunctionProjection, ProjectionRunner

async def read_model(envelope):
    await db.upsert("orders_view", envelope)

runner = ProjectionRunner(FunctionProjection("orders_view", read_model), store)
await runner.start()
```

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
      url: postgresql+asyncpg://user:pass@host/db   # optional
```

The URL resolves through the
[datasource registry](data-relational.md#module-datasources), which builds every engine:

- With no `pyfly.eventsourcing.store.url`, the event store uses the primary datasource
  (`pyfly.data.relational.url`). With no primary either, startup fails with a
  `DataSourceConfigurationError` that names both keys. Before 26.09.08 it silently opened
  `sqlite+aiosqlite:///./app.db`.
- A URL identical to a registered datasource's (the password aside) reuses that datasource's engine,
  so the event store and the repositories share one pool.
- Another URL registers the `event-store` datasource, with the same pool settings, connect arguments,
  SQLite setup and credential hook as the primary. The registry disposes it on shutdown.

`SqlAlchemyEventStore` manages the table `pyfly_event_store`:

```sql
CREATE TABLE IF NOT EXISTS pyfly_event_store (
    event_id        VARCHAR(64) PRIMARY KEY,
    aggregate_id    VARCHAR(64) NOT NULL,
    aggregate_type  VARCHAR(255) NOT NULL,
    sequence        INTEGER NOT NULL,
    event_type      VARCHAR(255) NOT NULL,
    payload         TEXT NOT NULL,
    metadata        TEXT NOT NULL,
    occurred_at     TIMESTAMP NOT NULL,
    version         INTEGER NOT NULL,
    tenant_id       VARCHAR(64) NULL,
    UNIQUE (aggregate_id, sequence)
)
```

The `UNIQUE (aggregate_id, sequence)` constraint provides **optimistic
locking**: concurrent appenders with the same expected version race on the
constraint and the loser receives a `ConcurrencyError`. The version check
and the inserts happen inside a single database transaction so there is no
TOCTOU race; the unique constraint is an additional backstop against races
between concurrent writers.

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
      url: postgresql+asyncpg://user:pass@host/db   # optional; the primary datasource when absent
```

The snapshot URL resolves like the event store's: no URL means the primary datasource (and no primary
is a startup error naming both keys), an identical URL reuses that datasource's engine, and another URL
registers the `snapshot-store` datasource.

`SqlAlchemySnapshotStore` manages the table `pyfly_snapshots`:

```sql
CREATE TABLE IF NOT EXISTS pyfly_snapshots (
    aggregate_id   VARCHAR(64) PRIMARY KEY,
    aggregate_type VARCHAR(255) NOT NULL,
    sequence       INTEGER NOT NULL,
    payload        TEXT NOT NULL,
    created_at     TIMESTAMP NOT NULL
)
```

Snapshots are upserted: a new snapshot row replaces the existing one only
when its `sequence` is **newer** than what is already stored
(`WHERE pyfly_snapshots.sequence < EXCLUDED.sequence`). This prevents
older snapshots from overwriting newer ones under concurrent saves.

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
| `event_sourcing_publisher` | `EventSourcingPublisher \| None` | EDA bridge; `None` when no `EventPublisher` bean is present. |

## Configuration reference

| Key | Default | Description |
|-----|---------|-------------|
| `pyfly.eventsourcing.enabled` | `false` | Enable the event-sourcing module. |
| `pyfly.eventsourcing.store.provider` | `memory` | Event store backend: `memory` or `sqlalchemy`. |
| `pyfly.eventsourcing.store.url` | *(none)* | Async SQLAlchemy URL for the event store. None: the primary datasource (no primary is a startup error). The same URL as a registered datasource reuses its engine; another URL registers the `event-store` datasource. |
| `pyfly.eventsourcing.snapshot.provider` | `memory` | Snapshot store backend: `memory` or `sqlalchemy`. |
| `pyfly.eventsourcing.snapshot.url` | *(none)* | Async SQLAlchemy URL for the snapshot store. None: the primary datasource (no primary is a startup error). The same URL as a registered datasource reuses its engine; another URL registers the `snapshot-store` datasource. |
| `pyfly.eventsourcing.eda.destination` | `pyfly.events` | EDA routing destination for `EventSourcingPublisher`. |

## Testing

The unit test suite in `tests/eventsourcing/test_durable_adapters.py`
covers `SqlAlchemySnapshotStore` against an in-memory SQLite engine,
`EventSourcingPublisher` with a fake bus, and the provider-selection logic
in `EventSourcingAutoConfiguration`.

`SqlAlchemyEventStore` is additionally covered by a real-Postgres
integration test in
`tests/integration/test_eventsourcing_postgres_integration.py` that runs
against a Testcontainers Postgres instance (gated by `@requires_docker`):

```
PYFLY_INTEGRATION_REQUIRE_DOCKER=1 uv run pytest -m integration \
    tests/integration/test_eventsourcing_postgres_integration.py -q
```
