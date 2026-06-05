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
"""TccContext — mutable runtime state carrier for a TCC execution."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any

from pyfly.transactional.tcc.core.phase import TccPhase


@dataclass
class TccContext:
    """Mutable bag of state threaded through every participant of a TCC execution.

    All dictionaries and collections are scoped to the running TCC instance
    identified by *correlation_id*.  The context is intentionally mutable so
    that the TCC engine can track phase transitions, accumulate try results,
    and record participant statuses as phases complete.
    """

    correlation_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    tcc_name: str = ""
    headers: dict[str, str] = field(default_factory=dict)
    variables: dict[str, Any] = field(default_factory=dict)
    try_results: dict[str, Any] = field(default_factory=dict)
    current_phase: TccPhase = TccPhase.TRY
    participant_statuses: dict[str, TccPhase] = field(default_factory=dict)
    # Per-participant phase errors and accumulated latency, so TccResult can
    # report which participant failed and how long each phase took (audit #57).
    participant_errors: dict[str, dict[TccPhase, Exception]] = field(default_factory=dict)
    participant_latency: dict[str, float] = field(default_factory=dict)

    # ── try-result helpers ────────────────────────────────────

    def get_try_result(self, participant_id: str) -> Any | None:
        """Return the try-phase result for *participant_id*, or ``None`` if absent."""
        return self.try_results.get(participant_id)

    def set_try_result(self, participant_id: str, result: Any) -> None:
        """Store *result* for *participant_id*, overwriting any previous value."""
        self.try_results[participant_id] = result

    # ── phase helpers ─────────────────────────────────────────

    def set_phase(self, phase: TccPhase) -> None:
        """Transition the overall TCC execution to *phase*."""
        self.current_phase = phase

    # ── participant-status helpers ────────────────────────────

    def set_participant_status(self, participant_id: str, phase: TccPhase) -> None:
        """Record the phase that *participant_id* has reached."""
        self.participant_statuses[participant_id] = phase

    # ── error / latency helpers ───────────────────────────────

    def record_participant_error(self, participant_id: str, phase: TccPhase, error: Exception) -> None:
        """Record the exception *participant_id* raised during *phase*."""
        self.participant_errors.setdefault(participant_id, {})[phase] = error

    def add_participant_latency(self, participant_id: str, latency_ms: float) -> None:
        """Accumulate elapsed time spent invoking *participant_id*'s phases."""
        self.participant_latency[participant_id] = self.participant_latency.get(participant_id, 0.0) + latency_ms

    def get_participant_error(self, participant_id: str, phase: TccPhase) -> Exception | None:
        """Return the exception *participant_id* raised during *phase*, if any."""
        return self.participant_errors.get(participant_id, {}).get(phase)
