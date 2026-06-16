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

A quick gloss before we start, since three terms recur in this chapter. A
**fixture** is a reusable piece of test setup — pytest builds it once, hands it to
your test, and tears it down afterward. A **conftest.py** is a special file pytest
discovers automatically; any fixtures you declare there become available to every
test in the package without an import. And **`pytest-asyncio` auto mode** simply
means you can write `async def test_...` and pytest will `await` it for you — no
per-test decorator required. Each of these appears again with a concrete example
below; this is just the map.

Install dev dependencies and run the suite:

```bash
uv run --extra dev pytest -q
```

The bare `uv sync` (without `--extra dev`) omits the dev group, so pytest is not
installed. Always include `--extra dev` when running tests.

**Run it.** From the `samples/lumen` directory, run the whole suite once now so you
have a baseline before changing anything:

```bash
uv run --extra dev pytest -q
```

You should see a row of dots — one per test — followed by a summary line:

```text
.........................................                                [100%]
41 passed in 0.28s
```

Forty-one passing tests, under a third of a second, no Docker and no external
process. That speed is the whole point of the pyramid: the fast base catches most
regressions before the slower integration layers ever run. If you instead see
`No module named pytest`, you forgot `--extra dev` — re-run with it.

---

## Unit-testing the domain

The domain model — `Money` and `Wallet` — has no framework dependencies. It never touches a database, a message bus, or an HTTP client. That purity makes it the ideal target for fast unit tests: construct objects, call methods, assert outcomes. No mocks, no fixtures, no `async`.

### Testing Money

`Money` is a frozen dataclass. Every operation either succeeds and returns a new `Money` instance, or raises `BusinessRuleViolation`. Each violation carries a `.rule` string that names the violated invariant — useful for asserting the exact rule in tests.

A quick gloss on two terms. A **value object** is an object defined entirely by its
values — two `Money(1050, EUR)` instances are equal because their fields are equal,
not because they are the same object in memory. A **frozen dataclass** is Python's
way of making such an object immutable: once constructed, you cannot reassign its
fields. Together they make `Money` safe to pass around freely — no caller can
mutate it behind your back, so it never needs defensive copying.

Let us build the test file one assertion-group at a time. Each step below maps to
one `def test_...` function in the listing that follows.

**Step 1 — equality is structural.** `test_value_equality_is_structural` asserts
that two `Money` values with the same amount and currency are equal, and that
differing in either field makes them unequal. This is the value-object contract.

**Step 2 — immutability is enforced.** `test_money_is_immutable` tries to assign to
`money.amount` and expects an exception. The frozen dataclass raises
`FrozenInstanceError`, proving you cannot mutate a value after construction.

**Step 3 — arithmetic returns new values.** `test_add_and_subtract_same_currency`
checks that `add` and `subtract` produce the expected `Money`, never mutating the
operands.

**Step 4 — the convenience surface.** `test_zero_factory_and_major_units` covers the
`Money.zero(currency)` factory, the `major_units` property, and the `__str__`
formatting.

**Step 5 — invariants reject bad input.** The last two tests assert that mixing
currencies and passing a non-integer amount each raise `BusinessRuleViolation` with
a specific `.rule` string — `"money-currency-mismatch"` and
`"money-amount-integer"`.

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

**Run it.** Run just this file to see the unit base of the pyramid in action:

```bash
uv run --extra dev pytest tests/test_money.py -q
```

Expected output:

```text
......                                                                   [100%]
6 passed in 0.02s
```

Six tests, twenty milliseconds. No database connected, no event bus started — these
tests construct a plain object and assert on its behaviour. That is what makes the
base of the pyramid so wide and so fast.

*What just happened.* You proved the entire `Money` contract — equality,
immutability, arithmetic, and every invariant — without touching a single piece of
framework infrastructure. Pure-domain code stays pure-domain testable. When one of
these fails, you know the bug is in the value object itself, not in wiring, a
session, or the bus.

!!! tip "Minor-unit arithmetic"
    `Money` stores amounts in **minor units** (integer cents). `Money(1050,
    Currency.EUR)` represents €10.50 — verified by `major_units == 10.5` and
    `str(...) == "10.50 EUR"`. The `Money.zero(currency)` factory returns a
    `Money(0, currency)`, useful for initialising wallet balances.

### Testing the Wallet aggregate

`Wallet` enforces several invariants: the owner must be a non-blank string, deposits must be positive, withdrawals must not overdraw, and amounts must match the wallet's currency. Each violation carries a `.rule` attribute for precise assertion.

One term first. An **aggregate** (or **aggregate root**) is a cluster of domain
objects treated as a single unit for consistency — here, the `Wallet` and its
balance. Every change goes through the aggregate's methods, so the aggregate is the
one place that guarantees its invariants hold. That makes it the natural unit to
test: drive it through its public methods and assert the rules never break.

The pattern every aggregate test follows is **arrange, act, assert**. Arrange:
construct the wallet into a known state. Act: call one method. Assert: check the
balance, the emitted event, or the raised violation. Watch for it in each test:

**Step 1 — opening emits an event.** `test_open_creates_empty_wallet` opens a wallet
and asserts the balance is zero and exactly one `WalletOpened` event is queued.

**Step 2 — opening validates its arguments.** `test_open_requires_owner` passes a
blank owner and expects `BusinessRuleViolation` with rule
`"wallet-owner-required"`.

**Step 3 — the happy path of deposit then withdraw.**
`test_deposit_then_withdraw_happy_path` deposits, asserts the balance and the
`FundsDeposited` event, then withdraws and asserts the balance and the
`FundsWithdrawn` event. Note the `clear_events()` call in the arrange step — more on
that just below the listing.

**Step 4 — invariants reject bad operations.** The final three tests prove a
withdrawal cannot overdraw, a deposit must be positive, and an amount must match the
wallet's currency. Each asserts the exact `.rule` string, and the overdraw test also
asserts the balance was left unchanged and no event was raised — proof the invariant
fired *before* any state changed.

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

**Run it.**

```bash
uv run --extra dev pytest tests/test_wallet_aggregate.py -q
```

Expected output:

```text
......                                                                   [100%]
6 passed in 0.02s
```

*What just happened.* The trickiest line to read is the assignment
`[event] = wallet.clear_events()`. That is a list unpacking: it asserts the returned
list has **exactly one** element and binds it to `event` in a single step. If the
aggregate had raised zero or two events, the unpacking itself would raise a
`ValueError` and the test would fail — so the shape of the event stream is checked
for free. This is why the happy-path test calls `clear_events()` right after opening:
it drains the `WalletOpened` event so the next unpacking sees only the
`FundsDeposited` event you are actually asserting on.

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

This file is the heart of the chapter, so we will read it top to bottom as a series
of small, layered steps. Each fixture builds on the one above it. The thing to keep
in mind: pytest links fixtures by **name**. When a fixture function declares a
parameter, pytest looks for a fixture with that name, builds it, and injects it. That
is how a small graph of independent fixtures composes into a full test stack.

**Step 1 — make the sample importable.** The first few lines push the sample's
`src/` onto `sys.path` so `import lumen...` resolves. The `pythonpath = ["src"]`
setting in `pyproject.toml` does the same thing for pytest's own collection; this
line covers direct imports inside `conftest.py` before pytest's path tweaks apply.

**Step 2 — `session_factory`: a database engine.** This async fixture creates an
in-memory SQLite engine, runs `Base.metadata.create_all` to build the schema, and
yields an `async_sessionmaker`. A **session factory** is a callable that hands out
fresh database sessions; the framework's relational auto-configuration creates one
exactly like this at startup. The single shared engine keeps the in-memory database
alive for the whole test — close it and the data vanishes.

**Step 3 — `repository`: the real Spring-Data-style repository.** It constructs the
framework `WalletRepository`, then calls
`RepositoryBeanPostProcessor().after_init(repo, "walletRepository")`. A
**post-processor** is a hook that runs against a freshly created bean — here it reads
method names like `find_by_owner_id` and compiles them into real queries. Skip this
call and those methods stay stubs that raise `NotImplementedError`.

**Step 4 — `event_bus`: the in-memory event bus.** A one-line fixture that yields an
`InMemoryEventBus` — the same publisher the application uses in non-Kafka
deployments.

**Step 5 — `audit_listener`: a subscriber on that bus.** It creates the
`WalletAuditListener` and subscribes its handler to the bus, reading the event
patterns straight off the decorated method's `__pyfly_event_patterns__` attribute —
exactly what the `ApplicationContext` does when it auto-wires listeners at startup.

**Step 6 — `command_bus` and `query_bus`: the CQRS dispatchers.** Each builds a
`HandlerRegistry`, registers the real handlers (passing them the `repository`,
`event_bus`, and `session_factory` they need), and yields a bus. **CQRS** —
Command/Query Responsibility Segregation — simply means writes go through a command
bus and reads through a query bus; a **handler** is the function that actually
processes one command or query.

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

*What just happened.* You wired a complete, production-shaped test stack — engine,
repository, event bus, listener, and both CQRS buses — entirely from fixtures, with
no mocks. The two facts that make it work are worth holding onto. First, **fixtures
compose by name**: `command_bus` asks for `repository`, `event_bus`, and
`session_factory` simply by naming them as parameters, and pytest threads the graph
together. Second, **a fixture requested by two others is built once per test**: both
`repository` and `command_bus` name `session_factory`, so they share one engine —
the write a command makes is the read a query sees. Get this file right and every
test in the next four sections is a two-line call.

!!! spring "Spring parity"
    `conftest.py` is PyFly's `@TestConfiguration` plus the shared
    `application-test.properties` of a Spring Boot project — a single place that
    declares the beans every test reuses. A `@pytest_asyncio.fixture` is the rough
    equivalent of a `@Bean` method in that config: pytest builds it lazily, injects
    it where its name is requested, and tears it down afterward, just as the Spring
    test context manages bean lifecycle and injection.

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

Notice how short each test body becomes now that the wiring lives in `conftest.py`.
A test declares the fixtures it needs as parameters, then reads as plain narrative:

**Step 1 — request the buses.** Each test signature lists `command_bus` and/or
`query_bus`. pytest sees those names, builds the fixture graph from `conftest.py`,
and injects the ready buses.

**Step 2 — send commands.** `await command_bus.send(OpenWallet(...))` returns the new
wallet id; subsequent `DepositFunds` and `WithdrawFunds` commands return the running
balance. You assert on each return value as you go.

**Step 3 — query the read side.** `await query_bus.query(GetWallet(...))` and
`GetBalance(...)` reload the persisted state and return DTOs (data-transfer objects —
plain read models). You assert their fields match what the commands wrote.

**Step 4 — prove the error paths.** The remaining tests send a command that must fail
— an overdraw, a non-positive deposit, an unknown wallet — wrapped in
`pytest.raises(CommandProcessingException)`. That context manager asserts the block
raises the named exception; if it does not, the test fails.

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

**Run it.**

```bash
uv run --extra dev pytest tests/test_cqrs_flow.py -q
```

Expected output:

```text
.....                                                                    [100%]
5 passed in 0.05s
```

*What just happened.* This is the first layer that touches real infrastructure, and
it still runs in milliseconds. The command went through the real bus, the real
handler opened a real `@transactional` unit of work on a real SQLite session,
committed it, and the query read it back — the exact path that runs in production,
minus the HTTP layer. Because the handler is registered in the fixture rather than
constructed in the test, you are testing the *dispatch* as well as the logic: if the
command-to-handler routing broke, these tests would catch it.

!!! note "asyncio_mode = \"auto\" and @pytest.mark.asyncio"
    With `asyncio_mode = "auto"` every async test is collected and run
    automatically. The `@pytest.mark.asyncio` decorator is **not required** but
    is harmless and makes the async intent explicit at a glance. Lumen keeps it
    for clarity.

---

## Testing the repository adapter

The CQRS flow tests prove the full open/deposit/withdraw/query pipeline. The repository adapter tests go one level deeper: they directly exercise the `WalletRepository` API — CRUD, **derived queries** compiled from method names, **`Pageable`/`Page`** pagination, and **`Specification`** predicates — against a temporary SQLite file database. No Docker, no external process, no network. pytest's built-in `tmp_path` fixture provides a temporary directory cleaned up automatically after each test.

The local fixture `_make_repo` mirrors what the `ApplicationContext` does at startup: construct the repository and run `RepositoryBeanPostProcessor.after_init` to compile the derived-query stubs. Without that call, methods like `find_by_owner_id` would raise `NotImplementedError`.

Two terms before the listing. A **derived query** is a repository method whose body
is generated from its *name*: `find_by_owner_id` becomes `WHERE owner_id = :value`,
no SQL written by hand. A **Specification** is a reusable, composable predicate
object you pass to a query — `balance_at_least(1000)` is one — for filters too
dynamic to bake into a method name. **Pagination** wraps both: `Pageable.of(page,
size, sort)` describes which slice you want, and the query returns a `Page` carrying
the items plus `total`, `total_pages`, and `has_next`.

This section uses a **file-based** SQLite database (via `tmp_path`) rather than the
in-memory one, so it can prove a stronger property — durability across a reconnect.
Here is the shape of each test:

**Step 1 — `sqlite_factory`: a temp-file engine.** The fixture builds a SQLite engine
backed by a real file under pytest's `tmp_path` (a fresh temp directory per test,
auto-deleted afterward), creates the schema, and yields both the factory and the URL.
Yielding the URL is what lets one test reconnect with a brand-new engine.

**Step 2 — CRUD and persistence.**
`test_upsert_inserts_then_updates_and_persists` upserts a row, upserts it again with
a new balance to prove update-in-place, commits, reads it back — then disposes the
engine and reconnects with a *fresh* one to prove the data truly hit disk.

**Step 3 — the unknown-id path.** `test_find_by_id_unknown_returns_none` confirms a
miss returns `None`, not an error.

**Step 4 — derived query.** `test_derived_find_by_owner_id` inserts wallets for two
owners and asserts `find_by_owner_id("alice")` returns only Alice's — proof the
post-processor compiled the method-name convention correctly.

**Step 5 — Specification + pagination.**
`test_specification_find_rich_paged_and_sorted` and
`test_find_all_pageable_counts_and_pages` exercise the `find_rich` /
`find_all_by_spec` predicate path and the `find_all(pageable)` paging path, asserting
`total`, `total_pages`, `has_next`, and the exact ordering of `page.items`.

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
async def test_find_all_pageable_counts_and_pages(
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

        page = await repo.find_all(
            Pageable.of(1, 2, Sort.by("created_at").descending())
        )
        assert page.total == 5
        assert page.total_pages == 3
        assert len(page.items) == 2
        # newest first -> wlt-4 (age 1 day), then wlt-3
        assert [w.id for w in page.items] == ["wlt-4", "wlt-3"]
:::

Four things to note. First, `_make_repo` calls `RepositoryBeanPostProcessor().after_init(repo, ...)` — without this, `find_by_owner_id` is still a stub and raises `NotImplementedError`. The post-processor compiles the method name into a SQLAlchemy `WHERE owner_id = :owner_id` clause. Second, `upsert` is the repository's insert-or-update; after each batch of upserts, `await session.commit()` flushes to SQLite. Third, `find_rich` takes a minimum balance and a `Pageable`; it delegates to `find_all_by_spec_paged(balance_at_least(min), pageable)`. Fourth, the two-engine pattern in `test_upsert_inserts_then_updates_and_persists` proves true durability: data committed through one engine is readable by a completely fresh engine and session.

**Run it.**

```bash
uv run --extra dev pytest tests/test_sql_wallet_repository.py -q
```

Expected output:

```text
.....                                                                    [100%]
5 passed in 0.06s
```

*What just happened.* The standout is the two-engine reconnect in the first test.
Many "persistence" tests pass even when nothing was written to disk, because the same
session caches the object in memory and hands it back on read. By disposing the
engine entirely and opening a *second* one against the same file URL, this test
forces a real round-trip to storage — if `upsert` or `commit` were silently not
persisting, the reconnect would return `None` and the test would fail. That is the
difference between testing your code and testing your cache.

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

An **event listener** is just a method that the framework subscribes to the bus so it
runs whenever a matching event is published. Lumen's `WalletAuditListener` keeps an
in-memory audit log and a running balance per wallet — a tiny **projection** (a read
model built by folding events). Testing it is the clearest demonstration of the
shared-fixture trick: because `command_bus` and `audit_listener` name the same
`event_bus`, an event a command publishes is an event the listener observes, with no
glue in the test body.

The tests follow one rhythm:

**Step 1 — drive commands.** Open a wallet, deposit, withdraw — all through
`command_bus`.

**Step 2 — read the projection.** Call `audit_listener.entries_for(wallet_id)` and
assert the recorded event types, in order, plus the `running_total`.

**Step 3 — assert the negative.** One test deliberately overdraws — a command that
must fail — and asserts the audit log records nothing from it. A failed operation
leaves no trace.

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

**Run it.**

```bash
uv run --extra dev pytest tests/test_event_listener.py -q
```

Expected output:

```text
...                                                                      [100%]
3 passed in 0.04s
```

*What just happened.* No part of the test connected the listener to the bus — the
`audit_listener` fixture did that in `conftest.py`, subscribing the handler to the
same `event_bus` the `command_bus` publishes through. So sending a command and then
reading `entries_for(...)` exercises the real publish/subscribe path end to end. The
negative test is the subtle one: it proves the audit log is driven by *events*, not
by *attempts* — a rejected withdrawal raises a `BusinessRuleViolation` before any
event is emitted, so nothing reaches the listener.

!!! tip "event_type is the class name"
    PyFly's event publisher sets `event_type` to the domain event class name:
    `"WalletOpened"`, `"FundsDeposited"`, `"FundsWithdrawn"`. The
    `@event_listener(event_types=["WalletOpened", "FundsDeposited", "FundsWithdrawn"])`
    decorator on `WalletAuditListener.on_wallet_event` names those three types
    explicitly; the framework stores them on the method as
    `__pyfly_event_patterns__`, which the `audit_listener` fixture reads to subscribe.
    The test asserts the string class names directly.

---

## Booted-context integration test

The unit tests, CQRS flow tests, and repository tests each wire one layer of the stack. The booted-context integration test wires everything at once: it starts the real `ApplicationContext` — DI component scan, CQRS auto-configuration, relational auto-configuration, `RepositoryBeanPostProcessor`, `@transactional` seam, EDA event bus — then resolves the `DefaultCommandBus` and `DefaultQueryBus` from the context and drives the full wallet lifecycle.

The **ApplicationContext** is PyFly's runtime container — the object that scans for
components, builds beans, runs post-processors, and holds the wired application
together. Booting it is the most faithful test you can write short of starting an
HTTP server: every piece of wiring the framework does at startup actually happens.

The database URL is overridden via an environment variable so the test never touches the developer's `lumen.db`. Here is the plan:

**Step 1 — isolate the database.** The `booted_context` fixture uses pytest's
`monkeypatch` fixture to set `PYFLY_DATA_RELATIONAL_URL` to a temp-file SQLite path
under `tmp_path`, *before* the app boots. `monkeypatch` is pytest's safe way to set
an environment variable for the duration of one test and automatically restore it
afterward — so this test can never clobber your real `lumen.db`.

**Step 2 — boot the real application.** It constructs `PyFlyApplication(LumenApplication,
config_path=...)` and `await app.startup()`. That single call runs the entire startup
sequence: component scan, all auto-configurations, the `RepositoryBeanPostProcessor`,
and the event bus. The fixture yields `app.context` and, in its `finally` block,
closes the shared session and calls `app.shutdown()`.

**Step 3 — resolve beans and drive the lifecycle.** The test calls
`ctx.get_bean(DefaultCommandBus)` and `ctx.get_bean(DefaultQueryBus)` — pulling the
*same* buses the application would use — then runs open → deposit → withdraw → list →
rich → balance, asserting each result.

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

    # --- paged list (find_all(pageable) + Page.map) ----------------------
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

`test_full_lifecycle_through_booted_context` exercises every query type the application exposes: `GetWallet` (aggregate reload), `ListWallets` (paginated list using `find_all(pageable)`), `ListRichWallets` (Specification predicate using `find_all_by_spec_paged`), and `GetBalance` (projection-backed balance). It proves that the `RepositoryBeanPostProcessor`, the `@transactional` boundary around each command handler, and the DI wiring all compose correctly in a single boot.

**Run it.**

```bash
uv run --extra dev pytest tests/test_app_context_integration.py -q
```

Expected output:

```text
.                                                                        [100%]
1 passed in 0.15s
```

One test, but the heaviest one in the suite: it actually started the framework. If
the DI scan missed a bean, an auto-configuration mis-wired, or the `@transactional`
boundary failed to commit, this is the test that catches it — which is exactly why it
sits at the peak of the pyramid and why there is only one of it.

*What just happened.* The environment-variable override is the load-bearing trick.
The framework reads `pyfly.data.relational.url` from config during relational
auto-configuration, and PyFly maps any config key to a `PYFLY_*` environment variable
(dots and dashes become underscores, uppercased), so `PYFLY_DATA_RELATIONAL_URL`
overrides the `url` in `pyfly.yaml`. Setting it with `monkeypatch` *before*
`app.startup()` is what redirects the whole booted application to a throwaway
database — and `monkeypatch` undoes the change when the fixture tears down, so no
other test is affected.

!!! tip "A dedicated test profile (v26.6.110)"
    Setting one variable is fine for a single override. When a project needs a whole
    block of test-only settings, PyFly's **profile** mechanism (Spring parity) is
    cleaner: drop a `pyfly-test.yaml` next to `pyfly.yaml` with your test overrides,
    then activate it by setting `PYFLY_PROFILES_ACTIVE=test` (or
    `pyfly.profiles.active: test` in config). On boot, PyFly overlays
    `pyfly-test.yaml` on top of the base `pyfly.yaml`, so values like the database
    URL or `ddl-auto` apply only under that profile. Lumen does not need a profile —
    one env override covers its single test-only setting — but reach for one as the
    test configuration grows.

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

**Run it.** You have now walked every layer file by file. Run the whole suite one more
time to confirm the full pyramid is green together:

```bash
uv run --extra dev pytest -q
```

Expected output — the same `41 passed` you started with, now with a mental model of
exactly what each dot proves:

```text
.........................................                                [100%]
41 passed in 0.28s
```

---

## What you built {.recap}

The six test files this chapter built add up to 26 passing tests, exercising every layer of the pyramid. Together with the saga tests from Chapter 12 and the event-sourcing tests from Chapter 9, Lumen's full suite is **41 passing tests** — the count you saw when you ran `uv run --extra dev pytest -q` at the start.

At the base, `test_money.py` and `test_wallet_aggregate.py` prove the domain model's arithmetic, immutability, and invariant rules. All tests are synchronous, pure Python functions — no fixtures, no DI, no `async`. The `BusinessRuleViolation.rule` attribute makes each assertion specific to the exact violated invariant.

In the middle, `conftest.py` wires the real components — the framework `WalletRepository` over an in-memory SQLite engine, `InMemoryEventBus`, all five command and query handlers (including `ListWalletsHandler` and `ListRichWalletsHandler`), and `WalletAuditListener` — into reusable async fixtures that pytest shares automatically across modules. The `RepositoryBeanPostProcessor` is applied to the repository fixture exactly as the `ApplicationContext` applies it at startup. `test_cqrs_flow.py` dispatches commands and queries through the real bus and checks every field of the query DTOs. `test_event_listener.py` proves that the audit listener observes exactly the events produced by successful commands and nothing from rejected ones.

`test_sql_wallet_repository.py` exercises the `WalletRepository` directly against a temporary SQLite file, covering the full CRUD surface, the derived query `find_by_owner_id` (compiled from the method name by `RepositoryBeanPostProcessor`), the `find_all(pageable)` API that returns a `Page` with total count and page metadata, and the `Specification` predicate path via `find_rich` / `find_all_by_spec`. The two-engine reconnect pattern proves true durability.

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
- **`Pageable.of(page, size, sort)` + `Page`** — the `find_all(pageable)`
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
- **`PYFLY_*` config overrides** — every config key maps to an environment
  variable (`pyfly.data.relational.url` → `PYFLY_DATA_RELATIONAL_URL`); set it
  with `monkeypatch.setenv` before booting to redirect the whole application.
- **Test profile (v26.6.110)** — for a block of test-only settings, add a
  `pyfly-test.yaml` overlay and activate it with `PYFLY_PROFILES_ACTIVE=test`
  (Spring `application-test.yaml` parity).
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
   `find_by_owner_id` for each owner, and then calls `find_all` with
   `Pageable.of(1, 3, Sort.by("balance_minor").descending())`. Assert that
   `page.total == 10`, `page.total_pages == 4`, and that the first item in
   `page.items` has the highest `balance_minor`. This proves that pagination
   is independent of the derived-query filter.
