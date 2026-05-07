<p align="center">
  <img src="../../assets/pyfly-logo.png" alt="PyFly Logo" width="600" />
</p>

<p align="center">
  <strong>PyFly Framework — Module Guides</strong>
</p>

<p align="center">
  <em>In-depth guides for every PyFly module. Each guide covers architecture, API reference, configuration, examples, and testing patterns.</em>
</p>

---

## How to Read These Guides

Each guide is **self-contained** — you can jump directly to the module you need. If you're new to PyFly, we recommend starting with the [Getting Started Tutorial](../getting-started.md) and the [Architecture Overview](../architecture.md) first, then diving into individual guides.

Guides are organized by architectural layer, mirroring the framework itself:

---

## Foundation

The building blocks that every PyFly application relies on.

| Guide | What You'll Learn |
|-------|-------------------|
| [Core & Lifecycle](core.md) | Application bootstrap with `@pyfly_application`, startup/shutdown sequence, configuration loading, profile overlays, banner rendering |
| [Dependency Injection](dependency-injection.md) | `@service`, `@repository`, `@controller`, `@component` stereotypes, constructor injection, `Autowired()`, scopes (singleton, transient, request), `@bean` factories, `@primary`, `Qualifier`, conditional beans, lifecycle hooks |
| [Configuration](configuration.md) | YAML/TOML config files, `Config` class, profile-specific overlays, `@config_properties` binding, environment variable overrides |
| [Error Handling](error-handling.md) | 25+ exception types, `ErrorResponse`, `ErrorCategory`, `ErrorSeverity`, HTTP status mapping, structured error responses, `@exception_handler` |

---

## Web Development

Build REST APIs with automatic OpenAPI documentation, validation, security headers, and extensible middleware.

| Guide | What You'll Learn |
|-------|-------------------|
| [Web Layer](web.md) | `@rest_controller`, `@get_mapping` / `@post_mapping` / `@put_mapping` / `@delete_mapping`, parameter binding (`Body`, `PathVar`, `QueryParam`, `Header`, `Cookie`), CORS configuration, OpenAPI 3.1 auto-generation, Swagger UI, ReDoc |
| [Validation](validation.md) | `Valid[T]` annotation for explicit request validation, structured 422 error responses with field-level errors, `Valid[Body[T]]`, `Valid[QueryParam[T]]`, Pydantic `Field()` constraints, custom validators |
| [WebFilters](web-filters.md) | `WebFilterChainMiddleware`, `OncePerRequestFilter` base class, URL pattern matching, built-in filters: `TransactionIdFilter`, `RequestLoggingFilter`, `SecurityHeadersFilter`, `SecurityFilter`, custom filter creation |
| [Actuator](actuator.md) | `/actuator/health`, `/actuator/beans`, `/actuator/env`, `/actuator/info`, `/actuator/loggers` (GET/POST), `/actuator/metrics`, HAL-style `_links` index, `ActuatorRegistry`, per-endpoint enable/disable |
| [Custom Actuator Endpoints](custom-actuator-endpoints.md) | `ActuatorEndpoint` protocol, building custom endpoints, auto-discovery from DI container, configuration-driven enable/disable |

---

## Data & Persistence

Access SQL databases and document stores through a unified repository pattern — inspired by Spring Data.

PyFly Data follows the **Spring Data architecture**: a shared commons layer (`pyfly.data`) with pluggable backend adapters. The `QueryMethodParser` and `RepositoryPort[T, ID]` are framework-agnostic; each adapter provides its own `QueryMethodCompilerPort` implementation.

| Guide | What You'll Learn |
|-------|-------------------|
| [Data Commons](data.md) | `RepositoryPort[T, ID]`, `CrudRepository`, `PagingRepository`, `QueryMethodParser`, `QueryMethodCompilerPort`, `Page`/`Pageable`/`Sort`, `Mapper`, derived query naming convention, building custom adapters |
| [Data Relational — SQLAlchemy Adapter](data-relational.md) | `Repository[T, ID]`, `BaseEntity`, `Specification` pattern, `@query` (JPQL/native SQL), `reactive_transactional`, `FilterOperator`/`FilterUtils`, `RepositoryBeanPostProcessor`, Alembic migrations |
| [Data Document — MongoDB Adapter](data-document.md) | `MongoRepository[T, ID]`, `BaseDocument`, `MongoQueryMethodCompiler`, `mongo_transactional`, `MongoRepositoryBeanPostProcessor`, Beanie ODM setup |

> **Multi-backend projects:** You can use SQL and MongoDB simultaneously. The CLI supports selecting both `data-relational` (SQL) and `data-document` features together — templates generate both `TodoEntity` + `TodoDocument` and both repository types.

---

## Messaging & Events

Build event-driven and message-driven architectures with pluggable broker backends.

| Guide | What You'll Learn |
|-------|-------------------|
| [Messaging](messaging.md) | `MessageBrokerPort`, `@message_listener`, Kafka adapter (`aiokafka`), RabbitMQ adapter (`aio-pika`), in-memory broker, message publishing and consumption |
| [Events (EDA)](events.md) | `EventPublisher`, `EventEnvelope`, `@event_listener`, `@publish_result`, `InMemoryEventBus`, `ErrorStrategy`, domain events, application events |
| [CQRS](cqrs.md) | `Command`, `Query`, `CommandHandler`, `QueryHandler`, `CommandBus`, `QueryBus`, `HandlerRegistry`, validation, authorization, caching, distributed tracing |

---

## Security

Protect your application with JWT authentication, password encoding, and role-based authorization.

| Guide | What You'll Learn |
|-------|-------------------|
| [Security](security.md) | `JWTService`, `BcryptPasswordEncoder`, `SecurityContext`, `@secure` decorator, `SecurityMiddleware`, role-based access control, protected endpoints, token refresh |

---

## Resilience & Performance

Make your services resilient with circuit breakers, rate limiters, and intelligent retry policies.

| Guide | What You'll Learn |
|-------|-------------------|
| [Resilience](resilience.md) | `@rate_limiter`, `@bulkhead`, `@time_limiter`, `@fallback`, sliding window algorithms, concurrent execution limiting |
| [HTTP Client](client.md) | `HttpClientPort`, `@service_client`, `@http_client`, `CircuitBreaker`, `RetryPolicy`, HTTPX adapter, timeout configuration |
| [Caching](caching.md) | `CacheAdapter`, `CacheManager`, `@cacheable`, `@cache_evict`, `@cache_put`, Redis adapter, in-memory adapter, TTL configuration |

---

## Distributed Transactions

Coordinate multi-step operations across services with automatic compensation and recovery.

| Guide | What You'll Learn |
|-------|-------------------|
| [Transactional Engine](transactional.md) | Saga (`@saga`, `@saga_step`), Workflow (`@workflow`, `@workflow_step`, `@wait_for_signal`, `@wait_for_timer`, `@child_workflow`), TCC (`@tcc`, `@tcc_participant`, `@try_method`, `@confirm_method`, `@cancel_method`), parameter injection (`Input`, `FromStep`, `Header`, `Variable`), 5 compensation policies, DAG execution, persistence (in-memory / Redis / SQLAlchemy / cache), recovery, dead-letter queue, scheduling, REST endpoints |
| [Event Sourcing](eventsourcing.md) | `AggregateRoot`, `EventStore` (in-memory + SQLAlchemy), `SnapshotStore`, `TransactionalOutbox`, `Projection` / `ProjectionRunner`, `EventUpcaster`, `EventSourcedRepository` |

---

## Integration

Bridge to the rest of your stack — outbound webhooks, inbound webhooks, identity, content management, notifications, plugins and rules.

| Guide | What You'll Learn |
|-------|-------------------|
| [Callbacks (outbound webhooks)](callbacks.md) | Subscriptions, HMAC signing, retry, execution tracking |
| [Webhooks (inbound)](webhooks.md) | Signature validation, idempotency, listener pattern |
| [Notifications](notifications.md) | Email / SMS / push ports, dummy + SMTP adapters |
| [IDP (Identity Provider)](idp.md) | `IdpAdapter` port + internal-DB reference adapter, login, MFA, roles |
| [ECM (Content Management)](ecm.md) | Document storage, metadata, folders, e-signature ports + adapters |
| [Plugins](plugins.md) | `@plugin`, `@extension`, `@extension_point`, `PluginManager`, dependency resolution |
| [Rule Engine](rule-engine.md) | YAML DSL, AST, batch evaluation, repository |

---

## CLI & Shell

Build CLI applications with DI-integrated commands, interactive REPLs, and post-startup runners.

| Guide | What You'll Learn |
|-------|-------------------|
| [Shell](shell.md) | `@shell_component`, `@shell_method`, `@shell_option`, `@shell_argument`, `ShellRunnerPort`, `CommandLineRunner`, `ApplicationRunner`, `ApplicationArguments`, parameter inference from type hints, Click adapter |

---

## Administration

Monitor and manage your running applications with an embedded dashboard.

| Guide | What You'll Learn |
|-------|-------------------|
| [Admin Dashboard](admin.md) | `pyfly.admin.enabled`, 15 built-in views (Overview, Health, Beans, Environment, Configuration, Loggers, Metrics, Scheduled Tasks, Traces, Mappings, Caches, CQRS, Transactions, Log Viewer, Instances), SSE streams, server mode fleet monitoring, `AdminViewExtension` protocol for custom views, client auto-registration |

---

## Operations

Monitor, schedule, and observe your applications in production.

| Guide | What You'll Learn |
|-------|-------------------|
| [Observability](observability.md) | `@timed`, `@counted`, `@span`, `MetricsRegistry`, `HealthChecker`, Prometheus metrics export, OpenTelemetry tracing, structured logging with correlation IDs |
| [Scheduling](scheduling.md) | `@scheduled`, `@async_method`, `CronExpression`, `TaskScheduler`, fixed-rate tasks, fixed-delay tasks, asyncio and thread pool executors |

---

## Advanced

Cross-cutting concerns, testing patterns, and aspect-oriented programming.

| Guide | What You'll Learn |
|-------|-------------------|
| [AOP](aop.md) | `@aspect`, `@before`, `@after`, `@around`, `@after_returning`, `@after_throwing`, `AspectBeanPostProcessor`, pointcut expressions |
| [Testing](testing.md) | `PyFlyTestCase`, `create_test_container`, event assertions, mock repositories, integration testing patterns, async test helpers |

---

## Architecture Quick Reference

```
pyfly/
├── kernel/          Exception hierarchy, error types
├── core/            Application bootstrap, Config, banner
├── container/       DI container, stereotypes, scopes
├── context/         ApplicationContext, lifecycle, events, conditions
├── config/          Auto-configuration engine
├── logging/         Structured logging (Structlog adapter)
├── web/             REST controllers, routing, OpenAPI, WebFilters
├── data/            Repository pattern (Spring Data architecture)
│   ├── relational/
│   │   └── sqlalchemy/   SQL via SQLAlchemy async ORM
│   ├── document/
│   │   └── mongodb/      MongoDB via Beanie ODM
│   └── ports/            RepositoryPort, QueryMethodCompilerPort
├── validation/      Valid[T], Pydantic validation
├── security/        JWT, password encoding, authorization
├── messaging/       Kafka, RabbitMQ, in-memory broker
├── eda/             Event-driven architecture, event bus
├── cqrs/            Command/Query segregation, CommandBus/QueryBus
├── cache/           Redis, in-memory caching
├── client/          HTTP client, circuit breaker, retry
├── scheduling/      Cron jobs, task scheduler
├── resilience/      Rate limiter, bulkhead, timeout
├── aop/             Aspect-oriented programming
├── observability/   Metrics, tracing, health checks
├── actuator/        Monitoring endpoints, extensible registry
├── admin/           Embedded management dashboard, SSE, server mode
├── transactional/   Saga, Workflow, TCC; persistence adapters; DLQ; scheduler
├── eventsourcing/   Aggregates, event store, snapshots, outbox, projections
├── callbacks/       Outbound webhooks (event → external HTTP)
├── webhooks/        Inbound webhooks with signature validation + idempotency
├── notifications/   Email / SMS / push provider abstraction
├── idp/             Identity-provider adapter (auth, users, roles, MFA)
├── ecm/             Documents, folders, e-signature ports + adapters
├── plugins/         Plugin system with extension points + lifecycle
├── rule_engine/     YAML-based business rules (AST + evaluator)
├── shell/           CLI commands, runners, Click adapter
├── testing/         Test fixtures and assertions
└── cli/             Project scaffolding and tooling
```

---

<p align="center">
  <a href="../README.md">Back to Documentation Home</a> · <a href="../../README.md">Back to PyFly README</a>
</p>
