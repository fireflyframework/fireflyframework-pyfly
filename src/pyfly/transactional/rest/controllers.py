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
"""Framework-agnostic controller classes — bind into Starlette/FastAPI elsewhere.

Each controller is a *plain class* with async methods that return
JSON-serializable dicts.  The pyfly web auto-configuration takes care of
mapping HTTP verbs / paths to those methods, but the controllers themselves
have no hard dependency on Starlette so they remain easy to unit-test.
"""

from __future__ import annotations

from typing import Any

from pyfly.transactional.core.dlq import DeadLetterEntry, DeadLetterService
from pyfly.transactional.core.model import ExecutionStatus
from pyfly.transactional.core.persistence import (
    ExecutionPersistenceProvider,
    ExecutionState,
)
from pyfly.transactional.workflow.engine import WorkflowEngine


def _state_to_dict(state: ExecutionState) -> dict[str, Any]:
    return {
        "correlation_id": state.correlation_id,
        "name": state.name,
        "pattern": state.pattern.value,
        "status": state.status.value,
        "started_at": state.started_at.isoformat(),
        "updated_at": state.updated_at.isoformat(),
        "completed_at": state.completed_at.isoformat() if state.completed_at else None,
    }


def _dlq_to_dict(entry: DeadLetterEntry) -> dict[str, Any]:
    return {
        "id": entry.id,
        "execution_name": entry.execution_name,
        "correlation_id": entry.correlation_id,
        "step_id": entry.step_id,
        "error_type": entry.error_type,
        "error_message": entry.error_message,
        "timestamp": entry.timestamp.isoformat(),
        "retry_count": entry.retry_count,
    }


class OrchestrationController:
    """``GET /api/orchestration/executions`` — surface persisted runs."""

    base_path = "/api/orchestration"

    def __init__(self, persistence: ExecutionPersistenceProvider) -> None:
        self._persistence = persistence

    async def list_executions(self, status: str | None = None) -> list[dict[str, Any]]:
        status_enum = ExecutionStatus(status) if status else None
        states = await self._persistence.find_all(status=status_enum)
        return [_state_to_dict(s) for s in states]

    async def get_execution(self, correlation_id: str) -> dict[str, Any] | None:
        state = await self._persistence.find(correlation_id)
        return _state_to_dict(state) if state is not None else None


class DeadLetterController:
    """``/api/orchestration/dlq`` — list, retry and delete dead-lettered runs."""

    base_path = "/api/orchestration/dlq"

    def __init__(self, dlq: DeadLetterService) -> None:
        self._dlq = dlq

    async def list(
        self, execution_name: str | None = None, correlation_id: str | None = None
    ) -> list[dict[str, Any]]:
        entries = await self._dlq.list(execution_name=execution_name, correlation_id=correlation_id)
        return [_dlq_to_dict(e) for e in entries]

    async def get(self, entry_id: str) -> dict[str, Any] | None:
        entry = await self._dlq.get(entry_id)
        return _dlq_to_dict(entry) if entry is not None else None

    async def retry(self, entry_id: str) -> dict[str, Any]:
        ok = await self._dlq.mark_retried(entry_id)
        return {"retried": ok}

    async def delete(self, entry_id: str) -> dict[str, Any]:
        ok = await self._dlq.delete(entry_id)
        return {"deleted": ok}


class WorkflowController:
    """``/api/orchestration/workflow`` — start workflows / deliver signals."""

    base_path = "/api/orchestration/workflow"

    def __init__(self, engine: WorkflowEngine) -> None:
        self._engine = engine

    async def start(self, workflow_id: str, input: Any = None) -> dict[str, Any]:
        result = await self._engine.start(workflow_id, input)
        return {
            "workflow_id": result.workflow_id,
            "correlation_id": result.correlation_id,
            "status": result.status.value,
            "duration_ms": result.duration_ms,
            "successful": result.successful,
            "step_results": result.step_results,
            "variables": result.variables,
            "error": result.error,
        }

    async def signal(self, correlation_id: str, signal: str, payload: Any = None) -> dict[str, Any]:
        ok = await self._engine.deliver_signal(correlation_id, signal, payload)
        return {"delivered": ok}

    async def query(self, correlation_id: str, query: str, **kwargs: Any) -> Any:
        return await self._engine.query(correlation_id, query, **kwargs)
