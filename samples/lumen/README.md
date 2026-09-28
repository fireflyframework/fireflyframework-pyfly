# Lumen — Digital Wallet Sample

A DDD-flavoured digital-wallet service built on the PyFly framework. A
**Wallet** can be opened, deposited to, and withdrawn from, protecting
one core invariant — **the balance never goes negative** — and modelling
money with an exact, immutable **`Money`** value object (integer minor
units + ISO-4217 currency).

This sample is the companion to *PyFly by Example*. Every pattern here is
the real, running framework API.

## Layered structure

```
samples/lumen/
├── src/lumen/
│   ├── interfaces/         # Public contract (DTOs, enums)
│   │   ├── dtos/v1/        # OpenWalletRequest, DepositRequest, WalletDto, BalanceDto
│   │   └── enums/v1/       # Currency
│   ├── models/             # Domain + persistence layer
│   │   ├── entities/v1/    # Money value object, Wallet aggregate + events, WalletEntity row
│   │   └── repositories/   # WalletRepository (framework Repository on SQLite)
│   ├── core/               # Application core
│   │   ├── services/wallets/   # Commands, queries, handlers
│   │   └── mappers/        # Aggregate -> DTO mapping
│   ├── web/                # REST controllers
│   ├── sdk/                # Typed HTTP client
│   ├── app.py              # @pyfly_application + @enable_domain_stack
│   └── main.py             # ASGI entry point (PyFlyApplication -> app)
├── tests/                  # Pytest end-to-end coverage
└── pyfly.yaml              # Framework configuration
```

The split mirrors every domain microservice in the Firefly
ecosystem: `interfaces` is the public
boundary, `models` holds the domain model and repositories, `core` holds
the business logic, `web` exposes HTTP endpoints, and `sdk` is what other
services import to call this one.

## What the sample shows

- **`pyfly.domain` DDD primitives** — `Wallet` is a real
  `AggregateRoot[str]` that protects `balance >= 0` with
  `BusinessRuleViolation` and raises `DomainEvent` instances
  (`WalletOpened`, `FundsDeposited`, `FundsWithdrawn`) on every state
  change. `Money` is a `ValueObject` with structural equality and exact
  integer arithmetic.
- **A Spring-Data-style repository** — `WalletRepository` extends the
  framework's `Repository[WalletEntity, str]` over SQLite: inherited CRUD,
  a derived query (`find_by_owner_id`) and a `Specification` query
  (`find_rich`). Every call joins the unit of work of the
  `@transactional` command handler that makes it, or runs in a short unit
  of its own.
- **The invariant under concurrency** — the aggregate can only check the
  balance it was loaded with, so two requests that race could both see
  enough funds. The deposit and withdrawal handlers read the wallet with a
  pessimistic lock (`find_by_id(..., lock=LockMode.PESSIMISTIC_WRITE)`), and
  the money-transfer saga, whose steps run outside a caller's transaction,
  changes balances with one guarded `UPDATE` (`WalletRepository.debit` /
  `credit`). Two withdrawals of 60 from a wallet holding 100 never both
  succeed (`tests/test_concurrent_balance_changes.py`).
- **CQRS** — write intents (`OpenWallet`, `DepositFunds`,
  `WithdrawFunds`) and read intents (`GetWallet`, `GetBalance`) flow
  through the command/query bus to their `@command_handler` /
  `@query_handler` handlers.
- **A thin REST controller** — `WalletController` maps HTTP onto
  commands/queries and dispatches through the bus; it holds no business
  logic.

## REST API

| Method | Path                              | Purpose                       |
|--------|-----------------------------------|-------------------------------|
| POST   | `/api/v1/wallets`                 | Open a wallet                 |
| POST   | `/api/v1/wallets/{id}/deposit`    | Deposit funds (minor units)   |
| POST   | `/api/v1/wallets/{id}/withdraw`   | Withdraw funds (minor units)  |
| GET    | `/api/v1/wallets/{id}`            | Fetch the full wallet         |
| GET    | `/api/v1/wallets/{id}/balance`    | Fetch just the balance        |

Amounts are in **minor units** (cents): `1500` means €15.00 for an EUR
wallet.

## Run it

```bash
cd samples/lumen
uv sync --extra dev               # the framework from this checkout + pytest
uv run pytest -q                  # all green, on SQLite
uv run pyfly run --server uvicorn # serve on :8080 (uvicorn comes with pyfly[web])
```

The concurrency tests also run on PostgreSQL when `LUMEN_TEST_POSTGRES_URL`
names a server they may create databases on (each test gets a database of
its own, dropped afterwards):

```bash
LUMEN_TEST_POSTGRES_URL=postgresql+asyncpg://postgres:postgres@localhost:5432/postgres \
  uv run --extra dev --with asyncpg pytest tests/test_concurrent_balance_changes.py
```

`pyfly run` discovers `lumen.main:app` (the ASGI entry point) and serves
every `@rest_controller`. The default server is Granian; this sample
ships with Uvicorn (via `pyfly[web]`), so pass `--server uvicorn` unless
you add `pyfly[granian]`. The `cli` extra (already in `pyproject.toml`)
provides the `pyfly` command itself.

Smoke test with curl (`--port 8099` shown to avoid clashing with :8080):

```bash
# open a wallet
curl -s -X POST localhost:8099/api/v1/wallets \
  -H 'content-type: application/json' \
  -d '{"owner_id":"u-1","currency":"EUR"}'
# -> {"wallet_id":"wlt-..."}

# deposit €15.00
curl -s -X POST localhost:8099/api/v1/wallets/<id>/deposit \
  -H 'content-type: application/json' -d '{"amount":1500}'
# -> {"wallet_id":"wlt-...","balance_minor":1500}

# check the balance
curl -s localhost:8099/api/v1/wallets/<id>/balance
# -> {"id":"wlt-...","currency":"EUR","balance_minor":1500,"balance":15.0}
```

## License

Apache-2.0.
