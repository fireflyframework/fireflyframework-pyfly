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
"""Workflow engine — public entry point for starting workflows."""

from __future__ import annotations

import asyncio
import inspect
import logging
import time
from typing import Any

from pyfly.transactional.core.context import ExecutionContext
from pyfly.transactional.core.dlq import DeadLetterService
from pyfly.transactional.core.events import LoggerOrchestrationEvents, OrchestrationEvents
from pyfly.transactional.core.exceptions import OrchestrationError, StepFailedError
from pyfly.transactional.core.model import (
    ExecutionPattern,
    ExecutionStatus,
    TriggerMode,
)
from pyfly.transactional.core.persistence import (
    ExecutionPersistenceProvider,
    ExecutionState,
    InMemoryPersistenceProvider,
)
from pyfly.transactional.workflow.child_workflow_service import ChildWorkflowService
from pyfly.transactional.workflow.continue_as_new_service import ContinueAsNewService
from pyfly.transactional.workflow.executor import WorkflowExecutor
from pyfly.transactional.workflow.query_service import WorkflowQueryService
from pyfly.transactional.workflow.registry import WorkflowRegistry
from pyfly.transactional.workflow.result import WorkflowResult
from pyfly.transactional.workflow.signal_service import SignalService

_logger = logging.getLogger(__name__)


class WorkflowEngine:
    """Top-level workflow runner — coordinates registry, executor, persistence, signals."""

    def __init__(
        self,
        *,
        registry: WorkflowRegistry,
        executor: WorkflowExecutor | None = None,
        persistence: ExecutionPersistenceProvider | None = None,
        events: OrchestrationEvents | None = None,
        signal_service: SignalService | None = None,
        query_service: WorkflowQueryService | None = None,
        child_service: ChildWorkflowService | None = None,
        continue_service: ContinueAsNewService | None = None,
        dead_letter_service: DeadLetterService | None = None,
    ) -> None:
        self._registry = registry
        self._signals = signal_service or SignalService()
        self._queries = query_service or WorkflowQueryService()
        self._children = child_service or ChildWorkflowService()
        self._continue = continue_service or ContinueAsNewService()
        self._events = events or LoggerOrchestrationEvents()
        self._persistence = persistence or InMemoryPersistenceProvider()
        self._dlq = dead_letter_service
        self._executor = executor or WorkflowExecutor(
            signal_service=self._signals,
            child_service=self._children,
            events=self._events,
        )
        self._children.bind(self)
        self._continue.bind(self)

    @property
    def signals(self) -> SignalService:
        return self._signals

    @property
    def queries(self) -> WorkflowQueryService:
        return self._queries

    async def start(self, workflow_id: str, input: Any = None) -> WorkflowResult:
        """Run a workflow synchronously.  Async-mode workflows fire-and-forget."""
        definition = self._registry.get(workflow_id)
        if definition is None:
            msg = f"unknown workflow '{workflow_id}'"
            raise OrchestrationError(msg)

        if definition.trigger_mode is TriggerMode.ASYNC:
            return await self._start_async(definition, input)

        return await self._run(definition, input)

    async def deliver_signal(self, correlation_id: str, signal: str, payload: Any = None) -> bool:
        return await self._signals.deliver(correlation_id, signal, payload)

    async def query(self, correlation_id: str, query_name: str, *args: Any, **kwargs: Any) -> Any:
        return await self._queries.query(correlation_id, query_name, *args, **kwargs)

    async def list_executions(
        self, *, status: ExecutionStatus | None = None
    ) -> list[ExecutionState]:
        return await self._persistence.find_all(status=status, pattern=ExecutionPattern.WORKFLOW)

    async def get_execution(self, correlation_id: str) -> ExecutionState | None:
        return await self._persistence.find(correlation_id)

    # --- private --------------------------------------------------------

    async def _start_async(
        self, definition: Any, input: Any
    ) -> WorkflowResult:
        ctx = ExecutionContext(
            name=definition.id, pattern=ExecutionPattern.WORKFLOW, input=input
        )
        await ctx.set_status(ExecutionStatus.PENDING)
        await self._persistence.save(ExecutionState.from_context(ctx))
        asyncio.create_task(self._run(definition, input, preset_ctx=ctx))
        return WorkflowResult(
            workflow_id=definition.id,
            correlation_id=ctx.correlation_id,
            status=ExecutionStatus.PENDING,
            duration_ms=0.0,
        )

    async def _run(
        self,
        definition: Any,
        input: Any,
        *,
        preset_ctx: ExecutionContext | None = None,
    ) -> WorkflowResult:
        ctx = preset_ctx or ExecutionContext(
            name=definition.id, pattern=ExecutionPattern.WORKFLOW, input=input
        )
        started = time.perf_counter()
        await self._signals.register(ctx)
        await self._queries.register(definition, ctx)
        await self._events.on_start(
            name=definition.id, pattern=ExecutionPattern.WORKFLOW, correlation_id=ctx.correlation_id
        )
        await ctx.set_status(ExecutionStatus.RUNNING)
        await self._persistence.save(ExecutionState.from_context(ctx))

        success = False
        error: BaseException | None = None
        try:
            if definition.timeout_ms > 0:
                await asyncio.wait_for(
                    self._executor.execute(definition, ctx), timeout=definition.timeout_ms / 1000.0
                )
            else:
                await self._executor.execute(definition, ctx)
            await ctx.set_status(ExecutionStatus.COMPLETED)
            success = True
        except StepFailedError as exc:
            error = exc
            await ctx.set_status(ExecutionStatus.FAILED, exc)
            if self._dlq is not None:
                await self._dlq.capture(
                    execution_name=definition.id,
                    correlation_id=ctx.correlation_id,
                    error=exc,
                    step_id=exc.step_id,
                    input=input,
                )
        except TimeoutError as exc:
            error = exc
            await ctx.set_status(ExecutionStatus.TIMED_OUT, exc)
        except Exception as exc:  # noqa: BLE001
            error = exc
            await ctx.set_status(ExecutionStatus.FAILED, exc)
        finally:
            duration_ms = (time.perf_counter() - started) * 1000.0
            try:
                if success and definition.on_complete is not None:
                    cb_result = definition.on_complete(definition.bean, ctx)
                    if inspect.isawaitable(cb_result):
                        await cb_result
                if not success and definition.on_error is not None and error is not None:
                    cb_result = definition.on_error(definition.bean, ctx, error)
                    if inspect.isawaitable(cb_result):
                        await cb_result
            except Exception as cb_exc:  # noqa: BLE001
                _logger.warning("workflow callback raised: %s", cb_exc)
            await self._persistence.save(ExecutionState.from_context(ctx))
            await self._events.on_completed(
                name=definition.id,
                pattern=ExecutionPattern.WORKFLOW,
                correlation_id=ctx.correlation_id,
                success=success,
                duration_ms=duration_ms,
            )
            await self._signals.unregister(ctx.correlation_id)
            await self._queries.unregister(ctx.correlation_id)

        return WorkflowResult(
            workflow_id=definition.id,
            correlation_id=ctx.correlation_id,
            status=ctx.status,
            duration_ms=(time.perf_counter() - started) * 1000.0,
            step_results={sid: rec.result for sid, rec in ctx.get_all_steps().items()},
            variables=ctx.get_all_variables(),
            error=ctx.error,
        )
