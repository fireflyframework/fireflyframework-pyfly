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
"""SQLAlchemy query method compiler — compiles ParsedQuery into async SQLAlchemy callables.

Implements :class:`~pyfly.data.ports.compiler.QueryMethodCompilerPort` for the
SQLAlchemy data adapter.

A compiled method (:class:`DerivedQuery`) is checked against the entity when it is compiled, at startup: every
field is a property of the entity (a column, a synonym, a hybrid, or a relationship to one entity or a composite,
compared with an instance of its class or ``None``), ``_ignore_case`` applies to string properties,
``_true``/``_false`` to boolean ones, and the return annotation is one a query of its prefix can return
(:class:`~pyfly.data.query_parser.ResultShape`). A method that is not raises
:class:`~pyfly.data.query_parser.InvalidQueryMethodError`.

Its statement is **built once** per shape and reused on every call, with its arguments bound as parameters
(Spring Data's ``PartTreeJpaQuery`` caches its criteria query the same way): SQLAlchemy memoizes a statement's
cache key on the statement, so a call costs what running a prebuilt statement costs. The shapes:

- ``None`` compares with ``IS NULL`` (``_not`` with ``IS NOT NULL``), a variant of the statement kept per
  pattern of ``None`` arguments;
- an ``_in`` list is one ``= ANY(:array)`` bind on PostgreSQL (the same statement for every length) and an
  expanding bind elsewhere, padded to the next power of two (``statements.padded``) within its share of what
  the dialect binds in one statement (the lists of a statement split it); a longer list runs one statement per
  chunk, where that gives the same answer (an unordered ``find_by`` of entities, a projection without ``_or_``,
  ``exists_by``, ``delete_by``, and ``count_by`` without ``_or_``), and raises ``ValueError`` otherwise;
- ``_containing``, ``_starting_with`` and ``_ending_with`` match their argument as it is (its ``%`` and ``_``
  escaped, ``ESCAPE '/'``); ``_like`` takes a pattern;
- a relationship or a composite compared with an instance, and a repository whose ``_criteria()`` is its own
  override (read on every call), build their statement per call.

What each prefix runs:

* ``find_by``   -> ``SELECT`` of the entity (or a projection's columns), shaped by the return annotation:
  ``list[T]``, ``T | None`` (``IncorrectResultSizeException`` when two rows match), ``Page[T]`` or
  ``Slice[T]`` with a ``Pageable`` argument, and ``list[T]`` ordered by a ``Sort`` argument;
* ``count_by``  -> ``SELECT count(*)`` -> ``int``;
* ``exists_by`` -> ``SELECT 1 ... LIMIT 1`` (``statements.exists_probe``) -> ``bool``;
* ``delete_by`` -> the number of deleted rows (or ``None`` for ``-> None``, the deleted entities for
  ``-> list[T]``): on a ``SoftDeleteRepository`` a soft delete of the live rows that match; elsewhere one bulk
  ``DELETE`` when the mapper has no cascade, version or delete listener (``statements.bulk_delete_safe``), and
  otherwise the matching entities deleted through the ORM (``soft_delete_criteria.hard_delete``: cascades run
  and reach soft-deleted dependents).

Soft delete: reads go through the soft-delete criteria every ORM ``SELECT`` gets (lifted inside
``including_deleted()``), and a ``SoftDeleteRepository`` adds its own ``deleted_at IS NULL``, which stays. A bulk
``DELETE`` of a soft-delete entity on a plain repository deletes what its reads see: the live rows, and the
deleted ones too inside ``including_deleted()``.
"""

from __future__ import annotations

import dataclasses
import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, TypeVar, cast

from sqlalchemy import (
    ARRAY,
    Boolean,
    Enum,
    String,
    all_,
    and_,
    any_,
    bindparam,
    false,
    func,
    not_,
    or_,
    select,
    true,
)
from sqlalchemy import delete as sa_delete
from sqlalchemy import inspect as sa_inspect
from sqlalchemy.engine import Dialect
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapper, RelationshipProperty
from sqlalchemy.types import TypeDecorator

from pyfly.data.pageable import Pageable, Sort
from pyfly.data.projection import projection_fields
from pyfly.data.property_resolver import InvalidPropertyError, PropertyResolver
from pyfly.data.query_parser import (
    CASE_FOLDING_OPERATORS,
    ElementKind,
    FieldPredicate,
    IncorrectResultSizeException,
    InvalidQueryMethodError,
    ParsedQuery,
    ResultKind,
    ResultShape,
    result_shape,
)
from pyfly.data.relational.sqlalchemy.entity import SoftDeleteMixin
from pyfly.data.relational.sqlalchemy.soft_delete_criteria import hard_delete, is_including_deleted
from pyfly.data.relational.sqlalchemy.statements import (
    RESERVED_BINDS,
    backend_name,
    bulk_delete_safe,
    dialect_of,
    exists_probe,
    in_list_limit,
    padded,
    unique_entities,
)

if TYPE_CHECKING:
    from pyfly.data.relational.sqlalchemy.repository import Repository

__all__ = ["ESCAPE", "DerivedQuery", "QueryMethodCompiler", "derived_properties", "escape_like"]

T = TypeVar("T")

ESCAPE = "/"
"""The escape character of the ``LIKE`` patterns that match an argument as it is (SQLAlchemy's
``autoescape`` uses the same)."""

_PER_LIST = frozenset({"oracle"})
"""Dialects whose IN limit counts the values of one list, not every bind of a statement."""

_NULL_AWARE = frozenset({"eq", "not"})
"""Operators that compare a ``None`` argument with ``IS NULL`` / ``IS NOT NULL``."""

_LIST_OPERATORS = frozenset({"in", "not_in"})
_RELATIONSHIP_OPERATORS = frozenset({"eq", "not", "is_null", "is_not_null"})
"""What a relationship or a composite compares with: an instance of its class, or ``None``."""
_BOOLEAN_OPERATORS = frozenset({"is_true", "is_false"})
_ESCAPED_PATTERNS: dict[str, Callable[[str], str]] = {
    "containing": lambda value: f"%{value}%",
    "not_containing": lambda value: f"%{value}%",
    "starting_with": lambda value: f"{value}%",
    "ending_with": lambda value: f"%{value}",
}


def escape_like(value: str) -> str:
    """*value* with the ``LIKE`` wildcards (and the escape character itself) escaped with :data:`ESCAPE`."""
    return value.replace(ESCAPE, ESCAPE * 2).replace("%", ESCAPE + "%").replace("_", ESCAPE + "_")


def derived_properties(entity: type) -> dict[str, str]:
    """The property names a derived query of *entity* may name: its columns, synonyms and hybrids
    (:class:`~pyfly.data.property_resolver.PropertyResolver`), its relationships to one entity and its
    composites (both compared with a value of their class)."""
    properties = dict(PropertyResolver.for_entity(entity).properties)
    mapper: Mapper[Any] = sa_inspect(entity)
    properties.update(
        (relationship.key, relationship.key)
        for relationship in mapper.relationships
        if not relationship.uselist and not relationship.key.startswith("_")
    )
    properties.update((prop.key, prop.key) for prop in mapper.composites if not prop.key.startswith("_"))
    return properties


def _sql_type(attribute: Any) -> Any:
    """The SQL type of an attribute's expression, through ``TypeDecorator`` wrappers."""
    sqltype = getattr(attribute, "type", None)
    while isinstance(sqltype, TypeDecorator):
        sqltype = sqltype.impl_instance
    return sqltype


def _is_string(attribute: Any) -> bool:
    """Whether case folding applies to *attribute*: a string column (an enum is a string type in SQLAlchemy, but
    a native type without ``lower()`` on PostgreSQL)."""
    sqltype = _sql_type(attribute)
    return isinstance(sqltype, String) and not isinstance(sqltype, Enum)


@dataclass(frozen=True)
class _Part:
    """A compiled predicate: the attribute it compares, how, and its arguments' positions."""

    predicate: FieldPredicate
    attribute: Any
    relationship: RelationshipProperty[Any] | None
    folds: bool
    first: int
    composite: Any = None
    """The composite property the attribute is (compared with a value of its class), or ``None``."""

    @property
    def by_value(self) -> type | None:
        """The class of the value a relationship or a composite compares with (``None`` for a column): such a
        comparison is built per call, with the value."""
        if self.relationship is not None:
            return cast(type, self.relationship.mapper.class_)
        if self.composite is not None:
            return cast(type, self.composite.composite_class)
        return None

    @property
    def operator(self) -> str:
        return self.predicate.operator

    @property
    def names(self) -> tuple[str, ...]:
        """The bind names of its arguments."""
        return tuple(f"p{self.first + offset}" for offset in range(self.predicate.arguments))


@dataclass(frozen=True)
class _Call:
    """One call's arguments: the ``None`` pattern that picks the statement variant and the bind parameters of
    each statement to run (one, or one per chunk of an IN list longer than a statement binds)."""

    nulls: tuple[bool, ...]
    parameters: list[dict[str, Any]]
    literals: dict[int, Any]
    criteria: tuple[Any, ...] = ()
    """The repository's own read criteria, for a query that reads them on every call."""


class DerivedQuery:
    """A compiled derived query method (module documentation).

    Call it as ``await query(session, *args)`` (a ``Pageable`` or ``Sort`` among *args pages or sorts it), or
    through :meth:`run` with the repository it belongs to, which the post-processor does.
    """

    def __init__(
        self,
        parsed: ParsedQuery,
        entity: type,
        shape: ResultShape,
        *,
        name: str,
        criteria: Sequence[Any] | None = (),
        load: Sequence[Any] = (),
        soft_deletes: bool = False,
    ) -> None:
        self._parsed = parsed
        self._entity = entity
        self._shape = shape
        self._name = name
        # None: the repository's own criteria (a subclass overriding _criteria()), read on every call.
        self._criteria = tuple(criteria) if criteria is not None else None
        self._load = tuple(load)
        self._soft_deletes = soft_deletes
        self._mapper: Mapper[Any] = sa_inspect(entity)
        self._parts = self._compile_parts()
        self._dynamic = self._criteria is None or any(part.by_value is not None for part in self._parts)
        self._lists = sum(1 for part in self._parts if part.operator in _LIST_OPERATORS)
        self._orders = self._compile_orders()
        self._columns = self._projection_columns()
        self._statements: dict[tuple[Any, ...], Any] = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Description
    # ------------------------------------------------------------------

    @property
    def parsed(self) -> ParsedQuery:
        """The parsed method name."""
        return self._parsed

    @property
    def shape(self) -> ResultShape:
        """The shape of the result (from the return annotation)."""
        return self._shape

    @property
    def name(self) -> str:
        """The method's name, for messages."""
        return self._name

    # ------------------------------------------------------------------
    # Compilation (at startup)
    # ------------------------------------------------------------------

    def _fail(self, message: str) -> InvalidQueryMethodError:
        return InvalidQueryMethodError(f"{self._name}: {message}")

    def _compile_parts(self) -> list[_Part]:
        parts: list[_Part] = []
        position = 0
        properties = derived_properties(self._entity)
        for predicate in self._parsed.predicates:
            name = predicate.field_name
            if name not in properties:
                raise self._fail(
                    f"{self._entity.__name__} has no property {name!r}; properties: {', '.join(sorted(properties))}"
                )
            attribute = getattr(self._entity, properties[name])
            relationship = self._mapper.relationships.get(name)
            composite = self._mapper.composites.get(name)
            operator = predicate.operator
            if (relationship is not None or composite is not None) and operator not in _RELATIONSHIP_OPERATORS:
                kind = "a relationship" if relationship is not None else "a composite"
                raise self._fail(
                    f"{name} is {kind}: it compares with an instance or None (equals, _not, _is_null, "
                    f"_is_not_null), not with {operator}"
                )
            by_value = relationship is not None or composite is not None
            if operator in _BOOLEAN_OPERATORS and not isinstance(_sql_type(attribute), Boolean):
                raise self._fail(f"{name} is not a boolean property, so it cannot be _true or _false")
            folds = False
            if predicate.ignore_case:
                if operator not in CASE_FOLDING_OPERATORS or by_value or not _is_string(attribute):
                    raise self._fail(f"{name} {operator} cannot ignore case: it applies to a string property's value")
                folds = True
            elif self._parsed.all_ignore_case:
                folds = operator in CASE_FOLDING_OPERATORS and not by_value and _is_string(attribute)
            parts.append(_Part(predicate, attribute, relationship, folds, position, composite))
            position += predicate.arguments
        return parts

    def _compile_orders(self) -> list[Any]:
        resolver = PropertyResolver.for_entity(self._entity)
        orders = []
        for clause in self._parsed.order_clauses:
            try:
                column = getattr(self._entity, resolver.resolve(clause.field_name, usage="sort"))
            except InvalidPropertyError as error:
                raise self._fail(str(error)) from None
            orders.append(column.desc() if clause.direction == "desc" else column.asc())
        return orders

    def _projection_columns(self) -> list[tuple[str, Any]]:
        if self._shape.element is not ElementKind.PROJECTION:
            return []
        resolver = PropertyResolver.for_entity(self._entity)
        columns = []
        for field in projection_fields(self._shape.type):
            try:
                columns.append((field, getattr(self._entity, resolver.resolve(field, usage="projection"))))
            except InvalidPropertyError as error:
                raise self._fail(str(error)) from None
        return columns

    # ------------------------------------------------------------------
    # Statements (built once per shape)
    # ------------------------------------------------------------------

    def _statement(self, kind: str, dialect: Dialect, call: _Call, *, visible_only: bool = False) -> Any:
        """The statement of *kind* for *dialect* and the call's ``None`` pattern, built on first use (a call with
        a relationship predicate builds its own, since it compares with the instance it is given, and so does a
        repository whose own ``_criteria()`` may change from call to call)."""
        key = (kind, backend_name(dialect), call.nulls, visible_only)
        if not self._dynamic:
            statement = self._statements.get(key)
            if statement is not None:
                return statement
        statement = self._build(kind, dialect, call, visible_only=visible_only)
        if not self._dynamic:
            with self._lock:
                statement = self._statements.setdefault(key, statement)
        return statement

    def _build(self, kind: str, dialect: Dialect, call: _Call, *, visible_only: bool) -> Any:
        where = [*(self._criteria if self._criteria is not None else call.criteria)]
        predicate = self._predicate(dialect, call)
        if predicate is not None:
            where.append(predicate)
        if kind == "find":
            if self._columns:
                statement: Any = select(*(column.label(field) for field, column in self._columns))
            else:
                statement = select(self._entity).options(*self._load)
            statement = statement.where(*where).order_by(*self._orders)
            return statement.limit(2) if self._shape.kind is ResultKind.ONE else statement
        if kind == "base":  # the find statement a page, a slice or a sort starts from
            return select(self._entity).where(*where).order_by(*self._orders)
        if kind == "count":
            return select(func.count()).select_from(self._entity).where(*where)
        if kind == "exists":
            return exists_probe(self._entity, *where)
        if kind == "keys":
            return select(*self._key_attributes()).where(*where)
        if kind == "delete":
            if visible_only:
                where.append(cast(Any, self._entity).deleted_at.is_(None))
            return sa_delete(self._entity).where(*where)
        raise ValueError(f"Unknown statement kind: {kind}")  # pragma: no cover — the kinds are fixed

    def _key_attributes(self) -> list[Any]:
        mapper = self._mapper
        return [getattr(self._entity, mapper.get_property_by_column(column).key) for column in mapper.primary_key]

    def _predicate(self, dialect: Dialect, call: _Call) -> Any:
        """The ``or`` of the ``and`` groups (``and`` binds tighter), ``None`` without predicates."""
        if not self._parts:
            return None
        by_predicate = {id(part.predicate): (index, part) for index, part in enumerate(self._parts)}
        groups = []
        for group in self._parsed.groups:
            conditions = []
            for predicate in group:
                index, part = by_predicate[id(predicate)]
                conditions.append(self._condition(part, dialect, call, index))
            groups.append(and_(*conditions) if len(conditions) > 1 else conditions[0])
        return or_(*groups) if len(groups) > 1 else groups[0]

    def _condition(self, part: _Part, dialect: Dialect, call: _Call, index: int) -> Any:
        column = part.attribute
        operator = part.operator
        if part.composite is not None:
            # A composite compares column by column (all of them NULL for None); its _not is the negation.
            value = None if operator in ("is_null", "is_not_null") else call.literals[part.first]
            matches = column == value
            return matches if operator in ("eq", "is_null") else not_(matches)
        if operator == "is_null" or (operator == "eq" and call.nulls[index]):
            return column.is_(None)
        if operator == "is_not_null" or (operator == "not" and call.nulls[index]):
            return column.is_not(None)
        if operator == "is_true":
            return column == true()
        if operator == "is_false":
            return column == false()
        if part.relationship is not None:
            value = call.literals[part.first]
            return column == value if operator == "eq" else column != value
        names = part.names
        if operator in _LIST_OPERATORS:
            if backend_name(dialect) == "postgresql":
                array = bindparam(names[0], type_=ARRAY(column.type))
                return column == any_(array) if operator == "in" else column != all_(array)
            listed: Any = bindparam(names[0], expanding=True)
            return column.in_(listed) if operator == "in" else column.not_in(listed)
        if operator == "between":
            return column.between(bindparam(names[0]), bindparam(names[1]))
        target = func.lower(column) if part.folds else column
        argument: Any = bindparam(names[0])
        if part.folds:
            argument = func.lower(argument)
        if operator == "eq":
            return target == argument
        if operator == "not":
            return target != argument
        if operator == "gt":
            return target > argument
        if operator == "gte":
            return target >= argument
        if operator == "lt":
            return target < argument
        if operator == "lte":
            return target <= argument
        if operator == "like":
            return target.like(argument)
        if operator == "not_like":
            return target.not_like(argument)
        if operator == "not_containing":
            return target.not_like(argument, escape=ESCAPE)
        if operator in _ESCAPED_PATTERNS:
            return target.like(argument, escape=ESCAPE)
        raise self._fail(f"unknown operator {operator!r}")  # pragma: no cover — the parser knows no other

    # ------------------------------------------------------------------
    # Arguments (per call)
    # ------------------------------------------------------------------

    def _call(self, dialect: Dialect, values: Sequence[Any]) -> _Call:
        """The call's ``None`` pattern and bind parameters (module documentation)."""
        if len(values) != self._parsed.argument_count:
            raise TypeError(
                f"{self._name} takes {self._parsed.argument_count} query argument"
                f"{'s' if self._parsed.argument_count != 1 else ''}, got {len(values)}"
            )
        nulls: list[bool] = []
        parameters: dict[str, Any] = {}
        literals: dict[int, Any] = {}
        overflowing: list[tuple[_Part, list[Any]]] = []
        backend = backend_name(dialect)
        capacity = self._capacity(dialect, backend)
        for part in self._parts:
            first = values[part.first] if part.predicate.arguments else None
            nulls.append(part.operator in _NULL_AWARE and part.by_value is None and first is None)
            if nulls[-1] or not part.predicate.arguments:
                continue
            if part.by_value is not None:
                literals[part.first] = self._related(part, first)
                continue
            if part.operator in _LIST_OPERATORS:
                listed = self._listed(part, first)
                if backend == "postgresql":
                    parameters[part.names[0]] = listed
                elif len(listed) > capacity:
                    overflowing.append((part, listed))
                else:
                    parameters[part.names[0]] = padded(listed, capacity)
                continue
            for name, value in zip(part.names, values[part.first : part.first + part.predicate.arguments], strict=True):
                parameters[name] = value
            pattern = _ESCAPED_PATTERNS.get(part.operator)
            if pattern is not None and first is not None:
                parameters[part.names[0]] = pattern(escape_like(str(first)))
        if not overflowing:
            return _Call(tuple(nulls), [parameters], literals)
        return _Call(tuple(nulls), self._chunked(parameters, overflowing, capacity, backend), literals)

    def _capacity(self, dialect: Dialect, backend: str) -> int:
        """How many values one IN list of the statement binds, padding included: the dialect's limit per list
        (Oracle), or its share of the statement's (the limit, less the binds reserved for the rest of the
        statement, split between its lists), so the lists together never bind more than the dialect takes."""
        if backend in _PER_LIST:
            return in_list_limit(dialect)
        return max(1, (in_list_limit(dialect) - RESERVED_BINDS) // max(1, self._lists))

    def _listed(self, part: _Part, value: Any) -> list[Any]:
        if value is None or isinstance(value, (str, bytes)) or not hasattr(value, "__iter__"):
            raise TypeError(f"{self._name}: {part.predicate.field_name} {part.operator} takes a collection of values")
        items = list(value)
        try:
            return list(dict.fromkeys(items))  # a repeated value changes nothing in an IN list
        except TypeError:
            return items

    def _related(self, part: _Part, value: Any) -> Any:
        """The value a relationship or a composite is compared with: an instance of its class, or ``None``."""
        target = cast(type, part.by_value)
        if value is not None and not isinstance(value, target):
            raise InvalidPropertyError(
                f"{self._name}: {part.predicate.field_name} is compared with a {target.__name__} or None, not a "
                f"{type(value).__name__}",
                entity=self._entity.__name__,
                property=part.predicate.field_name,
                usage="filter",
            )
        return value

    def _chunked(
        self,
        parameters: dict[str, Any],
        overflowing: list[tuple[_Part, list[Any]]],
        capacity: int,
        backend: str,
    ) -> list[dict[str, Any]]:
        """One set of parameters per chunk of the one IN list longer than a statement binds, when running the
        statement once per chunk gives the query's answer."""
        part, listed = overflowing[0]
        refusal = None
        if len(overflowing) > 1:
            refusal = "it has more than one such list"
        elif part.operator != "in":
            refusal = "a NOT IN list must be one list"
        elif self._parsed.prefix == "count_by" and len(self._parsed.groups) > 1:
            refusal = "a count with _or_ would count a row once per chunk it matches"
        elif self._shape.element is ElementKind.PROJECTION and len(self._parsed.groups) > 1:
            refusal = (
                "a projection with _or_ would return a row once per chunk it matches, and has no identity to tell "
                "the copies apart"
            )
        elif self._parsed.prefix == "find_by" and (
            self._orders or self._shape.kind in (ResultKind.PAGE, ResultKind.SLICE)
        ):
            refusal = "its rows have an order (or a page), which one statement per chunk would not keep"
        if refusal is not None:
            raise ValueError(
                f"{self._name}: {len(listed)} values for {part.predicate.field_name} exceed the {capacity} one "
                f"{backend} statement binds, and {refusal}"
            )
        name = part.names[0]
        return [
            {**parameters, name: padded(listed[start : start + capacity], capacity)}
            for start in range(0, len(listed), capacity)
        ]

    # ------------------------------------------------------------------
    # Execution
    # ------------------------------------------------------------------

    async def __call__(self, session: AsyncSession, *args: Any) -> Any:
        """Run the query on *session*; a ``Pageable`` or a ``Sort`` among *args* pages or sorts it."""
        pageable = next((arg for arg in args if isinstance(arg, Pageable)), None)
        sort = next((arg for arg in args if isinstance(arg, Sort)), None)
        values = [arg for arg in args if not isinstance(arg, (Pageable, Sort))]
        return await self.run(None, session, values, pageable=pageable, sort=sort)

    async def run(
        self,
        repository: Repository[Any, Any] | None,
        session: AsyncSession,
        values: Sequence[Any],
        *,
        pageable: Pageable | None = None,
        sort: Sort | None = None,
    ) -> Any:
        """Run the query on *session* for *repository* (a plain repository over the entity when ``None``) with
        the query *values* in order, paged by *pageable* or sorted by *sort*."""
        if repository is None:
            from pyfly.data.relational.sqlalchemy.repository import Repository

            repository = Repository(self._entity, session)
        dialect = dialect_of(session)
        call = self._call(dialect, values)
        if self._criteria is None:
            call = dataclasses.replace(call, criteria=tuple(repository._criteria()))
        prefix = self._parsed.prefix
        if prefix == "find_by":
            return await self._find(repository, session, dialect, call, pageable, sort)
        if prefix == "count_by":
            statement = self._statement("count", dialect, call)
            total = 0
            for parameters in call.parameters:
                total += int((await session.execute(statement, parameters)).scalar_one())
            return total
        if prefix == "exists_by":
            statement = self._statement("exists", dialect, call)
            for parameters in call.parameters:
                if (await session.execute(statement, parameters)).scalar() is not None:
                    return True
            return False
        if prefix == "delete_by":
            return await self._delete(repository, session, dialect, call)
        raise ValueError(f"Unknown prefix: {prefix}")  # pragma: no cover — the parser knows no other

    async def _find(
        self,
        repository: Repository[Any, Any],
        session: AsyncSession,
        dialect: Dialect,
        call: _Call,
        pageable: Pageable | None,
        sort: Sort | None,
    ) -> Any:
        kind = self._shape.kind
        if len(call.parameters) > 1 and (pageable is not None or sort is not None):
            raise ValueError(
                f"{self._name}: an IN list longer than one statement binds is read one statement per chunk, which "
                "would not keep the order (or the page) of the rows"
            )
        if pageable is not None or kind in (ResultKind.PAGE, ResultKind.SLICE):
            base = self._statement("base", dialect, call).params(**call.parameters[0])
            page = pageable or Pageable.unpaged()
            if kind is ResultKind.PAGE:
                return await repository._page(session, base, page, None)
            found = await repository._slice(session, base, page, None)
            return found if kind is ResultKind.SLICE else found.items
        statement = self._statement("find", dialect, call)
        if sort is not None:
            statement = statement.order_by(*repository._orders(session, sort))
        rows: list[Any] = []
        seen: set[int] = set()
        for parameters in call.parameters:
            result = await session.execute(statement, parameters)
            if self._columns:
                fields = [field for field, _column in self._columns]
                rows.extend(SimpleNamespace(**dict(zip(fields, row, strict=True))) for row in result.all())
                continue
            for entity in unique_entities(result):
                if id(entity) not in seen:  # a chunked list reads each entity once
                    seen.add(id(entity))
                    rows.append(entity)
        if kind is not ResultKind.ONE:
            return rows
        if len(rows) > 1:
            raise IncorrectResultSizeException(
                f"{self._name} returns one result, and more than one row matches", expected=1, actual=len(rows)
            )
        return rows[0] if rows else None

    async def _delete(
        self, repository: Repository[Any, Any], session: AsyncSession, dialect: Dialect, call: _Call
    ) -> Any:
        kind = self._shape.kind
        if self._soft_deletes:
            return await self._soft_delete(repository, session, dialect, call)
        if kind is ResultKind.LIST or not bulk_delete_safe(self._entity, session):
            entities = await self._entities(session, dialect, call, options=repository._delete_loads())
            if entities:
                await hard_delete(session, *entities)
            return self._deleted(entities, len(entities))
        visible_only = issubclass(self._entity, SoftDeleteMixin) and not is_including_deleted()
        statement = self._statement("delete", dialect, call, visible_only=visible_only)
        # The entities the unit holds are synchronized by fetching the deleted keys: evaluating the criteria in
        # Python would read the binds' values from the statement, and they come with the call.
        synchronize: str | bool = "fetch" if repository._holds_entities(session) else False
        deleted = 0
        for parameters in call.parameters:
            result = await session.execute(statement.execution_options(synchronize_session=synchronize), parameters)
            deleted += int(getattr(result, "rowcount", 0) or 0)
        return self._deleted([], deleted)

    async def _soft_delete(
        self, repository: Repository[Any, Any], session: AsyncSession, dialect: Dialect, call: _Call
    ) -> Any:
        """Soft-delete the live rows that match, through the ``SoftDeleteRepository``'s own soft delete (one
        ``UPDATE`` per key chunk: the version bumped, the audit columns stamped)."""
        soft = cast(Any, repository)
        if self._shape.kind is ResultKind.LIST:
            entities = await self._entities(session, dialect, call)
            if entities:
                await soft._soft_delete_entities(session, entities)
            return entities
        statement = self._statement("keys", dialect, call)
        keys: list[tuple[Any, ...]] = []
        for parameters in call.parameters:
            keys.extend(tuple(row) for row in (await session.execute(statement, parameters)).all())
        keys = list(dict.fromkeys(keys))  # a row an _or_ group matches comes back with every chunk
        if not keys:
            return self._deleted([], 0)
        stamps = await soft._stamps()
        criteria = soft._soft_delete_criteria(dialect, keys, stamps)
        deleted = await soft._soft_delete(session, criteria, stamps, None, keys, ())
        return self._deleted([], int(deleted))

    async def _entities(
        self, session: AsyncSession, dialect: Dialect, call: _Call, *, options: Sequence[Any] = ()
    ) -> list[Any]:
        statement = self._statement("base", dialect, call).options(*options)
        entities: list[Any] = []
        seen: set[int] = set()
        for parameters in call.parameters:
            for entity in unique_entities(await session.execute(statement, parameters)):
                if id(entity) not in seen:
                    seen.add(id(entity))
                    entities.append(entity)
        return entities

    def _deleted(self, entities: list[Any], count: int) -> Any:
        """What a delete returns for its shape: the entities, nothing, or the count."""
        if self._shape.kind is ResultKind.LIST:
            return entities
        if self._shape.kind is ResultKind.NONE:
            return None
        return count


class QueryMethodCompiler:
    """Compile a :class:`ParsedQuery` into a :class:`DerivedQuery`, an async callable.

    The returned callable has the signature::

        async def query_fn(session: AsyncSession, *args: Any) -> R

    where ``R`` depends on the prefix and the return annotation (module documentation):

    * ``find_by``   -> ``list[T]`` (``T | None``, ``Page[T]``, ``Slice[T]``, projections)
    * ``count_by``  -> ``int``
    * ``exists_by`` -> ``bool``
    * ``delete_by`` -> ``int``  (number of deleted rows; ``None`` or the deleted entities by annotation)
    """

    def compile(
        self,
        parsed: ParsedQuery,
        entity: type[T],
        *,
        return_type: Any = None,
        repository: Repository[Any, Any] | None = None,
        name: str | None = None,
    ) -> DerivedQuery:
        """Compile *parsed* for *entity*, shaped by *return_type* (the resolved return annotation; ``None``
        when there is none), with *repository*'s read criteria, fetch plan and soft delete when given. Raises
        :class:`~pyfly.data.query_parser.InvalidQueryMethodError` for a method that cannot be implemented."""
        described = name or f"{entity.__name__} repository {parsed.prefix}_..."
        if parsed.prefix not in ("find_by", "count_by", "exists_by", "delete_by"):
            raise ValueError(f"Unknown prefix: {parsed.prefix}")
        shape = _shape_of(parsed.prefix, return_type, entity, described)
        from pyfly.data.relational.sqlalchemy.soft_delete import SoftDeleteRepository

        return DerivedQuery(
            parsed,
            entity,
            shape,
            name=described,
            criteria=_static_criteria(repository),
            load=repository._load_options(None) if repository is not None else (),
            soft_deletes=isinstance(repository, SoftDeleteRepository),
        )


def _static_criteria(repository: Repository[Any, Any] | None) -> tuple[Any, ...] | None:
    """*repository*'s read criteria, when the framework's own ``_criteria()`` gives them (the same on every
    call); ``None`` when a subclass overrides it, so the query reads them on every call."""
    if repository is None:
        return ()
    from pyfly.data.relational.sqlalchemy.repository import Repository
    from pyfly.data.relational.sqlalchemy.soft_delete import SoftDeleteRepository

    own = type(repository)._criteria
    if own is Repository._criteria or own is SoftDeleteRepository._criteria:
        return tuple(repository._criteria())
    return None


def _shape_of(prefix: str, return_type: Any, entity: type, name: str) -> ResultShape:
    """The result shape of a derived query with *prefix* returning *return_type* (``None``: not annotated)."""
    if prefix == "count_by":
        if return_type not in (None, int, Any):
            raise InvalidQueryMethodError(f"{name}: a count_by method returns int, not {return_type}")
        return ResultShape(ResultKind.ONE, ElementKind.SCALAR, int)
    if prefix == "exists_by":
        if return_type not in (None, bool, Any):
            raise InvalidQueryMethodError(f"{name}: an exists_by method returns bool, not {return_type}")
        return ResultShape(ResultKind.ONE, ElementKind.SCALAR, bool)
    if return_type is None or return_type is Any:
        if prefix == "delete_by":
            return ResultShape(ResultKind.ONE, ElementKind.SCALAR, int)
        return ResultShape(ResultKind.LIST, ElementKind.ENTITY, entity)
    try:
        shape = result_shape(return_type, entity)
    except InvalidQueryMethodError as error:
        raise InvalidQueryMethodError(f"{name}: {error}") from None
    if prefix == "delete_by":
        if shape.kind is ResultKind.NONE or (shape.kind is ResultKind.ONE and shape.type is int):
            return shape
        if shape.kind is ResultKind.LIST and shape.element is ElementKind.ENTITY:
            return shape
        raise InvalidQueryMethodError(
            f"{name}: a delete_by method returns int (the count), None, or list[{entity.__name__}] (the deleted "
            f"entities), not {return_type}"
        )
    if shape.kind is ResultKind.NONE or shape.element not in (ElementKind.ENTITY, ElementKind.PROJECTION):
        raise InvalidQueryMethodError(
            f"{name}: a find_by method returns {entity.__name__} entities or a projection of them (list[...], "
            f"... | None, Page[...], Slice[...]), not {return_type}"
        )
    if shape.element is ElementKind.PROJECTION and shape.kind in (ResultKind.PAGE, ResultKind.SLICE):
        raise InvalidQueryMethodError(f"{name}: a page or slice holds entities, not projections")
    return shape
