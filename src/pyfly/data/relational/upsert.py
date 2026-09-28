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
"""Upserts and conditional inserts on Core tables, dispatched on the dialect.

Every backend spells "insert, or update the row that has this key" differently, and a hand-written
``ON CONFLICT`` fails with a syntax error on MySQL, MariaDB, SQL Server and Oracle. The helpers here take an
``AsyncSession`` (a unit of work's, from ``infrastructure_unit()``) or an ``AsyncConnection`` and a Core
:class:`~sqlalchemy.Table`, and send what the executor's dialect understands:

- :func:`upsert` inserts the row, or updates the existing row with the same key (only when *where* holds);
- :func:`insert_if_absent` inserts the row unless one with the same key exists (or, with *replace_where*,
  unless that row is still valid: an expired cache entry or lease is replaced) and returns whether it wrote;
- :func:`take_over` updates the row with the key when *where* holds, and returns whether it did.

====================  ==========================================  ==============================================
Dialect               ``upsert``                                  ``insert_if_absent``
====================  ==========================================  ==============================================
PostgreSQL, SQLite    ``INSERT ... ON CONFLICT (key) DO UPDATE``  ``INSERT ... ON CONFLICT DO NOTHING`` (or
                      (``WHERE``): one statement                  ``DO UPDATE ... WHERE`` *replace_where*): one
                                                                  statement
MySQL, MariaDB        ``INSERT ... ON DUPLICATE KEY UPDATE``;     :func:`take_over` with *replace_where*, then
                      with *where*, a no-op one then a            ``INSERT IGNORE``
                      conditional ``UPDATE``
Others (SQL Server,   :func:`portable_upsert`: ``UPDATE``, then   :func:`portable_insert_if_absent`: the
Oracle)               ``INSERT`` in a savepoint, then ``UPDATE``  take-over, then ``INSERT`` in a savepoint
                      again when a concurrent insert won
====================  ==========================================  ==============================================

The portable functions work on every backend (plain ``UPDATE``/``INSERT``/savepoints), so a dialect
without a native form is never broken, only a statement or two slower.

On MySQL and MariaDB, ``INSERT IGNORE`` turns other errors into warnings too (a value too long for its
column is truncated), so a caller validates lengths first. There, two callers racing for one key inside
long transactions can deadlock on InnoDB's locks (one of them gets error 1213): run a conditional insert
that is contended (a lock, a dedupe marker) in a short unit of its own, one statement per unit, as
``LeaseLock`` does.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import Any

from sqlalchemy import Table, and_, bindparam, exists, insert, literal, select
from sqlalchemy import update as sql_update
from sqlalchemy.engine import Connection, Dialect, Engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, AsyncSession
from sqlalchemy.sql.dml import Update
from sqlalchemy.sql.elements import ColumnElement

__all__ = [
    "Executor",
    "Where",
    "backend_name",
    "insert_if_absent",
    "native_conditional_insert",
    "native_upsert",
    "portable_insert_if_absent",
    "portable_upsert",
    "take_over",
    "update_statement",
    "upsert",
]

Executor = AsyncSession | AsyncConnection
"""What the helpers run statements on: a unit of work's session, or a connection."""

Where = Callable[[Any, Any], ColumnElement[bool]]
"""A condition on the existing row and the incoming values: ``where(existing, incoming)``, where
``existing`` is the table's columns and ``incoming`` gives each column's new value (attribute or item
access), e.g. ``lambda existing, incoming: existing.sequence < incoming.sequence``."""

_ON_CONFLICT = frozenset({"postgresql", "sqlite"})
_ON_DUPLICATE_KEY = frozenset({"mysql", "mariadb"})


def backend_name(bind: AsyncSession | AsyncConnection | AsyncEngine | Connection | Engine | Dialect) -> str:
    """The backend of *bind*: ``postgresql``, ``sqlite``, ``mysql``, ``mariadb``, ``mssql``, ``oracle``..."""
    dialect: Dialect
    if isinstance(bind, Dialect):
        dialect = bind
    elif isinstance(bind, AsyncSession):
        dialect = bind.get_bind().dialect
    else:
        dialect = bind.dialect
    return "mariadb" if getattr(dialect, "is_mariadb", False) else str(dialect.name)


def native_upsert(dialect: str, *, conditional: bool = False) -> bool:
    """Whether :func:`upsert` is one statement on *dialect* (PostgreSQL and SQLite; MySQL and MariaDB unless
    it is *conditional*, with a *where*): a caller may then run it as a single-statement unit."""
    return dialect in _ON_CONFLICT or (dialect in _ON_DUPLICATE_KEY and not conditional)


def native_conditional_insert(dialect: str) -> bool:
    """Whether :func:`insert_if_absent` (with or without *replace_where*) is one statement on *dialect*
    (PostgreSQL and SQLite): a caller may then run it as a single-statement unit."""
    return dialect in _ON_CONFLICT


# ---------------------------------------------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------------------------------------------


async def upsert(
    executor: Executor,
    table: Table,
    values: Mapping[str, Any],
    *,
    key: Sequence[str],
    update: Sequence[str] | None = None,
    where: Where | None = None,
) -> None:
    """Insert *values* into *table*, or update the row whose *key* columns match.

    *update* names the columns an existing row gets from *values* (by default every column of *values*
    that is not a key). With *where*, the existing row is updated only when ``where(existing, incoming)``
    holds (a snapshot that only moves forward: ``lambda old, new: old.sequence < new.sequence``).
    """
    columns = _update_columns(values, key, update)
    dialect = backend_name(executor)
    if dialect in _ON_CONFLICT:
        statement = _on_conflict_insert(dialect, table).values(dict(values))
        targets = [table.c[name] for name in key]
        if columns:
            upsert_statement = statement.on_conflict_do_update(
                index_elements=targets,
                set_={name: statement.excluded[name] for name in columns},
                where=where(table.c, statement.excluded) if where is not None else None,
            )
            await executor.execute(upsert_statement)
        else:
            await executor.execute(statement.on_conflict_do_nothing(index_elements=targets))
        return
    if dialect in _ON_DUPLICATE_KEY:
        from sqlalchemy.dialects.mysql import insert as mysql_insert

        duplicate = mysql_insert(table).values(dict(values))
        if columns and where is None:
            assignments = {name: duplicate.inserted[name] for name in columns}
            await executor.execute(duplicate.on_duplicate_key_update(assignments))
            return
        # ON DUPLICATE KEY UPDATE assigns left to right, so a condition over columns it assigns would see
        # the new values: insert (or leave the row alone), then update under the condition.
        await executor.execute(duplicate.on_duplicate_key_update({key[0]: table.c[key[0]]}))
        if columns:
            await executor.execute(update_statement(table, values, key=key, update=columns, where=where))
        return
    await portable_upsert(executor, table, values, key=key, update=update, where=where)


async def insert_if_absent(
    executor: Executor,
    table: Table,
    values: Mapping[str, Any],
    *,
    key: Sequence[str],
    replace_where: ColumnElement[bool] | None = None,
    replace_with: Mapping[str, Any] | None = None,
) -> bool:
    """Insert *values* unless a row with the same *key* exists; return whether this call wrote the row.

    With *replace_where* (a condition on *table*'s columns, the existing row's), an existing row for which
    it holds is replaced by *values* instead: ``table.c.expires_at <= now`` lets an expired entry be taken
    again. *replace_with* overrides some of *values* for that replacement, and may hold SQL expressions over
    the existing row (``{"fence": table.c.fence + 1}``). The native form is one statement (PostgreSQL,
    SQLite: :func:`native_conditional_insert`).
    """
    dialect = backend_name(executor)
    if dialect in _ON_CONFLICT:
        statement = _on_conflict_insert(dialect, table).values(dict(values))
        targets = [table.c[name] for name in key]
        if replace_where is None:
            result = await executor.execute(statement.on_conflict_do_nothing(index_elements=targets))
        else:
            columns = _update_columns(values, key, None) or list(key)
            overrides = replace_with or {}
            result = await executor.execute(
                statement.on_conflict_do_update(
                    index_elements=targets,
                    set_={name: overrides[name] if name in overrides else statement.excluded[name] for name in columns},
                    where=replace_where,
                )
            )
        return _rowcount(result) == 1
    if dialect in _ON_DUPLICATE_KEY:
        from sqlalchemy.dialects.mysql import insert as mysql_insert

        if replace_where is not None and await take_over(
            executor, table, {**values, **(replace_with or {})}, key=key, where=replace_where
        ):
            return True
        result = await executor.execute(mysql_insert(table).values(dict(values)).prefix_with("IGNORE"))
        return _rowcount(result) == 1
    return await portable_insert_if_absent(
        executor, table, values, key=key, replace_where=replace_where, replace_with=replace_with
    )


async def take_over(
    executor: Executor,
    table: Table,
    values: Mapping[str, Any],
    *,
    key: Sequence[str],
    where: ColumnElement[bool],
) -> bool:
    """Update the row whose *key* columns match *values* with the other *values*, only when *where* (a
    condition on the existing row) holds; return whether a row was updated. One portable statement.

    The values of the non-key columns may be SQL expressions (``table.c.fence + 1``)."""
    columns = {name: value for name, value in values.items() if name not in key}
    statement = sql_update(table).where(*_matching(table, values, key)).where(where).values(columns)
    result = await executor.execute(statement)
    return _rowcount(result) == 1


# ---------------------------------------------------------------------------------------------------------
# The portable forms (any backend)
# ---------------------------------------------------------------------------------------------------------


def update_statement(
    table: Table,
    values: Mapping[str, Any],
    *,
    key: Sequence[str],
    update: Sequence[str] | None = None,
    where: Where | None = None,
) -> Update:
    """The ``UPDATE`` of the row whose *key* columns match *values*: it sets the *update* columns (by
    default every non-key column of *values*), only where ``where(existing, incoming)`` holds. A plain
    statement every backend runs; the portable forms and the MySQL conditional upsert send it."""
    columns = _update_columns(values, key, update)
    statement = sql_update(table).where(*_matching(table, values, key)).values({name: values[name] for name in columns})
    if where is not None:
        statement = statement.where(where(table.c, _Incoming(table, values)))
    return statement


async def portable_upsert(
    executor: Executor,
    table: Table,
    values: Mapping[str, Any],
    *,
    key: Sequence[str],
    update: Sequence[str] | None = None,
    where: Where | None = None,
) -> None:
    """:func:`upsert` with plain statements: ``UPDATE``; when no row matched, ``INSERT`` in a savepoint;
    when that insert meets a duplicate key (a concurrent insert won), ``UPDATE`` once more."""
    columns = _update_columns(values, key, update)
    if columns and await _update_existing(executor, table, values, key, columns, where):
        return
    if await _insert_unless_duplicate(executor, table, values, key):
        return
    if columns:
        await _update_existing(executor, table, values, key, columns, where)


async def portable_insert_if_absent(
    executor: Executor,
    table: Table,
    values: Mapping[str, Any],
    *,
    key: Sequence[str],
    replace_where: ColumnElement[bool] | None = None,
    replace_with: Mapping[str, Any] | None = None,
) -> bool:
    """:func:`insert_if_absent` with plain statements: the :func:`take_over` of a row *replace_where* lets
    go (with *values* and *replace_with* over them), then ``INSERT`` in a savepoint, where a duplicate key
    means the row was there."""
    if replace_where is not None and await take_over(
        executor, table, {**values, **(replace_with or {})}, key=key, where=replace_where
    ):
        return True
    return await _insert_unless_duplicate(executor, table, values, key)


# ---------------------------------------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------------------------------------


class _Incoming:
    """The incoming values of the portable and MySQL forms, as typed bound parameters (what
    ``excluded``/``inserted`` are to ``ON CONFLICT``/``ON DUPLICATE KEY``)."""

    def __init__(self, table: Table, values: Mapping[str, Any]) -> None:
        self._table = table
        self._values = values

    def __getitem__(self, name: str) -> ColumnElement[Any]:
        return literal(self._values[name], type_=self._table.c[name].type)

    def __getattr__(self, name: str) -> ColumnElement[Any]:
        if name.startswith("_"):
            raise AttributeError(name)
        try:
            return self[name]
        except KeyError:
            raise AttributeError(name) from None


def _rowcount(result: Any) -> int:
    """The rows a DML statement matched (``CursorResult.rowcount``)."""
    return int(result.rowcount)


def _on_conflict_insert(dialect: str, table: Table) -> Any:
    if dialect == "postgresql":
        from sqlalchemy.dialects.postgresql import insert as postgresql_insert

        return postgresql_insert(table)
    from sqlalchemy.dialects.sqlite import insert as sqlite_insert

    return sqlite_insert(table)


def _update_columns(values: Mapping[str, Any], key: Sequence[str], update: Sequence[str] | None) -> list[str]:
    missing = [name for name in key if name not in values]
    if not key or missing:
        raise ValueError(f"The key columns {list(key)} must all be in the values (missing: {missing})")
    if update is None:
        return [name for name in values if name not in key]
    unknown = [name for name in update if name not in values]
    if unknown:
        raise ValueError(f"The update columns {unknown} have no value")
    return list(update)


def _matching(table: Table, values: Mapping[str, Any], key: Sequence[str]) -> list[ColumnElement[bool]]:
    return [table.c[name] == bindparam(None, values[name], type_=table.c[name].type) for name in key]


async def _update_existing(
    executor: Executor,
    table: Table,
    values: Mapping[str, Any],
    key: Sequence[str],
    columns: Sequence[str],
    where: Where | None,
) -> bool:
    result = await executor.execute(update_statement(table, values, key=key, update=columns, where=where))
    return _rowcount(result) >= 1


async def _insert_unless_duplicate(
    executor: Executor, table: Table, values: Mapping[str, Any], key: Sequence[str]
) -> bool:
    """``INSERT`` in a savepoint (a failure leaves the enclosing transaction healthy on every backend);
    ``False`` when the row with the key exists, any other integrity failure is raised."""
    try:
        async with executor.begin_nested():
            await executor.execute(insert(table).values(dict(values)))
    except IntegrityError:
        present = await executor.execute(select(exists().where(and_(*_matching(table, values, key)))))
        if present.scalar():
            return False
        raise
    return True
