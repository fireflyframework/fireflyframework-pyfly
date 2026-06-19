<p align="center">
  <img src="assets/banner.svg" alt="PyFly — Event-Driven Python Microservices with the Firefly Framework" width="100%" />
</p>

<h1 align="center">PyFly</h1>

<p align="center">
  <strong>The official Python implementation of the Firefly Framework — Spring Boot's cohesion, native to async Python.</strong>
</p>

<p align="center">
  <a href="https://github.com/fireflyframework/fireflyframework-pyfly/actions/workflows/ci.yml"><img src="https://github.com/fireflyframework/fireflyframework-pyfly/actions/workflows/ci.yml/badge.svg?branch=main" alt="CI"></a>
  <a href="https://github.com/fireflyframework"><img src="https://img.shields.io/badge/Firefly_Framework-official-ff6600?logo=data:image/svg+xml;base64,PHN2ZyB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciIHZpZXdCb3g9IjAgMCAyNCAyNCI+PHBhdGggZmlsbD0id2hpdGUiIGQ9Ik0xMiAyQzYuNDggMiAyIDYuNDggMiAxMnM0LjQ4IDEwIDEwIDEwIDEwLTQuNDggMTAtMTBTMTcuNTIgMiAxMiAyeiIvPjwvc3ZnPg==" alt="Firefly Framework"></a>
  <a href="https://www.python.org/"><img src="https://img.shields.io/badge/python-3.12%2B-blue?logo=python&logoColor=white" alt="Python 3.12+"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-Apache%202.0-green" alt="License: Apache 2.0"></a>
  <a href="CHANGELOG.md"><img src="https://img.shields.io/badge/version-26.06.113-brightgreen" alt="Version: 26.06.113"></a>
  <a href="https://mypy-lang.org/"><img src="https://img.shields.io/badge/type--checked-mypy%20strict-blue?logo=python&logoColor=white" alt="Type Checked: mypy strict"></a>
  <a href="https://docs.astral.sh/ruff/"><img src="https://img.shields.io/badge/code%20style-ruff-purple?logo=ruff&logoColor=white" alt="Code Style: Ruff"></a>
  <a href="#philosophy"><img src="https://img.shields.io/badge/async-first-brightgreen" alt="Async First"></a>
</p>

<p align="center">
  <em>Build production-grade Python applications with the patterns you trust — dependency injection, CQRS, event-driven architecture, and more — powered by the <a href="https://github.com/fireflyframework">Firefly Framework</a>.</em>
</p>

<p align="center">
  <a href="book/"><b>📘 The Book</b></a> &nbsp;·&nbsp;
  <a href="#quickstart"><b>Quickstart</b></a> &nbsp;·&nbsp;
  <a href="#why-pyfly">Why PyFly</a> &nbsp;·&nbsp;
  <a href="#architecture">Architecture</a> &nbsp;·&nbsp;
  <a href="#featured-patterns">Patterns</a> &nbsp;·&nbsp;
  <a href="#modules">Modules</a> &nbsp;·&nbsp;
  <a href="docs/README.md">Docs</a> &nbsp;·&nbsp;
  <a href="CHANGELOG.md">Changelog</a>
</p>

<details>
<summary><b>Table of contents</b></summary>

- [📘 The Book — *PyFly by Example*](#-the-book--pyfly-by-example)
- [Why PyFly?](#why-pyfly)
- [Quickstart](#quickstart)
- [Philosophy](#philosophy)
- [Architecture](#architecture) — [Dependency Injection](#dependency-injection) · [Hexagonal](#hexagonal-architecture) · [Auto-Configuration](#auto-configuration) · [Request Lifecycle](#request-lifecycle)
- [Featured Patterns](#featured-patterns)
- [Installation](#installation)
- [CLI & Project Scaffolding](#cli--project-scaffolding)
- [Modules](#modules)
- [Documentation](#documentation)
- [The Firefly Ecosystem](#firefly-framework-ecosystem)
- [Roadmap](#roadmap) · [Versioning](#versioning) · [Changelog](#changelog)
- [Requirements](#requirements) · [Contributing](#contributing) · [License](#license)

</details>

---

## 📘 The Book — *PyFly by Example*

**[PyFly by Example](book/)** is the official, project-driven book for the framework. Across **18 chapters in five parts** it builds **Lumen** — the wallet & ledger service in [`samples/lumen/`](samples/lumen/README.md) — from an empty folder into a secured, observable, event-driven microservice. Every code listing is drawn from that real, running project (it boots and passes its tests against this framework version), so what you read is what actually runs.

It covers the whole stack: dependency injection, configuration & profiles, the web layer, the Spring-Data `Repository` (derived queries, pagination, specifications, projections), DDD aggregates & `Money`, CQRS, domain events & event sourcing, messaging, HTTP clients, sagas, caching & resilience, security, observability, testing, scheduling, and going to production. Spring developers get a **Spring parity** callout at every turn.

**📥 Download the book:** **[PDF](book/dist/pyfly-by-example.pdf)** · **[EPUB](book/dist/pyfly-by-example.epub)** — or build it from source with `bash book/build/run.sh`. Run the companion sample with `cd samples/lumen && uv run --extra dev pytest` and `uv run pyfly run --server uvicorn`.

---

## Why PyFly?

### The problem

You've been here before. A new Python microservice needs to ship. Before writing a single line of business logic, you spend the first two weeks making choices:

- Which web framework? (FastAPI, Flask, Starlette, Django...)
- Which ORM? (SQLAlchemy, Tortoise, Django ORM...)
- Which message broker? (aiokafka, aio-pika, kombu...)
- How do you wire dependencies? (dependency-injector, python-inject, manual...)
- How do you structure the project? (Everyone invents their own layout)

You assemble a bespoke stack, glue it together, and move on. Six months later, another team builds a second service — and makes entirely different choices. Now you have two codebases with different conventions, different testing strategies, different deployment patterns, and no shared understanding of how things work.

**Python gives you infinite choice. What it doesn't give you is cohesion.**

---

### What PyFly is

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

## Quickstart

Zero to a running, production-grade service in under a minute:

```bash
# 1 · Install the CLI + framework (one line)
curl -fsSL https://get.pyfly.io/ | bash

# 2 · Scaffold a REST API with batteries included
pyfly new my-service --archetype web-api
cd my-service

# 3 · Run it — structured logging, health checks, metrics & OpenAPI all on by default
pyfly run --reload

# 4 · It's live
curl http://localhost:8080/health      # {"status":"UP"}
#    └ OpenAPI / Swagger UI at http://localhost:8080/docs
```

That is a fully wired service — DI container, web layer, actuator, observability, security headers, graceful shutdown — from **one install and one command**. See [Installation](#installation) for uv / pip / Docker options and [CLI & Project Scaffolding](#cli--project-scaffolding) for every archetype.

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

The first time you run `pyfly run`, your application already has structured logging with correlation IDs, PII redaction, health check endpoints, Prometheus metrics, OWASP security headers, and graceful shutdown. These aren't features you opt into — they're the baseline.

---

## Architecture

PyFly is **cohesive, layered, and hexagonal**: five layers on an async core, every external system reached through a Protocol port, and the right adapters wired automatically at startup.

<p align="center">
  <img src="assets/architecture.svg" alt="PyFly architecture at a glance: one front door (pyfly + extras, @pyfly_application) over five layers — Cross-Cutting, Integration, Infrastructure, Application, Foundation — on an async core (asyncio, uvloop, ASGI)." width="100%" />
</p>

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

### Request Lifecycle

Once the graph is built, a request flows through it end-to-end — filters, controller, service, port, adapter, database — with the response and any domain events on the way back, and you wrote none of the wiring:

<p align="center">
  <img src="assets/request-lifecycle.svg" alt="PyFly request lifecycle: an HTTP request flows through the web-filter chain, the @rest_controller (with validation), the @service, a repository port, the SQLAlchemy adapter, and PostgreSQL; a domain event is published and a JSON response is returned." width="100%" />
</p>

### Hexagonal Architecture

Every PyFly module that touches external systems is split into two halves: **ports** and **adapters**. Ports are abstract `Protocol` interfaces that your business logic depends on. Adapters are concrete implementations backed by real libraries. The DI container connects them at startup.

This separation is not conceptual — it is enforced by package structure:

<p align="center">
  <img src="assets/hexagonal.svg" alt="PyFly hexagonal architecture: an application core that depends only on Protocol ports (RepositoryPort, MessageBrokerPort, CacheAdapter, EventPublisher, HttpClientPort, WebServerPort), with swappable adapters — SQLAlchemy/MongoDB, Kafka/RabbitMQ, Redis, httpx, Starlette/FastAPI — implementing each port." width="100%" />
</p>

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

<p align="center">
  <img src="assets/auto-configuration.svg" alt="PyFly auto-configuration flow: entry-point discovery finds each @auto_configuration, conditional guards (@conditional_on_class, @conditional_on_missing_bean) decide, and the result is to bind an adapter (e.g. RedisCacheAdapter), fall back (InMemoryCache), or skip when your own bean already wins." width="100%" />
</p>

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

## Featured Patterns

PyFly is more than a web framework — it ships **production-grade implementations of the distributed patterns** that power real microservices: distributed transactions, durable workflows, event sourcing, identity, content management, multi-channel notifications, inbound/outbound webhooks, business rules, and more. Each one is a first-class module with a port-and-adapter design, CLI scaffolding, REST controllers (where applicable), metrics, tracing, and persistence.

The sections below show one representative example per pattern. The full guides live under [`docs/modules/`](docs/modules/README.md).

<p align="center">
  <img src="assets/distributed-patterns.svg" alt="PyFly distributed transaction patterns: a saga DAG (reserve → charge → ship) with reverse-order compensation, alongside summary cards for Saga (compensation-based), Workflow (durable, signal-driven), and TCC (try/confirm/cancel)." width="100%" />
</p>

### Saga — Distributed Transaction with Compensation

Coordinate work across multiple services with automatic compensation on failure. Sagas are declared with decorators on a class; the engine builds the DAG, executes steps in dependency order, and rolls back via compensation steps if anything fails.

```python
from pyfly.transactional.saga import saga, saga_step
from pyfly.transactional.saga.core.context import SagaContext

@saga(name="place-order", timeout_ms=30_000)
class PlaceOrderSaga:
    def __init__(self, payments: PaymentService, inventory: InventoryService, ship: ShippingService) -> None:
        self._payments = payments
        self._inventory = inventory
        self._ship = ship

    # `compensate=` names a method on this class to invoke if a later step fails.
    @saga_step(id="reserve-inventory", retry=3, backoff_ms=500, compensate="release_inventory")
    async def reserve(self, ctx: SagaContext) -> str:
        return await self._inventory.reserve(ctx.input["order_id"])

    async def release_inventory(self, ctx: SagaContext) -> None:
        await self._inventory.release(ctx.input["order_id"])

    @saga_step(id="charge-payment", depends_on=["reserve-inventory"], compensate="refund_payment")
    async def charge(self, ctx: SagaContext) -> str:
        return await self._payments.charge(ctx.input["order_id"], ctx.input["amount"])

    async def refund_payment(self, ctx: SagaContext) -> None:
        await self._payments.refund(ctx.input["order_id"])

    @saga_step(id="ship-order", depends_on=["charge-payment"])
    async def ship_order(self, ctx: SagaContext) -> None:
        await self._ship.dispatch(ctx.input["order_id"])
```

**Highlights:** parallel-by-default DAG execution, per-step retries with jitter, backpressure, idempotency keys, metrics + tracing, pluggable persistence (in-memory, Redis, SQLAlchemy, Cache), DLQ with `RecoveryService`, REST controllers for list/start/retry, and `OrchestrationHealthIndicator`. Programmatic API also available via `SagaBuilder()`. See [docs/modules/transactional.md](docs/modules/transactional.md).

### Workflow — Durable, Signal-Driven Orchestration

Long-running processes that can wait for external events, sleep for hours, spawn child workflows, and be queried while running. Inspired by Temporal/Cadence, native to PyFly.

```python
from pyfly.transactional.workflow import (
    workflow, workflow_step, wait_for_signal, wait_for_timer,
    child_workflow, workflow_query, on_workflow_complete,
)

@workflow(id="loan-approval", version=2, timeout_ms=7 * 24 * 3600 * 1000)
class LoanApprovalWorkflow:
    def __init__(self, scoring: ScoringService, kyc: KycService) -> None:
        self._scoring = scoring
        self._kyc = kyc
        self._decision: str | None = None

    @workflow_step(id="run-scoring")
    async def run_scoring(self, ctx) -> int:
        return await self._scoring.score(ctx.input["applicant_id"])

    @child_workflow(workflow_id="kyc-verification", wait_for_completion=True, timeout_ms=3600_000)
    @workflow_step(id="kyc", depends_on=["run-scoring"])
    async def run_kyc(self, ctx) -> dict:
        return {"applicant_id": ctx.input["applicant_id"]}

    @wait_for_signal("manual-decision", timeout_ms=48 * 3600 * 1000)
    @workflow_step(id="await-officer", depends_on=["kyc"])
    async def await_officer(self, ctx, signal_payload: dict) -> str:
        self._decision = signal_payload["decision"]
        return self._decision

    @wait_for_timer(delay_ms=24 * 3600 * 1000)
    @workflow_step(id="cooling-off", depends_on=["await-officer"])
    async def cooling_off(self, ctx) -> None: ...

    @workflow_query(name="status")
    def get_status(self) -> str:
        return self._decision or "pending"

    @on_workflow_complete
    async def notify(self, ctx) -> None:
        ctx.publish("LoanDecided", {"id": ctx.input["applicant_id"], "decision": self._decision})
```

**Highlights:** `@wait_for_signal` / `@wait_for_timer` / `@wait_for_all` / `@wait_for_any`, child workflows, queries, scheduling via cron, lifecycle hooks (`@on_workflow_complete`, `@on_workflow_error`, `@on_step_complete`), `SignalService` + `TimerService` + `ContinueAsNewService`, `WorkflowController` REST API. See [docs/modules/transactional.md](docs/modules/transactional.md).

### TCC — Try / Confirm / Cancel

Strong-consistency three-phase distributed transactions for financial workloads where compensation alone isn't enough.

```python
from pyfly.transactional.tcc import tcc, tcc_participant, try_method, confirm_method, cancel_method
from pyfly.transactional.tcc.context import TccContext
from typing import Annotated
from pyfly.transactional.tcc.annotations import FromTry

@tcc(name="transfer-funds", timeout_ms=10_000, retry_enabled=True, max_retries=3)
class TransferFundsTcc: ...

@tcc_participant(id="debit-source", order=1)
class DebitSource:
    def __init__(self, ledger: Ledger) -> None: self._ledger = ledger

    @try_method(timeout_ms=5_000)
    async def try_debit(self, ctx: TccContext) -> str:
        return await self._ledger.hold(ctx.input["from"], ctx.input["amount"])

    @confirm_method()
    async def confirm(self, hold_id: Annotated[str, FromTry()], ctx: TccContext) -> None:
        await self._ledger.commit_hold(hold_id)

    @cancel_method()
    async def cancel(self, hold_id: Annotated[str, FromTry()], ctx: TccContext) -> None:
        await self._ledger.release_hold(hold_id)

@tcc_participant(id="credit-target", order=2)
class CreditTarget:
    def __init__(self, ledger: Ledger) -> None: self._ledger = ledger

    @try_method()
    async def try_credit(self, ctx: TccContext) -> str:
        return await self._ledger.reserve(ctx.input["to"], ctx.input["amount"])

    @confirm_method()
    async def confirm(self, reservation_id: Annotated[str, FromTry()], ctx: TccContext) -> None:
        await self._ledger.commit_reservation(reservation_id)

    @cancel_method()
    async def cancel(self, reservation_id: Annotated[str, FromTry()], ctx: TccContext) -> None:
        await self._ledger.release_reservation(reservation_id)
```

The TCC engine runs Try across all participants in `order`. If every Try succeeds, it runs Confirm. If any Try fails, it runs Cancel for every participant whose Try succeeded. Try-results flow into Confirm/Cancel via `Annotated[T, FromTry()]`. See [docs/modules/transactional.md](docs/modules/transactional.md).

### Event Sourcing — Aggregates, Event Store, Outbox

Persist state as an append-only event log. Aggregates rebuild from history; projections update read models; the outbox reliably publishes events to brokers.

```python
from dataclasses import dataclass
from pyfly.eventsourcing import (
    AggregateRoot, DomainEvent, domain_event,
    EventStore, SqlAlchemyEventStore, TransactionalOutbox,
)

@domain_event
@dataclass(frozen=True)
class AccountOpened(DomainEvent):
    account_id: str
    owner: str
    initial_balance: int

@domain_event
@dataclass(frozen=True)
class MoneyDeposited(DomainEvent):
    account_id: str
    amount: int

class Account(AggregateRoot):
    def __init__(self) -> None:
        super().__init__()
        self.owner: str = ""
        self.balance: int = 0
        self.when(AccountOpened, Account._on_opened)
        self.when(MoneyDeposited, Account._on_deposit)

    @classmethod
    def open(cls, account_id: str, owner: str, initial_balance: int) -> "Account":
        agg = cls()
        agg.id = account_id
        agg.apply(AccountOpened(account_id=account_id, owner=owner, initial_balance=initial_balance))
        return agg

    def deposit(self, amount: int) -> None:
        if amount <= 0:
            raise ValueError("amount must be positive")
        self.apply(MoneyDeposited(account_id=self.id, amount=amount))

    def _on_opened(self, e: AccountOpened) -> None:
        self.owner, self.balance = e.owner, e.initial_balance

    def _on_deposit(self, e: MoneyDeposited) -> None:
        self.balance += e.amount

# Persisting and rebuilding
store: EventStore = SqlAlchemyEventStore(session_factory)
account = Account.open("acc-42", "Alice", 100)
account.deposit(25)
await store.append(account.id, account.pending_events(), expected_version=account.version - len(account.pending_events()))
account.mark_committed()

# Later — reconstruct from the log:
events = await store.load("acc-42")
rebuilt = Account()
rebuilt.id = "acc-42"
for envelope in events:
    rebuilt.replay(envelope.event_type, envelope.event)
assert rebuilt.balance == 125
```

**Highlights:** `AggregateRoot` with `when()`/`apply()`/`replay()`, optimistic concurrency via `expected_version`, snapshots (`SnapshotStore`), `TransactionalOutbox` for at-least-once publishing, `Projection` + `ProjectionRunner` for read models, `EventUpcaster` for schema evolution. Adapters: `InMemoryEventStore`, `SqlAlchemyEventStore`. See [docs/modules/eventsourcing.md](docs/modules/eventsourcing.md).

### CQRS — Command/Query Buses with Validation, Authorization, Caching

```python
from pydantic import BaseModel
from pyfly.cqrs import command, query, CommandHandler, QueryHandler, CommandBus, QueryBus

class CreateUser(BaseModel):
    name: str
    email: str

class FindUserById(BaseModel):
    user_id: int

@command(CreateUser)
class CreateUserHandler(CommandHandler[CreateUser, int]):
    def __init__(self, repo: UserRepository) -> None:
        self._repo = repo

    async def handle(self, cmd: CreateUser) -> int:
        return (await self._repo.save(User(name=cmd.name, email=cmd.email))).id

@query(FindUserById, cache_ttl=60)
class FindUserByIdHandler(QueryHandler[FindUserById, User]):
    def __init__(self, repo: UserRepository) -> None: self._repo = repo
    async def handle(self, q: FindUserById) -> User:
        return await self._repo.find_by_id(q.user_id)

# Dispatch:
async def usage(commands: CommandBus, queries: QueryBus) -> None:
    user_id = await commands.dispatch(CreateUser(name="Ada", email="ada@example.com"))
    user = await queries.dispatch(FindUserById(user_id=user_id))
```

Command/query are auto-wired via the `CqrsAutoConfiguration` entry point. Cross-cutting concerns (`@validates`, `@authorizes`, `@cacheable`) compose declaratively. See [docs/modules/cqrs.md](docs/modules/cqrs.md).

### Inbound Webhooks — Verify · Dedupe · Dispatch

```python
from pyfly.webhooks import (
    AbstractWebhookEventListener, HmacSignatureValidator,
    InMemoryWebhookEventStore, WebhookProcessor,
)
from pyfly.container import service

@service
class StripePaymentListener(AbstractWebhookEventListener):
    source = "stripe"

    async def handle(self, event_type: str, payload: dict) -> None:
        match event_type:
            case "payment_intent.succeeded":
                await self._mark_paid(payload["data"]["object"]["id"])
            case "charge.refunded":
                await self._mark_refunded(payload["data"]["object"]["id"])

# Auto-wired by AdapterAutoConfiguration:
processor = WebhookProcessor(
    validator=HmacSignatureValidator(secret=os.environ["STRIPE_SIGNING_SECRET"]),
    store=InMemoryWebhookEventStore(),
    listeners=[stripe_payment_listener],
)
```

The `WebhookProcessor` validates the signature, deduplicates by event id, persists to the `WebhookEventStore`, and routes to `AbstractWebhookEventListener` instances by source. Plug it into any controller (`@post_mapping("/webhooks/stripe")`) and you get production-grade ingestion. See [docs/modules/webhooks.md](docs/modules/webhooks.md).

### Outbound Callbacks — HMAC-Signed Webhook Dispatch

```python
from pyfly.callbacks import CallbackDispatcher, CallbackSubscription, CallbackConfig

dispatcher: CallbackDispatcher  # @autowired

await dispatcher.dispatch(
    event_type="OrderShipped",
    payload={"order_id": "ord-42", "tracking": "1Z..."},
    correlation_id="corr-1",
)
# Reads subscriptions, filters authorized domains, signs with HMAC,
# retries with exponential backoff, persists CallbackExecution records.
```

Subscriptions, authorized domains, and execution history are first-class persisted entities. Configure retry policies, secret rotation, and per-subscription filters declaratively. See [docs/modules/callbacks.md](docs/modules/callbacks.md).

### Notifications — Email · SMS · Push (Provider-Agnostic)

```python
from pyfly.notifications import EmailMessage, EmailService, SmsMessage, SmsService

email_service: EmailService  # auto-wired with SendGrid / Resend / SMTP / dummy
sms_service:   SmsService    # auto-wired with Twilio / dummy

await email_service.send(EmailMessage(
    to=["alice@example.com"],
    subject="Welcome to Acme",
    html="<h1>Hi Alice</h1>",
))

await sms_service.send(SmsMessage(to="+34611222333", body="Your code is 4242"))
```

Configuration in `pyfly.yaml` selects the provider:

```yaml
pyfly:
  notifications:
    email:
      provider: sendgrid     # sendgrid | resend | smtp | dummy
      api-key: ${SENDGRID_API_KEY}
    sms:
      provider: twilio       # twilio | dummy
      account-sid: ${TWILIO_ACCOUNT_SID}
      auth-token: ${TWILIO_AUTH_TOKEN}
    push:
      provider: firebase     # firebase | dummy
      credentials-file: /run/secrets/firebase.json
```

See [docs/modules/notifications.md](docs/modules/notifications.md).

### Identity Provider (IDP) — Multi-Provider Auth

Single port, four interchangeable adapters (Keycloak, AWS Cognito, Azure AD, internal-DB).

```python
from pyfly.idp import IdpAdapter, LoginRequest

idp: IdpAdapter  # auto-wired

result = await idp.login(LoginRequest(username="alice@acme.com", password="hunter2"))
if result.requires_mfa:
    result = await idp.verify_mfa(result.mfa_challenge_id, code="123456")

session = await idp.introspect(result.access_token)
print(session.user.email, session.roles)
```

```yaml
pyfly:
  idp:
    provider: keycloak       # keycloak | aws-cognito | azure-ad | internal-db
    realm: acme
    server-url: https://auth.acme.com
    client-id: acme-app
    client-secret: ${KEYCLOAK_CLIENT_SECRET}
```

Switching providers is a YAML one-liner — your business code keeps depending on `IdpAdapter`. See [docs/modules/idp.md](docs/modules/idp.md).

### ECM — Documents · Folders · E-Signature

```python
from pyfly.ecm import DocumentService, ESignatureService, SignatureRequest, Recipient

documents: DocumentService     # auto-wired (S3 / Azure Blob / local-fs)
esig: ESignatureService        # auto-wired (DocuSign / Adobe Sign / Logalty / no-op)

doc = await documents.upload("contracts/2026/acme.pdf", file_bytes, mime_type="application/pdf")

envelope = await esig.send(SignatureRequest(
    document_id=doc.id,
    recipients=[
        Recipient(email="alice@acme.com", name="Alice", role="signer"),
        Recipient(email="legal@acme.com", name="Legal", role="approver"),
    ],
    subject="Please sign the SaaS agreement",
))

status = await esig.status(envelope.id)
```

Storage and e-signature are independent ports — combine S3 storage with DocuSign, or Azure Blob with Adobe Sign, or local-fs with the no-op signer for tests. See [docs/modules/ecm.md](docs/modules/ecm.md).

### Rule Engine — YAML DSL with AST Evaluation

Externalize business rules so non-developers can change them without redeploys.

```yaml
# rules/credit_approval.yaml
name: credit_approval
description: Decision rules for personal loans
inputs: [income, debt, credit_score, employment_years]
rules:
  - id: high_credit_fast_track
    when: credit_score >= 750 and income >= 50000
    then: { decision: "approve", limit: 50000 }
  - id: standard_review
    when: credit_score >= 650 and (debt / income) < 0.4
    then: { decision: "review", limit: 25000 }
  - id: reject_low_credit
    when: credit_score < 600 or employment_years < 1
    then: { decision: "reject" }
```

```python
from pyfly.rule_engine import RuleEngine, RuleSetRepository

rules: RuleEngine  # auto-wired

decision = await rules.evaluate("credit_approval", {
    "income": 75000, "debt": 12000, "credit_score": 770, "employment_years": 5,
})
# {'decision': 'approve', 'limit': 50000, '_rule_id': 'high_credit_fast_track'}
```

Audit trails (which rule fired, why), batch evaluation, hot-reload from `RuleSetRepository`. See [docs/modules/rule-engine.md](docs/modules/rule-engine.md).

### Plugin SPI — `@plugin` / `@extension_point` / `@extension`

Build extensible products: define extension points, let third-party packages contribute extensions.

```python
from pyfly.plugins import plugin, extension_point, extension

@extension_point
class PaymentMethod:
    def display_name(self) -> str: ...
    async def charge(self, amount: int, customer_id: str) -> str: ...

@plugin(name="acme-stripe", version="1.0.0", depends_on=[])
class StripePlugin: ...

@extension(point=PaymentMethod, plugin="acme-stripe")
class StripePayment(PaymentMethod):
    def display_name(self) -> str: return "Credit Card (Stripe)"
    async def charge(self, amount: int, customer_id: str) -> str:
        return await self._stripe.charges.create(amount=amount, customer=customer_id)
```

The plugin manager resolves the dependency graph, loads plugins in order, and registers extensions with the DI container. See [docs/modules/plugins.md](docs/modules/plugins.md).

### Domain — DDD Building Blocks

`pyfly.domain` ships the foundational types every domain-driven design codebase ends up reinventing — `Entity`, `ValueObject`, `AggregateRoot`, `DomainEvent`, `Specification`, `DomainRepository`, and domain-flavoured exceptions. The module is **pure standard-library Python** with zero runtime dependencies.

```python
from dataclasses import dataclass
from pyfly.domain import AggregateRoot, BusinessRuleViolation, DomainEvent, ValueObject

@dataclass(frozen=True, slots=True)
class Money(ValueObject):
    amount: int
    currency: str

@dataclass(frozen=True)
class OrderShipped(DomainEvent):
    order_id: str = ""
    tracking_number: str = ""

class Order(AggregateRoot[str]):
    def __init__(self, id: str, total: Money) -> None:
        super().__init__(id)
        self.total = total
        self.status = "placed"

    def ship(self, tracking_number: str) -> None:
        if self.status == "shipped":
            raise BusinessRuleViolation("order-already-shipped")
        self.status = "shipped"
        assert self.id is not None
        self.raise_event(OrderShipped(order_id=self.id, tracking_number=tracking_number))

# Application service:
order = Order("o-1", Money(100, "EUR"))
order.ship("trk-42")

events = order.clear_events()      # drained by the repository
# repository.save(order); for e in events: bus.publish(e)
```

For domain-tier microservices, the **`@enable_domain_stack`** starter activates CQRS, the transactional engine (saga/workflow/TCC), event sourcing, the rule engine, and the relational data layer in a single decorator — mirroring `fireflyframework-starter-domain` (Java) and `AddFireflyDomain` (.NET):

```python
from pyfly.core import pyfly_application
from pyfly.starters.domain import enable_domain_stack

@enable_domain_stack
@pyfly_application(name="my-service", scan_packages=["my_service"])
class Application:
    pass
```

The full DDD primitives are also re-exported from `pyfly.starters.domain` so a single import line is enough:

```python
from pyfly.starters.domain import (
    AggregateRoot, BusinessRuleViolation, DomainEvent, DomainRepository,
    Entity, Specification, ValueObject, enable_domain_stack,
)
```

See **[`samples/lumen/`](samples/lumen/README.md)** for an end-to-end DDD microservice that uses every primitive: a layered split (interfaces / models / core / web / sdk), a real `Wallet` aggregate built on a `Money` value object, the Spring-Data `Repository` (derived queries, pagination, specifications, projections), CQRS handlers, an event-sourced ledger, domain-event publishing, and a money-transfer saga with full compensation. See [docs/modules/domain.md](docs/modules/domain.md).

---

## Installation

> **Note:** PyFly is distributed exclusively via [GitHub Releases](https://github.com/fireflyframework/fireflyframework-pyfly/releases). It is **not** published to PyPI.

### Install from GitHub Release (Recommended)

```bash
# Install the latest release (uv)
uv add "pyfly @ https://github.com/fireflyframework/fireflyframework-pyfly/releases/latest/download/pyfly-26.5.4-py3-none-any.whl"

# Install with specific extras
uv add "pyfly[web,data-relational,cache] @ https://github.com/fireflyframework/fireflyframework-pyfly/releases/latest/download/pyfly-26.5.4-py3-none-any.whl"

# Or with pip
pip install "pyfly @ https://github.com/fireflyframework/fireflyframework-pyfly/releases/latest/download/pyfly-26.5.4-py3-none-any.whl"
```

### One-Line Install (CLI + Framework)

```bash
# Via get.pyfly.io
curl -fsSL https://get.pyfly.io/ | bash

# Or directly from GitHub
curl -fsSL https://raw.githubusercontent.com/fireflyframework/fireflyframework-pyfly/main/install.sh | bash
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
git clone https://github.com/fireflyframework/fireflyframework-pyfly.git
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

PyFly ships with **39 fully-implemented modules** organized into five layers — covering everything from HTTP routing and database access to distributed transactions, event sourcing, identity, content management, observability, and DDD building blocks:

### Foundation Layer

| Module | Description | Firefly Java Equivalent |
|--------|-------------|------------------------|
| **Core** | Application bootstrap, lifecycle, banner, configuration | `fireflyframework-starter-core` |
| **Kernel** | Exception hierarchy, structured error types | `fireflyframework-kernel` |
| **Container** | Dependency injection, stereotypes, bean factories | Spring DI (built-in) |
| **Context** | ApplicationContext, events, lifecycle hooks, conditions | Spring ApplicationContext |
| **Config** | Decentralized auto-configuration via `@auto_configuration` entry points | Spring Auto-Configuration |
| **Logging** | Unified structured logging — intercepts all loggers (framework + third-party) through one formatter; Spring-style config (`pyfly.logging.*` — patterns, file output, rotation, external config file); PII redaction on by default (regex; optional Microsoft Presidio via `pyfly[pii]`) | `fireflyframework-observability` |

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
| **Domain (DDD)** | `Entity`, `ValueObject`, `AggregateRoot`, `DomainEvent`, `Specification`, `DomainRepository`, `BusinessRuleViolation` | `fireflyframework-starter-domain` |
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
| **Observability** | Prometheus metrics, OpenTelemetry tracing, server-layer metrics (workers, connections, in-flight requests, uptime) across Uvicorn/Granian/Hypercorn with multi-worker Prometheus aggregation, surfaced in a live admin Observability dashboard | `fireflyframework-observability` |
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
- [Domain (DDD primitives)](docs/modules/domain.md) — Entity, ValueObject, AggregateRoot, DomainEvent, Specification, DomainRepository, exceptions
- [Starters](docs/modules/starters.md) — Layered bundles (`@enable_core_stack`, `@enable_web_stack`, `@enable_application_stack`, `@enable_data_stack`, `@enable_domain_stack`) with one-line imperative APIs for .NET parity
- [Plugins](docs/modules/plugins.md) — Plugin SPI, extension points, lifecycle
- [Rule Engine](docs/modules/rule-engine.md) — YAML DSL, AST evaluator, batch evaluation
- [Callbacks (outbound)](docs/modules/callbacks.md) — Dispatch domain events to external HTTP endpoints
- [Webhooks (inbound)](docs/modules/webhooks.md) — Receive, verify, dedupe, dispatch
- [Notifications](docs/modules/notifications.md) — Email / SMS / push abstractions
- [IDP (Identity Provider)](docs/modules/idp.md) — Keycloak / AWS Cognito / Azure AD / internal-DB adapters
- [ECM (Content Management)](docs/modules/ecm.md) — Documents, folders, e-signature workflows
- [Admin Dashboard](docs/modules/admin.md) — Embedded management dashboard, server mode, custom views
- [Logging](docs/modules/logging.md) — Unified structured logging, Spring-style `pyfly.logging.*` config, PII redaction (`pyfly[pii]` for Presidio NER)

### Adapter Reference

Browse the [Adapter Catalog](docs/adapters/README.md) for setup and configuration of each concrete backend:

- [SQLAlchemy](docs/adapters/sqlalchemy.md) · [MongoDB](docs/adapters/mongodb.md) · [Starlette](docs/adapters/starlette.md) · [FastAPI](docs/adapters/fastapi.md) · [Granian](docs/adapters/granian.md) · [Kafka](docs/adapters/kafka.md) · [RabbitMQ](docs/adapters/rabbitmq.md) · [Redis](docs/adapters/redis.md) · [HTTPX](docs/adapters/httpx.md) · [Click](docs/adapters/click.md)

Browse the full list in the [Documentation Table of Contents](docs/README.md).

---

## Roadmap

See **[ROADMAP.md](ROADMAP.md)** for the full roadmap toward feature parity with the Firefly Framework Java ecosystem (40+ modules).

| Phase | Focus | Key Modules | Status |
|-------|-------|-------------|--------|
| **Phase 1** | Core Distributed Patterns | Saga/TCC, Workflow, Event Sourcing | Complete (v26.05.01) |
| **Phase 2** | Business Logic | Rule Engine, Plugins | Complete (v26.05.01) |
| **Phase 3** | Enterprise Integrations | Notifications, IDP, ECM, Webhooks, Callbacks, Config Server | Complete (v26.05.01) |
| **Phase 4** | Administrative & DDD | Backoffice, Utils, ~~DDD starters~~ (done in v26.05.02) | DDD complete; backoffice / utils planned |

**v26.05.01** closes the parity gap with the Java Firefly Framework: the transactional engine has been rewritten from scratch (Saga + Workflow + TCC), nine new modules have been added (Event Sourcing, Callbacks, Webhooks, Notifications, IDP, ECM, Plugins, Rule Engine, Config Server), 12 third-party adapters were added, four new client protocols (SOAP/gRPC/GraphQL/WebSocket) were introduced, and the validation library now ships 16 domain validators. The framework is feature-complete for production microservice workloads.

---

## Versioning

PyFly uses **Calendar Versioning** ([CalVer](https://calver.org/)) — `YY.MM.PATCH` — to stay aligned with the rest of the Firefly Framework family (Java, .NET, Go).

| Component | Meaning |
|-----------|---------|
| `YY` | Two-digit year (e.g., `26` = 2026) |
| `MM` | Two-digit month of the release |
| `PATCH` | Patch number within the month (`01`, `02`, …) |

**Examples:** `26.05.01`, `26.05.02`, `26.06.01`.

The git tag and human-readable display use the leading-zero form (`v26.05.01`); the `pyproject.toml` `version` field uses PEP 440's normalized form (`26.5.1`) so Python tooling (uv, pip, hatchling) accepts it without warnings. Both reference the same release. See [docs/versioning.md](docs/versioning.md) for full details, including the migration from the previous SemVer-with-milestone scheme.

---

## Changelog

The full release history lives in **[CHANGELOG.md](CHANGELOG.md)** ([Keep a Changelog](https://keepachangelog.com/) format). Recent highlights:

- **`v26.06.113`** (2026-06-17) — **server-layer observability**: per-server metrics (active connections, in-flight requests, workers, uptime) across Uvicorn / Granian / Hypercorn, correct multi-worker Prometheus aggregation, and a live admin **Observability** dashboard.
- **`v26.06.112`** (2026-06-16) — *PyFly by Example* figures rebuilt in one polished, vector visual language (English + Spanish editions).
- **`v26.05.01`** (2026-05-07) — **full Java-framework parity**: the Saga + Workflow + TCC transactional engine, nine new modules (event sourcing, callbacks, webhooks, notifications, IDP, ECM, plugins, rule engine, config server), 12 third-party adapters, and the move to CalVer.

See **[CHANGELOG.md](CHANGELOG.md)** for every release and the complete notes.

---

## Firefly Framework Ecosystem

PyFly is part of the [Firefly Framework](https://github.com/fireflyframework) ecosystem — one programming model across every runtime:

<p align="center">
  <img src="assets/ecosystem.svg" alt="The Firefly Framework family: Java/Spring Boot, .NET, PyFly (Python, highlighted), Rust, Go CLI, Angular frontend, and GenAI — all sharing one programming model." width="100%" />
</p>

| Platform | Repository | Status |
|----------|-----------|--------|
| **Java / Spring Boot** | [`fireflyframework-*`](https://github.com/fireflyframework) (40+ modules) | Production |
| **.NET 9** | [`fireflyframework-dotnet`](https://github.com/fireflyframework/fireflyframework-dotnet) | Beta (CalVer 26.05+) |
| **Python** | [`fireflyframework-pyfly`](https://github.com/fireflyframework/fireflyframework-pyfly) | Beta (CalVer 26.05+) |
| **Rust** | [`fireflyframework-rust`](https://github.com/fireflyframework/fireflyframework-rust) | Active Development |
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

## Contributing

Contributions are welcome. PyFly is type-checked with **mypy (strict)**, formatted and linted with **Ruff**, and tested with **pytest** — the same gates CI enforces:

```bash
uv sync --all-extras --group dev
uv run ruff format . && uv run ruff check .
uv run mypy src
uv run pytest
```

Branch from `main`, keep changes focused, add tests for new behaviour, and open a pull request. By contributing you agree your work is licensed under Apache-2.0. See **[CONTRIBUTING.md](CONTRIBUTING.md)** for the full guide.

---

## License

Apache License 2.0 — [Firefly Software Foundation.](https://github.com/fireflyframework)
