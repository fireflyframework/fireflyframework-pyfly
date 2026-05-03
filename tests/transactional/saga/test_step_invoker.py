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
"""Tests for saga step invoker — method invocation with argument resolution."""

from __future__ import annotations

import threading
from typing import Annotated, Any

import pytest

from pyfly.transactional.saga.annotations import FromStep, Input, SetVariable
from pyfly.transactional.saga.core.context import SagaContext
from pyfly.transactional.saga.engine.argument_resolver import ArgumentResolver
from pyfly.transactional.saga.engine.step_invoker import StepInvoker
from pyfly.transactional.saga.registry.step_definition import StepDefinition

# ── Helpers ──────────────────────────────────────────────────


class _FakeBean:
    """Dummy saga class used as the ``self`` receiver in test methods."""


def _make_step_def(
    step_method: Any = None,
    compensate_method: Any = None,
    cpu_bound: bool = False,
    step_id: str = "test-step",
) -> StepDefinition:
    return StepDefinition(
        id=step_id,
        step_method=step_method,
        compensate_method=compensate_method,
        cpu_bound=cpu_bound,
    )


@pytest.fixture
def resolver() -> ArgumentResolver:
    return ArgumentResolver()


@pytest.fixture
def invoker(resolver: ArgumentResolver) -> StepInvoker:
    return StepInvoker(argument_resolver=resolver)


@pytest.fixture
def ctx() -> SagaContext:
    return SagaContext(
        saga_name="test-saga",
        step_results={"validate": {"valid": True}},
    )


@pytest.fixture
def bean() -> _FakeBean:
    return _FakeBean()


# ── Invoke step ──────────────────────────────────────────────


class TestInvokeStep:
    @pytest.mark.anyio
    async def test_invoke_simple_async_step(self, invoker: StepInvoker, ctx: SagaContext, bean: _FakeBean) -> None:
        """Invoke a simple async step method and return its result."""

        async def process(self: Any, context: SagaContext) -> str:
            return f"done-{context.saga_name}"

        step_def = _make_step_def(step_method=process)
        result = await invoker.invoke_step(step_def, bean, ctx)
        assert result == "done-test-saga"

    @pytest.mark.anyio
    async def test_invoke_step_with_annotated_params(
        self, invoker: StepInvoker, ctx: SagaContext, bean: _FakeBean
    ) -> None:
        """Invoke a step that uses Input, FromStep, and SagaContext annotations."""

        async def process(
            self: Any,
            data: Annotated[dict, Input()],
            prev: Annotated[dict, FromStep("validate")],
            context: SagaContext,
        ) -> dict:
            return {"data": data, "prev": prev, "saga": context.saga_name}

        step_def = _make_step_def(step_method=process)
        result = await invoker.invoke_step(step_def, bean, ctx, step_input={"order_id": "o1"})
        assert result == {
            "data": {"order_id": "o1"},
            "prev": {"valid": True},
            "saga": "test-saga",
        }

    @pytest.mark.anyio
    async def test_invoke_sync_step(self, invoker: StepInvoker, ctx: SagaContext, bean: _FakeBean) -> None:
        """Invoke a synchronous (non-async) step method."""

        def process(self: Any, context: SagaContext) -> str:
            return f"sync-{context.saga_name}"

        step_def = _make_step_def(step_method=process)
        result = await invoker.invoke_step(step_def, bean, ctx)
        assert result == "sync-test-saga"

    @pytest.mark.anyio
    async def test_step_exception_propagates(self, invoker: StepInvoker, ctx: SagaContext, bean: _FakeBean) -> None:
        """An exception thrown by a step method propagates to the caller."""

        async def failing_step(self: Any, context: SagaContext) -> None:
            raise RuntimeError("step failed")

        step_def = _make_step_def(step_method=failing_step)
        with pytest.raises(RuntimeError, match="step failed"):
            await invoker.invoke_step(step_def, bean, ctx)

    @pytest.mark.anyio
    async def test_cpu_bound_step_runs_in_executor(
        self, invoker: StepInvoker, ctx: SagaContext, bean: _FakeBean
    ) -> None:
        """A cpu_bound step runs in a thread pool executor (different thread)."""
        main_thread_id = threading.current_thread().ident

        def heavy_work(self: Any, context: SagaContext) -> int:
            return threading.current_thread().ident  # type: ignore[return-value]

        step_def = _make_step_def(step_method=heavy_work, cpu_bound=True)
        worker_thread_id = await invoker.invoke_step(step_def, bean, ctx)
        assert worker_thread_id != main_thread_id


# ── Invoke compensation ──────────────────────────────────────


class TestInvokeCompensation:
    @pytest.mark.anyio
    async def test_invoke_compensation(self, invoker: StepInvoker, ctx: SagaContext, bean: _FakeBean) -> None:
        """Invoke a compensation method and return its result."""

        async def rollback(self: Any, context: SagaContext) -> str:
            return f"rolled-back-{context.saga_name}"

        step_def = _make_step_def(compensate_method=rollback)
        result = await invoker.invoke_compensation(step_def, bean, ctx)
        assert result == "rolled-back-test-saga"

    @pytest.mark.anyio
    async def test_invoke_compensation_no_method_raises(
        self, invoker: StepInvoker, ctx: SagaContext, bean: _FakeBean
    ) -> None:
        """Invoking compensation when no compensate_method is defined raises ValueError."""
        step_def = _make_step_def(compensate_method=None)
        with pytest.raises(ValueError, match="no compensation method"):
            await invoker.invoke_compensation(step_def, bean, ctx)

    @pytest.mark.anyio
    async def test_compensation_exception_propagates(
        self, invoker: StepInvoker, ctx: SagaContext, bean: _FakeBean
    ) -> None:
        """An exception thrown by a compensation method propagates to the caller."""

        async def bad_rollback(self: Any, context: SagaContext) -> None:
            raise RuntimeError("compensation failed")

        step_def = _make_step_def(compensate_method=bad_rollback)
        with pytest.raises(RuntimeError, match="compensation failed"):
            await invoker.invoke_compensation(step_def, bean, ctx)

    @pytest.mark.anyio
    async def test_invoke_sync_compensation(self, invoker: StepInvoker, ctx: SagaContext, bean: _FakeBean) -> None:
        """Invoke a synchronous compensation method."""

        def rollback(self: Any, context: SagaContext) -> str:
            return "sync-rollback"

        step_def = _make_step_def(compensate_method=rollback)
        result = await invoker.invoke_compensation(step_def, bean, ctx)
        assert result == "sync-rollback"


# ── SetVariable handling ─────────────────────────────────────


class TestSetVariableHandling:
    @pytest.mark.anyio
    async def test_set_variable_stored_in_context(
        self, invoker: StepInvoker, ctx: SagaContext, bean: _FakeBean
    ) -> None:
        """After step execution, SetVariable-annotated params store result in context."""

        async def step(
            self: Any,
            context: SagaContext,
            out: Annotated[str, SetVariable("output_key")],
        ) -> str:
            return "computed-value"

        step_def = _make_step_def(step_method=step)
        result = await invoker.invoke_step(step_def, bean, ctx)
        assert result == "computed-value"
        assert ctx.variables.get("output_key") == "computed-value"

    @pytest.mark.anyio
    async def test_multiple_set_variables(self, invoker: StepInvoker, ctx: SagaContext, bean: _FakeBean) -> None:
        """Multiple SetVariable markers all store the step result in context."""

        async def step(
            self: Any,
            a: Annotated[str, SetVariable("key_a")],
            b: Annotated[str, SetVariable("key_b")],
        ) -> str:
            return "multi-result"

        step_def = _make_step_def(step_method=step)
        result = await invoker.invoke_step(step_def, bean, ctx)
        assert result == "multi-result"
        assert ctx.variables["key_a"] == "multi-result"
        assert ctx.variables["key_b"] == "multi-result"
