# OrderService Sample

DDD-flavoured Order Service built on the PyFly framework. Mirrors the
[`FireflyFramework.Samples.OrdersService`](https://github.com/fireflyframework/fireflyframework-dotnet/tree/main/samples)
.NET sample and the layered split used by Java domain microservices in
[`firefly-oss`](https://github.com/firefly-oss).

## Layered structure

```
samples/order_service/
├── src/order_service/
│   ├── interfaces/         # Public contract (DTOs, enums)
│   │   ├── dtos/v1/        # PlaceOrderRequest, OrderDto
│   │   └── enums/v1/       # OrderStatus
│   ├── models/             # Persistence layer
│   │   ├── entities/v1/    # Order aggregate root + domain events
│   │   └── repositories/   # Port + InMemoryOrderRepository
│   ├── core/               # Application core
│   │   ├── services/orders/    # Commands, queries, handlers, saga
│   │   └── mappers/        # Aggregate -> DTO mapping
│   ├── web/                # REST controllers
│   ├── sdk/                # Typed HTTP client
│   └── app.py              # @pyfly_application + @enable_domain_stack
├── tests/                  # Pytest end-to-end coverage
└── pyfly.yaml              # Framework configuration
```

The split is the same one used by every domain microservice in the
Firefly Java ecosystem: `interfaces` is the public boundary, `models`
holds the domain entities and repositories, `core` holds the business
logic, `web` exposes HTTP endpoints, and `sdk` is what other services
import to call this one.

## What the sample shows

- **`pyfly.domain` DDD primitives** — `Order` is a real
  `AggregateRoot[str]` that protects its invariants with
  `BusinessRuleViolation` and raises `DomainEvent` instances on every
  state change.
- **CQRS** — `PlaceOrderCommand`/`PlaceOrderHandler` and
  `GetOrderQuery`/`GetOrderHandler` use the production CQRS bus.
- **Saga** — `ConfirmOrderSaga` walks an order from `PLACED` through
  `INVENTORY_RESERVED` → `PAID` → `SHIPPED` with full compensation
  (release inventory, refund payment) on failure.
- **Hexagonal architecture** — `OrderRepository` is a `Protocol` port;
  `InMemoryOrderRepository` is the adapter. Swap to SQLAlchemy / MongoDB
  by writing a new adapter — no business code changes.
- **One-line bootstrap** — `@enable_domain_stack` activates CQRS,
  transactional engine, event sourcing, relational data, and rule
  engine in a single decorator (mirrors `AddFireflyDomain` in .NET and
  `fireflyframework-starter-domain` in Java).

## Running the tests

```bash
cd samples/order_service
PYTHONPATH=src ../../.venv/bin/python -m pytest tests/ -v
```

All 13 tests should pass:

- 7 aggregate-root invariant tests
- 3 CQRS bus integration tests
- 2 saga integration tests (happy path + compensation)
- 1 not-found edge case

## Running the service

```bash
cd samples/order_service
uv sync
uv run pyfly run --reload
# REST API: http://localhost:8080/api/v1/orders
# OpenAPI:  http://localhost:8080/docs
# Health:   http://localhost:8080/actuator/health
```

## REST API

| Method | Path                              | Description                             |
|--------|-----------------------------------|-----------------------------------------|
| POST   | `/api/v1/orders`                  | Place a new order (returns id)          |
| GET    | `/api/v1/orders/{id}`             | Fetch a single order                    |
| POST   | `/api/v1/orders/{id}/confirm`     | Run the confirm-order saga (reserve -> charge -> ship) |

Place an order:

```bash
curl -X POST http://localhost:8080/api/v1/orders \
  -H 'Content-Type: application/json' \
  -d '{"sku":"SKU-1","quantity":2,"unit_price":15.0}'

# {"order_id":"ord-..."}
```

Confirm it:

```bash
curl -X POST http://localhost:8080/api/v1/orders/ord-.../confirm

# {"order_id":"ord-...","saga_correlation_id":"...","status":"completed"}
```
