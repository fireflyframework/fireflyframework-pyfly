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
"""Statement helpers shared by the relational repositories and the derived-query compiler.

Every helper is portable by construction: it renders what each dialect accepts, so SQL Server and Oracle are
not broken, and a PostgreSQL feature is only ever an accelerator.

- **Exists probes.** :func:`exists_probe` is ``SELECT 1 ... WHERE ... LIMIT 1`` (``TOP 1`` on SQL Server,
  ``FETCH FIRST 1 ROWS ONLY`` on Oracle), never a ``COUNT`` over every match and never a bare
  ``SELECT EXISTS``, which Oracle and SQL Server do not accept; :func:`exists` runs one.
- **IN lists.** :func:`in_criteria` gives one criterion per chunk of the dialect's limit (Oracle takes 1000
  values per list, SQL Server about 2100 parameters per statement, SQLite 32766, PostgreSQL 32767): run one
  statement per chunk in the same unit of work and concatenate the rows or add the counts. On PostgreSQL a
  single-column list is one ``= ANY(:array)`` bind, the same statement text for every length, which asyncpg's
  prepared-statement cache keeps; elsewhere a list is padded to the next power of two with its last value
  (Hibernate's ``in_clause_parameter_padding``), so a handful of texts cover every length. Composite keys are
  row values (``(a, b) IN ((?, ?), ...)``), or an OR of ANDs on SQL Server, which has no row values.
- **Entity results.** :func:`unique_entities` consumes an entity result through ``unique()``, which a joined
  eager load of a collection requires; :func:`stream_safe` loads such collections with ``selectin`` for a
  streamed result instead, since a streamed result cannot be made unique.
- **Ordering.** :func:`order_expressions` renders a :class:`~pyfly.data.pageable.Sort` with its NULL
  placement (native ``NULLS FIRST``/``NULLS LAST`` on PostgreSQL, SQLite and Oracle, an ``IS NULL`` key first
  on MySQL, MariaDB and SQL Server) and case folding; :func:`primary_key_orders` is the tie-breaker every
  paging path appends, which makes pages deterministic and is what SQL Server requires for ``OFFSET``.
- **Delete strategy.** :func:`bulk_delete_safe` says whether a bulk ``DELETE`` does what deleting entity by
  entity does: no ORM cascade, version column, inheritance or delete listener.
- **Fetch plans and locks.** :func:`loader_options` turns a repository's ``load=`` argument into loader
  options, and :class:`LockMode` is a pessimistic lock (``SELECT ... FOR UPDATE``).
"""

from __future__ import annotations

import enum
import sqlite3
from collections.abc import Iterable, Iterator, Sequence
from typing import Any, TypeVar

from sqlalchemy import ARRAY, Select, and_, any_, bindparam, case, func, literal_column, or_, select, tuple_
from sqlalchemy import inspect as sa_inspect
from sqlalchemy.engine import Dialect
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapper, QueryableAttribute, RelationshipProperty, Session, defaultload, selectinload
from sqlalchemy.orm.interfaces import MANYTOONE
from sqlalchemy.sql.base import ExecutableOption
from sqlalchemy.sql.elements import ColumnElement

from pyfly.data.pageable import NullHandling, Sort
from pyfly.data.property_resolver import InvalidPropertyError, PropertyResolver
from pyfly.data.relational.datasource_registry import DataSourceCapabilities

__all__ = [
    "FetchPlan",
    "FetchStep",
    "LockMode",
    "backend_name",
    "bulk_delete_safe",
    "chunked",
    "exists",
    "exists_probe",
    "in_criteria",
    "in_list_limit",
    "loader_options",
    "order_expressions",
    "padded",
    "primary_key_orders",
    "stream_safe",
    "unique_entities",
]

T = TypeVar("T")

_NATIVE_NULL_ORDERING = frozenset({"postgresql", "oracle", "sqlite"})
"""Dialects that render ``NULLS FIRST``/``NULLS LAST`` (SQLite from 3.30)."""

_SQLITE_NULLS_ORDERING = sqlite3.sqlite_version_info >= (3, 30, 0)

_NO_ROW_VALUE_IN = frozenset({"mssql"})
"""Dialects without ``(a, b) IN ((...), ...)``: composite keys become an OR of ANDs there."""

_IN_LIMITS: dict[str, int] = {}

_ONE: ColumnElement[int] = literal_column("1")
_ZERO: ColumnElement[int] = literal_column("0")


# ---------------------------------------------------------------------------------------------------------
# Dialects
# ---------------------------------------------------------------------------------------------------------


def backend_name(dialect: Dialect) -> str:
    """The backend of *dialect*: its name, and ``mariadb`` for a MySQL dialect talking to MariaDB."""
    return "mariadb" if getattr(dialect, "is_mariadb", False) else str(dialect.name)


def in_list_limit(dialect: Dialect) -> int:
    """The most values one IN list may bind on *dialect* (``DataSourceCapabilities.max_in_params``)."""
    name = backend_name(dialect)
    limit = _IN_LIMITS.get(name)
    if limit is None:
        limit = _IN_LIMITS[name] = DataSourceCapabilities.of(dialect).max_in_params
    return limit


def dialect_of(session: AsyncSession | Session) -> Dialect:
    """The dialect of the connection *session* runs its statements on."""
    sync_session = session.sync_session if isinstance(session, AsyncSession) else session
    return sync_session.get_bind().dialect


# ---------------------------------------------------------------------------------------------------------
# IN lists
# ---------------------------------------------------------------------------------------------------------


def chunked(values: Sequence[T], size: int) -> list[list[T]]:
    """*values* in consecutive chunks of at most *size*."""
    return [list(values[start : start + size]) for start in range(0, len(values), size)]


def padded(values: Sequence[T], limit: int) -> list[T]:
    """*values* padded to the next power of two (at most *limit*) by repeating the last value.

    A repeated value changes nothing in an IN list, and the statement text then only depends on the power of
    two, so a statement cache keeps a few texts instead of one per length.
    """
    count = len(values)
    if count <= 1:
        return list(values)
    size = max(count, min(1 << (count - 1).bit_length(), limit))
    return [*values, *([values[-1]] * (size - count))]


def in_criteria(
    columns: Sequence[ColumnElement[Any] | QueryableAttribute[Any]],
    values: Sequence[Any],
    dialect: Dialect,
) -> list[ColumnElement[bool]]:
    """Criteria that match rows whose *columns* equal one of *values*, one per chunk (module documentation).

    *values* are scalars for one column and tuples (one item per column, in order) for several. Run one
    statement per criterion; an empty *values* gives no criterion at all (nothing to match, nothing to run).
    """
    if not values:
        return []
    name = backend_name(dialect)
    if len(columns) == 1:
        (column,) = columns
        if name == "postgresql":
            array = bindparam(None, list(values), type_=ARRAY(column.type))
            return [column == any_(array)]
        limit = in_list_limit(dialect)
        return [column.in_(padded(chunk, limit)) for chunk in chunked(values, limit)]
    width = len(columns)
    limit = max(1, in_list_limit(dialect) // width)
    rows = [tuple(value) for value in values]
    if any(len(row) != width for row in rows):
        raise ValueError(f"Each key needs {width} values, one per primary-key column")
    if name in _NO_ROW_VALUE_IN:
        return [
            or_(*(and_(*(column == item for column, item in zip(columns, row, strict=True))) for row in chunk))
            for chunk in chunked(rows, limit)
        ]
    return [tuple_(*columns).in_(padded(chunk, limit)) for chunk in chunked(rows, limit)]


# ---------------------------------------------------------------------------------------------------------
# Exists probes and entity results
# ---------------------------------------------------------------------------------------------------------


def exists_probe(source: Any, *criteria: Any) -> Select[tuple[int]]:
    """``SELECT 1 FROM source WHERE criteria LIMIT 1``.

    *source* is an entity, a table, or a ``select(Entity)`` whose FROM and WHERE are kept (its columns,
    ORDER BY and loader options are not). Run it with :func:`exists`.
    """
    if isinstance(source, Select):
        probe: Select[tuple[int]] = source.with_only_columns(_ONE, maintain_column_froms=True).order_by(None)
    else:
        probe = select(_ONE).select_from(source)
    return probe.where(*criteria).limit(1)


async def exists(session: AsyncSession, probe: Select[tuple[int]]) -> bool:
    """Run *probe* (:func:`exists_probe`) and tell whether it found a row."""
    return (await session.execute(probe)).scalar() is not None


def unique_entities(result: Any) -> list[Any]:
    """The entities of an ORM result, each once (``unique()``: required by joined eager loads of collections,
    harmless otherwise)."""
    return list(result.unique().scalars().all())


def stream_safe(statement: Select[Any], entity: type) -> Select[Any]:
    """*statement* with every joined eager load of a collection that *entity*'s rows would bring (its own, and
    those behind its joined many-to-ones) turned into a ``selectin`` load, which a streamed result supports:
    the collections of each fetched batch are loaded by one more statement. Unchanged when there is none."""
    options = list(_joined_collections(sa_inspect(entity), None, frozenset()))
    return statement.options(*options) if options else statement


def _joined_collections(mapper: Mapper[Any], path: Any, seen: frozenset[Mapper[Any]]) -> Iterator[Any]:
    for relationship in mapper.relationships:
        if relationship.lazy not in ("joined", False):
            continue
        attribute = getattr(mapper.class_, relationship.key)
        if relationship.uselist:
            yield selectinload(attribute) if path is None else path.selectinload(attribute)
        elif relationship.mapper not in seen:
            step = defaultload(attribute) if path is None else path.defaultload(attribute)
            yield from _joined_collections(relationship.mapper, step, seen | {mapper})


# ---------------------------------------------------------------------------------------------------------
# Ordering
# ---------------------------------------------------------------------------------------------------------


def order_expressions(
    entity: type, sort: Sort, dialect: Dialect, *, resolver: PropertyResolver | None = None
) -> list[ColumnElement[Any]]:
    """The ORDER BY expressions of *sort* on *entity* for *dialect* (module documentation). Property names go
    through *resolver* when given (``InvalidPropertyError`` for a name it refuses)."""
    backend = backend_name(dialect)
    native_nulls = backend in _NATIVE_NULL_ORDERING and (backend != "sqlite" or _SQLITE_NULLS_ORDERING)
    expressions: list[ColumnElement[Any]] = []
    for order in sort.orders:
        name = resolver.resolve(order.property, usage="sort") if resolver is not None else order.property
        column = getattr(entity, name)
        key = func.lower(column) if order.ignore_case else column
        directed = key.desc() if order.direction == "desc" else key.asc()
        if order.null_handling is NullHandling.NATIVE:
            expressions.append(directed)
            continue
        first = order.null_handling is NullHandling.NULLS_FIRST
        if native_nulls:
            expressions.append(directed.nulls_first() if first else directed.nulls_last())
            continue
        # The NULL rows' key sorts them first (0) or last (1); the column's own order follows.
        null_key = case((column.is_(None), _ZERO if first else _ONE), else_=_ONE if first else _ZERO)
        expressions.append(null_key.asc())
        expressions.append(directed)
    return expressions


def primary_key_orders(entity: type, sort: Sort) -> list[ColumnElement[Any]]:
    """Ascending orders on the primary-key columns of *entity* that *sort* does not order by already."""
    mapper: Mapper[Any] = sa_inspect(entity)
    named = {order.property for order in sort.orders}
    orders: list[ColumnElement[Any]] = []
    for column in mapper.primary_key:
        key = mapper.get_property_by_column(column).key
        if key not in named:
            orders.append(getattr(entity, key).asc())
    return orders


# ---------------------------------------------------------------------------------------------------------
# Delete strategy
# ---------------------------------------------------------------------------------------------------------


def bulk_delete_safe(entity: type, session: AsyncSession | Session | None = None) -> bool:
    """Whether a bulk ``DELETE`` of *entity* rows does what deleting them entity by entity does.

    It does not when the ORM would do more than one ``DELETE`` per row: a version column to check, a
    relationship it cascades to or whose foreign keys it nulls out (unless ``passive_deletes`` leaves that to
    the database), joined-table inheritance, or listeners of the mapper's delete events or of *session*'s
    delete lifecycle events.
    """
    mapper: Mapper[Any] = sa_inspect(entity)
    if mapper.version_id_col is not None or mapper.inherits is not None or mapper.polymorphic_on is not None:
        return False
    if mapper.dispatch.before_delete or mapper.dispatch.after_delete:
        return False
    for relationship in mapper.relationships:
        if relationship.viewonly or relationship.passive_deletes:
            continue
        if relationship.direction is MANYTOONE and not relationship.cascade.delete:
            continue
        return False
    if session is not None:
        sync_session = session.sync_session if isinstance(session, AsyncSession) else session
        dispatch = sync_session.dispatch
        if dispatch.persistent_to_deleted or dispatch.deleted_to_detached:
            return False
    return True


# ---------------------------------------------------------------------------------------------------------
# Fetch plans and locks
# ---------------------------------------------------------------------------------------------------------


FetchStep = str | QueryableAttribute[Any] | ExecutableOption
"""One step of a fetch plan (see :data:`FetchPlan`)."""

FetchPlan = FetchStep | Iterable[FetchStep]
"""A repository's ``load=`` argument: relationship names (``"children"``, a dotted path ``"children.toys"``),
relationship attributes (``Parent.children``), loader options (``joinedload(Parent.children)``), or a
sequence of them. Names and attributes load with ``selectin``: one more statement per relationship, whatever
the number of rows, and correct with ``LIMIT``."""


def loader_options(entity: type, load: FetchPlan | None) -> list[Any]:
    """The loader options of the fetch plan *load* (:data:`FetchPlan`) on *entity*.

    A name that is not a relationship raises :class:`~pyfly.data.property_resolver.InvalidPropertyError`, and
    any other kind of value ``TypeError``.
    """
    if load is None:
        return []
    items: list[Any] = [load] if isinstance(load, (str, QueryableAttribute, ExecutableOption)) else list(load)
    options: list[Any] = []
    for item in items:
        if isinstance(item, str):
            options.append(_path_option(entity, item))
        elif isinstance(item, QueryableAttribute):
            if not isinstance(item.property, RelationshipProperty):
                raise InvalidPropertyError(
                    f"{item} is not a relationship: only relationships can be loaded",
                    entity=entity.__name__,
                    property=str(item.key),
                    usage="fetch",
                )
            options.append(selectinload(item))
        elif isinstance(item, ExecutableOption):
            options.append(item)
        else:
            raise TypeError(
                f"load= takes relationship names, relationship attributes and loader options, not {type(item).__name__}"
            )
    return options


def _path_option(entity: type, path: str) -> Any:
    option: Any = None
    current: Mapper[Any] = sa_inspect(entity)
    for step in path.split("."):
        relationship = current.relationships.get(step)
        if relationship is None:
            raise InvalidPropertyError(
                f"{current.class_.__name__} has no relationship {step!r} to load (in load={path!r}); relationships: "
                f"{', '.join(sorted(current.relationships.keys())) or '(none)'}",
                entity=current.class_.__name__,
                property=step,
                usage="fetch",
            )
        attribute = getattr(current.class_, step)
        option = selectinload(attribute) if option is None else option.selectinload(attribute)
        current = relationship.mapper
    return option


class LockMode(enum.Enum):
    """A pessimistic lock taken on the rows a query reads (``SELECT ... FOR UPDATE``).

    It needs a transaction: the lock lasts until the unit of work ends. SQLite has no row locks (it has one
    writer, and a write unit takes the database lock at ``BEGIN``), so there the clause is not rendered.
    """

    PESSIMISTIC_READ = "PESSIMISTIC_READ"
    """A shared lock: ``FOR SHARE`` (``LOCK IN SHARE MODE`` on older MySQL)."""
    PESSIMISTIC_WRITE = "PESSIMISTIC_WRITE"
    """An exclusive lock: ``FOR UPDATE``."""
    PESSIMISTIC_WRITE_NOWAIT = "PESSIMISTIC_WRITE_NOWAIT"
    """``FOR UPDATE NOWAIT``: fail at once instead of waiting for a row another transaction locked."""
    PESSIMISTIC_WRITE_SKIP_LOCKED = "PESSIMISTIC_WRITE_SKIP_LOCKED"
    """``FOR UPDATE SKIP LOCKED``: leave out the rows another transaction locked (work queues)."""

    @property
    def for_update(self) -> dict[str, bool]:
        """The arguments of ``Select.with_for_update()`` for this mode."""
        return dict(_FOR_UPDATE[self])

    def apply(self, statement: Select[Any]) -> Select[Any]:
        """*statement* taking this lock on the rows it reads."""
        flags = _FOR_UPDATE[self]
        return statement.with_for_update(
            read=flags.get("read", False),
            nowait=flags.get("nowait", False),
            skip_locked=flags.get("skip_locked", False),
        )


_FOR_UPDATE: dict[LockMode, dict[str, bool]] = {
    LockMode.PESSIMISTIC_READ: {"read": True},
    LockMode.PESSIMISTIC_WRITE: {},
    LockMode.PESSIMISTIC_WRITE_NOWAIT: {"nowait": True},
    LockMode.PESSIMISTIC_WRITE_SKIP_LOCKED: {"skip_locked": True},
}
