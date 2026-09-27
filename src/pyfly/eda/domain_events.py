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
"""Publishing the events aggregates raise, when their unit of work commits (Spring Data's domain events).

A :class:`~pyfly.domain.AggregateRoot` queues the :class:`~pyfly.domain.DomainEvent` instances it raises.
Nothing used to publish them. :class:`DomainEventPublisher`, while it runs (the EDA auto-configuration
registers it; ``pyfly.eda.domain-events.enabled``), collects them from the unit of work that persists the
aggregate, and publishes them as that unit commits, from a ``before_commit`` synchronization:

- to the application's event listeners (:class:`~pyfly.context.events.ApplicationEventPublisher`): a
  plain ``@app_event_listener`` runs there, inside the unit; one declared with a transaction phase runs at
  that phase (``AFTER_COMMIT``: once the unit committed, and not at all when it rolls back);
- and, with a *destination* (``pyfly.eda.domain-events.destination``), through the EDA event publisher,
  as ``publish(destination, event.event_type, event.to_payload(), headers)``. An outbox bus (the ``postgres``
  and ``database`` providers) writes them in the committing unit itself: they are published exactly when the
  aggregate's changes are. A broker bus (Kafka, RabbitMQ) gets them after the commit.

Which events it collects:

- every event an aggregate raises inside a unit of work (a ``@transactional`` method, a
  ``TransactionTemplate`` block, a repository call's own unit);
- the pending events of an aggregate a relational unit of work saves (``session.add``: ``Repository.save``
  of a new or detached aggregate), such as those an aggregate raises in its factory before any unit exists.

A unit that rolls back publishes nothing: the events stay pending on the aggregate, and a unit that saves it
again publishes them. Code that wants to publish by hand calls :meth:`DomainEventPublisher.publish` inside the
unit of work (it drains the aggregates it is given).
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from pyfly.domain.aggregate_root import AggregateRoot, add_event_observer, remove_event_observer
from pyfly.domain.domain_event import DomainEvent, event_payload

if TYPE_CHECKING:
    from pyfly.context.events import ApplicationEventPublisher
    from pyfly.data.transaction import UnitOfWork
    from pyfly.eda.ports.outbound import EventPublisher

_logger = logging.getLogger(__name__)

_UNIT_KEY = "pyfly.domain_events"
"""``UnitOfWork.attributes`` key of the unit's collector."""

MAX_PUBLICATION_ROUNDS = 16
"""How many times a unit's collector publishes the events raised while it publishes, before it gives up."""

EVENT_ID_HEADER = "x-pyfly-event-id"
AGGREGATE_TYPE_HEADER = "x-pyfly-aggregate-type"
AGGREGATE_ID_HEADER = "x-pyfly-aggregate-id"

_LOCK = threading.Lock()
_ACTIVE: tuple[DomainEventPublisher, ...] = ()
_orm_hook_installed = False


def active_domain_event_publisher() -> DomainEventPublisher | None:
    """The publisher started last, which the collected events go to; ``None`` when none runs."""
    active = _ACTIVE
    return active[-1] if active else None


class DomainEventPublisher:
    """Publishes aggregates' domain events as their unit of work commits (see the module documentation).

    *events* is the application's event publisher (``None``: no listener gets them), *publisher* the EDA event
    publisher and *destination* the destination on it (no destination: they are not published there), and
    *headers* go with each of them there (the event id, the aggregate type and id are added).
    """

    def __init__(
        self,
        events: ApplicationEventPublisher | None = None,
        publisher: EventPublisher | None = None,
        *,
        destination: str | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> None:
        self._events = events
        self._publisher = publisher
        self._destination = destination or None
        self._headers = dict(headers or {})

    # -- lifecycle -----------------------------------------------------------------------------------------------

    async def start(self) -> None:
        """Start collecting: this publisher receives the events raised from now on (the last one started does)."""
        global _ACTIVE
        with _LOCK:
            _ACTIVE = (*(active for active in _ACTIVE if active is not self), self)
            add_event_observer(_event_raised)
        _install_orm_hook()

    async def stop(self) -> None:
        """Stop collecting (idempotent); the publisher started before this one, if any, takes over."""
        global _ACTIVE
        with _LOCK:
            _ACTIVE = tuple(active for active in _ACTIVE if active is not self)
            if not _ACTIVE:
                remove_event_observer(_event_raised)

    @property
    def running(self) -> bool:
        """Whether the publisher collects events."""
        return self in _ACTIVE

    # -- publishing ------------------------------------------------------------------------------------------------

    async def publish(self, *aggregates: AggregateRoot[Any]) -> int:
        """Drain *aggregates*' pending events and publish them now: inside a unit of work, the listeners with a
        transaction phase run at that phase and an outbox bus writes them in the unit. Returns how many."""
        published = 0
        for aggregate in aggregates:
            for event in aggregate.clear_events():
                await self.publish_event(event, aggregate)
                published += 1
        return published

    async def publish_event(self, event: DomainEvent, aggregate: AggregateRoot[Any] | None = None) -> None:
        """Publish one event (see :meth:`publish`)."""
        if self._events is not None:
            await self._events.publish(event)
        if self._publisher is None or self._destination is None:
            return
        publisher, destination = self._publisher, self._destination
        payload = event.to_payload() if isinstance(event, DomainEvent) else event_payload(event)
        headers = {**self._headers, EVENT_ID_HEADER: str(getattr(event, "event_id", ""))}
        if aggregate is not None:
            headers[AGGREGATE_TYPE_HEADER] = type(aggregate).__name__
            if aggregate.id is not None:
                headers[AGGREGATE_ID_HEADER] = str(aggregate.id)
        event_type = str(getattr(event, "event_type", type(event).__name__))
        if getattr(publisher, "joins_transactions", False):
            await publisher.publish(destination, event_type, payload, headers)
            return
        from pyfly.data.transaction import after_commit

        async def send() -> None:
            await publisher.publish(destination, event_type, payload, headers)

        await after_commit(send)

    # -- collecting ------------------------------------------------------------------------------------------------

    def collect(self, aggregate: AggregateRoot[Any], unit: UnitOfWork | None = None) -> bool:
        """Have *aggregate*'s events published when *unit* (by default the innermost unit of work of the running
        task) commits; returns ``False`` when there is no unit (the events stay pending on the aggregate)."""
        if unit is None:
            from pyfly.data.transaction import current_unit_of_work

            unit = current_unit_of_work()
        if unit is None or unit.completed:
            return False
        collector = unit.attributes.get(_UNIT_KEY)
        if not isinstance(collector, _UnitEvents):
            collector = _UnitEvents(unit)
            unit.register_synchronization(collector)
            unit.attributes[_UNIT_KEY] = collector
        collector.track(aggregate)
        return True


def _event_raised(aggregate: AggregateRoot[Any], event: DomainEvent) -> None:
    """The aggregate observer: tie the aggregate that raised *event* to the running unit of work."""
    publisher = active_domain_event_publisher()
    if publisher is None:
        return
    try:
        publisher.collect(aggregate)
    except Exception:  # noqa: BLE001 — raising an event must never fail the domain code
        _logger.debug("domain_event_collection_failed", exc_info=True)


class _UnitEvents:
    """A unit's synchronization: publishes the pending events of the aggregates tied to it, before it commits."""

    def __init__(self, unit: UnitOfWork) -> None:
        self._unit = unit
        self._aggregates: dict[int, AggregateRoot[Any]] = {}

    def track(self, aggregate: AggregateRoot[Any]) -> None:
        self._aggregates.setdefault(id(aggregate), aggregate)

    async def before_commit(self, read_only: bool) -> None:
        publisher = active_domain_event_publisher()
        if publisher is None:
            return
        synchronizations = self._unit.synchronizations
        start = len(synchronizations)
        # A listener may make an aggregate raise more: publish until they are all drained (a bounded number of
        # rounds: listeners that keep raising events for each other would never let the unit commit).
        for _round in range(MAX_PUBLICATION_ROUNDS):
            pending = [aggregate for aggregate in self._aggregates.values() if aggregate.pending_events()]
            if not pending:
                break
            await publisher.publish(*pending)
        else:
            raise RuntimeError(
                f"The domain event listeners of {self._unit.describe()} kept raising events after "
                f"{MAX_PUBLICATION_ROUNDS} rounds of publication; the unit rolls back"
            )
        # Synchronizations registered while publishing (the BEFORE_COMMIT listeners of these events) came after
        # the unit's list of synchronizations was taken for this phase: run their before-commit part here, once.
        while start < len(synchronizations):
            end = len(synchronizations)
            for synchronization in synchronizations[start:end]:
                await synchronization.before_commit(read_only)
            start = end

    async def before_completion(self) -> None:
        """Nothing to do before completion."""

    async def after_commit(self) -> None:
        """Nothing to do after commit: the events went out before it."""

    async def after_completion(self, status: Any) -> None:
        self._aggregates.clear()


def _install_orm_hook() -> None:
    """Collect the pending events of an aggregate a relational unit of work saves (``session.add``), once per
    process; a no-op without the relational module."""
    global _orm_hook_installed
    if _orm_hook_installed:
        return
    try:
        from pyfly.data.relational.sqlalchemy.attach_events import listen_for_attached
    except ImportError:  # pragma: no cover — SQLAlchemy is an optional dependency
        return
    with _LOCK:
        if not _orm_hook_installed:
            listen_for_attached(_attached)
            _orm_hook_installed = True


def _attached(session: Any, instance: object) -> None:
    if not isinstance(instance, AggregateRoot) or not instance.pending_events():
        return
    publisher = active_domain_event_publisher()
    if publisher is None:
        return
    unit = _unit_of(session)
    if unit is not None:
        publisher.collect(instance, unit)


def _unit_of(session: Any) -> UnitOfWork | None:
    """The unit of work of the running task whose session is *session* (a sync ``Session``)."""
    from pyfly.data.transaction import UnitOfWork
    from pyfly.data.transaction.context import current_state

    state = current_state()
    candidates = [unit for _name, unit in reversed(state.scopes)]
    candidates += [bound for _name, bound in reversed(state.units) if isinstance(bound, UnitOfWork)]
    for unit in candidates:
        resource = unit.resource
        if resource is session or getattr(resource, "sync_session", None) is session:
            return unit
    return None
