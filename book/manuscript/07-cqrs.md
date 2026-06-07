<span class="eyebrow">Chapter 7</span>

# CQRS: Commands & Queries {.chtitle}

::: figure art/openers/ch07.svg | &nbsp;

Lumen's wallet is now a first-class citizen of the domain. The `Wallet` aggregate enforces its own invariants, emits domain events, and persists through a clean repository boundary. Chapter 6's controller, though, still calls `WalletApplicationService` directly — one method per operation, reads and writes sharing the same code path. That design is fine at small scale, but it starts to show friction as the system grows. The team wants to cache wallet balances. They want a single audit trail for every write. They want to add authorization rules to specific operations without tangling them with the business logic in the service. And they want to test each piece of that logic in isolation.

**CQRS** — Command Query Responsibility Segregation — addresses all of this by drawing a bright line between the two things a service can do: *change state* and *read state*. Writes become **commands**: strongly typed, named, immutable messages that flow through a `CommandBus`. Reads become **queries**: equally typed messages that flow through a `QueryBus`. Each bus runs a pipeline — validation, authorization, execution, then (for commands) domain event publishing, and (for queries) caching. Your handler implements exactly one intent; the bus handles everything else.

By the end of this chapter Lumen's controller dispatches commands and queries instead of calling the service directly. `OpenWallet`, `DepositFunds`, and `TransferFunds` travel the command path; `GetWallet` and `GetBalance` travel the query path. The invariant-enforcing `Wallet` aggregate you built in Chapter 6 remains untouched — CQRS is not a replacement for the domain model, it is the delivery mechanism for instructions to that model.

---

## Why separate reads from writes

Picture Lumen at the end of Chapter 6. `WalletController` calls `WalletApplicationService.credit(wallet_id, amount)`. That call mutates state, but nothing in the method signature makes that obvious. Now the team wants to add a balance cache. Where does it go? Inside `credit`? In a decorator around the service? The question itself reveals the problem: a single service method is asked to serve two masters — the write path, which must always touch the database, and the read path, which should avoid it whenever possible. Bolting caching onto a write method is awkward at best and dangerous at worst.

Writes and reads have fundamentally different shapes. A write arrives with an intent and data: "deposit 50 EUR into wallet w-001". A read arrives with a question: "what is the current balance of wallet w-001?" The first must reach the database every time. The second is repeatable — asking twice should return the same answer without doubling the database load. Running both through the same method conflates concerns that scale differently, test differently, and need different cross-cutting behaviour.

The deeper benefit is **clarity of intent**. When a future teammate reads `wallet_service.credit(wallet_id, amount)`, they must inspect the implementation to know whether it is safe to call twice, whether it publishes events, and whether it is idempotent. When they read `DepositFunds(wallet_id=..., amount_cents=..., currency=...)`, the intent is unambiguous — and if the intent turns out to be wrong, you rename the command, not the service signature.

There are three concrete benefits that matter for Lumen.

**Independent scaling.** Reads typically outnumber writes by an order of magnitude or more. Once the command and query paths are separate, the bus can cache query results without touching the write path at all. You can route queries to a read replica and commands to the primary database with a configuration change rather than a code change.

**Focused handlers.** Each handler implements exactly one operation. `TransferFundsHandler` knows how to load two wallets, drive both through their domain behaviour, and save both — nothing more. `GetBalanceHandler` loads one wallet and returns a projection — nothing more. Because handlers are plain Python classes with injected dependencies, you can unit-test each one in complete isolation from the HTTP layer.

**Centralized cross-cutting concerns.** Validation, authorization, caching, and distributed tracing are implemented once in the bus pipeline and apply uniformly to every handler — no boilerplate required in the handler itself. Adding per-operation authorization later is a matter of overriding `authorize()` on the command; the bus ensures it runs before `do_handle` is ever reached.

---

## Commands and command handlers

Before you write a single line of handler code, decide what your system's intentions are. In Lumen's wallet domain there are three things that can happen: a wallet can be opened, funds can be deposited, and funds can be transferred. Each of those is a **command** — a named, immutable message that expresses one intent. The bus delivers it; the handler acts on it; the domain aggregate enforces the rules. Commands are not method calls dressed up as objects: they are explicit contracts that live in your codebase as first-class citizens.

A **command** is a frozen dataclass that inherits from `Command[R]`, where `R` is the type the handler returns. The generic parameter is documentation and a type-checker hint; the bus does not enforce it at runtime.

::: listing lumen/cqrs/commands.py | Listing 7.1 — Lumen's write commands as frozen dataclasses
from __future__ import annotations
from dataclasses import dataclass

from pyfly.cqrs.types import Command
from pyfly.cqrs.validation.types import ValidationResult


@dataclass(frozen=True)
class OpenWallet(Command[str]):
    """Open a new wallet and return its ID."""
    owner_id: str
    currency: str

    async def validate(self) -> ValidationResult:
        if not self.owner_id.strip():
            return ValidationResult.failure("owner_id", "owner_id must not be blank")
        if len(self.currency) != 3:
            return ValidationResult.failure("currency", "currency must be an ISO 4217 code")
        return ValidationResult.success()


@dataclass(frozen=True)
class DepositFunds(Command[None]):
    """Credit funds into a wallet."""
    wallet_id: str
    amount_cents: int
    currency: str

    async def validate(self) -> ValidationResult:
        if self.amount_cents <= 0:
            return ValidationResult.failure("amount_cents", "Amount must be positive")
        return ValidationResult.success()


@dataclass(frozen=True)
class TransferFunds(Command[None]):
    """Transfer funds between two wallets of the same currency."""
    source_wallet_id: str
    target_wallet_id: str
    amount_cents: int
    currency: str

    async def validate(self) -> ValidationResult:
        if self.amount_cents <= 0:
            return ValidationResult.failure("amount_cents", "Amount must be positive")
        if self.source_wallet_id == self.target_wallet_id:
            return ValidationResult.failure(
                "target_wallet_id", "Source and target wallets must differ"
            )
        return ValidationResult.success()
:::

Three design choices are baked into every command here, and it is worth being explicit about each one.

`frozen=True` makes the dataclass immutable the moment it is constructed. You cannot accidentally mutate a field in one layer of the pipeline before it reaches another. Immutable messages are also hashable by default, which matters if you ever want to store or compare them in tests.

`validate()` is an async hook that runs in the bus *before* the handler is dispatched. Notice that `OpenWallet.validate` checks `owner_id.strip()` and `len(currency) != 3`, while `TransferFunds.validate` checks both the positive-amount rule and the same-wallet guard. These are pre-conditions that belong to the command itself — they do not require a database lookup, and they do not belong in the domain aggregate either. The aggregate enforces invariants that require loaded state (overdraft, currency match). Commands enforce invariants that are knowable from the fields alone. Keeping these two layers of validation separate means your aggregate is never called with data that is structurally wrong.

Naming follows the **imperative mood** from the caller's perspective: `DepositFunds`, not `WalletDeposit` or `DepositFundsCommand`. This convention makes the command log read like a business audit trail — a sequence of things that *happened* — rather than a list of technical operations.

### Implementing a command handler

A command handler inherits from `CommandHandler[C, R]` and implements exactly one method: `do_handle`. You write the *what*; the bus wraps it with the *how*.

Two decorators appear on every handler. `@command_handler` registers the class with the `HandlerRegistry` by introspecting the first generic type argument (`TransferFunds` in the example below) — no manual registration call is needed. `@service` wires the handler into PyFly's DI container so that constructor arguments (`WalletDomainRepository` here) are resolved and injected automatically when the application starts.

Here is the handler for `TransferFunds` — the most involved of the three because it must load two separate aggregates, drive both through their domain behaviour, and save both atomically:

::: listing lumen/cqrs/handlers/transfer_funds_handler.py | Listing 7.2 — TransferFundsHandler: loading two aggregates and driving the domain
from __future__ import annotations

from pyfly.container import service
from pyfly.cqrs.command.handler import CommandHandler
from pyfly.cqrs.decorators import command_handler
from pyfly.domain import AggregateNotFound

from lumen.cqrs.commands import TransferFunds
from lumen.domain.money import Money
from lumen.domain.wallet_repository import WalletDomainRepository


@command_handler
@service
class TransferFundsHandler(CommandHandler[TransferFunds, None]):
    """
    Loads the source and target wallets, drives the domain behaviour,
    and saves both.  Domain events are published by the bus pipeline
    after a successful save.
    """

    def __init__(self, repo: WalletDomainRepository) -> None:
        self._repo = repo

    async def do_handle(self, command: TransferFunds) -> None:
        source = await self._repo.find(command.source_wallet_id)
        if source is None:
            raise AggregateNotFound("Wallet", command.source_wallet_id)

        target = await self._repo.find(command.target_wallet_id)
        if target is None:
            raise AggregateNotFound("Wallet", command.target_wallet_id)

        amount = Money(amount=command.amount_cents, currency=command.currency)

        # Aggregate methods enforce all invariants — overdraft, currency match,
        # positive amount.  BusinessRuleViolation propagates as HTTP 422.
        source.withdraw(amount)
        target.deposit(amount)

        await self._repo.save(source)
        await self._repo.save(target)
:::

Walk through the handler method line by line. The first four lines load the source and target wallets by ID, raising `AggregateNotFound` if either is missing — those exceptions map to HTTP 404 automatically through the RFC 7807 error pipeline you configured in Chapter 4. The `Money` value object is constructed from the command's fields, giving the domain a strongly typed amount rather than raw integers. Then `source.withdraw(amount)` and `target.deposit(amount)` drive the two aggregates through their own invariant checks — overdraft protection, currency matching, positive-amount validation. If either call raises `BusinessRuleViolation`, the exception propagates as HTTP 422 without a single line of error-handling code in the handler. Finally, both aggregates are saved. Domain events queued inside each aggregate during the mutating calls are drained and published by the bus pipeline *after* both saves succeed.

Notice what is absent: no try/except blocks, no logging calls, no validation checks, no tracing setup. All of that is the bus's responsibility. The handler is a pure expression of business intent.

Here are the handlers for `OpenWallet` and `DepositFunds`:

::: listing lumen/cqrs/handlers/open_wallet_handler.py | Listing 7.3 — OpenWalletHandler and DepositFundsHandler
from __future__ import annotations

from pyfly.container import service
from pyfly.cqrs.command.handler import CommandHandler
from pyfly.cqrs.decorators import command_handler
from pyfly.domain import AggregateNotFound

from lumen.cqrs.commands import DepositFunds, OpenWallet
from lumen.domain.money import Money
from lumen.domain.wallet import Wallet
from lumen.domain.wallet_repository import WalletDomainRepository


@command_handler
@service
class OpenWalletHandler(CommandHandler[OpenWallet, str]):
    """Open a new wallet and return its ID."""

    def __init__(self, repo: WalletDomainRepository) -> None:
        self._repo = repo

    async def do_handle(self, command: OpenWallet) -> str:
        wallet = Wallet.open(owner_id=command.owner_id, currency=command.currency)
        await self._repo.save(wallet)
        assert wallet.id is not None
        return wallet.id


@command_handler
@service
class DepositFundsHandler(CommandHandler[DepositFunds, None]):
    """Credit funds into a wallet."""

    def __init__(self, repo: WalletDomainRepository) -> None:
        self._repo = repo

    async def do_handle(self, command: DepositFunds) -> None:
        wallet = await self._repo.find(command.wallet_id)
        if wallet is None:
            raise AggregateNotFound("Wallet", command.wallet_id)
        wallet.deposit(Money(amount=command.amount_cents, currency=command.currency))
        await self._repo.save(wallet)
:::

`OpenWalletHandler` delegates the creation decision entirely to `Wallet.open` — the factory method on your domain aggregate — and then saves the result. Because `Wallet.open` assigns the ID internally (it was set by the repository mapper in Chapter 6), the `assert wallet.id is not None` line is a safety net for the type-checker, not a runtime guard. The handler returns the string ID, which flows back to the controller as `send`'s return value.

`DepositFundsHandler` follows the classic command handler pattern: load, guard, act, save. The `AggregateNotFound` guard means you never pass a `None` wallet to `deposit` — the bus translates the exception to 404 before the controller ever sees it.

### Sending a command

The `CommandBus` is the single entry point for all writes. PyFly's auto-configuration registers a `DefaultCommandBus` as a singleton in the DI container, so you only need to declare it as a constructor argument and the framework injects it. Sending a command is a single awaited call:

```python
wallet_id: str = await command_bus.send(
    OpenWallet(owner_id="user-42", currency="EUR")
)
await command_bus.send(
    DepositFunds(wallet_id=wallet_id, amount_cents=5000, currency="EUR")
)
await command_bus.send(
    TransferFunds(
        source_wallet_id=wallet_id,
        target_wallet_id="w-target",
        amount_cents=1000,
        currency="EUR",
    )
)
```

`send` is a coroutine, so always `await` it. The return value is whatever `do_handle` returned — a `str` wallet ID for `OpenWallet`, and `None` for the mutation-only commands. If anything in the pipeline fails — validation, authorization, or the handler itself — the exception wraps in `CommandProcessingException` and propagates out of `send`, where the global error handler picks it up and maps it to the appropriate HTTP status code.

::: figure art/figures/07-cqrs.svg | Figure 7.1 — Commands flow to the write model; queries to the read model.

!!! spring "Spring parity"
    `CommandBus.send(command)` is the Python equivalent of Axon Framework's `CommandGateway.send(command)` or `CommandGateway.sendAndWait(command)`. Each command handler class corresponds to a method annotated with `@CommandHandler` in Axon, or a `@MessageHandler` in Spring Modulith's ApplicationEventPublisher model. The `@command_handler` decorator is PyFly's counterpart of `@CommandHandler`: it registers the handler with the registry by introspecting the generic type parameter, exactly as Axon resolves handler methods by parameter type.

---

## Queries and query handlers

Commands travel one direction: into the write model. Queries are the return journey: they ask the system for a projection of its current state and expect an answer, not a side effect.

A **query** is a frozen dataclass that inherits from `Query[R]`, where `R` is the type of the result. Like commands, queries are immutable messages — but they carry no intent to change state. The bus treats them differently: it checks the cache before invoking the handler, and writes the result back to the cache after a successful execution. From the caller's perspective, `query_bus.query(GetBalance(...))` either returns a fresh value from the database or a cached value from a previous read — the decision is transparent.

Queries return **read DTOs** rather than domain aggregates. This separation is deliberate and important. If you returned the `Wallet` aggregate from `GetWalletHandler`, your API layer would become coupled to every field on the aggregate — meaning a change to the domain model could silently break the API contract. A dedicated `WalletView` dataclass projects exactly the fields the HTTP response needs. Add a field to `Wallet`? The projection only changes if you explicitly add it to the view. Remove a field from `Wallet`? The projection continues to compile until you clean it up.

::: listing lumen/cqrs/queries.py | Listing 7.4 — Lumen's read queries and their result DTOs
from __future__ import annotations
from dataclasses import dataclass

from pyfly.cqrs.types import Query


@dataclass(frozen=True)
class GetWallet(Query["WalletView | None"]):
    """Retrieve a full wallet projection by ID."""
    wallet_id: str


@dataclass(frozen=True)
class GetBalance(Query["BalanceView | None"]):
    """Retrieve just the balance for a wallet."""
    wallet_id: str


# ── Read-model projections (DTOs) ────────────────────────────────────────────

@dataclass(frozen=True)
class WalletView:
    wallet_id: str
    owner_id: str
    balance_cents: int
    currency: str


@dataclass(frozen=True)
class BalanceView:
    wallet_id: str
    balance_cents: int
    currency: str
:::

Two things are worth noting about `BalanceView`. It has three fields — `wallet_id`, `balance_cents`, `currency` — and deliberately omits `owner_id`. A balance poll does not need to know the owner; by leaving that field out of the projection you save bandwidth and avoid accidentally exposing account ownership in a response that callers may log. The two queries therefore return different shapes for different purposes, even though both hit the same `WalletDomainRepository` under the hood.

Lumen needs two queries: one that returns the full wallet view (for a detail page), and one that returns only the balance (for a dashboard widget or frequent polling). Keeping them separate means you can tune their caching independently.

The `@query_handler` decorator mirrors `@command_handler` but adds caching parameters. Setting `cacheable=True` and a `cache_ttl` tells the bus to check the cache before hitting the handler, and to store the result after a successful read.

::: listing lumen/cqrs/handlers/wallet_query_handlers.py | Listing 7.5 — Query handlers with caching enabled
from __future__ import annotations

from pyfly.container import service
from pyfly.cqrs.decorators import query_handler
from pyfly.cqrs.query.handler import QueryHandler

from lumen.cqrs.queries import BalanceView, GetBalance, GetWallet, WalletView
from lumen.domain.wallet_repository import WalletDomainRepository


@query_handler(cacheable=True, cache_ttl=60)
@service
class GetWalletHandler(QueryHandler[GetWallet, "WalletView | None"]):
    """Return a full wallet projection, cached for 60 seconds."""

    def __init__(self, repo: WalletDomainRepository) -> None:
        self._repo = repo

    async def do_handle(self, query: GetWallet) -> WalletView | None:
        wallet = await self._repo.find(query.wallet_id)
        if wallet is None:
            return None
        return WalletView(
            wallet_id=str(wallet.id),
            owner_id=wallet.owner_id,
            balance_cents=wallet.balance.amount,
            currency=wallet.balance.currency,
        )


@query_handler(cacheable=True, cache_ttl=30)
@service
class GetBalanceHandler(QueryHandler[GetBalance, "BalanceView | None"]):
    """Return just the balance, cached for 30 seconds."""

    def __init__(self, repo: WalletDomainRepository) -> None:
        self._repo = repo

    async def do_handle(self, query: GetBalance) -> BalanceView | None:
        wallet = await self._repo.find(query.wallet_id)
        if wallet is None:
            return None
        return BalanceView(
            wallet_id=str(wallet.id),
            balance_cents=wallet.balance.amount,
            currency=wallet.balance.currency,
        )
:::

Notice the different TTL values: `GetWalletHandler` caches for 60 seconds, `GetBalanceHandler` for 30. Balance data changes more often (every deposit or transfer), so a shorter TTL means callers see fresher numbers after a mutation. The full wallet view includes less volatile data (owner, currency), so the longer TTL is safe. You are tuning caching policy at the handler level, not inside the handler — the handler itself is oblivious to whether its result was cached.

The cache key for `GetBalance(wallet_id="w-001")` is automatically computed as `ClassName:sha256_hex16(fields)` — a stable SHA-256 digest of the dataclass field values, prefixed with `:cqrs:` by the bus. The same query object always maps to the same cache key across processes. You can override `get_cache_key()` on the query if you need a fully custom strategy.

Executing a query goes through `QueryBus.query`:

```python
balance: BalanceView | None = await query_bus.query(
    GetBalance(wallet_id="w-001")
)
```

On a cache hit the handler is not called at all — the bus returns the stored value directly. On a miss the handler runs, the result is stored, and subsequent calls within the TTL return the cached value without touching the database. The cache is keyed per query *instance*, so `GetBalance(wallet_id="w-001")` and `GetBalance(wallet_id="w-002")` are completely independent entries. Adding a new wallet does not invalidate existing entries, and a deposit to `w-001` does not affect `w-002`'s cache slot.

!!! note "Queries return None, not exceptions"
    Query handlers return `None` when the resource is not found rather than raising `AggregateNotFound`. This is a deliberate convention: a query that finds nothing is not an error — it is an answer. The controller turns a `None` result into a 404 response, keeping the HTTP concern out of the handler.

---

## Wiring the bus into the controller

The controller is the system's HTTP boundary. Its only job is to translate an HTTP request into a domain message and an HTTP response into a domain result. Everything in between belongs to the bus and the handlers. That boundary is much easier to see once the controller dispatches commands and queries rather than calling service methods directly.

Before CQRS, `WalletController` held a reference to `WalletApplicationService` and called methods on it directly. Every time the service interface changed — a new parameter, a renamed method, a different return type — the controller had to change too. That coupling also meant the controller had implicit knowledge of how the service worked. With CQRS, the controller's knowledge is limited to one thing: what message to send.

Here is the before state (condensed from Chapter 4 and Chapter 6's service layer):

::: listing lumen/wallet_controller_before.py | Listing 7.6 — Before: controller coupled directly to the application service
from pyfly.container import rest_controller
from pyfly.web import Body, PathVar, Valid, patch_mapping, post_mapping, request_mapping

from lumen.wallet_application_service import WalletApplicationService


@rest_controller
@request_mapping("/wallets")
class WalletControllerBefore:

    def __init__(self, service: WalletApplicationService) -> None:
        self._service = service

    @post_mapping("", status_code=201)
    async def open_wallet(self, body: Valid[Body]) -> dict:  # type: ignore[type-arg]
        wallet_id = await self._service.open_wallet(
            owner_id=body.owner_id, currency=body.currency  # type: ignore[attr-defined]
        )
        return {"wallet_id": wallet_id}

    @patch_mapping("/{wallet_id}/deposit")
    async def deposit(self, wallet_id: PathVar[str], body: Valid[Body]) -> dict:  # type: ignore[type-arg]
        await self._service.deposit(
            wallet_id=wallet_id,
            amount_cents=body.amount_cents,  # type: ignore[attr-defined]
            currency=body.currency,  # type: ignore[attr-defined]
        )
        return {"status": "ok"}
:::

And here is the after state, where the controller dispatches messages instead:

::: listing lumen/wallet_controller.py | Listing 7.7 — After: controller dispatches commands and queries through the buses
from __future__ import annotations
from dataclasses import dataclass

from pydantic import BaseModel, Field

from pyfly.container import rest_controller
from pyfly.cqrs.command.bus import DefaultCommandBus
from pyfly.cqrs.query.bus import DefaultQueryBus
from pyfly.kernel.exceptions import ResourceNotFoundException
from pyfly.web import (
    PathVar,
    Valid,
    get_mapping,
    patch_mapping,
    post_mapping,
    request_mapping,
)

from lumen.cqrs.commands import DepositFunds, OpenWallet, TransferFunds
from lumen.cqrs.queries import GetBalance, GetWallet, WalletView


# ── Request models ────────────────────────────────────────────────────────────

class OpenWalletRequest(BaseModel):
    owner_id: str = Field(min_length=1)
    currency: str = Field(min_length=3, max_length=3)


class DepositRequest(BaseModel):
    amount_cents: int = Field(gt=0)
    currency: str = Field(min_length=3, max_length=3)


class TransferRequest(BaseModel):
    target_wallet_id: str = Field(min_length=1)
    amount_cents: int = Field(gt=0)
    currency: str = Field(min_length=3, max_length=3)


# ── Controller ────────────────────────────────────────────────────────────────

@rest_controller
@request_mapping("/wallets")
class WalletController:

    def __init__(
        self,
        command_bus: DefaultCommandBus,
        query_bus: DefaultQueryBus,
    ) -> None:
        self._commands = command_bus
        self._queries = query_bus

    @post_mapping("", status_code=201)
    async def open_wallet(self, body: Valid[OpenWalletRequest]) -> dict:
        wallet_id: str = await self._commands.send(
            OpenWallet(owner_id=body.owner_id, currency=body.currency)
        )
        return {"wallet_id": wallet_id}

    @get_mapping("/{wallet_id}")
    async def get_wallet(self, wallet_id: PathVar[str]) -> dict:
        view: WalletView | None = await self._queries.query(
            GetWallet(wallet_id=wallet_id)
        )
        if view is None:
            raise ResourceNotFoundException(
                f"Wallet {wallet_id} not found",
                code="WALLET_NOT_FOUND",
                context={"wallet_id": wallet_id},
            )
        return {
            "wallet_id": view.wallet_id,
            "owner_id": view.owner_id,
            "balance_cents": view.balance_cents,
            "currency": view.currency,
        }

    @get_mapping("/{wallet_id}/balance")
    async def get_balance(self, wallet_id: PathVar[str]) -> dict:
        view = await self._queries.query(GetBalance(wallet_id=wallet_id))
        if view is None:
            raise ResourceNotFoundException(
                f"Wallet {wallet_id} not found",
                code="WALLET_NOT_FOUND",
                context={"wallet_id": wallet_id},
            )
        return {
            "wallet_id": view.wallet_id,
            "balance_cents": view.balance_cents,
            "currency": view.currency,
        }

    @patch_mapping("/{wallet_id}/deposit")
    async def deposit(
        self, wallet_id: PathVar[str], body: Valid[DepositRequest]
    ) -> dict:
        await self._commands.send(
            DepositFunds(
                wallet_id=wallet_id,
                amount_cents=body.amount_cents,
                currency=body.currency,
            )
        )
        return {"status": "ok"}

    @post_mapping("/{wallet_id}/transfer")
    async def transfer(
        self, wallet_id: PathVar[str], body: Valid[TransferRequest]
    ) -> dict:
        await self._commands.send(
            TransferFunds(
                source_wallet_id=wallet_id,
                target_wallet_id=body.target_wallet_id,
                amount_cents=body.amount_cents,
                currency=body.currency,
            )
        )
        return {"status": "ok"}
:::

Compare the two constructors. In Listing 7.6 the controller takes `WalletApplicationService` — a concrete service class whose method signatures leak business logic decisions into the HTTP layer. In Listing 7.7 it takes `DefaultCommandBus` and `DefaultQueryBus` — two opaque channels through which messages flow. The controller knows *what* to send; it knows nothing about *how* the message is processed.

Look at `open_wallet`. In the before version it calls `self._service.open_wallet(owner_id=..., currency=...)` — a positional-argument contract that breaks if the service method ever grows a new parameter. In the after version it constructs `OpenWallet(owner_id=body.owner_id, currency=body.currency)` — a named, immutable object whose fields are its API. Add a field to the command? The controller stays the same until you choose to populate that field.

The request models (`OpenWalletRequest`, `DepositRequest`, `TransferRequest`) in the Pydantic section do the structural validation that was previously scattered across service methods. Field-level constraints — `min_length=1`, `gt=0`, three-character currency codes — are declared once here and enforced by `Valid[...]` before `do_handle` is ever called.

The `get_wallet` and `get_balance` methods show the only HTTP concern left in the controller: translating a `None` query result into a 404 response. That one mapping belongs here because 404 is an HTTP status code and the handler deliberately has no HTTP knowledge. The DI container injects `DefaultCommandBus` and `DefaultQueryBus` automatically from their type hints — no factory configuration required.

!!! tip "Let the bus raise"
    You do not need to catch `CommandProcessingException` or `QueryProcessingException` in the controller unless you want to customize the error shape. The global exception handler maps `AggregateNotFound` to 404 and `BusinessRuleViolation` to 422 — the same as before. The bus exceptions propagate those originals transparently.

---

## The handler pipeline

A single `send` or `query` call triggers a sequence of steps beyond the handler itself. Understanding this pipeline tells you where to put each type of cross-cutting concern — and, just as importantly, where *not* to put it.

Every call passes through a fixed pipeline before and after the handler runs. The pipeline is defined once, in the bus, and applies uniformly to every handler registered with it. You never write pipeline logic inside a handler. The order is strict: validation runs first, then authorization, then the handler, then (for commands) domain event publishing and tracing cleanup.

| Step | Where it is defined | Applies to | Failure result |
|---|---|---|---|
| Structural validation | Pydantic `BaseModel` / `AutoValidationProcessor` | Commands + Queries | `CqrsValidationException` (HTTP 400) |
| Business pre-condition validation | `validate()` hook on the message | Commands + Queries | `CqrsValidationException` (HTTP 422) |
| Authorization | `authorize()` hook on the message | Commands + Queries | `AuthorizationException` (HTTP 403) |
| Handler execution | `do_handle()` | Commands + Queries | Domain exceptions (4xx/5xx) |
| Domain event publishing | Bus pipeline (post-handler) | Commands only | — |
| Correlation ID cleanup | Bus pipeline (finally block) | Commands + Queries | — |

### Validation

Without a structured validation step, every handler would need its own guard clauses at the top: check this field is not blank, check that amount is positive, check the two wallets are different. That logic would be duplicated across handlers and tested only through integration paths. Centralizing validation in the message itself solves both problems.

The bus invokes the message's `validate()` method before looking up the handler. For request bodies backed by Pydantic `BaseModel`, the `AutoValidationProcessor` also runs field-level structural validation automatically. Both phases are combined into a single validation pass, and if either fails the bus raises `CqrsValidationException` without ever reaching the handler. You saw this in Listing 7.1.

The validation hook is also the right place for cross-field business pre-conditions that are knowable from the fields alone — too simple for the domain aggregate, too application-specific for the request model:

```python
@dataclass(frozen=True)
class TransferFunds(Command[None]):
    source_wallet_id: str
    target_wallet_id: str
    amount_cents: int
    currency: str

    async def validate(self) -> ValidationResult:
        if self.amount_cents <= 0:
            return ValidationResult.failure("amount_cents", "Amount must be positive")
        if self.source_wallet_id == self.target_wallet_id:
            return ValidationResult.failure(
                "target_wallet_id", "Source and target wallets must differ"
            )
        return ValidationResult.success()
```

### Authorization

Once a message is structurally valid, the bus asks: is the caller *allowed* to perform this operation? Authorization answers that question before any database access happens, which is both more efficient and safer — you do not load sensitive data only to discard it because the caller lacked permission.

Authorization runs after validation passes. Both commands and queries expose an `authorize()` hook. Return `AuthorizationResult.success()` to allow execution, or `AuthorizationResult.failure(resource, message)` to deny it. The bus raises `AuthorizationException` on denial, which maps to HTTP 403 via the global error handler.

A clean rule of thumb keeps authorization concerns in the right place: use `authorize()` on the command for **operation-level** checks — who is allowed to call this command at all — and leave **resource-level** decisions (can this caller access *this specific* wallet?) to the handler, which has the loaded aggregate in scope and can inspect its ownership fields:

::: listing lumen/cqrs/commands_auth.py | Listing 7.8 — Authorization hook on a command
from __future__ import annotations
from dataclasses import dataclass

from pyfly.cqrs.authorization.types import AuthorizationResult
from pyfly.cqrs.types import Command


@dataclass(frozen=True)
class CloseWallet(Command[None]):
    """Close a wallet.  Only internal service accounts may do this."""
    wallet_id: str
    requested_by: str

    async def authorize(self) -> AuthorizationResult:
        internal_accounts = {"ops-service", "compliance-bot"}
        if self.requested_by not in internal_accounts:
            return AuthorizationResult.failure(
                "wallet", "Only internal service accounts may close wallets"
            )
        return AuthorizationResult.success()
:::

`CloseWallet.authorize` checks a known set of internal service accounts. If `requested_by` is not in that set, authorization fails immediately and the bus never calls the handler. The set would normally come from a configuration value or a token claim injected into the command at the controller boundary — here it is hardcoded to keep the example readable. The key point is that the check lives inside the command, not scattered across handler code.

### Caching (queries)

You have already seen caching from the handler's perspective in Listing 7.5. Here is how it works inside the bus pipeline: when a query handler is annotated with `cacheable=True`, the bus computes the cache key, checks the cache store, and either returns the cached value immediately (skipping `do_handle` entirely) or calls the handler and stores the result before returning it. The handler is not involved in either decision.

For query handlers decorated with `@query_handler(cacheable=True, cache_ttl=N)`, the bus checks the cache before calling `do_handle`. The cache key is computed from the query's class name and a SHA-256 digest of its field values. If a cached value exists and has not expired, the handler is not called at all. After a successful handler execution the result is stored.

To invalidate a specific entry call `await query_bus.clear_cache(key)`, or `await query_bus.clear_all_cache()` to flush everything. When a command mutates a wallet, the next `GetBalance` query for that wallet will miss the cache, load fresh data from the repository, and repopulate it. This is the natural invalidation pattern: the write path and the read path are separate, and cache coherence is achieved through TTL expiry rather than explicit invalidation on every write.

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
    The bus pipeline is the right home for concerns that apply to *all* operations: validation, authorization, tracing, metrics, and caching. The handler is the right home for concerns specific to *one* operation: loading the aggregate, driving behaviour, saving. If you find yourself adding a try/except to every handler, or copying the same pre-condition check into multiple handlers, it belongs in the pipeline — either as a `validate()` hook on the command or as a bus-level service. The pipeline scales uniformly; handler boilerplate does not.

---

## What you built {.recap}

Part II is complete.

Lumen now has a full vertical slice from HTTP to domain and back — one built on architectural decisions that will scale without rewriting.

In Chapter 5 you gave the system persistence: SQLAlchemy `BaseEntity` with five audit columns, a `WalletRepository` that speaks paged specifications and reactive projections, and an R2DBC data layer for non-blocking database access. In Chapter 6 you promoted the wallet to a proper DDD aggregate: `Money` as an immutable value object, `Wallet(AggregateRoot[str])` as the consistency boundary that enforces the overdraft rule, the currency-match rule, and the positive-amount rule, with `WalletOpened`, `FundsDeposited`, and `FundsWithdrawn` domain events queued in a buffer and drained after a successful save. A `WalletMapper` kept the domain model free of SQLAlchemy, and `WalletDomainRepository` kept the application layer free of persistence concerns.

In this chapter you separated the write model from the read model with CQRS. `OpenWallet`, `DepositFunds`, and `TransferFunds` are frozen, validated command messages that flow through `DefaultCommandBus` — a pipeline that runs validation, authorization, handler execution, domain event publishing, and distributed tracing in that order, automatically, for every command. `GetWallet` and `GetBalance` are query messages that flow through `DefaultQueryBus` — the same pipeline, plus a cache check before the handler and a cache put after. Each handler is a small, focused class: load, act, save (for commands) or load, project, return (for queries).

The `WalletController` no longer knows about the service layer at all. It builds a command or query from the HTTP request, dispatches it to the appropriate bus, and either returns the result or raises a domain exception. The bus handles everything in between. Adding a new command to Lumen now means three things: define a frozen dataclass, implement one `do_handle` method, and add one endpoint that calls `self._commands.send`. The pipeline applies automatically.

The aggregate you spent Chapter 6 building is unchanged. CQRS does not replace the domain model — it delivers instructions to it. That principle carries forward into the next part of the book, where commands begin crossing service boundaries and state changes need to be recorded permanently as events in an immutable log.

---

## Try it yourself {.exercises}

1. **Add an `OpenWallet` command with end-to-end wiring.** The command exists in Listing 7.1 and its handler in Listing 7.3, but wire the full path: register both with the `HandlerRegistry`, confirm the controller's `open_wallet` endpoint dispatches the command, and write a test that sends `OpenWallet` through a `DefaultCommandBus` wired with a real `HandlerRegistry` and an in-memory repository. Assert that the returned wallet ID is a non-empty string and that the repository contains an aggregate with that ID.

2. **Add a `ListWallets` query with paging.** Define `ListWallets(Query[list["WalletView"]])` with `owner_id: str | None = None`, `page: int = 1`, and `size: int = 20`. Implement `ListWalletsHandler(QueryHandler[ListWallets, list["WalletView"]])` that delegates to `WalletDomainRepository.find_all(owner_id=..., page=..., size=...)` (add that method to the repository if it does not exist yet). Decorate the handler with `@query_handler(cacheable=True, cache_ttl=30)`. Add a `GET /wallets` endpoint to `WalletController` that reads `owner_id`, `page`, and `size` as `QueryParam` values and dispatches the query. Verify that `GET /wallets?owner_id=user-42&page=1&size=5` returns the correct subset.

3. **Add authorization to `TransferFunds`.** Extend `TransferFunds` with an `initiated_by: str` field. Override `authorize()` to return `AuthorizationResult.failure("transfer", "Self-transfers are not authorized")` when `initiated_by` is empty or blank, and `AuthorizationResult.success()` otherwise. Update `TransferFundsHandler.do_handle` to pass `command.initiated_by` as metadata to the domain event (extend `FundsWithdrawn` with an `initiated_by: str = ""` field). Write a test that calls `await TransferFunds(source_wallet_id="w-1", target_wallet_id="w-2", amount_cents=100, currency="EUR", initiated_by="").authorize()` and asserts that `result.authorized` is `False`.
