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
"""

from __future__ import annotations

from collections.abc import Callable, Coroutine, Sequence
from types import SimpleNamespace
from typing import Any, TypeVar, cast, get_args, get_origin

from sqlalchemy import Select, delete, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from pyfly.data.projection import is_projection, projection_fields
from pyfly.data.query_parser import FieldPredicate, ParsedQuery

T = TypeVar("T")


class QueryMethodCompiler:
    """Compile a :class:`ParsedQuery` into an async callable.

    The returned callable has the signature::

        async def query_fn(session: AsyncSession, *args: Any) -> R

    where ``R`` depends on the prefix:

    * ``find_by``   -> ``list[T]``
    * ``count_by``  -> ``int``
    * ``exists_by`` -> ``bool``
    * ``delete_by`` -> ``int``  (number of deleted rows)
    """

    def compile(
        self,
        parsed: ParsedQuery,
        entity: type[T],
        *,
        return_type: type | None = None,
    ) -> Callable[..., Coroutine[Any, Any, Any]]:
        """Dispatch to the correct compile method based on prefix."""
        dispatch: dict[str, Callable[..., Callable[..., Coroutine[Any, Any, Any]]]] = {
            "find_by": self._compile_find,
            "count_by": self._compile_count,
            "exists_by": self._compile_exists,
            "delete_by": self._compile_delete,
        }
        builder = dispatch.get(parsed.prefix)
        if builder is None:
            raise ValueError(f"Unknown prefix: {parsed.prefix}")
        if parsed.prefix == "find_by":
            return builder(parsed, entity, return_type=return_type)
        return builder(parsed, entity)

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

        if proj_type is not None:
            fields = projection_fields(proj_type)
            columns = [getattr(entity, f) for f in fields]

            async def _execute_projected(session: AsyncSession, *args: Any) -> list[Any]:
                stmt = select(*columns)
                stmt = self._apply_where(stmt, parsed, entity, args)
                stmt = self._apply_order(stmt, parsed, entity)
                result = await session.execute(stmt)
                rows = result.all()
                return [SimpleNamespace(**dict(zip(fields, row, strict=False))) for row in rows]

            return _execute_projected
        else:

            async def _execute(session: AsyncSession, *args: Any) -> list[T]:
                stmt = select(entity)
                stmt = self._apply_where(stmt, parsed, entity, args)
                stmt = self._apply_order(stmt, parsed, entity)
                result = await session.execute(stmt)
                return list(result.scalars().all())

            return _execute

    # ------------------------------------------------------------------
    # count_by
    # ------------------------------------------------------------------

    def _compile_count(
        self,
        parsed: ParsedQuery,
        entity: type[T],
    ) -> Callable[..., Coroutine[Any, Any, int]]:
        async def _execute(session: AsyncSession, *args: Any) -> int:
            stmt = select(func.count()).select_from(entity)
            stmt = self._apply_where(stmt, parsed, entity, args)
            result = await session.execute(stmt)
            return cast(int, result.scalar_one())

        return _execute

    # ------------------------------------------------------------------
    # exists_by
    # ------------------------------------------------------------------

    def _compile_exists(
        self,
        parsed: ParsedQuery,
        entity: type[T],
    ) -> Callable[..., Coroutine[Any, Any, bool]]:
        async def _execute(session: AsyncSession, *args: Any) -> bool:
            stmt = select(func.count()).select_from(entity)
            stmt = self._apply_where(stmt, parsed, entity, args)
            result = await session.execute(stmt)
            return cast(bool, result.scalar_one() > 0)

        return _execute

    # ------------------------------------------------------------------
    # delete_by
    # ------------------------------------------------------------------

    def _compile_delete(
        self,
        parsed: ParsedQuery,
        entity: type[T],
    ) -> Callable[..., Coroutine[Any, Any, int]]:
        async def _execute(session: AsyncSession, *args: Any) -> int:
            stmt = delete(entity)
            stmt = self._apply_where(stmt, parsed, entity, args)
            result = await session.execute(stmt)
            return result.rowcount or 0  # type: ignore[attr-defined]

        return _execute

    # ------------------------------------------------------------------
    # Shared helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _build_clause(col: Any, predicate: FieldPredicate, args: Sequence[Any], arg_idx: int) -> tuple[Any, int]:
        """Build a single SQLAlchemy clause from a predicate and consume args.

        Returns:
            A tuple of ``(clause, new_arg_index)`` so the caller knows
            how many positional arguments were consumed.
        """
        op = predicate.operator

        if op == "eq":
            return col == args[arg_idx], arg_idx + 1
        if op == "not":
            return col != args[arg_idx], arg_idx + 1
        if op == "gt":
            return col > args[arg_idx], arg_idx + 1
        if op == "gte":
            return col >= args[arg_idx], arg_idx + 1
        if op == "lt":
            return col < args[arg_idx], arg_idx + 1
        if op == "lte":
            return col <= args[arg_idx], arg_idx + 1
        if op == "like":
            return col.like(args[arg_idx]), arg_idx + 1
        if op == "containing":
            return col.like(f"%{args[arg_idx]}%"), arg_idx + 1
        if op == "in":
            return col.in_(args[arg_idx]), arg_idx + 1
        if op == "between":
            return col.between(args[arg_idx], args[arg_idx + 1]), arg_idx + 2
        if op == "is_null":
            return col.is_(None), arg_idx
        if op == "is_not_null":
            return col.isnot(None), arg_idx

        raise ValueError(f"Unknown operator: {op}")

    def _apply_where(
        self,
        stmt: Any,
        parsed: ParsedQuery,
        entity: type[T],
        args: Sequence[Any],
    ) -> Any:
        """Apply WHERE clauses from parsed predicates to a SELECT or DELETE statement."""
        if not parsed.predicates:
            return stmt

        clauses, _ = self._collect_clauses(parsed, entity, args)

        # Combine clauses with the connectors
        combined = clauses[0]
        for i, connector in enumerate(parsed.connectors):
            combined = combined & clauses[i + 1] if connector == "and" else or_(combined, clauses[i + 1])

        return stmt.where(combined)

    def _collect_clauses(
        self,
        parsed: ParsedQuery,
        entity: type[T],
        args: Sequence[Any],
    ) -> tuple[list[Any], int]:
        """Iterate over predicates and build SQLAlchemy column expressions.

        Returns:
            A tuple of ``(clause_list, final_arg_index)``.
        """
        clauses: list[Any] = []
        arg_idx = 0
        for predicate in parsed.predicates:
            col = getattr(entity, predicate.field_name)
            clause, arg_idx = self._build_clause(col, predicate, args, arg_idx)
            clauses.append(clause)
        return clauses, arg_idx

    @staticmethod
    def _apply_order(stmt: Select[Any], parsed: ParsedQuery, entity: type[T]) -> Select[Any]:
        """Apply ORDER BY clauses to a SELECT statement."""
        for order in parsed.order_clauses:
            col = getattr(entity, order.field_name)
            stmt = stmt.order_by(col.desc()) if order.direction == "desc" else stmt.order_by(col.asc())
        return stmt
