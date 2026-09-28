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
6. [Delivery Guarantees](#delivery-guarantees)
   - [Listener Transactions](#listener-transactions)
   - [Publishing from a Unit of Work](#publishing-from-a-unit-of-work)
7. [Adapters](#adapters)
   - [InMemoryMessageBroker](#inmemorymessagebroker)
   - [KafkaAdapter](#kafkaadapter)
   - [RabbitMQAdapter](#rabbitmqadapter)
8. [Auto-Configuration](#auto-configuration)
9. [Configuration Reference](#configuration-reference)
10. [Complete Example: Order Processing Pipeline](#complete-example-order-processing-pipeline)
11. [Testing with the In-Memory Broker](#testing-with-the-in-memory-broker)

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
| `partition` | `int \| None`  | `None`     | The Kafka partition the message was read from. |
| `offset`  | `int \| None`    | `None`     | The Kafka offset of the message. With `topic` and `partition`, a key to deduplicate on. |
| `message_id` | `str \| None` | `None`     | The AMQP message id on RabbitMQ. `RabbitMQAdapter.publish()` gives every message one, and a redelivery or a retry keeps it. |
| `delivery_attempt` | `int`   | `1`        | How many times this message has been delivered to the listener, this delivery included. |

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

| Parameter           | Type           | Default    | Description |
|---------------------|----------------|------------|-------------|
| `topic`             | `str`          | *required* | The topic to listen on. |
| `group`             | `str \| None`  | `None`     | Consumer group name. Handlers in the same group receive messages in round-robin fashion (only one handler per group processes each message). |
| `retries`           | `int \| None`  | `None`     | Deliveries after the first one when the handler fails (`0` dead-letters at the first failure). `None` keeps the container's `retry.max-attempts` (5 attempts in all). Keyword-only. |
| `retry_delay`       | `float \| None` | `None`    | Linear back-off: attempt *N+1* waits `retry_delay * N` seconds. `None` keeps the container's exponential back-off (1 s, doubling, at most 30 s). Keyword-only. |
| `dead_letter_topic` | `str \| None`  | `None`     | Where a message still failing after its last attempt goes, instead of `<topic>.DLT` (Kafka) or `<queue>.dlq` (RabbitMQ). Keyword-only. |

### How It Works

Under the hood, the decorator stores metadata attributes on the wrapped
function:

| Attribute                          | Value |
|------------------------------------|-------|
| `__pyfly_message_listener__`       | `True` |
| `__pyfly_listener_topic__`         | The topic string |
| `__pyfly_listener_group__`         | The group string (or `None`) |
| `__pyfly_listener_retries__`       | The retry count (or `None`) |
| `__pyfly_listener_retry_delay__`   | The base retry delay in seconds (or `None`) |
| `__pyfly_listener_dlq__`           | The dead-letter topic (or `None`) |

During application startup, the framework scans registered beans for functions
carrying `__pyfly_message_listener__ = True` and calls
`broker.subscribe(topic, handler, group)` automatically. On Kafka and RabbitMQ the
handler reaches the broker as a `ListenerEndpoint` that carries its `retries`,
`retry_delay` and `dead_letter_topic`; the broker's listener container applies them.

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

### Retry and Dead-Letter Routing

A failing listener is attempted again after a back-off, and after its last attempt its
message is dead-lettered. On Kafka and RabbitMQ the listener container does this outside
the delivery's unit of work, and each attempt reaches the broker again (see
[Delivery Guarantees](#delivery-guarantees)); on the in-memory broker the handler is
retried in process.

```python
from pyfly.container import service
from pyfly.data.transactional import transactional
from pyfly.messaging import message_listener, Message

@service
class PaymentProcessor:

    @message_listener(
        topic="payments",
        group="payment-group",
        retries=3,                      # up to 4 deliveries in all
        retry_delay=0.5,                # linear back-off: 0.5 s, 1.0 s, 1.5 s
        dead_letter_topic="payments.DLQ",
    )
    @transactional
    async def on_payment(self, msg: Message) -> None:
        data = json.loads(msg.value)
        await self._charge(data)        # if this keeps raising -> payments.DLQ
```

Without `retries` and `retry_delay`, a listener gets the container's policy
(`pyfly.messaging.listener.retry.*`): 5 attempts, 1 s apart and doubling, at most 30 s.
Without `dead_letter_topic`, the dead letter goes to `<topic>.DLT` on Kafka and to the
queue `<queue>.dlq` behind the exchange `pyfly.dlx` on RabbitMQ.

The dead-letter message keeps the original value, key and headers, and adds:

| Header                    | Value |
|---------------------------|-------|
| `x-original-topic`        | The topic the message was consumed from. |
| `x-exception`             | The class name of the exception (e.g. `ValueError`). |
| `x-exception-message`     | The exception's message (at most 1000 characters). |
| `x-dlt-attempts`          | How many times the message was delivered. |
| `x-dlt-reason`            | The class name of the exception, as the EDA buses name it. |
| `x-dlt-source-topic`      | The topic, again. |
| `x-dlt-source-partition`, `x-dlt-source-offset` | Where the record was, on Kafka. |
| `x-dlt-source-queue`      | The queue the message was consumed from, on RabbitMQ. |

You can inspect dead-lettered messages by subscribing to the DLQ topic like any
other listener:

```python
@message_listener(topic="payments.DLQ", group="dlq-audit")
async def on_dead_letter(self, msg: Message) -> None:
    original = msg.headers.get("x-original-topic")
    exc = msg.headers.get("x-exception")
    print(f"DLQ: {original} failed with {exc}: {msg.value!r}")
```

On RabbitMQ a `dead_letter_topic` is a routing key on the adapter's exchange: when no
queue is bound to it, the publish is returned and the message goes to `<queue>.dlq`
instead, so a dead letter is never dropped.

On the **in-memory broker** the handler is invoked again in process, up to `retries`
times with the `retry_delay` back-off, and then, if `dead_letter_topic` is set,
republished there; without one the exception propagates to the publisher. With no
retries and no dead-letter topic the handler is registered unchanged.

---

## Delivery Guarantees

Kafka and RabbitMQ listeners consume through one **listener container**
(`pyfly.messaging.listener_container`), shared with the Kafka and RabbitMQ buses of
`pyfly.eda`. Delivery is **at-least-once**, with the semantics of Spring Kafka and
Spring AMQP:

* **Each delivery runs in a unit of work the container opens, and is acknowledged
  only after that unit committed**: the Kafka offset is committed, the AMQP message is
  acked. The listener's repository calls run in the unit, so everything one delivery
  writes commits together or not at all. The unit takes the settings of the listener's
  own `@transactional` (see [Listener Transactions](#listener-transactions)). An
  application without a data layer runs its listeners without a unit and acknowledges
  once they return; `pyfly.messaging.listener.transactional: false` does the same with
  one.
* **A failure is attempted again, after a back-off.** Kafka seeks the partition back
  to the record and pauses it for the delay, so the record is fetched again and the
  records after it keep their order. RabbitMQ holds the message for the delay, then
  republishes it to its own queue with the attempt count in the
  `x-pyfly-delivery-attempt` header and acks the original. Every failure counts against
  the same `retry.max-attempts`, a transient one (a lost connection, a pool or statement
  timeout, a deadlock, a lock, serialization or optimistic-locking conflict, which a
  repository raises as `OptimisticLockingFailureException`) included; a duplicate key
  (`DuplicateKeyException`) is not transient. With the defaults (5
  attempts, with 1 + 2 + 4 + 8 = 15 s of back-off between them), a database outage or
  failover longer than about 15 s dead-letters every delivery consumed during it (on
  RabbitMQ, every prefetched message runs through its attempts): size
  `pyfly.messaging.listener.retry.max-attempts` and `retry.max-delay` to outlast the
  failover you expect. A message the adapter cannot read is dead-lettered at once, and
  so is a failure whose type the container's `RetryPolicy` lists in `not_retryable`;
  a transient one only when the entry names its kind or a narrower type
  (`OptimisticLockingFailureException` or `ConcurrencyException` for an
  optimistic-locking conflict, `ConnectionError` for a lost connection), while a
  broader entry (`Exception`, `ConflictException`) leaves it its attempts. A failure
  is transient when the exception, or one it was raised `from`, is: an exception
  raised while handling an optimistic-locking conflict is not.
* **After the last attempt, the message is dead-lettered, then acknowledged.** When
  the dead-letter publish fails, nothing is acknowledged: the offset stays where it
  was, the AMQP message is requeued, and the dead letter is published again later.
  `pyfly.messaging.kafka.dlt.enabled: false` switches the Kafka dead-letter topic off:
  the record is then logged at `ERROR` and skipped after its last attempt.
* **Concurrency is bounded.** A Kafka consumer handles one record at a time. Each
  RabbitMQ consumer has a channel of its own with a prefetch of 20
  (`pyfly.messaging.rabbitmq.prefetch`), and the adapter's consumers share a limit on
  the handlers running at once, sized from the datasource's connection pool (one on
  SQLite; `pyfly.messaging.listener.concurrency` sets it). A backlog therefore never
  becomes more handlers than connections. A handler that needs a second connection
  while its delivery holds one (a `REQUIRES_NEW` service it calls, another datasource
  on the same pool) can still wait for the pool, up to the pool's timeout: set
  `concurrency` below the pool size for such listeners.
* **Stopping is graceful.** The adapters are
  [`CONSUMER_PHASE`](dependency-injection.md#lifecycle-phases) lifecycle beans: the application context
  stops them before any `@pre_destroy`. `stop()` stops fetching, waits for the
  deliveries in flight for `pyfly.messaging.listener.shutdown-timeout` (10 s), then
  cancels the rest. A cancelled delivery rolls back and is not acknowledged: its offset
  is not committed, its message goes back to the queue, and the next consumer gets it
  again. Keep the listener timeout below `pyfly.context.shutdown-timeout` (30 s).
* **Deliveries do not join the caller's transaction.** The containers run their work in
  tasks started with `pyfly.data.transaction.detached`, so a consumer started inside a
  request never inherits its unit of work.

What this does *not* give you is exactly-once processing. A message whose unit
committed can still be delivered again: the process can stop between the commit and
the acknowledgement, a Kafka rebalance can hand a partition over while its offsets
wait for the end of a poll, and a crash in the middle of a poll delivers again the
records of that poll that were done (at most `pyfly.messaging.kafka.max-poll-records`,
100, per partition). A listener with side effects that must not happen twice keys them
on `Message.offset` (with `topic` and `partition`) or `Message.message_id`, or records
what it applied in the same unit of work. Without a consumer `group`, a Kafka listener
commits no offset at all, so a restart delivers nothing again; the container warns
about it.

### Listener Transactions

The unit the container opens for a delivery takes the settings the listener's own
`@transactional` declares, so a listener runs as it would outside a container:

| The listener is declared                 | Its delivery runs |
|------------------------------------------|-------------------|
| without `@transactional`                 | in a `REQUIRED` unit on `pyfly.messaging.listener.datasource` (the default datasource unless it names another). |
| `@transactional` (`REQUIRED`, `MANDATORY`) | in a unit with the listener's isolation, read-only flag, timeout, rollback rules and datasource; the listener joins it. |
| `REQUIRES_NEW` or `NESTED`               | in the unit the listener's `@transactional` begins itself; the container opens none. |
| `SUPPORTS`, `NOT_SUPPORTED` or `NEVER`   | without a unit: each repository call gets a short one of its own. |
| wrapped in `@retry` (an in-process retry) | each attempt in a unit of its own, as it would outside a container; the container opens none (a unit around the retry would be left rollback-only by the first failure, and `@retry` would stop). |

The message is acknowledged after the listener returned, so after its unit committed,
in every case. A `SERIALIZABLE` listener runs at `SERIALIZABLE`, a `timeout` rolls the
delivery back when it runs over (and the delivery is attempted again), and a
`read_only=True` listener cannot write.

Listeners that share a delivery (several `@message_listener` methods on one Kafka topic
and group, several `@event_listener` patterns that match one event) share its unit when
each of them would run in it with its own settings. When they cannot (one is
`SERIALIZABLE` and another is not, one is `REQUIRES_NEW`, `NEVER` or `@retry`), the container
opens none and logs `listener_units_differ` naming them (at subscription for a Kafka
topic and group, at the first event that reaches them on an EDA bus). Each then runs as
its own `@transactional` declares, and the delivery is no longer one unit: when a later
listener fails, the message is delivered again to the listeners whose work committed.

A `@transactional` service the listener calls joins the delivery's unit too. When such
a service fails, it marks the unit rollback-only, whether or not the listener catches
its exception: the unit rolls back at the end (`UnexpectedRollbackError`), and the
delivery is attempted again, then dead-lettered. For a best-effort step (an audit
record, a notification), declare the service
`@transactional(propagation=Propagation.NESTED)`: its failure rolls back to its own
savepoint, and the delivery commits.

On a SQLite file database the unit holds the single write lock for the whole delivery,
work that is not database work (an HTTP call) included, so the application's other
writes wait for it, and a `REQUIRES_NEW` write from a service the listener calls raises
`IllegalTransactionStateError`. Keep slow work out of such listeners, declare the
listener itself `REQUIRES_NEW` (it then runs in its own unit only), or set
`pyfly.messaging.listener.transactional: false`.

Each delivery opens and commits a unit (a connection checkout, `BEGIN`, `COMMIT`) even
when the listener never touches the database. A high-throughput listener that does not
can save that with `pyfly.messaging.listener.transactional: false`.

Before 26.09.08 none of this held. The Kafka adapter kept aiokafka's auto-commit and
skipped a failed record, and `stop()` committed the offset of a handler it had just
cancelled. The RabbitMQ adapter rejected a failed message without requeue into a queue
with no dead-letter exchange, and ran a whole backlog at once with no prefetch. A
listener whose transaction failed once lost its message.

### Publishing from a Unit of Work

The guarantees above are the consuming side's. On the producing side, `publish()` on a
broker sends at once: called inside a `@transactional` method, it reaches the broker
before the unit commits and stays there when the unit rolls back, and a process that
dies between its commit and its publish never sends. That is a dual write, and the
adapters of `pyfly.messaging` do not remove it.

The transactional outbox of `pyfly.eda` does, for events published through the EDA
`EventPublisher` (see [Any broker, transactional](events.md#any-broker-transactional-pyflyedaoutboxenabled)).
With `pyfly.eda.outbox.enabled: true`, a publish on the Kafka, RabbitMQ or Redis bus
is appended to the outbox tables in the caller's unit of work and forwarded to the
broker after the commit:

* **A unit that rolls back publishes nothing.**
* **A unit that commits publishes at least once**: once in the normal path, again
  after a failed publish, and again when a process died between the broker publish
  and the settling of its delivery (after the lease, `pyfly.eda.outbox.forward.claim-timeout`).
  Every copy carries the event's id in the `x-pyfly-event-id` header: deduplicate on
  it, as a listener deduplicates what the container delivers again.
* **Order holds per claim, not per group**: the events are claimed and published in
  publication order, but a failed publish is attempted again after later events, and
  several processes forward side by side.
* **Several instances share the forwarding** of one outbox and one group, each event
  forwarded by one of them; after the last attempt of a broker outage the event waits
  in the outbox's dead letters.

A message published through `MessageBrokerPort` itself has none of this: publish it
after the commit (`pyfly.data.transaction.after_commit`) where losing it on a crash is
acceptable, or through the EDA publisher where it is not.

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

await broker.stop()    # Drains the records in flight, then stops the producer
```

#### Constructor

| Parameter            | Type  | Default            | Description |
|----------------------|-------|--------------------|-------------|
| `bootstrap_servers`  | `str` | `"localhost:9092"` | Comma-separated list of Kafka bootstrap servers. |
| `settings`           | `ListenerContainerSettings \| None` | defaults | Retry policy, unit of work, shutdown timeout. Keyword-only. |
| `auto_offset_reset`  | `str` | `"latest"`         | Where a group with no committed offset starts (`latest` or `earliest`). Keyword-only. |
| `dead_letter_suffix` | `str \| None` | `".DLT"`  | Suffix of the dead-letter topic; `None` logs and skips a record after its last attempt. Keyword-only. |
| `consumer_factory`, `producer_factory` | callables | aiokafka | Build the aiokafka clients (pass a `functools.partial` to add SSL or SASL settings). Keyword-only. |

#### Internal Behavior

* **Producer**: An `AIOKafkaProducer` is created on `start()` and sends
  messages with `send_and_wait()` for reliable delivery.
* **Consumers**: One `AIOKafkaConsumer` per (topic, group) pair, with
  `enable_auto_commit=False`, run by a `KafkaListenerContainer` in a task of its
  own. The handlers of one (topic, group) share the consumer (a subscription made
  after `start()` joins it), and each record goes to all of them in one unit of
  work.
* **Offsets**: committed after each poll's records are done, before a partition is
  sought back, when partitions are revoked in a rebalance, and at stop: always the
  offset after the last record whose unit committed or that was dead-lettered.
* **Headers**: Kafka headers are byte-encoded on publish and decoded back to
  strings on consume. Non-decodable header values fall back to hex
  representation.
* **Shutdown**: `stop()` stops every consumer gracefully and in parallel (see
  [Delivery Guarantees](#delivery-guarantees)), then stops the producer, so a
  handler in flight can still publish.

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
| `settings`       | `ListenerContainerSettings \| None` | defaults | Retry policy, unit of work, prefetch, concurrency, shutdown timeout. Keyword-only. |
| `dead_letter_exchange` | `str \| None` | `"<exchange_name>.dlx"` | The exchange a message goes to after its last attempt. Keyword-only. |
| `connection_factory` | callable | `aio_pika.connect_robust` | Opens the connection. Keyword-only. |

#### Internal Behavior

* **Connection**: Uses `aio_pika.connect_robust()` for automatic reconnection.
* **Exchange**: A durable direct exchange is declared on `start()`.
* **Publishing**: Messages are persistent (`delivery_mode=2`) and carry a
  `message_id`.
* **Queues**: Each subscription creates a durable queue. The queue name is the
  `group` parameter if provided, otherwise `"pyfly.{topic}"`. The queue is
  bound to the exchange with the topic as the routing key. Its dead-letter queue
  `<queue>.dlq` is bound to the dead-letter exchange with the queue name as the
  routing key.
* **Consumers**: Each subscription is consumed by a `RabbitListenerContainer` on a
  channel of its own, with publisher confirms, a `basic.qos` prefetch and manual
  acknowledgement: a message is acked after its unit of work committed, republished
  with its attempt count after a failure, or dead-lettered after its last attempt
  (see [Delivery Guarantees](#delivery-guarantees)).
* **Shutdown**: `stop()` cancels the consumers, waits for the deliveries in flight,
  requeues the ones it had to cancel, and closes the connection.

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
      prefetch: 20

    listener:
      shutdown-timeout: 10
      retry:
        max-attempts: 5
        initial-delay: 1.0
```

| Property                              | Default                            | Description |
|---------------------------------------|------------------------------------|-------------|
| `pyfly.messaging.provider`            | `"memory"`                         | Which adapter to use: `"kafka"`, `"rabbitmq"`, or `"memory"`. |
| `pyfly.messaging.kafka.bootstrap-servers` | `"localhost:9092"`            | Kafka bootstrap servers (comma-separated). |
| `pyfly.messaging.kafka.auto-offset-reset` | `"latest"`                    | Where a consumer group with no committed offset starts: `latest` or `earliest`. |
| `pyfly.messaging.kafka.dlt.enabled`   | `true`                             | Dead-letter a record after its last attempt; `false` logs it and skips it. |
| `pyfly.messaging.kafka.dlt.suffix`    | `".DLT"`                           | Suffix of the dead-letter topic. |
| `pyfly.messaging.rabbitmq.url`        | `"amqp://guest:guest@localhost/"`  | AMQP connection URL for RabbitMQ. |
| `pyfly.messaging.rabbitmq.prefetch`   | `20`                               | `basic.qos` prefetch of each consumer channel. |
| `pyfly.messaging.rabbitmq.dead-letter-exchange` | `"pyfly.dlx"`            | The exchange a message goes to after its last attempt (into `<queue>.dlq`). |
| `pyfly.messaging.kafka.max-poll-records` | `100`                          | The most records one poll returns; their offsets are committed when they are all done. |
| `pyfly.messaging.listener.transactional` | `true`                          | Run each delivery in a unit of work the container opens (see [Listener Transactions](#listener-transactions)). |
| `pyfly.messaging.listener.datasource` | *(default datasource)*             | The datasource of that unit, for a listener without `@transactional`. |
| `pyfly.messaging.listener.shutdown-timeout` | `10`                         | Seconds `stop()` waits for the deliveries in flight before it cancels them. |
| `pyfly.messaging.listener.concurrency` | *(pool size; 1 on SQLite)*        | The most RabbitMQ deliveries the adapter runs at once. Unset, it is sized from the datasource's pool, at most the prefetch; a value set here is used as it is. |
| `pyfly.messaging.listener.retry.max-attempts` | `5`                        | Deliveries of a failing message, the first included, whatever the failure (a transient one too). |
| `pyfly.messaging.listener.retry.initial-delay` | `1.0`                     | Seconds before the second attempt. |
| `pyfly.messaging.listener.retry.multiplier` | `2.0`                        | Factor each later delay grows by. |
| `pyfly.messaging.listener.retry.max-delay` | `30.0`                        | The longest delay between two attempts. |

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
