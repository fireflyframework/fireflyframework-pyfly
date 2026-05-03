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
"""Tests for SagaRecoveryService — recovery of interrupted saga executions."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest

from pyfly.transactional.saga.persistence.recovery import SagaRecoveryService
from pyfly.transactional.shared.persistence.memory import InMemoryPersistenceAdapter

pytestmark = pytest.mark.anyio
anyio_backend = "asyncio"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_events_port() -> AsyncMock:
    """Create a mock TransactionalEventsPort."""
    port = AsyncMock()
    port.on_start = AsyncMock()
    port.on_step_success = AsyncMock()
    port.on_step_failed = AsyncMock()
    port.on_compensated = AsyncMock()
    port.on_completed = AsyncMock()
    return port


async def _persist_stale_saga(
    adapter: InMemoryPersistenceAdapter,
    correlation_id: str,
    saga_name: str = "order-saga",
    started_at: datetime | None = None,
) -> None:
    """Persist an IN_FLIGHT saga with an old started_at timestamp."""
    old_time = started_at or (datetime.now(UTC) - timedelta(hours=1))
    await adapter.persist_state(
        {
            "correlation_id": correlation_id,
            "saga_name": saga_name,
            "status": "IN_FLIGHT",
            "started_at": old_time,
        }
    )


async def _persist_completed_saga(
    adapter: InMemoryPersistenceAdapter,
    correlation_id: str,
    saga_name: str = "order-saga",
    completed_at: datetime | None = None,
) -> None:
    """Persist a completed saga with a specific completed_at timestamp."""
    old_time = completed_at or (datetime.now(UTC) - timedelta(hours=48))
    await adapter.persist_state(
        {
            "correlation_id": correlation_id,
            "saga_name": saga_name,
            "status": "IN_FLIGHT",
            "started_at": datetime.now(UTC) - timedelta(hours=49),
        }
    )
    await adapter.mark_completed(correlation_id, successful=True)
    # Manually override completed_at for deterministic test control
    state = await adapter.get_state(correlation_id)
    assert state is not None
    state["completed_at"] = old_time


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def adapter() -> InMemoryPersistenceAdapter:
    return InMemoryPersistenceAdapter()


@pytest.fixture
def events_port() -> AsyncMock:
    return _make_events_port()


@pytest.fixture
def recovery_service(
    adapter: InMemoryPersistenceAdapter,
) -> SagaRecoveryService:
    return SagaRecoveryService(persistence_port=adapter)


@pytest.fixture
def recovery_service_with_events(
    adapter: InMemoryPersistenceAdapter,
    events_port: AsyncMock,
) -> SagaRecoveryService:
    return SagaRecoveryService(persistence_port=adapter, events_port=events_port)


# ---------------------------------------------------------------------------
# recover_stale — finds and marks stale sagas
# ---------------------------------------------------------------------------


class TestRecoverStaleFindsAndMarks:
    """recover_stale identifies stale in-flight sagas and marks them FAILED."""

    async def test_marks_stale_saga_as_failed(
        self,
        adapter: InMemoryPersistenceAdapter,
        recovery_service: SagaRecoveryService,
    ) -> None:
        await _persist_stale_saga(adapter, "stale-1")

        await recovery_service.recover_stale(stale_threshold_seconds=60)

        state = await adapter.get_state("stale-1")
        assert state is not None
        assert state["status"] == "FAILED"
        assert state["successful"] is False

    async def test_marks_multiple_stale_sagas_as_failed(
        self,
        adapter: InMemoryPersistenceAdapter,
        recovery_service: SagaRecoveryService,
    ) -> None:
        await _persist_stale_saga(adapter, "stale-1")
        await _persist_stale_saga(adapter, "stale-2")
        await _persist_stale_saga(adapter, "stale-3")

        await recovery_service.recover_stale(stale_threshold_seconds=60)

        for cid in ("stale-1", "stale-2", "stale-3"):
            state = await adapter.get_state(cid)
            assert state is not None
            assert state["status"] == "FAILED"

    async def test_does_not_mark_recent_in_flight_sagas(
        self,
        adapter: InMemoryPersistenceAdapter,
        recovery_service: SagaRecoveryService,
    ) -> None:
        # A saga started just now — not stale
        await adapter.persist_state(
            {
                "correlation_id": "recent-1",
                "saga_name": "order-saga",
                "status": "IN_FLIGHT",
                "started_at": datetime.now(UTC),
            }
        )

        await recovery_service.recover_stale(stale_threshold_seconds=600)

        state = await adapter.get_state("recent-1")
        assert state is not None
        assert state["status"] == "IN_FLIGHT"

    async def test_does_not_mark_already_completed_sagas(
        self,
        adapter: InMemoryPersistenceAdapter,
        recovery_service: SagaRecoveryService,
    ) -> None:
        # A saga that is COMPLETED — even if old, should not be re-marked
        old_time = datetime.now(UTC) - timedelta(hours=2)
        await adapter.persist_state(
            {
                "correlation_id": "done-1",
                "saga_name": "order-saga",
                "status": "IN_FLIGHT",
                "started_at": old_time,
            }
        )
        await adapter.mark_completed("done-1", successful=True)

        await recovery_service.recover_stale(stale_threshold_seconds=60)

        state = await adapter.get_state("done-1")
        assert state is not None
        assert state["status"] == "COMPLETED"
        assert state["successful"] is True


# ---------------------------------------------------------------------------
# recover_stale — returns correct count
# ---------------------------------------------------------------------------


class TestRecoverStaleCount:
    """recover_stale returns the number of sagas recovered."""

    async def test_returns_count_of_recovered_sagas(
        self,
        adapter: InMemoryPersistenceAdapter,
        recovery_service: SagaRecoveryService,
    ) -> None:
        await _persist_stale_saga(adapter, "stale-1")
        await _persist_stale_saga(adapter, "stale-2")

        count = await recovery_service.recover_stale(stale_threshold_seconds=60)

        assert count == 2

    async def test_returns_zero_when_no_stale_sagas(
        self,
        adapter: InMemoryPersistenceAdapter,
        recovery_service: SagaRecoveryService,
    ) -> None:
        count = await recovery_service.recover_stale(stale_threshold_seconds=600)

        assert count == 0

    async def test_returns_zero_with_only_recent_sagas(
        self,
        adapter: InMemoryPersistenceAdapter,
        recovery_service: SagaRecoveryService,
    ) -> None:
        await adapter.persist_state(
            {
                "correlation_id": "recent-1",
                "saga_name": "order-saga",
                "status": "IN_FLIGHT",
                "started_at": datetime.now(UTC),
            }
        )

        count = await recovery_service.recover_stale(stale_threshold_seconds=600)

        assert count == 0

    async def test_counts_only_in_flight_stale_sagas(
        self,
        adapter: InMemoryPersistenceAdapter,
        recovery_service: SagaRecoveryService,
    ) -> None:
        # One stale IN_FLIGHT saga
        await _persist_stale_saga(adapter, "stale-1")

        # One old COMPLETED saga (should not count)
        old_time = datetime.now(UTC) - timedelta(hours=2)
        await adapter.persist_state(
            {
                "correlation_id": "done-old",
                "saga_name": "order-saga",
                "status": "IN_FLIGHT",
                "started_at": old_time,
            }
        )
        await adapter.mark_completed("done-old", successful=True)

        count = await recovery_service.recover_stale(stale_threshold_seconds=60)

        assert count == 1


# ---------------------------------------------------------------------------
# recover_stale — events emitted
# ---------------------------------------------------------------------------


class TestRecoverStaleEvents:
    """recover_stale emits warning events when events_port is configured."""

    async def test_emits_on_completed_for_each_recovered_saga(
        self,
        adapter: InMemoryPersistenceAdapter,
        recovery_service_with_events: SagaRecoveryService,
        events_port: AsyncMock,
    ) -> None:
        await _persist_stale_saga(adapter, "stale-1", saga_name="order-saga")
        await _persist_stale_saga(adapter, "stale-2", saga_name="payment-saga")

        await recovery_service_with_events.recover_stale(stale_threshold_seconds=60)

        assert events_port.on_completed.call_count == 2
        call_args_list = events_port.on_completed.call_args_list
        calls = {
            (call.kwargs.get("name") or call.args[0], call.kwargs.get("correlation_id") or call.args[1])
            for call in call_args_list
        }
        assert ("order-saga", "stale-1") in calls
        assert ("payment-saga", "stale-2") in calls

    async def test_on_completed_called_with_success_false(
        self,
        adapter: InMemoryPersistenceAdapter,
        recovery_service_with_events: SagaRecoveryService,
        events_port: AsyncMock,
    ) -> None:
        await _persist_stale_saga(adapter, "stale-1", saga_name="order-saga")

        await recovery_service_with_events.recover_stale(stale_threshold_seconds=60)

        events_port.on_completed.assert_called_once()
        call = events_port.on_completed.call_args
        # success should be False
        success_arg = call.kwargs.get("success") if "success" in call.kwargs else call.args[2]
        assert success_arg is False

    async def test_no_events_emitted_when_no_stale_sagas(
        self,
        adapter: InMemoryPersistenceAdapter,
        recovery_service_with_events: SagaRecoveryService,
        events_port: AsyncMock,
    ) -> None:
        await recovery_service_with_events.recover_stale(stale_threshold_seconds=600)

        events_port.on_completed.assert_not_called()


# ---------------------------------------------------------------------------
# recover_stale — works with events_port=None
# ---------------------------------------------------------------------------


class TestRecoverStaleWithoutEventsPort:
    """recover_stale works correctly when events_port is None."""

    async def test_recovers_without_events_port(
        self,
        adapter: InMemoryPersistenceAdapter,
        recovery_service: SagaRecoveryService,
    ) -> None:
        await _persist_stale_saga(adapter, "stale-1")

        count = await recovery_service.recover_stale(stale_threshold_seconds=60)

        assert count == 1
        state = await adapter.get_state("stale-1")
        assert state is not None
        assert state["status"] == "FAILED"


# ---------------------------------------------------------------------------
# cleanup
# ---------------------------------------------------------------------------


class TestCleanup:
    """cleanup delegates to persistence_port.cleanup and returns count."""

    async def test_cleanup_delegates_to_persistence_port(
        self,
        adapter: InMemoryPersistenceAdapter,
        recovery_service: SagaRecoveryService,
    ) -> None:
        await _persist_completed_saga(adapter, "old-done-1")

        count = await recovery_service.cleanup(older_than_hours=24)

        assert count == 1
        assert await adapter.get_state("old-done-1") is None

    async def test_cleanup_returns_correct_count(
        self,
        adapter: InMemoryPersistenceAdapter,
        recovery_service: SagaRecoveryService,
    ) -> None:
        await _persist_completed_saga(adapter, "old-done-1")
        await _persist_completed_saga(adapter, "old-done-2")

        count = await recovery_service.cleanup(older_than_hours=24)

        assert count == 2

    async def test_cleanup_returns_zero_when_nothing_to_clean(
        self,
        adapter: InMemoryPersistenceAdapter,
        recovery_service: SagaRecoveryService,
    ) -> None:
        count = await recovery_service.cleanup(older_than_hours=24)

        assert count == 0

    async def test_cleanup_does_not_remove_recent_completed_sagas(
        self,
        adapter: InMemoryPersistenceAdapter,
        recovery_service: SagaRecoveryService,
    ) -> None:
        # Completed just now — should not be cleaned up
        await adapter.persist_state(
            {
                "correlation_id": "recent-done",
                "saga_name": "order-saga",
                "status": "IN_FLIGHT",
                "started_at": datetime.now(UTC),
            }
        )
        await adapter.mark_completed("recent-done", successful=True)

        count = await recovery_service.cleanup(older_than_hours=24)

        assert count == 0
        assert await adapter.get_state("recent-done") is not None

    async def test_cleanup_does_not_remove_in_flight_sagas(
        self,
        adapter: InMemoryPersistenceAdapter,
        recovery_service: SagaRecoveryService,
    ) -> None:
        await adapter.persist_state(
            {
                "correlation_id": "in-flight-1",
                "saga_name": "order-saga",
                "status": "IN_FLIGHT",
                "started_at": datetime.now(UTC) - timedelta(hours=48),
            }
        )

        count = await recovery_service.cleanup(older_than_hours=24)

        assert count == 0
        assert await adapter.get_state("in-flight-1") is not None


# ---------------------------------------------------------------------------
# Constructor
# ---------------------------------------------------------------------------


class TestConstructor:
    """SagaRecoveryService constructor accepts optional parameters."""

    def test_creates_with_persistence_port_only(
        self,
        adapter: InMemoryPersistenceAdapter,
    ) -> None:
        service = SagaRecoveryService(persistence_port=adapter)
        assert service is not None

    def test_creates_with_all_parameters(
        self,
        adapter: InMemoryPersistenceAdapter,
        events_port: AsyncMock,
    ) -> None:
        service = SagaRecoveryService(
            persistence_port=adapter,
            saga_engine=object(),
            events_port=events_port,
        )
        assert service is not None

    def test_creates_with_none_optional_parameters(
        self,
        adapter: InMemoryPersistenceAdapter,
    ) -> None:
        service = SagaRecoveryService(
            persistence_port=adapter,
            saga_engine=None,
            events_port=None,
        )
        assert service is not None
