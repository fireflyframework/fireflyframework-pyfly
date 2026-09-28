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
"""Base documents: audit fields maintained on every write, and aggregates that raise domain events.

:class:`BaseDocument` carries ``created_at``/``updated_at``/``created_by``/``updated_by`` and keeps them
current (C107) through Beanie event actions, with the application's
:class:`~pyfly.data.auditing.AuditorAware` (who) and :class:`~pyfly.data.auditing.DateTimeProvider` (when),
as the relational ``BaseEntity`` does:

- **Insert** (``insert``, ``save`` of a new document, ``MongoRepository.save``/``save_all``): ``created_at`` and
  ``updated_at`` are the provider's time; ``created_by`` and ``updated_by`` the auditor, unless the application
  set them.
- **Update** (``save`` of a stored document, ``replace``, ``update``/``set``, ``save_changes`` when something
  changed): ``updated_at`` is the provider's time, and ``updated_by`` the current auditor (``None`` when there
  is none), unless the application changed it itself (known with state management).

The hooks stamp only while auditing is enabled (``pyfly.data.auditing.enabled``, on by default: the document
auto-configuration registers a :class:`DocumentAuditingHandler`); run a job's writes under
:func:`~pyfly.data.auditing.run_as` to name its principal. A bulk update through a query
(``Model.find(...).update(...)``) sends one command and runs no document hooks, as a bulk ``UPDATE`` does on
SQL: stamp ``updated_at`` and ``updated_by`` in it yourself (:func:`~pyfly.data.auditing.current_auditor`).

Timestamps are aware UTC ``datetime`` values (the Mongo lane of the timestamp contract): a naive value is taken
as UTC, and a stamped time is cut to the millisecond BSON stores, so what a save returns equals what a load
reads. The document client is built with ``tz_aware=True`` for the same reason.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, cast

from beanie import Document, Insert, Replace, Save, SaveChanges, Update, before_event
from pydantic import Field, PrivateAttr, field_validator

from pyfly.data.auditing import AuditingHandler, active_auditing_handler
from pyfly.domain.aggregate_root import AggregateRoot
from pyfly.domain.domain_event import DomainEvent


def _to_millisecond(moment: datetime) -> datetime:
    """*moment* as an aware UTC value cut to the millisecond (BSON's precision)."""
    aware = moment.replace(tzinfo=UTC) if moment.tzinfo is None else moment.astimezone(UTC)
    return aware.replace(microsecond=aware.microsecond - aware.microsecond % 1000)


def _now() -> datetime:
    handler = active_auditing_handler()
    return _to_millisecond(handler.now() if handler is not None else datetime.now(UTC))


class DocumentAuditingHandler(AuditingHandler):
    """The document backend's :class:`~pyfly.data.auditing.AuditingHandler`: while it is the registered one,
    :class:`BaseDocument` writes are stamped with its auditor and clock (the hooks are Beanie event actions of
    ``BaseDocument``, always present; they consult :func:`~pyfly.data.auditing.active_auditing_handler`)."""


class BaseDocument(Document):
    """Base document providing audit trail fields, kept current on every write (see the module documentation).

    The ``id`` field is inherited from :class:`beanie.Document` as a ``PydanticObjectId``. Subclasses configure
    their collection name via ``class Settings: name = "..."`` (a subclass's ``Settings`` replaces this one, so
    it declares ``use_state_management = True`` again when it wants Beanie's change tracking).

    Example::

        class UserDocument(BaseDocument):
            name: str
            email: str

            class Settings:
                name = "users"
                use_state_management = True
    """

    created_at: datetime = Field(default_factory=_now)
    updated_at: datetime = Field(default_factory=_now)
    created_by: str | None = None
    updated_by: str | None = None

    _pyfly_stamped: bool = PrivateAttr(default=False)

    class Settings:
        use_state_management = True

    @field_validator("created_at", "updated_at", mode="after")
    @classmethod
    def _aware_utc(cls, value: datetime) -> datetime:
        """A naive timestamp is UTC; an aware one is converted to UTC."""
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)

    # Beanie runs only public event actions (it skips names that start with "_").

    @before_event(Insert)
    async def pyfly_audit_insert(self) -> None:
        """Stamp a new document (``insert``)."""
        await _stamp(self, new=True)

    @before_event(Save)
    async def pyfly_audit_save(self) -> None:
        """Stamp a document ``save`` writes (new or stored); the update ``save`` runs is not stamped again."""
        await _stamp(self, new=_is_new(self))
        self._pyfly_stamped = True

    @before_event(SaveChanges)
    async def pyfly_audit_save_changes(self) -> None:
        """Stamp a document ``save_changes`` writes, only when something changed (nothing is written otherwise)."""
        if self.is_changed:
            await _stamp(self, new=False)
            self._pyfly_stamped = True

    @before_event(Replace)
    async def pyfly_audit_replace(self) -> None:
        """Stamp a document ``replace`` writes."""
        await _stamp(self, new=False)

    @before_event(Update)
    async def pyfly_audit_update(self) -> None:
        """Stamp a document ``update``/``set`` writes, unless ``save`` or ``save_changes`` stamped it already."""
        if self._pyfly_stamped:
            self._pyfly_stamped = False
            return
        await _stamp(self, new=False)


def _is_new(document: BaseDocument) -> bool:
    if document.id is None:
        return True
    return document.use_state_management() and document.get_saved_state() is None


async def _stamp(document: BaseDocument, *, new: bool) -> None:
    handler = active_auditing_handler()
    if handler is None:
        return
    now = _to_millisecond(handler.now())
    document.updated_at = now
    if new:
        document.created_at = now
        if document.created_by is None or document.updated_by is None:
            auditor = await handler.current_auditor()
            if document.created_by is None:
                document.created_by = auditor
            if document.updated_by is None:
                document.updated_by = auditor
        return
    saved = document.get_saved_state() if document.use_state_management() else None
    if saved is not None and saved.get("updated_by") != document.updated_by:
        return  # the application set it in this change
    document.updated_by = await handler.current_auditor()


class AggregateDocument(BaseDocument):
    """A document that is an aggregate root: it raises :class:`~pyfly.domain.DomainEvent` instances that are
    published when the unit of work that saves it commits (the relational ``AggregateRoot``'s contract, for
    Beanie, whose documents cannot inherit ``AggregateRoot``'s slots).

    An event raised inside a unit of work is tied to that unit; the pending events of a document
    :class:`~pyfly.data.document.mongodb.repository.MongoRepository` saves are tied to the unit of the save.
    The application's ``DomainEventPublisher`` publishes them as the unit commits; a unit that rolls back
    publishes nothing, and the events stay pending on the document::

        class Order(AggregateDocument):
            status: str = "NEW"

            def confirm(self) -> None:
                self.status = "CONFIRMED"
                self.raise_event(OrderConfirmed(order_id=str(self.id)))
    """

    _pending_events: list[DomainEvent] = PrivateAttr(default_factory=list)

    def _events(self) -> list[DomainEvent]:
        return self._pending_events

    def raise_event(self, event: DomainEvent) -> None:
        """Queue *event* for publication when the unit of work commits."""
        AggregateRoot.raise_event(cast(Any, self), event)

    def pending_events(self) -> list[DomainEvent]:
        """A snapshot of the pending events."""
        return list(self._pending_events)

    def clear_events(self) -> list[DomainEvent]:
        """Drain the pending events and return them."""
        events = self._pending_events
        self._pending_events = []
        return events
