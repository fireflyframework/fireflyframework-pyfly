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
"""Regression tests for workflow audit fixes (#58, #59, #60, #61)."""

from __future__ import annotations

import asyncio

import pytest

from pyfly.transactional.core.events import LoggerOrchestrationEvents
from pyfly.transactional.core.model import ExecutionStatus
from pyfly.transactional.core.persistence import InMemoryPersistenceProvider
from pyfly.transactional.workflow.annotations import on_workflow_error, workflow, workflow_step
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


@pytest.mark.asyncio
async def test_step_condition_false_skips_step() -> None:
    ran: list[str] = []

    @workflow(id="condwf")
    class CondWf:
        @workflow_step(id="always")
        async def always(self) -> str:
            ran.append("always")
            return "ran"

        @workflow_step(id="maybe", depends_on=["always"], condition="results['always'] == 'nope'")
        async def maybe(self) -> str:
            ran.append("maybe")
            return "x"

    engine, registry = _make_engine()
    registry.register_from_bean(CondWf())
    result = await engine.start("condwf")

    assert result.status == ExecutionStatus.COMPLETED
    assert ran == ["always"]  # 'maybe' was skipped by its condition


@pytest.mark.asyncio
async def test_on_workflow_error_suppress_completes() -> None:
    @workflow(id="supwf")
    class SupWf:
        @workflow_step(id="boom")
        async def boom(self) -> None:
            raise RuntimeError("boom")

        @on_workflow_error(suppress_error=True)
        def handle(self, ctx: object, error: BaseException) -> None: ...

    engine, registry = _make_engine()
    registry.register_from_bean(SupWf())
    result = await engine.start("supwf")

    assert result.status == ExecutionStatus.COMPLETED  # FAILED downgraded by suppress_error
    assert result.successful


@pytest.mark.asyncio
async def test_on_workflow_error_suppress_filtered_by_type() -> None:
    @workflow(id="supwf2")
    class SupWf2:
        @workflow_step(id="boom")
        async def boom(self) -> None:
            raise RuntimeError("boom")

        @on_workflow_error(suppress_error=True, error_types=("KeyError",))
        def handle(self, ctx: object, error: BaseException) -> None: ...

    engine, registry = _make_engine()
    registry.register_from_bean(SupWf2())
    result = await engine.start("supwf2")

    # RuntimeError is not in error_types → not suppressed.
    assert result.status == ExecutionStatus.FAILED


@pytest.mark.asyncio
async def test_async_step_is_fire_and_forget() -> None:
    done = asyncio.Event()

    @workflow(id="asyncwf")
    class AsyncWf:
        @workflow_step(id="bg", async_=True)
        async def bg(self) -> str:
            await asyncio.sleep(0.02)
            done.set()
            return "bg"

    engine, registry = _make_engine()
    registry.register_from_bean(AsyncWf())
    result = await engine.start("asyncwf")

    assert result.status == ExecutionStatus.COMPLETED
    # The async step body had not necessarily finished when the workflow
    # completed; it runs to completion in the background.
    await asyncio.wait_for(done.wait(), timeout=1.0)


def test_workflow_level_retry_propagates_to_steps() -> None:
    @workflow(id="retrywf", max_retries=3, retry_delay_ms=50)
    class RetryWf:
        @workflow_step(id="a")
        async def a(self) -> str:  # no explicit retry
            return "a"

        @workflow_step(id="b", max_retries=1)
        async def b(self) -> str:  # explicit retry kept
            return "b"

    registry = WorkflowRegistry()
    definition = registry.register_from_bean(RetryWf())

    assert definition.steps["a"].max_retries == 3  # inherited from workflow
    assert definition.steps["a"].retry_delay_ms == 50
    assert definition.steps["b"].max_retries == 1  # explicit value preserved
