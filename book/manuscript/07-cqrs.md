<span class="eyebrow">Chapter 7</span>

# CQRS: Commands & Queries {.chtitle}

::: figure art/openers/ch07.svg | &nbsp;

Lumen's wallet is now a first-class citizen of the domain. The `Wallet` aggregate enforces its own invariants, emits domain events, and persists through a clean repository boundary. Chapter 6's controller, though, still calls `WalletApplicationService` directly — one method per operation, reads and writes sharing the same code path. That design is fine at small scale, but it starts to show friction as the system grows. The team wants to cache wallet balances. They want a single audit trail for every write. They want to add authorization rules to specific operations without tangling them with the business logic in the service. And they want to test each piece of that logic in isolation.

**CQRS** — Command Query Responsibility Segregation — addresses all of this by drawing a bright line between the two things a service can do: *change state* and *read state*. Writes become **commands**: strongly typed, named, immutable messages that flow through a `CommandBus`. Reads become **queries**: equally typed messages that flow through a `QueryBus`. Each bus runs a pipeline — validation, authorization, execution, then (for commands) domain event publishing, and (for queries) optional caching. Your handler implements exactly one intent; the bus handles everything else.

By the end of this chapter Lumen's controller dispatches commands and queries instead of calling the service directly. `OpenWallet`, `DepositFunds`, and `WithdrawFunds` travel the command path; `GetWallet` and `GetBalance` travel the query path. The invariant-enforcing `Wallet` aggregate you built in Chapter 6 remains untouched — CQRS is not a replacement for the domain model, it is the delivery mechanism for instructions to that model.

---

## Why separate reads from writes

Picture Lumen at the end of Chapter 6. `WalletController` calls `WalletApplicationService.credit(wallet_id, amount)`. That call mutates state, but nothing in the method signature makes that obvious. Now the team wants to add a balance cache. Where does it go? Inside `credit`? In a decorator around the service? The question itself reveals the problem: a single service method is asked to serve two masters — the write path, which must always touch the database, and the read path, which should avoid it whenever possible. Bolting caching onto a write method is awkward at best and dangerous at worst.

Writes and reads have fundamentally different shapes. A write arrives with an intent and data: "deposit 1 500 minor units into wallet wlt-001". A read arrives with a question: "what is the current balance of wallet wlt-001?" The first must reach the database every time. The second is repeatable — asking twice should return the same answer without doubling the database load. Running both through the same method conflates concerns that scale differently, test differently, and need different cross-cutting behaviour.

The deeper benefit is **clarity of intent**. When a future teammate reads `wallet_service.credit(wallet_id, amount)`, they must inspect the implementation to know whether it is safe to call twice, whether it publishes events, and whether it is idempotent. When they read `DepositFunds(wallet_id=..., amount=...)`, the intent is unambiguous — and if the intent turns out to be wrong, you rename the command, not the service signature.

There are three concrete benefits that matter for Lumen.

**Independent scaling.** Reads typically outnumber writes by an order of magnitude or more. Once the command and query paths are separate, the bus can cache query results without touching the write path at all. You can route queries to a read replica and commands to the primary database with a configuration change rather than a code change.

**Focused handlers.** Each handler implements exactly one operation. `DepositFundsHandler` knows how to load a wallet, drive it through its domain behaviour, persist it, and drain its events — nothing more. `GetBalanceHandler` loads one wallet and returns a lightweight projection — nothing more. Because handlers are plain Python classes with injected dependencies, you can unit-test each one in complete isolation from the HTTP layer.

**Centralized cross-cutting concerns.** Validation, authorization, and distributed tracing are implemented once in the bus pipeline and apply uniformly to every handler — no boilerplate required in the handler itself. Adding per-operation authorization later is a matter of overriding `authorize()` on the command; the bus ensures it runs before `do_handle` is ever reached.

---

## Commands and command handlers

Before you write a single line of handler code, decide what your system's intentions are. In Lumen's wallet domain there are three things that can happen: a wallet can be opened, funds can be deposited, and funds can be withdrawn. Each of those is a **command** — a named, immutable message that expresses one intent. The bus delivers it; the handler acts on it; the domain aggregate enforces the rules. Commands are not method calls dressed up as objects: they are explicit contracts that live in your codebase as first-class citizens.

A **command** is a frozen dataclass that inherits from `Command[R]`, where `R` is the type the handler returns. The generic parameter is documentation and a type-checker hint; the bus does not enforce it at runtime.

Lumen's commands live in three separate files under `lumen/core/services/wallets/`, one per intent. Here is the first — `OpenWallet`:

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

And the deposit and withdrawal commands:

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

Four design choices are baked into every command, and it is worth being explicit about each one.

`frozen=True` makes the dataclass immutable the moment it is constructed. You cannot accidentally mutate a field in one layer of the pipeline before it reaches another. Immutable messages are also hashable by default, which matters if you ever want to store or compare them in tests.

`validate()` is an async hook that runs in the bus *before* the handler is dispatched. `OpenWallet.validate` checks that `owner_id` is not blank — the structural guarantee that the command carries a non-empty owner. `DepositFunds.validate` and `WithdrawFunds.validate` check that the amount is positive. These are pre-conditions that belong to the command itself — they do not require a database lookup, and they do not belong in the domain aggregate either. The aggregate enforces invariants that require loaded state (overdraft, currency match). Commands enforce invariants that are knowable from the fields alone. Keeping these two layers of validation separate means your aggregate is never called with data that is structurally wrong.

Notice that `DepositFunds` and `WithdrawFunds` carry only `wallet_id` and `amount` — no `currency` field. The wallet's own currency is the only valid currency for a deposit or withdrawal, and the repository knows that once the aggregate is loaded. Carrying a currency on the command would invite mismatches; the aggregate enforces the invariant from its own state instead.

Naming follows the **imperative mood** from the caller's perspective: `DepositFunds`, not `WalletDeposit` or `DepositFundsCommand`. This convention makes the command log read like a business audit trail — a sequence of things that *happened* — rather than a list of technical operations.

### Implementing a command handler

A command handler inherits from `CommandHandler[C, R]` and implements exactly one method: `do_handle`. You write the *what*; the bus wraps it with the *how*.

**Two decorators appear on every handler, and both are required.** `@command_handler` registers the class with the `HandlerRegistry` by introspecting the first generic type argument — no manual registration call is needed. `@service` wires the handler into PyFly's DI container so that constructor arguments are resolved and injected automatically when the application starts. The order matters: `@command_handler` goes on top, `@service` directly below it. Without `@service`, the DI container never instantiates the class and the bus cannot find the handler; without `@command_handler`, the registry never maps the command type to the class. Omitting either decorator is a silent failure — the bus raises "no handler found" at dispatch time.

Here is `OpenWalletHandler`:

::: listing lumen/core/services/wallets/open_wallet_handler.py | Listing 7.4 — OpenWalletHandler: @command_handler + @service stacking
from __future__ import annotations

from lumen.core.services.wallets.event_publishing import publish_domain_events
from lumen.core.services.wallets.open_wallet_command import OpenWallet
from lumen.models.entities.v1.wallet_entity import Wallet
from lumen.models.repositories.wallet_repository import WalletRepository
from pyfly.container import service
from pyfly.cqrs import CommandHandler, command_handler
from pyfly.eda import EventPublisher


@command_handler
@service
class OpenWalletHandler(CommandHandler[OpenWallet, str]):
    """Open a new, empty wallet."""

    def __init__(
        self, repository: WalletRepository, events: EventPublisher
    ) -> None:
        super().__init__()
        self._repository = repository
        self._events = events

    async def do_handle(  # type: ignore[override]
        self, command: OpenWallet
    ) -> str:
        wallet_id = await self._repository.next_id()
        wallet = Wallet.open(
            wallet_id=wallet_id,
            owner_id=command.owner_id,
            currency=command.currency,
        )
        await self._repository.add(wallet)
        await publish_domain_events(self._events, wallet.clear_events())
        return wallet_id
:::

Walk through the handler method step by step. `self._repository.next_id()` allocates a stable, prefixed identifier (the repository hands these out deterministically — `wlt-<uuid4>`). `Wallet.open(...)` calls the factory method on the aggregate, which enforces the non-empty owner pre-condition and raises a `WalletOpened` domain event into the aggregate's internal buffer. `self._repository.add(wallet)` persists the aggregate. Then `wallet.clear_events()` drains those buffered domain events and hands them to `publish_domain_events`, which forwards each one to the EDA bus so downstream listeners — the audit projection in this case — can react. The handler returns the wallet ID, which flows back to the controller as the `send` return value.

Notice the constructor: `super().__init__()` is an explicit call required by `CommandHandler`. Skip it and the base-class bookkeeping — correlation context, lifecycle hooks — is never initialized. The constructor also receives an `EventPublisher` alongside the repository. Both are injected by the DI container from their type hints; no factory configuration is required.

Here are the deposit and withdrawal handlers:

::: listing lumen/core/services/wallets/deposit_funds_handler.py | Listing 7.5 — DepositFundsHandler: load, act, save, drain events
from __future__ import annotations

from lumen.core.services.wallets.deposit_funds_command import DepositFunds
from lumen.core.services.wallets.event_publishing import publish_domain_events
from lumen.models.entities.v1.money import Money
from lumen.models.repositories.wallet_repository import WalletRepository
from pyfly.container import service
from pyfly.cqrs import CommandHandler, command_handler
from pyfly.domain import AggregateNotFound
from pyfly.eda import EventPublisher


@command_handler
@service
class DepositFundsHandler(CommandHandler[DepositFunds, int]):
    """Credit funds to an existing wallet; returns the new balance."""

    def __init__(
        self, repository: WalletRepository, events: EventPublisher
    ) -> None:
        super().__init__()
        self._repository = repository
        self._events = events

    async def do_handle(  # type: ignore[override]
        self, command: DepositFunds
    ) -> int:
        wallet = await self._repository.find(command.wallet_id)
        if wallet is None:
            raise AggregateNotFound("Wallet", command.wallet_id)
        wallet.deposit(Money(amount=command.amount, currency=wallet.currency))
        await self._repository.add(wallet)
        await publish_domain_events(self._events, wallet.clear_events())
        return wallet.balance.amount
:::

::: listing lumen/core/services/wallets/withdraw_funds_handler.py | Listing 7.6 — WithdrawFundsHandler: identical pattern, overdraft refused by the aggregate
from __future__ import annotations

from lumen.core.services.wallets.event_publishing import publish_domain_events
from lumen.core.services.wallets.withdraw_funds_command import WithdrawFunds
from lumen.models.entities.v1.money import Money
from lumen.models.repositories.wallet_repository import WalletRepository
from pyfly.container import service
from pyfly.cqrs import CommandHandler, command_handler
from pyfly.domain import AggregateNotFound
from pyfly.eda import EventPublisher


@command_handler
@service
class WithdrawFundsHandler(CommandHandler[WithdrawFunds, int]):
    """Debit funds from an existing wallet; returns the new balance."""

    def __init__(
        self, repository: WalletRepository, events: EventPublisher
    ) -> None:
        super().__init__()
        self._repository = repository
        self._events = events

    async def do_handle(  # type: ignore[override]
        self, command: WithdrawFunds
    ) -> int:
        wallet = await self._repository.find(command.wallet_id)
        if wallet is None:
            raise AggregateNotFound("Wallet", command.wallet_id)
        wallet.withdraw(Money(amount=command.amount, currency=wallet.currency))
        await self._repository.add(wallet)
        await publish_domain_events(self._events, wallet.clear_events())
        return wallet.balance.amount
:::

`DepositFundsHandler` and `WithdrawFundsHandler` follow the classic command handler pattern: load, guard, act, save, drain. The `AggregateNotFound` guard means you never pass a `None` wallet to `deposit` or `withdraw` — the bus translates the exception to 404 before the controller ever sees it. The `Money` value object is constructed from the command's `amount` and the *wallet's* currency — not from a currency field on the command — because the wallet owns that invariant. If `wallet.withdraw` refuses (balance would go negative), it raises `BusinessRuleViolation`, which propagates as HTTP 422 without a single line of error-handling code in the handler.

Notice what is absent: no try/except blocks, no logging calls, no tracing setup. All of that is the bus's responsibility. The handler is a pure expression of business intent.

### Sending a command

The `CommandBus` is the single entry point for all writes. PyFly's auto-configuration registers a `DefaultCommandBus` as a singleton in the DI container, so you only need to declare it as a constructor argument and the framework injects it. Sending a command is a single awaited call:

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

`send` is a coroutine, so always `await` it. The return value is whatever `do_handle` returned — a `str` wallet ID for `OpenWallet`, and the new balance as an `int` (minor units) for `DepositFunds` and `WithdrawFunds`. If anything in the pipeline fails — validation, authorization, or the handler itself — the exception wraps in `CommandProcessingException` and propagates out of `send`, where the global error handler picks it up and maps it to the appropriate HTTP status code.

::: figure art/figures/07-cqrs.svg | Figure 7.1 — Commands flow to the write model; queries to the read model.

!!! spring "Spring parity"
    `CommandBus.send(command)` is the Python equivalent of Axon Framework's `CommandGateway.send(command)` or `CommandGateway.sendAndWait(command)`. Each command handler class corresponds to a method annotated with `@CommandHandler` in Axon, or a `@MessageHandler` in Spring Modulith's ApplicationEventPublisher model. The `@command_handler` decorator is PyFly's counterpart of `@CommandHandler`: it registers the handler with the registry by introspecting the generic type parameter, exactly as Axon resolves handler methods by parameter type. The `@service` stacking mirrors the fact that in Spring every `@CommandHandler` bean is also a Spring `@Component` — registration and injection are inseparable.

---

## Queries and query handlers

Commands travel one direction: into the write model. Queries are the return journey: they ask the system for a projection of its current state and expect an answer, not a side effect.

A **query** is a frozen dataclass that inherits from `Query[R]`, where `R` is the type of the result. Like commands, queries are immutable messages — but they carry no intent to change state. From the caller's perspective, `query_bus.query(GetBalance(...))` loads fresh data from the repository and returns a typed DTO.

Queries return **read DTOs** rather than domain aggregates. This separation is deliberate and important. If you returned the `Wallet` aggregate from `GetWalletHandler`, your API layer would become coupled to every field on the aggregate — meaning a change to the domain model could silently break the API contract. A dedicated `WalletDto` Pydantic model projects exactly the fields the HTTP response needs. Add a field to `Wallet`? The projection only changes if you explicitly add it to the DTO. Remove a field from `Wallet`? The projection continues to compile until you clean it up.

::: listing lumen/core/services/wallets/get_wallet_query.py | Listing 7.7 — GetWallet: a frozen query returning WalletDto or None
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

Both queries carry only `wallet_id`. `GetWallet` returns a `WalletDto` — the full representation including `id`, `owner_id`, `currency`, `balance_minor`, `balance`, and `created_at`. `GetBalance` returns a `BalanceDto` — a lightweight projection that omits `owner_id` and `created_at`. A balance poll does not need to know the owner; by leaving those fields out of the projection you save bandwidth and avoid accidentally exposing account ownership in a response that callers may log. Keeping the two queries separate means you can tune each one independently — caching, authorization, or a dedicated read store — without changing the other.

The query handlers live under the same `wallets/` package as the commands. **The same `@query_handler` + `@service` stacking applies**: `@query_handler` registers the class with the handler registry; `@service` wires it into the DI container. Both decorators are required for the same reasons as on command handlers.

::: listing lumen/core/services/wallets/get_wallet_handler.py | Listing 7.9 — GetWalletHandler: load, map to DTO, return
from __future__ import annotations

from lumen.core.mappers.wallet_mapper import wallet_to_dto
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

    async def do_handle(  # type: ignore[override]
        self, query: GetWallet
    ) -> WalletDto | None:
        wallet = await self._repository.find(query.wallet_id)
        return wallet_to_dto(wallet) if wallet is not None else None
:::

::: listing lumen/core/services/wallets/get_balance_handler.py | Listing 7.10 — GetBalanceHandler: same pattern, lighter projection
from __future__ import annotations

from lumen.core.mappers.wallet_mapper import wallet_to_balance_dto
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

    async def do_handle(  # type: ignore[override]
        self, query: GetBalance
    ) -> BalanceDto | None:
        wallet = await self._repository.find(query.wallet_id)
        return wallet_to_balance_dto(wallet) if wallet is not None else None
:::

Both handlers delegate the aggregate-to-DTO mapping to `wallet_mapper` — a single module that owns the projection logic. `wallet_to_dto` fills in all six fields of `WalletDto` (including `balance` as a major-unit float via `balance.major_units`). `wallet_to_balance_dto` fills in the four fields of `BalanceDto`. Neither handler touches the Pydantic model directly; the mapper is the only place that knows the shape of each DTO, which means a field rename touches one file, not every handler.

Executing a query goes through `QueryBus.query`:

```python
from pyfly.cqrs import DefaultQueryBus
from lumen.core.services.wallets.get_balance_query import GetBalance

balance_dto = await query_bus.query(GetBalance(wallet_id="wlt-001"))
```

The return value is whatever `do_handle` returned — a `BalanceDto` or `None`. `None` means the wallet was not found; the controller is responsible for translating that into an HTTP 404, keeping the HTTP concern out of the handler.

!!! note "Queries return None, not exceptions"
    Query handlers return `None` when the resource is not found rather than raising `AggregateNotFound`. This is a deliberate convention: a query that finds nothing is not an error — it is an answer. The controller turns a `None` result into a 404 response, keeping the HTTP concern out of the handler.

---

## Wiring the bus into the controller

The controller is the system's HTTP boundary. Its only job is to translate an HTTP request into a domain message and an HTTP response into a domain result. Everything in between belongs to the bus and the handlers. That boundary is much easier to see once the controller dispatches commands and queries rather than calling service methods directly.

Before CQRS, `WalletController` held a reference to `WalletApplicationService` and called methods on it directly. Every time the service interface changed — a new parameter, a renamed method, a different return type — the controller had to change too. That coupling also meant the controller had implicit knowledge of how the service worked. With CQRS, the controller's knowledge is limited to one thing: what message to send.

The controller injects `DefaultCommandBus` and `DefaultQueryBus` by type — the **concrete bus classes**, not abstract protocols. This is the right import:

```python
from pyfly.cqrs import DefaultCommandBus, DefaultQueryBus
```

Why concrete classes? PyFly's CQRS auto-configuration registers exactly one instance of each bus in the DI container. Injecting by the concrete type is unambiguous — there is no protocol dispatch involved, and the type checker sees the full `send` / `query` surface. Using a protocol alias would require an explicit binding in the container; the concrete type works out of the box.

Here is the complete controller:

::: listing lumen/web/controllers/wallet_controller.py | Listing 7.11 — WalletController: injects DefaultCommandBus + DefaultQueryBus and dispatches messages
from __future__ import annotations

from lumen.core.services.wallets.deposit_funds_command import DepositFunds
from lumen.core.services.wallets.get_balance_query import GetBalance
from lumen.core.services.wallets.get_wallet_query import GetWallet
from lumen.core.services.wallets.open_wallet_command import OpenWallet
from lumen.core.services.wallets.withdraw_funds_command import WithdrawFunds
from lumen.interfaces.dtos.v1.balance_dto import BalanceDto
from lumen.interfaces.dtos.v1.deposit_request import DepositRequest
from lumen.interfaces.dtos.v1.open_wallet_request import OpenWalletRequest
from lumen.interfaces.dtos.v1.wallet_dto import WalletDto
from pyfly.container import rest_controller
from pyfly.cqrs import DefaultCommandBus, DefaultQueryBus
from pyfly.kernel import ResourceNotFoundException
from pyfly.web import (
    Body, PathVar, Valid,
    get_mapping, post_mapping, request_mapping,
)


@rest_controller
@request_mapping("/api/v1/wallets")
class WalletController:
    """Wallet REST API: open, deposit, withdraw, inspect.

    Injects the concrete ``DefaultCommandBus`` / ``DefaultQueryBus``
    beans registered by CQRS auto-configuration.  No business logic
    lives here — each endpoint builds a command or query and dispatches
    it through the bus.
    """

    def __init__(
        self,
        commands: DefaultCommandBus,
        queries: DefaultQueryBus,
    ) -> None:
        self._commands = commands
        self._queries = queries

    @post_mapping("", status_code=201)
    async def open_wallet(
        self, request: Valid[Body[OpenWalletRequest]]
    ) -> dict[str, str]:
        wallet_id = await self._commands.send(
            OpenWallet(
                owner_id=request.owner_id,
                currency=request.currency,
            )
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

    @get_mapping("/{wallet_id}")
    async def get_wallet(self, wallet_id: PathVar[str]) -> WalletDto:
        result = await self._queries.query(GetWallet(wallet_id=wallet_id))
        if result is None:
            raise ResourceNotFoundException(
                f"Wallet {wallet_id!r} not found",
                code="WALLET_NOT_FOUND",
                context={"wallet_id": wallet_id},
            )
        return result

    @get_mapping("/{wallet_id}/balance")
    async def get_balance(self, wallet_id: PathVar[str]) -> BalanceDto:
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

Compare the constructor to its pre-CQRS form. Before, the controller took `WalletApplicationService` — a concrete service class whose method signatures leaked business logic decisions into the HTTP layer. Now it takes `DefaultCommandBus` and `DefaultQueryBus` — two opaque channels through which messages flow. The controller knows *what* to send; it knows nothing about *how* the message is processed.

Look at `open_wallet`. In the before version it calls `self._service.open_wallet(owner_id=..., currency=...)` — a positional-argument contract that breaks if the service method ever grows a new parameter. In the after version it constructs `OpenWallet(owner_id=request.owner_id, currency=request.currency)` — a named, immutable object whose fields are its API. Add a field to the command? The controller stays the same until you choose to populate that field.

The request DTOs (`OpenWalletRequest`, `DepositRequest`) are separate Pydantic models in `lumen/interfaces/dtos/v1/`. `OpenWalletRequest` validates `owner_id` length and constrains `currency` to the `Currency` enum. `DepositRequest` is shared by both the deposit and withdraw endpoints — both move a positive `amount` in the wallet's own currency. Field-level constraints are declared in those DTOs and enforced by `Valid[Body[...]]` before the handler is ever called.

The `get_wallet` and `get_balance` methods show the only HTTP concern left in the controller: translating a `None` query result into a 404 response via `ResourceNotFoundException`. That one mapping belongs here because 404 is an HTTP status code and the handler deliberately has no HTTP knowledge. Return types are declared as `WalletDto` and `BalanceDto` — Pydantic models that the framework serializes to JSON automatically.

!!! tip "Let the bus raise"
    You do not need to catch `CommandProcessingException` or `QueryProcessingException` in the controller unless you want to customize the error shape. The global exception handler maps `AggregateNotFound` to 404 and `BusinessRuleViolation` to 422 — the same as before. The bus exceptions propagate those originals transparently.

---

## The handler pipeline

A single `send` or `query` call triggers a sequence of steps beyond the handler itself. Understanding this pipeline tells you where to put each type of cross-cutting concern — and, just as importantly, where *not* to put it.

Every call passes through a fixed pipeline before and after the handler runs. The pipeline is defined once, in the bus, and applies uniformly to every handler registered with it. You never write pipeline logic inside a handler. The order is strict: validation runs first, then authorization, then the handler, then (for commands) domain event publishing and tracing cleanup.

| Step | Where it is defined | Applies to | Failure result |
|---|---|---|---|
| Business pre-condition validation | `validate()` hook on the message | Commands + Queries | `CqrsValidationException` (HTTP 422) |
| Authorization | `authorize()` hook on the message | Commands + Queries | `AuthorizationException` (HTTP 403) |
| Handler execution | `do_handle()` | Commands + Queries | Domain exceptions (4xx/5xx) |
| Domain event publishing | Bus pipeline (post-handler) | Commands only | — |
| Correlation ID cleanup | Bus pipeline (finally block) | Commands + Queries | — |

### Validation

Without a structured validation step, every handler would need its own guard clauses at the top: check this field is not blank, check that amount is positive. That logic would be duplicated across handlers and tested only through integration paths. Centralizing validation in the message itself solves both problems.

The bus invokes the message's `validate()` method before looking up the handler. If validation fails the bus raises `CqrsValidationException` without ever reaching the handler.

The validation hook is also the right place for cross-field business pre-conditions that are knowable from the fields alone — too simple for the domain aggregate, too application-specific for the request model:

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

Once a message is structurally valid, the bus asks: is the caller *allowed* to perform this operation? Authorization answers that question before any database access happens, which is both more efficient and safer — you do not load sensitive data only to discard it because the caller lacked permission.

Authorization runs after validation passes. Both commands and queries expose an `authorize()` hook. Return `AuthorizationResult.success()` to allow execution, or `AuthorizationResult.failure(resource, message)` to deny it. The bus raises `AuthorizationException` on denial, which maps to HTTP 403 via the global error handler.

A clean rule of thumb keeps authorization concerns in the right place: use `authorize()` on the command for **operation-level** checks — who is allowed to call this command at all — and leave **resource-level** decisions (can this caller access *this specific* wallet?) to the handler, which has the loaded aggregate in scope and can inspect its ownership fields:

::: listing lumen/cqrs/commands_auth.py | Listing 7.12 — Authorization hook on a command
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

`CloseWallet.authorize` checks a known set of internal service accounts. If `requested_by` is not in that set, authorization fails immediately and the bus never calls the handler. The set would normally come from a configuration value or a token claim injected into the command at the controller boundary — here it is hardcoded to keep the example readable. The key point is that the check lives inside the command, not scattered across handler code.

### Distributed tracing

In a system where one HTTP request can trigger multiple commands — and each command might call downstream services — you need a way to stitch all those logs and spans together. That is what `CorrelationContext` provides.

Both buses set a correlation ID at the start of every pipeline execution. If the incoming message already carries an ID (set by the caller via `command.set_correlation_id(id)`), that ID is used. Otherwise a new UUID is generated and attached to the current execution context. The prior correlation ID is always restored in a `finally` block, so nested command dispatches within the same request do not clobber the outer trace.

`CorrelationContext` propagates correctly across `await` chains via Python's `contextvars` — you do not need to pass the correlation ID manually through every function argument. For cross-service propagation, where the trace must survive an HTTP hop to another microservice, serialize the context to headers on the outgoing call and restore it on the incoming side:

```python
from pyfly.cqrs.tracing.correlation import CorrelationContext

# On the sending side
headers = CorrelationContext.create_context_headers()
# {"X-Correlation-ID": "...", "X-Trace-ID": "...", "X-Span-ID": "..."}

# On the receiving side
CorrelationContext.extract_context_from_headers(headers)
```

The three headers — `X-Correlation-ID`, `X-Trace-ID`, and `X-Span-ID` — follow the W3C Trace Context naming convention, so they are compatible with OpenTelemetry-instrumented infrastructure out of the box.

!!! tip "Where to put cross-cutting logic"
    The bus pipeline is the right home for concerns that apply to *all* operations: validation, authorization, tracing, and metrics. The handler is the right home for concerns specific to *one* operation: loading the aggregate, driving behaviour, saving, draining events. If you find yourself adding a try/except to every handler, or copying the same pre-condition check into multiple handlers, it belongs in the pipeline — either as a `validate()` hook on the command or as a bus-level service. The pipeline scales uniformly; handler boilerplate does not.

---

## What you built {.recap}

Part II is complete.

Lumen now has a full vertical slice from HTTP to domain and back — one built on architectural decisions that will scale without rewriting.

In Chapter 5 you gave the system persistence: SQLAlchemy `BaseEntity` with five audit columns, a `WalletRepository` that speaks paged specifications and reactive projections, and an R2DBC data layer for non-blocking database access. In Chapter 6 you promoted the wallet to a proper DDD aggregate: `Money` as an immutable value object, `Wallet(AggregateRoot[str])` as the consistency boundary that enforces the overdraft rule, the currency-match rule, and the positive-amount rule, with `WalletOpened`, `FundsDeposited`, and `FundsWithdrawn` domain events queued in a buffer and drained after a successful save.

In this chapter you separated the write model from the read model with CQRS. `OpenWallet`, `DepositFunds`, and `WithdrawFunds` are frozen, validated command messages that flow through `DefaultCommandBus` — a pipeline that runs validation, authorization, handler execution, domain event publishing, and distributed tracing in that order, automatically, for every command. `GetWallet` and `GetBalance` are query messages that flow through `DefaultQueryBus` — the same pipeline without the event-publishing step. Each handler carries the `@command_handler` + `@service` (or `@query_handler` + `@service`) stack: the first decorator registers the class with the handler registry by introspecting its generic type argument; the second wires it into the DI container so constructor dependencies are injected automatically. Both decorators are required. Each handler is a small, focused class: `next_id` → `open` → `add` → `clear_events` → `publish` for commands, or `find` → `map_to_dto` → `return` for queries.

The `WalletController` no longer knows about the service layer at all. It injects the concrete `DefaultCommandBus` and `DefaultQueryBus`, builds a command or query from the HTTP request, dispatches it to the appropriate bus, and either returns the result or raises a domain exception. Adding a new command to Lumen now means three things: define a frozen dataclass, implement one `do_handle` method decorated with `@command_handler` + `@service`, and add one endpoint that calls `self._commands.send`. The pipeline applies automatically.

The aggregate you spent Chapter 6 building is unchanged. CQRS does not replace the domain model — it delivers instructions to it. That principle carries forward into the next part of the book, where commands begin crossing service boundaries and state changes need to be recorded permanently as events in an immutable log.

---

## Try it yourself {.exercises}

1. **Trace the full lifecycle in the test suite.** Open `samples/lumen/tests/test_cqrs_flow.py` and run it against a real database using Testcontainers (Chapter 11). The test `test_full_wallet_lifecycle` opens a wallet, deposits 1 500 minor units, withdraws 500, then queries both `GetWallet` and `GetBalance`. Step through it with a debugger: confirm that `wallet.clear_events()` drains the `FundsDeposited` and `FundsWithdrawn` events after each `repository.add` call, and that `GetWallet` returns a `WalletDto` with `balance_minor == 1000` and `balance == 10.0`.

2. **Add a `ListWallets` query with paging.** Define `ListWallets(Query[list[WalletDto]])` with `owner_id: str | None = None`, `page: int = 1`, and `size: int = 20`. Implement `ListWalletsHandler(QueryHandler[ListWallets, list[WalletDto]])` — decorated with `@query_handler` + `@service` — that delegates to `WalletRepository.find_all(owner_id=..., page=..., size=...)` (add that method to the repository if it does not exist yet). Add a `GET /api/v1/wallets` endpoint to `WalletController` that reads `owner_id`, `page`, and `size` as query parameters and dispatches the query. Verify that `GET /api/v1/wallets?owner_id=u-1&page=1&size=5` returns the correct subset.

3. **Add authorization to `WithdrawFunds`.** Extend `WithdrawFunds` with an `initiated_by: str` field. Override `authorize()` to return `AuthorizationResult.failure("withdraw", "Initiator is required")` when `initiated_by` is blank, and `AuthorizationResult.success()` otherwise. Update `WithdrawFundsHandler.do_handle` to record `command.initiated_by` in the `FundsWithdrawn` event payload. Write a test that calls `await WithdrawFunds(wallet_id="wlt-1", amount=100, initiated_by="").authorize()` and asserts that the result denies authorization.
