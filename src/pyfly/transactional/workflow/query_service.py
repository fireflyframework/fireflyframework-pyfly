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
"""Read-side queries against a live workflow execution."""

from __future__ import annotations

import asyncio
import inspect
from typing import Any

from pyfly.transactional.core.context import ExecutionContext
from pyfly.transactional.core.exceptions import OrchestrationError
from pyfly.transactional.workflow.definition import WorkflowDefinition


class WorkflowQueryService:
    """Routes incoming query requests to ``@workflow_query`` methods."""

    def __init__(self) -> None:
        self._executions: dict[str, tuple[WorkflowDefinition, ExecutionContext]] = {}
        self._lock = asyncio.Lock()

    async def register(self, definition: WorkflowDefinition, ctx: ExecutionContext) -> None:
        async with self._lock:
            self._executions[ctx.correlation_id] = (definition, ctx)

    async def unregister(self, correlation_id: str) -> None:
        async with self._lock:
            self._executions.pop(correlation_id, None)

    async def query(self, correlation_id: str, query_name: str, *args: Any, **kwargs: Any) -> Any:
        async with self._lock:
            entry = self._executions.get(correlation_id)
        if entry is None:
            msg = f"workflow execution '{correlation_id}' is not active"
            raise OrchestrationError(msg)
        definition, ctx = entry
        method = definition.queries.get(query_name)
        if method is None:
            msg = f"workflow '{definition.id}' has no @workflow_query named '{query_name}'"
            raise OrchestrationError(msg)
        result = method(definition.bean, ctx, *args, **kwargs)
        if inspect.isawaitable(result):
            return await result
        return result
