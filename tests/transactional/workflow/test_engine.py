# Copyright 2026 Firefly Software Foundation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""End-to-end tests for the WorkflowEngine."""

from __future__ import annotations

import asyncio
from typing import Annotated

import pytest

from pyfly.transactional.core.argument import FromStep, Input
from pyfly.transactional.core.events import LoggerOrchestrationEvents
from pyfly.transactional.core.model import ExecutionStatus, TriggerMode
from pyfly.transactional.core.persistence import InMemoryPersistenceProvider
from pyfly.transactional.workflow.annotations import (
    on_workflow_complete,
    on_workflow_error,
    wait_for_signal,
    wait_for_timer,
    workflow,
    workflow_step,
)
from pyfly.transactional.workflow.engine import WorkflowEngine
from pyfly.transactional.workflow.executor import WorkflowExecutor
from pyfly.transactional.workflow.registry import WorkflowRegistry


def _make_engine() -> tuple[WorkflowEngine, WorkflowRegistry]:
    registry = WorkflowRegistry()
    engine = WorkflowEngine(
        registry=registry,
        executor=WorkflowExecutor(events=LoggerOrchestrationEvents()),
        persistence=InMemoryPersistenceProvider(),
    )
    return engine, registry


class TestSimpleWorkflow:
    @pytest.mark.asyncio
    async def test_two_step_workflow_runs_in_dependency_order(self) -> None:
        @workflow(id="orders")
        class Orders:
            @workflow_step(id="validate")
            async def validate(self, payload: Annotated[dict, Input()]) -> dict:
                return {"valid": True, **payload}

            @workflow_step(id="reserve", depends_on=["validate"])
            async def reserve(self, prev: Annotated[dict, FromStep("validate")]) -> dict:
                return {"reserved": prev["valid"]}

        engine, registry = _make_engine()
        registry.register_from_bean(Orders())
        result = await engine.start("orders", {"order": 1})
        assert result.status == ExecutionStatus.COMPLETED
        assert result.successful
        assert result.step_results["reserve"] == {"reserved": True}

    @pytest.mark.asyncio
    async def test_failing_step_marks_workflow_failed(self) -> None:
        @workflow(id="bad")
        class Bad:
            @workflow_step(id="x")
            async def x(self) -> None:
                msg = "boom"
                raise RuntimeError(msg)

        engine, registry = _make_engine()
        registry.register_from_bean(Bad())
        result = await engine.start("bad")
        assert result.status == ExecutionStatus.FAILED
        assert not result.successful


class TestSignalDriven:
    @pytest.mark.asyncio
    async def test_wait_for_signal_resumes_workflow(self) -> None:
        @workflow(id="approval")
        class Approval:
            @workflow_step(id="submit")
            async def submit(self) -> str:
                return "submitted"

            @workflow_step(id="approve", depends_on=["submit"])
            @wait_for_signal("approved")
            async def approve(self) -> str:
                return "ok"

        engine, registry = _make_engine()
        registry.register_from_bean(Approval())

        async def deliver_later() -> None:
            await asyncio.sleep(0.05)
            cids = await engine.signals.list_active()
            assert cids
            await engine.deliver_signal(cids[0], "approved", payload="manager-A")

        runner = asyncio.create_task(engine.start("approval"))
        deliverer = asyncio.create_task(deliver_later())
        result = await runner
        await deliverer
        assert result.status == ExecutionStatus.COMPLETED


class TestTimer:
    @pytest.mark.asyncio
    async def test_wait_for_timer_pauses_briefly(self) -> None:
        @workflow(id="delayed")
        class Delayed:
            @workflow_step(id="warmup")
            @wait_for_timer(delay_ms=20)
            async def warmup(self) -> str:
                return "done"

        engine, registry = _make_engine()
        registry.register_from_bean(Delayed())
        result = await engine.start("delayed")
        assert result.status == ExecutionStatus.COMPLETED


class TestLifecycle:
    @pytest.mark.asyncio
    async def test_on_complete_callback_fires(self) -> None:
        invocations: list[str] = []

        @workflow(id="cb")
        class CB:
            @workflow_step(id="s")
            async def s(self) -> str:
                return "ok"

            @on_workflow_complete
            async def done(self, ctx: object) -> None:
                invocations.append("complete")

        engine, registry = _make_engine()
        registry.register_from_bean(CB())
        await engine.start("cb")
        assert invocations == ["complete"]

    @pytest.mark.asyncio
    async def test_on_error_callback_fires_on_failure(self) -> None:
        invocations: list[str] = []

        @workflow(id="cb-error")
        class CB:
            @workflow_step(id="s")
            async def s(self) -> None:
                msg = "boom"
                raise RuntimeError(msg)

            @on_workflow_error
            async def errored(self, ctx: object, error: BaseException) -> None:
                invocations.append(str(error))

        engine, registry = _make_engine()
        registry.register_from_bean(CB())
        await engine.start("cb-error")
        assert invocations  # at least one


class TestAsyncTrigger:
    @pytest.mark.asyncio
    async def test_async_workflow_returns_immediately(self) -> None:
        @workflow(id="bg", trigger_mode=TriggerMode.ASYNC)
        class BG:
            @workflow_step(id="slow")
            async def slow(self) -> None:
                await asyncio.sleep(0.05)

        engine, registry = _make_engine()
        registry.register_from_bean(BG())
        result = await engine.start("bg")
        assert result.status == ExecutionStatus.PENDING
        # Wait for background task to finish.
        await asyncio.sleep(0.15)
