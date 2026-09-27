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
"""Saga execution orchestrator — topological layer-based parallel execution.

Each step runs in a task of its own, started with the transaction state cleared
(:func:`pyfly.data.transaction.detached`): a step's ``@transactional`` work is its own unit of work, never
its caller's, and a step commits (and is compensated) on its own. The orchestrator owns those tasks
(structured concurrency):

- **A step that fails.** Once a step of a layer has failed (after its retries), the steps of the layer that
  have not started (they wait for :pyattr:`SagaDefinition.layer_concurrency`) never start, and the steps
  already running are awaited, not cancelled: a step cancelled while its ``COMMIT`` is in flight would commit
  without the saga knowing. A running step starts no new attempt, and one sleeping in its retry backoff wakes
  up and fails with its last error. The failure is raised once every step of the layer has settled.
- **A step that ends cancelled on its own.** A step whose body raises ``CancelledError`` although nothing
  cancelled its task (it awaited a future something else cancelled, such as a reply future a client library
  gave up on) failed: the attempt raises an :class:`~pyfly.transactional.core.exceptions.OrchestrationError`
  chained from that ``CancelledError``, and is retried, compensated and reported like any other failure. It
  is never taken for a success, nor for a cancellation of the saga.
- **The saga is cancelled** (a caller's timeout, a client disconnect, shutdown). Every step task is
  cancelled and awaited before the cancellation propagates, so none outlives the saga and commits afterwards.
  A step cancelled while its ``COMMIT`` was in flight still commits (commits are shielded).
- **Knowing what committed.** Each attempt runs inside :func:`pyfly.data.transaction.track_commits`. A step
  whose attempt failed, timed out or was cancelled after a unit of work of it committed (or with a commit
  whose outcome is unknown) is recorded in :pyattr:`SagaContext.committed_steps`, so the engine compensates
  it like a completed step, and it is never retried: a retry would apply its writes twice. An attempt that
  committed nothing is retried as before.

The step timeout (``timeout_ms``) bounds each attempt; a timeout that fires while the attempt's ``COMMIT``
is in flight lets the commit finish (it is shielded) and then fails the step without a retry. Only units of
work the framework manages are seen (``@transactional``, repositories, ``TransactionTemplate``): a step that
commits a session it opened itself from the ``async_sessionmaker`` is not.
"""

from __future__ import annotations

import asyncio
import contextlib
import random
import time
from collections.abc import Collection
from typing import Any

from pyfly.data.transaction import detached, track_commits
from pyfly.data.transaction.template import run_shielded
from pyfly.transactional.core.exceptions import OrchestrationError
from pyfly.transactional.saga.core.context import SagaContext
from pyfly.transactional.saga.engine.step_invoker import StepInvoker
from pyfly.transactional.saga.engine.topology import SagaTopology
from pyfly.transactional.saga.registry.saga_definition import SagaDefinition
from pyfly.transactional.saga.registry.step_definition import StepDefinition
from pyfly.transactional.shared.ports.outbound import TransactionalEventsPort
from pyfly.transactional.shared.types import StepStatus


class _Layer:
    """What the steps of one layer share: which have started, and the first failure."""

    __slots__ = ("failed", "failure", "started")

    def __init__(self) -> None:
        self.started: set[str] = set()
        self.failure: BaseException | None = None
        self.failed = asyncio.Event()  # set with the first failure: it ends the retry backoffs of the layer

    def fail(self, failure: BaseException) -> None:
        """Record *failure* unless a step failed first."""
        if self.failure is None:
            self.failure = failure
            self.failed.set()

    async def backoff(self, delay: float) -> None:
        """Sleep *delay* seconds before a retry, or less when a step of the layer fails meanwhile."""
        with contextlib.suppress(TimeoutError):
            async with asyncio.timeout(delay):
                await self.failed.wait()


def _cancellation_requested() -> bool:
    """Whether the running task was asked to cancel (by the orchestrator, or by the saga's caller)."""
    task = asyncio.current_task()
    return task is None or task.cancelling() > 0


def _as_step_failure(step_id: str, exc: BaseException) -> BaseException:
    """*exc*, unless it is a ``CancelledError`` nobody requested of the step's task: the step's own body ended
    cancelled, which is a failure of the step, returned as an :class:`OrchestrationError` to raise from it."""
    if not isinstance(exc, asyncio.CancelledError) or _cancellation_requested():
        return exc
    return OrchestrationError(
        f"step '{step_id}' ended cancelled although nothing cancelled it (it awaited something that was "
        "cancelled); the step failed"
    )


async def _settle_cancelled(tasks: Collection[asyncio.Task[Any]]) -> None:
    """Cancel *tasks* and wait until every one has ended, whatever cancellations the caller gets meanwhile."""
    for task in tasks:
        task.cancel()
    await run_shielded(asyncio.wait(tasks))
    _retrieve(tasks)


def _retrieve(tasks: Collection[asyncio.Task[Any]]) -> None:
    """Mark the outcome of every finished task as seen (the layer reports its first failure itself)."""
    for task in tasks:
        if task.done() and not task.cancelled():
            task.exception()


class SagaExecutionOrchestrator:
    """Executes saga steps in topological order with retry/timeout (see the module documentation).

    Steps within a single dependency layer run concurrently (subject to an
    optional :pyattr:`SagaDefinition.layer_concurrency` semaphore).  Each step
    is individually wrapped in a retry loop with exponential backoff, optional
    jitter, and a per-attempt timeout.
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
            Exception: The exception from the first failed step, once every step of its layer has settled.
            asyncio.CancelledError: The saga was cancelled; every step task has ended by then.
            Sets ctx.step_statuses, ctx.step_results, ctx.step_attempts,
            ctx.step_latencies_ms and ctx.committed_steps.
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
            await self._run_layer(saga_def, layer, ctx, step_input, completed_step_ids, semaphore)

        return completed_step_ids

    async def _run_layer(
        self,
        saga_def: SagaDefinition,
        layer: list[str],
        ctx: SagaContext,
        step_input: Any,
        completed_step_ids: list[str],
        semaphore: asyncio.Semaphore | None,
    ) -> None:
        """Run the steps of one layer, each in a detached task, and settle them all before returning."""
        state = _Layer()
        tasks: dict[str, asyncio.Task[None]] = {}
        for step_id in layer:
            tasks[step_id] = detached(
                self._execute_step(
                    saga_def=saga_def,
                    step_def=saga_def.steps[step_id],
                    bean=saga_def.bean,
                    ctx=ctx,
                    step_input=step_input,
                    completed_step_ids=completed_step_ids,
                    semaphore=semaphore,
                    layer=state,
                ),
                name=f"saga-step-{step_id}",
            )
        try:
            await asyncio.wait(tasks.values(), return_when=asyncio.FIRST_EXCEPTION)
            if state.failure is not None:
                # A step failed: the steps that have not started never do, the running ones are awaited.
                for step_id, task in tasks.items():
                    if step_id not in state.started:
                        task.cancel()
                await asyncio.wait(tasks.values())
        except asyncio.CancelledError:
            await _settle_cancelled(tasks.values())
            raise
        _retrieve(tasks.values())
        if state.failure is not None:
            raise state.failure
        for step_id, task in tasks.items():
            if task.cancelled():
                # Nothing here cancelled it (the step cancelled its own task): it failed, it did not succeed.
                raise OrchestrationError(f"step '{step_id}' was cancelled although the saga was not; the step failed")

    async def _execute_step(
        self,
        saga_def: SagaDefinition,
        step_def: StepDefinition,
        bean: Any,
        ctx: SagaContext,
        step_input: Any,
        completed_step_ids: list[str],
        semaphore: asyncio.Semaphore | None,
        layer: _Layer,
    ) -> None:
        """Execute a single step (once its layer lets it start) with retry, backoff, jitter, and timeout."""
        async with semaphore if semaphore is not None else contextlib.nullcontext():
            if layer.failure is not None:
                return  # a sibling failed while this step waited for its turn: it never starts
            layer.started.add(step_def.id)
            try:
                await self._attempts(saga_def, step_def, bean, ctx, step_input, completed_step_ids, layer)
            except Exception as exc:
                layer.fail(exc)
                raise
            except asyncio.CancelledError as cancelled:
                # Past its attempts (an events port call, say), a cancellation nobody requested fails the layer too,
                # as any exception there does.
                failure = _as_step_failure(step_def.id, cancelled)
                if failure is cancelled:
                    raise
                layer.fail(failure)
                raise failure from cancelled

    async def _attempts(
        self,
        saga_def: SagaDefinition,
        step_def: StepDefinition,
        bean: Any,
        ctx: SagaContext,
        step_input: Any,
        completed_step_ids: list[str],
        layer: _Layer,
    ) -> None:
        step_id = step_def.id
        max_retries = max(step_def.retry, 1)
        backoff_ms = float(step_def.backoff_ms)
        timeout_ms = step_def.timeout_ms
        saga_name = saga_def.name
        correlation_id = ctx.correlation_id

        for attempt in range(1, max_retries + 1):
            ctx.set_step_status(step_id, StepStatus.RUNNING)
            start = time.monotonic()
            with track_commits() as commits:
                try:
                    if timeout_ms > 0:
                        async with asyncio.timeout(timeout_ms / 1000.0):
                            result = await self._step_invoker.invoke_step(step_def, bean, ctx, step_input)
                    else:
                        result = await self._step_invoker.invoke_step(step_def, bean, ctx, step_input)
                except BaseException as caught:
                    exc = _as_step_failure(step_id, caught)  # the step's body ended cancelled on its own
                    latency = (time.monotonic() - start) * 1000
                    committed = commits.may_have_committed
                    retry = (
                        isinstance(exc, Exception)
                        and not committed
                        and attempt < max_retries
                        and layer.failure is None  # a sibling failed: the saga is failing, no new attempt
                    )
                    if retry:
                        delay = backoff_ms / 1000.0
                        if step_def.jitter:
                            delay *= 1 + random.uniform(-step_def.jitter_factor, step_def.jitter_factor)
                        await layer.backoff(delay)
                        backoff_ms *= 2  # exponential
                        if layer.failure is None:
                            continue
                        # A sibling failed during the backoff: no new attempt, the step fails with this error.
                    ctx.set_step_status(step_id, StepStatus.FAILED)
                    ctx.step_attempts[step_id] = attempt
                    if committed:
                        ctx.note_committed(step_id)  # compensated like a completed step, never retried
                    if isinstance(exc, Exception) and self._events_port is not None:
                        await self._events_port.on_step_failed(
                            saga_name, correlation_id, step_id, exc, attempt, latency
                        )
                    if exc is not caught:
                        raise exc from caught
                    raise

            latency = (time.monotonic() - start) * 1000
            ctx.set_result(step_id, result)
            ctx.set_step_status(step_id, StepStatus.DONE)
            ctx.step_attempts[step_id] = attempt
            ctx.step_latencies_ms[step_id] = latency
            ctx.note_committed(step_id)
            completed_step_ids.append(step_id)
            if self._events_port is not None:
                await self._events_port.on_step_success(saga_name, correlation_id, step_id, attempt, latency)
            return
