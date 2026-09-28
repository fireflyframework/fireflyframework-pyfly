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
"""SagaCompositor — executes a multi-saga composition as a DAG."""

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Any

from pyfly.data.transaction.template import run_shielded
from pyfly.transactional.core.backpressure import cancel_and_wait, retrieve_outcomes
from pyfly.transactional.core.exceptions import OrchestrationError
from pyfly.transactional.saga.composition.compensation_manager import (
    CompensationManager,
)
from pyfly.transactional.saga.composition.composition import SagaComposition
from pyfly.transactional.saga.composition.composition_context import (
    CompositionContext,
)
from pyfly.transactional.saga.composition.data_flow_manager import DataFlowManager
from pyfly.transactional.saga.engine.topology import SagaTopology

logger = logging.getLogger(__name__)


class SagaCompositor:
    """Executes a :class:`SagaComposition` using layer-based ordering.

    Sagas in the same DAG layer run concurrently, each in a task of its own,
    and the compositor waits for all of them. On failure the compositor
    compensates every saga that completed: those of the earlier layers, and
    those of the failing layer, whichever of its sagas failed. Each saga runs
    under a correlation id of its own, ``<composition correlation id>:<saga
    name>`` (:meth:`saga_correlation_id`), so a persistence provider keeps
    each saga's state apart.

    - A saga that ends cancelled although nothing cancelled the composition
      (it awaited something that was cancelled) failed: the composition fails
      with an :class:`~pyfly.transactional.core.exceptions.OrchestrationError`
      and compensates, as for any other failure.
    - When the caller cancels the composition, the sagas of the running layer
      are cancelled and awaited (each compensates its own committed steps),
      the sagas that completed are compensated, and ``CancelledError`` is
      re-raised. The compensation runs to completion whatever happens to the
      calling task, and in a task of its own with the transaction state
      cleared (see :class:`CompensationManager`).

    Parameters
    ----------
    saga_engine:
        The saga engine used to execute individual sagas.  Only the
        ``execute`` method is called.
    compensation_manager:
        Optional custom compensation manager.  If ``None`` the default
        :class:`CompensationManager` is used.
    """

    def __init__(
        self,
        saga_engine: Any,
        compensation_manager: CompensationManager | None = None,
    ) -> None:
        self._saga_engine = saga_engine
        self._compensation_manager = compensation_manager or CompensationManager()

    async def execute(
        self,
        composition: SagaComposition,
        initial_input: Any = None,
        headers: dict[str, str] | None = None,
    ) -> CompositionContext:
        """Execute a composition.

        Uses :meth:`SagaTopology.compute_layers` to determine execution
        ordering.  Sagas in the same layer run in parallel.  On failure
        completed sagas are compensated.

        Parameters
        ----------
        composition:
            The validated composition to execute.
        initial_input:
            Input data shared with root sagas and merged by the
            :class:`DataFlowManager` for downstream sagas.
        headers:
            Optional headers forwarded to every saga execution.

        Returns
        -------
        CompositionContext
            The final context containing all saga results, inputs, and
            any error information.
        """
        ctx = CompositionContext(
            correlation_id=str(uuid.uuid4()),
            composition_name=composition.name,
        )

        # Build dependency map and compute execution layers.
        deps: dict[str, list[str]] = {name: list(entry.depends_on) for name, entry in composition.entries.items()}
        layers = SagaTopology.compute_layers(deps)
        completed_sagas: list[str] = []

        logger.info(
            "Starting composition '%s' (correlation_id=%s) with %d layer(s)",
            composition.name,
            ctx.correlation_id,
            len(layers),
        )

        cancellation: asyncio.CancelledError | None = None
        try:
            for layer in layers:
                await self._run_layer(layer, composition, ctx, initial_input, headers, completed_sagas)
        except asyncio.CancelledError as exc:
            # The caller cancelled the composition: every saga of the running layer has ended. Undo the ones
            # that completed, then let the cancellation propagate.
            cancellation = exc
        except Exception as exc:
            ctx.error = exc

        if ctx.error is None and cancellation is None:
            return ctx

        logger.warning(
            "Composition '%s' (correlation_id=%s) %s. Compensating %d completed saga(s).",
            composition.name,
            ctx.correlation_id,
            "was cancelled" if cancellation is not None else f"failed: {ctx.error}",
            len(completed_sagas),
        )
        _result, error, cancelled = await run_shielded(
            self._compensation_manager.compensate_completed(
                completed_sagas=completed_sagas,
                composition=composition,
                ctx=ctx,
                saga_engine=self._saga_engine,
            )
        )
        if cancellation is not None:
            raise cancellation
        if cancelled:
            raise asyncio.CancelledError
        if error is not None:
            raise error
        return ctx

    async def _run_layer(
        self,
        layer: list[str],
        composition: SagaComposition,
        ctx: CompositionContext,
        initial_input: Any,
        headers: dict[str, str] | None,
        completed_sagas: list[str],
    ) -> None:
        """Run the sagas of one layer concurrently, record the ones that completed, and raise the first failure.

        Every saga of the layer is recorded before the failure is raised: a saga that completed after the failed
        one must be compensated too. A cancellation of the caller cancels and awaits the layer's sagas, records
        the ones that had completed, and propagates.
        """
        tasks = {
            saga_name: asyncio.ensure_future(
                self._execute_saga(
                    saga_name=saga_name,
                    composition=composition,
                    ctx=ctx,
                    initial_input=initial_input,
                    headers=headers,
                )
            )
            for saga_name in layer
        }
        try:
            if tasks:
                await asyncio.wait(tasks.values())
        except asyncio.CancelledError:
            await cancel_and_wait(tasks.values())
            for saga_name, task in tasks.items():
                if not task.cancelled() and task.exception() is None:
                    ctx.saga_results[saga_name] = task.result()
                    completed_sagas.append(saga_name)
            raise
        retrieve_outcomes(tasks.values())

        failure: BaseException | None = None
        for saga_name, task in tasks.items():
            if task.cancelled():
                # Nothing here cancelled it (the composition was not cancelled): the saga failed.
                failure = failure or OrchestrationError(
                    f"Saga '{saga_name}' ended cancelled in composition '{composition.name}' although the "
                    "composition was not cancelled; the saga failed"
                )
                continue
            error = task.exception()
            if error is not None:
                failure = failure or error
                continue
            result = task.result()
            ctx.saga_results[saga_name] = result
            completed_sagas.append(saga_name)
            if not result.success and failure is None:
                failure = RuntimeError(f"Saga '{saga_name}' failed in composition '{composition.name}'")
        if failure is not None:
            raise failure

    async def _execute_saga(
        self,
        saga_name: str,
        composition: SagaComposition,
        ctx: CompositionContext,
        initial_input: Any,
        headers: dict[str, str] | None,
    ) -> Any:
        """Execute a single saga within the composition."""
        entry = composition.entries[saga_name]
        resolved_input = DataFlowManager.resolve_input(entry, ctx, initial_input)
        ctx.saga_inputs[saga_name] = resolved_input

        logger.debug(
            "Executing saga '%s' in composition '%s'",
            saga_name,
            composition.name,
        )

        return await self._saga_engine.execute(
            saga_name,
            input_data=resolved_input,
            headers=headers,
            correlation_id=self.saga_correlation_id(ctx.correlation_id, saga_name),
        )

    @staticmethod
    def saga_correlation_id(composition_correlation_id: str, saga_name: str) -> str:
        """The correlation id a saga of a composition runs under: the composition's, then the saga's name.

        Each saga is an execution of its own (the persistence providers key an execution's state by its
        correlation id), and its id still names the composition it belongs to."""
        return f"{composition_correlation_id}:{saga_name}"
