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
   - [Introspection / registration SPI](#introspection-registration-spi)
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
   - [SESSION](#session)
   - [Custom scopes (ScopeHandler SPI)](#custom-scopes-scopehandler-spi)
   - [@refresh_scope (Spring Cloud parity)](#refresh_scope-spring-cloud-parity)
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
    - [@conditional_on_single_candidate](#conditional_on_single_candidate)
    - [@conditional_on_web_application](#conditional_on_web_application)
    - [@conditional_on_resource](#conditional_on_resource)
    - [@auto_configuration](#auto_configuration)
    - [Two-Pass Evaluation](#two-pass-evaluation)
18. [Application Events](#application-events)
    - [Built-in Events](#built-in-events)
    - [@app_event_listener](#app_event_listener)
    - [ApplicationEventBus](#applicationeventbus)
19. [Environment](#environment)
20. [Spring-parity DI features (v26.06.22+)](#spring-parity-di-features-v260622)
    - [Constructor-parameter @Value](#constructor-parameter-value)
    - [@Value SpEL expressions](#value-spel-expressions)
    - [Provider[T] — deferred resolution](#providert-deferred-resolution)
    - [Map injection — dict[str, T]](#map-injection-dictstr-t)
    - [@lazy beans](#lazy-beans)
    - [Generics-aware injection](#generics-aware-injection)
    - [@bean(primary=..., profile=...)](#beanprimary-profile)
    - [@Qualifier type verification](#qualifier-type-verification)
21. [Complete Example](#complete-example)

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
| **Scope** | How long an instance lives: `SINGLETON`, `TRANSIENT`, `REQUEST`, `SESSION`, or a custom/`"refresh"` string scope. |
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
| `scope` | `Scope \| str` | Lifecycle scope (default `SINGLETON`), or a custom scope name. |
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

1. **Direct registration** -- if `cls` is registered, resolve it. When several beans share that
   exact class (two `@bean` methods that both return `AsyncEngine`), the one marked `@primary`
   (or `@bean(primary=True)`) answers; without exactly one primary the lookup raises
   `NoUniqueBeanError`, naming the beans (and, when several are `@primary`, those). Through 26.09.07
   the bean registered **last** answered silently. Resolve one of them by name or `Qualifier`, or
   all of them with `list[T]`.
2. **Interface binding** -- if `cls` has exactly one bound implementation, resolve it.
3. **Multiple bindings** -- pick the implementation marked `@primary`.
4. **Error** -- `NoSuchBeanError` if nothing matches; `NoUniqueBeanError` if multiple candidates exist without a `@primary`.

Once `ApplicationContext.stop()` starts destroying beans, resolving a singleton that does not
exist yet raises `BeanCreationNotAllowedError` (Spring's `BeanCreationNotAllowedException`): a bean
built then would outlive the stop, as an `AsyncEngine` rebuilt after the datasource registry closed
did. Transient and scoped beans are still built while the singletons are destroyed, so a
`@pre_destroy` can use them; once the stop has released the singletons, no bean of any scope is
built. The instances that still exist are handed out until the stop releases them; `start()` allows
creation again.

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

### Introspection / registration SPI

`Container` exposes a small **public SPI** for installing pre-built instances and
inspecting registrations, so callers (refresh/config-reload, tests, tooling) never have
to reach into the private `_registrations` dict. All five methods live on `Container`
(from `pyfly.container`):

```python
def register_instance(self, cls: type, instance: Any, *, name: str = "") -> None:
def contains_type(self, cls: type) -> bool:
def get_registration(self, cls: type) -> Registration | None:
def registered_types(self) -> list[type]:
def reset_instance(self, cls: type) -> Any | None:
```

| Method | Behaviour |
|---|---|
| `register_instance(cls, instance, *, name="")` | Register an already-constructed object as a `SINGLETON` bean (Spring's `registerSingleton`). The supported way to install a pre-built instance — preferred over mutating registration internals. |
| `contains_type(cls)` | `True` if a bean is registered under **exactly** `cls`. (Compare with `contains(name)`, which checks the named-bean store.) |
| `get_registration(cls)` | The `Registration` for `cls`, or `None` if unregistered. |
| `registered_types()` | A snapshot `list[type]` of every registered bean type. |
| `reset_instance(cls)` | Drop the cached `SINGLETON` instance of `cls` so it is rebuilt on the next `resolve()`. Returns the evicted instance (or `None`). Used by refresh / config-reload to force re-creation without reaching into registration internals. |

```python
from pyfly.container import Container

container = Container()

# Install a pre-built singleton (optionally named):
container.register_instance(Clock, SystemClock(), name="system_clock")

# Inspect registrations:
container.contains_type(Clock)        # True
reg = container.get_registration(Clock)   # Registration | None
all_types = container.registered_types()  # [Clock, ...]

# Force re-creation on next resolve (refresh / config reload):
previous = container.reset_instance(Clock)  # returns the evicted instance or None
fresh = container.resolve(Clock)            # rebuilt
```

**Source:** `src/pyfly/container/container.py`

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
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from pyfly.container import repository

@repository
class UserRepository:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]):
        self._sessions = sessions

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
| `scope` | `Scope \| str` | `Scope.SINGLETON` | Lifecycle scope: a `Scope`, or a custom scope name such as `"refresh"`. |
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
    SESSION = auto()
```

A scope can also be a **string** naming a custom scope registered via
`Container.register_scope()` — the `scope=` parameter accepts `ScopeSpec = Scope | str`
everywhere a `Scope` is accepted.

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

A singleton cannot take a request-scoped bean as a plain dependency: there is no request when the
singleton is built. Mark the bean `@scoped_proxy` (from `pyfly.container.refresh_scope`) and the
singleton receives a proxy that resolves the current request's instance on every use (see
[Scoped proxies](#scoped-proxies)), or inject `Provider[CurrentUser]` and call `get()` inside the
request.

### SESSION

`Scope.SESSION` creates **one instance per HTTP session**. The instance is stored as an
attribute on the active `HttpSession`, so it persists across requests within the same
session (and is discarded when the session ends). Resolution reads the session from the
active request context.

```python
from pyfly.container import component, Scope

@component(scope=Scope.SESSION)
class ShoppingCart:
    def __init__(self) -> None:
        self.items: list[str] = []
```

This requires the session module to be enabled (a `SessionFilter` populating the request
context's `HttpSession`). Resolving a `SESSION`-scoped bean outside an active session (no
request context, or no session) raises `RuntimeError`. Because the instance lives as a
session attribute, it must be serializable when a non-memory session store (e.g. Redis) is
used.

### Custom scopes (ScopeHandler SPI)

PyFly exposes Spring's custom-scope SPI: register a handler under a name with
`Container.register_scope(name, handler)`, then declare beans with that scope string. A
handler implements the `ScopeHandler` protocol from `pyfly.container.types`:

```python
from collections.abc import Callable
from typing import Any

class ScopeHandler(Protocol):
    def get(self, name: str, object_factory: Callable[[], Any]) -> Any: ...
    def remove(self, name: str) -> Any | None: ...
```

- `get(name, object_factory)` returns the cached instance for `name`, or calls
  `object_factory()` (at most once), caches the result, and returns it.
- `remove(name)` evicts `name`, returning the removed instance or `None`.
- Optionally, `evict_all() -> dict[str, Any]` evicts every cached instance and returns them by
  name. When a handler has it, `ApplicationContext.stop()` calls it and destroys the returned
  instances after the singletons' `@pre_destroy` (the built-in refresh scope implements it).

`name` is the bean's scope key: `__pyfly_bean_<module>.<class qualname>`, followed by
`#<bean name>` for a named bean. It identifies the bean *definition*, so two `@bean` methods that
return one class (two request- or refresh-scoped session factories, one per database) get two
instances; through 26.09.07 the key was the class `__qualname__` alone, and every name after the
first received the first one's instance. A `SESSION`-scoped bean stored by an older version under
the old key is adopted and moved when exactly one session-scoped bean maps to that key.

```python
from collections.abc import Callable
from typing import Any
from pyfly.container import Container, component

class ThreadScope:
    """A trivial per-instance cache; a real handler might key by thread id."""
    def __init__(self) -> None:
        self._cache: dict[str, Any] = {}

    def get(self, name: str, object_factory: Callable[[], Any]) -> Any:
        if name not in self._cache:
            self._cache[name] = object_factory()
        return self._cache[name]

    def remove(self, name: str) -> Any | None:
        return self._cache.pop(name, None)

container = Container()
container.register_scope("thread", ThreadScope())

@component(scope="thread")          # string scope name
class PerThreadState:
    ...

container.register(PerThreadState)  # scope picked up from the decorator
```

`register_scope()` rejects an empty name and refuses to override the built-in scope names
(`"singleton"`, `"transient"`, `"request"`, `"session"`), raising `ValueError`.
`unregister_scope(name)` removes a custom scope (no-op if absent). Resolving a bean whose
string scope has no registered handler raises `RuntimeError` naming the missing scope.

### @refresh_scope (Spring Cloud parity)

`@refresh_scope` (or `scope="refresh"`) marks a bean as **refresh-scoped**: it is cached
like a singleton, but a *refresh* evicts every refresh-scoped instance so the next
resolution rebuilds it — re-running constructor/field injection and re-reading `@Value`
placeholders against the live `Config`. This mirrors Spring Cloud's `@RefreshScope`.

The `"refresh"` scope is **built in**: `ApplicationContext` registers a `RefreshScope`
handler under that name during construction, so no `register_scope()` call is needed.

```python
from pyfly.container import component, refresh_scope
from pyfly.core.value import Value

@refresh_scope
@component
class FeatureFlags:
    # re-read from the live Config every time the bean is rebuilt after a refresh
    enabled: bool = Value("${features.checkout.enabled:false}")
```

`refresh_scope`, `scoped_proxy`, `RefreshScope`, and `REFRESH_SCOPE_NAME` (`= "refresh"`) live in
`pyfly.container.refresh_scope` (`refresh_scope` and `RefreshScope` are also re-exported
from `pyfly.container`). The decorator sets `__pyfly_scope__ = "refresh"` on the class.

The decorator order no longer matters: `@component` written above `@refresh_scope` used to reset
the scope to `SINGLETON`, and the container now keeps the refresh scope either way. You can also
write the scope inline: `@component(scope="refresh")`.

**Evicted instances are destroyed.** A refresh evicts the cached instances and then destroys them,
after the swap: a new resolution already gets the rebuilt bean while the evicted one closes.
`ApplicationContext.stop()` destroys the instances still cached. Destroying an instance runs, in
order:

1. its `@pre_destroy` methods;
2. for the product of a `@bean` method, its destroy method: the one `@bean(destroy_method=...)`
   names, or, when the product declares no `@pre_destroy` and no `stop()`, the first of
   `dispose()`, `aclose()` and `close()` it has that takes no argument (Spring's inferred destroy
   method), so a refresh-scoped `AsyncEngine` bean is disposed. Before an `AsyncEngine`'s
   `dispose()`, the context makes a connection still in use close when it is returned
   (`dispose()` alone closes the idle ones, and a connection returned later stayed open in the
   disposed pool until the garbage collector found it); the engine pools again afterwards. Nothing
   is inferred for an engine a datasource registry owns (a scoped `@bean` that hands out
   `registry.engine("reporting")`): the registry disposes it, last;
3. `stop()`, when it defines `start()` and `stop()`. The context does not start a scoped lifecycle
   bean (the scope builds it on demand, in a synchronous resolution), but `stop()` is how it
   releases what it holds.

Each step is bounded by `pyfly.context.shutdown-timeout`; a failure is logged. Through 26.09.07 nothing
was destroyed, so each refresh of a bean that owned an engine leaked a connection pool.

**Injecting a refresh-scoped bean into a singleton.** A singleton receives the instance that exists
when it is built and keeps it: a refresh does not reach it. Either declare the bean with
`@refresh_scope(proxy=True)`, which injects a [scoped proxy](#scoped-proxies) that always forwards
to the current instance (Spring Cloud proxies refresh-scoped beans by default; PyFly makes it
opt-in), or inject `Provider[FeatureFlags]` and call `get()` each time.

```python
from pyfly.data.relational.datasource_registry import close_connections_on_return

@refresh_scope(proxy=True)
@component
class ReportingDataSource:
    def __init__(self, config: Config) -> None:
        self.engine = create_async_engine(str(config.get("reporting.url")))
        close_connections_on_return(self.engine)   # at creation (see Scoped proxies below)

    @pre_destroy
    async def close(self) -> None:      # runs when a refresh evicts this instance
        await self.engine.dispose()


@service
class ReportService:
    def __init__(self, reporting: ReportingDataSource) -> None:
        self.reporting = reporting       # a proxy: follows every refresh
```

For the application's own datasources you need none of this: inject the `DataSourceRegistry`, the
`AsyncEngine` or the session factory beans. They are stable singletons, and a refresh that rotates
the credentials evicts their pools in place (see the data-relational guide).

#### Scoped proxies

A scoped proxy stands in for a bean of a narrower scope (request, session, refresh or any custom
scope) and resolves the instance its scope holds on every use: an attribute, a call, `async with`.
`isinstance(proxy, Target)` is true; `type(proxy)` is `ScopedProxy`, and
`pyfly.container.scoped_proxy.proxy_target(proxy)` returns the current instance. Opt in per bean:

- `@refresh_scope(proxy=True)` on a class;
- `@scoped_proxy` on a class (with a request, session or custom scope) or on a non-singleton
  `@bean` method, written above `@bean`. On a singleton or transient `@bean` method it raises
  `TypeError` (written below `@bean`, when the context starts: it used to be ignored there), and so
  does registering a `@scoped_proxy` class as a singleton or transient bean.

A `with` or `async with` block on a proxy exits on the instance it entered, even when a refresh
swapped the scope's instance inside the block. A proxy forwards attribute access, calls, `bool()`,
`len()`, `iter()`, `in`, `[]`, `with` and `async with`, but not `async for`, `await`, `==` or
`hash()`: `async for` and `await` on a proxy raise `TypeError`, and `==` and `hash()` answer for the
proxy itself. Use `proxy_target(proxy)` for those.

```python
from pyfly.container.refresh_scope import scoped_proxy
from pyfly.data.relational.datasource_registry import close_connections_on_return

class ReportingDatabase:
    """The reporting engine and its sessions, rebuilt on every refresh."""

    def __init__(self, url: str) -> None:
        self.engine = create_async_engine(url)
        close_connections_on_return(self.engine)
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)

    async def dispose(self) -> None:
        await self.engine.dispose()


@configuration
class ReportingConfiguration:
    @scoped_proxy
    @bean(scope="refresh")
    def reporting_database(self, config: Config) -> ReportingDatabase:
        return ReportingDatabase(str(config.get("reporting.url")))
```

Each refresh destroys the evicted holder through its inferred destroy method (`dispose()`), and so
does `ApplicationContext.stop()` for the live one. An engine a bean creates and disposes itself
needs `close_connections_on_return(engine)` (from `pyfly.data.relational.datasource_registry`)
**when it is created**: without it, a connection in use during the refresh goes back into the
disposed pool when it is returned and stays open until the garbage collector finds that pool.
Calling it later, next to `dispose()`, adds pool listeners while a connection may be connecting,
which breaks that connect. The context hooks every `AsyncEngine` a `@bean` method returns, and the
datasource registry every engine it builds.

The holder type keeps the reporting engine apart from the application's primary. A refresh-scoped
`AsyncEngine` bean does not replace the primary either (only a singleton does), but an injection by
type (`AsyncEngine`) receives the primary, so it has to be injected by name.

#### Triggering a refresh — ContextRefresher

`ApplicationContext` also registers a singleton `ContextRefresher` (from `pyfly.context`)
that you can inject. Calling its async `refresh()` evicts all refresh-scoped beans, resets
`@config_properties` beans (so they re-bind from the live `Config` on next resolution),
destroys the evicted instances (see above), and publishes a
`RefreshScopeRefreshedEvent`. It returns the scope keys that were evicted
(`__pyfly_bean_<module>.<class>` or `...#<bean name>`).

```python
from pyfly.context import ContextRefresher, app_event_listener, RefreshScopeRefreshedEvent
from pyfly.container import service

@service
class ConfigAdmin:
    def __init__(self, refresher: ContextRefresher) -> None:
        self.refresher = refresher

    async def reload(self) -> list[str]:
        # rebuilds refresh-scoped + @config_properties beans against the live Config
        return await self.refresher.refresh()

@service
class RefreshLogger:
    @app_event_listener
    async def on_refresh(self, event: RefreshScopeRefreshedEvent) -> None:
        print("refreshed beans:", event.refreshed)
```

`RefreshScopeRefreshedEvent` (in `pyfly.context`) carries a `refreshed: list[str]` of the
evicted cache keys.

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

    @bean(name="audit_client", scope=Scope.TRANSIENT)
    def audit_client(self) -> AuditClient:
        return AuditClient(base_url="https://audit.internal")
```

### How It Works

During `ApplicationContext.start()`, the context:

1. Finds all classes with `__pyfly_stereotype__ == "configuration"`.
2. Resolves the configuration class itself (so it can receive injected dependencies).
3. Iterates over methods marked with `__pyfly_bean__ = True`.
4. Reads the return type hint to determine the bean's type.
5. For a **singleton**, calls the method (injecting any method parameters from the container) and
   registers the returned object under its concrete class and under the declared return type.
6. For a **non-singleton** (`TRANSIENT`, `REQUEST`, `SESSION`, `"refresh"`, a custom scope) whose
   return hint declares one class, does **not** call the method: it registers the method as the
   factory of that class, and the scope calls it when it needs an instance. Through 26.09.07 every
   `@bean` method ran once at startup whatever its scope, so a refresh-scoped or transient factory
   built an object for nothing (an extra engine per scoped datasource) and a request-scoped
   factory ran outside any request. A non-singleton is therefore resolvable by its declared
   return type, not by the concrete class it returns (as in Spring). A parametrized hint declares
   its origin class: `-> async_sessionmaker[AsyncSession]` (or `... | None`) registers
   `async_sessionmaker`, which is what a parameter or a `Provider` of
   `async_sessionmaker[AsyncSession]` resolves. A hint that declares no single class (`A | B`,
   `list[X]`) still makes the method run once at startup to learn the type.

### @bean Parameters

| Parameter | Type | Default | Description |
|---|---|---|---|
| `name` | `str` | `""` | Bean name. Defaults to the method name if not specified. |
| `scope` | `Scope \| str` | `Scope.SINGLETON` | Lifecycle scope of the produced bean: a `Scope`, or a custom scope name such as `"refresh"`. |
| `primary` | `bool` | `False` | The primary candidate among beans of one type (see [@bean(primary=..., profile=...)](#beanprimary-profile)). |
| `profile` | `str` | `""` | Create the bean only for matching profiles. |
| `destroy_method` | `str` | `INFER_DESTROY_METHOD` | The method called on the product when the bean is destroyed, after its `@pre_destroy` (a coroutine is awaited). `""` declares none. |

**Destroy methods.** A singleton is destroyed when the context stops: its `@pre_destroy` runs in
step 3 of [the stop() lifecycle](#the-stop-lifecycle), and its destroy method at the end of step 4,
after the lifecycle beans stopped, since one of them may still use the product in its `stop()`. A
refresh- or custom-scoped bean is destroyed when its scope evicts it and when the context stops; a
transient bean never. The default, `INFER_DESTROY_METHOD`
(`pyfly.container.bean`, Spring's `"(inferred)"`), infers the method for a **scoped** bean that declares
no `@pre_destroy` and no `stop()`: the first of `dispose()`, `aclose()` and `close()` it has that takes no
argument. A **singleton** infers nothing: its product is usually released by its owner, in order (the
datasource registry closes every engine last, a lifecycle bean closes its client), and closing it
earlier would break that order. Name the method to have a singleton's product destroyed:

```python
@configuration
class PartnerApiConfiguration:
    @bean(destroy_method="aclose")
    def partner_http(self, config: Config) -> httpx.AsyncClient:
        return httpx.AsyncClient(base_url=str(config.get("partner.url")))
```

**A second database is not an `AsyncEngine` bean.** A singleton `AsyncEngine` or `async_sessionmaker`
bean **replaces the application's primary** for `@transactional`, every repository, `SessionProvider`,
`infrastructure_unit()`, the session factory and the `AsyncSession` bean: the relational
auto-configuration backs off. The framework's SQL stores (event store, snapshots, checkpoints, saga
persistence, the SQL cache, the scheduler lock, the token and session stores, the outbox) still resolve
their datasource in the context's `DataSourceRegistry`, whose primary keeps `pyfly.data.relational.url`
(a WARNING, `relational_engine_not_in_registry`, says so when an engine or session factory bean over an
engine of its own meets a configured URL). A singleton `DataSourceRegistry` bean replaces the registry
for all of them.
Declare a second database under `pyfly.data.relational.datasources.<name>` and inject
`DataSourceRegistry` (`registry.engine("archive")`, `registry.session_factory("archive")`) or
`NamedDataSources`: the registry builds it with the same pool and dialect setup and disposes it last
(see the data-relational guide).

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
constructor resolution, it calls `resolve_by_name("name", expected_type=T)` instead of
`resolve(T)`. This is handled in the `Container._resolve_param()` method, which inspects
the `Annotated` args for any `Qualifier` metadata. The named bean must be assignable to
`T` — see [@Qualifier type verification](#qualifier-type-verification) below.

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
2. It finds the class attributes (across the MRO) that hold an `Autowired()` or `Value()`
   instance. A class without any (every third-party `@bean` product, such as SQLAlchemy's
   `AsyncSession`) is left alone: no annotation is evaluated.
3. For each such field it resolves **that field's annotation only**, in the module of the class
   that declares it (the result is cached per field, so a transient bean does not pay for it on
   every creation):
   - A `Value()` field needs no annotation: its expression decides.
   - If `qualifier` is set, resolve by name via `resolve_by_name()` (the annotation, when there is
     one, must match the bean).
   - Otherwise the annotation is resolved like a constructor parameter: a class,
     `Annotated[T, Qualifier("name")]`, `T | None`, `list[T]`, `Provider[T]` and generics all work.
   - If resolution fails and `required=False`, set the field to `None`.
4. The resolved value is injected via `setattr()`.

An annotation elsewhere in the class that cannot be resolved at runtime (a name imported under
`TYPE_CHECKING`) no longer matters. If the type of a **required** `Autowired` field cannot be known
(its annotation cannot be resolved, or it has no annotation and no `qualifier`), the creation fails
with a `BeanCreationException` naming the field; an optional one is set to `None` with a warning.
Through 26.09.07 any unresolvable annotation made the container log one warning and skip every
`Autowired` field of the class, which left required fields holding the `Autowired` sentinel (and
every transient `AsyncSession` logged that warning).

The annotations are read through the annotation API of the running Python, so field injection works
the same with and without `from __future__ import annotations`, including on Python 3.14, where a
class keeps no `__annotations__` in its dictionary (PEP 649/749).

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

The inner type of an Optional is resolved like any other parameter, so generics, `list[T]`,
`Provider[T]` and `Annotated[T, Qualifier("name")]` work inside it:
`async_sessionmaker[AsyncSession] | None`, `Provider[Job] | None` and `Repository[User, int] | None`
receive the bean (through 26.09.07 they always received `None`), and `None` when no candidate exists.

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
   their `@bean` factory method outputs. A user `@bean` whose parameters are not registered
   yet (typically a type an auto-configuration provides: `async_sessionmaker`, `EventPublisher`,
   a client pool) is **deferred** rather than failed: its declared return type is claimed now,
   so `@conditional_on_missing_bean` in step 2b still sees it, and the factory runs in step 2d.
   Parameters are resolved before the factory runs, so a deferred factory never starts a body it
   cannot finish. It runs once: a bean that resolves the claim in the meantime builds the product
   through it, and step 2d reuses that product (the claim is found by its class and bean name).
   The declared type is the class the hint names: `-> Port | None` claims `Port`, a parametrized
   hint claims its origin class (`-> async_sessionmaker[AsyncSession]` claims `async_sessionmaker`,
   so the auto-configured session factory backs off), a builtin or abstract collection
   (`list[X]`, `Callable[[str], str]`) or a two-class union claims nothing, and a factory that
   answers `None` registers nothing. The claim is made unless a **singleton** of the class is
   registered already: beside a request- or refresh-scoped bean of the class, whichever was
   registered first, the override is still seen (and with two such beans, mark the one to inject
   by type `primary=True`).
2b. **Evaluate conditions (pass 2)** -- removes beans that fail bean-dependent conditions
    (`@conditional_on_bean`, `@conditional_on_missing_bean`).
2c. **Process `@auto_configuration` classes** -- resolves auto-configuration `@bean` methods
    after user beans are visible. Each auto-configuration class uses `@conditional_on_class`,
    `@conditional_on_property`, and `@conditional_on_missing_bean` to guard its beans,
    so user-provided beans always take precedence. An auto-configuration `@bean` is never
    deferred: it is processed last, so a dependency it cannot resolve is a real error.
2d. **Complete deferred user `@bean` methods** -- every factory parked in step 2 is called now
    that the framework's beans exist. The pass repeats while it makes progress (deferred
    factories may depend on each other); a dependency nobody registers raises the same
    `NoSuchBeanError`, naming the configuration, the method and the parameter, that an eager
    failure raised before.
2e. **Start the lifecycle beans the `@bean` methods produced** -- every singleton whose class
    defines `start()` and `stop()` (cache adapters, message brokers, HTTP clients, the schema
    initializer, a user relay). They start in ascending **phase** and, within a phase, in
    **creation order**, so a bean starts after the beans it depends on: a user lifecycle bean
    that takes the session factory starts after `ddl-auto` and the migrations. A bean registered
    under several types (a `@bean` declared as a port) is one instance and starts once. Failures
    here raise `BeanCreationException` for fast feedback.
3. **Auto-discover `BeanPostProcessor` implementations** from registered beans.
3b. **Bind `@config_properties` beans** -- sets a factory on each `@config_properties`
    registration so instances are produced by `Config.bind()` and injectable by type.
4. **Eagerly resolve all singletons** -- sorted by `@order` value.
5. **Run post-processors and lifecycle hooks** -- for each resolved bean, and for each
   non-singleton bean created so far (a transient repository injected into a singleton):
   - `BeanPostProcessor.before_init()`
   - `@post_construct` methods
   - `BeanPostProcessor.after_init()`

   From this step on, every bean the container creates goes through the same pipeline when it is
   created: a `@lazy` singleton first resolved by a `@post_construct`, an event listener or a
   runner, and every `TRANSIENT`, `REQUEST`, `SESSION` or custom-scoped bean.
5b. **Start the remaining lifecycle beans** -- the ones created since step 2e (a scanned
    `@component` or `@service` with `start()`/`stop()`), in the same order. They used to be
    neither started nor stopped.
6. **Wire decorator-based beans** -- connects `@app_event_listener`, `@message_listener`,
   CQRS handlers, `@scheduled` methods, and `@async_method` to their targets.
7. **Publish lifecycle events** -- `ContextRefreshedEvent`, then `ApplicationReadyEvent`.

A start that fails at any step stops the lifecycle beans it had started, highest phase first and in
reverse start order within a phase (as `stop()` does, and as Spring does when a refresh fails), then
raises its failure as `BeanCreationException`. A bean that fails to stop then is logged
(`adapter_stop_failed`) and does not replace that failure. `stop()` still releases the rest of the
failed run, and does not stop those beans again. Through 26.09.07 they kept running until the caller
called `stop()`.

#### Lifecycle phases

A lifecycle bean's phase is its `phase` attribute or property when it declares an `int` one
(Spring's `SmartLifecycle.getPhase()`, see `pyfly.kernel.lifecycle`). Otherwise a bean that takes
subscriptions (its class defines `subscribe`: an event bus, a message broker) is in
`CONSUMER_PHASE` and every other bean in `DEFAULT_PHASE` (0). Lower phases start earlier and stop
later. The start order holds within one start pass: the lifecycle beans the `@bean` methods produced
start at step 2e and the ones created later (a scanned `@component`) at step 5b, so a consumer
`@bean` starts before a scanned default-phase lifecycle bean. The stop order covers every started
bean. The framework's schedulers, pollers and consumers declare `CONSUMER_PHASE`, among them
`OrchestrationScheduler`, `RecoveryService`, `WorkflowRuns`, `TaskScheduler`, `ProjectionRunner`, the
outbox relays (`TransactionalOutbox`, `OutboxRelay`, `TransactionalEventPublisher`, `DatabaseEventBus`)
and the broker buses: they stop before any `@pre_destroy`, and the outbox relay stops before the
publisher it relays through (a consumer created before it). A poller of your own that dispatches work
into other beans should declare it too.

```python
from pyfly.kernel.lifecycle import CONSUMER_PHASE

class OutboxRelay:
    phase = CONSUMER_PHASE    # starts last in its start pass, stops first (with the consumers)

    async def start(self) -> None: ...
    async def stop(self) -> None: ...
```

### The stop() Lifecycle

When `ApplicationContext.stop()` is called (each step bounded per bean by
`pyfly.context.shutdown-timeout`, 30 s by default):

1. **`ContextClosedEvent` is published**, while every bean still works (Spring publishes it first
   too).
2. **Drain** -- background tasks are cancelled, the `TaskScheduler` stops (it waits for the jobs in
   flight), and the lifecycle beans of `CONSUMER_PHASE` and above stop, so nothing dispatches new
   work into the beans about to be destroyed.
3. **Destroy** -- from here on the container builds no **singleton**
   (`BeanCreationNotAllowedError`), while transient and scoped beans are still built (Spring's
   "singletons currently in destruction"). Every singleton gets its `@pre_destroy`, each bean
   **before the beans it depends on** (reverse creation order), so a `@pre_destroy` that writes
   through a `Provider[AsyncSession]` or a proxied refresh-scoped datasource succeeds. Then the
   instances the custom scopes hold (refresh-scoped beans) are destroyed, after the singletons that
   used them.
4. **Stop the other lifecycle beans** -- highest phase first, in reverse start order within a
   phase. They own the resources the destroyed beans used (clients, the `create-drop` schema). A
   scoped instance one of them builds while stopping is destroyed right after; from then on no
   scoped instance is built either, since nothing would destroy it. Then each singleton `@bean`
   product gets its declared `destroy_method` (in the order of step 3): it releases what a lifecycle
   bean may still have used in its `stop()`, which would otherwise reopen a disposed engine's pool.
5. **Dispose the resource registries** -- every bean that implements
   `pyfly.kernel.lifecycle.ResourceRegistry` (`async dispose_all()`): the `DataSourceRegistry`
   closes every engine, **last**. A connection still in use then (a request or a readiness probe in
   flight) finishes its work and is closed when it is returned.
6. **Release** -- the singletons this run built are released, and everything the run added (the
   started lifecycle beans, the post-processors discovered from beans, the event listeners it
   wired, the post-create hook) is forgotten, so a later `start()` is a cold start. From here on
   the container builds no bean of any scope until the context starts again.

Through 26.09.07 the lifecycle beans stopped first, in reverse registration order, then
`@pre_destroy` ran and `ContextClosedEvent` came last: the primary engine was disposed before the
consumers, the user lifecycle beans and every `@pre_destroy`, and their writes reconnected through
a pool nobody disposed. A restart also restarted the previous run's adapters and delivered each
event to the previous run's listeners.

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
supports both sync and async methods. Beans are destroyed in reverse creation order, so each bean
is destroyed before the beans it depends on, and the datasources are still open: a `@pre_destroy`
that flushes rows to the database succeeds (the registry closes after every `@pre_destroy`), also
through a `Provider[AsyncSession]` or a proxied refresh-scoped datasource. A refresh-scoped bean's
`@pre_destroy` also runs when a refresh evicts it. The decorator sets `__pyfly_pre_destroy__ = True`
on the method.

---

## BeanPostProcessor

`BeanPostProcessor` is a `Protocol` (runtime-checkable) that lets you hook into the bean
creation lifecycle. Implementations are called for **every** bean the `ApplicationContext`
creates: eager and lazy singletons, and every `TRANSIENT`, `REQUEST`, `SESSION` and custom-scoped
instance (through 26.09.07 only singletons, so a transient or request-scoped repository kept its
derived-query stubs). Post-processors run in `@order`; the repository post-processors declare a
high precedence so that AOP advice wraps the compiled derived and `@query` methods.

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

### Singletons-Only Post-Processors

A post-processor that hands the beans it processes to something that outlives them (a registry, an
event bus, a scheduler) declares `singletons_only = True`: the context then gives it singletons only.
Running it on every instance would register a `TRANSIENT` bean again at each resolution and keep a
`REQUEST` or refresh-scoped one registered after its scope ended (Spring's
`ApplicationListenerDetector` registers singleton listeners only for the same reason). For each other
instance it skipped, the context calls its optional `non_singleton_skipped(bean, bean_name, scope)`,
so it can say why it ignores the bean.

```python
@component
class ListenerRegistrar:
    singletons_only = True

    def before_init(self, bean, bean_name: str):
        return bean

    def after_init(self, bean, bean_name: str):
        if hasattr(bean, "on_message"):
            broker.register(bean)          # kept for the broker's whole life
        return bean

    def non_singleton_skipped(self, bean, bean_name: str, scope) -> None:
        if hasattr(bean, "on_message"):
            logger.warning("listener_not_singleton", bean=bean_name)
```

### Built-in Post-Processors

PyFly's own modules use `BeanPostProcessor` extensively:

| Post-Processor | Module | Purpose |
|---|---|---|
| `AspectBeanPostProcessor` | `pyfly.aop` | Weaves AOP advice into target beans (order 0). |
| `RepositoryBeanPostProcessor` | `pyfly.data` | Wires query methods onto repository beans; `HIGHEST_PRECEDENCE + 100`, so it runs before AOP weaving (the Mongo one too). |
| `HttpClientBeanPostProcessor` | `pyfly.client` | Generates HTTP client method implementations. |
| `DataSourceSpiRegistrar` | `pyfly.data.relational` | Registers after-begin customizers and credentials providers with the `DataSourceRegistry`; singletons only. |

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

A bean of any scope counts, as in Spring. `singletons_only=True` counts only singletons: a
`TRANSIENT`, `REQUEST`, `SESSION` or custom-scoped (`"refresh"`) bean of the type is an
additional bean, and the default is registered beside it.

```python
@auto_configuration
class ReportingAutoConfiguration:
    @bean(primary=True)
    @conditional_on_missing_bean(ReportingClient, singletons_only=True)
    def reporting_client(self, config: Config) -> ReportingClient:
        # replaced by a singleton ReportingClient bean; kept beside a request-scoped one
        return ReportingClient(str(config.get("reporting.url")))
```

Mark such a default `primary=True` as above: when a scoped bean of the type exists beside it, an
injection by type is ambiguous without a primary, and the default is the one the rest of the
application means.

The data auto-configurations follow this pattern for the beans an application replaces:
`AsyncEngine` (`async_engine`), `async_sessionmaker` (`async_session_factory`),
`RoutingSessionFactory`, `DataSourceRegistry` and the Mongo `AsyncMongoClient`. A **singleton**
bean of one of these types replaces the application's **primary**. A request- or refresh-scoped
one is a second database: the auto-configured primary stays, and it is the `@primary` candidate,
so an injection by type (`AsyncEngine`, `async_sessionmaker[AsyncSession]`) receives the primary.
Inject the scoped bean by name (`Annotated[AsyncEngine, Qualifier("reporting_engine")]`). Through
26.09.07 a user engine or session factory, even `primary=True`, was shadowed by the framework's.

### @conditional_on_single_candidate

```python
from pyfly.context import conditional_on_single_candidate

@conditional_on_single_candidate(DataSource)
@service
class DefaultTransactionManager:
    """Only activate when there is exactly one DataSource candidate."""
    ...
```

Mirrors Spring Boot's `@ConditionalOnSingleCandidate`: the bean is registered when exactly
**one** bean assignable to `bean_type` exists, **or** when several exist but exactly one is
marked `@primary`. Counting is purely type/registration-based — it never resolves or
instantiates a candidate bean. Like `@conditional_on_bean`, it is bean-dependent and so is
evaluated in **pass 2**.

### @conditional_on_web_application

```python
from pyfly.context import conditional_on_web_application

@conditional_on_web_application()
@service
class WebOnlyMetrics:
    """Only activate when a web stack (Starlette or FastAPI) is present."""
    ...
```

Mirrors Spring Boot's `@ConditionalOnWebApplication`. The bean is registered only when
`starlette` or `fastapi` is importable. Note the trailing `()` — this is a factory that
returns the decorator. It checks no beans, so it is evaluated in **pass 1**.

### @conditional_on_resource

```python
from pyfly.context import conditional_on_resource

@conditional_on_resource("/etc/myapp/license.key")
@service
class LicensedFeature:
    """Only activate when the file at the given path exists."""
    ...
```

Mirrors Spring Boot's `@ConditionalOnResource`. The bean is registered only when the
filesystem path passed to the decorator exists (`os.path.exists`). Evaluated in **pass 1**.

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
| **Pass 1** | `@conditional_on_property`, `@conditional_on_class`, `@conditional_on_expression`, `@conditional_on_web_application`, `@conditional_on_resource`, stereotype `condition` callable | Before any `@configuration` classes are processed. |
| **Pass 2** | `@conditional_on_bean`, `@conditional_on_missing_bean`, `@conditional_on_single_candidate` | After user `@configuration` classes are processed but before `@auto_configuration`. |

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
| `ContextClosedEvent` | The `ApplicationContext` is shutting down: the first step of `stop()`, published while every bean still works (before the drain and every `@pre_destroy`). |

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

## Spring-parity DI features (v26.06.22+)

This release wave brought the container much closer to Spring's injection model. Every
feature below is resolved by the same `Container._resolve_param()` machinery used for
ordinary constructor injection, so they compose freely with `Optional[T]`, `list[T]`,
`@primary`, and qualifiers.

### Constructor-parameter @Value

Beyond field injection (`field: int = Value("${key}")`), you can now inject configuration
directly into **constructor parameters** by annotating the parameter type with `Value`.
The value is resolved from the application `Config` and **coerced to the declared type**.

```python
from typing import Annotated
from pyfly.container import service
from pyfly.core.value import Value

@service
class HttpServer:
    def __init__(
        self,
        port: Annotated[int, Value("${app.port:8080}")],
        name: Annotated[str, Value("${app.name}")],
    ) -> None:
        self.port = port   # int, coerced from config (or the "8080" default)
        self.name = name
```

`Value` lives in `pyfly.core.value` (not `pyfly.container`). The expression forms are:

| Form | Meaning |
|---|---|
| `${key}` | Resolve from `Config`; raise `KeyError` if missing and no default. |
| `${key:default}` | Resolve from `Config`; use `default` if the key is absent. |
| `#{ ... }` | Evaluate a SpEL-lite expression (see below). |
| `literal` | Return the string as-is when there is no `${}`/`#{}` wrapper. |

**Type coercion** is best-effort and driven by the parameter's base type: `bool` accepts
`true/1/yes/on` (case-insensitive); `int`, `float`, and `str` are constructed from the
resolved value; everything else is passed through unchanged. So a `${missing.port:8080}`
default (a string) injected into an `Annotated[int, ...]` parameter arrives as the integer
`8080`.

> `@Value` requires the `Config` bean to be registered. `ApplicationContext` registers
> `Config` automatically during construction, so this is the normal case.

### @Value SpEL expressions

The `#{ ... }` form evaluates a small, **safe** subset of Spring's SpEL — arithmetic,
comparison, boolean (`and`/`or`/`not`), the ternary (`a if c else b`), literals,
lists/tuples, `${key:default}` placeholder substitution, and an `env` mapping for
environment variables. It is parsed with `ast` and evaluated against a node whitelist, so
it can never execute arbitrary code (`eval` is never used). There is no attribute access,
no method calls, and no object navigation.

```python
from typing import Annotated
from pyfly.container import service
from pyfly.core.value import Value

@service
class PoolConfig:
    def __init__(
        self,
        # arithmetic over a config placeholder
        max_conns: Annotated[int, Value("#{ ${app.workers:4} * 8 }")],
        # ternary + comparison
        verbose: Annotated[bool, Value("#{ ${app.level:info} == 'debug' }")],
        # environment lookup via the `env` mapping
        home: Annotated[str, Value("#{ env['HOME'] }")],
    ) -> None:
        self.max_conns = max_conns
        self.verbose = verbose
        self.home = home
```

The SpEL evaluator is in `pyfly.core.expression` (`evaluate()` / `is_expression()`); both
the field-level and constructor-parameter `@Value` paths route through it when the
expression starts with `#{` and ends with `}`.

### Provider[T] — deferred resolution

Inject `Provider[T]` instead of `T` to **defer** resolution. Each `.get()` call resolves a
fresh bean from the container, so a singleton can obtain new `TRANSIENT` instances, and
expensive or construction-time-cyclic beans can be deferred until first use. This is the
Spring `ObjectFactory` / `Provider` equivalent.

```python
from pyfly.container import Provider, service, component, Scope

@component(scope=Scope.TRANSIENT)
class Job:
    ...

@service
class Worker:
    def __init__(self, jobs: Provider[Job]) -> None:
        self._jobs = jobs

    def run(self) -> None:
        job = self._jobs.get()   # a fresh Job each call (Job is TRANSIENT)
        same = self._jobs()      # __call__ is shorthand for .get()
```

`Provider` is exported from `pyfly.container`. It exposes `.get()` and is also callable
(`provider()` is equivalent to `provider.get()`). `T` can be anything a constructor parameter can
be: `Provider[async_sessionmaker[AsyncSession]]` resolves the `async_sessionmaker` bean, and
`Provider[X | None]` answers `None` when there is no `X` (through 26.09.07 a parametrized `T` raised
`NoSuchBeanError` even when the bean existed).

### Map injection — dict[str, T]

Declare a parameter as `dict[str, T]` to receive a `{bean-name: bean}` mapping of every
**named** bean assignable to `T` — Spring's `Map<String, T>` injection.

```python
from pyfly.container import service

@service
class Dispatcher:
    def __init__(self, handlers: dict[str, MessageHandler]) -> None:
        self.handlers = handlers   # {"email": EmailHandler(), "sms": SmsHandler(), ...}

    def dispatch(self, channel: str, msg: str) -> None:
        self.handlers[channel].handle(msg)
```

The map keys are the registered bean **names**, so only beans that were registered with a
name (e.g. via a stereotype `name=`, `@bean(name=...)`, or `container.register(..., name=...)`)
participate. Assignability is checked with a tolerant `isinstance` that accepts
non-runtime-checkable protocols and subscripted generics.

### @lazy beans

`@lazy` marks a bean so it is **not** eagerly created during `ApplicationContext.start()`;
it is constructed on first resolution instead. Useful for expensive beans that may never be
used, or to avoid heavy work at boot. This is the Spring `@Lazy` equivalent.

```python
from pyfly.container import lazy, service

@lazy
@service
class ReportGenerator:
    def __init__(self) -> None:
        # expensive setup that runs only when first resolved, not at startup
        self._templates = load_all_report_templates()
```

`@lazy` is exported from `pyfly.container` and simply sets `__pyfly_lazy__ = True` on the
class. The bean is still a normal singleton (or whatever its scope is) once resolved — only
its *creation* is deferred.

A lifecycle bean (one that defines `start()` and `stop()`) is started only if it exists when the
context starts its lifecycle beans. A `@lazy` one first resolved after `start()` is neither started
nor stopped (its `start()` cannot be awaited in a synchronous resolution), and the context logs a
`lifecycle_bean_created_after_start` warning naming it. Drop `@lazy` from a lifecycle bean.

### Generics-aware injection

When you depend on a parametrized generic interface such as `Repository[User, UUID]`, the
container resolves it to the registered implementation whose generic bases carry the
matching concrete type arguments — Spring's generic-aware injection.

```python
from typing import Generic, TypeVar
from uuid import UUID
from pyfly.container import repository, service

T = TypeVar("T")
ID = TypeVar("ID")

class Repository(Generic[T, ID]):
    ...

@repository
class UserRepository(Repository[User, UUID]):
    ...

@repository
class OrderRepository(Repository[Order, UUID]):
    ...

@service
class UserService:
    def __init__(self, repo: Repository[User, UUID]) -> None:
        self.repo = repo   # the UserRepository, not OrderRepository
```

Resolution rules for a parametrized generic `Origin[A, B]`:

1. The container collects every registered subclass of `Origin` (the "family"). If there
   are none, it falls back to resolving the bare `Origin` normally.
2. From the family, it keeps the impls whose generic bases include **all** the requested
   concrete type args.
3. Exactly one match → that bean. Multiple matches → the `@primary` one (or
   `NoUniqueBeanError`). No match (but the family is non-empty) → `NoSuchBeanError`.

### @bean(primary=..., profile=...)

`@bean` factory methods now accept `primary` and `profile`, mirroring `@Bean @Primary` and
`@Bean @Profile`.

```python
from pyfly.container import bean, configuration

@configuration
class GreetingConfig:

    @bean(primary=True)
    def english(self) -> Greeter:
        return EnglishGreeter()

    @bean
    def french(self) -> Greeter:
        return FrenchGreeter()

    @bean(profile="prod")
    def metrics(self) -> MetricsSink:
        return PrometheusSink()
```

- `primary=True` makes that factory the chosen candidate when several beans satisfy the
  same interface — so `get_bean(Greeter)` returns the `EnglishGreeter` above. (At the
  registration level this sets `Registration.primary`; class-level `@primary` instead sets
  `__pyfly_primary__`. Both are honored during interface resolution.)
- `profile="prod"` only creates the bean when the `prod` profile is active; otherwise the
  bean is skipped and resolving it raises `NoSuchBeanError`. The expression supports the
  same negation/comma syntax as stereotype `profile=`.

The full `@bean` signature is now:

| Parameter | Type | Default | Description |
|---|---|---|---|
| `name` | `str` | `""` | Bean name (defaults to the method name). |
| `scope` | `Scope \| str` | `Scope.SINGLETON` | Lifecycle scope of the produced bean: a `Scope`, or a custom scope name such as `"refresh"`. |
| `primary` | `bool` | `False` | Mark this the primary candidate for its interface. |
| `profile` | `str` | `""` | Only create the bean when the profile expression matches. |

### @Qualifier type verification

`@Qualifier` now verifies that the named bean is **assignable to the declared type** before
injecting it. A mistyped qualifier name that points at an incompatible bean raises
`NoSuchBeanError` instead of silently injecting the wrong object.

```python
from typing import Annotated
from pyfly.container import Qualifier, service

@service
class ReportService:
    def __init__(
        self,
        # if the bean named "analytics_db" is not a DataSource, this raises
        db: Annotated[DataSource, Qualifier("analytics_db")],
    ) -> None:
        self.db = db
```

Internally, qualified resolution calls `resolve_by_name(name, expected_type=base_type)`.
When the named bean is not assignable to `base_type`, a `NoSuchBeanError` is raised whose
message explains the actual vs. expected type. Protocols and subscripted generics that
cannot be `isinstance`-checked are accepted (treated as assignable), so this never breaks
legitimate protocol-typed qualifiers. The same check guards `Autowired(qualifier="...")`
field injection.

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
