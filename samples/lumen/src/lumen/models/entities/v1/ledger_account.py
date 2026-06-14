# Copyright 2026 Firefly Software Foundation.
# Licensed under the Apache License, Version 2.0.
"""The event-sourced :class:`LedgerAccount` aggregate (Chapter 9).

Where :class:`~lumen.models.entities.v1.wallet_entity.Wallet` is a
*state-stored* aggregate — its current balance lives in a row and the
repository overwrites that row on every change — ``LedgerAccount`` is the
**event-sourced** ledger behind the wallet's money movements. Nothing
stores "the balance". Instead every change appends an immutable
``LedgerOpened`` / ``Credited`` / ``Debited`` event to an
:class:`~pyfly.eventsourcing.EventStore`, and the *current* balance is
recomputed by **replaying** that event stream from the beginning.

The aggregate is built on :class:`pyfly.eventsourcing.AggregateRoot`,
which gives it the event-sourcing machinery:

* ``when(EventType, handler)`` registers an *apply-handler* — the pure
  function that folds one event into aggregate state.
* ``apply(event)`` routes a brand-new event through its handler **and**
  queues it for the event store (this is how commands mutate state).
* ``replay(event_type, event)`` re-runs a *persisted* event through the
  same handler **without** re-queuing it — this is how the repository
  rebuilds state on load.

Because the same handlers serve both ``apply`` and ``replay``, they must
be pure folds that only read the event's payload fields and never call
back into domain methods. On replay the repository hands the handler a
lightweight object whose attributes are the stored payload, so the
handlers read ``event.amount`` / ``event.balance`` / ``event.currency``
and trust those numbers — the invariants were already enforced on the
write that produced the event.

Money stays a first-class :class:`Money` value object on the command
side (so currency and overdraft rules are exact and centralised); the
events carry plain integer minor units + an ISO-4217 code, which is the
durable, schema-stable wire shape an event store should persist.
"""

from __future__ import annotations

from dataclasses import dataclass

from lumen.interfaces.enums.v1.currency import Currency
from lumen.models.entities.v1.money import Money
from pyfly.domain import BusinessRuleViolation
from pyfly.eventsourcing import AggregateRoot, DomainEvent

# ---------------------------------------------------------------------------
# Domain events — the durable facts of the ledger
# ---------------------------------------------------------------------------
#
# These subclass ``pyfly.eventsourcing.DomainEvent`` (NOT the
# ``pyfly.domain.DomainEvent`` the state-stored Wallet raises): the event
# store's repository serialises *these* with ``dataclasses.asdict`` and the
# event-sourcing ``AggregateRoot`` dispatches them by their ``event_type``
# (the class name). Every field carries a default so the repository can
# also build a typed instance from a stored payload.


@dataclass
class LedgerOpened(DomainEvent):
    """The ledger was opened for an owner in a single currency."""

    account_id: str = ""
    owner_id: str = ""
    currency: str = ""


@dataclass
class Credited(DomainEvent):
    """Money moved *into* the ledger (a deposit / inbound transfer leg)."""

    account_id: str = ""
    amount: int = 0
    currency: str = ""
    balance: int = 0


@dataclass
class Debited(DomainEvent):
    """Money moved *out of* the ledger (a withdrawal / outbound transfer leg)."""

    account_id: str = ""
    amount: int = 0
    currency: str = ""
    balance: int = 0


# ---------------------------------------------------------------------------
# The event-sourced aggregate root
# ---------------------------------------------------------------------------


class LedgerAccount(AggregateRoot):
    """An event-sourced money-movement ledger.

    Constructed with no arguments so it can serve as the repository's
    ``factory`` — :class:`~pyfly.eventsourcing.repository.EventSourcedRepository`
    calls ``LedgerAccount()`` and then assigns ``.id`` before replaying
    the stream. ``open`` is the only way to create a *new* ledger and is
    the first event in every stream.
    """

    def __init__(self) -> None:
        super().__init__()
        # Default ("empty") state, overwritten by replaying LedgerOpened.
        self.owner_id: str = ""
        self.currency: Currency = Currency.EUR
        self.balance: Money = Money.zero(Currency.EUR)
        # Register the apply-handlers. ``AggregateRoot._dispatch`` invokes a
        # handler as ``handler(aggregate, event)``, so each is a two-arg fold
        # over (aggregate, event). They are pure: read the event payload, set
        # state — the SAME handler runs for a freshly applied event and for a
        # replayed one.
        self.when(LedgerOpened, lambda agg, evt: agg._on_opened(evt))
        self.when(Credited, lambda agg, evt: agg._on_credited(evt))
        self.when(Debited, lambda agg, evt: agg._on_debited(evt))

    # --- factory ---------------------------------------------------------

    @classmethod
    def open(cls, account_id: str, owner_id: str, currency: Currency) -> LedgerAccount:
        """Open a brand-new, empty ledger; appends :class:`LedgerOpened`."""
        if not owner_id.strip():
            raise BusinessRuleViolation("ledger-owner-required", "owner_id is required")
        account = cls()
        account.id = account_id
        account.apply(LedgerOpened(account_id=account_id, owner_id=owner_id, currency=currency.value))
        return account

    # --- commands (write side: validate invariants, then ``apply``) ------

    def credit(self, amount: Money) -> None:
        """Record money entering the ledger; appends :class:`Credited`."""
        self._assert_currency(amount)
        if not amount.is_positive:
            raise BusinessRuleViolation("ledger-credit-positive", "credit amount must be > 0")
        new_balance = self.balance.add(amount)
        self.apply(
            Credited(
                account_id=self.id,
                amount=amount.amount,
                currency=amount.currency.value,
                balance=new_balance.amount,
            )
        )

    def debit(self, amount: Money) -> None:
        """Record money leaving the ledger; refuses to overdraw. Appends :class:`Debited`."""
        self._assert_currency(amount)
        if not amount.is_positive:
            raise BusinessRuleViolation("ledger-debit-positive", "debit amount must be > 0")
        remaining = self.balance.subtract(amount)
        if remaining.is_negative:
            raise BusinessRuleViolation(
                "ledger-insufficient-funds",
                f"cannot debit {amount}; balance is {self.balance}",
            )
        self.apply(
            Debited(
                account_id=self.id,
                amount=amount.amount,
                currency=amount.currency.value,
                balance=remaining.amount,
            )
        )

    # --- apply-handlers (the pure folds, shared by apply + replay) -------
    #
    # ``event`` is a ``Credited``/``Debited``/``LedgerOpened`` instance when
    # applied live, or a lightweight payload-carrying object when replayed
    # by the repository. Either way it exposes the same attribute names, so
    # these read fields and never call methods on it.

    def _on_opened(self, event: object) -> None:
        self.owner_id = event.owner_id  # type: ignore[attr-defined]
        self.currency = Currency(event.currency)  # type: ignore[attr-defined]
        self.balance = Money.zero(self.currency)

    def _on_credited(self, event: object) -> None:
        self.balance = Money(event.balance, Currency(event.currency))  # type: ignore[attr-defined]

    def _on_debited(self, event: object) -> None:
        self.balance = Money(event.balance, Currency(event.currency))  # type: ignore[attr-defined]

    # --- helpers ---------------------------------------------------------

    def _assert_currency(self, amount: Money) -> None:
        if amount.currency is not self.currency:
            raise BusinessRuleViolation(
                "ledger-currency-mismatch",
                f"ledger holds {self.currency.value}, got {amount.currency.value}",
            )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (
            f"LedgerAccount(id={self.id!r}, owner_id={self.owner_id!r}, balance={self.balance}, version={self.version})"
        )
