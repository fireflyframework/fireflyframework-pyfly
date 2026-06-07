<span class="eyebrow">Chapter 10</span>

# Messaging with Kafka & RabbitMQ {.chtitle}

::: figure art/openers/ch10.svg | &nbsp;

Lumen's wallet service is genuinely event-driven. Commands flow through typed handlers; domain events publish to an in-process bus; listeners react independently without coupling themselves to the write path. In Chapter 8 you built `BalanceProjection`, `Notifier`, and `AuditLog` — three services that react to the same facts without knowing about each other. In Chapter 9 you went further, storing those events *as the source of truth* so that every historical balance is computable from first principles.

There is one boundary neither chapter crossed: the network. `InMemoryEventBus` lives inside the Python process. The moment you want another service — a future `PaymentsService` that settles transfers, or a `NotificationsService` that sends push alerts across channels — to react to Lumen's facts, you need a message broker: an independent infrastructure component that can store events durably, route them to subscribers in other processes, and replay them when a consumer restarts after a crash.

This chapter takes Lumen's event-driven foundation across that boundary. You will learn how PyFly wraps the complexity of Apache Kafka and RabbitMQ behind a single clean abstraction — `MessageBrokerPort` — so that your application code never knows which broker is running beneath it. You will publish Lumen's wallet events to real topics, consume them with the `@message_listener` decorator, choose the right serialization format for your schema-evolution requirements, handle poisoned messages with dead-letter queues, and protect your service against broker outages with circuit breakers and retries.

By the end of the chapter, Lumen's integration events will flow across process and service boundaries, ready for the Part IV services that will consume them.

---

## One abstraction, many brokers

### Why an abstraction matters

Before you write a single line of Kafka or RabbitMQ code, it is worth asking: why does PyFly introduce an abstraction layer at all? After all, `aiokafka` and `aio-pika` both expose perfectly usable async APIs. The answer is the same reason you depend on `EventPublisher` rather than on `InMemoryEventBus` — the abstraction is what lets you swap infrastructure without touching business logic.

Without an abstraction, every service that produces or consumes a message imports Kafka-specific or RabbitMQ-specific types. Switching brokers — or running Kafka in production and an in-memory broker in CI — requires changing import paths, constructor signatures, and consumer-loop boilerplate across every affected file. With `MessageBrokerPort`, the swap is a configuration file change. The listeners and publishers that make up your business logic never change.

That same abstraction pays dividends in testing. The `InMemoryMessageBroker` satisfies the port protocol. You can inject it anywhere `MessageBrokerPort` is expected and write fast, deterministic tests with no Docker dependency. Chapter 11 will make this concrete.

### The MessageBrokerPort protocol

`MessageBrokerPort` is a `@runtime_checkable Protocol`. That means you can use it as a type hint throughout your code, and you can call `isinstance(obj, MessageBrokerPort)` at runtime if you ever need to verify that an injected bean satisfies the contract.

The protocol defines four methods:

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

**How it works — the four methods:**

`publish` sends a single message to the named topic. `value` is raw bytes — the protocol deliberately does not choose a serialization format for you; you encode the payload before calling `publish` and decode it inside your handler. `key` and `headers` are keyword-only, so callers cannot accidentally swap them with positional arguments. `key` drives Kafka partition assignment for ordering guarantees; RabbitMQ ignores it. `headers` carry cross-cutting metadata such as `event-type` and correlation IDs.

`subscribe` registers an async callable — a `MessageHandler` — for a topic. The optional `group` parameter maps to Kafka consumer groups and RabbitMQ competing-consumer queues: if you deploy three instances of a service and all three subscribe with the same `group`, only one instance processes each message. If you omit `group`, every subscriber receives every message (broadcast semantics), which is useful for fanout scenarios such as analytics that need a copy of every event.

`start` creates connections and begins consuming. Register all your subscriptions *before* calling `start`, then call `start` once during application startup.

`stop` drains in-flight messages and closes connections cleanly. PyFly's application lifecycle calls `stop` automatically during shutdown, so you rarely need to invoke it manually.

### The Message dataclass

Every handler receives a `Message` — a frozen dataclass that carries the full envelope of a received message:

```python
from pyfly.messaging import Message

msg = Message(
    topic="wallet.events",
    value=b'{"wallet_id": "w-001", "amount": 5000}',
    key=b"w-001",
    headers={"event-type": "wallet.fundsdeposited"},
)
```

| Field | Type | Default | Description |
|---|---|---|---|
| `topic` | `str` | required | The topic or queue the message arrived on. |
| `value` | `bytes` | required | The raw payload. You decode it inside your handler. |
| `key` | `bytes \| None` | `None` | Partition or routing key. Kafka uses it for partition assignment. |
| `headers` | `dict[str, str]` | `{}` | String metadata attached by the publisher. |

The dataclass is frozen: once the broker hands you a `Message`, its fields are immutable. That makes it safe to pass across async boundaries without defensive copying, and it prevents accidental mutation inside handlers.

### Kafka vs RabbitMQ — choosing the right broker

Before looking at adapter configuration, it helps to understand where each broker fits. The table below summarises the key trade-offs; neither choice is universally correct.

::: figure art/figures/10-messaging.svg | Figure 10.1 — MessageBrokerPort sits between application code and the broker adapters.

| Dimension | Apache Kafka | RabbitMQ |
|---|---|---|
| **Model** | Distributed commit log; consumers maintain their own offset | Message broker; messages are removed from the queue after acknowledgement |
| **Retention** | Configurable (days to indefinite); consumers can replay from any offset | Messages are removed on delivery; dead-letter queues for failed messages |
| **Throughput** | Millions of messages/second; optimised for streaming | Tens of thousands/second; optimised for task routing |
| **Ordering** | Guaranteed within a partition (keyed producers) | Guaranteed within a single queue (FIFO) |
| **Consumer groups** | Native partition-level load balancing | Competing-consumer queues; one message per consumer |
| **Schema evolution** | Works well with Avro/Protobuf + Schema Registry | Works well with JSON; schema coupling is a user concern |
| **When to choose** | Event streaming, audit logs, replay, high throughput | Task queues, RPC patterns, per-message routing with complex bindings |
| **PyFly extra** | `uv add "pyfly[kafka]"` | `uv add "pyfly[rabbitmq]"` |

For Lumen, Kafka is the natural fit: wallet events form an ordered stream per wallet, are worth replaying when a new consumer comes online, and will eventually feed high-throughput analytics. However, the examples in this chapter show both adapters interchangeably — because from your code's perspective, the choice is a configuration detail.

!!! note "Installing both adapters"
    If you want to support either broker in one install, `uv add "pyfly[eda]"` pulls in both `aiokafka` and `aio-pika`. The auto-configuration then selects Kafka if `aiokafka` is importable, RabbitMQ if `aio_pika` is importable, and falls back to the in-memory broker if neither is present.

---

## Configuring the adapters

### Kafka

Add `pyfly[kafka]` to your project and declare the broker in `pyfly.yaml`:

```yaml
pyfly:
  messaging:
    provider: "kafka"
    kafka:
      bootstrap-servers: "kafka-1:9092,kafka-2:9092"
```

That is all PyFly needs to auto-configure a `KafkaAdapter` and register it as the `MessageBrokerPort` bean. If you need a different exchange name or advanced producer options, construct the adapter manually as a `@bean` inside a `@configuration` class — but for most services the YAML is sufficient.

### RabbitMQ

```yaml
pyfly:
  messaging:
    provider: "rabbitmq"
    rabbitmq:
      url: "amqp://user:password@rabbitmq-host:5672/"
```

The `RabbitMQAdapter` uses a durable direct exchange named `"pyfly"` by default. To customise the exchange name, construct the adapter manually:

::: listing lumen/messaging/config.py | Listing 10.1 — Custom RabbitMQ exchange name via @bean
from pyfly.container import configuration, bean
from pyfly.messaging import MessageBrokerPort
from pyfly.messaging.adapters.rabbitmq import RabbitMQAdapter


@configuration
class BrokerConfig:
    """Wire up the message broker bean."""

    @bean
    def broker(self) -> MessageBrokerPort:
        return RabbitMQAdapter(
            url="amqp://user:password@rabbitmq-host:5672/",
            exchange_name="lumen-events",
        )
:::

**How it works:** `@configuration` marks the class as a PyFly configuration class — equivalent to a factory that the DI container calls during startup. `@bean` on `broker` tells the container to call `broker()` once, cache the result, and inject it wherever `MessageBrokerPort` is requested. Any `@service` class that declares a `MessageBrokerPort` parameter in its constructor receives this instance automatically, with no import of `RabbitMQAdapter` required in the consumer class.

### Auto-detection

When `provider` is set to `"auto"` — or omitted entirely — PyFly probes the installed packages in order:

| Priority | Library checked | Adapter selected |
|---|---|---|
| 1 | `aiokafka` | `KafkaAdapter` |
| 2 | `aio_pika` | `RabbitMQAdapter` |
| 3 | *(fallback)* | `InMemoryMessageBroker` |

This means you can set `provider: "memory"` in `pyfly-test.yaml` and `provider: "kafka"` in `pyfly-prod.yaml`, and every test and production run uses the appropriate adapter without code changes.

---

## Publishing integration events

### From in-process events to integration events

In Chapter 8, `DepositFundsHandler` drained the `Wallet` aggregate's event buffer and called `publisher.publish(...)` on the `InMemoryEventBus`. That bus was fine for notifying listeners within the same process. But when you want a separate `PaymentsService` or `NotificationsService` running in a different container to react, those in-process events are invisible.

The integration event pattern solves this. Where a *domain event* describes what happened inside an aggregate (a private fact, available to same-process listeners), an *integration event* is a sanitised, public representation of the same fact — designed for external consumers, stable across versions, and serialised to bytes for transport.

For Lumen, the integration event for a deposit carries only the fields an external consumer needs: the wallet identifier, the amount, the currency, and the event type. It does not expose the aggregate's internal implementation details.

### Draining the outbox to a topic

In Chapter 9, a transactional outbox captured integration events in the same database transaction as the aggregate write. The outbox relay is the component that reads those pending records and forwards them to the broker. In a full production setup, a dedicated background task polls the outbox table. For this chapter, you will publish directly from the command handler to demonstrate the broker API clearly — the outbox relay is the production-hardening step that ensures atomicity.

The publisher dependency is `MessageBrokerPort`. You encode the payload to bytes before calling `publish`:

::: listing lumen/messaging/deposit_publisher.py | Listing 10.2 — Publishing a wallet integration event to a Kafka topic
from __future__ import annotations

import json

from pyfly.container import service
from pyfly.cqrs.command.handler import CommandHandler
from pyfly.cqrs.decorators import command_handler
from pyfly.domain import AggregateNotFound
from pyfly.messaging import MessageBrokerPort

from lumen.cqrs.commands import DepositFunds
from lumen.domain.money import Money
from lumen.domain.wallet_repository import WalletDomainRepository


@command_handler
@service
class DepositFundsHandler(CommandHandler[DepositFunds, None]):
    """Credit funds and forward the integration event to the broker."""

    def __init__(
        self,
        repo: WalletDomainRepository,
        broker: MessageBrokerPort,
    ) -> None:
        self._repo = repo
        self._broker = broker

    async def do_handle(self, command: DepositFunds) -> None:
        wallet = await self._repo.find(command.wallet_id)
        if wallet is None:
            raise AggregateNotFound("Wallet", command.wallet_id)

        wallet.deposit(Money(
            amount=command.amount_cents,
            currency=command.currency,
        ))
        await self._repo.save(wallet)

        payload = json.dumps({
            "wallet_id": command.wallet_id,
            "amount_cents": command.amount_cents,
            "currency": command.currency,
        }).encode()

        await self._broker.publish(
            "wallet.events",
            payload,
            key=command.wallet_id.encode(),
            headers={"event-type": "wallet.fundsdeposited"},
        )
:::

**How it works — the key design decisions:**

`broker: MessageBrokerPort` is the port, not the adapter. The DI container injects whichever adapter is configured — Kafka in production, the in-memory broker in tests. This handler never mentions Kafka or RabbitMQ.

The payload is JSON-encoded to bytes with `json.dumps(...).encode()`. PyFly's `publish` accepts `bytes` and leaves serialization to you — this is intentional; section five covers richer formats.

`key=command.wallet_id.encode()` is the routing key. On Kafka, all messages sharing the same key land on the same partition, which means they are delivered to consumers in the order they were published — critical for a ledger where `deposit before withdraw` must be preserved. On RabbitMQ the key is ignored (routing uses the exchange binding), so this field is safe to include regardless of which broker is running.

`headers={"event-type": "wallet.fundsdeposited"}` allows consumers to inspect the event type without decoding the payload — useful for routing and filtering without full deserialization.

!!! warning "Publish after save, not before"
    Always call `self._broker.publish(...)` *after* `self._repo.save(wallet)`. If the save fails, no message reaches the broker and external consumers never see a fact that never persisted. Publishing before saving creates phantom events — facts about things that did not happen. The transactional outbox pattern (where the outbox row and the aggregate row are written in the same database transaction) provides the stronger atomic guarantee for production; direct publishing as shown here is a reasonable starting point for simpler services.

---

## Consuming events with @message_listener

### The problem with polling

Before brokers, services that needed to react to another service's state changes polled a shared database or a REST endpoint. Polling adds latency (the reaction is delayed until the next poll interval), wastes resources (most polls find nothing new), and couples consumer to producer at the API level. A message listener eliminates all three problems: the broker pushes the event to the consumer as soon as it is available, idle connections consume negligible CPU, and the consumer depends only on the message schema — not on the producer's internal API.

### Declarative listeners with @message_listener

`@message_listener` is the declarative subscription decorator. You decorate any async function or method with the topic it should consume, and PyFly wires the subscription during application startup — no bus reference, no `subscribe()` call, no lifecycle management required in your code.

::: listing lumen/messaging/payments_consumer.py | Listing 10.3 — @message_listener on a standalone function
from __future__ import annotations

import json

from pyfly.messaging import Message, message_listener


@message_listener(topic="wallet.events", group="payments-service")
async def on_wallet_event(msg: Message) -> None:
    """React to every wallet event published to the topic."""
    event_type = msg.headers.get("event-type", "unknown")
    payload = json.loads(msg.value)

    if event_type == "wallet.fundsdeposited":
        wallet_id: str = payload["wallet_id"]
        amount_cents: int = payload["amount_cents"]
        currency: str = payload["currency"]
        print(
            f"[Payments] Deposit received: "
            f"wallet={wallet_id} "
            f"amount={amount_cents} {currency}"
        )
:::

**How it works:** The decorator stores three metadata attributes on the wrapped function — `__pyfly_message_listener__ = True`, `__pyfly_listener_topic__`, and `__pyfly_listener_group__`. During application startup, the framework scans all registered beans, finds functions carrying `__pyfly_message_listener__ = True`, and calls `broker.subscribe(topic, handler, group)` automatically. You never call `subscribe()` manually.

`group="payments-service"` places this consumer in a consumer group. If you scale to multiple instances of the payments service, only one instance processes each message — the broker distributes load across the group. Omit `group` for broadcast semantics where every instance receives every message.

Inside the handler, `msg.headers.get("event-type", "unknown")` inspects the envelope metadata before touching the payload. This routing-by-header pattern avoids deserializing the full payload for messages the handler does not care about — important when a topic carries many event types and the handler is only interested in a subset.

### Listeners on service classes

When your listener needs collaborators — a repository, another service — declare it as a method on a `@service` class. PyFly injects the dependencies through the constructor and wires the listener subscription after the bean is initialised:

::: listing lumen/messaging/notifications_consumer.py | Listing 10.4 — @message_listener on a @service method with dependencies
from __future__ import annotations

import json

from pyfly.container import service
from pyfly.messaging import Message, message_listener


@service
class WalletNotificationConsumer:
    """Sends push notifications when wallet events arrive via the broker."""

    def __init__(self, smtp_client: object) -> None:
        # smtp_client would be an injected email/push service.
        self._smtp = smtp_client

    @message_listener(topic="wallet.events", group="notifications-service")
    async def on_wallet_event(self, msg: Message) -> None:
        event_type = msg.headers.get("event-type", "unknown")

        if event_type != "wallet.walletopened":
            return

        payload = json.loads(msg.value)
        owner_id: str = payload.get("owner_id", "")
        wallet_id: str = payload.get("wallet_id", "")
        currency: str = payload.get("currency", "")

        print(
            f"[Notification] Welcome {owner_id}! "
            f"Your {currency} wallet {wallet_id} is ready."
        )
:::

**How it works:** `@service` registers `WalletNotificationConsumer` in the DI container. The constructor receives `smtp_client` through injection — you do not `new` it yourself. After the bean is created, the framework detects `on_wallet_event` carrying `__pyfly_message_listener__ = True` and registers it as a bound method listener. The `self` reference is already captured, so every invocation of `on_wallet_event` has full access to `self._smtp`.

The early return on `event_type != "wallet.walletopened"` is a filtering guard. Because a single topic (`wallet.events`) can carry multiple event types, each listener filters for the types it cares about. This is simpler than maintaining a separate topic per event type — though for very high-volume or high-cardinality event streams, topic-per-type is a legitimate design trade-off.

!!! tip "Consumer group semantics at a glance"
    Two services with *different* group names each receive every message — the broker delivers a copy to each group. Two *instances* of the same service sharing the *same* group name share the load — each message goes to exactly one instance. Use different groups for fanout (payments and notifications both need the event); use the same group for horizontal scaling (three instances of the payments service share the work).

---

## Serialization and schema evolution

### Why bytes, and why this matters

`MessageBrokerPort.publish` accepts raw `bytes`. That was a deliberate design choice. A broker adaptor that forced a single serialization format would be convenient for simple cases and painful for anything else — schema evolution, multi-language consumers, compliance requirements, and throughput constraints all push in different directions. By leaving serialization to you, PyFly stays out of the way.

There are three formats worth knowing: JSON for simplicity, Avro for schema-registry-backed evolution, and Protobuf for performance-critical or multi-language environments. The table below summarises the trade-offs:

| Format | Human-readable | Schema enforcement | Schema evolution | Multi-language | PyFly encoding |
|---|---|---|---|---|---|
| **JSON** | Yes | Optional | Manual (consumer discipline) | Universal | `json.dumps(...).encode()` |
| **Avro** | No | Yes (via registry) | First-class (`BACKWARD` / `FORWARD` / `FULL`) | Good | `fastavro` library |
| **Protobuf** | No | Yes (`.proto` files) | First-class (field numbering) | Excellent | `protobuf` library |

### JSON — start here

JSON is the right default. It requires no tooling beyond the standard library, every language can parse it, and the payload is readable in broker monitoring UIs. The encoding pattern is two lines:

```python
import json

payload: bytes = json.dumps({
    "wallet_id": "w-001",
    "amount_cents": 5000,
    "currency": "EUR",
}).encode()

await broker.publish("wallet.events", payload)
```

Decoding in the consumer:

```python
data: dict = json.loads(msg.value)
```

The weakness of JSON is that there is no enforcement of the schema. If the publisher adds a required field and the consumer has not been updated, the consumer breaks silently. For Lumen's internal events where producer and consumer are deployed together, this is manageable. For events shared with external teams or long-lived topics, you need stronger guarantees.

### Avro — schema-registry-backed evolution

Avro schemas are JSON documents that describe the shape of a message. A Schema Registry (Confluent's is the most common, but open-source alternatives exist) stores those schemas and enforces compatibility rules when producers register new versions. The `fastavro` library encodes and decodes the binary payload:

::: listing lumen/messaging/avro_publisher.py | Listing 10.5 — Publishing a wallet event with Avro encoding
from __future__ import annotations

import io

import fastavro  # type: ignore[import]

from pyfly.messaging import MessageBrokerPort

WALLET_DEPOSITED_SCHEMA = {
    "type": "record",
    "name": "WalletFundsDeposited",
    "namespace": "lumen.wallet",
    "fields": [
        {"name": "wallet_id", "type": "string"},
        {"name": "amount_cents", "type": "long"},
        {"name": "currency", "type": "string"},
    ],
}

_PARSED = fastavro.parse_schema(WALLET_DEPOSITED_SCHEMA)


async def publish_deposit(
    broker: MessageBrokerPort,
    wallet_id: str,
    amount_cents: int,
    currency: str,
) -> None:
    """Encode a deposit event with Avro and publish to the topic."""
    record = {
        "wallet_id": wallet_id,
        "amount_cents": amount_cents,
        "currency": currency,
    }
    buf = io.BytesIO()
    fastavro.schemaless_writer(buf, _PARSED, record)

    await broker.publish(
        "wallet.events",
        buf.getvalue(),
        headers={"content-type": "avro/binary"},
    )
:::

**How it works:** `fastavro.parse_schema` compiles the JSON schema document once at module load time — do not parse it inside the publish function or you pay the compilation cost on every call. `fastavro.schemaless_writer` serializes the record into the `BytesIO` buffer without embedding the schema in every message (the registry provides the schema on the consumer side). `buf.getvalue()` extracts the bytes for `broker.publish`.

The `headers={"content-type": "avro/binary"}` header signals to consumers that they should use Avro decoding. This convention lets a topic carry both JSON and Avro messages during a migration period.

### Protobuf — performance and polyglot

Protocol Buffers compile a `.proto` file into a generated class. They produce smaller messages than JSON and Avro, and the generated code is available in every major language — making Protobuf the right choice when the consumer is a Go or Java service.

```python
# Assumes a generated class lumen_pb2.WalletFundsDeposited
from lumen_pb2 import WalletFundsDeposited  # type: ignore[import]

event = WalletFundsDeposited(
    wallet_id="w-001",
    amount_cents=5000,
    currency="EUR",
)
payload: bytes = event.SerializeToString()

await broker.publish(
    "wallet.events",
    payload,
    headers={"content-type": "application/protobuf"},
)
```

Decoding in the consumer follows the mirror pattern:

```python
from lumen_pb2 import WalletFundsDeposited  # type: ignore[import]

event = WalletFundsDeposited()
event.ParseFromString(msg.value)
```

!!! tip "Start with JSON, migrate when you feel the pain"
    The correct progression for most teams is: JSON first (fast to ship, easy to debug); add Avro when multiple teams own different sides of a topic and schema drift becomes a real coordination cost; switch to Protobuf when binary size or multi-language interop is a hard requirement. Because PyFly's `publish` and `@message_listener` accept raw bytes, you can change serialization format without changing the broker API calls — just swap the encoding and decoding steps.

---

## When delivery fails: dead-letter queues

### The inevitable bad message

Even a well-designed consumer will eventually encounter a message it cannot process. A downstream database might be unavailable. The payload might violate an assumption the consumer relied on. A transient network error might interrupt a third-party API call mid-handler. The question is not whether a consumer will fail — it is what happens when it does.

Without a dead-letter strategy, a failed consumer either drops the message (losing data) or re-queues it indefinitely (creating an infinite retry loop that blocks all subsequent messages, known as a *poison pill*). A dead-letter queue (DLQ) is the structured answer: a separate topic or queue where messages that cannot be processed after a configurable number of attempts are parked for inspection and manual reprocessing.

In PyFly's messaging model you implement dead-letter handling inside your listener. When delivery fails and retries are exhausted, you publish the original message bytes to a DLQ topic, then allow the exception to propagate (so the broker knows delivery failed) or acknowledge the message to prevent an infinite loop:

::: listing lumen/messaging/dlq_consumer.py | Listing 10.6 — Dead-letter queue handling in a message listener
from __future__ import annotations

import json
import logging

from pyfly.container import service
from pyfly.messaging import Message, MessageBrokerPort, message_listener

logger = logging.getLogger(__name__)

MAX_ATTEMPTS = 3


@service
class ResilientWalletConsumer:
    """Processes wallet events with dead-letter queue fallback."""

    def __init__(self, broker: MessageBrokerPort) -> None:
        self._broker = broker

    @message_listener(topic="wallet.events", group="payments-dlq")
    async def on_wallet_event(self, msg: Message) -> None:
        attempt_str = msg.headers.get("x-attempt", "1")
        attempt = int(attempt_str)

        try:
            await self._process(msg)
        except Exception:
            logger.exception(
                "Failed to process wallet event: "
                "attempt=%d topic=%s",
                attempt,
                msg.topic,
            )
            if attempt >= MAX_ATTEMPTS:
                await self._send_to_dlq(msg, attempt)
            else:
                raise

    async def _process(self, msg: Message) -> None:
        payload = json.loads(msg.value)
        event_type = msg.headers.get("event-type", "unknown")
        logger.info(
            "Processing event: type=%s wallet=%s",
            event_type,
            payload.get("wallet_id"),
        )

    async def _send_to_dlq(self, msg: Message, attempt: int) -> None:
        dlq_headers = dict(msg.headers)
        dlq_headers["x-failed-attempt"] = str(attempt)
        dlq_headers["x-original-topic"] = msg.topic

        await self._broker.publish(
            "wallet.events.dlq",
            msg.value,
            headers=dlq_headers,
        )
        logger.warning(
            "Message sent to DLQ after %d attempts: topic=%s",
            attempt,
            msg.topic,
        )
:::

**How it works — the DLQ pattern in five steps:**

`x-attempt` in the message headers tracks how many delivery attempts have occurred. The publisher increments this counter on each re-publish; on first delivery from the broker it defaults to `"1"`.

The `try/except` block calls `_process` and catches any exception. `logger.exception(...)` logs the full stack trace using a single call (the exception is the implicit last argument to SLF4J-style logging in Python's standard `logging` module — the exception is attached automatically to the `LogRecord`).

When `attempt >= MAX_ATTEMPTS`, the handler copies the original headers into `dlq_headers`, appends diagnostic headers (`x-failed-attempt`, `x-original-topic`), and publishes the original `msg.value` bytes unchanged to the DLQ topic `wallet.events.dlq`. The original bytes are not re-encoded — a DLQ observer needs to see exactly what the failed consumer saw.

When `attempt < MAX_ATTEMPTS`, the handler re-raises the exception. The broker interprets this as a negative acknowledgement and re-delivers the message.

When the DLQ publish succeeds, the handler returns normally. Returning without raising tells the broker the message was handled — preventing an infinite loop.

!!! warning "Design consumers for idempotency"
    A consumer that reaches `MAX_ATTEMPTS` and sends to the DLQ has consumed the message. If an operator later replays the DLQ message, the consumer will process it again. Without idempotency, that double-processing can corrupt data — crediting a wallet twice, sending a duplicate notification. Use the message's stable identifier (or `event_id` if you forward `EventEnvelope` headers) as an idempotency key: before processing, check whether that ID has already been recorded in a `processed_events` table, and skip the work if it has. The check-and-record step should be in the same database transaction as the business write.

---

## Resilience: circuit breakers and retries

### Protecting Lumen from a broker outage

A healthy broker is not guaranteed. Network partitions, rolling upgrades, and resource exhaustion can all make the broker temporarily unavailable. If the command handler that publishes the wallet event calls `broker.publish(...)` and the broker is down, you have two choices without a resilience layer: fail the entire command (refusing to deposit funds because the broker is unreachable) or silently drop the event (the deposit succeeds but the integration event is lost).

Neither is acceptable. The transactional outbox (Chapter 9) is the atomic solution — the event is captured in the database and a relay publishes it asynchronously, so a broker outage only adds latency, not data loss. Alongside the outbox, circuit breakers and retries protect the relay and any other broker-calling code from cascading failures.

PyFly's resilience module (`pyfly.resilience`) provides both primitives. The circuit breaker opens after a configurable failure threshold and blocks calls to the broker for a cool-down period, preventing a thundering-herd reconnection storm. The retry decorator handles transient errors with configurable back-off:

::: listing lumen/messaging/resilient_publisher.py | Listing 10.7 — Resilient broker publishing with retry and circuit breaker
from __future__ import annotations

import json
import logging

from pyfly.container import service
from pyfly.messaging import MessageBrokerPort
from pyfly.resilience import CircuitBreaker, circuit_breaker, retry

logger = logging.getLogger(__name__)


@service
class OutboxRelay:
    """
    Drains pending outbox records and forwards them to the broker.
    Applies retry and circuit-breaker protection on every publish call.
    """

    def __init__(self, broker: MessageBrokerPort) -> None:
        self._broker = broker

    @retry(max_attempts=3, delay=1.0, backoff=2.0)
    @circuit_breaker(CircuitBreaker(failure_threshold=5, recovery_timeout=30))
    async def forward(
        self,
        topic: str,
        payload: dict,
        event_type: str,
    ) -> None:
        """Forward a single outbox record to the broker."""
        await self._broker.publish(
            topic,
            json.dumps(payload).encode(),
            headers={"event-type": event_type},
        )
        logger.info(
            "Event forwarded: topic=%s event-type=%s",
            topic,
            event_type,
        )
:::

**How it works — the two decorators:**

`@retry(max_attempts=3, delay=1.0, backoff=2.0)` wraps `forward` in a retry loop of up to three attempts. After the first failure it waits `delay` seconds (1 s); after the second it waits `delay * backoff` (2 s) — the wait grows as `delay * backoff ** attempt`. If the third attempt still fails, the exception propagates to the caller. Retries are appropriate for *transient* failures (a single broker node restarting); they are counterproductive for *permanent* failures (a misconfigured topic that will never accept messages), so keep `max_attempts` low and let the circuit breaker handle sustained outages.

`@circuit_breaker(CircuitBreaker(failure_threshold=5, recovery_timeout=30))` guards `forward` with a shared `CircuitBreaker` instance that tracks consecutive failures across every call. When the count reaches `failure_threshold`, the circuit *opens* and subsequent calls fail immediately with a `CircuitBreakerException` instead of reaching for an unreachable broker — protecting it from a reconnection storm. After `recovery_timeout` seconds, the circuit enters a *half-open* state: the next call is allowed through as a probe. If it succeeds, the circuit closes; if it fails, it re-opens. Decorator order matters: `@retry` is the outer decorator, so all three attempts of one logical call happen before the circuit breaker registers a single failure.

!!! spring "Spring parity"
    `MessageBrokerPort` with `KafkaAdapter` is PyFly's counterpart of Spring Kafka's `KafkaTemplate` (publishing) and `@KafkaListener` (consuming). `RabbitMQAdapter` mirrors Spring AMQP's `RabbitTemplate` and `@RabbitListener`. The lifecycle model is the same: register listeners before starting the container, and the framework manages the consumer threads. Dead-letter queues in Spring Kafka are configured via `DeadLetterPublishingRecoverer` on the `DefaultErrorHandler`; in Spring AMQP via `RabbitListenerContainerFactory` with a `MessageRecoverer`. PyFly implements the same pattern in application code (as shown in Listing 10.6) rather than requiring broker-specific container configuration. The `@retry` and `@circuit_breaker` decorators mirror Resilience4j's `@Retryable` and `@CircuitBreaker` annotations used with Spring's messaging infrastructure.

---

## What you built {.recap}

Part III is complete.

Lumen is now fully event-driven, event-sourced, and broker-connected. Here is where each chapter left things.

**Chapter 8** introduced the two-bus model: `ApplicationEventBus` for framework lifecycle events, `InMemoryEventBus` for domain events. You wired `EventPublisher` into the command handlers so every aggregate mutation produced a fact that independent listeners — `BalanceProjection`, `Notifier`, `AuditLog` — could react to without knowing each other.

**Chapter 9** replaced the mutable aggregate-plus-read-model approach with event sourcing. Every financial movement is an immutable event appended to the ledger. The current balance is computed by replaying the event stream. The `EventEnvelope` you met in Chapter 8 became the unit of storage, and snapshots kept replay times bounded.

**This chapter** crossed the network boundary. `MessageBrokerPort` is the single abstraction that stands in front of Kafka, RabbitMQ, or the in-memory broker. Swapping adapters is a configuration change — no business code changes. `@message_listener` gives you declarative, zero-boilerplate subscriptions on both standalone functions and `@service` methods. You encoded payloads as JSON bytes, with Avro and Protobuf available when schema enforcement or binary efficiency matters more than simplicity. Dead-letter queues park unprocessable messages safely. `@retry` and `@circuit_breaker` protect the publish path from transient and sustained broker failures.

Three principles carry forward into Part IV:

- **Depend on the port, not the adapter.** `MessageBrokerPort` is injected; `KafkaAdapter` is a configuration detail.
- **Design consumers for idempotency.** Brokers deliver *at least once*. Guard against duplicate processing with a stable message identifier.
- **Capture events atomically.** The transactional outbox ensures that an event is never lost even if the broker is unavailable at write time.

Part IV introduces the `PaymentsService` and `NotificationsService`. Both services subscribe to `wallet.events`. The adapter and configuration choices you made in this chapter are all they need to start receiving Lumen's facts the moment they connect.

---

## Try it yourself {.exercises}

1. **Swap the adapter in one line.** Start with `provider: "memory"` in `pyfly.yaml` and add the `@message_listener` from Listing 10.3. Write an integration test that publishes a `wallet.fundsdeposited` message and asserts the listener receives it. Then switch `provider: "kafka"` in the YAML and confirm the same test (with a Testcontainers-managed Kafka broker) passes without changing the listener or the test assertion.

2. **Add a DLQ monitor.** Create a second `@message_listener` on topic `wallet.events.dlq` with group `dlq-monitor`. It should log the `x-original-topic` and `x-failed-attempt` headers along with the decoded payload. Write a test that simulates a failing consumer by raising `RuntimeError` on the first two attempts (track attempt count with a counter in a closure) and confirming on the third that the DLQ monitor receives the message with `x-failed-attempt: "3"`.

3. **Evolve the schema with Avro.** Start with the `WALLET_DEPOSITED_SCHEMA` from Listing 10.5. Add an optional `note` field with a default of `None` (Avro union `["null", "string"]`, default `null`). Confirm that a consumer compiled against the original schema can still decode a message encoded with the new schema — this is a *backward-compatible* change. Then try adding a required field without a default and observe the `SchemaParseException` the registry would raise, illustrating why defaults are mandatory for safe evolution.
