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
"""Saga topology — DAG topological sort into execution layers via Kahn's algorithm."""

from __future__ import annotations

from collections import defaultdict, deque


class SagaTopology:
    """Computes execution layers from a dependency graph using Kahn's algorithm.

    Each layer contains step IDs that can execute concurrently because all of
    their dependencies have been satisfied in earlier layers.  Within each
    layer, IDs are sorted lexicographically for deterministic output.
    """

    @staticmethod
    def compute_layers(deps: dict[str, list[str]]) -> list[list[str]]:
        """Compute execution layers from a dependency map.

        Parameters
        ----------
        deps:
            Mapping of ``step_id -> [dependency_ids]``.  Every step must
            appear as a key even if it has no dependencies (empty list).

        Returns
        -------
        list[list[str]]
            Ordered list of layers.  Each layer is a sorted list of step IDs
            whose dependencies are satisfied by all preceding layers.

        Raises
        ------
        ValueError
            If the dependency graph contains a cycle.
        """
        if not deps:
            return []

        # -- 1. Build adjacency list and in-degree map ----------------------
        in_degree: dict[str, int] = {node: 0 for node in deps}
        adjacency: dict[str, list[str]] = defaultdict(list)

        for node, predecessors in deps.items():
            for pred in predecessors:
                adjacency[pred].append(node)
                in_degree[node] += 1

        # -- 2. Seed the queue with all zero in-degree nodes ----------------
        queue: deque[str] = deque(
            sorted(node for node, deg in in_degree.items() if deg == 0),
        )

        layers: list[list[str]] = []
        processed = 0

        # -- 3. BFS layer by layer -----------------------------------------
        while queue:
            layer = sorted(queue)
            layers.append(layer)
            processed += len(layer)

            next_queue: deque[str] = deque()
            for node in layer:
                for neighbour in adjacency[node]:
                    in_degree[neighbour] -= 1
                    if in_degree[neighbour] == 0:
                        next_queue.append(neighbour)

            queue = deque(sorted(next_queue))

        # -- 4. Cycle detection ---------------------------------------------
        if processed != len(deps):
            raise ValueError(
                f"Dependency graph contains a cycle — processed {processed} of {len(deps)} steps",
            )

        return layers
