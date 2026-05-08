# Copyright 2026 Firefly Software Foundation.
# Licensed under the Apache License, Version 2.0.
"""Tests for :class:`pyfly.domain.AggregateRoot`."""

from __future__ import annotations

from dataclasses import dataclass

from pyfly.domain import AggregateRoot, DomainEvent


@dataclass(frozen=True)
class OrderPlaced(DomainEvent):
    order_id: str = ""
    total: int = 0


@dataclass(frozen=True)
class OrderShipped(DomainEvent):
    order_id: str = ""


class _Order(AggregateRoot[str]):
    def __init__(self, id: str | None = None) -> None:
        super().__init__(id)
        self.shipped = False

    def place(self, total: int) -> None:
        assert self.id is not None
        self.raise_event(OrderPlaced(order_id=self.id, total=total))

    def ship(self) -> None:
        assert self.id is not None
        self.shipped = True
        self.raise_event(OrderShipped(order_id=self.id))


def test_new_aggregate_has_no_pending_events() -> None:
    o = _Order(id="o-1")
    assert o.pending_events() == []


def test_raise_event_appends_to_pending_list() -> None:
    o = _Order(id="o-1")
    o.place(total=100)

    events = o.pending_events()
    assert len(events) == 1
    assert isinstance(events[0], OrderPlaced)
    assert events[0].order_id == "o-1"
    assert events[0].total == 100


def test_pending_events_returns_a_copy() -> None:
    o = _Order(id="o-1")
    o.place(total=100)
    snapshot = o.pending_events()

    o.ship()
    # snapshot must not have grown
    assert len(snapshot) == 1
    assert len(o.pending_events()) == 2


def test_clear_events_drains_and_returns_pending() -> None:
    o = _Order(id="o-1")
    o.place(total=100)
    o.ship()

    drained = o.clear_events()

    assert len(drained) == 2
    assert isinstance(drained[0], OrderPlaced)
    assert isinstance(drained[1], OrderShipped)
    assert o.pending_events() == []


def test_aggregate_root_inherits_entity_identity_semantics() -> None:
    a = _Order(id="o-1")
    b = _Order(id="o-1")
    a.place(total=100)
    # Despite different pending events, they share an identity, so they
    # compare equal — the event log is not part of identity.
    assert a == b
    assert hash(a) == hash(b)


def test_domain_event_assigns_id_and_timestamp_automatically() -> None:
    e1 = OrderPlaced(order_id="o-1", total=10)
    e2 = OrderPlaced(order_id="o-1", total=10)

    assert e1.event_id != e2.event_id
    assert e1.occurred_at is not None
    assert e1.event_type == "OrderPlaced"
