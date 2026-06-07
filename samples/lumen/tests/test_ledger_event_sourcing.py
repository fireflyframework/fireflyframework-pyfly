# Copyright 2026 Firefly Software Foundation.
# Licensed under the Apache License, Version 2.0.
"""Feature 3 — the event-sourced :class:`LedgerAccount` (Chapter 9).

These tests drive the ledger through PyFly's real event-sourcing stack —
:class:`InMemoryEventStore` (the exact bean ``enable_domain_stack`` /
``pyfly.eventsourcing.enabled=true`` registers) plus the generic
:class:`EventSourcedRepository` wrapped by ``LedgerAccountRepository``.

The headline test proves *event sourcing actually works*: after opening,
crediting and debiting a ledger and ``save``-ing it, a **brand-new**
repository + aggregate is reconstructed purely by **replaying** the
stored event stream, and the recovered balance matches. Nothing reads a
stored "balance" column — state is a fold over events.
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from lumen.interfaces.enums.v1.currency import Currency
from lumen.models.entities.v1.ledger_account import (
    Credited,
    Debited,
    LedgerAccount,
    LedgerOpened,
)
from lumen.models.entities.v1.money import Money
from lumen.models.repositories.ledger_repository import LedgerAccountRepository

from pyfly.domain import BusinessRuleViolation
from pyfly.eventsourcing import (
    InMemoryEventStore,
    InMemorySnapshotStore,
)


@pytest_asyncio.fixture
async def event_store() -> InMemoryEventStore:
    """The zero-dep event store — the bean the eventsourcing auto-config wires."""
    return InMemoryEventStore()


# ---------------------------------------------------------------------------
# The aggregate in isolation: commands append events, invariants hold
# ---------------------------------------------------------------------------


def test_open_emits_ledger_opened_and_starts_empty() -> None:
    account = LedgerAccount.open("led-1", owner_id="owner-1", currency=Currency.EUR)
    assert account.id == "led-1"
    assert account.owner_id == "owner-1"
    assert account.currency is Currency.EUR
    assert account.balance == Money.zero(Currency.EUR)
    # apply() queued the event for the store and bumped the version.
    [event] = account.pending_events()
    assert isinstance(event, LedgerOpened)
    assert event.account_id == "led-1"
    assert event.currency == "EUR"
    assert account.version == 1


def test_credit_then_debit_track_the_balance() -> None:
    account = LedgerAccount.open("led-2", owner_id="o", currency=Currency.EUR)
    account.credit(Money(1000, Currency.EUR))
    account.debit(Money(400, Currency.EUR))
    assert account.balance == Money(600, Currency.EUR)
    kinds = [type(e).__name__ for e in account.pending_events()]
    assert kinds == ["LedgerOpened", "Credited", "Debited"]
    assert account.version == 3


def test_debit_cannot_overdraw() -> None:
    account = LedgerAccount.open("led-3", owner_id="o", currency=Currency.EUR)
    account.credit(Money(500, Currency.EUR))
    with pytest.raises(BusinessRuleViolation) as exc:
        account.debit(Money(501, Currency.EUR))
    assert exc.value.rule == "ledger-insufficient-funds"
    # Invariant held: balance unchanged, no Debited event queued.
    assert account.balance == Money(500, Currency.EUR)
    assert [type(e).__name__ for e in account.pending_events()] == [
        "LedgerOpened",
        "Credited",
    ]


def test_credit_currency_must_match_the_ledger() -> None:
    account = LedgerAccount.open("led-4", owner_id="o", currency=Currency.EUR)
    with pytest.raises(BusinessRuleViolation) as exc:
        account.credit(Money(100, Currency.USD))
    assert exc.value.rule == "ledger-currency-mismatch"


# ---------------------------------------------------------------------------
# The headline proof: state survives a reload-by-replay from the store
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_balance_survives_reload_by_replay(event_store: InMemoryEventStore) -> None:
    # 1. Open + credit + debit, then persist the pending events.
    account = LedgerAccount.open("acct-1", owner_id="owner-7", currency=Currency.EUR)
    account.credit(Money(2500, Currency.EUR))  # +25.00
    account.debit(Money(1000, Currency.EUR))   # -10.00 -> 15.00
    assert account.balance == Money(1500, Currency.EUR)

    repo = LedgerAccountRepository(event_store)
    await repo.save(account)
    # After committing, the aggregate has no pending events left to flush.
    assert account.pending_events() == []

    # 2. Reconstruct from a *fresh* repository + aggregate. Nothing here
    #    carries the in-memory object's state — load() rebuilds the
    #    LedgerAccount purely by replaying the stored event stream.
    fresh_repo = LedgerAccountRepository(event_store)
    recovered = await fresh_repo.load("acct-1")

    assert recovered is not None, "ledger should rebuild from its event stream"
    assert recovered is not account
    assert recovered.owner_id == "owner-7"
    assert recovered.currency is Currency.EUR
    # >>> the load-by-replay proof: balance recomputed from events, not stored
    assert recovered.balance == Money(1500, Currency.EUR)
    # Three events were folded in: LedgerOpened, Credited, Debited.
    assert recovered.version == 3
    # A reconstructed aggregate has nothing pending — it was not "changed".
    assert recovered.pending_events() == []


@pytest.mark.asyncio
async def test_store_holds_the_immutable_event_stream(event_store: InMemoryEventStore) -> None:
    account = LedgerAccount.open("acct-2", owner_id="o", currency=Currency.GBP)
    account.credit(Money(800, Currency.GBP))
    account.debit(Money(300, Currency.GBP))
    await LedgerAccountRepository(event_store).save(account)

    # The store kept an ordered, typed envelope per event — the source of truth.
    envelopes = await event_store.load("acct-2")
    assert [e.event_type for e in envelopes] == ["LedgerOpened", "Credited", "Debited"]
    assert [e.sequence for e in envelopes] == [1, 2, 3]
    assert all(e.aggregate_type == "LedgerAccount" for e in envelopes)
    # The Debited payload carries the post-debit running balance (minor units).
    assert envelopes[2].payload["balance"] == 500
    assert await event_store.latest_version("acct-2") == 3


@pytest.mark.asyncio
async def test_typed_replay_rebuilds_real_event_dataclasses() -> None:
    """The repo override hydrates the concrete event dataclasses on replay."""
    repo = LedgerAccountRepository(InMemoryEventStore())
    # Simulate a stored envelope coming back from the store.
    from pyfly.eventsourcing import StoredEventEnvelope

    credited = repo._envelope_to_event(
        StoredEventEnvelope(
            aggregate_id="x",
            event_type="Credited",
            payload={"account_id": "x", "amount": 99, "currency": "EUR", "balance": 99},
        )
    )
    assert isinstance(credited, Credited)
    assert credited.amount == 99 and credited.balance == 99

    debited = repo._envelope_to_event(
        StoredEventEnvelope(
            aggregate_id="x",
            event_type="Debited",
            payload={"account_id": "x", "amount": 9, "currency": "EUR", "balance": 90},
        )
    )
    assert isinstance(debited, Debited)


@pytest.mark.asyncio
async def test_continues_appending_after_a_reload(event_store: InMemoryEventStore) -> None:
    """A reloaded aggregate keeps the correct version, so further appends
    flow into the same stream with the right optimistic-lock expectation."""
    repo = LedgerAccountRepository(event_store)
    account = LedgerAccount.open("acct-3", owner_id="o", currency=Currency.EUR)
    account.credit(Money(1000, Currency.EUR))
    await repo.save(account)

    # Reload, mutate again, save again — version must advance, no conflict.
    reloaded = await repo.load("acct-3")
    assert reloaded is not None
    assert reloaded.version == 2
    reloaded.debit(Money(250, Currency.EUR))
    await repo.save(reloaded)

    final = await repo.load("acct-3")
    assert final is not None
    assert final.balance == Money(750, Currency.EUR)
    assert final.version == 3
    assert await event_store.latest_version("acct-3") == 3


@pytest.mark.asyncio
async def test_snapshot_store_round_trips_the_ledger() -> None:
    """With a snapshot store wired, reload still yields the right state.

    The ledger's stream is far shorter than the snapshot interval, so this
    proves the snapshot seam is harmless (no snapshot taken yet) and the
    repository falls back to a full replay.
    """
    store = InMemoryEventStore()
    snapshots = InMemorySnapshotStore()
    repo = LedgerAccountRepository(store, snapshots=snapshots)

    account = LedgerAccount.open("acct-4", owner_id="o", currency=Currency.EUR)
    account.credit(Money(5000, Currency.EUR))
    await repo.save(account)

    recovered = await repo.load("acct-4")
    assert recovered is not None
    assert recovered.balance == Money(5000, Currency.EUR)


@pytest.mark.asyncio
async def test_load_unknown_ledger_returns_none(event_store: InMemoryEventStore) -> None:
    repo = LedgerAccountRepository(event_store)
    assert await repo.load("nope") is None


# ---------------------------------------------------------------------------
# Boot wiring: the eventsourcing auto-config registers the store beans
# ---------------------------------------------------------------------------


def test_auto_configuration_registers_event_store_beans() -> None:
    """``enable_domain_stack`` activates this auto-config when
    ``pyfly.eventsourcing.enabled=true`` (set in pyfly.yaml), registering
    the in-memory event/snapshot stores the ledger repository depends on."""
    from pyfly.eventsourcing.auto_configuration import EventSourcingAutoConfiguration

    config = EventSourcingAutoConfiguration()
    assert isinstance(config.event_store(), InMemoryEventStore)
    assert isinstance(config.snapshot_store(), InMemorySnapshotStore)
