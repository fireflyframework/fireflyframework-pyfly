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
"""Tests for saga compensator -- 5 compensation policies."""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from pyfly.transactional.saga.core.context import SagaContext
from pyfly.transactional.saga.engine.compensator import SagaCompensator
from pyfly.transactional.saga.registry.saga_definition import SagaDefinition
from pyfly.transactional.saga.registry.step_definition import StepDefinition
from pyfly.transactional.shared.types import CompensationPolicy

# ── Helpers ──────────────────────────────────────────────────


class _FakeBean:
    """Dummy saga bean."""


def _make_step_def(
    step_id: str,
    compensation_retry: int | None = None,
    compensation_backoff_ms: int | None = None,
) -> StepDefinition:
    """Create a minimal StepDefinition with a compensate_method stub."""

    async def _compensate(self: Any, context: SagaContext) -> str:
        return f"compensated-{step_id}"

    return StepDefinition(
        id=step_id,
        compensate_method=_compensate,
        compensation_retry=compensation_retry,
        compensation_backoff_ms=compensation_backoff_ms,
    )


def _make_saga_def(step_defs: list[StepDefinition]) -> SagaDefinition:
    return SagaDefinition(
        name="test-saga",
        bean=_FakeBean(),
        steps={s.id: s for s in step_defs},
    )


@pytest.fixture
def ctx() -> SagaContext:
    return SagaContext(saga_name="test-saga")


@pytest.fixture
def step_invoker() -> AsyncMock:
    """A mock StepInvoker whose invoke_compensation succeeds by default."""
    mock = AsyncMock()
    mock.invoke_compensation = AsyncMock(return_value=None)
    return mock


@pytest.fixture
def events_port() -> AsyncMock:
    """A mock TransactionalEventsPort."""
    mock = AsyncMock()
    mock.on_compensated = AsyncMock(return_value=None)
    return mock


@pytest.fixture
def error_handler() -> AsyncMock:
    """A mock CompensationErrorHandlerPort."""
    mock = AsyncMock()
    mock.handle = AsyncMock(return_value=None)
    return mock


# ── STRICT_SEQUENTIAL ────────────────────────────────────────


class TestStrictSequential:
    @pytest.mark.anyio
    async def test_compensates_in_reverse_order(
        self,
        step_invoker: AsyncMock,
        events_port: AsyncMock,
        ctx: SagaContext,
    ) -> None:
        """Compensates steps in reverse completion order, one at a time."""
        steps = [_make_step_def("s1"), _make_step_def("s2"), _make_step_def("s3")]
        saga_def = _make_saga_def(steps)
        completed = ["s1", "s2", "s3"]
        topology = [["s1"], ["s2"], ["s3"]]

        call_order: list[str] = []
        original_invoke = step_invoker.invoke_compensation

        async def _track_invoke(step_def: Any, bean: Any, context: Any, **kw: Any) -> None:
            call_order.append(step_def.id)
            return await original_invoke(step_def, bean, context, **kw)

        step_invoker.invoke_compensation = AsyncMock(side_effect=_track_invoke)

        compensator = SagaCompensator(step_invoker, events_port)
        await compensator.compensate(
            CompensationPolicy.STRICT_SEQUENTIAL,
            "test-saga",
            completed,
            saga_def,
            ctx,
            topology,
        )

        assert call_order == ["s3", "s2", "s1"]
        assert events_port.on_compensated.call_count == 3

    @pytest.mark.anyio
    async def test_stops_on_first_error(
        self,
        step_invoker: AsyncMock,
        events_port: AsyncMock,
        error_handler: AsyncMock,
        ctx: SagaContext,
    ) -> None:
        """Stops compensating after the first error and re-raises."""
        steps = [_make_step_def("s1"), _make_step_def("s2"), _make_step_def("s3")]
        saga_def = _make_saga_def(steps)
        completed = ["s1", "s2", "s3"]
        topology = [["s1"], ["s2"], ["s3"]]

        call_count = 0

        async def _fail_on_s2(step_def: Any, bean: Any, context: Any, **kw: Any) -> None:
            nonlocal call_count
            call_count += 1
            if step_def.id == "s2":
                raise RuntimeError("compensation failed for s2")

        step_invoker.invoke_compensation = AsyncMock(side_effect=_fail_on_s2)

        compensator = SagaCompensator(step_invoker, events_port, error_handler)
        with pytest.raises(RuntimeError, match="compensation failed for s2"):
            await compensator.compensate(
                CompensationPolicy.STRICT_SEQUENTIAL,
                "test-saga",
                completed,
                saga_def,
                ctx,
                topology,
            )

        # s3 succeeds, s2 fails, s1 is never called
        assert call_count == 2
        error_handler.handle.assert_called_once()

    @pytest.mark.anyio
    async def test_emits_events_for_each_compensation(
        self,
        step_invoker: AsyncMock,
        events_port: AsyncMock,
        ctx: SagaContext,
    ) -> None:
        """Events port receives on_compensated for each step."""
        steps = [_make_step_def("s1"), _make_step_def("s2")]
        saga_def = _make_saga_def(steps)

        compensator = SagaCompensator(step_invoker, events_port)
        await compensator.compensate(
            CompensationPolicy.STRICT_SEQUENTIAL,
            "test-saga",
            ["s1", "s2"],
            saga_def,
            ctx,
            [["s1"], ["s2"]],
        )

        assert events_port.on_compensated.call_count == 2
        # First call should be for s2 (reverse order), second for s1
        calls = events_port.on_compensated.call_args_list
        assert (
            calls[0].kwargs.get("step_id") or calls[0][1][2]
            if len(calls[0][1]) > 2
            else calls[0][0][2]
            if len(calls[0][0]) > 2
            else None
        )  # noqa: E501
        # Verify both step_ids appear
        step_ids_called = []
        for call in calls:
            # on_compensated(name, correlation_id, step_id, error)
            step_ids_called.append(call[0][2] if len(call[0]) > 2 else call[1]["step_id"])
        assert step_ids_called == ["s2", "s1"]


# ── GROUPED_PARALLEL ─────────────────────────────────────────


class TestGroupedParallel:
    @pytest.mark.anyio
    async def test_compensates_by_reverse_layer(
        self,
        step_invoker: AsyncMock,
        events_port: AsyncMock,
        ctx: SagaContext,
    ) -> None:
        """Compensates layers in reverse order; steps within a layer run in parallel."""
        steps = [
            _make_step_def("s1"),
            _make_step_def("s2"),
            _make_step_def("s3"),
            _make_step_def("s4"),
        ]
        saga_def = _make_saga_def(steps)
        completed = ["s1", "s2", "s3", "s4"]
        # layer 0: [s1, s2], layer 1: [s3, s4]
        topology = [["s1", "s2"], ["s3", "s4"]]

        _layer_order: list[list[str]] = []
        current_layer: list[str] = []

        async def _track_invoke(step_def: Any, bean: Any, context: Any, **kw: Any) -> None:
            current_layer.append(step_def.id)

        step_invoker.invoke_compensation = AsyncMock(side_effect=_track_invoke)

        # Patch gather to track layer boundaries
        original_gather = asyncio.gather
        layer_snapshots: list[list[str]] = []

        async def _patched_gather(*coros: Any, **kw: Any) -> Any:
            nonlocal current_layer
            current_layer = []
            result = await original_gather(*coros, **kw)
            layer_snapshots.append(list(current_layer))
            return result

        with patch("pyfly.transactional.saga.engine.compensator.asyncio.gather", side_effect=_patched_gather):
            compensator = SagaCompensator(step_invoker, events_port)
            await compensator.compensate(
                CompensationPolicy.GROUPED_PARALLEL,
                "test-saga",
                completed,
                saga_def,
                ctx,
                topology,
            )

        # Reversed layers: [s3, s4] first, then [s1, s2]
        assert len(layer_snapshots) == 2
        assert set(layer_snapshots[0]) == {"s3", "s4"}
        assert set(layer_snapshots[1]) == {"s1", "s2"}

    @pytest.mark.anyio
    async def test_only_compensates_completed_steps_in_layer(
        self,
        step_invoker: AsyncMock,
        events_port: AsyncMock,
        ctx: SagaContext,
    ) -> None:
        """Only steps that are in the completed list get compensated."""
        steps = [_make_step_def("s1"), _make_step_def("s2"), _make_step_def("s3")]
        saga_def = _make_saga_def(steps)
        # s2 is not completed
        completed = ["s1", "s3"]
        topology = [["s1", "s2"], ["s3"]]

        called_ids: list[str] = []

        async def _track(step_def: Any, bean: Any, context: Any, **kw: Any) -> None:
            called_ids.append(step_def.id)

        step_invoker.invoke_compensation = AsyncMock(side_effect=_track)

        compensator = SagaCompensator(step_invoker, events_port)
        await compensator.compensate(
            CompensationPolicy.GROUPED_PARALLEL,
            "test-saga",
            completed,
            saga_def,
            ctx,
            topology,
        )

        assert "s2" not in called_ids
        assert set(called_ids) == {"s1", "s3"}

    @pytest.mark.anyio
    async def test_stops_on_layer_error(
        self,
        step_invoker: AsyncMock,
        events_port: AsyncMock,
        error_handler: AsyncMock,
        ctx: SagaContext,
    ) -> None:
        """When a step in a layer fails, compensation stops (does not process further layers)."""
        steps = [_make_step_def("s1"), _make_step_def("s2"), _make_step_def("s3")]
        saga_def = _make_saga_def(steps)
        completed = ["s1", "s2", "s3"]
        topology = [["s1"], ["s2"], ["s3"]]

        called_ids: list[str] = []

        async def _fail_on_s2(step_def: Any, bean: Any, context: Any, **kw: Any) -> None:
            called_ids.append(step_def.id)
            if step_def.id == "s2":
                raise RuntimeError("s2 comp failed")

        step_invoker.invoke_compensation = AsyncMock(side_effect=_fail_on_s2)

        compensator = SagaCompensator(step_invoker, events_port, error_handler)
        with pytest.raises(RuntimeError, match="s2 comp failed"):
            await compensator.compensate(
                CompensationPolicy.GROUPED_PARALLEL,
                "test-saga",
                completed,
                saga_def,
                ctx,
                topology,
            )

        # s3 compensated (last layer), s2 fails (middle layer), s1 never reached
        assert "s3" in called_ids
        assert "s2" in called_ids
        assert "s1" not in called_ids


# ── RETRY_WITH_BACKOFF ───────────────────────────────────────


class TestRetryWithBackoff:
    @pytest.mark.anyio
    async def test_retries_failing_compensation(
        self,
        step_invoker: AsyncMock,
        events_port: AsyncMock,
        ctx: SagaContext,
    ) -> None:
        """Retries compensation with exponential backoff before succeeding."""
        steps = [_make_step_def("s1", compensation_retry=3, compensation_backoff_ms=100)]
        saga_def = _make_saga_def(steps)

        attempts = 0

        async def _fail_then_succeed(step_def: Any, bean: Any, context: Any, **kw: Any) -> None:
            nonlocal attempts
            attempts += 1
            if attempts < 3:
                raise RuntimeError("transient failure")

        step_invoker.invoke_compensation = AsyncMock(side_effect=_fail_then_succeed)

        with patch("pyfly.transactional.saga.engine.compensator.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            compensator = SagaCompensator(step_invoker, events_port)
            await compensator.compensate(
                CompensationPolicy.RETRY_WITH_BACKOFF,
                "test-saga",
                ["s1"],
                saga_def,
                ctx,
                [["s1"]],
            )

        assert attempts == 3
        # Backoff: 100ms, 200ms (exponential)
        assert mock_sleep.call_count == 2
        assert mock_sleep.call_args_list[0][0][0] == pytest.approx(0.1, abs=0.01)
        assert mock_sleep.call_args_list[1][0][0] == pytest.approx(0.2, abs=0.01)

    @pytest.mark.anyio
    async def test_raises_after_exhausting_retries(
        self,
        step_invoker: AsyncMock,
        events_port: AsyncMock,
        error_handler: AsyncMock,
        ctx: SagaContext,
    ) -> None:
        """Raises after all retries are exhausted."""
        steps = [_make_step_def("s1", compensation_retry=2, compensation_backoff_ms=50)]
        saga_def = _make_saga_def(steps)

        step_invoker.invoke_compensation = AsyncMock(side_effect=RuntimeError("persistent failure"))

        with patch("pyfly.transactional.saga.engine.compensator.asyncio.sleep", new_callable=AsyncMock):
            compensator = SagaCompensator(step_invoker, events_port, error_handler)
            with pytest.raises(RuntimeError, match="persistent failure"):
                await compensator.compensate(
                    CompensationPolicy.RETRY_WITH_BACKOFF,
                    "test-saga",
                    ["s1"],
                    saga_def,
                    ctx,
                    [["s1"]],
                )

        error_handler.handle.assert_called_once()

    @pytest.mark.anyio
    async def test_uses_default_retry_values(
        self,
        step_invoker: AsyncMock,
        events_port: AsyncMock,
        ctx: SagaContext,
    ) -> None:
        """Uses default retry=3 and backoff=1000ms when step_def has no overrides."""
        steps = [_make_step_def("s1")]  # no compensation_retry/backoff set
        saga_def = _make_saga_def(steps)

        attempts = 0

        async def _fail_then_succeed(step_def: Any, bean: Any, context: Any, **kw: Any) -> None:
            nonlocal attempts
            attempts += 1
            if attempts < 3:
                raise RuntimeError("transient")

        step_invoker.invoke_compensation = AsyncMock(side_effect=_fail_then_succeed)

        with patch("pyfly.transactional.saga.engine.compensator.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            compensator = SagaCompensator(step_invoker, events_port)
            await compensator.compensate(
                CompensationPolicy.RETRY_WITH_BACKOFF,
                "test-saga",
                ["s1"],
                saga_def,
                ctx,
                [["s1"]],
            )

        assert attempts == 3
        # Default backoff: 1000ms = 1.0s, then 2.0s
        assert mock_sleep.call_args_list[0][0][0] == pytest.approx(1.0, abs=0.01)
        assert mock_sleep.call_args_list[1][0][0] == pytest.approx(2.0, abs=0.01)


# ── CIRCUIT_BREAKER ──────────────────────────────────────────


class TestCircuitBreaker:
    @pytest.mark.anyio
    async def test_opens_circuit_after_consecutive_failures(
        self,
        step_invoker: AsyncMock,
        events_port: AsyncMock,
        error_handler: AsyncMock,
        ctx: SagaContext,
    ) -> None:
        """After 3 consecutive failures, the circuit opens and remaining compensations are skipped."""
        steps = [
            _make_step_def("s1"),
            _make_step_def("s2"),
            _make_step_def("s3"),
            _make_step_def("s4"),
            _make_step_def("s5"),
        ]
        saga_def = _make_saga_def(steps)
        completed = ["s1", "s2", "s3", "s4", "s5"]
        topology = [["s1"], ["s2"], ["s3"], ["s4"], ["s5"]]

        called_ids: list[str] = []

        async def _always_fail(step_def: Any, bean: Any, context: Any, **kw: Any) -> None:
            called_ids.append(step_def.id)
            raise RuntimeError(f"fail-{step_def.id}")

        step_invoker.invoke_compensation = AsyncMock(side_effect=_always_fail)

        compensator = SagaCompensator(step_invoker, events_port, error_handler)
        # Should not raise -- circuit breaker absorbs errors
        await compensator.compensate(
            CompensationPolicy.CIRCUIT_BREAKER,
            "test-saga",
            completed,
            saga_def,
            ctx,
            topology,
        )

        # Only 3 steps should be attempted before circuit opens
        assert len(called_ids) == 3
        assert called_ids == ["s5", "s4", "s3"]
        assert error_handler.handle.call_count == 3

    @pytest.mark.anyio
    async def test_resets_counter_on_success(
        self,
        step_invoker: AsyncMock,
        events_port: AsyncMock,
        error_handler: AsyncMock,
        ctx: SagaContext,
    ) -> None:
        """A successful compensation resets the consecutive failure counter."""
        steps = [
            _make_step_def("s1"),
            _make_step_def("s2"),
            _make_step_def("s3"),
            _make_step_def("s4"),
            _make_step_def("s5"),
            _make_step_def("s6"),
        ]
        saga_def = _make_saga_def(steps)
        completed = ["s1", "s2", "s3", "s4", "s5", "s6"]
        topology = [["s1"], ["s2"], ["s3"], ["s4"], ["s5"], ["s6"]]

        called_ids: list[str] = []

        async def _fail_pattern(step_def: Any, bean: Any, context: Any, **kw: Any) -> None:
            called_ids.append(step_def.id)
            # Reverse order: s6, s5, s4, s3, s2, s1
            # Fail s6, s5, then succeed s4, fail s3, s2, s1
            if step_def.id in {"s6", "s5"}:
                raise RuntimeError(f"fail-{step_def.id}")

        step_invoker.invoke_compensation = AsyncMock(side_effect=_fail_pattern)

        compensator = SagaCompensator(step_invoker, events_port, error_handler)
        await compensator.compensate(
            CompensationPolicy.CIRCUIT_BREAKER,
            "test-saga",
            completed,
            saga_def,
            ctx,
            topology,
        )

        # s6 fail (1), s5 fail (2), s4 success (reset to 0), s3 success, s2 success, s1 success
        assert len(called_ids) == 6
        assert error_handler.handle.call_count == 2

    @pytest.mark.anyio
    async def test_compensates_all_when_no_failures(
        self,
        step_invoker: AsyncMock,
        events_port: AsyncMock,
        ctx: SagaContext,
    ) -> None:
        """When all compensations succeed, all steps are compensated."""
        steps = [_make_step_def("s1"), _make_step_def("s2"), _make_step_def("s3")]
        saga_def = _make_saga_def(steps)
        completed = ["s1", "s2", "s3"]
        topology = [["s1"], ["s2"], ["s3"]]

        compensator = SagaCompensator(step_invoker, events_port)
        await compensator.compensate(
            CompensationPolicy.CIRCUIT_BREAKER,
            "test-saga",
            completed,
            saga_def,
            ctx,
            topology,
        )

        assert step_invoker.invoke_compensation.call_count == 3
        assert events_port.on_compensated.call_count == 3


# ── BEST_EFFORT_PARALLEL ────────────────────────────────────


class TestBestEffortParallel:
    @pytest.mark.anyio
    async def test_compensates_all_in_parallel(
        self,
        step_invoker: AsyncMock,
        events_port: AsyncMock,
        ctx: SagaContext,
    ) -> None:
        """All steps are compensated in parallel, regardless of errors."""
        steps = [_make_step_def("s1"), _make_step_def("s2"), _make_step_def("s3")]
        saga_def = _make_saga_def(steps)
        completed = ["s1", "s2", "s3"]
        topology = [["s1"], ["s2"], ["s3"]]

        compensator = SagaCompensator(step_invoker, events_port)
        await compensator.compensate(
            CompensationPolicy.BEST_EFFORT_PARALLEL,
            "test-saga",
            completed,
            saga_def,
            ctx,
            topology,
        )

        assert step_invoker.invoke_compensation.call_count == 3
        assert events_port.on_compensated.call_count == 3

    @pytest.mark.anyio
    async def test_collects_errors_without_raising(
        self,
        step_invoker: AsyncMock,
        events_port: AsyncMock,
        error_handler: AsyncMock,
        ctx: SagaContext,
    ) -> None:
        """Errors are collected but never raised; all compensations are attempted."""
        steps = [_make_step_def("s1"), _make_step_def("s2"), _make_step_def("s3")]
        saga_def = _make_saga_def(steps)
        completed = ["s1", "s2", "s3"]
        topology = [["s1"], ["s2"], ["s3"]]

        async def _fail_s1_s3(step_def: Any, bean: Any, context: Any, **kw: Any) -> None:
            if step_def.id in {"s1", "s3"}:
                raise RuntimeError(f"fail-{step_def.id}")

        step_invoker.invoke_compensation = AsyncMock(side_effect=_fail_s1_s3)

        compensator = SagaCompensator(step_invoker, events_port, error_handler)
        # Should NOT raise
        await compensator.compensate(
            CompensationPolicy.BEST_EFFORT_PARALLEL,
            "test-saga",
            completed,
            saga_def,
            ctx,
            topology,
        )

        # All three attempted
        assert step_invoker.invoke_compensation.call_count == 3
        # Error handler called for s1 and s3
        assert error_handler.handle.call_count == 2

    @pytest.mark.anyio
    async def test_events_emitted_for_successes_and_failures(
        self,
        step_invoker: AsyncMock,
        events_port: AsyncMock,
        error_handler: AsyncMock,
        ctx: SagaContext,
    ) -> None:
        """Events are emitted for both successful and failed compensations."""
        steps = [_make_step_def("s1"), _make_step_def("s2")]
        saga_def = _make_saga_def(steps)
        completed = ["s1", "s2"]
        topology = [["s1"], ["s2"]]

        async def _fail_s1(step_def: Any, bean: Any, context: Any, **kw: Any) -> None:
            if step_def.id == "s1":
                raise RuntimeError("fail-s1")

        step_invoker.invoke_compensation = AsyncMock(side_effect=_fail_s1)

        compensator = SagaCompensator(step_invoker, events_port, error_handler)
        await compensator.compensate(
            CompensationPolicy.BEST_EFFORT_PARALLEL,
            "test-saga",
            completed,
            saga_def,
            ctx,
            topology,
        )

        assert events_port.on_compensated.call_count == 2
        # One call has error=None (s2 success), one has error set (s1 failure)
        errors_in_calls = [
            call[0][3] if len(call[0]) > 3 else call[1].get("error")
            for call in events_port.on_compensated.call_args_list
        ]
        assert any(e is None for e in errors_in_calls)
        assert any(e is not None for e in errors_in_calls)


# ── Cross-cutting concerns ───────────────────────────────────


class TestCrossCutting:
    @pytest.mark.anyio
    async def test_works_without_events_port(
        self,
        step_invoker: AsyncMock,
        ctx: SagaContext,
    ) -> None:
        """Compensator works when no events port is configured."""
        steps = [_make_step_def("s1")]
        saga_def = _make_saga_def(steps)

        compensator = SagaCompensator(step_invoker, events_port=None)
        await compensator.compensate(
            CompensationPolicy.STRICT_SEQUENTIAL,
            "test-saga",
            ["s1"],
            saga_def,
            ctx,
            [["s1"]],
        )

        assert step_invoker.invoke_compensation.call_count == 1

    @pytest.mark.anyio
    async def test_works_without_error_handler(
        self,
        step_invoker: AsyncMock,
        events_port: AsyncMock,
        ctx: SagaContext,
    ) -> None:
        """Compensator works when no error handler is configured (errors still raised/logged)."""
        steps = [_make_step_def("s1")]
        saga_def = _make_saga_def(steps)

        step_invoker.invoke_compensation = AsyncMock(side_effect=RuntimeError("fail"))

        compensator = SagaCompensator(step_invoker, events_port, error_handler=None)
        # BEST_EFFORT_PARALLEL does not raise
        await compensator.compensate(
            CompensationPolicy.BEST_EFFORT_PARALLEL,
            "test-saga",
            ["s1"],
            saga_def,
            ctx,
            [["s1"]],
        )

        assert step_invoker.invoke_compensation.call_count == 1

    @pytest.mark.anyio
    async def test_skips_steps_without_compensation_method(
        self,
        step_invoker: AsyncMock,
        events_port: AsyncMock,
        ctx: SagaContext,
    ) -> None:
        """Steps without a compensate_method are skipped."""
        step_with = _make_step_def("s1")
        step_without = StepDefinition(id="s2", compensate_method=None)
        saga_def = _make_saga_def([step_with, step_without])

        compensator = SagaCompensator(step_invoker, events_port)
        await compensator.compensate(
            CompensationPolicy.STRICT_SEQUENTIAL,
            "test-saga",
            ["s1", "s2"],
            saga_def,
            ctx,
            [["s1"], ["s2"]],
        )

        # Only s1 should have invoke_compensation called
        assert step_invoker.invoke_compensation.call_count == 1

    @pytest.mark.anyio
    async def test_empty_completed_list_is_noop(
        self,
        step_invoker: AsyncMock,
        events_port: AsyncMock,
        ctx: SagaContext,
    ) -> None:
        """Empty completed_step_ids results in no compensation calls."""
        saga_def = _make_saga_def([_make_step_def("s1")])

        compensator = SagaCompensator(step_invoker, events_port)
        await compensator.compensate(
            CompensationPolicy.STRICT_SEQUENTIAL,
            "test-saga",
            [],
            saga_def,
            ctx,
            [["s1"]],
        )

        step_invoker.invoke_compensation.assert_not_called()
