<span class="eyebrow">Chapter 16</span>

# Testing PyFly Applications {.chtitle}

::: figure art/openers/ch16.svg | &nbsp;

The wallet works. Deposits land, balances update, events propagate through the
bus, and the saga coordinator rolls back cleanly on failure. What you do not yet
have is **confidence** that it will keep working after the next refactor. That
confidence comes from tests — tests that run in milliseconds, tests that prove
the domain model enforces its invariants, tests that verify the CQRS pipeline
dispatches correctly, and tests that start a real Postgres container and prove
the repository round-trips data exactly as expected.

PyFly treats testing as a first-class concern. The `pyfly.testing` module ships
a base test case class, a DI container factory for overrides, event assertion
helpers, test-slice decorators that mirror Spring Boot's `@WebMvcTest` and
`@DataJpaTest`, and Testcontainers wiring that boots real Docker-backed
infrastructure with one helper call. You do not build boilerplate; you write
behavior.

This chapter works through the full testing pyramid for Lumen.

::: figure art/figures/16-testing.svg | Figure 16.1 — PyFly's testing pyramid. Fast unit tests form the wide base; integration tests sit in the middle; Docker-backed Testcontainers tests crown the top and run only when Docker is available.

The pyramid has three levels. **Unit tests** sit at the base — many of them,
running in milliseconds, mocking every external dependency. **Integration tests**
occupy the middle tier — fewer, using in-memory adapters or `create_test_container`
with fakes to exercise multiple classes together. **Docker-backed tests** crown the
pyramid — a small number that spin up a real Postgres or Redis via Testcontainers
to prove the persistence and cache layers against real infrastructure. The higher
you climb, the slower, costlier, and fewer the tests.

| Level       | Dependencies          | Speed  | PyFly tools                                  |
|-------------|----------------------|--------|----------------------------------------------|
| Unit        | Mocked               | Fast   | `unittest.mock`, `mock_bean`                 |
| Integration | In-memory / fakes    | Medium | `create_test_container`, `PyFlyTestCase`     |
| Docker      | Real Postgres/Redis  | Slow   | `postgres_container`, `pyfly_config`         |

The project already uses pytest with `pytest-asyncio`. Enable auto-mode once in
`pyproject.toml` and every `async def test_*` function runs without a decorator:

```ini
[tool.pytest.ini_options]
asyncio_mode = "auto"
```

---

## Unit-testing the domain

The domain model — `Money` and `Wallet` — has no framework dependencies. It
never touches a database, a message bus, or an HTTP client. That makes it the
ideal subject for pure, fast unit tests: construct objects, call methods, assert
outcomes. No mocks required.

### Testing Money

`Money` is a frozen dataclass. Every operation either succeeds and returns a
new `Money` or raises `ValueError`. Tests are three-liners.

::: listing tests/domain/test_money.py | Listing 16.1 — Pure unit tests for the Money value object
import pytest
from lumen.domain.money import Money


class TestMoneyArithmetic:

    def test_add_same_currency(self):
        a = Money(amount=1000, currency="EUR")
        b = Money(amount=500, currency="EUR")
        result = a.add(b)
        assert result.amount == 1500
        assert result.currency == "EUR"

    def test_add_returns_new_instance(self):
        a = Money(amount=1000, currency="EUR")
        b = Money(amount=200, currency="EUR")
        result = a.add(b)
        assert result is not a  # immutable: always a fresh object

    def test_subtract_same_currency(self):
        a = Money(amount=1000, currency="EUR")
        b = Money(amount=300, currency="EUR")
        result = a.subtract(b)
        assert result.amount == 700

    def test_add_currency_mismatch_raises(self):
        a = Money(amount=1000, currency="EUR")
        b = Money(amount=500, currency="USD")
        with pytest.raises(ValueError, match="Cannot add EUR and USD"):
            a.add(b)

    def test_subtract_currency_mismatch_raises(self):
        a = Money(amount=1000, currency="EUR")
        b = Money(amount=200, currency="USD")
        with pytest.raises(ValueError, match="Cannot subtract USD from EUR"):
            a.subtract(b)

    def test_is_negative(self):
        assert Money(amount=-1, currency="EUR").is_negative() is True
        assert Money(amount=0, currency="EUR").is_negative() is False
        assert Money(amount=1, currency="EUR").is_negative() is False

    def test_is_zero(self):
        assert Money(amount=0, currency="EUR").is_zero() is True
        assert Money(amount=1, currency="EUR").is_zero() is False

    def test_str_formatting(self):
        m = Money(amount=10050, currency="EUR")
        assert str(m) == "100.50 EUR"
:::

Every test is synchronous — no `async`, no `await`, no DI container. The test
class is a plain Python class with no base class; pytest collects it automatically.

!!! tip "Minor-unit arithmetic"
    `Money` stores amounts in minor units (integer cents). The tests above use
    raw integers deliberately. When asserting formatted output with `str()`,
    remember `10050` renders as `"100.50 EUR"`, not `"10050.00 EUR"`. Keep that
    mental model consistent with Chapter 6.

### Testing the Wallet aggregate

`Wallet` enforces three invariants: deposits must be positive, withdrawals must
not overdraft, and amounts must match the wallet's currency. These rules live in
`Wallet.deposit` and `Wallet.withdraw`. Unit tests drive the wallet directly —
no bus, no repository, no persistence.

::: listing tests/domain/test_wallet.py | Listing 16.2 — Unit tests for the Wallet aggregate root
import pytest
from pyfly.domain import BusinessRuleViolation

from lumen.domain.money import Money
from lumen.domain.wallet import (
    FundsDeposited,
    FundsWithdrawn,
    Wallet,
    WalletOpened,
)


class TestWalletOpen:

    def test_open_creates_zero_balance(self):
        wallet = Wallet.open(owner_id="user-1", currency="EUR")
        assert wallet.balance.amount == 0
        assert wallet.balance.currency == "EUR"

    def test_open_queues_wallet_opened_event(self):
        wallet = Wallet.open(owner_id="user-1", currency="EUR")
        events = wallet.clear_events()
        assert len(events) == 1
        assert isinstance(events[0], WalletOpened)
        assert events[0].owner_id == "user-1"
        assert events[0].currency == "EUR"

    def test_open_generates_non_empty_id(self):
        wallet = Wallet.open(owner_id="user-1", currency="EUR")
        assert wallet.id and len(wallet.id) > 0


class TestWalletDeposit:

    def setup_method(self):
        self.wallet = Wallet.open(owner_id="user-1", currency="EUR")
        self.wallet.clear_events()  # discard the WalletOpened event

    def test_deposit_increases_balance(self):
        self.wallet.deposit(Money(amount=5000, currency="EUR"))
        assert self.wallet.balance.amount == 5000

    def test_deposit_queues_funds_deposited_event(self):
        self.wallet.deposit(Money(amount=5000, currency="EUR"))
        events = self.wallet.clear_events()
        assert len(events) == 1
        assert isinstance(events[0], FundsDeposited)
        assert events[0].amount == 5000
        assert events[0].new_balance == 5000

    def test_deposit_zero_raises(self):
        with pytest.raises(BusinessRuleViolation, match="greater than zero"):
            self.wallet.deposit(Money(amount=0, currency="EUR"))

    def test_deposit_negative_raises(self):
        with pytest.raises(BusinessRuleViolation):
            self.wallet.deposit(Money(amount=-100, currency="EUR"))

    def test_deposit_currency_mismatch_raises(self):
        with pytest.raises(BusinessRuleViolation, match="currency"):
            self.wallet.deposit(Money(amount=1000, currency="USD"))

    def test_clear_events_drains_buffer(self):
        self.wallet.deposit(Money(amount=1000, currency="EUR"))
        first = self.wallet.clear_events()
        second = self.wallet.clear_events()
        assert len(first) == 1
        assert len(second) == 0  # drained on first call


class TestWalletWithdraw:

    def setup_method(self):
        self.wallet = Wallet.open(owner_id="user-1", currency="EUR")
        self.wallet.deposit(Money(amount=10000, currency="EUR"))
        self.wallet.clear_events()

    def test_withdraw_decreases_balance(self):
        self.wallet.withdraw(Money(amount=3000, currency="EUR"))
        assert self.wallet.balance.amount == 7000

    def test_withdraw_queues_funds_withdrawn_event(self):
        self.wallet.withdraw(Money(amount=3000, currency="EUR"))
        events = self.wallet.clear_events()
        assert len(events) == 1
        assert isinstance(events[0], FundsWithdrawn)
        assert events[0].amount == 3000
        assert events[0].new_balance == 7000

    def test_overdraft_raises(self):
        with pytest.raises(BusinessRuleViolation, match="overdraft"):
            self.wallet.withdraw(Money(amount=20000, currency="EUR"))

    def test_withdraw_currency_mismatch_raises(self):
        with pytest.raises(BusinessRuleViolation, match="currency"):
            self.wallet.withdraw(Money(amount=1000, currency="USD"))
:::

Notice how clean the tests are: create a wallet, call a method, assert the
balance or the event buffer. `setup_method` is pytest's equivalent of `setUp`
in `unittest` — it runs before every test method in the class. The
`self.wallet.clear_events()` call in `setup_method` of `TestWalletWithdraw`
is deliberate: it drains the events queued by `Wallet.open` and `deposit`
before each withdrawal test, so the event-assertion tests only see events from
the operation under test.

!!! spring "Spring parity"
    Testing a DDD aggregate in isolation is the same discipline in any stack. In
    Spring / jMolecules you would test the aggregate by calling its methods
    directly and checking `aggregate.domainEvents()` (provided by
    `AbstractAggregateRoot`) before calling `afterDomainEventPublication()`.
    PyFly's `clear_events()` plays the same role as `@AfterDomainEventPublication`
    — drain the buffer, assert what was there, and move on.

---

## Testing services and CQRS handlers

Service-layer tests need more infrastructure than domain tests: the service
depends on a repository, the CQRS handler depends on a command bus, and
the event publisher needs somewhere to send events. PyFly provides two
complementary tools: `create_test_container` for lightweight DI wiring with
fake implementations, and `PyFlyTestCase` for tests that need the full
`ApplicationContext` lifecycle.

### Wiring fakes with create_test_container

`create_test_container` builds a `Container` pre-configured with your
`overrides` mapping. For each `(interface, impl)` pair it registers the
implementation as a `SINGLETON` and binds the interface to it, so
`container.resolve(interface)` returns the test double. You then register
the class under test and resolve it — its dependencies arrive already injected.

::: listing tests/services/test_wallet_service.py | Listing 16.3 — WalletApplicationService with a fake repository injected via create_test_container
import pytest
from pyfly.container import Scope
from pyfly.domain import BusinessRuleViolation
from pyfly.testing import create_test_container

from lumen.domain.money import Money
from lumen.domain.wallet import Wallet
from lumen.domain.wallet_repository import WalletDomainRepository
from lumen.services.wallet_application_service import WalletApplicationService


class FakeWalletRepository:
    """In-memory repository. No DB, no migrations, no network."""

    def __init__(self):
        self._store: dict[str, Wallet] = {}

    async def save(self, wallet: Wallet) -> None:
        self._store[wallet.id] = wallet

    async def find_by_id(self, wallet_id: str) -> Wallet | None:
        return self._store.get(wallet_id)


class TestWalletApplicationService:

    @pytest.fixture
    def container(self):
        c = create_test_container(
            overrides={WalletDomainRepository: FakeWalletRepository}
        )
        c.register(WalletApplicationService, scope=Scope.SINGLETON)
        return c

    @pytest.fixture
    def service(self, container) -> WalletApplicationService:
        return container.resolve(WalletApplicationService)

    @pytest.fixture
    def repo(self, container) -> FakeWalletRepository:
        return container.resolve(WalletDomainRepository)

    async def test_open_wallet_persists(self, service, repo):
        wallet_id = await service.open_wallet(
            owner_id="user-42", currency="EUR"
        )
        assert wallet_id
        assert wallet_id in {w.id for w in repo._store.values()}

    async def test_deposit_increases_stored_balance(self, service, repo):
        wallet_id = await service.open_wallet(
            owner_id="user-42", currency="EUR"
        )
        await service.deposit(
            wallet_id=wallet_id,
            amount=Money(amount=5000, currency="EUR"),
        )
        wallet = await repo.find_by_id(wallet_id)
        assert wallet is not None
        assert wallet.balance.amount == 5000

    async def test_overdraft_does_not_persist(self, service, repo):
        wallet_id = await service.open_wallet(
            owner_id="user-42", currency="EUR"
        )
        with pytest.raises(BusinessRuleViolation):
            await service.deposit(
                wallet_id=wallet_id,
                amount=Money(amount=-1000, currency="EUR"),
            )
        wallet = await repo.find_by_id(wallet_id)
        assert wallet is not None
        assert wallet.balance.amount == 0  # unchanged
:::

The fixture chain is clean: `container` builds the wired graph once; `service`
and `repo` resolve from it. Because both resolve from the same container
singleton, the `repo` fixture gives you a direct reference to the *same* fake
that the service writes to — so you can assert internal state without reaching
through HTTP.

!!! tip "Singleton scope matters here"
    `create_test_container` registers overrides with `Scope.SINGLETON`. That
    means `container.resolve(WalletDomainRepository)` always returns the same
    `FakeWalletRepository` instance — the same one injected into
    `WalletApplicationService`. If you used `Scope.TRANSIENT` you would get a
    fresh empty fake every time, and cross-fixture assertions would fail.

### Testing CQRS handlers with mock_bean

CQRS command handlers are the most focused objects in the service layer: each
handler implements one operation and depends on one (or a few) domain
repositories. `PyFlyTestCase` + `mock_bean` give you a test class where every
`mock_bean(...)` attribute is automatically wired into the test context — the
same `AsyncMock` the service layer resolves.

::: listing tests/cqrs/test_deposit_funds_handler.py | Listing 16.4 — DepositFundsHandler tested with mock_bean — the mock is wired into the ApplicationContext
import pytest
from unittest.mock import AsyncMock
from pyfly.testing import PyFlyTestCase, mock_bean

from lumen.cqrs.commands import DepositFunds
from lumen.cqrs.handlers.deposit_funds_handler import DepositFundsHandler
from lumen.domain.money import Money
from lumen.domain.wallet import Wallet
from lumen.domain.wallet_repository import WalletDomainRepository


class TestDepositFundsHandler(PyFlyTestCase):
    repo = mock_bean(WalletDomainRepository)

    async def test_deposit_calls_save(self):
        await self.setup()

        # Arrange: the repo returns a real Wallet aggregate
        wallet = Wallet.open(owner_id="user-1", currency="EUR")
        self.repo.find_by_id.return_value = wallet
        self.repo.save.return_value = None

        handler = DepositFundsHandler(wallet_repository=self.repo)
        cmd = DepositFunds(
            wallet_id=wallet.id,
            amount_cents=3000,
            currency="EUR",
        )

        # Act
        await handler.do_handle(cmd)

        # Assert: wallet was saved after deposit
        self.repo.save.assert_called_once_with(wallet)
        assert wallet.balance.amount == 3000

        await self.teardown()

    async def test_mock_wired_into_context(self):
        """The context resolves the same mock that mock_bean provides."""
        await self.setup()

        resolved = self.context.get_bean(WalletDomainRepository)
        assert resolved is self.repo

        await self.teardown()
:::

**How `mock_bean` works.** `mock_bean(WalletDomainRepository)` returns a
`MockBeanDescriptor` — a Python descriptor that lazily creates an
`AsyncMock(spec=WalletDomainRepository)` per test instance. Accessing
`self.repo` the first time materializes the mock. When you call `setup()`,
`_install_mock_beans` walks the class MRO, finds every descriptor, and
installs the materialized mock into the context's container keyed on
`WalletDomainRepository`. From that point on, `self.context.get_bean(
WalletDomainRepository)` and any DI-resolved collaborator that depends on
`WalletDomainRepository` both receive the same `AsyncMock` you configured
in the test.

!!! spring "Spring parity"
    `mock_bean(T)` is PyFly's counterpart of Spring's `@MockBean` annotation.
    Like `@MockBean`, it declares that a particular bean type should be replaced
    by a mock in the test context, and that mock is injected into any
    collaborator that depends on the type. The per-instance fresh-mock behavior
    mirrors Spring's `@MockBean` reset that Mockito performs between tests with
    `MockitoAnnotations.initMocks(this)`.

### Using test slices

Test slices let you declare the *intent* of a test class — web layer, service
layer, or data layer — without wiring the full application stack. The three
decorators `@WebTest`, `@ServiceTest`, and `@DataTest` mark the class; the
functional helpers `web_slice`, `service_slice`, and `data_slice` build a
minimal, started `ApplicationContext` containing only the beans you specify.

::: listing tests/slices/test_wallet_service_slice.py | Listing 16.5 — ServiceTest slice: a focused context with only the service and its fake collaborator
from pyfly.testing import ServiceTest, service_slice

from lumen.domain.wallet_repository import WalletDomainRepository
from lumen.services.wallet_application_service import WalletApplicationService


class FakeWalletRepository:
    def __init__(self):
        self._store: dict = {}

    async def save(self, wallet) -> None:
        self._store[wallet.id] = wallet

    async def find_by_id(self, wallet_id: str):
        return self._store.get(wallet_id)


@ServiceTest
class TestWalletServiceSlice:
    """Only WalletApplicationService + FakeWalletRepository are in the context."""

    async def test_open_wallet(self):
        async with await service_slice(
            WalletApplicationService,
            overrides={WalletDomainRepository: FakeWalletRepository},
        ) as ctx:
            service = ctx.get_bean(WalletApplicationService)
            wallet_id = await service.open_wallet(
                owner_id="user-99", currency="EUR"
            )
            assert wallet_id
:::

`service_slice` is an alias for `slice_context`. It registers only the beans
you pass (and overrides), starts the context, fails fast if a collaborator is
missing, and stops the context when the `async with` block exits. The
`@ServiceTest` decorator is a marker — it sets `__pyfly_test_slice__ = "service"`
on the class, which tooling and reporting can use to filter by layer.

| Slice helper    | Yields                   | Use for                              |
|----------------|--------------------------|--------------------------------------|
| `web_slice`    | `(context, client)` pair | Controllers + `PyFlyTestClient`      |
| `service_slice` | `context`               | Services and business logic          |
| `data_slice`   | `context`                | Repositories and queries             |

---

## Asserting events

Domain events are the proof that the aggregate changed state. After calling a
service method or a command handler, you need to verify that the right event
was published. PyFly provides two assertion helpers that work against a list
of `EventEnvelope` objects captured from an `InMemoryEventBus`.

### Capturing events with InMemoryEventBus

`PyFlyTestCase` gives you `self.event_bus` — an `InMemoryEventBus` ready to
use. Subscribe a capture list to the bus, trigger the operation, then assert:

::: listing tests/events/test_wallet_events.py | Listing 16.6 — Subscribing to the InMemoryEventBus and asserting published events
import pytest
from pyfly.eda.types import EventEnvelope
from pyfly.testing import (
    PyFlyTestCase,
    assert_event_published,
    assert_no_events_published,
)


class TestWalletEvents(PyFlyTestCase):

    async def test_deposit_publishes_funds_deposited(self):
        await self.setup()

        captured: list[EventEnvelope] = []

        async def capture(envelope: EventEnvelope) -> None:
            captured.append(envelope)

        # Wildcard pattern: matches "wallet.deposited", "wallet.opened", etc.
        self.event_bus.subscribe("wallet.*", capture)

        await self.event_bus.publish(
            destination="wallets",
            event_type="wallet.deposited",
            payload={
                "wallet_id": "w-001",
                "amount": 5000,
                "currency": "EUR",
                "new_balance": 5000,
            },
        )

        event = assert_event_published(
            captured,
            "wallet.deposited",
            payload_contains={"wallet_id": "w-001", "amount": 5000},
        )
        assert event.payload["new_balance"] == 5000
        assert event.destination == "wallets"

        await self.teardown()

    async def test_failed_deposit_publishes_no_events(self):
        await self.setup()

        captured: list[EventEnvelope] = []

        async def capture(envelope: EventEnvelope) -> None:
            captured.append(envelope)

        self.event_bus.subscribe("wallet.*", capture)

        # No publish call was made — validation rejected the operation
        assert_no_events_published(captured)

        await self.teardown()

    async def test_wildcard_pattern_matches_multiple_types(self):
        await self.setup()

        captured: list[EventEnvelope] = []

        async def capture(envelope: EventEnvelope) -> None:
            captured.append(envelope)

        self.event_bus.subscribe("wallet.*", capture)

        await self.event_bus.publish(
            "wallets", "wallet.opened",
            {"wallet_id": "w-001"},
        )
        await self.event_bus.publish(
            "wallets", "wallet.deposited",
            {"wallet_id": "w-001"},
        )
        await self.event_bus.publish(
            "accounts", "account.created",
            {"account_id": "a-001"},
        )

        # account.created does not match "wallet.*"
        assert len(captured) == 2
        assert_event_published(captured, "wallet.opened")
        assert_event_published(captured, "wallet.deposited")

        await self.teardown()
:::

### assert_event_published in depth

`assert_event_published(events, event_type, payload_contains=None)` scans the
list for the first envelope whose `event_type` matches, then optionally checks
that the payload contains every key-value pair in `payload_contains`. It
returns the matching `EventEnvelope` so you can make additional assertions on
it.

```
AssertionError: Expected event 'wallet.deposited' to be published.
Published events: ['wallet.opened']
```

If the event is found but a payload value does not match:

```
AssertionError: Expected payload['amount'] == 5000, got 3000
```

`assert_no_events_published(events)` fails with a list of all published types
if the list is non-empty — the right assertion after a rejected operation that
must not produce side effects.

!!! note "First match wins"
    When multiple envelopes share the same `event_type`,
    `assert_event_published` returns the **first** one. If you need to verify
    all occurrences, iterate `[e for e in events if e.event_type == t]`
    directly.

---

## Integration tests with Testcontainers

In-memory fakes are fast but not faithful. A fake repository never surfaces
a missing index, a constraint violation, or a connection-pool exhaustion. To
prove those paths you need real infrastructure — a real Postgres, a real Redis.
PyFly's Testcontainers helpers spin up Docker-backed containers for the
duration of a test and wire their connection details straight into a PyFly
`Config` with one call.

!!! spring "Spring parity"
    PyFly's Testcontainers integration mirrors Spring Boot's `@Testcontainers`
    annotation combined with `@ServiceConnection`. In Spring you declare a
    `@Container static PostgreSQLContainer<?> postgres = new
    PostgreSQLContainer<>("postgres:16-alpine")` and annotate it with
    `@ServiceConnection`; Spring autoconfigures the datasource URL from the
    running container. PyFly's `pyfly_config(postgres_container())` does the
    same: start the container, call the helper, get a `Config` whose
    `pyfly.data.relational.url` is the `postgresql+asyncpg://` URL pointing at
    that container.

### Installing Testcontainers support

```bash
pip install 'pyfly[testcontainers]'
```

This pulls in `testcontainers>=4.0.0` and the individual submodules
(`testcontainers.postgres`, `testcontainers.redis`, and so on). A running
Docker daemon is also required. If Docker is not available on the machine, the
`@requires_docker` decorator skips the test rather than failing it — so the
suite stays green on a machine that has no Docker installed.

### Container factories

Each factory returns an unstarted container. Start it with a `with` block; it
is stopped automatically on exit.

| Factory | Default image |
|---------|--------------|
| `postgres_container(image="postgres:16-alpine")` | `postgres:16-alpine` |
| `mysql_container(image="mysql:8")` | `mysql:8` |
| `redis_container(image="redis:7-alpine")` | `redis:7-alpine` |
| `mongodb_container(image="mongo:7")` | `mongo:7` |
| `kafka_container(image="confluentinc/cp-kafka:7.6.0")` | `confluentinc/cp-kafka:7.6.0` |

### Wiring containers into PyFly config

`pyfly_config_for(container)` maps a started container to a flat dict of
dotted config keys. `pyfly_config(*containers, base=None)` merges all of them
(plus an optional `base` dict) into a nested `Config` ready to pass to
`ApplicationContext`.

| Container | Config keys produced |
|-----------|---------------------|
| Postgres | `pyfly.data.relational.url` — rewritten to `postgresql+asyncpg://` |
| Redis | `pyfly.cache.redis.url` and `pyfly.session.redis.url` |
| Kafka | `pyfly.eda.kafka.bootstrap-servers` |

The Postgres driver URL is deliberately rewritten from the sync
`psycopg2://` scheme to the async `asyncpg://` scheme — the reactive data
layer requires it and the rewrite happens automatically.

### A real Postgres repository test

::: listing tests/integration/test_wallet_repository_postgres.py | Listing 16.7 — Integration test against a real Postgres container via Testcontainers
from pyfly.context import ApplicationContext
from pyfly.testing import (
    postgres_container,
    pyfly_config,
    requires_docker,
)

from lumen.domain.money import Money
from lumen.domain.wallet import Wallet


@requires_docker
async def test_wallet_round_trip_against_real_postgres():
    with postgres_container() as pg:
        # Wire the container URL into PyFly config automatically.
        config = pyfly_config(
            pg,
            base={
                "pyfly.data.enabled": True,
            },
        )

        assert config.get("pyfly.data.relational.url").startswith(
            "postgresql+asyncpg://"
        )

        context = ApplicationContext(config)
        await context.start()
        try:
            # Resolve the real repository (backed by the running Postgres).
            from lumen.domain.wallet_repository import WalletDomainRepository
            repo = context.get_bean(WalletDomainRepository)

            # Save a wallet.
            wallet = Wallet.open(owner_id="user-1", currency="EUR")
            wallet.deposit(Money(amount=7500, currency="EUR"))
            wallet.clear_events()
            await repo.save(wallet)

            # Retrieve it — proves the round-trip.
            loaded = await repo.find_by_id(wallet.id)
            assert loaded is not None
            assert loaded.balance.amount == 7500
            assert loaded.balance.currency == "EUR"
        finally:
            await context.stop()
:::

The `@requires_docker` decorator attaches a `pytest.mark.skipif` that evaluates
`is_docker_available()` at collection time. On a machine without Docker the test
is skipped, not failed. On CI pipelines that do have Docker it runs against a
fresh Postgres instance every time.

### A real Redis + Postgres test

When Lumen caches balance queries in Redis, you want to prove that a fresh
context hydrates the cache from the database. Here is how you start both
containers and merge their config in one call:

::: listing tests/integration/test_wallet_cache_integration.py | Listing 16.8 — Combining Postgres and Redis containers with pyfly_config
from pyfly.context import ApplicationContext
from pyfly.testing import (
    postgres_container,
    redis_container,
    pyfly_config,
    requires_docker,
)


@requires_docker
async def test_balance_cache_backed_by_real_redis():
    with postgres_container() as pg, redis_container() as redis:
        config = pyfly_config(
            pg,
            redis,
            base={
                "pyfly.data.enabled": True,
                "pyfly.cache.enabled": True,
                "pyfly.cache.provider": "redis",
            },
        )

        # Both URLs are present and correct.
        assert config.get("pyfly.data.relational.url").startswith(
            "postgresql+asyncpg://"
        )
        assert config.get("pyfly.cache.redis.url").startswith("redis://")

        context = ApplicationContext(config)
        await context.start()
        try:
            # ... resolve service, exercise cache-hit path, assert DB not called
            ...
        finally:
            await context.stop()
:::

`pyfly_config` handles the driver-URL rewriting and nesting automatically.
You never copy-paste a `JDBC_URL` or fiddle with `get_exposed_port` manually —
the `@ServiceConnection`-style mapping does it for you.

!!! tip "Skipping gracefully without Docker"
    Guard every Testcontainers test with `@requires_docker`. On a developer
    machine without Docker the tests skip cleanly; on a CI runner that spins
    up a Docker daemon (GitHub Actions `ubuntu-latest` does by default) they
    run. Do not use `@pytest.mark.skip` — that would permanently silence the
    test even on machines that do have Docker.

---

## Testing the web layer

The web layer test verifies that an HTTP endpoint returns the right status code,
the right JSON shape, and the right headers — without touching the database.
PyFly gives you two options: `PyFlyTestClient` wrapping a Starlette application
for synchronous-style assertions, and `web_slice` for a fully wired context
that mimics production routing.

### PyFlyTestClient and fluent assertions

`PyFlyTestClient` wraps Starlette's `TestClient` (which runs an in-process ASGI
server) with fluent `TestResponse` assertion methods. All assert methods return
`self` so you can chain them.

::: listing tests/web/test_wallet_controller_client.py | Listing 16.9 — WalletController tested with PyFlyTestClient and fluent assertions
import pytest
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.responses import JSONResponse

from pyfly.testing import PyFlyTestClient


def get_wallet(request):
    wallet_id = request.path_params["wallet_id"]
    if wallet_id == "missing":
        return JSONResponse(
            {"error": {"code": "WALLET_NOT_FOUND"}}, status_code=404
        )
    return JSONResponse(
        {
            "wallet_id": wallet_id,
            "owner_id": "user-1",
            "balance_cents": 5000,
            "currency": "EUR",
        }
    )


def deposit(request):
    return JSONResponse({"status": "ok"})


app = Starlette(
    routes=[
        Route("/wallets/{wallet_id}", get_wallet, methods=["GET"]),
        Route("/wallets/{wallet_id}/deposit", deposit, methods=["PATCH"]),
    ]
)


class TestWalletControllerClient:

    @pytest.fixture(autouse=True)
    def client(self):
        self.client = PyFlyTestClient(app)

    def test_get_wallet_returns_200(self):
        self.client.get("/wallets/w-001") \
            .assert_status(200) \
            .assert_json_path("$.wallet_id", value="w-001") \
            .assert_json_path("$.balance_cents", value=5000) \
            .assert_json_path("$.currency", value="EUR")

    def test_get_wallet_not_found_returns_404(self):
        self.client.get("/wallets/missing") \
            .assert_status(404) \
            .assert_json_path("$.error.code", value="WALLET_NOT_FOUND")

    def test_deposit_returns_200(self):
        self.client.patch(
            "/wallets/w-001/deposit",
            json={"amount_cents": 3000, "currency": "EUR"},
        ).assert_status(200)

    def test_response_body_inspection(self):
        response = self.client.get("/wallets/w-001")
        data = response.json()
        assert data["currency"] == "EUR"
        assert data["balance_cents"] == 5000
:::

`TestResponse` exposes four assertion methods:

| Method | Signature | Description |
|--------|-----------|-------------|
| `assert_status` | `(expected: int)` | Status code must match |
| `assert_json_path` | `(path, *, value=..., exists=True)` | JSONPath must exist / match |
| `assert_header` | `(name, *, value=None, exists=True)` | Header must exist / match |
| `assert_body_contains` | `(text: str)` | Body must contain substring |

`assert_json_path` uses `jsonpath-ng` syntax. `$.wallet_id` selects the top-
level key; `$[0].name` selects the `name` field of the first array element;
`$.error.code` drills into nested objects. Set `exists=False` to assert that a
path is absent — useful for verifying a `deleted_at` field is not leaked.

### Wiring a real controller with web_slice

`web_slice` builds a minimal `ApplicationContext` containing the controllers
you pass, wraps it with `create_app(context=...)` so routing, filters, and
error handlers are wired exactly as in production, and returns a
`PyFlyTestClient` alongside the context:

::: listing tests/web/test_wallet_controller_slice.py | Listing 16.10 — WalletController tested with web_slice: a production-like context with a faked service
from pyfly.testing import WebTest, web_slice, mock_bean

from lumen.controllers.wallet_controller import WalletController
from lumen.services.wallet_application_service import WalletApplicationService


class FakeWalletService:
    async def open_wallet(self, owner_id: str, currency: str) -> str:
        return "w-fake-001"

    async def get_wallet(self, wallet_id: str):
        if wallet_id == "missing":
            return None
        return {
            "wallet_id": wallet_id,
            "owner_id": "user-1",
            "balance_cents": 9900,
            "currency": "EUR",
        }


@WebTest
class TestWalletControllerSlice:

    async def test_open_wallet_returns_201(self):
        async with await web_slice(
            WalletController,
            overrides={WalletApplicationService: FakeWalletService()},
        ) as (ctx, client):
            client.post(
                "/wallets",
                json={"owner_id": "user-1", "currency": "EUR"},
            ).assert_status(201) \
             .assert_json_path("$.wallet_id", value="w-fake-001")

    async def test_get_wallet_returns_200(self):
        async with await web_slice(
            WalletController,
            overrides={WalletApplicationService: FakeWalletService()},
        ) as (_ctx, client):
            client.get("/wallets/w-001") \
                .assert_status(200) \
                .assert_json_path("$.balance_cents", value=9900)

    async def test_get_missing_wallet_returns_404(self):
        async with await web_slice(
            WalletController,
            overrides={WalletApplicationService: FakeWalletService()},
        ) as (_ctx, client):
            client.get("/wallets/missing").assert_status(404)
:::

`web_slice` accepts either a **class** or a **pre-built instance** as an
override value. Passing `FakeWalletService()` (an instance) installs it
directly under the `WalletApplicationService` key in the container — no
registration step needed. Passing `FakeWalletService` (the class) would
register and bind it as a singleton instead.

!!! spring "Spring parity"
    `@WebTest` + `web_slice` mirror `@WebMvcTest` in Spring Boot. `@WebMvcTest`
    loads only the web layer — controllers, filters, `@ControllerAdvice` — and
    replaces the service layer with mocks via `@MockBean`. `web_slice` does the
    same: only the controllers you name are in the context; the service is
    replaced by whatever you put in `overrides`. The `PyFlyTestClient` plays the
    role of `MockMvc` — the synchronous-style ASGI driver that runs the full
    request pipeline in-process.

---

## What you built {.recap}

The Lumen project now has a complete test pyramid.

At the base, `TestMoneyArithmetic` and `TestWalletWithdraw` prove the domain
model's arithmetic and invariant rules without any framework overhead — fast,
synchronous, pure Python. `setup_method` and `clear_events()` keep each test's
event buffer clean.

In the middle tier, `create_test_container` with `FakeWalletRepository` gives
`TestWalletApplicationService` a fully wired service that persists to an
in-memory dict. `PyFlyTestCase` + `mock_bean(WalletDomainRepository)` give
`TestDepositFundsHandler` an `AsyncMock` that is automatically installed into
the `ApplicationContext` — the same mock the handler's DI-injected dependencies
see. `assert_event_published` and `assert_no_events_published` verify that the
right events flow through `InMemoryEventBus` and that failed operations produce
no side effects.

At the peak, `@requires_docker` guards Testcontainers tests so they skip on
machines without Docker and run on CI. `postgres_container()` spins up a real
Postgres 16; `pyfly_config(pg, redis, base={...})` rewrites the sync driver
URL to `postgresql+asyncpg://` and wires both containers' connection details
into a `Config` that boots a full `ApplicationContext`. The real repository
round-trip proves that schema migrations, serialization, and query execution
all work against live infrastructure.

For the web layer, `PyFlyTestClient` drives the ASGI application synchronously
with fluent `assert_status`, `assert_json_path`, `assert_header`, and
`assert_body_contains` methods. `web_slice(WalletController, overrides={...})`
builds a production-like routing context with the service layer replaced by a
fake — the Spring `@WebMvcTest` equivalent in PyFly.

Concretely, you learned:

- **`create_test_container(overrides=...)`** — build a DI container with
  interface-to-fake mappings; resolve services that get their dependencies
  injected automatically.
- **`PyFlyTestCase` + `mock_bean(T)`** — declare `AsyncMock` attributes that
  are wired into the test `ApplicationContext`; configure return values and
  assert call counts without boilerplate setup.
- **`assert_event_published` / `assert_no_events_published`** — verify that
  `EventEnvelope` lists contain exactly the events you expect, with payload
  matching.
- **`@WebTest`, `@ServiceTest`, `@DataTest`** — intent markers; pair with
  `web_slice`, `service_slice`, `data_slice` for focused, started contexts.
- **`postgres_container()`, `redis_container()`, `pyfly_config(...)`** —
  Docker-backed integration tests that wire real infrastructure into PyFly
  config automatically, skipped cleanly when Docker is absent.
- **`PyFlyTestClient` + `TestResponse`** — synchronous ASGI test driver with
  JSONPath assertions and fluent chaining.

---

## Try it yourself {.exercises}

1. **Test the TransferFunds handler end-to-end.** Write a test class that
   extends `PyFlyTestCase` and declares `repo = mock_bean(WalletDomainRepository)`.
   In the test, configure `self.repo.find_by_id` as an `AsyncMock` that returns
   a real `Wallet` aggregate with a 10,000-cent EUR balance (for the source) and
   another with a 0-cent balance (for the target). Instantiate `TransferFundsHandler`
   with the mock, call `do_handle(TransferFunds(..., amount_cents=3000,
   currency="EUR"))`, and assert that `self.repo.save` was called twice and that
   both wallets' balances reflect the transfer.

2. **Add a Kafka container to the integration test.** Extend Listing 16.8 to
   include `kafka_container()` from `pyfly.testing`. Pass all three started
   containers to `pyfly_config`. Assert that `config.get(
   "pyfly.eda.kafka.bootstrap-servers")` is a non-empty string. Run the test
   locally with `pytest tests/integration -v` and confirm the Kafka URL is
   populated from the running container.

3. **Extend the web-slice test with header assertions.** Add a custom
   `X-Request-ID` response header to `WalletController.get_wallet` (generate a
   UUID and set it on the response). In the web-slice test, call
   `.assert_header("x-request-id")` (header names are lowercased by
   `PyFlyTestClient`) and `.assert_header("x-request-id", exists=True)` to
   verify the header is present. Then add a negative assertion:
   `.assert_header("x-powered-by", exists=False)` to confirm that header is
   absent from the response.
