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
6. [EventHandler Callable](#eventhandler-callable)
7. [InMemoryEventBus](#inmemoryeventbus)
8. [Provider Selection](#provider-selection)
   - [The transactional outbox: `postgres` and `database`](#the-transactional-outbox-postgres-and-database)
   - [Any broker, transactional: `pyfly.eda.outbox.enabled`](#any-broker-transactional-pyflyedaoutboxenabled)
   - [The outbox on MongoDB: `pyfly.eda.outbox.store: mongo`](#the-outbox-on-mongodb-pyflyedaoutboxstore-mongo)
   - [Postgres: what privileges a serving process actually needs](#postgres-what-privileges-a-serving-process-actually-needs)
   - [Dead letters](#dead-letters)
9. [Declarative Decorators](#declarative-decorators)
   - [@event_publisher](#event_publisher)
   - [@publish_result](#publish_result)
   - [@event_listener](#event_listener)
10. [Application Events](#application-events)
    - [Built-in Lifecycle Events](#built-in-lifecycle-events)
    - [ApplicationEventBus](#applicationeventbus)
    - [ApplicationEventPublisher (injectable)](#applicationeventpublisher-injectable)
    - [@app_event_listener](#app_event_listener)
    - [Transaction phases](#transaction-phases)
    - [Domain events of aggregates](#domain-events-of-aggregates)
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
          +-- InMemoryEventBus      (single-process, local pub/sub)
          +-- KafkaEventBus         (Apache Kafka via aiokafka)
          +-- RedisStreamsEventBus   (Redis Streams via redis-py)
          +-- PostgresEventBus      (transactional outbox, LISTEN/NOTIFY wake-ups)
          +-- DatabaseEventBus      (transactional outbox on any SQL datasource)
          +-- RabbitMqEventBus      (RabbitMQ via aio-pika)
          +-- TransactionalEventPublisher  (any of the brokers above behind the transactional outbox)
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

`ErrorStrategy` says what a failed delivery leads to on the **outbox buses** (the `postgres` and `database`
providers), through `pyfly.eda.outbox.error-strategy`. It is an enum with five members:

```python
from pyfly.eda import ErrorStrategy
```

| Member              | Value              | On an outbox bus |
|---------------------|--------------------|------------------|
| `DEAD_LETTER`       | `"DEAD_LETTER"`    | **The default.** The failing subscription is attempted again after the retry policy's back-off (`pyfly.eda.listener.retry.*`: 5 attempts, 1 s doubling to 30 s), and after the last attempt the event is copied into the dead-letter table for it. |
| `RETRY`             | `"RETRY"`          | Attempted again after the back-off for as long as it fails; never dead-lettered. |
| `FAIL_FAST`         | `"FAIL_FAST"`      | Dead-lettered at the first failure. |
| `LOG_AND_CONTINUE`  | `"LOG_AND_CONTINUE"` | Logged at WARNING, and the delivery is done: not attempted again. |
| `IGNORE`            | `"IGNORE"`         | Logged at DEBUG, and the delivery is done. |

Whatever the strategy, a failure never stalls the group: each subscription of a delivery is settled on its
own (the ones that succeeded are not run again), and the group's other events are delivered meanwhile.

The Kafka and RabbitMQ buses do not read `ErrorStrategy`: they consume through the listener container of
`pyfly.messaging`, which attempts a failed delivery again after a back-off and then dead-letters it to the
broker (`<topic>.DLT`, the dead-letter exchange); see
[Kafka: keys, the dead-letter topic and the family envelope](#kafka-keys-the-dead-letter-topic-and-the-family-envelope)
and [Delivery Guarantees](messaging.md#delivery-guarantees). No bus retries in the publishing process, and
the in-memory bus propagates a handler's exception to the caller of `publish()`.

Before 26.09.08 this section described retries and dead letters that no bus implemented.

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

## Provider Selection

PyFly auto-configures the `EventPublisher` bean from the `pyfly.eda.provider`
property. All keys are optional; the defaults work for local development.

| Config key | Type | Default | Description |
|---|---|---|---|
| `pyfly.eda.provider` | `str` | `auto` | `auto \| memory \| kafka \| redis \| postgres \| database \| rabbitmq`. When `auto`, the strongest available broker library wins (kafka > postgres > redis > rabbitmq > memory). |
| `pyfly.eda.destinations` | `str` | `pyfly.events` | Comma-separated list of topics / streams / routing keys to consume from. |
| `pyfly.eda.group` | `str` | `pyfly-default` | Consumer group name (used as Kafka group ID, Redis consumer group, the outbox buses' consumer group, or RabbitMQ queue prefix). |
| `pyfly.eda.serialization-format` | `str` | `json` | Serialization format: `json`, `firefly-json`, `avro`, or `protobuf`. `firefly-json` writes the LaraFly (PHP) envelope shape for topics shared with a PHP service; both JSON serializers read both shapes. |
| `pyfly.eda.kafka.bootstrap-servers` | `str` | `localhost:9092` | Kafka bootstrap server list. |
| `pyfly.eda.kafka.partition-key-header` | `str` | `partition_key` | Envelope header consulted first for the record key; then `x-correlation-id`, then the event type (the rule every Firefly Kafka publisher applies). |
| `pyfly.eda.kafka.dlt.enabled` | `bool` | `true` | Dead-letter a record to `<topic><suffix>`, verbatim, when the serializer cannot read it or its handlers failed on every attempt, and only then commit its offset. `false` logs it and skips it. |
| `pyfly.eda.kafka.dlt.suffix` | `str` | `.DLT` | Suffix of the dead-letter topic. |
| `pyfly.eda.redis.url` | `str` | `redis://localhost:6379/0` | Redis connection URL. |
| `pyfly.eda.outbox.datasource` | `str` | the primary | The datasource of the outbox, by name: the `postgres` and `database` buses', and the transactional publisher's (`pyfly.eda.outbox.enabled`). |
| `pyfly.eda.outbox.url` | `str` | | Its URL instead: an alias resolved through the registry (the datasource with that URL, or a new datasource `eda` with the registry's pool settings). |
| `pyfly.eda.postgres.datasource` / `pyfly.eda.postgres.dsn` | `str` | | The same two keys for `postgres`; `dsn` is the key it always had. Before 26.09.08 `dsn` was required, and the bus opened a connection pool of its own. |
| `pyfly.eda.postgres.listen-dsn` | `str` | | A direct DSN for the LISTEN connection (behind a pooler in transaction mode); by default it is checked out of the datasource's pool. |
| `pyfly.eda.postgres.channel` | `str` | `pyfly_eda` | `pg_notify` channel name. |
| `pyfly.eda.postgres.auto-create-tables` / `pyfly.eda.outbox.auto-create-tables` | `bool` | what `ddl-auto` allows | Create the outbox tables when they are missing. Unset, the outbox does what `pyfly.data.relational.ddl-auto` lets every framework store do: create them under `create` and `create-drop`, only check them otherwise. When they all exist nothing is created, so a serving process needs no schema-creation right; `false` means the framework never issues DDL for them (the tables are only checked). The Mongo store creates its collections' indexes unless `pyfly.eda.outbox.auto-create-tables` is `false` (then it only checks them); `ddl-auto` is the relational schema's. |
| `pyfly.eda.outbox.poll-interval` | duration | `5s` | How often an idle relay polls (seconds, or `500ms`, `90s`, `5m`, `2h`). |
| `pyfly.eda.outbox.batch-size` | `int` | `100` | Deliveries claimed per round. |
| `pyfly.eda.outbox.claim-timeout` | duration | `300s` | The lease of a claim: a delivery a relay claimed and did not settle (the process died) is claimed again once it ends. A relay extends the lease before it runs a delivery that could outlast it (see below), so such a delivery waits up to its worst case plus `claim-timeout`. |
| `pyfly.eda.outbox.handler-timeout` | duration | `60s` | How long one handler may run before it is cancelled and the attempt counts as failed (`none`: no limit, and a handler that outlasts the lease may run twice at once). Must be shorter than `claim-timeout`. A delivery runs every matching handler of the group in turn, so its worst case is `handler-timeout` times their number; when that is longer than what is left of the lease, the relay extends the lease first, and the delivery runs. |
| `pyfly.eda.outbox.start` | `str` | `latest` | Where a consumer group that registers for the first time starts: `latest` (the events published from then on) or `earliest` (every event the outbox still holds for its destinations, then the ones published after; an event still in flight while the group registers can be missed: see *The `latest` boundary* under [The transactional outbox](#the-transactional-outbox-postgres-and-database)). |
| `pyfly.eda.outbox.error-strategy` | `str` | `DEAD_LETTER` | See [ErrorStrategy](#errorstrategy-enum). |
| `pyfly.eda.outbox.retention.delivered` | duration | `1h` | An event every group handled is deleted once older than this (`none`: kept). |
| `pyfly.eda.outbox.retention.max-age` | duration | | An event older than this is deleted with the deliveries still owed for it (a group that stopped consuming loses them; a WARNING says how many). Unset: never. |
| `pyfly.eda.outbox.retention.interval` / `retention.batch-size` | duration / `int` | `1m` / `1000` | How often a relay prunes, and how many events one statement deletes. |
| `pyfly.eda.outbox.notify` | `bool` | | LISTEN/NOTIFY wake-ups; unset: on when the datasource is PostgreSQL and the LISTEN connection can be opened (the asyncpg driver, or `pyfly.eda.postgres.listen-dsn`); otherwise the relay polls, with a warning. |
| `pyfly.eda.outbox.enabled` | `bool` | `false` | Put the transactional outbox in front of the provider's publisher (`kafka`, `rabbitmq`, `redis`, `memory`): a publish is part of the caller's unit of work and reaches the broker after the commit, at least once ([Any broker, transactional](#any-broker-transactional-pyflyedaoutboxenabled)). The `database` and `postgres` providers are the outbox already. |
| `pyfly.eda.outbox.store` | `str` | `auto` | The outbox store of the transactional publisher and the `database` bus: `sql` (the `pyfly_outbox_*` tables on `pyfly.eda.outbox.datasource`), `mongo` (the `pyfly_outbox_*` collections of the document database, on the document datasource: [The outbox on MongoDB](#the-outbox-on-mongodb-pyflyedaoutboxstore-mongo)) or `auto` (`mongo` when the application has a Mongo client, `pyfly.data.document.enabled: true`, and no relational datasource: none in its `DataSourceRegistry`, however it is configured, and no `outbox.datasource`/`outbox.url`; else `sql`). The `postgres` provider is the SQL store on PostgreSQL: `mongo` raises there. |
| `pyfly.eda.outbox.forward.destinations` | `str` | every destination | The destinations the transactional publisher sends through the outbox (comma-separated; `*`: every one). A publish to another destination goes to the broker after the commit, at most once. |
| `pyfly.eda.outbox.forward.group` | `str` | `pyfly.forward:<provider>` | The forwarder's consumer group: the processes of one group split the forwarding, so they must forward to the same broker. |
| `pyfly.eda.outbox.forward.*` | | the `outbox.*` value | The forwarder's `poll-interval`, `batch-size`, `claim-timeout`, `handler-timeout` (the longest one publish may take), `start`, `error-strategy` and `retention.*`, each defaulting to the `pyfly.eda.outbox.*` key of the same name. |
| `pyfly.eda.outbox.forward.retry.*` | | `listener.retry.*` | The forwarder's retry policy: `max-attempts`, `initial-delay`, `multiplier`, `max-delay`, each defaulting to `pyfly.eda.listener.retry.*`. |
| `pyfly.eda.domain-events.enabled` | `bool` | `true` | Publish the events aggregates raise as their unit of work commits ([Domain events of aggregates](#domain-events-of-aggregates)). |
| `pyfly.eda.domain-events.destination` | `str` | | Also publish them through the event publisher, to this destination. |
| `pyfly.eda.rabbitmq.url` | `str` | `amqp://guest:guest@localhost/` | AMQP connection URL. |
| `pyfly.eda.rabbitmq.exchange-name` | `str` | `pyfly` | Name of the durable DIRECT exchange to declare. |
| `pyfly.eda.rabbitmq.prefetch` | `int` | `20` | `basic.qos` prefetch of each consumer channel. |
| `pyfly.eda.rabbitmq.dead-letter-exchange` | `str` | `<exchange-name>.dlx` | The exchange an event goes to after its last attempt, into the queue `<group>.<destination>.dlq`. |
| `pyfly.eda.kafka.max-poll-records` | `int` | `100` | The most records one poll of the Kafka bus returns; their offsets are committed when they are all done. |
| `pyfly.eda.listener.*` | | | The Kafka, RabbitMQ and outbox buses' listener container, with the keys and defaults of `pyfly.messaging.listener.*`: `transactional`, `datasource`, `shutdown-timeout`, `concurrency`, `retry.max-attempts` (5), `retry.initial-delay` (1.0), `retry.multiplier` (2.0), `retry.max-delay` (30.0). `concurrency` applies to the Kafka and RabbitMQ buses: an outbox relay handles its deliveries one at a time (run more processes of the group to handle more at once). See [Delivery Guarantees](messaging.md#delivery-guarantees). |

### Example configuration

```yaml
# pyfly.yaml
pyfly:
  eda:
    provider: "rabbitmq"
    destinations: "orders,payments"
    group: "order-service"
    rabbitmq:
      url: "amqp://user:pass@rabbitmq:5672/"
      exchange-name: "myapp"
```

### Kafka: keys, the dead-letter topic and the family envelope

Every record `KafkaEventBus.publish()` produces carries a **partition key**, so two events of one
aggregate land on one partition and are consumed in order. The key is `headers["partition_key"]`,
else `headers["x-correlation-id"]`, else the event type — the rule the LaraFly publisher applies,
so a topic written from both runtimes is keyed the same way. Pass `key=` to override it for one
call, or `partition_key_header` (`pyfly.eda.kafka.partition-key-header`) to consult a different
header first.

The bus consumes through the listener container it shares with `pyfly.messaging` (see
[Delivery Guarantees](messaging.md#delivery-guarantees)): auto-commit is off, the handlers that
match one record run in one unit of work the container opens with their `@transactional` settings
(see [Listener Transactions](messaging.md#listener-transactions)), and the record's offset is
committed only after that unit committed. A handler failure seeks the
partition back and attempts the record again after a back-off (`pyfly.eda.listener.retry.*`);
after the last attempt the record is **dead-lettered**. So is, at once, a record whose body the
serializer cannot read. Dead-lettering republishes the record verbatim (bytes, key and headers) to
`<topic>.DLT` with `x-dlt-reason`, `x-dlt-source-topic`, `x-dlt-source-partition`,
`x-dlt-source-offset` and `x-dlt-attempts` headers, and only then commits its offset. The publish
is retried three times; when it still fails, the round is counted on `bus.dlt_publish_failures`
and the record stays uncommitted, to be dead-lettered again. Scrape `bus.dlt_published` too: a
silent dead-letter topic is a failure of its own. An `EdaDeadLetterStore` bean, when the
application defines one, also records every event whose handlers failed on every attempt. Once
the record is in the dead-letter topic, the store is best effort: a failure to record it there is
logged and counted on `bus.dead_letter_store_failures`, not retried (that would only publish more
copies). With `pyfly.eda.kafka.dlt.enabled: false` the store is the only copy, and the record
stays uncommitted until the store takes it. `stop()` waits for the record in flight
(`pyfly.eda.listener.shutdown-timeout`) and never commits the offset of one it had to cancel.
Before 26.09.08 a failing handler was logged and skipped while auto-commit committed past it, and
a stop committed the offset of the handler it had just cancelled.

The RabbitMQ bus consumes each destination's queue through the same container: a channel of
its own with a prefetch of 20, a concurrency limit sized from the datasource pool, a bounded
number of attempts republished with a delay, and then the dead-letter queue
`<group>.<destination>.dlq` behind `<exchange-name>.dlx`. A message the serializer cannot read
goes there at once. The event is in the dead-letter queue before an `EdaDeadLetterStore` records
it, so a store that fails is logged and counted on `bus.dead_letter_store_failures`, and the
message is acked all the same. Before 26.09.08 the bus requeued a failing message at once and
forever, and ran a whole backlog at the same time.

Both buses start consuming only once a handler has subscribed. The application context starts
the bus before it subscribes the `@event_listener` methods; before 26.09.08 an event delivered in
between matched no handler and was acknowledged, lost. The Kafka consumer joins its group when
the bus starts, and the RabbitMQ queues are declared then, so events published meanwhile wait for
the handlers. After `stop()`, a `publish()` (from a `@pre_destroy`) opens a producer or connection of
its own and closes it again; it never restarts the consumers.

The JSON serializer **reads both envelope shapes** of the Firefly family — PyFly's snake_case
(`event_id`, `event_type`) and LaraFly's camelCase (`eventId`, `eventType`, with PHP's `[]` for an
empty object) — and raises one typed `EnvelopeDecodeError` for anything else. To *write* the
LaraFly shape (its key order, `DATE_ATOM` timestamps), select `serialization-format: firefly-json`.

### The transactional outbox: `postgres` and `database`

The `postgres` and `database` providers are one bus, `DatabaseEventBus` (`PostgresEventBus` keeps the
constructor the Postgres adapter always had): a **transactional outbox** on a datasource of the application's
`DataSourceRegistry`, with that datasource's pool, connect arguments and dialect setup. `database` runs on any
SQL backend (SQLite, PostgreSQL, MySQL, MariaDB); on PostgreSQL both use LISTEN/NOTIFY as a wake-up. In a MongoDB
application (`pyfly.eda.outbox.store: mongo`, or `auto` without a relational datasource) the `database` bus keeps its
outbox in the document database instead ([The outbox on MongoDB](#the-outbox-on-mongodb-pyflyedaoutboxstore-mongo)):
the tables and statements below are the SQL store's, the guarantees are both stores'.

- **A publish is part of the publisher's unit of work.** `publish()` writes the event into
  `pyfly_outbox_events` in the unit bound for the outbox's datasource (a short unit of its own outside one):
  a `@transactional` method that rolls back published nothing, and one that commits cannot lose its event.
  That holds when the outbox is on the datasource of the business unit (the default: the primary
  datasource). An outbox on another datasource (`pyfly.eda.outbox.datasource`) is written in a unit of that
  datasource, which commits on its own: the event and the business changes are then two writes again.
  The event is owed to every consumer group registered for its destination (a row in
  `pyfly_outbox_deliveries` per group), and to the publishing process's own group when that process consumes
  the destination (a handler subscribed), whether or not its relay has registered the group yet. On PostgreSQL
  a publish is one statement, `NOTIFY` included, and the server delivers the notification only if the unit
  commits. A publish never starts the bus: after `stop()` (from a `@pre_destroy` method) the event is still
  written, for this or another process to deliver.
- **Deliveries are claimed by state.** A group's relay claims the delivery rows that are due, for a lease
  (`claim-timeout`), with `FOR UPDATE SKIP LOCKED` where the backend has it (PostgreSQL, MySQL 8, MariaDB
  10.6) and an optimistic update elsewhere. An event whose transaction commits after a later one's is claimed
  when it becomes visible; several processes of one group share the deliveries without taking one twice; the
  deliveries of a process that dies are claimed again when their lease ends. At-least-once: a handler may see
  an event twice (after a crash between its commit and the settling), so deduplicate on
  `envelope.event_id`. A relay settles the deliveries it handled together, in one statement per batch
  (`batch-size`), so a relay that dies in the middle of a batch has that batch handled again.
- **The lease covers what a delivery may take.** A delivery runs every matching handler of the group in turn,
  each for at most `handler-timeout`. The first delivery of a batch always runs: when its worst case is longer
  than what is left of the lease, the relay first extends the lease of the batch to `claim-timeout` past that
  worst case. A later delivery that no longer fits in the lease is given back with the rest of the batch, where
  each was, for a fresh claim (this relay's or another's). No delivery runs past its lease while its relay
  lives, and none is held back: a group with many handlers for one event type (six under the default timeouts)
  used to stall on that event, and on every event behind it.
- **No order guarantee.** A relay handles a batch in publication order, but a failed delivery is attempted
  again after later events, and the processes of a group claim side by side: consume events as independent
  facts, or order them yourself (a version number in the payload).
- **Each subscription is settled on its own.** A delivery runs every handler of the group whose pattern
  matches; the ones that succeed are recorded, and a failing one is attempted again alone, after the retry
  policy's back-off, then dead-lettered ([ErrorStrategy](#errorstrategy-enum)). The group's other events go on
  meanwhile. A handler runs in the unit of work its `@transactional` gives it, as on the Kafka and RabbitMQ
  buses (`pyfly.eda.listener.transactional`), and is cancelled after `handler-timeout`. A handler that raises a
  `CancelledError` of its own (it awaited a future or a task another task cancelled) fails its delivery like
  any other exception; only a stop cancels the relay. When several handlers of one event fail, the event is
  attempted again after the shortest of their back-offs, and each of them runs again then (a handler with a
  longer back-off is attempted early, and its attempts are counted as usual).
- **Consumer groups.** A process's group is registered (for `pyfly.eda.destinations`) by its relay, in a
  short unit of its own, once a handler subscribes: a process that only publishes owes nothing to its own
  group, and a publish never registers a group (in the publisher's unit it raced the relays registering the
  same group, and on MariaDB failed the business unit). A new group starts at `pyfly.eda.outbox.start`
  (`latest`); when the processes of a new `earliest` group start at once, the first registration owes it the
  events the outbox holds, and the others find the group registered. Every process of a group must subscribe
  the same handlers (each event goes to one of them) and consume the same `pyfly.eda.destinations`: a
  registration makes the group's destinations its own list, so the last process to register decides what the
  whole group is owed, and processes that register different lists at once (a rolling deploy that changes the
  setting) can leave all of them registered. An event is owed to a group once, whichever of its destinations
  match. A group registered for every destination is never given the events of the event sourcing
  `TransactionalOutbox` (destinations `eventsourcing.outbox:<name>`), which share the tables.
- **The `latest` boundary.** A publish owes its event to the groups its unit sees registered. On MySQL and
  MariaDB a unit reads them in the snapshot of its first read (`REPEATABLE READ`): a group another process
  registers for the first time while the unit runs is owed the events published after it, not that one. On
  PostgreSQL (`READ COMMITTED`) the publish reads the groups registered when it runs; a unit declared
  `REPEATABLE READ` or `SERIALIZABLE` reads them in its snapshot, as on MySQL. The publishing process's own
  group is always owed its events (see above). An `earliest` group has the same boundary: the events the
  outbox holds when its registration reads them are owed to it, and so are the ones whose publish sees it
  registered, but an event whose unit is in flight meanwhile can be neither (its publish read the groups
  before the registration committed, and the registration read the events before that unit committed).
- **Retention.** The relays delete, in batches, the events every group handled (`retention.delivered`), and
  with `retention.max-age` the older ones whatever is still owed for them. The sweep of handled events reads
  past the events still owed: a group that stopped consuming keeps its backlog, and every sweep (every
  `retention.interval`, in every relay) reads it again. Remove such a group (`bus.outbox.unregister(group)`),
  or bound the backlog with `retention.max-age`.
- **Lifecycle and health.** `start()` checks (and creates) the tables, opens the LISTEN connection when a
  handler is subscribed, and starts the relay, serialized and idempotent; a failed start closes what it
  opened. A bus that only publishes opens no LISTEN connection; one whose handlers subscribe after it started
  opens it from its relay. The relay is a `CONSUMER_PHASE` lifecycle bean: the context stops it before any
  `@pre_destroy`, after the delivery in flight (`pyfly.eda.listener.shutdown-timeout`). The LISTEN connection
  is kept alive by the relay's polls and reopened when it is lost; meanwhile the relay polls every
  `poll-interval`, and the EDA health indicator stays `UP` (the events are still delivered, and a `DOWN` would
  pull a serving process out of its readiness and liveness probes) with `"listener": "reconnecting"` and a
  `degraded` detail saying since when. It is `DOWN` when the bus is not running, its database does not
  answer, or its relay's task ended while the bus runs (cancelled by something other than a stop: nothing more
  is delivered in the process; the reason and the relay's last error are in the details). `stop()` never
  raises what ended the relay's task: it logs it. The LISTEN connection uses asyncpg's listener API: on a
  datasource with another driver (psycopg) the bus polls, with a warning, unless
  `pyfly.eda.postgres.listen-dsn` gives it a connection of its own (`pyfly.eda.outbox.notify: true` then
  refuses to start). A publish after `stop()` on a `PostgresEventBus` given a URL outside an application
  context builds its private registry for that publish and closes it again.

Before 26.09.08 the Postgres bus wrote on a pool of its own, outside the business transaction, and consumed
by an id cursor: an event that committed after a higher id had been consumed was skipped for good (about 0.2 %
of the events under ordinary concurrency), a failing handler stalled its whole group and re-ran the others on
every retry, the table was never pruned, a new group replayed the whole history, concurrent first publishes
raced the DDL and leaked pools, a publish after `stop()` restarted everything, and a lost LISTEN connection was
never reopened while the health indicator said UP.

**Upgrading from 26.09.07.** The bus no longer reads `pyfly_eda_outbox` and `pyfly_eda_offsets`. Let every
consumer group drain them before the upgrade (or copy what a group had not consumed into
`pyfly_outbox_events` and its deliveries), then drop them.

**The store is a port.** The relay, both buses and the event-sourcing `TransactionalOutbox` read and write
through `OutboxStore` (`pyfly.eda.ports.outbox`): append in the caller's unit, register consumer groups, claim by
state for a lease, settle fenced by the claim, release, retention. `SqlOutboxStore` (`pyfly.eda.outbox`; its name
before 26.09.08, `Outbox`, is kept as an alias) is the SQL adapter these sections describe, and
`DatabaseEventBus(store=...)` and `TransactionalOutbox(publish, store=...)` run on any other, such as
`MongoOutboxStore` ([The outbox on MongoDB](#the-outbox-on-mongodb-pyflyedaoutboxstore-mongo)). The value types
(`Delivery`, `PendingDelivery`, `Retention`, `StartPosition`, `PruneResult`) are the port's, re-exported from
`pyfly.eda.outbox` unchanged. What an adapter must do is the contract suite `tests/support/outbox_contract.py`,
which the SQL store passes on SQLite, PostgreSQL, MySQL and MariaDB, and the Mongo store on a replica set.

### Any broker, transactional: `pyfly.eda.outbox.enabled`

Kafka, RabbitMQ, Redis Streams and the in-process bus publish at once. A `publish()` inside a `@transactional`
method reaches the broker before the unit commits, and stays there when the unit rolls back; a process that
dies between its commit and its publish loses the event. `pyfly.eda.outbox.enabled: true` puts the
transactional outbox in front of the provider's publisher, a `TransactionalEventPublisher`
(`pyfly.eda.outbox_forwarding`):

```yaml
pyfly:
  eda:
    provider: kafka
    kafka:
      bootstrap-servers: kafka:9092
    destinations: orders
    group: billing
    outbox:
      enabled: true             # the database and postgres providers are the outbox already
      forward:
        poll-interval: 1s       # how soon another process forwards what a dead one left
        retry:
          max-attempts: 10
```

- **A publish is part of the caller's unit of work.** `publish()` appends the event to the outbox store (the
  `pyfly_outbox_*` tables on `pyfly.eda.outbox.datasource`, the primary datasource by default) in the unit bound
  for that datasource, or in a short unit of its own outside one, owed to the forwarder's consumer group, and
  wakes the forwarder once that unit commits. Nothing reaches the broker before the commit. `subscribe()` and the
  consumption of events stay the broker's: `@event_listener` methods consume as they did, with the Kafka and
  RabbitMQ listener container.
- **The forwarder publishes after the commit.** `OutboxForwarder` is an outbox relay of the group
  `pyfly.forward:<provider>` (`pyfly.eda.outbox.forward.group`): it claims what the group is owed and publishes
  each event to the broker outside every unit of work, with the relay's leases, retries, dead letters and
  retention (`pyfly.eda.outbox.forward.*`). The publisher is a `CONSUMER_PHASE` lifecycle bean: `start()` starts
  the broker's bus, checks (or creates) the outbox tables and starts the forwarder; `stop()` stops the forwarder
  first, after the publish in flight, then the bus. A publish after `stop()` still appends its event, for another
  process or a restart to forward.
- **Domain events and command events are covered.** The publisher joins transactions (`joins_transactions`),
  so `pyfly.eda.domain-events.destination` and the CQRS command bus publish in the committing unit, as with an
  outbox bus.

What it guarantees:

- **A unit that rolls back publishes nothing**, and **a unit that commits publishes its event at least once**:
  once in the normal path; again after a publish that failed (after its back-off); and again when the process
  died after the broker took the event and before the delivery was settled, once the lease ends
  (`forward.claim-timeout`) and a forwarder (another process, or this one after a restart) claims it. Every copy
  carries the event's id in the `x-pyfly-event-id` header (a domain event's own id when it is one), the same on
  every attempt: a consumer deduplicates on it, for instance by recording it in the unit of work of its handler.

  ```python
  @event_listener(["order.*"])
  async def on_order(self, envelope: EventEnvelope) -> None:
      event_id = envelope.headers["x-pyfly-event-id"]
      if await self.handled.exists_by_id(event_id):   # a copy of an event handled already
          return
      await self.handled.save(HandledEvent(id=event_id))   # commits with the handler's own work
      ...
  ```

- **Order.** The events are claimed in publication order, and one forwarder at a time publishes a claim in that
  order, but a failed publish is attempted again after later events, and the forwarders of several processes
  claim side by side. Consume them as independent facts, or order them yourself (a version in the payload).
- **Several processes share the forwarding.** The processes whose forwarders run on one outbox with one group
  split its events, each forwarded by one of them; nothing is forwarded twice except after a lost lease (a
  crash, or a publish that outlasted `forward.claim-timeout`). They must forward to the same broker: give a
  deployment that forwards elsewhere from the same database a `forward.group` of its own.
- **A broker outage is retried, then dead-lettered.** Each attempt that fails (or outlasts
  `forward.handler-timeout`) is attempted again after the retry policy's back-off (`forward.retry.*`, by default
  `pyfly.eda.listener.retry.*`); after the last attempt the event goes to the outbox's dead letters
  (`await publisher.dead_letters()`), or to the application's `EdaDeadLetterStore` when it defines one as a bean
  (then `publisher.dead_letters()` stays empty: read that store), and the events behind it go on. With the
  defaults (5 attempts, 1 + 2 + 4 + 8 = 15 s apart) an outage longer than about 15 s dead-letters the events
  published during it: raise `forward.retry.max-attempts` and `retry.max-delay` to outlast the outages you expect,
  or set `forward.error-strategy: RETRY` to attempt every event until the broker is back. `LOG_AND_CONTINUE` and
  `IGNORE` settle a failed forward as done: the event is dropped, at most once.
- **The event commits in the store's unit.** With the outbox on the datasource of the business changes (the
  default), they commit together. An outbox on another datasource commits in a unit of that datasource, a second
  write. A MongoDB application keeps its outbox in its document database, and the event commits with the
  documents ([The outbox on MongoDB](#the-outbox-on-mongodb-pyflyedaoutboxstore-mongo)).

The forwarder's group is registered for `forward.destinations` (every destination by default, also when the list
holds `*`), so an event another writer of the store appends to one of them is forwarded too. A publish to a
destination left out of `forward.destinations` goes straight to the broker after the caller's unit commits (at
once outside one): at most once, and lost if the process dies in between. The group stays registered when
`outbox.enabled` is turned off, and every other writer of the same tables (an outbox bus) keeps owing it its
events: remove it (`await SqlOutboxStore("primary").unregister("pyfly.forward:kafka")`) when you switch the layer
off for good (on MongoDB, `await MongoOutboxStore("document", database="shop").unregister("pyfly.forward:kafka")`,
with the database of `pyfly.data.document.database`: without `database=` the store takes the one the client's URI
names, else `pyfly`).

The broker's bus builds the envelope it sends when the forwarder publishes: its `timestamp` is the instant of the
forward, not of the publish in the unit (the event's id is kept, in `x-pyfly-event-id`). With the in-process bus
(`provider: memory`) a forward is the whole fan-out to the `@event_listener` methods: when one of them raises, the
forward failed and is attempted again, so the listeners that succeeded receive the event again (and the ones after
the failing listener wait for the next attempt); handle it as the at-least-once delivery it is.

The health indicator reports the publisher `UP` while it runs, its forwarder's task runs, its outbox's database
answers and the broker's bus is up, and `DOWN` with the reason otherwise; the details count what was forwarded,
the failed attempts and the dead letters. `await publisher.pending()` lists what is not forwarded yet.

In a data test (`@DataTest`, `data_slice(..., rollback=True)`), the context's forwarder never sees what the test
publishes: forward it with a relay round of the test's own, which takes part in the test's transaction
(`await publisher.relay.run_once()`).

Without the auto-configuration:

```python
from pyfly.eda.adapters.kafka import KafkaEventBus
from pyfly.eda.outbox import SqlOutboxStore
from pyfly.eda.outbox_forwarding import TransactionalEventPublisher

publisher = TransactionalEventPublisher(
    KafkaEventBus(bootstrap_servers="kafka:9092", topics=["orders"], group="billing"),
    SqlOutboxStore("primary"),          # the tables on the primary datasource
    name="kafka",                       # the forwarder's group: pyfly.forward:kafka
    poll_interval=1.0,
)
await publisher.start()
```

### The outbox on MongoDB: `pyfly.eda.outbox.store: mongo`

A MongoDB application keeps its outbox beside its documents. `MongoOutboxStore` (`pyfly.eda.adapters.mongo_outbox`)
is the outbox store on collections of the document database, for the transactional publisher
(`pyfly.eda.outbox.enabled`) and for the `database` bus (`provider: database`). `pyfly.eda.outbox.store: auto` picks
it for an application with the document layer (`pyfly.data.document.enabled: true`) and no relational datasource;
`mongo` asks for it in any application with the document layer.

```yaml
pyfly:
  data:
    document:
      enabled: true
      uri: mongodb://mongo:27017/?replicaSet=rs0
      database: shop
  eda:
    provider: kafka
    kafka:
      bootstrap-servers: kafka:9092
    outbox:
      enabled: true      # store: auto picks mongo, as there is no relational datasource
```

Its guarantees are the SQL store's, and so are the sections above: the same contract suite
(`tests/support/outbox_contract.py`) runs on both. A unit that rolls back published nothing; a unit that commits
publishes its event at least once, with its id in `x-pyfly-event-id`; deliveries are claimed by state for a lease,
and every later write on a claim is fenced by it; the processes of a group share its deliveries; retention and dead
letters work alike. What differs is where the data lives, and why:

- **The event commits with the documents.** A publish joins the unit of work of the document datasource
  (`pyfly.data.document.datasource`, `document`; in a document-only application the one a bare `@transactional`
  runs on): the event and its deliveries are written in that unit's MongoDB transaction, so a `@transactional`
  method that saves documents and publishes commits both or neither. Outside a unit, the publish runs in a short
  transaction of its own, and so does a publish in a unit that runs no transaction: the one a single-command
  repository write opens outside `@transactional`. A `MongoRepository.save` there writes the document on its own,
  and the events of its aggregate, published one by one as that write's unit commits, are each written with their
  deliveries in a short transaction of the store's own, so no event is ever owed to no group. They are separate
  transactions, not one: when a later event fails, the earlier ones stand and are delivered, and the save raises,
  its document stored. When an aggregate's events must commit together (and with its document), save it in a
  `@transactional` method. A read-only unit refuses the publish (`IllegalTransactionStateError`), as a
  `MongoRepository` refuses a write there. In an application with both layers `auto` stays on the SQL store: set
  `store: mongo` to have the events commit with the document units instead (an event published in a relational
  unit is then written in a unit of the document datasource, a second write).
- **A replica set.** The store writes an event and its deliveries in one multi-document transaction (the caller's
  unit's, or one of its own), which MongoDB runs on a replica set or a sharded cluster only. Its `start()` refuses a
  standalone server with `IllegalTransactionStateError`, so the publisher, and the application, fail to start rather
  than write events that could lose their deliveries. A single-node replica set is enough ([Replica Set
  Requirement](data-document.md#replica-set-requirement)). The store is tested on a replica set; on a sharded
  cluster, keep its five collections unsharded on one shard (the database's primary shard, where MongoDB puts an
  unsharded collection), so an event and its deliveries commit on one shard and a claim, which reads them outside a
  transaction, never sees a delivery before its event.
- **Collections and indexes.** Five collections of the document database (`pyfly.data.document.database`), created
  with their indexes when the store starts (idempotent: an index that exists is left as it is):

  | Collection | Holds | Indexes |
  |---|---|---|
  | `pyfly_outbox_events` | one document per event: `_id` (the outbox id), `event_id`, `destination`, `event_type`, `payload` and `headers` (JSON text), `created_at` | `created_at` |
  | `pyfly_outbox_deliveries` | what is still owed, per consumer group and event: `available_at` (due, or the claim's lease end), `attempts`, `claimed_by`, `done` (the subscriptions that handled it), `last_error` | unique `(consumer_group, outbox_id)`; `(consumer_group, available_at, outbox_id)`; `outbox_id` |
  | `pyfly_outbox_consumers` | the consumer groups and their destinations (`*`: every one) | unique `(consumer_group, destination)`; `destination` |
  | `pyfly_outbox_dead_letters` | the dead letters, as the SQL table holds them | `(consumer_group, failed_at)`; `failed_at` |
  | `pyfly_outbox_counters` | the counter the outbox ids come from | |

  With `pyfly.eda.outbox.auto-create-tables: false` the store only checks the indexes and fails to start naming a
  missing one. A serving process needs `readWrite` on the database (it includes `createIndex`).
- **Outbox ids.** An event's id is taken from the counter with one atomic `findAndModify`, outside the unit's
  transaction: two MongoDB transactions that increment one document conflict (the second is aborted at once), so a
  counter inside the transaction would fail every publishing unit that ran beside another one. The ids grow as a SQL
  identity column's do: a unit that rolls back leaves a gap, and a unit that commits late leaves a lower id behind
  higher ones. Neither loses an event: a delivery is claimed by its state when it becomes visible, never by an id
  cursor.
- **Claims.** A claim reads a group's due deliveries, oldest first, then moves them a lease ahead with one
  `updateMany` that matches only the ones still due, marked with the claim's token. MongoDB applies it to each
  document atomically, so two relays never take one delivery; a claim that lost some of what it read to another
  relay reads on past them, as `FOR UPDATE SKIP LOCKED` would. The writes of a claim, a completion, an extension, a
  release and a plain settle are commands MongoDB applies to each document atomically (a claim reads around its
  `updateMany`, and an extension may read which deliveries it still holds), so they need no transaction: they join
  the caller's unit when one is bound, and otherwise run in a unit of their own without one. A registration, an
  unregistration, a settle with dead letters and a retention batch are several writes that belong together, so they
  run in a transaction (the caller's unit's when it runs one, else the store's own), run again when MongoDB aborts
  one of the store's own for a write conflict (another process registering the same group at once).
- **A registration from `earliest`** owes the new group every event the outbox holds, in its one transaction (the
  events read a thousand at a time). On a very large outbox that transaction can outlast MongoDB's
  `transactionLifetimeLimitSeconds` (60 seconds by default): MongoDB then aborts it, and after running it again a
  few times the registration fails. Prune the outbox, or raise the limit, before a new group starts from `earliest`
  on a large backlog.
- **The payload is JSON**, as the SQL store keeps it: a consumer gets the same values from either store (an instant,
  a decimal or a UUID in a payload arrives as a string).
- **The client is the application's.** The store runs on the document datasource's client and units of work,
  resolved when it runs; it never closes the client (the context that built it does, when it stops). The
  transactional publisher and the `database` bus stop their store when they stop, which releases nothing, so they
  may share one.

Without the auto-configuration:

```python
from pyfly.eda.adapters.kafka import KafkaEventBus
from pyfly.eda.adapters.mongo_outbox import MongoOutboxStore
from pyfly.eda.outbox_forwarding import TransactionalEventPublisher

publisher = TransactionalEventPublisher(
    KafkaEventBus(bootstrap_servers="kafka:9092", topics=["orders"], group="billing"),
    MongoOutboxStore("document", database="shop"),   # or the AsyncMongoClient, or its MongoTransactionManager
    name="kafka",
)
await publisher.start()
```

`DatabaseEventBus(store=MongoOutboxStore(...))` is the `database` bus on it, and `TransactionalOutbox(publish,
store=MongoOutboxStore(...))` the event-sourcing outbox. The `postgres` provider is the SQL store on PostgreSQL:
`pyfly.eda.outbox.store: mongo` raises there. The LISTEN/NOTIFY wake-ups are PostgreSQL's: the relays poll, and a
publish in the same process wakes them after its commit; `pyfly.eda.outbox.notify: true` makes the `database` bus
fail to start on the Mongo store.

### Postgres: what privileges a serving process actually needs

Postgres checks `CREATE` on the schema *before* it checks `IF NOT EXISTS`, so replaying `CREATE TABLE IF NOT
EXISTS` at every boot, as the adapter used to, needed schema-creation rights for work it never did. The outbox
tables are framework tables (`pyfly.data.relational.framework_schema`): `start()` creates the ones that are
missing, and when they all exist it only reads the catalog, which needs nothing beyond `USAGE` on the schema. It
creates them as every framework store creates its tables, when `pyfly.data.relational.ddl-auto` is `create` or
`create-drop`; with `none` or `validate` it only checks them, and migrations create them (list
`framework_metadata` in Alembic's `target_metadata`). Set `pyfly.eda.postgres.auto-create-tables` to `true` or
`false` to decide for the outbox alone: `false` means the framework must not issue DDL for it under any
circumstance.

A serving process therefore needs only:

```sql
GRANT USAGE ON SCHEMA public TO serving;
GRANT SELECT, INSERT, UPDATE, DELETE
  ON pyfly_outbox_events, pyfly_outbox_deliveries, pyfly_outbox_consumers, pyfly_outbox_dead_letters
  TO serving;
```

`DELETE` is new: a handled delivery row is deleted, and retention deletes the events every group handled.
The identity column of `pyfly_outbox_events` needs no grant on its sequence. Before 26.09.08 the grant was
`SELECT, INSERT, UPDATE` on `pyfly_eda_outbox` and `pyfly_eda_offsets` plus `USAGE` on
`pyfly_eda_outbox_id_seq`.

### Dead letters

An `EdaDeadLetterStore` records the events whose handlers failed on every attempt. The outbox buses write
theirs to `pyfly_outbox_dead_letters` (a table, or a collection on the Mongo store) in the unit that settles the
delivery, with the consumer group, the
subscription, the event and the last failure; `bus.outbox.dead_letters(group)` reads them. So does the forwarder
of a transactional publisher, for the events it failed to publish on every attempt
(`publisher.dead_letters()`), unless the application defines the bean below. The Kafka and
RabbitMQ buses dead-letter to their broker and record the event in the store the application defines as a
bean, after the broker has it (best effort there: a failure is logged and counted, not retried, except that
with the Kafka dead-letter topic off the store is the only copy and the record waits for it).
`SqlEdaDeadLetterStore` is the durable store, on the same table of any datasource:

```python
from pyfly.container import bean, configuration
from pyfly.eda.dlq import EdaDeadLetterStore, SqlEdaDeadLetterStore


@configuration
class DeadLetters:
    @bean
    def dead_letters(self) -> EdaDeadLetterStore:
        return SqlEdaDeadLetterStore("primary")
```

Given such a bean, the outbox buses and the forwarder hand their dead letters to it instead of writing them in
the settling unit.

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
| `ContextClosedEvent`     | The application is shutting down: the first step of `stop()`, while every bean (the datasources included) still works. | Flush buffers, close connections, save state. |

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
    owner: object | None = None,  # Optional: the bean it belongs to (unsubscribed when destroyed)
    phase: TransactionPhase | None = None,  # Optional: run at this transaction phase (see below)
)
```

### ApplicationEventPublisher (injectable)

*(v26.06.41)* You rarely need to touch the `ApplicationEventBus` directly. The
`ApplicationContext` registers an `ApplicationEventPublisher` as a singleton
bean wired to the same bus, so any bean can fire application events simply by
injecting it -- the Spring `ApplicationEventPublisher` equivalent.

```python
from pyfly.container import service
from pyfly.context import ApplicationEventPublisher


@service
class OrderService:
    def __init__(self, events: ApplicationEventPublisher) -> None:
        self._events = events

    async def place(self, order_id: str) -> None:
        # ... persist the order ...
        await self._events.publish(OrderPlacedEvent(order_id))
```

The publisher exposes a single async method:

```python
async def publish(self, event: object) -> None: ...
```

`publish()` accepts **any object** -- a built-in lifecycle event
(`ApplicationReadyEvent`, etc.) or an arbitrary domain event of your own. It
delegates straight to the underlying `ApplicationEventBus`, which dispatches
to every listener whose subscribed type matches the event via `isinstance`.

`ApplicationEventPublisher` is importable from either `pyfly.context` or
`pyfly.context.events`.

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
type on the `ApplicationEventBus`. The first type-annotated parameter wins; the
return annotation is ignored.

You can define **multiple** `@app_event_listener` methods in the same class,
each listening for a different event type.

#### Listening for arbitrary events

*(v26.06.41)* The inferred event type does **not** have to be an
`ApplicationEvent` subclass. The annotated parameter type may be any class, and
the listener is invoked whenever the published object satisfies `isinstance`.
Combined with the injectable `ApplicationEventPublisher`, this lets you use the
context event bus as a lightweight, in-process domain-event dispatcher:

```python
from dataclasses import dataclass

from pyfly.container import service
from pyfly.context import ApplicationEventPublisher
from pyfly.context.events import app_event_listener


@dataclass
class OrderPlacedEvent:  # a plain object, not an ApplicationEvent subclass
    order_id: str


@service
class FulfillmentService:

    @app_event_listener
    async def on_order_placed(self, event: OrderPlacedEvent) -> None:
        print(f"Fulfilling order {event.order_id}")


@service
class OrderService:
    def __init__(self, events: ApplicationEventPublisher) -> None:
        self._events = events

    async def place(self, order_id: str) -> None:
        await self._events.publish(OrderPlacedEvent(order_id))
```

> Note: listeners may also be plain (non-`async`) methods -- the bus awaits the
> result only when it is awaitable, so a synchronous `def` listener will not
> break startup.

### Transaction phases

A listener runs inline in `publish()`, inside the caller's transaction when there is one: a listener that
sends an e-mail would send it even if that transaction then rolls back. Declare the transaction phase it runs
at instead (Spring's `@TransactionalEventListener`):

```python
from pyfly.context.events import app_event_listener
from pyfly.data.transaction import TransactionPhase


@service
class Notifications:
    @app_event_listener(phase=TransactionPhase.AFTER_COMMIT)
    async def send_receipt(self, event: OrderPlacedEvent) -> None:
        await self.mailer.send(...)  # only once the order committed
```

| Phase | Runs |
|-------|------|
| `BEFORE_COMMIT` | Inside the unit of work the event was published in, right before it commits; an exception rolls it back. |
| `AFTER_COMMIT` | Once the unit committed, outside it (repository calls get units of their own); not when it rolls back. |
| `AFTER_ROLLBACK` | Only once the unit rolled back. |
| `AFTER_COMPLETION` | Once the unit completed, either way. |

Outside a transaction every phase but `AFTER_ROLLBACK` runs at once, and an `AFTER_ROLLBACK` listener does
not run. An `AFTER_*` listener's exception is logged and counted (`pyfly.tx.synchronization.failures`),
never raised: the unit has completed. For a delivery guarantee, publish through an outbox bus instead.

### Domain events of aggregates

The events a `pyfly.domain.AggregateRoot` raises are published when the unit of work that persists it
commits, by the `DomainEventPublisher` the EDA auto-configuration registers
(`pyfly.eda.domain-events.enabled`):

- to the application's listeners: a plain `@app_event_listener` runs inside the unit, right before it
  commits; one with a phase runs at that phase;
- with `pyfly.eda.domain-events.destination`, through the EDA event publisher too, as
  `publish(destination, event.event_type, event.to_payload(), headers)` with the headers
  `x-pyfly-event-id`, `x-pyfly-aggregate-type` and `x-pyfly-aggregate-id`. An outbox bus on the aggregate's
  datasource writes them in the committing unit itself, so they are published exactly when the aggregate's
  changes are (an outbox on another datasource commits them in a unit of its own, just before); a broker bus
  gets them after the commit.

It collects every event an aggregate raises inside a unit of work, and the pending events of an aggregate a
relational unit of work saves (`Repository.save` of a new or detached aggregate: events raised in a factory,
before any unit existed). A unit that rolls back publishes nothing. Raise the events on the instance the unit
holds (the one `save()` returns): events raised outside a unit on a detached copy that `save()` merges into an
instance the unit already holds stay pending on the copy (a DEBUG record, `domain_event_pending_outside_unit`,
is logged when an event is raised outside a unit). `DomainEventPublisher.publish(*aggregates)` publishes by
hand, inside the unit; without the EDA auto-configuration, register a
`DomainEventPublisher(ApplicationEventPublisher)` bean (an application's own `DomainEventPublisher` bean
replaces the auto-configured one).

**Upgrading from 26.09.07: a behavior change.** The `DomainEventPublisher` is on by default and drains an
aggregate's pending events as its unit of work commits. Without `pyfly.eda.domain-events.destination` it hands
them to the application's listeners only. An application that published its domain events itself, collecting
`pending_events()` (or calling `clear_events()`) after the unit and handing them to the EDA bus, now finds the
buffer empty and silently stops publishing those integration events. Either set
`pyfly.eda.domain-events.destination` (the publisher then sends them through the EDA bus, in the unit, and the
application code that did it goes), or set `pyfly.eda.domain-events.enabled: false` to keep publishing by hand.

---

## Events vs. Messaging: When to Use Which

PyFly provides both an EDA module (`pyfly.eda`) and a messaging module
(`pyfly.messaging`). Here is how to choose:

| Criterion | Domain Events (`pyfly.eda`) | Messaging (`pyfly.messaging`) |
|-----------|----------------------------|-------------------------------|
| **Scope** | In-process by default; also distributed via the broker buses. | Cross-process, cross-service, distributed. |
| **Transport** | `InMemoryEventBus` (direct calls) or a broker-backed bus: Kafka, Redis Streams, Postgres, RabbitMQ. | Kafka, RabbitMQ, or other external brokers. |
| **Payload** | `EventEnvelope` with typed `dict` payload. | Raw `bytes` -- you choose the serialization format. |
| **Pattern** | Glob-matched event types (`"order.*"`). | Topic-based with consumer groups. |
| **Durability** | In-memory bus: none (events lost if the process dies). Broker buses: durable + at-least-once (Redis Streams, RabbitMQ, Kafka with a consumer group). Outbox buses (`postgres`, `database`): transactional, published with the publisher's unit of work, then at-least-once. | At-least-once on Kafka and RabbitMQ: acknowledged after the listener's unit of work commits (see [Delivery Guarantees](messaging.md#delivery-guarantees)). |
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
