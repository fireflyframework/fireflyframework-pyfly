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
"""Immutable result types — StepOutcome and SagaResult."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from pyfly.transactional.shared.types import StepStatus


@dataclass(frozen=True)
class StepOutcome:
    """Immutable snapshot of how a single saga step completed.

    Fields
    ------
    status:
        Final lifecycle status of the step.
    attempts:
        Total number of execution attempts (including retries).
    latency_ms:
        Wall-clock duration of the last successful (or failing) attempt in
        milliseconds.
    result:
        Value returned by the step action, or ``None`` when the step failed
        without producing a result.
    error:
        The exception that caused failure, or ``None`` on success.
    compensated:
        Whether the compensating transaction was executed for this step.
    started_at:
        UTC timestamp when the first execution attempt began.
    compensation_result:
        Value returned by the compensating action, or ``None`` if no
        compensation was executed.
    compensation_error:
        Exception raised during compensation, or ``None`` on success / not
        compensated.
    """

    status: StepStatus
    attempts: int
    latency_ms: float
    result: Any
    error: Exception | None
    compensated: bool
    started_at: datetime
    compensation_result: Any | None
    compensation_error: Exception | None


@dataclass(frozen=True)
class SagaResult:
    """Immutable summary of a completed saga execution.

    Produced by the saga engine after all steps (and any compensations) have
    finished.  Consumers should treat this as a read-only value object.
    """

    saga_name: str
    correlation_id: str
    started_at: datetime
    completed_at: datetime
    success: bool
    error: Exception | None
    headers: dict[str, str]
    steps: dict[str, StepOutcome] = field(default_factory=dict)

    # ── query helpers ─────────────────────────────────────────

    def result_of(self, step_id: str) -> Any | None:
        """Return the result produced by *step_id*, or ``None`` if absent."""
        outcome = self.steps.get(step_id)
        if outcome is None:
            return None
        return outcome.result

    def failed_steps(self) -> dict[str, StepOutcome]:
        """Return a mapping of every step whose final status is ``FAILED``."""
        return {step_id: outcome for step_id, outcome in self.steps.items() if outcome.status == StepStatus.FAILED}

    def compensated_steps(self) -> dict[str, StepOutcome]:
        """Return a mapping of every step whose final status is ``COMPENSATED``."""
        return {step_id: outcome for step_id, outcome in self.steps.items() if outcome.status == StepStatus.COMPENSATED}
