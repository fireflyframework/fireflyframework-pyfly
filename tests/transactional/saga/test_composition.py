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
"""Tests for saga composition — Builder, Validator, DataFlowManager, Compositor."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock

import pytest

from pyfly.transactional.saga.composition.compensation_manager import (
    CompensationManager,
)
from pyfly.transactional.saga.composition.composition import (
    CompositionEntry,
    SagaComposition,
    SagaDataFlow,
)
from pyfly.transactional.saga.composition.composition_builder import (
    SagaCompositionBuilder,
)
from pyfly.transactional.saga.composition.composition_context import (
    CompositionContext,
)
from pyfly.transactional.saga.composition.compositor import SagaCompositor
from pyfly.transactional.saga.composition.data_flow_manager import DataFlowManager
from pyfly.transactional.saga.composition.validator import CompositionValidator
from pyfly.transactional.saga.core.result import SagaResult, StepOutcome
from pyfly.transactional.shared.types import CompensationPolicy, StepStatus

# ── Helpers ──────────────────────────────────────────────────────


def _make_saga_result(
    saga_name: str,
    *,
    success: bool = True,
    step_results: dict[str, Any] | None = None,
    correlation_id: str = "corr-123",
) -> SagaResult:
    """Build a minimal SagaResult for testing."""
    now = datetime.now(UTC)
    steps: dict[str, StepOutcome] = {}
    for step_id, result in (step_results or {}).items():
        steps[step_id] = StepOutcome(
            status=StepStatus.DONE,
            attempts=1,
            latency_ms=10.0,
            result=result,
            error=None,
            compensated=False,
            started_at=now,
            compensation_result=None,
            compensation_error=None,
        )
    return SagaResult(
        saga_name=saga_name,
        correlation_id=correlation_id,
        started_at=now,
        completed_at=now,
        success=success,
        error=None,
        headers={},
        steps=steps,
    )


# ── SagaComposition dataclass ────────────────────────────────────


class TestSagaComposition:
    def test_frozen_data_flow(self) -> None:
        flow = SagaDataFlow(source_saga="a")
        assert flow.source_saga == "a"
        assert flow.source_step is None
        assert flow.target_key is None

    def test_frozen_entry(self) -> None:
        entry = CompositionEntry(
            saga_name="reserve",
            depends_on=["validate"],
            data_flows=[SagaDataFlow(source_saga="validate", target_key="order")],
        )
        assert entry.saga_name == "reserve"
        assert len(entry.depends_on) == 1
        assert len(entry.data_flows) == 1

    def test_frozen_composition(self) -> None:
        comp = SagaComposition(name="test")
        assert comp.name == "test"
        assert comp.entries == {}
        assert comp.compensation_policy == CompensationPolicy.STRICT_SEQUENTIAL


# ── SagaCompositionBuilder ───────────────────────────────────────


class TestSagaCompositionBuilder:
    def test_builder_creates_valid_composition(self) -> None:
        composition = (
            SagaCompositionBuilder("order-fulfillment")
            .saga("reserve-inventory")
            .depends_on()
            .add()
            .saga("process-payment")
            .depends_on("reserve-inventory")
            .add()
            .saga("ship-order")
            .depends_on("process-payment")
            .add()
            .build()
        )
        assert composition.name == "order-fulfillment"
        assert len(composition.entries) == 3
        assert composition.entries["reserve-inventory"].depends_on == []
        assert composition.entries["process-payment"].depends_on == ["reserve-inventory"]
        assert composition.entries["ship-order"].depends_on == ["process-payment"]

    def test_builder_with_data_flows(self) -> None:
        composition = (
            SagaCompositionBuilder("test")
            .saga("saga-a")
            .depends_on()
            .add()
            .saga("saga-b")
            .depends_on("saga-a")
            .data_flow(
                source_saga="saga-a",
                source_step="step-1",
                target_key="input_from_a",
            )
            .add()
            .build()
        )
        entry_b = composition.entries["saga-b"]
        assert len(entry_b.data_flows) == 1
        assert entry_b.data_flows[0].source_saga == "saga-a"
        assert entry_b.data_flows[0].source_step == "step-1"
        assert entry_b.data_flows[0].target_key == "input_from_a"

    def test_builder_with_compensation_policy(self) -> None:
        composition = (
            SagaCompositionBuilder("test")
            .saga("only")
            .depends_on()
            .add()
            .compensation_policy(CompensationPolicy.GROUPED_PARALLEL)
            .build()
        )
        assert composition.compensation_policy == CompensationPolicy.GROUPED_PARALLEL

    def test_builder_multiple_data_flows(self) -> None:
        composition = (
            SagaCompositionBuilder("test")
            .saga("a")
            .depends_on()
            .add()
            .saga("b")
            .depends_on()
            .add()
            .saga("c")
            .depends_on("a", "b")
            .data_flow(source_saga="a", target_key="from_a")
            .data_flow(source_saga="b", source_step="s1", target_key="from_b")
            .add()
            .build()
        )
        entry_c = composition.entries["c"]
        assert len(entry_c.data_flows) == 2
        assert set(entry_c.depends_on) == {"a", "b"}

    def test_builder_raises_on_empty(self) -> None:
        with pytest.raises(ValueError, match="at least one saga"):
            SagaCompositionBuilder("empty").build()


# ── CompositionValidator ─────────────────────────────────────────


class TestCompositionValidator:
    def test_valid_composition(self) -> None:
        composition = (
            SagaCompositionBuilder("test").saga("a").depends_on().add().saga("b").depends_on("a").add().build()
        )
        CompositionValidator.validate(composition)

    def test_detects_cycle(self) -> None:
        # Manually build a composition with a cycle.
        composition = SagaComposition(
            name="cyclic",
            entries={
                "a": CompositionEntry(saga_name="a", depends_on=["b"]),
                "b": CompositionEntry(saga_name="b", depends_on=["a"]),
            },
        )
        with pytest.raises(ValueError, match="cycle"):
            CompositionValidator.validate(composition)

    def test_detects_missing_dependency_reference(self) -> None:
        composition = SagaComposition(
            name="missing-dep",
            entries={
                "a": CompositionEntry(saga_name="a", depends_on=["nonexistent"]),
            },
        )
        with pytest.raises(ValueError, match="nonexistent"):
            CompositionValidator.validate(composition)

    def test_detects_missing_data_flow_source(self) -> None:
        composition = SagaComposition(
            name="missing-source",
            entries={
                "a": CompositionEntry(saga_name="a"),
                "b": CompositionEntry(
                    saga_name="b",
                    depends_on=["a"],
                    data_flows=[SagaDataFlow(source_saga="ghost", target_key="x")],
                ),
            },
        )
        with pytest.raises(ValueError, match="ghost"):
            CompositionValidator.validate(composition)

    def test_self_dependency_detected_as_cycle(self) -> None:
        composition = SagaComposition(
            name="self-dep",
            entries={
                "a": CompositionEntry(saga_name="a", depends_on=["a"]),
            },
        )
        with pytest.raises(ValueError, match="cycle"):
            CompositionValidator.validate(composition)


# ── CompositionContext ───────────────────────────────────────────


class TestCompositionContext:
    def test_default_fields(self) -> None:
        ctx = CompositionContext(
            correlation_id="c-1",
            composition_name="test",
        )
        assert ctx.saga_results == {}
        assert ctx.saga_inputs == {}
        assert ctx.correlation_id == "c-1"
        assert ctx.composition_name == "test"

    def test_mutable_saga_results(self) -> None:
        ctx = CompositionContext(correlation_id="c-2", composition_name="test")
        result = _make_saga_result("my-saga")
        ctx.saga_results["my-saga"] = result
        assert ctx.saga_results["my-saga"].saga_name == "my-saga"


# ── DataFlowManager ─────────────────────────────────────────────


class TestDataFlowManager:
    def test_resolve_input_with_no_data_flows(self) -> None:
        entry = CompositionEntry(saga_name="a")
        ctx = CompositionContext(correlation_id="c", composition_name="test")
        result = DataFlowManager.resolve_input(entry, ctx, initial_input={"key": "val"})
        assert result == {"key": "val"}

    def test_resolve_input_from_saga_result(self) -> None:
        saga_result = _make_saga_result(
            "source",
            step_results={"step-1": {"item_id": 42}},
        )
        ctx = CompositionContext(correlation_id="c", composition_name="test")
        ctx.saga_results["source"] = saga_result

        entry = CompositionEntry(
            saga_name="target",
            depends_on=["source"],
            data_flows=[
                SagaDataFlow(
                    source_saga="source",
                    source_step="step-1",
                    target_key="reservation",
                ),
            ],
        )
        result = DataFlowManager.resolve_input(entry, ctx, initial_input={"base": 1})
        assert result["base"] == 1
        assert result["reservation"] == {"item_id": 42}

    def test_resolve_input_entire_saga_result(self) -> None:
        """When source_step is None, use the entire SagaResult."""
        saga_result = _make_saga_result("source")
        ctx = CompositionContext(correlation_id="c", composition_name="test")
        ctx.saga_results["source"] = saga_result

        entry = CompositionEntry(
            saga_name="target",
            depends_on=["source"],
            data_flows=[
                SagaDataFlow(source_saga="source", target_key="all_data"),
            ],
        )
        result = DataFlowManager.resolve_input(entry, ctx, initial_input=None)
        assert result["all_data"] is saga_result

    def test_resolve_input_no_target_key_merges_dict(self) -> None:
        """When target_key is None, merge the step result dict into input."""
        saga_result = _make_saga_result(
            "source",
            step_results={"s1": {"foo": "bar", "baz": 99}},
        )
        ctx = CompositionContext(correlation_id="c", composition_name="test")
        ctx.saga_results["source"] = saga_result

        entry = CompositionEntry(
            saga_name="target",
            depends_on=["source"],
            data_flows=[
                SagaDataFlow(source_saga="source", source_step="s1"),
            ],
        )
        result = DataFlowManager.resolve_input(entry, ctx, initial_input=None)
        assert result["foo"] == "bar"
        assert result["baz"] == 99


# ── CompensationManager ─────────────────────────────────────────


class TestCompensationManager:
    @pytest.mark.anyio
    async def test_compensate_completed_calls_engine_in_reverse(self) -> None:
        mock_engine = AsyncMock()
        mock_engine.execute = AsyncMock(
            return_value=_make_saga_result("a", success=True),
        )
        composition = (
            SagaCompositionBuilder("test")
            .saga("a")
            .depends_on()
            .add()
            .saga("b")
            .depends_on("a")
            .add()
            .saga("c")
            .depends_on("b")
            .add()
            .build()
        )
        ctx = CompositionContext(correlation_id="c-1", composition_name="test")
        ctx.saga_results["a"] = _make_saga_result("a")
        ctx.saga_results["b"] = _make_saga_result("b")

        manager = CompensationManager()
        await manager.compensate_completed(
            completed_sagas=["a", "b"],
            composition=composition,
            ctx=ctx,
            saga_engine=mock_engine,
        )
        # Verify compensated saga names are recorded in context.
        assert "a" in ctx.compensated_sagas
        assert "b" in ctx.compensated_sagas

    @pytest.mark.anyio
    async def test_compensate_empty_list(self) -> None:
        manager = CompensationManager()
        ctx = CompositionContext(correlation_id="c", composition_name="test")
        composition = SagaCompositionBuilder("test").saga("a").depends_on().add().build()
        await manager.compensate_completed(
            completed_sagas=[],
            composition=composition,
            ctx=ctx,
            saga_engine=AsyncMock(),
        )
        assert ctx.compensated_sagas == []


# ── SagaCompositor ───────────────────────────────────────────────


class TestSagaCompositor:
    @pytest.mark.anyio
    async def test_linear_composition(self) -> None:
        """Execute a linear composition: A -> B -> C."""
        call_order: list[str] = []

        async def mock_execute(
            saga_name: str,
            input_data: Any = None,
            headers: dict[str, str] | None = None,
            correlation_id: str | None = None,
            **kwargs: Any,
        ) -> SagaResult:
            call_order.append(saga_name)
            return _make_saga_result(
                saga_name,
                step_results={"main": f"result-{saga_name}"},
            )

        mock_engine = AsyncMock()
        mock_engine.execute = AsyncMock(side_effect=mock_execute)

        composition = (
            SagaCompositionBuilder("linear")
            .saga("a")
            .depends_on()
            .add()
            .saga("b")
            .depends_on("a")
            .add()
            .saga("c")
            .depends_on("b")
            .add()
            .build()
        )

        compositor = SagaCompositor(saga_engine=mock_engine)
        ctx = await compositor.execute(composition, initial_input={"key": "val"})

        assert call_order == ["a", "b", "c"]
        assert ctx.saga_results["a"].success is True
        assert ctx.saga_results["b"].success is True
        assert ctx.saga_results["c"].success is True
        assert ctx.composition_name == "linear"

    @pytest.mark.anyio
    async def test_parallel_composition(self) -> None:
        """Execute a composition where A and B run in parallel, then C."""
        call_log: list[str] = []

        async def mock_execute(
            saga_name: str,
            input_data: Any = None,
            headers: dict[str, str] | None = None,
            correlation_id: str | None = None,
            **kwargs: Any,
        ) -> SagaResult:
            call_log.append(saga_name)
            return _make_saga_result(saga_name)

        mock_engine = AsyncMock()
        mock_engine.execute = AsyncMock(side_effect=mock_execute)

        composition = (
            SagaCompositionBuilder("parallel")
            .saga("a")
            .depends_on()
            .add()
            .saga("b")
            .depends_on()
            .add()
            .saga("c")
            .depends_on("a", "b")
            .add()
            .build()
        )

        compositor = SagaCompositor(saga_engine=mock_engine)
        ctx = await compositor.execute(composition)

        # A and B should execute before C (order of A/B relative to each other is not defined).
        assert set(call_log[:2]) == {"a", "b"}
        assert call_log[2] == "c"
        assert "c" in ctx.saga_results

    @pytest.mark.anyio
    async def test_failure_records_error_in_context(self) -> None:
        """On failure the context should record the error and completed sagas."""
        _error = RuntimeError("payment failed")

        async def mock_execute(
            saga_name: str,
            input_data: Any = None,
            headers: dict[str, str] | None = None,
            correlation_id: str | None = None,
            **kwargs: Any,
        ) -> SagaResult:
            if saga_name == "b":
                return _make_saga_result(saga_name, success=False)
            return _make_saga_result(saga_name)

        mock_engine = AsyncMock()
        mock_engine.execute = AsyncMock(side_effect=mock_execute)

        composition = (
            SagaCompositionBuilder("failing")
            .saga("a")
            .depends_on()
            .add()
            .saga("b")
            .depends_on("a")
            .add()
            .saga("c")
            .depends_on("b")
            .add()
            .build()
        )

        compositor = SagaCompositor(saga_engine=mock_engine)
        ctx = await compositor.execute(composition)

        assert ctx.saga_results["a"].success is True
        assert ctx.saga_results["b"].success is False
        # c should not have been reached
        assert "c" not in ctx.saga_results

    @pytest.mark.anyio
    async def test_failure_with_exception_triggers_compensation(self) -> None:
        """When a saga raises an exception, compositor should compensate completed sagas."""

        async def mock_execute(
            saga_name: str,
            input_data: Any = None,
            headers: dict[str, str] | None = None,
            correlation_id: str | None = None,
            **kwargs: Any,
        ) -> SagaResult:
            if saga_name == "b":
                raise RuntimeError("saga b exploded")
            return _make_saga_result(saga_name)

        mock_engine = AsyncMock()
        mock_engine.execute = AsyncMock(side_effect=mock_execute)

        composition = (
            SagaCompositionBuilder("exploding").saga("a").depends_on().add().saga("b").depends_on("a").add().build()
        )

        compositor = SagaCompositor(saga_engine=mock_engine)
        ctx = await compositor.execute(composition)

        assert "a" in ctx.saga_results
        assert ctx.error is not None
        assert "saga b exploded" in str(ctx.error)

    @pytest.mark.anyio
    async def test_headers_passed_through(self) -> None:
        """Headers should be forwarded to each saga."""
        captured_headers: list[dict[str, str] | None] = []

        async def mock_execute(
            saga_name: str,
            input_data: Any = None,
            headers: dict[str, str] | None = None,
            correlation_id: str | None = None,
            **kwargs: Any,
        ) -> SagaResult:
            captured_headers.append(headers)
            return _make_saga_result(saga_name)

        mock_engine = AsyncMock()
        mock_engine.execute = AsyncMock(side_effect=mock_execute)

        composition = SagaCompositionBuilder("headers-test").saga("a").depends_on().add().build()

        compositor = SagaCompositor(saga_engine=mock_engine)
        await compositor.execute(
            composition,
            headers={"trace-id": "t-123"},
        )

        assert captured_headers[0] == {"trace-id": "t-123"}

    @pytest.mark.anyio
    async def test_data_flow_between_sagas(self) -> None:
        """Compositor resolves data flows when executing dependent sagas."""
        captured_inputs: dict[str, Any] = {}

        async def mock_execute(
            saga_name: str,
            input_data: Any = None,
            headers: dict[str, str] | None = None,
            correlation_id: str | None = None,
            **kwargs: Any,
        ) -> SagaResult:
            captured_inputs[saga_name] = input_data
            return _make_saga_result(
                saga_name,
                step_results={"main": {"val": f"from-{saga_name}"}},
            )

        mock_engine = AsyncMock()
        mock_engine.execute = AsyncMock(side_effect=mock_execute)

        composition = (
            SagaCompositionBuilder("data-flow-test")
            .saga("a")
            .depends_on()
            .add()
            .saga("b")
            .depends_on("a")
            .data_flow(source_saga="a", source_step="main", target_key="a_data")
            .add()
            .build()
        )

        compositor = SagaCompositor(saga_engine=mock_engine)
        await compositor.execute(composition, initial_input={"base": 1})

        assert captured_inputs["a"] == {"base": 1}
        assert captured_inputs["b"]["base"] == 1
        assert captured_inputs["b"]["a_data"] == {"val": "from-a"}
