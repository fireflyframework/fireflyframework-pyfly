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
"""Step definition — immutable metadata for a single saga step."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class StepDefinition:
    """Immutable descriptor holding all metadata for one saga step.

    Populated during saga registration by inspecting ``__pyfly_saga_step__``
    metadata on decorated methods.

    Attributes:
        id: Unique step identifier within the saga.
        step_method: Bound method implementing the forward action, or ``None``
            for external steps resolved later.
        compensate_name: Name of the compensation method on the saga bean,
            or ``None`` if the step has no compensation.
        compensate_method: Resolved bound compensation method, or ``None``.
        depends_on: Step ids that must complete before this step can execute.
        retry: Number of retry attempts for the forward action.
        backoff_ms: Base backoff duration in milliseconds between retries.
        timeout_ms: Execution timeout in milliseconds (0 = no timeout).
        jitter: Whether to add jitter to the backoff duration.
        jitter_factor: Fraction of backoff used as jitter range.
        cpu_bound: Whether to offload execution to a thread/process pool.
        idempotency_key: Template string for deduplication, or ``None``.
        compensation_retry: Override retry count for the compensation action.
        compensation_backoff_ms: Override backoff for the compensation action.
        compensation_timeout_ms: Override timeout for the compensation action.
        compensation_critical: If ``True``, saga failure is raised when
            compensation itself fails.
    """

    id: str
    step_method: Callable[..., Any] | None = None
    compensate_name: str | None = None
    compensate_method: Callable[..., Any] | None = None
    depends_on: list[str] = field(default_factory=list)
    retry: int = 0
    backoff_ms: int = 0
    timeout_ms: int = 0
    jitter: bool = False
    jitter_factor: float = 0.0
    cpu_bound: bool = False
    idempotency_key: str | None = None
    compensation_retry: int | None = None
    compensation_backoff_ms: int | None = None
    compensation_timeout_ms: int | None = None
    compensation_critical: bool = False
