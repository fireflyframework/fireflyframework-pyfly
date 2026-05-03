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
"""Immutable result types — ParticipantResult and TccResult."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from pyfly.transactional.tcc.core.phase import TccPhase


@dataclass(frozen=True)
class ParticipantResult:
    """Immutable snapshot of how a single TCC participant completed.

    Fields
    ------
    participant_id:
        Unique identifier of the participant within the TCC.
    try_result:
        Value returned by the try phase, or ``None`` when the try failed
        without producing a result.
    try_error:
        The exception raised during the try phase, or ``None`` on success.
    confirm_error:
        The exception raised during the confirm phase, or ``None`` on success.
    cancel_error:
        The exception raised during the cancel phase, or ``None`` on success /
        not cancelled.
    final_phase:
        The last phase this participant reached.
    latency_ms:
        Wall-clock duration of the participant's execution in milliseconds.
    """

    participant_id: str
    try_result: Any
    try_error: Exception | None
    confirm_error: Exception | None
    cancel_error: Exception | None
    final_phase: TccPhase
    latency_ms: float


@dataclass(frozen=True)
class TccResult:
    """Immutable summary of a completed TCC execution.

    Produced by the TCC engine after all participants (and any cancellations)
    have finished.  Consumers should treat this as a read-only value object.
    """

    correlation_id: str
    tcc_name: str
    success: bool
    final_phase: TccPhase
    try_results: dict[str, Any]
    participant_results: dict[str, ParticipantResult]
    started_at: datetime
    completed_at: datetime
    error: Exception | None
    failed_participant_id: str | None

    # ── query helpers ─────────────────────────────────────────

    def result_of(self, participant_id: str) -> Any | None:
        """Return the try-result produced by *participant_id*, or ``None`` if absent."""
        pr = self.participant_results.get(participant_id)
        return pr.try_result if pr else None

    def failed_participants(self) -> dict[str, ParticipantResult]:
        """Return a mapping of every participant that encountered an error."""
        return {
            pid: pr
            for pid, pr in self.participant_results.items()
            if pr.try_error or pr.confirm_error or pr.cancel_error
        }
