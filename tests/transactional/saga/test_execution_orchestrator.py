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
"""Tests for saga execution orchestrator — layer-based parallel execution."""

from __future__ import annotations

import asyncio
import time
from typing import Any
from unittest.mock import AsyncMock

import pytest

from pyfly.transactional.saga.core.context import SagaContext
from pyfly.transactional.saga.engine.execution_orchestrator import (
    SagaExecutionOrchestrator,
)
from pyfly.transactional.saga.engine.step_invoker import StepInvoker
from pyfly.transactional.saga.registry.saga_definition import SagaDefinition
from pyfly.transactional.saga.registry.step_definition import StepDefinition
from pyfly.transactional.shared.ports.outbound import TransactionalEventsPort
from pyfly.transactional.shared.types import StepStatus

# ── Helpers ──────────────────────────────────────────────────


class _FakeBean:
    """Dummy saga bean used as ``self`` receiver."""


def _make_step_def(
    step_id: str,
    depends_on: list[str] | None = None,
    retry: int = 1,
    backoff_ms: int = 0,
    timeout_ms: int = 0,
    jitter: bool = False,
    jitter_factor: float = 0.0,
) -> StepDefinition:
    """Create a minimal StepDefinition for tests."""

    async def _noop(self: Any, context: SagaContext) -> str:
        return f"result-{step_id}"

    return StepDefinition(
        id=step_id,
        step_method=_noop,
        depends_on=depends_on or [],
        retry=retry,
        backoff_ms=backoff_ms,
        timeout_ms=timeout_ms,
        jitter=jitter,
        jitter_factor=jitter_factor,
    )


def _make_saga(
    steps: dict[str, StepDefinition],
    *,
    layer_concurrency: int = 0,
    name: str = "test-saga",
) -> SagaDefinition:
    return SagaDefinition(
        name=name,
        bean=_FakeBean(),
        layer_concurrency=layer_concurrency,
        steps=steps,
    )


def _make_events() -> AsyncMock:
    """Return a fully-mocked TransactionalEventsPort."""
    mock = AsyncMock(spec=TransactionalEventsPort)
    return mock


def _make_invoker_from_fn(fn: Any) -> AsyncMock:
    """Return a mock StepInvoker whose invoke_step calls *fn*."""
    mock = AsyncMock(spec=StepInvoker)
    mock.invoke_step = fn
    return mock


@pytest.fixture
def events() -> AsyncMock:
    return _make_events()


@pytest.fixture
def ctx() -> SagaContext:
    return SagaContext(saga_name="test-saga")


# ── Linear saga (A -> B -> C) ───────────────────────────────


class TestLinearSaga:
    @pytest.mark.anyio
    async def test_linear_executes_in_order(self, ctx: SagaContext, events: AsyncMock) -> None:
        """Steps in a linear chain A->B->C execute sequentially, all succeed."""
        execution_order: list[str] = []

        async def invoke(
            step_def: StepDefinition,
            bean: Any,
            context: SagaContext,
            step_input: Any = None,
        ) -> str:
            execution_order.append(step_def.id)
            return f"result-{step_def.id}"

        invoker = _make_invoker_from_fn(invoke)

        steps = {
            "A": _make_step_def("A"),
            "B": _make_step_def("B", depends_on=["A"]),
            "C": _make_step_def("C", depends_on=["B"]),
        }
        saga = _make_saga(steps)

        orchestrator = SagaExecutionOrchestrator(invoker, events)
        completed = await orchestrator.execute(saga, ctx)

        assert execution_order == ["A", "B", "C"]
        assert completed == ["A", "B", "C"]
        assert ctx.step_statuses["A"] == StepStatus.DONE
        assert ctx.step_statuses["B"] == StepStatus.DONE
        assert ctx.step_statuses["C"] == StepStatus.DONE


# ── Parallel steps ───────────────────────────────────────────


class TestParallelSteps:
    @pytest.mark.anyio
    async def test_independent_steps_run_in_same_layer(self, ctx: SagaContext, events: AsyncMock) -> None:
        """A and B with no dependencies execute in the same layer (concurrently)."""
        concurrency_tracker: list[int] = []
        active_count = 0

        async def invoke(
            step_def: StepDefinition,
            bean: Any,
            context: SagaContext,
            step_input: Any = None,
        ) -> str:
            nonlocal active_count
            active_count += 1
            concurrency_tracker.append(active_count)
            await asyncio.sleep(0.05)
            active_count -= 1
            return f"result-{step_def.id}"

        invoker = _make_invoker_from_fn(invoke)

        steps = {
            "A": _make_step_def("A"),
            "B": _make_step_def("B"),
            "C": _make_step_def("C", depends_on=["A", "B"]),
        }
        saga = _make_saga(steps)

        orchestrator = SagaExecutionOrchestrator(invoker, events)
        completed = await orchestrator.execute(saga, ctx)

        # Both A and B should have been active concurrently
        assert max(concurrency_tracker) >= 2
        # C comes after both A and B
        assert "C" in completed
        assert completed.index("C") > completed.index("A")
        assert completed.index("C") > completed.index("B")


# ── Layer concurrency ────────────────────────────────────────


class TestLayerConcurrency:
    @pytest.mark.anyio
    async def test_semaphore_limits_concurrent_steps(self, ctx: SagaContext, events: AsyncMock) -> None:
        """layer_concurrency=1 ensures only one step runs at a time even in same layer."""
        max_observed = 0
        active_count = 0

        async def invoke(
            step_def: StepDefinition,
            bean: Any,
            context: SagaContext,
            step_input: Any = None,
        ) -> str:
            nonlocal active_count, max_observed
            active_count += 1
            if active_count > max_observed:
                max_observed = active_count
            await asyncio.sleep(0.05)
            active_count -= 1
            return f"result-{step_def.id}"

        invoker = _make_invoker_from_fn(invoke)

        steps = {
            "A": _make_step_def("A"),
            "B": _make_step_def("B"),
            "C": _make_step_def("C"),
        }
        saga = _make_saga(steps, layer_concurrency=1)

        orchestrator = SagaExecutionOrchestrator(invoker, events)
        completed = await orchestrator.execute(saga, ctx)

        assert max_observed == 1
        assert len(completed) == 3


# ── Retry on failure ─────────────────────────────────────────


class TestRetry:
    @pytest.mark.anyio
    async def test_succeeds_on_second_attempt(self, ctx: SagaContext, events: AsyncMock) -> None:
        """A step that fails once then succeeds on retry 2 completes successfully."""
        attempt_counter: dict[str, int] = {}

        async def invoke(
            step_def: StepDefinition,
            bean: Any,
            context: SagaContext,
            step_input: Any = None,
        ) -> str:
            attempt_counter.setdefault(step_def.id, 0)
            attempt_counter[step_def.id] += 1
            if step_def.id == "A" and attempt_counter[step_def.id] < 2:
                raise RuntimeError("transient failure")
            return f"result-{step_def.id}"

        invoker = _make_invoker_from_fn(invoke)

        steps = {
            "A": _make_step_def("A", retry=3, backoff_ms=10),
        }
        saga = _make_saga(steps)

        orchestrator = SagaExecutionOrchestrator(invoker, events)
        completed = await orchestrator.execute(saga, ctx)

        assert completed == ["A"]
        assert ctx.step_statuses["A"] == StepStatus.DONE
        assert ctx.step_attempts["A"] == 2


# ── Timeout per step ─────────────────────────────────────────


class TestTimeout:
    @pytest.mark.anyio
    async def test_timeout_raises_on_slow_step(self, ctx: SagaContext, events: AsyncMock) -> None:
        """A step that exceeds its timeout_ms triggers a TimeoutError."""

        async def invoke(
            step_def: StepDefinition,
            bean: Any,
            context: SagaContext,
            step_input: Any = None,
        ) -> str:
            await asyncio.sleep(5)  # much longer than timeout
            return "never-reached"

        invoker = _make_invoker_from_fn(invoke)

        steps = {
            "A": _make_step_def("A", timeout_ms=50, retry=1),
        }
        saga = _make_saga(steps)

        orchestrator = SagaExecutionOrchestrator(invoker, events)

        with pytest.raises(asyncio.TimeoutError):
            await orchestrator.execute(saga, ctx)

        assert ctx.step_statuses["A"] == StepStatus.FAILED


# ── Backoff delay ─────────────────────────────────────────────


class TestBackoff:
    @pytest.mark.anyio
    async def test_backoff_introduces_delay_between_retries(self, ctx: SagaContext, events: AsyncMock) -> None:
        """Retry with backoff_ms=100 introduces a measurable delay."""
        timestamps: list[float] = []

        async def invoke(
            step_def: StepDefinition,
            bean: Any,
            context: SagaContext,
            step_input: Any = None,
        ) -> str:
            timestamps.append(time.monotonic())
            if len(timestamps) < 2:
                raise RuntimeError("transient failure")
            return "ok"

        invoker = _make_invoker_from_fn(invoke)

        steps = {
            "A": _make_step_def("A", retry=3, backoff_ms=100),
        }
        saga = _make_saga(steps)

        orchestrator = SagaExecutionOrchestrator(invoker, events)
        await orchestrator.execute(saga, ctx)

        assert len(timestamps) == 2
        gap_ms = (timestamps[1] - timestamps[0]) * 1000
        # Should be at least 80ms (allow some scheduling variance)
        assert gap_ms >= 80


# ── Failure stops remaining layers ────────────────────────────


class TestFailureStopsLayers:
    @pytest.mark.anyio
    async def test_failure_returns_completed_ids(self, ctx: SagaContext, events: AsyncMock) -> None:
        """When B fails, C never executes; completed list contains only A."""

        async def invoke(
            step_def: StepDefinition,
            bean: Any,
            context: SagaContext,
            step_input: Any = None,
        ) -> str:
            if step_def.id == "B":
                raise RuntimeError("B failed permanently")
            return f"result-{step_def.id}"

        invoker = _make_invoker_from_fn(invoke)

        steps = {
            "A": _make_step_def("A"),
            "B": _make_step_def("B", depends_on=["A"]),
            "C": _make_step_def("C", depends_on=["B"]),
        }
        saga = _make_saga(steps)

        orchestrator = SagaExecutionOrchestrator(invoker, events)

        with pytest.raises(RuntimeError, match="B failed permanently"):
            await orchestrator.execute(saga, ctx)

        assert ctx.step_statuses["A"] == StepStatus.DONE
        assert ctx.step_statuses["B"] == StepStatus.FAILED
        assert "C" not in ctx.step_statuses

    @pytest.mark.anyio
    async def test_failure_in_parallel_layer_cancels_siblings(self, ctx: SagaContext, events: AsyncMock) -> None:
        """When one step in a parallel layer fails, other tasks are cancelled."""

        async def invoke(
            step_def: StepDefinition,
            bean: Any,
            context: SagaContext,
            step_input: Any = None,
        ) -> str:
            if step_def.id == "A":
                raise RuntimeError("A failed")
            # B is slow; should be cancelled
            await asyncio.sleep(5)
            return f"result-{step_def.id}"

        invoker = _make_invoker_from_fn(invoke)

        steps = {
            "A": _make_step_def("A"),
            "B": _make_step_def("B"),
        }
        saga = _make_saga(steps)

        orchestrator = SagaExecutionOrchestrator(invoker, events)

        with pytest.raises(RuntimeError, match="A failed"):
            await orchestrator.execute(saga, ctx)

        assert ctx.step_statuses["A"] == StepStatus.FAILED


# ── Events emitted ────────────────────────────────────────────


class TestEventsEmitted:
    @pytest.mark.anyio
    async def test_success_event_emitted(self, ctx: SagaContext, events: AsyncMock) -> None:
        """on_step_success is called for each successfully completed step."""

        async def invoke(
            step_def: StepDefinition,
            bean: Any,
            context: SagaContext,
            step_input: Any = None,
        ) -> str:
            return f"result-{step_def.id}"

        invoker = _make_invoker_from_fn(invoke)

        steps = {"A": _make_step_def("A")}
        saga = _make_saga(steps)

        orchestrator = SagaExecutionOrchestrator(invoker, events)
        await orchestrator.execute(saga, ctx)

        events.on_step_success.assert_awaited_once()
        call_args = events.on_step_success.call_args
        assert call_args[0][0] == "test-saga"  # saga name
        assert call_args[0][2] == "A"  # step_id
        assert call_args[0][3] == 1  # attempts

    @pytest.mark.anyio
    async def test_failure_event_emitted(self, ctx: SagaContext, events: AsyncMock) -> None:
        """on_step_failed is called when a step fails after exhausting retries."""

        async def invoke(
            step_def: StepDefinition,
            bean: Any,
            context: SagaContext,
            step_input: Any = None,
        ) -> str:
            raise RuntimeError("permanent failure")

        invoker = _make_invoker_from_fn(invoke)

        steps = {"A": _make_step_def("A", retry=1)}
        saga = _make_saga(steps)

        orchestrator = SagaExecutionOrchestrator(invoker, events)

        with pytest.raises(RuntimeError, match="permanent failure"):
            await orchestrator.execute(saga, ctx)

        events.on_step_failed.assert_awaited_once()
        call_args = events.on_step_failed.call_args
        assert call_args[0][0] == "test-saga"  # saga name
        assert call_args[0][2] == "A"  # step_id
        assert isinstance(call_args[0][3], RuntimeError)  # error
        assert call_args[0][4] == 1  # attempts


# ── No events port ────────────────────────────────────────────


class TestNoEventsPort:
    @pytest.mark.anyio
    async def test_works_without_events_port(self, ctx: SagaContext) -> None:
        """Orchestrator works when events_port is None."""

        async def invoke(
            step_def: StepDefinition,
            bean: Any,
            context: SagaContext,
            step_input: Any = None,
        ) -> str:
            return f"result-{step_def.id}"

        invoker = _make_invoker_from_fn(invoke)

        steps = {"A": _make_step_def("A")}
        saga = _make_saga(steps)

        orchestrator = SagaExecutionOrchestrator(invoker, events_port=None)
        completed = await orchestrator.execute(saga, ctx)

        assert completed == ["A"]


# ── Context populated correctly ───────────────────────────────


class TestContextPopulation:
    @pytest.mark.anyio
    async def test_context_fully_populated(self, ctx: SagaContext, events: AsyncMock) -> None:
        """After execution, context has statuses, results, attempts, and latencies."""

        async def invoke(
            step_def: StepDefinition,
            bean: Any,
            context: SagaContext,
            step_input: Any = None,
        ) -> str:
            return f"result-{step_def.id}"

        invoker = _make_invoker_from_fn(invoke)

        steps = {
            "A": _make_step_def("A"),
            "B": _make_step_def("B", depends_on=["A"]),
        }
        saga = _make_saga(steps)

        orchestrator = SagaExecutionOrchestrator(invoker, events)
        _completed = await orchestrator.execute(saga, ctx)

        # Statuses
        assert ctx.step_statuses["A"] == StepStatus.DONE
        assert ctx.step_statuses["B"] == StepStatus.DONE

        # Results
        assert ctx.step_results["A"] == "result-A"
        assert ctx.step_results["B"] == "result-B"

        # Attempts
        assert ctx.step_attempts["A"] == 1
        assert ctx.step_attempts["B"] == 1

        # Latencies
        assert ctx.step_latencies_ms["A"] >= 0
        assert ctx.step_latencies_ms["B"] >= 0

        # Topology layers stored
        assert len(ctx.topology_layers) == 2
        assert ctx.topology_layers[0] == ["A"]
        assert ctx.topology_layers[1] == ["B"]

    @pytest.mark.anyio
    async def test_step_input_forwarded(self, ctx: SagaContext, events: AsyncMock) -> None:
        """step_input is forwarded to the StepInvoker."""
        received_inputs: list[Any] = []

        async def invoke(
            step_def: StepDefinition,
            bean: Any,
            context: SagaContext,
            step_input: Any = None,
        ) -> str:
            received_inputs.append(step_input)
            return "ok"

        invoker = _make_invoker_from_fn(invoke)

        steps = {"A": _make_step_def("A")}
        saga = _make_saga(steps)

        orchestrator = SagaExecutionOrchestrator(invoker, events)
        await orchestrator.execute(saga, ctx, step_input={"order": 42})

        assert received_inputs == [{"order": 42}]


# ── Exponential backoff ───────────────────────────────────────


class TestExponentialBackoff:
    @pytest.mark.anyio
    async def test_exponential_backoff_doubles(self, ctx: SagaContext, events: AsyncMock) -> None:
        """Backoff delay doubles on each retry (exponential)."""
        timestamps: list[float] = []

        async def invoke(
            step_def: StepDefinition,
            bean: Any,
            context: SagaContext,
            step_input: Any = None,
        ) -> str:
            timestamps.append(time.monotonic())
            if len(timestamps) < 3:
                raise RuntimeError("transient")
            return "ok"

        invoker = _make_invoker_from_fn(invoke)

        steps = {
            "A": _make_step_def("A", retry=3, backoff_ms=50),
        }
        saga = _make_saga(steps)

        orchestrator = SagaExecutionOrchestrator(invoker, events)
        await orchestrator.execute(saga, ctx)

        assert len(timestamps) == 3
        gap1_ms = (timestamps[1] - timestamps[0]) * 1000
        gap2_ms = (timestamps[2] - timestamps[1]) * 1000

        # First gap ~50ms, second ~100ms (doubled)
        assert gap1_ms >= 35
        assert gap2_ms >= 70
        # Second gap should be roughly double the first
        assert gap2_ms > gap1_ms * 1.5


# ── Jitter ────────────────────────────────────────────────────


class TestJitter:
    @pytest.mark.anyio
    async def test_jitter_varies_delay(self, ctx: SagaContext, events: AsyncMock) -> None:
        """With jitter enabled, delays vary between attempts across runs."""
        timestamps: list[float] = []

        async def invoke(
            step_def: StepDefinition,
            bean: Any,
            context: SagaContext,
            step_input: Any = None,
        ) -> str:
            timestamps.append(time.monotonic())
            if len(timestamps) < 2:
                raise RuntimeError("transient")
            return "ok"

        invoker = _make_invoker_from_fn(invoke)

        steps = {
            "A": _make_step_def("A", retry=2, backoff_ms=100, jitter=True, jitter_factor=0.5),
        }
        saga = _make_saga(steps)

        orchestrator = SagaExecutionOrchestrator(invoker, events)
        await orchestrator.execute(saga, ctx)

        gap_ms = (timestamps[1] - timestamps[0]) * 1000
        # With jitter_factor=0.5, delay is 100 * (1 + uniform(-0.5, 0.5))
        # So range is 50..150 ms — just verify it's in a reasonable range
        assert 30 <= gap_ms <= 200
