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
"""DAG topology builder used by saga and workflow execution.

Mirrors ``org.fireflyframework.orchestration.core.topology.TopologyBuilder`` —
Kahn's BFS over step ``depends_on`` declarations to produce concurrency layers.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Mapping


class TopologyError(Exception):
    """Raised when the dependency graph is malformed (cycles, dangling refs)."""


class TopologyBuilder:
    """Compute execution layers from a DAG of step ids."""

    @staticmethod
    def build_layers(graph: Mapping[str, list[str]]) -> list[list[str]]:
        """Return a list of layers; nodes in the same layer can run in parallel.

        Args:
            graph: Adjacency map ``{step_id: [dependency_step_ids]}``.

        Raises:
            TopologyError: On missing dependencies or cycles.
        """
        all_ids = set(graph.keys())
        # Validate references
        for sid, deps in graph.items():
            for dep in deps:
                if dep not in all_ids:
                    msg = f"step '{sid}' depends on unknown step '{dep}'"
                    raise TopologyError(msg)

        in_degree: dict[str, int] = {sid: len(deps) for sid, deps in graph.items()}
        reverse: dict[str, list[str]] = {sid: [] for sid in all_ids}
        for sid, deps in graph.items():
            for dep in deps:
                reverse[dep].append(sid)

        layers: list[list[str]] = []
        ready: deque[str] = deque(sorted(sid for sid, d in in_degree.items() if d == 0))
        processed = 0

        while ready:
            current_layer = sorted(ready)
            layers.append(current_layer)
            ready.clear()
            for sid in current_layer:
                processed += 1
                for child in reverse[sid]:
                    in_degree[child] -= 1
                    if in_degree[child] == 0:
                        ready.append(child)

        if processed != len(all_ids):
            msg = f"dependency cycle detected (processed {processed}/{len(all_ids)} steps)"
            raise TopologyError(msg)
        return layers
