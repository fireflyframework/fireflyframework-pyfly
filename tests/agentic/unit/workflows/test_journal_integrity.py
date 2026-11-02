# Copyright 2026 Firefly Software Foundation
# SPDX-License-Identifier: Apache-2.0
"""A journal acknowledgement follows persistence, never a best-effort cache write."""

import asyncio
from copy import deepcopy

import pytest

from pyfly.agentic.exceptions import WorkflowError
from pyfly.agentic.pipeline import PipelineBuilder
from pyfly.agentic.workflows import AgentCall, FileJournalBackend, Journal, agent, parallel, pipeline, workflow

CANARY = "private-journal-content-do-not-log"


class Backend:
    def __init__(self, entries=None, *, load_error=None, save_error=None, mutate=False):
        self.entries = {} if entries is None else entries
        self.load_error, self.save_error, self.mutate = load_error, save_error, mutate
        self.saves = 0

    def load(self, run_key):
        if self.load_error:
            raise self.load_error
        return self.entries

    def save(self, run_key, entries):
        self.saves += 1
        if self.mutate:
            entries[0]["nested"].append("backend mutation")
        if self.save_error:
            raise self.save_error
        self.entries = entries


@pytest.mark.parametrize("error", [OSError(CANARY), ValueError(CANARY)])
def test_failed_load_never_starts_empty_history(error, caplog):
    with pytest.raises(WorkflowError, match="^Workflow journal load failed$") as raised:
        Journal(backend=Backend(load_error=error), run_key="run")
    assert raised.value.retryable is False
    assert CANARY not in caplog.text


@pytest.mark.parametrize("entries", [{-1: "bad"}, {True: "bad"}, {"0": "bad"}, ["bad"], None])
def test_malformed_backend_history_is_rejected(entries):
    backend = Backend()
    backend.entries = entries
    with pytest.raises(WorkflowError, match="^Workflow journal is invalid$"):
        Journal(backend=backend, run_key="run")


def test_failed_save_preserves_nested_acknowledged_cache_and_new_output(caplog):
    original = {0: {"nested": ["acknowledged"]}}
    backend = Backend(deepcopy(original), save_error=OSError(CANARY), mutate=True)
    journal = Journal(backend=backend, run_key="run")
    output = {"new": ["unacknowledged"]}
    with pytest.raises(WorkflowError, match="^Workflow journal save failed$") as raised:
        journal.record(1, output)
    assert raised.value.retryable is False
    assert journal.to_dict() == original
    assert not journal.has(1)
    assert output == {"new": ["unacknowledged"]}
    assert CANARY not in caplog.text


def test_backend_and_input_aliases_cannot_mutate_acknowledged_cache():
    original = {0: {"nested": ["acknowledged"]}}
    backend = Backend(original)
    journal = Journal(backend=backend, run_key="run")
    original[0]["nested"].append("outside")
    assert journal.get(0) == {"nested": ["acknowledged"]}
    journal.record(1, {"new": []})
    backend.entries[1]["new"].append("outside")
    assert journal.get(1) == {"new": []}


class CountingRunner:
    def __init__(self):
        self.calls = []

    async def run(self, prompt, **kwargs):
        self.calls.append(prompt)
        return AgentCall(output={"answer": "counted"}, tokens=7)


@pytest.mark.parametrize("shape", ["sequential", "parallel", "pipeline", "node-retry"])
async def test_journal_error_never_replays_model_or_becomes_success(shape):
    runner = CountingRunner()
    journal = Journal(backend=Backend(save_error=OSError(CANARY)), run_key="run")

    @workflow(register=False)
    async def execute(args, ctx):
        if shape == "parallel":
            return await parallel([lambda: agent("first")])
        if shape == "pipeline":
            return await pipeline(["first"], lambda item: agent(item))
        await agent("first")
        return await agent("downstream")

    async def call(context, inputs):
        return await execute.run(runner=runner, journal=journal)

    with pytest.raises(WorkflowError, match="^Workflow journal save failed$") as raised:
        if shape == "node-retry":
            engine = PipelineBuilder("outer").add_node("call", call, retry_max=2).build()
            await engine.run(inputs="start")
        else:
            await execute.run(runner=runner, journal=journal)
    assert raised.value.retryable is False
    assert runner.calls == ["first"]
    assert journal.to_dict() == {}


def test_file_history_corruption_is_not_a_new_run(tmp_path):
    backend = FileJournalBackend(tmp_path)
    (tmp_path / "run.json").write_text('{"0":"first","00":"overwritten"}')
    with pytest.raises(WorkflowError, match="^Workflow journal"):
        Journal(backend=backend, run_key="run")


def test_cancelled_save_does_not_acknowledge_new_entry():
    journal = Journal(backend=Backend(save_error=asyncio.CancelledError()), run_key="run")
    with pytest.raises(asyncio.CancelledError):
        journal.record(0, "not acknowledged")
    assert journal.to_dict() == {}


def test_successful_but_mutating_backend_does_not_rewrite_accepted_value():
    backend = Backend({0: {"nested": ["accepted"]}}, mutate=True)
    journal = Journal(backend=backend, run_key="run")
    journal.record(1, "next")
    assert journal.to_dict() == {0: {"nested": ["accepted"]}, 1: "next"}


@pytest.mark.parametrize("run_key", [None, ""])
def test_configured_backend_needs_stable_nonempty_key(run_key):
    with pytest.raises(WorkflowError, match="^Workflow journal configuration is invalid$"):
        Journal(backend=Backend(), run_key=run_key)


async def test_successful_file_reopen_replays_typed_json_without_more_model_effects(tmp_path):
    backend = FileJournalBackend(tmp_path)
    runner = CountingRunner()

    @workflow(register=False)
    async def execute(args):
        return await agent("first")

    expected = {"answer": "counted"}
    assert await execute.run(runner=runner, journal=Journal(backend=backend, run_key="run")) == expected
    assert await execute.run(runner=runner, journal=Journal(backend=backend, run_key="run")) == expected
    assert runner.calls == ["first"]


def test_cancelled_load_does_not_create_replay_history():
    with pytest.raises(asyncio.CancelledError):
        Journal(backend=Backend(load_error=asyncio.CancelledError()), run_key="run")


@pytest.mark.parametrize("read", ["get", "to_dict"])
def test_durable_replay_read_cannot_rewrite_previously_acknowledged_history(read):
    backend = Backend({0: {"nested": ["acknowledged"]}})
    journal = Journal(backend=backend, run_key="run")
    replay = journal.get(0) if read == "get" else journal.to_dict()[0]
    replay["nested"].append("caller-owned change")
    assert replay["nested"] == ["acknowledged", "caller-owned change"]
    journal.record(1, "next")
    reopened = Journal(backend=backend, run_key="run")
    assert reopened.get(0) == {"nested": ["acknowledged"]}


@pytest.mark.parametrize("read", ["get", "to_dict"])
def test_durable_read_clone_failure_is_content_free(read, caplog):
    class Value:
        fail = False

        def __deepcopy__(self, memo):
            if self.fail:
                raise ValueError(CANARY)
            return Value()

    journal = Journal(backend=Backend({0: Value()}), run_key="run")
    Value.fail = True
    with pytest.raises(WorkflowError, match="^Workflow journal read failed$"):
        journal.get(0) if read == "get" else journal.to_dict()
    assert CANARY not in caplog.text
