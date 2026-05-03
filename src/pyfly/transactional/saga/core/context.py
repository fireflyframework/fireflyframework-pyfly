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
"""SagaContext — mutable runtime state carrier for a saga execution."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any

from pyfly.transactional.shared.types import StepStatus


@dataclass
class SagaContext:
    """Mutable bag of state threaded through every step of a saga execution.

    All dictionaries and collections are scoped to the running saga instance
    identified by *correlation_id*.  The context is intentionally mutable so
    that the saga engine can track status transitions, accumulate results, and
    record timing information as steps complete.
    """

    correlation_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    saga_name: str = ""
    headers: dict[str, str] = field(default_factory=dict)
    variables: dict[str, Any] = field(default_factory=dict)
    step_results: dict[str, Any] = field(default_factory=dict)
    step_statuses: dict[str, StepStatus] = field(default_factory=dict)
    step_attempts: dict[str, int] = field(default_factory=dict)
    step_latencies_ms: dict[str, float] = field(default_factory=dict)
    step_started_at: dict[str, Any] = field(default_factory=dict)
    compensation_results: dict[str, Any] = field(default_factory=dict)
    compensation_errors: dict[str, Exception] = field(default_factory=dict)
    idempotency_keys: set[str] = field(default_factory=set)
    topology_layers: list[list[str]] = field(default_factory=list)
    step_dependencies: dict[str, list[str]] = field(default_factory=dict)

    # ── result helpers ────────────────────────────────────────

    def get_result(self, step_id: str) -> Any | None:
        """Return the result stored for *step_id*, or ``None`` if absent."""
        return self.step_results.get(step_id)

    def set_result(self, step_id: str, result: Any) -> None:
        """Store *result* for *step_id*, overwriting any previous value."""
        self.step_results[step_id] = result

    # ── variable helpers ──────────────────────────────────────

    def get_variable(self, key: str) -> Any | None:
        """Return the saga-level variable *key*, or ``None`` if absent."""
        return self.variables.get(key)

    def set_variable(self, key: str, value: Any) -> None:
        """Store *value* under saga-level variable *key*."""
        self.variables[key] = value

    # ── status helper ─────────────────────────────────────────

    def set_step_status(self, step_id: str, status: StepStatus) -> None:
        """Update the lifecycle status of *step_id*."""
        self.step_statuses[step_id] = status

    # ── idempotency helpers ───────────────────────────────────

    def has_idempotency_key(self, key: str) -> bool:
        """Return ``True`` if *key* has already been registered."""
        return key in self.idempotency_keys

    def add_idempotency_key(self, key: str) -> None:
        """Register *key* as seen; safe to call multiple times."""
        self.idempotency_keys.add(key)
