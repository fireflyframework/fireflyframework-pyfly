<p align="center">
  <img src="../assets/pyfly-logo.png" alt="PyFly Logo" width="600" />
</p>

<p align="center">
  <strong>PyFly Framework Documentation</strong>
</p>

<p align="center">
  <em>Everything you need to build production-grade Python applications with PyFly.</em>
</p>

---

## Getting Started

| Guide | Description |
|-------|-------------|
| [Introduction](index.md) | What is PyFly, philosophy, and why use it |
| [Installation](installation.md) | Install PyFly with the interactive installer or pip |
| [Getting Started Tutorial](getting-started.md) | Build your first PyFly application step by step |
| [Architecture Overview](architecture.md) | Understand the framework's hexagonal design and module layers |

---

## Module Guides

All module guides are organized in the [`modules/`](modules/README.md) directory. Here's a quick reference:

### Foundation

| Guide | Description |
|-------|-------------|
| [Core & Lifecycle](modules/core.md) | Application bootstrap, startup sequence, configuration, profiles, banner |
| [Dependency Injection](modules/dependency-injection.md) | Container, stereotypes, scopes, bean factories, conditional beans, lifecycle hooks |
| [Configuration](modules/configuration.md) | YAML/TOML config, profiles, property binding, environment variables |
| [Error Handling](modules/error-handling.md) | Exception hierarchy, HTTP status mapping, structured error responses |

### Web Development

| Guide | Description |
|-------|-------------|
| [Web Layer](modules/web.md) | REST controllers, routing, parameter binding, middleware, CORS, OpenAPI |
| [Validation](modules/validation.md) | `Valid[T]` annotation, Pydantic model validation, structured 422 errors |
| [WebFilters](modules/web-filters.md) | Request/response filter chain — `TransactionIdFilter`, `RequestLoggingFilter`, `SecurityHeadersFilter` |
| [WebSocket](modules/websocket.md) | `@websocket_mapping`, `WebSocketSession`, `WebSocketHandler` lifecycle, `on_disconnect` hook, route discovery |
| [Actuator](modules/actuator.md) | Health checks, beans endpoint, environment info, loggers, metrics |
| [Custom Actuator Endpoints](modules/custom-actuator-endpoints.md) | Build your own actuator endpoints with the `ActuatorEndpoint` protocol |

### Data & Persistence

| Guide | Description |
|-------|-------------|
| [Data Commons](modules/data.md) | Generic repository ports, derived query parsing, pagination, sorting, entity mapping — the shared layer for all data adapters |
| [Data Relational (SQL)](modules/data-relational.md) | SQLAlchemy adapter — `Repository[T, ID]`, specifications, transactions, custom queries |
| [Data Document (MongoDB)](modules/data-document.md) | MongoDB adapter — `MongoRepository[T, ID]`, `BaseDocument`, Beanie ODM patterns |

### Messaging & Events

| Guide | Description |
|-------|-------------|
| [Messaging](modules/messaging.md) | Kafka, RabbitMQ, in-memory broker, message publishing and consumption |
| [Events (EDA)](modules/events.md) | Event-driven architecture, domain events, application events, event bus |
| [CQRS](modules/cqrs.md) | Command/Query separation, CommandBus/QueryBus pipeline, validation, authorization, caching |

### Distributed Transactions

| Guide | Description |
|-------|-------------|
| [Transactional Engine](modules/transactional.md) | SAGA and TCC distributed transaction patterns, compensation, recovery, saga composition |

### Security

| Guide | Description |
|-------|-------------|
| [Security](modules/security.md) | JWT authentication, password encoding, authorization, protected endpoints |

### Resilience & Performance

| Guide | Description |
|-------|-------------|
| [Resilience](modules/resilience.md) | Rate limiting, bulkhead, timeout, fallback patterns |
| [HTTP Client](modules/client.md) | Service client builder, circuit breaker, retry, declarative clients |
| [Caching](modules/caching.md) | Cache decorators, Redis adapter, in-memory cache, cache management |

### CLI & Shell

| Guide | Description |
|-------|-------------|
| [Shell](modules/shell.md) | `@shell_component`, `@shell_method`, `CommandLineRunner`, `ApplicationRunner`, Click adapter |

### Administration

| Guide | Description |
|-------|-------------|
| [Admin Dashboard](modules/admin.md) | Embedded management dashboard with 15 views, SSE streams, server mode, custom view extensibility |

### Operations

| Guide | Description |
|-------|-------------|
| [Observability](modules/observability.md) | Prometheus metrics, OpenTelemetry tracing, structured logging, health checks |
| [Scheduling](modules/scheduling.md) | Cron jobs, fixed-rate tasks, fixed-delay tasks, async execution |

### Advanced

| Guide | Description |
|-------|-------------|
| [AOP](modules/aop.md) | Aspect-oriented programming, pointcuts, advice types, weaving |
| [Testing](modules/testing.md) | Test fixtures, mock containers, event assertions, testing patterns |

---

## Adapter Reference

Adapters are the concrete implementations behind PyFly's port contracts. Each adapter doc covers setup, configuration, and adapter-specific features.

Browse the full [Adapter Catalog](adapters/README.md), or jump directly:

| Adapter | Backend | Module |
|---------|---------|--------|
| [SQLAlchemy](adapters/sqlalchemy.md) | PostgreSQL, MySQL, SQLite | Data Relational |
| [MongoDB](adapters/mongodb.md) | MongoDB (Beanie ODM) | Data Document |
| [Starlette](adapters/starlette.md) | Starlette / Uvicorn | Web |
| [Kafka](adapters/kafka.md) | Apache Kafka (aiokafka) | Messaging |
| [RabbitMQ](adapters/rabbitmq.md) | RabbitMQ (aio-pika) | Messaging |
| [Redis](adapters/redis.md) | Redis (async) | Caching |
| [HTTPX](adapters/httpx.md) | HTTPX | Client |
| [Click](adapters/click.md) | Click 8.1+ | Shell |

---

## Reference

| Document | Description |
|----------|-------------|
| [Versioning](versioning.md) | Release stages (SNAPSHOT, Milestone, RC, GA), PEP 440 mapping, version history |
| [CLI Reference](cli.md) | Command-line tools — `new`, `run`, `info`, `doctor`, `db` |
| [Spring Boot Comparison](spring-comparison.md) | Side-by-side concept mapping for Java developers |

---

## Quick Links

- **New to PyFly?** Start with the [Getting Started Tutorial](getting-started.md)
- **Coming from Spring Boot?** Read the [Spring Boot Comparison](spring-comparison.md)
- **Building a web service?** See the [Web Layer Guide](modules/web.md)
- **Understanding the data layer?** Start with the [Data Commons Guide](modules/data.md) for shared ports and patterns
- **Setting up a SQL database?** See the [Data Relational Guide](modules/data-relational.md) and [SQLAlchemy Adapter](adapters/sqlalchemy.md)
- **Setting up MongoDB?** See the [Data Document Guide](modules/data-document.md) and [MongoDB Adapter](adapters/mongodb.md)
- **Need messaging?** See [Messaging](modules/messaging.md), [Kafka](adapters/kafka.md), and [RabbitMQ](adapters/rabbitmq.md)
- **Need distributed transactions?** See the [Transactional Engine Guide](modules/transactional.md) for SAGA and TCC patterns
- **Browse all modules:** See the [Module Guides Index](modules/README.md)
- **Building a CLI app?** See the [Shell Guide](modules/shell.md) and [Click Adapter](adapters/click.md)
- **Need an admin dashboard?** See the [Admin Dashboard Guide](modules/admin.md) for monitoring, fleet management, and custom views
- **Browse all adapters:** See the [Adapter Catalog](adapters/README.md)
