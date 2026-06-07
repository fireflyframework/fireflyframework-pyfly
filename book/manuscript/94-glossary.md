<span class="eyebrow">Appendix</span>

# Glossary {.chtitle}

**Adapter** — A concrete class that implements a port by delegating to a specific library or infrastructure technology (PostgreSQL, Redis, Kafka, etc.). Adapters live at the edge of the hexagonal architecture and can be swapped without touching the domain or application layers. In PyFly, the async SQLAlchemy session factory, `RedisCacheAdapter`, and `KafkaMessageBroker` are all adapters (Chapters 5, 10, 13).

**Aggregate root** — The single entry point to a cluster of domain objects that must stay consistent together; all state changes are routed through its methods, which enforce invariants and emit domain events. External code loads and saves only the root, never its inner objects. In PyFly, `AggregateRoot[ID]` is the base class; `Wallet` is Lumen's aggregate root (Chapter 6).

**AOP (Aspect-Oriented Programming)** — A technique for adding cross-cutting concerns — logging, metrics, security checks — declaratively to methods without modifying their source code. PyFly's `@aspect` and `@before`/`@after`/`@around` advice decorators implement AOP; Chapter 15 uses it to attach observability to every service method.

**ApplicationContext** — The central DI container that discovers beans, resolves dependencies, and manages their lifecycle from startup to shutdown. It fires `ContextRefreshedEvent` once all beans are wired and `ContextClosedEvent` on graceful shutdown. `ApplicationContext` is the PyFly equivalent of Spring's `ApplicationContext` (Chapter 2).

**Async/await** — Python's native coroutine syntax for non-blocking I/O. PyFly is built async-native: every HTTP handler, repository method, bus call, and service client is declared `async def` and scheduled on the event loop. Blocking calls inside an `async def` function freeze the loop and must be avoided (Chapter 1).

**Autowiring** — The mechanism by which the DI container resolves a bean's constructor arguments from type hints alone, with no factory code required. PyFly inspects `__init__` signatures at startup and injects matching beans automatically; the `@primary` annotation selects the preferred implementation when multiple candidates exist (Chapter 2).

**Bean** — Any Python object that the DI container creates, wires, and manages. You declare a class as a bean by applying a stereotype decorator (`@service`, `@repository`, `@component`, `@configuration`, or `@rest_controller`), or by annotating a factory method with `@bean` inside a `@configuration` class (Chapter 2).

**BFF (Backend for Frontend)** — An API gateway layer that sits in front of multiple microservices and composes their capabilities into a user-journey-focused API, tailored to a specific frontend client. In Chapter 11, Lumen's BFF tier aggregates wallet and payment data so the mobile app makes one request instead of two.

**Bounded context** — A DDD-level scope within which a domain model has a single, unambiguous meaning. Services in a microservices architecture often map one-to-one to bounded contexts: `WalletService` owns the wallet model; `PaymentsService` owns the payment model. Shared kernel or anti-corruption layers are needed when two contexts must exchange data (Chapter 6).

**Bulkhead** — A resilience pattern that limits the number of concurrent calls to a particular resource or downstream service, preventing a slow dependency from exhausting the thread or coroutine pool and degrading unrelated paths. PyFly's `@bulkhead(max_concurrent=N)` decorator implements the semaphore bulkhead (Chapter 13).

**Circuit breaker** — A resilience pattern that counts consecutive failures to a remote dependency; once the failure threshold is reached, the breaker trips to the open state and short-circuits further calls with a fast error, giving the dependency time to recover before calls resume. PyFly's `@circuit_breaker` decorator and `@service_client` inject this behaviour automatically (Chapters 11, 13).

**Command (CQRS)** — An immutable, frozen dataclass that expresses a single write intent — "open a wallet", "deposit funds" — and carries the exact data needed to fulfil it. Commands inherit from `Command[R]` where `R` is the handler's return type, and flow through the `CommandBus`. They may optionally implement `validate()` and `authorize()` (Chapter 7).

**CommandBus** — The pipeline that receives a `Command`, runs validation and authorization, dispatches it to the matching `CommandHandler`, and then publishes any domain events buffered by the aggregate. One handler is registered per command type; the bus enforces this constraint at startup (Chapter 7).

**Compensation** — A forward operation that semantically reverses the effect of a previously completed saga step when a later step fails. Unlike a database rollback, compensation is a new write that explicitly undoes the earlier change (for example, "refund payment" compensates "capture payment"). Each `@saga_step` names its compensation method (Chapter 12).

**Component** — A generic stereotype for a managed bean that does not fit the more specific roles of `@service`, `@repository`, or `@rest_controller`. `@component` registers the class with the container and enables injection, but carries no additional semantic meaning (Chapter 2).

**Configuration class** — A class decorated with `@configuration` that groups `@bean` factory methods. The container treats it as a source of bean definitions, calling each factory method and registering the return value as a named, typed bean. Profile guards and conditional annotations can be applied to the class or to individual factory methods (Chapter 3).

**Container (DI)** — See *ApplicationContext*.

**Convention over configuration** — The principle that sensible defaults eliminate boilerplate: a class annotated `@service` is automatically singleton-scoped, discovered by component scan, and injected by type without any XML or explicit registration. PyFly adopts this as a core design value, requiring explicit overrides only when the default is wrong (Chapter 1).

**CQRS (Command Query Responsibility Segregation)** — An architectural pattern that separates write operations (commands) from read operations (queries) into distinct code paths, buses, and potentially distinct data models. Reads can be cached independently of writes; handlers can be tested in isolation; cross-cutting concerns are applied uniformly by the respective bus (Chapter 7).

**Dead-letter queue (DLQ)** — A dedicated message queue or topic that receives messages which could not be processed after the configured number of retries. PyFly's `@message_listener` routes poison messages to the DLQ automatically, preventing them from blocking healthy messages (Chapter 10).

**Dependency injection (DI)** — A design pattern in which an object declares its collaborators as constructor parameters and an external container provides the concrete instances. DI decouples construction from use, making it straightforward to swap implementations and to test classes with fakes or mocks (Chapter 2).

**Domain event** — An immutable record of a business fact that has already occurred — "wallet opened", "funds deposited". Domain events are produced by aggregate roots via `raise_event()`, published through the `EventPublisher` port, and consumed by independent listeners. In event sourcing, they are also the source of truth for aggregate state (Chapters 6, 8, 9).

**DTO (Data Transfer Object)** — A plain, serializable object used to carry data across a layer boundary — between the HTTP controller and the service, or between a service and its API client — without exposing internal domain types. In PyFly, Pydantic `BaseModel` subclasses serve as request/response DTOs (Chapter 4).

**EDA (Event-Driven Architecture)** — An architectural style in which services communicate by publishing and subscribing to events rather than by direct synchronous calls. Producers and consumers are decoupled: a producer does not know which consumers exist. PyFly's `EventPublisher` and `@event_listener` are the intra-process EDA primitives; `MessageBrokerPort` extends the pattern across process boundaries (Chapters 8, 10).

**Entity** — A domain object with a stable identity that persists over time and through state changes. Two entities are equal if and only if they share the same non-null `id`. In PyFly, `Entity[TID]` tracks identity; `BaseEntity` adds audit columns (`created_at`, `updated_at`) for the persistence layer (Chapters 5, 6).

**Event sourcing** — A persistence strategy in which every state change is stored as an immutable domain event in an append-only stream. The current state of an aggregate is computed by replaying all events from the stream. PyFly's `pyfly.eventsourcing` module provides `AggregateRoot`, `EventStore`, `EventSourcedRepository`, snapshot support, and a `ProjectionRunner` (Chapter 9).

**EventEnvelope** — The metadata wrapper that packages a domain event payload for delivery: event ID, event type, aggregate stream ID, sequence number, timestamp, correlation ID, and causation ID. Every event reaching a listener arrives in an `EventEnvelope`; you never construct one manually (Chapters 8, 9).

**EventStore** — The append-only persistence layer for an event-sourced system. It records domain events indexed by stream ID and sequence number, enforces optimistic concurrency via the `version` token, and supports range queries for replay. PyFly ships `SQLAlchemyEventStore` and `InMemoryEventStore` (Chapter 9).

**Hexagonal architecture** — An architectural style that places the domain and application logic at the centre, surrounded by ports (interfaces), with adapters at the edges. Business code depends only on ports; adapters implement those ports using specific technologies. This is the organising principle of every PyFly module (Chapters 1, 2, 5).

**Idempotency** — The property that performing the same operation multiple times produces the same result as performing it once. Idempotent handlers are essential in messaging and saga compensation: network retries or at-least-once delivery guarantees mean a message may arrive more than once. Deduplication keys or idempotency tokens are used to detect and discard duplicate executions (Chapters 10, 12).

**Migration** — A versioned, ordered script that evolves a relational database schema without destroying data. PyFly integrates Alembic for migration management; `pyfly db migrate` auto-generates a migration from entity changes and `pyfly db upgrade` applies pending migrations (Chapter 5).

**Outbox pattern** — A technique for publishing domain events reliably alongside a database write: both the state change and the event records are written in the same local transaction; a background relay process reads unsent events from the outbox table and forwards them to the broker. This eliminates the two-phase commit between the database and the message broker (Chapters 9, 12).

**Port** — A Python `Protocol` class that defines the interface a piece of business logic depends on, without specifying any implementation. The DI container wires the concrete adapter that satisfies the protocol at startup. Ports enable the hexagonal architecture and make adapters swappable with zero business-logic changes (Chapters 1, 2, 5).

**Primary bean** — When multiple beans satisfy the same type, the one annotated `@primary` is preferred for injection. Without a `@primary` annotation the container raises an ambiguity error. `@primary` is how you designate the production adapter among several alternatives (Chapter 2).

**Profile** — A named activation tag that selects which beans and configuration values are active at runtime. PyFly loads `pyfly-{profile}.yaml` over the base `pyfly.yaml` and activates beans annotated `@profile("prod")` only when `prod` is in the active profile list. The active profile is set via `PYFLY_PROFILES_ACTIVE` or `pyfly.yaml` (Chapter 3).

**Projection** — A read model derived by consuming a stream of events. A projection subscribes to specific event types and incrementally builds a queryable view — a balance cache, an audit table, a dashboard aggregate — without touching the write model. In event sourcing, `ProjectionRunner` replays the `EventStore` to rebuild projections from scratch (Chapters 8, 9).

**Query (CQRS)** — An immutable dataclass that expresses a read intent — "get wallet balance", "list transactions". Queries inherit from `Query[R]` and flow through the `QueryBus`, which can cache results transparently. Queries never mutate state (Chapter 7).

**QueryBus** — The pipeline that receives a `Query`, optionally returns a cached result, dispatches to the matching `QueryHandler`, and optionally stores the result in cache. Separating the query bus from the command bus allows different cross-cutting behaviour — caching, read-replica routing — for read paths (Chapter 7).

**Rate limiter** — A resilience component that caps the number of requests an endpoint or service client can accept within a time window, preventing overload from bursty traffic. PyFly's `@rate_limit(requests=N, window=timedelta(...))` implements a token-bucket limiter (Chapter 13).

**Repository** — A collection-like abstraction over the persistence layer that allows the application to load and save aggregates or entities without any SQL in the business code. PyFly's `CrudRepository[E, ID]` provides typed `find_by_id`, `save`, `delete`, and derived-query helpers; custom implementations annotated `@repository` replace the in-memory defaults (Chapters 2, 5).

**Retry** — A resilience pattern that re-executes a failed operation after a delay, up to a configured maximum attempt count. Retries handle transient failures — brief network glitches, momentary 503s — without operator intervention. PyFly's `@retry(max_attempts=N, backoff=...)` implements exponential back-off with jitter; `@service_client` includes retry by default (Chapters 11, 13).

**Saga** — A sequence of local transactions coordinated by a central orchestrator. Each step commits to its own service's database; if a later step fails, the orchestrator calls the compensating transaction for each already-committed step in reverse order. PyFly's `@saga` and `@saga_step` decorators implement the orchestrated saga pattern with a parallel-execution DAG (Chapter 12).

**Serialization** — The process of converting an in-memory object to a wire format (JSON, Protobuf, Avro) for HTTP responses or message publishing, and deserializing the reverse. PyFly's central `ObjectMapper` bean configures Pydantic-backed JSON serialization once and applies it to every HTTP response and message envelope (Chapters 4, 10).

**Service** — A managed bean decorated with `@service` that houses business logic and orchestrates calls to repositories, event publishers, and other services. Services are the application layer in hexagonal architecture: they translate incoming commands into domain operations and persist results (Chapter 2).

**Snapshot** — A point-in-time serialized copy of an event-sourced aggregate's state, stored alongside the event stream to accelerate replay. On load, the repository restores the snapshot and replays only the events that occurred after the snapshot version, capping replay time regardless of stream length (Chapter 9).

**Stereotype** — A decorator that registers a class as a bean and signals its architectural role: `@service`, `@repository`, `@component`, `@configuration`, or `@rest_controller`. All stereotypes are technically equivalent in the container; the semantic difference is for human readers and tooling (Chapter 2).

**TCC (Try-Confirm-Cancel)** — A distributed-transaction pattern in which each participant first *reserves* a resource (Try), then the coordinator either *confirms* all reservations or *cancels* them based on whether all Tries succeeded. TCC is useful when exact, immediate reservation semantics are required — for example, holding funds before capturing a payment. PyFly's `@tcc_step` implements the TCC protocol alongside the saga engine (Chapter 12).

**Testcontainers** — A library that starts real Docker containers (PostgreSQL, Redis, Kafka) for integration tests and tears them down when the suite finishes. PyFly's `pyfly.testing` module provides `postgres_container` and `redis_container` fixtures that wire Testcontainers into the DI container via `@ServiceConnection`-style configuration (Chapter 16).

**Value object** — An immutable domain object identified by its value rather than by an identity field. Two value objects are equal if all their fields are equal. In PyFly, `ValueObject` is the base class; apply `@dataclass(frozen=True)` to enforce immutability. `Money` — an amount and a currency — is Lumen's canonical value object (Chapter 6).

**Webhook** — An inbound HTTP callback that an external provider (Stripe, Twilio, etc.) calls to notify your service of an asynchronous event, such as a payment-status change. PyFly's `@webhook_listener` decorator verifies the HMAC-SHA256 signature, deduplicates replays via a nonce cache, and routes the payload to a typed handler (Chapter 17).

**Workflow** — A long-running variant of the saga pattern where steps may pause for minutes, hours, or human approval before resuming. Workflows persist their state between steps so they survive process restarts; PyFly's `@workflow` and `@workflow_step` decorators provide this capability on top of the same saga engine (Chapter 12).
