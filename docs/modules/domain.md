# Domain (DDD building blocks)

`pyfly.domain` ships the foundational types every domain-driven design
codebase ends up reinventing — `Entity`, `ValueObject`, `AggregateRoot`,
`DomainEvent`, `Specification`, a `DomainRepository` protocol, and
domain-flavoured exceptions. The module is **pure standard-library
Python** with zero runtime dependencies, so you can import it from any
layer of the application.

It mirrors `org.fireflyframework.starter.domain` (Java) and
`FireflyFramework.Starter.Domain` (.NET) — but expressed natively in
modern Python (`Protocol`, `dataclass`, `StrEnum`, `Generic[T]`).

---

## Entity — identity-based equality

An *entity* is something whose identity is more important than its
state. Two entities are equal iff they share the same id, regardless of
any other field. Entities with `id=None` are *transient* (newly created,
not yet persisted) and compare equal only by Python `id()`.

```python
from pyfly.domain import Entity

class Account(Entity[int]):
    def __init__(self, id: int | None = None, balance: int = 0) -> None:
        super().__init__(id)
        self.balance = balance

assert Account(id=1, balance=100) == Account(id=1, balance=999)  # same identity
assert Account(id=1) != Account(id=2)                            # different identity
assert Account() != Account()                                    # both transient
```

Hashing follows the same contract: persisted entities hash by `(type, id)`,
transient entities use `object.__hash__`.

---

## ValueObject — structural equality, immutability

A *value object* is fully described by its attribute values. Subclass
`ValueObject` and decorate with `@dataclass(frozen=True)` (or
`frozen=True, slots=True` for performance). The base class is a marker
for static analysis and adds a uniform `replace(...)` helper.

```python
from dataclasses import dataclass
from pyfly.domain import ValueObject

@dataclass(frozen=True, slots=True)
class Money(ValueObject):
    amount: int
    currency: str

assert Money(100, "EUR") == Money(100, "EUR")
doubled = Money(100, "EUR").replace(amount=200)  # returns a new instance
```

Mutation raises `dataclasses.FrozenInstanceError`.

---

## AggregateRoot — entity that owns a consistency boundary

An aggregate root extends `Entity[TID]` with a buffer of *pending*
domain events. State changes happen through intent-revealing methods,
which call `self.raise_event(event)` to queue an event. Repositories
drain the buffer with `clear_events()` after persisting and the
application service publishes the events to the bus.

This is the *non-event-sourced* aggregate root. For the event-sourced
variant (with `apply`/`replay`/`when`) see
[`pyfly.eventsourcing.AggregateRoot`](eventsourcing.md).

```python
from dataclasses import dataclass
from pyfly.domain import AggregateRoot, BusinessRuleViolation, DomainEvent

@dataclass(frozen=True)
class OrderShipped(DomainEvent):
    order_id: str = ""
    tracking_number: str = ""

class Order(AggregateRoot[str]):
    def __init__(self, id: str, status: str = "placed") -> None:
        super().__init__(id)
        self.status = status

    def ship(self, tracking_number: str) -> None:
        if self.status == "shipped":
            raise BusinessRuleViolation("order-already-shipped")
        self.status = "shipped"
        assert self.id is not None
        self.raise_event(OrderShipped(order_id=self.id, tracking_number=tracking_number))

order = Order(id="o-1")
order.ship("trk-42")

events = order.clear_events()
# publish events to the message broker, then commit the unit of work
```

---

## DomainEvent — something that happened

`DomainEvent` is a frozen-dataclass base that auto-assigns a UUID
`event_id` and a UTC `occurred_at` timestamp. The `event_type` property
defaults to the subclass name.

```python
from dataclasses import dataclass
from pyfly.domain import DomainEvent

@dataclass(frozen=True)
class CustomerRegistered(DomainEvent):
    customer_id: str = ""
    email: str = ""

evt = CustomerRegistered(customer_id="c-1", email="alice@example.com")
print(evt.event_id, evt.occurred_at, evt.event_type)
```

---

## Specification — composable in-memory predicates

Specifications model business rules as objects: each one knows whether
a given candidate satisfies it. Combine specs with `&` (and), `|` (or),
and `~` (not).

```python
from pyfly.domain import Specification

class IsAdult(Specification[Customer]):
    def is_satisfied_by(self, c: Customer) -> bool:
        return c.age >= 18

class IsPremium(Specification[Customer]):
    def is_satisfied_by(self, c: Customer) -> bool:
        return c.is_premium

# Compose:
eligible = IsAdult() & IsPremium()
fallback = ~IsAdult() | IsPremium()

# Use as a predicate (it's callable):
adults = list(filter(IsAdult(), customers))

# Or build one from a lambda:
spec = Specification.of(lambda c: c.balance > 1000, name="big-spender")
```

Note: `pyfly.data.specification` is a *separate* abstraction for
backend-aware query predicates (it pushes the rule down into SQL or a
MongoDB filter). The DDD specification is the in-memory predicate used
inside aggregates and domain services. The two coexist.

---

## DomainRepository — collection-like protocol

A *DDD* repository speaks in aggregates, not rows. The protocol is
intentionally small — `add`, `find`, `remove`, `next_id` — because
aggregate boundaries make complex queries unnecessary inside the
domain. Read-side queries belong in projections and CQRS query
handlers.

```python
from pyfly.domain import AggregateRoot, DomainRepository

class Account(AggregateRoot[str]):
    ...

class AccountRepository(DomainRepository[Account, str]):
    ...

# A concrete implementation just needs the four methods. Because
# DomainRepository is a runtime-checkable Protocol, no inheritance is
# required.
```

---

## Exceptions — `DomainException` / `BusinessRuleViolation` / `AggregateNotFound`

Domain exceptions extend `pyfly.kernel.BusinessException` so the
existing RFC 7807 problem-details mapper, error filters, and
`@controller_advice` handlers translate them automatically.

```python
from pyfly.domain import BusinessRuleViolation, AggregateNotFound

raise BusinessRuleViolation(
    rule="loan-must-be-active",
    message="Cannot disburse a closed loan",
    context={"loan_id": loan_id},
)

raise AggregateNotFound("Loan", loan_id)
```

`BusinessRuleViolation` adds the rule name to `context["rule"]` and
defaults the error code to `DOMAIN_RULE_VIOLATION`.

---

## One-line bootstrap with the domain starter

For domain-tier microservices, decorate the application class with
`enable_domain_stack` to activate CQRS, the transactional engine
(saga/workflow/TCC), event sourcing, the rule engine, and the
relational data layer:

```python
from pyfly.core import pyfly_application
from pyfly.starters.domain import enable_domain_stack

@enable_domain_stack
@pyfly_application(name="my-domain-service", scan_packages=["my_service"])
class Application:
    pass
```

The full DDD primitives are also re-exported from
`pyfly.starters.domain`, so a single import line is enough:

```python
from pyfly.starters.domain import (
    AggregateRoot, BusinessRuleViolation, DomainEvent,
    DomainRepository, Entity, Specification, ValueObject,
    enable_domain_stack,
)
```

See the
[`samples/lumen/`](../../samples/lumen/README.md)
directory for an end-to-end DDD service that uses every primitive on
this page.
