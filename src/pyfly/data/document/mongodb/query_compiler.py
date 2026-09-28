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
"""MongoDB query method compiler — compiles a :class:`~pyfly.data.query_parser.ParsedQuery` into a query.

Implements :class:`~pyfly.data.ports.compiler.QueryMethodCompilerPort` for the MongoDB data adapter, on the
shared PartTree parser: the repository post-processor parses a derived method's name against the document's
fields (``parse(name, properties=...)``, so a name that names no field fails at startup), and this compiler
turns it into a :class:`MongoDerivedQuery` whose filter is built once per call from the arguments:

- the predicates are an ``or`` of ``and`` groups (:attr:`~pyfly.data.query_parser.ParsedQuery.groups`):
  ``find_by_a_or_b_and_c`` is ``a OR (b AND c)``, as in Spring and SQL;
- each field is mapped to its stored name (``id`` to ``_id``, an aliased field to its alias), and an id
  argument is converted to the document's id type;
- the operators have SQL's meaning (:mod:`~pyfly.data.document.mongodb.criteria`): anchored, case-sensitive
  ``like``; ``containing``, ``starting_with`` and ``ending_with`` match their argument as it is;
  ``_ignore_case``/``_all_ignore_case`` ignore case; ``!=``, ``not_in`` and ``not_like`` are false for a
  null or missing field.

What each prefix runs, in the repository's unit of work (its session):

* ``find_by``   -> the documents (``list``, one or ``None``, a ``Page`` or a ``Slice``, per the return
  annotation), a projection (``list[Projection]``, read with a server-side projection) or raw ``dict`` rows
* ``count_by``  -> ``int`` (``countDocuments``)
* ``exists_by`` -> ``bool`` (``{_id: 1}`` of at most one document)
* ``delete_by`` -> ``int``, the number deleted (one ``deleteMany``, or document by document when the
  document class has delete event actions)
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from types import SimpleNamespace
from typing import Any, TypeVar, cast

import pymongo

from pyfly.data.document.mongodb import criteria
from pyfly.data.document.mongodb.properties import ID_FIELD, InvalidIdError, coerce_id, document_properties
from pyfly.data.page import Page, Slice
from pyfly.data.pageable import Order, Pageable, Sort
from pyfly.data.projection import projection_fields
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

T = TypeVar("T")

_NOTHING: dict[str, Any] = {ID_FIELD: {"$in": []}}
"""A filter that matches no document (an id the document's id type cannot hold)."""


class MongoDerivedQuery:
    """One derived query method, compiled (see the module documentation).

    :meth:`run` executes it on a repository, in the repository's unit of work. Calling the object itself
    with a document class and the arguments (``await query(Model, *args)``, what the compiled callables of
    earlier releases took) runs it through a repository of that class.
    """

    def __init__(
        self,
        parsed: ParsedQuery,
        entity: type,
        shape: ResultShape,
        paths: Mapping[str, str],
        *,
        name: str | None = None,
    ) -> None:
        self.parsed = parsed
        self.entity = entity
        self.shape = shape
        self.name = name or parsed.prefix
        self._paths = dict(paths)
        self._projection: list[str] | None = (
            projection_fields(shape.type) if shape.element is ElementKind.PROJECTION and shape.type else None
        )

    # -- building --------------------------------------------------------------------------------------------

    def path(self, name: str) -> str:
        """The stored name of field *name*."""
        return self._paths.get(name, ID_FIELD if name == "id" else name)

    def filter(self, values: Sequence[Any]) -> dict[str, Any]:
        """The filter document for the call's *values*, in predicate order."""
        return build_filter(self.parsed, values, path=self.path, entity=self.entity)

    def orders(self, sort: Sort | None = None) -> Sort:
        """The name's ``order_by`` clauses, then *sort* (a ``Sort`` or a ``Pageable``'s)."""
        static = Sort(
            orders=tuple(
                Order(property=clause.field_name, direction="desc" if clause.direction == "desc" else "asc")
                for clause in self.parsed.order_clauses
            )
        )
        return static.and_then(sort) if sort is not None else static

    # -- running ---------------------------------------------------------------------------------------------

    async def run(
        self,
        repository: Any,
        values: Sequence[Any],
        *,
        pageable: Pageable | None = None,
        sort: Sort | None = None,
    ) -> Any:
        """Run the query on *repository* (a ``MongoRepository``) with the call's *values*."""
        filter_document = self.filter(values)
        prefix = self.parsed.prefix
        if prefix == "count_by":
            return await repository._count(filter_document)
        if prefix == "exists_by":
            return await repository._exists(filter_document)
        if prefix == "delete_by":
            return await repository._delete_where(filter_document, many=True)
        return await self._find(repository, filter_document, pageable=pageable, sort=sort)

    async def _find(
        self, repository: Any, filter_document: dict[str, Any], *, pageable: Pageable | None, sort: Sort | None
    ) -> Any:
        kind = self.shape.kind
        # The name's own orders are the repository's: __sortable__ narrows only the caller's Sort or Pageable.
        trusted = len(self.parsed.order_clauses)
        if kind is ResultKind.PAGE or kind is ResultKind.SLICE:
            if pageable is None:
                raise InvalidQueryMethodError(f"{self.name}: a Page or Slice result needs a Pageable argument")
            ordered = Pageable(page=pageable.page, size=pageable.size, sort=self.orders(pageable.sort))
            if kind is ResultKind.PAGE:
                page: Page[Any] = await repository._page(filter_document, ordered, trusted=trusted)
                return page
            window: Slice[Any] = await repository._slice(filter_document, ordered, trusted=trusted)
            return window
        order = self.orders(pageable.sort if pageable is not None else sort)
        skip = pageable.offset if pageable is not None and pageable.is_paged else None
        limit = pageable.size if pageable is not None and pageable.is_paged else None
        if kind is ResultKind.ONE:
            limit = 2
        if self.shape.element in (ElementKind.PROJECTION, ElementKind.MAPPING):
            rows = await _raw_rows(repository, filter_document, order, skip, limit, self._projected_paths(), trusted)
            results: list[Any] = [self._shape_row(row) for row in rows]
        else:
            results = await repository._find(filter_document, sort=order, skip=skip, limit=limit, trusted=trusted)
        if kind is ResultKind.ONE:
            if len(results) > 1:
                raise IncorrectResultSizeException(
                    f"{self.name} returns one document, and more than one matched", expected=1, actual=len(results)
                )
            return results[0] if results else None
        return results

    def _projected_paths(self) -> dict[str, int] | None:
        if self._projection is None:
            return None
        projected = {self.path(field): 1 for field in self._projection}
        if ID_FIELD not in projected:
            projected[ID_FIELD] = 0
        return projected

    def _shape_row(self, row: Mapping[str, Any]) -> Any:
        if self._projection is None:
            return dict(row)
        return SimpleNamespace(**{field: row.get(self.path(field)) for field in self._projection})

    async def __call__(self, target: Any, *args: Any) -> Any:
        """Run the query on *target*: a ``MongoRepository``, or a document class (through a repository of it)."""
        from pyfly.data.document.mongodb.repository import MongoRepository

        repository = target if isinstance(target, MongoRepository) else MongoRepository(target)
        pageable = next((arg for arg in args if isinstance(arg, Pageable)), None)
        sort = next((arg for arg in args if isinstance(arg, Sort)), None)
        values = [arg for arg in args if not isinstance(arg, (Pageable, Sort))]

        async def execute(self_arg: Any) -> Any:
            return await self.run(self_arg, values, pageable=pageable, sort=sort)

        from pyfly.data.document.mongodb.repository import repository_operation

        read = self.parsed.prefix != "delete_by"
        return await repository_operation(execute, read=read, atomic=True)(repository)


async def _raw_rows(
    repository: Any,
    filter_document: dict[str, Any],
    order: Sort,
    skip: int | None,
    limit: int | None,
    projection: dict[str, int] | None,
    trusted: int = 0,
) -> list[dict[str, Any]]:
    spec, computed = repository._sort_plan(order, trusted=trusted)
    criteria_document = repository._criteria(filter_document)
    async with repository._operation() as session:
        collection = repository._collection()
        if computed:
            pipeline: list[dict[str, Any]] = [
                {"$match": criteria_document},
                {"$addFields": computed},
                {"$sort": dict(spec)},
            ]
            if skip:
                pipeline.append({"$skip": skip})
            if limit is not None:
                pipeline.append({"$limit": limit})
            pipeline.append({"$unset": list(computed)})
            if projection is not None:
                pipeline.append({"$project": projection})
            cursor = await collection.aggregate(pipeline, session=session)
            return cast(list[dict[str, Any]], await cursor.to_list())
        found = collection.find(criteria_document, projection, session=session, sort=spec or None)
        if skip:
            found = found.skip(skip)
        if limit is not None:
            found = found.limit(limit)
        return cast(list[dict[str, Any]], await found.to_list())


def build_filter(
    parsed: ParsedQuery,
    values: Sequence[Any],
    *,
    path: Callable[[str], str] = lambda name: name,
    entity: type | None = None,
) -> dict[str, Any]:
    """The filter document of *parsed* for *values*: an ``$or`` of ``$and`` groups (a single clause or group
    stands alone). *path* maps a field to its stored name; with *entity*, id arguments are converted to its
    id type."""
    if not parsed.predicates:
        return {}
    groups: list[dict[str, Any]] = []
    index = 0
    for group in parsed.groups:
        clauses: list[dict[str, Any]] = []
        for predicate in group:
            clause, index = build_clause(
                path(predicate.field_name),
                predicate,
                values,
                index,
                ignore_case=predicate.ignore_case or parsed.all_ignore_case,
                entity=entity,
            )
            clauses.append(clause)
        groups.append(clauses[0] if len(clauses) == 1 else {"$and": clauses})
    return groups[0] if len(groups) == 1 else {"$or": groups}


def build_clause(
    field_name: str,
    predicate: FieldPredicate,
    args: Sequence[Any],
    arg_idx: int,
    *,
    ignore_case: bool = False,
    entity: type | None = None,
) -> tuple[dict[str, Any], int]:
    """One predicate's clause (see :mod:`~pyfly.data.document.mongodb.criteria`) and the index of the next
    argument."""
    op = predicate.operator
    folding = ignore_case and op in CASE_FOLDING_OPERATORS
    is_id = field_name == ID_FIELD and entity is not None

    def value(offset: int = 0) -> Any:
        return args[arg_idx + offset]

    def ids(raw: Any) -> list[Any]:
        items = criteria.listed(raw)
        if not is_id:
            return items
        converted = []
        for item in items:
            try:
                converted.append(coerce_id(cast(type, entity), item))
            except InvalidIdError:
                continue
        return converted

    if op in ("eq", "not") and is_id:
        converted = ids(value())
        if op == "eq":
            return ({field_name: converted[0]} if converted else dict(_NOTHING)), arg_idx + 1
        return ({field_name: criteria.not_equals(converted[0])} if converted else {}), arg_idx + 1
    if op == "eq":
        return {field_name: criteria.ignoring_case(value()) if folding else criteria.equals(value())}, arg_idx + 1
    if op == "not":
        if folding and isinstance(value(), str):
            return {field_name: {"$not": criteria.ignoring_case(value()), "$ne": None}}, arg_idx + 1
        return {field_name: criteria.not_equals(value())}, arg_idx + 1
    if op in ("gt", "gte", "lt", "lte"):
        return {field_name: {f"${op}": value()}}, arg_idx + 1
    if op == "between":
        return {field_name: {"$gte": value(), "$lte": value(1)}}, arg_idx + 2
    if op == "in":
        return {field_name: criteria.in_values(ids(value()))}, arg_idx + 1
    if op == "not_in":
        return {field_name: criteria.not_in_values(ids(value()))}, arg_idx + 1
    if op == "is_null":
        return {field_name: None}, arg_idx
    if op == "is_not_null":
        return {field_name: criteria.is_not_null()}, arg_idx
    if op == "is_true":
        return {field_name: True}, arg_idx
    if op == "is_false":
        return {field_name: False}, arg_idx
    if op == "like":
        return {field_name: criteria.like(value(), ignore_case=folding)}, arg_idx + 1
    if op == "not_like":
        return {field_name: {"$not": criteria.like(value(), ignore_case=folding), "$ne": None}}, arg_idx + 1
    if op == "containing":
        return {field_name: _literal(value(), start=False, end=False, folding=folding)}, arg_idx + 1
    if op == "not_containing":
        matched = _literal(value(), start=False, end=False, folding=folding)
        return {field_name: {"$not": matched, "$ne": None}}, arg_idx + 1
    if op == "starting_with":
        return {field_name: _literal(value(), start=True, end=False, folding=folding)}, arg_idx + 1
    if op == "ending_with":
        return {field_name: _literal(value(), start=False, end=True, folding=folding)}, arg_idx + 1
    raise ValueError(f"Unknown operator: {op}")


def _literal(value: Any, *, start: bool, end: bool, folding: bool) -> dict[str, Any]:
    return criteria.literal(value, anchor_start=start, anchor_end=end, ignore_case=folding)


class MongoQueryMethodCompiler:
    """Compile a :class:`ParsedQuery` into a :class:`MongoDerivedQuery` (see the module documentation)."""

    def compile(
        self,
        parsed: ParsedQuery,
        entity: type[T],
        *,
        return_type: Any = None,
        name: str | None = None,
    ) -> MongoDerivedQuery:
        """Compile *parsed* for the document class *entity*, returning what *return_type* annotates.

        Raises :class:`~pyfly.data.query_parser.InvalidQueryMethodError` for a prefix it does not know and
        for a ``find_by`` result a document query cannot give (a scalar, a tuple row or another class).
        """
        if parsed.prefix not in ("find_by", "count_by", "exists_by", "delete_by"):
            raise ValueError(f"Unknown prefix: {parsed.prefix}")
        shape = (
            result_shape(return_type, entity)
            if return_type is not None
            else ResultShape(ResultKind.LIST, ElementKind.ENTITY, entity)
        )
        if parsed.prefix == "find_by" and shape.element not in (
            ElementKind.ENTITY,
            ElementKind.PROJECTION,
            ElementKind.MAPPING,
        ):
            raise InvalidQueryMethodError(
                f"{name or parsed.prefix}: a derived MongoDB query returns documents, a projection or dict rows, "
                f"not {return_type!r}"
            )
        properties = document_properties(entity) or {}
        return MongoDerivedQuery(parsed, entity, shape, properties, name=name)

    # -- the building blocks, for adapters and tests ------------------------------------------------------------

    @staticmethod
    def _build_clause(
        field_name: str,
        predicate: FieldPredicate,
        args: Sequence[Any],
        arg_idx: int,
    ) -> tuple[dict[str, Any], int]:
        """One clause of a predicate on *field_name* (a stored name); see :func:`build_clause`."""
        return build_clause(field_name, predicate, args, arg_idx, ignore_case=predicate.ignore_case)

    def _build_filter(self, parsed: ParsedQuery, args: Sequence[Any]) -> dict[str, Any]:
        """The filter document of *parsed* for *args*, with the field names as they are (see
        :func:`build_filter`)."""
        return build_filter(parsed, args)

    @staticmethod
    def _build_sort(parsed: ParsedQuery) -> list[tuple[str, int]]:
        """The pymongo sort specification of the name's ``order_by`` clauses (field names as they are)."""
        return [
            (order.field_name, pymongo.ASCENDING if order.direction == "asc" else pymongo.DESCENDING)
            for order in parsed.order_clauses
        ]
