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

Each controller is a ``@rest_controller`` bean with async methods that return
JSON-serializable dicts.  Routing is declared with pure-metadata decorators
(``@request_mapping`` / ``@get_mapping`` / ``@post_mapping`` / ``@delete_mapping``)
and parameter binders (``PathVar`` / ``QueryParam`` / ``Body`` / ``Valid``).
Those decorators add no runtime dependency on Starlette, so the transactional
module stays decoupled from any concrete web adapter while still being
discovered and mounted by the ``ControllerRegistrar``.
"""

from __future__ import annotations

from typing import Any, cast

from pydantic import BaseModel

from pyfly.container import rest_controller
from pyfly.transactional.core.dlq import DeadLetterEntry, DeadLetterService
from pyfly.transactional.core.model import ExecutionStatus
from pyfly.transactional.core.persistence import (
    ExecutionPersistenceProvider,
    ExecutionState,
)
from pyfly.transactional.workflow.engine import WorkflowEngine
from pyfly.web import (
    Body,
    PathVar,
    QueryParam,
    Valid,
    delete_mapping,
    get_mapping,
    post_mapping,
    request_mapping,
)


class StartRequest(BaseModel):
    """Request body for :meth:`WorkflowController.start`."""

    workflow_id: str
    input: Any = None


class SignalRequest(BaseModel):
    """Request body for :meth:`WorkflowController.signal`."""

    correlation_id: str
    signal: str
    payload: Any = None


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


@rest_controller
@request_mapping("/api/orchestration")
class OrchestrationController:
    """``GET /api/orchestration/executions`` — surface persisted runs."""

    base_path = "/api/orchestration"

    def __init__(self, persistence: ExecutionPersistenceProvider) -> None:
        self._persistence = persistence

    @get_mapping("/executions")
    async def list_executions(self, status: QueryParam[str]) -> list[dict[str, Any]]:
        status_str = cast("str | None", status)
        status_enum = ExecutionStatus(status_str) if status_str else None
        states = await self._persistence.find_all(status=status_enum)
        return [_state_to_dict(s) for s in states]

    @get_mapping("/executions/{correlation_id}")
    async def get_execution(self, correlation_id: PathVar[str]) -> dict[str, Any] | None:
        cid = cast(str, correlation_id)
        state = await self._persistence.find(cid)
        return _state_to_dict(state) if state is not None else None


@rest_controller
@request_mapping("/api/orchestration/dlq")
class DeadLetterController:
    """``/api/orchestration/dlq`` — list, retry and delete dead-lettered runs."""

    base_path = "/api/orchestration/dlq"

    def __init__(self, dlq: DeadLetterService) -> None:
        self._dlq = dlq

    @get_mapping("")
    async def list(
        self,
        execution_name: QueryParam[str],
        correlation_id: QueryParam[str],
    ) -> list[dict[str, Any]]:
        name = cast("str | None", execution_name)
        cid = cast("str | None", correlation_id)
        entries = await self._dlq.list(execution_name=name, correlation_id=cid)
        return [_dlq_to_dict(e) for e in entries]

    @get_mapping("/{entry_id}")
    async def get(self, entry_id: PathVar[str]) -> dict[str, Any] | None:
        eid = cast(str, entry_id)
        entry = await self._dlq.get(eid)
        return _dlq_to_dict(entry) if entry is not None else None

    @post_mapping("/{entry_id}/retry")
    async def retry(self, entry_id: PathVar[str]) -> dict[str, Any]:
        eid = cast(str, entry_id)
        ok = await self._dlq.mark_retried(eid)
        return {"retried": ok}

    @delete_mapping("/{entry_id}")
    async def delete(self, entry_id: PathVar[str]) -> dict[str, Any]:
        eid = cast(str, entry_id)
        ok = await self._dlq.delete(eid)
        return {"deleted": ok}


@rest_controller
@request_mapping("/api/orchestration/workflow")
class WorkflowController:
    """``/api/orchestration/workflow`` — start workflows / deliver signals."""

    base_path = "/api/orchestration/workflow"

    def __init__(self, engine: WorkflowEngine) -> None:
        self._engine = engine

    @post_mapping("/start")
    async def start(self, body: Valid[Body[StartRequest]]) -> dict[str, Any]:
        req = cast(StartRequest, body)
        result = await self._engine.start(req.workflow_id, req.input)
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

    @post_mapping("/signal")
    async def signal(self, body: Valid[Body[SignalRequest]]) -> dict[str, Any]:
        req = cast(SignalRequest, body)
        ok = await self._engine.deliver_signal(req.correlation_id, req.signal, req.payload)
        return {"delivered": ok}

    async def query(self, correlation_id: str, query: str, **kwargs: Any) -> Any:
        return await self._engine.query(correlation_id, query, **kwargs)
