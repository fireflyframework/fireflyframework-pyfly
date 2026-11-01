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

"""Side-by-side model or prompt comparison."""

from __future__ import annotations

from collections.abc import Sequence
from copy import deepcopy
from typing import Any

from pydantic import BaseModel, ConfigDict

from pyfly.agentic.lab._inputs import LabInput, snapshot_input


class ComparisonEntry(BaseModel):
    """A single comparison data point."""

    model_config = ConfigDict(hide_input_in_errors=True)

    prompt: LabInput
    responses: dict[str, str] = {}  # agent_name -> response


class ModelComparison:
    """Run the same prompts across multiple agents and collect responses.

    Parameters:
        prompts: Test prompts.
    """

    def __init__(self, prompts: Sequence[LabInput]) -> None:
        self._prompts = [snapshot_input(value) for value in prompts]

    async def compare(self, agents: dict[str, Any]) -> list[ComparisonEntry]:
        """Run every prompt through every agent and return entries."""
        entries: list[ComparisonEntry] = []
        prompts = [snapshot_input(value) for value in self._prompts]
        for prompt in prompts:
            responses: dict[str, str] = {}
            for name, agent in agents.items():
                result = await agent.run(deepcopy(prompt))
                responses[name] = str(result.output if hasattr(result, "output") else result)
            entries.append(ComparisonEntry(prompt=prompt, responses=responses))
        return entries
