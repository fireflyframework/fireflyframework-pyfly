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
"""Saga engine — main orchestrator coordinating execution, compensation, persistence, and events.

When the saga fails, or its caller cancels it, the engine compensates every step whose work committed: the
completed steps, and the steps that failed, timed out or were cancelled after a unit of work of theirs
committed (:pyattr:`~pyfly.transactional.saga.core.context.SagaContext.committed_steps`, see
:mod:`~pyfly.transactional.saga.engine.execution_orchestrator`). Compensation runs in a task of its own with
the transaction state cleared, to completion even when the caller is cancelled meanwhile; a cancelled saga
then re-raises ``CancelledError`` once it is compensated and its final state is recorded.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import UTC, datetime
from typing import Any

from pyfly.data.transaction import detached
from pyfly.data.transaction.template import run_shielded
from pyfly.transactional.saga.core.context import SagaContext
from pyfly.transactional.saga.core.result import SagaResult, StepOutcome
from pyfly.transactional.saga.engine.compensator import SagaCompensator
from pyfly.transactional.saga.engine.execution_orchestrator import (
    SagaExecutionOrchestrator,
)
from pyfly.transactional.saga.engine.step_invoker import StepInvoker
from pyfly.transactional.saga.registry.saga_definition import SagaDefinition
from pyfly.transactional.saga.registry.saga_registry import SagaRegistry
from pyfly.transactional.shared.ports.outbound import (
    TransactionalEventsPort,
    TransactionalPersistencePort,
)
from pyfly.transactional.shared.types import CompensationPolicy, StepStatus

logger = logging.getLogger(__name__)


class SagaEngine:
    """Main saga orchestrator -- coordinates execution, compensation, persistence, and events."""

    def __init__(
        self,
        registry: SagaRegistry,
        step_invoker: StepInvoker,
        execution_orchestrator: SagaExecutionOrchestrator,
        compensator: SagaCompensator,
        persistence_port: TransactionalPersistencePort | None = None,
        events_port: TransactionalEventsPort | None = None,
        default_compensation_policy: CompensationPolicy = CompensationPolicy.STRICT_SEQUENTIAL,
    ) -> None:
        self._registry = registry
        self._step_invoker = step_invoker
        self._execution_orchestrator = execution_orchestrator
        self._compensator = compensator
        self._persistence_port = persistence_port
        self._events_port = events_port
        # Configured global default (saga compensation_policy property) used when
        # a caller does not override per-execution (audit #170).
        self._default_compensation_policy = default_compensation_policy

    async def execute(
        self,
        saga_name: str,
        input_data: Any = None,
        headers: dict[str, str] | None = None,
        correlation_id: str | None = None,
        compensation_policy: CompensationPolicy | None = None,
    ) -> SagaResult:
        """Execute a saga by name.

        Args:
            saga_name: Name of the saga to execute (must be registered).
            input_data: Input data passed to saga steps.
            headers: Optional headers (e.g., trace IDs, user IDs).
            correlation_id: Optional correlation ID (auto-generated if not provided).
            compensation_policy: Policy for compensation on failure.

        Returns:
            SagaResult with full execution details.

        Raises:
            ValueError: If saga_name is not registered.
            asyncio.CancelledError: The caller cancelled the saga. Its steps have all ended, the ones that
                committed are compensated, and ``on_completed`` / ``mark_completed`` recorded the failure.
        """
        # Fall back to the configured global policy when not overridden (#170).
        if compensation_policy is None:
            compensation_policy = self._default_compensation_policy

        # 1. Look up saga from registry.
        saga_def: SagaDefinition | None = self._registry.get(saga_name)
        if saga_def is None:
            msg = f"Saga '{saga_name}' is not registered"
            raise ValueError(msg)

        # 2. Create SagaContext.
        ctx = SagaContext(
            correlation_id=correlation_id or str(uuid.uuid4()),
            saga_name=saga_name,
            headers=headers or {},
        )

        started_at = datetime.now(UTC)
        success = False
        error: Exception | None = None

        # 3. Emit on_start event.
        if self._events_port is not None:
            await self._events_port.on_start(saga_name, ctx.correlation_id)

        # 4. Persist initial state.
        if self._persistence_port is not None:
            await self._persistence_port.persist_state(
                {
                    "saga_name": saga_name,
                    "correlation_id": ctx.correlation_id,
                    "headers": ctx.headers,
                    # Store the datetime itself (not an ISO string) so the
                    # persistence port's get_stale() can compare it against a
                    # datetime cutoff — passing a string raised TypeError during
                    # stale-saga recovery.
                    "started_at": started_at,
                }
            )

        cancelled = False
        cancellation: asyncio.CancelledError | None = None  # re-raised as is: a cancel scope knows its own
        try:
            try:
                # 5a. Execute via orchestrator.
                await self._execution_orchestrator.execute(
                    saga_def,
                    ctx,
                    step_input=input_data,
                )
                success = True
            except asyncio.CancelledError as exc:
                # The caller cancelled the saga (a timeout, a disconnect, shutdown). Every step task has ended:
                # undo the steps that committed, then let the cancellation propagate.
                cancelled = True
                cancellation = exc
                logger.debug(
                    "Saga '%s' (correlation_id=%s) cancelled. Running compensation.",
                    saga_name,
                    ctx.correlation_id,
                )
            except Exception as exc:
                error = exc
                logger.debug(
                    "Saga '%s' (correlation_id=%s) failed: %s. Running compensation.",
                    saga_name,
                    ctx.correlation_id,
                    exc,
                )
            if not success:
                # 6a. Compensate on failure: every step that committed, the ones that failed or were cancelled
                # after committing included, newest first.
                cancelled |= await self._compensate(compensation_policy, saga_name, saga_def, ctx)
        finally:
            # 7. Emit on_completed and persist the final state, to completion even when cancelled.
            _result, finish_error, finish_cancelled = await run_shielded(self._finish(saga_name, ctx, success))
            cancelled |= finish_cancelled
            if finish_error is not None and not cancelled:
                raise finish_error
        if cancellation is not None:
            raise cancellation
        if cancelled:
            raise asyncio.CancelledError

        # 8. Build and return SagaResult.
        return self._build_result(
            saga_name=saga_name,
            saga_def=saga_def,
            ctx=ctx,
            started_at=started_at,
            success=success,
            error=error,
        )

    async def _compensate(
        self,
        policy: CompensationPolicy,
        saga_name: str,
        saga_def: SagaDefinition,
        ctx: SagaContext,
    ) -> bool:
        """Compensate every step that committed; returns whether the caller was cancelled meanwhile.

        The compensations run to completion whatever happens to the calling task (a cancellation is reported,
        and re-raised once they ran), in a task of their own with the transaction state cleared: each
        compensation is a unit of work of its own, as the step it undoes was, never part of the caller's.
        """

        async def compensate() -> None:
            try:
                await self._compensator.compensate(
                    policy=policy,
                    saga_name=saga_name,
                    completed_step_ids=ctx.steps_to_compensate(),
                    saga_def=saga_def,
                    ctx=ctx,
                    topology_layers=ctx.topology_layers,
                )
            except Exception as comp_exc:
                logger.warning(
                    "Compensation for saga '%s' (correlation_id=%s) raised: %s",
                    saga_name,
                    ctx.correlation_id,
                    comp_exc,
                )

        _result, _error, cancelled = await run_shielded(detached(compensate(), name=f"saga-compensation-{saga_name}"))
        return cancelled

    async def _finish(self, saga_name: str, ctx: SagaContext, success: bool) -> None:
        """Emit ``on_completed`` and persist the final state."""
        if self._events_port is not None:
            await self._events_port.on_completed(saga_name, ctx.correlation_id, success)
        if self._persistence_port is not None:
            await self._persistence_port.mark_completed(ctx.correlation_id, success)

    @staticmethod
    def _build_result(
        saga_name: str,
        saga_def: SagaDefinition,
        ctx: SagaContext,
        started_at: datetime,
        success: bool,
        error: Exception | None,
    ) -> SagaResult:
        """Build a SagaResult from the execution context."""
        steps: dict[str, StepOutcome] = {}

        for step_id in saga_def.steps:
            steps[step_id] = StepOutcome(
                status=ctx.step_statuses.get(step_id, StepStatus.PENDING),
                attempts=ctx.step_attempts.get(step_id, 0),
                latency_ms=ctx.step_latencies_ms.get(step_id, 0.0),
                result=ctx.step_results.get(step_id),
                error=None if success else (error if ctx.step_statuses.get(step_id) == StepStatus.FAILED else None),
                compensated=step_id in ctx.compensation_results,
                started_at=ctx.step_started_at.get(step_id, started_at),
                compensation_result=ctx.compensation_results.get(step_id),
                compensation_error=ctx.compensation_errors.get(step_id),
            )

        return SagaResult(
            saga_name=saga_name,
            correlation_id=ctx.correlation_id,
            started_at=started_at,
            completed_at=datetime.now(UTC),
            success=success,
            error=error,
            headers=ctx.headers,
            steps=steps,
        )
