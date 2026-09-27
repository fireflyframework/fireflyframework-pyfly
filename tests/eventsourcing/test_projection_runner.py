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
store and checkpoints, the runner's paging and failure policy, and an event store written against the SPI of
earlier releases.
"""

from __future__ import annotations

import asyncio
import time

import pytest
from sqlalchemy.ext.asyncio import create_async_engine

from pyfly.data.relational.sqlalchemy.transaction_manager import SqlAlchemyTransactionManager
from pyfly.data.transaction import TransactionTemplate, current_unit_of_work
from pyfly.eventsourcing.checkpoint import CheckpointStore, InMemoryCheckpointStore
from pyfly.eventsourcing.event import StoredEventEnvelope
from pyfly.eventsourcing.projection import FunctionProjection, ProjectionRunner
from pyfly.eventsourcing.store import InMemoryEventStore


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
    assert elapsed < 0.9, f"catch-up slept between full pages: {elapsed:.2f}s"
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
