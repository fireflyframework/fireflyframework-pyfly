# Copyright 2026 Firefly Software Foundation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""DDD :class:`Specification` — composable in-memory predicate.

This is the classic Eric Evans Specification pattern: an object that
knows whether a given domain object satisfies a business rule.
Specifications compose with ``&`` (and), ``|`` (or) and ``~`` (not), so
complex rules can be assembled from small, named building blocks.

The specification in :mod:`pyfly.data.specification` is a separate
abstraction for query-backend predicates (it pushes the rule down into
SQL or another query language). The DDD specification here is the
in-memory predicate used inside aggregates and domain services.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable
from typing import Generic, TypeVar

T = TypeVar("T")


class Specification(ABC, Generic[T]):
    """Composable in-memory predicate."""

    @abstractmethod
    def is_satisfied_by(self, candidate: T) -> bool:
        """Return ``True`` iff *candidate* satisfies the rule."""

    def __and__(self, other: Specification[T]) -> Specification[T]:
        return _AndSpecification(self, other)

    def __or__(self, other: Specification[T]) -> Specification[T]:
        return _OrSpecification(self, other)

    def __invert__(self) -> Specification[T]:
        return _NotSpecification(self)

    def __call__(self, candidate: T) -> bool:
        return self.is_satisfied_by(candidate)

    @classmethod
    def of(cls, predicate: Callable[[T], bool], *, name: str = "") -> Specification[T]:
        """Build a specification from a plain callable predicate.

        Useful for one-off rules that don't deserve their own subclass.
        """
        return _CallableSpecification(predicate, name=name)


class _AndSpecification(Specification[T]):
    __slots__ = ("_left", "_right")

    def __init__(self, left: Specification[T], right: Specification[T]) -> None:
        self._left = left
        self._right = right

    def is_satisfied_by(self, candidate: T) -> bool:
        return self._left.is_satisfied_by(candidate) and self._right.is_satisfied_by(candidate)


class _OrSpecification(Specification[T]):
    __slots__ = ("_left", "_right")

    def __init__(self, left: Specification[T], right: Specification[T]) -> None:
        self._left = left
        self._right = right

    def is_satisfied_by(self, candidate: T) -> bool:
        return self._left.is_satisfied_by(candidate) or self._right.is_satisfied_by(candidate)


class _NotSpecification(Specification[T]):
    __slots__ = ("_inner",)

    def __init__(self, inner: Specification[T]) -> None:
        self._inner = inner

    def is_satisfied_by(self, candidate: T) -> bool:
        return not self._inner.is_satisfied_by(candidate)


class _CallableSpecification(Specification[T]):
    __slots__ = ("_predicate", "_name")

    def __init__(self, predicate: Callable[[T], bool], *, name: str = "") -> None:
        self._predicate = predicate
        self._name = name or predicate.__name__

    def is_satisfied_by(self, candidate: T) -> bool:
        return self._predicate(candidate)

    def __repr__(self) -> str:
        return f"Specification.of({self._name})"
