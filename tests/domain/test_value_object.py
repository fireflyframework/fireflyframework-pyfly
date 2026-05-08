# Copyright 2026 Firefly Software Foundation.
# Licensed under the Apache License, Version 2.0.
"""Tests for :class:`pyfly.domain.ValueObject`."""

from __future__ import annotations

from dataclasses import FrozenInstanceError, dataclass

import pytest

from pyfly.domain import ValueObject


@dataclass(frozen=True, slots=True)
class _Money(ValueObject):
    amount: int
    currency: str


class _NotADataclass(ValueObject):
    pass


def test_value_objects_with_same_state_are_equal() -> None:
    assert _Money(100, "EUR") == _Money(100, "EUR")


def test_value_objects_with_different_state_are_not_equal() -> None:
    assert _Money(100, "EUR") != _Money(100, "USD")
    assert _Money(100, "EUR") != _Money(200, "EUR")


def test_value_object_is_hashable_for_use_in_sets_and_dict_keys() -> None:
    seen = {_Money(100, "EUR"), _Money(100, "EUR"), _Money(200, "EUR")}
    assert len(seen) == 2


def test_replace_returns_a_new_instance() -> None:
    original = _Money(100, "EUR")
    doubled = original.replace(amount=200)

    assert doubled == _Money(200, "EUR")
    assert original == _Money(100, "EUR")  # original is untouched
    assert doubled is not original


def test_value_object_is_immutable() -> None:
    money = _Money(100, "EUR")
    with pytest.raises(FrozenInstanceError):
        money.amount = 999  # type: ignore[misc]


def test_replace_on_non_dataclass_raises() -> None:
    obj = _NotADataclass()
    with pytest.raises(TypeError, match=r"@dataclass\(frozen=True\)"):
        obj.replace()
