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
"""Generic async repository built on SQLAlchemy 2.0, resolving its session per call.

A :class:`Repository` never holds a session of its own (unless the caller hands it one). Every public
``async def`` (on ``Repository``, on ``SoftDeleteRepository``, on a subclass, and the derived and ``@query``
methods the post-processor compiles) is wrapped so that each call opens an **operation scope**:

- inside a unit of work for the repository's datasource (``@transactional``, a ``TransactionTemplate``
  block), the call joins that unit and uses its session, under the unit's operation guard;
- otherwise the outermost repository call opens a short **auto unit** of its own: a *read* method
  (``find*``, ``count*``, ``exists*``, ``stream*``, ``get*``, ``scroll*``, unless its name before ``_by_``
  holds a write verb such as ``create``, ``save``, ``update`` or ``delete``: ``get_or_create`` writes) gets a
  read unit (on PostgreSQL an ``AUTOCOMMIT`` connection, one round trip; elsewhere a short transaction that
  ends without writing) that is retried once when its connection turns out to be dead, and any other
  method a write unit that commits. Both run the datasource's after-begin customizers, and both always
  release their connection. A subclass method decorated with ``@transactional`` opens no auto unit: its
  boundary begins (or joins) the unit it runs in.

A write auto unit is the transaction of the repository call that opened it, as a Spring Data repository
method is ``@Transactional``: a ``@transactional`` call made inside it joins it (``REQUIRED``, ``SUPPORTS``,
``MANDATORY``), takes a savepoint on it (``NESTED``), suspends it (``REQUIRES_NEW``, ``NOT_SUPPORTED``) or is
refused (``NEVER``). A read auto unit is not a transaction (it may run on an ``AUTOCOMMIT`` connection): a
boundary inside a read method sees no transaction.

Nested repository calls inside one operation share its unit: a subclass method calling ``count()`` and
``exists_by_id()`` is one unit. The framework's own methods are atomic for a task that shares the unit
(they hold its operation guard for the whole call), so they never call a method a subclass may override.
Entities returned from an auto unit are detached with their loaded state intact (auto units never expire on
commit); a relationship that was not loaded needs a fetch plan (``load=``, see below).

Two modes:

- **managed** (a DI-built repository, or ``Repository(Model)``): the session is resolved per call as
  above, on the datasource named by ``datasource=`` or the class attribute ``__datasource__`` (default:
  the primary);
- **manual** (``Repository(Model, session)``): the caller owns that session and the repository uses it as
  is. Tests and scripts rely on this.

Custom methods keep working: ``self._session`` (and ``self._require_session()``) return the session of the
current operation scope.

Spring Data semantics, at the minimum statement count:

- ``save`` persists a new entity (one ``INSERT``; server-generated values come back through ``RETURNING``,
  or one targeted ``SELECT`` of just those columns on a backend without it) and merges any other: a DTO with
  an existing id updates its row, a detached entity is re-attached (one ``UPDATE`` of what changed), an
  entity of another session is copied in. An entity is new when its ``is_new()`` hook says so
  (:class:`~pyfly.data.ports.outbound.Persistable`), else when its version is ``None``, else when its
  primary key is. ``save_all`` merges existing DTOs with one ``SELECT`` for all of them.
- ``delete``, ``delete_by_id``, ``delete_all_by_id`` and ``delete_all`` delete entity by entity through the
  ORM, so cascades, version checks and delete listeners run; they send one bulk ``DELETE`` instead only
  when the mapper has none of those. ``delete_all_in_batch`` and ``delete_all_by_id_in_batch`` are always
  bulk (Spring's ``deleteAllInBatch``): they bypass cascades on purpose.
- ``exists_by_id`` answers from the unit's identity map, else with ``SELECT 1 ... LIMIT 1``.
- Every paging path orders by the primary key after the requested orders (deterministic pages, and what
  SQL Server requires for ``OFFSET``); ``find_all(Pageable)`` skips the ``COUNT`` when the page gives the
  total; ``find_slice`` sends no ``COUNT`` at all; ``scroll`` pages by keyset.
- Id lists are chunked to the dialect's limit and padded (one ``= ANY`` bind on PostgreSQL), composite keys
  included; entity results go through ``unique()``, so a ``lazy="joined"`` collection works everywhere.
- Sort and filter names are validated against the entity (``InvalidPropertyError``, a 400), optionally
  narrowed by the class attributes ``__sortable__`` and ``__filterable__``.
- Read methods take a fetch plan, ``load=`` (relationship names, attributes or loader options; the class
  attribute ``__load__`` is the default), and ``find_by_id`` a pessimistic ``lock=``
  (:class:`~pyfly.data.relational.sqlalchemy.statements.LockMode`), which needs a read-write transaction.
  A read method's ``load`` and ``lock`` keywords are not filters: filter on a column with those names
  through a :class:`~pyfly.data.relational.sqlalchemy.specification.Specification`.
"""

from __future__ import annotations

import functools
import inspect
import logging
from collections.abc import AsyncGenerator, AsyncIterator, Callable, Coroutine, Iterable, Sequence
from typing import Annotated, Any, ClassVar, Generic, TypeVar, cast, get_args, get_origin, overload

from sqlalchemy import Select, and_, func, or_, select
from sqlalchemy import delete as sa_delete
from sqlalchemy import inspect as sa_inspect
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import InstanceState, Mapper, selectinload
from sqlalchemy.orm.attributes import set_committed_value
from sqlalchemy.orm.exc import StaleDataError

from pyfly.container.types import NoAutowire
from pyfly.data.page import Page, Slice, Window
from pyfly.data.pageable import KeysetPosition, NullHandling, Order, Pageable, Sort
from pyfly.data.property_resolver import PropertyResolver
from pyfly.data.relational.datasource_registry import DataSourceCapabilities
from pyfly.data.relational.sqlalchemy.specification import Specification
from pyfly.data.relational.sqlalchemy.statements import (
    FetchPlan,
    LockMode,
    bulk_delete_safe,
    dialect_of,
    exists,
    exists_probe,
    in_criteria,
    loader_options,
    order_expressions,
    primary_key_orders,
    stream_safe,
    unique_entities,
)
from pyfly.data.transaction.context import bind_state, current_state, reset_state
from pyfly.data.transaction.decorator import is_transactional
from pyfly.data.transaction.errors import IllegalTransactionStateError
from pyfly.data.transaction.manager import TransactionManager
from pyfly.data.transaction.registry import PRIMARY, TransactionManagerRegistry, installed_registry
from pyfly.data.transaction.template import AutoUnit, complete_auto_unit
from pyfly.data.transaction.unit_of_work import UnitOfWork, cancel_requests

T = TypeVar("T")
ID = TypeVar("ID")

_logger = logging.getLogger(__name__)

STREAM_FIRST_BATCH = 5
"""How many rows ``stream_all`` fetches first; each batch after that is five times larger, up to
:data:`STREAM_BATCH_SIZE` (the buffering SQLAlchemy applies to a streamed result read row by row)."""

STREAM_BATCH_SIZE = 1000
"""The most rows ``stream_all`` fetches from its server-side cursor at a time."""

READ_PREFIXES: tuple[str, ...] = ("find", "count", "exists", "stream", "get", "scroll")
"""A repository method whose name starts with one of these runs in a read auto unit outside a transaction,
unless :data:`WRITE_WORDS` says it writes."""

WRITE_WORDS: frozenset[str] = frozenset(
    {"create", "save", "insert", "update", "upsert", "delete", "remove", "merge", "persist", "store", "lock"}
)
"""Words that make a read-prefixed method name a write method when they appear, as a whole word, in the
name before its criteria (``get_or_create``, ``find_or_create_by_email``, ``find_and_update``,
``find_and_lock_by_id``); ``find_by_update_time`` stays a read."""

_EAGER_LOADS = frozenset({"joined", "selectin", "subquery", "immediate"})
"""Relationship loading strategies that run statements (or joins) while an entity result is fetched."""


def is_read_method(name: str) -> bool:
    """Whether a repository method called *name* reads (and so gets a read auto unit)."""
    if not name.startswith(READ_PREFIXES):
        return False
    subject = name.split("_by_", 1)[0]
    return not any(word in WRITE_WORDS for word in subject.lower().split("_"))


def repository_operation(
    function: Callable[..., Coroutine[Any, Any, Any]], *, read: bool, atomic: bool = False
) -> Callable[..., Coroutine[Any, Any, Any]]:
    """Wrap a repository coroutine method so every call runs in an operation scope (module documentation).

    *read* selects a read auto unit outside a transaction. *atomic* holds the unit's operation guard for the
    whole call: the framework's own methods are atomic (``save`` is attach or merge and flush as one step for a
    task that shares the unit), while a subclass method is not, since it may await anything. A method
    decorated with ``@transactional`` opens no auto unit: its own boundary provides the unit.
    """
    transactional = is_transactional(function)

    @functools.wraps(function)
    async def operation(self: Repository[Any, Any], *args: Any, **kwargs: Any) -> Any:
        return await self._pyfly_run(function, args, kwargs, read=read, atomic=atomic, transactional=transactional)

    operation.__pyfly_repository_operation__ = True  # type: ignore[attr-defined]
    return operation


def repository_stream(function: Callable[..., AsyncGenerator[Any, None]]) -> Callable[..., AsyncIterator[Any]]:
    """Wrap a repository async-generator method (``stream_all``): it captures the unit at its first step, or
    opens its own read unit (always a transaction: server-side cursors need one), and owns that unit's
    connection until the iterator is exhausted or ``aclose()``d.

    The wrapper returns the stream itself (it is not a generator around it), so ``aclose()`` reaches the
    code that completes the unit at once.
    """

    @functools.wraps(function)
    def stream(self: Repository[Any, Any], *args: Any, **kwargs: Any) -> AsyncIterator[Any]:
        return self._pyfly_stream(function, args, kwargs)

    stream.__pyfly_repository_operation__ = True  # type: ignore[attr-defined]
    return stream


def _wrap_operations(cls: type, *, atomic: bool) -> None:
    for name, attribute in list(vars(cls).items()):
        if name.startswith("_") or getattr(attribute, "__pyfly_repository_operation__", False):
            continue
        if inspect.isasyncgenfunction(attribute):
            setattr(cls, name, repository_stream(attribute))
        elif inspect.iscoroutinefunction(attribute):
            setattr(cls, name, repository_operation(attribute, read=is_read_method(name), atomic=atomic))


def _type_arguments(cls: type) -> tuple[Any, Any] | None:
    """The ``(entity, id)`` arguments *cls* binds ``Repository[T, ID]`` to through its generic bases, with
    type variables substituted along the way (``class Base(Repository[T, ID])``, ``class X(Base[E, int])``,
    and bases that reorder their parameters); ``None`` when no base reaches ``Repository``."""

    def walk(klass: type, substitution: dict[Any, Any]) -> tuple[Any, ...] | None:
        bases = klass.__dict__.get("__orig_bases__", klass.__bases__)
        for base in bases:
            origin = get_origin(base) or base
            if not (isinstance(origin, type) and issubclass(origin, Repository)):
                continue
            arguments = tuple(substitution.get(arg, arg) for arg in get_args(base))
            if origin is Repository:
                return arguments
            parameters = getattr(origin, "__parameters__", ())
            found = walk(origin, dict(zip(parameters, arguments, strict=False)))
            if found is not None:
                return found
        return None

    found = walk(cls, {})
    if found is None:
        return None
    entity = found[0] if found else None
    id_type = found[1] if len(found) > 1 else None
    return entity, id_type


def _state(entity: Any) -> InstanceState[Any]:
    """The ORM state of a mapped instance."""
    return cast("InstanceState[Any]", sa_inspect(entity))


def _identity_key(entity: Any) -> tuple[Any, ...]:
    """The primary key of a persistent instance."""
    return tuple(_state(entity).identity or ())


def _persistable_hook(cls: type) -> Callable[[Any], bool] | None:
    """The entity class's ``is_new`` hook (a method or a property it defines, not a mapped column)."""
    static = inspect.getattr_static(cls, "is_new", None)
    if inspect.isfunction(static):
        return lambda entity: bool(entity.is_new())
    if isinstance(static, property):
        return lambda entity: bool(entity.is_new)
    return None


class Repository(Generic[T, ID]):
    """Generic CRUD repository for SQLAlchemy entities.

    Implements the Spring-parity ``PagingAndSortingRepository`` contract
    (``CrudRepository`` → ``ReactiveSortingRepository`` → paging) and the batch deletes and slices of
    :class:`~pyfly.data.ports.outbound.BatchRepository`, with async support. Subclass with concrete type
    parameters to enable DI-managed repositories.

    Type Parameters:
        T: The entity type (any SQLAlchemy model).
        ID: The primary key type (e.g. UUID, int, str; a tuple for a composite key).

    Usage::

        class UserRepository(Repository[User, UUID]):
            pass  # entity type auto-extracted; the session is resolved per call

        class ReportRepository(Repository[Report, int]):
            __datasource__ = "reporting"   # this repository's units run on the 'reporting' datasource

        class OrderRepository(Repository[Order, UUID]):
            __load__ = ("lines",)          # find_* load the lines unless a call passes its own load=
            __sortable__ = ("placed_at", "total")   # the only properties a Sort may name
    """

    _entity_type: type | None = None
    _id_type: type | None = None
    __datasource__: ClassVar[str | None] = None
    """The datasource a subclass's calls run on (``None``: the primary)."""
    __load__: ClassVar[FetchPlan | None] = None
    """The fetch plan of the read methods when a call passes no ``load=`` (``None``: the mapping's own)."""
    __sortable__: ClassVar[Sequence[str] | None] = None
    """The only properties a ``Sort`` may name (``None``: every mapped column)."""
    __filterable__: ClassVar[Sequence[str] | None] = None
    """The only properties ``find_all(**filters)`` may filter on (``None``: every mapped column)."""

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        arguments = _type_arguments(cls)
        if arguments is not None:
            entity, id_type = arguments
            if entity is not None and not isinstance(entity, TypeVar):
                cls._entity_type = entity
            if id_type is not None and not isinstance(id_type, TypeVar):
                cls._id_type = id_type
        _wrap_operations(cls, atomic=bool(vars(cls).get("_pyfly_framework_repository", False)))

    def __init__(
        self,
        model: type[T] | None = None,
        session: Annotated[AsyncSession | None, NoAutowire] = None,
        *,
        datasource: Annotated[str | None, NoAutowire] = None,
    ) -> None:
        resolved = model or getattr(type(self), "_entity_type", None)
        if resolved is None:
            raise TypeError(
                f"{type(self).__name__} requires either a concrete entity type in its declaration "
                "(Repository[Entity, ID], or a generic base such as SoftDeleteRepository[Entity, ID]) or an explicit "
                "model argument (Repository(Entity))"
            )
        self._model: type[T] = cast(type[T], resolved)
        from pyfly.data.relational.sqlalchemy.session import ScopedAsyncSession

        scoped_datasource: str | None = None
        if isinstance(session, ScopedAsyncSession):
            # The injected transient session of an older subclass constructor: stay managed, on its datasource.
            scoped_datasource = session.datasource
            session = None
        self._manual_session: AsyncSession | None = session
        self._datasource: str = datasource or type(self).__datasource__ or scoped_datasource or PRIMARY
        self._transaction_managers: TransactionManagerRegistry | None = None
        self._sort_names: PropertyResolver | None = None
        self._filter_names: PropertyResolver | None = None

    # ------------------------------------------------------------------
    # Session resolution
    # ------------------------------------------------------------------

    @property
    def datasource(self) -> str:
        """The datasource this repository's calls run on."""
        return self._datasource

    @property
    def _session(self) -> AsyncSession:
        """The session this call runs on: the manual session, else the current operation scope's or unit's.

        Raises :class:`~pyfly.data.transaction.errors.IllegalTransactionStateError` outside a repository call
        and outside a unit of work.
        """
        if self._manual_session is not None:
            return self._manual_session
        state = current_state()
        unit = state.scope(self._datasource) or state.unit(self._datasource)
        if unit is None:
            raise IllegalTransactionStateError(
                f"{type(self).__name__} has no session here: it resolves one per call, and no unit of work is "
                f"active for datasource '{self._datasource}'. Use the session inside a repository method (it "
                "opens a unit of its own), inside @transactional, or construct the repository with an explicit "
                "session.",
                datasource=self._datasource,
            )
        unit.check_usable()
        return cast(AsyncSession, unit.resource)

    @_session.setter
    def _session(self, session: AsyncSession | None) -> None:
        self._manual_session = session

    def _require_session(self) -> AsyncSession:
        """Return the session of the current call (see :attr:`_session`)."""
        return self._session

    def _bind_transaction_managers(self, managers: TransactionManagerRegistry) -> None:
        """Resolve this repository's transaction manager from *managers* (the post-processor binds the
        application context's registry, so two contexts in one process never share units)."""
        self._transaction_managers = managers

    def _transaction_manager(self) -> TransactionManager:
        registry = self._transaction_managers or installed_registry()
        if registry is None:
            raise IllegalTransactionStateError(
                f"{type(self).__name__} needs a transaction manager for datasource '{self._datasource}' to run "
                "outside a transaction, and no application context is running. Start the context, call it "
                "inside @transactional, or construct the repository with an explicit session.",
                datasource=self._datasource,
            )
        return registry.get(self._datasource)

    async def _pyfly_run(
        self,
        function: Callable[..., Coroutine[Any, Any, Any]],
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
        *,
        read: bool,
        atomic: bool,
        transactional: bool = False,
    ) -> Any:
        if self._manual_session is not None:
            return await function(self, *args, **kwargs)
        datasource = self._datasource
        state = current_state()
        scoped = state.scope(datasource)
        if scoped is not None:
            return await _run_on(scoped, function, self, args, kwargs, atomic)
        unit = state.unit(datasource)
        if unit is not None:
            unit.check_usable()
            token = bind_state(state.with_scope(datasource, unit))
            try:
                return await _run_on(unit, function, self, args, kwargs, atomic)
            finally:
                reset_state(token)
        if transactional:
            # The method's own boundary begins its unit; the method's session calls see that unit.
            return await function(self, *args, **kwargs)
        manager = self._transaction_manager()
        attempts = 2 if read else 1
        for attempt in range(1, attempts + 1):
            try:
                async with AutoUnit(manager, read_only=read) as auto:
                    return await _run_on(auto, function, self, args, kwargs, atomic)
            except Exception as error:
                if attempt < attempts and manager.is_disconnect(error):
                    _logger.info(
                        "repository_read_retried_after_disconnect",
                        extra={"repository": type(self).__name__, "datasource": datasource},
                    )
                    continue
                raise
        raise AssertionError("unreachable")  # pragma: no cover — the loop returns or raises

    async def _pyfly_stream(
        self, function: Callable[..., AsyncGenerator[Any, None]], args: tuple[Any, ...], kwargs: dict[str, Any]
    ) -> AsyncIterator[Any]:
        if self._manual_session is not None:
            async for item in function(self, *args, **kwargs):
                yield item
            return
        datasource = self._datasource
        state = current_state()
        unit = state.scope(datasource) or state.unit(datasource)
        owned = unit is None
        since = cancel_requests()
        if unit is None:
            unit = await self._transaction_manager().open_auto_unit(read_only=True, autocommit=False)
        else:
            unit.check_usable()
        inner = function(self, *args, **kwargs)
        step = inner.__anext__
        # The state the stream's steps run in, built once: the unit is bound only while the inner generator
        # runs a step, never across a yield (the consumer's own code between items is not inside it).
        scoped = current_state().with_scope(datasource, unit)
        error: BaseException | None = None
        try:
            while True:
                # Rows come from a fetched batch without touching the unit: a stream iterated after the unit
                # it captured completed still fails loudly.
                unit.check_usable()
                token = bind_state(scoped)
                try:
                    item = await step()
                except StopAsyncIteration:
                    break
                finally:
                    reset_state(token)
                yield item
        except BaseException as raised:
            error = raised
            raise
        finally:
            token = bind_state(scoped)
            try:
                await inner.aclose()
            finally:
                reset_state(token)
            if owned:
                await complete_auto_unit(unit, error, since=since)

    # ------------------------------------------------------------------
    # Mapping helpers
    # ------------------------------------------------------------------

    @property
    def _mapper(self) -> Mapper[Any]:
        return cast("Mapper[Any]", sa_inspect(self._model))

    @property
    def _pk_keys(self) -> tuple[str, ...]:
        """The attribute names of the primary-key columns, in key order."""
        mapper = self._mapper
        return tuple(mapper.get_property_by_column(column).key for column in mapper.primary_key)

    @property
    def _pk_attributes(self) -> tuple[Any, ...]:
        """The primary-key attributes of the model, in key order."""
        return tuple(getattr(self._model, key) for key in self._pk_keys)

    @property
    def _pk_column(self) -> Any:
        """The first primary-key attribute (the whole key of a single-column key)."""
        return self._pk_attributes[0]

    def _identity(self, id: Any) -> tuple[Any, ...]:
        """The primary-key tuple of *id*: a scalar for a single-column key; a tuple, list or mapping of key
        attribute names for a composite key (a scalar for a composite key raises ``TypeError``)."""
        keys = self._pk_keys
        if len(keys) == 1:
            return (id,)
        if isinstance(id, dict):
            try:
                return tuple(id[key] for key in keys)
            except KeyError as missing:
                raise TypeError(
                    f"{self._model.__name__} has the composite primary key ({', '.join(keys)}); the id {id!r} lacks "
                    f"{missing}"
                ) from None
        if isinstance(id, (tuple, list)) and len(id) == len(keys):
            return tuple(id)
        raise TypeError(
            f"{self._model.__name__} has the composite primary key ({', '.join(keys)}): pass each id as a tuple of "
            f"{len(keys)} values in that order (or a mapping by attribute name), not {id!r}"
        )

    def _key_value(self, identity: tuple[Any, ...]) -> Any:
        """What ``session.get`` and the IN helpers take for *identity*: the value, or the tuple."""
        return identity[0] if len(identity) == 1 else identity

    def _pk_equals(self, identity: tuple[Any, ...]) -> list[Any]:
        return [attribute == value for attribute, value in zip(self._pk_attributes, identity, strict=True)]

    def _in_ids(self, session: AsyncSession, identities: Sequence[tuple[Any, ...]]) -> list[Any]:
        """One IN criterion on the primary key per chunk (``statements.in_criteria``)."""
        values = [self._key_value(identity) for identity in identities]
        return in_criteria(self._pk_attributes, values, dialect_of(session))

    def _identity_of(self, entity: Any) -> tuple[Any, ...] | None:
        """The primary key of *entity*: its identity when it was persisted, else its key attributes' values
        (``None`` when they are not all set)."""
        state = _state(entity)
        if state.identity is not None:
            return tuple(state.identity)
        values = tuple(state.dict.get(key) for key in self._pk_keys)
        return None if any(value is None for value in values) else values

    def _is_new(self, entity: Any) -> bool:
        """Spring's rule: the entity's ``is_new`` hook, else a ``None`` version, else a ``None`` primary key."""
        hook = _persistable_hook(type(entity))
        if hook is not None:
            return hook(entity)
        mapper = self._mapper
        state = _state(entity)
        if mapper.version_id_col is not None:
            version_key = mapper.get_property_by_column(mapper.version_id_col).key
            return state.dict.get(version_key) is None
        return all(state.dict.get(key) is None for key in self._pk_keys)

    def _criteria(self) -> tuple[Any, ...]:
        """Criteria every read applies (``SoftDeleteRepository``: the row is not deleted)."""
        return ()

    def _visible(self, entity: Any) -> bool:
        """Whether an entity found by key is visible to the read methods."""
        return True

    def _filter_resolver(self) -> PropertyResolver:
        if self._filter_names is None:
            self._filter_names = PropertyResolver.for_entity(self._model, allowed=type(self).__filterable__)
        return self._filter_names

    def _sort_resolver(self) -> PropertyResolver:
        if self._sort_names is None:
            self._sort_names = PropertyResolver.for_entity(self._model, allowed=type(self).__sortable__)
        return self._sort_names

    def _filtered_select(self, **filters: Any) -> Select[Any]:
        """A ``SELECT`` of the model with the read criteria and equality *filters* (names validated)."""
        resolver = self._filter_resolver()
        stmt = select(self._model).where(*self._criteria())
        for key, value in filters.items():
            stmt = stmt.where(getattr(self._model, resolver.resolve(key, usage="filter")) == value)
        return stmt

    def _orders(self, session: AsyncSession, sort: Sort) -> list[Any]:
        """The ORDER BY expressions of *sort* (names validated, NULL placement and case rendered)."""
        return order_expressions(self._model, sort, dialect_of(session), resolver=self._sort_resolver())

    def _apply_orders(self, stmt: Select[Any], sort: Sort) -> Select[Any]:
        """Apply a :class:`Sort`'s orders to a ``SELECT`` statement (names validated; native NULL placement)."""
        resolver = self._sort_resolver()
        for order in sort.orders:
            col = getattr(self._model, resolver.resolve(order.property, usage="sort"))
            stmt = stmt.order_by(col.asc() if order.direction == "asc" else col.desc())
        return stmt

    def _load_options(self, load: FetchPlan | None) -> list[Any]:
        return loader_options(self._model, type(self).__load__ if load is None else load)

    def _check_lock(self, lock: LockMode) -> None:
        """A pessimistic lock needs a read-write transaction: refuse one in a read-only unit (a read method's
        auto unit outside a transaction, or ``@transactional(read_only=True)``), as JPA does."""
        if self._manual_session is not None:
            return
        state = current_state()
        unit = state.scope(self._datasource) or state.unit(self._datasource)
        if unit is not None and unit.read_only:
            raise IllegalTransactionStateError(
                f"{type(self).__name__}: lock={lock.name} needs a read-write transaction, and {unit.describe()} is "
                "read-only (a read method outside a transaction runs in a read-only auto unit). Call it inside "
                "@transactional, where the lock lasts until the transaction ends.",
                datasource=self._datasource,
            )

    # ------------------------------------------------------------------
    # CrudRepository
    # ------------------------------------------------------------------

    async def save(self, entity: T) -> T:
        """Persist a new entity or merge an existing one (Spring ``save``), and return the managed instance.

        Use the returned instance: for an entity that was not new and not attached it is the unit's own copy.
        """
        session = self._session
        managed = await self._attach(session, entity)
        await session.flush()
        await self._load_generated(session, [managed])
        return managed

    async def save_all(self, entities: Iterable[T]) -> list[T]:
        """Persist or merge every entity with one flush, and return the managed instances in order.

        Entities that carry an existing primary key are looked up with one ``SELECT`` for all of them.
        """
        session = self._session
        items = list(entities)
        managed = await self._attach_all(session, items)
        await session.flush()
        await self._load_generated(session, managed)
        return managed

    async def _attach(self, session: AsyncSession, entity: T) -> T:
        """Make *entity* part of *session* (module documentation) and return the managed instance."""
        sync_session = session.sync_session
        state = _state(entity)
        owner = state.session  # the live session that holds the entity, if any
        if owner is sync_session:
            return entity  # already pending or persistent here: its changes flush with the unit
        if owner is None and state.key is not None and not state.was_deleted:
            if state.key not in sync_session.identity_map:
                session.add(entity)  # re-attach a detached entity: what changed since it was loaded is updated
                return entity
        elif owner is None and state.key is None and self._is_new(entity):
            session.add(entity)
            return entity
        return cast(T, await session.merge(entity))

    async def _attach_all(self, session: AsyncSession, entities: list[T]) -> list[T]:
        """:meth:`_attach` for every entity, looking up the keys of the entities to merge in one ``SELECT``."""
        sync_session = session.sync_session
        mapper = self._mapper
        pending: list[tuple[int, tuple[Any, ...]]] = []
        for index, entity in enumerate(entities):
            state = _state(entity)
            if state.session is None and state.key is None and not self._is_new(entity):
                identity = self._identity_of(entity)
                if identity is not None and mapper.identity_key_from_primary_key(identity) not in (
                    sync_session.identity_map
                ):
                    pending.append((index, identity))
        found: set[tuple[Any, ...]] = set()
        if len(pending) > 1:
            for criterion in self._in_ids(session, [identity for _index, identity in pending]):
                rows = unique_entities(await session.execute(select(self._model).where(criterion)))
                found.update(_identity_key(row) for row in rows)
        missing = {index for index, identity in pending if identity not in found} if len(pending) > 1 else set()
        managed: list[T] = []
        for index, entity in enumerate(entities):
            if index in missing:
                session.add(entity)  # its key is not in the table: a new entity with an assigned key
                managed.append(entity)
            else:
                managed.append(await self._attach(session, entity))
        return managed

    async def _load_generated(self, session: AsyncSession, entities: Sequence[Any]) -> None:
        """Load the column values the database generated in the flush and did not return (a server default on a
        backend without ``RETURNING``, a server-side ``onupdate``): one ``SELECT`` of just those columns per key
        chunk, and nothing at all when every value came back, which is the usual case."""
        mapper = self._mapper
        columns = {attribute.key for attribute in mapper.column_attrs}
        stale: dict[tuple[Any, ...], tuple[Any, set[str]]] = {}
        for entity in entities:
            state = _state(entity)
            expired = set(state.expired_attributes) & columns
            if expired and state.identity is not None:
                stale[tuple(state.identity)] = (entity, expired)
        if not stale:
            return
        keys = sorted(set().union(*(expired for _entity, expired in stale.values())))
        width = len(self._pk_keys)
        for criterion in self._in_ids(session, list(stale)):
            statement = select(*self._pk_attributes, *(getattr(self._model, key) for key in keys)).where(criterion)
            for row in (await session.execute(statement)).all():
                entry = stale.get(tuple(row[:width]))
                if entry is None:
                    continue
                entity, expired = entry
                for key, value in zip(keys, row[width:], strict=True):
                    if key in expired:
                        set_committed_value(entity, key, value)

    async def find_by_id(self, id: ID, *, load: FetchPlan | None = None, lock: LockMode | None = None) -> T | None:
        """Find an entity by its primary key (a tuple for a composite key).

        *load* is a fetch plan for its relationships; *lock* takes a pessimistic lock on its row until the
        transaction ends (and reads the row again even when the unit had it already).
        """
        return await self._select_by_id(self._session, id, load=load, lock=lock)

    async def _select_by_id(
        self, session: AsyncSession, id: Any, *, load: FetchPlan | None = None, lock: LockMode | None = None
    ) -> T | None:
        identity = self._identity(id)
        options = self._load_options(load)
        if lock is not None:
            self._check_lock(lock)
        if not options and lock is None:
            entity = await session.get(self._model, self._key_value(identity))
            return entity if entity is not None and self._visible(entity) else None
        stmt = select(self._model).where(*self._pk_equals(identity), *self._criteria()).options(*options)
        if lock is not None:
            stmt = lock.apply(stmt).execution_options(populate_existing=True)
        found = unique_entities(await session.execute(stmt))
        return found[0] if found else None

    async def find_all_by_id(self, ids: Iterable[ID], *, load: FetchPlan | None = None) -> list[T]:
        """Find all entities with ids in *ids* (Spring ``findAllById``); ids that match nothing are skipped.

        The ids are sent in chunks the dialect accepts, in the same unit of work.
        """
        identities = [self._identity(id) for id in ids]
        if not identities:
            return []
        session = self._session
        options = self._load_options(load)
        found: list[T] = []
        seen: set[int] = set()
        for criterion in self._in_ids(session, identities):
            stmt = select(self._model).where(criterion, *self._criteria()).options(*options)
            for entity in unique_entities(await session.execute(stmt)):
                if id(entity) not in seen:
                    seen.add(id(entity))
                    found.append(entity)
        return found

    async def exists_by_id(self, id: ID) -> bool:
        """Check whether an entity with the given id exists (Spring ``existsById``).

        An entity the unit holds already answers without a statement; otherwise ``SELECT 1 ... LIMIT 1``. It
        does not go through ``find_by_id``: a framework method holds the unit's operation guard for the whole
        call, and never calls a method a subclass may override while it does (an override that fans out with
        ``gather()`` would wait for the guard its own caller holds).
        """
        session = self._session
        identity = self._identity(id)
        sync_session = session.sync_session
        held = sync_session.identity_map.get(self._mapper.identity_key_from_primary_key(identity))
        if held is not None and held not in sync_session.deleted:
            answer = self._held_visible(held)
            if answer is not None:
                return answer
        return await exists(session, exists_probe(self._model, *self._pk_equals(identity), *self._criteria()))

    def _held_visible(self, entity: Any) -> bool | None:
        """Whether an entity the unit holds is visible, or ``None`` when that needs the database."""
        return True

    async def count(self) -> int:
        """Return the total number of entities."""
        session = self._session
        stmt = select(func.count()).select_from(self._model).where(*self._criteria())
        result = await session.execute(stmt)
        return int(result.scalar_one())

    async def delete(self, entity: T) -> None:
        """Delete an entity (Spring ``delete(entity)``): a new entity is ignored, and so is one whose row is
        gone; a detached entity whose version is stale raises ``StaleDataError``. Cascades run."""
        session = self._session
        await self._delete_entities(session, [entity])
        await session.flush()

    async def delete_by_id(self, id: ID) -> None:
        """Delete an entity by its primary key (Spring ``deleteById``; a missing id is ignored). Cascades run."""
        session = self._session
        entity = await session.get(self._model, self._key_value(self._identity(id)))
        if entity is not None:
            await session.delete(entity)
            await session.flush()

    async def delete_all_by_id(self, ids: Iterable[ID]) -> None:
        """Delete all entities whose ids are in ``ids`` (Spring ``deleteAllById``).

        Entity by entity through the ORM (cascades, version checks, delete listeners), after loading them with
        one ``SELECT`` per id chunk; one bulk ``DELETE`` per chunk when the mapper has none of those.
        """
        identities = [self._identity(id) for id in ids]
        if not identities:
            return
        session = self._session
        if bulk_delete_safe(self._model, session):
            await self._bulk_delete(session, self._in_ids(session, identities))
            return
        for entity in await self._load_identities(session, identities, deleting=True):
            await session.delete(entity)
        await session.flush()

    async def delete_all(self, entities: Iterable[T] | None = None) -> None:
        """Delete the given entities, or ALL rows when ``entities`` is ``None`` (Spring ``deleteAll``).

        Entity by entity through the ORM, so cascades, version checks and delete listeners run; deleting every
        row sends one bulk ``DELETE`` when the mapper has none of those. ``delete_all_in_batch`` is the explicit
        bulk form.
        """
        session = self._session
        if entities is not None:
            await self._delete_entities(session, list(entities))
            await session.flush()
            return
        if bulk_delete_safe(self._model, session):
            await self._bulk_delete(session, [None])
            return
        for entity in unique_entities(await session.execute(select(self._model).options(*self._delete_loads()))):
            await session.delete(entity)
        await session.flush()

    async def delete_all_in_batch(self, entities: Iterable[T] | None = None) -> None:
        """Delete the given entities, or every row, with bulk ``DELETE`` statements (Spring
        ``deleteAllInBatch``): no cascade, version check or delete listener runs, by design."""
        session = self._session
        if entities is None:
            await self._bulk_delete(session, [None])
            return
        identities = [identity for entity in entities if (identity := self._identity_of(entity)) is not None]
        if identities:
            await self._bulk_delete(session, self._in_ids(session, identities))

    async def delete_all_by_id_in_batch(self, ids: Iterable[ID]) -> None:
        """Delete the rows with these ids with bulk ``DELETE`` statements (Spring ``deleteAllByIdInBatch``): no
        cascade, version check or delete listener runs, by design."""
        identities = [self._identity(id) for id in ids]
        if identities:
            session = self._session
            await self._bulk_delete(session, self._in_ids(session, identities))

    async def _bulk_delete(self, session: AsyncSession, criteria: Sequence[Any]) -> None:
        """One ``DELETE`` per criterion (``None``: every row). Entities the unit holds are synchronized only when
        it holds any of this model (otherwise there is nothing to synchronize, and no extra statement)."""
        synchronize: str | bool = "auto" if self._holds_entities(session) else False
        for criterion in criteria:
            stmt = sa_delete(self._model)
            if criterion is not None:
                stmt = stmt.where(criterion)
            await session.execute(stmt.execution_options(synchronize_session=synchronize))

    def _holds_entities(self, session: AsyncSession) -> bool:
        return any(isinstance(entity, self._model) for entity in session.sync_session.identity_map.values())

    def _delete_loads(self) -> list[Any]:
        """``selectin`` loads of the collections deleting an entity cascades to or nulls out, so the flush does
        not load them one entity at a time."""
        return [
            selectinload(getattr(self._model, relationship.key))
            for relationship in self._mapper.relationships
            if relationship.uselist and not relationship.viewonly and not relationship.passive_deletes
        ]

    async def _load_identities(
        self, session: AsyncSession, identities: Sequence[tuple[Any, ...]], *, deleting: bool = False
    ) -> list[Any]:
        """The entities with these keys: the unit's own, then one ``SELECT`` per chunk for the rest (with the
        collections a delete needs, when *deleting*)."""
        sync_session = session.sync_session
        mapper = self._mapper
        entities: list[Any] = []
        missing: list[tuple[Any, ...]] = []
        for identity in identities:
            held = sync_session.identity_map.get(mapper.identity_key_from_primary_key(identity))
            if held is not None:
                entities.append(held)
            else:
                missing.append(identity)
        options = self._delete_loads() if deleting else []
        for criterion in self._in_ids(session, missing):
            stmt = select(self._model).where(criterion).options(*options)
            entities.extend(unique_entities(await session.execute(stmt)))
        return entities

    async def _delete_entities(self, session: AsyncSession, entities: list[Any]) -> None:
        """Mark *entities* deleted in *session* (Spring's ``delete`` for each): the unit's own instances as they
        are; any other by its key, with the version it carries checked against the row's."""
        sync_session = session.sync_session
        mapper = self._mapper
        others: list[tuple[Any, tuple[Any, ...]]] = []
        for entity in entities:
            state = _state(entity)
            if state.session is sync_session:
                if state.key is None:
                    session.expunge(entity)  # pending: it is simply not inserted
                else:
                    await session.delete(entity)
                continue
            identity = self._identity_of(entity)
            if identity is None or (state.key is None and self._is_new(entity)):
                continue  # a new entity: nothing to delete
            others.append((entity, identity))
        if not others:
            return
        current = {
            _identity_key(held): held
            for held in await self._load_identities(session, [identity for _entity, identity in others], deleting=True)
        }
        version = mapper.version_id_col
        version_key = mapper.get_property_by_column(version).key if version is not None else None
        for entity, identity in others:
            held = current.get(identity)
            if held is None:
                continue  # the row is gone: Spring ignores it
            if version_key is not None and version_key in _state(entity).dict:
                expected, actual = getattr(entity, version_key), getattr(held, version_key)
                if expected != actual:
                    raise StaleDataError(
                        f"DELETE of {self._model.__name__} {self._key_value(identity)!r} expected version "
                        f"{expected!r}, and the stored version is {actual!r}: the entity was changed since it was read"
                    )
            await session.delete(held)

    # ------------------------------------------------------------------
    # ReactiveSortingRepository + PagingAndSortingRepository
    # ------------------------------------------------------------------

    @overload
    async def find_all(self, criteria: None = ..., *, load: FetchPlan | None = ..., **filters: Any) -> list[T]: ...
    @overload
    async def find_all(self, criteria: Sort, *, load: FetchPlan | None = ..., **filters: Any) -> list[T]: ...
    @overload
    async def find_all(self, criteria: Pageable, *, load: FetchPlan | None = ..., **filters: Any) -> Page[T]: ...
    async def find_all(
        self, criteria: Sort | Pageable | None = None, *, load: FetchPlan | None = None, **filters: Any
    ) -> list[T] | Page[T]:
        """Spring ``findAll`` family.

        - ``find_all()`` / ``find_all(status="X")`` → ``list[T]`` (optionally filtered)
        - ``find_all(Sort.by("name"))`` → sorted ``list[T]``
        - ``find_all(Pageable.of(1, 20))`` → ``Page[T]``
        """
        session = self._session
        stmt = self._filtered_select(**filters)
        if isinstance(criteria, Pageable):
            return await self._page(session, stmt, criteria, load)
        if isinstance(criteria, Sort):
            stmt = stmt.order_by(*self._orders(session, criteria))
        return unique_entities(await session.execute(stmt.options(*self._load_options(load))))

    async def find_slice(self, pageable: Pageable, *, load: FetchPlan | None = None, **filters: Any) -> Slice[T]:
        """A page and whether another one follows, with no ``COUNT`` (one query with ``LIMIT size + 1``)."""
        session = self._session
        return await self._slice(session, self._filtered_select(**filters), pageable, load)

    async def scroll(
        self,
        sort: Sort,
        position: KeysetPosition | None = None,
        *,
        size: int = 20,
        spec: Specification[T] | None = None,
        load: FetchPlan | None = None,
    ) -> Window[T]:
        """The next *size* entities in *sort* order after *position* (keyset paging: Spring's ``Window``).

        The window's ``next_position`` resumes after its last item; ``None`` starts at the beginning. The
        primary key breaks ties, so the scroll never skips or repeats a row, and its cost does not grow with
        the depth as an ``OFFSET`` does. The sort properties must not hold NULLs, and their orders must use
        the native NULL handling and no case folding.
        """
        if size < 1:
            raise ValueError(f"size must be >= 1, got {size}")
        session = self._session
        resolver = self._sort_resolver()
        orders = list(sort.orders)
        for order in orders:
            if order.null_handling is not NullHandling.NATIVE or order.ignore_case:
                raise ValueError(
                    f"scroll() compares keys, so order {order.property!r} cannot use null handling or ignore case"
                )
            resolver.resolve(order.property, usage="sort")
        named = {order.property for order in orders}
        orders += [Order.asc(key) for key in self._pk_keys if key not in named]
        properties = [order.property for order in orders]
        stmt = select(self._model).where(*self._criteria())
        if spec is not None:
            stmt = spec.to_predicate(self._model, stmt)
        if position is not None:
            try:
                values = [position.keys[name] for name in properties]
            except KeyError as missing:
                raise ValueError(f"The scroll position lacks the key {missing} (it needs {properties})") from None
            stmt = stmt.where(self._after(orders, values))
        stmt = stmt.order_by(*order_expressions(self._model, Sort(tuple(orders)), dialect_of(session)))
        stmt = stmt.limit(size + 1).options(*self._load_options(load))
        items = unique_entities(await session.execute(stmt))
        has_next = len(items) > size
        items = items[:size]
        after = KeysetPosition({name: getattr(items[-1], name) for name in properties}) if items else None
        return Window(items=items, has_next=has_next, next_position=after)

    def _after(self, orders: Sequence[Order], values: Sequence[Any]) -> Any:
        """The keyset predicate: rows after *values* in *orders* (an OR of prefix equalities, portable)."""
        columns = [getattr(self._model, order.property) for order in orders]
        branches = []
        for index, order in enumerate(orders):
            column, value = columns[index], values[index]
            beyond = column < value if order.direction == "desc" else column > value
            branches.append(and_(*(columns[j] == values[j] for j in range(index)), beyond))
        return or_(*branches)

    async def stream_all(
        self,
        criteria: Sort | None = None,
        *,
        load: FetchPlan | None = None,
        chunk_size: int | None = None,
        **filters: Any,
    ) -> AsyncIterator[T]:
        """Stream entities lazily (``Flux<T>`` analogue) via a server-side cursor.

        Rows are fetched in batches: *chunk_size* rows at a time when given, otherwise growing batches
        (:data:`STREAM_FIRST_BATCH` rows first, up to :data:`STREAM_BATCH_SIZE`): one fetch, and one pass of the
        unit's operation guard, per batch. A collection the entity loads eagerly with a join is loaded with
        ``selectin`` per batch instead (a streamed result cannot be made unique). On MySQL and MariaDB, where
        nothing else runs on a connection while its cursor is open, a stream that loads relationships per batch
        is read in full first. A stream closed before its end (``aclose()``, ``contextlib.aclosing``) closes its
        cursor at once, which frees the connection.
        """
        if chunk_size is not None and chunk_size < 1:
            raise ValueError(f"chunk_size must be >= 1, got {chunk_size}")
        session = self._session
        stmt = self._filtered_select(**filters)
        if criteria is not None:
            stmt = stmt.order_by(*self._orders(session, criteria))
        options = self._load_options(load)
        planned = stmt.options(*options)
        streamed = stream_safe(planned, self._model)
        loads_per_batch = bool(options) or streamed is not planned or self._eager_relationships()
        if loads_per_batch and not DataSourceCapabilities.of(dialect_of(session)).multiple_active_results:
            for entity in unique_entities(await session.execute(planned)):
                yield entity
            return
        result = await session.stream_scalars(streamed)
        try:
            size = chunk_size or STREAM_FIRST_BATCH
            while batch := await result.fetchmany(size):
                for row in batch:
                    yield row
                if chunk_size is None:
                    size = min(size * 5, STREAM_BATCH_SIZE)
        finally:
            await result.close()

    def _eager_relationships(self) -> bool:
        return any(
            relationship.lazy is False or relationship.lazy in _EAGER_LOADS
            for relationship in self._mapper.relationships
        )

    async def _page(
        self, session: AsyncSession, base: Select[Any], pageable: Pageable, load: FetchPlan | None
    ) -> Page[T]:
        content = base.order_by(*self._orders(session, pageable.sort)).options(*self._load_options(load))
        if pageable.is_paged:
            content = content.order_by(*primary_key_orders(self._model, pageable.sort))
            content = content.offset(pageable.offset).limit(pageable.size)
        items = unique_entities(await session.execute(content))
        total = _total_from_content(pageable, len(items))
        if total is None:
            total = int((await session.execute(select(func.count()).select_from(base.subquery()))).scalar_one())
        return Page(items=items, total=total, page=pageable.page, size=pageable.size)

    async def _slice(
        self, session: AsyncSession, base: Select[Any], pageable: Pageable, load: FetchPlan | None
    ) -> Slice[T]:
        content = base.order_by(*self._orders(session, pageable.sort)).options(*self._load_options(load))
        if not pageable.is_paged:
            items = unique_entities(await session.execute(content))
            return Slice(items=items, page=pageable.page, size=pageable.size, has_next=False)
        content = content.order_by(*primary_key_orders(self._model, pageable.sort))
        items = unique_entities(await session.execute(content.offset(pageable.offset).limit(pageable.size + 1)))
        return Slice(
            items=items[: pageable.size], page=pageable.page, size=pageable.size, has_next=len(items) > pageable.size
        )

    # ------------------------------------------------------------------
    # Specification extensions (PyFly)
    # ------------------------------------------------------------------

    async def find_all_by_spec(self, spec: Specification[T], *, load: FetchPlan | None = None) -> list[T]:
        """Find all entities matching the specification."""
        session = self._session
        stmt = spec.to_predicate(self._model, select(self._model).where(*self._criteria()))
        return unique_entities(await session.execute(stmt.options(*self._load_options(load))))

    async def find_all_by_spec_paged(
        self, spec: Specification[T], pageable: Pageable, *, load: FetchPlan | None = None
    ) -> Page[T]:
        """Find entities matching the specification with pagination and sorting."""
        session = self._session
        filtered = spec.to_predicate(self._model, select(self._model).where(*self._criteria()))
        return await self._page(session, filtered, pageable, load)

    async def find_slice_by_spec(
        self, spec: Specification[T], pageable: Pageable, *, load: FetchPlan | None = None
    ) -> Slice[T]:
        """A slice of the entities matching the specification (no ``COUNT``; see :meth:`find_slice`)."""
        session = self._session
        filtered = spec.to_predicate(self._model, select(self._model).where(*self._criteria()))
        return await self._slice(session, filtered, pageable, load)


def _total_from_content(pageable: Pageable, count: int) -> int | None:
    """The total a page's own content proves, or ``None`` when a ``COUNT`` is needed (Spring's
    ``PageableExecutionUtils``): an unpaged request, a first page shorter than its size, or any short page
    after it."""
    if not pageable.is_paged:
        return count
    if pageable.offset == 0:
        return count if count < pageable.size else None
    if 0 < count < pageable.size:
        return pageable.offset + count
    return None


async def _run_on(
    unit: UnitOfWork,
    function: Callable[..., Coroutine[Any, Any, Any]],
    repository: Repository[Any, Any],
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
    atomic: bool,
) -> Any:
    if not atomic:
        unit.check_usable()
        return await function(repository, *args, **kwargs)
    async with unit.guard:
        unit.check_usable()
        return await function(repository, *args, **kwargs)


# The framework's own methods are atomic operations; subclasses are wrapped by __init_subclass__.
_wrap_operations(Repository, atomic=True)
