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
"""Saga persistence state types — SagaExecutionStatus, StepExecutionState, SagaExecutionState."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any


class SagaExecutionStatus(StrEnum):
    """Status of a saga execution from the persistence perspective."""

    IN_FLIGHT = "IN_FLIGHT"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    COMPENSATING = "COMPENSATING"
    COMPENSATED = "COMPENSATED"


@dataclass(frozen=True)
class StepExecutionState:
    """Serializable state for a single saga step.

    Fields
    ------
    step_id:
        Unique identifier of the step within its saga definition.
    status:
        Current lifecycle status as a string (mirrors ``StepStatus`` values).
    attempts:
        Number of execution attempts (including retries).
    latency_ms:
        Wall-clock duration of the last attempt in milliseconds.
    started_at:
        UTC timestamp when the first execution attempt began, or ``None``
        if the step has not started.
    completed_at:
        UTC timestamp when the step finished, or ``None`` if still running
        or not yet started.
    error_message:
        Human-readable error description, or ``None`` on success.
    compensated:
        Whether the compensating transaction was executed for this step.
    """

    step_id: str
    status: str
    attempts: int = 0
    latency_ms: float = 0.0
    started_at: datetime | None = None
    completed_at: datetime | None = None
    error_message: str | None = None
    compensated: bool = False


@dataclass(frozen=True)
class SagaExecutionState:
    """Serializable state for a complete saga execution.

    Used by persistence adapters to store and restore saga state across
    process restarts or for audit/reporting purposes.

    Fields
    ------
    correlation_id:
        Unique identifier for this saga execution instance.
    saga_name:
        Logical name of the saga definition being executed.
    status:
        Current execution status from the persistence perspective.
    started_at:
        UTC timestamp when the saga execution began.
    completed_at:
        UTC timestamp when the saga finished, or ``None`` if still running.
    headers:
        Propagated headers (tenant, trace-id, etc.).
    steps:
        Mapping of step ID to its serializable execution state.
    error_message:
        Human-readable error description, or ``None`` on success.
    """

    correlation_id: str
    saga_name: str
    status: SagaExecutionStatus
    started_at: datetime
    completed_at: datetime | None = None
    headers: dict[str, str] = field(default_factory=dict)
    steps: dict[str, StepExecutionState] = field(default_factory=dict)
    error_message: str | None = None

    # ── serialisation ─────────────────────────────────────────

    def to_dict(self) -> dict[str, Any]:
        """Convert to a dict suitable for persistence.

        All datetime values are serialised to ISO-8601 strings and the
        execution status is stored as its string value so the result is
        directly JSON-serialisable.
        """
        return {
            "correlation_id": self.correlation_id,
            "saga_name": self.saga_name,
            "status": self.status.value,
            "started_at": self.started_at.isoformat(),
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
            "headers": dict(self.headers),
            "steps": {
                sid: {
                    "step_id": s.step_id,
                    "status": s.status,
                    "attempts": s.attempts,
                    "latency_ms": s.latency_ms,
                    "started_at": s.started_at.isoformat() if s.started_at else None,
                    "completed_at": s.completed_at.isoformat() if s.completed_at else None,
                    "error_message": s.error_message,
                    "compensated": s.compensated,
                }
                for sid, s in self.steps.items()
            },
            "error_message": self.error_message,
        }

    # ── deserialisation ───────────────────────────────────────

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SagaExecutionState:
        """Reconstruct from a persistence dict.

        Handles missing optional fields gracefully, applying the same
        defaults used by the dataclass constructor.
        """
        steps: dict[str, StepExecutionState] = {}
        for sid, sd in data.get("steps", {}).items():
            started_at_raw = sd.get("started_at")
            completed_at_raw = sd.get("completed_at")
            steps[sid] = StepExecutionState(
                step_id=sd["step_id"],
                status=sd["status"],
                attempts=sd.get("attempts", 0),
                latency_ms=sd.get("latency_ms", 0.0),
                started_at=datetime.fromisoformat(started_at_raw) if started_at_raw else None,
                completed_at=datetime.fromisoformat(completed_at_raw) if completed_at_raw else None,
                error_message=sd.get("error_message"),
                compensated=sd.get("compensated", False),
            )

        completed_at_raw = data.get("completed_at")
        return cls(
            correlation_id=data["correlation_id"],
            saga_name=data["saga_name"],
            status=SagaExecutionStatus(data["status"]),
            started_at=datetime.fromisoformat(data["started_at"]),
            completed_at=datetime.fromisoformat(completed_at_raw) if completed_at_raw else None,
            headers=data.get("headers", {}),
            steps=steps,
            error_message=data.get("error_message"),
        )
