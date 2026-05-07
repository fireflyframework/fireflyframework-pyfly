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
"""Public WorkflowResult value type returned by the engine entry point."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from pyfly.transactional.core.model import ExecutionStatus


@dataclass
class WorkflowResult:
    """Outcome of a workflow execution."""

    workflow_id: str
    correlation_id: str
    status: ExecutionStatus
    duration_ms: float
    step_results: dict[str, Any] = field(default_factory=dict)
    variables: dict[str, Any] = field(default_factory=dict)
    error: str | None = None

    @property
    def successful(self) -> bool:
        return self.status in {ExecutionStatus.COMPLETED, ExecutionStatus.CONFIRMED}
