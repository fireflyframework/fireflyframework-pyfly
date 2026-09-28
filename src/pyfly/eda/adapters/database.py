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
"""An ``EventPublisher`` on the transactional outbox, on any SQL datasource (``pyfly.eda.provider=database``).

:meth:`DatabaseEventBus.publish` writes the event into the outbox in the unit of work of the code that
publishes it, so the event exists exactly when that unit commits (see :mod:`pyfly.eda.outbox`). The bus's
relay, a :data:`~pyfly.kernel.lifecycle.CONSUMER_PHASE` lifecycle bean, delivers the events owed to its
consumer group to the subscribed handlers, at least once, each subscription settled on its own.

On PostgreSQL two accelerators are on: a publish is one statement that also sends ``NOTIFY`` (delivered by the
server only if the unit commits), and a bus that consumes keeps a ``LISTEN`` connection that wakes its relay at
once. The ``LISTEN`` connection is opened once the bus runs and a handler is subscribed (a bus that only
publishes never opens one), checked out of the datasource's pool (or opened on *listen_dsn*: a direct
connection, where a pooler in transaction mode stands in front of the server; the pool's connection is used only
with the asyncpg driver). It is kept alive by the relay's polls and opened again after it is lost; while it is
being reopened the relay polls, and the health indicator says so in its details (the bus is still ``UP``: its
events are delivered, at the next poll). Elsewhere the relay polls every ``poll_interval`` seconds, and a publish
in the same process wakes it when the publishing unit commits.

The lifecycle is explicit: :meth:`start` is serialized and idempotent and leaves nothing behind when it fails;
:meth:`stop` is idempotent; :meth:`publish` never starts anything, so a publish after :meth:`stop` (from a
``@pre_destroy`` method) still writes its event, for this or another process to deliver.
"""

from __future__ import annotations

import asyncio
import contextlib
import enum
import logging
import re
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from pyfly.eda.dlq import EdaDeadLetterStore
from pyfly.eda.outbox import (
    Outbox,
    OutboxRelay,
    OutboxTables,
    Retention,
    StartPosition,
    Subscription,
    describe_error,
)
from pyfly.eda.ports.outbound import EventHandler
from pyfly.eda.types import ErrorStrategy, EventEnvelope
from pyfly.kernel.lifecycle import CONSUMER_PHASE

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncConnection

    from pyfly.actuator.health import HealthStatus
    from pyfly.messaging.listener_container import ListenerContainerSettings, RetryPolicy

logger = logging.getLogger(__name__)

_IDENTIFIER = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")

LISTEN_RECONNECT_FIRST_DELAY = 0.5
"""How long a bus waits before it first tries to reopen a lost ``LISTEN`` connection, in seconds."""

LISTEN_RECONNECT_MAX_DELAY = 30.0
"""The longest wait between two attempts to reopen a lost ``LISTEN`` connection, in seconds."""


def _channel_name(name: str) -> str:
    """Validate a ``LISTEN``/``NOTIFY`` channel name (it is interpolated into ``LISTEN``)."""
    if not _IDENTIFIER.match(name):
        raise ValueError(f"invalid identifier: {name!r}")
    return name


class ListenerState(enum.Enum):
    """The state of a bus's ``LISTEN`` connection."""

    OFF = "off"
    """No wake-up connection: not PostgreSQL, notifications are switched off, or no handler is subscribed yet."""
    LISTENING = "listening"
    RECONNECTING = "reconnecting"
    """The connection was lost (or could not be opened); the relay polls until it is (re)opened."""


def listen_driver_supported(driver: str, listen_dsn: str | None) -> bool:
    """Whether a bus can ``LISTEN`` on a PostgreSQL datasource whose driver is *driver*: on a connection of the
    datasource's pool only with asyncpg (whose listener API it uses), and always on a *listen_dsn* of its own."""
    return listen_dsn is not None or driver == "asyncpg"


class _PostgresListener:
    """The ``LISTEN`` connection of a bus: it wakes the relay on every notification of *channel*."""

    def __init__(self, bus: DatabaseEventBus, channel: str, listen_dsn: str | None) -> None:
        self._bus = bus
        self._channel = channel
        self._listen_dsn = listen_dsn
        self._pooled: AsyncConnection | None = None
        self._driver: Any = None
        self.state = ListenerState.OFF
        self.lost_at: datetime | None = None
        self.last_error: str | None = None
        self._failures = 0
        self._retry_at = 0.0
        self._checked_at = 0.0

    async def open(self) -> None:
        """Open the connection and ``LISTEN``; raises when that fails (the bus's start fails with it)."""
        loop = asyncio.get_running_loop()
        try:
            if self._listen_dsn is not None:
                import asyncpg  # type: ignore[import-untyped]

                from pyfly.eda.adapters.postgres import _normalise_dsn

                self._driver = await asyncpg.connect(_normalise_dsn(self._listen_dsn))
            else:
                self._pooled = await self._bus.outbox.engine().connect()
                raw = await self._pooled.get_raw_connection()
                self._driver = raw.driver_connection
            await self._driver.add_listener(self._channel, self._notified)
            self._driver.add_termination_listener(self._terminated)
        except BaseException:
            await self._discard()
            raise
        self.state = ListenerState.LISTENING
        self.lost_at = None
        self.last_error = None
        self._failures = 0
        self._retry_at = self._checked_at = loop.time()

    def _notified(self, _connection: Any, _pid: int, _channel: str, _payload: str) -> None:
        self._bus.relay.wake()

    def _terminated(self, _connection: Any) -> None:
        if self.state is ListenerState.LISTENING:
            self._lost("the server closed the connection")

    def _lost(self, reason: str) -> None:
        self.state = ListenerState.RECONNECTING
        self.lost_at = datetime.now(UTC)
        self.last_error = reason
        self._failures = 0
        # A server that just closed its connections (a restart, a failover) is rarely back at once.
        self._retry_at = asyncio.get_running_loop().time() + LISTEN_RECONNECT_FIRST_DELAY
        self._bus.relay.wake_after(LISTEN_RECONNECT_FIRST_DELAY)
        logger.warning(
            "eda_listen_connection_lost",
            extra={"channel": self._channel, "group": self._bus.group, "reason": reason},
        )
        self._bus.relay.wake()

    async def check(self) -> None:
        """Before every relay round (the relay runs them once a handler is subscribed): open the connection the
        first time, keep it alive, or reopen a lost one (with a back-off)."""
        loop = asyncio.get_running_loop()
        if self.state is ListenerState.LISTENING:
            if loop.time() - self._checked_at < self._bus.relay.poll_interval:
                return  # checked within a poll interval (a busy relay runs rounds back to back)
            try:
                if self._driver is None or self._driver.is_closed():
                    raise ConnectionError("the LISTEN connection is closed")
                # LISTEN again: a no-op for the server, and a round trip that keeps an idle session alive
                # (idle_session_timeout) and finds a dead one.
                await asyncio.wait_for(self._driver.execute(f'LISTEN "{self._channel}"'), timeout=5.0)
            except Exception as error:  # noqa: BLE001 — any failure means the wake-ups are gone
                self._lost(describe_error(error))
            else:
                self._checked_at = loop.time()
                return
        if loop.time() < self._retry_at:
            return
        first = self.state is ListenerState.OFF
        await self._discard()
        try:
            await self.open()
        except Exception as error:  # noqa: BLE001 — the relay polls meanwhile; try again later
            if first:
                self.lost_at = datetime.now(UTC)
                logger.warning(
                    "eda_listen_connection_failed",
                    extra={"channel": self._channel, "group": self._bus.group, "error": describe_error(error)},
                )
            self.state = ListenerState.RECONNECTING
            self.last_error = describe_error(error)
            self._failures += 1
            delay = min(LISTEN_RECONNECT_MAX_DELAY, LISTEN_RECONNECT_FIRST_DELAY * 2**self._failures)
            self._retry_at = loop.time() + delay
            self._bus.relay.wake_after(delay)
            logger.debug("eda_listen_reconnect_failed", exc_info=True)
            return
        if not first:
            logger.info("eda_listen_connection_restored", extra={"channel": self._channel, "group": self._bus.group})
        self._bus.relay.wake()  # what was published while it was not listening is claimed now

    async def close(self) -> None:
        """Stop listening and let the connection go."""
        self.state = ListenerState.OFF
        await self._discard()

    async def _discard(self) -> None:
        driver, pooled = self._driver, self._pooled
        self._driver = self._pooled = None
        if driver is not None:
            with contextlib.suppress(Exception):
                driver.remove_termination_listener(self._terminated)
            with contextlib.suppress(Exception):
                await driver.remove_listener(self._channel, self._notified)
        if pooled is not None:
            # A pooled connection that LISTENed must not serve anyone else: close it instead of returning it.
            with contextlib.suppress(Exception):
                await pooled.invalidate()
            with contextlib.suppress(Exception):
                await pooled.close()
        elif driver is not None:
            with contextlib.suppress(Exception):
                await driver.close()


class BusState(enum.Enum):
    """Where a bus is in its lifecycle."""

    NEW = "new"
    RUNNING = "running"
    STOPPED = "stopped"


class DatabaseEventBus:
    """``EventPublisher`` on the transactional outbox (see the module documentation).

    - *datasource*: where the outbox lives (a name, a ``DataSource``, an ``AsyncEngine``; ``None``: the
      default datasource);
    - *destinations*: what the bus's consumer group consumes (``None``: every destination), *group*: its name.
      Every process of a group must subscribe the same handlers: a delivery goes to one of them;
    - *settings*: the listener container settings (``pyfly.eda.listener.*``: the retry policy, whether and
      how a handler runs in a unit of work, the shutdown timeout); *retry* overrides the retry policy, and
      *error_strategy* chooses what a failure leads to (:class:`~pyfly.eda.types.ErrorStrategy`);
    - *poll_interval*, *batch_size*, *claim_timeout*, *handler_timeout*, *retention*, *start_position*: see
      :class:`~pyfly.eda.outbox.OutboxRelay`;
    - *create_tables*: create the outbox tables when they are missing (otherwise they are only checked);
      *tables*: the outbox's tables (the ``pyfly_outbox_*`` ones by default);
    - *notify*: the PostgreSQL wake-ups (``None``: on when the datasource is PostgreSQL and the ``LISTEN``
      connection can be opened: with the asyncpg driver, or on *listen_dsn*), on *channel*, with the ``LISTEN``
      connection opened on *listen_dsn* when given;
    - *dead_letter_store*: where dead letters go instead of the outbox's table.
    """

    #: The bus consumes: it stops before any ``@pre_destroy``.
    phase = CONSUMER_PHASE
    #: A publish joins the publisher's unit of work (the CQRS and domain-event publishers rely on it): the unit
    #: bound for the outbox's datasource, so the event commits with the business changes when the outbox is on
    #: their datasource (on another one it commits in a unit of that datasource).
    joins_transactions = True
    #: The bus retries and dead-letters failed deliveries itself.
    manages_listener_errors = True

    def __init__(
        self,
        datasource: object = None,
        *,
        destinations: Sequence[str] | None = None,
        group: str = "default",
        settings: ListenerContainerSettings | None = None,
        retry: RetryPolicy | None = None,
        error_strategy: ErrorStrategy = ErrorStrategy.DEAD_LETTER,
        poll_interval: float = 5.0,
        batch_size: int = 100,
        claim_timeout: float = 300.0,
        handler_timeout: float | None = 60.0,
        retention: Retention | None = None,
        start_position: StartPosition | str = StartPosition.LATEST,
        create_tables: bool = True,
        tables: OutboxTables | None = None,
        notify: bool | None = None,
        channel: str = "pyfly_eda",
        listen_dsn: str | None = None,
        dead_letter_store: EdaDeadLetterStore | None = None,
        owner: str | None = None,
    ) -> None:
        self._channel = _channel_name(channel)
        self._notify = notify
        self._listen_dsn = listen_dsn
        self._group = group
        self._destinations = list(destinations) if destinations else None
        self._outbox = Outbox(
            datasource,
            tables=tables,
            create_tables=create_tables,
            notify_channel=None if notify is False else self._channel,  # sent on PostgreSQL only
        )
        self._relay = OutboxRelay(
            self._outbox,
            group=group,
            destinations=self._destinations,
            start_position=start_position,
            retry=retry,
            error_strategy=error_strategy,
            settings=settings,
            poll_interval=poll_interval,
            batch_size=batch_size,
            claim_timeout=claim_timeout,
            handler_timeout=handler_timeout,
            retention=retention if retention is not None else Retention(),
            dead_letter_store=dead_letter_store,
            owner=owner,
            name=f"eda {group}",
        )
        self._listener: _PostgresListener | None = None
        self._relay.add_round_hook(self._check_listener)
        self._state = BusState.NEW
        self._lock = asyncio.Lock()

    # -- introspection -----------------------------------------------------------------------------------------

    @property
    def outbox(self) -> Outbox:
        """The outbox the bus writes and reads."""
        return self._outbox

    @property
    def relay(self) -> OutboxRelay:
        """The relay that delivers the bus's consumer group."""
        return self._relay

    @property
    def group(self) -> str:
        """The bus's consumer group."""
        return self._group

    @property
    def destinations(self) -> list[str] | None:
        """What the group consumes (``None``: every destination)."""
        return None if self._destinations is None else list(self._destinations)

    @property
    def state(self) -> BusState:
        """Where the bus is in its lifecycle."""
        return self._state

    @property
    def running(self) -> bool:
        """Whether the bus is started."""
        return self._state is BusState.RUNNING

    @property
    def listener_state(self) -> ListenerState:
        """The state of the PostgreSQL wake-up connection."""
        return self._listener.state if self._listener is not None else ListenerState.OFF

    @property
    def subscriptions(self) -> list[Subscription]:
        """The handlers subscribed so far."""
        return self._relay.subscriptions

    # -- EventPublisher ----------------------------------------------------------------------------------------

    def subscribe(self, event_type_pattern: str, handler: EventHandler) -> None:
        """Subscribe *handler* to the event types matching the pattern; the relay claims once there is one."""
        self._relay.subscribe(event_type_pattern, handler)

    async def publish(
        self,
        destination: str,
        event_type: str,
        payload: dict[str, Any],
        headers: dict[str, str] | None = None,
    ) -> None:
        """Write the event in the unit of work bound for the outbox's datasource, or in a short one of its own.

        The event is published when (and only if) that unit commits. It starts nothing: after :meth:`stop` the
        event is still written, and delivered by whichever process runs the group's relay.
        """
        envelope = EventEnvelope(event_type=event_type, payload=payload, destination=destination, headers=headers or {})
        if self._state is BusState.STOPPED and self._owns_datasource():
            # After stop(), a datasource the bus builds for itself lives for this publish only.
            async with self._lock:
                try:
                    await self._append(envelope)
                finally:
                    if self._state is BusState.STOPPED:
                        await self._release_datasource()
            return
        await self._append(envelope)

    async def _append(self, envelope: EventEnvelope) -> None:
        await self._resolve()
        # The event is owed to the groups registered for its destination, as the publishing unit sees them, and
        # to this bus's own group whenever this process consumes it: its relay may not have registered the group
        # yet (the application context subscribes the listeners after the bus started, and the relay registers
        # at its next round), and on MySQL and MariaDB the unit's snapshot may predate the registration. A publish
        # never writes the consumer groups itself: in the caller's unit, racing a relay that registers the same
        # group, that failed the business unit on MariaDB (1020, "Record has changed since last read").
        include = (self._group,) if self._relay.subscriptions and self._consumes(envelope.destination) else ()
        await self._outbox.append(envelope, include=include)
        self._relay.wake_after_commit()

    def _consumes(self, destination: str) -> bool:
        return self._destinations is None or destination in self._destinations

    def _owns_datasource(self) -> bool:
        """Whether the bus builds its datasource itself (a subclass given a URL outside an application does)."""
        return False

    async def _resolve(self) -> None:
        """Resolve the datasource before its first use (a subclass that is given a URL registers it here)."""

    # -- lifecycle ---------------------------------------------------------------------------------------------

    async def start(self) -> None:
        """Check (and create) the outbox tables, open the PostgreSQL wake-up connection and start the relay.

        Serialized and idempotent. When a step fails, what the earlier ones opened is closed again and the
        failure is raised: nothing is left running."""
        async with self._lock:
            if self._state is BusState.RUNNING:
                return
            try:
                await self._resolve()
                await self._outbox.start()
                if self._relay.subscriptions:
                    # Subscribed before the start: the group is owed every event published from now on.
                    await self._relay.register()
                if self._notifies():
                    listener = _PostgresListener(self, self._channel, self._listen_dsn)
                    if self._relay.subscriptions:
                        await listener.open()  # otherwise the relay opens it once a handler subscribes
                    self._listener = listener
                await self._relay.start()
            except BaseException:
                await self._release()
                raise
            self._state = BusState.RUNNING
            logger.info(
                "eda_outbox_bus_started",
                extra={
                    "group": self._group,
                    "destinations": self._destinations,
                    "dialect": self._outbox.dialect(),
                    "listener": self.listener_state.value,
                },
            )

    def _notifies(self) -> bool:
        if self._notify is False:
            return False
        postgresql = self._outbox.dialect() == "postgresql"
        if self._notify and not postgresql:
            raise ValueError("notify=True needs a PostgreSQL datasource (LISTEN/NOTIFY)")
        if not postgresql:
            return False
        driver = self._outbox.engine().dialect.driver
        if listen_driver_supported(driver, self._listen_dsn):
            return True
        if self._notify:
            raise ValueError(
                f"notify=True needs the asyncpg driver for the datasource's LISTEN connection (it is {driver!r}), "
                "or a listen_dsn (pyfly.eda.postgres.listen-dsn)"
            )
        logger.warning(
            "eda_listen_unavailable",
            extra={
                "group": self._group,
                "driver": driver,
                "reason": "LISTEN needs the asyncpg driver or pyfly.eda.postgres.listen-dsn; the relay polls",
            },
        )
        return False

    async def stop(self) -> None:
        """Stop the relay (the delivery in flight finishes) and close the wake-up connection. Idempotent."""
        async with self._lock:
            if self._state is not BusState.RUNNING:
                await self._release_datasource()  # a publish may have resolved one on a bus never started
                return
            await self._release()
            self._state = BusState.STOPPED

    async def _release(self) -> None:
        with contextlib.suppress(Exception):
            await self._relay.stop()
        listener, self._listener = self._listener, None
        if listener is not None:
            await listener.close()
        await self._release_datasource()

    async def _release_datasource(self) -> None:
        """Release a datasource the bus built for itself (a subclass given a URL does)."""

    async def _check_listener(self) -> None:
        if self._listener is not None:
            await self._listener.check()

    # -- health ------------------------------------------------------------------------------------------------

    async def ping(self) -> None:
        """Run ``SELECT 1`` on the outbox's datasource; raises when the database cannot be reached."""
        from sqlalchemy import text

        from pyfly.data.transaction import infrastructure_unit
        from pyfly.data.transaction.context import outside_transaction

        with outside_transaction():
            async with infrastructure_unit(self._outbox.datasource, read_only=True, single_statement=True) as session:
                await session.execute(text("SELECT 1"))

    async def health_status(self) -> HealthStatus:
        """``UP`` while the bus runs, its relay's task runs and its database answers, ``DOWN`` otherwise, with the
        reason. The details give the state of the wake-up connection (on PostgreSQL): while a lost one is being
        reopened the bus stays ``UP``, since its relay still delivers at every poll, and the details say it is
        degraded and since when."""
        from pyfly.actuator.health import HealthStatus

        details: dict[str, Any] = {
            "adapter": type(self).__name__,
            "group": self._group,
            "listener": self.listener_state.value,
            "delivered": self._relay.counters.delivered,
            "dead_lettered": self._relay.counters.dead_lettered,
        }
        if self._state is not BusState.RUNNING:
            return HealthStatus(status="DOWN", details={**details, "reason": f"bus {self._state.value}"})
        if not self._relay.alive:
            # Nothing is delivered in this process any more (its events wait for another node, or a restart).
            return HealthStatus(
                status="DOWN",
                details={**details, "reason": "the relay's task ended", "error": self._relay.last_error},
            )
        try:
            await asyncio.wait_for(self.ping(), timeout=2.0)
        except Exception as error:  # noqa: BLE001 — reported, not raised
            return HealthStatus(status="DOWN", details={**details, "error": describe_error(error)[:200]})
        listener = self._listener
        if listener is not None and listener.state is ListenerState.RECONNECTING:
            details.update(
                degraded="the LISTEN connection is lost and being reopened; events are delivered at each poll",
                poll_interval=self._relay.poll_interval,
                since=listener.lost_at.isoformat() if listener.lost_at else None,
                listener_error=listener.last_error,
            )
        if self._relay.last_error is not None:
            details["last_round_error"] = self._relay.last_error
        return HealthStatus(status="UP", details=details)
