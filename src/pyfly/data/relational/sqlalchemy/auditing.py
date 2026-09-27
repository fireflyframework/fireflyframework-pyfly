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
"""Entity auditing for SQLAlchemy entities: populates the audit columns of
:class:`~pyfly.data.relational.sqlalchemy.entity.BaseEntity` from ORM events.

:class:`AuditingEntityListener` is the relational :class:`~pyfly.data.auditing.AuditingHandler`: it
stamps with the application's :class:`~pyfly.data.auditing.AuditorAware` (who) and
:class:`~pyfly.data.auditing.DateTimeProvider` (when). The relational auto-configuration registers one
(``pyfly.data.auditing.enabled``, on by default); declare your own ``AuditingEntityListener`` bean to
replace it.

- **Insert**: ``created_at`` and ``updated_at`` are the provider's time; ``created_by`` and ``updated_by``
  the auditor, unless the application set them.
- **Update**: only an entity with a changed column is stamped; a change to one of its collections alone (a
  child added to ``order.lines``) issues no ``UPDATE`` of the parent row and no version bump. ``updated_at``
  is the provider's time, and ``updated_by`` is always the current auditor, ``None`` when there is none (a
  job with no principal does not leave the last user's name on its change), unless the application set it in
  this flush.

The ORM hooks are installed once per process, on the ``BaseEntity`` hierarchy, however many listeners
register: the listener registered last stamps (two application contexts in one process, one after the
other), and the hooks are removed when the last one unregisters (its context stopped). Registering again
changes nothing. An ``async`` ``AuditorAware`` is awaited inside the flush (``AsyncSession``); it must not use
the session being flushed.
"""

from __future__ import annotations

import inspect
import logging
from typing import Any

from sqlalchemy import event
from sqlalchemy import inspect as sa_inspect
from sqlalchemy.orm import object_session
from sqlalchemy.util import await_only
from sqlalchemy.util.concurrency import in_greenlet

from pyfly.data.auditing import (
    AuditingHandler,
    AuditorAware,
    DateTimeProvider,
    active_auditing_handler,
    registered_auditing_handlers,
)

logger = logging.getLogger(__name__)


class AuditingEntityListener(AuditingHandler):
    """Registers SQLAlchemy ORM events that populate the audit columns of every ``BaseEntity`` (module
    documentation).

    Args:
        auditor_aware: who is writing (default: the authenticated user of the security context).
        date_time_provider: the clock (default: UTC now).
    """

    def __init__(
        self, auditor_aware: AuditorAware | None = None, date_time_provider: DateTimeProvider | None = None
    ) -> None:
        super().__init__(auditor_aware, date_time_provider)

    def register(self) -> None:
        """Make this the listener that stamps ``BaseEntity`` flushes, installing the ORM hooks if they are
        not installed yet. Idempotent."""
        super().register()

    def _activated(self) -> None:
        from pyfly.data.relational.sqlalchemy.entity import BaseEntity

        if not event.contains(BaseEntity, "before_insert", _before_insert):
            event.listen(BaseEntity, "before_insert", _before_insert, propagate=True)
            event.listen(BaseEntity, "before_update", _before_update, propagate=True)
            logger.info("Registered entity auditing listeners on BaseEntity")

    def _deactivated(self, *, remaining: int) -> None:
        if any(isinstance(handler, AuditingEntityListener) for handler in registered_auditing_handlers()):
            return
        from pyfly.data.relational.sqlalchemy.entity import BaseEntity

        if event.contains(BaseEntity, "before_insert", _before_insert):
            event.remove(BaseEntity, "before_insert", _before_insert)
            event.remove(BaseEntity, "before_update", _before_update)
            logger.info("Removed entity auditing listeners from BaseEntity")

    def _on_insert(self, mapper: Any, connection: Any, target: Any) -> None:
        """Set all audit fields on a new entity, with this listener's auditor and clock."""
        _stamp_insert(self, target)

    def _on_update(self, mapper: Any, connection: Any, target: Any) -> None:
        """Update the modification timestamp and user of a changed entity, with this listener's auditor and
        clock."""
        _stamp_update(self, target)

    def _get_current_user(self) -> str | None:
        """The current auditor (see :meth:`~pyfly.data.auditing.AuditingHandler.current_auditor`), resolved
        synchronously: an ``async`` auditor is awaited inside an ``AsyncSession`` flush."""
        return _resolve_auditor(self)


def _before_insert(mapper: Any, connection: Any, target: Any) -> None:
    handler = active_auditing_handler()
    if handler is not None:
        _stamp_insert(handler, target)


def _before_update(mapper: Any, connection: Any, target: Any) -> None:
    handler = active_auditing_handler()
    if handler is not None:
        _stamp_update(handler, target)


def _stamp_insert(handler: AuditingHandler, target: Any) -> None:
    now = handler.now()
    target.created_at = now
    target.updated_at = now
    if target.created_by is None or target.updated_by is None:
        auditor = _resolve_auditor(handler)
        if target.created_by is None:
            target.created_by = auditor
        if target.updated_by is None:
            target.updated_by = auditor


def _stamp_update(handler: AuditingHandler, target: Any) -> None:
    session = object_session(target)
    if session is not None and not session.is_modified(target, include_collections=False):
        # Only a collection changed (a child added or removed): the parent row is not updated, so it is not
        # stamped either. Stamping it would issue an UPDATE of the parent, take its row lock and bump its
        # version, failing concurrent child inserts on a versioned parent (C121).
        return
    target.updated_at = handler.now()
    if not sa_inspect(target).attrs.updated_by.history.has_changes():
        target.updated_by = _resolve_auditor(handler)


def _resolve_auditor(handler: AuditingHandler) -> str | None:
    auditor = handler.current_auditor_or_awaitable()
    if not inspect.isawaitable(auditor):
        return auditor
    if not in_greenlet():
        if inspect.iscoroutine(auditor):
            auditor.close()
        raise TypeError(
            f"{type(handler.auditor_aware).__name__}.get_current_auditor() is asynchronous and the flush is "
            "synchronous: an async AuditorAware needs an AsyncSession (or return the auditor synchronously)"
        )
    return await_only(auditor)
