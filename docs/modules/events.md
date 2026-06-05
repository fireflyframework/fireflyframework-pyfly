# Events & Event-Driven Architecture Guide

PyFly provides first-class support for event-driven architecture (EDA) through
two complementary subsystems: **domain events** (the `pyfly.eda` module) for
business-level event publishing and consumption, and **application events** (the
`pyfly.context.events` module) for framework lifecycle notifications. This
guide covers both in depth.

---

## Table of Contents

1. [Architecture Overview](#architecture-overview)
2. [Domain Events vs. Application Events](#domain-events-vs-application-events)
3. [The EventEnvelope](#the-eventenvelope)
4. [ErrorStrategy Enum](#errorstrategy-enum)
5. [EventPublisher Protocol](#eventpublisher-protocol)
6. [EventConsumer Protocol](#eventconsumer-protocol)
7. [EventHandler Callable](#eventhandler-callable)
8. [InMemoryEventBus](#inmemoryeventbus)
9. [Declarative Decorators](#declarative-decorators)
   - [@event_publisher](#event_publisher)
   - [@publish_result](#publish_result)
   - [@event_listener](#event_listener)
10. [Application Events](#application-events)
    - [Built-in Lifecycle Events](#built-in-lifecycle-events)
    - [ApplicationEventBus](#applicationeventbus)
    - [@app_event_listener](#app_event_listener)
11. [Events vs. Messaging: When to Use Which](#events-vs-messaging-when-to-use-which)
12. [Complete Example: Order Domain Events](#complete-example-order-domain-events)
13. [Testing with InMemoryEventBus](#testing-with-inmemoryeventbus)

---

## Architecture Overview

The EDA module follows the same hexagonal principles as the rest of PyFly:

```
Application / Domain Services
          |
          v
   EventPublisher  (protocol / port)
          |
          +-- InMemoryEventBus   (single-process, local pub/sub)
          +-- (future adapters)  (Kafka-backed, Redis Streams, etc.)
```

Events are wrapped in an `EventEnvelope` that carries the payload alongside
metadata (type, ID, timestamp, headers). Subscriptions are pattern-matched, so
a listener for `"order.*"` automatically receives `"order.created"`,
`"order.shipped"`, and any other event whose type matches the glob pattern.

---

## Domain Events vs. Application Events

PyFly distinguishes between two categories of events:

| Aspect | Domain Events (`pyfly.eda`) | Application Events (`pyfly.context.events`) |
|--------|----------------------------|----------------------------------------------|
| **Purpose** | Business logic -- things that happen in your domain (order created, payment received). | Framework lifecycle -- context initialized, application ready, shutdown. |
| **Envelope** | `EventEnvelope` with `event_type`, `payload`, `destination`, `headers`, etc. | Subclasses of `ApplicationEvent` (plain Python objects). |
| **Bus** | `InMemoryEventBus` (or any `EventPublisher` implementation). | `ApplicationEventBus` (always in-process). |
| **Subscription** | Pattern-matched strings (`"order.*"`). | Type-matched Python classes (`ApplicationReadyEvent`). |
| **Typical consumers** | Domain services, projections, sagas. | Startup hooks, health checks, cleanup tasks. |

Use domain events for anything that represents a meaningful fact in your
business domain. Use application events for framework-level coordination.

---

## The EventEnvelope

Every domain event travels inside an `EventEnvelope` -- a frozen dataclass that
pairs the event payload with rich metadata.

```python
from pyfly.eda import EventEnvelope

envelope = EventEnvelope(
    event_type="order.created",
    payload={"order_id": "abc-123", "customer_id": "cust-42", "total": 99.99},
    destination="orders",
    headers={"correlation-id": "req-789"},
)

# Auto-generated fields
print(envelope.event_id)    # e.g. "a1b2c3d4-..."  (UUID v4)
print(envelope.timestamp)   # e.g. 2026-02-14 12:00:00+00:00  (UTC)
```

### Fields

| Field         | Type              | Default            | Description |
|---------------|-------------------|--------------------|-------------|
| `event_type`  | `str`             | *required*         | A dot-separated identifier for the event (e.g. `"order.created"`). Used for pattern matching in subscriptions. |
| `payload`     | `dict[str, Any]`  | *required*         | The event data. Must be a dictionary. |
| `destination` | `str`             | *required*         | The logical channel or topic this event is published to. |
| `event_id`    | `str`             | auto-generated UUID | A unique identifier for this specific event instance. |
| `timestamp`   | `datetime`        | `datetime.now(UTC)` | When the event was created. Always UTC. |
| `headers`     | `dict[str, str]`  | `{}`               | Arbitrary key-value metadata (correlation IDs, trace context, etc.). |

The dataclass is frozen, making envelopes immutable once created.

---

## ErrorStrategy Enum

`ErrorStrategy` defines how the system should behave when an error occurs
during event processing. It is an enum with five members:

```python
from pyfly.eda import ErrorStrategy
```

| Member              | Value              | Behavior |
|---------------------|--------------------|----------|
| `IGNORE`            | `"IGNORE"`         | Silently swallow the exception. Processing continues with the next handler/event. |
| `LOG_AND_CONTINUE`  | `"LOG_AND_CONTINUE"` | Log the error at warning/error level, then continue processing. |
| `RETRY`             | `"RETRY"`          | Re-attempt delivery of the event to the failed handler. Retry policy (count, backoff) is configured separately. |
| `DEAD_LETTER`       | `"DEAD_LETTER"`    | Move the failed event to a dead-letter destination for later inspection and reprocessing. |
| `FAIL_FAST`         | `"FAIL_FAST"`      | Immediately propagate the exception to the caller. No further handlers are invoked. |

Choose the strategy that matches your reliability requirements. For most
applications, `LOG_AND_CONTINUE` is a sensible default; for financial
transactions, `RETRY` or `DEAD_LETTER` may be more appropriate.

---

## EventPublisher Protocol

The `EventPublisher` is the primary outbound port for event-driven
communication. It is a `@runtime_checkable` `Protocol`.

```python
from pyfly.eda import EventPublisher

class EventPublisher(Protocol):
    def subscribe(self, event_type_pattern: str, handler: EventHandler) -> None: ...

    async def publish(
        self,
        destination: str,
        event_type: str,
        payload: dict[str, Any],
        headers: dict[str, str] | None = None,
    ) -> None: ...
```

| Method      | Description |
|-------------|-------------|
| `subscribe(event_type_pattern, handler)` | Register a handler for events matching the given pattern. Supports glob-style wildcards (`"order.*"`, `"*"`). |
| `publish(destination, event_type, payload, headers)` | Publish an event. The bus wraps the arguments in an `EventEnvelope` and delivers it to all matching subscribers. |

---

## EventConsumer Protocol

The `EventConsumer` protocol defines the lifecycle for components that receive
events from external sources:

```python
from pyfly.eda.ports.outbound import EventConsumer

class EventConsumer(Protocol):
    async def start(self) -> None: ...
    async def stop(self) -> None: ...
```

This protocol is primarily used by adapters that poll or listen on external
event sources (message brokers, streams) and is separate from the in-process
`EventPublisher`.

---

## EventHandler Callable

An `EventHandler` is a type alias for any async callable that accepts an
`EventEnvelope` and returns nothing:

```python
from pyfly.eda.ports.outbound import EventHandler

# Type definition:
# EventHandler = Callable[[EventEnvelope], Awaitable[None]]

async def my_handler(envelope: EventEnvelope) -> None:
    print(f"Received {envelope.event_type}: {envelope.payload}")
```

---

## InMemoryEventBus

The `InMemoryEventBus` is the built-in implementation of `EventPublisher`. It
runs entirely in-process and is suitable for monolithic applications,
development, and testing.

```python
from pyfly.eda import EventEnvelope
from pyfly.eda.adapters.memory import InMemoryEventBus

bus = InMemoryEventBus()
```

### Subscribing

Subscriptions use **glob-style pattern matching** powered by Python's
`fnmatch` module:

```python
# Exact match -- only "order.created"
async def on_created(envelope: EventEnvelope) -> None:
    print(f"Created: {envelope.payload}")

bus.subscribe("order.created", on_created)

# Wildcard -- matches "order.created", "order.shipped", "order.cancelled", etc.
async def on_any_order(envelope: EventEnvelope) -> None:
    print(f"Order event: {envelope.event_type}")

bus.subscribe("order.*", on_any_order)

# Catch-all
async def audit_log(envelope: EventEnvelope) -> None:
    print(f"[AUDIT] {envelope.event_type}")

bus.subscribe("*", audit_log)
```

### Publishing

```python
await bus.publish(
    destination="orders",
    event_type="order.created",
    payload={"order_id": "123", "customer_id": "abc"},
    headers={"source": "order-service"},
)
```

When you call `publish()`, the bus:

1. Creates an `EventEnvelope` with auto-generated `event_id` and `timestamp`.
2. Iterates over all registered `(pattern, handler)` pairs.
3. For each pair where `fnmatch.fnmatch(event_type, pattern)` is `True`,
   invokes the handler with the envelope.
4. Handlers are called sequentially in subscription order.

---

## Declarative Decorators

PyFly provides three decorators that reduce boilerplate for common event
patterns.

### @event_publisher

Automatically publishes the decorated method's **arguments** as an event. This
is useful when you want to broadcast the inputs to a method.

```python
from pyfly.eda import event_publisher
from pyfly.eda.adapters.memory import InMemoryEventBus

bus = InMemoryEventBus()

@event_publisher(bus, destination="orders", event_type="order.creating", timing="BEFORE")
async def create_order(customer_id: str, items: list[dict]) -> dict:
    order = {"customer_id": customer_id, "items": items, "status": "CREATED"}
    return order
```

#### Parameters

| Parameter     | Type               | Default    | Description |
|---------------|--------------------|------------|-------------|
| `bus`         | `InMemoryEventBus` | *required* | The event bus instance. |
| `destination` | `str`              | *required* | Topic or channel name. |
| `event_type`  | `str`              | *required* | The event type string. |
| `timing`      | `str`              | `"BEFORE"` | When to publish relative to function execution: `"BEFORE"`, `"AFTER"`, or `"BOTH"`. |

#### Timing Behavior

| Timing   | Publish point | Payload |
|----------|---------------|---------|
| `BEFORE` | Published **before** the method body executes. | Bound method arguments. |
| `AFTER`  | Published **after** the method body returns. | Bound arguments **plus** `{"result": <return value>}`. |
| `BOTH`   | Published **twice** — once before and once after. | Before: arguments. After: arguments + `{"result": <return value>}`. |

For `AFTER` and `BOTH`, the post-call publish augments the pre-call
argument payload with the method's return value under the key `"result"`,
rather than re-publishing the arguments alone.

The payload is built by inspecting the method signature and serializing the
bound arguments into a dictionary. Objects with a `__dict__` attribute are
automatically converted.

---

### @publish_result

Publishes the method's **return value** as the event payload. This is the most
common pattern: execute a business operation and broadcast the result.

```python
from pyfly.eda import publish_result

@publish_result(bus, destination="orders", event_type="order.created")
async def create_order(customer_id: str, items: list[dict]) -> dict:
    return {"order_id": "abc", "customer_id": customer_id, "status": "CREATED"}
    # The returned dict IS the event payload
```

#### Parameters

| Parameter   | Type                    | Default    | Description |
|-------------|-------------------------|------------|-------------|
| `bus`       | `InMemoryEventBus`      | *required* | The event bus instance. |
| `destination` | `str`                | *required* | Topic or channel name. |
| `event_type` | `str`                 | *required* | The event type string. |
| `condition` | `Callable[..., bool] \| None` | `None` | An optional predicate. The event is only published if `condition(result)` returns `True`. |

#### Conditional Publishing

You can gate event publishing on a condition:

```python
@publish_result(
    bus,
    destination="orders",
    event_type="order.completed",
    condition=lambda result: result.get("status") == "COMPLETED",
)
async def update_order(order_id: str, data: dict) -> dict:
    updated = await db.update(order_id, data)
    return updated  # Only published if status is COMPLETED
```

#### Payload Rules

* If the return value is a `dict`, it is used directly as the payload.
* If the return value is any other type, it is wrapped as `{"result": value}`.

---

### @event_listener

Registers a function as a subscriber for one or more event type patterns.
The decorator supports two usage forms:

**Context-driven (recommended):** Pass only the patterns. The decorator
stamps the function with discovery metadata and the `ApplicationContext`
auto-subscribes it to the `EventPublisher` bean during startup. No bus
reference is needed at decoration time.

```python
from pyfly.eda import event_listener, EventEnvelope

@event_listener(["order.created", "order.updated"])
async def handle_order_changes(envelope: EventEnvelope) -> None:
    print(f"Event: {envelope.event_type}, Data: {envelope.payload}")
```

**Hand-wired (back-compat):** Pass a bus instance explicitly. The
subscription is established immediately when the decorator executes (at
import/definition time), in addition to stamping the discovery metadata.

```python
@event_listener(bus, event_types=["order.created", "order.updated"])
async def handle_order_changes(envelope: EventEnvelope) -> None:
    print(f"Event: {envelope.event_type}, Data: {envelope.payload}")
```

#### Parameters

| Parameter     | Type                           | Description |
|---------------|--------------------------------|-------------|
| `bus`         | `EventPublisher \| list[str]`  | The event bus instance, **or** the list of patterns when used positionally (context-driven form). |
| `event_types` | `list[str] \| None`            | A list of event type patterns to subscribe to. Each pattern supports glob wildcards. Required when `bus` is a bus instance. |

In the context-driven form, `bus` receives the pattern list as a positional
argument (e.g. `@event_listener(["order.*"])`) and no bus reference is stored.

#### Wildcard Subscriptions

```python
@event_listener(["order.*"])
async def on_any_order_event(envelope: EventEnvelope) -> None:
    # Matches order.created, order.shipped, order.cancelled, etc.
    pass

@event_listener(["*"])
async def on_everything(envelope: EventEnvelope) -> None:
    # Receives every event published on the bus
    pass
```

---

## Application Events

Separate from domain events, PyFly provides **application lifecycle events**
for framework-level coordination. These are published by the application
context during startup and shutdown.

### Built-in Lifecycle Events

All application events inherit from `ApplicationEvent`:

```python
from pyfly.context.events import (
    ApplicationEvent,
    ContextRefreshedEvent,
    ApplicationReadyEvent,
    ContextClosedEvent,
)
```

| Event                    | When Published | Typical Use |
|--------------------------|----------------|-------------|
| `ContextRefreshedEvent`  | The `ApplicationContext` has finished initializing all beans and wiring dependencies. | Run database migrations, seed caches, validate configuration. |
| `ApplicationReadyEvent`  | The application is fully started and ready to serve requests (after web server is listening). | Start background tasks, open WebSocket connections, log startup metrics. |
| `ContextClosedEvent`     | The application is shutting down. | Flush buffers, close connections, save state. |

### ApplicationEventBus

The `ApplicationEventBus` is a simple in-process event bus specifically for
lifecycle events. Unlike the domain `InMemoryEventBus`, it dispatches based on
**Python types** rather than string patterns.

```python
from pyfly.context.events import ApplicationEventBus, ApplicationReadyEvent

bus = ApplicationEventBus()

async def on_ready(event: ApplicationReadyEvent) -> None:
    print("Application is ready!")

# Subscribe by event type (Python class)
bus.subscribe(ApplicationReadyEvent, on_ready)

# Publish
await bus.publish(ApplicationReadyEvent())
```

#### Ordering

Listeners are invoked in order determined by the `@order` decorator on their
owning class. If no `@order` is specified, the default order value is `0`.
Lower values execute first.

#### Subscribe Signature

```python
bus.subscribe(
    event_type: type[ApplicationEvent],  # The event class to listen for
    listener: Callable[..., Awaitable[None]],  # The async handler
    *,
    owner_cls: type | None = None,  # Optional: the class that owns this listener (for ordering)
)
```

### @app_event_listener

The `@app_event_listener` decorator marks a method as an application event
listener. The event type is **inferred from the method's type hint** on the
event parameter.

```python
from pyfly.container import service
from pyfly.context.events import (
    app_event_listener,
    ApplicationReadyEvent,
    ContextClosedEvent,
)


@service
class LifecycleManager:

    @app_event_listener
    async def on_ready(self, event: ApplicationReadyEvent) -> None:
        print("Application is ready -- starting background workers")
        await self._start_workers()

    @app_event_listener
    async def on_shutdown(self, event: ContextClosedEvent) -> None:
        print("Shutting down -- stopping background workers")
        await self._stop_workers()
```

The framework inspects the type annotation on the `event` parameter (e.g.,
`ApplicationReadyEvent`) and automatically subscribes the method to that event
type on the `ApplicationEventBus`.

You can define **multiple** `@app_event_listener` methods in the same class,
each listening for a different event type.

---

## Events vs. Messaging: When to Use Which

PyFly provides both an EDA module (`pyfly.eda`) and a messaging module
(`pyfly.messaging`). Here is how to choose:

| Criterion | Domain Events (`pyfly.eda`) | Messaging (`pyfly.messaging`) |
|-----------|----------------------------|-------------------------------|
| **Scope** | In-process (same Python process). | Cross-process, cross-service, distributed. |
| **Transport** | `InMemoryEventBus` -- direct function calls. | Kafka, RabbitMQ, or other external brokers. |
| **Payload** | `EventEnvelope` with typed `dict` payload. | Raw `bytes` -- you choose the serialization format. |
| **Pattern** | Glob-matched event types (`"order.*"`). | Topic-based with consumer groups. |
| **Durability** | None -- if the process dies, events are lost. | Broker-dependent (Kafka retains messages, RabbitMQ can persist). |
| **Use case** | Decoupling domain services within a monolith. | Decoupling microservices across network boundaries. |

**Rule of thumb**: If the producer and consumer live in the same process, use
domain events. If they are in different services (or you need durability), use
messaging.

You can also **combine both**: publish a domain event within your process, and
have a listener that forwards it to a message broker for cross-service
consumption.

---

## Complete Example: Order Domain Events

This example demonstrates a realistic order processing system with domain
events flowing between services in the same process.

```python
import uuid
from pyfly.container import service
from pyfly.eda import (
    EventEnvelope,
    event_listener,
    event_publisher,
    publish_result,
)
from pyfly.eda.adapters.memory import InMemoryEventBus


# ---------------------------------------------------------------------------
# Shared event bus
# ---------------------------------------------------------------------------

bus = InMemoryEventBus()


# ---------------------------------------------------------------------------
# Order Service (producer)
# ---------------------------------------------------------------------------

@service
class OrderService:
    """Creates and manages orders, publishing domain events."""

    @publish_result(bus, destination="orders", event_type="order.created")
    async def create_order(self, customer_id: str, items: list[dict]) -> dict:
        order = {
            "order_id": str(uuid.uuid4()),
            "customer_id": customer_id,
            "items": items,
            "status": "CREATED",
        }
        # Save to database (omitted for brevity)
        return order  # This dict becomes the event payload

    @publish_result(
        bus,
        destination="orders",
        event_type="order.completed",
        condition=lambda r: r.get("status") == "COMPLETED",
    )
    async def complete_order(self, order_id: str) -> dict:
        # Update order status (omitted for brevity)
        return {"order_id": order_id, "status": "COMPLETED"}

    @event_publisher(
        bus,
        destination="orders",
        event_type="order.cancelling",
        timing="BEFORE",
    )
    async def cancel_order(self, order_id: str, reason: str) -> None:
        # The arguments (order_id, reason) are published as the event payload
        # BEFORE this method body executes.
        pass  # Perform cancellation logic


# ---------------------------------------------------------------------------
# Inventory Service (consumer)
# ---------------------------------------------------------------------------

@service
class InventoryService:
    """Reserves and releases stock based on order events."""

    @event_listener(bus, event_types=["order.created"])
    async def on_order_created(self, envelope: EventEnvelope) -> None:
        order = envelope.payload
        for item in order["items"]:
            await self._reserve_stock(item["product_id"], item["quantity"])
        print(f"[Inventory] Reserved stock for order {order['order_id']}")

    @event_listener(bus, event_types=["order.cancelling"])
    async def on_order_cancelling(self, envelope: EventEnvelope) -> None:
        order_id = envelope.payload.get("order_id")
        print(f"[Inventory] Releasing stock for cancelled order {order_id}")

    async def _reserve_stock(self, product_id: str, quantity: int) -> None:
        pass  # Database update


# ---------------------------------------------------------------------------
# Notification Service (consumer with wildcard)
# ---------------------------------------------------------------------------

@service
class NotificationService:
    """Sends email notifications for all order-related events."""

    @event_listener(bus, event_types=["order.*"])
    async def on_any_order_event(self, envelope: EventEnvelope) -> None:
        print(
            f"[Notification] {envelope.event_type} -- "
            f"order {envelope.payload.get('order_id', 'N/A')}"
        )


# ---------------------------------------------------------------------------
# Audit Service (consumer with catch-all)
# ---------------------------------------------------------------------------

@service
class AuditService:
    """Records every event for compliance."""

    @event_listener(bus, event_types=["*"])
    async def on_any_event(self, envelope: EventEnvelope) -> None:
        print(
            f"[Audit] {envelope.event_id} | {envelope.timestamp} | "
            f"{envelope.event_type} -> {envelope.destination}"
        )
```

---

## Testing with InMemoryEventBus

The `InMemoryEventBus` makes testing straightforward. You can subscribe
test-specific handlers and assert on the envelopes they receive.

```python
import pytest
from pyfly.eda import EventEnvelope
from pyfly.eda.adapters.memory import InMemoryEventBus


@pytest.fixture
def bus() -> InMemoryEventBus:
    return InMemoryEventBus()


@pytest.mark.asyncio
async def test_publish_delivers_to_matching_subscribers(bus: InMemoryEventBus) -> None:
    received: list[EventEnvelope] = []

    async def handler(envelope: EventEnvelope) -> None:
        received.append(envelope)

    bus.subscribe("order.created", handler)

    await bus.publish("orders", "order.created", {"order_id": "test-1"})

    assert len(received) == 1
    assert received[0].event_type == "order.created"
    assert received[0].payload["order_id"] == "test-1"
    assert received[0].destination == "orders"
    # Auto-generated fields
    assert received[0].event_id  # non-empty UUID string
    assert received[0].timestamp is not None


@pytest.mark.asyncio
async def test_wildcard_pattern_matching(bus: InMemoryEventBus) -> None:
    received: list[str] = []

    async def handler(envelope: EventEnvelope) -> None:
        received.append(envelope.event_type)

    bus.subscribe("order.*", handler)

    await bus.publish("orders", "order.created", {"id": "1"})
    await bus.publish("orders", "order.shipped", {"id": "1"})
    await bus.publish("payments", "payment.received", {"id": "1"})

    # Only order.* events should match
    assert received == ["order.created", "order.shipped"]


@pytest.mark.asyncio
async def test_no_match_means_no_delivery(bus: InMemoryEventBus) -> None:
    received: list[EventEnvelope] = []

    async def handler(envelope: EventEnvelope) -> None:
        received.append(envelope)

    bus.subscribe("payment.*", handler)

    await bus.publish("orders", "order.created", {"id": "1"})

    assert len(received) == 0


@pytest.mark.asyncio
async def test_publish_result_decorator(bus: InMemoryEventBus) -> None:
    from pyfly.eda import publish_result

    received: list[EventEnvelope] = []

    async def spy(envelope: EventEnvelope) -> None:
        received.append(envelope)

    bus.subscribe("order.created", spy)

    @publish_result(bus, destination="orders", event_type="order.created")
    async def create_order(name: str) -> dict:
        return {"name": name, "status": "CREATED"}

    result = await create_order("Test Order")

    assert result == {"name": "Test Order", "status": "CREATED"}
    assert len(received) == 1
    assert received[0].payload == {"name": "Test Order", "status": "CREATED"}
```
