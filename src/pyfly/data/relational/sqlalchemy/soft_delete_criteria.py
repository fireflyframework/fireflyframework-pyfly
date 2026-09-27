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
"""Soft-delete visibility at the ORM level: a soft-deleted row is invisible to every ORM load.

A :class:`~pyfly.data.relational.sqlalchemy.entity.SoftDeleteMixin` entity whose ``deleted_at`` is set
does not come back from any ORM ``SELECT``: a repository's reads (``SoftDeleteRepository``'s, a plain
``Repository``'s, derived and ``@query`` methods compiled to ORM statements), ``session.get()``, relationship
loads (selectin, joined and lazy collections, a many-to-one to a deleted parent, a refresh), and the
joins of a ``Specification`` or of any ORM statement, aliases included. That is Hibernate's
``@SoftDelete``/``@SQLRestriction`` behavior, done with ``with_loader_criteria`` in a ``do_orm_execute``
listener, so it is pure ORM and the same on every backend.

The listener is installed on SQLAlchemy's ``Session`` class the first time a ``SoftDeleteMixin`` entity is
declared, so it covers every session: the datasource registry's, an application's own session factory
(the primary transaction manager then runs its units on it), a session a test or script opens by hand. An
application without soft-delete entities pays nothing.

What it does not filter:

- an object already in the session's identity map (``session.get()`` returns it without SQL), which is why
  the repositories' ``find_by_id``, ``exists_by_id`` and ``delete_by_id`` check the ``deleted_at`` a held
  entity has themselves (a plain ``Repository`` outside :func:`including_deleted` only);
- the refresh of an object the session holds (``session.refresh()``, an expired attribute);
- ``UPDATE`` and ``DELETE`` statements, and raw ``text()`` SQL, as native queries in Spring;
- the ``EXISTS`` subqueries of ``relationship.any()`` and ``has()``: ``Author.books.any(Book.title == "x")``
  matches an author through a soft-deleted book. Add ``Book.deleted_at.is_(None)`` to the criterion.

A many-to-one loaded with an inner join (``joinedload(Book.author, innerjoin=True)``, or ``lazy="joined",
innerjoin=True``) to a soft-deleted parent drops the child row from the result, as Hibernate's
``@SQLRestriction`` does; the default outer join returns the child with the attribute ``None``.

``session.merge()`` loads through the criteria: a detached soft-deleted object finds no row, so the merge
INSERTs a copy that violates the primary key. Merge it inside :func:`including_deleted`.

Opt out for one statement with the ``include_deleted`` execution option, or for a block of code (a
retention job, an admin view) with :func:`including_deleted`; ``archive`` is a plain ``Repository`` over
the entity::

    stmt = select(Order).where(Order.deleted_at < cutoff).execution_options(include_deleted=True)
    order = await session.get(Order, order_id, execution_options={"include_deleted": True})

    with including_deleted():
        expired = await archive.find_by_deleted_at_less_than(cutoff)

The block lifts these criteria only: ``SoftDeleteRepository``'s own reads (``find_by_id``, ``find_all``,
``count``, ...) add ``deleted_at IS NULL`` themselves and keep excluding deleted rows inside it; its
``find_all_including_deleted()`` reads them.

**Hard deletes.** Deleting a row for good must reach the soft-deleted rows that depend on it: a
``cascade="all, delete-orphan"`` child that was soft-deleted has to be deleted with its parent, and one
without a delete cascade has its foreign key set to ``NULL``, or the parent's ``DELETE`` violates the
foreign key. The repositories' hard deletes (``Repository.delete``, ``delete_by_id``, ``delete_all_by_id``
and ``delete_all`` through the ORM, ``SoftDeleteRepository.hard_delete``) do that through :func:`hard_delete`,
which loads what the cascades reach with the deleted rows, even for a root and collections loaded (filtered)
earlier in the session, level by level and once for all the objects of a level. A ``session.delete()`` of
your own needs the same: call :func:`hard_delete`, or let the database do it (``passive_deletes=True`` on the
relationship and ``ON DELETE CASCADE`` on the foreign key).
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, cast

from sqlalchemy import event, inspect, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import (
    InstanceState,
    Mapper,
    ORMExecuteState,
    RelationshipProperty,
    Session,
    load_only,
    selectinload,
    with_loader_criteria,
)
from sqlalchemy.orm.interfaces import MANYTOONE

from pyfly.data.relational.sqlalchemy.entity import SoftDeleteMixin
from pyfly.data.relational.sqlalchemy.statements import in_criteria

INCLUDE_DELETED = "include_deleted"
"""The execution option that lets one statement see soft-deleted rows (``execution_options(include_deleted=True)``)."""

_including_deleted: ContextVar[bool] = ContextVar("pyfly_including_deleted", default=False)

_ACTIVE_ROWS = with_loader_criteria(
    SoftDeleteMixin,
    lambda cls: cls.deleted_at.is_(None),
    include_aliases=True,
)
"""The criteria every ORM ``SELECT`` gets: one option object, so a statement that carries it already (a
relationship load inherits the options of the load that brought its parent in) is recognized."""


@contextmanager
def including_deleted() -> Iterator[None]:
    """Let the ORM statements of this block (and of the tasks it starts) see soft-deleted rows.

    That includes the lazy loads of objects loaded before the block. What the session holds already is not
    reloaded: a collection loaded (without its deleted rows) before the block keeps its contents until it is
    expired, as an object in the identity map does.

    It lifts the loader criteria only: ``SoftDeleteRepository``'s own reads filter ``deleted_at`` themselves
    and keep excluding deleted rows here (``find_all_including_deleted()`` is their opt-out).
    """
    token = _including_deleted.set(True)
    try:
        yield
    finally:
        _including_deleted.reset(token)


def is_including_deleted() -> bool:
    """Whether the current code runs inside :func:`including_deleted`."""
    return _including_deleted.get()


def _hide_deleted_rows(state: ORMExecuteState) -> None:
    if not state.is_select or state.is_column_load:
        return
    statement: Any = state.statement
    inherited = state.is_relationship_load and _ACTIVE_ROWS in getattr(statement, "_with_options", ())
    if state.execution_options.get(INCLUDE_DELETED, False) or _including_deleted.get():
        if inherited:
            # The lazy load of an object loaded outside the block inherits the criteria from that load (they
            # propagate to loaders so that joined and selectin loads are filtered too): drop them here.
            revealed = statement._generate()
            revealed._with_options = tuple(option for option in statement._with_options if option is not _ACTIVE_ROWS)
            state.statement = revealed
        return
    if inherited:
        return  # inherited from the load that brought the parent in
    state.statement = statement.options(_ACTIVE_ROWS)


_installed = False


def install_soft_delete_criteria() -> None:
    """Install the listener on SQLAlchemy's ``Session`` class, once per process (idempotent).
    ``SoftDeleteMixin`` calls it when the first soft-delete entity is declared."""
    global _installed
    if not event.contains(Session, "do_orm_execute", _hide_deleted_rows):
        event.listen(Session, "do_orm_execute", _hide_deleted_rows)
    _installed = True


async def hard_delete(session: AsyncSession, *instances: object) -> None:
    """Delete *instances* for good, with everything their delete cascades reach, soft-deleted rows included,
    and flush.

    The soft-deleted children a cascade deletes, and the ones whose foreign key a delete sets to ``NULL``,
    are loaded with the deleted rows, inside :func:`including_deleted`: level by level (the instances, then
    the objects their cascades reach, and so on), the relationships the delete needs are loaded once for all
    the objects of a level, a ``SELECT`` of their keys and one per relationship (a lone object loads each
    relationship itself, as its flush would). A relationship to a soft-delete entity that the session loaded
    before (without the deleted rows) is loaded again. The flush runs inside the block too. Pending changes
    are flushed first.
    """
    await session.flush()
    with including_deleted():
        if _installed:
            await session.run_sync(_reveal_soft_deleted_dependents, instances)
        for instance in instances:
            await session.delete(instance)
        await session.flush()


def reaches_soft_deleted_rows(relationship: RelationshipProperty[Any]) -> bool:
    """Whether *relationship* loads a soft-delete entity (its target, or a subclass of it), whose deleted rows
    its loads hide."""
    return any(issubclass(mapper.class_, SoftDeleteMixin) for mapper in relationship.mapper.self_and_descendants)


_NOT_LOADED_BY_THE_WALK = (None, "noload", "dynamic", "write_only")
"""Relationship loading strategies whose values the reveal leaves to the delete's own cascade: nothing to load
(``noload``), or a query rather than a loaded collection (``dynamic``, ``write_only``)."""

_RAISES_ON_ACCESS = ("raise", "raise_on_sql")


def _delete_needs(mapper: Mapper[Any]) -> list[RelationshipProperty[Any]]:
    """The relationships the flush of a delete of *mapper*'s objects loads: those it cascades to, and the
    collections whose foreign keys it sets to ``NULL`` (unless ``passive_deletes`` leaves them to the
    database)."""
    return [
        relationship
        for relationship in mapper.relationships
        if not relationship.viewonly
        and not relationship.passive_deletes
        and (relationship.direction is not MANYTOONE or relationship.cascade.delete)
        and relationship.lazy not in _NOT_LOADED_BY_THE_WALK
    ]


def _reveal_soft_deleted_dependents(session: Session, instances: tuple[object, ...]) -> None:
    """Load what deleting *instances* needs, with the deleted rows (the caller is inside
    :func:`including_deleted`), level by level: the relationships the delete of each level's objects needs
    (:func:`_delete_needs`) for all of them at once, then the same for the objects their delete cascades
    reach. The loaded relationships to soft-delete entities are expired first: they may hold only the live
    rows.

    What the walk leaves out (a ``dynamic`` or ``write_only`` relationship), the delete's own cascade loads
    object by object, each object it reaches expired before its own collections are loaded."""
    seen: set[InstanceState[Any]] = set()
    level = [state for state in map(_state_of, instances) if _persistent_here(session, state)]
    while level:
        seen.update(level)
        by_mapper: dict[Mapper[Any], list[InstanceState[Any]]] = {}
        for state in level:
            by_mapper.setdefault(state.mapper, []).append(state)
        reached: dict[InstanceState[Any], None] = {}
        for mapper, states in by_mapper.items():
            needs = _delete_needs(mapper)
            _load_level(session, mapper, states, needs)
            for state in states:
                for relationship in needs:
                    if relationship.cascade.delete:
                        for child in _related_states(state, relationship):
                            if child not in seen and _persistent_here(session, child):
                                reached[child] = None
        level = list(reached)
    for state in map(_state_of, instances):
        if not _persistent_here(session, state):
            continue  # detached: session.delete() attaches it and loads its cascades, inside the block too
        # The iterator loads a relationship's value only when it gets to it, after it has yielded the
        # object that holds it: each object is expired before its own collections are loaded.
        for _child, _mapper, child_state, _dict in state.mapper.cascade_iterator("delete", state):
            if child_state not in seen:
                _expire_soft_delete_relationships(session, child_state)


def _load_level(
    session: Session, mapper: Mapper[Any], states: list[InstanceState[Any]], needs: list[RelationshipProperty[Any]]
) -> None:
    """Load *needs* on the objects of *states* (one mapper's objects of a level) that lack them, with one
    ``SELECT`` of their keys per chunk and one per relationship; the relationships to soft-delete entities are
    expired first. A lone object loads each relationship itself, one statement each, as its flush would."""
    soft = [relationship.key for relationship in needs if reaches_soft_deleted_rows(relationship)]
    for state in states:
        stale = [key for key in soft if key in state.dict]
        instance = state.obj()
        if stale and instance is not None:
            session.expire(instance, stale)
    lacking = [relationship for relationship in needs if any(relationship.key not in state.dict for state in states)]
    if not lacking:
        return
    if len(states) == 1 and all(relationship.lazy not in _RAISES_ON_ACCESS for relationship in lacking):
        instance = states[0].obj()
        if instance is not None:
            for relationship in lacking:
                getattr(instance, relationship.key)  # its lazy load
        return
    entity = mapper.class_
    keys = [getattr(entity, mapper.get_property_by_column(column).key) for column in mapper.primary_key]
    values = [identity[0] if len(identity) == 1 else identity for identity in (tuple(s.identity or ()) for s in states)]
    # The objects are in the session's identity map: the rows fill in only the relationships they lack.
    options = [load_only(*keys), *(selectinload(getattr(entity, relationship.key)) for relationship in lacking)]
    for criterion in in_criteria(keys, values, session.get_bind(mapper).dialect):
        session.execute(select(entity).where(criterion).options(*options)).scalars().all()


def _related_states(state: InstanceState[Any], relationship: RelationshipProperty[Any]) -> list[InstanceState[Any]]:
    """The states of the objects *state* holds in *relationship* (loaded), none when it is not loaded."""
    value = state.dict.get(relationship.key)
    if value is None:
        return []
    if not relationship.uselist:
        return [_state_of(value)]
    items = value.values() if isinstance(value, dict) else value
    return [_state_of(item) for item in items]


def _state_of(instance: object) -> InstanceState[Any]:
    return cast("InstanceState[Any]", inspect(instance))


def _persistent_here(session: Session, state: InstanceState[Any]) -> bool:
    """Whether *state* is persistent in *session* (so its relationships were loaded through the criteria): not
    pending, and not deleted either (a child deleted earlier in the unit stays in its parent's collection)."""
    return state.persistent and state.session_id == session.hash_key


def _expire_soft_delete_relationships(session: Session, state: InstanceState[Any]) -> None:
    if not _persistent_here(session, state):
        return  # pending, or not this session's: nothing was loaded through the criteria
    stale = [
        relationship.key
        for relationship in state.mapper.relationships
        if not relationship.viewonly and relationship.key in state.dict and reaches_soft_deleted_rows(relationship)
    ]
    if stale:
        session.expire(state.obj(), stale)
