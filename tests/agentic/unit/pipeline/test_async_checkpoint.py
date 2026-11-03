# Copyright 2026 Firefly Software Foundation
# SPDX-License-Identifier: Apache-2.0
"""The actual engine must await asynchronous persistence before publishing progress."""

import asyncio

import pytest

from pyfly.agentic.exceptions import PipelinePersistenceError
from pyfly.agentic.pipeline import DAG, DAGEdge, DAGNode, Pause, PipelineEngine


class AsyncMemory:
    def __init__(self):
        self.records = []
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.release.set()

    async def save(self, record):
        self.entered.set()
        await self.release.wait()
        self.records.append(record)

    async def load_latest(self, pipeline_name, run_id):
        await asyncio.sleep(0)
        return self.records[-1] if self.records else None


class Step:
    def __init__(self, value, calls, name):
        self.value, self.calls, self.name = value, calls, name

    async def execute(self, context, inputs):
        self.calls.append(self.name)
        return self.value


def engine(checkpointer, calls):
    dag = DAG("async")
    for name, value in [("a", {"answer": 7}), ("gate", Pause("approval")), ("b", "done")]:
        dag.add_node(DAGNode(node_id=name, step=Step(value, calls, name)))
    dag.add_edge(DAGEdge(source="a", target="gate"))
    dag.add_edge(DAGEdge(source="gate", target="b"))
    return PipelineEngine(dag, checkpointer=checkpointer)


async def test_save_is_awaited_before_next_node_and_pause_publication():
    store, calls = AsyncMemory(), []
    store.release.clear()
    task = asyncio.create_task(engine(store, calls).run(inputs={"question": "fixture"}, run_id="run"))
    try:
        await asyncio.wait_for(store.entered.wait(), 1)
        assert calls == ["a"] and not task.done()
        store.release.set()
        result = await task
        assert result.paused and calls == ["a", "gate"]
        assert store.records[-1].paused
        resumed = await engine(store, calls).run(run_id="run", approve_pause=True)
        assert resumed.success and calls == ["a", "gate", "b"]
        assert resumed.outputs["a"].output == {"answer": 7}
    finally:
        store.release.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_async_save_failure_is_content_free_and_prevents_downstream():
    class Broken(AsyncMemory):
        async def save(self, record):
            raise ValueError("fixture-secret-body")

    calls = []
    with pytest.raises(PipelinePersistenceError) as error:
        await engine(Broken(), calls).run(inputs="fixture")
    assert str(error.value) == "Pipeline checkpoint save failed"
    assert calls == ["a"]


async def test_async_load_failure_is_content_free_before_any_effect():
    class Broken(AsyncMemory):
        async def load_latest(self, pipeline_name, run_id):
            raise ValueError("fixture-secret-body")

    calls = []
    with pytest.raises(PipelinePersistenceError) as error:
        await engine(Broken(), calls).run(run_id="run")
    assert str(error.value) == "Pipeline checkpoint load failed"
    assert calls == []


async def test_malformed_loaded_checkpoint_is_rejected_without_serialization_warning():
    import warnings

    from pyfly.agentic.pipeline import CheckpointRecord

    store = AsyncMemory()
    calls = []
    store.records.append(
        CheckpointRecord.model_construct(
            pipeline_name={"token": "private-load-warning-canary"},
            run_id="run",
            node_id="a",
            sequence=1,
            state={"inputs": "fixture", "results": {}},
            completed_nodes=["a"],
        )
    )
    with warnings.catch_warnings(record=True) as captured:
        warnings.simplefilter("always")
        with pytest.raises(PipelinePersistenceError) as error:
            await engine(store, calls).run(run_id="run")
        assert "private-load-warning-canary" not in str(error.value)
        assert captured == []
    assert calls == []
