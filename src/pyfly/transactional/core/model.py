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
"""Core orchestration model — enums and immutable value types shared across patterns.

Mirrors ``org.fireflyframework.orchestration.core.model`` from the Java engine
so that saga, workflow and TCC executions speak the same vocabulary.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class ExecutionStatus(StrEnum):
    """Lifecycle status of an orchestration execution.

    Covers all three patterns (saga, workflow, TCC).  Pattern-specific
    transitions (TCC's ``TRYING``/``CONFIRMING``/``CANCELING``) coexist with
    universal ones (``RUNNING``, ``COMPLETED``, ``FAILED``).
    """

    PENDING = "PENDING"
    RUNNING = "RUNNING"
    WAITING = "WAITING"
    SUSPENDED = "SUSPENDED"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    TIMED_OUT = "TIMED_OUT"
    TRYING = "TRYING"
    CONFIRMING = "CONFIRMING"
    CONFIRMED = "CONFIRMED"
    CANCELING = "CANCELING"
    CANCELED = "CANCELED"
    COMPENSATING = "COMPENSATING"
    COMPENSATED = "COMPENSATED"

    @property
    def is_terminal(self) -> bool:
        """``True`` when the execution can no longer change state."""
        return self in {
            ExecutionStatus.COMPLETED,
            ExecutionStatus.FAILED,
            ExecutionStatus.CANCELLED,
            ExecutionStatus.TIMED_OUT,
            ExecutionStatus.CONFIRMED,
            ExecutionStatus.CANCELED,
            ExecutionStatus.COMPENSATED,
        }


class ExecutionPattern(StrEnum):
    """Which orchestration pattern produced an execution."""

    SAGA = "SAGA"
    WORKFLOW = "WORKFLOW"
    TCC = "TCC"


class TriggerMode(StrEnum):
    """How the caller wants to interact with the execution."""

    SYNC = "SYNC"
    ASYNC = "ASYNC"


class StepStatus(StrEnum):
    """Lifecycle status of a single step within an execution."""

    PENDING = "PENDING"
    RUNNING = "RUNNING"
    DONE = "DONE"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"
    COMPENSATING = "COMPENSATING"
    COMPENSATED = "COMPENSATED"
    COMPENSATION_FAILED = "COMPENSATION_FAILED"


class CompensationPolicy(StrEnum):
    """Strategy for executing compensating actions when a saga fails."""

    STRICT_SEQUENTIAL = "STRICT_SEQUENTIAL"
    GROUPED_PARALLEL = "GROUPED_PARALLEL"
    RETRY_WITH_BACKOFF = "RETRY_WITH_BACKOFF"
    CIRCUIT_BREAKER = "CIRCUIT_BREAKER"
    BEST_EFFORT_PARALLEL = "BEST_EFFORT_PARALLEL"


class TccPhase(StrEnum):
    """One of the three TCC execution phases."""

    TRY = "TRY"
    CONFIRM = "CONFIRM"
    CANCEL = "CANCEL"


@dataclass(frozen=True)
class RetryPolicy:
    """Immutable retry configuration for a single step or participant.

    Attributes:
        max_attempts: Total attempts including the first call.  ``1`` disables
            retry.
        backoff_ms: Base backoff between attempts.
        timeout_ms: Per-attempt timeout (``0`` disables).
        jitter: Whether to apply random jitter to the backoff.
        jitter_factor: Fraction of ``backoff_ms`` used as jitter range
            (``0.0–1.0``).
    """

    max_attempts: int = 1
    backoff_ms: int = 0
    timeout_ms: int = 0
    jitter: bool = False
    jitter_factor: float = 0.0


@dataclass
class CircuitBreakerConfig:
    """Backpressure circuit-breaker thresholds."""

    failure_threshold: int = 5
    recovery_timeout_ms: int = 30_000
    half_open_max_calls: int = 3


@dataclass
class BackpressureProperties:
    """Backpressure configuration block (mirrors Java ``BackpressureProperties``)."""

    strategy: str = "adaptive"
    batch_size: int = 10
    circuit_breaker: CircuitBreakerConfig = field(default_factory=CircuitBreakerConfig)
