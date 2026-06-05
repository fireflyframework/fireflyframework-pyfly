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
"""TCC engine — main orchestrator coordinating three-phase execution, persistence, and events."""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from typing import Any

from pyfly.transactional.shared.ports.outbound import (
    TransactionalEventsPort,
    TransactionalPersistencePort,
)
from pyfly.transactional.tcc.core.context import TccContext
from pyfly.transactional.tcc.core.phase import TccPhase
from pyfly.transactional.tcc.core.result import ParticipantResult, TccResult
from pyfly.transactional.tcc.engine.execution_orchestrator import (
    TccExecutionOrchestrator,
)
from pyfly.transactional.tcc.engine.participant_invoker import TccParticipantInvoker
from pyfly.transactional.tcc.registry.tcc_definition import TccDefinition
from pyfly.transactional.tcc.registry.tcc_registry import TccRegistry

logger = logging.getLogger(__name__)


class TccEngine:
    """Main TCC orchestrator -- coordinates three-phase execution, persistence, and events."""

    def __init__(
        self,
        registry: TccRegistry,
        participant_invoker: TccParticipantInvoker,
        orchestrator: TccExecutionOrchestrator,
        persistence_port: TransactionalPersistencePort | None = None,
        events_port: TransactionalEventsPort | None = None,
    ) -> None:
        self._registry = registry
        self._participant_invoker = participant_invoker
        self._orchestrator = orchestrator
        self._persistence_port = persistence_port
        self._events_port = events_port

    async def execute(
        self,
        tcc_name: str,
        input_data: Any = None,
        headers: dict[str, str] | None = None,
        correlation_id: str | None = None,
    ) -> TccResult:
        """Execute a TCC by name.

        Args:
            tcc_name: Name of the TCC to execute (must be registered).
            input_data: Input data passed to TCC participants.
            headers: Optional headers (e.g., trace IDs, user IDs).
            correlation_id: Optional correlation ID (auto-generated if not provided).

        Returns:
            TccResult with full execution details.

        Raises:
            ValueError: If tcc_name is not registered.
        """
        # 1. Look up TCC from registry.
        tcc_def: TccDefinition | None = self._registry.get(tcc_name)
        if tcc_def is None:
            msg = f"TCC '{tcc_name}' is not registered"
            raise ValueError(msg)

        # 2. Create TccContext.
        ctx = TccContext(
            correlation_id=correlation_id or str(uuid.uuid4()),
            tcc_name=tcc_name,
            headers=headers or {},
        )

        started_at = datetime.now(UTC)
        success = False
        failed_participant_id: str | None = None
        error: Exception | None = None

        # 3. Emit on_start event.
        if self._events_port is not None:
            await self._events_port.on_start(tcc_name, ctx.correlation_id)

        # 4. Persist initial state.
        if self._persistence_port is not None:
            await self._persistence_port.persist_state(
                {
                    "tcc_name": tcc_name,
                    "correlation_id": ctx.correlation_id,
                    "headers": ctx.headers,
                    "started_at": started_at.isoformat(),
                }
            )

        try:
            # 5. Execute via orchestrator.
            success, failed_participant_id = await self._orchestrator.execute(
                tcc_def,
                ctx,
                input_data=input_data,
            )
        except Exception as exc:
            error = exc
            logger.debug(
                "TCC '%s' (correlation_id=%s) raised: %s",
                tcc_name,
                ctx.correlation_id,
                exc,
            )
        finally:
            # 6. Emit on_completed event.
            if self._events_port is not None:
                await self._events_port.on_completed(
                    tcc_name,
                    ctx.correlation_id,
                    success,
                )

            # 7. Persist final state.
            if self._persistence_port is not None:
                await self._persistence_port.mark_completed(
                    ctx.correlation_id,
                    success,
                )

        # 8. Build and return TccResult.
        return self._build_result(
            tcc_name=tcc_name,
            tcc_def=tcc_def,
            ctx=ctx,
            started_at=started_at,
            success=success,
            error=error,
            failed_participant_id=failed_participant_id,
        )

    @staticmethod
    def _build_result(
        tcc_name: str,
        tcc_def: TccDefinition,
        ctx: TccContext,
        started_at: datetime,
        success: bool,
        error: Exception | None,
        failed_participant_id: str | None,
    ) -> TccResult:
        """Build a TccResult from the execution context."""
        participant_results: dict[str, ParticipantResult] = {}

        for pid, _p_def in tcc_def.participants.items():
            participant_results[pid] = ParticipantResult(
                participant_id=pid,
                try_result=ctx.try_results.get(pid),
                try_error=ctx.get_participant_error(pid, TccPhase.TRY),
                confirm_error=ctx.get_participant_error(pid, TccPhase.CONFIRM),
                cancel_error=ctx.get_participant_error(pid, TccPhase.CANCEL),
                final_phase=ctx.participant_statuses.get(pid, TccPhase.TRY),
                latency_ms=ctx.participant_latency.get(pid, 0.0),
            )

        return TccResult(
            correlation_id=ctx.correlation_id,
            tcc_name=tcc_name,
            success=success,
            final_phase=ctx.current_phase,
            try_results=dict(ctx.try_results),
            participant_results=participant_results,
            started_at=started_at,
            completed_at=datetime.now(UTC),
            error=error,
            failed_participant_id=failed_participant_id,
        )
