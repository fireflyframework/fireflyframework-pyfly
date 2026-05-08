# Copyright 2026 Firefly Software Foundation.
# Licensed under the Apache License, Version 2.0.
"""Tests for the :class:`pyfly.domain.Entity` primitive."""

from __future__ import annotations

from pyfly.domain import Entity


class _Account(Entity[int]):
    def __init__(self, id: int | None = None, balance: int = 0) -> None:
        super().__init__(id)
        self.balance = balance


class _OtherEntity(Entity[int]):
    pass


def test_two_entities_with_same_id_compare_equal() -> None:
    a = _Account(id=1, balance=100)
    b = _Account(id=1, balance=999)  # different state, same identity
    assert a == b


def test_entities_with_different_ids_compare_not_equal() -> None:
    assert _Account(id=1) != _Account(id=2)


def test_entities_of_different_subclasses_are_never_equal() -> None:
    assert _Account(id=1) != _OtherEntity(id=1)


def test_transient_entities_compare_equal_only_by_object_identity() -> None:
    a = _Account()
    b = _Account()
    assert a != b
    assert a == a
    assert a.is_transient
    assert b.is_transient


def test_assigning_id_makes_entity_non_transient() -> None:
    a = _Account()
    assert a.is_transient
    a.id = 42
    assert not a.is_transient


def test_entity_is_hashable_when_id_is_set() -> None:
    a = _Account(id=1)
    b = _Account(id=1)
    seen = {a, b}
    assert len(seen) == 1


def test_entity_repr_includes_id() -> None:
    a = _Account(id=7)
    assert "_Account" in repr(a)
    assert "7" in repr(a)


def test_eq_with_non_entity_returns_notimplemented() -> None:
    a = _Account(id=1)
    assert (a == "not-an-entity") is False
    assert a != "not-an-entity"


def test_transient_entity_uses_object_identity_hash() -> None:
    a = _Account()
    b = _Account()
    assert hash(a) != hash(b)  # statistically guaranteed for distinct id()s
