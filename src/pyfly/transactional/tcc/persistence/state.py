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
"""Frozen dataclasses representing the serializable persistence state of a TCC execution.

These types mirror the saga persistence state pattern and are designed
to be round-trippable through ``to_dict`` / ``from_dict`` for storage
in any key-value or document store.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any


class TccExecutionStatus(StrEnum):
    """Status of a TCC execution from the persistence perspective.

    * **IN_FLIGHT** — the TCC is still running (try phase in progress).
    * **CONFIRMED** — all participants have been confirmed successfully.
    * **CANCELLED** — the TCC was cancelled (compensation path).
    * **FAILED** — an unrecoverable error prevented completion.
    """

    IN_FLIGHT = "IN_FLIGHT"
    CONFIRMED = "CONFIRMED"
    CANCELLED = "CANCELLED"
    FAILED = "FAILED"


@dataclass(frozen=True)
class ParticipantExecutionState:
    """Serializable state for a single TCC participant.

    Captures the phase the participant reached, an optional key for
    retrieving its try-phase result from an external store, its
    execution latency, and any error message.
    """

    participant_id: str
    phase: str  # TccPhase value
    try_result_key: str | None = None
    latency_ms: float = 0.0
    error_message: str | None = None


@dataclass(frozen=True)
class TccExecutionState:
    """Serializable state for a complete TCC execution.

    Designed to capture the full snapshot of a TCC run so that it can
    be persisted to a document or key-value store and later
    reconstructed via :meth:`from_dict`.
    """

    correlation_id: str
    tcc_name: str
    status: TccExecutionStatus
    started_at: datetime
    completed_at: datetime | None = None
    participants: dict[str, ParticipantExecutionState] = field(default_factory=dict)
    error_message: str | None = None
    failed_participant_id: str | None = None

    # ── serialisation helpers ─────────────────────────────────

    def to_dict(self) -> dict[str, Any]:
        """Convert to a dict suitable for persistence.

        Datetime values are serialised to ISO-8601 strings and
        participant states are converted to plain dicts.
        """
        return {
            "correlation_id": self.correlation_id,
            "tcc_name": self.tcc_name,
            "status": self.status.value,
            "started_at": self.started_at.isoformat(),
            "completed_at": self.completed_at.isoformat() if self.completed_at is not None else None,
            "participants": {
                pid: {
                    "participant_id": p.participant_id,
                    "phase": p.phase,
                    "try_result_key": p.try_result_key,
                    "latency_ms": p.latency_ms,
                    "error_message": p.error_message,
                }
                for pid, p in self.participants.items()
            },
            "error_message": self.error_message,
            "failed_participant_id": self.failed_participant_id,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TccExecutionState:
        """Reconstruct from a persistence dict.

        Expects the same structure produced by :meth:`to_dict`.
        ISO-8601 datetime strings are parsed back to ``datetime``
        objects and participant dicts are hydrated into
        :class:`ParticipantExecutionState` instances.
        """
        completed_at_raw = data.get("completed_at")
        completed_at = datetime.fromisoformat(completed_at_raw) if completed_at_raw is not None else None

        participants: dict[str, ParticipantExecutionState] = {}
        for pid, pdata in data.get("participants", {}).items():
            participants[pid] = ParticipantExecutionState(
                participant_id=pdata["participant_id"],
                phase=pdata["phase"],
                try_result_key=pdata.get("try_result_key"),
                latency_ms=pdata.get("latency_ms", 0.0),
                error_message=pdata.get("error_message"),
            )

        return cls(
            correlation_id=data["correlation_id"],
            tcc_name=data["tcc_name"],
            status=TccExecutionStatus(data["status"]),
            started_at=datetime.fromisoformat(data["started_at"]),
            completed_at=completed_at,
            participants=participants,
            error_message=data.get("error_message"),
            failed_participant_id=data.get("failed_participant_id"),
        )
