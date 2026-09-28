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

    # authors with a book titled "t1", or named "Z": the join stays inside the operand
    has_t1 = Specification(lambda root, q: q.join(root.books).where(Book.title == "t1"))
    results = await repo.find_all_by_spec(has_t1 | Specification(lambda root, q: q.where(root.name == "Z")))

How the combinators work:

- ``&`` chains the two predicates on the query, so the joins of both stay in it (a page of entities a join
  repeats is cut from their distinct keys: see the repository's paging).
- ``|`` and ``~`` combine what their operands *match*, as criteria (:meth:`Specification.as_criterion`): an
  operand that only filters the root's own rows is its ``WHERE``; one that joins another table (or adds any
  other ``FROM``) is an ``EXISTS`` over its own statement, on an alias of the root correlated by its primary
  key. The join therefore never reaches the combined query: no cartesian product, no row dropped because an
  inner join found nothing for it, each entity once. What an operand does besides matching rows (an ordering, a
  fetch plan, a lock) does not reach the combined query either: apply it with ``&`` or to the result.
- Each operand is evaluated on a clean ``select`` of the root, never on the query it is combined into, so the
  query's own criteria (a ``SoftDeleteRepository``'s ``deleted_at IS NULL``) are neither copied into an operand
  nor negated with it, and the SQL grows linearly with the number of operands.
- An operand that matches everything (it adds no criterion, such as ``FilterUtils.from_dict({})``) is absent,
  as in Spring: ``noop | admin`` is ``admin`` and ``~noop`` restricts nothing.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, TypeVar

from sqlalchemy import Select, literal_column, not_, or_, select
from sqlalchemy import inspect as sa_inspect
from sqlalchemy.orm import Mapper, aliased
from sqlalchemy.sql.elements import ColumnElement

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

    ``root`` is the entity class, or an alias of it when the specification is an operand of ``|`` or ``~`` that
    joins another table: refer to the entity's attributes through ``root`` (module documentation).
    """

    def __init__(self, predicate: Callable[[type[T], Select[Any]], Select[Any]]) -> None:
        self._predicate = predicate

    def to_predicate(self, root: type[T], query: Select[Any]) -> Select[Any]:
        """Apply this specification's predicate to *query*."""
        return self._predicate(root, query)

    def as_criterion(self, root: type[T]) -> ColumnElement[bool] | None:
        """What this specification matches among *root*'s rows, as one criterion: ``None`` when it restricts
        nothing, its ``WHERE`` when it only filters *root*'s own rows, and otherwise an ``EXISTS`` over its own
        statement on an alias of *root*, correlated by the primary key (module documentation)."""
        base = select(root)
        applied = self._predicate(root, base)
        if _froms(applied) == _froms(base):
            return applied.whereclause
        inspected: Any = sa_inspect(root)  # the mapper of a mapped class, or the inspection of an alias
        mapper: Mapper[Any] = inspected.mapper
        alias: Any = aliased(mapper.class_)
        inner = self._predicate(alias, select(alias))
        keys = [mapper.get_property_by_column(column).key for column in mapper.primary_key]
        correlated = [getattr(alias, key) == getattr(root, key) for key in keys]
        probe: Select[Any] = inner.with_only_columns(literal_column("1"), maintain_column_froms=True).order_by(None)
        return probe.where(*correlated).exists()

    # ------------------------------------------------------------------
    # Combinators
    # ------------------------------------------------------------------

    def __and__(self, other: Specification[T]) -> Specification[T]:  # type: ignore[override]
        """Combine with AND: both specs must match.

        Implemented by chaining the two predicates sequentially — the
        left predicate is applied first, then the right predicate is
        applied to the already-filtered statement.  SQLAlchemy naturally
        combines successive ``.where()`` calls with AND, and the joins of both stay in the query.
        """
        left, right = self._predicate, other._predicate
        return Specification(lambda root, q: right(root, left(root, q)))

    def __or__(self, other: Specification[T]) -> Specification[T]:  # type: ignore[override]
        """Combine with OR: either spec may match.

        The operands' criteria (:meth:`as_criterion`) are combined with ``sqlalchemy.or_()`` and added to the
        query; an operand that restricts nothing is absent (``noop | spec`` is ``spec``).
        """
        left, right = self, other

        def or_predicate(root: type[T], query: Select[Any]) -> Select[Any]:
            criteria = [
                criterion for criterion in (left.as_criterion(root), right.as_criterion(root)) if criterion is not None
            ]
            if not criteria:
                return query
            return query.where(criteria[0] if len(criteria) == 1 else or_(*criteria))

        return Specification(or_predicate)

    def __invert__(self) -> Specification[T]:
        """Negate this specification: NOT.

        The specification's criterion (:meth:`as_criterion`) is wrapped with ``sqlalchemy.not_()`` and added to
        the query; a specification that restricts nothing stays so (``~noop`` restricts nothing).
        """
        spec = self

        def not_predicate(root: type[T], query: Select[Any]) -> Select[Any]:
            criterion = spec.as_criterion(root)
            return query if criterion is None else query.where(not_(criterion))

        return Specification(not_predicate)


def _froms(statement: Select[Any]) -> set[Any]:
    """The FROM elements *statement* renders (its tables and joins), without ORM annotations."""
    return {element._deannotate() for element in statement.get_final_froms()}
