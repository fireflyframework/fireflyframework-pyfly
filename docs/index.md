# PyFly Framework

**The Official Python Implementation of the [Firefly Framework](https://github.com/fireflyframework)**

> *Build production-grade Python applications with the patterns you trust — dependency injection, CQRS, event-driven architecture, and more — powered by the Firefly Framework.*

---

## Table of Contents

- [The Problem](#the-problem)
- [What is PyFly?](#what-is-pyfly)
- [Philosophy](#philosophy)
- [How It Works](#how-it-works)
- [Quick Start](#quick-start)
- [Modules](#modules)
- [Documentation](#documentation)

---

## The Problem

You've been here before. A new Python microservice needs to ship. Before writing a single line of business logic, you spend the first two weeks making choices:

- Which web framework? (FastAPI, Flask, Starlette, Django...)
- Which ORM? (SQLAlchemy, Tortoise, Django ORM...)
- Which message broker? (aiokafka, aio-pika, kombu...)
- How do you wire dependencies? (dependency-injector, python-inject, manual...)
- How do you handle configuration? (pydantic-settings, python-dotenv, dynaconf...)
- How do you structure the project? (Everyone invents their own layout)

You assemble a bespoke stack, glue it together, and move on. Six months later, another team builds a second service — and makes entirely different choices. Now you have two codebases with different conventions, different testing strategies, different deployment patterns, and no shared understanding of how things work.

**Python gives you infinite choice. What it doesn't give you is cohesion.**

---

## What is PyFly?

PyFly makes these decisions for you.

It is a **cohesive, full-stack framework** for building production-grade Python applications — microservices, monoliths, and libraries — where every module is designed to work together seamlessly. Dependency injection, HTTP routing, database access, messaging, caching, security, observability, and more — all integrated, all consistent, all with production-ready defaults from day one.

```python
from pyfly.container import service
from pyfly.web import rest_controller, get_mapping

@service
class OrderService:
    def __init__(self, repo: OrderRepository, events: EventPublisher) -> None:
        self._repo = repo
        self._events = events

    async def place_order(self, order: Order) -> Order:
        saved = await self._repo.save(order)
        await self._events.publish(OrderPlaced(order_id=saved.id))
        return saved

@rest_controller("/orders")
class OrderController:
    def __init__(self, service: OrderService) -> None:
        self._service = service

    @post_mapping
    async def create(self, order: Valid[Body[Order]]) -> Order:
        return await self._service.place_order(order)
```

No boilerplate. No manual wiring. The DI container resolves `OrderRepository` and `EventPublisher` from type hints, validates the request body, and publishes domain events — all out of the box.

Under the hood, PyFly delegates to the best async libraries in the Python ecosystem:

| Concern | PyFly Module | Underlying Library |
|---------|-------------|-------------------|
| Web framework | `pyfly.web` | Starlette |
| SQL databases | `pyfly.data.relational` | SQLAlchemy (async) |
| Document databases | `pyfly.data.document` | Beanie / Motor |
| Message broker | `pyfly.messaging` | aiokafka, aio-pika |
| Caching | `pyfly.cache` | Redis (async) |
| HTTP client | `pyfly.client` | httpx |
| Observability | `pyfly.observability` | Prometheus, OpenTelemetry |
| Logging | `pyfly.logging` | structlog |
| Security | `pyfly.security` | PyJWT, bcrypt |
| Validation | `pyfly.validation` | Pydantic |

The key difference: **you depend on PyFly's ports (protocols), not on these libraries directly**. If tomorrow a better ORM appears, PyFly can add an adapter without breaking your code.

PyFly is the **official Python implementation** of the [Firefly Framework](https://github.com/fireflyframework), a battle-tested enterprise platform originally built on Spring Boot for Java (40+ modules in production). PyFly brings the same cohesive programming model to Python 3.12+ — not as a port, but as a native implementation reimagined for `async/await`, type hints, and protocols.

### Who is PyFly for?

- **Python developers** who want enterprise-grade patterns without reinventing the wheel for every project
- **Teams** tired of assembling bespoke stacks and want every service to follow the same conventions
- **Architects** building polyglot platforms who need consistency across Java and Python services
- **Anyone migrating from Spring Boot** who wants familiar concepts expressed natively in Python

Coming from Spring Boot? See the [Spring Boot Comparison Guide](spring-comparison.md) for a side-by-side concept mapping.

---

## Philosophy

Four principles shape every design decision in PyFly. Together, they answer a single question: *how do you build applications that are easy to start, easy to change, and ready for production from the first commit?*

### Convention Over Configuration

Starting a new project should take seconds, not days. PyFly ships with production-ready defaults for every module — logging formats, connection pool sizes, retry policies, security headers, health endpoints — so a new service works immediately with minimal configuration:

```yaml
# A complete, production-ready web service:
pyfly:
  web:
    port: 8080
```

When you need to customize, you override only what matters. Everything else stays sensible.

### Your Code, Not Ours

Your business logic should never import `sqlalchemy`, `redis`, `aiokafka`, or any other infrastructure library. PyFly enforces this through **hexagonal architecture** — the same ports-and-adapters pattern used across all Firefly Framework modules:

- **Ports** are Python `Protocol` classes that define contracts
- **Adapters** are concrete implementations that fulfill those contracts
- Your services depend on ports. The DI container wires the adapters at startup.

The result: you can swap your database from PostgreSQL to MongoDB, your broker from Kafka to RabbitMQ, or your cache from Redis to in-memory — without touching a single line of business logic.

### Async-Native, Type-Safe

Every PyFly API is designed for `asyncio` from the ground up — no sync-to-async bridges, no thread pool workarounds. Every public surface has complete type annotations validated by mypy in strict mode. If it compiles, it's consistent.

### Production-Ready from Day One

The first time you run `pyfly run`, your application already has structured logging with correlation IDs, health check endpoints, Prometheus metrics, OWASP security headers, and graceful shutdown. These aren't features you opt into — they're the baseline.

---

## How It Works

### Dependency Injection

PyFly's DI container resolves dependencies from type hints — no XML, no reflection, just decorators and annotations:

```python
@service
class OrderService:
    def __init__(self, repo: OrderRepository, events: EventPublisher) -> None:
        self._repo = repo
        self._events = events
```

The container inspects `__init__` parameters, resolves dependencies recursively, and manages bean lifecycles (singleton, transient, request-scoped).

### Hexagonal Architecture

Every PyFly module that touches external systems is split into two halves: **ports** and **adapters**. Ports are abstract `Protocol` interfaces that your business logic depends on. Adapters are concrete implementations backed by real libraries. The DI container connects them at startup.

This separation is not conceptual — it is enforced by package structure:

```
┌──────────────────────────────────────────────────────────┐
│                    APPLICATION LAYER                     │
│                                                          │
│  Your services, controllers, and domain logic.           │
│  They depend ONLY on ports.                              │
│                                                          │
│    @service                                              │
│    class OrderService:                                   │
│        repo: RepositoryPort[Order, int]                  │
│        events: EventPublisher                            │
│        cache: CacheAdapter                               │
│                                                          │
└────────────────────────────┬─────────────────────────────┘
                             │ depends on
┌────────────────────────────┴─────────────────────────────┐
│                 PORTS  (Python Protocols)                │
│                                                          │
│  pyfly.data           RepositoryPort[T, ID]              │
│  pyfly.messaging      MessageBrokerPort                  │
│  pyfly.cache          CacheAdapter                       │
│  pyfly.eda            EventPublisher                     │
│  pyfly.client         HttpClientPort                     │
│  pyfly.scheduling     TaskExecutorPort                   │
│  pyfly.web            WebServerPort                      │
│                                                          │
└────────────────────────────┬─────────────────────────────┘
                             │ implements
┌────────────────────────────┴─────────────────────────────┐
│            ADAPTERS  (Concrete Implementations)          │
│                                                          │
│  pyfly.data.relational.sqlalchemy                        │
│  pyfly.data.document.mongodb                             │
│  pyfly.messaging.adapters.kafka                          │
│  pyfly.messaging.adapters.rabbitmq                       │
│  pyfly.cache.adapters.redis                              │
│  pyfly.eda.adapters.memory                               │
│  pyfly.client.adapters.httpx_adapter                     │
│  pyfly.scheduling.adapters.asyncio_executor              │
│  pyfly.web.adapters.starlette                            │
│                                                          │
└──────────────────────────────────────────────────────────┘
```

The practical result — swap any adapter without changing a single line of business logic:

```python
# Your service depends on the port, never on the adapter
@service
class OrderService:
    def __init__(self, repo: RepositoryPort[Order, int]) -> None:
        self._repo = repo

    async def place_order(self, cmd: PlaceOrder) -> Order:
        return await self._repo.save(Order(name=cmd.name))

# The @repository stereotype wires the adapter at startup.
# Switch from SQL to MongoDB by changing one class declaration:

# SQL:     class OrderRepo(Repository[OrderEntity, int]): ...
# MongoDB: class OrderRepo(MongoRepository[OrderDoc, str]): ...
# Custom:  class OrderRepo(DynamoRepository[OrderItem, str]): ...
#
# OrderService never changes. Tests never change. Controllers never change.
```

### Auto-Configuration

PyFly detects installed libraries at startup and wires the right adapters automatically. Install `sqlalchemy` and it binds the relational adapter. Install `redis` and it binds the Redis cache. No broker library installed? Messaging falls back to in-memory. You can always override with explicit configuration in `pyfly.yaml` — but you rarely need to.

---

## Quick Start

```bash
# Clone the repository
git clone https://github.com/fireflyframework/fireflyframework-pyfly.git
cd pyfly

# Install PyFly
bash install.sh

# Create a new project
pyfly new my-service

# Navigate and run
cd my-service
pyfly run --reload

# Visit the API docs
open http://localhost:8080/docs
```

See the [Getting Started Tutorial](getting-started.md) for a comprehensive walkthrough.

---

## Modules

PyFly is organized into four layers:

### Foundation Layer

| Module | Description | Guide |
|--------|-------------|-------|
| **Core** | Application bootstrap, lifecycle, banner, configuration | [Core & Lifecycle](modules/core.md) |
| **Kernel** | Exception hierarchy, structured error types | [Error Handling](modules/error-handling.md) |
| **Container** | Dependency injection, stereotypes, bean factories | [Dependency Injection](modules/dependency-injection.md) |
| **Context** | ApplicationContext, events, lifecycle hooks, conditions | [Dependency Injection](modules/dependency-injection.md) |

### Application Layer

| Module | Description | Guide |
|--------|-------------|-------|
| **Web** | HTTP routing, controllers, middleware, OpenAPI | [Web Layer](modules/web.md) |
| **Data** | Repository ports, derived queries, pagination, sorting, entity mapping | [Data Commons](modules/data.md) |
| **Data Relational** | SQLAlchemy adapter — specifications, transactions, custom queries | [Data Relational](modules/data-relational.md) |
| **Data Document** | MongoDB adapter — Beanie ODM, document repositories | [Data Document](modules/data-document.md) |
| **CQRS** | Command/Query segregation with CommandBus/QueryBus | [CQRS](modules/cqrs.md) |
| **Validation** | Input validation with Pydantic | [Validation](modules/validation.md) |

### Infrastructure Layer

| Module | Description | Guide |
|--------|-------------|-------|
| **Security** | JWT, password encoding, authorization | [Security](modules/security.md) |
| **Messaging** | Kafka, RabbitMQ, in-memory broker | [Messaging](modules/messaging.md) |
| **EDA** | Event-driven architecture, event bus | [Events](modules/events.md) |
| **Cache** | Caching decorators, Redis adapter | [Caching](modules/caching.md) |
| **Client** | HTTP client, circuit breaker, retry | [HTTP Client](modules/client.md) |
| **Scheduling** | Cron jobs, fixed-rate tasks | [Scheduling](modules/scheduling.md) |
| **Resilience** | Rate limiter, bulkhead, timeout, fallback | [Resilience](modules/resilience.md) |

### Cross-Cutting Layer

| Module | Description | Guide |
|--------|-------------|-------|
| **AOP** | Aspect-oriented programming | [AOP](modules/aop.md) |
| **Observability** | Prometheus metrics, OpenTelemetry tracing | [Observability](modules/observability.md) |
| **Logging** | Structured logging with structlog | [Observability](modules/observability.md) |
| **Actuator** | Health checks, monitoring endpoints | [Actuator](modules/actuator.md) |
| **Admin** | Embedded management dashboard, real-time monitoring | [Admin Dashboard](modules/admin.md) |
| **Testing** | Test fixtures and assertions | [Testing](modules/testing.md) |
| **CLI** | Command-line tools | [CLI Reference](cli.md) |

---

## Documentation

### Getting Started

- [Getting Started Tutorial](getting-started.md) — Build your first PyFly application step by step
- [Installation](installation.md) — Install and configure PyFly with the right extras
- [Architecture Overview](architecture.md) — Understand the framework's design and patterns

### Guides

- [Core & Lifecycle](modules/core.md) — Application bootstrap, configuration, profiles, banner
- [Dependency Injection](modules/dependency-injection.md) — Container, stereotypes, scopes, bean factories
- [Configuration](modules/configuration.md) — YAML config, profiles, property binding, environment variables
- [Error Handling](modules/error-handling.md) — Exception hierarchy, structured error responses
- [Web Layer](modules/web.md) — Controllers, routing, parameter binding, middleware, CORS, OpenAPI
- [Actuator](modules/actuator.md) — Health checks, beans, environment, info endpoints
- [Admin Dashboard](modules/admin.md) — Embedded management dashboard, real-time monitoring, server mode
- [Data Commons](modules/data.md) — Repository ports, derived queries, pagination, sorting, entity mapping
- [Data Relational](modules/data-relational.md) — SQLAlchemy adapter: specifications, transactions, custom queries
- [Messaging](modules/messaging.md) — Kafka, RabbitMQ, in-memory message broker
- [Events](modules/events.md) — Event-driven architecture, domain events, application events
- [CQRS](modules/cqrs.md) — Command/Query separation, CommandBus/QueryBus pipeline
- [Security](modules/security.md) — JWT authentication, password encoding, authorization
- [Resilience](modules/resilience.md) — Rate limiting, bulkhead, timeout, fallback
- [HTTP Client](modules/client.md) — Service client, circuit breaker, retry
- [Caching](modules/caching.md) — Cache decorators, Redis adapter
- [Observability](modules/observability.md) — Metrics, tracing, health checks
- [Scheduling](modules/scheduling.md) — Cron jobs, fixed-rate/delay tasks
- [AOP](modules/aop.md) — Aspect-oriented programming, pointcuts, advice
- [Validation](modules/validation.md) — Input validation with Pydantic
- [Testing](modules/testing.md) — Test fixtures, assertions, mock containers

### Reference

- [Spring Boot Comparison](spring-comparison.md) — Side-by-side concept mapping for Java developers
- [CLI Reference](cli.md) — Command-line tools (new, run, db, info, doctor)

---

## Requirements

| Requirement | Version |
|-------------|---------|
| Python | >= 3.12 |
| pip | Latest recommended |
| OS | macOS, Linux (Windows support planned) |

See [Installation](installation.md) for optional dependencies and extras.

---

## License

Apache License 2.0 — Firefly Software Foundation.
