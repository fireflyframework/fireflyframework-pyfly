<span class="eyebrow">Chapter 16</span>

# Testing PyFly Applications {.chtitle}

::: figure art/openers/ch16.svg | &nbsp;

The wallet works. Deposits land, balances update, events propagate through the bus, and the saga coordinator rolls back cleanly on failure. What you do not yet have is **confidence** that it will keep working after the next refactor. That confidence comes from tests — tests that run in milliseconds, prove the domain model enforces its invariants, verify the CQRS pipeline dispatches correctly, exercise the repository's derived queries and Specification predicates against a real SQLite database, and boot the full application context in an integration test that proves the entire DI + persistence composition.

PyFly treats testing as a first-class concern. The `pyfly.testing` module ships higher-level helpers — `PyFlyTestCase`, `create_test_container`, `assert_event_published`, Testcontainers wiring — that you can reach for when you need them. Lumen's own test suite does not use them: it wires real components directly from `conftest.py`, uses standard pytest fixtures, and covers every layer of the pyramid with no boilerplate. That is the approach this chapter teaches.

::: figure art/figures/16-testing.svg | Figure 16.1 — PyFly's testing pyramid. Fast unit tests form the wide base; integration tests sit in the middle; real-DB adapter tests and a booted-context integration test crown the top.

The pyramid has four levels. **Unit tests** sit at the base — many of them, running in milliseconds, exercising the domain model with no dependencies. **CQRS flow tests** occupy the next tier — the full open/deposit/withdraw/query cycle routed through the real bus and the real repository, all wired in `conftest.py`. **Repository tests** exercise derived queries, pagination, and Specification predicates against SQLite. At the peak, a **booted-context integration test** starts the real `ApplicationContext` — DI scan, CQRS auto-config, `RepositoryBeanPostProcessor`, `@transactional` seam, EDA — and drives the full lifecycle.

| Level               | Dependencies                      | Speed | Lumen approach              |
|---------------------|-----------------------------------|-------|-----------------------------|
| Unit                | None                              | Fast  | plain pytest, no fixtures   |
| CQRS flow           | Real bus + repository over SQLite | Fast  | conftest.py fixtures        |
| Repository          | SQLite + aiosqlite                | Fast  | `tmp_path` + SQLAlchemy     |
| Booted-context      | Full ApplicationContext + SQLite  | Fast  | `monkeypatch` env override  |

The project uses pytest with `pytest-asyncio` in **auto mode**. Enable it once in `pyproject.toml` and every `async def test_*` function is collected automatically:

```ini
[tool.pytest.ini_options]
asyncio_mode = "auto"
testpaths = ["tests"]
pythonpath = ["src"]
```

Install dev dependencies and run the suite:

```bash
uv run --extra dev pytest -q
```

The bare `uv sync` (without `--extra dev`) omits the dev group, so pytest is not installed. Always include `--extra dev` when running tests.

---

## Unit-testing the domain

The domain model — `Money` and `Wallet` — has no framework dependencies. It never touches a database, a message bus, or an HTTP client. That purity makes it the ideal target for fast unit tests: construct objects, call methods, assert outcomes. No mocks, no fixtures, no `async`.

### Testing Money

`Money` is a frozen dataclass. Every operation either succeeds and returns a new `Money` instance, or raises `BusinessRuleViolation`. Each violation carries a `.rule` string that names the violated invariant — useful for asserting the exact rule in tests.

::: listing tests/test_money.py | Listing 16.1 — Pure unit tests for the Money value object
from __future__ import annotations

import pytest
from lumen.interfaces.enums.v1.currency import Currency
from lumen.models.entities.v1.money import Money

from pyfly.domain import BusinessRuleViolation


def test_value_equality_is_structural() -> None:
    assert Money(1050, Currency.EUR) == Money(1050, Currency.EUR)
    assert Money(1050, Currency.EUR) != Money(1050, Currency.USD)
    assert Money(1050, Currency.EUR) != Money(999, Currency.EUR)


def test_money_is_immutable() -> None:
    money = Money(1050, Currency.EUR)
    with pytest.raises(Exception):  # frozen dataclass -> FrozenInstanceError
        money.amount = 0  # type: ignore[misc]


def test_add_and_subtract_same_currency() -> None:
    a = Money(1050, Currency.EUR)
    b = Money(450, Currency.EUR)
    assert a.add(b) == Money(1500, Currency.EUR)
    assert a.subtract(b) == Money(600, Currency.EUR)


def test_zero_factory_and_major_units() -> None:
    assert Money.zero(Currency.USD) == Money(0, Currency.USD)
    assert Money(1050, Currency.EUR).major_units == 10.5
    assert str(Money(1050, Currency.EUR)) == "10.50 EUR"


def test_currency_mismatch_is_rejected() -> None:
    with pytest.raises(BusinessRuleViolation) as exc:
        Money(100, Currency.EUR).add(Money(100, Currency.USD))
    assert exc.value.rule == "money-currency-mismatch"


def test_non_integer_amount_is_rejected() -> None:
    with pytest.raises(BusinessRuleViolation) as exc:
        Money(10.5, Currency.EUR)  # type: ignore[arg-type]
    assert exc.value.rule == "money-amount-integer"
:::

Every test is synchronous — no `async`, no `await`, no fixtures. Pytest collects the module-level functions automatically. `Currency.EUR` is an enum value, not a plain string, matching the domain model's type contract exactly.

!!! tip "Minor-unit arithmetic"
    `Money` stores amounts in **minor units** (integer cents). `Money(1050,
    Currency.EUR)` represents €10.50 — verified by `major_units == 10.5` and
    `str(...) == "10.50 EUR"`. The `Money.zero(currency)` factory returns a
    `Money(0, currency)`, useful for initialising wallet balances.

### Testing the Wallet aggregate

`Wallet` enforces several invariants: the owner must be a non-blank string, deposits must be positive, withdrawals must not overdraw, and amounts must match the wallet's currency. Each violation carries a `.rule` attribute for precise assertion.

::: listing tests/test_wallet_aggregate.py | Listing 16.2 — Unit tests for the Wallet aggregate root
from __future__ import annotations

import pytest
from lumen.interfaces.enums.v1.currency import Currency
from lumen.models.entities.v1.money import Money
from lumen.models.entities.v1.wallet_entity import (
    FundsDeposited,
    FundsWithdrawn,
    Wallet,
    WalletOpened,
)

from pyfly.domain import BusinessRuleViolation


def test_open_creates_empty_wallet() -> None:
    wallet = Wallet.open("wlt-1", "owner-1", Currency.EUR)
    assert wallet.owner_id == "owner-1"
    assert wallet.currency is Currency.EUR
    assert wallet.balance == Money.zero(Currency.EUR)
    [event] = wallet.pending_events()
    assert isinstance(event, WalletOpened)
    assert event.wallet_id == "wlt-1"
    assert event.currency == "EUR"


def test_open_requires_owner() -> None:
    with pytest.raises(BusinessRuleViolation) as exc:
        Wallet.open("wlt-x", "   ", Currency.EUR)
    assert exc.value.rule == "wallet-owner-required"


def test_deposit_then_withdraw_happy_path() -> None:
    wallet = Wallet.open("wlt-2", "owner-2", Currency.EUR)
    wallet.clear_events()

    wallet.deposit(Money(1000, Currency.EUR))
    assert wallet.balance == Money(1000, Currency.EUR)
    [event] = wallet.clear_events()
    assert isinstance(event, FundsDeposited)
    assert event.amount == 1000
    assert event.balance == 1000

    wallet.withdraw(Money(400, Currency.EUR))
    assert wallet.balance == Money(600, Currency.EUR)
    [event] = wallet.clear_events()
    assert isinstance(event, FundsWithdrawn)
    assert event.amount == 400
    assert event.balance == 600


def test_withdraw_cannot_overdraw() -> None:
    wallet = Wallet.open("wlt-3", "owner-3", Currency.EUR)
    wallet.deposit(Money(500, Currency.EUR))
    wallet.clear_events()
    with pytest.raises(BusinessRuleViolation) as exc:
        wallet.withdraw(Money(501, Currency.EUR))
    assert exc.value.rule == "wallet-insufficient-funds"
    # invariant held: balance unchanged, no event raised
    assert wallet.balance == Money(500, Currency.EUR)
    assert wallet.pending_events() == []


def test_deposit_must_be_positive() -> None:
    wallet = Wallet.open("wlt-4", "owner-4", Currency.EUR)
    with pytest.raises(BusinessRuleViolation) as exc:
        wallet.deposit(Money(0, Currency.EUR))
    assert exc.value.rule == "wallet-deposit-positive"


def test_currency_mismatch_is_rejected() -> None:
    wallet = Wallet.open("wlt-5", "owner-5", Currency.EUR)
    with pytest.raises(BusinessRuleViolation) as exc:
        wallet.deposit(Money(100, Currency.USD))
    assert exc.value.rule == "wallet-currency-mismatch"
:::

Three details deserve attention. First, `Wallet.open` takes three positional arguments: a pre-generated `wallet_id`, an `owner_id`, and a `Currency` enum value — the aggregate does not generate its own ID. Second, `pending_events()` returns buffered events without draining; `clear_events()` returns and drains. The `test_deposit_then_withdraw_happy_path` test calls `clear_events()` after opening so each assertion sees exactly one event. Third, `FundsDeposited` and `FundsWithdrawn` carry `amount` (the operation amount in minor units) and `balance` (the post-operation running balance) — not `new_balance`. Always verify real event dataclass fields before asserting them.

!!! spring "Spring parity"
    Testing a DDD aggregate in isolation is the same discipline in any stack. In
    Spring / jMolecules you would call the aggregate's methods directly and
    check `aggregate.domainEvents()` (provided by `AbstractAggregateRoot`)
    before calling `afterDomainEventPublication()` to drain the buffer. PyFly's
    `clear_events()` plays the same role — drain, assert, move on.

---

## Wiring the test stack with conftest.py

The CQRS and event-listener tests need real infrastructure: a SQLite-backed `WalletRepository`, an event bus, command and query handlers, and a running bus. Rather than recreating this in every test module, Lumen declares the wiring once in `tests/conftest.py`. Pytest discovers the file automatically and makes the fixtures available to every test in the package.

The key difference from a hand-rolled adapter test is that the `repository` fixture uses the **real framework `WalletRepository`** — the same Spring-Data-style class the application boots — and runs it through the **real `RepositoryBeanPostProcessor`**, which compiles derived-query stubs from method names at startup.

::: listing tests/conftest.py | Listing 16.3 — conftest.py: real framework components wired with no mocks
from __future__ import annotations

import sys
from collections.abc import AsyncIterator
from pathlib import Path

import pytest_asyncio
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

# Make the sample's `src/` importable
_HERE = Path(__file__).resolve().parent
_SRC = _HERE.parent / "src"
sys.path.insert(0, str(_SRC))

from lumen.core.services.listeners import WalletAuditListener
from lumen.core.services.wallets import (
    DepositFundsHandler,
    GetBalanceHandler,
    GetWalletHandler,
    ListRichWalletsHandler,
    ListWalletsHandler,
    OpenWalletHandler,
    WithdrawFundsHandler,
)
from lumen.models.entities.v1.wallet_orm import WalletEntity
from lumen.models.repositories import WalletRepository

from pyfly.cqrs import DefaultCommandBus, DefaultQueryBus, HandlerRegistry
from pyfly.data.relational.sqlalchemy import Base
from pyfly.data.relational.sqlalchemy.post_processor import (
    RepositoryBeanPostProcessor,
)
from pyfly.eda.adapters.memory import InMemoryEventBus


@pytest_asyncio.fixture
async def session_factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """An in-memory SQLite engine + session factory, schema created.

    Mirrors the framework's relational auto-configuration: build the
    async engine and run Base.metadata.create_all.
    """
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        yield factory
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def repository(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[WalletRepository]:
    """The framework WalletRepository, post-processed.

    RepositoryBeanPostProcessor compiles derived-query stubs
    (e.g. find_by_owner_id) onto the bean — the same step the
    ApplicationContext runs at startup.
    """
    session = session_factory()
    repo = WalletRepository(WalletEntity, session)
    RepositoryBeanPostProcessor().after_init(repo, "walletRepository")
    try:
        yield repo
    finally:
        await session.close()


@pytest_asyncio.fixture
async def event_bus() -> AsyncIterator[InMemoryEventBus]:
    """A real in-memory EDA bus — the same EventPublisher used in
    production."""
    yield InMemoryEventBus()


@pytest_asyncio.fixture
async def audit_listener(
    event_bus: InMemoryEventBus,
) -> AsyncIterator[WalletAuditListener]:
    """The wallet audit projection, subscribed to the bus exactly as the
    ApplicationContext auto-wires it at startup."""
    listener = WalletAuditListener()
    method = listener.on_wallet_event
    for pattern in method.__pyfly_event_patterns__:
        event_bus.subscribe(pattern, method)
    yield listener


@pytest_asyncio.fixture
async def command_bus(
    repository: WalletRepository,
    event_bus: InMemoryEventBus,
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[DefaultCommandBus]:
    registry = HandlerRegistry()
    registry.register_command_handler(
        OpenWalletHandler(
            repository=repository,
            events=event_bus,
            session_factory=session_factory,
        )
    )
    registry.register_command_handler(
        DepositFundsHandler(
            repository=repository,
            events=event_bus,
            session_factory=session_factory,
        )
    )
    registry.register_command_handler(
        WithdrawFundsHandler(
            repository=repository,
            events=event_bus,
            session_factory=session_factory,
        )
    )
    yield DefaultCommandBus(registry=registry)


@pytest_asyncio.fixture
async def query_bus(
    repository: WalletRepository,
) -> AsyncIterator[DefaultQueryBus]:
    registry = HandlerRegistry()
    registry.register_query_handler(GetWalletHandler(repository=repository))
    registry.register_query_handler(GetBalanceHandler(repository=repository))
    registry.register_query_handler(
        ListWalletsHandler(repository=repository)
    )
    registry.register_query_handler(
        ListRichWalletsHandler(repository=repository)
    )
    yield DefaultQueryBus(registry=registry)
:::

Each fixture is declared with `@pytest_asyncio.fixture` (not the bare `@pytest.fixture`) so pytest-asyncio manages the async iterator lifecycle. `asyncio_mode = "auto"` in `pyproject.toml` makes async fixtures and tests work without per-function decorators — but the fixture decorator itself must still be `pytest_asyncio.fixture`.

The `session_factory` fixture is shared. `repository` and `command_bus` both receive it, so the same in-memory SQLite engine backs reads, writes, and the `@transactional` boundary the handlers open. The `audit_listener` and `command_bus` fixtures both receive `event_bus`; pytest instantiates that once per test and shares it between them, so events published by the command handlers are visible to the listener.

!!! tip "No mocks anywhere"
    Every component in `conftest.py` is the real production implementation.
    `WalletRepository` is the same class the application boots.
    `RepositoryBeanPostProcessor` is the same post-processor the
    `ApplicationContext` runs at startup to compile derived-query stubs.
    `InMemoryEventBus` is the same bus used in non-Kafka deployments. The
    goal is to test the real code paths, not the wiring.

---

## Testing the CQRS flow end to end

With the fixtures from `conftest.py`, exercising the full command/query cycle is a matter of calling `command_bus.send(...)` and `query_bus.query(...)`. No handler is instantiated in the test body — the bus dispatches to the handler already registered in the fixture.

::: listing tests/test_cqrs_flow.py | Listing 16.4 — End-to-end CQRS tests through the real bus
from __future__ import annotations

import pytest
from lumen.core.services.wallets.deposit_funds_command import DepositFunds
from lumen.core.services.wallets.get_balance_query import GetBalance
from lumen.core.services.wallets.get_wallet_query import GetWallet
from lumen.core.services.wallets.open_wallet_command import OpenWallet
from lumen.core.services.wallets.withdraw_funds_command import WithdrawFunds
from lumen.interfaces.enums.v1.currency import Currency

from pyfly.cqrs import DefaultCommandBus, DefaultQueryBus


@pytest.mark.asyncio
async def test_full_wallet_lifecycle(
    command_bus: DefaultCommandBus,
    query_bus: DefaultQueryBus,
) -> None:
    wallet_id = await command_bus.send(
        OpenWallet(owner_id="u-1", currency=Currency.EUR)
    )
    assert isinstance(wallet_id, str) and wallet_id.startswith("wlt-")

    balance = await command_bus.send(
        DepositFunds(wallet_id=wallet_id, amount=1500)
    )
    assert balance == 1500

    balance = await command_bus.send(
        WithdrawFunds(wallet_id=wallet_id, amount=500)
    )
    assert balance == 1000

    wallet = await query_bus.query(GetWallet(wallet_id=wallet_id))
    assert wallet is not None
    assert wallet.id == wallet_id
    assert wallet.owner_id == "u-1"
    assert wallet.currency is Currency.EUR
    assert wallet.balance_minor == 1000
    assert wallet.balance == 10.0

    balance_dto = await query_bus.query(GetBalance(wallet_id=wallet_id))
    assert balance_dto is not None
    assert balance_dto.balance_minor == 1000
    assert balance_dto.balance == 10.0


@pytest.mark.asyncio
async def test_get_wallet_returns_none_for_unknown_id(
    query_bus: DefaultQueryBus,
) -> None:
    assert await query_bus.query(
        GetWallet(wallet_id="wlt-does-not-exist")
    ) is None


@pytest.mark.asyncio
async def test_overdraw_is_rejected_through_the_bus(
    command_bus: DefaultCommandBus,
) -> None:
    from pyfly.cqrs.exceptions import CommandProcessingException

    wallet_id = await command_bus.send(
        OpenWallet(owner_id="u-2", currency=Currency.EUR)
    )
    await command_bus.send(DepositFunds(wallet_id=wallet_id, amount=100))

    with pytest.raises(CommandProcessingException):
        await command_bus.send(
            WithdrawFunds(wallet_id=wallet_id, amount=999)
        )


@pytest.mark.asyncio
async def test_validation_rejects_non_positive_deposit(
    command_bus: DefaultCommandBus,
) -> None:
    from pyfly.cqrs.exceptions import CommandProcessingException

    wallet_id = await command_bus.send(
        OpenWallet(owner_id="u-3", currency=Currency.EUR)
    )
    with pytest.raises(CommandProcessingException):
        await command_bus.send(DepositFunds(wallet_id=wallet_id, amount=0))


@pytest.mark.asyncio
async def test_deposit_to_unknown_wallet_is_rejected(
    command_bus: DefaultCommandBus,
) -> None:
    from pyfly.cqrs.exceptions import CommandProcessingException

    with pytest.raises(CommandProcessingException):
        await command_bus.send(
            DepositFunds(wallet_id="wlt-nope", amount=100)
        )
:::

`test_full_wallet_lifecycle` is the primary smoke test: it sends every command in the natural order and then queries both the full wallet DTO and the balance DTO. The wallet DTO exposes `balance_minor` (integer minor units) and `balance` (major units as a float); both derive from the same `WalletEntity` row stored through the repository.

The error-path tests verify that the bus surfaces domain violations correctly. **`CommandProcessingException`** is the bus's wrapper for any exception raised inside a handler — including `BusinessRuleViolation` from the aggregate. Calling code never sees the raw domain exception; it always sees the bus wrapper.

!!! note "asyncio_mode = \"auto\" and @pytest.mark.asyncio"
    With `asyncio_mode = "auto"` every async test is collected and run
    automatically. The `@pytest.mark.asyncio` decorator is **not required** but
    is harmless and makes the async intent explicit at a glance. Lumen keeps it
    for clarity.

---

## Testing the repository adapter

The CQRS flow tests prove the full open/deposit/withdraw/query pipeline. The repository adapter tests go one level deeper: they directly exercise the `WalletRepository` API — CRUD, **derived queries** compiled from method names, **`Pageable`/`Page`** pagination, and **`Specification`** predicates — against a temporary SQLite file database. No Docker, no external process, no network. pytest's built-in `tmp_path` fixture provides a temporary directory cleaned up automatically after each test.

The local fixture `_make_repo` mirrors what the `ApplicationContext` does at startup: construct the repository and run `RepositoryBeanPostProcessor.after_init` to compile the derived-query stubs. Without that call, methods like `find_by_owner_id` would raise `NotImplementedError`.

::: listing tests/test_sql_wallet_repository.py | Listing 16.5 — Repository tests: CRUD, derived query, pagination, Specification
from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from lumen.models.entities.v1.wallet_orm import WalletEntity
from lumen.models.repositories.wallet_repository import (
    WalletRepository,
    balance_at_least,
)
from pyfly.data import Pageable, Sort
from pyfly.data.relational.sqlalchemy import Base
from pyfly.data.relational.sqlalchemy.post_processor import (
    RepositoryBeanPostProcessor,
)


def _entity(
    wid: str,
    owner: str,
    minor: int,
    *,
    currency: str = "EUR",
    age_days: int = 0,
) -> WalletEntity:
    created = datetime.now(UTC) - timedelta(days=age_days)
    return WalletEntity(
        id=wid,
        owner_id=owner,
        currency=currency,
        balance_minor=minor,
        created_at=created,
    )


def _make_repo(session: AsyncSession) -> WalletRepository:
    repo = WalletRepository(WalletEntity, session)
    # Mirror the ApplicationContext: compile derived-query stubs.
    RepositoryBeanPostProcessor().after_init(repo, "walletRepository")
    return repo


@pytest_asyncio.fixture
async def sqlite_factory(
    tmp_path: Path,
) -> AsyncIterator[tuple[async_sessionmaker[AsyncSession], str]]:
    """A temp-file SQLite engine + session factory, schema created.

    Yields the session factory and the database URL so the test can
    reconnect with a fresh engine to verify true persistence.
    """
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'wallets.db'}"
    engine = create_async_engine(db_url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        yield factory, db_url
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_upsert_inserts_then_updates_and_persists(
    sqlite_factory: tuple[async_sessionmaker[AsyncSession], str],
) -> None:
    factory, db_url = sqlite_factory

    async with factory() as session:
        repo = _make_repo(session)
        await repo.upsert(_entity("wlt-1", "owner-42", 0, currency="USD"))
        # update: same PK, new balance
        await repo.upsert(_entity("wlt-1", "owner-42", 2500, currency="USD"))
        await session.commit()

        got = await repo.find_by_id("wlt-1")
        assert got is not None
        assert got.owner_id == "owner-42"
        assert got.currency == "USD"
        assert got.balance_minor == 2500
        assert await repo.count() == 1

    # prove persistence: reconnect with a brand-new engine/session
    fresh_engine = create_async_engine(db_url)
    fresh_factory = async_sessionmaker(fresh_engine, expire_on_commit=False)
    try:
        async with fresh_factory() as fresh_session:
            fresh_repo = _make_repo(fresh_session)
            persisted = await fresh_repo.find_by_id("wlt-1")
            assert persisted is not None, "wallet should survive a reconnect"
            assert persisted.balance_minor == 2500
    finally:
        await fresh_engine.dispose()


@pytest.mark.asyncio
async def test_find_by_id_unknown_returns_none(
    sqlite_factory: tuple[async_sessionmaker[AsyncSession], str],
) -> None:
    factory, _ = sqlite_factory
    async with factory() as session:
        repo = _make_repo(session)
        assert await repo.find_by_id("wlt-nope") is None


@pytest.mark.asyncio
async def test_derived_find_by_owner_id(
    sqlite_factory: tuple[async_sessionmaker[AsyncSession], str],
) -> None:
    factory, _ = sqlite_factory
    async with factory() as session:
        repo = _make_repo(session)
        await repo.upsert(_entity("wlt-1", "alice", 100))
        await repo.upsert(_entity("wlt-2", "alice", 200))
        await repo.upsert(_entity("wlt-3", "bob", 300))
        await session.commit()

        owned = await repo.find_by_owner_id("alice")
        assert sorted(w.id for w in owned) == ["wlt-1", "wlt-2"]
        assert await repo.find_by_owner_id("nobody") == []


@pytest.mark.asyncio
async def test_specification_find_rich_paged_and_sorted(
    sqlite_factory: tuple[async_sessionmaker[AsyncSession], str],
) -> None:
    factory, _ = sqlite_factory
    async with factory() as session:
        repo = _make_repo(session)
        # age_days drives created_at so we can assert newest-first ordering.
        await repo.upsert(_entity("wlt-poor", "a", 50, age_days=3))
        await repo.upsert(_entity("wlt-mid", "b", 1000, age_days=2))
        await repo.upsert(_entity("wlt-rich", "c", 5000, age_days=1))
        await session.commit()

        # Specification: balance_minor >= 1000, newest first, page size 1.
        newest_first = Sort.by("created_at").descending()
        page = await repo.find_rich(1000, Pageable.of(1, 1, newest_first))
        assert page.total == 2          # mid + rich
        assert page.total_pages == 2
        assert page.has_next is True
        assert [w.id for w in page.items] == ["wlt-rich"]

        page2 = await repo.find_rich(1000, Pageable.of(2, 1, newest_first))
        assert [w.id for w in page2.items] == ["wlt-mid"]

        # The bare predicate also works through find_all_by_spec.
        rich = await repo.find_all_by_spec(balance_at_least(5000))
        assert [w.id for w in rich] == ["wlt-rich"]


@pytest.mark.asyncio
async def test_find_paginated_counts_and_pages(
    sqlite_factory: tuple[async_sessionmaker[AsyncSession], str],
) -> None:
    factory, _ = sqlite_factory
    async with factory() as session:
        repo = _make_repo(session)
        for i in range(5):
            await repo.upsert(
                _entity(f"wlt-{i}", "owner", i * 100, age_days=5 - i)
            )
        await session.commit()

        page = await repo.find_paginated(
            pageable=Pageable.of(1, 2, Sort.by("created_at").descending())
        )
        assert page.total == 5
        assert page.total_pages == 3
        assert len(page.items) == 2
        # newest first -> wlt-4 (age 1 day), then wlt-3
        assert [w.id for w in page.items] == ["wlt-4", "wlt-3"]
:::

Four things to note. First, `_make_repo` calls `RepositoryBeanPostProcessor().after_init(repo, ...)` — without this, `find_by_owner_id` is still a stub and raises `NotImplementedError`. The post-processor compiles the method name into a SQLAlchemy `WHERE owner_id = :owner_id` clause. Second, `upsert` is the repository's insert-or-update; after each batch of upserts, `await session.commit()` flushes to SQLite. Third, `find_rich` takes a minimum balance and a `Pageable`; it delegates to `find_all_by_spec_paged(balance_at_least(min), pageable)`. Fourth, the two-engine pattern in `test_upsert_inserts_then_updates_and_persists` proves true durability: data committed through one engine is readable by a completely fresh engine and session.

!!! spring "Spring parity"
    This test layer is the Python equivalent of `@DataJpaTest` with an embedded
    H2 database in Spring Boot. `@DataJpaTest` loads only the JPA layer (entities,
    repositories, Flyway) and wires a fresh in-memory H2 for every test class.
    The `sqlite_factory` fixture does the same: create the schema, run the tests,
    dispose the engine. No Docker, no external process.

!!! tip "Derived queries are method-name conventions"
    `WalletRepository.find_by_owner_id` is declared as a stub
    (`raise NotImplementedError`). `RepositoryBeanPostProcessor` inspects the
    method name at startup — `find_by_owner_id` → `WHERE owner_id = :value` —
    and replaces the stub with a real coroutine. Testing this method therefore
    also tests that the post-processor convention is working correctly.

---

## Testing the event listener

`WalletAuditListener` listens for domain events published by the command handlers. Testing it end to end — command runs on the bus, handler publishes events, listener receives them — requires all three components to share the same `InMemoryEventBus`. The `conftest.py` fixtures already arrange this: both `command_bus` and `audit_listener` accept an `event_bus` argument, and pytest injects the same instance into both.

::: listing tests/test_event_listener.py | Listing 16.6 — Event listener tests: command publishes, listener observes
from __future__ import annotations

import pytest
from lumen.core.services.listeners import WalletAuditListener
from lumen.core.services.wallets.deposit_funds_command import DepositFunds
from lumen.core.services.wallets.open_wallet_command import OpenWallet
from lumen.core.services.wallets.withdraw_funds_command import WithdrawFunds
from lumen.interfaces.enums.v1.currency import Currency

from pyfly.cqrs import DefaultCommandBus


@pytest.mark.asyncio
async def test_listener_observes_wallet_events(
    command_bus: DefaultCommandBus,
    audit_listener: WalletAuditListener,
) -> None:
    wallet_id = await command_bus.send(
        OpenWallet(owner_id="u-1", currency=Currency.EUR)
    )
    await command_bus.send(DepositFunds(wallet_id=wallet_id, amount=1500))
    await command_bus.send(WithdrawFunds(wallet_id=wallet_id, amount=400))

    entries = audit_listener.entries_for(wallet_id)
    assert [e.event_type for e in entries] == [
        "WalletOpened",
        "FundsDeposited",
        "FundsWithdrawn",
    ]

    # The payload carried the real domain-event fields.
    deposited = entries[1]
    assert deposited.payload["amount"] == 1500
    assert deposited.payload["currency"] == "EUR"
    assert deposited.payload["balance"] == 1500
    assert deposited.event_id  # the aggregate's DomainEvent.event_id

    # The running-total projection reflects deposit − withdrawal.
    assert audit_listener.running_total(wallet_id) == 1100


@pytest.mark.asyncio
async def test_listener_records_nothing_before_any_command(
    audit_listener: WalletAuditListener,
) -> None:
    assert audit_listener.entries == []
    assert audit_listener.running_total("anything") == 0


@pytest.mark.asyncio
async def test_event_type_matches_domain_event_class_names(
    command_bus: DefaultCommandBus,
    audit_listener: WalletAuditListener,
) -> None:
    # A rejected withdrawal raises no event — it must not appear in the log.
    wallet_id = await command_bus.send(
        OpenWallet(owner_id="u-2", currency=Currency.USD)
    )
    await command_bus.send(DepositFunds(wallet_id=wallet_id, amount=100))

    from pyfly.cqrs.exceptions import CommandProcessingException

    with pytest.raises(CommandProcessingException):
        await command_bus.send(
            WithdrawFunds(wallet_id=wallet_id, amount=9999)
        )

    types = [e.event_type for e in audit_listener.entries_for(wallet_id)]
    assert types == ["WalletOpened", "FundsDeposited"]
    assert audit_listener.running_total(wallet_id) == 100
:::

`test_listener_observes_wallet_events` is the core integration proof: three commands produce three events, the listener records all three in order, the payload fields match the aggregate's event dataclass fields, and the `running_total` projection equals the arithmetic result. No bus mock, no event capture list — the production listener runs on the production bus.

`test_event_type_matches_domain_event_class_names` proves a domain invariant: a rejected command (overdraw) raises no event. The audit log must never record a side effect from a failed operation.

!!! tip "event_type is the class name"
    PyFly's event publisher sets `event_type` to the domain event class name:
    `"WalletOpened"`, `"FundsDeposited"`, `"FundsWithdrawn"`. The
    `@event_listener(pattern)` decorator on `WalletAuditListener.on_wallet_event`
    uses a glob pattern (`"Wallet*"`, `"Funds*"`) to subscribe to all three.
    The test asserts the string class names directly.

---

## Booted-context integration test

The unit tests, CQRS flow tests, and repository tests each wire one layer of the stack. The booted-context integration test wires everything at once: it starts the real `ApplicationContext` — DI component scan, CQRS auto-configuration, relational auto-configuration, `RepositoryBeanPostProcessor`, `@transactional` seam, EDA event bus — then resolves the `DefaultCommandBus` and `DefaultQueryBus` from the context and drives the full wallet lifecycle.

The database URL is overridden via an environment variable so the test never touches the developer's `lumen.db`.

::: listing tests/test_app_context_integration.py | Listing 16.7 — Booted-context integration: full DI + persistence composition
from __future__ import annotations

import logging
import sys
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession

_HERE = Path(__file__).resolve().parent
_SRC = _HERE.parent / "src"
sys.path.insert(0, str(_SRC))


@pytest_asyncio.fixture
async def booted_context(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[object]:
    """Boot the full LumenApplication against an isolated SQLite file."""
    db_path = tmp_path / "lumen-it.db"
    monkeypatch.setenv(
        "PYFLY_DATA_RELATIONAL_URL",
        f"sqlite+aiosqlite:///{db_path}",
    )
    # Silence the pool-GC warning that aiosqlite emits at teardown.
    logging.getLogger(
        "sqlalchemy.pool.impl.AsyncAdaptedQueuePool"
    ).setLevel(logging.CRITICAL)

    from lumen.app import LumenApplication
    from pyfly.core import PyFlyApplication

    app = PyFlyApplication(
        LumenApplication,
        config_path=str(_HERE.parent / "pyfly.yaml"),
    )
    await app.startup()
    try:
        yield app.context
    finally:
        await app.context.get_bean(AsyncSession).close()
        await app.shutdown()


@pytest.mark.asyncio
async def test_full_lifecycle_through_booted_context(
    booted_context: object,
) -> None:
    from lumen.core.services.wallets.deposit_funds_command import DepositFunds
    from lumen.core.services.wallets.get_balance_query import GetBalance
    from lumen.core.services.wallets.get_wallet_query import GetWallet
    from lumen.core.services.wallets.list_rich_wallets_query import (
        ListRichWallets,
    )
    from lumen.core.services.wallets.list_wallets_query import ListWallets
    from lumen.core.services.wallets.open_wallet_command import OpenWallet
    from lumen.core.services.wallets.withdraw_funds_command import WithdrawFunds
    from lumen.interfaces.enums.v1.currency import Currency
    from pyfly.cqrs import DefaultCommandBus, DefaultQueryBus
    from pyfly.data import Pageable

    ctx = booted_context
    commands = ctx.get_bean(DefaultCommandBus)
    queries = ctx.get_bean(DefaultQueryBus)

    # --- open -> deposit -> withdraw, each a committed unit of work -------
    w1 = await commands.send(OpenWallet(owner_id="u-1", currency=Currency.EUR))
    w2 = await commands.send(OpenWallet(owner_id="u-2", currency=Currency.EUR))
    assert w1.startswith("wlt-") and w2.startswith("wlt-")

    assert await commands.send(DepositFunds(wallet_id=w1, amount=5000)) == 5000
    assert await commands.send(WithdrawFunds(wallet_id=w1, amount=1500)) == 3500
    assert await commands.send(DepositFunds(wallet_id=w2, amount=100)) == 100

    # --- persistence survived: reload the aggregate via the query side ---
    reloaded = await queries.query(GetWallet(wallet_id=w1))
    assert reloaded is not None
    assert reloaded.owner_id == "u-1"
    assert reloaded.balance_minor == 3500

    # --- paged list (find_paginated + Page.map) --------------------------
    page = await queries.query(ListWallets(pageable=Pageable.of(1, 10)))
    assert page.total == 2
    assert {w.id for w in page.items} == {w1, w2}

    # --- Specification: only wallets with balance >= 1000 ----------------
    rich = await queries.query(
        ListRichWallets(min_minor=1000, pageable=Pageable.of(1, 10))
    )
    assert rich.total == 1
    assert [w.id for w in rich.items] == [w1]

    everyone = await queries.query(
        ListRichWallets(min_minor=0, pageable=Pageable.of(1, 10))
    )
    assert everyone.total == 2

    # --- projection-backed balance ---------------------------------------
    balance = await queries.query(GetBalance(wallet_id=w1))
    assert balance is not None
    assert balance.balance_minor == 3500
    assert balance.balance == 35.0
:::

`booted_context` uses pytest's built-in `monkeypatch` fixture to set `PYFLY_DATA_RELATIONAL_URL` before the application boots. The framework reads this environment variable during relational auto-configuration, so the context uses the isolated temp-file SQLite database for the life of the test, then disposes of it when the fixture tears down.

`test_full_lifecycle_through_booted_context` exercises every query type the application exposes: `GetWallet` (aggregate reload), `ListWallets` (paginated list using `find_paginated`), `ListRichWallets` (Specification predicate using `find_all_by_spec_paged`), and `GetBalance` (projection-backed balance). It proves that the `RepositoryBeanPostProcessor`, the `@transactional` boundary around each command handler, and the DI wiring all compose correctly in a single boot.

!!! spring "Spring parity"
    This test is the Python equivalent of `@SpringBootTest` with an embedded H2
    database. `@SpringBootTest` loads the full application context, including all
    auto-configurations and the JPA layer; you set `spring.datasource.url` in
    `application-test.properties` to redirect to H2. PyFly's environment
    variable override (`monkeypatch.setenv`) plays the same role. Both
    approaches prove that the composed application works end to end without any
    external infrastructure.

---

## Framework testing helpers

The tests above cover Lumen's full pyramid with standard pytest primitives and PyFly's real production components. For larger applications or teams that prefer more structure, `pyfly.testing` ships higher-level helpers that mirror Spring Boot's testing annotations.

**`PyFlyTestCase` + `mock_bean(T)`** work like `@MockBean` in `@SpringBootTest`. Declare `repo = mock_bean(WalletDomainRepository)` on the class body; `setup()` installs an `AsyncMock(spec=T)` into the application context and wires it into any collaborator that depends on it.

**`create_test_container(overrides={Interface: Implementation})`** builds a DI container with fakes registered for specific interfaces. Resolve the class under test from it and its dependencies are already injected.

**`assert_event_published(events, event_type, payload_contains=...)`** scans a captured `EventEnvelope` list for the first envelope with the given type, optionally checks payload keys, and returns the envelope for further assertions. `assert_no_events_published(events)` fails if the list is non-empty.

**Testcontainers integration** (`postgres_container()`, `redis_container()`, `pyfly_config(container, base={...})`) is PyFly's equivalent of Spring Boot's `@Testcontainers` + `@ServiceConnection`. Start a real Postgres container; `pyfly_config` rewrites the sync `psycopg2://` URL to `postgresql+asyncpg://` and merges it into a `Config` ready to boot an `ApplicationContext`. Install support with:

```bash
pip install 'pyfly[testcontainers]'
```

Guard every Testcontainers test with `@requires_docker` so it skips cleanly on machines without Docker and runs automatically on CI runners that have it:

```python
from pyfly.testing import postgres_container, pyfly_config, requires_docker

@requires_docker
async def test_wallet_round_trip_against_real_postgres():
    with postgres_container() as pg:
        config = pyfly_config(pg, base={"pyfly.data.enabled": True})
        assert config.get("pyfly.data.relational.url").startswith(
            "postgresql+asyncpg://"
        )
        ...
```

Lumen does not use these helpers — SQLite covers the persistence layer without Docker, and the in-memory bus covers event routing. Reach for them when your project has infrastructure that cannot be reproduced without a real daemon.

---

## What you built {.recap}

Lumen now has 41 passing tests across six files, exercising every layer of the pyramid.

At the base, `test_money.py` and `test_wallet_aggregate.py` prove the domain model's arithmetic, immutability, and invariant rules. All tests are synchronous, pure Python functions — no fixtures, no DI, no `async`. The `BusinessRuleViolation.rule` attribute makes each assertion specific to the exact violated invariant.

In the middle, `conftest.py` wires the real components — the framework `WalletRepository` over an in-memory SQLite engine, `InMemoryEventBus`, all five command and query handlers (including `ListWalletsHandler` and `ListRichWalletsHandler`), and `WalletAuditListener` — into reusable async fixtures that pytest shares automatically across modules. The `RepositoryBeanPostProcessor` is applied to the repository fixture exactly as the `ApplicationContext` applies it at startup. `test_cqrs_flow.py` dispatches commands and queries through the real bus and checks every field of the query DTOs. `test_event_listener.py` proves that the audit listener observes exactly the events produced by successful commands and nothing from rejected ones.

`test_sql_wallet_repository.py` exercises the `WalletRepository` directly against a temporary SQLite file, covering the full CRUD surface, the derived query `find_by_owner_id` (compiled from the method name by `RepositoryBeanPostProcessor`), the `find_paginated` API that returns a `Page` with total count and page metadata, and the `Specification` predicate path via `find_rich` / `find_all_by_spec`. The two-engine reconnect pattern proves true durability.

At the peak, `test_app_context_integration.py` boots the real `LumenApplication` with the database URL overridden to an isolated SQLite file, then drives the full open → deposit → withdraw → list → rich → balance lifecycle through the context-resolved buses. This single test proves that the DI scan, CQRS auto-configuration, `RepositoryBeanPostProcessor`, and `@transactional` boundary all compose correctly.

Concretely, you learned:

- **`asyncio_mode = "auto"` + `pythonpath = ["src"]`** in `pyproject.toml` —
  all async tests run without decorators; the `src/` layout is importable.
- **`uv run --extra dev pytest -q`** — the bare `uv sync` drops the dev group;
  always include `--extra dev` to get pytest.
- **`@pytest_asyncio.fixture`** — async fixture lifecycle managed by
  pytest-asyncio; plain `@pytest.fixture` does not handle async generators.
- **Shared fixture instances** — when two fixtures request the same fixture
  name (e.g., `event_bus`, `session_factory`), pytest resolves it once per
  test and shares the instance.
- **`RepositoryBeanPostProcessor().after_init(repo, name)`** — must be
  called in tests that exercise derived queries; without it, the method
  stubs raise `NotImplementedError`.
- **Derived queries** (`find_by_owner_id`) — declared as stubs; the
  post-processor compiles them to `WHERE owner_id = :value` at startup.
- **`Pageable.of(page, size, sort)` + `Page`** — the `find_paginated`
  API returns a `Page` with `total`, `total_pages`, `has_next`, and
  `items`; assert each field for pagination correctness.
- **`Specification` predicates** — `balance_at_least(n)` is passed to
  `find_rich` / `find_all_by_spec` to filter by an arbitrary predicate
  without adding a new derived-query method.
- **`pending_events()` vs `clear_events()`** — `pending_events()` reads
  without draining; `clear_events()` drains. Always call `clear_events()` in
  arrange steps so assertions only see events from the act step.
- **`BusinessRuleViolation.rule`** — assert the exact rule string, not just
  the exception class, to prove that the right invariant fired.
- **`monkeypatch.setenv`** — override configuration before booting the
  context in integration tests; the framework reads environment variables
  during auto-configuration.
- **Framework helpers** (`PyFlyTestCase`, `mock_bean`, `create_test_container`,
  Testcontainers) — available in `pyfly.testing` for projects that need them;
  Lumen keeps things simple with real components.

---

## Try it yourself {.exercises}

1. **Add a test for zero-amount withdrawal.** In `test_wallet_aggregate.py`,
   add `test_withdraw_zero_is_rejected`. Open a wallet, deposit 500 EUR, then
   attempt `wallet.withdraw(Money(0, Currency.EUR))`. Assert that a
   `BusinessRuleViolation` is raised and check its `.rule` attribute. Compare
   the rule name with the deposit equivalent — are the rules symmetric?

2. **Test an unknown wallet via the CQRS bus.** In `test_cqrs_flow.py`, add
   `test_withdraw_from_unknown_wallet_is_rejected`. Send only a
   `WithdrawFunds(wallet_id="wlt-ghost", amount=50)` command without opening a
   wallet first. Assert that `CommandProcessingException` is raised. Confirm
   that the repository still holds no wallets by querying
   `GetWallet(wallet_id="wlt-ghost")` and asserting `None`.

3. **Extend the listener test with a second wallet.** In
   `test_event_listener.py`, add a test that opens two wallets (`u-A` and
   `u-B`), deposits different amounts into each, and then calls
   `audit_listener.entries_for(wallet_id_A)` and
   `audit_listener.entries_for(wallet_id_B)` separately. Assert that each
   returns exactly two entries (`WalletOpened` + `FundsDeposited`) and that
   the payload `amount` values differ. This proves that `entries_for` filters
   by wallet ID, not by event type.

4. **Add a multi-owner pagination test.** In `test_sql_wallet_repository.py`,
   add a test that inserts ten wallets with two different owners, calls
   `find_by_owner_id` for each owner, and then calls `find_paginated` with
   `Pageable.of(1, 3, Sort.by("balance_minor").descending())`. Assert that
   `page.total == 10`, `page.total_pages == 4`, and that the first item in
   `page.items` has the highest `balance_minor`. This proves that pagination
   is independent of the derived-query filter.
