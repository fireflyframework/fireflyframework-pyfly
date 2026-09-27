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
  ``SoftDeleteRepository.find_by_id`` still checks ``deleted_at`` itself;
- the refresh of an object the session holds (``session.refresh()``, an expired attribute);
- ``UPDATE`` and ``DELETE`` statements, and raw ``text()`` SQL, as native queries in Spring.

Opt out for one statement with the ``include_deleted`` execution option, or for a block of code (a
retention job, an admin view) with :func:`including_deleted`::

    stmt = select(Order).where(Order.deleted_at < cutoff).execution_options(include_deleted=True)
    order = await session.get(Order, order_id, execution_options={"include_deleted": True})

    with including_deleted():
        deleted = await orders.find_all()

**Hard deletes.** Deleting a row for good must reach the soft-deleted rows that depend on it: a
``cascade="all, delete-orphan"`` child that was soft-deleted has to be deleted with its parent, and one
without a delete cascade has its foreign key set to ``NULL``, or the parent's ``DELETE`` violates the
foreign key. The repositories' hard deletes (``Repository.delete``, ``delete_by_id`` and
``delete_all(entities)``, ``SoftDeleteRepository.hard_delete``) do that through :func:`hard_delete`, which
loads what the cascades reach with the deleted rows, even for a root and collections loaded (filtered)
earlier in the session. A ``session.delete()`` of your own needs the same: call :func:`hard_delete`, or let
the database do it (``passive_deletes=True`` on the relationship and ``ON DELETE CASCADE`` on the foreign
key).
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, cast

from sqlalchemy import event, inspect
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import InstanceState, ORMExecuteState, Session, with_loader_criteria

from pyfly.data.relational.sqlalchemy.entity import SoftDeleteMixin

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
    are loaded with the deleted rows: the collections the session loaded before (without them) are expired
    and loaded again, inside :func:`including_deleted`. The flush runs inside the block too, since that is
    where the relationships without a delete cascade are loaded. Pending changes are flushed first.
    """
    await session.flush()
    with including_deleted():
        if _installed:
            await session.run_sync(_reveal_soft_deleted_dependents, instances)
        for instance in instances:
            await session.delete(instance)
        await session.flush()


def _reveal_soft_deleted_dependents(session: Session, instances: tuple[object, ...]) -> None:
    """Expire the loaded relationships to soft-delete entities of *instances* and of everything their delete
    cascades reach, loading those cascades on the way (with the deleted rows, the caller being inside
    :func:`including_deleted`), so that the delete and its flush see every dependent row."""
    for instance in instances:
        state = cast("InstanceState[Any]", inspect(instance))
        if state.session_id != session.hash_key:
            continue  # detached: session.delete() attaches it and loads its cascades, inside the block too
        _expire_soft_delete_relationships(session, state)
        # The iterator loads a relationship's value only when it gets to it, after it has yielded the
        # object that holds it: each object is expired before its own collections are loaded.
        for _child, _mapper, child_state, _dict in state.mapper.cascade_iterator("delete", state):
            _expire_soft_delete_relationships(session, child_state)


def _expire_soft_delete_relationships(session: Session, state: InstanceState[Any]) -> None:
    if state.key is None or state.session_id != session.hash_key:
        return  # pending, or not this session's: nothing was loaded through the criteria
    stale = [
        relationship.key
        for relationship in state.mapper.relationships
        if not relationship.viewonly
        and relationship.key in state.dict
        and any(issubclass(mapper.class_, SoftDeleteMixin) for mapper in relationship.mapper.self_and_descendants)
    ]
    if stale:
        session.expire(state.obj(), stale)
