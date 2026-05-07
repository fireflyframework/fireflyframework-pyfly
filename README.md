<p align="center">
  <img src="assets/pyfly-logo.png" alt="PyFly Logo" width="600" />
</p>

<p align="center">
  <strong>The Official Python Implementation of the Firefly Framework</strong>
</p>

<p align="center">
  <a href="https://github.com/fireflyframework/pyfly/actions/workflows/ci.yml"><img src="https://github.com/fireflyframework/pyfly/actions/workflows/ci.yml/badge.svg?branch=main" alt="CI"></a>
  <a href="https://github.com/fireflyframework"><img src="https://img.shields.io/badge/Firefly_Framework-official-ff6600?logo=data:image/svg+xml;base64,PHN2ZyB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciIHZpZXdCb3g9IjAgMCAyNCAyNCI+PHBhdGggZmlsbD0id2hpdGUiIGQ9Ik0xMiAyQzYuNDggMiAyIDYuNDggMiAxMnM0LjQ4IDEwIDEwIDEwIDEwLTQuNDggMTAtMTBTMTcuNTIgMiAxMiAyeiIvPjwvc3ZnPg==" alt="Firefly Framework"></a>
  <a href="https://www.python.org/"><img src="https://img.shields.io/badge/python-3.12%2B-blue?logo=python&logoColor=white" alt="Python 3.12+"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-Apache%202.0-green" alt="License: Apache 2.0"></a>
  <a href="#"><img src="https://img.shields.io/badge/version-0.2.0--M11-yellow" alt="Version: 0.2.0-M11"></a>
  <a href="#"><img src="https://img.shields.io/badge/type--checked-mypy%20strict-blue?logo=python&logoColor=white" alt="Type Checked: mypy strict"></a>
  <a href="#"><img src="https://img.shields.io/badge/code%20style-ruff-purple?logo=ruff&logoColor=white" alt="Code Style: Ruff"></a>
  <a href="#"><img src="https://img.shields.io/badge/async-first-brightgreen" alt="Async First"></a>
</p>

<p align="center">
  <em>Build production-grade Python applications with the patterns you trust — dependency injection, CQRS, event-driven architecture, and more — powered by the <a href="https://github.com/fireflyframework">Firefly Framework</a>.</em>
</p>

---

## The Problem

You've been here before. A new Python microservice needs to ship. Before writing a single line of business logic, you spend the first two weeks making choices:

- Which web framework? (FastAPI, Flask, Starlette, Django...)
- Which ORM? (SQLAlchemy, Tortoise, Django ORM...)
- Which message broker? (aiokafka, aio-pika, kombu...)
- How do you wire dependencies? (dependency-injector, python-inject, manual...)
- How do you structure the project? (Everyone invents their own layout)

You assemble a bespoke stack, glue it together, and move on. Six months later, another team builds a second service — and makes entirely different choices. Now you have two codebases with different conventions, different testing strategies, different deployment patterns, and no shared understanding of how things work.

**Python gives you infinite choice. What it doesn't give you is cohesion.**

---

## What is PyFly?

PyFly makes these decisions for you.

It is a **cohesive, full-stack framework** for building production-grade Python applications — microservices, monoliths, and libraries — where every module is designed to work together seamlessly. Dependency injection, HTTP routing, database access, messaging, caching, security, observability, and more — all integrated, all consistent, all with production-ready defaults from day one.

```python
from pyfly.container import rest_controller, service
from pyfly.web import request_mapping, post_mapping, Body, Valid

@service
class OrderService:
    def __init__(self, repo: OrderRepository, events: EventPublisher) -> None:
        self._repo = repo
        self._events = events

    async def place_order(self, order: Order) -> Order:
        saved = await self._repo.save(order)
        await self._events.publish(OrderPlaced(order_id=saved.id))
        return saved

@rest_controller
@request_mapping("/orders")
class OrderController:
    def __init__(self, service: OrderService) -> None:
        self._service = service

    @post_mapping("", status_code=201)
    async def create(self, order: Valid[Body[Order]]) -> Order:
        return await self._service.place_order(order)
```

No boilerplate. No manual wiring. The DI container resolves `OrderRepository` and `EventPublisher` from type hints, validates the request body, and publishes domain events — all out of the box.

PyFly is the **official Python implementation** of the [Firefly Framework](https://github.com/fireflyframework), a battle-tested enterprise platform originally built on Spring Boot for Java (40+ modules in production). PyFly brings the same cohesive programming model to Python 3.12+ — not as a port, but as a **native implementation** reimagined for `async/await`, type hints, protocols, and the full power of modern Python.

### Who is PyFly for?

- **Python developers** who want enterprise-grade patterns without reinventing the wheel for every project
- **Teams** tired of assembling bespoke stacks and want every service to follow the same conventions
- **Architects** building polyglot platforms who need consistency across Java and Python services
- **Anyone migrating from Spring Boot** who wants familiar concepts expressed natively in Python

Coming from Spring Boot? See the [Spring Boot Comparison Guide](docs/spring-comparison.md) for a side-by-side concept mapping.

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

PyFly's DI container resolves dependencies from **type hints** — no XML, no service locators, just decorators and Python annotations. The container scans packages listed in `scan_packages`, discovers all decorated classes, and builds a complete dependency graph at startup.

```python
from pyfly.container import Autowired, service

@service
class OrderService:
    metrics: MetricsCollector = Autowired(required=False)  # field injection

    def __init__(self, repo: OrderRepository, events: EventPublisher) -> None:
        self._repo = repo      # constructor injection (preferred)
        self._events = events
```

**How resolution works:** When the container creates `OrderService`, it inspects the `__init__` type hints, finds `OrderRepository` and `EventPublisher` in the bean registry, resolves them recursively (including *their* dependencies), and injects the fully-initialized instances. After construction, it sets any `Autowired()` fields via `setattr`. The entire graph is resolved before your application handles its first request.

**Stereotypes** mark classes with their architectural role and register them with the container:

| Stereotype | Purpose | Layer |
|------------|---------|-------|
| `@component` | Generic managed bean | Any |
| `@service` | Business logic | Service |
| `@repository` | Data access | Data |
| `@controller` | Web controller (template responses) | Web |
| `@rest_controller` | REST endpoints (JSON) | Web |
| `@shell_component` | CLI commands (import from `pyfly.shell`) | Shell |
| `@configuration` + `@bean` | Bean factory methods | Infrastructure |

All stereotypes default to **singleton scope** (one instance per application). You can override with `@service(scope=Scope.TRANSIENT)` for a new instance on every injection, or `Scope.REQUEST` for one instance per HTTP request.

**Advanced capabilities:** `Optional[T]` resolves to `None` when no bean is registered. `list[T]` collects all implementations of a type. `Qualifier("name")` selects a specific named bean when multiple candidates exist. `@primary` marks the default when there are multiple implementations of the same port. The container detects circular dependencies at startup and reports them clearly rather than deadlocking at runtime.

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
│  pyfly.shell          ShellRunnerPort                    │
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
│  pyfly.shell.adapters.click_adapter                      │
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

PyFly detects installed libraries at startup and wires the right adapters automatically — no manual bean registration needed.

This works through two complementary mechanisms:

**1. Declarative auto-configuration** — `@configuration` classes guarded by conditions. They act as "default with override" factories:

```python
from pyfly.context.conditions import auto_configuration, conditional_on_class, conditional_on_missing_bean
from pyfly.container.bean import bean

@auto_configuration
@conditional_on_missing_bean(CacheAdapter)    # only if user hasn't registered one
@conditional_on_class("redis.asyncio")        # only if redis is installed
class RedisCacheAutoConfig:
    @bean
    def cache(self) -> CacheAdapter:
        return RedisCacheAdapter(url=self._props.redis.url)
```

This bean is created only when (1) no user-provided `CacheAdapter` exists and (2) the `redis` library is installed. If the user registers their own `CacheAdapter` via `@bean`, the auto-configuration is silently skipped.

**2. Decentralized entry-point discovery** — Each subsystem owns its own `@auto_configuration` class, registered as a `pyfly.auto_configuration` entry point in `pyproject.toml`. At startup, `discover_auto_configurations()` uses `importlib.metadata.entry_points(group="pyfly.auto_configuration")` to find and load them — no hardcoded imports, no central engine:

| Entry Point | Class | Detects | Binds | Fallback |
|-------------|-------|---------|-------|----------|
| `web_fastapi` | `FastAPIAutoConfiguration` | `fastapi` | `FastAPIWebAdapter` | none |
| `web` | `WebAutoConfiguration` | `starlette` | `StarletteWebAdapter` | none |
| `server_granian` | `GranianServerAutoConfiguration` | `granian` | `GranianServerAdapter` | none |
| `server_uvicorn` | `UvicornServerAutoConfiguration` | `uvicorn` | `UvicornServerAdapter` | none |
| `server_hypercorn` | `HypercornServerAutoConfiguration` | `hypercorn` | `HypercornServerAdapter` | none |
| `event-loop` | `EventLoopAutoConfiguration` | `uvloop` / `winloop` | Event loop policy | `asyncio` |
| `relational` | `RelationalAutoConfiguration` | `sqlalchemy` | `Repository[T, ID]` | none |
| `document` | `DocumentAutoConfiguration` | `motor`, `beanie` | `MongoRepository[T, ID]` | none |
| `messaging` | `MessagingAutoConfiguration` | `aiokafka` / `aio-pika` | `KafkaAdapter` / `RabbitMQAdapter` | `InMemoryMessageBroker` |
| `cache` | `CacheAutoConfiguration` | `redis.asyncio` | `RedisCacheAdapter` | `InMemoryCache` |
| `client` | `ClientAutoConfiguration` | `httpx` | `HttpxClientAdapter` | none |
| `shell` | `ShellAutoConfiguration` | `click` | `ClickShellAdapter` | none |
| `cqrs` | `CqrsAutoConfiguration` | — | CQRS handlers | none |
| `admin` | `AdminAutoConfiguration` | — | Admin dashboard | none |
| `transactional` | `TransactionalEngineAutoConfiguration` | — | Saga/TCC engines | none |
| `security-jwt` | `JwtAutoConfiguration` | `pyjwt` | `JWTService` | none |
| `security-password` | `PasswordEncoderAutoConfiguration` | `bcrypt` | `BcryptPasswordEncoder` | none |
| `scheduling` | `SchedulingAutoConfiguration` | `croniter` | `TaskScheduler` | none |
| `metrics` | `MetricsAutoConfiguration` | `prometheus_client` | `MetricsRegistry` | none |
| `tracing` | `TracingAutoConfiguration` | `opentelemetry` | `TracerProvider` | none |
| `actuator` | `ActuatorAutoConfiguration` | — | `ActuatorRegistry`, `HealthAggregator` | none |
| `actuator-metrics` | `MetricsActuatorAutoConfiguration` | `prometheus_client` | `MetricsEndpoint`, `PrometheusEndpoint` | none |
| `aop` | `AopAutoConfiguration` | — | `AspectBeanPostProcessor` | none |

Third-party packages can register their own auto-configurations by adding entries to the same entry-point group — the same extensibility model as Spring Boot's `META-INF/spring.factories`:

```toml
# In a third-party pyproject.toml:
[project.entry-points."pyfly.auto_configuration"]
my-addon = "my_package.auto_configuration:MyAutoConfiguration"
```

**The practical workflow:** During development, install `uv add "pyfly[full]"` (or `pip install pyfly[full]`) and everything auto-wires. In production Docker images, install only the extras you need (e.g., `pyfly[web,data-relational,cache]`) and the discovered auto-configurations bind exactly those adapters. You can always override any auto-configured adapter with explicit `provider` settings in `pyfly.yaml` or by registering your own bean.

---

## Installation

> **Note:** PyFly is distributed exclusively via [GitHub Releases](https://github.com/fireflyframework/pyfly/releases). It is **not** published to PyPI.

### Install from GitHub Release (Recommended)

```bash
# Install the latest release (uv)
uv add "pyfly @ https://github.com/fireflyframework/pyfly/releases/latest/download/pyfly-0.2.0a11-py3-none-any.whl"

# Install with specific extras
uv add "pyfly[web,data-relational,cache] @ https://github.com/fireflyframework/pyfly/releases/latest/download/pyfly-0.2.0a11-py3-none-any.whl"

# Or with pip
pip install "pyfly @ https://github.com/fireflyframework/pyfly/releases/latest/download/pyfly-0.2.0a11-py3-none-any.whl"
```

### One-Line Install (CLI + Framework)

```bash
# Via get.pyfly.io
curl -fsSL https://get.pyfly.io/ | bash

# Or directly from GitHub
curl -fsSL https://raw.githubusercontent.com/fireflyframework/pyfly/main/install.sh | bash
```

The installer clones the repo, creates a virtual environment, installs PyFly with all extras, and adds `pyfly` to your PATH. You can customize with environment variables:

```bash
# Install to a custom directory
PYFLY_HOME=/opt/pyfly curl -fsSL https://get.pyfly.io/ | bash

# Install with specific extras only
PYFLY_EXTRAS=web,data-relational,security curl -fsSL https://get.pyfly.io/ | bash
```

### Install from Source

```bash
# Clone the repository
git clone https://github.com/fireflyframework/pyfly.git
cd pyfly

# Run the interactive installer
bash install.sh

# Or install manually with uv
uv sync --all-extras --group dev

# Or with pip
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[full]"
```

### Verify Installation

```bash
pyfly --version
pyfly doctor
pyfly info
```

### Create Your First Project

```bash
# Quick start — create a REST API with all the batteries
pyfly new my-service --archetype web-api
cd my-service
pyfly run --reload

# Visit http://localhost:8080/health
```

See the [Installation Guide](docs/installation.md) for detailed options, Docker examples, and CI/CD setup.

---

## CLI & Project Scaffolding

The `pyfly` CLI generates production-ready project structures with DI stereotypes, Docker support, and layered architecture out of the box.

### Archetypes

| Command | What you get |
|---------|-------------|
| `pyfly new my-app` | Minimal microservice (`core` archetype) |
| `pyfly new my-api --archetype web-api` | REST API with controllers, services, repositories |
| `pyfly new my-api --archetype fastapi-api` | REST API with FastAPI and native OpenAPI |
| `pyfly new my-site --archetype web` | Server-rendered HTML with Jinja2 templates |
| `pyfly new my-svc --archetype hexagonal` | Hexagonal architecture with ports & adapters |
| `pyfly new my-lib --archetype library` | Reusable library with `py.typed` marker |
| `pyfly new my-tool --archetype cli` | CLI application with interactive shell and DI |

### Feature Selection

Choose which PyFly extras to include with `--features`:

```bash
# REST API with database and caching
pyfly new order-service --archetype web-api --features web,data-relational,cache
```

Available features: `web`, `data-relational`, `data-document`, `eda`, `cache`, `client`, `security`, `scheduling`, `observability`, `cqrs`, `shell`

### Interactive Mode

Run `pyfly new` without arguments for a guided experience:

```
$ pyfly new

  ╭──────────────────────────────────╮
  │   PyFly Project Generator        │
  ╰──────────────────────────────────╯

  Step 1 of 4 — Project Details
  ? Project name: order-service
  ? Package name: order_service

  Step 2 of 4 — Architecture
  ? Select archetype: (use arrow keys)
    ❯ core          Minimal microservice with DI container and config
      web-api       Full REST API with controller/service/repository layers
      web           Server-rendered HTML with Jinja2 templates and static assets
      hexagonal     Clean architecture with domain isolation
      library       Reusable library with py.typed and packaging best practices
      cli           Command-line application with interactive shell and DI

  Step 3 of 4 — Features
  ? Select features: (space to toggle, enter to confirm)
    ❯ [x] web          HTTP server, REST controllers, OpenAPI docs
      [ ] data-relational  Data Relational — SQL databases (SQLAlchemy ORM)
      ...

  Step 4 of 4 — Review & Create
  ? Create this project? Yes
```

### Generated Web API Structure

```
order-service/
├── Dockerfile              # Multi-stage production build
├── README.md               # Project docs with quick start
├── pyfly.yaml              # Framework configuration
├── pyproject.toml          # Dependencies based on selected features
├── .gitignore
├── .env.example
├── src/order_service/
│   ├── __init__.py
│   ├── app.py              # @pyfly_application entry point
│   ├── main.py             # ASGI app factory
│   ├── controllers/
│   │   ├── __init__.py
│   │   ├── health_controller.py   # @rest_controller — /health
│   │   └── todo_controller.py     # @rest_controller — CRUD /todos
│   ├── services/
│   │   ├── __init__.py
│   │   └── todo_service.py        # @service — business logic
│   ├── models/
│   │   ├── __init__.py
│   │   └── todo.py                # Pydantic DTOs
│   └── repositories/
│       ├── __init__.py
│       └── todo_repository.py     # @repository — data access
└── tests/
    ├── __init__.py
    ├── conftest.py
    └── test_todo_service.py
```

### Other CLI Commands

| Command | Description |
|---------|-------------|
| `pyfly run --reload` | Start the application server with auto-reload |
| `pyfly info` | Show installed framework version and extras |
| `pyfly doctor` | Diagnose your development environment |
| `pyfly db init` | Initialize Alembic migration environment |
| `pyfly db migrate -m "msg"` | Auto-generate a database migration |
| `pyfly db upgrade` | Apply pending migrations |
| `pyfly license` | Display the Apache 2.0 license |
| `pyfly sbom` | Software Bill of Materials (table or JSON) |

See the full [CLI Reference](docs/cli.md) for details.

---

## Modules

PyFly currently implements **36 modules** organized into five layers:

### Foundation Layer

| Module | Description | Firefly Java Equivalent |
|--------|-------------|------------------------|
| **Core** | Application bootstrap, lifecycle, banner, configuration | `fireflyframework-starter-core` |
| **Kernel** | Exception hierarchy, structured error types | `fireflyframework-kernel` |
| **Container** | Dependency injection, stereotypes, bean factories | Spring DI (built-in) |
| **Context** | ApplicationContext, events, lifecycle hooks, conditions | Spring ApplicationContext |
| **Config** | Decentralized auto-configuration via `@auto_configuration` entry points | Spring Auto-Configuration |
| **Logging** | Structured logging port and adapters | `fireflyframework-observability` |

### Application Layer

| Module | Description | Firefly Java Equivalent |
|--------|-------------|------------------------|
| **Web** | HTTP routing, controllers, middleware, OpenAPI (Starlette and FastAPI adapters) | `fireflyframework-web` |
| **Server** | Pluggable ASGI servers (Granian, Uvicorn, Hypercorn) and event loops (uvloop, asyncio) | Embedded Tomcat/Jetty/Undertow |
| **Data** | Repository ports, derived queries, pagination, sorting, entity mapping | Spring Data Commons |
| **Data Relational** | SQLAlchemy adapter — specifications, transactions, custom queries | `fireflyframework-r2dbc` |
| **Data Document** | MongoDB adapter — Beanie ODM, document repositories | `fireflyframework-mongodb` |
| **CQRS** | Command/Query segregation with CommandBus/QueryBus, validation, authorization, caching | `fireflyframework-cqrs` |
| **Validation** | Input validation with Pydantic | `fireflyframework-validators` |

### Infrastructure Layer

| Module | Description | Firefly Java Equivalent |
|--------|-------------|------------------------|
| **Security** | JWT, password encoding, authorization | Part of `fireflyframework-starter-application` |
| **Messaging** | Kafka, RabbitMQ, in-memory broker | `fireflyframework-eda` |
| **EDA** | Event-driven architecture, event bus | `fireflyframework-eda` |
| **Cache** | Caching decorators, Redis adapter | `fireflyframework-cache` |
| **Client** | HTTP client, circuit breaker, retry | `fireflyframework-client` |
| **Scheduling** | Cron jobs, fixed-rate tasks | Spring Scheduling |
| **Resilience** | Rate limiter, bulkhead, timeout, fallback | Resilience4j (in `fireflyframework-client`) |
| **Shell** | CLI commands, interactive REPL, runners | Spring Shell |
| **Transactional** | Saga + Workflow + TCC orchestration: signal-driven, DAG, compensation, multi-backend persistence, DLQ, recovery | `fireflyframework-orchestration` |
| **Event Sourcing** | AggregateRoot, EventStore, snapshots, transactional outbox, projections, upcasting | `fireflyframework-eventsourcing` |
| **Plugins** | `@plugin` / `@extension_point` / `@extension`, dependency-ordered lifecycle | `fireflyframework-plugins` |
| **Rule Engine** | YAML DSL, AST evaluator, batch evaluation, rule-set repository | `fireflyframework-rule-engine` |
| **Config Server** | Spring Cloud Config Server analogue + client | `fireflyframework-config-server` |

### Integration Layer

| Module | Description | Firefly Java Equivalent |
|--------|-------------|------------------------|
| **IDP** | Identity-provider port + Keycloak / AWS Cognito / Azure AD / internal-DB adapters | `fireflyframework-idp` + adapters |
| **ECM** | Document storage / metadata / folders / e-signature ports + AWS S3 / Azure Blob / DocuSign / Adobe Sign / Logalty / local-fs adapters | `fireflyframework-ecm` + adapters |
| **Notifications** | Email / SMS / push ports + SendGrid / Twilio / Firebase / Resend / SMTP / dummy adapters | `fireflyframework-notifications` + adapters |
| **Callbacks** | Outbound webhook dispatcher with HMAC signing, retries, execution tracking | `fireflyframework-callbacks` |
| **Webhooks** | Inbound webhook ingestion with signature validation, idempotency, listener pattern | `fireflyframework-webhooks` |
| **Starters** | Meta-packages (`enable_core_stack` / `application` / `data` / `domain`) | `fireflyframework-starter-*` |

### Cross-Cutting Layer

| Module | Description | Firefly Java Equivalent |
|--------|-------------|------------------------|
| **AOP** | Aspect-oriented programming | Spring AOP |
| **Observability** | Prometheus metrics, OpenTelemetry tracing | `fireflyframework-observability` |
| **Actuator** | Health checks, monitoring endpoints | `fireflyframework-starter-core` (actuator) |
| **Admin** | Embedded management dashboard with 15 views, SSE streams, server mode fleet monitoring | Spring Boot Admin |
| **Testing** | Test fixtures and assertions | Spring Test |
| **CLI** | Command-line tools | `fireflyframework-cli` |

---

## Documentation

Full documentation lives in the [`docs/`](docs/README.md) directory:

- [Getting Started Tutorial](docs/getting-started.md) — Build your first PyFly application step by step
- [Installation](docs/installation.md) — Install and configure PyFly with the right extras
- [Architecture Overview](docs/architecture.md) — Understand the framework's design and patterns
- [CLI Reference](docs/cli.md) — Command-line tools (new, run, db, info, doctor, license, sbom)
- [Spring Boot Comparison](docs/spring-comparison.md) — Side-by-side concept mapping for Java developers

### Module Guides

Browse all guides in the [Module Guides Index](docs/modules/README.md):

- [Web Layer](docs/modules/web.md) — REST controllers, routing, parameter binding, OpenAPI
- [Server Layer](docs/modules/server.md) — Pluggable ASGI servers, event loops, auto-configuration
- [Data Commons](docs/modules/data.md) — Repository ports, derived queries, pagination, sorting, entity mapping
- [Data Relational (SQL)](docs/modules/data-relational.md) — SQLAlchemy adapter: specifications, transactions, custom queries
- [Data Document (MongoDB)](docs/modules/data-document.md) — MongoDB adapter: MongoRepository, Beanie ODM patterns
- [Validation](docs/modules/validation.md) — `Valid[T]` annotation, structured 422 errors
- [WebFilters](docs/modules/web-filters.md) — Request/response filter chain
- [Actuator](docs/modules/actuator.md) — Health checks, extensible endpoints
- [Custom Actuator Endpoints](docs/modules/custom-actuator-endpoints.md) — Build your own actuator endpoints
- [Transactional Engine](docs/modules/transactional.md) — Saga, Workflow, and TCC distributed transaction patterns
- [Event Sourcing](docs/modules/eventsourcing.md) — Aggregates, event store, snapshots, outbox, projections
- [Plugins](docs/modules/plugins.md) — Plugin SPI, extension points, lifecycle
- [Rule Engine](docs/modules/rule-engine.md) — YAML DSL, AST evaluator, batch evaluation
- [Callbacks (outbound)](docs/modules/callbacks.md) — Dispatch domain events to external HTTP endpoints
- [Webhooks (inbound)](docs/modules/webhooks.md) — Receive, verify, dedupe, dispatch
- [Notifications](docs/modules/notifications.md) — Email / SMS / push abstractions
- [IDP (Identity Provider)](docs/modules/idp.md) — Keycloak / AWS Cognito / Azure AD / internal-DB adapters
- [ECM (Content Management)](docs/modules/ecm.md) — Documents, folders, e-signature workflows
- [Admin Dashboard](docs/modules/admin.md) — Embedded management dashboard, server mode, custom views

### Adapter Reference

Browse the [Adapter Catalog](docs/adapters/README.md) for setup and configuration of each concrete backend:

- [SQLAlchemy](docs/adapters/sqlalchemy.md) · [MongoDB](docs/adapters/mongodb.md) · [Starlette](docs/adapters/starlette.md) · [FastAPI](docs/adapters/fastapi.md) · [Granian](docs/adapters/granian.md) · [Kafka](docs/adapters/kafka.md) · [RabbitMQ](docs/adapters/rabbitmq.md) · [Redis](docs/adapters/redis.md) · [HTTPX](docs/adapters/httpx.md) · [Click](docs/adapters/click.md)

Browse the full list in the [Documentation Table of Contents](docs/README.md).

---

## Roadmap

See **[ROADMAP.md](ROADMAP.md)** for the full roadmap toward feature parity with the Firefly Framework Java ecosystem (40+ modules).

| Phase | Focus | Key Modules |
|-------|-------|-------------|
| **Phase 1** | Core Distributed Patterns | Event Sourcing, ~~Saga/TCC~~ (done), Workflow, DDD |
| **Phase 2** | Business Logic | Rule Engine, Plugins, Data Processing |
| **Phase 3** | Enterprise Integrations | Notifications, IDP, ECM, Webhooks |
| **Phase 4** | Administrative | Backoffice, Config Server, Utils |

---

## Versioning

PyFly follows the same versioning system as Spring Boot, based on **Semantic Versioning** (`MAJOR.MINOR.PATCH`) with four release stages:

| Stage | Format | Description |
|-------|--------|-------------|
| **SNAPSHOT** | `0.2.0-SNAPSHOT` | Active development build. Unstable, changes daily. |
| **Milestone** | `0.2.0-M11` | Pre-release feature preview. New functionality available for early feedback. |
| **Release Candidate** | `0.2.0-RC1` | Feature-complete. Only bug fixes from this point. |
| **GA** | `0.2.0` | General Availability. Production-ready, fully tested and stable. |

**Release lifecycle:** `SNAPSHOT` → `M1` → `M2` → ... → `RC1` → `RC2` → ... → `GA`

For Python packaging (PEP 440), milestone versions map to alpha pre-releases (`0.2.0a11`), release candidates map to `rc` (`0.2.0rc1`), and GA is the final release (`0.2.0`). See [docs/versioning.md](docs/versioning.md) for full details.

---

## Changelog

See **[CHANGELOG.md](CHANGELOG.md)** for detailed release notes.

**Current:** 0.2.0-M11 (2026-03-01) — Thread-safety, correctness, and robustness audit: 18 fixes across DI container, web layer, resilience, security, and data modules.

---

## Firefly Framework Ecosystem

PyFly is part of the [Firefly Framework](https://github.com/fireflyframework) ecosystem:

| Platform | Repository | Status |
|----------|-----------|--------|
| **Java / Spring Boot** | [`fireflyframework-*`](https://github.com/fireflyframework) (40+ modules) | Production |
| **Python** | [`pyfly`](https://github.com/fireflyframework/pyfly) | Milestone (M6) |
| **Frontend (Angular)** | [`flyfront`](https://github.com/fireflyframework/flyfront) | Active Development |
| **GenAI** | [`fireflyframework-genai`](https://github.com/fireflyframework/fireflyframework-genai) | Active Development |
| **CLI (Go)** | [`fireflyframework-cli`](https://github.com/fireflyframework/fireflyframework-cli) | Active Development |

---

## Requirements

| Requirement | Version |
|-------------|---------|
| Python | >= 3.12 |
| uv | >= 0.5 recommended (pip also supported) |
| Git | For cloning the repository |
| OS | macOS, Linux (Windows support planned) |

---

## License

Apache License 2.0 — [Firefly Software Foundation.](https://github.com/fireflyframework)
