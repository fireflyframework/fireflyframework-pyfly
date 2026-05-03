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
"""Tests for InMemoryPersistenceAdapter."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from pyfly.transactional.shared.persistence.memory import InMemoryPersistenceAdapter
from pyfly.transactional.shared.ports.outbound import TransactionalPersistencePort

pytestmark = pytest.mark.anyio
anyio_backend = "asyncio"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def adapter() -> InMemoryPersistenceAdapter:
    return InMemoryPersistenceAdapter()


# ---------------------------------------------------------------------------
# persist_state / get_state round-trip
# ---------------------------------------------------------------------------


class TestPersistAndGetState:
    """persist_state -> get_state round-trip."""

    async def test_persist_and_retrieve_state(self, adapter: InMemoryPersistenceAdapter) -> None:
        state = {"correlation_id": "abc-123", "data": "hello"}
        await adapter.persist_state(state)

        retrieved = await adapter.get_state("abc-123")

        assert retrieved is not None
        assert retrieved["correlation_id"] == "abc-123"
        assert retrieved["data"] == "hello"

    async def test_persist_state_sets_in_flight_status(self, adapter: InMemoryPersistenceAdapter) -> None:
        state = {"correlation_id": "abc-123"}
        await adapter.persist_state(state)

        retrieved = await adapter.get_state("abc-123")

        assert retrieved is not None
        assert retrieved["status"] == "IN_FLIGHT"

    async def test_persist_state_sets_started_at(self, adapter: InMemoryPersistenceAdapter) -> None:
        before = datetime.now(UTC)
        state = {"correlation_id": "abc-123"}
        await adapter.persist_state(state)
        after = datetime.now(UTC)

        retrieved = await adapter.get_state("abc-123")

        assert retrieved is not None
        assert before <= retrieved["started_at"] <= after

    async def test_persist_state_preserves_existing_status(self, adapter: InMemoryPersistenceAdapter) -> None:
        state = {"correlation_id": "abc-123", "status": "COMPLETED"}
        await adapter.persist_state(state)

        retrieved = await adapter.get_state("abc-123")

        assert retrieved is not None
        assert retrieved["status"] == "COMPLETED"

    async def test_persist_state_preserves_existing_started_at(self, adapter: InMemoryPersistenceAdapter) -> None:
        custom_time = datetime(2025, 1, 1, tzinfo=UTC)
        state = {"correlation_id": "abc-123", "started_at": custom_time}
        await adapter.persist_state(state)

        retrieved = await adapter.get_state("abc-123")

        assert retrieved is not None
        assert retrieved["started_at"] == custom_time

    async def test_get_state_returns_none_for_unknown_id(self, adapter: InMemoryPersistenceAdapter) -> None:
        result = await adapter.get_state("unknown-id")

        assert result is None


# ---------------------------------------------------------------------------
# update_step_status
# ---------------------------------------------------------------------------


class TestUpdateStepStatus:
    """update_step_status modifies step status within the transaction state."""

    async def test_update_step_status_creates_steps_dict_if_missing(self, adapter: InMemoryPersistenceAdapter) -> None:
        await adapter.persist_state({"correlation_id": "abc-123"})

        await adapter.update_step_status("abc-123", "step-1", "RUNNING")

        state = await adapter.get_state("abc-123")
        assert state is not None
        assert state["steps"]["step-1"]["status"] == "RUNNING"

    async def test_update_step_status_modifies_existing_step(self, adapter: InMemoryPersistenceAdapter) -> None:
        await adapter.persist_state(
            {
                "correlation_id": "abc-123",
                "steps": {"step-1": {"status": "PENDING"}},
            }
        )

        await adapter.update_step_status("abc-123", "step-1", "DONE")

        state = await adapter.get_state("abc-123")
        assert state is not None
        assert state["steps"]["step-1"]["status"] == "DONE"

    async def test_update_step_status_adds_new_step_to_existing_steps(
        self, adapter: InMemoryPersistenceAdapter
    ) -> None:
        await adapter.persist_state(
            {
                "correlation_id": "abc-123",
                "steps": {"step-1": {"status": "DONE"}},
            }
        )

        await adapter.update_step_status("abc-123", "step-2", "RUNNING")

        state = await adapter.get_state("abc-123")
        assert state is not None
        assert state["steps"]["step-1"]["status"] == "DONE"
        assert state["steps"]["step-2"]["status"] == "RUNNING"


# ---------------------------------------------------------------------------
# mark_completed
# ---------------------------------------------------------------------------


class TestMarkCompleted:
    """mark_completed sets status, successful flag, and completed_at."""

    async def test_mark_completed_successful(self, adapter: InMemoryPersistenceAdapter) -> None:
        await adapter.persist_state({"correlation_id": "abc-123"})

        before = datetime.now(UTC)
        await adapter.mark_completed("abc-123", successful=True)
        after = datetime.now(UTC)

        state = await adapter.get_state("abc-123")
        assert state is not None
        assert state["status"] == "COMPLETED"
        assert state["successful"] is True
        assert before <= state["completed_at"] <= after

    async def test_mark_completed_failed(self, adapter: InMemoryPersistenceAdapter) -> None:
        await adapter.persist_state({"correlation_id": "abc-123"})

        await adapter.mark_completed("abc-123", successful=False)

        state = await adapter.get_state("abc-123")
        assert state is not None
        assert state["status"] == "FAILED"
        assert state["successful"] is False
        assert state["completed_at"] is not None


# ---------------------------------------------------------------------------
# get_in_flight
# ---------------------------------------------------------------------------


class TestGetInFlight:
    """get_in_flight returns only states with status == IN_FLIGHT."""

    async def test_returns_only_in_flight_states(self, adapter: InMemoryPersistenceAdapter) -> None:
        await adapter.persist_state({"correlation_id": "flight-1"})
        await adapter.persist_state({"correlation_id": "flight-2"})
        await adapter.persist_state({"correlation_id": "done-1"})
        await adapter.mark_completed("done-1", successful=True)

        in_flight = await adapter.get_in_flight()

        ids = {s["correlation_id"] for s in in_flight}
        assert ids == {"flight-1", "flight-2"}

    async def test_returns_empty_list_when_none_in_flight(self, adapter: InMemoryPersistenceAdapter) -> None:
        await adapter.persist_state({"correlation_id": "done-1"})
        await adapter.mark_completed("done-1", successful=True)

        in_flight = await adapter.get_in_flight()

        assert in_flight == []


# ---------------------------------------------------------------------------
# get_stale
# ---------------------------------------------------------------------------


class TestGetStale:
    """get_stale returns states with started_at before the given cutoff."""

    async def test_returns_states_started_before_cutoff(self, adapter: InMemoryPersistenceAdapter) -> None:
        old_time = datetime(2024, 1, 1, tzinfo=UTC)
        recent_time = datetime(2026, 1, 1, tzinfo=UTC)

        await adapter.persist_state(
            {
                "correlation_id": "old-1",
                "started_at": old_time,
            }
        )
        await adapter.persist_state(
            {
                "correlation_id": "recent-1",
                "started_at": recent_time,
            }
        )

        cutoff = datetime(2025, 6, 1, tzinfo=UTC)
        stale = await adapter.get_stale(cutoff)

        ids = {s["correlation_id"] for s in stale}
        assert ids == {"old-1"}

    async def test_returns_empty_list_when_no_stale(self, adapter: InMemoryPersistenceAdapter) -> None:
        await adapter.persist_state({"correlation_id": "recent-1"})

        # Use a time in the distant past so nothing is stale
        cutoff = datetime(2020, 1, 1, tzinfo=UTC)
        stale = await adapter.get_stale(cutoff)

        assert stale == []


# ---------------------------------------------------------------------------
# cleanup
# ---------------------------------------------------------------------------


class TestCleanup:
    """cleanup removes old completed states and returns count removed."""

    async def test_removes_old_completed_states(self, adapter: InMemoryPersistenceAdapter) -> None:
        # Create a completed state with old completed_at
        old_completed_at = datetime.now(UTC) - timedelta(hours=5)
        await adapter.persist_state({"correlation_id": "old-done"})
        await adapter.mark_completed("old-done", successful=True)
        # Manually set completed_at to an old time
        state = await adapter.get_state("old-done")
        assert state is not None
        state["completed_at"] = old_completed_at

        # Create a recently completed state
        await adapter.persist_state({"correlation_id": "recent-done"})
        await adapter.mark_completed("recent-done", successful=True)

        # Create an in-flight state (should not be removed)
        await adapter.persist_state({"correlation_id": "in-flight"})

        count = await adapter.cleanup(older_than=timedelta(hours=1))

        assert count == 1
        assert await adapter.get_state("old-done") is None
        assert await adapter.get_state("recent-done") is not None
        assert await adapter.get_state("in-flight") is not None

    async def test_returns_zero_when_nothing_to_clean(self, adapter: InMemoryPersistenceAdapter) -> None:
        await adapter.persist_state({"correlation_id": "in-flight"})

        count = await adapter.cleanup(older_than=timedelta(hours=1))

        assert count == 0

    async def test_cleanup_removes_failed_states_too(self, adapter: InMemoryPersistenceAdapter) -> None:
        old_completed_at = datetime.now(UTC) - timedelta(hours=5)
        await adapter.persist_state({"correlation_id": "old-failed"})
        await adapter.mark_completed("old-failed", successful=False)
        state = await adapter.get_state("old-failed")
        assert state is not None
        state["completed_at"] = old_completed_at

        count = await adapter.cleanup(older_than=timedelta(hours=1))

        assert count == 1
        assert await adapter.get_state("old-failed") is None


# ---------------------------------------------------------------------------
# is_healthy
# ---------------------------------------------------------------------------


class TestIsHealthy:
    """is_healthy always returns True for in-memory adapter."""

    async def test_is_healthy_returns_true(self, adapter: InMemoryPersistenceAdapter) -> None:
        result = await adapter.is_healthy()

        assert result is True


# ---------------------------------------------------------------------------
# Protocol conformance
# ---------------------------------------------------------------------------


class TestProtocolConformance:
    """InMemoryPersistenceAdapter satisfies TransactionalPersistencePort."""

    def test_isinstance_check(self) -> None:
        adapter = InMemoryPersistenceAdapter()
        assert isinstance(adapter, TransactionalPersistencePort)
