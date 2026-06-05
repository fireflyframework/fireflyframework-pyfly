# Dependency Injection Guide

This guide covers PyFly's dependency injection (DI) system in depth -- from the low-level
`Container` to the high-level `ApplicationContext`, stereotypes, scopes, lifecycle hooks,
conditional beans, and application events. By the end you will understand how to structure
a multi-layer application where every component is managed, wired, and lifecycle-aware.

---

## Table of Contents

1. [Introduction](#introduction)
2. [Container](#container)
   - [register()](#register)
   - [bind()](#bind)
   - [resolve()](#resolve)
   - [resolve_by_name()](#resolve_by_name)
   - [resolve_all()](#resolve_all)
   - [contains()](#contains)
3. [Stereotypes](#stereotypes)
   - [@component](#component)
   - [@service](#service)
   - [@repository](#repository)
   - [@rest_controller](#rest_controller)
   - [@controller](#controller)
   - [@configuration](#configuration)
   - [Stereotype Parameters](#stereotype-parameters)
4. [Scope](#scope)
   - [SINGLETON](#singleton)
   - [TRANSIENT](#transient)
   - [REQUEST](#request)
5. [@bean and @configuration](#bean-and-configuration)
6. [@primary](#primary)
7. [@order](#order)
8. [Qualifier](#qualifier)
9. [Autowired (Field Injection)](#autowired-field-injection)
10. [Optional and Collection Injection](#optional-and-collection-injection)
11. [Circular Dependency Detection](#circular-dependency-detection)
12. [Interface Binding](#interface-binding)
13. [Component Scanning](#component-scanning)
14. [ApplicationContext](#applicationcontext)
    - [get_bean()](#get_bean)
    - [get_bean_by_name()](#get_bean_by_name)
    - [get_beans_of_type()](#get_beans_of_type)
    - [register_bean() and register_post_processor()](#register_bean-and-register_post_processor)
    - [Properties](#applicationcontext-properties)
15. [Lifecycle Hooks](#lifecycle-hooks)
    - [@post_construct](#post_construct)
    - [@pre_destroy](#pre_destroy)
16. [BeanPostProcessor](#beanpostprocessor)
17. [Conditional Beans](#conditional-beans)
    - [@conditional_on_property](#conditional_on_property)
    - [@conditional_on_class](#conditional_on_class)
    - [@conditional_on_bean](#conditional_on_bean)
    - [@conditional_on_missing_bean](#conditional_on_missing_bean)
    - [@auto_configuration](#auto_configuration)
    - [Two-Pass Evaluation](#two-pass-evaluation)
18. [Application Events](#application-events)
    - [Built-in Events](#built-in-events)
    - [@app_event_listener](#app_event_listener)
    - [ApplicationEventBus](#applicationeventbus)
19. [Environment](#environment)
20. [Complete Example](#complete-example)

---

## Introduction

Dependency injection (DI) is a design pattern where objects receive their collaborators
instead of creating them. This decouples components, makes code testable, and lets the
framework manage object lifecycles.

PyFly supports two injection styles:

**Constructor injection** (recommended) — declare dependencies as `__init__` parameters:

```python
from pyfly.container import service

@service
class OrderService:
    def __init__(self, repo: OrderRepository, notifier: NotificationService):
        self.repo = repo
        self.notifier = notifier
```

**Field injection** — use `Autowired()` as a class attribute:

```python
from pyfly.container import Autowired, service

@service
class OrderService:
    repo: OrderRepository = Autowired()
    notifier: NotificationService = Autowired()
    metrics: MetricsCollector = Autowired(required=False)  # optional dependency
```

You never construct beans yourself. The container sees the type hints, resolves
dependencies, and injects them — first via the constructor, then into `Autowired` fields.

### Key Concepts

| Concept | Description |
|---|---|
| **Container** | Low-level DI container that stores registrations and resolves instances. |
| **ApplicationContext** | High-level orchestrator that wraps the Container and adds lifecycle, events, conditions, and bean factory support. |
| **Stereotype** | A decorator (`@service`, `@component`, etc.) that marks a class as container-managed. |
| **Scope** | How long an instance lives: `SINGLETON`, `TRANSIENT`, or `REQUEST`. |
| **Bean** | Any object managed by the container. |
| **Autowired** | A field descriptor that marks a class attribute for injection after construction. |

---

## Container

`Container` is the low-level DI engine. It stores registrations, resolves dependencies by
type hints, and supports interface binding, named beans, and `@primary` disambiguation.

```python
from pyfly.container import Container

container = Container()
```

The `Container` maintains three internal stores:

- `_registrations: dict[type, Registration]` -- maps each class to its `Registration`
  metadata (scope, condition, name, cached instance, optional factory).
- `_named: dict[str, Registration]` -- maps bean names to registrations for
  name-based resolution.
- `_bindings: dict[type, list[type]]` -- maps interfaces to their bound implementation
  types.

The `Registration` dataclass has these fields:

| Field | Type | Description |
|---|---|---|
| `impl_type` | `type` | The concrete class being registered. |
| `scope` | `Scope` | Lifecycle scope (default `SINGLETON`). |
| `condition` | `Callable \| None` | Optional condition callable. |
| `instance` | `Any` | Cached singleton instance (set after first resolution). |
| `name` | `str` | Bean name for named resolution. |
| `factory` | `Callable \| None` | Optional factory closure (set by `@bean` methods). When present, the factory is called instead of `impl_type.__init__` on every resolution — preserving `TRANSIENT` `@bean` semantics. |

### register()

```python
def register(
    self,
    cls: type,
    scope: Scope = Scope.SINGLETON,
    condition: Any = None,
    name: str = "",
) -> None:
```

Registers a class for injection. The scope and name can also come from stereotype
decorator attributes on the class itself (`__pyfly_scope__`, `__pyfly_bean_name__`),
which take precedence if set.

```python
from pyfly.container import Container, Scope

container = Container()
container.register(MyService, scope=Scope.SINGLETON, name="my_svc")
```

### bind()

```python
def bind(self, interface: type, implementation: type) -> None:
```

Binds an interface (or abstract base class / protocol) to a concrete implementation.
Multiple implementations can be bound to the same interface. When resolving, if there
is exactly one binding, it is used directly. If there are multiple, the one decorated
with `@primary` is selected.

```python
container.register(PostgresRepository)
container.bind(RepositoryPort, PostgresRepository)
```

### resolve()

```python
def resolve(self, cls: type[T]) -> T:
```

Resolves an instance of the given type. The resolution order is:

1. **Direct registration** -- if `cls` is registered, resolve it.
2. **Interface binding** -- if `cls` has exactly one bound implementation, resolve it.
3. **Multiple bindings** -- pick the implementation marked `@primary`.
4. **Error** -- `NoSuchBeanError` if nothing matches; `NoUniqueBeanError` if multiple candidates exist without a `@primary`.

Constructor parameters are resolved recursively via type hints. If a parameter uses
`Annotated[T, Qualifier("name")]`, the container resolves by name instead of type.

**Parameter defaults:** When a constructor parameter has a default value and the container
cannot resolve the type, the default is used instead of raising an error. This enables
patterns like `Repository[T, ID]` where `model: type[T] | None = None` falls back to the
auto-extracted entity type from `__init_subclass__`.

**`type[T]` parameters:** The container cannot resolve bare `type` or `type[T]` parameters
(class references are not beans). If encountered without a default, a `KeyError` is raised
with a descriptive message. Use generic subclassing (e.g., `Repository[Entity, ID]`) to
auto-extract entity types instead.

For singleton-scoped beans, the instance is cached on the `Registration` object after
first creation and reused for subsequent calls.

```python
service = container.resolve(OrderService)
```

### resolve_by_name()

```python
def resolve_by_name(self, name: str) -> Any:
```

Resolves a bean by its registered name. Raises `KeyError` if not found.

```python
db = container.resolve_by_name("primary_db")
```

### resolve_all()

```python
def resolve_all(self, cls: type[T]) -> list[T]:
```

Resolves **all** implementations bound to an interface, returning a list.

```python
all_validators = container.resolve_all(Validator)
```

### contains()

```python
def contains(self, name: str) -> bool:
```

Returns `True` if a bean with the given name is registered.

```python
if container.contains("cache_adapter"):
    cache = container.resolve_by_name("cache_adapter")
```

---

## Stereotypes

Stereotypes are semantic decorators that mark a class as a container-managed bean. Each
stereotype carries a specific architectural meaning but they all work identically at the
container level -- the only difference is the `__pyfly_stereotype__` label.

All stereotypes are created by an internal `_make_stereotype()` factory, which means they
all share the same signature and behavior.

### @component

A generic managed bean with no specific architectural role.

```python
from pyfly.container import component

@component
class EmailFormatter:
    def format(self, template: str, **kwargs) -> str:
        return template.format(**kwargs)
```

### @service

Business logic layer. Use `@service` for classes that contain domain operations.

```python
from pyfly.container import service

@service
class PaymentService:
    def __init__(self, gateway: PaymentGateway, repo: PaymentRepository):
        self.gateway = gateway
        self.repo = repo

    async def process(self, order_id: str, amount: float) -> str:
        # business logic here
        ...
```

### @repository

Data access layer. Use `@repository` for classes that interact with databases or
external storage.

```python
from pyfly.container import repository

@repository
class UserRepository:
    def __init__(self, session: SessionPort):
        self.session = session

    async def find_by_email(self, email: str) -> User | None:
        ...
```

### @rest_controller

REST controller for handling HTTP requests and returning JSON responses. The web layer
auto-discovers classes with this stereotype.

```python
from pyfly.container import rest_controller

@rest_controller
class UserController:
    def __init__(self, user_service: UserService):
        self.user_service = user_service
```

### @controller

Web controller for template-based (non-JSON) responses.

```python
from pyfly.container import controller

@controller
class PageController:
    def __init__(self, template_engine: TemplateEngine):
        self.engine = template_engine
```

### @configuration

A configuration class that can contain `@bean` factory methods. See the
[@bean and @configuration](#bean-and-configuration) section below.

```python
from pyfly.container import configuration

@configuration
class AppConfig:
    ...
```

### Stereotype Parameters

All stereotypes accept the same optional keyword arguments:

```python
@service(
    name="payment_svc",
    scope=Scope.SINGLETON,
    profile="production",
    condition=lambda: os.getenv("PAYMENTS_ENABLED") == "true",
)
class PaymentService:
    ...
```

| Parameter | Type | Default | Description |
|---|---|---|---|
| `name` | `str` | `""` | Bean name for named resolution. |
| `scope` | `Scope` | `Scope.SINGLETON` | Lifecycle scope. |
| `profile` | `str` | `""` | Only activate when this profile is active. Supports negation (`"!test"`) and comma-separated values (`"dev,staging"`). |
| `condition` | `Callable[..., bool] \| None` | `None` | Callable that must return `True` for the bean to be registered. |

Stereotypes can also be used without parentheses for the common case:

```python
@service              # No args -- all defaults
class SimpleService:
    pass

@service(name="svc")  # With args
class NamedService:
    pass
```

When a stereotype is applied to a class, it sets these internal attributes:

| Attribute | Value |
|---|---|
| `__pyfly_injectable__` | `True` |
| `__pyfly_stereotype__` | `"component"`, `"service"`, `"repository"`, etc. |
| `__pyfly_scope__` | The `scope` argument |
| `__pyfly_condition__` | The `condition` argument |
| `__pyfly_bean_name__` | The `name` argument (only if non-empty) |
| `__pyfly_profile__` | The `profile` argument (only if non-empty) |

---

## Scope

The `Scope` enum controls how long a bean instance lives.

```python
from pyfly.container import Scope

class Scope(Enum):
    SINGLETON = auto()
    TRANSIENT = auto()
    REQUEST = auto()
```

### SINGLETON

The default scope. A single instance is created the first time the bean is resolved and
reused for all subsequent resolutions. Singletons are eagerly instantiated during
`ApplicationContext.start()`.

```python
@service  # Singleton by default
class CacheManager:
    ...

@service(scope=Scope.SINGLETON)  # Explicit
class CacheManager:
    ...
```

### TRANSIENT

A new instance is created every time the bean is resolved. Use this for stateful objects
that must not be shared.

```python
@component(scope=Scope.TRANSIENT)
class RequestContext:
    def __init__(self):
        self.data = {}
```

### REQUEST

Scoped to a single HTTP request. A new instance is created per request and discarded
afterward. This scope is intended for web-layer beans that carry request-specific state.

```python
@component(scope=Scope.REQUEST)
class CurrentUser:
    ...
```

---

## @bean and @configuration

`@bean` marks a method inside a `@configuration` class as a **bean factory**. The method's
return type annotation determines the interface the produced bean satisfies.

```python
from pyfly.container import configuration, bean, Scope

@configuration
class InfraConfig:

    @bean
    def payment_gateway(self) -> PaymentGateway:
        return StripeGateway(api_key="sk_test_...")

    @bean(name="secondary_db", scope=Scope.TRANSIENT)
    def secondary_database(self) -> DataSource:
        return PostgresDataSource(url="postgresql://...")
```

### How It Works

During `ApplicationContext.start()`, the context:

1. Finds all classes with `__pyfly_stereotype__ == "configuration"`.
2. Resolves the configuration class itself (so it can receive injected dependencies).
3. Iterates over methods marked with `__pyfly_bean__ = True`.
4. Reads the return type hint to determine the bean's type.
5. Calls the method (injecting any method parameters from the container).
6. Registers the returned object as a singleton (or the specified scope).

### @bean Parameters

| Parameter | Type | Default | Description |
|---|---|---|---|
| `name` | `str` | `""` | Bean name. Defaults to the method name if not specified. |
| `scope` | `Scope` | `Scope.SINGLETON` | Lifecycle scope of the produced bean. |

### Injecting Dependencies into @bean Methods

Bean factory methods can declare parameters with type hints. The container resolves them
automatically:

```python
@configuration
class MessagingConfig:

    @bean
    def event_publisher(self, broker: MessageBrokerPort) -> EventPublisher:
        return KafkaEventPublisher(broker)
```

---

## @primary

When multiple implementations are bound to the same interface, `@primary` marks the
default one to use.

```python
from pyfly.container import primary, service

class NotificationSender(Protocol):
    def send(self, msg: str) -> None: ...

@service
class EmailSender:
    def send(self, msg: str) -> None: ...

@primary
@service
class SmsSender:
    def send(self, msg: str) -> None: ...
```

When `container.resolve(NotificationSender)` is called and both `EmailSender` and
`SmsSender` are bound, `SmsSender` is returned because it is `@primary`.

Without `@primary`, the container raises a `NoUniqueBeanError` listing the ambiguous candidates:

```
NoUniqueBeanError: Multiple beans of type 'NotificationSender' found but none is marked @primary
  Candidates: ['EmailSender', 'SmsSender']
```

The `@primary` decorator simply sets `__pyfly_primary__ = True` on the class.

---

## @order

`@order` controls the initialization order of beans. Lower values are initialized first.

```python
from pyfly.container import order, HIGHEST_PRECEDENCE, LOWEST_PRECEDENCE

@order(HIGHEST_PRECEDENCE)
@service
class SecurityInitializer:
    """Must start before anything else."""
    ...

@order(100)
@service
class CacheWarmer:
    """Runs after normal services."""
    ...

@order(LOWEST_PRECEDENCE)
@service
class MetricsReporter:
    """Runs last."""
    ...
```

### Constants

| Constant | Value | Description |
|---|---|---|
| `HIGHEST_PRECEDENCE` | `-2147483648` (`-(2**31)`) | Highest priority (initialized first). |
| `LOWEST_PRECEDENCE` | `2147483647` (`2**31 - 1`) | Lowest priority (initialized last). |

Beans without `@order` default to `0`. The `get_order()` function reads the
`__pyfly_order__` attribute from a class, returning `0` if absent.

Ordering affects:
- The order in which singletons are eagerly resolved during startup.
- The order in which `BeanPostProcessor` instances are applied.
- The order of `get_beans_of_type()` results.
- The order in which event listeners are invoked.

---

## Qualifier

`Qualifier` enables named bean resolution through `typing.Annotated`. Use it when you
have multiple beans of the same type and need to select a specific one by name.

```python
from typing import Annotated
from pyfly.container import Qualifier, service, bean, configuration

@configuration
class DataSourceConfig:

    @bean(name="primary_db")
    def primary(self) -> DataSource:
        return PostgresDataSource(url="postgresql://primary/db")

    @bean(name="analytics_db")
    def analytics(self) -> DataSource:
        return PostgresDataSource(url="postgresql://analytics/db")

@service
class ReportService:
    def __init__(
        self,
        db: Annotated[DataSource, Qualifier("analytics_db")],
    ):
        self.db = db  # Receives the analytics DataSource
```

### How It Works

When the container encounters an `Annotated[T, Qualifier("name")]` type hint during
constructor resolution, it calls `resolve_by_name("name")` instead of `resolve(T)`.
This is handled in the `Container._resolve_param()` method, which inspects the
`Annotated` args for any `Qualifier` metadata.

### Qualifier Class

```python
class Qualifier:
    __slots__ = ("name",)

    def __init__(self, name: str) -> None:
        self.name = name
```

`Qualifier` is a lightweight metadata object. It carries only a `name` string and is
designed to be used exclusively within `Annotated[...]` type hints.

---

## Autowired (Field Injection)

`Autowired` enables field-level dependency injection. After the container creates an
instance via constructor injection, it scans class annotations for `Autowired()` sentinels
and injects the resolved dependencies.

```python
from pyfly.container import Autowired, service

@service
class OrderService:
    repo: OrderRepository = Autowired()
    cache: CacheAdapter = Autowired(qualifier="redis_cache")
    metrics: MetricsCollector = Autowired(required=False)
```

### Parameters

| Parameter | Type | Default | Description |
|---|---|---|---|
| `qualifier` | `str \| None` | `None` | If set, resolve by bean name instead of type. |
| `required` | `bool` | `True` | If `False`, set the field to `None` when the dependency cannot be resolved. |

### How It Works

1. The container creates the instance via constructor injection (as before).
2. It calls `typing.get_type_hints()` on the class to discover field annotations.
3. For each field whose class-level default is an `Autowired()` instance:
   - If `qualifier` is set, resolve by name via `resolve_by_name()`.
   - If the type hint uses `Annotated[T, Qualifier("name")]`, resolve via the qualifier.
   - Otherwise, resolve by type via `resolve()`.
   - If resolution fails and `required=False`, set the field to `None`.
4. The resolved value is injected via `setattr()`.

### Mixing Constructor and Field Injection

A class can use both constructor and field injection. Constructor injection runs first,
then field injection:

```python
@service
class OrderService:
    cache: CacheAdapter = Autowired(required=False)

    def __init__(self, repo: OrderRepository) -> None:
        self.repo = repo
```

### When to Use

- **Constructor injection** for mandatory dependencies — makes them explicit and testable.
- **Field injection** for optional or supplemental dependencies, or when the constructor
  parameter list grows unwieldy.

---

## Optional and Collection Injection

### Optional[T]

Declare a parameter as `Optional[T]` or `T | None` to make it optional. If no bean
of type `T` is registered, the container injects `None` instead of raising `KeyError`.

Both `typing.Optional` and PEP 604 union syntax are fully supported:

```python
from typing import Optional

@service
class OrderService:
    # typing.Optional style
    def __init__(self, cache: Optional[CacheAdapter] = None) -> None:
        self.cache = cache  # None if CacheAdapter is not registered

@service
class ShippingService:
    # PEP 604 style (Python 3.10+) — works identically
    def __init__(self, tracker: ShipmentTracker | None = None) -> None:
        self.tracker = tracker  # None if ShipmentTracker is not registered
```

### list[T]

Declare a parameter as `list[T]` to collect **all** implementations bound to type `T`.
This is equivalent to Spring's `List<T>` injection:

```python
@service
class ValidationService:
    def __init__(self, validators: list[Validator]) -> None:
        self.validators = validators  # all Validator implementations
```

If no implementations are bound, an empty list is injected.

---

## Circular Dependency Detection

The container detects circular dependencies during resolution and raises a clear
`BeanCurrentlyInCreationError` instead of entering infinite recursion:

```python
from pyfly.container import BeanCurrentlyInCreationError

class A:
    def __init__(self, b: B) -> None: ...

class B:
    def __init__(self, a: A) -> None: ...

# Raises: BeanCurrentlyInCreationError: Circular dependency: A -> B -> A
container.resolve(A)
```

The container tracks types currently being resolved in a `_resolving` dict. When a type
is encountered that is already being resolved, the cycle is detected and a descriptive
error message shows the full dependency chain.

---

## Interface Binding

Interface binding connects an abstract port (protocol or ABC) to a concrete adapter.
This is the core mechanism for hexagonal architecture in PyFly.

```python
from typing import Protocol

class EmailPort(Protocol):
    async def send(self, to: str, subject: str, body: str) -> None: ...

@service
class SmtpEmailAdapter:
    async def send(self, to: str, subject: str, body: str) -> None:
        # SMTP implementation
        ...

# Wire the binding
container.register(SmtpEmailAdapter)
container.bind(EmailPort, SmtpEmailAdapter)

# Now any class depending on EmailPort gets SmtpEmailAdapter
@service
class UserService:
    def __init__(self, email: EmailPort):
        self.email = email  # SmtpEmailAdapter instance
```

Multiple implementations can be bound to the same interface. Resolution follows these
rules:

1. If exactly one implementation is bound, it is used.
2. If multiple are bound, the one marked `@primary` is used.
3. If multiple are bound and none is `@primary`, a `KeyError` is raised.
4. Use `resolve_all()` to retrieve all implementations as a list.

---

## Component Scanning

Component scanning auto-discovers stereotype-decorated classes in specified packages.

```python
from pyfly.container.scanner import scan_package

count = scan_package("myapp.services", container)
# Returns the number of classes registered
```

### How It Works

1. The specified package is imported via `importlib.import_module()`.
2. If the module has a `__path__` (i.e., it is a package), all submodules are recursively
   walked using `pkgutil.walk_packages()`.
3. For each module, `scan_module_classes()` extracts all classes with
   `__pyfly_injectable__ = True` whose `__module__` matches the current module (to avoid
   re-registering imported classes).
4. Each discovered class is registered in the container with its scope, condition, and
   name from the stereotype decorator attributes.
5. **Auto-binding:** After registration, the scanner inspects the class's MRO and
   automatically binds it to any `Protocol`, `ABC`, or base class interface it
   implements. This eliminates the need for manual `container.bind()` calls — just like
   Spring's `@ComponentScan` auto-discovers `implements` relationships.

### Auto-Binding Example

```python
from typing import Protocol, runtime_checkable

@runtime_checkable
class OrderRepository(Protocol):
    async def find_by_id(self, id: int) -> dict: ...

@repository
class PostgresOrderRepository(OrderRepository):
    async def find_by_id(self, id: int) -> dict:
        ...
```

When `PostgresOrderRepository` is discovered during scanning, it is automatically
bound to `OrderRepository`. No manual `container.bind(OrderRepository, PostgresOrderRepository)`
is needed.

### scan_module_classes()

```python
def scan_module_classes(module: object) -> list[type]:
```

A lower-level function that extracts all injectable classes from a single module without
recursion. Used internally by `scan_package()` but also available for custom scanning
logic.

### Triggering via @pyfly_application

The most common way to trigger scanning is through the `scan_packages` parameter:

```python
@pyfly_application(
    name="my-app",
    scan_packages=["myapp.services", "myapp.controllers", "myapp.repositories"],
)
class MyApp:
    pass
```

During `PyFlyApplication.__init__()`, each package is scanned and discovered beans are
registered. The framework logs the package name and number of beans found for each scan.

---

## ApplicationContext

`ApplicationContext` is the central orchestrator. It wraps the `Container` and adds
lifecycle management, event publishing, profile filtering, condition evaluation, and
`@configuration`/`@bean` processing.

This is the PyFly equivalent of Spring's `ApplicationContext`. It is the recommended
entry point for bean access in application code.

```python
from pyfly.context import ApplicationContext
from pyfly.core import Config

config = Config.from_file("pyfly.yaml")
ctx = ApplicationContext(config)
```

During construction, the `ApplicationContext` automatically registers the `Config` object
itself as a singleton bean, making it available for injection into any component.

### get_bean()

```python
def get_bean(self, bean_type: type[T]) -> T:
```

Resolves a bean by type. Delegates to `Container.resolve()`.

```python
user_service = ctx.get_bean(UserService)
```

### get_bean_by_name()

```python
def get_bean_by_name(self, name: str) -> Any:
```

Resolves a bean by its registered name.

```python
primary_db = ctx.get_bean_by_name("primary_db")
```

### get_beans_of_type()

```python
def get_beans_of_type(self, bean_type: type[T]) -> list[T]:
```

Returns all beans of the given type, sorted by `@order` value. This calls
`Container.resolve_all()` internally and then sorts the results.

```python
all_validators = ctx.get_beans_of_type(Validator)
```

### register_bean() and register_post_processor()

```python
def register_bean(self, cls: type, **kwargs: Any) -> None:
def register_post_processor(self, processor: BeanPostProcessor) -> None:
```

Manually register a bean class or a `BeanPostProcessor` with the context. The
`register_bean()` method reads `name` and `scope` from kwargs or from the class's
stereotype attributes.

### ApplicationContext Properties

| Property | Type | Description |
|---|---|---|
| `container` | `Container` | Escape hatch: direct access to the underlying DI container. |
| `config` | `Config` | Application configuration. |
| `environment` | `Environment` | Profile-aware environment. |
| `event_bus` | `ApplicationEventBus` | Application event bus. |
| `bean_count` | `int` | Number of beans eagerly initialized during `start()` (counts all registrations with a non-None instance). |

### The start() Lifecycle

When `ApplicationContext.start()` is called, it executes these steps in order:

0. **Register auto-configurations** -- discovers `@auto_configuration` classes via
   `importlib.metadata.entry_points(group="pyfly.auto_configuration")`. Each subsystem
   (web, cache, messaging, client, data) owns its own `@auto_configuration` class,
   declared in `pyproject.toml` entry points -- like Spring Boot's
   `META-INF/spring.factories`.
1. **Filter beans by active profiles** -- removes beans whose `profile` expression does
   not match the active profiles.
1b. **Evaluate conditions (pass 1)** -- removes beans that fail non-bean-dependent conditions
    (`@conditional_on_property`, `@conditional_on_class`, and stereotype `condition` callables).
2. **Process user `@configuration` classes** -- resolves configuration beans and registers
   their `@bean` factory method outputs.
2b. **Evaluate conditions (pass 2)** -- removes beans that fail bean-dependent conditions
    (`@conditional_on_bean`, `@conditional_on_missing_bean`).
2c. **Process `@auto_configuration` classes** -- resolves auto-configuration `@bean` methods
    after user beans are visible. Each auto-configuration class uses `@conditional_on_class`,
    `@conditional_on_property`, and `@conditional_on_missing_bean` to guard its beans,
    so user-provided beans always take precedence.
2c. **Start infrastructure** -- starts any bean that implements `start()`/`stop()` lifecycle
    methods (e.g., cache adapters, message brokers, HTTP clients). Failures here raise
    `BeanCreationException` for fast feedback.
3. **Auto-discover `BeanPostProcessor` implementations** from registered beans.
3b. **Bind `@config_properties` beans** -- sets a factory on each `@config_properties`
    registration so instances are produced by `Config.bind()` and injectable by type.
4. **Eagerly resolve all singletons** -- sorted by `@order` value.
5. **Run post-processors and lifecycle hooks** -- for each resolved bean:
   - `BeanPostProcessor.before_init()`
   - `@post_construct` methods
   - `BeanPostProcessor.after_init()`
6. **Wire decorator-based beans** -- connects `@app_event_listener`, `@message_listener`,
   CQRS handlers, `@scheduled` methods, and `@async_method` to their targets.
7. **Publish lifecycle events** -- `ContextRefreshedEvent`, then `ApplicationReadyEvent`.

### The stop() Lifecycle

When `ApplicationContext.stop()` is called:

1. `@pre_destroy` methods are called on all resolved beans in **reverse** initialization order.
2. `ContextClosedEvent` is published.

---

## Lifecycle Hooks

### @post_construct

```python
from pyfly.context import post_construct

@service
class CacheWarmer:
    @post_construct
    async def warm_cache(self):
        """Called after all dependencies are injected."""
        await self._load_frequently_accessed_data()
```

`@post_construct` marks a method to be called after the bean is fully initialized (after
constructor injection and `BeanPostProcessor.before_init()`). The method can be either
synchronous or asynchronous -- if it returns an awaitable, the context will `await` it.

A bean can have multiple `@post_construct` methods. The decorator simply sets
`__pyfly_post_construct__ = True` on the method.

### @pre_destroy

```python
from pyfly.context import pre_destroy

@service
class DatabasePool:
    @pre_destroy
    async def close_pool(self):
        """Called before the bean is destroyed during shutdown."""
        await self.pool.close()
```

`@pre_destroy` marks a method to be called during shutdown. Like `@post_construct`, it
supports both sync and async methods. Beans are destroyed in reverse initialization order.
The decorator sets `__pyfly_pre_destroy__ = True` on the method.

---

## BeanPostProcessor

`BeanPostProcessor` is a `Protocol` (runtime-checkable) that lets you hook into the bean
creation lifecycle. Implementations are called for **every** bean resolved by the
`ApplicationContext`.

```python
from pyfly.context import BeanPostProcessor

@runtime_checkable
class BeanPostProcessor(Protocol):
    def before_init(self, bean: Any, bean_name: str) -> Any:
        """Called before @post_construct. May return a replacement bean."""
        ...

    def after_init(self, bean: Any, bean_name: str) -> Any:
        """Called after @post_construct. May return a replacement bean."""
        ...
```

Both methods receive the bean instance and the bean name. Both methods **must return a
bean** (either the original or a replacement). This return-a-replacement pattern enables
proxy wrapping (e.g., for AOP).

### Example: Logging Post-Processor

```python
from pyfly.container import component, order, HIGHEST_PRECEDENCE
import structlog

logger = structlog.get_logger()

@order(HIGHEST_PRECEDENCE)
@component
class LoggingPostProcessor:
    def before_init(self, bean, bean_name: str):
        logger.debug("initializing_bean", name=bean_name)
        return bean

    def after_init(self, bean, bean_name: str):
        logger.info("bean_initialized", name=bean_name, type=type(bean).__name__)
        return bean
```

### Example: Proxy Post-Processor

Post-processors can return a **replacement** object, enabling proxy patterns like AOP:

```python
@component
class TimingPostProcessor:
    def before_init(self, bean, bean_name: str):
        return bean  # No change before init

    def after_init(self, bean, bean_name: str):
        # Wrap the bean with a timing proxy
        return TimingProxy(bean)
```

Post-processors are applied in `@order` order. They are registered with
`ApplicationContext.register_post_processor()`.

### Built-in Post-Processors

PyFly's own modules use `BeanPostProcessor` extensively:

| Post-Processor | Module | Purpose |
|---|---|---|
| `AspectBeanPostProcessor` | `pyfly.aop` | Weaves AOP advice into target beans. |
| `RepositoryBeanPostProcessor` | `pyfly.data` | Wires query methods onto repository beans. |
| `HttpClientBeanPostProcessor` | `pyfly.client` | Generates HTTP client method implementations. |

---

## Conditional Beans

Conditional decorators control whether a bean is included during startup. They are evaluated
by the `ConditionEvaluator` in a two-pass strategy.

Multiple conditions can be stacked on a single class. All conditions must pass for the
bean to be included. Conditions are stored as a list of dicts in the `__pyfly_conditions__`
attribute.

### @conditional_on_property

```python
from pyfly.context import conditional_on_property

@conditional_on_property("pyfly.cache.enabled", having_value="true")
@service
class RedisCacheService:
    ...
```

The bean is only registered if the config key exists and (optionally) matches the specified
value. If `having_value` is empty, the condition passes as long as the key has any non-None
value.

| Parameter | Type | Description |
|---|---|---|
| `key` | `str` | Dot-notation config key to check. |
| `having_value` | `str` | Expected value (empty string means "any non-None value"). |

### @conditional_on_class

```python
from pyfly.context import conditional_on_class

@conditional_on_class("redis.asyncio")
@service
class RedisCacheAdapter:
    ...
```

The bean is only registered if the specified Python module is importable. This mirrors
Spring Boot's `@ConditionalOnClass` and is used for library-aware auto-configuration.
Internally, it attempts `importlib.import_module(module_name)` and catches `ImportError`.

### @conditional_on_bean

```python
from pyfly.context import conditional_on_bean

@conditional_on_bean(DataSource)
@service
class DataMigrator:
    """Only activate if a DataSource bean exists."""
    ...
```

The bean is only registered if another bean of the specified type (or a subclass of it)
is present in the container. Evaluated in pass 2 (after pass 1 conditions have been
applied). The declaring class itself is excluded from the check.

### @conditional_on_missing_bean

```python
from pyfly.context import conditional_on_missing_bean

@conditional_on_missing_bean(CacheAdapter)
@service
class InMemoryCacheFallback:
    """Only activate if no CacheAdapter is registered."""
    ...
```

The bean is only registered if **no** other bean of the specified type (or a subclass)
exists. This is the key mechanism for "default with override" patterns: auto-configuration
provides a default that is automatically skipped when the user provides their own
implementation.

### @auto_configuration

```python
from pyfly.context import auto_configuration
from pyfly.container import configuration, bean

@auto_configuration
class CacheAutoConfiguration:

    @bean
    def cache_adapter(self) -> CacheAdapter:
        return InMemoryCache()
```

`@auto_configuration` marks a `@configuration` class for deferred processing. Auto-configuration classes:

- Are processed **after** user `@configuration` classes during startup.
- Receive an implicit `@order(1000)` (lower priority than default).
- Work seamlessly with `@conditional_on_*` decorators.
- Have `__pyfly_auto_configuration__ = True`, `__pyfly_injectable__ = True`, and
  `__pyfly_stereotype__ = "configuration"` set automatically (so you do not need to also
  add `@configuration`).

### @config_properties Beans as Injectable Dependencies

Classes decorated with `@config_properties` are also injectable by type. The decorator
sets `__pyfly_injectable__ = True`, so the component scanner registers them as container
beans (with stereotype `"config_properties"`). Any bean can declare a
`@config_properties` class as a constructor parameter and receive the bound instance:

```python
@config_properties(prefix="pyfly.data")
@dataclass
class DataConfig:
    url: str = "sqlite+aiosqlite:///pyfly.db"
    pool_size: int = 5

@service
class OrderRepository:
    def __init__(self, data_config: DataConfig) -> None:
        self.url = data_config.url  # injected automatically
```

The `DataConfig` instance is bound from `Config.effective_section("pyfly.data")` (with
placeholders resolved and env overrides applied) before being registered in the container.

---

### Two-Pass Evaluation

The `ConditionEvaluator` uses a two-pass strategy to handle ordering dependencies:

| Pass | Conditions Evaluated | When |
|---|---|---|
| **Pass 1** | `@conditional_on_property`, `@conditional_on_class`, stereotype `condition` callable | Before any `@configuration` classes are processed. |
| **Pass 2** | `@conditional_on_bean`, `@conditional_on_missing_bean` | After user `@configuration` classes are processed but before `@auto_configuration`. |

This ensures that bean-dependent conditions see the full set of user-provided beans but
not yet the auto-configured defaults. The separation prevents auto-configuration from
blocking itself.

---

## Application Events

PyFly provides an event bus for application lifecycle notifications. Events are published
during startup and shutdown.

### Built-in Events

All events inherit from the `ApplicationEvent` base class.

| Event | Published When |
|---|---|
| `ContextRefreshedEvent` | The `ApplicationContext` is fully initialized (all beans created, all post-processors run). |
| `ApplicationReadyEvent` | The application is ready to serve requests (published immediately after `ContextRefreshedEvent`). |
| `ContextClosedEvent` | The `ApplicationContext` is shutting down (published after all `@pre_destroy` methods). |

### @app_event_listener

```python
from pyfly.context import app_event_listener, ApplicationReadyEvent

@service
class StartupNotifier:

    @app_event_listener
    async def on_ready(self, event: ApplicationReadyEvent):
        print("Application is ready!")
```

The `@app_event_listener` decorator marks a method as a listener for application events.
The event type is inferred from the method's type hint on the event parameter. The
decorator sets `__pyfly_app_event_listener__ = True` on the method.

### ApplicationEventBus

The `ApplicationEventBus` is the in-process event bus that dispatches lifecycle events.

```python
class ApplicationEventBus:
    def subscribe(
        self,
        event_type: type[ApplicationEvent],
        listener: Callable[..., Awaitable[None]],
        *,
        owner_cls: type | None = None,
    ) -> None:
        """Register a listener for a specific event type."""

    async def publish(self, event: ApplicationEvent) -> None:
        """Publish an event to all matching listeners, sorted by @order."""
```

Key behaviors:

- Listeners are invoked in `@order` order of their owning class (lower order = called
  first).
- Each listener must be an async callable.
- Event matching uses `isinstance()`, so a listener for `ApplicationEvent` receives all
  event types.

---

## Environment

The `Environment` class provides unified access to configuration properties and active
profiles.

```python
from pyfly.context import Environment

env = ctx.environment
```

### Properties and Methods

| Member | Description |
|---|---|
| `active_profiles` | `list[str]` -- currently active profiles (returns a copy). |
| `accepts_profiles(*profiles)` | Returns `True` if any of the given profile expressions match. |
| `get_property(key, default)` | Get a configuration property by dotted key (delegates to `Config.get()`). |

### Profile Expression Syntax

`accepts_profiles()` supports:

| Expression | Meaning |
|---|---|
| `"dev"` | Matches if `"dev"` is an active profile. |
| `"!production"` | Matches if `"production"` is **not** active. |
| `"dev,test"` | Matches if `"dev"` **or** `"test"` is active (comma = OR). |

### Profile Loading Priority

1. `PYFLY_PROFILES_ACTIVE` environment variable (comma-separated).
2. `pyfly.profiles.active` config key.

---

## Complete Example

This example builds a multi-layer application with DI: a REST controller, a service,
a repository with interface binding, lifecycle hooks, conditional beans, and event
listeners.

### Ports (Interfaces)

```python
# ports.py
from typing import Protocol, runtime_checkable

@runtime_checkable
class UserRepository(Protocol):
    async def find_by_id(self, user_id: str) -> dict | None: ...
    async def save(self, user: dict) -> None: ...

@runtime_checkable
class NotificationSender(Protocol):
    async def send(self, to: str, message: str) -> None: ...
```

### Repository Implementation

```python
# repositories.py
from pyfly.container import repository, primary
from pyfly.context import post_construct, pre_destroy

@primary
@repository
class PostgresUserRepository(UserRepository):
    """Production repository backed by PostgreSQL.

    Explicitly inherits UserRepository so the scanner auto-binds it.
    """

    @post_construct
    async def init_pool(self):
        self.pool = await create_pool("postgresql://localhost/mydb")

    @pre_destroy
    async def close_pool(self):
        await self.pool.close()

    async def find_by_id(self, user_id: str) -> dict | None:
        async with self.pool.acquire() as conn:
            return await conn.fetchrow("SELECT * FROM users WHERE id = $1", user_id)

    async def save(self, user: dict) -> None:
        ...
```

### Service Layer

```python
# services.py
from pyfly.container import service

@service
class UserService:
    def __init__(self, repo: UserRepository, notifier: NotificationSender):
        self.repo = repo
        self.notifier = notifier

    async def create_user(self, name: str, email: str) -> dict:
        user = {"name": name, "email": email}
        await self.repo.save(user)
        await self.notifier.send(email, f"Welcome, {name}!")
        return user
```

### Conditional Notification Sender

```python
# notifications.py
from pyfly.container import service
from pyfly.context import conditional_on_property, conditional_on_missing_bean

@conditional_on_property("pyfly.smtp.host")
@service
class SmtpNotificationSender:
    async def send(self, to: str, message: str) -> None:
        # Real SMTP sending
        ...

@conditional_on_missing_bean(NotificationSender)
@service
class LoggingNotificationSender:
    """Fallback: just log the notification."""
    async def send(self, to: str, message: str) -> None:
        print(f"[NOTIFICATION] to={to} message={message}")
```

### Configuration with @bean

```python
# config.py
from pyfly.container import configuration, bean
from pyfly.context import auto_configuration, conditional_on_class

@configuration
class AppConfig:

    @bean(name="primary_db")
    def primary_database(self) -> DataSource:
        return PostgresDataSource(url="postgresql://primary/db")

@auto_configuration
@conditional_on_class("redis.asyncio")
class CacheAutoConfig:

    @bean
    def cache(self) -> CacheAdapter:
        import redis.asyncio as aioredis
        return RedisCacheAdapter(aioredis.from_url("redis://localhost:6379"))
```

### Controller

```python
# controllers.py
from pyfly.container import rest_controller

@rest_controller
class UserController:
    def __init__(self, user_service: UserService):
        self.user_service = user_service
```

### Event Listener

```python
# listeners.py
from pyfly.container import component, order
from pyfly.context import app_event_listener, ApplicationReadyEvent, ContextClosedEvent

@order(100)
@component
class LifecycleLogger:

    @app_event_listener
    async def on_ready(self, event: ApplicationReadyEvent):
        print("Application is ready to serve requests")

    @app_event_listener
    async def on_close(self, event: ContextClosedEvent):
        print("Application is shutting down")
```

### Application Entry Point

```python
# app.py
import asyncio
from pyfly.core import PyFlyApplication, pyfly_application

@pyfly_application(
    name="user-service",
    version="1.0.0",
    scan_packages=["myapp"],
)
class UserApp:
    pass

async def main():
    app = PyFlyApplication(UserApp)

    # Interface bindings are auto-discovered during scanning when
    # implementations explicitly inherit from Protocol/ABC interfaces.
    # No manual container.bind() calls needed!

    await app.startup()

    # Use the application
    user_service = app.context.get_bean(UserService)
    await user_service.create_user("Alice", "alice@example.com")

    await app.shutdown()

if __name__ == "__main__":
    asyncio.run(main())
```

This example demonstrates:

- **Stereotype decorators** (`@service`, `@repository`, `@rest_controller`, `@component`, `@configuration`)
- **Constructor injection** (type-hint based, fully automatic)
- **Field injection** (`Autowired()` for optional or supplemental dependencies)
- **Auto-binding** (interfaces are automatically bound during component scanning)
- **@primary** (default implementation selection)
- **@bean factory methods** (inside `@configuration`)
- **Lifecycle hooks** (`@post_construct`, `@pre_destroy`)
- **Conditional beans** (`@conditional_on_property`, `@conditional_on_missing_bean`, `@conditional_on_class`)
- **@auto_configuration** (deferred, low-priority config)
- **@order** (initialization ordering)
- **Application events** (`ApplicationReadyEvent`, `ContextClosedEvent`)
- **Component scanning** (via `scan_packages` with auto-binding)
