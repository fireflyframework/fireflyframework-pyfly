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
"""Regression tests for event-sourcing fixes (v26.06.10).

- EventHandlerException is exported from the package (API symmetry w/ ConcurrencyError).
- Upcasters are applied on read (load + stream_all) — previously dead code.
- TransactionalOutbox exposes dead-lettered (exhausted) records.
- SqlAlchemyEventStore append translates a concurrent UNIQUE collision into
  ConcurrencyError instead of leaking a raw IntegrityError (TOCTOU fix).
"""

from __future__ import annotations

import asyncio
import dataclasses
from pathlib import Path

import pytest

from pyfly.eventsourcing.event import StoredEventEnvelope
from pyfly.eventsourcing.outbox import TransactionalOutbox
from pyfly.eventsourcing.store import ConcurrencyError, InMemoryEventStore, SqlAlchemyEventStore


def _env(aggregate_id: str, event_type: str, **payload: object) -> StoredEventEnvelope:
    return StoredEventEnvelope(
        aggregate_id=aggregate_id, aggregate_type="Account", event_type=event_type, payload=dict(payload)
    )


def test_event_handler_exception_is_exported_from_package() -> None:
    # Regression: EventHandlerException was defined in the submodule but, unlike
    # its sibling ConcurrencyError, never re-exported from pyfly.eventsourcing.
    from pyfly.eventsourcing import EventHandlerException
    from pyfly.eventsourcing.aggregate import EventHandlerException as Submodule

    assert EventHandlerException is Submodule


class _RenameUpcaster:
    """Upcasts the legacy event name to the current one (returns a copy)."""

    def applies_to(self, envelope: StoredEventEnvelope) -> bool:
        return envelope.event_type == "legacy.opened"

    def upcast(self, envelope: StoredEventEnvelope) -> StoredEventEnvelope:
        return dataclasses.replace(envelope, event_type="account.opened", payload={**envelope.payload, "upcast": True})


class TestUpcastersAppliedOnRead:
    @pytest.mark.asyncio
    async def test_load_and_stream_apply_upcasters(self) -> None:
        store = InMemoryEventStore(upcasters=[_RenameUpcaster()])
        await store.append("acc-1", "Account", [_env("acc-1", "legacy.opened", owner="Ada")], expected_version=0)

        loaded = await store.load("acc-1")
        assert [e.event_type for e in loaded] == ["account.opened"]
        assert loaded[0].payload["upcast"] is True

        streamed = await store.stream_all()
        assert [e.event_type for e in streamed] == ["account.opened"]

    @pytest.mark.asyncio
    async def test_no_upcasters_is_identity(self) -> None:
        store = InMemoryEventStore()
        await store.append("acc-1", "Account", [_env("acc-1", "legacy.opened")], expected_version=0)
        assert (await store.load("acc-1"))[0].event_type == "legacy.opened"


class TestOutboxDeadLetters:
    @pytest.mark.asyncio
    async def test_exhausted_records_are_surfaced(self) -> None:
        async def always_fail(_env: StoredEventEnvelope) -> None:
            raise RuntimeError("upstream down")

        outbox = TransactionalOutbox(publish=always_fail, max_attempts=2, poll_interval_s=0.02)
        record = await outbox.enqueue(_env("acc-1", "account.opened"))
        await outbox.start()
        for _ in range(100):
            await asyncio.sleep(0.02)
            if record.attempts >= 2:
                break
        await outbox.stop()

        assert record.attempts >= 2
        assert record.delivered is False
        assert await outbox.pending() == []  # excluded from the publish loop
        assert record in await outbox.dead_letters()  # but surfaced for inspection


class TestSqlAlchemyConcurrency:
    @pytest.mark.asyncio
    async def test_concurrent_append_raises_concurrency_error_not_raw_db_error(self, tmp_path: Path) -> None:
        pytest.importorskip("sqlalchemy")
        pytest.importorskip("aiosqlite")
        from sqlalchemy.ext.asyncio import create_async_engine

        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'es.db'}", connect_args={"timeout": 30})
        try:
            store = SqlAlchemyEventStore(engine)
            await store.initialize()
            await store.append("acc-1", "Account", [_env("acc-1", "account.opened")], expected_version=0)

            async def writer(event_type: str) -> None:
                await store.append("acc-1", "Account", [_env("acc-1", event_type)], expected_version=1)

            results = await asyncio.gather(writer("a.deposited"), writer("b.deposited"), return_exceptions=True)
            errors = [r for r in results if isinstance(r, BaseException)]

            # exactly one writer wins; the loser sees ConcurrencyError, NOT a raw
            # IntegrityError / OperationalError leaking the DB constraint.
            assert len(errors) == 1, results
            assert isinstance(errors[0], ConcurrencyError), f"got {type(errors[0]).__name__}: {errors[0]}"
            assert await store.latest_version("acc-1") == 2
        finally:
            await engine.dispose()
