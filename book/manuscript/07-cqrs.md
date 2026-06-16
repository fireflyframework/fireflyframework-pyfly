<span class="eyebrow">Chapter 7</span>

# CQRS: Commands & Queries {.chtitle}

::: figure art/openers/ch07.svg | &nbsp;

Lumen's wallet is now a first-class citizen of the domain. The `Wallet` aggregate enforces its own invariants, emits domain events, and persists through a clean repository boundary. The controller, though, still calls `WalletApplicationService` directly — one method per operation, reads and writes sharing the same code path. That design is fine at small scale, but it shows friction as the system grows. The team wants to cache wallet balances, maintain a single audit trail for every write, add authorization rules to specific operations, and test each piece of logic in complete isolation from the others.

**CQRS** — Command Query Responsibility Segregation — addresses all of this by drawing a bright line between the two things a service can do: *change state* and *read state*. Writes become **commands**: strongly typed, named, immutable messages that flow through a `CommandBus`. Reads become **queries**: equally typed messages that flow through a `QueryBus`. Each bus runs a fixed pipeline — validation, authorization, execution, then (for commands) domain event publishing. Your handler implements exactly one intent; the bus handles everything else.

By the end of this chapter Lumen's controller dispatches commands and queries instead of calling the service directly. `OpenWallet`, `DepositFunds`, and `WithdrawFunds` travel the command path; `GetWallet`, `GetBalance`, `ListWallets`, and `ListRichWallets` travel the query path. The `Wallet` aggregate you built in Chapter 6 remains untouched — CQRS does not replace the domain model; it is the delivery mechanism for instructions to it.

!!! note "New jargon, in plain terms"
    A **bus** here is not hardware — it is a single object you hand a message to, and it figures out which handler should run. A **handler** is one small class that does the work for exactly one message type. A **DTO** (data transfer object) is a plain shape — id, owner, balance — that you put on the wire as JSON; it is deliberately separate from your rich domain object. A **projection** is a read-only slice of your data shaped for one specific view. You will meet each of these as we build, one piece at a time, so do not worry if they feel abstract right now.

This chapter is built around PyFly **v26.6.110**, and every listing is taken verbatim from the Lumen sample under `samples/lumen/src/lumen`. We will go gently: build the commands first, then their handlers, then the queries, then wire everything into the controller — running the app and the tests at each milestone so you can see the pieces come alive before the next one is added.

---

## Why separate reads from writes

Picture Lumen at the end of Chapter 6. `WalletController` calls `WalletApplicationService.credit(wallet_id, amount)`. That call mutates state, but nothing in the method signature makes that obvious. The team wants to add a balance cache. Where does it go? Inside `credit`? In a decorator around the service? The question reveals the problem: a single service method is asked to serve two masters — the write path, which must always touch the database, and the read path, which should avoid it whenever possible. Bolting caching onto a write method is awkward at best and dangerous at worst.

Writes and reads have fundamentally different shapes. A write carries intent and data: "deposit 1 500 minor units into wallet wlt-001". A read carries a question: "what is the current balance of wallet wlt-001?" The first must reach the database every time. The second is repeatable — asking twice should return the same answer without doubling the database load. Funnelling both through the same method conflates concerns that scale differently, test differently, and need different cross-cutting behaviour.

The deeper benefit is **clarity of intent**. When a teammate reads `wallet_service.credit(wallet_id, amount)`, they must inspect the implementation to know whether it is safe to call twice, whether it publishes events, and whether it is idempotent. When they read `DepositFunds(wallet_id=..., amount=...)`, the intent is unambiguous — and if the intent turns out to be wrong, you rename the command, not the service signature.

Three concrete benefits matter for Lumen:

**Independent scaling.** Reads typically outnumber writes by an order of magnitude or more. Once the two paths are separate, the bus can cache query results without touching the write path. You can route queries to a read replica and commands to the primary database with a configuration change, not a code change.

**Focused handlers.** Each handler implements exactly one operation. `DepositFundsHandler` loads a wallet, drives its domain behaviour, persists it, and drains events — nothing more. `GetBalanceHandler` loads one wallet and returns a lightweight projection — nothing more. Because handlers are plain Python classes with injected dependencies, you can unit-test each in complete isolation from the HTTP layer.

**Centralized cross-cutting concerns.** Validation, authorization, and distributed tracing are implemented once in the bus pipeline and apply uniformly to every handler — no boilerplate in the handler itself. Adding per-operation authorization later is a matter of overriding `authorize()` on the command; the bus ensures it runs before `do_handle` is ever reached.

---

## Commands and command handlers

Before writing a single line of handler code, name your system's intentions. In Lumen's wallet domain three things can happen: a wallet can be opened, funds can be deposited, and funds can be withdrawn. Each is a **command** — a named, immutable message that expresses one intent. The bus delivers it; the handler acts on it; the domain aggregate enforces the rules. Commands are not method calls dressed up as objects: they are explicit contracts that live in your codebase as first-class citizens.

A command is a frozen dataclass that inherits from `Command[R]`, where `R` is the type the handler returns. The generic parameter is documentation and a type-checker hint; the bus does not enforce it at runtime.

!!! note "What is `Command[R]`?"
    The `[R]` in `Command[R]` is a *generic type parameter* — a placeholder for "whatever this command returns". `OpenWallet(Command[str])` says "sending me gives you back a `str`" (the new wallet id). `DepositFunds(Command[int])` says "sending me gives you back an `int`" (the new balance). Your editor and type checker use this to catch mistakes; at runtime the bus simply returns whatever the handler returned.

Lumen's commands live in three separate files under `lumen/core/services/wallets/`, one per intent. We will build them one at a time.

**Step 1 — Write the `OpenWallet` command.** Create `open_wallet_command.py`. It carries the two facts needed to open a wallet — who owns it and which currency it holds — and a `validate()` hook that rejects a blank owner before the bus ever looks for a handler.

::: listing lumen/core/services/wallets/open_wallet_command.py | Listing 7.1 — OpenWallet: a frozen command with built-in validation
from __future__ import annotations

from dataclasses import dataclass

from lumen.interfaces.enums.v1.currency import Currency
from pyfly.cqrs import Command, ValidationResult


@dataclass(frozen=True)
class OpenWallet(Command[str]):
    """Open a new wallet. Returns the generated wallet id."""

    owner_id: str
    currency: Currency

    async def validate(self) -> ValidationResult:  # type: ignore[override]
        if not self.owner_id.strip():
            return ValidationResult.failure(
                "owner_id", "Owner id is required"
            )
        return ValidationResult.success()
:::

**Step 2 — Write the `DepositFunds` and `WithdrawFunds` commands.** Each carries a `wallet_id` to target and an `amount` in minor units, and validates that the id is present and the amount is positive. They are deliberately near-identical twins — same shape, opposite direction.

::: listing lumen/core/services/wallets/deposit_funds_command.py | Listing 7.2 — DepositFunds: amount in minor units, no currency field
from __future__ import annotations

from dataclasses import dataclass

from pyfly.cqrs import Command, ValidationResult


@dataclass(frozen=True)
class DepositFunds(Command[int]):
    """Deposit ``amount`` minor units. Returns the new balance."""

    wallet_id: str
    amount: int

    async def validate(self) -> ValidationResult:  # type: ignore[override]
        if not self.wallet_id.strip():
            return ValidationResult.failure(
                "wallet_id", "Wallet id is required"
            )
        if self.amount <= 0:
            return ValidationResult.failure(
                "amount", "Deposit amount must be > 0"
            )
        return ValidationResult.success()
:::

::: listing lumen/core/services/wallets/withdraw_funds_command.py | Listing 7.3 — WithdrawFunds: same shape as DepositFunds
from __future__ import annotations

from dataclasses import dataclass

from pyfly.cqrs import Command, ValidationResult


@dataclass(frozen=True)
class WithdrawFunds(Command[int]):
    """Withdraw ``amount`` minor units. Returns the new balance."""

    wallet_id: str
    amount: int

    async def validate(self) -> ValidationResult:  # type: ignore[override]
        if not self.wallet_id.strip():
            return ValidationResult.failure(
                "wallet_id", "Wallet id is required"
            )
        if self.amount <= 0:
            return ValidationResult.failure(
                "amount", "Withdrawal amount must be > 0"
            )
        return ValidationResult.success()
:::

Four design choices are baked into every command:

- **`frozen=True`** makes the dataclass immutable the moment it is constructed. Fields cannot be accidentally mutated in one layer of the pipeline before reaching another, and immutable messages are hashable by default — useful when storing or comparing them in tests.

- **`validate()`** is an async hook the bus calls before dispatching the handler. `OpenWallet.validate` checks that `owner_id` is not blank; `DepositFunds.validate` and `WithdrawFunds.validate` check that the amount is positive. These pre-conditions belong on the command — they require no database lookup and do not belong in the domain aggregate. The aggregate enforces invariants that need loaded state (overdraft, currency match); commands enforce invariants knowable from the fields alone. Keeping these two layers separate means the aggregate is never called with structurally wrong data.

- **No `currency` field** on `DepositFunds` or `WithdrawFunds`. The wallet's own currency is the only valid currency for a deposit or withdrawal, and the repository resolves that once the aggregate is loaded. Carrying a currency on the command would invite mismatches; the aggregate enforces the invariant from its own state.

- **Imperative-mood naming**: `DepositFunds`, not `WalletDeposit` or `DepositFundsCommand`. This makes the command log read like a business audit trail — a sequence of things that *happened* — rather than a list of technical operations.

!!! note "What just happened"
    You now have three small files, each describing *one thing the system can do*, with no logic beyond a couple of field checks. There are no handlers yet — these commands do nothing on their own. That is the point: a command is an envelope, not the worker who opens it. Next you will write the workers (the handlers) that actually carry out each intent.

### Implementing a command handler

A command handler inherits from `CommandHandler[C, R]` and implements exactly one method: `do_handle`. You write the *what*; the bus wraps it with the *how*.

**Both decorators on every handler are required.** `@command_handler` registers the class with the `HandlerRegistry` by introspecting the first generic type argument — no manual registration needed. `@service` wires the handler into PyFly's DI container so constructor arguments are resolved and injected automatically at startup. The order matters: `@command_handler` on top, `@service` directly below. Without `@service`, the DI container never instantiates the class and the bus cannot find the handler; without `@command_handler`, the registry never maps the command type to the class. Omitting either decorator is a silent failure — the bus raises "no handler found" at dispatch time.

**`@transactional()` turns `do_handle` into a committed unit of work.** Command handlers inject `session_factory: async_sessionmaker[AsyncSession]` and store it as `self._session_factory`. When `@transactional()` runs `do_handle` it opens a fresh session from that factory, swaps it onto the repository for the duration of the call, commits on success, and rolls back on any exception. Without `@transactional()` the framework's shared session only flushes — the write survives within the request but is never committed to the database.

!!! note "Flush vs. commit, in plain terms"
    A **flush** pushes your pending changes into the database connection so later queries in the *same* session can see them — but they are still inside an open transaction that can be rolled back. A **commit** makes them permanent. Without `@transactional()` your deposit would flush (visible mid-request) but never commit (gone after the request). The decorator is what makes the change stick.

**Step 3 — Write `OpenWalletHandler`.** Now build the worker for the first command. Create `open_wallet_handler.py`, stack `@command_handler` over `@service`, inject the repository, the event publisher, and the session factory, and implement the single `do_handle` method.

::: listing lumen/core/services/wallets/open_wallet_handler.py | Listing 7.4 — OpenWalletHandler: @transactional() unit of work + upsert
from __future__ import annotations

from uuid import uuid4

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from lumen.core.mappers.wallet_mapper import to_entity
from lumen.core.services.wallets.event_publishing import publish_domain_events
from lumen.core.services.wallets.open_wallet_command import OpenWallet
from lumen.models.entities.v1.wallet_entity import Wallet
from lumen.models.repositories.wallet_repository import WalletRepository
from pyfly.container import service
from pyfly.cqrs import CommandHandler, command_handler
from pyfly.data.relational.sqlalchemy import transactional
from pyfly.eda import EventPublisher


@command_handler
@service
class OpenWalletHandler(CommandHandler[OpenWallet, str]):
    """Open a new, empty wallet."""

    def __init__(
        self,
        repository: WalletRepository,
        events: EventPublisher,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        super().__init__()
        self._repository = repository
        self._events = events
        # @transactional resolves the unit-of-work session from here.
        self._session_factory = session_factory

    @transactional()
    async def do_handle(self, command: OpenWallet) -> str:  # type: ignore[override]
        wallet_id = f"wlt-{uuid4()}"
        wallet = Wallet.open(
            wallet_id=wallet_id,
            owner_id=command.owner_id,
            currency=command.currency,
        )
        await self._repository.upsert(to_entity(wallet))
        await publish_domain_events(self._events, wallet.clear_events())
        return wallet_id
:::

Walk through `do_handle` step by step. `f"wlt-{uuid4()}"` generates a stable prefixed identifier. `Wallet.open(...)` calls the factory, which enforces the non-empty owner pre-condition and buffers a `WalletOpened` event. `to_entity(wallet)` maps the aggregate to a flat `WalletEntity` row. `repository.upsert(...)` calls `session.merge` — a single call that inserts if no row exists or updates if one does — then flushes. Using `upsert` instead of `save` avoids an `IntegrityError` on the primary key: the aggregate owns its id, so both INSERT and UPDATE key on the same stable string. `wallet.clear_events()` drains the buffer and `publish_domain_events` forwards each event to the EDA bus. The `@transactional()` decorator commits the session on the way out. The handler returns the wallet ID, which flows back to the controller as the `send` return value.

Note the constructor requirement: `super().__init__()` is mandatory on `CommandHandler`. Skip it and the base-class bookkeeping — correlation context, lifecycle hooks — is never initialized. The repository, `EventPublisher`, and `session_factory` are all injected by the DI container from type hints; no factory configuration is needed.

**Step 4 — Write the deposit and withdrawal handlers.** These two add one move that `OpenWalletHandler` did not need: they *load* an existing wallet before acting on it. The shape is the same in both, differing only in whether they call `wallet.deposit(...)` or `wallet.withdraw(...)`.

::: listing lumen/core/services/wallets/deposit_funds_handler.py | Listing 7.5 — DepositFundsHandler: find_by_id → to_aggregate → act → upsert
from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from lumen.core.mappers.wallet_mapper import to_aggregate, to_entity
from lumen.core.services.wallets.deposit_funds_command import DepositFunds
from lumen.core.services.wallets.event_publishing import publish_domain_events
from lumen.models.entities.v1.money import Money
from lumen.models.repositories.wallet_repository import WalletRepository
from pyfly.container import service
from pyfly.cqrs import CommandHandler, command_handler
from pyfly.domain import AggregateNotFound
from pyfly.data.relational.sqlalchemy import transactional
from pyfly.eda import EventPublisher


@command_handler
@service
class DepositFundsHandler(CommandHandler[DepositFunds, int]):
    """Credit funds to an existing wallet; returns the new balance."""

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
    async def do_handle(self, command: DepositFunds) -> int:  # type: ignore[override]
        entity = await self._repository.find_by_id(command.wallet_id)
        if entity is None:
            raise AggregateNotFound("Wallet", command.wallet_id)

        wallet = to_aggregate(entity)
        wallet.deposit(Money(amount=command.amount, currency=wallet.currency))
        await self._repository.upsert(to_entity(wallet))

        await publish_domain_events(self._events, wallet.clear_events())
        return wallet.balance.amount
:::

::: listing lumen/core/services/wallets/withdraw_funds_handler.py | Listing 7.6 — WithdrawFundsHandler: identical pattern, overdraft refused by the aggregate
from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from lumen.core.mappers.wallet_mapper import to_aggregate, to_entity
from lumen.core.services.wallets.event_publishing import publish_domain_events
from lumen.core.services.wallets.withdraw_funds_command import WithdrawFunds
from lumen.models.entities.v1.money import Money
from lumen.models.repositories.wallet_repository import WalletRepository
from pyfly.container import service
from pyfly.cqrs import CommandHandler, command_handler
from pyfly.domain import AggregateNotFound
from pyfly.data.relational.sqlalchemy import transactional
from pyfly.eda import EventPublisher


@command_handler
@service
class WithdrawFundsHandler(CommandHandler[WithdrawFunds, int]):
    """Debit funds from an existing wallet; returns the new balance."""

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
    async def do_handle(self, command: WithdrawFunds) -> int:  # type: ignore[override]
        entity = await self._repository.find_by_id(command.wallet_id)
        if entity is None:
            raise AggregateNotFound("Wallet", command.wallet_id)

        wallet = to_aggregate(entity)
        wallet.withdraw(Money(amount=command.amount, currency=wallet.currency))
        await self._repository.upsert(to_entity(wallet))

        await publish_domain_events(self._events, wallet.clear_events())
        return wallet.balance.amount
:::

`DepositFundsHandler` and `WithdrawFundsHandler` follow the classic pattern: **find → to_aggregate → act → to_entity → upsert → drain**. `repository.find_by_id` returns the flat `WalletEntity` row; `to_aggregate(entity)` rehydrates the rich domain object so the aggregate's invariants are in scope. `Money` is constructed from the command's `amount` and the *wallet's* currency — never a currency from the command itself — because the wallet owns that invariant. If `wallet.withdraw` refuses (balance would go negative), it raises `BusinessRuleViolation`, which propagates as HTTP 422 without a single line of error-handling code in the handler.

Notice what is absent: no try/except blocks, no logging calls, no tracing setup. All of that belongs to the bus pipeline. The handler is a pure expression of business intent.

!!! note "What just happened"
    The write side is now complete: three commands and three handlers. Sending `OpenWallet` creates and persists a fresh wallet; sending `DepositFunds` or `WithdrawFunds` loads one, drives its domain behaviour, and saves it. The `@command_handler` + `@service` stack means PyFly discovers and wires these at startup — you never call them directly, and you never register them by hand.

**Run it — confirm the write side works end to end.** The Lumen sample already ships a test that exercises the full command path. From the `samples/lumen` directory, run just that test:

::: listing terminal | Listing 7.4a — Exercise the command path
uv run --extra dev pytest tests/test_cqrs_flow.py::test_full_wallet_lifecycle -q
:::

You should see a single passing test:

```
1 passed in 0.42s
```

That one test opens a wallet, deposits 1 500 minor units, withdraws 500, and asserts the balance lands at 1 000 — proving `OpenWalletHandler`, `DepositFundsHandler`, and `WithdrawFundsHandler` all commit through the bus. If you see `0 items collected`, you are not in the `samples/lumen` directory; `cd` there first. If you see `no handler found`, double-check that both decorators are present on each handler — that is the single most common cause.

### The entity↔aggregate mapper

Command handlers do not interact with the repository through the domain aggregate. They interact through a flat `WalletEntity` row — the persistence shape the framework `Repository[WalletEntity, str]` understands — and use `wallet_mapper` to translate between the two worlds:

```python
# Aggregate → row (before upsert)
to_entity(wallet)      # Wallet → WalletEntity

# Row → aggregate (after find_by_id)
to_aggregate(entity)   # WalletEntity → Wallet
```

This separation keeps the aggregate free of SQLAlchemy annotations and the repository free of domain logic. The mapper is a single module; changing the storage schema touches one file, not every handler.

### Sending a command

The `CommandBus` is the single entry point for all writes. PyFly's auto-configuration registers a `DefaultCommandBus` as a singleton in the DI container; declare it as a constructor argument and the framework injects it. Sending a command is a single awaited call:

```python
from pyfly.cqrs import DefaultCommandBus
from lumen.core.services.wallets.open_wallet_command import OpenWallet
from lumen.core.services.wallets.deposit_funds_command import DepositFunds
from lumen.interfaces.enums.v1.currency import Currency

wallet_id: str = await command_bus.send(
    OpenWallet(owner_id="u-1", currency=Currency.EUR)
)
balance: int = await command_bus.send(
    DepositFunds(wallet_id=wallet_id, amount=1500)
)
```

`send` is a coroutine — always `await` it. The return value is whatever `do_handle` returned: a `str` wallet ID for `OpenWallet`, and the new balance as an `int` (minor units) for `DepositFunds` and `WithdrawFunds`. If anything in the pipeline fails — validation, authorization, or the handler itself — the exception wraps in `CommandProcessingException` and propagates out of `send`, where the global error handler maps it to the appropriate HTTP status code.

::: figure art/figures/07-cqrs.svg | Figure 7.1 — Commands flow to the write model; queries to the read model.

!!! spring "Spring parity"
    `CommandBus.send(command)` is the Python equivalent of Axon Framework's `CommandGateway.send(command)` or `CommandGateway.sendAndWait(command)`. Each command handler class corresponds to a method annotated with `@CommandHandler` in Axon, or a `@MessageHandler` in Spring Modulith's ApplicationEventPublisher model. The `@command_handler` decorator is PyFly's counterpart of `@CommandHandler`: it registers the handler with the registry by introspecting the generic type parameter, exactly as Axon resolves handler methods by parameter type. The `@service` stacking mirrors the fact that in Spring every `@CommandHandler` bean is also a Spring `@Component` — registration and injection are inseparable. The `@transactional()` decorator maps directly to Spring's `@Transactional`: both open a unit-of-work session, commit on success, and roll back on any exception — so `upsert` (backed by `session.merge`) is the Python analogue of `repository.save()` inside a `@Transactional` method.

---

## Queries and query handlers

Commands travel one direction: into the write model. Queries are the return journey — they ask the system for a projection of current state and expect an answer, not a side effect.

A **query** is a frozen dataclass that inherits from `Query[R]`, where `R` is the result type. Like commands, queries are immutable messages, but they carry no intent to change state. `query_bus.query(GetBalance(...))` loads fresh data from the repository and returns a typed DTO. Queries do not need `@transactional()` — reads do not mutate state, so there is nothing to commit or roll back.

Queries return **read DTOs** rather than domain aggregates. The separation is deliberate. If `GetWalletHandler` returned a `Wallet` aggregate, the API layer would be coupled to every field on the aggregate — a change to the domain model could silently break the API contract. A dedicated `WalletDto` Pydantic model projects exactly the fields the HTTP response needs. Add a field to `Wallet`? The projection changes only if you explicitly include it in the DTO. Remove a field from `Wallet`? The projection compiles until you clean it up.

The read side mirrors the write side step for step — query message, then query handler — but with two simplifications: no `@transactional()` (nothing to commit) and no event publishing (nothing changed).

**Step 5 — Write the single-lookup queries.** Create `get_wallet_query.py` and `get_balance_query.py`. Both carry only a `wallet_id`; what differs is what they promise to return.

::: listing lumen/core/services/wallets/get_wallet_query.py | Listing 7.7 — GetWallet: a single-lookup query returning a full WalletDto
from __future__ import annotations

from dataclasses import dataclass

from lumen.interfaces.dtos.v1.wallet_dto import WalletDto
from pyfly.cqrs import Query


@dataclass(frozen=True)
class GetWallet(Query[WalletDto | None]):
    """Look up a wallet by its identifier."""

    wallet_id: str
:::

::: listing lumen/core/services/wallets/get_balance_query.py | Listing 7.8 — GetBalance: a lighter query returning only the balance projection
from __future__ import annotations

from dataclasses import dataclass

from lumen.interfaces.dtos.v1.balance_dto import BalanceDto
from pyfly.cqrs import Query


@dataclass(frozen=True)
class GetBalance(Query[BalanceDto | None]):
    """Look up just the balance of a wallet by its identifier."""

    wallet_id: str
:::

Both queries carry only `wallet_id`. `GetWallet` returns a `WalletDto` — the full representation including `id`, `owner_id`, `currency`, `balance_minor`, `balance`, and `created_at`. `GetBalance` returns a `BalanceDto` — a lighter projection that omits `owner_id` and `created_at`. A balance poll does not need the owner; leaving those fields out saves bandwidth and avoids accidentally exposing account ownership in a response that callers may log. Keeping the two queries separate means you can tune each independently — caching, authorization, or a dedicated read store — without touching the other.

**Step 6 — Write the single-lookup query handlers.** The query handlers live under the same `wallets/` package as the commands. **The same `@query_handler` + `@service` stacking applies**: `@query_handler` registers the class with the handler registry; `@service` wires it into the DI container. Both decorators are required for the same reasons as on command handlers. Notice how much smaller these are than the command handlers — no session factory, no event publisher, just the repository and a one-line projection.

::: listing lumen/core/services/wallets/get_wallet_handler.py | Listing 7.9 — GetWalletHandler: find_by_id → entity_to_dto → return
from __future__ import annotations

from lumen.core.mappers.wallet_mapper import entity_to_dto
from lumen.core.services.wallets.get_wallet_query import GetWallet
from lumen.interfaces.dtos.v1.wallet_dto import WalletDto
from lumen.models.repositories.wallet_repository import WalletRepository
from pyfly.container import service
from pyfly.cqrs import QueryHandler, query_handler


@query_handler
@service
class GetWalletHandler(QueryHandler[GetWallet, WalletDto | None]):
    def __init__(self, repository: WalletRepository) -> None:
        super().__init__()
        self._repository = repository

    async def do_handle(self, query: GetWallet) -> WalletDto | None:  # type: ignore[override]
        entity = await self._repository.find_by_id(query.wallet_id)
        return entity_to_dto(entity) if entity is not None else None
:::

::: listing lumen/core/services/wallets/get_balance_handler.py | Listing 7.10 — GetBalanceHandler: @projection view via Mapper.project
from __future__ import annotations

from lumen.core.mappers.wallet_mapper import entity_to_balance_dto
from lumen.core.services.wallets.get_balance_query import GetBalance
from lumen.interfaces.dtos.v1.balance_dto import BalanceDto
from lumen.models.repositories.wallet_repository import WalletRepository
from pyfly.container import service
from pyfly.cqrs import QueryHandler, query_handler


@query_handler
@service
class GetBalanceHandler(QueryHandler[GetBalance, BalanceDto | None]):
    def __init__(self, repository: WalletRepository) -> None:
        super().__init__()
        self._repository = repository

    async def do_handle(self, query: GetBalance) -> BalanceDto | None:  # type: ignore[override]
        entity = await self._repository.find_by_id(query.wallet_id)
        return entity_to_balance_dto(entity) if entity is not None else None
:::

Both handlers delegate projection to `wallet_mapper` — the single module that owns the DTO shape. `entity_to_dto` fills in all six fields of `WalletDto` directly from the row. `entity_to_balance_dto` takes a different path: it calls `Mapper.project(entity, BalanceView)` against a `@projection`-marked interface that declares exactly the four fields the balance endpoint needs, with a registered transform that computes `balance` (major units) from `balance_minor`. The mapper copies only those declared fields — a read-side equivalent of Spring Data's interface projections. Neither handler touches the Pydantic model directly; a field rename touches one file.

### Paged and specification queries

The read side does not stop at single-resource lookups. Production systems need lists with pagination metadata and the ability to filter by runtime predicates. The framework handles both through the `Repository` base class.

!!! note "Pageable and Specification, in plain terms"
    A **`Pageable`** bundles three things a list endpoint needs: which page you want, how big each page is, and how to sort. A **`Specification`** is a reusable, composable filter — think of it as a `WHERE` clause you can build as an object and combine with `&` (and), `|` (or), and `~` (not) before it ever touches SQL. Both come from the framework's data layer; you do not write the SQL yourself.

**Step 7 — Write the list queries and handlers.** `ListWallets` wraps a `Pageable` (page number, size, sort) and asks the repository for a counted, sorted, limited slice. `ListRichWallets` adds a `min_minor` threshold and runs it through a composable `Specification`. Build the two query messages first, then their handlers.

::: listing lumen/core/services/wallets/list_wallets_query.py | Listing 7.11 — ListWallets: a Pageable-carrying query
from __future__ import annotations

from dataclasses import dataclass

from lumen.interfaces.dtos.v1.wallet_dto import WalletDto
from pyfly.data import Page, Pageable
from pyfly.cqrs import Query


@dataclass(frozen=True)
class ListWallets(Query[Page[WalletDto]]):
    """List wallets, one page at a time."""

    pageable: Pageable
:::

::: listing lumen/core/services/wallets/list_rich_wallets_query.py | Listing 7.12 — ListRichWallets: adds a balance threshold for Specification filtering
from __future__ import annotations

from dataclasses import dataclass

from lumen.interfaces.dtos.v1.wallet_dto import WalletDto
from pyfly.data import Page, Pageable
from pyfly.cqrs import Query


@dataclass(frozen=True)
class ListRichWallets(Query[Page[WalletDto]]):
    """List wallets whose balance is at least ``min_minor``, paged."""

    min_minor: int
    pageable: Pageable
:::

::: listing lumen/core/services/wallets/list_wallets_handler.py | Listing 7.13 — ListWalletsHandler: find_all(pageable) + Page.map
from __future__ import annotations

from lumen.core.mappers.wallet_mapper import entity_to_dto
from lumen.core.services.wallets.list_wallets_query import ListWallets
from lumen.interfaces.dtos.v1.wallet_dto import WalletDto
from lumen.models.repositories.wallet_repository import WalletRepository
from pyfly.container import service
from pyfly.cqrs import QueryHandler, query_handler
from pyfly.data import Page


@query_handler
@service
class ListWalletsHandler(QueryHandler[ListWallets, Page[WalletDto]]):
    def __init__(self, repository: WalletRepository) -> None:
        super().__init__()
        self._repository = repository

    async def do_handle(self, query: ListWallets) -> Page[WalletDto]:  # type: ignore[override]
        page = await self._repository.find_all(query.pageable)
        return page.map(entity_to_dto)
:::

::: listing lumen/core/services/wallets/list_rich_wallets_handler.py | Listing 7.14 — ListRichWalletsHandler: Specification + find_all_by_spec_paged
from __future__ import annotations

from lumen.core.mappers.wallet_mapper import entity_to_dto
from lumen.core.services.wallets.list_rich_wallets_query import ListRichWallets
from lumen.interfaces.dtos.v1.wallet_dto import WalletDto
from lumen.models.repositories.wallet_repository import WalletRepository
from pyfly.container import service
from pyfly.cqrs import QueryHandler, query_handler
from pyfly.data import Page


@query_handler
@service
class ListRichWalletsHandler(QueryHandler[ListRichWallets, Page[WalletDto]]):
    def __init__(self, repository: WalletRepository) -> None:
        super().__init__()
        self._repository = repository

    async def do_handle(self, query: ListRichWallets) -> Page[WalletDto]:  # type: ignore[override]
        page = await self._repository.find_rich(query.min_minor, query.pageable)
        return page.map(entity_to_dto)
:::

`find_all(pageable)` is inherited from the framework `Repository` base. It counts the total rows, applies the `Pageable`'s sort, and slices with `LIMIT`/`OFFSET` — returning a `Page[WalletEntity]` that carries `items`, `total`, `page`, `size`, `total_pages`, `has_next`, and `has_previous`. `Page.map(entity_to_dto)` transforms the items without touching the metadata. The controller wraps the result in a `PageDto` for the wire.

`find_rich` is defined on `WalletRepository` itself and delegates to the inherited `find_all_by_spec_paged`. It constructs a `Specification` — a composable `WHERE` predicate — and passes it alongside the `Pageable`. The framework appends the `WHERE` clause, the sort, and the `LIMIT`/`OFFSET`, then executes a count query for the total. The handler calls `repo.find_rich(query.min_minor, query.pageable)` and maps the page exactly as before.

Executing a query goes through `QueryBus.query`:

```python
from pyfly.cqrs import DefaultQueryBus
from lumen.core.services.wallets.get_balance_query import GetBalance

balance_dto = await query_bus.query(GetBalance(wallet_id="wlt-001"))
```

The return value is whatever `do_handle` returned — a `BalanceDto` or `None`. `None` means the wallet was not found. The controller is responsible for translating that into HTTP 404, keeping HTTP concerns out of the handler.

!!! note "Queries return None, not exceptions"
    Query handlers return `None` when the resource is not found rather than raising `AggregateNotFound`. This is a deliberate convention: a query that finds nothing is not an error — it is an answer. The controller turns a `None` result into a 404 response, keeping the HTTP concern out of the handler.

!!! note "What just happened"
    Both sides of CQRS now exist. Three commands and three handlers change state; four queries and four handlers read it. None of them know about HTTP, and none of them are registered by hand — the decorators do that at startup. The only thing missing is the HTTP boundary that turns a web request into a message and a message result into a web response. That is the controller, which you build next.

**Run it — confirm every handler is registered.** Before touching the controller, prove the bus discovered all your handlers. Start the app, then ask the CQRS health indicator how many handlers it found. From the `samples/lumen` directory:

::: listing terminal | Listing 7.14a — Start Lumen
uv run pyfly run --server uvicorn
:::

In a second terminal, query the actuator health endpoint. In v26.6.110 the actuator lives on its own management port, **9090** by default — not the app's 8080:

::: listing terminal | Listing 7.14b — Count the registered handlers
curl -s localhost:9090/actuator/health | python -m json.tool
:::

Look for the `cqrs_health_indicator` block. With every command and query handler from this chapter in place it reports three command handlers and four query handlers:

```json
"cqrs_health_indicator": {
  "status": "UP",
  "details": {"command_handlers": 3, "query_handlers": 4}
}
```

If a count is lower than you expect, a handler is missing one of its two decorators and the registry never mapped it. Stop the server with `Ctrl-C` when you are done.

!!! note "The actuator lives on its own port now"
    In PyFly v26.6.110 the business API and the actuator run on **separate** ports — Spring-style. Your wallet endpoints listen on `pyfly.server.port` (default **8080**), while actuator endpoints and the admin dashboard listen on `pyfly.management.server.port` (default **9090**), which is open and unauthenticated by default. Lumen keeps the defaults, so health is at `localhost:9090/actuator/health` and the wallet API is at `localhost:8080/api/v1/wallets`. The default actuator HTTP exposure is `health,info`; expose more via `pyfly.management.endpoints.web.exposure.include`. Lock the management port down in production with `pyfly.management.security.enabled: true`, or disable it entirely with `pyfly.management.server.port: -1`.

---

## Wiring the bus into the controller

The controller is the system's HTTP boundary. Its only job is to translate an HTTP request into a domain message and map the result back to an HTTP response. Everything in between belongs to the bus and the handlers — and that boundary becomes much cleaner once the controller dispatches commands and queries instead of calling service methods directly.

Before CQRS, `WalletController` held a reference to `WalletApplicationService` and called methods on it directly. Every time the service interface changed — a new parameter, a renamed method, a different return type — the controller had to change too. With CQRS, the controller knows one thing: what message to send.

The controller injects `DefaultCommandBus` and `DefaultQueryBus` by type — the **concrete bus classes**, not abstract protocols. This is the correct import:

```python
from pyfly.cqrs import DefaultCommandBus, DefaultQueryBus
```

Why concrete classes? PyFly's CQRS auto-configuration registers exactly one instance of each bus in the DI container. Injecting by the concrete type is unambiguous — no protocol dispatch, and the type checker sees the full `send` / `query` surface. Using a protocol alias would require an explicit container binding; the concrete type works out of the box.

### Route ordering: why the single-resource handlers are named `wallet_*`

The framework registers a controller's routes in **alphabetical method-name order**; Starlette's router then applies first-registered-wins matching. This means a literal segment like `/rich` must be registered *before* the path variable `/{wallet_id}` — otherwise every `GET /api/v1/wallets/rich` request would match the variable route and look for a wallet whose id is the string `"rich"`.

The collection handlers are named `list_wallets` and `list_rich_wallets`; the single-resource handlers are named `wallet_detail` and `wallet_balance`. Alphabetically, `l` sorts before `w`, so the collection routes (`GET /`, `GET /rich`) are always registered ahead of the parameterised routes (`GET /{wallet_id}`, `GET /{wallet_id}/balance`). If you rename `wallet_detail` to something that sorts before `list_*`, the `/rich` route will silently break.

**Step 8 — Wire the buses into the controller.** Replace the old `WalletApplicationService` dependency with the two buses, then turn each endpoint into a one-liner: build a command or query from the request, dispatch it, return the result. Here is the complete controller.

::: listing lumen/web/controllers/wallet_controller.py | Listing 7.15 — WalletController: DefaultCommandBus + DefaultQueryBus + paged list endpoints
from __future__ import annotations

from lumen.core.services.wallets.deposit_funds_command import DepositFunds
from lumen.core.services.wallets.get_balance_query import GetBalance
from lumen.core.services.wallets.get_wallet_query import GetWallet
from lumen.core.services.wallets.list_rich_wallets_query import ListRichWallets
from lumen.core.services.wallets.list_wallets_query import ListWallets
from lumen.core.services.wallets.open_wallet_command import OpenWallet
from lumen.core.services.wallets.withdraw_funds_command import WithdrawFunds
from lumen.interfaces.dtos.v1.balance_dto import BalanceDto
from lumen.interfaces.dtos.v1.deposit_request import DepositRequest
from lumen.interfaces.dtos.v1.open_wallet_request import OpenWalletRequest
from lumen.interfaces.dtos.v1.page_dto import PageDto
from lumen.interfaces.dtos.v1.wallet_dto import WalletDto
from pyfly.container import rest_controller
from pyfly.cqrs import DefaultCommandBus, DefaultQueryBus
from pyfly.data import Pageable, Sort
from pyfly.kernel import ResourceNotFoundException
from pyfly.web import (
    Body, PathVar, QueryParam, Valid,
    get_mapping, post_mapping, request_mapping,
)

#: Newest-first ordering shared by the list endpoints.
_NEWEST_FIRST = Sort.by("created_at").descending()


@rest_controller
@request_mapping("/api/v1/wallets")
class WalletController:
    """Digital-wallet REST API: open, deposit, withdraw, list, inspect."""

    def __init__(
        self, commands: DefaultCommandBus, queries: DefaultQueryBus
    ) -> None:
        self._commands = commands
        self._queries = queries

    # --- commands --------------------------------------------------------

    @post_mapping("", status_code=201)
    async def open_wallet(
        self, request: Valid[Body[OpenWalletRequest]]
    ) -> dict[str, str]:
        wallet_id = await self._commands.send(
            OpenWallet(owner_id=request.owner_id, currency=request.currency)
        )
        return {"wallet_id": wallet_id}

    @post_mapping("/{wallet_id}/deposit")
    async def deposit(
        self,
        wallet_id: PathVar[str],
        request: Valid[Body[DepositRequest]],
    ) -> dict[str, int | str]:
        balance = await self._commands.send(
            DepositFunds(wallet_id=wallet_id, amount=request.amount)
        )
        return {"wallet_id": wallet_id, "balance_minor": balance}

    @post_mapping("/{wallet_id}/withdraw")
    async def withdraw(
        self,
        wallet_id: PathVar[str],
        request: Valid[Body[DepositRequest]],
    ) -> dict[str, int | str]:
        balance = await self._commands.send(
            WithdrawFunds(wallet_id=wallet_id, amount=request.amount)
        )
        return {"wallet_id": wallet_id, "balance_minor": balance}

    # --- paged / specification queries (registered before /{wallet_id}) --

    @get_mapping("")
    async def list_wallets(
        self, page: QueryParam[int] = 1, size: QueryParam[int] = 20
    ) -> PageDto[WalletDto]:
        result = await self._queries.query(
            ListWallets(pageable=Pageable.of(page, size, _NEWEST_FIRST))
        )
        return PageDto.from_page(result)

    @get_mapping("/rich")
    async def list_rich_wallets(
        self,
        min_minor: QueryParam[int] = 0,
        page: QueryParam[int] = 1,
        size: QueryParam[int] = 20,
    ) -> PageDto[WalletDto]:
        result = await self._queries.query(
            ListRichWallets(
                min_minor=min_minor,
                pageable=Pageable.of(page, size, _NEWEST_FIRST),
            )
        )
        return PageDto.from_page(result)

    # --- single-wallet queries (named wallet_* so they sort after list_*) -

    @get_mapping("/{wallet_id}")
    async def wallet_detail(self, wallet_id: PathVar[str]) -> WalletDto:
        result = await self._queries.query(GetWallet(wallet_id=wallet_id))
        if result is None:
            raise ResourceNotFoundException(
                f"Wallet {wallet_id!r} not found",
                code="WALLET_NOT_FOUND",
                context={"wallet_id": wallet_id},
            )
        return result

    @get_mapping("/{wallet_id}/balance")
    async def wallet_balance(self, wallet_id: PathVar[str]) -> BalanceDto:
        result = await self._queries.query(
            GetBalance(wallet_id=wallet_id)
        )
        if result is None:
            raise ResourceNotFoundException(
                f"Wallet {wallet_id!r} not found",
                code="WALLET_NOT_FOUND",
                context={"wallet_id": wallet_id},
            )
        return result
:::

Compare the constructor to its pre-CQRS form. Before, the controller took `WalletApplicationService` — a concrete class whose method signatures leaked business logic into the HTTP layer. Now it takes `DefaultCommandBus` and `DefaultQueryBus` — two opaque channels. The controller knows *what* to send; it knows nothing about *how* the message is processed.

Look at `open_wallet`. Before, it called `self._service.open_wallet(owner_id=..., currency=...)` — a positional contract that breaks whenever the service grows a new parameter. Now it constructs `OpenWallet(owner_id=request.owner_id, currency=request.currency)` — a named, immutable object whose fields are its own API. Add a field to the command? The controller stays the same until you choose to populate it.

The request DTOs (`OpenWalletRequest`, `DepositRequest`) are Pydantic models in `lumen/interfaces/dtos/v1/`. `OpenWalletRequest` validates `owner_id` length and constrains `currency` to the `Currency` enum. `DepositRequest` is shared by both the deposit and withdraw endpoints — both move a positive `amount` in the wallet's own currency. Field-level constraints in those DTOs are enforced by `Valid[Body[...]]` before the handler is ever called.

The paged list endpoints (`list_wallets`, `list_rich_wallets`) build a `Pageable` from the query-string parameters, dispatch the query through the bus, and wrap the resulting `Page[WalletDto]` in a `PageDto` for the wire. `PageDto` is a Pydantic model that mirrors all the `Page` metadata fields — `total`, `total_pages`, `has_next`, `has_previous` — so clients get consistent pagination envelopes without a custom serializer.

The `wallet_detail` and `wallet_balance` methods show the only remaining HTTP concern in the controller: translating a `None` query result into a 404 via `ResourceNotFoundException`. That mapping belongs here because 404 is an HTTP status code and the handler deliberately has no HTTP knowledge. Return types are declared as `WalletDto` and `BalanceDto` — Pydantic models the framework serializes to JSON automatically.

!!! tip "Let the bus raise"
    You do not need to catch `CommandProcessingException` or `QueryProcessingException` in the controller unless you want to customize the error shape. The global exception handler maps `AggregateNotFound` to 404 and `BusinessRuleViolation` to 422 — the same as before. The bus exceptions propagate those originals transparently.

**Run it — drive the full HTTP path.** The vertical slice is complete: HTTP request → command/query → bus → handler → domain → repository → response. Prove it from the outside. Start the app (`uv run pyfly run --server uvicorn`), then in a second terminal open a wallet:

::: listing terminal | Listing 7.15a — Open a wallet over HTTP
curl -s -X POST localhost:8080/api/v1/wallets \
  -H 'content-type: application/json' \
  -d '{"owner_id":"u-1","currency":"EUR"}'
:::

The `open_wallet` endpoint dispatches `OpenWallet` and returns the generated id:

```json
{"wallet_id": "wlt-c5bbb2a7-dd49-4321-932e-e4c6bfa5cc2c"}
```

Copy that id, deposit into it, then read the balance back:

::: listing terminal | Listing 7.15b — Deposit, then read the balance
curl -s -X POST localhost:8080/api/v1/wallets/wlt-c5bbb2a7-dd49-4321-932e-e4c6bfa5cc2c/deposit \
  -H 'content-type: application/json' -d '{"amount":1500}'

curl -s localhost:8080/api/v1/wallets/wlt-c5bbb2a7-dd49-4321-932e-e4c6bfa5cc2c/balance
:::

The deposit echoes the new balance in minor units; the balance query returns the `BalanceDto`:

```json
{"wallet_id": "wlt-c5bbb2a7-dd49-4321-932e-e4c6bfa5cc2c", "balance_minor": 1500}
{"wallet_id": "wlt-c5bbb2a7-dd49-4321-932e-e4c6bfa5cc2c", "balance_minor": 1500, "balance": 15.0}
```

Finally, confirm the route-ordering decision from earlier pays off — list the wallets and the "rich" subset:

::: listing terminal | Listing 7.15c — Both list routes resolve correctly
curl -s 'localhost:8080/api/v1/wallets?page=1&size=20'
curl -s 'localhost:8080/api/v1/wallets/rich?min_minor=1000'
:::

Both return a `PageDto` envelope (`items`, `total`, `total_pages`, `has_next`, `has_previous`). The `/rich` call resolves to `list_rich_wallets`, *not* to `wallet_detail` looking for a wallet whose id is the literal string `"rich"` — exactly because `list_*` sorts before `wallet_*`. If `/rich` ever returns a 404, that ordering has been broken; revisit the method-name rule above.

!!! warning "Use a real id"
    The wallet id above is illustrative — yours will differ on every `open_wallet` call. Paste the id returned by your own `POST /api/v1/wallets` into the deposit and balance URLs, or you will get a 404.

---

## The handler pipeline

A single `send` or `query` call triggers more than just the handler. Understanding the pipeline tells you where to put each cross-cutting concern — and, just as importantly, where *not* to put it.

!!! note "What is a 'pipeline'?"
    A **pipeline** is just a fixed sequence of steps the bus runs around your handler — like an assembly line. Your message enters one end, passes through validation and authorization, gets handled, and (for commands) has its events published, before the result comes back out. You write only the one step in the middle (`do_handle`); the bus owns the rest, identically for every message.

The pipeline is defined once, in the bus, and applies uniformly to every handler. You never write pipeline logic inside a handler. The order is strict:

| Step | Where it is defined | Applies to | Failure result |
|---|---|---|---|
| Business pre-condition validation | `validate()` hook on the message | Commands + Queries | `CqrsValidationException` (HTTP 422) |
| Authorization | `authorize()` hook on the message | Commands + Queries | `AuthorizationException` (HTTP 403) |
| Handler execution | `do_handle()` | Commands + Queries | Domain exceptions (4xx/5xx) |
| Domain event publishing | Bus pipeline (post-handler) | Commands only | — |
| Correlation ID cleanup | Bus pipeline (finally block) | Commands + Queries | — |

### Validation

Without a structured validation step, every handler would open with its own guard clauses: check this field is not blank, check that amount is positive. That logic would be duplicated across handlers and tested only through integration paths. Centralizing validation in the message itself solves both problems.

The bus invokes `validate()` before looking up the handler. If validation fails, the bus raises `CqrsValidationException` without ever reaching the handler.

The validation hook is also the right place for cross-field pre-conditions knowable from the fields alone — too simple for the domain aggregate, too application-specific for the request model:

```python
@dataclass(frozen=True)
class DepositFunds(Command[int]):
    wallet_id: str
    amount: int

    async def validate(self) -> ValidationResult:
        if not self.wallet_id.strip():
            return ValidationResult.failure(
                "wallet_id", "Wallet id is required"
            )
        if self.amount <= 0:
            return ValidationResult.failure(
                "amount", "Deposit amount must be > 0"
            )
        return ValidationResult.success()
```

### Authorization

Once a message is structurally valid, the bus asks: is the caller *allowed* to perform this operation? Authorization answers before any database access happens — more efficient and safer, since you never load sensitive data only to discard it because the caller lacked permission.

Both commands and queries expose an `authorize()` hook. Return `AuthorizationResult.success()` to allow execution, or `AuthorizationResult.failure(resource, message)` to deny it. The bus raises `AuthorizationException` on denial, mapping to HTTP 403 via the global error handler.

A clean rule of thumb: use `authorize()` on the command for **operation-level** checks — who is allowed to call this command at all — and leave **resource-level** decisions (can this caller access *this specific* wallet?) to the handler, which has the loaded aggregate in scope:

::: listing lumen/cqrs/commands_auth.py | Listing 7.16 — Authorization hook on a command
from __future__ import annotations
from dataclasses import dataclass

from pyfly.cqrs.authorization.types import AuthorizationResult
from pyfly.cqrs import Command


@dataclass(frozen=True)
class CloseWallet(Command[None]):
    """Close a wallet.  Only internal service accounts may do this."""
    wallet_id: str
    requested_by: str

    async def authorize(self) -> AuthorizationResult:
        internal_accounts = {"ops-service", "compliance-bot"}
        if self.requested_by not in internal_accounts:
            return AuthorizationResult.failure(
                "wallet",
                "Only internal service accounts may close wallets",
            )
        return AuthorizationResult.success()
:::

`CloseWallet.authorize` checks a known set of internal service accounts. If `requested_by` is not in the set, authorization fails before the handler is called. The set would normally come from a configuration value or a token claim injected at the controller boundary — it is hardcoded here for readability. The key point is that the check lives inside the command, not scattered across handler code.

### Distributed tracing

When one HTTP request triggers multiple commands — and each command may call downstream services — you need a way to stitch all the logs and spans together. That is what `CorrelationContext` provides.

Both buses set a correlation ID at the start of every pipeline execution. If the message carries an ID already (set via `command.set_correlation_id(id)`), that ID is used; otherwise a new UUID is generated. The prior ID is always restored in a `finally` block, so nested command dispatches within the same request do not clobber the outer trace.

`CorrelationContext` propagates across `await` chains via Python's `contextvars` — no need to thread the ID manually through every function argument. For cross-service propagation, serialize the context to outgoing headers and restore it on the receiving side:

```python
from pyfly.cqrs.tracing.correlation import CorrelationContext

# On the sending side
headers = CorrelationContext.create_context_headers()
# {"X-Correlation-ID": "...", "X-Trace-ID": "...", "X-Span-ID": "..."}

# On the receiving side
CorrelationContext.extract_context_from_headers(headers)
```

The three headers — `X-Correlation-ID`, `X-Trace-ID`, and `X-Span-ID` — follow W3C Trace Context naming, so they are compatible with OpenTelemetry-instrumented infrastructure out of the box.

!!! tip "Where to put cross-cutting logic"
    The bus pipeline is the right home for concerns that apply to *all* operations: validation, authorization, tracing, and metrics. The handler is the right home for concerns specific to *one* operation: loading the aggregate, driving behaviour, saving, draining events. If you find yourself adding a try/except to every handler, or copying the same pre-condition check into multiple handlers, it belongs in the pipeline — either as a `validate()` hook on the command or as a bus-level service. The pipeline scales uniformly; handler boilerplate does not.

---

## What you built {.recap}

Part II is complete. Lumen now has a full vertical slice from HTTP to domain and back — one built on architectural decisions that will scale without rewriting.

In Chapter 5 you gave the system persistence: a `WalletRepository` subclassing `Repository[WalletEntity, str]` — the framework's Spring-Data-style generic repository that provides `find_by_id`, `find_all(pageable)`, `find_all_by_spec_paged`, and more out of the box, with the `AsyncSession` injected by relational auto-configuration. In Chapter 6 you promoted the wallet to a proper DDD aggregate: `Money` as an immutable value object, `Wallet(AggregateRoot[str])` as the consistency boundary enforcing the overdraft, currency-match, and positive-amount invariants, with `WalletOpened`, `FundsDeposited`, and `FundsWithdrawn` domain events buffered in the aggregate and drained to the event bus after a successful save.

In this chapter you separated the write model from the read model. `OpenWallet`, `DepositFunds`, and `WithdrawFunds` are frozen, validated command messages that flow through `DefaultCommandBus` — a pipeline that runs validation, authorization, handler execution, domain event publishing, and distributed tracing automatically for every command. Each command handler carries `@transactional()` on `do_handle`: the decorator opens a committed unit of work from `self._session_factory`, swaps the session onto the repository, commits on success, and rolls back on failure. Persistence goes through `repository.upsert` — backed by `session.merge` — so INSERT and UPDATE share a single code path keyed on the aggregate's own id.

`GetWallet` and `GetBalance` are query messages that flow through `DefaultQueryBus` — the same pipeline without the event-publishing step, and without `@transactional()` because reads do not commit. `GetBalanceHandler` projects through a `@projection`-marked `BalanceView` interface and `Mapper.project`, copying only the declared fields and applying a registered major-unit transform. `ListWallets` and `ListRichWallets` round out the query side: `find_all(pageable)` returns a counted, sorted, offset-limited `Page[WalletEntity]`; `find_all_by_spec_paged` runs a composable `Specification` predicate on top of the same pagination machinery. Both use `Page.map(entity_to_dto)` to project items without touching the metadata.

Each handler carries the `@command_handler` + `@service` (or `@query_handler` + `@service`) stack: the first decorator registers the class by introspecting its generic type argument; the second wires it into the DI container so constructor dependencies are injected automatically.

`WalletController` no longer knows about the service layer. It injects `DefaultCommandBus` and `DefaultQueryBus`, builds a command or query from the HTTP request, dispatches it, and either returns the result or raises a domain exception. Single-resource handler methods are named `wallet_detail` and `wallet_balance` — a deliberate choice so they sort alphabetically *after* the collection methods `list_wallets` and `list_rich_wallets`, ensuring the literal `/rich` segment is registered before the `/{wallet_id}` variable route.

Adding a new command now means three things: define a frozen dataclass, implement one `do_handle` decorated with `@command_handler` + `@service` and annotated with `@transactional()`, and add one endpoint that calls `self._commands.send`. The pipeline applies automatically.

**Run it — the whole chapter, in one command.** From the `samples/lumen` directory, run the CQRS flow tests one last time to confirm every piece you built this chapter still hangs together:

::: listing terminal | Listing 7.17 — Verify the full CQRS slice
uv run --extra dev pytest tests/test_cqrs_flow.py -q
:::

All five scenarios pass — the happy-path lifecycle, the not-found query, the rejected overdraw, the rejected non-positive deposit, and the rejected deposit to an unknown wallet:

```
5 passed in 0.61s
```

Those last four cases are worth pausing on: each one exercises the *pipeline*, not the handler. The overdraw is refused by the aggregate and surfaces as `CommandProcessingException`; the non-positive deposit never reaches a handler at all because `validate()` rejects it first. You wrote zero error-handling code to get any of that.

---

## Try it yourself {.exercises}

1. **Trace the full lifecycle in the test suite.** Open `samples/lumen/tests/test_cqrs_flow.py` and run it against a real database using Testcontainers (Chapter 11). The test `test_full_wallet_lifecycle` opens a wallet, deposits 1 500 minor units, withdraws 500, then queries both `GetWallet` and `GetBalance`. Step through it with a debugger: confirm that `wallet.clear_events()` drains the `FundsDeposited` and `FundsWithdrawn` events after each `upsert` call, and that `GetWallet` returns a `WalletDto` with `balance_minor == 1000` and `balance == 10.0`.

2. **Observe `upsert` vs `save`.** In a test, call `DepositFunds` twice on the same wallet without `@transactional()` and observe the `IntegrityError`. Then restore `@transactional()` and verify both deposits commit. Open `WalletRepository.upsert` and trace how `session.merge` resolves the primary-key conflict that a plain `INSERT` would raise.

3. **Add a `ListByOwner` query.** Define `ListByOwner(Query[list[WalletDto]])` with an `owner_id: str` field. Implement `ListByOwnerHandler` — decorated with `@query_handler` + `@service` — that calls `WalletRepository.find_by_owner_id(query.owner_id)` (the derived query stub already exists) and maps the result list with `entity_to_dto`. Add a `GET /api/v1/wallets/by-owner/{owner_id}` endpoint to `WalletController`. Ensure the new endpoint method name sorts before `wallet_detail` so Starlette matches the literal `/by-owner/…` segment first.

4. **Add authorization to `WithdrawFunds`.** Extend `WithdrawFunds` with an `initiated_by: str` field. Override `authorize()` to return `AuthorizationResult.failure("withdraw", "Initiator is required")` when `initiated_by` is blank, and `AuthorizationResult.success()` otherwise. Update `WithdrawFundsHandler.do_handle` to record `command.initiated_by` in the `FundsWithdrawn` event payload. Write a test that calls `await WithdrawFunds(wallet_id="wlt-1", amount=100, initiated_by="").authorize()` and asserts that the result denies authorization.
