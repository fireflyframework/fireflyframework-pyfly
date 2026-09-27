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
  (``find*``, ``count*``, ``exists*``, ``stream*``, ``get*``) gets a read unit (on PostgreSQL an
  ``AUTOCOMMIT`` connection, one round trip; elsewhere a short transaction that ends without writing) that
  is retried once when its connection turns out to be dead, and any other method a write unit that
  commits. Both run the datasource's after-begin customizers, and both always release their connection.

Nested repository calls inside one operation share its unit: a subclass method calling ``count()`` and
``exists_by_id()`` is one unit. The framework's own methods are atomic for a task that shares the unit
(they hold its operation guard for the whole call), so they never call a method a subclass may override.
Entities returned from an auto unit are detached with their loaded state intact (``expire_on_commit=False``);
lazy relationships need an explicit fetch.

Two modes:

- **managed** (a DI-built repository, or ``Repository(Model)``): the session is resolved per call as
  above, on the datasource named by ``datasource=`` or the class attribute ``__datasource__`` (default:
  the primary);
- **manual** (``Repository(Model, session)``): the caller owns that session and the repository uses it as
  is. Tests and scripts rely on this.

Custom methods keep working: ``self._session`` (and ``self._require_session()``) return the session of the
current operation scope.
"""

from __future__ import annotations

import functools
import inspect
import logging
from collections.abc import AsyncGenerator, AsyncIterator, Callable, Coroutine
from typing import Annotated, Any, ClassVar, Generic, TypeVar, cast, get_args, get_origin, overload

from sqlalchemy import Select, func, select
from sqlalchemy import delete as sa_delete
from sqlalchemy.ext.asyncio import AsyncSession

from pyfly.container.types import NoAutowire
from pyfly.data.page import Page
from pyfly.data.pageable import Pageable, Sort
from pyfly.data.relational.sqlalchemy.specification import Specification
from pyfly.data.transaction.context import bind_state, current_state, reset_state
from pyfly.data.transaction.errors import IllegalTransactionStateError
from pyfly.data.transaction.manager import TransactionManager
from pyfly.data.transaction.registry import PRIMARY, TransactionManagerRegistry, installed_registry
from pyfly.data.transaction.template import AutoUnit, complete_auto_unit
from pyfly.data.transaction.unit_of_work import UnitOfWork

T = TypeVar("T")
ID = TypeVar("ID")

_logger = logging.getLogger(__name__)

READ_PREFIXES: tuple[str, ...] = ("find", "count", "exists", "stream", "get")
"""A repository method whose name starts with one of these runs in a read auto unit outside a transaction."""


def is_read_method(name: str) -> bool:
    """Whether a repository method called *name* reads (and so gets a read auto unit)."""
    return name.startswith(READ_PREFIXES)


def repository_operation(
    function: Callable[..., Coroutine[Any, Any, Any]], *, read: bool, atomic: bool = False
) -> Callable[..., Coroutine[Any, Any, Any]]:
    """Wrap a repository coroutine method so every call runs in an operation scope (module documentation).

    *read* selects a read auto unit outside a transaction. *atomic* holds the unit's operation guard for the
    whole call: the framework's own methods are atomic (``save`` is add, flush and refresh as one step for a
    task that shares the unit), while a subclass method is not, since it may await anything.
    """

    @functools.wraps(function)
    async def operation(self: Repository[Any, Any], *args: Any, **kwargs: Any) -> Any:
        return await self._pyfly_run(function, args, kwargs, read=read, atomic=atomic)

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


class Repository(Generic[T, ID]):
    """Generic CRUD repository for SQLAlchemy entities.

    Implements the Spring-parity ``PagingAndSortingRepository`` contract
    (``CrudRepository`` → ``ReactiveSortingRepository`` → paging) with async
    support. Subclass with concrete type parameters to enable DI-managed
    repositories.

    Type Parameters:
        T: The entity type (any SQLAlchemy model).
        ID: The primary key type (e.g. UUID, int, str).

    Usage::

        class UserRepository(Repository[User, UUID]):
            pass  # entity type auto-extracted; the session is resolved per call

        class ReportRepository(Repository[Report, int]):
            __datasource__ = "reporting"   # this repository's units run on the 'reporting' datasource
    """

    _entity_type: type | None = None
    _id_type: type | None = None
    __datasource__: ClassVar[str | None] = None
    """The datasource a subclass's calls run on (``None``: the primary)."""

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        for base in getattr(cls, "__orig_bases__", []):
            origin = get_origin(base)
            # Repository[T, ID] or a generic subclass of it (SoftDeleteRepository[T, ID]).
            if isinstance(origin, type) and issubclass(origin, Repository):
                args = get_args(base)
                if args and not isinstance(args[0], TypeVar):
                    cls._entity_type = args[0]
                if len(args) > 1 and not isinstance(args[1], TypeVar):
                    cls._id_type = args[1]
                break
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
                f"{type(self).__name__} requires either Repository[Entity, ID] declaration or explicit model argument"
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
        if unit is None:
            unit = await self._transaction_manager().open_auto_unit(read_only=True, autocommit=False)
        else:
            unit.check_usable()
        inner = function(self, *args, **kwargs)
        error: BaseException | None = None
        try:
            while True:
                # The unit is bound only while the inner generator runs a step, never across a yield: the
                # consumer's own code between items is not inside this stream's unit.
                token = bind_state(current_state().with_scope(datasource, unit))
                try:
                    item = await anext(inner)
                except StopAsyncIteration:
                    break
                finally:
                    reset_state(token)
                yield item
        except BaseException as raised:
            error = raised
            raise
        finally:
            token = bind_state(current_state().with_scope(datasource, unit))
            try:
                await inner.aclose()
            finally:
                reset_state(token)
            if owned:
                await complete_auto_unit(unit, error)

    @property
    def _pk_column(self) -> Any:
        """Discover the primary key column dynamically."""
        from sqlalchemy import inspect as sa_inspect

        mapper = sa_inspect(self._model)
        if mapper is None:
            return self._model.id  # type: ignore[attr-defined]
        pk_cols = mapper.primary_key
        if pk_cols:
            return getattr(self._model, pk_cols[0].name)
        return self._model.id  # type: ignore[attr-defined]

    def _filtered_select(self, **filters: Any) -> Select[Any]:
        """Build a ``SELECT`` for the model with optional equality filters."""
        stmt = select(self._model)
        for key, value in filters.items():
            stmt = stmt.where(getattr(self._model, key) == value)
        return stmt

    def _apply_orders(self, stmt: Select[Any], sort: Sort) -> Select[Any]:
        """Apply a :class:`Sort`'s orders to a ``SELECT`` statement."""
        for order in sort.orders:
            col = getattr(self._model, order.property)
            stmt = stmt.order_by(col.asc() if order.direction == "asc" else col.desc())
        return stmt

    # ------------------------------------------------------------------
    # CrudRepository
    # ------------------------------------------------------------------

    async def save(self, entity: T) -> T:
        """Persist an entity (insert or update)."""
        session = self._require_session()
        session.add(entity)
        await session.flush()
        await session.refresh(entity)
        return entity

    async def save_all(self, entities: list[T]) -> list[T]:
        """Persist multiple entities in a single batch."""
        session = self._require_session()
        session.add_all(entities)
        await session.flush()
        for entity in entities:
            await session.refresh(entity)
        return entities

    async def find_by_id(self, id: ID) -> T | None:
        """Find an entity by its primary key."""
        return await self._select_by_id(id)

    async def find_all_by_id(self, ids: list[ID]) -> list[T]:
        """Find all entities with IDs in the given list (Spring ``findAllById``)."""
        if not ids:
            return []
        session = self._require_session()
        stmt = select(self._model).where(self._pk_column.in_(ids))
        result = await session.execute(stmt)
        return list(result.scalars().all())

    async def exists_by_id(self, id: ID) -> bool:
        """Check whether an entity with the given id exists (Spring ``existsById``).

        It does not go through ``find_by_id``: a framework method holds the unit's operation guard for the
        whole call, and never calls a method a subclass may override while it does (an override that fans
        out with ``gather()`` would wait for the guard its own caller holds).
        """
        return await self._select_by_id(id) is not None

    async def _select_by_id(self, id: ID) -> T | None:
        session = self._require_session()
        return await session.get(self._model, id)

    async def count(self) -> int:
        """Return the total number of entities."""
        session = self._require_session()
        stmt = select(func.count()).select_from(self._model)
        result = await session.execute(stmt)
        return result.scalar_one()

    async def delete(self, entity: T) -> None:
        """Delete a managed entity instance (Spring ``delete(entity)``)."""
        session = self._require_session()
        await session.delete(entity)
        await session.flush()

    async def delete_by_id(self, id: ID) -> None:
        """Delete an entity by its primary key (Spring ``deleteById``)."""
        session = self._require_session()
        entity = await session.get(self._model, id)
        if entity is not None:
            await session.delete(entity)
            await session.flush()

    async def delete_all_by_id(self, ids: list[ID]) -> None:
        """Delete all entities whose ids are in ``ids`` (Spring ``deleteAllById``)."""
        if not ids:
            return
        session = self._require_session()
        await session.execute(sa_delete(self._model).where(self._pk_column.in_(ids)))
        await session.flush()

    async def delete_all(self, entities: list[T] | None = None) -> None:
        """Delete the given entities, or ALL rows when ``entities`` is ``None`` (Spring ``deleteAll``)."""
        session = self._require_session()
        if entities is None:
            await session.execute(sa_delete(self._model))
        else:
            for entity in entities:
                await session.delete(entity)
        await session.flush()

    # ------------------------------------------------------------------
    # ReactiveSortingRepository + PagingAndSortingRepository
    # ------------------------------------------------------------------

    @overload
    async def find_all(self, criteria: None = ..., **filters: Any) -> list[T]: ...
    @overload
    async def find_all(self, criteria: Sort, **filters: Any) -> list[T]: ...
    @overload
    async def find_all(self, criteria: Pageable, **filters: Any) -> Page[T]: ...
    async def find_all(self, criteria: Sort | Pageable | None = None, **filters: Any) -> list[T] | Page[T]:
        """Spring ``findAll`` family.

        - ``find_all()`` / ``find_all(status="X")`` → ``list[T]`` (optionally filtered)
        - ``find_all(Sort.by("name"))`` → sorted ``list[T]``
        - ``find_all(Pageable.of(1, 20))`` → ``Page[T]``
        """
        if isinstance(criteria, Pageable):
            return await self._find_page(criteria, **filters)
        session = self._require_session()
        stmt = self._filtered_select(**filters)
        if isinstance(criteria, Sort):
            stmt = self._apply_orders(stmt, criteria)
        result = await session.execute(stmt)
        return list(result.scalars().all())

    async def stream_all(self, criteria: Sort | None = None, **filters: Any) -> AsyncIterator[T]:
        """Stream entities lazily (``Flux<T>`` analogue) via a server-side cursor."""
        session = self._require_session()
        stmt = self._filtered_select(**filters)
        if criteria is not None:
            stmt = self._apply_orders(stmt, criteria)
        result = await session.stream_scalars(stmt)
        async for row in result:
            yield row

    async def _find_page(self, pageable: Pageable, **filters: Any) -> Page[T]:
        session = self._require_session()
        base = self._filtered_select(**filters)
        count_stmt = select(func.count()).select_from(base.subquery())
        total = (await session.execute(count_stmt)).scalar_one()
        stmt = self._apply_orders(base, pageable.sort).offset(pageable.offset).limit(pageable.size)
        items = list((await session.execute(stmt)).scalars().all())
        return Page(items=items, total=total, page=pageable.page, size=pageable.size)

    # ------------------------------------------------------------------
    # Specification extensions (PyFly)
    # ------------------------------------------------------------------

    async def find_all_by_spec(self, spec: Specification[T]) -> list[T]:
        """Find all entities matching the specification."""
        session = self._require_session()
        stmt = select(self._model)
        stmt = spec.to_predicate(self._model, stmt)
        result = await session.execute(stmt)
        return list(result.scalars().all())

    async def find_all_by_spec_paged(self, spec: Specification[T], pageable: Pageable) -> Page[T]:
        """Find entities matching the specification with pagination and sorting."""
        session = self._require_session()
        base = select(self._model)
        filtered = spec.to_predicate(self._model, base)
        count_stmt = select(func.count()).select_from(filtered.subquery())
        total = (await session.execute(count_stmt)).scalar_one()
        stmt = self._apply_orders(filtered, pageable.sort).offset(pageable.offset).limit(pageable.size)
        items = list((await session.execute(stmt)).scalars().all())
        return Page(items=items, total=total, page=pageable.page, size=pageable.size)


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
