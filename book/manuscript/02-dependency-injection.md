<span class="eyebrow">Chapter 2</span>

# Dependency Injection & the Application Context {.chtitle}

::: figure art/openers/ch02.svg | &nbsp;

In the previous chapter you gave Lumen its application entry point and watched the container start with zero beans. Now you will declare Lumen's first real components — a `Wallet` model, a `WalletRepository` port with an in-memory implementation, and a `WalletService` that depends on both the repository and an event publisher — and let PyFly wire them together from nothing but type hints. No factories, no manual `new`, no glue code.

Before a single line of Lumen code appears, it is worth pausing on *why* that matters. In a conventional Python project you would write something like `service = WalletService(repo=InMemoryWalletRepository(), events=InMemoryEventBus())` somewhere near the startup path. That one line seems harmless, but it makes every decision — which repository class, which event bus — permanent at the point where the objects are created. Swap the repository for a Postgres adapter and you have to find every construction site. Add a test double and you need to restructure the wiring. Dependency injection inverts this relationship: classes *declare* what they need, and the container *decides* what to provide. The result is code that is open to extension but closed to modification — the `WalletService` you write today will accept a production database adapter in Part II without a single change to its source.

---

## Stereotypes: declaring your beans

A **bean** is any object that the container creates, wires, and manages. The container cannot manage what it does not know about, so your first task is to make your classes visible to it. You do that by applying a **stereotype decorator** — a thin annotation that registers the class with the container and signals its architectural role.

PyFly ships five stereotypes:

| Decorator | Meaning |
|---|---|
| `@service` | Business-logic layer: domain operations, use-case orchestration. |
| `@component` | Generic managed bean with no specific architectural role. |
| `@repository` | Data-access layer: databases, external storage, ports. |
| `@configuration` | Configuration class that can contain `@bean` factory methods. |
| `@rest_controller` | HTTP layer: handles requests and returns JSON responses. |

Semantics aside, all five stereotypes are **container-equivalent**. They are all produced by the same internal `_make_stereotype()` factory, and they all accept the same optional keyword arguments (`name`, `scope`, `profile`, `condition`). The only meaningful differences are the `__pyfly_stereotype__` label — used by the web layer to find controllers and by the context to find `@configuration` classes — and the architectural clarity they bring to readers of your code. Choosing `@repository` over `@component` costs nothing technically, but it tells every future reader — and every tooling plugin — exactly what the class is for.

Both bare and parenthesised forms work:

```python
@service              # bare — all defaults
class SimpleService:
    pass

@service(name="wallet_svc")   # with keyword args
class NamedService:
    pass
```

For Lumen, add two new files under `src/lumen/`. First, the `WalletRepository` port and its in-memory implementation:

::: listing lumen/wallet_repository.py | Listing 2.1 — The repository port and its in-memory adapter
from typing import Protocol, runtime_checkable
from pyfly.container import repository, primary


@runtime_checkable
class WalletRepository(Protocol):
    async def find_by_id(self, wallet_id: str) -> dict | None:
        ...

    async def save(self, wallet: dict) -> None:
        ...

    async def find_by_owner(self, owner_id: str) -> list[dict]:
        ...


@primary
@repository
class InMemoryWalletRepository:
    """Simple in-memory repository — swap for a real adapter later."""

    def __init__(self) -> None:
        self._store: dict[str, dict] = {}

    async def find_by_id(self, wallet_id: str) -> dict | None:
        return self._store.get(wallet_id)

    async def save(self, wallet: dict) -> None:
        self._store[wallet["id"]] = wallet

    async def find_by_owner(self, owner_id: str) -> list[dict]:
        return [w for w in self._store.values() if w.get("owner_id") == owner_id]
:::

**How it works.** `WalletRepository` is a plain Python `Protocol` — not a PyFly construct. Marking it `@runtime_checkable` lets the container verify at registration time that an implementation actually satisfies the interface, rather than discovering the mismatch at the first method call. The implementation, `InMemoryWalletRepository`, carries two decorators: `@repository` tells the container to manage it, and `@primary` tells it to prefer this class when more than one implementation of `WalletRepository` is registered. The internal `_store` dictionary is private; callers only ever see the `WalletRepository` protocol. When you add a Postgres adapter in Part II, you swap the `@primary` annotation — nothing else changes.

`@primary` marks `InMemoryWalletRepository` as the default candidate when multiple implementations are bound to `WalletRepository`. You will add a database-backed one in Part II and switch between them without touching `WalletService`.

!!! spring "Spring parity"
    `@service`, `@component`, `@repository`, and `@configuration` map directly to Spring's `@Service`, `@Component`, `@Repository`, and `@Configuration`. The stereotype labels carry the same architectural intent and are used by the framework for the same purposes: `@repository` will eventually get exception-translation behaviour; `@configuration` triggers `@bean` scanning. `@rest_controller` mirrors `@RestController`.

---

## Constructor injection

With the repository declared, you need a service that uses it — and that is where the container's most important trick becomes visible. The most important thing to understand about PyFly's DI system is that you never call constructors yourself. You declare what a class *needs* as `__init__` parameters with type annotations, and the container fills them in automatically. This is **constructor injection**, and it is the recommended approach for all mandatory dependencies.

The mental model is simple: treat `__init__` parameters as a wishlist. You list the types you need; the container delivers the right instances. If a dependency does not exist at startup, you get a clear `NoSuchBeanError` immediately — not a cryptic `AttributeError` three call frames deep at runtime.

When the container resolves `WalletService`, it inspects `typing.get_type_hints(WalletService.__init__, include_extras=True)`, discovers that the constructor needs a `WalletRepository` and an `EventPublisher`, resolves each of those recursively, and injects them before returning the fully-wired instance:

::: listing lumen/wallet_service.py | Listing 2.2 — WalletService with constructor injection
import uuid
from pyfly.container import service
from pyfly.eda import EventPublisher

from lumen.wallet_repository import WalletRepository


@service
class WalletService:
    def __init__(
        self,
        repo: WalletRepository,
        events: EventPublisher,
    ) -> None:
        self.repo = repo
        self.events = events

    async def create_wallet(self, owner_id: str) -> dict:
        wallet = {"id": str(uuid.uuid4()), "owner_id": owner_id, "balance": 0}
        await self.repo.save(wallet)
        await self.events.publish(
            {"type": "wallet.created", "wallet_id": wallet["id"]}
        )
        return wallet

    async def get_wallet(self, wallet_id: str) -> dict | None:
        return await self.repo.find_by_id(wallet_id)

    async def credit(self, wallet_id: str, amount: float) -> dict | None:
        wallet = await self.repo.find_by_id(wallet_id)
        if wallet is None:
            return None
        wallet = {**wallet, "balance": wallet["balance"] + amount}
        await self.repo.save(wallet)
        await self.events.publish(
            {"type": "wallet.credited", "wallet_id": wallet_id, "amount": amount}
        )
        return wallet
:::

**How it works.** `@service` registers `WalletService` with the container. The `__init__` signature is the entire wiring specification: `repo: WalletRepository` is an interface type, so the container resolves the `@primary` implementation (`InMemoryWalletRepository`); `events: EventPublisher` resolves the bean produced by `LumenInfraConfig` (which you will define in Listing 2.3). `WalletService` itself imports neither concrete class — it knows only about the interfaces.

The business methods follow the same pattern throughout: read through the repository, compute the new state as an immutable dict spread (`{**wallet, "balance": ...}`), persist via the repository, then publish a domain event. That sequencing is deliberate — the wallet is saved *before* the event is published, so a listener that queries the repository will always find the updated record.

The container resolves dependencies **recursively**. When it constructs `WalletService` it will also construct `InMemoryWalletRepository` (because that is the `@primary` implementation bound to `WalletRepository`) and the `EventPublisher` adapter — neither of which `WalletService` needs to know about.

::: figure art/figures/02-di.svg | Figure 2.1 — The container injects dependencies from type hints.

!!! spring "Spring parity"
    Constructor injection in PyFly is functionally identical to Spring's `@Autowired` constructor injection. In modern Spring you do not even write `@Autowired` — the framework infers injection from the single constructor, just as PyFly reads `__init__` type hints. The mental model is the same: declare what you need, let the container provide it.

!!! tip "Tip"
    Prefer constructor injection for mandatory dependencies. It makes them visible in the class signature, lets you write plain-Python unit tests without a container (`service = WalletService(repo=MockRepo(), events=MockEvents())`), and prevents accidental missing-dependency bugs at startup rather than at runtime.

---

## The Container and the ApplicationContext

Understanding the two-layer architecture of PyFly's DI system will save you considerable debugging time. The layers are cleanly separated: one handles object graphs, the other handles the full application lifecycle. Conflating them is a common source of confusion, so it is worth being explicit about where each responsibility lives.

PyFly's DI system has two layers.

**`Container`** (from `pyfly.container`) is the low-level DI engine. It stores `Registration` objects, resolves types by constructor hints, manages scopes, applies `@primary` disambiguation, and handles `Qualifier`-based named lookups. It has no lifecycle awareness — it is a pure "give me a `T`" machine.

**`ApplicationContext`** (from `pyfly.context`) is the high-level orchestrator. It wraps `Container` and adds the startup sequence: profile filtering, condition evaluation, `@configuration`/`@bean` processing, `BeanPostProcessor` weaving, `@post_construct` / `@pre_destroy` hooks, event publishing, and auto-configuration. You interact with the `ApplicationContext` in application code; the raw `Container` is mostly an implementation detail (accessible via `ctx.container` as an escape hatch).

Think of it this way: `Container` is the factory floor — it knows how to build things. `ApplicationContext` is the production manager — it decides what gets built, in what order, and what happens when the factory opens or closes.

### Resolution rules

When the container needs to resolve a type `T`, it follows this order:

1. **Direct registration** — if `T` is registered directly, resolve it.
2. **Interface binding** — if `T` is a `Protocol` or ABC with bound implementations, and exactly one is bound, resolve that implementation.
3. **`@primary` disambiguation** — if multiple implementations are bound, the one decorated with `@primary` wins.
4. **Error** — `NoSuchBeanError` if nothing matches; `NoUniqueBeanError` if multiple candidates exist with no `@primary`.

These rules run in strict priority order. Step 4 is deliberately loud: a missing or ambiguous dependency is a configuration error, and surfacing it at startup rather than burying it in a runtime traceback is one of the key guarantees the container provides.

### @primary

`@primary` resolves ambiguity when several beans satisfy the same interface. Place it on the implementation you want to be the default:

```python
from pyfly.container import repository, primary


@primary
@repository
class InMemoryWalletRepository:
    ...
```

Without `@primary`, resolving `WalletRepository` when two implementations are registered raises:

```
NoUniqueBeanError: Multiple beans of type 'WalletRepository' found but none is marked @primary
  Candidates: ['InMemoryWalletRepository', 'PostgresWalletRepository']
```

This error message is intentionally informative: it names every competing candidate so you can make an explicit decision rather than guessing which one the container picked.

### @order

The container initializes singleton beans eagerly during startup. By default the order is undefined, but some beans genuinely must be ready before others — a security filter that must wrap every inbound request, or a schema migrator that must run before any repository is touched. `@order` gives you explicit control.

`@order` controls initialization order. Lower values are resolved first during the eager startup pass. The constants `HIGHEST_PRECEDENCE` (`-(2**31)`) and `LOWEST_PRECEDENCE` (`2**31 - 1`) mark the extremes:

```python
from pyfly.container import order, HIGHEST_PRECEDENCE, service


@order(HIGHEST_PRECEDENCE)
@service
class SecurityInitializer:
    """Must be ready before any other service."""
    ...
```

Ordering affects singleton resolution during startup, the sequence in which `BeanPostProcessor` instances run, and the ordering of `get_beans_of_type()` results.

### Qualifier — named bean resolution

Type-based injection covers most scenarios, but occasionally you genuinely need a particular *instance* rather than any satisfying implementation. The classic case is a configuration class that produces two beans of the same type — say, a primary and a read-replica database connection — where a downstream service needs to be specific about which one it receives.

When you need to select a specific bean by name — most common when `@bean` factory methods produce multiple instances of the same type — use `Annotated[T, Qualifier("name")]`:

```python
from typing import Annotated
from pyfly.container import Qualifier, service


@service
class ReportService:
    def __init__(
        self,
        db: Annotated[object, Qualifier("analytics_db")],
    ) -> None:
        self.db = db  # receives the bean named "analytics_db"
```

The container calls `resolve_by_name("analytics_db", expected_type=T)` and verifies assignability — a mistyped name pointing at the wrong type raises `NoSuchBeanError` with a clear message instead of silently injecting the wrong object.

---

## Bean factories: @configuration and @bean

Stereotype decorators work beautifully for your own classes, but not every dependency is a class you control. Third-party clients need constructor arguments that only become known at runtime. Pairs of related beans share configuration state. Some beans are best expressed as a single factory that makes several things at once. For all of these situations, PyFly provides the `@configuration` / `@bean` pattern — a way to write explicit factory code while still participating fully in the container's resolution and lifecycle machinery.

Some objects cannot be expressed as stereotype-decorated classes — perhaps you are wrapping a third-party client, or a single configuration class must produce multiple related beans. For those cases, PyFly provides the **`@configuration` / `@bean`** pattern.

A `@configuration` class acts as a factory. Its `@bean` methods are called during the startup sequence, and each method's return value is registered as a bean whose type is derived from the method's return annotation:

::: listing lumen/infra_config.py | Listing 2.3 — Producing an EventPublisher bean via @configuration
from pyfly.container import configuration, bean
from pyfly.eda import EventPublisher, InMemoryEventBus


@configuration
class LumenInfraConfig:
    """Wires infrastructure beans that require explicit construction."""

    @bean
    def event_publisher(self) -> EventPublisher:
        """In-memory event bus — replace with Kafka adapter in production."""
        return InMemoryEventBus()
:::

**How it works.** `@configuration` tells the context to scan `LumenInfraConfig` for `@bean` methods during startup — before any stereotype beans are constructed. The `event_publisher` method's return annotation, `EventPublisher`, is the key: the context reads it and registers the produced `InMemoryEventBus` instance *as* an `EventPublisher`, not as an `InMemoryEventBus`. That distinction matters — when `WalletService` later asks the container for an `EventPublisher`, it gets the `InMemoryEventBus` instance without knowing or caring about the concrete type.

Swapping to a Kafka adapter in production means replacing `InMemoryEventBus()` with `KafkaEventPublisher(settings.kafka_url)` in a single method. The rest of the codebase is untouched.

`@bean` methods can also declare constructor parameters; the container resolves them automatically:

```python
@configuration
class MessagingConfig:

    @bean
    def audited_publisher(self, base: EventPublisher) -> EventPublisher:
        """Wrap the base publisher with audit logging."""
        return AuditingEventPublisher(base)
```

### @bean parameters

| Parameter | Default | Description |
|---|---|---|
| `name` | method name | Bean name for named resolution. |
| `scope` | `Scope.SINGLETON` | Lifecycle scope of the produced bean. |
| `primary` | `False` | Mark this the primary candidate for its interface. |
| `profile` | `""` | Only create the bean when the profile expression matches. |

!!! note "Note"
    The return type annotation on a `@bean` method is **mandatory**. The context reads it to know which interface type to register the produced bean under. Omitting it will cause the bean to be unreachable by type.

---

## Scopes

Every bean has a scope that controls how long its instance lives. Getting scope right is less about performance and more about correctness: sharing a stateful object that was designed for single-use will produce race conditions; creating a new singleton on every resolution wastes resources and defeats caching. The `Scope` enum defines three values that cover the vast majority of real-world needs.

Every bean has a scope that controls how long its instance lives. The `Scope` enum defines three values:

**`Scope.SINGLETON`** (default) — a single instance is created on first resolution and reused forever. Singletons are instantiated eagerly during `ApplicationContext.start()`, sorted by `@order`. Almost all application beans should be singletons.

**`Scope.TRANSIENT`** — a fresh instance is created on every resolution. Use this for stateful, non-shareable objects:

::: listing lumen/contexts.py | Listing 2.4 — A transient bean for per-operation context
from pyfly.container import component, Scope


@component(scope=Scope.TRANSIENT)
class TransferContext:
    """Carries state for a single wallet transfer operation."""

    def __init__(self) -> None:
        self.steps: list[str] = []
        self.rolled_back: bool = False
:::

**How it works.** `TransferContext` is an accumulator: it collects the steps of a multi-hop transfer so that a saga can roll them back in reverse if anything fails. Sharing a single instance across concurrent requests would mix their state; `Scope.TRANSIENT` ensures every resolution yields a fresh, empty `TransferContext`. The container still manages the class — it will be injected, profiled, and potentially post-processed — but it will never be cached.

**`Scope.REQUEST`** — scoped to a single HTTP request. A new instance is created when a request arrives and discarded when it completes. Use this for web-layer beans that carry request-specific state, such as the current authenticated user.

```python
from pyfly.container import component, Scope


@component(scope=Scope.REQUEST)
class CurrentUser:
    user_id: str = ""
    roles: list[str] = []
```

A quick rule of thumb for choosing scope:

- **SINGLETON** — the bean is stateless, or its state is safe to share across all callers (connection pools, caches, service objects).
- **TRANSIENT** — the bean accumulates per-operation state that must not bleed between operations (sagas, builders, context carriers).
- **REQUEST** — the bean carries per-HTTP-request state that must be isolated between concurrent requests (authenticated user, request-scoped trace).

---

## Lifecycle and conditions

Constructing an object and wiring its dependencies is only half the story. Real infrastructure beans need to *do* something after they are built — reserve a thread pool, pre-load a cache, subscribe to a message queue — and they need to *undo* those actions cleanly when the application shuts down. PyFly gives you two hooks for this, plus a family of conditional decorators that let you decide whether a bean should exist at all based on runtime configuration.

### @post_construct and @pre_destroy

Once the container has constructed a bean and injected all its dependencies, you often need to perform one-time initialisation — opening a connection pool, warming a cache, registering a listener. Mark a method with `@post_construct` and the context will call it after construction is complete. Both synchronous and `async` methods are supported:

::: listing lumen/wallet_service.py | Listing 2.5 — Lifecycle hooks on WalletService
from pyfly.container import service
from pyfly.context import post_construct, pre_destroy
from pyfly.eda import EventPublisher
import structlog

from lumen.wallet_repository import WalletRepository

logger = structlog.get_logger()


@service
class WalletServiceWithLifecycle:
    def __init__(
        self,
        repo: WalletRepository,
        events: EventPublisher,
    ) -> None:
        self.repo = repo
        self.events = events

    @post_construct
    async def on_start(self) -> None:
        logger.info("wallet_service_ready")

    @pre_destroy
    async def on_stop(self) -> None:
        logger.info("wallet_service_shutting_down")
:::

**How it works.** `on_start` fires *after* the constructor returns and all injected dependencies have been set — so `self.repo` and `self.events` are fully initialised when the method runs. This makes it safe to issue repository queries, open connections, or publish an application event from inside `@post_construct`. `on_stop` mirrors it: called during `ApplicationContext.stop()`, with dependencies still intact, so the service can flush state, drain queues, or publish a shutdown event before the container discards it.

The `async` keyword works without any extra setup. The context calls `await on_start()` when it detects a coroutine function, and falls back to a direct call for synchronous methods.

`@pre_destroy` is the counterpart: called during `ApplicationContext.stop()` before the bean is discarded. Beans are destroyed in **reverse** initialization order, so if `WalletService` started after `InMemoryWalletRepository`, it will be stopped before it.

::: figure art/figures/02-lifecycle.svg | Figure 2.2 — A bean's lifecycle.

### Conditional beans

Conditions answer the question: *should this bean exist at all, given the current environment?* That turns out to be one of the most powerful abstractions in a framework. It is how you make the same codebase work in development (with cheap in-memory adapters), in CI (with testcontainers), and in production (with real infrastructure) — without `if` statements scattered through your service code.

Conditional decorators control whether a bean participates in the container at all. They are evaluated in a two-pass strategy during `ApplicationContext.start()`:

**Pass 1** (before user `@configuration` is processed) evaluates:
- `@conditional_on_property(key, having_value="...")` — the config key must exist and optionally match a value.
- `@conditional_on_class("module.name")` — the Python module must be importable.
- The `condition` callable on a stereotype decorator.

**Pass 2** (after user `@configuration` is processed) evaluates:
- `@conditional_on_bean(SomeType)` — only register if another bean of that type already exists.
- `@conditional_on_missing_bean(SomeType)` — only register if no bean of that type exists yet.

The two-pass design is not accidental. Pass 1 conditions depend only on external facts (configuration files, installed packages) that are knowable before any beans are constructed. Pass 2 conditions depend on *which beans got registered* — information that is only available after Pass 1 has settled. Processing them in order ensures each condition evaluates against a stable, predictable view of the world.

The most powerful pattern is **"default with override"**: ship a fallback in your own code that yields to any user-provided implementation:

::: listing lumen/notifications.py | Listing 2.6 — Default-with-override using @conditional_on_missing_bean
from pyfly.container import service
from pyfly.context import conditional_on_missing_bean, conditional_on_property
import structlog

logger = structlog.get_logger()


class NotificationPort:
    async def send(self, owner_id: str, message: str) -> None:
        ...


@conditional_on_property("lumen.smtp.host")
@service
class SmtpNotificationAdapter:
    """Real email sender — only active when SMTP is configured."""

    async def send(self, owner_id: str, message: str) -> None:
        logger.info("smtp_send", owner=owner_id, msg=message)


@conditional_on_missing_bean(NotificationPort)
@service
class LoggingNotificationFallback:
    """Log-only fallback — active whenever no real sender is wired."""

    async def send(self, owner_id: str, message: str) -> None:
        logger.info("notification_fallback", owner=owner_id, msg=message)
:::

**How it works.** Read the two service classes as a chain of intent. `SmtpNotificationAdapter` is the production implementation — it only activates when `lumen.smtp.host` is present in configuration, keeping development environments free of half-configured mail clients. `LoggingNotificationFallback` picks up whenever no real `NotificationPort` implementation is registered — in practice, whenever SMTP is not configured. The fallback does not check *why* the real adapter is absent; it simply fills the gap.

Any `WalletService` that injects `NotificationPort` therefore always gets *something* — no `NoSuchBeanError`, no `None` guard. In development and CI you get structured log output. In production you get real email. The choice is made entirely in configuration, with no code change and no branch in service logic.

!!! tip "Tip"
    The `@conditional_on_missing_bean` / `@conditional_on_property` pair is how all of PyFly's own auto-configuration works. Every subsystem (cache, messaging, HTTP client) ships a default bean that backs off automatically the moment you register your own implementation.

---

## What you built {.recap}

Lumen now has a `WalletRepository` port backed by an in-memory implementation, a `WalletService` wired to it by type hints, and an `EventPublisher` bean produced by a `@configuration` factory — none of it glued together manually. You also saw how `@primary` resolves ambiguity, how `@post_construct` / `@pre_destroy` bracket a bean's life, and how `@conditional_on_missing_bean` enables defaults that give way to real implementations.

The through-line across every feature in this chapter is the same: you declare intent with annotations and type hints; PyFly provides the instances. That separation means you can test each class in isolation, swap adapters without touching business logic, and configure the full application from a YAML file — all of which become essential as Lumen grows through the rest of the book.

---

## Try it yourself {.exercises}

1. **Add a second repository implementation and use `@primary`.** Create a `DictWalletRepository` alongside `InMemoryWalletRepository` — both implementing `WalletRepository`. Start the application and observe the `NoUniqueBeanError`. Then add `@primary` to the one you prefer and watch startup succeed. Next, try injecting the other by name: annotate a constructor parameter as `Annotated[WalletRepository, Qualifier("dict_wallet_repo")]` after registering it with `@repository(name="dict_wallet_repo")`.

2. **Add a `@post_construct` that logs startup metadata.** Extend `WalletService` with an `async def on_ready(self)` method decorated with `@post_construct`. Inside it, log the class name of the injected repository using `structlog.get_logger()`. Run `pyfly run --reload`, start the server, and confirm the log line appears after the framework's own startup messages.

3. **Make a bean conditional on a property.** Add a `WalletAuditService` decorated with `@conditional_on_property("lumen.audit.enabled", having_value="true")`. Open `pyfly.yaml` and omit the key. Verify the service is absent from the bean list at startup. Then add `lumen.audit.enabled: "true"` to `pyfly.yaml` and re-run — confirm it appears. This is exactly how you gate optional subsystems without touching service code.
