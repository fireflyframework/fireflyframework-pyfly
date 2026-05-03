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
"""Shared types for the pyfly.transactional module."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class StepStatus(StrEnum):
    """Lifecycle status of a single transactional step."""

    PENDING = "PENDING"
    RUNNING = "RUNNING"
    DONE = "DONE"
    FAILED = "FAILED"
    COMPENSATED = "COMPENSATED"


class CompensationPolicy(StrEnum):
    """Strategy used to execute compensating transactions on failure."""

    STRICT_SEQUENTIAL = "STRICT_SEQUENTIAL"
    GROUPED_PARALLEL = "GROUPED_PARALLEL"
    RETRY_WITH_BACKOFF = "RETRY_WITH_BACKOFF"
    CIRCUIT_BREAKER = "CIRCUIT_BREAKER"
    BEST_EFFORT_PARALLEL = "BEST_EFFORT_PARALLEL"


@dataclass(frozen=True)
class BackpressureConfig:
    """Immutable configuration for backpressure and concurrency control."""

    strategy: str = "adaptive"
    concurrency: int = 10
    batch_size: int = 5
    failure_threshold: int = 50
    success_threshold: int = 2
    wait_duration_ms: int = 60000
