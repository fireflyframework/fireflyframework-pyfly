# Event Sourcing

`pyfly.eventsourcing` is a port of `org.fireflyframework.eventsourcing`.
Aggregates emit `DomainEvent`s; an `EventStore` persists them; a
repository replays the stream to reconstruct state; a snapshot store
truncates replay cost; a `TransactionalOutbox` provides at-least-once
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

```python
from pyfly.eventsourcing import TransactionalOutbox

async def publish(envelope):
    await broker.publish(envelope)

outbox = TransactionalOutbox(publish=publish, max_attempts=5)
await outbox.start()
await outbox.enqueue(envelope_for_event)
```

## Projections

```python
from pyfly.eventsourcing.projection import FunctionProjection, ProjectionRunner

async def read_model(envelope):
    await db.upsert("orders_view", envelope)

runner = ProjectionRunner(FunctionProjection("orders_view", read_model), store)
await runner.start()
```
