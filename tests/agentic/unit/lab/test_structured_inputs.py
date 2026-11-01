# Copyright 2026 Firefly Software Foundation
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

"""Portable structured Lab inputs preserve provenance through the native pipeline."""

from copy import deepcopy
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from pydantic import TypeAdapter, ValidationError

from pyfly.agentic.lab import Benchmark, EvalCase, EvalDataset, EvalOrchestrator, LabSession, ModelComparison
from pyfly.agentic.pipeline import CallableStep, PipelineBuilder, PipelineEngine

PAYLOAD = {"input": "business field", "nested": {"values": [1, True, None, {"amount": 2.5}]}, "label": "café"}


class MutatingRunner:
    def __init__(self):
        self.seen = []

    async def run(self, prompt, **kwargs):
        self.seen.append(deepcopy(prompt))
        if isinstance(prompt, dict):
            prompt["nested"]["values"].append("changed by runner")
        return SimpleNamespace(output="done")


def test_dataset_nested_json_roundtrip_and_public_type(tmp_path):
    from pyfly.agentic.lab import LabInput

    assert TypeAdapter(LabInput).validate_python(PAYLOAD) == PAYLOAD
    original = deepcopy(PAYLOAD)
    dataset = EvalDataset([EvalCase(input=original, expected_output="done", metadata={"suite": "native"})])
    path = tmp_path / "cases.json"
    dataset.to_json(path)
    loaded = EvalDataset.from_json(path)
    assert loaded.cases[0].input == PAYLOAD
    assert isinstance(loaded.cases[0].input, dict)
    assert loaded.cases[0].expected_output == "done"
    assert loaded.cases[0].metadata == {"suite": "native"}


def test_dataset_export_preserves_existing_case_subclass_fields(tmp_path):
    import json

    class TaggedCase(EvalCase):
        tag: str

    path = tmp_path / "subclass.json"
    EvalDataset([TaggedCase(input="legacy", expected_output="done", tag="retained")]).to_json(path)
    assert json.loads(path.read_text())[0]["tag"] == "retained"


@pytest.mark.parametrize(
    "value",
    [
        None,
        True,
        1,
        ["text"],
        ("text",),
        b"text",
        {1: "coercion prohibited"},
        {"bad": float("nan")},
        {"bad": float("inf")},
        {"bad": float("-inf")},
        {"bad": object()},
        {"bad": datetime(2026, 1, 1, tzinfo=UTC)},
        {"bad": (1, 2)},
        {"bad": {1, 2}},
    ],
)
def test_input_rejects_non_object_or_nonfinite_non_json_data(value):
    with pytest.raises(ValidationError):
        EvalCase(input=value)


def test_input_rejects_cyclic_objects():
    value = {}
    value["cycle"] = value
    with pytest.raises(ValidationError):
        EvalCase(input=value)


async def test_native_pipeline_receives_original_object_and_report_survives_runner_mutation():
    received = []

    async def step(context, inputs):
        received.append(deepcopy(context.inputs))
        assert inputs["input"] == PAYLOAD
        context.inputs["nested"]["values"].append("pipeline mutation")
        return "done"

    builder = PipelineBuilder("native-lab")
    builder.add_node("work", CallableStep(step))
    engine = PipelineEngine(builder.build_dag())

    class NativeRunner:
        async def run(self, prompt, **kwargs):
            result = await engine.run(inputs=prompt)
            assert result.success
            return SimpleNamespace(output=result.final_output)

    caller_value = deepcopy(PAYLOAD)
    dataset = EvalDataset([EvalCase(input=caller_value, expected_output=" done ")])
    scores = []

    def scorer(expected, actual):
        scores.append((expected, actual))
        return 1.0 if expected.strip() == actual else 0.0

    report = await EvalOrchestrator(scorer).evaluate(NativeRunner(), dataset, agent_name="native")
    assert received == [PAYLOAD]
    assert dataset.cases[0].input == caller_value == PAYLOAD
    assert report.results[0].input == PAYLOAD
    assert report.results[0].actual == "done" and report.results[0].score == 1.0
    assert report.total_cases == 1 and report.avg_score == 1.0
    assert scores == [(" done ", "done")]
    assert type(report).model_validate_json(report.model_dump_json()).results[0].input == PAYLOAD
    dataset.cases[0].input["nested"]["values"].append("edited after evaluation")
    assert report.results[0].input == PAYLOAD


async def test_evaluation_validates_entire_mutated_dataset_before_any_work():
    runner = MutatingRunner()
    second = EvalCase.model_construct(input={"bad": object()}, expected_output="done")
    with pytest.raises((ValidationError, ValueError)):
        await EvalOrchestrator().evaluate(runner, EvalDataset([EvalCase(input="first"), second]))
    assert runner.seen == []


def test_serialization_rejects_mutated_invalid_cases_before_writing(tmp_path):
    case = EvalCase.model_construct(input={"bad": object()})
    path = tmp_path / "invalid.json"
    with pytest.raises((ValidationError, ValueError)):
        EvalDataset([case]).to_json(path)
    assert not path.exists()


async def test_benchmark_owns_configured_objects_and_isolates_each_execution():
    original = deepcopy(PAYLOAD)
    benchmark = Benchmark([original])
    original["nested"]["values"].append("caller mutation")
    runner = MutatingRunner()
    first = await benchmark.run(runner)
    second = await benchmark.run(runner)
    assert first.total_runs == second.total_runs == 1
    assert runner.seen == [PAYLOAD, PAYLOAD]


async def test_benchmark_excludes_input_copy_cost_from_agent_latency(monkeypatch):
    from pyfly.agentic.lab import benchmark as module

    clock = [0.0]
    copy = module.deepcopy

    def slow_copy(value):
        clock[0] += 0.005
        return copy(value)

    class Runner:
        async def run(self, prompt):
            clock[0] += 0.002
            return "done"

    monkeypatch.setattr(module, "deepcopy", slow_copy)
    monkeypatch.setattr(module.time, "perf_counter", lambda: clock[0])
    result = await Benchmark([PAYLOAD]).run(Runner())
    assert result.avg_latency_ms == pytest.approx(2.0)


async def test_comparison_isolates_each_agent_and_preserves_recorded_input():
    original = deepcopy(PAYLOAD)
    comparison = ModelComparison([original])
    original["nested"]["values"].append("caller mutation before comparison")
    first, second = MutatingRunner(), MutatingRunner()
    entries = await comparison.compare({"one": first, "two": second})
    original["nested"]["values"].append("caller mutation")
    assert first.seen == second.seen == [PAYLOAD]
    assert entries[0].prompt == PAYLOAD
    assert entries[0].responses == {"one": "done", "two": "done"}


@pytest.mark.parametrize("kind", ["benchmark", "comparison"])
async def test_invalid_configured_batch_is_rejected_before_any_calls(kind):
    runner = MutatingRunner()
    with pytest.raises((ValidationError, ValueError)):
        if kind == "benchmark":
            await Benchmark(["valid first", {"bad": object()}]).run(runner)
        else:
            await ModelComparison(["valid first", {"bad": object()}]).compare({"runner": runner})
    assert runner.seen == []


async def test_session_keeps_original_history_and_rejects_invalid_input_before_work():
    runner = MutatingRunner()
    session = LabSession("native", runner)
    original = deepcopy(PAYLOAD)
    assert await session.interact(original) == "done"
    original["nested"]["values"].append("caller mutation")
    assert session.history[0].prompt == PAYLOAD
    with pytest.raises((ValidationError, ValueError)):
        await session.interact({"bad": object()})
    assert runner.seen == [PAYLOAD]
    assert len(session.history) == 1


async def test_text_contracts_outputs_and_scorers_remain_backward_compatible():
    runner = MutatingRunner()
    dataset = EvalDataset([EvalCase(input="", expected_output=" done ")])
    report = await EvalOrchestrator().evaluate(runner, dataset)
    assert report.results[0].input == "" and report.avg_score == 1.0
    assert (await Benchmark(["hello"]).run(runner)).total_runs == 1
    assert (await ModelComparison(["hello"]).compare({"runner": runner}))[0].prompt == "hello"
    session = LabSession("legacy", runner)
    assert await session.interact("hello") == "done"
    assert session.history[0].prompt == "hello"
