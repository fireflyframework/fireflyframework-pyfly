# Copyright 2026 Firefly Software Foundation
# SPDX-License-Identifier: Apache-2.0
import pytest

from pyfly.agentic.pipeline import DAG, DAGEdge, DAGNode, Pause, PipelineEngine
from pyfly.agentic.pipeline.durable import BoundDurableCheckpointer, DurableStateError, durable_graph_digest
from tests.agentic.unit.pipeline.test_durable_sql import AUTHOR, BINDING, INPUT, KEY, opened


class Agent:
    def __init__(self, name, calls):
        self.name, self.calls = name, calls

    async def execute(self, context, inputs):
        self.calls.append(self.name)
        if self.name == "a":
            return {"answer": 7}
        if self.name == "wait":
            return Pause("approve")
        return {"prior": context.get_node_result("a").output, "original": context.inputs}


def graph(calls, *, parallel=False):
    dag = DAG("flow")
    for name in ("a", "wait", "b"):
        dag.add_node(DAGNode(node_id=name, step=Agent(name, calls)))
    dag.add_edge(DAGEdge(source="a", target="wait"))
    dag.add_edge(DAGEdge(source="a" if parallel else "wait", target="b"))
    return dag


def build(checkpointer, calls, *, parallel=False):
    return PipelineEngine(graph(calls, parallel=parallel), checkpointer=checkpointer)


def binding(calls):
    return BINDING.model_copy(
        update={"graph_digest": durable_graph_digest(graph(calls)), "graph_version": "pyfly-linear-v1"}
    )


async def test_actual_engine_reopens_approved_checkpoint_without_a_replay_or_boolean(tmp_path):
    path = tmp_path / "engine.db"
    calls = []
    pinned = binding(calls)
    async with opened(path) as (store, _, _):
        await store.create(KEY, pinned, INPUT, actor=AUTHOR)
        lease = await store.claim(KEY, pinned, owner="first", expected_generation=1)
        result = await build(BoundDurableCheckpointer(store, lease), calls).run(inputs=INPUT, run_id="run")
        assert result.paused and calls == ["a", "wait"]
    async with opened(path, create=False) as (store, _, _):
        value = await store.load(KEY, pinned)
        from tests.agentic.unit.pipeline.test_durable_sql import REVIEWER

        await store.decide(
            KEY,
            pinned,
            task_id=value.task.task_id,
            expected_generation=1,
            decision="approve",
            actor=REVIEWER,
            idempotency_key="approved",
            policy_digest="c" * 64,
        )
        lease = await store.claim(KEY, pinned, owner="second", expected_generation=2)
        result = await build(BoundDurableCheckpointer(store, lease), calls).run(run_id="run")
        assert result.success and calls == ["a", "wait", "b"]
        assert result.outputs["b"].output == {"prior": {"answer": 7}, "original": INPUT}


async def test_unsupported_graph_or_untrusted_start_flags_fail_before_effect(tmp_path):
    pinned = binding([])
    async with opened(tmp_path / "denied.db") as (store, _, _):
        await store.create(KEY, pinned, INPUT, actor=AUTHOR)
        lease = await store.claim(KEY, pinned, owner="worker", expected_generation=1)
        calls = []
        cp = BoundDurableCheckpointer(store, lease)
        for kwargs in ({"approve_pause": True}, {"start_at": "b"}, {"run_id": "other"}, {"inputs": "other"}):
            with pytest.raises(DurableStateError):
                await build(cp, calls).run(**({"inputs": INPUT, "run_id": "run"} | kwargs))
        with pytest.raises(DurableStateError):
            await build(cp, calls, parallel=True).run(inputs=INPUT, run_id="run")
        assert calls == []


def test_graph_digest_normalizes_default_timeout_and_binds_edge_semantics():
    first = graph([])
    second = graph([])
    for node in second.nodes.values():
        node.timeout_seconds = 0.0
    assert durable_graph_digest(first) == durable_graph_digest(second)
    second.edges[0].input_key = "different"
    assert durable_graph_digest(first) != durable_graph_digest(second)


async def test_unbound_shared_state_or_injected_context_is_rejected_before_effect(tmp_path):
    from pydantic import BaseModel

    from pyfly.agentic.pipeline import PipelineContext

    class State(BaseModel):
        changed: bool = False

    calls = []
    pinned = binding(calls)
    async with opened(tmp_path / "state.db") as (store, _, _):
        await store.create(KEY, pinned, INPUT, actor=AUTHOR)
        lease = await store.claim(KEY, pinned, owner="worker", expected_generation=1)
        cp = BoundDurableCheckpointer(store, lease)
        for kwargs in ({"state": State()}, {"context": PipelineContext(inputs=INPUT)}):
            with pytest.raises(DurableStateError):
                await build(cp, calls).run(**({"inputs": INPUT, "run_id": "run"} | kwargs))
        with pytest.raises(DurableStateError):
            await PipelineEngine(graph(calls), checkpointer=cp, state_schema=State).run(inputs=INPUT, run_id="run")
        assert calls == []
