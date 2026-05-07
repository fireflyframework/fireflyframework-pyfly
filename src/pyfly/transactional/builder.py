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
"""Top-level orchestration builder — pick a pattern, get a fluent builder."""

from __future__ import annotations

from pyfly.transactional.workflow.builder import WorkflowBuilder


class OrchestrationBuilder:
    """``OrchestrationBuilder.workflow("foo")`` ➜ :class:`WorkflowBuilder`.

    A saga and TCC builder follow the same shape (see
    :class:`pyfly.transactional.saga.registry.saga_builder.SagaBuilder`).
    """

    @staticmethod
    def workflow(workflow_id: str, *, name: str | None = None) -> WorkflowBuilder:
        return WorkflowBuilder(workflow_id, name=name)
