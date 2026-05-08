# Copyright 2026 Firefly Software Foundation.
# Licensed under the Apache License, Version 2.0.
"""Regression test: async saga steps must be awaited by the engine.

The ``@saga_step`` decorator used to wrap the function with a synchronous
``functools.wraps`` adapter. That made ``inspect.iscoroutinefunction``
return ``False`` for ``async def`` steps, so the engine called them
without ``await`` and the actual coroutine never ran. This test pins the
fix in place.
"""

from __future__ import annotations

import asyncio
import inspect

import pytest

from pyfly.transactional.saga import saga, saga_step
from pyfly.transactional.saga.core.context import SagaContext
from pyfly.transactional.saga.engine.argument_resolver import ArgumentResolver
from pyfly.transactional.saga.engine.compensator import SagaCompensator
from pyfly.transactional.saga.engine.execution_orchestrator import (
    SagaExecutionOrchestrator,
)
from pyfly.transactional.saga.engine.saga_engine import SagaEngine
from pyfly.transactional.saga.engine.step_invoker import StepInvoker
from pyfly.transactional.saga.registry.saga_registry import SagaRegistry


@pytest.mark.asyncio
async def test_async_saga_step_is_awaited() -> None:
    executed: list[str] = []

    @saga(name="async-saga")
    class AsyncSaga:
        @saga_step(id="step-1")
        async def step_1(self) -> str:
            await asyncio.sleep(0)
            executed.append("step-1")
            return "ok"

        @saga_step(id="step-2", depends_on=["step-1"])
        async def step_2(self, ctx: SagaContext) -> str:
            del ctx
            await asyncio.sleep(0)
            executed.append("step-2")
            return "ok"

    # Sanity: the decorator must not strip async-ness.
    assert inspect.iscoroutinefunction(AsyncSaga.step_1)
    assert inspect.iscoroutinefunction(AsyncSaga.step_2)

    registry = SagaRegistry()
    registry.register_from_bean(AsyncSaga())

    resolver = ArgumentResolver()
    invoker = StepInvoker(resolver)
    engine = SagaEngine(
        registry=registry,
        step_invoker=invoker,
        execution_orchestrator=SagaExecutionOrchestrator(invoker),
        compensator=SagaCompensator(invoker),
    )

    result = await engine.execute(saga_name="async-saga")

    assert result.success is True
    assert executed == ["step-1", "step-2"]
