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
"""Composable query predicates for type-safe dynamic queries.

Inspired by Spring Data's ``Specification`` pattern, this module lets
callers build arbitrarily complex SQLAlchemy WHERE clauses by composing
small, reusable predicate objects with ``&`` (AND), ``|`` (OR), and
``~`` (NOT).

Example::

    active = Specification(lambda root, q: q.where(root.active == True))
    admin  = Specification(lambda root, q: q.where(root.role == "admin"))

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

from sqlalchemy import Select

from pyfly.data.specification import Specification as SpecificationBase

T = TypeVar("T")


class Specification(SpecificationBase[T, Select[Any]]):
    """Composable query predicate for type-safe dynamic queries.

    A *Specification* wraps a callable that receives an entity class
    (``root``) and a SQLAlchemy ``Select`` statement, returning a
    modified ``Select`` with the desired WHERE clause applied.

    Specifications can be combined using the standard Python operators:

    * ``spec_a & spec_b`` — both predicates must match (AND).
    * ``spec_a | spec_b`` — either predicate may match (OR).
    * ``~spec_a`` — negated predicate (NOT).
    """

    def __init__(self, predicate: Callable[[type[T], Select[Any]], Select[Any]]) -> None:
        self._predicate = predicate

    def to_predicate(self, root: type[T], query: Select[Any]) -> Select[Any]:
        """Apply this specification's predicate to *query*."""
        return self._predicate(root, query)

    # ------------------------------------------------------------------
    # Combinators
    # ------------------------------------------------------------------

    def __and__(self, other: Specification[T]) -> Specification[T]:  # type: ignore[override]
        """Combine with AND: both specs must match.

        Implemented by chaining the two predicates sequentially — the
        left predicate is applied first, then the right predicate is
        applied to the already-filtered statement.  SQLAlchemy naturally
        combines successive ``.where()`` calls with AND.
        """
        left, right = self._predicate, other._predicate
        return Specification(lambda root, q: right(root, left(root, q)))

    def __or__(self, other: Specification[T]) -> Specification[T]:  # type: ignore[override]
        """Combine with OR: either spec may match.

        Each predicate is applied independently to a *clean* copy of the
        query.  The resulting ``whereclause`` elements are extracted and
        combined using ``sqlalchemy.or_()``.
        """
        from sqlalchemy import or_

        left_pred, right_pred = self._predicate, other._predicate

        def or_predicate(root: type[T], query: Select[Any]) -> Select[Any]:
            left_q = left_pred(root, query)
            right_q = right_pred(root, query)
            left_clause = left_q.whereclause
            right_clause = right_q.whereclause
            if left_clause is not None and right_clause is not None:
                return query.where(or_(left_clause, right_clause))
            if left_clause is not None:
                return left_q
            if right_clause is not None:
                return right_q
            return query

        return Specification(or_predicate)

    def __invert__(self) -> Specification[T]:
        """Negate this specification: NOT.

        The original predicate is applied to the query, and its
        ``whereclause`` is wrapped with ``sqlalchemy.not_()``.
        """
        from sqlalchemy import not_

        pred = self._predicate

        def not_predicate(root: type[T], query: Select[Any]) -> Select[Any]:
            modified = pred(root, query)
            clause = modified.whereclause
            if clause is not None:
                return query.where(not_(clause))
            return query

        return Specification(not_predicate)
