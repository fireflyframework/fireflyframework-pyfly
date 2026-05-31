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
"""Saga compensator -- executes compensation for completed saga steps.

Supports five compensation policies:

* **STRICT_SEQUENTIAL** -- reverse order, one at a time, stop on first error.
* **GROUPED_PARALLEL** -- reverse topology layers, parallel within each layer.
* **RETRY_WITH_BACKOFF** -- reverse order with exponential backoff retries.
* **CIRCUIT_BREAKER** -- reverse order, opens circuit after 3 consecutive failures.
* **BEST_EFFORT_PARALLEL** -- all steps in parallel, collect errors without raising.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from pyfly.transactional.shared.types import CompensationPolicy, StepStatus

if TYPE_CHECKING:
    from pyfly.transactional.saga.core.context import SagaContext
    from pyfly.transactional.saga.engine.step_invoker import StepInvoker
    from pyfly.transactional.saga.registry.saga_definition import SagaDefinition
    from pyfly.transactional.saga.registry.step_definition import StepDefinition
    from pyfly.transactional.shared.ports.outbound import (
        CompensationErrorHandlerPort,
        TransactionalEventsPort,
    )

logger = logging.getLogger(__name__)

_DEFAULT_COMPENSATION_RETRY = 3
_DEFAULT_COMPENSATION_BACKOFF_MS = 1000
_CIRCUIT_BREAKER_THRESHOLD = 3


class SagaCompensator:
    """Executes compensation for completed saga steps using the configured policy."""

    def __init__(
        self,
        step_invoker: StepInvoker,
        events_port: TransactionalEventsPort | None = None,
        error_handler: CompensationErrorHandlerPort | None = None,
    ) -> None:
        self._step_invoker = step_invoker
        self._events_port = events_port
        self._error_handler = error_handler

    async def compensate(
        self,
        policy: CompensationPolicy,
        saga_name: str,
        completed_step_ids: list[str],
        saga_def: SagaDefinition,
        ctx: SagaContext,
        topology_layers: list[list[str]],
    ) -> None:
        """Compensate completed steps according to the given policy."""
        if not completed_step_ids:
            return

        dispatch = {
            CompensationPolicy.STRICT_SEQUENTIAL: self._strict_sequential,
            CompensationPolicy.GROUPED_PARALLEL: self._grouped_parallel,
            CompensationPolicy.RETRY_WITH_BACKOFF: self._retry_with_backoff,
            CompensationPolicy.CIRCUIT_BREAKER: self._circuit_breaker,
            CompensationPolicy.BEST_EFFORT_PARALLEL: self._best_effort_parallel,
        }

        handler = dispatch[policy]
        await handler(saga_name, completed_step_ids, saga_def, ctx, topology_layers)

    # ------------------------------------------------------------------
    # Policy implementations
    # ------------------------------------------------------------------

    async def _strict_sequential(
        self,
        saga_name: str,
        completed_step_ids: list[str],
        saga_def: SagaDefinition,
        ctx: SagaContext,
        topology_layers: list[list[str]],
    ) -> None:
        """Compensate in reverse completion order, one at a time. Stop on first error."""
        for step_id in reversed(completed_step_ids):
            step_def = saga_def.steps.get(step_id)
            if step_def is None or step_def.compensate_method is None:
                continue

            await self._invoke_one(saga_name, step_def, saga_def, ctx, raise_on_error=True)

    async def _grouped_parallel(
        self,
        saga_name: str,
        completed_step_ids: list[str],
        saga_def: SagaDefinition,
        ctx: SagaContext,
        topology_layers: list[list[str]],
    ) -> None:
        """Reverse topology layers; within each layer, compensate in parallel."""
        completed_set = set(completed_step_ids)

        for layer in reversed(topology_layers):
            # Filter to only completed steps that have a compensation method.
            layer_steps: list[StepDefinition] = []
            for step_id in layer:
                if step_id not in completed_set:
                    continue
                step_def = saga_def.steps.get(step_id)
                if step_def is None or step_def.compensate_method is None:
                    continue
                layer_steps.append(step_def)

            if not layer_steps:
                continue

            tasks = [self._invoke_one(saga_name, sd, saga_def, ctx, raise_on_error=True) for sd in layer_steps]
            await asyncio.gather(*tasks)

    async def _retry_with_backoff(
        self,
        saga_name: str,
        completed_step_ids: list[str],
        saga_def: SagaDefinition,
        ctx: SagaContext,
        topology_layers: list[list[str]],
    ) -> None:
        """Compensate in reverse order with exponential backoff retries."""
        for step_id in reversed(completed_step_ids):
            step_def = saga_def.steps.get(step_id)
            if step_def is None or step_def.compensate_method is None:
                continue

            max_retries = (
                step_def.compensation_retry if step_def.compensation_retry is not None else _DEFAULT_COMPENSATION_RETRY
            )
            backoff_ms = (
                step_def.compensation_backoff_ms
                if step_def.compensation_backoff_ms is not None
                else _DEFAULT_COMPENSATION_BACKOFF_MS
            )

            last_error: Exception | None = None
            for attempt in range(max_retries):
                try:
                    await self._invoke_one(
                        saga_name,
                        step_def,
                        saga_def,
                        ctx,
                        raise_on_error=True,
                        skip_error_handler=True,
                    )
                    last_error = None
                    break
                except Exception as exc:  # noqa: BLE001
                    last_error = exc
                    if attempt < max_retries - 1:
                        delay_s = (backoff_ms * (2**attempt)) / 1000.0
                        await asyncio.sleep(delay_s)

            if last_error is not None:
                await self._emit_compensated(saga_name, step_def.id, ctx, last_error)
                if self._error_handler is not None:
                    await self._error_handler.handle(
                        saga_name,
                        step_def.id,
                        last_error,
                        ctx,
                    )
                raise last_error

    async def _circuit_breaker(
        self,
        saga_name: str,
        completed_step_ids: list[str],
        saga_def: SagaDefinition,
        ctx: SagaContext,
        topology_layers: list[list[str]],
    ) -> None:
        """Compensate in reverse order; open circuit after consecutive failures."""
        consecutive_failures = 0

        for step_id in reversed(completed_step_ids):
            if consecutive_failures >= _CIRCUIT_BREAKER_THRESHOLD:
                logger.warning(
                    "Circuit breaker open after %d consecutive compensation failures "
                    "in saga '%s'. Skipping remaining compensations.",
                    _CIRCUIT_BREAKER_THRESHOLD,
                    saga_name,
                )
                break

            step_def = saga_def.steps.get(step_id)
            if step_def is None or step_def.compensate_method is None:
                continue

            try:
                await self._invoke_one(
                    saga_name,
                    step_def,
                    saga_def,
                    ctx,
                    raise_on_error=True,
                )
                consecutive_failures = 0
            except Exception:  # noqa: BLE001
                consecutive_failures += 1

    async def _best_effort_parallel(
        self,
        saga_name: str,
        completed_step_ids: list[str],
        saga_def: SagaDefinition,
        ctx: SagaContext,
        topology_layers: list[list[str]],
    ) -> None:
        """Compensate all steps in parallel; collect errors without raising."""
        _tasks: list[asyncio.Task[None]] = []
        step_defs_for_tasks: list[StepDefinition] = []

        for step_id in completed_step_ids:
            step_def = saga_def.steps.get(step_id)
            if step_def is None or step_def.compensate_method is None:
                continue
            step_defs_for_tasks.append(step_def)

        async def _compensate_one(sd: StepDefinition) -> Exception | None:
            try:
                result = await self._step_invoker.invoke_compensation(
                    sd,
                    saga_def.bean,
                    ctx,
                )
                ctx.compensation_results[sd.id] = result
                ctx.set_step_status(sd.id, StepStatus.COMPENSATED)
                await self._emit_compensated(saga_name, sd.id, ctx, error=None)
                return None
            except Exception as exc:  # noqa: BLE001
                await self._emit_compensated(saga_name, sd.id, ctx, error=exc)
                if self._error_handler is not None:
                    await self._error_handler.handle(saga_name, sd.id, exc, ctx)
                logger.error(
                    "Best-effort compensation failed for step '%s' in saga '%s': %s",
                    sd.id,
                    saga_name,
                    exc,
                )
                return exc

        results = await asyncio.gather(
            *[_compensate_one(sd) for sd in step_defs_for_tasks],
            return_exceptions=True,
        )

        errors = [r for r in results if r is not None and isinstance(r, Exception)]
        if errors:
            logger.warning(
                "Best-effort compensation for saga '%s' completed with %d error(s).",
                saga_name,
                len(errors),
            )

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    async def _invoke_one(
        self,
        saga_name: str,
        step_def: StepDefinition,
        saga_def: SagaDefinition,
        ctx: SagaContext,
        *,
        raise_on_error: bool = False,
        skip_error_handler: bool = False,
    ) -> None:
        """Invoke a single compensation, emit events, and optionally handle errors."""
        try:
            result = await self._step_invoker.invoke_compensation(
                step_def,
                saga_def.bean,
                ctx,
            )
            ctx.compensation_results[step_def.id] = result
            ctx.set_step_status(step_def.id, StepStatus.COMPENSATED)
            await self._emit_compensated(saga_name, step_def.id, ctx, error=None)
        except Exception as exc:
            await self._emit_compensated(saga_name, step_def.id, ctx, error=exc)
            if not skip_error_handler and self._error_handler is not None:
                await self._error_handler.handle(saga_name, step_def.id, exc, ctx)
            if raise_on_error:
                raise

    async def _emit_compensated(
        self,
        saga_name: str,
        step_id: str,
        ctx: SagaContext,
        error: Exception | None,
    ) -> None:
        """Emit an ``on_compensated`` event if the events port is configured."""
        if self._events_port is not None:
            await self._events_port.on_compensated(
                saga_name,
                ctx.correlation_id,
                step_id,
                error,
            )
