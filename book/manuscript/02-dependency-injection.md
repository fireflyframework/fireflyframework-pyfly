<span class="eyebrow">Chapter 2</span>

# Dependency Injection & the Application Context {.chtitle}

::: figure art/openers/ch02.svg | &nbsp;

In the previous chapter you gave Lumen its application entry point
and watched the container start. Now you will declare Lumen's first
real components — a `WalletRepository` that subclasses the
framework's Spring-Data-style `Repository`, a `WalletEntity` that
maps the persistence row, and a CQRS handler that depends on both —
and let PyFly wire them together from nothing but type hints.
No factories, no manual `new`, no glue code.

Before a single line of Lumen code appears, it is worth pausing on
*why* that matters. In a conventional Python project you would write
something like:

```python
handler = DepositFundsHandler(
    repository=InMemoryWalletRepository(),
    events=InMemoryEventBus(),
)
```

somewhere near the startup path. That one line seems harmless, but it
locks every decision — which repository class, which event bus — at the
point of construction. Swap the repository for a Postgres adapter and
you must find every construction site. Add a test double and you need
to restructure the wiring. **Dependency injection** inverts this
relationship: classes *declare* what they need, and the container
*decides* what to provide. The result is code that is open to extension
but closed to modification — the `DepositFundsHandler` you write today
will accept a production database adapter in Part II without a single
change to its source.

---

## Stereotypes: declaring your beans

Before the container can wire anything, it needs to know which classes
to manage. A **bean** is any object the container creates, wires, and
owns. You make a class visible to the container by applying a
**stereotype decorator** — a thin annotation that registers the class
and signals its architectural role.

PyFly ships five stereotypes:

| Decorator | Meaning |
|---|---|
| `@service` | Business-logic layer: domain operations, use-case orchestration. |
| `@component` | Generic managed bean with no specific architectural role. |
| `@repository` | Data-access layer: databases, external storage, ports. |
| `@configuration` | Configuration class that can contain `@bean` factory methods. |
| `@rest_controller` | HTTP layer: handles requests and returns JSON responses. |

All five stereotypes are **container-equivalent**: they share the same
internal `_make_stereotype()` factory and accept the same optional
keyword arguments (`name`, `scope`, `profile`, `condition`). The
meaningful differences are the `__pyfly_stereotype__` label — used by
the web layer to discover controllers and by the context to find
`@configuration` classes — and the architectural clarity each name
brings to readers of your code. Choosing `@repository` over `@component`
costs nothing technically but tells every future reader exactly what
the class is for.

Both bare and parenthesised forms work:

```python
@service              # bare — all defaults
class SimpleService:
    pass

@service(name="wallet_svc")   # with keyword args
class NamedService:
    pass
```

### The scan_packages bootstrap

The container only discovers beans in packages it has been told to
scan. In `lumen/app.py`, `@pyfly_application` lists every subpackage
the container should introspect for stereotype declarations:

::: listing lumen/app.py | Listing 2.1 — Application entry point with scan_packages
from pyfly.core import pyfly_application
from pyfly.starters.domain import enable_domain_stack


@enable_domain_stack
@pyfly_application(
    name="lumen",
    version="1.0.0",
    description=(
        "Lumen — a DDD digital-wallet service"
        " built on the PyFly framework."
    ),
    scan_packages=[
        "lumen.models.repositories",
        "lumen.core.services.wallets",
        "lumen.core.services.transfers",
        "lumen.core.services.listeners",
        "lumen.web.controllers",
    ],
)
class LumenApplication:
    pass
:::

**How it works.** `@pyfly_application` registers `LumenApplication` as
the application root and seeds the container with framework
auto-configurations. `scan_packages` is the exact list of Python
package paths the container walks at startup, collecting every class
decorated with a stereotype. Any package not listed here is invisible
to the container — the most common source of "why is my bean not
found?" confusion when adding new subpackages. `@enable_domain_stack`
activates the CQRS, transactional engine, event sourcing, relational
data, and rule-engine auto-configurations in a single line.

!!! spring "Spring parity"
    `scan_packages` is the equivalent of Spring's
    `@ComponentScan(basePackages = {...})`. The semantics are identical:
    list every subpackage you want the framework to introspect, and it
    will register everything it finds.

### The entity and the repository

Lumen stores wallets in a relational database. Two classes carry this
responsibility: `WalletEntity` (the persistence row) and
`WalletRepository` (the data-access bean).

**The entity.** `WalletEntity` is a plain SQLAlchemy-mapped class that
inherits the framework's `Base`:

::: listing lumen/models/entities/v1/wallet_orm.py | Listing 2.2a — WalletEntity: the persistence row
from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import String
from sqlalchemy.orm import Mapped, mapped_column

from pyfly.data.relational.sqlalchemy import Base


class WalletEntity(Base):
    """One persisted wallet row, keyed by the aggregate's own id."""

    __tablename__ = "wallets"

    id: Mapped[str] = mapped_column(
        String(64), primary_key=True
    )
    owner_id: Mapped[str] = mapped_column(
        String(255), nullable=False, index=True
    )
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    balance_minor: Mapped[int] = mapped_column(
        nullable=False, default=0
    )
    created_at: Mapped[datetime] = mapped_column(
        default=lambda: datetime.now(UTC)
    )
:::

Inheriting `Base` (PyFly's declarative base) registers the `wallets`
table in `Base.metadata`; the framework's engine lifecycle creates it
on startup. No further wiring is needed.

**The repository.** `WalletRepository` subclasses the framework's
generic `Repository[WalletEntity, str]`. The two type arguments tell
the framework the *entity type* (`WalletEntity`) and the *primary-key
type* (`str`); from that it generates and injects a full async CRUD
surface — `find_by_id`, `save`, `find_all`, `find_all(pageable)`,
`delete`, `delete_by_id`, `count`, and more — backed by the transactional
`AsyncSession` from the relational auto-configuration:

::: listing lumen/models/repositories/wallet_repository.py | Listing 2.2b — WalletRepository: framework Repository subclass
from __future__ import annotations

from lumen.models.entities.v1.wallet_orm import WalletEntity
from pyfly.container import repository
from pyfly.data import Page, Pageable
from pyfly.data.relational.sqlalchemy import (
    Repository,
    Specification,
)


def balance_at_least(min_minor: int) -> Specification[WalletEntity]:
    """Reusable predicate: wallets with balance >= min_minor."""
    return Specification(
        lambda root, q: q.where(root.balance_minor >= min_minor)
    )


@repository
class WalletRepository(Repository[WalletEntity, str]):
    """CRUD + derived + specification queries for WalletEntity."""

    # Derived query — compiled from the name by the post-processor
    async def find_by_owner_id(
        self, owner_id: str
    ) -> list[WalletEntity]:
        """All wallets owned by owner_id (derived query stub)."""
        ...

    # Specification query — composable predicate + pagination
    async def find_rich(
        self, min_minor: int, pageable: Pageable
    ) -> Page[WalletEntity]:
        """Page of wallets with balance >= min_minor."""
        return await self.find_all_by_spec_paged(
            balance_at_least(min_minor), pageable
        )

    # Upsert: one call for INSERT or UPDATE
    async def upsert(self, entity: WalletEntity) -> WalletEntity:
        """Persist entity whether the row is new or already exists."""
        session = self._require_session()
        merged = await session.merge(entity)
        await session.flush()
        return merged
:::

**How it works.** `@repository` tells the container to manage
`WalletRepository` as a DI bean. The framework reads
`Repository[WalletEntity, str]` at startup, generates the CRUD
implementation internally, and registers the class — you inject
`WalletRepository` directly by type anywhere in the application.
There is no hand-written port interface and no separate adapter to
maintain: **the framework supplies and injects the implementation;
you depend on the repository class itself by type.**

The three extra methods show the extension points the framework
exposes on top of the inherited CRUD:

- `find_by_owner_id` is a **derived query** — the
  `RepositoryBeanPostProcessor` parses the method name and compiles
  a real `SELECT … WHERE owner_id = :owner_id` at startup; you write
  the stub (`...`) and the framework fills it in.
- `find_rich` is a **Specification query** — it composes a reusable
  `Specification` predicate and runs it with pagination and sorting
  via the inherited `find_all_by_spec_paged`.
- `upsert` is a thin convenience over `session.merge` so a command
  handler can persist an entity whether it is new or already exists
  with a single call.

!!! spring "Spring parity"
    `@service`, `@component`, `@repository`, and `@configuration` map
    directly to Spring's `@Service`, `@Component`, `@Repository`, and
    `@Configuration`. `@rest_controller` mirrors `@RestController`.
    `Repository[E, ID]` mirrors Spring Data's `JpaRepository<E, ID>`:
    declare the entity and key types; the framework generates and
    injects the full implementation. Derived query methods (names like
    `find_by_owner_id`) compile to SQL at startup — the same mechanism
    as Spring Data's query derivation from method names.

---

## Constructor injection

With the repository declared, you need a handler that uses it. That
is where the container's most important capability becomes visible: you
never call constructors yourself. You declare what a class *needs* as
`__init__` parameters with type annotations, and the container fills
them in automatically. This is **constructor injection**, and it is the
recommended approach for all mandatory dependencies.

The mental model is a simple wishlist: list the types you need; the
container delivers the right instances. If a dependency does not exist
at startup, you get a clear `NoSuchBeanError` immediately — not a
cryptic `AttributeError` three call frames deep at runtime.

### Stacking handler decorators on @service

In Lumen's CQRS design, every write-side handler carries two
decorators: `@command_handler` (or `@query_handler`) **stacked on
`@service`**. The pattern is non-negotiable: `@service` registers the
class as a bean; the CQRS decorator adds only routing metadata
(`__pyfly_command_type__` or `__pyfly_query_type__`) so the
command/query bus can dispatch to the right handler. Without `@service`,
the container never sees the class and the bus raises `NoHandlerError`
at dispatch time.

The `DepositFundsHandler` shows the pattern in full:

::: listing lumen/core/services/wallets/deposit_funds_handler.py | Listing 2.3 — DepositFundsHandler: @command_handler + @service stacked
from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from lumen.core.mappers.wallet_mapper import to_aggregate, to_entity
from lumen.core.services.wallets.deposit_funds_command import (
    DepositFunds,
)
from lumen.core.services.wallets.event_publishing import (
    publish_domain_events,
)
from lumen.models.entities.v1.money import Money
from lumen.models.repositories.wallet_repository import WalletRepository
from pyfly.container import service
from pyfly.cqrs import CommandHandler, command_handler
from pyfly.data.relational.sqlalchemy import transactional
from pyfly.domain import AggregateNotFound
from pyfly.eda import EventPublisher


@command_handler
@service
class DepositFundsHandler(CommandHandler[DepositFunds, int]):
    """Credit funds to an existing wallet; returns new balance."""

    def __init__(
        self,
        repository: WalletRepository,
        events: EventPublisher,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        super().__init__()
        self._repository = repository
        self._events = events
        self._session_factory = session_factory

    @transactional()
    async def do_handle(  # type: ignore[override]
        self, command: DepositFunds
    ) -> int:
        entity = await self._repository.find_by_id(
            command.wallet_id
        )
        if entity is None:
            raise AggregateNotFound("Wallet", command.wallet_id)

        wallet = to_aggregate(entity)
        wallet.deposit(
            Money(amount=command.amount, currency=wallet.currency)
        )
        await self._repository.upsert(to_entity(wallet))

        await publish_domain_events(
            self._events, wallet.clear_events()
        )
        return wallet.balance.amount
:::

**How it works.** Five decisions are visible in this listing:

- `@service` registers the class as a singleton bean. Without it,
  the container never sees the class.
- `@command_handler` (applied above `@service`, so it runs *after*
  registration) reads the first generic argument of
  `CommandHandler[DepositFunds, int]` and records that this bean
  handles `DepositFunds` commands.
- The `__init__` signature is the complete wiring specification:
  `repository: WalletRepository` — the framework-generated CRUD
  bean; `events: EventPublisher` — resolved by the CQRS
  auto-configuration; `session_factory: async_sessionmaker[AsyncSession]`
  — the shared connection factory provided by the relational
  auto-configuration. All three are resolved by type; `DepositFundsHandler`
  never imports a concrete class.
- `@transactional()` on `do_handle` wraps the entire body in a
  single committed unit of work. The decorator opens a session from
  `session_factory`, binds it to the repository for the duration of
  the call, and commits on success (or rolls back on error).
- The business logic follows the standard CQRS/DDD sequence: load
  the entity, rehydrate the aggregate via the mapper, mutate through
  domain methods that enforce invariants, persist via `upsert`, drain
  and publish the events. The wallet is saved *before* events are
  published, so any listener that queries the repository finds the
  updated record.

A read-side handler uses the same stacking pattern, only with
`@query_handler` and `QueryHandler`:

```python
from pyfly.container import service
from pyfly.cqrs import QueryHandler, query_handler
from lumen.models.repositories.wallet_repository import WalletRepository


@query_handler
@service
class GetWalletHandler(QueryHandler[GetWallet, WalletDto | None]):
    def __init__(self, repository: WalletRepository) -> None:
        super().__init__()
        self._repository = repository

    async def do_handle(
        self, query: GetWallet
    ) -> WalletDto | None:
        entity = await self._repository.find_by_id(query.wallet_id)
        return entity_to_dto(entity) if entity is not None else None
```

The container resolves dependencies **recursively**. When it constructs
`DepositFundsHandler` it also constructs `WalletRepository` (the
framework-generated CRUD bean), the `EventPublisher`, and the
`async_sessionmaker` — none of which the handler needs to know about.

::: figure art/figures/02-di.svg | Figure 2.1 — The container injects dependencies from type hints.

!!! spring "Spring parity"
    Constructor injection in PyFly is functionally identical to
    Spring's `@Autowired` constructor injection. In modern Spring you
    do not even write `@Autowired` — the framework infers injection
    from the single constructor, just as PyFly reads `__init__` type
    hints. The mental model is the same: declare what you need, let
    the container provide it.

!!! tip "Tip"
    Prefer constructor injection for mandatory dependencies. It makes
    them visible in the class signature, lets you write plain-Python
    unit tests without a container
    (`handler = DepositFundsHandler(repo=MockRepo(), events=MockBus())`),
    and prevents accidental missing-dependency bugs at startup rather
    than at runtime.

---

## The Container and the ApplicationContext

PyFly's DI system has two layers, and understanding the boundary
between them will save you real debugging time. One layer handles
object graphs; the other handles the full application lifecycle.
Conflating them is a common source of confusion.

**`Container`** (from `pyfly.container`) is the low-level DI engine.
It stores `Registration` objects, resolves types by constructor hints,
manages scopes, applies `@primary` disambiguation, and handles
`Qualifier`-based named lookups. It has no lifecycle awareness — it
is a pure "give me a `T`" machine.

**`ApplicationContext`** (from `pyfly.context`) is the high-level
orchestrator. It wraps `Container` and adds the full startup sequence:
profile filtering, condition evaluation, `@configuration`/`@bean`
processing, `BeanPostProcessor` weaving, `@post_construct` /
`@pre_destroy` hooks, event publishing, and auto-configuration. You
interact with the `ApplicationContext` in application code; the raw
`Container` is an implementation detail, accessible via `ctx.container`
as an escape hatch.

Think of it this way: `Container` is the factory floor — it knows how
to build things. `ApplicationContext` is the production manager — it
decides what gets built, in what order, and what happens when the
factory opens or closes.

### Resolution rules

When the container needs to resolve a type `T`, it applies four rules
in strict priority order:

1. **Direct registration** — if `T` is registered directly, resolve it.
2. **Interface binding** — if `T` is a `Protocol` or ABC with exactly
   one bound implementation, resolve that implementation.
3. **`@primary` disambiguation** — if multiple implementations are
   bound, the one decorated with `@primary` wins.
4. **Error** — `NoSuchBeanError` when nothing matches;
   `NoUniqueBeanError` when multiple candidates exist with no `@primary`.

Step 4 is deliberately loud. A missing or ambiguous dependency is a
configuration error, and surfacing it at startup rather than burying it
in a runtime traceback is one of the container's key guarantees.

### @primary

`@primary` resolves ambiguity when several beans satisfy the same
interface. Place it on the implementation you want as the default.
This arises whenever you have a `@runtime_checkable Protocol` port
with more than one registered adapter — a common pattern for swappable
infrastructure (cache store, message bus, notification channel).

For example, suppose your application defines a `CacheStore` protocol
and ships two adapters — an in-process one for local development and a
Redis one for production:

```python
from pyfly.container import repository, primary


@primary
@repository
class InMemoryCacheStore(CacheStore):
    """Default: active in development and tests."""
    ...


@repository
class RedisCacheStore(CacheStore):
    """Production cache — activated by profile or condition."""
    ...
```

Without `@primary`, resolving `CacheStore` with two registered
implementations raises:

```
NoUniqueBeanError: Multiple beans of type 'CacheStore' found
  but none is marked @primary
  Candidates: ['InMemoryCacheStore', 'RedisCacheStore']
```

The message names every competing candidate so you can make a
deliberate decision rather than guessing which one the container would
have picked. Moving `@primary` from one adapter to the other is the
only change needed to switch the application's backing store — nothing
in the service code changes.

Note that Lumen's own `WalletRepository` is a framework `Repository`
subclass, so only one bean is registered and no `@primary` is needed.
`@primary` is relevant whenever you hand-roll a port/adapter pair with
multiple adapter implementations.

### @order

The container initialises singleton beans eagerly during startup, but
some beans genuinely must be ready before others — a security filter
that must wrap every inbound request, or a schema migrator that must
run before any repository is touched. `@order` gives you explicit
control over initialisation sequence.

Lower values are resolved first during the eager startup pass. The
constants `HIGHEST_PRECEDENCE` (`-(2**31)`) and `LOWEST_PRECEDENCE`
(`2**31 - 1`) mark the extremes:

```python
from pyfly.container import order, HIGHEST_PRECEDENCE, service


@order(HIGHEST_PRECEDENCE)
@service
class SecurityInitializer:
    """Must be ready before any other service."""
    ...
```

`@order` affects singleton resolution during startup, the sequence in
which `BeanPostProcessor` instances run, and the ordering of
`get_beans_of_type()` results.

### Qualifier — named bean resolution

Type-based injection covers most scenarios. Occasionally, though, you
genuinely need a particular *instance* rather than any satisfying
implementation — the classic case being a `@configuration` class that
produces two beans of the same type (say, a primary and a read-replica
database connection) where a downstream service must receive a specific
one.

Select a specific bean by name with `Annotated[T, Qualifier("name")]`:

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

The container calls `resolve_by_name("analytics_db", expected_type=T)`
and verifies assignability — a mistyped name pointing at the wrong type
raises `NoSuchBeanError` with a clear message rather than silently
injecting the wrong object.

---

## Bean factories: @configuration and @bean

Stereotype decorators work beautifully for classes you own, but not
every dependency is a class you control. Third-party clients need
constructor arguments known only at runtime; related beans share
configuration state; some families of beans are most clearly expressed
as a single factory. For all of these situations, PyFly provides the
`@configuration` / `@bean` pattern — explicit factory code that still
participates fully in the container's resolution and lifecycle
machinery.

A `@configuration` class acts as a factory. Its `@bean` methods are
called during the startup sequence, and each method's return value is
registered as a bean whose type comes from the method's return
annotation:

::: listing lumen/infra_config.py | Listing 2.4 — Producing an EventPublisher bean via @configuration
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

**How it works.** `@configuration` tells the context to scan
`LumenInfraConfig` for `@bean` methods during startup, before any
stereotype beans are constructed. The return annotation `EventPublisher`
is the key: the context reads it and registers the produced
`InMemoryEventBus` instance *as* an `EventPublisher`, not as an
`InMemoryEventBus`. That distinction matters — when `DepositFundsHandler`
later asks for an `EventPublisher`, it receives the `InMemoryEventBus`
instance without knowing or caring about the concrete type.

Swapping to a Kafka adapter for production means replacing
`InMemoryEventBus()` with `KafkaEventPublisher(settings.kafka_url)` in
a single method. The rest of the codebase is untouched.

`@bean` methods can also declare parameters; the container resolves
them automatically:

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
    The return type annotation on a `@bean` method is **mandatory**.
    The context reads it to know which interface type to register the
    produced bean under. Omitting it will cause the bean to be
    unreachable by type.

---

## Scopes

Every bean has a **scope** that controls how long its instance lives.
Getting scope right is less about performance and more about
correctness: sharing a stateful object designed for single-use produces
race conditions; re-creating a singleton on every resolution wastes
resources and defeats caching. The `Scope` enum defines three values
that cover the vast majority of real-world needs.

**`Scope.SINGLETON`** (default) — one instance is created on first
resolution and reused for the life of the application. Singletons are
instantiated eagerly during `ApplicationContext.start()`, sorted by
`@order`. Almost all application beans should be singletons.

**`Scope.TRANSIENT`** — a fresh instance is created on every resolution.
Use this for stateful, non-shareable objects:

::: listing lumen/contexts.py | Listing 2.5 — A transient bean for per-operation context
from pyfly.container import component, Scope


@component(scope=Scope.TRANSIENT)
class TransferContext:
    """Carries state for a single wallet transfer operation."""

    def __init__(self) -> None:
        self.steps: list[str] = []
        self.rolled_back: bool = False
:::

**How it works.** `TransferContext` accumulates the steps of a
multi-hop transfer so that a saga can roll them back in reverse order
if anything fails. Sharing a single instance across concurrent requests
would blend their state; `Scope.TRANSIENT` ensures every resolution
produces a fresh, empty `TransferContext`. The container still manages
the class — injecting it, profiling it, post-processing it — but never
caches the result.

**`Scope.REQUEST`** — scoped to a single HTTP request. A new instance
is created when a request arrives and discarded when it completes. Use
this for web-layer beans that carry request-specific state, such as
the current authenticated user.

```python
from pyfly.container import component, Scope


@component(scope=Scope.REQUEST)
class CurrentUser:
    user_id: str = ""
    roles: list[str] = []
```

A quick rule of thumb:

- **SINGLETON** — the bean is stateless, or its state is safe to share
  across all callers (connection pools, caches, service objects).
- **TRANSIENT** — the bean accumulates per-operation state that must
  not bleed between operations (sagas, builders, context carriers).
- **REQUEST** — the bean carries per-HTTP-request state that must be
  isolated between concurrent requests (authenticated user,
  request-scoped trace ID).

---

## Lifecycle and conditions

Construction and wiring are only half the story. Real infrastructure
beans need to *act* after they are built — reserving a thread pool,
pre-loading a cache, subscribing to a message queue — and they need to
*undo* those actions cleanly on shutdown. PyFly gives you two lifecycle
hooks for this, plus a family of conditional decorators that control
whether a bean participates in the container at all.

### @post_construct and @pre_destroy

Once the container constructs a bean and injects all its dependencies,
you often need one-time initialisation — opening a connection pool,
warming a cache, registering a listener. Mark a method `@post_construct`
and the context calls it after construction completes. Both synchronous
and `async` methods are supported:

::: listing lumen/wallet_audit_listener.py | Listing 2.6 — Lifecycle hooks on a @service bean
from pyfly.container import service
from pyfly.context import post_construct, pre_destroy
import logging

logger = logging.getLogger(__name__)


@service
class WalletAuditListenerWithLifecycle:
    def __init__(self) -> None:
        self._entries: list[dict] = []

    @post_construct
    async def on_start(self) -> None:
        logger.info("wallet_audit_listener_ready")

    @pre_destroy
    async def on_stop(self) -> None:
        logger.info("wallet_audit_listener_shutting_down")
:::

**How it works.** `on_start` fires *after* the constructor returns and
all injected dependencies are set — making it safe to issue repository
queries, open connections, or publish an application event. The `async`
keyword works without any extra setup: the context calls
`await on_start()` when it detects a coroutine, and falls back to a
direct call for synchronous methods.

`@pre_destroy` is the counterpart, called during
`ApplicationContext.stop()` before the bean is discarded. Beans are
destroyed in **reverse** initialisation order, so a listener started
after the repository is stopped before it.

::: figure art/figures/02-lifecycle.svg | Figure 2.2 — A bean's lifecycle.

### Conditional beans

Conditions answer a powerful question: *should this bean exist at all,
given the current environment?* They are how the same codebase works
in development (cheap in-memory adapters), in CI (Testcontainers), and
in production (real infrastructure) — without a single `if` statement
in your service code.

Conditional decorators are evaluated in a two-pass strategy during
`ApplicationContext.start()`:

**Pass 1** (before user `@configuration` is processed) evaluates:
- `@conditional_on_property(key, having_value="...")` — the config
  key must exist and optionally match a value.
- `@conditional_on_class("module.name")` — the Python module must be
  importable.
- The `condition` callable on a stereotype decorator.

**Pass 2** (after user `@configuration` is processed) evaluates:
- `@conditional_on_bean(SomeType)` — only register if another bean
  of that type already exists.
- `@conditional_on_missing_bean(SomeType)` — only register if no
  bean of that type exists yet.

The two-pass design is deliberate. Pass 1 conditions depend on external
facts — configuration files and installed packages — that are knowable
before any beans are constructed. Pass 2 conditions depend on *which
beans got registered*, information available only after Pass 1 settles.
Processing them in order ensures each condition evaluates against a
stable, predictable view of the container.

The most powerful pattern is **"default with override"** — ship a
fallback that automatically yields to any user-provided implementation:

::: listing lumen/notifications.py | Listing 2.7 — Default-with-override using @conditional_on_missing_bean
from pyfly.container import service
from pyfly.context import conditional_on_missing_bean, conditional_on_property
import logging

logger = logging.getLogger(__name__)


class NotificationPort:
    async def send(self, owner_id: str, message: str) -> None:
        ...


@conditional_on_property("lumen.smtp.host")
@service
class SmtpNotificationAdapter:
    """Real email sender — only active when SMTP is configured."""

    async def send(self, owner_id: str, message: str) -> None:
        logger.info("smtp_send owner=%s", owner_id)


@conditional_on_missing_bean(NotificationPort)
@service
class LoggingNotificationFallback:
    """Log-only fallback — active whenever no real sender is wired."""

    async def send(self, owner_id: str, message: str) -> None:
        logger.info("notification_fallback owner=%s", owner_id)
:::

**How it works.** Read the two classes as a chain of intent.
`SmtpNotificationAdapter` activates only when `lumen.smtp.host` is
present in configuration, keeping development environments free of
half-configured mail clients. `LoggingNotificationFallback` activates
whenever no real `NotificationPort` is registered — in practice, any
environment where SMTP is not configured. The fallback does not check
*why* the real adapter is absent; it simply fills the gap.

Any handler that injects `NotificationPort` therefore always receives
*something* — no `NoSuchBeanError`, no `None` guard. In development and
CI you get structured log output; in production you get real email. The
choice is made entirely in configuration, with no code change and no
branch in service logic.

!!! tip "Tip"
    The `@conditional_on_missing_bean` / `@conditional_on_property`
    pair is how all of PyFly's own auto-configuration works. Every
    subsystem (cache, messaging, HTTP client) ships a default bean
    that backs off automatically the moment you register your own
    implementation.

---

## What you built {.recap}

Lumen now has a `WalletEntity` mapped to the `wallets` table, a
`WalletRepository` that subclasses the framework's
`Repository[WalletEntity, str]` (giving a full async CRUD surface, a
derived query, and a specification query), and a
`DepositFundsHandler` wired to the repository, the event publisher,
and the session factory — all by type hints alone. You saw why
`@command_handler` and `@query_handler` **must be stacked on
`@service`** — the CQRS decorators add routing metadata, but
`@service` is what registers the bean. You saw that the framework
auto-generates and injects the `Repository` implementation so you
depend on the repository class itself by type, with no hand-written
port/adapter pair required. You also saw how `@primary` resolves
ambiguity when two adapters compete for a hand-rolled port, how
`@post_construct` / `@pre_destroy` bracket a bean's life, and how
`@conditional_on_missing_bean` enables defaults that automatically
yield to real implementations.

The through-line is consistent: you declare intent with decorators and
type hints; PyFly provides the instances. That separation lets you test
each class in isolation, swap adapters without touching business logic,
and drive the full application configuration from a YAML file — all of
which become essential as Lumen grows through the rest of the book.

---

## Try it yourself {.exercises}

1. **Practice `@primary` with a hand-rolled port.**
   Define a `CacheStore` protocol with a single `async def get(key)`
   method. Register two `@repository` adapters — an in-process one
   and a stub "remote" one — both inheriting `CacheStore`. Start the
   application and observe the `NoUniqueBeanError`. Then add `@primary`
   to the in-process adapter and watch startup succeed. Next, try
   injecting the stub by name: annotate a constructor parameter as
   `Annotated[CacheStore, Qualifier("remote_cache")]` after registering
   it with `@repository(name="remote_cache")`.

2. **Add a `@post_construct` that logs startup metadata.** Extend
   `WalletAuditListener` with an `async def on_ready(self)` method
   decorated with `@post_construct`. Inside it, log the class name of
   any injected dependency. Run `pyfly run --reload`, start the server,
   and confirm the log line appears after the framework's own startup
   messages.

3. **Make a bean conditional on a property.** Add a
   `WalletAuditService` decorated with
   `@conditional_on_property("lumen.audit.enabled", having_value="true")`.
   Open `pyfly.yaml` and omit the key. Verify the service is absent
   from the bean list at startup. Then add
   `lumen.audit.enabled: "true"` to `pyfly.yaml` and re-run — confirm
   it appears. This is exactly how you gate optional subsystems without
   touching service code.
