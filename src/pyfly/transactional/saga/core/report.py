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
"""FailureReport — immutable diagnostic record produced when a saga fails."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class FailureReport:
    """Immutable record summarising a saga failure and its compensation state.

    Fields
    ------
    saga_name:
        Logical name of the saga that failed.
    correlation_id:
        Unique identifier for the saga execution instance.
    failed_step_id:
        Identifier of the step that triggered the saga failure.
    error:
        The exception raised by the failing step.
    completed_steps:
        Ordered list of step IDs that completed successfully before failure.
    compensated_steps:
        List of step IDs for which compensation was executed successfully.
    compensation_errors:
        Mapping from step ID to the exception raised during its compensation,
        for steps whose compensation itself failed.
    """

    saga_name: str
    correlation_id: str
    failed_step_id: str
    error: Exception
    completed_steps: list[str] = field(default_factory=list)
    compensated_steps: list[str] = field(default_factory=list)
    compensation_errors: dict[str, Exception] = field(default_factory=dict)
