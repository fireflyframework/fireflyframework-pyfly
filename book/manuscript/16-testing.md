<span class="eyebrow">Chapter 16</span>

# Testing PyFly Applications {.chtitle}

::: figure art/openers/ch16.svg | &nbsp;

The wallet works. Deposits land, balances update, events propagate through the
bus, and the saga coordinator rolls back cleanly on failure. What you do not yet
have is **confidence** that it will keep working after the next refactor. That
confidence comes from tests — tests that run in milliseconds, tests that prove
the domain model enforces its invariants, tests that verify the CQRS pipeline
dispatches correctly, and tests that exercise the repository against a real
SQLite database file without any external infrastructure.

PyFly treats testing as a first-class concern. The `pyfly.testing` module ships
higher-level helpers — `PyFlyTestCase`, `create_test_container`,
`assert_event_published`, Testcontainers wiring — that you can reach for when
you need them. Lumen's own test suite does not use them: it wires the
real components directly from `conftest.py`, uses standard pytest fixtures, and
proves every layer of the pyramid with no boilerplate whatsoever. That is the
approach this chapter teaches.

This chapter works through the full testing pyramid for Lumen.

::: figure art/figures/16-testing.svg | Figure 16.1 — PyFly's testing pyramid. Fast unit tests form the wide base; integration tests sit in the middle; real-DB adapter tests crown the top.

The pyramid has three levels. **Unit tests** sit at the base — many of them,
running in milliseconds, exercising the domain model with no dependencies at
all. **CQRS flow tests** occupy the middle tier — the full open/deposit/
withdraw/query cycle routed through the real bus and the in-memory repository,
all wired in `conftest.py`. **SQLite adapter tests** crown the pyramid — a
small number that exercise the SQLAlchemy/aiosqlite repository against a
temporary file database, with no Docker required.

| Level            | Dependencies                  | Speed  | Lumen approach                    |
|------------------|------------------------------|--------|-----------------------------------|
| Unit             | None                         | Fast   | plain pytest, no fixtures         |
| CQRS flow        | In-memory bus + repository   | Fast   | conftest.py fixtures              |
| Repository/DB    | SQLite + aiosqlite           | Fast   | `tmp_path` + SQLAlchemy async     |

The project uses pytest with `pytest-asyncio` in **auto mode**. Enable it once
in `pyproject.toml` and every `async def test_*` function is picked up:

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

The bare `uv sync` (without `--extra dev`) drops the dev group, so pytest is
not installed. Always pass `--extra dev` when running tests.

---

## Unit-testing the domain

The domain model — `Money` and `Wallet` — has no framework dependencies. It
never touches a database, a message bus, or an HTTP client. That makes it the
ideal subject for pure, fast unit tests: construct objects, call methods, assert
outcomes. No mocks, no fixtures, no `async`.

### Testing Money

`Money` is a frozen dataclass. Every operation either succeeds and returns a
new `Money` or raises `BusinessRuleViolation`. Each violation carries a `.rule`
string that names the violated invariant — useful for asserting the exact rule
in tests.

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

Every test is synchronous — no `async`, no `await`, no fixtures. Pytest collects
the module-level functions automatically. `Currency.EUR` is an enum value, not
a plain string, matching the domain model's type contract exactly.

!!! tip "Minor-unit arithmetic"
    `Money` stores amounts in **minor units** (integer cents). `Money(1050,
    Currency.EUR)` represents €10.50 — verified by `major_units == 10.5` and
    `str(...) == "10.50 EUR"`. The `Money.zero(currency)` factory returns a
    `Money(0, currency)`, useful for initialising wallet balances.

### Testing the Wallet aggregate

`Wallet` enforces several invariants: the owner must be a non-blank string,
deposits must be positive, withdrawals must not overdraw, and amounts must
match the wallet's currency. Each rule violation carries a `.rule` attribute
for precise assertion.

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

Three details deserve attention. First, `Wallet.open` takes three positional
arguments: a pre-generated `wallet_id`, an `owner_id`, and a `Currency` enum
value — the aggregate does not generate its own ID. Second, `pending_events()`
returns the events buffered so far without draining the buffer; `clear_events()`
returns and drains it. The `test_deposit_then_withdraw_happy_path` test calls
`clear_events()` after opening so the deposit and withdrawal assertions each
see exactly one event. Third, `FundsDeposited` and `FundsWithdrawn` carry an
`amount` field (the operation amount in minor units) and a `balance` field (the
post-operation running balance) — not `new_balance`. Always check the real
event dataclass fields before asserting them.

!!! spring "Spring parity"
    Testing a DDD aggregate in isolation is the same discipline in any stack. In
    Spring / jMolecules you would call the aggregate's methods directly and
    check `aggregate.domainEvents()` (provided by `AbstractAggregateRoot`)
    before calling `afterDomainEventPublication()` to drain the buffer. PyFly's
    `clear_events()` plays the same role — drain, assert, move on.

---

## Wiring the test stack with conftest.py

The CQRS and event-listener tests need real infrastructure: a repository, an
event bus, command and query handlers, and a running bus. Rather than
re-creating this in every test module, Lumen declares the wiring once in
`tests/conftest.py`. Pytest discovers the file automatically and makes the
fixtures available to every test in the package.

::: listing tests/conftest.py | Listing 16.3 — conftest.py: real components wired with no mocks
from __future__ import annotations

import sys
from collections.abc import AsyncIterator
from pathlib import Path

import pytest_asyncio

# Make the sample's `src/` importable
_HERE = Path(__file__).resolve().parent
_SRC = _HERE.parent / "src"
sys.path.insert(0, str(_SRC))

from lumen.core.services.listeners import WalletAuditListener
from lumen.core.services.wallets import (
    DepositFundsHandler,
    GetBalanceHandler,
    GetWalletHandler,
    OpenWalletHandler,
    WithdrawFundsHandler,
)
from lumen.models.repositories import InMemoryWalletRepository

from pyfly.cqrs import (
    DefaultCommandBus,
    DefaultQueryBus,
    HandlerRegistry,
)
from pyfly.eda.adapters.memory import InMemoryEventBus


@pytest_asyncio.fixture
async def repository() -> AsyncIterator[InMemoryWalletRepository]:
    yield InMemoryWalletRepository()


@pytest_asyncio.fixture
async def event_bus() -> AsyncIterator[InMemoryEventBus]:
    """A real in-memory EDA bus — same EventPublisher as production."""
    yield InMemoryEventBus()


@pytest_asyncio.fixture
async def audit_listener(
    event_bus: InMemoryEventBus,
) -> AsyncIterator[WalletAuditListener]:
    """The wallet audit projection, subscribed to the bus exactly
    as ApplicationContext auto-wires it at startup."""
    listener = WalletAuditListener()
    method = listener.on_wallet_event
    for pattern in method.__pyfly_event_patterns__:
        event_bus.subscribe(pattern, method)
    yield listener


@pytest_asyncio.fixture
async def command_bus(
    repository: InMemoryWalletRepository,
    event_bus: InMemoryEventBus,
) -> AsyncIterator[DefaultCommandBus]:
    registry = HandlerRegistry()
    registry.register_command_handler(
        OpenWalletHandler(repository=repository, events=event_bus)
    )
    registry.register_command_handler(
        DepositFundsHandler(repository=repository, events=event_bus)
    )
    registry.register_command_handler(
        WithdrawFundsHandler(repository=repository, events=event_bus)
    )
    yield DefaultCommandBus(registry=registry)


@pytest_asyncio.fixture
async def query_bus(
    repository: InMemoryWalletRepository,
) -> AsyncIterator[DefaultQueryBus]:
    registry = HandlerRegistry()
    registry.register_query_handler(GetWalletHandler(repository=repository))
    registry.register_query_handler(GetBalanceHandler(repository=repository))
    yield DefaultQueryBus(registry=registry)
:::

Each fixture is declared with `@pytest_asyncio.fixture` (not the bare
`@pytest.fixture`) so pytest-asyncio manages the async iterator lifecycle.
`asyncio_mode = "auto"` in `pyproject.toml` makes async fixtures and tests
work without any per-function decorator — but the fixture decorator still has
to be `pytest_asyncio.fixture`.

The `audit_listener` fixture wires itself to the **same** `event_bus` that the
`command_bus` handlers publish to. Both fixtures receive the same instance
because pytest resolves fixtures by name within a test's dependency graph:
`command_bus` depends on `event_bus`, and so does `audit_listener` — pytest
instantiates `event_bus` once per test and shares it between them.

!!! tip "No mocks anywhere"
    Every component in `conftest.py` is the real production implementation.
    `InMemoryWalletRepository` is the adapter used in development; the
    `InMemoryEventBus` is the same bus the application uses in non-Kafka
    deployments. The goal is to test the real code paths, not the wiring.

---

## Testing the CQRS flow end to end

With the fixtures from `conftest.py`, exercising the full command/query cycle
is a matter of calling `command_bus.send(...)` and `query_bus.query(...)`.
No handler is instantiated in the test body — the bus dispatches to the handler
registered in the fixture.

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

`test_full_wallet_lifecycle` is the primary smoke test: it sends every command
in the natural order and then queries both the full wallet DTO and the balance
DTO. The wallet DTO exposes `balance_minor` (integer minor units) and `balance`
(major units as a float). Both views derive from the same `Money` object stored
in the repository.

The error-path tests verify that the bus surfaces domain violations correctly.
`CommandProcessingException` is the bus's wrapper for any exception raised
inside a handler — including `BusinessRuleViolation` from the aggregate. The
calling code never sees the raw domain exception; it always sees the bus
wrapper.

!!! note "asyncio_mode = \"auto\" and @pytest.mark.asyncio"
    With `asyncio_mode = "auto"` every async test is collected and run
    automatically. The `@pytest.mark.asyncio` decorator is **not required** but
    is harmless and makes the async intent explicit at a glance. Lumen keeps it
    for clarity.

---

## Testing the SQLite repository adapter

The in-memory repository proves the domain logic; the SQLAlchemy adapter test
proves the persistence layer. Lumen uses SQLite + `aiosqlite` — no Docker, no
external process, no network. The `tmp_path` fixture from pytest provides a
temporary directory that is cleaned up after each test.

::: listing tests/test_sql_wallet_repository.py | Listing 16.5 — SQLite adapter tests with a temporary file database
from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from lumen.interfaces.enums.v1.currency import Currency
from lumen.models.entities.v1.money import Money
from lumen.models.entities.v1.wallet_entity import Wallet
from lumen.models.repositories.sql_wallet_repository import (
    SqlAlchemyWalletRepository,
)
from pyfly.data.relational.sqlalchemy import Base


@pytest_asyncio.fixture
async def sqlite_session(
    tmp_path: Path,
) -> AsyncIterator[tuple[async_sessionmaker[AsyncSession], str]]:
    """Temp-file SQLite engine + session factory, schema created.

    Mirrors what PyFly's EngineLifecycle does at startup: build the
    async engine and run Base.metadata.create_all. Yields the session
    factory and the database URL so the test can reconnect later.
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
async def test_full_flow_persists_through_sqlite_adapter(
    sqlite_session: tuple[async_sessionmaker[AsyncSession], str],
) -> None:
    factory, db_url = sqlite_session

    # open -> deposit -> withdraw through the SQLite adapter
    async with factory() as session:
        repo = SqlAlchemyWalletRepository(session=session)

        wallet_id = await repo.next_id()
        wallet = Wallet.open(wallet_id, owner_id="owner-42", currency=Currency.USD)
        await repo.add(wallet)

        loaded = await repo.find(wallet_id)
        assert loaded is not None
        loaded.deposit(Money(2500, Currency.USD))
        await repo.add(loaded)

        loaded = await repo.find(wallet_id)
        assert loaded is not None
        loaded.withdraw(Money(1000, Currency.USD))
        await repo.add(loaded)

        got = await repo.find(wallet_id)
        assert got is not None
        assert got.owner_id == "owner-42"
        assert got.currency is Currency.USD
        assert got.balance == Money(1500, Currency.USD)

    # prove persistence: reconnect with a brand-new engine/session
    fresh_engine = create_async_engine(db_url)
    fresh_factory = async_sessionmaker(fresh_engine, expire_on_commit=False)
    try:
        async with fresh_factory() as fresh_session:
            fresh_repo = SqlAlchemyWalletRepository(session=fresh_session)
            persisted = await fresh_repo.find(wallet_id)
            assert persisted is not None, "wallet should survive a reconnect"
            assert persisted.balance == Money(1500, Currency.USD)
            assert persisted.owner_id == "owner-42"
            assert await fresh_repo.all_ids() == [wallet_id]
    finally:
        await fresh_engine.dispose()


@pytest.mark.asyncio
async def test_find_unknown_returns_none(
    sqlite_session: tuple[async_sessionmaker[AsyncSession], str],
) -> None:
    factory, _ = sqlite_session
    async with factory() as session:
        repo = SqlAlchemyWalletRepository(session=session)
        assert await repo.find("wlt-nope") is None


@pytest.mark.asyncio
async def test_remove_deletes_the_row(
    sqlite_session: tuple[async_sessionmaker[AsyncSession], str],
) -> None:
    factory, _ = sqlite_session
    async with factory() as session:
        repo = SqlAlchemyWalletRepository(session=session)
        wallet = Wallet.open(
            await repo.next_id(), owner_id="o", currency=Currency.EUR
        )
        await repo.add(wallet)
        assert await repo.find(wallet.id) is not None  # type: ignore[arg-type]

        await repo.remove(wallet)
        assert await repo.find(wallet.id) is None  # type: ignore[arg-type]
        assert await repo.all_ids() == []
:::

`test_full_flow_persists_through_sqlite_adapter` is the key test: it opens a
wallet, deposits, withdraws — each operation flushes through `repo.add` — then
creates a **new** engine from the same file URL and verifies that a fresh
repository instance reads back the expected balance. This two-engine pattern
proves that data is actually committed, not just cached in memory.

`Base.metadata.create_all` runs the full DDL (the same `CREATE TABLE`
statements that `alembic upgrade head` applies in production) so the schema is
always in sync with the SQLAlchemy models.

!!! spring "Spring parity"
    This test is the Python equivalent of `@DataJpaTest` with an embedded H2
    database in Spring Boot. `@DataJpaTest` loads only the JPA layer (entities,
    repositories, Flyway) and wires a fresh in-memory H2 for every test class.
    The `sqlite_session` fixture does the same: create the schema, run the test,
    dispose the engine. No Docker, no external process.

---

## Testing the event listener

The `WalletAuditListener` listens for domain events published by the command
handlers. Testing it end to end — command runs on the bus, handler publishes
events, listener receives them — requires all three components to share the
same `InMemoryEventBus`. The `conftest.py` fixtures already arrange this: both
`command_bus` and `audit_listener` accept an `event_bus` argument, and pytest
injects the same instance into both.

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

`test_listener_observes_wallet_events` is the core integration proof: three
commands produce three events, the listener's log records all three in order,
the payload fields match the aggregate's domain event dataclass fields, and the
`running_total` projection (deposit amount minus withdrawal amount) equals the
arithmetic result. No bus mock, no event capture list — the production listener
runs on the production bus.

`test_event_type_matches_domain_event_class_names` proves a domain invariant:
a rejected command (overdraw) raises no event. The audit log must never record
a side effect from a failed operation.

!!! tip "event_type is the class name"
    PyFly's event publisher sets `event_type` to the domain event class name:
    `"WalletOpened"`, `"FundsDeposited"`, `"FundsWithdrawn"`. The
    `@event_listener(pattern)` decorator on `WalletAuditListener.on_wallet_event`
    uses a glob pattern (`"Wallet*"`, `"Funds*"`) to subscribe to all three.
    The test asserts the string class names directly.

---

## Framework testing helpers

The tests above cover Lumen's full pyramid with nothing but standard pytest
primitives and PyFly's real production components. For larger applications or
teams that prefer more structure, `pyfly.testing` ships higher-level helpers
that mirror Spring Boot's testing annotations:

**`PyFlyTestCase` + `mock_bean(T)`** work like `@MockBean` in Spring Boot's
`@SpringBootTest`. Declare `repo = mock_bean(WalletDomainRepository)` on the
class body; `setup()` installs an `AsyncMock(spec=T)` for that type into the
application context and wires it into any collaborator that depends on it.

**`create_test_container(overrides={Interface: Implementation})`** builds a
dependency-injection container with fakes registered for specific interfaces.
Resolve the class under test from it and its dependencies are already injected.

**`assert_event_published(events, event_type, payload_contains=...)`** scans
a captured `EventEnvelope` list for the first envelope with the given type,
optionally checks payload keys, and returns the envelope for further assertions.
`assert_no_events_published(events)` fails if the list is non-empty.

**Testcontainers integration** (`postgres_container()`, `redis_container()`,
`pyfly_config(container, base={...})`) is PyFly's equivalent of Spring Boot's
`@Testcontainers` + `@ServiceConnection`. Start a real Postgres container;
`pyfly_config` rewrites the sync `psycopg2://` URL to `postgresql+asyncpg://`
and merges it into a `Config` ready to boot an `ApplicationContext`. Install
support with:

```bash
pip install 'pyfly[testcontainers]'
```

Guard every Testcontainers test with `@requires_docker` so it skips cleanly on
machines without Docker and runs on CI runners that do:

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

Lumen does not use these helpers — it has no need for them. SQLite covers the
persistence layer without Docker; the in-memory bus covers event routing. Use
the helpers when your project has infrastructure that cannot be reproduced
without a real daemon.

---

## What you built {.recap}

Lumen now has 23 passing tests across four files, exercising every layer of
the pyramid.

At the base, `test_money.py` and `test_wallet_aggregate.py` prove the domain
model's arithmetic, immutability, and invariant rules. All tests are
synchronous, pure Python functions — no fixtures, no DI, no `async`. The
`BusinessRuleViolation.rule` attribute makes each assertion specific to the
exact invariant that was violated.

In the middle, `conftest.py` wires the real components — `InMemoryWalletRepository`,
`InMemoryEventBus`, all five command and query handlers, `WalletAuditListener`
— into reusable async fixtures that pytest shares automatically across modules.
`test_cqrs_flow.py` dispatches commands and queries through the real bus and
checks every field of the query DTOs. `test_event_listener.py` proves that the
audit listener observes exactly the events produced by successful commands and
nothing from rejected ones.

At the peak, `test_sql_wallet_repository.py` exercises the SQLAlchemy adapter
against a temporary SQLite file, applying the full schema with
`Base.metadata.create_all`, then reconnecting with a fresh engine to prove true
persistence.

Concretely, you learned:

- **`asyncio_mode = "auto"` + `pythonpath = ["src"]`** in `pyproject.toml` —
  all async tests run without decorators; the `src/` layout is importable.
- **`uv run --extra dev pytest -q`** — the bare `uv sync` drops the dev group;
  always include `--extra dev` to get pytest.
- **`@pytest_asyncio.fixture`** — async fixture lifecycle managed by
  pytest-asyncio; plain `@pytest.fixture` does not handle async generators.
- **Shared fixture instances** — when two fixtures request the same fixture
  name (e.g., `event_bus`), pytest resolves it once per test and shares the
  instance, making `command_bus` and `audit_listener` write and read from the
  same bus.
- **`pending_events()` vs `clear_events()`** — `pending_events()` reads
  without draining; `clear_events()` drains. Always call `clear_events()` in
  arrange steps so assertions only see events from the act step.
- **`BusinessRuleViolation.rule`** — assert the exact rule string, not just
  the exception class, to prove that the right invariant fired.
- **SQLite + aiosqlite + `tmp_path`** — a real async relational test with no
  external infrastructure; the two-engine pattern proves true durability.
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
