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
"""``ProjectionRunner`` over the in-memory adapters (C066, C068, C179).

The relational lanes are in ``tests/integration/test_projection_matrix.py``; these runs cover the in-memory
store and checkpoints, the runner's paging and failure policy, its lease and its ``stop()``, and an event store
written against the SPI of earlier releases.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import create_async_engine

from pyfly.data.relational.sqlalchemy.transaction_manager import SqlAlchemyTransactionManager
from pyfly.data.transaction import TransactionTemplate, current_unit_of_work
from pyfly.eventsourcing.checkpoint import CheckpointStore, InMemoryCheckpointStore, SqlAlchemyCheckpointStore
from pyfly.eventsourcing.event import StoredEventEnvelope
from pyfly.eventsourcing.projection import FunctionProjection, ProjectionRunner
from pyfly.eventsourcing.store import InMemoryEventStore, SqlAlchemyEventStore
from pyfly.scheduling.adapters.lease_lock import LeaseLock


def _event(name: str) -> StoredEventEnvelope:
    return StoredEventEnvelope(event_type=name)


async def _append(store: InMemoryEventStore, count: int, *, prefix: str = "e") -> None:
    for index in range(count):
        await store.append(f"{prefix}-{index}", "Thing", [_event(f"{prefix}{index}")], expected_version=0)


async def _caught_up(checkpoints: CheckpointStore, store: InMemoryEventStore, *, timeout: float = 5.0) -> None:
    last = await store.last_position()
    deadline = time.monotonic() + timeout
    while (await checkpoints.position("p") or 0) < last:
        assert time.monotonic() < deadline, "the projection did not catch up"
        await asyncio.sleep(0.01)


class _Recorder:
    def __init__(self) -> None:
        self.seen: list[str] = []

    async def __call__(self, event: StoredEventEnvelope) -> None:
        self.seen.append(event.event_type)


class _PagingStore(InMemoryEventStore):
    """The in-memory store, recording the page size of every read."""

    def __init__(self) -> None:
        super().__init__()
        self.limits: list[int] = []

    async def stream_all(
        self, *, after_position: int | None = None, after_event_id: str | None = None, limit: int = 100
    ) -> list[StoredEventEnvelope]:
        self.limits.append(limit)
        return await super().stream_all(after_position=after_position, after_event_id=after_event_id, limit=limit)


async def test_the_in_memory_store_numbers_its_events_in_append_order() -> None:
    store = InMemoryEventStore()
    await _append(store, 3)
    events = await store.stream_all()
    assert [event.global_position for event in events] == [1, 2, 3]
    assert [event.event_type for event in await store.stream_all(after_position=1, limit=1)] == ["e1"]
    assert [event.event_type for event in await store.stream_all(after_event_id=events[0].event_id)] == ["e1", "e2"]
    assert await store.last_position() == 3
    with pytest.raises(ValueError, match="unknown"):
        await store.stream_all(after_event_id="unknown")


async def test_a_restarted_runner_resumes_from_the_shared_checkpoint() -> None:
    store, checkpoints = InMemoryEventStore(), InMemoryCheckpointStore()
    await _append(store, 5)
    first = _Recorder()
    runner = ProjectionRunner(FunctionProjection("p", first), store, checkpoints=checkpoints, poll_interval_s=0.01)
    await runner.start()
    await _caught_up(checkpoints, store)
    await runner.stop()

    await _append(store, 2, prefix="late")
    second = _Recorder()
    restarted = ProjectionRunner(FunctionProjection("p", second), store, checkpoints=checkpoints, poll_interval_s=0.01)
    await restarted.start()
    await _caught_up(checkpoints, store)
    await restarted.stop()

    assert first.seen == ["e0", "e1", "e2", "e3", "e4"]
    assert second.seen == ["late0", "late1"]


async def test_full_pages_are_read_back_to_back_and_the_page_size_is_configurable() -> None:
    """C068: 500 events took 4 s at the default poll interval; the page size was hard-coded."""
    store, checkpoints = _PagingStore(), InMemoryCheckpointStore()
    await _append(store, 500)
    recorder = _Recorder()
    runner = ProjectionRunner(FunctionProjection("p", recorder), store, checkpoints=checkpoints, batch_size=40)
    started = time.monotonic()
    await runner.start()
    await _caught_up(checkpoints, store)
    elapsed = time.monotonic() - started
    await runner.stop()

    assert len(recorder.seen) == 500
    # Sleeping the default poll interval after each of the 13 pages would take at least 12 s.
    assert elapsed < 3.0, f"catch-up slept between full pages: {elapsed:.2f}s"
    assert set(store.limits) == {40}


async def test_a_failed_event_stops_its_batch_and_is_retried_in_order() -> None:
    store, checkpoints = InMemoryEventStore(), InMemoryCheckpointStore()
    await _append(store, 5)
    attempts: dict[str, int] = {}
    applied: list[str] = []

    async def flaky(event: StoredEventEnvelope) -> None:
        attempts[event.event_type] = attempts.get(event.event_type, 0) + 1
        if event.event_type == "e2" and attempts["e2"] < 3:
            raise RuntimeError("downstream unavailable")
        applied.append(event.event_type)

    runner = ProjectionRunner(FunctionProjection("p", flaky), store, checkpoints=checkpoints, poll_interval_s=0.01)
    await runner.start()
    await _caught_up(checkpoints, store)
    await runner.stop()

    # In-memory checkpoints are not transactional: what a failed batch applied before its failure is kept,
    # and only the failed event (and what follows it) is delivered again.
    assert applied == ["e0", "e1", "e2", "e3", "e4"]
    assert attempts == {"e0": 1, "e1": 1, "e2": 3, "e3": 1, "e4": 1}


async def test_without_checkpoints_the_runner_keeps_its_position_in_memory() -> None:
    store = InMemoryEventStore()
    await _append(store, 3)
    recorder = _Recorder()
    runner = ProjectionRunner(FunctionProjection("p", recorder), store, poll_interval_s=0.01)
    await runner.start()
    for _ in range(200):
        if len(recorder.seen) == 3:
            break
        await asyncio.sleep(0.01)
    await _append(store, 1, prefix="more")
    for _ in range(200):
        if len(recorder.seen) == 4:
            break
        await asyncio.sleep(0.01)
    await runner.stop()
    await runner.stop()  # idempotent
    assert recorder.seen == ["e0", "e1", "e2", "more0"]


async def test_a_durable_store_with_checkpoints_in_memory_is_reported(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Review of WP08: a runner over the SQL event store without ``checkpoints=`` replays the store at every start
    and runs on every replica (C066, C067), silently. It still runs, and says so at start."""
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'events.db'}")
    try:
        store = SqlAlchemyEventStore(engine)
        await store.start()
        durable = ProjectionRunner(FunctionProjection("durable", _Recorder()), store, poll_interval_s=0.01)
        with caplog.at_level("WARNING", logger="pyfly.eventsourcing.projection"):
            await durable.start()
            await durable.stop()
        warnings = [record for record in caplog.records if record.msg == "projection_checkpoints_in_memory"]
        assert [record.levelname for record in warnings] == ["WARNING"]
        assert warnings[0].projection == "durable"  # type: ignore[attr-defined]

        caplog.clear()
        with caplog.at_level("WARNING", logger="pyfly.eventsourcing.projection"):
            checkpointed = ProjectionRunner(
                FunctionProjection("checkpointed", _Recorder()),
                store,
                poll_interval_s=0.01,
                checkpoints=SqlAlchemyCheckpointStore(engine),
            )
            await checkpointed.start()
            await checkpointed.stop()
            in_memory = ProjectionRunner(FunctionProjection("memory", _Recorder()), InMemoryEventStore())
            await in_memory.start()
            await in_memory.stop()
        assert "projection_checkpoints_in_memory" not in caplog.text
    finally:
        await engine.dispose()


async def test_the_runner_works_outside_the_transaction_that_started_it() -> None:
    """A runner started inside a unit of work must not run its batches in that unit (which ends long before)."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    template = TransactionTemplate(SqlAlchemyTransactionManager.for_engine(engine))
    store = InMemoryEventStore()
    await _append(store, 2)
    units: list[object] = []

    async def handle(event: StoredEventEnvelope) -> None:
        units.append(current_unit_of_work())

    runner = ProjectionRunner(FunctionProjection("p", handle), store, poll_interval_s=0.01)
    try:
        async with template.transaction():
            await runner.start()
            await runner.start()  # idempotent
        for _ in range(200):
            if len(units) == 2:
                break
            await asyncio.sleep(0.01)
    finally:
        await runner.stop()
        await engine.dispose()
    assert units == [None, None]


class _RecordingLease(LeaseLock):
    """A lease lock on a real table that records what the runner asks of it."""

    def __init__(self, engine: object, owner: str) -> None:
        super().__init__(engine, owner=owner)
        self.calls: list[str] = []

    async def try_acquire(self, name: str, ttl: float) -> bool:
        taken = await super().try_acquire(name, ttl)
        self.calls.append(f"try_acquire:{taken}")
        return taken

    async def extend(self, name: str, ttl: float) -> bool:
        extended = await super().extend(name, ttl)
        self.calls.append(f"extend:{extended}")
        return extended

    async def release(self, name: str) -> None:
        self.calls.append("release")
        await super().release(name)


async def test_a_runner_that_loses_its_lease_lets_go_of_that_acquisition(tmp_path: Path) -> None:
    """The lease lock keeps each acquisition until it is released: a runner whose lease ran out while a batch was
    in flight (another node took it) now releases that acquisition instead of keeping one per lease it lost."""
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'leases.db'}")
    mine, other = _RecordingLease(engine, "node-a"), LeaseLock(engine, owner="node-b")
    store = InMemoryEventStore()
    await _append(store, 1)
    in_batch, finish = asyncio.Event(), asyncio.Event()

    async def slow(event: StoredEventEnvelope) -> None:
        in_batch.set()
        await finish.wait()

    runner = ProjectionRunner(
        FunctionProjection("p", slow),
        store,
        checkpoints=InMemoryCheckpointStore(),
        lease=mine,
        lease_ttl_s=0.2,
        poll_interval_s=0.01,
    )
    try:
        await runner.start()
        await asyncio.wait_for(in_batch.wait(), 5)
        deadline = time.monotonic() + 5
        while not await other.try_acquire("pyfly.projection.p", 30):  # once the runner's lease has run out
            assert time.monotonic() < deadline, "the runner's lease never ran out"
            await asyncio.sleep(0.05)
        finish.set()
        while "extend:False" not in mine.calls:
            assert time.monotonic() < deadline, "the runner never found its lease gone"
            await asyncio.sleep(0.01)
        while mine.calls[-1] == "extend:False":
            await asyncio.sleep(0.01)
    finally:
        await runner.stop()
        await engine.dispose()

    lost = mine.calls.index("extend:False")
    assert mine.calls[lost + 1] == "release", mine.calls


async def test_stop_lets_a_cancellation_of_its_caller_through() -> None:
    store = InMemoryEventStore()
    await _append(store, 1)
    in_batch = asyncio.Event()

    async def stuck(event: StoredEventEnvelope) -> None:
        in_batch.set()
        await asyncio.Event().wait()

    runner = ProjectionRunner(FunctionProjection("p", stuck), store, poll_interval_s=0.01)
    await runner.start()
    await asyncio.wait_for(in_batch.wait(), 5)
    stopping = asyncio.create_task(runner.stop())
    await asyncio.sleep(0.02)  # stop() is waiting for the batch in flight
    stopping.cancel()
    with pytest.raises(asyncio.CancelledError):
        await stopping
    await runner.stop()  # nothing left to stop


class _LegacyStore(InMemoryEventStore):
    """A store written against the SPI of earlier releases: ``stream_all`` has no ``after_position``."""

    async def stream_all(self, *, after_event_id: str | None = None, limit: int = 100) -> list[StoredEventEnvelope]:  # type: ignore[override]
        events = await super().stream_all(after_event_id=after_event_id, limit=limit)
        for event in events:
            event.global_position = None
        return events


async def test_a_store_without_global_positions_is_still_projected(caplog: pytest.LogCaptureFixture) -> None:
    store = _LegacyStore()
    await _append(store, 3)
    recorder = _Recorder()
    runner = ProjectionRunner(FunctionProjection("p", recorder), store, poll_interval_s=0.01)
    await runner.start()
    for _ in range(200):
        if len(recorder.seen) == 3:
            break
        await asyncio.sleep(0.01)
    await runner.stop()
    assert recorder.seen == ["e0", "e1", "e2"]
    assert "projection_store_without_positions" in caplog.text


async def test_checkpoints_need_a_store_with_global_positions() -> None:
    with pytest.raises(TypeError, match="after_position"):
        ProjectionRunner(FunctionProjection("p", _Recorder()), _LegacyStore(), checkpoints=InMemoryCheckpointStore())


def test_the_runner_validates_its_settings() -> None:
    projection = FunctionProjection("p", _Recorder())
    with pytest.raises(ValueError, match="batch_size"):
        ProjectionRunner(projection, InMemoryEventStore(), batch_size=0)
    with pytest.raises(ValueError, match="start_from"):
        ProjectionRunner(projection, InMemoryEventStore(), start_from="middle")  # type: ignore[arg-type]
