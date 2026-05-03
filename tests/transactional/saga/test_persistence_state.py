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
"""Tests for saga persistence state types — SagaExecutionStatus, StepExecutionState, SagaExecutionState."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from pyfly.transactional.saga.persistence.state import (
    SagaExecutionState,
    SagaExecutionStatus,
    StepExecutionState,
)

# ── helpers ───────────────────────────────────────────────────


def _now() -> datetime:
    return datetime.now(UTC)


def _make_step_state(
    step_id: str = "step-1",
    status: str = "DONE",
    attempts: int = 1,
    latency_ms: float = 12.5,
    started_at: datetime | None = None,
    completed_at: datetime | None = None,
    error_message: str | None = None,
    compensated: bool = False,
) -> StepExecutionState:
    return StepExecutionState(
        step_id=step_id,
        status=status,
        attempts=attempts,
        latency_ms=latency_ms,
        started_at=started_at or _now(),
        completed_at=completed_at,
        error_message=error_message,
        compensated=compensated,
    )


def _make_saga_state(
    correlation_id: str = "corr-1",
    saga_name: str = "order-saga",
    status: SagaExecutionStatus = SagaExecutionStatus.IN_FLIGHT,
    started_at: datetime | None = None,
    completed_at: datetime | None = None,
    headers: dict[str, str] | None = None,
    steps: dict[str, StepExecutionState] | None = None,
    error_message: str | None = None,
) -> SagaExecutionState:
    return SagaExecutionState(
        correlation_id=correlation_id,
        saga_name=saga_name,
        status=status,
        started_at=started_at or _now(),
        completed_at=completed_at,
        headers=headers or {},
        steps=steps or {},
        error_message=error_message,
    )


# ── SagaExecutionStatus ──────────────────────────────────────


class TestSagaExecutionStatus:
    def test_enum_has_five_members(self) -> None:
        assert len(SagaExecutionStatus) == 5

    def test_in_flight_value(self) -> None:
        assert SagaExecutionStatus.IN_FLIGHT.value == "IN_FLIGHT"

    def test_completed_value(self) -> None:
        assert SagaExecutionStatus.COMPLETED.value == "COMPLETED"

    def test_failed_value(self) -> None:
        assert SagaExecutionStatus.FAILED.value == "FAILED"

    def test_compensating_value(self) -> None:
        assert SagaExecutionStatus.COMPENSATING.value == "COMPENSATING"

    def test_compensated_value(self) -> None:
        assert SagaExecutionStatus.COMPENSATED.value == "COMPENSATED"

    def test_is_str_enum(self) -> None:
        assert isinstance(SagaExecutionStatus.IN_FLIGHT, str)

    def test_string_comparison(self) -> None:
        assert SagaExecutionStatus.COMPLETED == "COMPLETED"

    def test_from_string_value(self) -> None:
        assert SagaExecutionStatus("FAILED") is SagaExecutionStatus.FAILED


# ── StepExecutionState ────────────────────────────────────────


class TestStepExecutionStateCreation:
    def test_fields_stored_correctly(self) -> None:
        started = _now()
        completed = _now()
        state = StepExecutionState(
            step_id="step-pay",
            status="DONE",
            attempts=3,
            latency_ms=55.2,
            started_at=started,
            completed_at=completed,
            error_message=None,
            compensated=False,
        )
        assert state.step_id == "step-pay"
        assert state.status == "DONE"
        assert state.attempts == 3
        assert state.latency_ms == 55.2
        assert state.started_at == started
        assert state.completed_at == completed
        assert state.error_message is None
        assert state.compensated is False

    def test_defaults_applied(self) -> None:
        state = StepExecutionState(step_id="step-1", status="PENDING")
        assert state.attempts == 0
        assert state.latency_ms == 0.0
        assert state.started_at is None
        assert state.completed_at is None
        assert state.error_message is None
        assert state.compensated is False


class TestStepExecutionStateFrozen:
    def test_frozen_raises_on_step_id_mutation(self) -> None:
        state = _make_step_state()
        with pytest.raises(AttributeError):
            state.step_id = "other"  # type: ignore[misc]

    def test_frozen_raises_on_status_mutation(self) -> None:
        state = _make_step_state()
        with pytest.raises(AttributeError):
            state.status = "FAILED"  # type: ignore[misc]

    def test_frozen_raises_on_attempts_mutation(self) -> None:
        state = _make_step_state()
        with pytest.raises(AttributeError):
            state.attempts = 99  # type: ignore[misc]

    def test_frozen_raises_on_latency_mutation(self) -> None:
        state = _make_step_state()
        with pytest.raises(AttributeError):
            state.latency_ms = 0.0  # type: ignore[misc]

    def test_frozen_raises_on_compensated_mutation(self) -> None:
        state = _make_step_state()
        with pytest.raises(AttributeError):
            state.compensated = True  # type: ignore[misc]

    def test_frozen_raises_on_error_message_mutation(self) -> None:
        state = _make_step_state()
        with pytest.raises(AttributeError):
            state.error_message = "boom"  # type: ignore[misc]


class TestStepExecutionStateWithError:
    def test_error_message_stored(self) -> None:
        state = _make_step_state(status="FAILED", error_message="timeout exceeded")
        assert state.error_message == "timeout exceeded"

    def test_compensated_step(self) -> None:
        state = _make_step_state(status="COMPENSATED", compensated=True)
        assert state.compensated is True
        assert state.status == "COMPENSATED"


# ── SagaExecutionState ────────────────────────────────────────


class TestSagaExecutionStateCreation:
    def test_required_fields_stored(self) -> None:
        started = _now()
        state = SagaExecutionState(
            correlation_id="corr-abc",
            saga_name="order-saga",
            status=SagaExecutionStatus.IN_FLIGHT,
            started_at=started,
        )
        assert state.correlation_id == "corr-abc"
        assert state.saga_name == "order-saga"
        assert state.status == SagaExecutionStatus.IN_FLIGHT
        assert state.started_at == started

    def test_defaults_applied(self) -> None:
        state = SagaExecutionState(
            correlation_id="corr-1",
            saga_name="test-saga",
            status=SagaExecutionStatus.IN_FLIGHT,
            started_at=_now(),
        )
        assert state.completed_at is None
        assert state.headers == {}
        assert state.steps == {}
        assert state.error_message is None

    def test_custom_headers(self) -> None:
        headers = {"x-tenant": "acme", "x-trace": "t-1"}
        state = _make_saga_state(headers=headers)
        assert state.headers == headers

    def test_custom_steps(self) -> None:
        step = _make_step_state(step_id="step-pay")
        state = _make_saga_state(steps={"step-pay": step})
        assert "step-pay" in state.steps
        assert state.steps["step-pay"].step_id == "step-pay"

    def test_with_error_message(self) -> None:
        state = _make_saga_state(
            status=SagaExecutionStatus.FAILED,
            error_message="Step 'ship' failed: timeout",
        )
        assert state.error_message == "Step 'ship' failed: timeout"
        assert state.status == SagaExecutionStatus.FAILED


class TestSagaExecutionStateFrozen:
    def test_frozen_raises_on_correlation_id_mutation(self) -> None:
        state = _make_saga_state()
        with pytest.raises(AttributeError):
            state.correlation_id = "new"  # type: ignore[misc]

    def test_frozen_raises_on_saga_name_mutation(self) -> None:
        state = _make_saga_state()
        with pytest.raises(AttributeError):
            state.saga_name = "new"  # type: ignore[misc]

    def test_frozen_raises_on_status_mutation(self) -> None:
        state = _make_saga_state()
        with pytest.raises(AttributeError):
            state.status = SagaExecutionStatus.COMPLETED  # type: ignore[misc]

    def test_frozen_raises_on_started_at_mutation(self) -> None:
        state = _make_saga_state()
        with pytest.raises(AttributeError):
            state.started_at = _now()  # type: ignore[misc]

    def test_frozen_raises_on_error_message_mutation(self) -> None:
        state = _make_saga_state()
        with pytest.raises(AttributeError):
            state.error_message = "boom"  # type: ignore[misc]


# ── to_dict ───────────────────────────────────────────────────


class TestSagaExecutionStateToDict:
    def test_minimal_to_dict(self) -> None:
        started = datetime(2026, 1, 15, 10, 30, 0, tzinfo=UTC)
        state = SagaExecutionState(
            correlation_id="corr-1",
            saga_name="order-saga",
            status=SagaExecutionStatus.IN_FLIGHT,
            started_at=started,
        )
        d = state.to_dict()
        assert d["correlation_id"] == "corr-1"
        assert d["saga_name"] == "order-saga"
        assert d["status"] == "IN_FLIGHT"
        assert d["started_at"] == started.isoformat()
        assert d["completed_at"] is None
        assert d["headers"] == {}
        assert d["steps"] == {}
        assert d["error_message"] is None

    def test_completed_saga_to_dict(self) -> None:
        started = datetime(2026, 1, 15, 10, 30, 0, tzinfo=UTC)
        completed = datetime(2026, 1, 15, 10, 30, 5, tzinfo=UTC)
        state = SagaExecutionState(
            correlation_id="corr-2",
            saga_name="order-saga",
            status=SagaExecutionStatus.COMPLETED,
            started_at=started,
            completed_at=completed,
            headers={"x-tenant": "acme"},
        )
        d = state.to_dict()
        assert d["status"] == "COMPLETED"
        assert d["completed_at"] == completed.isoformat()
        assert d["headers"] == {"x-tenant": "acme"}

    def test_steps_serialized_in_to_dict(self) -> None:
        started = datetime(2026, 1, 15, 10, 30, 0, tzinfo=UTC)
        step_started = datetime(2026, 1, 15, 10, 30, 1, tzinfo=UTC)
        step_completed = datetime(2026, 1, 15, 10, 30, 2, tzinfo=UTC)
        step = StepExecutionState(
            step_id="step-pay",
            status="DONE",
            attempts=2,
            latency_ms=45.3,
            started_at=step_started,
            completed_at=step_completed,
            error_message=None,
            compensated=False,
        )
        state = SagaExecutionState(
            correlation_id="corr-3",
            saga_name="order-saga",
            status=SagaExecutionStatus.IN_FLIGHT,
            started_at=started,
            steps={"step-pay": step},
        )
        d = state.to_dict()
        assert "step-pay" in d["steps"]
        sd = d["steps"]["step-pay"]
        assert sd["step_id"] == "step-pay"
        assert sd["status"] == "DONE"
        assert sd["attempts"] == 2
        assert sd["latency_ms"] == 45.3
        assert sd["started_at"] == step_started.isoformat()
        assert sd["completed_at"] == step_completed.isoformat()
        assert sd["error_message"] is None
        assert sd["compensated"] is False

    def test_step_with_none_timestamps_to_dict(self) -> None:
        step = StepExecutionState(step_id="step-1", status="PENDING")
        state = _make_saga_state(steps={"step-1": step})
        d = state.to_dict()
        sd = d["steps"]["step-1"]
        assert sd["started_at"] is None
        assert sd["completed_at"] is None

    def test_error_message_in_to_dict(self) -> None:
        state = _make_saga_state(
            status=SagaExecutionStatus.FAILED,
            error_message="step-ship failed",
        )
        d = state.to_dict()
        assert d["error_message"] == "step-ship failed"

    def test_multiple_steps_to_dict(self) -> None:
        step1 = _make_step_state(step_id="step-pay", status="DONE")
        step2 = _make_step_state(step_id="step-ship", status="FAILED", error_message="timeout")
        state = _make_saga_state(steps={"step-pay": step1, "step-ship": step2})
        d = state.to_dict()
        assert len(d["steps"]) == 2
        assert d["steps"]["step-pay"]["status"] == "DONE"
        assert d["steps"]["step-ship"]["status"] == "FAILED"
        assert d["steps"]["step-ship"]["error_message"] == "timeout"


# ── from_dict ─────────────────────────────────────────────────


class TestSagaExecutionStateFromDict:
    def test_round_trip_minimal(self) -> None:
        started = datetime(2026, 1, 15, 10, 30, 0, tzinfo=UTC)
        original = SagaExecutionState(
            correlation_id="corr-rt",
            saga_name="order-saga",
            status=SagaExecutionStatus.IN_FLIGHT,
            started_at=started,
        )
        reconstructed = SagaExecutionState.from_dict(original.to_dict())
        assert reconstructed.correlation_id == original.correlation_id
        assert reconstructed.saga_name == original.saga_name
        assert reconstructed.status == original.status
        assert reconstructed.started_at == original.started_at
        assert reconstructed.completed_at is None
        assert reconstructed.headers == {}
        assert reconstructed.steps == {}
        assert reconstructed.error_message is None

    def test_round_trip_with_steps(self) -> None:
        started = datetime(2026, 1, 15, 10, 30, 0, tzinfo=UTC)
        step_started = datetime(2026, 1, 15, 10, 30, 1, tzinfo=UTC)
        step_completed = datetime(2026, 1, 15, 10, 30, 2, tzinfo=UTC)
        step = StepExecutionState(
            step_id="step-pay",
            status="DONE",
            attempts=2,
            latency_ms=45.3,
            started_at=step_started,
            completed_at=step_completed,
            error_message=None,
            compensated=False,
        )
        original = SagaExecutionState(
            correlation_id="corr-rt2",
            saga_name="order-saga",
            status=SagaExecutionStatus.COMPLETED,
            started_at=started,
            completed_at=datetime(2026, 1, 15, 10, 31, 0, tzinfo=UTC),
            headers={"x-tenant": "acme"},
            steps={"step-pay": step},
            error_message=None,
        )
        reconstructed = SagaExecutionState.from_dict(original.to_dict())
        assert reconstructed.correlation_id == original.correlation_id
        assert reconstructed.status == SagaExecutionStatus.COMPLETED
        assert reconstructed.completed_at == original.completed_at
        assert reconstructed.headers == {"x-tenant": "acme"}
        assert "step-pay" in reconstructed.steps
        rs = reconstructed.steps["step-pay"]
        assert rs.step_id == "step-pay"
        assert rs.status == "DONE"
        assert rs.attempts == 2
        assert rs.latency_ms == 45.3
        assert rs.started_at == step_started
        assert rs.completed_at == step_completed
        assert rs.error_message is None
        assert rs.compensated is False

    def test_round_trip_with_error(self) -> None:
        started = datetime(2026, 1, 15, 10, 30, 0, tzinfo=UTC)
        original = SagaExecutionState(
            correlation_id="corr-err",
            saga_name="payment-saga",
            status=SagaExecutionStatus.FAILED,
            started_at=started,
            error_message="step-charge timed out",
        )
        reconstructed = SagaExecutionState.from_dict(original.to_dict())
        assert reconstructed.status == SagaExecutionStatus.FAILED
        assert reconstructed.error_message == "step-charge timed out"

    def test_from_dict_handles_missing_optional_fields(self) -> None:
        """from_dict should handle a dict missing optional keys gracefully."""
        data = {
            "correlation_id": "corr-missing",
            "saga_name": "test-saga",
            "status": "IN_FLIGHT",
            "started_at": "2026-01-15T10:30:00+00:00",
        }
        state = SagaExecutionState.from_dict(data)
        assert state.correlation_id == "corr-missing"
        assert state.saga_name == "test-saga"
        assert state.status == SagaExecutionStatus.IN_FLIGHT
        assert state.completed_at is None
        assert state.headers == {}
        assert state.steps == {}
        assert state.error_message is None

    def test_from_dict_step_missing_optional_fields(self) -> None:
        """from_dict should handle step dicts missing optional keys."""
        data = {
            "correlation_id": "corr-step-min",
            "saga_name": "test-saga",
            "status": "IN_FLIGHT",
            "started_at": "2026-01-15T10:30:00+00:00",
            "steps": {
                "step-1": {
                    "step_id": "step-1",
                    "status": "PENDING",
                },
            },
        }
        state = SagaExecutionState.from_dict(data)
        s = state.steps["step-1"]
        assert s.step_id == "step-1"
        assert s.status == "PENDING"
        assert s.attempts == 0
        assert s.latency_ms == 0.0
        assert s.started_at is None
        assert s.completed_at is None
        assert s.error_message is None
        assert s.compensated is False

    def test_from_dict_compensated_status(self) -> None:
        data = {
            "correlation_id": "corr-comp",
            "saga_name": "test-saga",
            "status": "COMPENSATED",
            "started_at": "2026-01-15T10:30:00+00:00",
            "completed_at": "2026-01-15T10:31:00+00:00",
        }
        state = SagaExecutionState.from_dict(data)
        assert state.status == SagaExecutionStatus.COMPENSATED
        assert state.completed_at is not None

    def test_from_dict_compensating_status(self) -> None:
        data = {
            "correlation_id": "corr-comping",
            "saga_name": "test-saga",
            "status": "COMPENSATING",
            "started_at": "2026-01-15T10:30:00+00:00",
        }
        state = SagaExecutionState.from_dict(data)
        assert state.status == SagaExecutionStatus.COMPENSATING

    def test_from_dict_preserves_frozen(self) -> None:
        data = {
            "correlation_id": "corr-frozen",
            "saga_name": "test-saga",
            "status": "IN_FLIGHT",
            "started_at": "2026-01-15T10:30:00+00:00",
        }
        state = SagaExecutionState.from_dict(data)
        with pytest.raises(AttributeError):
            state.status = SagaExecutionStatus.COMPLETED  # type: ignore[misc]
