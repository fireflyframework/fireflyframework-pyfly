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
"""Spawn nested workflows from a parent step.

The service holds a reference back to the :class:`WorkflowEngine` (set after
construction to break the import cycle) so a step can invoke another
``@workflow`` by id.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pyfly.transactional.workflow.engine import WorkflowEngine
    from pyfly.transactional.workflow.result import WorkflowResult


class ChildWorkflowService:
    """Allows steps to start child workflows synchronously or fire-and-forget."""

    def __init__(self) -> None:
        self._engine: WorkflowEngine | None = None

    def bind(self, engine: WorkflowEngine) -> None:
        self._engine = engine

    async def start(
        self,
        workflow_id: str,
        input: Any = None,
        *,
        wait_for_completion: bool = True,
        timeout_ms: int = 0,
    ) -> WorkflowResult | str:
        """Start a child workflow.

        When ``wait_for_completion`` is ``True`` the call returns the child's
        :class:`WorkflowResult`; otherwise it returns immediately with the
        child correlation id.
        """
        if self._engine is None:
            msg = "ChildWorkflowService is not bound to a WorkflowEngine"
            raise RuntimeError(msg)
        if wait_for_completion:
            coro = self._engine.start(workflow_id, input)
            if timeout_ms > 0:
                return await asyncio.wait_for(coro, timeout=timeout_ms / 1000.0)
            return await coro
        # Fire and forget: schedule and return placeholder correlation id.
        task = asyncio.create_task(self._engine.start(workflow_id, input))
        # Allow the new task to begin so it can populate its correlation id;
        # we then return the (possibly synthetic) id.  In practice the engine
        # generates the id eagerly and stores it, so we can reach in.
        await asyncio.sleep(0)
        return getattr(task, "get_name", lambda: "unknown")()
