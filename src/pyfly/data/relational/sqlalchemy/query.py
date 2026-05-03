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
"""SQLAlchemy ``@Query`` executor and JPQL transpiler.

Provides :class:`QueryExecutor` to compile ``@query``-decorated repository
methods into executable async callables backed by SQLAlchemy.

The :func:`query` decorator itself is backend-neutral and lives in
:mod:`pyfly.data.query`.  It is re-exported here for backward compatibility::

    from pyfly.data.relational.sqlalchemy.query import query   # still works
    from pyfly.data.query import query                         # preferred

Usage::

    from pyfly.data.query import query

    class UserRepository(Repository[User]):

        @query("SELECT u FROM User u WHERE u.email LIKE :pattern AND u.active = true")
        async def find_active_by_email_pattern(self, pattern: str) -> list[User]: ...

        @query("SELECT COUNT(u) FROM User u WHERE u.role = :role")
        async def count_by_role(self, role: str) -> int: ...

        @query("SELECT * FROM users WHERE email = :email", native=True)
        async def find_by_email_native(self, email: str) -> list[User]: ...
"""

from __future__ import annotations

import re
from collections.abc import Callable, Coroutine
from typing import Any, TypeVar

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from pyfly.data.query import query

T = TypeVar("T")

__all__ = ["QueryExecutor", "query"]


class QueryExecutor:
    """Execute ``@query``-decorated methods against SQLAlchemy.

    This class is used by the ``RepositoryBeanPostProcessor`` to wire up
    query methods at startup time.  Given a decorated method and an entity
    type it produces an async callable that, when invoked with an
    :class:`~sqlalchemy.ext.asyncio.AsyncSession` and keyword arguments,
    executes the query and returns mapped results.
    """

    def compile_query_method(
        self,
        method: Callable[..., Any],
        entity: type[T],
    ) -> Callable[..., Coroutine[Any, Any, Any]]:
        """Compile a ``@query``-decorated method into an executable async function.

        Args:
            method: The decorated method (must have ``__pyfly_query__``).
            entity: The entity type used for result mapping and JPQL
                    transpilation.

        Returns:
            An async function with signature
            ``(session: AsyncSession, **kwargs) -> Any`` that returns:

            - ``int`` for ``SELECT COUNT(...)`` queries
            - ``bool`` for queries containing ``EXISTS``
            - ``list[entity]`` for all other SELECT queries

        Raises:
            AttributeError: If *method* was not decorated with :func:`query`.
        """
        if not hasattr(method, "__pyfly_query__"):
            raise AttributeError(f"{method} is not decorated with @query (missing __pyfly_query__)")

        sql: str = method.__pyfly_query__
        is_native: bool = method.__pyfly_query_native__  # type: ignore[attr-defined]

        if not is_native:
            sql = self._transpile_jpql(sql, entity)

        # Determine return type from query shape
        is_count = sql.strip().upper().startswith("SELECT COUNT")
        is_exists = bool(re.search(r"\bEXISTS\s*\(", sql, re.IGNORECASE))

        async def _execute(session: AsyncSession, **kwargs: Any) -> Any:
            result = await session.execute(text(sql), kwargs)
            if is_count:
                return result.scalar_one()
            if is_exists:
                return result.scalar_one() > 0
            # Default: return entity list
            rows = result.fetchall()
            return [entity(**dict(row._mapping)) for row in rows]

        return _execute

    @staticmethod
    def _transpile_jpql(jpql: str, entity: type) -> str:
        """Lightweight JPQL-to-SQL transpiler.

        Transforms JPQL-like query strings into valid SQL by:

        1. Replacing ``FROM Entity alias`` with ``FROM <tablename>``.
        2. Replacing ``SELECT alias`` with ``SELECT *``.
        3. Replacing ``COUNT(alias)`` with ``COUNT(*)``.
        4. Stripping entity alias prefixes (``alias.field`` -> ``field``).
        5. Converting boolean literals ``true`` / ``false`` to ``1`` / ``0``.

        Examples::

            "SELECT u FROM User u WHERE u.email = :email"
            ->  "SELECT * FROM users WHERE email = :email"

            "SELECT COUNT(u) FROM User u WHERE u.role = :role"
            ->  "SELECT COUNT(*) FROM users WHERE role = :role"
        """
        # Get table name from entity
        table_name = getattr(entity, "__tablename__", entity.__name__.lower() + "s")
        entity_name = re.escape(entity.__name__)

        # Find the alias (e.g., "u" in "FROM User u") — anchored to the
        # entity class name so that SQL keywords like WHERE are never
        # mistakenly captured as the alias.
        alias_match = re.search(rf"FROM\s+{entity_name}\s+(\w+)", jpql, re.IGNORECASE)
        alias = alias_match.group(1) if alias_match else None

        sql = jpql

        # Replace "FROM Entity alias" with "FROM table_name" (strip the alias
        # identifier since it is no longer needed after alias references are
        # resolved below).
        sql = re.sub(
            rf"FROM\s+{entity_name}\s+\w+",
            f"FROM {table_name}",
            sql,
            count=1,
            flags=re.IGNORECASE,
        )

        # Replace "SELECT alias" (plain entity select) with "SELECT *"
        if alias:
            sql = re.sub(
                rf"SELECT\s+{re.escape(alias)}\b",
                "SELECT *",
                sql,
                flags=re.IGNORECASE,
            )

        # Replace "SELECT COUNT(alias)" with "SELECT COUNT(*)"
        if alias:
            sql = re.sub(
                rf"SELECT\s+COUNT\s*\(\s*{re.escape(alias)}\s*\)",
                "SELECT COUNT(*)",
                sql,
                flags=re.IGNORECASE,
            )

        # Replace alias.field references with just field
        if alias:
            sql = re.sub(rf"{re.escape(alias)}\.", "", sql)

        # Replace "= true" / "= false" with proper SQL
        sql = re.sub(r"=\s*true\b", "= 1", sql, flags=re.IGNORECASE)
        sql = re.sub(r"=\s*false\b", "= 0", sql, flags=re.IGNORECASE)

        return sql
