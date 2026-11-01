# Lab Guide

Use these capability snippets inside your application’s [native runtime](native-runtime.md).
Installation is through [PyFly Agentic extras](getting-started/installation.md).

Copyright 2026 Firefly Software Foundation. Licensed under the Apache License 2.0.

The Lab module provides interactive sessions, benchmarking, side-by-side comparison,
dataset management, and pluggable evaluators for developing and testing GenAI agents.

---

## Concepts

The Lab is a development and testing environment where engineers can iterate on agent
behaviour without wiring agents into a host service. It supports interactive exploration,
reproducible benchmarks, and structured evaluation.

`lab` is an **optional leaf dev-tooling module**: nothing in the core framework imports it,
and it ships with the package (there is no separate extra to install). Use it from scripts,
notebooks, or tests; keep it out of production code paths.

```mermaid
flowchart TD
    subgraph Lab
        SESSION[LabSession]
        BENCH[Benchmark]
        CMP[ModelComparison]
        DS[EvalDataset]
        ORCH[EvalOrchestrator]
        SCORE[Scorer]
    end

    SESSION --> AGENT[Agent]
    BENCH --> AGENT
    CMP --> AGENT
    ORCH --> AGENT
    DS --> ORCH
    SCORE --> ORCH
```

---

## Interactive Sessions

A `LabSession` provides a conversational interface for testing an agent interactively.
Each exchange is recorded with timestamps and metadata.

```python
from pyfly.agentic.lab import LabSession

session = LabSession(name="writing-test", agent=writer_agent)
print(session.name)  # "writing-test"

response = await session.interact("Write a haiku about testing.")
print(response)

# Review the session history
for entry in session.history:
    print(f"[{entry.timestamp}] {entry.prompt} -> {entry.response}")

# Reset between experiments
session.clear_history()
```

---

## Benchmarking

The `Benchmark` runs an agent against a list of prompts and measures **latency only**
(average, min, max, p95). It does not score outputs — for correctness scoring against
expected answers, use the `EvalOrchestrator` described below.

`Benchmark.run(agent, agent_name="")` returns a `BenchmarkResult`. The optional
`agent_name` labels the result; when omitted it falls back to `getattr(agent, "name",
"unknown")`.

```python
from pyfly.agentic.lab import Benchmark

bench = Benchmark(inputs=[
    "Translate 'hello' to French.",
    "Translate 'goodbye' to French.",
])
result = await bench.run(translator_agent, agent_name="translator")
print(f"Runs: {result.total_runs}")
print(f"Avg latency: {result.avg_latency_ms:.1f} ms, P95: {result.p95_latency_ms:.1f} ms")
```

```mermaid
sequenceDiagram
    participant Bench as Benchmark
    participant Agent

    loop For each input prompt
        Bench->>Bench: start timer
        Bench->>Agent: run(prompt)
        Agent-->>Bench: result
        Bench->>Bench: record elapsed ms
    end
    Bench->>Bench: aggregate latency (avg/min/max/p95)
```

---

## Side-by-Side Comparison

The `ModelComparison` class runs the same prompts through multiple agents and collects
their outputs side by side.

```python
from pyfly.agentic.lab import ModelComparison

comparison = ModelComparison(prompts=[
    "Write a haiku about Python.",
    "Explain recursion in one sentence.",
])
entries = await comparison.compare({"writer_v1": agent_v1, "writer_v2": agent_v2})
for entry in entries:
    print(f"Input: {entry.prompt}")
    for agent_name, response in entry.responses.items():
        print(f" {agent_name}: {response}")
```

---

## Datasets

The `EvalDataset` class manages collections of `EvalCase` test inputs and optional
expected outputs. Datasets can be loaded from JSON files or built programmatically.

```python
from pyfly.agentic.lab import EvalDataset, EvalCase

# Programmatically
dataset = EvalDataset(cases=[
    EvalCase(input="Hello", expected_output="Hi"),
    EvalCase(input="Goodbye", expected_output="Bye"),
])

# Append cases incrementally
dataset.add(EvalCase(input="Thanks", expected_output="You're welcome"))
print(len(dataset))  # 3

# Persist to / load from a JSON file
dataset.to_json("test_data.json")
dataset = EvalDataset.from_json("test_data.json")
```

---

## Structured process inputs

Lab inputs can be text or a JSON object through the shared `LabInput` type. An
object retains its nested lists, numbers, booleans and null values in datasets,
evaluation reports, comparison entries and session history. `to_json` and
`from_json` preserve that structure; a field named `input` inside the object is
ordinary business data, not an instruction to unwrap the payload.

Lab validates inputs before invoking the subject. Non-JSON values and nonfinite
numbers are rejected. Each invocation receives its own copy so a process that
changes its input cannot alter the dataset, another comparison subject's input,
or the original input recorded in the report. Configured batches are validated
before their first invocation.

This contract describes the input to the **evaluation subject**. A concrete
PyFly or PydanticAI agent still accepts its supported text or multimodal prompt.
To evaluate a process that accepts a business object, adapt the configured native
pipeline explicitly. Pass the object as `inputs=` to `PipelineEngine.run`; its
first positional argument has a different purpose.

The following adapter evaluates an existing native engine and exposes its final
output to the existing string scorer. It fails the evaluation if the pipeline
fails, rather than scoring a failed execution as an ordinary response.

```python
from types import SimpleNamespace
from typing import Any

from pyfly.agentic.lab import EvalCase, EvalDataset, EvalOrchestrator, LabInput
from pyfly.agentic.pipeline import PipelineEngine


class PipelineSubject:
    def __init__(self, engine: PipelineEngine) -> None:
        self.engine = engine

    async def run(self, prompt: LabInput, **kwargs: Any) -> SimpleNamespace:
        result = await self.engine.run(inputs=prompt, **kwargs)
        if not result.success:
            raise RuntimeError("The evaluated pipeline did not complete successfully")
        return SimpleNamespace(output=result.final_output)


async def evaluate_pipeline(engine: PipelineEngine):
    dataset = EvalDataset([
        EvalCase(
            input={"input": {"order_id": "order-42", "quantity": 2}},
            expected_output="approved",
        ),
    ])
    report = await EvalOrchestrator().evaluate(
        PipelineSubject(engine), dataset, agent_name="order-review"
    )
    assert report.results[0].input == dataset.cases[0].input
    return report
```

The supplied engine must implement the order-review process and validate its
business schema. Lab's JSON validation does not replace application permissions,
resource limits, secret handling or runtime admission. Keep those checks on the
same native execution path used outside evaluation. Lab records inputs in its
reports and dataset files, so use evaluation data appropriate for their access
and retention policy.

`Benchmark`, `ModelComparison` and `LabSession` accept the same input contract
when their subjects support it. Expected outputs, actual outputs and custom
scorers remain strings; structured input does not silently change scoring
semantics or add a structured-output evaluator.

---

## Evaluators

The `EvalOrchestrator` runs an agent against an `EvalDataset` with a pluggable scorer
function. A `Scorer` is any callable `(expected: str, actual: str) -> float`. When no
scorer is supplied, the orchestrator uses `exact_match_scorer`, which returns `1.0` when
the stripped expected and actual strings are equal and `0.0` otherwise.

`evaluate(agent, dataset, agent_name="")` returns an `EvalReport` whose `results` list
holds one `EvalResult` (`input`, `expected`, `actual`, `score`) per case. The optional
`agent_name` labels the report and falls back to `getattr(agent, "name", "unknown")`.

```python
from pyfly.agentic.lab import EvalOrchestrator, EvalDataset, EvalCase
from pyfly.agentic.lab.evaluator import exact_match_scorer

# Default exact-match scoring
orchestrator = EvalOrchestrator()  # uses exact_match_scorer

# ...or a custom scorer
def fuzzy_scorer(expected: str, actual: str) -> float:
    return 1.0 if expected.lower() in actual.lower() else 0.0

orchestrator = EvalOrchestrator(scorer=fuzzy_scorer)
report = await orchestrator.evaluate(my_agent, dataset, agent_name="writer-v2")
print(f"Avg score: {report.avg_score:.2f} across {report.total_cases} cases")
for r in report.results:
    print(f"{r.input!r}: expected {r.expected!r}, got {r.actual!r} (score {r.score})")
```

> `exact_match_scorer` and the `Scorer` type alias live in
> `pyfly.agentic.lab.evaluator` (import them from that submodule); they are not
> re-exported from the `pyfly.agentic.lab` package and are not part of its `__all__`.
