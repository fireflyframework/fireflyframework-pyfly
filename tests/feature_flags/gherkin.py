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
"""A minimal Gherkin reader for the flagd-testbed evaluator suite: scenario outlines expanded, tables kept."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

_KEYWORD = re.compile(r"^(Given|When|Then|And|But)\s+")
_PLACEHOLDER = re.compile(r"<([^>]+)>")


@dataclass
class Step:
    text: str
    table: list[list[str]] = field(default_factory=list)


@dataclass
class Scenario:
    feature: str
    name: str
    tags: frozenset[str]
    steps: list[Step]

    @property
    def id(self) -> str:
        return f"{self.feature}::{self.name}"


def _cells(line: str) -> list[str]:
    return [cell.strip() for cell in line.strip().strip("|").split("|")]


def _substitute(text: str, values: dict[str, str]) -> str:
    return _PLACEHOLDER.sub(lambda match: values[match.group(1)], text)


def read_feature(path: Path) -> list[Scenario]:
    """Every concrete scenario of *path*, background steps first, outline placeholders substituted."""
    feature_tags: frozenset[str] = frozenset()
    pending: set[str] = set()
    background: list[Step] = []
    outlines: list[dict[str, Any]] = []
    steps: list[Step] = background
    examples: list[dict[str, Any]] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("@"):
            pending.update(line.split())
        elif line.startswith("Feature:"):
            feature_tags, pending = frozenset(pending), set()
        elif line.startswith("Background:"):
            steps = background
        elif line.startswith(("Scenario:", "Scenario Outline:")):
            steps, examples = [], []
            name = line.split(":", 1)[1].strip()
            outlines.append({"name": name, "tags": feature_tags | pending, "steps": steps, "examples": examples})
            pending = set()
        elif line.startswith("Examples"):
            examples.append({"tags": frozenset(pending), "rows": []})
            pending = set()
        elif line.startswith("|"):
            if examples:
                examples[-1]["rows"].append(_cells(line))
            else:
                steps[-1].table.append(_cells(line))
        else:
            steps.append(Step(_KEYWORD.sub("", line)))
    scenarios: list[Scenario] = []
    for outline in outlines:
        if not outline["examples"]:
            tags = frozenset(outline["tags"])
            scenarios.append(Scenario(path.name, outline["name"], tags, [*background, *outline["steps"]]))
            continue
        for example in outline["examples"]:
            header, *rows = example["rows"]
            for row in rows:
                values = dict(zip(header, row, strict=True))
                expanded = [Step(_substitute(step.text, values), step.table) for step in outline["steps"]]
                tags = frozenset(outline["tags"] | example["tags"])
                name = f"{outline['name']} [{', '.join(row)}]"
                scenarios.append(Scenario(path.name, name, tags, [*background, *expanded]))
    return scenarios
