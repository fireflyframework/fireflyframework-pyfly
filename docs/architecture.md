# Architecture Overview

This document describes the architectural vision, module organization, design decisions,
and internal mechanics of the PyFly framework. It is intended for contributors,
advanced users, and anyone who wants to understand *why* PyFly is structured the way it is.

---

## Table of Contents

1. [Introduction](#introduction)
2. [Architectural Vision](#architectural-vision)
3. [Hexagonal Architecture: Ports and Adapters](#hexagonal-architecture-ports-and-adapters)
   - [What Are Ports?](#what-are-ports)
   - [What Are Adapters?](#what-are-adapters)
   - [Hexagonal Diagram](#hexagonal-diagram)
4. [Module Layer Organization](#module-layer-organization)
   - [Foundation Layer](#foundation-layer)
   - [Application Layer](#application-layer)
   - [Infrastructure Layer](#infrastructure-layer)
   - [Cross-Cutting Layer](#cross-cutting-layer)
   - [Layer Diagram](#layer-diagram)
5. [The Startup Sequence in Detail](#the-startup-sequence-in-detail)
   - [Phase 1: PyFlyApplication Constructor](#phase-1-pyflyapplication-constructor)
   - [Phase 2: startup()](#phase-2-startup)
   - [Phase 3: ApplicationContext.start()](#phase-3-applicationcontextstart)
   - [Complete Startup Flow Diagram](#complete-startup-flow-diagram)
6. [Dependency Injection Architecture](#dependency-injection-architecture)
   - [Container vs ApplicationContext](#container-vs-applicationcontext)
   - [Registration and Resolution](#registration-and-resolution)
   - [Stereotype System](#stereotype-system)
7. [Auto-Configuration](#auto-configuration)
   - [Declarative Auto-Configuration](#declarative-auto-configuration)
   - [Decentralized Auto-Configuration via Entry Points](#decentralized-auto-configuration-via-entry-points)
   - [Provider Detection](#provider-detection)
8. [Framework-Agnostic Design](#framework-agnostic-design)
   - [Web Module](#web-module)
   - [Data Module](#data-module)
   - [Messaging Module](#messaging-module)
   - [Cache Module](#cache-module)
   - [Scheduling Module](#scheduling-module)
9. [Configuration Architecture](#configuration-architecture)
10. [Event-Driven Design](#event-driven-design)
    - [Application Events](#application-events)
    - [Domain Events (EDA)](#domain-events-eda)
11. [Cross-Cutting Concerns](#cross-cutting-concerns)
    - [Aspect-Oriented Programming](#aspect-oriented-programming)
    - [Observability](#observability)
    - [Resilience](#resilience)
    - [Security](#security)
    - [Validation](#validation)
12. [Design Decisions and Trade-Offs](#design-decisions-and-trade-offs)

---

## Introduction

PyFly is an enterprise Python framework that brings the architectural patterns of
Spring Boot to the Python ecosystem while remaining Pythonic, async-first, and
lightweight. It provides a comprehensive foundation for building production-grade
applications with dependency injection, configuration management, web serving, data
access, messaging, caching, security, observability, and more.

The framework is organized around two key architectural ideas:

1. **Hexagonal (ports-and-adapters) architecture** at the module level.
2. **Layered module organization** that enforces dependency direction.

---

## Architectural Vision

PyFly's vision is to let application developers focus on business logic while the
framework handles infrastructure concerns. The guiding principles are:

- **Convention over configuration**: sensible defaults for everything; override only
  what differs.
- **Dependency inversion**: application code depends on ports (interfaces), not on
  infrastructure details.
- **Framework-agnostic abstractions**: swap web frameworks, databases, message brokers,
  or cache backends without touching business logic.
- **Async-first**: all I/O-bound operations use `async`/`await` natively.
- **Testability**: constructor injection and interface binding make every component
  independently testable.

---

## Hexagonal Architecture: Ports and Adapters

PyFly implements hexagonal architecture (also called ports and adapters) at the module
level. Every infrastructure-facing module separates its abstract contracts from
concrete implementations.

### What Are Ports?

Ports are abstract contracts (Python `Protocol` classes or ABCs) that define what
the application needs without specifying how. They live in `ports/` subdirectories
within each module.

Examples of ports in the framework:

| Port | Module | Purpose |
|---|---|---|
| `RepositoryPort` | `pyfly.data.ports.outbound` | Defines data access operations (save, find, delete). |
| `TransactionManager` | `pyfly.data.transaction` | Opens, commits and rolls back units of work on one datasource (`SessionPort` is its deprecated alias). |
| `MessageBrokerPort` | `pyfly.messaging.ports.outbound` | Defines message publishing and subscribing. |
| `CacheAdapter` | `pyfly.cache.ports.outbound` | Defines cache get/set/delete operations. |
| `HttpClientPort` | `pyfly.client.ports.outbound` | Defines HTTP request operations. |
| `TaskExecutorPort` | `pyfly.scheduling.ports.outbound` | Defines task execution. |
| `ApplicationServerPort` | `pyfly.server.ports.outbound` | Defines the contract for running an ASGI application on a network socket. |
| `EventLoopPort` | `pyfly.server.ports.outbound` | Defines the contract for configuring the asyncio event loop policy. |
| `LoggingPort` | `pyfly.logging` | Defines structured logging operations. |

### What Are Adapters?

Adapters are concrete implementations of ports, bound to a specific technology. They
live in `adapters/` subdirectories within each module.

Examples of adapters:

| Adapter | Module | Implements | Technology |
|---|---|---|---|
| `Repository` | `pyfly.data.relational.sqlalchemy` | `RepositoryPort` | SQLAlchemy |
| `KafkaAdapter` | `pyfly.messaging.adapters.kafka` | `MessageBrokerPort` | aiokafka |
| `RabbitMQAdapter` | `pyfly.messaging.adapters.rabbitmq` | `MessageBrokerPort` | aio_pika |
| `InMemoryMessageBroker` | `pyfly.messaging.adapters.memory` | `MessageBrokerPort` | In-process |
| `RedisCacheAdapter` | `pyfly.cache.adapters.redis` | `CacheAdapter` | redis.asyncio |
| `InMemoryCache` | `pyfly.cache.adapters.memory` | `CacheAdapter` | In-process dict |
| `HttpxClientAdapter` | `pyfly.client.adapters.httpx_adapter` | `HttpClientPort` | HTTPX |
| `ControllerRegistrar` | `pyfly.web.adapters.starlette` | Web routing | Starlette/ASGI |
| `FastAPIControllerRegistrar` | `pyfly.web.adapters.fastapi` | Web routing | FastAPI/ASGI |
| `GranianServerAdapter` | `pyfly.server.adapters.granian` | `ApplicationServerPort` | Granian (Rust/tokio) |
| `UvicornServerAdapter` | `pyfly.server.adapters.uvicorn` | `ApplicationServerPort` | Uvicorn |
| `HypercornServerAdapter` | `pyfly.server.adapters.hypercorn` | `ApplicationServerPort` | Hypercorn |
| `UvloopEventLoopAdapter` | `pyfly.server.adapters.uvloop` | `EventLoopPort` | uvloop (libuv) |
| `AsyncIOTaskExecutor` | `pyfly.scheduling.adapters.asyncio_executor` | `TaskExecutorPort` | asyncio |
| `ThreadPoolTaskExecutor` | `pyfly.scheduling.adapters.thread_executor` | `TaskExecutorPort` | concurrent.futures |
| `StructlogAdapter` | `pyfly.logging` | `LoggingPort` | structlog |

### Hexagonal Diagram

```
                     +-------------------------------------------+
                     |           Application Core                |
                     |                                           |
                     |   @service    @repository   @component    |
                     |   Business logic, domain models           |
                     |                                           |
                     +------+----+---------+----+--------+-------+
                            |    |         |    |        |
                     +------v-+  |  +------v-+  |  +-----v------+
                     | Port   |  |  | Port   |  |  | Port       |
                     | Data   |  |  | Cache  |  |  | Messaging  |
                     +------+-+  |  +------+-+  |  +-----+------+
                            |    |         |    |        |
               +------------v-+  |  +------v--+ |  +-----v--------+
               | Adapter      |  |  | Adapter | |  | Adapter      |
               | SQLAlchemy   |  |  | Redis   | |  | Kafka        |
               +--------------+  |  +---------+ |  +--------------+
                                 |
                          +------v-------+
                          | Adapter      |
                          | Starlette    |
                          +--------------+
```

Application code depends only on ports. Adapters are wired at startup through DI container
bindings or auto-configuration. Swapping an adapter (e.g., from Redis to Memcached)
requires only changing a binding -- no business logic changes.

---

## Unified Lifecycle Protocol

All infrastructure adapters implement the `Lifecycle` protocol from `pyfly.kernel`:

```python
from typing import Protocol, runtime_checkable

@runtime_checkable
class Lifecycle(Protocol):
    async def start(self) -> None: ...
    async def stop(self) -> None: ...
```

This provides a standard contract for managing infrastructure connections:

- **`start()`** — Initialize connections, validate connectivity (e.g., Redis `PING`, Kafka broker connect). Called during application startup.
- **`stop()`** — Release resources, close connections. Called during application shutdown in reverse order.

All port protocols include `start()` and `stop()` alongside their domain methods:

| Port | Domain Methods | Lifecycle |
|---|---|---|
| `MessageBrokerPort` | `publish()`, `subscribe()` | `start()`, `stop()` |
| `CacheAdapter` | `get()`, `put()`, `evict()`, `exists()`, `clear()` | `start()`, `stop()` |
| `HttpClientPort` | `request()` | `start()`, `stop()` |
| `TaskExecutorPort` | `submit()` | `start()`, `stop()` |
| `EventPublisher` | `publish()`, `subscribe()` | `start()`, `stop()` |

This unified lifecycle enables:
- **Fail-fast startup**: adapters validate connectivity in `start()`, failing immediately if infrastructure is unreachable.
- **Graceful shutdown**: adapters release resources in `stop()`, called in reverse initialization order.
- **Consistent testing**: mock adapters implement the same `start()`/`stop()` contract.
- **Generic infrastructure startup**: `ApplicationContext._start_lifecycle_beans()` starts every
  singleton that defines `start()` and `stop()` methods, in ascending phase -- no hardcoded
  subsystem knowledge required. This also enables lifecycle beans like `BeanieInitializer`
  (which binds Beanie and releases the shared PyMongo `AsyncMongoClient` when the context
  disposes its resources).

---

## Module Layer Organization

PyFly's modules are organized into four layers. Dependencies flow downward only: higher
layers may depend on lower layers, but never the reverse.

### Foundation Layer

The foundation provides primitives with zero or minimal external dependencies.

| Module | Package | Purpose |
|---|---|---|
| **Kernel** | `pyfly.kernel` | Exception hierarchy (`PyFlyException` and 25+ domain-specific subclasses), error types (`ErrorResponse`, `ErrorCategory`, `ErrorSeverity`, `FieldError`), `Lifecycle` protocol for unified adapter lifecycle. Zero external dependencies. |
| **Core** | `pyfly.core` | Application bootstrap (`PyFlyApplication`, `@pyfly_application`), configuration (`Config`, `@config_properties`), and banner rendering (`BannerPrinter`, `BannerMode`). |
| **Container** | `pyfly.container` | DI container (`Container`), stereotypes (`@service`, `@component`, `@repository`, `@controller`, `@rest_controller`, `@configuration`), scopes (`Scope`), `@bean`, `@primary`, `@order`, `Qualifier`, component scanning. |
| **Context** | `pyfly.context` | `ApplicationContext`, lifecycle hooks (`@post_construct`, `@pre_destroy`), `BeanPostProcessor` protocol, conditions (`@conditional_on_property`, `@conditional_on_class`, `@conditional_on_bean`, `@conditional_on_missing_bean`), events (`ApplicationReadyEvent`, `ContextRefreshedEvent`, `ContextClosedEvent`, `@app_event_listener`), `Environment`. |
| **Config** | `pyfly.config` | Auto-configuration utilities (`AutoConfiguration` for provider detection, `discover_auto_configurations()` for entry-point discovery). Each subsystem owns its own `@auto_configuration` class. |
| **Logging** | `pyfly.logging` | Logging port (`LoggingPort`) and structlog adapter (`StructlogAdapter`). |

### Application Layer

Application-layer modules provide framework-agnostic domain patterns.

| Module | Package | Purpose |
|---|---|---|
| **Validation** | `pyfly.validation` | Input validation decorators (`@validate_input`, `@validator`) and Pydantic model validation (`validate_model`). |
| **CQRS** | `pyfly.cqrs` | Command/Query Responsibility Segregation: `Command`, `Query`, `CommandHandler`, `QueryHandler`, `CommandBus`, `QueryBus`, `HandlerRegistry`, validation, authorization, caching, and distributed tracing. |
| **EDA** | `pyfly.eda` | Event-Driven Architecture: `EventPublisher` port, `EventEnvelope`, `@event_listener`, `@publish_result`, `InMemoryEventBus`, `ErrorStrategy`. |

### Infrastructure Layer

Infrastructure modules follow the hexagonal pattern: ports in `ports/`, adapters in
`adapters/`.

| Module | Package | Ports | Adapters |
|---|---|---|---|
| **Web** | `pyfly.web` | Mappings (`@get_mapping`, `@post_mapping`, etc.), params (`Body`, `PathVar`, `QueryParam`, `Header`, `Cookie`, `Valid`), `CORSConfig`, `SecurityHeadersConfig`, `WebFilter` protocol, `OncePerRequestFilter`, `@exception_handler`. Config-driven adapter selection (`pyfly.web.adapter`). | Starlette/ASGI (`StarletteWebAdapter`, `ControllerRegistrar`, `create_app`, `WebFilterChainMiddleware`, built-in filters), FastAPI (`FastAPIWebAdapter`, `FastAPIControllerRegistrar`). |
| **Server** | `pyfly.server` | `ApplicationServerPort` (ASGI server contract), `EventLoopPort` (event loop policy contract), `ServerProperties`. Cascading auto-configuration for server and event loop selection. | Granian (`GranianServerAdapter`), Uvicorn (`UvicornServerAdapter`), Hypercorn (`HypercornServerAdapter`), uvloop (`UvloopEventLoopAdapter`), winloop (`WinloopEventLoopAdapter`), asyncio (`AsyncioEventLoopAdapter`). |
| **Data** | `pyfly.data` | `RepositoryPort`, `TransactionManager` (`pyfly.data.transaction`; `SessionPort` is its deprecated alias), `QueryMethodCompilerPort`. | SQLAlchemy (`Repository`, `Specification`, `FilterUtils`, `@query`, `QueryMethodCompiler`, `RepositoryBeanPostProcessor`), MongoDB (`MongoRepository`, `BaseDocument`, `MongoQueryMethodCompiler`, `MongoRepositoryBeanPostProcessor`). |
| **Messaging** | `pyfly.messaging` | `MessageBrokerPort`, `MessageHandler`, `Message`, `@message_listener`. | Kafka (`KafkaAdapter`), RabbitMQ (`RabbitMQAdapter`), in-memory (`InMemoryMessageBroker`). |
| **Cache** | `pyfly.cache` | `CacheAdapter`, `CacheManager`, `@cacheable`, `@cache_evict`, `@cache_put`. | Redis (`RedisCacheAdapter`), in-memory (`InMemoryCache`). |
| **Client** | `pyfly.client` | `HttpClientPort`, `@service_client`, `@http_client`, `CircuitBreaker`, `RetryPolicy`, declarative `@get`, `@post`, `@put`, `@patch`, `@delete`. | HTTPX (`HttpxClientAdapter`), `HttpClientBeanPostProcessor`. |
| **Scheduling** | `pyfly.scheduling` | `TaskExecutorPort`, `CronExpression`, `TaskScheduler`, `@scheduled`, `@async_method`. | AsyncIO (`AsyncIOTaskExecutor`), thread pool (`ThreadPoolTaskExecutor`). |
| **Security** | `pyfly.security` | `SecurityContext`, `PasswordEncoder`, `@secure`. | `JWTService`, `BcryptPasswordEncoder`, `SecurityMiddleware` (canonical: `pyfly.web.adapters.starlette.security_middleware`, re-exported from `pyfly.security`). |
| **Actuator** | `pyfly.actuator` | `HealthIndicator`, `HealthAggregator`, `HealthResult`, `HealthStatus`, `ActuatorEndpoint` protocol, `ActuatorRegistry`. | Extensible HTTP endpoints (`make_starlette_actuator_routes`), built-in: health, beans, env, info, loggers, metrics. Per-endpoint enable/disable via config. |

### Cross-Cutting Layer

Cross-cutting modules provide capabilities that span all other layers.

| Module | Package | Purpose |
|---|---|---|
| **AOP** | `pyfly.aop` | Aspect-Oriented Programming: `@aspect`, `@before`, `@after`, `@around`, `@after_returning`, `@after_throwing`, pointcut matching (`matches_pointcut`), `AspectBeanPostProcessor`, `JoinPoint`, `AspectRegistry`. |
| **Observability** | `pyfly.observability` | Metrics (`@timed`, `@counted`, `MetricsRegistry`), tracing (`@span`), health checks (`HealthChecker`, `HealthResult`, `HealthStatus`), structured logging (`configure_logging`, `get_logger`). |
| **Resilience** | `pyfly.resilience` | Rate limiting (`@rate_limiter`, `RateLimiter`), bulkhead (`@bulkhead`, `Bulkhead`), timeout (`@time_limiter`), fallback (`@fallback`). |
| **Testing** | `pyfly.testing` | Test utilities (`PyFlyTestCase`, `create_test_container`), event assertions (`assert_event_published`, `assert_no_events_published`). |
| **CLI** | `pyfly.cli` | Project scaffolding and management commands. |

### Layer Diagram

```
+-----------------------------------------------------------------------+
|                         Cross-Cutting Layer                           |
|   aop    observability    resilience    testing    cli                |
+-----------------------------------------------------------------------+
          |                   |                  |
+-----------------------------------------------------------------------+
|                       Infrastructure Layer                            |
|   web    server    data    messaging    cache    client    scheduling |
|   security    actuator                                                |
|                                                                       |
|   Each module:  ports/  <--  adapters/                                |
+-----------------------------------------------------------------------+
          |                   |                  |
+-----------------------------------------------------------------------+
|                        Application Layer                              |
|   validation    cqrs    eda                                           |
+-----------------------------------------------------------------------+
          |                   |                  |
+-----------------------------------------------------------------------+
|                        Foundation Layer                               |
|   kernel    core    container    context    config    logging         |
+-----------------------------------------------------------------------+
```

### Module Structure Convention

Every infrastructure module follows this standard layout:

```
pyfly/
  <module>/
    __init__.py          # Public API exports
    ports/
      __init__.py
      outbound.py        # Protocol definitions (ports)
    adapters/
      __init__.py
      memory.py          # In-memory adapter (testing / fallback)
      <vendor>/          # Vendor-specific adapter
        __init__.py
        ...
    decorators.py        # Module-specific decorators
    types.py             # Data types and enums
    *.py                 # Domain logic
```

---

## The Startup Sequence in Detail

The startup sequence is divided across the `PyFlyApplication` constructor, the
`startup()` async method, and the `ApplicationContext.start()` method.

### Phase 1: PyFlyApplication Constructor

When `PyFlyApplication(app_class)` is called:

1. **Read metadata** from the `@pyfly_application`-decorated class: `name`, `version`,
   `scan_packages`, `description`.
2. **Find config file** -- check the explicit `config_path`, then auto-discover by checking
   candidates in order: `pyfly.yaml`, `pyfly.toml`, `config/pyfly.yaml`, `config/pyfly.toml`.
3. **Resolve profiles early** -- call `_resolve_profiles_early()` which reads
   `PYFLY_PROFILES_ACTIVE` env var, then reads `pyfly.profiles.active` from the raw
   base config file (using `yaml.safe_load`). This must happen before full config loading
   so profile overlay files can be merged.
4. **Load configuration** via `Config.from_file()`, which merges:
   - Framework defaults (`pyfly-defaults.yaml` via `importlib.resources`)
   - Base config file
   - Profile overlay files (for each active profile)
5. **Configure logging** -- create `StructlogAdapter`, configure from the logging section
   of the config.
6. **Create ApplicationContext** -- wraps a new `Container`, `Environment`, and
   `ApplicationEventBus`. The `Config` object is registered as a singleton bean so it can
   be injected into any component.
7. **Scan packages** -- for each package in `scan_packages`, call `scan_package()` which
   recursively discovers and registers stereotype-decorated classes. Logs the package name
   and bean count for each scan.

### Phase 2: startup()

When `await app.startup()` is called:

1. **Render and print banner** -- create `BannerPrinter.from_config()`, render the banner
   text (ASCII art, minimal, or off), print to stdout, flush.
2. **Log startup info** -- `"Starting {app} v{version}"`.
3. **Log profiles** -- `"Active profiles: [...]"` or `"No active profiles set, falling back to default"`.
4. **Call `await ApplicationContext.start()`** -- the core initialization (Phase 3).
5. **Record startup time** -- `time.perf_counter()` delta.
6. **Log completion** -- `"Started {app} in {time}s ({count} beans initialized)"`.

### Phase 3: ApplicationContext.start()

This is where the bulk of the DI and lifecycle work happens. The steps are:

0. **Register auto-configurations** -- `_register_auto_configurations()` discovers
   `@auto_configuration` classes via `importlib.metadata.entry_points(group="pyfly.auto_configuration")`.
   Each subsystem registers its own auto-configuration class as an entry point in
   `pyproject.toml` (analogous to Spring Boot's `META-INF/spring.factories`). The six
   built-in auto-configuration classes are: `WebAutoConfiguration`,
   `CacheAutoConfiguration`, `MessagingAutoConfiguration`, `ClientAutoConfiguration`,
   `DocumentAutoConfiguration`, `RelationalAutoConfiguration`. Third-party packages
   can add their own by declaring entries in the same group.

1. **Filter by profile** -- `_filter_by_profile()` removes registrations whose
   `__pyfly_profile__` expression does not match the `Environment.active_profiles`.
   Uses `Environment.accepts_profiles()` which supports negation and comma-separated OR.

1b. **Evaluate conditions (pass 1)** -- `_evaluate_conditions()` calls
   `ConditionEvaluator.should_include(cls, bean_pass=False)` and removes beans that
   fail non-bean-dependent conditions:
   - `@conditional_on_property` -- config key must exist (and optionally match a value).
   - `@conditional_on_class` -- Python module must be importable.
   - Stereotype `condition` callable -- must return `True`.

2. **Process user `@configuration` classes** -- `_process_configurations(auto=False)`:
   - Find all `@configuration`-stereotyped classes that are NOT `@auto_configuration`.
   - Resolve the configuration class itself.
   - Find `@bean`-decorated methods.
   - Read the return type hint to determine the bean type.
   - Call each method (injecting parameters from the container).
   - Register the produced bean by its return type.

2b. **Evaluate conditions (pass 2)** -- `_evaluate_bean_conditions()` calls
   `ConditionEvaluator.should_include(cls, bean_pass=True)` and removes beans that
   fail bean-dependent conditions:
   - `@conditional_on_bean` -- another bean of the specified type must exist.
   - `@conditional_on_missing_bean` -- no bean of the specified type must exist.
   This runs after user `@configuration` processing so user-provided beans are visible.

2c. **Process `@auto_configuration` classes** -- `_process_configurations(auto=True)`:
   same as step 2 but only for classes with `__pyfly_auto_configuration__ = True`.
   Each subsystem's `@auto_configuration` class owns its own wiring: it uses
   `@conditional_on_class` / `@conditional_on_property` / `@conditional_on_missing_bean`
   to guard its `@bean` methods, and delegates provider detection to its own
   ``detect_provider()`` static method when the configured provider is `"auto"`.

2d. **Complete deferred user `@bean` methods** -- a user factory whose parameters were not
   registered yet is called now that the auto-configurations ran.

2e. **Start lifecycle beans** -- `_start_lifecycle_beans()` starts every singleton whose
   class defines `start()` and `stop()` lifecycle methods, in ascending phase (startup
   migrations, the schema strategy, the default phase, then the consumers). This is fully
   generic -- it does not hardcode subsystem names. Examples of lifecycle beans:
   `RedisCacheAdapter`, `KafkaAdapter`, `BeanieInitializer`, `HttpxClientAdapter`. On
   failure, `BeanCreationException` is raised immediately (fail-fast), after the lifecycle beans
   already started are stopped in reverse order (a start that fails at any later step stops them
   too). The lifecycle beans
   created later (step 5) start at step 5b; see
   [The start() Lifecycle](modules/dependency-injection.md#the-start-lifecycle).

3. **Discover post-processors** -- `_discover_post_processors()` scans registered
   beans for `BeanPostProcessor` implementations and adds them to the post-processor
   list.

4. **Eagerly resolve all singletons** -- sorted by `@order` value (lower = higher
   priority = resolved first). For each singleton registration with no cached instance,
   call `container.resolve()` (which triggers constructor injection recursively).
   Resolution failures are silently suppressed via `contextlib.suppress(KeyError)`.

5. **Run post-processors and lifecycle hooks** -- for each resolved bean instance,
   in registration order:
   - All `BeanPostProcessor.before_init()` methods (sorted by `@order` of the post-processor).
   - All `@post_construct` methods on the bean (async-aware: if the method returns an
     awaitable, it is `await`ed).
   - All `BeanPostProcessor.after_init()` methods (sorted by `@order` of the post-processor).
   Post-processors can return replacement beans (enabling proxy patterns like AOP).

6. **Wire decorator-based beans** -- connect `@app_event_listener`,
   `@message_listener`, CQRS handlers, `@scheduled`, and `@async_method` to their
   respective targets.

7. **Publish lifecycle events**:
   - `ContextRefreshedEvent` -- signals that the context is fully initialized.
   - `ApplicationReadyEvent` -- signals that the application is ready to serve requests.

### Complete Startup Flow Diagram

```
PyFlyApplication.__init__(app_class)
    |
    +-- Read @pyfly_application metadata
    +-- Find config file (auto-discover or explicit)
    +-- Resolve profiles early (env var or config file)
    +-- Config.from_file() (merge defaults + base + profiles)
    +-- Configure logging (StructlogAdapter)
    +-- Create ApplicationContext (Container + Environment + EventBus)
    +-- Register Config as singleton bean
    +-- Scan packages -> register stereotype beans
    |
await app.startup()
    |
    +-- Render & print banner (TEXT / MINIMAL / OFF)
    +-- Log "Starting {app} v{version}"
    +-- Log active profiles
    |
    +-- await ApplicationContext.start()
    |       |
    |       +-- 0.  _register_auto_configurations()
    |       |       +-- discover_auto_configurations() via entry points
    |       |       +-- Register each @auto_configuration class
    |       +-- 1.  _filter_by_profile()
    |       +-- 1b. _evaluate_conditions() (pass 1: on_property, on_class)
    |       +-- 2.  _process_configurations(auto=False)  [user @configuration]
    |       +-- 2b. _evaluate_bean_conditions() (pass 2: on_bean, on_missing_bean)
    |       +-- 2c. _process_configurations(auto=True)   [@auto_configuration]
    |       +-- 2d. Complete deferred user @bean methods
    |       +-- 2e. _start_lifecycle_beans()
    |       |       +-- For each bean with start()/stop(), in ascending phase: await bean.start()
    |       |       +-- On failure: BeanCreationException
    |       +-- 3.  _discover_post_processors()
    |       +-- 4.  Eagerly resolve singletons (sorted by @order)
    |       +-- 5.  For each bean:
    |       |       +-- BeanPostProcessor.before_init()
    |       |       +-- @post_construct methods
    |       |       +-- BeanPostProcessor.after_init()
    |       +-- 6.  Wire decorator-based beans (@app_event_listener, etc.)
    |       +-- 7.  Publish ContextRefreshedEvent
    |       +-- 7b. Publish ApplicationReadyEvent
    |
    +-- Record startup time
    +-- Log "Started {app} in {time}s ({count} beans)"
```

### Shutdown Sequence

```
await app.shutdown()
    |
    +-- Log "Shutting down {app}"
    +-- await ApplicationContext.stop()
            |
            +-- 1. Publish ContextClosedEvent
            +-- 2. Drain: tasks, TaskScheduler, lifecycle beans of CONSUMER_PHASE and above
            +-- 3. @pre_destroy in reverse dependency order (no new singleton), then scoped instances
            +-- 4. The other lifecycle beans stop, then singleton destroy methods run
            +-- 5. ResourceRegistry.dispose_all(): the DataSourceRegistry closes every engine, last
            +-- 6. Release: resolving a bean afterwards raises BeanCreationNotAllowedError
```

See [The stop() Lifecycle](modules/dependency-injection.md#the-stop-lifecycle) for each step.

---

## Dependency Injection Architecture

### Container vs ApplicationContext

PyFly's DI system has two layers, each with a distinct responsibility:

| Component | Role | When to Use |
|---|---|---|
| `Container` | Low-level DI engine. Stores `Registration` objects, resolves by type hints, handles scopes, `@primary`, and `Qualifier`. No lifecycle awareness. Registered as a singleton bean so it can be injected into any component (e.g., auto-configuration classes that need to scan the registry). | Standalone tests, framework extensions, custom resolution logic, auto-configuration classes that need registry access. |
| `ApplicationContext` | High-level orchestrator. Wraps the Container and adds `@bean` factory methods, lifecycle hooks, post-processors, profile filtering, condition evaluation, auto-configuration, and events. | Always use in application code. |

The `ApplicationContext` exposes the underlying container via its `container` property
as an escape hatch for advanced use cases.

### Registration and Resolution

Bean registration happens through multiple channels:

1. **Component scanning** -- `scan_package()` discovers stereotype-decorated classes and
   calls `container.register()` for each.
2. **Manual registration** -- `container.register(cls)` or `context.register_bean(cls)`.
3. **@bean factory methods** -- methods inside `@configuration` classes, processed by
   `_process_configurations()`.
4. **Auto-configuration** -- per-subsystem `@auto_configuration` classes (discovered via
   entry points) register adapter beans through their `@bean` methods.

Resolution follows these rules (in `Container.resolve()`):

1. **Direct registration** -- if the requested type has a `Registration`, use it.
2. **Interface binding** -- check `_bindings` for the type. If exactly one implementation
   is bound, resolve it.
3. **@primary disambiguation** -- if multiple implementations are bound, select the one
   with `__pyfly_primary__ = True`.
4. **Qualifier** -- when a constructor parameter uses `Annotated[T, Qualifier("name")]`,
   resolution bypasses type-based lookup and uses `resolve_by_name()` directly.

Constructor dependencies are resolved recursively. The container inspects
`typing.get_type_hints(init, include_extras=True)` to discover parameter types, including
`Annotated` metadata.

### Stereotype System

All stereotypes (`@component`, `@service`, `@repository`, `@controller`,
`@rest_controller`, `@configuration`) are generated by the `_make_stereotype()` factory
in `pyfly.container.stereotypes`. They are functionally identical at the container level.
The differences are:

- **Semantic**: the `__pyfly_stereotype__` string communicates architectural intent.
- **Processing**: `"configuration"` triggers `@bean` method scanning in
  `ApplicationContext._process_configurations()`.
- **Discovery**: `"rest_controller"` is used by the web layer to find controllers.
- **Extensibility**: future features (e.g., repository exception translation) can key
  off the stereotype label.

---

## Auto-Configuration

PyFly uses a fully decentralized auto-configuration model. Each subsystem owns its
own `@auto_configuration` class, discovered at startup via entry points.

### Declarative Auto-Configuration

The `@auto_configuration` decorator (from `pyfly.context.conditions`) marks a
`@configuration` class for deferred processing. Combined with `@conditional_on_*`
decorators, it provides "default with override" semantics:

```python
@auto_configuration
@conditional_on_missing_bean(CacheAdapter)
@conditional_on_property("pyfly.cache.enabled", having_value="true")
class CacheAutoConfiguration:
    @bean
    def cache_adapter(self, config: Config) -> CacheAdapter:
        provider = CacheAutoConfiguration.detect_provider()
        if provider == "redis":
            return RedisCacheAdapter(...)
        return InMemoryCache()
```

This bean is only created if (1) no user-provided `CacheAdapter` exists and (2) the
cache subsystem is enabled in config.

Auto-configuration classes:
- Are discovered via `importlib.metadata.entry_points(group="pyfly.auto_configuration")`
  by `discover_auto_configurations()` in `pyfly.config.auto`.
- Receive an implicit `@order(1000)` (lower priority than user beans at default 0).
- Are processed after user `@configuration` classes.
- Have `__pyfly_auto_configuration__ = True`, `__pyfly_injectable__ = True`, and
  `__pyfly_stereotype__ = "configuration"` set by the decorator.

The twenty built-in auto-configuration classes are:

| Auto-Configuration Class | Module | Port/Bean | Conditions |
|---|---|---|---|
| `WebAutoConfiguration` | `pyfly.web.auto_configuration` | `WebServerPort` | `@conditional_on_class("starlette")`, `@conditional_on_missing_bean(WebServerPort)` |
| `FastAPIAutoConfiguration` | `pyfly.web.auto_configuration` | `WebServerPort` | `@conditional_on_class("fastapi")`, `@conditional_on_missing_bean(WebServerPort)` |
| `GranianServerAutoConfiguration` | `pyfly.server.auto_configuration` | `ApplicationServerPort` | `@conditional_on_class("granian")`, `@conditional_on_missing_bean(ApplicationServerPort)` |
| `UvicornServerAutoConfiguration` | `pyfly.server.auto_configuration` | `ApplicationServerPort` | `@conditional_on_class("uvicorn")`, `@conditional_on_missing_bean(ApplicationServerPort)` |
| `HypercornServerAutoConfiguration` | `pyfly.server.auto_configuration` | `ApplicationServerPort` | `@conditional_on_class("hypercorn")`, `@conditional_on_missing_bean(ApplicationServerPort)` |
| `EventLoopAutoConfiguration` | `pyfly.server.auto_configuration` | `EventLoopPort` | `@conditional_on_class("uvloop")` |
| `CacheAutoConfiguration` | `pyfly.cache.auto_configuration` | `CacheAdapter` | `@conditional_on_property("pyfly.cache.enabled")`, `@conditional_on_missing_bean(CacheAdapter)` |
| `MessagingAutoConfiguration` | `pyfly.messaging.auto_configuration` | `MessageBrokerPort` | `@conditional_on_property("pyfly.messaging.provider")`, `@conditional_on_missing_bean(MessageBrokerPort)` |
| `ClientAutoConfiguration` | `pyfly.client.auto_configuration` | `HttpClientPort` | `@conditional_on_class("httpx")`, `@conditional_on_missing_bean(HttpClientPort)` |
| `DocumentAutoConfiguration` | `pyfly.data.document.auto_configuration` | PyMongo `AsyncMongoClient`, `MongoTransactionManager`, `BeanieInitializer`, `MongoHealthIndicator`, `MongoRepositoryBeanPostProcessor` | `@conditional_on_class("beanie")`; its beans need `pyfly.data.document.enabled` |
| `RelationalAutoConfiguration` | `pyfly.data.relational.auto_configuration` | `RepositoryBeanPostProcessor` | `@conditional_on_class("sqlalchemy")`, `@conditional_on_property("pyfly.data.relational.enabled")` |
| `ShellAutoConfiguration` | `pyfly.shell.auto_configuration` | `ShellRunnerPort` | `@conditional_on_class("click")` |
| `CqrsAutoConfiguration` | `pyfly.cqrs.config.auto_configuration` | CQRS handlers | (unconditional) |
| `AdminAutoConfiguration` | `pyfly.admin.auto_configuration` | Admin dashboard | `@conditional_on_property("pyfly.admin.enabled")` |
| `TransactionalEngineAutoConfiguration` | `pyfly.transactional.auto_configuration` | Saga/TCC engines | `@conditional_on_property("pyfly.transactional.enabled")` |
| `JwtAutoConfiguration` | `pyfly.security.auto_configuration` | `JWTService` | `@conditional_on_property("pyfly.security.enabled")`, `@conditional_on_class("jwt")` |
| `PasswordEncoderAutoConfiguration` | `pyfly.security.auto_configuration` | `BcryptPasswordEncoder` | `@conditional_on_property("pyfly.security.enabled")`, `@conditional_on_class("bcrypt")` |
| `SchedulingAutoConfiguration` | `pyfly.scheduling.auto_configuration` | `TaskScheduler` | `@conditional_on_class("croniter")` |
| `MetricsAutoConfiguration` | `pyfly.observability.auto_configuration` | `MetricsRegistry` | `@conditional_on_class("prometheus_client")` |
| `TracingAutoConfiguration` | `pyfly.observability.auto_configuration` | `TracerProvider` | `@conditional_on_class("opentelemetry")` |
| `ActuatorAutoConfiguration` | `pyfly.actuator.auto_configuration` | `ActuatorRegistry`, `HealthAggregator` | `@conditional_on_property("pyfly.web.actuator.enabled")` |
| `MetricsActuatorAutoConfiguration` | `pyfly.actuator.auto_configuration` | `MetricsEndpoint`, `PrometheusEndpoint` | `@conditional_on_property("pyfly.web.actuator.enabled")`, `@conditional_on_class("prometheus_client")` |
| `AopAutoConfiguration` | `pyfly.aop.auto_configuration` | `AspectBeanPostProcessor` | (unconditional — always active) |

### Decentralized Auto-Configuration via Entry Points

The `AutoConfigurationEngine` class has been removed. Auto-configuration is now fully
decentralized: each subsystem owns its own `@auto_configuration` class, discovered at
startup via `importlib.metadata.entry_points(group="pyfly.auto_configuration")`. This
mirrors Spring Boot's `META-INF/spring.factories` / `AutoConfiguration.imports`
mechanism and is fully pluggable -- third-party packages can add their own
auto-configuration classes by declaring entries in the same group.

The built-in auto-configuration classes are registered in `pyproject.toml`:

```toml
[project.entry-points."pyfly.auto_configuration"]
web              = "pyfly.web.auto_configuration:WebAutoConfiguration"
web_fastapi      = "pyfly.web.auto_configuration:FastAPIAutoConfiguration"
server_granian   = "pyfly.server.auto_configuration:GranianServerAutoConfiguration"
server_uvicorn   = "pyfly.server.auto_configuration:UvicornServerAutoConfiguration"
server_hypercorn = "pyfly.server.auto_configuration:HypercornServerAutoConfiguration"
cache            = "pyfly.cache.auto_configuration:CacheAutoConfiguration"
messaging        = "pyfly.messaging.auto_configuration:MessagingAutoConfiguration"
client           = "pyfly.client.auto_configuration:ClientAutoConfiguration"
document         = "pyfly.data.document.auto_configuration:DocumentAutoConfiguration"
relational       = "pyfly.data.relational.auto_configuration:RelationalAutoConfiguration"
shell            = "pyfly.shell.auto_configuration:ShellAutoConfiguration"
cqrs             = "pyfly.cqrs.config.auto_configuration:CqrsAutoConfiguration"
admin            = "pyfly.admin.auto_configuration:AdminAutoConfiguration"
transactional    = "pyfly.transactional.auto_configuration:TransactionalEngineAutoConfiguration"
server           = "pyfly.server.auto_configuration:ServerAutoConfiguration"
event-loop       = "pyfly.server.auto_configuration:EventLoopAutoConfiguration"
security-jwt     = "pyfly.security.auto_configuration:JwtAutoConfiguration"
security-password = "pyfly.security.auto_configuration:PasswordEncoderAutoConfiguration"
scheduling       = "pyfly.scheduling.auto_configuration:SchedulingAutoConfiguration"
metrics          = "pyfly.observability.auto_configuration:MetricsAutoConfiguration"
tracing          = "pyfly.observability.auto_configuration:TracingAutoConfiguration"
actuator         = "pyfly.actuator.auto_configuration:ActuatorAutoConfiguration"
actuator-metrics = "pyfly.actuator.auto_configuration:MetricsActuatorAutoConfiguration"
aop              = "pyfly.aop.auto_configuration:AopAutoConfiguration"
```

Each `@auto_configuration` class uses `@conditional_on_class`, `@conditional_on_property`,
and `@conditional_on_missing_bean` decorators to guard its `@bean` methods. The typical
pattern for a subsystem auto-configuration class is:

1. `@conditional_on_class` -- skip the entire class if the adapter library is not installed.
2. `@conditional_on_property` -- skip if the subsystem is not enabled in config (e.g., `pyfly.cache.enabled`).
3. `@conditional_on_missing_bean` -- skip if the user has already provided a bean for the port type.
4. `@bean` methods read config to determine the provider and delegate to the class's own `detect_provider()` static method when the provider is `"auto"`.
5. The `@bean` method instantiates and returns the appropriate adapter.

`discover_auto_configurations()` (from `pyfly.config.auto`) replaces the hardcoded
engine. It is called by `ApplicationContext._register_auto_configurations()` at the
start of the context lifecycle. Because each auto-configuration class is a regular
`@configuration` bean with conditions, the existing condition evaluation and
`_process_configurations()` machinery handles it generically -- no special-case code
is needed in the context.

### Provider Detection

The `AutoConfiguration` class (from `pyfly.config.auto`) provides `is_available(module)`, which
checks library availability via `importlib.import_module()`. A subsystem whose provider can be
`"auto"` detects it in a `detect_provider()` static method of its own `@auto_configuration` class
(`CacheAutoConfiguration`, `MessagingAutoConfiguration`, `EdaAutoConfiguration`). The data layers
are switched on by configuration instead: the relational layer by `pyfly.data.relational.enabled`
and a URL (only the `dev` profile falls back to `./app.db`), the document layer by `beanie` plus
`pyfly.data.document.enabled`.

---

## Framework-Agnostic Design

Each infrastructure module separates framework-agnostic types from adapter-specific
implementations. This separation ensures business code never imports technology-specific
libraries directly.

### Web Module

```
pyfly.web/
    mappings.py          # @get_mapping, @post_mapping, @put_mapping,
    |                    # @patch_mapping, @delete_mapping, @request_mapping
    params.py            # Body, PathVar, QueryParam, Header, Cookie, Valid
    filters.py           # OncePerRequestFilter base class
    cors.py              # CORSConfig
    security_headers.py  # SecurityHeadersConfig
    exception_handler.py # @exception_handler
    ports/
        outbound.py      # WebServerPort protocol
        filter.py        # WebFilter protocol, CallNext type alias
    adapters/
        starlette/       # Starlette/ASGI implementation
            adapter.py           # StarletteWebAdapter (WebServerPort impl)
            app.py               # create_app() factory
            controller.py        # ControllerRegistrar
            resolver.py          # ParameterResolver (with Valid[T] support)
            filter_chain.py      # WebFilterChainMiddleware
            security_middleware.py  # SecurityMiddleware (canonical location)
            filters/             # Built-in filter implementations
                transaction_id_filter.py
                request_logging_filter.py
                security_headers_filter.py
                security_filter.py
```

Framework-agnostic decorators (`@get_mapping`, `@post_mapping`), parameter types
(`Body`, `QueryParam`, `Valid`), and the `WebFilter` protocol work with any web
framework. The Starlette adapter is the default; config-driven selection
(`pyfly.web.adapter: auto|starlette|fastapi`) allows multiple adapters. When FastAPI is
installed, the FastAPI adapter is auto-selected over Starlette.

### Server Module

```
pyfly.server/
    __init__.py              # Public API exports
    properties.py            # ServerProperties dataclass
    ports/
        outbound.py          # ApplicationServerPort, EventLoopPort protocols
    adapters/
        granian.py           # GranianServerAdapter (highest priority)
        uvicorn.py           # UvicornServerAdapter (ecosystem standard)
        hypercorn.py         # HypercornServerAdapter (HTTP/2 + HTTP/3)
        uvloop.py            # UvloopEventLoopAdapter (Linux/macOS)
        winloop.py           # WinloopEventLoopAdapter (Windows)
        asyncio.py           # AsyncioEventLoopAdapter (fallback)
    auto_configuration.py    # Cascading auto-configuration classes
```

The server module adds two port interfaces to the hexagonal architecture:

- **`ApplicationServerPort`** — Defines the contract for running an ASGI application on a
  network socket. Implementations: `GranianServerAdapter`, `UvicornServerAdapter`,
  `HypercornServerAdapter`. This sits between the web adapter (which creates the ASGI app)
  and the network, analogous to Spring Boot's `WebServer` interface.

- **`EventLoopPort`** — Defines the contract for configuring the asyncio event loop policy.
  Implementations: `UvloopEventLoopAdapter`, `WinloopEventLoopAdapter`,
  `AsyncioEventLoopAdapter`. This is analogous to Netty's `EventLoopGroup` in Spring Boot.

The relationship between the three layers:

```
EventLoopPort          ApplicationServerPort          WebServerPort
  (uvloop)      →        (Granian)              →      (FastAPI/Starlette)
  configures             serves                        creates
  event loop             ASGI app                      ASGI app
```

Server selection uses cascading `@conditional_on_class` auto-configuration:
Granian > Uvicorn > Hypercorn. Event loop selection: uvloop > winloop > asyncio.
Both can be overridden via `pyfly.server.type` and `pyfly.server.event-loop` config
or by registering a user-provided bean.

### Data Module

```
pyfly/data/                     # Data Commons
├── page.py                     # Page[T]
├── pageable.py                 # Pageable, Sort, Order
├── mapper.py                   # Mapper
├── query_parser.py             # QueryMethodParser
├── exception_translation.py    # driver errors -> kernel exceptions (409s)
├── auditing.py                 # AuditorAware, DateTimeProvider, run_as
├── ports/                      # Shared ports
│   ├── outbound.py             # RepositoryPort, SessionPort (deprecated alias)
│   └── compiler.py             # QueryMethodCompilerPort
├── transaction/                # Backend-neutral unit of work
│   ├── decorator.py            # @transactional
│   ├── template.py             # TransactionTemplate
│   ├── manager.py              # TransactionManager SPI
│   ├── unit_of_work.py         # UnitOfWork
│   ├── context.py              # the task's transaction state, detached()
│   ├── synchronization.py      # after_commit, TransactionSynchronization
│   └── observation.py          # track_commits, untracked
├── relational/                 # Relational namespace
│   ├── datasource_registry.py  # DataSourceRegistry, DataSource
│   ├── framework_schema.py     # the pyfly_* framework tables
│   ├── schema.py               # ddl-auto (SchemaInitializer)
│   ├── migrations.py           # startup migrations (MigrationRunner)
│   ├── upsert.py               # dialect-dispatched upserts
│   ├── routing.py              # RoutingSessionFactory, read_only
│   └── sqlalchemy/             # SQLAlchemy adapter
│       ├── entity.py           # Base, BaseEntity, VersionedMixin
│       ├── types.py            # UtcDateTime
│       ├── repository.py       # Repository[T, ID]
│       ├── transaction_manager.py  # SqlAlchemyTransactionManager
│       ├── specification.py    # Specification
│       ├── filter.py           # FilterOperator, FilterUtils
│       ├── query.py            # @query, QueryExecutor
│       ├── query_compiler.py   # QueryMethodCompiler
│       ├── post_processor.py   # RepositoryBeanPostProcessor
│       └── transactional.py    # reactive_transactional
└── document/                   # Document namespace
    └── mongodb/                # MongoDB adapter
        ├── document.py         # BaseDocument, AggregateDocument
        ├── repository.py       # MongoRepository[T, ID]
        ├── transaction_manager.py  # MongoTransactionManager
        ├── query_compiler.py   # MongoQueryMethodCompiler
        ├── post_processor.py   # MongoRepositoryBeanPostProcessor
        ├── transactional.py    # mongo_transactional (deprecated alias)
        └── initializer.py      # BeanieInitializer lifecycle bean
```

### Messaging Module

```
pyfly.messaging/
    types.py           # Message dataclass
    decorators.py      # @message_listener
    ports/
        outbound.py    # MessageBrokerPort, MessageHandler
    adapters/
        memory.py      # InMemoryMessageBroker (testing / fallback)
        kafka.py       # KafkaAdapter (aiokafka)
        rabbitmq.py    # RabbitMQAdapter (aio_pika)
```

### Cache Module

```
pyfly.cache/
    decorators.py     # @cacheable, @cache_evict, @cache_put, @cache
    manager.py        # CacheManager
    ports/
        outbound.py   # CacheAdapter
    adapters/
        memory.py     # InMemoryCache
        redis.py      # RedisCacheAdapter (redis.asyncio)
```

### Scheduling Module

```
pyfly.scheduling/
    decorators.py       # @scheduled, @async_method
    cron.py             # CronExpression parser
    task_scheduler.py   # TaskScheduler
    ports/
        outbound.py     # TaskExecutorPort
    adapters/
        asyncio_executor.py   # AsyncIOTaskExecutor
        thread_executor.py    # ThreadPoolTaskExecutor
```

---

## Configuration Architecture

Configuration follows a layered loading strategy with four levels:

```
+---------------------------------------------------+
| 4. Environment Variables  (read-time priority)    |
+---------------------------------------------------+
| 3. Profile Overlays       (pyfly-{profile}.yaml)  |
+---------------------------------------------------+
| 2. User Config File       (pyfly.yaml / .toml)    |
+---------------------------------------------------+
| 1. Framework Defaults     (pyfly-defaults.yaml)   |
+---------------------------------------------------+
```

Key architectural decisions:

- **Deep merge** -- nested dictionaries are merged recursively (`Config._deep_merge()`),
  not replaced wholesale. This lets profile overlays specify only the keys that change.
- **Read-time env var resolution** -- environment variables are not baked into the config
  dict at load time. They are checked on every `Config.get()` call, ensuring they always
  win. The mapping strips the `pyfly.` prefix, replaces dots and hyphens with underscores,
  uppercases, and prefixes with `PYFLY_`.
- **Typed binding** -- `@config_properties` dataclasses provide compile-time type
  safety and IDE support. The `Config.bind()` method handles type coercion for `int`,
  `float`, and `bool` fields.
- **Early profile resolution** -- profiles must be resolved before `Config.from_file()`
  runs, because the method needs to know which overlay files to merge. This is handled
  by `PyFlyApplication._resolve_profiles_early()`, which reads the env var first, then
  parses the raw YAML file to extract the `pyfly.profiles.active` value.
- **Dual format support** -- YAML (via `PyYAML`) and TOML (via `tomllib`) are both
  supported. The format is determined by the file extension.

---

## Event-Driven Design

PyFly supports two separate event systems for different concerns.

### Application Events

Application events are lifecycle notifications published by the `ApplicationContext`.
They use `ApplicationEventBus` (in-process, ordered dispatch with async listeners).

| Event | When Published |
|---|---|
| `ContextRefreshedEvent` | After all beans are initialized and post-processed. |
| `ApplicationReadyEvent` | Immediately after `ContextRefreshedEvent`. |
| `ContextClosedEvent` | After all `@pre_destroy` methods during shutdown. |

All events inherit from `ApplicationEvent`. Listeners are invoked in `@order` order of
their owning class.

Application events are for framework-level lifecycle coordination (e.g., "start accepting
HTTP requests after all beans are ready").

### Domain Events (EDA)

The `pyfly.eda` module provides a separate event system for domain-level events:

- `EventPublisher` port -- abstract interface for publishing domain events.
- `EventEnvelope` -- wraps events with metadata (timestamp, correlation ID, etc.).
- `@event_listener` -- decorator for domain event handlers.
- `@event_publisher` -- decorator for classes that publish events.
- `@publish_result` -- decorator to automatically publish a method's return value.
- `InMemoryEventBus` -- default in-process adapter.
- `ErrorStrategy` -- configurable error handling for event processing.

Domain events flow through the `EventPublisher` port and can be routed to external
systems (Kafka, RabbitMQ) via adapter binding.

The two systems are intentionally separate: application events are internal lifecycle
signals; domain events are part of your business logic.

---

## Cross-Cutting Concerns

### Aspect-Oriented Programming

The `pyfly.aop` module provides Spring-style AOP:

- `@aspect` -- marks a class as an aspect container.
- `@before`, `@after`, `@around`, `@after_returning`, `@after_throwing` -- advice
  decorators with pointcut expressions.
- `AspectBeanPostProcessor` -- a `BeanPostProcessor` that weaves aspects into target
  beans during `after_init()`.
- `JoinPoint` -- provides context about the intercepted method call.
- `AspectRegistry` / `AdviceBinding` -- manages the mapping of pointcuts to advice.

AOP is implemented through proxy wrapping in `BeanPostProcessor.after_init()`, which
means it works seamlessly with the DI lifecycle. The `weave_bean()` function performs
the actual weaving.

### Observability

The `pyfly.observability` module provides:

- **Metrics**: `@timed` and `@counted` decorators for automatic metric collection, `MetricsRegistry` for programmatic metric access.
- **Tracing**: `@span` decorator for distributed tracing.
- **Health checks**: `HealthChecker` with `HealthResult` and `HealthStatus`.
- **Logging**: `configure_logging()` and `get_logger()` for structured logging.

The `pyfly.actuator` module exposes health and info endpoints for production monitoring
via `make_actuator_routes()`.

### Resilience

The `pyfly.resilience` module provides resilience patterns as decorators:

- `@rate_limiter` / `RateLimiter` -- limits call frequency.
- `@bulkhead` / `Bulkhead` -- limits concurrent executions.
- `@time_limiter` -- enforces execution timeouts.
- `@fallback` -- provides fallback values on failure.

The `pyfly.client` module adds `CircuitBreaker` (with `CircuitState`) and `RetryPolicy`
for HTTP client resilience.

### Security

The `pyfly.security` module provides:

- `SecurityContext` -- holds the current user/principal.
- `@secure` -- method-level authorization decorator.
- `JWTService` -- JWT token creation and validation.
- `SecurityMiddleware` -- HTTP request authentication middleware (canonical location:
  `pyfly.web.adapters.starlette.security_middleware`, re-exported from `pyfly.security`
  for backward compatibility).
- `PasswordEncoder` / `BcryptPasswordEncoder` -- password hashing.

### Validation

The `pyfly.validation` module integrates with Pydantic:

- `@validate_input` -- validates method arguments against Pydantic models.
- `@validator` -- custom validation decorators.
- `validate_model()` -- programmatic model validation.
- `Valid[T]` -- web parameter annotation that triggers explicit validation with
  structured 422 error responses. Works standalone (`Valid[CreateDTO]` = body
  validation) or wrapping any binding type (`Valid[QueryParam[int]]`).

---

## Design Decisions and Trade-Offs

### Injection Strategy

PyFly supports two injection styles, mirroring Spring Boot:

1. **Constructor injection** (preferred) — dependencies declared as `__init__` type hints.
   Dependencies are explicit, immutable, and visible in the class signature.
2. **Field injection** via `Autowired()` — dependencies declared as class attributes.
   Useful when constructor parameter lists grow large or for optional collaborators.

Additional injection features:
- **`Optional[T]`** — resolves to `None` when the dependency is not registered.
- **`list[T]`** — collects all implementations bound to an interface.
- **Circular dependency detection** — the container tracks types currently being
  resolved and raises `CircularDependencyError` with a clear chain message instead
  of infinite recursion.

**Recommendation:** prefer constructor injection for mandatory dependencies.
Use `Autowired()` for optional or supplemental dependencies where it improves
readability. Deeply nested dependency graphs with many constructor parameters are
a signal to refactor into smaller components.

### Why Async-First?

All lifecycle methods (`startup()`, `shutdown()`, `@post_construct`, `@pre_destroy`),
event handlers, and I/O operations are async.

**Rationale:**
- Modern Python services are I/O-bound (HTTP, databases, message brokers). `async/await`
  provides efficient concurrency without threads.
- ASGI web servers (Uvicorn, Hypercorn) require async handlers.
- Database drivers (asyncpg, aiosqlite) and message broker clients (aiokafka, aio_pika)
  are async-native.

**Trade-off:** CPU-bound work must be offloaded to thread pools or process pools. The
`@async_method` decorator and `ThreadPoolTaskExecutor` provide escape hatches.

### Why Protocols over ABCs?

Ports use `typing.Protocol` (structural typing) rather than `abc.ABC` (nominal typing).

**Rationale:**
- Protocol classes define contracts without requiring inheritance. Any class that
  implements the right methods satisfies the protocol, even without explicitly subclassing.
- This supports duck typing, which is idiomatic Python.
- It reduces coupling: adapter classes do not need to know about or import the port
  definitions (though they usually do for clarity).
- The `BeanPostProcessor` protocol uses `@runtime_checkable` so it can be checked with
  `isinstance()` when needed.

**Trade-off:** Protocol violations are caught at type-check time (mypy/pyright) rather
than at class definition time. This means errors may surface later if type checking is
not enforced.

### Why Two DI Layers (Container + ApplicationContext)?

**Rationale:**
- The `Container` is a focused, simple DI engine that can be used standalone (especially
  in tests via `create_test_container()`).
- The `ApplicationContext` adds lifecycle concerns (events, post-processors, conditions,
  auto-configuration) that would complicate the Container's API.
- This separation follows the Single Responsibility Principle and makes each layer
  independently testable.

**Trade-off:** users must understand which layer to use. The guideline is simple:
use `ApplicationContext` in application code, use `Container` directly only in tests
or framework extensions.

### Why Decentralized Auto-Configuration?

**Rationale:**
- Each subsystem owns its own wiring via an `@auto_configuration` class with
  `@conditional_on_*` guards. This is more cohesive than a central engine that
  hardcodes knowledge of every subsystem.
- Discovery via `importlib.metadata.entry_points(group="pyfly.auto_configuration")`
  makes the system fully pluggable -- third-party packages can register their own
  auto-configuration classes without modifying framework code.
- The pattern mirrors Spring Boot's `META-INF/spring.factories` mechanism, which
  has proven effective at scale.
- Provider detection lives inside each subsystem's `@auto_configuration` class
  (via a `detect_provider()` static method). The core `AutoConfiguration` class
  only provides the generic `is_available()` helper — it has zero knowledge of
  specific providers.

**Trade-off:** the decentralized approach requires each subsystem to duplicate a
small amount of boilerplate (`@auto_configuration` + `@conditional_on_*` +
`@bean`). In practice, this boilerplate is minimal and improves cohesion -- each
module is self-contained and independently understandable.

### Why Deep Merge Instead of Override?

**Rationale:**
- Deep merge lets profile overlays specify only changed keys. Without it, you would
  need to duplicate the entire config tree in every profile file.
- It matches the mental model of "this profile changes these specific settings."

**Trade-off:** you cannot "unset" a key by omitting it in an overlay. To effectively
remove a value, you must explicitly set it to `null`/`""` in the overlay file.

### Key Design Patterns Used

| Pattern | Module | Description |
|---|---|---|
| **Repository** | `pyfly.data` | Abstract data access behind ports. |
| **Specification** | `pyfly.data` | Composable query predicates. |
| **Circuit Breaker** | `pyfly.client` | Prevent cascading failures. |
| **Command/Query Bus** | `pyfly.cqrs` | Route commands/queries to handlers through a full pipeline. |
| **Chain of Responsibility** | `pyfly.web` | Exception converter chain. |
| **Observer** | `pyfly.context`, `pyfly.eda` | Application and domain events. |
| **Decorator** | `pyfly.aop` | Aspect-oriented cross-cutting concerns via proxies. |
| **Factory** | `pyfly.container` | Bean factories via `@configuration` / `@bean`. |
| **Decorator** | `pyfly.client` | `@service_client` with declarative resilience (circuit breaker, retry). |
| **Strategy** | `pyfly.resilience` | Pluggable resilience policies. |
| **Template Method** | `pyfly.context` | `ApplicationContext.start()` orchestration. |

---

## Architectural Rules (Enforced)

These rules ensure the framework stays modular, hexagonal, and Spring Boot-like. Every contributor must follow them.

### Rule 1: No Subsystem Knowledge in Core

`ApplicationContext`, `Container`, and `core/application.py` must **never** import or reference specific subsystem adapters (Starlette, Redis, Kafka, PyMongo, httpx, SQLAlchemy, etc.). All subsystem wiring lives in per-subsystem `@auto_configuration` classes.

**Violation example** (banned):
```python
# In application_context.py — NEVER DO THIS
from pyfly.cache.adapters.redis import RedisCacheAdapter
if provider == "redis":
    self._container.register(RedisCacheAdapter, ...)
```

**Correct approach**: Create a lifecycle bean (e.g. `BeanieInitializer`, `RedisCacheAdapter`) via the subsystem's `@auto_configuration` class. `_start_lifecycle_beans()` discovers and starts them generically via MRO inspection.

### Rule 2: Auto-Configuration via Entry Points

Every new subsystem must:

1. Create `auto_configuration.py` in its own package
2. Use `@auto_configuration` + `@conditional_on_*` + `@bean`
3. Register via `pyfly.auto_configuration` entry point in `pyproject.toml`

```toml
[project.entry-points."pyfly.auto_configuration"]
my_subsystem = "pyfly.my_subsystem.auto_configuration:MyAutoConfiguration"
```

No hardcoded imports in `discover_auto_configurations()` — it reads entry points dynamically.

### Rule 3: Port-Adapter Boundary

- **Port interfaces** (`ports/outbound.py`) must be `Protocol` classes with **zero** adapter imports
- **Adapter implementations** import their third-party library and implement the port
- Core code depends on ports, never on adapters
- `@conditional_on_missing_bean(PortType)` ensures user beans override auto-configured ones

### Rule 4: Lifecycle via Duck Typing

Infrastructure lifecycle is managed generically. Any bean with `start()` and `stop()` methods (defined on its class, not via `__getattr__`) is automatically started by `_start_lifecycle_beans()` and stopped by `ApplicationContext.stop()`, in phase order. No hardcoded adapter lists.

### Rule 5: Web-Framework Agnostic Security

Security middleware must live in the web adapter module (e.g., `web/adapters/starlette/security_middleware.py`). Core security (`pyfly.security`) must not import web-framework-specific types. Re-exports for convenience are acceptable.

### Rule 6: Condition Evaluator Correctness

- `@conditional_on_property` comparisons must be **case-insensitive** (`str(value).lower()`)
- Two-pass evaluation: pass 1 (property/class), pass 2 (bean-dependent)
- `@auto_configuration` classes are evaluated and processed **after** user `@configuration` classes

### Adding a New Subsystem — Checklist

1. [ ] Define port interface in `my_subsystem/ports/outbound.py` (Protocol, no adapter imports)
2. [ ] Implement adapter(s) in `my_subsystem/adapters/`
3. [ ] Create `my_subsystem/auto_configuration.py` with `@auto_configuration`, conditions, and `@bean` methods
4. [ ] Add entry point in `pyproject.toml` under `[project.entry-points."pyfly.auto_configuration"]`
5. [ ] Add optional dependency group in `pyproject.toml` under `[project.optional-dependencies]`
6. [ ] Run `uv sync` to register the entry point
7. [ ] Write tests that verify: bean production, condition gating, user-bean precedence
8. [ ] Verify: `grep -r "my_subsystem" src/pyfly/context/ src/pyfly/core/` returns **zero** results
