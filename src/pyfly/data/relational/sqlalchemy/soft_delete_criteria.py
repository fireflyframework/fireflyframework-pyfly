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
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

from sqlalchemy import event
from sqlalchemy.orm import ORMExecuteState, Session, with_loader_criteria

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
    """Let the ORM statements of this block (and of the tasks it starts) see soft-deleted rows."""
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
    if state.execution_options.get(INCLUDE_DELETED, False) or _including_deleted.get():
        return
    statement: Any = state.statement
    if state.is_relationship_load and _ACTIVE_ROWS in getattr(statement, "_with_options", ()):
        return  # inherited from the load that brought the parent in
    state.statement = statement.options(_ACTIVE_ROWS)


def install_soft_delete_criteria() -> None:
    """Install the listener on SQLAlchemy's ``Session`` class, once per process (idempotent).
    ``SoftDeleteMixin`` calls it when the first soft-delete entity is declared."""
    if not event.contains(Session, "do_orm_execute", _hide_deleted_rows):
        event.listen(Session, "do_orm_execute", _hide_deleted_rows)
