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
"""Projections — read-model builders that consume the event log.

A :class:`ProjectionRunner` reads the store's global stream in pages of *batch_size* events, by global position,
and hands each event to its :class:`Projection`, in order:

- **Checkpointed.** Where the projection got to is a :class:`~pyfly.eventsourcing.checkpoint.CheckpointStore`
  checkpoint, loaded when the runner starts and moved by every batch. With
  :class:`~pyfly.eventsourcing.checkpoint.SqlAlchemyCheckpointStore` a batch is one unit of work: the handlers'
  writes on the checkpoint's datasource and the new checkpoint commit together, so a restart resumes exactly
  where the last committed batch ended. Without a checkpoint store the position lives in the runner (a new
  runner starts from the beginning).
- **One active replica.** With a lease (by default the checkpoint store's
  :meth:`~pyfly.eventsourcing.checkpoint.SqlAlchemyCheckpointStore.projection_lease`), only the runner holding
  the projection's lease applies events; the others wait and take over when it stops or its lease ends. The
  checkpoint is fenced too: a batch whose starting position another runner has moved meanwhile applies nothing.
- **Full speed while behind.** A full page is followed at once by the next one; the runner sleeps
  *poll_interval_s* only on a short page, when it has caught up.
- **In order, never past a failure.** A handler that raises stops the batch there; the events before it are
  applied (in their own batch when the store rolled the failed one back), and the failed event is retried
  after *poll_interval_s*, until it succeeds. The events of a batch whose commit fails are retried one at a
  time until the runner is past them, so the event that breaks it is found and the others go through.

The runner is a lifecycle bean of :data:`~pyfly.kernel.lifecycle.CONSUMER_PHASE`, and its work runs in a task of
its own, outside any unit of work of the code that started it.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Literal, Protocol, runtime_checkable

from pyfly.data.transaction import detached
from pyfly.eventsourcing.checkpoint import CheckpointBatch, CheckpointStore, InMemoryCheckpointStore, ProjectionLease
from pyfly.eventsourcing.event import StoredEventEnvelope
from pyfly.eventsourcing.store import EventStore
from pyfly.kernel.lifecycle import CONSUMER_PHASE

_logger = logging.getLogger(__name__)

LEASE_PREFIX = "pyfly.projection."
"""A projection's lease is named ``pyfly.projection.<projection name>``."""


@runtime_checkable
class Projection(Protocol):
    """A projection consumes events from the store and updates a read model."""

    name: str

    async def handle(self, event: StoredEventEnvelope) -> None: ...


def _takes_positions(store: EventStore) -> bool:
    """Whether *store*'s ``stream_all`` pages by global position (the SPI since 26.09.08)."""
    try:
        parameters = inspect.signature(store.stream_all).parameters
    except (TypeError, ValueError):
        return True
    return "after_position" in parameters or any(
        parameter.kind is inspect.Parameter.VAR_KEYWORD for parameter in parameters.values()
    )


class ProjectionRunner:
    """Feeds the events of a store's global stream to a projection (see the module documentation).

    - *checkpoints*: where the projection's position is kept (in the runner when ``None``: a new runner starts
      from the beginning, and :meth:`start` logs a WARNING when the store is a durable one).
    - *batch_size*: the events read, applied and checkpointed together.
    - *poll_interval_s*: the sleep once caught up, and before a failed event is retried.
    - *lease*: the lease that makes one runner active; ``None`` takes the checkpoint store's lease when it has
      one (``projection_lease()``), ``False`` runs without one. *lease_ttl_s* is how long a lease lasts
      unrenewed; the runner renews it after a third of that.
    - *start_from*: where a projection with no checkpoint yet starts, ``earliest`` (it applies the whole
      history) or ``latest`` (only the events appended after it first starts).
    """

    #: The poller stops before any ``@pre_destroy``, so no event reaches a destroyed projection or
    #: read model (see :mod:`pyfly.kernel.lifecycle`).
    phase = CONSUMER_PHASE

    def __init__(
        self,
        projection: Projection,
        store: EventStore,
        *,
        poll_interval_s: float = 1.0,
        checkpoints: CheckpointStore | None = None,
        batch_size: int = 100,
        lease: ProjectionLease | Literal[False] | None = None,
        lease_ttl_s: float = 30.0,
        start_from: Literal["earliest", "latest"] = "earliest",
    ) -> None:
        if batch_size < 1:
            raise ValueError(f"batch_size must be at least 1, got {batch_size}")
        if poll_interval_s < 0:
            raise ValueError(f"poll_interval_s cannot be negative, got {poll_interval_s}")
        if lease_ttl_s <= 0:
            raise ValueError(f"lease_ttl_s must be positive, got {lease_ttl_s}")
        if start_from not in ("earliest", "latest"):
            raise ValueError(f"start_from is 'earliest' or 'latest', got {start_from!r}")
        self._projection = projection
        self._store = store
        self._poll_interval = poll_interval_s
        self._batch_size = batch_size
        self._start_from = start_from
        self._positions = _takes_positions(store)
        if not self._positions and (checkpoints is not None or start_from == "latest"):
            raise TypeError(
                f"{type(store).__name__}.stream_all has no after_position parameter: checkpoints and start_from need "
                "an event store with global positions"
            )
        if start_from == "latest" and not callable(getattr(store, "last_position", None)):
            raise TypeError(f"start_from='latest' needs {type(store).__name__}.last_position()")
        self._checkpoints: CheckpointStore = checkpoints if checkpoints is not None else InMemoryCheckpointStore()
        self._own_checkpoints = checkpoints is None
        if lease is None:
            offered = getattr(checkpoints, "projection_lease", None)
            lease = offered() if callable(offered) else False
        self._lease: ProjectionLease | None = lease if lease is not False else None
        self._lease_ttl = lease_ttl_s
        self._lease_held = False
        self._renew_at = 0.0
        self._latest = 0  # the last position when the runner started (start_from="latest")
        self._narrow_until: int | None = None  # the last position of a page that failed at commit
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()

    @property
    def name(self) -> str:
        """The projection's name: its checkpoint's key."""
        return self._projection.name

    async def start(self) -> None:
        """Start applying events, in a task of its own (idempotent). With ``start_from="latest"`` it reads the
        stream's last position first: a projection with no checkpoint yet starts after the events appended
        before this call."""
        if self._task is not None:
            return
        if self._start_from == "latest":
            last_position: Callable[[], Awaitable[int]] = self._store.last_position  # type: ignore[attr-defined]
            self._latest = await last_position()
        # A store with an engine (read off its class: a datasource name resolves only once a manager is installed)
        # keeps its events across restarts.
        if self._own_checkpoints and self._positions and getattr(type(self._store), "engine", None) is not None:
            _logger.warning(
                "projection_checkpoints_in_memory",
                extra={
                    "projection": self.name,
                    "store": type(self._store).__name__,
                    "hint": "the events are durable but the runner keeps its position in memory: it applies the whole "
                    "store again at every start, and on every replica; pass checkpoints= (the "
                    "projection_checkpoint_store bean)",
                },
            )
        if not self._positions:
            _logger.warning(
                "projection_store_without_positions",
                extra={
                    "projection": self.name,
                    "store": type(self._store).__name__,
                    "hint": "the store's stream_all has no after_position: the runner keeps its place in memory "
                    "by event id, without checkpoints or a lease",
                },
            )
        self._stop.clear()
        self._task = detached(self._loop(), name=f"pyfly-projection-{self.name}")

    async def stop(self) -> None:
        """Let the batch in flight finish, stop, and release the lease (idempotent).

        A cancellation of the caller goes through: the runner's task is cancelled with it (its batch rolls back)
        and the lease ends at its ttl."""
        task = self._task
        if task is None:
            return
        self._stop.set()
        try:
            await task
        except asyncio.CancelledError:
            current = asyncio.current_task()
            if current is not None and current.cancelling():
                raise  # the caller of stop() is cancelled, not only the runner's task
        finally:
            self._task = None
        await self._release_lease()

    # ------------------------------------------------------------------
    # The loop
    # ------------------------------------------------------------------

    async def _loop(self) -> None:
        if not self._positions:
            await self._loop_by_event_id()
            return
        position: int | None = None  # the checkpoint's position, as this runner knows it; None: load it
        limit = self._batch_size
        while not self._stop.is_set():
            pause = True
            try:
                if not await self._hold_lease():
                    position = None  # another runner is active: read the checkpoint again once this one is
                else:
                    if position is None:
                        position = await self._initial_position()
                    events = await self._store.stream_all(after_position=position, limit=limit)
                    if events:
                        position, limit, pause = await self._apply(events, position, limit)
            except Exception:  # noqa: BLE001 — the runner outlives a failing store or checkpoint; it tries again
                _logger.error("projection_runner_failed", extra={"projection": self.name}, exc_info=True)
                position = None
            if pause:
                await self._pause()

    async def _initial_position(self) -> int:
        return await self._checkpoints.load(self.name, initial=self._latest)

    async def _apply(self, events: list[StoredEventEnvelope], position: int, limit: int) -> tuple[int, int, bool]:
        """Apply one page in one checkpoint batch; returns the checkpoint's position, the next page's size, and
        whether to pause before reading it."""
        target = events[-1].global_position
        if target is None:
            raise TypeError(f"{type(self._store).__name__} returned an event without a global position")
        batch: CheckpointBatch | None = None
        failed_at: int | None = None
        try:
            async with self._checkpoints.batch(self.name, expected=position, position=target) as batch:
                if batch.claimed:
                    for index, event in enumerate(events):
                        failed_at = index
                        await self._projection.handle(event)
                        batch.applied(event.global_position if event.global_position is not None else target)
                    failed_at = None
        except Exception as error:  # noqa: BLE001 — the event is retried in order; the runner goes on
            now = batch.position if batch is not None else position
            if failed_at is not None:
                _logger.error(
                    "projection_event_failed",
                    extra={"projection": self.name, "event_id": events[failed_at].event_id},
                    exc_info=(type(error), error, error.__traceback__),
                )
                if failed_at > 0 and now == position:
                    return now, failed_at, False  # the batch rolled back: apply the events before the failure
                return now, self._page_size(now), True
            _logger.error(
                "projection_batch_failed",
                extra={"projection": self.name, "events": len(events)},
                exc_info=(type(error), error, error.__traceback__),
            )
            if len(events) > 1:
                # Find the event that breaks the batch: its events one at a time, until the runner is past them.
                self._narrow_until = max(self._narrow_until or 0, target)
                return now, 1, False
            return now, 1, True
        if not batch.claimed:
            # Another runner moved the checkpoint: this batch applied nothing; go on from where it is now.
            _logger.debug("projection_batch_superseded", extra={"projection": self.name, "expected": position})
            now = await self._checkpoints.load(self.name)
            return now, self._page_size(now), False
        return target, self._page_size(target), len(events) < limit

    def _page_size(self, position: int) -> int:
        """The next page's size from *position*: one event while the runner is inside a page that failed at
        commit, *batch_size* once it is past it."""
        if self._narrow_until is not None:
            if position < self._narrow_until:
                return 1
            self._narrow_until = None
        return self._batch_size

    async def _loop_by_event_id(self) -> None:
        """The loop for an event store without global positions (written against the SPI of earlier releases):
        the place is the last event id, in memory."""
        last_event_id: str | None = None
        while not self._stop.is_set():
            pause = True
            try:
                events = await self._store.stream_all(after_event_id=last_event_id, limit=self._batch_size)
                for event in events:
                    try:
                        await self._projection.handle(event)
                    except Exception:  # noqa: BLE001 — retried in order at the next poll
                        _logger.error(
                            "projection_event_failed",
                            extra={"projection": self.name, "event_id": event.event_id},
                            exc_info=True,
                        )
                        break
                    last_event_id = event.event_id
                else:
                    pause = len(events) < self._batch_size
            except Exception:  # noqa: BLE001 — the runner outlives a failing store; it tries again
                _logger.error("projection_runner_failed", extra={"projection": self.name}, exc_info=True)
            if pause:
                await self._pause()

    async def _pause(self) -> None:
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._stop.wait(), timeout=self._poll_interval)

    # ------------------------------------------------------------------
    # The lease
    # ------------------------------------------------------------------

    @property
    def _lease_name(self) -> str:
        return f"{LEASE_PREFIX}{self.name}"

    async def _hold_lease(self) -> bool:
        """Whether this runner holds the projection's lease (always without one): renew it once a third of its
        time has passed, or try to take it."""
        lease = self._lease
        if lease is None:
            return True
        now = time.monotonic()
        if self._lease_held:
            if now < self._renew_at:
                return True
            if await lease.extend(self._lease_name, self._lease_ttl):
                self._renew_at = now + self._lease_ttl / 3
                return True
            _logger.warning("projection_lease_lost", extra={"projection": self.name, "lease": self._lease_name})
            await self._release_lease()  # the acquisition has ended: the lease need not keep it
        if await lease.try_acquire(self._lease_name, self._lease_ttl):
            self._lease_held = True
            self._renew_at = now + self._lease_ttl / 3
            _logger.info("projection_lease_acquired", extra={"projection": self.name, "lease": self._lease_name})
            return True
        return False

    async def _release_lease(self) -> None:
        lease = self._lease
        if lease is None or not self._lease_held:
            return
        self._lease_held = False
        try:
            await lease.release(self._lease_name)
        except Exception:  # noqa: BLE001 — stopping goes on; the lease ends at its ttl anyway
            _logger.warning("projection_lease_release_failed", extra={"projection": self.name}, exc_info=True)


class FunctionProjection:
    """Quick projection wrapper around a single async callable."""

    def __init__(self, name: str, handler: Callable[[StoredEventEnvelope], Awaitable[None]]) -> None:
        self.name = name
        self._handler = handler

    async def handle(self, event: StoredEventEnvelope) -> None:
        await self._handler(event)
