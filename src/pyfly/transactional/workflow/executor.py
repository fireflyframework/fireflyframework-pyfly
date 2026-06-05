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
"""Workflow executor — orchestrates step execution layer-by-layer.

Handles step pre-conditions (signal waits, timer waits, child workflows),
delegates the actual method call to the shared :class:`StepInvoker`, and
emits lifecycle events through :class:`OrchestrationEvents`.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import time
from typing import Any

from pyfly.transactional.core.argument import ArgumentResolver
from pyfly.transactional.core.backpressure import (
    AdaptiveBackpressureStrategy,
    BackpressureStrategy,
)
from pyfly.transactional.core.context import ExecutionContext
from pyfly.transactional.core.events import LoggerOrchestrationEvents, OrchestrationEvents
from pyfly.transactional.core.exceptions import StepFailedError
from pyfly.transactional.core.step_invoker import StepInvoker
from pyfly.transactional.core.topology import TopologyBuilder
from pyfly.transactional.workflow.child_workflow_service import ChildWorkflowService
from pyfly.transactional.workflow.condition import ConditionError, evaluate_condition
from pyfly.transactional.workflow.definition import (
    WorkflowDefinition,
    WorkflowStepDefinition,
)
from pyfly.transactional.workflow.signal_service import SignalService
from pyfly.transactional.workflow.timer_service import TimerService

_logger = logging.getLogger(__name__)


class WorkflowExecutor:
    """Layer-by-layer execution engine for a single workflow run."""

    def __init__(
        self,
        *,
        step_invoker: StepInvoker | None = None,
        signal_service: SignalService | None = None,
        timer_service: TimerService | None = None,
        child_service: ChildWorkflowService | None = None,
        events: OrchestrationEvents | None = None,
        backpressure: BackpressureStrategy | None = None,
    ) -> None:
        self._invoker = step_invoker or StepInvoker(ArgumentResolver())
        # Dedicated resolver for compensation calls (mirrors the invoker's own
        # resolver). Compensation methods are invoked directly — without retry,
        # timeout, or step-record mutation — so they cannot clobber the
        # already-recorded success of the step they roll back.
        self._compensation_resolver = ArgumentResolver()
        self._signals = signal_service or SignalService()
        self._timers = timer_service or TimerService()
        self._children = child_service or ChildWorkflowService()
        self._events = events or LoggerOrchestrationEvents()
        self._backpressure = backpressure or AdaptiveBackpressureStrategy()
        # Strong references to fire-and-forget @workflow_step(async_=True) tasks
        # so the event loop does not GC-cancel them mid-flight (audit #60/#62).
        self._async_step_tasks: set[asyncio.Task[Any]] = set()

    async def execute(self, definition: WorkflowDefinition, ctx: ExecutionContext) -> None:
        """Run all steps respecting their dependency graph.

        On any step failure, already-completed *compensatable* steps are rolled
        back (in reverse execution order) before the original error propagates.
        """
        layers = TopologyBuilder.build_layers(definition.graph())
        try:
            for layer in layers:
                steps = [definition.steps[sid] for sid in layer]
                await self._execute_layer(definition, ctx, steps)
        except BaseException as exc:
            await self._compensate(definition, ctx, layers, exc)
            raise

    async def _compensate(
        self,
        definition: WorkflowDefinition,
        ctx: ExecutionContext,
        layers: list[list[str]],
        error: BaseException,
    ) -> None:
        """Roll back completed compensatable steps in reverse execution order.

        The set of completed steps is derived from the context (a step may have
        succeeded concurrently with the one that failed), and ordered using the
        topology layers so compensation runs newest-first. Compensation errors
        are recorded and surfaced via events but never mask the original
        failure that triggered the rollback.
        """
        completed_ids: list[str] = []
        for layer in layers:
            for step_id in layer:
                step = definition.steps.get(step_id)
                if step is None or not step.compensatable or step.compensation_method is None:
                    continue
                if ctx.is_step_done(step_id):
                    completed_ids.append(step_id)

        if not completed_ids:
            return

        await self._events.on_compensation_started(name=definition.id, correlation_id=ctx.correlation_id)

        for step_id in reversed(completed_ids):
            step = definition.steps[step_id]
            method = step.compensation_method
            assert method is not None  # filtered above
            comp_error: BaseException | None = None
            comp_result: Any = None
            try:
                kwargs = self._compensation_resolver.resolve(
                    method,
                    ctx,
                    compensation_error=error,
                    skip_first=definition.bean is not None,
                )
                call = method(definition.bean, **kwargs)
                if inspect.isawaitable(call):
                    comp_result = await call
                else:
                    comp_result = call
            except Exception as exc:  # noqa: BLE001
                comp_error = exc
                _logger.warning(
                    "compensation for %s.%s failed: %s",
                    definition.id,
                    step_id,
                    exc,
                )
            await ctx.record_step_compensated(step_id, comp_result, comp_error)
            await self._events.on_step_compensated(
                name=definition.id,
                correlation_id=ctx.correlation_id,
                step_id=step_id,
                error=comp_error,
            )

    async def _execute_layer(
        self,
        definition: WorkflowDefinition,
        ctx: ExecutionContext,
        steps: list[WorkflowStepDefinition],
    ) -> None:
        async def run_one(step: WorkflowStepDefinition) -> None:
            await self._run_step(definition, ctx, step)

        await self._backpressure.apply(steps, run_one)

    async def _run_step(
        self,
        definition: WorkflowDefinition,
        ctx: ExecutionContext,
        step: WorkflowStepDefinition,
    ) -> None:
        await self._events.on_step_started(name=definition.id, correlation_id=ctx.correlation_id, step_id=step.id)

        # Evaluate the step condition (SpEL substitute). A condition that
        # resolves to False skips the step entirely (audit #59).
        if step.condition and not self._evaluate_condition(step.condition, ctx):
            await ctx.record_step_skipped(step.id)
            await self._events.on_step_skipped(name=definition.id, correlation_id=ctx.correlation_id, step_id=step.id)
            return

        # Handle pre-step waits and child invocations.
        if step.wait_for_timer_ms > 0:
            timer_id = step.wait_for_timer_id or step.id
            await self._events.on_workflow_suspended(
                name=definition.id, correlation_id=ctx.correlation_id, reason=f"timer:{timer_id}"
            )
            await self._timers.sleep_ms(step.wait_for_timer_ms)
            await self._events.on_timer_fired(name=definition.id, correlation_id=ctx.correlation_id, timer_id=timer_id)
            await self._events.on_workflow_resumed(name=definition.id, correlation_id=ctx.correlation_id)

        if step.wait_for_signal:
            await self._events.on_workflow_suspended(
                name=definition.id, correlation_id=ctx.correlation_id, reason=f"signal:{step.wait_for_signal}"
            )
            payload = await ctx.wait_for_signal(step.wait_for_signal, step.wait_for_signal_timeout_ms)
            await ctx.set_variable(f"signal:{step.wait_for_signal}", payload)
            await self._events.on_signal_delivered(
                name=definition.id, correlation_id=ctx.correlation_id, signal=step.wait_for_signal
            )
            await self._events.on_workflow_resumed(name=definition.id, correlation_id=ctx.correlation_id)

        if step.wait_for_all or step.wait_for_all_timers:
            # Wait for every signal AND every timer to complete.
            awaitables = [ctx.wait_for_signal(sig, step.wait_for_all_timeout_ms) for sig in step.wait_for_all]
            awaitables += [self._timers.sleep_ms(delay) for delay in step.wait_for_all_timers]
            if awaitables:
                await asyncio.gather(*awaitables)

        if step.wait_for_any or step.wait_for_any_timers:
            signal_names = list(step.wait_for_any)
            tasks = [
                asyncio.create_task(ctx.wait_for_signal(sig, step.wait_for_any_timeout_ms)) for sig in signal_names
            ]
            # Timers race alongside signals; whichever fires first wins.
            tasks += [asyncio.create_task(self._timers.sleep_ms(delay)) for delay in step.wait_for_any_timers]
            done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for t in pending:
                t.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            # Retrieve the winning future: a TimeoutError must surface (not be
            # swallowed). When a signal wins its payload is captured as a
            # variable; when a timer wins there is nothing to record (audit
            # #55/#63).
            winner = next(iter(done))
            winner_idx = tasks.index(winner)
            payload = winner.result()  # re-raises asyncio.TimeoutError on timeout
            if winner_idx < len(signal_names):
                await ctx.set_variable(f"signal:{signal_names[winner_idx]}", payload)
                await self._events.on_signal_delivered(
                    name=definition.id, correlation_id=ctx.correlation_id, signal=signal_names[winner_idx]
                )

        if step.child_workflow_id:
            await self._events.on_child_workflow_started(
                parent=definition.id,
                correlation_id=ctx.correlation_id,
                child_workflow=step.child_workflow_id,
                child_correlation="<pending>",
            )
            child_result: Any
            try:
                child_result = await self._children.start(
                    step.child_workflow_id,
                    input=ctx.input,
                    wait_for_completion=step.child_wait_for_completion,
                    timeout_ms=step.child_timeout_ms,
                )
            except Exception:
                await self._events.on_child_workflow_completed(
                    parent=definition.id,
                    correlation_id=ctx.correlation_id,
                    child_workflow=step.child_workflow_id,
                    success=False,
                )
                raise
            await self._events.on_child_workflow_completed(
                parent=definition.id,
                correlation_id=ctx.correlation_id,
                child_workflow=step.child_workflow_id,
                success=True,
            )
            await ctx.record_step_success(step.id, child_result, latency_ms=0.0)
            return

        # Async steps fire-and-forget: schedule the invocation and let the
        # workflow proceed without blocking the layer (audit #60).
        if step.async_:
            task = asyncio.create_task(self._run_step_body(definition, ctx, step))
            self._async_step_tasks.add(task)
            task.add_done_callback(self._async_step_tasks.discard)
            return

        await self._run_step_body(definition, ctx, step)

    async def _run_step_body(
        self,
        definition: WorkflowDefinition,
        ctx: ExecutionContext,
        step: WorkflowStepDefinition,
    ) -> None:
        # Regular method invocation.
        started = time.perf_counter()
        try:
            result = await self._invoker.invoke(
                bean=definition.bean,
                method=step.method,
                step_id=step.id,
                ctx=ctx,
                retry_policy=step.to_retry_policy(),
            )
            elapsed = (time.perf_counter() - started) * 1000.0
            step_record = ctx.get_step(step.id)
            await self._events.on_step_success(
                name=definition.id,
                correlation_id=ctx.correlation_id,
                step_id=step.id,
                attempts=step_record.attempts if step_record is not None else 1,
                latency_ms=elapsed,
            )
            on_step_cb = definition.on_step_callbacks.get(step.id)
            if on_step_cb is not None:
                cb_result = on_step_cb(definition.bean, ctx, result)
                if hasattr(cb_result, "__await__"):
                    await cb_result
        except StepFailedError as exc:
            elapsed = (time.perf_counter() - started) * 1000.0
            await self._events.on_step_failed(
                name=definition.id,
                correlation_id=ctx.correlation_id,
                step_id=step.id,
                error=exc,
                attempts=exc.attempts,
                latency_ms=elapsed,
            )
            raise

    @staticmethod
    def _evaluate_condition(expression: str, ctx: ExecutionContext) -> bool:
        """Evaluate a step condition against the workflow facts.

        A malformed/unknown-name condition fails closed (treated as False, the
        step is skipped) rather than aborting the workflow.
        """
        namespace = {
            "results": {sid: rec.result for sid, rec in ctx.get_all_steps().items()},
            "variables": ctx.get_all_variables(),
            "headers": dict(ctx.headers),
            "input": ctx.input,
        }
        try:
            return evaluate_condition(expression, namespace)
        except ConditionError:
            _logger.warning(
                "workflow_step_condition_invalid",
                extra={"correlation_id": ctx.correlation_id, "condition": expression},
            )
            return False
