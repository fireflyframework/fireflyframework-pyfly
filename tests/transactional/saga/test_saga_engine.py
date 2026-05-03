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
"""Tests for SagaEngine — main saga orchestrator."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from pyfly.transactional.saga.core.context import SagaContext
from pyfly.transactional.saga.core.result import SagaResult, StepOutcome
from pyfly.transactional.saga.engine.saga_engine import SagaEngine
from pyfly.transactional.saga.registry.saga_definition import SagaDefinition
from pyfly.transactional.saga.registry.saga_registry import SagaRegistry
from pyfly.transactional.saga.registry.step_definition import StepDefinition
from pyfly.transactional.shared.types import CompensationPolicy, StepStatus

# ── Helpers ──────────────────────────────────────────────────


class _FakeBean:
    """Dummy saga bean."""


def _make_step_def(step_id: str, depends_on: list[str] | None = None) -> StepDefinition:
    """Create a minimal StepDefinition."""

    async def _step(self: Any, context: SagaContext) -> str:
        return f"result-{step_id}"

    async def _compensate(self: Any, context: SagaContext) -> str:
        return f"compensated-{step_id}"

    return StepDefinition(
        id=step_id,
        step_method=_step,
        compensate_method=_compensate,
        depends_on=depends_on or [],
    )


def _make_saga_def(
    name: str = "test-saga",
    step_defs: list[StepDefinition] | None = None,
) -> SagaDefinition:
    step_defs = step_defs or [
        _make_step_def("s1"),
        _make_step_def("s2"),
        _make_step_def("s3"),
    ]
    return SagaDefinition(
        name=name,
        bean=_FakeBean(),
        steps={s.id: s for s in step_defs},
    )


@pytest.fixture
def registry() -> MagicMock:
    """A mock SagaRegistry."""
    mock = MagicMock(spec=SagaRegistry)
    return mock


@pytest.fixture
def step_invoker() -> AsyncMock:
    """A mock StepInvoker."""
    mock = AsyncMock()
    return mock


@pytest.fixture
def execution_orchestrator() -> AsyncMock:
    """A mock SagaExecutionOrchestrator whose execute succeeds by default."""
    mock = AsyncMock()
    mock.execute = AsyncMock(return_value=["s1", "s2", "s3"])
    return mock


@pytest.fixture
def compensator() -> AsyncMock:
    """A mock SagaCompensator."""
    mock = AsyncMock()
    mock.compensate = AsyncMock(return_value=None)
    return mock


@pytest.fixture
def persistence_port() -> AsyncMock:
    """A mock TransactionalPersistencePort."""
    mock = AsyncMock()
    mock.persist_state = AsyncMock(return_value=None)
    mock.mark_completed = AsyncMock(return_value=None)
    return mock


@pytest.fixture
def events_port() -> AsyncMock:
    """A mock TransactionalEventsPort."""
    mock = AsyncMock()
    mock.on_start = AsyncMock(return_value=None)
    mock.on_completed = AsyncMock(return_value=None)
    return mock


def _build_engine(
    registry: MagicMock,
    step_invoker: AsyncMock,
    execution_orchestrator: AsyncMock,
    compensator: AsyncMock,
    persistence_port: AsyncMock | None = None,
    events_port: AsyncMock | None = None,
) -> SagaEngine:
    return SagaEngine(
        registry=registry,
        step_invoker=step_invoker,
        execution_orchestrator=execution_orchestrator,
        compensator=compensator,
        persistence_port=persistence_port,
        events_port=events_port,
    )


# ── Successful execution ─────────────────────────────────────


class TestSuccessfulExecution:
    @pytest.mark.anyio
    async def test_successful_saga_with_3_steps(
        self,
        registry: MagicMock,
        step_invoker: AsyncMock,
        execution_orchestrator: AsyncMock,
        compensator: AsyncMock,
        events_port: AsyncMock,
        persistence_port: AsyncMock,
    ) -> None:
        """Successful saga with 3 steps returns SagaResult.success=True."""
        saga_def = _make_saga_def()
        registry.get.return_value = saga_def

        # Simulate orchestrator populating context
        async def _execute_side_effect(saga_def: Any, ctx: SagaContext, step_input: Any = None) -> list[str]:
            ctx.step_statuses["s1"] = StepStatus.DONE
            ctx.step_statuses["s2"] = StepStatus.DONE
            ctx.step_statuses["s3"] = StepStatus.DONE
            ctx.step_attempts["s1"] = 1
            ctx.step_attempts["s2"] = 1
            ctx.step_attempts["s3"] = 1
            ctx.step_latencies_ms["s1"] = 10.0
            ctx.step_latencies_ms["s2"] = 20.0
            ctx.step_latencies_ms["s3"] = 30.0
            ctx.step_results["s1"] = "result-s1"
            ctx.step_results["s2"] = "result-s2"
            ctx.step_results["s3"] = "result-s3"
            return ["s1", "s2", "s3"]

        execution_orchestrator.execute = AsyncMock(side_effect=_execute_side_effect)

        engine = _build_engine(
            registry,
            step_invoker,
            execution_orchestrator,
            compensator,
            persistence_port,
            events_port,
        )

        result = await engine.execute("test-saga", input_data={"key": "value"})

        assert isinstance(result, SagaResult)
        assert result.success is True
        assert result.error is None
        assert result.saga_name == "test-saga"
        assert len(result.steps) == 3
        assert result.steps["s1"].status == StepStatus.DONE
        assert result.steps["s2"].status == StepStatus.DONE
        assert result.steps["s3"].status == StepStatus.DONE
        assert result.steps["s1"].result == "result-s1"
        assert result.steps["s1"].attempts == 1
        assert result.steps["s1"].latency_ms == 10.0
        assert result.steps["s1"].compensated is False

    @pytest.mark.anyio
    async def test_steps_populated_correctly(
        self,
        registry: MagicMock,
        step_invoker: AsyncMock,
        execution_orchestrator: AsyncMock,
        compensator: AsyncMock,
    ) -> None:
        """SagaResult.steps populated correctly with StepOutcome for each step."""
        saga_def = _make_saga_def()
        registry.get.return_value = saga_def

        async def _execute_side_effect(saga_def: Any, ctx: SagaContext, step_input: Any = None) -> list[str]:
            ctx.step_statuses["s1"] = StepStatus.DONE
            ctx.step_statuses["s2"] = StepStatus.DONE
            ctx.step_statuses["s3"] = StepStatus.DONE
            ctx.step_attempts["s1"] = 1
            ctx.step_attempts["s2"] = 2
            ctx.step_attempts["s3"] = 1
            ctx.step_latencies_ms["s1"] = 5.0
            ctx.step_latencies_ms["s2"] = 15.0
            ctx.step_latencies_ms["s3"] = 25.0
            ctx.step_results["s1"] = "r1"
            ctx.step_results["s2"] = "r2"
            ctx.step_results["s3"] = "r3"
            return ["s1", "s2", "s3"]

        execution_orchestrator.execute = AsyncMock(side_effect=_execute_side_effect)

        engine = _build_engine(registry, step_invoker, execution_orchestrator, compensator)
        result = await engine.execute("test-saga")

        for step_id in ["s1", "s2", "s3"]:
            assert step_id in result.steps
            outcome = result.steps[step_id]
            assert isinstance(outcome, StepOutcome)

        assert result.steps["s2"].attempts == 2
        assert result.steps["s2"].latency_ms == 15.0
        assert result.steps["s2"].result == "r2"


# ── Failed execution with compensation ───────────────────────


class TestFailedExecution:
    @pytest.mark.anyio
    async def test_failed_saga_triggers_compensation(
        self,
        registry: MagicMock,
        step_invoker: AsyncMock,
        execution_orchestrator: AsyncMock,
        compensator: AsyncMock,
        events_port: AsyncMock,
        persistence_port: AsyncMock,
    ) -> None:
        """Failed saga triggers compensation and returns SagaResult.success=False."""
        saga_def = _make_saga_def()
        registry.get.return_value = saga_def

        error = RuntimeError("step s3 failed")

        async def _execute_side_effect(saga_def: Any, ctx: SagaContext, step_input: Any = None) -> list[str]:
            ctx.step_statuses["s1"] = StepStatus.DONE
            ctx.step_statuses["s2"] = StepStatus.DONE
            ctx.step_statuses["s3"] = StepStatus.FAILED
            ctx.step_attempts["s1"] = 1
            ctx.step_attempts["s2"] = 1
            ctx.step_attempts["s3"] = 1
            ctx.step_results["s1"] = "r1"
            ctx.step_results["s2"] = "r2"
            raise error

        execution_orchestrator.execute = AsyncMock(side_effect=_execute_side_effect)

        engine = _build_engine(
            registry,
            step_invoker,
            execution_orchestrator,
            compensator,
            persistence_port,
            events_port,
        )

        result = await engine.execute("test-saga")

        assert result.success is False
        assert result.error is error
        compensator.compensate.assert_called_once()
        # Verify compensation was called with the correct policy
        call_args = compensator.compensate.call_args
        assert call_args[1]["policy"] == CompensationPolicy.STRICT_SEQUENTIAL

    @pytest.mark.anyio
    async def test_failed_saga_steps_reflect_failure(
        self,
        registry: MagicMock,
        step_invoker: AsyncMock,
        execution_orchestrator: AsyncMock,
        compensator: AsyncMock,
    ) -> None:
        """Failed saga result includes step with FAILED status."""
        saga_def = _make_saga_def()
        registry.get.return_value = saga_def

        error = RuntimeError("s2 boom")

        async def _execute_side_effect(saga_def: Any, ctx: SagaContext, step_input: Any = None) -> list[str]:
            ctx.step_statuses["s1"] = StepStatus.DONE
            ctx.step_statuses["s2"] = StepStatus.FAILED
            ctx.step_attempts["s1"] = 1
            ctx.step_attempts["s2"] = 3
            ctx.step_latencies_ms["s1"] = 5.0
            ctx.step_latencies_ms["s2"] = 50.0
            ctx.step_results["s1"] = "r1"
            raise error

        execution_orchestrator.execute = AsyncMock(side_effect=_execute_side_effect)

        engine = _build_engine(registry, step_invoker, execution_orchestrator, compensator)
        result = await engine.execute("test-saga")

        assert result.success is False
        assert result.steps["s2"].status == StepStatus.FAILED
        assert result.steps["s2"].attempts == 3
        # s3 was never started
        assert result.steps["s3"].status == StepStatus.PENDING
        assert result.steps["s3"].attempts == 0


# ── Events emission ──────────────────────────────────────────


class TestEventsEmission:
    @pytest.mark.anyio
    async def test_on_start_and_on_completed_emitted_on_success(
        self,
        registry: MagicMock,
        step_invoker: AsyncMock,
        execution_orchestrator: AsyncMock,
        compensator: AsyncMock,
        events_port: AsyncMock,
    ) -> None:
        """Events port receives on_start and on_completed on successful execution."""
        saga_def = _make_saga_def()
        registry.get.return_value = saga_def
        execution_orchestrator.execute = AsyncMock(return_value=["s1", "s2", "s3"])

        engine = _build_engine(
            registry,
            step_invoker,
            execution_orchestrator,
            compensator,
            events_port=events_port,
        )

        result = await engine.execute("test-saga")

        events_port.on_start.assert_called_once_with("test-saga", result.correlation_id)
        events_port.on_completed.assert_called_once_with(
            "test-saga",
            result.correlation_id,
            True,
        )

    @pytest.mark.anyio
    async def test_on_start_and_on_completed_emitted_on_failure(
        self,
        registry: MagicMock,
        step_invoker: AsyncMock,
        execution_orchestrator: AsyncMock,
        compensator: AsyncMock,
        events_port: AsyncMock,
    ) -> None:
        """Events port receives on_start and on_completed(success=False) on failure."""
        saga_def = _make_saga_def()
        registry.get.return_value = saga_def
        execution_orchestrator.execute = AsyncMock(
            side_effect=RuntimeError("boom"),
        )

        engine = _build_engine(
            registry,
            step_invoker,
            execution_orchestrator,
            compensator,
            events_port=events_port,
        )

        result = await engine.execute("test-saga")

        events_port.on_start.assert_called_once()
        events_port.on_completed.assert_called_once_with(
            "test-saga",
            result.correlation_id,
            False,
        )

    @pytest.mark.anyio
    async def test_no_events_port_configured(
        self,
        registry: MagicMock,
        step_invoker: AsyncMock,
        execution_orchestrator: AsyncMock,
        compensator: AsyncMock,
    ) -> None:
        """Engine works without events port configured (no errors)."""
        saga_def = _make_saga_def()
        registry.get.return_value = saga_def
        execution_orchestrator.execute = AsyncMock(return_value=["s1", "s2", "s3"])

        engine = _build_engine(
            registry,
            step_invoker,
            execution_orchestrator,
            compensator,
            events_port=None,
        )

        result = await engine.execute("test-saga")
        assert result.success is True


# ── Persistence ──────────────────────────────────────────────


class TestPersistence:
    @pytest.mark.anyio
    async def test_persist_state_and_mark_completed_on_success(
        self,
        registry: MagicMock,
        step_invoker: AsyncMock,
        execution_orchestrator: AsyncMock,
        compensator: AsyncMock,
        persistence_port: AsyncMock,
    ) -> None:
        """Persistence port receives persist_state and mark_completed on success."""
        saga_def = _make_saga_def()
        registry.get.return_value = saga_def
        execution_orchestrator.execute = AsyncMock(return_value=["s1", "s2", "s3"])

        engine = _build_engine(
            registry,
            step_invoker,
            execution_orchestrator,
            compensator,
            persistence_port=persistence_port,
        )

        result = await engine.execute("test-saga")

        persistence_port.persist_state.assert_called_once()
        persist_call_state = persistence_port.persist_state.call_args[0][0]
        assert persist_call_state["saga_name"] == "test-saga"
        assert persist_call_state["correlation_id"] == result.correlation_id

        persistence_port.mark_completed.assert_called_once_with(
            result.correlation_id,
            True,
        )

    @pytest.mark.anyio
    async def test_persist_state_and_mark_completed_on_failure(
        self,
        registry: MagicMock,
        step_invoker: AsyncMock,
        execution_orchestrator: AsyncMock,
        compensator: AsyncMock,
        persistence_port: AsyncMock,
    ) -> None:
        """Persistence port receives persist_state and mark_completed(False) on failure."""
        saga_def = _make_saga_def()
        registry.get.return_value = saga_def
        execution_orchestrator.execute = AsyncMock(
            side_effect=RuntimeError("boom"),
        )

        engine = _build_engine(
            registry,
            step_invoker,
            execution_orchestrator,
            compensator,
            persistence_port=persistence_port,
        )

        result = await engine.execute("test-saga")

        persistence_port.persist_state.assert_called_once()
        persistence_port.mark_completed.assert_called_once_with(
            result.correlation_id,
            False,
        )

    @pytest.mark.anyio
    async def test_no_persistence_port_configured(
        self,
        registry: MagicMock,
        step_invoker: AsyncMock,
        execution_orchestrator: AsyncMock,
        compensator: AsyncMock,
    ) -> None:
        """Engine works without persistence port configured (no errors)."""
        saga_def = _make_saga_def()
        registry.get.return_value = saga_def
        execution_orchestrator.execute = AsyncMock(return_value=["s1", "s2", "s3"])

        engine = _build_engine(
            registry,
            step_invoker,
            execution_orchestrator,
            compensator,
            persistence_port=None,
        )

        result = await engine.execute("test-saga")
        assert result.success is True


# ── Unknown saga ─────────────────────────────────────────────


class TestUnknownSaga:
    @pytest.mark.anyio
    async def test_unknown_saga_raises_value_error(
        self,
        registry: MagicMock,
        step_invoker: AsyncMock,
        execution_orchestrator: AsyncMock,
        compensator: AsyncMock,
    ) -> None:
        """Unknown saga name raises ValueError."""
        registry.get.return_value = None

        engine = _build_engine(
            registry,
            step_invoker,
            execution_orchestrator,
            compensator,
        )

        with pytest.raises(ValueError, match="not registered"):
            await engine.execute("non-existent-saga")


# ── Correlation ID ───────────────────────────────────────────


class TestCorrelationId:
    @pytest.mark.anyio
    async def test_custom_correlation_id_is_used(
        self,
        registry: MagicMock,
        step_invoker: AsyncMock,
        execution_orchestrator: AsyncMock,
        compensator: AsyncMock,
    ) -> None:
        """Custom correlation_id is propagated to the result."""
        saga_def = _make_saga_def()
        registry.get.return_value = saga_def
        execution_orchestrator.execute = AsyncMock(return_value=["s1", "s2", "s3"])

        engine = _build_engine(
            registry,
            step_invoker,
            execution_orchestrator,
            compensator,
        )

        result = await engine.execute(
            "test-saga",
            correlation_id="custom-correlation-123",
        )

        assert result.correlation_id == "custom-correlation-123"

    @pytest.mark.anyio
    async def test_auto_generated_correlation_id(
        self,
        registry: MagicMock,
        step_invoker: AsyncMock,
        execution_orchestrator: AsyncMock,
        compensator: AsyncMock,
    ) -> None:
        """Auto-generated correlation_id when not provided."""
        saga_def = _make_saga_def()
        registry.get.return_value = saga_def
        execution_orchestrator.execute = AsyncMock(return_value=["s1", "s2", "s3"])

        engine = _build_engine(
            registry,
            step_invoker,
            execution_orchestrator,
            compensator,
        )

        result = await engine.execute("test-saga")

        assert result.correlation_id is not None
        assert len(result.correlation_id) > 0


# ── Headers propagation ──────────────────────────────────────


class TestHeaders:
    @pytest.mark.anyio
    async def test_custom_headers_propagated(
        self,
        registry: MagicMock,
        step_invoker: AsyncMock,
        execution_orchestrator: AsyncMock,
        compensator: AsyncMock,
    ) -> None:
        """Custom headers are propagated to the result."""
        saga_def = _make_saga_def()
        registry.get.return_value = saga_def
        execution_orchestrator.execute = AsyncMock(return_value=["s1", "s2", "s3"])

        engine = _build_engine(
            registry,
            step_invoker,
            execution_orchestrator,
            compensator,
        )

        headers = {"trace-id": "abc-123", "user-id": "user-42"}
        result = await engine.execute("test-saga", headers=headers)

        assert result.headers == headers

    @pytest.mark.anyio
    async def test_default_empty_headers(
        self,
        registry: MagicMock,
        step_invoker: AsyncMock,
        execution_orchestrator: AsyncMock,
        compensator: AsyncMock,
    ) -> None:
        """Default headers is empty dict when not provided."""
        saga_def = _make_saga_def()
        registry.get.return_value = saga_def
        execution_orchestrator.execute = AsyncMock(return_value=["s1", "s2", "s3"])

        engine = _build_engine(
            registry,
            step_invoker,
            execution_orchestrator,
            compensator,
        )

        result = await engine.execute("test-saga")
        assert result.headers == {}


# ── Compensation policies ────────────────────────────────────


class TestCompensationPolicy:
    @pytest.mark.anyio
    async def test_default_strict_sequential_policy(
        self,
        registry: MagicMock,
        step_invoker: AsyncMock,
        execution_orchestrator: AsyncMock,
        compensator: AsyncMock,
    ) -> None:
        """Default compensation policy is STRICT_SEQUENTIAL."""
        saga_def = _make_saga_def()
        registry.get.return_value = saga_def
        execution_orchestrator.execute = AsyncMock(
            side_effect=RuntimeError("fail"),
        )

        engine = _build_engine(
            registry,
            step_invoker,
            execution_orchestrator,
            compensator,
        )

        await engine.execute("test-saga")

        call_kwargs = compensator.compensate.call_args[1]
        assert call_kwargs["policy"] == CompensationPolicy.STRICT_SEQUENTIAL

    @pytest.mark.anyio
    async def test_custom_compensation_policy_passed_through(
        self,
        registry: MagicMock,
        step_invoker: AsyncMock,
        execution_orchestrator: AsyncMock,
        compensator: AsyncMock,
    ) -> None:
        """Custom compensation policy is passed through to the compensator."""
        saga_def = _make_saga_def()
        registry.get.return_value = saga_def
        execution_orchestrator.execute = AsyncMock(
            side_effect=RuntimeError("fail"),
        )

        engine = _build_engine(
            registry,
            step_invoker,
            execution_orchestrator,
            compensator,
        )

        await engine.execute(
            "test-saga",
            compensation_policy=CompensationPolicy.BEST_EFFORT_PARALLEL,
        )

        call_kwargs = compensator.compensate.call_args[1]
        assert call_kwargs["policy"] == CompensationPolicy.BEST_EFFORT_PARALLEL

    @pytest.mark.anyio
    async def test_grouped_parallel_policy(
        self,
        registry: MagicMock,
        step_invoker: AsyncMock,
        execution_orchestrator: AsyncMock,
        compensator: AsyncMock,
    ) -> None:
        """GROUPED_PARALLEL policy is passed through to the compensator."""
        saga_def = _make_saga_def()
        registry.get.return_value = saga_def
        execution_orchestrator.execute = AsyncMock(
            side_effect=RuntimeError("fail"),
        )

        engine = _build_engine(
            registry,
            step_invoker,
            execution_orchestrator,
            compensator,
        )

        await engine.execute(
            "test-saga",
            compensation_policy=CompensationPolicy.GROUPED_PARALLEL,
        )

        call_kwargs = compensator.compensate.call_args[1]
        assert call_kwargs["policy"] == CompensationPolicy.GROUPED_PARALLEL


# ── Compensation results in SagaResult ───────────────────────


class TestCompensationResults:
    @pytest.mark.anyio
    async def test_compensation_results_reflected_in_steps(
        self,
        registry: MagicMock,
        step_invoker: AsyncMock,
        execution_orchestrator: AsyncMock,
        compensator: AsyncMock,
    ) -> None:
        """Compensation results and errors from context are reflected in step outcomes."""
        saga_def = _make_saga_def()
        registry.get.return_value = saga_def

        error = RuntimeError("s3 failed")

        async def _execute_side_effect(saga_def: Any, ctx: SagaContext, step_input: Any = None) -> list[str]:
            ctx.step_statuses["s1"] = StepStatus.DONE
            ctx.step_statuses["s2"] = StepStatus.DONE
            ctx.step_statuses["s3"] = StepStatus.FAILED
            ctx.step_attempts["s1"] = 1
            ctx.step_attempts["s2"] = 1
            ctx.step_attempts["s3"] = 1
            ctx.step_results["s1"] = "r1"
            ctx.step_results["s2"] = "r2"
            raise error

        async def _compensate_side_effect(**kwargs: Any) -> None:
            ctx = kwargs["ctx"]
            ctx.compensation_results["s1"] = "compensated-s1"
            ctx.compensation_results["s2"] = "compensated-s2"

        execution_orchestrator.execute = AsyncMock(side_effect=_execute_side_effect)
        compensator.compensate = AsyncMock(side_effect=_compensate_side_effect)

        engine = _build_engine(
            registry,
            step_invoker,
            execution_orchestrator,
            compensator,
        )

        result = await engine.execute("test-saga")

        assert result.success is False
        assert result.steps["s1"].compensated is True
        assert result.steps["s1"].compensation_result == "compensated-s1"
        assert result.steps["s2"].compensated is True
        assert result.steps["s2"].compensation_result == "compensated-s2"
        assert result.steps["s3"].compensated is False


# ── Timing ───────────────────────────────────────────────────


class TestTiming:
    @pytest.mark.anyio
    async def test_started_at_and_completed_at_set(
        self,
        registry: MagicMock,
        step_invoker: AsyncMock,
        execution_orchestrator: AsyncMock,
        compensator: AsyncMock,
    ) -> None:
        """SagaResult has started_at before completed_at."""
        saga_def = _make_saga_def()
        registry.get.return_value = saga_def
        execution_orchestrator.execute = AsyncMock(return_value=["s1", "s2", "s3"])

        engine = _build_engine(
            registry,
            step_invoker,
            execution_orchestrator,
            compensator,
        )

        result = await engine.execute("test-saga")

        assert result.started_at is not None
        assert result.completed_at is not None
        assert result.started_at <= result.completed_at
