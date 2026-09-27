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
  values per list, SQL Server about 2100 parameters per statement, SQLite 32766, PostgreSQL 32767), leaving
  room for the statement's other binds where the limit is per statement: run one statement per chunk in the
  same unit of work and concatenate the rows or add the counts. On PostgreSQL a
  single-column list is one ``= ANY(:array)`` bind, the same statement text for every length, which asyncpg's
  prepared-statement cache keeps; elsewhere a list is padded to the next power of two with its last value
  (Hibernate's ``in_clause_parameter_padding``), so a handful of texts cover every length. Composite keys are
  row values (``(a, b) IN ((?, ?), ...)``), or an OR of ANDs on SQL Server, which has no row values.
- **Entity results.** :func:`unique_entities` consumes an entity result through ``unique()``, which a joined
  eager load of a collection requires; :func:`stream_safe` loads such collections with ``selectin`` for a
  streamed result instead, since a streamed result cannot be made unique.
- **Ordering.** :func:`order_expressions` renders a :class:`~pyfly.data.pageable.Sort` with its NULL
  placement (native ``NULLS FIRST``/``NULLS LAST`` on PostgreSQL, SQLite and Oracle, an ``IS NULL`` key first
  on MySQL, MariaDB and SQL Server) and the case folding of string columns; :func:`primary_key_orders` is the
  tie-breaker every paging path appends, which makes pages deterministic and is what SQL Server requires for
  ``OFFSET``.
- **Entities a join repeats.** :func:`joins_rows` tells whether a specification's join can repeat an entity;
  :func:`distinct_entity_page` then cuts a page from the distinct primary keys, and
  :func:`distinct_entity_count` counts them, so pages count entities rather than joined rows;
  :func:`row_count` counts the rows of any other statement. The page's entities are read by the statement
  itself joined to its keys, so what it asks of them (its fetch plan, a ``contains_eager`` of its own join,
  loader criteria, execution options, and a lock of any table it reads) applies to them.
- **Delete strategy.** :func:`bulk_delete_safe` says whether a bulk ``DELETE`` does what deleting entity by
  entity does: no ORM cascade, version column, inheritance or delete listener.
- **Fetch plans and locks.** :func:`loader_options` turns a repository's ``load=`` argument into loader
  options, and :class:`LockMode` is a pessimistic lock (``SELECT ... FOR UPDATE``).
"""

from __future__ import annotations

import enum
import sqlite3
from collections.abc import Callable, Iterable, Iterator, Sequence
from typing import Any, TypeVar, cast

from sqlalchemy import (
    ARRAY,
    Select,
    and_,
    any_,
    asc,
    bindparam,
    case,
    desc,
    func,
    literal_column,
    nulls_first,
    nulls_last,
    or_,
    select,
    tuple_,
)
from sqlalchemy import inspect as sa_inspect
from sqlalchemy.engine import Dialect
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import (
    LoaderCriteriaOption,
    Mapper,
    QueryableAttribute,
    RelationshipProperty,
    Session,
    defaultload,
    selectinload,
)
from sqlalchemy.orm.interfaces import MANYTOONE
from sqlalchemy.sql import operators
from sqlalchemy.sql.base import ExecutableOption
from sqlalchemy.sql.elements import ColumnElement, UnaryExpression
from sqlalchemy.types import Enum, String, TypeDecorator

from pyfly.data.pageable import NullHandling, Sort
from pyfly.data.property_resolver import InvalidPropertyError, PropertyResolver
from pyfly.data.relational.datasource_registry import DataSourceCapabilities
from pyfly.data.relational.sqlalchemy.compat import drop_distinct_on_extension

__all__ = [
    "FetchPlan",
    "FetchStep",
    "RESERVED_BINDS",
    "LockMode",
    "backend_name",
    "bulk_delete_safe",
    "chunked",
    "distinct_entity_count",
    "distinct_entity_page",
    "exists",
    "exists_probe",
    "in_criteria",
    "in_list_limit",
    "joins_rows",
    "loader_options",
    "loads_per_batch",
    "order_expressions",
    "padded",
    "primary_key_orders",
    "row_count",
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

_PER_LIST_LIMITS = frozenset({"oracle"})
"""Dialects whose IN limit counts the values of one list (Oracle: 1000 per list), not every bind of a statement."""

RESERVED_BINDS = 32
"""How many binds :func:`in_criteria` leaves free by default for the rest of the statement (its other criteria)
where the dialect's limit counts every bind of a statement (SQLite, SQL Server, PostgreSQL, MySQL, MariaDB)."""

_ROW_VALUES_PER_LIST = 1000
"""The most composite keys one IN list holds, whatever the dialect's parameter limit."""

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
    *,
    reserved: int = RESERVED_BINDS,
) -> list[ColumnElement[bool]]:
    """Criteria that match rows whose *columns* equal one of *values*, one per chunk (module documentation).

    *values* are scalars for one column and tuples (one item per column, in order) for several. Run one
    statement per criterion; an empty *values* gives no criterion at all (nothing to match, nothing to run).
    *reserved* is how many binds the statement carries beside the list (its other criteria, the values an
    ``UPDATE`` sets): where the dialect's limit counts every bind of a statement, a chunk, padding included,
    leaves them room. Pass more than the default when the statement carries more.
    """
    if not values:
        return []
    name = backend_name(dialect)
    capacity = in_list_limit(dialect) if name in _PER_LIST_LIMITS else in_list_limit(dialect) - max(0, reserved)
    if capacity < 1:
        raise ValueError(f"{name} binds {in_list_limit(dialect)} values per statement, and {reserved} are reserved")
    if len(columns) == 1:
        (column,) = columns
        if name == "postgresql":
            array = bindparam(None, list(values), type_=ARRAY(column.type))
            return [column == any_(array)]
        return [column.in_(padded(chunk, capacity)) for chunk in chunked(values, capacity)]
    width = len(columns)
    # A long row-value list is parsed recursively (PostgreSQL runs out of stack depth past a few thousand rows),
    # so row values go 1000 at a time, the smallest per-list limit of any dialect (Oracle's).
    limit = max(1, min(_ROW_VALUES_PER_LIST, capacity // width))
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


def exists_probe(source: Any, *criteria: Any) -> Select[Any]:
    """``SELECT 1 FROM source WHERE criteria LIMIT 1``.

    *source* is an entity, a table, or a ``select(Entity)`` whose FROM and WHERE are kept (its columns,
    ORDER BY and loader options are not). Run it with :func:`exists`.
    """
    if isinstance(source, Select):
        probe: Select[Any] = source.with_only_columns(_ONE, maintain_column_froms=True).order_by(None)
    else:
        probe = select(_ONE).select_from(source)
    return probe.where(*criteria).limit(1)


async def exists(session: AsyncSession, probe: Select[Any]) -> bool:
    """Run *probe* (:func:`exists_probe`) and tell whether it found a row."""
    return (await session.execute(probe)).scalar() is not None


def unique_entities(result: Any, *, distinct: bool = False) -> list[Any]:
    """The entities of an ORM result, each once.

    The result goes through ``unique()`` when a joined eager load of a collection repeats its entities (the ORM
    requires it then), or when *distinct* says the statement's own joins may repeat them (:func:`joins_rows`);
    otherwise every row is a different entity and the rows are read as they are, which spares hashing each
    one.
    """
    # The ORM marks a result that needs unique() (joined eager loads of collections) with a filter that
    # raises until unique() is called; a result without it has one row per entity.
    if distinct or getattr(result, "_unique_filter_state", None) is not None:
        return list(result.unique().scalars().all())
    return list(result.scalars().all())


# ---------------------------------------------------------------------------------------------------------
# Entities a join repeats
# ---------------------------------------------------------------------------------------------------------


_ORDER_MODIFIERS: dict[Any, Callable[[Any], Any]] = {
    operators.asc_op: asc,
    operators.desc_op: desc,
    operators.nulls_first_op: nulls_first,
    operators.nulls_last_op: nulls_last,
}


def joins_rows(statement: Select[Any], entity: type) -> bool:
    """Whether *statement*, a ``SELECT`` of *entity*, reads rows of other tables beside the entity's own (a join
    or another FROM a specification added), so one entity can come back on several rows.

    A ``LIMIT`` on such a statement counts rows, not entities: page it with :func:`distinct_entity_page` and
    count it with :func:`distinct_entity_count`. A subquery (``EXISTS``, ``IN (SELECT ...)``) adds no FROM, and
    a join along a many-to-one relationship (``join(Child.parent)``, from the entity or from a many-to-one
    joined before it) matches at most one row per entity; any other join, and any FROM the joins did not bring
    in, may repeat it.
    """
    mapper: Mapper[Any] = sa_inspect(entity)
    covered: set[Any] = set(mapper.tables)
    for target, onclause, _left, flags in statement._setup_joins:
        relationship = _relationship_of(onclause) or _relationship_of(target)
        if (
            relationship is None
            or flags.get("full")
            or relationship.direction is not MANYTOONE
            or relationship.uselist
            or relationship.parent.local_table not in covered
        ):
            return True
        # An aliased target (of_type) is not covered: a WHERE on the alias then counts as another FROM.
        covered.update(
            relationship.mapper.tables if isinstance(target, QueryableAttribute) else [cast(Any, target)._deannotate()]
        )
    where = statement.whereclause
    sources = [*statement._from_obj, *(where._from_objects if where is not None else ())]
    return any(source._deannotate() not in covered for source in sources)


def _relationship_of(element: Any) -> RelationshipProperty[Any] | None:
    """The relationship *element* is an attribute of (``Child.parent``), or ``None``."""
    prop = getattr(element, "property", None) if isinstance(element, QueryableAttribute) else None
    return prop if isinstance(prop, RelationshipProperty) else None


def distinct_entity_page(
    statement: Select[Any], entity: type, *, offset: int | None = None, limit: int | None = None
) -> Select[Any]:
    """The entities *statement* selects, each once, in its ORDER BY, with *offset* and *limit* counting entities.

    The page is cut from the distinct primary keys of the matching rows (with the ORDER BY keys beside them,
    since a ``DISTINCT`` may only order by what it selects), and the entities are read by *statement* itself
    joined to those keys, in the same order: ``SELECT e.* FROM e <its joins> JOIN (SELECT DISTINCT e.pk, <keys>
    ... ORDER BY ... LIMIT ...) AS page ON e.pk = page.pk WHERE <its criteria> ORDER BY page.<keys>``. Portable
    (SQL Server 2012 and later included), and one statement. Order *statement* by the entity's own properties:
    ordering by a joined row's column repeats an entity once per distinct value.

    Read through its own FROMs, joins and criteria, the entities get what *statement* asks of them: a
    ``contains_eager`` load gets the rows its join matched, and its loader options (a fetch plan,
    ``with_loader_criteria``) and execution options apply. Its joins repeat an entity on that read too: consume
    the result with :func:`unique_entities` (``distinct=True``). Its ``DISTINCT``, ``GROUP BY``, ``HAVING``,
    ``LIMIT`` and ``OFFSET`` shape the keys alone, which then hold only the entities they admit (on the entities'
    rows, a ``DISTINCT`` or a ``GROUP BY`` could not order by the page's keys). Its lock (``with_for_update``),
    which PostgreSQL and Oracle refuse on a ``DISTINCT``, leaves the keys and takes the rows the entities are read
    from: the entity's own (``FOR UPDATE OF`` its table where the dialect names tables) unless it names what to
    lock itself, a table it joins included. More loader options go on the returned statement.
    """
    mapper: Mapper[Any] = sa_inspect(entity)
    key_columns = [getattr(entity, mapper.get_property_by_column(column).key) for column in mapper.primary_key]
    orders = [_split_order(clause) for clause in statement._order_by_clauses]
    keys = [column.label(f"pyfly_k{index}") for index, column in enumerate(key_columns)]
    sort_keys = [key.label(f"pyfly_o{index}") for index, (key, _modifiers) in enumerate(orders)]
    inner = _unlocked(statement.with_only_columns(*keys, *sort_keys, maintain_column_froms=True).order_by(None))
    inner = inner.distinct()
    if offset or limit is not None:
        # SQL Server refuses an ORDER BY in a derived table without OFFSET or TOP: order only a cut page.
        inner = inner.order_by(
            *(_directed(label, modifiers) for label, (_key, modifiers) in zip(sort_keys, orders, strict=True))
        )
        if offset:
            inner = inner.offset(offset)
        if limit is not None:
            inner = inner.limit(limit)
    page = inner.subquery("pyfly_page")
    matched = and_(*(column == page.c[f"pyfly_k{index}"] for index, column in enumerate(key_columns)))
    # Joined from the entity, since the statement's FROM may start with another table (select_from).
    outer: Select[Any] = (
        _entity_rows(statement)
        .join_from(entity, page, matched)
        .order_by(*(_directed(page.c[f"pyfly_o{index}"], modifiers) for index, (_key, modifiers) in enumerate(orders)))
    )
    lock = statement._for_update_arg
    if lock is not None:
        # A column names its table on every dialect (Oracle renders OF with columns only).
        outer = outer.with_for_update(
            read=lock.read,
            nowait=lock.nowait,
            skip_locked=lock.skip_locked,
            key_share=lock.key_share,
            of=cast(Any, lock.of) if lock.of else key_columns,
        )
    return outer


def distinct_entity_count(statement: Select[Any], entity: type) -> Select[Any]:
    """``SELECT count(*)`` of the distinct *entity* primary keys *statement* matches (see :func:`row_count` for
    what the count keeps of *statement*)."""
    mapper: Mapper[Any] = sa_inspect(entity)
    key_columns = [getattr(entity, mapper.get_property_by_column(column).key) for column in mapper.primary_key]
    keys = statement.with_only_columns(*key_columns, maintain_column_froms=True).distinct()
    return _count(keys, statement)


def row_count(statement: Select[Any]) -> Select[Any]:
    """``SELECT count(*)`` of the rows *statement* matches.

    The count runs over *statement* as a subquery, without its ORDER BY and its lock (counting locks nothing,
    and PostgreSQL refuses a lock on a ``DISTINCT``). It keeps the loader criteria of *statement*
    (``with_loader_criteria``) and its execution options, which the ORM would not apply to a subquery: the
    count covers what the statement reads.
    """
    return _count(statement, statement)


def _count(counted: Select[Any], source: Select[Any]) -> Select[Any]:
    count: Select[Any] = select(func.count()).select_from(_unlocked(counted.order_by(None)).subquery())
    criteria = [option for option in source._with_options if isinstance(option, LoaderCriteriaOption)]
    if criteria:
        count = count.options(*criteria)
    execution_options = source.get_execution_options()
    return count.execution_options(**execution_options) if execution_options else count


def _unlocked(statement: Select[Any]) -> Select[Any]:
    """*statement* without its ``FOR UPDATE`` (there is no public way to take a lock off a ``SELECT``)."""
    if statement._for_update_arg is None:
        return statement
    copy = statement._generate()
    copy._for_update_arg = None
    return copy


def _entity_rows(statement: Select[Any]) -> Select[Any]:
    """*statement* reading every row it matches: without the ``DISTINCT``, ``GROUP BY``, ``HAVING``, ``ORDER BY``,
    ``LIMIT``, ``OFFSET`` and lock that shape, cut or lock them (there is no public way to take a ``DISTINCT``, a
    ``HAVING`` or a lock off a ``SELECT``)."""
    rows = _unlocked(statement).order_by(None).group_by(None).limit(None).offset(None)
    # The generative calls returned a copy of its own: what is reset here is shared with no other statement.
    rows._distinct, rows._distinct_on, rows._having_criteria = False, (), ()
    drop_distinct_on_extension(rows)  # SQLAlchemy 2.1's ext(postgresql.distinct_on(...))
    return rows


def _split_order(clause: Any) -> tuple[Any, list[Any]]:
    """An ORDER BY clause as its key expression and its modifiers (direction, NULL placement), outermost first."""
    modifiers: list[Any] = []
    element = clause
    while isinstance(element, UnaryExpression) and element.modifier in _ORDER_MODIFIERS:
        modifiers.append(element.modifier)
        element = element.element
    return element, modifiers


def _directed(expression: Any, modifiers: Sequence[Any]) -> Any:
    """*expression* with the ORDER BY *modifiers* of :func:`_split_order` applied again."""
    for modifier in reversed(modifiers):
        expression = _ORDER_MODIFIERS[modifier](expression)
    return expression


def stream_safe(statement: Select[Any], entity: type) -> Select[Any]:
    """*statement* with every joined eager load of a collection that *entity*'s rows would bring (its own, and
    those behind its joined many-to-ones) turned into a ``selectin`` load, which a streamed result supports:
    the collections of each fetched batch are loaded by one more statement. Unchanged when there is none."""
    options = list(_joined_collections(sa_inspect(entity), None, frozenset()))
    return statement.options(*options) if options else statement


_PER_BATCH_LOADS = frozenset({"selectin", "subquery", "immediate"})
"""Relationship loading strategies that run statements of their own for the rows a result fetched."""


def loads_per_batch(entity: type) -> bool:
    """Whether reading *entity* rows in a stream runs statements of its own for each fetched batch: a
    ``selectin``, ``subquery`` or ``immediate`` relationship (its own, or one behind a joined many-to-one), or a
    joined collection, which :func:`stream_safe` turns into a ``selectin`` load. A joined many-to-one comes with
    its row and runs nothing."""
    return _loads_per_batch(sa_inspect(entity), frozenset())


def _loads_per_batch(mapper: Mapper[Any], seen: frozenset[Mapper[Any]]) -> bool:
    for relationship in mapper.relationships:
        if relationship.lazy in _PER_BATCH_LOADS:
            return True
        if relationship.lazy not in ("joined", False):
            continue
        if relationship.uselist:
            return True
        if relationship.mapper not in seen and _loads_per_batch(relationship.mapper, seen | {mapper}):
            return True
    return False


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
        key = func.lower(column) if order.ignore_case and _folds(column) else column
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


def _folds(column: Any) -> bool:
    """Whether ``ignore_case`` folds *column*: a string column, as Spring folds only ``String`` expressions (an
    enum is a string type in SQLAlchemy, but a native type without ``lower()`` on PostgreSQL)."""
    sqltype = getattr(column, "type", None)
    while isinstance(sqltype, TypeDecorator):
        sqltype = sqltype.impl_instance
    return isinstance(sqltype, String) and not isinstance(sqltype, Enum)


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
    the database), joined-table inheritance, or listeners of the mapper's delete events, of *session*'s
    delete lifecycle events or of its flushes (``before_flush``, ``after_flush``, ``after_flush_postexec``: an
    audit of ``session.deleted`` would miss the rows).
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
        if any(getattr(dispatch, hook) for hook in _SESSION_DELETE_HOOKS):
            return False
    return True


_SESSION_DELETE_HOOKS = (
    "persistent_to_deleted",
    "deleted_to_detached",
    "before_flush",
    "after_flush",
    "after_flush_postexec",
)
"""Session events that see an entity deleted through the ORM (a flush listener auditing ``session.deleted``),
and never see the rows a bulk ``DELETE`` removes."""


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
