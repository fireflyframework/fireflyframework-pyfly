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
"""Saga execution orchestrator — topological layer-based parallel execution."""

from __future__ import annotations

import asyncio
import random
import time
from typing import Any

from pyfly.transactional.saga.core.context import SagaContext
from pyfly.transactional.saga.engine.step_invoker import StepInvoker
from pyfly.transactional.saga.engine.topology import SagaTopology
from pyfly.transactional.saga.registry.saga_definition import SagaDefinition
from pyfly.transactional.saga.registry.step_definition import StepDefinition
from pyfly.transactional.shared.ports.outbound import TransactionalEventsPort
from pyfly.transactional.shared.types import StepStatus


class SagaExecutionOrchestrator:
    """Executes saga steps in topological order with retry/timeout.

    Steps within a single dependency layer run concurrently (subject to an
    optional :pyattr:`SagaDefinition.layer_concurrency` semaphore).  Each step
    is individually wrapped in a retry loop with exponential backoff, optional
    jitter, and an ``asyncio.wait_for`` timeout.
    """

    def __init__(
        self,
        step_invoker: StepInvoker,
        events_port: TransactionalEventsPort | None = None,
    ) -> None:
        self._step_invoker = step_invoker
        self._events_port = events_port

    async def execute(
        self,
        saga_def: SagaDefinition,
        ctx: SagaContext,
        step_input: Any = None,
    ) -> list[str]:
        """Execute all saga steps in topological order.

        Returns:
            List of completed step IDs (in completion order).

        Raises:
            Exception: The exception from the first failed step.
            Sets ctx.step_statuses, ctx.step_results, ctx.step_attempts,
            ctx.step_latencies_ms.
        """
        # 1. Compute topology layers from step dependencies.
        deps = {step_id: list(step_def.depends_on) for step_id, step_def in saga_def.steps.items()}
        layers = SagaTopology.compute_layers(deps)
        ctx.topology_layers = layers

        # 2. Build semaphore for layer concurrency control.
        semaphore: asyncio.Semaphore | None = None
        if saga_def.layer_concurrency > 0:
            semaphore = asyncio.Semaphore(saga_def.layer_concurrency)

        completed_step_ids: list[str] = []

        # 3. Execute layer by layer.
        for layer in layers:
            tasks: dict[str, asyncio.Task[None]] = {}

            for step_id in layer:
                step_def = saga_def.steps[step_id]
                task = asyncio.create_task(
                    self._execute_step(
                        saga_def=saga_def,
                        step_def=step_def,
                        bean=saga_def.bean,
                        ctx=ctx,
                        step_input=step_input,
                        completed_step_ids=completed_step_ids,
                        semaphore=semaphore,
                    ),
                    name=f"saga-step-{step_id}",
                )
                tasks[step_id] = task

            # Wait for all tasks in the layer; on first failure cancel the rest.
            first_error: BaseException | None = None
            done, pending = await asyncio.wait(
                tasks.values(),
                return_when=asyncio.FIRST_EXCEPTION,
            )

            # Check completed tasks for exceptions.
            for finished_task in done:
                exc = finished_task.exception()
                if exc is not None and first_error is None:
                    first_error = exc

            if first_error is not None:
                # Cancel remaining tasks in this layer.
                for pending_task in pending:
                    pending_task.cancel()
                # Await cancellation to avoid warnings.
                if pending:
                    await asyncio.wait(pending)
                raise first_error

        return completed_step_ids

    async def _execute_step(
        self,
        saga_def: SagaDefinition,
        step_def: StepDefinition,
        bean: Any,
        ctx: SagaContext,
        step_input: Any,
        completed_step_ids: list[str],
        semaphore: asyncio.Semaphore | None,
    ) -> None:
        """Execute a single step with retry, backoff, jitter, and timeout."""
        step_id = step_def.id
        max_retries = max(step_def.retry, 1)
        backoff_ms = float(step_def.backoff_ms)
        timeout_ms = step_def.timeout_ms
        jitter = step_def.jitter
        jitter_factor = step_def.jitter_factor

        saga_name = saga_def.name
        correlation_id = ctx.correlation_id

        async def _run_attempt() -> Any:
            return await self._step_invoker.invoke_step(step_def, bean, ctx, step_input)

        async def _guarded() -> None:
            nonlocal backoff_ms

            for attempt in range(1, max_retries + 1):
                try:
                    ctx.set_step_status(step_id, StepStatus.RUNNING)
                    start = time.monotonic()

                    if timeout_ms > 0:
                        result = await asyncio.wait_for(
                            _run_attempt(),
                            timeout=timeout_ms / 1000.0,
                        )
                    else:
                        result = await _run_attempt()

                    latency = (time.monotonic() - start) * 1000

                    ctx.set_result(step_id, result)
                    ctx.set_step_status(step_id, StepStatus.DONE)
                    ctx.step_attempts[step_id] = attempt
                    ctx.step_latencies_ms[step_id] = latency

                    if self._events_port is not None:
                        await self._events_port.on_step_success(saga_name, correlation_id, step_id, attempt, latency)

                    completed_step_ids.append(step_id)
                    return  # Success

                except Exception as exc:
                    latency = (time.monotonic() - start) * 1000

                    if attempt < max_retries:
                        delay = backoff_ms / 1000.0
                        if jitter:
                            delay *= 1 + random.uniform(-jitter_factor, jitter_factor)
                        await asyncio.sleep(delay)
                        backoff_ms *= 2  # exponential
                    else:
                        ctx.set_step_status(step_id, StepStatus.FAILED)
                        ctx.step_attempts[step_id] = attempt

                        if self._events_port is not None:
                            await self._events_port.on_step_failed(
                                saga_name,
                                correlation_id,
                                step_id,
                                exc,
                                attempt,
                                latency,
                            )
                        raise

        if semaphore is not None:
            async with semaphore:
                await _guarded()
        else:
            await _guarded()
