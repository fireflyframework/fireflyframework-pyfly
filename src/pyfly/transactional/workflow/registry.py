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
"""Workflow registry — discovery and indexing of ``@workflow`` beans."""

from __future__ import annotations

from typing import Any

from pyfly.transactional.core.exceptions import OrchestrationValidationError
from pyfly.transactional.core.topology import TopologyBuilder, TopologyError
from pyfly.transactional.workflow.definition import (
    WorkflowDefinition,
    WorkflowStepDefinition,
)


class WorkflowRegistry:
    """Discover ``@workflow``-decorated beans and turn them into definitions."""

    def __init__(self) -> None:
        self._definitions: dict[str, WorkflowDefinition] = {}

    def register_from_bean(self, bean: Any) -> WorkflowDefinition:
        cls = type(bean)
        meta = getattr(cls, "__pyfly_workflow__", None)
        if meta is None:
            msg = f"{cls.__qualname__} is not decorated with @workflow"
            raise OrchestrationValidationError(msg)

        definition = WorkflowDefinition(
            id=meta.id,
            name=meta.name,
            bean=bean,
            description=meta.description,
            version=meta.version,
            trigger_mode=meta.trigger_mode,
            trigger_event_type=meta.trigger_event_type,
            timeout_ms=meta.timeout_ms,
            publish_events=meta.publish_events,
            layer_concurrency=meta.layer_concurrency,
        )

        # Discover all decorated methods on the class.
        compensation_methods: dict[str, Any] = {}
        for attr_name in dir(cls):
            attr = getattr(cls, attr_name, None)
            if attr is None:
                continue
            step_meta = getattr(attr, "__pyfly_workflow_step__", None)
            if step_meta is not None:
                step = WorkflowStepDefinition(
                    id=step_meta.id,
                    method=attr,
                    depends_on=list(step_meta.depends_on),
                    timeout_ms=step_meta.timeout_ms,
                    max_retries=step_meta.max_retries,
                    retry_delay_ms=step_meta.retry_delay_ms,
                    output_event_type=step_meta.output_event_type,
                    condition=step_meta.condition,
                    async_=step_meta.async_,
                    compensatable=step_meta.compensatable,
                    compensation_method_name=step_meta.compensation_method or None,
                )
                # Pull signal/timer/child metadata from same method.
                wait_signal = getattr(attr, "__pyfly_workflow_wait_signal__", None)
                if wait_signal is not None:
                    step.wait_for_signal = wait_signal.name
                    step.wait_for_signal_timeout_ms = wait_signal.timeout_ms
                wait_timer = getattr(attr, "__pyfly_workflow_wait_timer__", None)
                if wait_timer is not None:
                    step.wait_for_timer_ms = wait_timer.delay_ms
                wait_all = getattr(attr, "__pyfly_workflow_wait_all__", None)
                if wait_all is not None:
                    step.wait_for_all = wait_all.signals
                wait_any = getattr(attr, "__pyfly_workflow_wait_any__", None)
                if wait_any is not None:
                    step.wait_for_any = wait_any.signals
                child = getattr(attr, "__pyfly_workflow_child__", None)
                if child is not None:
                    step.child_workflow_id = child.workflow_id
                    step.child_wait_for_completion = child.wait_for_completion
                    step.child_timeout_ms = child.timeout_ms
                definition.steps[step_meta.id] = step

            comp_meta = getattr(attr, "__pyfly_workflow_compensation__", None)
            if comp_meta is not None:
                compensation_methods[comp_meta.for_step] = attr

            on_complete = getattr(attr, "__pyfly_workflow_on_complete__", None)
            if on_complete is not None:
                definition.on_complete = attr
            on_error = getattr(attr, "__pyfly_workflow_on_error__", None)
            if on_error is not None:
                definition.on_error = attr
            on_step = getattr(attr, "__pyfly_workflow_on_step__", None)
            if on_step is not None:
                definition.on_step_callbacks[on_step.step_id] = attr
            query = getattr(attr, "__pyfly_workflow_query__", None)
            if query is not None:
                definition.queries[query.name] = attr

        # Wire compensation methods into step definitions.
        for step_id, step in definition.steps.items():
            if step.compensation_method_name and step.compensation_method_name in compensation_methods:
                step.compensation_method = compensation_methods[step.compensation_method_name]
            elif step_id in compensation_methods:
                step.compensation_method = compensation_methods[step_id]

        # Validate DAG.
        try:
            TopologyBuilder.build_layers(definition.graph())
        except TopologyError as exc:
            raise OrchestrationValidationError(f"workflow '{definition.id}': {exc}") from exc

        self._definitions[definition.id] = definition
        return definition

    def get(self, workflow_id: str) -> WorkflowDefinition | None:
        return self._definitions.get(workflow_id)

    def list(self) -> list[WorkflowDefinition]:
        return list(self._definitions.values())

    def remove(self, workflow_id: str) -> bool:
        return self._definitions.pop(workflow_id, None) is not None
