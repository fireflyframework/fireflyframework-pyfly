<span class="eyebrow">Chapter 10</span>

# Messaging with Kafka & RabbitMQ {.chtitle}

::: figure art/openers/ch10.svg | &nbsp;

Lumen's wallet service is genuinely event-driven. Commands flow through typed handlers; domain events publish to an in-process bus; listeners react independently without coupling to the write path. In Chapter 8 you built `WalletAuditListener` — a service that reacts to `WalletOpened`, `FundsDeposited`, and `FundsWithdrawn` events without knowing the command handlers. In Chapter 9 you went further, storing those events *as the source of truth* so every historical balance is computable from first principles.

There is one boundary neither chapter crossed: the network. `InMemoryEventBus` lives inside the Python process. The moment another service — a future `PaymentsService` that settles transfers, or a `NotificationsService` that sends push alerts — needs to react to Lumen's facts, you need a **message broker**: an independent infrastructure component that stores events durably, routes them to subscribers in other processes, and replays them when a consumer restarts after a crash.

This chapter takes Lumen's event-driven foundation across that boundary. You will see how PyFly wraps the complexity of Apache Kafka and RabbitMQ behind a single clean abstraction — `MessageBrokerPort` — so that application code never knows which broker is running beneath it. You will publish Lumen's wallet events to real topics, consume them with the `@message_listener` decorator, choose the right serialisation format for your schema-evolution requirements, handle poisoned messages with dead-letter queues built into the decorator, and protect your service against broker outages with circuit breakers and retries.

By the end of the chapter Lumen's integration events flow across process boundaries, ready for the Part IV services that will consume them.

---

## One abstraction, many brokers

### Why an abstraction matters

Before writing a line of Kafka or RabbitMQ code, it is worth asking: why does PyFly introduce an abstraction layer at all? `aiokafka` and `aio-pika` both expose perfectly usable async APIs. The answer is the same reason you depend on `EventPublisher` rather than `InMemoryEventBus` — the abstraction is what lets you swap infrastructure without touching business logic.

Without an abstraction, every service that produces or consumes a message imports Kafka-specific or RabbitMQ-specific types. Switching brokers — or running Kafka in production and an in-memory broker in CI — means changing import paths, constructor signatures, and consumer-loop boilerplate across every affected file. With `MessageBrokerPort`, the swap is a YAML change. The listeners and publishers that make up your business logic never change.

The abstraction pays dividends in testing too. `InMemoryMessageBroker` satisfies the port protocol. Inject it wherever `MessageBrokerPort` is expected and write fast, deterministic tests with no Docker dependency. Chapter 16 makes this concrete.

### The MessageBrokerPort protocol

**`MessageBrokerPort`** is a `@runtime_checkable Protocol`. Use it as a type hint throughout your code; call `isinstance(obj, MessageBrokerPort)` at runtime if you need to verify that an injected bean satisfies the contract.

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

**The four methods:**

`publish` sends a single message to the named topic. `value` is raw bytes — the protocol deliberately leaves serialisation to you; encode the payload before calling `publish` and decode it inside your handler. `key` and `headers` are keyword-only so callers cannot accidentally transpose them. `key` drives Kafka partition assignment for ordering guarantees; RabbitMQ ignores it. `headers` carry cross-cutting metadata such as `event-type` and correlation IDs.

`subscribe` registers an async `MessageHandler` for a topic. The optional `group` parameter maps to Kafka consumer groups and RabbitMQ competing-consumer queues. Deploy three instances of a service, all subscribing with the same `group`, and only one instance processes each message. Omit `group` for broadcast semantics — every subscriber receives every message — which is useful for analytics that need a copy of every event.

`start` creates connections and begins consuming. Register all subscriptions *before* calling `start`, then call it once during application startup.

`stop` drains in-flight messages and closes connections cleanly. PyFly's application lifecycle calls `stop` automatically during shutdown, so you rarely need to invoke it manually.

### The Message dataclass

Every handler receives a **`Message`** — a frozen dataclass carrying the full envelope of a received message:

```python
from pyfly.messaging import Message

msg = Message(
    topic="wallet.events",
    value=b'{"wallet_id": "w-001", "amount": 5000}',
    key=b"w-001",
    headers={"event-type": "FundsDeposited"},
)
```

| Field | Type | Default | Description |
|---|---|---|---|
| `topic` | `str` | required | The topic or queue the message arrived on. |
| `value` | `bytes` | required | The raw payload. You decode it inside your handler. |
| `key` | `bytes \| None` | `None` | Partition or routing key. Kafka uses it for partition assignment. |
| `headers` | `dict[str, str]` | `{}` | String metadata attached by the publisher. |

The dataclass is frozen: once the broker hands you a `Message` its fields are immutable — safe to pass across async boundaries without defensive copying, and immune to accidental mutation inside handlers.

### Kafka vs RabbitMQ — choosing the right broker

Before diving into configuration, it helps to understand where each broker fits. The table below summarises the key trade-offs; neither choice is universally correct.

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

For Lumen, Kafka is the natural fit: wallet events form an ordered stream per wallet, are worth replaying when a new consumer comes online, and will eventually feed high-throughput analytics. The examples in this chapter show both adapters interchangeably — from your code's perspective, the choice is a configuration detail.

!!! note "Installing both adapters"
    If you want to support either broker in one install,
    `uv add "pyfly[eda]"` pulls in both `aiokafka` and `aio-pika`. The
    auto-configuration then selects Kafka if `aiokafka` is importable,
    RabbitMQ if `aio_pika` is importable, and falls back to the in-memory
    broker if neither is present.

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

That is all PyFly needs to auto-configure a `KafkaAdapter` and register it as the `MessageBrokerPort` bean. For most services the YAML is sufficient; if you need advanced producer options, construct the adapter manually as a `@bean` inside a `@configuration` class.

### RabbitMQ

```yaml
pyfly:
  messaging:
    provider: "rabbitmq"
    rabbitmq:
      url: "amqp://user:password@rabbitmq-host:5672/"
```

`RabbitMQAdapter` uses a durable direct exchange named `"pyfly"` by default. To customise the exchange name, construct the adapter manually:

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

**How it works.** `@configuration` marks the class as a factory that the DI container calls during startup. `@bean` on `broker` tells the container to call `broker()` once, cache the result, and inject it wherever `MessageBrokerPort` is requested. Any `@service` that declares `MessageBrokerPort` in its constructor receives this instance automatically — no import of `RabbitMQAdapter` required in the consumer class.

### Auto-detection

When `provider` is `"memory"` (the default) or `"auto"`, PyFly probes installed packages in order:

| Priority | Library checked | Adapter selected |
|---|---|---|
| 1 | `aiokafka` | `KafkaAdapter` |
| 2 | `aio_pika` | `RabbitMQAdapter` |
| 3 | *(fallback)* | `InMemoryMessageBroker` |

Set `provider: "memory"` in `pyfly-test.yaml` and `provider: "kafka"` in `pyfly-prod.yaml`, and every test and production run uses the appropriate adapter without code changes.

---

## Publishing integration events

### From in-process events to integration events

In Chapter 8, Lumen's command handlers drained the `Wallet` aggregate's event buffer with `wallet.clear_events()` and published each domain event through `EventPublisher`. `WalletAuditListener` subscribed using `@event_listener` and reacted within the same process.

The **integration event** pattern crosses the process boundary. Where a *domain event* describes what happened inside an aggregate — a private fact, available to same-process listeners — an integration event is a sanitised, public representation of the same fact: designed for external consumers, stable across versions, and serialised to bytes for transport over a broker.

For Lumen, the integration event for a deposit carries only what an external consumer needs: the wallet identifier, the amount in minor units, the currency code, and the resulting balance. It does not expose the aggregate's internal implementation details.

### How Lumen drains events to the broker

Lumen separates the publish bridge from the command handlers so every handler publishes events identically. `publish_domain_events` (in `lumen/core/services/wallets/event_publishing.py`) iterates the drained events, converts each frozen dataclass to a dict, and calls `EventPublisher.publish`:

```python
# lumen/core/services/wallets/event_publishing.py  (real Lumen code)
from pyfly.eda import EventPublisher
from pyfly.domain import DomainEvent

async def publish_domain_events(
    publisher: EventPublisher,
    events: Iterable[DomainEvent],
) -> None:
    for event in events:
        payload = dataclasses.asdict(event)
        payload.setdefault("event_type", event.event_type)
        await publisher.publish(
            destination="wallet.events",
            event_type=event.event_type,   # "WalletOpened" / "FundsDeposited" / …
            payload=payload,
        )
```

The `EventPublisher.publish` signature is:

```python
async def publish(
    self,
    destination: str,
    event_type: str,
    payload: dict[str, Any],
    headers: dict[str, str] | None = None,
) -> None: ...
```

`destination` is the logical channel name (`"wallet.events"`). `event_type` is the domain event class name — `"WalletOpened"`, `"FundsDeposited"`, or `"FundsWithdrawn"` — which is exactly what `@event_listener` subscribers filter on.

Every command handler wires in `EventPublisher` via the constructor and calls `publish_domain_events` after persisting:

::: listing lumen/core/services/wallets/deposit_funds_handler.py | Listing 10.2 — DepositFundsHandler drains events via EventPublisher
from __future__ import annotations

from lumen.core.services.wallets.deposit_funds_command import DepositFunds
from lumen.core.services.wallets.event_publishing import publish_domain_events
from lumen.models.entities.v1.money import Money
from lumen.models.repositories.wallet_repository import WalletRepository
from pyfly.container import service
from pyfly.cqrs import CommandHandler, command_handler
from pyfly.domain import AggregateNotFound
from pyfly.eda import EventPublisher


@command_handler
@service
class DepositFundsHandler(CommandHandler[DepositFunds, int]):
    """Credit funds to an existing wallet; returns the new balance."""

    def __init__(
        self,
        repository: WalletRepository,
        events: EventPublisher,
    ) -> None:
        super().__init__()
        self._repository = repository
        self._events = events

    async def do_handle(self, command: DepositFunds) -> int:
        wallet = await self._repository.find(command.wallet_id)
        if wallet is None:
            raise AggregateNotFound("Wallet", command.wallet_id)

        wallet.deposit(Money(
            amount=command.amount,     # integer minor units (e.g. 5000 = €50.00)
            currency=wallet.currency,
        ))
        await self._repository.add(wallet)

        # Drain pending events and forward them to the EDA bus.
        await publish_domain_events(
            self._events, wallet.clear_events()
        )
        return wallet.balance.amount
:::

**Key design decisions:**

`events: EventPublisher` is the port, not the adapter. The DI container injects whichever bus is configured — in-memory in tests, a broker-backed bus in production. This handler never mentions Kafka or RabbitMQ.

`command.amount` is the deposit in integer minor units (e.g. `5000` means €50.00 for a EUR wallet). The `FundsDeposited` domain event records the same `amount` field, plus `currency` (a string like `"EUR"`) and `balance` (the new balance in minor units).

`wallet.clear_events()` drains the aggregate's pending events list and returns them. Calling it *after* `repository.add` ensures the events describe a fact that persisted. Publishing before saving would create phantom events — facts about things that never happened.

The domain events raised during a deposit are instances of:

```python
@dataclass(frozen=True)
class FundsDeposited(DomainEvent):
    wallet_id: str = ""
    amount: int = 0      # integer minor units
    currency: str = ""   # e.g. "EUR"
    balance: int = 0     # new balance after deposit, minor units
```

When `publish_domain_events` publishes this event, `event_type` is the class name `"FundsDeposited"` — *not* a dotted string like `"wallet.fundsdeposited"`.

### Publishing an integration event directly to the broker

When a separate service running in a different process needs to receive Lumen's wallet events, the EDA bus must be backed by a real broker adapter. The payload flowing over the wire is the same dict the in-process listeners see. A dedicated `OutboxRelay` (covered in the resilience section) or a broker-backed `EventPublisher` handles the transport:

::: listing lumen/messaging/deposit_publisher.py | Listing 10.3 — Publishing a wallet integration event to a Kafka topic
from __future__ import annotations

import json

from pyfly.messaging import MessageBrokerPort


async def publish_deposit_event(
    broker: MessageBrokerPort,
    wallet_id: str,
    amount: int,
    currency: str,
    balance: int,
) -> None:
    """Encode a FundsDeposited integration event and publish to the topic."""
    payload = json.dumps({
        "wallet_id": wallet_id,
        "amount": amount,        # integer minor units
        "currency": currency,    # e.g. "EUR"
        "balance": balance,      # new balance, minor units
        "event_type": "FundsDeposited",
    }).encode()

    await broker.publish(
        "wallet.events",
        payload,
        key=wallet_id.encode(),
        headers={"event-type": "FundsDeposited"},
    )
:::

**Key design decisions:**

`broker: MessageBrokerPort` is the port, not the adapter. The DI container injects whichever adapter is configured — Kafka in production, the in-memory broker in tests.

`key=wallet_id.encode()` is the routing key. On Kafka, all messages sharing the same key land on the same partition, delivering them to consumers in publication order — critical for a ledger where deposit before withdraw must be preserved. On RabbitMQ the key is ignored (routing uses the exchange binding), so this field is safe to include regardless of which broker is running.

`headers={"event-type": "FundsDeposited"}` uses the domain event class name — not a dotted path like `"wallet.fundsdeposited"`. Consumers can inspect the event type without decoding the payload, which is useful for routing and filtering without full deserialisation.

!!! warning "Publish after save, not before"
    Always drain and publish events *after* `repository.add(wallet)`. If
    the save fails, no message reaches the broker and external consumers
    never see a fact that never persisted. The transactional outbox pattern
    (where the outbox row and the aggregate row are written in the same
    database transaction) provides the stronger atomic guarantee for
    production; direct publishing as shown here is a reasonable starting
    point for simpler services.

---

## Consuming events with @message_listener

### The problem with polling

Before brokers, services reacted to another service's state changes by polling a shared database or a REST endpoint. Polling adds latency (the reaction waits until the next poll interval), wastes resources (most polls find nothing new), and couples consumer to producer at the API level. A message listener eliminates all three problems: the broker pushes the event as soon as it is available, idle connections consume negligible CPU, and the consumer depends only on the message schema — not on the producer's internal API.

### Declarative listeners with @message_listener

**`@message_listener`** is the declarative subscription decorator. Decorate any async function or method with the topic it should consume, and PyFly wires the subscription during application startup — no bus reference, no `subscribe()` call, no lifecycle management in your code.

The decorator signature is:

```python
def message_listener(
    topic: str,
    group: str | None = None,
    *,
    retries: int = 0,
    retry_delay: float = 0.0,
    dead_letter_topic: str | None = None,
) -> ...: ...
```

| Parameter | Type | Default | Description |
|---|---|---|---|
| `topic` | `str` | required | The topic to listen on. |
| `group` | `str \| None` | `None` | Consumer group name. |
| `retries` | `int` | `0` | Times to re-invoke the handler on failure. |
| `retry_delay` | `float` | `0.0` | Base delay (seconds) between retries — attempt N waits `retry_delay * N`. |
| `dead_letter_topic` | `str \| None` | `None` | When set, a message still failing after `retries` is re-published here. |

::: listing lumen/messaging/payments_consumer.py | Listing 10.4 — @message_listener on a standalone function
from __future__ import annotations

import json

from pyfly.messaging import Message, message_listener


@message_listener(topic="wallet.events", group="payments-service")
async def on_wallet_event(msg: Message) -> None:
    """React to every wallet event published to the topic."""
    event_type = msg.headers.get("event-type", "unknown")
    payload = json.loads(msg.value)

    if event_type == "FundsDeposited":
        wallet_id: str = payload["wallet_id"]
        amount: int = payload["amount"]        # minor units
        currency: str = payload["currency"]
        print(
            f"[Payments] Deposit received: "
            f"wallet={wallet_id} "
            f"amount={amount} {currency}"
        )
:::

**How it works.** The decorator stores six metadata attributes on the wrapped function — `__pyfly_message_listener__ = True`, plus `__pyfly_listener_topic__`, `__pyfly_listener_group__`, `__pyfly_listener_retries__`, `__pyfly_listener_retry_delay__`, and `__pyfly_listener_dlq__`. During startup the framework scans all registered beans, finds functions carrying `__pyfly_message_listener__ = True`, and calls `broker.subscribe(topic, handler, group)` automatically. You never call `subscribe()` manually.

`group="payments-service"` places the consumer in a consumer group. Scale to multiple instances of the payments service and only one processes each message — the broker distributes load across the group. Omit `group` for broadcast semantics where every instance receives every message.

Inside the handler, `msg.headers.get("event-type", "unknown")` inspects the envelope metadata before touching the payload. The header value is the domain event class name — `"FundsDeposited"`, `"WalletOpened"`, or `"FundsWithdrawn"` — matching what Lumen sets on the publisher side.

### Listeners on service classes

When a listener needs collaborators — a repository, another service — declare it as a method on a `@service` class. PyFly injects the dependencies through the constructor and wires the listener subscription after the bean is initialised:

::: listing lumen/messaging/notifications_consumer.py | Listing 10.5 — @message_listener on a @service method with dependencies
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

        if event_type != "WalletOpened":
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

**How it works.** `@service` registers `WalletNotificationConsumer` in the DI container. The constructor receives `smtp_client` through injection. After the bean is created, the framework detects `on_wallet_event` carrying `__pyfly_message_listener__ = True` and registers it as a bound-method listener — `self` is already captured, so every invocation has full access to `self._smtp`.

The early return on `event_type != "WalletOpened"` is a filtering guard. A single topic (`wallet.events`) carries multiple event types, so each listener filters for the ones it cares about. This is simpler than maintaining a separate topic per event type, though for very high-volume streams, topic-per-type is a legitimate design trade-off.

!!! tip "Consumer group semantics at a glance"
    Two services with *different* group names each receive every message —
    the broker delivers a copy to each group. Two *instances* of the same
    service sharing the *same* group name share the load — each message
    goes to exactly one instance. Use different groups for fanout (payments
    and notifications both need the event); use the same group for
    horizontal scaling (three instances of the payments service share the
    work).

---

## Serialisation and schema evolution

### Why bytes, and why this matters

`MessageBrokerPort.publish` accepts raw `bytes`. That is a deliberate choice. A broker adaptor that forced a single serialisation format would be convenient for simple cases and painful for everything else — schema evolution, multi-language consumers, compliance requirements, and throughput constraints all push in different directions. By leaving serialisation to you, PyFly stays out of the way.

Three formats are worth knowing: JSON for simplicity, Avro for schema-registry-backed evolution, and Protobuf for performance-critical or multi-language environments:

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
    "amount": 5000,          # integer minor units (€50.00)
    "currency": "EUR",
    "balance": 10000,        # new balance, minor units
    "event_type": "FundsDeposited",
}).encode()

await broker.publish("wallet.events", payload)
```

Decoding in the consumer:

```python
data: dict = json.loads(msg.value)
```

JSON's weakness is that the schema is unenforced. If a publisher adds a required field and the consumer has not been updated, the consumer breaks silently. For Lumen's internal events where producer and consumer are deployed together, this is manageable. For events shared with external teams or long-lived topics, you need stronger guarantees.

### Avro — schema-registry-backed evolution

Avro schemas are JSON documents describing the shape of a message. A Schema Registry (Confluent's is the most common, but open-source alternatives exist) stores those schemas and enforces compatibility rules when producers register new versions. The `fastavro` library encodes and decodes the binary payload:

::: listing lumen/messaging/avro_publisher.py | Listing 10.6 — Publishing a wallet event with Avro encoding
from __future__ import annotations

import io

import fastavro  # type: ignore[import]

from pyfly.messaging import MessageBrokerPort

WALLET_DEPOSITED_SCHEMA = {
    "type": "record",
    "name": "FundsDeposited",
    "namespace": "lumen.wallet",
    "fields": [
        {"name": "wallet_id", "type": "string"},
        {"name": "amount", "type": "long"},     # integer minor units
        {"name": "currency", "type": "string"},
        {"name": "balance", "type": "long"},    # new balance, minor units
    ],
}

_PARSED = fastavro.parse_schema(WALLET_DEPOSITED_SCHEMA)


async def publish_deposit_avro(
    broker: MessageBrokerPort,
    wallet_id: str,
    amount: int,
    currency: str,
    balance: int,
) -> None:
    """Encode a FundsDeposited event with Avro and publish to the topic."""
    record = {
        "wallet_id": wallet_id,
        "amount": amount,      # integer minor units
        "currency": currency,
        "balance": balance,
    }
    buf = io.BytesIO()
    fastavro.schemaless_writer(buf, _PARSED, record)

    await broker.publish(
        "wallet.events",
        buf.getvalue(),
        headers={"content-type": "avro/binary",
                 "event-type": "FundsDeposited"},
    )
:::

**How it works.** `fastavro.parse_schema` compiles the JSON schema document once at module load time — never parse it inside the publish function or you pay the compilation cost on every call. `fastavro.schemaless_writer` serialises the record into the `BytesIO` buffer without embedding the schema in each message (the registry provides the schema on the consumer side). `buf.getvalue()` extracts the bytes for `broker.publish`.

The `headers={"content-type": "avro/binary", "event-type": "FundsDeposited"}` headers signal to consumers that Avro decoding is required and carry the event type for routing — consistent with the JSON convention.

### Protobuf — performance and polyglot

Protocol Buffers compile a `.proto` file into a generated class. They produce smaller messages than JSON or Avro, and the generated code is available in every major language — making Protobuf the right choice when the consumer is a Go or Java service.

```python
# Assumes a generated class lumen_pb2.FundsDeposited
from lumen_pb2 import FundsDeposited  # type: ignore[import]

event = FundsDeposited(
    wallet_id="w-001",
    amount=5000,      # integer minor units
    currency="EUR",
    balance=10000,
)
payload: bytes = event.SerializeToString()

await broker.publish(
    "wallet.events",
    payload,
    headers={"content-type": "application/protobuf",
             "event-type": "FundsDeposited"},
)
```

Decoding in the consumer follows the mirror pattern:

```python
from lumen_pb2 import FundsDeposited  # type: ignore[import]

event = FundsDeposited()
event.ParseFromString(msg.value)
```

!!! tip "Start with JSON, migrate when you feel the pain"
    The correct progression for most teams is: JSON first (fast to ship,
    easy to debug); add Avro when multiple teams own different sides of a
    topic and schema drift becomes a real coordination cost; switch to
    Protobuf when binary size or multi-language interop is a hard
    requirement. Because PyFly's `publish` and `@message_listener` accept
    raw bytes, you can change serialization format without changing the
    broker API calls — just swap the encoding and decoding steps.

---

## When delivery fails: dead-letter queues

### The inevitable bad message

Even a well-designed consumer will eventually encounter a message it cannot process. A downstream database may be unavailable. The payload may violate an assumption the consumer relied on. A transient network error may interrupt a third-party API call mid-handler. The question is not whether a consumer will fail — it is what happens when it does.

Without a dead-letter strategy, a failed consumer either drops the message (data loss) or re-queues it indefinitely, creating an infinite retry loop that blocks all subsequent messages — a *poison pill*. A **dead-letter queue** (DLQ) is the structured answer: a separate topic or queue where messages that cannot be processed after a configurable number of attempts are parked for inspection and manual reprocessing.

### Decorator-native retry and DLQ

In PyFly, retry and dead-letter routing are built into `@message_listener` — no try/except scaffolding, no manual publish to the DLQ. Declare `retries` and `dead_letter_topic` directly on the decorator:

::: listing lumen/messaging/resilient_consumer.py | Listing 10.7 — Retry and DLQ wired through @message_listener
from __future__ import annotations

import json
import logging

from pyfly.container import service
from pyfly.messaging import Message, message_listener

logger = logging.getLogger(__name__)


@service
class ResilientWalletConsumer:
    """Processes wallet events with built-in retry and DLQ fallback."""

    @message_listener(
        topic="wallet.events",
        group="payments-dlq",
        retries=3,
        retry_delay=0.5,              # waits 0.5s, 1.0s, 1.5s (linear)
        dead_letter_topic="wallet.events.DLQ",
    )
    async def on_wallet_event(self, msg: Message) -> None:
        payload = json.loads(msg.value)
        event_type = msg.headers.get("event-type", "unknown")
        logger.info(
            "Processing event: type=%s wallet=%s",
            event_type,
            payload.get("wallet_id"),
        )
        # Any unhandled exception triggers a retry; after 3 retries
        # the original message is forwarded to wallet.events.DLQ.
        await self._charge(payload)

    async def _charge(self, payload: dict) -> None:
        raise NotImplementedError("replace with real payment logic")
:::

**The three parameters:**

`retries=3` re-invokes `on_wallet_event` up to three more times after the first failure. Retries are appropriate for *transient* failures (a single database node restarting); keep the count low and let the DLQ handle sustained failures.

`retry_delay=0.5` applies linear back-off: attempt 1 waits 0.5 s, attempt 2 waits 1.0 s, attempt 3 waits 1.5 s. With `retry_delay=0.0` (the default), retries are immediate.

`dead_letter_topic="wallet.events.DLQ"` is the safety net. When all retries are exhausted, the framework re-publishes the original message to the DLQ topic, preserving the original `value` and `key`, and adds two diagnostic headers:

| Header | Value |
|---|---|
| `x-original-topic` | The topic the message was originally consumed from. |
| `x-exception` | The exception class name (e.g. `RuntimeError`). |

The exception is then swallowed so the consumer keeps running — the message is parked, not lost, and the next message on the topic is processed normally.

### Monitoring the DLQ

Subscribe to the DLQ topic like any other listener to observe and alert on dead-lettered messages:

::: listing lumen/messaging/dlq_monitor.py | Listing 10.8 — Subscribing to the DLQ topic
from __future__ import annotations

import json
import logging

from pyfly.messaging import Message, message_listener

logger = logging.getLogger(__name__)


@message_listener(topic="wallet.events.DLQ", group="dlq-monitor")
async def on_dead_letter(msg: Message) -> None:
    """Log every message that failed all retries."""
    original = msg.headers.get("x-original-topic", "unknown")
    exc = msg.headers.get("x-exception", "unknown")
    payload = json.loads(msg.value) if msg.value else {}
    logger.warning(
        "DLQ message: original_topic=%s exception=%s wallet=%s",
        original,
        exc,
        payload.get("wallet_id"),
    )
:::

!!! warning "Design consumers for idempotency"
    A consumer that reaches the DLQ retry limit has consumed the message.
    If an operator later replays the DLQ message, the consumer will
    process it again. Without idempotency, that double-processing can
    corrupt data — crediting a wallet twice, sending a duplicate
    notification. Use the message's stable identifier as an idempotency
    key: before processing, check whether that ID has already been
    recorded in a `processed_events` table, and skip the work if it has.
    The check-and-record step should be in the same database transaction
    as the business write.

---

## Resilience: circuit breakers and retries

### Protecting Lumen from a broker outage

A healthy broker is not guaranteed. Network partitions, rolling upgrades, and resource exhaustion can all make the broker temporarily unavailable. If the command handler calls `broker.publish(...)` and the broker is down, you face two bad choices without a resilience layer: fail the entire command (refusing to deposit funds because the broker is unreachable) or silently drop the event (the deposit succeeds but the integration event is lost).

Neither is acceptable. The transactional outbox (Chapter 9) is the atomic solution — the event is captured in the database and a relay publishes it asynchronously, so a broker outage adds only latency, not data loss. Alongside the outbox, **circuit breakers** and **retries** protect the relay and any broker-calling code from cascading failures.

PyFly's resilience module (`pyfly.resilience`) provides both primitives. The circuit breaker opens after a configurable failure threshold and blocks calls to the broker during a cool-down period, preventing a thundering-herd reconnection storm. The retry decorator handles transient errors with configurable back-off:

::: listing lumen/messaging/resilient_publisher.py | Listing 10.9 — Resilient broker publishing with retry and circuit breaker
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

**The two decorators:**

`@retry(max_attempts=3, delay=1.0, backoff=2.0)` wraps `forward` in a retry loop of up to three attempts. After the first failure it waits `delay` seconds (1 s); after the second it waits `delay * backoff` (2 s) — the wait grows as `delay * backoff ** attempt`. If the third attempt still fails, the exception propagates. Retries suit *transient* failures (a single broker node restarting); they are counterproductive for *permanent* failures (a misconfigured topic). Keep `max_attempts` low and let the circuit breaker handle sustained outages.

`@circuit_breaker(CircuitBreaker(failure_threshold=5, recovery_timeout=30))` guards `forward` with a shared `CircuitBreaker` instance that tracks consecutive failures across all calls. When the count reaches `failure_threshold`, the circuit *opens* and subsequent calls fail immediately with `CircuitBreakerException` rather than attempting to reach an unreachable broker — preventing a reconnection storm. After `recovery_timeout` seconds the circuit enters a *half-open* state: the next call is allowed through as a probe. If it succeeds, the circuit closes; if it fails, it re-opens. Decorator order matters: `@retry` is the outer decorator, so all three attempts of one logical call happen before the circuit breaker registers a single failure.

!!! spring "Spring parity"
    `MessageBrokerPort` with `KafkaAdapter` is PyFly's counterpart of
    Spring Kafka's `KafkaTemplate` (publishing) and `@KafkaListener`
    (consuming). `RabbitMQAdapter` mirrors Spring AMQP's `RabbitTemplate`
    and `@RabbitListener`. The lifecycle model is the same: register
    listeners before starting the container, and the framework manages the
    consumer threads. Dead-letter queues in Spring Kafka are configured
    via `DeadLetterPublishingRecoverer` on the `DefaultErrorHandler`; in
    Spring AMQP via `RabbitListenerContainerFactory` with a
    `MessageRecoverer`. PyFly implements the same pattern declaratively
    through `@message_listener(retries=..., dead_letter_topic=...)` rather
    than requiring broker-specific container configuration. The `@retry`
    and `@circuit_breaker` decorators mirror Resilience4j's `@Retryable`
    and `@CircuitBreaker` annotations used with Spring's messaging
    infrastructure.

---

## What you built {.recap}

Part III is complete.

Lumen is now fully event-driven, event-sourced, and broker-connected. Here is where each chapter left things.

**Chapter 8** introduced the two-bus model: `ApplicationEventBus` for framework lifecycle events, `InMemoryEventBus` for domain events. `EventPublisher` was wired into the command handlers so every aggregate mutation produced a fact that independent listeners — `WalletAuditListener` among them — could react to without knowing each other. Subscriptions use `@event_listener(event_types=["WalletOpened", "FundsDeposited", "FundsWithdrawn"])`; handlers receive an `EventEnvelope` whose `event_type` is the domain event class name.

**Chapter 9** replaced the mutable aggregate-plus-read-model approach with event sourcing. Every financial movement is an immutable event appended to the ledger. The current balance is computed by replaying the event stream. `EventEnvelope` became the unit of storage, and snapshots kept replay times bounded.

**This chapter** crossed the network boundary. `MessageBrokerPort` is the single abstraction in front of Kafka, RabbitMQ, or the in-memory broker. Swapping adapters is a configuration change — no business code changes. `@message_listener` gives declarative, zero-boilerplate subscriptions on both standalone functions and `@service` methods. The `retries` and `dead_letter_topic` parameters handle poisoned messages without manual try/except scaffolding. Payloads were encoded as JSON bytes, with Avro and Protobuf available when schema enforcement or binary efficiency matters more than simplicity. `@retry` and `@circuit_breaker` protect the publish path from transient and sustained broker failures.

The domain events flowing through all three chapters are:

| Event class | Fields |
|---|---|
| `WalletOpened` | `wallet_id`, `owner_id`, `currency` |
| `FundsDeposited` | `wallet_id`, `amount`, `currency`, `balance` |
| `FundsWithdrawn` | `wallet_id`, `amount`, `currency`, `balance` |

`amount` and `balance` are always integer minor units (e.g. `5000` for
€50.00). `currency` is a string value from the `Currency` StrEnum
(`"EUR"`, `"USD"`, `"GBP"`). The `event_type` header value is always the
class name — `"FundsDeposited"` — never a dotted path.

Three principles carry forward into Part IV:

- **Depend on the port, not the adapter.** `MessageBrokerPort` is injected; `KafkaAdapter` is a configuration detail.
- **Design consumers for idempotency.** Brokers deliver *at least once*. Guard against duplicate processing with a stable message identifier.
- **Capture events atomically.** The transactional outbox ensures an event is never lost even when the broker is unavailable at write time.

Part IV introduces the `PaymentsService` and `NotificationsService`. Both subscribe to `wallet.events`. The adapter and configuration choices made in this chapter are all they need to start receiving Lumen's facts the moment they connect.

---

## Try it yourself {.exercises}

1. **Swap the adapter in one line.** Start with `provider: "memory"` in
   `pyfly.yaml` and add the `@message_listener` from Listing 10.4. Write
   an integration test that publishes a `FundsDeposited` message with
   `amount=5000` and `currency="EUR"` and asserts the listener receives
   it. Then switch `provider: "kafka"` in the YAML and confirm the same
   test (with a Testcontainers-managed Kafka broker) passes without
   changing the listener or the test assertion.

2. **Add a DLQ monitor.** Create a second `@message_listener` on topic
   `wallet.events.DLQ` with group `dlq-monitor`. It should log the
   `x-original-topic` and `x-exception` headers along with the decoded
   payload. Write a test that simulates a failing consumer by raising
   `RuntimeError` inside the handler, configures `retries=2` and
   `dead_letter_topic="wallet.events.DLQ"`, and confirms the DLQ monitor
   receives the message with `x-original-topic: "wallet.events"`.

3. **Evolve the schema with Avro.** Start with the
   `WALLET_DEPOSITED_SCHEMA` from Listing 10.6. Add an optional `note`
   field with a default of `None` (Avro union `["null", "string"]`,
   default `null`). Confirm that a consumer compiled against the original
   schema can still decode a message encoded with the new schema — this is
   a *backward-compatible* change. Then try adding a required field
   without a default and observe the `SchemaParseException` the registry
   would raise, illustrating why defaults are mandatory for safe
   evolution.
