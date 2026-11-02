# Copyright 2026 Firefly Software Foundation
# SPDX-License-Identifier: Apache-2.0
"""Persistence failures cannot admit downstream effects or partial restores."""

from copy import deepcopy

import pytest
from pydantic import BaseModel

from pyfly.agentic.exceptions import PipelineError
from pyfly.agentic.pipeline import CheckpointRecord, Pause, PipelineBuilder, Send

CANARY = "private-storage-content-do-not-publish"


class State(BaseModel):
    value: int = 0


class Store:
    def __init__(self, *, record=None, save_error=None, load_error=None):
        self.record = record
        self.save_error = save_error
        self.load_error = load_error
        self.saved = []

    def save(self, record):
        if self.save_error:
            raise self.save_error
        self.saved.append(record)
        self.record = record

    def load_latest(self, pipeline_name, run_id):
        if self.load_error:
            raise self.load_error
        return self.record

    def list_runs(self, pipeline_name):
        return []


@pytest.mark.parametrize("shape", ["linear", "parallel", "cycle", "pause"])
async def test_failed_checkpoint_stops_before_next_effect(shape, caplog):
    calls = []
    store = Store(save_error=OSError(CANARY))

    async def first(state):
        calls.append("first")
        if shape == "pause":
            return Pause("wait")
        if shape == "send":
            return Send("worker", {})
        return {"value": 1}

    async def worker(state):
        calls.append("worker")
        return {"value": 2}

    async def last(state):
        calls.append("last")
        return {}

    builder = PipelineBuilder("integrity", state=State, checkpointer=store).add_node(first).add_node(last)
    if shape == "cycle":
        builder.branch(first, lambda state: "first" if state.value < 2 else "last")
    elif shape == "parallel" or shape == "send":
        builder.add_node(worker).add_edge(first, worker).add_edge(worker, last)
    else:
        builder.add_edge(first, last)
    with pytest.raises(PipelineError, match="^Pipeline checkpoint save failed$") as error:
        await builder.build().run(state=State())
    assert type(error.value).__name__ == "PipelinePersistenceError"
    assert error.value.retryable is False
    assert "last" not in calls
    assert calls.count("first") == 1
    assert CANARY not in caplog.text


async def test_checkpoint_load_failure_is_fixed_nonretryable_and_precedes_effects(caplog):
    calls = []

    async def node(state):
        calls.append(1)
        return {}

    engine = (
        PipelineBuilder("integrity", state=State, checkpointer=Store(load_error=OSError(CANARY))).add_node(node).build()
    )
    with pytest.raises(PipelineError, match="^Pipeline checkpoint load failed$") as error:
        await engine.run(run_id="run")
    assert error.value.retryable is False
    assert calls == []
    assert CANARY not in caplog.text


def record():
    return CheckpointRecord(
        pipeline_name="integrity",
        run_id="run",
        node_id="first",
        sequence=1,
        completed_nodes=["first"],
        state={
            "inputs": "original",
            "shared_state": {"value": 1},
            "results": {"first": {"node_id": "first", "output": {"value": 1}, "success": True}},
        },
    )


@pytest.mark.parametrize(
    "case",
    [
        "pipeline",
        "run",
        "unknown",
        "duplicate",
        "missing-result",
        "missing-output",
        "missing-success",
        "extra-result",
        "malformed-result",
        "wrong-node",
        "failed-result",
        "skipped-result",
        "missing-state",
        "bad-state",
        "bad-sequence",
        "bad-terminal",
        "pause-mismatch",
    ],
)
async def test_invalid_restore_is_rejected_whole_before_any_effect(case, caplog):
    saved = deepcopy(record())
    if case == "pipeline":
        saved.pipeline_name = "other"
    elif case == "run":
        saved.run_id = "other"
    elif case == "unknown":
        saved.completed_nodes = ["unknown"]
    elif case == "duplicate":
        saved.completed_nodes = ["first", "first"]
    elif case == "missing-result":
        saved.state["results"] = {}
    elif case == "missing-output":
        saved.state["results"]["first"].pop("output")
    elif case == "missing-success":
        saved.state["results"]["first"].pop("success")
    elif case == "extra-result":
        saved.state["results"]["last"] = {"node_id": "last"}
    elif case == "malformed-result":
        saved.state["results"]["first"] = CANARY
    elif case == "wrong-node":
        saved.state["results"]["first"]["node_id"] = "last"
    elif case == "failed-result":
        saved.state["results"]["first"]["success"] = False
    elif case == "skipped-result":
        saved.state["results"]["first"]["skipped"] = True
    elif case == "missing-state":
        saved.state.pop("shared_state")
    elif case == "bad-state":
        saved.state["shared_state"] = {"value": CANARY}
    elif case == "bad-sequence":
        saved.sequence = -1
    elif case == "bad-terminal":
        saved.node_id = "last"
    else:
        saved.paused = True
        saved.pause_reason = None
    calls = []

    async def first(state):
        calls.append("first")
        return {}

    async def last(state):
        calls.append("last")
        return {}

    engine = (
        PipelineBuilder("integrity", state=State, checkpointer=Store(record=saved))
        .add_node(first)
        .add_node(last)
        .chain(first, last)
        .build()
    )
    with pytest.raises(PipelineError, match="^Pipeline checkpoint is invalid$") as error:
        await engine.run(run_id="run", approve_pause=True)
    assert error.value.retryable is False
    assert calls == []
    assert CANARY not in caplog.text


async def test_valid_resume_restores_original_input_and_state_without_repeating_effect():
    calls = []

    async def first(state):
        calls.append("first")
        return {}

    async def last(state):
        calls.append(state.value)
        return {"value": state.value + 1}

    engine = (
        PipelineBuilder("integrity", state=State, checkpointer=Store(record=record()))
        .add_node(first)
        .add_node(last)
        .chain(first, last)
        .build()
    )
    result = await engine.run(run_id="run")
    assert result.success and result.state.value == 2
    assert calls == [1]


@pytest.mark.parametrize("routed", [False, True])
async def test_checkpointed_dynamic_send_rejects_before_workers_without_replay_identity(routed):
    calls = []

    async def first(state):
        return {} if routed else Send("worker", {})

    async def worker(state):
        calls.append("worker")
        return {}

    builder = PipelineBuilder("fanout", state=State, checkpointer=Store()).add_node(first).add_node(worker)
    if routed:
        builder.branch(first, lambda state: Send("worker", {}))
    else:
        builder.add_edge(first, worker)
    with pytest.raises(PipelineError, match="^Checkpointed dynamic fan-out is unsupported$") as error:
        await builder.build().run(state=State())
    assert error.value.retryable is False
    assert calls == []


async def test_failed_pause_is_not_announced_as_persisted():
    pauses = []

    class Events:
        async def on_node_pause(self, **kwargs):
            pauses.append(kwargs)

    async def gate(state):
        return Pause("wait")

    engine = (
        PipelineBuilder("pause", state=State, checkpointer=Store(save_error=OSError(CANARY)), event_handler=Events())
        .add_node(gate)
        .build()
    )
    with pytest.raises(PipelineError):
        await engine.run(state=State())
    assert pauses == []


async def test_failed_checkpoint_cancels_and_reaps_already_started_parallel_work():
    import asyncio

    started, cancelled = asyncio.Event(), asyncio.Event()
    effects = []

    async def first(state):
        await started.wait()
        effects.append("first")
        return {}

    async def concurrent(state):
        started.set()
        try:
            await asyncio.Future()
        finally:
            cancelled.set()

    async def downstream(state):
        effects.append("downstream")
        return {}

    engine = (
        PipelineBuilder("parallel", state=State, checkpointer=Store(save_error=OSError(CANARY)))
        .add_node(first)
        .add_node(concurrent)
        .add_node(downstream)
        .add_edge(first, downstream)
        .build()
    )
    with pytest.raises(PipelineError, match="^Pipeline checkpoint save failed$"):
        await asyncio.wait_for(engine.run(state=State()), 2)
    assert cancelled.is_set()
    assert effects == ["first"]


async def test_cancelled_save_propagates_and_never_schedules_successor():
    import asyncio

    calls = []

    async def first(state):
        calls.append("first")
        return {}

    async def last(state):
        calls.append("last")
        return {}

    engine = (
        PipelineBuilder("cancel", state=State, checkpointer=Store(save_error=asyncio.CancelledError()))
        .add_node(first)
        .add_node(last)
        .chain(first, last)
        .build()
    )
    with pytest.raises(asyncio.CancelledError):
        await engine.run(state=State())
    assert calls == ["first"]


@pytest.mark.parametrize(
    "payload", ["not-json", '{"sequence":true}', '{"pipeline_name":"private-storage-content-do-not-publish"}']
)
async def test_corrupt_file_load_has_no_payload_in_error_or_log(tmp_path, payload, caplog):
    from pyfly.agentic.pipeline import FileCheckpointer

    directory = tmp_path / "integrity" / "run"
    directory.mkdir(parents=True)
    (directory / "000001_first.json").write_text(payload)

    async def first(state):
        pytest.fail("No effects on invalid restore")

    engine = PipelineBuilder("integrity", state=State, checkpointer=FileCheckpointer(tmp_path)).add_node(first).build()
    with pytest.raises(PipelineError, match="^Pipeline checkpoint load failed$") as error:
        await engine.run(run_id="run")
    assert error.value.__cause__ is None
    assert CANARY not in caplog.text


async def test_missing_checkpoint_is_distinct_from_storage_failure():
    async def first(state):
        pytest.fail("No effects without a checkpoint")

    engine = PipelineBuilder("missing", state=State, checkpointer=Store()).add_node(first).build()
    with pytest.raises(PipelineError, match="^No checkpoint found for requested run$") as error:
        await engine.run(run_id="run")
    assert type(error.value) is PipelineError


async def test_file_checkpoint_does_not_coerce_invalid_persistence_metadata(tmp_path):
    import json

    from pyfly.agentic.pipeline import FileCheckpointer

    directory = tmp_path / "integrity" / "run"
    directory.mkdir(parents=True)
    payload = record().model_dump(mode="json")
    payload["sequence"] = True
    (directory / "000001_first.json").write_text(json.dumps(payload))

    async def first(state):
        pytest.fail("Must not rerun a malformed checkpoint")

    engine = PipelineBuilder("integrity", state=State, checkpointer=FileCheckpointer(tmp_path)).add_node(first).build()
    with pytest.raises(PipelineError, match="^Pipeline checkpoint load failed$"):
        await engine.run(run_id="run")


async def test_durably_paused_run_never_announces_successful_pipeline_completion():
    events = []

    class Events:
        async def on_pipeline_complete(self, success):
            events.append(success)

    async def gate(state):
        return Pause("wait")

    engine = PipelineBuilder("paused", state=State, checkpointer=Store(), event_handler=Events()).add_node(gate).build()
    result = await engine.run(state=State())
    assert result.paused and not result.success
    assert events == [False]


async def test_explicit_null_output_is_a_valid_saved_result():
    saved = record()
    saved.state["results"]["first"]["output"] = None
    calls = []

    async def first(state):
        calls.append("must-not-run")
        return {}

    engine = PipelineBuilder("integrity", state=State, checkpointer=Store(record=saved)).add_node(first).build()
    assert (await engine.run(run_id="run")).success
    assert calls == []


async def test_start_at_checkpoint_need_not_include_ancestor_results(tmp_path):
    from pyfly.agentic.pipeline import FileCheckpointer

    calls = []

    async def first(state):
        calls.append("first")
        return {}

    async def last(state):
        calls.append("last")
        return {"value": 2}

    store = FileCheckpointer(tmp_path)
    engine = (
        PipelineBuilder("partial", state=State, checkpointer=store)
        .add_node(first)
        .add_node(last)
        .chain(first, last)
        .build()
    )
    initial = await engine.run(state=State(value=1), start_at="last")
    saved = store.load_latest("partial", initial.run_id)
    assert saved.completed_nodes == ["last"]
    assert set(saved.state["results"]) == {"last"}
    assert (await engine.run(run_id=initial.run_id)).success
    assert calls == ["last"]


def test_cycle_pause_resume_uses_saved_frontier_without_spinning_or_skipping_revisits(tmp_path):
    import os
    import subprocess
    import sys
    import textwrap

    code = textwrap.dedent("""
        import asyncio, sys
        from pydantic import BaseModel
        from pyfly.agentic.pipeline import PipelineBuilder, FileCheckpointer, Pause
        class State(BaseModel):
            value: int = 0
        counts = {"a":0,"b":0,"end":0}
        async def a(state):
            counts["a"] += 1
            return {"value":state.value+1}
        async def b(state):
            counts["b"] += 1
            return Pause("approve") if state.value == 1 else {}
        async def end(state):
            counts["end"] += 1
            return {}
        def build():
            return (PipelineBuilder("cycle",state=State,checkpointer=FileCheckpointer(sys.argv[1]))
                .add_node(a).add_node(b).add_node(end).branch(a,lambda state:"b")
                .branch(b,lambda state:"a" if state.value<3 else "end").build())
        async def main():
            first = await build().run(state=State())
            assert first.paused
            second = await build().run(run_id=first.run_id,approve_pause=True)
            assert second.success and second.state.value == 3
            assert counts == {"a":3,"b":3,"end":1}, counts
            partial = await build().run(state=State(value=3),start_at="end")
            assert partial.success and counts == {"a":3,"b":3,"end":2}
        asyncio.run(main())
    """)
    try:
        result = subprocess.run(
            [sys.executable, "-B", "-c", code, str(tmp_path)],
            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except subprocess.TimeoutExpired:
        pytest.fail("Cyclic resume did not terminate within the owned child deadline")
    assert result.returncode == 0, result.stderr


async def test_legacy_ambiguous_missing_ancestor_rejects_before_new_effects():
    saved = record()
    saved.node_id = "last"
    saved.completed_nodes = ["last"]
    saved.state["results"] = {"last": {"node_id": "last", "output": None, "success": True}}
    calls = []

    async def first(state):
        calls.append("unexpected")
        return {}

    async def last(state):
        calls.append("last")
        return {}

    engine = (
        PipelineBuilder("integrity", state=State, checkpointer=Store(record=saved))
        .add_node(first)
        .add_node(last)
        .chain(first, last)
        .build()
    )
    with pytest.raises(PipelineError, match="^Pipeline checkpoint is invalid$"):
        await engine.run(run_id="run")
    assert calls == []


async def test_execution_start_survives_failures_and_fresh_reopens_without_ancestor_effects(tmp_path):
    from pyfly.agentic.pipeline import FileCheckpointer

    calls = {"a": 0, "b": 0, "c": 0, "d": 0}

    async def a(state):
        calls["a"] += 1
        return {}

    async def b(state):
        calls["b"] += 1
        return {}

    async def c(state):
        calls["c"] += 1
        if calls["c"] == 1:
            raise RuntimeError("first counted failure")
        return {}

    async def d(state):
        calls["d"] += 1
        if calls["d"] == 1:
            raise RuntimeError("second counted failure")
        return {}

    store = FileCheckpointer(tmp_path)

    def build():
        return (
            PipelineBuilder("start", state=State, checkpointer=store)
            .add_node(a)
            .add_node(b)
            .add_node(c)
            .add_node(d)
            .chain(a, b, c, d)
            .build()
        )

    first = await build().run(state=State(), start_at="b")
    assert not first.success and first.failed_node == "c"
    second = await build().run(run_id=first.run_id)
    assert not second.success and second.failed_node == "d"
    third = await build().run(run_id=first.run_id)
    assert third.success
    assert calls == {"a": 0, "b": 1, "c": 2, "d": 2}
    assert store.load_latest("start", first.run_id).execution_start == "b"


async def test_ambiguous_conditional_skip_cannot_become_a_new_effect_on_resume(tmp_path):
    from pyfly.agentic.pipeline import DAGEdge, FileCheckpointer, PipelineEngine

    calls = []

    async def a(state):
        return {"value": 1}

    async def b(state):
        calls.append("previously inactive")
        return {}

    async def c(state):
        return {"value": 0}

    store = FileCheckpointer(tmp_path)
    dag = PipelineBuilder("branch", state=State).add_node(a).add_node(b).add_node(c).add_edge(a, c).build_dag()
    dag.add_edge(DAGEdge(source="a", target="b", condition=lambda ctx: ctx.state.value == 0))
    first = await PipelineEngine(dag, state_schema=State, checkpointer=store).run(state=State())
    assert first.success and calls == []
    with pytest.raises(PipelineError, match="^Pipeline checkpoint is invalid$"):
        await PipelineEngine(dag, state_schema=State, checkpointer=store).run(run_id=first.run_id)
    assert calls == []


@pytest.mark.parametrize("start", ["unknown", "last"])
async def test_invalid_execution_start_metadata_rejects_before_effects(tmp_path, start):
    import json

    from pyfly.agentic.pipeline import FileCheckpointer

    path = tmp_path / "integrity" / "run"
    path.mkdir(parents=True)
    payload = record().model_dump(mode="json")
    payload["execution_start"] = start
    (path / "000001_first.json").write_text(json.dumps(payload))

    async def first(state):
        pytest.fail("No effects")

    async def last(state):
        pytest.fail("No effects")

    engine = (
        PipelineBuilder("integrity", state=State, checkpointer=FileCheckpointer(tmp_path))
        .add_node(first)
        .add_node(last)
        .chain(first, last)
        .build()
    )
    with pytest.raises(PipelineError, match="^Pipeline checkpoint is invalid$"):
        await engine.run(run_id="run")


@pytest.mark.parametrize("pause_first", [True, False])
@pytest.mark.parametrize("two_pauses", [True, False])
async def test_same_completed_batch_cannot_overwrite_pause_barrier(tmp_path, monkeypatch, pause_first, two_pauses):
    import asyncio

    from pyfly.agentic.pipeline import FileCheckpointer

    store = FileCheckpointer(tmp_path)
    calls = {"gate": 0, "peer": 0, "end": 0}
    completed, paused = [], []
    original_wait = asyncio.wait

    async def ordered_wait(tasks, **kwargs):
        done, pending = await original_wait(tasks, return_when=asyncio.ALL_COMPLETED)
        return sorted(
            done,
            key=lambda task: (task.result().node_id != "gate") if pause_first else (task.result().node_id == "gate"),
        ), pending

    monkeypatch.setattr(asyncio, "wait", ordered_wait)

    class Events:
        async def on_node_complete(self, node_id):
            completed.append(node_id)

        async def on_node_pause(self, node_id, run_id):
            latest = store.load_latest("batch", run_id)
            assert latest.paused
            paused.append(node_id)

    async def gate(state):
        calls["gate"] += 1
        return Pause("first approval")

    async def peer(state):
        calls["peer"] += 1
        return Pause("second approval") if two_pauses else {"value": 1}

    async def end(state):
        calls["end"] += 1
        return {}

    def build():
        return (
            PipelineBuilder("batch", state=State, checkpointer=store, event_handler=Events())
            .add_node(gate)
            .add_node(peer)
            .add_node(end)
            .add_edge(gate, end)
            .add_edge(peer, end)
            .build()
        )

    first = await build().run(state=State())
    assert first.paused and not first.success
    saved = store.load_latest("batch", first.run_id)
    assert saved.paused
    assert set(saved.completed_nodes) == {"gate", "peer"}
    assert set(saved.state["results"]) == {"gate", "peer"}
    assert completed == ([] if two_pauses else ["peer"])
    assert set(paused) == ({"gate", "peer"} if two_pauses else {"gate"})
    with pytest.raises(PipelineError, match="approve_pause=True"):
        await build().run(run_id=first.run_id)
    approved = await build().run(run_id=first.run_id, approve_pause=True)
    assert approved.success
    assert calls == {"gate": 1, "peer": 1, "end": 1}
