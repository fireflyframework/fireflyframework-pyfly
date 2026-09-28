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
"""Manages cross-saga compensation within a composition.

The compensations run as the saga engine runs a saga's own: in a task of their own with the transaction
state cleared (:func:`~pyfly.data.transaction.detached`), to completion even when the caller is cancelled
meanwhile. Each saga of a composition committed its steps on its own, never in the caller's unit of work, so
its compensation commits on its own too: a caller's rollback must not take the compensation with it while the
saga's effects stay.

Once they ran, the persisted state of each saga that had completed is updated where the engine records a
saga's state (in the caller's task, as ``mark_completed`` is): its compensated steps are recorded
``COMPENSATED`` (``update_step_status``) and the saga is marked failed (``mark_completed(..., False)``), since
its effects did not stay.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from pyfly.data.transaction import detached
from pyfly.data.transaction.template import run_shielded
from pyfly.transactional.saga.core.context import SagaContext
from pyfly.transactional.shared.types import StepStatus

if TYPE_CHECKING:
    from pyfly.transactional.saga.composition.composition import SagaComposition
    from pyfly.transactional.saga.composition.composition_context import (
        CompositionContext,
    )
    from pyfly.transactional.saga.core.result import SagaResult

logger = logging.getLogger(__name__)


class CompensationManager:
    """Compensates successfully completed sagas after a composition failure (see the module documentation).

    Compensation is executed in reverse completion order so that the most
    recently completed saga is compensated first.  The manager delegates to
    the saga engine to re-execute each saga's built-in compensation steps.
    """

    async def compensate_completed(
        self,
        completed_sagas: list[str],
        composition: SagaComposition,
        ctx: CompositionContext,
        saga_engine: Any,
    ) -> None:
        """Compensate completed sagas in reverse order, then record their persisted state.

        The compensations run detached and shielded (see the module documentation); a cancellation of the
        calling task meanwhile is re-raised once they ran and the persisted states are updated.

        Parameters
        ----------
        completed_sagas:
            Names of sagas that completed successfully and need to be
            compensated.
        composition:
            The composition definition (for policy look-up).
        ctx:
            The mutable composition context where compensation status is
            recorded.
        saga_engine:
            The saga engine used to trigger compensation.

        Raises
        ------
        asyncio.CancelledError
            The caller was cancelled while the compensations ran; they ran to completion first.
        """
        if not completed_sagas:
            return

        compensated: dict[str, SagaContext] = {}
        _result, _error, cancelled = await run_shielded(
            detached(
                self._compensate_each(completed_sagas, composition, ctx, saga_engine, compensated),
                name=f"composition-compensation-{composition.name}",
            )
        )
        _result, _error, recording_cancelled = await run_shielded(
            self._record_compensated(compensated, ctx, saga_engine)
        )
        if cancelled or recording_cancelled:
            raise asyncio.CancelledError

    async def _compensate_each(
        self,
        completed_sagas: list[str],
        composition: SagaComposition,
        ctx: CompositionContext,
        saga_engine: Any,
        compensated: dict[str, SagaContext],
    ) -> None:
        """Compensate each saga, newest first; *compensated* gets the context of each one that had steps to."""
        for saga_name in reversed(completed_sagas):
            logger.info(
                "Compensating saga '%s' in composition '%s' (correlation_id=%s)",
                saga_name,
                composition.name,
                ctx.correlation_id,
            )
            try:
                saga_def = saga_engine._registry.get(saga_name)
                if saga_def is not None:
                    saga_result = ctx.saga_results.get(saga_name)
                    completed_step_ids = [
                        step_id
                        for step_id, outcome in (saga_result.steps.items() if saga_result else [])
                        if outcome.status == StepStatus.DONE
                    ]
                    if completed_step_ids:
                        saga_ctx = self._build_saga_context(
                            saga_name,
                            saga_result,
                        )
                        compensated[saga_name] = saga_ctx
                        await saga_engine._compensator.compensate(
                            policy=composition.compensation_policy,
                            saga_name=saga_name,
                            completed_step_ids=completed_step_ids,
                            saga_def=saga_def,
                            ctx=saga_ctx,
                            topology_layers=[],
                        )
                ctx.compensated_sagas.append(saga_name)
            except Exception as exc:
                logger.error(
                    "Compensation failed for saga '%s': %s",
                    saga_name,
                    exc,
                )
                ctx.compensated_sagas.append(saga_name)

    @staticmethod
    async def _record_compensated(
        compensated: dict[str, SagaContext],
        ctx: CompositionContext,
        saga_engine: Any,
    ) -> None:
        """Record the compensated steps of each saga in *compensated*, and mark the saga failed."""
        persistence = getattr(saga_engine, "_persistence_port", None)
        if persistence is None:
            return
        for saga_name, saga_ctx in compensated.items():
            try:
                for step_id, status in saga_ctx.step_statuses.items():
                    if status == StepStatus.COMPENSATED:
                        await persistence.update_step_status(saga_ctx.correlation_id, step_id, status.value)
                saga_result = ctx.saga_results.get(saga_name)
                if saga_result is not None and saga_result.success:
                    await persistence.mark_completed(saga_ctx.correlation_id, False)
            except Exception as exc:  # noqa: BLE001 — the compensation ran; a state that cannot be updated is logged
                logger.warning(
                    "Could not record the compensation of saga '%s' (correlation_id=%s): %s",
                    saga_name,
                    saga_ctx.correlation_id,
                    exc,
                )

    @staticmethod
    def _build_saga_context(
        saga_name: str,
        saga_result: SagaResult | None,
    ) -> SagaContext:
        """Reconstruct a minimal SagaContext from a completed SagaResult.

        The compensator needs a mutable SagaContext to track compensation
        outcomes.  Since the composition layer only retains the immutable
        SagaResult, we rebuild the essential fields here.
        """
        if saga_result is None:
            return SagaContext(saga_name=saga_name)

        step_statuses = {step_id: outcome.status for step_id, outcome in saga_result.steps.items()}
        step_results = {
            step_id: outcome.result for step_id, outcome in saga_result.steps.items() if outcome.result is not None
        }
        return SagaContext(
            correlation_id=saga_result.correlation_id,
            saga_name=saga_name,
            headers=dict(saga_result.headers),
            step_statuses=step_statuses,
            step_results=step_results,
        )
