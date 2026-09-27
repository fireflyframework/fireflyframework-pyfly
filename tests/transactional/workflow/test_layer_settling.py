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
"""A workflow layer settles every step before it fails, and the engine drains its runs on stop (WP13-01, WP13-11).

The backpressure strategies used to ``gather`` a layer and return on the first failure, leaving the siblings
running: the workflow compensated at once and a sibling committed afterwards (C017). Here, with plain
coroutines (the committed-work side runs on real databases in
``tests/integration/test_workflow_step_commits_matrix.py``):

- a failure cancels the siblings still running and waits for them before it is raised;
- a cancellation of the layer cancels and awaits every sibling before it propagates;
- the ASYNC runs of a :class:`WorkflowEngine` are awaited by :class:`WorkflowRuns` on stop, cancelled and
  awaited when the stop is cut short, and no new one starts while it drains.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from pyfly.transactional.core.backpressure import (
    AdaptiveBackpressureStrategy,
    BackpressureStrategy,
    BatchedBackpressureStrategy,
    settle_all,
)
from pyfly.transactional.core.exceptions import OrchestrationError
from pyfly.transactional.core.model import ExecutionStatus, TriggerMode
from pyfly.transactional.workflow.annotations import workflow, workflow_step
from pyfly.transactional.workflow.engine import WorkflowEngine, WorkflowRuns
from pyfly.transactional.workflow.registry import WorkflowRegistry

STRATEGIES = [AdaptiveBackpressureStrategy(concurrency=4), BatchedBackpressureStrategy(batch_size=4)]


@pytest.mark.parametrize("strategy", STRATEGIES, ids=lambda s: s.name)
async def test_a_failure_is_raised_once_every_running_sibling_has_ended(strategy: BackpressureStrategy) -> None:
    ended: list[str] = []
    sibling_running = asyncio.Event()

    async def process(item: str) -> str:
        try:
            if item == "fail":
                await sibling_running.wait()
                raise RuntimeError("step failed")
            sibling_running.set()
            await asyncio.sleep(30)
            return item
        finally:
            await asyncio.sleep(0)  # its cleanup awaits, and still ends before the failure is raised
            ended.append(item)

    with pytest.raises(RuntimeError, match="step failed"):
        await strategy.apply(["slow", "fail"], process)
    assert sorted(ended) == ["fail", "slow"]


@pytest.mark.parametrize("strategy", STRATEGIES, ids=lambda s: s.name)
async def test_cancelling_the_layer_cancels_and_awaits_every_sibling(strategy: BackpressureStrategy) -> None:
    ended: list[str] = []
    running = asyncio.Event()

    async def process(item: str) -> str:
        running.set()
        try:
            await asyncio.sleep(30)
            return item
        finally:
            ended.append(item)

    layer = asyncio.create_task(strategy.apply(["a", "b"], process))
    await running.wait()
    layer.cancel()
    with pytest.raises(asyncio.CancelledError):
        await layer
    assert sorted(ended) == ["a", "b"]


async def test_settle_all_returns_the_results_in_order() -> None:
    async def value(n: int) -> int:
        await asyncio.sleep(0.01 * (3 - n))
        return n

    assert await settle_all([value(1), value(2), value(3)]) == [1, 2, 3]
    assert await settle_all([]) == []


async def test_a_sibling_that_finishes_its_cleanup_under_a_shield_is_awaited() -> None:
    finished = asyncio.Event()

    async def committing() -> None:
        try:
            await asyncio.sleep(30)
        finally:
            await asyncio.shield(asyncio.sleep(0.05))  # a shielded commit still landing
            finished.set()

    async def failing() -> None:
        await asyncio.sleep(0.01)
        raise ValueError("boom")

    with pytest.raises(ValueError, match="boom"):
        await settle_all([committing(), failing()])
    assert finished.is_set()


@workflow(id="drained", trigger_mode=TriggerMode.ASYNC)
class Drained:
    def __init__(self) -> None:
        self.release = asyncio.Event()
        self.ran: list[str] = []

    @workflow_step(id="load")
    async def load(self) -> None:
        try:
            await self.release.wait()
            self.ran.append("loaded")
        except asyncio.CancelledError:
            self.ran.append("cancelled")
            raise


def _engine(bean: Any) -> WorkflowEngine:
    registry = WorkflowRegistry()
    registry.register_from_bean(bean)
    return WorkflowEngine(registry=registry)


async def test_stopping_the_runs_waits_for_a_background_run() -> None:
    bean = Drained()
    engine = _engine(bean)
    runs = WorkflowRuns(engine)
    await runs.start()
    started = await engine.start("drained")
    assert started.status is ExecutionStatus.PENDING

    stopping = asyncio.create_task(runs.stop())
    await asyncio.sleep(0.02)
    assert not stopping.done()  # the run is still in flight
    bean.release.set()
    await asyncio.wait_for(stopping, 5)

    assert bean.ran == ["loaded"]
    state = await engine.get_execution(started.correlation_id)
    assert state is not None and state.status is ExecutionStatus.COMPLETED


async def test_a_stop_cut_short_cancels_and_awaits_the_runs_in_flight() -> None:
    bean = Drained()
    engine = _engine(bean)
    runs = WorkflowRuns(engine)
    started = await engine.start("drained")

    with pytest.raises(TimeoutError):
        await asyncio.wait_for(runs.stop(), 0.05)  # the context's shutdown timeout

    assert bean.ran == ["cancelled"]
    state = await engine.get_execution(started.correlation_id)
    assert state is not None and state.status is not ExecutionStatus.RUNNING


async def test_no_background_run_starts_while_the_engine_drains_and_one_does_after_a_restart() -> None:
    bean = Drained()
    engine = _engine(bean)
    runs = WorkflowRuns(engine)
    await runs.stop()

    with pytest.raises(OrchestrationError, match="stopping"):
        await engine.start("drained")

    await runs.start()
    bean.release.set()
    started = await engine.start("drained")
    await runs.stop()
    state = await engine.get_execution(started.correlation_id)
    assert state is not None and state.status is ExecutionStatus.COMPLETED
