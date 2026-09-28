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
"""Workflow engine — public entry point for starting workflows.

An ASYNC workflow (and :meth:`WorkflowEngine.start_async`) runs in a background task started with the
transaction state cleared (:func:`pyfly.data.transaction.detached`): it never joins, nor outlives, the
caller's unit of work.

Started inside a unit of work, the run starts once that unit commits (Spring's ``TransactionSynchronization``):
its ``PENDING`` state is saved in the unit (the SQL provider joins it, the cache provider writes it at the
commit), and the run is started by an after-commit synchronization registered after that save, so every state
the run writes comes after the ``PENDING`` one, with every provider. When the unit rolls back the run never
starts and its state is deleted (a provider outside the unit, such as the in-memory one, wrote it at once).
When its commit outcome is unknown, or it commits while the engine drains, the run is not started either, and
its ``PENDING`` state is kept for the recovery scan. Outside a unit the run starts at once.

:class:`WorkflowRuns` is the lifecycle bean of those runs, in
:data:`~pyfly.kernel.lifecycle.CONSUMER_PHASE`: when the application context stops,
:meth:`WorkflowEngine.drain` refuses new background runs and waits for the runs (and the fire-and-forget
``async_`` steps) in flight before any bean they use is destroyed, so a run a one-shot shell command started
right before shutdown completes. When the context's ``pyfly.context.shutdown-timeout`` cuts the wait short,
the runs still in flight are cancelled (their committed steps are compensated) and awaited.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import time
from typing import Any

from pyfly.data.transaction import (
    CompletionStatus,
    TransactionSynchronizationAdapter,
    current_unit_of_work,
    detached,
)
from pyfly.data.transaction.template import run_shielded
from pyfly.kernel.lifecycle import CONSUMER_PHASE
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
        # Strong references to fire-and-forget run tasks so the event loop does
        # not GC-cancel an ASYNC workflow mid-flight (audit #62).
        self._background_tasks: set[asyncio.Task[Any]] = set()
        self._stopping = False

    # --- background runs ------------------------------------------------

    def accept_background_runs(self) -> None:
        """Start ASYNC workflows again after :meth:`drain`."""
        self._stopping = False

    async def drain(self) -> None:
        """Refuse new background runs, then wait for the ones in flight (and the ``async_`` steps, those the runs
        spawn meanwhile included).

        When the wait is cut short (the caller is cancelled: the context's shutdown timeout), the runs still in
        flight are cancelled and awaited before the cancellation propagates; each compensates what it committed.
        """
        self._stopping = True
        try:
            while pending := self._in_flight():
                await asyncio.wait(pending)
        except asyncio.CancelledError:
            while pending := self._in_flight():
                for task in pending:
                    task.cancel()
                await run_shielded(asyncio.wait(pending))
            raise

    def _in_flight(self) -> set[asyncio.Task[Any]]:
        tasks = set(self._background_tasks)
        tasks.update(getattr(self._executor, "background_tasks", ()))
        return {task for task in tasks if not task.done()}

    @property
    def signals(self) -> SignalService:
        return self._signals

    @property
    def queries(self) -> WorkflowQueryService:
        return self._queries

    async def start(self, workflow_id: str, input: Any = None) -> WorkflowResult:
        """Run a workflow synchronously.  Async-mode workflows fire-and-forget, as :meth:`start_async` does."""
        definition = self._registry.get(workflow_id)
        if definition is None:
            msg = f"unknown workflow '{workflow_id}'"
            raise OrchestrationError(msg)

        if definition.trigger_mode is TriggerMode.ASYNC:
            return await self._start_async(definition, input)

        return await self._run(definition, input)

    async def start_async(self, workflow_id: str, input: Any = None) -> WorkflowResult:
        """Start a workflow fire-and-forget.

        Returns immediately with a :class:`WorkflowResult` carrying the child's
        real ``correlation_id`` (status PENDING); the run continues in the
        background. Use this instead of scheduling :meth:`start` as a bare task
        when you need the correlation id up front (e.g. child workflows).

        Called inside a unit of work (``@transactional``, a ``@transactional`` step), the run starts once
        that unit commits, and never when it rolls back (see the module documentation): do not wait inside
        the unit for the run to progress, and deliver its signals after the commit. A start inside a
        ``Propagation.NESTED`` scope that rolls back to its savepoint still runs when the unit commits: the
        synchronization belongs to the unit, not to the savepoint.
        """
        definition = self._registry.get(workflow_id)
        if definition is None:
            msg = f"unknown workflow '{workflow_id}'"
            raise OrchestrationError(msg)
        return await self._start_async(definition, input)

    async def deliver_signal(self, correlation_id: str, signal: str, payload: Any = None) -> bool:
        return await self._signals.deliver(correlation_id, signal, payload)

    async def query(self, correlation_id: str, query_name: str, *args: Any, **kwargs: Any) -> Any:
        return await self._queries.query(correlation_id, query_name, *args, **kwargs)

    async def list_executions(self, *, status: ExecutionStatus | None = None) -> list[ExecutionState]:
        return await self._persistence.find_all(status=status, pattern=ExecutionPattern.WORKFLOW)

    async def get_execution(self, correlation_id: str) -> ExecutionState | None:
        return await self._persistence.find(correlation_id)

    # --- private --------------------------------------------------------

    @staticmethod
    def _should_suppress(definition: Any, error: BaseException) -> bool:
        """Decide whether a failed workflow should be downgraded to COMPLETED.

        Honors @on_workflow_error(suppress_error, error_types, step_ids):
        suppression requires suppress_error=True and, when given, a matching
        exception class name and failed step id (audit #58).
        """
        if not getattr(definition, "on_error_suppress", False):
            return False
        error_types = getattr(definition, "on_error_types", ())
        if error_types:
            names = {cls.__name__ for cls in type(error).__mro__}
            if not (set(error_types) & names):
                return False
        step_ids = getattr(definition, "on_error_step_ids", ())
        if step_ids:
            failed_step = getattr(error, "step_id", None)
            if failed_step not in step_ids:
                return False
        return True

    async def _start_async(self, definition: Any, input: Any) -> WorkflowResult:
        if self._stopping:
            msg = f"workflow '{definition.id}' not started: the workflow engine is stopping"
            raise OrchestrationError(msg)
        ctx = ExecutionContext(name=definition.id, pattern=ExecutionPattern.WORKFLOW, input=input)
        await ctx.set_status(ExecutionStatus.PENDING)
        # Inside a unit of work the PENDING state is part of it: the SQL provider joins the unit, and the cache
        # provider defers the write to its commit with an after-commit synchronization.
        await self._persistence.save(ExecutionState.from_context(ctx))
        unit = current_unit_of_work()  # the unit that deferred synchronization belongs to, if any
        if unit is not None and not unit.completed:
            # Registered after the save, so it runs after the deferred PENDING write: the run's own states always
            # come later, and a run whose caller rolls back never starts.
            unit.register_synchronization(_StartAfterCommit(self, definition, input, ctx))
        else:
            self._spawn(definition, input, ctx)
        return WorkflowResult(
            workflow_id=definition.id,
            correlation_id=ctx.correlation_id,
            status=ExecutionStatus.PENDING,
            duration_ms=0.0,
        )

    def _spawn(self, definition: Any, input: Any, ctx: ExecutionContext) -> None:
        """Start the background run of *ctx*, whose ``PENDING`` state is saved."""
        # Detached: the run is not part of the caller's unit of work, which may end before it does.
        task = detached(self._run(definition, input, preset_ctx=ctx), name=f"workflow-run-{ctx.correlation_id}")
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

    def _start_committed(self, definition: Any, input: Any, ctx: ExecutionContext) -> None:
        """Start the run of *ctx* now that the unit of work it was started in has committed.

        While the engine drains (the context is stopping), the run is not started: its ``PENDING`` state stays
        for the recovery scan to report, as a run the next process must start again.
        """
        if self._stopping:
            _logger.warning(
                "workflow %s (%s) not started: its unit of work committed while the workflow engine was stopping; "
                "its PENDING state is left for the recovery scan",
                definition.id,
                ctx.correlation_id,
            )
            return
        self._spawn(definition, input, ctx)

    async def _discard(self, definition: Any, ctx: ExecutionContext, status: CompletionStatus) -> None:
        """Forget the run of *ctx*, never started because the unit of work it was started in did not commit.

        On a rollback its ``PENDING`` state is deleted: a provider that joined the unit (SQL) or deferred the
        write to the commit (cache) wrote nothing, and one outside the unit (in-memory, Redis, SQL on another
        datasource) wrote it at once. When the unit's commit outcome is unknown the state is kept, since the
        unit (and the state with it) may have committed.
        """
        if status is CompletionStatus.ROLLED_BACK:
            await self._persistence.delete(ctx.correlation_id)
            return
        _logger.warning(
            "workflow %s (%s) not started: the commit outcome of the unit of work it was started in is unknown; "
            "its PENDING state, if the unit committed, is left for the recovery scan",
            definition.id,
            ctx.correlation_id,
        )

    async def _run(
        self,
        definition: Any,
        input: Any,
        *,
        preset_ctx: ExecutionContext | None = None,
    ) -> WorkflowResult:
        ctx = preset_ctx or ExecutionContext(name=definition.id, pattern=ExecutionPattern.WORKFLOW, input=input)
        started = time.perf_counter()
        await self._signals.register(ctx)
        await self._queries.register(definition, ctx)
        await self._events.on_start(
            name=definition.id, pattern=ExecutionPattern.WORKFLOW, correlation_id=ctx.correlation_id
        )
        await ctx.set_status(ExecutionStatus.RUNNING)
        await self._persistence.save(ExecutionState.from_context(ctx))

        success = False
        cancelled = False
        original_error: BaseException | None = None  # actual error → drives the callback
        try:
            if definition.timeout_ms > 0:
                await asyncio.wait_for(self._executor.execute(definition, ctx), timeout=definition.timeout_ms / 1000.0)
            else:
                await self._executor.execute(definition, ctx)
            await ctx.set_status(ExecutionStatus.COMPLETED)
            success = True
        except StepFailedError as exc:
            original_error = exc
            if self._should_suppress(definition, exc):
                await ctx.set_status(ExecutionStatus.COMPLETED)
                success = True
            else:
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
            original_error = exc
            await ctx.set_status(ExecutionStatus.TIMED_OUT, exc)
        except asyncio.CancelledError:
            # Cancelled (shutdown cut a background run short, the caller gave up): its committed steps are
            # compensated by now, and the persisted state says so instead of RUNNING.
            cancelled = True
            await ctx.set_status(ExecutionStatus.CANCELLED)
            raise
        except Exception as exc:  # noqa: BLE001
            original_error = exc
            if self._should_suppress(definition, exc):
                await ctx.set_status(ExecutionStatus.COMPLETED)
                success = True
            else:
                await ctx.set_status(ExecutionStatus.FAILED, exc)
        finally:
            duration_ms = (time.perf_counter() - started) * 1000.0
            try:
                # The @on_workflow_error handler fires whenever an error
                # occurred, even when suppressed (it is what declares the
                # suppression); on_complete fires only on a clean run.
                if original_error is not None and definition.on_error is not None:
                    cb_result = definition.on_error(definition.bean, ctx, original_error)
                    if inspect.isawaitable(cb_result):
                        await cb_result
                elif success and definition.on_complete is not None:
                    cb_result = definition.on_complete(definition.bean, ctx)
                    if inspect.isawaitable(cb_result):
                        await cb_result
            except Exception as cb_exc:  # noqa: BLE001
                _logger.warning("workflow callback raised: %s", cb_exc)
            # The final state is recorded to completion even when the run is cancelled (again) meanwhile, as a
            # cancel scope does at every await; a cancellation that arrives then is raised once it is recorded.
            _result, finish_error, finish_cancelled = await run_shielded(
                self._finish(definition, ctx, success=success, duration_ms=duration_ms)
            )
            if finish_error is not None:
                if not (cancelled or finish_cancelled):
                    raise finish_error
                _logger.warning(
                    "recording the final state of cancelled workflow %s raised: %s", definition.id, finish_error
                )
            if finish_cancelled and not cancelled:
                raise asyncio.CancelledError

        return WorkflowResult(
            workflow_id=definition.id,
            correlation_id=ctx.correlation_id,
            status=ctx.status,
            duration_ms=(time.perf_counter() - started) * 1000.0,
            step_results={sid: rec.result for sid, rec in ctx.get_all_steps().items()},
            variables=ctx.get_all_variables(),
            error=ctx.error,
        )

    async def _finish(self, definition: Any, ctx: ExecutionContext, *, success: bool, duration_ms: float) -> None:
        """Persist a run's final state, emit ``on_completed`` and stop routing its signals and queries."""
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


class _StartAfterCommit(TransactionSynchronizationAdapter):
    """Starts a background run once the unit of work it was started in commits (see the module documentation).

    The unit runs its after-commit callbacks in registration order, so the ``PENDING`` write a cache provider
    deferred to the commit (registered before this) is done when the run starts.
    """

    def __init__(self, engine: WorkflowEngine, definition: Any, input: Any, ctx: ExecutionContext) -> None:
        self._engine = engine
        self._definition = definition
        self._input = input
        self._ctx = ctx

    async def after_commit(self) -> None:
        """Start the run."""
        self._engine._start_committed(self._definition, self._input, self._ctx)

    async def after_completion(self, status: CompletionStatus) -> None:
        """Forget the run when the unit did not commit."""
        if status is not CompletionStatus.COMMITTED:
            await self._engine._discard(self._definition, self._ctx, status)


class WorkflowRuns:
    """The lifecycle of a :class:`WorkflowEngine`'s background runs (see the module documentation).

    It stops before any ``@pre_destroy`` (:data:`~pyfly.kernel.lifecycle.CONSUMER_PHASE`), draining the ASYNC
    workflow runs in flight while the beans their steps use still work. Starting and stopping it twice is
    harmless.
    """

    phase = CONSUMER_PHASE

    def __init__(self, engine: WorkflowEngine) -> None:
        self._engine = engine

    async def start(self) -> None:
        """Let the engine start background runs."""
        self._engine.accept_background_runs()

    async def stop(self) -> None:
        """Refuse new background runs and wait for the ones in flight."""
        await self._engine.drain()
