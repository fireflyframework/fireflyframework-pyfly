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

"""Evaluation orchestrator for running agents against datasets with scoring."""

from __future__ import annotations

from collections.abc import Callable
from copy import deepcopy

from pydantic import BaseModel, ConfigDict

from pyfly.agentic.lab._inputs import LabInput, snapshot_input
from pyfly.agentic.lab.dataset import EvalDataset
from pyfly.agentic.types import AgentLike

# A scorer takes (expected, actual) and returns a float score
Scorer = Callable[[str, str], float]


class EvalResult(BaseModel):
    """Result for a single evaluation case."""

    model_config = ConfigDict(hide_input_in_errors=True)

    input: LabInput
    expected: str
    actual: str
    score: float = 0.0


class EvalReport(BaseModel):
    """Summary report of an evaluation run."""

    agent_name: str
    total_cases: int = 0
    avg_score: float = 0.0
    results: list[EvalResult] = []


def exact_match_scorer(expected: str, actual: str) -> float:
    """Return 1.0 if expected equals actual, else 0.0."""
    return 1.0 if expected.strip() == actual.strip() else 0.0


class EvalOrchestrator:
    """Run evaluations with configurable scorers.

    Parameters:
        scorer: A callable ``(expected, actual) -> float``.
    """

    def __init__(self, scorer: Scorer | None = None) -> None:
        self._scorer = scorer or exact_match_scorer

    async def evaluate(self, agent: AgentLike, dataset: EvalDataset, agent_name: str = "") -> EvalReport:
        """Evaluate *agent* against *dataset*."""
        name = agent_name or getattr(agent, "name", "unknown")
        results: list[EvalResult] = []
        cases = [(snapshot_input(case.input), case.expected_output) for case in dataset.cases]

        for original, expected in cases:
            result = await agent.run(deepcopy(original))
            actual = str(result.output if hasattr(result, "output") else result)
            score = self._scorer(expected, actual)
            results.append(
                EvalResult(
                    input=original,
                    expected=expected,
                    actual=actual,
                    score=score,
                )
            )

        avg = sum(r.score for r in results) / len(results) if results else 0
        return EvalReport(
            agent_name=name,
            total_cases=len(results),
            avg_score=avg,
            results=results,
        )
