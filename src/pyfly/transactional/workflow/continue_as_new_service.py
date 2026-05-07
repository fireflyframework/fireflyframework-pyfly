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
"""Continue-as-new: restart a workflow with new input but a fresh correlation id."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pyfly.transactional.workflow.engine import WorkflowEngine


class ContinueAsNewService:
    """Helper that workflows call to perform a ``continue-as-new`` restart."""

    def __init__(self) -> None:
        self._engine: WorkflowEngine | None = None

    def bind(self, engine: WorkflowEngine) -> None:
        self._engine = engine

    async def restart(self, workflow_id: str, input: Any) -> str:
        if self._engine is None:
            msg = "ContinueAsNewService is not bound to a WorkflowEngine"
            raise RuntimeError(msg)
        result = await self._engine.start(workflow_id, input)
        return result.correlation_id
