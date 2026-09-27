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
"""The scheduler never overlaps a job, drains every job type alike, and reports lock errors (WP13-06..08).

- C071: ``fixed_rate`` and ``cron`` used to submit a run each period whether the last one had ended or not, so
  a slow run overlapped the next ones without bound (each ``@transactional`` run holding a pooled
  connection). A run still going now delays the next one; ``@scheduled(concurrent=N)`` opts in to *N*.
- C151: ``stop()`` cancelled an in-flight ``fixed_delay`` run at once (rolling its transaction back) while it
  let ``fixed_rate``/``cron`` runs finish. Every run is drained now, and cancelled only when the drain is cut
  short.
- C152: a failure to take or release the lock escaped the run's error handling (``Task exception was never
  retrieved``, without the job's name). It is logged with the job's name, and the run is skipped.
- A run that outlives its lock's TTL is cancelled: the lock has ended and another node may run the job. It
  releases the lock from its own task, so when it ends after the job's next run took the lock, that run keeps
  it.

The database side (real ``@transactional`` runs, a real lease lock) is in
``tests/integration/test_scheduler_units_of_work_matrix.py``.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import threading
import time
from datetime import timedelta
from unittest.mock import patch

import pytest

from pyfly.scheduling.adapters.thread_executor import ThreadPoolTaskExecutor
from pyfly.scheduling.decorators import scheduled
from pyfly.scheduling.lock import InProcessDistributedLock, LocalLock
from pyfly.scheduling.task_scheduler import TaskScheduler


class Probe:
    """Counts the runs in flight."""

    def __init__(self) -> None:
        self.in_flight = 0
        self.max_in_flight = 0
        self.runs = 0
        self.completed = 0
        self.cancelled = 0

    async def run(self, seconds: float) -> None:
        self.runs += 1
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            await asyncio.sleep(seconds)
            self.completed += 1
        except asyncio.CancelledError:
            self.cancelled += 1
            raise
        finally:
            self.in_flight -= 1


class SlowRate:
    def __init__(self) -> None:
        self.probe = Probe()

    @scheduled(fixed_rate=timedelta(seconds=0.02))
    async def tick(self) -> None:
        await self.probe.run(0.1)


class SlowRateTwoAtOnce:
    def __init__(self) -> None:
        self.probe = Probe()

    @scheduled(fixed_rate=timedelta(seconds=0.01), concurrent=2)
    async def tick(self) -> None:
        await self.probe.run(0.1)


class SlowCron:
    def __init__(self) -> None:
        self.probe = Probe()

    @scheduled(cron="* * * * * *")
    async def tick(self) -> None:
        await self.probe.run(0.1)


class LongDelayJob:
    def __init__(self) -> None:
        self.probe = Probe()

    @scheduled(fixed_delay=timedelta(seconds=10))
    async def batch(self) -> None:
        await self.probe.run(0.2)


class LongRateJob:
    def __init__(self) -> None:
        self.probe = Probe()

    @scheduled(fixed_rate=timedelta(seconds=10))
    async def batch(self) -> None:
        await self.probe.run(0.2)


async def _until(condition: object, timeout: float = 5.0) -> None:
    for _ in range(int(timeout / 0.005)):
        if callable(condition) and condition():
            return
        await asyncio.sleep(0.005)
    raise AssertionError("condition never held")


async def test_a_slow_fixed_rate_job_never_overlaps_itself() -> None:
    bean = SlowRate()
    scheduler = TaskScheduler()
    scheduler.discover([bean])
    await scheduler.start()
    await asyncio.sleep(0.35)
    await scheduler.stop()

    assert bean.probe.max_in_flight == 1
    assert bean.probe.runs >= 2  # late, back to back: never skipped, never concurrent


async def test_a_slow_cron_job_never_overlaps_itself() -> None:
    bean = SlowCron()
    with patch("pyfly.scheduling.task_scheduler.CronExpression") as cron:
        cron.return_value.seconds_until_next.return_value = 0.01
        scheduler = TaskScheduler()
        scheduler.discover([bean])
        await scheduler.start()
        await asyncio.sleep(0.35)
        await scheduler.stop()

    assert bean.probe.max_in_flight == 1
    assert bean.probe.runs >= 2


async def test_concurrent_opts_in_to_that_many_runs_at_once() -> None:
    bean = SlowRateTwoAtOnce()
    scheduler = TaskScheduler()
    scheduler.discover([bean])
    await scheduler.start()
    await asyncio.sleep(0.3)
    await scheduler.stop()

    assert bean.probe.max_in_flight == 2


def test_concurrent_is_validated() -> None:
    with pytest.raises(ValueError, match="at least 1"):
        scheduled(fixed_rate=timedelta(seconds=1), concurrent=0)
    with pytest.raises(ValueError, match="fixed_delay"):
        scheduled(fixed_delay=timedelta(seconds=1), concurrent=2)


@pytest.mark.parametrize("bean_type", [LongDelayJob, LongRateJob], ids=["fixed_delay", "fixed_rate"])
async def test_stop_drains_a_run_in_flight_of_every_trigger(bean_type: type[LongDelayJob | LongRateJob]) -> None:
    bean = bean_type()
    scheduler = TaskScheduler()
    scheduler.discover([bean])
    await scheduler.start()
    await _until(lambda: bean.probe.in_flight == 1)

    await scheduler.stop()

    assert bean.probe.completed == 1
    assert bean.probe.cancelled == 0


async def test_a_stop_cut_short_cancels_and_awaits_the_runs_in_flight() -> None:
    bean = LongDelayJob()
    scheduler = TaskScheduler()
    scheduler.discover([bean])
    await scheduler.start()
    await _until(lambda: bean.probe.in_flight == 1)

    with pytest.raises(TimeoutError):
        await asyncio.wait_for(scheduler.stop(), 0.05)  # the context's shutdown timeout

    assert bean.probe.cancelled == 1
    assert bean.probe.in_flight == 0


async def test_start_and_stop_are_idempotent() -> None:
    bean = SlowRate()
    scheduler = TaskScheduler()
    scheduler.discover([bean])
    await scheduler.start()
    await scheduler.start()
    assert len(scheduler._loop_tasks) == 1
    await scheduler.stop()
    await scheduler.stop()
    assert scheduler._loop_tasks == []


class _FailingLock(LocalLock):
    def __init__(self, *, acquire: bool) -> None:
        self._fail_acquire = acquire

    async def try_acquire(self, name: str, ttl: float) -> bool:
        if self._fail_acquire:
            raise ConnectionRefusedError("lock database down")
        return True

    async def release(self, name: str) -> None:
        raise ConnectionResetError("lock connection lost")


class Locked:
    def __init__(self) -> None:
        self.runs = 0

    @scheduled(fixed_rate=timedelta(seconds=10), lock="report")
    async def report(self) -> None:
        self.runs += 1


async def test_a_failure_to_take_the_lock_is_logged_with_the_job_name(caplog: pytest.LogCaptureFixture) -> None:
    bean = Locked()
    scheduler = TaskScheduler(lock=_FailingLock(acquire=True))
    scheduler.discover([bean])
    with caplog.at_level(logging.ERROR, logger="pyfly.scheduling.task_scheduler"):
        await scheduler._invoke(bean, bean.report, lock="report", lock_ttl=5.0)

    assert bean.runs == 0
    record = next(r for r in caplog.records if "acquiring lock" in r.getMessage())
    assert "Locked.report" in record.getMessage()
    assert record.exc_info is not None and isinstance(record.exc_info[1], ConnectionRefusedError)


async def test_a_failure_to_release_the_lock_is_logged_with_the_job_name(caplog: pytest.LogCaptureFixture) -> None:
    bean = Locked()
    scheduler = TaskScheduler(lock=_FailingLock(acquire=False))
    with caplog.at_level(logging.ERROR, logger="pyfly.scheduling.task_scheduler"):
        await scheduler._invoke(bean, bean.report, lock="report", lock_ttl=5.0)

    assert bean.runs == 1
    record = next(r for r in caplog.records if "releasing lock" in r.getMessage())
    assert "Locked.report" in record.getMessage()


async def test_lock_errors_of_a_running_loop_never_escape_as_unretrieved_task_exceptions(
    caplog: pytest.LogCaptureFixture,
) -> None:
    bean = Locked()
    scheduler = TaskScheduler(lock=_FailingLock(acquire=True))
    scheduler.discover([bean])
    loop = asyncio.get_running_loop()
    unhandled: list[dict[str, object]] = []
    loop.set_exception_handler(lambda _loop, context: unhandled.append(context))
    try:
        with caplog.at_level(logging.ERROR, logger="pyfly.scheduling.task_scheduler"):
            await scheduler.start()
            await _until(lambda: any("acquiring lock" in r.getMessage() for r in caplog.records))
            await scheduler.stop()
    finally:
        loop.set_exception_handler(None)
    assert unhandled == []


class Overrunning:
    def __init__(self) -> None:
        self.probe = Probe()

    @scheduled(fixed_rate=timedelta(seconds=10), lock="overrun", lock_ttl=timedelta(seconds=0.05))
    async def overrun(self) -> None:
        await self.probe.run(1.0)


async def test_a_run_that_outlives_its_lock_ttl_is_cancelled(caplog: pytest.LogCaptureFixture) -> None:
    bean = Overrunning()
    lock = InProcessDistributedLock()
    scheduler = TaskScheduler(lock=lock)
    with caplog.at_level(logging.ERROR, logger="pyfly.scheduling.task_scheduler"):
        await scheduler._invoke(bean, bean.overrun, lock="overrun", lock_ttl=0.05)

    assert bean.probe.cancelled == 1
    assert any("ran past the ttl" in r.getMessage() and "Overrunning.overrun" in r.getMessage() for r in caplog.records)
    assert await lock.try_acquire("overrun", 1.0)  # released


class LateEnder:
    """Its first run is cancelled at its lock's ttl and takes a while to end (its shielded ``COMMIT`` is in
    flight); its second run holds the lock until told to end."""

    def __init__(self) -> None:
        self.runs: list[str] = []
        self.first_cancelled = asyncio.Event()
        self.first_may_end = asyncio.Event()
        self.second_holds = asyncio.Event()
        self.second_may_end = asyncio.Event()

    async def run(self) -> None:
        self.runs.append(f"run-{len(self.runs) + 1}")
        if len(self.runs) == 1:
            try:
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                self.first_cancelled.set()
                await asyncio.shield(self.first_may_end.wait())
                raise
        elif len(self.runs) == 2:
            self.second_holds.set()
            await self.second_may_end.wait()


async def test_the_late_release_of_a_run_cancelled_at_its_ttl_leaves_the_next_holders_lock() -> None:
    """The in-process lock tells holders apart per task: the release must come from the run's own task, or it
    ends the lock of whichever run took it since, and the job overlaps itself."""
    job = LateEnder()
    scheduler = TaskScheduler(lock=InProcessDistributedLock())

    first = asyncio.create_task(scheduler._invoke(job, job.run, lock="late", lock_ttl=0.05))
    await asyncio.wait_for(job.first_cancelled.wait(), 5)  # its lock ended at the ttl; the run is still ending
    second = asyncio.create_task(scheduler._invoke(job, job.run, lock="late", lock_ttl=30.0))
    await asyncio.wait_for(job.second_holds.wait(), 5)  # the next run took the lock
    job.first_may_end.set()
    await first  # the first run ends now, and releases its lock late

    await scheduler._invoke(job, job.run, lock="late", lock_ttl=30.0)  # the second run still holds the lock
    job.second_may_end.set()
    await second

    assert job.runs == ["run-1", "run-2"]


class SyncJob:
    def __init__(self) -> None:
        self.threads: list[str] = []

    @scheduled(fixed_rate=timedelta(seconds=10))
    def work(self) -> None:
        self.threads.append(threading.current_thread().name)


async def test_a_sync_job_runs_on_the_thread_executors_pool() -> None:
    bean = SyncJob()
    executor = ThreadPoolTaskExecutor(max_workers=1)
    executor._executor._thread_name_prefix = "pyfly-pool"
    scheduler = TaskScheduler(executor=executor)
    scheduler.discover([bean])
    await scheduler.start()
    await _until(lambda: bool(bean.threads))
    await scheduler.stop()

    assert bean.threads[0].startswith("pyfly-pool")


class OverrunningSyncJob:
    """A synchronous job past its lock's ttl: its run is cancelled, but its thread goes on."""

    def __init__(self) -> None:
        self.started = threading.Event()
        self.release = threading.Event()

    @scheduled(fixed_rate=timedelta(seconds=30), lock="sync", lock_ttl=timedelta(seconds=0.05))
    def work(self) -> None:
        self.started.set()
        self.release.wait(1.0)


class Heartbeat:
    """Counts the event loop's turns while it is free."""

    def __init__(self) -> None:
        self.ticks = 0
        self._task = asyncio.create_task(self._beat())

    async def _beat(self) -> None:
        while True:
            await asyncio.sleep(0.01)
            self.ticks += 1

    async def stop(self) -> None:
        self._task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._task


async def _start_overrunning_sync_job() -> tuple[OverrunningSyncJob, TaskScheduler]:
    bean = OverrunningSyncJob()
    scheduler = TaskScheduler(executor=ThreadPoolTaskExecutor(max_workers=1), lock=InProcessDistributedLock())
    scheduler.discover([bean])
    with patch.object(logging.getLogger("pyfly.scheduling.task_scheduler"), "error"):  # the ttl overrun
        await scheduler.start()
        await _until(bean.started.is_set)
        await asyncio.sleep(0.1)  # its run was cancelled at the ttl; its thread is still running
    return bean, scheduler


async def test_stopping_waits_for_a_thread_still_running_without_blocking_the_event_loop() -> None:
    bean, scheduler = await _start_overrunning_sync_job()
    heartbeat = Heartbeat()
    stopping = asyncio.create_task(scheduler.stop())
    await asyncio.sleep(0.2)
    try:
        assert not stopping.done()  # it waits for the thread
        assert heartbeat.ticks >= 5  # while the event loop goes on
    finally:
        bean.release.set()
        await asyncio.wait_for(stopping, 5)
        await heartbeat.stop()


async def test_a_stop_cut_short_leaves_a_thread_still_running_behind_at_once() -> None:
    bean, scheduler = await _start_overrunning_sync_job()
    started = time.monotonic()
    try:
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(scheduler.stop(), 0.05)  # the context's shutdown timeout
        assert time.monotonic() - started < 0.5  # not the thread's remaining second
    finally:
        bean.release.set()


async def test_runs_start_with_the_transaction_state_cleared() -> None:
    from pyfly.data.transaction.context import EMPTY, bind_state, current_state, reset_state

    seen: list[object] = []

    class Job:
        @scheduled(fixed_rate=timedelta(seconds=10))
        async def job(self) -> None:
            seen.append(current_state())

    scheduler = TaskScheduler()
    scheduler.discover([Job()])
    token = bind_state(EMPTY.with_read_only(True))  # the loops start from a state that is not the cleared one
    try:
        await scheduler.start()
    finally:
        reset_state(token)
    await _until(lambda: bool(seen))
    await scheduler.stop()
    assert seen == [EMPTY]
