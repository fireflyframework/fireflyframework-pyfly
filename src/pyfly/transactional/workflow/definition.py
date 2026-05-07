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
"""WorkflowDefinition / WorkflowStepDefinition — registry data structures."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from pyfly.transactional.core.model import RetryPolicy, TriggerMode


@dataclass
class WorkflowStepDefinition:
    """Resolved metadata for a single workflow step method."""

    id: str
    method: Callable[..., Any]
    depends_on: list[str] = field(default_factory=list)
    timeout_ms: int = 0
    max_retries: int = 0
    retry_delay_ms: int = 0
    output_event_type: str = ""
    condition: str = ""
    async_: bool = False
    compensatable: bool = False
    compensation_method_name: str | None = None
    compensation_method: Callable[..., Any] | None = None
    wait_for_signal: str | None = None
    wait_for_signal_timeout_ms: int = 0
    wait_for_timer_ms: int = 0
    wait_for_all: tuple[str, ...] = ()
    wait_for_any: tuple[str, ...] = ()
    child_workflow_id: str | None = None
    child_wait_for_completion: bool = True
    child_timeout_ms: int = 0

    def to_retry_policy(self) -> RetryPolicy:
        return RetryPolicy(
            max_attempts=max(1, self.max_retries + 1),
            backoff_ms=self.retry_delay_ms,
            timeout_ms=self.timeout_ms,
        )


@dataclass
class WorkflowDefinition:
    """All metadata needed to execute a workflow."""

    id: str
    name: str
    bean: Any
    description: str = ""
    version: int = 1
    trigger_mode: TriggerMode = TriggerMode.SYNC
    trigger_event_type: str = ""
    timeout_ms: int = 0
    publish_events: bool = True
    layer_concurrency: int = 0
    steps: dict[str, WorkflowStepDefinition] = field(default_factory=dict)
    on_complete: Callable[..., Any] | None = None
    on_error: Callable[..., Any] | None = None
    on_step_callbacks: dict[str, Callable[..., Any]] = field(default_factory=dict)
    queries: dict[str, Callable[..., Any]] = field(default_factory=dict)

    def graph(self) -> dict[str, list[str]]:
        return {sid: list(s.depends_on) for sid, s in self.steps.items()}
