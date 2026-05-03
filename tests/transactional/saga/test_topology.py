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
"""Tests for saga topology — DAG layer computation."""

from __future__ import annotations

import pytest

from pyfly.transactional.saga.engine.topology import SagaTopology


class TestSagaTopology:
    def test_linear_chain(self) -> None:
        # A -> B -> C
        deps = {"A": [], "B": ["A"], "C": ["B"]}
        layers = SagaTopology.compute_layers(deps)
        assert layers == [["A"], ["B"], ["C"]]

    def test_parallel_roots(self) -> None:
        # A and B are independent, C depends on both
        deps = {"A": [], "B": [], "C": ["A", "B"]}
        layers = SagaTopology.compute_layers(deps)
        assert len(layers) == 2
        assert set(layers[0]) == {"A", "B"}
        assert layers[1] == ["C"]

    def test_diamond_dag(self) -> None:
        # A -> B, A -> C, B -> D, C -> D
        deps = {"A": [], "B": ["A"], "C": ["A"], "D": ["B", "C"]}
        layers = SagaTopology.compute_layers(deps)
        assert layers[0] == ["A"]
        assert set(layers[1]) == {"B", "C"}
        assert layers[2] == ["D"]

    def test_single_step(self) -> None:
        deps = {"only": []}
        layers = SagaTopology.compute_layers(deps)
        assert layers == [["only"]]

    def test_cycle_raises(self) -> None:
        deps = {"A": ["B"], "B": ["A"]}
        with pytest.raises(ValueError, match="cycle"):
            SagaTopology.compute_layers(deps)

    def test_empty(self) -> None:
        layers = SagaTopology.compute_layers({})
        assert layers == []
