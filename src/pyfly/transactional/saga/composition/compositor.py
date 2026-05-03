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

    Sagas in the same DAG layer run concurrently via :func:`asyncio.gather`.
    On failure the compositor compensates all previously completed sagas.

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

        try:
            for layer in layers:
                results = await asyncio.gather(
                    *(
                        self._execute_saga(
                            saga_name=saga_name,
                            composition=composition,
                            ctx=ctx,
                            initial_input=initial_input,
                            headers=headers,
                        )
                        for saga_name in layer
                    ),
                    return_exceptions=True,
                )

                # Process results from this layer.
                for saga_name, result in zip(layer, results, strict=True):
                    if isinstance(result, BaseException):
                        raise result

                    ctx.saga_results[saga_name] = result
                    completed_sagas.append(saga_name)

                    if not result.success:
                        msg = f"Saga '{saga_name}' failed in composition '{composition.name}'"
                        raise RuntimeError(msg)

        except Exception as exc:
            ctx.error = exc
            logger.warning(
                "Composition '%s' (correlation_id=%s) failed: %s. Compensating %d completed saga(s).",
                composition.name,
                ctx.correlation_id,
                exc,
                len(completed_sagas),
            )
            await self._compensation_manager.compensate_completed(
                completed_sagas=completed_sagas,
                composition=composition,
                ctx=ctx,
                saga_engine=self._saga_engine,
            )

        return ctx

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
            correlation_id=ctx.correlation_id,
        )
