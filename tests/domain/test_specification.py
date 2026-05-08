# Copyright 2026 Firefly Software Foundation.
# Licensed under the Apache License, Version 2.0.
"""Tests for :class:`pyfly.domain.Specification`."""

from __future__ import annotations

from dataclasses import dataclass

from pyfly.domain import Specification


@dataclass
class _Customer:
    name: str
    age: int
    is_premium: bool = False


class IsAdult(Specification[_Customer]):
    def is_satisfied_by(self, candidate: _Customer) -> bool:
        return candidate.age >= 18


class IsPremium(Specification[_Customer]):
    def is_satisfied_by(self, candidate: _Customer) -> bool:
        return candidate.is_premium


def test_basic_specification_evaluates_predicate() -> None:
    spec = IsAdult()
    assert spec.is_satisfied_by(_Customer("Ada", 30))
    assert not spec.is_satisfied_by(_Customer("Bea", 12))


def test_specification_is_callable_for_use_with_filter() -> None:
    spec = IsAdult()
    customers = [_Customer("Ada", 30), _Customer("Bea", 12), _Customer("Coco", 18)]
    adults = list(filter(spec, customers))
    assert [c.name for c in adults] == ["Ada", "Coco"]


def test_and_combinator() -> None:
    spec = IsAdult() & IsPremium()
    assert spec.is_satisfied_by(_Customer("Ada", 30, is_premium=True))
    assert not spec.is_satisfied_by(_Customer("Ada", 30, is_premium=False))
    assert not spec.is_satisfied_by(_Customer("Bea", 12, is_premium=True))


def test_or_combinator() -> None:
    spec = IsAdult() | IsPremium()
    assert spec.is_satisfied_by(_Customer("Ada", 30, is_premium=False))
    assert spec.is_satisfied_by(_Customer("Bea", 12, is_premium=True))
    assert not spec.is_satisfied_by(_Customer("Bea", 12, is_premium=False))


def test_not_combinator() -> None:
    spec = ~IsAdult()
    assert not spec.is_satisfied_by(_Customer("Ada", 30))
    assert spec.is_satisfied_by(_Customer("Bea", 12))


def test_combinators_compose() -> None:
    # (adult AND premium) OR (NOT adult)
    spec = (IsAdult() & IsPremium()) | ~IsAdult()
    assert spec.is_satisfied_by(_Customer("Ada", 30, is_premium=True))  # adult premium
    assert spec.is_satisfied_by(_Customer("Bea", 12, is_premium=False))  # not adult
    assert not spec.is_satisfied_by(_Customer("Coco", 30, is_premium=False))  # adult, not premium


def test_specification_of_callable() -> None:
    spec = Specification.of(lambda c: c.age == 25, name="is_age_25")
    assert spec.is_satisfied_by(_Customer("Ada", 25))
    assert not spec.is_satisfied_by(_Customer("Bea", 30))
    assert "is_age_25" in repr(spec)


def test_callable_specification_composes_like_a_subclass() -> None:
    is_minor = Specification.of(lambda c: c.age < 18)
    is_premium = IsPremium()
    spec = is_minor & is_premium
    assert spec.is_satisfied_by(_Customer("Bea", 12, is_premium=True))
    assert not spec.is_satisfied_by(_Customer("Bea", 12, is_premium=False))
