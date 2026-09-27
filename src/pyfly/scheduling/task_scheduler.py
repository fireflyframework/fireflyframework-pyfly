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
"""Task scheduler engine — discovers @scheduled methods and manages execution loops.

Each ``@scheduled`` method gets a loop that submits its runs through the :class:`TaskExecutorPort`. The loops
follow Spring's ``ThreadPoolTaskScheduler`` contract:

- **No overlap.** A job never runs concurrently with itself unless it opts in with
  ``@scheduled(concurrent=N)``. ``fixed_rate`` starts a run every period, but a run that is still going when
  the next one is due delays it (the next run starts when it ends, then ``period`` after its own start);
  ``cron`` computes the next fire time once the previous run has ended (fires missed meanwhile are skipped);
  ``fixed_delay`` waits ``fixed_delay`` after each run. With ``concurrent=N`` up to *N* runs of the job are in
  flight (``fixed_rate`` and ``cron`` only). A slow run of a ``@transactional`` job therefore holds one pooled
  connection, not one per period.
- **Own units of work.** Runs are submitted as tasks started with the transaction state cleared (the built-in
  executors use :func:`pyfly.data.transaction.detached`).
- **Distributed lock.** With ``lock=...`` the run takes the lock first and skips the tick when it is held
  elsewhere. The run is time-boxed to ``lock_ttl``: the lock ends at its TTL whatever the run does, so a run
  still going then is cancelled (its ``@transactional`` work rolls back) instead of overlapping the run another
  node starts. A failure to take or release the lock (the database is down, a misconfigured provider) is
  logged with the job's name, like a failure of the job itself.
- **Graceful stop.** :meth:`TaskScheduler.stop` stops the loops (a loop only ever waits: for its next fire
  time, a free slot or its run's end, so stopping it never cancels a run), then drains the runs in flight of
  every trigger type alike through the executor. When the caller cuts the drain short (the application
  context bounds it by ``pyfly.context.shutdown-timeout``), the runs still going are cancelled and awaited.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import inspect
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from pyfly.data.transaction.template import run_shielded
from pyfly.kernel.lifecycle import CONSUMER_PHASE
from pyfly.scheduling.adapters.asyncio_executor import AsyncIOTaskExecutor
from pyfly.scheduling.lock import DistributedLock, LocalLock
from pyfly.scheduling.ports.outbound import TaskExecutorPort

try:
    from pyfly.scheduling.cron import CronExpression
except ImportError:  # pragma: no cover — croniter is the 'scheduling' extra; only cron triggers need it
    CronExpression = None  # type: ignore[assignment,misc]

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class _ScheduledEntry:
    """Internal record of a discovered @scheduled method and its metadata."""

    bean: Any
    method: Callable[..., Any]
    cron: str | None = None
    fixed_rate: timedelta | None = None
    fixed_delay: timedelta | None = None
    initial_delay: timedelta | None = None
    zone: str | None = None
    lock: str | None = None
    lock_ttl: float = 60.0
    concurrent: int = 1

    @property
    def name(self) -> str:
        """``Class.method``, the name a failure of the job is logged with."""
        return _job_name(self.bean, self.method)


def _job_name(bean: Any, method: Callable[..., Any]) -> str:
    method_name = getattr(method, "__name__", repr(method))
    return f"{type(bean).__name__}.{method_name}" if bean is not None else method_name


class TaskScheduler:
    """Discovers @scheduled methods on beans and manages their execution loops (see the module docs).

    Usage::

        scheduler = TaskScheduler()
        count = scheduler.discover(beans)
        await scheduler.start()
        # ... application runs ...
        await scheduler.stop()

    It is a lifecycle bean of :data:`~pyfly.kernel.lifecycle.CONSUMER_PHASE`: the application context stops it
    before any ``@pre_destroy``, while the beans its jobs use still work. :meth:`start` starts the loops of the
    entries discovered since the last start, and :meth:`stop` may be called again.
    """

    phase = CONSUMER_PHASE

    def __init__(self, executor: TaskExecutorPort | None = None, lock: DistributedLock | None = None) -> None:
        self._executor: TaskExecutorPort = executor or AsyncIOTaskExecutor()
        self._lock: DistributedLock = lock or LocalLock()
        self._running: bool = False
        self._entries: list[_ScheduledEntry] = []
        self._loop_tasks: list[asyncio.Task[Any]] = []
        self._started: set[int] = set()  # the indexes of the entries whose loop is running

    @property
    def executor(self) -> TaskExecutorPort:
        """The executor the runs are submitted through (``@async_method`` calls go through it too)."""
        return self._executor

    def discover(self, beans: list[Any]) -> int:
        """Scan beans for @scheduled methods. Return number of scheduled methods found.

        For each bean, inspects all attributes. If an attribute is callable and
        has ``__pyfly_scheduled__ == True``, it is recorded for later scheduling.
        """
        count = 0
        for bean in beans:
            for name in dir(bean):
                if name.startswith("_"):
                    continue
                # Look up statically first so @property / cached_property getters
                # are never evaluated during discovery — a side-effecting or
                # raising property must not break scheduled-task scanning.
                static_attr = inspect.getattr_static(bean, name, None)
                if isinstance(static_attr, (property, functools.cached_property)):
                    continue
                try:
                    attr = getattr(bean, name)
                except Exception:  # noqa: BLE001
                    continue
                if not callable(attr):
                    continue
                if not getattr(attr, "__pyfly_scheduled__", False):
                    continue

                # Resolve the lock name: True -> "Class.method", str -> as-is, else None.
                lock_value = getattr(attr, "__pyfly_scheduled_lock__", None)
                if lock_value is True:
                    lock_name: str | None = f"{type(bean).__name__}.{name}"
                elif isinstance(lock_value, str):
                    lock_name = lock_value
                else:
                    lock_name = None
                lock_ttl = getattr(attr, "__pyfly_scheduled_lock_ttl__", None)

                entry = _ScheduledEntry(
                    bean=bean,
                    method=attr,
                    cron=getattr(attr, "__pyfly_scheduled_cron__", None),
                    fixed_rate=getattr(attr, "__pyfly_scheduled_fixed_rate__", None),
                    fixed_delay=getattr(attr, "__pyfly_scheduled_fixed_delay__", None),
                    initial_delay=getattr(attr, "__pyfly_scheduled_initial_delay__", None),
                    zone=getattr(attr, "__pyfly_scheduled_zone__", None),
                    lock=lock_name,
                    lock_ttl=lock_ttl if lock_ttl is not None else 60.0,
                    concurrent=int(getattr(attr, "__pyfly_scheduled_concurrent__", 1) or 1),
                )
                self._entries.append(entry)
                count += 1
                logger.debug(
                    "Discovered scheduled method %s.%s",
                    type(bean).__name__,
                    name,
                )
        return count

    async def start(self) -> None:
        """Start the loops of the entries discovered since the last start. Call after :meth:`discover`."""
        self._running = True
        for index, entry in enumerate(self._entries):
            if index in self._started:
                continue
            if entry.cron is not None:
                loop = self._run_cron_loop(entry)
            elif entry.fixed_rate is not None:
                loop = self._run_fixed_rate_loop(entry)
            elif entry.fixed_delay is not None:
                loop = self._run_fixed_delay_loop(entry)
            else:
                logger.warning(
                    "Scheduled method %s has no trigger — skipping",
                    entry.method,
                )
                continue
            self._started.add(index)
            task = asyncio.create_task(loop, name=f"pyfly-scheduled[{entry.name}]")
            task.add_done_callback(self._loop_done_callback)
            self._loop_tasks.append(task)

    async def stop(self) -> None:
        """Stop the loops, then drain the runs in flight (of every trigger type) and stop the executor.

        Stopping a loop never cancels a run. The drain waits for every run; when the caller cancels it (the
        application context bounds it by ``pyfly.context.shutdown-timeout``), the runs still going are
        cancelled and awaited before the cancellation propagates.
        """
        self._running = False
        loops = self._loop_tasks
        self._loop_tasks = []
        self._started.clear()
        for task in loops:
            task.cancel()
        if loops:
            await asyncio.gather(*loops, return_exceptions=True)
        await self._executor.stop()

    @staticmethod
    def _loop_done_callback(task: asyncio.Task[Any]) -> None:
        """Log errors from scheduling loop tasks."""
        if not task.cancelled():
            exc = task.exception()
            if exc is not None:
                logger.error("Scheduling loop task failed: %s", exc, exc_info=exc)

    # ------------------------------------------------------------------
    # Private loop methods
    # ------------------------------------------------------------------

    async def _submit(self, entry: _ScheduledEntry, slots: asyncio.Semaphore) -> asyncio.Task[Any] | None:
        """Take a free slot of *entry* (waiting for a run to end when there is none) and submit a run; the run
        gives the slot back when it ends. ``None`` when the scheduler stopped meanwhile."""
        await slots.acquire()
        try:
            if not self._running:
                slots.release()
                return None
            task = await self._executor.submit(self._invoke(entry.bean, entry.method, entry.lock, entry.lock_ttl))
        except BaseException:
            slots.release()
            raise
        task.add_done_callback(lambda _done: slots.release())
        return task

    async def _run_cron_loop(self, entry: _ScheduledEntry) -> None:
        """Loop: wait for a free slot, then until the next cron fire time, and submit a run."""
        if CronExpression is None:
            raise ImportError("cron triggers need croniter: pip install 'pyfly[scheduling]'")
        assert entry.cron is not None
        cron = CronExpression(entry.cron, zone=entry.zone)
        slots = asyncio.Semaphore(entry.concurrent)
        while self._running:
            # With no free slot (a run in flight, concurrent=1), the next fire time is computed once it ended.
            async with slots:
                pass
            await asyncio.sleep(cron.seconds_until_next())
            if not self._running:
                break
            await self._submit(entry, slots)

    async def _run_fixed_rate_loop(self, entry: _ScheduledEntry) -> None:
        """Loop: start a run every period; a run still going when the next is due delays it (no overlap)."""
        assert entry.fixed_rate is not None
        loop = asyncio.get_running_loop()
        period = entry.fixed_rate.total_seconds()
        slots = asyncio.Semaphore(entry.concurrent)
        next_start = loop.time() + (entry.initial_delay.total_seconds() if entry.initial_delay else 0.0)
        while self._running:
            await asyncio.sleep(max(0.0, next_start - loop.time()))
            if not self._running:
                break
            task = await self._submit(entry, slots)  # waits for a run in flight when the slots are taken
            if task is None:
                break
            next_start = loop.time() + period

    async def _run_fixed_delay_loop(self, entry: _ScheduledEntry) -> None:
        """Loop: run, wait for the run to end, then wait the delay."""
        assert entry.fixed_delay is not None
        if entry.initial_delay:
            await asyncio.sleep(entry.initial_delay.total_seconds())
        slots = asyncio.Semaphore(1)
        while self._running:
            task = await self._submit(entry, slots)
            if task is None:
                break
            await asyncio.wait((task,))  # not `await task`: stopping the loop must not cancel the run
            if not self._running:
                break
            await asyncio.sleep(entry.fixed_delay.total_seconds())

    async def _invoke(
        self, bean: Any, method: Callable[..., Any], lock: str | None = None, lock_ttl: float = 60.0
    ) -> None:
        """Run one tick of a scheduled method; every failure is logged with the job's name, never raised.

        An async method is awaited on the event loop; a **synchronous** method is offloaded to a worker thread
        (the executor's pool when it has one, ``asyncio.to_thread`` otherwise) so a blocking body does not
        stall the loop — and therefore the whole application — for the duration of the task.

        When *lock* is set, the lock is taken first; if it is held elsewhere the tick is **skipped** (so only
        one instance in a cluster runs the job). The run is time-boxed to *lock_ttl*, when the lock ends
        whatever the run does: a run still going then is cancelled (a synchronous body's thread cannot be, and
        goes on). The lock is released once the run ends. A failure to take or release the lock is logged like
        a failure of the run (audit #186: a cron or fixed-rate run is not awaited by its loop, so nothing else
        would report it).
        """
        name = _job_name(bean, method)
        if lock is not None:
            try:
                acquired = await self._lock.try_acquire(lock, lock_ttl)
            except Exception:
                logger.exception("scheduled task '%s' skipped: acquiring lock %r failed", name, lock)
                return
            if not acquired:
                logger.debug("scheduled task '%s' skipped — lock %r held elsewhere", name, lock)
                return
        deadline = asyncio.timeout(lock_ttl) if lock is not None else None
        try:
            async with deadline if deadline is not None else contextlib.nullcontext():
                await self._call(method)
        except Exception:
            if deadline is not None and deadline.expired():
                logger.error(
                    "scheduled task '%s' ran past the ttl of lock %r (%.1f s) and was cancelled: another instance "
                    "may run it now; raise lock_ttl above the job's longest run",
                    name,
                    lock,
                    lock_ttl,
                )
            else:
                logger.exception("scheduled task '%s' failed", name)
        finally:
            if lock is not None:
                # Released even when the run is being cancelled (a stop cut short): shielded, logged on failure.
                _result, error, _cancelled = await run_shielded(self._lock.release(lock))
                if error is not None:
                    logger.error(
                        "scheduled task '%s': releasing lock %r failed; it ends at its ttl",
                        name,
                        lock,
                        exc_info=(type(error), error, error.__traceback__),
                    )

    async def _call(self, method: Callable[..., Any]) -> None:
        if inspect.iscoroutinefunction(method):
            await method()
            return
        run_sync = getattr(self._executor, "run_sync", None)
        result = await (run_sync(method) if callable(run_sync) else asyncio.to_thread(method))
        if inspect.isawaitable(result):  # rare: sync method returning an awaitable
            await result
