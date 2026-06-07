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
│   │   ├── entities/v1/    # Money value object, Wallet aggregate + events
│   │   └── repositories/   # Port + InMemoryWalletRepository
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

The split mirrors the `order_service` sample and every domain
microservice in the Firefly ecosystem: `interfaces` is the public
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
- **Hexagonal repository** — the core depends on the `WalletRepository`
  *port*; `InMemoryWalletRepository` is the in-memory *adapter*
  (`@repository`).
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
uv sync --extra dev               # framework (local v26.6.60) + pytest
uv run pytest -q                  # 17 tests, all green
uv run pyfly run --server uvicorn # serve on :8080 (uvicorn comes with pyfly[web])
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
