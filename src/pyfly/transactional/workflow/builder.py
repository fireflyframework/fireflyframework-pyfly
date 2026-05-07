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
"""Fluent programmatic builder for workflows (no decorators required)."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from pyfly.transactional.workflow.definition import (
    WorkflowDefinition,
    WorkflowStepDefinition,
)


class WorkflowBuilder:
    """Programmatic alternative to the ``@workflow`` decorator."""

    def __init__(self, workflow_id: str, *, name: str | None = None) -> None:
        self._definition = WorkflowDefinition(id=workflow_id, name=name or workflow_id, bean=None)

    def description(self, value: str) -> WorkflowBuilder:
        self._definition.description = value
        return self

    def step(
        self,
        step_id: str,
        handler: Callable[..., Awaitable[Any] | Any],
        *,
        depends_on: list[str] | None = None,
        timeout_ms: int = 0,
        max_retries: int = 0,
        retry_delay_ms: int = 0,
        compensation: Callable[..., Any] | None = None,
        compensatable: bool = False,
    ) -> WorkflowBuilder:
        step = WorkflowStepDefinition(
            id=step_id,
            method=handler,
            depends_on=depends_on or [],
            timeout_ms=timeout_ms,
            max_retries=max_retries,
            retry_delay_ms=retry_delay_ms,
            compensatable=compensatable,
            compensation_method=compensation,
        )
        self._definition.steps[step_id] = step
        return self

    def wait_signal(
        self,
        step_id: str,
        signal: str,
        *,
        depends_on: list[str] | None = None,
        timeout_ms: int = 0,
    ) -> WorkflowBuilder:
        async def _noop_handler(*_: Any, **__: Any) -> None:
            return None

        step = WorkflowStepDefinition(
            id=step_id,
            method=_noop_handler,
            depends_on=depends_on or [],
            wait_for_signal=signal,
            wait_for_signal_timeout_ms=timeout_ms,
        )
        self._definition.steps[step_id] = step
        return self

    def wait_timer(
        self,
        step_id: str,
        delay_ms: int,
        *,
        depends_on: list[str] | None = None,
    ) -> WorkflowBuilder:
        async def _noop_handler(*_: Any, **__: Any) -> None:
            return None

        step = WorkflowStepDefinition(
            id=step_id,
            method=_noop_handler,
            depends_on=depends_on or [],
            wait_for_timer_ms=delay_ms,
        )
        self._definition.steps[step_id] = step
        return self

    def child(
        self,
        step_id: str,
        child_workflow_id: str,
        *,
        depends_on: list[str] | None = None,
        wait_for_completion: bool = True,
        timeout_ms: int = 0,
    ) -> WorkflowBuilder:
        async def _noop_handler(*_: Any, **__: Any) -> None:
            return None

        step = WorkflowStepDefinition(
            id=step_id,
            method=_noop_handler,
            depends_on=depends_on or [],
            child_workflow_id=child_workflow_id,
            child_wait_for_completion=wait_for_completion,
            child_timeout_ms=timeout_ms,
        )
        self._definition.steps[step_id] = step
        return self

    def build(self) -> WorkflowDefinition:
        return self._definition
