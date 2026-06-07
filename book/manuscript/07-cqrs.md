<span class="eyebrow">Chapter 7</span>

# CQRS: Commands & Queries {.chtitle}

::: figure art/openers/ch07.svg | &nbsp;

Lumen's wallet is now a first-class citizen of the domain. The `Wallet` aggregate enforces its own invariants, emits domain events, and persists through a clean repository boundary. Chapter 6's controller, though, still calls `WalletApplicationService` directly — one method per operation, reads and writes sharing the same code path. That design is fine at small scale, but it starts to show friction as the system grows. The team wants to cache wallet balances. They want a single audit trail for every write. They want to add authorization rules to specific operations without tangling them with the business logic in the service. And they want to test each piece of that logic in isolation.

**CQRS** — Command Query Responsibility Segregation — addresses all of this by drawing a bright line between the two things a service can do: *change state* and *read state*. Writes become **commands**: strongly typed, named, immutable messages that flow through a `CommandBus`. Reads become **queries**: equally typed messages that flow through a `QueryBus`. Each bus runs a pipeline — validation, authorization, execution, then (for commands) domain event publishing, and (for queries) caching. Your handler implements exactly one intent; the bus handles everything else.

By the end of this chapter Lumen's controller dispatches commands and queries instead of calling the service directly. `OpenWallet`, `DepositFunds`, and `TransferFunds` travel the command path; `GetWallet` and `GetBalance` travel the query path. The invariant-enforcing `Wallet` aggregate you built in Chapter 6 remains untouched — CQRS is not a replacement for the domain model, it is the delivery mechanism for instructions to that model.

---

## Why separate reads from writes

The surface-level reason to separate reads from writes is that they have different shapes. A write comes in with an intent and some data: "deposit 50 EUR into wallet w-001". A read comes in with a question: "what is the balance of wallet w-001?" The first operation modifies state and must never be cached. The second is repeatable and a natural candidate for caching. Running both through the same service method conflates two concerns that scale differently, test differently, and need different cross-cutting behaviour.

The deeper reason is **clarity of intent**. When `WalletController` calls `wallet_service.credit(wallet_id, amount)`, every developer who reads that code must understand what `credit` does to know whether it is a safe operation to call twice or whether it has side effects. When the controller dispatches `DepositFunds(wallet_id=..., amount_cents=..., currency=...)`, the intent is unambiguous — and if the intent is wrong, you change the command name, not the service signature.

There are three concrete benefits that matter for Lumen.

**Independent scaling.** Reads typically outnumber writes by orders of magnitude. Once the command and query paths are separate, you can cache query results at the bus level without affecting the write path at all. You can route queries to a read replica and commands to the primary database with a configuration change rather than a code change.

**Focused handlers.** Each handler implements one operation. A `TransferFundsHandler` knows about loading two wallets, calling `wallet.withdraw` and `target.deposit`, and saving both — nothing more. A `GetBalanceHandler` loads one wallet and returns a projection — nothing more. Both are trivially unit-testable because they are plain Python classes with injected dependencies.

**Centralized cross-cutting concerns.** Validation, authorization, caching, and distributed tracing are defined once, in the bus pipeline, and apply to every handler without any handler-level boilerplate. Adding per-operation authorization later is a matter of overriding `authorize()` on the command; the bus ensures it runs.

---

## Commands and command handlers

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

Three things to notice. First, every command is `frozen=True` — once created it is immutable, which makes it safe to pass across async boundaries. Second, the optional `validate()` hook on each command encodes business-rule pre-conditions that the bus validates *before* the handler even runs; there is no coupling between the command and the handler here. Third, each command is named in the **imperative mood** from the caller's perspective: not `WalletDeposit` but `DepositFunds`.

### Implementing a command handler

A command handler inherits from `CommandHandler[C, R]` and implements exactly one method: `do_handle`. The `@command_handler` decorator registers the class with the `HandlerRegistry` and configures the per-handler pipeline options. The `@service` decorator from `pyfly.container` wires the handler into the DI container so its constructor dependencies are injected automatically.

Here is the handler for `TransferFunds` — the most involved of the three because it loads two aggregates, drives both through their domain behaviour, and saves both:

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

The handler is ten focused lines of business logic. Everything else — validation, tracing, event publishing — is handled by the bus pipeline. The two `AggregateNotFound` raises translate automatically to HTTP 404 responses via the RFC 7807 mapper you saw in Chapter 4. The `BusinessRuleViolation` from `wallet.withdraw` (insufficient funds, currency mismatch) translates to HTTP 422, also automatically.

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

### Sending a command

The `CommandBus` is the single entry point for all writes. After auto-configuration wires the bus into the container, inject `DefaultCommandBus` by type and call `send`:

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

`send` is a coroutine; `await` it. The return value is whatever `do_handle` returned — `str` for `OpenWallet`, `None` for `DepositFunds` and `TransferFunds`. Failures are wrapped in `CommandProcessingException` and propagate out of `send`.

::: figure art/figures/07-cqrs.svg | Figure 7.1 — Commands flow to the write model; queries to the read model.

!!! spring "Spring parity"
    `CommandBus.send(command)` is the Python equivalent of Axon Framework's `CommandGateway.send(command)` or `CommandGateway.sendAndWait(command)`. Each command handler class corresponds to a method annotated with `@CommandHandler` in Axon, or a `@MessageHandler` in Spring Modulith's ApplicationEventPublisher model. The `@command_handler` decorator is PyFly's counterpart of `@CommandHandler`: it registers the handler with the registry by introspecting the generic type parameter, exactly as Axon resolves handler methods by parameter type.

---

## Queries and query handlers

A **query** is a frozen dataclass that inherits from `Query[R]`, where `R` is the type of the result. Like commands, queries are immutable messages — but they carry no intent to change state. The bus treats them differently: it checks the cache before invoking the handler, and writes the result back to the cache after a successful execution.

Queries return **read DTOs** rather than domain aggregates. This keeps the query side free of domain model dependencies and lets you project exactly the fields the caller needs — no more, no less.

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

The cache key for `GetBalance(wallet_id="w-001")` is automatically computed as `ClassName:sha256_hex16(fields)` — a stable SHA-256 digest of the dataclass field values, prefixed with `:cqrs:` by the bus. The same query object always maps to the same cache key across processes. You can override `get_cache_key()` on the query if you need a fully custom strategy.

Executing a query goes through `QueryBus.query`:

```python
balance: BalanceView | None = await query_bus.query(
    GetBalance(wallet_id="w-001")
)
```

On a cache hit the handler is not called at all. On a miss the handler runs, the result is cached, and subsequent calls within the TTL return the cached value. The cache is keyed per query instance, so `GetBalance(wallet_id="w-001")` and `GetBalance(wallet_id="w-002")` are independent entries.

!!! note "Queries return None, not exceptions"
    Query handlers return `None` when the resource is not found rather than raising `AggregateNotFound`. This is a deliberate convention: a query that finds nothing is not an error — it is an answer. The controller turns a `None` result into a 404 response, keeping the HTTP concern out of the handler.

---

## Wiring the bus into the controller

Before CQRS, `WalletController` held a reference to `WalletApplicationService` and called methods on it directly. That coupling made the controller a relay: it bound the request, called the service, and returned the result. With CQRS, the controller's job narrows further — it binds the request, builds a command or query, and dispatches it. The bus handles the rest.

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

The refactor is mechanical: replace `self._service.method(args)` with `self._commands.send(Command(...))` or `self._queries.query(Query(...))`. The controller no longer knows how the operation is executed, only what the caller intended. The DI container injects `DefaultCommandBus` and `DefaultQueryBus` automatically from their type hints — no factory configuration required.

!!! tip "Let the bus raise"
    You do not need to catch `CommandProcessingException` or `QueryProcessingException` in the controller unless you want to customize the error shape. The global exception handler maps `AggregateNotFound` to 404 and `BusinessRuleViolation` to 422 — the same as before. The bus exceptions propagate those originals transparently.

---

## The handler pipeline

Every `send` and `query` call passes through a fixed pipeline before and after the handler runs. The pipeline is defined once, in the bus, and applies uniformly to every handler registered with it. You never write pipeline logic inside a handler.

### Validation

The bus invokes the message's `validate()` method before looking up the handler. For dataclasses backed by Pydantic `BaseModel`, the `AutoValidationProcessor` also runs field-level structural validation. Both phases are combined, and if either fails the bus raises `CqrsValidationException` — no handler code runs. You already saw this in the command definitions in Listing 7.1.

The validation hook is also the right place for cross-field business pre-conditions that are too simple to belong in the domain aggregate:

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

Authorization runs after validation passes. Both commands and queries expose an `authorize()` hook. Return `AuthorizationResult.success()` to allow execution, or `AuthorizationResult.failure(resource, message)` to deny it. The bus raises `AuthorizationException` on denial.

Use `authorize()` for operation-level access control — who is allowed to call this command at all — and leave resource-level decisions (can this user access *this* wallet?) to the handler, which has access to the loaded aggregate:

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

### Caching (queries)

For query handlers decorated with `@query_handler(cacheable=True, cache_ttl=N)`, the bus checks the cache before calling `do_handle`. The cache key is computed from the query's class name and a SHA-256 digest of its field values. If a cached value exists and has not expired, the handler is not called at all. After a successful handler execution the result is stored.

To invalidate a specific entry call `await query_bus.clear_cache(key)`, or `await query_bus.clear_all_cache()` to flush everything. When a command mutates a wallet, the next `GetBalance` query for that wallet will miss the cache, load fresh data from the repository, and repopulate it.

### Distributed tracing

Both buses set a correlation ID at the start of every pipeline execution via `CorrelationContext`. If the incoming message already carries a correlation ID (set by the caller via `command.set_correlation_id(id)`), that ID is used; otherwise a new UUID is generated. The prior correlation ID is always restored in a `finally` block, so nested command dispatches do not clobber the outer request's trace.

The `CorrelationContext` propagates correctly across `await` chains via Python's `contextvars`. To pass trace context across service boundaries, read the outbound headers with `CorrelationContext.create_context_headers()` and restore them on the receiving side with `CorrelationContext.extract_context_from_headers(headers)`:

```python
from pyfly.cqrs.tracing.correlation import CorrelationContext

# On the sending side
headers = CorrelationContext.create_context_headers()
# {"X-Correlation-ID": "...", "X-Trace-ID": "...", "X-Span-ID": "..."}

# On the receiving side
CorrelationContext.extract_context_from_headers(headers)
```

!!! tip "Where to put cross-cutting logic"
    The bus pipeline is the right home for concerns that apply to *all* operations: validation, authorization, tracing, metrics, and caching. The handler is the right home for concerns specific to *one* operation: loading the aggregate, driving behaviour, saving. If you find yourself adding a try/except to every handler, or copying the same pre-condition check into multiple handlers, it belongs in the pipeline — either as a `validate()` hook on the command or as a bus-level service.

---

## What you built {.recap}

Part II is complete.

Lumen now has a full vertical slice from HTTP to domain and back — one built on architectural decisions that will scale without rewriting.

In Chapter 5 you gave the system persistence: SQLAlchemy `BaseEntity` with five audit columns, a `WalletRepository` that speaks paged specifications and reactive projections, and an R2DBC data layer for non-blocking database access. In Chapter 6 you promoted the wallet to a proper DDD aggregate: `Money` as an immutable value object, `Wallet(AggregateRoot[str])` as the consistency boundary that enforces the overdraft rule, the currency-match rule, and the positive-amount rule, with `WalletOpened`, `FundsDeposited`, and `FundsWithdrawn` domain events queued in a buffer and drained after a successful save. A `WalletMapper` kept the domain model free of SQLAlchemy, and `WalletDomainRepository` kept the application layer free of persistence concerns.

In this chapter you separated the write model from the read model with CQRS. `OpenWallet`, `DepositFunds`, and `TransferFunds` are frozen, validated command messages that flow through `DefaultCommandBus` — a pipeline that runs validation, authorization, handler execution, domain event publishing, and distributed tracing in that order, automatically, for every command. `GetWallet` and `GetBalance` are query messages that flow through `DefaultQueryBus` — the same pipeline, plus a cache check before the handler and a cache put after. Each handler is a small, focused class: load, act, save (for commands) or load, project, return (for queries).

The `WalletController` no longer knows about the service layer at all. It builds a command or query from the HTTP request, dispatches it to the appropriate bus, and either returns the result or raises a domain exception. The bus handles everything in between.

The aggregate you spent Chapter 6 building is unchanged. CQRS does not replace the domain model — it delivers instructions to it.

---

## Try it yourself {.exercises}

1. **Add an `OpenWallet` command with end-to-end wiring.** The command exists in Listing 7.1 and its handler in Listing 7.3, but wire the full path: register both with the `HandlerRegistry`, confirm the controller's `open_wallet` endpoint dispatches the command, and write a test that sends `OpenWallet` through a `DefaultCommandBus` wired with a real `HandlerRegistry` and an in-memory repository. Assert that the returned wallet ID is a non-empty string and that the repository contains an aggregate with that ID.

2. **Add a `ListWallets` query with paging.** Define `ListWallets(Query[list["WalletView"]])` with `owner_id: str | None = None`, `page: int = 1`, and `size: int = 20`. Implement `ListWalletsHandler(QueryHandler[ListWallets, list["WalletView"]])` that delegates to `WalletDomainRepository.find_all(owner_id=..., page=..., size=...)` (add that method to the repository if it does not exist yet). Decorate the handler with `@query_handler(cacheable=True, cache_ttl=30)`. Add a `GET /wallets` endpoint to `WalletController` that reads `owner_id`, `page`, and `size` as `QueryParam` values and dispatches the query. Verify that `GET /wallets?owner_id=user-42&page=1&size=5` returns the correct subset.

3. **Add authorization to `TransferFunds`.** Extend `TransferFunds` with an `initiated_by: str` field. Override `authorize()` to return `AuthorizationResult.failure("transfer", "Self-transfers are not authorized")` when `initiated_by` is empty or blank, and `AuthorizationResult.success()` otherwise. Update `TransferFundsHandler.do_handle` to pass `command.initiated_by` as metadata to the domain event (extend `FundsWithdrawn` with an `initiated_by: str = ""` field). Write a test that calls `await TransferFunds(source_wallet_id="w-1", target_wallet_id="w-2", amount_cents=100, currency="EUR", initiated_by="").authorize()` and asserts that `result.authorized` is `False`.
