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
"""MongoDB query method compiler — compiles ParsedQuery into Beanie/pymongo operations.

Implements :class:`~pyfly.data.ports.compiler.QueryMethodCompilerPort` for the
MongoDB data adapter.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Coroutine, Sequence
from types import SimpleNamespace
from typing import Any, TypeVar, cast, get_args, get_origin

import pymongo

from pyfly.data.projection import is_projection, projection_fields
from pyfly.data.query_parser import FieldPredicate, ParsedQuery

T = TypeVar("T")


class MongoQueryMethodCompiler:
    """Compile a :class:`ParsedQuery` into an async callable for MongoDB.

    The returned callable has the signature::

        async def query_fn(model: type[T], *args: Any) -> R

    where ``R`` depends on the prefix:

    * ``find_by``   -> ``list[T]``
    * ``count_by``  -> ``int``
    * ``exists_by`` -> ``bool``
    * ``delete_by`` -> ``int``  (number of deleted documents)
    """

    def compile(
        self,
        parsed: ParsedQuery,
        entity: type[T],
        *,
        return_type: type | None = None,
    ) -> Callable[..., Coroutine[Any, Any, Any]]:
        """Dispatch to the correct compile method based on prefix."""
        dispatch: dict[str, Any] = {
            "find_by": self._compile_find,
            "count_by": self._compile_count,
            "exists_by": self._compile_exists,
            "delete_by": self._compile_delete,
        }
        builder = dispatch.get(parsed.prefix)
        if builder is None:
            raise ValueError(f"Unknown prefix: {parsed.prefix}")
        result_type = Callable[..., Coroutine[Any, Any, Any]]
        if parsed.prefix == "find_by":
            return cast(result_type, builder(parsed, entity, return_type=return_type))
        return cast(result_type, builder(parsed, entity))

    # ------------------------------------------------------------------
    # find_by
    # ------------------------------------------------------------------

    def _compile_find(
        self,
        parsed: ParsedQuery,
        entity: type[T],
        *,
        return_type: type | None = None,
    ) -> Callable[..., Coroutine[Any, Any, list[T]]]:
        # Detect projection type from list[ProjectionType] annotation
        proj_type: type | None = None
        if return_type is not None:
            origin = get_origin(return_type)
            if origin is list:
                type_args = get_args(return_type)
                if type_args and is_projection(type_args[0]):
                    proj_type = type_args[0]

        async def _execute(model: type[T], *args: Any) -> list[T]:
            filter_doc = self._build_filter(parsed, args)
            query = model.find(filter_doc)  # type: ignore[attr-defined]
            sort_spec = self._build_sort(parsed)
            if sort_spec:
                query = query.sort(sort_spec)
            docs = await query.to_list()
            if proj_type is not None:
                fields = projection_fields(proj_type)
                return [
                    SimpleNamespace(  # type: ignore[misc]
                        **{f: getattr(doc, f, doc.get(f) if isinstance(doc, dict) else None) for f in fields}
                    )
                    for doc in docs
                ]
            return cast(list[T], docs)

        return _execute

    # ------------------------------------------------------------------
    # count_by
    # ------------------------------------------------------------------

    def _compile_count(
        self,
        parsed: ParsedQuery,
        entity: type[T],
    ) -> Callable[..., Coroutine[Any, Any, int]]:
        async def _execute(model: type[T], *args: Any) -> int:
            filter_doc = self._build_filter(parsed, args)
            return cast(int, await model.find(filter_doc).count())  # type: ignore[attr-defined]

        return _execute

    # ------------------------------------------------------------------
    # exists_by
    # ------------------------------------------------------------------

    def _compile_exists(
        self,
        parsed: ParsedQuery,
        entity: type[T],
    ) -> Callable[..., Coroutine[Any, Any, bool]]:
        async def _execute(model: type[T], *args: Any) -> bool:
            filter_doc = self._build_filter(parsed, args)
            count = await model.find(filter_doc).count()  # type: ignore[attr-defined]
            return cast(bool, count > 0)

        return _execute

    # ------------------------------------------------------------------
    # delete_by
    # ------------------------------------------------------------------

    def _compile_delete(
        self,
        parsed: ParsedQuery,
        entity: type[T],
    ) -> Callable[..., Coroutine[Any, Any, int]]:
        async def _execute(model: type[T], *args: Any) -> int:
            filter_doc = self._build_filter(parsed, args)
            docs = await model.find(filter_doc).to_list()  # type: ignore[attr-defined]
            for doc in docs:
                await doc.delete()
            return len(docs)

        return _execute

    # ------------------------------------------------------------------
    # Filter building
    # ------------------------------------------------------------------

    @staticmethod
    def _build_clause(
        field_name: str,
        predicate: FieldPredicate,
        args: Sequence[Any],
        arg_idx: int,
    ) -> tuple[dict[str, Any], int]:
        """Build a single MongoDB filter clause from a predicate.

        Returns:
            A tuple of ``(filter_dict, new_arg_index)``.
        """
        op = predicate.operator

        if op == "eq":
            return {field_name: args[arg_idx]}, arg_idx + 1
        if op == "not":
            return {field_name: {"$ne": args[arg_idx]}}, arg_idx + 1
        if op == "gt":
            return {field_name: {"$gt": args[arg_idx]}}, arg_idx + 1
        if op == "gte":
            return {field_name: {"$gte": args[arg_idx]}}, arg_idx + 1
        if op == "lt":
            return {field_name: {"$lt": args[arg_idx]}}, arg_idx + 1
        if op == "lte":
            return {field_name: {"$lte": args[arg_idx]}}, arg_idx + 1
        if op == "like":
            # Translate SQL LIKE wildcards per-character: ``%`` -> ``.*`` and
            # ``_`` -> ``.``; every other character is regex-escaped. (Note:
            # ``re.escape`` does NOT escape ``%``/``_``, so the previous
            # ``.replace(r"\%", ...)`` calls were dead code and never matched.)
            parts: list[str] = []
            for char in str(args[arg_idx]):
                if char == "%":
                    parts.append(".*")
                elif char == "_":
                    parts.append(".")
                else:
                    parts.append(re.escape(char))
            return {field_name: {"$regex": "".join(parts)}}, arg_idx + 1
        if op == "containing":
            escaped = re.escape(str(args[arg_idx]))
            return {field_name: {"$regex": f".*{escaped}.*", "$options": "i"}}, arg_idx + 1
        if op == "in":
            return {field_name: {"$in": args[arg_idx]}}, arg_idx + 1
        if op == "between":
            return {field_name: {"$gte": args[arg_idx], "$lte": args[arg_idx + 1]}}, arg_idx + 2
        if op == "is_null":
            return {field_name: None}, arg_idx
        if op == "is_not_null":
            return {field_name: {"$ne": None}}, arg_idx

        raise ValueError(f"Unknown operator: {op}")

    def _build_filter(self, parsed: ParsedQuery, args: Sequence[Any]) -> dict[str, Any]:
        """Build a complete MongoDB filter document from parsed predicates."""
        if not parsed.predicates:
            return {}

        clauses: list[dict[str, Any]] = []
        arg_idx = 0
        for predicate in parsed.predicates:
            clause, arg_idx = self._build_clause(
                predicate.field_name,
                predicate,
                args,
                arg_idx,
            )
            clauses.append(clause)

        if len(clauses) == 1:
            return clauses[0]

        # Combine with connectors
        if not parsed.connectors:
            return {"$and": clauses}

        # Check if all connectors are the same type
        all_and = all(c == "and" for c in parsed.connectors)
        all_or = all(c == "or" for c in parsed.connectors)

        if all_and:
            return {"$and": clauses}
        if all_or:
            return {"$or": clauses}

        # Mixed connectors — build nested expression left to right
        result = clauses[0]
        for i, connector in enumerate(parsed.connectors):
            key = "$and" if connector == "and" else "$or"
            result = {key: [result, clauses[i + 1]]}
        return result

    @staticmethod
    def _build_sort(parsed: ParsedQuery) -> list[tuple[str, int]]:
        """Build a pymongo sort specification from parsed order clauses."""
        sort_spec: list[tuple[str, int]] = []
        for order in parsed.order_clauses:
            direction = pymongo.ASCENDING if order.direction == "asc" else pymongo.DESCENDING
            sort_spec.append((order.field_name, direction))
        return sort_spec
