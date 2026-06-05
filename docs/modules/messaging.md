# Messaging Guide

PyFly's messaging module provides a broker-agnostic abstraction for publishing and
consuming messages. It follows the hexagonal architecture pattern: a single
`MessageBrokerPort` protocol defines the contract, while pluggable adapters
(in-memory, Kafka, RabbitMQ) supply the implementation. You write your business
logic against the port, and the framework wires in the correct adapter at
runtime.

---

## Table of Contents

1. [Architecture Overview](#architecture-overview)
2. [The Message Type](#the-message-type)
3. [MessageBrokerPort Protocol](#messagebrokerport-protocol)
4. [MessageHandler Callable](#messagehandler-callable)
5. [The @message_listener Decorator](#the-message_listener-decorator)
6. [Adapters](#adapters)
   - [InMemoryMessageBroker](#inmemorymessagebroker)
   - [KafkaAdapter](#kafkaadapter)
   - [RabbitMQAdapter](#rabbitmqadapter)
7. [Auto-Configuration](#auto-configuration)
8. [Configuration Reference](#configuration-reference)
9. [Complete Example: Order Processing Pipeline](#complete-example-order-processing-pipeline)
10. [Testing with the In-Memory Broker](#testing-with-the-in-memory-broker)

---

## Architecture Overview

PyFly messaging is built on two concepts from hexagonal architecture:

* **Port** -- `MessageBrokerPort` is a `Protocol` that defines publish, subscribe,
  start, and stop operations. Your application code depends only on this
  abstraction.
* **Adapters** -- Concrete classes (`InMemoryMessageBroker`, `KafkaAdapter`,
  `RabbitMQAdapter`) implement the port for a specific technology.

```
Application Code
       |
       v
 MessageBrokerPort  (protocol / port)
       |
       +-- InMemoryMessageBroker  (dev / test)
       +-- KafkaAdapter           (production, via aiokafka)
       +-- RabbitMQAdapter        (production, via aio-pika)
```

Because every adapter satisfies the same protocol, you can swap brokers without
changing a single line of business logic.

---

## The Message Type

`Message` is a frozen dataclass that carries a message through the system. It is
the only object your handler ever receives.

```python
from pyfly.messaging import Message

msg = Message(
    topic="orders",
    value=b'{"order_id": "abc-123"}',
    key=b"customer-42",
    headers={"content-type": "application/json"},
)
```

### Fields

| Field     | Type              | Default | Description                                         |
|-----------|-------------------|---------|-----------------------------------------------------|
| `topic`   | `str`             | *required* | The topic or queue the message belongs to.        |
| `value`   | `bytes`           | *required* | The raw message payload. Serialization is up to you (JSON, Avro, Protobuf, etc.). |
| `key`     | `bytes \| None`   | `None`     | An optional partition/routing key. Kafka uses this for partition assignment; RabbitMQ ignores it. |
| `headers` | `dict[str, str]`  | `{}`       | Key-value metadata headers attached to the message. |

Because the dataclass is frozen, `Message` instances are immutable and safe to
pass across async boundaries.

---

## MessageBrokerPort Protocol

The port is defined as a `@runtime_checkable` `Protocol`, so you can use
`isinstance()` checks at runtime and depend on it for type hints everywhere.

```python
from pyfly.messaging import MessageBrokerPort

class MessageBrokerPort(Protocol):
    async def publish(
        self,
        topic: str,
        value: bytes,
        *,
        key: bytes | None = None,
        headers: dict[str, str] | None = None,
    ) -> None: ...

    async def subscribe(
        self,
        topic: str,
        handler: MessageHandler,
        group: str | None = None,
    ) -> None: ...

    async def start(self) -> None: ...

    async def stop(self) -> None: ...
```

### Method Reference

| Method                                      | Description |
|---------------------------------------------|-------------|
| `publish(topic, value, *, key, headers)`    | Send a message to the given topic. `key` and `headers` are optional keyword-only arguments. |
| `subscribe(topic, handler, group)`          | Register a `MessageHandler` for a topic. If `group` is provided, handlers in the same group share load (consumer group semantics). |
| `start()`                                   | Initialize connections and begin consuming. Call this after all subscriptions are registered. |
| `stop()`                                    | Gracefully shut down consumers and producers, releasing connections. |

**Lifecycle**: Register subscriptions first with `subscribe()`, then call
`start()`. When your application shuts down, call `stop()`.

---

## MessageHandler Callable

A `MessageHandler` is a type alias for any async callable that accepts a
`Message` and returns `None`:

```python
from pyfly.messaging import MessageHandler

# Type definition:
# MessageHandler = Callable[[Message], Coroutine[Any, Any, None]]

async def my_handler(msg: Message) -> None:
    print(f"Received on {msg.topic}: {msg.value}")
```

You can pass standalone async functions, bound methods, or any object with a
matching `__call__` signature.

---

## The @message_listener Decorator

The `@message_listener` decorator provides declarative message subscription. It
marks a function or method so the framework can auto-discover it during context
initialization and register it with the broker.

```python
from pyfly.messaging import message_listener, Message

@message_listener(topic="orders", group="order-processors")
async def handle_order(msg: Message) -> None:
    order = json.loads(msg.value)
    print(f"Processing order {order['order_id']}")
```

### Parameters

| Parameter | Type           | Default | Description |
|-----------|----------------|---------|-------------|
| `topic`   | `str`          | *required* | The topic to listen on. |
| `group`   | `str \| None`  | `None`     | Consumer group name. Handlers in the same group receive messages in round-robin fashion (only one handler per group processes each message). |

### How It Works

Under the hood, the decorator stores three metadata attributes on the wrapped
function:

| Attribute                       | Value |
|---------------------------------|-------|
| `__pyfly_message_listener__`    | `True` |
| `__pyfly_listener_topic__`      | The topic string |
| `__pyfly_listener_group__`      | The group string (or `None`) |

During application startup, the framework scans registered beans for functions
carrying `__pyfly_message_listener__ = True` and calls
`broker.subscribe(topic, handler, group)` automatically.

### Using Inside a Service Class

When decorating a method on a `@service` class, the method becomes a bound
listener after the container creates the bean:

```python
from pyfly.container import service
from pyfly.messaging import message_listener, Message

@service
class PaymentProcessor:

    @message_listener(topic="payments", group="payment-group")
    async def on_payment(self, msg: Message) -> None:
        data = json.loads(msg.value)
        await self._process_payment(data)
```

---

## Adapters

### InMemoryMessageBroker

The in-memory broker is designed for **development, testing, and single-process
applications**. It requires no external infrastructure.

```python
from pyfly.messaging import Message
from pyfly.messaging.adapters.memory import InMemoryMessageBroker

broker = InMemoryMessageBroker()

received: list[Message] = []

async def handler(msg: Message) -> None:
    received.append(msg)

await broker.subscribe("orders", handler)
await broker.start()

await broker.publish("orders", b'{"id": 1}')
assert len(received) == 1
assert received[0].topic == "orders"

await broker.stop()
```

#### Consumer Group Semantics

When multiple handlers subscribe with the same `group`, the in-memory broker
distributes messages using **round-robin**:

```python
results_a: list[Message] = []
results_b: list[Message] = []

async def handler_a(msg: Message) -> None:
    results_a.append(msg)

async def handler_b(msg: Message) -> None:
    results_b.append(msg)

await broker.subscribe("orders", handler_a, group="workers")
await broker.subscribe("orders", handler_b, group="workers")
await broker.start()

# Send three messages -- they alternate between handler_a and handler_b
await broker.publish("orders", b"msg-1")  # -> handler_a
await broker.publish("orders", b"msg-2")  # -> handler_b
await broker.publish("orders", b"msg-3")  # -> handler_a
```

Handlers with `group=None` receive **every** message (broadcast semantics).

---

### KafkaAdapter

The `KafkaAdapter` is the production adapter for Apache Kafka. It wraps the
[aiokafka](https://github.com/aio-libs/aiokafka) library, managing producers
and consumers internally.

**Install:** `uv add "pyfly[kafka]"` (this pulls in `aiokafka`).

```python
from pyfly.messaging.adapters.kafka import KafkaAdapter

broker = KafkaAdapter(bootstrap_servers="kafka-1:9092,kafka-2:9092")

async def handle_order(msg: Message) -> None:
    print(f"Order: {msg.value}")

await broker.subscribe("orders", handle_order, group="order-service")
await broker.start()   # Creates AIOKafkaProducer + AIOKafkaConsumer(s)

await broker.publish(
    "orders",
    b'{"order_id": "123"}',
    key=b"customer-42",
    headers={"event-type": "order.created"},
)

await broker.stop()    # Cancels consumer tasks, stops producer
```

#### Constructor

| Parameter            | Type  | Default            | Description |
|----------------------|-------|--------------------|-------------|
| `bootstrap_servers`  | `str` | `"localhost:9092"` | Comma-separated list of Kafka bootstrap servers. |

#### Internal Behavior

* **Producer**: An `AIOKafkaProducer` is created on `start()` and sends
  messages with `send_and_wait()` for reliable delivery.
* **Consumers**: One `AIOKafkaConsumer` per unique (topic, group) pair is
  created on `start()`, each running in its own `asyncio.Task`.
* **Headers**: Kafka headers are byte-encoded on publish and decoded back to
  strings on consume. Non-decodable header values fall back to hex
  representation.
* **Shutdown**: `stop()` cancels all consumer tasks, stops every consumer, then
  stops the producer.

---

### RabbitMQAdapter

The `RabbitMQAdapter` is the production adapter for RabbitMQ. It wraps the
[aio-pika](https://github.com/mosquito/aio-pika) library and uses a single
direct exchange.

**Install:** `uv add "pyfly[rabbitmq]"` (this pulls in `aio-pika`).

```python
from pyfly.messaging.adapters.rabbitmq import RabbitMQAdapter

broker = RabbitMQAdapter(
    url="amqp://user:password@rabbitmq-host:5672/",
    exchange_name="my-app",
)

await broker.subscribe("orders", handle_order, group="order-service")
await broker.start()

await broker.publish("orders", b'{"order_id": "456"}')
await broker.stop()
```

#### Constructor

| Parameter        | Type  | Default                             | Description |
|------------------|-------|-------------------------------------|-------------|
| `url`            | `str` | `"amqp://guest:guest@localhost/"`   | AMQP connection URL. |
| `exchange_name`  | `str` | `"pyfly"`                           | Name of the direct exchange to declare. |

#### Internal Behavior

* **Connection**: Uses `aio_pika.connect_robust()` for automatic reconnection.
* **Exchange**: A durable direct exchange is declared on `start()`.
* **Queues**: Each subscription creates a durable queue. The queue name is the
  `group` parameter if provided, otherwise `"pyfly.{topic}"`. The queue is
  bound to the exchange with the topic as the routing key.
* **Message acknowledgement**: Messages are processed inside
  `message.process()`, which handles acknowledgement automatically.
* **Shutdown**: `stop()` closes the underlying AMQP connection.

---

## Auto-Configuration

When using the `"auto"` provider setting (or when no provider is explicitly
configured), PyFly detects which messaging library is installed and selects the
appropriate adapter:

| Detection Order | Library Checked | Adapter Selected          |
|-----------------|-----------------|---------------------------|
| 1               | `aiokafka`      | `KafkaAdapter`            |
| 2               | `aio_pika`      | `RabbitMQAdapter`         |
| 3               | *(fallback)*    | `InMemoryMessageBroker`   |

This means you can switch brokers simply by installing a different library,
with no code changes required.

---

## Configuration Reference

Configure messaging in your `pyfly.yaml`:

```yaml
pyfly:
  messaging:
    provider: memory           # "kafka", "rabbitmq", or "memory"

    kafka:
      bootstrap-servers: localhost:9092

    rabbitmq:
      url: amqp://guest:guest@localhost/
```

| Property                              | Default                            | Description |
|---------------------------------------|------------------------------------|-------------|
| `pyfly.messaging.provider`            | `"memory"`                         | Which adapter to use: `"kafka"`, `"rabbitmq"`, or `"memory"`. |
| `pyfly.messaging.kafka.bootstrap-servers` | `"localhost:9092"`            | Kafka bootstrap servers (comma-separated). |
| `pyfly.messaging.rabbitmq.url`        | `"amqp://guest:guest@localhost/"`  | AMQP connection URL for RabbitMQ. |

> **Note:** The `RabbitMQAdapter` exchange name is fixed at `"pyfly"` when auto-configured. To use a different exchange name, construct the `RabbitMQAdapter` manually and register it as a bean.

---

## Complete Example: Order Processing Pipeline

The following example demonstrates a realistic multi-service messaging setup
with an `OrderService` that publishes messages and a `NotificationService` and
`AnalyticsService` that consume them.

```python
import json
import uuid
from dataclasses import dataclass

from pyfly.container import service, configuration, bean
from pyfly.messaging import (
    Message,
    MessageBrokerPort,
    message_listener,
)
from pyfly.messaging.adapters.memory import InMemoryMessageBroker


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

@configuration
class MessagingConfig:
    """Wire up the message broker as a bean."""

    @bean
    def broker(self) -> MessageBrokerPort:
        # Use InMemoryMessageBroker for local dev; swap to KafkaAdapter or
        # RabbitMQAdapter in production via pyfly.yaml auto-configuration.
        return InMemoryMessageBroker()


# ---------------------------------------------------------------------------
# Producer
# ---------------------------------------------------------------------------

@service
class OrderService:
    """Creates orders and publishes events to the 'orders' topic."""

    def __init__(self, broker: MessageBrokerPort) -> None:
        self._broker = broker

    async def create_order(self, customer_id: str, items: list[dict]) -> dict:
        order = {
            "order_id": str(uuid.uuid4()),
            "customer_id": customer_id,
            "items": items,
            "status": "CREATED",
        }

        await self._broker.publish(
            "orders",
            json.dumps(order).encode(),
            key=customer_id.encode(),
            headers={"event-type": "order.created"},
        )

        return order

    async def cancel_order(self, order_id: str) -> None:
        await self._broker.publish(
            "orders",
            json.dumps({"order_id": order_id, "status": "CANCELLED"}).encode(),
            headers={"event-type": "order.cancelled"},
        )


# ---------------------------------------------------------------------------
# Consumers
# ---------------------------------------------------------------------------

@service
class NotificationService:
    """Sends customer notifications for order events."""

    @message_listener(topic="orders", group="notifications")
    async def on_order_event(self, msg: Message) -> None:
        order = json.loads(msg.value)
        event_type = msg.headers.get("event-type", "unknown")
        print(f"[Notification] {event_type}: order {order['order_id']}")


@service
class AnalyticsService:
    """Tracks order metrics. Runs in its own consumer group."""

    @message_listener(topic="orders", group="analytics")
    async def on_order_event(self, msg: Message) -> None:
        order = json.loads(msg.value)
        print(f"[Analytics] Recording event for order {order['order_id']}")
```

Because `NotificationService` and `AnalyticsService` use different consumer
groups (`"notifications"` and `"analytics"`), every message on the `"orders"`
topic is delivered to **both** services. Within each group, if you scale to
multiple instances, only one instance handles each message.

---

## Testing with the In-Memory Broker

The `InMemoryMessageBroker` makes it straightforward to write deterministic
tests without spinning up Kafka or RabbitMQ:

```python
import json
import pytest
from pyfly.messaging import Message
from pyfly.messaging.adapters.memory import InMemoryMessageBroker


@pytest.fixture
def broker() -> InMemoryMessageBroker:
    return InMemoryMessageBroker()


@pytest.mark.asyncio
async def test_publish_and_consume(broker: InMemoryMessageBroker) -> None:
    received: list[Message] = []

    async def handler(msg: Message) -> None:
        received.append(msg)

    await broker.subscribe("orders", handler)
    await broker.start()

    payload = json.dumps({"order_id": "test-1"}).encode()
    await broker.publish("orders", payload, headers={"event-type": "order.created"})

    assert len(received) == 1
    assert received[0].topic == "orders"
    assert json.loads(received[0].value)["order_id"] == "test-1"
    assert received[0].headers["event-type"] == "order.created"

    await broker.stop()


@pytest.mark.asyncio
async def test_consumer_group_round_robin(broker: InMemoryMessageBroker) -> None:
    """Messages are distributed round-robin within a consumer group."""
    results: dict[str, list[Message]] = {"a": [], "b": []}

    async def handler_a(msg: Message) -> None:
        results["a"].append(msg)

    async def handler_b(msg: Message) -> None:
        results["b"].append(msg)

    await broker.subscribe("events", handler_a, group="workers")
    await broker.subscribe("events", handler_b, group="workers")
    await broker.start()

    for i in range(4):
        await broker.publish("events", f"msg-{i}".encode())

    # Round-robin: handler_a gets msg-0, msg-2; handler_b gets msg-1, msg-3
    assert len(results["a"]) == 2
    assert len(results["b"]) == 2

    await broker.stop()
```

Because `InMemoryMessageBroker` satisfies `MessageBrokerPort`, you can inject
it anywhere the protocol is expected -- no mocking required.

---

## Adapters

- [Kafka Adapter](../adapters/kafka.md) — Setup, configuration reference, and adapter-specific features for the Apache Kafka backend
- [RabbitMQ Adapter](../adapters/rabbitmq.md) — Setup, configuration reference, and adapter-specific features for the RabbitMQ backend
