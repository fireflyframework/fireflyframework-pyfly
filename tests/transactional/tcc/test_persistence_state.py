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
"""Tests for TCC persistence state types."""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime

import pytest

from pyfly.transactional.tcc.persistence.state import (
    ParticipantExecutionState,
    TccExecutionState,
    TccExecutionStatus,
)

# ---------------------------------------------------------------------------
# TccExecutionStatus
# ---------------------------------------------------------------------------


class TestTccExecutionStatus:
    """Tests for the TccExecutionStatus enum."""

    def test_members_count(self) -> None:
        assert len(TccExecutionStatus) == 4

    def test_in_flight_value(self) -> None:
        assert TccExecutionStatus.IN_FLIGHT == "IN_FLIGHT"
        assert TccExecutionStatus.IN_FLIGHT.value == "IN_FLIGHT"

    def test_confirmed_value(self) -> None:
        assert TccExecutionStatus.CONFIRMED == "CONFIRMED"
        assert TccExecutionStatus.CONFIRMED.value == "CONFIRMED"

    def test_cancelled_value(self) -> None:
        assert TccExecutionStatus.CANCELLED == "CANCELLED"
        assert TccExecutionStatus.CANCELLED.value == "CANCELLED"

    def test_failed_value(self) -> None:
        assert TccExecutionStatus.FAILED == "FAILED"
        assert TccExecutionStatus.FAILED.value == "FAILED"

    def test_is_str_subclass(self) -> None:
        assert isinstance(TccExecutionStatus.IN_FLIGHT, str)


# ---------------------------------------------------------------------------
# ParticipantExecutionState
# ---------------------------------------------------------------------------


class TestParticipantExecutionState:
    """Tests for the frozen ParticipantExecutionState dataclass."""

    def test_creation_with_defaults(self) -> None:
        state = ParticipantExecutionState(
            participant_id="payment",
            phase="TRY",
        )
        assert state.participant_id == "payment"
        assert state.phase == "TRY"
        assert state.try_result_key is None
        assert state.latency_ms == 0.0
        assert state.error_message is None

    def test_creation_with_all_fields(self) -> None:
        state = ParticipantExecutionState(
            participant_id="inventory",
            phase="CONFIRM",
            try_result_key="inventory-result",
            latency_ms=35.7,
            error_message="something went wrong",
        )
        assert state.participant_id == "inventory"
        assert state.phase == "CONFIRM"
        assert state.try_result_key == "inventory-result"
        assert state.latency_ms == 35.7
        assert state.error_message == "something went wrong"

    def test_frozen(self) -> None:
        state = ParticipantExecutionState(
            participant_id="payment",
            phase="TRY",
        )
        with pytest.raises(dataclasses.FrozenInstanceError):
            state.participant_id = "other"  # type: ignore[misc]

    def test_frozen_phase(self) -> None:
        state = ParticipantExecutionState(
            participant_id="payment",
            phase="TRY",
        )
        with pytest.raises(dataclasses.FrozenInstanceError):
            state.phase = "CONFIRM"  # type: ignore[misc]

    def test_is_dataclass(self) -> None:
        state = ParticipantExecutionState(
            participant_id="payment",
            phase="TRY",
        )
        assert dataclasses.is_dataclass(state)


# ---------------------------------------------------------------------------
# TccExecutionState
# ---------------------------------------------------------------------------


class TestTccExecutionState:
    """Tests for the frozen TccExecutionState dataclass."""

    @staticmethod
    def _make(**overrides) -> TccExecutionState:  # noqa: ANN003
        now = datetime.now(tz=UTC)
        defaults: dict = {
            "correlation_id": "corr-123",
            "tcc_name": "order-payment",
            "status": TccExecutionStatus.IN_FLIGHT,
            "started_at": now,
        }
        defaults.update(overrides)
        return TccExecutionState(**defaults)

    def test_creation_with_defaults(self) -> None:
        state = self._make()
        assert state.correlation_id == "corr-123"
        assert state.tcc_name == "order-payment"
        assert state.status == TccExecutionStatus.IN_FLIGHT
        assert state.started_at is not None
        assert state.completed_at is None
        assert state.participants == {}
        assert state.error_message is None
        assert state.failed_participant_id is None

    def test_creation_with_all_fields(self) -> None:
        now = datetime.now(tz=UTC)
        participant = ParticipantExecutionState(
            participant_id="payment",
            phase="CONFIRM",
            try_result_key="payment-result",
            latency_ms=42.0,
        )
        state = TccExecutionState(
            correlation_id="corr-456",
            tcc_name="booking",
            status=TccExecutionStatus.CONFIRMED,
            started_at=now,
            completed_at=now,
            participants={"payment": participant},
            error_message=None,
            failed_participant_id=None,
        )
        assert state.correlation_id == "corr-456"
        assert state.tcc_name == "booking"
        assert state.status == TccExecutionStatus.CONFIRMED
        assert state.completed_at == now
        assert len(state.participants) == 1
        assert state.participants["payment"] is participant

    def test_frozen(self) -> None:
        state = self._make()
        with pytest.raises(dataclasses.FrozenInstanceError):
            state.correlation_id = "other"  # type: ignore[misc]

    def test_frozen_status(self) -> None:
        state = self._make()
        with pytest.raises(dataclasses.FrozenInstanceError):
            state.status = TccExecutionStatus.CONFIRMED  # type: ignore[misc]

    def test_is_dataclass(self) -> None:
        state = self._make()
        assert dataclasses.is_dataclass(state)

    # -- to_dict / from_dict round-trip ------------------------------------

    def test_to_dict_minimal(self) -> None:
        now = datetime(2026, 1, 15, 12, 0, 0, tzinfo=UTC)
        state = TccExecutionState(
            correlation_id="corr-1",
            tcc_name="order-tcc",
            status=TccExecutionStatus.IN_FLIGHT,
            started_at=now,
        )
        d = state.to_dict()

        assert d["correlation_id"] == "corr-1"
        assert d["tcc_name"] == "order-tcc"
        assert d["status"] == "IN_FLIGHT"
        assert d["started_at"] == now.isoformat()
        assert d["completed_at"] is None
        assert d["participants"] == {}
        assert d["error_message"] is None
        assert d["failed_participant_id"] is None

    def test_to_dict_with_participants(self) -> None:
        now = datetime(2026, 2, 1, 10, 0, 0, tzinfo=UTC)
        participant = ParticipantExecutionState(
            participant_id="payment",
            phase="CONFIRM",
            try_result_key="payment-key",
            latency_ms=55.5,
            error_message=None,
        )
        state = TccExecutionState(
            correlation_id="corr-2",
            tcc_name="booking",
            status=TccExecutionStatus.CONFIRMED,
            started_at=now,
            completed_at=now,
            participants={"payment": participant},
        )
        d = state.to_dict()

        assert d["completed_at"] == now.isoformat()
        assert "payment" in d["participants"]
        p = d["participants"]["payment"]
        assert p["participant_id"] == "payment"
        assert p["phase"] == "CONFIRM"
        assert p["try_result_key"] == "payment-key"
        assert p["latency_ms"] == 55.5
        assert p["error_message"] is None

    def test_to_dict_with_error(self) -> None:
        now = datetime(2026, 3, 1, 8, 0, 0, tzinfo=UTC)
        state = TccExecutionState(
            correlation_id="corr-3",
            tcc_name="failing-tcc",
            status=TccExecutionStatus.FAILED,
            started_at=now,
            completed_at=now,
            error_message="participant timed out",
            failed_participant_id="inventory",
        )
        d = state.to_dict()

        assert d["status"] == "FAILED"
        assert d["error_message"] == "participant timed out"
        assert d["failed_participant_id"] == "inventory"

    def test_from_dict_minimal(self) -> None:
        now = datetime(2026, 1, 15, 12, 0, 0, tzinfo=UTC)
        data = {
            "correlation_id": "corr-1",
            "tcc_name": "order-tcc",
            "status": "IN_FLIGHT",
            "started_at": now.isoformat(),
            "completed_at": None,
            "participants": {},
            "error_message": None,
            "failed_participant_id": None,
        }
        state = TccExecutionState.from_dict(data)

        assert state.correlation_id == "corr-1"
        assert state.tcc_name == "order-tcc"
        assert state.status == TccExecutionStatus.IN_FLIGHT
        assert state.started_at == now
        assert state.completed_at is None
        assert state.participants == {}
        assert state.error_message is None
        assert state.failed_participant_id is None

    def test_from_dict_with_participants(self) -> None:
        now = datetime(2026, 2, 1, 10, 0, 0, tzinfo=UTC)
        data = {
            "correlation_id": "corr-2",
            "tcc_name": "booking",
            "status": "CONFIRMED",
            "started_at": now.isoformat(),
            "completed_at": now.isoformat(),
            "participants": {
                "payment": {
                    "participant_id": "payment",
                    "phase": "CONFIRM",
                    "try_result_key": "payment-key",
                    "latency_ms": 55.5,
                    "error_message": None,
                },
            },
            "error_message": None,
            "failed_participant_id": None,
        }
        state = TccExecutionState.from_dict(data)

        assert state.status == TccExecutionStatus.CONFIRMED
        assert state.completed_at == now
        assert len(state.participants) == 1
        p = state.participants["payment"]
        assert isinstance(p, ParticipantExecutionState)
        assert p.participant_id == "payment"
        assert p.phase == "CONFIRM"
        assert p.try_result_key == "payment-key"
        assert p.latency_ms == 55.5

    def test_round_trip(self) -> None:
        """to_dict -> from_dict should produce an equal object."""
        now = datetime(2026, 4, 10, 14, 30, 0, tzinfo=UTC)
        participant = ParticipantExecutionState(
            participant_id="shipping",
            phase="CANCEL",
            try_result_key="ship-key",
            latency_ms=100.0,
            error_message="network timeout",
        )
        original = TccExecutionState(
            correlation_id="corr-rt",
            tcc_name="round-trip-tcc",
            status=TccExecutionStatus.CANCELLED,
            started_at=now,
            completed_at=now,
            participants={"shipping": participant},
            error_message="cancelled due to failure",
            failed_participant_id="shipping",
        )
        d = original.to_dict()
        reconstructed = TccExecutionState.from_dict(d)

        assert reconstructed.correlation_id == original.correlation_id
        assert reconstructed.tcc_name == original.tcc_name
        assert reconstructed.status == original.status
        assert reconstructed.started_at == original.started_at
        assert reconstructed.completed_at == original.completed_at
        assert reconstructed.error_message == original.error_message
        assert reconstructed.failed_participant_id == original.failed_participant_id
        assert len(reconstructed.participants) == len(original.participants)

        rp = reconstructed.participants["shipping"]
        op = original.participants["shipping"]
        assert rp.participant_id == op.participant_id
        assert rp.phase == op.phase
        assert rp.try_result_key == op.try_result_key
        assert rp.latency_ms == op.latency_ms
        assert rp.error_message == op.error_message

    def test_round_trip_no_participants(self) -> None:
        """Round-trip with no participants and no completed_at."""
        now = datetime(2026, 5, 1, 9, 0, 0, tzinfo=UTC)
        original = TccExecutionState(
            correlation_id="corr-empty",
            tcc_name="empty-tcc",
            status=TccExecutionStatus.IN_FLIGHT,
            started_at=now,
        )
        d = original.to_dict()
        reconstructed = TccExecutionState.from_dict(d)

        assert reconstructed.correlation_id == original.correlation_id
        assert reconstructed.status == original.status
        assert reconstructed.started_at == original.started_at
        assert reconstructed.completed_at is None
        assert reconstructed.participants == {}
