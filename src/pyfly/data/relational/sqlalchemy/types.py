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
"""Portable column types for PyFly entities.

:class:`UtcDateTime` stores an instant, the same way on every backend. A plain
``DateTime(timezone=True)`` does not: PostgreSQL keeps the instant (``TIMESTAMP WITH TIME ZONE``) and
reads a naive value as the *host's* local time, while SQLite, MySQL and MariaDB compile it to
``DATETIME``, drop the offset of an aware value (``12:00+02:00`` is stored as ``12:00``), read back naive
values, and on MySQL/MariaDB keep whole seconds only (MySQL rounds into the future, MariaDB truncates).
The same entity then compares, sorts and filters differently per backend, and ``updated_at -
created_at`` raises ``TypeError`` wherever one side was loaded and the other stamped.

``UtcDateTime`` normalizes on the way in and on the way out:

- **bind**: an aware value is converted to UTC; a naive value is taken as UTC (or rejected with
  ``strict=True``). Backends without a time-zone type get the naive UTC wall time, so their stored
  values sort and compare as instants.
- **result**: every value comes back aware, in UTC. On asyncpg the driver already returns a
  ``timestamptz`` as aware UTC (whatever the server's ``TimeZone``), so only a naive value is processed
  there.
- **DDL**: ``TIMESTAMP WITH TIME ZONE`` on PostgreSQL and Oracle, ``DATETIMEOFFSET`` on SQL Server,
  ``DATETIME`` on SQLite (all unchanged from ``DateTime(timezone=True)``, except Oracle, which got a
  ``DATE`` without fractional seconds), and ``DATETIME(6)`` on MySQL and MariaDB (microseconds).

A ``datetime`` compared with a ``UtcDateTime`` column goes through the same bind processing, so a derived
``find_by_created_at_between(lo, hi)`` with ``+02:00`` parameters compares instants on every backend. Any
other value is bound as it is beside a plain ``DateTime``: the ``timedelta`` of ``created_at +
timedelta(days=1)`` as an ``Interval``, a string as a string. That SQL interval arithmetic runs on
PostgreSQL, where the ``Interval`` is native. SQLAlchemy has no date arithmetic for SQLite, MySQL and
MariaDB: there ``created_at + timedelta(days=1) > now`` compiles to a numeric addition and matches the
wrong rows. Shift the parameter instead, ``created_at > now - timedelta(days=1)``: it is portable, and it
leaves the column bare for an index. Raw ``text()`` SQL does not know the column type and gets no
normalization.

``BaseEntity.created_at``/``updated_at`` and ``SoftDeleteMixin.deleted_at`` use it. Declare it on your own
columns (``mapped_column(UtcDateTime())``), or make it the type of every ``Mapped[datetime]`` of your
models before they are defined::

    Base.registry.update_type_annotation_map({datetime: UtcDateTime()})

Existing MySQL/MariaDB tables keep ``DATETIME`` until migrated
(``ALTER TABLE t MODIFY created_at DATETIME(6) NOT NULL``). The values the framework stamped there are
already UTC wall times, so they read back correctly as they are.

On PostgreSQL the type expects ``TIMESTAMP WITH TIME ZONE``. A ``timestamp without time zone`` column
that adopts it (the ``update_type_annotation_map`` line above makes every ``Mapped[datetime]`` one) reads
back its wall times as UTC, but the server converts the aware values bound to it, and compares with it, in
the session's ``TimeZone``: under a non-UTC ``TimeZone`` the stored instants drift. Migrate such a column
first, reading its values as the UTC wall times they are:
``ALTER TABLE t ALTER COLUMN c TYPE timestamptz USING c AT TIME ZONE 'UTC'``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import DateTime
from sqlalchemy.engine import Dialect
from sqlalchemy.sql.operators import OperatorType
from sqlalchemy.types import TypeDecorator, TypeEngine

_NATIVE_TIME_ZONE_DIALECTS = frozenset({"postgresql", "oracle", "mssql"})
"""Dialects whose column type keeps the offset, so a bound value stays aware."""

_MYSQL_FAMILY = frozenset({"mysql", "mariadb"})


def to_utc(value: datetime, *, strict: bool = False) -> datetime:
    """*value* as an aware UTC ``datetime``.

    An aware value is converted to UTC. A naive value is taken as UTC, or rejected with ``ValueError``
    when *strict* is true (a naive ``datetime`` usually means the caller forgot the zone).
    """
    if value.tzinfo is None or value.utcoffset() is None:
        if strict:
            raise ValueError(
                f"naive datetime {value.isoformat()} for a strict UtcDateTime column: pass an aware value "
                "(datetime.now(UTC), or a value with its offset)"
            )
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


class UtcDateTime(TypeDecorator[datetime]):
    """An instant with microsecond precision, aware UTC in Python on every backend (module documentation).

    Args:
        strict: reject naive values with ``ValueError`` instead of taking them as UTC.
    """

    impl = DateTime(timezone=True)
    cache_ok = True

    def __init__(self, *, strict: bool = False) -> None:
        super().__init__()
        self.strict = strict

    @property
    def python_type(self) -> type[datetime]:
        return datetime

    def load_dialect_impl(self, dialect: Dialect) -> TypeEngine[Any]:
        if dialect.name in _MYSQL_FAMILY:
            from sqlalchemy.dialects import mysql

            return dialect.type_descriptor(mysql.DATETIME(fsp=6))
        if dialect.name == "oracle":
            from sqlalchemy.dialects import oracle

            return dialect.type_descriptor(oracle.TIMESTAMP(timezone=True))
        return dialect.type_descriptor(DateTime(timezone=True))

    def coerce_compared_value(self, op: OperatorType | None, value: Any) -> Any:
        """The type a literal beside the column is bound with: this type for a ``datetime`` (so a ``+02:00``
        parameter is normalized like a written value), and the plain ``DateTime``'s choice for anything else:
        an ``Interval`` for the ``timedelta`` of ``created_at + timedelta(days=1)``, a ``String`` for a string,
        a ``Date`` for a date. Bound as this type, a ``timedelta`` made ``timestamptz + timestamptz`` on
        PostgreSQL and a string raised ``TypeError`` on SQLite."""
        if isinstance(value, datetime):
            return self
        return self.impl_instance.coerce_compared_value(op, value)

    def process_bind_param(self, value: Any, dialect: Dialect) -> Any:
        if not isinstance(value, datetime):
            return value
        instant = to_utc(value, strict=self.strict)
        if dialect.name in _NATIVE_TIME_ZONE_DIALECTS:
            return instant
        # DATETIME has no offset: store the UTC wall time, so stored values order as instants.
        return instant.replace(tzinfo=None)

    def process_result_value(self, value: Any, dialect: Dialect) -> datetime | None:
        if isinstance(value, datetime):
            return to_utc(value)
        unchanged: datetime | None = value  # NULL
        return unchanged

    def result_processor(self, dialect: Dialect, coltype: Any) -> Any:
        """The implementation type's result processing, then :meth:`process_result_value`. It runs for every
        datetime of every row read, so the common cases take a short path: on asyncpg an aware value is
        returned as it is (the driver returns ``timestamptz`` as aware UTC, whatever the server's
        ``TimeZone``) and only a naive one (a ``timestamp without time zone`` column) is tagged UTC, and on
        SQLite the text the bind stored (the UTC wall time) is parsed straight to an aware value."""
        impl_processor = self.impl_instance.result_processor(dialect, coltype)
        to_aware = self.process_result_value
        if dialect.driver == "asyncpg" and impl_processor is None:

            def process_asyncpg(value: Any) -> Any:
                if value is None or value.tzinfo is not None:
                    return value
                return to_aware(value, dialect)

            return process_asyncpg

        def process(value: Any) -> Any:
            if impl_processor is not None:
                value = impl_processor(value)
            return to_aware(value, dialect)

        if dialect.name != "sqlite":
            return process

        def process_sqlite_text(value: Any) -> Any:
            if value.__class__ is str:
                try:
                    return datetime.fromisoformat(value + "+00:00")
                except ValueError:
                    pass  # stored with an offset of its own, or in another format: the general path
            return process(value)

        return process_sqlite_text

    def __repr__(self) -> str:
        return "UtcDateTime(strict=True)" if self.strict else "UtcDateTime()"
