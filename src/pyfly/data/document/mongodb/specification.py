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
"""Composable query predicates for MongoDB via Beanie ODM.

Mirrors the SQLAlchemy :class:`~pyfly.data.relational.sqlalchemy.specification.Specification`
pattern but produces MongoDB filter documents (``dict``) instead of
SQLAlchemy ``Select`` statements.

Example::

    active = MongoSpecification(lambda root, q: {"active": True})
    admin  = MongoSpecification(lambda root, q: {"role": "admin"})

    # active admins
    results = await repo.find_all_by_spec(active & admin)

    # active OR admin
    results = await repo.find_all_by_spec(active | admin)

    # inactive users
    results = await repo.find_all_by_spec(~active)
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, TypeVar

from pyfly.data.specification import Specification as SpecificationBase

T = TypeVar("T")


class MongoSpecification(SpecificationBase[T, dict[str, Any]]):
    """Composable query predicate for MongoDB filter documents.

    A *MongoSpecification* wraps a callable that receives an entity class
    (``root``) and a query dict, returning a MongoDB filter document.

    Specifications can be combined using the standard Python operators:

    * ``spec_a & spec_b`` — both predicates must match (``$and``).
    * ``spec_a | spec_b`` — either predicate may match (``$or``).
    * ``~spec_a`` — negated predicate (``$nor``).
    """

    def __init__(self, predicate: Callable[[type[T], dict[str, Any]], dict[str, Any]]) -> None:
        self._predicate = predicate

    def to_predicate(self, root: type[T], query: dict[str, Any]) -> dict[str, Any]:
        """Apply this specification's predicate and return a MongoDB filter document."""
        return self._predicate(root, query)

    # ------------------------------------------------------------------
    # Combinators
    # ------------------------------------------------------------------

    def __and__(self, other: MongoSpecification[T]) -> MongoSpecification[T]:  # type: ignore[override]
        """Combine with AND: both specs must match (``$and``)."""
        left, right = self._predicate, other._predicate

        def and_predicate(root: type[T], q: dict[str, Any]) -> dict[str, Any]:
            left_doc = left(root, q)
            right_doc = right(root, q)
            if not left_doc:
                return right_doc
            if not right_doc:
                return left_doc
            return {"$and": [left_doc, right_doc]}

        return MongoSpecification(and_predicate)

    def __or__(self, other: MongoSpecification[T]) -> MongoSpecification[T]:  # type: ignore[override]
        """Combine with OR: either spec may match (``$or``)."""
        left, right = self._predicate, other._predicate

        def or_predicate(root: type[T], q: dict[str, Any]) -> dict[str, Any]:
            left_doc = left(root, q)
            right_doc = right(root, q)
            if not left_doc:
                return right_doc
            if not right_doc:
                return left_doc
            return {"$or": [left_doc, right_doc]}

        return MongoSpecification(or_predicate)

    def __invert__(self) -> MongoSpecification[T]:
        """Negate this specification: NOT (``$nor``)."""
        pred = self._predicate

        def not_predicate(root: type[T], q: dict[str, Any]) -> dict[str, Any]:
            doc = pred(root, q)
            if not doc:
                return {}
            return {"$nor": [doc]}

        return MongoSpecification(not_predicate)
